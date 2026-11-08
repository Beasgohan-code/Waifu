"""Bounded, public catalog artwork validation without server-side URL fetching."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import warnings
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError

from summon.config import _contains_unsafe_url_characters, _valid_url_host

# Smaller than Telegram's limits and Vercel's typical function response limit.
MAX_ARTWORK_BYTES = 3 * 1024 * 1024
MAX_ARTWORK_DIMENSION = 8192
MAX_ARTWORK_PIXELS = 16_000_000


class InvalidArtwork(ValueError):
    """Artwork was rejected without disclosing the original URL or contents."""


@dataclass(frozen=True, slots=True)
class Artwork:
    digest: str
    content: bytes

    @property
    def path(self) -> str:
        return f"/api/artwork/{self.digest}"


def validate_external_url(value: str) -> str:
    """Accept a public-looking HTTPS URL; neither the bot nor API fetches it.

    DNS names are not resolved here and may still redirect or change. Operators
    must choose trusted public hosts. Clients and Telegram fetch these URLs.
    """
    if not value or _contains_unsafe_url_characters(value):
        raise InvalidArtwork("Use a public HTTPS artwork URL without credentials")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        _ = parsed.port  # Reject malformed or out-of-range ports.
        if (
            parsed.scheme != "https"
            or not _valid_url_host(host)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or host is None
        ):
            raise InvalidArtwork("Use a public HTTPS artwork URL without credentials")
        hostname = host.rstrip(".").casefold()
        if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
            raise InvalidArtwork("Use a public HTTPS artwork URL without credentials")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            pass  # DNS is not queried; a name's destination cannot be guaranteed.
        else:
            if not address.is_global:
                raise InvalidArtwork("Use a public HTTPS artwork URL without credentials")
    except ValueError as exc:
        raise InvalidArtwork("Use a public HTTPS artwork URL without credentials") from exc
    return value


def _normalize_photo(content: bytes) -> Artwork:
    if not content or len(content) > MAX_ARTWORK_BYTES:
        raise InvalidArtwork("Photo must be at most 3 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                if image.format != "JPEG":
                    raise InvalidArtwork("Send an ordinary Telegram photo")
                width, height = image.size
                if (
                    width < 1
                    or height < 1
                    or width > MAX_ARTWORK_DIMENSION
                    or height > MAX_ARTWORK_DIMENSION
                    or width * height > MAX_ARTWORK_PIXELS
                ):
                    raise InvalidArtwork("Photo dimensions are too large")
                # Decode only after checking dimensions. Transcode and explicitly
                # omit EXIF/ICC/text so location and other metadata are not served.
                image.load()
                clean = ImageOps.exif_transpose(image).convert("RGB")
                output = BytesIO()
                clean.save(output, format="JPEG", quality=90, optimize=True, exif=b"")
                clean.close()
                result = output.getvalue()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise InvalidArtwork("Could not read this photo safely") from exc
    if not result or len(result) > MAX_ARTWORK_BYTES:
        raise InvalidArtwork("Processed photo must be at most 3 MiB")
    return Artwork(hashlib.sha256(result).hexdigest(), result)


async def normalize_photo(content: bytes) -> Artwork:
    # Decoding/compression may take noticeable CPU time; never stall the update
    # loop and its ingress-lease renewal. Concurrency is limited by the caller.
    return await asyncio.to_thread(_normalize_photo, content)
