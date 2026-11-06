# 结构化日志与可观测性

应用使用标准库 `logging`，不新增依赖。`create_app()` 和 CLI `main()` 调用
`configure_logging(settings)`，配置 `oos` 命名空间并屏蔽 httpx/httpcore 的传输细节，重复配置不会增加 handler。
日志写到 stderr；保留 `propagate=True`，pytest 的 caplog 和宿主 root handler 可继续接收。
模块通过 `get_logger(__name__)` 获取 `oos.<module>` logger，通过 `log_event()` 打点。
导入日志模块不会配置 root logger。
`observed()` 使用 `functools.wraps` 包装同步或异步函数，保持业务函数原有缩进；
`fields` 回调接收绑定参数（含默认值），函数内部通过 `enrich()` 补充安全字段。

## 开关

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `OOS_LOG_JSON` | `true` | 每行一个 JSON 对象；`false` 使用级别、logger 前缀和 JSON 字段的文本格式 |
| `OOS_LOG_LEVEL` | `INFO` | `oos` 的日志级别，如 DEBUG、INFO、WARNING、ERROR；未知名称回退 INFO |

当前业务事件均为 INFO；设置 WARNING 会隐藏这些事件，包括 outcome 为 failed 的事件。
`worker.live_unavailable` 记录完整 LIVE 配置校验的固定 `reason`，不输出配置值或异常原文；
表示本轮消费者不能领取 LIVE，FIXTURE 任务仍可处理，不等同于队列为空或没有恢复/核验工作。
root logger 的级别、handler 不受配置影响；httpx/httpcore 在 logger 上丢弃记录并提高级别。
直接 LiveLLMClient 构造也执行该屏蔽，范围及启动异常限制见 [运行边界](DEPLOYMENT_BOUNDARIES.md)。
多个应用共享进程时，最近一次配置的
格式、级别对整个 `oos` 命名空间生效；之前配置的密钥仍保留在脱敏集合中。

## 字段

字段按事件适用性输出；标识不存在、任务尚未创建或元数据读取失败时，字段可能省略或为 null。
Decimal、datetime、UUID 等值安全转换为字符串，金额使用美元十进制字符串，避免浮点精度损失。

| 字段名 | JSON 类型 | 含义 | 出现在哪些事件 |
| --- | --- | --- | --- |
| `ts` | string | ISO8601 UTC 时间，带 `+00:00` | 全部 |
| `level` | string | logging 级别 | 全部 |
| `logger` | string | `oos` 下的模块名 | 全部 |
| `event` | string | 稳定的 snake.dot 事件名 | 全部 |
| `message` | string | 事件名，通用日志则是脱敏后的消息 | 全部 |
| `request_id` | string | 请求标识，与响应头 `x-request-id` 相同 | HTTP 及请求内事件 |
| `correlation_id` | string | 链路标识，与响应头 `x-correlation-id` 相同 | HTTP 及请求内事件 |
| `task_id` | string/null | 任务标识 | 收件、dispatch、模型、预算、批准、提交、outbox、task.finished、lease、recovery、verification.retry |
| `attempt_id` | string/null | 当前或最近一次执行尝试 | dispatch 及其子调用、批准、提交、outbox、lease、recovery、verification.retry |
| `mode` | string | `FIXTURE` / `LIVE`，来自执行配置或客户端类型 | dispatch、模型；预算仅在上下文已有 mode 时输出 |
| `app_mode` | string | 应用启动时 resolve_mode 的配置结果 | 仅 http.request |
| `outcome` | string | started / success / failed / duplicate / rejected / expired / cleared / still_pending / error | 业务事件 |
| `status` | string | 任务状态 | 收件、dispatch.finish、task.finished |
| `error_code` | string/null | 现有 AppError.code；意外错误使用 INTERNAL_ERROR | 失败事件、HTTP |
| `duration_ms` | number | perf_counter 测量的耗时；开始事件为 0；瞬时 task.finished / lease 拒绝事件可省略 | observed 事件、lease.acquired、verification.retry |
| `method` | string | HTTP 方法 | http.request |
| `path` | string | URL 路径，不含 query | http.request |
| `status_code` | integer | HTTP 响应码；未处理异常为 500 | http.request |
| `input_id` | string | 持久化输入事件标识 | inbox.received |
| `duplicate` | boolean | 是否命中投递身份去重 | inbox.received |
| `text_len` | integer | 文本字符数 | inbox.received、source.parsed |
| `text_sha256` | string | UTF-8 文本 SHA256 的前 12 位 | inbox.received、source.parsed |
| `model` | string/null | 模型名，fixture 使用 `fixture` | model.call、预算上下文 |
| `prompt_kind` | string | 响应 schema/kind 名，如 parser.output | model.call、预算上下文 |
| `input_tokens` / `output_tokens` | integer | 输入/输出 token；fixture 为 0 | model.call、预算事件 |
| `reservation_id` | string | 用量预留行标识 | budget.reserve / budget.settle |
| `reserved_usd` | string | 预估输入和最大输出的预留金额 | budget.reserve / budget.settle / budget.refused |
| `actual_usd` | string/null | 结算金额；预留阶段为 null | budget.reserve / budget.settle / budget.refused |
| `cap_usd` | string | 配置预算与硬上限的较小值 | 预算事件 |
| `plan_id` | string | 计划标识 | 批准、提交 |
| `approval_id` | string | 批准标识 | approval.granted、提交 |
| `actor` | string | 现有业务身份，不读取任意请求身份 | 批准、提交 |
| `order_id` | string/null | 订单标识 | dispatch、批准、提交、outbox |
| `base_revision` / `intent_revision` | integer | 计划基于的订单版本、意图版本 | 批准、提交 |
| `revision` | integer | 提交后的订单版本 | commit.applied |
| `replayed` | boolean/null | 是否为幂等重放；recovery 开始或提交结果未知时为 null | commit.applied、recovery.durable_commit |
| `event_id` / `delivery_id` | string | outbox 业务事件 / 投递行标识 | outbox；consume 去重没有 delivery_id |
| `consumer` | string | 消费者名称 | outbox |
| `source_id` | string | 来源标识，不记录文件名 | 上传、解析 |
| `file_size` | integer | 上传文件字节数 | upload.received |
| `file_sha256` | string | 文件 SHA256 前 12 位 | upload.received |
| `span_count` | integer | 解析生成的片段数 | source.parsed |
| `operation_id` | string | 待核验操作标识 | verification.retry |
| `lease_owner` | string | 领取 lease 的 worker 标识 | lease.acquired |
| `reason` | string | deadline / stalled / lease_expired | lease.expired_or_fenced（watchdog） |
| `scanned_tasks` / `recovered_tasks` | integer | 扫描的活动任务数 / 发生恢复转换的任务数 | watchdog.scan |
| `outbox_created` / `pending_retries` | integer | 补建投递数 / 尝试 pending 核验的操作数 | watchdog.scan |

