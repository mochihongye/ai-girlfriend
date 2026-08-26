"""记忆系统：对话历史 + 长期记忆，基于 SQLite。

线程安全：每个线程用独立连接（wcferry 在子线程里收消息）。
"""
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class Memory:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True) if db_path else None
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        with self._cursor() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts TEXT DEFAULT (datetime('now','localtime'))
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts TEXT DEFAULT (datetime('now','localtime'))
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id)")

    @contextmanager
    def _cursor(self):
        conn = self._conn()
        c = conn.cursor()
        try:
            yield c
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ---------- 对话历史 ----------
    def add_message(self, user_id: str, role: str, content: str):
        with self._cursor() as c:
            c.execute(
                "INSERT INTO messages(user_id, role, content) VALUES(?,?,?)",
                (user_id, role, content),
            )

    def get_recent_messages(self, user_id: str, n: int):
        with self._cursor() as c:
            rows = c.execute(
                "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, n),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def get_recent_raw(self, user_id: str, n: int):
        """取最近 n 条消息（含时间戳），按时间正序返回。"""
        with self._cursor() as c:
            rows = c.execute(
                "SELECT role, content, ts FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, n),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def count_user_messages(self, user_id: str) -> int:
        with self._cursor() as c:
            r = c.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id=? AND role='user'",
                (user_id,),
            ).fetchone()
        return r[0] if r else 0

    # ---------- 长期记忆 ----------
    def add_memory(self, user_id: str, content: str):
        content = (content or "").strip()
        if not content:
            return
        with self._cursor() as c:
            c.execute(
                "INSERT INTO memories(user_id, content) VALUES(?,?)", (user_id, content)
            )
        logger.info("新增长期记忆 [%s]: %s", user_id, content)

    def get_memories(self, user_id: str, limit: int = 50):
        with self._cursor() as c:
            rows = c.execute(
                "SELECT content FROM memories WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [r["content"] for r in rows]

    # ---------- 会话/用户列表 ----------
    def list_users(self) -> list:
        """返回所有有对话记录的 user_id（正序）。"""
        with self._cursor() as c:
            rows = c.execute(
                "SELECT DISTINCT user_id FROM messages ORDER BY user_id"
            ).fetchall()
        return [r["user_id"] for r in rows]

    
