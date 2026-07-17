"""Optional Redis coordination used for notifications and readiness."""

from redis.asyncio import Redis

from .config import Settings


def create_redis(settings: Settings) -> Redis | None:
    if not settings.redis_url:
        return None
    return Redis.from_url(settings.redis_url, decode_responses=True)
