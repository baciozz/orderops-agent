# order-ops-superagent 方案修订补充 v1.1

> 基线：[PLAN_v1.0_dot.md](PLAN_v1.0_dot.md)（dot《服装贸易 Super Agent 实施交接方案》v1.0，两天 P0）。
> 本文只写 Qi 已确认的 6 项修订（2026-11-03），没提到的部分一律沿用 v1.0，包括两天 P0 的 0–5 阶段节奏、事务、幂等、租约和安全边界。两份文档冲突时以本文为准。
> 本文只做设计，不含代码。所有客户、订单、单据和数量都是合成测试数据。

**项目名**：`order-ops-superagent`
**简历一句话**：OrderOps SuperAgent：服装批发订单的多 Agent 调度系统，Router 分派解析、库存、单据核对子 Agent，人工审批后执行并核验。

---

## ① 修订总览

| # | 主题 | v1.0 原方案 | v1.1 修订 |
|---|---|---|---|
| 1 | 跨单据核对（核心差异点） | 只有订单、库存、发货，没有生产单和采购单 | 新增沙箱生产单 ProductionOrder 和采购单 PurchaseOrder，二者引用销售订单的 revision。新增「改单影响核对」：列出仍基于旧版本的单据和已执行到不可改的部分，生成下游变更草案，和订单一起走同一次批准、同一个原子事务；提交后回读核验 |
| 2 | 子 Agent | 订单、库存、采购专家只是模块职责，由单个 Worker 顺序执行 | 确定性业务内核保留不变。新增 SubAgent 契约（注册表、capabilities、Pydantic I/O、工具白名单、超时与预算、版本），Router 按事件类型和能力路由。P0 有解析、库存核对、单据核对 3 个子 Agent，都只能出提案。P1 预留外部 Agent/MCP 插槽。界面展示调度轨迹 |
| 3 | 新建订单 | 放在 P1 | 纳入 P0：可从聊天或 XLSX 新建；客户和 SKU 必须匹配已有主数据；流程为预检 → 批准 → 原子提交，包含预占和缺货采购建议 |
| 4 | 模型预算 | 有预算才接 live，上限未定 | 默认 FixtureAdapter。LiveAdapter 只用于最后的小样本 smoke，硬上限 **5 美元**，配置项和代码级熔断双重限制，记录 token 和费用，默认用便宜模型；提供方等 Qi 指定 |
| 5 | 前端 | 第 3 阶段做完整界面 | P0 先定信息结构和接口字段，一次定好；视觉美化和 2 分钟演示视频放到链路跑通后的独立阶段 |
| 6 | 仓库 | 禁止推公开仓库 | 用 GitHub 私有仓库 `baciozz/order-ops-superagent`。第一条链路跑通、测试和 README 齐备后再公开，公开前清查真实数据和密钥 |

v1.0 的 B01–B18 和 R01–R16 继续有效。本文新增 B19–B32 和 R17–R24，P0 用例从 34 条增加到 56 条。

---

## ② 逐条详细设计

### 修订 1：跨单据核对

**1.1 履约方式**
- OrderLine 新增 `fulfillment_mode`，取值为 `STOCK`（现货）或 `MTO`（按单生产），默认值取自 SKU。
- STOCK 行沿用 v1.0 的库存预占规则。MTO 行**不产生 Reservation，也不影响 StockBalance**：P0 没有成品入库，入库后再预占放到 P1。
- PurchaseOrder 采购的是面料（Material），不是成品 SKU，因此不动成品库存。
- v1.0 的 PurchaseProposal（缺货采购建议）保持不变，它和 PurchaseOrder 是两回事：系统**永不**自动把 Proposal 变成 PurchaseOrder。

**1.2 「未执行量」统一定义**
- STOCK 行：未执行量 = ordered_qty − 已发货。
- MTO 行：未执行量 = ordered_qty − 已裁剪（各生产单 cut_qty 之和）。
- 这样 v1.0 的 `replace_remaining` 和 `cancel_remaining` 不需要新模式，就能用于 MTO 行，只是「剩余」改按未执行量计算。

