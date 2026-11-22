"""SQLite 层：建表、写入、查询。零外部依赖。"""
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "quickreg.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL,
    password    TEXT,
    name        TEXT,
    state       TEXT NOT NULL DEFAULT 'created',
    step        TEXT,
    fail_reason TEXT,
    proxy       TEXT,
    profile_id  TEXT,
    retries     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);

CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    task_id INTEGER,
    level   TEXT NOT NULL,
    msg     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pools (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'idle',
    used_count INTEGER NOT NULL DEFAULT 0,
    success    INTEGER NOT NULL DEFAULT 0,
    fail       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS counters (
    key   TEXT PRIMARY KEY,
    value REAL NOT NULL
);
"""


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def log(msg, level="info", task_id=None):
    with conn() as c:
        c.execute("INSERT INTO logs (ts, task_id, level, msg) VALUES (?,?,?,?)",
                  (time.time(), task_id, level, msg))


def create_task(email, password=None, name=None, proxy=None, profile_id=None):
    now = time.time()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO tasks (email,password,name,state,step,proxy,profile_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (email, password, name, "created", "created", proxy, profile_id, now, now))
        return cur.lastrowid


def update_task(tid, **kw):
    if not kw:
        return
    kw["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in kw)
    with conn() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE id=?", list(kw.values()) + [tid])


def get_task(tid):
    with conn() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None


def list_tasks(state=None, limit=200):
    q = "SELECT * FROM tasks"
    args = []
    if state:
        q += " WHERE state=?"
        args.append(state)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def stats():
    with conn() as c:
        rows = c.execute("SELECT state, COUNT(*) n FROM tasks GROUP BY state").fetchall()
        by = {r["state"]: r["n"] for r in rows}
        total = sum(by.values())
        ok = by.get("account_active", 0)
        fails = c.execute(
            "SELECT fail_reason, COUNT(*) n FROM tasks WHERE fail_reason IS NOT NULL"
            " GROUP BY fail_reason ORDER BY n DESC").fetchall()
        dur = c.execute(
            "SELECT AVG(duration_ms) a FROM tasks WHERE duration_ms IS NOT NULL").fetchone()["a"]
        return {
            "total": total,
            "by_state": by,
            "success": ok,
            "failed": sum(v for k, v in by.items() if k in ("blocked", "verify_failed", "dead")),
            "success_rate": round(ok / total * 100, 1) if total else 0.0,
            "avg_duration_ms": int(dur) if dur else 0,
            "fail_reasons": [{"reason": r["fail_reason"], "n": r["n"]} for r in fails],
        }


def tail_logs(after_id=0, limit=200):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM logs WHERE id>? ORDER BY id ASC LIMIT ?", (after_id, limit)).fetchall()]


def seed_pools():
    """按配置写入真实域名；没有配置就什么都不放，避免用假域名去注册。"""
    try:
        import config
        dom = (config.CFG.get("mail") or {}).get("domain") or ""
    except Exception:
        dom = ""
    if not dom or dom in ("your-domain.com", "example.com"):
        return
    with conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM pools WHERE kind='domain'").fetchone()["n"]
        if n:
            return
        c.execute("INSERT INTO pools (kind,value) VALUES ('domain',?)", (dom,))


def pools():
    with conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM pools ORDER BY kind, id").fetchall()]
    out = {}
    for r in rows:
        out.setdefault(r["kind"], []).append(r)
    return out


def list_resources(kind=None):
    with conn() as c:
        if kind:
            rows = c.execute("SELECT * FROM pools WHERE kind=? ORDER BY id", (kind,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM pools ORDER BY kind, id").fetchall()
        return [dict(r) for r in rows]


def pick_resource(kind):
    """轮询取一个资源并累加使用次数。"""
    with conn() as c:
        r = c.execute(
            "SELECT * FROM pools WHERE kind=? ORDER BY used_count ASC, id ASC LIMIT 1", (kind,)).fetchone()
        if not r:
            return None
        c.execute("UPDATE pools SET used_count=used_count+1, status='busy' WHERE id=?", (r["id"],))
        return dict(r)


def mark_resource(kind, value, ok):
    col = "success" if ok else "fail"
    with conn() as c:
        c.execute(f"UPDATE pools SET {col}={col}+1, status='idle' WHERE kind=? AND value=?",
                  (kind, value))
