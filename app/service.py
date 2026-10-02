"""账本业务层。

关键约定：
* 所有写操作都在 ``BEGIN IMMEDIATE`` 事务中完成，SQLite 的单写者锁负责把
  转账 / 关期 / 冲正并发串行化，最终冲突交由 UNIQUE 索引与触发器裁决。
* 分录只追加、不修改、不删除；冲正 = 引用原交易追加金额相反的新分录。
* 应用层按“先正后负”顺序插入分录并提前做余额预检，触发器 trg_entry_no_overdraft
  作为最后防线，保证任何路径下余额都不可能为负。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from .db import current_open_period
from .errors import Conflict, InvalidRequest, NotFound


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _account_map(conn: sqlite3.Connection, codes: set[str]) -> dict[str, sqlite3.Row]:
    if not codes:
        return {}
    marks = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT * FROM accounts WHERE code IN ({marks})", tuple(codes)
    ).fetchall()
    return {row["code"]: row for row in rows}


def _balance(conn: sqlite3.Connection, account_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM entries WHERE account_id = ?",
        (account_id,),
    ).fetchone()[0]


# ---------------------------------------------------------------- 账户 / 入账

def create_account(conn: sqlite3.Connection, code: str, opening_balance: int = 0) -> dict:
    with immediate(conn):
        if conn.execute("SELECT 1 FROM accounts WHERE code = ?", (code,)).fetchone():
            raise Conflict(f"account already exists: {code}")
        period = current_open_period(conn)
        cur = conn.execute("INSERT INTO accounts (code) VALUES (?)", (code,))
        account_id = cur.lastrowid
        if opening_balance > 0:
            ref = f"opening:{code}"
            txn_id = conn.execute(
                "INSERT INTO transactions (ref, type, period_id) VALUES (?, 'opening', ?)",
                (ref, period["id"]),
            ).lastrowid
            conn.execute(
                "INSERT INTO entries (txn_id, account_id, period_id, amount) "
                "VALUES (?, ?, ?, ?)",
                (txn_id, account_id, period["id"], opening_balance),
            )
        return get_account(conn, code)


def deposit(conn: sqlite3.Connection, code: str, amount: int, ref: str) -> dict:
    with immediate(conn):
        account = conn.execute("SELECT * FROM accounts WHERE code = ?", (code,)).fetchone()
        if account is None:
            raise NotFound(f"unknown account: {code}")
        if conn.execute("SELECT 1 FROM transactions WHERE ref = ?", (ref,)).fetchone():
            raise Conflict(f"duplicate transaction ref: {ref}")
        period = current_open_period(conn)
        txn_id = conn.execute(
            "INSERT INTO transactions (ref, type, period_id) VALUES (?, 'deposit', ?)",
            (ref, period["id"]),
        ).lastrowid
        conn.execute(
            "INSERT INTO entries (txn_id, account_id, period_id, amount) "
            "VALUES (?, ?, ?, ?)",
            (txn_id, account["id"], period["id"], amount),
        )
        return _get_transaction(conn, txn_id)


# ---------------------------------------------------------------- 预留 / 捕获
def get_hold(conn: sqlite3.Connection, ref: str) -> dict:
    row = conn.execute(
        """SELECT h.*, s.code AS source, t.code AS target
           FROM holds h JOIN accounts s ON s.id = h.source_id
           JOIN accounts t ON t.id = h.target_id WHERE h.ref = ?""",
        (ref,),
    ).fetchone()
    if row is None:
        raise NotFound(f"unknown hold: {ref}")
    return {"ref": row["ref"], "source": row["source"], "target": row["target"],
            "amount": row["amount"], "status": row["status"],
            "period_id": row["period_id"], "created_at": row["created_at"]}


def create_hold(conn: sqlite3.Connection, ref: str, source: str, target: str, amount: int) -> dict:
    if source == target or amount <= 0:
        raise InvalidRequest("hold needs distinct accounts and a positive amount")
    with immediate(conn):
        if conn.execute("SELECT 1 FROM holds WHERE ref = ?", (ref,)).fetchone():
            raise Conflict(f"duplicate hold ref: {ref}")
        accounts = _account_map(conn, {source, target})
        if len(accounts) != 2:
            raise InvalidRequest("hold references an unknown account")
        if _balance(conn, accounts[source]["id"]) < amount:
            raise InvalidRequest("insufficient balance for hold")
        period = current_open_period(conn)
        conn.execute(
            "INSERT INTO holds (ref, source_id, target_id, amount, period_id) VALUES (?, ?, ?, ?, ?)",
            (ref, accounts[source]["id"], accounts[target]["id"], amount, period["id"]),
        )
        return get_hold(conn, ref)


def capture_hold(conn: sqlite3.Connection, ref: str) -> dict:
    hold = get_hold(conn, ref)
    if hold["status"] != "active":
        raise Conflict(f"hold is already {hold['status']}")
    post_batch(conn, f"hold:{ref}", [{"from": hold["source"],
                                      "to": hold["target"], "amount": hold["amount"]}])
    with immediate(conn):
        conn.execute("UPDATE holds SET status = 'captured' WHERE ref = ?", (ref,))
        return get_hold(conn, ref)


def release_hold(conn: sqlite3.Connection, ref: str) -> dict:
    with immediate(conn):
        hold = get_hold(conn, ref)
        if hold["status"] != "active":
            raise Conflict(f"hold is already {hold['status']}")
        conn.execute("UPDATE holds SET status = 'released' WHERE ref = ?", (ref,))
        return get_hold(conn, ref)


# ---------------------------------------------------------------- 批次转账

def post_batch(
    conn: sqlite3.Connection,
    ref: str,
    transfers: list[dict],
    note: str | None = None,
) -> dict:
    """整批转账：要么全部记账，要么全部不记（一个事务）。"""
    if not transfers:
        raise InvalidRequest("batch must contain at least one transfer")

    with immediate(conn):
        if conn.execute("SELECT 1 FROM batches WHERE ref = ?", (ref,)).fetchone():
            raise Conflict(f"duplicate batch ref: {ref}")
        period = current_open_period(conn)

        codes: set[str] = set()
        for t in transfers:
            if t["from"] == t["to"]:
                raise InvalidRequest(f"transfer cannot target the same account: {t['from']}")
            codes.add(t["from"])
            codes.add(t["to"])

        accounts = _account_map(conn, codes)
        missing = sorted(codes - accounts.keys())
        if missing:
            raise InvalidRequest(f"unknown accounts: {missing}")

        # 事务内预检：模拟整批应用后的余额，任何账户为负则整批不记
        balances = {code: _balance(conn, row["id"]) for code, row in accounts.items()}
        for t in transfers:
            balances[t["from"]] -= t["amount"]
            balances[t["to"]] += t["amount"]
        overdrawn = sorted(code for code, bal in balances.items() if bal < 0)
        if overdrawn:
            raise InvalidRequest(f"insufficient balance for accounts: {overdrawn}")

        try:
            batch_id = conn.execute(
                "INSERT INTO batches (ref, note) VALUES (?, ?)", (ref, note)
            ).lastrowid
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"duplicate batch ref: {ref}") from exc

        txn_ids: list[int] = []
        for idx, t in enumerate(transfers):
            txn_id = conn.execute(
                "INSERT INTO transactions (ref, type, batch_id, period_id) "
                "VALUES (?, 'transfer', ?, ?)",
                (f"{ref}:{idx}", batch_id, period["id"]),
            ).lastrowid
            txn_ids.append(txn_id)

        # 先插所有贷方（正），再插所有借方（负）：
        # 保证逐行触发器在任何中间状态都看不到“假透支”。
        ordered: list[tuple[int, int, int]] = []  # (txn_id, account_id, amount)
        for idx, t in enumerate(transfers):
            ordered.append((txn_ids[idx], accounts[t["to"]]["id"], t["amount"]))
        for idx, t in enumerate(transfers):
            ordered.append((txn_ids[idx], accounts[t["from"]]["id"], -t["amount"]))

        for txn_id, account_id, amount in ordered:
            conn.execute(
                "INSERT INTO entries (txn_id, account_id, period_id, amount) "
                "VALUES (?, ?, ?, ?)",
                (txn_id, account_id, period["id"], amount),
            )

        return {
            "ref": ref,
            "batch_id": batch_id,
            "period_id": period["id"],
            "transfers": [
                {"ref": f"{ref}:{idx}", "from": t["from"], "to": t["to"], "amount": t["amount"]}
                for idx, t in enumerate(transfers)
            ],
        }


# ---------------------------------------------------------------- 冲正

def post_reversal(conn: sqlite3.Connection, ref: str, original_ref: str) -> dict:
    """冲正原交易：追加金额相反的分录，记入当前开放期；原分录保留不动。"""
    with immediate(conn):
        if conn.execute("SELECT 1 FROM transactions WHERE ref = ?", (ref,)).fetchone():
            raise Conflict(f"duplicate transaction ref: {ref}")

        orig = conn.execute(
            "SELECT * FROM transactions WHERE ref = ?", (original_ref,)
        ).fetchone()
        if orig is None:
            raise NotFound(f"unknown original transaction: {original_ref}")
        if orig["type"] not in ("transfer", "deposit", "opening"):
            raise InvalidRequest("only original transfer/deposit/opening transactions can be reversed")

        prior = conn.execute(
            "SELECT ref FROM transactions WHERE reversal_of = ?", (orig["id"],)
        ).fetchone()
        if prior is not None:
            # 并发冲正同时也会被部分唯一索引 idx_txn_reversal_once 裁决
            raise Conflict(f"transaction {original_ref} was already reversed by {prior['ref']}")

        period = current_open_period(conn)
        orig_entries = conn.execute(
            "SELECT * FROM entries WHERE txn_id = ?", (orig["id"],)
        ).fetchall()

        affected = {row["account_id"] for row in orig_entries}
        balances = {aid: _balance(conn, aid) for aid in affected}
        for row in orig_entries:
            balances[row["account_id"]] -= row["amount"]  # 新分录金额 = -原金额
        overdrawn = sorted(aid for aid, bal in balances.items() if bal < 0)
        if overdrawn:
            codes = [
                r["code"]
                for r in conn.execute(
                    f"SELECT id, code FROM accounts WHERE id IN ({','.join('?' * len(overdrawn))})",
                    tuple(overdrawn),
                )
            ]
            raise InvalidRequest(f"reversal would overdraw accounts: {codes}")

        try:
            txn_id = conn.execute(
                "INSERT INTO transactions (ref, type, period_id, reversal_of) "
                "VALUES (?, 'reversal', ?, ?)",
                (ref, period["id"], orig["id"]),
            ).lastrowid
        except sqlite3.IntegrityError as exc:
            # 并发冲正在部分唯一索引 idx_txn_reversal_once 上撞车
            raise Conflict(f"transaction {original_ref} can only be reversed once") from exc

        # 原借方(负)最先翻转成新贷方(正)，保证“先正后负”
        for row in sorted(orig_entries, key=lambda r: r["amount"]):
            conn.execute(
                "INSERT INTO entries (txn_id, account_id, period_id, amount) "
                "VALUES (?, ?, ?, ?)",
                (txn_id, row["account_id"], period["id"], -row["amount"]),
            )

        return _get_transaction(conn, txn_id)


# ---------------------------------------------------------------- 关期

def close_current_period(conn: sqlite3.Connection) -> dict:
    """关闭当前开放期并同事务保存期末快照、开启下一期。"""
    with immediate(conn):
        old = current_open_period(conn)

        # 快照在写锁内计算，必然与已落账分录一致；
        # 之后向该期插入分录会被 trg_entry_period_open 拒绝。
        snapshot_rows = conn.execute(
            """
            SELECT a.id AS account_id,
                   COALESCE((SELECT SUM(amount) FROM entries e
                             WHERE e.account_id = a.id), 0) AS balance
            FROM accounts a
            ORDER BY a.id
            """
        ).fetchall()
        for row in snapshot_rows:
            conn.execute(
                "INSERT INTO period_balances (period_id, account_id, balance) "
                "VALUES (?, ?, ?)",
                (old["id"], row["account_id"], row["balance"]),
            )

        conn.execute(
            "UPDATE periods SET status = 'closed', closed_at = ? WHERE id = ?",
            (utcnow(), old["id"]),
        )
        new_id = conn.execute(
            "INSERT INTO periods (seq, status) VALUES (?, 'open')",
            (old["seq"] + 1,),
        ).lastrowid

        return {
            "closed_period": _period_dict(conn, old["id"]),
            "opened_period": _period_dict(conn, new_id),
            "snapshot": [
                {"account_id": r["account_id"], "balance": r["balance"]}
                for r in snapshot_rows
            ],
        }


# ---------------------------------------------------------------- 查询

def _held(conn: sqlite3.Connection, account_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM holds WHERE source_id = ? AND status = 'active'",
        (account_id,),
    ).fetchone()[0]


def _account_dict(row: sqlite3.Row, balance: int, held: int) -> dict:
    return {"id": row["id"], "code": row["code"], "balance": balance,
            "available": balance - held, "held": held,
            "created_at": row["created_at"]}


def get_account(conn: sqlite3.Connection, code: str) -> dict:
    row = conn.execute("SELECT * FROM accounts WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise NotFound(f"unknown account: {code}")
    return _account_dict(row, _balance(conn, row["id"]), _held(conn, row["id"]))


def list_accounts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.*, COALESCE((SELECT SUM(amount) FROM entries e
                              WHERE e.account_id = a.id), 0) AS balance
        FROM accounts a ORDER BY a.id
        """
    ).fetchall()
    return [_account_dict(r, r["balance"], _held(conn, r["id"])) for r in rows]


