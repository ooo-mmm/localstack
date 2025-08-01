import copy
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from localstack.aws.api import RequestContext, handler
from localstack.aws.api.cloudformation import (
    Changes,
    ChangeSetNameOrId,
    ChangeSetNotFoundException,
    ChangeSetStatus,
    ChangeSetType,
    ClientRequestToken,
    CreateChangeSetInput,
    CreateChangeSetOutput,
    CreateStackInput,
    CreateStackOutput,
    DeleteChangeSetOutput,
    DeletionMode,
    DescribeChangeSetOutput,
    DescribeStackEventsOutput,
    DescribeStackResourcesOutput,
    DescribeStacksOutput,
    DisableRollback,
    ExecuteChangeSetOutput,
    ExecutionStatus,
    GetTemplateOutput,
    GetTemplateSummaryInput,
    GetTemplateSummaryOutput,
    IncludePropertyValues,
    InsufficientCapabilitiesException,
    InvalidChangeSetStatusException,
    LogicalResourceId,
    NextToken,
    Parameter,
    PhysicalResourceId,
    RetainExceptOnCreate,
    RetainResources,
    RoleARN,
    RollbackConfiguration,
    StackName,
    StackNameOrId,
    StackStatus,
    TemplateStage,
    UpdateStackInput,
    UpdateStackOutput,
)
from localstack.services.cloudformation import api_utils
from localstack.services.cloudformation.engine import template_preparer
from localstack.services.cloudformation.engine.v2.change_set_model import (
    ChangeSetModel,
    ChangeType,
    UpdateModel,
)
from localstack.services.cloudformation.engine.v2.change_set_model_describer import (
    ChangeSetModelDescriber,
)
from localstack.services.cloudformation.engine.v2.change_set_model_executor import (
    ChangeSetModelExecutor,
)
from localstack.services.cloudformation.engine.v2.change_set_model_transform import (
    ChangeSetModelTransform,
)
from localstack.services.cloudformation.engine.validations import ValidationError
from localstack.services.cloudformation.provider import (
    ARN_CHANGESET_REGEX,
    ARN_STACK_REGEX,
    CloudformationProvider,
)
from localstack.services.cloudformation.stores import (
    CloudFormationStore,
    get_cloudformation_store,
)
from localstack.services.cloudformation.v2.entities import ChangeSet, Stack
from localstack.utils.threads import start_worker_thread

LOG = logging.getLogger(__name__)


def is_stack_arn(stack_name_or_id: str) -> bool:
    return ARN_STACK_REGEX.match(stack_name_or_id) is not None


def is_changeset_arn(change_set_name_or_id: str) -> bool:
    return ARN_CHANGESET_REGEX.match(change_set_name_or_id) is not None


class StackWithNameNotFoundError(ValidationError):
    def __init__(self, stack_name: str):
        super().__init__(f"Stack [{stack_name}] does not exist")


class StackWithIdNotFoundError(ValidationError):
    def __init__(self, stack_id: str):
        super().__init__("Stack with id <stack-name> does not exist")


def find_stack_v2(state: CloudFormationStore, stack_name: str | None) -> Stack | None:
    if stack_name:
        if is_stack_arn(stack_name):
            return state.stacks_v2[stack_name]
        else:
            stack_candidates = []
            for stack in state.stacks_v2.values():
                if stack.stack_name == stack_name and stack.status != StackStatus.DELETE_COMPLETE:
                    stack_candidates.append(stack)
            if len(stack_candidates) == 0:
                return None
            elif len(stack_candidates) > 1:
                raise RuntimeError("Programing error, duplicate stacks found")
            else:
                return stack_candidates[0]
    else:
        raise ValueError("No stack name specified when finding stack")


def find_change_set_v2(
    state: CloudFormationStore, change_set_name: str, stack_name: str | None = None
) -> ChangeSet | None:
    if is_changeset_arn(change_set_name):
        return state.change_sets[change_set_name]
    else:
        if stack_name is not None:
            stack = find_stack_v2(state, stack_name)
            if not stack:
                raise StackWithNameNotFoundError(stack_name)

            for change_set_id in stack.change_set_ids:
                change_set_candidate = state.change_sets[change_set_id]
                if change_set_candidate.change_set_name == change_set_name:
                    return change_set_candidate
        else:
            raise ValueError("No stack name specified when finding change set")


