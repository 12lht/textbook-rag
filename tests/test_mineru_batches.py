"""MinerU 多批解析不能串页（不联网、不烧额度）。

背景：以前每批都解压到同一个 workdir，再用 `sorted(glob("*content_list_v2.json"))[0]`
取文件。批次数一多目录里就有好几个 json，字典序最小的那个不一定是刚解析的这批，
于是后半本书套用前半本的内容、还不报错（线上 Hello-Algo 78%、Hello-Agents 45% 中招）。

这里把网络层换成假的：三批各自返回**不同的正文**和**同名但不同内容**的图片，
检查合并结果是否逐批正确、图片是否指向各自批次而不是被后一批覆盖。

原 `tests/probes/probe-mineru-batches.py`，2026-11-03 迁到 pytest。
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
import requests

import documents
import mineru

TEXTS = ["第一批正文", "第二批正文", "第三批正文"]
COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]


class FakeResponse:
    def __init__(self, *, status_code: int = 200, payload=None, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text or (json.dumps(payload, ensure_ascii=False) if payload else "")

    def json(self):
        return self._payload


def jpeg_bytes(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def batch_zip(text: str, image: bytes, *, image_name: str = "img_0.jpg") -> bytes:
    """造一份和云端同构的批次产物：一个 v2 json + 一张同名图。"""
    blocks = [[
        {
            "type": "paragraph",
            "bbox": [10, 10, 900, 60],
            "content": {"paragraph_content": [{"type": "text", "content": text}]},
        },
        {
            "type": "image",
            "bbox": [10, 100, 900, 600],
            "content": {"image_source": {"path": f"images/{image_name}"}, "image_caption": []},
        },
    ]]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("document_content_list_v2.json", json.dumps(blocks, ensure_ascii=False))
        archive.writestr(f"images/{image_name}", image)
    return buffer.getvalue()


@pytest.fixture
def payload(monkeypatch, tmp_path):
    """装好假网络，解析三批，返回合并后的 payload。"""
    monkeypatch.setenv("MINERU_API_KEY", "probe-fake-key")  # 只过 `_headers` 的检查
    monkeypatch.setattr(mineru, "POLL_INTERVAL", 0.0)
    monkeypatch.setattr(mineru.time, "sleep", lambda *_args: None)  # 退避不真等
    monkeypatch.setattr(mineru, "_page_count", lambda _data: 600)  # 3 批
    monkeypatch.setattr(
        mineru, "_split", lambda _data, _size: [(b"fake", 0), (b"fake", 200), (b"fake", 400)]
    )

    zips = [batch_zip(text, jpeg_bytes(color)) for text, color in zip(TEXTS, COLORS, strict=True)]
    state = {"batch": -1, "calls": []}

    def fake_request(method: str, url: str, *, retries: int = 3, **kwargs):
        state["calls"].append(f"{method} {url}")
        if url.endswith("/file-urls/batch"):
            state["batch"] += 1
            return FakeResponse(payload={
                "code": 0,
                "data": {
                    "batch_id": f"b{state['batch']}",
                    "file_urls": [f"https://upload.example/{state['batch']}"],
                },
            })
        if method == "PUT":
            return FakeResponse(status_code=200)
        if "/extract-results/batch/" in url:
            return FakeResponse(payload={
                "code": 0,
                "data": {"extract_result": [{
                    "state": "done",
                    "full_zip_url": f"https://zip.example/{state['batch']}",
                    "err_msg": "",
                }]},
            })
        if url.startswith("https://zip.example/"):
            return FakeResponse(content=zips[int(url.rsplit("/", 1)[-1])])
        raise AssertionError(f"没预期的请求：{method} {url}")

    monkeypatch.setattr(mineru, "_request", fake_request)
    return mineru.parse(b"pdf", "document.pdf", tmp_path / "work", model="vlm")


def _paragraphs(payload) -> list[str]:
    return [
        block["content"]["paragraph_content"][0]["content"]
        for block in payload["blocks"]
        if block.get("type") == "paragraph"
    ]


def _image_paths(payload) -> list[str]:
    return [
        block["content"]["image_source"]["path"]
        for block in payload["blocks"]
        if block.get("type") == "image"
    ]


def test_batches_keep_their_own_text_and_pages(payload):
    assert _paragraphs(payload) == TEXTS, _paragraphs(payload)
    assert payload["batches"] == 3, payload["batches"]

    pages = [block["page_idx"] for block in payload["blocks"] if block.get("type") == "paragraph"]
    assert pages == [0, 200, 400], f"页号没按批次加偏移：{pages}"


def test_images_do_not_bleed_across_batches(payload):
    paths = _image_paths(payload)
    assert all(path.startswith("batch_") for path in paths), f"路径没带批次前缀：{paths}"
    assert len(set(paths)) == 3, f"三批指向了同一个路径：{paths}"

    resolved = [documents._mineru_image_path(payload["images_dir"], path) for path in paths]
    assert all(item is not None and item.is_file() for item in resolved), resolved

    digests = {hashlib.sha1(item.read_bytes()).hexdigest() for item in resolved if item is not None}
    assert len(digests) == 3, f"只有 {len(digests)} 种内容——被后一批覆盖了"


def test_assets_are_distinct_and_captioned(payload, tmp_path):
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    parsed = documents._elements_from_mineru(payload, assets_dir)
    assert len(parsed.assets) == 3, len(parsed.assets)

    digests = {hashlib.sha1(Path(item["file_path"]).read_bytes()).hexdigest() for item in parsed.assets}
    assert len(digests) == 3, f"asset 只有 {len(digests)} 种内容"
    assert all(element.content.strip() for element in parsed.elements if element.kind == "figure"), (
        "无题注图没有占位图注"
    )


def test_same_bytes_reuse_same_asset(payload, tmp_path):
    """同一批里重复出现的同名图，按字节去重成一个 asset。"""
    first_dir = tmp_path / "dedupe"
    first_dir.mkdir()
    parsed = documents._elements_from_mineru(payload, first_dir)
    first = parsed.assets[0]["asset_id"]

    second_dir = tmp_path / "dedupe2"
    second_dir.mkdir()
    same = documents._save_mineru_image(
        payload["images_dir"], second_dir, _image_paths(payload)[0], parsed, 1, "重复图"
    )
    assert same == first, f"{same} vs {first}"


def test_missing_json_raises(tmp_path):
    with pytest.raises(mineru.MineruError, match="没有 v2 json"):
        mineru._pick_v2_json(tmp_path, "document.pdf")


def test_multiple_json_without_match_raises(tmp_path):
    (tmp_path / "a_content_list_v2.json").write_text("[]", encoding="utf-8")
    (tmp_path / "b_content_list_v2.json").write_text("[]", encoding="utf-8")
    with pytest.raises(mineru.MineruError, match="多个 v2 json"):
        mineru._pick_v2_json(tmp_path, "document.pdf")


class RawResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text
        self.content = b""

    def json(self):
        return None


def _run_request(monkeypatch, status: int, text: str = "") -> tuple[str, int]:
    calls = []

    def fake(*_args, **_kwargs):
        calls.append(status)
        return RawResponse(status, text)

    monkeypatch.setattr(requests, "request", fake)
    error = ""
    try:
        mineru._request("GET", "https://mineru.example/x")
    except mineru.MineruError as exc:
        error = str(exc)
    return error, len(calls)


@pytest.mark.parametrize(
    "status,body,fragment",
    [(401, '{"msg":"invalid token"}', "invalid token"), (403, "denied", "403")],
)
def test_client_errors_fail_fast(monkeypatch, status, body, fragment):
    """4xx 要立刻抛，不去空转重试 30 分钟。"""
    error, count = _run_request(monkeypatch, status, body)
    assert str(status) in error and fragment in error, f"{status}：{error[:90]}"
    assert count == 1, f"发了 {count} 次"


def test_server_errors_retry_three_times(monkeypatch):
    error, count = _run_request(monkeypatch, 503, "busy")
    assert count == 3, f"发了 {count} 次"
    assert "503" in error, error[:60]
