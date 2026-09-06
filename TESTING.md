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

## handoff（任务可续）—— 2026-09-06 第二轮

- 单测+E2E：tests/test_handoff.py 5 项（快照差分/路径逃逸/超限跳过/跨节点续跑/不存在任务）全过；全套 11/11。
- 真机跨机：mapian(mimo) 创建 chain_demo.py → handoff 捕获 → `resume T-2453c1 @test-node` → 本机恢复文件、模型在**原文件基础上**追加修改（保留 mapian 的首行）、运行输出正确。
- 失败任务也带 handoff（504 中断后文件照常上报）→ `resume` 失败任务即断点续跑。
- 链式语义：节点把"恢复且未变"的文件并入自己的 handoff，resume(resume(T)) 任意长度不断档。
- 已知：zen 免费层偶发 `504 Upstream idle timeout`（本轮 T-c89462 尾部被截，工作成果完好落盘）；重跑/resume 即可。

### 部署教训（重要）
麦片 venv 是 pip 装进 site-packages 的副本，`/opt/agent-fabric/fabric/` 源码树是"装饰品"——rsync 源码树不生效。正确姿势：同步到
`/opt/agent-fabric/.venv/lib/python3.10/site-packages/fabric/`。本机无此问题（fabric-node-local 用源码树 cwd 直跑）。

## 三级记忆（V0.6）—— 2026-09-06 第三轮

- 层级：task（任务完成自动入库，零审核）/ project、skill、fact、lesson 等（`memory add <kind> <内容>` 人工即审即入，飞书可直接说）/ experience（节点候选→review 原有）
- 检索：**CJK bigram + 拉丁词混合分词**（store._tokenize）——修复中文无空格整句脱靶问题；确定性零token
- 组包：build_context_package 按层预算截断（task 400/project 800/experience 600/skill 600 字符），resume 链父任务记忆整条确定性带入
- 注入：adapter 渲染 [上一轮任务]/[项目知识]/[历史经验]/[可用技能] 分块
- 测试：17/17（新增 6 项：自动捕获/预算截断/父记忆/三tier渲染/say注入E2E/中文检索）
- 真机：注入 project 运维约定 → 中文 goal → mimo 回答"systemctl status fabric-node，引用[项目知识]" ✅
- 模型稳定性观察：nemotron-3.5-lightning-free 两次挂死(上游流空闲,480s行超时兜底)；mimo-v2.5-free 全天稳定——交互任务建议路由 mapian/mimo

## 混合检索（V0.7）—— 2026-09-06 第四轮

- 方案：BM25×向量 0.6/0.4 融合，仍跑 SQLite（V1 迁 PG+pgvector 同构升级）；零 API 费用
- 向量：fastembed BAAI/bge-small-zh-v1.5（512维本地 ONNX，hf-mirror 拉取，零token）惰性加载 + 启动后台回填旧记忆
- BM25：手写 Lucene 式（idf=ln(1+..) 恒非负）——**rank_bm25 库在微语料上 epsilon 地板把命中打成负分**（实测单文档命中得-0.549），弃用
- 退化链：向量模型不可得→纯BM25；BM25异常→纯向量；两者皆空→不注入（记忆问题不阻塞任务）
- 测试：22/22（新增5项：BM25命中/假向量融合公式/空结果/回填/真模型smoke）
- 真机：零共同bigram改写query"更新完程序服务没生效如何排查"→运维约定0.735分召回第一（bigram版此query返回空）；E2E任务mimo引用[项目知识]准确作答 ✅
- 运维注记：curl 查中文参数须 -G --data-urlencode（裸UTF-8请求行被h11拒绝）；服务首次检索调用含模型加载~10s

## 一体化闭环（V0.8）—— 2026-09-06 第五轮

