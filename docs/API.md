# 接口契约（Stage 4）

已接通确定性业务内核、持久任务、静态来源上传、任务视图与导出，以及受预算限制的可选 OpenAI Responses 解析。默认 FIXTURE；live 须显式启用并配置价格、密钥、提供方和模型。

结构化业务响应通常带 `request_id`、`correlation_id`、`schema_version`；根路径、来源 spans 列表、HTML 和文件下载使用各自格式。错误体是：

```json
{
  "request_id": "...",
  "correlation_id": "...",
  "schema_version": "1.0",
  "error": {"code": "TASK_NOT_FOUND", "message": "中文说明", "retryable": false, "details": {}}
}
```

形状错误走 400 `VALIDATION_ERROR`。业务错误码由 `AppError` 决定，例如 404 `TASK_NOT_FOUND`。堆栈和密钥不会放进响应。上传拒绝返回 400、`parser_status=rejected` 和中文原因。

## 已挂载

以下路由均由 `app/main.py` 挂载。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 进程状态、FIXTURE/LIVE、预算上限及 live 阻断原因；兼容字段 `stage` 仍为 `"0"` |
| GET | `/` | 服务信息、健康检查和任务演示桩链接 |
| POST | `/dispatch` | 同步调度：文本 → Router → 子 Agent → 预检；默认 fixture，可按配置使用 live |
| POST | `/inputs` | 原子保存输入事件和任务，返回 202 入队回执 |
| GET | `/tasks/{task_id}` | 持久状态、问题、计划、来源、结果和错误码 |
| POST | `/tasks/{task_id}/answers` | 保存新回答事件，恢复等待中的同一任务，返回 202 |
| POST | `/tasks/{task_id}/cancel` | 取消任务；已提交时返回 ALREADY_COMMITTED |
| GET | `/tasks/{task_id}/view` | 数据库任务视图：调度、差量、下游、来源、批准区和结果 |
| GET | `/ui/tasks/{task_id}` | 同一任务视图的 HTML 页面 |
| GET | `/ui`、`/ui/demo` | 仅本机合成数据演示首页 |
| GET / POST | `/ui/demo/submit` | 改单表单；POST 复用 inbox，303 跳转任务页 |
| GET | `/ui/demo/tasks`、`/ui/demo/orders/{order_id}` | 最近任务列表、订单只读页 |
| POST | `/ui/demo/worker-once` | 本机 FIXTURE 扫描恢复、处理一条任务、投递 outbox；返回 mode / recovered_task_ids / result（含 task_id、status，空闲为 null）/ published |
| GET | `/tasks/{task_id}/audit` | 独立、只读、无原文的 JSON 审计摘要，见 [v1 契约与限制](TASK_AUDIT.md) |
| GET | `/tasks/{task_id}/download` | 已有变更清单的 CSV/XLSX 下载，`fmt` 默认为 `csv` |
| POST | `/sources` | multipart `task_id` + `file`，可选 `client_upload_key` 幂等重试；每次一个文件、每任务最多三个，静态切分 TXT/MD/XLSX |
| GET | `/sources/{source_id}/spans` | 必填查询参数 `task_id`，只读取属于该任务的来源 |
| GET | `/orders/{order_id}` | 当前订单版本、状态和行明细 |
| GET | `/stock` | 库存与可用量查询 |
| POST | `/plans` | 订单变更的确定性预检，生成计划或澄清问题 |
| POST | `/plans/{plan_id}/approve` | 校验计划 hash 和意图版本，以服务端演示身份创建限时批准 |
| POST | `/plans/{plan_id}/commit` | 校验批准、版本和库存，原子提交并回读核验；支持幂等重放 |
| GET | `/inspections/downstream` | 只读下游一致性巡检，返回漂移列表 |

`T-STUB` 保留为空结构演示，`approvable=false`；实际任务从数据库读取。页面已接通批准与提交。批准过期时视图仍设置 `approvable=false`、`disabled_reason=EXPIRED`，旧批准不能提交。如果计划、任务、意图和依赖检查仍有效，HTML 页面另提供「重新批准当前计划」按钮；用户必须重新核对后明确批准，再单独提交。已撤销、已消费、已取消或依赖变化的计划不会显示该恢复入口。

