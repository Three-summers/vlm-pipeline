"""SQLite 数据层：建表、连接、通用 DAO。

替代参考文档中的 MySQL——离线单机环境无 MySQL 服务，SQLite(WAL) 足以支撑
单机多任务的告警写入与查询。引擎进程与 Web 进程共用此文件同一个 .db。
"""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_DB = str(Path(__file__).resolve().parent.parent / "data" / "vlmp.db")

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',      -- admin | operator | viewer
    display_name TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,                        -- rtsp | video | image
    uri TEXT NOT NULL,
    location TEXT DEFAULT '',
    note TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_check_at TEXT DEFAULT '',
    last_check_ok INTEGER DEFAULT 0,
    last_check_msg TEXT DEFAULT '',
    thumb_path TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS algorithms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    weights_path TEXT NOT NULL,
    framework TEXT NOT NULL DEFAULT 'ultralytics',
    classes_json TEXT DEFAULT '[]',            -- [{"id":0,"name":"person"}, ...]
    note TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vlm_endpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    api_key TEXT DEFAULT 'EMPTY',
    model TEXT NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_check_at TEXT DEFAULT '',
    last_check_ok INTEGER DEFAULT 0,
    last_check_msg TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    filename TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    content TEXT NOT NULL,
    chunk_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL REFERENCES rule_docs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    terms_json TEXT NOT NULL DEFAULT '{}'      -- {term: tf}
);
CREATE INDEX IF NOT EXISTS idx_chunk_doc ON rule_chunks(doc_id);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id),
    algorithm_id INTEGER REFERENCES algorithms(id),
    endpoint_id INTEGER REFERENCES vlm_endpoints(id),
    mode TEXT NOT NULL DEFAULT 'small_crop',
    config_json TEXT NOT NULL DEFAULT '{}',
    rule_doc_ids TEXT DEFAULT '',              -- "1,3"
    autostart INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    pid INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'starting',   -- starting|running|stopped|error|finished
    started_at TEXT NOT NULL,
    stopped_at TEXT DEFAULT '',
    heartbeat_at TEXT DEFAULT '',
    stats_json TEXT DEFAULT '{}',
    last_error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_run_task ON task_runs(task_id, id DESC);

CREATE TABLE IF NOT EXISTS alarms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    run_id INTEGER DEFAULT 0,
    is_alarm INTEGER NOT NULL DEFAULT 1,
    action TEXT DEFAULT '',
    confidence REAL DEFAULT 0,
    description TEXT DEFAULT '',
    det_label TEXT DEFAULT '',
    det_conf REAL DEFAULT 0,
    track_id INTEGER DEFAULT -1,
    box_json TEXT DEFAULT '[]',
    frame_idx INTEGER DEFAULT 0,
    stream_ts REAL DEFAULT 0,
    alarm_time TEXT NOT NULL,
    alarm_ts REAL NOT NULL,
    mode TEXT DEFAULT '',
    snapshots_json TEXT DEFAULT '[]',
    clip_path TEXT DEFAULT '',
    vlm_latency REAL DEFAULT 0,
    vlm_raw TEXT DEFAULT '',
    ack_by TEXT DEFAULT '',
    ack_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_alarm_task_time ON alarms(task_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_alarm_time ON alarms(alarm_ts DESC);
CREATE INDEX IF NOT EXISTS idx_alarm_isalarm ON alarms(is_alarm, id DESC);

CREATE TABLE IF NOT EXISTS push_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'POST',
    headers_json TEXT DEFAULT '{}',
    body_template TEXT DEFAULT '',
    only_alarm INTEGER NOT NULL DEFAULT 1,
    task_filter TEXT DEFAULT '',               -- 空=全部；否则 "1,2"
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS push_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alarm_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pushlog ON push_logs(id DESC);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    scopes TEXT NOT NULL DEFAULT 'read',       -- read | read,write
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_used_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT DEFAULT '新会话',
    username TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    refs_json TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chatmsg ON chat_messages(session_id, id);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT DEFAULT '',
    ip TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit ON audit_logs(id DESC);
"""

DEFAULT_SETTINGS = {
    "site_name": "AI 视频数据分析系统",
    "retention_days": "30",              # 告警保留天数，0=不清理
    "snapshot_quality": "85",
    "default_frame_stride": "5",
    "wall_columns": "2",
    "alarm_page_size": "20",
    "chat_max_alarms": "30",
    "rag_top_k": "3",
    "gpu_index": "0",
}


def db_path() -> str:
    return os.environ.get("VLMP_DB", DEFAULT_DB)


def connect(path: str = None) -> sqlite3.Connection:
    """每线程一个连接。WAL + busy_timeout 允许引擎与 Web 并发写。"""
    path = path or db_path()
    key = f"conn::{path}"
    conn = getattr(_local, key, None)
    if conn is None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        setattr(_local, key, conn)
    return conn


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def init_db(path: str = None):
    conn = connect(path)
    conn.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",
            (k, v, now_str()))
    conn.commit()
    return conn


# --- 通用小工具 ---------------------------------------------------------

def query(sql, args=(), path=None):
    return connect(path).execute(sql, args).fetchall()


def query_one(sql, args=(), path=None):
    return connect(path).execute(sql, args).fetchone()


def execute(sql, args=(), path=None) -> int:
    conn = connect(path)
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.lastrowid


def executemany(sql, seq_of_args, path=None) -> int:
    """单事务批量执行，返回影响行数。大批量插入（如文档分块索引）用它。"""
    conn = connect(path)
    cur = conn.executemany(sql, seq_of_args)
    conn.commit()
    return cur.rowcount


def row_to_dict(row) -> dict:
    return dict(row) if row is not None else None


def rows_to_dicts(rows) -> list:
    return [dict(r) for r in rows]


def get_setting(key, default="", path=None):
    r = query_one("SELECT value FROM settings WHERE key=?", (key,), path)
    return r["value"] if r else DEFAULT_SETTINGS.get(key, default)


def set_setting(key, value, path=None):
    execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), now_str()), path)


def audit(username, action, detail="", ip="", path=None):
    execute("INSERT INTO audit_logs(username,action,detail,ip,created_at) VALUES(?,?,?,?,?)",
            (username, action, detail[:500], ip, now_str()), path)


def task_config(task_row) -> dict:
    """任务的 config_json 解析为 dict。"""
    try:
        return json.loads(task_row["config_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def insert_alarm(rec: dict, path=None) -> int:
    """引擎侧写入一条告警/巡检记录，返回 alarm id。"""
    return execute(
        """INSERT INTO alarms(task_id,run_id,is_alarm,action,confidence,description,
               det_label,det_conf,track_id,box_json,frame_idx,stream_ts,alarm_time,alarm_ts,
               mode,snapshots_json,clip_path,vlm_latency,vlm_raw)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rec["task_id"], rec.get("run_id", 0), 1 if rec.get("is_alarm") else 0,
         rec.get("action", ""), rec.get("confidence", 0.0), rec.get("description", ""),
         rec.get("det_label", ""), rec.get("det_conf", 0.0), rec.get("track_id", -1),
         json.dumps(rec.get("box", []), ensure_ascii=False), rec.get("frame_idx", 0),
         rec.get("stream_ts", 0.0), rec.get("alarm_time", now_str()),
         rec.get("alarm_ts", time.time()), rec.get("mode", ""),
         json.dumps(rec.get("snapshots", []), ensure_ascii=False),
         rec.get("clip_path", ""), rec.get("vlm_latency", 0.0),
         (rec.get("vlm_raw", "") or "")[:4000]),
        path)
