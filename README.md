# Agent Fabric

> 统一控制、记忆与节点调度层 —— 位于现有 Harness（DSH / OpenCode / Codex / Hermes）之上。
> **Channel 与 Memory 归中央，Execution 归节点。**

用一个飞书 Bot（或任意入口）与"同一个持续存在的 Agent 身份"交互：Agent 在不同时间调用不同机器上的成熟 Harness、切换模型与执行节点，而任务状态、项目知识与经验持续积累，不跟具体机器绑定。

完整方案见 [docs/PLAN.md](docs/PLAN.md)，决策记录见 [docs/DECISIONS.md](docs/DECISIONS.md)，参考项目映射见 [docs/REFERENCES.md](docs/REFERENCES.md)。

## 架构（V0 已实现部分加粗）

```text
  飞书(V1,默认关) / REST /api/say        ← Channel 归中央
            │
   ┌────────▼─────────────────────────┐
   │      CENTRAL (FastAPI)           │
   │  命令路由 · 节点注册 · 任务管理    │
   │  **中央记忆(candidate→review→检索)**│
   └────────┬─────────────────────────┘
            │ WebSocket (af/1 控制面：小消息高频)
   ┌────────┴────────┬────────────────┐
   ▼                 ▼                ▼
 fabric-node      fabric-node     fabric-node   ← Execution 归节点
 (本机 test-node)  (麦片 mapian)   (…Windows-Home)
   echo/opencode   echo/opencode     …
```

- **af/1 协议**：`node.online / heartbeat / task.start / task.progress / task.result / task.failed / task.cancel / memory.candidate`（`fabric/shared/protocol.py`）
- **节点**：出站 WS（无公网 IP 也可接入）、2s→60s 指数退避重连、15s 心跳、断网 outbox（SQLite）重放，中央终态幂等
- **安全**：每节点独立 token；shell 默认拒绝（`AF_ALLOW_SHELL=1` 才开）

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/fabric-central                # 终端1：中央（默认 0.0.0.0:8000）
.venv/bin/fabric-node --node-id test-node --token dev-token   # 终端2：本机节点
curl -XPOST localhost:8000/api/say -H 'content-type: application/json' \
     -d '{"text":"run echo probe"}'
curl -s localhost:8000/api/tasks | python3 -m json.tool | head
```

## 命令语法（V0）

```text
run [@节点] [harness] <任务>     例：run @mapian opencode probe ／ run @mapian echo !df -h
status                          节点与任务
cancel <T-xxxxxx>               取消任务
memory list / memory search 关键词
```

## 调试 API

`GET /api/health` `GET /api/nodes` `GET /api/tasks` `GET /api/events?task_id=` 
`GET /api/memory?q=` `GET /api/memory/candidates` `POST /api/memory/review` `POST /api/say`

## Roadmap

- [x] V0：af/1 协议 + central + node + echo/opencode 适配器 + 本地 E2E
- [x] 跨机：central 常驻本机 VPS，麦片节点接入
- [ ] V1：飞书 channel 启用（需先解决与旧 dsh 桥互斥）、ACP 会话 adapter、handoff 跨节点续任务、PG+pgvector 记忆层、模型配额感知路由
- [ ] V2：skill 系统、经验提炼（dream/consolidate）、多入口（Web/CLI）
