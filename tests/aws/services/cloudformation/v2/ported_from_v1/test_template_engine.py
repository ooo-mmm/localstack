import base64
import json
import os
import re
from copy import deepcopy

import botocore.exceptions
import pytest
import yaml

from localstack.aws.api.lambda_ import Runtime
from localstack.services.cloudformation.engine.yaml_parser import parse_yaml
from localstack.services.cloudformation.v2.utils import is_v2_engine
from localstack.testing.aws.cloudformation_utils import load_template_file, load_template_raw
from localstack.testing.aws.util import is_aws_cloud
from localstack.testing.pytest import markers
from localstack.testing.pytest.fixtures import StackDeployError
from localstack.utils.common import short_uid
from localstack.utils.files import load_file
from localstack.utils.sync import wait_until

pytestmark = pytest.mark.skipif(
    condition=not is_v2_engine() and not is_aws_cloud(),
    reason="Only targeting the new engine",
)


def create_macro(
    macro_name, function_path, deploy_cfn_template, create_lambda_function, lambda_client
):
    macro_function_path = function_path

    func_name = f"test_lambda_{short_uid()}"
    create_lambda_function(
        func_name=func_name,
        handler_file=macro_function_path,
        runtime=Runtime.python3_12,
        client=lambda_client,
        timeout=1,
    )

    return deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../../templates/macro_resource.yml"
        ),
        parameters={"FunctionName": func_name, "MacroName": macro_name},
    )


class TestTypes:
    @markers.aws.validated
    def test_implicit_type_conversion(self, deploy_cfn_template, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.sqs_api())
        stack = deploy_cfn_template(
            max_wait=180,
            template_path=os.path.join(
                os.path.dirname(__file__),
                "../../../../templates/engine/implicit_type_conversion.yml",
            ),
        )
        queue = aws_client.sqs.get_queue_attributes(
            QueueUrl=stack.outputs["QueueUrl"], AttributeNames=["All"]
        )
        snapshot.match("queue", queue)


