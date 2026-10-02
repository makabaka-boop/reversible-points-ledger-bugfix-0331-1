"""预留（hold）/ 捕获 / 释放的测试。

覆盖场景中的每个失败模式：
* 两笔超额预留不能同时成立，账户可用余额永不为负；
* 普通批量转账 / 冲正不能花掉已预留积分，早先成功的预留必然能捕获；
* 捕获是“状态翻转 + 两条分录”的单事务原子操作，中途故障零残留；
* 重复捕获 / 重复释放幂等；捕获与释放互为终态冲突（409）；
* 预留跨结算期仍有效；捕获分录进入当前开放期；旧期快照冻结 balance 与 held；
* 关键不变量由数据库触发器裁决，绕过应用层的直接 SQL 同样被拒。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import service
from app.db import connect
from app.errors import Conflict, InvalidRequest, LedgerError

from .conftest import assert_balances_match, recompute_balances


class FaultyConn:
    """在第 fail_at 次写入 entries 时抛错的连接代理。"""

    def __init__(self, real, fail_at: int):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_n", 0)
        object.__setattr__(self, "_fail_at", fail_at)

    def execute(self, sql, params=None):
        if sql.lstrip().upper().startswith("INSERT INTO ENTRIES"):
            object.__setattr__(self, "_n", self._n + 1)
            if self._n == self._fail_at:
                raise RuntimeError("simulated I/O failure while writing entry")
        real = self._real
        return real.execute(sql) if params is None else real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


def _setup(conn) -> None:
    service.create_account(conn, "A", opening_balance=100)
    service.create_account(conn, "B")
    service.create_account(conn, "C")


# ---------------------------------------------------------------- 预留占用可用余额

def test_holds_consume_available_not_booked_balance(conn):
    _setup(conn)
    h = service.create_hold(conn, "h1", "A", "B", 80)
    assert h["status"] == "active"

    acc = service.get_account(conn, "A")
    assert acc["balance"] == 100          # 已入账余额不变
    assert acc["held"] == 80
    assert acc["available"] == 20         # 可用余额为正

    # 第二笔 80：可用余额不足，拒绝；账户可用余额绝不为负
    with pytest.raises(InvalidRequest):
        service.create_hold(conn, "h2", "A", "C", 80)
    assert conn.execute("SELECT COUNT(*) FROM holds WHERE ref='h2'").fetchone()[0] == 0
    again = service.get_account(conn, "A")
    assert again["held"] == 80 and again["available"] == 20

    # 恰好占满可用余额可以成功
    service.create_hold(conn, "h3", "A", "C", 20)
    acc = service.get_account(conn, "A")
    assert acc["held"] == 100 and acc["available"] == 0

    # 再预留 1 分也不行
    with pytest.raises(InvalidRequest):
        service.create_hold(conn, "h4", "A", "C", 1)

    # 预留不产生任何分录，重算余额不变
    assert_balances_match(conn)
    assert recompute_balances(conn) == {"A": 100, "B": 0, "C": 0}


def test_duplicate_hold_ref_conflicts(conn):
    _setup(conn)
    service.create_hold(conn, "h1", "A", "B", 10)
    with pytest.raises(Conflict):
        service.create_hold(conn, "h1", "A", "C", 10)


def test_hold_validation(conn):
    _setup(conn)
    with pytest.raises(InvalidRequest):
        service.create_hold(conn, "h1", "A", "A", 10)
    with pytest.raises(InvalidRequest):
        service.create_hold(conn, "h2", "A", "X", 10)
    with pytest.raises(InvalidRequest):
        service.create_hold(conn, "h3", "A", "B", 0)


# ---------------------------------------------------------------- 普通转账不能花掉预留

def test_batch_transfer_cannot_spend_held_funds(conn):
    _setup(conn)
    service.create_hold(conn, "h1", "A", "B", 80)
    service.create_hold(conn, "h2", "A", "C", 15)  # 可用仅剩 5

    for amount in (6, 21, 100):
        with pytest.raises(InvalidRequest):
            service.post_batch(conn, f"b-{amount}",
                               [{"from": "A", "to": "B", "amount": amount}])
        assert conn.execute(
            "SELECT COUNT(*) FROM batches WHERE ref = ?", (f"b-{amount}",)
        ).fetchone()[0] == 0

    # 可用 5 以内可以
    service.post_batch(conn, "b-ok", [{"from": "A", "to": "B", "amount": 5}])

    # 早先成功的两笔预留现在必然都能捕获——预留时已锁定额度
    assert service.capture_hold(conn, "h1")["status"] == "captured"
    assert service.capture_hold(conn, "h2")["status"] == "captured"
    assert_balances_match(conn)
    assert recompute_balances(conn) == {"A": 0, "B": 85, "C": 15}


def test_batch_combined_overdraft_against_holds_aborts_whole_batch(conn):
    _setup(conn)
    service.create_hold(conn, "h1", "A", "B", 80)  # A 可用 20
    with pytest.raises(InvalidRequest):
        service.post_batch(conn, "multi", [
            {"from": "A", "to": "C", "amount": 15},
            {"from": "A", "to": "C", "amount": 10},
        ])
    assert conn.execute("SELECT COUNT(*) FROM batches WHERE ref='multi'").fetchone()[0] == 0
    assert_balances_match(conn)


def test_inbound_transfer_then_hold_funds_are_both_usable(conn):
    """贷方入账与预留互不干扰：B 收到钱后可被其自己的预留占用。"""
    _setup(conn)
    service.create_hold(conn, "h1", "A", "B", 60)
    service.post_batch(conn, "b1", [{"from": "A", "to": "B", "amount": 40}])  # A=0 B=40
    service.create_hold(conn, "hb", "B", "C", 40)
    service.capture_hold(conn, "hb")    # B=0 C=40
    service.capture_hold(conn, "h1")    # A=0 B=100
    assert recompute_balances(conn) == {"A": 0, "B": 60, "C": 40}


# ---------------------------------------------------------------- 捕获：幂等 / 原子 / 终态

def test_capture_posts_transfer_and_frees_the_hold(conn):
    _setup(conn)
    service.create_hold(conn, "h1", "A", "B", 70)
    out = service.capture_hold(conn, "h1")
    assert out["status"] == "captured"
    assert out["transfer_ref"] == "hold:h1"

    txn = service.get_transaction_by_ref(conn, "hold:h1")
    assert txn["type"] == "transfer"
    assert {e["amount"] for e in txn["entries"]} == {70, -70}

    acc = service.get_account(conn, "A")
    assert acc["balance"] == 30 and acc["held"] == 0 and acc["available"] == 30
    assert service.get_account(conn, "B")["balance"] == 70


def test_capture_is_idempotent_under_duplicate_requests(conn):
    _setup(conn)
    service.create_hold(conn, "h1", "A", "B", 40)
    first = service.capture_hold(conn, "h1")
    second = service.capture_hold(conn, "h1")  # 网络重试
    assert first == second
    # 只产生一笔转账（两条分录）
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE ref='hold:h1'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM entries e JOIN transactions t ON t.id=e.txn_id "
        "WHERE t.ref='hold:h1'"
    ).fetchone()[0] == 2
    assert service.get_account(conn, "B")["balance"] == 40


def test_capture_after_release_conflicts_and_vice_versa(conn):
    _setup(conn)
    service.create_hold(conn, "hc", "A", "B", 10)
    service.create_hold(conn, "hr", "A", "B", 10)
    service.release_hold(conn, "hr")
    with pytest.raises(Conflict):
        service.capture_hold(conn, "hr")
    service.capture_hold(conn, "hc")
    with pytest.raises(Conflict):
        service.release_hold(conn, "hc")
    # 重复释放幂等
    assert service.release_hold(conn, "hr")["status"] == "released"


def test_capture_failure_midway_leaves_nothing_and_hold_active(conn):
    _setup(conn)
    service.create_hold(conn, "h1", "A", "B", 30)
    before = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    for fail_at in (1, 2):  # 第一条（贷）或第二条（借）分录写入时故障
        with pytest.raises(RuntimeError, match="simulated I/O failure"):
            service.capture_hold(FaultyConn(conn, fail_at), "h1")
        assert service.get_hold(conn, "h1")["status"] == "active"
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE ref='hold:h1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM batches WHERE ref='hold:h1'"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == before
        assert_balances_match(conn)

    # 故障后重试必须成功（且只成功一次）
    assert service.capture_hold(conn, "h1")["status"] == "captured"
    assert recompute_balances(conn) == {"A": 70, "B": 30, "C": 0}


def test_release_returns_availability_without_entries(conn):
    _setup(conn)
    service.create_hold(conn, "h1", "A", "B", 100)
    assert service.get_account(conn, "A")["available"] == 0
    service.release_hold(conn, "h1")
    acc = service.get_account(conn, "A")
    assert acc["held"] == 0 and acc["available"] == 100
    assert recompute_balances(conn) == {"A": 100, "B": 0, "C": 0}


def test_capture_unknown_hold_404(conn):
    _setup(conn)
    from app.errors import NotFound
    with pytest.raises(NotFound):
        service.capture_hold(conn, "ghost")
    with pytest.raises(NotFound):
        service.release_hold(conn, "ghost")


# ---------------------------------------------------------------- 跨结算期

def test_hold_survives_period_close_and_captures_into_new_period(conn):
    _setup(conn)
    service.create_hold(conn, "h1", "A", "B", 60)
    closed = service.close_current_period(conn)
    old_id = closed["closed_period"]["id"]
    new_id = closed["opened_period"]["id"]

    # 旧期快照同时冻结 balance 与 held；available 与当时账户口径一致
    snap = {b["code"]: b for b in service.get_period_snapshot(conn, old_id)["balances"]}
    assert snap["A"]["balance"] == 100
    assert snap["A"]["held"] == 60
    assert snap["A"]["available"] == 40

    # 跨期后预留仍有效，普通转账仍只能动用可用的 40
    with pytest.raises(InvalidRequest):
        service.post_batch(conn, "b1", [{"from": "A", "to": "C", "amount": 41}])
    service.post_batch(conn, "b2", [{"from": "A", "to": "C", "amount": 40}])

    # 捕获分录全部落入新期；旧期快照纹丝不动
    service.capture_hold(conn, "h1")
    txn = service.get_transaction_by_ref(conn, "hold:h1")
    assert {e["period_id"] for e in txn["entries"]} == {new_id}
    snap_after = {b["code"]: b for b in service.get_period_snapshot(conn, old_id)["balances"]}
    assert (snap_after["A"]["balance"], snap_after["A"]["held"]) == (100, 60)

    assert_balances_match(conn)
    assert recompute_balances(conn) == {"A": 0, "B": 60, "C": 40}


def test_release_across_period_does_not_rewrite_closed_snapshot(conn):
    _setup(conn)
    service.create_hold(conn, "h1", "A", "B", 60)
    closed = service.close_current_period(conn)
    old_id = closed["closed_period"]["id"]
    service.release_hold(conn, "h1")  # 在新期释放
    # 旧期快照仍记录当时冻结的 60 预留；当前账户可用已恢复
    snap = service.get_period_snapshot(conn, old_id)["balances"]
    assert [b["held"] for b in snap if b["code"] == "A"] == [60]
    assert service.get_account(conn, "A")["held"] == 0
    assert service.get_account(conn, "A")["available"] == 100


# ---------------------------------------------------------------- 冲正尊重预留

def test_reversal_cannot_pull_back_held_funds(conn):
    _setup(conn)
    # A=100, B=0, C=0
    service.post_batch(conn, "b1", [{"from": "A", "to": "B", "amount": 90}])  # A=10 B=90
    service.create_hold(conn, "h1", "A", "B", 10)                            # A 可用 0
    # B 再转出 50 给 C：B=40
    service.post_batch(conn, "b2", [{"from": "B", "to": "C", "amount": 50}])

    # 冲正 b1 需要从 B 扣 90，但 B 可用仅 40：拒绝（即使跨期也拒绝）
    service.close_current_period(conn)
    with pytest.raises(InvalidRequest):
        service.post_reversal(conn, "r1", "b1:0")
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE ref='r1'"
    ).fetchone()[0] == 0

    # C 把 50 退回 B 后（B 可用 90），冲正即可成功；
    # A 侧的有效预留 h1=10 自始至终未被冲正/其它转账动用。
    service.post_batch(conn, "b3", [{"from": "C", "to": "B", "amount": 50}])
    service.post_reversal(conn, "r2", "b1:0")
    assert service.get_account(conn, "A")["held"] == 10
    assert_balances_match(conn)
    assert recompute_balances(conn) == {"A": 100, "B": 0, "C": 0}


# ---------------------------------------------------------------- 并发

def test_concurrent_captures_all_succeed_once_each(conn):
    _setup(conn)
    for i in range(10):
        service.create_hold(conn, f"h{i}", "A", "B", 10)

    def attempt(i: int):
        c = connect()
        try:
            service.capture_hold(c, f"h{i}")
            return "ok"
        except LedgerError:
            return "err"
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(10)))
    assert outcomes == ["ok"] * 10
    assert service.get_account(conn, "A")["balance"] == 0
    assert_balances_match(conn)


def test_concurrent_duplicate_captures_transfer_exactly_once(conn):
    _setup(conn)
    service.create_hold(conn, "dup", "A", "B", 30)

    def attempt(_: int):
        c = connect()
        try:
            service.capture_hold(c, "dup")
            return "ok"
        except LedgerError as exc:
            return type(exc).__name__
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(12)))
    # 重复请求全部幂等成功，底层转账恰好一笔
    assert outcomes == ["ok"] * 12
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE ref='hold:dup'"
    ).fetchone()[0] == 1
    assert service.get_account(conn, "B")["balance"] == 30


def test_concurrent_oversubscribed_holds_only_available_total_wins(conn):
    _setup(conn)

    def attempt(i: int):
        c = connect()
        try:
            service.create_hold(c, f"g{i}", "A", "B", 30)
            return "ok"
        except LedgerError:
            return "rejected"
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(attempt, range(20)))
    assert outcomes.count("ok") == 3       # 100 / 30 取整
    assert outcomes.count("rejected") == 17
    acc = service.get_account(conn, "A")
    assert acc["held"] == 90 and acc["available"] == 10
    # 没有任何预留产生分录
    assert recompute_balances(conn) == {"A": 100, "B": 0, "C": 0}


def test_concurrent_capture_and_release_exactly_one_terminal_state(conn):
    _setup(conn)
    service.create_hold(conn, "race", "A", "B", 25)
    barrier = threading.Barrier(8)

    def decide(kind: str):
        c = connect()
        try:
            barrier.wait()
            if kind == "capture":
                service.capture_hold(c, "race")
            else:
                service.release_hold(c, "race")
            return "ok"
        except Conflict:
            return "conflict"
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(decide, "capture") for _ in range(4)]
        futures += [pool.submit(decide, "release") for _ in range(4)]
        outcomes = [f.result() for f in futures]

    final = service.get_hold(conn, "race")["status"]
    assert final in ("captured", "released")
    # 要么只捕获（4 捕获成功 + 4 释放冲突），要么只释放（1 释放成功 + 其余冲突/幂等）
    if final == "captured":
        assert service.get_account(conn, "B")["balance"] == 25
    else:
        assert service.get_account(conn, "B")["balance"] == 0
        assert service.get_account(conn, "A")["available"] == 100
    assert "ok" in outcomes
    assert_balances_match(conn)


# ---------------------------------------------------------------- 数据库兜底

def test_database_triggers_reject_bypass_writes(conn):
    import sqlite3

    _setup(conn)
    period = conn.execute(
        "SELECT id FROM periods WHERE status='open' ORDER BY id LIMIT 1"
    ).fetchone()[0]
    a = conn.execute("SELECT id FROM accounts WHERE code='A'").fetchone()[0]
    b = conn.execute("SELECT id FROM accounts WHERE code='B'").fetchone()[0]

    # 绕过应用层插入超额预留
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO holds (ref,source_id,target_id,amount,period_id) "
            "VALUES ('x',?,?,?,?)",
            (a, b, 101, period),
        )
        conn.execute("COMMIT")
    conn.execute("ROLLBACK")

    # 绕过应用层用借方分录花掉已预留积分
    service.create_hold(conn, "h1", "A", "B", 100)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("BEGIN IMMEDIATE")
        bid = conn.execute("INSERT INTO batches (ref) VALUES ('x')").lastrowid
        tid = conn.execute(
            "INSERT INTO transactions (ref,type,batch_id,period_id) "
            "VALUES ('x:0','transfer',?,?)",
            (bid, period),
        ).lastrowid
        conn.execute(
            "INSERT INTO entries (txn_id,account_id,period_id,amount) VALUES (?,?,?,?)",
            (tid, b, period, 100),
        )
        conn.execute(
            "INSERT INTO entries (txn_id,account_id,period_id,amount) VALUES (?,?,?,?)",
            (tid, a, period, -100),
        )
        conn.execute("COMMIT")
    conn.execute("ROLLBACK")
    assert recompute_balances(conn) == {"A": 100, "B": 0, "C": 0}


def test_holds_are_persistent_and_immutable_after_terminal(conn):
    import sqlite3

    _setup(conn)
    service.create_hold(conn, "h1", "A", "B", 10)
    service.capture_hold(conn, "h1")
    for sql in (
        "UPDATE holds SET status='active' WHERE ref='h1'",
        "UPDATE holds SET amount=99 WHERE ref='h1'",
        "DELETE FROM holds WHERE ref='h1'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
