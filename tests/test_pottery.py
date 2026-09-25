from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.pottery.rules import evaluate_pair, normalize_config


def rule_config(**overrides):
    config = {
        "weights": {"part": 1, "fabric": 1, "thickness": 1, "decoration": 1, "diameter": 1, "context": 1},
        "threshold": 0.5,
        "part_compatible": [["rim", "body"], ["body", "base"], ["rim", "base"]],
        "fabric_groups": [["灰陶", "灰黑陶"], ["红陶", "红褐陶"]],
        "thickness_tolerance_mm": 1.0,
        "decoration_min_shared": 0,
        "diameter_tolerance_mm": 20.0,
        "context_forbid": [],
        "context_forbid_same": False,
    }
    config.update(overrides)
    return config


def eval_sherd(**overrides):
    base = {
        "part": "body",
        "fabric_color": "灰陶",
        "thickness_min": 4.0,
        "thickness_max": 6.0,
        "decoration": [],
        "diameter_mm": None,
        "context_code": "H1",
    }
    base.update(overrides)
    return base


def make_project(client, owner, code="POT"):
    response = client.post("/api/projects", json={"code": code, "name": "陶器拼合研究", "site_name": "某遗址"}, headers=owner["headers"])
    assert response.status_code == 201
    return response.json()["id"]


def add_rule_set(client, owner, project_id, config=None, name="拼合规则"):
    response = client.post(
        f"/api/projects/{project_id}/pottery/rule-sets",
        json={"name": name, "config": config if config is not None else rule_config()},
        headers=owner["headers"],
    )
    assert response.status_code == 201, response.json()
    return response.json()


def add_sherd(client, owner, project_id, **overrides):
    payload = {
        "sherd_code": "S1",
        "part": "body",
        "fabric_color": "灰陶",
        "thickness_min": 4.0,
        "thickness_max": 6.0,
        "context_code": "H1",
    }
    payload.update(overrides)
    response = client.post(f"/api/projects/{project_id}/pottery/sherds", json=payload, headers=owner["headers"])
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


def confirm(client, owner, project_id, group_id, rationale="胎色、厚度与纹饰一致，可拼", vessel_code=None):
    payload = {"rationale": rationale}
    if vessel_code:
        payload["vessel_code"] = vessel_code
    return client.post(f"/api/projects/{project_id}/pottery/candidates/{group_id}/confirm", json=payload, headers=owner["headers"])


# ---------- 区间临界值 ----------


def test_thickness_and_diameter_boundary_values():
    config = normalize_config({"thickness_tolerance_mm": 1.0, "diameter_tolerance_mm": 20.0, "threshold": 0.0})
    lower = eval_sherd(thickness_min=5.0, thickness_max=7.0)
    touching = eval_sherd(thickness_min=7.0, thickness_max=9.0)
    at_tolerance = eval_sherd(thickness_min=8.0, thickness_max=9.0)
    beyond = eval_sherd(thickness_min=8.1, thickness_max=9.0)
    assert evaluate_pair(config, lower, touching)["dimensions"]["thickness"]["gap_mm"] == 0.0
    assert evaluate_pair(config, lower, touching)["compatible"] is True
    boundary = evaluate_pair(config, lower, at_tolerance)
    assert boundary["dimensions"]["thickness"]["gap_mm"] == 1.0
    assert boundary["compatible"] is True
    outside = evaluate_pair(config, lower, beyond)
    assert outside["compatible"] is False
    assert "thickness_incompatible" in outside["hard_failures"]

    d1 = eval_sherd(diameter_mm=100.0)
    d2 = eval_sherd(diameter_mm=120.0)
    d3 = eval_sherd(diameter_mm=120.1)
    assert evaluate_pair(config, d1, d2)["compatible"] is True
    diameter_fail = evaluate_pair(config, d1, d3)
    assert diameter_fail["compatible"] is False
    assert "diameter_incompatible" in diameter_fail["hard_failures"]


# ---------- 传递相似但直接不兼容 ----------


