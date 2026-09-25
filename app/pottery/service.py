from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.database import connection, now, transaction
from app.pagination import paginate
from app.security import stable_json
from app.service import ResearchService, ServiceError
from app.pottery.matching import DEFAULT_RULES, build_candidate_groups, compile_rules
from app.pottery.stats import compute_metrics, diff_metrics

READ_ROLES = {"owner", "researcher", "recorder", "reviewer", "viewer"}
WRITE_ROLES = {"owner", "researcher", "recorder"}
REVIEW_ROLES = {"owner", "researcher", "reviewer"}

SHERD_SORTS = {
    "id": ("id", "id"),
    "code": ("sherd_code", "sherd_code"),
    "part": ("part", "part"),
    "type": ("vessel_type", "vessel_type"),
    "created": ("created_at", "created_at"),
}
CANDIDATE_SORTS = {"id": ("id", "id"), "status": ("status", "status"), "created": ("created_at", "created_at")}
VESSEL_SORTS = {"id": ("id", "id"), "code": ("vessel_code", "vessel_code"), "created": ("created_at", "created_at")}
CONTEXT_SORTS = {"id": ("id", "id"), "code": ("code", "code"), "created": ("created_at", "created_at")}
RULE_SET_SORTS = {"id": ("id", "id"), "version": ("version", "version"), "created": ("created_at", "created_at")}
REVIEW_SORTS = {"id": ("id", "id"), "created": ("created_at", "created_at")}
PLAN_SORTS = {"id": ("id", "id"), "code": ("code", "code"), "created": ("created_at", "created_at")}
REPORT_SORTS = {"id": ("id", "id"), "version": ("version", "version"), "created": ("created_at", "created_at")}
IMPORT_SORTS = {"id": ("id", "id"), "created": ("created_at", "created_at")}


