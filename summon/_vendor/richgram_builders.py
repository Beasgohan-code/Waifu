"""Richgram 0.2.0 HTML builders adapted for SUMMON.

The upstream MIT license and attribution are in RICHGRAM_LICENSE. Only the
heading, table and details builders are vendored; Richgram's senders are not.
See https://github.com/Badmunda05/richgram, commit c95cc39.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from wzgram import Client
from wzgram.types import InputRichMessage

RICH_AVAILABLE = InputRichMessage is not None and hasattr(Client, "send_rich_message")


def rich_heading(text: str, level: int = 1) -> str:
    level = max(1, min(6, int(level)))
    return f"<h{level}>{text}</h{level}>"


def rich_table(
    headers: Sequence[str] | None,
    rows: Iterable[Sequence[str]],
    border: int = 1,
    *,
    compact: bool = False,
) -> str:
    # Bot API Rich HTML uses boolean attributes, not HTML's border="1".
    attrs = (" bordered" if border else "") + (" compact" if compact else "")
    parts = [f"<table{attrs}>"]
    if headers:
        cells = "".join(f"<th>{'' if h is None else h}</th>" for h in headers)
        parts.append(f"<tr>{cells}</tr>")
    for row in rows:
        cells = "".join(f"<td>{'' if c is None else c}</td>" for c in row)
        parts.append(f"<tr>{cells}</tr>")
    parts.append("</table>")
    return "".join(parts)


def rich_kv_table(
    pairs: Iterable[tuple[str, str]], border: int = 1, *, compact: bool = False
) -> str:
    rows = [(f"<b>{key}</b>", value) for key, value in pairs if value is not None]
    return rich_table(None, rows, border=border, compact=compact)


def rich_details(summary: str, body: str, open: bool = False) -> str:
    attr = " open" if open else ""
    return f"<details{attr}><summary>{summary}</summary>{body}</details>"
