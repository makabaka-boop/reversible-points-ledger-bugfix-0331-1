"""并发测试：多线程各持独立连接，同时冲正 / 转账 / 关期。

裁决方必须是数据库（BEGIN IMMEDIATE 单写者锁 + 唯一索引 + 触发器），
最终状态一律与“从原始分录重算的余额”对照。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app import service
from app.db import connect
from app.errors import Conflict, InvalidRequest, LedgerError

from .conftest import assert_balances_match, recompute_balances

_DB = None  # 由 fixture 注入路径（conftest 已设置 LEDGER_DB_PATH）


def _fresh_conn():
    return connect()


def test_concurrent_reversals_exactly_one_wins(conn):
    from app.db import db_path

    service.create_account(conn, "A", opening_balance=100)
    service.create_account(conn, "B")
    service.post_batch(conn, "b1", [{"from": "A", "to": "B", "amount": 10}])

    results: list[Exception | str] = []
    lock = threading.Lock()

    def attempt(i: int):
        c = _fresh_conn()
        try:
            service.post_reversal(c, f"rev-{i}", "b1:0")
            with lock:
                results.append("ok")
        except LedgerError as exc:
            with lock:
                results.append(exc)
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(attempt, range(16)))

    winners = [r for r in results if r == "ok"]
    losers = [r for r in results if r != "ok"]
    assert len(winners) == 1, results
    assert len(losers) == 15
    assert all(isinstance(r, Conflict) for r in losers)

    # 唯一索引保证：反向交易只有一条；余额等于原交易被抵消
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE reversal_of IS NOT NULL"
    ).fetchone()[0] == 1
    assert_balances_match(conn)
    assert recompute_balances(conn) == {"A": 100, "B": 0}


def test_concurrent_transfers_are_serialized_and_never_overdraw(conn):
    service.create_account(conn, "A", opening_balance=500)
    service.create_account(conn, "B")
    service.create_account(conn, "C")

    def transfer(i: int):
        c = _fresh_conn()
        try:
            service.post_batch(c, f"batch-{i}", [
                {"from": "A", "to": "B", "amount": 1},
                {"from": "A", "to": "C", "amount": 1},
            ])
            return "ok"
        except LedgerError:
            return "conflict"
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(transfer, range(200)))

    assert set(outcomes) <= {"ok", "conflict"}
    n_ok = outcomes.count("ok")
    assert n_ok == 200  # 总额度足够，全部串行成功

    assert_balances_match(conn)
    bals = recompute_balances(conn)
    assert bals == {"A": 100, "B": 200, "C": 200}

    # 全库分录借贷平衡
    assert conn.execute("SELECT COALESCE(SUM(amount),0) FROM entries").fetchone()[0] == 500


def test_concurrent_overdraft_attempts_only_valid_total_wins(conn):
    service.create_account(conn, "A", opening_balance=25)
    service.create_account(conn, "B")

    def drain(i: int):
        c = _fresh_conn()
        try:
            service.post_batch(c, f"drain-{i}",
                               [{"from": "A", "to": "B", "amount": 10}])
            return "ok"
        except LedgerError:
            return "rejected"
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(drain, range(20)))

    assert outcomes.count("ok") == 2
    assert outcomes.count("rejected") == 18
    assert_balances_match(conn)
    assert recompute_balances(conn)["A"] == 5


def test_transfer_concurrent_with_close_never_splits_across_periods(conn):
    """转账批次要么整体在关期前，要么整体在新期，绝不跨期分裂，
    且关期快照与重算余额永远一致。
    """
    service.create_account(conn, "A", opening_balance=1000)
    service.create_account(conn, "B")

    stop = threading.Event()

    def do_transfers(i_start: int):
        c = _fresh_conn()
        n = 0
        i = i_start
        try:
            while not stop.is_set():
                try:
                    service.post_batch(c, f"t-{i}",
                                       [{"from": "A", "to": "B", "amount": 1}])
                    n += 1
                except Conflict:
                    # 关期或冲正类冲突：关期窗口内可重试
                    if stop.is_set():
                        break
                    continue
                except InvalidRequest:
                    # 余额耗尽：可记账的转账是有限的，停止而不是空转
                    break
                i += 100
        finally:
            c.close()
        return n

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(do_transfers, s) for s in range(6)]
        # 让转账先飞一会，再并发关期
        import time
        time.sleep(0.2)
        closer = _fresh_conn()
        try:
            closed = service.close_current_period(closer)
        finally:
            closer.close()
        stop.set()
        total_transfers = sum(f.result() for f in futures)

    closed_id = closed["closed_period"]["id"]

    # 不变量 1：任何一笔交易的两条分录 period_id 必须相同
    split = conn.execute(
        """
        SELECT t.id, COUNT(DISTINCT e.period_id) AS np
        FROM transactions t JOIN entries e ON e.txn_id = t.id
        WHERE t.type = 'transfer'
        GROUP BY t.id HAVING np > 1
        """
    ).fetchall()
    assert split == []

    # 不变量 2：所有关闭期内的分录都属于该期内创建的交易（关闭之后不得补写）
    late = conn.execute(
        """
        SELECT COUNT(*) FROM transactions t
        WHERE t.period_id = ?
          AND t.created_at > (SELECT closed_at FROM periods WHERE id = ?)
        """,
        (closed_id, closed_id),
    ).fetchone()[0]
    assert late == 0
    backfill_entries = conn.execute(
        """
        SELECT COUNT(*) FROM entries e
        WHERE e.period_id = ?
          AND e.created_at > (SELECT closed_at FROM periods WHERE id = ?)
        """,
        (closed_id, closed_id),
    ).fetchone()[0]
    assert backfill_entries == 0

    # 不变量 3：快照 == 截至关闭期原始分录重算
    snap = {b["code"]: b["balance"]
            for b in service.get_period_snapshot(conn, closed_id)["balances"]}
    replay_closed = {}
    for code in ("A", "B"):
        replay_closed[code] = conn.execute(
            "SELECT COALESCE(SUM(e.amount),0) FROM entries e "
            "JOIN accounts a ON a.id=e.account_id "
            "WHERE a.code=? AND e.period_id <= ?",
            (code, closed_id),
        ).fetchone()[0]
    assert snap == replay_closed

    # 不变量 4：当前总余额与原始分录重算一致
    assert_balances_match(conn)
    total = service.get_account(conn, "A")["balance"] + service.get_account(conn, "B")["balance"]
    assert total == 1000
    assert total_transfers >= 1
