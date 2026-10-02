# 模拟积分账本（FastAPI + SQLite）

一个以**原始分录（append-only ledger entries）为唯一事实来源**的模拟积分账本。
账户余额只能是整数、不可为负；每笔转账在同一事务写入金额相反的两条不可修改分录；
批量转账原子提交；冲正只能成功一次且保留原分录；结算期可关闭并保存期末快照。
转账预留可先占用账户的**可用**积分（已入账余额 − 有效预留），再捕获成正式转账或释放；
预留本身不改变已入账余额与不可变分录，且有效预留总额永远不超过已入账余额。
所有关键并发裁决都由数据库（SQLite 的写锁、部分唯一索引、触发器）完成，
余额、预留、分录与期末快照在任何故障/并发路径下都不会互相矛盾。

## 为什么关键不变量由数据库裁决

应用层只做提前校验（便于给出友好错误），最终裁决全部下沉到 SQLite：

| 不变量 | 裁决机制 |
| --- | --- |
| 余额是整数、非零 | `entries.amount INTEGER` + `CHECK (amount <> 0)`；API 层 Pydantic `StrictInt` |
| 已入账余额不可为负 | 触发器 `trg_entry_no_overdraft`：每行插入后按账户重算 `SUM(entries) − SUM(active holds)`，为负即 `RAISE(ABORT)` |
| 有效预留总额不超过已入账余额（可用余额永不为负） | 触发器 `trg_holds_no_overdraft`（插入预留时裁决）+ 同一口径的借方分录触发器 |
| 预留状态机与不可删除 | 触发器 `trg_holds_status_gate`（仅 `active→captured/released`，终态不可变，核心字段不可改）、`trg_holds_no_delete` |
| 捕获 = 一次原子状态翻转 + 两条分录 | 单事务 `BEGIN IMMEDIATE`：先把 hold 翻成 `captured`（释放该笔占用），再按“先正后负”插分录；中途失败整体回滚 |
| 每笔转账两条相反分录、借贷平衡 | 触发器 `trg_entry_shape`：每笔交易最多两条分录；opening/deposit 仅一条且为正 |
| 一批转账原子提交 | 单事务 `BEGIN IMMEDIATE`，失败整体 `ROLLBACK` |
| 冲正最多成功一次 | 部分唯一索引 `idx_txn_reversal_once ON transactions(reversal_of) WHERE reversal_of IS NOT NULL` |
| 冲正必须引用真实原交易、不可冲正冲正 | 触发器 `trg_txn_shape` |
| 分录/交易/快照不可修改、不可删除 | 六个 `BEFORE UPDATE/DELETE ... RAISE(ABORT)` 触发器 |
| 关闭后不得补写该期 | 触发器 `trg_entry_period_open`（分录期必须仍为 open） |
| 同一时刻只有一个开放期 | 部分唯一索引 `idx_periods_single_open`；期间仅允许 open→closed |
| 转账与关期、并发冲正/预留 | 所有写事务 `BEGIN IMMEDIATE`，SQLite 单写者锁串行化；冲突方收到约束错误（HTTP 409/400） |
| 快照与分录、预留一致 | 快照在同一把写锁内由 `SUM(entries)` 与 `SUM(active holds)` 计算并提交，同时冻结 `balance` 与 `held` |

冲正的反向分录**记入当前开放期**（而不是原交易所在期），原分录原样保留，
因此已关闭期的期末快照永远不变。

## 运行

需要 Docker + Docker Compose v2。

```bash
# 固定验收（依次执行）
docker compose config --quiet
docker compose build
docker compose run --rm verify
```

- API 服务（需要时启动）：`docker compose up -d api`，http://127.0.0.1:8000 ，交互式文档 `/docs`
- `verify` 是一次性测试服务：在容器内对独立的临时数据库运行全部 pytest 用例并退出。
- 数据库持久化在命名卷 `ledger-data`（容器内 `/data/ledger.db`，WAL 模式）。

## HTTP API

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /accounts` | 开户，body `{"code": "A", "opening_balance": 100}`（期初余额为非负整数，可选） |
| `GET /accounts` / `GET /accounts/{code}` | 账户与当前余额（由原始分录实时汇总） |
| `POST /accounts/{code}/deposit` | 存款/发放积分，body `{"ref": "d1", "amount": 50}`，`ref` 幂等去重 |
| `POST /transfers/batches` | 一批转账，原子提交 |
| `POST /reversals` | 冲正，body `{"ref": "r1", "original_ref": "b1:0"}` |
| `POST /holds` | 预留积分，body `{"ref":"h1","source":"A","target":"B","amount":20}` |
| `GET /holds/{ref}` | 查询预留状态 |
| `POST /holds/{ref}/capture` | 捕获预留并转账 |
| `POST /holds/{ref}/release` | 释放未捕获的预留 |
| `GET /transactions` / `GET /transactions/{ref}` | 查询交易及其分录 |
| `POST /periods/close` | 关闭当前开放期：写期末快照并开启下一期 |
| `GET /periods` | 期间列表（始终恰有一个 open） |
| `GET /periods/{id}/snapshot` | 已关闭期的期末余额快照 |

批次转账示例：

```bash
curl -X POST http://127.0.0.1:8000/transfers/batches \
  -H 'Content-Type: application/json' \
  -d '{"ref":"b1","note":"工资发放","transfers":[
        {"from":"A","to":"B","amount":30},
        {"from":"A","to":"C","amount":20}]}'
