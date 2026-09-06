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

## V0.10.1：dream cron 化 + 蒸馏自动判断 —— 2026-09-06 第十轮

- dream 去常驻线程改外部驱动：POST /api/dream + systemd fabric-dream.timer（boot 10min 后首跑、每 6h、Persistent）
- auto_crystallize：promote（REST/say 两路径）后 fire-and-forget 自动判断蒸馏——冷却 1h、素材≥3、无 cos≥0.85 同主题 skill 才触发；触发后 🧢 推送结果（人可见可撤）
- LLM key 问题：零新凭据——蒸馏走节点 opencode 免费模型，central 零 LLM key（能力租借自节点）
- 测试 44/44（素材不足/同主题判重两条路径分别用 BM25-only 与常向量 embedder 验证）

## V0.11：自动入库 + 记忆更正 —— 2026-09-06 第十一轮

- 用户拍板：①自动 promote 开启 ②入库允许更正
- ingest_candidate 新语义：成功任务+非教训类 LESSON（无 失败/踩坑/教训/不要/超时… 词）自动入库 experience（importance=2）+轻推送（带 edit/forget 提示）+触发 auto_crystallize；教训型仍 pending 人审——高价值高风险不 automation
- memory edit <id> <新内容>：更正任意记忆（含结晶技能）；embedding 清空后立即重算，旧向量不残留
- 测试自足化：cluster node-b 工作区预置 demo.txt（followup 测试曾隐式依赖 handoff 测试先行恢复文件——测试顺序耦合）
- 测试 46/46

## V0.12：自我完善机制 —— 2026-09-06 第十二轮

- 用户拍板边界：消耗可控（节点免费模型·internal·零 key）+ 只建议不自动改码（建议→人拍板→执行）
- selfimprove.run_audit：确定性收集（git log/规模/pytest/TODO 扫描/记忆健康统计/TESTING 尾部）→ 组审计 goal → internal 任务到节点 → 报告推送 🔍
- 触发：improve 命令 / fabric-improve.timer（每日，boot 30min 后首跑）
- 配套：TASK_START 信封带 internal → daemon 不回流 LESSON（系统任务的经验不是业务经验；旧节点不识别该标志会照回流——须同步节点代码）
- 首跑实况（2026-09-06 19:30 @mapian mimo）：体检结论=21% 记忆零命中+daemon 两个 V1 遗留；建议=①治理零命中记忆②补 harness 转发/热更新③闭环执行路径；风险=自审计建议质量受免费模型推理上限约束（系统自知）
- 首跑即自我提醒："建议必须闭环执行，否则只收集不落地"（自动入库）
- 测试 46/46

## V0.12.1：汤圆节点上线（双 harness）—— 2026-09-06 第十三轮

- 汤圆 = 199.68.217.229（雨云，Ubuntu 22.04，2C/3.8G，OpenClaw 共存不碰）
- 部署：npm 全局 @deepseek-ai/dsh 0.1.2-rc.1 + @openai/codex 0.153.4；fabric 走 site-packages 直装（mapian 模式——pip 打包被本地 stale build/egg-info 污染，直装绕开）
- 双 harness：DshAdapter（dsh --profile headless 单发；模型=DSH_HOME/settings.yaml agent-default-model→dashscope/deepseek-v4-flash-0731，凭据 .credentials.yaml）+ CodexAdapter（codex exec --skip-git-repo-check；ChatGPT 登录态 auth.json+config.toml 复制，workspace trusted）
- daemon harness 自动发现：which 探测 dsh/codex/opencode，显式传参优先（测试 cluster 不受影响）
- 配置隔离纪律：只带走 settings+credentials+skills；feishu/（小九桥）绝不复制——双桥会冲突；sessions/evolve-workspace（本机会话与记忆）不带
- router KNOWN_HARNESSES 补 codex（此前 codex 命令 fallthrough 到 echo）
- 真机验证：双 probe done；双真任务 done（today.txt/codex.txt 均落盘）；**双 harness LESSON 同库回流**（[dsh]date+%Y-%m-%d、[codex]printf 避免换行）——跨 harness 经验共享成事实
- 测试 46/46

## V0.12.2：会话 model 指令 + 汤圆 codex 默认 luna —— 2026-09-06 第十四轮

