from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from math import ceil

from PySide6.QtCore import QEvent, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QCloseEvent, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionButton, QStyleOptionViewItem, QStylePainter, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .api import ApiError, ApiSessionExpired, Cancelled, NcmApi
from .auth import parse_cookie, scan_cookie
from .catalog import CatalogComparison, compare_catalog
from .normalize import title_prefix
from .removal import RemovalReport, by_playlist, remove_tracks
from .scanner import Occurrence, Playlist, ScanResult, TrackKey, collect_duplicates, playlists_for
from .session import SessionStore, SessionStoreError

CHECKED = Qt.CheckState.Checked
UNCHECKED = Qt.CheckState.Unchecked
USER = Qt.ItemDataRole.UserRole

STYLE = """
QWidget { font-size: 14px; color: #20243b; }
QMainWindow, QDialog { background: #f4f6fc; }
QDialog QWidget { color: #20243b; }
#title { font-size: 28px; font-weight: 700; }
#session, #status, #selection { color: #555e7b; }
#section { font-size: 19px; font-weight: 650; }
#summary { color: #49599c; font-weight: 600; }
#card { background: #ffffff; border: 1px solid #dbe1f0; border-radius: 13px; }
#overviewMetric { background: #f3f5fe; border: 1px solid #dce2f4; border-radius: 9px; }
#metricValue { font-size: 29px; font-weight: 700; color: #39468e; }
QPushButton {
    background: #ffffff; border: 1px solid #cbd4e8; border-radius: 7px;
    padding: 7px 13px; min-height: 19px;
}
QPushButton:hover { background: #edf1fc; border-color: #93a4df; }
QPushButton:focus { border-color: #566dc0; }
QPushButton:disabled { color: #788098; background: #f2f4f8; border-color: #e3e7f1; }
QPushButton#primary { background: #4c59b0; border-color: #4c59b0; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #3c4a9c; }
QPushButton#primary:disabled { background: #eceefa; border-color: #dce1f2; color: #777e9c; }
QPushButton#featureEntry {
    background: #ecefff; border: 1px solid #c8d0ef; border-radius: 9px;
    min-height: 35px; font-size: 16px; font-weight: 600; color: #323c82;
}
QPushButton#featureEntry:hover { background: #e0e5ff; border-color: #8594d3; }
QPushButton#featureEntry:focus { border-color: #566dc0; }
QPushButton#featureEntry[current="true"] { background: #4c59b0; border-color: #4c59b0; color: #ffffff; }
QPushButton#featureEntry:disabled { background: #f0f2f8; color: #68728e; border-color: #dbe1f0; }
QPushButton#remove { color: #a42b44; border-color: #ddb1ba; font-weight: 600; }
QPushButton#remove:hover { background: #fff0f3; border-color: #ba5368; }
QPushButton#remove:disabled { color: #788098; border-color: #e3e7f1; }
QTreeWidget {
    background: #ffffff; alternate-background-color: #f7f8fc;
    border: 1px solid #dce2f0; border-radius: 8px; outline: 0;
    selection-background-color: #e7ebff; selection-color: #20243b;
}
QTreeWidget::item { height: 30px; border: none; padding-right: 8px; }
QTreeWidget::item:selected { background: #e7ebff; color: #20243b; }
QTreeWidget::indicator { width: 16px; height: 16px; background: transparent; border: none; }
QCheckBox { spacing: 8px; color: #283452; }
QCheckBox:disabled { color: #69738e; }
QCheckBox::indicator { width: 18px; height: 18px; background: transparent; border: none; }
QTabWidget::pane { background: #ffffff; border: 1px solid #dbe1f0; border-radius: 6px; }
QTabBar::tab {
    background: #e9edf9; color: #303d65; border: 1px solid #d3daf0;
    border-radius: 6px; margin-right: 4px; padding: 8px 16px; font-weight: 600;
}
QTabBar::tab:hover { background: #dfe6fc; }
QTabBar::tab:selected { background: #4c59b0; border-color: #4c59b0; color: #ffffff; }
QMenu { background: #ffffff; color: #20243b; border: 1px solid #d3daf0; }
QMenu::item { padding: 5px 13px; }
QMenu::item:selected { background: #e7ebff; color: #20243b; }
QMenu::item:disabled { color: #69738e; }
QHeaderView::section {
    background: #edf0f9; border: none; border-bottom: 1px solid #dce2f0;
    padding: 7px 9px; color: #394564; font-weight: 600;
}
QProgressBar { height: 7px; background: #e3e7f4; border: none; border-radius: 3px; }
QProgressBar::chunk { background: #606bd0; border-radius: 3px; }
QLineEdit, QPlainTextEdit {
    background: white; color: #20243b; border: 1px solid #cbd4e8; border-radius: 5px; padding: 5px;
}
"""


