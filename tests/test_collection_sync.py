import unittest
from unittest.mock import patch

from ncm_checker.api import ApiConnectionError, ApiError, ApiRateLimitError
from ncm_checker.collection_sync import add_missing_likes, create_category_playlist, scan_collection


def progress(*_):
    pass


class CollectionApi:
    def __init__(self):
        self.playlists = [
            {"id": 1, "userId": 9, "name": "已有红心", "specialType": 5},
            {"id": 2, "userId": 9, "name": "通勤"},
            {"id": 3, "userId": 9, "name": "书房"},
            {"id": 4, "userId": 42, "name": "外部歌单", "subscribed": True},
            {"id": 5, "userId": 9, "name": "MV", "type": "VIDEO"},
        ]
        self.ids = {1: {11, 12, 13, 14, 17}, 2: {11, 20, 21}, 3: {13, 20}, 4: {12, 22}, 5: {14, 23}}
        self.mutations = []
        self.detail_queries = []
        self.playlist_queries = []
        self.uid = 9

    def login_status(self):
        return {"userId": self.uid}

    def user_playlists(self, uid):
        return self.playlists

    def playlist_ids(self, playlist_id):
        self.playlist_queries.append(playlist_id)
        return self.ids[playlist_id].copy()

    def song_details(self, song_ids):
        self.detail_queries.extend(song_ids)
        titles = {12: "你と海", 14: "바다", 20: "Night", 21: "Река", 22: "海洋"}
        return [
            {"id": song_id, "name": titles[song_id], "ar": [{"name": "Artist"}], "al": {"name": "Album"}}
            for song_id in song_ids if song_id in titles
        ]

    def like_track(self, song_id):
        self.mutations.append(("like", song_id))
        self.ids[1].add(song_id)

    def create_playlist(self, name):
        self.mutations.append(("create", name))
        playlist_id = 90
        self.playlists.append({"id": playlist_id, "userId": self.uid, "name": name, "privacy": 0})
        self.ids[playlist_id] = set()
        return playlist_id

    def add_playlist_tracks(self, playlist_id, song_ids):
        self.mutations.append(("add", playlist_id, tuple(song_ids)))
        self.ids[playlist_id].update(song_ids)