class TestIntrinsicFunctions:
    @pytest.mark.parametrize(
        ("intrinsic_fn", "parameter_1", "parameter_2", "expected_bucket_created"),
        [
            ("Fn::And", "0", "0", False),
            ("Fn::And", "0", "1", False),
            ("Fn::And", "1", "0", False),
            ("Fn::And", "1", "1", True),
            ("Fn::Or", "0", "0", False),
            ("Fn::Or", "0", "1", True),
            ("Fn::Or", "1", "0", True),
            ("Fn::Or", "1", "1", True),
        ],
    )
    @markers.aws.validated
    def test_and_or_functions(
        self,
        intrinsic_fn,
        parameter_1,
        parameter_2,
        expected_bucket_created,
        deploy_cfn_template,
        aws_client,
    ):
        bucket_name = f"ls-bucket-{short_uid()}"

        deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/cfn_intrinsic_functions.yaml"
            ),
            parameters={
                "Param1": parameter_1,
                "Param2": parameter_2,
                "BucketName": bucket_name,
            },
            template_mapping={
                "intrinsic_fn": intrinsic_fn,
            },
        )

        buckets = aws_client.s3.list_buckets()
        bucket_names = [b["Name"] for b in buckets["Buckets"]]
        assert (bucket_name in bucket_names) == expected_bucket_created

    @markers.aws.validated
    def test_base64_sub_and_getatt_functions(self, deploy_cfn_template):
        template_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/functions_getatt_sub_base64.yml"
        )
        original_string = f"string-{short_uid()}"
        deployed = deploy_cfn_template(
            template_path=template_path, parameters={"OriginalString": original_string}
        )

        converted_string = base64.b64encode(bytes(original_string, "utf-8")).decode("utf-8")
        assert converted_string == deployed.outputs["Encoded"]

    @pytest.mark.skip(reason="CFNV2:LanguageExtensions")
    @markers.aws.validated
    def test_split_length_and_join_functions(self, deploy_cfn_template):
        template_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/functions_select_split_join.yml"
        )

        first_value = f"string-{short_uid()}"
        second_value = f"string-{short_uid()}"
        deployed = deploy_cfn_template(
            template_path=template_path,
            parameters={
                "MultipleValues": f"{first_value};{second_value}",
                "Value1": first_value,
                "Value2": second_value,
            },
        )

        assert first_value == deployed.outputs["SplitResult"]
        assert f"{first_value}_{second_value}" == deployed.outputs["JoinResult"]

        # TODO support join+split and length operations
        # assert f"{first_value}_{second_value}" == deployed.outputs["SplitJoin"]
        # assert 2 == deployed.outputs["LengthResult"]

    @markers.aws.validated
    @pytest.mark.skip(reason="functions not currently supported")
    def test_to_json_functions(self, deploy_cfn_template):
        template_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/function_to_json_string.yml"
        )

        first_value = f"string-{short_uid()}"
        second_value = f"string-{short_uid()}"
        deployed = deploy_cfn_template(
            template_path=template_path,
            parameters={
                "Value1": first_value,
                "Value2": second_value,
            },
        )

        json_result = json.loads(deployed.outputs["Result"])

        assert json_result["key1"] == first_value
        assert json_result["key2"] == second_value
        assert "value1" == deployed.outputs["Result2"]

    @markers.aws.validated
    def test_find_map_function(self, deploy_cfn_template):
        template_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/function_find_in_map.yml"
        )

        deployed = deploy_cfn_template(
            template_path=template_path,
        )

        assert deployed.outputs["Result"] == "us-east-1"

    @markers.aws.validated
    @pytest.mark.skip(reason="function not currently supported")
    def test_cidr_function(self, deploy_cfn_template):
        template_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/functions_cidr.yml"
        )

        # TODO parametrize parameters and result
        deployed = deploy_cfn_template(
            template_path=template_path,
            parameters={"IpBlock": "10.0.0.0/16", "Count": "1", "CidrBits": "8", "Select": "0"},
        )

        assert deployed.outputs["Address"] == "10.0.0.0/24"

    @pytest.mark.parametrize(
        "region",
        [
            "us-east-1",
            "us-east-2",
            "us-west-1",
            "us-west-2",
            "ap-southeast-2",
            "ap-northeast-1",
            "eu-central-1",
            "eu-west-1",
        ],
    )
    @markers.aws.validated
    def test_get_azs_function(self, deploy_cfn_template, region, aws_client_factory):
        """
        TODO parametrize this test.
        For that we need to be able to parametrize the client region. The docs show the we should be
        able to put any region in the parameters but it doesn't work. It only accepts the same region from the client config
        if you put anything else it just returns an empty list.
        """
        template_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/functions_get_azs.yml"
        )

        aws_client = aws_client_factory(region_name=region)
        deployed = deploy_cfn_template(
            template_path=template_path,
            custom_aws_client=aws_client,
            parameters={"DeployRegion": region},
        )

        azs = deployed.outputs["Zones"].split(";")
        assert len(azs) > 0
        assert all(re.match(f"{region}[a-f]", az) for az in azs)

    @markers.aws.validated
    def test_sub_not_ready(self, deploy_cfn_template):
        template_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/sub_dependencies.yaml"
        )
        deploy_cfn_template(
            template_path=template_path,
            max_wait=120,
        )

    @markers.aws.validated
    def test_cfn_template_with_short_form_fn_sub(self, deploy_cfn_template):
        stack = deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/engine/cfn_short_sub.yml"
            ),
        )

        result = stack.outputs["Result"]
        assert result == "test"

    @markers.aws.validated
    def test_sub_number_type(self, deploy_cfn_template):
        alarm_name_prefix = "alarm-test-latency-preemptive"
        threshold = "1000.0"
        period = "60"
        stack = deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/sub_number_type.yml"
            ),
            parameters={
                "ResourceNamePrefix": alarm_name_prefix,
                "RestLatencyPreemptiveAlarmThreshold": threshold,
                "RestLatencyPreemptiveAlarmPeriod": period,
            },
        )

        assert stack.outputs["AlarmName"] == f"{alarm_name_prefix}-{period}"
        assert stack.outputs["Threshold"] == threshold
        assert stack.outputs["Period"] == period

    @markers.aws.validated
    def test_join_no_value_construct(self, deploy_cfn_template, snapshot, aws_client):
        stack = deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/engine/join_no_value.yml"
            )
        )

        snapshot.match("join-output", stack.outputs)


