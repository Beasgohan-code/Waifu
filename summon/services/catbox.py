"""Bounded, opt-in public Catbox hosting for catalog media."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from summon.services.artwork import validate_external_url
from summon.services.imgbb import ImageHostError

_CATBOX_UPLOAD = "https://catbox.moe/user/api.php"
MAX_MEDIA_BYTES = 16 * 1024 * 1024
_MAX_RESPONSE_BYTES = 4096
_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
}


def valid_media_signature(content: bytes, extension: str) -> bool:
    if extension == "jpg":
        return content.startswith(b"\xff\xd8\xff")
    if extension == "gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if extension in {"mp4", "mov"}:
        return len(content) >= 12 and content[4:8] == b"ftyp"
    if extension == "webm":
        return content.startswith(b"\x1a\x45\xdf\xa3")
    return False


async def upload_catbox(
    content: bytes,
    extension: str,
    userhash: SecretStr,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Upload once to a fixed endpoint; never expose a provider response or secret.

    Files are public. A failed or timed-out request may still create an orphan.
    The caller must persist an idempotency claim before starting this request.
    """
    if (
        not isinstance(content, bytes)
        or not 0 < len(content) <= MAX_MEDIA_BYTES
        or extension not in _CONTENT_TYPES
        or not valid_media_signature(content, extension)
    ):
        raise ImageHostError("Media host input is invalid")
    fields = {"reqtype": "fileupload"}
    if userhash.get_secret_value().strip():
        fields["userhash"] = userhash.get_secret_value().strip()
    try:
        async with asyncio.timeout(90):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(75, connect=5),
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client:
                async with client.stream(
                    "POST",
                    _CATBOX_UPLOAD,
                    data=fields,
                    files={
                        "fileToUpload": (f"summon.{extension}", content, _CONTENT_TYPES[extension])
                    },
                    headers={"Accept": "text/plain", "Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code != 200:
                        raise ImageHostError("Media host did not confirm the upload")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ImageHostError("Media host returned an encoded response")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=2048):
                        if len(chunk) > _MAX_RESPONSE_BYTES - len(payload):
                            raise ImageHostError("Media host response is too large")
                        payload.extend(chunk)
        url = payload.decode("ascii", "strict").strip()
        validate_external_url(url)
        parsed = urlsplit(url)
        if (
            parsed.hostname != "files.catbox.moe"
            or parsed.port not in (None, 443)
            or parsed.query
            or not re.fullmatch(r"/[a-zA-Z0-9_-]+\.[a-zA-Z0-9]{1,8}", parsed.path)
            or not parsed.path.lower().endswith(f".{extension}")
        ):
            raise ImageHostError("Media host returned an unexpected URL")
        return url
    except ImageHostError:
        raise
    except (httpx.HTTPError, httpx.StreamError, TimeoutError, ValueError, TypeError, UnicodeError):
        raise ImageHostError("Media host result is unconfirmed") from None
