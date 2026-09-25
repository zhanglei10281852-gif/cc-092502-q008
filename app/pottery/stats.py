from __future__ import annotations

from typing import Any

UNTYPED = "未分类"


def _type_of(value: str) -> str:
    return (value or "").strip() or UNTYPED


def _vessel_type(members: list[dict[str, Any]]) -> str:
    """器物类型取成员的众数，并列时按编码字典序取小者，保证确定性。"""
    counts: dict[str, int] = {}
    for member in members:
        key = _type_of(member["vessel_type"])
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _bucket(table: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
    return table.setdefault(key, {"value": 0, "contributors": []})


def _finalize(table: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for key in sorted(table):
        entry = table[key]
        entry["contributors"].sort(key=lambda c: (c["kind"], c["code"]))
        out[key] = entry
    return out


def _total(table: dict[str, dict[str, Any]]) -> dict[str, Any]:
    value = round(sum(entry["value"] for entry in table.values()), 6)
    contributors = sorted((c for entry in table.values() for c in entry["contributors"]), key=lambda c: (c["kind"], c["code"]))
    return {"value": value, "contributors": contributors}


def compute_metrics(snapshot: dict[str, Any]) -> dict[str, Any]:
    """按研究方案快照计算统计值。

    - mni 最小器物数：已确认器物数 + 未入组陶片数（每片至少代表一件器物）。
    - rim_equivalent 口沿当量：器物按口沿成员弧度百分比求和（封顶 1.0），
      未入组口沿片各自按弧度百分比计入。
    - type_frequency 类型频次：已确认器物按类型的数量与占比。
    每个统计值都带 contributors，指明由哪些片段或已确认器物贡献。
    """
    mni_by_type: dict[str, dict[str, Any]] = {}
    rim_by_type: dict[str, dict[str, Any]] = {}
    freq_by_type: dict[str, dict[str, Any]] = {}

    for vessel in snapshot["vessels"]:
        members = vessel["members"]
        if not members:
            continue
        vessel_type = _vessel_type(members)
        bucket = _bucket(mni_by_type, vessel_type)
        bucket["value"] += 1
        bucket["contributors"].append({"kind": "vessel", "code": vessel["code"], "contribution": 1})
        bucket = _bucket(freq_by_type, vessel_type)
        bucket["value"] += 1
        bucket["contributors"].append({"kind": "vessel", "code": vessel["code"], "contribution": 1})
        rim_percent = sum(float(member["rim_arc_percent"]) for member in members if member["part"] == "rim")
        if rim_percent > 0:
            contribution = round(min(1.0, rim_percent / 100.0), 6)
            bucket = _bucket(rim_by_type, vessel_type)
            bucket["value"] = round(bucket["value"] + contribution, 6)
            bucket["contributors"].append({"kind": "vessel", "code": vessel["code"], "contribution": contribution})

    for sherd in snapshot["unassigned"]:
        sherd_type = _type_of(sherd["vessel_type"])
        bucket = _bucket(mni_by_type, sherd_type)
        bucket["value"] += 1
        bucket["contributors"].append({"kind": "sherd", "code": sherd["code"], "contribution": 1})
        if sherd["part"] == "rim" and float(sherd["rim_arc_percent"]) > 0:
            contribution = round(float(sherd["rim_arc_percent"]) / 100.0, 6)
            bucket = _bucket(rim_by_type, sherd_type)
            bucket["value"] = round(bucket["value"] + contribution, 6)
            bucket["contributors"].append({"kind": "sherd", "code": sherd["code"], "contribution": contribution})

    vessel_total = sum(entry["value"] for entry in freq_by_type.values())
    for entry in freq_by_type.values():
        entry["share"] = round(entry["value"] / vessel_total, 6) if vessel_total else 0

    return {
        "mni": {"total": _total(mni_by_type), "by_type": _finalize(mni_by_type)},
        "rim_equivalent": {"total": _total(rim_by_type), "by_type": _finalize(rim_by_type)},
        "type_frequency": {"total": _total(freq_by_type), "by_type": _finalize(freq_by_type)},
    }


def _flatten_metrics(node: Any, prefix: str, out: dict[str, dict[str, Any]]) -> None:
    if isinstance(node, dict) and "value" in node and "contributors" in node:
        out[prefix] = node
        return
    if isinstance(node, dict):
        for key in sorted(node):
            if key == "contributors":
                continue
            _flatten_metrics(node[key], f"{prefix}.{key}" if prefix else key, out)


def diff_metrics(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """对比两个报告版本的统计结果，给出数值变化与贡献者增删。"""
    old_flat: dict[str, dict[str, Any]] = {}
    new_flat: dict[str, dict[str, Any]] = {}
    _flatten_metrics(old, "", old_flat)
    _flatten_metrics(new, "", new_flat)
    changes: dict[str, Any] = {}
    unchanged = 0
    for path in sorted(set(old_flat) | set(new_flat)):
        old_entry, new_entry = old_flat.get(path), new_flat.get(path)
        old_value = old_entry["value"] if old_entry else None
        new_value = new_entry["value"] if new_entry else None
        old_codes = {f"{c['kind']}:{c['code']}" for c in old_entry["contributors"]} if old_entry else set()
        new_codes = {f"{c['kind']}:{c['code']}" for c in new_entry["contributors"]} if new_entry else set()
        if old_value == new_value and old_codes == new_codes:
            unchanged += 1
            continue
        delta = None
        if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)):
            delta = round(new_value - old_value, 6)
        changes[path] = {
            "old": old_value,
            "new": new_value,
            "delta": delta,
            "added_contributors": sorted(new_codes - old_codes),
            "removed_contributors": sorted(old_codes - new_codes),
        }
    return {"changed": len(changes), "unchanged": unchanged, "metrics": changes}
