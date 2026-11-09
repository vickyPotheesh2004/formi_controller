"""SQLite schema and small persistence helpers.

The decisions and counterfactuals tables are append-only: triggers reject
UPDATE and DELETE.  Lifecycle changes are recorded as new decision rows with a
``correlation_id``, which keeps the audit trail immutable.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    first_event_id INTEGER,
    last_event_id INTEGER,
    events INTEGER,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk INTEGER,
    line_no INTEGER,
    error TEXT NOT NULL,
    raw_head TEXT
);

CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT PRIMARY KEY,
    tenant_id TEXT,
    public_json TEXT NOT NULL,
    updated_sim_minute INTEGER
);

CREATE TABLE IF NOT EXISTS config_versions (
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    actor TEXT,
    changed_dims_json TEXT,
    config_json TEXT NOT NULL,
    applied_ts TEXT,
    proposal_id TEXT,
    approval_id TEXT,
    PRIMARY KEY (workflow_id, version)
);

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    leads INTEGER NOT NULL DEFAULT 0,
    created_minute INTEGER,
    created_day INTEGER,
    config_version_at_creation INTEGER
);

CREATE TABLE IF NOT EXISTS lead_features (
    lead_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    campaign_id TEXT,
    person_id TEXT,
    lang TEXT,
    city_tier TEXT,
    age_band TEXT,
    value_band TEXT
);
CREATE INDEX IF NOT EXISTS ix_lead_features_campaign ON lead_features(workflow_id, campaign_id);

CREATE TABLE IF NOT EXISTS lead_daily (
    workflow_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    lang TEXT NOT NULL,
    city_tier TEXT NOT NULL,
    age_band TEXT NOT NULL,
    value_band TEXT NOT NULL,
    uploaded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, day, lang, city_tier, age_band, value_band)
);

CREATE TABLE IF NOT EXISTS variant_daily (
    workflow_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    hour INTEGER NOT NULL,
    cohort_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    welcome_message TEXT NOT NULL,
    voice TEXT NOT NULL,
    caller_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    dials INTEGER NOT NULL DEFAULT 0,
    connected INTEGER NOT NULL DEFAULT 0,
    engaged INTEGER NOT NULL DEFAULT 0,
    interested INTEGER NOT NULL DEFAULT 0,
    halluc INTEGER NOT NULL DEFAULT 0,
    flow_sum REAL NOT NULL DEFAULT 0,
    flow_n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, day, hour, cohort_id, config_version,
                 welcome_message, voice, caller_id, prompt_version)
);
CREATE INDEX IF NOT EXISTS ix_variant_daily_wf_day ON variant_daily(workflow_id, day);

CREATE TABLE IF NOT EXISTS feature_variant_daily (
    workflow_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    feature TEXT NOT NULL,
    feature_value TEXT NOT NULL,
    dim TEXT NOT NULL,
    variant_value TEXT NOT NULL,
    dials INTEGER NOT NULL DEFAULT 0,
    connected INTEGER NOT NULL DEFAULT 0,
    engaged INTEGER NOT NULL DEFAULT 0,
    halluc INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, day, config_version, feature, feature_value, dim, variant_value)
);

CREATE TABLE IF NOT EXISTS hour_feature_daily (
    workflow_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    feature TEXT NOT NULL,
    feature_value TEXT NOT NULL,
    hour INTEGER NOT NULL,
    dials INTEGER NOT NULL DEFAULT 0,
    connected INTEGER NOT NULL DEFAULT 0,
    engaged INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, day, feature, feature_value, hour)
);

CREATE TABLE IF NOT EXISTS did_daily (
    workflow_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    caller_id TEXT NOT NULL,
    dials INTEGER NOT NULL DEFAULT 0,
    connected INTEGER NOT NULL DEFAULT 0,
    engaged INTEGER NOT NULL DEFAULT 0,
    halluc INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, day, caller_id)
);

CREATE TABLE IF NOT EXISTS prompt_daily (
    workflow_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    dials INTEGER NOT NULL DEFAULT 0,
    connected INTEGER NOT NULL DEFAULT 0,
    engaged INTEGER NOT NULL DEFAULT 0,
    halluc INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, day, prompt_version)
);

CREATE TABLE IF NOT EXISTS cohort_hour_daily (
    workflow_id TEXT NOT NULL,
    cohort_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    hour INTEGER NOT NULL,
    dials INTEGER NOT NULL DEFAULT 0,
    connected INTEGER NOT NULL DEFAULT 0,
    engaged INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, cohort_id, day, hour)
);

CREATE TABLE IF NOT EXISTS concluded_daily (
    workflow_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    concluded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, day)
);

CREATE TABLE IF NOT EXISTS person_hour (
    workflow_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    hour INTEGER NOT NULL,
    dials INTEGER NOT NULL DEFAULT 0,
    connected INTEGER NOT NULL DEFAULT 0,
    engaged INTEGER NOT NULL DEFAULT 0,
    last_minute INTEGER,
    PRIMARY KEY (workflow_id, person_id, hour)
);

CREATE TABLE IF NOT EXISTS cohorts (
    workflow_id TEXT NOT NULL,
    cohort_id TEXT NOT NULL,
    where_json TEXT NOT NULL,
    rationale TEXT,
    filter_json TEXT,
    created_sim_minute INTEGER,
    sample_leads INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    decision_id TEXT,
    PRIMARY KEY (workflow_id, cohort_id)
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    cohort_id TEXT,
    baseline_option TEXT,
    base_version INTEGER,
    config_version INTEGER,
    design_json TEXT,
    status TEXT NOT NULL,
    started_sim_minute INTEGER,
    started_day INTEGER,
    evaluated_sim_minute INTEGER,
    confirmations INTEGER NOT NULL DEFAULT 0,
    next_eval_sim_minute INTEGER,
    evidence_json TEXT,
    decision_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_experiments_wf ON experiments(workflow_id, status);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    parent_id TEXT,
    base_version INTEGER NOT NULL,
    proposed_config_json TEXT NOT NULL,
    change_summary TEXT,
    diff_json TEXT,
    dry_run_json TEXT,
    affected_leads INTEGER,
    impact_score REAL,
    rationale TEXT,
    evidence_json TEXT,
    mode TEXT NOT NULL,
    approval_id TEXT,
    status TEXT NOT NULL,
    created_sim_minute INTEGER,
    decided_sim_minute INTEGER,
    applied_version INTEGER,
    attempts INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    dedupe_key TEXT
);
CREATE INDEX IF NOT EXISTS ix_proposals_wf ON proposals(workflow_id, status);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    status TEXT NOT NULL,
    base_version INTEGER,
    created_sim_minute INTEGER,
    decided_sim_minute INTEGER,
    expires_sim_minute INTEGER,
    apply_by TEXT,
    reasons_json TEXT,
    applied_version INTEGER,
    raw_json TEXT,
    last_polled_sim_minute INTEGER
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    seq INTEGER UNIQUE,
    ts TEXT NOT NULL,
    sim_minute INTEGER,
    run_id TEXT,
    scenario TEXT,
    workflow_id TEXT,
    tenant_id TEXT,
    decision_type TEXT NOT NULL,
    decision_status TEXT NOT NULL,
    summary TEXT NOT NULL,
    decision_statement TEXT NOT NULL,
    source_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    policy_reference TEXT,
    alternatives_json TEXT,
    selected_action TEXT,
    expected_impact_json TEXT,
    risk_assessment TEXT,
    approval_status TEXT,
    execution_status TEXT,
    result_reference TEXT,
    correlation_id TEXT,
    supersedes TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_wf ON decisions(workflow_id, sim_minute);
CREATE INDEX IF NOT EXISTS ix_decisions_type ON decisions(decision_type, decision_status);
CREATE INDEX IF NOT EXISTS ix_decisions_corr ON decisions(correlation_id);

CREATE TABLE IF NOT EXISTS counterfactuals (
    decision_id TEXT PRIMARY KEY,
    counterfactual_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_minute INTEGER,
    operation TEXT NOT NULL,
    cost REAL NOT NULL,
    remaining REAL,
    reason TEXT,
    decision_id TEXT
);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    detected_sim_minute INTEGER,
    detected_day INTEGER,
    last_seen_sim_minute INTEGER,
    resolved_sim_minute INTEGER,
    evidence_json TEXT,
    decision_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_incidents_wf ON incidents(workflow_id, status);

CREATE TABLE IF NOT EXISTS rollback_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    changed_dims_json TEXT,
    reason TEXT,
    created_sim_minute INTEGER,
    source_decision_id TEXT,
    status TEXT NOT NULL DEFAULT 'available'
);

CREATE TABLE IF NOT EXISTS workflow_state (
    workflow_id TEXT PRIMARY KEY,
    last_eval_sim_minute INTEGER,
    last_schedule_sim_minute INTEGER,
    last_prompt_sim_minute INTEGER,
    next_proposal_sim_minute INTEGER,
    notes_json TEXT
);

CREATE TABLE IF NOT EXISTS ingestion_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_minute INTEGER,
    events INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    leads INTEGER NOT NULL DEFAULT 0,
    duplicate_events INTEGER NOT NULL DEFAULT 0,
    malformed INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL,
    chunks INTEGER NOT NULL DEFAULT 0
);

CREATE TRIGGER IF NOT EXISTS decisions_no_update
BEFORE UPDATE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS decisions_no_delete
BEFORE DELETE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS counterfactuals_no_update
BEFORE UPDATE ON counterfactuals
BEGIN
    SELECT RAISE(ABORT, 'counterfactuals are append-only');
END;

CREATE TRIGGER IF NOT EXISTS counterfactuals_no_delete
BEFORE DELETE ON counterfactuals
BEGIN
    SELECT RAISE(ABORT, 'counterfactuals are append-only');
END;
"""

