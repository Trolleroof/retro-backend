"""Production Redpanda-to-S3 consumer entrypoint."""

from __future__ import annotations

import asyncio
import logging
import signal

import aioboto3
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from prometheus_client import start_http_server

from ..config import Settings, get_settings
from .dlq import KafkaDlqPublisher
from .kafka import AiokafkaRecordSource
from .metrics import ConsumerMetrics
from .retry import (
    RetryingBatchWriter,
    RetryingCommitter,
    RetryingDlqPublisher,
    RetryPolicy,
)
from .s3_storage import S3ObjectStorage
from .s3_writer import IdempotentObjectWriter, ParquetBatchWriter
from .service import ConsumerService
from .worker import ConsumerWorker

logger = logging.getLogger(__name__)


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for value in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(value, stop_event.set)


async def run(settings: Settings) -> None:
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    source = AiokafkaRecordSource(
        AIOKafkaConsumer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            group_id=settings.kafka_consumer_group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            isolation_level="read_committed",
        ),
        topic=settings.kafka_topic,
    )
    dlq_producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        acks="all",
        enable_idempotence=True,
        compression_type="zstd",
    )
    storage = S3ObjectStorage(
        session=aioboto3.Session(),
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
    )
    retry_policy = RetryPolicy(
        initial_delay_seconds=settings.consumer_retry_initial_seconds,
        max_delay_seconds=settings.consumer_retry_max_seconds,
        shutdown_attempts=settings.consumer_shutdown_retry_attempts,
    )
    metrics = ConsumerMetrics()
    worker = ConsumerWorker(
        committer=RetryingCommitter(
            source,
            policy=retry_policy,
            stop_event=stop_event,
        ),
        batch_writer=RetryingBatchWriter(
            ParquetBatchWriter(
                IdempotentObjectWriter(storage),
                prefix=settings.s3_raw_prefix,
            ),
            policy=retry_policy,
            stop_event=stop_event,
        ),
        dlq=RetryingDlqPublisher(
            KafkaDlqPublisher(dlq_producer, topic=settings.kafka_dlq_topic),
            policy=retry_policy,
            stop_event=stop_event,
        ),
        max_records=settings.consumer_batch_max_records,
        max_rows=settings.consumer_batch_max_rows,
        max_bytes=settings.consumer_batch_max_bytes,
        metrics=metrics,
    )
    service = ConsumerService(
        source=source,
        worker=worker,
        stop_event=stop_event,
        poll_timeout_ms=settings.consumer_poll_timeout_ms,
        poll_max_records=settings.consumer_poll_max_records,
        flush_interval_seconds=settings.consumer_flush_interval_seconds,
    )

    start_http_server(settings.consumer_metrics_port)
    await dlq_producer.start()
    try:
        await service.run()
    finally:
        await dlq_producer.stop()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info(
        "starting gameplay event consumer",
        extra={"topic": settings.kafka_topic, "group": settings.kafka_consumer_group_id},
    )
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
