import json
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ncm_checker.management import ManagementScan
from ncm_checker.management_store import ManagementStoreError, load_snapshot, save_snapshot
from ncm_checker.scanner import Occurrence, Playlist


class ManagementStoreTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "management"
        work = Playlist(10, "通勤", 1, "自建", editable=True, privacy=10)
        empty = Playlist(11, "空歌单", 0, "自建", editable=True)
        unread = Playlist(12, "待读歌单", 10, "自建", editable=True)
        self.scan = ManagementScan(
            [work, empty, unread], {10: [Occurrence(20, "歌曲", "歌手", work, "专辑")], 11: []},
            ["「待读歌单」读取失败。"], {20: True},
        )

    def test_round_trip_keeps_account_scope_and_memberships(self):
        self.assertIsNone(load_snapshot(123, directory=self.directory))
        path = save_snapshot(123, self.scan, directory=self.directory)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        loaded = load_snapshot(123, directory=self.directory)
        self.assertEqual(loaded, self.scan)
        self.assertIs(loaded.tracks[10][0].playlist, loaded.playlists[0])
        self.assertIsNone(load_snapshot(456, directory=self.directory))

    def test_rejects_invalid_cache_contents(self):
        path = save_snapshot(123, self.scan, directory=self.directory)
        valid = path.read_text(encoding="utf-8")
        for field, value in (
            ("uid", 456), ("version", True), ("tracks", {"99": []}), ("instrumentals", {"20": "true"}),
        ):
            with self.subTest(field=field):
                data = json.loads(valid)
                data[field] = value
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ManagementStoreError):
                    load_snapshot(123, directory=self.directory)
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(ManagementStoreError):
            load_snapshot(123, directory=self.directory)

    def test_loads_a_snapshot_saved_before_the_instrumental_filter(self):
        path = save_snapshot(123, self.scan, directory=self.directory)
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["instrumentals"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(load_snapshot(123, directory=self.directory), replace(self.scan, instrumentals={}))

    def test_failed_save_keeps_previous_result(self):
        path = save_snapshot(123, self.scan, directory=self.directory)
        with patch("ncm_checker.local_state.os.replace", side_effect=OSError("permission denied")):
            with self.assertRaises(ManagementStoreError):
                save_snapshot(123, ManagementScan([], {}, []), directory=self.directory)
        self.assertEqual(load_snapshot(123, directory=self.directory), self.scan)
        self.assertEqual(list(self.directory.iterdir()), [path])
