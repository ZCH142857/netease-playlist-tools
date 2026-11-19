from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QStackedWidget, QTabWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .api import NcmApi
from .classification import (
    LANGUAGE_ORDER, ClassificationSnapshot, ClassificationStoreError, CollectionTrack, load_snapshot, save_snapshot,
)
from .collection_sync import (
    CollectionScan, SyncReport, add_missing_likes, create_category_playlist, scan_collection,
)
from .gui import CHECKED, UNCHECKED, USER, Worker, button, label, table
from .scanner import Playlist, TrackKey


@dataclass(frozen=True)
class PublishTarget:
    playlist_id: int
    name: str
    song_ids: frozenset[int]


class CollectionWorker(Worker):
    write_progress = Signal(int, str)


class CollectionPage(QWidget):
    busy_changed = Signal(bool)
    playlists_changed = Signal()

    def __init__(self, parent: QWidget, api_factory: Callable, port: int):
        super().__init__(parent)
        self.api_factory = api_factory
        self.port = port
        self.cookie: dict[str, str] = {}
        self.uid: int | None = None
        self.snapshot: ClassificationSnapshot | None = None
        self.publish_targets: dict[str, PublishTarget] = {}
        self.uncertain_names: dict[str, str] = {}
        self._changed_playlists = False
        self.job: CollectionWorker | None = None
        self._external_busy = False
        self.details: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        card = QFrame()
        card.setObjectName("card")
        body = QVBoxLayout(card)
        body.setContentsMargins(17, 15, 17, 15)
        body.setSpacing(11)
        heading = QHBoxLayout()
        heading.addWidget(label("歌单归集与分类", "section"))
        heading.addStretch(1)
        self.load_button = button("加载上次结果", self._load)
        self.load_button.setToolTip("从本机恢复待分类和待补红心列表")
        self.scan_button = button("扫描当前歌单", self._scan, primary=True)
        heading.addWidget(self.load_button)
        heading.addWidget(self.scan_button)
        body.addLayout(heading)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._category_tab(), "待分类")
        self.tabs.addTab(self._likes_tab(), "待补红心")
        body.addWidget(self.tabs, 1)
        layout.addWidget(card, 1)

        self.footer = QWidget()
        footer = QVBoxLayout(self.footer)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(6)
        actions = QHBoxLayout()
        self.status = label("登录后可扫描歌单或加载上次结果", "status")
        self.status.setWordWrap(True)
        actions.addWidget(self.status, 1)
        self.progress_value = label("", "status")
        actions.addWidget(self.progress_value)
        self.details_button = button("查看详情", self._show_details)
        self.stop_button = button("停止", self.stop)
        actions.addWidget(self.details_button)
        actions.addWidget(self.stop_button)
        footer.addLayout(actions)
        self.progress_bars = QStackedWidget()
        self.scan_progress = self._progress_bar("扫描进度")
        self.write_progress = self._progress_bar("写入进度")
        self.progress_bars.addWidget(self.scan_progress)
        self.progress_bars.addWidget(self.write_progress)
        self.progress_bars.setFixedHeight(7)
        self.progress_bars.hide()
        footer.addWidget(self.progress_bars)
        layout.addWidget(self.footer)
        self._render_categories()
        self._render_likes()
        self._update_controls()

    @staticmethod
    def _progress_bar(name: str) -> QProgressBar:
        bar = QProgressBar()
        bar.setAccessibleName(name)
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setValue(0)
        bar.setFixedHeight(7)
        return bar

    @property
    def busy(self) -> bool:
        return self.job is not None

    @property
    def missing_likes(self) -> tuple[CollectionTrack, ...] | None:
        return self.snapshot.missing_likes if self.snapshot is not None else None

    def set_session(self, cookie: dict[str, str], uid: int | None) -> None:
        self.cookie = cookie.copy()
        if self.uid != uid:
            self.uid = uid
            self.snapshot = None
            self.publish_targets.clear()
            self.uncertain_names.clear()
            self.details = []
            self._clear_progress()
            self._render_categories()
            self._render_likes()
            self.tabs.setCurrentIndex(0)
            self.status.setText(
                "扫描当前歌单，或加载本机上次结果。" if uid is not None
                else "登录后可扫描歌单或加载上次结果"
            )
        self._update_controls()

    def set_external_busy(self, busy: bool) -> None:
        self._external_busy = busy
        self.footer.setVisible(not busy)
        self._update_controls()

    def _category_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 13, 12, 9)
        self.category_summary = label("", "summary")
        self.category_summary.setWordWrap(True)
        layout.addWidget(self.category_summary)
        self.category_table = table(["歌曲 / 分类", "歌手", "专辑", "歌曲 ID", "公开歌单"])
        self.category_table.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.category_table.header().setStretchLastSection(False)
        self.category_table.setIndentation(18)
        for column, width in [(1, 155), (2, 147), (3, 135), (4, 157)]:
            self.category_table.setColumnWidth(column, width)
        layout.addWidget(self.category_table, 1)
        self.publish_buttons: list[QPushButton] = []
        return page

    def _likes_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 13, 12, 9)
        self.likes_summary = label("", "summary")
        layout.addWidget(self.likes_summary)
        self.likes_summary.setWordWrap(True)
        self.likes_summary.setToolTip("同一首歌出现在多个歌单时，勾选任意一处即可。")
        self.likes_table = table(["歌单 / 歌曲", "歌手", "专辑", "歌曲 ID"])
        self.likes_table.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.likes_table.header().setStretchLastSection(False)
        self.likes_table.setIndentation(18)
        for column, width in [(1, 151), (2, 190), (3, 135)]:
            self.likes_table.setColumnWidth(column, width)
        self.likes_selection_timer = QTimer(self)
        self.likes_selection_timer.setSingleShot(True)
        self.likes_selection_timer.timeout.connect(self._update_likes_selection)
        self.likes_table.itemChanged.connect(lambda: self.likes_selection_timer.start(0))
        layout.addWidget(self.likes_table, 1)
        actions = QHBoxLayout()
        self.select_likes = button("全选", lambda: self._check_all_likes(CHECKED))
        self.clear_likes = button("清空", lambda: self._check_all_likes(UNCHECKED))
        self.like_selection = label("", "selection")
        self.like_button = button("为所选补红心", self._like_selected, primary=True)
        actions.addWidget(self.select_likes)
        actions.addWidget(self.clear_likes)
        actions.addWidget(self.like_selection)
        actions.addStretch(1)
        actions.addWidget(self.like_button)
        layout.addLayout(actions)
        return page

    def _scan_time(self) -> str:
        return datetime.fromisoformat(self.snapshot.saved_at).astimezone().strftime("%Y-%m-%d %H:%M")

    def _render_categories(self) -> None:
        view = self.category_table
        view.setUpdatesEnabled(False)
        self.publish_buttons.clear()
        view.clear()
        if self.snapshot is None:
            self.tabs.setTabText(0, "待分类")
            self.category_summary.setText("选择「扫描当前歌单」或「加载上次结果」查看待分类歌曲。")
        else:
            count = sum(map(len, self.snapshot.groups.values()))
            self.tabs.setTabText(0, f"待分类  {count:,}")
            detail = f"{count:,} 首待分类" if count else "当前分类结果为空"
            self.category_summary.setText(f"{detail} · 扫描于 {self._scan_time()}")
            for category in LANGUAGE_ORDER:
                songs = self.snapshot.groups[category]
                if not songs:
                    continue
                group = QTreeWidgetItem(view, [f"{category}  ·  {len(songs):,} 首"])
                font = group.font(0)
                font.setBold(True)
                group.setFont(0, font)
                for song in songs:
                    row = QTreeWidgetItem(group, [
                        song.title or f"歌曲 {song.song_id}", song.artists or "—",
                        song.album or "—", str(song.song_id),
                    ])
                    for column in range(4):
                        row.setToolTip(column, row.text(column))
                pending = self.publish_targets.get(category)
                recovering = pending and any(song.song_id in pending.song_ids for song in songs)
                action = "继续添加" if recovering else "核实后重试" if category in self.uncertain_names else "新建公开歌单"
                create = button(action, lambda _checked=False, category=category: self._publish_category(category))
                create.setStyleSheet("QPushButton {min-height: 16px; padding: 2px 10px; margin: 2px 4px;}")
                view.setItemWidget(group, 4, create)
                self.publish_buttons.append(create)
                group.setExpanded(view.topLevelItemCount() == 1)
        view.setUpdatesEnabled(True)

    def _render_likes(self, checked_rows: set[TrackKey] | None = None) -> None:
        view = self.likes_table
        view.blockSignals(True)
        view.setUpdatesEnabled(False)
        expanded = {
            group.data(0, USER) for index in range(view.topLevelItemCount())
            if (group := view.topLevelItem(index)).isExpanded()
        }
        view.clear()
        songs = self.missing_likes
        self.tabs.setTabText(1, "待补红心" if songs is None else f"待补红心  {len(songs):,}")
        if songs is None:
            self.likes_summary.setText(
                "旧记录仅含分类。点击「扫描当前歌单」可保存待补红心列表。"
                if self.snapshot is not None else
                "选择「扫描当前歌单」或「加载上次结果」查看待补红心歌曲。"
            )
        elif not songs:
            self.likes_summary.setText(f"当前待补红心列表为空 · 扫描于 {self._scan_time()}")
        else:
            playlists: dict[int, tuple[Playlist, list[CollectionTrack]]] = {}
            for song in songs:
                for playlist in song.sources:
                    playlists.setdefault(playlist.id, (playlist, []))[1].append(song)
            self.likes_summary.setText(
                f"{len(songs):,} 首待补红心 · {len(playlists):,} 个歌单 · 扫描于 {self._scan_time()}"
            )
            names = Counter(playlist.name for playlist, _ in playlists.values())
            groups = sorted(playlists.values(), key=lambda pair: (pair[0].name.casefold(), pair[0].id))
            for index, (playlist, tracks) in enumerate(groups):
                name = f"{playlist.name}（{playlist.id}）" if names[playlist.name] > 1 else playlist.name
                group = QTreeWidgetItem(view, [f"{name}  ·  {len(tracks):,} 首"])
                group.setData(0, USER, playlist.id)
                group.setToolTip(0, f"{playlist.name} · 歌单 ID {playlist.id}")
                group.setFlags(group.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
                group.setCheckState(0, UNCHECKED)
                font = group.font(0)
                font.setBold(True)
                group.setFont(0, font)
                for song in tracks:
                    row = QTreeWidgetItem(group, [
                        song.title or f"歌曲 {song.song_id}", song.artists or "—", song.album or "—",
                        str(song.song_id),
                    ])
                    row.setData(0, USER, song.song_id)
                    selected = checked_rows is None or (playlist.id, song.song_id) in checked_rows
                    row.setCheckState(0, CHECKED if selected else UNCHECKED)
                    for column in range(4):
                        row.setToolTip(column, row.text(column))
                group.setExpanded(playlist.id in expanded or (not expanded and index == 0))
        view.setUpdatesEnabled(True)
        view.blockSignals(False)
        self._update_likes_selection()

    def _checked_likes_rows(self) -> set[TrackKey]:
        checked = set()
        for index in range(self.likes_table.topLevelItemCount()):
            group = self.likes_table.topLevelItem(index)
            playlist_id = group.data(0, USER)
            checked.update(
                (playlist_id, row.data(0, USER)) for child_index in range(group.childCount())
                if (row := group.child(child_index)).checkState(0) == CHECKED
            )
        return checked

    def _selected_likes(self) -> set[int]:
        return {song_id for _, song_id in self._checked_likes_rows()}

    def _update_likes_selection(self) -> None:
        checked = self._checked_likes_rows()
        count = len({song_id for _, song_id in checked})
        songs = self.missing_likes or ()
        total = len(songs)
        idle = not (self.busy or self._external_busy) and self.uid is not None
        self.like_selection.setText(f"已选 {count:,} / {total:,} 首")
        self.select_likes.setEnabled(idle and len(checked) < sum(len(song.sources) for song in songs))
        self.clear_likes.setEnabled(idle and count > 0)
        self.like_button.setEnabled(idle and count > 0)

    def _check_all_likes(self, state: Qt.CheckState) -> None:
        self.likes_table.blockSignals(True)
        for index in range(self.likes_table.topLevelItemCount()):
            self.likes_table.topLevelItem(index).setCheckState(0, state)
        self.likes_table.blockSignals(False)
        self._update_likes_selection()

    def _update_controls(self) -> None:
        idle = not (self.busy or self._external_busy)
        editable = idle and self.uid is not None
        for control in (
            self.load_button, self.scan_button,
            self.category_table, self.likes_table, *self.publish_buttons,
        ):
            control.setEnabled(editable)
        self.details_button.setVisible(bool(self.details))
        self.details_button.setEnabled(idle)
        self.stop_button.setVisible(self.busy)
        if self.job is not None:
            self.stop_button.setText("停止剩余操作" if self.job.kind in ("publish", "likes") else "停止扫描")
            self.stop_button.setEnabled(not self.job.isInterruptionRequested())
        self._update_likes_selection()

    def _launch(self, kind: str, action: Callable, message: str, *, write: bool = False) -> None:
        if self.busy or self._external_busy or self.uid is None:
            return

        def run(worker: CollectionWorker):
            api: NcmApi = self.api_factory(self.port, (lambda: None) if write else worker.check_cancel)
            api.cookie = self.cookie.copy()
            return action(api, worker.progress.emit, worker.isInterruptionRequested, worker.write_progress.emit)

        worker = CollectionWorker(kind, run, self, deliver_on_cancel=write)
        worker.progress.connect(self._scan_progress)
        worker.write_progress.connect(self._write_progress)
        worker.result.connect(self._receive)
        worker.failure.connect(self._failure)
        worker.finished.connect(self._finished)
        self.job = worker
        self._changed_playlists = False
        self.details = []
        self._clear_progress()
        self._scan_progress(0, message)
        self._update_controls()
        self.busy_changed.emit(True)
        worker.start()

    def _clear_progress(self) -> None:
        self.scan_progress.setValue(0)
        self.write_progress.setValue(0)
        self.progress_bars.setCurrentWidget(self.scan_progress)
        self.progress_bars.hide()
        self.progress_value.clear()

    def _scan_progress(self, value: int, message: str) -> None:
        self.scan_progress.setValue(value)
        self.progress_bars.show()
        self.progress_value.setText(f"扫描 {value}%")
        self.status.setText(message)

    def _write_progress(self, value: int, message: str) -> None:
        self.progress_bars.setCurrentWidget(self.write_progress)
        self.write_progress.setValue(value)
        action = "补红心" if self.job is not None and self.job.kind == "likes" else "添加到歌单"
        self.progress_value.setText(f"{action} {value}%")
        self.status.setText(message)

    def stop(self) -> None:
        if self.job is not None:
            self.job.requestInterruption()
            self.status.setText("正在停止，等待当前请求返回。")
            self.stop_button.setEnabled(False)

    def _finished(self) -> None:
        if self.job is not None:
            self.job.deleteLater()
            self.job = None
        self._update_controls()
        self.busy_changed.emit(False)
        if self._changed_playlists:
            self.playlists_changed.emit()

    def _failure(self, message: str) -> None:
        self.status.setText(message)
        if message != "操作已取消":
            self.details.append(message)

    def _load(self) -> None:
        if self.busy or self._external_busy or self.uid is None:
            return
        self.details = []
        self._clear_progress()
        try:
            snapshot = load_snapshot(self.uid)
        except ClassificationStoreError as exc:
            self._failure(str(exc))
        else:
            if snapshot is None:
                self.status.setText("本机还没有这个账号的归集结果。")
            else:
                self.snapshot = snapshot
                self._render_categories()
                self._render_likes()
                self.status.setText(
                    "已加载上次待分类和待补红心结果。" if snapshot.missing_likes is not None
                    else "已加载上次分类。重新扫描后可一并保存待补红心列表。"
                )
        self._update_controls()

    def _scan(self) -> None:
        self._launch(
            "scan", lambda api, progress, stop, _write_progress: scan_collection(api, self.uid, progress, stop),
            "正在读取当前歌单。",
        )

    def _save(self) -> bool:
        if self.snapshot is None:
            return False
        try:
            save_snapshot(self.snapshot)
        except ClassificationStoreError as exc:
            self.details.append(str(exc))
            return False
        return True

    def _show_scan(self, scan: CollectionScan) -> None:
        self.snapshot = replace(scan.snapshot, missing_likes=scan.missing_likes)
        self.details.extend(scan.warnings)
        saved = self._save()
        self._render_categories()
        self._render_likes()
        self._scan_progress(
            100,
            f"已检查 {scan.child_count:,} 个歌单 · 红心 {scan.liked_count:,} 首 · "
            + ("归集结果已保存" if saved else "归集结果未能保存，请查看详情"),
        )

    def _confirm(self, title: str, message: str) -> bool:
        dialog = QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(message)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return dialog.exec() == QMessageBox.StandardButton.Ok

    def _like_selected(self) -> None:
        if self.busy or self._external_busy or self.uid is None or not self.missing_likes:
            return
        checked = self._selected_likes()
        if not checked or not self._confirm("确认补红心", f"将所选的 {len(checked):,} 首歌曲补入「我喜欢的音乐」。"):
            return
        self._launch(
            "likes",
            lambda api, progress, stop, write_progress: add_missing_likes(
                api, self.uid, sorted(checked), progress, stop, write_progress=write_progress,
            ),
            "正在核对待补红心的歌曲。",
            write=True,
        )

    def _publish_category(self, category: str) -> None:
        if self.busy or self._external_busy or self.uid is None or self.snapshot is None:
            return
        songs = self.snapshot.groups[category]
        if not songs:
            return
        song_ids = {song.song_id for song in songs}
        target = self.publish_targets.get(category)
        if target and target.song_ids.intersection(song_ids):
            name = target.name
            playlist_id = target.playlist_id
            song_ids.intersection_update(target.song_ids)
            confirmation = f"将「{category}」剩余的 {len(song_ids):,} 首歌曲添加到公开歌单「{name}」。"
        else:
            previous = self.uncertain_names.get(category)
            if previous and not self._confirm(
                "核实创建结果",
                f"上次创建「{previous}」的结果仍待核实。请在网易云检查该歌单，确认未创建后再继续。",
            ):
                return
            name, accepted = QInputDialog.getText(self, "新建公开歌单", "歌单名称", text=category)
            name = name.strip()
            if not accepted or not name:
                return
            playlist_id = None
            confirmation = f"将在你的账号下新建公开歌单「{name}」，并添加「{category}」的 {len(song_ids):,} 首歌曲。"
        if not self._confirm("确认添加到公开歌单", confirmation):
            return
        self.uncertain_names.pop(category, None)
        self._launch(
            "publish",
            lambda api, progress, stop, write_progress: (
                category, name, frozenset(song_ids),
                create_category_playlist(
                    api, self.uid, name, sorted(song_ids), progress, stop,
                    playlist_id=playlist_id, write_progress=write_progress,
                ),
            ),
            "正在核对歌曲并准备公开歌单。",
            write=True,
        )

    def _receive(self, kind: str, result: object) -> None:
        if kind == "scan":
            self._show_scan(result)
        elif kind == "likes":
            checked = self._checked_likes_rows()
            done = result.verified | result.already_present | result.skipped.keys()
            saved = True
            if done:
                remaining = tuple(song for song in self.missing_likes if song.song_id not in done)
                self.snapshot = replace(self.snapshot, missing_likes=remaining)
                saved = self._save()
                self._render_likes(checked)
            self._show_report(result, "已补红心", saved=saved)
        elif kind == "publish":
            category, name, selected, report = result
            done = report.verified | report.already_present | report.skipped.keys()
            if report.playlist_id is not None and (unfinished := selected - done):
                self.publish_targets[category] = PublishTarget(report.playlist_id, name, unfinished)
            else:
                self.publish_targets.pop(category, None)
            if report.creation_uncertain:
                self.uncertain_names[category] = name
            saved = True
            if done:
                groups = self.snapshot.groups.copy()
                groups[category] = tuple(song for song in groups[category] if song.song_id not in done)
                self.snapshot = replace(self.snapshot, groups=groups)
                saved = self._save()
            self._render_categories()
            self._show_report(report, "已添加", saved=saved)

    def _show_report(self, report: SyncReport, action: str, *, saved: bool) -> None:
        self._changed_playlists |= bool(
            report.verified or report.already_present or report.playlist_id is not None or report.creation_uncertain
        )
        details = [(report.failures, "失败"), (report.skipped, "跳过")]
        self.details.extend(f"{reason} · {sid} · {message}" for entries, reason in details for sid, message in entries.items())
        if report.message:
            self.details.insert(0, report.message)
        pieces = [f"{action} {len(report.verified):,} 首"]
        for caption, rows in (
            ("原已在列表", report.already_present), ("跳过", report.skipped),
            ("失败", report.failures), ("待确认或未处理", report.pending),
        ):
            if rows:
                pieces.append(f"{caption} {len(rows):,} 首")
        if report.stopped:
            pieces.insert(0, "已停止")
        if report.message:
            pieces.append(report.message.splitlines()[0])
        if not saved:
            pieces.append("归集结果未能保存，请查看详情")
        self.status.setText(" · ".join(pieces))

    def _show_details(self) -> None:
        panel = QDialog(self)
        panel.setWindowTitle("归集详情")
        panel.resize(650, 400)
        layout = QVBoxLayout(panel)
        details = QPlainTextEdit("\n".join(self.details))
        details.setReadOnly(True)
        layout.addWidget(details, 1)
        layout.addWidget(button("关闭", panel.reject), alignment=Qt.AlignmentFlag.AlignRight)
        panel.exec()