**1.3 下游单据状态**
- ProductionOrder：`NOT_STARTED`（未开始）、`CUT`（已裁剪，cut_qty = planned_qty）、`PARTIALLY_CUT`、`CANCELLED`。缝制、完工放到 P1。
- PurchaseOrder：`PLACED`（已下采购）、`PARTIALLY_RECEIVED`、`RECEIVED`（已到料）、`CANCELLED`。另有 `change_state`，取值为 `NONE` 或 `PENDING_SUPPLIER`（沙箱变更待供应商确认）。P0 不对外发送任何内容。
- 状态事实只能由 seed 或受控的沙箱事件写入（同 v1.0 的发货事实规则），不暴露给 LLM。

**1.4 版本链接**
- 新增 append-only 表 DownstreamLink，记录「某单据基于哪张订单的哪个 revision、哪一行」，取 link_seq 最大的一条为当前值。
- 每次订单提交时，在同一事务内：内容受影响的单据追加新的单据修订（DocRevision）并重新链接；内容不受影响的未取消单据**只重新链接**，内容不变。
- 因此提交后，所有单据都指向新的 current_revision。

**1.5 改单影响核对（单据核对 Agent + 确定性预检）**

对每张下游单据给出一个结论：

| 结论 | 含义 | 计划中的动作 |
|---|---|---|
| UNAFFECTED_RELINK | 版本落后，但内容不需要改 | 只重新链接 |
| CHANGE_DRAFT | 未执行部分需要改 | 生成单据变更草案（旧值、新值、依据） |
| EXECUTED_LOCKED | 已裁剪或已到料的部分受到影响 | 不允许静默更改；整项进入 NEEDS_CLARIFICATION，或返回 422 |
| CANCEL_DRAFT | 未执行部分被取消 | 取消草案；已下采购的单据标记 PENDING_SUPPLIER |

**1.6 批准与提交**
- 下游草案和预期单据版本（expected_doc_revisions，含状态）都写入 Plan，并纳入 plan_hash 和 dependencies_hash。
- 批准后如果任何一张单据的版本或状态发生变化，提交返回 STALE_DEPENDENCY。
- 订单修订、库存动作、单据修订、重新链接、Approval 消费、Audit 和 Outbox 在**同一个** BEGIN IMMEDIATE 事务里完成，要么全部成功，要么全部回滚。

**1.7 回读核验**
- 事务提交后，由只读核验器检查：
  - (a) 所有未取消单据都链接到 current_revision；
  - (b) 每个 MTO SKU 的未取消生产单 planned_qty 之和 = 订单行 ordered_qty；
  - (c) 每种面料的采购单 ordered_qty 之和 = Σ（对应生产单 planned_qty × 单耗）。
- 核验失败时标记 `committed_verification_pending`（沿用 v1.0），**不重做写入**。
- 另提供只读的「一致性巡检」接口，可以发现被外部改动的漂移，只出报告和修复草案，不自动修复。

**1.8 新增不变量**（接续 v1.0 的 1–5 条）
6. MTO 行 ordered_qty ≥ 该 SKU 已裁剪量；已裁剪部分不能改 SKU，也不能改色。
7. 生产单 cut_qty ≤ planned_qty，修改后 planned_qty 不得低于 cut_qty。
8. 采购单 received_qty ≤ ordered_qty；已到料部分不能改面料、不能减量；只有未到料部分可以出变更草案。
9. 订单提交后，所有未取消的下游单据都链接到 current_revision（核验条件 a）。
10. 覆盖一致：满足核验条件 (b) 和 (c)。
11. MTO 行永不产生 Reservation；PurchaseOrder 不写 StockBalance 或 StockMovement。

**1.9 黄金演示增补（从 seed 单独运行）**

seed 增量：
- SO-1001 r1 原有 `A-BLACK-M 100（STOCK，已发 60，预占 40）`，新增 `C-BLACK-L 300（MTO）`。
- C 款单耗为 2 米/件。面料有 FAB-BLACK 和 FAB-NAVY 两种；SKU `C-NAVY-L` 已在主数据中。
- 合成供应商 SUP-01。

| 单据 | 内容 | 状态 | 自洽校验 |
|---|---|---|---|
| MO-2001 | C-BLACK-L 计划 120，已裁 120 | CUT | 120 + 180 = 300 ✓ |
| MO-2002 | C-BLACK-L 计划 180，已裁 0 | NOT_STARTED | |
| PUR-3001 | FAB-BLACK 240 米（供 MO-2001），已到 240 | RECEIVED | 120 × 2 = 240 ✓ |
| PUR-3002 | FAB-BLACK 360 米（供 MO-2002），已到 0 | PLACED | 180 × 2 = 360 ✓ |

