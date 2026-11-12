# S3 单/双角色对照结果（v1）

> 120 条合成样本上的**开发期诊断**，`formal_qualified=false`，不是正式准确率，不能当作生产性能或发布门槛。参考答案由小八复核，不是 Qi 人工复核。S3 全部组在 `model_max_tokens=3000` 下测得，**不得与 S2B 在 1000 上限下的 100/120（83.3%）直接对比**。

按 [预登记 v1](AGENT_COMPARISON_PREREG_V1.zh-CN.md)（#67，427d3f9）的指标与判定口径写。采集代码冻结在 `ff97cb13a216`（#68 分支 `portfolio/s3-agent-comparison`），三组 summary 的 `code_sha` 都是这个值。本文只做**离线重评分，零模型调用**。

## 0. 结论（先看这里）

- **主结论只看主模型 `gpt-6-luna` 的单/双角色对照。** 按预登记判定规则（总体指标须 3 次重复方向一致，且差异 ≥3 个样本，才算有差异）：
  - `single` 对 `double_roles`：原始准确率与规则后准确率每次重复都高 5 / 4 / 4 条，方向一致，每次都 ≥3 条。按规则判为有差异：**在这个合成数据集、这组配置下，单角色每次重复都多对 4～5 条，三次方向一致**。
  - `single` 对 `double_same_prompt`：每次分别高 1 / 2 / 7 条，方向一致，但有两次不足 3 条，判为**无结论**。
  - `double_same_prompt` 对 `double_roles`：+4 / +2 / −3，方向不一致，判为**无结论**。
  - 危险错误：三组 9 次重复的原始层和规则后层都是 0，没有差异。
- 分母 ≤20 的指标（转人工召回 ⑤ / ⑫ / 合并 20、应澄清召回、静默忽略）只列数字，**不下结论**。
- 双组每条样本调用 2 次，费用约为单路的 1.9–2.0 倍（$0.000533–0.000545 对 $0.000277 每条）。
- 强模型 `gpt-6.1-sol` 没有可比数据：冒烟因参数不兼容失败，待 Qi 决定，见第 5 节。即使以后跑，因推理档不同也只作参考、不下结论。
- 局限：这是 120 条合成样本、单一模型、3 次重复的诊断，没有做显著性检验，参考答案未经 Qi 复核。上面的「有差异」只表示在这个合成数据集、这组配置下满足预登记的判定规则，不推广到其他数据、模型或生产。

## 1. 运行概况

| 组 | 观测（样本×重复） | 客户端调用 | 账本行 | 账本花费 | 输入 / 输出 token | 每条观测 | 每条原始正确 | 第 10 次外推（累计） |
|---|---|---|---|---|---|---|---|---|
| `single` | 360/360 | 360 | 361 SETTLED（含 1 行 $0） | $0.099826 | 747,202 / 50,210 | $0.000277 | $0.000328 | $0.568117 |
| `double_same_prompt` | 360/360 | 720 | 720 SETTLED | $0.191794 | 1,475,580 / 88,446 | $0.000533 | $0.000652 | $0.522420 |
| `double_roles` | 360/360 | 720 | 720 SETTLED | $0.196374 | 1,508,340 / 91,085 | $0.000545 | $0.000675 | $0.532054 |
| `single` 保守记账（ORPHANED，单列） | — | — | 1 ORPHANED（MU-bb9468e735ec4959） | $0.002221 | 7,210 / 3,000 | | | |
| 合计（仅 SETTLED） | 1080 | **1800**（= 预登记计划） | 1801 | **$0.487994** | 3,731,122 / 229,741 | | | 均 < $10 |
| 合计（含 ORPHANED） | | | 1802 | **$0.490215** | 3,738,332 / 232,741 | | | |

- 每条观测和每条原始正确的费用都按记录所附账本（$0.099826 / $0.191794 / $0.196374）计算。分母分别是 360 条观测，和 3 次重复合计的原始正确数：304 / 294 / 291。
- `single` 的账本比记录多出 2 行：
  - 1 行是 ORPHANED 的保守记账，$0.002221，见第 3 节。
  - 1 行是 0 token、$0 的 SETTLED 行（2026-11-11 18:37 北京时间，早于 ff97cb1 提交，不属于任何观测），不影响费用。
- 三组 `MODEL_INCOMPLETE`（因输出上限被截断）都是 **0**。
- 双组的 `INCONSISTENT_PROPOSAL` 都发生在同一条观测的两路上，所以是 2 次调用、1 条观测。

