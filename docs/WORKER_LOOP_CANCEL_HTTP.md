# 有界循环取消后的后续任务验收

基线为 `0c97fe532f6f1b9e41aab091ca83e1f355189a6d`。本轮使用合成数据、真实本机 HTTP、独立 CLI/worker 进程和本机静态提供方，实际排入“处理时取消的任务 → 正常任务”。没有真实模型、浏览器操作、ERP 或共享部署。

## 实际入口与复现

公开 `python -m app.cli worker-loop` 始终 FIXTURE-only。未改动的 CLI 在有效本机 LIVE 配置下，仍将 LIVE 任务留在 QUEUED，并把另一个 FIXTURE 任务处理到 AWAITING_APPROVAL，提供方请求为0。这是保留的模式边界，不能把下面的 LIVE 测试驱动称为公开 CLI 能力。

为确定性地取消正在处理的 FIXTURE 任务，测试子进程只在第一次 `FixtureLLMClient.structured_call` 前请求本机时序服务并等待放行，再调用原始 fixture 方法。模拟 HTTP 回复不参与意图解析，也不产生模型用量；CLI 主流程、领取、取消和事务检查全部实际执行。这是测试时序注入，没有改产品配置或把 FIXTURE 升级成 LIVE。

另用一个明确 `fixture_only=False` 的测试进程连续调用两次既有 `run_cycle`，运行真实本机静态 JSON 提供方；它是显式 opt-in 的两轮实验，不是新增的长期 LIVE 调度器。

| 执行路径 | 基线实际结果 | 修复后实际结果 |
| --- | --- | --- |
| 公开有界 CLI，第一次 FIXTURE 响应被测试暂停 | 收到 RUNNING 后经 HTTP 取消、排入正常任务并放行；CLI 因 STALE_ATTEMPT 退出1，后排任务仍 QUEUED | 同一进程完成两轮、退出0；取消任务保持 CANCELLED，正常任务进入 AWAITING_APPROVAL，无需人工重启救活队列 |
| 显式允许 LIVE 的两轮测试驱动，本机提供方延迟返回 | 第一轮取消后迟到写回触发 STALE_ATTEMPT，进程退出1，后排任务 QUEUED | 两轮完成并退出0；先返回取消结果，再规划正常任务；两次模拟用量均结算 |
| 未改动的公开 CLI 模式隔离 | LIVE 保持 QUEUED、FIXTURE 待批准，零提供方调用 | 行为不变 |

基线手动重启后，后排正常任务才可继续；修复后重启只用于验证持久状态。两条业务路径均完成 API/worker 重启、原取消输入重放、正常任务人工审批及单独提交，再重启 API 以原提交键取回同一个 operation。各库只产生一个批准、一个 operation；取消任务没有计划或批准被迟到响应恢复。最终正常结果仍为 partial。

FIXTURE 时序实验有一次本机 gate 请求、模型账本为0；LIVE 实验有两次本机提供方请求、两条 SETTLED 模拟用量。取消不表示提供方调用已经中止或不产生用量。数据库完整性及外键检查通过。[前后运行证据](evidence/worker-loop-cancel-20261107.json)保存具体结果与工作区证据位置。

## 修复范围

实际用户影响为 P2：取消一个任务会异常结束有界循环，后排正常任务需手动重启才能处理。现在 `run_once` 仅在以下事实同时成立时，将 STALE_ATTEMPT 归为已完成的取消结果：任务为 CANCELLED、取消标记存在、输入事件仍相同、最新尝试的 fencing token 仍属于本次调用。

此结果不产生新计划/批准，也不修改任务终态；沿用原尝试的投递清理，循环继续 outbox 和后续轮次。不重置次数、不重试被取消任务、不忽略其他执行错误。仍在 RUNNING、尝试已被替换或不是 STALE_ATTEMPT 的错误会继续抛出，保留原 fail-fast 行为。增加固定的 `worker.cancelled` 日志。

没有新增 schema、迁移、配置开关或公开 LIVE 入口。现有数据不回填；回退代码会恢复已取消迟到响应导致循环退出的可用性问题，但取消及旧结果拒绝保护仍须保留。

## 可重复验证与边界

```sh
python -m pytest -q tests/test_worker_loop_cancel_http.py tests/test_cancel_journeys.py tests/test_worker_loop.py
```

基线3条 HTTP/进程用例为2失败/1通过；两个失败均是非零退出导致后排任务没有执行。修复后连同取消、旧循环和三个“不吞掉错误”负对照共67项通过。完整门禁、精确交付提交和 CI 见本轮交付记录。

这是两轮有界运行及重启实验，不是常驻托管、长期负载、真实模型停止/账单、浏览器或生产运维验收。公开 CLI 不消费 LIVE；未来生产 LIVE 调度仍需单独设计与验收。未合并、部署或修改 PR 正文。
