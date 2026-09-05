可以。下面这版我把它收敛成一个真正可开发的系统方案，暂时给它一个工作名：**Agent Fabric**。

它不是新的 Agent，也不是新的 Harness，而是一个位于它们之上的**统一控制、记忆和节点调度层**。

---

# 一、目标定义

系统要解决的是：

> 用一个飞书 Bot 与“同一个持续存在的 Agent 身份”交互；这个 Agent 可以在不同时间调用不同机器上的成熟 Harness，切换模型和执行节点，但任务状态、项目知识、经验和技能持续积累，不跟具体机器绑定。

最终体验应该是：

```text
你：
去服务器继续改昨天那个 OpenCode bridge。

Agent Fabric：
→ 找到昨天的任务
→ 找到相关记忆
→ 判断 Ubuntu-01 在线
→ 选择 OpenCode + DeepSeek
→ 恢复任务
→ 工作

你：
服务器我等会关机，你换到家里电脑继续。

Agent Fabric：
→ 保存 checkpoint
→ 选择 Windows-Home
→ 注入 handoff context
→ 调用 Codex
→ 继续任务
```

飞书 Bot、记忆和“Agent 身份”都没有变化。

---

# 二、总架构

建议采用：

```text
                         ┌──────────────────────┐
                         │     Feishu Bot       │
                         │   唯一用户入口        │
                         └──────────┬───────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────┐
│                     CENTRAL CONTROLLER                      │
│                                                             │
│  Identity / Conversation / Project / Task                   │
│                                                             │
│  Command Router                                             │
│  Node Scheduler                                             │
│  Harness Router                                             │
│  Model Policy                                               │
│  Memory Manager                                             │
│  Session / Handoff Manager                                  │
│                                                             │
└────────────┬───────────────────────┬────────────────────────┘
             │                       │
             ▼                       ▼
┌────────────────────┐     ┌─────────────────────┐
│   Central Memory   │     │    Node Registry    │
│                    │     │                     │
│ Task State         │     │ node status         │
│ Project Memory     │     │ capabilities        │
│ Experience         │     │ harness             │
│ Skills             │     │ models              │
│ Artifacts Index    │     │ workspace           │
└────────────────────┘     └──────────┬──────────┘
                                     │
                              WebSocket / RPC
                                     │
          ┌──────────────────────────┼──────────────────────────┐
          │                          │                          │
          ▼                          ▼                          ▼
┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
│ Windows-Home    │       │ Ubuntu-Server   │       │ GPU-Server      │
│                 │       │                 │       │                 │
│ Node Agent      │       │ Node Agent      │       │ Node Agent      │
│       ↓         │       │       ↓         │       │       ↓         │
│ Codex / DSH     │       │ OpenCode / DSH  │       │ Harness X       │
│       ↓         │       │       ↓         │       │       ↓         │
│ GPT / GLM       │       │ DeepSeek / GLM  │       │ Local Model     │
└─────────────────┘       └─────────────────┘       └─────────────────┘
```

最重要的边界有三个：

**Channel 属于中央。Memory 属于中央。Execution 属于节点。**

---

# 三、中央控制器

中央控制器不是“大 Agent”。

它主要做 orchestration。

建议至少分成以下几个模块。

## 1. Channel Gateway

飞书只配置一次：

```text
Feishu App
APP_ID
APP_SECRET
event stream
card actions
user open_id
```

全部存在中央。

工作节点完全不知道飞书存在。

节点只和 Central Controller 通信。

这样避免：

```text
Bot 重复登录
多实例消息重复消费
节点切换时迁移 token
节点宕机导致 Bot 消失
```

中央收到飞书消息后，把它转换成内部事件：

```json
{
  "type": "user.message",
  "user_id": "u001",
  "conversation_id": "conv_91",
  "text": "去服务器继续改那个 bridge"
}
```

---

# 四、Agent Identity

系统一定不要用：

```text
Node ID = Agent ID
```

而应该是：

