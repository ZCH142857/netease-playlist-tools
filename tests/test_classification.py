import json
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ncm_checker.classification import (
    LANGUAGE_ORDER,
    ClassificationStoreError,
    ClassifiedSong,
    CollectionTrack,
    build_snapshot,
    classify_title,
    load_snapshot,
    save_snapshot,
)
from ncm_checker.scanner import Playlist


class ClassificationTest(unittest.TestCase):
    def test_mixed_scripts_respect_language_priority(self):
        cases = (
            ("中文 Night Привет 한글 さよなら", "日语"),
            ("中文 Night Привет 가", "韩语"),
            ("中文 Night Привет", "俄语"),
            ("中文 Ｎｉｇｈｔ", "英语"),
            ("月光𠮷", "中文"),
            ("中文 Αθήνα", "其他/待确认"),
            (" 2026 ♪ ！ ", "其他/待确认"),
            ("", "其他/待确认"),
        )
        for title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(classify_title(title), expected)
        self.assertEqual(classify_title("中文 ｶﾀｶﾅ"), "日语")
        self.assertEqual(classify_title("中文 Étoile"), "英语")

    def test_snapshot_sorts_each_group_and_retains_full_metadata(self):
        tracks = [
            ClassifiedSong(20, "Ｂeta（现场版）", "歌手", "专辑"),
            ClassifiedSong(19, "Alpha  "),
            ClassifiedSong(17, "Ａlpha  "),
            ClassifiedSong(17, "different title for same id"),
            ClassifiedSong(16, ""),
        ]
        snapshot = build_snapshot(123, tracks)
        self.assertEqual(tuple(snapshot.groups), LANGUAGE_ORDER)
        self.assertEqual(snapshot.groups["英语"], (tracks[2], tracks[1], tracks[0]))
        self.assertEqual(snapshot.groups["其他/待确认"], (tracks[-1],))

    def test_round_trip_is_local_per_user_and_reads_older_owned_snapshot(self):
        snapshot = build_snapshot(123, [ClassifiedSong(12, "加油")])
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "private"
            self.assertIsNone(load_snapshot(123, directory=directory))
            path = save_snapshot(snapshot, directory=directory)
            self.assertEqual(path.name, "123.json")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(load_snapshot(123, directory=directory), snapshot)
            self.assertIsNone(load_snapshot(456, directory=directory))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(data), {"version", "uid", "saved_at", "groups"})
            data["include_subscribed"] = False
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(load_snapshot(123, directory=directory), snapshot)
            data["uid"] = 456
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ClassificationStoreError, "不一致"):
                load_snapshot(123, directory=directory)

    def test_old_snapshot_with_subscribed_playlists_requires_rescan(self):
        snapshot = build_snapshot(123, [ClassifiedSong(12, "加油")])
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = save_snapshot(snapshot, directory=directory)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["include_subscribed"] = True
            stale = json.dumps(data)
            path.write_text(stale, encoding="utf-8")
            with self.assertRaisesRegex(ClassificationStoreError, "重新扫描自建歌单"):
                load_snapshot(123, directory=directory)
            self.assertEqual(path.read_text(encoding="utf-8"), stale)

    def test_pending_likes_round_trip_keeps_sources_and_empty_results(self):
        work = Playlist(80, "通勤", 12, "自建", editable=True, privacy=10)
        home = Playlist(90, "周末", 7, "自建", editable=True, privacy=0)
        track = CollectionTrack(13, "歌曲", "歌手", "专辑", (work, home))
        classification = build_snapshot(123, [ClassifiedSong(12, "加油")])
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            save_snapshot(classification, directory=directory)
            self.assertIsNone(load_snapshot(123, directory=directory).missing_likes)

            for songs in ((track,), ()):
                with self.subTest(songs=songs):
                    snapshot = replace(classification, missing_likes=songs)
                    save_snapshot(snapshot, directory=directory)
                    self.assertEqual(load_snapshot(123, directory=directory), snapshot)

    def test_pending_likes_reject_subscribed_playlists(self):
        source = Playlist(80, "通勤", 12, "自建", editable=True)
        snapshot = replace(build_snapshot(123, []), missing_likes=(CollectionTrack(13, "歌曲", sources=(source,)),))
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = save_snapshot(snapshot, directory=directory)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["missing_likes"][0]["sources"][0]["category"] = "收藏"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ClassificationStoreError, "来源歌单无效"):
                load_snapshot(123, directory=directory)

    def test_failed_save_keeps_last_snapshot_and_cleans_temporary_file(self):
        original = build_snapshot(123, [ClassifiedSong(12, "原来的歌")])
        replacement = build_snapshot(123, [ClassifiedSong(13, "新的歌")])
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = save_snapshot(original, directory=directory)
            with patch("ncm_checker.classification.os.replace", side_effect=OSError("permission denied")):
                with self.assertRaises(ClassificationStoreError):
                    save_snapshot(replacement, directory=directory)
            self.assertEqual(load_snapshot(123, directory=directory), original)
            self.assertEqual(list(directory.iterdir()), [path])

    def test_invalid_json_reports_a_store_error(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "123.json").write_text("{", encoding="utf-8")
            with self.assertRaises(ClassificationStoreError):
                load_snapshot(123, directory=directory)