- 用户要求：codex 默认 gpt-5.6-luna + harness 切模型指令
- 链路：model <名> → session.model → tm.create(model=) → TASK_START payload.model → daemon RunContext.model → adapter（codex -m / opencode --model / dsh 提示不支持由节点 settings 定）
- model 查询显示当前 harness@node；切换即时生效；use/harness 换时不重置（adapter 不支持自动忽略）
- 坑①：sed 前 grep -q "^model" 被 model_provider 前缀撞车 → 精确 "^model = "
- 坑②：use 分支 harness 校验用 act.node（别名"汤圆"）查 registry 必空 → 先 resolve 再查
- 坑③：蒸馏 internal 任务派 focus 节点但硬编码 opencode——汤圆无 opencode 全失败 → node=None 跨节点自挑（T-15794f@test-node 蒸馏成功验证）
- 真机：model gpt-5.6-luna → codex 任务 luna-ok 落盘 done
- 测试 47/47（新增 model 链路 payload 单测）

## V1.1：State/Memory 分离 + 结晶收紧 + 模型档案 —— 2026-09-06 第十五轮

（合并 GPT 建议与自审报告，三件套一次落地）
- ①auto_crystallize 收紧：素材须来自 ≥2 不同任务（单任务经验不配变 skill；判重先行——已有同主题技能时跨不跨任务都不结）
- ②State/Memory 分离：on_task_result 停写 kind=task 记忆；resume 父记忆直读 tasks 表（pkg.task.parent = goal+尾部400，不再走 memories 复制）；dream ①改为一次性清空 kind=task 残留（首跑清 30 条）③零命中降权对象 task→experience
- ③节点上报 model_profiles（dsh=settings.yaml agent-default-model；codex=config.toml model 行；opencode=AF_OPENCODE_MODEL；pyyaml 入依赖）——central 零 key：只报名不报凭据；model 命令查询显示档案
- 真机：tangyuan 档案 dsh=dashscope/deepseek-v4-flash-0731;codex=gpt-5.6-luna；零命中 21%→17%（task 流水 30 条清出，剩余为真经验/技能）
- 测试 48/48（dream 清空语义、parent 读 tasks 表、跨任务结晶门槛各更新）

## V1.2：Task/Run 分离 —— 2026-09-06 第十六轮

- runs 表（R-xxx）：harness+model 对 task 的一次执行；task.status 由最新 run 派生
- 协议保守演进：TASK_START/CANCEL/RESULT payload 加 run_id（事件类型不动，节点同步升级即可）
- per-run cancel：daemon 持 _current_run，run_id 不匹配的 cancel 视为过期请求忽略（防旧 cancel 误杀新 run）
- retry <T-id> [harness] [model]：终态任务同 task 新 run——"换 codex 再试"一步到位
- task 面板显示 runs 明细
- 真 bug 顺手修：create() 的 task dict 从未带 internal 键（存库恒 0，蒸馏任务静默靠巧合）；daemon RESULT 回包补 run_id（central 据此落 run 终态）
- 真机：retry T-6419cb dsh → 同 task 双 run 轨迹（codex 建 v12b.txt → dsh 复核"一致无需覆盖"——换 harness 复核场景自然涌现）
- 测试 49/49

## V2a：插话注入 + 带因中断 —— 2026-09-06 第十七轮

- 用户拍板：不执着 ACP——只有"危险指令急中断"才需要，且 cancel(kill) 已覆盖；ACP 彻底放下
- 插话：任务运行中说话 → 排队续跑（兜底保留）+ T_TASK_MESSAGE → 节点写 workspace/.fabric/inbox.md（带时间戳）；prompt 预置约定"每完成一步检查 inbox.md，处理完删该行"——利用 agent 自主读文件的本性，零长连接
- 带因中断：cancel <T-id> <原因> → kill 照旧（急中断）+ 原因存 tasks.cancel_reason 列 → resume 时注入"[用户中断原因]…（上次因此被中断，务必调整）"
- 坑：save_task 的 INSERT 列清单是白名单——task dict 多余键静默丢弃（cancel_reason 存不上），加列必须同步改 INSERT/UPDATE；python heredoc 中 assert 失败会丢掉同脚本里已做的 replace（write 在最后）——一个脚本一个原子改动
- 真机：use 麦片 → 长任务跑 → 插话"循环上限改 20" → 回复"已排队+已即时插入" → mapian inbox.md 落盘插话行 ✓
- 测试 50/50
