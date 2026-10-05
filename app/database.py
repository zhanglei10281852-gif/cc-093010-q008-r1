from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.clock import to_storage, utc_now


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "health-innovation.db"
_local = threading.local()


SCHEMA = r'''
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    manager TEXT NOT NULL,
    phone TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    department_id INTEGER REFERENCES departments(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','locked')),
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    password_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_system INTEGER NOT NULL DEFAULT 0 CHECK(is_system IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    resource TEXT NOT NULL,
    action TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(role_id, permission_id)
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    assigned_by INTEGER REFERENCES users(id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(user_id, role_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_digest TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    client_label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES users(id),
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('success','denied','failure')),
    before_json TEXT,
    after_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
CREATE TABLE IF NOT EXISTS idempotency_records (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);
CREATE TABLE IF NOT EXISTS department_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    title TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, department_id, starts_at)
);
CREATE TABLE IF NOT EXISTS background_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    deduplication_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_at TEXT,
    locked_by TEXT,
    result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON background_jobs(status,available_at);

CREATE TABLE IF NOT EXISTS health_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    organization TEXT NOT NULL,
    origin_country TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('康复设备','辅助诊断','数字疗法','慢病管理','数字中医','健康消费')),
    intended_use TEXT NOT NULL,
    intended_populations_json TEXT NOT NULL DEFAULT '[]',
    risk_level TEXT NOT NULL CHECK(risk_level IN ('low','medium','high')),
    regulatory_status TEXT NOT NULL DEFAULT '展示' CHECK(regulatory_status IN ('展示','研究','已注册','暂停')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    site_type TEXT NOT NULL CHECK(site_type IN ('展会体验点','医院','康复机构','研究机构','产业伙伴')),
    region TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    served_populations_json TEXT NOT NULL DEFAULT '[]',
    max_concurrent INTEGER NOT NULL DEFAULT 1 CHECK(max_concurrent > 0),
    ethics_prepared_level TEXT NOT NULL DEFAULT 'none' CHECK(ethics_prepared_level IN ('none','basic','full')),
    supported_risk_levels_json TEXT NOT NULL DEFAULT '["low","medium","high"]',
    open_windows_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','closed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES health_products(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL CHECK(evidence_type IN ('临床','性能','安全','合规','体验')),
    title TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_region TEXT NOT NULL,
    version TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'submitted' CHECK(status IN ('submitted','accepted','rejected','superseded')),
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    UNIQUE(product_id, evidence_type, version, content_digest)
);
CREATE TABLE IF NOT EXISTS public_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES health_products(id) ON DELETE CASCADE,
    site_id INTEGER NOT NULL REFERENCES pilot_sites(id) ON DELETE CASCADE,
    session_reference TEXT NOT NULL,
    audience_type TEXT NOT NULL CHECK(audience_type IN ('公众','临床人员','采购商','产业伙伴')),
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    tags_json TEXT NOT NULL DEFAULT '[]',
    comment TEXT NOT NULL DEFAULT '',
    contact_digest TEXT NOT NULL DEFAULT '',
    consent_to_follow_up INTEGER NOT NULL DEFAULT 0 CHECK(consent_to_follow_up IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, session_reference, audience_type, contact_digest)
);

CREATE TABLE IF NOT EXISTS pilot_protocols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    capability TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    parameter_schema_json TEXT NOT NULL,
    default_parameters_json TEXT NOT NULL DEFAULT '{}',
    max_runtime_seconds INTEGER NOT NULL CHECK(max_runtime_seconds > 0),
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('user','role','project')),
    subject_key TEXT NOT NULL,
    max_queued INTEGER NOT NULL CHECK(max_queued >= 0),
    max_running INTEGER NOT NULL CHECK(max_running >= 0),
    daily_submissions INTEGER NOT NULL CHECK(daily_submissions >= 0),
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_type, subject_key)
);
CREATE TABLE IF NOT EXISTS pilot_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_id INTEGER NOT NULL REFERENCES pilot_protocols(id) ON DELETE RESTRICT,
    project_code TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    parameter_digest TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','cancel_requested','cancelled','succeeded','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    available_at TEXT NOT NULL,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    current_observation_version INTEGER,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requested_by, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_pilot_queue ON pilot_sessions(status,priority DESC,available_at,created_at);
CREATE TABLE IF NOT EXISTS pilot_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES pilot_sessions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    observation_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    observation_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, version)
);
CREATE TABLE IF NOT EXISTS pilot_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES pilot_sessions(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    batch_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pilot_interventions ON pilot_interventions(session_id,id);

CREATE TABLE IF NOT EXISTS pilot_matchings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matching_code TEXT NOT NULL UNIQUE,
    product_id INTEGER NOT NULL REFERENCES health_products(id) ON DELETE RESTRICT,
    site_id INTEGER NOT NULL REFERENCES pilot_sites(id) ON DELETE RESTRICT,
    slot_quantity INTEGER NOT NULL DEFAULT 1 CHECK(slot_quantity > 0),
    status TEXT NOT NULL DEFAULT 'reserved' CHECK(status IN (
        'reserved','hospital_accepted','hospital_conditional','hospital_rejected',
        'confirmed','enterprise_declined','expired','revoked'
    )),
    requested_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    hospital_contact TEXT NOT NULL DEFAULT '',
    enterprise_contact TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL,
    hospital_responded_at TEXT NOT NULL DEFAULT '',
    hospital_condition TEXT NOT NULL DEFAULT '',
    enterprise_responded_at TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT NOT NULL DEFAULT '',
    released_at TEXT NOT NULL DEFAULT '',
    release_reason TEXT NOT NULL DEFAULT '',
    last_actor TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matchings_status ON pilot_matchings(status,expires_at);
CREATE INDEX IF NOT EXISTS idx_matchings_site ON pilot_matchings(site_id,status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_matchings_idempotency
    ON pilot_matchings(requested_by, idempotency_key) WHERE idempotency_key<>'';
CREATE UNIQUE INDEX IF NOT EXISTS idx_matchings_one_active_pair
    ON pilot_matchings(product_id,site_id)
    WHERE status IN ('reserved','hospital_accepted','hospital_conditional');
CREATE TABLE IF NOT EXISTS pilot_matching_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matching_id INTEGER NOT NULL REFERENCES pilot_matchings(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(matching_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_matching_events_time ON pilot_matching_events(created_at,id);
CREATE TABLE IF NOT EXISTS pilot_matching_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matching_id INTEGER NOT NULL REFERENCES pilot_matchings(id) ON DELETE CASCADE,
    recipient_role TEXT NOT NULL CHECK(recipient_role IN ('hospital','enterprise','operator')),
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    delivered_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matching_notifications_pending
    ON pilot_matching_notifications(recipient_role, delivered_at, id);
'''