def configure_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = app.style().standardPalette()
    roles = QPalette.ColorRole
    for role, color in {
        roles.Window: "#f4f6fc",
        roles.WindowText: "#20243b",
        roles.Base: "#ffffff",
        roles.AlternateBase: "#f7f8fc",
        roles.Text: "#20243b",
        roles.Button: "#ffffff",
        roles.ButtonText: "#20243b",
        roles.Highlight: "#4c59b0",
        roles.HighlightedText: "#ffffff",
        roles.ToolTipBase: "#ffffff",
        roles.ToolTipText: "#20243b",
    }.items():
        palette.setColor(role, QColor(color))
    for role in (roles.WindowText, roles.Text, roles.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#69738e"))
    app.setPalette(palette)


def label(text: str, name: str = "") -> QLabel:
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def button(text: str, action: Callable, *, primary: bool = False) -> QPushButton:
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(action)
    return widget


def paint_checkbox_indicator(painter: QPainter, rect, state: Qt.CheckState, enabled: bool) -> None:
    rect = QRectF(rect).adjusted(1, 1, -1, -1)
    if not rect.isValid():
        return
    edge = "#626c88" if enabled else "#a7afc2"
    active = "#4c59b0" if enabled else "#8a93bd"
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(edge if state == UNCHECKED else active), 1.25))
    painter.setBrush(QColor("#ffffff" if state == UNCHECKED else active))
    painter.drawRoundedRect(rect, 3, 3)
    if state != UNCHECKED:
        mark = QPainterPath()
        x, y, width, height = rect.x(), rect.y(), rect.width(), rect.height()
        if state == CHECKED:
            mark.moveTo(x + width * 0.24, y + height * 0.52)
            mark.lineTo(x + width * 0.43, y + height * 0.70)
            mark.lineTo(x + width * 0.77, y + height * 0.31)
        else:
            mark.moveTo(x + width * 0.27, rect.center().y())
            mark.lineTo(x + width * 0.73, rect.center().y())
        painter.setPen(QPen(QColor("#ffffff"), 1.8, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawPath(mark)
    painter.restore()


class VisibleCheckBox(QCheckBox):
    def paintEvent(self, event) -> None:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_CheckBox, option)
        rect = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
        paint_checkbox_indicator(painter, rect, self.checkState(), self.isEnabled())


class CheckableItemDelegate(QStyledItemDelegate):
    def indicator_rect(self, option, index):
        item = QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        style = item.widget.style() if item.widget else QApplication.style()
        return style.subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, item, item.widget)

    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        value = index.data(Qt.ItemDataRole.CheckStateRole)
        if value is None:
            return
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        paint_checkbox_indicator(painter, self.indicator_rect(option, index), Qt.CheckState(value), enabled)

    def editorEvent(self, event, model, option, index) -> bool:
        flags = index.flags()
        value = index.data(Qt.ItemDataRole.CheckStateRole)
        if (event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and option.rect.contains(event.position().toPoint())
                and flags & Qt.ItemFlag.ItemIsUserCheckable
                and flags & Qt.ItemFlag.ItemIsEnabled
                and value is not None):
            state = UNCHECKED if Qt.CheckState(value) == CHECKED else CHECKED
            return model.setData(index, state.value, Qt.ItemDataRole.CheckStateRole)
        return super().editorEvent(event, model, option, index)


def table(headers: list[str]) -> QTreeWidget:
    widget = QTreeWidget()
    widget.setHeaderLabels(headers)
    widget.setAlternatingRowColors(True)
    widget.setUniformRowHeights(True)
    widget.setAllColumnsShowFocus(True)
    widget.setItemDelegate(CheckableItemDelegate(widget))
    widget.header().setMinimumSectionSize(65)
    return widget


@dataclass(frozen=True)
class AccountRead:
    cookie: dict[str, str] = field(default_factory=dict, repr=False)
    profile: dict | None = None
    playlists: list[Playlist] | None = None
    note: str = ""


class Worker(QThread):
    result = Signal(str, object)
    failure = Signal(str)
    progress = Signal(int, str)
    qr_ready = Signal(bytes)
    qr_status = Signal(str)

    def __init__(self, kind: str, action: Callable, parent: QWidget, *, deliver_on_cancel: bool = False):
        super().__init__(parent)
        self.kind = kind
        self.action = action
        self.deliver_on_cancel = deliver_on_cancel

    def check_cancel(self) -> None:
        if self.isInterruptionRequested():
            raise Cancelled

    def run(self) -> None:
        try:
            result = self.action(self)
            if self.kind != "remove" and not self.deliver_on_cancel:
                self.check_cancel()
            self.result.emit(self.kind, result)
        except Cancelled:
            self.failure.emit("操作已取消")
        except ApiError as exc:
            self.failure.emit(str(exc))
        except Exception as exc:
            self.failure.emit(f"无法解析网易云接口数据（{type(exc).__name__}）")


