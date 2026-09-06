# V1.2 Task/Run 分离设计（实施中速记）

## 数据模型
- tasks 表：用户的事（goal/node 绑定/status）——不变
- runs 表（新）：R-xxx，harness+model 对 task 的一次执行
  - id, task_id, harness, model, status, result(JSON), started_at, ended_at
  - task.status = 最新 run 的终态派生

## 协议（保守：复用事件类型，payload 加字段）
- T_TASK_START payload + run_id
- T_TASK_CANCEL payload + run_id（per-run：daemon 只杀匹配的当前执行）
- T_TASK_PROGRESS / T_TASK_RESULT payload + run_id 透传回

## 生命周期
- dispatch(task) → 建 run → TASK_START(run_id)
- on_event RESULT → 落 run 行 + 派生 task 终态
- retry <T-id> [harness] [model] → 同 task 新 run（"换 codex 再试"）
- resume 链暂不动（同 task 续 run 留给 V2 ACP——那里才真正需要 session 连续性）

## 分寸（YAGNI 裁剪）
- 信封 seq/correlation_id：暂缓（outbox 已保序，V2 多 run 并发时再加）
- 同 task 并发多 run：不——daemon concurrency=1，串行 retry

## 步骤
1. store: runs 表 + migration + save_run/list_runs/update_run
2. tasks: dispatch 建 run；RESULT 落 run+派生 task；cancel 带 run_id
3. daemon: cancel 按 run_id 匹配（不匹配=已过期，忽略）；runtime 引用带 run_id
4. app/router: retry 命令 + task 面板显示 runs
5. 测试: run 创建/终态派生/retry 新 run/cancel run_id 不匹配忽略
