import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem, QVBoxLayout, QWidget

from ncm_checker.api import NcmApi
from ncm_checker.classification import (
    ClassifiedSong, CollectionTrack, build_snapshot, load_snapshot, save_snapshot, snapshot_path,
)
from ncm_checker.collection_sync import CollectionScan, SyncReport
from ncm_checker.collection_ui import CollectionPage
from ncm_checker.gui import CHECKED, UNCHECKED, USER
from ncm_checker.scanner import Playlist


class CollectionPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test-collection-page"])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        storage = patch(
            "ncm_checker.classification.snapshot_path",
            side_effect=lambda uid, *, directory=None: snapshot_path(uid, directory=directory or Path(temp.name)),
        )
        storage.start()
        self.addCleanup(storage.stop)

    def wait_for(self, condition):
        deadline = time.monotonic() + 4
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(condition())

    def wait_idle(self, dialog):
        self.wait_for(lambda: not dialog.busy)

    def make_page(self, uid):
        parent = QWidget()

        def close_parent():
            parent.close()
            parent.deleteLater()
            self.app.sendPostedEvents(parent, QEvent.Type.DeferredDelete)

        self.addCleanup(close_parent)
        layout = QVBoxLayout(parent)
        page = CollectionPage(parent, NcmApi, 3100)
        layout.addWidget(page)
        page.set_session({"MUSIC_U": "fixture"}, uid)
        parent.resize(1000, 690)
        parent.show()
        self.app.processEvents()
        self.assertFalse(page.isWindow())
        return page

    def test_scan_load_likes_and_continue_partial_public_playlist(self):
        uid = 88
        snapshot = build_snapshot(uid, [ClassifiedSong(11, "かな"), ClassifiedSong(12, "夢の中")])
        commute = Playlist(10, "通勤", 1, "自建", editable=True)
        missing = (CollectionTrack(13, "到不了", "歌手", "", (commute,)),)
        preview = CollectionScan(
            snapshot, missing, liked_count=2, child_count=3,
            warnings=[],
        )
        dialog = self.make_page(uid)
        self.assertIn("加载上次结果", dialog.category_summary.text())
        self.assertIn("加载上次结果", dialog.likes_summary.text())
        with patch("ncm_checker.collection_ui.scan_collection", return_value=preview) as scan:
            dialog.scan_button.click()
            self.wait_idle(dialog)
            scan.assert_called_once()
            self.assertEqual(load_snapshot(uid), replace(snapshot, missing_likes=missing))

            dialog = self.make_page(uid)
            dialog.tabs.setCurrentIndex(1)
            dialog.load_button.click()
            scan.assert_called_once()
            self.assertEqual(dialog.tabs.currentIndex(), 1)
            self.assertEqual(dialog.category_table.topLevelItemCount(), 1)
            self.assertEqual(dialog.tabs.tabText(0), "待分类  2")
            self.assertEqual(dialog.tabs.tabText(1), "待补红心  1")
            self.assertIn("待分类和待补红心", dialog.status.text())
            self.assertIn("扫描于", dialog.likes_summary.text())
            self.assertEqual(dialog.like_selection.text(), "已选 1 / 1 首")
            self.assertEqual(dialog.likes_table.topLevelItem(0).text(0), "通勤  ·  1 首")
            self.assertTrue(dialog.like_button.isEnabled())

        changed = []
        dialog.playlists_changed.connect(lambda: changed.append(True))
        with (
            patch("ncm_checker.collection_ui.add_missing_likes", return_value=SyncReport(verified={13})) as likes,
            patch("ncm_checker.collection_ui.create_category_playlist", side_effect=[
                SyncReport(verified={11}, pending={12}, playlist_id=511),
                SyncReport(verified={12}, playlist_id=511),
            ]) as publish,
            patch("ncm_checker.collection_ui.QInputDialog.getText", return_value=("日语新歌单", True)) as name,
            patch.object(dialog, "_confirm", return_value=True),
        ):
            dialog.like_button.click()
            self.wait_idle(dialog)
            self.assertEqual(likes.call_args.args[2], [13])
            self.assertEqual(dialog.likes_table.topLevelItemCount(), 0)
            self.assertEqual(load_snapshot(uid).missing_likes, ())
            self.assertEqual(changed, [True])

            dialog.tabs.setCurrentIndex(0)
            dialog.publish_buttons[0].click()
            self.wait_idle(dialog)
            self.assertEqual(dialog.publish_buttons[0].text(), "继续添加")
            self.assertEqual([song.song_id for song in load_snapshot(uid).groups["日语"]], [12])
            dialog.publish_buttons[0].click()
            self.wait_idle(dialog)
            first_call, second_call = publish.call_args_list
            self.assertEqual(first_call.args[2:4], ("日语新歌单", [11, 12]))
            self.assertIsNone(first_call.kwargs["playlist_id"])
            self.assertEqual(second_call.args[2:4], ("日语新歌单", [12]))
            self.assertEqual(second_call.kwargs["playlist_id"], 511)
            name.assert_called_once()
            dialog.load_button.click()
            self.assertIn("当前分类结果为空", dialog.category_summary.text())
            self.assertIn("当前待补红心列表为空", dialog.likes_summary.text())

    def test_loading_legacy_classification_leaves_pending_likes_unavailable(self):
        uid = 88
        snapshot = build_snapshot(uid, [ClassifiedSong(11, "かな")])
        save_snapshot(snapshot)
        dialog = self.make_page(uid)
        dialog.tabs.setCurrentIndex(1)
        dialog.load_button.click()
        self.assertEqual(dialog.snapshot, snapshot)
        self.assertEqual(dialog.tabs.currentIndex(), 1)
        self.assertEqual(dialog.category_table.topLevelItemCount(), 1)
        self.assertIn("扫描当前歌单", dialog.likes_summary.text())
        self.assertFalse(dialog.like_button.isEnabled())

    def test_group_toggle_preserves_other_playlist_choices_and_likes_shared_id_only_once(self):
        uid = 88
        first = Playlist(10, "通勤", 2, "自建", editable=True)
        second = Playlist(20, "通勤", 2, "自建", editable=True)
        third = Playlist(30, "周末", 1, "自建", editable=True)
        dialog = self.make_page(uid)
        missing = (
            CollectionTrack(13, "共享", "", "", (first, second)),
            CollectionTrack(14, "一", "", "", (first, third)),
            CollectionTrack(15, "二", "", "", (second,)),
        )
        dialog.snapshot = replace(build_snapshot(uid, []), missing_likes=missing)
        dialog._render_likes()
        dialog.tabs.setCurrentIndex(1)
        dialog.show()
        self.app.processEvents()
        view = dialog.likes_table
        groups = {view.topLevelItem(i).data(0, USER): view.topLevelItem(i) for i in range(view.topLevelItemCount())}
        self.assertEqual(set(groups), {10, 20, 30})
        self.assertIn("（10）", groups[10].text(0))
        self.assertIn("（20）", groups[20].text(0))
        self.assertEqual(dialog.like_selection.text(), "已选 3 / 3 首")

        index = view.indexFromItem(groups[10])
        option = QStyleOptionViewItem()
        option.widget = view
        option.rect = view.visualRect(index)
        QTest.mouseClick(
            view.viewport(), Qt.MouseButton.LeftButton,
            pos=view.itemDelegate().indicator_rect(option, index).center(),
        )
        self.assertEqual(groups[10].child(0).checkState(0), UNCHECKED)
        self.assertEqual(groups[10].child(1).checkState(0), UNCHECKED)
        self.assertEqual(groups[20].checkState(0), CHECKED)
        self.assertEqual(dialog._selected_likes(), {13, 14, 15})

        groups[20].child(1).setCheckState(0, UNCHECKED)
        self.assertEqual(groups[20].checkState(0), Qt.CheckState.PartiallyChecked)
        groups[10].child(0).setCheckState(0, CHECKED)
        self.assertEqual(dialog._checked_likes_rows(), {(10, 13), (20, 13), (30, 14)})
        with (
            patch.object(dialog, "_confirm", return_value=True) as confirm,
            patch("ncm_checker.collection_ui.add_missing_likes", return_value=SyncReport(
                verified={13}, pending={14},
            )) as likes,
        ):
            dialog._like_selected()
            self.wait_idle(dialog)
        self.assertEqual(likes.call_args.args[2], [13, 14])
        self.assertIn("2 首", confirm.call_args.args[1])
        self.assertEqual(dialog._checked_likes_rows(), {(30, 14)})
        self.assertEqual(dialog.like_selection.text(), "已选 1 / 2 首")
        self.assertEqual(load_snapshot(uid).missing_likes, missing[1:])

    def test_scan_and_like_progress_are_sequential_and_independent(self):
        uid = 88
        page = self.make_page(uid)
        source = Playlist(10, "通勤", 1, "自建", editable=True)
        page.snapshot = replace(
            build_snapshot(uid, []), missing_likes=(CollectionTrack(13, "一首歌", sources=(source,)),),
        )
        page._render_likes()
        finish_scan = Event()
        finish_write = Event()

        def like(_api, _uid, _ids, progress, _stop, *, write_progress):
            progress(62, "正在扫描歌单")
            finish_scan.wait(3)
            progress(100, "扫描完成")
            write_progress(0, "开始补红心")
            finish_write.wait(3)
            write_progress(100, "补红心完成")
            return SyncReport(verified={13})

        with (
            patch.object(page, "_confirm", return_value=True),
            patch("ncm_checker.collection_ui.add_missing_likes", side_effect=like),
        ):
            page._like_selected()
            try:
                self.wait_for(lambda: page.scan_progress.value() == 62)
                self.assertIs(page.progress_bars.currentWidget(), page.scan_progress)
                self.assertEqual(page.progress_value.text(), "扫描 62%")
                finish_scan.set()
                self.wait_for(lambda: page.progress_bars.currentWidget() is page.write_progress)
                self.assertEqual(page.scan_progress.value(), 100)
                self.assertEqual(page.write_progress.value(), 0)
                self.assertEqual(page.progress_value.text(), "补红心 0%")
            finally:
                finish_scan.set()
                finish_write.set()
                self.wait_idle(page)
        self.assertEqual(page.write_progress.value(), 100)
        self.assertEqual(page.likes_table.topLevelItemCount(), 0)
        page.set_session({}, None)
        self.assertIsNone(page.snapshot)
        self.assertFalse(page.load_button.isEnabled())
