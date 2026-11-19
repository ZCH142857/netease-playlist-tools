import unittest
from types import SimpleNamespace

from ncm_checker.api import ApiError
from ncm_checker.normalize import normalize_title
from ncm_checker.scanner import Playlist, collect_duplicates, playlists_for


def track(song_id, name, artist):
    return {"id": song_id, "name": name, "ar": [{"name": artist}]}


class FakeApi:
    def __init__(self):
        self.red_queries = []
        self.comment_queries = []
        self.version_queries = []
        self.tracks = {
            1: [
                dict(track(11, "Ｓｏｎｇ", "A"), privilege={"st": -200, "pl": 0}),
                dict(track(12, "song", "B"), privilege={"st": 0, "pl": 0, "fee": 1}),
                track(13, "Songs (Live)", "A"),
            ],
            2: [track(11, "Song", "A"), track(20, "Only once", "D")],
        }

    def recommended_versions(self, song_id):
        self.version_queries.append(song_id)
        return {42: "Other edition"}

    def playlist_tracks(self, playlist_id):
        return self.tracks[playlist_id], 0

    def red_count(self, song_id):
        self.red_queries.append(song_id)
        return 0 if song_id == 11 else 29

    def comment_counts(self, song_ids):
        self.comment_queries.append(song_ids)
        return {11: 1_000}

    def comment_count(self, song_id):
        raise ApiError("暂无评论数量")


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.playlists = [Playlist(1, "我喜欢的音乐", 3, "喜欢"), Playlist(2, "收藏夹", 2, "自建")]
        self.progress = []

    def scan(self, combine=False):
        return collect_duplicates(self.api, self.playlists, combine, lambda n, _: self.progress.append(n))

    def test_playlists_omit_subscribed_and_unowned_entries(self):
        api = SimpleNamespace(user_playlists=lambda uid: [
            {"id": 1, "name": "我喜欢的音乐", "creator": {"userId": uid}, "specialType": 5},
            {"id": 2, "name": "收藏夹", "userId": str(uid)},
            {"id": 3, "name": "别人的收藏歌单", "creator": {"userId": 100}, "subscribed": True},
            {"id": 4, "name": "归属不同", "userId": 100},
            {"id": 5, "name": "订阅标记优先", "userId": uid, "subscribed": True},
        ])
        self.assertEqual([(row.id, row.category) for row in playlists_for(api, 99)],
                         [(1, "喜欢"), (2, "自建")])

    def test_only_duplicates_are_looked_up_and_unknown_is_distinct_from_zero(self):
        result = self.scan()
        self.assertEqual(list(result.groups), ["song"])
        self.assertEqual([(row.song_id, row.playlist.id) for row in result.groups["song"]], [(11, 1), (12, 1)])
        self.assertEqual(self.api.red_queries, [11, 12])
        self.assertEqual(self.api.comment_queries, [[11, 12]])
        self.assertEqual(result.hearts, {11: 0, 12: 29})
        self.assertEqual(result.comments, {11: 1_000})
        self.assertTrue(result.warnings)
        self.assertEqual(self.progress, sorted(self.progress))
        self.assertEqual(self.progress[-1], 100)

    def test_cross_playlist_groups_retain_all_memberships(self):
        result = self.scan(combine=True)
        self.assertEqual([row.playlist.id for row in result.groups["song"]], [1, 1, 2])
        self.assertEqual(self.api.red_queries, [11, 12])

    def test_recommendations_require_positive_evidence_and_vip_is_not_grey(self):
        result = self.scan(combine=True)
        self.assertEqual(self.api.version_queries, [11])
        self.assertEqual(result.playback[11].status, "其他版本可播")
        self.assertEqual(result.playback[11].versions, ((42, "Other edition"),))
        self.assertEqual(result.playback[12].status, "权限待确认")
        self.api.recommended_versions = lambda _: {}
        self.assertEqual(self.scan().playback[11].status, "此版本受限")

    def test_matches_space_and_parenthetical_variants_with_clear_boundaries(self):
        for variant in ("AAA（xxx版）", "AA A（）", "ＡＡＡ （混音（现场）版）（）"):
            self.assertEqual(normalize_title("AAA"), normalize_title(variant))
        for other in ("AAAA", "AAA 续集", "AA（中间）A", "AAA（没结束"):
            self.assertNotEqual(normalize_title("AAA"), normalize_title(other))
        self.assertEqual(normalize_title("（独立标题）"), "(独立标题)")
