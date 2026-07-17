"""Small aioboto3 adapter for event object storage."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from botocore.config import Config
from botocore.exceptions import ClientError

from .s3_writer import ObjectInfo

MAX_STREAM_LINE_BYTES = 16 * 1024


class S3ObjectStorage:
    def __init__(
        self,
        *,
        session: Any,
        bucket: str,
        endpoint_url: str | None,
        region: str,
        access_key_id: str | None,
        secret_access_key: str | None,
    ) -> None:
        self._session = session
        self._bucket = bucket
        self._client_options = {
            "endpoint_url": endpoint_url,
            "region_name": region,
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
            "config": Config(s3={"addressing_style": "path"}),
        }

    def _client(self) -> Any:
        return self._session.client("s3", **self._client_options)

    async def head(self, key: str) -> ObjectInfo | None:
        try:
            async with self._client() as client:
                response = await client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return ObjectInfo(
            size=int(response["ContentLength"]),
            metadata={str(key): str(value) for key, value in response.get("Metadata", {}).items()},
        )

    async def put(self, key: str, body: bytes, metadata: dict[str, str]) -> None:
        async with self._client() as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                Metadata=metadata,
                ContentType=(
                    "application/json"
                    if key.endswith(".json")
                    else "application/vnd.apache.parquet"
                ),
            )

    async def put_file(self, key: str, path: Path, metadata: dict[str, str]) -> None:
        async with self._client() as client:
            await client.upload_file(
                str(path),
                self._bucket,
                key,
                {
                    "Metadata": metadata,
                    "ContentType": "application/x-ndjson",
                },
            )

    async def get(self, key: str) -> bytes:
        async with self._client() as client:
            response = await client.get_object(Bucket=self._bucket, Key=key)
            return bytes(await response["Body"].read())

    async def get_with_version(self, key: str) -> tuple[bytes, str] | None:
        try:
            async with self._client() as client:
                response = await client.get_object(Bucket=self._bucket, Key=key)
                body = bytes(await response["Body"].read())
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        version = response.get("ETag")
        if not isinstance(version, str) or not version:
            raise OSError(f"object {key} did not return an ETag")
        return body, version

    async def iter_lines(self, key: str) -> AsyncIterator[bytes]:
        async with self._client() as client:
            response = await client.get_object(Bucket=self._bucket, Key=key)
            buffer = bytearray()
            async for chunk in response["Body"].iter_chunks(chunk_size=64 * 1024):
                buffer.extend(chunk)
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    if newline > MAX_STREAM_LINE_BYTES:
                        raise OSError(f"object {key} contains an oversized line")
                    yield bytes(buffer[: newline + 1])
                    del buffer[: newline + 1]
                if len(buffer) > MAX_STREAM_LINE_BYTES:
                    raise OSError(f"object {key} contains an oversized line")
            if buffer:
                raise OSError(f"object {key} contains an unterminated final line")

    async def put_if_version(
        self,
        key: str,
        body: bytes,
        metadata: dict[str, str],
        expected_version: str | None,
    ) -> bool:
        condition = (
            {"IfNoneMatch": "*"}
            if expected_version is None
            else {"IfMatch": expected_version}
        )
        try:
            async with self._client() as client:
                await client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=body,
                    Metadata=metadata,
                    ContentType="application/json",
                    **condition,
                )
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            status = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            if code in {"PreconditionFailed", "ConditionalRequestConflict"} or status in {
                409,
                412,
            }:
                return False
            raise
        return True

    async def iter_keys(self, prefix: str) -> AsyncIterator[str]:
        async with self._client() as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for value in page.get("Contents", []):
                    if "Key" in value:
                        yield str(value["Key"])
