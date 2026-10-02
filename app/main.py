"""FastAPI 入口。

每个请求一个 SQLite 连接；写操作走 service 层的 ``BEGIN IMMEDIATE`` 事务。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Optional

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt
from sqlite3 import IntegrityError, OperationalError

from . import service
from .db import connect, db_path, init_db
from .errors import Conflict, InvalidRequest, LedgerError, NotFound


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="模拟积分账本", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- 模型

class AccountIn(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=128)]
    opening_balance: Annotated[int, StrictInt] = Field(default=0, ge=0)


class DepositIn(BaseModel):
    amount: Annotated[int, StrictInt] = Field(gt=0)
    ref: Annotated[str, Field(min_length=1, max_length=200)]


class TransferIn(BaseModel):
    from_: Annotated[str, Field(alias="from", min_length=1, max_length=128)]
    to: Annotated[str, Field(min_length=1, max_length=128)]
    amount: Annotated[int, StrictInt] = Field(gt=0)


class BatchIn(BaseModel):
    ref: Annotated[str, Field(min_length=1, max_length=200)]
    transfers: list[TransferIn]
    note: Optional[str] = None


class ReversalIn(BaseModel):
    ref: Annotated[str, Field(min_length=1, max_length=200)]
    original_ref: Annotated[str, Field(min_length=1, max_length=200)]


class HoldIn(BaseModel):
    ref: Annotated[str, Field(min_length=1, max_length=200)]
    source: Annotated[str, Field(min_length=1, max_length=128)]
    target: Annotated[str, Field(min_length=1, max_length=128)]
    amount: Annotated[int, StrictInt] = Field(gt=0)


# ---------------------------------------------------------------- 生命周期 / 依赖

def get_conn() -> Iterator:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


ConnDep = Annotated[object, Depends(get_conn)]


@app.exception_handler(LedgerError)
def _handle_ledger_error(request, exc: LedgerError):  # noqa: ANN001
    return JSONResponse(status_code=exc.status_code,
                        content={"error": exc.code, "detail": str(exc)})


@app.exception_handler(IntegrityError)
def _handle_integrity(request, exc: IntegrityError):  # noqa: ANN001
    # 漏到 API 层的数据库裁决冲突（理论上 service 层已覆盖，此处兜底）
    return JSONResponse(status_code=Conflict.status_code,
                        content={"error": Conflict.code, "detail": str(exc)})


@app.exception_handler(OperationalError)
def _handle_operational(request, exc: OperationalError):  # noqa: ANN001
    # busy_timeout 耗尽等数据库锁错误：可安全重试
    return JSONResponse(status_code=503,
                        content={"error": "temporarily_unavailable",
                                 "detail": str(exc)})


@app.exception_handler(NotFound)
def _handle_not_found(request, exc: NotFound):  # noqa: ANN001
    return JSONResponse(status_code=exc.status_code,
                        content={"error": exc.code, "detail": str(exc)})


# ---------------------------------------------------------------- 路由

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "db": db_path()}


@app.post("/accounts", status_code=201)
def create_account(body: AccountIn, conn: ConnDep) -> dict:
    return service.create_account(conn, body.code, body.opening_balance)


@app.get("/accounts")
def list_accounts(conn: ConnDep) -> list[dict]:
    return service.list_accounts(conn)


@app.get("/accounts/{code}")
def get_account(code: str, conn: ConnDep) -> dict:
    return service.get_account(conn, code)


@app.post("/accounts/{code}/deposit", status_code=201)
def deposit(code: str, body: DepositIn, conn: ConnDep) -> dict:
    return service.deposit(conn, code, body.amount, body.ref)


@app.post("/transfers/batches", status_code=201)
def post_batch(body: BatchIn, conn: ConnDep) -> dict:
    transfers = [t.model_dump(by_alias=True) for t in body.transfers]
    return service.post_batch(conn, body.ref, transfers, body.note)


@app.post("/reversals", status_code=201)
def post_reversal(body: ReversalIn, conn: ConnDep) -> dict:
    return service.post_reversal(conn, body.ref, body.original_ref)


@app.post("/holds", status_code=201)
def create_hold(body: HoldIn, conn: ConnDep) -> dict:
    return service.create_hold(conn, body.ref, body.source, body.target, body.amount)


@app.get("/holds/{ref:path}")
def get_hold(ref: str, conn: ConnDep) -> dict:
    return service.get_hold(conn, ref)


@app.post("/holds/{ref}/capture")
def capture_hold(ref: str, conn: ConnDep) -> dict:
    return service.capture_hold(conn, ref)


@app.post("/holds/{ref}/release")
def release_hold(ref: str, conn: ConnDep) -> dict:
    return service.release_hold(conn, ref)


@app.get("/transactions")
def list_transactions(conn: ConnDep, limit: int = 100) -> list[dict]:
    return service.list_transactions(conn, min(max(limit, 1), 1000))


@app.get("/transactions/{ref:path}")
def get_transaction(ref: str, conn: ConnDep) -> dict:
    return service.get_transaction_by_ref(conn, ref)


@app.post("/periods/close", status_code=201)
def close_period(conn: ConnDep) -> dict:
    return service.close_current_period(conn)


@app.get("/periods")
def list_periods(conn: ConnDep) -> list[dict]:
    return service.list_periods(conn)


@app.get("/periods/{period_id}/snapshot")
def period_snapshot(period_id: int, conn: ConnDep) -> dict:
    return service.get_period_snapshot(conn, period_id)
