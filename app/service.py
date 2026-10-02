"""账本业务层。

关键约定：
* 所有写操作都在 ``BEGIN IMMEDIATE`` 事务中完成，SQLite 的单写者锁负责把
  转账 / 关期 / 冲正 / 预留并发串行化，最终冲突交由 UNIQUE 索引与触发器裁决。
* 分录只追加、不修改、不删除；冲正 = 引用原交易追加金额相反的新分录。
* 预留（hold）不是分录，不改变已入账余额，只占用“可用余额”。
  全库硬不变量由触发器裁决：
      可用余额 = SUM(entries) - SUM(active holds) >= 0
* 应用层按“先正后负”顺序插入分录并提前做可用余额预检，触发器作为最后防线，
  保证任何路径下余额非负、且已预留积分不可能被花掉。
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


def _held(conn: sqlite3.Connection, account_id: int) -> int:
    """账户作为 source、仍处于 active 的预留总额（调用方应在写事务内）。"""
    return conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM holds "
        "WHERE source_id = ? AND status = 'active'",
        (account_id,),
    ).fetchone()[0]


def _available(conn: sqlite3.Connection, account_id: int) -> int:
    return _balance(conn, account_id) - _held(conn, account_id)


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
def _hold_row(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    return conn.execute(
        """SELECT h.*, s.code AS source, t.code AS target
           FROM holds h JOIN accounts s ON s.id = h.source_id
           JOIN accounts t ON t.id = h.target_id WHERE h.ref = ?""",
        (ref,),
    ).fetchone()


def get_hold(conn: sqlite3.Connection, ref: str) -> dict:
    row = _hold_row(conn, ref)
    if row is None:
        raise NotFound(f"unknown hold: {ref}")
    out = {"ref": row["ref"], "source": row["source"], "target": row["target"],
           "amount": row["amount"], "status": row["status"],
           "period_id": row["period_id"], "created_at": row["created_at"]}
    if row["status"] == "captured":
        out["transfer_ref"] = f"hold:{ref}"
    return out


def create_hold(conn: sqlite3.Connection, ref: str, source: str, target: str, amount: int) -> dict:
    if source == target or amount <= 0:
        raise InvalidRequest("hold needs distinct accounts and a positive amount")
    with immediate(conn):
        if _hold_row(conn, ref) is not None:
            raise Conflict(f"duplicate hold ref: {ref}")
        accounts = _account_map(conn, {source, target})
        if len(accounts) != 2:
            raise InvalidRequest("hold references an unknown account")
        # 占用的是“可用余额”（余额 - 已有效预留），不是总余额。
        # 触发器 trg_holds_no_overdraft 在数据库层做同样的裁决。
        if _available(conn, accounts[source]["id"]) < amount:
            raise InvalidRequest("insufficient available balance for hold")
        period = current_open_period(conn)
        try:
            conn.execute(
                "INSERT INTO holds (ref, source_id, target_id, amount, period_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (ref, accounts[source]["id"], accounts[target]["id"], amount, period["id"]),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"duplicate hold or insufficient available balance: {ref}") from exc
        return get_hold(conn, ref)


def capture_hold(conn: sqlite3.Connection, ref: str) -> dict:
    """把预留捕获为正式转账。

    * 幂等：对已 captured 的预留重复请求（网络重试）返回既有结果，不重复转账；
    * 原子：预留状态翻转与两条分录在同一个 ``BEGIN IMMEDIATE`` 事务内提交，
      任何中途故障都不会出现“钱已转、预留仍 active”或其反面；
    * 捕获释放该笔占用，借方分录只可动用释放出来的这部分额度——其它有效预留
      仍由触发器 trg_entry_no_overdraft 保护。
    """
    with immediate(conn):
        hold = _hold_row(conn, ref)
        if hold is None:
            raise NotFound(f"unknown hold: {ref}")
        if hold["status"] == "captured":
            return get_hold(conn, ref)
        if hold["status"] == "released":
            raise Conflict(f"hold {ref} was released and cannot be captured")

        period = current_open_period(conn)
        batch_id = conn.execute(
            "INSERT INTO batches (ref, note) VALUES (?, ?)",
            (f"hold:{ref}", f"capture of hold {ref}"),
        ).lastrowid
        txn_id = conn.execute(
            "INSERT INTO transactions (ref, type, batch_id, period_id) "
            "VALUES (?, 'transfer', ?, ?)",
            (f"hold:{ref}", batch_id, period["id"]),
        ).lastrowid
        # 先翻转状态：该笔预留立即退出“有效占用”，下面的借方分录才过得去
        # 可用余额触发器；此时分录尚未插入，一旦后续语句失败整事务回滚。
        conn.execute("UPDATE holds SET status = 'captured' WHERE id = ?", (hold["id"],))
        # 先正后负：与普通批次相同的插入顺序，避免逐行触发器看到“假透支”。
        conn.execute(
            "INSERT INTO entries (txn_id, account_id, period_id, amount) "
            "VALUES (?, ?, ?, ?)",
            (txn_id, hold["target_id"], period["id"], hold["amount"]),
        )
        conn.execute(
            "INSERT INTO entries (txn_id, account_id, period_id, amount) "
            "VALUES (?, ?, ?, ?)",
            (txn_id, hold["source_id"], period["id"], -hold["amount"]),
        )
        return get_hold(conn, ref)


def release_hold(conn: sqlite3.Connection, ref: str) -> dict:
    """释放未捕获的预留。幂等：重复 release 返回当前状态；
    已 captured 的预留不可释放（409）。"""
    with immediate(conn):
        hold = _hold_row(conn, ref)
        if hold is None:
            raise NotFound(f"unknown hold: {ref}")
        if hold["status"] == "captured":
            raise Conflict(f"hold {ref} was captured and cannot be released")
        if hold["status"] == "active":
            conn.execute("UPDATE holds SET status = 'released' WHERE id = ?", (hold["id"],))
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

        # 事务内预检：按“可用余额”模拟整批应用后的余额，任何账户为负则整批不记。
        # 已被有效预留占用的积分不能再用于普通转账（触发器同样裁决）。
        available = {code: _available(conn, row["id"]) for code, row in accounts.items()}
        for t in transfers:
            available[t["from"]] -= t["amount"]
            available[t["to"]] += t["amount"]
        overdrawn = sorted(code for code, bal in available.items() if bal < 0)
        if overdrawn:
            raise InvalidRequest(
                f"insufficient available balance for accounts: {overdrawn}"
            )

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
        # 冲正把资金拉回原付款方/扣回原收款方：借方一侧同样只能动用“可用余额”，
        # 不能把仍被有效预留占用的积分拉走（触发器 trg_entry_no_overdraft 兜底）。
        balances = {aid: _available(conn, aid) for aid in affected}
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
            raise InvalidRequest(
                f"reversal would overdraw available balance of accounts: {codes}"
            )

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

        # 快照在写锁内计算，必然与已落账分录、当时仍有效的预留一致；
        # 之后向该期插入分录会被 trg_entry_period_open 拒绝。
        # 跨期的有效预留同样被冻结进 held，使快照的 available = balance - held
        # 与当时账户接口完全同口径；跨期释放/捕获不再与旧期快照矛盾。
        snapshot_rows = conn.execute(
            """
            SELECT a.id AS account_id,
                   COALESCE((SELECT SUM(amount) FROM entries e
                             WHERE e.account_id = a.id), 0) AS balance,
                   COALESCE((SELECT SUM(amount) FROM holds h
                             WHERE h.source_id = a.id AND h.status = 'active'), 0) AS held
            FROM accounts a
            ORDER BY a.id
            """
        ).fetchall()
        for row in snapshot_rows:
            conn.execute(
                "INSERT INTO period_balances (period_id, account_id, balance, held) "
                "VALUES (?, ?, ?, ?)",
                (old["id"], row["account_id"], row["balance"], row["held"]),
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
                {"account_id": r["account_id"], "balance": r["balance"],
                 "held": r["held"], "available": r["balance"] - r["held"]}
                for r in snapshot_rows
            ],
        }


# ---------------------------------------------------------------- 查询

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
        SELECT a.*,
               COALESCE((SELECT SUM(amount) FROM entries e
                         WHERE e.account_id = a.id), 0) AS balance,
               COALESCE((SELECT SUM(amount) FROM holds h
                         WHERE h.source_id = a.id AND h.status = 'active'), 0) AS held
        FROM accounts a ORDER BY a.id
        """
    ).fetchall()
    return [_account_dict(r, r["balance"], r["held"]) for r in rows]


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
        SELECT pb.account_id, a.code, pb.balance, pb.held
        FROM period_balances pb JOIN accounts a ON a.id = pb.account_id
        WHERE pb.period_id = ? ORDER BY pb.account_id
        """,
        (period_id,),
    ).fetchall()
    return {
        "period": _period_dict(conn, period_id),
        "balances": [{"account_id": r["account_id"], "code": r["code"],
                      "balance": r["balance"], "held": r["held"],
                      "available": r["balance"] - r["held"]} for r in rows],
    }