### 错误码分布（全部计入分母，按已完成失败记）

| 组 | 调用级 | 观测级 | `MODEL_INCOMPLETE` |
|---|---|---|---|
| `single` | 正常 357，`MODEL_TIMEOUT` 2，`INCONSISTENT_PROPOSAL` 1 | 同左（每条 1 次调用） | 0 |
| `double_same_prompt` | 正常 718，`INCONSISTENT_PROPOSAL` 2 | 正常 359，`INCONSISTENT_PROPOSAL` 1（r3） | 0 |
| `double_roles` | 正常 718，`INCONSISTENT_PROPOSAL` 2 | 正常 359，`INCONSISTENT_PROPOSAL` 1（r1） | 0 |

注意：`reports/.../observations.jsonl` 每行的顶层 `error` 字段取的是记录级错误，调用级错误写在 `calls[].error` 里，所以只看顶层字段会以为「error 全空」。上表按 `state.json` 中每次调用的 `error` 统计。

## 2. 各组、各次重复指标

分母为该次重复计划的 120 条样本，失败、分歧都算在分母里。「需人工」= 规则后结果与参考不一致的条数 / 120，由参考自动判定，不是真人实测。转人工召回只认 `clarify`，静默忽略单独计数。

| 组 | 重复 | 原始准确率 | 规则后准确率 | 需人工 | 危险 原始/规则后/规则拦下 | 应澄清召回 | 不必要澄清 | 转人工 ⑤ | 转人工 ⑫ | 转人工合并 20 | 静默忽略 ⑤/⑫ | 不含 ⑪（次要） |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `single` | 1 | 102/120 | 102/120 | 18/120 | 0/0/0 | 10/10 | 16 | 7/10 | 2/10 | 9/20 | 3/8 | 100/110 |
| `single` | 2 | 100/120 | 100/120 | 20/120 | 0/0/0 | 10/10 | 18 | 8/10 | 2/10 | 10/20 | 2/8 | 98/110 |
| `single` | 3 | 102/120 | 102/120 | 18/120 | 0/0/0 | 10/10 | 17 | 7/10 | 2/10 | 9/20 | 3/8 | 101/110 |
| `double_same_prompt` | 1 | 101/120 | 101/120 | 19/120 | 0/0/0 | 10/10 | 17 | 7/10 | 2/10 | 9/20 | 3/8 | 100/110 |
| `double_same_prompt` | 2 | 98/120 | 98/120 | 22/120 | 0/0/0 | 10/10 | 14 | 7/10 | 2/10 | 9/20 | 3/8 | 97/110 |
| `double_same_prompt` | 3 | 95/120 | 95/120 | 25/120 | 0/0/0 | 10/10 | 16 | 7/10 | 2/10 | 9/20 | 2/8 | 94/110 |
| `double_roles` | 1 | 97/120 | 97/120 | 23/120 | 0/0/0 | 9/10 | 13 | 7/10 | 2/10 | 9/20 | 2/8 | 96/110 |
| `double_roles` | 2 | 96/120 | 96/120 | 24/120 | 0/0/0 | 9/10 | 16 | 7/10 | 2/10 | 9/20 | 2/8 | 95/110 |
| `double_roles` | 3 | 98/120 | 98/120 | 22/120 | 0/0/0 | 9/10 | 16 | 7/10 | 2/10 | 9/20 | 2/8 | 97/110 |

三次均值（原始 = 规则后）：`single` 84.44%（304/360），`double_same_prompt` 81.67%（294/360），`double_roles` 80.83%（291/360）。

### 组间比较（预登记判定）

| 比较 | 指标 | 每次重复差（条，r1/r2/r3） | 方向一致 | 每次 ≥3 条 | 判定 |
|---|---|---|---|---|---|
| `single` − `double_same_prompt` | 原始 / 规则后 | +1 / +2 / +7 | 是 | 否 | 无结论（视为无差异） |
| `single` − `double_roles` | 原始 / 规则后 | +5 / +4 / +4 | 是 | 是 | 有差异：在本数据集与配置下 `single` 每次多对 4～5 条，方向一致 |
| `double_same_prompt` − `double_roles` | 原始 / 规则后 | +4 / +2 / −3 | 否 | 否 | 无结论（视为无差异） |
| 三组两两 | 危险错误（原始 / 规则后） | 0 / 0 / 0 | — | — | 无差异 |