库存 seed 不变：A-BLACK-M 140/40/100，A-WHITE-M 120/0/120，B-BLUE-L 30/10/20（on_hand/reserved/available）。v1.0 没写 B-BLUE-L 的 10 件预占来自哪里，本文假定来自 seed 订单 SO-1002。

- **X1 输入**「C 款黑色 L 全部改藏青」
  - 预检结论：MO-2001 和 PUR-3001 为 EXECUTED_LOCKED；MO-2002 和 PUR-3002 可以改。
  - 进入 NEEDS_CLARIFICATION，提问：「已裁剪的 120 件黑色不能改色。① 只把未开裁的 180 件改成藏青（黑 120 + 藏青 180，共 300）；② 其他处理（例如追加生产藏青）P0 不支持，请线下决定。」
  - 此时没有任何业务写入。
- **X2 回答**「①」，对应 `replace_remaining C-BLACK-L → C-NAVY-L qty=180`。

  | 项 | 修订前 | 修订后 |
  |---|---|---|
  | SO-1001 | r1：A-BLACK-M 100；C-BLACK-L 300 | r2：A-BLACK-M 100；C-BLACK-L 120；C-NAVY-L 180（总 400 件，与 r1 相同） |
  | MO-2001 | 黑 L 120，CUT | 内容不变，重新链接到 r2 |
  | MO-2002 | 黑 L 180，NOT_STARTED | DocRev 2：C-NAVY-L 180，NOT_STARTED，链接到 r2 |
  | PUR-3001 | FAB-BLACK 240，RECEIVED | 内容不变，重新链接到 r2 |
  | PUR-3002 | FAB-BLACK 360，PLACED | DocRev 2：FAB-NAVY 360，PLACED + PENDING_SUPPLIER，链接到 r2 |
  | 成品库存 | A-BLACK-M 140/40/100 | 不变（MTO 不预占） |

  回读核验：黑 L 120 = MO-2001 120；藏青 L 180 = MO-2002 180；FAB-BLACK 240 = 120 × 2；FAB-NAVY 360 = 180 × 2。全部通过，界面显示「已提交并核验」。
- **X3 竞态演示**：在 X2 批准之后、提交之前，用沙箱事件把 MO-2002 改为已裁剪 50 件。提交返回 STALE_DEPENDENCY，需要重新预检；这时只剩 130 件可改，必须重新批准。
- **和 v1.0 主流程的衔接**：主流程的 B01 提交（A 款黑换白）同样会把 4 张单据重新链接到新 revision，内容不变。如果 X 演示接在主流程后面运行，revision 号依次顺延，数量结论不变。
- **对 v1.0 文案的唯一影响**：v1.0 中「总计 100/120/80」都指 **A 款**合计。加入 C 款后整单合计另加 300，B01–B06 的断言按 A 款理解。「再加 20」的澄清候选中会多出 C 款，这一点 v1.0 原文的「或加到其他款」已经覆盖。「取消订单未发部分」对 C 款的处理：取消 MO-2002；为 PUR-3002 出取消草案并标记 PENDING_SUPPLIER；C 款数量降为 120（已裁剪的部分保留）。

### 修订 2：统一子 Agent 接口

**2.1 原则**
- 事务、库存、版本、批准、提交全部由确定性内核负责。子 Agent 只读取数据、产出**提案**，不能写业务表，也不能调用 approve 或 commit。
- 预检会重新读取数据库，**不采信**子 Agent 声称的库存、状态或版本。

**2.2 SubAgent 契约**（schemas/ 中的 Pydantic 定义，带 schema_version）

| 字段 | 说明 |
|---|---|
| agent_id、version | 例如 `parser@0.1.0`；注册表内 agent_id + 主版本唯一 |
| kind | `deterministic`、`model` 或 `external`（P1） |
| capabilities | 能力 ID 列表，例如 `parse.text`、`parse.chat`、`parse.md`、`parse.xlsx`、`check.inventory`、`check.downstream` |
| handles | 可以处理的业务事件类型 |
| input_schema / output_schema | Pydantic 模型 ID + schema_version；拒绝额外字段 |
| tools | 只读工具白名单，例如 `order.get`、`inventory.check`、`downstream.get`、`source.read` |
| scope | 只能读本任务的 order_id、source_id 和相关 SKU；在查询层强制 |
| timeout_s、max_steps、max_retries | 默认 30 s、8 步、2 次（P0 可配置） |
| budget | `uses_model`、每次调用的 max_tokens、每个任务的最大调用数（model 类必填） |

