# 夜间进展报告（2026-09-06 凌晨，用户睡眠期间自主推进）

## 一句话总结

**Agent Fabric V0 已上线并跨机跑通**：本机中央 + 麦片节点常驻运行，从"发命令"到"麦片上的 opencode 用订阅免费模型干活再回传结果"的完整闭环已实测打通。

## 已完成

| # | 事项 | 状态 |
|---|------|------|
| 1 | 仓库 `agent-fabric`（Python，af/1 协议/central/node/outbox/adapters） | ✅ 本地 E2E 测试 6/6 全绿，git 已提交 |
| 2 | **central 常驻本机**（154.64.231.1:8000，systemd `fabric-central`） | ✅ 开机自启，重启自动拉起 |
| 3 | **麦片节点接入**（154.64.231.89，systemd `fabric-node`，node_id=mapian） | ✅ 出站 WS、心跳、断线 2s 自动重连（central 重启实测验证）、outbox 零积压 |
| 4 | 本机也挂了 test-node 节点（对照/调试用） | ✅ 两节点同时在线 |
| 5 | **跨机真实任务**：`run @mapian echo probe` / `run @mapian opencode probe` / `run @mapian opencode <自然语言任务>` | ✅ 全部 done，结果与进度流实时回流 |
| 6 | **麦片 opencode 配好免费模型**：百炼订阅内 `deepseek-v4-flash-0731`（token-plan 端点，零新增费用），默认模型已设 | ✅ 实测模型自述正确 |
| 7 | 中央记忆最小版：任务完成自动交 candidate → 人工 review → 入库 → 检索 → 组装 context_package 随任务下发 | ✅ 流水线已产生首批 3 条候选 |
| 8 | 飞书 channel 代码已写完（长连接+白名单+回发），**默认关闭**（原因见下） | ⚠️ 待你一个决定 |
| 9 | 文档：PLAN（原方案全文）/ DECISIONS / REFERENCES（参考项目映射） | ✅ |

## 飞书为什么没直接启用（需要你拍板）

本机现在跑着你原来的 dsh 飞书桥（`dsh --profile feishu`，就是你现在跟我聊天的通道）。**同一个飞书应用的长连接事件会被所有客户端抢**，直接启用会双回复/打架。两个选项：

- **A（推荐）**：在飞书开放平台花 5 分钟新建一个专用 App（只要开"接收消息+回复消息"权限），把新 APP_ID/SECRET 给我，我把 fabric 的飞书入口点亮。
- **B**：复用现有 App——但需要先停旧桥（你当前的聊天通道会断）。

## 麦片 opencode 修复过程（记录给你）

1. 旧凭据（kimi）藏在 `opencode.db` 的 `credential` 表里，schema 老旧导致 1.17.9 全局报错 → 已清掉；
2. 7 月的老 DB 与新版迁移不兼容（`no such column: replacement_seq`）→ 旧库挪到 `opencode.db.old-20260906`，新库自动重建（旧 kimi 会话本来也废了）；
3. 现在配置：`~/.config/opencode/opencode.jsonc`（bailian provider）+ `auth.json`（订阅 key），可用模型：deepseek-v4-flash-0731（默认）/ qwen3.8-flash / glm-5.2 / deepseek-v4-pro 等 12 个订阅内模型。

## ⏰ 需要你处理的事

1. **麦片服务器 2026-09-13 到期**（还有 7 天）——记得续费，否则节点 2 下线（fabric-central 不受影响，它在本机）。[面板截图信息]
2. 飞书 A/B 选择（见上）。
3. GitHub 建远程仓库的话（比如 `agent-fabric`），给我地址，我推送。

## 怎么玩（现在就能用）

```bash
# 在任何能访问本机 8000 的地方
curl -XPOST http://154.64.231.1:8000/api/say -H 'content-type: application/json' \
     -d '{"text":"run @mapian opencode 帮我查一下磁盘占用并给出清理建议"}'
curl -s http://154.64.231.1:8000/api/tasks | python3 -m json.tool | head -30   # 看结果
curl -s -XPOST http://154.64.231.1:8000/api/say -H 'content-type: application/json' -d '{"text":"status"}'
```

命令语法：`run [@节点] [harness] 任务` ｜ `status` ｜ `cancel T-xxx` ｜ `memory list` ｜ `memory search 关键词`

## V1 待办（按优先级）

1. 飞书入口点亮（等 A/B 决定）
2. opencode ACP 会话模式（现在是 `opencode run` 一次性；ACP 才有多轮会话/工具循环）
3. handoff 跨节点续任务（麦片↔本机迁移任务）
4. PG+pgvector 记忆层（SQLite 结构已同构对齐，迁移成本低）+ 记忆 consolidate（Mem0 "dream"）
5. TLS/wss（域名定了就上）+ 配额感知模型路由（百炼 5h 滚动限额那类）

## 安全基线（当前状态）

- 节点准入：每节点独立长随机 token（在 `/root/dsh-ws/agent-fabric/.env`，已 gitignore）
- shell 执行：**默认拒绝**，麦片未开 `AF_ALLOW_SHELL`（要开随时说）
- 明文 ws（同机房内网段直连，风险可控；域名+TLS 是 V1 项）
