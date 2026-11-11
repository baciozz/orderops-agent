# 新清单工作区的本地停服备份

只适用于本地SQLite清单工作区。先停止全部API、worker和其他写者；脚本无法自动证明已经停写，`--all-writers-stopped`只是操作者对前置条件的明确确认。不提供在线一致性、断电恢复、加密备份或异地灾备承诺。

数据库含原文、快照、提案、人工历史和费用账本；原件/图片预览是另一组文件。备份和manifest均留在操作者自己的私有目录，不发送第三方，不放进Git。保留可信本地备份，清单哈希不是签名认证。

## 备份

在仓库根目录，替换为自己隔离试用目录的绝对路径。`originals`默认是数据库旁的`review-originals`，若曾设置`OOS_SOURCE_STORAGE_DIR`则使用实际目录。archive必须不存在，父目录应预先设为0700。

```bash
.venv/bin/python scripts/offline_review_backup.py backup \
  --database /absolute/private-preview/review.db \
  --originals /absolute/private-preview/review-originals \
  --archive /absolute/private-backups/snapshot-001 \
  --all-writers-stopped
```

脚本以SQLite backup API复制已提交WAL内容，在关闭后的副本上检查完整性；枚举review_workspace全部当前原件和图片预览引用并核对大小/哈希，再复制原件目录的所有RVO文件（含未引用文件），复核副本。完成须退出0并显示backup=verified，且保留review.db、manifest.json和originals目录。失败的目录可能不完整，不能恢复；不覆盖旧备份或自动删除失败文件。

该脚本拒绝符号链接、混用多个原件根目录、非RVO文件以及已有旧演示input_source记录的库。混合旧业务库须额外采用[旧入口备份步骤](OFFLINE_BACKUP_RESTORE.zh-CN.md)，不能只保存新清单原件便声称全库备份。

## 恢复和启动

仍须全部停服。先把损坏/当前数据库及原件目录移动到明确的保留位置，不删除、覆盖或手改存储路径。数据库可以恢复到新运行目录；**原件必须回到manifest记录的原绝对目录**，它必须不存在，且父目录存在。更改OOS_SOURCE_STORAGE_DIR不会迁移旧引用；若原路径不能安全恢复，停止并调查，不用符号链接绕过。

```bash
.venv/bin/python scripts/offline_review_backup.py restore \
  --database /absolute/restored-run/review.db \
  --originals /absolute/private-preview/review-originals \
  --archive /absolute/private-backups/snapshot-001 \
  --all-writers-stopped
export OOS_DATABASE_URL="sqlite:////absolute/restored-run/review.db"
export OOS_SOURCE_STORAGE_DIR="/absolute/private-preview/review-originals"
export OOS_LIVE_ENABLED=false
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

只从自己的可信备份恢复。脚本在写目标前复核数据库、原件和引用，不覆盖现有目标；恢复失败保留源备份并检查部分输出，不直接启服。成功后从/ui/reviews找回任务，核对已确认/待复核/已取消状态，打开原件再导出检查；不要重建相同任务或删除费用账本来重置调用限额。

## 本轮实际演练和清理

应用1e63f7c、合成库停服：12份原件/预览全部备份。未确认停写、覆盖现有原件根、损坏备份分别拒绝；可信副本恢复到新数据库目录，原件回原路径，重启后找回CONFIRMED/CANCELLED/NEEDS_REVIEW，真实HTTP导出38/-12，数据库全部表行与停服前一致。普通浏览器复查单列于浏览器验收记录，不以HTTP替代它。

没有自动清理。仅丢弃自己创建的合成试用资料时，可停服后把整个隔离运行目录、原件和备份一起移动到归档位置，确认不再有任务引用后由操作者决定保留或删除；不要只删原件、上传目录或账本。真实资料保留期限与备份访问者须由所有者制定，本项目不擅自选择。