class CollectionSyncTest(unittest.TestCase):
    def setUp(self):
        self.api = CollectionApi()

    def test_scan_exact_id_differences_uses_only_details_for_those_differences(self):
        result = scan_collection(self.api, 9, progress)
        groups = result.snapshot.groups
        self.assertEqual((result.liked_count, result.child_count), (5, 2))
        self.assertEqual([song.song_id for song in groups["日语"]], [12])
        self.assertEqual([song.song_id for song in groups["韩语"]], [14])
        unsure = groups["其他/待确认"][0]
        self.assertEqual((unsure.song_id, unsure.title), (17, ""))
        self.assertEqual([song.song_id for song in result.missing_likes], [20, 21])
        sources = result.missing_likes[0].sources
        self.assertEqual([(playlist.id, playlist.name) for playlist in sources], [(2, "通勤"), (3, "书房")])
        self.assertEqual(self.api.detail_queries, [12, 14, 17, 20, 21])
        self.assertEqual(self.api.playlist_queries, [1, 2, 3])
        self.assertTrue(result.warnings)
        self.assertEqual(self.api.mutations, [])

    def test_sources_preserve_distinct_owned_playlists_with_the_same_name(self):
        self.api.playlists[2]["name"] = "通勤"
        result = scan_collection(self.api, 9, progress)
        sources = result.missing_likes[0].sources
        self.assertEqual([(playlist.id, playlist.name) for playlist in sources], [(2, "通勤"), (3, "通勤")])

    def test_partial_playlist_membership_does_not_create_a_misleading_snapshot(self):
        with patch.object(self.api, "playlist_ids", side_effect=[{11}, ApiError("列表不完整")]):
            with self.assertRaisesRegex(ApiError, "通勤"):
                scan_collection(self.api, 9, progress)

    def test_like_progress_finishes_verification_before_starting_write_progress(self):
        events = []
        with patch("ncm_checker.collection_sync.wait_until", return_value=True):
            report = add_missing_likes(
                self.api, 9, [20, 21],
                lambda percent, _: events.append(("check", percent)),
                lambda: False,
                write_progress=lambda percent, _: events.append(("write", percent)),
            )
        check = [percent for phase, percent in events if phase == "check"]
        write = [percent for phase, percent in events if phase == "write"]
        self.assertEqual(report.verified, {20, 21})
        self.assertEqual((check[0], check[-1]), (0, 100))
        self.assertEqual((write[0], write[-1]), (0, 100))
        self.assertIn(50, write)
        self.assertEqual(write, sorted(write))
        self.assertEqual(events[len(check) - 1:len(check) + 1], [("check", 100), ("write", 0)])

    def test_no_write_progress_when_songs_are_already_liked(self):
        check = []
        write = []
        report = add_missing_likes(
            self.api, 9, [11], lambda percent, _: check.append(percent), lambda: False,
            write_progress=lambda percent, _: write.append(percent),
        )
        self.assertEqual(report.already_present, {11})
        self.assertEqual(check[-1], 100)
        self.assertEqual(write, [])

    def test_public_playlist_write_stage_starts_with_creation(self):
        events = []
        report = create_category_playlist(
            self.api, 9, "示例", [12],
            lambda percent, msg: events.append(("check", percent, msg)), lambda: False,
            write_progress=lambda percent, msg: events.append(("write", percent, msg)),
        )
        self.assertEqual(report.verified, {12})
        first_write = next(index for index, event in enumerate(events) if event[0] == "write")
        self.assertEqual(events[first_write - 1][:2], ("check", 100))
        self.assertEqual(events[first_write], ("write", 0, "创建公开歌单"))
        self.assertEqual(events[-1][:2], ("write", 100))

    def test_retries_rate_limited_like_after_verification_without_resending_applied_song(self):
        append = self.api.like_track
        pacing = []
        cooldowns = []

        def like(song_id):
            append(song_id)
            if song_id == 20:
                raise ApiRateLimitError(405)

        def wait(deadline, _should_stop, countdown=None):
            if countdown:
                cooldowns.append(deadline)
            else:
                pacing.append(deadline)
            return True

        with (patch.object(self.api, "like_track", side_effect=like),
              patch("ncm_checker.collection_sync.wait_until", side_effect=wait),
              patch("ncm_checker.collection_sync.time.monotonic", return_value=100)):
            report = add_missing_likes(self.api, 9, [11, 20, 20, 21, 22], progress, lambda: False)
        self.assertEqual(report.already_present, {11})
        self.assertEqual(report.verified, {20, 21})
        self.assertEqual(set(report.skipped), {22})
        self.assertFalse(report.pending)
        self.assertEqual(self.api.mutations, [("like", 20), ("like", 21)])
        self.assertEqual(cooldowns, [160])
        self.assertEqual(pacing, [0, 104])

    def test_stop_during_cooldown_verifies_only_in_flight_write(self):
        append = self.api.like_track
        stopped = False

        def like(song_id):
            append(song_id)
            raise ApiRateLimitError(405)

        def wait(_deadline, _should_stop, countdown=None):
            nonlocal stopped
            stopped = countdown is not None
            return not stopped

        with (patch.object(self.api, "like_track", side_effect=like),
              patch("ncm_checker.collection_sync.wait_until", side_effect=wait)):
            report = add_missing_likes(self.api, 9, [20, 21], progress, lambda: stopped)
        self.assertTrue(report.stopped)
        self.assertEqual(report.verified, {20})
        self.assertEqual(report.pending, {21})
        self.assertEqual(self.api.mutations, [("like", 20)])

    def test_stale_category_skips_moved_and_unliked_tracks_and_continues_same_id(self):
        original_add = self.api.add_playlist_tracks

        def partial_add(playlist_id, song_ids):
            original_add(playlist_id, song_ids[:1])
            raise ApiError("部分歌曲添加失败")

        with patch.object(self.api, "add_playlist_tracks", side_effect=partial_add):
            first = create_category_playlist(self.api, 9, "示例", [12, 14, 13, 22], progress, lambda: False)
        self.assertEqual(first.playlist_id, 90)
        self.assertEqual(first.verified, {12})
        self.assertEqual(set(first.failures), {14})
        self.assertEqual(set(first.skipped), {13, 22})
        second = create_category_playlist(self.api, 9, "示例", [12, 14], progress, lambda: False, playlist_id=90)
        self.assertEqual(second.already_present, {12})
        self.assertEqual(second.verified, {14})
        self.assertEqual(self.api.mutations, [("create", "示例"), ("add", 90, (12,)), ("add", 90, (14,))])

    def test_uncertain_create_is_never_automatically_reissued(self):
        with patch.object(self.api, "create_playlist", side_effect=ApiConnectionError("结果未知")) as create:
            report = create_category_playlist(self.api, 9, "示例", [12], progress, lambda: False)
        create.assert_called_once_with("示例")
        self.assertTrue(report.creation_uncertain)
        self.assertIsNone(report.playlist_id)
        self.assertEqual(report.pending, {12})
        self.assertEqual(self.api.mutations, [])

    def test_continuation_rejects_target_that_is_now_private(self):
        playlist_id = self.api.create_playlist("示例")
        self.api.playlists[-1]["privacy"] = 10
        report = create_category_playlist(self.api, 9, "示例", [12], progress, lambda: False,
                                          playlist_id=playlist_id)
        self.assertIn("公开", report.message)
        self.assertEqual(report.pending, {12})
        self.assertEqual(self.api.mutations, [("create", "示例")])

    def test_account_mismatch_does_not_send_a_write(self):
        self.api.uid = 10
        report = create_category_playlist(self.api, 9, "示例", [12], progress, lambda: False)
        self.assertIn("账号", report.message)
        self.assertEqual(self.api.mutations, [])
