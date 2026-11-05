# 架构（Stage 0）

本文件记录 Stage 0 已经冻结的边界，以及和两份方案不一致时的决定。方案原文在同目录，冲突时以 `PLAN_REVISION.md` 为准。

## 职责

| 模块 | Stage 0 状态 |
|---|---|
| `schemas/` | Pydantic 契约，`extra=forbid`，`schema_version=1.0` |
| `app/db/` | SQLite 表、WAL、外键、`begin_immediate` |
| `app/domain/` | 可用量、未执行量、流水重建。状态推导未实现 |
| `app/services/seed.py` | 载入黄金演示合成快照 |
| `app/services/` 其余 | 预检、批准、提交、核验、巡检、导出：`TODO(stage-*)` |
| `app/orchestration/` | 注册表与按能力路由。Worker、inbox、outbox、watchdog 未实现 |
| `app/adapters/` | Fixture 客户端、live 守卫、预算账本。文件解析未实现 |
| `app/api/` | `/health`、`/tasks/{id}/view`、`/ui/tasks/{id}` |

> 2026-11-03 注：上表 `app/services/` 中的占位文件（approval、commit、inspection、precheck、verification）已在 #15 删除，业务逻辑在 `app/services/kernel.py`。

业务校验不写在提示词里。模型调用不放进持有写锁的事务里。Stage 0 的 live 客户端在记账检查之后、打开网络之前失败。

## 为何用 SQLite

单机、一个写者、P0 演示。连接打开时设置 `journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout`。普通读取使用 `BEGIN`（DEFERRED），不占 RESERVED 锁。写路径调用 `begin_immediate`，在读依赖之前就取得写锁。实现方式是关掉 sqlite3 自己的隐式 `BEGIN`（`isolation_level=None`），由 SQLAlchemy 的 `begin` 事件按 contextvar 发出 `BEGIN` 或 `BEGIN IMMEDIATE`。

迁到 PostgreSQL 时要重做锁和竞争测试，不能只换连接串。多实例不在本阶段。

## 事件流（目标，尚未接通）

浏览器 → 输入 API → inbox → Router → 子 Agent 提案 → 确定性预检 → 人工批准 → `BEGIN IMMEDIATE` 提交 → outbox。查询不经过子 Agent。

Stage 0 的 Router 已经能按事件类型选出能力。找不到能力时返回 `CAPABILITY_MISSING`，不静默跳过。

## 任务视图

`GET /tasks/{id}/view` 的字段按修订第 5 节一次定好：任务卡、调度轨迹、旧新版对照、下游影响、来源、批准区、结果。`T-STUB` 返回空结构，页面写明这不是「订单已修改」。其他 id 返回 404 `TASK_NOT_FOUND`。

## 与方案的差异

