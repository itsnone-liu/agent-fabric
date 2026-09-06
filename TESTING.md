# 测试报告 —— V0 双节点 opencode 闭环（2026-09-06）

## 模型配置（硬约束：测试只用免费模型）

| 节点 | opencode | 测试模型（AF_OPENCODE_MODEL） | 费用 |
|---|---|---|---|
| mapian（麦片 154.64.231.89） | 1.17.9 (npx) | `opencode/mimo-v2.5-free` | 0（zen 匿名免费层） |
| test-node（本机 154.64.231.1） | 1.17.11 (/root/.opencode/bin) | `opencode/nemotron-3.5-lightning-free` | 0（zen 匿名免费层） |

- **zen 免费模型经 CLI 匿名可用，无需任何 key**（`opencode run --model opencode/xxx-free` 直接通；两台机器 0 credentials 状态实测）。
- 直连 `https://opencode.ai/zen/v1/chat/completions` 需要 key（/models 端点开放但 chat 强鉴权）。
- 用户提供的 zen 凭据（client_id `cli_…` + key）已在三种格式下实测直连 chat 均 401 —— 留存 `.env`（AF_ZEN_*）备用，待确认正确用法（疑似 OAuth 用途或需配套端点）。
- 麦片原配置 `bailian/deepseek-v4-flash-0731`（订阅模型）已从节点 .env 移除，杜绝测试误用订阅。

## 测试矩阵

| # | 项目 | 结果 | 证据 |
|---|---|---|---|
| T1 | 双节点 probe | ✅ | mapian: opencode 1.17.9；test-node: 1.17.11，模型钉定正确 |
| T2a | mapian 真实任务（建文件+读回） | ✅ | `workspace/t2_mapian.txt` 磁盘可查，内容含模型自报名称 |
| T2b | test-node 真实任务（写代码+运行） | ✅ | `fib.py` 271B，fibonacci(1..10) 输出全对 |
| T4 | 记忆→任务注入链路 | ✅ | 候选 m-d5fe3bdf34 promote 后，下个任务模型回报 `CONTEXT-OK` 并摘出记忆原文；prompt 459 字 vs 裸 goal ~100 字 |
| T5 | 双节点并行 | ✅ | T-ca1ce8 / T-147edd 同时在跑、各自完成 |
| T6 | 取消（杀进程） | ✅ | cancel 后无残留 opencode 进程；中央侧 canceling 竞态兜底已修（canceled=True） |
| T8 | 经验候选回流闭环 | ✅ | 每个任务（含失败的）都自动提交 memory.candidate（9 条 pending） |
| R | 本地 E2E 回归 | ✅ | tests/test_e2e.py 6/6 |

已知边界（免费模型能力差异，非 fabric 缺陷）：
- nemotron-3.5-lightning-free 偶尔只答话不调工具（T4 未建 t4_ctx.txt，但上下文注入已由回报证明）；mimo-v2.5-free 工具调用稳定。
- 中文 goal 在 V0 关键词检索下召回弱（分词是按空格），V1 换 pgvector+BM25 解决。

## 本轮代码改动

- `node/adapters/base.py`：RunContext 增加 context_package
- `node/adapters/opencode.py`：注入中央记忆到 prompt（`experience`/`memories` 双键兼容）、AF_OPENCODE_TIMEOUT 可配、prompt 长度回报
- `node/daemon.py`：透传 context_package
- `central/tasks.py`：canceling 状态下失败结果兜底标记 canceled（修节点侧毫秒竞态）
- 麦片 `/opt/agent-fabric/.env`：AF_OPENCODE_MODEL → zen 免费模型

## 运维备忘

- 本机节点 unit 名是 `fabric-node-local`（不是 fabric-node），改代码后必须 `systemctl restart fabric-node-local`（曾因 start 幂等踩坑：旧进程存活 8h 用旧代码旧环境）。
- 麦片部署 = rsync（非 git），改完中央/共享代码要手动同步。
