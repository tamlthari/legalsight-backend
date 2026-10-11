import json
from functools import cached_property
from io import BytesIO
from typing import Any, BinaryIO

import boto3
from botocore.config import Config

from app.config import settings


def _session() -> boto3.Session:
    credentials: dict[str, str] = {}
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        credentials = {
            "aws_access_key_id": settings.aws_access_key_id,
            "aws_secret_access_key": settings.aws_secret_access_key,
        }
        if settings.aws_session_token:
            credentials["aws_session_token"] = settings.aws_session_token
    return boto3.Session(region_name=settings.aws_region, **credentials)


class AWS:
    def __init__(self) -> None:
        self.session = _session()
        self.config = Config(retries={"max_attempts": 8, "mode": "adaptive"})

    @cached_property
    def s3(self):
        return self.session.client("s3", config=self.config)

    @cached_property
    def textract(self):
        return self.session.client("textract", config=self.config)

    @cached_property
    def sqs(self):
        return self.session.client("sqs", config=self.config)

    def put_file(self, fileobj: BinaryIO, key: str, content_type: str) -> None:
        extra: dict[str, str] = {"ContentType": content_type}
        if settings.s3_kms_key_id:
            extra.update(ServerSideEncryption="aws:kms", SSEKMSKeyId=settings.s3_kms_key_id)
        else:
            extra["ServerSideEncryption"] = "AES256"
        fileobj.seek(0)
        self.s3.upload_fileobj(fileobj, settings.s3_bucket, key, ExtraArgs=extra)

    def put_bytes(self, data: bytes, key: str, content_type: str) -> None:
        self.put_file(BytesIO(data), key, content_type)

    def get_bytes(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read()

    def delete_prefix(self, prefix: str) -> None:
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
            objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if objects:
                self.s3.delete_objects(Bucket=settings.s3_bucket, Delete={"Objects": objects, "Quiet": True})

    def bucket_region(self) -> str:
        region = self.s3.get_bucket_location(Bucket=settings.s3_bucket).get("LocationConstraint")
        return "us-east-1" if region is None else "eu-west-1" if region == "EU" else region

    def assert_textract_region(self) -> None:
        actual = self.bucket_region()
        if actual != settings.aws_region:
            raise RuntimeError(
                f"S3 bucket ở {actual}, nhưng AWS_REGION={settings.aws_region}. "
                "Textract và S3 phải cùng Region."
            )

    def start_analysis(self, key: str, token: str, job_tag: str) -> str:
        self.assert_textract_region()
        request: dict[str, Any] = {
            "DocumentLocation": {"S3Object": {"Bucket": settings.s3_bucket, "Name": key}},
            "FeatureTypes": ["LAYOUT", "TABLES", "FORMS"],
            "ClientRequestToken": token,
            "JobTag": job_tag,
        }
        if settings.textract_completion_mode == "sqs":
            request["NotificationChannel"] = {
                "SNSTopicArn": settings.textract_sns_topic_arn,
                "RoleArn": settings.textract_sns_role_arn,
            }
        return self.textract.start_document_analysis(**request)["JobId"]

    def analysis_status(self, job_id: str) -> tuple[str, str | None]:
        response = self.textract.get_document_analysis(JobId=job_id, MaxResults=1)
        return response["JobStatus"], response.get("StatusMessage")

    def analysis_result(self, job_id: str) -> dict[str, Any]:
        pages: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"JobId": job_id, "MaxResults": 1000}
            if token:
                kwargs["NextToken"] = token
            response = self.textract.get_document_analysis(**kwargs)
            if response["JobStatus"] != "SUCCEEDED":
                raise RuntimeError(response.get("StatusMessage") or f"Textract status: {response['JobStatus']}")
            pages.append(response)
            token = response.get("NextToken")
            if not token:
                break
        return {
            "DocumentMetadata": pages[0].get("DocumentMetadata", {}),
            "AnalyzeDocumentModelVersion": pages[0].get("AnalyzeDocumentModelVersion"),
            "Blocks": [block for page in pages for block in page.get("Blocks", [])],
        }

    @staticmethod
    def notification(body: str) -> dict[str, Any]:
        payload = json.loads(body)
        if isinstance(payload.get("Message"), str):
            payload = json.loads(payload["Message"])
        return payload


aws = AWS()

