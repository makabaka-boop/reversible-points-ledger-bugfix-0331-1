"""重放测试：以“从原始分录重算的余额”为唯一对照，覆盖
开账、批次转账、冲正、关期快照、关期后补写、不可变性等路径。
"""
from __future__ import annotations

import sqlite3

import pytest

from app import service
from app.errors import Conflict, InvalidRequest, NotFound

from .conftest import assert_balances_match, open_period_id


def test_batch_transfer_balances_equal_replay(conn):
    service.create_account(conn, "A", opening_balance=100)
    service.create_account(conn, "B", opening_balance=20)
    service.create_account(conn, "C")

    service.post_batch(conn, "b1", [
        {"from": "A", "to": "B", "amount": 30},
        {"from": "A", "to": "C", "amount": 20},
        {"from": "B", "to": "C", "amount": 10},
    ])

    # 逐账户重算对照
    assert_balances_match(conn)
    assert service.get_account(conn, "A")["balance"] == 50
    assert service.get_account(conn, "B")["balance"] == 40
    assert service.get_account(conn, "C")["balance"] == 30

    # 每笔转账恰好两条金额相反的分录
    rows = conn.execute(
        """
        SELECT t.ref,
               (SELECT COUNT(*) FROM entries e WHERE e.txn_id = t.id) AS n,
               (SELECT SUM(amount) FROM entries e WHERE e.txn_id = t.id) AS s
        FROM transactions t WHERE t.type = 'transfer'
        """
    ).fetchall()
    assert {r["n"] for r in rows} == {2}
    assert {r["s"] for r in rows} == {0}


def test_reversal_replays_into_current_period_and_keeps_originals(conn):
    service.create_account(conn, "A", opening_balance=100)
    service.create_account(conn, "B")
    service.post_batch(conn, "b1", [{"from": "A", "to": "B", "amount": 40}])

    # 关第一期；旧交易 b1:0 属于已关闭期
    close = service.close_current_period(conn)
    closed_id = close["closed_period"]["id"]
    assert open_period_id(conn) == closed_id + 1

    # 冲正旧交易：反向分录只能进当前开放期，原分录保留
    rev = service.post_reversal(conn, "r1", "b1:0")
    assert {e["period_id"] for e in rev["entries"]} == {open_period_id(conn)}
    assert open_period_id(conn) != closed_id

    n_original_entries = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE period_id = ?", (closed_id,)
    ).fetchone()[0]
    # 原两条转账分录 + A 的一条开账分录，均仍在已关闭期，未被删除
    assert n_original_entries == 3

    assert_balances_match(conn)
    assert service.get_account(conn, "A")["balance"] == 100
    assert service.get_account(conn, "B")["balance"] == 0

    # 快照必须等于截至该期从原始分录重算的余额
    snap = service.get_period_snapshot(conn, closed_id)
    assert {b["code"]: b["balance"] for b in snap["balances"]} == {
        "A": 60, "B": 40,
    }


def test_reversal_can_only_succeed_once(conn):
    service.create_account(conn, "A", opening_balance=100)
    service.create_account(conn, "B")
    service.post_batch(conn, "b1", [{"from": "A", "to": "B", "amount": 10}])

    service.post_reversal(conn, "r1", "b1:0")
    with pytest.raises(Conflict):
        service.post_reversal(conn, "r2", "b1:0")
    # 换个 ref 也不行：裁决依据是原交易 id，不是冲正交易 ref
    with pytest.raises(Conflict):
        service.post_reversal(conn, "r1", "b1:0")

    assert_balances_match(conn)
    # 反向交易确实只有一条
    n = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE reversal_of IS NOT NULL"
    ).fetchone()[0]
    assert n == 1


def test_reversal_must_reference_existing_original(conn):
    service.create_account(conn, "A", opening_balance=100)
    with pytest.raises(NotFound):
        service.post_reversal(conn, "r1", "does-not-exist")

    # 不能冲正另一笔冲正
    service.create_account(conn, "B")
    service.post_batch(conn, "b1", [{"from": "A", "to": "B", "amount": 10}])
    service.post_reversal(conn, "r1", "b1:0")
    with pytest.raises(InvalidRequest):
        service.post_reversal(conn, "r2", "r1")


