"""Fail closed if multiple packages overwrite the same Pyrogram namespace."""

from __future__ import annotations

import inspect
from importlib import metadata


def validate_runtime() -> None:
    try:
        installed_wzgram = metadata.version("wzgram")
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            "Wzgram is required; reinstall the project in a clean environment"
        ) from exc
    for name in ("kurigram", "pyrogram", "pyrofork"):
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        raise RuntimeError(
            f"{name.title()} conflicts with Wzgram's pyrogram namespace. "
            "Create a clean environment and install only this project's dependencies."
        )
    import wzgram

    if wzgram.__version__ != installed_wzgram:
        raise RuntimeError("The imported pyrogram namespace does not match the installed Wzgram")
    if "no_updates" not in inspect.signature(wzgram.Client).parameters:
        raise RuntimeError("The installed Wzgram does not support outbound-only operation")
    sender = getattr(wzgram.Client, "send_rich_message", None)
    if sender is None or "rich_text" not in inspect.signature(sender).parameters:
        raise RuntimeError("The installed Wzgram rich-message API is incompatible")
