"""跑一遍**全部离线回归**（不联网、不烧额度），有一条失败就退出码非 0。

为什么要有这个入口：项目没有 CI，探针全靠手动，`tests/README.md` 自己也写了
「签名漂移会让脚本几周后才报 TypeError」。这个脚本是「改完先跑它」的那条最短路径：

    python tests/run-offline.py            # 全部
    python tests/run-offline.py --list     # 只列会跑哪些
    python tests/run-offline.py vectors    # 只跑名字里含 vectors 的

判定口径：**子进程退出码 0 且输出里没有 FAIL**。探针自己打印的「跳过」也算失败——
静默跳过是之前踩过的坑（找不到 fixture 就 `exit(0)`，看起来像通过）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 只放**全离线**的：会调 MinerU / 视觉模型 / embedding 的探针不在这里（会花钱、也慢）
OFFLINE = [
    "probe-mineru-batches.py",   # 多批不串页、4xx 立即失败
    "probe-chunking.py",         # 空锚点段 / 超短碎片 / 超长代码块
    "probe-inline-math.py",      # 裸 LaTeX 包裹的边界
    "probe-vectors.py",          # 改 markdown 后向量必须失效
    "probe-astral-repair.py",    # U+FFFD 回填（表格 / 公式也要覆盖）
    # probe-markdown.py 不在这里：它会真解析一份 30 页 PDF（MinerU，要联网 + 花额度），
    # 不是离线回归。它验证的往返/重切在 probe-chunking + probe-vectors 里有离线覆盖，
    # 需要时单独跑：python tests/probes/probe-markdown.py
    "probe-upload.py",           # 上传闸门 / 去重 / 老库迁移 / 删除
    "probe-conversation-owners.py",
    "probe-asset-render.py",     # 签名 URL 取图 / 越权 / 过期（21s，偏慢但值得）
    "probe-cassette.py",
    "probe-token-estimate.py",
]


def main() -> int:
    # 探针输出里有 U+FFFD（就是回填测试的输入），Windows 控制台默认 GBK，
    # print 会 UnicodeEncodeError 把整个 runner 带崩（不是探针失败）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("pattern", nargs="?", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument(
        "--exclude",
        default="",
        help="逗号分隔的子串，命中的探针不跑。CI 用：那里没有 data/ 下的样本 PDF",
    )
    args = ap.parse_args()

    excluded = [part.strip() for part in args.exclude.split(",") if part.strip()]
    wanted = [
        name for name in OFFLINE
        if args.pattern in name and not any(skip in name for skip in excluded)
    ]
    missing = [name for name in wanted if not (REPO_ROOT / "tests" / "probes" / name).is_file()]
    wanted = [name for name in wanted if name not in missing]

    if args.list:
        for name in wanted:
            print(name)
        return 0

    results: list[tuple[str, bool, str]] = []
    for name in wanted:
        path = REPO_ROOT / "tests" / "probes" / name
        started = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            # 子进程的 stdout 是管道，默认按系统 locale（中文 Windows = GBK）编码。
            # 不强制 UTF-8 的话，打印 U+FFFD 的探针会自己 UnicodeEncodeError 退出 1，
            # 看起来像断言失败。
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
        seconds = time.perf_counter() - started
        output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        bad = [
            line.strip() for line in output.splitlines()
            if line.strip().startswith("FAIL") or "跳过" in line
        ]
        ok = proc.returncode == 0 and not bad
        results.append((name, ok, f"{seconds:.1f}s" + (f"　{bad[0][:60]}" if bad else "")))
        print(f"{'PASS' if ok else 'FAIL'}  {name:<32}{results[-1][2]}", flush=True)
        if not ok:
            tail = "\n".join(output.strip().splitlines()[-12:])
            print("     " + tail.replace("\n", "\n     "))

    if missing:
        print(f"\n（清单里这些文件不存在，已跳过：{', '.join(missing)}）")

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    if failed:
        print("失败：" + "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
