# 学习导读：能解释、能修改、能验证

目标不是记住 Agent 名称，而是独立说明一条订单为何可以修改、何时必须停止，以及重试为何不会重复扣库存。

## 先读一条链

按以下顺序打开代码，每一步写一句自己的解释：

1. `app/api/inputs.py`、`app/orchestration/inbox.py`：输入怎样变成持久任务，同一事件如何去重。
2. `app/orchestration/worker.py`、`dispatch.py`：谁领取任务，旧 worker 为什么不能覆盖新结果。
3. `app/orchestration/agents.py`、`app/adapters/`：模型/子 Agent 能提出什么，不能做什么。
4. `app/services/kernel.py`：预检、批准、提交的区别；为什么提交前还要重读库存和版本。
5. `app/orchestration/recovery.py`、`watchdog.py`：提交成功但响应丢失时如何对账。
6. `app/services/task_view.py`：页面是业务事实的投影，不是完成事实本身。

对照 [架构](../ARCHITECTURE.md)、[接口](API.md) 和 [固定测试证据](TEST_REPORT.md)。不要把 fixture 输出说成语言模型推理结果。

## 五个小练习

先在独立练习分支写测试与自己的实现，再与现有实现对照。不要修改真实库、削弱批准或删掉失败测试。下表命令运行仓库现有参考测试，自己的新增测试还需单独运行。

| 练习 | 输入与预期 | 参考验证 |
| --- | --- | --- |
| 1. 数量和执行边界 | STOCK 订购 100、已发 60，未执行 40；MTO 订购 300、已裁 120，未执行 180。总量不能低于已执行量；0 不能偷偷当取消 | `python -m pytest tests/test_domain.py tests/test_qi_zero_qty_not_cancel.py` |
| 2. 库存缺口 | on_hand=30、reserved=10，新增 25；可用 20、缺口 5；拒绝整个提交，不先占一部分 | `python -m pytest tests/test_stage1a.py` |
| 3. 安全导出 | 文本 `=1+1`、`@SUM(A1:A2)` 以及前导空格的公式前缀；导出后必须作为文本，不执行公式 | `python -m pytest tests/test_uploads.py tests/test_stage3.py` |
| 4. 过期或变化的批准 | 计划批准后库存/单据版本变化；旧计划提交必须被拒绝，不能继续写入 | `python -m pytest tests/test_stage1a.py tests/test_stage1b.py` |
| 5. 已提交但核验待补 | 注入回读失败，再恢复和重复调用；只清 pending，不新增订单修订、库存流水或 Operation | `python -m pytest tests/test_commit_verification_pending.py tests/test_qi_verification_retry.py` |

每题提交四样东西：自己的规则解释、一个失败前的测试、实现差异、修复后的测试输出。只跑对应文件是局部验证，完成修改后仍须 `make check`。

## 用三分钟讲项目

- 问题：订单改了，生产/采购仍指向旧版本，可能做错颜色或数量。
- 方案：解析提出结构化草案，确定性规则计算影响，人批准绑定的版本后才原子提交。
- 关键失败：批准后已有部分开裁，旧批准必须失效；提交回执丢失，不能重新扣占。
- 证据：展示合成演示、数据库断言、一次实际缺陷与修复。固定版本测试数见报告，不把测试覆盖率当准确率。
- 边界：本机 SQLite、合成数据、有限模板、未完成生产认证与真实 ERP 接入；模型历史样本很小。

可以准确讲“设计并验证可靠的订单变更沙箱”。不能据此声称生产落地、通用服装业务覆盖或已证明多 Agent 比单 Worker 更优。若要比较编排收益，应在同一数据、模型、工具和预算下设顺序基线，测业务正确性、澄清、耗时与费用。