同一计划的有效批准重复调用 `/approve` 仍返回原批准；到期后明确再次调用会将旧批准记为 `EXPIRED` 并创建新的 `approval_id` 与有效期，不延长旧批准、不写业务订单。GET 视图不修改批准历史。续批 API 保留原有计划状态、hash 与提交的意图版本检查；库存、订单和下游依赖仍由最终 `/commit` 重新校验，页面检查不替代提交校验。现有 JSON 字段契约不变。

## 尚未挂载的契约

`schemas/api.py` 保留方案中的模型；以下接口当前没有路由。

| 方案中的接口 | 契约 |
|---|---|
| GET `/plans/{id}` | 预检响应契约 |
| GET `/operations/{id}` | `OperationGetResponse` |
| POST `/purchase-proposals` | `PurchaseProposeRequest` / `PurchaseProposeResponse` |

示例 JSON 在 `schemas/examples/`。`PlanChangeRequest` 拒绝多余字段，包括客户端自带的 `approved`。批准身份来自服务端本地演示用户，不接受模型或请求体自批。

## 持久执行与来源

- `POST /inputs` 使用 `InputSubmitRequest`，在一个事务保存 Event、Task、InputPayload 和 InboxDelivery，返回 **202** 的 `InputSubmitResponse`。`client_event_key` 是来源投递 ID / 客户端事件 ID，在 thread 内去重；相同文字的新 key 是新事件。选择订单时，接收事务就递增意图并使旧批准失效，重复投递不递增。
- `python -m app.cli worker-once` 执行恢复扫描、一条任务和 outbox 投递。202 不是业务完成回执；API 入队不自动运行 worker。
- `python -m app.cli worker-loop --interval-seconds 5 --max-iterations 60` 是操作者显式启动的有界 FIXTURE 循环，不自动批准/提交，也不随 API 启动。参数、停止语义和退出码见 [WORKER_LOOP](WORKER_LOOP.md)。
- 回答使用同一输入模型和新 key。默认 fixture 回答使用完整的已登记语句；可选 live 按预算调用模型。worker 在写入结果前检查租约与 fencing。
- `source_ids` 持久保存，仅允许读取当前任务关联的静态 spans。上传本身只保存证据，不触发调度或批准；回答可显式选择上传来源参与核对。
- 取消先完成则 commit 返回 `CANCELLED`；commit 先完成则取消返回 `ALREADY_COMMITTED`，不撤销业务历史。
- 幂等键、plan_hash 和版本冲突规则沿用方案第 7 节；提交响应丢失后必须重用原请求和原 key。

### 静态上传解析资源上限

每文件最多 10,000 个来源片段（TXT/MD 的非空行，或 XLSX 的非空单元格）。
XLSX 每张表的实际行位置不得超过 10,000，列位置不得超过 256；空白间隔也计入坐标限制。
虚报的 worksheet dimension 不作为数据范围，实际 XML 坐标在展开空行/空单元格前检查。
超过上限返回上传拒绝及中文说明，请拆分文件或将数据移至表格左上区域。
现有 5 MB、5 张工作表、总计 5,000 非空行和 100,000 非空单元格限制仍生效。
嵌套行、重复/逆序行列坐标或单元格行归属冲突返回 `UPLOAD_CORRUPT`，避免证据静默覆盖；合法隐式坐标仍受支持。

### XLSX 静态值结构完整性

XLSX 还会检查用于取值的核心 XML 结构：每张表须有一个直属 worksheet 的主命名空间 sheetData，row 须直属其中，c 须直属 row。嵌套或错位单元格、数据边界处的错误命名空间、同一单元格重复 v/is、同一文本容器或单次富文本运行内重复 t、会被单元格类型忽略的非空值，以及值容器中标量元素外的非空白正文或尾随文字，均返回 `UPLOAD_CORRUPT`，避免读取器静默丢弃证据。共享字符串表也检查相同的文本结构，拒绝错误命名空间或未知包装层藏入的取值内容。公式继续按静态上传限制拒绝。

