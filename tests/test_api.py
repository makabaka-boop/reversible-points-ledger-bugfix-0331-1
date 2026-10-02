"""端到端 HTTP API 测试。"""
from __future__ import annotations

from .conftest import recompute_balances


def _replay(client) -> dict[str, int]:
    txns = client.get("/transactions", params={"limit": 1000}).json()
    out: dict[str, int] = {}
    code_by_id = {a["id"]: a["code"] for a in client.get("/accounts").json()}
    for t in txns:
        for e in t["entries"]:
            out[code_by_id[e["account_id"]]] = out.get(code_by_id[e["account_id"]], 0) + e["amount"]
    return dict(sorted(out.items()))


def test_full_lifecycle_over_http(client):
    r = client.get("/health")
    assert r.status_code == 200

    # 开户 + 期初余额（只接受非负整数）
    assert client.post("/accounts", json={"code": "A", "opening_balance": 100}).status_code == 201
    assert client.post("/accounts", json={"code": "B"}).status_code == 201
    r = client.post("/accounts", json={"code": "A"})
    assert r.status_code == 409
    r = client.post("/accounts", json={"code": "X", "opening_balance": -1})
    assert r.status_code == 422
    r = client.post("/accounts", json={"code": "Y", "opening_balance": 1.5})
    assert r.status_code == 422

    # 批次转账
    r = client.post("/transfers/batches", json={
        "ref": "b1",
        "transfers": [
            {"from": "A", "to": "B", "amount": 30},
            {"from": "A", "to": "B", "amount": 5},
        ],
    })
    assert r.status_code == 201, r.text
    assert len(r.json()["transfers"]) == 2

    # 重复批次
    r = client.post("/transfers/batches", json={
        "ref": "b1", "transfers": [{"from": "A", "to": "B", "amount": 1}],
    })
    assert r.status_code == 409

    # 透支整批拒绝
    r = client.post("/transfers/batches", json={
        "ref": "b2", "transfers": [{"from": "B", "to": "A", "amount": 100}],
    })
    assert r.status_code == 400
    assert client.get("/transactions/b2:0").status_code == 404

    # 金额必须是正整数
    r = client.post("/transfers/batches", json={
        "ref": "b3", "transfers": [{"from": "A", "to": "B", "amount": 0}],
    })
    assert r.status_code == 422
    r = client.post("/transfers/batches", json={
        "ref": "b4", "transfers": [{"from": "A", "to": "B", "amount": 2.5}],
    })
    assert r.status_code == 422

    # 余额与重算一致
    accounts = {a["code"]: a["balance"] for a in client.get("/accounts").json()}
    assert accounts == {"A": 65, "B": 35}
    assert accounts == _replay(client)

    # 查交易
    t = client.get("/transactions/b1:0").json()
    assert t["type"] == "transfer"
    assert {e["amount"] for e in t["entries"]} == {30, -30}

    # 关期
    r = client.post("/periods/close")
    assert r.status_code == 201
    closed_id = r.json()["closed_period"]["id"]
    id_to_code = {a["id"]: a["code"] for a in client.get("/accounts").json()}
    assert {id_to_code[b["account_id"]]: b["balance"] for b in r.json()["snapshot"]} == {"A": 65, "B": 35}
    periods = client.get("/periods").json()
    assert [p["status"] for p in periods] == ["closed", "open"]

    # 快照可查；开放期没有快照
    snap = client.get(f"/periods/{closed_id}/snapshot")
    assert snap.status_code == 200
    assert client.get(f"/periods/{closed_id + 1}/snapshot").status_code == 400

    # 关期后补写：新批次只进新期，旧期快照不变
    r = client.post("/transfers/batches", json={
        "ref": "b5", "transfers": [{"from": "A", "to": "B", "amount": 10}],
    })
    assert r.status_code == 201
    assert r.json()["period_id"] == closed_id + 1
    assert {b["code"]: b["balance"]
            for b in client.get(f"/periods/{closed_id}/snapshot").json()["balances"]} == {
        "A": 65, "B": 35,
    }

    # 冲正旧交易：记入当前开放期
    r = client.post("/reversals", json={"ref": "r1", "original_ref": "b1:0"})
    assert r.status_code == 201, r.text
    assert {e["period_id"] for e in r.json()["entries"]} == {closed_id + 1}

    # 第二次冲正必败（409，由数据库唯一索引裁决）
    r = client.post("/reversals", json={"ref": "r2", "original_ref": "b1:0"})
    assert r.status_code == 409

    # 冲正不存在的交易 -> 404
    r = client.post("/reversals", json={"ref": "r3", "original_ref": "nope"})
    assert r.status_code == 404

    # 最终余额仍然与从原始分录的重算完全一致，且非负
    final = {a["code"]: a["balance"] for a in client.get("/accounts").json()}
    assert final == {"A": 85, "B": 15}
    assert final == _replay(client)
    assert all(v >= 0 for v in final.values())


def test_deposit_and_reversal_http(client):
    client.post("/accounts", json={"code": "A"})
    r = client.post("/accounts/A/deposit", json={"amount": 50, "ref": "d1"})
    assert r.status_code == 201

    # 同 ref 重复提交不重复入账
    r = client.post("/accounts/A/deposit", json={"amount": 50, "ref": "d1"})
    assert r.status_code == 409

    # 冲正 deposit
    r = client.post("/reversals", json={"ref": "rd1", "original_ref": "d1"})
    assert r.status_code == 201
    assert client.get("/accounts/A").json()["balance"] == 0
    assert client.get("/accounts/A").json()["balance"] == _replay(client)["A"]

    # 再次冲正失败
    assert client.post("/reversals",
                       json={"ref": "rd2", "original_ref": "d1"}).status_code == 409
