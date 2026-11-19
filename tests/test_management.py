import unittest
from unittest.mock import patch

from ncm_checker.api import ApiConnectionError, ApiError
from ncm_checker.management import add_to_playlist, scan_management
from ncm_checker.removal import remove_tracks


def progress(*_):
    pass


class ManagementApi:
    def __init__(self):
        self.uid = 9
        self.playlists = [
            {"id": 1, "userId": 9, "name": "我喜欢的音乐", "specialType": 5},
            {"id": 2, "userId": 9, "name": "通勤", "privacy": 10},
            {"id": 3, "userId": 9, "name": "周末"},
            {"id": 4, "userId": 42, "name": "外部歌单", "subscribed": True},
            {"id": 5, "userId": 9, "name": "MV", "type": "VIDEO"},
            {"id": 6, "userId": 9, "name": "收藏标记", "subscribed": True},
            {"id": 7, "userId": 9, "name": "空歌单"},
        ]
        self.ids = {2: [21, 20], 3: [20], 7: []}
        self.playlist_reads = []
        self.detail_reads = []
        self.writes = []

    def login_status(self):
        return {"userId": self.uid}

    def user_playlists(self, _uid):
        return self.playlists

    def playlist_ids(self, playlist_id):
        self.playlist_reads.append(playlist_id)
        return self.ids[playlist_id].copy()

    def song_details(self, song_ids):
        self.detail_reads.extend(song_ids)
        return [{"id": 20, "name": "老歌", "ar": [{"name": "歌手"}], "al": {"name": "专辑"}}]

    def add_playlist_tracks(self, playlist_id, song_ids):
        self.writes.append(("add", playlist_id, song_ids))
        self.ids[playlist_id].extend(song_ids)

    def remove_playlist_tracks(self, playlist_id, song_ids):
        self.writes.append(("del", playlist_id, song_ids))
        self.ids[playlist_id] = [song_id for song_id in self.ids[playlist_id] if song_id not in song_ids]


class ManagementTest(unittest.TestCase):
    def setUp(self):
        self.api = ManagementApi()

    def test_scan_preserves_membership_and_loads_unique_song_details(self):
        result = scan_management(self.api, 9, progress)
        self.assertEqual([playlist.id for playlist in result.playlists], [2, 3, 7])
        self.assertEqual(self.api.playlist_reads, [2, 3, 7])
        self.assertEqual(self.api.detail_reads, [21, 20])
        self.assertEqual([track.key for track in result.tracks[2]], [(2, 21), (2, 20)])
        self.assertEqual([track.key for track in result.tracks[3]], [(3, 20)])
        self.assertEqual(result.tracks[2][0].title, "歌曲 21")
        self.assertEqual(result.tracks[2][1].artists, "歌手")
        self.assertEqual(result.tracks[2][1].album, "专辑")
        self.assertEqual(result.tracks[7], [])
        self.assertTrue(result.warnings)

    def test_scan_marks_failed_memberships_unread(self):
        before = self.api.playlist_ids

        def playlist_ids(playlist_id):
            if playlist_id == 2:
                raise ApiError("列表不完整")
            return before(playlist_id)

        with patch.object(self.api, "playlist_ids", side_effect=playlist_ids):
            result = scan_management(self.api, 9, progress)
        self.assertNotIn(2, result.tracks)
        self.assertEqual(result.tracks[7], [])
        self.assertIn("通勤", result.warnings[0])

    def test_add_skips_existing_songs_and_verifies_uncertain_write_without_resending(self):
        target = scan_management(self.api, 9, progress).playlists[1]
        original_add = self.api.add_playlist_tracks

        def add(playlist_id, song_ids):
            original_add(playlist_id, song_ids)
            raise ApiConnectionError("请求结果未知")

        with (patch.object(self.api, "add_playlist_tracks", side_effect=add),
              patch("ncm_checker.collection_sync.wait_until", return_value=True)):
            report = add_to_playlist(self.api, 9, target, [20, 21, 21], progress, lambda: False)
        self.assertEqual(report.already_present, {20})
        self.assertEqual(report.verified, {21})
        self.assertFalse(report.pending)
        self.assertEqual(self.api.writes, [("add", 3, [21])])

    def test_add_rejects_changed_account_and_target_permissions(self):
        target = scan_management(self.api, 9, progress).playlists[1]
        self.api.uid = 42
        wrong_account = add_to_playlist(self.api, 9, target, [21], progress, lambda: False)
        self.assertIn("账号", wrong_account.message)
        self.api.uid = 9
        self.api.playlists[2]["subscribed"] = True
        wrong_target = add_to_playlist(self.api, 9, target, [21], progress, lambda: False)
        self.assertIn("目标歌单", wrong_target.message)
        self.assertEqual(wrong_target.pending, {21})
        self.assertFalse(self.api.writes)

    def test_remove_rejects_playlist_that_has_changed_type(self):
        row = scan_management(self.api, 9, progress).tracks[2][0]
        self.api.playlists[1]["type"] = "VIDEO"
        report = remove_tracks(self.api, 9, [row], progress, lambda: False)
        self.assertIn(row.key, report.failures)
        self.assertFalse(self.api.writes)
