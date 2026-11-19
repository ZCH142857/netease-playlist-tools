from __future__ import annotations

import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from math import ceil

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFrame, QHeaderView, QHBoxLayout, QInputDialog,
    QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .collection_sync import SyncReport, add_missing_likes
from .gui import CHECKED, UNCHECKED, USER, Worker, button, label, table
from .management import ManagementScan, add_to_playlist, scan_management
from .removal import RemovalReport, by_playlist, remove_tracks
from .scanner import Occurrence, Playlist, TrackKey


def _search_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


class ManagementPage(QWidget):
    busy_changed = Signal(bool)
    playlists_changed = Signal()

    def __init__(self, parent: QWidget, api_factory: Callable, port: int):
        super().__init__(parent)
        self.api_factory = api_factory
        self.port = port
        self.uid: int | None = None
        self.cookie: dict[str, str] = {}
        self.job: Worker | None = None
        self._external_busy = False
        self._scan_attempted = False
        self._has_results = False
        self._remove_retry_at = 0.0
        self.playlists: list[Playlist] = []
        self.tracks: dict[int, list[Occurrence]] = {}
        self.warnings: list[str] = []
        self.details: list[str] = []
        self._checked: set[TrackKey] = set()
        self._visible_rows: dict[TrackKey, Occurrence] = {}
        self._visible_items: dict[TrackKey, QTreeWidgetItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        card = QFrame()
        card.setObjectName("card")
        body = QVBoxLayout(card)
        body.setContentsMargins(17, 15, 17, 15)
        body.setSpacing(11)
        heading = QHBoxLayout()
        heading.addWidget(label("歌单管理", "section"))
        heading.addStretch(1)
        self.refresh_button = button("刷新歌单", self._refresh)
        heading.addWidget(self.refresh_button)
        body.addLayout(heading)

        filters = QHBoxLayout()
        self.playlist_filter = QComboBox()
        self.playlist_filter.setAccessibleName("搜索范围")
        self.playlist_filter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.playlist_filter.setMinimumContentsLength(13)
        self.playlist_filter.setMaximumWidth(270)
        self.search_input = QLineEdit()
        self.search_input.setAccessibleName("搜索歌曲")
        self.search_input.setPlaceholderText("搜索歌名、歌手、专辑或歌曲 ID")
        self.search_input.setClearButtonEnabled(True)
        filters.addWidget(self.playlist_filter)
        filters.addWidget(self.search_input, 1)
        body.addLayout(filters)
        self.summary = label("", "summary")
        self.summary.setWordWrap(True)
        body.addWidget(self.summary)

        self.track_table = table(["歌单 / 歌曲", "歌手", "专辑", "歌曲 ID"])
        self.track_table.setIndentation(18)
        header = self.track_table.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in [(1, 150), (2, 160), (3, 128)]:
            self.track_table.setColumnWidth(column, width)
        body.addWidget(self.track_table, 1)

        actions = QHBoxLayout()
        self.select_button = button("全选结果", lambda: self._check_all(CHECKED))
        self.clear_button = button("清除勾选", self._clear_checks)
        self.clear_button.setToolTip("清除所有勾选，包括搜索筛选隐藏的条目")
        self.selection = label("", "selection")
        self.selection.setWordWrap(True)
        self.like_button = button("批量红心", self._like_selected, primary=True)
        self.copy_button = button("添加到歌单", self._copy_selected)
        self.remove_button = button("从歌单移除", self._remove_selected)
        self.remove_button.setObjectName("remove")
        actions.addWidget(self.select_button)
        actions.addWidget(self.clear_button)
        actions.addWidget(self.selection, 1)
        for action in (self.like_button, self.copy_button, self.remove_button):
            actions.addWidget(action)
        body.addLayout(actions)
        layout.addWidget(card, 1)

        self.footer = QWidget()
        footer = QVBoxLayout(self.footer)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(6)
        status_row = QHBoxLayout()
        self.status = label("登录后读取自建歌单。", "status")
        self.status.setWordWrap(True)
        self.progress_value = label("", "status")
        self.details_button = button("查看详情", self._show_details)
        self.stop_button = button("停止", self.stop)
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.progress_value)
        status_row.addWidget(self.details_button)
        status_row.addWidget(self.stop_button)
        footer.addLayout(status_row)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("歌单管理进度")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(7)
        self.progress.hide()
        footer.addWidget(self.progress)
        layout.addWidget(self.footer)

        self._selection_timer = QTimer(self)
        self._selection_timer.setSingleShot(True)
        self._selection_timer.timeout.connect(self._update_selection)
        self.track_table.itemChanged.connect(lambda: self._selection_timer.start(0))
        self._cooldown_timer = QTimer(self)
        self._cooldown_timer.setInterval(1000)
        self._cooldown_timer.timeout.connect(self._update_selection)
        self.playlist_filter.currentIndexChanged.connect(self._filter_changed)
        self.search_input.textChanged.connect(self._filter_changed)
        self._set_playlist_options()
        self._render()
        self._update_controls()

    @property
    def busy(self) -> bool:
        return self.job is not None

    def set_session(self, cookie: dict[str, str], uid: int | None) -> None:
        self.cookie = cookie.copy()
        if uid != self.uid:
            self.uid = uid
            self.invalidate()
            self._has_results = False
            self._remove_retry_at = 0.0
            self.playlists = []
            self.tracks = {}
            self.warnings = []
            self.details = []
            self._checked.clear()
            self._set_playlist_options()
            self.search_input.blockSignals(True)
            self.search_input.clear()
            self.search_input.blockSignals(False)
            self.progress.hide()
            self.progress_value.clear()
            self.status.setText("准备读取自建歌单。" if uid is not None else "登录后读取自建歌单。")
            self._render()
        self._update_controls()

    def set_external_busy(self, busy: bool) -> None:
        self._external_busy = busy
        self.footer.setVisible(not busy)
        self._update_controls()

    def invalidate(self) -> None:
        self._scan_attempted = False

    def ensure_loaded(self) -> None:
        if not self._scan_attempted:
            self._refresh()

    @staticmethod
    def _playlist_label(playlist: Playlist) -> str:
        return f"{playlist.name}（ID {playlist.id}）"

    def _set_playlist_options(self) -> None:
        combo = self.playlist_filter
        selected = combo.currentData()
        names = Counter(playlist.name for playlist in self.playlists)
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("全部自建歌单", None)
        for playlist in self.playlists:
            caption = self._playlist_label(playlist) if names[playlist.name] > 1 else playlist.name
            combo.addItem(caption, playlist.id)
        combo.setCurrentIndex(max(0, combo.findData(selected)))
        combo.blockSignals(False)

    def _filter_changed(self, *_args) -> None:
        self._remember_checks()
        self._render()

    @staticmethod
    def _matches(row: Occurrence, terms: list[str]) -> bool:
        text = _search_text(f"{row.title}\n{row.artists}\n{row.album}\n{row.song_id}")
        return all(term in text for term in terms)

    def _render(self) -> None:
        view = self.track_table
        expanded = {
            group.data(0, USER) for i in range(view.topLevelItemCount())
            if (group := view.topLevelItem(i)).isExpanded()
        }
        view.blockSignals(True)
        view.setUpdatesEnabled(False)
        self._visible_rows.clear()
        self._visible_items.clear()
        view.clear()
        search_terms = _search_text(self.search_input.text()).split()
        selected_pid = self.playlist_filter.currentData()
        names = Counter(playlist.name for playlist in self.playlists)
        for playlist in self.playlists:
            if selected_pid is not None and playlist.id != selected_pid:
                continue
            rows = self.tracks.get(playlist.id)
            matches = [row for row in (rows or ()) if self._matches(row, search_terms)] if search_terms else rows or []
            if search_terms and not matches:
                continue
            name = self._playlist_label(playlist) if names[playlist.name] > 1 else playlist.name
            count = f"{len(matches):,} 条" if rows is not None else "未读取"
            group = QTreeWidgetItem(view, [f"{name}  ·  {count}"])
            group.setData(0, USER, playlist.id)
            group.setToolTip(0, self._playlist_label(playlist))
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            if matches:
                group.setFlags(group.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
                group.setCheckState(0, UNCHECKED)
            for row in matches:
                item = QTreeWidgetItem(group, [row.title, row.artists or "—", row.album or "—", str(row.song_id)])
                item.setData(0, USER, row.song_id)
                item.setCheckState(0, CHECKED if row.key in self._checked else UNCHECKED)
                for column in range(4):
                    item.setToolTip(column, item.text(column))
                self._visible_rows[row.key] = row
                self._visible_items[row.key] = item
            group.setExpanded(bool(search_terms) or selected_pid is not None or playlist.id in expanded
                              or (not expanded and view.topLevelItemCount() == 1))
        view.setUpdatesEnabled(True)
        view.blockSignals(False)

        if not self._has_results:
            headline = "登录后显示自建歌单" if self.uid is None else "等待读取自建歌单"
        elif not self.playlists:
            headline = "当前账号没有可管理的自建歌单"
        elif search_terms and not self._visible_rows:
            headline = "当前范围内没有匹配的歌曲"
        else:
            headline = f"{view.topLevelItemCount():,} 个歌单 · {len(self._visible_rows):,} 条歌曲"
        self.summary.setText(headline)
        self._update_selection()

    def _remember_checks(self) -> set[TrackKey]:
        checked = {key for key, item in self._visible_items.items() if item.checkState(0) == CHECKED}
        self._checked.difference_update(self._visible_items)
        self._checked.update(checked)
        return checked

    def _update_selection(self) -> None:
        current = self._remember_checks()
        count = len(current)
        hidden = len(self._checked) - count
        text = f"本次勾选 {count:,} / {len(self._visible_items):,} 条"
        if hidden:
            text += f" · 筛选外 {hidden:,} 条"
        self.selection.setText(text)
        idle = self.uid is not None and not (self.busy or self._external_busy)
        self.select_button.setEnabled(idle and count < len(self._visible_items))
        self.clear_button.setEnabled(idle and bool(self._checked))
        self.like_button.setEnabled(idle and count > 0)
        self.copy_button.setEnabled(idle and count > 0)
        seconds = max(0, ceil(self._remove_retry_at - time.monotonic()))
        if seconds == 0:
            self._cooldown_timer.stop()
        self.remove_button.setText(f"稍后移除（{seconds} 秒）" if seconds else "从歌单移除")
        self.remove_button.setEnabled(idle and count > 0 and seconds == 0)

    def _check_all(self, state: Qt.CheckState) -> None:
        view = self.track_table
        view.blockSignals(True)
        for index in range(view.topLevelItemCount()):
            item = view.topLevelItem(index)
            if item.childCount():
                item.setCheckState(0, state)
        view.blockSignals(False)
        self._update_selection()

    def _clear_checks(self) -> None:
        self._checked.clear()
        self._check_all(UNCHECKED)

    def _selected(self) -> list[Occurrence]:
        checked = self._remember_checks()
        return [row for key, row in self._visible_rows.items() if key in checked]

    def _update_controls(self) -> None:
        idle = not (self.busy or self._external_busy)
        readable = idle and self.uid is not None
        self.refresh_button.setEnabled(readable)
        self.playlist_filter.setEnabled(readable and bool(self.playlists))
        self.search_input.setEnabled(readable and bool(self.playlists))
        self.track_table.setEnabled(readable)
        self.details_button.setVisible(bool(self.warnings or self.details))
        self.details_button.setEnabled(idle)
        self.stop_button.setVisible(self.busy)
        self.stop_button.setEnabled(self.busy and not self.job.isInterruptionRequested())
        self._update_selection()

    def _refresh(self) -> None:
        if self._launch("scan", scan_management, "正在读取自建歌单。"):
            self._scan_attempted = True

    def _launch(self, kind: str, action: Callable, message: str) -> bool:
        uid = self.uid
        if uid is None or self.busy or self._external_busy:
            return False
        cookie = self.cookie.copy()
        write = kind != "scan"

        def run(worker: Worker):
            # A stopped write still needs a read-back to reconcile in-flight changes.
            api = self.api_factory(self.port, (lambda: None) if write else worker.check_cancel)
            api.cookie = cookie
            return action(api, uid, worker.progress.emit, worker.isInterruptionRequested)

        worker = Worker(kind, run, self, deliver_on_cancel=write)
        worker.progress.connect(self._report_progress)
        worker.result.connect(self._receive)
        worker.failure.connect(self._failure)
        worker.finished.connect(self._finished)
        self.job = worker
        self.details = []
        self._report_progress(0, message)
        self._update_controls()
        self.busy_changed.emit(True)
        worker.start()
        return True

    def _report_progress(self, value: int, message: str) -> None:
        self.progress.show()
        self.progress.setValue(value)
        self.progress_value.setText(f"{value}%")
        self.status.setText(message)

    def stop(self) -> None:
        if self.job is not None:
            self.job.requestInterruption()
            self.status.setText("正在停止，等待当前请求返回。")
            self.stop_button.setEnabled(False)

    def _finished(self) -> None:
        changed = self.job.kind != "scan"
        self.job.deleteLater()
        self.job = None
        self._update_controls()
        if changed:
            self.playlists_changed.emit()
        self.busy_changed.emit(False)

    def _failure(self, message: str) -> None:
        self.status.setText(message)
        if message != "操作已取消":
            self.details.append(message)

    def _confirm(self, title: str, message: str, *, details: str = "") -> bool:
        dialog = QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(message)
        if details:
            dialog.setDetailedText(details)
        buttons = QMessageBox.StandardButton
        dialog.setStandardButtons(buttons.Ok | buttons.Cancel)
        dialog.setDefaultButton(buttons.Cancel)
        return dialog.exec() == buttons.Ok

    def _like_selected(self) -> None:
        if not self.like_button.isEnabled():
            return
        songs = list(dict.fromkeys(row.song_id for row in self._selected()))
        if songs and self._confirm("确认批量红心", f"将所选的 {len(songs):,} 首歌曲加入「我喜欢的音乐」。"):
            self._launch(
                "likes", lambda api, uid, progress, stop: add_missing_likes(api, uid, songs, progress, stop),
                "正在核对待加红心的歌曲。",
            )

    def _copy_selected(self) -> None:
        if not self.copy_button.isEnabled():
            return
        selected = {row.song_id: row for row in self._selected()}
        if not selected:
            return
        destinations = {
            self._playlist_label(playlist): playlist for playlist in self.playlists
            if playlist.category == "自建" and playlist.editable and not playlist.video
        }
        if not destinations:
            return
        choice, accepted = QInputDialog.getItem(self, "添加到歌单", "目标歌单", list(destinations), 0, False)
        if not accepted:
            return
        target = destinations[choice]
        if not self._confirm(
            "确认添加到歌单",
            f"将所选的 {len(selected):,} 首歌曲复制到「{self._playlist_label(target)}」。",
        ):
            return
        self._launch(
            "copy", lambda api, uid, progress, stop: (
                target, selected, add_to_playlist(api, uid, target, selected, progress, stop),
            ),
            "正在核对目标歌单。",
        )

    def _remove_selected(self) -> None:
        if not self.remove_button.isEnabled():
            return
        rows = self._selected()
        if not rows:
            return
        playlists = by_playlist(rows)
        sources = []
        details = []
        for songs in playlists.values():
            first = next(iter(songs.values()))
            source = f"{self._playlist_label(first.playlist)} · {len(songs):,} 条"
            sources.append(source)
            details.append(source + "\n" + "\n".join(f"  {row.title}（ID {row.song_id}）" for row in songs.values()))
        header = f"将从下列 {len(playlists):,} 个歌单移除 {len(rows):,} 条歌曲。"
        preview = "\n".join(sources[:5])
        if len(sources) > 5:
            preview += "\n其余歌单及歌曲见详情。"
        if self._confirm("确认从歌单移除", f"{header}\n\n{preview}", details="\n\n".join(details)):
            self._launch(
                "remove", lambda api, uid, progress, stop: remove_tracks(api, uid, rows, progress, stop),
                "正在核对账号和来源歌单。",
            )

    def _receive(self, kind: str, result: object) -> None:
        if kind == "scan":
            self._show_scan(result)
        elif kind == "remove":
            self._show_removal(result)
        elif kind == "copy":
            target, selected, report = result
            present = report.verified | report.already_present
            target_rows = self.tracks.get(target.id)
            if present and target_rows is not None:
                known = {row.song_id for row in target_rows}
                additions = [replace(row, playlist=target) for sid, row in selected.items() if sid in present - known]
                self.tracks[target.id] = [*target_rows, *additions]
                self._render()
            elif present:
                self.details.append("刷新歌单可查看目标歌单的最新歌曲。")
            self._show_sync_report(report, "已添加")
        elif kind == "likes":
            self._show_sync_report(result, "已加红心")

    def _show_scan(self, result: ManagementScan) -> None:
        self._remember_checks()
        self.playlists = result.playlists
        self.tracks = result.tracks
        self.warnings = result.warnings
        self._has_results = True
        self._checked.intersection_update(row.key for rows in self.tracks.values() for row in rows)
        self._set_playlist_options()
        self._render()
        note = f"已读取 {len(self.tracks):,} 个自建歌单"
        if self.warnings:
            note += " · 部分数据缺失，请查看详情"
        self._report_progress(100, note)

    def _show_sync_report(self, report: SyncReport, action: str) -> None:
        pieces = [f"{action} {len(report.verified):,} 首"]
        for caption, rows in (
            ("原已在列表", report.already_present), ("跳过", report.skipped), ("未确认", report.failures),
            ("待确认或未处理", report.pending),
        ):
            if rows:
                pieces.append(f"{caption} {len(rows):,} 首")
        for caption, entries in (("跳过", report.skipped), ("未确认", report.failures)):
            self.details.extend(f"{caption} · 歌曲 {sid} · {reason}" for sid, reason in entries.items())
        if report.message:
            pieces.append(report.message.splitlines()[0])
            self.details.append(report.message)
        if report.stopped:
            pieces.insert(0, "已停止")
        self.status.setText(" · ".join(pieces))

    def _show_removal(self, report: RemovalReport) -> None:
        self._remember_checks()
        done = report.removed | report.already_absent
        self._checked.difference_update(done)
        for pid in {pid for pid, _ in done}:
            self.tracks[pid] = [row for row in self.tracks[pid] if row.key not in done]
        self.details.extend(f"歌单 {pid} · 歌曲 {sid} · {reason}" for (pid, sid), reason in report.failures.items())
        if report.retry_at > time.monotonic():
            self._remove_retry_at = report.retry_at
            self._cooldown_timer.start()
        self._render()
        pieces = [f"已移除 {len(report.removed):,} 条"]
        for caption, count in (
            ("原已不在歌单", len(report.already_absent)), ("未确认", len(report.failures)), ("未处理", report.pending),
        ):
            if count:
                pieces.append(f"{caption} {count:,} 条")
        if report.stopped:
            pieces.insert(0, "已停止")
        if not report.pending and not report.failures:
            self.progress.setValue(100)
            self.progress_value.setText("100%")
        self.status.setText(" · ".join(pieces))

    def _show_details(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("歌单管理详情")
        dialog.resize(650, 390)
        layout = QVBoxLayout(dialog)
        details = QPlainTextEdit("\n".join([*self.warnings, *self.details]))
        details.setReadOnly(True)
        layout.addWidget(details, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
