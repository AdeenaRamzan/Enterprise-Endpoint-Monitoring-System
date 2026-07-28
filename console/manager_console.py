"""
Manager mode: built for a non-technical user. One screen, one level of
navigation depth (card -> that employee's gallery), no menus/settings.
Session persists between launches so the manager just double-clicks a
shortcut and sees the grid -- no re-login friction (see session_store.py).
"""

import sys
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QFrame, QListWidget, QListWidgetItem,
    QStackedWidget, QDialog
)
from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import QColor, QPalette, QPixmap

STATUS_COLORS = {"active": "#2e7d32", "idle": "#f9a825", "offline": "#c62828"}
STATUS_LABELS = {"active": "Active now", "idle": "Idle", "offline": "Offline"}


def humanize_status(entry):
    status = entry.get("status", "offline")
    if status == "idle" and entry.get("idle_since"):
        import time
        minutes = int((time.time() - entry["idle_since"]) / 60)
        return f"Idle {minutes} min"
    if status == "offline":
        return "Offline"
    return "Active now"


class EmployeeCard(QFrame):
    THUMB_SIZE = QSize(198, 100)

    def __init__(self, api, employee_id, display_name, presence, on_click):
        super().__init__()
        self.api = api
        self.employee_id = employee_id
        self.setProperty("role", "card")
        self.setFixedSize(230, 210)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout()
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        self.thumb_label = QLabel("No screenshot yet")
        self.thumb_label.setFixedSize(self.THUMB_SIZE)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setStyleSheet("background-color: #0b0f14; color: #64748b; font-size: 11px;")
        layout.addWidget(self.thumb_label)

        top = QHBoxLayout()
        top.setSpacing(8)

        dot = QLabel("●")
        color = STATUS_COLORS.get(presence.get("status", "offline"), "#999")
        dot.setStyleSheet(f"color: {color}; font-size: 16px;")
        top.addWidget(dot)

        name_label = QLabel(display_name)
        name_label.setStyleSheet("font-weight: 600; font-size: 14px;")
        top.addWidget(name_label)
        top.addStretch()
        layout.addLayout(top)

        status_label = QLabel(humanize_status(presence))
        status_label.setStyleSheet("color: #94a3b8; font-size: 12px;")
        layout.addWidget(status_label)
        self.setLayout(layout)
        self._on_click = on_click

    def mousePressEvent(self, event):
        self._on_click(self.employee_id)

    def refresh_thumbnail(self):
        try:
            data = self.api.get_latest_screenshot_bytes(self.employee_id)
        except Exception:
            return
        if not data:
            return
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        self.thumb_label.setPixmap(pixmap.scaled(
            self.THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))


