from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str, fallback: str = "item") -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_")
    return text or fallback


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=True, sort_keys=True)


def try_load_json(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        raise ValueError("Empty response.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Some commands print logs before/after JSON payload.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start != -1 and end != -1 and end > start:
            chunk = raw[start : end + 1]
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                continue
    raise ValueError("Could not parse JSON output.")
