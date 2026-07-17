import pytest

from retro_backend.consumer.s3_storage import S3ObjectStorage


class Body:
    async def read(self) -> bytes:
        return b"stored"


class Client:
    def __init__(self) -> None:
        self.puts: list[dict[str, object]] = []

    async def head_object(self, **_: object) -> dict[str, object]:
        return {"ContentLength": 6, "Metadata": {"sha256": "abc"}}

    async def put_object(self, **kwargs: object) -> None:
        self.puts.append(kwargs)

    async def get_object(self, **_: object) -> dict[str, object]:
        return {"Body": Body()}

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
async def test_s3_storage_exposes_idempotency_and_compaction_operations() -> None:
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

    assert head and head.size == 6 and head.metadata == {"sha256": "abc"}
    assert await storage.get("raw/a.parquet") == b"stored"
    assert [key async for key in storage.iter_keys("raw/")] == [
        "raw/a.parquet",
        "raw/b.parquet",
    ]
    assert session.value.puts[0]["Bucket"] == "events"