ANALYTICS_TABLES = [
    "chunks", "ingestion_errors", "campaigns", "lead_features", "lead_daily",
    "variant_daily", "feature_variant_daily", "hour_feature_daily", "did_daily",
    "prompt_daily", "cohort_hour_daily", "concluded_daily", "person_hour",
    "cohorts", "experiments", "proposals", "approvals", "credit_events",
    "incidents", "rollback_snapshots", "workflow_state", "ingestion_metrics",
    "config_versions", "workflows",
]


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def connect(path: str) -> sqlite3.Connection:
    _ensure_parent(path)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def reset_analytics(conn: sqlite3.Connection) -> None:
    """Reset world-scoped state after the simulator starts a new world.

    Decisions, counterfactuals and credit history are deliberately kept: the
    audit trail is not deleted, it records that a new world was observed.
    """
    with transaction(conn):
        for table in ANALYTICS_TABLES:
            conn.execute(f"DELETE FROM {table}")


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    val = value if isinstance(value, str) else json.dumps(value, default=str)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, val),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    raw = row["value"]
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def upsert_many(conn: sqlite3.Connection, table: str, columns: list[str],
                rows: Iterable[tuple], key: list[str], add: list[str]) -> None:
    """Bulk INSERT ... ON CONFLICT DO UPDATE with additive counters."""
    rows = list(rows)
    if not rows:
        return
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conflict = ", ".join(key)
    sets = ", ".join(f"{c}={c}+excluded.{c}" for c in add)
    sql = (f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
           f"ON CONFLICT({conflict}) DO UPDATE SET {sets}")
    conn.executemany(sql, rows)


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def load_notes(conn: sqlite3.Connection, workflow_id: str) -> dict:
    row = conn.execute("SELECT notes_json FROM workflow_state WHERE workflow_id=?",
                       (workflow_id,)).fetchone()
    if row is None or not row["notes_json"]:
        return {}
    try:
        value = json.loads(row["notes_json"])
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def save_notes(conn: sqlite3.Connection, workflow_id: str, notes: dict,
               **fields: Any) -> None:
    """Persist small mutable controller learning state.

    This table is not the audit trail; decisions remain append-only in
    ``decisions``.  Losing this row costs re-learning, never a silent apply.
    """
    cols = {"notes_json": json_dumps(notes)}
    cols.update({k: v for k, v in fields.items() if v is not None})
    names = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{k}=excluded.{k}" for k in cols if k != "workflow_id")
    conn.execute(
        f"INSERT INTO workflow_state(workflow_id,{names}) VALUES(?,{placeholders}) "
        f"ON CONFLICT(workflow_id) DO UPDATE SET {updates}",
        (workflow_id, *cols.values()))