合法隐式坐标、纯文本后接多段富文本、rPr 样式、xml:space、注音 rPh/phoneticPr 和不携带冲突值的空 v/is 占位仍受支持；注音是元数据，不并入业务文本。空白缩进、页眉页脚与合法 extension 元数据文字不会被当作冲突值。行尾 extLst 只能出现一次且不能带单元格坐标，之后不能再有单元格；它仍经过原有坐标/数量检查，因此到达列上限的行附带扩展时可能被保守拒绝。

压缩包资源检查先执行；按内容类型清单选中的共享字符串 XML 在 load_workbook 前检查，所有工作表的结构与坐标在任何 iter_rows 前检查，通过后才读取业务值。结构检查失败不保存部分来源或片段。这是针对静态证据完整性的有界结构检查，不是完整 OOXML/XSD 验证，现有文件、压缩包、行列、单元格和片段上限继续生效。

### 提交后的有限只读核验

业务已提交与核验通过是独立事实；核验 pending 或转人工不允许重新提交订单。
`result.verification` 保持 `pending` / `passed` 兼容语义，新增 `verification_manual_required`、
`verification_retry_attempts`、`verification_next_attempt_at`。任务状态仍可为 `SUCCEEDED`。
首次提交读回一次，自动重试由 watchdog 或幂等重放触发，不启动常驻调度器。
首次自动重试可立即运行；失败后指数退避，默认最多 5 次自动重试；通过后不再核验。

环境配置：`OOS_VERIFICATION_MAX_RETRIES=5`（0–100，0 表示下次到期扫描或重放时直接转人工，不执行自动重试）、
`OOS_VERIFICATION_BASE_DELAY_SECONDS=30`、`OOS_VERIFICATION_MAX_DELAY_SECONDS=3600`
（均 1–86400）、`OOS_VERIFICATION_LEASE_SECONDS=120`（1–3600）、
`OOS_VERIFICATION_BATCH_SIZE=100`（1–1000）。延迟为 base × 2^(已重试次数−1)，受 max 上限约束。
重试预算在读取前持久化；崩溃同样消耗预算，租约过期后恢复，耗尽转人工。
租约限制并发，但超时的只读核验可能仍在运行；成功结果保持有效，旧 token 不改写新计划。

操作者调查并处理后，可运行 `python -m app.cli verify-operation OPERATION_ID` 执行一次只读核验。
仅已转人工且无有效租约的 pending 操作可执行；通过后清 pending，失败仍转人工，不重置自动预算。已通过的 COMMITTED 操作直接返回成功，不发起新的读回。
命令退出码 0=已通过、1=仍未通过、2=不存在或尚不可执行。命令不会修复或重提业务数据，
也没有强制标记通过的功能。用任务视图或结构化日志定位需人工的操作；尚无外部推送通知。
升级前先运行 `python -m app.cli init-db`，历史操作和回执保持不变。
降级迁移只删除核验重试元数据列，保留已提交业务、原始回执及 pending 标记；再次升级会从零次自动重试开始，不能把降级/升级当成恢复或重试预算重置的常规操作。

### 本机浏览器请求边界

所有 HTTP 路由要求单个受信任 Host（`localhost`、`127.0.0.1` 或 `[::1]`，可带有效端口）；否则返回 `UNTRUSTED_HOST`（400）。GET/HEAD/OPTIONS 之外的方法还检查 Origin、Referer 和 fetch metadata，拒绝非同源或冲突证据，返回 `CROSS_ORIGIN_REQUEST`（403）。拒绝发生在读取请求体和路由分派之前，使用统一错误信封。无浏览器来源头的 CLI/API 请求保持兼容，不等于认证成功；不要对外开放。防嵌套响应头及完整例外见 [browser-security](browser-security.md)。

### Answer concurrency extension (compatible with v1.0 clients)

`POST /tasks/{task_id}/answers` accepts `AnswerSubmitRequest`, an answer-only
extension of the unchanged `InputSubmitRequest`. Both new fields are optional:
`expected_intent_revision` (non-negative strict integer) and
`expected_input_event_id` (non-empty string). Missing/null fields preserve legacy
behavior. Existing response contracts, `POST /inputs`, and frozen `TaskView`
remain unchanged; no database migration is required.

For new deliveries, supplied fences are checked inside the same SQLite
`BEGIN IMMEDIATE` transaction as event append and approval invalidation.
A mismatch returns HTTP 409 `STALE_INTENT` with no writes. Exact delivery replays
(thread + client event key) return their original receipt before checking fences,
including after revisions or task status advance. Existing identity semantics
are unchanged: changing content requires a new client event key.

