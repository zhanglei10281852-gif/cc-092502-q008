from __future__ import annotations

import threading

import pytest

from app.pottery.matching import DEFAULT_RULES, compile_rules, evaluate_pair
from app.pottery.service import PotteryService
from app.service import ServiceError


def make_project(client, owner, code="PT01"):
    response = client.post("/api/projects", json={"code": code, "name": "陶器拼合项目", "site_name": "测试遗址"}, headers=owner["headers"])
    assert response.status_code == 201
    return response.json()["id"]


def make_user(client, username, password="Passw0rd!23456"):
    created = client.post("/api/users", json={"username": username, "display_name": username, "password": password})
    assert created.status_code == 201
    login = client.post("/api/sessions", json={"username": username, "password": password})
    assert login.status_code == 200
    return {"id": created.json()["id"], "headers": {"Authorization": f"Bearer {login.json()['token']}"}}


def sherd_payload(code, **overrides):
    payload = {
        "sherd_code": code,
        "part": "body",
        "fabric_color": "红陶",
        "thickness_min_mm": 5.0,
        "thickness_max_mm": 6.0,
        "vessel_type": "罐",
    }
    payload.update(overrides)
    return payload


def add_sherd(client, owner, project_id, code, **overrides):
    response = client.post(f"/api/projects/{project_id}/pottery/sherds", json=sherd_payload(code, **overrides), headers=owner["headers"])
    assert response.status_code == 201, response.json()
    return response.json()


def generate(client, owner, project_id):
    response = client.post(f"/api/projects/{project_id}/pottery/candidates/generate", headers=owner["headers"])
    assert response.status_code == 200, response.json()
    return response.json()


def list_candidates(client, owner, project_id, **params):
    response = client.get(f"/api/projects/{project_id}/pottery/candidates", params=params, headers=owner["headers"])
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def confirm(client, owner, project_id, candidate_id, rationale="胎色厚度一致，可拼", **extra):
    payload = {"rationale": rationale, **extra}
    return client.post(f"/api/projects/{project_id}/pottery/candidates/{candidate_id}/confirm", json=payload, headers=owner["headers"])


def member_codes(candidate):
    return tuple(sorted(member["sherd_code"] for member in candidate["members"]))


def default_plan_id(client, owner, project_id):
    plans = client.get(f"/api/projects/{project_id}/pottery/plans", headers=owner["headers"]).json()["data"]
    return next(plan["id"] for plan in plans if plan["code"] == "default")


# ---------------------------------------------------------------- 厚度区间临界值


def test_thickness_interval_boundary_values():
    rules = compile_rules(DEFAULT_RULES)
    base = {"part": "body", "fabric_color": "红陶", "decoration_code": "", "length_mm": None, "width_mm": None, "context_id": None}
    a = {**base, "thickness_min_mm": 5.0, "thickness_max_mm": 7.0}
    touching = {**base, "thickness_min_mm": 7.5, "thickness_max_mm": 9.0}  # 间距恰好等于容差 0.5
    beyond = {**base, "thickness_min_mm": 7.6, "thickness_max_mm": 9.0}  # 间距 0.6 超出容差
    assert evaluate_pair(rules, a, touching, set()) is not None
    assert evaluate_pair(rules, a, beyond, set()) is None


def test_thickness_boundary_via_api(client, owner):
    project_id = make_project(client, owner)
    add_sherd(client, owner, project_id, "T1", thickness_min_mm=5.0, thickness_max_mm=7.0)
    add_sherd(client, owner, project_id, "T2", thickness_min_mm=7.5, thickness_max_mm=9.0)
    add_sherd(client, owner, project_id, "T3", thickness_min_mm=7.6, thickness_max_mm=9.0)
    generate(client, owner, project_id)
    groups = {member_codes(c) for c in list_candidates(client, owner, project_id)}
    assert ("T1", "T2") in groups  # 临界值内：兼容
    assert ("T1", "T3") not in groups  # 超出容差：不兼容
    assert ("T2", "T3") in groups