class CloudformationProviderV2(CloudformationProvider):
    @staticmethod
    def _setup_change_set_model(
        change_set: ChangeSet,
        before_template: Optional[dict],
        after_template: Optional[dict],
        before_parameters: Optional[dict],
        after_parameters: Optional[dict],
        previous_update_model: Optional[UpdateModel],
    ):
        # Create and preprocess the update graph for this template update.
        change_set_model = ChangeSetModel(
            before_template=before_template,
            after_template=after_template,
            before_parameters=before_parameters,
            after_parameters=after_parameters,
        )
        raw_update_model: UpdateModel = change_set_model.get_update_model()
        # If there exists an update model which operated in the 'before' version of this change set,
        # port the runtime values computed for the before version into this latest update model.
        if previous_update_model:
            raw_update_model.before_runtime_cache.clear()
            raw_update_model.before_runtime_cache.update(previous_update_model.after_runtime_cache)
        change_set.set_update_model(raw_update_model)

        # Apply global transforms.
        # TODO: skip this process iff both versions of the template don't specify transform blocks.
        change_set_model_transform = ChangeSetModelTransform(
            change_set=change_set,
            before_parameters=before_parameters,
            after_parameters=after_parameters,
            before_template=before_template,
            after_template=after_template,
        )
        transformed_before_template, transformed_after_template = (
            change_set_model_transform.transform()
        )

        # Remodel the update graph after the applying the global transforms.
        change_set_model = ChangeSetModel(
            before_template=transformed_before_template,
            after_template=transformed_after_template,
            before_parameters=before_parameters,
            after_parameters=after_parameters,
        )
        update_model = change_set_model.get_update_model()
        # Bring the cache for the previous operations forward in the update graph for this version
        # of the templates. This enables downstream update graph visitors to access runtime
        # information computed whilst evaluating the previous version of this template, and during
        # the transformations.
        update_model.before_runtime_cache.update(raw_update_model.before_runtime_cache)
        update_model.after_runtime_cache.update(raw_update_model.after_runtime_cache)
        change_set.set_update_model(update_model)
        change_set.stack.processed_template = transformed_after_template

    @handler("CreateChangeSet", expand=False)
    def create_change_set(
        self, context: RequestContext, request: CreateChangeSetInput
    ) -> CreateChangeSetOutput:
        try:
            stack_name = request["StackName"]
        except KeyError:
            # TODO: proper exception
            raise ValidationError("StackName must be specified")
        try:
            change_set_name = request["ChangeSetName"]
        except KeyError:
            # TODO: proper exception
            raise ValidationError("StackName must be specified")

        state = get_cloudformation_store(context.account_id, context.region)

        change_set_type = request.get("ChangeSetType", "UPDATE")
        template_body = request.get("TemplateBody")
        # s3 or secretsmanager url
        template_url = request.get("TemplateURL")

        # validate and resolve template
        if template_body and template_url:
            raise ValidationError(
                "Specify exactly one of 'TemplateBody' or 'TemplateUrl'"
            )  # TODO: check proper message

        if not template_body and not template_url:
            raise ValidationError(
                "Specify exactly one of 'TemplateBody' or 'TemplateUrl'"
            )  # TODO: check proper message

        template_body = api_utils.extract_template_body(request)
        structured_template = template_preparer.parse_template(template_body)

        # this is intentionally not in a util yet. Let's first see how the different operations deal with these before generalizing
        # handle ARN stack_name here (not valid for initial CREATE, since stack doesn't exist yet)
        if is_stack_arn(stack_name):
            stack = state.stacks_v2.get(stack_name)
            if not stack:
                raise ValidationError(f"Stack '{stack_name}' does not exist.")
        else:
            # stack name specified, so fetch the stack by name
            stack_candidates: list[Stack] = [
                s for stack_arn, s in state.stacks_v2.items() if s.stack_name == stack_name
            ]
            active_stack_candidates = [s for s in stack_candidates if s.is_active()]

            # on a CREATE an empty Stack should be generated if we didn't find an active one
            if not active_stack_candidates and change_set_type == ChangeSetType.CREATE:
                stack = Stack(
                    account_id=context.account_id,
                    region_name=context.region,
                    request_payload=request,
                    template=structured_template,
                    template_body=template_body,
                )
                state.stacks_v2[stack.stack_id] = stack
            else:
                if not active_stack_candidates:
                    raise ValidationError(f"Stack '{stack_name}' does not exist.")
                stack = active_stack_candidates[0]

        if stack.status in [StackStatus.CREATE_COMPLETE, StackStatus.UPDATE_COMPLETE]:
            stack.set_stack_status(StackStatus.UPDATE_IN_PROGRESS)
        else:
            stack.set_stack_status(StackStatus.REVIEW_IN_PROGRESS)

        # TODO: test if rollback status is allowed as well
        if (
            change_set_type == ChangeSetType.CREATE
            and stack.status != StackStatus.REVIEW_IN_PROGRESS
        ):
            raise ValidationError(
                f"Stack [{stack_name}] already exists and cannot be created again with the changeSet [{change_set_name}]."
            )

        before_parameters: dict[str, Parameter] | None = None
        match change_set_type:
            case ChangeSetType.UPDATE:
                before_parameters = stack.resolved_parameters
                # add changeset to existing stack
                # old_parameters = {
                #     k: mask_no_echo(strip_parameter_type(v))
                #     for k, v in stack.resolved_parameters.items()
                # }
            case ChangeSetType.IMPORT:
                raise NotImplementedError()  # TODO: implement importing resources
            case ChangeSetType.CREATE:
                pass
            case _:
                msg = (
                    f"1 validation error detected: Value '{change_set_type}' at 'changeSetType' failed to satisfy "
                    f"constraint: Member must satisfy enum value set: [IMPORT, UPDATE, CREATE] "
                )
                raise ValidationError(msg)

        # TODO: reconsider the way parameters are modelled in the update graph process.
        #  The options might be reduce to using the current style, or passing the extra information
        #  as a metadata object. The choice should be made considering when the extra information
        #  is needed for the update graph building, or only looked up in downstream tasks (metadata).
        request_parameters = request.get("Parameters", list())
        # TODO: handle parameter defaults and resolution
        after_parameters: dict[str, Any] = {
            parameter["ParameterKey"]: parameter["ParameterValue"]
            for parameter in request_parameters
        }

        # TODO: update this logic to always pass the clean template object if one exists. The
        #  current issue with relaying on stack.template_original is that this appears to have
        #  its parameters and conditions populated.
        before_template = None
        if change_set_type == ChangeSetType.UPDATE:
            before_template = stack.template
        after_template = structured_template

        previous_update_model = None
        try:
            # FIXME: 'change_set_id' for 'stack' objects is dynamically attributed
            if previous_change_set := find_change_set_v2(state, stack.change_set_id):
                previous_update_model = previous_change_set.update_model
        except Exception:
            # No change set available on this stack.
            pass

        # create change set for the stack and apply changes
        change_set = ChangeSet(stack, request, template=after_template)
        self._setup_change_set_model(
            change_set=change_set,
            before_template=before_template,
            after_template=after_template,
            before_parameters=before_parameters,
            after_parameters=after_parameters,
            previous_update_model=previous_update_model,
        )

        change_set.set_change_set_status(ChangeSetStatus.CREATE_COMPLETE)
        stack.change_set_id = change_set.change_set_id
        stack.change_set_ids.append(change_set.change_set_id)
        state.change_sets[change_set.change_set_id] = change_set

        return CreateChangeSetOutput(StackId=stack.stack_id, Id=change_set.change_set_id)

    @handler("ExecuteChangeSet")
    def execute_change_set(
        self,
        context: RequestContext,
        change_set_name: ChangeSetNameOrId,
        stack_name: StackNameOrId | None = None,
        client_request_token: ClientRequestToken | None = None,
        disable_rollback: DisableRollback | None = None,
        retain_except_on_create: RetainExceptOnCreate | None = None,
        **kwargs,
    ) -> ExecuteChangeSetOutput:
        state = get_cloudformation_store(context.account_id, context.region)

        change_set = find_change_set_v2(state, change_set_name, stack_name)
        if not change_set:
            raise ChangeSetNotFoundException(f"ChangeSet [{change_set_name}] does not exist")

        if change_set.execution_status != ExecutionStatus.AVAILABLE:
            LOG.debug("Change set %s not in execution status 'AVAILABLE'", change_set_name)
            raise InvalidChangeSetStatusException(
                f"ChangeSet [{change_set.change_set_id}] cannot be executed in its current status of [{change_set.status}]"
            )
        # LOG.debug(
        #     'Executing change set "%s" for stack "%s" with %s resources ...',
        #     change_set_name,
        #     stack_name,
        #     len(change_set.template_resources),
        # )
        if not change_set.update_model:
            raise RuntimeError("Programming error: no update graph found for change set")

        change_set.set_execution_status(ExecutionStatus.EXECUTE_IN_PROGRESS)
        change_set.stack.set_stack_status(
            StackStatus.UPDATE_IN_PROGRESS
            if change_set.change_set_type == ChangeSetType.UPDATE
            else StackStatus.CREATE_IN_PROGRESS
        )

        change_set_executor = ChangeSetModelExecutor(
            change_set,
        )

        def _run(*args):
            try:
                result = change_set_executor.execute()
                new_stack_status = StackStatus.UPDATE_COMPLETE
                if change_set.change_set_type == ChangeSetType.CREATE:
                    new_stack_status = StackStatus.CREATE_COMPLETE
                change_set.stack.set_stack_status(new_stack_status)
                change_set.set_execution_status(ExecutionStatus.EXECUTE_COMPLETE)
                change_set.stack.resolved_resources = result.resources
                change_set.stack.resolved_parameters = result.parameters
                change_set.stack.resolved_outputs = result.outputs
                # if the deployment succeeded, update the stack's template representation to that
                # which was just deployed
                change_set.stack.template = change_set.template
            except Exception as e:
                LOG.error(
                    "Execute change set failed: %s", e, exc_info=LOG.isEnabledFor(logging.WARNING)
                )
                new_stack_status = StackStatus.UPDATE_FAILED
                if change_set.change_set_type == ChangeSetType.CREATE:
                    new_stack_status = StackStatus.CREATE_FAILED

                change_set.stack.set_stack_status(new_stack_status)
                change_set.set_execution_status(ExecutionStatus.EXECUTE_FAILED)

        start_worker_thread(_run)

        return ExecuteChangeSetOutput()

    def _describe_change_set(
        self, change_set: ChangeSet, include_property_values: bool
    ) -> DescribeChangeSetOutput:
        # TODO: The ChangeSetModelDescriber currently matches AWS behavior by listing
        #       resource changes in the order they appear in the template. However, when
        #       a resource change is triggered indirectly (e.g., via Ref or GetAtt), the
        #       dependency's change appears first in the list.
        #       Snapshot tests using the `capture_update_process` fixture rely on a
        #       normalizer to account for this ordering. This should be removed in the
        #       future by enforcing a consistently correct change ordering at the source.
        change_set_describer = ChangeSetModelDescriber(
            change_set=change_set, include_property_values=include_property_values
        )
        changes: Changes = change_set_describer.get_changes()

        result = DescribeChangeSetOutput(
            Status=change_set.status,
            ChangeSetId=change_set.change_set_id,
            ChangeSetName=change_set.change_set_name,
            ExecutionStatus=change_set.execution_status,
            RollbackConfiguration=RollbackConfiguration(),
            StackId=change_set.stack.stack_id,
            StackName=change_set.stack.stack_name,
            CreationTime=change_set.creation_time,
            Parameters=[
                # TODO: add masking support.
                Parameter(ParameterKey=key, ParameterValue=value)
                for (key, value) in change_set.stack.resolved_parameters.items()
            ],
            Changes=changes,
        )
        return result

    @handler("DescribeChangeSet")
    def describe_change_set(
        self,
        context: RequestContext,
        change_set_name: ChangeSetNameOrId,
        stack_name: StackNameOrId | None = None,
        next_token: NextToken | None = None,
        include_property_values: IncludePropertyValues | None = None,
        **kwargs,
    ) -> DescribeChangeSetOutput:
        # TODO add support for include_property_values
        # only relevant if change_set_name isn't an ARN
        state = get_cloudformation_store(context.account_id, context.region)
        change_set = find_change_set_v2(state, change_set_name, stack_name)
        if not change_set:
            raise ChangeSetNotFoundException(f"ChangeSet [{change_set_name}] does not exist")
        result = self._describe_change_set(
            change_set=change_set, include_property_values=include_property_values or False
        )
        return result

    @handler("DeleteChangeSet")
    def delete_change_set(
        self,
        context: RequestContext,
        change_set_name: ChangeSetNameOrId,
        stack_name: StackNameOrId = None,
        **kwargs,
    ) -> DeleteChangeSetOutput:
        state = get_cloudformation_store(context.account_id, context.region)

        if is_changeset_arn(change_set_name):
            change_set = state.change_sets.get(change_set_name)
        elif not is_changeset_arn(change_set_name) and stack_name:
            change_set = find_change_set_v2(state, change_set_name, stack_name)
        else:
            raise ValidationError(
                "StackName must be specified if ChangeSetName is not specified as an ARN."
            )

        if not change_set:
            return DeleteChangeSetOutput()

        change_set.stack.change_set_ids.remove(change_set.change_set_id)
        state.change_sets.pop(change_set.change_set_id)

        return DeleteChangeSetOutput()

    @handler("CreateStack", expand=False)
    def create_stack(self, context: RequestContext, request: CreateStackInput) -> CreateStackOutput:
        try:
            stack_name = request["StackName"]
        except KeyError:
            # TODO: proper exception
            raise ValidationError("StackName must be specified")

        state = get_cloudformation_store(context.account_id, context.region)
        # TODO: copied from create_change_set, consider unifying
        template_body = request.get("TemplateBody")
        # s3 or secretsmanager url
        template_url = request.get("TemplateURL")

        # validate and resolve template
        if template_body and template_url:
            raise ValidationError(
                "Specify exactly one of 'TemplateBody' or 'TemplateUrl'"
            )  # TODO: check proper message

        if not template_body and not template_url:
            raise ValidationError(
                "Specify exactly one of 'TemplateBody' or 'TemplateUrl'"
            )  # TODO: check proper message

        template_body = api_utils.extract_template_body(request)
        structured_template = template_preparer.parse_template(template_body)

        if "CAPABILITY_AUTO_EXPAND" not in request.get("Capabilities", []) and (
            "Transform" in structured_template.keys() or "Fn::Transform" in template_body
        ):
            raise InsufficientCapabilitiesException(
                "Requires capabilities : [CAPABILITY_AUTO_EXPAND]"
            )

        stack = Stack(
            account_id=context.account_id,
            region_name=context.region,
            request_payload=request,
            template=structured_template,
            template_body=template_body,
        )
        # TODO: what is the correct initial status?
        state.stacks_v2[stack.stack_id] = stack

        # TODO: reconsider the way parameters are modelled in the update graph process.
        #  The options might be reduce to using the current style, or passing the extra information
        #  as a metadata object. The choice should be made considering when the extra information
        #  is needed for the update graph building, or only looked up in downstream tasks (metadata).
        request_parameters = request.get("Parameters", list())
        # TODO: handle parameter defaults and resolution
        after_parameters: dict[str, Any] = {
            parameter["ParameterKey"]: parameter["ParameterValue"]
            for parameter in request_parameters
        }
        after_template = structured_template

        # Create internal change set to execute
        change_set = ChangeSet(
            stack,
            {"ChangeSetName": f"cs-{stack_name}-create", "ChangeSetType": ChangeSetType.CREATE},
            template=after_template,
        )
        self._setup_change_set_model(
            change_set=change_set,
            before_template=None,
            after_template=after_template,
            before_parameters=None,
            after_parameters=after_parameters,
            previous_update_model=None,
        )

        # deployment process
        stack.set_stack_status(StackStatus.CREATE_IN_PROGRESS)
        change_set_executor = ChangeSetModelExecutor(change_set)

        def _run(*args):
            try:
                result = change_set_executor.execute()
                stack.set_stack_status(StackStatus.CREATE_COMPLETE)
                stack.resolved_resources = result.resources
                stack.resolved_parameters = result.parameters
                stack.resolved_outputs = result.outputs
                # if the deployment succeeded, update the stack's template representation to that
                # which was just deployed
                stack.template = change_set.template
            except Exception as e:
                LOG.error(
                    "Create Stack set failed: %s", e, exc_info=LOG.isEnabledFor(logging.WARNING)
                )
                stack.set_stack_status(StackStatus.CREATE_FAILED)

        start_worker_thread(_run)

        return CreateStackOutput(StackId=stack.stack_id)

    @handler("DescribeStacks")
    def describe_stacks(
        self,
        context: RequestContext,
        stack_name: StackName = None,
        next_token: NextToken = None,
        **kwargs,
    ) -> DescribeStacksOutput:
        state = get_cloudformation_store(context.account_id, context.region)
        stack = find_stack_v2(state, stack_name)
        if not stack:
            raise StackWithIdNotFoundError(stack_name)
        return DescribeStacksOutput(Stacks=[stack.describe_details()])

    @handler("DescribeStackResources")
    def describe_stack_resources(
        self,
        context: RequestContext,
        stack_name: StackName = None,
        logical_resource_id: LogicalResourceId = None,
        physical_resource_id: PhysicalResourceId = None,
        **kwargs,
    ) -> DescribeStackResourcesOutput:
        if physical_resource_id and stack_name:
            raise ValidationError("Cannot specify both StackName and PhysicalResourceId")
        state = get_cloudformation_store(context.account_id, context.region)
        stack = find_stack_v2(state, stack_name)
        if not stack:
            raise StackWithIdNotFoundError(stack_name)
        # TODO: filter stack by PhysicalResourceId!
        statuses = []
        for resource_id, resource_status in stack.resource_states.items():
            if resource_id == logical_resource_id or logical_resource_id is None:
                status = copy.deepcopy(resource_status)
                status.setdefault("DriftInformation", {"StackResourceDriftStatus": "NOT_CHECKED"})
                statuses.append(status)
        return DescribeStackResourcesOutput(StackResources=statuses)

    @handler("DescribeStackEvents")
    def describe_stack_events(
        self,
        context: RequestContext,
        stack_name: StackName = None,
        next_token: NextToken = None,
        **kwargs,
    ) -> DescribeStackEventsOutput:
        state = get_cloudformation_store(context.account_id, context.region)
        stack = find_stack_v2(state, stack_name)
        if not stack:
            raise StackWithIdNotFoundError(stack_name)
        return DescribeStackEventsOutput(StackEvents=stack.events)

    @handler("GetTemplate")
    def get_template(
        self,
        context: RequestContext,
        stack_name: StackName = None,
        change_set_name: ChangeSetNameOrId = None,
        template_stage: TemplateStage = None,
        **kwargs,
    ) -> GetTemplateOutput:
        state = get_cloudformation_store(context.account_id, context.region)
        if change_set_name:
            change_set = find_change_set_v2(state, change_set_name, stack_name=stack_name)
            stack = change_set.stack
        elif stack_name:
            stack = find_stack_v2(state, stack_name)
        else:
            raise StackWithIdNotFoundError(stack_name)

        if template_stage == TemplateStage.Processed and "Transform" in stack.template_body:
            template_body = json.dumps(stack.processed_template)
        else:
            template_body = stack.template_body

        return GetTemplateOutput(
            TemplateBody=template_body,
            StagesAvailable=[TemplateStage.Original, TemplateStage.Processed],
        )

    @handler("GetTemplateSummary", expand=False)
    def get_template_summary(
        self,
        context: RequestContext,
        request: GetTemplateSummaryInput,
    ) -> GetTemplateSummaryOutput:
        state = get_cloudformation_store(context.account_id, context.region)
        stack_name = request.get("StackName")

        if stack_name:
            stack = find_stack_v2(state, stack_name)
            if not stack:
                raise StackWithIdNotFoundError(stack_name)
            template = stack.template
        else:
            template_body = request.get("TemplateBody")
            # s3 or secretsmanager url
            template_url = request.get("TemplateURL")

            # validate and resolve template
            if template_body and template_url:
                raise ValidationError(
                    "Specify exactly one of 'TemplateBody' or 'TemplateUrl'"
                )  # TODO: check proper message

            if not template_body and not template_url:
                raise ValidationError(
                    "Specify exactly one of 'TemplateBody' or 'TemplateUrl'"
                )  # TODO: check proper message

            template_body = api_utils.extract_template_body(request)
            template = template_preparer.parse_template(template_body)

        id_summaries = defaultdict(list)
        for resource_id, resource in template["Resources"].items():
            res_type = resource["Type"]
            id_summaries[res_type].append(resource_id)

        summarized_parameters = []
        for parameter_id, parameter_body in template.get("Parameters", {}).items():
            summarized_parameters.append(
                {
                    "ParameterKey": parameter_id,
                    "DefaultValue": parameter_body.get("Default"),
                    "ParameterType": parameter_body["Type"],
                    "Description": parameter_body.get("Description"),
                }
            )
        result = GetTemplateSummaryOutput(
            Parameters=summarized_parameters,
            Metadata=template.get("Metadata"),
            ResourceIdentifierSummaries=[
                {"ResourceType": key, "LogicalResourceIds": values}
                for key, values in id_summaries.items()
            ],
            ResourceTypes=list(id_summaries.keys()),
            Version=template.get("AWSTemplateFormatVersion", "2010-09-09"),
        )

        return result

    @handler("UpdateStack", expand=False)
    def update_stack(
        self,
        context: RequestContext,
        request: UpdateStackInput,
    ) -> UpdateStackOutput:
        try:
            stack_name = request["StackName"]
        except KeyError:
            # TODO: proper exception
            raise ValidationError("StackName must be specified")
        state = get_cloudformation_store(context.account_id, context.region)
        template_body = request.get("TemplateBody")
        # s3 or secretsmanager url
        template_url = request.get("TemplateURL")

        # validate and resolve template
        if template_body and template_url:
            raise ValidationError(
                "Specify exactly one of 'TemplateBody' or 'TemplateUrl'"
            )  # TODO: check proper message

        if not template_body and not template_url:
            raise ValidationError(
                "Specify exactly one of 'TemplateBody' or 'TemplateUrl'"
            )  # TODO: check proper message

        template_body = api_utils.extract_template_body(request)
        structured_template = template_preparer.parse_template(template_body)

        if "CAPABILITY_AUTO_EXPAND" not in request.get("Capabilities", []) and (
            "Transform" in structured_template.keys() or "Fn::Transform" in template_body
        ):
            raise InsufficientCapabilitiesException(
                "Requires capabilities : [CAPABILITY_AUTO_EXPAND]"
            )

        # this is intentionally not in a util yet. Let's first see how the different operations deal with these before generalizing
        # handle ARN stack_name here (not valid for initial CREATE, since stack doesn't exist yet)
        stack: Stack
        if is_stack_arn(stack_name):
            stack = state.stacks_v2.get(stack_name)
            if not stack:
                raise ValidationError(f"Stack '{stack_name}' does not exist.")

        else:
            # stack name specified, so fetch the stack by name
            stack_candidates: list[Stack] = [
                s for stack_arn, s in state.stacks_v2.items() if s.stack_name == stack_name
            ]
            active_stack_candidates = [
                s for s in stack_candidates if self._stack_status_is_active(s.status)
            ]

            if not active_stack_candidates:
                raise ValidationError(f"Stack '{stack_name}' does not exist.")
            elif len(active_stack_candidates) > 1:
                raise RuntimeError("Multiple stacks matched, update matching logic")
            stack = active_stack_candidates[0]

        # TODO: proper status modeling
        before_parameters = stack.resolved_parameters
        # TODO: reconsider the way parameters are modelled in the update graph process.
        #  The options might be reduce to using the current style, or passing the extra information
        #  as a metadata object. The choice should be made considering when the extra information
        #  is needed for the update graph building, or only looked up in downstream tasks (metadata).
        request_parameters = request.get("Parameters", list())
        # TODO: handle parameter defaults and resolution
        after_parameters: dict[str, Any] = {
            parameter["ParameterKey"]: parameter["ParameterValue"]
            for parameter in request_parameters
        }
        before_template = stack.template
        after_template = structured_template

        previous_update_model = None
        if stack.change_set_id:
            if previous_change_set := find_change_set_v2(state, stack.change_set_id):
                previous_update_model = previous_change_set.update_model

        change_set = ChangeSet(
            stack,
            {"ChangeSetName": f"cs-{stack_name}-create", "ChangeSetType": ChangeSetType.CREATE},
            template=after_template,
        )
        self._setup_change_set_model(
            change_set=change_set,
            before_template=before_template,
            after_template=after_template,
            before_parameters=before_parameters,
            after_parameters=after_parameters,
            previous_update_model=previous_update_model,
        )

        # TODO: some changes are only detectable at runtime; consider using
        #       the ChangeSetModelDescriber, or a new custom visitors, to
        #       pick-up on runtime changes.
        if change_set.update_model.node_template.change_type == ChangeType.UNCHANGED:
            raise ValidationError("No updates are to be performed.")

        stack.set_stack_status(StackStatus.UPDATE_IN_PROGRESS)
        change_set_executor = ChangeSetModelExecutor(change_set)

        def _run(*args):
            try:
                result = change_set_executor.execute()
                stack.set_stack_status(StackStatus.UPDATE_COMPLETE)
                stack.resolved_resources = result.resources
                stack.resolved_parameters = result.parameters
                stack.resolved_outputs = result.outputs
                # if the deployment succeeded, update the stack's template representation to that
                # which was just deployed
                stack.template = change_set.template
            except Exception as e:
                LOG.error("Update Stack failed: %s", e, exc_info=LOG.isEnabledFor(logging.WARNING))
                stack.set_stack_status(StackStatus.UPDATE_FAILED)

        start_worker_thread(_run)

        # TODO: stack id
        return UpdateStackOutput(StackId=stack.stack_id)

    @handler("DeleteStack")
    def delete_stack(
        self,
        context: RequestContext,
        stack_name: StackName,
        retain_resources: RetainResources = None,
        role_arn: RoleARN = None,
        client_request_token: ClientRequestToken = None,
        deletion_mode: DeletionMode = None,
        **kwargs,
    ) -> None:
        state = get_cloudformation_store(context.account_id, context.region)
        stack = find_stack_v2(state, stack_name)
        if not stack:
            # aws will silently ignore invalid stack names - we should do the same
            return

        # shortcut for stacks which have no deployed resources i.e. where a change set was
        # created, but never executed
        if stack.status == StackStatus.REVIEW_IN_PROGRESS and not stack.resolved_resources:
            stack.set_stack_status(StackStatus.DELETE_COMPLETE)
            stack.deletion_time = datetime.now(tz=timezone.utc)
            return

        previous_update_model = None
        if stack.change_set_id:
            if previous_change_set := find_change_set_v2(state, stack.change_set_id):
                previous_update_model = previous_change_set.update_model

        # create a dummy change set
        change_set = ChangeSet(stack, {"ChangeSetName": f"delete-stack_{stack.stack_name}"})  # noqa
        self._setup_change_set_model(
            change_set=change_set,
            before_template=stack.template,
            after_template=None,
            before_parameters=stack.resolved_parameters,
            after_parameters=None,
            previous_update_model=previous_update_model,
        )

        change_set_executor = ChangeSetModelExecutor(change_set)

        def _run(*args):
            try:
                stack.set_stack_status(StackStatus.DELETE_IN_PROGRESS)
                change_set_executor.execute()
                stack.set_stack_status(StackStatus.DELETE_COMPLETE)
                stack.deletion_time = datetime.now(tz=timezone.utc)
            except Exception as e:
                LOG.warning(
                    "Failed to delete stack '%s': %s",
                    stack.stack_name,
                    e,
                    exc_info=LOG.isEnabledFor(logging.DEBUG),
                )
                stack.set_stack_status(StackStatus.DELETE_FAILED)

        start_worker_thread(_run)
