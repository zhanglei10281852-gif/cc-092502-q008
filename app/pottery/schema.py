from __future__ import annotations

from app.database import connection

POTTERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS pottery_contexts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 code TEXT NOT NULL,
 kind TEXT NOT NULL DEFAULT '灰坑',
 description TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 UNIQUE(project_id, code)
);
CREATE TABLE IF NOT EXISTS pottery_context_exclusions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 context_a_id INTEGER NOT NULL REFERENCES pottery_contexts(id) ON DELETE CASCADE,
 context_b_id INTEGER NOT NULL REFERENCES pottery_contexts(id) ON DELETE CASCADE,
 reason TEXT NOT NULL,
 created_at TEXT NOT NULL,
 CHECK(context_a_id <> context_b_id),
 UNIQUE(project_id, context_a_id, context_b_id)
);
CREATE TABLE IF NOT EXISTS pottery_rule_sets (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 version INTEGER NOT NULL,
 name TEXT NOT NULL,
 rules_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS pottery_imports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 batch_key TEXT NOT NULL,
 file_name TEXT NOT NULL DEFAULT '',
 file_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'done' CHECK(status IN ('done','failed')),
 stats_json TEXT NOT NULL DEFAULT '{}',
 error TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(project_id, batch_key)
);
CREATE TABLE IF NOT EXISTS pottery_sherds (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 sherd_code TEXT NOT NULL,
 part TEXT NOT NULL CHECK(part IN ('rim','body','base','handle','other')),
 fabric_color TEXT NOT NULL,
 thickness_min_mm REAL NOT NULL,
 thickness_max_mm REAL NOT NULL,
 decoration_code TEXT NOT NULL DEFAULT '',
 length_mm REAL,
 width_mm REAL,
 rim_arc_percent REAL NOT NULL DEFAULT 0,
 vessel_type TEXT NOT NULL DEFAULT '',
 context_id INTEGER REFERENCES pottery_contexts(id) ON DELETE SET NULL,
 import_id INTEGER REFERENCES pottery_imports(id) ON DELETE SET NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 CHECK(thickness_min_mm <= thickness_max_mm),
 CHECK(rim_arc_percent >= 0 AND rim_arc_percent <= 100),
 UNIQUE(project_id, sherd_code)
);
CREATE INDEX IF NOT EXISTS idx_pottery_sherds_project ON pottery_sherds(project_id, sherd_code, id);
CREATE TABLE IF NOT EXISTS pottery_candidates (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 rule_set_id INTEGER NOT NULL REFERENCES pottery_rule_sets(id) ON DELETE CASCADE,
 group_key TEXT NOT NULL,
 score REAL NOT NULL,
 breakdown_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','rejected')),
 created_at TEXT NOT NULL,
 UNIQUE(project_id, rule_set_id, group_key)
);
CREATE INDEX IF NOT EXISTS idx_pottery_candidates_project ON pottery_candidates(project_id, status, id);
CREATE TABLE IF NOT EXISTS pottery_candidate_members (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 candidate_id INTEGER NOT NULL REFERENCES pottery_candidates(id) ON DELETE CASCADE,
 sherd_id INTEGER NOT NULL REFERENCES pottery_sherds(id) ON DELETE CASCADE,
 item_score REAL NOT NULL,
 UNIQUE(candidate_id, sherd_id)
);
CREATE TABLE IF NOT EXISTS pottery_vessels (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 vessel_code TEXT NOT NULL,
 rule_set_id INTEGER REFERENCES pottery_rule_sets(id) ON DELETE SET NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','dissolved')),
 source TEXT NOT NULL DEFAULT 'candidate' CHECK(source IN ('candidate','merge','split')),
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 dissolved_at TEXT NOT NULL DEFAULT '',
 UNIQUE(project_id, vessel_code)
);
CREATE TABLE IF NOT EXISTS pottery_vessel_members (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 vessel_id INTEGER NOT NULL REFERENCES pottery_vessels(id) ON DELETE CASCADE,
 sherd_id INTEGER NOT NULL REFERENCES pottery_sherds(id) ON DELETE CASCADE,
 removed_at TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 UNIQUE(vessel_id, sherd_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pottery_vessel_active_sherd ON pottery_vessel_members(sherd_id) WHERE removed_at='';
CREATE INDEX IF NOT EXISTS idx_pottery_vessel_members_vessel ON pottery_vessel_members(vessel_id, removed_at);
CREATE TABLE IF NOT EXISTS pottery_reviews (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 action TEXT NOT NULL CHECK(action IN ('confirm','reject','split','merge')),
 candidate_id INTEGER REFERENCES pottery_candidates(id) ON DELETE SET NULL,
 vessel_id INTEGER REFERENCES pottery_vessels(id) ON DELETE SET NULL,
 rationale TEXT NOT NULL,
 detail_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pottery_reviews_project ON pottery_reviews(project_id, id);
CREATE TABLE IF NOT EXISTS pottery_plans (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 code TEXT NOT NULL,
 name TEXT NOT NULL,
 config_json TEXT NOT NULL DEFAULT '{}',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(project_id, code)
);
CREATE TABLE IF NOT EXISTS pottery_reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 plan_id INTEGER NOT NULL REFERENCES pottery_plans(id) ON DELETE CASCADE,
 version INTEGER NOT NULL,
 rule_set_id INTEGER REFERENCES pottery_rule_sets(id) ON DELETE SET NULL,
 input_hash TEXT NOT NULL,
 result_json TEXT NOT NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(plan_id, version)
);
"""


def init_pottery_db() -> None:
    connection().executescript(POTTERY_SCHEMA)
