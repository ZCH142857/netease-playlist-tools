from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from .api import ApiError, ApiConnectionError, ApiRateLimitError, NcmApi
from .normalize import normalize_title

TrackKey = tuple[int, int]


@dataclass(frozen=True)
class Playlist:
    id: int
    name: str
    track_count: int
    category: str
    editable: bool = False
    privacy: int | None = None
    video: bool = False


@dataclass(frozen=True)
class Occurrence:
    song_id: int
    title: str
    artists: str
    playlist: Playlist
    album: str = ""

    @property
    def key(self) -> TrackKey:
        return self.playlist.id, self.song_id


@dataclass(frozen=True)
class PlaybackInfo:
    status: str
    versions: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class ScanResult:
    groups: dict[str, list[Occurrence]]
    hearts: dict[int, int]
    comments: dict[int, int]
    warnings: list[str]
    playback: dict[int, PlaybackInfo] = field(default_factory=dict)


def track_artists(track: dict) -> str:
    names = track.get("ar") or track.get("artists") or []
    return " / ".join(str(person["name"]) for person in names if isinstance(person, dict) and person.get("name"))


def track_album(track: dict) -> str:
    album = track.get("al") or track.get("album")
    return str(album.get("name") or "") if isinstance(album, dict) else ""


def playback_from_detail(track: dict) -> PlaybackInfo:
    privilege = track.get("privilege")
    if not isinstance(privilege, dict):
        return PlaybackInfo("状态未知")
    status = privilege.get("st")
    if (type(status) is int and status < 0) or privilege.get("toast") is True:
        return PlaybackInfo("此版本受限")
    bitrate = privilege.get("pl")
    level = privilege.get("plLevel")
    if (type(bitrate) is int and bitrate > 0) or (isinstance(level, str) and level not in ("", "none")):
        return PlaybackInfo("有播放权限")
    if (type(bitrate) is int and bitrate == 0) or level == "none":
        return PlaybackInfo("权限待确认")
    return PlaybackInfo("状态未知")


def playlists_for(api: NcmApi, uid: int) -> list[Playlist]:
    result = []
    for entry in api.user_playlists(uid):
        owner = (entry.get("creator") or {}).get("userId") or entry.get("userId")
        if entry.get("subscribed") or str(owner) != str(uid):
            continue
        liked = int(entry.get("specialType") or 0) == 5
        category = "喜欢" if liked else "自建"
        privacy = entry.get("privacy")
        privacy = int(privacy) if str(privacy) in ("0", "10") else None
        video = entry.get("video") is True or str(entry.get("playlistType") or entry.get("type")).upper() == "VIDEO"
        result.append(Playlist(
            int(entry["id"]), entry["name"], int(entry.get("trackCount") or 0), category,
            editable=True, privacy=privacy, video=video,
        ))
    return result


def collect_duplicates(
    api: NcmApi, playlists: list[Playlist], combine: bool, progress: Callable[[int, str], None],
) -> ScanResult:
    by_title: dict[str, list[Occurrence]] = defaultdict(list)
    warnings = []
    loaded = 0
    playback = {}
    check_versions = set()
    for index, playlist in enumerate(playlists):
        progress(round(index / len(playlists) * 45), f"读取歌单 {index + 1}/{len(playlists)} · {playlist.name}")
        try:
            tracks, missing = api.playlist_tracks(playlist.id)
        except ApiConnectionError:
            raise
        except ApiError as exc:
            warnings.append(f"{playlist.name}（{playlist.id}）读取失败。{exc}")
            continue
        loaded += 1
        if missing:
            warnings.append(f"{playlist.name}（{playlist.id}）少了 {missing} 首歌曲的详情")
        local: dict[str, list[Occurrence]] = defaultdict(list)
        for track in tracks:
            song_id = int(track["id"])
            title = track["name"]
            key = normalize_title(title)
            local[key].append(Occurrence(song_id, title, track_artists(track), playlist, track_album(track)))
            if song_id not in playback:
                info = playback_from_detail(track)
                playback[song_id] = info
                if info.status == "此版本受限" or (info.status != "有播放权限" and track.get("noCopyrightRcmd")):
                    check_versions.add(song_id)
        for key, copies in local.items():
            if combine or len(copies) > 1:
                by_title[key].extend(copies)

    if loaded == 0:
        raise ApiError("未能读取所选歌单。\n" + "\n".join(warnings))
    groups = {key: by_title[key] for key in sorted(by_title) if len(by_title[key]) > 1}
    song_ids = list(dict.fromkeys(row.song_id for rows in groups.values() for row in rows))
    if not song_ids:
        progress(100, "查重完成")
        return ScanResult(groups, {}, {}, warnings)

    playback = {sid: playback[sid] for sid in song_ids}
    to_check = [sid for sid in song_ids if sid in check_versions]
    unknown_versions = 0
    for index, sid in enumerate(to_check):
        progress(45 + round(index / len(to_check) * 10), f"查询其他可播版本 {index + 1}/{len(to_check)}")
        try:
            versions = api.recommended_versions(sid)
        except (ApiConnectionError, ApiRateLimitError):
            unknown_versions += len(to_check) - index
            break
        except ApiError:
            unknown_versions += 1
            continue
        if versions:
            playback[sid] = PlaybackInfo("其他版本可播", tuple(versions.items()))
    if unknown_versions:
        warnings.append(f"{unknown_versions} 首歌曲的其他版本查询未完成。")

    hearts = {}
    rating_start = 55 if to_check else 45
    for index, song_id in enumerate(song_ids):
        progress(round(rating_start + index / len(song_ids) * (85 - rating_start)),
                 f"查询同名歌曲的红心数 {index + 1}/{len(song_ids)}")
        try:
            count = api.red_count(song_id)
        except ApiConnectionError:
            raise
        except ApiError:
            continue
        if count is not None:
            hearts[song_id] = count

    comments = {}
    for index in range(0, len(song_ids), 50):
        batch = song_ids[index:index + 50]
        progress(85 + round(index / len(song_ids) * 14), f"读取评论数 {index + 1}–{index + len(batch)}/{len(song_ids)}")
        try:
            counts = api.comment_counts(batch)
        except ApiConnectionError:
            raise
        except ApiError:
            counts = {}
        for song_id in batch:
            if song_id in counts:
                comments[song_id] = counts[song_id]
                continue
            try:
                count = api.comment_count(song_id)
            except ApiConnectionError:
                raise
            except ApiError:
                continue
            if count is not None:
                comments[song_id] = count

    missing_hearts = len(song_ids) - len(hearts)
    missing_comments = len(song_ids) - len(comments)
    if missing_hearts or missing_comments:
        warnings.append(f"{missing_hearts} 首歌曲缺少收藏人数，{missing_comments} 首缺少评论数。表格中以「—」显示。")
    progress(100, "查重完成")
    return ScanResult(groups, hearts, comments, warnings, playback)
