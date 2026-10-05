"""执行会话持久化：SQLite（进程重启后会话与已确认记录仍在）。

设计要点：
- 单文件 SQLite，默认 /data/sessions.db（容器内挂命名卷），可用环境变量
  DISPATCH_SESSION_DB 覆盖；无外部依赖，随 Compose 一起起；
- 所有写事务走 process 内 RLock + BEGIN IMMEDIATE：同一进程的并发上报在
  临界区内串行判定，只有一份能写入；SQLite 文件锁同时防多进程抢写；
- 记录按 (session_id, idx) 主键落库，天然防重复下标；整批 INSERT 在同一
  事务内，任一条失败即回滚，满足「整批不入库」；
- session_batches 记录每批边界（起点、长度）、规范化记录与首次成功回执，
  与该批逐段记录在同一写事务内确认：重试只有起点/长度/内容完全命中某行
  才判 deduped，回执取冻结值；表以 CREATE TABLE IF NOT EXISTS 自动补建，
  旧库逐段记录原样保留，历史区间不按前缀猜测去重。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, List, Optional

# 单条记录为半小时实测，计划最长 200 段，记录列只存 JSON，无需额外规模设计。
# session_batches 记录批次边界（起点、长度）、本批规范化记录与首次成功回执：
# 重试只有「起点 + 长度 + 内容」与某行完全一致才判 deduped，且回执取首次成功时
# 冻结的值。表随 CREATE TABLE IF NOT EXISTS 在打开旧库时自动补建（迁移），
# 旧库逐段记录原样保留：查询与按 next_index 续报照常，但历史区间没有批次边界，
# 不会按前缀猜测去重。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    dispatch_json TEXT NOT NULL,
    plan_snapshot_json TEXT NOT NULL,
    audit_tolerance_kwh REAL NOT NULL,
    num_periods INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_records (
    session_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    PRIMARY KEY (session_id, idx),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE TABLE IF NOT EXISTS session_batches (
    session_id TEXT NOT NULL,
    start_index INTEGER NOT NULL,
    batch_size INTEGER NOT NULL,
    records_json TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    PRIMARY KEY (session_id, start_index),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
"""


def default_db_path() -> str:
    """数据库路径：环境变量优先；容器内优先可写的 /data，否则落到工作目录 data/。"""
    env = os.environ.get("DISPATCH_SESSION_DB")
    if env:
        return env
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data/sessions.db"
    return os.path.join(os.getcwd(), "data", "sessions.db")


class SessionStore:
    """SQLite 会话存储。方法均为短事务，调用方见 app/sessions.py。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or default_db_path()
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            # WAL：读不阻塞写、写不阻塞读；同一时刻仍只有一个写事务。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """加进程锁后开启 IMMEDIATE 事务，异常自动回滚。

        会话追加的「查重 — 边界核对 — 审计 — 入库」整段都在此临界区内完成，
        保证并发上报只有一份生效。
        """
        conn = self._connect()
        try:
            with self._lock:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def insert_session(
        self,
        session_id: str,
        dispatch_json: str,
        plan_snapshot_json: str,
        audit_tolerance_kwh: float,
        num_periods: int,
        created_at: str,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions"
                "(session_id, dispatch_json, plan_snapshot_json, audit_tolerance_kwh, "
                "num_periods, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    dispatch_json,
                    plan_snapshot_json,
                    audit_tolerance_kwh,
                    num_periods,
                    created_at,
                ),
            )
            conn.commit()

    def get_session_row(self, session_id: str) -> Optional[sqlite3.Row]:
        with self._lock, self._connect() as conn:
            return conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()

    def list_session_rows(self) -> List[sqlite3.Row]:
        with self._lock, self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT s.*, "
                    " (SELECT COUNT(*) FROM session_records r WHERE r.session_id = s.session_id)"
                    " AS confirmed_periods, "
                    " (SELECT MAX(stored_at) FROM session_records r WHERE r.session_id = s.session_id)"
                    " AS updated_at, "
                    " (SELECT record_json FROM session_records r WHERE r.session_id = s.session_id"
                    "  ORDER BY r.idx DESC LIMIT 1) AS last_record_json"
                    " FROM sessions s ORDER BY s.created_at, s.session_id"
                ).fetchall()
            )

    def count_records(self, conn: sqlite3.Connection, session_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM session_records WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["c"])

    def get_all_records(self, session_id: str) -> List[sqlite3.Row]:
        with self._lock, self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT idx, record_json, stored_at FROM session_records "
                    "WHERE session_id = ? ORDER BY idx",
                    (session_id,),
                ).fetchall()
            )

    def insert_record(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        idx: int,
        record_json: str,
        stored_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO session_records(session_id, idx, record_json, stored_at)"
            " VALUES (?, ?, ?, ?)",
            (session_id, idx, record_json, stored_at),
        )

    def get_batch(
        self, conn: sqlite3.Connection, session_id: str, start_index: int
    ) -> Optional[sqlite3.Row]:
        """按精确起点取已确认批次（边界 + 内容 + 首次成功回执）；无该起点批次返回 None。"""
        return conn.execute(
            "SELECT start_index, batch_size, records_json, receipt_json, stored_at "
            "FROM session_batches WHERE session_id = ? AND start_index = ?",
            (session_id, start_index),
        ).fetchone()

    def insert_batch(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        start_index: int,
        batch_size: int,
        records_json: str,
        receipt_json: str,
        stored_at: str,
    ) -> None:
        """批次边界与首次回执落库；与同事务的 session_records 插入一起原子确认。"""
        conn.execute(
            "INSERT INTO session_batches"
            "(session_id, start_index, batch_size, records_json, receipt_json, stored_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, start_index, batch_size, records_json, receipt_json, stored_at),
        )

    def get_last_record_row(
        self, conn: sqlite3.Connection, session_id: str
    ) -> Optional[sqlite3.Row]:
        """写事务内取已保存末条记录（用于下一批首态衔接与累计末态）。"""
        return conn.execute(
            "SELECT idx, record_json FROM session_records "
            "WHERE session_id = ? ORDER BY idx DESC LIMIT 1",
            (session_id,),
        ).fetchone()


_store: Optional[SessionStore] = None
_store_lock = threading.Lock()


def configure_store(db_path: Optional[str]) -> SessionStore:
    """替换全局存储（测试用同一路径重建即可模拟进程重启）。"""
    global _store
    with _store_lock:
        _store = SessionStore(db_path) if db_path is not None else None
    return get_store()


def get_store() -> SessionStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SessionStore()
    return _store