1. **SubAgentRegistration 不落库。** 修订要求启动时从配置加载、运行中只读。注册表是进程内对象，没有自注册 API。
2. **`kind=external` 留在枚举里，但注册即拒绝。** P1 才接外部 Agent。产品名、包名、界面标题都不使用「金蝶 / Kingdee」。
3. **同一能力被多个 Agent 声明时失败**，错误码 `AMBIGUOUS_CAPABILITY`，不挑一个。
4. **`clarify_answer` 没有路由行。** v1.1 的表没有为它指定能力。Router 返回 `disposition=unspecified` 和零步骤，不猜测。`query` 的 disposition 是 `read_service`。
5. **预算双限。** 有效上限 = min(`OOS_LIVE_BUDGET_USD`, 5.00)。代码常量是 `HARD_BUDGET_CEILING_USD`。
6. **估价公式。** `input_tokens × 输入单价 / 1_000_000 + max_output_tokens × 输出单价 / 1_000_000`。已花费 + 预估 **大于** 上限才拒绝，等于上限允许。单价用 `Decimal`，SQLite 里以字符串保存，避免变成 float。
7. **Fixture 不写 `model_usage`。** 因此不计入 live 熔断。
8. **Live HTTP 未实现。** 守卫通过后抛出 `LIVE_TRANSPORT_NOT_READY`（`TODO(stage-4)`）。
9. **建表走 Alembic。** `app/db/schema.py` 的 `init_db` 执行 `upgrade head`。初始修订由模型自动生成。请求处理器不建表。
10. **SO-1002 的客户定为华南制衣（CUST-HN）。** 修订只说蓝色 L 的 10 件预占来自 SO-1002，没有客户名。
11. **A-BLACK-M 初始入库 200。** 当前 on_hand 140，发货流水 −60，只有入库 200 才能重建余额。方案没写入库数量。
12. **C-NAVY-L 的单耗是 FAB-NAVY 每件 2 米。** X2 的核验要求如此。seed 里有主数据，还没有生产单。
13. **颜色和尺码用代码**（`BLACK`、`M`）。中文别名留给阶段 3 的 resolver。面料单位存 `m`，含义是米。
14. **生产单、采购单的状态和数量用 CHECK 绑在一起。** 例如 `CUT` 必须 `cut_qty = planned_qty`，`PLACED` 必须还没到料。
15. **`StockMovement.operation_id` 和审计里的 operation 不做外键。** seed 历史流水不必伪装成一次 Agent Operation。
16. **`ModelUsage.task_id` 可空。** 熔断测试可以在没有任务行时记账。业务调用仍应填写 task_id。
17. **`committed_verification_pending` 放在 `operation` 上。** 业务提交时设为 True，表示尚未回读核验或核验失败；核验通过后设为 False。重试只更新此标记，不修复数据。
18. **未实现的 HTTP 路由不挂出来。** 契约在 `schemas/api.py`。已挂载的只有健康检查和任务视图。
19. **seed 的 `intent_revision` 等于 `current_revision`（1）。** 表示没有尚未提交的新意图。
20. **`Reservation.revision`** 取该预占数量最后一次写入时的库存 revision。
21. **增加了 `warehouse` 表。** 方案要求保留 `warehouse_id`，但实体表没有单独的仓库行。P0 只有 W1。
22. **Inbox 和 Outbox 分成两张表**，字段相同。
23. **调度状态** 定为 `PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`、`TIMEOUT`、`SKIPPED`。库存流水类型定为 `RECEIPT`、`RESERVE`、`SHIP`、`RELEASE`、`ADJUST`。方案没有逐个列出这些词。`operation.status` 和投递状态不加 CHECK，等真正写入它们的阶段再定。
24. **Mapper 不声明 relationship。** SQLAlchemy 不会仅凭 `ForeignKey` 排列插入顺序。seed 按父表、子表分段 flush。阶段 1A 的提交要么补 relationship，要么继续按这个顺序写。
25. **R17、R22、R23 在本阶段有真实测试。** 方案把它们排在 1C 和阶段 4；这次的范围要求注册表、路由失败和预算熔断可用。其余 53 条 B/R 用例以跳过入库，不计入通过。
26. **Stage 1C 在进程内执行三个子 Agent。** `Router.plan("new_order")` 仍只返回 `check.inventory`。调度器额外写一条 doc-check 的 SKIPPED，原因是「无下游单据」，用来满足 R21。内核重新计算库存和下游结论，不采用提案里的数字。parser 只做 fixture 精确匹配。

## 停止条件

Ctrl+C 停止 API 进程。没有模型密钥时不要打开 live。没有单价时代码会拒绝 live，不要在代码里填一个“大概的价格”。

## 下游生产单与补购草稿

下游规划先分配 MO 的生产计划，再按每个 `(production_order_id, material_id)` 汇总所有未取消 PO 的 `ordered_qty`，与变更后 MO 的 `planned_qty × 该 SKU 对同一面料的单耗` 比较。多个 PO 共享一份面料需求，不能把每张 PO 都改成整张 MO 的需求量，也不能用另一张 MO 的采购盈余抵消缺口；巡检和提交后的核验使用同一汇总边界。

Qi 已确认：当同一 MO、同一面料的所有有效 PO 均为 `RECEIVED` 且采购总量不足时，原 PO 以 `UNAFFECTED_RELINK` 链接到新订单修订，数量、状态及内容修订均不重写；缺口单独生成 `CHANGE_DRAFT`，`before=None`。新 PO 沿用原供应商、面料和 MO，`order_line_sku` 指向变更后的服装 SKU，数量仅为缺口，到料量为 0，状态为 `PLACED` / `PENDING_SUPPLIER`，仍等待供应商确认。同面料改 SKU 后单耗提高也可使用此流程。

补单以按 ID 排序的第一张有效已收货 PO 为锚点，ID 为 `{received_po_id}-S{n}`，从 1 起取最小未占用后缀；已取消或未链接的现存 PO ID 也不可复用。计划记录新 ID 尚不存在的依赖，并将单据依赖和补单内容纳入批准哈希；批准后若该 ID 被并发创建，提交返回现有的 `STALE_DEPENDENCY` /「下游单据版本或状态已变化」，不覆盖原行。提交在同一事务建立 `PurchaseOrder(doc_revision=1)`、`DocRevision(rev_no=1)` 和 `DownstreamLink`，无需 schema 迁移。

