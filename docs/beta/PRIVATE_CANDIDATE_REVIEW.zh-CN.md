# 私有无历史源码审查包

尚未批准公开、未创建tag/Release、没有部署。原开发仓库继续私有，Git历史、讨论及原始CI证据不随源码候选复制。审查不等于全部G0–G5通过。

`benchmarks/build_private_candidate.py`从干净、已提交的checkout按允许列表复制代码、测试、锁文件、必要技术文档及明确标为开发资料的v3合成包，生成候选专用README、当前变更说明和逐文件SHA256清单。它不复制.git、内部完整验收方案、历史交接、原始证据日志、数据库、运行凭据、浏览器下载/录屏、字体或OCR/模型权重。归档统一uid/gid、用户名、时间等元数据；两次构建已得到相同字节。包只保存在操作者自己的私有输出目录，不上传发布服务。

```bash
python benchmarks/build_private_candidate.py \
  --repo /absolute/clean-checkout \
  --output /absolute/private-review/new-candidate
```

初次在510ce29构建实际发现漏带evals/offline_cases.json，离线评测测试6失败，保留失败记录；补入合成evals允许列表后，候选目录中的架构引用、v3数据冻结及离线评测测试16项通过。普通wheel输入检查又复现漏带MANIFEST.in，已将其显式include依赖纳入打包闭包；从修正候选实际构建、独立venv安装及资源/迁移探针通过，不依赖源码editable安装。完整候选make check另行执行，不能预写成功或以原仓库测试替代。最终来源SHA、文件数、归档摘要和完整结果由交付时的MANIFEST与报告记录。

指定审计快照1e63f7c共165提交、125ref（含分支/PR头及既有tag）、1154可达blob，其中1153文本已扫描、1合成数据二进制未作通用嵌入内容扫描；历史有3个个人Git提交邮箱、内部路径和测试/示例凭据形状命中。GitHub另枚举61个PR+1Issue、全部已返回讨论、278运行/作业日志，0 Release/发布附件/Actions artifact。讨论有内部路径，日志未命中既定模式；没有宣称找得到已删除/过期或未advertise的数据。详见私有evidence/GIT_PUBLICATION_AUDIT及GITHUB_PUBLICATION_AUDIT，后续提交/CI不混入原快照结论。

候选文本扫描仍命中安全测试的合成凭据、示例URL邮箱及扫描器自身的路径正则；已经核对为测试/文档内容，不粘贴命中值。保留测试，不把扫描零命中当安全认证。实际客户数据/密钥从未用于此开发，仍需所有者检查拟公开内容及二进制生成链。

许可证盘点见THIRD_PARTY_NOTICES与依赖声明JSON：仓库已有MIT，但版权、贡献权利、维护者身份/私密报告入口及部分依赖license文件缺口未获所有者最终确认。不得因为打包脚本成功就直接公开。

最终阻塞保持：20故事及全部新风险独立真人参考复核、30条新隔离风险集与评分定义确认、正式模型/简单基线与可选公平单多Agent指标、陌生真人试用、3–5分钟合格演示、权利/维护者确认。现有42次真实调用和17条开发小批的临时15条一致不算正式准确率。新离线评分/隔离交接/只读双提取工具及录屏预检见EVALUATION_PROTOCOL，未替代这些门槛。本人独立讲解/现场修改也须由用户实际完成，见RESUME_EVIDENCE。应用迁移仍需停旧writer；新工作区有数据时拒绝破坏性回退，先备份，不自动认证旧库。
