from __future__ import annotations

import http.client
import json
import os
import time
from typing import Any, Callable


class ApiError(RuntimeError):
    pass


class ApiConnectionError(ApiError):
    pass


class ApiSessionExpired(ApiError):
    def __init__(self):
        super().__init__("登录会话已失效，请重新扫码或粘贴 Cookie")


class ApiRateLimitError(ApiError):
    def __init__(self, status: int):
        super().__init__(f"网易云提示操作频繁（{status}）")


class Cancelled(Exception):
    pass


def check_rate_limit(body: dict, status: int) -> None:
    codes = {str(status), str(body.get("code"))}
    message = str(body.get("msg") or body.get("message") or "")
    if "429" in codes:
        raise ApiRateLimitError(429)
    if "405" in codes and "操作频繁" in message:
        raise ApiRateLimitError(405)


class NcmApi:
    def __init__(self, port: int, check_cancel: Callable[[], None] = lambda: None):
        self.port = port
        self.check_cancel = check_cancel
        self.cookie: dict[str, str] = {}
        self.proxy = os.environ.get("NCM_PROXY", "").strip()
        self.last_request = 0.0
        self.timestamp = 0

    def request(
        self, path: str, payload: dict[str, Any] | None = None, *, auth: bool = True
    ) -> dict[str, Any]:
        self.check_cancel()
        data = {"noCookie": True, **(payload or {})}
        if self.proxy:
            data.setdefault("proxy", self.proxy)
        if auth:
            if not self.cookie:
                raise ApiError("请先登录")
            data["cookie"] = self.cookie

        time.sleep(max(0, 0.35 - (time.monotonic() - self.last_request)))
        self.timestamp = max(time.time_ns() // 1_000_000, self.timestamp + 1)
        self.last_request = time.monotonic()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=12)
        try:
            conn.request(
                "POST", f"{path}?timestamp={self.timestamp}",
                body=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            response = conn.getresponse()
            http_status = response.status
            if http_status == 429:
                raise ApiRateLimitError(429)
            body = json.loads(response.read())
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise ApiConnectionError(
                f"{path} 请求失败（{type(exc).__name__}）。请检查 Enhanced 服务及 SSH 转发。"
            ) from None
        finally:
            conn.close()

        if not isinstance(body, dict):
            raise ApiError(f"{path} 返回的内容不是 JSON 对象")
        self.check_cancel()
        check_rate_limit(body, http_status)
        if path == "/playlist/tracks" and isinstance(body.get("body"), dict):
            check_rate_limit(body["body"], http_status)
        code = body.get("code")
        if path == "/login/status" and (http_status == 401 or str(code) in {"301", "401"}):
            raise ApiSessionExpired()
        allowed = {None, 200, 201}
        if path == "/login/qr/check":
            allowed.update({800, 801, 802, 803})
        if not 200 <= http_status < 300 or code not in allowed:
            safe_code = code if type(code) is int else "未知"
            raise ApiError(f"{path} 请求失败（HTTP {http_status}，接口代码 {safe_code}）")
        return body

    def login_status(self) -> dict[str, Any]:
        body = self.request("/login/status")
        data = body.get("data")
        if isinstance(data, dict):
            if str(data.get("code")) in {"301", "401"}:
                raise ApiSessionExpired()
            if "profile" in data:
                body = data
        if "profile" in body and body["profile"] is None:
            raise ApiSessionExpired()
        profile = body.get("profile")
        if isinstance(profile, dict) and profile.get("userId"):
            return profile
        raise ApiError("账号接口未返回可验证的登录信息")

    def user_playlists(self, uid: int) -> list[dict[str, Any]]:
        playlists = {}
        limit = 100
        offset = 0
        while True:
            body = self.request("/user/playlist", {"uid": uid, "limit": limit, "offset": offset})
            page = body.get("playlist")
            if not isinstance(page, list):
                raise ApiError("无法读取歌单列表")
            seen = len(playlists)
            playlists.update((int(item["id"]), item) for item in page)
            if body.get("more") is False or (body.get("more") is None and len(page) < limit):
                return list(playlists.values())
            if len(playlists) == seen:
                raise ApiError("歌单分页没有返回新歌单")
            offset += len(page)

    def playlist_ids(self, playlist_id: int) -> list[int]:
        body = self.request("/playlist/detail", {"id": playlist_id, "s": 0})
        playlist = body.get("playlist") or {}
        ids = playlist.get("trackIds")
        if not isinstance(ids, list):
            raise ApiError(f"歌单 {playlist_id} 未返回歌曲 ID")
        track_ids = [int(item["id"]) for item in ids]
        if int(playlist.get("trackCount", len(track_ids))) != len(track_ids):
            raise ApiError(f"歌单 {playlist_id} 的歌曲列表不完整")
        return track_ids

    def playlist_tracks(self, playlist_id: int) -> tuple[list[dict[str, Any]], int]:
        ids = self.playlist_ids(playlist_id)
        found = self.song_details(ids)
        return found, len(ids) - len(found)

    def song_details(self, song_ids: list[int]) -> list[dict[str, Any]]:
        songs = {}
        for index in range(0, len(song_ids), 200):
            batch = song_ids[index:index + 200]
            body = self.request("/song/detail", {"ids": ",".join(map(str, batch))})
            if not isinstance(body.get("songs"), list):
                raise ApiError("歌曲详情响应不完整")
            privileges = {
                str(entry["id"]): entry for entry in body.get("privileges") or []
                if isinstance(entry, dict) and "id" in entry
            }
            for song in body["songs"]:
                if isinstance(song, dict) and "id" in song and isinstance(song.get("name"), str) and song["name"].strip():
                    sid = int(song["id"])
                    songs[sid] = {**song, "privilege": privileges.get(str(sid), song.get("privilege"))}
        return [songs[sid] for sid in song_ids if sid in songs]

    def remove_playlist_tracks(self, playlist_id: int, song_ids: list[int]) -> None:
        self._edit_playlist_tracks(playlist_id, song_ids, "del")

    def add_playlist_tracks(self, playlist_id: int, song_ids: list[int]) -> None:
        self._edit_playlist_tracks(playlist_id, song_ids, "add")

    def _edit_playlist_tracks(self, playlist_id: int, song_ids: list[int], operation: str) -> None:
        if not song_ids:
            return
        body = self.request("/playlist/tracks", {
            "op": operation, "pid": playlist_id, "tracks": ",".join(map(str, song_ids)),
        })
        result = body.get("body", body)
        code = result.get("code") if isinstance(result, dict) else None
        if code != 200:
            action = "移除" if operation == "del" else "添加"
            raise ApiError(f"{action}接口返回异常（接口代码 {code if type(code) is int else '未知'}）")

    def unlike_track(self, song_id: int) -> None:
        self._set_track_like(song_id, False)

    def like_track(self, song_id: int) -> None:
        self._set_track_like(song_id, True)

    def _set_track_like(self, song_id: int, liked: bool) -> None:
        # Enhanced expects the string "false" for an explicit unlike.
        body = self.request("/like", {"id": song_id, "like": "true" if liked else "false"})
        if body.get("code") != 200:
            action = "添加红心" if liked else "取消红心"
            raise ApiError(f"{action}接口未确认成功")

    def create_playlist(self, name: str) -> int:
        name = name.strip()
        if not name:
            raise ApiError("请填写公开歌单名称")
        body = self.request("/playlist/create", {"name": name, "privacy": "0", "type": "NORMAL"})
        data = body.get("data")
        data = data if isinstance(data, dict) else {}
        playlist = body.get("playlist") or data.get("playlist")
        playlist = playlist if isinstance(playlist, dict) else {}
        raw_id = playlist.get("id") or body.get("id") or data.get("id")
        pid = str(raw_id or "")
        if not (pid.isascii() and pid.isdecimal() and len(pid) <= 20 and int(pid) > 0):
            raise ApiConnectionError("创建响应未提供歌单 ID，请先在网易云核对是否已经创建")
        return int(pid)

    def recommended_versions(self, song_id: int) -> dict[int, str]:
        body = self.request("/song/copyright/rcmd", {"songid": song_id})
        versions = {}
        pending = [(body, False)]
        song_fields = {"ar", "artists", "artistName", "al", "album", "duration", "dt"}
        song_wrappers = {"song", "songs", "songInfo", "songList", "recommendSongs", "track", "tracks", "trackList"}
        metadata = song_fields | {"privilege", "privileges", "pic", "image"}
        while pending:
            value, song_context = pending.pop()
            if isinstance(value, list):
                pending.extend((item, song_context) for item in reversed(value))
            elif isinstance(value, dict):
                explicit_id = value.get("songId") or value.get("songid")
                title = value.get("songName") or value.get("name") or ""
                if explicit_id or song_context or song_fields.intersection(value):
                    raw_id = str(explicit_id or value.get("id") or "")
                    if raw_id.isascii() and raw_id.isdecimal() and len(raw_id) <= 20:
                        sid = int(raw_id)
                        if sid > 0 and sid != song_id:
                            versions.setdefault(sid, title if isinstance(title, str) else "")
                            continue
                pending.extend(
                    (item, key in song_wrappers or (key == "data" and isinstance(item, list)))
                    for key, item in value.items() if key not in metadata
                )
        return versions

    def red_count(self, song_id: int) -> int | None:
        body = self.request("/song/red/count", {"id": song_id})
        value = (body.get("data") or {}).get("count")
        if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
            value = int(value)
        return nonnegative_int(value)

    def search_song_page(self, keywords: str, offset: int, limit: int) -> tuple[list[dict], int | None]:
        body = self.request("/cloudsearch", {
            "keywords": keywords, "type": 1, "offset": offset, "limit": limit,
        })
        result = body.get("result")
        if not isinstance(result, dict):
            raise ApiError("站内搜索未返回结果")
        total = nonnegative_int(result.get("songCount"))
        songs = result.get("songs", [])
        if not isinstance(songs, list):
            raise ApiError("站内搜索返回的歌曲列表格式有误")
        return songs, total

    def comment_counts(self, song_ids: list[int]) -> dict[int, int]:
        body = self.request("/comment/info/list", {"type": 0, "ids": ",".join(map(str, song_ids))})
        data = body.get("data")
        if not isinstance(data, list):
            raise ApiError("批量评论接口未返回统计列表")
        counts = {}
        for entry in data:
            count = nonnegative_int(entry.get("commentCount"))
            try:
                sid = int(entry["resourceId"])
            except (KeyError, TypeError, ValueError):
                continue
            if count is not None:
                counts[sid] = count
        return counts

    def comment_count(self, song_id: int) -> int | None:
        body = self.request("/comment/music", {"id": song_id, "limit": 1})
        return nonnegative_int(body.get("total"))


def nonnegative_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None
