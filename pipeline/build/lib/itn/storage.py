"""Cloudflare R2 access through its S3-compatible API."""

import hashlib
import json

import boto3

from itn.config import R2Settings


class R2:
    def __init__(self, settings: R2Settings | None = None):
        self.settings = settings or R2Settings.from_env()
        self.client = boto3.client(
            "s3",
            endpoint_url=self.settings.endpoint,
            aws_access_key_id=self.settings.access_key_id,
            aws_secret_access_key=self.settings.secret_access_key,
            region_name="auto",
        )

    def put_text(self, key: str, text: str, content_type: str = "text/plain; charset=utf-8") -> str:
        """Store text and return its sha256, skipping the upload when unchanged."""
        body = text.encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        try:
            head = self.client.head_object(Bucket=self.settings.bucket, Key=key)
            if head.get("Metadata", {}).get("sha256") == digest:
                return digest
        except self.client.exceptions.ClientError:
            pass
        self.client.put_object(
            Bucket=self.settings.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            Metadata={"sha256": digest},
        )
        return digest

    def put_json(self, key: str, data, cache_control: str = "public, max-age=31536000, immutable") -> None:
        self.client.put_object(
            Bucket=self.settings.bucket,
            Key=key,
            Body=json.dumps(data, separators=(",", ":"), default=str).encode("utf-8"),
            ContentType="application/json",
            CacheControl=cache_control,
        )
