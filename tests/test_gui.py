import base64
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from unittest.mock import patch

from PySide6.QtGui import QColor, QImage, QPalette
from PySide6.QtCore import QBuffer, QEvent, QIODevice, QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QDialogButtonBox, QStyle, QStyleOptionButton, QStyleOptionViewItem, QTreeWidget,
)

from ncm_checker.gui import CHECKED, UNCHECKED, USER, AccountRead, MainWindow, configure_theme
from ncm_checker.scanner import playlists_for


def close_window(window):
    window.close()
    window.deleteLater()
    QApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)


class GuiFlowTest(unittest.TestCase):
    def test_feature_entries_align_and_cross_playlist_checkbox_paints_and_toggles(self):
        app = QApplication.instance() or QApplication(["test"])
        dark = app.palette()
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Button, QPalette.ColorRole.Base):
            dark.setColor(role, QColor("#101019"))
        app.setPalette(dark)
        configure_theme(app)
        window = MainWindow(3100)
        window.show()
        app.processEvents()
        try:
            first, second, third = window.feature_buttons
            self.assertEqual({entry.geometry().top() for entry in window.feature_buttons}, {first.geometry().top()})
            widths = [entry.width() for entry in window.feature_buttons]
            self.assertLessEqual(max(widths) - min(widths), 1)
            self.assertLess(first.geometry().right(), second.geometry().left())
            self.assertLess(second.geometry().right(), third.geometry().left())
            self.assertIs(window.pages.currentWidget(), window.overview_page)
            second.click()
            self.assertIs(window.pages.currentWidget(), window.collection_page)
            self.assertIs(window.collection_page.window(), window)
            self.assertFalse(window.main_footer.isVisible())
            self.assertFalse(window.collection_page.scan_button.isEnabled())
            first.click()
            self.assertIs(window.pages.currentWidget(), window.playlist_page)
            option = QStyleOptionButton()
            check = window.combine
            check.initStyleOption(option)
            indicator = check.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, check)
            # The sample point lies inside the fill and above the check mark.
            pixel = indicator.topLeft() + QPoint(5, 4)

            def indicator_color():
                pixmap = check.grab()
                return pixmap.toImage().pixelColor(pixel * pixmap.devicePixelRatio()).name()

            self.assertFalse(check.isChecked())
            self.assertEqual(indicator_color(), "#ffffff")
            QTest.mouseClick(check, Qt.MouseButton.LeftButton, pos=indicator.center())
            self.assertTrue(check.isChecked())
            self.assertEqual(indicator_color(), "#4c59b0")
            QTest.keyClick(check, Qt.Key.Key_Space)
            self.assertFalse(check.isChecked())
        finally:
            close_window(window)

    def test_management_loads_owned_playlists_and_applies_only_visible_selections(self):
        app = QApplication.instance() or QApplication(["test"])
        configure_theme(app)
        tracks = {1: [25], 10: [21, 22], 20: [21, 23], 30: [], 60: [23], 90: [25]}
        playlist_names = {1: "我喜欢的音乐", 10: "通勤", 20: "周末", 30: "目标", 60: "视频", 90: "收藏"}
        titles = {21: "日落", 22: "山谷", 23: "湖面"}
        reads = []
        writes = []

        class FixtureApi:
            def __init__(self, _port, check_cancel):
                self.check_cancel = check_cancel

            def login_status(self):
                self.check_cancel()
                return {"userId": 99, "nickname": "Test"}

            def user_playlists(self, _uid):
                return [
                    {"id": pid, "name": name, "userId": 55 if pid == 90 else 99,
                     "subscribed": pid == 90, "trackCount": len(tracks[pid]),
                     "specialType": 5 if pid == 1 else 0, "video": pid == 60}
                    for pid, name in playlist_names.items()
                ]

            def playlist_ids(self, pid):
                self.check_cancel()
                reads.append(pid)
                return tracks[pid].copy()

            def song_details(self, song_ids):
                self.check_cancel()
                return [{"id": sid, "name": titles[sid], "ar": [{"name": "歌手"}]} for sid in song_ids]

            def like_track(self, sid):
                writes.append(("like", sid))
                tracks[1].append(sid)

            def add_playlist_tracks(self, pid, song_ids):
                writes.append(("add", pid, tuple(song_ids)))
                tracks[pid].extend(song_ids)

            def remove_playlist_tracks(self, pid, song_ids):
                writes.append(("remove", pid, tuple(song_ids)))
                tracks[pid][:] = [sid for sid in tracks[pid] if sid not in song_ids]

        window = MainWindow(3100, api_factory=FixtureApi)
        window.show()
        page = window.management_page

        def wait_idle():
            deadline = time.monotonic() + 5
            while (window.job is not None or page.busy) and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            self.assertFalse(window._is_busy())
            app.processEvents()

        def group(playlist_id):
            return next(
                item for index in range(page.track_table.topLevelItemCount())
                if (item := page.track_table.topLevelItem(index)).data(0, USER) == playlist_id
            )

        def filter_to(playlist_id):
            index = page.playlist_filter.findData(playlist_id)
            self.assertGreaterEqual(index, 0)
            page.playlist_filter.setCurrentIndex(index)
            app.processEvents()

        def choose_target(_parent, _title, _prompt, items, *_args, **_kwargs):
            return next(item for item in items if "目标" in item), True

        try:
            window.management_button.click()
            self.assertIs(window.pages.currentWidget(), page)
            self.assertFalse(page.refresh_button.isEnabled())
            self.assertEqual(reads, [])
            api = FixtureApi(3100, lambda: None)
            window._launch("login", lambda _worker: AccountRead(
                {"MUSIC_U": "fixture"}, api.login_status(), playlists_for(api, 99),
            ), "模拟登录")
            wait_idle()
            self.assertEqual(set(reads), {10, 20, 30})
            self.assertIs(window.pages.currentWidget(), page)
            self.assertFalse(window.main_footer.isVisible())
            self.assertEqual(group(10).childCount(), 2)
            self.assertEqual(group(20).childCount(), 2)
            self.assertFalse(page.like_button.isEnabled())

            group(10).setCheckState(0, CHECKED)
            app.processEvents()
            self.assertEqual(group(10).child(1).checkState(0), CHECKED)
            filter_to(20)
            page.search_input.setText("日落")
            app.processEvents()
            self.assertEqual(group(20).childCount(), 1)
            self.assertFalse(page.like_button.isEnabled())
            group(20).child(0).setCheckState(0, CHECKED)
            app.processEvents()

            with patch.object(page, "_confirm", return_value=True), patch(
                "ncm_checker.management_ui.QInputDialog.getItem", side_effect=choose_target,
            ):
                page.like_button.click()
                wait_idle()
                self.assertEqual(writes, [("like", 21)])

                page.playlist_filter.setCurrentIndex(0)
                app.processEvents()
                group(10).setCheckState(0, CHECKED)
                group(20).setCheckState(0, CHECKED)
                app.processEvents()
                page.copy_button.click()
                wait_idle()
                self.assertEqual(writes[-1], ("add", 30, (21,)))
                self.assertIn(21, tracks[10])
                self.assertIn(21, tracks[20])

            filter_to(20)
            group(20).child(0).setCheckState(0, CHECKED)
            app.processEvents()
            with patch.object(page, "_confirm", return_value=False) as confirm:
                page.remove_button.click()
                confirm.assert_called_once()
                self.assertEqual(len(writes), 2)
            with patch.object(page, "_confirm", return_value=True):
                page.remove_button.click()
                wait_idle()
            self.assertEqual(writes[-1], ("remove", 20, (21,)))
            self.assertEqual(tracks, {1: [25, 21], 10: [21, 22], 20: [23], 30: [21], 60: [23], 90: [25]})

            window._receive("forget", None)
            self.assertIs(window.pages.currentWidget(), window.overview_page)
            self.assertEqual(page.track_table.topLevelItemCount(), 0)
        finally:
            close_window(window)

    def test_qr_login_scan_checkbox_clicks_and_confirmed_unlike(self):
        app = QApplication.instance() or QApplication(["test"])
        app.setStyle("Fusion")
        calls = []
        tracks = [21, 22]
        confirmations = []
        pauses = []

        class MemorySessionStore:
            def __init__(self):
                self.cookie = None

            def available(self):
                return True

            def load(self):
                return self.cookie

            def save(self, cookie):
                self.cookie = cookie.copy()

            def forget(self):
                self.cookie = None
        image = QImage(24, 24, QImage.Format.Format_RGB32)
        image.fill(QColor("white"))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        qr = "data:image/png;base64," + base64.b64encode(buffer.data()).decode()
        buffer.close()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                path = urlsplit(self.path).path
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((path, body, self.path))
                status = 200
                if path == "/user/playlist":
                    offset = body["offset"]
                    playlist = {"id": offset + 1, "name": "我喜欢的音乐" if offset == 0 else "通勤",
                                "trackCount": len(tracks), "specialType": 5 if offset == 0 else 0,
                                "creator": {"userId": 99}}
                    rows = [playlist]
                    if offset != 0:
                        rows.append({"id": 888, "name": "他人的歌单", "creator": {"userId": 55}, "subscribed": True})
                    fixture = {"playlist": rows, "more": offset == 0}
                elif path == "/like" and body.get("like") == "false":
                    if body["id"] == 22 and sum(p == "/like" and b.get("id") == 22 for p, b, _ in calls) == 1:
                        status = 405
                        fixture = {"code": 405, "msg": "操作频繁，请稍候再试"}
                    else:
                        tracks.remove(body["id"])
                        fixture = {"code": 200}
                elif path == "/cloudsearch":
                    fixture = {"result": {"songCount": 1, "songs": [
                        {"id": 41, "name": "晴 天（重录版）", "ar": [{"name": "周杰伦"}], "al": {"name": "其他专辑"}},
                    ]}}
                else:
                    fixture = {
                        "/login/qr/key": {"data": {"unikey": "fake"}},
                        "/login/qr/create": {"data": {"qrimg": qr}},
                        "/login/qr/check": {"code": 803, "cookie": "MUSIC_U=fixture-token; Path=/"},
                        "/login/status": {"data": {"profile": {"userId": 99, "nickname": "Test"}}},
                        "/playlist/detail": {
                            "playlist": {"trackIds": [{"id": sid} for sid in tracks], "trackCount": len(tracks)}
                        },
                        "/song/detail": {
                            "songs": [
                                {"id": 21, "name": "晴天", "ar": [{"name": "周杰伦"}]},
                                {"id": 22, "name": "晴 天（）", "ar": [{"name": "翻唱歌手"}]},
                            ],
                            "privileges": [{"id": 21, "st": -200}, {"id": 22, "st": 0, "pl": 128000}],
                        },
                        "/song/copyright/rcmd": {"data": {"songs": [{"id": 32, "name": "其他专辑"}]}},
                        "/song/red/count": {"data": {"count": {21: 60, 41: 8000}.get(body.get("id"), 0)}},
                        "/comment/info/list": {"data": [
                            {"resourceId": 21, "commentCount": 300}, {"resourceId": 22, "commentCount": 19},
                        ]},
                    }.get(path, {"code": 404})
                data = json.dumps(fixture).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        def wait_idle(window):
            deadline = time.monotonic() + 8
            while window.job is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            self.assertIsNone(window.job)

        def click_indicator(table, index):
            option = QStyleOptionViewItem()
            option.widget = table
            option.rect = table.visualRect(index)
            rect = table.itemDelegate().indicator_rect(option, index)
            self.assertTrue(rect.isValid())
            QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())

        def respond_to_confirmation(button_text):
            dialog = app.activeModalWidget()
            group = dialog.findChild(QTreeWidget).topLevelItem(0)
            confirmations.append((group.text(2), [group.child(i).text(1) for i in range(group.childCount())]))
            buttons = dialog.findChild(QDialogButtonBox)
            for button in buttons.buttons():
                if button.text() == button_text:
                    button.click()
                    return
            dialog.reject()

        proxy = "http://127.0.0.1:7897"

        def wait_for_test(_deadline, should_stop, countdown=None):
            if countdown:
                pauses.append(60)
                countdown(60)
            return not should_stop()

        with (patch.dict("os.environ", {"NCM_PROXY": proxy}),
              patch("ncm_checker.removal.wait_until", side_effect=wait_for_test),
              ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                store = MemorySessionStore()
                window = MainWindow(server.server_port, session_store=store)
                window.show()
                window._login_qr()
                wait_idle(window)
                self.assertEqual(window.session.text(), "Test  ·  UID 99")
                self.assertEqual(window.playlist_table.topLevelItemCount(), 2)
                self.assertEqual(store.cookie, {"MUSIC_U": "fixture-token"})
                close_window(window)
                window = MainWindow(server.server_port, session_store=store)
                window.show()
                window.restore_session()
                wait_idle(window)
                self.assertIn("已恢复上次登录", window.status.text())
                self.assertEqual(window.session.text(), "Test  ·  UID 99")
                self.assertIs(window.pages.currentWidget(), window.overview_page)
                self.assertEqual(window.owned_count.text(), "1 个")
                self.assertEqual(window.liked_count.text(), "2 首")
                self.assertTrue(window.collection_button.isEnabled())
                window.collection_button.click()
                self.assertTrue(window.collection_page.scan_button.isEnabled())
                window.duplicate_button.click()
                self.assertFalse(window.scan.isEnabled())
                click_indicator(window.playlist_table, window.playlist_table.model().index(0, 0))
                self.assertTrue(window.scan.isEnabled())
                window.scan.click()
                wait_idle(window)
                self.assertIs(window.pages.currentWidget(), window.results_page)
                self.assertEqual(window.progress.value(), 100)
                self.assertEqual(window.result_table.topLevelItemCount(), 1)
                window.collection_button.click()
                window.duplicate_button.click()
                self.assertIs(window.pages.currentWidget(), window.results_page)
                group = window.result_table.topLevelItem(0)
                self.assertEqual(group.childCount(), 2)
                self.assertEqual([group.child(0).text(i) for i in range(5)], ["晴天", "周杰伦", "60", "300", "我喜欢的音乐"])
                self.assertEqual(group.child(1).text(2), "0")
                self.assertEqual(group.child(0).text(5), "其他版本可播")
                self.assertIn("32", group.child(0).toolTip(5))
                table = window.result_table
                table.setCurrentItem(group.child(0))
                window.catalog_button.menu().actions()[0].trigger()
                wait_idle(window)
                catalog = app.activeModalWidget()
                self.assertEqual(catalog.windowTitle(), "站内同名版本")
                first_match = catalog.findChild(QTreeWidget).topLevelItem(0).child(0)
                self.assertEqual([first_match.text(i) for i in (3, 4, 5)], ["8,000", "站内候选", "41"])
                catalog.reject()
                self.assertIn("0 / 2", window.result_selection.text())
                group_index = table.model().index(0, 0)
                first = table.model().index(0, 0, group_index)
                second = table.model().index(1, 0, group_index)
                click_indicator(table, first)
                self.assertIn("1 / 2", window.result_selection.text())
                self.assertEqual(group.checkState(0), Qt.CheckState.PartiallyChecked)
                second_cell = table.visualRect(second)
                QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton,
                                 pos=QPoint(second_cell.x() + 85, second_cell.center().y()))
                self.assertIn("2 / 2", window.result_selection.text())
                click_indicator(table, group_index)
                self.assertIn("0 / 2", window.result_selection.text())
                click_indicator(table, group_index)
                self.assertIn("2 / 2", window.result_selection.text())
                window.clear_results.click()
                self.assertEqual(group.child(0).checkState(0), UNCHECKED)
                self.assertFalse(window.remove.isEnabled())
                click_indicator(table, first)
                self.assertTrue(window.remove.isEnabled())
                QTimer.singleShot(0, lambda: respond_to_confirmation("取消"))
                window.remove.click()
                self.assertIsNone(window.job)
                self.assertEqual(tracks, [21, 22])
                QTimer.singleShot(0, lambda: respond_to_confirmation("确认移除"))
                window.remove.click()
                wait_idle(window)
                self.assertIn("已移除 1 条", window.status.text())
                self.assertEqual(table.topLevelItem(0).childCount(), 1)
                self.assertEqual(table.topLevelItem(0).child(0).text(1), "翻唱歌手")
                self.assertEqual(window.playlist_table.topLevelItem(0).text(2), "1")
                remaining_group = table.model().index(0, 0)
                click_indicator(table, table.model().index(0, 0, remaining_group))
                QTimer.singleShot(0, lambda: respond_to_confirmation("确认移除"))
                window.remove.click()
                wait_idle(window)
                self.assertIn("已移除 1 条", window.status.text())
                self.assertEqual(table.topLevelItemCount(), 0)
                self.assertFalse(window.remove.isEnabled())
                self.assertEqual(tracks, [])
                window.logout_button.click()
                wait_idle(window)
                self.assertIsNone(store.cookie)
                self.assertEqual(window.session.text(), "未登录")
                self.assertIs(window.pages.currentWidget(), window.overview_page)
                window.collection_button.click()
                self.assertIs(window.pages.currentWidget(), window.collection_page)
                self.assertFalse(window.collection_page.scan_button.isEnabled())
                close_window(window)
            finally:
                server.shutdown()
                thread.join()

        self.assertEqual([body["offset"] for path, body, _ in calls if path == "/user/playlist"], [0, 1] * 5)
        self.assertEqual([path for path, _, _ in calls].count("/login/qr/key"), 1)
        self.assertEqual([body["id"] for path, body, _ in calls if path == "/playlist/detail"], [1] * 7)
        self.assertEqual({body["keywords"] for path, body, _ in calls if path == "/cloudsearch"},
                         {"晴天", "晴天 周杰伦", "晴天 翻唱歌手"})
        self.assertEqual([(body["id"], body["like"]) for path, body, _ in calls if path == "/like"],
                         [(21, "false"), (22, "false"), (22, "false")])
        self.assertEqual(pauses, [60])
        self.assertEqual(confirmations, [("取消红心", ["周杰伦"]), ("取消红心", ["周杰伦"]),
                                         ("取消红心", ["翻唱歌手"])])
        self.assertTrue(all(body.get("proxy") == proxy for _, body, _ in calls))
        self.assertTrue(all("fixture-token" not in url for _, _, url in calls))
