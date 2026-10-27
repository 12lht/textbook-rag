"""MinerU 云端 PDF 解析探针：速度 + 质量，对照本地 PyMuPDF 管线。

跑法（仓库根目录）：
    python tests/probes/probe-mineru.py                       # 默认 Hello-Algo 1-40 页，pipeline
    python tests/probes/probe-mineru.py --pages 1-40 --model vlm
    python tests/probes/probe-mineru.py --pdf textbook/xxx.pdf --pages 1-200 --model vlm --no-local

只打印数据、不下断言：解析质量的结论要靠人看 md 片段。

为什么要先裁页：云端精准解析限 200 页 / 200MB，本地管线不限；裁成同一个页范围
两边才可比（否则就是「解析 40 页 vs 解析 348 页」的假对比）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

import requests  # noqa: E402

BASE = "https://mineru.net/api/v4"
OUT_ROOT = REPO_ROOT / ".workbuddy" / "tmp" / "mineru"

def _headers() -> dict:
    token = os.getenv("MINERU_API_KEY", "").strip()
    if not token:
        raise SystemExit("MINERU_API_KEY 没配（.env）")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _sub_pdf(path: Path, pages: str) -> bytes:
    """按 "1-40" / "2,4-6" 裁出子 PDF 的字节。"""
    import pymupdf

    doc = pymupdf.open(path)
    wanted: list[int] = []
    for part in pages.split(","):
        part = part.strip().replace("--", "-")
        if "-" in part:
            lo, hi = part.split("-", 1)
            wanted.extend(range(int(lo), int(hi) + 1))
        elif part:
            wanted.append(int(part))
    sub = pymupdf.open()
    for page_no in wanted:
        sub.insert_pdf(doc, from_page=page_no - 1, to_page=page_no - 1)
    data = sub.tobytes()
    sub.close()
    doc.close()
    return data


def submit_and_wait(pdf_bytes: bytes, filename: str, model: str, pages: str) -> tuple[dict, dict]:
    """申请上传链接 → PUT → 轮询。返回 (marker 时间字典, 结果)。"""
    marks: dict[str, float] = {}
    t0 = time.perf_counter()

    payload = {
        "enable_formula": True,
        "enable_table": True,
        "language": "ch",
        "model_version": model,
        # 不传 page_ranges：文件已经裁过了，云端再按原页码找会报越界
        "files": [{"name": filename, "data_id": "probe"}],
    }
    resp = requests.post(f"{BASE}/file-urls/batch", headers=_headers(), json=payload, timeout=60)
    body = resp.json()
    if body.get("code") not in (0, None):
        raise SystemExit(f"申请上传链接失败：{body}")
    batch_id = body["data"]["batch_id"]
    upload_url = body["data"]["file_urls"][0]
    marks["apply"] = time.perf_counter() - t0

    t1 = time.perf_counter()
    # 上传链接是预签名的，带 Content-Type 会破坏签名
    put = requests.put(upload_url, data=pdf_bytes, timeout=600)
    put.raise_for_status()
    marks["upload"] = time.perf_counter() - t1

    t2 = time.perf_counter()
    result: dict = {}
    last_state = ""
    progress_note = ""
    while True:
        time.sleep(2)
        poll = requests.get(
            f"{BASE}/extract-results/batch/{batch_id}", headers=_headers(), timeout=60
        ).json()
        items = (poll.get("data") or {}).get("extract_result") or []
        if not items:
            raise SystemExit(f"查询结果异常：{poll}")
        item = items[0]
        state = item.get("state", "")
        if state != last_state:
            progress_note += f" {state}@{time.perf_counter() - t2:.1f}s"
            last_state = state
        if state in {"done", "failed"}:
            result = item
            break
        if time.perf_counter() - t2 > 900:
            raise SystemExit(f"轮询超时，最后状态 {state}")
    marks["parse"] = time.perf_counter() - t2
    marks["parse_trace"] = progress_note  # type: ignore[assignment]

    if result.get("state") == "failed":
        raise SystemExit(f"云端解析失败：{result.get('err_msg')}")

    t3 = time.perf_counter()
    zip_bytes = requests.get(result["full_zip_url"], timeout=600).content
    marks["download"] = time.perf_counter() - t3
    marks["total"] = time.perf_counter() - t0
    marks["zip_mb"] = round(len(zip_bytes) / 1024 / 1024, 2)

    out_dir = OUT_ROOT / f"{Path(filename).stem}-{model}-{pages.replace(',', '_')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not (out_dir / "full.md").exists():
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            archive.extractall(out_dir)
    result["local_dir"] = str(out_dir)
    return marks, result


def mineru_stats(out_dir: Path) -> dict:
    """云端产物的量化指标。"""
    md = (out_dir / "full.md").read_text(encoding="utf-8", errors="replace")
    images = list((out_dir / "images").glob("*")) if (out_dir / "images").is_dir() else []
    import re

    # 行间公式 $$...$$ 与行内公式 $...$
    display_math = len(re.findall(r"\$\$[\s\S]+?\$\$", md))
    inline_math = len(re.findall(r"(?<!\$)\$(?!\$)[^$\n]{2,}?\$(?!\$)", md))
    return {
        "md_chars": len(md),
        "md_kb": round(len(md.encode()) / 1024, 1),
        "lines": md.count("\n"),
        "images": len(images),
        "headings": len(re.findall(r"^#{1,6} ", md, re.M)),
        "display_math": display_math,
        "inline_math": inline_math,
        "md_tables": len(re.findall(r"^\|", md, re.M)),
        "md_path": str(out_dir / "full.md"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="", help="样本 PDF；不给就从库里取 Hello-Algo")
    ap.add_argument("--pages", default="1-40")
    ap.add_argument("--model", default=os.getenv("MINERU_MODEL", "vlm"),
                    choices=["pipeline", "vlm"], help="默认跟 .env 的 MINERU_MODEL（vlm）")
    ap.add_argument("--project-chunks", action="store_true",
                    help="额外跑一遍本项目的转换+切分（用已下载的产物，不额外花额度）")
    ap.add_argument("--out", default="", help="产物目录，默认 .workbuddy/tmp/mineru")
    args = ap.parse_args()

    if args.out:
        global OUT_ROOT
        OUT_ROOT = Path(args.out)
        if not OUT_ROOT.is_absolute():
            OUT_ROOT = REPO_ROOT / OUT_ROOT
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _sample import sample_pdf

    pdf_path = Path(args.pdf) if args.pdf else sample_pdf("Hello-Algo")
    if not pdf_path.is_absolute():
        pdf_path = REPO_ROOT / pdf_path

    print(f"样本：{pdf_path.name} 页范围 {args.pages} 模型 {args.model}")
    pdf_bytes = _sub_pdf(pdf_path, args.pages)
    print(f"子 PDF：{len(pdf_bytes) / 1024 / 1024:.2f} MB\n")

    print("=== MinerU 云端 ===")
    marks, result = submit_and_wait(pdf_bytes, pdf_path.name, args.model, args.pages)
    out_dir = Path(result["local_dir"])
    for key in ("apply", "upload", "parse", "download", "total"):
        print(f"  {key:<9}{marks[key]:>8.1f}s")
    print(f"  zip      {marks['zip_mb']:>8.2f} MB")
    print(f"  状态轨迹 {marks['parse_trace']}")
    cloud = mineru_stats(out_dir)
    for key, value in cloud.items():
        print(f"  {key:<14}{value}")

    if args.project_chunks:
        # 用**已经下载的** v2 json 走本项目的转换链路，不再调一次云端（以前这里会双倍烧额度）
        import documents

        print("\n=== 本项目转换 + 切分（复用上面的产物）===")
        v2 = sorted(out_dir.glob("*content_list_v2.json"))
        if not v2:
            print("  没有 v2 json，跳过")
        else:
            blocks = []
            for page_index, page_blocks in enumerate(
                json.loads(v2[0].read_text(encoding="utf-8"))
            ):
                for block in page_blocks:
                    block = dict(block)
                    block["page_idx"] = page_index
                    blocks.append(block)
            payload = {"blocks": blocks, "images_dir": out_dir, "page_count": 0}
            parsed = documents._elements_from_mineru(payload, out_dir / "assets")
            markdown = documents.render_markdown(parsed.elements)
            chunks = documents.cut_chunks(markdown, "probe")
            print(f"  元素 {len(parsed.elements)}　图 {len(parsed.assets)}　"
                  f"段落 {len(chunks)}　字符 {len(markdown)}")
            (out_dir / "project.md").write_text(markdown, encoding="utf-8")
            print(f"  md {out_dir / 'project.md'}")

    print("\n产物：")
    print(f"  md  {cloud['md_path']}")
    print(json.dumps({"cloud_marks": {k: v for k, v in marks.items() if k != "parse_trace"}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
