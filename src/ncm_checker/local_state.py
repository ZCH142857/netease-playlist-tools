from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile


def state_directory() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "网易云歌单查重"
    state_home = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not state_home.is_absolute():
        state_home = Path.home() / ".local" / "state"
    return state_home / "ncm-checker"


def write_json(path: Path, payload: dict) -> None:
    temporary_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
