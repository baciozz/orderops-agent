# 操作与故障交接

## 这是什么

这是本机合成数据的服装订单变更沙箱。模型只能提出建议；订单、库存、生产单和采购单由确定性内核检查，在人批准后一起提交，并回读核验。它不是生产 ERP，也不会替用户向供应商下单。

候选分支不等于已合并 main，验收时记录实际 checkout SHA，不把旧 main 结果套到新改动。源码/editable 安装须保留迁移资源；现有非 editable wheel 缺资源，尚不支持。

固定基线与复验结果见 [TEST_REPORT](TEST_REPORT.md)。正式版本 tag、公开仓库、部署、接真实业务和新 live 调用不由本文授权。

## 第一次启动与演示

优先使用 [两条合成验收](FIXTURE_ACCEPTANCE.md)：一条命令在全新临时库内演示库存改单和已裁保护恢复，保留 CSV/XLSX/审计，不重置默认库。它会明确模拟批准和提交，不能当作真实人工验收。

下面是另行手动网页演示（须新合成库，真实浏览器交互仍未验证）：

按 [README](../README.md#快速上手) 创建虚拟环境并安装锁定依赖，随后：

```bash
make init-db
make seed
make run
```

1. 打开 `http://127.0.0.1:8000/ui`，进入「提交改单」。
2. 保留 SO-1001 和预填文字「SO-1001 把未发的 40 件黑色 M 改成白色 M」。
3. 提交后触发 worker 一次。任务到 `AWAITING_APPROVAL` 时只生成计划，没有改订单。
4. 检查黑色减 40、白色加 40、已发部分不动，以及下游单据的影响。
5. 点击「批准当前计划」，再点「提交生效」。在全新 seed 中应看到 revision 2、核验 passed。
6. 打开订单查看和下载结果，核对 Operation ID。不要把 T-STUB 当成执行结果。

网页 worker 强制 FIXTURE；自由改写可能得到 FIXTURE_NOT_FOUND，不会悄悄调用付费模型。worker 一次处理全局队列中的一条任务，可能不是当前页面的任务；查看返回的任务 ID。
重复 seed 不重置已有 SO-1001，演示后再次执行同一文字未必得到同样结果。需要重演时使用新副本/新数据库，保留旧库作为证据，不直接删除正在使用的库。

本机服务已有 Host 白名单、浏览器同源校验与防嵌套响应头，但没有身份认证；剥离全部浏览器来源头的请求无法与 CLI 区分。不要通过局域网、公网或代理隧道暴露服务；完整边界见 [browser-security](browser-security.md)。

## 正常状态与故障处理

| 看到的状态 | 意味着什么 | 下一步 |
| --- | --- | --- |
| QUEUED | 已入队，还没规划 | 手动运行 worker；已接收不等于已修改 |
| NEEDS_CLARIFICATION | 缺信息或存在冲突 | 网页回答入口和 answers API 已实现；按 [两条合成验收](FIXTURE_ACCEPTANCE.md) 的原文恢复，真实浏览器交互仍待验证 |
| AWAITING_APPROVAL | 草案待人工检查 | 核对来源、差量和下游后批准 |
| STALE_INTENT / STALE_DEPENDENCY | 意图、库存或单据事实已变化 | 按当前事实重新规划并批准，不强行复用旧批准 |
| EXPIRED | 批准已过有效期 | 刷新查看当前状态，按当前版本的批准接口重新确认；不要改数据库延长期限 |
| committed_verification_pending | 业务已提交，但核验未完成 | 记录 Operation ID；只重试核验，不再次创建业务操作 |
| FIXTURE_NOT_FOUND | 输入不在固定演示字典 | 使用明确的演示输入；这不是模型理解失败的测量 |

提交超时或页面显示网络错误时，先查任务/Operation。若重放原提交请求，保留原幂等 key，不能换 key 盲重发。

## 重启和恢复

默认文件为 `data/oos.db`，SQLite WAL 运行期间还可能有 `-wal` / `-shm`。不要仅复制运行中的 db 文件并声称完成备份；本项目尚无已验证的备份恢复流程。
停止 API 可在运行终端按 Ctrl+C。重启前升级 schema 使用 `make init-db`，再启动 API。

```bash
python -m app.cli worker-once
python -m app.cli reconcile-budget
```

worker-once 调用 watchdog、处理一条规划任务并投递 outbox；pending 核验可在扫描中重试，仍不自动批准或再次提交业务。需要连续检查可由操作者显式运行有界 FIXTURE `worker-loop`，默认 5 秒间隔、最多 60 轮；不安装服务或自动重启，详见 [WORKER_LOOP](WORKER_LOOP.md)。
reconcile-budget 只将超时的预算预留标记为 ORPHANED，不释放可能已计费的预算。
pending 核验已持久化自动重试计数、下次时间和有期限的唯一租约 token。首次提交读回另计，默认最多 5 次自动重试；首次可立即运行，失败后指数退避，耗尽后保留 COMMITTED 和 pending 并转人工。查看任务视图的 `verification_manual_required` 与日志 `verification.manual_required`，调查后运行：

```bash
python -m app.cli verify-operation OPERATION_ID
```

仅已转人工且没有有效租约的 pending 操作可发起人工核验；已通过则直接成功返回，不重复读回。退出码 0 表示已通过，1 表示仍未通过，2 表示不存在或尚不可执行。命令不重置自动预算、不修复数据、不再次提交业务，也不能强制通过。配置与迁移边界见 [API](API.md#提交后的有限只读核验)。

没有常驻调度器；无人显式运行 `worker-loop`、调用 watchdog / `worker-once` 或原提交重放时，到期核验不会自己执行。仍无外部推送告警，且核验当前事实而非历史快照，见 [issue #20](https://github.com/baciozz/order-ops-superagent/issues/20)。

## 后续接手顺序

1. 集成任何补丁后重跑 `make check` 和离线评测，报告候选 SHA 与原始结果，不复用基线数字。
2. 上传、澄清、取消入口已实现；补做真实浏览器验收，实际验证 JavaScript、刷新、重复点击与网络中断。
3. 在现有有限核验重试和人工入口之上，规划常驻调度与外部告警；明确当前事实核验与历史快照的边界，不让恢复逻辑自动批准或重复业务写入。
4. 在预算和数据授权范围内做独立 holdout 模型评测，业务人士核对合成案例。
5. 之后再做行为不变的 kernel 拆分，保留事务边界和竞争测试。

故障报告应包含版本、模式、复现输入（仅合成/允许分享的数据）、任务和 Operation ID、错误码、预期/实际状态、已尝试操作。不得粘贴密钥或真实客户信息。
