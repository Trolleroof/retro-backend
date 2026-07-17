from pathlib import Path

import pytest

from retro_backend.consumer.s3_storage import S3ObjectStorage


class Body:
    async def read(self) -> bytes:
        return b"stored"

    async def iter_chunks(self, chunk_size: int = 64 * 1024):  # type: ignore[no-untyped-def]
        _ = chunk_size
        yield b'"raw/a'
        yield b'.parquet"\n"raw/b.parquet"\n'


class Client:
    def __init__(self) -> None:
        self.puts: list[dict[str, object]] = []
        self.uploads: list[tuple[str, str, str, dict[str, object]]] = []

    async def head_object(self, **_: object) -> dict[str, object]:
        return {"ContentLength": 6, "Metadata": {"sha256": "abc"}}

    async def put_object(self, **kwargs: object) -> None:
        self.puts.append(kwargs)

    async def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
        extra_args: dict[str, object],
    ) -> None:
        self.uploads.append((filename, bucket, key, extra_args))

    async def get_object(self, **_: object) -> dict[str, object]:
        return {"Body": Body(), "ETag": '"etag-1"'}

    def get_paginator(self, _: str) -> "Paginator":
        return Paginator()


class Paginator:
    async def paginate(self, **_: object):  # type: ignore[no-untyped-def]
        yield {"Contents": [{"Key": "raw/a.parquet"}, {"Key": "raw/b.parquet"}]}


class Context:
    def __init__(self, client: Client) -> None:
        self.client = client

    async def __aenter__(self) -> Client:
        return self.client

    async def __aexit__(self, *_: object) -> None:
        return None


class Session:
    def __init__(self) -> None:
        self.value = Client()

    def client(self, *_args: object, **_kwargs: object) -> Context:
        return Context(self.value)


@pytest.mark.asyncio
async def test_s3_storage_exposes_idempotency_and_compaction_operations(
    tmp_path: Path,
) -> None:
    session = Session()
    storage = S3ObjectStorage(
        session=session,  # type: ignore[arg-type]
        bucket="events",
        endpoint_url="http://minio:9000",
        region="us-east-1",
        access_key_id="key",
        secret_access_key="secret",
    )

    head = await storage.head("raw/a.parquet")
    await storage.put("raw/c.parquet", b"body", {"sha256": "digest"})
    source_path = tmp_path / "source-keys.ndjson"
    source_path.write_text('"raw/a.parquet"\n')
    await storage.put_file("source-keys.ndjson", source_path, {"sha256": "source"})

    assert head and head.size == 6 and head.metadata == {"sha256": "abc"}
    assert await storage.get("raw/a.parquet") == b"stored"
    assert await storage.get_with_version("latest.json") == (b"stored", '"etag-1"')
    assert await storage.put_if_version(
        "latest.json", b"first", {"sha256": "one"}, None
    )
    assert await storage.put_if_version(
        "latest.json", b"second", {"sha256": "two"}, '"etag-1"'
    )
    assert [key async for key in storage.iter_keys("raw/")] == [
        "raw/a.parquet",
        "raw/b.parquet",
    ]
    assert [line async for line in storage.iter_lines("source-keys.ndjson")] == [
        b'"raw/a.parquet"',
        b'"raw/b.parquet"',
    ]
    assert session.value.puts[0]["Bucket"] == "events"
    assert session.value.puts[1]["IfNoneMatch"] == "*"
    assert session.value.puts[2]["IfMatch"] == '"etag-1"'
    assert session.value.uploads == [
        (
            str(source_path),
            "events",
            "source-keys.ndjson",
            {"Metadata": {"sha256": "source"}, "ContentType": "application/x-ndjson"},
        )
    ]
