"""pytest 公共设施：路径、临时存储、需要样本 PDF 的标记。

这三件事以前是每个探针各自抄一遍的：

1. 仓库根插进 `sys.path`（否则 `import documents` 直接失败）；
2. 把 `documents_db.DB_PATH` / `UPLOAD_DIR` / `ASSET_DIR` 指到临时目录——
   漏指一处就会静默写进真库，这个坑 2026-11-02 真的踩过；
3. 判断样本 PDF 在不在（`textbook/` 已移出仓库，只剩 `data/uploads/`）。

现在收敛到这里，测试函数只声明自己需要的 fixture。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session", autouse=True)
def _load_dotenv():
    """读仓库根的 `.env`，让需要 key 的测试和 `python api.py` 走同一份配置。"""
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """把 documents 的全部存储重定向到临时目录，测试结束自动还原。

    存储路径只在 `documents/db.py` 定义一次，其余模块用 `_db.XXX` 动态读，
    所以 patch 这一个模块就够了——不要再去 patch `documents.DB_PATH`。
    """
    from documents import db

    (tmp_path / "uploads").mkdir()
    (tmp_path / "assets").mkdir()
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "documents.sqlite3")
    monkeypatch.setattr(db, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(db, "ASSET_DIR", tmp_path / "assets")
    return tmp_path


@pytest.fixture(scope="session")
def sample_pdf():
    """`data/uploads/` 里的真实样本 PDF。找不到直接失败，不静默跳过。"""
    probes_dir = Path(__file__).resolve().parent / "probes"
    if str(probes_dir) not in sys.path:
        sys.path.insert(0, str(probes_dir))
    from _sample import sample_pdf as load

    return load


@pytest.fixture(scope="session")
def find_sample():
    """同 `sample_pdf`，但找不到时返回 None 而不是退出（调用方自己决定怎么报错）。"""
    probes_dir = Path(__file__).resolve().parent / "probes"
    if str(probes_dir) not in sys.path:
        sys.path.insert(0, str(probes_dir))
    from _sample import find_sample as find

    return find