本文采用较严的读法：3 次方向一致，且每次差 ≥3 条，才算有差异。换成「均值差 ≥3 条且方向一致」的读法，判定也不变：三组差值的均值分别是 3.3 条（但有两次 <3）、4.3 条、1.0 条。

### 12 类原始正确数（每类 10 条，r1/r2/r3；只列数字）

| 类 | `single` r1/r2/r3 | `double_same_prompt` r1/r2/r3 | `double_roles` r1/r2/r3 |
|---|---|---|---|
| cancel_remaining | 8/7/8 | 7/7/7 | 8/5/7 |
| clarify | 10/10/10 | 10/10/10 | 9/9/9 |
| colloquial | 7/6/7 | 5/6/6 | 6/6/6 |
| cross_order | 9/10/9 | 10/10/9 | 10/10/9 |
| increment | 9/9/9 | 10/8/9 | 10/9/9 |
| multi_change | 9/8/10 | 10/8/7 | 8/9/9 |
| new_order | 10/10/10 | 10/10/9 | 9/9/9 |
| out_of_scope | 10/10/10 | 10/10/10 | 10/10/10 |
| pack_unit | 2/2/1 | 1/1/1 | 1/1/1 |
| prompt_injection | 10/9/10 | 10/10/9 | 8/10/10 |
| replace_remaining | 9/9/9 | 9/9/9 | 9/9/9 |
| set_total | 9/10/9 | 9/9/9 | 9/9/10 |

⑪ `pack_unit` 留在官方分母里。三组都只答对 1–2/10，大多是不必要澄清（见下表）。原因是合同没有包装单位字段，换算关系要从款名文字里读出来。「不含 ⑪」的次要数字见上面主表最后一列，只作补充，不能替换主数字。

### 不必要澄清（按类）

| 组 | r1 | r2 | r3 |
|---|---|---|---|
| `single` | {"cancel_remaining": 2, "colloquial": 2, "increment": 1, "multi_change": 1, "pack_unit": 8, "replace_remaining": 1, "set_total": 1} | {"cancel_remaining": 3, "colloquial": 4, "increment": 1, "multi_change": 1, "pack_unit": 8, "replace_remaining": 1} | {"cancel_remaining": 2, "colloquial": 3, "increment": 1, "pack_unit": 9, "replace_remaining": 1, "set_total": 1} |
| `double_same_prompt` | {"cancel_remaining": 2, "colloquial": 4, "pack_unit": 9, "replace_remaining": 1, "set_total": 1} | {"cancel_remaining": 2, "colloquial": 3, "pack_unit": 7, "replace_remaining": 1, "set_total": 1} | {"cancel_remaining": 2, "colloquial": 3, "increment": 1, "pack_unit": 9, "replace_remaining": 1} |
| `double_roles` | {"cancel_remaining": 2, "colloquial": 3, "pack_unit": 7, "replace_remaining": 1} | {"cancel_remaining": 2, "colloquial": 4, "multi_change": 1, "pack_unit": 8, "replace_remaining": 1} | {"cancel_remaining": 2, "colloquial": 4, "multi_change": 1, "pack_unit": 8, "replace_remaining": 1} |

### 双组：两路分别评分、分歧率（分母 120）

| 组 | 重复 | 路 0 原始 | 路 1 原始 | 危险（路 0 / 路 1） | 分歧 | 类型 | 分歧样本 |
|---|---|---|---|---|---|---|---|
| `double_same_prompt` | 1 | 102/120 | 102/120 | 0/0 | 2/120 | {"intent_differs": 2} | lv1-c04-10, lv1-c10-02 |
| `double_same_prompt` | 2 | 101/120 | 103/120 | 0/0 | 8/120 | {"intent_differs": 8} | lv1-c02-05, lv1-c02-07, lv1-c04-02, lv1-c09-02, lv1-c09-08, lv1-c10-01, lv1-c11-02, lv1-c11-09 |
| `double_same_prompt` | 3 | 101/120 | 99/120 | 0/0 | 9/120 | {"call_failed_or_invalid": 1, "intent_differs": 8} | lv1-c01-07, lv1-c04-10, lv1-c05-07, lv1-c07-08, lv1-c08-07, lv1-c09-02, lv1-c09-08, lv1-c09-09, lv1-c10-02 |
| `double_roles` | 1 | 104/120 | 102/120 | 0/0 | 10/120 | {"call_failed_or_invalid": 1, "intent_differs": 9} | lv1-c01-07, lv1-c05-08, lv1-c06-06, lv1-c08-02, lv1-c08-07, lv1-c09-08, lv1-c09-09, lv1-c10-03, lv1-c11-02, lv1-c11-05 |
| `double_roles` | 2 | 103/120 | 98/120 | 0/0 | 8/120 | {"intent_differs": 8} | lv1-c01-07, lv1-c02-07, lv1-c04-02, lv1-c04-07, lv1-c04-10, lv1-c05-08, lv1-c06-06, lv1-c11-02 |
| `double_roles` | 3 | 102/120 | 101/120 | 0/0 | 6/120 | {"intent_differs": 6} | lv1-c02-07, lv1-c04-10, lv1-c05-08, lv1-c06-06, lv1-c07-10, lv1-c11-05 |

