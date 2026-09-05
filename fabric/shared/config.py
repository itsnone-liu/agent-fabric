"""环境变量配置（中央与节点共用）。所有 AF_* 见 .env.example。"""
from __future__ import annotations

import os


def node_tokens() -> dict[str, str]:
    """解析 AF_NODE_TOKENS="node1:tok1,node2:tok2" → {node1: tok1}"""
    raw = os.getenv("AF_NODE_TOKENS", "test-node:dev-token")
    out: dict[str, str] = {}
    for part in raw.split(","):
        if ":" in part:
            nid, tok = part.split(":", 1)
            nid, tok = nid.strip(), tok.strip()
            if nid and tok:
                out[nid] = tok
    return out


def host() -> str:
    return os.getenv("AF_HOST", "0.0.0.0")


def port() -> int:
    return int(os.getenv("AF_PORT", "8000"))


def db_path() -> str:
    return os.getenv("AF_DB", "data/fabric.db")
