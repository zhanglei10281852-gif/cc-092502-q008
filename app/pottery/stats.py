"""按研究方案统计最小器物数、口沿当量与类型频次，并支持结果版本间差异比较。

统计为纯函数：输入（规则配置、陶片、已确认器物、方案）确定时输出确定。
每个统计值都带有贡献者列表，可回溯到具体陶片或已确认器物。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.pottery.rules import build_groups


def compute_result(cfg: dict[str, Any], sherds: list[dict[str, Any]], vessels: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    contexts = plan.get("contexts")
    parts = plan.get("parts")
    context_set = set(contexts) if contexts else None
    part_set = set(parts) if parts else None

    def in_scope(sherd: dict[str, Any]) -> bool:
        return (context_set is None or sherd["context_code"] in context_set) and (part_set is None or sherd["part"] in part_set)

    by_id = {s["id"]: s for s in sherds}
    assigned: set[int] = set()
    for vessel in vessels:
        assigned.update(vessel["member_ids"])

    vessel_info = []
    for vessel in vessels:
        member_types = [by_id[m]["typology_code"] for m in vessel["member_ids"] if m in by_id]
        if member_types:
            counts = Counter(member_types)
            vessel_type = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        else:
            vessel_type = ""
        scoped = any(m in by_id and in_scope(by_id[m]) for m in vessel["member_ids"])
        vessel_info.append({
            "id": vessel["id"],
            "vessel_code": vessel["vessel_code"],
            "member_ids": list(vessel["member_ids"]),
            "typology_code": vessel_type,
            "in_scope": scoped,
        })

    types = sorted({s["typology_code"] for s in sherds if in_scope(s)} | {v["typology_code"] for v in vessel_info if v["in_scope"]})
    mni_by_type: dict[str, Any] = {}
    eve_by_type: dict[str, Any] = {}
    freq_by_type: dict[str, Any] = {}
    for type_code in types:
        type_sherds = [s for s in sherds if s["typology_code"] == type_code and in_scope(s)]
        type_vessels = sorted((v for v in vessel_info if v["typology_code"] == type_code and v["in_scope"]), key=lambda v: v["id"])
        leftover = [s for s in type_sherds if s["id"] not in assigned]
        groups = build_groups(cfg, leftover)
        grouped_ids = {sid for group in groups for sid in group["member_ids"]}
        singles = sorted((s for s in leftover if s["id"] not in grouped_ids), key=lambda s: s["id"])
        mni_by_type[type_code] = {
            "value": len(type_vessels) + len(groups) + len(singles),
            "vessels": [{"id": v["id"], "code": v["vessel_code"]} for v in type_vessels],
            "sherd_groups": [[{"id": sid, "code": by_id[sid]["code_norm"]} for sid in group["member_ids"]] for group in groups],
            "single_sherds": [{"id": s["id"], "code": s["code_norm"]} for s in singles],
        }

        contributors = []
        eve_total = 0.0
        for vessel in type_vessels:
            percent = sum(
                (by_id[m]["rim_percent"] or 0.0)
                for m in vessel["member_ids"]
                if m in by_id and by_id[m]["part"] == "rim" and in_scope(by_id[m])
            )
            value = min(1.0, round(percent / 100.0, 6))
            if value > 0:
                contributors.append({"kind": "vessel", "id": vessel["id"], "code": vessel["vessel_code"], "value": round(value, 6)})
                eve_total += value
        for sherd in sorted(leftover, key=lambda s: s["id"]):
            if sherd["part"] == "rim" and sherd["rim_percent"]:
                value = round(sherd["rim_percent"] / 100.0, 6)
                contributors.append({"kind": "sherd", "id": sherd["id"], "code": sherd["code_norm"], "rim_percent": sherd["rim_percent"], "value": value})
                eve_total += value
        eve_by_type[type_code] = {"value": round(eve_total, 6), "contributors": contributors}

        freq_by_type[type_code] = {
            "sherds": len(type_sherds),
            "vessels": len(type_vessels),
            "sherd_ids": sorted(s["id"] for s in type_sherds),
            "vessel_ids": sorted(v["id"] for v in type_vessels),
        }

    return {
        "mni": {"total": sum(entry["value"] for entry in mni_by_type.values()), "by_type": mni_by_type},
        "rim_eve": {"total": round(sum(entry["value"] for entry in eve_by_type.values()), 6), "by_type": eve_by_type},
        "type_frequency": {"by_type": freq_by_type},
    }


def _mni_ids(entry: dict[str, Any]) -> tuple[set[int], set[int]]:
    vessel_ids = {item["id"] for item in entry["vessels"]}
    sherd_ids = {item["id"] for group in entry["sherd_groups"] for item in group} | {item["id"] for item in entry["single_sherds"]}
    return vessel_ids, sherd_ids


def _eve_ids(entry: dict[str, Any]) -> set[tuple[str, int]]:
    return {(item["kind"], item["id"]) for item in entry["contributors"]}


def diff_results(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """比较两个统计结果版本，输出各统计项的数值变化与贡献者增删。"""
    changes: dict[str, Any] = {"mni": {}, "rim_eve": {}, "type_frequency": {}}
    empty_mni = {"value": 0, "vessels": [], "sherd_groups": [], "single_sherds": []}
    empty_eve = {"value": 0.0, "contributors": []}
    empty_freq = {"sherds": 0, "vessels": 0, "sherd_ids": [], "vessel_ids": []}

    old_mni, new_mni = old["mni"]["by_type"], new["mni"]["by_type"]
    for type_code in sorted(set(old_mni) | set(new_mni)):
        before, after = old_mni.get(type_code, empty_mni), new_mni.get(type_code, empty_mni)
        old_vessels, old_sherds = _mni_ids(before)
        new_vessels, new_sherds = _mni_ids(after)
        if before["value"] != after["value"] or old_vessels != new_vessels or old_sherds != new_sherds:
            changes["mni"][type_code] = {
                "from": before["value"],
                "to": after["value"],
                "delta": after["value"] - before["value"],
                "added_vessels": sorted(new_vessels - old_vessels),
                "removed_vessels": sorted(old_vessels - new_vessels),
                "added_sherds": sorted(new_sherds - old_sherds),
                "removed_sherds": sorted(old_sherds - new_sherds),
            }

    old_eve, new_eve = old["rim_eve"]["by_type"], new["rim_eve"]["by_type"]
    for type_code in sorted(set(old_eve) | set(new_eve)):
        before, after = old_eve.get(type_code, empty_eve), new_eve.get(type_code, empty_eve)
        old_contribs, new_contribs = _eve_ids(before), _eve_ids(after)
        if before["value"] != after["value"] or old_contribs != new_contribs:
            changes["rim_eve"][type_code] = {
                "from": before["value"],
                "to": after["value"],
                "delta": round(after["value"] - before["value"], 6),
                "added_contributors": [{"kind": kind, "id": sid} for kind, sid in sorted(new_contribs - old_contribs)],
                "removed_contributors": [{"kind": kind, "id": sid} for kind, sid in sorted(old_contribs - new_contribs)],
            }

    old_freq, new_freq = old["type_frequency"]["by_type"], new["type_frequency"]["by_type"]
    for type_code in sorted(set(old_freq) | set(new_freq)):
        before, after = old_freq.get(type_code, empty_freq), new_freq.get(type_code, empty_freq)
        if before != after:
            changes["type_frequency"][type_code] = {
                "sherds": {"from": before["sherds"], "to": after["sherds"]},
                "vessels": {"from": before["vessels"], "to": after["vessels"]},
                "added_sherds": sorted(set(after["sherd_ids"]) - set(before["sherd_ids"])),
                "removed_sherds": sorted(set(before["sherd_ids"]) - set(after["sherd_ids"])),
                "added_vessels": sorted(set(after["vessel_ids"]) - set(before["vessel_ids"])),
                "removed_vessels": sorted(set(before["vessel_ids"]) - set(after["vessel_ids"])),
            }
    return changes
