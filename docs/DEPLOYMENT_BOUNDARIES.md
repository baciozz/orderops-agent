# 受控单机演示的运行与隐私边界

该项目仍没有用户认证、会话授权或角色隔离，只适合操作者控制的单机合成数据演示。
仅绑定 loopback；不要开放到局域网、公网、隧道或共享代理。Host/Origin 检查用于浏览器请求边界，
**Host 不是认证**：能够连接服务的 CLI 可构造允许的 Host 并且不携带浏览器来源头。
本轮没有实现多人部署，没有连接真实模型、ERP 或客户数据。

## 任务运行模式持久化

原行为在 FIXTURE API 接收普通输入后，仅按消费者当前配置选择模型。
合成复现中，另一 LIVE worker 把该任务交给 MockTransport；`worker-once` 的 fixture 帮助文案也与行为不符。
现在任务创建事务同时写入 `Task.runtime_mode`，只允许 FIXTURE/LIVE，默认 FIXTURE。
标准调度路径保留该字段；数据库触发器仅拒绝通过 UPDATE 改变模式，不是数据库全面不可变保证。

| 创建入口 | 新任务模式 |
| --- | --- |
| 普通 `/inputs`、同步 `/dispatch` | 接收端 Settings 的 `resolve_mode` 结果 |
| `/ui/demo/*` 表单、固定表格激活指令 | FIXTURE，即使 API 进程启用了 LIVE |
| 内部 `accept_input` 调用省略模式 | FIXTURE；明确的 LIVE 调用必须显式传入 |
| 升级前已有任务 | 一律 FIXTURE，不从历史 ModelUsage 推断再次发送许可 |

重复输入键/同步键继续返回原任务，不改变模式。回答、租约恢复、重试保留原任务模式。
HTTP 输入模型拒绝客户端传入 `runtime_mode`；`GET /tasks/{id}` 提供该持久字段供检查。
任务卡仍保留历史用量为 LIVE 的显示，因此迁移后的老任务可能有 LIVE 历史卡片而 `runtime_mode=FIXTURE`；
历史事实不等于未来执行许可。已批准的计划与业务提交边界不因模式迁移自动重置。

| 持久任务模式 | FIXTURE 消费者 | 有效 LIVE 配置消费者 |
| --- | --- | --- |
| FIXTURE | fixture 解析 | 仍使用 fixture，零模型传输/预算预留 |
| LIVE | 不领取，保持 QUEUED | 允许现有受预算限制的模型解析 |

`worker-once`、`worker-loop` 和网页演示 worker 都是 FIXTURE 消费者。
内部 `run_once(settings=...)` 依照传入配置判断是否可领取 LIVE；`run_cycle` 默认 fixture-only，
内部调用必须显式 `fixture_only=False` 才能消费 LIVE。API 启动不会自行运行 worker。
领取 LIVE 前另行执行 `validated_live_provider`：与模型客户端共用开关、单价、密钥、provider、模型名和端点
校验，不构造 HTTP 客户端、不解析 DNS、不预留预算、不访问数据库或输出配置值。
`resolve_mode` 保持原接收/回退策略，只表示名义模式，不能单独证明 provider 已就绪。
若完整校验拒绝，记录 `worker.live_unavailable` 和固定 reason；该消费者继续处理 FIXTURE，
LIVE 保持排队，不产生新 JobAttempt、fencing token 或投递尝试，也不降级或更改任务模式。
`run_once` 在校验前复制 Settings，校验和本轮 dispatch 使用同一私有快照；调用者后续修改原 Settings
只影响下一轮。此校验不探测网络连通性或上游认证有效性，真实运行的网络/认证失败仍遵守已有调用计费和恢复规则。
直接持久调度若遇 LIVE 任务但配置不可用，会失败 `TASK_MODE_UNAVAILABLE`，不降级为 fixture。
未知/不可验证模式不会构造 LIVE 客户端。固定表格路径继续使用已有有界解析规则。

这改变了旧行为：不能把演示任务通过重启 LIVE worker 升级，也不能依靠 `worker-once` 消费 LIVE 队列。
要继续 LIVE，操作者必须审查输入和所用提供方配置，使用新提交键新建明确 LIVE 任务。
新建仍遵守已有意图撤销/审批规则，不自动复制旧批准，也不自动重放旧业务提交。
任务模式仅表示允许的模型执行类别，不固定提供方、账户、端点或价格；有效 LIVE 消费者继续使用自己的配置。

## 迁移、旧消费者与回滚

迁移链增加 `task_runtime_mode`，位于 `sync_dispatch_receipts` 之后。
SQLite ADD COLUMN 带默认值、NOT NULL 与 CHECK，避免重建有大量外键引用的 task 表；同时安装 UPDATE 防误改触发器。
不重写旧输入、来源、任务状态、模型费用、计划、批准、Operation 或业务事实。
迁移前停掉所有 API/worker/其他写者与消费者并备份数据库，再显式执行 `python -m app.cli init-db`。
HTTP 启动仍只检查 schema，不自动迁移；反复 init 不重置现有任务模式。

