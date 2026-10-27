"""探针：装饰图判定（几何三档 + 灰区问 VLM）。

背景：MinerU 的 image 块里混着两类东西——真插图（题注「图 2-5 尾递归过程」）和纯装饰
（课件模板的地球图标、蜂窝底纹、项目符号）。装饰图既不该占一个 chunk，也不该进 assets。

**判据只有一份**：直接调 `documents._figure_verdict`（keep / drop / ask），阈值不会两处漂移。
灰区（几何量判不了）在这里也会真的问一次模型，答案直接写进表格，方便核对。

金标只有一半：**有题注的一定是插图**（作者写了编号），没题注的两类都有。所以这里不判
「准确率」，只做两件事：
  1. 硬约束：带题注的图一张都不许被丢（误杀真插图是最贵的错）；
  2. 把图按判定导出成 HTML，人眼扫一遍就知道阈值该收紧还是放松。

样本选择：Hello-Algo 是纯教材，几乎没有装饰图（0 张是正常结果）；要验判据请指向课件 PDF，
例如 `--pdf "<课件.pdf>" --pages 1-30`（样本默认从库里取，textbook/ 已移出仓库）。

用法（仓库根目录，会消耗 MinerU 额度）：
    python tests/probes/probe-figure-filter.py
    python tests/probes/probe-figure-filter.py --pdf <课件.pdf> --pages 1-30
"""
from __future__ import annotations

import argparse
import html
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import documents  # noqa: E402
from _sample import sample_pdf  # noqa: E402
import figures  # noqa: E402
import mineru  # noqa: E402

