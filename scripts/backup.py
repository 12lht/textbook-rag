"""把 `data/` 的状态打包成一份可恢复的备份。可以反复跑，不影响正在运行的服务。


1. **正在写的 SQLite 直接拷文件，可能拷到半页。** 恢复时库是坏的，而且只有当
   你真正要用备份那天才发现——那时原始数据已经没了。这里每个库都走
   `sqlite3.Connection.backup()`（SQLite 自己的在线备份接口），
   拿到的保证是一个一致快照。
2. **`-wal` / `-shm` 边车文件怎么拷、什么顺序拷，很容易搞错。** 走 backup API
   之后它们根本不需要：数据已经合并进快照。
3. **两个 `*_secret.key` 必须跟数据一起走。** `asset_secret.key` 丢了，
   历史消息里的插图全部 403（签名再也验不过，只能重新解析教材）；
   `guest_secret.key` 丢了只是访客 cookie 失效，影响小但也别丢。


```bash
python scripts/backup.py

python scripts/backup.py --slim

python scripts/backup.py --keep 5
```


**先停服务**。SQLite 文件被进程占着时替换，轻则新版文件被忽略，重则下次启动
读到不一致的库。

```bash
docker compose stop app            # 或 systemctl stop agent
mv data data.broken
tar xzf backups/data-20261022-0100.tar.gz   # 解出来是一个 data/ 目录
docker compose start app
```

`data.broken` 别急着删：如果备份本身有问题，它就是最后的退路。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
BACKUP_DIR = ROOT / "backups"

DB_SUFFIXES = {".sqlite", ".sqlite3"}
SIDECAR_SUFFIXES = ("-wal", "-shm")
REGENERABLE_DIRS = {"assets", "uploads"}


def copy_sqlite(src: Path, dst: Path) -> None:
    """用 SQLite 的在线备份接口复制一个库。

    `mode=ro` 打开源库：备份是只读操作，不该给源库任何写机会
    （万一备份逻辑有 bug，也不会把生产库改坏）。

    注意**不能**写成 `with sqlite3.connect(...) as conn` ——那个 `with`
    只管事务、不管关闭连接，Windows 上会留下文件句柄。
    """
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def build_archive(slim: bool) -> Path:
    if not DATA_DIR.is_dir():
        raise SystemExit(f"没有数据目录：{DATA_DIR}")

    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = BACKUP_DIR / f"data-{stamp}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="agent-backup-") as tmp:
        stage = Path(tmp) / "data"
        stage.mkdir()

        for item in sorted(DATA_DIR.iterdir()):
            name = item.name
            if item.is_dir():
                if slim and name in REGENERABLE_DIRS:
                    print(f"  跳过 {name}/（--slim，可重新解析生成）")
                    continue
                shutil.copytree(item, stage / name)
                continue
            if name.endswith(SIDECAR_SUFFIXES):
                continue
            if item.suffix in DB_SUFFIXES:
                copy_sqlite(item, stage / name)
            else:
                shutil.copy2(item, stage / name)

        with tarfile.open(archive, "w:gz") as tar:
            tar.add(stage, arcname="data")

    return archive


def prune(keep: int) -> list[Path]:
    """只保留最近 `keep` 份。按文件名里的时间戳排（字典序即时间序）。"""
    if keep <= 0:
        return []
    archives = sorted(BACKUP_DIR.glob("data-*.tar.gz"), reverse=True)
    doomed = archives[keep:]
    for path in doomed:
        path.unlink()
    return doomed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="备份 data/ 状态")
    parser.add_argument(
        "--slim",
        action="store_true",
        help="跳过 assets/ 和 uploads/（可重新解析生成）",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=0,
        help="只保留最近 N 份备份（0 = 不清理）",
    )
    args = parser.parse_args(argv)

    print(f"备份源：{DATA_DIR}")
    archive = build_archive(args.slim)
    size_mb = archive.stat().st_size / 1024 / 1024
    print(f"完成：{archive}（{size_mb:.1f} MB）")

    removed = prune(args.keep)
    for path in removed:
        print(f"清理旧备份：{path.name}")

    print("\n恢复方法见本脚本顶部注释；**替换 data/ 前先停服务**。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
