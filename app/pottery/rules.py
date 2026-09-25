"""可配置、有版本的陶片兼容规则引擎。

所有函数都是纯函数：相同输入产生相同输出，不依赖数据库与时间。
规则配置先经 normalize_config 校验并补全默认值，再存入规则版本；
候选生成时按配置逐项评分并做确定性的贪心成团。
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from app.security import stable_json

PARTS = ("rim", "body", "base", "handle", "other")
DIMENSIONS = ("part", "fabric", "thickness", "decoration", "diameter", "context")

EPS = 1e-9

DEFAULT_CONFIG: dict[str, Any] = {
    "weights": {key: 1.0 for key in DIMENSIONS},
    "threshold": 0.5,
    "part_compatible": [["rim", "body"], ["body", "base"]],
    "fabric_groups": [],
    "thickness_tolerance_mm": 0.0,
    "decoration_min_shared": 0,
    "diameter_tolerance_mm": 0.0,
    "context_forbid": [],
    "context_forbid_same": False,
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """校验规则配置并补全默认值，返回可 JSON 序列化的规范配置。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("规则配置必须是 JSON 对象")
    unknown = sorted(set(raw) - set(DEFAULT_CONFIG))
    if unknown:
        raise ValueError(f"未知规则配置项: {unknown}")
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(raw)

    weights = cfg["weights"]
    if not isinstance(weights, dict):
        raise ValueError("weights 必须是对象")
    bad_keys = sorted(set(weights) - set(DIMENSIONS))
    if bad_keys:
        raise ValueError(f"未知评分维度: {bad_keys}")
    merged = {key: 1.0 for key in DIMENSIONS}
    for key, value in weights.items():
        if not _is_number(value) or value < 0:
            raise ValueError("评分权重必须是非负数值")
        merged[key] = float(value)
    total = sum(merged.values())
    if total <= 0:
        raise ValueError("评分权重之和必须大于 0")
    cfg["weights"] = {key: merged[key] / total for key in DIMENSIONS}

    threshold = cfg["threshold"]
    if not _is_number(threshold) or not 0 <= threshold <= 1:
        raise ValueError("threshold 必须在 [0,1] 区间")
    cfg["threshold"] = float(threshold)

    for pair in cfg["part_compatible"]:
        if not isinstance(pair, list) or len(pair) != 2 or any(p not in PARTS for p in pair):
            raise ValueError(f"部位兼容对无效: {pair}")

    seen_colors: set[str] = set()
    for group in cfg["fabric_groups"]:
        if not isinstance(group, list) or not group or any(not isinstance(c, str) or not c for c in group):
            raise ValueError("胎色分组必须是非空字符串列表")
        for color in group:
            if color in seen_colors:
                raise ValueError(f"胎色 {color} 出现在多个分组")
            seen_colors.add(color)

    for key in ("thickness_tolerance_mm", "diameter_tolerance_mm"):
        value = cfg[key]
        if not _is_number(value) or value < 0:
            raise ValueError(f"{key} 必须是非负数值")
        cfg[key] = float(value)

    min_shared = cfg["decoration_min_shared"]
    if not isinstance(min_shared, int) or isinstance(min_shared, bool) or min_shared < 0:
        raise ValueError("decoration_min_shared 必须是非负整数")

    for pair in cfg["context_forbid"]:
        if not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(x, str) or not x for x in pair):
            raise ValueError(f"上下文禁配对无效: {pair}")

    if not isinstance(cfg["context_forbid_same"], bool):
        raise ValueError("context_forbid_same 必须是布尔值")
    return cfg


def derive(cfg: dict[str, Any]) -> dict[str, Any]:
    """从规范配置构造评估用的派生结构（不入库）。"""
    return {
        "part_pairs": {frozenset(pair) for pair in cfg["part_compatible"]},
        "fabric_index": {color: idx for idx, group in enumerate(cfg["fabric_groups"]) for color in group},
        "forbid": {frozenset(pair) for pair in cfg["context_forbid"]},
    }


def context_forbidden(cfg: dict[str, Any], left: str, right: str, der: dict[str, Any] | None = None) -> bool:
    der = der or derive(cfg)
    if cfg["context_forbid_same"] and left == right:
        return True
    return frozenset((left, right)) in der["forbid"]


def thickness_gap(a_min: float, a_max: float, b_min: float, b_max: float) -> float:
    """两个厚度区间的间距，相交或相切时为 0。"""
    return max(0.0, max(a_min, b_min) - min(a_max, b_max))


