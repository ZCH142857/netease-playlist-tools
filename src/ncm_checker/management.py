from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .api import ApiConnectionError, ApiError, ApiRateLimitError, Cancelled, NcmApi
from .collection_sync import Progress, Stop, SyncReport, _add_and_verify, _check_stop, _read_ids, _retry_read
from .scanner import Occurrence, Playlist, playlists_for, track_album, track_artists


@dataclass(frozen=True)
class ManagementScan:
    playlists: list[Playlist]
    tracks: dict[int, list[Occurrence]]
    warnings: list[str]


def _managed_playlists(api: NcmApi, uid: int) -> list[Playlist]:
    if str(api.login_status().get("userId")) != str(uid):
        raise ApiError("当前登录账号已变化，请重新登录并读取歌单。")
    return [
        playlist for playlist in playlists_for(api, uid)
        if playlist.category == "自建" and playlist.editable and not playlist.video
    ]


def scan_management(
    api: NcmApi,
    uid: int,
    progress: Progress,
    should_stop: Stop = lambda: False,
) -> ManagementScan:
    _check_stop(should_stop)
    progress(0, "读取自建歌单列表")
    playlists = _managed_playlists(api, uid)
    memberships: dict[int, list[int]] = {}
    warnings = []
    for index, playlist in enumerate(playlists):
        _check_stop(should_stop)
        progress(round(60 * index / len(playlists)), f"读取歌单 {index + 1}/{len(playlists)} · {playlist.name}")
        try:
            memberships[playlist.id] = list(dict.fromkeys(api.playlist_ids(playlist.id)))
        except (ApiConnectionError, ApiRateLimitError) as exc:
            warnings.append(f"读取「{playlist.name}」时中断，{len(playlists) - index} 个歌单未读取。{exc}")
            break
        except ApiError as exc:
            warnings.append(f"无法读取「{playlist.name}」（{playlist.id}）。{exc}")

    # Fetch details once per song while retaining each distinct playlist membership.
    song_ids = list(dict.fromkeys(song_id for ids in memberships.values() for song_id in ids))
    details = {}
    for offset in range(0, len(song_ids), 200):
        _check_stop(should_stop)
        progress(60 + round(39 * offset / len(song_ids)), f"读取歌曲资料 {offset + 1}/{len(song_ids)}")
        try:
            batch = api.song_details(song_ids[offset:offset + 200])
        except (ApiConnectionError, ApiRateLimitError) as exc:
            warnings.append(f"读取歌曲资料时中断。{exc}")
            break
        except ApiError as exc:
            warnings.append(f"一批歌曲资料读取失败。{exc}")
            continue
        details.update((int(song["id"]), song) for song in batch)

    _check_stop(should_stop)
    missing = len(set(song_ids) - details.keys())
    if missing:
        warnings.append(f"{missing} 首歌曲的资料暂缺，列表中保留了歌曲 ID。")
    tracks = {}
    for playlist in playlists:
        if playlist.id not in memberships:
            continue
        rows = []
        for song_id in memberships[playlist.id]:
            song = details.get(song_id, {})
            rows.append(Occurrence(
                song_id, song.get("name") or f"歌曲 {song_id}", track_artists(song), playlist, track_album(song),
            ))
        tracks[playlist.id] = rows
    progress(100, "歌单读取完成，有部分资料未能读取" if warnings else "歌单读取完成")
    return ManagementScan(playlists, tracks, warnings)


def add_to_playlist(
    api: NcmApi,
    uid: int,
    target: Playlist,
    song_ids: Iterable[int],
    progress: Progress,
    should_stop: Stop,
) -> SyncReport:
    ordered = list(dict.fromkeys(song_ids))
    report = SyncReport(pending=set(ordered), playlist_id=target.id)
    try:
        _check_stop(should_stop)
        if not ordered:
            return report
        progress(0, "核对网易云账号与目标歌单")
        playlists = _retry_read(lambda: _managed_playlists(api, uid), progress, should_stop, 0)
        live = next((playlist for playlist in playlists if playlist.id == target.id), None)
        if live is None:
            raise ApiError("目标歌单已不存在或不可编辑，请重新读取歌单。")

        def read() -> set[int]:
            return _read_ids(api, live)

        progress(10, f"读取目标歌单 · {live.name}")
        present = _retry_read(read, progress, should_stop, 10)
        report.already_present = report.pending & present
        report.pending.difference_update(report.already_present)
        _check_stop(should_stop)
        if report.pending:
            _add_and_verify(
                report, ordered, read, lambda batch: api.add_playlist_tracks(live.id, batch),
                f"添加到「{live.name}」", lambda value, message: progress(10 + round(0.9 * value), message),
                should_stop, batch_size=50, verification_size=50,
            )
        progress(100, "添加到歌单处理结束")
    except Cancelled:
        report.stopped = True
    except ApiError as exc:
        report.stopped = should_stop()
        report.message = str(exc)
    return report
