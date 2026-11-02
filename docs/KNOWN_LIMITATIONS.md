# 已知限制

Stage 2 在 fixture 模式下接通持久 inbox、worker、outbox、取消和恢复。parser 只查 fixture，不调用 live 模型。库存和下游数字由内核重算。下面这些还没做。

- Worker 由 `worker-once` / Python 调用驱动，没有常驻调度服务；outbox 默认消费者只写本地去重回执，浏览器实时推送尚未接入。R01–R09、R16 有确定性故障测试。
- 回答仍需完整 fixture 文本；source_ids 只持久保存，解析在 Stage 3。
- 文件解析、注入防护的执行路径、安全导出、来源对照（R10–R14）
- 完整任务界面，以及过期批准按钮置灰（R15 的完整报告、R24）
- live 模型的 HTTP 传输。预算熔断和「缺单价就保持 FIXTURE」已经可用（R22、R23），但没有提供方，也不会发出网络请求
- ORM relationship、生产认证、CSRF、多用户。Alembic 初始迁移已经接入。
- `docs/TEST_REPORT.md`、`HANDOFF.md`、`LEARNING_GUIDE.md` 留到阶段 5。现在的测试清单是 `docs/TEST_CHECKLIST.md`

禁止用途：连接真实客户、ERP、支付、采购或发货；把 fixture 成功说成模型理解；在单价未知时打开 live；把 `T-STUB` 当成业务结果。

跳过的测试不是通过。尚未实现的 B/R 用例在 pytest 里会显示跳过原因和计划阶段。
