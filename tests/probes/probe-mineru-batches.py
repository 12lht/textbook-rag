"""离线回归：MinerU 多批解析不能串页（不联网、不烧额度）。

背景：以前每批都解压到同一个 workdir，再用 `sorted(glob("*content_list_v2.json"))[0]`
取文件。批次数一多目录里就有好几个 json，字典序最小的那个不一定是刚解析的这批，
于是后半本书套用前半本的内容、还不报错（线上 Hello-Algo 78%、Hello-Agents 45% 中招）。

这个探针把网络层换成假的：三批各自返回**不同的正文**和**同名但不同内容**的图片，
检查合并结果是否逐批正确、图片是否指向各自批次而不是被后一批覆盖。

跑法：python tests/probes/probe-mineru-batches.py
"""
from __future__ import annotations

import hashlib
import io
import os
import json
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import documents  # noqa: E402
import mineru  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


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


def batch_zip(text: str, image: bytes, *, image_name: str = "img_0.jpg",
              json_name: str = "document_content_list_v2.json",
              extra_json: bool = False) -> bytes:
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
            "content": {
                "image_source": {"path": f"images/{image_name}"},
                "image_caption": [],
            },
        },
    ]]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(json_name, json.dumps(blocks, ensure_ascii=False))
        if extra_json:
            archive.writestr("readme_content_list_v2.json", json.dumps([[{"type": "title"}]], ensure_ascii=False))
        archive.writestr(f"images/{image_name}", image)
    return buffer.getvalue()


def install_fake_network(zips: list[bytes]) -> dict:
    """把 `mineru._request` 换成按调用顺序发预置响应的桩。"""
    state = {"batch": -1, "calls": []}

    def fake_request(method: str, url: str, *, retries: int = 3, **kwargs):
        state["calls"].append(f"{method} {url}")
        if url.endswith("/file-urls/batch"):
            state["batch"] += 1
            return FakeResponse(payload={
                "code": 0,
                "data": {"batch_id": f"b{state['batch']}",
                         "file_urls": [f"https://upload.example/{state['batch']}"]},
            })
        if method == "PUT":
            return FakeResponse(status_code=200)
        if "/extract-results/batch/" in url:
            index = state["batch"]
            return FakeResponse(payload={
                "code": 0,
                "data": {"extract_result": [{
                    "state": "done",
                    "full_zip_url": f"https://zip.example/{index}",
                    "err_msg": "",
                }]},
            })
        if url.startswith("https://zip.example/"):
            index = int(url.rsplit("/", 1)[-1])
            return FakeResponse(content=zips[index])
        raise AssertionError(f"没预期的请求：{method} {url}")

    mineru._request = fake_request
    return state


