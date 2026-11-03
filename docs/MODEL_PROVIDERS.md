# 模型提供方

本次兼容适配器**未做真实联调（仅 mock）**。测试使用 `httpx.MockTransport` 并阻断 socket/DNS；兼容接口的实际可用性、模型理解质量及各服务的 JSON 能力尚未验证。

## 接口与职责

`app/adapters/providers/base.py` 定义小型 `ModelProvider` Protocol：

- `name` 标识账本/UsageReport 的提供方；`validate(settings)` 在发送前检查配置。
- `build_request(request, schema)` 返回 `ProviderRequest`：URL、JSON body、实际 system/user 文本。最后一项包含适配器追加的 schema 指令，供客户端保守估算输入。
- `parse_response(response)` 返回 `ParsedResponse`：有效用量或 `None`、完成标志、结构化对象或 `None`。即使内容不合法，也保留有效用量供结算。
- `validate_payload` 标识兼容 Chat 路径需要本地 schema 校验；Responses 保持既有下游 `ParserOutput` 严格校验行为。

提供方不发送 HTTP、不构造鉴权头、不记账。`LiveLLMClient` 选择提供方，统一执行 token 上限检查、输入估计、`BudgetLedger.reserve`、HTTP 请求、错误映射、密钥回显拒绝和一次 `finally` 结算，返回 `UsageReport(mode="LIVE")`。现有 dispatch 构造同一个客户端，按 `settings.llm_provider` 选择；未知提供方启动时抛出 `LiveStartRefused("PROVIDER_UNSUPPORTED")`。

客户端通过提供方配置校验并构造成功后，模块 logger 发出一条 INFO：`live model provider=<name> host=<hostname>`。只记录提供方和 URL 主机名，不包含路径、query 或密钥。

| 配置值 | 实现 | 接口 |
|---|---|---|
| `openai` | `OpenAIResponsesProvider` | `/responses`；保持 `input`、`text.format` 严格 schema、`reasoning.effort`、`max_output_tokens`、`store=false` 及原 URL 限制 |
| `openai_compatible` | `OpenAICompatibleChatProvider` | `/chat/completions`；`messages=[system,user]`、`max_tokens`、`temperature=0`、`stream=false` |

## 配置示例

以下都是配置模板，模型名与价格必须替换为操作者核实的值。价格占位符不能直接作为配置值运行。保持仓库中的 `.env.example` 默认关闭 LIVE；密钥字段留空。通过运行器/容器的进程环境映射，在运行时注入 `OOS_LLM_API_KEY`，不将密钥写入 `.env`、命令行参数、提示词或仓库。

DeepSeek HTTPS 模板（远端必须注入密钥）：

```text
OOS_LIVE_ENABLED=true
OOS_LLM_PROVIDER=openai_compatible
OOS_LLM_BASE_URL=https://api.deepseek.com/v1
OOS_LLM_MODEL=<DEEPSEEK_MODEL_ID>
OOS_LLM_API_KEY=
OOS_PRICE_IN_PER_MTOK=<INPUT_USD_PER_MILLION_TOKENS>
OOS_PRICE_OUT_PER_MTOK=<OUTPUT_USD_PER_MILLION_TOKENS>
OOS_LLM_JSON_SCHEMA_SUPPORTED=false
```

vLLM loopback 模板（模型名须匹配服务暴露的名字）：

```text
OOS_LIVE_ENABLED=true
OOS_LLM_PROVIDER=openai_compatible
OOS_LLM_BASE_URL=http://127.0.0.1:8000/v1
OOS_LLM_MODEL=<SERVED_MODEL_ID>
OOS_LLM_API_KEY=
OOS_PRICE_IN_PER_MTOK=<EXPLICIT_INPUT_PRICE_OR_ZERO>
OOS_PRICE_OUT_PER_MTOK=<EXPLICIT_OUTPUT_PRICE_OR_ZERO>
OOS_LLM_JSON_SCHEMA_SUPPORTED=false
```

Ollama loopback 模板：

```text
OOS_LIVE_ENABLED=true
OOS_LLM_PROVIDER=openai_compatible
OOS_LLM_BASE_URL=http://localhost:11434/v1
OOS_LLM_MODEL=<OLLAMA_MODEL_ID>
OOS_LLM_API_KEY=
OOS_PRICE_IN_PER_MTOK=<EXPLICIT_INPUT_PRICE_OR_ZERO>
OOS_PRICE_OUT_PER_MTOK=<EXPLICIT_OUTPUT_PRICE_OR_ZERO>
OOS_LLM_JSON_SCHEMA_SUPPORTED=false
```