@pytest.mark.skip(reason="CFNV2:Imports unsupported")
class TestImports:
    @markers.aws.validated
    def test_stack_imports(self, deploy_cfn_template, aws_client):
        queue_name1 = f"q-{short_uid()}"
        queue_name2 = f"q-{short_uid()}"
        deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/sqs_export.yml"
            ),
            parameters={"QueueName": queue_name1},
        )
        stack2 = deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/sqs_import.yml"
            ),
            parameters={"QueueName": queue_name2},
        )
        queue_url1 = aws_client.sqs.get_queue_url(QueueName=queue_name1)["QueueUrl"]
        queue_url2 = aws_client.sqs.get_queue_url(QueueName=queue_name2)["QueueUrl"]

        queue_arn1 = aws_client.sqs.get_queue_attributes(
            QueueUrl=queue_url1, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        queue_arn2 = aws_client.sqs.get_queue_attributes(
            QueueUrl=queue_url2, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]

        assert stack2.outputs["MessageQueueArn1"] == queue_arn1
        assert stack2.outputs["MessageQueueArn2"] == queue_arn2


@pytest.mark.skip(reason="CFNV2:resolve")
class TestSsmParameters:
    @markers.aws.validated
    def test_create_stack_with_ssm_parameters(
        self, create_parameter, deploy_cfn_template, snapshot, aws_client
    ):
        snapshot.add_transformer(snapshot.transform.cloudformation_api())
        snapshot.add_transformer(snapshot.transform.key_value("ParameterValue"))
        snapshot.add_transformer(snapshot.transform.key_value("ResolvedValue"))

        parameter_name = f"ls-param-{short_uid()}"
        parameter_value = f"ls-param-value-{short_uid()}"
        create_parameter(Name=parameter_name, Value=parameter_value, Type="String")
        stack = deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/dynamicparameter_ssm_string.yaml"
            ),
            template_mapping={"parameter_name": parameter_name},
        )

        stack_description = aws_client.cloudformation.describe_stacks(StackName=stack.stack_name)[
            "Stacks"
        ][0]
        snapshot.match("stack-details", stack_description)

        topics = aws_client.sns.list_topics()
        topic_arns = [t["TopicArn"] for t in topics["Topics"]]

        matching = [arn for arn in topic_arns if parameter_value in arn]
        assert len(matching) == 1

        tags = aws_client.sns.list_tags_for_resource(ResourceArn=matching[0])
        snapshot.match("topic-tags", tags)

    @markers.aws.validated
    def test_resolve_ssm(self, create_parameter, deploy_cfn_template):
        parameter_key = f"param-key-{short_uid()}"
        parameter_value = f"param-value-{short_uid()}"
        create_parameter(Name=parameter_key, Value=parameter_value, Type="String")

        result = deploy_cfn_template(
            parameters={"DynamicParameter": parameter_key},
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/resolve_ssm.yaml"
            ),
        )

        topic_name = result.outputs["TopicName"]
        assert topic_name == parameter_value

    @markers.aws.validated
    def test_resolve_ssm_with_version(self, create_parameter, deploy_cfn_template, aws_client):
        parameter_key = f"param-key-{short_uid()}"
        parameter_value_v0 = f"param-value-{short_uid()}"
        parameter_value_v1 = f"param-value-{short_uid()}"
        parameter_value_v2 = f"param-value-{short_uid()}"

        create_parameter(Name=parameter_key, Type="String", Value=parameter_value_v0)

        v1 = aws_client.ssm.put_parameter(
            Name=parameter_key, Overwrite=True, Type="String", Value=parameter_value_v1
        )
        aws_client.ssm.put_parameter(
            Name=parameter_key, Overwrite=True, Type="String", Value=parameter_value_v2
        )

        result = deploy_cfn_template(
            parameters={"DynamicParameter": f"{parameter_key}:{v1['Version']}"},
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/resolve_ssm.yaml"
            ),
        )

        topic_name = result.outputs["TopicName"]
        assert topic_name == parameter_value_v1

    @markers.aws.needs_fixing
    def test_resolve_ssm_secure(self, create_parameter, deploy_cfn_template):
        parameter_key = f"param-key-{short_uid()}"
        parameter_value = f"param-value-{short_uid()}"

        create_parameter(Name=parameter_key, Value=parameter_value, Type="SecureString")

        result = deploy_cfn_template(
            parameters={"DynamicParameter": f"{parameter_key}"},
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/resolve_ssm_secure.yaml"
            ),
        )

        topic_name = result.outputs["TopicName"]
        assert topic_name == parameter_value

    @markers.aws.validated
    def test_ssm_nested_with_nested_stack(self, s3_create_bucket, deploy_cfn_template, aws_client):
        """
        When resolving the references in the cloudformation template for 'Fn::GetAtt' we need to consider the attribute subname.
        Eg: In "Fn::GetAtt": "ChildParam.Outputs.Value", where attribute reference is ChildParam.Outputs.Value the:
        resource logical id is ChildParam and attribute name is Outputs we need to fetch the Value attribute from the resource properties
        of the model instance.
        """

        bucket_name = s3_create_bucket()
        domain = "amazonaws.com" if is_aws_cloud() else "localhost.localstack.cloud:4566"

        aws_client.s3.upload_file(
            os.path.join(os.path.dirname(__file__), "../../../../templates/nested_child_ssm.yaml"),
            Bucket=bucket_name,
            Key="nested_child_ssm.yaml",
        )

        key_value = "child-2-param-name"

        deploy_cfn_template(
            max_wait=120 if is_aws_cloud() else 20,
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/nested_parent_ssm.yaml"
            ),
            parameters={
                "ChildStackURL": f"https://{bucket_name}.s3.{domain}/nested_child_ssm.yaml",
                "KeyValue": key_value,
            },
        )

        ssm_parameter = aws_client.ssm.get_parameter(Name="test-param")["Parameter"]["Value"]

        assert ssm_parameter == key_value

    @markers.aws.validated
    def test_create_change_set_with_ssm_parameter_list(
        self, deploy_cfn_template, aws_client, region_name, account_id, snapshot
    ):
        snapshot.add_transformer(snapshot.transform.key_value(key="role-name"))

        parameter_logical_id = "parameter123"
        parameter_name = f"ls-param-{short_uid()}"
        role_name = f"ls-role-{short_uid()}"
        parameter_value = ",".join(
            [
                f"arn:aws:ssm:{region_name}:{account_id}:parameter/some/params",
                f"arn:aws:ssm:{region_name}:{account_id}:parameter/some/other/params",
            ]
        )
        snapshot.match("role-name", role_name)

        aws_client.ssm.put_parameter(Name=parameter_name, Value=parameter_value, Type="StringList")

        deploy_cfn_template(
            max_wait=120 if is_aws_cloud() else 20,
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/dynamicparameter_ssm_list.yaml"
            ),
            template_mapping={"role_name": role_name},
            parameters={parameter_logical_id: parameter_name},
        )
        role_policy = aws_client.iam.get_role_policy(RoleName=role_name, PolicyName="policy-123")
        snapshot.match("iam_role_policy", role_policy)