两路单独评分时，单路准确率（98–104/120）比合并结果高。这是因为预登记规定分歧一律交人工，不挑胜者。这组数字只用来说明合并规则的代价，不作为组间结论。

### 三次重复一致性（按样本，分母 120）

| 组 | 3 次正确与否一致 | 3 次业务意图一致 | 3 次输出完全相同（冻结 runner 的 `proposal_sha256`，含自由文本） |
|---|---|---|---|
| `single` | 111/120 | 109/120 | 1/120 |
| `double_same_prompt` | 108/120 | 105/120 | 0/120 |
| `double_roles` | 111/120 | 107/120 | 3/120 |

### 客户端延迟（秒）

`state.json` 里没有存 `wall_s`，所以每条观测取各路 `calls[].elapsed_s` 的最大值；并行两路时这近似墙钟，不含本地合并，也不是端到端耗时。

| 组 | 全部尝试 p50 / p95（n） | 仅成功 p50 / p95（n） |
|---|---|---|
| `single` | 2.14 / 3.16（360） | 2.13 / 3.14（357） |
| `double_same_prompt` | 2.29 / 3.27（360） | 2.29 / 3.29（359） |
| `double_roles` | 3.20 / 6.75（360） | 3.20 / 6.86（359） |

## 3. 挂起事件与超时

`single` 共有 2 条 `MODEL_TIMEOUT`，证据强度不同，分开标注：

- `lv1-c10-04` r1：**挂起导致，有 etime 证据**。
- `lv1-c08-07` r2：**疑似挂起，证据只有账本时间和客户端计时对不上**（账本 `reserved_at → settled_at` 的墙钟远长于客户端单调时钟的 `elapsed_s`），没有进程 etime 证据。

两条合计 2 条，未超过「超过 3 次即停下问小八」的阈值。两组双路实验都没有超时。

| 观测 | 账本 | 预留（北京时间） | 结算（北京时间） | 账本墙钟 | 客户端 elapsed | 记账 |
|---|---|---|---|---|---|---|
| `s3-single-r1-lv1-c10-04` | MU-4458ab76ef074533 | 11-11 23:45:31 | 11-12 01:20:44 | 95 分 13 秒 | 60.13 s | SETTLED $0.002159（按预留额保守结算） |
| `s3-single-r2-lv1-c08-07` | MU-bf367d6cde4b4e2e | 11-12 01:28:16 | 11-12 01:46:29 | 18 分 13 秒 | 60.16 s | SETTLED $0.002164（按预留额保守结算） |

**事件 1（lv1-c10-04，挂起导致，有 etime 证据）：** 云电脑在北京时间 23:45–01:20 被挂起，导致 `MODEL_TIMEOUT`。证据（`suspend-incident-1.md`，仓库外笔记）：
- 01:20 时进程 119878 的 `ps etime` 只有 12:09，但它大约 23:32 就启动了；
- 日志在 90/360 之后约 95 分钟没有输出；
- 上表账本墙钟 95 分钟，而客户端单调时钟只走了 60 秒。

按预登记，该条计为**已完成失败，没有重发**。两笔保守记账**各记一次**：MU-4458ab76ef074533 SETTLED $0.002159；MU-bb9468e735ec4959（第 92 条，停进程时还在途）在续跑时经 `reconcile_reservations` 标为 ORPHANED，$0.002221 照算。续跑从 `s3-single-r1-lv1-c09-02` 开始，用的是同一 ff97cb1、同一 state，`run_id` 不变。被 ORPHANED 的那次在途请求所属的观测是续跑时重新发出的，所以 `single` 实际向提供方发出了 361 次请求，观测仍是 360 条。