PERMISSIONS = [
    ("users.read", "查看用户", "users", "read"),
    ("users.write", "维护用户", "users", "write"),
    ("roles.read", "查看角色", "roles", "read"),
    ("roles.write", "维护角色", "roles", "write"),
    ("departments.read", "查看协作团队", "departments", "read"),
    ("departments.write", "维护协作团队", "departments", "write"),
    ("catalog.read", "查看健康创新目录", "catalog", "read"),
    ("catalog.write", "维护健康创新目录", "catalog", "write"),
    ("evidence.review", "审阅产品证据", "evidence", "review"),
    ("feedback.read", "查看体验反馈", "feedback", "read"),
    ("matching.read", "查看试点名额匹配", "matching", "read"),
    ("matching.write", "运营试点名额匹配", "matching", "write"),
    ("audit.read", "查看审计", "audit", "read"),
    ("jobs.run", "执行后台任务", "jobs", "run"),
]


def database_path() -> Path:
    raw = os.getenv("HEALTH_INNOVATION_DATABASE_PATH", str(DEFAULT_DB_PATH))
    return Path(raw).expanduser().resolve()


def _create_connection() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def get_connection() -> sqlite3.Connection:
    connection = getattr(_local, "connection", None)
    if connection is None:
        connection = _create_connection()
        _local.connection = connection
    return connection


def close_connection() -> None:
    connection = getattr(_local, "connection", None)
    if connection is not None:
        connection.close()
        _local.connection = None


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    connection = get_connection()
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_columns(connection: sqlite3.Connection) -> None:
    """对已存在的旧版数据库补齐新增列；全新数据库的建表语句已包含这些列。"""
    product_columns = {
        "intended_populations_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    for name, declaration in product_columns.items():
        if name not in _column_names(connection, "health_products"):
            connection.execute(f"ALTER TABLE health_products ADD COLUMN {name} {declaration}")
    site_columns = {
        "served_populations_json": "TEXT NOT NULL DEFAULT '[]'",
        "ethics_prepared_level": "TEXT NOT NULL DEFAULT 'none'",
        "supported_risk_levels_json": "TEXT NOT NULL DEFAULT '[\"low\",\"medium\",\"high\"]'",
        "open_windows_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    for name, declaration in site_columns.items():
        if name not in _column_names(connection, "pilot_sites"):
            connection.execute(f"ALTER TABLE pilot_sites ADD COLUMN {name} {declaration}")


def init_db() -> None:
    now = to_storage(utc_now())
    with transaction(immediate=True) as connection:
        connection.executescript(SCHEMA)
        _migrate_columns(connection)
        connection.execute("PRAGMA user_version=3")
        for code, name, resource, action in PERMISSIONS:
            connection.execute(
                "INSERT OR IGNORE INTO permissions(code,name,resource,action) VALUES(?,?,?,?)",
                (code, name, resource, action),
            )
        roles = [
            ("administrator", "系统管理员", "拥有全部系统权限"),
            ("operator", "试点运营员", "维护目录、场地和体验场次"),
            ("reviewer", "证据审阅员", "审阅产品证据与体验反馈"),
            ("auditor", "审计查看员", "只读查看运行与审计记录"),
        ]
        for code, name, description in roles:
            connection.execute(
                "INSERT OR IGNORE INTO roles(code,name,description,is_system,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                (code, name, description, now, now),
            )
        administrator = connection.execute("SELECT id FROM roles WHERE code='administrator'").fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO role_permissions(role_id,permission_id,granted_at) SELECT ?,id,? FROM permissions",
            (administrator, now),
        )
        operator = connection.execute("SELECT id FROM roles WHERE code='operator'").fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO role_permissions(role_id,permission_id,granted_at) SELECT ?,id,? FROM permissions "
            "WHERE code IN ('catalog.read','catalog.write','evidence.review','feedback.read','matching.read','matching.write','jobs.run')",
            (operator, now),
        )


def migrate_db() -> None:
    init_db()