class TestSecretsManagerParameters:
    @pytest.mark.skip(reason="CFNV2:resolve")
    @pytest.mark.parametrize(
        "template_name",
        [
            "resolve_secretsmanager_full.yaml",
            "resolve_secretsmanager_partial.yaml",
            "resolve_secretsmanager.yaml",
        ],
    )
    @markers.aws.validated
    def test_resolve_secretsmanager(self, create_secret, deploy_cfn_template, template_name):
        parameter_key = f"param-key-{short_uid()}"
        parameter_value = f"param-value-{short_uid()}"

        create_secret(Name=parameter_key, SecretString=parameter_value)

        result = deploy_cfn_template(
            parameters={"DynamicParameter": f"{parameter_key}"},
            template_path=os.path.join(
                os.path.dirname(__file__),
                "../../../../templates",
                template_name,
            ),
        )

        topic_name = result.outputs["TopicName"]
        assert topic_name == parameter_value


class TestPreviousValues:
    @pytest.mark.skip(reason="outputs don't behave well in combination with conditions")
    @markers.aws.validated
    def test_parameter_usepreviousvalue_behavior(
        self, deploy_cfn_template, is_stack_updated, aws_client
    ):
        template_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/cfn_reuse_param.yaml"
        )

        # 1. create with overridden default value. Due to the condition this should neither create the optional topic,
        # nor the corresponding output
        stack = deploy_cfn_template(template_path=template_path, parameters={"DeployParam": "no"})

        stack_describe_response = aws_client.cloudformation.describe_stacks(
            StackName=stack.stack_name
        )["Stacks"][0]
        assert len(stack_describe_response["Outputs"]) == 1

        # 2. update using UsePreviousValue. DeployParam should still be "no", still overriding the default and the only
        # change should be the changed tag on the required topic
        aws_client.cloudformation.update_stack(
            StackName=stack.stack_namestack_name,
            TemplateBody=load_template_raw(template_path),
            Parameters=[
                {"ParameterKey": "CustomTag", "ParameterValue": "trigger-change"},
                {"ParameterKey": "DeployParam", "UsePreviousValue": True},
            ],
        )
        wait_until(is_stack_updated(stack.stack_id))
        stack_describe_response = aws_client.cloudformation.describe_stacks(
            StackName=stack.stack_name
        )["Stacks"][0]
        assert len(stack_describe_response["Outputs"]) == 1

        # 3. update with setting the deployparam to "yes" not. The condition will evaluate to true and thus create the
        # topic + output note: for an even trickier challenge for the cloudformation engine, remove the second parameter
        # key. Behavior should stay the same.
        aws_client.cloudformation.update_stack(
            StackName=stack.stack_name,
            TemplateBody=load_template_raw(template_path),
            Parameters=[
                {"ParameterKey": "CustomTag", "ParameterValue": "trigger-change-2"},
                {"ParameterKey": "DeployParam", "ParameterValue": "yes"},
            ],
        )
        assert is_stack_updated(stack.stack_id)
        stack_describe_response = aws_client.cloudformation.describe_stacks(
            StackName=stack.stack_id
        )["Stacks"][0]
        assert len(stack_describe_response["Outputs"]) == 2


@pytest.mark.skip(reason="CFNV2:Imports unsupported")
class TestImportValues:
    @markers.aws.validated
    def test_cfn_with_exports(self, deploy_cfn_template, aws_client, snapshot):
        stack = deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/engine/cfn_exports.yml"
            )
        )

        exports = aws_client.cloudformation.list_exports()["Exports"]
        filtered = [exp for exp in exports if exp["ExportingStackId"] == stack.stack_id]
        filtered.sort(key=lambda x: x["Name"])

        snapshot.match("exports", filtered)

        snapshot.add_transformer(snapshot.transform.regex(stack.stack_id, "<stack-id>"))
        snapshot.add_transformer(snapshot.transform.regex(stack.stack_name, "<stack-name>"))

    @markers.aws.validated
    def test_import_values_across_stacks(self, deploy_cfn_template, aws_client):
        export_name = f"b-{short_uid()}"

        # create stack #1
        result = deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/cfn_function_export.yml"
            ),
            parameters={"BucketExportName": export_name},
        )
        bucket_name1 = result.outputs.get("BucketName1")
        assert bucket_name1

        # create stack #2
        result = deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/cfn_function_import.yml"
            ),
            parameters={"BucketExportName": export_name},
        )
        bucket_name2 = result.outputs.get("BucketName2")
        assert bucket_name2

        # assert that correct bucket tags have been created
        tagging = aws_client.s3.get_bucket_tagging(Bucket=bucket_name2)
        test_tag = [tag for tag in tagging["TagSet"] if tag["Key"] == "test"]
        assert test_tag
        assert test_tag[0]["Value"] == bucket_name1

        # TODO support this method
        # assert cfn_client.list_imports(ExportName=export_name)["Imports"]


