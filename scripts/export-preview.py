"""把库里已解析好的 Markdown 导出成人能看的预览：**不重新解析、不烧任何额度**。

给每本书出三样东西：
    out/preview/<书名>/doc.md     原始 Markdown（VS Code 里直接预览也行）
    out/preview/<书名>/doc.html   渲染后（marked + KaTeX，公式和表格都是真效果）
    out/preview/<书名>/images/    这本书的图

外加一个 `out/preview/index.html` 汇总页。默认只给 4 本有代表性的书出 HTML（带图最占地方），
其余的书只导出 .md，用 `--all` 全出。

用法（仓库根目录）：
    python scripts/export-preview.py                 # 4 本代表 + 全部 .md
    python scripts/export-preview.py --all           # 全部出 HTML
    python scripts/export-preview.py --only Hello-Algo 隐马尔科夫
"""
from __future__ import annotations

import argparse
import html
import shutil
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import documents  # noqa: E402

OUT = REPO_ROOT / "out" / "preview"
VENDOR = REPO_ROOT / "frontend" / "vendor"
PICK = ["Hello-Algo", "11_隐马尔科夫模型-I", "通信网理论基础习题集", "15_基因表达数据分析"]

PAGE = """<!doctype html><html lang="zh"><meta charset="utf-8">
<title>{title}</title>
<link rel="stylesheet" href="../vendor/katex/katex.min.css">
<style>
body{{max-width:900px;margin:0 auto;padding:32px 24px 120px;
font:16px/1.85 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#1c1c1e}}
h1,h2,h3{{line-height:1.35;margin:1.6em 0 .6em;font-weight:500}}
h1{{font-size:26px}} h2{{font-size:21px}} h3{{font-size:18px}}
p{{margin:.8em 0}}
img{{max-width:100%;height:auto;border:1px solid #e5e5ea;border-radius:6px;margin:.6em 0}}
table{{border-collapse:collapse;margin:1em 0;font-size:14px;display:block;overflow-x:auto}}
td,th{{border:1px solid #dcdce1;padding:6px 10px}}
th{{background:#f5f5f7}}
pre{{background:#f5f5f7;padding:12px 14px;border-radius:6px;overflow-x:auto;font-size:13px}}
code{{font-family:ui-monospace,Consolas,monospace;font-size:.92em}}
blockquote{{border-left:3px solid #dcdce1;margin:1em 0;padding:.2em 0 .2em 14px;color:#555}}
hr{{border:0;border-top:1px solid #e5e5ea;margin:2em 0}}
.bar{{position:sticky;top:0;background:#fff;border-bottom:1px solid #e5e5ea;
padding:10px 0;margin-bottom:12px;font-size:13px;color:#666}}
</style>
<div class="bar">{title}　·　{meta}　·　<a href="../index.html">返回目录</a></div>
<article id="view"></article>
<script type="text/plain" id="md">{markdown}</script>
<script src="../vendor/marked.min.js"></script>
<script src="../vendor/purify.min.js"></script>
<script src="../vendor/katex/katex.min.js"></script>
<script src="../vendor/katex/auto-render.min.js"></script>
<script>
const raw = document.getElementById('md').textContent;
const view = document.getElementById('view');
view.innerHTML = DOMPurify.sanitize(marked.parse(raw), {{ADD_TAGS:['annotation','semantics']}});
view.querySelectorAll('img').forEach(img => {{ img.loading = 'lazy'; }});
renderMathInElement(view, {{
  delimiters: [{{left:'$$',right:'$$',display:true}}, {{left:'$',right:'$',display:false}}],
  throwOnError: false
}});
</script>
</html>"""

