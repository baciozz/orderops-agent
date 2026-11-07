# 本机离线备份与恢复演练

适用：受控单机、可信合成 SQLite 库、单个持久原件目录，以及待批准、已批准未提交或已提交的任务。全部使用本地文件；这不是在线备份、断电恢复、任意历史库迁移或生产备份方案。使用与备份相同的已验收源码/安装包，保持 LIVE 关闭。

**数据库可以恢复到新的运行目录，原件必须恢复到数据库原先记录的绝对路径。** `OOS_SOURCE_STORAGE_DIR` 只控制新上传；把数据库和原件都搬到另一根目录，或用符号链接代替旧原件路径，不是当前支持的迁移方式。不要修改 `input_source.storage_ref`、摘要、批准、租约或提交记录来强行放行。

先停止所有 API、worker-loop、worker-once、其他写者及计划任务，确认它们退出；备份、校验和恢复完成前均不再启动。下面的命令不能替你证明已经停写，不能与原实例同时运行恢复实例。恢复旧快照也不撤销快照之后已经发生的业务；本演练不接外部 ERP。

## 1. 明确本地路径并备份

在同一终端设置绝对路径。`PY` 使用已安装本项目的 Python；`LIVE_DB` 为现有库，`ORIGINALS` 为该库实际使用的持久目录。`SNAPSHOT` 须不存在，其父目录须已存在且由当前用户管理。不要把备份放在原件目录中。

```sh
umask 077
export PY='/绝对路径/已安装环境/bin/python'
export LIVE_DB='/绝对路径/原运行目录/demo.db'
export ORIGINALS='/绝对路径/原运行目录/originals'
export SNAPSHOT='/绝对路径/备份父目录/snapshot-01'
```

先人工核对这些路径。以下步骤仅覆盖**所有来源均在同一明确原件根目录**的演示库；发现旧临时来源、其他根目录、缺失、摘要不符或异常结构就停止，不跳过来源。多来源根的整理/迁移不在本演练内。任何异常都表示本次备份不完整；保留现场检查，换一个新的 `SNAPSHOT` 重做，不能覆盖半成品。

<!-- offline-backup-python -->
```sh
"$PY" -I -B - <<'PYTHON'
import hashlib, json, os, re, shutil, sqlite3, stat
from pathlib import Path
from contextlib import closing

def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()
def absolute(p):
    assert p.is_absolute() and p != Path('/') and '..' not in p.parts
    assert not any(x.is_symlink() for x in (p, *p.parents))
def copy_new(src, dst):
    with src.open('rb') as r, os.fdopen(os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as w:
        shutil.copyfileobj(r, w)
def sources(db):
    return [dict(zip(('id', 'storage_ref', 'size', 'sha256'), r)) for r in db.execute('SELECT id,storage_ref,size,sha256 FROM input_source ORDER BY id')]

source, root, out = (Path(os.environ[k]) for k in ('LIVE_DB', 'ORIGINALS', 'SNAPSHOT'))
for p in (source, root, out): absolute(p)
assert source.is_file() and root.is_dir()
assert stat.S_IMODE(root.stat().st_mode) == 0o700 and root.stat().st_uid == os.geteuid()
assert not out.exists() and out != root and root not in out.parents
out.mkdir(mode=0o700)
(out / 'originals').mkdir(mode=0o700)
os.close(os.open(out / 'demo.db', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as old, closing(sqlite3.connect(out / 'demo.db')) as copied:
    old.backup(copied)
with closing(sqlite3.connect((out / 'demo.db').as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
    assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    refs = sources(db)
for ref in refs:
    assert re.fullmatch(r'SRC-[0-9a-f]{32}', ref['id'])
    path = Path(ref['storage_ref'])
    assert path == root / ref['id'] and stat.S_ISREG(path.lstat().st_mode)
    assert type(ref['size']) is int and 0 <= ref['size'] <= 5 * 1024 * 1024
    assert path.stat().st_size == ref['size'] and digest(path) == ref['sha256']
    copy_new(path, out / 'originals' / ref['id'])
    assert digest(out / 'originals' / ref['id']) == ref['sha256']
manifest = dict(format=1, original_root=str(root), database_sha256=digest(out / 'demo.db'), sources=refs)
with os.fdopen(os.open(out / 'manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
print('离线备份完成：数据库完整性、外键和全部已记录原件摘要已核对。')
PYTHON
```

SQLite backup API 生成独立快照并关闭复制连接后，再以只读 immutable 模式核对该已完成的快照，避免校验产生临时 WAL/SHM 文件。不要对原始活动库使用 immutable 模式，否则可能忽略 WAL。该流程避免只复制裸 `.db` 而遗漏已提交 WAL 内容。仍必须先停写：此代码不提供数据库与活动原件之间的在线一致性保证。完整备份须有 `demo.db`、`manifest.json` 和清单中的所有 `originals/SRC-*`。清单、数据库和文件均须来自自己的可信本地备份；它不是签名或防篡改认证。

## 2. 恢复到新运行目录，保留原件原路径

保持全部旧进程停止。演练中先将整个旧运行目录移到一个新的保留位置，使旧原件不再能被原路径读取；不要删除它，不覆盖已有位置。这样能排除“恢复实例其实仍在读取原实例文件”的假成功。该移动只适用于自己创建的隔离合成目录；既有共用目录不要照搬。

`RESTORED` 为新的、不存在的运行目录。`ORIGINALS` 必须与清单的 `original_root` 完全相同，且当前不存在；其父目录若随旧目录一起移动，先人工核对后重新创建该空父目录（0700）。旧运行目录中的旧数据库应仍在保留位置，不能重新启动。

```sh
export RESTORED='/绝对路径/新运行父目录/rehearsal-01'
# ORIGINALS 保持备份时的原绝对路径，不能改成 "$RESTORED/originals"。
```