**2.3 统一输出信封 AgentProposal**

字段：agent_id、agent_version、task_id、intent_revision、fencing_token、input_hash、payload、source_refs、issues、usage（tokens、cost、mode）。
- 输出必须通过 schema 校验，同时 fencing_token 和 intent_revision 都匹配，才会被接收，否则按 v1.0 的晚到结果规则丢弃。

**2.4 注册表与 Router**
- P0 的注册表在启动时从配置加载，运行中不允许 Agent 自注册或改权限。重复注册或 schema 缺失时启动失败。
- Router 按「事件类型 → 所需能力」路由，不直接写 Agent 名：

| 业务事件 | 所需能力（依次执行） |
|---|---|
| 任意非结构化输入 | `parse.*`，产出 BusinessEvent，类型为 new_order、change_order、cancel_remaining、clarify_answer 或 query |
| new_order | `check.inventory` |
| change_order / cancel_remaining | `check.inventory`；订单有下游单据时再加 `check.downstream` |
| query | 不经过子 Agent，直接走只读服务 |

- 找不到能提供所需能力的 Agent 时，任务以 CAPABILITY_MISSING 失败，并明确显示，**不能**静默跳过。

**2.5 P0 的三个子 Agent**

| 子 Agent | kind | 输入 | 输出 |
|---|---|---|---|
| 解析 Agent `parser` | model（默认 Fixture） | 已切分的 SourceSpan（聊天、TXT、MD、XLSX） | BusinessEvent + v1.0 第 9 节的抽取结果（含 source_refs、questions） |
| 库存核对 Agent `inventory-check` | deterministic | 候选订单行 | 每个 SKU 的预占差量、缺口、PurchaseProposal 建议 |
| 单据核对 Agent `doc-check` | deterministic | 订单旧行、新行 + 下游单据快照 | 每张单据的结论（1.5 节表）和变更草案 |

文件切分（Parser 把文件变成 Span）仍是确定性的 adapters 层，不算 Agent。

**2.6 调度轨迹**
- 每次调用写一条 DispatchStep，记录序号、agent、版本、能力、状态、耗时、tokens、费用、错误码和原因。
- 被跳过的 Agent 也要记录原因，例如「无下游单据」。
- 界面在任务卡上按时间线展示，例如：`parser@0.1.0 ✓ 1.2s → inventory-check ✓ → doc-check ✓ 4 张单据`。

**2.7 外部 Agent / MCP 插槽（P1）**
- ExternalAgentAdapter 实现同一套契约，kind 为 external，输出一律视为不可信数据。
- 第一个计划接入的是只读的「兼容金蝶 WebAPI 格式的模拟服务，非官方」。
- 仓库名、包名、界面和文档标题都**不得**用「金蝶/Kingdee」作产品名，只能在说明文字里写上面这句。

### 修订 3：P0 纳入新建订单

- **事件**：`new_order`，用 BusinessEvent 表达。还没有 order_id 时，intent_revision 挂在 chain 上；Plan 的 base_revision 为 0，提交时由服务端生成 SO 编号。
- **输入**：聊天/文本，或 XLSX「订单」表（列：客户、款、颜色、尺码、数量），每个字段都能定位到单元格。P0 只支持 **STOCK 行**新建；MTO 新建和自动生成生产单放到 P1。
- **主数据**：客户和 SKU 必须唯一匹配已有主数据。缺失、同名、未知颜色或尺码时，先进入 NEEDS_CLARIFICATION，绝不自动创建。数量必须是正整数，单位按「件」处理。
- **预检**：逐行计算预占；任一行缺货，整单原子拒绝（沿用 v1.0 的 P0 规则），并生成 PurchaseProposal。
- **疑似重复**：同一客户、相同行内容、24 小时内的已提交订单，在计划中标出「疑似重复」，需要批准人逐项确认。和 client_event_key 去重是两层不同的机制。
- **提交**：SalesOrder、OrderRevision r1、OrderLine、Reservation、StockMovement、Approval 消费、Audit 和 Outbox 在同一事务内写入；幂等规则同 v1.0。