- 皇冠四块齐：①handoff任务可续 ②三级记忆 ③BM25×向量混合检索 ④经验→技能闭环
- 审核闭环：`candidates` 列待审（18条积压全pending→`review all promote` 18条入experience库）
- 结晶：`memory crystallize <主题>` 检索聚类 experience/task → 确定性拼接草稿 → skill入库（人过目即审）
- 面板：`tasks [N]` / `task <T-id>`（含handoff文件列表）/ `memories` / `skills`
- 检索分改为**检索内归一化**（相对最佳命中0~1）——绝对分在向量模式(0.6×cos)与BM25-only模式分布不可通约，跨模式阈值失效（实测两次翻车）
- 结晶质量结论：聚类OK，拼接草稿是流水汤——确定性无LLM无法提炼；已删除劣质结晶产物防污染；**V1：结晶蒸馏走节点免费模型**（dispatch内部distill任务）
- 测试：28/28（+审核闭环E2E、结晶排除skill自噬、噪声过滤）

## 会话化指挥（V0.9）—— 2026-09-06 第六轮

- 语义：use <麦片|米线|节点id|@节点> 选主机 → 之后纯自然语言即任务；运行中发言=排队意见，任务完成自动 resume 续跑（handoff 恢复工作区+意见）；过程 ⏳ 关键行节流推送（8s窗/6行），结果尾部 1800 字符+工作区文件清单完整推送
- 单会话约束（V1 用户拍板）：同时只跟一个 agent 对话；切换 use 清空排队
- 坑：busy 判定必须用"非终态"（accepted 介于 pending/running 之间漏判会绕过排队）；resolve 节点 id 要直通（别名表不含 node-a 这类 id）
- 测试 30/30（会话流 E2E：use→直通→排队→自动续跑→@切换；ReadFileAdapter 加 2s 延迟造真实 running 窗口）
- 真机：mapian 自然语言建文件✅ 排队意见自动续跑✅（second.txt 追加生效）⏳过程推送✅ use 米线切换✅

## 结果正文提取（V0.9.1）—— 2026-09-06 第七轮

- reply_clean.extract_reply：尾部扫描剥过程行（>build/$cmd/←✱→工具行/Wrote状态行/ANSI），整行匹配才删、不碰字符
- 正文保全：``` fence 内全保留（行首$示例/#注释），货币$5、数字、$PATH 等字符级不删（用户点名要求）
- 提取失败/过短(<8字符)退回原始尾部1200——宁可多给不可丢答案；events 表仍存原始输出（审计）
- _fmt_result 用清洗正文(≤2500)；task 面板"回复:"字段同步
- 测试 34/34（4项新增：基础提取/代码块+符号保全/短文回退/多段正文）

## LESSON 经验回流（V0.9.2）—— 2026-09-06 第八轮

- 改动：goal 模板尾部要求模型自标 `[LESSON] 一句话经验`；节点只回流含 LESSON 的输出（goal+tail 流水终止，2026-09-06 用户 discard 全部流水候选后拍板）；reply_clean 剥除用户回复中的 LESSON 行
- 解析宽松化：`[LESSON]` 行首/行中均可（run 命令空白折叠吃换行；模型可能包 markdown）
- 真机：画正弦波任务 → 模型自总结"matplotlib中文字体 rcParams 设置"→ 候选池 1 条高质量（对比此前 8 条流水全废）
- 测试 38/38（echo 适配器补注册进 cluster conftest——daemon 不内建 echo，mapian 的 echo 来自其配置）

## 记忆质量三件套（V0.10）—— 2026-09-06 第九轮

- 用户拍板：LLM 可用于记忆质量提升，消耗可控（免费模型=零成本）
- ① dream 确定性整理：task 流水限额 50 条；同 kind 向量 cos≥0.93 合并（计数并入）；零命中老 task 降权；启动 60s 后首跑+每 6h；写操作全部 store 锁内（后台线程与在线写并发安全）
- ② 结晶蒸馏：crystallize 素材派 internal 任务到节点免费模型提炼（fabric 吃自己狗粮），失败/超时退确定性拼接；internal 任务不推送结果/进度、不触发 auto_followup
- ③ memory forget <id> 命令补删除闭环；检索命中计数 hits（注入即命中，dream 衰减依据）
- 坑：批量 replace 把 create 的 internal 参数误注入 resume 的任务字典（签名无此参）→ NameError 空响应；FakeTM 签名要跟 tm.create 同步
- scope 列已建未启用（单项目下过滤是伪需求，第二个项目上来再开）
- 测试 43/43