class GalleryView(QWidget):
    THUMB_SIZE = QSize(200, 112)

    def __init__(self, api, on_back):
        super().__init__()
        self.api = api
        layout = QVBoxLayout()

        back_btn = QPushButton("← Back to team")
        back_btn.clicked.connect(on_back)
        layout.addWidget(back_btn)

        self.title = QLabel("")
        self.title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(self.title)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #94a3b8;")
        layout.addWidget(self.status_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.grid_container = QWidget()
        self.grid = QGridLayout()
        self.grid.setSpacing(10)
        self.grid_container.setLayout(self.grid)
        self.scroll.setWidget(self.grid_container)
        layout.addWidget(self.scroll)
        self.setLayout(layout)

    def load(self, employee_id, display_name):
        self.title.setText(f"{display_name}'s screenshots")
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        try:
            shots = self.api.get_screenshots(employee_id)
        except Exception as e:
            self.status_label.setText(f"Could not load screenshots: {e}")
            return
        if not shots:
            self.status_label.setText("No screenshots yet.")
            return
        self.status_label.setText(f"{len(shots)} screenshot(s) — most recent first")
        col_count = 4
        for i, shot in enumerate(shots):
            self.grid.addWidget(self._make_thumbnail(employee_id, shot), i // col_count, i % col_count)

    def _make_thumbnail(self, employee_id, shot):
        frame = QFrame()
        frame.setCursor(Qt.CursorShape.PointingHandCursor)
        v = QVBoxLayout()
        v.setContentsMargins(6, 6, 6, 6)
        img_label = QLabel("Loading…")
        img_label.setFixedSize(self.THUMB_SIZE)
        img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        img_label.setStyleSheet("background-color: #0b0f14; color: #64748b;")
        v.addWidget(img_label)
        time_label = QLabel(shot["captured_at"])
        time_label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        v.addWidget(time_label)
        frame.setLayout(v)

        try:
            data = self.api.get_screenshot_bytes(employee_id, shot["id"])
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            img_label.setPixmap(pixmap.scaled(
                self.THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

            def _open(event, full=pixmap, cap=f"{shot['captured_at']}"):
                dlg = QDialog(self)
                dlg.setWindowTitle(cap)
                dl = QVBoxLayout()
                screen_size = QApplication.primaryScreen().availableSize()
                shown = full
                max_w, max_h = int(screen_size.width() * 0.8), int(screen_size.height() * 0.8)
                if full.width() > max_w or full.height() > max_h:
                    shown = full.scaled(max_w, max_h, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation)
                lbl = QLabel()
                lbl.setPixmap(shown)
                dl.addWidget(lbl)
                dlg.setLayout(dl)
                dlg.exec()
            img_label.mousePressEvent = _open
        except Exception:
            img_label.setText("Failed to load")

        return frame


class AlertsPanel(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.list_widget = QListWidget()
        self.list_widget.setMaximumHeight(140)
        layout.addWidget(self.list_widget)
        self.setLayout(layout)

    def refresh(self):
        self.list_widget.clear()
        try:
            events = self.api.get_alert_events()
        except Exception as e:
            self.list_widget.addItem(f"Could not load alerts: {e}")
            return
        if not events:
            self.list_widget.addItem("No alerts.")
        for e in events:
            # Plain-language message only -- never raw rule/technical text.
            self.list_widget.addItem(e["message"])


class ManagerMainWindow(QMainWindow):
    def __init__(self, api):
        super().__init__()
        self.api = api
        self.setWindowTitle(f"Team Monitor - {api.display_name}")
        self.resize(900, 600)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        # --- Grid page ---
        self.grid_page = QWidget()
        grid_layout = QVBoxLayout()
        grid_layout.setContentsMargins(24, 20, 24, 20)
        grid_layout.setSpacing(14)

        header = QLabel("Your Team")
        header.setProperty("role", "title")
        grid_layout.addWidget(header)

        search_row = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search by name...")
        self.search_box.textChanged.connect(self.render_grid)
        search_row.addWidget(self.search_box)
        grid_layout.addLayout(search_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.cards_container = QWidget()
        self.cards_grid = QGridLayout()
        self.cards_grid.setSpacing(14)
        self.cards_container.setLayout(self.cards_grid)
        self.scroll.setWidget(self.cards_container)
        grid_layout.addWidget(self.scroll)

        alerts_header = QLabel("Alerts")
        alerts_header.setProperty("role", "title")
        grid_layout.addWidget(alerts_header)

        self.alerts_panel = AlertsPanel(api)
        grid_layout.addWidget(self.alerts_panel)

        self.grid_page.setLayout(grid_layout)
        self.stack.addWidget(self.grid_page)

        # --- Gallery page ---
        self.gallery_view = GalleryView(api, on_back=self.show_grid)
        self.stack.addWidget(self.gallery_view)

        self.status_data = []
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.refresh_status)
        self.refresh_timer.start(5000)
        self.refresh_status()

    def refresh_status(self):
        try:
            self.status_data = self.api.get_status()
        except Exception:
            self.status_data = []
        self.render_grid()
        self.alerts_panel.refresh()

    def render_grid(self):
        # clear existing cards
        while self.cards_grid.count():
            item = self.cards_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        query = self.search_box.text().strip().lower()
        row, col, per_row = 0, 0, 3
        for entry in self.status_data:
            name = entry.get("employee_id", "unknown")
            if query and query not in name.lower():
                continue
            card = EmployeeCard(self.api, entry["employee_id"], name, entry, self.open_gallery)
            card.refresh_thumbnail()
            self.cards_grid.addWidget(card, row, col)
            col += 1
            if col >= per_row:
                col = 0
                row += 1

    def open_gallery(self, employee_id):
        self.gallery_view.load(employee_id, employee_id)
        self.stack.setCurrentWidget(self.gallery_view)

    def show_grid(self):
        self.stack.setCurrentWidget(self.grid_page)
        self.refresh_status()


def run_manager_console(api):
    app = QApplication.instance() or QApplication(sys.argv)
    window = ManagerMainWindow(api)
    window.show()
    return app, window
