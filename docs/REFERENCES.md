# 参考项目 → 实现映射（PLAN §27-33，用户要求"技术上尽量参考成熟项目"）

| 项目 | 借什么 | 落在哪里 |
|------|--------|----------|
| **Syne**（riyogarta/syne） | remote node 出站 WS、指数退避重连（其参数 5s→60s，我们取 2s→60s）、心跳判活、节点注册表 | `fabric/node/daemon.py` `fabric/central/registry.py` |
| **Engram**（thebtf/engram） | 中央 memory server + 多 workstation token 准入；"热路径零 LLM"（session-start 不调模型）——我们的关键词检索 V0 同哲学 | `fabric/central/store.py` `memory.py`；节点 token 准入 `shared/config.py` |
| **Agent Memory OS**（yamantaka520） | scope 命名（private:user / agent / project / global）、硬 ACL 先于排序（不做软加权） | memories.scope 字段；检索前 scope 过滤（V1） |
| **Mem0**（mem0ai/mem0） | triage→recall→dream 生命周期 → 我们的 capture(candidate)→review→consolidate(V1) | `fabric/central/memory.py` |
| **agent-gateway**（agent-guide） | runtime / agent-SPI 分层；`opencode acp --cwd` 与 codex-acp 的启动方式；V1 的 ACP 会话 adapter 直接照此写 | `fabric/node/adapters/`（V0 先 CLI，V1 换 ACP） |
| **Felix**（sausheong/felix） | "session 不属于入口"：Browser/CLI 共享 session ↔ 我们 Feishu/REST 共享 handle_user_text | `fabric/central/app.py::handle_user_text` |

## 明确不 fork 的理由（PLAN §34）

我们的架构横跨这些项目各自的局部；自写薄 Fabric Core，只借协议/思想/参数，避免永久维护一个改得面目全非的 fork。
