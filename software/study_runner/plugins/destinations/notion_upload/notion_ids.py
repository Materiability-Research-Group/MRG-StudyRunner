"""Read a Notion page or database ID from what an operator pastes.

In Notion, "••• > Copy link" gives a whole link such as
``https://www.notion.so/team/Study-results-1a2b...?source=copy_link``. The ID
is the 32 hexadecimal characters at the end of the last path segment, joined
to the page title with "-". Removing every dash instead would glue the title
to the ID, and a title made of hex letters ("Cafe") would then be read as
part of it. A link opened from a database view names the page in ``?p=``.

Standard library only: study settings are validated with this before any
Notion client exists.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_COMPACT = re.compile(r"[0-9a-fA-F]{32}")
_DASHED = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_AT_SEGMENT_END = re.compile(r"(?:^|-)([0-9a-fA-F]{32})$")


def notion_id_from_input(value: object) -> str:
    """The 32-character ID in ``value``, lower case without dashes; "" when there is none."""
    text = str(value or "").strip()
    if not text:
        return ""
    if _is_id(text):
        return _compact(text)
    if "/" not in text and "?" not in text:
        return _id_at_segment_end(text)
    parts = urlsplit(text if "://" in text else f"https://{text}")
    for candidate in parse_qs(parts.query).get("p", []):
        if _is_id(candidate):
            return _compact(candidate)
    return _id_at_segment_end(parts.path.rstrip("/").rsplit("/", 1)[-1])


def _id_at_segment_end(segment: str) -> str:
    dashed = _DASHED.search(segment)
    if dashed and segment.endswith(dashed.group(0)):
        return _compact(dashed.group(0))
    compact = _AT_SEGMENT_END.search(segment)
    return compact.group(1).lower() if compact else ""


def _is_id(text: str) -> bool:
    return bool(_COMPACT.fullmatch(text) or _DASHED.fullmatch(text))


def _compact(text: str) -> str:
    return text.replace("-", "").lower()
