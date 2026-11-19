from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TypeVar, Union

from .api import ApiConnectionError, ApiError, ApiRateLimitError, Cancelled, NcmApi
from .classification import ClassificationSnapshot, ClassifiedSong, CollectionTrack, build_snapshot
from .scanner import Playlist, playlists_for, track_album, track_artists
from .timing import HEART_INTERVAL_SECONDS, RETRY_COOLDOWN_SECONDS, wait_until

Progress = Callable[[int, str], None]
Stop = Callable[[], bool]
TemporaryError = Union[ApiConnectionError, ApiRateLimitError]
_TEMPORARY = (ApiConnectionError, ApiRateLimitError)
ReadResult = TypeVar("ReadResult")


@dataclass(frozen=True)
class CollectionScan:
    snapshot: ClassificationSnapshot
    missing_likes: tuple[CollectionTrack, ...]
    liked_count: int
    child_count: int
    warnings: list[str]


@dataclass
class SyncReport:
    verified: set[int] = field(default_factory=set)
    already_present: set[int] = field(default_factory=set)
    skipped: dict[int, str] = field(default_factory=dict)
    failures: dict[int, str] = field(default_factory=dict)
    pending: set[int] = field(default_factory=set)
    stopped: bool = False
    playlist_id: int | None = None
    message: str = ""
    creation_uncertain: bool = False


def _check_stop(should_stop: Stop) -> None:
    if should_stop():
        raise Cancelled()


def _collection_playlists(api: NcmApi, uid: int) -> tuple[Playlist, list[Playlist]]:
    current_uid = api.login_status().get("userId")
    if str(current_uid) != str(uid):
        raise ApiError("当前网易云账号与分类结果的账号不同，请重新登录并扫描。")
    playlists = playlists_for(api, uid)
    liked = [playlist for playlist in playlists if playlist.editable and playlist.category == "喜欢"]
    if len(liked) != 1:
        raise ApiError("无法唯一识别当前账号的「我喜欢的音乐」歌单。")
    children = [
        playlist for playlist in playlists
        if playlist.category == "自建" and playlist.editable and not playlist.video
    ]
    return liked[0], children


def _read_ids(api: NcmApi, playlist: Playlist) -> set[int]:
    try:
        return set(api.playlist_ids(playlist.id))
    except _TEMPORARY:
        raise
    except ApiError as exc:
        raise ApiError(f"无法完整读取「{playlist.name}」。{exc}") from exc


def _child_sources(
    api: NcmApi,
    children: list[Playlist],
    progress: Progress,
    should_stop: Stop,
    wanted: set[int] | None = None,
    *,
    retry: bool = False,
    end_percent: int = 65,
) -> dict[int, tuple[Playlist, ...]]:
    sources: dict[int, dict[int, Playlist]] = {}
    if wanted is not None and not wanted:
        return {}
    for index, child in enumerate(children, 1):
        _check_stop(should_stop)
        percent = round(10 + (end_percent - 10) * (index - 1) / len(children))
        progress(percent, f"读取子歌单 {index}/{len(children)} · {child.name}")
        if retry:
            track_ids = _retry_read(lambda: _read_ids(api, child), progress, should_stop, percent)
        else:
            track_ids = _read_ids(api, child)
        for song_id in track_ids if wanted is None else track_ids & wanted:
            sources.setdefault(song_id, {})[child.id] = child
        if wanted is not None and wanted <= sources.keys():
            break
    return {song_id: tuple(playlists.values()) for song_id, playlists in sources.items()}


def scan_collection(
    api: NcmApi,
    uid: int,
    progress: Progress,
    should_stop: Stop = lambda: False,
) -> CollectionScan:
    _check_stop(should_stop)
    progress(0, "核对网易云账号与歌单")
    liked, children = _collection_playlists(api, uid)
    liked_ids = _read_ids(api, liked)
    sources = _child_sources(api, children, progress, should_stop)
    unfiled_ids = liked_ids - sources.keys()
    missing_ids = sources.keys() - liked_ids
    relevant_ids = sorted(unfiled_ids | missing_ids)
    details: dict[int, dict] = {}
    warnings = []
    for offset in range(0, len(relevant_ids), 200):
        _check_stop(should_stop)
        progress(
            round(65 + 34 * offset / len(relevant_ids)),
            f"读取待整理歌曲资料 {offset + 1}/{len(relevant_ids)}",
        )
        batch = relevant_ids[offset:offset + 200]
        try:
            response = api.song_details(batch)
        except _TEMPORARY as exc:
            warnings.append(f"歌曲资料读取中断。{exc}")
            break
        except ApiError:
            continue
        details.update((int(song["id"]), song) for song in response)
    _check_stop(should_stop)
    if missing_metadata := len(set(relevant_ids) - details.keys()):
        warnings.append(f"{missing_metadata} 首歌曲没有返回标题资料。未归类的歌曲保留 ID，归入「其他/待确认」。")

    def describe(song_id: int) -> ClassifiedSong:
        song = details.get(song_id, {})
        return ClassifiedSong(song_id, str(song.get("name") or ""), track_artists(song), track_album(song))

    unfiled = [describe(song_id) for song_id in sorted(unfiled_ids)]
    missing = []
    for song_id in sorted(missing_ids):
        song = describe(song_id)
        missing.append(CollectionTrack(song.song_id, song.title, song.artists, song.album, sources[song_id]))
    missing.sort(key=lambda song: (song.title.casefold(), song.song_id))
    snapshot = build_snapshot(uid, unfiled)
    progress(100, "歌单整理扫描完成")
    return CollectionScan(snapshot, tuple(missing), len(liked_ids), len(children), warnings)


