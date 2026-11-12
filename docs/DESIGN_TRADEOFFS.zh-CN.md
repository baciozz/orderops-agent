# 设计取舍与踩坑

本文只写仓库里有据可查的取舍和修复。数字与提交均可在本公开仓核对。

## 设计取舍

每条写清选了什么、放弃了什么、代价是什么。

### 模型只提案，规则核对，人工确认

选了：LLM 只提出改动，库存和已发货量由确定性规则核对，人确认后才导出清单；模型不能直接改单。

放弃了：让 Agent 自主执行改单。

代价：流程多一步人工确认，不能自动落单。

### 同步提交，不用队列

选了：用 SQLite，`POST /plans/{plan_id}/commit` 在一个 `BEGIN IMMEDIATE` 事务内同步提交业务写入。

放弃了：批准后排队新的 commit job，再用队列异步提交。

代价：租约和 fencing 只用在规划阶段的 worker（`JobAttempt`）上；提交阶段的 commit job 没有实现，提交也不领取新的 lease / fencing_token。

### 预算账本用悲观预留

选了：调用前先按悲观估算扣预留额（`RESERVED`）。进程中断、超时、连接失败，或成功响应缺少有效用量时，按预留额结算，不把预留直接释放成零。

放弃了：中断时按「可能还没计费」把预留清零。

代价：费用按偏高的口径算。预算门只会更保守，不会少算。

## 踩坑故事 A：回读前崩溃被显示成「通过」

**现象。** 提交事务已经把业务写入落库，但进程在回读核验前退出。当时提交时把 `committed_verification_pending` 写成 False，watchdog 和幂等重放看到的都是已通过，把它显示成 passed。

**根因。** 核验标记没有在同一提交事务里先标成「待核验」。崩溃发生在回读前时，库里留下的是「已通过」，恢复路径不会再做核验。

**修法。** 私有开发期修复（[dcd380d](https://github.com/baciozz/orderops-agent/commit/dcd380d9a11cd86bdf2018b4ccf7c4a41c0a510d)）：在提交的同一事务里先把状态标成待核验（`committed_verification_pending=True`），回读通过后再清掉。

**怎么验证。** 先写测试复现（[6e4ec6e](https://github.com/baciozz/orderops-agent/commit/6e4ec6ecc60b4c89d869903c9c2c9f6a00813cfc) 新增 `tests/test_commit_verification_pending.py`，当时共 5 条用例；该提交信息标了 red，先只加测试，后在 [dcd380d](https://github.com/baciozz/orderops-agent/commit/dcd380d9a11cd86bdf2018b4ccf7c4a41c0a510d) 修复）。其中 3 条分别断言：崩溃后、watchdog 之后、幂等重放之后都应显示 pending；另 2 条覆盖正常通过与核验失败。

**遗留。** `committed_verification_pending` 为 True 时，既表示还没核验，也表示核验失败；任务视图一律显示为 `pending`，界面上分不出来。

## 踩坑故事 B：减订到正好等于已收量时撞上数据库约束

**现象。** 采购单原订 20、已收 8、改订 8 时，状态仍是 `PARTIALLY_RECEIVED`，触发 `CHECK constraint failed: ck_po_status_qty`。该约束要求：`RECEIVED` 必须 `received_qty = ordered_qty`，`PARTIALLY_RECEIVED` 必须 `received_qty < ordered_qty`。

**根因。** 减量草稿改了订购量，却没有同步改状态。数量已经相等，还标着部分收货，直接违反约束，提交变成完整性错误。

**修法。** 私有开发期修复（[c732066](https://github.com/baciozz/orderops-agent/commit/c732066012a3f500c26225591e0e89200c1ca43c)）：当订购量正好等于已收量且大于 0 时，把采购单转为 `RECEIVED`，没有选择在预检里拒绝。`ck_po_status_qty` 要求 `RECEIVED` 时 `received_qty = ordered_qty` 且 `ordered_qty > 0`，这条边界正好满足。减量仍标 `PENDING_SUPPLIER`，等待供应商确认。

**怎么验证。** 先写测试复现（[04e1f99](https://github.com/baciozz/orderops-agent/commit/04e1f999eeec58081618e92dd5693499fc843191)）。`tests/test_review_po_reduce_to_received.py` 用原订 20、已收 8、改订 8 这条边界，断言提交后为 `(8, 8, RECEIVED)`。这只测了数量正好相等的边界。

**遗留。** 同一批修复还把新建订单的 `customer_id` 纳入审批哈希（[4750181](https://github.com/baciozz/orderops-agent/commit/475018171210480f51d37cbf30063b164cca60a2)）。测试写明：这是对直接改库的纵深防御，不是已证实的漏洞；公开 API 不会改这个字段。

## 踩坑故事 C：无关的并发写入让核验误判、耗尽预算

**现象。** 核验读完事实后，其他连接只要提交（哪怕只是更新 outbox 投递次数），就会让快照失效。旧实现把这类无关写入当成核验失败，反复出现会耗尽自动重试预算并转人工。

**根因。** 用 SQLite `data_version` 检测中间提交是对的，但版本变化被直接当成事实不一致，没有区分「无关写入」和「核验依赖的事实真的变了」。

**修法。** 私有开发期修复（[72938f9](https://github.com/baciozz/orderops-agent/commit/72938f9d53b826728a0577b9cf32cc0a017631c6)）：仍用同一物理连接的 `data_version` 判断中途有没有其他提交；若版本变了、首次核验成功且仍持有有效租约，就在结果写事务内最多复核一次当前事实。自动预算只扣一次。无关写入不再直接判失败；事实真变且不一致时仍失败。

**怎么验证。** `tests/test_verification_concurrent_writes.py` 覆盖无关 outbox 更新不再耗尽预算、真实不一致仍失败、锁内异常或租约到期仍保留 pending 等路径。持锁成本记在 `docs/VERIFICATION_CONCURRENT_WRITES.md`。

**遗留。** 锁内复核的持锁中位时间，在基准种子数据、额外 100 张、额外 1000 张合成订单上分别是 6.349、75.383、688.717 ms。这是诊断采样，不是生产性能保证。锁等待失败、事实错误、复核异常或租约过期，仍可能耗尽预算并转人工。

## 仍未实现、仍未验证

来源是 `docs/KNOWN_LIMITATIONS.md`，以及上面每个故事的遗留。

- 批准后不会排队新的 commit job，提交也不领取新的 lease / fencing_token。
- 没有常驻调度器或外部推送告警；pending 核验要靠显式运行 worker-loop、watchdog、`worker-once` 或提交重放，才会检查到期工作。
- 核验看的是当前事实，不是历史快照；已通过的操作不会自动重查。
- pending 在界面上既表示尚未核验，也表示核验失败，两者分不出来。
- 采购单减到已收量的修复，只测了数量正好相等的边界。
- 关联全部丢失、未关联单据或取消状态的真实性，没有完整审计；独立的来源审计尚未接入提交核验。
- 上传原件的持久存储、备份和清理策略尚未提供；提交前硬崩溃仍可能留下孤儿文件。
- 多用户、身份认证和会话权限尚未完成；本机 Host 白名单不是认证。
- 兼容 provider 只用 mock 测试，未做真实联调；真实浏览器 JavaScript 交互尚未验证。
- 自由语言能力，以及真实 ERP、完整来源父链，都不在已验证范围内。
