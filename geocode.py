"""把「用户授权提供的坐标」解析成可读位置。

这是**客户端位置**这一路的解析，和 IP 定位是两回事：
  - IP 定位（已删除）：拿请求的来源 IP 去猜，多用户部署下所有人都会被猜成服务器所在地；
  - 本模块：坐标由用户在浏览器里明确授权给出，是权威的，不需要猜。

实现只用标准库（urllib），不引 langchain，也不被 tools.py / mcp_server.py 引用——
坐标解析发生在 Agent 进程里，不属于 MCP server 的职责。

两个高德接口的坑（都用真实请求验证过）：
  1. `location` 参数是「**经度,纬度**」，和浏览器 `navigator.geolocation` 给的
     `{latitude, longitude}` 顺序**正好相反**。传反了不会报错，只会安静地返回
     另一个半球的位置，所以这里显式换序。
  2. 直辖市（北京/上海/天津/重庆）的 `city` 字段是**空列表 `[]`**，不是空字符串；
     广州/成都这类才给 `'广州市'`。所以取字段前必须判类型，否则拼接会报 TypeError
     或者拼出「北京市[]」这种东西。
"""
from __future__ import annotations

import json
import os
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

REGEO_URL = "https://restapi.amap.com/v3/geocode/regeo"

_CACHE_LIMIT = 256
_cache: dict[tuple[float, float], str] = {}
_lock = threading.Lock()


def _text(value: object) -> str:
    """高德的字段可能是字符串，也可能是空列表（`city` 对直辖市就是 `[]`）。"""
    return value if isinstance(value, str) else ""


def _cache_key(latitude: float, longitude: float) -> tuple[float, float]:
    return (round(latitude, 2), round(longitude, 2))


def _cached(key: tuple[float, float]) -> str | None:
    with _lock:
        return _cache.get(key)


def _remember(key: tuple[float, float], value: str) -> None:
    with _lock:
        if len(_cache) >= _CACHE_LIMIT:
            _cache.pop(next(iter(_cache)), None)
        _cache[key] = value


def describe(latitude: float, longitude: float) -> str | None:
    """把坐标解析成「省 + 市」的可读文本，例如 `广东省广州市`、`北京市`。

    解析不出来（没配 key、网络失败、坐标无效）一律返回 None，由调用方决定
    「没有位置」时怎么办。这里不抛异常——位置是锦上添花的信息，
    拿不到不该让整条对话失败。
    """
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None

    key = _cache_key(latitude, longitude)
    hit = _cached(key)
    if hit is not None:
        return hit

    api_key = os.getenv("AMAP_API_KEY")
    if not api_key:
        return None

    location = f"{longitude:.6f},{latitude:.6f}"
    params = urlencode({"location": location, "key": api_key, "extensions": "base"})
    try:
        with urlopen(f"{REGEO_URL}?{params}", timeout=10) as response:
            data = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None

    if data.get("status") != "1":
        return None
    component = (data.get("regeocode") or {}).get("addressComponent") or {}
    province = _text(component.get("province"))
    city = _text(component.get("city"))
    described = f"{province}{city}" or province
    if not described:
        return None

    _remember(key, described)
    return described
