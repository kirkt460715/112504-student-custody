"""SQLite 持久化层。

所有业务状态都落盘：服务重启后在途学生、未决门岗证据、争议与交接事实
都能继续处理。写操作统一走 BEGIN IMMEDIATE 事务，配合唯一约束保证
两个岗位几乎同时确认同一学生时只有一种交接生效。
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 站点（出发校区 / 临时校区）与车辆 ------------------------------------------------
CREATE TABLE IF NOT EXISTS sites (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operators (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('desk', 'crew')),
    scope TEXT NOT NULL            -- 站点 id 或车辆 id
);

CREATE TABLE IF NOT EXISTS students (
    id TEXT PRIMARY KEY,
    home_site TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guardians (
    student_id TEXT NOT NULL,
    guardian_id TEXT NOT NULL,
    PRIMARY KEY (student_id, guardian_id)
);

-- 接领凭证：家长 / 授权亲属持有的不透明令牌 ---------------------------------------
CREATE TABLE IF NOT EXISTS collector_tokens (
    token TEXT PRIMARY KEY,
    collector_id TEXT NOT NULL,
    request_id TEXT,               -- 家长只能看与自己接领请求有关的状态
    issued_at TEXT NOT NULL
);

-- 代领授权：有时限、指定孩子，可撤回 ----------------------------------------------
CREATE TABLE IF NOT EXISTS delegations (
    id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    guardian_id TEXT NOT NULL,     -- 授予该授权的登记监护人
    delegate_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);

-- 转运车辆/班次 -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trips (
    id TEXT PRIMARY KEY,
    from_site TEXT NOT NULL,
    to_site TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('planned', 'departed', 'arrived')),
    created_at TEXT NOT NULL,
    departed_at TEXT,
    arrived_at TEXT
);

-- 每个班次每个学生的点名状态，按人保留差异，不做整车覆盖 ---------------------------
CREATE TABLE IF NOT EXISTS trip_roster (
    trip_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    booked INTEGER NOT NULL DEFAULT 0,      -- 预安排
    boarded INTEGER NOT NULL DEFAULT 0,     -- 发车实际点到
    counted INTEGER NOT NULL DEFAULT 0,     -- 到场实际清点到
    note TEXT,
    PRIMARY KEY (trip_id, student_id)
);

-- 照管方：每个学生至多一条，原子更新即“照管责任转移” ------------------------------
CREATE TABLE IF NOT EXISTS custody (
    student_id TEXT PRIMARY KEY,
    party_type TEXT NOT NULL CHECK (party_type IN ('site', 'crew', 'person')),
    party_id TEXT NOT NULL,
    since TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

-- 交接事实：只追加。任何名单更正都不得删除或改写已核实的交接 -----------------------
CREATE TABLE IF NOT EXISTS handovers (
    id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'boarding', 'arrival', 'pickup', 'custody_review')),
    from_party_type TEXT,
    from_party_id TEXT,
    to_party_type TEXT NOT NULL,
    to_party_id TEXT NOT NULL,
    collector_id TEXT,             -- pickup 时实际接领人
    request_id TEXT,
    trip_id TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'proposed', 'confirmed', 'rejected', 'superseded', 'blocked')),
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    operator_id TEXT NOT NULL,
    -- 现场确认时使用的授权快照：撤回后排队中的交接必须凭快照重新核验
    auth_basis TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_handovers_student ON handovers(student_id);
CREATE INDEX IF NOT EXISTS idx_handovers_status ON handovers(status);

-- 门岗证据：终端实物交接记录，独立于业务规则，冲突时留在待核对 ---------------------
CREATE TABLE IF NOT EXISTS gate_evidence (
    op_id TEXT PRIMARY KEY,
    terminal TEXT NOT NULL,
    student_id TEXT NOT NULL,
    handover_id TEXT NOT NULL,
    collector_id TEXT,
    receipt_id TEXT,
    observed_at TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'matched', 'conflict', 'rejected')),
    detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gate_student ON gate_evidence(student_id);
CREATE INDEX IF NOT EXISTS idx_gate_state ON gate_evidence(state);

-- 争议/待核对（点名不符、证据冲突、授权撤回等） -----------------------------------
CREATE TABLE IF NOT EXISTS disputes (
    id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    kind TEXT NOT NULL,            -- count_mismatch / gate_conflict / auth_revoked / other
    state TEXT NOT NULL CHECK (state IN ('open', 'resolved')),
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT
);
CREATE INDEX IF NOT EXISTS idx_disputes_student ON disputes(student_id);
CREATE INDEX IF NOT EXISTS idx_disputes_state ON disputes(state);

-- 家长/亲属到校的接领请求（请求不等于实际接领） -----------------------------------
CREATE TABLE IF NOT EXISTS pickup_requests (
    id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    collector_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'registered', 'queued', 'reverified', 'denied', 'fulfilled', 'cancelled')),
    registered_at TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_student ON pickup_requests(student_id);

-- 只追加的审计日志，交班使用 ------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        parent = Path(self.path).parent
        parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path, timeout=15, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        # 跨线程写事务串行化：两岗同时确认同一学生时，后到者在锁上等待，
        # 拿到锁后重新读取照管方，只会得到冲突而不会产生第二种生效交接
        self._write_lock = threading.RLock()
        self._tx_depth = threading.local()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """串行化、线程内可重入的写事务。

        业务规则拒绝（ApiError）也要保留留痕（blocked 交接、争议、审计），
        因此 ApiError 走提交；只有未预期异常才回滚。
        """
        from .auth import ApiError
        conn = self._conn
        depth = getattr(self._tx_depth, "depth", 0)
        self._tx_depth.depth = depth + 1
        if depth == 0:
            self._write_lock.acquire()
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            if depth == 0:
                conn.execute("COMMIT")
        except ApiError:
            if depth == 0:
                conn.execute("COMMIT")
            raise
        except Exception:
            if depth == 0:
                conn.execute("ROLLBACK")
            raise
        finally:
            self._tx_depth.depth = depth
            if depth == 0:
                self._write_lock.release()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()