**必须统一升级所有进程**：旧二进制忽略新列，仍可能按自身 LIVE 配置发送旧输入。
触发器只拦截 UPDATE。直接 `INSERT OR REPLACE` 可替换同 ID 记录及其模式，DELETE 后 INSERT 也不在保证内，
即使保持 foreign_keys=ON 仍不能把此触发器当作完整写者隔离。不得以关闭外键/删表等方式修复任务。
它不能给旧程序加发送检查，也不能防御可直接写数据库、修改文件或 DDL 的操作者。
只升级生产者或只升级数据库不形成安全边界。旧 writer 省略新列会得到 FIXTURE，但旧 consumer 仍不受保护。

新的标准消费者先完成配置校验，再在同一 BEGIN IMMEDIATE 领取事务内按模式过滤并写入原有租约；
标准应用路径不修改模式，重启后从数据库读取。直接数据库写者仍属于受信任边界，不是受保护的对手。
多进程共用原有 SQLite 单写者、租约、fencing 和预算约束。忙锁、旧进程、手工篡改和无限规模不在此保证内。
新消费者读取旧 schema 会报错，不会通过缺省值猜测 LIVE。

downgrade 删除触发器及运行模式列，保留其他表的数据；随后升级会把所有任务重新设为 FIXTURE。
**回滚会移除本安全边界**：须先停止所有消费者，保持 LIVE 关闭，核对历史任务后再决定是否运行旧版本。
SQLite DROP COLUMN 要求运行时支持该操作；只在测试所用 SQLite 上验证，不承诺任意旧 SQLite 可降级。
迁移/回滚与进程滚动升级没有被视为线上无停机方案。

领取前配置校验的后续修复不新增迁移或回填记录。`1f84f3a` 的合成复现中，缺模型、未知 provider 或非法
Responses URL 都会先领取再拒绝；重复恢复可用完三次尝试并变成 ATTEMPT_LIMIT，传输和 ModelUsage 均为零。
修复后反复扫描/错误配置不会新增尝试；已有合法过期尝试仍由 watchdog 恢复，配置修正后可继续领取。
已经因旧行为 FAILED 的任务不会自动重置次数、恢复批准或重提业务，须人工核对后使用新键创建任务。
回退此修复会重新暴露错误配置消耗尝试的问题，即使保留 runtime_mode 迁移也不能避免。

## HTTP 日志、启动错误与端点 URL

- `configure_logging` 与直接构造 `LiveLLMClient` 均屏蔽锁定依赖使用的 httpx/httpcore logger：
  httpx、httpcore、connection、http11、http2、proxy、socks，以及配置时已存在的子 logger。
  在原 logger 上添加丢弃过滤器并提高级别，阻止 URL、headers 和传输 traceback 流向自身/root handler；
  root 配置不改动，应用保留结构化模型/预算事件。
- Settings 的普通 ValidationError 字符串隐藏输入值；应用 factory、lifespan 准备/清理和 CLI 边界
  把非预期异常转为固定提示/错误码，不打印异常原文。受信任的固定 schema 修复提示仍保留。
  CLI 成功提示不再输出数据库 URL。失败返回非零或停止启动，不自动重试、回滚或假称已有提交未发生。
- 兼容端点原有校验继续拒绝 URL 用户信息、query、fragment、控制字符等；另外拒绝 URL 原文或一次
  percent 解码结果中出现已配置 API key。拒绝在构造模型客户端/预算预留前发生；凭据只能由传输层放进 Authorization。
  Responses provider 继续限制固定 OpenAI `/v1` 地址。正常兼容路径仍可使用，不把未知任意路径字符串当作密钥。

这不是全进程防泄漏保证：原始 `ValidationError.errors()`、调试器/局部变量、外部 HTTP/代理/访问日志、
任意运行时重新配置日志、未来依赖新 logger、未知或自定义编码的路径凭据仍须操作者约束。
应用日志仍包含既有任务/订单标识和路径；合成数据是前提。兼容端点没有网络目的地白名单，
显式配置的 HTTPS 主机仍可接收其密钥，模式字段不提供身份鉴别或端点审批。

## 合成验收

`tests/test_deployment_boundaries.py` 用阻断 socket/DNS 的 MockTransport 测试：模式矩阵、API/demo/表格入口、
重复输入/回答/同步重放、租约恢复、并发竞争、独立解释器消费者、旧 schema 升降级、外键/历史证据保留、
默认/非法模式及 UPDATE 限制、传输 logger 自带 handler、配置/启动/清理/CLI 故障和 URL 凭据拒绝。
`tests/test_live_worker_preflight.py` 另覆盖错误配置反复恢复、标准 worker 各入口、有效/无效消费者竞争、
过期任务修正配置后恢复、配置快照、校验故障先于领取、固定日志 reason，以及 SQL REPLACE 的实际边界。
原有 LIVE/provider/预算/来源测试明确在创建时选择 LIVE；手工 smoke 入口也显式建 LIVE 任务，
本轮只执行它的离线 mock 测试。没有真实浏览器、真实模型自由语言质量或真实部署验收。
