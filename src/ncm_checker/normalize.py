from __future__ import annotations

import re
import unicodedata

_SPACE_RE = re.compile(r"\s+")


def title_prefix(value: str) -> str:
    """Return the title before balanced trailing parenthetical suffixes."""
    original = unicodedata.normalize("NFKC", value or "").strip()
    title = original
    while title.endswith(")"):
        depth = 0
        for index in range(len(title) - 1, -1, -1):
            if title[index] == ")":
                depth += 1
            elif title[index] == "(":
                depth -= 1
                if depth == 0:
                    break
        else:
            break
        prefix = title[:index].rstrip()
        if not prefix:
            break
        title = prefix
    return title


def normalize_title(value: str) -> str:
    return _SPACE_RE.sub("", title_prefix(value)).casefold()