```

批次内每笔转账的交易 ref 为 `批次ref:序号`（如 `b1:0`），可直接用于冲正。
业务校验失败返回 400，引用不存在返回 404，并发/唯一性冲突返回 409。
账户响应中的 `balance` 是原始分录重算的已入账余额，`held` 是仍有效的预留额，
`available = balance − held` 是可继续用于转账或新预留的余额，三者在任何路径下
（包括绕过应用层的直接 SQL）都满足 `available ≥ 0`。预留本身不产生分录、
不改变 `balance`；跨结算期预留仍有效，捕获分录记入当前开放期。

预留生命周期语义：

- `POST /holds`：按**可用余额**预留；余额不足返回 400，`ref` 重复返回 409。
- `POST /holds/{ref}/capture`：在单事务内把预留翻成 `captured` 并写入正式转账
  （交易 ref 为 `hold:{hold_ref}`）。对已捕获预留的重复请求（网络重试）**幂等**，
  返回同一结果且不会重复转账；预留已释放则返回 409。
- `POST /holds/{ref}/release`：释放预留、恢复可用余额，不产生分录。重复释放幂等；
  预留已捕获则返回 409。
- 关期快照同时冻结 `balance`、`held` 与派生的 `available`：跨期释放/捕获只影响
  当前期，已关闭期快照永不被重写。

## 测试策略（`tests/`）

每个用例使用全新数据库，且以 **`recompute_balances()` —— 从原始分录逐行重算的余额**
为对照，校验 API/查询接口返回的余额始终与之相等且为非负整数。

- `test_replay.py`（重放）：批次转账、冲正落入新期、原分录保留、冲正仅一次、
  关期后补写被拒、期末快照 == 截至该期分录重算、分录/交易/快照不可变、非负/整数约束。
- `test_concurrency.py`（并发）：
  - 16 个并发冲正同一笔交易 → 恰有 1 个成功，其余全部 409；
  - 200 批并发转账串行化、不丢不重、余额与重算一致、全库借贷平衡；
  - 20 个并发超额提款 → 成功总额恰好等于余额，其余全部拒绝；
  - 转账与关期并发 → 单笔交易的两条分录绝不跨期分裂，快照与重算永远一致。
- `test_rollback.py`（故障回滚）：记账中途注入 I/O 故障（第 1/中间/最后一条分录），
  整批零残留；多笔合计透支整批拒绝；冲正中途失败不留半截；未提交事务在连接丢失
  （模拟进程崩溃）后从磁盘重开数据库自动回滚。
- `test_holds.py`（预留/捕获/释放）：
  - 100 分上两笔各 80 的预留 → 第二笔 400，`available` 恒非负；
  - 普通批量转账（含批次合计透支）与冲正都不能花掉已预留积分，早先成功的预留必然可捕获；
  - 捕获中途 I/O 故障（任一一条分录）→ hold 仍 `active`、批次/交易/分录零残留，重试成功一次；
  - 重复捕获幂等（12 个并发重复请求只产生一笔转账）、重复释放幂等、捕获/释放终态互斥 409；
  - 预留跨期有效、捕获分录只进新期、旧期快照冻结 `balance/held`、跨期释放不重写快照；
  - 20 个并发各 30 的超额预留 → 恰好 3 笔成功；捕获/释放并发竞争只有一种终态；
  - 绕过应用层直接 INSERT 超额预留/借方分录 → 触发器拒绝；终态 hold 不可改不可删。
- `test_api.py`：完整 HTTP 生命周期（含 422 拒绝非整数/负数金额）。

本地直接运行（不用 Docker）：

```bash
pip install -r requirements.txt
pytest -q
```

## 目录结构

```
app/
  schema.sql    # 表、部分唯一索引、全部触发器
  db.py         # 连接（WAL / foreign_keys / busy_timeout）、建库
  service.py    # 事务边界与业务操作（BEGIN IMMEDIATE）
  errors.py     # 业务错误 -> HTTP 状态码
  main.py       # FastAPI 路由与 Pydantic 校验
tests/          # 重放 / 并发 / 故障回滚 / HTTP 测试
Dockerfile
docker-compose.yml   # api + 一次性 verify 服务
```
