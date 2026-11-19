import unittest
from unittest.mock import patch

from ncm_checker.api import NcmApi
from ncm_checker.catalog import compare_catalog
from ncm_checker.scanner import Occurrence, Playlist


class FakeApi:
    def __init__(self):
        self.searches = []
        self.red_queries = []

    def search_song_page(self, keywords, offset, _limit):
        self.searches.append((keywords, offset))
        if keywords == "歌名" and offset == 0:
            return [{"id": 11, "name": "歌名"}, {"id": 80, "name": "歌名续集 (Live)"}], 3
        if keywords == "歌名":
            return [{"id": 12, "name": "歌 名（现场版）", "al": {"name": "新专辑"}}], 3
        return [{"id": 12, "name": "歌名"}, {"id": 13, "name": "歌名"}], 2

    def red_count(self, song_id):
        self.red_queries.append(song_id)
        return {11: 0, 12: 600, 13: None}[song_id]


class CatalogTest(unittest.TestCase):
    def test_paged_name_and_artist_matches_rank_higher_heart_count(self):
        api = FakeApi()
        home = Playlist(1, "收藏夹", 1, "喜欢")
        gym = Playlist(2, "跑步", 1, "自建")
        occurrences = [Occurrence(11, "歌名", "歌手", home, "旧专辑"), Occurrence(11, "歌名", "歌手", gym)]
        result = compare_catalog(api, {"歌名": occurrences}, lambda *_: None)
        self.assertEqual(api.searches, [("歌名", 0), ("歌名", 2), ("歌名 歌手", 0)])
        self.assertEqual(api.red_queries, [11, 12, 13])
        self.assertEqual([row.song_id for row in result.groups[0].tracks], [12, 11, 13])
        external, existing, unknown = result.groups[0].tracks
        self.assertEqual((external.hearts, external.album, external.playlists), (600, "新专辑", ()))
        self.assertEqual((existing.hearts, existing.album, existing.playlists), (0, "旧专辑", ("收藏夹", "跑步")))
        self.assertIsNone(unknown.hearts)

    def test_reports_page_limit(self):
        api = FakeApi()
        row = Occurrence(11, "歌名", "", Playlist(1, "歌单", 1, "喜欢"))
        with patch("ncm_checker.catalog.TITLE_PAGES", 1):
            result = compare_catalog(api, {"歌名": [row]}, lambda *_: None)
        self.assertTrue(any("页数上限" in note for note in result.notes))

    def test_search_transport_and_exact_decimal_red_count(self):
        api = NcmApi(3100)
        with patch.object(api, "request", return_value={"result": {"songCount": 0}}) as request:
            self.assertEqual(api.search_song_page("歌名", 50, 50), ([], 0))
        request.assert_called_once_with("/cloudsearch", {"keywords": "歌名", "type": 1, "offset": 50, "limit": 50})
        with patch.object(api, "request", return_value={"data": {"count": "638000"}}):
            self.assertEqual(api.red_count(123), 638000)