def _cooldown(
    error: TemporaryError,
    completed_percent: int,
    progress: Progress,
    should_stop: Stop,
) -> None:
    limited = isinstance(error, ApiRateLimitError)
    reason = "网易云限流" if limited else "连接中断"

    def show(seconds: int) -> None:
        progress(completed_percent, f"{reason} · {seconds} 秒后自动继续")

    if not wait_until(time.monotonic() + RETRY_COOLDOWN_SECONDS, should_stop, show):
        raise Cancelled()


def _retry_read(
    read: Callable[[], ReadResult], progress: Progress, should_stop: Stop, percent: int,
) -> ReadResult:
    while not should_stop():
        try:
            return read()
        except _TEMPORARY as exc:
            _cooldown(exc, percent, progress, should_stop)
    raise Cancelled()


def _add_and_verify(
    report: SyncReport,
    ordered: list[int],
    read: Callable[[], set[int]],
    write: Callable[[list[int]], None],
    action: str,
    progress: Progress,
    should_stop: Stop,
    *,
    batch_size: int,
    verification_size: int,
    delay_between: float = 0,
) -> None:
    total = len(report.pending)
    next_write = 0.0
    remaining = [song_id for song_id in ordered if song_id in report.pending]
    percent = 0
    while remaining:
        _check_stop(should_stop)
        completed = total - len(remaining)
        window = remaining[:verification_size]
        attempted: dict[int, str] = {}
        retry: TemporaryError | None = None
        retry_ids: set[int] = set()
        for index in range(0, len(window), batch_size):
            if should_stop() or (delay_between and not wait_until(next_write, should_stop)):
                break
            batch = window[index:index + batch_size]
            for song_id in batch:
                attempted[song_id] = ""
            progress(percent, f"{action} · 处理第 {completed + index + 1}/{total} 首")
            try:
                next_write = time.monotonic() + delay_between
                write(batch)
            except _TEMPORARY as exc:
                retry = exc
                retry_ids = set(batch)
                break
            except ApiError as exc:
                attempted.update(dict.fromkeys(batch, str(exc)))
            submitted = completed + index + len(batch)
            percent = max(percent, min(99, round(100 * submitted / total)))
            progress(percent, f"{action} · 已处理 {submitted}/{total} 首")

        if not attempted:
            break
        progress(percent, f"核对{action}结果")
        try:
            if retry is not None:
                _check_stop(should_stop)
                _cooldown(retry, percent, progress, should_stop)
            live = _retry_read(read, progress, should_stop, percent)
        except Cancelled:
            # Score writes that reached the server before the stop, without retrying the read.
            live = read()
        done = set(attempted) & live
        report.verified.update(done)
        report.already_present.update(report.pending.intersection(live).difference(attempted))
        report.pending.difference_update(report.verified, report.already_present)
        if retry is None and not should_stop() and set(attempted) - done:
            live = _retry_read(read, progress, should_stop, percent)
            done.update(set(attempted) & live)
            report.verified.update(done)
            report.already_present.update(report.pending.intersection(live).difference(attempted))
        for song_id in set(attempted) - done - retry_ids:
            if attempted[song_id] or (retry is None and not should_stop()):
                report.failures[song_id] = attempted[song_id] or f"复查时网易云仍未确认{action}。"
        report.pending.difference_update(report.verified, report.already_present, report.failures)
        remaining = [song_id for song_id in remaining if song_id in report.pending]
        percent = max(percent, min(99, round(100 * (total - len(remaining)) / total)))
        progress(percent, f"{action} · 已复核 {total - len(remaining)}/{total} 首")
    _check_stop(should_stop)


def _requested(song_ids: Iterable[int]) -> list[int]:
    return list(dict.fromkeys(song_ids))


def _progress_stages(progress: Progress, write_progress: Progress | None) -> tuple[Progress, Progress]:
    if write_progress is not None:
        return progress, write_progress
    return (
        lambda value, message: progress(round(value * 0.69), message),
        lambda value, message: progress(70 + round(value * 0.3), message),
    )