def _require_rationale(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ServiceError("rationale_required", "复核操作必须填写依据", 422)
    return text


class PotteryService:
    def __init__(self, db: sqlite3.Connection | None = None):
        self.db = db or connection()
        self.foundation = ResearchService(self.db)

    def _role(self, project_id: int, user_id: int, allowed: set[str]) -> str:
        return self.foundation.require_role(project_id, user_id, allowed)

    def _audit(self, action: str, resource_type: str, resource_id: str, payload: dict[str, Any], project_id: int, actor_id: int) -> None:
        self.foundation.audit(action, resource_type, resource_id, payload, project_id=project_id, actor_id=actor_id)

    # ------------------------------------------------------------------ 出土上下文
    def create_context(self, project_id: int, actor_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self._role(project_id, actor_id, WRITE_ROLES)
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO pottery_contexts(project_id,code,kind,description,created_at) VALUES(?,?,?,?,?)",
                    (project_id, payload["code"].strip(), payload.get("kind") or "灰坑", payload.get("description") or "", stamp),
                )
                self._audit("pottery.context.create", "pottery_context", str(cursor.lastrowid), payload, project_id, actor_id)
                return dict(db.execute("SELECT * FROM pottery_contexts WHERE id=?", (cursor.lastrowid,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise ServiceError("context_exists", f"出土上下文已存在: {payload['code']}", 409) from exc

    def list_contexts(self, project_id: int, actor_id: int, *, cursor: str | None, limit: int, sort: str) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        return paginate(self.db, select="*", source="pottery_contexts", where="project_id=?", params=[project_id], sort=sort, sorts=CONTEXT_SORTS, cursor=cursor, limit=limit)

    def create_exclusion(self, project_id: int, actor_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self._role(project_id, actor_id, WRITE_ROLES)
        with transaction(immediate=True) as db:
            first = self._context_by_code(db, project_id, payload["context_a"])
            second = self._context_by_code(db, project_id, payload["context_b"])
            if first["id"] == second["id"]:
                raise ServiceError("exclusion_invalid", "禁配关系的两个上下文不能相同", 400)
            low, high = sorted([first["id"], second["id"]])
            try:
                cursor = db.execute(
                    "INSERT INTO pottery_context_exclusions(project_id,context_a_id,context_b_id,reason,created_at) VALUES(?,?,?,?,?)",
                    (project_id, low, high, payload["reason"], now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ServiceError("exclusion_exists", "该上下文禁配关系已存在", 409) from exc
            self._audit("pottery.context_exclusion.create", "pottery_context_exclusion", str(cursor.lastrowid), payload, project_id, actor_id)
            return dict(db.execute("SELECT * FROM pottery_context_exclusions WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_exclusions(self, project_id: int, actor_id: int) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        rows = self.db.execute(
            """SELECT e.*, ca.code AS context_a_code, cb.code AS context_b_code
               FROM pottery_context_exclusions e
               JOIN pottery_contexts ca ON ca.id=e.context_a_id
               JOIN pottery_contexts cb ON cb.id=e.context_b_id
               WHERE e.project_id=? ORDER BY e.id""",
            (project_id,),
        ).fetchall()
        return {"data": [dict(row) for row in rows]}

    def _context_by_code(self, db: sqlite3.Connection, project_id: int, code: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM pottery_contexts WHERE project_id=? AND code=?", (project_id, (code or "").strip())).fetchone()
        if row is None:
            raise ServiceError("context_not_found", f"出土上下文不存在: {code}", 400)
        return row

    def _exclusion_pairs(self, db: sqlite3.Connection, project_id: int) -> set[frozenset]:
        rows = db.execute("SELECT context_a_id, context_b_id FROM pottery_context_exclusions WHERE project_id=?", (project_id,)).fetchall()
        return {frozenset((row["context_a_id"], row["context_b_id"])) for row in rows}

    # ------------------------------------------------------------------ 陶片
    def create_sherd(self, project_id: int, actor_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self._role(project_id, actor_id, WRITE_ROLES)
        with transaction(immediate=True) as db:
            row = self._insert_sherd(db, project_id, actor_id, payload)
            self._audit("pottery.sherd.create", "pottery_sherd", str(row["id"]), payload, project_id, actor_id)
            return dict(row)

    def _insert_sherd(self, db: sqlite3.Connection, project_id: int, actor_id: int, data: dict[str, Any], import_id: int | None = None) -> sqlite3.Row:
        if float(data["thickness_min_mm"]) > float(data["thickness_max_mm"]):
            raise ServiceError("invalid_thickness", "厚度区间下界不能大于上界", 400)
        context_id = None
        context_code = (data.get("context_code") or "").strip()
        if context_code:
            context_id = self._context_by_code(db, project_id, context_code)["id"]
        stamp = now()
        try:
            cursor = db.execute(
                """INSERT INTO pottery_sherds(project_id,sherd_code,part,fabric_color,thickness_min_mm,thickness_max_mm,
                   decoration_code,length_mm,width_mm,rim_arc_percent,vessel_type,context_id,import_id,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    project_id,
                    data["sherd_code"].strip(),
                    data["part"],
                    data["fabric_color"].strip(),
                    float(data["thickness_min_mm"]),
                    float(data["thickness_max_mm"]),
                    (data.get("decoration_code") or "").strip(),
                    data.get("length_mm"),
                    data.get("width_mm"),
                    float(data.get("rim_arc_percent") or 0),
                    (data.get("vessel_type") or "").strip(),
                    context_id,
                    import_id,
                    actor_id,
                    stamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError("sherd_exists", f"陶片编号已存在: {data['sherd_code']}", 409) from exc
        return db.execute("SELECT * FROM pottery_sherds WHERE id=?", (cursor.lastrowid,)).fetchone()

    def get_sherd(self, project_id: int, actor_id: int, sherd_id: int) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        row = self.db.execute("SELECT * FROM pottery_sherds WHERE id=? AND project_id=?", (sherd_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("sherd_not_found", "陶片不存在", 404)
        return dict(row)

    def list_sherds(self, project_id: int, actor_id: int, *, cursor: str | None, limit: int, sort: str) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        return paginate(self.db, select="*", source="pottery_sherds", where="project_id=?", params=[project_id], sort=sort, sorts=SHERD_SORTS, cursor=cursor, limit=limit)

    # ------------------------------------------------------------------ 兼容规则集
    def create_rule_set(self, project_id: int, actor_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self._role(project_id, actor_id, WRITE_ROLES)
        try:
            compile_rules(payload["rules"])
        except (ValueError, KeyError) as exc:
            raise ServiceError("invalid_rules", f"兼容规则无效: {exc}", 400) from exc
        with transaction(immediate=True) as db:
            row = self._insert_rule_set(db, project_id, actor_id, payload["name"], payload["rules"])
            self._audit("pottery.rule_set.create", "pottery_rule_set", str(row["id"]), {"name": payload["name"], "version": row["version"]}, project_id, actor_id)
            reports = self._recompute_all_reports(db, project_id, actor_id)
            return {**dict(row), "reports": reports}

    def _insert_rule_set(self, db: sqlite3.Connection, project_id: int, actor_id: int, name: str, rules: dict[str, Any]) -> sqlite3.Row:
        latest = db.execute("SELECT MAX(version) AS v FROM pottery_rule_sets WHERE project_id=?", (project_id,)).fetchone()["v"] or 0
        db.execute("UPDATE pottery_rule_sets SET status='superseded' WHERE project_id=? AND status='active'", (project_id,))
        cursor = db.execute(
            "INSERT INTO pottery_rule_sets(project_id,version,name,rules_json,status,created_by,created_at) VALUES(?,?,?,?,'active',?,?)",
            (project_id, latest + 1, name, stable_json(rules), actor_id, now()),
        )
        return db.execute("SELECT * FROM pottery_rule_sets WHERE id=?", (cursor.lastrowid,)).fetchone()

    def _active_rule_set(self, db: sqlite3.Connection, project_id: int) -> sqlite3.Row | None:
        return db.execute("SELECT * FROM pottery_rule_sets WHERE project_id=? AND status='active' ORDER BY version DESC LIMIT 1", (project_id,)).fetchone()

    def _ensure_rule_set(self, db: sqlite3.Connection, project_id: int, actor_id: int) -> sqlite3.Row:
        row = self._active_rule_set(db, project_id)
        if row is None:
            row = self._insert_rule_set(db, project_id, actor_id, DEFAULT_RULES["name"], DEFAULT_RULES)
        return row

    def list_rule_sets(self, project_id: int, actor_id: int, *, cursor: str | None, limit: int, sort: str) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        page = paginate(self.db, select="*", source="pottery_rule_sets", where="project_id=?", params=[project_id], sort=sort, sorts=RULE_SET_SORTS, cursor=cursor, limit=limit)
        for item in page["data"]:
            item["rules"] = json.loads(item.pop("rules_json"))
        return page

    # ------------------------------------------------------------------ 候选组
    def generate_candidates(self, project_id: int, actor_id: int) -> dict[str, Any]:
        self._role(project_id, actor_id, WRITE_ROLES)
        with transaction(immediate=True) as db:
            return self._generate_candidates(db, project_id, actor_id)

    def _generate_candidates(self, db: sqlite3.Connection, project_id: int, actor_id: int) -> dict[str, Any]:
        rule_set = self._ensure_rule_set(db, project_id, actor_id)
        compiled = compile_rules(json.loads(rule_set["rules_json"]))
        exclusions = self._exclusion_pairs(db, project_id)
        rows = db.execute(
            """SELECT * FROM pottery_sherds WHERE project_id=? AND id NOT IN (
                 SELECT vm.sherd_id FROM pottery_vessel_members vm
                 JOIN pottery_vessels v ON v.id=vm.vessel_id
                 WHERE v.status='active' AND vm.removed_at='')
               ORDER BY sherd_code""",
            (project_id,),
        ).fetchall()
        groups = build_candidate_groups([dict(row) for row in rows], compiled, exclusions)
        existing = {
            row["group_key"]: row
            for row in db.execute("SELECT * FROM pottery_candidates WHERE project_id=? AND rule_set_id=?", (project_id, rule_set["id"])).fetchall()
        }
        new_keys = {group["group_key"] for group in groups}
        removed = 0
        for key, row in existing.items():
            if key not in new_keys and row["status"] == "pending":
                db.execute("DELETE FROM pottery_candidate_members WHERE candidate_id=?", (row["id"],))
                db.execute("DELETE FROM pottery_candidates WHERE id=?", (row["id"],))
                removed += 1
        # 已废弃规则版本遗留的待复核候选一并清理（已确认/已拒绝的保留为历史）
        for row in db.execute("SELECT id FROM pottery_candidates WHERE project_id=? AND status='pending' AND rule_set_id<>?", (project_id, rule_set["id"])).fetchall():
            db.execute("DELETE FROM pottery_candidate_members WHERE candidate_id=?", (row["id"],))
            db.execute("DELETE FROM pottery_candidates WHERE id=?", (row["id"],))
            removed += 1
        code_to_id = {row["sherd_code"]: row["id"] for row in rows}
        created = 0
        for group in groups:
            if group["group_key"] in existing:
                continue
            cursor = db.execute(
                "INSERT INTO pottery_candidates(project_id,rule_set_id,group_key,score,breakdown_json,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
                (project_id, rule_set["id"], group["group_key"], group["score"], stable_json(group["breakdown"]), now()),
            )
            candidate_id = cursor.lastrowid
            for member in group["members"]:
                db.execute(
                    "INSERT INTO pottery_candidate_members(candidate_id,sherd_id,item_score) VALUES(?,?,?)",
                    (candidate_id, code_to_id[member["sherd_code"]], member["item_score"]),
                )
            created += 1
        summary = {"rule_set_id": rule_set["id"], "rule_version": rule_set["version"], "created": created, "removed": removed, "total": len(groups)}
        self._audit("pottery.candidates.generate", "pottery_rule_set", str(rule_set["id"]), summary, project_id, actor_id)
        return summary

    def _candidate_view(self, db: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["breakdown"] = json.loads(data.pop("breakdown_json"))
        members = db.execute(
            """SELECT cm.sherd_id, cm.item_score, s.sherd_code FROM pottery_candidate_members cm
               JOIN pottery_sherds s ON s.id=cm.sherd_id WHERE cm.candidate_id=? ORDER BY s.sherd_code""",
            (row["id"],),
        ).fetchall()
        data["members"] = [dict(member) for member in members]
        return data

    def get_candidate(self, project_id: int, actor_id: int, candidate_id: int) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        row = self.db.execute("SELECT * FROM pottery_candidates WHERE id=? AND project_id=?", (candidate_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("candidate_not_found", "候选组不存在", 404)
        return self._candidate_view(self.db, dict(row))

    def list_candidates(self, project_id: int, actor_id: int, *, cursor: str | None, limit: int, sort: str, status: str | None) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        where, params = "project_id=?", [project_id]
        if status:
            where += " AND status=?"
            params.append(status)
        page = paginate(self.db, select="*", source="pottery_candidates", where=where, params=params, sort=sort, sorts=CANDIDATE_SORTS, cursor=cursor, limit=limit)
        page["data"] = [self._candidate_view(self.db, row) for row in page["data"]]
        return page

    # ------------------------------------------------------------------ 复核：确认 / 拒绝 / 拆组 / 合组
    def confirm_candidate(self, project_id: int, candidate_id: int, actor_id: int, rationale: str, expected_rule_set_id: int | None = None) -> dict[str, Any]:
        self._role(project_id, actor_id, REVIEW_ROLES)
        rationale = _require_rationale(rationale)
        with transaction(immediate=True) as db:
            candidate = db.execute("SELECT * FROM pottery_candidates WHERE id=? AND project_id=?", (candidate_id, project_id)).fetchone()
            if candidate is None:
                raise ServiceError("candidate_not_found", "候选组不存在", 404)
            if candidate["status"] != "pending":
                raise ServiceError("candidate_not_pending", "候选组已完成复核，不能重复确认", 409)
            active = self._active_rule_set(db, project_id)
            if active is None or active["id"] != candidate["rule_set_id"]:
                raise ServiceError("rule_version_conflict", "候选组基于的规则版本已不是当前版本，请重新生成候选", 409)
            if expected_rule_set_id is not None and expected_rule_set_id != candidate["rule_set_id"]:
                raise ServiceError("rule_version_conflict", "请求声明的规则版本与候选组不一致", 409)
            members = db.execute("SELECT sherd_id FROM pottery_candidate_members WHERE candidate_id=? ORDER BY sherd_id", (candidate_id,)).fetchall()
            sherd_ids = [member["sherd_id"] for member in members]
            self._assert_members_unassigned(db, sherd_ids)
            self._assert_context_compatible(db, project_id, sherd_ids)
            vessel = self._create_vessel(db, project_id, actor_id, candidate["rule_set_id"], "candidate", sherd_ids)
            db.execute("UPDATE pottery_candidates SET status='confirmed' WHERE id=?", (candidate_id,))
            self._review(db, project_id, actor_id, "confirm", rationale, candidate_id=candidate_id, vessel_id=vessel["id"], detail={"vessel_code": vessel["vessel_code"], "member_count": len(sherd_ids)})
            self._audit("pottery.candidate.confirm", "pottery_vessel", str(vessel["id"]), {"candidate_id": candidate_id, "rationale": rationale}, project_id, actor_id)
            reports = self._recompute_all_reports(db, project_id, actor_id)
            return {"vessel": self._vessel_view(db, vessel["id"]), "reports": reports}

    def reject_candidate(self, project_id: int, candidate_id: int, actor_id: int, rationale: str) -> dict[str, Any]:
        self._role(project_id, actor_id, REVIEW_ROLES)
        rationale = _require_rationale(rationale)
        with transaction(immediate=True) as db:
            candidate = db.execute("SELECT * FROM pottery_candidates WHERE id=? AND project_id=?", (candidate_id, project_id)).fetchone()
            if candidate is None:
                raise ServiceError("candidate_not_found", "候选组不存在", 404)
            if candidate["status"] != "pending":
                raise ServiceError("candidate_not_pending", "候选组已完成复核，不能重复拒绝", 409)
            db.execute("UPDATE pottery_candidates SET status='rejected' WHERE id=?", (candidate_id,))
            self._review(db, project_id, actor_id, "reject", rationale, candidate_id=candidate_id, detail={"group_key": candidate["group_key"]})
            self._audit("pottery.candidate.reject", "pottery_candidate", str(candidate_id), {"rationale": rationale}, project_id, actor_id)
            return self._candidate_view(db, dict(db.execute("SELECT * FROM pottery_candidates WHERE id=?", (candidate_id,)).fetchone()))

    def split_vessel(self, project_id: int, vessel_id: int, actor_id: int, groups: list[list[str]], rationale: str) -> dict[str, Any]:
        self._role(project_id, actor_id, REVIEW_ROLES)
        rationale = _require_rationale(rationale)
        with transaction(immediate=True) as db:
            vessel = self._active_vessel(db, project_id, vessel_id)
            members = db.execute(
                """SELECT s.id, s.sherd_code FROM pottery_vessel_members vm JOIN pottery_sherds s ON s.id=vm.sherd_id
                   WHERE vm.vessel_id=? AND vm.removed_at='' ORDER BY s.sherd_code""",
                (vessel_id,),
            ).fetchall()
            code_to_id = {row["sherd_code"]: row["id"] for row in members}
            flat = [code for group in groups for code in group]
            if len(groups) < 2 or any(not group for group in groups) or len(flat) != len(set(flat)) or set(flat) != set(code_to_id):
                raise ServiceError("split_invalid", "拆组方案必须完整且不重复地划分器物现有成员", 400)
            group_ids = [[code_to_id[code] for code in group] for group in groups]
            for sherd_ids in group_ids:
                self._assert_context_compatible(db, project_id, sherd_ids)
            self._dissolve_vessel(db, vessel_id)
            vessels = [self._create_vessel(db, project_id, actor_id, vessel["rule_set_id"], "split", sherd_ids) for sherd_ids in group_ids]
            self._review(db, project_id, actor_id, "split", rationale, vessel_id=vessel_id, detail={"from": vessel["vessel_code"], "groups": groups, "new_vessels": [v["vessel_code"] for v in vessels]})
            self._audit("pottery.vessel.split", "pottery_vessel", str(vessel_id), {"groups": groups, "rationale": rationale}, project_id, actor_id)
            reports = self._recompute_all_reports(db, project_id, actor_id)
            return {"vessels": [self._vessel_view(db, vessel["id"]) for vessel in vessels], "reports": reports}

    def merge_vessels(self, project_id: int, actor_id: int, vessel_ids: list[int], rationale: str) -> dict[str, Any]:
        self._role(project_id, actor_id, REVIEW_ROLES)
        rationale = _require_rationale(rationale)
        if len(set(vessel_ids)) != len(vessel_ids) or len(vessel_ids) < 2:
            raise ServiceError("merge_invalid", "合组需要至少两个不同的器物", 400)
        with transaction(immediate=True) as db:
            vessels = [self._active_vessel(db, project_id, vessel_id) for vessel_id in vessel_ids]
            sherd_ids: list[int] = []
            for vessel in vessels:
                rows = db.execute("SELECT sherd_id FROM pottery_vessel_members WHERE vessel_id=? AND removed_at=''", (vessel["id"],)).fetchall()
                sherd_ids.extend(row["sherd_id"] for row in rows)
            self._assert_context_compatible(db, project_id, sherd_ids)
            active = self._active_rule_set(db, project_id)
            for vessel in vessels:
                self._dissolve_vessel(db, vessel["id"])
            merged = self._create_vessel(db, project_id, actor_id, active["id"] if active else None, "merge", sherd_ids)
            self._review(db, project_id, actor_id, "merge", rationale, vessel_id=merged["id"], detail={"from": [v["vessel_code"] for v in vessels], "merged": merged["vessel_code"]})
            self._audit("pottery.vessel.merge", "pottery_vessel", str(merged["id"]), {"from": vessel_ids, "rationale": rationale}, project_id, actor_id)
            reports = self._recompute_all_reports(db, project_id, actor_id)
            return {"vessel": self._vessel_view(db, merged["id"]), "reports": reports}

    def _active_vessel(self, db: sqlite3.Connection, project_id: int, vessel_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM pottery_vessels WHERE id=? AND project_id=?", (vessel_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("vessel_not_found", "器物不存在", 404)
        if row["status"] != "active":
            raise ServiceError("vessel_not_active", "器物已被拆组或合组，当前不可用", 409)
        return row

    def _assert_members_unassigned(self, db: sqlite3.Connection, sherd_ids: list[int]) -> None:
        if not sherd_ids:
            return
        marks = ",".join("?" for _ in sherd_ids)
        rows = db.execute(
            f"""SELECT s.sherd_code, v.vessel_code FROM pottery_vessel_members vm
                JOIN pottery_vessels v ON v.id=vm.vessel_id AND v.status='active'
                JOIN pottery_sherds s ON s.id=vm.sherd_id
                WHERE vm.removed_at='' AND vm.sherd_id IN ({marks}) ORDER BY s.sherd_code""",
            sherd_ids,
        ).fetchall()
        if rows:
            taken = ", ".join(f"{row['sherd_code']}→{row['vessel_code']}" for row in rows)
            raise ServiceError("member_conflict", f"陶片已被确认到其他器物: {taken}", 409)

    def _assert_context_compatible(self, db: sqlite3.Connection, project_id: int, sherd_ids: list[int]) -> None:
        if len(sherd_ids) < 2:
            return
        marks = ",".join("?" for _ in sherd_ids)
        rows = db.execute(f"SELECT id, sherd_code, context_id FROM pottery_sherds WHERE id IN ({marks}) ORDER BY sherd_code", sherd_ids).fetchall()
        exclusions = db.execute(
            """SELECT e.context_a_id, e.context_b_id, e.reason, ca.code AS a_code, cb.code AS b_code
               FROM pottery_context_exclusions e
               JOIN pottery_contexts ca ON ca.id=e.context_a_id
               JOIN pottery_contexts cb ON cb.id=e.context_b_id
               WHERE e.project_id=?""",
            (project_id,),
        ).fetchall()
        banned = {frozenset((row["context_a_id"], row["context_b_id"])): row for row in exclusions}
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                first, second = rows[i], rows[j]
                if not first["context_id"] or not second["context_id"] or first["context_id"] == second["context_id"]:
                    continue
                hit = banned.get(frozenset((first["context_id"], second["context_id"])))
                if hit:
                    raise ServiceError(
                        "context_excluded",
                        f"陶片 {first['sherd_code']} 与 {second['sherd_code']} 的出土上下文 {hit['a_code']}~{hit['b_code']} 存在禁配关系: {hit['reason']}",
                        409,
                    )

    def _create_vessel(self, db: sqlite3.Connection, project_id: int, actor_id: int, rule_set_id: int | None, source: str, sherd_ids: list[int]) -> dict[str, Any]:
        stamp = now()
        sequence = db.execute("SELECT COUNT(*) AS n FROM pottery_vessels WHERE project_id=?", (project_id,)).fetchone()["n"] + 1
        code = f"V{sequence:04d}"
        try:
            cursor = db.execute(
                "INSERT INTO pottery_vessels(project_id,vessel_code,rule_set_id,source,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (project_id, code, rule_set_id, source, actor_id, stamp),
            )
            vessel_id = cursor.lastrowid
            for sherd_id in sherd_ids:
                db.execute("INSERT INTO pottery_vessel_members(vessel_id,sherd_id,created_at) VALUES(?,?,?)", (vessel_id, sherd_id, stamp))
        except sqlite3.IntegrityError as exc:
            raise ServiceError("member_conflict", "陶片已被确认到其他器物，无法重复确认", 409) from exc
        return {"id": vessel_id, "vessel_code": code}

    def _dissolve_vessel(self, db: sqlite3.Connection, vessel_id: int) -> None:
        stamp = now()
        db.execute("UPDATE pottery_vessels SET status='dissolved', dissolved_at=? WHERE id=?", (stamp, vessel_id))
        db.execute("UPDATE pottery_vessel_members SET removed_at=? WHERE vessel_id=? AND removed_at=''", (stamp, vessel_id))

    def _review(self, db: sqlite3.Connection, project_id: int, actor_id: int, action: str, rationale: str, *, candidate_id: int | None = None, vessel_id: int | None = None, detail: dict[str, Any] | None = None) -> None:
        db.execute(
            "INSERT INTO pottery_reviews(project_id,actor_id,action,candidate_id,vessel_id,rationale,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (project_id, actor_id, action, candidate_id, vessel_id, rationale, stable_json(detail or {}), now()),
        )

    def _vessel_view(self, db: sqlite3.Connection, vessel_id: int) -> dict[str, Any]:
        vessel = dict(db.execute("SELECT * FROM pottery_vessels WHERE id=?", (vessel_id,)).fetchone())
        members = db.execute(
            """SELECT vm.sherd_id, vm.removed_at, s.sherd_code, s.part, s.vessel_type FROM pottery_vessel_members vm
               JOIN pottery_sherds s ON s.id=vm.sherd_id WHERE vm.vessel_id=? ORDER BY s.sherd_code""",
            (vessel_id,),
        ).fetchall()
        vessel["members"] = [dict(member) for member in members]
        return vessel

    def get_vessel(self, project_id: int, actor_id: int, vessel_id: int) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        row = self.db.execute("SELECT * FROM pottery_vessels WHERE id=? AND project_id=?", (vessel_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("vessel_not_found", "器物不存在", 404)
        return self._vessel_view(self.db, vessel_id)

    def list_vessels(self, project_id: int, actor_id: int, *, cursor: str | None, limit: int, sort: str, status: str | None) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        where, params = "project_id=?", [project_id]
        if status:
            where += " AND status=?"
            params.append(status)
        page = paginate(self.db, select="*", source="pottery_vessels", where=where, params=params, sort=sort, sorts=VESSEL_SORTS, cursor=cursor, limit=limit)
        page["data"] = [self._vessel_view(self.db, row["id"]) for row in page["data"]]
        return page

    def list_reviews(self, project_id: int, actor_id: int, *, cursor: str | None, limit: int, sort: str) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        page = paginate(self.db, select="*", source="pottery_reviews", where="project_id=?", params=[project_id], sort=sort, sorts=REVIEW_SORTS, cursor=cursor, limit=limit)
        for item in page["data"]:
            item["detail"] = json.loads(item.pop("detail_json"))
        return page

    # ------------------------------------------------------------------ 研究方案与统计报告
    def create_plan(self, project_id: int, actor_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self._role(project_id, actor_id, WRITE_ROLES)
        config = payload.get("config") or {}
        if not isinstance(config, dict):
            raise ServiceError("invalid_plan", "研究方案配置必须是对象", 400)
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO pottery_plans(project_id,code,name,config_json,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (project_id, payload["code"].strip(), payload["name"], stable_json(config), actor_id, stamp),
                )
                self._audit("pottery.plan.create", "pottery_plan", str(cursor.lastrowid), payload, project_id, actor_id)
                return dict(db.execute("SELECT * FROM pottery_plans WHERE id=?", (cursor.lastrowid,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise ServiceError("plan_exists", f"研究方案编码已存在: {payload['code']}", 409) from exc

    def _ensure_plan(self, db: sqlite3.Connection, project_id: int, actor_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM pottery_plans WHERE project_id=? AND code='default'", (project_id,)).fetchone()
        if row is None:
            cursor = db.execute(
                "INSERT INTO pottery_plans(project_id,code,name,config_json,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (project_id, "default", "默认研究方案", "{}", actor_id, now()),
            )
            row = db.execute("SELECT * FROM pottery_plans WHERE id=?", (cursor.lastrowid,)).fetchone()
        return row

    def list_plans(self, project_id: int, actor_id: int, *, cursor: str | None, limit: int, sort: str) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        page = paginate(self.db, select="*", source="pottery_plans", where="project_id=?", params=[project_id], sort=sort, sorts=PLAN_SORTS, cursor=cursor, limit=limit)
        for item in page["data"]:
            item["config"] = json.loads(item.pop("config_json"))
        return page

    def _plan(self, db: sqlite3.Connection, project_id: int, plan_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM pottery_plans WHERE id=? AND project_id=?", (plan_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("plan_not_found", "研究方案不存在", 404)
        return row

    def _snapshot_sherd(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"code": row["sherd_code"], "part": row["part"], "vessel_type": row["vessel_type"], "rim_arc_percent": row["rim_arc_percent"]}

    def _build_snapshot(self, db: sqlite3.Connection, project_id: int, plan: sqlite3.Row, rule_set: sqlite3.Row | None) -> dict[str, Any]:
        config = json.loads(plan["config_json"] or "{}")
        context_ids = config.get("context_ids")
        vessel_types = config.get("vessel_types")

        def in_scope(row: sqlite3.Row) -> bool:
            if context_ids is not None and row["context_id"] not in context_ids:
                return False
            if vessel_types is not None and row["vessel_type"] not in vessel_types:
                return False
            return True

        sherds = db.execute("SELECT * FROM pottery_sherds WHERE project_id=? ORDER BY sherd_code", (project_id,)).fetchall()
        assigned = {
            row["sherd_id"]
            for row in db.execute(
                """SELECT vm.sherd_id FROM pottery_vessel_members vm JOIN pottery_vessels v ON v.id=vm.vessel_id
                   WHERE v.project_id=? AND v.status='active' AND vm.removed_at=''""",
                (project_id,),
            ).fetchall()
        }
        by_id = {row["id"]: row for row in sherds}
        vessels = []
        for vessel in db.execute("SELECT * FROM pottery_vessels WHERE project_id=? AND status='active' ORDER BY vessel_code", (project_id,)).fetchall():
            member_rows = db.execute(
                "SELECT sherd_id FROM pottery_vessel_members WHERE vessel_id=? AND removed_at='' ORDER BY sherd_id", (vessel["id"],)
            ).fetchall()
            members = sorted((self._snapshot_sherd(by_id[m["sherd_id"]]) for m in member_rows if m["sherd_id"] in by_id and in_scope(by_id[m["sherd_id"]])), key=lambda s: s["code"])
            if members:
                vessels.append({"code": vessel["vessel_code"], "members": members})
        unassigned = [self._snapshot_sherd(row) for row in sherds if row["id"] not in assigned and in_scope(row)]
        return {
            "plan": {"code": plan["code"], "config": config},
            "rule_set_version": rule_set["version"] if rule_set else None,
            "vessels": vessels,
            "unassigned": unassigned,
        }

    def compute_report(self, project_id: int, plan_id: int, actor_id: int) -> dict[str, Any]:
        self._role(project_id, actor_id, WRITE_ROLES)
        with transaction(immediate=True) as db:
            self._plan(db, project_id, plan_id)
            return self._compute_report(db, project_id, plan_id, actor_id)

    def _compute_report(self, db: sqlite3.Connection, project_id: int, plan_id: int, actor_id: int) -> dict[str, Any]:
        plan = self._plan(db, project_id, plan_id)
        rule_set = self._active_rule_set(db, project_id)
        snapshot = self._build_snapshot(db, project_id, plan, rule_set)
        input_hash = hashlib.sha256(stable_json(snapshot).encode()).hexdigest()
        latest = db.execute("SELECT * FROM pottery_reports WHERE plan_id=? ORDER BY version DESC LIMIT 1", (plan_id,)).fetchone()
        if latest is not None and latest["input_hash"] == input_hash:
            return {"plan_code": plan["code"], "version": latest["version"], "created_new": False}
        result = {
            "plan_code": plan["code"],
            "rule_set_version": rule_set["version"] if rule_set else None,
            "scope": {"vessel_count": len(snapshot["vessels"]), "sherd_count": len(snapshot["unassigned"])},
            "metrics": compute_metrics(snapshot),
        }
        version = (latest["version"] if latest else 0) + 1
        db.execute(
            "INSERT INTO pottery_reports(plan_id,version,rule_set_id,input_hash,result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (plan_id, version, rule_set["id"] if rule_set else None, input_hash, stable_json(result), actor_id, now()),
        )
        self._audit("pottery.report.compute", "pottery_plan", str(plan_id), {"version": version}, project_id, actor_id)
        return {"plan_code": plan["code"], "version": version, "created_new": True}

    def _recompute_all_reports(self, db: sqlite3.Connection, project_id: int, actor_id: int) -> list[dict[str, Any]]:
        plans = db.execute("SELECT id FROM pottery_plans WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        return [self._compute_report(db, project_id, plan["id"], actor_id) for plan in plans]

    def list_reports(self, project_id: int, plan_id: int, actor_id: int, *, cursor: str | None, limit: int, sort: str) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        self._plan(self.db, project_id, plan_id)
        return paginate(self.db, select="id,plan_id,version,rule_set_id,input_hash,created_by,created_at", source="pottery_reports", where="plan_id=?", params=[plan_id], sort=sort, sorts=REPORT_SORTS, cursor=cursor, limit=limit)

    def get_report(self, project_id: int, plan_id: int, version: int, actor_id: int) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        self._plan(self.db, project_id, plan_id)
        row = self.db.execute("SELECT * FROM pottery_reports WHERE plan_id=? AND version=?", (plan_id, version)).fetchone()
        if row is None:
            raise ServiceError("report_not_found", "统计报告版本不存在", 404)
        data = dict(row)
        data["result"] = json.loads(data.pop("result_json"))
        return data

    def diff_reports(self, project_id: int, plan_id: int, version: int, other_version: int, actor_id: int) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        current = self.get_report(project_id, plan_id, version, actor_id)
        other = self.get_report(project_id, plan_id, other_version, actor_id)
        diff = diff_metrics(current["result"]["metrics"], other["result"]["metrics"])
        return {"plan_code": current["result"]["plan_code"], "from_version": version, "to_version": other_version, **diff}

    # ------------------------------------------------------------------ 离线导入
    def import_batch(self, project_id: int, actor_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self._role(project_id, actor_id, WRITE_ROLES)
        content = {"contexts": payload["contexts"], "exclusions": payload["exclusions"], "sherds": payload["sherds"]}
        file_hash = hashlib.sha256(stable_json(content).encode()).hexdigest()
        existing = self.db.execute("SELECT * FROM pottery_imports WHERE project_id=? AND batch_key=?", (project_id, payload["batch_key"])).fetchone()
        if existing is not None:
            if existing["file_hash"] != file_hash:
                raise ServiceError("import_conflict", "导入批次键已存在且文件内容不同", 409)
            return {"import": dict(existing), "idempotent": True, **json.loads(existing["stats_json"])}
        with transaction(immediate=True) as db:
            stamp = now()
            cursor = db.execute(
                "INSERT INTO pottery_imports(project_id,batch_key,file_name,file_hash,status,created_by,created_at) VALUES(?,?,?,?,'done',?,?)",
                (project_id, payload["batch_key"], payload.get("file_name") or "", file_hash, actor_id, stamp),
            )
            import_id = cursor.lastrowid
            for context in payload["contexts"]:
                db.execute(
                    "INSERT INTO pottery_contexts(project_id,code,kind,description,created_at) VALUES(?,?,?,?,?) ON CONFLICT(project_id,code) DO NOTHING",
                    (project_id, context["code"].strip(), context.get("kind") or "灰坑", context.get("description") or "", stamp),
                )
            for exclusion in payload["exclusions"]:
                first = self._context_by_code(db, project_id, exclusion["context_a"])
                second = self._context_by_code(db, project_id, exclusion["context_b"])
                if first["id"] == second["id"]:
                    raise ServiceError("exclusion_invalid", "禁配关系的两个上下文不能相同", 400)
                low, high = sorted([first["id"], second["id"]])
                db.execute(
                    "INSERT INTO pottery_context_exclusions(project_id,context_a_id,context_b_id,reason,created_at) VALUES(?,?,?,?,?) ON CONFLICT(project_id,context_a_id,context_b_id) DO NOTHING",
                    (project_id, low, high, exclusion["reason"], stamp),
                )
            for sherd in payload["sherds"]:
                self._insert_sherd(db, project_id, actor_id, sherd, import_id=import_id)
            candidates = self._generate_candidates(db, project_id, actor_id)
            self._ensure_plan(db, project_id, actor_id)
            reports = self._recompute_all_reports(db, project_id, actor_id)
            stats = {
                "sherd_count": len(payload["sherds"]),
                "context_count": len(payload["contexts"]),
                "candidates": candidates,
                "reports": reports,
            }
            db.execute("UPDATE pottery_imports SET stats_json=? WHERE id=?", (stable_json(stats), import_id))
            self._audit("pottery.import", "pottery_import", str(import_id), {"batch_key": payload["batch_key"], "sherd_count": len(payload["sherds"])}, project_id, actor_id)
            record = dict(db.execute("SELECT * FROM pottery_imports WHERE id=?", (import_id,)).fetchone())
            return {"import": record, "idempotent": False, **stats}

    def list_imports(self, project_id: int, actor_id: int, *, cursor: str | None, limit: int, sort: str) -> dict[str, Any]:
        self._role(project_id, actor_id, READ_ROLES)
        page = paginate(self.db, select="*", source="pottery_imports", where="project_id=?", params=[project_id], sort=sort, sorts=IMPORT_SORTS, cursor=cursor, limit=limit)
        for item in page["data"]:
            item["stats"] = json.loads(item.pop("stats_json"))
        return page