`openai_compatible` 的 base URL **没有默认值**，模型名必填。`.env.example` 保留原 OpenAI 默认 URL，切换兼容提供方时必须显式替换这一行，不能只修改 provider。缺配置时 `resolve_mode` 保持 FIXTURE；直接构造 LIVE 客户端抛出 `LiveStartRefused`，原因保存在 `reason` / `details.reason`。缺少或空的 base URL 返回 `BASE_URL_NOT_CONFIGURED`，提示「openai_compatible 必须配置 OOS_LLM_BASE_URL」。

## 价格与限额

缺任一输入/输出单价，拒绝启用 LIVE：保持 FIXTURE，原因 `PRICES_NOT_CONFIGURED`。永远不猜价格。本地模型同样必须显式填写两个单价；确认无需计费时可明确设为 `0`，空值不等于零。零价调用仍记录 `ModelUsage` 并占用每任务调用次数。

有效总预算仍为 `min(OOS_LIVE_BUDGET_USD, 5.00 USD)`。HTTP 之前原子预留保守输入估计、最大输出费用与每任务调用名额；JSON object 模式追加的 schema 指令也纳入输入估计。成功响应缺少有效的 `usage.prompt_tokens` / `completion_tokens` 时抛出 `SCHEMA_INVALID`，按预留输入/最大输出悲观结算。非负整数才是有效用量（bool、字符串、负数无效）。

有效用量即使超过预估也照实记录。非 JSON、schema 不匹配、`finish_reason="length"` 等失败同样结算；HTTP 拒绝有用量时照实记录，无有效用量时记零 token/费用，但保留调用名额。进程崩溃保留预留费用。任务卡只有存在实际 `ModelUsage` 行才标 LIVE；配置开启或 fixture 成功不算模型调用。

## JSON 能力与本地校验

`OOS_LLM_JSON_SCHEMA_SUPPORTED=false` 为保守默认：发送 `response_format={"type":"json_object"}`，同时将 schema 与仅返回 JSON 的指令追加到 system prompt。

只有确认服务支持时才设为 `true`：发送 `response_format={"type":"json_schema","json_schema":{"name":...,"schema":...,"strict":true}}`。不会因服务拒绝 schema 自动降级或重新请求。

两种兼容 Chat 模式都读取 `choices[0].message.content` 的 JSON 字符串，并按本次请求的 `response_schema` 本地校验。`finish_reason="stop"` 才算完成，`length` 或其他未完成标志返回 `MODEL_INCOMPLETE`。额外 `approved` 字段、错误类型/枚举、缺字段、Markdown 包裹和非对象输出都返回 `SCHEMA_INVALID`，错误不包含响应内容。

仓库未声明/安装 `jsonschema`，因此使用无新增依赖的严格子集校验器：支持 object/properties/required/additionalProperties、array/items、type（含 null 联合）、enum、const、anyOf 和本地 `$defs`/JSON Pointer `$ref`。整数不会接受 bool 或数字字符串；不解析远端引用，不认识的约束拒绝通过。title/description/default 等注释不参与校验。这覆盖当前 `parser_schema()`；扩展 schema 约束时需同步扩展校验器。之后仍经过既有 Pydantic 抽取模型及业务内核校验。

兼容路径在预算预留和 HTTP 前用 `ensure_supported_schema` 检查完整 schema（含未使用的属性、`$defs`、items、anyOf 和 schema 形式的 additionalProperties），并限制递归深度。不支持的关键字、非本地或无法解析的引用、无效容器形状会提前返回 `SCHEMA_INVALID`，不预留、不计费、不发送请求；响应到达后仍逐节点校验 payload。

兼容 Chat **不发送 `store` 字段**，因为各兼容服务不一定支持；不据此承诺服务端零留存。OpenAI Responses 继续发送 `store=false`。

## 安全边界

