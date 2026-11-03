# 结构化日志与可观测性

应用使用标准库 `logging`，不新增依赖。`create_app()` 和 CLI `main()` 调用
`configure_logging(settings)`，只配置 `oos` 命名空间，重复配置不会增加 handler。
日志写到 stderr；保留 `propagate=True`，pytest 的 caplog 和宿主 root handler 可继续接收。
模块通过 `get_logger(__name__)` 获取 `oos.<module>` logger，通过 `log_event()` 打点。
导入日志模块不会配置 root logger。

## 开关

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `OOS_LOG_JSON` | `true` | 每行一个 JSON 对象；`false` 使用级别、logger 前缀和 JSON 字段的文本格式 |
| `OOS_LOG_LEVEL` | `INFO` | `oos` 的日志级别，如 DEBUG、INFO、WARNING、ERROR；未知名称回退 INFO |

当前业务事件均为 INFO；设置 WARNING 会隐藏这些事件，包括 outcome 为 failed 的事件。
其他库和 root logger 的级别、handler 不受配置影响。多个应用共享进程时，最近一次配置的
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
| `task_id` | string/null | 任务标识 | 收件、dispatch、模型、预算、批准、提交、outbox |
| `attempt_id` | string/null | 当前或最近一次执行尝试 | dispatch 及其子调用、批准、提交、outbox |
| `mode` | string | `FIXTURE` / `LIVE` | HTTP、dispatch、模型、预算及可关联任务的事件 |
| `outcome` | string | started / success / failed / duplicate | 业务事件 |
| `status` | string | 任务状态 | 收件、dispatch.finish |
| `error_code` | string/null | 现有 AppError.code；意外错误使用 INTERNAL_ERROR | 失败事件、HTTP |
| `duration_ms` | number | perf_counter 测量的耗时；dispatch.start 为 0 | 业务事件 |
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
| `replayed` | boolean | 是否为幂等重放 | commit.applied |
| `event_id` / `delivery_id` | string | outbox 业务事件 / 投递行标识 | outbox；consume 去重没有 delivery_id |
| `consumer` | string | 消费者名称 | outbox |
| `source_id` | string | 来源标识，不记录文件名 | 上传、解析 |
| `file_size` | integer | 上传文件字节数 | upload.received |
| `file_sha256` | string | 文件 SHA256 前 12 位 | upload.received |
| `span_count` | integer | 解析生成的片段数 | source.parsed |

ContextVar 将请求字段传给请求内同步服务，将任务字段传给 dispatch 的模型和预算调用。
服务和 outbox 在已有会话内以 no_autoflush 做尽力而为的元数据读取；dispatch 和提交重放也会
使用独立只读会话补齐字段。批准、提交和 outbox 的 mode 根据任务是否有 ModelUsage 行推断；
不额外调用模型，不创建或修改用量记录。

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

## 示例

以下为合成数据、假 ID；金额与 token 仅用于示意。

```jsonl
{"ts":"2026-11-03T01:00:00+00:00","level":"INFO","logger":"oos.orchestration.inbox","event":"inbox.received","message":"inbox.received","task_id":"T-example","input_id":"EV-example","mode":"FIXTURE","duplicate":false,"text_len":8,"text_sha256":"012345abcdef","outcome":"success","duration_ms":2.3}
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
- 字符串中的独立 `sk-...` 密钥、`Bearer ...` 及曾经配置的 llm_api_key 明文都会替换。
  `task-example` 中位于普通标识内部的 `sk-` 不被当成密钥。
- SecretStr 一律替换，不通过日志展开。循环引用输出 `[CIRCULAR]`，无法字符串化的对象
  输出 `[UNPRINTABLE]`。异常正文、traceback、stack_info 不输出，避免带出原文或凭据。
- 收件只使用 `text_digest()`，不记录客户原始消息、source_refs、文件名、文件内容、prompt
  或模型响应。上传只记大小和摘要；source.parsed 的文本摘要基于既有解析片段的拼接，
  不是原始文件全文摘要，片段沿用现有 500 字符截断规则。
- 日志出口异常会被吞掉；业务异常继续按原代码抛出，日志不改变 API 响应或事务结果。

## 已知不足

- 只有进程日志，没有指标服务、分布式 tracing、日志轮转或持久化收集端。宿主另外配置
  root 输出 handler 时可能同时收到传播日志，需要在宿主统一安排输出。
- lease 获取、到期和 recovery 尚未单独打点；dispatch 客户端构造之前的路由/配置准备错误
  可能没有完整的 start/finish 配对；不存在的计划/批准无法补齐 task_id。
- HTTP duration 截止响应对象创建，不含流式响应全部发送时间。请求中间件记录业务状态码，
  不读取响应正文；普通框架 404 等非 AppError 错误可能没有 error_code。
- live 传输失败或上游缺失有效 usage 时，token 与金额沿用原业务的保守结算值，不能解释为
  上游确认的实际计费。fixture 的 token 是 0，不代表真实模型性能。
- commit.applied 在现有提交、后续校验、ack 流程返回后记录；若后续步骤异常则记录
  commit.failed，订单事务可能已经提交，需要结合既有 operation/recovery 状态判断。
- 摘要不是加密，短文本可能被猜测；不用于身份验证。脱敏按字段和模式匹配，不识别所有
  个人信息或任意编码后的凭据。业务新增日志仍需坚持只记录安全元数据。
