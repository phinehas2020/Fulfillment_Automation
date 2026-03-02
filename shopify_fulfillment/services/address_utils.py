import re
from typing import List, Tuple


_WHITESPACE_RE = re.compile(r"\s+")


def _split_address_lines(value) -> List[str]:
    """Split multiline address input into trimmed non-empty lines."""
    if not value:
        return []

    normalized = str(value).replace("\r\n", "\n").replace("\r", "\n")
    lines: List[str] = []
    for raw_line in normalized.split("\n"):
        line = _WHITESPACE_RE.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return lines


def normalize_address_lines(line1, line2) -> Tuple[str, str]:
    """Return deterministic (line1, line2) address values.

    Handles embedded newlines, extra whitespace, and duplicate content so we
    always pass clean, separated street lines to downstream carriers.
    """
    line1_parts = _split_address_lines(line1)
    line2_parts = _split_address_lines(line2)

    primary = line1_parts[0] if line1_parts else ""
    secondary_candidates = line1_parts[1:] + line2_parts

    primary_key = primary.casefold() if primary else ""
    seen = set()
    secondary_parts: List[str] = []
    for part in secondary_candidates:
        key = part.casefold()
        if not part:
            continue
        if primary_key and key == primary_key:
            continue
        if key in seen:
            continue
        seen.add(key)
        secondary_parts.append(part)

    if not primary and secondary_parts:
        primary = secondary_parts.pop(0)

    return primary, ", ".join(secondary_parts)