def test_closed_period_rejects_backfill_and_matches_replay(conn):
    service.create_account(conn, "A", opening_balance=100)
    service.create_account(conn, "B")
    service.post_batch(conn, "b1", [{"from": "A", "to": "B", "amount": 30}])
    close = service.close_current_period(conn)
    closed_id = close["closed_period"]["id"]

    # 应用层：关期后的新批次只能进新期
    service.post_batch(conn, "b2", [{"from": "A", "to": "B", "amount": 10}])
    b2_period = conn.execute(
        "SELECT period_id FROM transactions WHERE ref = 'b2:0'"
    ).fetchone()[0]
    assert b2_period != closed_id

    # 数据库层：直接试图往已关闭期塞分录必须被触发器拒绝
    conn.execute("BEGIN IMMEDIATE")
    txn_id = conn.execute(
        "INSERT INTO transactions (ref, type, batch_id, period_id) "
        "VALUES ('hack:0', 'transfer', 1, ?)",
        (closed_id,),
    ).lastrowid
    account_a = conn.execute("SELECT id FROM accounts WHERE code='A'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO entries (txn_id, account_id, period_id, amount) "
            "VALUES (?, ?, ?, 1)",
            (txn_id, account_a, closed_id),
        )
    conn.execute("ROLLBACK")  # 模拟故障：整笔脏事务回滚，不留半成品

    # 脏事务回滚后库仍然自洽
    assert_balances_match(conn)
    assert service.get_account(conn, "A")["balance"] == 60
    assert service.get_account(conn, "B")["balance"] == 40

    # 快照 = 从截至关闭期的原始分录重算
    snap = service.get_period_snapshot(conn, closed_id)
    replay_closed = conn.execute(
        """
        SELECT a.code, COALESCE(SUM(e.amount), 0) AS bal
        FROM accounts a
        LEFT JOIN entries e ON e.account_id = a.id AND e.period_id = ?
        GROUP BY a.id
        """,
        (closed_id,),
    ).fetchall()
    assert {r["code"]: r["bal"] for r in replay_closed} == {
        b["code"]: b["balance"] for b in snap["balances"]
    }

    # 累计余额 = 逐期快照滚动（第一期快照 + 第二期发生额 = 当前余额）
    current = {b["code"]: b["balance"] for b in snap["balances"]}
    for code in list(current):
        current[code] += conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM entries e "
            "JOIN accounts a ON a.id=e.account_id WHERE a.code=?",
            (code,),
        ).fetchone()[0] - conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM entries e "
            "JOIN accounts a ON a.id=e.account_id "
            "WHERE a.code=? AND e.period_id=?",
            (code, closed_id),
        ).fetchone()[0]
    assert current == {a["code"]: a["balance"] for a in service.list_accounts(conn)}


def test_entries_and_snapshots_are_immutable(conn):
    service.create_account(conn, "A", opening_balance=100)
    service.close_current_period(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE entries SET amount = 999")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM entries")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE transactions SET type='deposit' WHERE type='opening'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM transactions")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE period_balances SET balance = balance + 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM period_balances")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE periods SET status='open' WHERE status='closed'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM periods")

    assert_balances_match(conn)


def test_integer_amounts_and_non_negative(conn):
    service.create_account(conn, "A", opening_balance=10)
    service.create_account(conn, "B")

    # 0 在 service 层被数据库 CHECK 拒绝
    conn.execute("BEGIN IMMEDIATE")
    pid = open_period_id(conn)
    aid = conn.execute("SELECT id FROM accounts WHERE code='A'").fetchone()[0]
    tid = conn.execute(
        "INSERT INTO transactions (ref,type,period_id) VALUES ('z','deposit',?)",
        (pid,),
    ).lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO entries (txn_id,account_id,period_id,amount) VALUES (?,?,?,0)",
            (tid, aid, pid),
        )
    conn.execute("ROLLBACK")

    # 透支：批次整批拒绝
    with pytest.raises(InvalidRequest):
        service.post_batch(conn, "bad", [{"from": "A", "to": "B", "amount": 11}])
    assert_balances_match(conn)
    assert service.get_account(conn, "A")["balance"] == 10

    # 数据库最后防线：直接构造使余额为负的分录
    conn.execute("BEGIN IMMEDIATE")
    tid = conn.execute(
        "INSERT INTO transactions (ref,type,period_id) VALUES ('z2','deposit',?)",
        (pid,),
    ).lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO entries (txn_id,account_id,period_id,amount) VALUES (?,?,?,-11)",
            (tid, aid, pid),
        )
    conn.execute("ROLLBACK")
    assert_balances_match(conn)
