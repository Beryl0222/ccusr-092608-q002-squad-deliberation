"""带时区时间解析。"""

from __future__ import annotations

from datetime import datetime


def parse_time(value: str) -> datetime:
    """解析 ISO 8601 时间并强制要求时区。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时间必须携带时区: {value}")
    return parsed
