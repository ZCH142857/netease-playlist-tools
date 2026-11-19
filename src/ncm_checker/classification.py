from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .local_state import state_directory, write_json
from .scanner import Playlist


LANGUAGE_ORDER = ("日语", "韩语", "俄语", "英语", "中文", "其他/待确认")
SCHEMA_VERSION = 1


class ClassificationStoreError(RuntimeError):
    """Saved collection results could not be used."""


@dataclass(frozen=True)
class ClassifiedSong:
    song_id: int
    title: str
    artists: str = ""
    album: str = ""


@dataclass(frozen=True)
class CollectionTrack(ClassifiedSong):
    sources: tuple[Playlist, ...] = ()


@dataclass(frozen=True)
class ClassificationSnapshot:
    uid: int
    saved_at: str
    groups: dict[str, tuple[ClassifiedSong, ...]]
    # Older files lack this list. An empty tuple means the list was saved and has no pending tracks.
    missing_likes: tuple[CollectionTrack, ...] | None = None


def classify_title(title: str) -> str:
    """Classify a title by its scripts using the configured language order."""
    priority = len(LANGUAGE_ORDER) - 1
    other_letters = False
    for character in unicodedata.normalize("NFKC", title):
        if not character.isalpha():
            continue
        name = unicodedata.name(character, "")
        if name.startswith(("HIRAGANA LETTER", "KATAKANA LETTER")):
            return LANGUAGE_ORDER[0]
        if name.startswith("HANGUL"):
            priority = min(priority, 1)
        elif name.startswith("CYRILLIC"):
            priority = min(priority, 2)
        elif name.startswith("LATIN"):
            priority = min(priority, 3)
        elif name.startswith(("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH")):
            priority = min(priority, 4)
        elif name and not name.endswith(("PROLONGED SOUND MARK", "ITERATION MARK")):
            other_letters = True

    if priority == 4 and other_letters:
        return LANGUAGE_ORDER[-1]
    return LANGUAGE_ORDER[priority]


def build_snapshot(
    uid: int,
    songs: Iterable[ClassifiedSong],
) -> ClassificationSnapshot:
    groups: dict[str, list[ClassifiedSong]] = {language: [] for language in LANGUAGE_ORDER}
    seen = set()
    for song in songs:
        if song.song_id not in seen:
            seen.add(song.song_id)
            groups[classify_title(song.title)].append(song)
    return ClassificationSnapshot(
        uid,
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        {
            language: tuple(sorted(rows, key=lambda song: (
                unicodedata.normalize("NFKC", song.title).casefold(),
                song.song_id,
            )))
            for language, rows in groups.items()
        },
    )


def snapshot_path(uid: int, *, directory: Path | None = None) -> Path:
    if type(uid) is not int or uid <= 0:
        raise ClassificationStoreError("无法确定分类结果所属的账号。")
    if directory is None:
        directory = state_directory() / "classification"
    return directory / f"{uid}.json"


def save_snapshot(snapshot: ClassificationSnapshot, *, directory: Path | None = None) -> Path:
    path = snapshot_path(snapshot.uid, directory=directory)
    payload = {
        "version": SCHEMA_VERSION,
        "uid": snapshot.uid,
        "saved_at": snapshot.saved_at,
        "groups": {
            language: [asdict(song) for song in snapshot.groups.get(language, ())]
            for language in LANGUAGE_ORDER
        },
    }
    if snapshot.missing_likes is not None:
        payload["missing_likes"] = [asdict(song) for song in snapshot.missing_likes]
    try:
        write_json(path, payload)
    except (OSError, UnicodeError) as exc:
        raise ClassificationStoreError(f"保存归集结果失败。{path}\n{exc}") from exc
    return path


def _check_saved_song(song: ClassifiedSong, seen: set[int]) -> None:
    if type(song.song_id) is not int or song.song_id <= 0 or song.song_id in seen:
        raise ValueError("歌曲 ID 无效或重复")
    if not all(isinstance(value, str) for value in (song.title, song.artists, song.album)):
        raise ValueError("歌曲标题、歌手或专辑无效")
    seen.add(song.song_id)


def _load_missing_likes(raw_songs: list) -> tuple[CollectionTrack, ...]:
    if not isinstance(raw_songs, list):
        raise ValueError("待补红心列表无效")
    seen = set()
    songs = []
    for raw_song in raw_songs:
        raw_playlists = raw_song["sources"]
        if not isinstance(raw_playlists, list) or not raw_playlists:
            raise ValueError("待补红心歌曲缺少来源歌单")
        playlists = tuple(Playlist(**source) for source in raw_playlists)
        playlist_ids = set()
        for playlist in playlists:
            if (type(playlist.id) is not int or playlist.id <= 0 or playlist.id in playlist_ids
                    or not isinstance(playlist.name, str) or type(playlist.track_count) is not int
                    or playlist.category != "自建" or playlist.editable is not True or playlist.video is not False
                    or (playlist.privacy is not None and type(playlist.privacy) is not int)):
                raise ValueError("待补红心的来源歌单无效")
            playlist_ids.add(playlist.id)
        song = CollectionTrack(**{**raw_song, "sources": playlists})
        _check_saved_song(song, seen)
        songs.append(song)
    return tuple(songs)


def load_snapshot(uid: int, *, directory: Path | None = None) -> ClassificationSnapshot | None:
    path = snapshot_path(uid, directory=directory)
    try:
        with path.open(encoding="utf-8") as source:
            data = json.load(source)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ClassificationStoreError(f"读取本地归集结果失败。{path}\n{exc}") from exc

    try:
        if type(data["version"]) is not int or data["version"] != SCHEMA_VERSION:
            raise ValueError("分类文件版本不受支持")
        if type(data["uid"]) is not int or data["uid"] != uid:
            raise ValueError("分类文件与当前登录账号不一致")
        if datetime.fromisoformat(data["saved_at"]).tzinfo is None:
            raise ValueError("分类时间缺少时区")
        include_subscribed = data.get("include_subscribed", False)
        if type(include_subscribed) is not bool:
            raise ValueError("分类的歌单范围无效")
        if include_subscribed:
            raise ClassificationStoreError("上次分类包含收藏歌单，请重新扫描自建歌单。")
        raw_groups = data["groups"]
        if not isinstance(raw_groups, dict) or set(raw_groups) != set(LANGUAGE_ORDER):
            raise ValueError("语言分类不完整")

        groups = {}
        seen = set()
        for language in LANGUAGE_ORDER:
            raw_songs = raw_groups[language]
            if not isinstance(raw_songs, list):
                raise ValueError("歌曲数据无效")
            songs = []
            for raw_song in raw_songs:
                song = ClassifiedSong(**raw_song)
                _check_saved_song(song, seen)
                songs.append(song)
            groups[language] = tuple(songs)
        missing_likes = _load_missing_likes(data["missing_likes"]) if "missing_likes" in data else None
    except (KeyError, TypeError, ValueError) as exc:
        raise ClassificationStoreError(f"本地归集结果无法使用。{path}\n{exc}") from exc
    return ClassificationSnapshot(uid, data["saved_at"], groups, missing_likes)