```text
Agent Identity
    ↓
可以使用多个 Node
    ↓
可以使用多个 Harness
    ↓
可以使用多个 Model
```

例如：

```yaml
agent:
  id: primary
  owner: user001

default_preferences:
  coding:
    harness: dsh
    model: glm53

memory_namespace:
  user: user001
```

这样未来换掉 DSH、OpenCode、Codex 都不影响身份。

---

# 五、节点设计

每台机器只安装一个很轻的：

```text
fabric-node
```

它不需要自己有飞书 Bot。

职责：

```text
连接中央
报告机器能力
接受任务
启动 Harness
回传事件
保存本地 checkpoint/outbox
```

例如：

```yaml
node_id: ubuntu-main

os: ubuntu

capabilities:
  shell: true
  docker: true
  gpu: false

harnesses:
  - dsh
  - opencode
  - hermes

models:
  - glm53
  - deepseek-v4

workspaces:
  dsh:
    path: /opt/dsh
  opencode-bridge:
    path: /opt/opencode-bridge
```

节点主动连接中央：

```text
Node
   │
   │ outbound WebSocket
   ▼
Cloud Controller
```

这样家庭电脑不需要公网 IP。

---

# 六、通信机制

这里继续沿用刚才讨论的原则：

> 高频轻通信，低频重数据。

节点与中央保持 WebSocket。

控制消息可能非常频繁：

```text
heartbeat
task_started
step_completed
tool_started
tool_completed
checkpoint
task_finished
```

但都很小。

真正大的内容：

```text
完整日志
文件
patch
artifact
长 transcript
```

只按需上传。

因此：

```text
Control Plane
WebSocket
高频、小数据

Data Plane
HTTP/Object Storage/Git
低频、大数据
```

最好不要所有文件也塞 WebSocket。

---

# 七、Node Protocol

V1 不需要复杂。

先定义十几个事件已经足够。

### Central → Node

```text
node.configure

task.start
task.resume
task.message
task.cancel

session.snapshot
session.restore

model.switch
harness.switch
```

### Node → Central

```text
node.online
node.status
node.capabilities

task.accepted
task.running
task.progress
task.checkpoint
task.result
task.failed

memory.candidate
skill.candidate

artifact.created
```

例如：

```json
{
  "type": "task.start",
  "task_id": "T-882",
  "workspace": "opencode-bridge",
  "harness": "opencode",
  "model_profile": "deepseek-v4",
  "context_package": "CTX-199"
}
```

---

# 八、Harness Adapter

这是节点最重要的抽象。

统一接口：

```text
HarnessAdapter
│
├── start()
├── resume()
├── send()
├── cancel()
├── status()
├── transcript()
└── checkpoint()
```

然后分别实现：

```text
DSHAdapter
CodexAdapter
OpenCodeAdapter
HermesAdapter
ClaudeCodeAdapter
```

中央不用知道：

```text
opencode 是 ACP
Codex 是 app-server
DSH 是 CLI
Hermes 是自己的协议
```

这些全部藏在 adapter 后面。

---

# 九、现有 Harness 不重造

这一点保持你的原则：

**执行端必须优先使用现有成熟 Harness。**

我们不重新实现：

```text
shell tools
code edit
repo search
planning
tool loop
subagent
permission system
```

因为 Codex、DSH、OpenCode、Hermes 已经做了。

Agent Fabric 只做：

```text
谁执行
在哪里执行
用什么执行
带什么记忆执行
执行完留下什么
```

---

# 十、模型切换

模型分成两层。

中央保存：

```text
Model Profile
```

比如：

```yaml
glm-fast:
  capability: coding
  cost: low

deepseek-heavy:
  capability: long_context

gpt-frontier:
  capability: architecture
```

节点保存真正 provider 配置：

```text
API_KEY
base_url
subscription auth
CLI login
model mapping
```

例如中央只发：

```text
model_profile = glm-fast
```

节点自己解析成：

```text
provider = zai
model = glm-5.3-flash
credential = local secret
```

优点是凭据不用全部集中到中央。

---

# 十一、Node Scheduler

