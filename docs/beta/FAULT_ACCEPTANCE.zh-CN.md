# 业务故障与恢复回归索引

应用版本1e63f7c7a83c3ac44289727d408956d01d56e489。2026-11-09在合成隔离库实际运行下面的9个既有测试文件，111项通过、0失败，60.86秒；JUnit逐例结果保留于本轮私有证据目录。使用Python模块入口，与Makefile相同；一次直接调用pytest可执行文件因tests包导入路径收集失败，保留该失败日志，没有算通过。此索引复用真实业务断言，不添加仅凑数量的测试。

```bash
python -m pytest -q --no-cov \
  tests/test_competing_order_journeys.py \
  tests/test_approval_recovery.py tests/test_review_customer_hash.py \
  tests/test_sync_dispatch_safety.py tests/test_source_availability.py \
  tests/test_http_storage_failure.py tests/test_commit_verification_pending.py \
  tests/test_review_workspace.py tests/test_cancel_journeys.py
```

| 类别 | 正向与负向条件；实际检查 |
| --- | --- |
| 同单新旧请求 | competing_order_journeys：旧计划/批准拒绝，重新规划成功，订单修订与提交计数核对 |
| 共享库存竞争 | competing_order_journeys：首单占用后第二单拒绝超卖，重新规划安全范围 |
| 批准过期 | approval_recovery：过期明确续批、并发只创建一个；未过期重放返回原批准；其他失效不能被续期掩盖 |
| 批准后资料变化 | review_customer_hash：客户需求变化后拒绝提交；source_availability：原件恢复后才重新允许 |
| 重复点击/同键竞争 | sync_dispatch_safety与review_workspace：同键只建一个任务/模型调用，异内容冲突，新键不吞掉合法新请求 |
| 重启/进程退出 | sync_dispatch_safety：在计划写入前后真实进程退出，恢复原任务或明确失败；review_workspace：重建应用后找回 |
| 数据库写入失败 | http_storage_failure与review_workspace：合成故障触发回滚、安全503或显式恢复，同键恢复不重复业务写入 |
| 附件丢失/摘要变化 | source_availability：丢失前后批准及并发提交拒绝，可信相同原件恢复后继续 |
| 核验失败 | commit_verification_pending：已提交但核验失败保留pending，不能把提交当核验成功 |
| 恢复/迟到/重放 | cancel_journeys和review_workspace：取消/新资料/恢复后迟到结果不覆盖；已提交重放不重复；确认与导出状态受版本约束 |

这是确定性/本机模拟提供方及部分真实HTTP回归，不是付费模型、真人浏览器或生产ERP证明。源码完整门禁另在1e63f7c通过2789项、5项明确OCR配置跳过；实际中文OCR另有专门组件运行记录。清单入口只有建议权限，零业务写入本身不等于模型理解正确。
