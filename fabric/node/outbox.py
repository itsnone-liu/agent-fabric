"""节点本地 outbox（PLAN §23）：断网时事件落地，重连后按序重放，中央终态幂等吸收重复。"""
from __future__ import annotations

import json
import os
import sqlite3


class Outbox:
    def __init__(self, path):
        d = os.path.dirname(str(path))
        if d:
            os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT, env TEXT NOT NULL)")
        self.db.commit()

    def size(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def enqueue(self, env: dict):
        self.db.execute("INSERT INTO outbox(env) VALUES(?)", (json.dumps(env, ensure_ascii=False),))
        self.db.commit()

    async def drain(self, send) -> int:
        """按序重放；单条失败即停（连接又断了），下次重连继续。"""
        rows = self.db.execute("SELECT seq, env FROM outbox ORDER BY seq").fetchall()
        n = 0
        for seq, raw in rows:
            try:
                await send(json.loads(raw))
            except Exception:
                break
            self.db.execute("DELETE FROM outbox WHERE seq=?", (seq,))
            self.db.commit()
            n += 1
        return n