中央节点选择不要一开始用 LLM。

先规则化。

例如：

```text
requires_windows
→ Windows

requires_gpu
→ GPU node

workspace exists locally
→ +30

harness already running
→ +20

preferred model available
→ +20

machine idle
→ +10

user explicitly specified
→ +100
```

最后得分最高的节点执行。

以后再加智能调度。

---

# 十二、Memory 架构

这一块是整个项目真正的核心。

我建议明确拆成：

```text
Working Context
Task State
Project Memory
Experience Memory
Skill Memory
Artifact Index
```

---

# 十三、Working Context

这是节点本地当前 Harness session。

例如：

```text
对话历史
最近读取文件
tool results
reasoning state
```

中央不要实时维护完整副本。

Harness 自己维护。

生命周期：

```text
当前 session
```

---

# 十四、Task State

中央保存。

例如：

```yaml
task: T882

goal:
  fix ACP reconnect

status:
  running

node:
  ubuntu-main

harness:
  opencode

branch:
  fix/acp-session

completed:
  - heartbeat fixed
  - reconnect added

pending:
  - restore session

last_checkpoint:
  CP102
```

这是跨节点 continuation 的关键。

---

# 十五、Project Memory

例如：

```text
项目架构
重要技术决策
目录结构
部署方式
约定
已知问题
```

这些应该长期保存。

例如：

```yaml
project: dsh-feishu

memory:
  - Feishu uses long connection
  - allowedOpenIds controls access
  - node-sdk is preferred
  - card action handling is separate
```

---

# 十六、Experience Memory

这是项目真正产生长期价值的地方。

节点完成任务以后可能发现：

```text
OpenCode reconnect 后 session 不能直接依赖旧 connection id。
```

这应该成为：

```yaml
type: experience

topic:
  opencode ACP

trigger:
  reconnect

lesson:
  restore session independently from transport connection

confidence:
  0.82

source:
  task T882
```

以后其他项目也可以检索。

---

# 十七、Skill Memory

Skill 和经验分开。

经验：

```text
知道该怎么做
```

Skill：

```text
已经形成可重复执行的方法
```

例如：

```text
repair_feishu_connection

inspect_acp_session

repo_architecture_scan
```

Skill 可能包含：

```text
说明
prompt
shell script
python
workflow
工具组合
```

最终甚至可以安装到 harness。

---

# 十八、Memory 生命周期

节点不应该直接无限写永久记忆。

正确流程：

```text
工作节点
    ↓
产生 memory candidates
    ↓
Central Memory Manager
    ↓
分类
 ┌─────────┬──────────┬────────────┐
 ▼         ▼          ▼            ▼
Task     Project    Experience    Skill
```

例如节点提交：

```json
{
  "type": "memory.candidate",
  "task": "T882",
  "content": "ACP reconnect should recreate transport state..."
}
```

Memory Manager 判断：

```text
重复吗？
已有知识冲突吗？
只和当前任务相关吗？
是否值得永久保存？
是否足够成熟成为 skill？
```

---

# 十九、Memory 不做全量同步

这一点明确：

**中央是 Source of Truth。**

节点不是 memory replica。

结构：

```text
Central Memory
     ↓ retrieval
Context Package
     ↓
Node
```

每次任务只传：

```text
current task
相关项目知识
相关经验
必要技能
机器信息
```

而不是：

```text
半年所有历史聊天
```

---

# 二十、Context Package

中央每次启动任务生成：

```yaml
context_package:

identity:
  user preferences

goal:
  current task

task_state:
  previous progress

project_context:
  relevant facts

experience:
  top relevant lessons

skills:
  recommended reusable procedures

machine:
  local paths / capabilities
```

给 Harness 的上下文控制在合理 token budget。

例如：

```text
identity       300 tokens
task          500
project       1500
experience    1000
skills        1000

≈ 4300 tokens
```

不会随着时间无限膨胀。

---

# 二十一、Handoff

节点迁移的关键不是复制 session，而是：

```text
handoff package
```

例如：

