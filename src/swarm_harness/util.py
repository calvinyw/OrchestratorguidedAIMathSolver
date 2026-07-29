from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_id(raw: str, fallback: str = "problem") -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw.strip()).strip("-._")
    return value[:80] or fallback


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


def first_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort extraction for model output that should already be JSON."""

    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        try:
            parsed = json.loads(fence.group(1))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None

