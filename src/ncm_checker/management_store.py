from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .local_state import state_directory, write_json
from .management import ManagementScan
from .scanner import Occurrence, Playlist


SCHEMA_VERSION = 1


class ManagementStoreError(RuntimeError):
    """Saved playlist management results could not be used."""


def snapshot_path(uid: int, *, directory: Path | None = None) -> Path:
    if type(uid) is not int or uid <= 0:
        raise ManagementStoreError("无法确定歌单管理结果所属的账号。")
    if directory is None:
        directory = state_directory() / "management"
    return directory / f"{uid}.json"


def save_snapshot(uid: int, result: ManagementScan, *, directory: Path | None = None) -> Path:
    path = snapshot_path(uid, directory=directory)
    payload = {
        "version": SCHEMA_VERSION,
        "uid": uid,
        "playlists": [asdict(playlist) for playlist in result.playlists],
        "tracks": {
            str(pid): [
                {"song_id": row.song_id, "title": row.title, "artists": row.artists, "album": row.album}
                for row in rows
            ]
            for pid, rows in result.tracks.items()
        },
        "warnings": result.warnings,
        "instrumentals": result.instrumentals,
    }
    try:
        write_json(path, payload)
    except (OSError, UnicodeError) as exc:
        raise ManagementStoreError(f"保存歌单管理结果失败。{path}\n{exc}") from exc
    return path


def load_snapshot(uid: int, *, directory: Path | None = None) -> ManagementScan | None:
    path = snapshot_path(uid, directory=directory)
    try:
        with path.open(encoding="utf-8") as source:
            data = json.load(source)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ManagementStoreError(f"读取本地歌单管理结果失败。{path}\n{exc}") from exc

    try:
        if type(data["version"]) is not int or data["version"] != SCHEMA_VERSION:
            raise ValueError("歌单管理文件版本不受支持")
        if type(data["uid"]) is not int or data["uid"] != uid:
            raise ValueError("歌单管理文件与当前登录账号不一致")
        warnings = data["warnings"]
        if not isinstance(warnings, list) or not all(isinstance(message, str) for message in warnings):
            raise ValueError("歌单管理提示信息无效")
        playlists = _load_playlists(data["playlists"])
        tracks = _load_tracks(data["tracks"], playlists)
        raw_instrumentals = data.get("instrumentals", {})
        if not isinstance(raw_instrumentals, dict):
            raise ValueError("纯音乐筛选数据无效")
        instrumentals = {}
        for song_id, matched in raw_instrumentals.items():
            if not song_id.isascii() or not song_id.isdecimal() or int(song_id) <= 0 or type(matched) is not bool:
                raise ValueError("纯音乐筛选数据无效")
            instrumentals[int(song_id)] = matched
    except (KeyError, TypeError, ValueError) as exc:
        raise ManagementStoreError(f"本地歌单管理结果无法使用。{path}\n{exc}") from exc
    return ManagementScan(list(playlists.values()), tracks, warnings, instrumentals)


def _load_playlists(raw_playlists: list) -> dict[str, Playlist]:
    if not isinstance(raw_playlists, list):
        raise ValueError("歌单列表无效")
    playlists = {}
    for raw_playlist in raw_playlists:
        playlist = Playlist(**raw_playlist)
        if (type(playlist.id) is not int or playlist.id <= 0 or str(playlist.id) in playlists
                or not isinstance(playlist.name, str) or type(playlist.track_count) is not int
                or playlist.track_count < 0 or playlist.category != "自建"
                or playlist.editable is not True or playlist.video is not False
                or (playlist.privacy is not None and type(playlist.privacy) is not int)):
            raise ValueError("自建歌单数据无效")
        playlists[str(playlist.id)] = playlist
    return playlists


def _load_tracks(raw_tracks: dict, playlists: dict[str, Playlist]) -> dict[int, list[Occurrence]]:
    if not isinstance(raw_tracks, dict) or not raw_tracks.keys() <= playlists.keys():
        raise ValueError("歌曲所属的歌单无效")
    tracks = {}
    for pid, raw_rows in raw_tracks.items():
        if not isinstance(raw_rows, list):
            raise ValueError("歌曲列表无效")
        playlist = playlists[pid]
        rows = []
        seen = set()
        for raw_row in raw_rows:
            row = Occurrence(**raw_row, playlist=playlist)
            if (type(row.song_id) is not int or row.song_id <= 0 or row.song_id in seen
                    or not all(isinstance(value, str) for value in (row.title, row.artists, row.album))):
                raise ValueError("歌曲数据无效或重复")
            rows.append(row)
            seen.add(row.song_id)
        tracks[playlist.id] = rows
    return tracks
