"""教材分类：建、改名、删、把书归到某类下面。

分类只影响「藏书阁怎么摆」，不参与检索——所以它和 core 里的上传/切分互不依赖。
"""
from __future__ import annotations

import secrets
import sqlite3

from .db import _connect, _now


MAX_CATEGORY_NAME = 40


class CategoryExists(ValueError):
    """同一个人已经有同名分类。和 `AccountTaken` 一样用异常，让上层能区分
    「你填的东西不行」（→ 400）和系统故障。"""


def _clean_category_name(name: object) -> str:
    """折叠空白 + 截断。清洗后为空直接抛——空名字的分类在界面上没法点。"""
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        raise ValueError("分类名不能为空")
    if len(cleaned) > MAX_CATEGORY_NAME:
        raise ValueError(f"分类名不能超过 {MAX_CATEGORY_NAME} 个字")
    return cleaned


def list_categories(user_id: str) -> list[dict[str, object]]:
    """这个人的全部分类，带上每类有几本书，按创建先后排。
    用 `LEFT JOIN` 而不是先查分类再逐个查书数（N+1）；`d.user_id = c.user_id` 不能少，
    否则别人的书会让这个人的计数串过来。
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT c.category_id, c.name, c.created_at,
                   COUNT(d.doc_id) AS doc_count
              FROM categories c
              LEFT JOIN documents d
                     ON d.category_id = c.category_id AND d.user_id = c.user_id
             WHERE c.user_id = ?
             GROUP BY c.category_id
             ORDER BY c.created_at ASC, c.rowid ASC
            """,
            (str(user_id),),
        ).fetchall()
    return [
        {
            "category_id": row["category_id"],
            "name": row["name"],
            "doc_count": row["doc_count"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def create_category(user_id: str, name: object) -> dict[str, object]:
    """新建分类。重名抛 `CategoryExists`。

    靠唯一索引判重而不是「先查再插」：先查再插中间有窗口，两次并发新建会双双通过。
    """
    cleaned = _clean_category_name(name)
    category_id = f"c-{secrets.token_hex(4)}"
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO categories (category_id, user_id, name, created_at)"
                " VALUES (?, ?, ?, ?)",
                (category_id, str(user_id), cleaned, _now()),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise CategoryExists("已经有同名的分类了") from exc
    return {"category_id": category_id, "name": cleaned, "doc_count": 0}


def rename_category(category_id: str, user_id: str, name: object) -> str:
    """改分类名。返回 `"ok"` / `"missing"`（没这类）/ `"clash"`（改成重名了）。

    三个结果分开，是因为对用户的意义不同：404 该刷新，409 该换个名字。
    混成一句「改失败」会让人反复重试同一个名字。
    """
    cleaned = _clean_category_name(name)
    try:
        with _connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM categories WHERE category_id = ? AND user_id = ?",
                (category_id, str(user_id)),
            ).fetchone()
            if exists is None:
                return "missing"
            conn.execute(
                "UPDATE categories SET name = ? WHERE category_id = ? AND user_id = ?",
                (cleaned, category_id, str(user_id)),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return "clash"
    return "ok"


def delete_category(category_id: str, user_id: str) -> bool:
    """删分类，类下的书退回未分类、不跟着删，返回它此前是否存在。
    书是几十 MB 解析出来的贵重资产，分类只是标签——「删掉一个标签顺手删掉 8 本书」
    点错了就没法挽回。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM categories WHERE category_id = ? AND user_id = ?",
            (category_id, str(user_id)),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE documents SET category_id = NULL WHERE category_id = ? AND user_id = ?",
            (category_id, str(user_id)),
        )
        conn.execute(
            "DELETE FROM categories WHERE category_id = ? AND user_id = ?",
            (category_id, str(user_id)),
        )
        conn.commit()
    return True


def assign_category(doc_id: str, user_id: str, category_id: str | None) -> str:
    """把一本书挪到某个分类（`None` = 挪回未分类）。返回 ok / missing / no_category。
    书和分类两个 id 都要按 `user_id` 验一遍：只验书的话，别人可以把他的书挂到我的分类 id 上。
    """
    target = None if category_id is None else str(category_id)
    with _connect() as conn:
        doc = conn.execute(
            "SELECT 1 FROM documents WHERE doc_id = ? AND user_id = ?",
            (doc_id, str(user_id)),
        ).fetchone()
        if doc is None:
            return "missing"
        if target is not None:
            owned = conn.execute(
                "SELECT 1 FROM categories WHERE category_id = ? AND user_id = ?",
                (target, str(user_id)),
            ).fetchone()
            if owned is None:
                return "no_category"
        conn.execute(
            "UPDATE documents SET category_id = ? WHERE doc_id = ? AND user_id = ?",
            (target, doc_id, str(user_id)),
        )
        conn.commit()
    return "ok"
