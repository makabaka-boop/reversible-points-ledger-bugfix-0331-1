-- 积分账本结构定义 (SQLite)
-- 设计原则：余额、分录不可变、期末快照、冲正唯一性等关键不变量
-- 全部由数据库约束 / 触发器 / 部分唯一索引裁决，应用层只做提前校验。
--
-- 预留（holds）不是分录，不改变已入账余额；它占用的是“可用余额”。
-- 全库硬不变量（由触发器裁决，任何写入路径都绕不过）：
--     可用余额 = SUM(entries.amount) - SUM(active holds.amount) >= 0
-- 即：余额非负，且有效预留总额永远不超过已入账余额。

CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 结算期：同一时刻最多只有一个 open 期（部分唯一索引裁决）
CREATE TABLE IF NOT EXISTS periods (
    id         INTEGER PRIMARY KEY,
    seq        INTEGER NOT NULL UNIQUE,
    status     TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'closed')),
    opened_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    closed_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_periods_single_open
    ON periods(id) WHERE status = 'open';

-- 关期时写入的期末余额快照（不可修改、不可删除，见触发器）。
-- held = 截至关期仍有效（active）的预留额，跨期预留随之被冻结进快照。
CREATE TABLE IF NOT EXISTS period_balances (
    period_id  INTEGER NOT NULL REFERENCES periods(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    balance    INTEGER NOT NULL,
    held       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (period_id, account_id)
);

CREATE TABLE IF NOT EXISTS batches (
    id         INTEGER PRIMARY KEY,
    ref        TEXT NOT NULL UNIQUE,
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 尚未记入不可变分录的转账预留；捕获后形成普通转账，释放则不落账。
CREATE TABLE IF NOT EXISTS holds (
    id          INTEGER PRIMARY KEY,
    ref         TEXT NOT NULL UNIQUE,
    source_id   INTEGER NOT NULL REFERENCES accounts(id),
    target_id   INTEGER NOT NULL REFERENCES accounts(id),
    amount      INTEGER NOT NULL CHECK (amount > 0),
    status      TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'captured', 'released')),
    period_id   INTEGER NOT NULL REFERENCES periods(id),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_holds_active_source
    ON holds(source_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY,
    ref         TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL
                   CHECK (type IN ('transfer', 'reversal', 'opening', 'deposit')),
    batch_id    INTEGER REFERENCES batches(id),
    period_id   INTEGER NOT NULL REFERENCES periods(id),
    reversal_of INTEGER REFERENCES transactions(id),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 一笔原交易最多被冲正一次：部分唯一索引由数据库裁决并发冲正
CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_reversal_once
    ON transactions(reversal_of)
    WHERE reversal_of IS NOT NULL;

-- 不可修改的分录行：金额为整数（CHECK），正负代表借贷方向
CREATE TABLE IF NOT EXISTS entries (
    id         INTEGER PRIMARY KEY,
    txn_id     INTEGER NOT NULL REFERENCES transactions(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    period_id  INTEGER NOT NULL REFERENCES periods(id),
    amount     INTEGER NOT NULL CHECK (amount <> 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (txn_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_entries_account_period
    ON entries(account_id, period_id);

-- ========== 触发器 ==========
-- 重新执行本文件时用 DROP + CREATE 替换触发器体（CREATE IF NOT EXISTS 不会更新定义）。

DROP TRIGGER IF EXISTS trg_txn_shape;
DROP TRIGGER IF EXISTS trg_entry_shape;
DROP TRIGGER IF EXISTS trg_entry_no_overdraft;
DROP TRIGGER IF EXISTS trg_entry_period_open;
DROP TRIGGER IF EXISTS trg_entries_no_update;
DROP TRIGGER IF EXISTS trg_entries_no_delete;
DROP TRIGGER IF EXISTS trg_transactions_no_update;
DROP TRIGGER IF EXISTS trg_transactions_no_delete;
DROP TRIGGER IF EXISTS trg_snapshot_no_update;
DROP TRIGGER IF EXISTS trg_snapshot_no_delete;
DROP TRIGGER IF EXISTS trg_periods_update_gate;
DROP TRIGGER IF EXISTS trg_periods_no_delete;
DROP TRIGGER IF EXISTS trg_accounts_code_no_update;
DROP TRIGGER IF EXISTS trg_accounts_no_delete;
DROP TRIGGER IF EXISTS trg_holds_no_overdraft;
DROP TRIGGER IF EXISTS trg_holds_status_gate;
DROP TRIGGER IF EXISTS trg_holds_no_delete;

-- 交易形状：冲正必须引用一笔已存在的原始交易；只有冲正能带 reversal_of；
-- 转账必须属于一个批次；opening/deposit/reversal 不得挂批次。
CREATE TRIGGER trg_txn_shape
BEFORE INSERT ON transactions
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.type = 'reversal'
             AND (NEW.reversal_of IS NULL
                  OR NEW.batch_id IS NOT NULL
                  OR NOT EXISTS (SELECT 1 FROM transactions o
                                 WHERE o.id = NEW.reversal_of
                                   AND o.type IN ('transfer', 'deposit', 'opening')))
            THEN RAISE(ABORT, 'reversal must reference one original transfer/deposit/opening and carry no batch')
        WHEN NEW.type IN ('transfer', 'opening', 'deposit')
             AND NEW.reversal_of IS NOT NULL
            THEN RAISE(ABORT, 'only reversal transactions may reference an original transaction')
        WHEN NEW.type = 'transfer' AND NEW.batch_id IS NULL
            THEN RAISE(ABORT, 'transfer transactions must belong to a batch')
        WHEN NEW.type <> 'transfer' AND NEW.batch_id IS NOT NULL
            THEN RAISE(ABORT, 'batch_id is only allowed on transfer transactions')
    END;
END;

-- 分录形状：金额非零、期间必须与交易一致、每笔交易至多两条分录、
-- opening/deposit 只能有一条且为正。
CREATE TRIGGER trg_entry_shape
BEFORE INSERT ON entries
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.amount = 0
            THEN RAISE(ABORT, 'zero amount entries are forbidden')
        WHEN NEW.period_id <> (SELECT period_id FROM transactions WHERE id = NEW.txn_id)
            THEN RAISE(ABORT, 'entry period_id must match its transaction period_id')
        WHEN (SELECT type FROM transactions WHERE id = NEW.txn_id)
                 IN ('opening', 'deposit')
             AND ((SELECT COUNT(*) FROM entries WHERE txn_id = NEW.txn_id) >= 1
                  OR NEW.amount < 0)
            THEN RAISE(ABORT, 'opening/deposit transactions have a single positive entry')
        WHEN (SELECT COUNT(*) FROM entries WHERE txn_id = NEW.txn_id) >= 2
            THEN RAISE(ABORT, 'a transaction may have at most two entries')
    END;
END;

-- 可用余额不可为负：每行插入后按账户重算
--     SUM(entries) - SUM(active holds as source)
-- 为负即中止。预留占用的积分因此不可能被任何借方分录（普通转账 / 捕获 /
-- 冲正）花掉。捕获时应用层先在同一事务把该 hold 翻成 captured，
-- 该 hold 随即退出占用，随后的借方分录才得以通过。
CREATE TRIGGER trg_entry_no_overdraft
AFTER INSERT ON entries
FOR EACH ROW
WHEN (
        SELECT COALESCE(SUM(amount), 0)
        FROM entries WHERE account_id = NEW.account_id
     ) - (
        SELECT COALESCE(SUM(amount), 0)
        FROM holds
        WHERE source_id = NEW.account_id AND status = 'active'
     ) < 0
BEGIN
    SELECT RAISE(ABORT, 'available balance must not be negative');
END;

-- 预留本身同样受可用余额约束：新预留不得使 source 的可用余额为负。
CREATE TRIGGER trg_holds_no_overdraft
AFTER INSERT ON holds
FOR EACH ROW
WHEN (
        SELECT COALESCE(SUM(amount), 0)
        FROM entries WHERE account_id = NEW.source_id
     ) - (
        SELECT COALESCE(SUM(amount), 0)
        FROM holds
        WHERE source_id = NEW.source_id AND status = 'active'
     ) < 0
BEGIN
    SELECT RAISE(ABORT, 'hold would overdraw the available balance');
END;

-- 预留状态机：active -> captured | released，终态不可再变；
-- 金额 / 双方账户等核心字段不可修改。
CREATE TRIGGER trg_holds_status_gate
BEFORE UPDATE ON holds
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN OLD.status = 'active' AND NEW.status NOT IN ('captured', 'released')
            THEN RAISE(ABORT, 'an active hold may only be captured or released')
        WHEN OLD.status <> 'active' AND NEW.status <> OLD.status
            THEN RAISE(ABORT, 'captured or released holds are terminal')
        WHEN NEW.ref <> OLD.ref OR NEW.amount <> OLD.amount
             OR NEW.source_id <> OLD.source_id OR NEW.target_id <> OLD.target_id
             OR NEW.period_id <> OLD.period_id
            THEN RAISE(ABORT, 'hold core fields are immutable')
    END;
END;

-- 预留不可删除：要解除占用只能 release（保留审计轨迹）
CREATE TRIGGER trg_holds_no_delete
BEFORE DELETE ON holds
BEGIN
    SELECT RAISE(ABORT, 'holds cannot be deleted; release them instead');
END;

-- 关闭后的期间不得补写任何分录（转账与关期并发由数据库裁决）
CREATE TRIGGER trg_entry_period_open
AFTER INSERT ON entries
FOR EACH ROW
WHEN (SELECT status FROM periods WHERE id = NEW.period_id) = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'cannot post entries into a closed period');
END;

-- 分录不可修改、不可删除（冲正只能追加反向分录）
CREATE TRIGGER trg_entries_no_update
BEFORE UPDATE ON entries
BEGIN
    SELECT RAISE(ABORT, 'entries are immutable and cannot be updated');
END;
CREATE TRIGGER trg_entries_no_delete
BEFORE DELETE ON entries
BEGIN
    SELECT RAISE(ABORT, 'entries cannot be deleted; post a reversal instead');
END;

-- 交易本身同样不可修改、不可删除
CREATE TRIGGER trg_transactions_no_update
BEFORE UPDATE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are immutable and cannot be updated');
END;
CREATE TRIGGER trg_transactions_no_delete
BEFORE DELETE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions cannot be deleted; post a reversal instead');
END;

-- 快照不可修改、不可删除
CREATE TRIGGER trg_snapshot_no_update
BEFORE UPDATE ON period_balances
BEGIN
    SELECT RAISE(ABORT, 'period balance snapshots are immutable');
END;
CREATE TRIGGER trg_snapshot_no_delete
BEFORE DELETE ON period_balances
BEGIN
    SELECT RAISE(ABORT, 'period balance snapshots cannot be deleted');
END;

-- 期间只允许 open -> closed 这一种变更，关闭后不可重开，不可删除
CREATE TRIGGER trg_periods_update_gate
BEFORE UPDATE ON periods
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN OLD.status = 'closed' AND NEW.status = 'open'
            THEN RAISE(ABORT, 'closed periods cannot be reopened')
        WHEN NOT (OLD.status = 'open'
                  AND NEW.status = 'closed'
                  AND OLD.id = NEW.id
                  AND OLD.seq = NEW.seq
                  AND OLD.opened_at = NEW.opened_at)
            THEN RAISE(ABORT, 'the only allowed period change is open -> closed')
    END;
END;
CREATE TRIGGER trg_periods_no_delete
BEFORE DELETE ON periods
BEGIN
    SELECT RAISE(ABORT, 'periods cannot be deleted');
END;

-- 账户不可修改编码、不可删除（外键 + 触发器双重保护）
CREATE TRIGGER trg_accounts_code_no_update
BEFORE UPDATE ON accounts
FOR EACH ROW WHEN NEW.code <> OLD.code
BEGIN
    SELECT RAISE(ABORT, 'account code is immutable');
END;
CREATE TRIGGER trg_accounts_no_delete
BEFORE DELETE ON accounts
BEGIN
    SELECT RAISE(ABORT, 'accounts cannot be deleted');
END;
