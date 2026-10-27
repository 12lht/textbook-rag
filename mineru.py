"""MinerU 云端解析：只负责「把 PDF 换成 blocks + 图片目录」，不构造项目结构。

应用层模块（同 `documents.py` / `memories.py`）：不发日志到 agent logger 之外，不 import
agent 相关的东西。**它刻意不 import `documents`**——结构转换留在 documents 那侧做，
否则 documents 要 import 它、它又要 import documents 的 `_Parsed`，成环。

两步分开是有原因的：云端跑一次要几十秒到几分钟，而且**按页算额度**（每天 1000 页最高
优先级）。所以 `parse()` 拿到 raw 结果就返回，缓存与否、转不转结构由调用方决定。
"""
from __future__ import annotations

import io
import json
import logging
import os
import time
import zipfile
from pathlib import Path

_log = logging.getLogger("agent.documents")

BASE = "https://mineru.net/api/v4"
KEY_ENV = "MINERU_API_KEY"
# 云端单文件限 200 页 / 200MB，超了要自己拆——26 本书里有 720 页的
MAX_PAGES_PER_BATCH = 200
MAX_BYTES_PER_BATCH = 200 * 1024 * 1024  # 云端同样限 200MB，只按页数切会漏
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 1800.0


class MineruError(RuntimeError):
    """云端调用失败。调用方据此决定回退还是报错。"""


def available() -> bool:
    return bool(os.getenv(KEY_ENV, "").strip())


def _headers() -> dict:
    token = os.getenv(KEY_ENV, "").strip()
    if not token:
        raise MineruError(f"没配 {KEY_ENV}")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _page_count(pdf_bytes: bytes) -> int:
    import pymupdf

    pdf = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        return pdf.page_count
    finally:
        pdf.close()


def _split(pdf_bytes: bytes, size: int) -> list[tuple[bytes, int]]:
    """按 size 页切片，返回 [(子 PDF 字节, 起始页序号 0-based)]。

    **只按页数切不够**：逐页 `insert_pdf` 会把共享资源（字体、图片）复制 N 份，720 页的书
    切出 200 页一批能鼓到 200MB 以上，云端直接回 "file size exceeds limit"。所以这里
    ① 一次插入连续页范围（资源不重复）② 存盘带 `garbage=3, deflate=True`
    ③ 仍然超限就把这一批砍一半重切。实测 Hello-Agents（97.9MB / 720 页）这样切没问题。
    """
    import pymupdf

    pdf = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = pdf.page_count
        out: list[tuple[bytes, int]] = []
        start = 0
        while start < total:
            end = min(start + size, total)
            while True:
                sub = pymupdf.open()
                sub.insert_pdf(pdf, from_page=start, to_page=end - 1)
                blob = sub.tobytes(garbage=3, deflate=True)
                sub.close()
                if len(blob) <= MAX_BYTES_PER_BATCH or end - start <= 1:
                    break
                end = start + max(1, (end - start) // 2)
            out.append((blob, start))
            start = end
        return out
    finally:
        pdf.close()


class _Fatal(MineruError):
    """不该重试的错（4xx）。单独一类，`_request` 见到就直接抛，循环里不会重试。"""


def _body_snippet(resp) -> str:
    try:
        text = resp.text or ""
    except Exception:  # noqa: BLE001
        return ""
    return " ".join(text.split())[:200]


def _request(method: str, url: str, *, retries: int = 3, **kwargs):
    """发一个请求。

    **4xx 一律当致命错立即抛**：key 过期 / 配置错 / 预签名链接被拒都会返回 4xx，而旧实现
    只把 5xx 当错误，4xx 原样返回给调用方——轮询于是空转到 1800s 才报「轮询超时」，
    把真正的原因（401/403）埋掉；下载 403 更会变成 `BadZipFile`（拿 HTML 当 zip 解）。
    429 是限流，仍然值得重试，算例外。
    """
    import requests

    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, timeout=120, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 网络抖动重试
            last = exc
        else:
            status = resp.status_code
            if 400 <= status < 500 and status != 429:
                raise _Fatal(f"HTTP {status}（{method} {url.split('?')[0]}）：{_body_snippet(resp)}")
            if status < 500:
                return resp
            last = _Fatal(f"HTTP {status}（{method} {url.split('?')[0]}）：{_body_snippet(resp)}")
        if attempt + 1 < retries:
            time.sleep(2 * (attempt + 1))
    raise MineruError(f"请求失败：{last}")


def _safe_extract(archive: zipfile.ZipFile, target: Path) -> None:
    """解压到指定目录，**跳过绝对路径和 `..`**。

    云端给的 zip 正常，但这是外部输入：一条 `../xx` 就能写到临时目录外面。
    逐项判断比 `extractall` 多几行，代价可以忽略。
    """
    root = target.resolve()
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if name.endswith("/") or name.startswith("/") or ":" in name.split("/")[0]:
            continue
        destination = (root / name).resolve()
        if not str(destination).startswith(str(root)):
            _log.warning("跳过可疑的压缩包条目：%s", info.filename)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, destination.open("wb") as sink:
            sink.write(source.read())


def _prefixed_media(block: dict, prefix: str) -> None:
    """把块里的图片路径加上批次前缀。

    **每批解压到独立目录**，所以块里的 `images/xxx.jpg` 要变成 `batch_2/images/xxx.jpg`，
    否则后一批的同名图会盖掉前一批（MinerU 每批都从 0 重新编号）。
    """
    content = block.get("content")
    if not isinstance(content, dict):
        return
    for key, value in content.items():
        if not key.endswith("_source") or not isinstance(value, dict):
            continue
        path = value.get("path")
        if path:
            value["path"] = f"{prefix}/{str(path).replace(chr(92), '/').lstrip('/')}"


def _parse_batch(pdf_bytes: bytes, name: str, batch_dir: Path, model: str) -> list[dict]:
    """一批（≤200 页）提交并等结果，返回该批的 v2 blocks。

    `batch_dir` 必须是**这一批自己的目录**：json 和图片都落在这里。多批共用同一个目录时，
    `glob("*content_list_v2.json")` 会同时看到好几个文件，字典序最小的那个不一定是刚解析的
    这批——后半本书就会套用前半本的内容，还不报错（Hello-Algo 78%、Hello-Agents 45% 中招）。
    """
    batch_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "enable_formula": True,
        "enable_table": True,
        "language": "ch",
        "model_version": model,
        "files": [{"name": name, "data_id": "docreader"}],
    }
    resp = _request("POST", f"{BASE}/file-urls/batch", headers=_headers(), json=payload)
    body = resp.json()
    if body.get("code") not in (0, None):
        raise MineruError(f"申请上传链接失败：{body.get('msg') or body}")
    batch_id = body["data"]["batch_id"]
    upload_url = body["data"]["file_urls"][0]

    # 预签名链接，带上 Content-Type 会破坏签名
    put = _request("PUT", upload_url, data=pdf_bytes)
    if put.status_code >= 400:
        raise MineruError(f"上传失败：HTTP {put.status_code}")

    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            poll = _request(
                "GET", f"{BASE}/extract-results/batch/{batch_id}", headers=_headers()
            ).json()
        except ValueError as exc:  # 200 但返回 HTML（网关/代理页）
            raise MineruError(f"轮询返回的不是 JSON：{exc}") from exc
        items = (poll.get("data") or {}).get("extract_result") or []
        if not items:
            continue
        item = items[0]
        state = item.get("state", "")
        if state == "done":
            break
        if state == "failed":
            raise MineruError(f"云端解析失败：{item.get('err_msg') or '未知原因'}")
    else:
        raise MineruError("轮询超时")

    zip_bytes = _request("GET", item["full_zip_url"]).content
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            _safe_extract(archive, batch_dir)
    except zipfile.BadZipFile as exc:
        raise MineruError(f"下载到的不是压缩包（可能被网关拦了）：{exc}") from exc

    v2 = _pick_v2_json(batch_dir, name)
    # v2 是「每页一个数组」的嵌套结构，单个 block 上没有 page_idx，页号只能从外层取
    blocks: list[dict] = []
    for page_index, page_blocks in enumerate(
        json.loads(v2.read_text(encoding="utf-8"))
    ):
        for block in page_blocks:
            block = dict(block)
            block["page_idx"] = page_index
            blocks.append(block)
    return blocks


