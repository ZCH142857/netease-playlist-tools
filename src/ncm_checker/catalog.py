from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from .api import ApiConnectionError, ApiError, ApiRateLimitError, NcmApi
from .normalize import normalize_title, title_prefix
from .scanner import Occurrence, track_album, track_artists

PAGE_SIZE = 50
TITLE_PAGES = 3
ARTIST_PAGES = 2


@dataclass(frozen=True)
class CatalogTrack:
    song_id: int
    title: str
    artists: str
    album: str
    playlists: tuple[str, ...] = ()
    hearts: int | None = None


@dataclass(frozen=True)
class CatalogGroup:
    title: str
    tracks: list[CatalogTrack]


@dataclass(frozen=True)
class CatalogComparison:
    groups: list[CatalogGroup]
    notes: list[str]


def compare_catalog(
    api: NcmApi,
    groups: dict[str, list[Occurrence]],
    progress: Callable[[int, str], None],
) -> CatalogComparison:
    comparisons = []
    notes = []
    hearts = {}
    stopped = False

    for index, (key, occurrences) in enumerate(groups.items()):
        if stopped:
            notes.append(f"剩余 {len(groups) - index} 组尚未搜索。")
            break
        title = title_prefix(occurrences[0].title)
        compact_title = "".join(title.split())
        tracks: dict[int, CatalogTrack] = {}
        for row in occurrences:
            track = tracks.get(row.song_id, CatalogTrack(row.song_id, row.title, row.artists, row.album))
            if row.playlist.name not in track.playlists:
                tracks[row.song_id] = replace(track, playlists=track.playlists + (row.playlist.name,))

        artists = sorted({
            name.strip() for row in occurrences for name in row.artists.split(" / ") if name.strip()
        })
        queries = [(title, TITLE_PAGES)]
        if compact_title != title:
            queries.append((compact_title, TITLE_PAGES))
        queries.extend((f"{title} {name}", ARTIST_PAGES) for name in artists)
        progress(round(index / len(groups) * 100), f"搜索 {index + 1}/{len(groups)} · {title}")
        truncated = False
        for query, page_cap in queries:
            offset = 0
            try:
                for _ in range(page_cap):
                    songs, total = api.search_song_page(query, offset, PAGE_SIZE)
                    for song in songs:
                        if not isinstance(song, dict):
                            continue
                        hit = song.get("name")
                        if not isinstance(hit, str) or normalize_title(hit) != key:
                            continue
                        raw_id = str(song.get("id", ""))
                        if raw_id.isascii() and raw_id.isdecimal() and len(raw_id) <= 20:
                            sid = int(raw_id)
                            if sid > 0 and sid not in tracks:
                                tracks[sid] = CatalogTrack(sid, hit, track_artists(song), track_album(song))
                    offset += len(songs)
                    if not songs or (total is not None and offset >= total) or (
                        total is None and len(songs) < PAGE_SIZE
                    ):
                        break
                else:
                    truncated = True
            except (ApiConnectionError, ApiRateLimitError) as exc:
                notes.append(f"{title} · 搜索中断 · {exc}")
                stopped = True
                break
            except ApiError as exc:
                notes.append(f"{query} · 部分搜索结果缺失 · {exc}")
        if truncated:
            notes.append(f"{title} · 已达到搜索页数上限，站内可能还有其他结果。")

        for row_index, sid in enumerate(tracks):
            if stopped:
                break
            if sid in hearts:
                continue
            value = round((index + 0.35 + 0.64 * row_index / len(tracks)) / len(groups) * 100)
            progress(value, f"核对红心数 · {title} · {row_index + 1}/{len(tracks)}")
            try:
                hearts[sid] = api.red_count(sid)
            except (ApiConnectionError, ApiRateLimitError) as exc:
                notes.append(f"{title} · 红心数查询中断 · {exc}")
                stopped = True
            except ApiError:
                hearts[sid] = None
        ranked = sorted(
            (replace(track, hearts=hearts.get(sid)) for sid, track in tracks.items()),
            key=lambda track: (track.hearts is None, -(track.hearts or 0), not track.playlists, track.song_id),
        )
        comparisons.append(CatalogGroup(title, ranked))

    return CatalogComparison(comparisons, notes)