```yaml
task:
  T882

goal:
  fix ACP reconnect

previous_node:
  ubuntu-main

repo:
  branch: fix/session
  commit: 8fa23

changes:
  - src/acp/session.ts
  - src/router.ts

tests:
  passed: 14
  failed: 2

known_problem:
  reload loses session mapping

next_action:
  inspect session recovery path
```

新节点拿这个继续。

如果 Harness 原生支持 session restore：

```text
优先恢复原 session
```

否则：

```text
handoff + git state + relevant memory
```

继续。

---

# 二十二、代码/文件状态不要靠 Memory 同步

这一点很重要。

代码同步交给：

```text
Git
```

大型 artifact：

```text
Object Storage
```

Memory 只保存索引：

```text
repo
branch
commit
artifact id
```

不要让中央 memory store 变成文件同步系统。

---

# 二十三、本地断网机制

节点可以放一个极轻量 SQLite：

```text
node.db

pending_events
pending_results
pending_memory
checkpoints
```

断网时：

```text
Harness 继续工作
      ↓
写 local outbox
```

恢复以后：

```text
outbox
→ Central
```

因此中央暂时掉线不会导致任务中断。

---

# 二十四、推荐技术栈

Central：

```text
Go / Rust / TypeScript
```

考虑你现有 DSH/Agent 环境，我会偏向：

```text
Go
```

或者：

```text
TypeScript
```

V1 数据：

```text
PostgreSQL
```

向量：

```text
pgvector
```

缓存/消息：

```text
最初不需要 Redis
```

节点通信：

```text
WebSocket + JSON
```

大文件：

```text
HTTP / S3-compatible
```

代码：

```text
Git
```

---

# 二十五、推荐数据库结构

V1：

```text
users
agents
conversations

projects
tasks
task_checkpoints

nodes
node_capabilities

harness_profiles
model_profiles

memories
memory_links
skills

artifacts

events
```

Memory 至少：

```text
id
type
scope
project_id
content
embedding
importance
confidence
source_task
created_at
updated_at
```

---

# 二十六、Memory Scope

建议一开始就支持：

```text
private:user

agent:primary

project:dsh

team:xxx

global
```

以后如果有多个 Agent：

```text
Agent A
能看到 project:dsh

Agent B
也能看到 project:dsh

但不能看到 private:user
```

这个设计可以参考 Agent Memory OS。

它已经实现 agent/team/project ACL、共享 memory store，并明确支持 Claude Code、Codex、OpenClaw 和多个 Hermes profile 共存。citeturn778121search2turn778121search3

---

# 二十七、参考项目：第一优先级

## Syne

这是目前和我们整体架构最像的。

它已经实现：

```text
中央 Syne Server
     ↕
Persistent WebSocket
     ↕
Remote Node
```

Remote Node 可以在另一台机器执行命令和访问文件，并通过 Telegram 中央控制；配对之后自动重连。citeturn394733search5

非常值得直接研究：

```text
Remote Node protocol
pairing
heartbeat
reconnect
remote exec
node registry
```

GitHub：

urlSyne repositoryhttps://github.com/riyogarta/syne

我认为 **Node Layer 可以优先参考 Syne**。

---

# 二十八、参考项目：Engram

Engram 直接解决：

> 一台中央 memory server，多台 coding workstation 共用。

它使用 PostgreSQL 保存 memory、rules、issues、documents 等；每个 workstation 用自己的 token 接中央，并通过本地 MCP 进程暴露给 Agent。官方定位就是 multiple workstations + shared persistent memory。citeturn394733search1

非常值得研究：

```text
memory server
workstation identity
MCP bridge
server/workstation auth
memory injection
```

GitHub：

urlEngram repositoryhttps://github.com/thebtf/engram

但有一点值得注意：Engram v5 反而简化了原来的复杂 relevance/graph/reranking/extraction 路径，目前主要采用静态 composite injection。citeturn394733search1

这恰好说明：

**Memory 系统别一开始设计得太聪明。**

---

# 二十九、参考项目：Agent Memory OS