OUT = REPO_ROOT / "compare" / "figure-filter"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def inline_caption(block: dict) -> str:
    for key in ("image_caption", "chart_caption"):
        items = (block.get("content") or {}).get(key) or []
        text = "".join(str(i.get("content", "")) for i in items if isinstance(i, dict)).strip()
        if text:
            return text
    return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="", help="样本 PDF；不给就从库里取 Hello-Algo")
    ap.add_argument("--pages", default="31-60")
    ap.add_argument("--no-ask", action="store_true", help="灰区不调模型，只标出来")
    args = ap.parse_args()
    ask = not args.no_ask and figures.available()

    start, _, end = args.pages.partition("-")
    first, last = int(start), int(end or start)
    src = pymupdf.open(args.pdf or sample_pdf("Hello-Algo"))
    sub = pymupdf.open()
    sub.insert_pdf(src, from_page=first - 1, to_page=last - 1)
    data = sub.tobytes()
    sub.close()
    src.close()
    print(f"样本：{src.name} 第 {first}-{last} 页（{len(data) / 1024 / 1024:.1f}MB）\n")

    workdir = Path(tempfile.mkdtemp(prefix="probe-fig-"))
    payload = mineru.parse(data, "sample.pdf", workdir, model="vlm")
    images_dir: Path = payload["images_dir"]

    if OUT.exists():
        mineru.purge_dir(OUT)  # 宿主对「一轮删 >50 文件」会杀进程，别用 rmtree
    for name in ("kept", "dropped"):
        (OUT / name).mkdir(parents=True, exist_ok=True)

    rows = []
    for block in payload["blocks"]:
        kind = block.get("type")
        if kind not in {"image", "chart"}:
            continue
        caption = inline_caption(block)
        rel = ((block.get("content") or {}).get("image_source") or {}).get("path")
        if not rel:
            continue
        src_file = images_dir / Path(rel).name
        if not src_file.is_file():
            continue
        with Image.open(src_file) as img:
            width, height = img.size
        area = documents._bbox_area(block.get("bbox"))
        verdict = documents._figure_verdict(width, height, area, caption)
        if caption:
            why = "有题注"
        elif min(width, height) < documents._DECOR_SHORT_SIDE and area < documents._DECOR_AREA_RATIO:
            why = f"短边 {min(width, height)}px + 占页面 {area:.1%}"
        elif max(width, height) / max(1, min(width, height)) >= documents._DECOR_ASPECT:
            why = f"长宽比 {max(width, height) / max(1, min(width, height)):.1f}"
        elif verdict == "ask":
            answer = figures.judge(src_file) if ask else None
            why = f"灰区 → 模型答 {answer}"
            verdict = "drop" if answer is False else "keep"
        else:
            why = "无题注但尺寸正常"
        drop = verdict == "drop"
        bucket = "dropped" if drop else "kept"
        target = OUT / bucket / f"{kind}-p{int(block.get('page_idx', 0)) + 1}-{Path(rel).name}.png"
        with Image.open(src_file) as img:
            img.convert("RGB").save(target, format="PNG")
        rows.append({
            "page": int(block.get("page_idx", 0)) + 1, "kind": kind,
            "width": width, "height": height, "area": area,
            "caption": caption, "drop": drop, "why": why, "file": target,
        })

    rows.sort(key=lambda r: (r["drop"], -r["area"]))
    print(f"{'页':>4} {'类型':<6} {'尺寸':>11} {'占页面':>7} {'判定':<4} 判据 / 题注")
    for r in rows:
        flag = "丢" if r["drop"] else "留"
        print(f"{r['page']:>4} {r['kind']:<6} {r['width']:>5}x{r['height']:<5} "
              f"{r['area']:>6.1%} {flag:<4} {r['why']}  {r['caption'][:34]}")

    dropped = [r for r in rows if r["drop"]]
    captioned = [r for r in rows if r["caption"]]
    print(f"\n共 {len(rows)} 张：留 {len(rows) - len(dropped)}、丢 {len(dropped)}；"
          f"有题注 {len(captioned)} 张")

    check("带题注的图一张都没被丢", all(not r["drop"] for r in captioned),
          f"{sum(1 for r in captioned if r['drop'])} 张被误杀")
    check("被丢的都有判据（几何或模型给的理由）",
          all(not r["drop"] or r["why"] != "无题注但尺寸正常" for r in rows))
    asked = [r for r in rows if r["why"].startswith("灰区")]
    print(f"灰区（问了模型）：{len(asked)} 张" if asked else "灰区（需要问模型）：0 张")

    cards = []
    for r in rows:
        rel_path = r["file"].relative_to(OUT).as_posix()
        tag = "丢" if r["drop"] else "留"
        cards.append(
            f'<figure class="card {"drop" if r["drop"] else "keep"}">'
            f'<img src="{rel_path}" alt="">'
            f'<figcaption><b>{tag}</b> p{r["page"]} {r["width"]}x{r["height"]} '
            f'{r["area"]:.1%}<br>{html.escape(r["why"])}<br>'
            f'<span class="cap">{html.escape(r["caption"][:40]) or "（无题注）"}</span>'
            f"</figcaption></figure>"
        )
    page = f"""<!doctype html><meta charset="utf-8"><title>装饰图判定</title>
<style>
body{{font:13px/1.5 system-ui,sans-serif;margin:24px;color:#222}}
h1{{font-size:16px;font-weight:500}}
.grid{{display:flex;flex-wrap:wrap;gap:14px}}
.card{{margin:0;width:190px;border:1px solid #ddd;border-radius:8px;padding:8px;background:#fff}}
.card.drop{{border-color:#c33;background:#fff6f6}}
.card img{{width:100%;height:130px;object-fit:contain;background:#fafafa;border-radius:4px}}
figcaption{{font-size:11px;color:#555;margin-top:6px}}
.cap{{color:#888}}
</style>
<h1>第 {first}-{last} 页：{len(rows)} 张图，丢 {len(dropped)} 张（红框 = 判定为装饰图）</h1>
<p>红框里如果不是装饰图，说明阈值太松；白框里如果是图标，说明太紧。</p>
<div class="grid">{"".join(cards)}</div>"""
    (OUT / "index.html").write_text(page, encoding="utf-8")
    print(f"\n逐张对照：{OUT / 'index.html'}")
    print(f"\n{'全部通过' if not failures else '失败：' + '、'.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