def test_sherd_thickness_validation(client, owner):
    project_id = make_project(client, owner)
    bad = client.post(
        f"/api/projects/{project_id}/pottery/sherds",
        json=sherd_payload("BAD", thickness_min_mm=8.0, thickness_max_mm=5.0),
        headers=owner["headers"],
    )
    assert bad.status_code == 422
    degenerate = client.post(
        f"/api/projects/{project_id}/pottery/sherds",
        json=sherd_payload("OK", thickness_min_mm=6.0, thickness_max_mm=6.0),
        headers=owner["headers"],
    )
    assert degenerate.status_code == 201  # 区间退化为单点仍合法


# ---------------------------------------------------------------- 候选生成与逐项评分


def test_transitive_similar_but_directly_incompatible(client, owner):
    project_id = make_project(client, owner)
    add_sherd(client, owner, project_id, "A1", thickness_min_mm=5.0, thickness_max_mm=6.0)
    add_sherd(client, owner, project_id, "B1", thickness_min_mm=6.4, thickness_max_mm=7.0)
    add_sherd(client, owner, project_id, "C1", thickness_min_mm=7.4, thickness_max_mm=8.0)
    summary = generate(client, owner, project_id)
    assert summary["created"] == 2
    candidates = list_candidates(client, owner, project_id)
    groups = sorted(member_codes(c) for c in candidates)
    assert groups == [("A1", "B1"), ("B1", "C1")]
    # A1~C1 传递相似但直接不兼容，三者不得同组
    assert all(not {"A1", "C1"} <= set(member_codes(c)) for c in candidates)


def test_candidate_scoring_breakdown(client, owner):
    project_id = make_project(client, owner)
    add_sherd(client, owner, project_id, "R1", part="rim", decoration_code="SW01", rim_arc_percent=20.0)
    add_sherd(client, owner, project_id, "R2", part="body", decoration_code="SW01")
    generate(client, owner, project_id)
    candidate = list_candidates(client, owner, project_id)[0]
    breakdown = candidate["breakdown"]
    assert breakdown["aggregation"] == "min_pair_total"
    pair = breakdown["pairs"][0]
    assert set(pair["dimensions"]) == {"part", "fabric_color", "thickness", "decoration", "dimensions"}
    assert pair["dimensions"]["part"]["detail"] == "rim~body"
    assert pair["dimensions"]["decoration"]["detail"] == "same"
    assert candidate["score"] == pair["total"] == 6.6
    assert {m["sherd_code"] for m in candidate["members"]} == {"R1", "R2"}
    assert all(m["item_score"] == 6.6 for m in candidate["members"])


# ---------------------------------------------------------------- 复核：成员唯一 / 依据 / 版本一致性 / 上下文禁配


def test_member_uniqueness_sequential(client, owner):
    project_id = make_project(client, owner)
    add_sherd(client, owner, project_id, "A1", thickness_min_mm=5.0, thickness_max_mm=6.0)
    add_sherd(client, owner, project_id, "B1", thickness_min_mm=6.4, thickness_max_mm=7.0)
    add_sherd(client, owner, project_id, "C1", thickness_min_mm=7.4, thickness_max_mm=8.0)
    generate(client, owner, project_id)
    candidates = {member_codes(c): c for c in list_candidates(client, owner, project_id)}
    first = confirm(client, owner, project_id, candidates[("A1", "B1")]["id"])
    assert first.status_code == 200
    assert first.json()["vessel"]["vessel_code"] == "V0001"
    second = confirm(client, owner, project_id, candidates[("B1", "C1")]["id"])
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "member_conflict"


def test_concurrent_review_confirmation(client, owner):
    project_id = make_project(client, owner)
    add_sherd(client, owner, project_id, "A1", thickness_min_mm=5.0, thickness_max_mm=6.0)
    add_sherd(client, owner, project_id, "B1", thickness_min_mm=6.4, thickness_max_mm=7.0)
    add_sherd(client, owner, project_id, "C1", thickness_min_mm=7.4, thickness_max_mm=8.0)
    generate(client, owner, project_id)
    candidate_ids = [c["id"] for c in list_candidates(client, owner, project_id)]
    assert len(candidate_ids) == 2
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt(candidate_id):
        barrier.wait(timeout=10)
        try:
            PotteryService().confirm_candidate(project_id, candidate_id, owner["user"]["id"], "并发复核确认")
            outcomes.append("ok")
        except ServiceError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=attempt, args=(cid,)) for cid in candidate_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # 同一碎片不得同时确认进两个器物：恰好一个成功，一个成员冲突
    assert sorted(outcomes) == ["member_conflict", "ok"]
    vessels = client.get(f"/api/projects/{project_id}/pottery/vessels", headers=owner["headers"]).json()["data"]
    assert len([v for v in vessels if v["status"] == "active"]) == 1


