"""陶片与器物分析模块的服务层。

跨表写入全部放在即时事务中，任何校验失败都会整体回滚；
确认关系强制成员唯一（数据库唯一索引 + 事务内校验）、上下文禁配与规则版本一致性。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.database import connection, now, transaction
from app.pottery.pagination import decode_cursor, encode_cursor
from app.pottery.rules import build_groups, context_forbidden, derive, group_key, normalize_config
from app.pottery.stats import compute_result, diff_results
from app.security import stable_json
from app.service import ResearchService, ServiceError

READ_ROLES = {"owner", "researcher", "recorder", "reviewer", "viewer"}
WRITE_ROLES = {"owner", "researcher", "recorder"}
REVIEW_ROLES = {"owner", "researcher", "reviewer"}
RULE_ROLES = {"owner", "researcher"}
STATS_ROLES = {"owner", "researcher"}


def _sherd_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["decoration_codes"] = json.loads(data.pop("decoration_json"))
    return data


def _eval_sherd(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": data["id"],
        "code_norm": data["code_norm"],
        "part": data["part"],
        "fabric_color": data["fabric_color"],
        "thickness_min": data["thickness_min"],
        "thickness_max": data["thickness_max"],
        "decoration": list(data["decoration_codes"]),
        "diameter_mm": data["diameter_mm"],
        "context_code": data["context_code"],
        "typology_code": data["typology_code"],
        "rim_percent": data["rim_percent"],
    }


def _normalize_code(code: str) -> str:
    return " ".join(code.split()).upper()


class PotteryService:
    def __init__(self, db: sqlite3.Connection | None = None):
        self.db = db or connection()
        self.base = ResearchService(self.db)

    # ---------- 规则版本 ----------

    def _rule_set_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["config"] = json.loads(data.pop("config_json"))
        return data

    def _active_rule_set(self, db: sqlite3.Connection, project_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM pottery_rule_sets WHERE project_id=? AND status='active'", (project_id,)).fetchone()
        if row is None:
            raise ServiceError("no_active_rule_set", "项目没有可用的兼容规则版本", 409)
        return row

    def _create_rule_set(self, db: sqlite3.Connection, project_id: int, actor_id: int, name: str, raw_config: dict[str, Any]) -> dict[str, Any]:
        try:
            config = normalize_config(raw_config)
        except ValueError as exc:
            raise ServiceError("invalid_rule_config", str(exc), 422) from exc
        row = db.execute("SELECT COALESCE(MAX(version),0) AS v FROM pottery_rule_sets WHERE project_id=?", (project_id,)).fetchone()
        version = row["v"] + 1
        db.execute("UPDATE pottery_rule_sets SET status='retired' WHERE project_id=? AND status='active'", (project_id,))
        cursor = db.execute(
            "INSERT INTO pottery_rule_sets(project_id,version,name,config_json,status,created_by,created_at) VALUES(?,?,?,?,'active',?,?)",
            (project_id, version, name, stable_json(config), actor_id, now()),
        )
        self.base.audit("pottery.rule_set.create", "pottery_rule_set", str(cursor.lastrowid), {"name": name, "version": version}, project_id=project_id, actor_id=actor_id)
        return self._rule_set_dict(db.execute("SELECT * FROM pottery_rule_sets WHERE id=?", (cursor.lastrowid,)).fetchone())

    def create_rule_set(self, project_id: int, actor_id: int, name: str, raw_config: dict[str, Any]) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, RULE_ROLES)
        with transaction(immediate=True) as db:
            return self._create_rule_set(db, project_id, actor_id, name, raw_config)

    def list_rule_sets(self, project_id: int, actor_id: int, cursor: str = "", limit: int = 50) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        key = decode_cursor(cursor, 1)
        where, params = "project_id=?", [project_id]
        if key:
            where += " AND version < ?"
            params.append(key[0])
        rows = self.db.execute(f"SELECT * FROM pottery_rule_sets WHERE {where} ORDER BY version DESC LIMIT ?", (*params, limit + 1)).fetchall()
        page = rows[:limit]
        next_cursor = encode_cursor([page[-1]["version"]]) if len(rows) > limit and page else None
        return {"data": [self._rule_set_dict(row) for row in page], "next_cursor": next_cursor}

    # ---------- 陶片 ----------

    def _insert_sherd(self, db: sqlite3.Connection, project_id: int, payload: dict[str, Any]) -> int:
        stamp = now()
        cursor = db.execute(
            "INSERT INTO pottery_sherds(project_id,sherd_code,code_norm,part,fabric_color,thickness_min,thickness_max,decoration_json,diameter_mm,length_mm,width_mm,context_code,context_layer,typology_code,rim_percent,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                project_id,
                payload["sherd_code"],
                _normalize_code(payload["sherd_code"]),
                payload["part"],
                payload["fabric_color"],
                payload["thickness_min"],
                payload["thickness_max"],
                stable_json(payload.get("decoration_codes") or []),
                payload.get("diameter_mm"),
                payload.get("length_mm"),
                payload.get("width_mm"),
                payload["context_code"],
                payload.get("context_layer") or "",
                payload.get("typology_code") or "",
                payload.get("rim_percent"),
                stamp,
                stamp,
            ),
        )
        return cursor.lastrowid

    def create_sherd(self, project_id: int, actor_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, WRITE_ROLES)
        try:
            with transaction(immediate=True) as db:
                sherd_id = self._insert_sherd(db, project_id, payload)
                self.base.audit("pottery.sherd.create", "pottery_sherd", str(sherd_id), payload, project_id=project_id, actor_id=actor_id)
                return _sherd_dict(db.execute("SELECT * FROM pottery_sherds WHERE id=?", (sherd_id,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise ServiceError("sherd_exists", "陶片编号已存在", 409) from exc

    def get_sherd(self, project_id: int, actor_id: int, sherd_id: int) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        row = self.db.execute("SELECT * FROM pottery_sherds WHERE id=? AND project_id=?", (sherd_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("sherd_not_found", "陶片不存在", 404)
        return _sherd_dict(row)

    def list_sherds(self, project_id: int, actor_id: int, cursor: str = "", limit: int = 50, part: str | None = None, context_code: str | None = None) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        key = decode_cursor(cursor, 2)
        where, params = "project_id=?", [project_id]
        if part:
            where += " AND part=?"
            params.append(part)
        if context_code:
            where += " AND context_code=?"
            params.append(context_code)
        if key:
            where += " AND (code_norm > ? OR (code_norm = ? AND id > ?))"
            params += [key[0], key[0], key[1]]
        rows = self.db.execute(f"SELECT * FROM pottery_sherds WHERE {where} ORDER BY code_norm, id LIMIT ?", (*params, limit + 1)).fetchall()
        page = rows[:limit]
        next_cursor = encode_cursor([page[-1]["code_norm"], page[-1]["id"]]) if len(rows) > limit and page else None
        return {"data": [_sherd_dict(row) for row in page], "next_cursor": next_cursor}

    def update_sherd(self, project_id: int, actor_id: int, sherd_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, WRITE_ROLES)
        with transaction(immediate=True) as db:
            row = db.execute("SELECT * FROM pottery_sherds WHERE id=? AND project_id=?", (sherd_id, project_id)).fetchone()
            if row is None:
                raise ServiceError("sherd_not_found", "陶片不存在", 404)
            if db.execute("SELECT 1 FROM pottery_vessel_members WHERE sherd_id=?", (sherd_id,)).fetchone():
                raise ServiceError("sherd_locked", "陶片已确认到器物，不能修改", 409)
            current = _sherd_dict(row)
            data = {key: value for key, value in patch.items() if value is not None}
            if not data:
                return current
            thickness_min = data.get("thickness_min", current["thickness_min"])
            thickness_max = data.get("thickness_max", current["thickness_max"])
            if thickness_min > thickness_max:
                raise ServiceError("invalid_thickness", "厚度区间下界不能大于上界", 422)
            sets, params = [], []
            for key, value in data.items():
                if key == "decoration_codes":
                    sets.append("decoration_json=?")
                    params.append(stable_json(value))
                else:
                    sets.append(f"{key}=?")
                    params.append(value)
            sets.append("updated_at=?")
            params.append(now())
            params.append(sherd_id)
            db.execute(f"UPDATE pottery_sherds SET {', '.join(sets)} WHERE id=?", params)
            self.base.audit("pottery.sherd.update", "pottery_sherd", str(sherd_id), data, project_id=project_id, actor_id=actor_id)
            return _sherd_dict(db.execute("SELECT * FROM pottery_sherds WHERE id=?", (sherd_id,)).fetchone())

    # ---------- 候选组 ----------

    def _generate_candidates(self, db: sqlite3.Connection, project_id: int, actor_id: int) -> dict[str, Any]:
        rule = self._active_rule_set(db, project_id)
        config = json.loads(rule["config_json"])
        rows = db.execute(
            "SELECT * FROM pottery_sherds WHERE project_id=? AND id NOT IN (SELECT sherd_id FROM pottery_vessel_members) ORDER BY code_norm, id",
            (project_id,),
        ).fetchall()
        sherds = [_eval_sherd(_sherd_dict(row)) for row in rows]
        groups = build_groups(config, sherds)
        existing = {
            row["group_key"]: row
            for row in db.execute("SELECT * FROM pottery_candidate_groups WHERE project_id=? AND rule_set_id=?", (project_id, rule["id"])).fetchall()
        }
        seen: set[str] = set()
        created = updated = kept = 0
        summaries = []
        stamp = now()
        for group in groups:
            key = group_key(rule["id"], group["member_codes"])
            seen.add(key)
            scores_json = stable_json({"pairs": group["pair_scores"]})
            old = existing.get(key)
            if old is None:
                cursor = db.execute(
                    "INSERT INTO pottery_candidate_groups(project_id,rule_set_id,group_key,status,score,scores_json,created_at,updated_at) VALUES(?,?,?,'pending',?,?,?,?)",
                    (project_id, rule["id"], key, group["score"], scores_json, stamp, stamp),
                )
                for sherd_id in group["member_ids"]:
                    db.execute("INSERT INTO pottery_candidate_members(group_id,sherd_id) VALUES(?,?)", (cursor.lastrowid, sherd_id))
                created += 1
            elif old["status"] == "pending":
                if old["score"] != group["score"] or old["scores_json"] != scores_json:
                    db.execute("UPDATE pottery_candidate_groups SET score=?,scores_json=?,updated_at=? WHERE id=?", (group["score"], scores_json, stamp, old["id"]))
                    updated += 1
                else:
                    kept += 1
            else:
                kept += 1
            summaries.append({"group_key": key, "member_codes": group["member_codes"], "score": group["score"]})
        removed = 0
        for key, row in existing.items():
            if key not in seen and row["status"] == "pending":
                db.execute("DELETE FROM pottery_candidate_members WHERE group_id=?", (row["id"],))
                db.execute("DELETE FROM pottery_candidate_groups WHERE id=?", (row["id"],))
                removed += 1
        # 旧规则版本遗留的待复核候选已不可确认（版本一致性），生成时一并清理；已确认/已拒绝的保留为历史。
        stale = db.execute(
            "SELECT id FROM pottery_candidate_groups WHERE project_id=? AND rule_set_id<>? AND status='pending'",
            (project_id, rule["id"]),
        ).fetchall()
        for row in stale:
            db.execute("DELETE FROM pottery_candidate_members WHERE group_id=?", (row["id"],))
            db.execute("DELETE FROM pottery_candidate_groups WHERE id=?", (row["id"],))
            removed += 1
        self.base.audit(
            "pottery.candidates.generate",
            "pottery_rule_set",
            str(rule["id"]),
            {"created": created, "updated": updated, "removed": removed, "kept": kept},
            project_id=project_id,
            actor_id=actor_id,
        )
        return {"rule_set_id": rule["id"], "rule_version": rule["version"], "created": created, "updated": updated, "removed": removed, "kept": kept, "groups": summaries}

    def generate_candidates(self, project_id: int, actor_id: int) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, WRITE_ROLES)
        with transaction(immediate=True) as db:
            return self._generate_candidates(db, project_id, actor_id)

    def _candidate_summary(self, row: sqlite3.Row) -> dict[str, Any]:
        members = self.db.execute(
            "SELECT s.code_norm FROM pottery_candidate_members m JOIN pottery_sherds s ON s.id=m.sherd_id WHERE m.group_id=? ORDER BY s.code_norm, s.id",
            (row["id"],),
        ).fetchall()
        codes = [member["code_norm"] for member in members]
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "rule_set_id": row["rule_set_id"],
            "group_key": row["group_key"],
            "status": row["status"],
            "score": row["score"],
            "member_count": len(codes),
            "member_codes": codes,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_candidates(self, project_id: int, actor_id: int, cursor: str = "", limit: int = 50, status: str | None = None) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        key = decode_cursor(cursor, 2)
        where, params = "project_id=?", [project_id]
        if status:
            where += " AND status=?"
            params.append(status)
        if key:
            where += " AND (score < ? OR (score = ? AND id > ?))"
            params += [key[0], key[0], key[1]]
        rows = self.db.execute(f"SELECT * FROM pottery_candidate_groups WHERE {where} ORDER BY score DESC, id LIMIT ?", (*params, limit + 1)).fetchall()
        page = rows[:limit]
        next_cursor = encode_cursor([page[-1]["score"], page[-1]["id"]]) if len(rows) > limit and page else None
        return {"data": [self._candidate_summary(row) for row in page], "next_cursor": next_cursor}

    def get_candidate(self, project_id: int, actor_id: int, group_id: int) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        row = self.db.execute("SELECT * FROM pottery_candidate_groups WHERE id=? AND project_id=?", (group_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("candidate_not_found", "候选组不存在", 404)
        rule = self.db.execute("SELECT version FROM pottery_rule_sets WHERE id=?", (row["rule_set_id"],)).fetchone()
        members = [
            _sherd_dict(member)
            for member in self.db.execute(
                "SELECT s.* FROM pottery_candidate_members m JOIN pottery_sherds s ON s.id=m.sherd_id WHERE m.group_id=? ORDER BY s.code_norm, s.id",
                (group_id,),
            ).fetchall()
        ]
        events = [
            dict(event)
            for event in self.db.execute(
                "SELECT * FROM pottery_review_events WHERE target_type='candidate_group' AND target_id=? ORDER BY id",
                (group_id,),
            ).fetchall()
        ]
        result = self._candidate_summary(row)
        result.update({
            "rule_version": rule["version"] if rule else None,
            "members": members,
            "pair_scores": json.loads(row["scores_json"]).get("pairs", {}),
            "review_events": events,
        })
        return result

    # ---------- 复核操作 ----------

    def _check_rationale(self, rationale: str | None) -> str:
        value = (rationale or "").strip()
        if not value:
            raise ServiceError("rationale_required", "复核操作必须填写依据", 422)
        return value

    def _review_event(self, db: sqlite3.Connection, project_id: int, actor_id: int, action: str, target_type: str, target_id: int, rationale: str, detail: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO pottery_review_events(project_id,actor_id,action,target_type,target_id,rationale,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (project_id, actor_id, action, target_type, target_id, rationale, stable_json(detail), now()),
        )

    def confirm_candidate(self, project_id: int, actor_id: int, group_id: int, rationale: str, vessel_code: str | None = None) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, REVIEW_ROLES)
        rationale = self._check_rationale(rationale)
        try:
            with transaction(immediate=True) as db:
                group = db.execute("SELECT * FROM pottery_candidate_groups WHERE id=?", (group_id,)).fetchone()
                if group is None or group["project_id"] != project_id:
                    raise ServiceError("candidate_not_found", "候选组不存在", 404)
                if group["status"] != "pending":
                    raise ServiceError("candidate_not_pending", "候选组已被复核处理", 409)
                rule = self._active_rule_set(db, project_id)
                if group["rule_set_id"] != rule["id"]:
                    raise ServiceError("rule_version_stale", "候选组生成规则版本与当前版本不一致，请重新生成候选", 409)
                config = json.loads(rule["config_json"])
                members = [
                    _sherd_dict(member)
                    for member in db.execute(
                        "SELECT s.* FROM pottery_candidate_members m JOIN pottery_sherds s ON s.id=m.sherd_id WHERE m.group_id=? ORDER BY s.code_norm, s.id",
                        (group_id,),
                    ).fetchall()
                ]
                if len(members) < 2:
                    raise ServiceError("candidate_empty", "候选组成员不足", 409)
                member_ids = [member["id"] for member in members]
                placeholders = ",".join("?" * len(member_ids))
                conflict = db.execute(f"SELECT sherd_id FROM pottery_vessel_members WHERE sherd_id IN ({placeholders})", member_ids).fetchall()
                if conflict:
                    raise ServiceError("member_conflict", "存在已确认到其他器物的陶片，成员唯一性冲突", 409)
                derived = derive(config)
                for i in range(len(members)):
                    for j in range(i + 1, len(members)):
                        left, right = members[i]["context_code"], members[j]["context_code"]
                        if context_forbidden(config, left, right, derived):
                            raise ServiceError("context_forbidden", f"出土上下文 {left} 与 {right} 禁止拼合", 409)
                code = (vessel_code or "").strip() or f"V-G{group_id}"
                stamp = now()
                cursor = db.execute(
                    "INSERT INTO pottery_vessels(project_id,vessel_code,rule_set_id,source_group_id,status,rationale,created_by,created_at,updated_at) VALUES(?,?,?,?,'confirmed',?,?,?,?)",
                    (project_id, code, rule["id"], group_id, rationale, actor_id, stamp, stamp),
                )
                vessel_id = cursor.lastrowid
                for member_id in member_ids:
                    db.execute("INSERT INTO pottery_vessel_members(vessel_id,sherd_id,added_at) VALUES(?,?,?)", (vessel_id, member_id, stamp))
                db.execute("UPDATE pottery_candidate_groups SET status='confirmed',updated_at=? WHERE id=?", (stamp, group_id))
                self._review_event(db, project_id, actor_id, "confirm", "vessel", vessel_id, rationale, {"group_id": group_id, "vessel_code": code, "member_ids": member_ids})
                self.base.audit("pottery.candidate.confirm", "pottery_vessel", str(vessel_id), {"group_id": group_id, "vessel_code": code}, project_id=project_id, actor_id=actor_id)
                return self.get_vessel(project_id, actor_id, vessel_id)
        except sqlite3.IntegrityError as exc:
            if "vessel_code" in str(exc):
                raise ServiceError("vessel_code_exists", "器物编号已存在", 409) from exc
            raise ServiceError("member_conflict", "存在已确认到其他器物的陶片，成员唯一性冲突", 409) from exc

    def reject_candidate(self, project_id: int, actor_id: int, group_id: int, rationale: str) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, REVIEW_ROLES)
        rationale = self._check_rationale(rationale)
        with transaction(immediate=True) as db:
            group = db.execute("SELECT * FROM pottery_candidate_groups WHERE id=?", (group_id,)).fetchone()
            if group is None or group["project_id"] != project_id:
                raise ServiceError("candidate_not_found", "候选组不存在", 404)
            if group["status"] != "pending":
                raise ServiceError("candidate_not_pending", "候选组已被复核处理", 409)
            member_ids = [row["sherd_id"] for row in db.execute("SELECT sherd_id FROM pottery_candidate_members WHERE group_id=?", (group_id,)).fetchall()]
            db.execute("UPDATE pottery_candidate_groups SET status='rejected',updated_at=? WHERE id=?", (now(), group_id))
            self._review_event(db, project_id, actor_id, "reject", "candidate_group", group_id, rationale, {"member_ids": member_ids})
            self.base.audit("pottery.candidate.reject", "pottery_candidate_group", str(group_id), {"member_ids": member_ids}, project_id=project_id, actor_id=actor_id)
        return {"id": group_id, "status": "rejected"}

    # ---------- 器物 ----------

    def _vessel_dict(self, row: sqlite3.Row, with_events: bool = True) -> dict[str, Any]:
        members = [
            _sherd_dict(member)
            for member in self.db.execute(
                "SELECT s.* FROM pottery_vessel_members m JOIN pottery_sherds s ON s.id=m.sherd_id WHERE m.vessel_id=? ORDER BY s.code_norm, s.id",
                (row["id"],),
            ).fetchall()
        ]
        rule = self.db.execute("SELECT version FROM pottery_rule_sets WHERE id=?", (row["rule_set_id"],)).fetchone()
        result = {
            "id": row["id"],
            "project_id": row["project_id"],
            "vessel_code": row["vessel_code"],
            "status": row["status"],
            "rule_set_id": row["rule_set_id"],
            "rule_version": rule["version"] if rule else None,
            "source_group_id": row["source_group_id"],
            "rationale": row["rationale"],
            "members": members,
            "member_codes": [member["code_norm"] for member in members],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if with_events:
            result["review_events"] = [
                dict(event)
                for event in self.db.execute(
                    "SELECT * FROM pottery_review_events WHERE target_type='vessel' AND target_id=? ORDER BY id",
                    (row["id"],),
                ).fetchall()
            ]
        return result

    def get_vessel(self, project_id: int, actor_id: int, vessel_id: int) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        row = self.db.execute("SELECT * FROM pottery_vessels WHERE id=? AND project_id=?", (vessel_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("vessel_not_found", "器物不存在", 404)
        return self._vessel_dict(row)

    def list_vessels(self, project_id: int, actor_id: int, cursor: str = "", limit: int = 50, status: str | None = None) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        key = decode_cursor(cursor, 1)
        where, params = "project_id=?", [project_id]
        if status:
            where += " AND status=?"
            params.append(status)
        if key:
            where += " AND id > ?"
            params.append(key[0])
        rows = self.db.execute(f"SELECT * FROM pottery_vessels WHERE {where} ORDER BY id LIMIT ?", (*params, limit + 1)).fetchall()
        page = rows[:limit]
        next_cursor = encode_cursor([page[-1]["id"]]) if len(rows) > limit and page else None
        return {"data": [self._vessel_dict(row, with_events=False) for row in page], "next_cursor": next_cursor}

    def split_vessel(self, project_id: int, actor_id: int, vessel_id: int, rationale: str) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, REVIEW_ROLES)
        rationale = self._check_rationale(rationale)
        with transaction(immediate=True) as db:
            vessel = db.execute("SELECT * FROM pottery_vessels WHERE id=?", (vessel_id,)).fetchone()
            if vessel is None or vessel["project_id"] != project_id:
                raise ServiceError("vessel_not_found", "器物不存在", 404)
            if vessel["status"] != "confirmed":
                raise ServiceError("vessel_not_confirmed", "器物不是已确认状态", 409)
            member_ids = [row["sherd_id"] for row in db.execute("SELECT sherd_id FROM pottery_vessel_members WHERE vessel_id=?", (vessel_id,)).fetchall()]
            db.execute("DELETE FROM pottery_vessel_members WHERE vessel_id=?", (vessel_id,))
            db.execute("UPDATE pottery_vessels SET status='dissolved',updated_at=? WHERE id=?", (now(), vessel_id))
            if vessel["source_group_id"] is not None:
                db.execute("UPDATE pottery_candidate_groups SET status='pending',updated_at=? WHERE id=? AND status='confirmed'", (now(), vessel["source_group_id"]))
            self._review_event(db, project_id, actor_id, "split", "vessel", vessel_id, rationale, {"member_ids": member_ids})
            self.base.audit("pottery.vessel.split", "pottery_vessel", str(vessel_id), {"member_ids": member_ids}, project_id=project_id, actor_id=actor_id)
            return self.get_vessel(project_id, actor_id, vessel_id)

    def merge_vessels(self, project_id: int, actor_id: int, vessel_ids: list[int], rationale: str, vessel_code: str | None = None) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, REVIEW_ROLES)
        rationale = self._check_rationale(rationale)
        ids = sorted(set(vessel_ids))
        if len(ids) < 2:
            raise ServiceError("merge_too_few", "合组至少需要两个器物", 422)
        try:
            with transaction(immediate=True) as db:
                vessels = [db.execute("SELECT * FROM pottery_vessels WHERE id=?", (vessel_id,)).fetchone() for vessel_id in ids]
                if any(vessel is None or vessel["project_id"] != project_id for vessel in vessels):
                    raise ServiceError("vessel_not_found", "器物不存在", 404)
                if any(vessel["status"] != "confirmed" for vessel in vessels):
                    raise ServiceError("vessel_not_confirmed", "参与合组的器物必须都是已确认状态", 409)
                rule = self._active_rule_set(db, project_id)
                if any(vessel["rule_set_id"] != rule["id"] for vessel in vessels):
                    raise ServiceError("rule_version_stale", "器物确认规则版本与当前版本不一致", 409)
                config = json.loads(rule["config_json"])
                derived = derive(config)
                members: dict[int, dict[str, Any]] = {}
                for vessel_id in ids:
                    for member in db.execute(
                        "SELECT s.* FROM pottery_vessel_members m JOIN pottery_sherds s ON s.id=m.sherd_id WHERE m.vessel_id=? ORDER BY s.code_norm, s.id",
                        (vessel_id,),
                    ).fetchall():
                        members[member["id"]] = _sherd_dict(member)
                member_list = sorted(members.values(), key=lambda item: (item["code_norm"], item["id"]))
                for i in range(len(member_list)):
                    for j in range(i + 1, len(member_list)):
                        left, right = member_list[i]["context_code"], member_list[j]["context_code"]
                        if context_forbidden(config, left, right, derived):
                            raise ServiceError("context_forbidden", f"出土上下文 {left} 与 {right} 禁止拼合", 409)
                code = (vessel_code or "").strip() or f"V-M{'-'.join(str(vessel_id) for vessel_id in ids)}"
                stamp = now()
                # 先释放旧成员再写入新器物；任一步失败整体回滚，旧器物保持原状。
                for vessel_id in ids:
                    db.execute("DELETE FROM pottery_vessel_members WHERE vessel_id=?", (vessel_id,))
                placeholders = ",".join("?" * len(ids))
                db.execute(f"UPDATE pottery_vessels SET status='dissolved',updated_at=? WHERE id IN ({placeholders})", [stamp, *ids])
                cursor = db.execute(
                    "INSERT INTO pottery_vessels(project_id,vessel_code,rule_set_id,source_group_id,status,rationale,created_by,created_at,updated_at) VALUES(?,?,?,NULL,'confirmed',?,?,?,?)",
                    (project_id, code, rule["id"], rationale, actor_id, stamp, stamp),
                )
                vessel_id = cursor.lastrowid
                for member in member_list:
                    db.execute("INSERT INTO pottery_vessel_members(vessel_id,sherd_id,added_at) VALUES(?,?,?)", (vessel_id, member["id"], stamp))
                self._review_event(db, project_id, actor_id, "merge", "vessel", vessel_id, rationale, {"merged_vessel_ids": ids, "member_ids": [member["id"] for member in member_list]})
                self.base.audit("pottery.vessel.merge", "pottery_vessel", str(vessel_id), {"merged_vessel_ids": ids}, project_id=project_id, actor_id=actor_id)
                return self.get_vessel(project_id, actor_id, vessel_id)
        except sqlite3.IntegrityError as exc:
            if "vessel_code" in str(exc):
                raise ServiceError("vessel_code_exists", "器物编号已存在", 409) from exc
            raise ServiceError("member_conflict", "存在已确认到其他器物的陶片，成员唯一性冲突", 409) from exc

    # ---------- 统计 ----------

    def _stat_run_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "plan_code": row["plan_code"],
            "version": row["version"],
            "rule_set_id": row["rule_set_id"],
            "input_hash": row["input_hash"],
            "result": json.loads(row["result_json"]),
            "created_at": row["created_at"],
        }

    def _run_stats(self, db: sqlite3.Connection, project_id: int, actor_id: int, plan: dict[str, Any]) -> dict[str, Any]:
        rule = self._active_rule_set(db, project_id)
        config = json.loads(rule["config_json"])
        sherds = [
            _eval_sherd(_sherd_dict(row))
            for row in db.execute("SELECT * FROM pottery_sherds WHERE project_id=? ORDER BY code_norm, id", (project_id,)).fetchall()
        ]
        vessels = []
        for vessel in db.execute("SELECT * FROM pottery_vessels WHERE project_id=? AND status='confirmed' ORDER BY id", (project_id,)).fetchall():
            member_ids = [row["sherd_id"] for row in db.execute("SELECT sherd_id FROM pottery_vessel_members WHERE vessel_id=? ORDER BY sherd_id", (vessel["id"],)).fetchall()]
            vessels.append({"id": vessel["id"], "vessel_code": vessel["vessel_code"], "member_ids": member_ids})
        norm_plan = {
            "plan_code": plan["plan_code"],
            "contexts": sorted(plan["contexts"]) if plan.get("contexts") else None,
            "parts": sorted(plan["parts"]) if plan.get("parts") else None,
        }
        result = compute_result(config, sherds, vessels, norm_plan)
        payload = {"plan": norm_plan, "rule_set_id": rule["id"], "rule_version": rule["version"], **result}
        input_hash = hashlib.sha256(
            stable_json({
                "rule_set_id": rule["id"],
                "rule_version": rule["version"],
                "plan": norm_plan,
                "sherds": [
                    [
                        sherd["id"], sherd["code_norm"], sherd["part"], sherd["fabric_color"], sherd["thickness_min"], sherd["thickness_max"],
                        sorted(sherd["decoration"]), sherd["diameter_mm"], sherd["context_code"], sherd["typology_code"], sherd["rim_percent"],
                    ]
                    for sherd in sherds
                ],
                "vessels": [[vessel["id"], vessel["vessel_code"], vessel["member_ids"]] for vessel in vessels],
            }).encode()
        ).hexdigest()
        latest = db.execute(
            "SELECT * FROM pottery_stat_runs WHERE project_id=? AND plan_code=? ORDER BY version DESC LIMIT 1",
            (project_id, norm_plan["plan_code"]),
        ).fetchone()
        if latest is not None and latest["input_hash"] == input_hash:
            return self._stat_run_dict(latest) | {"created": False}
        version = (latest["version"] if latest else 0) + 1
        cursor = db.execute(
            "INSERT INTO pottery_stat_runs(project_id,plan_code,version,rule_set_id,input_hash,result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (project_id, norm_plan["plan_code"], version, rule["id"], input_hash, stable_json(payload), actor_id, now()),
        )
        self.base.audit("pottery.stats.run", "pottery_stat_run", str(cursor.lastrowid), {"plan_code": norm_plan["plan_code"], "version": version}, project_id=project_id, actor_id=actor_id)
        row = db.execute("SELECT * FROM pottery_stat_runs WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._stat_run_dict(row) | {"created": True}

    def run_stats(self, project_id: int, actor_id: int, plan: dict[str, Any]) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, STATS_ROLES)
        with transaction(immediate=True) as db:
            return self._run_stats(db, project_id, actor_id, plan)

    def list_stat_runs(self, project_id: int, actor_id: int, cursor: str = "", limit: int = 50, plan_code: str | None = None) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        key = decode_cursor(cursor, 1)
        where, params = "project_id=?", [project_id]
        if plan_code:
            where += " AND plan_code=?"
            params.append(plan_code)
        if key:
            where += " AND id < ?"
            params.append(key[0])
        rows = self.db.execute(f"SELECT * FROM pottery_stat_runs WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        page = rows[:limit]
        next_cursor = encode_cursor([page[-1]["id"]]) if len(rows) > limit and page else None
        return {"data": [self._stat_run_dict(row) for row in page], "next_cursor": next_cursor}

    def get_stat_run(self, project_id: int, actor_id: int, run_id: int) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        row = self.db.execute("SELECT * FROM pottery_stat_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("stat_run_not_found", "统计结果版本不存在", 404)
        return self._stat_run_dict(row)

    def diff_stats(self, project_id: int, actor_id: int, plan_code: str, from_version: int, to_version: int) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        old = self.db.execute(
            "SELECT * FROM pottery_stat_runs WHERE project_id=? AND plan_code=? AND version=?",
            (project_id, plan_code, from_version),
        ).fetchone()
        new = self.db.execute(
            "SELECT * FROM pottery_stat_runs WHERE project_id=? AND plan_code=? AND version=?",
            (project_id, plan_code, to_version),
        ).fetchone()
        if old is None or new is None:
            raise ServiceError("stat_run_not_found", "统计结果版本不存在", 404)
        return {
            "plan_code": plan_code,
            "from_version": from_version,
            "to_version": to_version,
            "changes": diff_results(json.loads(old["result_json"]), json.loads(new["result_json"])),
        }

    # ---------- 离线导入 ----------

    def import_bundle(self, project_id: int, actor_id: int, bundle: dict[str, Any]) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, WRITE_ROLES)
        try:
            with transaction(immediate=True) as db:
                rule_info = None
                if bundle.get("rule_set"):
                    rule = self._create_rule_set(db, project_id, actor_id, bundle["rule_set"]["name"], bundle["rule_set"]["config"])
                    rule_info = {"id": rule["id"], "version": rule["version"]}
                imported_codes = []
                for sherd in bundle.get("sherds", []):
                    self._insert_sherd(db, project_id, sherd)
                    imported_codes.append(sherd["sherd_code"])
                summary: dict[str, Any] = {"imported": len(imported_codes), "sherd_codes": imported_codes, "rule_set": rule_info, "candidates": None, "stats": None}
                if bundle.get("run_candidates", True):
                    summary["candidates"] = self._generate_candidates(db, project_id, actor_id)
                if bundle.get("run_stats", True) and bundle.get("plan"):
                    summary["stats"] = self._run_stats(db, project_id, actor_id, bundle["plan"])
                self.base.audit("pottery.import", "project", str(project_id), {"imported": len(imported_codes)}, project_id=project_id, actor_id=actor_id)
                return summary
        except sqlite3.IntegrityError as exc:
            raise ServiceError("import_conflict", "导入失败：陶片编号重复或数据冲突，已整体回滚", 409) from exc
