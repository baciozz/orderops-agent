# 合成评测资料 v3（人工待复核，不是已通过的模型评测）

本仓库不公开保留集（`provisional-v3.FREEZE.json` 与压缩包均不在本仓库中）。对外只公布 `provisional-v3.FREEZE.json` 的 SHA256 作为承诺：`b93d35c2b02533a10f3ce1e6543c12acbd4a2bc3d738b59d8b61d1d29bad4f43`。持有私有副本的人可用该哈希核验完整性。下列说明保留生成方法与覆盖范围，不代替发布该资料包。

原先私有资料包包含120个独立合成故事、每故事3种表达共360功能样本；故事分区60/20/40，样本180/60/120。保留集中文文字18、聊天18、粘贴/Markdown17、CSV17、XLSX16、PDF17、图片17。另有60风险案例分20/10/30。均为模板与确定性状态程序生成，语言多样性有限，不能外推到真实客户或市场准确率。

状态先生成，再渲染实际PDF/Excel/像素。数字包含前导零、款色码、显式零量组合、未知事实、库存不足、部分已发/已裁、数量语义、局部修改、撤回、冲突、询价和单位不明；风险含提示注入、错单、已履约、未知SKU/库存/单位、负总量及整单取消。尺码横排/颜色矩阵订单表只承载订购量，已发/已裁/库存等事实保留在明确的baseline.json，不把缺列当零。三种布局均是实际XLSX，非以扩展名冒充。

目录隔离：`public_inputs`只有应用可读取的业务快照和原件；`private_oracle`保存参考处理、期望数量、必要澄清、禁止动作及渲染底稿。外层清单中的case_id/story_id/split只用于评测，不能发给应用或模型。读取工具`benchmarks.read_inputs`只打开清单指定的public资产，并只把无答案的`request.*`文件名交给reader；应用没有导入benchmarks。评分器必须在收集应用输出之后独立读取oracle，不能用答案纠正OCR再报自动效果。

目前只有独立算术、快照结构、故事分区/哈希与应用规则一致性通过；**人工复核尚未完成**。`HUMAN_REVIEW_PENDING.json`列20个覆盖多类别的开发/验证故事及全部30个保留风险，reviewer为空，不能由AI签署。参考答案允许追问，不能用措辞逐字匹配；“未执行订单”是本清单入口的实际边界，expected_post_commit_state=null明确说明没有业务提交接口，不能冒充ERP执行评测。

保留集不随本仓库公开，只公布 `provisional-v3.FREEZE.json` 的 SHA256 作为承诺：`b93d35c2b02533a10f3ce1e6543c12acbd4a2bc3d738b59d8b61d1d29bad4f43`。该文件是人工待复核的机器候选冻结；压缩包另有独立哈希，但不在本仓库公布该文件本身。同一生成器、锁定依赖及同一字体实际重建两次，全部文件哈希一致。字体只记录SHA，不打包；图像使用本机NotoSansCJK，OCR权重也不在资料包中。公开后不称秘密测试集；当前尚未以保留集真实模型结果调优。若后续据保留失败修改实现，相应故事须移为回归并补新故事、重新冻结，不能挑最好结果。

源码环境复现（仅限持有私有保留集副本的维护者；输出目录必须不存在）。本仓库不含保留集压缩包，也不含 `tests/test_beta_dataset.py`：

```bash
.venv/bin/python -m benchmarks.build_dataset --output ./synthetic-v3 \
  --font /本机已获许可的字体/NotoSansCJK-Regular.ttc
.venv/bin/python -m benchmarks.validate_dataset ./synthetic-v3
.venv/bin/python -m benchmarks.read_inputs ./synthetic-v3 --output ./reader-evidence-v3 \
  --ocr-data-dir "$PWD/data/ocr"
```

已有压缩包只应在私有隔离目录解开；不要解入运行数据库或原件目录，也不要提交回本仓库。`validate_dataset`读取oracle只作离线标签审核，不是应用提取。`read_inputs`真实读取420件，但不计算理解准确率；读取成功不等于关键字段OCR正确。普通CI只跑冻结结构与规则一致性，不持有付费密钥。真实模型对照/稳定性/风险重复评测、完整来源命中评分、独立人工复核仍待完成。v1/v2是生成阶段原型，未作为最终评测结果，失败与变更记录保留在本地证据中。