def test_transitive_similarity_does_not_group_incompatible(client, owner):
    project_id = make_project(client, owner, "TRANS")
    add_rule_set(client, owner, project_id)
    add_sherd(client, owner, project_id, sherd_code="A1", thickness_min=4.0, thickness_max=6.0)
    add_sherd(client, owner, project_id, sherd_code="B1", thickness_min=6.5, thickness_max=7.5)
    add_sherd(client, owner, project_id, sherd_code="C1", thickness_min=8.0, thickness_max=9.0)
    generate(client, owner, project_id)
    groups = list_candidates(client, owner, project_id)
    assert len(groups) == 1
    member_codes = set(groups[0]["member_codes"])
    assert member_codes == {"A1", "B1"}
    assert not {"A1", "C1"} <= member_codes
    detail = client.get(f"/api/projects/{project_id}/pottery/candidates/{groups[0]['id']}", headers=owner["headers"]).json()
    pair = detail["pair_scores"]["A1~B1"]
    assert pair["dimensions"]["thickness"]["gap_mm"] == 0.5
    assert set(pair["dimensions"]) == {"part", "fabric", "thickness", "decoration", "diameter", "context"}


# ---------- 复核：确认、拒绝、依据 ----------


def test_confirm_requires_rationale(client, owner):
    project_id = make_project(client, owner, "RAT1")
    add_rule_set(client, owner, project_id)
    add_sherd(client, owner, project_id, sherd_code="A1")
    add_sherd(client, owner, project_id, sherd_code="A2")
    generate(client, owner, project_id)
    group_id = list_candidates(client, owner, project_id)[0]["id"]
    missing = client.post(f"/api/projects/{project_id}/pottery/candidates/{group_id}/confirm", json={}, headers=owner["headers"])
    assert missing.status_code == 422
    blank = confirm(client, owner, project_id, group_id, rationale="   ")
    assert blank.status_code == 422


def test_confirm_creates_vessel_and_enforces_member_uniqueness(client, owner):
    project_id = make_project(client, owner, "CONF1")
    rule = add_rule_set(client, owner, project_id)
    add_sherd(client, owner, project_id, sherd_code="A1")
    add_sherd(client, owner, project_id, sherd_code="A2")
    add_sherd(client, owner, project_id, sherd_code="A3", fabric_color="红陶")
    generate(client, owner, project_id)
    group = list_candidates(client, owner, project_id)[0]
    created = confirm(client, owner, project_id, group["id"])
    assert created.status_code == 200, created.json()
    vessel = created.json()
    assert vessel["status"] == "confirmed"
    assert sorted(vessel["member_codes"]) == ["A1", "A2"]
    assert vessel["rationale"]
    assert vessel["review_events"][0]["action"] == "confirm"

    again = confirm(client, owner, project_id, group["id"])
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "candidate_not_pending"

    # 模拟历史遗留候选：包含已确认陶片 A1 与游离陶片 A3，确认时必须被成员唯一性拦截
    from app.database import connection, now

    db = connection()
    stamp = now()
    cursor = db.execute(
        "INSERT INTO pottery_candidate_groups(project_id,rule_set_id,group_key,status,score,scores_json,created_at,updated_at) VALUES(?,?,?,'pending',0.9,'{}',?,?)",
        (project_id, rule["id"], "GSTALE1", stamp, stamp),
    )
    stale_id = cursor.lastrowid
    sherd_rows = db.execute("SELECT id, code_norm FROM pottery_sherds WHERE project_id=?", (project_id,)).fetchall()
    ids = {row["code_norm"]: row["id"] for row in sherd_rows}
    db.execute("INSERT INTO pottery_candidate_members(group_id,sherd_id) VALUES(?,?)", (stale_id, ids["A1"]))
    db.execute("INSERT INTO pottery_candidate_members(group_id,sherd_id) VALUES(?,?)", (stale_id, ids["A3"]))
    conflict = confirm(client, owner, project_id, stale_id)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "member_conflict"


