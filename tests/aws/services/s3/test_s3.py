import base64
import contextlib
import datetime
import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
from importlib.util import find_spec
from io import BytesIO
from operator import itemgetter
from typing import TYPE_CHECKING
from urllib.parse import SplitResult, parse_qs, quote, urlencode, urlparse, urlunsplit
from zoneinfo import ZoneInfo

import boto3 as boto3
import botocore
import pytest
import requests
import xmltodict
from boto3.s3.transfer import KB, TransferConfig
from botocore import UNSIGNED
from botocore.auth import SigV4Auth
from botocore.client import Config
from botocore.exceptions import ClientError
from localstack_snapshot.snapshots.transformer import RegexTransformer

import localstack.config
from localstack import config
from localstack.aws.api.lambda_ import Runtime
from localstack.aws.api.s3 import StorageClass, TransitionDefaultMinimumObjectSize
from localstack.config import S3_VIRTUAL_HOSTNAME
from localstack.constants import (
    AWS_REGION_US_EAST_1,
    LOCALHOST_HOSTNAME,
)
from localstack.services.s3 import constants as s3_constants
from localstack.services.s3.utils import (
    RFC1123,
    etag_to_base_64_content_md5,
    parse_expiration_header,
    rfc_1123_datetime,
)
from localstack.testing.aws.util import in_default_partition, is_aws_cloud
from localstack.testing.config import (
    SECONDARY_TEST_AWS_ACCESS_KEY_ID,
    SECONDARY_TEST_AWS_SECRET_ACCESS_KEY,
    TEST_AWS_ACCESS_KEY_ID,
)
from localstack.testing.pytest import markers
from localstack.testing.snapshots.transformer_utility import TransformerUtility
from localstack.utils import testutil
from localstack.utils.aws.arns import get_partition
from localstack.utils.aws.request_context import mock_aws_request_headers
from localstack.utils.aws.resources import create_s3_bucket
from localstack.utils.files import load_file
from localstack.utils.run import run
from localstack.utils.strings import (
    checksum_crc32,
    checksum_crc32c,
    checksum_crc64nvme,
    hash_sha1,
    hash_sha256,
    long_uid,
    short_uid,
    to_bytes,
    to_str,
)
from localstack.utils.sync import retry
from localstack.utils.testutil import check_expected_lambda_log_events_length
from localstack.utils.urls import localstack_host as get_localstack_host
from tests.aws.services.s3.conftest import TEST_S3_IMAGE

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

LOG = logging.getLogger(__name__)


# transformer list to transform headers, that will be validated for some specific s3-tests
HEADER_TRANSFORMER = [
    TransformerUtility.jsonpath("$..HTTPHeaders.date", "date", reference_replacement=False),
    TransformerUtility.jsonpath(
        "$..HTTPHeaders.last-modified", "last-modified", reference_replacement=False
    ),
    TransformerUtility.jsonpath("$..HTTPHeaders.server", "server", reference_replacement=False),
    TransformerUtility.jsonpath("$..HTTPHeaders.x-amz-id-2", "id-2", reference_replacement=False),
    TransformerUtility.jsonpath(
        "$..HTTPHeaders.x-amz-request-id", "request-id", reference_replacement=False
    ),
    TransformerUtility.key_value("HostId", reference_replacement=False),
    TransformerUtility.key_value("RequestId", reference_replacement=False),
]

S3_ASSUME_ROLE_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "s3.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

S3_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:*",
            ],
            "Resource": "*",
        }
    ],
}


@pytest.fixture
def anonymous_client(aws_client_factory, region_name):
    """
    This fixture returns a factory that creates a client for a given service. This client is configured with credentials
    that can be effectively be treated as anonymous.
    """

    def _anonymous_client(service_name: str):
        return aws_client_factory.get_client(
            service_name=service_name,
            region_name=region_name,
            aws_access_key_id=SECONDARY_TEST_AWS_ACCESS_KEY_ID,
            aws_secret_access_key=SECONDARY_TEST_AWS_SECRET_ACCESS_KEY,
            config=Config(signature_version=UNSIGNED),
        )

    yield _anonymous_client


@pytest.fixture(scope="function")
def patch_s3_skip_signature_validation_false(monkeypatch):
    monkeypatch.setattr(config, "S3_SKIP_SIGNATURE_VALIDATION", False)


@pytest.fixture
def s3_multipart_upload(aws_client):
    def perform_multipart_upload(
        bucket, key, data=None, zipped=False, acl=None, parts: int = 1, **kwargs
    ):
        # beware, the last part can be under 5 MiB, but previous parts needs to be between 5MiB and 5GiB
        if acl:
            kwargs["ACL"] = acl
        multipart_upload_dict = aws_client.s3.create_multipart_upload(
            Bucket=bucket, Key=key, **kwargs
        )
        upload_id = multipart_upload_dict["UploadId"]
        data = data or (5 * short_uid())
        multipart_upload_parts = []
        for part in range(parts):
            # Write contents to memory rather than a file.
            part_number = part + 1

            part_data = data or (5 * short_uid())
            if part_number < parts and ((len_data := len(part_data)) < 5_242_880):
                # data must be at least 5MiB
                multiple = 5_242_880 // len_data
                part_data = part_data * (multiple + 1)

            part_data = to_bytes(part_data)
            upload_file_object = BytesIO(part_data)
            if zipped:
                upload_file_object = BytesIO()
                with gzip.GzipFile(fileobj=upload_file_object, mode="w") as filestream:
                    filestream.write(part_data)

            response = aws_client.s3.upload_part(
                Bucket=bucket,
                Key=key,
                Body=upload_file_object,
                PartNumber=part_number,
                UploadId=upload_id,
            )

            multipart_upload_parts.append({"ETag": response["ETag"], "PartNumber": part_number})
            # multiple parts won't work with zip, stop at one
            if zipped:
                break

        return aws_client.s3.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            MultipartUpload={"Parts": multipart_upload_parts},
            UploadId=upload_id,
        )

    return perform_multipart_upload


@pytest.fixture
def s3_multipart_upload_with_snapshot(aws_client, snapshot):
    def perform_multipart_upload(
        bucket: str, key: str, data: bytes, snapshot_prefix: str, **kwargs
    ):
        create_multipart_resp = aws_client.s3.create_multipart_upload(
            Bucket=bucket, Key=key, **kwargs
        )
        snapshot.match(f"{snapshot_prefix}-create-multipart", create_multipart_resp)
        upload_id = create_multipart_resp["UploadId"]

        # Write contents to memory rather than a file.
        upload_file_object = BytesIO(data)

        response = aws_client.s3.upload_part(
            Bucket=bucket,
            Key=key,
            Body=upload_file_object,
            PartNumber=1,
            UploadId=upload_id,
        )
        snapshot.match(f"{snapshot_prefix}-upload-part", response)

        response = aws_client.s3.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            MultipartUpload={"Parts": [{"ETag": response["ETag"], "PartNumber": 1}]},
            UploadId=upload_id,
        )
        snapshot.match(f"{snapshot_prefix}-compete-multipart", response)
        return response

    return perform_multipart_upload


@pytest.fixture
def create_tmp_folder_lambda():
    cleanup_folders = []

    def prepare_folder(path_to_lambda, run_command=None):
        tmp_dir = tempfile.mkdtemp()
        shutil.copy(path_to_lambda, tmp_dir)
        if run_command:
            run(f"cd {tmp_dir}; {run_command}")
        cleanup_folders.append(tmp_dir)
        return tmp_dir

    yield prepare_folder

    for folder in cleanup_folders:
        try:
            shutil.rmtree(folder)
        except Exception:
            LOG.warning("could not delete folder %s", folder)


@pytest.fixture
def allow_bucket_acl(s3_bucket, aws_client):
    """
    # Since April 2023, AWS will by default block setting ACL to your bucket and object. You need to manually disable
    # the BucketOwnershipControls and PublicAccessBlock to make your objects public.
    # See https://aws.amazon.com/about-aws/whats-new/2022/12/amazon-s3-automatically-enable-block-public-access-disable-access-control-lists-buckets-april-2023/
    """
    aws_client.s3.delete_bucket_ownership_controls(Bucket=s3_bucket)
    aws_client.s3.delete_public_access_block(Bucket=s3_bucket)


def _filter_header(param: dict) -> dict:
    return {k: v for k, v in param.items() if k.startswith("x-amz") or k in ["content-type"]}


def _simple_bucket_policy(s3_bucket: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Action": "s3:GetObject",
                "Effect": "Allow",
                "Resource": f"arn:aws:s3:::{s3_bucket}/*",
                "Principal": {"AWS": "*"},
            }
        ],
    }


class TestS3:
    @pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="KMS not enabled in S3 image")
    @markers.aws.validated
    def test_copy_object_kms(self, s3_bucket, kms_create_key, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        # because of the kms-key, the etag will be different on AWS
        # FIXME there is currently no server side encryption is place and thus the etag is the same for the copied objects in LS
        snapshot.add_transformer(
            snapshot.transform.jsonpath(
                "$..CopyObjectResult.ETag", "copy-etag", reference_replacement=False
            )
        )
        snapshot.add_transformer(
            snapshot.transform.jsonpath(
                "$..get-copied-object.ETag", "etag", reference_replacement=False
            )
        )
        snapshot.add_transformer(snapshot.transform.key_value("SSEKMSKeyId", "key-id"))
        key_id = kms_create_key()["KeyId"]
        body = "hello world"
        aws_client.s3.put_object(Bucket=s3_bucket, Key="mykey", Body=body)

        response = aws_client.s3.get_object(Bucket=s3_bucket, Key="mykey")
        snapshot.match("get-object", response)
        response = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/mykey",
            Key="copiedkey",
            BucketKeyEnabled=True,
            SSEKMSKeyId=key_id,
            ServerSideEncryption="aws:kms",
        )
        snapshot.match("copy-object", response)

        response = aws_client.s3.get_object(Bucket=s3_bucket, Key="copiedkey")
        snapshot.match("get-copied-object", response)

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(paths=["$..AccessPointAlias"])
    def test_region_header_exists_outside_us_east_1(
        self, s3_create_bucket_with_client, snapshot, aws_client_factory
    ):
        """
        We need the region to be hardcoded to something else than `us-east-1`, as you cannot create a bucket with
        a LocationConstraint in that region
        """
        snapshot.add_transformer(snapshot.transform.s3_api())
        region_us_west_2 = "us-west-2"
        snapshot.add_transformer(RegexTransformer(region_us_west_2, "<region>"))
        client_us_east_1 = aws_client_factory(region_name=AWS_REGION_US_EAST_1).s3
        bucket_us_west_2 = f"test-bucket-{short_uid()}"
        s3_create_bucket_with_client(
            client_us_east_1,
            Bucket=bucket_us_west_2,
            CreateBucketConfiguration={"LocationConstraint": region_us_west_2},
        )

        response = client_us_east_1.head_bucket(Bucket=bucket_us_west_2)
        assert (
            response["ResponseMetadata"]["HTTPHeaders"]["x-amz-bucket-region"] == region_us_west_2
        )
        snapshot.match("head_bucket", response)
        response = client_us_east_1.list_objects_v2(Bucket=bucket_us_west_2)
        assert (
            response["ResponseMetadata"]["HTTPHeaders"]["x-amz-bucket-region"] == region_us_west_2
        )
        snapshot.match("list_objects_v2", response)

        bucket_us_east_1 = f"test-bucket-{short_uid()}"
        s3_create_bucket_with_client(client_us_east_1, Bucket=bucket_us_east_1)
        response = client_us_east_1.head_bucket(Bucket=bucket_us_east_1)
        assert (
            response["ResponseMetadata"]["HTTPHeaders"]["x-amz-bucket-region"]
            == AWS_REGION_US_EAST_1
        )

    @markers.aws.validated
    # TODO list-buckets contains other buckets when running in CI
    @markers.snapshot.skip_snapshot_verify(paths=["$..Prefix", "$..list-buckets.Buckets"])
    def test_delete_bucket_with_content(self, s3_bucket, s3_empty_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        bucket_name = s3_bucket

        for i in range(0, 10, 1):
            body = "test-" + str(i)
            key = "test-key-" + str(i)
            aws_client.s3.put_object(Bucket=bucket_name, Key=key, Body=body)

        resp = aws_client.s3.list_objects(Bucket=bucket_name, MaxKeys=100)
        snapshot.match("list-objects", resp)
        assert 10 == len(resp["Contents"])

        s3_empty_bucket(bucket_name)
        aws_client.s3.delete_bucket(Bucket=bucket_name)

        resp = aws_client.s3.list_buckets()
        # TODO - this fails in the CI pipeline and is currently skipped from verification
        snapshot.match("list-buckets", resp)
        assert bucket_name not in [b["Name"] for b in resp["Buckets"]]

    @markers.aws.validated
    def test_put_and_get_object_with_utf8_key(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())

        response = aws_client.s3.put_object(Bucket=s3_bucket, Key="Ā0Ä", Body=b"abc123")
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200
        snapshot.match("put-object", response)

        response = aws_client.s3.get_object(Bucket=s3_bucket, Key="Ā0Ä")
        snapshot.match("get-object", response)
        assert response["Body"].read() == b"abc123"

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(paths=["$..MaxAttemptsReached"])
    @markers.snapshot.skip_snapshot_verify(
        paths=[
            "$..HTTPHeaders.connection",
            # TODO content-length and type is wrong, skipping for now
            "$..HTTPHeaders.content-length",  # 58, but should be 0 # TODO!!!
            "$..HTTPHeaders.content-type",  # application/xml but should not be set
        ],
    )
    def test_put_and_get_object_with_content_language_disposition(
        self, s3_bucket, snapshot, aws_client
    ):
        snapshot.add_transformer(snapshot.transform.s3_api())
        snapshot.add_transformer(HEADER_TRANSFORMER)

        response = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="test",
            Body=b"abc123",
            ContentLanguage="de",
            ContentDisposition='attachment; filename="foo.jpg"',
            CacheControl="no-cache",
        )
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200
        snapshot.match("put-object", response)
        snapshot.match("put-object-headers", response["ResponseMetadata"])

        response = aws_client.s3.get_object(Bucket=s3_bucket, Key="test")
        snapshot.match("get-object", response)
        snapshot.match("get-object-headers", response["ResponseMetadata"])
        assert response["Body"].read() == b"abc123"

    @markers.aws.validated
    @pytest.mark.parametrize(
        "use_virtual_address",
        [True, False],
    )
    def test_object_with_slashes_in_key(
        self, s3_bucket, aws_client_factory, use_virtual_address, snapshot
    ):
        snapshot.add_transformer(snapshot.transform.key_value("Name"))
        s3_config = {"addressing_style": "virtual"} if use_virtual_address else {}
        s3_client = aws_client_factory(
            config=Config(s3=s3_config),
            endpoint_url=_endpoint_url(),
        ).s3

        s3_client.put_object(Bucket=s3_bucket, Key="/foo", Body=b"foobar")
        s3_client.put_object(Bucket=s3_bucket, Key="bar", Body=b"barfoo")
        s3_client.put_object(Bucket=s3_bucket, Key="/bar/foo/", Body=b"test")

        list_objects = s3_client.list_objects_v2(Bucket=s3_bucket)
        snapshot.match("list-objects-slashes", list_objects)

        with pytest.raises(ClientError, match="NoSuchKey"):
            s3_client.get_object(Bucket=s3_bucket, Key="foo")

        with pytest.raises(ClientError, match="NoSuchKey"):
            s3_client.get_object(Bucket=s3_bucket, Key="//foo")

        with pytest.raises(ClientError, match="NoSuchKey"):
            s3_client.get_object(Bucket=s3_bucket, Key="/bar")

        response = s3_client.get_object(Bucket=s3_bucket, Key="/foo")
        assert response["Body"].read() == b"foobar"
        response = s3_client.get_object(Bucket=s3_bucket, Key="bar")
        assert response["Body"].read() == b"barfoo"
        response = s3_client.get_object(Bucket=s3_bucket, Key="/bar/foo/")
        assert response["Body"].read() == b"test"

    @markers.aws.validated
    def test_metadata_header_character_decoding(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        # Object metadata keys should accept keys with underscores
        # https://github.com/localstack/localstack/issues/1790
        # put object
        object_key = "key-with-metadata"
        metadata = {"TEST_META_1": "foo", "__meta_2": "bar"}
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Metadata=metadata, Body="foo")
        metadata_saved = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object", metadata_saved)

        # note that casing is removed (since headers are case-insensitive)
        assert metadata_saved["Metadata"] == {"test_meta_1": "foo", "__meta_2": "bar"}

    @markers.aws.validated
    def test_upload_file_multipart(self, s3_bucket, tmpdir, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        key = "my-key"
        # https://boto3.amazonaws.com/v1/documentation/api/latest/guide/s3.html#multipart-transfers
        tranfer_config = TransferConfig(multipart_threshold=5 * KB, multipart_chunksize=1 * KB)

        file = tmpdir / "test-file.bin"
        data = b"1" * (6 * KB)  # create 6 kilobytes of ones
        file.write(data=data, mode="w")
        aws_client.s3.upload_file(
            Bucket=s3_bucket, Key=key, Filename=str(file.realpath()), Config=tranfer_config
        )

        obj = aws_client.s3.get_object(Bucket=s3_bucket, Key=key)
        assert obj["Body"].read() == data, f"body did not contain expected data {obj}"
        snapshot.match("get_object", obj)

    @markers.aws.validated
    @pytest.mark.parametrize(
        "key",
        [
            "file%2Fname",
            "test@key/",
            "test%123",
            "test%percent",
            "test key/",
            "test key//",
            "test%123/",
            "a/%F0%9F%98%80/",
        ],
    )
    def test_put_get_object_special_character(self, s3_bucket, aws_client, snapshot, key):
        snapshot.add_transformer(snapshot.transform.s3_api())
        resp = aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body=b"test")
        snapshot.match("put-object-special-char", resp)
        resp = aws_client.s3.list_objects_v2(Bucket=s3_bucket)
        snapshot.match("list-object-special-char", resp)
        resp = aws_client.s3.get_object(Bucket=s3_bucket, Key=key)
        snapshot.match("get-object-special-char", resp)
        resp = aws_client.s3.delete_object(Bucket=s3_bucket, Key=key)
        snapshot.match("del-object-special-char", resp)

    @markers.aws.validated
    def test_put_get_object_single_character_trailing_slash(self, s3_bucket, aws_client, snapshot):
        snapshot.add_transformer(snapshot.transform.key_value("Name"))
        single_chars = [
            "a/",
            "t/",
            "u/",
        ]
        for char in single_chars:
            resp = aws_client.s3.put_object(Bucket=s3_bucket, Key=char, Body=b"test")
            snapshot.match(f"put-object-single-char-{char}", resp)
            resp = aws_client.s3.get_object(Bucket=s3_bucket, Key=char)
            snapshot.match(f"get-object-single-char-{char}", resp)

        resp = aws_client.s3.list_objects_v2(Bucket=s3_bucket)
        snapshot.match("list-objects-single-char", resp)

    @markers.aws.validated
    def test_copy_object_special_character(self, s3_bucket, s3_create_bucket, aws_client, snapshot):
        snapshot.add_transformer(snapshot.transform.s3_api())
        dest_bucket = s3_create_bucket()
        special_keys = [
            "file%2Fname",
            "test@key/",
            "test key/",
            "test key//",
            "a/%F0%9F%98%80/",
            "test+key",
        ]

        for key in special_keys:
            resp = aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body=b"test")
            snapshot.match(f"put-object-src-special-char-{key}", resp)

            copy_obj = aws_client.s3.copy_object(
                Bucket=dest_bucket,
                Key=key,
                CopySource=f"{s3_bucket}/{key}",
            )
            snapshot.match(f"copy-object-special-char-{key}", copy_obj)

        resp = aws_client.s3.list_objects_v2(Bucket=dest_bucket)
        snapshot.match("list-object-copy-dest-special-char", resp)

    @markers.aws.validated
    def test_copy_object_special_character_plus_for_space(
        self, s3_bucket, aws_client, aws_http_client_factory
    ):
        """
        Different languages don't always handle the space character the same way when encoding URL. Python uses %20
        when Go for example encodes it with `+`, which is the form way. This leads to a specific edge case for
        the CopySource header.
        """
        object_key = "test key.txt"
        dest_key = "dest-key"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="test-body")

        s3_http_client = aws_http_client_factory("s3", signer_factory=SigV4Auth)
        bucket_url = _bucket_url(s3_bucket)

        copy_object_url = f"{bucket_url}/{dest_key}"
        copy_source = f"{s3_bucket}%2F{object_key.replace(' ', '+')}"
        copy_resp = s3_http_client.put(
            copy_object_url,
            headers={
                "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
                "x-amz-copy-source": copy_source,
            },
        )
        assert copy_resp.ok
        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=dest_key)
        assert head_object["ResponseMetadata"]["HTTPStatusCode"] == 200

    @markers.aws.validated
    @pytest.mark.parametrize(
        "use_virtual_address",
        [True, False],
    )
    def test_url_encoded_key(self, s3_bucket, aws_client_factory, snapshot, use_virtual_address):
        """Boto adds a trailing slash always?"""
        snapshot.add_transformer(snapshot.transform.key_value("Name"))
        s3_config = {"addressing_style": "virtual"} if use_virtual_address else {}
        s3_client = aws_client_factory(
            config=Config(s3=s3_config),
            endpoint_url=_endpoint_url(),
        ).s3

        key = "test@key/"
        s3_client.put_object(Bucket=s3_bucket, Key=key, Body=b"test-non-encoded")
        encoded_key = "test%40key/"
        s3_client.put_object(Bucket=s3_bucket, Key=encoded_key, Body=b"test-encoded")
        encoded_key_no_trailing = "test%40key"
        s3_client.put_object(
            Bucket=s3_bucket, Key=encoded_key_no_trailing, Body=b"test-encoded-no-trailing"
        )
        # assert that one did not override the over, and that both key are different
        assert s3_client.get_object(Bucket=s3_bucket, Key=key)["Body"].read() == b"test-non-encoded"
        assert (
            s3_client.get_object(Bucket=s3_bucket, Key=encoded_key)["Body"].read()
            == b"test-encoded"
        )
        assert (
            s3_client.get_object(Bucket=s3_bucket, Key=encoded_key_no_trailing)["Body"].read()
            == b"test-encoded-no-trailing"
        )

        resp = s3_client.list_objects_v2(Bucket=s3_bucket)
        snapshot.match("list-object-encoded-char", resp)

    @markers.aws.validated
    def test_get_object_no_such_bucket(self, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=f"does-not-exist-{short_uid()}", Key="foobar")

        snapshot.match("expected_error", e.value.response)

    @markers.aws.validated
    def test_delete_bucket_no_such_bucket(self, snapshot, aws_client):
        with pytest.raises(ClientError) as e:
            aws_client.s3.delete_bucket(Bucket="does-not-exist-localstack-test")

        snapshot.match("expected_error", e.value.response)

    @markers.aws.validated
    def test_get_bucket_notification_configuration_no_such_bucket(self, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_notification_configuration(
                Bucket=f"doesnotexist-{short_uid()}"
            )

        snapshot.match("expected_error", e.value.response)

    @markers.aws.validated
    def test_get_object_attributes(self, s3_bucket, snapshot, s3_multipart_upload, aws_client):
        aws_client.s3.put_object(Bucket=s3_bucket, Key="data.txt", Body=b"69\n420\n")
        response = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key="data.txt",
            ObjectAttributes=["StorageClass", "ETag", "ObjectSize", "ObjectParts", "Checksum"],
        )
        snapshot.match("object-attrs", response)

        multipart_key = "test-get-obj-attrs-multipart"
        s3_multipart_upload(bucket=s3_bucket, key=multipart_key, data="upload-part-1" * 5)
        response = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=multipart_key,
            ObjectAttributes=["StorageClass", "ETag", "ObjectSize", "ObjectParts"],
        )
        snapshot.match("object-attrs-multiparts-1-part", response)

        multipart_key = "test-get-obj-attrs-multipart-2"
        s3_multipart_upload(bucket=s3_bucket, key=multipart_key, data="upload-part-1" * 5, parts=2)
        response = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=multipart_key,
            ObjectAttributes=["StorageClass", "ETag", "ObjectSize", "ObjectParts"],
            MaxParts=3,
        )
        snapshot.match("object-attrs-multiparts-2-parts", response)

        multipart_key = "test-get-obj-attrs-multipart-2"
        s3_multipart_upload(bucket=s3_bucket, key=multipart_key, data="upload-part-1" * 5, parts=2)
        response = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=multipart_key,
            ObjectAttributes=["StorageClass", "ETag", "ObjectSize", "Checksum"],
            MaxParts=3,
        )
        snapshot.match("object-attrs-multiparts-2-parts-checksum", response)

    @markers.aws.validated
    def test_get_object_attributes_with_space(
        self, s3_bucket, aws_client, aws_http_client_factory, snapshot
    ):
        """
        It seems AWS SDKs are aligning themselves and are now putting whitespace between comas in headers list
        See https://github.com/aws/aws-sdk-ruby/issues/3032
        https://www.rfc-editor.org/rfc/rfc9110.html#name-lists-rule-abnf-extension
        """
        object_key = "test-attrs"
        aws_client.s3.put_object(
            Bucket=s3_bucket, Key=object_key, Body="test-body", ChecksumAlgorithm="SHA256"
        )

        s3_http_client = aws_http_client_factory("s3", signer_factory=SigV4Auth)
        bucket_url = _bucket_url(s3_bucket)

        get_object_attrs_url = f"{bucket_url}/{object_key}?attributes"
        get_obj_attrs_resp = s3_http_client.get(
            get_object_attrs_url,
            headers={
                "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
                "x-amz-object-attributes": "ETag, Checksum, ObjectParts, StorageClass, ObjectSize",
            },
        )
        assert get_obj_attrs_resp.ok
        body = xmltodict.parse(get_obj_attrs_resp.content)
        snapshot.match("get-attrs-with-whitespace", body)

        get_obj_attrs_resp = s3_http_client.get(
            get_object_attrs_url,
            headers={
                "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
                "x-amz-object-attributes": "ETag,Checksum,ObjectParts,StorageClass,ObjectSize",
            },
        )
        assert get_obj_attrs_resp.ok
        body = xmltodict.parse(get_obj_attrs_resp.content)
        snapshot.match("get-attrs-without-whitespace", body)

    @markers.aws.validated
    def test_get_object_attributes_versioned(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        key = "key-attrs-versioned"
        put_obj_1 = aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body=b"69\n420\n")
        snapshot.match("put-obj-v1", put_obj_1)

        put_obj_2 = aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body=b"version 2")
        snapshot.match("put-obj-v2", put_obj_2)

        response = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key,
            ObjectAttributes=["StorageClass", "ETag", "ObjectSize", "ObjectParts", "Checksum"],
        )
        snapshot.match("object-attrs", response)

        response = aws_client.s3.delete_object(Bucket=s3_bucket, Key=key)
        snapshot.match("delete-key", response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object_attributes(
                Bucket=s3_bucket,
                Key=key,
                ObjectAttributes=["StorageClass", "ETag", "ObjectSize"],
            )
        snapshot.match("deleted-object-attrs", e.value.response)

        response = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key,
            VersionId=put_obj_1["VersionId"],
            ObjectAttributes=["StorageClass", "ETag", "ObjectSize"],
        )
        snapshot.match("get-object-attrs-v1", response)

    @markers.aws.validated
    def test_multipart_and_list_parts(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("DisplayName", reference_replacement=False),
                snapshot.transform.key_value("UploadId"),
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value(
                    "ID", value_replacement="owner-id", reference_replacement=False
                ),
            ]
        )

        key_name = "test-list-parts"
        response = aws_client.s3.create_multipart_upload(Bucket=s3_bucket, Key=key_name)
        snapshot.match("create-multipart", response)
        upload_id = response["UploadId"]

        list_part = aws_client.s3.list_parts(Bucket=s3_bucket, Key=key_name, UploadId=upload_id)
        snapshot.match("list-part-after-created", list_part)

        list_multipart_uploads = aws_client.s3.list_multipart_uploads(Bucket=s3_bucket)
        snapshot.match("list-all-uploads", list_multipart_uploads)

        # Write contents to memory rather than a file.
        data = "upload-part-1" * 5
        data = to_bytes(data)
        upload_file_object = BytesIO(data)

        response = aws_client.s3.upload_part(
            Bucket=s3_bucket,
            Key=key_name,
            Body=upload_file_object,
            PartNumber=1,
            UploadId=upload_id,
        )
        snapshot.match("upload-part", response)
        list_part = aws_client.s3.list_parts(Bucket=s3_bucket, Key=key_name, UploadId=upload_id)
        snapshot.match("list-part-after-upload", list_part)

        list_multipart_uploads = aws_client.s3.list_multipart_uploads(Bucket=s3_bucket)
        snapshot.match("list-all-uploads-after", list_multipart_uploads)

        multipart_upload_parts = [{"ETag": response["ETag"], "PartNumber": 1}]

        response = aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload={"Parts": multipart_upload_parts},
            UploadId=upload_id,
        )
        snapshot.match("complete-multipart", response)
        with pytest.raises(ClientError) as e:
            aws_client.s3.list_parts(Bucket=s3_bucket, Key=key_name, UploadId=upload_id)
        snapshot.match("list-part-after-complete-exc", e.value.response)

        list_multipart_uploads = aws_client.s3.list_multipart_uploads(Bucket=s3_bucket)
        snapshot.match("list-all-uploads-completed", list_multipart_uploads)

        head_object = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=key_name, ChecksumMode="ENABLED"
        )
        snapshot.match("head-multipart-checksum", head_object)

        get_object = aws_client.s3.get_object(
            Bucket=s3_bucket, Key=key_name, ChecksumMode="ENABLED"
        )
        snapshot.match("get-multipart-checksum", get_object)

    @markers.aws.validated
    def test_multipart_no_such_upload(self, s3_bucket, snapshot, aws_client):
        fake_upload_id = "fakeid"
        fake_key = "fake-key"

        with pytest.raises(ClientError) as e:
            aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=fake_key,
                Body=BytesIO(b"data"),
                PartNumber=1,
                UploadId=fake_upload_id,
            )
        snapshot.match("upload-exc", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket, Key=fake_key, UploadId=fake_upload_id
            )
        snapshot.match("complete-exc", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.abort_multipart_upload(
                Bucket=s3_bucket, Key=fake_key, UploadId=fake_upload_id
            )
        snapshot.match("abort-exc", e.value.response)

    @markers.aws.validated
    def test_multipart_complete_multipart_too_small(self, s3_bucket, snapshot, aws_client):
        key_name = "test-upload-part-exc"
        response = aws_client.s3.create_multipart_upload(Bucket=s3_bucket, Key=key_name)
        upload_id = response["UploadId"]

        parts = []

        for i in range(1, 3):
            upload_part = aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name,
                Body=BytesIO(b"data"),
                PartNumber=i,
                UploadId=upload_id,
            )
            parts.append({"ETag": upload_part["ETag"], "PartNumber": i})
            snapshot.match(f"upload-part{i}", upload_part)

        with pytest.raises(ClientError) as e:
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket, Key=key_name, UploadId=upload_id
            )
        snapshot.match("complete-exc-no-parts", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket, Key=key_name, UploadId=upload_id, MultipartUpload={"Parts": parts}
            )
        snapshot.match("complete-exc-too-small", e.value.response)

    @markers.aws.validated
    def test_multipart_complete_multipart_wrong_part(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("UploadId"))
        key_name = "test-upload-part-exc"
        response = aws_client.s3.create_multipart_upload(Bucket=s3_bucket, Key=key_name)
        upload_id = response["UploadId"]

        upload_part = aws_client.s3.upload_part(
            Bucket=s3_bucket,
            Key=key_name,
            Body=BytesIO(b"data"),
            PartNumber=1,
            UploadId=upload_id,
        )
        part_etag = upload_part["ETag"]

        with pytest.raises(ClientError) as e:
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                UploadId=upload_id,
                MultipartUpload={"Parts": [{"ETag": part_etag, "PartNumber": 2}]},
            )
        snapshot.match("complete-exc-wrong-part-number", e.value.response)

        with pytest.raises(ClientError) as e:
            wrong_etag = "d41d8cd98f00b204e9800998ecf8427e"
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                UploadId=upload_id,
                MultipartUpload={"Parts": [{"ETag": wrong_etag, "PartNumber": 1}]},
            )
        snapshot.match("complete-exc-wrong-etag", e.value.response)

    @markers.aws.validated
    def test_put_and_get_object_with_hash_prefix(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        key_name = "#key-with-hash-prefix"
        content = b"test 123"
        response = aws_client.s3.put_object(Bucket=s3_bucket, Key=key_name, Body=content)
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200
        snapshot.match("put-object", response)

        response = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_name)
        snapshot.match("get-object", response)
        assert response["Body"].read() == content

    @markers.aws.validated
    def test_invalid_range_error(self, s3_bucket, snapshot, aws_client):
        key = "my-key"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body=b"abcdefgh")

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=s3_bucket, Key=key, Range="bytes=1024-4096")
        snapshot.match("exc", e.value.response)

    @markers.aws.validated
    def test_range_key_not_exists(self, s3_bucket, snapshot, aws_client):
        key = "my-key"
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=s3_bucket, Key=key, Range="bytes=1024-4096")

        snapshot.match("exc", e.value.response)

    @markers.aws.validated
    def test_create_bucket_via_host_name(self, s3_vhost_client, aws_client, region_name):
        # TODO check redirection (happens in AWS because of region name), should it happen in LS?
        # https://docs.aws.amazon.com/AmazonS3/latest/userguide/VirtualHosting.html#VirtualHostingBackwardsCompatibility
        bucket_name = f"test-{short_uid()}"
        try:
            response = s3_vhost_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={
                    "LocationConstraint": region_name
                    if region_name != "us-east-1"
                    else "eu-central-1"
                },
            )
            assert "Location" in response
            assert response["ResponseMetadata"]["HTTPStatusCode"] == 200
            response = s3_vhost_client.get_bucket_location(Bucket=bucket_name)
            assert response["ResponseMetadata"]["HTTPStatusCode"] == 200
            assert (
                response["LocationConstraint"] == region_name
                if region_name != "us-east-1"
                else "eu-central-1"
            )
        finally:
            s3_vhost_client.delete_bucket(Bucket=bucket_name)

    @markers.aws.validated
    def test_get_bucket_policy(self, s3_bucket, snapshot, aws_client, allow_bucket_acl, account_id):
        snapshot.add_transformer(snapshot.transform.key_value("Resource"))
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_policy(Bucket=s3_bucket)
        snapshot.match("get-bucket-policy-no-such-bucket-policy", e.value.response)

        policy = _simple_bucket_policy(s3_bucket)
        aws_client.s3.put_bucket_policy(Bucket=s3_bucket, Policy=json.dumps(policy))

        # retrieve and check policy config
        response = aws_client.s3.get_bucket_policy(Bucket=s3_bucket)
        snapshot.match("get-bucket-policy", response)
        assert policy == json.loads(response["Policy"])

        response = aws_client.s3.get_bucket_policy(Bucket=s3_bucket, ExpectedBucketOwner=account_id)
        snapshot.match("get-bucket-policy-with-expected-bucket-owner", response)
        assert policy == json.loads(response["Policy"])

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_policy(Bucket=s3_bucket, ExpectedBucketOwner="000000000002")
        snapshot.match("get-bucket-policy-with-expected-bucket-owner-error", e.value.response)

    @pytest.mark.parametrize(
        "invalid_account_id", ["0000", "0000000000020", "abcd", "aa000000000$"]
    )
    @markers.aws.validated
    def test_get_bucket_policy_invalid_account_id(
        self, s3_bucket, snapshot, aws_client, invalid_account_id
    ):
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_policy(
                Bucket=s3_bucket, ExpectedBucketOwner=invalid_account_id
            )

        snapshot.match("get-bucket-policy-invalid-bucket-owner", e.value.response)

    @markers.aws.validated
    def test_put_bucket_policy(self, s3_bucket, snapshot, aws_client, allow_bucket_acl):
        # just for the joke: Response syntax HTTP/1.1 200
        # sample response: HTTP/1.1 204 No Content
        # https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketPolicy.html
        snapshot.add_transformer(snapshot.transform.key_value("Resource"))
        # put bucket policy
        policy = _simple_bucket_policy(s3_bucket)
        response = aws_client.s3.put_bucket_policy(Bucket=s3_bucket, Policy=json.dumps(policy))
        snapshot.match("put-bucket-policy", response)

        response = aws_client.s3.get_bucket_policy(Bucket=s3_bucket)
        snapshot.match("get-bucket-policy", response)
        assert policy == json.loads(response["Policy"])

    @markers.aws.validated
    def test_put_bucket_policy_expected_bucket_owner(
        self, s3_bucket, snapshot, aws_client, allow_bucket_acl, account_id, secondary_account_id
    ):
        snapshot.add_transformer(snapshot.transform.key_value("Resource"))
        policy = _simple_bucket_policy(s3_bucket)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_policy(
                Bucket=s3_bucket,
                Policy=json.dumps(policy),
                ExpectedBucketOwner=secondary_account_id,
            )
        snapshot.match("put-bucket-policy-with-expected-bucket-owner-error", e.value.response)

        response = aws_client.s3.put_bucket_policy(
            Bucket=s3_bucket, Policy=json.dumps(policy), ExpectedBucketOwner=account_id
        )
        snapshot.match("put-bucket-policy-with-expected-bucket-owner", response)

    @pytest.mark.parametrize(
        "invalid_account_id", ["0000", "0000000000020", "abcd", "aa000000000$"]
    )
    @markers.aws.validated
    def test_put_bucket_policy_invalid_account_id(
        self, s3_bucket, snapshot, aws_client, invalid_account_id
    ):
        snapshot.add_transformer(snapshot.transform.key_value("Resource"))
        policy = _simple_bucket_policy(s3_bucket)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_policy(
                Bucket=s3_bucket, Policy=json.dumps(policy), ExpectedBucketOwner=invalid_account_id
            )

        snapshot.match("put-bucket-policy-invalid-bucket-owner", e.value.response)

    @markers.aws.validated
    def test_delete_bucket_policy(self, s3_bucket, snapshot, aws_client, allow_bucket_acl):
        snapshot.add_transformer(snapshot.transform.key_value("Resource"))
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))

        policy = _simple_bucket_policy(s3_bucket)
        aws_client.s3.put_bucket_policy(Bucket=s3_bucket, Policy=json.dumps(policy))

        response = aws_client.s3.delete_bucket_policy(Bucket=s3_bucket)
        snapshot.match("delete-bucket-policy", response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_policy(Bucket=s3_bucket)
        snapshot.match("get-bucket-policy-no-such-bucket-policy", e.value.response)

    @markers.aws.validated
    def test_delete_bucket_policy_expected_bucket_owner(
        self, s3_bucket, snapshot, aws_client, allow_bucket_acl, account_id, secondary_account_id
    ):
        snapshot.add_transformer(snapshot.transform.key_value("Resource"))
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))

        policy = _simple_bucket_policy(s3_bucket)
        aws_client.s3.put_bucket_policy(Bucket=s3_bucket, Policy=json.dumps(policy))

        with pytest.raises(ClientError) as e:
            aws_client.s3.delete_bucket_policy(
                Bucket=s3_bucket, ExpectedBucketOwner=secondary_account_id
            )
        snapshot.match("delete-bucket-policy-with-expected-bucket-owner-error", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.delete_bucket_policy(Bucket=s3_bucket, ExpectedBucketOwner="invalid")
        snapshot.match("delete-bucket-policy-invalid-bucket-owner", e.value.response)

        response = aws_client.s3.delete_bucket_policy(
            Bucket=s3_bucket, ExpectedBucketOwner=account_id
        )
        snapshot.match("delete-bucket-policy-with-expected-bucket-owner", response)

    @markers.aws.validated
    def test_put_object_tagging_empty_list(self, s3_bucket, snapshot, aws_client):
        key = "my-key"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body=b"abcdefgh")

        object_tags = aws_client.s3.get_object_tagging(Bucket=s3_bucket, Key=key)
        snapshot.match("created-object-tags", object_tags)

        tag_set = {"TagSet": [{"Key": "tag1", "Value": "tag1"}, {"Key": "tag2", "Value": ""}]}
        aws_client.s3.put_object_tagging(Bucket=s3_bucket, Key=key, Tagging=tag_set)

        object_tags = aws_client.s3.get_object_tagging(Bucket=s3_bucket, Key=key)
        snapshot.match("updated-object-tags", object_tags)

        aws_client.s3.put_object_tagging(Bucket=s3_bucket, Key=key, Tagging={"TagSet": []})

        object_tags = aws_client.s3.get_object_tagging(Bucket=s3_bucket, Key=key)
        snapshot.match("deleted-object-tags", object_tags)

    @markers.aws.validated
    def test_head_object_fields(self, s3_bucket, snapshot, aws_client):
        key = "my-key"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body=b"abcdefgh")
        response = aws_client.s3.head_object(Bucket=s3_bucket, Key=key)
        snapshot.match("head-object", response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.head_object(Bucket=s3_bucket, Key="doesnotexist")
        snapshot.match("head-object-404", e.value.response)

    @markers.aws.validated
    def test_get_object_after_deleted_in_versioned_bucket(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("VersionId"))
        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket, VersioningConfiguration={"Status": "Enabled"}
        )

        key = "my-key"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body=b"abcdefgh")

        s3_obj = aws_client.s3.get_object(Bucket=s3_bucket, Key=key)
        snapshot.match("get-object", s3_obj)

        aws_client.s3.delete_object(Bucket=s3_bucket, Key=key)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=s3_bucket, Key=key)

        snapshot.match("get-object-after-delete", e.value.response)

    @markers.aws.validated
    def test_s3_copy_metadata_replace(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())

        object_key = "source-object"
        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body='{"key": "value"}',
            ContentType="application/json",
            Metadata={"key": "value"},
            ContentLanguage="en-US",
        )
        snapshot.match("put_object", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head_object", head_object)

        object_key_copy = f"{object_key}-copy"
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key_copy,
            Metadata={"another-key": "value"},
            ContentType="image/jpg",
            MetadataDirective="REPLACE",
        )
        snapshot.match("copy_object", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key_copy)
        snapshot.match("head_object_copy", head_object)

    @markers.aws.validated
    def test_s3_copy_metadata_directive_copy(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())

        object_key = "source-object"
        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body="test",
            Metadata={"key": "value"},
            ContentLanguage="en-US",
        )
        snapshot.match("put-object", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object", head_object)

        object_key_copy = f"{object_key}-copy"
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key_copy,
            Metadata={"another-key": "value"},  # this will be ignored
            ContentLanguage="en-GB",
            ContentType="image/jpg",
            MetadataDirective="COPY",
        )
        snapshot.match("copy-object", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key_copy)
        snapshot.match("head-object-copy", head_object)

    @markers.aws.validated
    @pytest.mark.parametrize("tagging_directive", ["COPY", "REPLACE", None])
    def test_s3_copy_tagging_directive(self, s3_bucket, snapshot, aws_client, tagging_directive):
        snapshot.add_transformer(snapshot.transform.s3_api())

        object_key = "source-object"
        resp = aws_client.s3.put_object(
            Bucket=s3_bucket, Key=object_key, Body="test", Tagging="key1=value1"
        )
        snapshot.match("put-object", resp)

        get_object_tags = aws_client.s3.get_object_tagging(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-object-tag", get_object_tags)

        kwargs = {"TaggingDirective": tagging_directive} if tagging_directive else {}

        object_key_copy = f"{object_key}-copy"
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key_copy,
            Tagging="key2=value2",
            **kwargs,
        )
        snapshot.match("copy-object", resp)

        get_object_tags = aws_client.s3.get_object_tagging(Bucket=s3_bucket, Key=object_key_copy)
        snapshot.match("get-copy-object-tag", get_object_tags)

        object_key_copy_tag_empty = f"{object_key}-copy-tag-empty"
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key_copy_tag_empty,
            **kwargs,
        )
        snapshot.match("copy-object-tag-empty", resp)

        get_object_tags = aws_client.s3.get_object_tagging(
            Bucket=s3_bucket, Key=object_key_copy_tag_empty
        )
        snapshot.match("get-copy-object-tag-empty", get_object_tags)

    @markers.aws.validated
    @pytest.mark.parametrize("tagging_directive", ["COPY", "REPLACE", None])
    def test_s3_copy_tagging_directive_versioned(
        self, s3_bucket, snapshot, aws_client, tagging_directive
    ):
        snapshot.add_transformer(snapshot.transform.s3_api())
        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        object_key = "source-object"
        resp = aws_client.s3.put_object(
            Bucket=s3_bucket, Key=object_key, Body="test", Tagging="key1=value1"
        )
        snapshot.match("put-object", resp)
        version_1 = resp["VersionId"]

        resp = aws_client.s3.put_object(
            Bucket=s3_bucket, Key=object_key, Body="test-v2", Tagging="key1=value1-v2"
        )
        snapshot.match("put-object-v2", resp)

        get_object_tags = aws_client.s3.get_object_tagging(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-object-tag", get_object_tags)

        get_object_tags_v1 = aws_client.s3.get_object_tagging(
            Bucket=s3_bucket, Key=object_key, VersionId=version_1
        )
        snapshot.match("get-object-tag-v1", get_object_tags_v1)

        kwargs = {"TaggingDirective": tagging_directive} if tagging_directive else {}

        object_key_copy = f"{object_key}-copy"
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key_copy,
            Tagging="key2=value2",
            **kwargs,
        )
        snapshot.match("copy-object", resp)

        get_object_tags = aws_client.s3.get_object_tagging(Bucket=s3_bucket, Key=object_key_copy)
        snapshot.match("get-copy-object-tag", get_object_tags)

        object_key_copy_tag_empty = f"{object_key}-copy-tag-empty"
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key_copy_tag_empty,
            **kwargs,
        )
        snapshot.match("copy-object-tag-empty", resp)

        get_object_tags = aws_client.s3.get_object_tagging(
            Bucket=s3_bucket, Key=object_key_copy_tag_empty
        )
        snapshot.match("get-copy-object-tag-empty", get_object_tags)

        object_key_copy_v1 = f"{object_key}-copy-v1"
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}?versionId={version_1}",
            Key=object_key_copy_v1,
            Tagging="key2=value2",
            **kwargs,
        )
        snapshot.match("copy-object-v1", resp)

        get_object_tags = aws_client.s3.get_object_tagging(Bucket=s3_bucket, Key=object_key_copy_v1)
        snapshot.match("get-copy-object-tag-v1", get_object_tags)

        object_key_copy_tag_empty_v1 = f"{object_key}-copy-tag-empty-v1"
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}?versionId={version_1}",
            Key=object_key_copy_tag_empty_v1,
            **kwargs,
        )
        snapshot.match("copy-object-tag-empty-v1", resp)

        get_object_tags = aws_client.s3.get_object_tagging(
            Bucket=s3_bucket, Key=object_key_copy_tag_empty_v1
        )
        snapshot.match("get-copy-object-tag-empty-v1", get_object_tags)

    @markers.aws.validated
    def test_s3_copy_content_type_and_metadata(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = "source-object"
        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body='{"key": "value"}',
            ContentType="application/json",
            Metadata={"key": "value"},
        )
        snapshot.match("put_object", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head_object", head_object)

        object_key_copy = f"{object_key}-copy"
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket, CopySource=f"{s3_bucket}/{object_key}", Key=object_key_copy
        )
        snapshot.match("copy_object", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key_copy)
        snapshot.match("head_object_copy", head_object)

        aws_client.s3.delete_objects(
            Bucket=s3_bucket, Delete={"Objects": [{"Key": object_key_copy}]}
        )

        # does not set MetadataDirective=REPLACE, so the original metadata should be kept
        object_key_copy = f"{object_key}-second-copy"
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key_copy,
            Metadata={"another-key": "value"},
            ContentType="application/javascript",
        )
        snapshot.match("copy_object_second", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key_copy)
        snapshot.match("head_object_second_copy", head_object)

    @markers.aws.validated
    def test_s3_copy_object_in_place(self, s3_bucket, allow_bucket_acl, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("DisplayName"),
                snapshot.transform.key_value("ID", value_replacement="owner-id"),
            ]
        )
        object_key = "source-object"

        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body='{"key": "value"}',
            ContentType="application/json",
            Metadata={"key": "value"},
        )
        snapshot.match("put_object", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head_object", head_object)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=object_key,
            ObjectAttributes=["StorageClass"],
        )
        snapshot.match("object-attrs", object_attrs)

        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket, CopySource=f"{s3_bucket}/{object_key}", Key=object_key
            )
        snapshot.match("copy-object-in-place-no-change", e.value.response)

        # it seems as long as you specify the field necessary, it does not check if the previous value was the same
        # and allows the copy

        # copy the object with the same StorageClass as the source object
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            ChecksumAlgorithm="SHA256",
            StorageClass=StorageClass.STANDARD,
        )
        snapshot.match("copy-object-in-place-with-storage-class", resp)
        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=object_key,
            ObjectAttributes=["StorageClass"],
        )
        snapshot.match("object-attrs-after-copy", object_attrs)

        # get source object ACl, private
        object_acl = aws_client.s3.get_object_acl(Bucket=s3_bucket, Key=object_key)
        snapshot.match("object-acl", object_acl)
        # copy the object with any ACL does not work, even if different from source object
        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket,
                CopySource=f"{s3_bucket}/{object_key}",
                Key=object_key,
                ACL="public-read",
            )
        snapshot.match("copy-object-in-place-with-acl", e.value.response)

    @markers.aws.validated
    def test_s3_copy_object_in_place_versioned(
        self, s3_bucket, allow_bucket_acl, snapshot, aws_client
    ):
        snapshot.add_transformer(snapshot.transform.s3_api())
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("DisplayName"),
                snapshot.transform.key_value("ID", value_replacement="owner-id"),
            ]
        )
        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        object_key = "source-object"

        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body='{"key": "value"}',
            ContentType="application/json",
            Metadata={"key": "value"},
        )
        snapshot.match("put_object", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head_object", head_object)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=object_key,
            ObjectAttributes=["StorageClass"],
        )
        snapshot.match("object-attrs", object_attrs)

        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket, CopySource=f"{s3_bucket}/{object_key}", Key=object_key
            )
        snapshot.match("copy-object-in-place-no-change", e.value.response)

        copy_obj = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            MetadataDirective="REPLACE",
        )
        snapshot.match("copy-in-place-versioned", copy_obj)
        object_version_id = copy_obj["VersionId"]

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object-copied", head_object)

        get_obj = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-object-copied", get_obj)

        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket, VersioningConfiguration={"Status": "Suspended"}
        )

        copy_obj = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            MetadataDirective="REPLACE",
        )
        snapshot.match("copy-in-place-versioned-suspended", copy_obj)
        assert copy_obj["VersionId"] == "null"

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object-copied-suspended", head_object)

        get_obj = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-object-copied-suspended", get_obj)

        head_object = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=object_key, VersionId=object_version_id
        )
        snapshot.match("head-object-previous-version-suspended", head_object)

        # re-enable the bucket versioning, to copy from `null` to new version
        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        copy_obj = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            MetadataDirective="REPLACE",
        )
        snapshot.match("copy-in-place-versioned-re-enabled", copy_obj)

    @markers.aws.validated
    def test_s3_copy_object_in_place_suspended_only(
        self, s3_bucket, allow_bucket_acl, snapshot, aws_client
    ):
        snapshot.add_transformer(snapshot.transform.s3_api())
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("DisplayName"),
                snapshot.transform.key_value("ID", value_replacement="owner-id"),
            ]
        )
        object_key = "source-object"

        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body='{"key": "value"}',
            ContentType="application/json",
            Metadata={"key": "value"},
        )
        snapshot.match("put_object", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head_object", head_object)

        copy_obj = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            MetadataDirective="REPLACE",
        )
        snapshot.match("copy-in-place-non-versioned", copy_obj)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object-copied", head_object)

        get_obj = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-object-copied", get_obj)

        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket, VersioningConfiguration={"Status": "Suspended"}
        )

        copy_obj = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            MetadataDirective="REPLACE",
        )
        snapshot.match("copy-in-place-versioned-suspended", copy_obj)
        assert copy_obj["VersionId"] == "null"

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object-copied-suspended", head_object)

        get_obj = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-object-copied-suspended", get_obj)

        # this is to verify the CopySourceVersionId field, if returned if both objects got `null`
        copy_obj_again = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            MetadataDirective="REPLACE",
        )
        snapshot.match("copy-in-place-versioned-suspended-twice", copy_obj_again)
        assert copy_obj_again["VersionId"] == "null"

    @markers.aws.validated
    def test_s3_copy_object_in_place_storage_class(self, s3_bucket, snapshot, aws_client):
        # this test will validate that setting StorageClass (even the same as source) allows a copy in place
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = "source-object"

        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body="test",
            StorageClass=StorageClass.STANDARD,
        )
        snapshot.match("put-object", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object", head_object)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=object_key,
            ObjectAttributes=["StorageClass"],
        )
        snapshot.match("object-attrs", object_attrs)

        # copy the object with the same StorageClass as the source object
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            StorageClass=StorageClass.STANDARD,
        )
        snapshot.match("copy-object-in-place-with-storage-class", resp)
        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=object_key,
            ObjectAttributes=["StorageClass"],
        )
        snapshot.match("object-attrs-after-copy", object_attrs)

    @pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="KMS not enabled in S3 image")
    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        paths=[
            "$..ServerSideEncryption",
            "$..SSEKMSKeyId",
            # TODO: fix this in moto, when not providing a KMS key, it should use AWS managed one
            "$..ETag",  # Etag are different because of encryption
        ]
    )
    def test_s3_copy_object_in_place_with_encryption(
        self, s3_bucket, kms_create_key, snapshot, aws_client
    ):
        # this test will validate encryption parameters that allows a copy in place
        snapshot.add_transformer(snapshot.transform.s3_api())
        snapshot.add_transformer(snapshot.transform.key_value("Description"))
        snapshot.add_transformer(snapshot.transform.key_value("SSEKMSKeyId"))
        object_key = "source-object"
        kms_key_id = kms_create_key()["KeyId"]

        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body="test",
            ServerSideEncryption="aws:kms",
            BucketKeyEnabled=True,
            SSEKMSKeyId=kms_key_id,
        )
        snapshot.match("put-object-with-kms-encryption", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object", head_object)

        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            ServerSideEncryption="aws:kms",
            # this will use AWS managed key, and not copy the original object key
        )
        snapshot.match("copy-object-in-place-with-sse", resp)
        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-copy-with-sse", head_object)

        # this is an edge case, if the source object SSE was not AES256, AWS allows you to not specify any fields
        # as it will use AES256 by default and is different from the source key
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
        )
        snapshot.match("copy-object-in-place-without-kms-sse", resp)
        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-copy-without-kms-sse", head_object)

        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            ServerSideEncryption="AES256",
        )
        snapshot.match("copy-object-in-place-with-aes", resp)
        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-copy-with-aes", head_object)

    @markers.aws.validated
    def test_copy_in_place_with_bucket_encryption(self, aws_client, s3_bucket, snapshot):
        response = aws_client.s3.put_bucket_encryption(
            Bucket=s3_bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                        "BucketKeyEnabled": False,
                    },
                ]
            },
        )
        snapshot.match("put-bucket-encryption", response)

        key_name = "test-enc"
        response = aws_client.s3.put_object(
            Body=b"",
            Bucket=s3_bucket,
            Key=key_name,
        )
        snapshot.match("put-obj", response)

        response = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource={"Bucket": s3_bucket, "Key": key_name},
            Key=key_name,
        )
        snapshot.match("copy-obj", response)

    @markers.aws.validated
    def test_s3_copy_object_in_place_metadata_directive(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = "source-object"
        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body='{"key": "value"}',
            ContentType="application/json",
            Metadata={"key": "value"},
        )
        snapshot.match("put_object", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head_object", head_object)

        with pytest.raises(ClientError) as e:
            # copy the object with the same Metadata as the source object, it will fail
            aws_client.s3.copy_object(
                Bucket=s3_bucket,
                CopySource=f"{s3_bucket}/{object_key}",
                Key=object_key,
                Metadata={"key": "value"},
            )
        snapshot.match("no-metadata-directive-fail", e.value.response)

        # copy the object in place, it needs MetadataDirective="REPLACE"
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            Metadata={"key2": "value2"},
            MetadataDirective="REPLACE",
        )
        snapshot.match("copy-replace-directive", resp)
        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-replace-directive", head_object)

        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            MetadataDirective="COPY",  # this is the default value
            StorageClass=StorageClass.STANDARD,
            # we need to add storage class to make the copy request legal
        )
        snapshot.match("copy-copy-directive", resp)
        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-copy-directive", head_object)

        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            MetadataDirective="COPY",
            Metadata={"key3": "value3"},  # assert that this is ignored
            StorageClass=StorageClass.STANDARD,
            # we need to add storage class to make the copy request legal
        )
        snapshot.match("copy-copy-directive-ignore", resp)
        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-copy-directive-ignore", head_object)

        # copy the object with no Metadata as the source object but with REPLACE
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            MetadataDirective="REPLACE",
        )
        snapshot.match("copy-replace-directive-empty", resp)
        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-replace-directive-empty", head_object)

    @markers.aws.validated
    def test_s3_copy_object_in_place_website_redirect_location(
        self, s3_bucket, snapshot, aws_client
    ):
        # this test will validate that setting WebsiteRedirectLocation (even the same as source) allows a copy in place
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = "source-object"

        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body="test",
            WebsiteRedirectLocation="/test/direct",
        )
        snapshot.match("put-object", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object", head_object)

        # copy the object with the same WebsiteRedirectLocation as the source object
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            WebsiteRedirectLocation="/test/direct",
        )
        snapshot.match("copy-object-in-place-with-website-redirection", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object-after-copy", head_object)

    @markers.aws.validated
    def test_s3_copy_object_storage_class(self, s3_bucket, snapshot, aws_client):
        # this test will validate that setting StorageClass (even the same as source) allows a copy in place
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = "source-object"
        dest_key = "dest-object"

        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body="test",
            StorageClass=StorageClass.STANDARD_IA,
        )
        snapshot.match("put-object", resp)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object", head_object)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=object_key,
            ObjectAttributes=["StorageClass"],
        )
        snapshot.match("object-attrs", object_attrs)

        # copy the object to see if it keeps the StorageClass from the source object
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=dest_key,
        )
        snapshot.match("copy-object-in-place-with-storage-class", resp)
        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=dest_key,
            ObjectAttributes=["StorageClass"],
        )
        # the destination key does not keep the source key storage class
        snapshot.match("object-attrs-after-copy", object_attrs)

        # try copying in place, as the StorageClass by default would be STANDARD and different from source
        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket,
                CopySource=f"{s3_bucket}/{object_key}",
                Key=object_key,
            )
        snapshot.match("exc-invalid-request-storage-class", e.value.response)

    @markers.aws.validated
    @pytest.mark.parametrize("algorithm", ["CRC32", "CRC32C", "SHA1", "SHA256", "CRC64NVME"])
    def test_s3_copy_object_with_checksum(self, s3_bucket, snapshot, aws_client, algorithm):
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = "source-object"
        # create key with no checksum
        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body='{"key": "value"}',
            ContentType="application/json",
            Metadata={"key": "value"},
        )
        snapshot.match("put-object-no-checksum", resp)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=object_key,
            ObjectAttributes=["Checksum"],
        )
        snapshot.match("object-attrs", object_attrs)

        # copy the object in place with some metadata and replacing it, but with a checksum
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            ChecksumAlgorithm=algorithm,
            Metadata={"key1": "value1"},
            MetadataDirective="REPLACE",
        )
        snapshot.match("copy-object-in-place-with-checksum", resp)
        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=object_key,
            ObjectAttributes=["Checksum"],
        )
        snapshot.match("object-attrs-after-copy", object_attrs)

        dest_key = "dest-object"
        # copy the object to check if the new object has the checksum too
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=dest_key,
        )
        snapshot.match("copy-object-to-dest-keep-checksum", resp)

    @markers.aws.validated
    @pytest.mark.parametrize("algorithm", ["CRC32", "CRC32C", "SHA1", "SHA256", "CRC64NVME"])
    def test_s3_copy_object_with_default_checksum(self, s3_bucket, snapshot, aws_client, algorithm):
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = "source-object"
        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body='{"key": "value"}',
            ContentType="application/json",
            ChecksumAlgorithm=algorithm,
            Metadata={"key": "value"},
        )
        snapshot.match("put-object-no-checksum", resp)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=object_key,
            ObjectAttributes=["Checksum"],
        )
        snapshot.match("object-attrs", object_attrs)

        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=object_key,
            Metadata={"key1": "value1"},
            MetadataDirective="REPLACE",
        )
        snapshot.match("copy-object-in-place-with-no-checksum", resp)
        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=object_key,
            ObjectAttributes=["Checksum"],
        )
        snapshot.match("object-attrs-after-copy", object_attrs)

        dest_key = "dest-object"
        # copy the object to check if the new object has the checksum too
        resp = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=dest_key,
        )
        snapshot.match("copy-object-to-dest-keep-checksum", resp)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=dest_key,
            ObjectAttributes=["Checksum"],
        )
        snapshot.match("dest-object-attrs-after-copy", object_attrs)

    @markers.aws.validated
    def test_s3_copy_object_preconditions(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = "source-object"
        dest_key = "dest-object"
        # create key with no checksum
        put_object = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body=b"data",
        )
        head_obj = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object", head_obj)

        # wait a bit for the `unmodified_since` value so that it's unvalid.
        # S3 compares it the last-modified field, but you can't set the value in the future otherwise it ignores it
        # It needs to be now or less, but the object needs to be a bit more recent than that.
        time.sleep(3)

        # we're testing the order of validation at the same time by validating all of them at once, by elimination
        now = datetime.datetime.now().astimezone(tz=ZoneInfo("GMT"))
        wrong_unmodified_since = now - datetime.timedelta(days=1)

        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket,
                CopySource=f"{s3_bucket}/{object_key}",
                Key=dest_key,
                CopySourceIfModifiedSince=now,
                CopySourceIfUnmodifiedSince=wrong_unmodified_since,
                CopySourceIfMatch="etag123",
                CopySourceIfNoneMatch=put_object["ETag"],
            )
        snapshot.match("copy-precondition-if-match", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket,
                CopySource=f"{s3_bucket}/{object_key}",
                Key=dest_key,
                CopySourceIfModifiedSince=now,
                CopySourceIfUnmodifiedSince=wrong_unmodified_since,
                CopySourceIfNoneMatch=put_object["ETag"],
            )
        snapshot.match("copy-precondition-if-unmodified-since", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket,
                CopySource=f"{s3_bucket}/{object_key}",
                Key=dest_key,
                CopySourceIfModifiedSince=now,
                CopySourceIfNoneMatch=put_object["ETag"],
            )
        snapshot.match("copy-precondition-if-none-match", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket,
                CopySource=f"{s3_bucket}/{object_key}",
                Key=dest_key,
                CopySourceIfModifiedSince=now,
            )
        snapshot.match("copy-precondition-if-modified-since", e.value.response)

        # AWS will ignore the value if it's in the future
        copy_obj = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=dest_key,
            CopySourceIfModifiedSince=now + datetime.timedelta(days=1),
        )
        snapshot.match("copy-ignore-future-modified-since", copy_obj)

        # AWS will ignore the missing quotes around the ETag and still reject the request
        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket,
                CopySource=f"{s3_bucket}/{object_key}",
                Key=dest_key,
                CopySourceIfNoneMatch=put_object["ETag"].strip('"'),
            )
        snapshot.match("copy-etag-missing-quotes", e.value.response)

        # Positive tests with all conditions checked
        copy_obj_all_positive = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=f"{s3_bucket}/{object_key}",
            Key=dest_key,
            CopySourceIfMatch=put_object["ETag"].strip('"'),
            CopySourceIfNoneMatch="etag123",
            CopySourceIfModifiedSince=now - datetime.timedelta(days=1),
            CopySourceIfUnmodifiedSince=now,
        )
        snapshot.match("copy-success", copy_obj_all_positive)

    @markers.aws.validated
    def test_s3_copy_object_wrong_format(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket,
                CopySource="wrongformat",
                Key="destination-key",
            )
        snapshot.match("copy-object-wrong-copy-source", e.value.response)

    @markers.aws.validated
    @pytest.mark.parametrize("method", ("get_object", "head_object"))
    def test_s3_get_object_preconditions(self, s3_bucket, snapshot, aws_client, method):
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = "test-object"
        put_object = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body=b"data",
        )
        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)

        client_method = getattr(aws_client.s3, method)

        # wait a bit for the `unmodified_since` value so that it's invalid.
        # S3 compares it the last-modified field, but you can't set the value in the future otherwise it ignores it.
        # It needs to be now or less, but the object needs to be a bit more recent than that.
        time.sleep(3)

        # we're testing the order of validation at the same time by validating all of them at once, by elimination
        now = datetime.datetime.now().astimezone(tz=ZoneInfo("GMT"))
        wrong_unmodified_since = now - datetime.timedelta(days=1)

        with pytest.raises(ClientError) as e:
            client_method(
                Bucket=s3_bucket,
                Key=object_key,
                IfModifiedSince=now,
                IfUnmodifiedSince=wrong_unmodified_since,
                IfMatch="etag123",
                IfNoneMatch=put_object["ETag"],
            )
        snapshot.match("precondition-if-match", e.value.response)

        with pytest.raises(ClientError) as e:
            client_method(
                Bucket=s3_bucket,
                Key=object_key,
                IfModifiedSince=now,
                IfUnmodifiedSince=wrong_unmodified_since,
                IfNoneMatch=put_object["ETag"],
            )
        snapshot.match("precondition-if-unmodified-since", e.value.response)

        with pytest.raises(ClientError) as e:
            client_method(
                Bucket=s3_bucket,
                Key=object_key,
                IfModifiedSince=now,
                IfNoneMatch=put_object["ETag"],
            )
        snapshot.match("precondition-if-none-match", e.value.response)

        with pytest.raises(ClientError) as e:
            client_method(
                Bucket=s3_bucket,
                Key=object_key,
                IfModifiedSince=now,
            )
        snapshot.match("copy-precondition-if-modified-since", e.value.response)

        # AWS will ignore the value if it's in the future
        get_obj = client_method(
            Bucket=s3_bucket,
            Key=object_key,
            IfModifiedSince=now + datetime.timedelta(days=1),
        )
        snapshot.match("obj-ignore-future-modified-since", get_obj)
        # # AWS will ignore the missing quotes around the ETag and still reject the request
        with pytest.raises(ClientError) as e:
            client_method(
                Bucket=s3_bucket,
                Key=object_key,
                IfModifiedSince=now,
                IfNoneMatch=put_object["ETag"].strip('"'),
            )
        snapshot.match("etag-missing-quotes", e.value.response)

        # test If*ModifiedSince precision
        response = client_method(
            Bucket=s3_bucket,
            Key=object_key,
            IfUnmodifiedSince=head_object["LastModified"],
        )
        snapshot.match("precondition-if-unmodified-since-is-object", response)

        with pytest.raises(ClientError) as e:
            client_method(
                Bucket=s3_bucket,
                Key=object_key,
                IfModifiedSince=head_object["LastModified"],
            )
        snapshot.match("precondition-if-modified-since-is-object", e.value.response)

        # Positive tests with all conditions checked
        get_obj_all_positive = client_method(
            Bucket=s3_bucket,
            Key=object_key,
            IfMatch=put_object["ETag"].strip('"'),
            IfNoneMatch="etag123",
            IfModifiedSince=now - datetime.timedelta(days=1),
            IfUnmodifiedSince=now,
        )
        snapshot.match("obj-success", get_obj_all_positive)

    @markers.aws.validated
    def test_s3_multipart_upload_acls(
        self, s3_bucket, allow_bucket_acl, s3_multipart_upload, snapshot, aws_client
    ):
        # https://docs.aws.amazon.com/AmazonS3/latest/userguide/managing-acls.html
        # > Bucket and object permissions are independent of each other. An object does not inherit the permissions
        # > from its bucket. For example, if you create a bucket and grant write access to a user, you can't access
        # > that user’s objects unless the user explicitly grants you access.
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("DisplayName"),
                snapshot.transform.key_value("ID", value_replacement="owner-id"),
            ]
        )

        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")
        response = aws_client.s3.get_bucket_acl(Bucket=s3_bucket)
        snapshot.match("bucket-acl", response)

        def check_permissions(key):
            acl_response = aws_client.s3.get_object_acl(Bucket=s3_bucket, Key=key)
            snapshot.match(f"permission-{key}", acl_response)

        # perform uploads (multipart and regular) and check ACLs
        aws_client.s3.put_object(Bucket=s3_bucket, Key="acl-key0", Body="something")
        check_permissions("acl-key0")
        s3_multipart_upload(bucket=s3_bucket, key="acl-key1")
        check_permissions("acl-key1")
        s3_multipart_upload(bucket=s3_bucket, key="acl-key2", acl="public-read-write")
        check_permissions("acl-key2")

    @markers.aws.validated
    def test_s3_bucket_acl(self, s3_bucket, allow_bucket_acl, snapshot, aws_client):
        # loosely based on
        # https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketAcl.html
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("DisplayName"),
                snapshot.transform.key_value("ID", value_replacement="owner-id"),
            ]
        )
        list_bucket_output = aws_client.s3.list_buckets()
        owner = list_bucket_output["Owner"]

        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")

        response = aws_client.s3.get_bucket_acl(Bucket=s3_bucket)
        snapshot.match("get-bucket-acl", response)

        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="private")

        response = aws_client.s3.get_bucket_acl(Bucket=s3_bucket)
        snapshot.match("get-bucket-canned-acl", response)

        aws_client.s3.put_bucket_acl(
            Bucket=s3_bucket, GrantRead='uri="http://acs.amazonaws.com/groups/s3/LogDelivery"'
        )

        response = aws_client.s3.get_bucket_acl(Bucket=s3_bucket)
        snapshot.match("get-bucket-grant-acl", response)

        # Owner is mandatory, otherwise raise MalformedXML
        acp = {
            "Owner": owner,
            "Grants": [
                {
                    "Grantee": {"ID": owner["ID"], "Type": "CanonicalUser"},
                    "Permission": "FULL_CONTROL",
                },
                {
                    "Grantee": {
                        "URI": "http://acs.amazonaws.com/groups/s3/LogDelivery",
                        "Type": "Group",
                    },
                    "Permission": "WRITE",
                },
            ],
        }
        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, AccessControlPolicy=acp)

        response = aws_client.s3.get_bucket_acl(Bucket=s3_bucket)
        snapshot.match("get-bucket-acp-acl", response)

    @markers.aws.validated
    def test_s3_bucket_acl_exceptions(self, s3_bucket, snapshot, aws_client):
        list_bucket_output = aws_client.s3.list_buckets()
        owner = list_bucket_output["Owner"]

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="fake-acl")

        snapshot.match("put-bucket-canned-acl", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(
                Bucket=s3_bucket, GrantWrite='uri="http://acs.amazonaws.com/groups/s3/FakeGroup"'
            )

        snapshot.match("put-bucket-grant-acl-fake-uri", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(Bucket=s3_bucket, GrantWrite='fakekey="1234"')

        snapshot.match("put-bucket-grant-acl-fake-key", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(Bucket=s3_bucket, GrantWrite='id="wrong-id"')

        snapshot.match("put-bucket-grant-acl-wrong-id", e.value.response)

        acp = {
            "Grants": [
                {
                    "Grantee": {
                        "URI": "http://acs.amazonaws.com/groups/s3/LogDelivery",
                        "Type": "Group",
                    },
                    "Permission": "WRITE",
                }
            ]
        }
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(Bucket=s3_bucket, AccessControlPolicy=acp)
        snapshot.match("put-bucket-acp-acl-1", e.value.response)

        # add Owner, but modify the permission
        acp["Owner"] = owner
        acp["Grants"][0]["Permission"] = "WRONG-PERMISSION"

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(Bucket=s3_bucket, AccessControlPolicy=acp)
        snapshot.match("put-bucket-acp-acl-2", e.value.response)

        # restore good permission, but put bad format Owner ID
        acp["Owner"] = {"ID": "wrong-id"}
        acp["Grants"][0]["Permission"] = "FULL_CONTROL"

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(Bucket=s3_bucket, AccessControlPolicy=acp)
        snapshot.match("put-bucket-acp-acl-3", e.value.response)

        # restore owner, but wrong URI
        acp["Owner"] = owner
        acp["Grants"][0]["Grantee"]["URI"] = "http://acs.amazonaws.com/groups/s3/FakeGroup"

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(Bucket=s3_bucket, AccessControlPolicy=acp)
        snapshot.match("put-bucket-acp-acl-4", e.value.response)

        # different type of failing grantee (CanonicalUser/ID)
        acp["Grants"][0]["Grantee"]["Type"] = "CanonicalUser"
        acp["Grants"][0]["Grantee"]["ID"] = "wrong-id"
        acp["Grants"][0]["Grantee"].pop("URI")

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(Bucket=s3_bucket, AccessControlPolicy=acp)
        snapshot.match("put-bucket-acp-acl-5", e.value.response)

        # different type of failing grantee (Wrong type)
        acp["Grants"][0]["Grantee"]["Type"] = "BadType"

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(Bucket=s3_bucket, AccessControlPolicy=acp)
        snapshot.match("put-bucket-acp-acl-6", e.value.response)

        # test setting empty ACP
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(Bucket=s3_bucket, AccessControlPolicy={})

        snapshot.match("put-bucket-empty-acp", e.value.response)

        # test setting nothing
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(Bucket=s3_bucket)

        snapshot.match("put-bucket-empty", e.value.response)

        # test setting two different kind of valid ACL
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(
                Bucket=s3_bucket,
                ACL="private",
                GrantRead='uri="http://acs.amazonaws.com/groups/s3/LogDelivery"',
            )

        snapshot.match("put-bucket-two-type-acl", e.value.response)

        # test setting again two different kind of valid ACL
        acp = {
            "Owner": owner,
            "Grants": [
                {
                    "Grantee": {"ID": owner["ID"], "Type": "CanonicalUser"},
                    "Permission": "FULL_CONTROL",
                },
            ],
        }
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_acl(
                Bucket=s3_bucket,
                ACL="private",
                AccessControlPolicy=acp,
            )

        snapshot.match("put-bucket-two-type-acl-acp", e.value.response)

    @markers.aws.validated
    def test_s3_object_acl(self, s3_bucket, allow_bucket_acl, snapshot, aws_client):
        # loosely based on
        # https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketAcl.html
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("DisplayName"),
                snapshot.transform.key_value("ID", value_replacement="owner-id"),
            ]
        )
        list_bucket_output = aws_client.s3.list_buckets()
        owner = list_bucket_output["Owner"]
        object_key = "object-key-acl"
        put_object = aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("put-object-default-acl", put_object)

        response = aws_client.s3.get_object_acl(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-object-acl-default", response)

        put_object_acl = aws_client.s3.put_object_acl(
            Bucket=s3_bucket, Key=object_key, ACL="public-read"
        )
        snapshot.match("put-object-acl", put_object_acl)

        response = aws_client.s3.get_object_acl(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-object-acl", response)

        # this a bucket URI?
        aws_client.s3.put_object_acl(
            Bucket=s3_bucket,
            Key=object_key,
            GrantRead='uri="http://acs.amazonaws.com/groups/s3/LogDelivery"',
        )

        response = aws_client.s3.get_object_acl(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-object-grant-acl", response)

        # Owner is mandatory, otherwise raise MalformedXML
        acp = {
            "Owner": owner,
            "Grants": [
                {
                    "Grantee": {"ID": owner["ID"], "Type": "CanonicalUser"},
                    "Permission": "FULL_CONTROL",
                },
                {
                    "Grantee": {
                        "URI": "http://acs.amazonaws.com/groups/s3/LogDelivery",
                        "Type": "Group",
                    },
                    "Permission": "WRITE",
                },
            ],
        }
        aws_client.s3.put_object_acl(Bucket=s3_bucket, Key=object_key, AccessControlPolicy=acp)

        response = aws_client.s3.get_object_acl(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-object-acp-acl", response)

    @markers.aws.validated
    def test_s3_object_acl_exceptions(self, s3_bucket, snapshot, aws_client):
        list_bucket_output = aws_client.s3.list_buckets()
        owner = list_bucket_output["Owner"]
        object_key = "object-key-acl"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, ACL="fake-acl")
        snapshot.match("put-object-canned-acl", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(Bucket=s3_bucket, Key=object_key, ACL="fake-acl")
        snapshot.match("put-object-acl-canned-acl", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(
                Bucket=s3_bucket,
                Key=object_key,
                GrantWrite='uri="http://acs.amazonaws.com/groups/s3/FakeGroup"',
            )
        snapshot.match("put-object-grant-acl-fake-uri", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(
                Bucket=s3_bucket, Key=object_key, GrantWrite='fakekey="1234"'
            )
        snapshot.match("put-object-grant-acl-fake-key", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(
                Bucket=s3_bucket, Key=object_key, GrantWrite='id="wrong-id"'
            )

        snapshot.match("put-object-grant-acl-wrong-id", e.value.response)

        acp = {
            "Grants": [
                {
                    "Grantee": {
                        "URI": "http://acs.amazonaws.com/groups/s3/LogDelivery",
                        "Type": "Group",
                    },
                    "Permission": "WRITE",
                }
            ]
        }
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(Bucket=s3_bucket, Key=object_key, AccessControlPolicy=acp)
        snapshot.match("put-object-acp-acl-1", e.value.response)

        # add Owner, but modify the permission
        acp["Owner"] = owner
        acp["Grants"][0]["Permission"] = "WRONG-PERMISSION"

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(Bucket=s3_bucket, Key=object_key, AccessControlPolicy=acp)
        snapshot.match("put-object-acp-acl-2", e.value.response)

        # restore good permission, but put bad format Owner ID
        acp["Owner"] = {"ID": "wrong-id"}
        acp["Grants"][0]["Permission"] = "FULL_CONTROL"

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(Bucket=s3_bucket, Key=object_key, AccessControlPolicy=acp)
        snapshot.match("put-object-acp-acl-3", e.value.response)

        # restore owner, but wrong URI
        acp["Owner"] = owner
        acp["Grants"][0]["Grantee"]["URI"] = "http://acs.amazonaws.com/groups/s3/FakeGroup"

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(Bucket=s3_bucket, Key=object_key, AccessControlPolicy=acp)
        snapshot.match("put-object-acp-acl-4", e.value.response)

        # different type of failing grantee (CanonicalUser/ID)
        acp["Grants"][0]["Grantee"]["Type"] = "CanonicalUser"
        acp["Grants"][0]["Grantee"]["ID"] = "wrong-id"
        acp["Grants"][0]["Grantee"].pop("URI")

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(Bucket=s3_bucket, Key=object_key, AccessControlPolicy=acp)
        snapshot.match("put-object-acp-acl-5", e.value.response)

        # different type of failing grantee (Wrong type)
        acp["Grants"][0]["Grantee"]["Type"] = "BadType"

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(Bucket=s3_bucket, Key=object_key, AccessControlPolicy=acp)
        snapshot.match("put-object-acp-acl-6", e.value.response)

        # test setting empty ACP
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(Bucket=s3_bucket, Key=object_key, AccessControlPolicy={})

        snapshot.match("put-object-empty-acp", e.value.response)

        # test setting nothing
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(Bucket=s3_bucket, Key=object_key)

        snapshot.match("put-object-acl-empty", e.value.response)

        # test setting two different kind of valid ACL
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(
                Bucket=s3_bucket,
                Key=object_key,
                ACL="private",
                GrantRead='uri="http://acs.amazonaws.com/groups/s3/LogDelivery"',
            )

        snapshot.match("put-object-two-type-acl", e.value.response)

        # test setting again two different kind of valid ACL
        acp = {
            "Owner": owner,
            "Grants": [
                {
                    "Grantee": {"ID": owner["ID"], "Type": "CanonicalUser"},
                    "Permission": "FULL_CONTROL",
                },
            ],
        }
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(
                Bucket=s3_bucket,
                Key=object_key,
                ACL="private",
                AccessControlPolicy=acp,
            )

        snapshot.match("put-object-two-type-acl-acp", e.value.response)

    @markers.aws.validated
    def test_s3_object_expires(self, s3_bucket, snapshot, aws_client):
        """
        `Expires` header indicates the date and time at which the object is no longer cacheable, and is not linked to
        Object Expiration.
        https://www.rfc-editor.org/rfc/rfc7234#section-5.3
        """
        snapshot.add_transformer(snapshot.transform.s3_api())
        snapshot.add_transformer(
            snapshot.transform.key_value(
                "ExpiresString", reference_replacement=False, value_replacement="<expires>"
            )
        )

        now = datetime.datetime.now(tz=datetime.UTC)
        expires_in_future = now + datetime.timedelta(days=1)
        object_key_expires_future = "key-object-future"
        object_key_expires_past = "key-object-past"

        put_obj_future = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key_expires_future,
            Body="foo",
            Expires=expires_in_future,
        )
        snapshot.match("put-object-expires-future", put_obj_future)

        response = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key_expires_future)
        assert response["Expires"] > now
        assert re.match(
            r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT$",
            response["ExpiresString"],
        )
        snapshot.match("head-object-expires-future", response)

        get_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key_expires_future)
        assert response["Expires"] > now
        snapshot.match("get-object-expires-future", get_object)

        put_obj_past = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key_expires_past,
            Body="foo",
            Expires=now - datetime.timedelta(days=1),
        )
        snapshot.match("put-object-expires-past", put_obj_past)

        response = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key_expires_past)
        assert response["Expires"] < now
        snapshot.match("head-object-expires-past", response)

        get_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key_expires_past)
        assert response["Expires"] < now
        snapshot.match("get-object-expires-past", get_object)

    @markers.aws.validated
    def test_upload_file_with_xml_preamble(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = f"key-{short_uid()}"
        body = '<?xml version="1.0" encoding="UTF-8"?><test/>'

        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body=body)

        response = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get_object", response)

    @markers.aws.validated
    def test_bucket_availability(self, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))
        # make sure to have a non created bucket, got some AccessDenied against AWS
        bucket_name = f"test-bucket-lifecycle-{long_uid()}"
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_lifecycle(Bucket=bucket_name)
        snapshot.match("bucket-lifecycle", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_replication(Bucket=bucket_name)
        snapshot.match("bucket-replication", e.value.response)

    @markers.aws.validated
    def test_different_location_constraint(
        self,
        s3_create_bucket,
        aws_client_factory,
        s3_create_bucket_with_client,
        snapshot,
        aws_client,
    ):
        region_us_east_2 = "us-east-2"
        region_us_west_1 = "us-west-1"

        snapshot.add_transformer(snapshot.transform.s3_api())
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("Location", "<location>", reference_replacement=False),
                snapshot.transform.key_value(
                    "LocationConstraint", "<location-constraint>", reference_replacement=False
                ),
                snapshot.transform.regex(AWS_REGION_US_EAST_1, "<region-1>"),
                snapshot.transform.regex(region_us_east_2, "<region-2>"),
                snapshot.transform.regex(region_us_west_1, "<region-3>"),
            ]
        )
        bucket_us_east_1 = f"bucket-{short_uid()}"
        client_us_east_1 = aws_client_factory(
            region_name=AWS_REGION_US_EAST_1, config=Config(parameter_validation=False)
        ).s3
        s3_create_bucket_with_client(
            client_us_east_1,
            Bucket=bucket_us_east_1,
        )
        response = client_us_east_1.get_bucket_location(Bucket=bucket_us_east_1)
        snapshot.match("get-bucket-location-bucket-us-east-1", response)

        # assert creation fails with location constraint for us-east-1 region
        with pytest.raises(ClientError) as exc:
            client_us_east_1.create_bucket(
                Bucket=f"bucket-{short_uid()}",
                CreateBucketConfiguration={"LocationConstraint": AWS_REGION_US_EAST_1},
            )
        snapshot.match("create-bucket-constraint-us-east-1", exc.value.response)

        # assert creation fails with location constraint with the region unset
        with pytest.raises(ClientError) as exc:
            client_us_east_1.create_bucket(
                Bucket=f"bucket-{short_uid()}",
                CreateBucketConfiguration={"LocationConstraint": None},
            )
        snapshot.match("create-bucket-constraint-us-east-1-with-None", exc.value.response)

        client_us_east_2 = aws_client_factory(region_name=region_us_east_2).s3
        bucket_us_east_2 = f"bucket-{short_uid()}"
        s3_create_bucket_with_client(
            client_us_east_2,
            Bucket=bucket_us_east_2,
            CreateBucketConfiguration={"LocationConstraint": region_us_east_2},
        )
        response = client_us_east_2.get_bucket_location(Bucket=bucket_us_east_2)
        snapshot.match("get-bucket-location-bucket-us-east-2", response)

        # assert creation fails without location constraint for us-east-2 region
        with pytest.raises(ClientError) as exc:
            client_us_east_2.create_bucket(Bucket=f"bucket-{short_uid()}")
        snapshot.match("create-bucket-us-east-2-no-constraint-exc", exc.value.response)

        # assert creation fails with wrong location constraint from us-east-2 region to us-west-1 region
        with pytest.raises(ClientError) as exc:
            client_us_east_2.create_bucket(
                Bucket=f"bucket-{short_uid()}",
                CreateBucketConfiguration={"LocationConstraint": region_us_west_1},
            )
        snapshot.match("create-bucket-us-east-2-constraint-to-us-west-1", exc.value.response)

        client_us_west_1 = aws_client_factory(region_name=region_us_west_1).s3

        with pytest.raises(ClientError) as exc:
            client_us_west_1.create_bucket(
                Bucket=f"bucket-{short_uid()}",
                CreateBucketConfiguration={"LocationConstraint": region_us_east_2},
            )
        snapshot.match("create-bucket-us-west-1-constraint-to-us-east-2", exc.value.response)

        with pytest.raises(ClientError) as exc:
            client_us_west_1.create_bucket(
                Bucket=f"bucket-{short_uid()}",
                CreateBucketConfiguration={"LocationConstraint": AWS_REGION_US_EAST_1},
            )
        snapshot.match("create-bucket-us-west-1-constraint-to-us-east-1", exc.value.response)

        with pytest.raises(ClientError) as exc:
            aws_client.s3.get_bucket_location(Bucket=f"random-bucket-test-{short_uid()}")

        snapshot.match("get-bucket-location-non-existent-bucket", exc.value.response)

    @markers.aws.validated
    def test_bucket_operation_between_regions(
        self,
        aws_client_factory,
        s3_create_bucket_with_client,
        snapshot,
    ):
        snapshot.add_transformer(snapshot.transform.s3_api())

        region_us_west_2 = "us-west-2"
        client_us_west_2 = aws_client_factory(region_name=region_us_west_2).s3
        bucket_name = f"bucket-{short_uid()}"
        s3_create_bucket_with_client(
            client_us_west_2,
            Bucket=bucket_name,
            CreateBucketConfiguration={"LocationConstraint": region_us_west_2},
        )

        put_website_config = client_us_west_2.put_bucket_website(
            Bucket=bucket_name,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
            },
        )
        snapshot.match("put-website-config-region-1", put_website_config)

        bucket_cors_config = {
            "CORSRules": [
                {
                    "AllowedOrigins": ["*"],
                    "AllowedMethods": ["GET"],
                }
            ]
        }
        put_cors_config = client_us_west_2.put_bucket_cors(
            Bucket=bucket_name, CORSConfiguration=bucket_cors_config
        )
        snapshot.match("put-cors-config-region-1", put_cors_config)

        client_us_east_1 = aws_client_factory(region_name=AWS_REGION_US_EAST_1).s3

        get_website_config = client_us_east_1.get_bucket_website(Bucket=bucket_name)
        snapshot.match("get-website-config-region-2", get_website_config)

        get_cors_config = client_us_east_1.get_bucket_cors(Bucket=bucket_name)
        snapshot.match("get-cors-config-region-2", get_cors_config)

    @markers.aws.validated
    def test_get_object_with_anon_credentials(
        self, s3_bucket, allow_bucket_acl, snapshot, aws_client, anonymous_client
    ):
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = f"key-{short_uid()}"
        body = "body data"

        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body=body,
        )
        aws_client.s3.put_object_acl(Bucket=s3_bucket, Key=object_key, ACL="public-read")
        s3_anon_client = anonymous_client("s3")

        response = s3_anon_client.get_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get_object", response)

    @markers.aws.validated
    def test_putobject_with_multiple_keys(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        key_by_path = "aws/key1/key2/key3"

        aws_client.s3.put_object(Body=b"test", Bucket=s3_bucket, Key=key_by_path)
        result = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_by_path)
        snapshot.match("get_object", result)

    @markers.aws.validated
    def test_range_header_body_length(self, s3_bucket, snapshot, aws_client):
        # Test for https://github.com/localstack/localstack/issues/1952
        # object created is random, ETag will be as well
        snapshot.add_transformer(snapshot.transform.key_value("ETag"))
        object_key = "sample.bin"
        chunk_size = 1024

        with io.BytesIO() as data:
            data.write(os.urandom(chunk_size * 2))
            data.seek(0)
            aws_client.s3.upload_fileobj(data, s3_bucket, object_key)

        range_header = f"bytes=0-{(chunk_size - 1)}"
        resp = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key, Range=range_header)
        content = resp["Body"].read()
        assert chunk_size == len(content)
        snapshot.match("get-object", resp)

        range_header = f"bytes={chunk_size}-2048"
        resp = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key, Range=range_header)
        content = resp["Body"].read()
        assert chunk_size == len(content)
        snapshot.match("get-object-2", resp)

    @markers.aws.validated
    def test_download_fileobj_multiple_range_requests(self, s3_bucket, aws_client):
        object_key = "test-download_fileobj"

        body = os.urandom(70_000 * 100 * 5)
        aws_client.s3.upload_fileobj(BytesIO(body), s3_bucket, object_key)

        # get object and compare results
        downloaded_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        assert downloaded_object["Body"].read() == body

        # use download_fileobj to verify multithreaded range requests work
        test_fileobj = BytesIO()
        aws_client.s3.download_fileobj(Bucket=s3_bucket, Key=object_key, Fileobj=test_fileobj)
        assert body == test_fileobj.getvalue()

    @markers.aws.validated
    def test_get_range_object_headers(self, s3_bucket, aws_client):
        object_key = "sample.bin"
        chunk_size = 1024

        with io.BytesIO() as data:
            data.write(os.urandom(chunk_size * 2))
            data.seek(0)
            aws_client.s3.upload_fileobj(data, s3_bucket, object_key)

        range_header = f"bytes=0-{(chunk_size - 1)}"
        resp = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key, Range=range_header)
        assert resp.get("AcceptRanges") == "bytes"
        resp_headers = resp["ResponseMetadata"]["HTTPHeaders"]
        assert "x-amz-request-id" in resp_headers
        assert "x-amz-id-2" in resp_headers
        # `content-language` should not be in the response
        if is_aws_cloud():  # fixme parity issue
            assert "content-language" not in resp_headers
        # We used to return `cache-control: no-cache` if the header wasn't set
        # by the client, but this was a bug because s3 doesn't do that. It simply
        # omits it.
        assert "cache-control" not in resp_headers
        # Do not send a content-encoding header as discussed in Issue #3608
        assert "content-encoding" not in resp_headers

    @markers.aws.only_localstack
    def test_put_object_chunked_newlines(self, s3_bucket, aws_client, region_name):
        # Boto still does not support chunk encoding, which means we can't test with the client nor
        # aws_http_client_factory. See open issue: https://github.com/boto/boto3/issues/751
        # Test for https://github.com/localstack/localstack/issues/1571
        object_key = "data"
        body = "Hello\r\n\r\n\r\n\r\n"
        headers = {
            "Authorization": mock_aws_request_headers(
                "s3",
                aws_access_key_id=TEST_AWS_ACCESS_KEY_ID,
                region_name=region_name,
            )["Authorization"],
            "Content-Type": "audio/mpeg",
            "X-Amz-Content-Sha256": "STREAMING-AWS4-HMAC-SHA256-PAYLOAD",
            "X-Amz-Date": "20190918T051509Z",
            "X-Amz-Decoded-Content-Length": str(len(body)),
            "Content-Encoding": "aws-chunked",
        }
        data = (
            "d;chunk-signature=af5e6c0a698b0192e9aa5d9083553d4d241d81f69ec62b184d05c509ad5166af\r\n"
            f"{body}\r\n0;chunk-signature=f2a50a8c0ad4d212b579c2489c6d122db88d8a0d0b987ea1f3e9d081074a5937\r\n"
        )
        # put object
        url = f"{config.internal_service_url()}/{s3_bucket}/{object_key}"
        requests.put(url, data, headers=headers, verify=False)
        # get object and assert content length
        downloaded_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        download_file_object = to_str(downloaded_object["Body"].read())
        assert len(body) == len(str(download_file_object))
        assert body == str(download_file_object)

    @markers.aws.only_localstack
    def test_put_object_chunked_newlines_with_trailing_checksum(
        self, s3_bucket, aws_client, region_name
    ):
        # Boto still does not support chunk encoding, which means we can't test with the client nor
        # aws_http_client_factory. See open issue: https://github.com/boto/boto3/issues/751
        # Test for https://github.com/localstack/localstack/issues/6659
        object_key = "data"
        body = "Hello Blob"
        valid_checksum = hash_sha256(body)
        headers = {
            "Authorization": mock_aws_request_headers(
                "s3",
                aws_access_key_id=TEST_AWS_ACCESS_KEY_ID,
                region_name=region_name,
            )["Authorization"],
            "Content-Type": "audio/mpeg",
            "X-Amz-Content-Sha256": "STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER",
            "X-Amz-Date": "20190918T051509Z",
            "X-Amz-Decoded-Content-Length": str(len(body)),
            "x-amz-trailer": "x-amz-checksum-sha256",
            "Content-Encoding": "aws-chunked",
        }

        def get_data(content: str, checksum_value: str) -> str:
            return (
                "a;chunk-signature=b5311ac60a88890e740a41e74f3d3b03179fd058b1e24bb3ab224042377c4ec9\r\n"
                f"{content}\r\n"
                "0;chunk-signature=78fae1c533e34dbaf2b83ad64ff02e4b64b7bc681ea76b6acf84acf1c48a83cb\r\n"
                f"x-amz-checksum-sha256:{checksum_value}\r\n"
                "x-amz-trailer-signature:712fb67227583c88ac32f468fc30a249cf9ceeb0d0e947ea5e5209a10b99181c\r\n\r\n"
            )

        url = f"{config.internal_service_url()}/{s3_bucket}/{object_key}"

        # test with wrong checksum
        wrong_data = get_data(body, "wrongchecksum")
        request = requests.put(url, wrong_data, headers=headers, verify=False)
        assert request.status_code == 400
        assert "Value for x-amz-checksum-sha256 header is invalid." in request.text

        # assert the object has not been created
        with pytest.raises(ClientError):
            aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)

        # put object with good checksum
        valid_data = get_data(body, valid_checksum)
        req = requests.put(url, valid_data, headers=headers, verify=False)
        assert req.ok

        # get object and assert content length
        downloaded_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        download_file_object = to_str(downloaded_object["Body"].read())
        assert len(body) == len(str(download_file_object))
        assert body == str(download_file_object)

    @markers.aws.only_localstack
    def test_put_object_chunked_checksum(self, s3_bucket, aws_client, region_name):
        # Boto still does not support chunk encoding, which means we can't test with the client nor
        # aws_http_client_factory. See open issue: https://github.com/boto/boto3/issues/751
        # Test for https://github.com/localstack/localstack/issues/6659
        object_key = "data"
        body = "Hello Blob"
        valid_checksum = hash_sha256(body)
        headers = {
            "Authorization": mock_aws_request_headers(
                "s3",
                aws_access_key_id=TEST_AWS_ACCESS_KEY_ID,
                region_name=region_name,
            )["Authorization"],
            "Content-Type": "audio/mpeg",
            "X-Amz-Content-Sha256": "STREAMING-AWS4-HMAC-SHA256-PAYLOAD",
            "X-Amz-Date": "20190918T051509Z",
            "X-Amz-Decoded-Content-Length": str(len(body)),
            "Content-Encoding": "aws-chunked",
        }

        data = (
            "a;chunk-signature=b5311ac60a88890e740a41e74f3d3b03179fd058b1e24bb3ab224042377c4ec9\r\n"
            f"{body}\r\n"
            "0;chunk-signature=78fae1c533e34dbaf2b83ad64ff02e4b64b7bc681ea76b6acf84acf1c48a83cb\r\n"
        )

        url = f"{config.internal_service_url()}/{s3_bucket}/{object_key}"

        # test with wrong checksum
        headers["x-amz-checksum-sha256"] = "wrongchecksum"
        request = requests.put(url, data, headers=headers, verify=False)
        assert request.status_code == 400
        assert "Value for x-amz-checksum-sha256 header is invalid." in request.text

        # assert the object has not been created
        with pytest.raises(ClientError):
            aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)

        # put object with good checksum
        headers["x-amz-checksum-sha256"] = valid_checksum
        req = requests.put(url, data, headers=headers, verify=False)
        assert req.ok

        # get object and assert content length
        downloaded_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        download_file_object = to_str(downloaded_object["Body"].read())
        assert len(body) == len(str(download_file_object))
        assert body == str(download_file_object)

    @markers.aws.only_localstack
    def test_upload_part_chunked_newlines_valid_etag(self, s3_bucket, aws_client, region_name):
        # Boto still does not support chunk encoding, which means we can't test with the client nor
        # aws_http_client_factory. See open issue: https://github.com/boto/boto3/issues/751
        # Test for https://github.com/localstack/localstack/issues/8703
        body = "Hello Blob"
        precalculated_etag = hashlib.md5(body.encode()).hexdigest()
        headers = {
            "Authorization": mock_aws_request_headers(
                "s3",
                aws_access_key_id=TEST_AWS_ACCESS_KEY_ID,
                region_name=region_name,
            )["Authorization"],
            "Content-Type": "audio/mpeg",
            "X-Amz-Content-Sha256": "STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER",
            "X-Amz-Date": "20190918T051509Z",
            "X-Amz-Decoded-Content-Length": str(len(body)),
            "Content-Encoding": "aws-chunked",
        }

        data = (
            "a;chunk-signature=b5311ac60a88890e740a41e74f3d3b03179fd058b1e24bb3ab224042377c4ec9\r\n"
            f"{body}\r\n"
            "0;chunk-signature=78fae1c533e34dbaf2b83ad64ff02e4b64b7bc681ea76b6acf84acf1c48a83cb\r\n"
        )

        key_name = "test-multipart-chunked"
        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
        )
        upload_id = response["UploadId"]

        # # upload the part 1
        url = f"{config.internal_service_url()}/{s3_bucket}/{key_name}?partNumber={1}&uploadId={upload_id}"
        response = requests.put(url, data, headers=headers, verify=False)
        assert response.ok
        part_etag = response.headers.get("ETag")
        assert not response.content

        # validate that the object etag is the same as the pre-calculated one
        assert part_etag.strip('"') == precalculated_etag

        multipart_upload_parts = [
            {
                "ETag": part_etag,
                "PartNumber": 1,
            }
        ]

        aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload={"Parts": multipart_upload_parts},
            UploadId=upload_id,
        )

        completed_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_name)
        assert completed_object["Body"].read() == to_bytes(body)

    @markers.aws.only_localstack
    def test_upload_part_chunked_cancelled_valid_etag(self, s3_bucket, aws_client, region_name):
        """
        When using async-type requests, it's possible to cancel them inflight. This will make the request body
        incomplete, and will fail during the stream decoding. We can simulate this with body by passing an incomplete
        body, which triggers the same kind of exception.
        This test is to avoid regression for https://github.com/localstack/localstack/issues/9851
        """
        body = "Hello Blob"
        precalculated_etag = hashlib.md5(body.encode()).hexdigest()
        headers = {
            "Authorization": mock_aws_request_headers(
                "s3",
                aws_access_key_id=TEST_AWS_ACCESS_KEY_ID,
                region_name=region_name,
            )["Authorization"],
            "Content-Type": "audio/mpeg",
            "X-Amz-Content-Sha256": "STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER",
            "X-Amz-Date": "20190918T051509Z",
            "X-Amz-Decoded-Content-Length": str(len(body)),
            "Content-Encoding": "aws-chunked",
        }

        key_name = "test-multipart-chunked"
        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
        )
        upload_id = response["UploadId"]

        # # upload the invalid part 1
        invalid_data = (
            "\r\n"
            f"{body}\r\n"
            "0;chunk-signature=78fae1c533e34dbaf2b83ad64ff02e4b64b7bc681ea76b6acf84acf1c48a83cb\r\n"
        )
        url = f"{config.internal_service_url()}/{s3_bucket}/{key_name}?partNumber={1}&uploadId={upload_id}"

        response = requests.put(url, invalid_data, headers=headers, verify=False)
        assert response.status_code == 500

        # now re-upload the valid part and assert that the part was correctly uploaded
        data = (
            "a;chunk-signature=b5311ac60a88890e740a41e74f3d3b03179fd058b1e24bb3ab224042377c4ec9\r\n"
            f"{body}\r\n"
            "0;chunk-signature=78fae1c533e34dbaf2b83ad64ff02e4b64b7bc681ea76b6acf84acf1c48a83cb\r\n"
        )
        response = requests.put(url, data, headers=headers, verify=False)
        assert response.ok

        part_etag = response.headers.get("ETag")
        assert not response.content

        # validate that the object etag is the same as the pre-calculated one
        assert part_etag.strip('"') == precalculated_etag

        multipart_upload_parts = [
            {
                "ETag": part_etag,
                "PartNumber": 1,
            }
        ]

        aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload={"Parts": multipart_upload_parts},
            UploadId=upload_id,
        )

        completed_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_name)
        assert completed_object["Body"].read() == to_bytes(body)

    @markers.aws.only_localstack
    def test_put_object_chunked_newlines_no_sig(self, s3_bucket, aws_client, region_name):
        object_key = "data"
        body = "test;test;test\r\ntest1;test1;test1\r\n"
        headers = {
            "Authorization": mock_aws_request_headers(
                "s3", aws_access_key_id=TEST_AWS_ACCESS_KEY_ID, region_name=region_name
            )["Authorization"],
            "Content-Type": "audio/mpeg",
            "X-Amz-Date": "20190918T051509Z",
            "X-Amz-Decoded-Content-Length": str(len(body)),
            "Content-Encoding": "aws-chunked",
            "X-Amz-Trailer": "x-amz-checksum-crc32",
        }
        data = f"23\r\n{body}\r\n0\r\nx-amz-checksum-crc32:AKHICA==\r\n\r\n"
        # put object
        url = f"{config.internal_service_url()}/{s3_bucket}/{object_key}"
        requests.put(url, data, headers=headers, verify=False)
        # get object and assert content length
        downloaded_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        download_file_object = to_str(downloaded_object["Body"].read())
        assert len(body) == len(str(download_file_object))
        assert body == str(download_file_object)

    @markers.aws.only_localstack
    def test_put_object_chunked_newlines_no_sig_empty_body(
        self, s3_bucket, aws_client, region_name
    ):
        object_key = "data"
        headers = {
            "Authorization": mock_aws_request_headers(
                "s3", aws_access_key_id=TEST_AWS_ACCESS_KEY_ID, region_name=region_name
            )["Authorization"],
            "Content-Type": "audio/mpeg",
            "X-Amz-Date": "20190918T051509Z",
            "X-Amz-Decoded-Content-Length": "0",
            "Content-Encoding": "aws-chunked",
            "X-Amz-Trailer": "x-amz-checksum-crc32",
        }
        data = "0\r\nx-amz-checksum-crc32:AAAAAA==\r\n\r\n"
        # put object
        url = f"{config.internal_service_url()}/{s3_bucket}/{object_key}"
        requests.put(url, data, headers=headers, verify=False)
        # get object and assert content length
        downloaded_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        download_file_object = to_str(downloaded_object["Body"].read())
        assert len(str(download_file_object)) == 0

    @markers.aws.only_localstack
    def test_put_object_chunked_content_encoding(self, s3_bucket, aws_client, region_name):
        # when a request is sent with a content-encoding set to `aws-chunked`, AWS will remove it from the object
        # Content-Encoding field.
        # Comment from Amazon employee, saying the server should remove it
        # https://github.com/aws/aws-sdk-java-v2/issues/5769#issuecomment-2594242699
        object_key = "data"
        body = "Hello"
        headers = {
            "Authorization": mock_aws_request_headers(
                "s3",
                aws_access_key_id=TEST_AWS_ACCESS_KEY_ID,
                region_name=region_name,
            )["Authorization"],
            "Content-Type": "audio/mpeg",
            "X-Amz-Content-Sha256": "STREAMING-AWS4-HMAC-SHA256-PAYLOAD",
            "X-Amz-Date": "20190918T051509Z",
            "X-Amz-Decoded-Content-Length": str(len(body)),
            "Content-Encoding": "aws-chunked",
        }
        data = (
            f"5;chunk-signature=af5e6c0a698b0192e9aa5d9083553d4d241d81f69ec62b184d05c509ad5166af\r\n"
            f"{body}\r\n"
            "0;chunk-signature=f2a50a8c0ad4d212b579c2489c6d122db88d8a0d0b987ea1f3e9d081074a5937\r\n"
        )
        # put object
        url = f"{config.internal_service_url()}/{s3_bucket}/{object_key}"
        requests.put(url, data, headers=headers, verify=False)
        # get object and assert content length
        downloaded_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        assert "ContentEncoding" not in downloaded_object

        upload_file_object = BytesIO()
        mtime = 1676569620  # hardcode the GZIP timestamp
        with gzip.GzipFile(fileobj=upload_file_object, mode="w", mtime=mtime) as filestream:
            filestream.write(body.encode("utf-8"))
        raw_gzip = upload_file_object.getvalue()
        gzip_data = (
            b"19;chunk-signature=af5e6c0a698b0192e9aa5d9083553d4d241d81f69ec62b184d05c509ad5166af\r\n"
            + raw_gzip
            + b"\r\n"
            + b"0;chunk-signature=f2a50a8c0ad4d212b579c2489c6d122db88d8a0d0b987ea1f3e9d081074a5937\r\n"
        )
        headers["Content-Encoding"] = "aws-chunked,gzip"
        headers["X-Amz-Decoded-Content-Length"] = str(len(raw_gzip))
        requests.put(url, gzip_data, headers=headers, verify=False, stream=True)
        downloaded_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        # assert that we correctly removed `aws-chunked` from the object ContentEncoding
        assert downloaded_object["ContentEncoding"] == "gzip"
        assert downloaded_object["Body"].read() == raw_gzip

    @markers.aws.only_localstack
    def test_virtual_host_proxy_does_not_decode_gzip(self, aws_client, s3_bucket):
        # Write contents to memory rather than a file.
        data = "123gzipfile"
        upload_file_object = BytesIO()
        mtime = 1676569620  # hardcode the GZIP timestamp
        with gzip.GzipFile(fileobj=upload_file_object, mode="w", mtime=mtime) as filestream:
            filestream.write(data.encode("utf-8"))
        raw_gzip = upload_file_object.getvalue()
        # Upload gzip
        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="test.gz",
            ContentEncoding="gzip",
            Body=raw_gzip,
        )

        key_url = f"{_bucket_url_vhost(s3_bucket)}/test.gz"
        gzip_response = requests.get(key_url, stream=True)
        # get the raw data, don't let requests decode the response
        raw_data = b"".join(chunk for chunk in gzip_response.raw.stream(1024, decode_content=False))
        assert raw_data == raw_gzip

    @markers.aws.only_localstack
    def test_put_object_with_md5_and_chunk_signature(self, s3_bucket, aws_client):
        # Boto still does not support chunk encoding, which means we can't test with the client nor
        # aws_http_client_factory. See open issue: https://github.com/boto/boto3/issues/751
        # Test for https://github.com/localstack/localstack/issues/4987
        object_key = "test-runtime.properties"
        object_data = (
            "#20211122+0100\n"
            "#Mon Nov 22 20:10:44 CET 2021\n"
            "last.sync.url.test-space-key=2822a50f-4992-425a-b8fb-923735a9ddff317e3479-5907-46cf-b33a-60da9709274f\n"
        )
        object_data_chunked = (
            "93;chunk-signature=5be6b2d473e96bb9f297444da60bdf0ff8f5d2e211e1d551b3cf3646c0946641\r\n"
            f"{object_data}"
            "\r\n0;chunk-signature=bd5c830b94346b57ddc8805ba26c44a122256c207014433bf6579b0985f21df7\r\n\r\n"
        )
        content_md5 = base64.b64encode(hashlib.md5(object_data.encode()).digest()).decode()
        headers = {
            "Content-Md5": content_md5,
            "Content-Type": "application/octet-stream",
            "User-Agent": (
                "aws-sdk-java/1.11.951 Mac_OS_X/10.15.7 OpenJDK_64-Bit_Server_VM/11.0.11+9-LTS "
                "java/11.0.11 scala/2.13.6 kotlin/1.5.31 vendor/Amazon.com_Inc."
            ),
            "X-Amz-Content-Sha256": "STREAMING-AWS4-HMAC-SHA256-PAYLOAD",
            "X-Amz-Date": "20211122T191045Z",
            "X-Amz-Decoded-Content-Length": str(len(object_data)),
            "Content-Length": str(len(object_data_chunked)),
            "Connection": "Keep-Alive",
            "Expect": "100-continue",
        }

        url = aws_client.s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": s3_bucket,
                "Key": object_key,
                "ContentType": "application/octet-stream",
                "ContentMD5": content_md5,
            },
        )
        result = requests.put(url, data=object_data_chunked, headers=headers)
        assert result.status_code == 200, (result, result.content)

    @markers.aws.validated
    def test_delete_object_tagging(self, s3_bucket, snapshot, aws_client):
        object_key = "test-key-tagging"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something")
        # get object and assert response
        s3_obj = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-obj", s3_obj)
        # delete object tagging
        aws_client.s3.delete_object_tagging(Bucket=s3_bucket, Key=object_key)
        # assert that the object still exists
        s3_obj = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-obj-after-tag-deletion", s3_obj)

    @markers.aws.validated
    def test_delete_non_existing_keys_quiet(self, s3_bucket, snapshot, aws_client):
        object_key = "test-key-nonexistent"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something")
        response = aws_client.s3.delete_objects(
            Bucket=s3_bucket,
            Delete={
                "Objects": [{"Key": object_key}, {"Key": "dummy1"}, {"Key": "dummy2"}],
                "Quiet": True,
            },
        )
        snapshot.match("deleted-resp", response)
        assert "Deleted" not in response
        assert "Errors" not in response

    @markers.aws.validated
    def test_delete_non_existing_keys(self, s3_bucket, snapshot, aws_client):
        object_key = "test-key-nonexistent"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something")
        response = aws_client.s3.delete_objects(
            Bucket=s3_bucket,
            Delete={
                "Objects": [{"Key": object_key}, {"Key": "dummy1"}, {"Key": "dummy2"}],
            },
        )
        response["Deleted"].sort(key=itemgetter("Key"))
        snapshot.match("deleted-resp", response)
        assert len(response["Deleted"]) == 3
        assert "Errors" not in response

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        # we cannot guarantee order nor we can sort it
        path=["$..Deleted..VersionId"],
    )
    def test_delete_keys_in_versioned_bucket(self, s3_bucket, snapshot, aws_client):
        # see https://docs.aws.amazon.com/AmazonS3/latest/userguide/DeletingObjectVersions.html
        snapshot.add_transformer(snapshot.transform.s3_api())
        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        object_key = "test-key-versioned"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something")
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something-v2")

        response = aws_client.s3.list_objects_v2(Bucket=s3_bucket)
        snapshot.match("list-objects-v2", response)

        # delete objects
        response = aws_client.s3.delete_objects(
            Bucket=s3_bucket,
            Delete={
                "Objects": [{"Key": object_key}],
            },
        )
        snapshot.match("delete-object", response)

        response = aws_client.s3.list_object_versions(Bucket=s3_bucket)
        snapshot.match("list-object-version", response)

        # delete objects with version
        versions_to_delete = [
            {"Key": version["Key"], "VersionId": version["VersionId"]}
            for version in response["Versions"]
        ]
        response = aws_client.s3.delete_objects(
            Bucket=s3_bucket,
            Delete={"Objects": versions_to_delete},
        )
        snapshot.match("delete-object-version", response)

        response = aws_client.s3.list_objects_v2(Bucket=s3_bucket)
        snapshot.match("list-objects-v2-after-delete", response)

        response = aws_client.s3.list_object_versions(Bucket=s3_bucket)
        snapshot.match("list-object-version-after-delete", response)

        delete_marker = response["DeleteMarkers"][0]
        response = aws_client.s3.delete_objects(
            Bucket=s3_bucket,
            Delete={
                "Objects": [{"Key": delete_marker["Key"], "VersionId": delete_marker["VersionId"]}]
            },
        )
        snapshot.match("delete-object-delete-marker", response)

    @markers.aws.validated
    def test_delete_non_existing_keys_in_non_existing_bucket(self, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))
        with pytest.raises(ClientError) as e:
            aws_client.s3.delete_objects(
                Bucket=f"non-existent-bucket-{long_uid()}",
                Delete={"Objects": [{"Key": "dummy1"}, {"Key": "dummy2"}]},
            )
        assert "NoSuchBucket" == e.value.response["Error"]["Code"]
        snapshot.match("error-non-existent-bucket", e.value.response)

    @markers.aws.validated
    def test_delete_objects_encoding(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("Name"))
        object_key_1 = "a%2Fb"
        object_key_2 = "a/%F0%9F%98%80"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key_1, Body="percent encoding")
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key_2, Body="percent encoded emoji")

        list_objects = aws_client.s3.list_objects_v2(Bucket=s3_bucket)
        snapshot.match("list-objects-before-delete", list_objects)

        response = aws_client.s3.delete_objects(
            Bucket=s3_bucket,
            Delete={
                "Objects": [
                    {"Key": object_key_1},
                    {"Key": object_key_2},
                ],
            },
        )
        response["Deleted"].sort(key=itemgetter("Key"))
        snapshot.match("deleted-resp", response)

        list_objects = aws_client.s3.list_objects_v2(Bucket=s3_bucket)
        snapshot.match("list-objects", list_objects)

    @markers.aws.validated
    def test_put_object_acl_on_delete_marker(
        self, s3_bucket, allow_bucket_acl, snapshot, aws_client
    ):
        # see https://docs.aws.amazon.com/AmazonS3/latest/userguide/DeletingObjectVersions.html
        snapshot.add_transformer(snapshot.transform.s3_api())
        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        object_key = "test-key-versioned"
        put_obj_1 = aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something")
        snapshot.match("put-obj-1", put_obj_1)
        put_obj_2 = aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something-v2")
        snapshot.match("put-obj-2", put_obj_2)

        response = aws_client.s3.delete_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("delete-object", response)
        delete_marker_version_id = response["VersionId"]

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(Bucket=s3_bucket, Key=object_key, ACL="public-read")
        snapshot.match("put-acl-delete-marker", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object_acl(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-acl-delete-marker", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_acl(
                Bucket=s3_bucket,
                Key=object_key,
                VersionId=delete_marker_version_id,
                ACL="public-read",
            )
        snapshot.match("put-acl-delete-marker-version-id", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object_acl(
                Bucket=s3_bucket, Key=object_key, VersionId=delete_marker_version_id
            )
        snapshot.match("get-acl-delete-marker-version-id", e.value.response)

    @markers.aws.validated
    def test_s3_request_payer(self, s3_bucket, snapshot, aws_client):
        response = aws_client.s3.put_bucket_request_payment(
            Bucket=s3_bucket, RequestPaymentConfiguration={"Payer": "Requester"}
        )
        snapshot.match("put-bucket-request-payment", response)
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

        response = aws_client.s3.get_bucket_request_payment(Bucket=s3_bucket)
        snapshot.match("get-bucket-request-payment", response)
        assert "Requester" == response["Payer"]

    @markers.aws.validated
    def test_s3_request_payer_exceptions(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_request_payment(
                Bucket=s3_bucket, RequestPaymentConfiguration={"Payer": "Random"}
            )
        snapshot.match("wrong-payer-type", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_request_payment(
                Bucket=f"fake-bucket-{long_uid()}",
                RequestPaymentConfiguration={"Payer": "Requester"},
            )
        snapshot.match("wrong-bucket-name", e.value.response)

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        paths=["$..Error.RequestID", "$..Grants..Grantee.DisplayName"]
    )
    def test_bucket_exists(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("DisplayName"),
                snapshot.transform.key_value("ID", value_replacement="owner-id"),
            ]
        )
        aws_client.s3.put_bucket_cors(
            Bucket=s3_bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedMethods": ["GET", "POST", "PUT", "DELETE"],
                        "AllowedOrigins": ["localhost"],
                    }
                ]
            },
        )

        response = aws_client.s3.get_bucket_cors(Bucket=s3_bucket)
        snapshot.match("get-bucket-cors", response)

        result = aws_client.s3.get_bucket_acl(Bucket=s3_bucket)
        snapshot.match("get-bucket-acl", result)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_acl(Bucket="bucket-not-exists")
        snapshot.match("get-bucket-not-exists", e.value.response)

    @markers.aws.validated
    def test_s3_uppercase_key_names(self, s3_create_bucket, snapshot, aws_client):
        # bucket name should be case-sensitive
        bucket_name = f"testuppercase-{short_uid()}"
        s3_create_bucket(Bucket=bucket_name)

        # key name should be case-sensitive
        object_key = "camelCaseKey"
        aws_client.s3.put_object(Bucket=bucket_name, Key=object_key, Body="something")
        res = aws_client.s3.get_object(Bucket=bucket_name, Key=object_key)
        snapshot.match("response", res)
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=bucket_name, Key="camelcasekey")
        snapshot.match("wrong-case-key", e.value.response)

    @pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="Lambda not enabled in S3 image")
    @markers.aws.validated
    def test_s3_download_object_with_lambda(
        self, s3_bucket, create_lambda_function, lambda_su_role, aws_client
    ):
        function_name = f"func-{short_uid()}"
        key = f"key-{short_uid()}"

        aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body="something..")

        create_lambda_function(
            handler_file=os.path.join(
                os.path.dirname(__file__),
                "../lambda_",
                "functions",
                "lambda_triggered_by_sqs_download_s3_file.py",
            ),
            func_name=function_name,
            role=lambda_su_role,
            runtime=Runtime.python3_12,
            envvars={
                "BUCKET_NAME": s3_bucket,
                "OBJECT_NAME": key,
                "LOCAL_FILE_NAME": "/tmp/" + key,
            },
        )
        aws_client.lambda_.invoke(FunctionName=function_name, InvocationType="Event")

        # TODO maybe this check can be improved (do not rely on logs)
        retry(
            check_expected_lambda_log_events_length,
            retries=10,
            sleep=1,
            function_name=function_name,
            regex_filter="success",
            expected_length=1,
            logs_client=aws_client.logs,
        )

    @markers.aws.validated
    def test_precondition_failed_error(self, s3_bucket, snapshot, aws_client):
        aws_client.s3.put_object(Bucket=s3_bucket, Key="foo", Body=b'{"foo": "bar"}')

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=s3_bucket, Key="foo", IfMatch='"not good etag"')

        snapshot.match("get-object-if-match", e.value.response)

    @markers.aws.validated
    def test_s3_invalid_content_md5(self, s3_bucket, snapshot, aws_client):
        # put object with invalid content MD5
        # TODO: implement ContentMD5 in ASF
        content = "something"
        response = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="test-key",
            Body=content,
        )
        md = hashlib.md5(content.encode("utf-8")).digest()
        content_md5 = base64.b64encode(md).decode("utf-8")
        base_64_content_md5 = etag_to_base_64_content_md5(response["ETag"])
        assert content_md5 == base_64_content_md5

        bad_digest_md5 = base64.b64encode(
            hashlib.md5(f"{content}1".encode("utf-8")).digest()
        ).decode("utf-8")

        hashes = [
            "__invalid__",
            "000",
            "not base64 encoded checksum",
            "MTIz",
            base64.b64encode(b"test-string").decode("utf-8"),
        ]

        for index, md5hash in enumerate(hashes):
            with pytest.raises(ClientError) as e:
                aws_client.s3.put_object(
                    Bucket=s3_bucket,
                    Key="test-key",
                    Body=content,
                    ContentMD5=md5hash,
                )
            snapshot.match(f"md5-error-{index}", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key="test-key",
                Body=content,
                ContentMD5=bad_digest_md5,
            )
        snapshot.match("md5-error-bad-digest", e.value.response)

        response = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="test-key",
            Body=content,
            ContentMD5=base_64_content_md5,
        )
        snapshot.match("success-put-object-md5", response)

        # also try with UploadPart, same logic
        create_multipart = aws_client.s3.create_multipart_upload(Bucket=s3_bucket, Key="multi-key")
        upload_id = create_multipart["UploadId"]

        for index, md5hash in enumerate(hashes):
            with pytest.raises(ClientError) as e:
                aws_client.s3.upload_part(
                    Bucket=s3_bucket,
                    Key="multi-key",
                    Body=content,
                    UploadId=upload_id,
                    PartNumber=1,
                    ContentMD5=md5hash,
                )
            snapshot.match(f"upload-part-md5-error-{index}", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key="multi-key",
                Body=content,
                UploadId=upload_id,
                PartNumber=1,
                ContentMD5=bad_digest_md5,
            )
        snapshot.match("upload-part-md5-bad-digest", e.value.response)

        response = aws_client.s3.upload_part(
            Bucket=s3_bucket,
            Key="multi-key",
            Body=content,
            UploadId=upload_id,
            PartNumber=1,
            ContentMD5=base_64_content_md5,
        )
        snapshot.match("success-upload-part-md5", response)

    @markers.aws.validated
    def test_s3_upload_download_gzip(self, s3_bucket, snapshot, aws_client):
        data = "1234567890 " * 100

        # Write contents to memory rather than a file.
        upload_file_object = BytesIO()
        mtime = 1676569620  # hardcode the GZIP timestamp
        with gzip.GzipFile(fileobj=upload_file_object, mode="w", mtime=mtime) as filestream:
            filestream.write(data.encode("utf-8"))

        # Upload gzip
        response = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="test.gz",
            ContentEncoding="gzip",
            Body=upload_file_object.getvalue(),
        )
        snapshot.match("put-object", response)

        # Download gzip
        downloaded_object = aws_client.s3.get_object(Bucket=s3_bucket, Key="test.gz")
        snapshot.match("get-object", downloaded_object)
        download_file_object = BytesIO(downloaded_object["Body"].read())
        with gzip.GzipFile(fileobj=download_file_object, mode="rb") as filestream:
            downloaded_data = filestream.read().decode("utf-8")

        assert downloaded_data == data

    @markers.aws.validated
    def test_multipart_overwrite_key(self, s3_bucket, s3_multipart_upload, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("Bucket"),
            ]
        )
        key = "test.file"
        content = b"test content 123"
        put_object = aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body=content)
        snapshot.match("put-object", put_object)

        # create a multipart upload on an existing key, overwrite it
        response = s3_multipart_upload(bucket=s3_bucket, key=key, data=content)
        snapshot.match("multipart-upload", response)

        get_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=key)
        assert get_object["Body"].read() == content

    @markers.aws.validated
    def test_multipart_copy_object_etag(self, s3_bucket, s3_multipart_upload, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("Bucket"),
            ]
        )
        key = "test.file"
        copy_key = "copy.file"
        src_object_path = f"{s3_bucket}/{key}"
        content = "test content 123"

        response = s3_multipart_upload(bucket=s3_bucket, key=key, data=content)
        snapshot.match("multipart-upload", response)
        multipart_etag = response["ETag"]

        response = aws_client.s3.copy_object(
            Bucket=s3_bucket, CopySource=src_object_path, Key=copy_key
        )
        snapshot.match("copy-object", response)
        copy_etag = response["CopyObjectResult"]["ETag"]
        # etags should be different
        assert copy_etag != multipart_etag

        # copy-in place to check
        response = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource=src_object_path,
            Key=key,
            MetadataDirective="REPLACE",
        )
        snapshot.match("copy-object-in-place", response)
        copy_etag = response["CopyObjectResult"]["ETag"]
        # etags should be different
        assert copy_etag != multipart_etag

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=key, ChecksumMode="ENABLED")
        snapshot.match("head-obj", head_object)

    @markers.aws.validated
    def test_get_object_part(self, s3_bucket, s3_multipart_upload, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("Bucket"),
            ]
        )
        key = "test.file"
        content = "test content 123"

        response = s3_multipart_upload(bucket=s3_bucket, key=key, data=content, parts=2)
        snapshot.match("multipart-upload", response)

        head_object_part = aws_client.s3.head_object(Bucket=s3_bucket, Key=key, PartNumber=2)
        snapshot.match("head-object-part", head_object_part)

        get_object_part = aws_client.s3.get_object(Bucket=s3_bucket, Key=key, PartNumber=2)
        snapshot.match("get-object-part", get_object_part)

        get_object_part = aws_client.s3.get_object(
            Bucket=s3_bucket, Key=key, PartNumber=2, ChecksumMode="ENABLED"
        )
        snapshot.match("get-object-part-with-checksum", get_object_part)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=s3_bucket, Key=key, PartNumber=10)
        snapshot.match("part-doesnt-exist", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(
                Bucket=s3_bucket,
                Key=key,
                PartNumber=2,
                Range="bytes=0-8",
            )
        snapshot.match("part-with-range", e.value.response)

        key_no_part = "key-no-part"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=key_no_part, Body="test-123")
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=s3_bucket, Key=key_no_part, PartNumber=2)
        snapshot.match("part-no-multipart", e.value.response)

        get_obj_no_part = aws_client.s3.get_object(
            Bucket=s3_bucket, Key=key_no_part, PartNumber=1, ChecksumMode="ENABLED"
        )
        snapshot.match("get-obj-no-multipart", get_obj_no_part)

    @markers.aws.validated
    @pytest.mark.parametrize("checksum_type", ("COMPOSITE", "FULL_OBJECT"))
    def test_get_object_part_checksum(self, s3_bucket, snapshot, aws_client, checksum_type):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("Bucket"),
                snapshot.transform.key_value("UploadId"),
            ]
        )
        content = "test content 123"
        key_name = "test-multipart-checksum"
        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket, Key=key_name, ChecksumAlgorithm="CRC32C", ChecksumType=checksum_type
        )
        snapshot.match("create-mpu-checksum", response)
        upload_id = response["UploadId"]

        part_number = 1
        response = aws_client.s3.upload_part(
            Bucket=s3_bucket,
            Key=key_name,
            Body=content,
            PartNumber=part_number,
            UploadId=upload_id,
            ChecksumAlgorithm="CRC32C",
        )
        snapshot.match("upload-part", response)
        multipart_upload_parts = [
            {
                "ETag": response["ETag"],
                "PartNumber": part_number,
                "ChecksumCRC32C": response["ChecksumCRC32C"],
            }
        ]

        response = aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload={"Parts": multipart_upload_parts},
            UploadId=upload_id,
        )
        snapshot.match("complete-multipart-checksum", response)

        head_object_part = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=key_name, PartNumber=1, ChecksumMode="ENABLED"
        )
        snapshot.match("head-object-part", head_object_part)

        get_object_part = aws_client.s3.get_object(
            Bucket=s3_bucket, Key=key_name, PartNumber=1, ChecksumMode="ENABLED"
        )
        snapshot.match("get-object-part", get_object_part)

    @markers.aws.validated
    def test_set_external_hostname(
        self, s3_bucket, allow_bucket_acl, s3_multipart_upload, monkeypatch, snapshot, aws_client
    ):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("Bucket"),
            ]
        )
        custom_hostname = "foobar"
        monkeypatch.setattr(
            config,
            "LOCALSTACK_HOST",
            config.HostAndPort(host=custom_hostname, port=config.GATEWAY_LISTEN[0].port),
        )
        key = "test.file"
        content = "test content 123"
        acl = "public-read"
        # upload file
        response = s3_multipart_upload(bucket=s3_bucket, key=key, data=content, acl=acl)
        snapshot.match("multipart-upload", response)

        assert s3_bucket in response["Location"]
        assert key in response["Location"]
        if not is_aws_cloud():
            expected_url = (
                f"{_bucket_url(bucket_name=s3_bucket, localstack_host=custom_hostname)}/{key}"
            )
            assert response["Location"] == expected_url

        # download object via API
        downloaded_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=key)
        snapshot.match("get-object", response)
        assert content == to_str(downloaded_object["Body"].read())

        # download object directly from download link
        download_url = response["Location"].replace(
            f"{get_localstack_host().host}:", "localhost.localstack.cloud:"
        )
        response = requests.get(download_url)
        assert response.status_code == 200
        assert to_str(response.content) == content

    @markers.aws.only_localstack
    def test_s3_hostname_with_subdomain(self, aws_http_client_factory, aws_client):
        """
        This particular test validates the fix for localstack#7424
        Moto would still validate with the `host` header if buckets where subdomain based even though in the new ASF
        provider, every request was forwarded by the VirtualHost proxy.
        """
        s3_http_client = aws_http_client_factory("s3", signer_factory=SigV4Auth)
        endpoint_url = _endpoint_url()
        # this will represent a ListBuckets call, calling the base endpoint
        resp = s3_http_client.get(endpoint_url)
        assert resp.ok
        assert b"<Bucket" in resp.content

        # the same ListBuckets call, but with subdomain based `host` header
        resp = s3_http_client.get(endpoint_url, headers={"host": "aws.test.local"})
        assert resp.ok
        assert b"<Bucket" in resp.content

    @pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="Lambda not enabled in S3 image")
    @markers.skip_offline
    @markers.aws.validated
    def test_s3_lambda_integration(
        self,
        create_lambda_function,
        lambda_su_role,
        s3_create_bucket,
        create_tmp_folder_lambda,
        snapshot,
        aws_client,
    ):
        snapshot.add_transformer(snapshot.transform.s3_api())
        handler_file = os.path.join(
            os.path.dirname(__file__), "../lambda_/functions/lambda_s3_integration.mjs"
        )
        temp_folder = create_tmp_folder_lambda(
            handler_file,
            run_command="npm i @aws-sdk/util-endpoints @aws-sdk/client-s3 @aws-sdk/s3-request-presigner @aws-sdk/middleware-endpoint",
        )

        function_name = f"func-integration-{short_uid()}"
        create_lambda_function(
            func_name=function_name,
            zip_file=testutil.create_zip_file(temp_folder, get_content=True),
            runtime=Runtime.nodejs20_x,
            handler="lambda_s3_integration.handler",
            role=lambda_su_role,
        )
        s3_create_bucket(Bucket=function_name)

        response = aws_client.lambda_.invoke(FunctionName=function_name)
        payload = json.load(response["Payload"])
        presigned_url = payload["body"].strip('"')

        response = requests.put(presigned_url, verify=False)
        assert response.status_code == 200

        response = aws_client.s3.head_object(Bucket=function_name, Key="key.png")
        snapshot.match("head_object", response)

    @markers.aws.validated
    def test_s3_uppercase_bucket_name(self, s3_create_bucket, snapshot, aws_client):
        # bucket name should be lower-case
        snapshot.add_transformer(snapshot.transform.s3_api())
        bucket_name = f"TESTUPPERCASE-{short_uid()}"
        with pytest.raises(ClientError) as e:
            s3_create_bucket(Bucket=bucket_name)
        snapshot.match("uppercase-bucket", e.value.response)

    @markers.aws.validated
    def test_create_bucket_with_existing_name(
        self, s3_create_bucket_with_client, snapshot, aws_client_factory
    ):
        snapshot.add_transformer(snapshot.transform.s3_api())
        bucket_name = f"bucket-{short_uid()}"
        us_east_1_client = aws_client_factory(region_name=AWS_REGION_US_EAST_1).s3
        s3_create_bucket_with_client(
            us_east_1_client,
            Bucket=bucket_name,
            CreateBucketConfiguration={"LocationConstraint": "us-west-1"},
        )

        for loc_constraint in ["us-west-1", "us-east-2"]:
            with pytest.raises(ClientError) as e:
                us_east_1_client.create_bucket(
                    Bucket=bucket_name,
                    CreateBucketConfiguration={"LocationConstraint": loc_constraint},
                )
            e.match("BucketAlreadyOwnedByYou")
            snapshot.match(f"create-bucket-{loc_constraint}", e.value.response)

    @markers.aws.validated
    @pytest.mark.skipif(
        reason="asf provider: routing for region-path style not working; "
        "both provider: return 200 for other regions (no redirects)"
    )
    def test_access_bucket_different_region(self, s3_create_bucket, s3_vhost_client, aws_client):
        bucket_name = f"my-bucket-{short_uid()}"

        s3_create_bucket(
            Bucket=bucket_name,
            ACL="public-read",
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        s3_vhost_client.list_objects(Bucket=bucket_name)
        bucket_vhost_url = _bucket_url_vhost(bucket_name, region="us-west-2")
        response = requests.get(bucket_vhost_url)
        assert response.status_code == 200

        bucket_url = _bucket_url(bucket_name, region="us-west-2")
        response = requests.get(bucket_url)
        assert response.status_code == 200

        bucket_vhost_url = _bucket_url_vhost(bucket_name, region="us-east-2")
        response = requests.get(bucket_vhost_url)
        assert response.status_code == 301

        bucket_vhost_url = _bucket_url_vhost(bucket_name, region="us-east-1")
        response = requests.get(bucket_vhost_url)
        assert response.status_code == 200
        assert response.history[0].status_code == 307

    @markers.aws.validated
    def test_bucket_does_not_exist(self, s3_vhost_client, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        bucket_name = f"bucket-does-not-exist-{short_uid()}"

        with pytest.raises(ClientError) as e:
            aws_client.s3.list_objects(Bucket=bucket_name)
        e.match("NoSuchBucket")
        snapshot.match("list_object", e.value.response)

        with pytest.raises(ClientError) as e:
            s3_vhost_client.list_objects(Bucket=bucket_name)
        e.match("NoSuchBucket")
        snapshot.match("list_object_vhost", e.value.response)

        bucket_vhost_url = _bucket_url_vhost(bucket_name, region="us-east-1")
        assert "us-east-1" not in bucket_vhost_url

        response = requests.get(bucket_vhost_url)
        assert response.status_code == 404

        bucket_url = _bucket_url(bucket_name, region="us-east-1")
        assert "us-east-1" not in bucket_url
        response = requests.get(bucket_url)
        assert response.status_code == 404

        bucket_vhost_url = _bucket_url_vhost(bucket_name, region="eu-central-1")
        assert "eu-central-1" in bucket_vhost_url
        response = requests.get(bucket_vhost_url)
        assert response.status_code == 404

        bucket_url = _bucket_url(bucket_name, region="eu-central-1")
        assert "eu-central-1" in bucket_url
        response = requests.get(bucket_url)
        assert response.status_code == 404

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        paths=["$..x-amz-access-point-alias", "$..x-amz-id-2", "$..AccessPointAlias"],
    )
    def test_create_bucket_head_bucket(
        self, aws_client_factory, snapshot, aws_client, secondary_region_name
    ):
        snapshot.add_transformer(snapshot.transform.s3_api())

        bucket_1 = f"my-bucket-1{short_uid()}"
        bucket_2 = f"my-bucket-2{short_uid()}"

        snapshot.add_transformers_list(
            [
                snapshot.transform.regex(rf"{bucket_1}", "<bucket-name:1>"),
                snapshot.transform.regex(rf"{bucket_2}", "<bucket-name:2>"),
                snapshot.transform.key_value("x-amz-id-2", reference_replacement=False),
                snapshot.transform.key_value("x-amz-request-id", reference_replacement=False),
                snapshot.transform.regex(r"s3\.amazonaws\.com", "<host>"),
                snapshot.transform.regex(r"s3\.localhost\.localstack\.cloud:4566", "<host>"),
                snapshot.transform.regex(r"s3\.localhost\.localstack\.cloud:443", "<host>"),
                snapshot.transform.key_value("x-amz-bucket-region"),
            ]
        )

        client_us_east_1 = aws_client_factory(region_name=AWS_REGION_US_EAST_1).s3
        try:
            response = client_us_east_1.create_bucket(Bucket=bucket_1)
            snapshot.match("create_bucket", response)

            response = client_us_east_1.create_bucket(
                Bucket=bucket_2,
                CreateBucketConfiguration={
                    "LocationConstraint": secondary_region_name,
                },
            )
            snapshot.match("create_bucket_location_constraint", response)

            response = client_us_east_1.head_bucket(Bucket=bucket_1)
            snapshot.match("head_bucket", response)
            snapshot.match(
                "head_bucket_filtered_header",
                _filter_header(response["ResponseMetadata"]["HTTPHeaders"]),
            )

            response = aws_client.s3.head_bucket(Bucket=bucket_2)
            snapshot.match("head_bucket_2", response)
            snapshot.match(
                "head_bucket_2_filtered_header",
                _filter_header(response["ResponseMetadata"]["HTTPHeaders"]),
            )

            with pytest.raises(ClientError) as e:
                aws_client.s3.head_bucket(Bucket=f"does-not-exist-{long_uid()}")
            snapshot.match("head_bucket_not_exist", e.value.response)
        finally:
            client_us_east_1.delete_bucket(Bucket=bucket_1)
            client_us_east_1.delete_bucket(Bucket=bucket_2)

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        # TODO: it seems that we should not return the Owner when the request is public, but we dont have that concept
        paths=["$..ListBucketResult.Contents.Owner"],
    )
    def test_bucket_name_with_dots(self, s3_create_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        bucket_name = f"my.bucket.name.{short_uid()}"

        s3_create_bucket(Bucket=bucket_name)
        aws_client.s3.delete_bucket_ownership_controls(Bucket=bucket_name)
        aws_client.s3.delete_public_access_block(Bucket=bucket_name)
        aws_client.s3.put_bucket_acl(Bucket=bucket_name, ACL="public-read")
        aws_client.s3.put_object(Bucket=bucket_name, Key="my-content", Body="something")
        response = aws_client.s3.list_objects(Bucket=bucket_name)
        snapshot.match("list-objects", response)

        # will result in a host-name-match if we use https, as the bucket contains dots
        response_vhost = requests.get(_bucket_url_vhost(bucket_name).replace("https://", "http://"))
        vhost_xml_response = xmltodict.parse(response_vhost.content)
        snapshot.match("request-vhost-url-content", vhost_xml_response)

        response_path_style = requests.get(_bucket_url(bucket_name))
        path_xml_response = xmltodict.parse(response_path_style.content)

        snapshot.match("request-path-url-content", path_xml_response)

    @markers.aws.validated
    def test_s3_put_more_than_1000_items(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        for i in range(0, 1010, 1):
            body = "test-" + str(i)
            key = "test-key-" + str(i)
            aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body=body)

        # trying to get the last item of 1010 items added.
        resp = aws_client.s3.get_object(Bucket=s3_bucket, Key="test-key-1009")
        snapshot.match("get_object-1009", resp)

        # trying to get the first item of 1010 items added.
        resp = aws_client.s3.get_object(Bucket=s3_bucket, Key="test-key-0")
        snapshot.match("get_object-0", resp)

        # according docs for MaxKeys: the response might contain fewer keys but will never contain more.
        # AWS returns less during testing
        resp = aws_client.s3.list_objects(Bucket=s3_bucket, MaxKeys=1010)
        assert 1010 >= len(resp["Contents"])

        resp = aws_client.s3.list_objects(Bucket=s3_bucket, Delimiter="/")
        assert 1000 == len(resp["Contents"])
        # way too much content, remove it from this match
        snapshot.add_transformer(
            snapshot.transform.jsonpath(
                "$..list-objects.Contents", "<content>", reference_replacement=False
            )
        )
        snapshot.match("list-objects", resp)
        next_marker = resp["NextMarker"]

        # Second list
        resp = aws_client.s3.list_objects(Bucket=s3_bucket, Marker=next_marker)
        snapshot.match("list-objects-next_marker", resp)
        assert 10 == len(resp["Contents"])

    @markers.aws.validated
    def test_upload_big_file(self, s3_create_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        bucket_name = f"bucket-{short_uid()}"
        key1 = "test_key1"
        key2 = "test_key1"

        s3_create_bucket(Bucket=bucket_name)

        body1 = "\x01" * 10000000
        rs = aws_client.s3.put_object(Bucket=bucket_name, Key=key1, Body=body1)
        snapshot.match("put_object_key1", rs)

        body2 = "a" * 10000000
        rs = aws_client.s3.put_object(Bucket=bucket_name, Key=key2, Body=body2)
        snapshot.match("put_object_key2", rs)

        rs = aws_client.s3.head_object(Bucket=bucket_name, Key=key1)
        snapshot.match("head_object_key1", rs)

        rs = aws_client.s3.head_object(Bucket=bucket_name, Key=key2)
        snapshot.match("head_object_key2", rs)

    @markers.aws.validated
    def test_get_bucket_versioning_order(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        rs = aws_client.s3.list_object_versions(Bucket=s3_bucket, EncodingType="url")
        snapshot.match("list_object_versions_before", rs)

        rs = aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        snapshot.match("put_bucket_versioning", rs)

        rs = aws_client.s3.get_bucket_versioning(Bucket=s3_bucket)
        snapshot.match("get_bucket_versioning", rs)

        aws_client.s3.put_object(Bucket=s3_bucket, Key="test", Body="body")
        aws_client.s3.put_object(Bucket=s3_bucket, Key="test", Body="body")
        aws_client.s3.put_object(Bucket=s3_bucket, Key="test2", Body="body")
        rs = aws_client.s3.list_object_versions(
            Bucket=s3_bucket,
        )

        snapshot.match("list_object_versions", rs)

    @markers.aws.validated
    def test_etag_on_get_object_call(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = "my-key"

        body = "Lorem ipsum dolor sit amet, ... " * 30
        rs = aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body=body)

        rs = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get_object", rs)

        rs = aws_client.s3.get_object(
            Bucket=s3_bucket,
            Key=object_key,
            Range="bytes=0-16",
        )
        snapshot.match("get_object_range", rs)

    @markers.aws.validated
    def test_s3_delete_object_with_version_id(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())

        test_1st_key = "aws/s3/testkey1.txt"
        test_2nd_key = "aws/s3/testkey2.txt"

        body = "Lorem ipsum dolor sit amet, ... " * 30

        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
        rs = aws_client.s3.get_bucket_versioning(Bucket=s3_bucket)
        snapshot.match("get_bucket_versioning", rs)

        # put 2 objects
        rs = aws_client.s3.put_object(Bucket=s3_bucket, Key=test_1st_key, Body=body)
        aws_client.s3.put_object(Bucket=s3_bucket, Key=test_2nd_key, Body=body)
        version_id = rs["VersionId"]

        # delete 1st object with version
        rs = aws_client.s3.delete_objects(
            Bucket=s3_bucket,
            Delete={"Objects": [{"Key": test_1st_key, "VersionId": version_id}]},
        )

        deleted = rs["Deleted"][0]
        assert test_1st_key == deleted["Key"]
        assert version_id == deleted["VersionId"]
        snapshot.match("delete_objects", rs)

        rs = aws_client.s3.list_object_versions(Bucket=s3_bucket)
        object_versions = [object["VersionId"] for object in rs["Versions"]]
        snapshot.match("list_object_versions_after_delete", rs)

        assert version_id not in object_versions

        # disable versioning
        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket,
            VersioningConfiguration={"Status": "Suspended"},
        )
        rs = aws_client.s3.get_bucket_versioning(Bucket=s3_bucket)
        snapshot.match("get_bucket_versioning_suspended", rs)

    @markers.aws.validated
    def test_s3_put_object_versioned(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())

        # this object is put before the bucket is versioned, its internal versionId is `null`
        key = "non-version-bucket-key"
        put_obj_pre_versioned = aws_client.s3.put_object(
            Bucket=s3_bucket, Key=key, Body="non-versioned-key"
        )
        snapshot.match("put-pre-versioned", put_obj_pre_versioned)
        get_obj_pre_versioned = aws_client.s3.get_object(Bucket=s3_bucket, Key=key)
        snapshot.match("get-pre-versioned", get_obj_pre_versioned)

        list_obj_pre_versioned = aws_client.s3.list_object_versions(Bucket=s3_bucket)
        snapshot.match("list-object-pre-versioned", list_obj_pre_versioned)

        # we activate the bucket versioning then check if the object has a versionId
        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )

        get_obj_non_versioned = aws_client.s3.get_object(Bucket=s3_bucket, Key=key)
        snapshot.match("get-post-versioned", get_obj_non_versioned)

        # create versioned key, then update it, and check we got the last versionId
        key_2 = "versioned-bucket-key"
        put_obj_versioned_1 = aws_client.s3.put_object(
            Bucket=s3_bucket, Key=key_2, Body="versioned-key"
        )
        snapshot.match("put-obj-versioned-1", put_obj_versioned_1)
        put_obj_versioned_2 = aws_client.s3.put_object(
            Bucket=s3_bucket, Key=key_2, Body="versioned-key-updated"
        )
        snapshot.match("put-obj-versioned-2", put_obj_versioned_2)

        get_obj_versioned = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_2)
        snapshot.match("get-obj-versioned", get_obj_versioned)

        list_obj_post_versioned = aws_client.s3.list_object_versions(Bucket=s3_bucket)
        snapshot.match("list-object-versioned", list_obj_post_versioned)

        # disable versioning to check behaviour after getting keys
        # all keys will now have versionId when getting them, even non-versioned ones
        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket,
            VersioningConfiguration={"Status": "Suspended"},
        )
        list_obj_post_versioned_disabled = aws_client.s3.list_object_versions(Bucket=s3_bucket)
        snapshot.match("list-bucket-suspended", list_obj_post_versioned_disabled)

        get_obj_versioned_disabled = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_2)
        snapshot.match("get-obj-versioned-disabled", get_obj_versioned_disabled)

        get_obj_non_versioned_disabled = aws_client.s3.get_object(Bucket=s3_bucket, Key=key)
        snapshot.match("get-obj-non-versioned-disabled", get_obj_non_versioned_disabled)

        # won't return the versionId from put
        key_3 = "non-version-bucket-key-after-disable"
        put_obj_non_version_post_disable = aws_client.s3.put_object(
            Bucket=s3_bucket, Key=key_3, Body="non-versioned-key-post"
        )
        snapshot.match("put-non-versioned-post-disable", put_obj_non_version_post_disable)
        # will return the versionId now, when it didn't before setting the BucketVersioning to `Enabled`
        get_obj_non_version_post_disable = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_3)
        snapshot.match("get-non-versioned-post-disable", get_obj_non_version_post_disable)

        # manually assert all VersionId, as it's hard to do in snapshots:
        assert "VersionId" not in get_obj_pre_versioned
        assert get_obj_non_versioned["VersionId"] == "null"
        assert list_obj_pre_versioned["Versions"][0]["VersionId"] == "null"
        assert get_obj_versioned["VersionId"] is not None
        assert list_obj_post_versioned["Versions"][0]["VersionId"] == "null"
        assert list_obj_post_versioned["Versions"][1]["VersionId"] is not None
        assert list_obj_post_versioned["Versions"][2]["VersionId"] is not None

    @markers.aws.validated
    @pytest.mark.skipif(reason="ACL behaviour is not implemented, see comments")
    def test_s3_batch_delete_objects_using_requests_with_acl(
        self, s3_bucket, allow_bucket_acl, snapshot, aws_client, anonymous_client
    ):
        # If an object is created in a public bucket by the owner, it can't be deleted by anonymous clients
        # https://docs.aws.amazon.com/AmazonS3/latest/userguide/acl-overview.html#specifying-grantee-predefined-groups
        # only "public" created objects can be deleted by anonymous clients
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key_1 = "key-created-by-owner"
        object_key_2 = "key-created-by-anonymous"

        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read-write")
        aws_client.s3.put_object(
            Bucket=s3_bucket, Key=object_key_1, Body="This body document", ACL="public-read-write"
        )
        anon = anonymous_client("s3")
        anon.put_object(
            Bucket=s3_bucket,
            Key=object_key_2,
            Body="This body document #2",
            ACL="public-read-write",
        )

        url = f"{_bucket_url(s3_bucket, localstack_host=get_localstack_host().host)}?delete"

        data = f"""
        <Delete xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
          <Object>
            <Key>{object_key_1}</Key>
          </Object>
          <Object>
            <Key>{object_key_2}</Key>
          </Object>
        </Delete>
        """

        md = hashlib.md5(data.encode("utf-8")).digest()
        contents_md5 = base64.b64encode(md).decode("utf-8")
        header = {"content-md5": contents_md5, "x-amz-request-payer": "requester"}
        r = requests.post(url=url, data=data, headers=header)

        assert 200 == r.status_code
        response = xmltodict.parse(r.content)
        response["DeleteResult"].pop("@xmlns", None)
        assert response["DeleteResult"]["Error"]["Key"] == object_key_1
        assert response["DeleteResult"]["Error"]["Code"] == "AccessDenied"
        assert response["DeleteResult"]["Deleted"]["Key"] == object_key_2
        snapshot.match("multi-delete-with-requests", response)

        response = aws_client.s3.list_objects(Bucket=s3_bucket)
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200
        assert len(response["Contents"]) == 1
        snapshot.match("list-remaining-objects", response)

    @markers.aws.validated
    def test_s3_batch_delete_public_objects_using_requests(
        self, s3_bucket, allow_bucket_acl, snapshot, aws_client, anonymous_client
    ):
        # only "public" created objects can be deleted by anonymous clients
        # https://docs.aws.amazon.com/AmazonS3/latest/userguide/acl-overview.html#specifying-grantee-predefined-groups
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key_1 = "key-created-by-anonymous-1"
        object_key_2 = "key-created-by-anonymous-2"

        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read-write")
        anon = anonymous_client("s3")
        anon.put_object(
            Bucket=s3_bucket, Key=object_key_1, Body="This body document", ACL="public-read-write"
        )
        anon.put_object(
            Bucket=s3_bucket,
            Key=object_key_2,
            Body="This body document #2",
            ACL="public-read-write",
        )

        # TODO delete does currently not work with S3_VIRTUAL_HOSTNAME
        url = f"{_bucket_url(s3_bucket, localstack_host=get_localstack_host().host)}?delete"

        data = f"""
            <Delete xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
              <Object>
                <Key>{object_key_1}</Key>
              </Object>
              <Object>
                <Key>{object_key_2}</Key>
              </Object>
            </Delete>
            """

        md = hashlib.md5(data.encode("utf-8")).digest()
        contents_md5 = base64.b64encode(md).decode("utf-8")
        header = {"content-md5": contents_md5, "x-amz-request-payer": "requester"}
        r = requests.post(url=url, data=data, headers=header)

        assert 200 == r.status_code
        response = xmltodict.parse(r.content)
        response["DeleteResult"]["Deleted"].sort(key=itemgetter("Key"))
        snapshot.match("multi-delete-with-requests", response)

        response = aws_client.s3.list_objects(Bucket=s3_bucket)
        snapshot.match("list-remaining-objects", response)

    @markers.aws.validated
    def test_s3_batch_delete_objects(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        snapshot.add_transformer(snapshot.transform.key_value("Key"))
        delete_object = []
        for _ in range(5):
            key_name = f"key-batch-delete-{short_uid()}"
            aws_client.s3.put_object(Bucket=s3_bucket, Key=key_name, Body="This body document")
            delete_object.append({"Key": key_name})

        response = aws_client.s3.delete_objects(Bucket=s3_bucket, Delete={"Objects": delete_object})
        snapshot.match("batch-delete", response)

        response = aws_client.s3.list_objects(Bucket=s3_bucket)
        snapshot.match("list-remaining-objects", response)

    @markers.aws.validated
    def test_s3_get_object_header_overrides(self, s3_bucket, snapshot, aws_client):
        # Signed requests may include certain header overrides in the querystring
        # https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html
        object_key = "key-header-overrides"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something")

        expiry_date = "Wed, 21 Oct 2015 07:28:00 GMT"
        response = aws_client.s3.get_object(
            Bucket=s3_bucket,
            Key=object_key,
            ResponseCacheControl="max-age=74",
            ResponseContentDisposition='attachment; filename="foo.jpg"',
            ResponseContentEncoding="identity",
            ResponseContentLanguage="de-DE",
            ResponseContentType="image/jpeg",
            ResponseExpires=expiry_date,
        )
        snapshot.match("get-object", response)

    @markers.aws.only_localstack
    def test_virtual_host_proxying_headers(self, s3_bucket, aws_client):
        # forwarding requests from virtual host to path addressed will double add server specific headers
        # (date and server). Verify that those are not double added after a fix to the proxy
        key = "test-double-headers"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body="test-headers", ACL="public-read")

        key_url = f"{_bucket_url(bucket_name=s3_bucket)}/{key}"
        response = requests.get(key_url)
        assert response.headers["server"]

        key_url = f"{_bucket_url_vhost(bucket_name=s3_bucket)}/{key}"
        proxied_response = requests.get(key_url)
        assert proxied_response.ok
        assert proxied_response.headers["server"] == response.headers["server"]
        assert len(proxied_response.headers["server"].split(",")) == 1
        assert len(proxied_response.headers["date"].split(",")) == 2  # coma in the date

    @pytest.mark.skipif(
        not in_default_partition(), reason="Test not applicable in non-default partitions"
    )
    @pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="KMS not enabled in S3 image")
    @markers.aws.validated
    def test_s3_sse_validate_kms_key(
        self,
        aws_client_factory,
        s3_create_bucket_with_client,
        kms_create_key,
        monkeypatch,
        snapshot,
    ):
        region_us_east_2 = "us-east-2"
        region_us_west_2 = "us-west-2"
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("Description"),
                snapshot.transform.regex(region_us_east_2, "<region_1>"),
                snapshot.transform.regex(region_us_west_2, "<region_2>"),
            ]
        )

        data = b"test-sse"
        bucket_name = f"bucket-test-kms-{short_uid()}"

        us_east_2_client = aws_client_factory(region_name=region_us_east_2).s3
        s3_create_bucket_with_client(
            us_east_2_client,
            Bucket=bucket_name,
            CreateBucketConfiguration={"LocationConstraint": region_us_east_2},
        )
        # create key in a different region than the bucket
        create_kms_key = kms_create_key(region_name=region_us_west_2)
        # snapshot the KMS key to save the UUID for replacement in Error message.
        snapshot.match("create-kms-key", create_kms_key)

        # test whether the validation is skipped when not disabling the validation
        if not is_aws_cloud():
            key_name = "test-sse-validate-kms-key-no-check"
            response = us_east_2_client.put_object(
                Bucket=bucket_name,
                Key=key_name,
                Body=data,
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId="fake-key-id",
            )
            assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

            response = us_east_2_client.create_multipart_upload(
                Bucket=bucket_name,
                Key="multipart-test-sse-validate-kms-key-no-check",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId="fake-key-id",
            )
            assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

            response = us_east_2_client.copy_object(
                Bucket=bucket_name,
                Key="copy-test-sse-validate-kms-key-no-check",
                CopySource={"Bucket": bucket_name, "Key": key_name},
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId="fake-key-id",
            )
            assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

        key_name = "test-sse-validate-kms-key"
        fake_key_uuid = "134f2428-cec1-4b25-a1ae-9048164dba47"

        # activating the validation, for AWS parity
        monkeypatch.setattr(config, "S3_SKIP_KMS_KEY_VALIDATION", False)
        with pytest.raises(ClientError) as e:
            us_east_2_client.put_object(
                Bucket=bucket_name,
                Key=key_name,
                Body=data,
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId="fake-key-id",
            )
        snapshot.match("put-obj-wrong-kms-key", e.value.response)

        with pytest.raises(ClientError) as e:
            us_east_2_client.put_object(
                Bucket=bucket_name,
                Key=key_name,
                Body=data,
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=fake_key_uuid,
            )
        snapshot.match("put-obj-wrong-kms-key-real-uuid", e.value.response)

        # we create a wrong arn but with the right region to test error message
        wrong_id_arn = (
            create_kms_key["Arn"]
            .replace(region_us_west_2, region_us_east_2)
            .replace(create_kms_key["KeyId"], fake_key_uuid)
        )
        with pytest.raises(ClientError) as e:
            us_east_2_client.put_object(
                Bucket=bucket_name,
                Key=key_name,
                Body=data,
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=wrong_id_arn,
            )
        snapshot.match("put-obj-wrong-kms-key-real-uuid-arn", e.value.response)

        with pytest.raises(ClientError) as e:
            us_east_2_client.put_object(
                Bucket=bucket_name,
                Key="test-sse-validate-kms-key-no-check-region",
                Body=data,
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=create_kms_key["Arn"],
            )
        snapshot.match("put-obj-different-region-kms-key", e.value.response)

        with pytest.raises(ClientError) as e:
            us_east_2_client.put_object(
                Bucket=bucket_name,
                Key="test-sse-validate-kms-key-different-region-no-arn",
                Body=data,
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=create_kms_key["KeyId"],
            )
        snapshot.match("put-obj-different-region-kms-key-no-arn", e.value.response)

        with pytest.raises(ClientError) as e:
            us_east_2_client.create_multipart_upload(
                Bucket=bucket_name,
                Key="multipart-test-sse-validate-kms-key-no-check",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId="fake-key-id",
            )
        snapshot.match("create-multipart-wrong-kms-key", e.value.response)

        # create a object to be copied
        src_key = "key-to-be-copied"
        us_east_2_client.put_object(Bucket=bucket_name, Key=src_key, Body=b"test-data")
        with pytest.raises(ClientError) as e:
            us_east_2_client.copy_object(
                Bucket=bucket_name,
                Key="copy-test-sse-validate-kms-key-no-check",
                CopySource={"Bucket": bucket_name, "Key": src_key},
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId="fake-key-id",
            )
        snapshot.match("copy-obj-wrong-kms-key", e.value.response)

    @pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="KMS not enabled in S3 image")
    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        paths=[
            "$..ETag",  # the ETag is different as we don't encrypt the object with the KMS key
        ]
    )
    def test_s3_sse_validate_kms_key_state(
        self, s3_bucket, kms_create_key, monkeypatch, snapshot, aws_client
    ):
        snapshot.add_transformer(snapshot.transform.key_value("Description"))
        data = b"test-sse"

        # create key in the same region as the bucket
        kms_key = kms_create_key()
        # snapshot the KMS key to save the UUID for replacement in Error message.
        snapshot.match("create-kms-key", kms_key)
        key_name = "put-object-with-sse"
        put_object_with_sse = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=key_name,
            Body=data,
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=kms_key["KeyId"],
        )
        snapshot.match("success-put-object-sse", put_object_with_sse)

        get_object_with_sse = aws_client.s3.get_object(
            Bucket=s3_bucket,
            Key=key_name,
        )
        snapshot.match("success-get-object-sse", get_object_with_sse)

        # disable the key
        aws_client.kms.disable_key(KeyId=kms_key["KeyId"])

        # test whether the validation is skipped when not disabling the validation
        if not is_aws_cloud():
            get_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_name)
            assert get_object["ResponseMetadata"]["HTTPStatusCode"] == 200

            response = aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key="test-sse-kms-disabled-key-no-check",
                Body=data,
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=kms_key["KeyId"],
            )
            assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

        # activating the validation, for AWS parity
        monkeypatch.setattr(config, "S3_SKIP_KMS_KEY_VALIDATION", False)

        # disable the key, try to put an object
        aws_client.kms.disable_key(KeyId=kms_key["KeyId"])

        def _is_key_disabled():
            key = aws_client.kms.describe_key(KeyId=kms_key["KeyId"])
            assert not key["KeyMetadata"]["Enabled"]

        retry(_is_key_disabled, retries=3, sleep=0.5)
        if is_aws_cloud():
            # time for the key state to be propagated
            time.sleep(5)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(
                Bucket=s3_bucket,
                Key=key_name,
            )
        snapshot.match("get-obj-disabled-key", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key="key-is-deactivated",
                Body=data,
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=kms_key["KeyId"],
            )
        snapshot.match("put-obj-disabled-key", e.value.response)

        # schedule the deletion of the key
        aws_client.kms.schedule_key_deletion(KeyId=kms_key["KeyId"], PendingWindowInDays=7)
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(
                Bucket=s3_bucket,
                Key=key_name,
            )
        snapshot.match("get-obj-pending-deletion-key", e.value.response)

    @markers.aws.validated
    def test_complete_multipart_parts_order(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("UploadId"),
            ]
        )

        key_name = "test-order-parts"
        response = aws_client.s3.create_multipart_upload(Bucket=s3_bucket, Key=key_name)
        upload_id = response["UploadId"]

        # data must be at least 5MiB
        part_data = "a" * (5_242_880 + 1)
        part_data = to_bytes(part_data)

        parts = 3
        multipart_upload_parts = []
        for part in range(parts):
            # Write contents to memory rather than a file.
            part_number = part + 1
            upload_file_object = BytesIO(part_data)
            response = aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name,
                Body=upload_file_object,
                PartNumber=part_number,
                UploadId=upload_id,
            )
            multipart_upload_parts.append({"ETag": response["ETag"], "PartNumber": part_number})

        with pytest.raises(ClientError) as e:
            aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name,
                Body=BytesIO(b""),
                PartNumber=-1,
                UploadId=upload_id,
            )
        snapshot.match("upload-part-negative-part-number", e.value.response)

        # testing completing the multipart with an unordered sequence of parts
        with pytest.raises(ClientError) as e:
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload={"Parts": list(reversed(multipart_upload_parts))},
                UploadId=upload_id,
            )
        snapshot.match("complete-multipart-unordered", e.value.response)

        response = aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload={"Parts": multipart_upload_parts},
            UploadId=upload_id,
        )
        snapshot.match("complete-multipart-ordered", response)

        # testing completing the multipart with a sequence of parts number going from 2, 4, and 6 (missing numbers)
        key_name_2 = "key-sequence-with-step-2"
        response = aws_client.s3.create_multipart_upload(Bucket=s3_bucket, Key=key_name_2)
        upload_id = response["UploadId"]

        multipart_upload_parts = []
        for part in range(parts):
            # Write contents to memory rather than a file.
            part_number = part + 2
            upload_file_object = BytesIO(part_data)
            response = aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name_2,
                Body=upload_file_object,
                PartNumber=part_number,
                UploadId=upload_id,
            )
            multipart_upload_parts.append({"ETag": response["ETag"], "PartNumber": part_number})

        response = aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name_2,
            MultipartUpload={"Parts": multipart_upload_parts},
            UploadId=upload_id,
        )
        snapshot.match("complete-multipart-with-step-2", response)

    @markers.aws.validated
    @pytest.mark.parametrize(
        "storage_class, is_retrievable",
        [
            (StorageClass.STANDARD, True),
            (StorageClass.STANDARD_IA, True),
            (StorageClass.GLACIER, False),
            (StorageClass.GLACIER_IR, True),
            (StorageClass.REDUCED_REDUNDANCY, True),
            (StorageClass.ONEZONE_IA, True),
            (StorageClass.INTELLIGENT_TIERING, True),
            (StorageClass.DEEP_ARCHIVE, False),
        ],
    )
    def test_put_object_storage_class(
        self, s3_bucket, snapshot, storage_class, is_retrievable, aws_client
    ):
        key_name = "test-put-object-storage-class"
        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=key_name,
            Body=b"body-test",
            StorageClass=storage_class,
        )

        response = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key_name,
            ObjectAttributes=["StorageClass"],
        )
        snapshot.match("get-object-storage-class", response)

        if is_retrievable:
            response = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_name)
            snapshot.match("get-object", response)
        else:
            with pytest.raises(ClientError) as e:
                aws_client.s3.get_object(Bucket=s3_bucket, Key=key_name)
            snapshot.match("get-object", e.value.response)

    @markers.aws.validated
    def test_put_object_storage_class_outposts(
        self, s3_bucket, s3_multipart_upload, snapshot, aws_client
    ):
        key_name = "test-put-object-storage-class"
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key=key_name,
                Body=b"body-test",
                StorageClass=StorageClass.OUTPOSTS,
            )
        snapshot.match("put-object-outposts", e.value.response)

        key_name = "test-multipart-storage-class"
        with pytest.raises(ClientError) as e:
            aws_client.s3.create_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                StorageClass=StorageClass.OUTPOSTS,
            )
        snapshot.match("create-multipart-outposts-exc", e.value.response)

    @markers.aws.validated
    def test_response_structure(self, aws_http_client_factory, s3_bucket, aws_client):
        """
        Test that the response structure is correct for the S3 API
        """
        aws_client.s3.put_object(Bucket=s3_bucket, Key="test", Body="test")
        headers = {"x-amz-content-sha256": "UNSIGNED-PAYLOAD"}

        s3_http_client = aws_http_client_factory("s3", signer_factory=SigV4Auth)

        def get_xml_content(http_response_content: bytes) -> bytes:
            # just format a bit the XML, nothing bad parity wise, but allow the test to run against AWS
            return http_response_content.replace(b"'", b'"').replace(b"utf", b"UTF")

        # Lists all buckets
        endpoint_url = _endpoint_url()
        resp = s3_http_client.get(endpoint_url, headers=headers)
        assert b'<?xml version="1.0" encoding="UTF-8"?>\n' in get_xml_content(resp.content)

        resp_dict = xmltodict.parse(resp.content)
        assert "ListAllMyBucketsResult" in resp_dict
        # validate that the Owner tag is first, before Buckets. This is because the Java SDK is counting on the order
        # to properly set the Owner value to the buckets.
        assert (
            resp_dict["ListAllMyBucketsResult"].pop("@xmlns")
            == "http://s3.amazonaws.com/doc/2006-03-01/"
        )
        list_buckets_tags = list(resp_dict["ListAllMyBucketsResult"].keys())
        assert list_buckets_tags[0] == "Owner"
        assert list_buckets_tags[1] == "Buckets"

        # Lists all objects in a bucket
        bucket_url = _bucket_url(s3_bucket)
        resp = s3_http_client.get(bucket_url, headers=headers)
        assert b'<?xml version="1.0" encoding="UTF-8"?>\n' in get_xml_content(resp.content)
        resp_dict = xmltodict.parse(resp.content)
        assert "ListBucketResult" in resp_dict
        assert resp_dict["ListBucketResult"]["@xmlns"] == "http://s3.amazonaws.com/doc/2006-03-01/"
        # validate that the Contents tag is last, after BucketName. Again for the Java SDK to properly set the
        # BucketName value to the objects.
        list_objects_tags = list(resp_dict["ListBucketResult"].keys())
        assert list_objects_tags.index("Name") < list_objects_tags.index("Contents")
        assert list_objects_tags[-1] == "Contents"

        # Lists all objects V2 in a bucket
        list_objects_v2_url = f"{bucket_url}?list-type=2"
        resp = s3_http_client.get(list_objects_v2_url, headers=headers)
        assert b'<?xml version="1.0" encoding="UTF-8"?>\n' in get_xml_content(resp.content)
        resp_dict = xmltodict.parse(resp.content)
        assert "ListBucketResult" in resp_dict
        assert resp_dict["ListBucketResult"]["@xmlns"] == "http://s3.amazonaws.com/doc/2006-03-01/"
        # same as ListObjects
        list_objects_v2_tags = list(resp_dict["ListBucketResult"].keys())
        assert list_objects_v2_tags.index("Name") < list_objects_v2_tags.index("Contents")
        assert list_objects_v2_tags[-1] == "Contents"

        # Lists all multipart uploads in a bucket
        list_multipart_uploads_url = f"{bucket_url}?uploads"
        resp = s3_http_client.get(list_multipart_uploads_url, headers=headers)
        assert b'<?xml version="1.0" encoding="UTF-8"?>\n' in get_xml_content(resp.content)
        resp_dict = xmltodict.parse(resp.content)
        assert "ListMultipartUploadsResult" in resp_dict
        assert (
            resp_dict["ListMultipartUploadsResult"]["@xmlns"]
            == "http://s3.amazonaws.com/doc/2006-03-01/"
        )

        # GetBucketLocation
        location_constraint_url = f"{bucket_url}?location"
        resp = s3_http_client.get(location_constraint_url, headers=headers)
        xml_content = get_xml_content(resp.content)
        assert b'<?xml version="1.0" encoding="UTF-8"?>\n' in xml_content
        assert b'<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"' in xml_content

        tagging = {"TagSet": [{"Key": "tag1", "Value": "tag1"}]}
        # put some tags on the bucket
        aws_client.s3.put_bucket_tagging(Bucket=s3_bucket, Tagging=tagging)

        # GetBucketTagging
        get_bucket_tagging_url = f"{bucket_url}?tagging"
        resp = s3_http_client.get(get_bucket_tagging_url, headers=headers)
        resp_dict = xmltodict.parse(resp.content)
        assert resp_dict["Tagging"]["TagSet"] == {"Tag": {"Key": "tag1", "Value": "tag1"}}
        assert resp_dict["Tagging"]["@xmlns"] == "http://s3.amazonaws.com/doc/2006-03-01/"

        # put an object to tests the next requests
        key_name = "test-key"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=key_name, Tagging="tag1=tag1")

        # Lists all objects versions in a bucket
        list_objects_version_url = f"{bucket_url}?versions"
        resp = s3_http_client.get(list_objects_version_url, headers=headers)
        assert b'<?xml version="1.0" encoding="UTF-8"?>\n' in get_xml_content(resp.content)
        resp_dict = xmltodict.parse(resp.content)
        assert "ListVersionsResult" in resp_dict
        assert (
            resp_dict["ListVersionsResult"]["@xmlns"] == "http://s3.amazonaws.com/doc/2006-03-01/"
        )
        # same as ListObjects
        list_objects_versions_tags = list(resp_dict["ListVersionsResult"].keys())
        assert list_objects_versions_tags.index("Name") < list_objects_versions_tags.index(
            "Version"
        )
        assert list_objects_versions_tags[-1] == "Version"

        # GetObjectTagging
        get_object_tagging_url = f"{bucket_url}/{key_name}?tagging"
        resp = s3_http_client.get(get_object_tagging_url, headers=headers)
        resp_dict = xmltodict.parse(resp.content)
        assert resp_dict["Tagging"]["TagSet"] == {"Tag": {"Key": "tag1", "Value": "tag1"}}
        assert resp_dict["Tagging"]["@xmlns"] == "http://s3.amazonaws.com/doc/2006-03-01/"

        # CopyObject
        get_object_tagging_url = f"{bucket_url}/{key_name}?tagging"
        resp = s3_http_client.get(get_object_tagging_url, headers=headers)
        resp_dict = xmltodict.parse(resp.content)
        assert resp_dict["Tagging"]["TagSet"] == {"Tag": {"Key": "tag1", "Value": "tag1"}}
        assert resp_dict["Tagging"]["@xmlns"] == "http://s3.amazonaws.com/doc/2006-03-01/"

        copy_object_url = f"{bucket_url}/copied-key"
        copy_object_headers = {**headers, "x-amz-copy-source": f"{bucket_url}/{key_name}"}
        resp = s3_http_client.put(copy_object_url, headers=copy_object_headers)
        resp_dict = xmltodict.parse(resp.content)
        assert "CopyObjectResult" in resp_dict
        assert resp_dict["CopyObjectResult"]["@xmlns"] == "http://s3.amazonaws.com/doc/2006-03-01/"
        assert resp.status_code == 200

        multipart_key = "multipart-key"
        create_multipart = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket, Key=multipart_key
        )
        upload_id = create_multipart["UploadId"]

        upload_part_url = f"{bucket_url}/{multipart_key}?UploadId={upload_id}&PartNumber=1"
        resp = s3_http_client.put(upload_part_url, headers=headers)
        assert not resp.content, resp.content
        assert resp.status_code == 200
        assert resp.headers.get("Content-Type") is None
        assert resp.headers["Content-Length"] == "0"

        # DeleteObjectTagging
        resp = s3_http_client.delete(get_object_tagging_url, headers=headers)
        assert not resp.content, resp.content
        assert resp.status_code == 204
        assert resp.headers.get("Content-Type") is None
        assert resp.headers.get("Content-Length") is None

    @markers.aws.validated
    def test_s3_timestamp_precision(self, s3_bucket, aws_client, aws_http_client_factory):
        object_key = "test-key"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="test-body")

        def assert_timestamp_is_iso8061_s3_format(_timestamp: str):
            # the timestamp should be looking like the following
            # 2023-11-15T12:02:40.000Z
            assert _timestamp.endswith(".000Z")
            assert len(_timestamp) == 24
            # assert that it follows the right format and it does not raise an exception during parsing
            parsed_ts = datetime.datetime.strptime(_timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
            assert parsed_ts.microsecond == 0

        s3_http_client = aws_http_client_factory("s3", signer_factory=SigV4Auth)
        list_buckets_endpoint = _endpoint_url()
        list_buckets_resp = s3_http_client.get(
            list_buckets_endpoint, headers={"x-amz-content-sha256": "UNSIGNED-PAYLOAD"}
        )
        list_buckets_dict = xmltodict.parse(list_buckets_resp.content)

        buckets = list_buckets_dict["ListAllMyBucketsResult"]["Buckets"]["Bucket"]
        # because of XML parsing, it can either be a list or a dict

        if isinstance(buckets, list):
            bucket = buckets[0]
        else:
            bucket = buckets
        bucket_timestamp: str = bucket["CreationDate"]
        assert_timestamp_is_iso8061_s3_format(bucket_timestamp)

        bucket_url = _bucket_url(s3_bucket)
        object_url = f"{bucket_url}/{object_key}"
        head_obj_resp = s3_http_client.head(
            object_url, headers={"x-amz-content-sha256": "UNSIGNED-PAYLOAD"}
        )
        last_modified: str = head_obj_resp.headers["Last-Modified"]
        assert datetime.datetime.strptime(last_modified, RFC1123)
        assert last_modified.endswith(" GMT")

        get_obj_resp = s3_http_client.get(
            object_url, headers={"x-amz-content-sha256": "UNSIGNED-PAYLOAD"}
        )
        last_modified: str = get_obj_resp.headers["Last-Modified"]
        assert datetime.datetime.strptime(last_modified, RFC1123)
        assert last_modified.endswith(" GMT")

        object_attrs_url = f"{object_url}?attributes"
        get_obj_attrs_resp = s3_http_client.get(
            object_attrs_url,
            headers={"x-amz-content-sha256": "UNSIGNED-PAYLOAD", "x-amz-object-attributes": "ETag"},
        )
        last_modified: str = get_obj_attrs_resp.headers["Last-Modified"]
        assert datetime.datetime.strptime(last_modified, RFC1123)
        assert last_modified.endswith(" GMT")

        copy_object_url = f"{bucket_url}/copied-key"
        copy_resp = s3_http_client.put(
            copy_object_url,
            headers={
                "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
                "x-amz-copy-source": f"{bucket_url}/{object_key}",
            },
        )
        copy_resp_dict = xmltodict.parse(copy_resp.content)
        copy_timestamp: str = copy_resp_dict["CopyObjectResult"]["LastModified"]
        assert_timestamp_is_iso8061_s3_format(copy_timestamp)

    # This test doesn't work against AWS anymore because of some authorization error.
    @markers.aws.only_localstack
    def test_s3_delete_objects_trailing_slash(self, aws_http_client_factory, s3_bucket, aws_client):
        object_key = "key-to-delete-trailing-slash"
        # create an object to delete
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body=b"123")

        s3_http_client = aws_http_client_factory("s3", signer_factory=SigV4Auth)

        # Endpoint as created by Rust and AWS JS SDK v3
        bucket_url = f"{_bucket_url(s3_bucket)}/?delete&x-id=DeleteObjects"

        delete_body = f"""<Delete>
            <Object>
                <Key>{object_key}</Key>
             </Object>
        </Delete>
        """
        # Post the request to delete the objects, with a trailing slash in the URL
        resp = s3_http_client.post(bucket_url, data=delete_body)
        assert resp.status_code == 200, (resp.content, resp.headers)

        resp_dict = xmltodict.parse(resp.content)
        assert "DeleteResult" in resp_dict
        assert resp_dict["DeleteResult"]["Deleted"]["Key"] == object_key

    @markers.aws.validated
    @pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="KMS not enabled in S3 image")
    # there is currently no server side encryption is place in LS, ETag will be different
    @markers.snapshot.skip_snapshot_verify(paths=["$..ETag"])
    def test_s3_multipart_upload_sse(
        self,
        aws_client,
        s3_bucket,
        s3_multipart_upload_with_snapshot,
        kms_create_key,
        snapshot,
    ):
        snapshot.add_transformer(
            [
                snapshot.transform.resource_name("SSEKMSKeyId"),
                snapshot.transform.key_value(
                    "Bucket", reference_replacement=False, value_replacement="<bucket>"
                ),
                snapshot.transform.key_value("UploadId"),
                snapshot.transform.key_value("Location"),
            ]
        )

        key_name = "test-sse-field-multipart"
        data = b"test-sse"
        key_id = kms_create_key()["KeyId"]
        # if you only pass the key id, the key must be in the same region and account as the bucket
        # otherwise, pass the ARN (always same region)
        # but the response always return the ARN

        s3_multipart_upload_with_snapshot(
            bucket=s3_bucket,
            key=key_name,
            data=data,
            snapshot_prefix="multi-sse",
            BucketKeyEnabled=True,
            SSEKMSKeyId=key_id,
            ServerSideEncryption="aws:kms",
        )

        response = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_name)
        snapshot.match("get-obj", response)

    @pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="KMS not enabled in S3 image")
    @markers.aws.validated
    # there is currently no server side encryption is place in LS, ETag will be different
    @markers.snapshot.skip_snapshot_verify(paths=["$..ETag"])
    def test_s3_sse_bucket_key_default(
        self,
        aws_client,
        s3_bucket,
        kms_create_key,
        snapshot,
    ):
        snapshot.add_transformer(
            [
                snapshot.transform.resource_name("SSEKMSKeyId"),
                snapshot.transform.key_value(
                    "Bucket", reference_replacement=False, value_replacement="<bucket>"
                ),
                snapshot.transform.key_value("Location"),
            ]
        )
        key_before_set = "test-sse-bucket-before"
        key_after_set = "test-sse-bucket-after"
        data = b"test-sse"
        key_id = kms_create_key()["KeyId"]
        response = aws_client.s3.put_object(Bucket=s3_bucket, Key=key_before_set, Body=data)
        snapshot.match("put-obj-default-before-setting", response)

        response = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_before_set)
        snapshot.match("get-obj-default-before-setting", response)

        response = aws_client.s3.put_bucket_encryption(
            Bucket=s3_bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "aws:kms",
                            "KMSMasterKeyID": key_id,
                        },
                        "BucketKeyEnabled": True,
                    }
                ]
            },
        )
        snapshot.match("put-bucket-encryption", response)

        response = aws_client.s3.get_bucket_encryption(Bucket=s3_bucket)
        snapshot.match("get-bucket-encryption", response)

        # verify that setting BucketKeyEnabled didn't affect existing keys
        response = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_before_set)
        snapshot.match("get-obj-default-after-setting", response)

        # set a new key and see the configuration is in effect
        response = aws_client.s3.put_object(Bucket=s3_bucket, Key=key_after_set, Body=data)
        snapshot.match("put-obj-after-setting", response)

        response = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_after_set)
        snapshot.match("get-obj-after-setting", response)

    @pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="KMS not enabled in S3 image")
    @markers.aws.validated
    @pytest.mark.skip(
        reason="Behaviour not implemented yet: https://github.com/localstack/localstack/issues/6882"
    )
    # there is currently no server side encryption is place in LS, ETag will be different
    @markers.snapshot.skip_snapshot_verify(paths=["$..ETag"])
    def test_s3_sse_default_kms_key(
        self,
        aws_client,
        s3_create_bucket,
        snapshot,
    ):
        snapshot.add_transformer(
            [
                snapshot.transform.resource_name("SSEKMSKeyId"),
                snapshot.transform.key_value(
                    "Bucket", reference_replacement=False, value_replacement="<bucket>"
                ),
                snapshot.transform.key_value("UploadId"),
                snapshot.transform.key_value("Location"),
            ]
        )
        bucket_1 = s3_create_bucket()
        bucket_2 = s3_create_bucket()
        key_name = "test-sse-default-key"
        data = b"test-sse"
        response = aws_client.s3.put_object(
            Bucket=bucket_1, Key=key_name, Body=data, ServerSideEncryption="aws:kms"
        )
        snapshot.match("put-obj-default-kms-s3-key", response)

        response = aws_client.s3.get_object(Bucket=bucket_1, Key=key_name)
        snapshot.match("get-obj-default-kms-s3-key", response)

        # validate that the AWS managed key is the same between buckets
        response = aws_client.s3.put_object(
            Bucket=bucket_2, Key=key_name, Body=data, ServerSideEncryption="aws:kms"
        )
        snapshot.match("put-obj-default-kms-s3-key-bucket-2", response)

        response = aws_client.s3.get_object(Bucket=bucket_2, Key=key_name)
        snapshot.match("get-obj-default-kms-s3-key-bucket-2", response)

        response = aws_client.s3.put_bucket_encryption(
            Bucket=bucket_1,
            ServerSideEncryptionConfiguration={
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "aws:kms",
                        },
                        "BucketKeyEnabled": True,
                    }
                ]
            },
        )
        snapshot.match("put-bucket-encryption-default-kms-s3-key", response)

        response = aws_client.s3.get_bucket_encryption(Bucket=bucket_1)
        snapshot.match("get-bucket-encryption-default-kms-s3-key", response)

        key_name = "test-sse-default-key-from-bucket"
        response = aws_client.s3.put_object(
            Bucket=bucket_1, Key=key_name, Body=data, ServerSideEncryption="aws:kms"
        )
        snapshot.match("put-obj-default-kms-s3-key-from-bucket", response)

        response = aws_client.s3.get_object(Bucket=bucket_1, Key=key_name)
        snapshot.match("get-obj-default-kms-s3-key-from-bucket", response)

    @markers.aws.validated
    def test_s3_analytics_configurations(self, aws_client, s3_create_bucket, snapshot):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value(
                    "Bucket", reference_replacement=False, value_replacement="<bucket>"
                ),
            ]
        )

        bucket = s3_create_bucket()
        analytics_bucket = s3_create_bucket()
        analytics_bucket_arn = f"arn:aws:s3:::{analytics_bucket}"

        storage_analysis = {
            "Id": "config_with_storage_analysis_1",
            "Filter": {
                "Prefix": "test_ls",
            },
            "StorageClassAnalysis": {
                "DataExport": {
                    "OutputSchemaVersion": "V_1",
                    "Destination": {
                        "S3BucketDestination": {
                            "Format": "CSV",
                            "Bucket": analytics_bucket_arn,
                            "Prefix": "test",
                        }
                    },
                }
            },
        }
        # id in storage analysis is different from the one in the request
        with pytest.raises(ClientError) as err_put:
            aws_client.s3.put_bucket_analytics_configuration(
                Bucket=bucket,
                Id="different-id",
                AnalyticsConfiguration=storage_analysis,
            )
        snapshot.match("put_config_with_storage_analysis_err", err_put.value.response)

        # non-existing storage analysis get
        with pytest.raises(ClientError) as err_get:
            aws_client.s3.get_bucket_analytics_configuration(
                Bucket=bucket,
                Id="non-existing",
            )
        snapshot.match("get_config_with_storage_analysis_err", err_get.value.response)

        # non-existing storage analysis delete
        with pytest.raises(ClientError) as err_delete:
            aws_client.s3.delete_bucket_analytics_configuration(
                Bucket=bucket,
                Id=storage_analysis["Id"],
            )
        snapshot.match("delete_config_with_storage_analysis_err", err_delete.value.response)

        # put storage analysis
        response = aws_client.s3.put_bucket_analytics_configuration(
            Bucket=bucket,
            Id=storage_analysis["Id"],
            AnalyticsConfiguration=storage_analysis,
        )
        snapshot.match("put_config_with_storage_analysis_1", response)

        response = aws_client.s3.get_bucket_analytics_configuration(
            Bucket=bucket,
            Id=storage_analysis["Id"],
        )
        snapshot.match("get_config_with_storage_analysis_1", response)

        # update storage analysis
        storage_analysis["Filter"]["Prefix"] = "test_ls_2"
        aws_client.s3.put_bucket_analytics_configuration(
            Bucket=bucket,
            Id=storage_analysis["Id"],
            AnalyticsConfiguration=storage_analysis,
        )
        response = aws_client.s3.get_bucket_analytics_configuration(
            Bucket=bucket,
            Id=storage_analysis["Id"],
        )
        snapshot.match("get_config_with_storage_analysis_2", response)

        # add a new storage analysis
        storage_analysis["Id"] = "config_with_storage_analysis_2"
        storage_analysis["Filter"]["Prefix"] = "test_ls_3"
        aws_client.s3.put_bucket_analytics_configuration(
            Bucket=bucket, Id=storage_analysis["Id"], AnalyticsConfiguration=storage_analysis
        )
        response = aws_client.s3.get_bucket_analytics_configuration(
            Bucket=bucket,
            Id=storage_analysis["Id"],
        )
        snapshot.match("get_config_with_storage_analysis_3", response)

        response = aws_client.s3.list_bucket_analytics_configurations(Bucket=bucket)
        snapshot.match("list_config_with_storage_analysis_1", response)

        # delete storage analysis
        aws_client.s3.delete_bucket_analytics_configuration(
            Bucket=bucket,
            Id=storage_analysis["Id"],
        )
        response = aws_client.s3.list_bucket_analytics_configurations(Bucket=bucket)
        snapshot.match("list_config_with_storage_analysis_2", response)

    @markers.aws.validated
    def test_s3_intelligent_tier_config(self, aws_client, s3_bucket, snapshot):
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))
        intelligent_tier_configuration = {
            "Id": "test1",
            "Filter": {
                "Prefix": "test1",
            },
            "Status": "Enabled",
            "Tierings": [
                {"Days": 90, "AccessTier": "ARCHIVE_ACCESS"},
            ],
        }

        # different id in tiering config and in put request
        with pytest.raises(ClientError) as put_err_1:
            aws_client.s3.put_bucket_intelligent_tiering_configuration(
                Bucket=s3_bucket,
                Id="incorrect_id",
                IntelligentTieringConfiguration=intelligent_tier_configuration,
            )
        snapshot.match(
            "put_bucket_intelligent_tiering_configuration_err_1`", put_err_1.value.response
        )

        # put tiering config
        response = aws_client.s3.put_bucket_intelligent_tiering_configuration(
            Bucket=s3_bucket,
            Id=intelligent_tier_configuration["Id"],
            IntelligentTieringConfiguration=intelligent_tier_configuration,
        )
        snapshot.match("put_bucket_intelligent_tiering_configuration_1", response)

        # get tiering config and snapshot match
        response = aws_client.s3.get_bucket_intelligent_tiering_configuration(
            Bucket=s3_bucket,
            Id=intelligent_tier_configuration["Id"],
        )
        snapshot.match("get_bucket_intelligent_tiering_configuration_1", response)

        # put tiering config with different id
        intelligent_tier_configuration["Id"] = "test2"
        intelligent_tier_configuration["Filter"]["Prefix"] = "test2"

        aws_client.s3.put_bucket_intelligent_tiering_configuration(
            Bucket=s3_bucket,
            Id=intelligent_tier_configuration["Id"],
            IntelligentTieringConfiguration=intelligent_tier_configuration,
        )

        response = aws_client.s3.list_bucket_intelligent_tiering_configurations(Bucket=s3_bucket)
        snapshot.match("list_bucket_intelligent_tiering_configurations_1", response)

        # update the config by adding config with same id
        intelligent_tier_configuration["Id"] = "test1"
        intelligent_tier_configuration["Filter"]["Prefix"] = "testupdate"

        aws_client.s3.put_bucket_intelligent_tiering_configuration(
            Bucket=s3_bucket,
            Id=intelligent_tier_configuration["Id"],
            IntelligentTieringConfiguration=intelligent_tier_configuration,
        )

        response = aws_client.s3.list_bucket_intelligent_tiering_configurations(Bucket=s3_bucket)
        snapshot.match("list_bucket_intelligent_tiering_configurations_2", response)

        # delete the config with non-existing bucket
        with pytest.raises(ClientError) as delete_err_1:
            aws_client.s3.delete_bucket_intelligent_tiering_configuration(
                Bucket=f"non-existing-bucket-{short_uid()}-{short_uid()}",
                Id=intelligent_tier_configuration["Id"],
            )
        snapshot.match(
            "delete_bucket_intelligent_tiering_configuration_err_1", delete_err_1.value.response
        )

        # delete the config with non-existing id
        with pytest.raises(ClientError) as delete_err_2:
            aws_client.s3.delete_bucket_intelligent_tiering_configuration(
                Bucket=s3_bucket,
                Id="non-existing-id",
            )
        snapshot.match(
            "delete_bucket_intelligent_tiering_configuration_err_2", delete_err_2.value.response
        )

        # delete the config
        aws_client.s3.delete_bucket_intelligent_tiering_configuration(
            Bucket=s3_bucket,
            Id=intelligent_tier_configuration["Id"],
        )

        response = aws_client.s3.list_bucket_intelligent_tiering_configurations(Bucket=s3_bucket)
        snapshot.match("list_bucket_intelligent_tiering_configurations_3", response)

    @markers.aws.validated
    def test_s3_get_object_headers(self, aws_client, s3_bucket, snapshot):
        key = "en-gb.wav"
        file_path = os.path.join(os.path.dirname(__file__), f"../../files/{key}")

        aws_client.s3.upload_file(file_path, s3_bucket, key)
        objects = aws_client.s3.list_objects(Bucket=s3_bucket)
        etag = objects["Contents"][0]["ETag"]

        # TODO: some of the headers missing in the get object response
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=s3_bucket, Key=key, IfNoneMatch=etag)
        snapshot.match("if_none_match_err_1", e.value.response["Error"])

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=s3_bucket, Key=key, IfNoneMatch=etag.strip('"'))
        snapshot.match("if_none_match_err_2", e.value.response["Error"])

        response = aws_client.s3.get_object(Bucket=s3_bucket, Key=key, IfNoneMatch="etag")
        snapshot.match("if_none_match_1", response["ResponseMetadata"]["HTTPStatusCode"])

        response = aws_client.s3.get_object(Bucket=s3_bucket, Key=key, IfMatch=etag)
        snapshot.match("if_match_1", response["ResponseMetadata"]["HTTPStatusCode"])

        response = aws_client.s3.get_object(Bucket=s3_bucket, Key=key, IfMatch=etag.strip('"'))
        snapshot.match("if_match_2", response["ResponseMetadata"]["HTTPStatusCode"])

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=s3_bucket, Key=key, IfMatch="etag")
        snapshot.match("if_match_err_1", e.value.response["Error"])

    @markers.aws.validated
    def test_s3_inventory_report_crud(self, aws_client, s3_create_bucket, snapshot, region_name):
        snapshot.add_transformer(snapshot.transform.resource_name())
        src_bucket = s3_create_bucket()
        dest_bucket = s3_create_bucket()

        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "InventoryPolicy",
                    "Effect": "Allow",
                    "Principal": {"Service": "s3.amazonaws.com"},
                    "Action": "s3:PutObject",
                    "Resource": [f"arn:{get_partition(region_name)}:s3:::{dest_bucket}/*"],
                    "Condition": {
                        "ArnLike": {"aws:SourceArn": f"arn:aws:s3:::{src_bucket}"},
                    },
                },
            ],
        }

        aws_client.s3.put_bucket_policy(Bucket=dest_bucket, Policy=json.dumps(policy))
        inventory_config = {
            "Id": "test-inventory",
            "Destination": {
                "S3BucketDestination": {
                    "Bucket": f"arn:{get_partition(region_name)}:s3:::{dest_bucket}",
                    "Format": "CSV",
                }
            },
            "IsEnabled": True,
            "IncludedObjectVersions": "All",
            "OptionalFields": ["Size", "ETag"],
            "Schedule": {"Frequency": "Daily"},
        }

        put_inv_config = aws_client.s3.put_bucket_inventory_configuration(
            Bucket=src_bucket,
            Id=inventory_config["Id"],
            InventoryConfiguration=inventory_config,
        )
        snapshot.match("put-inventory-config", put_inv_config)

        list_inv_configs = aws_client.s3.list_bucket_inventory_configurations(Bucket=src_bucket)
        snapshot.match("list-inventory-config", list_inv_configs)

        get_inv_config = aws_client.s3.get_bucket_inventory_configuration(
            Bucket=src_bucket,
            Id=inventory_config["Id"],
        )
        snapshot.match("get-inventory-config", get_inv_config)

        del_inv_config = aws_client.s3.delete_bucket_inventory_configuration(
            Bucket=src_bucket,
            Id=inventory_config["Id"],
        )
        snapshot.match("del-inventory-config", del_inv_config)

        list_inv_configs_after_del = aws_client.s3.list_bucket_inventory_configurations(
            Bucket=src_bucket
        )
        snapshot.match("list-inventory-config-after-del", list_inv_configs_after_del)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_inventory_configuration(
                Bucket=src_bucket,
                Id=inventory_config["Id"],
            )
        snapshot.match("get-nonexistent-inv-config", e.value.response)

    @markers.aws.validated
    def test_s3_put_inventory_report_exceptions(self, aws_client, s3_create_bucket, snapshot):
        snapshot.add_transformer(snapshot.transform.resource_name())
        src_bucket = s3_create_bucket()
        dest_bucket = s3_create_bucket()
        config_id = "test-inventory"

        def _get_config():
            return {
                "Id": config_id,
                "Destination": {
                    "S3BucketDestination": {
                        "Bucket": f"arn:aws:s3:::{dest_bucket}",
                        "Format": "CSV",
                    }
                },
                "IsEnabled": True,
                "IncludedObjectVersions": "All",
                "Schedule": {"Frequency": "Daily"},
            }

        def _put_bucket_inventory_configuration(inventory_configuration):
            aws_client.s3.put_bucket_inventory_configuration(
                Bucket=src_bucket,
                Id=config_id,
                InventoryConfiguration=inventory_configuration,
            )

        # put an inventory config with a wrong ID
        with pytest.raises(ClientError) as e:
            inv_config = _get_config()
            inv_config["Id"] = config_id + "wrong"
            _put_bucket_inventory_configuration(inv_config)
        snapshot.match("wrong-id", e.value.response)

        # set the Destination Bucket only as the name and not the ARN
        with pytest.raises(ClientError) as e:
            inv_config = _get_config()
            inv_config["Destination"]["S3BucketDestination"]["Bucket"] = dest_bucket
            _put_bucket_inventory_configuration(inv_config)
        snapshot.match("wrong-destination-arn", e.value.response)

        # set the wrong Destination Format (should be CSV/ORC/Parquet)
        with pytest.raises(ClientError) as e:
            inv_config = _get_config()
            inv_config["Destination"]["S3BucketDestination"]["Format"] = "WRONG-FORMAT"
            _put_bucket_inventory_configuration(inv_config)
        snapshot.match("wrong-destination-format", e.value.response)

        # set the wrong Schedule Frequency (should be Daily/Weekly)
        with pytest.raises(ClientError) as e:
            inv_config = _get_config()
            inv_config["Schedule"]["Frequency"] = "Hourly"
            _put_bucket_inventory_configuration(inv_config)
        snapshot.match("wrong-schedule-frequency", e.value.response)

        # set the wrong IncludedObjectVersions (should be All/Current)
        with pytest.raises(ClientError) as e:
            inv_config = _get_config()
            inv_config["IncludedObjectVersions"] = "Wrong"
            _put_bucket_inventory_configuration(inv_config)
        snapshot.match("wrong-object-versions", e.value.response)

        # set wrong OptionalFields
        with pytest.raises(ClientError) as e:
            inv_config = _get_config()
            inv_config["OptionalFields"] = ["TestField"]
            _put_bucket_inventory_configuration(inv_config)
        snapshot.match("wrong-optional-field", e.value.response)

    @markers.aws.validated
    def test_put_bucket_inventory_config_order(
        self, aws_client, s3_create_bucket, snapshot, region_name
    ):
        snapshot.add_transformer(snapshot.transform.resource_name())
        src_bucket = s3_create_bucket()
        dest_bucket = s3_create_bucket()

        def _put_bucket_inventory_configuration(config_id: str):
            inventory_configuration = {
                "Id": config_id,
                "Destination": {
                    "S3BucketDestination": {
                        "Bucket": f"arn:{get_partition(region_name)}:s3:::{dest_bucket}",
                        "Format": "CSV",
                    }
                },
                "IsEnabled": True,
                "IncludedObjectVersions": "All",
                "Schedule": {"Frequency": "Daily"},
            }
            aws_client.s3.put_bucket_inventory_configuration(
                Bucket=src_bucket,
                Id=config_id,
                InventoryConfiguration=inventory_configuration,
            )

        for inv_config_id in ("test-1", "z-test", "a-test"):
            _put_bucket_inventory_configuration(inv_config_id)

        list_inv_configs = aws_client.s3.list_bucket_inventory_configurations(Bucket=src_bucket)
        snapshot.match("list-inventory-config", list_inv_configs)

        del_inv_config = aws_client.s3.delete_bucket_inventory_configuration(
            Bucket=src_bucket,
            Id="z-test",
        )
        snapshot.match("del-inventory-config", del_inv_config)

        list_inv_configs = aws_client.s3.list_bucket_inventory_configurations(Bucket=src_bucket)
        snapshot.match("list-inventory-config-after-del", list_inv_configs)

    @pytest.mark.parametrize(
        "use_virtual_address",
        [True, False],
    )
    @markers.aws.validated
    def test_get_object_content_length_with_virtual_host(
        self,
        s3_bucket,
        use_virtual_address,
        snapshot,
        aws_client,
        aws_client_factory,
    ):
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("x-amz-request-id"),
                snapshot.transform.key_value("x-amz-id-2"),
                snapshot.transform.key_value("last-modified", reference_replacement=False),
                snapshot.transform.key_value("date", reference_replacement=False),
                snapshot.transform.key_value("server"),
            ]
        )
        object_key = "temp.txt"
        aws_client.s3.put_object(Key=object_key, Bucket=s3_bucket, Body="123")

        s3_config = {"addressing_style": "virtual"} if use_virtual_address else {}
        client = aws_client_factory(
            config=Config(s3=s3_config),
            endpoint_url=_endpoint_url(),
        ).s3

        url = _generate_presigned_url(client, {"Bucket": s3_bucket, "Key": object_key}, expires=10)
        response = requests.get(url)
        assert response.ok
        lowercase_headers = {k.lower(): v for k, v in response.headers.items()}
        snapshot.match("get-obj-content-len-headers", lowercase_headers)

    @markers.aws.validated
    def test_empty_bucket_fixture(self, s3_bucket, s3_empty_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("Name"))
        for i in range(3):
            aws_client.s3.put_object(Bucket=s3_bucket, Key=f"key{i}", Body="123")

        response = aws_client.s3.list_objects_v2(Bucket=s3_bucket)
        snapshot.match("list-obj", response)

        s3_empty_bucket(s3_bucket)

        response = aws_client.s3.list_objects_v2(Bucket=s3_bucket)
        snapshot.match("list-obj-after-empty", response)

    @markers.aws.only_localstack
    def test_s3_raw_request_routing(self, s3_bucket, aws_client):
        """
        When sending a PutObject request to S3 with a very raw request not having any indication that the request is
        directed to S3 (no signing, no specific S3 endpoint) and encoded as a form, the request will go through the
        ServiceNameParser handler.
        This parser will try to parse the form data (which in our case is binary data), and will fail with a decoding
        error. It also consumes the stream, and leaves S3 with no data to save.
        This test verifies that this scenario works by skipping the service name thanks to the early S3 CORS handler.
        """
        default_endpoint = f"http://{get_localstack_host().host_and_port()}"
        object_key = "test-routing-key"
        key_url = f"{default_endpoint}/{s3_bucket}/{object_key}"
        data = os.urandom(445529)
        resp = requests.put(
            key_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        assert resp.ok

        get_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        assert get_object["Body"].read() == data

        fake_key_url = f"{default_endpoint}/fake-bucket-{short_uid()}/{object_key}"
        resp = requests.put(
            fake_key_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        assert b"NoSuchBucket" in resp.content


class TestS3MultiAccounts:
    @pytest.fixture
    def primary_client(self, aws_client):
        return aws_client.s3

    @pytest.fixture
    def secondary_client(self, secondary_aws_client):
        """
        Create a boto client with secondary test credentials and region.
        """
        return secondary_aws_client.s3

    @markers.aws.only_localstack
    def test_shared_bucket_namespace(self, primary_client, secondary_client, cleanups):
        bucket_name = short_uid()

        # Ensure that the bucket name space is shared by all accounts and regions
        create_s3_bucket(bucket_name=bucket_name, s3_client=primary_client)
        cleanups.append(lambda: primary_client.delete_bucket(Bucket=bucket_name))

        with pytest.raises(ClientError) as exc:
            create_s3_bucket(bucket_name=bucket_name, s3_client=secondary_client)
        exc.match("BucketAlreadyExists")

    @markers.aws.only_localstack
    def test_cross_account_access(
        self, primary_client, secondary_client, cleanups, s3_empty_bucket
    ):
        # Ensure that following operations can be performed across accounts
        # - ListObjects
        # - PutObject
        # - GetObject

        bucket_name = short_uid()
        key_name = "lorem/ipsum"
        body1 = b"zaphod beeblebrox"
        body2 = b"42"

        # First user creates a bucket and puts an object
        create_s3_bucket(bucket_name=bucket_name, s3_client=primary_client)
        cleanups.append(lambda: primary_client.delete_bucket(Bucket=bucket_name))
        cleanups.append(lambda: s3_empty_bucket(bucket_name))

        response = primary_client.list_buckets()
        assert bucket_name in [bucket["Name"] for bucket in response["Buckets"]]
        primary_client.put_object(Bucket=bucket_name, Key=key_name, Body=body1)

        # Second user must not see this bucket in their `ListBuckets` response
        response = secondary_client.list_buckets()
        assert bucket_name not in [bucket["Name"] for bucket in response["Buckets"]]

        # Yet they should be able to `ListObjects` in that bucket
        response = secondary_client.list_objects(Bucket=bucket_name)
        assert key_name in [key["Key"] for key in response["Contents"]]

        # Along with `GetObject` and `PutObject`
        # ACL and permission enforcement is currently not implemented
        response = secondary_client.get_object(Bucket=bucket_name, Key=key_name)
        assert response["Body"].read() == body1
        assert secondary_client.put_object(Bucket=bucket_name, Key=key_name, Body=body2)

        # The modified object must be reflected for the first user
        response = primary_client.get_object(Bucket=bucket_name, Key=key_name)
        assert response["Body"].read() == body2

    @markers.aws.only_localstack
    def test_cross_account_copy_object(
        self, primary_client, secondary_client, cleanups, s3_empty_bucket
    ):
        bucket_name = short_uid()
        key_name = "lorem/ipsum"
        key_name_copy = "lorem/ipsum2"
        body1 = b"zaphod beeblebrox"

        # First user creates a bucket and puts an object
        create_s3_bucket(bucket_name=bucket_name, s3_client=primary_client)
        cleanups.append(lambda: primary_client.delete_bucket(Bucket=bucket_name))
        cleanups.append(lambda: s3_empty_bucket(bucket_name))

        primary_client.put_object(Bucket=bucket_name, Key=key_name, Body=body1)

        # Assert that the secondary client can copy an object in the other account bucket
        response = secondary_client.copy_object(
            Bucket=bucket_name, Key=key_name_copy, CopySource=f"{bucket_name}/{key_name}"
        )

        # Yet they should be able to `ListObjects` in that bucket
        response = secondary_client.list_objects(Bucket=bucket_name)
        bucket_keys = {key["Key"] for key in response["Contents"]}
        assert key_name in bucket_keys
        assert key_name_copy in bucket_keys


class TestS3TerraformRawRequests:
    @markers.aws.only_localstack
    def test_terraform_request_sequence(self, aws_client):
        reqs = load_file(
            os.path.join(
                os.path.dirname(__file__),
                "../../files/s3.requests.txt",
            )
        )
        reqs = reqs.split("---")

        for req in reqs:
            header, _, body = req.strip().partition("\n\n")
            req, _, headers = header.strip().partition("\n")
            headers = {h.split(":")[0]: h.partition(":")[2].strip() for h in headers.split("\n")}
            method, path, _ = req.split(" ")
            url = f"{config.internal_service_url()}{path}"
            result = requests.request(method=method, url=url, data=body, headers=headers)
            assert result.status_code < 400


class TestS3PresignedUrl:
    """
    These tests pertain to S3's presigned URL feature.
    """

    # # Note: This test may have side effects (via `s3_client.meta.events.register(..)`) and
    # # may not be suitable for parallel execution
    @markers.aws.validated
    def test_presign_with_additional_query_params(
        self, s3_bucket, patch_s3_skip_signature_validation_false, aws_client
    ):
        """related to issue: https://github.com/localstack/localstack/issues/4133"""

        def add_query_param(request, **kwargs):
            request.url += "?requestedBy=abcDEF123"

        aws_client.s3.put_object(Body="test-value", Bucket=s3_bucket, Key="test")
        s3_presigned_client = _s3_client_pre_signed_client(
            Config(signature_version="s3v4"),
            endpoint_url=_endpoint_url(),
        )
        s3_presigned_client.meta.events.register("before-sign.s3.GetObject", add_query_param)
        try:
            presign_url = s3_presigned_client.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": s3_bucket, "Key": "test"},
                ExpiresIn=86400,
            )
            assert "requestedBy=abcDEF123" in presign_url
            response = requests.get(presign_url)
            assert b"test-value" == response._content
        finally:
            s3_presigned_client.meta.events.unregister("before-sign.s3.GetObject", add_query_param)

    @markers.aws.only_localstack
    def test_presign_check_signature_validation_for_port_permutation(
        self, s3_bucket, patch_s3_skip_signature_validation_false, aws_client
    ):
        host = f"{S3_VIRTUAL_HOSTNAME}:{config.GATEWAY_LISTEN[0].port}"
        s3_presign = _s3_client_pre_signed_client(
            Config(signature_version="s3v4"),
            endpoint_url=f"http://{host}",
        )

        aws_client.s3.put_object(Body="test-value", Bucket=s3_bucket, Key="test")

        presign_url = s3_presign.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": s3_bucket, "Key": "test"},
            ExpiresIn=86400,
        )
        assert f":{config.GATEWAY_LISTEN[0].port}" in presign_url

        host_443 = host.replace(f":{config.GATEWAY_LISTEN[0].port}", ":443")
        response = requests.get(presign_url, headers={"host": host_443})
        assert b"test-value" == response._content

        host_no_port = host_443.replace(":443", "")
        response = requests.get(presign_url, headers={"host": host_no_port})
        assert b"test-value" == response._content

    @markers.aws.validated
    def test_put_object(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())

        key = "my-key"

        url = aws_client.s3.generate_presigned_url(
            "put_object", Params={"Bucket": s3_bucket, "Key": key}
        )
        requests.put(url, data="something", verify=False)

        response = aws_client.s3.get_object(Bucket=s3_bucket, Key=key)
        assert response["Body"].read() == b"something"
        snapshot.match("get_object", response)

    @markers.aws.only_localstack
    def test_get_request_expires_ignored_if_validation_disabled(
        self, s3_bucket, monkeypatch, patch_s3_skip_signature_validation_false, aws_client
    ):
        aws_client.s3.put_object(Body="test-value", Bucket=s3_bucket, Key="test")

        presigned_request = aws_client.s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": s3_bucket, "Key": "test"},
            ExpiresIn=2,
        )
        # sleep so it expires
        time.sleep(3)

        # attempt to use the presigned request
        response = requests.get(presigned_request)
        # response should not be successful as it is expired -> signature will not match
        # "SignatureDoesNotMatch" in str(response.content)
        assert response.status_code in [400, 403]

        # set skip signature validation to True -> the request should now work
        monkeypatch.setattr(config, "S3_SKIP_SIGNATURE_VALIDATION", True)
        response = requests.get(presigned_request)
        assert response.status_code == 200
        assert b"test-value" == response.content

    @markers.aws.validated
    def test_delete_has_empty_content_length_header(self, s3_bucket, aws_client):
        for encoding in None, "gzip":
            # put object
            object_key = "key-by-hostname"
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key=object_key,
                Body="something",
                ContentType="text/html; charset=utf-8",
            )
            url = aws_client.s3.generate_presigned_url(
                "delete_object", Params={"Bucket": s3_bucket, "Key": object_key}
            )

            # get object and assert headers
            headers = {}
            if encoding:
                headers["Accept-Encoding"] = encoding
            response = requests.delete(url, headers=headers, verify=False)
            assert not response.content
            assert response.status_code == 204
            assert response.headers.get("x-amz-id-2") is not None
            # AWS does not return a Content-Type when the body is empty and it returns 204
            assert response.headers.get("content-type") is None
            # AWS does not send a content-length header at all
            assert response.headers.get("content-length") is None

    @markers.aws.validated
    def test_head_has_correct_content_length_header(self, s3_bucket, aws_client):
        body = "something body \n \n\r"
        # put object
        object_key = "key-by-hostname"
        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body=body,
            ContentType="text/html; charset=utf-8",
        )
        url = aws_client.s3.generate_presigned_url(
            "head_object", Params={"Bucket": s3_bucket, "Key": object_key}
        )
        # get object and assert headers
        response = requests.head(url, verify=False)
        assert response.headers.get("content-length") == str(len(body))

    @markers.aws.validated
    @pytest.mark.parametrize("verify_signature", (True, False))
    def test_put_url_metadata_with_sig_s3v4(
        self,
        s3_bucket,
        snapshot,
        aws_client,
        verify_signature,
        monkeypatch,
        presigned_snapshot_transformers,
    ):
        snapshot.add_transformer(snapshot.transform.s3_api())
        snapshot.add_transformer(snapshot.transform.key_value("HostId"))
        snapshot.add_transformer(snapshot.transform.key_value("RequestId"))
        if verify_signature:
            monkeypatch.setattr(config, "S3_SKIP_SIGNATURE_VALIDATION", False)
        else:
            monkeypatch.setattr(config, "S3_SKIP_SIGNATURE_VALIDATION", True)

        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version="s3v4"),
            endpoint_url=_endpoint_url(),
        )

        # Object metadata should be passed as signed headers when sending the pre-signed URL, the boto signer does not
        # append it to the URL
        # https://github.com/localstack/localstack/issues/544
        metadata = {"foo": "bar"}
        object_key = "key-by-hostname"

        # put object via presigned URL with metadata
        url = presigned_client.generate_presigned_url(
            "put_object",
            Params={"Bucket": s3_bucket, "Key": object_key, "Metadata": metadata},
        )
        assert "x-amz-meta-foo=bar" not in url

        # put the request without the headers
        response = requests.put(url, data="content 123")
        # if we skip validation, it should work for LocalStack
        if not verify_signature and not is_aws_cloud():
            assert response.ok, f"response returned {response.status_code}: {response.text}"
            # response body should be empty, see https://github.com/localstack/localstack/issues/1317
            assert not response.text
        else:
            assert response.status_code == 403
            exception = xmltodict.parse(response.content)
            snapshot.match("no-meta-headers", exception)

        # put it now with the signed headers
        response = requests.put(url, data="content 123", headers={"x-amz-meta-foo": "bar"})
        # assert metadata is present
        assert response.ok

        response = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        assert response["Metadata"]["foo"] == "bar"
        snapshot.match("head_object", response)

        # assert with another metadata, should fail if verify_signature is not True
        response = requests.put(url, data="content 123", headers={"x-amz-meta-wrong": "wrong"})

        # if we skip validation, it should work for LocalStack
        if not verify_signature and not is_aws_cloud():
            assert response.ok, f"response returned {response.status_code}: {response.text}"
        else:
            assert response.status_code == 403
            exception = xmltodict.parse(response.content)
            snapshot.match("wrong-meta-headers", exception)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        if not verify_signature and not is_aws_cloud():
            assert head_object["Metadata"]["wrong"] == "wrong"
        else:
            assert "wrong" not in head_object["Metadata"]

    @markers.aws.validated
    @pytest.mark.parametrize("verify_signature", (True, False))
    def test_put_url_metadata_with_sig_s3(
        self,
        s3_bucket,
        snapshot,
        aws_client,
        verify_signature,
        monkeypatch,
        presigned_snapshot_transformers,
    ):
        snapshot.add_transformer(snapshot.transform.s3_api())
        if verify_signature:
            monkeypatch.setattr(config, "S3_SKIP_SIGNATURE_VALIDATION", False)
        else:
            monkeypatch.setattr(config, "S3_SKIP_SIGNATURE_VALIDATION", True)

        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version="s3"),
            endpoint_url=_endpoint_url(),
        )

        # Object metadata should be passed as query params via presigned URL
        # https://github.com/localstack/localstack/issues/544
        metadata = {"foo": "bar"}
        object_key = "key-by-hostname"

        # put object via presigned URL with metadata
        url = presigned_client.generate_presigned_url(
            "put_object",
            Params={"Bucket": s3_bucket, "Key": object_key, "Metadata": metadata},
        )
        assert "x-amz-meta-foo=bar" in url

        response = requests.put(url, data="content 123", verify=False)
        assert response.ok, f"response returned {response.status_code}: {response.text}"
        # response body should be empty, see https://github.com/localstack/localstack/issues/1317
        assert not response.text

        # assert metadata is present
        response = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        assert response.get("Metadata", {}).get("foo") == "bar"
        snapshot.match("head_object", response)

        # assert with another metadata directly in the headers
        response = requests.put(url, data="content 123", headers={"x-amz-meta-wrong": "wrong"})
        # if we skip validation, it should work for LocalStack
        if not verify_signature and not is_aws_cloud():
            assert response.ok, f"response returned {response.status_code}: {response.text}"
        else:
            assert response.status_code == 403
            exception = xmltodict.parse(response.content)
            snapshot.match("wrong-meta-headers", exception)

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        if not verify_signature and not is_aws_cloud():
            assert head_object["Metadata"]["wrong"] == "wrong"
        else:
            assert "wrong" not in head_object["Metadata"]

    @markers.aws.validated
    def test_get_object_ignores_request_body(self, s3_bucket, aws_client):
        key = "foo-key"
        body = "foobar"

        aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body=body)

        url = aws_client.s3.generate_presigned_url(
            "get_object", Params={"Bucket": s3_bucket, "Key": key}
        )

        response = requests.get(url, data=b"get body is ignored by AWS")
        assert response.status_code == 200
        assert response.text == body

    @markers.aws.validated
    def test_presigned_double_encoded_credentials(
        self, s3_bucket, aws_client, snapshot, presigned_snapshot_transformers
    ):
        key = "foo-key"
        body = "foobar"

        aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body=body)

        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version="s3v4"),
            endpoint_url=_endpoint_url(),
        )
        url = presigned_client.generate_presigned_url(
            "get_object", Params={"Bucket": s3_bucket, "Key": key}
        )
        url = url.replace("%2F", "%252F")

        response = requests.get(url)
        assert response.status_code == 400
        exception = xmltodict.parse(response.content)
        snapshot.match("error-malformed", exception)

    @markers.aws.validated
    @pytest.mark.parametrize(
        "signature_version, verify_signature",
        [
            ("s3", True),
            ("s3", False),
            ("s3v4", True),
            ("s3v4", False),
        ],
    )
    def test_put_object_with_md5_and_chunk_signature_bad_headers(
        self,
        s3_bucket,
        signature_version,
        verify_signature,
        monkeypatch,
        snapshot,
        aws_client,
        presigned_snapshot_transformers,
    ):
        snapshotted = False
        if verify_signature:
            monkeypatch.setattr(config, "S3_SKIP_SIGNATURE_VALIDATION", False)
            snapshotted = True
        else:
            monkeypatch.setattr(config, "S3_SKIP_SIGNATURE_VALIDATION", True)

        object_key = "test-runtime.properties"
        content_md5 = "pX8KKuGXS1f2VTcuJpqjkw=="
        headers = {
            "Content-Md5": content_md5,
            "Content-Type": "application/octet-stream",
            "X-Amz-Content-Sha256": "STREAMING-AWS4-HMAC-SHA256-PAYLOAD",
            "X-Amz-Date": "20211122T191045Z",
            "X-Amz-Decoded-Content-Length": "test",  # string instead of int
            "Content-Length": "10",
            "Connection": "Keep-Alive",
            "Expect": "100-continue",
        }

        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version=signature_version),
            endpoint_url=_endpoint_url(),
        )
        url = presigned_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": s3_bucket,
                "Key": object_key,
                "ContentType": "application/octet-stream",
                "ContentMD5": content_md5,
            },
        )
        result = requests.put(url, data="test", headers=headers)
        assert result.status_code == 403
        if snapshotted:
            exception = xmltodict.parse(result.content)
            snapshot.match("with-decoded-content-length", exception)

        if signature_version == "s3" or (not verify_signature and not is_aws_cloud()):
            assert b"SignatureDoesNotMatch" in result.content
        # we are either using s3v4 with new provider or whichever signature against AWS
        else:
            assert b"AccessDenied" in result.content

        # check also no X-Amz-Decoded-Content-Length
        headers.pop("X-Amz-Decoded-Content-Length")
        result = requests.put(url, data="test", headers=headers)
        assert result.status_code == 403, (result, result.content)
        if snapshotted:
            exception = xmltodict.parse(result.content)
            snapshot.match("without-decoded-content-length", exception)
        if signature_version == "s3" or (not verify_signature and not is_aws_cloud()):
            assert b"SignatureDoesNotMatch" in result.content
        else:
            assert b"AccessDenied" in result.content

    @markers.aws.validated
    def test_s3_get_response_default_content_type(self, s3_bucket, aws_client):
        # When no content type is provided by a PUT request
        # 'binary/octet-stream' should be used
        # src: https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectPUT.html

        # put object
        object_key = "key-by-hostname"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something")

        # get object and assert headers
        url = aws_client.s3.generate_presigned_url(
            "get_object", Params={"Bucket": s3_bucket, "Key": object_key}
        )
        response = requests.get(url, verify=False)
        assert response.headers["content-type"] == "binary/octet-stream"

    @markers.aws.validated
    @pytest.mark.parametrize("signature_version", ["s3", "s3v4"])
    def test_s3_presigned_url_expired(
        self,
        s3_bucket,
        signature_version,
        snapshot,
        patch_s3_skip_signature_validation_false,
        aws_client,
        presigned_snapshot_transformers,
    ):
        object_key = "key-expires-in-2"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something")

        # get object and assert headers
        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version=signature_version),
            endpoint_url=_endpoint_url(),
        )
        url = presigned_client.generate_presigned_url(
            "get_object", Params={"Bucket": s3_bucket, "Key": object_key}, ExpiresIn=2
        )
        # retrieving it before expiry
        resp = requests.get(url, verify=False)
        assert resp.status_code == 200
        assert to_str(resp.content) == "something"

        time.sleep(3)  # wait for the URL to expire
        resp = requests.get(url, verify=False)
        resp_content = to_str(resp.content)
        assert resp.status_code == 403
        exception = xmltodict.parse(resp.content)
        snapshot.match("expired-exception", exception)

        assert "<Code>AccessDenied</Code>" in resp_content
        assert "<Message>Request has expired</Message>" in resp_content

        url = presigned_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": s3_bucket, "Key": object_key},
            ExpiresIn=120,
        )

        resp = requests.get(url, verify=False)
        assert resp.status_code == 200
        assert to_str(resp.content) == "something"

    @markers.aws.validated
    @pytest.mark.parametrize("signature_version", ["s3", "s3v4"])
    def test_s3_put_presigned_url_with_different_headers(
        self,
        s3_bucket,
        signature_version,
        snapshot,
        patch_s3_skip_signature_validation_false,
        aws_client,
        presigned_snapshot_transformers,
    ):
        object_key = "key-double-header-param"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something")

        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version=signature_version),
            endpoint_url=_endpoint_url(),
        )
        # Content-Type, Content-MD5 and Date are specific headers for SigV2 and are checked
        # others are not verified in the signature
        # Manually set the content-type for it to be added to the signature
        presigned_url = presigned_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": s3_bucket,
                "Key": object_key,
                "ContentType": "text/plain",
            },
            ExpiresIn=10,
        )
        # Use the pre-signed URL with the right ContentType
        response = requests.put(
            presigned_url,
            data="test_data",
            headers={"Content-Type": "text/plain"},
        )
        assert not response.content
        assert response.status_code == 200

        # Use the pre-signed URL with the wrong ContentType
        response = requests.put(
            presigned_url,
            data="test_data",
            headers={"Content-Type": "text/xml"},
        )
        assert response.status_code == 403

        exception = xmltodict.parse(response.content)
        exception["StatusCode"] = response.status_code
        snapshot.match("content-type-exception", exception)

        if signature_version == "s3":
            # we sleep 1 second to allow the StringToSign value in the exception change between both call
            # (timestamped value, to avoid the test being flaky)
            time.sleep(1.1)

        # regenerate a new pre-signed URL with no content-type specified
        presigned_url = presigned_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": s3_bucket,
                "Key": object_key,
                "ContentEncoding": "identity",
            },
            ExpiresIn=10,
        )

        # send the pre-signed URL with the right ContentEncoding
        response = requests.put(
            presigned_url,
            data="test_data",
            headers={"Content-Encoding": "identity"},
        )
        assert not response.content
        assert response.status_code == 200

        # send the pre-signed URL with the right ContentEncoding but a new ContentType
        # should fail with SigV2 and succeed with SigV4
        response = requests.put(
            presigned_url,
            data="test_data",
            headers={"Content-Encoding": "identity", "Content-Type": "text/xml"},
        )
        if signature_version == "s3":
            assert response.status_code == 403
        else:
            assert response.status_code == 200

        exception = xmltodict.parse(response.content) if response.content else {}
        exception["StatusCode"] = response.status_code
        snapshot.match("content-type-response", exception)

        # now send the pre-signed URL with the wrong ContentEncoding
        # should succeed with SigV2 as only hard coded headers are checked
        # but fail with SigV4 as Content-Encoding was part of the signed headers
        response = requests.put(
            presigned_url,
            data="test_data",
            headers={"Content-Encoding": "gzip"},
        )
        if signature_version == "s3":
            assert response.status_code == 200
        else:
            assert response.status_code == 403
        exception = xmltodict.parse(response.content) if response.content else {}
        exception["StatusCode"] = response.status_code
        snapshot.match("wrong-content-encoding-response", exception)

    @markers.aws.validated
    def test_s3_put_presigned_url_same_header_and_qs_parameter(
        self,
        s3_bucket,
        snapshot,
        patch_s3_skip_signature_validation_false,
        aws_client,
        presigned_snapshot_transformers,
    ):
        # this test tries to check if double query/header values trigger InvalidRequest like said in the documentation
        # spoiler: they do not
        # https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-query-string-auth.html#query-string-auth-v4-signing

        object_key = "key-double-header-param"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something")

        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version="s3v4"),
            endpoint_url=_endpoint_url(),
        )
        presigned_url = presigned_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": s3_bucket,
                "Key": object_key,
                "RequestPayer": "requester",
            },
            ExpiresIn=10,
        )
        # add the same parameter as a query string parameter as well as header, with different values
        parsed = urlparse(presigned_url)
        query_params = parse_qs(parsed.query)
        # auth params needs to be at the end
        new_query_params = {"x-amz-request-payer": ["non-valid"]}
        for k, v in query_params.items():
            new_query_params[k] = v[0]

        new_query_params = urlencode(new_query_params, quote_via=quote, safe=" ")
        new_url = urlunsplit(
            SplitResult(  # noqa
                parsed.scheme, parsed.netloc, parsed.path, new_query_params, parsed.fragment
            )
        )
        response = requests.put(
            new_url,
            data="test_data",
            headers={"x-amz-request-payer": "requester"},
        )
        exception = xmltodict.parse(response.content) if response.content else {}
        exception["StatusCode"] = response.status_code
        snapshot.match("double-header-query-string", exception)

        # test overriding a signed query parameter
        response = requests.put(
            presigned_url,
            data="test_data",
            headers={"x-amz-expires": "5"},
        )
        exception = xmltodict.parse(response.content) if response.content else {}
        exception["StatusCode"] = response.status_code
        snapshot.match("override-signed-qs", exception)

    @markers.aws.validated
    @pytest.mark.parametrize("signature_version", ["s3", "s3v4"])
    def test_s3_put_presigned_url_missing_sig_param(
        self,
        s3_bucket,
        signature_version,
        snapshot,
        patch_s3_skip_signature_validation_false,
        aws_client,
        presigned_snapshot_transformers,
    ):
        object_key = "key-missing-param"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something")

        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version=signature_version),
            endpoint_url=_endpoint_url(),
        )
        url = presigned_client.generate_presigned_url(
            "get_object", Params={"Bucket": s3_bucket, "Key": object_key}, ExpiresIn=5
        )
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        # sig v2
        if "Signature" in query_params:
            query_params.pop("Expires", None)
        else:  # sig v4
            query_params.pop("X-Amz-Date", None)
        new_query_params = urlencode(
            {k: v[0] for k, v in query_params.items()}, quote_via=quote, safe=" "
        )

        invalid_url = urlunsplit(
            SplitResult(  # noqa
                parsed.scheme, parsed.netloc, parsed.path, new_query_params, parsed.fragment
            )
        )

        resp = requests.get(invalid_url, verify=False)
        assert resp.status_code in [
            400,
            403,
        ]  # the snapshot will differentiate between sig v2 and sig v4
        exception = xmltodict.parse(resp.content)
        exception["StatusCode"] = resp.status_code
        snapshot.match("missing-param-exception", exception)

    @markers.aws.validated
    def test_s3_get_response_content_type_same_as_upload_and_range(self, s3_bucket, aws_client):
        # put object
        object_key = "foo/bar/key-by-hostname"
        content_type = "foo/bar; charset=utf-8"
        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=object_key,
            Body="something " * 20,
            ContentType=content_type,
        )

        url = aws_client.s3.generate_presigned_url(
            "get_object", Params={"Bucket": s3_bucket, "Key": object_key}
        )

        # get object and assert headers
        response = requests.get(url, verify=False)
        assert content_type == response.headers["content-type"]

        # get object using range query and assert headers
        response = requests.get(url, headers={"Range": "bytes=0-18"}, verify=False)
        assert content_type == response.headers["content-type"]
        # test we only get the first 18 bytes from the object
        assert "something something" == to_str(response.content)

    @pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="STS not enabled in S3 image")
    @markers.aws.validated
    def test_presigned_url_with_session_token(
        self,
        s3_create_bucket_with_client,
        patch_s3_skip_signature_validation_false,
        aws_client,
        region_name,
    ):
        bucket_name = f"bucket-{short_uid()}"
        key_name = "key"
        response = aws_client.sts.get_session_token()
        if not is_aws_cloud():
            # Moto does not register the default returned value from STS as a valid IAM user, which is way we can't
            # retrieve the secret access key
            # we need to hardcode the secret access key to the default one
            response["Credentials"]["SecretAccessKey"] = (
                s3_constants.DEFAULT_PRE_SIGNED_SECRET_ACCESS_KEY
            )

        client = boto3.client(
            "s3",
            config=Config(signature_version="s3v4"),
            region_name=AWS_REGION_US_EAST_1,
            endpoint_url=_endpoint_url(),
            aws_access_key_id=response["Credentials"]["AccessKeyId"],
            aws_secret_access_key=response["Credentials"]["SecretAccessKey"],
            aws_session_token=response["Credentials"]["SessionToken"],
        )
        s3_create_bucket_with_client(s3_client=client, Bucket=bucket_name)
        client.put_object(Body="test-value", Bucket=bucket_name, Key=key_name)
        presigned_url = client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": bucket_name, "Key": key_name},
            ExpiresIn=600,
        )
        response = requests.get(presigned_url)
        assert response._content == b"test-value"

    @pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="STS not enabled in S3 image")
    @markers.aws.validated
    def test_presigned_url_with_different_user_credentials(
        self,
        aws_client,
        s3_create_bucket_with_client,
        create_role_with_policy,
        account_id,
        wait_and_assume_role,
        patch_s3_skip_signature_validation_false,
        region_name,
        aws_client_factory,
    ):
        bucket_name = f"bucket-{short_uid()}"
        key_name = "key"
        actions = [
            "s3:CreateBucket",
            "s3:PutObject",
            "s3:GetObject",
            "s3:DeleteBucket",
            "s3:DeleteObject",
        ]

        assume_policy_doc = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "sts:AssumeRole",
                    "Principal": {"AWS": account_id},
                    "Effect": "Allow",
                }
            ],
        }
        assume_policy_doc = json.dumps(assume_policy_doc)
        role_name, role_arn = create_role_with_policy(
            effect="Allow",
            actions=actions,
            assume_policy_doc=assume_policy_doc,
            resource="*",
        )

        credentials = wait_and_assume_role(role_arn=role_arn)

        client = boto3.client(
            "s3",
            region_name=region_name,
            config=Config(signature_version="s3v4"),
            endpoint_url=_endpoint_url(),
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )

        kwargs = (
            {"CreateBucketConfiguration": {"LocationConstraint": region_name}}
            if region_name != AWS_REGION_US_EAST_1
            else {}
        )
        retry(
            lambda: s3_create_bucket_with_client(s3_client=client, Bucket=bucket_name, **kwargs),
            sleep=3 if is_aws_cloud() else 0.5,
        )

        client.put_object(Body="test-value", Bucket=bucket_name, Key=key_name)
        presigned_url = client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": bucket_name, "Key": key_name},
            ExpiresIn=600,
        )
        response = requests.get(presigned_url)
        assert response._content == b"test-value"

    @markers.aws.validated
    @pytest.mark.parametrize("signature_version", ["s3", "s3v4"])
    def test_s3_get_response_header_overrides(
        self, s3_bucket, signature_version, patch_s3_skip_signature_validation_false, aws_client
    ):
        # Signed requests may include certain header overrides in the querystring
        # https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectGET.html
        object_key = "key-header-overrides"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something")

        # get object and assert headers
        expiry_date = "Wed, 21 Oct 2015 07:28:00 GMT"
        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version=signature_version), endpoint_url=_endpoint_url()
        )

        url = presigned_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": s3_bucket,
                "Key": object_key,
                "ResponseCacheControl": "max-age=74",
                "ResponseContentDisposition": 'attachment; filename="foo.jpg"',
                "ResponseContentEncoding": "identity",
                "ResponseContentLanguage": "de-DE",
                "ResponseContentType": "image/jpeg",
                "ResponseExpires": expiry_date,
            },
        )
        response = requests.get(url, verify=False)
        assert response.status_code == 200
        headers = response.headers
        assert headers["cache-control"] == "max-age=74"
        assert headers["content-disposition"] == 'attachment; filename="foo.jpg"'
        assert headers["content-encoding"] == "identity"
        assert headers["content-language"] == "de-DE"
        assert headers["content-type"] == "image/jpeg"

        # Note: looks like depending on the environment/libraries, we can get different date formats...
        possible_date_formats = ["2015-10-21T07:28:00Z", expiry_date]
        assert headers["expires"] in possible_date_formats

    @markers.aws.validated
    def test_s3_copy_md5(self, s3_bucket, snapshot, monkeypatch, aws_client):
        if not is_aws_cloud():
            monkeypatch.setattr(config, "S3_SKIP_SIGNATURE_VALIDATION", False)
        src_key = "src"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=src_key, Body="something")

        # copy object
        dest_key = "dest"
        response = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            CopySource={"Bucket": s3_bucket, "Key": src_key},
            Key=dest_key,
        )
        snapshot.match("copy-obj", response)

        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version="s3v4", s3={"payload_signing_enabled": True}),
            endpoint_url=_endpoint_url(),
        )

        # Create copy object to try to match s3a setting Content-MD5
        dest_key2 = "dest"
        url = presigned_client.generate_presigned_url(
            "copy_object",
            Params={
                "Bucket": s3_bucket,
                "CopySource": {"Bucket": s3_bucket, "Key": src_key},
                "Key": dest_key2,
            },
        )

        request_response = requests.put(
            url, headers={"x-amz-copy-source": f"{s3_bucket}/{src_key}"}, verify=False
        )
        assert request_response.status_code == 200

    @markers.aws.only_localstack
    def test_s3_get_response_case_sensitive_headers(self, s3_bucket, aws_client):
        # Test that ETag headers is case sensitive
        object_key = "key-by-hostname"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=object_key, Body="something")

        # get object and assert headers
        url = aws_client.s3.generate_presigned_url(
            "get_object", Params={"Bucket": s3_bucket, "Key": object_key}
        )
        response = requests.get(url, verify=False)
        # expect that Etag is contained
        header_names = list(response.headers.keys())
        assert "ETag" in header_names

    @pytest.mark.parametrize(
        "signature_version, use_virtual_address",
        [
            ("s3", False),
            ("s3", True),
            ("s3v4", False),
            ("s3v4", True),
        ],
    )
    @markers.aws.validated
    def test_presigned_url_signature_authentication_expired(
        self,
        s3_create_bucket,
        signature_version,
        use_virtual_address,
        snapshot,
        patch_s3_skip_signature_validation_false,
        aws_client,
        presigned_snapshot_transformers,
    ):
        bucket_name = f"presign-{short_uid()}"

        s3_endpoint_path_style = _endpoint_url()

        s3_create_bucket(Bucket=bucket_name)
        object_key = "temp.txt"
        aws_client.s3.put_object(Key=object_key, Bucket=bucket_name, Body="123")

        s3_config = {"addressing_style": "virtual"} if use_virtual_address else {}
        client = _s3_client_pre_signed_client(
            Config(signature_version=signature_version, s3=s3_config),
            endpoint_url=s3_endpoint_path_style,
        )

        url = _generate_presigned_url(client, {"Bucket": bucket_name, "Key": object_key}, expires=1)
        time.sleep(2)
        response = requests.get(url)
        assert response.status_code == 403
        exception = xmltodict.parse(response.content)
        snapshot.match("expired", exception)

    @pytest.mark.parametrize(
        "signature_version, use_virtual_address",
        [
            ("s3", False),
            ("s3", True),
            ("s3v4", False),
            ("s3v4", True),
        ],
    )
    @markers.aws.validated
    def test_presigned_url_signature_authentication(
        self,
        s3_create_bucket,
        signature_version,
        use_virtual_address,
        snapshot,
        patch_s3_skip_signature_validation_false,
        aws_client,
        presigned_snapshot_transformers,
    ):
        bucket_name = f"presign-{short_uid()}"

        s3_endpoint_path_style = _endpoint_url()
        s3_url = _bucket_url_vhost(bucket_name) if use_virtual_address else _bucket_url(bucket_name)

        s3_create_bucket(Bucket=bucket_name)
        object_key = "temp.txt"
        aws_client.s3.put_object(Key=object_key, Bucket=bucket_name, Body="123")

        s3_config = {"addressing_style": "virtual"} if use_virtual_address else {}
        client = _s3_client_pre_signed_client(
            Config(signature_version=signature_version, s3=s3_config),
            endpoint_url=s3_endpoint_path_style,
        )

        expires = 20

        # GET requests
        simple_params = {"Bucket": bucket_name, "Key": object_key}
        url = _generate_presigned_url(client, simple_params, expires)
        response = requests.get(url)
        assert response.status_code == 200
        assert response.content == b"123"

        params = {
            "Bucket": bucket_name,
            "Key": object_key,
            "ResponseContentType": "text/plain",
            "ResponseContentDisposition": "attachment;  filename=test.txt",
        }

        presigned = _generate_presigned_url(client, params, expires)
        response = requests.get(presigned)
        assert response.status_code == 200
        assert response.content == b"123"

        object_data = f"this should be found in when you download {object_key}."

        # invalid requests
        response = requests.get(
            _make_url_invalid(s3_url, object_key, presigned),
            data=object_data,
            headers={"Content-Type": "my-fake-content/type"},
        )
        assert response.status_code == 403
        exception = xmltodict.parse(response.content)
        snapshot.match("invalid-get-1", exception)

        # put object valid
        response = requests.put(
            _generate_presigned_url(client, simple_params, expires, client_method="put_object"),
            data=object_data,
        )
        # body should be empty, and it will show us the exception if it's not
        assert not response.content
        assert response.status_code == 200

        params = {
            "Bucket": bucket_name,
            "Key": object_key,
            "ContentType": "text/plain",
        }
        presigned_put_url = _generate_presigned_url(
            client, params, expires, client_method="put_object"
        )
        response = requests.put(
            presigned_put_url,
            data=object_data,
            headers={"Content-Type": "text/plain"},
        )
        assert not response.content
        assert response.status_code == 200

        # Invalid request
        response = requests.put(
            _make_url_invalid(s3_url, object_key, presigned_put_url),
            data=object_data,
            headers={"Content-Type": "my-fake-content/type"},
        )
        assert response.status_code == 403
        exception = xmltodict.parse(response.content)
        snapshot.match("invalid-put-1", exception)

        # DELETE requests
        presigned_delete_url = _generate_presigned_url(
            client, simple_params, expires, client_method="delete_object"
        )
        response = requests.delete(presigned_delete_url)
        assert response.status_code == 204

    @pytest.mark.parametrize(
        "signature_version, use_virtual_address",
        [
            ("s3", False),
            ("s3", True),
            ("s3v4", False),
            ("s3v4", True),
        ],
    )
    @markers.aws.validated
    def test_presigned_url_signature_authentication_multi_part(
        self,
        s3_create_bucket,
        signature_version,
        use_virtual_address,
        patch_s3_skip_signature_validation_false,
        aws_client,
    ):
        # it should test if the user is sending wrong signature
        bucket_name = f"presign-{short_uid()}"

        s3_endpoint_path_style = _endpoint_url()

        s3_create_bucket(Bucket=bucket_name)
        object_key = "temp.txt"

        s3_config = {"addressing_style": "virtual"} if use_virtual_address else {}
        client = _s3_client_pre_signed_client(
            Config(signature_version=signature_version, s3=s3_config),
            endpoint_url=s3_endpoint_path_style,
        )
        upload_id = client.create_multipart_upload(
            Bucket=bucket_name,
            Key=object_key,
        )["UploadId"]

        data = to_bytes("hello this is a upload test")
        upload_file_object = BytesIO(data)

        signed_url = _generate_presigned_url(
            client,
            {
                "Bucket": bucket_name,
                "Key": object_key,
                "UploadId": upload_id,
                "PartNumber": 1,
            },
            expires=4,
            client_method="upload_part",
        )

        response = requests.put(signed_url, data=upload_file_object)
        assert response.status_code == 200
        multipart_upload_parts = [{"ETag": response.headers["ETag"], "PartNumber": 1}]

        response = client.complete_multipart_upload(
            Bucket=bucket_name,
            Key=object_key,
            MultipartUpload={"Parts": multipart_upload_parts},
            UploadId=upload_id,
        )

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

        simple_params = {"Bucket": bucket_name, "Key": object_key}
        response = requests.get(_generate_presigned_url(client, simple_params, 4))
        assert response.status_code == 200
        assert response.content == data

    @pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="Lambda not enabled in S3 image")
    @markers.aws.validated
    def test_presigned_url_v4_x_amz_in_qs(
        self,
        s3_bucket,
        s3_create_bucket,
        patch_s3_skip_signature_validation_false,
        create_lambda_function,
        lambda_su_role,
        create_tmp_folder_lambda,
        aws_client,
        snapshot,
    ):
        # test that Boto does not hoist x-amz-storage-class in the query string while pre-signing
        object_key = "temp.txt"
        client = _s3_client_pre_signed_client(
            Config(signature_version="s3v4"),
        )
        url = client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": s3_bucket,
                "Key": object_key,
                "StorageClass": StorageClass.STANDARD,
                "Metadata": {"foo": "bar-complicated-no-random"},
            },
        )
        assert StorageClass.STANDARD not in url
        assert "bar-complicated-no-random" not in url

        handler_file = os.path.join(
            os.path.dirname(__file__), "../lambda_/functions/lambda_s3_integration_presign.mjs"
        )
        temp_folder = create_tmp_folder_lambda(
            handler_file,
            run_command="npm i @aws-sdk/util-endpoints @aws-sdk/client-s3 @aws-sdk/s3-request-presigner @aws-sdk/middleware-endpoint",
        )

        function_name = f"func-integration-{short_uid()}"
        create_lambda_function(
            func_name=function_name,
            zip_file=testutil.create_zip_file(temp_folder, get_content=True),
            runtime=Runtime.nodejs20_x,
            handler="lambda_s3_integration_presign.handler",
            role=lambda_su_role,
            envvars={
                "ACCESS_KEY": s3_constants.DEFAULT_PRE_SIGNED_ACCESS_KEY_ID,
                "SECRET_KEY": s3_constants.DEFAULT_PRE_SIGNED_SECRET_ACCESS_KEY,
            },
        )
        s3_create_bucket(Bucket=function_name)

        response = aws_client.lambda_.invoke(FunctionName=function_name)
        payload = json.load(response["Payload"])
        presigned_url = payload["body"].strip('"')
        # assert that the Javascript SDK hoists it in the URL, unlike Boto
        assert StorageClass.STANDARD in presigned_url
        assert "bar-complicated-no-random" in presigned_url
        # the JS SDK also adds a default checksum now even for pre-signed URLs
        assert "x-amz-checksum-crc32=AAAAAA%3D%3D" in presigned_url

        # missing Content-MD5
        response = requests.put(presigned_url, verify=False, data=b"123456")
        assert response.status_code == 403

        # AWS needs the Content-MD5 header to validate the integrity of the file as set in the pre-signed URL
        # but do not provide StorageClass in the headers, because it's not in SignedHeaders
        response = requests.put(
            presigned_url,
            data=b"123456",
            verify=False,
            headers={"Content-MD5": "4QrcOUm6Wau+VuBX8g+IPg=="},
        )
        assert response.status_code == 200

        # assert that the checksum-crc-32 value is still validated and important for the signature
        bad_presigned_url = presigned_url.replace("crc32=AAAAAA%3D%3D", "crc32=BBBBBB%3D%3D")
        response = requests.put(
            bad_presigned_url,
            data=b"123456",
            verify=False,
            headers={"Content-MD5": "4QrcOUm6Wau+VuBX8g+IPg=="},
        )
        assert response.status_code == 403

        # verify that we properly saved the data
        head_object = aws_client.s3.head_object(
            Bucket=function_name, Key=object_key, ChecksumMode="ENABLED"
        )
        snapshot.match("head-object", head_object)

    @pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="Lambda not enabled in S3 image")
    @markers.aws.validated
    def test_presigned_url_v4_signed_headers_in_qs(
        self,
        s3_bucket,
        s3_create_bucket,
        patch_s3_skip_signature_validation_false,
        create_lambda_function,
        lambda_su_role,
        create_tmp_folder_lambda,
        aws_client,
    ):
        # test that Boto does not hoist x-amz-server-side-encryption in the query string while pre-signing
        # it means we would need to provide it in the request headers
        object_key = "temp.txt"
        client = _s3_client_pre_signed_client(
            Config(signature_version="s3v4"),
        )
        url = client.generate_presigned_url(
            "put_object",
            Params={"Bucket": s3_bucket, "Key": object_key, "ServerSideEncryption": "AES256"},
        )
        assert "=AES256" not in url

        handler_file = os.path.join(
            os.path.dirname(__file__), "../lambda_/functions/lambda_s3_integration_sdk_v2.js"
        )
        temp_folder = create_tmp_folder_lambda(handler_file)

        function_name = f"func-integration-{short_uid()}"
        # we need the AWS SDK v2, and Node 16 still has it by default
        # TODO since Node 16 is getting depricated we should consider ugrading to Node 20
        create_lambda_function(
            func_name=function_name,
            zip_file=testutil.create_zip_file(temp_folder, get_content=True),
            runtime=Runtime.nodejs16_x,
            handler="lambda_s3_integration_sdk_v2.handler",
            role=lambda_su_role,
            envvars={
                "ACCESS_KEY": s3_constants.DEFAULT_PRE_SIGNED_ACCESS_KEY_ID,
                "SECRET_KEY": s3_constants.DEFAULT_PRE_SIGNED_SECRET_ACCESS_KEY,
            },
        )
        s3_create_bucket(Bucket=function_name)

        response = aws_client.lambda_.invoke(FunctionName=function_name)
        payload = json.load(response["Payload"])
        presigned_url = payload["body"].strip('"')
        assert "=AES256" in presigned_url

        # AWS needs the Content-MD5 header to validate the integrity of the file as set in the pre-signed URL
        response = requests.put(presigned_url, verify=False, data=b"123456")
        assert response.status_code == 403

        # assert that we don't need to give x-amz-server-side-encryption even though it's in SignedHeaders,
        # because it's in the query string
        response = requests.put(
            presigned_url,
            data=b"123456",
            verify=False,
            headers={"Content-MD5": "4QrcOUm6Wau+VuBX8g+IPg=="},
        )
        assert response.status_code == 200

        # assert that even if we give x-amz-server-side-encryption, as long as it's the same value as the query string,
        # it will work
        response = requests.put(
            presigned_url,
            data=b"123456",
            verify=False,
            headers={
                "Content-MD5": "4QrcOUm6Wau+VuBX8g+IPg==",
                "x-amz-server-side-encryption": "AES256",
            },
        )
        assert response.status_code == 200

    @markers.aws.validated
    def test_pre_signed_url_forward_slash_bucket(
        self, s3_bucket, patch_s3_skip_signature_validation_false, aws_client
    ):
        # PHP SDK accepts a bucket name with a forward slash when generating a pre-signed URL
        # however the signature will not match afterward (in AWS or with LocalStack)
        # the error message was misleading, because by default we remove the double slash from the path, and we did not
        # calculate the same signature as AWS
        object_key = "temp.txt"
        aws_client.s3.put_object(Key=object_key, Bucket=s3_bucket, Body="123")

        s3_endpoint_path_style = _endpoint_url()
        client = _s3_client_pre_signed_client(
            Config(signature_version="s3v4", s3={}),
            endpoint_url=s3_endpoint_path_style,
        )

        url = client.generate_presigned_url(
            "put_object",
            Params={"Bucket": s3_bucket, "Key": object_key},
        )
        parts = url.partition(s3_bucket)
        # add URL encoded forward slash to the bucket name in the path
        url_f_slash = parts[0] + "%2F" + parts[1] + parts[2]

        req = requests.get(url_f_slash)
        request_content = xmltodict.parse(req.content)
        assert "GET\n//test-bucket" in request_content["Error"]["CanonicalRequest"]

    @pytest.mark.parametrize(
        "signature_version",
        ["s3", "s3v4"],
    )
    @markers.aws.validated
    def test_s3_presign_url_encoding(
        self, aws_client, s3_bucket, signature_version, patch_s3_skip_signature_validation_false
    ):
        object_key = "table1-partitioned/date=2023-06-28/test.csv"
        aws_client.s3.put_object(Key=object_key, Bucket=s3_bucket, Body="123")

        s3_endpoint_path_style = _endpoint_url()
        client = _s3_client_pre_signed_client(
            Config(signature_version=signature_version, s3={}),
            endpoint_url=s3_endpoint_path_style,
        )

        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": s3_bucket, "Key": object_key},
        )

        req = requests.get(url)
        assert req.ok
        assert req.content == b"123"

    @markers.aws.validated
    def test_s3_ignored_special_headers(
        self,
        s3_bucket,
        patch_s3_skip_signature_validation_false,
        monkeypatch,
    ):
        # if the crt.auth is not available, not need to patch as it will use it by default
        if find_spec("botocore.crt.auth"):
            # the CRT client does not allow us to pass a protected header, it will trigger an exception, so we need
            # to patch the Signer selection to the Python implementation which does not have this check
            from botocore.auth import AUTH_TYPE_MAPS, S3SigV4QueryAuth

            monkeypatch.setitem(AUTH_TYPE_MAPS, "s3v4-query", S3SigV4QueryAuth)

        key = "my-key"
        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version="s3v4", s3={"payload_signing_enabled": True}),
            endpoint_url=_endpoint_url(),
        )

        def add_content_sha_header(request, **kwargs):
            request.headers["x-amz-content-sha256"] = "UNSIGNED-PAYLOAD"

        presigned_client.meta.events.register(
            "before-sign.s3.PutObject",
            handler=add_content_sha_header,
        )
        try:
            url = presigned_client.generate_presigned_url(
                "put_object", Params={"Bucket": s3_bucket, "Key": key}
            )
            assert "x-amz-content-sha256" in url
            # somehow, it's possible to add "x-amz-content-sha256" to signed headers, the AWS Go SDK does it
            resp = requests.put(
                url,
                data="something",
                verify=False,
                headers={"x-amz-content-sha256": "UNSIGNED-PAYLOAD"},
            )
            assert resp.ok

            # if signed but not provided, AWS will raise an exception
            resp = requests.put(url, data="something", verify=False)
            assert resp.status_code == 403

        finally:
            presigned_client.meta.events.unregister(
                "before-sign.s3.PutObject",
                add_content_sha_header,
            )

        # recreate the request, without the signed header
        url = presigned_client.generate_presigned_url(
            "put_object", Params={"Bucket": s3_bucket, "Key": key}
        )
        assert "x-amz-content-sha256" not in url

        # assert that if provided and not signed, AWS will ignore it even if it starts with `x-amz`
        resp = requests.put(
            url,
            data="something",
            verify=False,
            headers={"x-amz-content-sha256": "UNSIGNED-PAYLOAD"},
        )
        assert resp.ok

        # assert that x-amz-user-agent is not ignored, it must be set in SignedHeaders
        resp = requests.put(
            url, data="something", verify=False, headers={"x-amz-user-agent": "test"}
        )
        assert resp.status_code == 403

        # X-Amz-Signature needs to be the last query string parameter: insert x-id before like the Go SDK
        index = url.find("&X-Amz-Signature")
        rewritten_url = url[:index] + "&x-id=PutObject" + url[index:]
        # however, the x-id query string parameter is not ignored
        resp = requests.put(rewritten_url, data="something", verify=False)
        assert resp.status_code == 403

    @markers.aws.validated
    def test_pre_signed_url_if_none_match(self, s3_bucket, aws_client, aws_session):
        # there currently is a bug in Boto3: https://github.com/boto/boto3/issues/4367
        # so we need to use botocore directly to allow testing of this, as other SDK like the Java SDK have the correct
        # behavior
        object_key = "temp.txt"

        s3_endpoint_path_style = _endpoint_url()

        # assert that the regular Boto3 client does not work, and does not sign the parameter as requested
        client = _s3_client_pre_signed_client(
            Config(signature_version="s3v4", s3={}),
            endpoint_url=s3_endpoint_path_style,
        )
        bad_url = client.generate_presigned_url(
            "put_object",
            Params={"Bucket": s3_bucket, "Key": object_key, "IfNoneMatch": "*"},
        )
        assert "if-none-match=%2a" not in bad_url.lower()

        req = botocore.awsrequest.AWSRequest(
            method="PUT",
            url=f"{s3_endpoint_path_style}/{s3_bucket}/{object_key}",
            data={},
            params={
                "If-None-Match": "*",
            },
            headers={},
        )

        botocore.auth.S3SigV4QueryAuth(aws_session.get_credentials(), "s3", "us-east-1").add_auth(
            req
        )

        assert "if-none-match=%2a" in req.url.lower()

        response = requests.put(req.url)
        assert response.status_code == 200

        response = requests.put(req.url)
        # we are now failing because the object already exists
        assert response.status_code == 412

    @markers.aws.validated
    def test_pre_signed_url_if_match(self, s3_bucket, aws_client, aws_session):
        key = "test-precondition"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body="test")

        s3_endpoint_path_style = _endpoint_url()
        # empty object ETag is provided
        empty_object_etag = "d41d8cd98f00b204e9800998ecf8427e"

        # assert that the regular Boto3 client does not work, and does not sign the parameter as requested
        client = _s3_client_pre_signed_client(
            Config(signature_version="s3v4", s3={}),
            endpoint_url=s3_endpoint_path_style,
        )
        bad_url = client.generate_presigned_url(
            "put_object",
            Params={"Bucket": s3_bucket, "Key": key, "IfMatch": empty_object_etag},
        )
        assert "if-match=d41d8cd98f00b204e9800998ecf8427e" not in bad_url.lower()

        req = botocore.awsrequest.AWSRequest(
            method="PUT",
            url=f"{s3_endpoint_path_style}/{s3_bucket}/{key}",
            data={},
            params={
                "If-Match": empty_object_etag,
            },
            headers={},
        )

        botocore.auth.S3SigV4QueryAuth(aws_session.get_credentials(), "s3", "us-east-1").add_auth(
            req
        )
        assert "if-match=d41d8cd98f00b204e9800998ecf8427e" in req.url.lower()

        response = requests.put(req.url)
        assert response.status_code == 412


class TestS3DeepArchive:
    """
    Test to cover DEEP_ARCHIVE Storage Class functionality.
    """

    @markers.aws.validated
    def test_storage_class_deep_archive(self, s3_bucket, tmpdir, aws_client):
        key = "my-key"

        transfer_config = TransferConfig(multipart_threshold=5 * KB, multipart_chunksize=1 * KB)

        def upload_file(size_in_kb: int):
            file = tmpdir / f"test-file-{short_uid()}.bin"
            data = b"1" * (size_in_kb * KB)
            file.write(data=data, mode="w")
            aws_client.s3.upload_file(
                Bucket=s3_bucket,
                Key=key,
                Filename=str(file.realpath()),
                ExtraArgs={"StorageClass": "DEEP_ARCHIVE"},
                Config=transfer_config,
            )

        upload_file(1)
        upload_file(9)
        upload_file(15)

        for obj in aws_client.s3.list_objects_v2(Bucket=s3_bucket)["Contents"]:
            assert obj["StorageClass"] == "DEEP_ARCHIVE"

    @markers.aws.validated
    def test_s3_get_deep_archive_object_restore(self, s3_create_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())

        bucket_name = f"bucket-{short_uid()}"
        object_key = f"key-{short_uid()}"

        s3_create_bucket(Bucket=bucket_name)

        # put DEEP_ARCHIVE object
        aws_client.s3.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body="body data",
            StorageClass="DEEP_ARCHIVE",
        )
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=bucket_name, Key=object_key)
        snapshot.match("get-object-invalid-state", e.value.response)

        snapshot.match("get_object_invalid_state", e.value.response)
        response = aws_client.s3.restore_object(
            Bucket=bucket_name,
            Key=object_key,
            RestoreRequest={
                "Days": 30,
                "GlacierJobParameters": {
                    "Tier": "Bulk",
                },
            },
        )
        snapshot.match("restore_object", response)

        # AWS tier is currently configured to retrieve within 48 hours, so we cannot test the get-object here
        response = aws_client.s3.head_object(Bucket=bucket_name, Key=object_key)
        if 'ongoing-request="false"' in response.get("Restore", ""):
            # if the restoring happens in LocalStack (or was fast in AWS) we can retrieve the object
            restore_bucket_name = f"bucket-{short_uid()}"
            s3_create_bucket(Bucket=restore_bucket_name)

            aws_client.s3.copy_object(
                CopySource={"Bucket": bucket_name, "Key": object_key},
                Bucket=restore_bucket_name,
                Key=object_key,
            )
            response = aws_client.s3.get_object(Bucket=restore_bucket_name, Key=object_key)
            assert "etag" in response.get("ResponseMetadata").get("HTTPHeaders")


class TestS3StaticWebsiteHosting:
    """
    Test to cover StaticWebsiteHosting functionality.
    """

    @markers.aws.validated
    def test_s3_static_website_index(self, s3_bucket, aws_client, allow_bucket_acl):
        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")
        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="index.html",
            Body="index",
            ContentType="text/html",
            ACL="public-read",
        )

        aws_client.s3.put_bucket_website(
            Bucket=s3_bucket,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
            },
        )

        url = _website_bucket_url(s3_bucket)

        response = requests.get(url, verify=False)
        assert response.status_code == 200
        assert response.text == "index"

    @markers.aws.validated
    def test_s3_static_website_hosting(self, s3_bucket, aws_client, allow_bucket_acl):
        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")
        index_obj = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="test/index.html",
            Body="index",
            ContentType="text/html",
            ACL="public-read",
        )
        error_obj = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="test/error.html",
            Body="error",
            ContentType="text/html",
            ACL="public-read",
        )
        actual_key_obj = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="actual/key.html",
            Body="key",
            ContentType="text/html",
            ACL="public-read",
        )
        with_content_type_obj = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="with-content-type/key.js",
            Body="some js",
            ContentType="application/javascript; charset=utf-8",
            ACL="public-read",
        )
        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="to-be-redirected.html",
            WebsiteRedirectLocation="/actual/key.html",
            ACL="public-read",
        )
        aws_client.s3.put_bucket_website(
            Bucket=s3_bucket,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
                "ErrorDocument": {"Key": "test/error.html"},
            },
        )
        website_url = _website_bucket_url(s3_bucket)
        # actual key
        url = f"{website_url}/actual/key.html"
        response = requests.get(url, verify=False)
        assert response.status_code == 200
        assert response.text == "key"
        assert "content-type" in response.headers
        assert response.headers["content-type"] == "text/html"
        assert "etag" in response.headers
        assert actual_key_obj["ETag"] in response.headers["etag"]

        # If-None-Match and Etag
        response = requests.get(
            url, headers={"If-None-Match": actual_key_obj["ETag"]}, verify=False
        )
        assert response.status_code == 304

        # key with specified content-type
        url = f"{website_url}/with-content-type/key.js"
        response = requests.get(url, verify=False)
        assert response.status_code == 200
        assert response.text == "some js"
        assert "content-type" in response.headers
        assert response.headers["content-type"] == "application/javascript; charset=utf-8"
        assert "etag" in response.headers
        assert response.headers["etag"] == with_content_type_obj["ETag"]

        # index document
        url = f"{website_url}/test"
        response = requests.get(url, verify=False)
        assert response.status_code == 200
        assert response.text == "index"
        assert "content-type" in response.headers
        assert "text/html" in response.headers["content-type"]
        assert "etag" in response.headers
        assert response.headers["etag"] == index_obj["ETag"]

        # root path test
        url = f"{website_url}/"
        response = requests.get(url, verify=False)
        assert response.status_code == 404
        assert response.text == "error"
        assert "content-type" in response.headers
        assert "text/html" in response.headers["content-type"]
        assert "etag" in response.headers
        assert response.headers["etag"] == error_obj["ETag"]

        # error document
        url = f"{website_url}/something"
        response = requests.get(url, verify=False)
        assert response.status_code == 404
        assert response.text == "error"
        assert "content-type" in response.headers
        assert "text/html" in response.headers["content-type"]
        assert "etag" in response.headers
        assert response.headers["etag"] == error_obj["ETag"]

        # redirect object
        url = f"{website_url}/to-be-redirected.html"
        response = requests.get(url, verify=False, allow_redirects=False)
        assert response.status_code == 301
        assert "location" in response.headers
        assert "actual/key.html" in response.headers["location"]

        response = requests.get(url, verify=False)
        assert response.status_code == 200
        assert response.headers["etag"] == actual_key_obj["ETag"]

    @markers.aws.validated
    def test_website_hosting_no_such_website(
        self, s3_bucket, snapshot, aws_client, allow_bucket_acl
    ):
        snapshot.add_transformers_list(self._get_static_hosting_transformers(snapshot))

        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")

        random_url = _website_bucket_url(f"non-existent-bucket-{short_uid()}")
        response = requests.get(random_url, verify=False)
        assert response.status_code == 404
        snapshot.match("no-such-bucket", response.text)

        website_url = _website_bucket_url(s3_bucket)
        # actual key
        response = requests.get(website_url, verify=False)
        assert response.status_code == 404
        snapshot.match("no-such-website-config", response.text)

        url = f"{website_url}/actual/key.html"
        response = requests.get(url)
        assert response.status_code == 404
        snapshot.match("no-such-website-config-key", response.text)

    @markers.aws.validated
    def test_website_hosting_http_methods(self, s3_bucket, snapshot, aws_client, allow_bucket_acl):
        snapshot.add_transformers_list(self._get_static_hosting_transformers(snapshot))

        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")

        aws_client.s3.put_bucket_website(
            Bucket=s3_bucket,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
            },
        )
        website_url = _website_bucket_url(s3_bucket)
        req = requests.post(website_url, data="test")
        assert req.status_code == 405
        error_response = req.text
        snapshot.match("not-allowed-post", {"content": error_response})

        req = requests.delete(website_url)
        assert req.status_code == 405
        error_response = req.text
        snapshot.match("not-allowed-delete", {"content": error_response})

        aws_client.s3.put_bucket_website(
            Bucket=s3_bucket,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
                "ErrorDocument": {"Key": "error.html"},
            },
        )
        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="error.html",
            Body="error",
            ContentType="text/html",
            ACL="public-read",
        )

        # documentation states that error code in the range 4XX are redirected to the ErrorDocument
        # 405 in not concerned by this
        req = requests.post(website_url, data="test")
        assert req.status_code == 405

    @markers.aws.validated
    def test_website_hosting_index_lookup(self, s3_bucket, snapshot, aws_client, allow_bucket_acl):
        snapshot.add_transformers_list(self._get_static_hosting_transformers(snapshot))

        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")
        aws_client.s3.put_bucket_website(
            Bucket=s3_bucket,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
            },
        )

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="index.html",
            Body="index",
            ContentType="text/html",
            ACL="public-read",
        )

        website_url = _website_bucket_url(s3_bucket)
        # actual key
        response = requests.get(website_url)
        assert response.status_code == 200
        assert response.text == "index"

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="directory/index.html",
            Body="index",
            ContentType="text/html",
            ACL="public-read",
        )

        response = requests.get(f"{website_url}/directory", allow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"] == "/directory/"

        response = requests.get(f"{website_url}/directory/", verify=False)
        assert response.status_code == 200
        assert response.text == "index"

        response = requests.get(f"{website_url}/directory-wrong", verify=False)
        assert response.status_code == 404
        snapshot.match("404-no-trailing-slash", response.text)

        response = requests.get(f"{website_url}/directory-wrong/", verify=False)
        assert response.status_code == 404
        snapshot.match("404-with-trailing-slash", response.text)

    @markers.aws.validated
    def test_website_hosting_404(self, s3_bucket, snapshot, aws_client, allow_bucket_acl):
        snapshot.add_transformers_list(self._get_static_hosting_transformers(snapshot))

        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")
        aws_client.s3.put_bucket_website(
            Bucket=s3_bucket,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
            },
        )

        website_url = _website_bucket_url(s3_bucket)

        response = requests.get(website_url)
        assert response.status_code == 404
        snapshot.match("404-no-such-key", response.text)

        aws_client.s3.put_bucket_website(
            Bucket=s3_bucket,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
                "ErrorDocument": {"Key": "error.html"},
            },
        )
        response = requests.get(website_url)
        assert response.status_code == 404
        snapshot.match("404-no-such-key-nor-custom", response.text)

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="error.html",
            Body="error",
            ContentType="text/html",
            ACL="public-read",
        )

        response = requests.get(website_url)
        assert response.status_code == 404
        assert response.text == "error"

    @markers.aws.validated
    def test_object_website_redirect_location(self, s3_bucket, aws_client, allow_bucket_acl):
        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")
        aws_client.s3.put_bucket_website(
            Bucket=s3_bucket,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
                "ErrorDocument": {"Key": "error.html"},
            },
        )

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="index.html",
            WebsiteRedirectLocation="/another/index.html",
            ACL="public-read",
        )

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="error.html",
            Body="error_redirected",
            WebsiteRedirectLocation="/another/error.html",
            ACL="public-read",
        )

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="another/error.html",
            Body="error",
            ACL="public-read",
        )

        website_url = _website_bucket_url(s3_bucket)

        response = requests.get(website_url)
        # losing the status code because of the redirection in the error document
        assert response.status_code == 200
        assert response.text == "error"

    @markers.aws.validated
    def test_routing_rules_conditions(self, s3_bucket, aws_client, allow_bucket_acl):
        # https://github.com/localstack/localstack/issues/6308

        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")
        aws_client.s3.put_bucket_website(
            Bucket=s3_bucket,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
                "ErrorDocument": {"Key": "error.html"},
                "RoutingRules": [
                    {
                        "Condition": {
                            "KeyPrefixEquals": "both-prefixed/",
                            "HttpErrorCodeReturnedEquals": "404",
                        },
                        "Redirect": {"ReplaceKeyWith": "redirected-both.html"},
                    },
                    {
                        "Condition": {"KeyPrefixEquals": "prefixed"},
                        "Redirect": {"ReplaceKeyWith": "redirected.html"},
                    },
                    {
                        "Condition": {"HttpErrorCodeReturnedEquals": "404"},
                        "Redirect": {"ReplaceKeyWith": "redirected.html"},
                    },
                ],
            },
        )

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="redirected.html",
            Body="redirected",
            ACL="public-read",
        )

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="prefixed-key-test",
            Body="prefixed",
            ACL="public-read",
        )

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="redirected-both.html",
            Body="redirected-both",
            ACL="public-read",
        )

        website_url = _website_bucket_url(s3_bucket)

        response = requests.get(f"{website_url}/non-existent-key", allow_redirects=False)
        assert response.status_code == 301
        assert response.headers["Location"] == f"{website_url}/redirected.html"

        # redirects when the custom ErrorDocument is not found
        response = requests.get(f"{website_url}/non-existent-key")
        assert response.status_code == 200
        assert response.text == "redirected"

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="error.html",
            Body="error",
            ACL="public-read",
        )

        response = requests.get(f"{website_url}/non-existent-key")
        assert response.status_code == 200
        assert response.text == "redirected"

        response = requests.get(f"{website_url}/prefixed-key-test")
        assert response.status_code == 200
        assert response.text == "redirected"

        response = requests.get(f"{website_url}/both-prefixed/")
        assert response.status_code == 200
        assert response.text == "redirected-both"

    @markers.aws.validated
    def test_routing_rules_redirects(self, s3_bucket, aws_client, allow_bucket_acl):
        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")
        aws_client.s3.put_bucket_website(
            Bucket=s3_bucket,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
                "ErrorDocument": {"Key": "error.html"},
                "RoutingRules": [
                    {
                        "Condition": {
                            "KeyPrefixEquals": "host/",
                        },
                        "Redirect": {"HostName": "random-hostname"},
                    },
                    {
                        "Condition": {
                            "KeyPrefixEquals": "replace-prefix/",
                        },
                        "Redirect": {"ReplaceKeyPrefixWith": "replaced-prefix/"},
                    },
                    {
                        "Condition": {
                            "KeyPrefixEquals": "protocol/",
                        },
                        "Redirect": {"Protocol": "https"},
                    },
                    {
                        "Condition": {
                            "KeyPrefixEquals": "code/",
                        },
                        "Redirect": {"HttpRedirectCode": "307"},
                    },
                ],
            },
        )

        website_url = _website_bucket_url(s3_bucket)

        response = requests.get(f"{website_url}/host/key", allow_redirects=False)
        assert response.status_code == 301
        assert response.headers["Location"] == "http://random-hostname/host/key"

        response = requests.get(f"{website_url}/replace-prefix/key", allow_redirects=False)
        assert response.status_code == 301
        assert response.headers["Location"] == f"{website_url}/replaced-prefix/key"

        response = requests.get(f"{website_url}/protocol/key", allow_redirects=False)
        assert response.status_code == 301
        assert not website_url.startswith("https")
        assert response.headers["Location"].startswith("https")

        response = requests.get(f"{website_url}/code/key", allow_redirects=False)
        assert response.status_code == 307

    @markers.aws.validated
    def test_routing_rules_empty_replace_prefix(self, s3_bucket, aws_client, allow_bucket_acl):
        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")
        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="index.html",
            Body="index",
            ACL="public-read",
        )
        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="test.html",
            Body="test",
            ACL="public-read",
        )
        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="error.html",
            Body="error",
            ACL="public-read",
        )
        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="mydocs/test.html",
            Body="mydocs",
            ACL="public-read",
        )

        # change configuration
        aws_client.s3.put_bucket_website(
            Bucket=s3_bucket,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
                "ErrorDocument": {"Key": "error.html"},
                "RoutingRules": [
                    {
                        "Condition": {"KeyPrefixEquals": "docs/"},
                        "Redirect": {"ReplaceKeyPrefixWith": ""},
                    },
                    {
                        "Condition": {"KeyPrefixEquals": "another/path/"},
                        "Redirect": {"ReplaceKeyPrefixWith": ""},
                    },
                ],
            },
        )

        website_url = _website_bucket_url(s3_bucket)

        # testing that routing rule redirect correctly (by removing the defined prefix)
        response = requests.get(f"{website_url}/docs/test.html")
        assert response.status_code == 200
        assert response.text == "test"

        response = requests.get(f"{website_url}/another/path/test.html")
        assert response.status_code == 200
        assert response.text == "test"

        response = requests.get(f"{website_url}/docs/mydocs/test.html")
        assert response.status_code == 200
        assert response.text == "mydocs"

        # no routing rule defined -> should result in error
        response = requests.get(f"{website_url}/docs2/test.html")
        assert response.status_code == 404
        assert response.text == "error"

    @markers.aws.validated
    def test_routing_rules_order(self, s3_bucket, aws_client, allow_bucket_acl):
        aws_client.s3.put_bucket_acl(Bucket=s3_bucket, ACL="public-read")
        aws_client.s3.put_bucket_website(
            Bucket=s3_bucket,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
                "ErrorDocument": {"Key": "error.html"},
                "RoutingRules": [
                    {
                        "Condition": {
                            "KeyPrefixEquals": "prefix",
                        },
                        "Redirect": {"ReplaceKeyWith": "redirected.html"},
                    },
                    {
                        "Condition": {
                            "KeyPrefixEquals": "index",
                        },
                        "Redirect": {"ReplaceKeyWith": "redirected.html"},
                    },
                ],
            },
        )

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="index.html",
            Body="index",
            ACL="public-read",
        )

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="redirected.html",
            Body="redirected",
            ACL="public-read",
        )

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="website-redirected.html",
            Body="website-redirected",
            ACL="public-read",
        )

        aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key="prefixed-key-test",
            Body="prefixed",
            ACL="public-read",
            WebsiteRedirectLocation="/website-redirected.html",
        )

        website_url = _website_bucket_url(s3_bucket)
        # testing that routing rules have precedence over individual object redirection
        response = requests.get(f"{website_url}/prefixed-key-test")
        assert response.status_code == 200
        assert response.text == "redirected"

        # assert that prefix rules don't apply for root path (internally redirected to index.html)
        response = requests.get(website_url)
        assert response.status_code == 200
        assert response.text == "index"

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        # todo: serializer issue with empty node, very tricky one...
        paths=["$.invalid-website-conf-1.Error.ArgumentValue"]
    )
    def test_validate_website_configuration(self, s3_bucket, snapshot, aws_client):
        website_configurations = [
            # can't have slash in the suffix
            {
                "IndexDocument": {"Suffix": "/index.html"},
            },
            # empty suffix value
            {
                "IndexDocument": {"Suffix": ""},
            },
            # if RedirectAllRequestsTo is set, cannot have other fields
            {
                "RedirectAllRequestsTo": {"HostName": "test"},
                "IndexDocument": {"Suffix": "/index.html"},
            },
            # does not have an IndexDocument field
            {
                "ErrorDocument": {"Key": "/index.html"},
            },
            # wrong protocol, must be http|https
            {
                "IndexDocument": {"Suffix": "index.html"},
                "RoutingRules": [{"Redirect": {"Protocol": "protocol"}}],
            },
            # has both ReplaceKeyPrefixWith and ReplaceKeyWith
            {
                "IndexDocument": {"Suffix": "index.html"},
                "RoutingRules": [
                    {
                        "Redirect": {
                            "ReplaceKeyPrefixWith": "prefix",
                            "ReplaceKeyWith": "key-name",
                        }
                    }
                ],
            },
            # empty Condition field in Routing Rule
            {
                "IndexDocument": {"Suffix": "index.html"},
                "RoutingRules": [
                    {
                        "Redirect": {
                            "ReplaceKeyPrefixWith": "prefix",
                        },
                        "Condition": {},
                    }
                ],
            },
            # empty routing rules
            {
                "IndexDocument": {"Suffix": "index.html"},
                "RoutingRules": [],
            },
        ]

        for index, invalid_configuration in enumerate(website_configurations):
            # not using pytest.raises, to have better debugging value in case of not raising exception
            # because of the loop, we don't know which configuration has not raised the exception
            try:
                aws_client.s3.put_bucket_website(
                    Bucket=s3_bucket,
                    WebsiteConfiguration=invalid_configuration,
                )
                raise AssertionError(f"{invalid_configuration} should have raised an exception")
            except ClientError as e:
                snapshot.match(f"invalid-website-conf-{index}", e.response)

    @markers.aws.validated
    def test_crud_website_configuration(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_website(Bucket=s3_bucket)
        snapshot.match("get-no-such-website-config", e.value.response)

        resp = aws_client.s3.delete_bucket_website(Bucket=s3_bucket)
        snapshot.match("del-no-such-website-config", resp)

        response = aws_client.s3.put_bucket_website(
            Bucket=s3_bucket,
            WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}},
        )
        snapshot.match("put-website-config", response)

        response = aws_client.s3.get_bucket_website(Bucket=s3_bucket)
        snapshot.match("get-website-config", response)

        aws_client.s3.delete_bucket_website(Bucket=s3_bucket)
        with pytest.raises(ClientError):
            aws_client.s3.get_bucket_website(Bucket=s3_bucket)

    @markers.aws.validated
    def test_website_hosting_redirect_all(self, s3_create_bucket, aws_client):
        bucket_name_redirected = f"bucket-{short_uid()}"
        bucket_name = f"bucket-{short_uid()}"

        s3_create_bucket(Bucket=bucket_name_redirected)
        aws_client.s3.delete_bucket_ownership_controls(Bucket=bucket_name_redirected)
        aws_client.s3.delete_public_access_block(Bucket=bucket_name_redirected)
        aws_client.s3.put_bucket_acl(Bucket=bucket_name_redirected, ACL="public-read")

        bucket_website_url = _website_bucket_url(bucket_name)
        bucket_website_host = urlparse(bucket_website_url).netloc

        aws_client.s3.put_bucket_website(
            Bucket=bucket_name_redirected,
            WebsiteConfiguration={
                "RedirectAllRequestsTo": {"HostName": bucket_website_host},
            },
        )

        s3_create_bucket(Bucket=bucket_name)
        aws_client.s3.delete_bucket_ownership_controls(Bucket=bucket_name)
        aws_client.s3.delete_public_access_block(Bucket=bucket_name)
        aws_client.s3.put_bucket_acl(Bucket=bucket_name, ACL="public-read")

        aws_client.s3.put_bucket_website(
            Bucket=bucket_name,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
            },
        )

        aws_client.s3.put_object(
            Bucket=bucket_name,
            Key="index.html",
            Body="index",
            ContentType="text/html",
            ACL="public-read",
        )

        redirected_bucket_website = _website_bucket_url(bucket_name_redirected)

        response_no_redirect = requests.get(redirected_bucket_website, allow_redirects=False)
        assert response_no_redirect.status_code == 301
        assert response_no_redirect.content == b""

        response_redirected = requests.get(redirected_bucket_website)
        assert response_redirected.status_code == 200
        assert response_redirected.content == b"index"

        response = requests.get(bucket_website_url)
        assert response.status_code == 200
        assert response.content == b"index"

        assert response.content == response_redirected.content

        response_redirected = requests.get(f"{redirected_bucket_website}/random-key")
        assert response_redirected.status_code == 404

    @staticmethod
    def _get_static_hosting_transformers(snapshot):
        return [
            snapshot.transform.regex(
                "RequestId: (.*?)</li>", replacement="RequestId: <request-id></li>"
            ),
            snapshot.transform.regex("HostId: (.*?)</li>", replacement="HostId: <host-id></li>"),
            snapshot.transform.regex(
                "BucketName: (.*?)</li>", replacement="BucketName: <bucket-name></li>"
            ),
        ]


class TestS3Routing:
    @markers.aws.only_localstack
    @pytest.mark.parametrize(
        "domain, use_virtual_address",
        [
            ("s3.amazonaws.com", False),
            ("s3.amazonaws.com", True),
            ("s3.us-west-2.amazonaws.com", False),
            ("s3.us-west-2.amazonaws.com", True),
        ],
    )
    def test_access_favicon_via_aws_endpoints(
        self, s3_bucket, domain, use_virtual_address, aws_client, region_name
    ):
        """Assert that /favicon.ico objects can be created/accessed/deleted using amazonaws host headers"""

        s3_key = "favicon.ico"
        content = b"test 123"
        aws_client.s3.put_object(Bucket=s3_bucket, Key=s3_key, Body=content)
        aws_client.s3.head_object(Bucket=s3_bucket, Key=s3_key)

        path = s3_key if use_virtual_address else f"{s3_bucket}/{s3_key}"
        url = f"{config.internal_service_url()}/{path}"
        headers = mock_aws_request_headers(
            "s3",
            aws_access_key_id=TEST_AWS_ACCESS_KEY_ID,
            region_name=region_name,
        )
        headers["host"] = f"{s3_bucket}.{domain}" if use_virtual_address else domain

        # get object via *.amazonaws.com host header
        result = requests.get(url, headers=headers)
        assert result.ok
        assert result.content == content

        # delete object via *.amazonaws.com host header
        result = requests.delete(url, headers=headers)
        assert result.ok

        # assert that object has been deleted
        with pytest.raises(ClientError) as exc:
            aws_client.s3.head_object(Bucket=s3_bucket, Key=s3_key)
        assert exc.value.response["Error"]["Message"] == "Not Found"


class TestS3BucketLifecycle:
    @markers.aws.validated
    def test_delete_bucket_lifecycle_configuration(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_lifecycle_configuration(Bucket=s3_bucket)
        snapshot.match("get-bucket-lifecycle-exc-1", e.value.response)

        resp = aws_client.s3.delete_bucket_lifecycle(Bucket=s3_bucket)
        snapshot.match("delete-bucket-lifecycle-no-bucket", resp)

        lfc = {
            "Rules": [
                {
                    "Expiration": {"Days": 7},
                    "ID": "wholebucket",
                    "Filter": {"Prefix": ""},
                    "Status": "Enabled",
                }
            ]
        }
        aws_client.s3.put_bucket_lifecycle_configuration(
            Bucket=s3_bucket, LifecycleConfiguration=lfc
        )
        result = retry(
            aws_client.s3.get_bucket_lifecycle_configuration, retries=3, sleep=1, Bucket=s3_bucket
        )
        snapshot.match("get-bucket-lifecycle-conf", result)
        aws_client.s3.delete_bucket_lifecycle(Bucket=s3_bucket)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_lifecycle_configuration(Bucket=s3_bucket)
        snapshot.match("get-bucket-lifecycle-exc-2", e.value.response)

    @markers.aws.validated
    def test_delete_lifecycle_configuration_on_bucket_deletion(
        self, s3_create_bucket, snapshot, aws_client
    ):
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))
        bucket_name = f"test-bucket-{short_uid()}"  # keep the same name for both bucket
        s3_create_bucket(Bucket=bucket_name)
        lfc = {
            "Rules": [
                {
                    "Expiration": {"Days": 7},
                    "ID": "wholebucket",
                    "Filter": {"Prefix": ""},
                    "Status": "Enabled",
                }
            ]
        }
        aws_client.s3.put_bucket_lifecycle_configuration(
            Bucket=bucket_name, LifecycleConfiguration=lfc
        )
        result = aws_client.s3.get_bucket_lifecycle_configuration(Bucket=bucket_name)
        snapshot.match("get-bucket-lifecycle-conf", result)
        aws_client.s3.delete_bucket(Bucket=bucket_name)
        s3_create_bucket(Bucket=bucket_name)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_lifecycle_configuration(Bucket=bucket_name)
        snapshot.match("get-bucket-lifecycle-exc", e.value.response)

    @markers.aws.validated
    def test_put_bucket_lifecycle_conf_exc(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(
            snapshot.transform.key_value("ArgumentValue", value_replacement="datetime")
        )
        lfc = {"Rules": []}
        with pytest.raises(ClientError) as e:
            lfc["Rules"] = [
                {
                    "Expiration": {"Days": 7},
                    "Status": "Enabled",
                }
            ]
            aws_client.s3.put_bucket_lifecycle_configuration(
                Bucket=s3_bucket, LifecycleConfiguration=lfc
            )
        snapshot.match("missing-id", e.value.response)

        with pytest.raises(ClientError) as e:
            lfc["Rules"] = [
                {
                    "Expiration": {"Days": 7},
                    "ID": "wholebucket",
                    "Status": "Enabled",
                }
            ]
            aws_client.s3.put_bucket_lifecycle_configuration(
                Bucket=s3_bucket, LifecycleConfiguration=lfc
            )
        snapshot.match("missing-filter", e.value.response)

        with pytest.raises(ClientError) as e:
            lfc["Rules"] = [
                {
                    "Expiration": {"Days": 7},
                    "Filter": {},
                    "ID": "wholebucket",
                    "Status": "Enabled",
                    "NoncurrentVersionExpiration": {},
                    # No NewerNoncurrentVersions or NoncurrentDays
                }
            ]
            aws_client.s3.put_bucket_lifecycle_configuration(
                Bucket=s3_bucket, LifecycleConfiguration=lfc
            )
        snapshot.match("missing-noncurrent-version-expiration-data", e.value.response)

        with pytest.raises(ClientError) as e:
            lfc["Rules"] = [
                {
                    "Expiration": {"Days": 7},
                    "Filter": {
                        "And": {
                            "Prefix": "test",
                        },
                        "Prefix": "",
                    },
                    "ID": "wholebucket",
                    "Status": "Enabled",
                }
            ]
            aws_client.s3.put_bucket_lifecycle_configuration(
                Bucket=s3_bucket, LifecycleConfiguration=lfc
            )
        snapshot.match("wrong-filter-and-plus-prefix", e.value.response)

        with pytest.raises(ClientError) as e:
            lfc["Rules"] = [
                {
                    "Expiration": {"Days": 7},
                    "Filter": {
                        "ObjectSizeGreaterThan": 500,
                        "Prefix": "",
                    },
                    "ID": "wholebucket",
                    "Status": "Enabled",
                }
            ]
            aws_client.s3.put_bucket_lifecycle_configuration(
                Bucket=s3_bucket, LifecycleConfiguration=lfc
            )
        snapshot.match("wrong-filter-and-and-object-size", e.value.response)

        with pytest.raises(ClientError) as e:
            lfc["Rules"] = [
                {
                    "Expiration": {
                        "Date": datetime.datetime(year=2023, month=1, day=1, hour=2, minute=2)
                    },
                    "ID": "wrong-data",
                    "Filter": {},
                    "Status": "Enabled",
                }
            ]
            aws_client.s3.put_bucket_lifecycle_configuration(
                Bucket=s3_bucket, LifecycleConfiguration=lfc
            )
        snapshot.match("wrong-data-no-midnight", e.value.response)

        with pytest.raises(ClientError) as e:
            lfc["Rules"] = [
                {
                    "ID": "duplicate-tag-keys",
                    "Filter": {
                        "And": {
                            "Tags": [
                                {
                                    "Key": "testlifecycle",
                                    "Value": "positive",
                                },
                                {
                                    "Key": "testlifecycle",
                                    "Value": "positive-two",
                                },
                            ],
                        },
                    },
                    "Status": "Enabled",
                    "Expiration": {"Days": 1},
                }
            ]
            aws_client.s3.put_bucket_lifecycle_configuration(
                Bucket=s3_bucket, LifecycleConfiguration=lfc
            )

        snapshot.match("duplicate-tag-keys", e.value.response)

        with pytest.raises(ClientError) as e:
            lfc["Rules"] = [
                {
                    "ID": "expired-delete-marker-and-days",
                    "Filter": {},
                    "Status": "Enabled",
                    "Expiration": {
                        "Days": 1,
                        "ExpiredObjectDeleteMarker": True,
                    },
                }
            ]
            aws_client.s3.put_bucket_lifecycle_configuration(
                Bucket=s3_bucket, LifecycleConfiguration=lfc
            )

        snapshot.match("expired-delete-marker-and-days", e.value.response)

    @markers.aws.validated
    def test_bucket_lifecycle_configuration_date(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("BucketName"),
            ]
        )
        rule_id = "rule_number_one"

        lfc = {
            "Rules": [
                {
                    "Expiration": {
                        "Date": datetime.datetime(year=2023, month=1, day=1, tzinfo=ZoneInfo("GMT"))
                    },
                    "ID": rule_id,
                    "Filter": {},
                    "Status": "Enabled",
                }
            ]
        }
        aws_client.s3.put_bucket_lifecycle_configuration(
            Bucket=s3_bucket, LifecycleConfiguration=lfc
        )
        result = aws_client.s3.get_bucket_lifecycle_configuration(Bucket=s3_bucket)
        snapshot.match("get-bucket-lifecycle-conf", result)

    @markers.aws.validated
    def test_bucket_lifecycle_configuration_object_expiry(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("BucketName"),
                snapshot.transform.key_value(
                    "Expiration", reference_replacement=False, value_replacement="<expiration>"
                ),
            ]
        )
        rule_id = "rule_number_one"

        lfc = {
            "Rules": [
                {
                    "Expiration": {"Days": 7},
                    "ID": rule_id,
                    "Filter": {"Prefix": ""},
                    "Status": "Enabled",
                }
            ]
        }
        aws_client.s3.put_bucket_lifecycle_configuration(
            Bucket=s3_bucket, LifecycleConfiguration=lfc
        )
        result = aws_client.s3.get_bucket_lifecycle_configuration(Bucket=s3_bucket)
        snapshot.match("get-bucket-lifecycle-conf", result)

        key = "test-object-expiry"
        aws_client.s3.put_object(Body=b"test", Bucket=s3_bucket, Key=key)

        response = aws_client.s3.head_object(Bucket=s3_bucket, Key=key)
        snapshot.match("head-object-expiry", response)
        response = aws_client.s3.get_object(Bucket=s3_bucket, Key=key)
        snapshot.match("get-object-expiry", response)

        expiration = response["Expiration"]

        parsed_exp_date, parsed_exp_rule = parse_expiration_header(expiration)
        assert parsed_exp_rule == rule_id
        last_modified = response["LastModified"]

        # use a bit of margin for the 7 days expiration, as it can depend on the time of day, but at least we validate
        assert 6 <= (parsed_exp_date - last_modified).days <= 8

    @markers.aws.validated
    def test_bucket_lifecycle_configuration_object_expiry_versioned(
        self, s3_bucket, snapshot, aws_client
    ):
        snapshot.add_transformer(snapshot.transform.key_value("VersionId"), priority=-1)
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("BucketName"),
                snapshot.transform.key_value(
                    "Expiration", reference_replacement=False, value_replacement="<expiration>"
                ),
            ]
        )

        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        rule_id = "rule2"
        current_exp_days = 3
        non_current_exp_days = 1
        lfc = {
            "Rules": [
                {
                    "ID": rule_id,
                    "Status": "Enabled",
                    "Filter": {},
                    "Expiration": {"Days": current_exp_days},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": non_current_exp_days},
                }
            ]
        }
        aws_client.s3.put_bucket_lifecycle_configuration(
            Bucket=s3_bucket, LifecycleConfiguration=lfc
        )
        result = aws_client.s3.get_bucket_lifecycle_configuration(Bucket=s3_bucket)
        snapshot.match("get-bucket-lifecycle-conf", result)

        key = "test-object-expiry"
        put_object_1 = aws_client.s3.put_object(Body=b"test", Bucket=s3_bucket, Key=key)
        version_id_1 = put_object_1["VersionId"]

        response = aws_client.s3.head_object(Bucket=s3_bucket, Key=key)
        snapshot.match("head-object-expiry", response)

        parsed_exp_date, parsed_exp_rule = parse_expiration_header(response["Expiration"])
        assert parsed_exp_rule == rule_id
        # use a bit of margin for the days expiration, as it can depend on the time of day, but at least we validate
        assert (
            current_exp_days - 1
            <= (parsed_exp_date - response["LastModified"]).days
            <= current_exp_days + 1
        )

        key = "test-object-expiry"
        put_object_2 = aws_client.s3.put_object(Body=b"test", Bucket=s3_bucket, Key=key)
        version_id_2 = put_object_2["VersionId"]

        response = aws_client.s3.head_object(Bucket=s3_bucket, Key=key, VersionId=version_id_1)
        snapshot.match("head-object-expiry-noncurrent", response)

        # This is not in the documentation anymore, but it still seems to be the case
        # See https://stackoverflow.com/questions/33096697/object-expiration-of-non-current-version
        # Note that for versioning-enabled buckets, this header applies only to current versions; Amazon S3 does not
        # provide a header to infer when a noncurrent version will be eligible for permanent deletion.
        assert "Expiration" not in response

        # if you specify the VersionId, AWS won't return the Expiration header, even if that's the current version
        response = aws_client.s3.head_object(Bucket=s3_bucket, Key=key, VersionId=version_id_2)
        snapshot.match("head-object-expiry-current-with-version-id", response)
        assert "Expiration" not in response

        response = aws_client.s3.head_object(Bucket=s3_bucket, Key=key)
        snapshot.match("head-object-expiry-current-without-version-id", response)
        # assert that the previous version id which didn't return the Expiration header is the same object
        assert response["VersionId"] == version_id_2

        parsed_exp_date, parsed_exp_rule = parse_expiration_header(response["Expiration"])
        assert parsed_exp_rule == rule_id
        # use a bit of margin for the days expiration, as it can depend on the time of day, but at least we validate
        assert (
            current_exp_days - 1
            <= (parsed_exp_date - response["LastModified"]).days
            <= current_exp_days + 1
        )

    @markers.aws.validated
    def test_object_expiry_after_bucket_lifecycle_configuration(
        self, s3_bucket, snapshot, aws_client
    ):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("BucketName"),
                snapshot.transform.key_value(
                    "Expiration", reference_replacement=False, value_replacement="<expiration>"
                ),
            ]
        )
        key = "test-object-expiry"
        put_object = aws_client.s3.put_object(Body=b"test", Bucket=s3_bucket, Key=key)
        snapshot.match("put-object-before", put_object)

        rule_id = "rule3"
        current_exp_days = 7
        lfc = {
            "Rules": [
                {
                    "Expiration": {"Days": current_exp_days},
                    "ID": rule_id,
                    "Filter": {},
                    "Status": "Enabled",
                }
            ]
        }
        aws_client.s3.put_bucket_lifecycle_configuration(
            Bucket=s3_bucket, LifecycleConfiguration=lfc
        )
        result = aws_client.s3.get_bucket_lifecycle_configuration(Bucket=s3_bucket)
        snapshot.match("get-bucket-lifecycle-conf", result)

        response = aws_client.s3.head_object(Bucket=s3_bucket, Key=key)
        snapshot.match("head-object-expiry-before", response)

        put_object = aws_client.s3.put_object(Body=b"test", Bucket=s3_bucket, Key=key)
        snapshot.match("put-object-after", put_object)

        response = aws_client.s3.head_object(Bucket=s3_bucket, Key=key)
        snapshot.match("head-object-expiry-after", response)

    @markers.aws.validated
    def test_bucket_lifecycle_multiple_rules(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("BucketName"),
                snapshot.transform.key_value(
                    "Expiration", reference_replacement=False, value_replacement="<expiration>"
                ),
            ]
        )

        rule_id_1 = "rule_one"
        rule_id_2 = "rule_two"
        rule_id_3 = "rule_three"
        current_exp_days = 7
        lfc = {
            "Rules": [
                {
                    "ID": rule_id_1,
                    "Filter": {"Prefix": "testobject"},
                    "Status": "Enabled",
                    "Expiration": {"Days": current_exp_days},
                },
                {
                    "ID": rule_id_2,
                    "Filter": {"Prefix": "test"},
                    "Status": "Enabled",
                    "Expiration": {"Days": current_exp_days},
                },
                {
                    "ID": rule_id_3,
                    "Filter": {"Prefix": "t"},
                    "Status": "Enabled",
                    "Expiration": {"Days": current_exp_days},
                },
            ]
        }

        aws_client.s3.put_bucket_lifecycle_configuration(
            Bucket=s3_bucket, LifecycleConfiguration=lfc
        )
        result = aws_client.s3.get_bucket_lifecycle_configuration(Bucket=s3_bucket)
        snapshot.match("get-bucket-lifecycle-conf", result)

        key_match_1 = "testobject-expiry"
        put_object = aws_client.s3.put_object(Body=b"test", Bucket=s3_bucket, Key=key_match_1)
        snapshot.match("put-object-match-both-rules", put_object)

        _, parsed_exp_rule = parse_expiration_header(put_object["Expiration"])
        assert parsed_exp_rule == rule_id_1

        key_match_2 = "test-one-rule"
        put_object_2 = aws_client.s3.put_object(Body=b"test", Bucket=s3_bucket, Key=key_match_2)
        snapshot.match("put-object-match-rule-2", put_object_2)

        _, parsed_exp_rule = parse_expiration_header(put_object_2["Expiration"])
        assert parsed_exp_rule == rule_id_2

        key_no_match = "no-rules"
        put_object_3 = aws_client.s3.put_object(Body=b"test", Bucket=s3_bucket, Key=key_no_match)
        snapshot.match("put-object-no-match", put_object_3)
        assert "Expiration" not in put_object_3

    @markers.aws.validated
    def test_bucket_lifecycle_object_size_rules(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("BucketName"),
                snapshot.transform.key_value(
                    "Expiration", reference_replacement=False, value_replacement="<expiration>"
                ),
            ]
        )

        rule_id_1 = "rule_one"
        rule_id_2 = "rule_two"
        current_exp_days = 7
        lfc = {
            "Rules": [
                {
                    "ID": rule_id_1,
                    "Filter": {
                        "ObjectSizeGreaterThan": 20,
                    },
                    "Status": "Enabled",
                    "Expiration": {"Days": current_exp_days},
                },
                {
                    "ID": rule_id_2,
                    "Filter": {
                        "ObjectSizeLessThan": 10,
                    },
                    "Status": "Enabled",
                    "Expiration": {"Days": current_exp_days},
                },
            ]
        }

        aws_client.s3.put_bucket_lifecycle_configuration(
            Bucket=s3_bucket, LifecycleConfiguration=lfc
        )
        result = aws_client.s3.get_bucket_lifecycle_configuration(Bucket=s3_bucket)
        snapshot.match("get-bucket-lifecycle-conf", result)

        key_match_1 = "testobject-expiry"
        put_object = aws_client.s3.put_object(Body=b"a" * 22, Bucket=s3_bucket, Key=key_match_1)
        snapshot.match("put-object-match-rule-1", put_object)

        _, parsed_exp_rule = parse_expiration_header(put_object["Expiration"])
        assert parsed_exp_rule == rule_id_1

        key_match_2 = "test-one-rule"
        put_object_2 = aws_client.s3.put_object(Body=b"a" * 5, Bucket=s3_bucket, Key=key_match_2)
        snapshot.match("put-object-match-rule-2", put_object_2)

        _, parsed_exp_rule = parse_expiration_header(put_object_2["Expiration"])
        assert parsed_exp_rule == rule_id_2

        key_no_match = "no-rules"
        put_object_3 = aws_client.s3.put_object(Body=b"a" * 15, Bucket=s3_bucket, Key=key_no_match)
        snapshot.match("put-object-no-match", put_object_3)
        assert "Expiration" not in put_object_3

    @markers.aws.validated
    def test_bucket_lifecycle_tag_rules(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("BucketName"),
                snapshot.transform.key_value(
                    "Expiration", reference_replacement=False, value_replacement="<expiration>"
                ),
            ]
        )

        rule_id_1 = "rule_one"
        rule_id_2 = "rule_two"
        current_exp_days = 7
        lfc = {
            "Rules": [
                {
                    "ID": rule_id_1,
                    "Filter": {
                        "Tag": {
                            "Key": "testlifecycle",
                            "Value": "positive",
                        },
                    },
                    "Status": "Enabled",
                    "Expiration": {"Days": current_exp_days},
                },
                {
                    "ID": rule_id_2,
                    "Filter": {
                        "And": {
                            "Tags": [
                                {
                                    "Key": "testlifecycle",
                                    "Value": "positive",
                                },
                                {
                                    "Key": "testlifecycletwo",
                                    "Value": "positive-two",
                                },
                            ],
                        },
                    },
                    "Status": "Enabled",
                    "Expiration": {"Days": current_exp_days},
                },
            ]
        }

        aws_client.s3.put_bucket_lifecycle_configuration(
            Bucket=s3_bucket, LifecycleConfiguration=lfc
        )
        result = aws_client.s3.get_bucket_lifecycle_configuration(Bucket=s3_bucket)
        snapshot.match("get-bucket-lifecycle-conf", result)

        key_match_1 = "testobject-expiry"
        tag_set_match = "testlifecycle=positive&testlifecycletwo=positivetwo"
        put_object = aws_client.s3.put_object(
            Body=b"test", Bucket=s3_bucket, Key=key_match_1, Tagging=tag_set_match
        )
        snapshot.match("put-object-match-both-rules", put_object)

        get_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_match_1)
        snapshot.match("get-object-match-both-rules", get_object)

        _, parsed_exp_rule = parse_expiration_header(put_object["Expiration"])
        assert parsed_exp_rule == rule_id_1

        key_match_2 = "test-one-rule"
        tag_set_match_one = "testlifecycle=positive"
        put_object_2 = aws_client.s3.put_object(
            Body=b"test", Bucket=s3_bucket, Key=key_match_2, Tagging=tag_set_match_one
        )
        snapshot.match("put-object-match-rule-1", put_object_2)

        get_object_2 = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_match_2)
        snapshot.match("get-object-match-rule-1", get_object_2)

        _, parsed_exp_rule = parse_expiration_header(put_object_2["Expiration"])
        assert parsed_exp_rule == rule_id_1

        key_no_match = "no-rules"
        tag_set_no_match = "testlifecycle2=positivetwo"
        put_object_3 = aws_client.s3.put_object(
            Body=b"test", Bucket=s3_bucket, Key=key_no_match, Tagging=tag_set_no_match
        )
        snapshot.match("put-object-no-match", put_object_3)
        assert "Expiration" not in put_object_3

        get_object_3 = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_no_match)
        snapshot.match("get-object-no-match", get_object_3)

        key_no_tags = "no-tags"
        put_object_4 = aws_client.s3.put_object(Body=b"test", Bucket=s3_bucket, Key=key_no_tags)
        snapshot.match("put-object-no-tags", put_object_4)
        assert "Expiration" not in put_object_4

        get_object_4 = aws_client.s3.get_object(Bucket=s3_bucket, Key=key_no_tags)
        snapshot.match("get-object-no-tags", get_object_4)

    @markers.aws.validated
    def test_lifecycle_expired_object_delete_marker(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("BucketName"),
                snapshot.transform.key_value(
                    "Expiration", reference_replacement=False, value_replacement="<expiration>"
                ),
            ]
        )
        rule_id = "rule-marker"
        lfc = {
            "Rules": [
                {
                    "Expiration": {"ExpiredObjectDeleteMarker": True},
                    "ID": rule_id,
                    "Filter": {},
                    "Status": "Enabled",
                }
            ]
        }
        aws_client.s3.put_bucket_lifecycle_configuration(
            Bucket=s3_bucket, LifecycleConfiguration=lfc
        )
        result = aws_client.s3.get_bucket_lifecycle_configuration(Bucket=s3_bucket)
        snapshot.match("get-bucket-lifecycle-conf", result)

        key = "test-expired-object-delete-marker"
        put_object = aws_client.s3.put_object(Body=b"test", Bucket=s3_bucket, Key=key)
        snapshot.match("put-object", put_object)

        response = aws_client.s3.head_object(Bucket=s3_bucket, Key=key)
        snapshot.match("head-object", response)

    @markers.aws.validated
    def test_s3_transition_default_minimum_object_size(self, aws_client, s3_bucket, snapshot):
        lfc = {
            "Rules": [
                {
                    "Expiration": {"Days": 7},
                    "ID": "wholebucket",
                    "Filter": {"Prefix": ""},
                    "Status": "Enabled",
                }
            ]
        }
        put_lifecycle_varies = aws_client.s3.put_bucket_lifecycle_configuration(
            Bucket=s3_bucket,
            LifecycleConfiguration=lfc,
            TransitionDefaultMinimumObjectSize=TransitionDefaultMinimumObjectSize.varies_by_storage_class,
        )
        snapshot.match("varies-by-storage", put_lifecycle_varies)

        get_lifecycle_varies = aws_client.s3.get_bucket_lifecycle_configuration(Bucket=s3_bucket)
        snapshot.match("get-varies-by-storage", get_lifecycle_varies)

        put_lifecycle_default = aws_client.s3.put_bucket_lifecycle_configuration(
            Bucket=s3_bucket,
            LifecycleConfiguration=lfc,
        )
        snapshot.match("default", put_lifecycle_default)

        get_default = aws_client.s3.get_bucket_lifecycle_configuration(Bucket=s3_bucket)
        snapshot.match("get-default", get_default)

        put_lifecycle_all_storage = aws_client.s3.put_bucket_lifecycle_configuration(
            Bucket=s3_bucket,
            LifecycleConfiguration=lfc,
            TransitionDefaultMinimumObjectSize=TransitionDefaultMinimumObjectSize.all_storage_classes_128K,
        )
        snapshot.match("all-storage", put_lifecycle_all_storage)

        get_all_storage = aws_client.s3.get_bucket_lifecycle_configuration(Bucket=s3_bucket)
        snapshot.match("get-all-storage", get_all_storage)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_lifecycle_configuration(
                Bucket=s3_bucket,
                LifecycleConfiguration=lfc,
                TransitionDefaultMinimumObjectSize="value",
            )
        snapshot.match("bad-value", e.value.response)


class TestS3ObjectLockRetention:
    @markers.aws.validated
    def test_s3_object_retention_exc(self, aws_client, s3_create_bucket, snapshot):
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))
        s3_bucket_locked = s3_create_bucket(ObjectLockEnabledForBucket=True)

        current_year = datetime.datetime.now().year
        future_datetime = datetime.datetime(current_year + 5, 1, 1)

        # non-existing bucket
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_retention(
                Bucket=f"non-existing-bucket-{long_uid()}",
                Key="fake-key",
                Retention={"Mode": "GOVERNANCE", "RetainUntilDate": future_datetime},
            )
        snapshot.match("put-object-retention-no-bucket", e.value.response)

        # non-existing key
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_retention(
                Bucket=s3_bucket_locked,
                Key="non-existing-key",
                Retention={"Mode": "GOVERNANCE", "RetainUntilDate": future_datetime},
            )
        snapshot.match("put-object-retention-no-key", e.value.response)

        object_key = "test-key"
        put_obj_locked = aws_client.s3.put_object(
            Bucket=s3_bucket_locked, Key=object_key, Body="test"
        )
        version_id = put_obj_locked["VersionId"]
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object_retention(
                Bucket=s3_bucket_locked, Key=object_key, VersionId=version_id
            )
        snapshot.match("get-object-retention-never-set", e.value.response)

        # missing fields
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_retention(
                Bucket=s3_bucket_locked,
                Key=object_key,
                Retention={"Mode": "GOVERNANCE"},
                BypassGovernanceRetention=True,
            )
        snapshot.match("put-object-missing-retention-fields", e.value.response)

        # set a retention
        aws_client.s3.put_object_retention(
            Bucket=s3_bucket_locked,
            Key=object_key,
            Retention={"Mode": "GOVERNANCE", "RetainUntilDate": future_datetime},
        )

        # update a retention to be lower than the existing one without bypass
        earlier_datetime = future_datetime - datetime.timedelta(days=365)
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_retention(
                Bucket=s3_bucket_locked,
                Key=object_key,
                VersionId=version_id,
                Retention={"Mode": "GOVERNANCE", "RetainUntilDate": earlier_datetime},
            )
        snapshot.match("update-retention-no-bypass", e.value.response)

        # update a retention with date in the past
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_retention(
                Bucket=s3_bucket_locked,
                Key=object_key,
                VersionId=version_id,
                Retention={"Mode": "GOVERNANCE", "RetainUntilDate": datetime.datetime(2020, 1, 1)},
            )
        snapshot.match("update-retention-past-date", e.value.response)

        # update a retention with a bad ObjectLock Mode
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_retention(
                Bucket=s3_bucket_locked,
                Key=object_key,
                VersionId=version_id,
                Retention={"Mode": "BAD-VALUE", "RetainUntilDate": future_datetime},
            )
        snapshot.match("update-retention-bad-value", e.value.response)

        s3_bucket_basic = s3_create_bucket(ObjectLockEnabledForBucket=False)  # same as default
        aws_client.s3.put_object(Bucket=s3_bucket_basic, Key=object_key, Body="test")
        # put object retention in a object in bucket without lock configured
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_retention(
                Bucket=s3_bucket_basic,
                Key=object_key,
                Retention={"Mode": "GOVERNANCE", "RetainUntilDate": future_datetime},
            )
        snapshot.match("put-object-retention-regular-bucket", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object_retention(
                Bucket=s3_bucket_basic,
                Key=object_key,
            )
        snapshot.match("get-object-retention-regular-bucket", e.value.response)

    @markers.aws.validated
    def test_s3_object_retention(self, aws_client, s3_create_bucket, snapshot):
        snapshot.add_transformer(snapshot.transform.key_value("VersionId"))
        object_key = "test-retention-locked-object"

        s3_bucket_lock = s3_create_bucket(ObjectLockEnabledForBucket=True)
        put_obj_1 = aws_client.s3.put_object(Bucket=s3_bucket_lock, Key=object_key, Body="test")
        snapshot.match("put-obj-locked-1", put_obj_1)

        version_id = put_obj_1["VersionId"]

        response = aws_client.s3.put_object_retention(
            Bucket=s3_bucket_lock,
            Key=object_key,
            Retention={"Mode": "GOVERNANCE", "RetainUntilDate": datetime.datetime(2030, 1, 1)},
        )
        snapshot.match("put-object-retention-on-key-1", response)

        response = aws_client.s3.get_object_retention(Bucket=s3_bucket_lock, Key=object_key)
        snapshot.match("get-object-retention-on-key-1", response)

        head_object_locked = aws_client.s3.head_object(Bucket=s3_bucket_lock, Key=object_key)
        snapshot.match("head-object-locked", head_object_locked)

        # delete object with retention lock without bypass
        with pytest.raises(ClientError) as e:
            aws_client.s3.delete_object(Bucket=s3_bucket_lock, Key=object_key, VersionId=version_id)
        snapshot.match("delete-obj-locked", e.value.response)

        # delete object with retention lock with bypass
        response = aws_client.s3.delete_object(
            Bucket=s3_bucket_lock,
            Key=object_key,
            VersionId=version_id,
            BypassGovernanceRetention=True,
        )
        snapshot.match("delete-obj-locked-bypass", response)

        put_obj_2 = aws_client.s3.put_object(
            Bucket=s3_bucket_lock,
            Key=object_key,
            Body="test",
            ObjectLockMode="GOVERNANCE",
            ObjectLockRetainUntilDate=datetime.datetime(2030, 1, 1),
        )
        snapshot.match("put-obj-locked-2", put_obj_2)
        version_id = put_obj_2["VersionId"]

        # update object retention with 5 seconds retention, no bypass
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_retention(
                Bucket=s3_bucket_lock,
                Key=object_key,
                Retention={
                    "Mode": "GOVERNANCE",
                    "RetainUntilDate": datetime.datetime.now(tz=datetime.UTC)
                    + datetime.timedelta(seconds=5),
                },
            )
        snapshot.match("update-retention-locked-object", e.value.response)

        # update with empty retention
        empty_retention = aws_client.s3.put_object_retention(
            Bucket=s3_bucket_lock,
            Key=object_key,
            Retention={},
            BypassGovernanceRetention=True,
        )
        snapshot.match("put-object-empty-retention", empty_retention)

        update_retention = aws_client.s3.put_object_retention(
            Bucket=s3_bucket_lock,
            Key=object_key,
            Retention={
                "Mode": "GOVERNANCE",
                "RetainUntilDate": datetime.datetime.now(tz=datetime.UTC)
                + datetime.timedelta(seconds=5),
            },
        )
        snapshot.match("update-retention-object", update_retention)

        # delete object with retention lock without bypass before 5 seconds
        with pytest.raises(ClientError):
            aws_client.s3.delete_object(Bucket=s3_bucket_lock, Key=object_key, VersionId=version_id)

        # delete object with lock without bypass after 5 seconds
        sleep = 10 if is_aws_cloud() else 6
        time.sleep(sleep)

        aws_client.s3.delete_object(
            Bucket=s3_bucket_lock,
            Key=object_key,
            VersionId=version_id,
        )

    @markers.aws.validated
    def test_s3_copy_object_retention_lock(self, s3_create_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = "source-object"
        dest_key = "dest-key"
        # creating a bucket with ObjectLockEnabledForBucket enables versioning by default, as it's not allowed otherwise
        # see https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-overview.html
        bucket_name = s3_create_bucket(ObjectLockEnabledForBucket=True)

        put_locked_objected = aws_client.s3.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body='{"key": "value"}',
            ObjectLockMode="GOVERNANCE",  # allows the root user to delete it
            ObjectLockRetainUntilDate=datetime.datetime.now() + datetime.timedelta(minutes=10),
        )
        snapshot.match("put-source-object", put_locked_objected)

        head_object = aws_client.s3.head_object(Bucket=bucket_name, Key=object_key)
        snapshot.match("head-source-object", head_object)

        resp = aws_client.s3.copy_object(
            Bucket=bucket_name,
            CopySource=f"{bucket_name}/{object_key}",
            Key=dest_key,
        )
        snapshot.match("copy-lock", resp)
        # the destination key did not keep the lock nor lock until from the source key
        head_object = aws_client.s3.head_object(Bucket=bucket_name, Key=dest_key)
        snapshot.match("head-dest-key", head_object)

    @markers.aws.validated
    def test_bucket_config_default_retention(self, s3_create_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("VersionId"))
        bucket_name = s3_create_bucket(ObjectLockEnabledForBucket=True)
        object_key = "default-object"
        put_lock_config = aws_client.s3.put_object_lock_configuration(
            Bucket=bucket_name,
            ObjectLockConfiguration={
                "ObjectLockEnabled": "Enabled",
                "Rule": {
                    "DefaultRetention": {
                        "Mode": "GOVERNANCE",
                        "Days": 1,
                    }
                },
            },
        )
        snapshot.match("put-lock-config", put_lock_config)

        put_locked_object_default = aws_client.s3.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body="test-default-lock",
        )
        snapshot.match("put-object-default", put_locked_object_default)

        head_object = aws_client.s3.head_object(Bucket=bucket_name, Key=object_key)
        snapshot.match("head-object-default", head_object)

        # add one day to LastModified to validate the Retain date is precise or rounding (it is precise, exactly 1 day
        # after the LastModified (created date)
        last_modified_and_one_day = head_object["LastModified"] + datetime.timedelta(days=1)
        delta_2_min = datetime.timedelta(minutes=2)  # to add a bit of margin
        assert (
            last_modified_and_one_day - delta_2_min
            <= head_object["ObjectLockRetainUntilDate"]
            <= last_modified_and_one_day + delta_2_min
        )

        put_locked_object = aws_client.s3.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body="test-put-object-lock",
            ObjectLockMode="GOVERNANCE",
            ObjectLockRetainUntilDate=datetime.datetime.now() + datetime.timedelta(minutes=10),
        )
        snapshot.match("put-object-with-lock", put_locked_object)

        head_object = aws_client.s3.head_object(Bucket=bucket_name, Key=object_key)
        snapshot.match("head-object-with-lock", head_object)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=bucket_name,
                Key=object_key + "2",
                Body="test-put-object-lock",
                ObjectLockMode="GOVERNANCE",
            )
        snapshot.match("put-object-with-lock-no-date", e.value.response)

    @markers.aws.validated
    def test_object_lock_delete_markers(self, s3_create_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("VersionId"))
        bucket_name = s3_create_bucket(ObjectLockEnabledForBucket=True)
        object_key = "default-object"

        put_locked_object = aws_client.s3.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body="test-put-object-lock",
            ObjectLockMode="GOVERNANCE",
            ObjectLockRetainUntilDate=datetime.datetime.now() + datetime.timedelta(minutes=10),
        )
        snapshot.match("put-object-with-lock", put_locked_object)

        head_object = aws_client.s3.head_object(Bucket=bucket_name, Key=object_key)
        snapshot.match("head-object-with-lock", head_object)

        put_delete_marker = aws_client.s3.delete_object(Bucket=bucket_name, Key=object_key)
        snapshot.match("put-delete-marker", put_delete_marker)
        delete_marker_version = put_delete_marker["VersionId"]

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_retention(
                Bucket=bucket_name,
                Key=object_key,
                VersionId=delete_marker_version,
                Retention={"Mode": "GOVERNANCE", "RetainUntilDate": datetime.datetime(2030, 1, 1)},
            )
        snapshot.match("put-object-retention-delete-marker", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object_retention(
                Bucket=bucket_name, Key=object_key, VersionId=delete_marker_version
            )
        snapshot.match("get-object-retention-delete-marker", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.head_object(
                Bucket=bucket_name, Key=object_key, VersionId=delete_marker_version
            )
        snapshot.match("head-object-locked-delete-marker", e.value.response)

    @markers.aws.validated
    def test_object_lock_extend_duration(self, s3_create_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("VersionId"))
        bucket_name = s3_create_bucket(ObjectLockEnabledForBucket=True)
        object_key = "default-object"

        put_locked_object = aws_client.s3.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body="test-put-object-lock",
            ObjectLockMode="GOVERNANCE",
            ObjectLockRetainUntilDate=datetime.datetime.now() + datetime.timedelta(minutes=10),
        )
        snapshot.match("put-object-with-lock", put_locked_object)
        version_id = put_locked_object["VersionId"]

        head_object = aws_client.s3.head_object(Bucket=bucket_name, Key=object_key)
        snapshot.match("head-object-with-lock", head_object)

        # not putting BypassGovernanceRetention=True on purpose, to see if you can extend the duration by default
        put_locked_object_extend = aws_client.s3.put_object_retention(
            Bucket=bucket_name,
            Key=object_key,
            VersionId=version_id,
            Retention={
                "Mode": "GOVERNANCE",
                "RetainUntilDate": datetime.datetime.now() + datetime.timedelta(minutes=20),
            },
        )
        snapshot.match("put-object-retention-extend", put_locked_object_extend)

        head_object = aws_client.s3.head_object(Bucket=bucket_name, Key=object_key)
        snapshot.match("head-object-with-lock-extended", head_object)

        # assert that reducing the duration again won't work
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_retention(
                Bucket=bucket_name,
                Key=object_key,
                VersionId=version_id,
                Retention={
                    "Mode": "GOVERNANCE",
                    "RetainUntilDate": datetime.datetime.now() + datetime.timedelta(minutes=10),
                },
            )
        snapshot.match("put-object-retention-reduce", e.value.response)

    @markers.aws.validated
    def test_s3_object_retention_compliance_mode(self, aws_client, s3_create_bucket, snapshot):
        # BEWARE of this test!
        # using `COMPLIANCE` will make the object virtually *impossible* to delete, so don't set a long duration
        # for the `RetainUntilDate`
        # only way to delete the object and indirectly the bucket will be to delete the AWS Account
        # see https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html#object-lock-overview
        # > The only way to delete an object under the compliance mode before its retention date expires is to delete
        # > the associated AWS account.
        snapshot.add_transformer(snapshot.transform.key_value("VersionId"))
        object_key = "test-retention-locked-object"

        s3_bucket_lock = s3_create_bucket(ObjectLockEnabledForBucket=True)
        put_obj_1 = aws_client.s3.put_object(Bucket=s3_bucket_lock, Key=object_key, Body="test")
        snapshot.match("put-obj-locked-1", put_obj_1)

        version_id = put_obj_1["VersionId"]

        short_future = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(seconds=5)

        update_retention = aws_client.s3.put_object_retention(
            Bucket=s3_bucket_lock,
            Key=object_key,
            Retention={
                "Mode": "COMPLIANCE",
                "RetainUntilDate": short_future,
            },
        )
        snapshot.match("add-compliance-retention", update_retention)

        # delete object with retention lock without bypass before 5 seconds
        with pytest.raises(ClientError) as e:
            aws_client.s3.delete_object(Bucket=s3_bucket_lock, Key=object_key, VersionId=version_id)
        snapshot.match("delete-locked-1", e.value.response)

        put_delete_marker = aws_client.s3.delete_object(Bucket=s3_bucket_lock, Key=object_key)
        snapshot.match("put-delete-marker", put_delete_marker)

        # delete object with retention lock with bypass before 5 seconds
        with pytest.raises(ClientError) as e:
            aws_client.s3.delete_object(
                Bucket=s3_bucket_lock,
                Key=object_key,
                VersionId=version_id,
                BypassGovernanceRetention=True,
            )
        snapshot.match("delete-locked-2", e.value.response)

        # update a retention to be lower than the existing one without bypass
        earlier_datetime = short_future - datetime.timedelta(seconds=1)
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_retention(
                Bucket=s3_bucket_lock,
                Key=object_key,
                VersionId=version_id,
                Retention={"Mode": "COMPLIANCE", "RetainUntilDate": earlier_datetime},
            )
        snapshot.match("update-retention-shortened", e.value.response)

        # update a retention to be less restrictive than COMPLIANCE
        earlier_datetime = short_future + datetime.timedelta(seconds=1)
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_retention(
                Bucket=s3_bucket_lock,
                Key=object_key,
                VersionId=version_id,
                Retention={"Mode": "GOVERNANCE", "RetainUntilDate": earlier_datetime},
            )
        snapshot.match("update-retention-less-restrictive", e.value.response)

        # delete object with lock without bypass after 5 seconds
        sleep = 10 if is_aws_cloud() else 6
        time.sleep(sleep)

        response = aws_client.s3.delete_object(
            Bucket=s3_bucket_lock,
            Key=object_key,
            VersionId=version_id,
        )
        snapshot.match("delete-obj-after-lock-expiration", response)

    @markers.aws.validated
    def test_s3_object_lock_mode_validation(self, aws_client, s3_create_bucket, snapshot):
        snapshot.add_transformer(snapshot.transform.key_value("VersionId"))
        object_key = "test-retention-validation"

        s3_bucket_lock = s3_create_bucket(ObjectLockEnabledForBucket=True)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=s3_bucket_lock,
                Key=object_key,
                Body="test",
                ObjectLockMode="BAD-VALUE",
            )
        snapshot.match("put-obj-locked-error-no-retain-date", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=s3_bucket_lock,
                Key=object_key,
                Body="test",
                ObjectLockRetainUntilDate=datetime.datetime.now() + datetime.timedelta(minutes=10),
            )
        snapshot.match("put-obj-locked-error-no-mode", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=s3_bucket_lock,
                Key=object_key,
                Body="test",
                ObjectLockMode="BAD-VALUE",
                ObjectLockRetainUntilDate=datetime.datetime.now() + datetime.timedelta(minutes=10),
            )
        snapshot.match("put-obj-locked-bad-value", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.create_multipart_upload(
                Bucket=s3_bucket_lock,
                Key=object_key,
                ObjectLockMode="BAD-VALUE",
                ObjectLockRetainUntilDate=datetime.datetime.now() + datetime.timedelta(minutes=10),
            )
        snapshot.match("create-mpu-locked-bad-value", e.value.response)


class TestS3ObjectLockLegalHold:
    @markers.aws.validated
    def test_put_get_object_legal_hold(self, s3_create_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("VersionId"))
        object_key = "locked-object"
        bucket_name = s3_create_bucket(ObjectLockEnabledForBucket=True)

        put_obj = aws_client.s3.put_object(Bucket=bucket_name, Key=object_key, Body="test")
        snapshot.match("put-obj", put_obj)
        version_id = put_obj["VersionId"]

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object_legal_hold(
                Bucket=bucket_name, Key=object_key, VersionId=version_id
            )
        snapshot.match("get-legal-hold-unset", e.value.response)

        put_legal_hold = aws_client.s3.put_object_legal_hold(
            Bucket=bucket_name,
            Key=object_key,
            VersionId=version_id,
            LegalHold={"Status": "ON"},
        )
        snapshot.match("put-object-legal-hold", put_legal_hold)

        head_object = aws_client.s3.head_object(Bucket=bucket_name, Key=object_key)
        snapshot.match("head-object-with-legal-hold", head_object)

        get_legal_hold = aws_client.s3.get_object_legal_hold(
            Bucket=bucket_name, Key=object_key, VersionId=version_id
        )
        snapshot.match("get-legal-hold-set", get_legal_hold)

        # disable the LegalHold so that the fixture can clean up
        put_legal_hold = aws_client.s3.put_object_legal_hold(
            Bucket=bucket_name,
            Key=object_key,
            VersionId=version_id,
            LegalHold={"Status": "OFF"},
        )
        snapshot.match("put-object-legal-hold-off", put_legal_hold)

    @markers.aws.validated
    def test_put_object_with_legal_hold(self, s3_create_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("VersionId"))
        object_key = "locked-object"
        bucket_name = s3_create_bucket(ObjectLockEnabledForBucket=True)

        put_obj = aws_client.s3.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body="test",
            ObjectLockLegalHoldStatus="ON",
        )
        snapshot.match("put-obj", put_obj)
        version_id = put_obj["VersionId"]

        head_object = aws_client.s3.head_object(Bucket=bucket_name, Key=object_key)
        snapshot.match("head-object-with-legal-hold", head_object)

        # disable the LegalHold so that the fixture can clean up
        put_legal_hold = aws_client.s3.put_object_legal_hold(
            Bucket=bucket_name,
            Key=object_key,
            VersionId=version_id,
            LegalHold={"Status": "OFF"},
        )
        snapshot.match("put-object-legal-hold-off", put_legal_hold)

    @markers.aws.validated
    def test_put_object_legal_hold_exc(self, s3_create_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("BucketName"))
        s3_bucket_locked = s3_create_bucket(ObjectLockEnabledForBucket=True)
        # non-existing bucket
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_legal_hold(
                Bucket=f"non-existing-bucket-{long_uid()}",
                Key="fake-key",
                LegalHold={"Status": "ON"},
            )
        snapshot.match("put-object-legal-hold-no-bucket", e.value.response)

        # non-existing key
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_legal_hold(
                Bucket=s3_bucket_locked,
                Key="non-existing-key",
                LegalHold={"Status": "ON"},
            )
        snapshot.match("put-object-legal-hold-no-key", e.value.response)

        object_key = "test-legal-hold"
        s3_bucket_basic = s3_create_bucket(ObjectLockEnabledForBucket=False)  # same as default
        aws_client.s3.put_object(Bucket=s3_bucket_basic, Key=object_key, Body="test")
        # put object retention in a object in bucket without lock configured
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_legal_hold(
                Bucket=s3_bucket_basic,
                Key=object_key,
                LegalHold={"Status": "ON"},
            )
        snapshot.match("put-object-retention-regular-bucket", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object_legal_hold(
                Bucket=s3_bucket_basic,
                Key=object_key,
            )
        snapshot.match("put-object-retention-empty", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object_legal_hold(
                Bucket=s3_bucket_basic,
                Key=object_key,
            )
        snapshot.match("get-object-retention-regular-bucket", e.value.response)

    @markers.aws.validated
    def test_delete_locked_object(self, s3_create_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.key_value("VersionId"))
        bucket_name = s3_create_bucket(ObjectLockEnabledForBucket=True)
        object_key = "test-delete-locked"
        put_obj = aws_client.s3.put_object(Bucket=bucket_name, Key=object_key, Body="test")
        snapshot.match("put-obj", put_obj)
        version_id = put_obj["VersionId"]

        put_legal_hold = aws_client.s3.put_object_legal_hold(
            Bucket=bucket_name,
            Key=object_key,
            VersionId=version_id,
            LegalHold={"Status": "ON"},
        )
        snapshot.match("put-object-legal-hold", put_legal_hold)

        with pytest.raises(ClientError) as e:
            aws_client.s3.delete_object(Bucket=bucket_name, Key=object_key, VersionId=version_id)
        snapshot.match("delete-object-locked", e.value.response)

        delete_objects = aws_client.s3.delete_objects(
            Bucket=bucket_name, Delete={"Objects": [{"Key": object_key, "VersionId": version_id}]}
        )
        snapshot.match("delete-objects-locked", delete_objects)

        # disable the LegalHold so that the fixture can clean up
        put_legal_hold = aws_client.s3.put_object_legal_hold(
            Bucket=bucket_name,
            Key=object_key,
            VersionId=version_id,
            LegalHold={"Status": "OFF"},
        )
        snapshot.match("put-object-legal-hold-off", put_legal_hold)

    @markers.aws.validated
    def test_s3_legal_hold_lock_versioned(self, aws_client, s3_create_bucket, snapshot):
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = "locked-object"
        # creating a bucket with ObjectLockEnabledForBucket enables versioning by default, as it's not allowed otherwise
        bucket_name = s3_create_bucket(ObjectLockEnabledForBucket=True)

        # create an object, get version1
        resp = aws_client.s3.put_object(Bucket=bucket_name, Key=object_key, Body="test")
        snapshot.match("put-object", resp)
        version_id = resp["VersionId"]

        # put a legal hold on the object with version1
        resp = aws_client.s3.put_object_legal_hold(
            Bucket=bucket_name,
            Key=object_key,
            VersionId=version_id,
            LegalHold={"Status": "ON"},
        )
        snapshot.match("put-object-legal-hold-ver1", resp)

        head_object = aws_client.s3.head_object(
            Bucket=bucket_name, Key=object_key, VersionId=version_id
        )
        snapshot.match("head-object-ver1", head_object)

        resp = aws_client.s3.put_object(Bucket=bucket_name, Key=object_key, Body="test")
        snapshot.match("put-object-2", resp)
        version_id_2 = resp["VersionId"]

        # put a legal hold on the object with version2
        resp = aws_client.s3.put_object_legal_hold(
            Bucket=bucket_name,
            Key=object_key,
            VersionId=version_id_2,
            LegalHold={"Status": "ON"},
        )
        snapshot.match("put-object-legal-hold-ver2", resp)

        head_object = aws_client.s3.head_object(
            Bucket=bucket_name, Key=object_key, VersionId=version_id_2
        )
        snapshot.match("head-object-ver2", head_object)

        # remove the legal hold from the version1
        resp = aws_client.s3.put_object_legal_hold(
            Bucket=bucket_name,
            Key=object_key,
            VersionId=version_id,
            LegalHold={"Status": "OFF"},
        )
        snapshot.match("remove-object-legal-hold-ver1", resp)

        head_object = aws_client.s3.head_object(
            Bucket=bucket_name, Key=object_key, VersionId=version_id
        )
        snapshot.match("head-object-ver1-no-lock", head_object)

        # now delete the object with version1, the legal hold should be off
        resp = aws_client.s3.delete_object(
            Bucket=bucket_name,
            Key=object_key,
            VersionId=version_id,
        )
        snapshot.match("delete-object-ver1", resp)

        # disabled the Legal Hold so that the fixture can clean up
        aws_client.s3.put_object_legal_hold(
            Bucket=bucket_name,
            Key=object_key,
            LegalHold={"Status": "OFF"},
            VersionId=version_id_2,
        )

    @markers.aws.validated
    def test_s3_copy_object_legal_hold(self, s3_create_bucket, snapshot, aws_client):
        snapshot.add_transformer(snapshot.transform.s3_api())
        object_key = "source-object"
        dest_key = "dest-key"
        # creating a bucket with ObjectLockEnabledForBucket enables versioning by default
        bucket_name = s3_create_bucket(ObjectLockEnabledForBucket=True)

        resp = aws_client.s3.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body='{"key": "value"}',
            ObjectLockLegalHoldStatus="ON",
        )
        snapshot.match("put-object", resp)

        head_object = aws_client.s3.head_object(Bucket=bucket_name, Key=object_key)
        snapshot.match("head-object", head_object)

        resp = aws_client.s3.copy_object(
            Bucket=bucket_name,
            CopySource=f"{bucket_name}/{object_key}",
            Key=dest_key,
        )
        snapshot.match("copy-legal-hold", resp)
        # the destination key did not keep the legal hold from the source key
        head_object = aws_client.s3.head_object(Bucket=bucket_name, Key=dest_key)
        snapshot.match("head-dest-key", head_object)

        # disable the Legal Hold so that the fixture can clean up
        for key in (object_key, dest_key):
            with contextlib.suppress(ClientError):
                aws_client.s3.put_object_legal_hold(
                    Bucket=bucket_name, Key=key, LegalHold={"Status": "OFF"}
                )


class TestS3BucketLogging:
    @markers.aws.validated
    def test_put_bucket_logging(self, aws_client, s3_create_bucket, snapshot):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("TargetBucket"),
                snapshot.transform.key_value("DisplayName", reference_replacement=False),
                snapshot.transform.key_value(
                    "ID", value_replacement="owner-id", reference_replacement=False
                ),
            ]
        )

        bucket_name = s3_create_bucket()
        target_bucket = s3_create_bucket()

        resp = aws_client.s3.get_bucket_logging(Bucket=bucket_name)
        snapshot.match("get-bucket-logging-default", resp)

        bucket_logging_status = {
            "LoggingEnabled": {
                "TargetBucket": target_bucket,
                "TargetPrefix": "log",
            },
        }
        resp = aws_client.s3.get_bucket_acl(Bucket=target_bucket)
        snapshot.match("get-bucket-default-acl", resp)

        # this might have been failing in the past, as the target bucket does not give access to LogDelivery to
        # write/read_acp. however, AWS accepts it, because you can also set it with Permissions
        resp = aws_client.s3.put_bucket_logging(
            Bucket=bucket_name, BucketLoggingStatus=bucket_logging_status
        )
        snapshot.match("put-bucket-logging", resp)

        resp = aws_client.s3.get_bucket_logging(Bucket=bucket_name)
        snapshot.match("get-bucket-logging", resp)

        # delete BucketLogging
        resp = aws_client.s3.put_bucket_logging(Bucket=bucket_name, BucketLoggingStatus={})
        snapshot.match("put-bucket-logging-delete", resp)

    @markers.aws.validated
    def test_put_bucket_logging_accept_wrong_grants(self, aws_client, s3_create_bucket, snapshot):
        snapshot.add_transformer(snapshot.transform.key_value("TargetBucket"))

        bucket_name = s3_create_bucket()

        target_bucket = s3_create_bucket()
        # We need to delete the ObjectOwnership from the bucket, because you otherwise can't set TargetGrants on it
        # TODO: have the same default as AWS and have ObjectOwnership set
        aws_client.s3.delete_bucket_ownership_controls(Bucket=target_bucket)

        bucket_logging_status = {
            "LoggingEnabled": {
                "TargetBucket": target_bucket,
                "TargetPrefix": "log",
                "TargetGrants": [
                    {
                        "Grantee": {
                            "URI": "http://acs.amazonaws.com/groups/s3/LogDelivery",
                            "Type": "Group",
                        },
                        "Permission": "WRITE",
                    },
                    {
                        "Grantee": {
                            "URI": "http://acs.amazonaws.com/groups/s3/LogDelivery",
                            "Type": "Group",
                        },
                        "Permission": "READ_ACP",
                    },
                ],
            },
        }

        # from the documentation, only WRITE | READ | FULL_CONTROL are allowed, but AWS let READ_ACP pass
        resp = aws_client.s3.put_bucket_logging(
            Bucket=bucket_name, BucketLoggingStatus=bucket_logging_status
        )
        snapshot.match("put-bucket-logging", resp)

        resp = aws_client.s3.get_bucket_logging(Bucket=bucket_name)
        snapshot.match("get-bucket-logging", resp)

    @markers.aws.validated
    def test_put_bucket_logging_wrong_target(
        self,
        aws_client_factory,
        s3_create_bucket_with_client,
        snapshot,
    ):
        region_us_west_2 = "us-west-2"
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("TargetBucket"),
                snapshot.transform.regex(AWS_REGION_US_EAST_1, "<region_1>"),
                snapshot.transform.regex(region_us_west_2, "<region_2>"),
            ]
        )

        bucket_name_us_east_1 = f"bucket-{short_uid()}"
        target_bucket_us_west_2 = f"bucket-{short_uid()}"

        client_us_east_1 = aws_client_factory(region_name=AWS_REGION_US_EAST_1).s3
        s3_create_bucket_with_client(
            client_us_east_1,
            Bucket=bucket_name_us_east_1,
        )
        s3_create_bucket_with_client(
            client_us_east_1,
            Bucket=target_bucket_us_west_2,
            CreateBucketConfiguration={"LocationConstraint": region_us_west_2},
        )

        with pytest.raises(ClientError) as e:
            bucket_logging_status = {
                "LoggingEnabled": {
                    "TargetBucket": target_bucket_us_west_2,
                    "TargetPrefix": "log",
                },
            }
            client_us_east_1.put_bucket_logging(
                Bucket=bucket_name_us_east_1, BucketLoggingStatus=bucket_logging_status
            )
        snapshot.match("put-bucket-logging-different-regions", e.value.response)

        nonexistent_target_bucket = f"target-bucket-{long_uid()}"
        with pytest.raises(ClientError) as e:
            bucket_logging_status = {
                "LoggingEnabled": {
                    "TargetBucket": nonexistent_target_bucket,
                    "TargetPrefix": "log",
                },
            }
            client_us_east_1.put_bucket_logging(
                Bucket=bucket_name_us_east_1, BucketLoggingStatus=bucket_logging_status
            )
        snapshot.match("put-bucket-logging-non-existent-bucket", e.value.response)
        assert e.value.response["Error"]["TargetBucket"] == nonexistent_target_bucket

    @markers.aws.validated
    def test_put_bucket_logging_cross_locations(
        self,
        aws_client,
        aws_client_factory,
        s3_create_bucket,
        s3_create_bucket_with_client,
        snapshot,
    ):
        # The aim of the test is to check the behavior of the CrossLocationLoggingProhibitions
        # exception for us-east-1 and regions other than us-east-1.
        region_us_east_2 = "us-east-2"
        region_us_west_2 = "us-west-2"
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("TargetBucket"),
                snapshot.transform.regex(AWS_REGION_US_EAST_1, "<region_1>"),
                snapshot.transform.regex(region_us_east_2, "<region_2>"),
                snapshot.transform.regex(region_us_west_2, "<region_3>"),
            ]
        )

        bucket_name_us_east_1 = f"bucket-{short_uid()}"
        client_us_east_1 = aws_client_factory(region_name=AWS_REGION_US_EAST_1).s3
        s3_create_bucket_with_client(s3_client=client_us_east_1, Bucket=bucket_name_us_east_1)

        bucket_name_us_east_2 = f"bucket-{short_uid()}"
        s3_create_bucket_with_client(
            s3_client=client_us_east_1,
            Bucket=bucket_name_us_east_2,
            CreateBucketConfiguration={"LocationConstraint": region_us_east_2},
        )

        target_bucket_us_west_2 = f"bucket-{short_uid()}"
        s3_create_bucket_with_client(
            s3_client=client_us_east_1,
            Bucket=target_bucket_us_west_2,
            CreateBucketConfiguration={"LocationConstraint": region_us_west_2},
        )

        with pytest.raises(ClientError) as e:
            bucket_logging_status = {
                "LoggingEnabled": {
                    "TargetBucket": target_bucket_us_west_2,
                    "TargetPrefix": "log",
                },
            }
            client_us_east_1.put_bucket_logging(
                Bucket=bucket_name_us_east_1, BucketLoggingStatus=bucket_logging_status
            )
        snapshot.match("put-bucket-logging-cross-us-east-1", e.value.response)

        with pytest.raises(ClientError) as e:
            bucket_logging_status = {
                "LoggingEnabled": {
                    "TargetBucket": target_bucket_us_west_2,
                    "TargetPrefix": "log",
                },
            }
            client_us_east_1.put_bucket_logging(
                Bucket=bucket_name_us_east_2, BucketLoggingStatus=bucket_logging_status
            )
        snapshot.match("put-bucket-logging-different-regions", e.value.response)


# TODO: maybe we can fake the IAM role as it's not needed in LocalStack
@pytest.mark.skipif(condition=TEST_S3_IMAGE, reason="IAM not enabled in S3 image")
class TestS3BucketReplication:
    @markers.aws.validated
    def test_replication_config_without_filter(
        self, s3_create_bucket, create_iam_role_with_policy, snapshot, aws_client
    ):
        snapshot.add_transformer(snapshot.transform.s3_api())
        snapshot.add_transformer(
            snapshot.transform.jsonpath(
                "$..ReplicationConfiguration.Role", "role", reference_replacement=False
            )
        )
        snapshot.add_transformer(
            snapshot.transform.jsonpath(
                "$..Destination.Bucket", "dest-bucket", reference_replacement=False
            )
        )
        bucket_src = f"src-{short_uid()}"
        bucket_dst = f"dst-{short_uid()}"
        role_name = f"replication_role_{short_uid()}"
        policy_name = f"replication_policy_{short_uid()}"

        role_arn = create_iam_role_with_policy(
            RoleName=role_name,
            PolicyName=policy_name,
            RoleDefinition=S3_ASSUME_ROLE_POLICY,
            PolicyDefinition=S3_POLICY,
        )
        s3_create_bucket(Bucket=bucket_src)
        # enable versioning on src
        aws_client.s3.put_bucket_versioning(
            Bucket=bucket_src, VersioningConfiguration={"Status": "Enabled"}
        )

        s3_create_bucket(Bucket=bucket_dst)

        replication_config = {
            "Role": role_arn,
            "Rules": [
                {
                    "ID": "rtc",
                    "Priority": 0,
                    "Filter": {},
                    "Status": "Disabled",
                    "Destination": {
                        "Bucket": "arn:aws:s3:::does-not-exist",
                        "StorageClass": "STANDARD",
                        "ReplicationTime": {"Status": "Enabled", "Time": {"Minutes": 15}},
                        "Metrics": {"Status": "Enabled", "EventThreshold": {"Minutes": 15}},
                    },
                    "DeleteMarkerReplication": {"Status": "Disabled"},
                }
            ],
        }
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_replication(
                ReplicationConfiguration=replication_config, Bucket=bucket_src
            )
        snapshot.match("expected_error_dest_does_not_exist", e.value.response)

        # set correct destination
        replication_config["Rules"][0]["Destination"]["Bucket"] = f"arn:aws:s3:::{bucket_dst}"

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_replication(
                ReplicationConfiguration=replication_config, Bucket=bucket_src
            )
        snapshot.match("expected_error_dest_versioning_disabled", e.value.response)

        # enable versioning on destination bucket
        aws_client.s3.put_bucket_versioning(
            Bucket=bucket_dst, VersioningConfiguration={"Status": "Enabled"}
        )

        response = aws_client.s3.put_bucket_replication(
            ReplicationConfiguration=replication_config, Bucket=bucket_src
        )
        snapshot.match("put-bucket-replication", response)

        response = aws_client.s3.get_bucket_replication(Bucket=bucket_src)
        snapshot.match("get-bucket-replication", response)

    @markers.aws.validated
    def test_replication_config(
        self,
        s3_create_bucket,
        s3_create_bucket_with_client,
        create_iam_role_with_policy,
        snapshot,
        aws_client,
        aws_client_factory,
    ):
        snapshot.add_transformer(snapshot.transform.s3_api())
        snapshot.add_transformer(
            snapshot.transform.jsonpath(
                "$..ReplicationConfiguration.Role", "role", reference_replacement=False
            )
        )
        snapshot.add_transformer(
            snapshot.transform.jsonpath(
                "$..Destination.Bucket", "dest-bucket", reference_replacement=False
            )
        )
        snapshot.add_transformer(
            snapshot.transform.key_value("ID", "id", reference_replacement=False)
        )
        bucket_src = f"src-{short_uid()}"
        bucket_dst = f"dst-{short_uid()}"
        role_name = f"replication_role_{short_uid()}"
        policy_name = f"replication_policy_{short_uid()}"

        role_arn = create_iam_role_with_policy(
            RoleName=role_name,
            PolicyName=policy_name,
            RoleDefinition=S3_ASSUME_ROLE_POLICY,
            PolicyDefinition=S3_POLICY,
        )
        s3_create_bucket(Bucket=bucket_src)

        s3_client_secondary = aws_client_factory(region_name="us-west-2").s3
        s3_create_bucket_with_client(
            s3_client=s3_client_secondary,
            Bucket=bucket_dst,
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        aws_client.s3.put_bucket_versioning(
            Bucket=bucket_dst, VersioningConfiguration={"Status": "Enabled"}
        )

        # expect error if versioning is disabled on src-bucket
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_bucket_replication(Bucket=bucket_src)
        snapshot.match("expected_error_no_replication_set", e.value.response)

        replication_config = {
            "Role": role_arn,
            "Rules": [
                {
                    "Status": "Enabled",
                    "Priority": 1,
                    "DeleteMarkerReplication": {"Status": "Disabled"},
                    "Filter": {"Prefix": "Tax"},
                    "Destination": {"Bucket": f"arn:aws:s3:::{bucket_dst}"},
                }
            ],
        }
        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_replication(
                ReplicationConfiguration=replication_config, Bucket=bucket_src
            )
        snapshot.match("expected_error_versioning_not_enabled", e.value.response)

        # enable versioning
        aws_client.s3.put_bucket_versioning(
            Bucket=bucket_src, VersioningConfiguration={"Status": "Enabled"}
        )

        response = aws_client.s3.put_bucket_replication(
            ReplicationConfiguration=replication_config, Bucket=bucket_src
        )
        snapshot.match("put-bucket-replication", response)

        response = aws_client.s3.get_bucket_replication(Bucket=bucket_src)
        snapshot.match("get-bucket-replication", response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_bucket_replication(
                Bucket=bucket_src,
                ReplicationConfiguration={
                    "Role": role_arn,
                    "Rules": [],
                },
            )
        snapshot.match("put-empty-bucket-replication-rules", e.value.response)

        delete_replication = aws_client.s3.delete_bucket_replication(Bucket=bucket_src)
        snapshot.match("delete-bucket-replication", delete_replication)

        delete_replication = aws_client.s3.delete_bucket_replication(Bucket=bucket_src)
        snapshot.match("delete-bucket-replication-idempotent", delete_replication)


class TestS3PresignedPost:
    DEFAULT_FILE_VALUE = "abcdef"

    def post_generated_presigned_post_with_default_file(
        self, generated_request: dict
    ) -> requests.Response:
        return requests.post(
            generated_request["url"],
            data=generated_request["fields"],
            files={"file": self.DEFAULT_FILE_VALUE},
            verify=False,
            allow_redirects=False,
        )

    @markers.aws.validated
    def test_post_object_with_files(self, s3_bucket, aws_client):
        object_key = "test-presigned-post-key"

        body = (
            b"0" * 70_000
        )  # make sure the payload size is large to force chunking in our internal implementation

        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Conditions=[{"bucket": s3_bucket}],
        )
        # put object
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": body},
            verify=False,
        )
        assert response.status_code == 204

        # get object and compare results
        downloaded_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        assert downloaded_object["Body"].read() == body

    @markers.aws.validated
    def test_post_request_expires(
        self, s3_bucket, snapshot, aws_client, presigned_snapshot_transformers
    ):
        # presign a post with a short expiry time
        object_key = "test-presigned-post-key"

        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket, Key=object_key, ExpiresIn=2
        )

        # sleep so it expires
        time.sleep(3)

        # attempt to use the presigned request
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "file content"},
            verify=False,
        )

        exception = xmltodict.parse(response.content)
        exception["StatusCode"] = response.status_code
        snapshot.match("exception", exception)
        assert response.status_code in [400, 403]

    @markers.aws.validated
    @pytest.mark.parametrize(
        "signature_version",
        ["s3", "s3v4"],
    )
    def test_post_request_malformed_policy(
        self,
        s3_bucket,
        snapshot,
        signature_version,
        patch_s3_skip_signature_validation_false,
        aws_client,
        presigned_snapshot_transformers,
    ):
        object_key = "test-presigned-malformed-policy"

        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version=signature_version),
            endpoint_url=_endpoint_url(),
        )

        presigned_request = presigned_client.generate_presigned_post(
            Bucket=s3_bucket, Key=object_key, ExpiresIn=60
        )

        # modify the base64 string to be wrong
        original_policy = presigned_request["fields"]["policy"]
        presigned_request["fields"]["policy"] = original_policy[:-2]

        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "file content"},
            verify=False,
        )
        # the policy has been modified, so the signature does not correspond
        exception = xmltodict.parse(response.content)
        exception["StatusCode"] = response.status_code
        snapshot.match("exception-policy", exception)
        # assert fields that snapshot cannot match
        signature_field = "signature" if signature_version == "s3" else "x-amz-signature"
        assert (
            exception["Error"]["SignatureProvided"] == presigned_request["fields"][signature_field]
        )
        assert exception["Error"]["StringToSign"] == presigned_request["fields"]["policy"]

    @markers.aws.validated
    @pytest.mark.parametrize(
        "signature_version",
        ["s3", "s3v4"],
    )
    def test_post_request_missing_signature(
        self,
        s3_bucket,
        snapshot,
        signature_version,
        patch_s3_skip_signature_validation_false,
        aws_client,
        presigned_snapshot_transformers,
    ):
        object_key = "test-presigned-missing-signature"

        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version=signature_version),
            endpoint_url=_endpoint_url(),
        )

        presigned_request = presigned_client.generate_presigned_post(
            Bucket=s3_bucket, Key=object_key, ExpiresIn=60
        )

        # remove the signature field
        signature_field = "signature" if signature_version == "s3" else "x-amz-signature"
        presigned_request["fields"].pop(signature_field)

        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "file content"},
            verify=False,
        )

        # AWS seems to detected what kind of signature is missing from the policy fields
        exception = xmltodict.parse(response.content)
        exception["StatusCode"] = response.status_code
        snapshot.match("exception-missing-signature", exception)

    @markers.aws.validated
    @pytest.mark.parametrize(
        "signature_version",
        ["s3", "s3v4"],
    )
    def test_post_request_missing_fields(
        self,
        s3_bucket,
        snapshot,
        signature_version,
        patch_s3_skip_signature_validation_false,
        aws_client,
        presigned_snapshot_transformers,
    ):
        object_key = "test-presigned-missing-fields"

        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version=signature_version),
            endpoint_url=_endpoint_url(),
        )

        presigned_request = presigned_client.generate_presigned_post(
            Bucket=s3_bucket, Key=object_key, ExpiresIn=60
        )

        # remove some signature related fields
        if signature_version == "s3":
            presigned_request["fields"].pop("AWSAccessKeyId")
        else:
            presigned_request["fields"].pop("x-amz-algorithm")
            presigned_request["fields"].pop("x-amz-credential")

        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "file content"},
            verify=False,
        )

        exception = xmltodict.parse(response.content)
        exception["StatusCode"] = response.status_code
        snapshot.match("exception-missing-fields", exception)

        # pop everything else to see what exception comes back
        presigned_request["fields"] = {
            k: v for k, v in presigned_request["fields"].items() if k in ("key", "policy")
        }
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "file content"},
            verify=False,
        )

        exception = xmltodict.parse(response.content)
        exception["StatusCode"] = response.status_code
        snapshot.match("exception-no-sig-related-fields", exception)

    @markers.aws.validated
    def test_s3_presigned_post_success_action_status_201_response(
        self, s3_bucket, aws_client, region_name
    ):
        # a security policy is required if the bucket is not publicly writable
        # see https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectPOST.html#RESTObjectPOST-requests-form-fields
        body = "something body"
        # get presigned URL
        object_key = "key-${filename}"
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            Fields={"success_action_status": "201"},
            Conditions=[{"bucket": s3_bucket}, ["eq", "$success_action_status", "201"]],
            ExpiresIn=60,
        )
        files = {"file": ("my-file", body)}
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files=files,
            verify=False,
        )

        assert response.status_code == 201
        json_response = xmltodict.parse(response.content)
        assert "PostResponse" in json_response
        json_response = json_response["PostResponse"]

        etag = '"43281e21fce675ac3bcb3524b38ca4ed"'
        assert response.headers["ETag"] == etag

        location = f"{_bucket_url_vhost(s3_bucket, region_name)}/key-my-file"
        if region_name != "us-east-1":
            # the format is a bit different for non-default regions, we don't return the region as part of the
            # `Location` to avoid SSL issue, but we still want to test it works with `_bucket_url_vhost`
            location = location.replace(f".{region_name}.", ".")

        assert response.headers["Location"] == location
        assert json_response["Location"] == location

        assert json_response["Bucket"] == s3_bucket
        assert json_response["Key"] == "key-my-file"
        assert json_response["ETag"] == etag

    @markers.aws.validated
    def test_s3_presigned_post_success_action_redirect(self, s3_bucket, aws_client):
        # a security policy is required if the bucket is not publicly writable
        # see https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectPOST.html#RESTObjectPOST-requests-form-fields
        body = "something body"
        # get presigned URL
        object_key = "key-test"
        redirect_location = "http://localhost.test/random"
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            Fields={"success_action_redirect": redirect_location},
            Conditions=[
                {"bucket": s3_bucket},
                ["eq", "$success_action_redirect", redirect_location],
            ],
            ExpiresIn=60,
        )
        files = {"file": ("my-file", body)}
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files=files,
            verify=False,
            allow_redirects=False,
        )

        assert response.status_code == 303
        assert not response.text
        location = urlparse(response.headers["Location"])
        location_qs = parse_qs(location.query)
        assert location_qs["key"][0] == object_key
        assert location_qs["bucket"][0] == s3_bucket
        # TODO requests.post has known issues when running in CI -> sometimes the body is empty, etag is therefore different
        #  assert location_qs["etag"][0] == '"43281e21fce675ac3bcb3524b38ca4ed"'

        # If S3 cannot interpret the URL, it acts as if the field is not present.
        wrong_redirect = "/wrong/redirect/relative"
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            Fields={"success_action_redirect": wrong_redirect},
            Conditions=[
                {"bucket": s3_bucket},
                ["eq", "$success_action_redirect", wrong_redirect],
            ],
            ExpiresIn=60,
        )
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files=files,
            verify=False,
            allow_redirects=False,
        )
        assert response.status_code == 204

    @markers.aws.validated
    @pytest.mark.parametrize(
        "tagging",
        [
            "<Tagging><TagSet><Tag><Key>TagName</Key><Value>TagValue</Value></Tag></TagSet></Tagging>",
            "<Tagging><TagSet><Tag><Key>TagName</Key><Value>TagValue</Value></Tag><Tag><Key>TagName2</Key><Value>TagValue2</Value></Tag></TagSet></Tagging>",
            "<InvalidXmlTagging></InvalidXmlTagging>",
            "not-xml",
        ],
        ids=["single", "list", "invalid", "notxml"],
    )
    @markers.snapshot.skip_snapshot_verify(
        paths=["$..HostId"],  # missing from the exception XML
    )
    def test_post_object_with_tags(self, s3_bucket, aws_client, snapshot, tagging):
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("HostId"),
                snapshot.transform.key_value("RequestId"),
            ]
        )
        object_key = "test-presigned-post-key-tagging"
        # need to set the tagging directly as XML, per the documentation
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Fields={"tagging": tagging},
            Conditions=[
                {"bucket": s3_bucket},
                ["eq", "$tagging", tagging],
            ],
        )
        # put object
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "test-body-tagging"},
            verify=False,
        )
        if tagging == "not-xml":
            assert response.status_code == 400
            snapshot.match("tagging-error", xmltodict.parse(response.content))
            with pytest.raises(ClientError) as e:
                aws_client.s3.get_object_tagging(Bucket=s3_bucket, Key=object_key)
            e.match("NoSuchKey")
        else:
            assert response.status_code == 204
            tagging = aws_client.s3.get_object_tagging(Bucket=s3_bucket, Key=object_key)
            snapshot.match("get-tagging", tagging)

    @markers.aws.validated
    def test_post_object_with_metadata(self, s3_bucket, aws_client, snapshot):
        snapshot.add_transformer(
            snapshot.transform.key_value(
                "ExpiresString", reference_replacement=False, value_replacement="<expires>"
            )
        )
        object_key = "test-presigned-post-key-metadata"
        object_expires = rfc_1123_datetime(
            datetime.datetime.now(ZoneInfo("GMT")) + datetime.timedelta(minutes=10)
        )

        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Fields={
                "x-amz-meta-test-1": "test-meta-1",
                "x-amz-meta-TEST-2": "test-meta-2",
                "Content-Type": "text/plain",
                "Expires": object_expires,
            },
            Conditions=[
                {"bucket": s3_bucket},
                ["eq", "$x-amz-meta-test-1", "test-meta-1"],
                ["eq", "$x-amz-meta-TEST-2", "test-meta-2"],
                ["eq", "$Content-Type", "text/plain"],
                ["eq", "$Expires", object_expires],
            ],
        )
        # PostObject
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "test-body-tagging"},
            verify=False,
        )
        assert response.status_code == 204
        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object", head_object)

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        paths=[
            "$..HostId",
            "$..ContentLength",
            "$..ETag",
        ],  # missing from the exception XML, and failing in CI
    )
    def test_post_object_with_storage_class(self, s3_bucket, aws_client, snapshot):
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("HostId"),
                snapshot.transform.key_value("RequestId"),
            ]
        )
        object_key = "test-presigned-post-key-storage-class"
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Fields={
                "x-amz-storage-class": StorageClass.STANDARD_IA,
            },
            Conditions=[
                {"bucket": s3_bucket},
                ["eq", "$x-amz-storage-class", StorageClass.STANDARD_IA],
            ],
        )
        # PostObject
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "test-body-storage-class"},
            verify=False,
        )
        assert response.status_code == 204
        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object", head_object)

        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Fields={
                "x-amz-storage-class": "FakeClass",
            },
            Conditions=[
                {"bucket": s3_bucket},
                ["eq", "$x-amz-storage-class", "FakeClass"],
            ],
        )
        # PostObject
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "test-body-storage-class"},
            verify=False,
        )
        assert response.status_code == 400
        snapshot.match("invalid-storage-error", xmltodict.parse(response.content))

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        paths=["$..HostId"],
    )
    def test_post_object_with_wrong_content_type(self, s3_bucket, aws_client, snapshot):
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("HostId"),
                snapshot.transform.key_value("RequestId"),
            ]
        )
        object_key = "test-presigned-post-key-wrong-content-type"
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Conditions=[
                {"bucket": s3_bucket},
            ],
        )
        # PostObject
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "test-body-wrong-content-type"},
            headers={"Content-Type": "text/html"},
            verify=False,
        )

        assert response.status_code == 412
        snapshot.match("invalid-content-type-error", xmltodict.parse(response.content))

    @markers.aws.validated
    def test_post_object_default_checksum(self, s3_bucket, aws_client, snapshot):
        object_key = "test-presigned-post-checksum"

        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Conditions=[{"bucket": s3_bucket}],
        )

        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "test-body-tagging"},
            verify=False,
        )
        assert response.status_code == 204
        assert "x-amz-checksum-crc64nvme" in response.headers
        assert response.headers["x-amz-checksum-type"] == "FULL_OBJECT"

        head_object = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=object_key, ChecksumMode="ENABLED"
        )
        snapshot.match("head-object", head_object)
        assert head_object["ChecksumCRC64NVME"] == response.headers["x-amz-checksum-crc64nvme"]

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        paths=[
            "$..ContentLength",
            "$..ETag",
            "$..HostId",
        ],  # FIXME: in CI, it fails sporadically and the form is empty
    )
    def test_post_object_with_file_as_string(self, s3_bucket, aws_client, snapshot):
        # this is a test for https://github.com/localstack/localstack/issues/10309
        # You can send requests with node.js with a different format than what we can with Python
        # (the actual file would just be a regular `file` key of the form with content)
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("HostId"),
                snapshot.transform.key_value("RequestId"),
                snapshot.transform.key_value("Name"),
            ]
        )
        object_key = "test-presigned-post-file-as-field"
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Conditions=[
                {"bucket": s3_bucket},
            ],
        )

        # we need to define a proper format for `files` so that we don't add the filename= field to the form
        # see https://github.com/psf/requests/issues/1081

        # PostObject
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={
                "file": (None, "test-body-file-as-field"),
            },
            verify=False,
        )
        assert response.status_code == 204

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object", head_object)

        object_key = "file-as-field-${filename}"
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Conditions=[
                {"bucket": s3_bucket},
            ],
        )
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={
                "file": (None, "test-body-file-as-field-filename-replacement"),
            },
            verify=False,
        )
        assert response.status_code == 204

        response = aws_client.s3.list_objects_v2(Bucket=s3_bucket)
        snapshot.match("list-objects", response)

    @markers.snapshot.skip_snapshot_verify(
        paths=[
            # TODO: wrong exception implement, still missing the extra input fields validation
            "$.invalid-condition-missing-prefix.Error.Message",
            # TODO: we should add HostId to every serialized exception for S3, and not have them as part as the spec
            "$.invalid-condition-wrong-condition.Error.HostId",
        ],
    )
    @markers.aws.validated
    def test_post_object_policy_conditions_validation_eq(self, s3_bucket, aws_client, snapshot):
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value(
                    "HostId", reference_replacement=False, value_replacement="<host-id>"
                ),
                snapshot.transform.key_value("RequestId"),
                snapshot.transform.key_value(
                    "ExpiresString", reference_replacement=False, value_replacement="<expires>"
                ),
            ]
        )
        object_key = "validate-policy-1"

        redirect_location = "http://localhost.test/random"
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            Fields={"success_action_redirect": redirect_location},
            Conditions=[
                ["eq", "$success_action_redirect", redirect_location],
            ],
            ExpiresIn=60,
        )

        # PostObject with a wrong redirect location
        presigned_request["fields"]["success_action_redirect"] = "http://wrong.location/test"
        response = self.post_generated_presigned_post_with_default_file(presigned_request)

        # assert that it's rejected
        assert response.status_code == 403
        snapshot.match("invalid-condition-eq", xmltodict.parse(response.content))

        # PostObject with a wrong condition (missing $ prefix)
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            Fields={"success_action_redirect": redirect_location},
            Conditions=[
                ["eq", "success_action_redirect", redirect_location],
            ],
            ExpiresIn=60,
        )

        response = self.post_generated_presigned_post_with_default_file(presigned_request)

        # assert that it's rejected
        assert response.status_code == 403
        snapshot.match("invalid-condition-missing-prefix", xmltodict.parse(response.content))

        # PostObject with a wrong condition (multiple condition in one dict)
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            Fields={"success_action_redirect": redirect_location},
            Conditions=[
                {"bucket": s3_bucket, "success_action_redirect": redirect_location},
            ],
            ExpiresIn=60,
        )

        response = self.post_generated_presigned_post_with_default_file(presigned_request)

        # assert that it's rejected
        assert response.status_code == 400
        snapshot.match("invalid-condition-wrong-condition", xmltodict.parse(response.content))

        # PostObject with a wrong condition value casing
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            Fields={"success_action_redirect": redirect_location},
            Conditions=[
                ["eq", "$success_action_redirect", redirect_location.replace("http://", "HTTP://")],
            ],
            ExpiresIn=60,
        )
        response = self.post_generated_presigned_post_with_default_file(presigned_request)
        # assert that it's rejected
        assert response.status_code == 403
        snapshot.match("invalid-condition-wrong-value-casing", xmltodict.parse(response.content))

        object_expires = rfc_1123_datetime(
            datetime.datetime.now(ZoneInfo("GMT")) + datetime.timedelta(minutes=10)
        )

        # test casing for x-amz-meta and specific Content-Type/Expires S3 headers
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Fields={
                "x-amz-meta-test-1": "test-meta-1",
                "x-amz-meta-TEST-2": "test-meta-2",
                "Content-Type": "text/plain",
                "Expires": object_expires,
            },
            Conditions=[
                {"bucket": s3_bucket},
                ["eq", "$x-amz-meta-test-1", "test-meta-1"],
                ["eq", "$x-amz-meta-test-2", "test-meta-2"],
                ["eq", "$content-type", "text/plain"],
                ["eq", "$Expires", object_expires],
            ],
        )
        # assert that it kept the casing
        assert "x-amz-meta-TEST-2" in presigned_request["fields"]
        response = self.post_generated_presigned_post_with_default_file(presigned_request)
        # assert that it's accepted
        assert response.status_code == 204

        head_object = aws_client.s3.head_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("head-object-metadata", head_object)

        # PostObject with a wrong condition key casing, should still work
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            Fields={"success_action_redirect": redirect_location},
            Conditions=[
                ["eq", "$success_Action_REDIRECT", redirect_location],
            ],
            ExpiresIn=60,
        )

        # load the generated policy to assert that it kept the casing, and it is sent to AWS
        generated_policy = json.loads(
            base64.b64decode(presigned_request["fields"]["policy"]).decode("utf-8")
        )
        eq_condition = [
            cond
            for cond in generated_policy["conditions"]
            if isinstance(cond, list) and cond[0] == "eq"
        ][0]
        assert eq_condition[1] == "$success_Action_REDIRECT"

        response = self.post_generated_presigned_post_with_default_file(presigned_request)
        # assert that it's accepted
        assert response.status_code == 303

        final_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("final-object", final_object)

        # test casing for x-amz-meta and specific Content-Type/Expires S3 headers, but without eq
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Fields={
                "x-amz-meta-test-1": "test-meta-1",
                "x-amz-meta-TEST-2": "test-meta-2",
                "Content-Type": "text/plain",
                "Expires": object_expires,
            },
            Conditions=[
                {"bucket": s3_bucket},
                {"x-amz-meta-test-1": "test-meta-1"},
                {"x-amz-meta-test-2": "test-meta-2"},
                {"Content-Type": "text/plain"},
                {"Expires": object_expires},
            ],
        )
        # assert that it kept the casing
        assert "x-amz-meta-TEST-2" in presigned_request["fields"]
        assert "Content-Type" in presigned_request["fields"]
        response = self.post_generated_presigned_post_with_default_file(presigned_request)
        # assert that it's accepted
        assert response.status_code == 204

    @markers.snapshot.skip_snapshot_verify(
        paths=[
            # TODO: wrong exception implement, still missing the extra input fields validation
            "$.invalid-condition-missing-prefix.Error.Message",
            # TODO: we should add HostId to every serialized exception for S3, and not have them as part as the spec
            "$.invalid-condition-wrong-condition.Error.HostId",
        ],
    )
    @markers.aws.validated
    def test_post_object_policy_conditions_validation_starts_with(
        self, s3_bucket, aws_client, snapshot
    ):
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value(
                    "HostId", reference_replacement=False, value_replacement="<host-id>"
                ),
                snapshot.transform.key_value("RequestId"),
            ]
        )
        object_key = "validate-policy-2"

        redirect_location = "http://localhost.test/random"
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            Fields={"success_action_redirect": redirect_location},
            Conditions=[
                ["starts-with", "$success_action_redirect", "http://localhost"],
            ],
            ExpiresIn=60,
        )

        # PostObject with a wrong redirect location start
        presigned_request["fields"]["success_action_redirect"] = "http://wrong.location/test"
        response = self.post_generated_presigned_post_with_default_file(presigned_request)

        # assert that it's rejected
        assert response.status_code == 403
        snapshot.match("invalid-condition-starts-with", xmltodict.parse(response.content))

        # PostObject with a right redirect location start but wrong casing
        presigned_request["fields"]["success_action_redirect"] = "HTTP://localhost.test/random"
        response = self.post_generated_presigned_post_with_default_file(presigned_request)

        # assert that it's rejected
        assert response.status_code == 403
        snapshot.match("invalid-condition-starts-with-casing", xmltodict.parse(response.content))

        # PostObject with a right redirect location start
        presigned_request["fields"]["success_action_redirect"] = redirect_location
        response = self.post_generated_presigned_post_with_default_file(presigned_request)

        # assert that it's accepted
        assert response.status_code == 303
        assert response.headers["Location"].startswith(redirect_location)

        get_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-object-1", get_object)

        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            Fields={"success_action_redirect": redirect_location},
            Conditions=[
                [
                    "starts-with",
                    "$success_action_redirect",
                    "",
                ],  # this allows to accept anything for it
            ],
            ExpiresIn=60,
        )

        # PostObject with a different redirect location, but should be accepted
        # manually generate the pre-signed with a different file value to change ETag, to later validate that the file
        # has properly been written in S3
        presigned_request["fields"]["success_action_redirect"] = "http://wrong.location/test"
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "manual value to change ETag"},
            verify=False,
            allow_redirects=False,
        )

        # assert that it's accepted
        assert response.status_code == 303
        assert response.headers["Location"].startswith("http://wrong.location/test")

        get_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("get-object-2", get_object)

    @markers.snapshot.skip_snapshot_verify(
        paths=[
            # TODO: we should add HostId to every serialized exception for S3, and not have them as part as the spec
            "$.invalid-content-length-too-small.Error.HostId",
        ],
    )
    @markers.aws.validated
    def test_post_object_policy_validation_size(self, s3_bucket, aws_client, snapshot):
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value(
                    "HostId", reference_replacement=False, value_replacement="<host-id>"
                ),
                snapshot.transform.key_value("RequestId"),
            ]
        )
        object_key = "validate-policy-content-length"
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Conditions=[
                {"bucket": s3_bucket},
                ["content-length-range", 5, 10],
            ],
        )
        # PostObject with a body length of 12
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "a" * 12},
            verify=False,
        )

        # assert that it's rejected
        assert response.status_code == 400
        snapshot.match("invalid-content-length-too-big", xmltodict.parse(response.content))

        # PostObject with a body length of 1
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "a" * 1},
            verify=False,
        )

        # assert that it's rejected
        assert response.status_code == 400
        snapshot.match("invalid-content-length-too-small", xmltodict.parse(response.content))

        # PostObject with a body length of 5
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "a" * 5},
            verify=False,
        )
        assert response.status_code == 204

        # PostObject with a body length of 10
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "a" * 10},
            verify=False,
        )
        assert response.status_code == 204

        final_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=object_key)
        snapshot.match("final-object", final_object)

        # try with string values for the content length range
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Conditions=[
                {"bucket": s3_bucket},
                ["content-length-range", "5", "10"],
            ],
        )
        # PostObject with a body length of 10
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "a" * 10},
            verify=False,
        )
        assert response.status_code == 204

        # try with string values that are not cast-able for the content length range
        presigned_request = aws_client.s3.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Conditions=[
                {"bucket": s3_bucket},
                ["content-length-range", "test", "10"],
            ],
        )
        # PostObject with a body length of 10
        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": "a" * 10},
            verify=False,
        )
        assert response.status_code == 403
        snapshot.match("invalid-content-length-wrong-type", xmltodict.parse(response.content))

    @pytest.mark.skipif(
        condition=TEST_S3_IMAGE,
        reason="STS not enabled in S3 image",
    )
    @markers.aws.validated
    def test_presigned_post_with_different_user_credentials(
        self,
        aws_client,
        s3_create_bucket_with_client,
        create_role_with_policy,
        account_id,
        wait_and_assume_role,
        snapshot,
    ):
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value(
                    "HostId", reference_replacement=False, value_replacement="<host-id>"
                ),
                snapshot.transform.key_value("RequestId"),
            ]
        )
        bucket_name = f"bucket-{short_uid()}"
        actions = [
            "s3:CreateBucket",
            "s3:PutObject",
            "s3:GetObject",
            "s3:DeleteBucket",
            "s3:DeleteObject",
        ]

        assume_policy_doc = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "sts:AssumeRole",
                    "Principal": {"AWS": account_id},
                    "Effect": "Allow",
                }
            ],
        }
        assume_policy_doc = json.dumps(assume_policy_doc)
        role_name, role_arn = create_role_with_policy(
            effect="Allow",
            actions=actions,
            assume_policy_doc=assume_policy_doc,
            resource="*",
        )

        credentials = wait_and_assume_role(role_arn=role_arn)

        client = boto3.client(
            "s3",
            config=Config(signature_version="s3v4"),
            endpoint_url=_endpoint_url(),
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )

        retry(
            lambda: s3_create_bucket_with_client(s3_client=client, Bucket=bucket_name),
            sleep=3 if is_aws_cloud() else 0.5,
        )

        object_key = "validate-policy-full-credentials"
        presigned_request = client.generate_presigned_post(
            Bucket=bucket_name,
            Key=object_key,
            ExpiresIn=60,
            Conditions=[
                {"bucket": bucket_name},
            ],
        )
        # load the generated policy to assert that it kept the casing, and it is sent to AWS
        generated_policy = json.loads(
            base64.b64decode(presigned_request["fields"]["policy"]).decode("utf-8")
        )
        policy_conditions_fields = set()
        token_condition = None
        for condition in generated_policy["conditions"]:
            if isinstance(condition, dict):
                for k, v in condition.items():
                    policy_conditions_fields.add(k)
                    if k == "x-amz-security-token":
                        token_condition = v
            else:
                # format is [operator, key, value]
                policy_conditions_fields.add(condition[1])

        assert policy_conditions_fields == {
            "bucket",
            "key",
            "x-amz-security-token",
            "x-amz-credential",
            "x-amz-date",
            "x-amz-algorithm",
        }
        assert token_condition == credentials["SessionToken"]

        response = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files={"file": self.DEFAULT_FILE_VALUE},
            verify=False,
        )
        assert response.status_code == 204
        assert response.headers.get("Content-Type") is None

        get_obj = aws_client.s3.get_object(Bucket=bucket_name, Key=object_key)
        snapshot.match("get-obj", get_obj)

    @markers.aws.validated
    @pytest.mark.parametrize(
        "signature_version",
        ["s3", "s3v4"],
    )
    def test_post_object_policy_casing(self, s3_bucket, signature_version):
        object_key = "validate-policy-casing"
        presigned_client = _s3_client_pre_signed_client(
            Config(signature_version=signature_version),
            endpoint_url=_endpoint_url(),
        )
        presigned_request = presigned_client.generate_presigned_post(
            Bucket=s3_bucket,
            Key=object_key,
            ExpiresIn=60,
            Conditions=[
                {"bucket": s3_bucket},
                ["content-length-range", 5, 10],
            ],
        )

        # test that we can change the casing of the Policy field
        fields = presigned_request["fields"]
        fields["Policy"] = fields.pop("policy")
        response = requests.post(
            presigned_request["url"],
            data=fields,
            files={"file": "a" * 5},
            verify=False,
        )
        assert response.status_code == 204

        # test that we can change the casing of the credentials field
        if signature_version == "s3":
            field_name = "AWSAccessKeyId"
            new_field_name = "awsaccesskeyid"
        else:
            field_name = "x-amz-credential"
            new_field_name = "X-Amz-Credential"

        fields[new_field_name] = fields.pop(field_name)
        response = requests.post(
            presigned_request["url"],
            data=fields,
            files={"file": "a" * 5},
            verify=False,
        )
        assert response.status_code == 204


# LocalStack does not apply encryption, so the ETag is different
@markers.snapshot.skip_snapshot_verify(paths=["$..ETag"])
class TestS3SSECEncryption:
    # https://docs.aws.amazon.com/AmazonS3/latest/userguide/ServerSideEncryptionCustomerKeys.html
    ENCRYPTION_KEY = b"1234567890abcdef1234567890abcdef"
    ENCRYPTION_KEY_2 = b"abcdef1234567890abcdef1234567890"

    @staticmethod
    def get_encryption_key_b64_and_md5(encryption_key: bytes) -> tuple[str, str]:
        sse_customer_key_base64 = base64.b64encode(encryption_key).decode("utf-8")
        sse_customer_key_md5 = base64.b64encode(hashlib.md5(encryption_key).digest()).decode(
            "utf-8"
        )
        return sse_customer_key_base64, sse_customer_key_md5

    @markers.aws.validated
    def test_put_object_lifecycle_with_sse_c(self, aws_client, s3_bucket, snapshot):
        body = "test_data"
        key_name = "test-sse-c"
        cus_key, cus_key_md5 = self.get_encryption_key_b64_and_md5(self.ENCRYPTION_KEY)
        put_obj = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=key_name,
            Body=body,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
        )
        snapshot.match("put-obj-sse-c", put_obj)

        head_obj = aws_client.s3.head_object(
            Bucket=s3_bucket,
            Key=key_name,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
        )
        snapshot.match("head-obj-sse-c", head_obj)

        get_obj = aws_client.s3.get_object(
            Bucket=s3_bucket,
            Key=key_name,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
        )
        snapshot.match("get-obj-sse-c", get_obj)

        get_obj_attr = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key_name,
            ObjectAttributes=["ETag", "ObjectSize"],
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
        )
        snapshot.match("get-obj-attrs-sse-c", get_obj_attr)

        del_obj = aws_client.s3.delete_object(Bucket=s3_bucket, Key=key_name)
        snapshot.match("del-obj-sse-c", del_obj)

    @markers.aws.validated
    def test_put_object_validation_sse_c(self, aws_client, s3_bucket, snapshot):
        body = "test_data"
        key_name = "test-sse-c"
        cus_key, cus_key_md5 = self.get_encryption_key_b64_and_md5(self.ENCRYPTION_KEY)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key=key_name,
                Body=body,
                ServerSideEncryption="AES256",
                SSECustomerAlgorithm="KMS",
                SSECustomerKey=cus_key,
                SSECustomerKeyMD5=cus_key_md5,
            )
        snapshot.match("put-obj-sse-c-both-encryption", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key=key_name,
                Body=body,
                SSECustomerAlgorithm="KMS",
                SSECustomerKey=cus_key,
                SSECustomerKeyMD5=cus_key_md5,
            )
        snapshot.match("put-obj-sse-c-wrong-algo", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key=key_name,
                Body=body,
                SSECustomerKey=cus_key,
                SSECustomerKeyMD5=cus_key_md5,
            )
        snapshot.match("put-obj-sse-c-no-algo", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key=key_name,
                Body=body,
                SSECustomerAlgorithm="AES256",
                SSECustomerKeyMD5=cus_key_md5,
            )
        snapshot.match("put-obj-sse-c-no-key", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key=key_name,
                Body=body,
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=cus_key,
            )
        snapshot.match("put-obj-sse-c-no-md5", e.value.response)

        with pytest.raises(ClientError) as e:
            bad_key_size = base64.b64encode(self.ENCRYPTION_KEY[:10]).decode("utf-8")
            bad_key_size_md5 = base64.b64encode(
                hashlib.md5(self.ENCRYPTION_KEY[:10]).digest()
            ).decode("utf-8")
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key=key_name,
                Body=body,
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=bad_key_size,
                SSECustomerKeyMD5=bad_key_size_md5,
            )
        snapshot.match("put-obj-sse-c-wrong-key-size", e.value.response)

        with pytest.raises(ClientError) as e:
            bad_char = "a" if cus_key_md5[0] != "a" else "b"
            bad_md5 = bad_char + cus_key_md5[1:]
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key=key_name,
                Body=body,
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=cus_key,
                SSECustomerKeyMD5=bad_md5,
            )
        snapshot.match("put-obj-sse-c-bad-md5", e.value.response)

    @markers.aws.validated
    def test_object_retrieval_sse_c(self, aws_client, s3_bucket, snapshot):
        body = "test_data"
        key_name = "test-sse-c"
        cus_key, cus_key_md5 = self.get_encryption_key_b64_and_md5(self.ENCRYPTION_KEY)
        put_obj = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=key_name,
            Body=body,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
        )
        snapshot.match("put-obj-sse-c", put_obj)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=s3_bucket, Key=key_name)
        snapshot.match("get-obj-no-sse-c", e.value.response)

        with pytest.raises(ClientError) as e:
            key_2, key_2_md5 = self.get_encryption_key_b64_and_md5(self.ENCRYPTION_KEY_2)
            aws_client.s3.get_object(
                Bucket=s3_bucket,
                Key=key_name,
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=key_2,
                SSECustomerKeyMD5=key_2_md5,
            )
        snapshot.match("get-obj-sse-c-wrong-key", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(
                Bucket=s3_bucket,
                Key=key_name,
                SSECustomerAlgorithm="KMS",
                SSECustomerKey=cus_key,
                SSECustomerKeyMD5=cus_key_md5,
            )
        snapshot.match("get-obj-sse-c-wrong-algo", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.head_object(
                Bucket=s3_bucket,
                Key=key_name,
                SSECustomerAlgorithm="KMS",
                SSECustomerKey=cus_key,
                SSECustomerKeyMD5=cus_key_md5,
            )
        snapshot.match("head-obj-sse-c-wrong-algo", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object_attributes(
                Bucket=s3_bucket,
                Key=key_name,
                SSECustomerAlgorithm="KMS",
                SSECustomerKey=cus_key,
                SSECustomerKeyMD5=cus_key_md5,
                ObjectAttributes=["ETag"],
            )
        snapshot.match("get-obj-attrs-sse-c-wrong-algo", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(
                Bucket=s3_bucket,
                Key=key_name,
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=cus_key,
            )
        snapshot.match("get-obj-sse-c-no-md5", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.head_object(
                Bucket=s3_bucket,
                Key=key_name,
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=cus_key,
            )
        snapshot.match("head-obj-sse-c-no-md5", e.value.response)

        with pytest.raises(ClientError) as e:
            bad_key_size = base64.b64encode(self.ENCRYPTION_KEY[:10]).decode("utf-8")
            bad_key_size_md5 = base64.b64encode(
                hashlib.md5(self.ENCRYPTION_KEY[:10]).digest()
            ).decode("utf-8")
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key=key_name,
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=bad_key_size,
                SSECustomerKeyMD5=bad_key_size_md5,
            )
        snapshot.match("get-obj-sse-c-wrong-key-size", e.value.response)

        with pytest.raises(ClientError) as e:
            bad_char = "a" if cus_key_md5[0] != "a" else "b"
            bad_md5 = bad_char + cus_key_md5[1:]
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key=key_name,
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=cus_key,
                SSECustomerKeyMD5=bad_md5,
            )
        snapshot.match("get-obj-sse-c-bad-md5", e.value.response)

    @markers.aws.validated
    def test_copy_object_with_sse_c(self, aws_client, s3_bucket, snapshot):
        body = "test_data"
        key_name_src = "test-sse-c-src"
        key_name_target = "test-sse-c-target"
        cus_key, cus_key_md5 = self.get_encryption_key_b64_and_md5(self.ENCRYPTION_KEY)
        put_obj = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=key_name_src,
            Body=body,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
        )
        snapshot.match("put-obj-sse-c", put_obj)

        # successful copy without encrypting the target
        copy_obj = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            Key=key_name_target,
            CopySource=f"{s3_bucket}/{key_name_src}",
            CopySourceSSECustomerAlgorithm="AES256",
            CopySourceSSECustomerKey=cus_key,
            CopySourceSSECustomerKeyMD5=cus_key_md5,
        )
        snapshot.match("copy-obj-sse-c-target-no-sse-c", copy_obj)

        # successful copy while encrypting the target
        copy_obj = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            Key=key_name_target,
            CopySource=f"{s3_bucket}/{key_name_src}",
            CopySourceSSECustomerAlgorithm="AES256",
            CopySourceSSECustomerKey=cus_key,
            CopySourceSSECustomerKeyMD5=cus_key_md5,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
        )
        snapshot.match("copy-obj-sse-c", copy_obj)

        # assert the encryption is successful by trying to get object it without SSE-C
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=s3_bucket, Key=key_name_target)
        snapshot.match("get-obj-no-sse-c-param", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket,
                Key=key_name_target,
                CopySource=f"{s3_bucket}/{key_name_src}",
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=cus_key,
                SSECustomerKeyMD5=cus_key_md5,
            )
        snapshot.match("copy-obj-no-src-sse-c", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket,
                Key=key_name_target,
                CopySource=f"{s3_bucket}/{key_name_src}",
                CopySourceSSECustomerAlgorithm="KMS",
                CopySourceSSECustomerKey=cus_key,
                CopySourceSSECustomerKeyMD5=cus_key_md5,
            )
        snapshot.match("copy-obj-wrong-src-sse-c-algo", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket,
                Key=key_name_target,
                CopySource=f"{s3_bucket}/{key_name_src}",
                CopySourceSSECustomerAlgorithm="AES256",
                CopySourceSSECustomerKey=cus_key,
                CopySourceSSECustomerKeyMD5=cus_key_md5,
                SSECustomerAlgorithm="KMS",
                SSECustomerKey=cus_key,
                SSECustomerKeyMD5=cus_key_md5,
            )
        snapshot.match("copy-obj-wrong-target-sse-c-algo", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.copy_object(
                Bucket=s3_bucket,
                Key=key_name_target,
                CopySource=f"{s3_bucket}/{key_name_src}",
                CopySourceSSECustomerAlgorithm="AES256",
                CopySourceSSECustomerKey=cus_key,
                CopySourceSSECustomerKeyMD5=cus_key_md5,
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=cus_key,
                SSECustomerKeyMD5=cus_key_md5,
                ServerSideEncryption="AES256",
            )
        snapshot.match("copy-obj-target-double-encryption", e.value.response)

    @markers.aws.validated
    def test_multipart_upload_sse_c(self, aws_client, s3_bucket, snapshot):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("UploadId"),
                snapshot.transform.key_value("DisplayName", reference_replacement=False),
                snapshot.transform.key_value("ID", reference_replacement=False),
            ]
        )
        key_name = "test-sse-c-multipart"
        cus_key, cus_key_md5 = self.get_encryption_key_b64_and_md5(self.ENCRYPTION_KEY)

        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
        )
        snapshot.match("create-mpu-sse-c", response)
        upload_id = response["UploadId"]

        # data must be at least 5MiB
        part_data = "a" * (5_242_880 + 1)
        part_data = to_bytes(part_data)

        parts = 3
        multipart_upload_parts = []
        for part in range(parts):
            # Write contents to memory rather than a file.
            part_number = part + 1
            upload_file_object = BytesIO(part_data)
            response = aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name,
                Body=upload_file_object,
                PartNumber=part_number,
                UploadId=upload_id,
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=cus_key,
                SSECustomerKeyMD5=cus_key_md5,
            )
            snapshot.match(f"upload-part-{part}", response)
            multipart_upload_parts.append(
                {
                    "ETag": response["ETag"],
                    "PartNumber": part_number,
                }
            )

        response = aws_client.s3.list_parts(Bucket=s3_bucket, Key=key_name, UploadId=upload_id)
        snapshot.match("list-parts", response)

        # no need to add the SSE-C on complete (from the documentation, but you still can?? weird?) TODO check
        response = aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload={"Parts": multipart_upload_parts},
            UploadId=upload_id,
        )
        snapshot.match("complete-multipart-checksum", response)

        # assert the encryption is successful by trying to get object it without SSE-C
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(Bucket=s3_bucket, Key=key_name)
        snapshot.match("get-obj-no-sse-c-param", e.value.response)

        get_obj = aws_client.s3.get_object(
            Bucket=s3_bucket,
            Key=key_name,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
        )
        # object is big, so we remove the body
        get_obj["Body"].read()
        snapshot.match("get-obj-sse-c", get_obj)

    @markers.aws.validated
    def test_multipart_upload_sse_c_validation(self, aws_client, s3_bucket, snapshot):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("UploadId"),
                snapshot.transform.key_value("DisplayName", reference_replacement=False),
                snapshot.transform.key_value("ID", reference_replacement=False),
            ]
        )
        body = "testbody"
        key_name = "test-sse-c-multipart"
        cus_key, cus_key_md5 = self.get_encryption_key_b64_and_md5(self.ENCRYPTION_KEY)

        # create a multipart without SSE-C
        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
        )
        snapshot.match("create-mpu-no-sse-c", response)
        upload_id = response["UploadId"]

        # assert that if the multipart isnt created with SSE-C, you cannot upload with SSE-C
        with pytest.raises(ClientError) as e:
            aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name,
                Body=body,
                PartNumber=1,
                UploadId=upload_id,
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=cus_key,
                SSECustomerKeyMD5=cus_key_md5,
            )
        snapshot.match("mpu-no-sse-c-upload-part-with-sse-c", e.value.response)
        # remove the multipart
        aws_client.s3.abort_multipart_upload(Bucket=s3_bucket, Key=key_name, UploadId=upload_id)

        # create a multipart with SSE-C
        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
        )
        snapshot.match("create-mpu-sse-c", response)
        upload_id = response["UploadId"]

        # assert that if the multipart is created with SSE-C, you cannot upload without SSE-C
        with pytest.raises(ClientError) as e:
            aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name,
                Body=body,
                PartNumber=1,
                UploadId=upload_id,
            )
        snapshot.match("mpu-sse-c-upload-part-no-sse-c", e.value.response)

        with pytest.raises(ClientError) as e:
            key_2, key_2_md5 = self.get_encryption_key_b64_and_md5(self.ENCRYPTION_KEY_2)
            aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name,
                Body=body,
                PartNumber=1,
                UploadId=upload_id,
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=key_2,
                SSECustomerKeyMD5=key_2_md5,
            )
        snapshot.match("mpu-sse-c-upload-part-wrong-sse-c", e.value.response)
        # TODO: check complete with wrong parameters, even though it is not required to give them?

    @markers.aws.validated
    def test_sse_c_with_versioning(self, aws_client, s3_bucket, snapshot):
        snapshot.add_transformer(snapshot.transform.key_value("VersionId"))
        # enable versioning on the bucket
        aws_client.s3.put_bucket_versioning(
            Bucket=s3_bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        # assert that you can use different encryption keys for different versions
        key_name = "test-versioning-sse-c"
        cus_key, cus_key_md5 = self.get_encryption_key_b64_and_md5(self.ENCRYPTION_KEY)
        put_obj = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=key_name,
            Body="version1",
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
        )
        snapshot.match("put-obj-sse-c-version-1", put_obj)
        version_1 = put_obj["VersionId"]

        cus_key_2, cus_key_2_md5 = self.get_encryption_key_b64_and_md5(self.ENCRYPTION_KEY_2)

        put_obj_version_2 = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=key_name,
            Body="version2",
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key_2,
            SSECustomerKeyMD5=cus_key_2_md5,
        )
        snapshot.match("put-obj-sse-c-version-2", put_obj_version_2)
        version_2 = put_obj_version_2["VersionId"]

        # last version should be what we call version-2, try getting it with Key 2
        get_current_obj = aws_client.s3.get_object(
            Bucket=s3_bucket,
            Key=key_name,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key_2,
            SSECustomerKeyMD5=cus_key_2_md5,
        )
        snapshot.match("get-obj-sse-c-last-version", get_current_obj)

        # access directly version 2
        get_obj_2 = aws_client.s3.get_object(
            Bucket=s3_bucket,
            Key=key_name,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key_2,
            SSECustomerKeyMD5=cus_key_2_md5,
            VersionId=version_2,
        )
        snapshot.match("get-obj-sse-c-version-2", get_obj_2)

        # try getting the version 1 with Key 2
        with pytest.raises(ClientError) as e:
            aws_client.s3.get_object(
                Bucket=s3_bucket,
                Key=key_name,
                SSECustomerAlgorithm="AES256",
                SSECustomerKey=cus_key_2,
                SSECustomerKeyMD5=cus_key_2_md5,
                VersionId=version_1,
            )
        snapshot.match("get-obj-sse-c-last-version-wrong-key", e.value.response)

        get_version_1_obj = aws_client.s3.get_object(
            Bucket=s3_bucket,
            Key=key_name,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
            VersionId=version_1,
        )
        snapshot.match("get-obj-sse-c-version-1", get_version_1_obj)

    @markers.aws.validated
    def test_put_object_default_checksum_with_sse_c(
        self, aws_client, s3_bucket, snapshot, aws_http_client_factory
    ):
        cus_key, cus_key_md5 = self.get_encryption_key_b64_and_md5(self.ENCRYPTION_KEY)
        headers = {
            "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
            "x-amz-server-side-encryption-customer-algorithm": "AES256",
            "x-amz-server-side-encryption-customer-key": cus_key,
            "x-amz-server-side-encryption-customer-key-MD5": cus_key_md5,
        }
        data = b"test data.."

        s3_http_client = aws_http_client_factory("s3", signer_factory=SigV4Auth)
        bucket_url = _bucket_url(s3_bucket)

        no_checksum_key_sse_c = "test-sse-c"

        # https://docs.aws.amazon.com/sdkref/latest/guide/feature-dataintegrity.html
        no_checksum_put_object_url = f"{bucket_url}/{no_checksum_key_sse_c}"
        resp = s3_http_client.put(no_checksum_put_object_url, headers=headers, data=data)
        assert resp.ok

        head_obj = aws_client.s3.head_object(
            Bucket=s3_bucket,
            Key=no_checksum_key_sse_c,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
            ChecksumMode="ENABLED",
        )
        snapshot.match("head-obj-sse-c", head_obj)

        get_obj = aws_client.s3.get_object(
            Bucket=s3_bucket,
            Key=no_checksum_key_sse_c,
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
        )
        snapshot.match("get-obj-sse-c", get_obj)

        get_obj_attr = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=no_checksum_key_sse_c,
            ObjectAttributes=["ETag", "Checksum"],
            SSECustomerAlgorithm="AES256",
            SSECustomerKey=cus_key,
            SSECustomerKeyMD5=cus_key_md5,
        )
        snapshot.match("get-obj-attrs-sse-c", get_obj_attr)


class TestS3PutObjectChecksum:
    @markers.aws.validated
    @pytest.mark.parametrize("algorithm", ["CRC32", "CRC32C", "SHA1", "SHA256", "CRC64NVME"])
    def test_put_object_checksum(self, s3_bucket, algorithm, snapshot, aws_client):
        key = f"file-{short_uid()}"
        data = b"test data.."

        params = {
            "Bucket": s3_bucket,
            "Key": key,
            "Body": data,
            "ChecksumAlgorithm": algorithm,
            f"Checksum{algorithm}": short_uid(),
        }

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(**params)
        snapshot.match("put-wrong-checksum-no-b64", e.value.response)

        with pytest.raises(ClientError) as e:
            params[f"Checksum{algorithm}"] = get_checksum_for_algorithm(algorithm, b"bad data")
            aws_client.s3.put_object(**params)
        snapshot.match("put-wrong-checksum-value", e.value.response)

        # Test our generated checksums
        params[f"Checksum{algorithm}"] = get_checksum_for_algorithm(algorithm, data)
        response = aws_client.s3.put_object(**params)
        snapshot.match("put-object-generated", response)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key,
            ObjectAttributes=["ETag", "Checksum"],
        )
        snapshot.match("get-object-attrs-generated", object_attrs)

        # Test the autogenerated checksums
        params.pop(f"Checksum{algorithm}")
        response = aws_client.s3.put_object(**params)
        snapshot.match("put-object-autogenerated", response)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key,
            ObjectAttributes=["ETag", "Checksum"],
        )
        snapshot.match("get-object-attrs-auto-generated", object_attrs)

        get_object_with_checksum = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=key, ChecksumMode="ENABLED"
        )
        snapshot.match("head-object-with-checksum", get_object_with_checksum)

    @markers.aws.validated
    @pytest.mark.parametrize("algorithm", ["CRC32", "CRC32C", "SHA1", "SHA256", "CRC64NVME", None])
    def test_s3_get_object_checksum(self, s3_bucket, snapshot, algorithm, aws_client):
        key = "test-checksum-retrieval"
        body = b"test-checksum"
        kwargs = {}
        if algorithm:
            kwargs["ChecksumAlgorithm"] = algorithm
        put_object = aws_client.s3.put_object(Bucket=s3_bucket, Key=key, Body=body, **kwargs)
        snapshot.match("put-object", put_object)

        get_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=key)
        snapshot.match("get-object", get_object)

        get_object_with_checksum = aws_client.s3.get_object(
            Bucket=s3_bucket, Key=key, ChecksumMode="ENABLED"
        )
        snapshot.match("get-object-with-checksum", get_object_with_checksum)

        # test that the casing of ChecksumMode is not important, the spec indicate only ENABLED
        head_object_with_checksum = aws_client.s3.get_object(
            Bucket=s3_bucket, Key=key, ChecksumMode="enabled"
        )
        snapshot.match("head-object-with-checksum", head_object_with_checksum)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key,
            ObjectAttributes=["Checksum"],
        )
        snapshot.match("get-object-attrs", object_attrs)

    @markers.aws.validated
    def test_s3_checksum_with_content_encoding(self, s3_bucket, snapshot, aws_client):
        data = "1234567890 " * 100
        key = "test.gz"

        # Write contents to memory rather than a file.
        upload_file_object = BytesIO()
        # GZIP has the timestamp and filename in its headers, so set them to have same ETag and hash for AWS and LS
        # hardcode the timestamp, the filename will be an empty string because we're passing a BytesIO stream
        mtime = 1676569620
        with gzip.GzipFile(fileobj=upload_file_object, mode="w", mtime=mtime) as filestream:
            filestream.write(data.encode("utf-8"))

        response = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=key,
            ContentEncoding="gzip",
            Body=upload_file_object.getvalue(),
            ChecksumAlgorithm="SHA256",
        )
        snapshot.match("put-object", response)

        get_object = aws_client.s3.get_object(Bucket=s3_bucket, Key=key)
        # FIXME: empty the encoded GZIP stream so it does not break snapshot (can't decode it to UTF-8)
        get_object["Body"].read()
        snapshot.match("get-object", get_object)

        get_object_with_checksum = aws_client.s3.get_object(
            Bucket=s3_bucket, Key=key, ChecksumMode="ENABLED"
        )
        get_object_with_checksum["Body"].read()
        snapshot.match("get-object-with-checksum", get_object_with_checksum)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key,
            ObjectAttributes=["Checksum"],
        )
        snapshot.match("get-object-attrs", object_attrs)

    @markers.aws.validated
    def test_s3_checksum_no_algorithm(self, s3_bucket, snapshot, aws_client):
        key = f"file-{short_uid()}"
        data = b"test data.."

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key=key,
                Body=data,
                ChecksumSHA256=short_uid(),
            )
        snapshot.match("put-wrong-checksum", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.put_object(
                Bucket=s3_bucket,
                Key=key,
                Body=data,
                ChecksumSHA256=short_uid(),
                ChecksumCRC32=short_uid(),
            )
        snapshot.match("put-2-checksums", e.value.response)

        resp = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=key,
            Body=data,
            ChecksumSHA256=hash_sha256(data),
        )
        snapshot.match("put-right-checksum", resp)

        head_obj = aws_client.s3.head_object(Bucket=s3_bucket, Key=key, ChecksumMode="ENABLED")
        snapshot.match("head-obj", head_obj)

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        paths=[
            "$.wrong-checksum.Error.HostId",  # FIXME: not returned in the exception
        ]
    )
    def test_s3_checksum_no_automatic_sdk_calculation(
        self, s3_bucket, snapshot, aws_client, aws_http_client_factory
    ):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("HostId"),
                snapshot.transform.key_value("RequestId"),
            ]
        )
        headers = {"x-amz-content-sha256": "UNSIGNED-PAYLOAD"}
        data = b"test data.."
        hash_256_data = hash_sha256(data)

        s3_http_client = aws_http_client_factory("s3", signer_factory=SigV4Auth)
        bucket_url = _bucket_url(s3_bucket)

        wrong_object_key = "wrong-checksum"
        wrong_put_object_url = f"{bucket_url}/{wrong_object_key}"
        wrong_put_object_headers = {**headers, "x-amz-checksum-sha256": short_uid()}
        resp = s3_http_client.put(wrong_put_object_url, headers=wrong_put_object_headers, data=data)
        resp_dict = xmltodict.parse(resp.content)
        snapshot.match("wrong-checksum", resp_dict)

        object_key = "right-checksum"
        put_object_url = f"{bucket_url}/{object_key}"
        put_object_headers = {**headers, "x-amz-checksum-sha256": hash_256_data}
        resp = s3_http_client.put(put_object_url, headers=put_object_headers, data=data)
        assert resp.ok

        head_obj = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=object_key, ChecksumMode="ENABLED"
        )
        snapshot.match("head-obj-right-checksum", head_obj)

        algo_object_key = "algo-only-checksum"
        algo_put_object_url = f"{bucket_url}/{algo_object_key}"
        algo_put_object_headers = {**headers, "x-amz-checksum-algorithm": "SHA256"}
        resp = s3_http_client.put(algo_put_object_url, headers=algo_put_object_headers, data=data)
        assert resp.ok

        head_obj = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=algo_object_key, ChecksumMode="ENABLED"
        )
        snapshot.match("head-obj-only-checksum-algo", head_obj)

        wrong_algo_object_key = "algo-wrong-checksum"
        wrong_algo_put_object_url = f"{bucket_url}/{wrong_algo_object_key}"
        wrong_algo_put_object_headers = {**headers, "x-amz-checksum-algorithm": "TEST"}
        resp = s3_http_client.put(
            wrong_algo_put_object_url, headers=wrong_algo_put_object_headers, data=data
        )
        assert resp.ok

        algo_diff_object_key = "algo-diff-checksum"
        algo_diff_put_object_url = f"{bucket_url}/{algo_diff_object_key}"
        algo_diff_put_object_headers = {
            **headers,
            "x-amz-checksum-algorithm": "SHA1",
            "x-amz-checksum-sha256": hash_256_data,
        }
        resp = s3_http_client.put(
            algo_diff_put_object_url, headers=algo_diff_put_object_headers, data=data
        )
        assert resp.ok

        head_obj = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=algo_diff_object_key, ChecksumMode="ENABLED"
        )
        snapshot.match("head-obj-diff-checksum-algo", head_obj)

        # https://docs.aws.amazon.com/sdkref/latest/guide/feature-dataintegrity.html
        no_checksum_object_key = "no-checksum"
        no_checksum_put_object_url = f"{bucket_url}/{no_checksum_object_key}"
        resp = s3_http_client.put(no_checksum_put_object_url, headers=headers, data=data)
        assert resp.ok

        head_obj = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=no_checksum_object_key, ChecksumMode="ENABLED"
        )
        snapshot.match("head-obj-no-checksum", head_obj)

        obj_attributes = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket, Key=no_checksum_object_key, ObjectAttributes=["Checksum"]
        )
        snapshot.match("get-obj-attrs-no-checksum", obj_attributes)

        dest_checksum_object_key = "dest-key-checksum"
        copy_obj = aws_client.s3.copy_object(
            Bucket=s3_bucket,
            Key=dest_checksum_object_key,
            CopySource=f"{s3_bucket}/{no_checksum_object_key}",
        )
        snapshot.match("copy-obj-default-checksum", copy_obj)

        obj_attributes = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket, Key=dest_checksum_object_key, ObjectAttributes=["Checksum"]
        )
        snapshot.match("get-copy-obj-attrs-no-checksum", obj_attributes)


class TestS3MultipartUploadChecksum:
    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        # it seems the PartNumber might not be deterministic, possibly parallelized on S3 side?
        paths=["$.complete-multipart-wrong-parts-checksum.Error.PartNumber"]
    )
    @pytest.mark.parametrize("algorithm", ["CRC32", "CRC32C", "SHA1", "SHA256"])
    def test_complete_multipart_parts_checksum_composite(
        self, s3_bucket, snapshot, aws_client, algorithm
    ):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("UploadId"),
                snapshot.transform.key_value("DisplayName", reference_replacement=False),
                snapshot.transform.key_value("ID", reference_replacement=False),
            ]
        )

        key_name = "test-multipart-checksum"
        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket, Key=key_name, ChecksumAlgorithm=algorithm, ChecksumType="COMPOSITE"
        )
        snapshot.match("create-mpu-checksum", response)
        upload_id = response["UploadId"]

        # data must be at least 5MiB
        part_data = "a" * (5_242_880 + 1)
        part_data = to_bytes(part_data)

        parts = 3
        multipart_upload_parts = []
        for part in range(parts):
            # Write contents to memory rather than a file.
            part_number = part + 1
            if part_number == parts:
                # the last part does not need to be 5mb, so make it smaller
                part_data = part_data[:10]
            upload_file_object = BytesIO(part_data)
            response = aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name,
                Body=upload_file_object,
                PartNumber=part_number,
                UploadId=upload_id,
                ChecksumAlgorithm=algorithm,
            )
            snapshot.match(f"upload-part-{part}", response)
            multipart_upload_parts.append(
                {
                    "ETag": response["ETag"],
                    "PartNumber": part_number,
                    f"Checksum{algorithm}": response[f"Checksum{algorithm}"],
                }
            )

        response = aws_client.s3.list_parts(Bucket=s3_bucket, Key=key_name, UploadId=upload_id)
        snapshot.match("list-parts", response)

        with pytest.raises(ClientError) as e:
            # testing completing the multipart with bad checksums of parts
            multipart_upload_parts_wrong_checksum = [
                {
                    "ETag": upload_part["ETag"],
                    "PartNumber": upload_part["PartNumber"],
                    f"Checksum{algorithm}": get_checksum_for_algorithm(algorithm, b"bbb"),
                }
                for upload_part in multipart_upload_parts
            ]
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload={"Parts": multipart_upload_parts_wrong_checksum},
                UploadId=upload_id,
            )
        snapshot.match("complete-multipart-wrong-parts-checksum", e.value.response)

        with pytest.raises(ClientError) as e:
            # testing completing the multipart without the checksum of parts
            multipart_upload_parts_no_checksum = [
                {"ETag": upload_part["ETag"], "PartNumber": upload_part["PartNumber"]}
                for upload_part in multipart_upload_parts
            ]
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload={"Parts": multipart_upload_parts_no_checksum},
                UploadId=upload_id,
            )
        snapshot.match("complete-multipart-no-checksum", e.value.response)

        response = aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload={"Parts": multipart_upload_parts},
            UploadId=upload_id,
        )
        snapshot.match("complete-multipart-checksum", response)

        get_object_with_checksum = aws_client.s3.get_object(
            Bucket=s3_bucket, Key=key_name, ChecksumMode="ENABLED"
        )
        # empty the stream, it's a 15MB string, we don't need to snapshot that
        get_object_with_checksum["Body"].read()
        snapshot.match("get-object-with-checksum", get_object_with_checksum)

        head_object_with_checksum = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=key_name, ChecksumMode="ENABLED"
        )
        snapshot.match("head-object-with-checksum", head_object_with_checksum)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key_name,
            ObjectAttributes=["Checksum", "ETag", "ObjectParts"],
        )
        snapshot.match("get-object-attrs", object_attrs)

        dest_key = "mpu-copy-checksum"
        copy_obj = aws_client.s3.copy_object(
            Bucket=s3_bucket, Key=dest_key, CopySource=f"{s3_bucket}/{key_name}"
        )
        snapshot.match("copy-obj-checksum", copy_obj)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=dest_key,
            ObjectAttributes=["Checksum", "ETag", "ObjectParts"],
        )
        snapshot.match("get-copy-object-attrs", object_attrs)

        get_object_part_checksum = aws_client.s3.get_object(
            Bucket=s3_bucket,
            Key=key_name,
            PartNumber=3,
            ChecksumMode="ENABLED",
        )
        snapshot.match("get-object-part-checksum", get_object_part_checksum)

    @markers.aws.validated
    @pytest.mark.parametrize("algorithm", ["CRC32", "CRC32C", "SHA1", "SHA256", "CRC64NVME"])
    @pytest.mark.parametrize("checksum_type", ["COMPOSITE", "FULL_OBJECT"])
    def test_multipart_checksum_type_compatibility(
        self, aws_client, s3_bucket, snapshot, algorithm, checksum_type
    ):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("UploadId"),
            ]
        )
        try:
            key_name = "test-multipart-checksum-compat"
            response = aws_client.s3.create_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                ChecksumAlgorithm=algorithm,
                ChecksumType=checksum_type,
            )
            snapshot.match("create-mpu-checksum", response)
        except ClientError as e:
            snapshot.match("create-mpu-checksum-exc", e.response)

    @markers.aws.validated
    @pytest.mark.parametrize("algorithm", ["CRC32", "CRC32C", "SHA1", "SHA256", "CRC64NVME"])
    def test_multipart_checksum_type_default_for_checksum(
        self, aws_client, s3_bucket, snapshot, algorithm
    ):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("UploadId"),
            ]
        )
        # test the default ChecksumType for each ChecksumAlgorithm
        key_name = "test-multipart-checksum-default"
        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket, Key=key_name, ChecksumAlgorithm=algorithm
        )
        snapshot.match("create-mpu-default-checksum-type", response)

    @markers.aws.validated
    @pytest.mark.parametrize("algorithm", ["CRC32", "CRC32C", "SHA1", "SHA256", "CRC64NVME"])
    def test_multipart_upload_part_checksum_exception(
        self, aws_client, s3_bucket, snapshot, algorithm
    ):
        key_name = "test-multipart-checksum-default"
        response = aws_client.s3.create_multipart_upload(Bucket=s3_bucket, Key=key_name)
        upload_id = response["UploadId"]
        body = b"right body"

        with pytest.raises(ClientError) as e:
            kwargs = {
                f"Checksum{algorithm}": short_uid(),
            }
            aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name,
                UploadId=upload_id,
                PartNumber=1,
                Body=body,
                ChecksumAlgorithm=algorithm,
                **kwargs,
            )
        snapshot.match("put-wrong-checksum-no-b64", e.value.response)

        with pytest.raises(ClientError) as e:
            kwargs = {f"Checksum{algorithm}": get_checksum_for_algorithm(algorithm, b"bad data")}
            aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name,
                UploadId=upload_id,
                PartNumber=1,
                Body=body,
                ChecksumAlgorithm=algorithm,
                **kwargs,
            )
        snapshot.match("put-wrong-checksum-value", e.value.response)

    @markers.aws.validated
    def test_multipart_parts_checksum_exceptions_composite(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("UploadId"),
                snapshot.transform.key_value("DisplayName", reference_replacement=False),
                snapshot.transform.key_value("ID", reference_replacement=False),
            ]
        )

        key_name = "test-multipart-checksum-exc"
        with pytest.raises(ClientError) as e:
            aws_client.s3.create_multipart_upload(
                Bucket=s3_bucket, Key=key_name, ChecksumAlgorithm="TEST"
            )
        snapshot.match("create-mpu-wrong-checksum-algo", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.create_multipart_upload(
                Bucket=s3_bucket, Key=key_name, ChecksumType="COMPOSITE"
            )
        snapshot.match("create-mpu-no-checksum-algo-with-type", e.value.response)

        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket, Key=key_name, ChecksumType="COMPOSITE", ChecksumAlgorithm="CRC32"
        )
        snapshot.match("create-mpu-composite-checksum", response)
        upload_id = response["UploadId"]

        list_multiparts = aws_client.s3.list_multipart_uploads(Bucket=s3_bucket)
        snapshot.match("list-multiparts", list_multiparts)

        part_data = "abc"
        checksum_part = hash_sha256(to_bytes(part_data))

        upload_resp = aws_client.s3.upload_part(
            Bucket=s3_bucket,
            Key=key_name,
            Body=part_data,
            PartNumber=1,
            UploadId=upload_id,
        )
        snapshot.match("upload-part-no-checksum-ok", upload_resp)

        with pytest.raises(ClientError) as e:
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload={
                    "Parts": [
                        {
                            "ETag": upload_resp["ETag"],
                            "PartNumber": 1,
                            "ChecksumSHA256": checksum_part,
                        }
                    ],
                },
                UploadId=upload_id,
            )
        snapshot.match("complete-part-with-checksum", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload={
                    "Parts": [
                        {
                            "ETag": upload_resp["ETag"],
                            "PartNumber": 1,
                        }
                    ],
                },
                UploadId=upload_id,
                ChecksumType="FULL_OBJECT",
            )
        snapshot.match("complete-part-with-bad-checksum-type", e.value.response)

        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket, Key=key_name, ChecksumAlgorithm="SHA256"
        )
        snapshot.match("create-mpu-with-checksum", response)
        upload_id = response["UploadId"]

        with pytest.raises(ClientError) as e:
            aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name,
                Body=part_data,
                PartNumber=1,
                UploadId=upload_id,
            )
        snapshot.match("upload-part-different-checksum-exc", e.value.response)

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        # it seems the PartNumber might not be deterministic, possibly parallelized on S3 side?
        paths=[
            "$.complete-multipart-wrong-parts-checksum.Error.PartNumber",
            "$.complete-multipart-wrong-parts-checksum.Error.ETag",
        ]
    )
    @pytest.mark.parametrize("algorithm", ["CRC32", "CRC32C", "CRC64NVME"])
    def test_complete_multipart_parts_checksum_full_object(
        self, s3_bucket, snapshot, aws_client, algorithm
    ):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("UploadId"),
                snapshot.transform.key_value("DisplayName", reference_replacement=False),
                snapshot.transform.key_value("ID", reference_replacement=False),
            ]
        )

        key_name = "test-multipart-checksum"
        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket, Key=key_name, ChecksumAlgorithm=algorithm, ChecksumType="FULL_OBJECT"
        )
        snapshot.match("create-mpu-checksum", response)
        upload_id = response["UploadId"]

        # data must be at least 5MiB
        part_data = "a" * (5_242_880 + 1)
        part_data = to_bytes(part_data)
        full_object_hash = get_checksum_for_algorithm(
            algorithm, to_bytes(part_data * 2 + part_data[:10])
        )

        parts = 3
        multipart_upload_parts = []
        for part in range(parts):
            # Write contents to memory rather than a file.
            part_number = part + 1
            if part_number == parts:
                # the last part does not need to be 5mb, so make it smaller
                part_data = part_data[:10]
            upload_file_object = BytesIO(part_data)
            response = aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name,
                Body=upload_file_object,
                PartNumber=part_number,
                UploadId=upload_id,
                ChecksumAlgorithm=algorithm,
            )
            snapshot.match(f"upload-part-{part}", response)
            # with `FULL_OBJECT`, there is no need to store intermediate part checksums
            multipart_upload_parts.append({"ETag": response["ETag"], "PartNumber": part_number})

        response = aws_client.s3.list_parts(Bucket=s3_bucket, Key=key_name, UploadId=upload_id)
        snapshot.match("list-parts", response)

        with pytest.raises(ClientError) as e:
            # testing completing the multipart with bad checksums of parts
            multipart_upload_parts_wrong_checksum = [
                {
                    "ETag": upload_part["ETag"],
                    "PartNumber": upload_part["PartNumber"],
                    f"Checksum{algorithm}": get_checksum_for_algorithm(algorithm, b"bbb"),
                }
                for upload_part in multipart_upload_parts
            ]
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload={"Parts": multipart_upload_parts_wrong_checksum},
                UploadId=upload_id,
            )
        snapshot.match("complete-multipart-wrong-parts-checksum", e.value.response)

        kwargs = {f"Checksum{algorithm.upper()}": full_object_hash}
        response = aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload={"Parts": multipart_upload_parts},
            UploadId=upload_id,
            ChecksumType="FULL_OBJECT",
            **kwargs,
        )
        snapshot.match("complete-multipart-checksum", response)

        get_object_with_checksum = aws_client.s3.get_object(
            Bucket=s3_bucket, Key=key_name, ChecksumMode="ENABLED"
        )
        # empty the stream, it's a 15MB string, we don't need to snapshot that
        get_object_with_checksum["Body"].read()
        snapshot.match("get-object-with-checksum", get_object_with_checksum)

        head_object_with_checksum = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=key_name, ChecksumMode="ENABLED"
        )
        snapshot.match("head-object-with-checksum", head_object_with_checksum)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key_name,
            ObjectAttributes=["Checksum", "ETag", "ObjectParts"],
        )
        snapshot.match("get-object-attrs", object_attrs)

        dest_key = "mpu-copy-checksum"
        copy_obj = aws_client.s3.copy_object(
            Bucket=s3_bucket, Key=dest_key, CopySource=f"{s3_bucket}/{key_name}"
        )
        snapshot.match("copy-obj-checksum", copy_obj)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=dest_key,
            ObjectAttributes=["Checksum", "ETag", "ObjectParts"],
        )
        snapshot.match("get-copy-object-attrs", object_attrs)

        get_object_part_checksum = aws_client.s3.get_object(
            Bucket=s3_bucket,
            Key=key_name,
            PartNumber=3,
            ChecksumMode="ENABLED",
        )
        snapshot.match("get-object-part-checksum", get_object_part_checksum)

    @markers.aws.validated
    def test_multipart_parts_checksum_exceptions_full_object(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("UploadId"),
                snapshot.transform.key_value("DisplayName", reference_replacement=False),
                snapshot.transform.key_value("ID", reference_replacement=False),
            ]
        )

        key_name = "test-multipart-checksum-exc"

        with pytest.raises(ClientError) as e:
            aws_client.s3.create_multipart_upload(
                Bucket=s3_bucket, Key=key_name, ChecksumType="FULL_OBJECT"
            )
        snapshot.match("create-mpu-no-checksum-algo-with-type", e.value.response)

        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket, Key=key_name, ChecksumAlgorithm="CRC32C", ChecksumType="FULL_OBJECT"
        )
        snapshot.match("create-mpu-checksum-crc32c", response)
        upload_id = response["UploadId"]

        list_multiparts = aws_client.s3.list_multipart_uploads(Bucket=s3_bucket)
        snapshot.match("list-multiparts", list_multiparts)

        part_data = "abc"
        checksum_part = checksum_crc32c(part_data)

        upload_resp = aws_client.s3.upload_part(
            Bucket=s3_bucket,
            Key=key_name,
            Body=part_data,
            PartNumber=1,
            UploadId=upload_id,
            ChecksumAlgorithm="CRC32C",
        )
        snapshot.match("upload-part-no-checksum-ok", upload_resp)

        mpu_data = {
            "Parts": [
                {
                    "ETag": upload_resp["ETag"],
                    "PartNumber": 1,
                    "ChecksumCRC32C": checksum_part,
                }
            ],
        }
        with pytest.raises(ClientError) as e:
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload=mpu_data,
                UploadId=upload_id,
                ChecksumType="COMPOSITE",
            )
        snapshot.match("complete-part-bad-checksum-type", e.value.response)

        with pytest.raises(ClientError) as e:
            composite_hash = checksum_crc32c(base64.b64decode(checksum_part))
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload=mpu_data,
                UploadId=upload_id,
                ChecksumCRC32C=f"{composite_hash}-1",
            )
        snapshot.match("complete-part-good-checksum-no-type", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload=mpu_data,
                UploadId=upload_id,
                ChecksumCRC32C=checksum_part,
            )
        snapshot.match("complete-part-only-checksum-algo", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload=mpu_data,
                UploadId=upload_id,
                ChecksumCRC64NVME=checksum_crc64nvme(part_data),
            )
        snapshot.match("complete-part-only-checksum-algo-diff", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload=mpu_data,
                UploadId=upload_id,
                ChecksumCRC32C=checksum_crc32c("bad string"),
                ChecksumType="FULL_OBJECT",
            )
        snapshot.match("complete-part-bad-checksum", e.value.response)

        with pytest.raises(ClientError) as e:
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload=mpu_data,
                UploadId=upload_id,
                ChecksumCRC32=checksum_crc32("bad string"),
                ChecksumType="FULL_OBJECT",
            )
        snapshot.match("complete-part-bad-checksum-algo", e.value.response)

        complete_mpu = aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload=mpu_data,
            UploadId=upload_id,
            ChecksumCRC32C=checksum_part,
            ChecksumType="FULL_OBJECT",
        )
        snapshot.match("complete-success", complete_mpu)

        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket, Key=key_name, ChecksumAlgorithm="CRC32C", ChecksumType="FULL_OBJECT"
        )
        snapshot.match("create-mpu-with-checksum", response)
        upload_id = response["UploadId"]

        with pytest.raises(ClientError) as e:
            aws_client.s3.upload_part(
                Bucket=s3_bucket,
                Key=key_name,
                Body=part_data,
                PartNumber=1,
                UploadId=upload_id,
                ChecksumAlgorithm="CRC32",
            )
        snapshot.match("upload-part-different-checksum-exc", e.value.response)

    @markers.aws.validated
    def test_complete_multipart_parts_checksum_default(self, s3_bucket, snapshot, aws_client):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("UploadId"),
                snapshot.transform.key_value("DisplayName", reference_replacement=False),
                snapshot.transform.key_value("ID", reference_replacement=False),
            ]
        )

        key_name = "test-multipart-checksum"
        response = aws_client.s3.create_multipart_upload(Bucket=s3_bucket, Key=key_name)
        snapshot.match("create-mpu-no-checksum", response)
        upload_id = response["UploadId"]

        list_multiparts = aws_client.s3.list_multipart_uploads(Bucket=s3_bucket)
        snapshot.match("list-multiparts", list_multiparts)

        data = b"aaa"

        upload_part = aws_client.s3.upload_part(
            Bucket=s3_bucket,
            Key=key_name,
            Body=data,
            PartNumber=1,
            UploadId=upload_id,
            ChecksumAlgorithm="CRC32C",
        )
        snapshot.match("upload-part-different-checksum-than-default", upload_part)

        list_parts = aws_client.s3.list_parts(Bucket=s3_bucket, Key=key_name, UploadId=upload_id)
        snapshot.match("list-parts", list_parts)

        multipart_upload_parts = [
            {
                "ETag": upload_part["ETag"],
                "PartNumber": 1,
                "ChecksumCRC32C": upload_part["ChecksumCRC32C"],
            }
        ]
        multipart_upload_parts_no_checksum = [
            {"ETag": upload_part["ETag"], "PartNumber": upload_part["PartNumber"]}
            for upload_part in multipart_upload_parts
        ]

        with pytest.raises(ClientError) as e:
            # testing completing the multipart with the parts checksums will fail if the multipart does not have a
            # configured checksum
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload={"Parts": multipart_upload_parts},
                UploadId=upload_id,
            )
        snapshot.match("complete-multipart-parts-checksum", e.value.response)

        with pytest.raises(ClientError) as e:
            # testing completing the multipart with different checksum type than uploaded
            multipart_upload_parts_wrong_checksum = [
                {
                    "ETag": upload_part["ETag"],
                    "PartNumber": upload_part["PartNumber"],
                    "ChecksumSHA256": hash_sha256(data),
                }
                for upload_part in multipart_upload_parts
            ]
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload={"Parts": multipart_upload_parts_wrong_checksum},
                UploadId=upload_id,
            )
        snapshot.match("complete-multipart-wrong-parts-checksum", e.value.response)

        with pytest.raises(ClientError) as e:
            # testing completing the multipart with bad checksum type?
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload={"Parts": multipart_upload_parts_no_checksum},
                UploadId=upload_id,
                ChecksumType="FULL_OBJECT",
            )
        snapshot.match("complete-multipart-full-object-type", e.value.response)

        with pytest.raises(ClientError) as e:
            # testing completing the multipart with bad checksum type?
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload={"Parts": multipart_upload_parts_no_checksum},
                UploadId=upload_id,
                ChecksumType="COMPOSITE",
            )
        snapshot.match("complete-multipart-composite-type", e.value.response)

        # complete with the checksums even if unspecified
        response = aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload={"Parts": multipart_upload_parts_no_checksum},
            UploadId=upload_id,
            # bad composite checksum, seems like it is ignored
            ChecksumCRC32C=f"{checksum_crc32c(base64.b64decode(checksum_crc32c(data)))}-2",
        )
        snapshot.match("complete-multipart-checksum", response)

        get_object_with_checksum = aws_client.s3.get_object(
            Bucket=s3_bucket, Key=key_name, ChecksumMode="ENABLED"
        )
        # empty the stream, it's a 15MB string, we don't need to snapshot that
        get_object_with_checksum["Body"].read()
        snapshot.match("get-object-with-checksum", get_object_with_checksum)

        head_object_with_checksum = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=key_name, ChecksumMode="ENABLED"
        )
        snapshot.match("head-object-with-checksum", head_object_with_checksum)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key_name,
            ObjectAttributes=["Checksum", "ETag", "ObjectParts"],
        )
        snapshot.match("get-object-attrs", object_attrs)

        dest_key = "mpu-copy-checksum"
        copy_obj = aws_client.s3.copy_object(
            Bucket=s3_bucket, Key=dest_key, CopySource=f"{s3_bucket}/{key_name}"
        )
        snapshot.match("copy-obj-checksum", copy_obj)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=dest_key,
            ObjectAttributes=["Checksum", "ETag", "ObjectParts"],
        )
        snapshot.match("get-copy-object-attrs", object_attrs)

    @markers.aws.validated
    def test_complete_multipart_parts_checksum_full_object_default(
        self, s3_bucket, snapshot, aws_client
    ):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("UploadId"),
                snapshot.transform.key_value("DisplayName", reference_replacement=False),
                snapshot.transform.key_value("ID", reference_replacement=False),
            ]
        )

        key_name = "test-multipart-checksum"
        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket, Key=key_name, ChecksumAlgorithm="CRC64NVME"
        )
        snapshot.match("create-mpu-checksum-crc64", response)
        upload_id = response["UploadId"]

        data = b"aaa"

        upload_part = aws_client.s3.upload_part(
            Bucket=s3_bucket,
            Key=key_name,
            Body=data,
            PartNumber=1,
            UploadId=upload_id,
            ChecksumAlgorithm="CRC64NVME",
        )
        snapshot.match("upload-part", upload_part)

        # complete with no checksum type specified, just all default values
        response = aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload={
                "Parts": [
                    {
                        "ETag": upload_part["ETag"],
                        "PartNumber": 1,
                        "ChecksumCRC64NVME": upload_part["ChecksumCRC64NVME"],
                    }
                ]
            },
            UploadId=upload_id,
        )
        snapshot.match("complete-multipart-checksum", response)

        get_object_with_checksum = aws_client.s3.get_object(
            Bucket=s3_bucket, Key=key_name, ChecksumMode="ENABLED"
        )
        # empty the stream
        get_object_with_checksum["Body"].read()
        snapshot.match("get-object-with-checksum", get_object_with_checksum)

        head_object_with_checksum = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=key_name, ChecksumMode="ENABLED"
        )
        snapshot.match("head-object-with-checksum", head_object_with_checksum)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key_name,
            ObjectAttributes=["Checksum", "ETag", "ObjectParts"],
        )
        snapshot.match("get-object-attrs", object_attrs)

    @markers.aws.validated
    def test_multipart_size_validation(self, aws_client, s3_bucket, snapshot):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("UploadId"),
                snapshot.transform.key_value("Location"),
            ]
        )
        # test the default ChecksumType for each ChecksumAlgorithm
        key_name = "test-multipart-size"
        response = aws_client.s3.create_multipart_upload(Bucket=s3_bucket, Key=key_name)
        snapshot.match("create-mpu", response)
        upload_id = response["UploadId"]

        data = b"aaaa"

        upload_part = aws_client.s3.upload_part(
            Bucket=s3_bucket,
            Key=key_name,
            Body=data,
            PartNumber=1,
            UploadId=upload_id,
        )
        snapshot.match("upload-part", upload_part)

        parts = [
            {
                "ETag": upload_part["ETag"],
                "PartNumber": 1,
            }
        ]

        with pytest.raises(ClientError) as e:
            aws_client.s3.complete_multipart_upload(
                Bucket=s3_bucket,
                Key=key_name,
                MultipartUpload={"Parts": parts},
                UploadId=upload_id,
                MpuObjectSize=len(data) + 1,
            )
        snapshot.match("complete-multipart-wrong-size", e.value.response)

        success = aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload={"Parts": parts},
            UploadId=upload_id,
            MpuObjectSize=len(data),
        )
        snapshot.match("complete-multipart-good-size", success)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key_name,
            ObjectAttributes=["Checksum", "ETag"],
        )
        snapshot.match("get-object-attrs", object_attrs)

    @markers.aws.validated
    @pytest.mark.parametrize("checksum_type", ("COMPOSITE", "FULL_OBJECT"))
    def test_multipart_upload_part_copy_checksum(
        self, s3_bucket, snapshot, aws_client, checksum_type
    ):
        snapshot.add_transformer(
            [
                snapshot.transform.key_value("Bucket", reference_replacement=False),
                snapshot.transform.key_value("Location"),
                snapshot.transform.key_value("UploadId"),
                snapshot.transform.key_value("DisplayName", reference_replacement=False),
                snapshot.transform.key_value("ID", reference_replacement=False),
            ]
        )

        part_key = "test-part-checksum"
        put_object = aws_client.s3.put_object(
            Bucket=s3_bucket,
            Key=part_key,
            Body="this is a part",
        )
        snapshot.match("put-object", put_object)

        key_name = "test-multipart-checksum"
        response = aws_client.s3.create_multipart_upload(
            Bucket=s3_bucket, Key=key_name, ChecksumAlgorithm="CRC32C", ChecksumType=checksum_type
        )
        snapshot.match("create-mpu-checksum-sha256", response)
        upload_id = response["UploadId"]

        copy_source_key = f"{s3_bucket}/{part_key}"
        upload_part_copy = aws_client.s3.upload_part_copy(
            Bucket=s3_bucket,
            UploadId=upload_id,
            Key=key_name,
            PartNumber=1,
            CopySource=copy_source_key,
        )
        snapshot.match("upload-part-copy", upload_part_copy)

        list_parts = aws_client.s3.list_parts(
            Bucket=s3_bucket,
            UploadId=upload_id,
            Key=key_name,
        )
        snapshot.match("list-parts", list_parts)

        # complete with no checksum type specified, just all default values
        response = aws_client.s3.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload={
                "Parts": [
                    {
                        "ETag": upload_part_copy["CopyPartResult"]["ETag"],
                        "PartNumber": 1,
                        "ChecksumCRC32C": upload_part_copy["CopyPartResult"]["ChecksumCRC32C"],
                    }
                ]
            },
            UploadId=upload_id,
        )
        snapshot.match("complete-multipart-checksum", response)

        get_object_with_checksum = aws_client.s3.get_object(
            Bucket=s3_bucket, Key=key_name, ChecksumMode="ENABLED"
        )
        snapshot.match("get-object-with-checksum", get_object_with_checksum)

        head_object_with_checksum = aws_client.s3.head_object(
            Bucket=s3_bucket, Key=key_name, ChecksumMode="ENABLED"
        )
        snapshot.match("head-object-with-checksum", head_object_with_checksum)

        object_attrs = aws_client.s3.get_object_attributes(
            Bucket=s3_bucket,
            Key=key_name,
            ObjectAttributes=["Checksum", "ETag", "ObjectParts"],
        )
        snapshot.match("get-object-attrs", object_attrs)


def _s3_client_pre_signed_client(conf: Config, endpoint_url: str = None):
    if is_aws_cloud():
        return boto3.client("s3", config=conf, endpoint_url=endpoint_url)

    # TODO: create a similar ClientFactory for these parameters
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        config=conf,
        aws_access_key_id=s3_constants.DEFAULT_PRE_SIGNED_ACCESS_KEY_ID,
        aws_secret_access_key=s3_constants.DEFAULT_PRE_SIGNED_SECRET_ACCESS_KEY,
    )


def _endpoint_url(region: str = "", localstack_host: str = None) -> str:
    if not region:
        region = AWS_REGION_US_EAST_1
    if is_aws_cloud():
        if region == "us-east-1":
            return "https://s3.amazonaws.com"
        else:
            return f"http://s3.{region}.amazonaws.com"
    if region == "us-east-1":
        return f"{config.internal_service_url(host=localstack_host or S3_VIRTUAL_HOSTNAME)}"
    return config.internal_service_url(host=f"s3.{region}.{LOCALHOST_HOSTNAME}")


def _bucket_url(bucket_name: str, region: str = "", localstack_host: str = None) -> str:
    return f"{_endpoint_url(region, localstack_host)}/{bucket_name}"


def _website_bucket_url(bucket_name: str):
    # TODO depending on region the syntax of the website vary (dot vs dash before region)
    if is_aws_cloud():
        region = AWS_REGION_US_EAST_1
        return f"http://{bucket_name}.s3-website-{region}.amazonaws.com"
    return _bucket_url_vhost(
        bucket_name, localstack_host=localstack.config.S3_STATIC_WEBSITE_HOSTNAME
    )


def _bucket_url_vhost(bucket_name: str, region: str = "", localstack_host: str = None) -> str:
    if not region:
        region = AWS_REGION_US_EAST_1
    if is_aws_cloud():
        if region == "us-east-1":
            return f"https://{bucket_name}.s3.amazonaws.com"
        else:
            return f"https://{bucket_name}.s3.{region}.amazonaws.com"

    host_definition = get_localstack_host()
    if localstack_host:
        host_and_port = f"{localstack_host}:{config.GATEWAY_LISTEN[0].port}"
    else:
        host_and_port = (
            f"s3.{region}.{host_definition.host_and_port()}"
            if region != "us-east-1"
            else f"s3.{host_definition.host_and_port()}"
        )

    # TODO might add the region here
    return f"{config.get_protocol()}://{bucket_name}.{host_and_port}"


def _generate_presigned_url(
    client: "S3Client", params: dict, expires: int, client_method: str = "get_object"
) -> str:
    return client.generate_presigned_url(
        client_method,
        Params=params,
        ExpiresIn=expires,
    )


def _make_url_invalid(url_prefix: str, object_key: str, url: str) -> str:
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    if "Signature" in query_params:
        # v2 style
        return "{}/{}?AWSAccessKeyId={}&Signature={}&Expires={}".format(
            url_prefix,
            object_key,
            query_params["AWSAccessKeyId"][0],
            query_params["Signature"][0],
            query_params["Expires"][0],
        )
    else:
        # v4 style
        return (
            "{}/{}?X-Amz-Algorithm=AWS4-HMAC-SHA256&"
            "X-Amz-Credential={}&X-Amz-Date={}&"
            "X-Amz-Expires={}&X-Amz-SignedHeaders=host&"
            "X-Amz-Signature={}"
        ).format(
            url_prefix,
            object_key,
            quote(query_params["X-Amz-Credential"][0]).replace("/", "%2F"),
            query_params["X-Amz-Date"][0],
            query_params["X-Amz-Expires"][0],
            query_params["X-Amz-Signature"][0],
        )


@pytest.fixture
def presigned_snapshot_transformers(snapshot):
    snapshot.add_transformers_list(
        [
            snapshot.transform.key_value("AWSAccessKeyId"),
            snapshot.transform.key_value("HostId", reference_replacement=False),
            snapshot.transform.key_value("RequestId"),
            snapshot.transform.key_value("SignatureProvided"),
            snapshot.transform.jsonpath(
                "$..Error.StringToSign",
                value_replacement="<string-to-sign>",
                reference_replacement=False,
            ),
            snapshot.transform.key_value("StringToSignBytes"),
            snapshot.transform.jsonpath(
                "$..Error.CanonicalRequest",
                value_replacement="<canonical-request>",
                reference_replacement=False,
            ),
            snapshot.transform.key_value("CanonicalRequestBytes"),
        ]
    )


def get_checksum_for_algorithm(algorithm: str, data: bytes) -> str:
    # Test our generated checksums
    match algorithm:
        case "CRC32":
            return checksum_crc32(data)
        case "CRC32C":
            return checksum_crc32c(data)
        case "SHA1":
            return hash_sha1(data)
        case "SHA256":
            return hash_sha256(data)
        case "CRC64NVME":
            return checksum_crc64nvme(data)
        case _:
            return ""