def add_missing_likes(
    api: NcmApi,
    uid: int,
    song_ids: Iterable[int],
    progress: Progress,
    should_stop: Stop,
    *,
    write_progress: Progress | None = None,
) -> SyncReport:
    ordered = _requested(song_ids)
    report = SyncReport(pending=set(ordered))
    check, write = _progress_stages(progress, write_progress)
    try:
        _check_stop(should_stop)
        if not ordered:
            return report
        check(0, "核对网易云账号与歌单")
        liked, children = _retry_read(
            lambda: _collection_playlists(api, uid), check, should_stop, 0,
        )
        check(5, "读取「我喜欢的音乐」")
        present = _retry_read(lambda: _read_ids(api, liked), check, should_stop, 5)
        report.already_present = report.pending & present
        report.pending.difference_update(report.already_present)
        sources = _child_sources(api, children, check, should_stop, report.pending, retry=True, end_percent=95)
        report.skipped.update(dict.fromkeys(report.pending - sources.keys(), "已不在选定范围的子歌单中"))
        report.pending.difference_update(report.skipped)
        _check_stop(should_stop)
        check(100, f"歌单核对完成 · {len(report.pending)} 首待补红心")
        if not report.pending:
            if write_progress is None:
                progress(100, "歌单核对完成")
            return report

        write(0, f"开始补加红心 · {len(report.pending)} 首")
        _add_and_verify(
            report, ordered, lambda: _read_ids(api, liked), lambda batch: api.like_track(batch[0]),
            "补加红心", write, should_stop, batch_size=1, verification_size=20,
            delay_between=HEART_INTERVAL_SECONDS,
        )
        write(100, "补加红心处理结束")
    except Cancelled:
        report.stopped = True
    except ApiError as exc:
        report.stopped = should_stop()
        report.message = str(exc)
    return report


def create_category_playlist(
    api: NcmApi,
    uid: int,
    name: str,
    song_ids: Iterable[int],
    progress: Progress,
    should_stop: Stop,
    *,
    playlist_id: int | None = None,
    write_progress: Progress | None = None,
) -> SyncReport:
    ordered = _requested(song_ids)
    report = SyncReport(pending=set(ordered), playlist_id=playlist_id)
    check, write = _progress_stages(progress, write_progress)
    name = name.strip()
    if not name:
        report.message = "请填写公开歌单名称。"
        return report
    try:
        _check_stop(should_stop)
        if not ordered:
            return report
        check(0, "核对分类歌曲与当前歌单")
        liked, children = _retry_read(
            lambda: _collection_playlists(api, uid), check, should_stop, 0,
        )
        target = None
        if playlist_id is not None:
            target = next((playlist for playlist in children if playlist.id == playlist_id), None)
            if target is None or not target.editable or target.name != name or target.privacy != 0:
                raise ApiError("续填目标的归属、名称或公开状态已变化，请回网易云核对。")
            children = [child for child in children if child.id != playlist_id]
            check(5, f"读取目标歌单 · {target.name}")
            target_ids = _retry_read(lambda: _read_ids(api, target), check, should_stop, 5)
            report.already_present = report.pending & target_ids
            report.pending.difference_update(report.already_present)
        check(8, "读取「我喜欢的音乐」")
        liked_ids = _retry_read(lambda: _read_ids(api, liked), check, should_stop, 8)
        report.skipped.update(dict.fromkeys(report.pending - liked_ids, "已不在「我喜欢的音乐」"))
        report.pending.difference_update(report.skipped)
        sources = _child_sources(api, children, check, should_stop, report.pending, retry=True, end_percent=95)
        report.skipped.update(
            (song_id, f"已分入子歌单 · {'、'.join(playlist.name for playlist in playlists)}")
            for song_id, playlists in sources.items()
        )
        report.pending.difference_update(report.skipped)
        _check_stop(should_stop)
        check(100, f"歌单核对完成 · {len(report.pending)} 首待添加")
        if not report.pending:
            if write_progress is None:
                progress(100, "分类歌曲已经归入歌单或已失效")
            return report

        if target is None:
            write(0, "创建公开歌单")
            _check_stop(should_stop)
            try:
                created_id = api.create_playlist(name)
            except ApiConnectionError:
                report.creation_uncertain = True
                report.message = "创建请求结果无法确认。请先在网易云检查是否已出现同名歌单。"
                return report
            report.playlist_id = created_id
            target = Playlist(created_id, name, 0, "自建", editable=True, privacy=0)
        else:
            write(0, f"开始添加到歌单 · {target.name}")

        _add_and_verify(
            report, ordered, lambda: _read_ids(api, target),
            lambda batch: api.add_playlist_tracks(target.id, batch),
            "添加到公开歌单", write, should_stop, batch_size=50, verification_size=50,
        )
        write(100, "公开歌单处理结束")
    except Cancelled:
        report.stopped = True
    except ApiError as exc:
        report.stopped = should_stop()
        report.message = str(exc)
    return report