已有补单参与覆盖量计算，重复规划不会重复补购；相同幂等键提交重放返回原操作，相同总量的新计划只重链。后续减量优先缩减或取消未到料补单，再调整其他未到料或部分到料 PO；任何减量都不能低于累计已到料量。已收货原单始终不改，换面料若涉及已到料部分则保留现有拒绝提示；所有 PO 均未到料时可按新面料重新分配总量。锁定预览逐单展示：未开裁 MO 下的已收货原单锁定、未到料补单可变更。

Qi 已确认：未取消且未开裁（`cut_qty == 0`，由生产单 CHECK 约束对应 `NOT_STARTED`）的 MO 可增大 `planned_qty`，吸收同 SKU 的全部剩余订单需求，并生成 `CHANGE_DRAFT`；部分开裁或全部开裁的 MO 仍不得超过其原 `planned_qty`。未开裁 MO 的改派、缩量和无承接需求时取消规则保持不变。分配后若仍有需求、没有可扩容的同 SKU MO，仍提示「P0 不支持为剩余数量追加生产单」，不新建 MO。因此常见的 `MO planned_qty == 订单数量` 场景中，同 SKU 订单增量会扩容未开裁 MO；若该 MO 同面料的有效 PO 全部已收货，则按扩容后的面料缺口生成补购草稿，已收货原单仍只重链。

## Stage 2 已实现

`inbox.accept_input` 原子保存输入和任务；`state.ALLOWED` 定义允许转换。
`worker.claim` 使用短 BEGIN IMMEDIATE 事务领取任务，最多两条 RUNNING；JobAttempt
每次接管增加 attempt_no 与 fencing_token。fixture 子 Agent 在普通读事务运行，
结果写事务重新检查当前 token、任务状态及租约，旧 attempt 不能写入预检或轨迹。
步骤完成更新 heartbeat/progress；`heartbeat` 可单独续租。默认 lease 60 秒、无进展
120 秒、3 次 attempt、8 步，参数集中在 `worker.Limits`。等待人工时释放租约；
问题及来源保存于计划或输入 payload，回答经新事件恢复同一任务。

`watchdog.scan` 检查 Operation、outbox、有效租约、截止时间及进展，决定对账或
重新排队；不统一判失败。现有 BEGIN IMMEDIATE 事务结束后，通过内核的
`retry_pending_verification` 对到期、未转人工的 COMMITTED 且 pending 操作限批重试回读，包括
任务已经 SUCCEEDED 的操作；scan 返回值仍只包含本次恢复的任务 ID。业务提交和取消共用 SQLite 写序列。回执丢失可由
`recovery.durable_commit` 故障钩子模拟，再以原幂等键读取同一 Operation。
首次提交仍只调用一次 `_post_verify`；`commit_plan` 的幂等重放（包括 durable_commit）
仅按同一持久计划重试本次操作的 pending 核验，已通过、未到期或已转人工的操作跳过。重试不重新提交，不新增审计、
事件或业务修订；失败或异常保留 pending。写标记前在独立写事务中重新读取其值，
若并发核验已通过，则保留 False，避免较慢的失败结果覆盖成功结果。
`app/services/verification.py` 持久化自动重试计数、下次时间、租约和唯一 token。
默认最多 5 次自动重试（不含首次提交的读回），失败指数退避，耗尽后只转人工。
`verify-operation` 只允许对已转人工操作执行一次只读核验，不重置预算、不改业务数据。
提交事务已有 outbox；投递先发送再确认，失败退避重试。消费者 `consume` 在同一
事务执行效果并写唯一 `(event_id, consumer)` receipt，支持重复发送后的去重。
这是 at-least-once，不宣称 exactly-once。

`python -m app.cli worker-once` 执行恢复扫描、一条任务及 outbox 投递，可由本地
调度器重复运行；没有常驻调度器，watchdog 仅在被调用时扫描和重试。回读核验当前
库存流水及下游一致性，不修复数据，也不重建历史快照。API 只负责入库，不在 HTTP 请求中执行 worker。新迁移保存
InputPayload 与 ConsumerReceipt，`init-db` 继续执行 Alembic upgrade head。