**黄金演示增补（从 seed 单独运行）**
- **N1**：上传 XLSX，内容为「华东服饰 A-WHITE-M 30；B-BLUE-L 25」。B-BLUE-L 可用 20，缺 5，整单拒绝，生成 PurchaseProposal（B-BLUE-L 5）。不创建订单，也不预占。
- **N2**：聊天输入「蓝 L 改成 20」，产生新的 intent_revision 和新计划，批准后提交。
  - 新订单 r1：A-WHITE-M 30 + B-BLUE-L 20。
  - A-WHITE-M：120/30/90。
  - B-BLUE-L：30/30/0，reserved = on_hand，满足不变量。
- 如果 N 演示接在主流程 B01 后运行，A-WHITE-M 从 120/40/80 变为 120/70/50。

### 修订 4：模型预算

| 项 | 设计 |
|---|---|
| 默认模式 | FixtureAdapter；界面和报告始终标出 FIXTURE 或 LIVE |
| 开关 | `OOS_LIVE_ENABLED=false`；只在第 4 阶段的 live smoke 中手动打开 |
| 硬上限 | `OOS_LIVE_BUDGET_USD=5.00`（配置）+ 代码级熔断：每次调用前按（输入 token + max_tokens）× 单价预估，已花费 + 预估 > 上限时拒绝，返回 429 BUDGET_EXCEEDED；调用后按 API 返回的 usage 记账 |
| 账本 | ModelUsage 表持久化，重启后不清零；每次记录 provider、model、tokens、cost、价格版本 |
| 单价 | 由 Qi 在配置中填写（`OOS_PRICE_IN_PER_MTOK`、`OOS_PRICE_OUT_PER_MTOK`）；未配置时 live 拒绝启动，**不猜价格** |
| 用量控制 | 默认便宜模型；每次 max_tokens 1000，每个任务最多 3 次调用；smoke 样本约 20 条 |
| 密钥 | 只从环境变量 `OOS_LLM_API_KEY` 读取；`.env.example` 只列键名；不得进入日志或提示词 |
| 背景 | Qi 在 11 月 11 日前只剩约 30 美元额度，5 美元是 P0 的总上限，不是每天的上限 |

### 修订 5：前端策略

P0 一次定好任务视图接口（`GET /tasks/{id}/view`）的字段，页面朴素可用即可。

| 区块 | 必备字段 |
|---|---|
| 任务卡 | task_id、order_id、状态、intent_revision、等待对象（人、子 Agent、无）、FIXTURE/LIVE 标识 |
| 调度轨迹 | DispatchStep 列表（2.6 节） |
| 旧新版对照 | 每个 SKU 的旧量、新量、差量、已发货或已裁剪（锁定）、未执行量、预占变化、可用量 |
| 下游影响清单 | 单据号、类型、当前链接 revision、状态、结论（1.5 节）、变更前后值、是否可执行及原因 |
| 来源定位 | 每个字段的 source_ref（行号或单元格），可展开原文 |
| 批准区 | plan_hash 摘要、生成时间、到期时间、`approvable`（bool）+ `disabled_reason`（STALE、EXPIRED、CONSUMED）；过期或失效时按钮置灰，历史保留 |
| 结果 | operation_id、新 revision、核验结果（通过或 pending）、下载链接 |

动画、配色、排版和 2 分钟演示视频放到第一条链路跑通后的**独立阶段**（不在两天 P0 内）。

### 修订 6：仓库

- 用私有仓库 `baciozz/order-ops-superagent`。需要 Qi 先建一个空仓库（不勾选 README），之后再推送骨架。
- **公开条件**：第一条链路「改单 → 核对 → 审批 → 执行 → 核验」能演示；B 类和 R 类核心用例有真实测试记录；README、ARCHITECTURE 和 KNOWN_LIMITATIONS 齐全；没做完的功能标「规划中」。
- **公开前清查**：用密钥扫描工具（例如 gitleaks）扫描全部 git 历史；确认没有真实客户或公司数据、私有路径、.env 文件；确认商标措辞符合 2.7 节；确定许可证。
- 文档新增 `docs/SUBAGENTS.md`（契约和注册表）。`ARCHITECTURE.md` 新增跨单据核对和调度轨迹两节，其余交付文档同 v1.0。

---

## ③ 实体表增量（在 v1.0 实体表基础上）

