from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .api import ApiConnectionError, ApiError, ApiRateLimitError, NcmApi
from .scanner import Occurrence, TrackKey, playlists_for
from .timing import HEART_INTERVAL_SECONDS, RETRY_COOLDOWN_SECONDS, wait_until


@dataclass
class RemovalReport:
    removed: set[TrackKey] = field(default_factory=set)
    already_absent: set[TrackKey] = field(default_factory=set)
    failures: dict[TrackKey, str] = field(default_factory=dict)
    track_counts: dict[int, int] = field(default_factory=dict)
    pending: int = 0
    stopped: bool = False
    retry_at: float = 0.0
    retry_reason: str = ""
    attempted: set[TrackKey] = field(default_factory=set)


def by_playlist(rows: Iterable[Occurrence]) -> dict[int, dict[int, Occurrence]]:
    playlists: dict[int, dict[int, Occurrence]] = {}
    for row in rows:
        playlists.setdefault(row.playlist.id, {})[row.song_id] = row
    return playlists


def pause_for(report: RemovalReport, error: ApiRateLimitError | ApiConnectionError) -> None:
    limited = isinstance(error, ApiRateLimitError)
    if not report.retry_at:
        report.retry_at = time.monotonic() + RETRY_COOLDOWN_SECONDS
        report.retry_reason = "网易云限流" if limited else "连接中断"


def remove_tracks(
    api: NcmApi,
    uid: int,
    selected: list[Occurrence],
    progress: Callable[[int, str], None],
    should_stop: Callable[[], bool],
) -> RemovalReport:
    remaining = {row.key: row for row in selected}
    total = len(remaining)
    result = RemovalReport()

    while remaining and not should_stop():
        remaining_count = len(remaining)
        previous_done = total - remaining_count

        def report_progress(value: int, message: str) -> None:
            progress(round((previous_done + remaining_count * value / 100) / total * 100), message)

        try:
            attempt = remove_tracks_once(api, uid, list(remaining.values()), report_progress, should_stop)
        except ApiError as exc:
            if not result.attempted and not result.already_absent:
                raise
            result.failures.update(dict.fromkeys(remaining, str(exc)))
            remaining.clear()
            break
        observed_after_write = attempt.already_absent & result.attempted
        result.removed.update(attempt.removed | observed_after_write)
        result.already_absent.update(attempt.already_absent - observed_after_write)
        result.attempted.update(attempt.attempted)
        result.failures.update(attempt.failures)
        result.track_counts.update(attempt.track_counts)
        finished = attempt.removed | attempt.already_absent | attempt.failures.keys()
        for key in finished:
            remaining.pop(key, None)

        if not remaining or not attempt.retry_at:
            break

        result.retry_at = attempt.retry_at

        def show_countdown(seconds: int) -> None:
            progress(round((total - len(remaining)) / total * 100),
                     f"{attempt.retry_reason} · {seconds} 秒后自动继续 · 剩余 {len(remaining)} 条")

        if not wait_until(result.retry_at, should_stop, show_countdown):
            break
        result.retry_at = 0.0

    result.pending = len(remaining)
    result.stopped = should_stop() and result.pending > 0
    return result


def remove_tracks_once(
    api: NcmApi,
    uid: int,
    selected: list[Occurrence],
    progress: Callable[[int, str], None],
    should_stop: Callable[[], bool],
) -> RemovalReport:
    playlists = by_playlist(selected)
    total = sum(map(len, playlists.values()))
    report = RemovalReport()
    if should_stop():
        return report

    try:
        if int(api.login_status()["userId"]) != uid:
            raise ApiError("当前登录账号与查重时不同，请重新登录并查重")
        live_playlists = {p.id: p for p in playlists_for(api, uid)}
    except (ApiRateLimitError, ApiConnectionError) as exc:
        pause_for(report, exc)
        return report
    processed = 0
    next_unlike = 0.0

    for pid, rows in playlists.items():
        if should_stop() or report.retry_at:
            break
        scanned_playlist = next(iter(rows.values())).playlist
        live = live_playlists.get(pid)
        keys = {sid: row.key for sid, row in rows.items()}
        if (live is None or not live.editable or live.category != scanned_playlist.category
                or live.video != scanned_playlist.video):
            report.failures.update(dict.fromkeys(keys.values(), "歌单归属、类型或编辑权限发生变化"))
            processed += len(rows)
            continue

        progress(round(processed / total * 90), f"正在核对歌单 · {live.name}")
        try:
            before = set(api.playlist_ids(pid))
        except (ApiRateLimitError, ApiConnectionError) as exc:
            pause_for(report, exc)
            break
        except ApiError as exc:
            report.failures.update(dict.fromkeys(keys.values(), f"无法读取歌单。{exc}"))
            processed += len(rows)
            continue

        report.track_counts[pid] = len(before)
        absent = rows.keys() - before
        report.already_absent.update(keys[sid] for sid in absent)
        processed += len(absent)
        pending = [sid for sid in rows if sid in before]
        liked = live.category == "喜欢"
        batch_size = 1 if liked else 50
        attempted: set[int] = set()
        errors: dict[int, str] = {}
        for offset in range(0, len(pending), batch_size):
            if should_stop():
                break
            batch = pending[offset:offset + batch_size]
            action = "取消红心" if liked else "从歌单移除"
            progress(round(processed / total * 90), f"{action} · {live.name} · {processed}/{total} 条")
            if liked and not wait_until(next_unlike, should_stop):
                break
            attempted.update(batch)
            report.attempted.update(keys[sid] for sid in batch)
            processed += len(batch)
            try:
                if liked:
                    next_unlike = time.monotonic() + HEART_INTERVAL_SECONDS
                    api.unlike_track(batch[0])
                else:
                    api.remove_playlist_tracks(pid, batch)
            except (ApiRateLimitError, ApiConnectionError) as exc:
                pause_for(report, exc)
                break
            except ApiError as exc:
                errors.update(dict.fromkeys(batch, str(exc)))

        if not attempted:
            continue
        progress(round(processed / total * 90), f"正在复查移除结果 · {live.name}")
        try:
            after = set(api.playlist_ids(pid))
        except (ApiRateLimitError, ApiConnectionError) as exc:
            report.track_counts.pop(pid, None)
            pause_for(report, exc)
            break
        except ApiError as exc:
            report.track_counts.pop(pid, None)
            for sid in attempted:
                report.failures[keys[sid]] = f"暂时无法复查移除结果。{exc}"
            continue

        # Some updates only show up on a second read. Keep the first snapshot if that read fails.
        if not report.retry_at and (attempted - errors.keys()) & after:
            try:
                after = set(api.playlist_ids(pid))
            except (ApiRateLimitError, ApiConnectionError) as exc:
                pause_for(report, exc)
            except ApiError:
                pass
        report.track_counts[pid] = len(after)
        for sid in attempted:
            if sid in after:
                if not report.retry_at or sid in errors:
                    report.failures[keys[sid]] = errors.get(sid, "复查时歌曲仍在歌单里")
            else:
                report.removed.add(keys[sid])

    return report