class MainWindow(QMainWindow):
    def __init__(self, port: int, api_factory: Callable = NcmApi, session_store: SessionStore | None = None):
        super().__init__()
        from .collection_ui import CollectionPage
        from .management_ui import ManagementPage

        self.port = port
        self.api_factory = api_factory
        self.session_store = session_store if session_store is not None else SessionStore(port)
        self.job: Worker | None = None
        self.cookie: dict[str, str] = {}
        self.uid: int | None = None
        self.retry_at = 0.0
        self.cooldown_timer = QTimer(self)
        self.cooldown_timer.setInterval(1000)
        self.cooldown_timer.timeout.connect(self._result_selection)
        self.playlists: list[Playlist] = []
        self.playlists_loaded = False
        self.playlists_need_refresh = False
        self.result: ScanResult | None = None
        self.result_rows: list[QTreeWidgetItem] = []
        self.close_after_job = False
        self.qr_dialog: QDialog | None = None
        self.setWindowTitle("网易云歌单工具")
        self.resize(1000, 700)
        self.setMinimumSize(840, 550)
        self.setStyleSheet(STYLE)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(30, 25, 30, 23)
        layout.setSpacing(16)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(3)
        heading.addWidget(label("网易云歌单工具", "title"))
        self.session = label("未登录", "session")
        heading.addWidget(self.session)
        header.addLayout(heading)
        header.addStretch()
        self.overview_button = button("总览", self._show_overview)
        header.addWidget(self.overview_button)
        self.cookie_button = button("网页 Cookie", self._login_cookie)
        self.login_button = button("扫码登录", self._login_qr, primary=True)
        self.restore_button = button("恢复登录", self.restore_session)
        self.logout_button = button("退出登录", self._forget_session)
        self.logout_button.setToolTip("清除本工具保存的网易云登录凭据")
        header.addWidget(self.restore_button)
        header.addWidget(self.logout_button)
        header.addWidget(self.cookie_button)
        header.addWidget(self.login_button)
        layout.addLayout(header)

        features = QHBoxLayout()
        self.duplicate_button = button("歌单查重", self._open_duplicates)
        self.collection_button = button("歌单归集与分类", self._open_collection)
        self.management_button = button("歌单管理", self._open_management)
        self.feature_buttons = (self.duplicate_button, self.collection_button, self.management_button)
        for entry in self.feature_buttons:
            entry.setObjectName("featureEntry")
            features.addWidget(entry, 1)
        layout.addLayout(features)

        self.pages = QStackedWidget()
        self.overview_page = self._overview_page()
        self.playlist_page = self._playlist_page()
        self.results_page = self._results_page()
        self.collection_page = CollectionPage(self, self.api_factory, self.port)
        self.management_page = ManagementPage(self, self.api_factory, self.port)
        self.worker_pages = (self.collection_page, self.management_page)
        for page in (self.overview_page, self.playlist_page, self.results_page, *self.worker_pages):
            self.pages.addWidget(page)
        self.duplicate_page = self.playlist_page
        layout.addWidget(self.pages, 1)

        self.main_footer = QWidget()
        footer_layout = QVBoxLayout(self.main_footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(6)
        footer = QHBoxLayout()
        self.status = label("登录后读取歌单", "status")
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.progress_value = label("", "status")
        footer.addWidget(self.progress_value)
        self.cancel = button("取消", self._cancel)
        self.cancel.hide()
        footer.addWidget(self.cancel)
        footer_layout.addLayout(footer)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(7)
        self.progress.hide()
        footer_layout.addWidget(self.progress)
        layout.addWidget(self.main_footer)
        self.pages.currentChanged.connect(self._sync_page_chrome)
        for page in self.worker_pages:
            page.busy_changed.connect(self._feature_busy_changed)
        self.collection_page.playlists_changed.connect(self._collection_playlists_changed)
        self.management_page.playlists_changed.connect(self._playlists_changed)
        self._playlist_selection()
        self._result_selection()
        self._account_controls(False)
        self._sync_page_chrome()

    def _card(self) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(17, 15, 17, 15)
        layout.setSpacing(11)
        return card, layout

    def _overview_page(self) -> QWidget:
        page, layout = self._card()
        layout.addWidget(label("歌单总览", "section"))
        metrics = QHBoxLayout()
        self.owned_count = label("—", "metricValue")
        self.liked_count = label("—", "metricValue")
        for caption, value in (("自建歌单", self.owned_count), ("我喜欢的音乐", self.liked_count)):
            tile = QFrame()
            tile.setObjectName("overviewMetric")
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(14, 11, 14, 11)
            tile_layout.setSpacing(4)
            tile_layout.addWidget(label(caption, "summary"))
            tile_layout.addWidget(value)
            metrics.addWidget(tile, 1)
        layout.addLayout(metrics)
        layout.addStretch(1)
        return page

    def _playlist_page(self) -> QWidget:
        page, layout = self._card()
        top = QHBoxLayout()
        top.addWidget(label("选择歌单", "section"))
        top.addStretch()
        self.refresh = button("刷新歌单", self._refresh_playlists)
        self.refresh.setEnabled(False)
        top.addWidget(self.refresh)
        layout.addLayout(top)
        self.playlist_table = table(["歌单", "类型", "歌曲数"])
        header = self.playlist_table.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.playlist_table.setColumnWidth(1, 100)
        self.playlist_table.setColumnWidth(2, 100)
        self.playlist_table.setRootIsDecorated(False)
        self.playlist_table.itemChanged.connect(self._playlist_selection)
        layout.addWidget(self.playlist_table, 1)

        row = QHBoxLayout()
        self.select_playlists = button("全选", lambda: self._check_playlists(CHECKED))
        self.clear_playlists = button("清空", lambda: self._check_playlists(UNCHECKED))
        row.addWidget(self.select_playlists)
        row.addWidget(self.clear_playlists)
        self.playlist_selection = label("", "selection")
        row.addWidget(self.playlist_selection)
        row.addStretch()
        self.combine = VisibleCheckBox("跨歌单查重")
        self.combine.setToolTip("合并所选歌单中的歌曲后按歌名查重。同一歌曲出现在多个歌单时也会列出。")
        row.addWidget(self.combine)
        self.scan = button("开始查重", self._scan, primary=True)
        row.addWidget(self.scan)
        layout.addLayout(row)
        return page

    def _results_page(self) -> QWidget:
        page, layout = self._card()
        top = QHBoxLayout()
        top.addWidget(label("同名歌曲", "section"))
        self.summary = label("", "summary")
        top.addWidget(self.summary)
        top.addStretch()
        self.catalog_button = QPushButton("搜索站内同名版本")
        menu = QMenu(self.catalog_button)
        current = menu.addAction("搜索当前同名组")
        current.triggered.connect(lambda: self._search_catalog(all_groups=False))
        menu.addAction("搜索全部同名组").triggered.connect(lambda: self._search_catalog(all_groups=True))
        menu.aboutToShow.connect(lambda: current.setEnabled(self._current_group_key() is not None))
        self.catalog_button.setMenu(menu)
        self.catalog_button.setToolTip("在结果表选一行后，可搜索当前同名组。搜索全部组耗时更长。")
        top.addWidget(self.catalog_button)
        self.back = button("返回歌单", self._back)
        top.addWidget(self.back)
        layout.addLayout(top)
        self.result_table = table(["歌曲", "歌手", "收藏（红心）", "评论", "所属歌单", "播放状态"])
        self.result_table.setIndentation(18)
        self.result_table.setExpandsOnDoubleClick(False)
        header = self.result_table.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        for column, width in [(1, 143), (2, 108), (3, 78), (4, 157), (5, 132)]:
            self.result_table.setColumnWidth(column, width)
        self.result_table.itemChanged.connect(self._result_selection)
        layout.addWidget(self.result_table, 1)
        row = QHBoxLayout()
        self.select_results = button("全选", lambda: self._check_results(CHECKED))
        self.clear_results = button("清空", lambda: self._check_results(UNCHECKED))
        row.addWidget(self.select_results)
        row.addWidget(self.clear_results)
        self.result_selection = label("", "selection")
        row.addWidget(self.result_selection)
        row.addStretch()
        self.detail = button("查看详情", self._show_warnings)
        self.detail.hide()
        row.addWidget(self.detail)
        self.remove = button("从歌单移除所选", self._remove_checked)
        self.remove.setObjectName("remove")
        row.addWidget(self.remove)
        layout.addLayout(row)
        self.warnings: list[str] = []
        return page

    def _launch(self, kind: str, action: Callable, message: str, determinate: bool = False) -> None:
        if self._is_busy():
            return
        worker = Worker(kind, action, self)
        worker.progress.connect(self._update_progress)
        worker.result.connect(self._receive)
        worker.failure.connect(self._failure)
        worker.finished.connect(self._finished)
        if self.qr_dialog is not None:
            worker.qr_ready.connect(self._set_qr)
            worker.qr_status.connect(self.qr_hint.setText)
        self.job = worker
        self._set_busy(True)
        self._sync_page_chrome()
        self.progress.show()
        self.progress.setRange(0, 100 if determinate else 0)
        self.progress.setValue(0)
        self.progress_value.setText("0%" if determinate else "")
        self.status.setText(message)
        self.status.setToolTip("")
        worker.start()

    def _set_busy(self, busy: bool) -> None:
        for control in (self.cookie_button, self.login_button, self.playlist_table,
                        self.result_table, self.combine, self.back, self.detail):
            control.setEnabled(not busy)
        self.refresh.setEnabled(not busy and bool(self.cookie))
        self._account_controls(busy)
        main_busy = self.job is not None
        removing = main_busy and self.job.kind == "remove"
        self.cancel.setText("停止剩余操作" if removing else "取消")
        self.cancel.setVisible(main_busy)
        self.cancel.setEnabled(main_busy)
        self._playlist_selection()
        self._result_selection()
        active = self._busy_page()
        for page in self.worker_pages:
            page.set_external_busy(main_busy or (active is not None and active is not page))

    def _busy_page(self) -> QWidget | None:
        return next((page for page in self.worker_pages if page.busy), None)

    def _is_busy(self) -> bool:
        return self.job is not None or self._busy_page() is not None

    def _account_controls(self, busy: bool) -> None:
        logged_in = self.uid is not None
        self.overview_button.setEnabled(not busy and self.pages.currentWidget() is not self.overview_page)
        self.restore_button.setVisible(not logged_in and self.session_store.available())
        self.restore_button.setEnabled(not busy)
        self.logout_button.setVisible(logged_in)
        self.logout_button.setEnabled(not busy)
        for entry in self.feature_buttons:
            entry.setEnabled(not busy)

    def _sync_page_chrome(self, *_args) -> None:
        page = self.pages.currentWidget()
        self.main_footer.setVisible(page not in self.worker_pages or self.job is not None)
        for entry, selected in (
            (self.duplicate_button, page in (self.playlist_page, self.results_page)),
            (self.collection_button, page is self.collection_page),
            (self.management_button, page is self.management_page),
        ):
            if entry.property("current") != selected:
                entry.setProperty("current", selected)
                entry.style().unpolish(entry)
                entry.style().polish(entry)
                entry.update()
        self._account_controls(self._is_busy())

    def _update_progress(self, value: int, message: str) -> None:
        self.progress.setValue(value)
        self.progress_value.setText(f"{value}%")
        self.status.setText(message)

    def _cancel(self) -> None:
        if self.job is not None:
            self.job.requestInterruption()
            self.status.setText("正在停止并核对已处理条目" if self.job.kind == "remove" else "正在取消")
            self.cancel.setEnabled(False)

    def _finished(self) -> None:
        if self.job is not None:
            self.job.deleteLater()
            self.job = None
        self._set_busy(False)
        self._sync_page_chrome()
        if self.close_after_job:
            self.close()
        elif self.pages.currentWidget() is self.management_page:
            self.management_page.ensure_loaded()

    def _failure(self, message: str) -> None:
        self._close_qr()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress_value.clear()
        self.progress.hide()
        self.status.setText(message.splitlines()[0])
        self.status.setToolTip(message)
        if message != "操作已取消" and not self.close_after_job:
            title = "移除失败" if self.job is not None and self.job.kind == "remove" else "读取失败"
            dialog = QMessageBox(QMessageBox.Icon.Warning, title, message, parent=self)
            dialog.setTextFormat(Qt.TextFormat.PlainText)
            dialog.exec()

    def _receive(self, kind: str, data: object) -> None:
        if kind in {"login", "refresh", "restore"}:
            self._close_qr()
            if data.profile is None:
                self._reset_status(data.note or "登录后读取歌单")
                return
            profile = data.profile
            new_uid = int(profile["userId"])
            if self.uid is not None and self.uid != new_uid:
                self._set_results(ScanResult({}, {}, {}, []))
                self.result = None
                self.retry_at = 0
                self.duplicate_page = self.playlist_page
                if self.pages.currentWidget() is self.results_page:
                    self.pages.setCurrentWidget(self.playlist_page)
            self.cookie = data.cookie
            preserve_selection = kind == "refresh" and new_uid == self.uid
            self.uid = new_uid
            self.session.setText(f"{profile.get('nickname') or '已登录'}  ·  UID {profile['userId']}")
            for page in self.worker_pages:
                page.set_session(self.cookie, new_uid)
            self.playlists_loaded = data.playlists is not None
            self.playlists_need_refresh = False
            self._set_playlists(data.playlists or [], keep_checks=preserve_selection)
            self._reset_status(data.note or f"已读取 {len(self.playlists)} 个歌单")
        elif kind == "forget":
            self.cookie = {}
            self.uid = None
            self.retry_at = 0
            self.playlists_loaded = False
            self.playlists_need_refresh = False
            self.session.setText("未登录")
            for page in self.worker_pages:
                page.set_session({}, None)
            self._set_playlists([], keep_checks=False)
            self._set_results(ScanResult({}, {}, {}, []))
            self.result = None
            self.duplicate_page = self.playlist_page
            self.pages.setCurrentWidget(self.overview_page)
            self._reset_status("本工具的登录已清除")
        elif kind == "scan":
            self._set_results(data)
            self.duplicate_page = self.results_page
            self.pages.setCurrentWidget(self.results_page)
            self._update_progress(100, "查重完成" if not data.warnings else "查重完成，部分数据未能读取")
        elif kind == "remove":
            self._show_removal_report(data)
        elif kind == "catalog":
            self._update_progress(100, "站内搜索结束")
            if not self.close_after_job:
                self._show_catalog(data)

    def _login_cookie(self) -> None:
        raw, accepted = QInputDialog.getText(
            self, "使用网页 Cookie", "粘贴 music.163.com 的 Cookie 请求标头",
            QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return
        try:
            candidate = parse_cookie(raw)
        except ApiError as exc:
            self._failure(str(exc))
            return
        self._read_account("login", candidate)

    def _login_qr(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("网易云音乐扫码登录")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 19, 24, 20)
        self.qr_image = label("")
        self.qr_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_image.setFixedSize(236, 236)
        self.qr_image.setStyleSheet("background: white; border-radius: 8px;")
        layout.addWidget(self.qr_image, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.qr_hint = label("正在取得二维码", "status")
        self.qr_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.qr_hint)
        buttons = QDialogButtonBox()
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.rejected.connect(self._cancel)
        self.qr_dialog = dialog
        self._read_account("login", None)
        dialog.open()

    def _set_qr(self, image: bytes) -> None:
        if self.qr_dialog is None:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(image, "PNG"):
            self.qr_image.setPixmap(pixmap.scaled(
                220, 220, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation,
            ))
            self.qr_hint.setText("用手机网易云音乐 App 扫码")

    def _close_qr(self) -> None:
        if self.qr_dialog is not None:
            self.qr_dialog.accept()
            self.qr_dialog.deleteLater()
            self.qr_dialog = None

    def _refresh_playlists(self) -> None:
        self._read_account("refresh", self.cookie)

    def restore_session(self) -> None:
        if self.job is not None or self.uid is not None or not self.session_store.available():
            return

        def restore(worker: Worker) -> AccountRead:
            try:
                cookie = self.session_store.load()
            except SessionStoreError as exc:
                return AccountRead(note=f"{exc}。可使用扫码登录。")
            if cookie is None:
                return AccountRead(note="扫码登录后可记住会话")
            api = self.api_factory(self.port, worker.check_cancel)
            api.cookie = cookie
            try:
                profile = api.login_status()
            except ApiSessionExpired:
                try:
                    self.session_store.forget()
                except SessionStoreError:
                    pass
                return AccountRead(note="已保存的会话已过期，请重新扫码登录")
            except ApiError as exc:
                return AccountRead(note=f"暂时无法恢复登录。{exc}。可稍后点「恢复登录」。")
            return self._account_playlists(api, profile, "restore")

        self._launch("restore", restore, "正在恢复上次登录")

    def _forget_session(self) -> None:
        def forget(_worker: Worker) -> None:
            try:
                self.session_store.forget()
            except SessionStoreError as exc:
                raise ApiError(str(exc)) from None

        self._launch("forget", forget, "正在清除本工具的登录")

    def _read_account(self, kind: str, cookie: dict[str, str] | None) -> None:
        def read(worker: Worker) -> AccountRead:
            api = self.api_factory(self.port, worker.check_cancel)
            api.cookie = cookie if cookie is not None else scan_cookie(
                api, worker.qr_ready.emit, worker.qr_status.emit,
            )
            profile = api.login_status()
            note = ""
            if kind == "login" and self.session_store.available():
                try:
                    self.session_store.save(api.cookie)
                except SessionStoreError as exc:
                    note = f"本次已登录，无法记住下次登录。{exc}"
            return self._account_playlists(api, profile, kind, note)

        self._launch(kind, read, "正在登录并读取歌单" if kind == "login" else "正在刷新歌单")

    def _account_playlists(self, api: NcmApi, profile: dict, kind: str, note: str = "") -> AccountRead:
        try:
            playlists = playlists_for(api, int(profile["userId"]))
        except ApiError as exc:
            if kind == "refresh":
                raise
            message = f"已登录，读取歌单失败。{exc}"
            return AccountRead(api.cookie, profile, note=f"{note} {message}".strip())
        if kind == "restore":
            note = f"已恢复上次登录 · {len(playlists)} 个歌单"
        return AccountRead(api.cookie, profile, playlists, note)

    def _open_collection(self) -> None:
        if not self._is_busy():
            self.pages.setCurrentWidget(self.collection_page)

    def _open_management(self) -> None:
        if not self._is_busy():
            self.pages.setCurrentWidget(self.management_page)
            self.management_page.ensure_loaded()

    def _open_duplicates(self) -> None:
        self._open_main_page(self.duplicate_page)

    def _show_overview(self) -> None:
        self._open_main_page(self.overview_page)

    def _open_main_page(self, page: QWidget) -> None:
        if self._is_busy():
            return
        if self.pages.currentWidget() is not page:
            message = "登录后读取歌单"
            if self.uid is not None:
                if page is self.overview_page:
                    message = "选择一个功能开始"
                elif page is self.playlist_page:
                    message = "勾选要查重的歌单"
                else:
                    message = "本次查重结果"
            self._reset_status(message)
            self.pages.setCurrentWidget(page)
        if self.playlists_need_refresh:
            self._refresh_playlists()

    def _feature_busy_changed(self, _busy: bool) -> None:
        busy = self._is_busy()
        self._set_busy(busy)
        self._sync_page_chrome()
        if not busy and self.close_after_job:
            self.close()

    def _collection_playlists_changed(self) -> None:
        self.management_page.invalidate()
        self._playlists_changed()

    def _playlists_changed(self) -> None:
        self.playlists_need_refresh = True

    def _set_playlists(self, playlists: list[Playlist], keep_checks: bool) -> None:
        checked = {item.id for item in self._checked_playlists()} if keep_checks else set()
        self.playlist_table.blockSignals(True)
        self.playlist_table.clear()
        self.playlists = playlists
        for index, playlist in enumerate(playlists):
            item = QTreeWidgetItem([playlist.name, playlist.category, f"{playlist.track_count:,}"])
            item.setData(0, USER, index)
            item.setToolTip(0, f"{playlist.name} · ID {playlist.id}")
            item.setCheckState(0, CHECKED if playlist.id in checked else UNCHECKED)
            item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.playlist_table.addTopLevelItem(item)
        self.playlist_table.blockSignals(False)
        self._playlist_selection()
        liked = next((playlist.track_count for playlist in playlists if playlist.category == "喜欢"), None)
        owned = sum(playlist.category == "自建" for playlist in playlists)
        self.owned_count.setText(f"{owned:,} 个" if self.playlists_loaded else "—")
        self.liked_count.setText(f"{liked:,} 首" if liked is not None else "—")

    def _checked_playlists(self) -> list[Playlist]:
        return [
            self.playlists[item.data(0, USER)]
            for index in range(self.playlist_table.topLevelItemCount())
            if (item := self.playlist_table.topLevelItem(index)).checkState(0) == CHECKED
        ]

    def _playlist_selection(self, *_args) -> None:
        count = len(self._checked_playlists())
        self.playlist_selection.setText(f"已选 {count} / {len(self.playlists)} 个")
        idle = not self._is_busy()
        self.select_playlists.setEnabled(idle and bool(self.playlists))
        self.clear_playlists.setEnabled(idle and count > 0)
        self.scan.setEnabled(idle and count > 0)

    def _check_playlists(self, check_state: Qt.CheckState) -> None:
        table = self.playlist_table
        table.blockSignals(True)
        for index in range(table.topLevelItemCount()):
            table.topLevelItem(index).setCheckState(0, check_state)
        table.blockSignals(False)
        self._playlist_selection()

    def _scan(self) -> None:
        playlists = self._checked_playlists()
        if not playlists:
            return
        combine = self.combine.isChecked()
        cookie = self.cookie.copy()

        def scan(worker: Worker) -> ScanResult:
            api = self.api_factory(self.port, worker.check_cancel)
            api.cookie = cookie
            return collect_duplicates(api, playlists, combine, worker.progress.emit)

        self._launch("scan", scan, "正在读取所选歌单", determinate=True)

    def _set_results(self, result: ScanResult, checked: set[TrackKey] | None = None) -> None:
        self.result = result
        checked = checked or set()
        results = self.result_table
        results.blockSignals(True)
        results.setUpdatesEnabled(False)
        self.result_rows.clear()
        results.clear()
        for key, rows in result.groups.items():
            group = QTreeWidgetItem([f"{title_prefix(rows[0].title)}  ·  {len(rows)} 条"])
            group.setData(0, USER, key)
            group.setFlags(group.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
            group.setCheckState(0, UNCHECKED)
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            group.setForeground(0, QColor("#4856a3"))
            results.addTopLevelItem(group)
            for row in rows:
                hearts = result.hearts.get(row.song_id)
                comments = result.comments.get(row.song_id)
                playback = result.playback.get(row.song_id)
                child = QTreeWidgetItem(group, [
                    row.title, row.artists or "—",
                    "—" if hearts is None else f"{hearts:,}",
                    "—" if comments is None else f"{comments:,}",
                    row.playlist.name, playback.status if playback else "状态未知",
                ])
                child.setData(0, USER, row)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, CHECKED if row.key in checked else UNCHECKED)
                child.setToolTip(0, f"{row.title} · 歌曲 ID {row.song_id}")
                child.setToolTip(1, row.artists)
                playlist_tip = f"{row.playlist.name} · 歌单 ID {row.playlist.id}"
                if not row.playlist.editable:
                    playlist_tip += "\n此歌单仅可查重"
                child.setToolTip(4, playlist_tip)
                if playback and playback.versions:
                    child.setForeground(5, QColor("#4856a3"))
                    child.setToolTip(5, "网易云推荐的其他版本\n" + "\n".join(
                        f"{name or '其他版本'} · 歌曲 ID {sid}" for sid, name in playback.versions
                    ))
                else:
                    child.setToolTip(5, "根据当前账号的歌曲详情判断，实际播放以网易云客户端为准。")
                for column in (2, 3):
                    child.setTextAlignment(column, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.result_rows.append(child)
            group.setExpanded(True)
        results.setUpdatesEnabled(True)
        results.blockSignals(False)
        self.warnings = result.warnings.copy()
        self.detail.setVisible(bool(self.warnings))
        self.summary.setText(
            f"{len(result.groups)} 组 · {len(self.result_rows)} 条" if result.groups else "未发现同名歌曲"
        )
        self._result_selection()

    def _check_results(self, check_state: Qt.CheckState) -> None:
        results = self.result_table
        results.blockSignals(True)
        for index in range(results.topLevelItemCount()):
            results.topLevelItem(index).setCheckState(0, check_state)
        results.blockSignals(False)
        self._result_selection()

    def _checked_results(self) -> list[Occurrence]:
        return [item.data(0, USER) for item in self.result_rows if item.checkState(0) == CHECKED]

    def _current_group_key(self) -> str | None:
        if self.result is None or not self.result.groups:
            return None
        item = self.result_table.currentItem()
        if item is not None:
            group = item.parent() or item
            return group.data(0, USER)
        if len(self.result.groups) == 1:
            return next(iter(self.result.groups))
        return None

    def _search_catalog(self, *, all_groups: bool) -> None:
        if self.result is None or self.job is not None:
            return
        key = self._current_group_key()
        groups = self.result.groups if all_groups else (
            {key: self.result.groups[key]} if key is not None else {}
        )
        if not groups:
            return
        cookie = self.cookie.copy()

        def search(worker: Worker) -> CatalogComparison:
            api = self.api_factory(self.port, worker.check_cancel)
            api.cookie = cookie
            return compare_catalog(api, groups, worker.progress.emit)

        self._launch("catalog", search, "正在搜索网易云同名版本", determinate=True)

    def _show_catalog(self, comparison: CatalogComparison) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("站内同名版本")
        dialog.resize(1000, 590)
        dialog.setMinimumSize(760, 420)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 16, 18, 16)
        external = sum(not track.playlists for group in comparison.groups for track in group.tracks)
        layout.addWidget(label(f"{len(comparison.groups)} 个歌名 · {external} 首站内候选"
                               f" · 按红心人数排序", "section"))
        results = table(["歌曲", "歌手", "专辑", "红心人数", "来源", "歌曲 ID"])
        results.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in [(1, 135), (2, 135), (3, 100), (4, 150), (5, 125)]:
            results.setColumnWidth(column, width)
        layout.addWidget(results, 1)
        for index, group in enumerate(comparison.groups):
            item = QTreeWidgetItem(results, [f"{group.title}  ·  {len(group.tracks)} 首"])
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
            for track in group.tracks:
                source = " / ".join(track.playlists) or "站内候选"
                row = QTreeWidgetItem(item, [
                    track.title, track.artists or "—", track.album or "—",
                    f"{track.hearts:,}" if track.hearts is not None else "—", source, str(track.song_id),
                ])
                row.setTextAlignment(3, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                for column in (0, 1, 2, 4, 5):
                    row.setToolTip(column, row.text(column))
            item.setExpanded(index == 0)
        if comparison.notes:
            notes = QPlainTextEdit("\n".join(comparison.notes))
            notes.setReadOnly(True)
            notes.setMaximumHeight(97)
            layout.addWidget(notes)
        buttons = QDialogButtonBox()
        buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.open()

    def _result_selection(self, *_args) -> None:
        checked = self._checked_results()
        count = len(checked)
        self.result_selection.setText(f"已勾选 {count} / {len(self.result_rows)} 条")
        idle = not self._is_busy()
        self.catalog_button.setEnabled(idle and self.result is not None and bool(self.result.groups))
        self.select_results.setEnabled(idle and bool(self.result_rows))
        self.clear_results.setEnabled(idle and count > 0)
        writable = sum(row.playlist.editable for row in checked)
        seconds = max(0, ceil(self.retry_at - time.monotonic()))
        if seconds == 0:
            self.cooldown_timer.stop()
        self.remove.setText(f"稍后重试（{seconds} 秒）" if seconds else "从歌单移除所选")
        self.remove.setEnabled(idle and self.uid is not None and writable > 0 and seconds == 0)
        self.remove.setToolTip(f"{writable} 条可移除，{count - writable} 条仅可查重")

    def _remove_checked(self) -> None:
        uid = self.uid
        if uid is None or self.job is not None or self.retry_at > time.monotonic():
            return
        checked = self._checked_results()
        selected = {row.key: row for row in checked if row.playlist.editable}
        if not selected:
            return
        skipped = len({row.key for row in checked if not row.playlist.editable})
        rows = list(selected.values())
        if not self._confirm_removal(rows, skipped):
            return
        cookie = self.cookie.copy()

        def remove(worker: Worker) -> RemovalReport:
            # Wait for the in-flight request and re-read the playlist even after Stop.
            api = self.api_factory(self.port, lambda: None)
            api.cookie = cookie
            return remove_tracks(api, uid, rows, worker.progress.emit, worker.isInterruptionRequested)

        self._launch("remove", remove, "正在核对账号及歌单权限", determinate=True)

    def _confirm_removal(self, rows: list[Occurrence], readonly_count: int) -> bool:
        dialog = QDialog(self)
        dialog.setWindowTitle("确认从歌单移除")
        dialog.resize(690, 475)
        dialog.setMinimumSize(530, 330)
        layout = QVBoxLayout(dialog)
        playlists = by_playlist(rows)
        heading = label(f"从 {len(playlists)} 个歌单移除 {len(rows)} 条歌曲", "section")
        layout.addWidget(heading)
        layout.addWidget(label("遇到限流时会自动等待并继续。执行期间可停止剩余操作。"))
        if readonly_count:
            layout.addWidget(label(f"同时勾选了 {readonly_count} 条仅可查重的歌曲，确认范围见下表。"))
        preview = table(["歌单与歌曲", "歌手", "操作"])
        preview.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        preview.setColumnWidth(1, 150)
        preview.setColumnWidth(2, 115)
        layout.addWidget(preview, 1)
        for songs in playlists.values():
            first = next(iter(songs.values()))
            action = "取消红心" if first.playlist.category == "喜欢" else "从歌单移除"
            group = QTreeWidgetItem(preview, [f"{first.playlist.name}  ·  {len(songs)} 条", "", action])
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            for song in songs.values():
                item = QTreeWidgetItem(group, [song.title, song.artists or "—", action])
                item.setToolTip(0, f"{song.title} · 歌曲 ID {song.song_id}")
                item.setToolTip(1, song.artists)
            group.setExpanded(True)
        buttons = QDialogButtonBox()
        cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        confirm = buttons.addButton("确认移除", QDialogButtonBox.ButtonRole.AcceptRole)
        confirm.setObjectName("remove")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        cancel.setDefault(True)
        cancel.setFocus()
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _show_removal_report(self, report: RemovalReport) -> None:
        if report.attempted or report.already_absent:
            self.management_page.invalidate()
        if self.result is None:
            return
        if report.retry_at:
            self.retry_at = report.retry_at
            self.cooldown_timer.start()
        checked = {row.key for row in self._checked_results()}
        updated = {
            p.id: replace(p, track_count=report.track_counts[p.id])
            for p in self.playlists if p.id in report.track_counts
        }
        if updated:
            self._set_playlists([updated.get(p.id, p) for p in self.playlists], keep_checks=True)

        groups = {}
        failures = []
        done = report.removed | report.already_absent
        for key, rows in self.result.groups.items():
            remaining = []
            for row in rows:
                if row.key in done:
                    continue
                if row.key in report.failures:
                    failures.append(f"{row.playlist.name} · {row.title} · {row.artists or row.song_id} · "
                                    f"{report.failures[row.key]}")
                remaining.append(row)
            if remaining:
                groups[key] = remaining
        self._set_results(replace(self.result, groups=groups), checked - done)
        if not groups:
            self.summary.setText("当前结果已清空")
        self.warnings.extend(failures)
        self.detail.setVisible(bool(self.warnings))
        summary = [f"已移除 {len(report.removed)} 条"]
        if report.already_absent:
            summary.append(f"原本已不在歌单 {len(report.already_absent)} 条")
        if report.failures:
            summary.append(f"未确认移除 {len(report.failures)} 条")
        if report.pending:
            summary.append(f"未处理 {report.pending} 条")
        if report.stopped:
            summary.insert(0, "已停止")
        if report.retry_at:
            summary.append("冷却时间尚未结束")
        self._update_progress(100, " · ".join(summary))

    def _show_warnings(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("处理详情")
        dialog.resize(660, 390)
        layout = QVBoxLayout(dialog)
        details = QPlainTextEdit("\n".join(self.warnings))
        details.setReadOnly(True)
        layout.addWidget(details)
        buttons = QDialogButtonBox()
        buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _back(self) -> None:
        self.duplicate_page = self.playlist_page
        self._open_main_page(self.playlist_page)

    def _reset_status(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.hide()
        self.progress_value.clear()
        self.status.setText(message)

    def closeEvent(self, event: QCloseEvent) -> None:
        feature = self._busy_page()
        if self.job is not None:
            self.close_after_job = True
            self._cancel()
            event.ignore()
        elif feature is not None:
            self.close_after_job = True
            feature.stop()
            event.ignore()
        else:
            event.accept()


def main() -> int:
    parser = argparse.ArgumentParser(prog="run.py")
    parser.add_argument("--api-port", type=int, default=3000, help="本机或 SSH 转发的 API 端口")
    args = parser.parse_args()
    if not 1 <= args.api_port <= 65535:
        parser.error("--api-port 应在 1 到 65535 之间")
    app = QApplication([sys.argv[0]])
    configure_theme(app)
    app.setApplicationName("网易云歌单工具")
    window = MainWindow(args.api_port)
    window.show()
    QTimer.singleShot(0, window.restore_session)
    return app.exec()
