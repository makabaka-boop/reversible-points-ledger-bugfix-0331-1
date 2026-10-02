"""故障回滚测试：

* 批次记账中途“磁盘故障”（在第 N 条分录写入时抛错）→ 整批零残留；
* 批次内多笔交易合计透支 → 整批不记，而不是记一半；
* 冲正中途失败 → 原交易不受影响、不产生半截冲正；
* 模拟进程崩溃（事务未提交即丢弃连接）→ 重开数据库后数据回滚。
"""
from __future__ import annotations

import pytest

from app import service
from app.db import connect, db_path
from app.errors import InvalidRequest

from .conftest import assert_balances_match, recompute_balances


class FaultyConn:
    """连接代理：在第 fail_at 次写入 entries 时抛出“磁盘 I/O 故障”。

    sqlite3.Connection 的内置方法不可在实例上替换，因此用代理包装。
    """

    def __init__(self, real, fail_at: int):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_fail_at", fail_at)
        object.__setattr__(self, "_n", 0)

    def execute(self, sql, params=None):
        if sql.lstrip().upper().startswith("INSERT INTO ENTRIES"):
            object.__setattr__(self, "_n", self._n + 1)
            if self._n == self._fail_at:
                raise RuntimeError("simulated I/O failure while writing entry")
        real = self._real
        return real.execute(sql) if params is None else real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


def test_batch_failure_midway_posts_nothing(conn):
    service.create_account(conn, "A", opening_balance=100)
    service.create_account(conn, "B")
    service.create_account(conn, "C")

    transfers = [
        {"from": "A", "to": "B", "amount": 10},
        {"from": "A", "to": "C", "amount": 5},
        {"from": "B", "to": "C", "amount": 2},
    ]

    before_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    before_balances = recompute_balances(conn)

    for fail_at in (1, 3, 6):  # 第一批分录、中间、最后一条
        with pytest.raises(RuntimeError, match="simulated I/O failure"):
            service.post_batch(FaultyConn(conn, fail_at), f"boom-{fail_at}", transfers)

        # 整批零残留：没有批次、没有交易、没有任何新增分录
        assert conn.execute(
            "SELECT COUNT(*) FROM batches WHERE ref = ?", (f"boom-{fail_at}",)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE ref LIKE ?",
            (f"boom-{fail_at}:%",),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == before_entries
        assert recompute_balances(conn) == before_balances

    # 故障之后库依然可以正常记账
    service.post_batch(conn, "ok-after-failure", transfers)
    assert_balances_match(conn)
    assert service.get_account(conn, "A")["balance"] == 85
    assert service.get_account(conn, "B")["balance"] == 8
    assert service.get_account(conn, "C")["balance"] == 7


def test_batch_combined_overdraft_whole_batch_aborts(conn):
    service.create_account(conn, "A", opening_balance=50)
    service.create_account(conn, "B")
    service.create_account(conn, "C")

    # 单看每笔似乎都能过，但合计 A 为 -5：必须整批拒绝
    with pytest.raises(InvalidRequest):
        service.post_batch(conn, "multi-overdraft", [
            {"from": "A", "to": "B", "amount": 30},
            {"from": "A", "to": "C", "amount": 30},
            {"from": "B", "to": "A", "amount": 5},
        ])

    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1  # 仅 A 的一笔 opening
    assert_balances_match(conn)
    assert recompute_balances(conn) == {"A": 50, "B": 0, "C": 0}


def test_circular_batch_with_tight_balances_commits_cleanly(conn):
    # 先正后负的写入顺序保证：A/B 各只有 10，互转 10 也不会误触透支触发器
    service.create_account(conn, "A", opening_balance=10)
    service.create_account(conn, "B", opening_balance=10)
    service.post_batch(conn, "circle", [
        {"from": "A", "to": "B", "amount": 10},
        {"from": "B", "to": "A", "amount": 10},
    ])
    assert_balances_match(conn)
    assert recompute_balances(conn) == {"A": 10, "B": 10}


def test_reversal_failure_posts_nothing(conn):
    service.create_account(conn, "A", opening_balance=100)
    service.create_account(conn, "B")
    service.post_batch(conn, "b1", [{"from": "A", "to": "B", "amount": 40}])

    before = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    # 冲正第一条反向分录就“故障”
    with pytest.raises(RuntimeError):
        service.post_reversal(FaultyConn(conn, 1), "r-boom", "b1:0")

    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE reversal_of IS NOT NULL"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == before
    assert_balances_match(conn)
    assert recompute_balances(conn) == {"A": 60, "B": 40}

    # 原交易仍可被正常冲正一次
    service.post_reversal(conn, "r1", "b1:0")
    assert recompute_balances(conn) == {"A": 100, "B": 0}


def test_uncommitted_transaction_vanishes_after_connection_loss(conn):
    """模拟进程崩溃：拿到写锁写了一半，未提交直接丢弃连接。"""
    service.create_account(conn, "A", opening_balance=100)
    path = db_path()

    victim = connect(path)
    victim.execute("BEGIN IMMEDIATE")
    pid = victim.execute("SELECT id FROM periods WHERE status='open'").fetchone()[0]
    aid = victim.execute("SELECT id FROM accounts WHERE code='A'").fetchone()[0]
    tid = victim.execute(
        "INSERT INTO transactions (ref,type,period_id) VALUES ('ghost','deposit',?)",
        (pid,),
    ).lastrowid
    victim.execute(
        "INSERT INTO entries (txn_id,account_id,period_id,amount) VALUES (?,?,?,77)",
        (tid, aid, pid),
    )
    # “崩溃”：不 COMMIT，直接关闭
    victim.close()

    # 全新连接重开数据库：未提交事务必须已回滚
    recovered = connect(path)
    try:
        assert recovered.execute(
            "SELECT COUNT(*) FROM transactions WHERE ref='ghost'"
        ).fetchone()[0] == 0
        bal = recovered.execute(
            "SELECT COALESCE(SUM(amount),0) FROM entries e "
            "JOIN accounts a ON a.id=e.account_id WHERE a.code='A'"
        ).fetchone()[0]
        assert bal == 100
    finally:
        recovered.close()
