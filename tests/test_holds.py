"""预留/捕获/释放的不变量测试。

对照基准仍是 ``recompute_balances()``（从不可变分录逐行重算）。
核心不变量：
* available = balance - SUM(active holds) 永远 >= 0；
* 普通转账、冲正都不能花掉已预留积分，任何预留最终都能被捕获；
* 捕获/释放是一次性状态迁移，重复请求幂等，并发下恰有一个结果；
* 跨期预留有效，捕获落入新期；旧期快照（含 held）不可变且与重算一致。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app import service
import pytest

from app.db import connect

from .conftest import assert_balances_match, recompute_balances


def _two_accounts(conn, a=100, b=0):
    service.create_account(conn, "A", opening_balance=a)
    service.create_account(conn, "B", opening_balance=b)
    service.create_account(conn, "C", opening_balance=0)


def test_hold_cannot_overcommit_available(conn):
    _two_accounts(conn)
    service.create_hold(conn, "h1", "A", "B", 80)
    acc = service.get_account(conn, "A")
    assert acc["balance"] == 100 and acc["held"] == 80 and acc["available"] == 20

    # 第二笔 80（共 160 > 100）必须被拒，可用余额不得为负
    from app.errors import InvalidRequest
    with pytest.raises(InvalidRequest):
        service.create_hold(conn, "h2", "A", "C", 80)
    acc = service.get_account(conn, "A")
    assert acc["held"] == 80 and acc["available"] == 20
    assert_balances_match(conn)


def test_hold_does_not_post_entries(conn):
    _two_accounts(conn)
    service.create_hold(conn, "h1", "A", "B", 80)
    # 预留只占可用额度，不写任何不可变分录，已入账余额不变
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1  # 仅 opening
    assert recompute_balances(conn) == {"A": 100, "B": 0, "C": 0}


def test_plain_batch_cannot_spend_held_points(conn):
    from app.errors import InvalidRequest
    _two_accounts(conn)
    service.create_hold(conn, "h1", "A", "B", 80)

    # 50 > 可用 20：拒绝，且不是由余额触发器兜底（已入账余额仍有 100）
    with pytest.raises(InvalidRequest):
        service.post_batch(conn, "b1", [{"from": "A", "to": "C", "amount": 50}])

    # 花掉可用部分 20 可以；此后早先的预留仍然一定能捕获
    service.post_batch(conn, "b2", [{"from": "A", "to": "C", "amount": 20}])
    hold = service.capture_hold(conn, "h1")
    assert hold["status"] == "captured"
    assert recompute_balances(conn) == {"A": 0, "B": 80, "C": 20}
    assert_balances_match(conn)


def test_capture_is_idempotent_and_transfers_once(conn):
    _two_accounts(conn)
    service.create_hold(conn, "h1", "A", "B", 40)
    first = service.capture_hold(conn, "h1")
    second = service.capture_hold(conn, "h1")  # 客户端重试
    assert first["status"] == second["status"] == "captured"
    # 只有一条捕获交易、两条相反分录
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE ref = 'hold:h1:0'"
    ).fetchone()[0] == 1
    assert tuple(conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM entries WHERE txn_id IN "
        "(SELECT id FROM transactions WHERE ref='hold:h1:0')"
    ).fetchone()) == (2, 0)
    assert recompute_balances(conn) == {"A": 60, "B": 40, "C": 0}


def test_release_is_idempotent_and_blocks_late_capture(conn):
    from app.errors import Conflict
    _two_accounts(conn)
    service.create_hold(conn, "h1", "A", "B", 40)
    assert service.release_hold(conn, "h1")["status"] == "released"
    assert service.release_hold(conn, "h1")["status"] == "released"  # 重试幂等
    with pytest.raises(Conflict):
        service.capture_hold(conn, "h1")
    # 释放后额度回来，可用余额恢复
    acc = service.get_account(conn, "A")
    assert acc["held"] == 0 and acc["available"] == 100


def test_captured_hold_cannot_be_released(conn):
    from app.errors import Conflict
    _two_accounts(conn)
    service.create_hold(conn, "h1", "A", "B", 40)
    service.capture_hold(conn, "h1")
    with pytest.raises(Conflict):
        service.release_hold(conn, "h1")
    assert service.get_hold(conn, "h1")["status"] == "captured"


def test_concurrent_capture_vs_release_has_single_outcome(conn):
    _two_accounts(conn)
    service.create_hold(conn, "h", "A", "B", 60)
    errs = []
    lock = threading.Lock()

    def cap():
        c = connect()
        try:
            service.capture_hold(c, "h")
        except Exception as e:  # noqa: BLE001
            with lock:
                errs.append(("cap", type(e).__name__))
        finally:
            c.close()

    def rel():
        c = connect()
        try:
            service.release_hold(c, "h")
        except Exception as e:  # noqa: BLE001
            with lock:
                errs.append(("rel", type(e).__name__))
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        [f.result() for f in [pool.submit(cap), pool.submit(rel)]]

    status = service.get_hold(conn, "h")["status"]
    assert status in ("captured", "released")
    a = service.get_account(conn, "A")
    b = service.get_account(conn, "B")
    if status == "captured":
        assert (a["balance"], a["held"], b["balance"]) == (40, 0, 60)
        assert errs == [("rel", "Conflict")]
    else:
        assert (a["balance"], a["held"], b["balance"]) == (100, 0, 0)
        assert errs == [("cap", "Conflict")]
    assert_balances_match(conn)


def test_concurrent_holds_never_exceed_available(conn):
    from app.errors import InvalidRequest, LedgerError
    service.create_account(conn, "A", opening_balance=100)
    service.create_account(conn, "B")

    def place(i):
        c = connect()
        try:
            service.create_hold(c, f"h{i}", "A", "B", 30)
            return "ok"
        except LedgerError:
            return "rejected"
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(place, range(12)))

    # 100 / 30 -> 恰 3 笔成功；可用余额不为负
    assert outcomes.count("ok") == 3, outcomes
    acc = service.get_account(conn, "A")
    assert acc["held"] == 90 and acc["available"] == 10
    # 全部成功的预留都能被捕获（不出现“先成功后失败”的预留）；
    # 被拒的预留根本不存在（没有悬空的 active 行）
    active_refs = [r[0] for r in conn.execute(
        "SELECT ref FROM holds WHERE status='active' ORDER BY ref"
    ).fetchall()]
    assert len(active_refs) == 3
    for ref in active_refs:
        assert service.capture_hold(conn, ref)["status"] == "captured"
    assert service.get_account(conn, "B")["balance"] == 90
    assert_balances_match(conn)


def test_cross_period_hold_survives_close_and_captures_in_new_period(conn):
    from app.db import db_path
    _two_accounts(conn)
    service.create_hold(conn, "h1", "A", "B", 70)

    closed = service.close_current_period(conn)
    old_id = closed["closed_period"]["id"]

    # 旧期快照同时记录已入账余额与当时的有效预留
    snap = {x["code"]: x for x in service.get_period_snapshot(conn, old_id)["balances"]}
    assert snap["A"]["balance"] == 100 and snap["A"]["held"] == 70
    assert snap["A"]["available"] == 30

    # 预留跨期仍有效，捕获进入新期
    new_id = conn.execute(
        "SELECT id FROM periods WHERE status='open' ORDER BY id LIMIT 1"
    ).fetchone()[0]
    assert service.get_hold(conn, "h1")["status"] == "active"
    service.capture_hold(conn, "h1")
    txn = service.get_transaction_by_ref(conn, "hold:h1:0")
    assert txn["period_id"] == new_id
    assert {e["period_id"] for e in txn["entries"]} == {new_id}

    # 旧期快照不可变：余额/预留都保持关期时的值
    snap2 = {x["code"]: x for x in service.get_period_snapshot(conn, old_id)["balances"]}
    assert snap2["A"] == snap["A"]
    assert service.get_account(conn, "A")["balance"] == 30
    assert_balances_match(conn)


def test_reversal_cannot_take_back_held_points(conn):
    from app.errors import InvalidRequest
    _two_accounts(conn)
    service.create_hold(conn, "h1", "A", "B", 70)
    service.capture_hold(conn, "h1")  # B 现有 70
    service.create_hold(conn, "h2", "B", "A", 40)  # B 的可用只剩 30

    # 冲正捕获交易要从 B 拿走 70，但 40 已被 B 的预留占用 -> 拒绝
    with pytest.raises(InvalidRequest):
        service.post_reversal(conn, "r1", "hold:h1:0")

    service.release_hold(conn, "h2")
    service.post_reversal(conn, "r2", "hold:h1:0")
    assert_balances_match(conn)
    assert recompute_balances(conn)["B"] == 0


def test_hold_state_machine_enforced_by_database(conn):
    import sqlite3
    _two_accounts(conn)
    service.create_hold(conn, "h1", "A", "B", 10)

    for sql in (
        "UPDATE holds SET amount = 999 WHERE ref='h1'",
        "UPDATE holds SET status='active' WHERE ref='h1'",
        "DELETE FROM holds WHERE ref='h1'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
