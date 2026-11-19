import unittest
from unittest.mock import patch

from ncm_checker.api import ApiConnectionError, ApiError, ApiRateLimitError, NcmApi
from ncm_checker.removal import remove_tracks
from ncm_checker.scanner import Occurrence, Playlist


class FakeApi:
    uid = 99

    def __init__(self):
        self.ids = {1: {21, 22, 25}, 2: {21, 23, 24}, 3: {21, 23}}
        self.calls = []
        self.stop = False

    def login_status(self):
        return {"userId": self.uid}

    def user_playlists(self, uid):
        return [
            {"id": 1, "name": "我喜欢的音乐", "specialType": 5, "userId": uid},
            {"id": 2, "name": "跑步", "creator": {"userId": str(uid)}},
            {"id": 3, "name": "别人的歌单", "creator": {"userId": 123}, "subscribed": True},
        ]

    def playlist_ids(self, playlist_id):
        return list(self.ids[playlist_id])

    def unlike_track(self, song_id):
        self.calls.append(("like", song_id))
        self.ids[1].remove(song_id)
        self.stop = True

    def remove_playlist_tracks(self, playlist_id, song_ids):
        self.calls.append(("tracks", playlist_id, song_ids))
        self.ids[playlist_id].difference_update(sid for sid in song_ids if sid != 23)


class RemovalTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.liked = Playlist(1, "我喜欢的音乐", 3, "喜欢", editable=True)
        self.normal = Playlist(2, "跑步", 3, "自建", editable=True)
        self.readonly = Playlist(3, "别人的歌单", 2, "收藏", editable=False)

    def remove(self, *rows):
        return remove_tracks(self.api, 99, list(rows), lambda *_: None, lambda: self.api.stop)

    def test_shared_song_is_removed_only_from_selected_playlist(self):
        row = Occurrence(21, "晴天", "", self.normal)
        report = self.remove(row, row, Occurrence(23, "晴天", "", self.normal),
                             Occurrence(88, "晴天", "", self.normal),
                             Occurrence(21, "晴天", "", self.readonly))
        self.assertEqual(self.api.calls, [("tracks", 2, [21, 23])])
        self.assertIn(21, self.api.ids[1])
        self.assertIn(21, self.api.ids[3])
        self.assertEqual(report.removed, {(2, 21)})
        self.assertEqual(report.already_absent, {(2, 88)})
        self.assertEqual(set(report.failures), {(2, 23), (3, 21)})
        self.assertEqual(report.track_counts, {2: 2})
        self.assertEqual(report.pending, 0)

    def test_stop_verifies_in_flight_unlike_and_preserves_remaining_song(self):
        report = self.remove(Occurrence(21, "晴天", "", self.liked),
                             Occurrence(22, "晴天", "", self.liked))
        self.assertEqual(self.api.calls, [("like", 21)])
        self.assertEqual(report.removed, {(1, 21)})
        self.assertTrue(report.stopped)
        self.assertEqual(report.pending, 1)
        self.assertEqual(self.api.ids[1], {22, 25})

    def test_rate_limit_waits_and_resumes_only_remaining_tracks(self):
        attempted = []
        cooldowns = []
        pacing = []

        def unlike(song_id):
            limited = song_id == 22 and song_id not in attempted
            attempted.append(song_id)
            if limited:
                raise ApiRateLimitError(405)
            self.api.ids[1].remove(song_id)

        def wait_until(deadline, _should_stop, countdown=None):
            if countdown:
                cooldowns.append(deadline)
                countdown(60)
            else:
                pacing.append(deadline)
            return True

        self.api.unlike_track = unlike
        rows = [Occurrence(sid, "晴天", "", self.liked) for sid in [21, 22, 25]]
        rows.append(Occurrence(21, "晴天", "", self.normal))
        with (patch("ncm_checker.removal.wait_until", side_effect=wait_until),
              patch("ncm_checker.removal.time.monotonic", return_value=100)):
            report = self.remove(*rows)
        self.assertEqual(attempted, [21, 22, 22, 25])
        self.assertEqual(cooldowns, [160])
        self.assertEqual(pacing, [0.0, 104.0, 0.0, 104.0])
        self.assertEqual(report.removed, {(1, 21), (1, 22), (1, 25), (2, 21)})
        self.assertFalse(report.failures)
        self.assertEqual(report.pending, 0)
        self.assertEqual(report.retry_at, 0)
        self.assertEqual(self.api.calls, [("tracks", 2, [21])])

    def test_repeated_limits_keep_fixed_cooldown_and_can_stop(self):
        cooldowns = []

        def wait_until(deadline, _should_stop, countdown=None):
            if countdown is not None:
                cooldowns.append(deadline)
                if len(cooldowns) == 2:
                    self.api.stop = True
                    return False
            return True

        row = Occurrence(22, "晴天", "", self.liked)
        with (patch.object(self.api, "unlike_track", side_effect=ApiRateLimitError(405)) as unlike,
              patch("ncm_checker.removal.wait_until", side_effect=wait_until),
              patch("ncm_checker.removal.time.monotonic", return_value=100)):
            report = self.remove(row)
        self.assertEqual(cooldowns, [160, 160])
        self.assertEqual(unlike.call_count, 2)
        self.assertEqual(report.pending, 1)
        self.assertEqual(report.retry_at, 160)
        self.assertTrue(report.stopped)
        self.assertIn(22, self.api.ids[1])

    def test_connection_error_after_write_is_reconciled(self):
        def apply_then_timeout(playlist_id, song_ids):
            self.api.ids[playlist_id].difference_update(song_ids)
            raise ApiConnectionError("timeout")

        self.api.remove_playlist_tracks = apply_then_timeout
        report = self.remove(Occurrence(21, "晴天", "", self.normal))
        self.assertEqual(report.removed, {(2, 21)})
        self.assertFalse(report.failures)

    def test_connection_failure_uses_the_same_fixed_cooldown(self):
        with (patch.object(self.api, "login_status", side_effect=[
                  ApiConnectionError("timeout"), {"userId": 99},
              ]),
              patch("ncm_checker.removal.wait_until", return_value=True) as wait,
              patch("ncm_checker.removal.time.monotonic", return_value=100)):
            report = self.remove(Occurrence(21, "晴天", "", self.normal))
        self.assertEqual(wait.call_count, 1)
        self.assertEqual(wait.call_args.args[0], 160)
        self.assertEqual(report.removed, {(2, 21)})

    def test_retry_read_failure_keeps_tracks_already_verified(self):
        before = self.api.ids[2].copy()
        after = before - {21}
        with patch.object(self.api, "playlist_ids", side_effect=[before, after, ApiError("不可用")]):
            report = self.remove(Occurrence(21, "晴天", "", self.normal),
                                 Occurrence(23, "晴天", "", self.normal))
        self.assertEqual(report.removed, {(2, 21)})
        self.assertEqual(set(report.failures), {(2, 23)})
        self.assertEqual(report.track_counts, {2: len(after)})

    def test_account_change_prevents_any_write(self):
        self.api.uid = 98
        with self.assertRaisesRegex(ApiError, "账号"):
            self.remove(Occurrence(21, "晴天", "", self.normal))
        self.assertEqual(self.api.calls, [])

    def test_enhanced_batch_response_is_checked(self):
        api = NcmApi(3000)
        with patch.object(api, "request", return_value={"body": {"code": 200}}) as request:
            api.remove_playlist_tracks(2, [21, 23])
        request.assert_called_once_with("/playlist/tracks", {"op": "del", "pid": 2, "tracks": "21,23"})
        with patch.object(api, "request", return_value={"body": {"code": 403}}):
            with self.assertRaisesRegex(ApiError, "403"):
                api.remove_playlist_tracks(2, [21])

    def test_recommendation_parser_excludes_source_song_and_artist_ids(self):
        api = NcmApi(3000)
        response = {"data": {"items": [
            {"song": {"id": 1, "name": "Original"}},
            {"song": {"id": 2, "name": "Other", "ar": [{"id": 3, "name": "Artist"}]}},
            {"songId": "4", "songName": "Other single"},
        ], "album": {"id": 99, "name": "Album"}}}
        with patch.object(api, "request", return_value=response):
            self.assertEqual(api.recommended_versions(1), {2: "Other", 4: "Other single"})