class TestMacros:
    @markers.aws.validated
    def test_macro_deployment(
        self, deploy_cfn_template, create_lambda_function, snapshot, aws_client
    ):
        macro_function_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/macros/format_template.py"
        )
        macro_name = "SubstitutionMacro"

        func_name = f"test_lambda_{short_uid()}"
        create_lambda_function(
            func_name=func_name,
            handler_file=macro_function_path,
            runtime=Runtime.python3_12,
            client=aws_client.lambda_,
        )

        stack_with_macro = deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/macro_resource.yml"
            ),
            parameters={"FunctionName": func_name, "MacroName": macro_name},
        )

        description = aws_client.cloudformation.describe_stack_resources(
            StackName=stack_with_macro.stack_name
        )

        snapshot.add_transformer(snapshot.transform.cloudformation_api())
        snapshot.match("stack_outputs", stack_with_macro.outputs)
        snapshot.match("stack_resource_descriptions", description)

    @pytest.mark.skip("CFNV2:Macros")
    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        paths=[
            "$..TemplateBody.Resources.Parameter.LogicalResourceId",
            "$..TemplateBody.Conditions",
            "$..TemplateBody.Mappings",
            "$..TemplateBody.StackId",
            "$..TemplateBody.StackName",
            "$..TemplateBody.Transform",
        ]
    )
    def test_global_scope(
        self, deploy_cfn_template, create_lambda_function, snapshot, cleanups, aws_client
    ):
        """
        This test validates the behaviour of a template deployment that includes a global transformation
        """

        macro_function_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/macros/format_template.py"
        )
        macro_name = "SubstitutionMacro"
        func_name = f"test_lambda_{short_uid()}"
        create_lambda_function(
            func_name=func_name,
            handler_file=macro_function_path,
            runtime=Runtime.python3_12,
            client=aws_client.lambda_,
            timeout=1,
        )

        deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/macro_resource.yml"
            ),
            parameters={"FunctionName": func_name, "MacroName": macro_name},
        )

        new_value = f"new-value-{short_uid()}"
        stack_name = f"stake-{short_uid()}"
        aws_client.cloudformation.create_stack(
            StackName=stack_name,
            Capabilities=["CAPABILITY_AUTO_EXPAND"],
            TemplateBody=load_template_file(
                os.path.join(
                    os.path.dirname(__file__),
                    "../../../../templates/transformation_global_parameter.yml",
                )
            ),
            Parameters=[{"ParameterKey": "Substitution", "ParameterValue": new_value}],
        )
        cleanups.append(lambda: aws_client.cloudformation.delete_stack(StackName=stack_name))
        aws_client.cloudformation.get_waiter("stack_create_complete").wait(StackName=stack_name)

        processed_template = aws_client.cloudformation.get_template(
            StackName=stack_name, TemplateStage="Processed"
        )
        snapshot.add_transformer(snapshot.transform.regex(new_value, "new-value"))
        snapshot.match("processed_template", processed_template)

    @pytest.mark.skip(
        reason="CFNV2:Fn::Transform as resource property with missing Name and Parameters fields."
    )
    @markers.aws.validated
    @pytest.mark.parametrize(
        "template_to_transform",
        ["transformation_snippet_topic.yml", "transformation_snippet_topic.json"],
    )
    def test_snipped_scope(
        self,
        deploy_cfn_template,
        create_lambda_function,
        snapshot,
        template_to_transform,
        aws_client,
    ):
        """
        This test validates the behaviour of a template deployment that includes a snipped transformation also the
        responses from the get_template with different template formats.
        """
        macro_function_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/macros/add_standard_attributes.py"
        )

        func_name = f"test_lambda_{short_uid()}"
        create_lambda_function(
            func_name=func_name,
            handler_file=macro_function_path,
            runtime=Runtime.python3_12,
            client=aws_client.lambda_,
            timeout=1,
        )

        macro_name = "ConvertTopicToFifo"
        stack_name = f"stake-macro-{short_uid()}"
        deploy_cfn_template(
            stack_name=stack_name,
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/macro_resource.yml"
            ),
            parameters={"FunctionName": func_name, "MacroName": macro_name},
        )

        topic_name = f"topic-{short_uid()}.fifo"
        stack = deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__),
                "../../../../templates",
                template_to_transform,
            ),
            parameters={"TopicName": topic_name},
        )
        original_template = aws_client.cloudformation.get_template(
            StackName=stack.stack_name, TemplateStage="Original"
        )
        processed_template = aws_client.cloudformation.get_template(
            StackName=stack.stack_name, TemplateStage="Processed"
        )
        snapshot.add_transformer(snapshot.transform.regex(topic_name, "topic-name"))

        snapshot.match("original_template", original_template)
        snapshot.match("processed_template", processed_template)

    @markers.aws.validated
    def test_attribute_uses_macro(self, deploy_cfn_template, create_lambda_function, aws_client):
        macro_function_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/macros/return_random_string.py"
        )

        func_name = f"test_lambda_{short_uid()}"
        create_lambda_function(
            func_name=func_name,
            handler_file=macro_function_path,
            runtime=Runtime.python3_12,
            client=aws_client.lambda_,
        )

        macro_name = "GenerateRandom"
        deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/macro_resource.yml"
            ),
            parameters={"FunctionName": func_name, "MacroName": macro_name},
        )

        stack = deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__),
                "../../../../templates",
                "transformation_resource_att.yml",
            ),
            parameters={"Input": "test"},
        )

        resulting_value = stack.outputs["Parameter"]
        assert "test-" in resulting_value

    @markers.aws.validated
    @pytest.mark.skip(reason="Fn::Transform does not support array of transformations")
    def test_scope_order_and_parameters(
        self, deploy_cfn_template, create_lambda_function, snapshot, aws_client
    ):
        """
        The test validates the order of execution of transformations and also asserts that any type of
        transformation can receive inputs.
        """

        macro_function_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/macros/replace_string.py"
        )
        macro_name = "ReplaceString"
        func_name = f"test_lambda_{short_uid()}"
        create_lambda_function(
            func_name=func_name,
            handler_file=macro_function_path,
            runtime=Runtime.python3_12,
            client=aws_client.lambda_,
            timeout=1,
        )

        deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/macro_resource.yml"
            ),
            parameters={"FunctionName": func_name, "MacroName": macro_name},
        )

        stack = deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__),
                "../../../../templates/transformation_multiple_scope_parameter.yml",
            ),
        )

        processed_template = aws_client.cloudformation.get_template(
            StackName=stack.stack_name, TemplateStage="Processed"
        )
        snapshot.match("processed_template", processed_template)

    @pytest.mark.skip(reason="CFNV2:Validation")
    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        paths=[
            "$..TemplateBody.Resources.Parameter.LogicalResourceId",
            "$..TemplateBody.Conditions",
            "$..TemplateBody.Mappings",
            "$..TemplateBody.Parameters",
            "$..TemplateBody.StackId",
            "$..TemplateBody.StackName",
            "$..TemplateBody.Transform",
            "$..TemplateBody.Resources.Role.LogicalResourceId",
        ]
    )
    def test_capabilities_requirements(
        self, deploy_cfn_template, create_lambda_function, snapshot, cleanups, aws_client
    ):
        """
        The test validates that AWS will return an error about missing CAPABILITY_AUTOEXPAND when adding a
        resource during the transformation, and it will ask for CAPABILITY_NAMED_IAM when the new resource is a
        IAM role
        """

        macro_function_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/macros/add_role.py"
        )
        macro_name = "AddRole"
        func_name = f"test_lambda_{short_uid()}"
        create_lambda_function(
            func_name=func_name,
            handler_file=macro_function_path,
            runtime=Runtime.python3_12,
            client=aws_client.lambda_,
            timeout=1,
        )

        deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/macro_resource.yml"
            ),
            parameters={"FunctionName": func_name, "MacroName": macro_name},
        )

        stack_name = f"stack-{short_uid()}"
        args = {
            "StackName": stack_name,
            "TemplateBody": load_file(
                os.path.join(
                    os.path.dirname(__file__),
                    "../../../../templates/transformation_add_role.yml",
                )
            ),
        }
        with pytest.raises(botocore.exceptions.ClientError) as ex:
            aws_client.cloudformation.create_stack(**args)
        snapshot.match("error", ex.value.response)

        args["Capabilities"] = [
            "CAPABILITY_AUTO_EXPAND",  # Required to allow macro to add a role to template
            "CAPABILITY_NAMED_IAM",  # Required to allow CFn create added role
        ]
        aws_client.cloudformation.create_stack(**args)
        cleanups.append(lambda: aws_client.cloudformation.delete_stack(StackName=stack_name))
        aws_client.cloudformation.get_waiter("stack_create_complete").wait(StackName=stack_name)
        processed_template = aws_client.cloudformation.get_template(
            StackName=stack_name, TemplateStage="Processed"
        )
        snapshot.add_transformer(snapshot.transform.key_value("RoleName", "role-name"))
        snapshot.match("processed_template", processed_template)

    @pytest.mark.skip("CFNV2:GetTemplate")
    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        paths=[
            "$..Event.fragment.Conditions",
            "$..Event.fragment.Mappings",
            "$..Event.fragment.Outputs",
            "$..Event.fragment.Resources.Parameter.LogicalResourceId",
            "$..Event.fragment.StackId",
            "$..Event.fragment.StackName",
            "$..Event.fragment.Transform",
        ]
    )
    def test_validate_lambda_internals(
        self, deploy_cfn_template, create_lambda_function, snapshot, cleanups, aws_client
    ):
        """
        The test validates the content of the event pass into the macro lambda
        """
        macro_function_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/macros/print_internals.py"
        )

        macro_name = "PrintInternals"
        func_name = f"test_lambda_{short_uid()}"
        create_lambda_function(
            func_name=func_name,
            handler_file=macro_function_path,
            runtime=Runtime.python3_12,
            client=aws_client.lambda_,
            timeout=1,
        )

        deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/macro_resource.yml"
            ),
            parameters={"FunctionName": func_name, "MacroName": macro_name},
        )

        stack_name = f"stake-{short_uid()}"
        aws_client.cloudformation.create_stack(
            StackName=stack_name,
            Capabilities=["CAPABILITY_AUTO_EXPAND"],
            TemplateBody=load_template_file(
                os.path.join(
                    os.path.dirname(__file__),
                    "../../../../templates/transformation_print_internals.yml",
                )
            ),
        )
        cleanups.append(lambda: aws_client.cloudformation.delete_stack(StackName=stack_name))
        aws_client.cloudformation.get_waiter("stack_create_complete").wait(StackName=stack_name)

        processed_template = aws_client.cloudformation.get_template(
            StackName=stack_name, TemplateStage="Processed"
        )
        snapshot.match(
            "event",
            processed_template["TemplateBody"]["Resources"]["Parameter"]["Properties"]["Value"],
        )

    @pytest.mark.skip("CFNV2:Validation")
    @markers.aws.validated
    def test_to_validate_template_limit_for_macro(
        self, deploy_cfn_template, create_lambda_function, snapshot, aws_client
    ):
        """
        The test validates the max size of a template that can be passed into the macro function
        """
        macro_function_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/macros/format_template.py"
        )
        macro_name = "FormatTemplate"
        func_name = f"test_lambda_{short_uid()}"
        create_lambda_function(
            func_name=func_name,
            handler_file=macro_function_path,
            runtime=Runtime.python3_12,
            client=aws_client.lambda_,
            timeout=1,
        )

        deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/macro_resource.yml"
            ),
            parameters={"FunctionName": func_name, "MacroName": macro_name},
        )

        template_dict = parse_yaml(
            load_file(
                os.path.join(
                    os.path.dirname(__file__),
                    "../../../../templates/transformation_global_parameter.yml",
                )
            )
        )
        for n in range(0, 1000):
            template_dict["Resources"][f"Parameter{n}"] = deepcopy(
                template_dict["Resources"]["Parameter"]
            )

        template = yaml.dump(template_dict)

        with pytest.raises(botocore.exceptions.ClientError) as ex:
            aws_client.cloudformation.create_stack(
                StackName=f"stack-{short_uid()}", TemplateBody=template
            )

        response = ex.value.response
        response["Error"]["Message"] = response["Error"]["Message"].replace(
            template, "<template-body>"
        )
        snapshot.match("error_response", response)

    @pytest.mark.skip("CFNV2:Validation")
    @markers.aws.validated
    def test_error_pass_macro_as_reference(self, snapshot, aws_client):
        """
        This test shows that the CFn will reject any transformation name that has been specified as reference, for
        example, a parameter.
        """

        with pytest.raises(botocore.exceptions.ClientError) as ex:
            aws_client.cloudformation.create_stack(
                StackName=f"stack-{short_uid()}",
                TemplateBody=load_file(
                    os.path.join(
                        os.path.dirname(__file__),
                        "../../../../templates/transformation_macro_as_reference.yml",
                    )
                ),
                Capabilities=["CAPABILITY_AUTO_EXPAND"],
                Parameters=[{"ParameterKey": "MacroName", "ParameterValue": "NonExistent"}],
            )
        snapshot.match("error", ex.value.response)

    @pytest.mark.skip("CFNV2:GetTemplate")
    @markers.aws.validated
    def test_functions_and_references_during_transformation(
        self, deploy_cfn_template, create_lambda_function, snapshot, cleanups, aws_client
    ):
        """
        This tests shows the state of intrinsic functions during the execution of the macro
        """
        macro_function_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/macros/print_references.py"
        )
        macro_name = "PrintReferences"
        func_name = f"test_lambda_{short_uid()}"
        create_lambda_function(
            func_name=func_name,
            handler_file=macro_function_path,
            runtime=Runtime.python3_12,
            client=aws_client.lambda_,
            timeout=1,
        )

        deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/macro_resource.yml"
            ),
            parameters={"FunctionName": func_name, "MacroName": macro_name},
        )

        stack_name = f"stake-{short_uid()}"
        aws_client.cloudformation.create_stack(
            StackName=stack_name,
            Capabilities=["CAPABILITY_AUTO_EXPAND"],
            TemplateBody=load_template_file(
                os.path.join(
                    os.path.dirname(__file__),
                    "../../../../templates/transformation_macro_params_as_reference.yml",
                )
            ),
            Parameters=[{"ParameterKey": "MacroInput", "ParameterValue": "CreateStackInput"}],
        )
        cleanups.append(lambda: aws_client.cloudformation.delete_stack(StackName=stack_name))
        aws_client.cloudformation.get_waiter("stack_create_complete").wait(StackName=stack_name)

        processed_template = aws_client.cloudformation.get_template(
            StackName=stack_name, TemplateStage="Processed"
        )
        snapshot.match(
            "event",
            processed_template["TemplateBody"]["Resources"]["Parameter"]["Properties"]["Value"],
        )

    @pytest.mark.skip(reason="CFNV2:Validation")
    @pytest.mark.parametrize(
        "macro_function",
        [
            "return_unsuccessful_with_message.py",
            "return_unsuccessful_without_message.py",
            "return_invalid_template.py",
            "raise_error.py",
        ],
    )
    @markers.aws.validated
    def test_failed_state(
        self,
        deploy_cfn_template,
        create_lambda_function,
        snapshot,
        cleanups,
        macro_function,
        aws_client,
    ):
        """
        This test shows the error responses for different negative responses from the macro lambda
        """
        macro_function_path = os.path.join(
            os.path.dirname(__file__), "../../../../templates/macros/", macro_function
        )

        macro_name = "Unsuccessful"
        func_name = f"test_lambda_{short_uid()}"
        create_lambda_function(
            func_name=func_name,
            handler_file=macro_function_path,
            runtime=Runtime.python3_12,
            client=aws_client.lambda_,
            timeout=1,
        )

        deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/macro_resource.yml"
            ),
            parameters={"FunctionName": func_name, "MacroName": macro_name},
        )

        template = load_file(
            os.path.join(
                os.path.dirname(__file__),
                "../../../../templates/transformation_unsuccessful.yml",
            )
        )

        stack_name = f"stack-{short_uid()}"
        aws_client.cloudformation.create_stack(
            StackName=stack_name, Capabilities=["CAPABILITY_AUTO_EXPAND"], TemplateBody=template
        )
        cleanups.append(lambda: aws_client.cloudformation.delete_stack(StackName=stack_name))

        with pytest.raises(botocore.exceptions.WaiterError):
            aws_client.cloudformation.get_waiter("stack_create_complete").wait(StackName=stack_name)

        events = aws_client.cloudformation.describe_stack_events(StackName=stack_name)[
            "StackEvents"
        ]

        failed_events_by_policy = [
            event
            for event in events
            if "ResourceStatusReason" in event and event["ResourceStatus"] == "ROLLBACK_IN_PROGRESS"
        ]

        snapshot.add_transformer(snapshot.transform.cloudformation_api())
        snapshot.match("failed_description", failed_events_by_policy[0])

    @markers.aws.validated
    def test_pyplate_param_type_list(self, deploy_cfn_template, aws_client, snapshot):
        deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/pyplate_deploy_template.yml"
            ),
        )

        tags = "Env=Prod,Application=MyApp,BU=ModernisationTeam"
        param_tags = {pair.split("=")[0]: pair.split("=")[1] for pair in tags.split(",")}

        stack_with_macro = deploy_cfn_template(
            template_path=os.path.join(
                os.path.dirname(__file__), "../../../../templates/pyplate_example.yml"
            ),
            parameters={"Tags": tags},
        )

        bucket_name_output = stack_with_macro.outputs["BucketName"]
        assert bucket_name_output

        tagging = aws_client.s3.get_bucket_tagging(Bucket=bucket_name_output)
        tags_s3 = [tag for tag in tagging["TagSet"]]

        resp = []
        for tag in tags_s3:
            if tag["Key"] in param_tags:
                assert tag["Value"] == param_tags[tag["Key"]]
                resp.append([tag["Key"], tag["Value"]])
        assert len(tags_s3) >= len(param_tags)
        snapshot.match("tags", sorted(resp))


