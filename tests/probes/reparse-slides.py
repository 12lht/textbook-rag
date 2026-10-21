# -*- coding: utf-8 -*-
"""重解析全部幻灯片版式的书：让「邻近图取全」的修复落到已入库的书上。

改动前切出来的图左右被题注范围裁边（图 2.13 的「测量」框被切一半），
修复在 documents.py 的 _slide_figure_rect，已入库的书要重解析才有新图。
Hello-Agents / Hello-Algo 是排版教材（_nearest_graphic_above 路径），不受影响，跳过。
"""
import asyncio
import sqlite3
import sys
import time

sys.path.insert(0, "D:/agent")

import service  # noqa: E402

USER = "u-671d242584"


def slide_doc_ids() -> list[str]:
    conn = sqlite3.connect("D:/agent/data/documents.sqlite3")
    rows = conn.execute(
        "SELECT DISTINCT d.doc_id FROM documents d JOIN assets a ON a.doc_id = d.doc_id "
        "WHERE a.label LIKE '%整页图示%' AND d.status = 'ready'"
    ).fetchall()
    return [r[0] for r in rows]


async def main():
    doc_ids = slide_doc_ids()
    print(f"待重解析 {len(doc_ids)} 本", flush=True)
    conn = sqlite3.connect("D:/agent/data/documents.sqlite3")
    # 复用原文件重解析：置 pending 再逐本跑（与上传后的后台解析同一条链路）
    conn.executemany(
        "UPDATE documents SET status='pending', error=NULL WHERE doc_id=?",
        [(d,) for d in doc_ids],
    )
    conn.commit()
    conn.close()

    ok = failed = 0
    for index, doc_id in enumerate(doc_ids, 1):
        started = time.time()
        record = await service.parse_uploaded_document(doc_id, USER)
        status = record.get("status")
        if status == "ready":
            ok += 1
        else:
            failed += 1
        print(
            f"[{index}/{len(doc_ids)}] {doc_id} {status} "
            f"chunks={record.get('chunk_count')} assets={record.get('asset_count')} "
            f"{time.time() - started:.0f}s"
            + (f" err={record.get('error')}" if status != "ready" else ""),
            flush=True,
        )
    print(f"完成：{ok} 成功 / {failed} 失败", flush=True)


asyncio.run(main())
