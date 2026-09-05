# Agent Fabric 决策记录

## 2026-09-05 V0 启动（用户拍板项）

| # | 决策 | 依据/备注 |
|---|------|-----------|
| 1 | 名称 agent-fabric；语言 Python（≥3.10） | 单用户系统可维护性优先；麦片是 Py3.10 |
| 2 | 拓扑：中央=Ubuntu VPS 154.64.231.1（7×24，与麦片同机房）；节点2=麦片 154.64.231.89（RainYun LA，Ubuntu 22.04，opencode 1.17.9 + Hermes） | 用户指定"中央肯定是ubuntu，第二个用麦片" |
| 3 | Adapter 接口 V0 只保留 start/cancel（+progress 流）；checkpoint/transcript 不做一等公民 | 评审结论：checkpoint 最难且 §21 已给 handoff+git 兜底 |
| 4 | 记忆策略：中央记忆为唯一权威层，自研（参考 Engram/Mem0/Agent Memory OS），不基于 dsh-evolve；各 harness 自带记忆保留不动，V1 不统一不桥接；"剥离 harness 内建记忆只留中央"为可选后续 | 用户原话"留着就是，甚至改造掉也行只留中央的" |
| 5 | 安全基线：每节点独立 token（hmac 比较）+ 出站 WS；shell 执行默认拒绝（节点侧 AF_ALLOW_SHELL=1 才开）；TLS/wss 待域名后补 | 节点执行 shell，不能裸奔 |
| 6 | 存储 V0=SQLite(WAL)，表结构对齐 PLAN §25；PG+pgvector compose 已备，记忆层成熟后迁移 | V0 零依赖快速验证 |
| 7 | 麦片 opencode 模型：百炼订阅内免费模型 deepseek-v4-flash-0731（token-plan 端点），复用现有订阅 key，不注册新服务 | 用户："选免费的就行" |
| 8 | 飞书 channel 代码就绪但默认关闭：同一 App 长连接会与本机运行中的 dsh feishu 桥竞争事件，启用前需二选一（停旧桥 / 新建独立 App） | 避免双回复事故 |
| 9 | 重连退避 2s→60s、心跳 15s、离线 outbox 重放、中央终态幂等 | 对齐 Syne remote node 实测参数 |
| 10 | 技术实现尽量对齐 PLAN §27-33 列出的成熟项目 | 用户指示；映射见 REFERENCES.md |

## V0 明确不做（PLAN §35）

知识图谱 / LLM 调度 / 多 master / CRDT / P2P / memory federation / 全自动 skill 演化。