class TestStackEvents:
    @pytest.mark.skip(reason="CFNV2:Validation")
    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        paths=[
            "$..EventId",
            "$..PhysicalResourceId",
            "$..ResourceProperties",
            # TODO: we do not maintain parity here, just that the property exists
            "$..ResourceStatusReason",
        ]
    )
    def test_invalid_stack_deploy(self, deploy_cfn_template, aws_client, snapshot):
        logical_resource_id = "MyParameter"
        template = {
            "Resources": {
                logical_resource_id: {
                    "Type": "AWS::SSM::Parameter",
                    "Properties": {
                        # invalid: missing required property _type_
                        "Value": "abc123",
                    },
                },
            },
        }

        with pytest.raises(StackDeployError) as exc_info:
            deploy_cfn_template(template=json.dumps(template))

        stack_events = exc_info.value.events
        # filter out only the single create event that failed
        failed_events = [
            every
            for every in stack_events
            if every["ResourceStatus"] == "CREATE_FAILED"
            and every["LogicalResourceId"] == logical_resource_id
        ]
        assert len(failed_events) == 1
        failed_event = failed_events[0]

        snapshot.add_transformer(snapshot.transform.cloudformation_api())
        snapshot.match("failed_event", failed_event)
        assert "ResourceStatusReason" in failed_event


class TestPseudoParameters:
    @markers.aws.validated
    def test_stack_id(self, deploy_cfn_template, snapshot):
        template = {
            "Resources": {
                "MyParameter": {
                    "Type": "AWS::SSM::Parameter",
                    "Properties": {
                        "Type": "String",
                        "Value": {
                            "Ref": "AWS::StackId",
                        },
                    },
                },
            },
            "Outputs": {
                "StackId": {
                    "Value": {
                        "Fn::GetAtt": [
                            "MyParameter",
                            "Value",
                        ],
                    },
                },
            },
        }

        stack = deploy_cfn_template(template=json.dumps(template))

        snapshot.add_transformer(snapshot.transform.regex(stack.stack_id, "<stack-id>"))

        snapshot.match("StackId", stack.outputs["StackId"])
