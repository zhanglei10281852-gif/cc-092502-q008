"""游标分页工具：键集（keyset）分页，排序键始终包含唯一 id 作为最终次序。"""

from __future__ import annotations

import base64
import json
from typing import Any

from app.security import stable_json
from app.service import ServiceError


def encode_cursor(values: list[Any]) -> str:
    return base64.urlsafe_b64encode(stable_json(list(values)).encode()).decode()


def decode_cursor(token: str, length: int) -> list[Any] | None:
    if not token:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(token.encode()).decode())
    except Exception as exc:
        raise ServiceError("invalid_cursor", "分页游标无效", 400) from exc
    if not isinstance(data, list) or len(data) != length:
        raise ServiceError("invalid_cursor", "分页游标无效", 400)
    return data