这个项目和你的需求极其相关。

它明确考虑：

```text
Claude Code
Codex
OpenClaw
Hermes
```

共同使用一个 memory store。

支持：

```text
agent memory
team memory
project memory
ACL
federated sync
MCP
```

甚至已经提供 Hermes 原生 memory provider。citeturn778121search2turn778121search3

GitHub：

urlAgent Memory OS repositoryhttps://github.com/yamantaka520/Agent-Memory-OS

这里重点研究：

```text
Memory namespace
ACL
context pack
memory consolidation
agent identity
MCP integration
```

---

# 三十、参考项目：Mem0

如果以后要把 Experience Memory 做好，Mem0 很值得参考。

它最新的 OpenClaw integration 已经做成：

```text
Triage
→ 判断什么值得记

Recall
→ 查询相关 memory 并注入

Dream
→ 合并重复、解决冲突、清理过时 memory
```

citeturn778121search0turn778121search4

这套：

```text
Triage
Recall
Dream
```

非常适合直接借鉴成我们的：

```text
Capture
Retrieve
Consolidate
```

GitHub：

urlMem0 repositoryhttps://github.com/mem0ai/mem0

---

# 三十一、Mem0 Multi Pool

还有一个小项目非常值得看架构思想：

`openclaw-mem0-multi-pool`

它解决的是：

```text
多个 Agent
共享某些 memory pool
同时隔离另外一些 memory pool
```

即：

```text
capture pool
recall pools
```

可以 N:M 配置。citeturn778121search6

这非常适合我们以后：

```text
primary agent
coding agent
research agent
family agent
```

之间共享不同层级的 memory。

urlopenclaw-mem0-multi-pool repositoryhttps://github.com/jamebobob/openclaw-mem0-multi-pool

---

# 三十二、参考项目：AgentGuide agent-gateway

这个对 Harness 层尤其重要。

它已经明确支持：

```text
ACP
Codex
OpenCode
```

并且有：

```text
ACP service
ACP route
runtime pool
thread/session
transcript replay
model config
permission handling
```

citeturn394733search0turn394733search2

OpenCode 就直接启动：

```text
opencode acp --cwd <cwd>
```

Codex 则通过 `codex-acp` adapter。citeturn394733search2

这说明我们自己的：

```text
Harness Adapter Layer
```

完全可以大量参考它。

GitHub：

urlAgentGuide agent-gateway repositoryhttps://github.com/agent-guide/agent-gateway

尤其建议拆：

```text
pkg/acp/runtime
pkg/acp/agentspi
pkg/acp/agent/codex
pkg/acp/agent/opencode
```

它的依赖设计也很好：

```text
runtime
   ↓
agent SPI
   ↑
Codex/OpenCode adapter
```

跟我们需要的一样。citeturn394733search2

不过它目前 memory 还没有真正实现，官方 README 明确说 memory API 仍是 reserved、返回 501。citeturn394733search0

所以：

> AgentGuide 可以借执行控制层，不能直接当我们的 Memory 层。

---

# 三十三、参考项目：Felix

Felix 很适合研究：

```text
session 不属于入口
```

它的 Browser Chat 和 CLI 可以共享：

```text
session
memory
MCP state
```

还提供 WebSocket JSON-RPC control plane。citeturn394733search3

这正好对应我们一个重要原则：

```text
Feishu
Web
CLI
```

以后都只是入口。

不是 Agent 本身。

GitHub：

urlFelix repositoryhttps://github.com/sausheong/felix

---

# 三十四、这些项目怎么拼

我现在会这样理解：

```text
Syne
↓
借 Node architecture

AgentGuide Gateway
↓
借 Harness / ACP control architecture

Engram
↓
借 Central Memory server architecture

Agent Memory OS
↓
借 Memory namespace / ACL / agent identity

Mem0
↓
借 Memory lifecycle

Felix
↓
借 Session / interface separation
```

但**不建议直接 fork 一个然后不断改**。

因为我们的架构实际上横跨了这些项目各自的一部分。

