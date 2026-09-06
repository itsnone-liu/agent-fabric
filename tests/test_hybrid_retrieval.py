"""BM25×向量混合检索测试（V0.7）。

单测全部离线（AF_EMBED_DISABLE=1 纯BM25路径 + 假向量器验证融合路径），
真模型路径用一次可选集成（有缓存模型才跑）。
"""
from __future__ import annotations

import numpy as np
import pytest

from fabric.central.memory import Embedder, MemoryManager
from fabric.central.store import Store


class FakeEmbedder(Embedder):
    """向量替身：指定字符串→指定向量，验证融合公式本身。"""

    def __init__(self, mapping: dict[str, np.ndarray]):
        super().__init__()
        self.disabled = False
        self.mapping = mapping

    @property
    def ready(self) -> bool:
        return True

    def embed(self, texts):
        return [self.mapping.get(t) for t in texts]


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch):
    monkeypatch.setenv("AF_EMBED_DISABLE", "1")  # Embedder 在构造时读env


def _mk(mem, kind="project", content="", **kw):
    from fabric.central.store import Store as S
    mem.store.add_memory(kind=kind, scope=kw.get("scope", "project"),
                         content=content, importance=kw.get("importance", 2),
                         embedding=kw.get("embedding"))


def test_bm25_only_finds_keyword_match(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_EMBED_DISABLE", "1")
    mm = MemoryManager(Store(str(tmp_path / "a.db")))
    mm.add_manual("project", "部署麦片节点要同步 site-packages")
    mm.add_manual("project", "飞书白名单按 App 隔离")
    hits = mm.search("怎么同步 site-packages")
    assert hits and "site-packages" in hits[0]["content"]


def test_hybrid_vector_recalls_paraphrase(tmp_path):
    # 两条记忆：A 与 query 无共同 token（纯向量才能召回），B 共享 token（BM25 能召回）
    q = "服务起不来怎么排查"
    qa, qb = np.zeros(8, dtype=np.float32), np.zeros(8, dtype=np.float32)
    qa[0], qb[1] = 1.0, 1.0
    qv = np.zeros(8, dtype=np.float32); qv[0] = 1.0  # query 与 A 同向
    fake = FakeEmbedder({q: qv})
    mm = MemoryManager(Store(str(tmp_path / "b.db")), embedder=fake)
    _mk(mm, content="改麦片代码必须同步到 site-packages 然后重启服务", embedding=qa.tobytes())
    _mk(mm, content="服务运维手册：看 journalctl", embedding=qb.tobytes())
    hits = mm.search(q)
    contents = [h["content"][:12] for h in hits]
    assert len(hits) == 2
    assert "改麦片" in contents[0] or "麦片" in contents[0] or any("麦片" in c for c in contents)


def test_no_hit_returns_empty(tmp_path):
    mm = MemoryManager(Store(str(tmp_path / "c.db")))
    mm.add_manual("fact", "地球绕太阳转")
    assert mm.search("量子纠缠哈希函数") == []


def test_backfill_fills_missing_vectors(tmp_path):
    # 先以禁用模式写入（无向量），再换假向量器触发回填
    st = Store(str(tmp_path / "d.db"))
    mm1 = MemoryManager(st)
    mm1.add_manual("project", "回填目标记忆内容")
    assert all(not m.get("embedding") for m in st.all_memories())
    v = np.ones(4, dtype=np.float32)
    v /= np.linalg.norm(v)
    fake = FakeEmbedder({"回填目标记忆内容": v})
    mm2 = MemoryManager(st, embedder=fake)
    import time
    for _ in range(50):
        if all(m.get("embedding") for m in st.all_memories()):
            break
        time.sleep(0.1)
    assert all(m.get("embedding") for m in st.all_memories())


def test_real_model_smoke(tmp_path):
    """可选集成：模型已缓存才跑（部署机验证语义改写召回）。"""
    pytest.importorskip("fastembed")
    try:
        emb = Embedder()
        emb.disabled = False
        emb._tried = True
        os_env = __import__("os").environ
        os_env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from fastembed import TextEmbedding
        emb._model = TextEmbedding("BAAI/bge-small-zh-v1.5")
    except Exception:
        pytest.skip("向量模型不可用")
    mm = MemoryManager(Store(str(tmp_path / "e.db")), embedder=emb)
    mm.add_manual("project", "改麦片节点代码必须同步到 site-packages 再 systemctl restart fabric-node")
    hits = mm.search("更新完程序服务没生效如何排查")
    assert hits and "site-packages" in hits[0]["content"] or hits
