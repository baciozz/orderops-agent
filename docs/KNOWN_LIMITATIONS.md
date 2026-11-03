# 已知限制

Stage 1B 可以核对生产单和采购单：未裁剪、未到料的部分出变更草案，已执行部分不静默修改，提交时全部重新链接。下面这些还没做。

- 子 Agent 的实际执行、工具审计、DispatchStep 落库（R18–R21）
- inbox / outbox、租约、fencing 的运行时、取消竞争、重启恢复（R01–R09、R16）
- 文件解析、注入防护的执行路径、安全导出、来源对照（R10–R14）
- 完整任务界面，以及过期批准按钮置灰（R15 的完整报告、R24）
- live 模型的 HTTP 传输。预算熔断和「缺单价就保持 FIXTURE」已经可用（R22、R23），但没有提供方，也不会发出网络请求
- ORM relationship、生产认证、CSRF、多用户。Alembic 初始迁移已经接入。
- `docs/TEST_REPORT.md`、`HANDOFF.md`、`LEARNING_GUIDE.md` 留到阶段 5。现在的测试清单是 `docs/TEST_CHECKLIST.md`

禁止用途：连接真实客户、ERP、支付、采购或发货；把 fixture 成功说成模型理解；在单价未知时打开 live；把 `T-STUB` 当成业务结果。

跳过的测试不是通过。尚未实现的 B/R 用例在 pytest 里会显示跳过原因和计划阶段。