Use both fences for a page-based client. Intent revision alone is insufficient
for unbound clarification tasks and may repeat when an order becomes known.
The input fence is the latest durable `InputPayload.event_id`, not the task's
`last_event_id` (which planning can overwrite). The HTML task page exposes it as
`data-input-event-id`; it is captured before view projection so a racing answer
can only leave a conservatively stale page, never an old view with a newer input
fence. The demo sends both fences and includes both in its delivery-key digest.
Tasks without any durable input payload expose no answer/upload controls in the
HTML UI and direct the operator to start a new task. Legacy API clients can still
omit the input fence; they retain revision/status guards only. No unbound-task revision
semantics were changed. Clients omitting fences retain legacy concurrency limits.

## 上传重试身份（兼容不带 key 的客户端）

`POST /sources` 的 multipart 表单可选 `client_upload_key`。允许 1–128 个 ASCII 字母、数字及 `.`、`_`、`:`、`-`；重复、空白或不合法字段返回 400。作用域为 `task_id`，不同任务可使用相同 key。

- 首次成功上传将回执与 `input_source`、`task_source`、`source_span` 同事务提交。身份指纹包含完整解码文件名参数（保留首尾空白，净化显示名前；等价的 RFC 编码形式不算不同文件名）和原始文件字节，使用带长度边界的 SHA-256；不是仅按显示名或文件内容去重。
- 同一任务、key、文件名和字节重试返回 200，复用原 `source_id` / `parser_status=ok`，不新增文件、来源或 spans，也不占用新的三文件额度。响应 `request_id` / `correlation_id` 与 HTTP trace 头均属于本次请求，`limits` 为当前上限。
- 同 key 搭配不同文件名或字节返回 409 `UPLOAD_KEY_CONFLICT`，不覆盖旧文件。文件名净化后相同也不视为相同请求。使用不同 key，或不传 key，可以有意上传相同内容的独立副本，仍受每任务三个文件限制。
- 重放先只读查询已提交回执；未命中才在写锁外静态解析，再在 `BEGIN IMMEDIATE` 事务内重查身份与任务关联、检查额度并保存。并发首次请求只会提交一份证据；解析不持有 SQLite 写锁。回执通过复合外键关联该任务的来源，查询也再次核对来源归属。
- 上传接口维持原行为：任务须存在，但不要求可回答状态。重放可在任务完成、取消或额度已满后返回旧来源；不会追加输入、重开任务、调度 worker、批准、提交或修改业务。网页仍会先核对任务是否变化。
- 网页用任务 ID、完整文件名和文件字节摘要生成稳定 key，不包含意图版本。选择同名同内容文件，重试、刷新或再次选择都会复用原证据；列表按 `source_id` 避免重复展示。网页不提供“强制上传相同副本”按钮。上传后仍需重新勾选证据，再单独提交回答。

新增 Alembic revision `upload_idempotency`，前序为 `verification_retries`。部署候选时须由操作者显式运行 `python -m app.cli init-db`，启动和请求不会自动迁移。升级仅新增回执表，不改写历史来源；降级仅移除回执表，保留文件、来源归属和 spans，但丢失对应重试身份。再次升级不会重建丢失的身份，因此旧 key 可能再次占用额度。

普通写入/提交失败经只读核对确认无持久来源后，删除本请求成功创建的文件。已有文件发生 `xb` 路径冲突时不会被删除；提交成功后回调/响应失败时保留已提交文件，随后同 key 可重放。清理核对或删除失败时保留文件并记录脱敏 `upload.cleanup_deferred`，不覆盖原异常。

本扩展解决“提交成功但响应丢失”的重试，不提供文件系统与 SQLite 的跨资源事务。提交前进程硬崩溃仍可能留下未关联临时文件，重试会使用新的随机路径而不覆盖孤儿。默认文件目录是进程临时目录，尚无持久存储、垃圾回收或灾难恢复保证；数据库回执存在不代表原文件永远可用；重放不会重建丢失的临时原文件，已存数据库 spans 仍可用于任务证据读取。原 5 MiB、静态解析和恶意文件保护不变。
