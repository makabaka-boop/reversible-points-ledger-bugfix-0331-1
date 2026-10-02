"""错误类型：把业务校验失败与数据库裁决冲突映射为合适的 HTTP 状态码。"""
from __future__ import annotations

import sqlite3


class LedgerError(Exception):
    code = "ledger_error"
    status_code = 400


class NotFound(LedgerError):
    code = "not_found"
    status_code = 404


class Conflict(LedgerError):
    code = "conflict"
    status_code = 409


class InvalidRequest(LedgerError):
    code = "invalid_request"
    status_code = 400


def translate_integrity_error(exc: sqlite3.IntegrityError) -> LedgerError:
    """把数据库裁决出来的约束/触发器冲突转成合适的业务错误。

    * 可用余额不足类触发器（预留超额、转账花掉已预留积分）-> 400；
    * 其余唯一约束 / 状态机冲突 -> 409。
    """
    msg = str(exc)
    if "insufficient available balance for hold" in msg or "reserved points" in msg:
        return InvalidRequest(msg)
    return Conflict(msg)