def test_reject_candidate_keeps_record(client, owner):
    project_id = make_project(client, owner, "REJ1")
    add_rule_set(client, owner, project_id)
    add_sherd(client, owner, project_id, sherd_code="A1")
    add_sherd(client, owner, project_id, sherd_code="A2")
    generate(client, owner, project_id)
    group_id = list_candidates(client, owner, project_id)[0]["id"]
    response = client.post(
        f"/api/projects/{project_id}/pottery/candidates/{group_id}/reject",
        json={"rationale": "胎色虽近但断面不连续"},
        headers=owner["headers"],
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    generate(client, owner, project_id)
    groups = list_candidates(client, owner, project_id)
    assert len(groups) == 1 and groups[0]["status"] == "rejected"
    detail = client.get(f"/api/projects/{project_id}/pottery/candidates/{group_id}", headers=owner["headers"]).json()
    assert detail["review_events"][0]["rationale"] == "胎色虽近但断面不连续"


def test_confirm_checks_context_forbidden_with_latest_data(client, owner):
    project_id = make_project(client, owner, "CTX1")
    add_rule_set(client, owner, project_id, config=rule_config(context_forbid=[["H1", "H9"]]))
    add_sherd(client, owner, project_id, sherd_code="A1", context_code="H1")
    sherd_b = add_sherd(client, owner, project_id, sherd_code="B1", context_code="H2")
    generate(client, owner, project_id)
    group_id = list_candidates(client, owner, project_id)[0]["id"]

    update = client.put(
        f"/api/projects/{project_id}/pottery/sherds/{sherd_b['id']}",
        json={"context_code": "H9"},
        headers=owner["headers"],
    )
    assert update.status_code == 200
    blocked = confirm(client, owner, project_id, group_id)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "context_forbidden"
    # 回滚验证：候选仍为待复核，未产生器物
    assert list_candidates(client, owner, project_id, status="pending")
    vessels = client.get(f"/api/projects/{project_id}/pottery/vessels", headers=owner["headers"]).json()["data"]
    assert vessels == []

    client.put(f"/api/projects/{project_id}/pottery/sherds/{sherd_b['id']}", json={"context_code": "H2"}, headers=owner["headers"])
    assert confirm(client, owner, project_id, group_id).status_code == 200


def test_confirm_requires_current_rule_version(client, owner):
    project_id = make_project(client, owner, "VER1")
    add_rule_set(client, owner, project_id, name="规则v1")
    add_sherd(client, owner, project_id, sherd_code="A1")
    add_sherd(client, owner, project_id, sherd_code="A2")
    generate(client, owner, project_id)
    group_id = list_candidates(client, owner, project_id)[0]["id"]
    add_rule_set(client, owner, project_id, name="规则v2")
    stale = confirm(client, owner, project_id, group_id)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "rule_version_stale"
    generate(client, owner, project_id)
    fresh = list_candidates(client, owner, project_id, status="pending")
    assert confirm(client, owner, project_id, fresh[0]["id"]).status_code == 200


def test_invalid_rule_config_rejected(client, owner):
    project_id = make_project(client, owner, "CFG1")
    unknown = client.post(f"/api/projects/{project_id}/pottery/rule-sets", json={"name": "x", "config": {"bogus": 1}}, headers=owner["headers"])
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "invalid_rule_config"
    bad_weight = client.post(
        f"/api/projects/{project_id}/pottery/rule-sets",
        json={"name": "x", "config": {"weights": {"part": -1}}},
        headers=owner["headers"],
    )
    assert bad_weight.status_code == 422


def test_confirmed_sherd_is_locked(client, owner):
    project_id = make_project(client, owner, "LOCK1")
    add_rule_set(client, owner, project_id)
    sherd = add_sherd(client, owner, project_id, sherd_code="A1")
    add_sherd(client, owner, project_id, sherd_code="A2")
    generate(client, owner, project_id)
    group_id = list_candidates(client, owner, project_id)[0]["id"]
    assert confirm(client, owner, project_id, group_id).status_code == 200
    locked = client.put(f"/api/projects/{project_id}/pottery/sherds/{sherd['id']}", json={"context_code": "H9"}, headers=owner["headers"])
    assert locked.status_code == 409
    assert locked.json()["error"]["code"] == "sherd_locked"


# ---------- 并发复核 ----------


def test_concurrent_confirm_only_one_wins(client, owner):
    project_id = make_project(client, owner, "RACE1")
    add_rule_set(client, owner, project_id)
    add_sherd(client, owner, project_id, sherd_code="A1")
    add_sherd(client, owner, project_id, sherd_code="A2")
    generate(client, owner, project_id)
    group_id = list_candidates(client, owner, project_id)[0]["id"]

    def attempt(index):
        return confirm(client, owner, project_id, group_id, rationale=f"并发依据{index}", vessel_code=f"V{index}").status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert results.count(200) == 1
    assert results.count(409) == 3
    vessels = client.get(f"/api/projects/{project_id}/pottery/vessels", headers=owner["headers"]).json()["data"]
    assert len(vessels) == 1
    detail = client.get(f"/api/projects/{project_id}/pottery/vessels/{vessels[0]['id']}", headers=owner["headers"]).json()
    assert len(detail["review_events"]) == 1


def test_concurrent_confirm_of_disjoint_groups_both_succeed(client, owner):
    project_id = make_project(client, owner, "RACE2")
    add_rule_set(client, owner, project_id)
    add_sherd(client, owner, project_id, sherd_code="A1")
    add_sherd(client, owner, project_id, sherd_code="A2")
    add_sherd(client, owner, project_id, sherd_code="B1", fabric_color="红陶")
    add_sherd(client, owner, project_id, sherd_code="B2", fabric_color="红陶")
    generate(client, owner, project_id)
    groups = list_candidates(client, owner, project_id)
    assert len(groups) == 2

    def attempt(item):
        index, group = item
        return confirm(client, owner, project_id, group["id"], rationale=f"依据{index}").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, enumerate(groups)))
    assert results == [200, 200]
    vessels = client.get(f"/api/projects/{project_id}/pottery/vessels", headers=owner["headers"]).json()["data"]
    assert len(vessels) == 2