def _pick_v2_json(batch_dir: Path, name: str) -> Path:
    """在这一批的目录里找 v2 json。**找不到就报错**，不返回空。

    以前 `if v2:` 为假时静默返回空列表，等于这一批 200 页凭空消失、状态还是 ready；
    宁愿让整本解析失败，也不要交出一本缺了一半的书。
    """
    candidates = sorted(batch_dir.rglob("*content_list_v2.json"))
    if not candidates:
        raise MineruError(f"批次结果里没有 v2 json（目录：{batch_dir}）")
    expected = Path(name).stem + "_content_list_v2.json"
    for candidate in candidates:
        if candidate.name == expected:
            return candidate
    if len(candidates) > 1:
        raise MineruError(
            "批次里有多个 v2 json，无法确定哪个是本批的："
            + "、".join(item.name for item in candidates)
        )
    return candidates[0]


def purge_dir(path: Path) -> None:
    """逐个删文件再删目录，**失败一律忽略**。

    不能用 `shutil.rmtree`：本机沙箱对「一轮里删超过 50 个文件」会直接抛 `SystemExit`
    （连 `except Exception` 都接不住，整个进程静默结束）；Windows 长路径同理。
    宁可留垃圾也不能让清理拖垮解析——调用方每次都用全新临时目录，残留不会串。
    """
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink()
        except BaseException:  # noqa: BLE001 - 见上：宿主会抛 SystemExit
            pass
    try:
        path.rmdir()
    except BaseException:  # noqa: BLE001
        pass


def parse(
    pdf_bytes: bytes,
    filename: str,
    workdir: Path,
    model: str = "vlm",
) -> dict:
    """解析整本 PDF，超过 200 页自动分批。返回 {blocks, images_dir, page_count, batches}。

    返回 v2 blocks（带 `level` 的 title），**不用 v1**：v1 的 text 块不带 `#`，标题信息
    根本不在里面，章节层级无从重建。
    """
    purge_dir(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    total = _page_count(pdf_bytes)
    batches = (
        [(pdf_bytes, 0)] if total <= MAX_PAGES_PER_BATCH else _split(pdf_bytes, MAX_PAGES_PER_BATCH)
    )

    merged: list[dict] = []
    for index, (chunk, offset) in enumerate(batches):
        prefix = f"batch_{index + 1}"
        _log.info("MinerU 第 %d/%d 批（%d 页起）", index + 1, len(batches), offset + 1)
        blocks = _parse_batch(chunk, filename, workdir / prefix, model)
        for block in blocks:
            block["page_idx"] = int(block.get("page_idx", 0)) + offset
            _prefixed_media(block, prefix)
        merged.extend(blocks)

    return {
        "blocks": merged,
        # 媒体根目录（不是 `images/`）：每批一个 `batch_N/images/` 子目录，块的 path 已带前缀
        "images_dir": workdir,
        "page_count": total,
        "batches": len(batches),
        "model": model,
    }
