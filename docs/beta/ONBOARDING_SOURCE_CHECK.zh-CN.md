# 干净源码安装实测

版本1e63f7c7a83c3ac44289727d408956d01d56e489，Linux/Python3.12。用源码归档复制到全新目录、全新虚拟环境、单独HOME/TMP，无OOS/PYTHON开发设置；按锁定runtime/build依赖从本地预置wheelhouse安装，no-cache/no-index/no-build-isolation，再源码editable安装。wheelhouse自身是此前从许可包源下载的固定依赖，不把离线安装时间冒充首次互联网下载时间。

从无关运行目录执行app.cli init-db/seed，真实Uvicorn启动于127.0.0.1；HTTP导入合成快照、人工37、规则核对、明确确认、CSV/XLSX实际导出，所有已有业务表前后完全不变。pip check和安装后导入路径核对通过。不是独立真人、不是浏览器、也不是模型评测。普通wheel安装检查另见G2切片，不以源码安装替代wheel验证。

另按源码归档用户常用的accept_checklist.py命令实际发现：工作流和导出已成功，但最终读取Git元数据时因归档不含.git而整体报失败。修复仅使无Git元数据时保存实际文件哈希，code_sha/working_tree_dirty为null，source_provenance=file_hashes_only，不能伪造已验证SHA或干净状态。同一个已安装的源码归档按原命令复测通过；Git checkout路径仍保存真实SHA与dirty状态。失败输出保留，没有覆盖。

独立非开发者尚未按文档实际操作，原方案G4人工门槛保留。没有测试Windows/macOS、任意Linux发行版或首次系统OCR安装，不能宣称全平台支持。