# ---------- 拆组与合组、失败回滚 ----------


def _two_confirmed_vessels(client, owner, project_id, context_a="H1", context_b="H1"):
    add_sherd(client, owner, project_id, sherd_code="A1", context_code=context_a)
    add_sherd(client, owner, project_id, sherd_code="A2", context_code=context_a)
    add_sherd(client, owner, project_id, sherd_code="B1", fabric_color="红陶", context_code=context_b)
    add_sherd(client, owner, project_id, sherd_code="B2", fabric_color="红陶", context_code=context_b)
    generate(client, owner, project_id)
    vessels = []
    for group in list_candidates(client, owner, project_id, status="pending"):
        response = confirm(client, owner, project_id, group["id"])
        assert response.status_code == 200, response.json()
        vessels.append(response.json())
    return vessels


def test_split_vessel_frees_members_for_regeneration(client, owner):
    project_id = make_project(client, owner, "SPLIT1")
    add_rule_set(client, owner, project_id)
    vessels = _two_confirmed_vessels(client, owner, project_id)
    target = vessels[0]
    response = client.post(
        f"/api/projects/{project_id}/pottery/vessels/{target['id']}/split",
        json={"rationale": "复查发现断面不连续，拆组"},
        headers=owner["headers"],
    )
    assert response.status_code == 200
    assert response.json()["status"] == "dissolved"
    assert response.json()["review_events"][-1]["action"] == "split"
    generate(client, owner, project_id)
    pending = list_candidates(client, owner, project_id, status="pending")
    assert sorted(pending[0]["member_codes"]) == target["member_codes"]


def test_merge_vessels_combines_members(client, owner):
    project_id = make_project(client, owner, "MERGE1")
    add_rule_set(client, owner, project_id)
    vessels = _two_confirmed_vessels(client, owner, project_id)
    response = client.post(
        f"/api/projects/{project_id}/pottery/vessels/merge",
        json={"vessel_ids": [vessels[0]["id"], vessels[1]["id"]], "rationale": "两器物实为同一个体"},
        headers=owner["headers"],
    )
    assert response.status_code == 200, response.json()
    merged = response.json()
    assert sorted(merged["member_codes"]) == ["A1", "A2", "B1", "B2"]
    assert merged["review_events"][0]["action"] == "merge"
    old = client.get(f"/api/projects/{project_id}/pottery/vessels/{vessels[0]['id']}", headers=owner["headers"]).json()
    assert old["status"] == "dissolved"


