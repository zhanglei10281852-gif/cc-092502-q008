"""陶片拼合模块的数据库表结构。

表结构通过 app.database.SCHEMA 拼接进统一初始化流程，不单独建库。
"""

POTTERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS pottery_rule_sets (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 version INTEGER NOT NULL,
 name TEXT NOT NULL,
 config_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired')),
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS pottery_sherds (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 sherd_code TEXT NOT NULL,
 code_norm TEXT NOT NULL,
 part TEXT NOT NULL CHECK(part IN ('rim','body','base','handle','other')),
 fabric_color TEXT NOT NULL,
 thickness_min REAL NOT NULL,
 thickness_max REAL NOT NULL,
 decoration_json TEXT NOT NULL DEFAULT '[]',
 diameter_mm REAL,
 length_mm REAL,
 width_mm REAL,
 context_code TEXT NOT NULL,
 context_layer TEXT NOT NULL DEFAULT '',
 typology_code TEXT NOT NULL DEFAULT '',
 rim_percent REAL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id, code_norm)
);
CREATE INDEX IF NOT EXISTS idx_pottery_sherds_project ON pottery_sherds(project_id, code_norm, id);
CREATE TABLE IF NOT EXISTS pottery_candidate_groups (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 rule_set_id INTEGER NOT NULL REFERENCES pottery_rule_sets(id) ON DELETE CASCADE,
 group_key TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','rejected')),
 score REAL NOT NULL,
 scores_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id, rule_set_id, group_key)
);
CREATE INDEX IF NOT EXISTS idx_pottery_candidates_project ON pottery_candidate_groups(project_id, status, score, id);
CREATE TABLE IF NOT EXISTS pottery_candidate_members (
 group_id INTEGER NOT NULL REFERENCES pottery_candidate_groups(id) ON DELETE CASCADE,
 sherd_id INTEGER NOT NULL REFERENCES pottery_sherds(id) ON DELETE CASCADE,
 PRIMARY KEY(group_id, sherd_id)
);
CREATE TABLE IF NOT EXISTS pottery_vessels (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 vessel_code TEXT NOT NULL,
 rule_set_id INTEGER NOT NULL REFERENCES pottery_rule_sets(id),
 source_group_id INTEGER REFERENCES pottery_candidate_groups(id) ON DELETE SET NULL,
 status TEXT NOT NULL DEFAULT 'confirmed' CHECK(status IN ('confirmed','dissolved')),
 rationale TEXT NOT NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id, vessel_code)
);
CREATE TABLE IF NOT EXISTS pottery_vessel_members (
 vessel_id INTEGER NOT NULL REFERENCES pottery_vessels(id) ON DELETE CASCADE,
 sherd_id INTEGER NOT NULL REFERENCES pottery_sherds(id) ON DELETE CASCADE,
 added_at TEXT NOT NULL,
 PRIMARY KEY(vessel_id, sherd_id)
);
-- 成员唯一约束：拆组时删除成员行，因此同一陶片在任意时刻只能属于一个已确认器物。
CREATE UNIQUE INDEX IF NOT EXISTS idx_pottery_vessel_members_unique ON pottery_vessel_members(sherd_id);
CREATE TABLE IF NOT EXISTS pottery_review_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 action TEXT NOT NULL CHECK(action IN ('confirm','reject','split','merge')),
 target_type TEXT NOT NULL,
 target_id INTEGER NOT NULL,
 rationale TEXT NOT NULL,
 detail_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pottery_review_project ON pottery_review_events(project_id, target_type, target_id);
CREATE TABLE IF NOT EXISTS pottery_stat_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 plan_code TEXT NOT NULL,
 version INTEGER NOT NULL,
 rule_set_id INTEGER NOT NULL REFERENCES pottery_rule_sets(id),
 input_hash TEXT NOT NULL,
 result_json TEXT NOT NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(project_id, plan_code, version)
);
"""