- `openai` 仍只接受 `https://api.openai.com/v1`（可带尾部 `/`）。
- 兼容 URL 必须 HTTPS；仅精确 host `localhost`、`127.0.0.1`、`::1` 可用 HTTP，并允许无密钥。IPv6 URL 需用方括号，例如 `http://[::1]:8000/v1`。不做 DNS 查询来认定 loopback；相似域名不享受例外。URL 不允许 userinfo、query、fragment、反斜线或空白/控制字符；不支持的 URL 返回 `BASE_URL_UNSUPPORTED`，不安全协议返回 `BASE_URL_INSECURE`。仅向明确配置且信任的远端发送密钥和合成上下文。
- 仅配置密钥时添加 `Authorization: Bearer ...`；密钥不进入请求 JSON、业务数据或用量账本，回显密钥的模型输出被拒绝。HTTP 错误仅反映安全的状态码及 allowlist 格式的 type/code，密钥子串会被替换为 `REDACTED`。
- HTTP 使用超时、`trust_env=False`、`follow_redirects=False`，不继承环境代理，不跟随跳转，无自动 HTTP 重试。401/403 → `MODEL_AUTH`，429 → `MODEL_RATE_LIMIT`，5xx → `MODEL_UPSTREAM`，其他 → `MODEL_HTTP`，全部 `retryable=False`；超时/连接错误同样不自动重试。既有 parser 的 schema 失败处理可能再次调用，仍受持久调用上限和预算约束。
- 提供方只返回意图。人工批准、库存、版本及 fencing 检查继续由内核执行；黄金输入仅进入 `AWAITING_APPROVAL`，不会自动创建 Approval 或 Operation。

## 对外错误码与 details.reason 对照

`LiveStartRefused`（`app/errors.py`）总是带 `details.reason`。reason 属于 PRICES_NOT_CONFIGURED、KEY_NOT_CONFIGURED、LIVE_DISABLED、BASE_URL_NOT_CONFIGURED 时，对外 `code` 与 reason 相同；其余 reason 对外统一为 `LIVE_START_REFUSED`。以下取值均来自当前代码。HTTP 状态见 `app/errors.py` 与 `app/adapters/live_model.py`。

| 对外 code | details.reason | 抛出位置 |
|---|---|---|
| `PRICES_NOT_CONFIGURED` | `PRICES_NOT_CONFIGURED` | `resolve_mode`（`app/adapters/llm.py`）返回的 live_block_reason，由 `LiveLLMClient.__init__` 抛出 |
| `KEY_NOT_CONFIGURED` | `KEY_NOT_CONFIGURED` | `resolve_mode`（openai 无密钥）；`OpenAICompatibleChatProvider.validate`（远端 URL 无密钥） |
| `LIVE_DISABLED` | `LIVE_DISABLED` | `LiveLLMClient.__init__`：未开启 live 且没有其他 block reason 时的兜底值 |
| `BASE_URL_NOT_CONFIGURED` | `BASE_URL_NOT_CONFIGURED` | `OpenAICompatibleChatProvider.validate`；`app/adapters/providers/urls.py` |
| `LIVE_START_REFUSED` | `PROVIDER_UNSUPPORTED` | `provider_for_settings`（`app/adapters/providers/__init__.py`） |
| `LIVE_START_REFUSED` | `MODEL_NOT_CONFIGURED` | `LiveLLMClient.__init__`；两个 provider 的 `validate` 与 `build_request` |
| `LIVE_START_REFUSED` | `BASE_URL_UNSUPPORTED` | `OpenAIResponsesProvider`（非官方 URL 或未配置 URL）；`urls.py`（URL 无法解析或含 userinfo/query/fragment 等） |
| `LIVE_START_REFUSED` | `BASE_URL_INSECURE` | `urls.py`：协议不是 HTTPS，且不是 loopback 主机上的 HTTP |

以下 code 不带 `details.reason`：

| 对外 code | details | 抛出位置 |
|---|---|---|
| `TOKEN_LIMIT` | 无 | `LiveLLMClient.structured_call`：输出 token 超上限或为负 |
| `SCHEMA_INVALID` | 无 | `SchemaInvalid`：缺有效用量、非结构化对象（`live_model.py`）；不支持的 schema 关键字或输出不合 schema（`app/adapters/providers/schema.py`） |
| `MODEL_INCOMPLETE` | 无 | `live_model.py`：provider 解析为未完成（如兼容 Chat 的 `finish_reason` 不是 `stop`） |
| `MODEL_TIMEOUT` / `MODEL_TRANSPORT` | 无 | `live_model.py` 发送阶段：httpx 超时 / 连接错误 |
| `MODEL_AUTH` / `MODEL_RATE_LIMIT` / `MODEL_UPSTREAM` / `MODEL_HTTP` | `status_code`，以及 allowlist 格式的 `error_type`、`error_code` | `LiveLLMClient._raise_http_error`：401/403、429、5xx、其他 |
| `BUDGET_EXCEEDED` | `spent_usd`、`estimate_usd`、`cap_usd` | `BudgetLedger.reserve`（`app/adapters/budget.py`） |
| `CALL_LIMIT` | `task_id`、`calls` | `BudgetLedger.reserve` |
| `PRICES_NOT_CONFIGURED`（`PricesNotConfigured`） | 无 | `quote_cost`：账本缺单价；与上表的 LiveStartRefused 同 code，但不带 reason |

