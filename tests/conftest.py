"""共享夹具与“从原始分录重算余额”的对照函数。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 独立测试库：尊重外部注入的 LEDGER_DB_PATH（容器 verify 服务），
# 否则在仓库 data/ 下使用临时测试库。
_TMP_DB = Path(os.environ.get(
    "LEDGER_DB_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "test-ledger.db"),
))
os.environ["LEDGER_DB_PATH"] = str(_TMP_DB)

from app import service  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def conn():
    """每个用例一个全新的数据库文件，避免跨用例状态污染。"""
    if _TMP_DB.exists():
        _TMP_DB.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(_TMP_DB) + suffix)
        if p.exists():
            p.unlink()
    init_db(str(_TMP_DB))
    c = connect(str(_TMP_DB))
    try:
        yield c
    finally:
        c.close()
        if _TMP_DB.exists():
            _TMP_DB.unlink()
        for suffix in ("-wal", "-shm"):
            p = Path(str(_TMP_DB) + suffix)
            if p.exists():
                p.unlink()


@pytest.fixture()
def client():
    if _TMP_DB.exists():
        _TMP_DB.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(_TMP_DB) + suffix)
        if p.exists():
            p.unlink()
    init_db(str(_TMP_DB))
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- 独立对照实现

def recompute_balances(conn) -> dict[str, int]:
    """从原始分录逐行重算每个账户余额（不使用任何缓存/快照）。

    没有任何分录的账户（或分录全部抵消）也以 0 出现，与账户列表口径一致。
    """
    out: dict[str, int] = {
        r["code"]: 0 for r in conn.execute("SELECT code FROM accounts").fetchall()
    }
    rows = conn.execute(
        """
        SELECT a.code, e.amount
        FROM entries e JOIN accounts a ON a.id = e.account_id
        ORDER BY e.id
        """
    ).fetchall()
    for row in rows:
        out[row["code"]] += row["amount"]
    return out


def stored_balances(conn) -> dict[str, int]:
    return {r["code"]: r["balance"] for r in service.list_accounts(conn)}


def assert_balances_match(conn) -> None:
    """API 暴露的余额必须等于从原始分录重算的余额，且全部为非负整数。"""
    recomputed = recompute_balances(conn)
    stored = stored_balances(conn)
    assert stored == recomputed
    assert all(isinstance(v, int) and v >= 0 for v in stored.values())


def open_period_id(conn) -> int:
    return conn.execute(
        "SELECT id FROM periods WHERE status='open' ORDER BY id LIMIT 1"
    ).fetchone()[0]
