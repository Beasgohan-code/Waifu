"""Explicit opt-in ImgBB photo hosting; never fetch operator-supplied URLs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from summon.services.artwork import MAX_ARTWORK_BYTES, Artwork, validate_external_url

_IMGBB_UPLOAD = "https://api.imgbb.com/1/upload"
_MAX_RESPONSE_BYTES = 32 * 1024


class ImageHostError(Exception):
    """The external upload did not return a confirmed, safe public image URL."""


async def upload_imgbb(
    artwork: Artwork,
    key: SecretStr,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Upload a normalized photo once; failures may leave a public orphan.

    The API key stays in the POST body, never the URL. Redirects and environment
    proxy settings are disabled so neither user-controlled URLs nor forwarded
    credentials can change the fixed destination. Only the public direct image
    URL is retained; the response's secret deletion URL is discarded.
    """
    if (
        not key.get_secret_value().strip()
        or not 0 < len(artwork.content) <= MAX_ARTWORK_BYTES
        or not artwork.content.startswith(b"\xff\xd8\xff")
        or hashlib.sha256(artwork.content).hexdigest() != artwork.digest
    ):
        raise ImageHostError("Image host input is invalid")
    try:
        async with asyncio.timeout(30):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20, connect=5),
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client:
                async with client.stream(
                    "POST",
                    _IMGBB_UPLOAD,
                    data={"key": key.get_secret_value()},
                    files={"image": ("summon.jpg", artwork.content, "image/jpeg")},
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code != 200:
                        raise ImageHostError("Image host did not confirm the upload")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ImageHostError("Image host returned an encoded response")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        if len(chunk) > _MAX_RESPONSE_BYTES - len(payload):
                            raise ImageHostError("Image host response is too large")
                        payload.extend(chunk)
        decoded = json.loads(payload)
        if not isinstance(decoded, dict) or decoded.get("success") is not True:
            raise ImageHostError("Image host did not confirm the upload")
        data = decoded.get("data")
        url = data.get("url") if isinstance(data, dict) else None
        if not isinstance(url, str):
            raise ImageHostError("Image host did not return a public URL")
        validate_external_url(url)
        parsed = urlsplit(url)
        if (
            parsed.hostname != "i.ibb.co"
            or parsed.port not in (None, 443)
            or parsed.query
            or not parsed.path.startswith("/")
            or parsed.path == "/"
        ):
            raise ImageHostError("Image host returned an unexpected URL")
        return url
    except ImageHostError:
        raise
    except (
        httpx.HTTPError,
        httpx.StreamError,
        TimeoutError,
        ValueError,
        TypeError,
        UnicodeError,
        RecursionError,
    ):
        # Never expose a provider response or request (which contains the key).
        raise ImageHostError("Image host result is unconfirmed") from None