ContextVar 将请求字段传给请求内同步服务，将任务字段传给 dispatch 的模型和预算调用。
服务和 outbox 在已有会话内以 no_autoflush 做尽力而为的元数据读取；dispatch 和提交重放也会
使用独立只读会话补齐字段，不额外调用模型，不创建或修改用量记录。

mode 只记录可靠事实，不从 ModelUsage 是否存在推断：

- dispatch 在调用模型前将 `resolve_mode(configured).mode.value` 写入自身 ContextVar，
  所以路由提前失败、无需模型或预算拒绝时，start/finish 也带实际配置的 mode。
- model.call 的 mode 来自 FixtureLLMClient / LiveLLMClient 自身类型；其子调用继承该上下文。
- BudgetLedger 不写死 LIVE。预算事件继承当前上下文的 mode；直接调用 ledger 且没有
  mode 上下文时省略该字段。
- Task 未持久化 mode，`task_log_fields()` 不返回 mode。批准、提交、outbox 事件省略 mode，
  并隔离调用方可能传入的 mode 上下文。
- HTTP 只在 http.request 上记录进程启动配置 `app_mode`，不将该配置作为 mode 传播给内层事件。

## 事件清单与位置

| 事件 | 入口与触发条件 |
| --- | --- |
| `http.request` | `app/main.py::attach_ids`，成功、已处理错误和未处理异常均记录 |
| `inbox.received` | `app/orchestration/inbox.py::accept_input`，覆盖 POST /inputs、回答和直接调用；事务成功退出后记录，失败记录 outcome=failed |
| `dispatch.start` / `dispatch.finish` | `app/orchestration/dispatch.py::dispatch_text`，开始执行、结束或失败；返回失败结果也记录失败 |
| `model.call` | `app/adapters/fixture_model.py`、`live_model.py::structured_call`，成功、拒绝、传输/解析错误 |
| `budget.reserve` | `app/adapters/budget.py::reserve`，预留事务完成 |
| `budget.refused` | 同上，预算、调用次数、价格或其他预留错误 |
| `budget.settle` | `app/adapters/budget.py::settle`，结算成功或失败 |
| `approval.granted` / `approval.rejected` | `app/services/kernel.py::approve_plan`，包含已有批准重用、版本/哈希等拒绝 |
| `commit.applied` / `commit.failed` | `app/services/kernel.py::commit_plan`，含重放、过期批准、版本冲突、提交后校验异常 |
| `outbox.delivered` / `outbox.failed` | `app/orchestration/outbox.py::publish_pending`，投递和确认；失败保留原有重试/异常行为 |
| `outbox.duplicate_skipped` | `app/orchestration/outbox.py::consume`，已有消费者收据，无重复副作用 |
| `upload.received` | `app/api/sources.py::upload_source`，上传成功或拒绝 |
| `source.parsed` | `app/adapters/parser.py::split_source`，片段解析成功或失败 |
| `task.finished` | `app/db/session.py`，任务新进入 SUCCEEDED / FAILED；flush 收集、commit 后发出，rollback 丢弃，重放不重复计数 |
| `lease.acquired` | `app/orchestration/worker.py::claim`，领取事务提交后发出；空队列、达到并发上限不发出 |
| `lease.expired_or_fenced` | `app/orchestration/state.py::check_fence` 拒绝旧 attempt；`watchdog.py::scan` 发现 deadline、停滞或 lease 到期 |
| `recovery.durable_commit` | `app/orchestration/recovery.py::durable_commit`，started / success / failed，含 replayed；异常原样抛出 |
| `watchdog.scan` | `app/orchestration/watchdog.py::scan`，started 与 success（完成）或 failed；完成时含扫描、恢复、补建 outbox 和 pending 重试数 |
| `verification.retry` | `app/services/kernel.py::retry_pending_verification`，每个实际领取的到期重试一次：cleared / still_pending / error；异常仅记录错误码，继续其它操作 |

