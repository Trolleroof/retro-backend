"""Small, validated Queue messages for deferred gameplay processing."""

import json
from typing import cast

from ..bindings import QueueBinding, is_binding_null, value_field
from ..contracts import GameplayPointer

_MAX_POINTER_BYTES = 120 * 1024


class GameplayQueuePublisher:
    def __init__(self, environment: object) -> None:
        queue = value_field(environment, "GAMEPLAY_QUEUE")
        if is_binding_null(queue):
            raise RuntimeError("gameplay Queue binding unavailable")
        self.queue = cast(QueueBinding, queue)

    async def publish(self, pointer: GameplayPointer) -> None:
        message = pointer.model_dump(mode="json")
        encoded = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_POINTER_BYTES:
            raise RuntimeError("gameplay pointer exceeds safe Queue size")
        await self.queue.send(message)
