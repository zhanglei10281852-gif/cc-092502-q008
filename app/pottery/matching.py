from __future__ import annotations

import hashlib
from typing import Any

from app.security import stable_json

PARTS = ("rim", "body", "base", "handle", "other")

# 默认兼容规则：硬过滤（部位组合、胎色、纹饰冲突、厚度容差、尺寸差异、上下文禁配）
# 加上分维度加权评分，总分低于 candidate_min_score 的配对不进入候选。
DEFAULT_RULES: dict[str, Any] = {
    "name": "默认兼容规则",
    "candidate_min_score": 3.0,
    "dimensions": {
        "part": {
            "weight": 1.0,
            "same_factor": 1.0,
            "compatible_factor": 0.6,
            "compatible_pairs": [
                ["rim", "rim"],
                ["rim", "body"],
                ["rim", "handle"],
                ["body", "body"],
                ["body", "base"],
                ["body", "handle"],
                ["base", "base"],
            ],
        },
        "fabric_color": {
            "weight": 2.0,
            "same_factor": 1.0,
            "compatible_factor": 0.5,
            "compatible": [["红陶", "红褐陶"], ["灰陶", "灰黑陶"], ["黑陶", "灰黑陶"]],
        },
        "thickness": {"weight": 1.5, "tolerance_mm": 0.5},
        "decoration": {"weight": 2.0, "same_factor": 1.0, "both_blank_factor": 0.5, "one_blank_factor": 0.25},
        "dimensions": {"weight": 1.0, "tolerance_ratio": 0.5, "missing_factor": 0.5},
    },
}


def _pair_key(x: str, y: str) -> tuple[str, str]:
    return (x, y) if x <= y else (y, x)


def compile_rules(rules: dict[str, Any]) -> dict[str, Any]:
    """校验并预编译兼容规则，结构非法时抛出 ValueError。"""
    try:
        dims = rules["dimensions"]
        part = dims["part"]
        fabric = dims["fabric_color"]
        thickness = dims["thickness"]
        decoration = dims["decoration"]
        dimensions = dims["dimensions"]
        compiled = {
            "candidate_min_score": float(rules["candidate_min_score"]),
            "part": {
                "weight": float(part["weight"]),
                "same_factor": float(part.get("same_factor", 1.0)),
                "compatible_factor": float(part.get("compatible_factor", 0.6)),
                "pairs": {_pair_key(str(p[0]), str(p[1])) for p in part["compatible_pairs"]},
            },
            "fabric_color": {
                "weight": float(fabric["weight"]),
                "same_factor": float(fabric.get("same_factor", 1.0)),
                "compatible_factor": float(fabric.get("compatible_factor", 0.5)),
                "compatible": {_pair_key(str(p[0]), str(p[1])) for p in fabric.get("compatible", [])},
            },
            "thickness": {"weight": float(thickness["weight"]), "tolerance_mm": float(thickness["tolerance_mm"])},
            "decoration": {
                "weight": float(decoration["weight"]),
                "same_factor": float(decoration.get("same_factor", 1.0)),
                "both_blank_factor": float(decoration.get("both_blank_factor", 0.5)),
                "one_blank_factor": float(decoration.get("one_blank_factor", 0.25)),
            },
            "dimensions": {
                "weight": float(dimensions["weight"]),
                "tolerance_ratio": float(dimensions.get("tolerance_ratio", 0.5)),
                "missing_factor": float(dimensions.get("missing_factor", 0.5)),
            },
        }
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"兼容规则结构无效: {exc}") from exc
    if compiled["candidate_min_score"] < 0:
        raise ValueError("candidate_min_score 不能为负")
    if compiled["thickness"]["tolerance_mm"] < 0:
        raise ValueError("thickness.tolerance_mm 不能为负")
    return compiled


