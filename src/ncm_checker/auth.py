from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable
from urllib.parse import unquote

from .api import ApiError, NcmApi


def parse_cookie(raw: str) -> dict[str, str]:
    cookie = {}
    attributes = {"path", "domain", "expires", "max-age", "samesite", "priority", "version"}
    raw = re.sub(r"^cookie:\s*", "", raw.strip(), flags=re.IGNORECASE)
    for pair in raw.split(";"):
        name, separator, value = pair.strip().partition("=")
        if separator and name.lower() not in attributes and re.fullmatch(r"[\w.-]+", name):
            cookie[name] = unquote(value.strip())
    if not cookie.get("MUSIC_U"):
        raise ApiError("未找到登录 Cookie MUSIC_U，请确认网页已登录或改用扫码")
    return cookie


def scan_cookie(
    api: NcmApi, display_qr: Callable[[bytes], None], display_status: Callable[[str], None]
) -> dict[str, str]:
    key = (api.request("/login/qr/key", auth=False).get("data") or {}).get("unikey")
    if not key:
        raise ApiError("无法取得二维码登录凭据")
    data = (api.request("/login/qr/create", {"key": key, "qrimg": True}, auth=False).get("data") or {})
    try:
        prefix, encoded = data["qrimg"].split(",", 1)
        if prefix != "data:image/png;base64":
            raise ValueError
        display_qr(base64.b64decode(encoded, validate=True))
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ApiError("接口未返回有效的二维码图片") from None
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        result = api.request("/login/qr/check", {"key": key, "noCookie": True}, auth=False)
        code = result.get("code")
        if code == 803:
            return parse_cookie(str(result.get("cookie") or ""))
        if code == 800:
            raise ApiError("二维码已过期，请重新扫码登录")
        if code not in (801, 802):
            raise ApiError("无法读取扫码状态，请稍后重新扫码登录")
        display_status("已扫码，请在手机上确认" if code == 802 else "用手机网易云音乐 App 扫码")
        for _ in range(8):
            api.check_cancel()
            time.sleep(0.25)
    raise ApiError("等待扫码超时，请重新扫码登录")