下面先核对全部备份，再独占创建目标，不覆盖任何已有文件。中途失败就停止检查，不能启动不完整的恢复实例；不要自动删除不明目标或继续重试覆盖。

<!-- offline-restore-python -->
```sh
"$PY" -I -B - <<'PYTHON'
import hashlib, json, os, re, shutil, sqlite3, stat
from pathlib import Path
from contextlib import closing

def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()
def absolute(p):
    assert p.is_absolute() and p != Path('/') and '..' not in p.parts
    assert not any(x.is_symlink() for x in (p, *p.parents))
def copy_new(src, dst):
    with src.open('rb') as r, os.fdopen(os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as w:
        shutil.copyfileobj(r, w)
def sources(db):
    return [dict(zip(('id', 'storage_ref', 'size', 'sha256'), r)) for r in db.execute('SELECT id,storage_ref,size,sha256 FROM input_source ORDER BY id')]

archive, root, dest = (Path(os.environ[k]) for k in ('SNAPSHOT', 'ORIGINALS', 'RESTORED'))
for p in (archive, root, dest): absolute(p)
assert not root.exists() and not dest.exists()
assert dest != root and dest not in root.parents and root not in dest.parents
manifest = json.loads((archive / 'manifest.json').read_text())
assert manifest['format'] == 1 and manifest['original_root'] == str(root)
assert stat.S_ISREG((archive / 'demo.db').lstat().st_mode)
assert digest(archive / 'demo.db') == manifest['database_sha256']
with closing(sqlite3.connect((archive / 'demo.db').as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
    assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert sources(db) == manifest['sources']
for ref in manifest['sources']:
    assert re.fullmatch(r'SRC-[0-9a-f]{32}', ref['id'])
    assert Path(ref['storage_ref']) == root / ref['id']
    path = archive / 'originals' / ref['id']
    assert stat.S_ISREG(path.lstat().st_mode)
    assert type(ref['size']) is int and 0 <= ref['size'] <= 5 * 1024 * 1024
    assert path.stat().st_size == ref['size'] and digest(path) == ref['sha256']
dest.mkdir(mode=0o700)
root.mkdir(mode=0o700)
copy_new(archive / 'demo.db', dest / 'demo.db')
assert digest(dest / 'demo.db') == manifest['database_sha256']
for ref in manifest['sources']:
    path = root / ref['id']
    copy_new(archive / 'originals' / ref['id'], path)
    assert digest(path) == ref['sha256']
print('离线恢复完成：数据库位于新目录，原件恢复到原路径；未改写数据库来源或业务记录。')
PYTHON
```

此流程没有 `UPDATE`、重新 seed、迁移或批准重置。所有目标文件以 0600 创建，目录为 0700。备份含同一快照的任务、输入、来源、批准和提交历史；恢复不会刷新批准到期时间、租约或重试预算。

## 3. 启动并核对任务

先核对当前只启动一个恢复实例，保留旧目录及备份。不要执行 `seed`，也不要对恢复库自动跑迁移；若当前安装版本无法通过结构检查，停止核对版本与备份，不绕过校验。

```sh
mkdir -m 700 "$RESTORED/home" "$RESTORED/tmp"
cd "$RESTORED"
env -i PATH=/usr/bin:/bin HOME="$RESTORED/home" TMPDIR="$RESTORED/tmp" \
  OOS_DATABASE_URL="sqlite:///$RESTORED/demo.db" OOS_SOURCE_STORAGE_DIR="$ORIGINALS" \
  OOS_LIVE_ENABLED=false "$PY" -I -B -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

确认服务启动后，从 `/ui/demo/tasks` 找回原 task ID，核对状态、订单差异、来源和批准记录：

- 待批准：核对原件和计划后明确批准、再单独提交。上传同键、同文件名及相同字节只返回原 `source_id`。
- 已批准未提交：保留原批准身份和原到期时间；仍有效时可按原提交请求继续。过期时先检查页面是否提供正常重新批准入口；满足当前条件才可续批，否则按提示重新规划或新建任务。不能改数据库时间；有依赖变化或来源异常则按提示处理。
- 已提交：先核对原 `operation_id` 和当前订单；若重放确认，使用原计划、批准及幂等键，应返回同一回执，不能新建任务再次执行已经生效的业务。
- 原件不可用时：即使提取片段可读也不能认为恢复成功；停止处理来源，不能把持久目录设置当成自动路径迁移。

## 4. 本轮实测与限制

以真实本地 Uvicorn HTTP 和合成 Excel 分别演练了待批准、已批准未提交、已提交三个快照。原实例完全停止并移到保留目录；恢复数据库在另一运行目录，原件从备份恢复到原绝对路径。找回同一任务和来源，继续审批/提交或重放，每个恢复库均只有一个新订单、一次 operation，模型调用为零，数据库完整性与外键检查通过。

另验证了批准过期后再恢复：旧提交返回 `EXPIRED`，原到期时间不变；从现有页面入口明确重新批准后提交成功，重复请求仍只有一次业务写入。

负对照把数据库和同名字节正确的原件一起搬到新根目录、仅改配置：页面禁用审批，上传重放返回 `SOURCE_UNAVAILABLE`，数据库不变。这是当前路径契约，不是完整路径迁移已经实现。

本说明中的两个 Python 代码块也在新的隔离合成库逐字执行，验证备份、拒绝覆盖、完整性失败停止和恢复后 HTTP 提交。没有验证在线备份、断电、硬件损坏、任意旧 schema、多根来源迁移、浏览器交互或生产恢复；限定核验仍为 `partial`，不是全库通过。备份和恢复演练目录均由操作者保留，不自动清理。