**事件 2（lv1-c08-07，疑似挂起）：** 重评分时从账本时间戳发现，之前没有单独报告。证据只有账本时间和客户端计时对不上（北京时间 01:28:16 预留、01:46:29 结算，墙钟 18 分 13 秒，客户端 60.16 s），没有进程 etime 或日志证据，所以只标「疑似挂起」。按同一规则计为已完成失败，没有重发。

**另记一条（不是超时）：** `s3-single-r1-lv1-c08-06` 是一次成功调用（北京时间 23:36–23:44），客户端耗时 494 秒，已超过 60 秒超时却没有报超时，结果计入成功。**原因未查明，可能和挂起有关**，不下结论。

**敏感性（仅供参考，正式分以含该失败为准）：** 去掉 `s3-single-r1-lv1-c10-04` 后，`single` r1 为 102/119（85.71%），r2 / r3 不变（100/120、102/120），3 次合计 304/359（84.68%）。正式分 r1 = 102/120（85.00%），合计 304/360（84.44%）。超时那条本来就没有答对，所以去掉它不改变分子，组间判定（+5 / +4 / +4）也不变。如果连事件 2 也去掉，r2 为 100/119，判定同样不变。

## 4. 已知显示问题与偏离预登记（冻结点未改）

`benchmarks/run_live_eval.py:2289` 的进度行打印 `completed=skipped+calls_done`，`planned=len(schedule_rows)`。其中 `calls_done` 按调用计，分母按观测计，单位混用了，所以双组跑满时日志显示 `720/360`，这属于正常。第 10 次外推门禁用的是账本行数（调用次数），不受这个问题影响。冻结点没有改，留到 S3 合入后的重构再处理。

另外两点：
- 双组 summary 的 `worktree_dirty=true`，原因是采集时冻结工作树里有前一组未入库的 `reports/` 输出（未跟踪文件），代码本身没有改动，三组的 `code_sha` 都是 ff97cb1。

### 偏离预登记

- **运行时上限：** 各组 summary 记录的运行时 `cap_usd` 实际是默认的 **$5.00**（= min（预登记主模型 $10、全局 $25，`live_budget_usd` 默认 $5）），而预登记的主模型上限是 $10。这比预登记更严；全程累计最高 $0.536332，**未触发**。
- 强模型推理档的偏离见第 5 节和 #70，尚未执行。

## 5. 强模型 `gpt-6.1-sol`：冒烟因参数不兼容失败，待 Qi 决定（A 或 B）