更合理的是：

```text
自己写很薄的 Fabric Core

大量复用思想 / 协议 / adapter
```

---

# 三十五、V1 不要做的东西

这非常重要。

先不要做：

```text
知识图谱
复杂 agent society
LLM 自动节点调度
完全自动 skill evolution
多 master
memory federation
P2P nodes
CRDT
完整 distributed DB
```

否则这个项目马上失控。

---

# 三十六、V1 应该做到什么

我会把 V1 收得非常小：

```text
1 个 Central

1 个 Feishu Bot

2 台 Node

2 种 Harness
例如：
DSH
OpenCode

3 种 Memory
Task
Project
Experience

2 个模型

WebSocket 通信

PostgreSQL 中央存储
```

做到：

```text
飞书：
去服务器做任务

↓

Server 执行

↓

飞书：
换电脑继续

↓

PC 恢复 task

↓

任务完成

↓

经验写入中央 memory

↓

三天后新任务自动检索该经验
```

如果这个闭环跑通：

**整个系统的核心价值就已经验证了。**

---

# 三十七、推荐开发顺序

最合理的顺序不是先 Memory。

而是：

```text
Phase 1
Central ↔ Node

Phase 2
Node ↔ Harness

Phase 3
Feishu ↔ Central

Phase 4
Task State / Handoff

Phase 5
Project Memory

Phase 6
Experience extraction

Phase 7
Skill system

Phase 8
Model / Node intelligent routing
```

原因很简单：

没有可靠的：

```text
Task continuity
```

谈再高级的 memory 都没有意义。

---

# 三十八、第一版目录甚至可以这么设计

```text
agent-fabric/

central/
  channel/
    feishu/

  controller/
    router/
    scheduler/

  memory/
    task/
    project/
    experience/
    skill/

  registry/
    node/
    harness/
    model/

  protocol/

node/
  daemon/
  outbox/

  adapters/
    dsh/
    opencode/
    codex/
    hermes/

shared/
  protocol/
  schemas/

deploy/
  docker/
```

---

# 三十九、长期版本

真正成熟以后，它可能变成：

```text
                 Agent Identity
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
      Memory         Tasks          Skills
        │              │              │
        └──────────────┼──────────────┘
                       ▼
                   Scheduler
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
        Codex         DSH        OpenCode
          │            │            │
          ▼            ▼            ▼
       Windows       Linux        Server
          │            │            │
          ▼            ▼            ▼
        GPT          GLM        DeepSeek
```

这时候：

```text
机器
Harness
模型
Channel
```

全部都成为可替换组件。

真正不能替换的反而只剩：

```text
Identity
Memory
Tasks
Skills
```

这四样才是 Agent 的连续性。

---

## 最终判断

我认为这个方向值得做，而且跟单纯做一个“飞书多机控制器”已经不是一回事。

它真正解决的是：

> **Agent 的身份与执行环境解耦。**

今天它在：

```text
Windows + Codex + GPT
```

明天可以在：

```text
Ubuntu + DSH + GLM
```

后天变成：

```text
Server + OpenCode + DeepSeek
```

但它仍然知道：

```text
以前做过什么
当前做到哪里
哪些方法有效
哪些坑踩过
有哪些已经形成的技能
```

而现有 GitHub 项目基本已经把我们需要的各个局部证明了一遍：Syne 证明 remote node，AgentGuide 证明 ACP harness gateway，Engram/Agent Memory OS 证明跨工作站共享记忆，Mem0 证明记忆提炼生命周期，Felix 证明入口和 session 可以解耦。citeturn394733search5turn394733search0turn394733search1turn778121search2turn778121search0turn394733search3

所以我们真正需要创新的，并不是重新发明其中任何一块，而是**把这些能力用正确边界拼成一个面向个人、多机器、多 Harness 的持久 Agent Fabric。** memcite

---

If you want, I can:

- 设计Agent Fabric系统的模块详细功能
- 制定节点选择和任务调度规则
- 规划Memory管理的具体流程和生命周期