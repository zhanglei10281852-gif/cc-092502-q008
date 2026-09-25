from __future__ import annotations

import base64
import json
import sqlite3
from typing import Any

from app.service import ServiceError


def encode_cursor(values: list[Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(values, ensure_ascii=False).encode()).decode()


def decode_cursor(raw: str) -> list[Any]:
    try:
        value = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
    except Exception as exc:
        raise ServiceError("invalid_cursor", "分页游标无效", 400) from exc
    if not isinstance(value, list) or len(value) != 2:
        raise ServiceError("invalid_cursor", "分页游标无效", 400)
    return value


def paginate(
    db: sqlite3.Connection,
    *,
    select: str,
    source: str,
    where: str,
    params: list[Any],
    sort: str,
    sorts: dict[str, tuple[str, str]],
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    """游标分页 + 稳定排序。

    sorts 把对外排序字段映射为 (SQL 表达式, 行内键名)；排序键允许重复，
    因此始终追加 id 作为决胜键，保证相同数据的分页顺序稳定。
    sort 支持 "-字段" 表示倒序。
    """
    desc = sort.startswith("-")
    key = sort[1:] if desc else sort
    if key not in sorts:
        raise ServiceError("invalid_sort", f"不支持的排序字段: {key}", 400)
    column, row_key = sorts[key]
    clause, values = where, list(params)
    if cursor:
        last_value, last_id = decode_cursor(cursor)
        operator = "<" if desc else ">"
        clause = f"({clause}) AND ({column} {operator} ? OR ({column} = ? AND id > ?))"
        values += [last_value, last_value, last_id]
    direction = "DESC" if desc else "ASC"
    sql = f"SELECT {select} FROM {source} WHERE {clause} ORDER BY {column} {direction}, id ASC LIMIT ?"
    rows = db.execute(sql, values + [limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor([last[row_key], last["id"]])
    return {
        "data": [dict(row) for row in rows],
        "page": {"limit": limit, "sort": sort, "has_more": has_more, "next_cursor": next_cursor},
    }