def test_merge_context_forbidden_rolls_back(client, owner):
    project_id = make_project(client, owner, "MROLL1")
    add_rule_set(client, owner, project_id, config=rule_config(context_forbid=[["H1", "H9"]]))
    vessels = _two_confirmed_vessels(client, owner, project_id, context_a="H1", context_b="H9")
    response = client.post(
        f"/api/projects/{project_id}/pottery/vessels/merge",
        json={"vessel_ids": [vessels[0]["id"], vessels[1]["id"]], "rationale": "尝试合组"},
        headers=owner["headers"],
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "context_forbidden"
    for vessel in vessels:
        current = client.get(f"/api/projects/{project_id}/pottery/vessels/{vessel['id']}", headers=owner["headers"]).json()
        assert current["status"] == "confirmed"
        assert len(current["members"]) == 2
        assert current["review_events"][-1]["action"] == "confirm"


def test_merge_failure_mid_transaction_rolls_back(client, owner):
    project_id = make_project(client, owner, "MROLL2")
    add_rule_set(client, owner, project_id)
    vessels = _two_confirmed_vessels(client, owner, project_id)
    response = client.post(
        f"/api/projects/{project_id}/pottery/vessels/merge",
        json={"vessel_ids": [vessels[0]["id"], vessels[1]["id"]], "rationale": "编号冲突触发回滚", "vessel_code": vessels[0]["vessel_code"]},
        headers=owner["headers"],
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "vessel_code_exists"
    # 中段失败（先释放成员后插入冲突编号）必须整体回滚
    from app.database import connection

    db = connection()
    members = db.execute("SELECT COUNT(*) AS c FROM pottery_vessel_members").fetchone()["c"]
    events = db.execute("SELECT COUNT(*) AS c FROM pottery_review_events WHERE action='merge'").fetchone()["c"]
    assert members == 4
    assert events == 0
    for vessel in vessels:
        current = client.get(f"/api/projects/{project_id}/pottery/vessels/{vessel['id']}", headers=owner["headers"]).json()
        assert current["status"] == "confirmed"
        assert len(current["members"]) == 2


# ---------- 统计：数值、贡献者、版本与差异 ----------


def _stats_fixture(client, owner, project_id):
    add_rule_set(client, owner, project_id)
    add_sherd(client, owner, project_id, sherd_code="R1", part="rim", rim_percent=60.0, typology_code="A")
    add_sherd(client, owner, project_id, sherd_code="R2", part="rim", rim_percent=50.0, typology_code="A")
    add_sherd(client, owner, project_id, sherd_code="B1", fabric_color="红陶", typology_code="A")
    add_sherd(client, owner, project_id, sherd_code="R3", part="rim", rim_percent=50.0, typology_code="A", thickness_min=9.0, thickness_max=10.0, context_code="H2")
    add_sherd(client, owner, project_id, sherd_code="X1", fabric_color="红陶", thickness_min=9.0, thickness_max=10.0, typology_code="B")
    generate(client, owner, project_id)


def test_stats_values_and_contributors(client, owner):
    project_id = make_project(client, owner, "STAT1")
    _stats_fixture(client, owner, project_id)
    group = list_candidates(client, owner, project_id, status="pending")[0]
    assert sorted(group["member_codes"]) == ["R1", "R2"]
    vessel = confirm(client, owner, project_id, group["id"]).json()

    run = client.post(f"/api/projects/{project_id}/pottery/stats/run", json={"plan_code": "P1"}, headers=owner["headers"])
    assert run.status_code == 200, run.json()
    result = run.json()["result"]
    mni = result["mni"]
    assert mni["by_type"]["A"]["value"] == 3
    assert mni["by_type"]["B"]["value"] == 1
    assert mni["total"] == 4
    assert mni["by_type"]["A"]["vessels"] == [{"id": vessel["id"], "code": vessel["vessel_code"]}]
    single_codes = sorted(item["code"] for item in mni["by_type"]["A"]["single_sherds"])
    assert single_codes == ["B1", "R3"]

    eve = result["rim_eve"]["by_type"]
    assert eve["A"]["value"] == 1.5
    assert eve["B"]["value"] == 0.0
    contributors = {(item["kind"], item["id"]): item["value"] for item in eve["A"]["contributors"]}
    assert contributors[("vessel", vessel["id"])] == 1.0
    r3 = next(item for item in eve["A"]["contributors"] if item["kind"] == "sherd")
    assert r3["value"] == 0.5 and r3["rim_percent"] == 50.0

    freq = result["type_frequency"]["by_type"]
    assert freq["A"] == {"sherds": 4, "vessels": 1, "sherd_ids": freq["A"]["sherd_ids"], "vessel_ids": [vessel["id"]]}
    assert freq["B"]["sherds"] == 1 and freq["B"]["vessels"] == 0


def test_stats_versioning_idempotency_and_diff(client, owner):
    project_id = make_project(client, owner, "STAT2")
    _stats_fixture(client, owner, project_id)

    first = client.post(f"/api/projects/{project_id}/pottery/stats/run", json={"plan_code": "P1"}, headers=owner["headers"]).json()
    assert first["created"] is True and first["version"] == 1
    assert first["result"]["rim_eve"]["by_type"]["A"]["value"] == 1.6

    again = client.post(f"/api/projects/{project_id}/pottery/stats/run", json={"plan_code": "P1"}, headers=owner["headers"]).json()
    assert again["created"] is False
    assert again["id"] == first["id"] and again["input_hash"] == first["input_hash"]
    assert again["result"] == first["result"]

    group = list_candidates(client, owner, project_id, status="pending")[0]
    vessel = confirm(client, owner, project_id, group["id"]).json()
    second = client.post(f"/api/projects/{project_id}/pottery/stats/run", json={"plan_code": "P1"}, headers=owner["headers"]).json()
    assert second["created"] is True and second["version"] == 2
    assert second["result"]["rim_eve"]["by_type"]["A"]["value"] == 1.5

    diff = client.get(
        f"/api/projects/{project_id}/pottery/stats/diff",
        params={"plan_code": "P1", "from_version": 1, "to_version": 2},
        headers=owner["headers"],
    ).json()
    eve_change = diff["changes"]["rim_eve"]["A"]
    assert eve_change["from"] == 1.6 and eve_change["to"] == 1.5
    assert eve_change["delta"] == pytest.approx(-0.1)
    assert eve_change["added_contributors"] == [{"kind": "vessel", "id": vessel["id"]}]
    removed = sorted(item["id"] for item in eve_change["removed_contributors"])
    r1 = next(s for s in vessel["members"] if s["code_norm"] == "R1")
    r2 = next(s for s in vessel["members"] if s["code_norm"] == "R2")
    assert removed == sorted([r1["id"], r2["id"]])
    freq_change = diff["changes"]["type_frequency"]["A"]
    assert freq_change["vessels"] == {"from": 0, "to": 1}
    mni_change = diff["changes"]["mni"]["A"]
    assert mni_change["delta"] == 0
    assert mni_change["added_vessels"] == [vessel["id"]]
    assert mni_change["removed_sherds"] == sorted([r1["id"], r2["id"]])


def test_stats_deterministic_for_same_input(client, owner):
    project_id = make_project(client, owner, "STAT3")
    _stats_fixture(client, owner, project_id)
    first_gen = generate(client, owner, project_id)
    second_gen = generate(client, owner, project_id)
    assert first_gen["groups"] == second_gen["groups"]
    assert second_gen["created"] == 0 and second_gen["removed"] == 0

    first = client.post(f"/api/projects/{project_id}/pottery/stats/run", json={"plan_code": "P1"}, headers=owner["headers"]).json()
    second = client.post(f"/api/projects/{project_id}/pottery/stats/run", json={"plan_code": "P1"}, headers=owner["headers"]).json()
    assert first["id"] == second["id"]
    assert first["result"] == second["result"]

    from app.pottery.stats import compute_result
    from app.security import stable_json

    config = normalize_config(rule_config())
    sherds = [
        {"id": 1, "code_norm": "R1", "part": "rim", "fabric_color": "灰陶", "thickness_min": 4.0, "thickness_max": 6.0, "decoration": [], "diameter_mm": None, "context_code": "H1", "typology_code": "A", "rim_percent": 30.0},
        {"id": 2, "code_norm": "R2", "part": "rim", "fabric_color": "灰陶", "thickness_min": 4.0, "thickness_max": 6.0, "decoration": [], "diameter_mm": None, "context_code": "H1", "typology_code": "A", "rim_percent": 20.0},
    ]
    plan = {"plan_code": "P", "contexts": None, "parts": None}
    assert stable_json(compute_result(config, sherds, [], plan)) == stable_json(compute_result(config, sherds, [], plan))


# ---------- 游标分页与稳定排序 ----------


def test_cursor_pagination_stable_order(client, owner):
    project_id = make_project(client, owner, "PAGE1")
    for index in range(25):
        add_sherd(client, owner, project_id, sherd_code=f"S{index:02d}")
    codes, cursor = [], ""
    while True:
        response = client.get(
            f"/api/projects/{project_id}/pottery/sherds",
            params={"limit": 10, "cursor": cursor},
            headers=owner["headers"],
        )
        assert response.status_code == 200, response.json()
        body = response.json()
        codes.extend(item["code_norm"] for item in body["data"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert codes == [f"S{index:02d}" for index in range(25)]
    assert len(set(codes)) == 25

    invalid = client.get(f"/api/projects/{project_id}/pottery/sherds", params={"cursor": "!!bad!!"}, headers=owner["headers"])
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_cursor"


# ---------- 离线导入 ----------


def test_import_triggers_candidates_and_stats(client, owner):
    project_id = make_project(client, owner, "IMP1")
    bundle = {
        "rule_set": {"name": "导入规则", "config": rule_config()},
        "sherds": [
            {"sherd_code": "I1", "part": "rim", "fabric_color": "灰陶", "thickness_min": 4.0, "thickness_max": 6.0, "context_code": "H1", "typology_code": "A", "rim_percent": 30.0},
            {"sherd_code": "I2", "part": "rim", "fabric_color": "灰陶", "thickness_min": 4.5, "thickness_max": 6.5, "context_code": "H1", "typology_code": "A", "rim_percent": 20.0},
            {"sherd_code": "I3", "part": "body", "fabric_color": "红陶", "thickness_min": 4.0, "thickness_max": 6.0, "context_code": "H2", "typology_code": "B"},
        ],
        "plan": {"plan_code": "IMP"},
    }
    response = client.post(f"/api/projects/{project_id}/pottery/import", json=bundle, headers=owner["headers"])
    assert response.status_code == 200, response.json()
    summary = response.json()
    assert summary["imported"] == 3
    assert summary["rule_set"]["version"] == 1
    assert summary["candidates"]["created"] == 1
    stats = summary["stats"]
    assert stats["version"] == 1
    assert stats["result"]["mni"]["total"] == 2
    assert stats["result"]["rim_eve"]["by_type"]["A"]["value"] == 0.5
    run_id = stats["id"]
    fetched = client.get(f"/api/projects/{project_id}/pottery/stats/runs/{run_id}", headers=owner["headers"])
    assert fetched.status_code == 200
    assert fetched.json()["result"]["mni"]["by_type"]["A"]["sherd_groups"][0][0]["code"] == "I1"


def test_import_duplicate_rolls_back_everything(client, owner):
    project_id = make_project(client, owner, "IMP2")
    sherd = {"sherd_code": "X1", "part": "rim", "fabric_color": "灰陶", "thickness_min": 4.0, "thickness_max": 6.0, "context_code": "H1"}
    bundle = {
        "rule_set": {"name": "导入规则", "config": rule_config()},
        "sherds": [sherd, {"sherd_code": "X2", "part": "body", "fabric_color": "灰陶", "thickness_min": 4.0, "thickness_max": 6.0, "context_code": "H1"}, dict(sherd)],
        "plan": {"plan_code": "IMP"},
    }
    response = client.post(f"/api/projects/{project_id}/pottery/import", json=bundle, headers=owner["headers"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "import_conflict"
    sherds = client.get(f"/api/projects/{project_id}/pottery/sherds", headers=owner["headers"]).json()["data"]
    rules = client.get(f"/api/projects/{project_id}/pottery/rule-sets", headers=owner["headers"]).json()["data"]
    assert sherds == []
    assert rules == []