| 实体 | 关键字段 | 约束 |
|---|---|---|
| OrderLine（改） | + fulfillment_mode | STOCK 或 MTO；MTO 不预占 |
| SKU（改） | + default_fulfillment_mode | 合成主数据 |
| Supplier | id, code, name | 仅合成数据 |
| Material | id, code, color, unit | 单位为米，整数 |
| MaterialUsage | sku_id, material_id, qty_per_piece | P0 中 C 款 = 2；整数 |
| ProductionOrder | id, sku_id, planned_qty, cut_qty, status, doc_revision | 不变量 6、7；doc_revision 只增不减 |
| PurchaseOrder | id, supplier_id, material_id, production_order_id, ordered_qty, received_qty, status, change_state, doc_revision | 不变量 8；没有对外发送工具 |
| DocRevision | doc_type, doc_id, rev_no, snapshot_json, operation_id | append-only；(doc_type, doc_id, rev_no) 唯一 |
| DownstreamLink | doc_type, doc_id, order_id, order_revision, order_line_sku, link_seq, operation_id | append-only；取 link_seq 最大者为当前链接 |
| DownstreamChangeDraft | plan_id, doc_type, doc_id, base_doc_revision, verdict, before, after, reason | 结论取 1.5 节四类；纳入 plan_hash |
| Plan（改） | + plan_kind（create/change/cancel）、expected_doc_revisions、downstream_changes、chain_intent_revision | 新建订单时 base_revision = 0 |
| SubAgentRegistration | agent_id, version, kind, capabilities, input/output schema, tools, scope, timeout_s, max_steps, budget | 启动时加载，运行中只读 |
| DispatchStep | task_id, step_no, agent_id, agent_version, capability, input_ref, output_ref, status, started_at, ended_at, tokens, cost_usd, error_code, skip_reason | 每次调用或跳过记一条 |
| ModelUsage | id, task_id, agent_id, provider, model, input_tokens, output_tokens, cost_usd（Decimal）, price_version, at | 持久化账本，供熔断使用 |

---

## ④ 新增验收用例（接续 v1.0）

| 编号 | 场景 | 预期结果 |
|---|---|---|
| B19 | X1：C 款黑 L 全部改藏青 | 列出 MO-2001/PUR-3001 为 EXECUTED_LOCKED、MO-2002/PUR-3002 可改；NEEDS_CLARIFICATION；没有任何业务写入 |
| B20 | X2：选 ① 后批准提交 | SO r2 = A-BLACK-M 100 + C-BLACK-L 120 + C-NAVY-L 180；MO-2002 DocRev 2 为藏青 180；PUR-3002 DocRev 2 为 FAB-NAVY 360 + PENDING_SUPPLIER；MO-2001/PUR-3001 内容不变；4 张单据都链接 r2；成品库存不变；一个 Operation |
| B21 | 提交后回读核验 | 核验条件 (a)(b)(c) 全部通过；注入核验读失败时标记 committed_verification_pending，不重写 |
| B22 | 明确要求把已裁的 120 也改藏青 | 422 EXECUTED_PORTION_IMMUTABLE；没有写入 |
| B23 | X3：批准后 MO-2002 被裁剪 50 | STALE_DEPENDENCY；旧批准作废；重新预检后可改 130 |
| B24 | C 款总量改为 100（已裁 120） | 422；违反不变量 6 |
| B25 | v1.0 B01 提交时有下游单据 | 4 张单据为 UNAFFECTED_RELINK，重新链接到 r2，内容和 DocRev 不变 |
| B26 | 一致性巡检：单据被外部改成链接旧版且内容不符 | 只读报告列出漂移单据和修复草案；没有自动写入 |
| B27 | X2 提交事务中途注入异常（写完 MO-2002、PUR-3002 之前） | 订单修订、单据修订、链接、批准、outbox 全部回滚 |
| B28 | 聊天新建「华东服饰 A 款白 M 30」 | 批准后新 SO r1；A-WHITE-M 120/30/90；1 条 StockMovement |
| B29 | N1：XLSX 新建，蓝 L 25 缺 5 | 整单拒绝；没有订单、没有预占；PurchaseProposal B-BLUE-L 5；来源定位到单元格 |
| B30 | 新建时出现未知 SKU 或同名客户 | NEEDS_CLARIFICATION 并列出候选；不创建主数据 |
| B31 | 新建：同 key 重复提交 / 两次独立的相同输入 | 同 key 只产生一张订单、返回同一回执；两次独立输入的第二次标「疑似重复」，需逐项确认 |
| B32 | 新建计划批准后库存 revision 变化 | STALE_DEPENDENCY；重新批准 |
| R17 | 注册表重复或 schema 缺失；路由所需能力不存在 | 前者启动失败；后者任务以 CAPABILITY_MISSING 失败并显示，不静默跳过 |
| R18 | 子 Agent 调用白名单外的工具（commit、approve、写入） | 拒绝并审计；DispatchStep 为 FAILED；没有业务写入 |
| R19 | 子 Agent 输出含额外字段或不合 schema | 拒绝，按上限重试后可解释地失败；不进入预检 |
| R20 | 子 Agent 超时后结果晚到 | DispatchStep 为 TIMEOUT；晚到结果因 fencing 不匹配被丢弃 |
| R21 | 调度轨迹 | 改单任务轨迹为 parser → inventory-check → doc-check；新建任务的 doc-check 记为跳过并写明原因 |
| R22 | 预算熔断（上限设为 0.01 美元） | 调用前拒绝，返回 429 BUDGET_EXCEEDED；重启后账本累计值仍在 |
| R23 | 单价或密钥未配置时打开 live | live 拒绝启动，保持 FIXTURE；界面和报告都标明 |
| R24 | 任务视图字段契约 | 第 ⑤ 节字段齐全（schema 快照测试）；过期时 approvable=false，disabled_reason=EXPIRED |

