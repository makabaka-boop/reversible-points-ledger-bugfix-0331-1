"""SQLite 连接与建库。

每个请求使用独立连接（短事务），写事务一律 ``BEGIN IMMEDIATE``：
SQLite 同一时刻只允许一个写者，转账与关期、并发冲正因此被串行化，
冲突由数据库（UNIQUE / 触发器）裁决，而不是由应用内存状态裁决。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "ledger.db")
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def db_path() -> str:
    import os

    return os.environ.get("LEDGER_DB_PATH", DEFAULT_DB_PATH)


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL + 全同步：崩溃后已提交数据不丢、未提交数据回滚
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: str | None = None) -> None:
    """创建全部表/触发器并确保存在第一个开放期。幂等，可多进程同时调用。"""
    path = path or db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        # 种入第一个开放期（幂等）
        conn.execute(
            """
            INSERT INTO periods (seq, status)
            SELECT 0, 'open'
            WHERE NOT EXISTS (SELECT 1 FROM periods WHERE seq = 0)
            """
        )
    finally:
        conn.close()


def current_open_period(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM periods WHERE status = 'open' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:  # 正常流程下不会发生（关期会同事务开新期）
        raise RuntimeError("no open period")
    return row