## 进程内指标

`GET /metrics` 返回 JSON 对象，13 个 counter 始终存在（无事件时为 0），例如
`{"tasks_enqueued":1,"worker_claims":1,...}`。这是演示用 JSON，**不是 Prometheus 文本格式**。
不访问数据库、不调用模型、不新增依赖；`/health` 响应保持原契约。

`app/metrics.py` 用标准库 Lock 保护累加、快照及 `reset_for_tests()`；pytest autouse
fixture 每个测试前后清零。所有计数经 `log_event()` 的事件入口更新，先于日志过滤和输出，
因此 WARNING 级别、重复格式化、多个 handler 或输出失败不会漏计或重复累加。

| Counter | 累加条件 |
| --- | --- |
| `tasks_enqueued` | inbox.received 成功且 duplicate=false；包括同任务澄清后的再次入队 |
| `tasks_succeeded` / `tasks_failed` | task.finished 的 SUCCEEDED / FAILED，事务提交后累加；其它终态不计入这两项 |
| `approvals_granted` | approval.granted 成功事件，包括重用已有批准 |
| `commits_applied` / `commits_replayed` | commit.applied 成功，按 replayed 分流；recovery 包装事件不会再次累加 |
| `verification_pending_retries` | 每个 verification.retry，无论 cleared / still_pending / error |
| `verification_pending_cleared` | verification.retry 的 outcome=cleared；表示重试后观察到已通过（含并发重试通过） |
| `verification_manual_required` | 自动核验预算耗尽后转人工的事件 |
| `watchdog_scans` | watchdog.scan 的 started；空扫描和最终失败的调用也算一次 |
| `worker_claims` | lease.acquired；只有 worker 成功领取才计数 |
| `model_calls` | 每个 model.call，含 fixture、成功、失败和调用前拒绝 |
| `budget_refused` | 每个 budget.refused，沿用现有预算预留拒绝事件语义 |

计数仅属于**当前进程**，重启归零，不持久化、不跨进程聚合。独立 CLI worker 的计数
不会出现在 API 进程的 `/metrics` 中；在同进程调用 worker 后可直接看到累加值。
这些指标是尽力而为的进程观测，不是可用于结算的持久账本；进程在数据库提交与事件输出
之间退出可能漏计。commit.applied 在回读/ack 返回后发出，后续异常可能已提交业务但没有
成功事件，须结合 operation、task.finished 和 recovery 日志判断。
已提供持久化核验重试上限/退避和转人工日志；操作者显式启动的有界循环另有 `worker_loop.started/cycle/wait/stopped/failed` 和 CLI 边界 `worker_loop.cli_failed` 事件，字段与限制见 [WORKER_LOOP](WORKER_LOOP.md)。没有常驻托管调度器、外部推送告警、OTel、Prometheus exporter 或分布式 tracing。

## 示例

以下为合成数据、假 ID；金额与 token 仅用于示意。

