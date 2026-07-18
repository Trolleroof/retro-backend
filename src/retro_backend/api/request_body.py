"""Shared bounded request-body reader for authenticated HTTP routes."""

from fastapi import Request


class BodyTooLarge(ValueError):
    """Raised as soon as a request body exceeds its configured bound."""


def _decimal_exceeds(value: str, limit: int) -> bool:
    if not value.isascii() or not value.isdecimal():
        return False
    normalized = value.lstrip("0") or "0"
    limit_text = str(limit)
    if len(normalized) != len(limit_text):
        return len(normalized) > len(limit_text)
    return normalized > limit_text


async def read_limited_body(request: Request, *, max_bytes: int) -> bytes:
    """Read at most ``max_bytes``, rejecting known oversize bodies up front."""
    content_length = request.headers.get("content-length")
    if content_length is not None and _decimal_exceeds(content_length, max_bytes):
        raise BodyTooLarge("request body exceeds maximum size")

    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > max_bytes - len(body):
            raise BodyTooLarge("request body exceeds maximum size")
        body.extend(chunk)
    return bytes(body)