def _txn_dict(row: sqlite3.Row, entries: list[sqlite3.Row]) -> dict:
    return {
        "id": row["id"],
        "ref": row["ref"],
        "type": row["type"],
        "batch_id": row["batch_id"],
        "period_id": row["period_id"],
        "reversal_of": row["reversal_of"],
        "created_at": row["created_at"],
        "entries": [
            {"account_id": e["account_id"], "amount": e["amount"],
             "period_id": e["period_id"]}
            for e in entries
        ],
    }


def _get_transaction(conn: sqlite3.Connection, txn_id: int) -> dict:
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if row is None:
        raise NotFound(f"unknown transaction id: {txn_id}")
    entries = conn.execute(
        "SELECT * FROM entries WHERE txn_id = ? ORDER BY id", (txn_id,)
    ).fetchall()
    return _txn_dict(row, entries)


def get_transaction_by_ref(conn: sqlite3.Connection, ref: str) -> dict:
    row = conn.execute("SELECT * FROM transactions WHERE ref = ?", (ref,)).fetchone()
    if row is None:
        raise NotFound(f"unknown transaction ref: {ref}")
    return _get_transaction(conn, row["id"])


def list_transactions(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM transactions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_get_transaction(conn, r["id"]) for r in rows]


def _period_dict(conn: sqlite3.Connection, period_id: int) -> dict:
    row = conn.execute("SELECT * FROM periods WHERE id = ?", (period_id,)).fetchone()
    return {"id": row["id"], "seq": row["seq"], "status": row["status"],
            "opened_at": row["opened_at"], "closed_at": row["closed_at"]}


def list_periods(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM periods ORDER BY id").fetchall()
    return [_period_dict(conn, r["id"]) for r in rows]


def get_period_snapshot(conn: sqlite3.Connection, period_id: int) -> dict:
    period = conn.execute("SELECT * FROM periods WHERE id = ?", (period_id,)).fetchone()
    if period is None:
        raise NotFound(f"unknown period: {period_id}")
    if period["status"] != "closed":
        raise InvalidRequest(f"period {period_id} is still open; no snapshot exists")
    rows = conn.execute(
        """
        SELECT pb.account_id, a.code, pb.balance
        FROM period_balances pb JOIN accounts a ON a.id = pb.account_id
        WHERE pb.period_id = ? ORDER BY pb.account_id
        """,
        (period_id,),
    ).fetchall()
    return {
        "period": _period_dict(conn, period_id),
        "balances": [{"account_id": r["account_id"], "code": r["code"],
                      "balance": r["balance"]} for r in rows],
    }