```jsonl
{"ts":"2026-11-03T01:00:00+00:00","level":"INFO","logger":"oos.orchestration.inbox","event":"inbox.received","message":"inbox.received","task_id":"T-example","input_id":"EV-example","duplicate":false,"text_len":8,"text_sha256":"012345abcdef","outcome":"success","duration_ms":2.3}
{"ts":"2026-11-03T01:00:01+00:00","level":"INFO","logger":"oos.adapters.live_model","event":"model.call","message":"model.call","task_id":"T-example","attempt_id":"JA-example","mode":"LIVE","model":"example-model","prompt_kind":"parser.output","input_tokens":1000,"output_tokens":200,"outcome":"success","duration_ms":12.4}
{"ts":"2026-11-03T01:00:02+00:00","level":"INFO","logger":"oos.adapters.budget","event":"budget.settle","message":"budget.settle","task_id":"T-example","mode":"LIVE","reserved_usd":"0.001000","actual_usd":"0.000200","cap_usd":"5.00","input_tokens":1000,"output_tokens":200,"outcome":"success","duration_ms":1.5}
```

## 脱敏和原文保护

- `RedactionFilter` 同时挂在 `oos` logger、应用子 logger 和输出 handler。传播给 root/caplog
  之前直接净化 LogRecord；JSON/text formatter 再做一次保护。
- 递归处理 message、格式化参数、extra 的 dict/list/tuple/set。键名忽略大小写匹配
  api_key、apikey、authorization、token、secret、password、cookie，也匹配 `_` / `-`
  分隔的后缀（如 access_token、llm_api_key）；值改为 `[REDACTED]`。
  `input_tokens`、`output_tokens` 是计数，保留数值。
- 字符串中的密钥匹配 `(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}`，区分大小写，
  `sk-` 后至少 16 个合法字符；`sk-test-FAKE1234567890abcdef`、`sk-proj-` 开头的长密钥
  都会替换。款号 `SK-2025`、短串 `sk-abc` 和普通标识内部的 `sk-` 原样保留。
  `Bearer ...` 中 Bearer 忽略大小写；曾经配置的 llm_api_key 明文始终替换，不受模式长度限制。
- SecretStr 一律替换，不通过日志展开。循环引用输出 `[CIRCULAR]`，无法字符串化的对象
  输出 `[UNPRINTABLE]`。异常正文、traceback、stack_info 不输出，避免带出原文或凭据。
- 收件只使用 `text_digest()`，不记录客户原始消息、source_refs、文件名、文件内容、prompt
  或模型响应。上传只记大小和摘要；source.parsed 的文本摘要基于既有解析片段的拼接，
  不是原始文件全文摘要，片段沿用现有 500 字符截断规则。
- 日志出口异常会被吞掉；业务异常继续按原代码抛出，日志不改变 API 响应或事务结果。

## 已知不足

- 只有进程日志和进程内 JSON 指标，没有跨进程指标聚合、分布式 tracing、日志轮转或持久化收集端。宿主另外配置
  root 输出 handler 时可能同时收到传播日志，需要在宿主统一安排输出。
- lease、recovery、watchdog 和 pending 核验重试已打点；dispatch 客户端构造之前的路由/配置准备错误
  可能没有完整的 start/finish 配对；不存在的计划/批准无法补齐 task_id。
- HTTP duration 截止响应对象创建，不含流式响应全部发送时间。请求中间件记录业务状态码，
  不读取响应正文；普通框架 404 等非 AppError 错误可能没有 error_code。
- live 传输失败或上游缺失有效 usage 时，token 与金额沿用原业务的保守结算值，不能解释为
  上游确认的实际计费。fixture 的 token 是 0，不代表真实模型性能。
- commit.applied 在现有提交、后续校验、ack 流程返回后记录；若后续步骤异常则记录
  commit.failed，订单事务可能已经提交，需要结合既有 operation/recovery 状态判断。
- 摘要不是加密，短文本可能被猜测；不用于身份验证。脱敏按字段和模式匹配，不识别所有
  个人信息或任意编码后的凭据。业务新增日志仍需坚持只记录安全元数据。

### 有限核验重试

`verification.retry` 仅在实际领取预算并开始只读核验时发出；未到期/有效租约/已转人工不计重试。
`verification.manual_required` 在自动预算耗尽转人工时发出（operation_id，无原始异常文本），
并累计进程内 `verification_manual_required` 计数；持久状态以 Operation 和任务视图为准。
`verification.manual` 记录操作者触发的单次只读核验，outcome 为 cleared/still_pending/error。
这些日志不等同于向维护人员发送通知；未配置外部告警。
