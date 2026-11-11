# Order Ops 接手交接

## 摘要

这是服装批发订单变更的**本机单用户开发预览**。用户导入订单、库存、已发货等材料，复核来源和字段，得到逐行差异及待办；可人工修正、确认并导出 CSV/XLSX。新清单入口不提交订单、不预占库存、不连接 ERP。旧 FIXTURE 演示另有批准/提交入口，二者不能混作真实业务执行。

本项目尚未完成正式模型效果、独立真人、真实业务或生产验收。不要部署到生产、不要打正式 tag 或创建 Release。未获用户新的配置确认前，不重试此前返回 401 的模型认证，不读取或输出凭据。

## 目录

1. [版本和同步](#版本和同步)
2. [实现与关键入口](#实现与关键入口)
3. [安装、离线演示和检查](#安装离线演示和检查)
4. [证据和未覆盖范围](#证据和未覆盖范围)
5. [备份、迁移和回退](#备份迁移和回退)
6. [下一步](#下一步)

## 版本和同步

- 代码候选：`4342ea8b3a05b1ef954df42aa45ee59f83c07790`，其历史包含冻结候选 `0b4984e32666d10dbfffa3b91344b2ddea53644a`。原 [PR #62](https://github.com/baciozz/order-ops-superagent/pull/62) 头为 `d887ed8679fa830f8672a4ec935a54725ae98023`；本次继续其现有历史，而非从源码压缩包重建历史。
- 同步前 main 是 `4f0536c50d95fc77d0bb2fa4f8fc8100ffcede90`，已包含 [PR #61](https://github.com/baciozz/order-ops-superagent/pull/61)。实际合并状态、最终 main SHA 和精确提交的 CI 请查看 [PR #62](https://github.com/baciozz/order-ops-superagent/pull/62)、[main 历史](https://github.com/baciozz/order-ops-superagent/commits/main/)及其检查；本文件不应预填包含它自身的未来提交哈希。合并后的私人交接副本另写实际 SHA。
- [PR #60](https://github.com/baciozz/order-ops-superagent/pull/60) 是独立 MTO 数量缩减草稿，头 `1de016e54669a8c98877cec738ff025c1b469746`，不在上述候选历史中。不要假定它已合并，也不要未经独立审核把它混入本次工作。
- GitHub 集成读取 main 的分支保护详情曾返回 403；不能据此断言保护规则的内容。只使用正常 PR 合并路径，由 GitHub 执行其实际规则，不绕过拒绝。

## 实现与关键入口

- 产品说明和入口：[README](../README.md)、[本机使用说明](LOCAL_BETA_GUIDE.zh-CN.md)、[离线合成示例](OFFLINE_WALKTHROUGH.zh-CN.md)。新手人工示例在 `app/web/templates/review_new.html` 和 `review_detail.html` 提示操作。
- 真实材料读取与来源位置：`app/review/documents.py`、`reader_worker.py`、`assets.py` 和 `app/api/review_sources.py`。支持范围、大小上限及不能读取的格式见[输入支持](beta/INPUT_SUPPORT.zh-CN.md)。不要根据文件名猜角色或把未知数量当零。
- 确定性规则及人工工作区：`app/review/rules.py`、`app/review/service.py`、`app/api/reviews.py`。PDF 行位置和零差量审计、人工动作与确认失效、图片尺码/中文/数字复核由对应测试覆盖；规则过滤后的清单不能掩盖模型原始提案的错误。
- 数字复核回退副本：`scripts/prepare_numeric_rollback_copy.py`，覆盖重复回退/升级、原件副本校验和目标预检。它不直接修改来源库，不是通用数据库迁移工具。
- 单/双读取器的比较适配在 `benchmarks/compare_readers.py`；双调用不是已投产的完整多代理系统，也未证明速度或质量优势。[评测方案](beta/AGENT_COMPARISON.zh-CN.md)要求同数据、模型、工具和预算的对照，并报告人工裁决、危险错误、耗时和成本。

## 安装、离线演示和检查

已验证平台是 Linux、Python 3.12；其他系统、触屏和多人使用没有完整验收。以下只用新建的合成资料目录，依赖首次安装可能需要网络：

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
```

随后严格按[无需模型的合成示例](OFFLINE_WALKTHROUGH.zh-CN.md)从新的空目录启动；不要在已有业务库初始化，也不要从含 `.env` 的工作目录启动离线演示。演示清空继承环境、只监听 `127.0.0.1`，不需要密钥或外网。打开 `/ui/reviews`，保留预填 `SYN-2401`，人工选择 `00123 · 藏蓝 · M`、总量 36，核对 **50→36，差量 −14，已发 10**，确认并分别下载 CSV/XLSX。人工来源和“未执行订单”必须可见；重启后应能恢复查看。完整启动与失败处理以该文档为准。

```sh
# 安装 requirements-dev.lock 后；不得把保留集测试重新加入普通运行。
PYTEST_ADDOPTS="--ignore=tests/test_beta_dataset.py" OOS_LIVE_ENABLED=false make check
OOS_LIVE_ENABLED=false make eval-offline
# provisioned wheelhouse 为事先准备的锁定依赖目录；输出目录须不存在且位于源码外。
make check-wheel WHEELHOUSE="<wheelhouse>" WHEEL_OUTPUT="<new-evidence-directory>"
```

保留集不随本仓库公开。对外只公布冻结文件的 SHA256 作为承诺：`b93d35c2b02533a10f3ce1e6543c12acbd4a2bc3d738b59d8b61d1d29bad4f43`。本仓库不含 `benchmarks/datasets/` 与 `tests/test_beta_dataset.py`。普通 CI 不持有、不读取保留集，也**不验证数据集完整性**。不要把受保护内容加入本仓库。持有私有副本的独立维护者按[保留集协议](beta/HELDOUT_INDEPENDENCE.zh-CN.md)在隔离条件下安排后续工作。

实际中文 OCR 另需 Tesseract 及固定版本中文语言数据，安装和测试方法见[输入支持](beta/INPUT_SUPPORT.zh-CN.md)；缺少可选数据时应报告跳过，不能把 mock 或跳过写成真实 OCR 通过。`make eval-offline` 测的是固定答案下的管线与规则，不是自由语言理解准确率。

## 证据和未覆盖范围

- `4342ea8` 在 Linux/Python 3.12 的独立源码和普通 wheel 环境，用 Chromium/Playwright 自动操作了上述合成示例、确认、两种导出读回和重启恢复；38 张业务表摘要不变，模型调用为 0。不是独立真人试用或模型效果。私有离线交付包 `libfile_4591a4da6a8c819186f28c7899b1ab96` 保存源码、wheel 和原始证据；它不含受保护数据集。
- 先前 `6367e5a` 的相关页面/安全回归为 188 项通过，后续到 `4342ea8` 仅有文档文字修改；这些结果属于当时提交，不替代最终提交的检查。更早 `45da4bd` 的完整分片测试也只能归属于 `45da4bd`。本次 PR 的精确 head 请以其实际 CI 与合并后交接副本为准。
- 主线程在另一环境独立报告干净源码/wheel 安装以及 HTTP 人工录入、确认、导出和重启通过；该环境受 socket/ptrace 限制，浏览器未验证。原环境曾有一次照片自动化断言失败，缺少完整中间证据，后续未重现，根因仍未知；见[当前限制](LOCAL_BETA_LIMITATIONS.zh-CN.md)。
- 旧真实模型开发实验使用过合成材料。`738c909` 两个已调优案例各复测三次：换款数量三次符合，PDF 两次仍多余澄清，一次有效差异正确但原始动作含零差量冗余。参考答案尚未独立真人复核，具体模型快照未知，不能转成正式准确率；人工纠正也不是模型成功。
- 图片 OCR 曾把中文颜色误读；低分提示、尺码差异检查和数字勾选都不是准确率保证，可能误拦或漏错。即使无提示，也须对照原图核对款色码、编号、数量、金额、日期及漏读；不确定时完整转录。勾选不能证明识别正确或确有真人。
- 历史风险样本已被开发过程暴露，不能改名当盲测。独立风险集、参考答案真人复核、正式基线/多代理对照、全格式真实模型验收和陌生用户试用均未完成。不要把 fixture、脚本点击、少数开发案例或来源哈希当这些验收的替代品。

## 备份、迁移和回退

先停止所有 API、worker 和其他写者，分别保留可信的 SQLite 一致性备份及原件/预览，再按[新清单停服备份](REVIEW_OFFLINE_BACKUP.zh-CN.md)核对大小、哈希和引用。恢复时数据库可在新目录，原件须回到已记录的原绝对路径；单改 `OOS_SOURCE_STORAGE_DIR` 不会迁移旧引用。混合旧入口的库还要参考[旧入口备份](OFFLINE_BACKUP_RESTORE.zh-CN.md)。这些流程不承诺在线备份、断电恢复或任意路径迁移。

需要从数字复核候选回退到指定旧版 `3d4bcd6e555962b287cba70dba719873a2db6084` 时，按[回退步骤](beta/INPUT_SUPPORT.zh-CN.md)对**副本**执行工具，校验数据库、原件和图片预览后在隔离环境核对。该验证仅针对文档所述 SQLite/版本组合；旧版没有数字复核门槛，重新升级后需重新核对图片。源备份及回退期间的新工作都应保留，不覆盖现有目标。其他数据库、版本、生产部署未验证。

清单工作区使用 `local_review_workspace` Alembic 迁移；升级前须停写并备份。非空清单不能当成可无损降级，历史确认和费用账本不能通过删除来重置。不要把回退工具视为所有迁移的逆操作。

## 下一步

1. 先读 PR 的最终合并和 CI 状态、对应提交与本交接副本；若同步、CI 或合并未成功，先解决具体阻碍，不能宣称已完成。
2. 等用户安全确认模型环境绑定后，再按现有预算账本、隔离和授权范围做有界认证及真实模型测试；不要让凭据进入日志、仓库、浏览器截图或其他服务。真实模型工作结束后提醒用户撤销专用密钥。
3. 由独立人员建立新的隔离风险样本并冻结，安排业务人员审核开发/验证参考及全部新风险答案，明确评分与可追问事实。先解决数据和真人门槛，再做正式 G1/G3 测试。
4. 在相同输入、模型、工具、基线快照和预算下，预先定义并重复运行单次、等调用次数自检及双角色对照；记录所有故事的可用结果、危险错误、漏报、多余澄清、人审负担、耗时和费用。不要只报告双方一致的子集或预设多代理更优。
5. 后续处理 PDF 多余澄清、图片错读、尚未定位的浏览器断言及格式覆盖，使用独立样本、真实浏览器和明确分母；不要对已暴露保留样本调参后再称其独立测试。

关键链接：[PR #62](https://github.com/baciozz/order-ops-superagent/pull/62)、[PR #60](https://github.com/baciozz/order-ops-superagent/pull/60)、[评测方案](beta/EVALUATION_PROTOCOL.zh-CN.md)、[项目进度](beta/PROGRESS.zh-CN.md)、[当前限制](LOCAL_BETA_LIMITATIONS.zh-CN.md)。
