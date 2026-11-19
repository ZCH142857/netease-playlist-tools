import unittest
from unittest.mock import patch

from ncm_checker.api import ApiConnectionError, NcmApi


class CollectionApiTest(unittest.TestCase):
    def test_public_create_sends_explicit_privacy(self):
        api = NcmApi(3100)
        with patch.object(api, "request", return_value={"playlist": {"id": 54321, "privacy": 0}}) as request:
            self.assertEqual(api.create_playlist("  英语曲目  "), 54321)
        request.assert_called_once_with("/playlist/create", {
            "name": "英语曲目", "privacy": "0", "type": "NORMAL",
        })

    def test_create_response_without_id_is_uncertain(self):
        api = NcmApi(3100)
        with patch.object(api, "request", return_value={"code": 200}) as request:
            with self.assertRaises(ApiConnectionError):
                api.create_playlist("测试")
        self.assertEqual(request.call_count, 1)

    def test_writes_and_reads_collection_tracks(self):
        api = NcmApi(3100)
        with patch.object(api, "request", side_effect=[
            {"code": 200}, {"body": {"code": 200}},
            {"songs": [{"id": 8, "name": "English"}, {"id": 9, "name": "中文"}],
             "privileges": [{"id": 8, "st": -200}]},
        ]) as request:
            api.like_track(8)
            api.add_playlist_tracks(10, [8, 9])
            tracks = api.song_details([9, 8, 17])
        self.assertEqual([song["id"] for song in tracks], [9, 8])
        self.assertEqual(tracks[1]["privilege"]["st"], -200)
        self.assertEqual([call.args for call in request.call_args_list], [
            ("/like", {"id": 8, "like": "true"}),
            ("/playlist/tracks", {"op": "add", "pid": 10, "tracks": "8,9"}),
            ("/song/detail", {"ids": "9,8,17"}),
        ])