def evaluate_pair(compiled: dict[str, Any], a: dict[str, Any], b: dict[str, Any], excluded_contexts: set[frozenset]) -> dict[str, Any] | None:
    """评估一对陶片：不兼容返回 None，兼容则返回逐项评分。

    厚度区间按容差扩展后判断是否相接（临界值包含边界），评分使用原始区间的重叠比例。
    """
    context_a, context_b = a.get("context_id"), b.get("context_id")
    if context_a and context_b and context_a != context_b and frozenset((context_a, context_b)) in excluded_contexts:
        return None

    out: dict[str, Any] = {}

    part_rule = compiled["part"]
    part_a, part_b = a["part"], b["part"]
    if _pair_key(part_a, part_b) not in part_rule["pairs"]:
        return None
    factor = part_rule["same_factor"] if part_a == part_b else part_rule["compatible_factor"]
    out["part"] = {"score": round(part_rule["weight"] * factor, 6), "weight": part_rule["weight"], "detail": f"{part_a}~{part_b}"}

    fabric_rule = compiled["fabric_color"]
    fabric_a, fabric_b = a["fabric_color"], b["fabric_color"]
    if fabric_a == fabric_b:
        factor = fabric_rule["same_factor"]
    elif _pair_key(fabric_a, fabric_b) in fabric_rule["compatible"]:
        factor = fabric_rule["compatible_factor"]
    else:
        return None
    out["fabric_color"] = {"score": round(fabric_rule["weight"] * factor, 6), "weight": fabric_rule["weight"], "detail": f"{fabric_a}~{fabric_b}"}

    thickness_rule = compiled["thickness"]
    lo = max(a["thickness_min_mm"], b["thickness_min_mm"])
    hi = min(a["thickness_max_mm"], b["thickness_max_mm"])
    gap = lo - hi
    if gap > thickness_rule["tolerance_mm"]:
        return None
    overlap = max(0.0, hi - lo)
    shortest = max(
        1e-9,
        min(a["thickness_max_mm"] - a["thickness_min_mm"], b["thickness_max_mm"] - b["thickness_min_mm"]),
    )
    ratio = min(1.0, overlap / shortest)
    out["thickness"] = {
        "score": round(thickness_rule["weight"] * ratio, 6),
        "weight": thickness_rule["weight"],
        "detail": f"gap={round(max(0.0, gap), 6)}",
    }

    decoration_rule = compiled["decoration"]
    decoration_a = a.get("decoration_code") or ""
    decoration_b = b.get("decoration_code") or ""
    if decoration_a and decoration_b:
        if decoration_a != decoration_b:
            return None
        factor, note = decoration_rule["same_factor"], "same"
    elif not decoration_a and not decoration_b:
        factor, note = decoration_rule["both_blank_factor"], "both_blank"
    else:
        factor, note = decoration_rule["one_blank_factor"], "one_blank"
    out["decoration"] = {"score": round(decoration_rule["weight"] * factor, 6), "weight": decoration_rule["weight"], "detail": note}

    dimension_rule = compiled["dimensions"]
    length_a, length_b = a.get("length_mm"), b.get("length_mm")
    width_a, width_b = a.get("width_mm"), b.get("width_mm")
    if length_a and length_b and width_a and width_b:
        ratio_length = abs(length_a - length_b) / max(length_a, length_b)
        ratio_width = abs(width_a - width_b) / max(width_a, width_b)
        if ratio_length > dimension_rule["tolerance_ratio"] or ratio_width > dimension_rule["tolerance_ratio"]:
            return None
        similarity = 1.0 - (ratio_length + ratio_width) / 2.0
        out["dimensions"] = {
            "score": round(dimension_rule["weight"] * similarity, 6),
            "weight": dimension_rule["weight"],
            "detail": f"similarity={round(similarity, 6)}",
        }
    else:
        out["dimensions"] = {
            "score": round(dimension_rule["weight"] * dimension_rule["missing_factor"], 6),
            "weight": dimension_rule["weight"],
            "detail": "missing",
        }

    total = round(sum(item["score"] for item in out.values()), 6)
    if total < compiled["candidate_min_score"]:
        return None
    return {"total": total, "dimensions": out}


def _maximal_cliques(nodes: list[str], adjacency: dict[str, set[str]]) -> list[list[str]]:
    """Bron–Kerbosch（带枢轴），顶点按编码排序遍历，保证输出确定。

    候选组必须是团（组内两两兼容），因此传递相似但直接不兼容的陶片
    不会被并入同一组。
    """
    cliques: list[list[str]] = []

    def search(r: set[str], p: set[str], x: set[str]) -> None:
        if not p and not x:
            cliques.append(sorted(r))
            return
        pivot = max(p | x, key=lambda u: len(adjacency[u] & p), default=None)
        todo = p - (adjacency[pivot] if pivot is not None else set())
        for vertex in sorted(todo):
            search(r | {vertex}, p & adjacency[vertex], x & adjacency[vertex])
            p = p - {vertex}
            x = x | {vertex}

    search(set(), set(nodes), set())
    return cliques


def build_candidate_groups(sherds: list[dict[str, Any]], compiled: dict[str, Any], excluded_contexts: set[frozenset]) -> list[dict[str, Any]]:
    """由陶片集合生成候选组：组内两两兼容，组评分为最弱配对得分（min），
    每个成员带逐项评分的均值，breakdown 记录每一对的分维度明细。"""
    ordered = sorted(sherds, key=lambda s: s["sherd_code"])
    codes = [s["sherd_code"] for s in ordered]
    adjacency: dict[str, set[str]] = {code: set() for code in codes}
    pair_evals: dict[tuple[str, str], dict[str, Any]] = {}
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            evaluation = evaluate_pair(compiled, ordered[i], ordered[j], excluded_contexts)
            if evaluation is not None:
                key = (ordered[i]["sherd_code"], ordered[j]["sherd_code"])
                pair_evals[key] = evaluation
                adjacency[key[0]].add(key[1])
                adjacency[key[1]].add(key[0])
    groups: list[dict[str, Any]] = []
    for clique in _maximal_cliques(codes, adjacency):
        if len(clique) < 2:
            continue
        members = sorted(clique)
        pairs = []
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                evaluation = pair_evals[(members[x], members[y])]
                pairs.append({"a": members[x], "b": members[y], "total": evaluation["total"], "dimensions": evaluation["dimensions"]})
        score = round(min(pair["total"] for pair in pairs), 6)
        member_scores = []
        for code in members:
            involving = [pair["total"] for pair in pairs if pair["a"] == code or pair["b"] == code]
            member_scores.append({"sherd_code": code, "item_score": round(sum(involving) / len(involving), 6)})
        breakdown = {"aggregation": "min_pair_total", "group_score": score, "pairs": pairs}
        group_key = hashlib.sha256(stable_json({"members": members}).encode()).hexdigest()[:32]
        groups.append({"group_key": group_key, "score": score, "members": member_scores, "member_codes": members, "breakdown": breakdown})
    groups.sort(key=lambda group: group["member_codes"])
    return groups