def test_review_requires_rationale(client, owner):
    project_id = make_project(client, owner)
    add_sherd(client, owner, project_id, "A1")
    add_sherd(client, owner, project_id, "A2")
    generate(client, owner, project_id)
    candidate = list_candidates(client, owner, project_id)[0]
    assert confirm(client, owner, project_id, candidate["id"], rationale="").status_code == 422
    assert confirm(client, owner, project_id, candidate["id"], rationale="   ").status_code == 422
    rejected = client.post(
        f"/api/projects/{project_id}/pottery/candidates/{candidate['id']}/reject",
        json={"rationale": "  "},
        headers=owner["headers"],
    )
    assert rejected.status_code == 422
    # 服务层同样强制依据
    with pytest.raises(ServiceError):
        PotteryService().confirm_candidate(project_id, candidate["id"], owner["user"]["id"], "")


def test_reject_and_review_trail(client, owner):
    project_id = make_project(client, owner)
    add_sherd(client, owner, project_id, "A1")
    add_sherd(client, owner, project_id, "A2")
    add_sherd(client, owner, project_id, "A3", thickness_min_mm=8.0, thickness_max_mm=9.0)
    generate(client, owner, project_id)
    candidate = list_candidates(client, owner, project_id)[0]
    rejected = client.post(
        f"/api/projects/{project_id}/pottery/candidates/{candidate['id']}/reject",
        json={"rationale": "胎色虽同但断面不吻合"},
        headers=owner["headers"],
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    again = confirm(client, owner, project_id, candidate["id"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "candidate_not_pending"
    reviews = client.get(f"/api/projects/{project_id}/pottery/reviews", headers=owner["headers"]).json()["data"]
    assert len(reviews) == 1
    assert reviews[0]["action"] == "reject"
    assert reviews[0]["rationale"] == "胎色虽同但断面不吻合"


def test_rule_version_consistency(client, owner):
    project_id = make_project(client, owner)
    add_sherd(client, owner, project_id, "A1")
    add_sherd(client, owner, project_id, "A2")
    generate(client, owner, project_id)
    stale = list_candidates(client, owner, project_id)[0]
    # 发布规则新版本后，旧版本生成的候选不得再确认
    import copy

    rules_v2 = copy.deepcopy(DEFAULT_RULES)
    rules_v2["dimensions"]["thickness"]["tolerance_mm"] = 1.0
    created = client.post(f"/api/projects/{project_id}/pottery/rule-sets", json={"name": "加严厚度容差", "rules": rules_v2}, headers=owner["headers"])
    assert created.status_code == 201
    assert created.json()["version"] == 2
    conflict = confirm(client, owner, project_id, stale["id"])
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "rule_version_conflict"
    # 按新版本重新生成后可以确认，且声明错误版本号也会被拒绝
    generate(client, owner, project_id)
    fresh = list_candidates(client, owner, project_id, status="pending")[0]
    wrong = confirm(client, owner, project_id, fresh["id"], expected_rule_set_id=stale["rule_set_id"])
    assert wrong.status_code == 409
    ok = confirm(client, owner, project_id, fresh["id"], expected_rule_set_id=fresh["rule_set_id"])
    assert ok.status_code == 200


def test_context_exclusion_blocks_generation_and_confirm(client, owner):
    project_id = make_project(client, owner)
    for code in ("H1", "H2"):
        response = client.post(f"/api/projects/{project_id}/pottery/contexts", json={"code": code, "kind": "灰坑"}, headers=owner["headers"])
        assert response.status_code == 201
    add_sherd(client, owner, project_id, "H1:001", context_code="H1")
    add_sherd(client, owner, project_id, "H2:001", context_code="H2")
    generate(client, owner, project_id)
    candidate = list_candidates(client, owner, project_id)[0]
    # 候选生成后新增禁配关系，确认时仍会重新校验
    exclusion = client.post(
        f"/api/projects/{project_id}/pottery/context-exclusions",
        json={"context_a": "H1", "context_b": "H2", "reason": "层位关系冲突"},
        headers=owner["headers"],
    )
    assert exclusion.status_code == 201
    blocked = confirm(client, owner, project_id, candidate["id"])
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "context_excluded"
    # 重新生成候选时禁配同样生效
    summary = generate(client, owner, project_id)
    assert summary["total"] == 0
    assert list_candidates(client, owner, project_id, status="pending") == []


# ---------------------------------------------------------------- 拆组与合组


def test_split_and_merge_vessels(client, owner):
    project_id = make_project(client, owner)
    for code in ("H1", "H2"):
        client.post(f"/api/projects/{project_id}/pottery/contexts", json={"code": code}, headers=owner["headers"])
    client.post(
        f"/api/projects/{project_id}/pottery/context-exclusions",
        json={"context_a": "H1", "context_b": "H2", "reason": "不同灰坑禁止拼合"},
        headers=owner["headers"],
    )
    add_sherd(client, owner, project_id, "H1:001", context_code="H1", thickness_min_mm=5.0, thickness_max_mm=6.0)
    add_sherd(client, owner, project_id, "H1:002", context_code="H1", thickness_min_mm=5.1, thickness_max_mm=6.0)
    add_sherd(client, owner, project_id, "H2:001", context_code="H2", thickness_min_mm=8.0, thickness_max_mm=9.0)
    add_sherd(client, owner, project_id, "H2:002", context_code="H2", thickness_min_mm=8.2, thickness_max_mm=9.0)
    generate(client, owner, project_id)
    candidates = {member_codes(c): c for c in list_candidates(client, owner, project_id)}
    assert set(candidates) == {("H1:001", "H1:002"), ("H2:001", "H2:002")}
    v1 = confirm(client, owner, project_id, candidates[("H1:001", "H1:002")]["id"]).json()["vessel"]
    v2 = confirm(client, owner, project_id, candidates[("H2:001", "H2:002")]["id"]).json()["vessel"]
    # 跨禁配上下文合组被拒绝且原器物保持不变
    denied = client.post(
        f"/api/projects/{project_id}/pottery/vessels/merge",
        json={"vessel_ids": [v1["id"], v2["id"]], "rationale": "尝试跨灰坑合组"},
        headers=owner["headers"],
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "context_excluded"
    active = client.get(f"/api/projects/{project_id}/pottery/vessels", params={"status": "active"}, headers=owner["headers"]).json()["data"]
    assert len(active) == 2
    # 拆组：V1 拆成两个单片器物
    split = client.post(
        f"/api/projects/{project_id}/pottery/vessels/{v1['id']}/split",
        json={"groups": [["H1:001"], ["H1:002"]], "rationale": "复查后认为不应拼合"},
        headers=owner["headers"],
    )
    assert split.status_code == 200
    pieces = split.json()["vessels"]
    assert len(pieces) == 2
    assert all(p["source"] == "split" for p in pieces)
    # 合组：同一灰坑的两片重新合并
    merged = client.post(
        f"/api/projects/{project_id}/pottery/vessels/merge",
        json={"vessel_ids": [pieces[0]["id"], pieces[1]["id"]], "rationale": "二次比对确认可拼"},
        headers=owner["headers"],
    )
    assert merged.status_code == 200
    assert sorted(m["sherd_code"] for m in merged.json()["vessel"]["members"]) == ["H1:001", "H1:002"]
    reviews = client.get(f"/api/projects/{project_id}/pottery/reviews", headers=owner["headers"]).json()["data"]
    actions = [r["action"] for r in reviews]
    assert actions.count("confirm") == 2 and "split" in actions and "merge" in actions
    assert all(r["rationale"] for r in reviews)


def test_split_invalid_partition(client, owner):
    project_id = make_project(client, owner)
    add_sherd(client, owner, project_id, "A1")
    add_sherd(client, owner, project_id, "A2")
    add_sherd(client, owner, project_id, "A3", thickness_min_mm=8.0, thickness_max_mm=9.0)
    generate(client, owner, project_id)
    candidate = list_candidates(client, owner, project_id)[0]
    vessel = confirm(client, owner, project_id, candidate["id"]).json()["vessel"]
    bad = client.post(
        f"/api/projects/{project_id}/pottery/vessels/{vessel['id']}/split",
        json={"groups": [["A1"], ["A1"]], "rationale": "重复划分"},
        headers=owner["headers"],
    )
    assert bad.status_code == 400
    missing = client.post(
        f"/api/projects/{project_id}/pottery/vessels/{vessel['id']}/split",
        json={"groups": [["A1"], ["A3"]], "rationale": "包含非成员"},
        headers=owner["headers"],
    )
    assert missing.status_code == 400


# ---------------------------------------------------------------- 统计报告：版本、溯源、差异


def test_report_versions_provenance_and_diff(client, owner):
    project_id = make_project(client, owner)
    batch = {
        "batch_key": "stats-1",
        "file_name": "h1.json",
        "contexts": [{"code": "H1"}, {"code": "H2"}],
        "sherds": [
            sherd_payload("H1:001", context_code="H1", thickness_min_mm=5.0, thickness_max_mm=6.0),
            sherd_payload("H1:002", context_code="H1", thickness_min_mm=5.2, thickness_max_mm=6.1),
            sherd_payload("H2:001", context_code="H2", thickness_min_mm=8.0, thickness_max_mm=9.0),
        ],
    }
    imported = client.post(f"/api/projects/{project_id}/pottery/imports", json=batch, headers=owner["headers"])
    assert imported.status_code == 201
    plan_id = default_plan_id(client, owner, project_id)
    report_v1 = client.get(f"/api/projects/{project_id}/pottery/plans/{plan_id}/reports/1", headers=owner["headers"]).json()
    mni_v1 = report_v1["result"]["metrics"]["mni"]["total"]
    assert mni_v1["value"] == 3
    assert {c["code"] for c in mni_v1["contributors"]} == {"H1:001", "H1:002", "H2:001"}
    # 确认一组后分组变化 → 产生新报告版本
    candidate = list_candidates(client, owner, project_id)[0]
    confirmed = confirm(client, owner, project_id, candidate["id"])
    assert confirmed.status_code == 200
    assert confirmed.json()["reports"] == [{"plan_code": "default", "version": 2, "created_new": True}]
    report_v2 = client.get(f"/api/projects/{project_id}/pottery/plans/{plan_id}/reports/2", headers=owner["headers"]).json()
    mni_v2 = report_v2["result"]["metrics"]["mni"]["total"]
    assert mni_v2["value"] == 2
    assert {c["code"] for c in mni_v2["contributors"]} == {"V0001", "H2:001"}
    # 差异对比
    diff = client.get(f"/api/projects/{project_id}/pottery/plans/{plan_id}/reports/1/diff/2", headers=owner["headers"]).json()
    assert diff["from_version"] == 1 and diff["to_version"] == 2
    change = diff["metrics"]["mni.total"]
    assert change["old"] == 3 and change["new"] == 2 and change["delta"] == -1
    assert change["added_contributors"] == ["vessel:V0001"]
    assert change["removed_contributors"] == ["sherd:H1:001", "sherd:H1:002"]
    # 分组未再变化时重复计算不产生新版本
    recompute = client.post(f"/api/projects/{project_id}/pottery/plans/{plan_id}/reports", headers=owner["headers"])
    assert recompute.status_code == 200
    assert recompute.json() == {"plan_code": "default", "version": 2, "created_new": False}


def test_rim_equivalent_and_type_frequency(client, owner):
    project_id = make_project(client, owner)
    batch = {
        "batch_key": "rim-1",
        "sherds": [
            sherd_payload("R1", part="rim", rim_arc_percent=60.0, vessel_type="罐"),
            sherd_payload("R2", part="rim", rim_arc_percent=60.0, vessel_type="罐"),
            sherd_payload("R3", part="rim", rim_arc_percent=30.0, vessel_type="盆", thickness_min_mm=8.0, thickness_max_mm=9.0),
        ],
    }
    assert client.post(f"/api/projects/{project_id}/pottery/imports", json=batch, headers=owner["headers"]).status_code == 201
    candidate = list_candidates(client, owner, project_id)[0]
    assert member_codes(candidate) == ("R1", "R2")
    assert confirm(client, owner, project_id, candidate["id"]).status_code == 200
    plan_id = default_plan_id(client, owner, project_id)
    report = client.get(f"/api/projects/{project_id}/pottery/plans/{plan_id}/reports/2", headers=owner["headers"]).json()["result"]
    rim = report["metrics"]["rim_equivalent"]
    # 器物口沿当量封顶 1.0（60%+60%），未入组口沿片按 0.3 计入
    assert rim["by_type"]["罐"]["value"] == 1.0
    assert rim["by_type"]["罐"]["contributors"] == [{"kind": "vessel", "code": "V0001", "contribution": 1.0}]
    assert rim["by_type"]["盆"]["value"] == 0.3
    assert rim["by_type"]["盆"]["contributors"] == [{"kind": "sherd", "code": "R3", "contribution": 0.3}]
    assert rim["total"]["value"] == 1.3
    freq = report["metrics"]["type_frequency"]
    assert freq["by_type"]["罐"]["value"] == 1 and freq["by_type"]["罐"]["share"] == 1.0
    assert report["metrics"]["mni"]["total"]["value"] == 2


# ---------------------------------------------------------------- 离线导入：触发、幂等、回滚


def test_import_triggers_candidates_and_statistics(client, owner):
    project_id = make_project(client, owner)
    batch = {
        "batch_key": "batch-1",
        "file_name": "sherds.json",
        "contexts": [{"code": "H1", "kind": "灰坑"}, {"code": "H2", "kind": "灰坑"}],
        "sherds": [
            sherd_payload("H1:001", context_code="H1"),
            sherd_payload("H1:002", context_code="H1", thickness_min_mm=5.1, thickness_max_mm=6.1),
            sherd_payload("H2:001", context_code="H2", thickness_min_mm=8.0, thickness_max_mm=9.0),
        ],
    }
    first = client.post(f"/api/projects/{project_id}/pottery/imports", json=batch, headers=owner["headers"])
    assert first.status_code == 201
    body = first.json()
    assert body["idempotent"] is False
    assert body["candidates"]["created"] == 1
    assert body["reports"] == [{"plan_code": "default", "version": 1, "created_new": True}]
    assert body["sherd_count"] == 3
    # 相同批次键与内容重放：幂等返回，不产生重复数据
    second = client.post(f"/api/projects/{project_id}/pottery/imports", json=batch, headers=owner["headers"])
    assert second.status_code == 200
    assert second.json()["idempotent"] is True
    sherds = client.get(f"/api/projects/{project_id}/pottery/sherds", headers=owner["headers"]).json()["data"]
    assert len(sherds) == 3
    # 相同批次键但内容不同：冲突
    changed = {**batch, "sherds": batch["sherds"][:1]}
    third = client.post(f"/api/projects/{project_id}/pottery/imports", json=changed, headers=owner["headers"])
    assert third.status_code == 409
    assert third.json()["error"]["code"] == "import_conflict"


def test_import_failure_rolls_back(client, owner):
    project_id = make_project(client, owner)
    batch = {
        "batch_key": "bad-1",
        "contexts": [{"code": "H1"}],
        "sherds": [
            sherd_payload("X1", context_code="H1"),
            sherd_payload("X2", context_code="H9"),  # 引用不存在的上下文
        ],
    }
    response = client.post(f"/api/projects/{project_id}/pottery/imports", json=batch, headers=owner["headers"])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "context_not_found"
    # 整批回滚：陶片、上下文、导入记录、候选、报告都不存在
    assert client.get(f"/api/projects/{project_id}/pottery/sherds", headers=owner["headers"]).json()["data"] == []
    assert client.get(f"/api/projects/{project_id}/pottery/contexts", headers=owner["headers"]).json()["data"] == []
    assert client.get(f"/api/projects/{project_id}/pottery/imports", headers=owner["headers"]).json()["data"] == []
    assert list_candidates(client, owner, project_id) == []
    assert client.get(f"/api/projects/{project_id}/pottery/plans", headers=owner["headers"]).json()["data"] == []
    # 批次内编号重复同样整体回滚
    duplicate = {"batch_key": "bad-2", "sherds": [sherd_payload("DUP"), sherd_payload("DUP")]}
    assert client.post(f"/api/projects/{project_id}/pottery/imports", json=duplicate, headers=owner["headers"]).status_code == 409
    assert client.get(f"/api/projects/{project_id}/pottery/sherds", headers=owner["headers"]).json()["data"] == []


# ---------------------------------------------------------------- 确定性输出


def test_deterministic_output_for_same_input(client, owner):
    batch = {
        "batch_key": "det-1",
        "sherds": [
            sherd_payload("D1", thickness_min_mm=5.0, thickness_max_mm=6.0),
            sherd_payload("D2", thickness_min_mm=5.2, thickness_max_mm=6.1),
            sherd_payload("D3", thickness_min_mm=8.0, thickness_max_mm=9.0),
        ],
    }
    project_one = make_project(client, owner, "DET1")
    project_two = make_project(client, owner, "DET2")
    assert client.post(f"/api/projects/{project_one}/pottery/imports", json=batch, headers=owner["headers"]).status_code == 201
    assert client.post(f"/api/projects/{project_two}/pottery/imports", json=batch, headers=owner["headers"]).status_code == 201
    candidates_one = list_candidates(client, owner, project_one)
    candidates_two = list_candidates(client, owner, project_two)
    fingerprint = lambda items: [(c["group_key"], c["score"], c["breakdown"], member_codes(c)) for c in items]
    assert fingerprint(candidates_one) == fingerprint(candidates_two)
    # 相同输入重复生成候选：无增删
    again = generate(client, owner, project_one)
    assert again["created"] == 0 and again["removed"] == 0
    assert fingerprint(list_candidates(client, owner, project_one)) == fingerprint(candidates_one)
    # 相同输入的统计结果逐字节一致
    report_one = client.get(f"/api/projects/{project_one}/pottery/plans/{default_plan_id(client, owner, project_one)}/reports/1", headers=owner["headers"]).json()
    report_two = client.get(f"/api/projects/{project_two}/pottery/plans/{default_plan_id(client, owner, project_two)}/reports/1", headers=owner["headers"]).json()
    assert report_one["input_hash"] == report_two["input_hash"]
    assert report_one["result"] == report_two["result"]


# ---------------------------------------------------------------- 分页与排序


def test_cursor_pagination_stable_sort(client, owner):
    project_id = make_project(client, owner)
    for index in range(5):
        add_sherd(client, owner, project_id, f"S{index:02d}", thickness_min_mm=5.0 + index, thickness_max_mm=6.0 + index)
    codes, cursor = [], None
    while True:
        params = {"limit": 2, "sort": "code"}
        if cursor:
            params["cursor"] = cursor
        page = client.get(f"/api/projects/{project_id}/pottery/sherds", params=params, headers=owner["headers"]).json()
        codes += [s["sherd_code"] for s in page["data"]]
        cursor = page["page"]["next_cursor"]
        if not page["page"]["has_more"]:
            break
    assert codes == ["S00", "S01", "S02", "S03", "S04"]
    descending = client.get(f"/api/projects/{project_id}/pottery/sherds", params={"limit": 3, "sort": "-code"}, headers=owner["headers"]).json()
    assert [s["sherd_code"] for s in descending["data"]] == ["S04", "S03", "S02"]
    assert descending["page"]["has_more"] is True
    assert client.get(f"/api/projects/{project_id}/pottery/sherds", params={"cursor": "!!"}, headers=owner["headers"]).status_code == 400
    assert client.get(f"/api/projects/{project_id}/pottery/sherds", params={"sort": "unknown"}, headers=owner["headers"]).status_code == 400


# ---------------------------------------------------------------- 权限


def test_role_permissions(client, owner):
    project_id = make_project(client, owner)
    viewer = make_user(client, "viewer1")
    recorder = make_user(client, "recorder1")
    client.post(f"/api/projects/{project_id}/members", json={"user_id": viewer["id"], "role": "viewer"}, headers=owner["headers"])
    client.post(f"/api/projects/{project_id}/members", json={"user_id": recorder["id"], "role": "recorder"}, headers=owner["headers"])
    add_sherd(client, owner, project_id, "A1")
    add_sherd(client, owner, project_id, "A2")
    generate(client, owner, project_id)
    candidate = list_candidates(client, owner, project_id)[0]
    denied_create = client.post(f"/api/projects/{project_id}/pottery/sherds", json=sherd_payload("A3"), headers=viewer["headers"])
    assert denied_create.status_code == 403
    denied_confirm = confirm(client, {"headers": recorder["headers"]}, project_id, candidate["id"])
    assert denied_confirm.status_code == 403
    allowed_read = client.get(f"/api/projects/{project_id}/pottery/sherds", headers=viewer["headers"])
    assert allowed_read.status_code == 200