- 2026-11-12 03:10（北京时间）在 ff97cb1 上跑了 5 条预登记冒烟（`lv1-c01-01`、`c05-01`、`c08-01`、`c10-01`、`c12-01`）。结果 5 次全部 `MODEL_HTTP`，输入输出都是 0 token，账本 5 行都是 $0。外推为 $0.536332，没有意义。
- 原因：ff97cb1 在默认配置下请求体发 `reasoning.effort="none"`（`app/config.py` 中 `model_reasoning_effort` 默认 `"none"`；`check_auth` 里写死为 `"none"`）。小八另外单独探测：`gpt-6.1-sol` 对此返回 400 `unsupported_value`，只支持 low / medium / high / xhigh / max；端点、鉴权和模型都可用。这条 400 证据来自小八的探测，本次重评分（零调用）没有复现。
- **360 次全量没有批准，冒烟也没有重跑。** 改冻结点等于改预登记。偏离预登记的改动（按模型配置推理档：sol 用 low，luna 保持 none）放在 draft PR [#70](https://github.com/baciozz/order-ops-superagent/pull/70)（分支 `portfolio/s3-reasoning-effort`）等 Qi 决定。若选方案 A，强模型组在 #70 合入（压缩为一个提交）后的提交上跑，code_sha 待合入后填写。
- 已给 Qi 两个选项：
  - **A**：把 sol 改为 low 推理档后重新冒烟。外推要算上 low 档多出的推理 token，累计不超过 $22。
  - **B**：本次不做强模型对照。
- **待 Qi 决定（A 或 B）；若之后跑了，另开补充 PR**，不改本文的主模型结果。
- **Qi 回复前不调用任何模型。** 即使选 A 跑完，强模型与主模型的推理档不同（low 对 none），存在混杂，结果只作参考、**不下结论**。本文主结论只来自主模型单/双角色对照。

## 6. 代码版本、预算与实际花费

| 项 | 值 |
|---|---|
| 采集 `code_sha` | 见下方「按组 code_sha」 |
| 重评分 `rescored_with_code_sha` | `e9f3a8d817a92e89211e4c010fc4baf0d8e9cd57`（最后一次修改评分脚本的提交，与 `rescore_summary.json` 一致）。这是 PR 分支上的 SHA，squash 合并后以 main 上对应 SHA 为准 |
| 冻结评分路径相对 ff97cb1 是否未改 | `app/`、`benchmarks/score_review.py`、`run_live_eval.py`、`compare_readers.py`、`evals/live_v1` 均未改（`frozen_scoring_paths_unchanged=true`） |
| manifest / 索引 SHA | 与预登记一致（重评分时由 `build_comparison_index` 重新校验） |
| 授权总额 | $30（$25 无条件停止） |
| 主模型上限 | $10（共享账本累计） |
| 强模型上限 | $22（共享账本累计） |
| 共享账本累计（gpt-6-luna） | **$0.536332**：1970 SETTLED $0.534111 + 1 ORPHANED $0.002221；1971 行 |
| 其中 S2B（含冒烟） | $0.046117 |
| 其中 S3 主模型三组 | **$0.490215**（含 ORPHANED $0.002221；仅 SETTLED $0.487994） |
| 强模型冒烟 | $0（5 行，0 token） |
| 距各上限 | 主模型 $10 剩 $9.46；全局 $25 剩 $24.46 |

### 按组 code_sha

| 组 | 模型 | code_sha | 状态 |
|---|---|---|---|
| `single` | gpt-6-luna | `ff97cb13a21665082ab0b62f95e016c6d78befac` | 360/360 完成 |
| `double_same_prompt` | gpt-6-luna | `ff97cb13a21665082ab0b62f95e016c6d78befac` | 360/360 完成 |
| `double_roles` | gpt-6-luna | `ff97cb13a21665082ab0b62f95e016c6d78befac` | 360/360 完成 |
| `single` 冒烟（5 条） | gpt-6.1-sol | `ff97cb13a21665082ab0b62f95e016c6d78befac` | 失败（参数不兼容），待决定 |
| `single` 冒烟与 360（仅方案 A） | gpt-6.1-sol | 待 [#70](https://github.com/baciozz/order-ops-superagent/pull/70) 合入后填写（合入时压缩为一个提交，与 ff97cb1 只差该提交） | 未跑；Qi 回复前不调用 |

若 Qi 选方案 A，强模型组在 #70 合入后的提交上跑；#70 合入时压缩为一个提交，与主模型三组所用的 ff97cb1 只差这一个提交（按模型配置 reasoning effort：sol 用 low，luna 保持 none）。这一点连同推理档不同带来的混杂，都要在强模型结果旁注明；强模型结果只作参考，不下结论。

## 7. 复现与产出

```
env -u OOS_LLM_API_KEY -u OPENAI_API_KEY OOS_LIVE_ENABLED=false \
  python -m benchmarks.rescore_comparison \
    --inputs reports/agent_comparison_v1/inputs \
    --ledger-db <ledger path> \
    --out evals/agent_comparison_v1/results
```

- 输入：`--inputs` 下的 `single/`、`double_same_prompt/`、`double_roles/`、`sol_smoke/`，每个目录放该组的 `state.json` 和 `summary.json`，都是采集时的原文件，不入库。账本以只读方式打开。
- 评分直接调用冻结的 `score_review.score`、`automatic_dangerous_*`、`handoff_recall_report`，本脚本只做按重复聚合和组间比较。
- 脚本还用冻结的 `build_comparison_summary` 从 state 重建 runner 摘要，与采集时的 `summary.json` 逐项比对（准确率、按重复、危险、澄清、按类、一致性、费用、转人工）。三组都**无不一致项**。
- 同一输入连跑两次，输出逐字节相同。
- 入库的产出：
  - `evals/agent_comparison_v1/results/rescore_summary.json`：全部指标、比较、账本、超时、敏感性和冒烟。
  - `observations_<组>.jsonl`：每条观测的脱敏结果，只保留处置、订单号、SKU 与数量、规则状态、得分、错误、token、费用、耗时。不含提示、样本原文、引用原文、澄清问题文本、模型摘要或 issue 文本。
- 未脱敏的 `state.json`、`observations.jsonl` 和日志都不入库。