def evaluate_pair(cfg: dict[str, Any], a: dict[str, Any], b: dict[str, Any], der: dict[str, Any] | None = None) -> dict[str, Any]:
    """对一对陶片逐项评分。硬约束不满足则 incompatible，软评分按权重加权。"""
    der = der or derive(cfg)
    hard: list[str] = []
    dims: dict[str, Any] = {}

    if a["part"] == b["part"]:
        part_ok, part_score = True, 1.0
    elif frozenset((a["part"], b["part"])) in der["part_pairs"]:
        part_ok, part_score = True, 0.8
    else:
        part_ok, part_score = False, 0.0
    if not part_ok:
        hard.append("part_incompatible")
    dims["part"] = {"score": part_score, "compatible": part_ok, "left": a["part"], "right": b["part"]}

    if a["fabric_color"] == b["fabric_color"]:
        fabric_ok, fabric_score = True, 1.0
    elif der["fabric_index"].get(a["fabric_color"], -1) == der["fabric_index"].get(b["fabric_color"], -2):
        fabric_ok, fabric_score = True, 0.7
    else:
        fabric_ok, fabric_score = False, 0.0
    if not fabric_ok:
        hard.append("fabric_incompatible")
    dims["fabric"] = {"score": fabric_score, "compatible": fabric_ok, "left": a["fabric_color"], "right": b["fabric_color"]}

    gap = thickness_gap(a["thickness_min"], a["thickness_max"], b["thickness_min"], b["thickness_max"])
    tol = cfg["thickness_tolerance_mm"]
    thick_ok = gap <= tol + EPS
    if not thick_ok:
        hard.append("thickness_incompatible")
    if gap <= EPS:
        thick_score = 1.0
    elif tol <= EPS:
        thick_score = 0.0
    else:
        thick_score = max(0.0, 1.0 - gap / tol)
    dims["thickness"] = {"score": round(thick_score, 6), "compatible": thick_ok, "gap_mm": round(gap, 6), "tolerance_mm": tol}

    deco_a, deco_b = set(a["decoration"]), set(b["decoration"])
    shared = sorted(deco_a & deco_b)
    deco_ok = len(shared) >= cfg["decoration_min_shared"]
    if not deco_ok:
        hard.append("decoration_insufficient")
    deco_score = 0.5 if not deco_a and not deco_b else len(shared) / len(deco_a | deco_b)
    dims["decoration"] = {"score": round(deco_score, 6), "compatible": deco_ok, "shared": shared}

    if a["diameter_mm"] is None or b["diameter_mm"] is None:
        diam_ok, diam_score, diff = True, 0.5, None
    else:
        diff = abs(a["diameter_mm"] - b["diameter_mm"])
        d_tol = cfg["diameter_tolerance_mm"]
        diam_ok = diff <= d_tol + EPS
        if diff <= EPS:
            diam_score = 1.0
        elif d_tol <= EPS:
            diam_score = 0.0
        else:
            diam_score = max(0.0, 1.0 - diff / d_tol)
    if not diam_ok:
        hard.append("diameter_incompatible")
    dims["diameter"] = {"score": round(diam_score, 6), "compatible": diam_ok, "diff_mm": round(diff, 6) if diff is not None else None}

    forbidden = context_forbidden(cfg, a["context_code"], b["context_code"], der)
    if forbidden:
        hard.append("context_forbidden")
    ctx_score = 0.0 if forbidden else (1.0 if a["context_code"] == b["context_code"] else 0.6)
    dims["context"] = {"score": ctx_score, "compatible": not forbidden, "left": a["context_code"], "right": b["context_code"]}

    weights = cfg["weights"]
    total = sum(weights[key] * dims[key]["score"] for key in DIMENSIONS)
    return {
        "compatible": not hard and total >= cfg["threshold"] - EPS,
        "score": round(total, 6),
        "threshold": cfg["threshold"],
        "hard_failures": hard,
        "dimensions": dims,
    }


def build_groups(cfg: dict[str, Any], sherds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """确定性贪心成团：按评分降序处理兼容对，新成员必须与组内全部成员两两兼容。

    因此传递相似（A~B、B~C）但直接不兼容（A!~C）的陶片不会同组。
    """
    der = derive(cfg)
    ordered = sorted(sherds, key=lambda s: (s["code_norm"], s["id"]))
    by_id = {s["id"]: s for s in ordered}

    def pair_key(x: int, y: int) -> tuple[int, int]:
        return (x, y) if (by_id[x]["code_norm"], x) <= (by_id[y]["code_norm"], y) else (y, x)

    pairs: dict[tuple[int, int], dict[str, Any]] = {}
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            evaluation = evaluate_pair(cfg, ordered[i], ordered[j], der)
            if evaluation["compatible"]:
                pairs[(ordered[i]["id"], ordered[j]["id"])] = evaluation

    def pair_order(item: tuple[tuple[int, int], dict[str, Any]]) -> tuple:
        (a_id, b_id), evaluation = item
        return (-evaluation["score"], by_id[a_id]["code_norm"], by_id[b_id]["code_norm"], a_id, b_id)

    assignment: dict[int, list[int]] = {}
    groups: list[list[int]] = []
    for (a_id, b_id), _ in sorted(pairs.items(), key=pair_order):
        group_a = assignment.get(a_id)
        group_b = assignment.get(b_id)
        if group_a is None and group_b is None:
            group = [a_id, b_id]
            groups.append(group)
            assignment[a_id] = group
            assignment[b_id] = group
        elif group_a is not None and group_b is None:
            if all(pair_key(b_id, member) in pairs for member in group_a):
                group_a.append(b_id)
                assignment[b_id] = group_a
        elif group_b is not None and group_a is None:
            if all(pair_key(a_id, member) in pairs for member in group_b):
                group_b.append(a_id)
                assignment[a_id] = group_b

    result = []
    for group in groups:
        members = sorted(group, key=lambda i: (by_id[i]["code_norm"], i))
        pair_scores: dict[str, Any] = {}
        scores = []
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                evaluation = pairs.get(pair_key(members[x], members[y]))
                if evaluation is None:
                    continue
                label = f"{by_id[members[x]]['code_norm']}~{by_id[members[y]]['code_norm']}"
                pair_scores[label] = evaluation
                scores.append(evaluation["score"])
        if not scores:
            continue
        result.append({
            "member_ids": members,
            "member_codes": [by_id[i]["code_norm"] for i in members],
            "score": round(sum(scores) / len(scores), 6),
            "pair_scores": pair_scores,
        })
    result.sort(key=lambda g: (g["member_codes"], g["member_ids"]))
    return result


def group_key(rule_set_id: int, member_codes: list[str]) -> str:
    digest = hashlib.sha256(stable_json({"rule_set_id": rule_set_id, "members": list(member_codes)}).encode()).hexdigest()
    return f"G{digest[:16]}"