---

## ⑤ 更新后的两天阶段安排

总时长仍为 48 小时。新增工作约增加四成工作量，第 4 阶段 live 最有可能被跳过。

| 阶段 | 时间窗 | 交付与完成标准（加粗为 v1.1 新增） |
|---|---|---|
| 0 范围与契约 | 0–2 h | v1.0 内容 + **冻结 SubAgent 契约、下游单据和链接 schema、新建订单 schema、预算配置键、任务视图字段** |
| 1A 业务内核 | 2–10 h | v1.0 内容（B01–B18）+ **STOCK/MTO 行、新建订单（B28–B32）** |
| 1B 跨单据核对 | 10–14 h | **生产单、采购单、链接、影响核对、回读核验、巡检（B19–B27）**；不接 LLM 也能跑 |
| 1C 子 Agent 接口 | 14–18 h | **注册表、能力路由、3 个子 Agent（parser 用 Fixture）、DispatchStep（R17–R21）** |
| 2 持久执行 | 18–26 h | v1.0 内容（R01–R09） |
| 3 理解与界面 | 26–36 h | v1.0 内容 + **调度轨迹、下游影响清单、置灰规则（R24）**；黄金链 + X、N 演示可以跑通 |
| 4 可选 live | 36–40 h | **5 美元熔断（R22、R23）**；有提供方、单价和密钥时才跑约 20 条 smoke，否则跳过 |
| 5 冻结与交接 | 40–48 h | v1.0 内容；TEST_REPORT 分表列出 56 条用例的通过、失败、跳过和未执行原因 |
| （P0 之后）视觉阶段 | 独立安排 | 美化、2 分钟演示视频，然后按修订 6 的条件公开仓库 |

**时间不足时的冻结优先级**（从高到低，前面的必须保住）：
1. 业务内核 + 跨单据核对（1A、1B）
2. 子 Agent 接口（1C）
3. 持久执行（2）
4. 界面（3）
5. live（4）

前一级不稳定时不做后一级，没做完的写进 KNOWN_LIMITATIONS。v1.0 的「不得删失败测试、不得伪造通过率」规则不变。

---

## ⑥ 仍待 Qi 决定

1. **LLM 提供方和便宜模型**，以及该模型的单价（填入配置）。不定的话第 4 阶段直接跳过。
2. **两天的起止时间和时区**（v1.0 要求开工前确认）。
3. **GitHub 空私有仓库**：Qi 需要在 baciozz 账号下新建 `order-ops-superagent`，不勾选 README。
4. **公开时用的许可证**（例如 MIT 或 Apache-2.0）。
5. **P1 范围**：「追加生产藏青」（X1 的选项 ②）、MTO 新建订单、外部模拟 ERP 适配器，哪些先做。
6. **合成场景是否贴近真实**：C 款按单生产、单耗 2 米/件、裁剪后不可改色，这些设定要不要先找做服装贸易的朋友核对。