INDEX = """<!doctype html><html lang="zh"><meta charset="utf-8">
<title>解析成品预览</title>
<style>
body{{max-width:860px;margin:0 auto;padding:40px 24px;
font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#1c1c1e}}
h1{{font-size:22px;font-weight:500;margin-bottom:4px}}
p.sub{{color:#666;margin-top:0}}
table{{border-collapse:collapse;width:100%;margin-top:20px;font-size:14px}}
td,th{{border-bottom:1px solid #e5e5ea;padding:9px 8px;text-align:left}}
th{{font-weight:500;color:#444;border-bottom:1px solid #c7c7cc}}
tr:hover td{{background:#fafafa}}
a{{color:#0b5cd5;text-decoration:none}}
a:hover{{text-decoration:underline}}
.muted{{color:#999;font-size:13px}}
</style>
<h1>解析成品预览</h1>
<p class="sub">共 {count} 本 · {pages} 页 · {chunks} 段 · {assets} 张图　<span class="muted">（{html_count} 本可点开看渲染效果）</span></p>
<table><tr><th>书名</th><th>页</th><th>段</th><th>图</th><th>公式</th><th>表格</th><th>代码</th></tr>
{rows}
</table>
"""


def _copy_vendor() -> None:
    target = OUT / "vendor"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("marked.min.js", "purify.min.js"):
        shutil.copyfile(VENDOR / name, target / name)
    if (target / "katex").exists():
        shutil.rmtree(target / "katex")
    shutil.copytree(VENDOR / "katex", target / "katex")


def _stats(conn: sqlite3.Connection, doc_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT kind, count(*) FROM chunks WHERE doc_id = ? GROUP BY kind", (doc_id,)
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全部书都出 HTML（默认只出 4 本）")
    ap.add_argument("--only", nargs="*", help="按书名关键词筛选")
    args = ap.parse_args()

    conn = sqlite3.connect(documents.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT doc_id, title, page_count, chunk_count, asset_count, markdown"
        " FROM documents WHERE status = ? ORDER BY page_count DESC",
        (documents.STATUS_READY,),
    ).fetchall()

    if args.only:
        rows = [r for r in rows if any(word in r["title"] for word in args.only)]

    if OUT.exists():
        documents._log.info("清掉旧的预览目录")
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    _copy_vendor()
    (OUT / "md").mkdir(exist_ok=True)

    index_rows = []
    for row in rows:
        title = str(row["title"])
        folder = title.replace("/", "_").replace("\\", "_")[:40]
        doc_id = str(row["doc_id"])
        markdown = row["markdown"] or ""
        stats = _stats(conn, doc_id)
        want_html = args.all or any(word in title for word in PICK)

        # 原始 md 一律导一份：轻量，VS Code 里直接预览
        (OUT / "md" / f"{folder}.md").write_text(markdown, encoding="utf-8")

        if want_html:
            book = OUT / folder
            (book / "images").mkdir(parents=True, exist_ok=True)
            for asset in conn.execute(
                "SELECT asset_id, file_path FROM assets WHERE doc_id = ?", (doc_id,)
            ):
                src = Path(str(asset["file_path"]))
                if src.is_file():
                    shutil.copyfile(src, book / "images" / f"{asset['asset_id']}.png")
            body = markdown.replace("](as_", "](images/as_")
            (book / "doc.html").write_text(
                PAGE.format(
                    title=html.escape(title),
                    meta=f"{row['page_count']} 页 · {row['chunk_count']} 段 · {row['asset_count']} 图",
                    markdown=body.replace("</script", "<\\/script"),
                ),
                encoding="utf-8",
            )

        name = (
            f'<a href="{folder}/doc.html">{html.escape(title)}</a>'
            if want_html else html.escape(title)
        )
        index_rows.append(
            f"<tr><td>{name}</td><td>{row['page_count']}</td><td>{row['chunk_count']}</td>"
            f"<td>{row['asset_count']}</td><td>{stats.get('formula', 0)}</td>"
            f"<td>{stats.get('table', 0)}</td><td>{stats.get('code', 0)}</td></tr>"
        )
        print(f"导出 {title[:36]:<38} {row['page_count']:>4}页 "
              f"{'HTML' if want_html else 'md  '}")

    (OUT / "index.html").write_text(
        INDEX.format(
            count=len(rows),
            pages=sum(int(r["page_count"] or 0) for r in rows),
            chunks=sum(int(r["chunk_count"] or 0) for r in rows),
            assets=sum(int(r["asset_count"] or 0) for r in rows),
            html_count=sum(1 for r in rows if args.all or any(w in r["title"] for w in PICK)),
            rows="\n".join(index_rows),
        ),
        encoding="utf-8",
    )
    print(f"\n打开：{OUT / 'index.html'}")


if __name__ == "__main__":
    main()