def main() -> int:
    os.environ.setdefault("MINERU_API_KEY", "probe-fake-key")  # 只过 `_headers` 的检查，网络是桩
    mineru.POLL_INTERVAL = 0.0
    mineru.time.sleep = lambda *_args: None  # 重试退避也不真等
    mineru._page_count = lambda _data: 600  # 3 批
    mineru._split = lambda _data, _size: [(b"fake", 0), (b"fake", 200), (b"fake", 400)]

    texts = ["第一批正文", "第二批正文", "第三批正文"]
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    zips = [batch_zip(text, jpeg_bytes(color)) for text, color in zip(texts, colors, strict=True)]
    real_request = mineru._request  # `_request` 下面会被换成桩，这里留一份真身
    install_fake_network(zips)

    workdir = Path(tempfile.mkdtemp(prefix="mineru-batches-"))
    asset_dir = Path(tempfile.mkdtemp(prefix="mineru-assets-"))
    payload = mineru.parse(b"pdf", "document.pdf", workdir, model="vlm")

    print("== 1. 每批正文 ==\n")
    paragraphs = [
        block["content"]["paragraph_content"][0]["content"]
        for block in payload["blocks"]
        if block.get("type") == "paragraph"
    ]
    check("三批正文都在、顺序正确", paragraphs == texts, str(paragraphs))
    check("批次数正确", payload["batches"] == 3, str(payload["batches"]))

    pages = [block["page_idx"] for block in payload["blocks"] if block.get("type") == "paragraph"]
    check("页号按批次加偏移", pages == [0, 200, 400], str(pages))

    print("\n== 2. 图片不串批 ==\n")
    paths = [
        block["content"]["image_source"]["path"]
        for block in payload["blocks"]
        if block.get("type") == "image"
    ]
    check("图片路径带批次前缀", all(p.startswith("batch_") for p in paths), str(paths))
    check("三批路径各不相同", len(set(paths)) == 3, str(paths))

    resolved = [documents._mineru_image_path(payload["images_dir"], p) for p in paths]
    check("每个路径都能解析到真实文件", all(p is not None and p.is_file() for p in resolved),
          str(resolved))
    digests = [
        hashlib.sha1(p.read_bytes()).hexdigest() for p in resolved if p is not None
    ]
    check("三张图内容各不相同（没被后一批覆盖）", len(set(digests)) == 3, f"{len(set(digests))} 种")

    print("\n== 3. 折成元素流 ==\n")
    parsed = documents._elements_from_mineru(payload, asset_dir)
    check("三张图都存成 asset", len(parsed.assets) == 3, str(len(parsed.assets)))
    asset_digests = {
        hashlib.sha1(Path(item["file_path"]).read_bytes()).hexdigest() for item in parsed.assets
    }
    check("asset 是三张不同的图", len(asset_digests) == 3, f"{len(asset_digests)} 种")
    check("无题注图有占位图注",
          all(element.content.strip() for element in parsed.elements if element.kind == "figure"))

    print("\n== 4. 同名图去重（同一批重复出现） ==\n")
    duplicated = documents._elements_from_mineru(payload, Path(tempfile.mkdtemp(prefix="dedupe-")))
    first = duplicated.assets[0]["asset_id"]
    same = documents._save_mineru_image(
        payload["images_dir"], Path(tempfile.mkdtemp(prefix="dedupe2-")),
        paths[0], duplicated, 1, "重复图",
    )
    check("字节相同的图复用同一个 asset", same == first, f"{same} vs {first}")

    print("\n== 5. 缺 json / 多个 json 要报错，不能静默返回空 ==\n")
    empty_dir = Path(tempfile.mkdtemp(prefix="nojson-"))
    try:
        mineru._pick_v2_json(empty_dir, "document.pdf")
        check("缺 json 抛错", False, "居然返回了")
    except mineru.MineruError as exc:
        check("缺 json 抛错", "没有 v2 json" in str(exc), str(exc)[:70])

    multi_dir = Path(tempfile.mkdtemp(prefix="multijson-"))
    (multi_dir / "a_content_list_v2.json").write_text("[]", encoding="utf-8")
    (multi_dir / "b_content_list_v2.json").write_text("[]", encoding="utf-8")
    try:
        mineru._pick_v2_json(multi_dir, "document.pdf")
        check("多个 json 且无同名匹配时抛错", False, "居然挑了一个")
    except mineru.MineruError as exc:
        check("多个 json 且无同名匹配时抛错", "多个 v2 json" in str(exc), str(exc)[:70])

    print("\n== 6. 4xx 立即失败（不再空转 30 分钟）==\n")
    import requests

    class RawResponse:
        def __init__(self, status_code: int, text: str = ""):
            self.status_code = status_code
            self.text = text
            self.content = b""
            self._payload = None

        def json(self):
            return self._payload

    def run_request(status: int, text: str = "") -> tuple[str, int]:
        calls = []
        original = requests.request

        def fake(*_args, **_kwargs):
            calls.append(status)
            return RawResponse(status, text)

        requests.request = fake
        err = ""
        try:
            real_request("GET", "https://mineru.example/x")
        except mineru.MineruError as exc:
            err = str(exc)
        finally:
            requests.request = original
        return err, len(calls)

    err, count = run_request(401, '{"msg":"invalid token"}')
    check("401 直接抛错且带原因", "401" in err and "invalid token" in err, err[:90])
    check("401 只发一次（不重试）", count == 1, f"发了 {count} 次")

    err, count = run_request(403, "denied")
    check("403 也直接抛（下载 403 不再变 BadZipFile）", "403" in err, err[:90])

    err, count = run_request(503, "busy")
    check("503 重试 3 次后放弃", count == 3 and "503" in err, f"{count} 次 / {err[:60]}")

    print()
    if failures:
        print(f"{len(failures)} 项失败：" + "、".join(failures))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
