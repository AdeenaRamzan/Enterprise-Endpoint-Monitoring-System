import sys
import json
import random
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem,
    QComboBox, QMessageBox, QFormLayout, QGridLayout, QScrollArea, QFrame, QDialog
)
from PyQt6.QtCore import QTimer, Qt, QSize
from PyQt6.QtGui import QPixmap, QPainter, QColor, QFont

from remote_view import RemoteViewWidget
from theme import COLORS


class StatusGridTab(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Employee ID", "Status", "Last seen"])
        layout.addWidget(self.table)
        self.setLayout(layout)

    def refresh(self):
        try:
            data = self.api.get_status()
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
            return
        self.table.setRowCount(len(data))
        for i, entry in enumerate(data):
            self.table.setItem(i, 0, QTableWidgetItem(entry["employee_id"]))
            self.table.setItem(i, 1, QTableWidgetItem(entry["status"]))
            last_seen = entry.get("last_seen")
            self.table.setItem(i, 2, QTableWidgetItem(str(last_seen) if last_seen else "-"))


class EnlargedImageDialog(QDialog):
    """Full-size view of one screenshot -- what a thumbnail 'enlarges' into."""
    def __init__(self, pixmap: QPixmap, caption: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(caption)
        layout = QVBoxLayout()
        label = QLabel()
        screen_size = QApplication.primaryScreen().availableSize()
        max_w, max_h = int(screen_size.width() * 0.85), int(screen_size.height() * 0.85)
        if pixmap.width() > max_w or pixmap.height() > max_h:
            pixmap = pixmap.scaled(max_w, max_h, Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
        label.setPixmap(pixmap)
        layout.addWidget(label)
        cap = QLabel(caption)
        cap.setStyleSheet("color: #94a3b8;")
        layout.addWidget(cap)
        self.setLayout(layout)


class GalleryTab(QWidget):
    """Thumbnail grid of an employee's screenshots. Click a thumbnail to enlarge it."""
    THUMB_SIZE = QSize(220, 124)

    def __init__(self, api):
        super().__init__()
        self.api = api
        self.shots = []
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        row = QHBoxLayout()
        self.employee_input = QLineEdit()
        self.employee_input.setPlaceholderText("employee_id")
        row.addWidget(self.employee_input)
        load_btn = QPushButton("Load gallery")
        load_btn.clicked.connect(self.load_gallery)
        row.addWidget(load_btn)
        layout.addLayout(row)

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

    def load_gallery(self):
        emp_id = self.employee_input.text().strip()
        if not emp_id:
            return
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        try:
            self.shots = self.api.get_screenshots(emp_id)
        except Exception as e:
            self.status_label.setText(f"Error: {e}")
            return
        if not self.shots:
            self.status_label.setText("No screenshots yet.")
            return
        self.status_label.setText(f"{len(self.shots)} screenshot(s) — most recent first")
        col_count = 4
        for i, shot in enumerate(self.shots):
            thumb = self._make_thumbnail(emp_id, shot)
            self.grid.addWidget(thumb, i // col_count, i % col_count)

    def _make_thumbnail(self, emp_id, shot):
        frame = QFrame()
        frame.setCursor(Qt.CursorShape.PointingHandCursor)
        frame.setFixedSize(self.THUMB_SIZE.width() + 16, self.THUMB_SIZE.height() + 40)
        v = QVBoxLayout()
        v.setContentsMargins(8, 8, 8, 8)
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
            data = self.api.get_screenshot_bytes(emp_id, shot["id"])
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            thumb_pixmap = pixmap.scaled(self.THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation)
            img_label.setPixmap(thumb_pixmap)

            def _open(event, full=pixmap, cap=f"{emp_id} — {shot['captured_at']}"):
                dlg = EnlargedImageDialog(full, cap, self)
                dlg.exec()
            img_label.mousePressEvent = _open
        except Exception:
            img_label.setText("Failed to load")

        return frame


class RemoteControlTab(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api
        self.session_id = None
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        row = QHBoxLayout()
        self.employee_input = QLineEdit()
        self.employee_input.setPlaceholderText("employee_id")
        row.addWidget(self.employee_input)
        layout.addLayout(row)

        self.remote_view = RemoteViewWidget()
        self.remote_view.setMinimumHeight(320)
        layout.addWidget(self.remote_view)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start Session")
        self.start_btn.clicked.connect(self.start_session)
        btn_row.addWidget(self.start_btn)
        self.end_btn = QPushButton("End Session")
        self.end_btn.clicked.connect(self.end_session)
        self.end_btn.setEnabled(False)
        btn_row.addWidget(self.end_btn)
        layout.addLayout(btn_row)

        self.status_label = QLabel("No active session.")
        layout.addWidget(self.status_label)

        blank_row = QHBoxLayout()
        self.blank_color = QComboBox()
        self.blank_color.addItems(["black", "white"])
        blank_row.addWidget(self.blank_color)
        self.blank_btn = QPushButton("Blank Screen: OFF")
        self.blank_btn.setCheckable(True)
        self.blank_btn.clicked.connect(self.toggle_blank)
        self.blank_btn.setEnabled(False)
        blank_row.addWidget(self.blank_btn)
        layout.addLayout(blank_row)

        self.blank_warning = QLabel("")
        self.blank_warning.setStyleSheet("color: red; font-weight: bold;")
        layout.addWidget(self.blank_warning)

        layout.addStretch()
        self.setLayout(layout)

    def start_session(self):
        emp_id = self.employee_input.text().strip()
        if not emp_id:
            return
        try:
            result = self.api.start_remote_session(emp_id)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
            return
        self.session_id = result["session_id"]
        self.status_label.setText(f"Session #{self.session_id} active with {emp_id}")
        self.start_btn.setEnabled(False)
        self.end_btn.setEnabled(True)
        self.blank_btn.setEnabled(True)
        self.remote_view.connect_to(self.api.remote_ws_url(emp_id), input_enabled=True)

    def end_session(self):
        if self.session_id is None:
            return
        try:
            self.api.end_remote_session(self.session_id)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
        self.remote_view.disconnect_stream()
        self.status_label.setText("No active session.")
        self.session_id = None
        self.start_btn.setEnabled(True)
        self.end_btn.setEnabled(False)
        self.blank_btn.setEnabled(False)
        self.blank_btn.setChecked(False)
        self.blank_btn.setText("Blank Screen: OFF")
        self.blank_warning.setText("")

    def open_for_employee(self, employee_id: str):
        """Called from the Live View thumbnail wall: pre-fill and auto-start."""
        self.employee_input.setText(employee_id)
        if self.session_id is None:
            self.start_session()

    def toggle_blank(self):
        emp_id = self.employee_input.text().strip()
        enabled = self.blank_btn.isChecked()
        try:
            self.api.set_blank_screen(emp_id, enabled, self.blank_color.currentText())
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
            return
        self.blank_btn.setText(f"Blank Screen: {'ON' if enabled else 'OFF'}")
        self.blank_warning.setText("⚠ Employee screen is blanked" if enabled else "")


class AlertRulesTab(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        form = QFormLayout()
        self.name_input = QLineEdit()
        form.addRow("Rule name", self.name_input)
        self.type_input = QComboBox()
        self.type_input.addItems(["idle_minutes", "after_hours", "blacklisted_app"])
        form.addRow("Rule type", self.type_input)
        self.params_input = QLineEdit()
        self.params_input.setPlaceholderText('e.g. {"minutes": 30}')
        form.addRow("Params (JSON)", self.params_input)
        self.notify_via = QComboBox()
        self.notify_via.addItems(["webhook", "smtp"])
        form.addRow("Notify via", self.notify_via)
        self.notify_target = QLineEdit()
        self.notify_target.setPlaceholderText("webhook URL or email")
        form.addRow("Notify target", self.notify_target)
        layout.addLayout(form)

        create_btn = QPushButton("Create rule")
        create_btn.clicked.connect(self.create_rule)
        layout.addWidget(create_btn)

        self.rules_list = QListWidget()
        layout.addWidget(self.rules_list)
        refresh_btn = QPushButton("Refresh rules")
        refresh_btn.clicked.connect(self.refresh)
        layout.addWidget(refresh_btn)

        self.setLayout(layout)

    def create_rule(self):
        try:
            params = json.loads(self.params_input.text() or "{}")
            self.api.create_alert_rule(
                self.name_input.text(), self.type_input.currentText(), params,
                self.notify_via.currentText(), self.notify_target.text(),
            )
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
            return
        self.refresh()

    def refresh(self):
        self.rules_list.clear()
        try:
            rules = self.api.list_alert_rules()
        except Exception as e:
            self.rules_list.addItem(f"Error: {e}")
            return
        for r in rules:
            self.rules_list.addItem(f"[{r['id']}] {r['name']} ({r['rule_type']}) -> {r['notify_via']}:{r['notify_target']}")


class UserManagementTab(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        form = QFormLayout()
        self.username_input = QLineEdit()
        form.addRow("Username", self.username_input)
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password", self.password_input)
        self.role_input = QComboBox()
        self.role_input.addItems(["SuperAdmin", "ITStaff", "Manager", "Viewer"])
        form.addRow("Role", self.role_input)
        self.display_name_input = QLineEdit()
        form.addRow("Display name", self.display_name_input)
        self.managed_ids_input = QLineEdit()
        self.managed_ids_input.setPlaceholderText('["emp001","emp002"] (Manager/Viewer only)')
        form.addRow("Managed employee IDs", self.managed_ids_input)
        layout.addLayout(form)

        create_btn = QPushButton("Create user")
        create_btn.clicked.connect(self.create_user)
        layout.addWidget(create_btn)

        self.users_list = QListWidget()
        layout.addWidget(self.users_list)
        refresh_btn = QPushButton("Refresh users")
        refresh_btn.clicked.connect(self.refresh)
        layout.addWidget(refresh_btn)

        self.setLayout(layout)

    def create_user(self):
        try:
            managed = json.loads(self.managed_ids_input.text() or "[]")
            self.api.create_user(
                self.username_input.text(), self.password_input.text(),
                self.role_input.currentText(), self.display_name_input.text(), managed,
            )
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
            return
        self.refresh()

    def refresh(self):
        self.users_list.clear()
        try:
            users = self.api.list_users()
        except Exception as e:
            self.users_list.addItem(f"Error: {e}")
            return
        for u in users:
            self.users_list.addItem(f"{u['username']} ({u['role']}) - {u['display_name']}")


class AuditLogTab(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        row = QHBoxLayout()
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter by employee_id (optional)")
        row.addWidget(self.filter_input)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        row.addWidget(refresh_btn)
        layout.addLayout(row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Actor", "Role", "Action", "Target", "At"])
        layout.addWidget(self.table)
        self.setLayout(layout)

    def refresh(self):
        try:
            rows = self.api.query_audit_log(self.filter_input.text().strip() or None)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
            return
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(r["actor"]))
            self.table.setItem(i, 1, QTableWidgetItem(r["role"]))
            self.table.setItem(i, 2, QTableWidgetItem(r["action"]))
            self.table.setItem(i, 3, QTableWidgetItem(r.get("target_employee_id") or "-"))
            self.table.setItem(i, 4, QTableWidgetItem(r["at"]))


class EmployeesTab(QWidget):
    """
    Machines show up here the moment their agent's consent dialog is
    accepted -- no manual registration step. IT can rename the display
    name to something friendlier than the raw hostname.
    """
    def __init__(self, api):
        super().__init__()
        self.api = api
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        info = QLabel("Employees appear here automatically once their agent is installed and consent is accepted.")
        info.setWordWrap(True)
        info.setStyleSheet("color: #94a3b8;")
        layout.addWidget(info)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Employee ID", "Display Name", "Hostname", "IP Address", "Last Seen"])
        self.table.cellDoubleClicked.connect(self.rename_selected)
        layout.addWidget(self.table)

        hint = QLabel("Double-click a row's Display Name to rename it.")
        hint.setStyleSheet("color: #64748b; font-size: 11px;")
        layout.addWidget(hint)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        layout.addWidget(refresh_btn)
        self.setLayout(layout)

    def refresh(self):
        try:
            employees = self.api.list_employees()
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
            return
        self._employees = employees
        self.table.setRowCount(len(employees))
        for i, e in enumerate(employees):
            self.table.setItem(i, 0, QTableWidgetItem(e["employee_id"]))
            self.table.setItem(i, 1, QTableWidgetItem(e["display_name"]))
            self.table.setItem(i, 2, QTableWidgetItem(e.get("hostname") or "-"))
            self.table.setItem(i, 3, QTableWidgetItem(e.get("ip_address") or "-"))
            self.table.setItem(i, 4, QTableWidgetItem(e.get("last_seen_at") or "-"))

    def rename_selected(self, row, column):
        if column != 1:
            return
        employee_id = self.table.item(row, 0).text()
        current_name = self.table.item(row, 1).text()
        from PyQt6.QtWidgets import QInputDialog
        new_name, ok = QInputDialog.getText(self, "Rename employee", "Display name:", text=current_name)
        if ok and new_name.strip():
            try:
                self.api.rename_employee(employee_id, new_name.strip())
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))
                return
            self.refresh()


class LiveThumbnail(QFrame):
    """
    One small tile in the wall. Shows the employee's most recent
    screenshot (refreshed on a timer -- same cadence as the screenshot
    pipeline, ~10s, not a full 8fps remote stream). Clicking it jumps
    into a real live remote-control session for just that employee --
    the wall itself never opens N simultaneous live sessions, which
    would be needlessly heavy on bandwidth/CPU for a "who's doing
    what" overview.
    """
    SIZE = QSize(200, 116)

    def __init__(self, api, employee, on_click):
        super().__init__()
        self.api = api
        self.employee_id = employee["employee_id"]
        self.on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.SIZE.width() + 16, self.SIZE.height() + 34)

        v = QVBoxLayout()
        v.setContentsMargins(8, 8, 8, 6)
        v.setSpacing(4)
        self.img_label = QLabel("No screenshot yet")
        self.img_label.setFixedSize(self.SIZE)
        self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_label.setStyleSheet("background-color: #0b0f14; color: #64748b; font-size: 11px;")
        v.addWidget(self.img_label)
        name_label = QLabel(employee["display_name"])
        name_label.setStyleSheet("font-weight: 600; font-size: 12px;")
        v.addWidget(name_label)
        self.setLayout(v)

    def mousePressEvent(self, event):
        self.on_click(self.employee_id)

    def refresh(self):
        try:
            data = self.api.get_latest_screenshot_bytes(self.employee_id)
        except Exception:
            return
        if not data:
            return
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        thumb = pixmap.scaled(self.SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        self.img_label.setPixmap(thumb)


class LiveViewTab(QWidget):
    def __init__(self, api, open_remote_control):
        super().__init__()
        self.api = api
        self.open_remote_control = open_remote_control
        self.tiles = {}

        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        info = QLabel("Click any tile to open a live, controllable view of that screen.")
        info.setStyleSheet("color: #94a3b8;")
        layout.addWidget(info)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.grid_container = QWidget()
        self.grid = QGridLayout()
        self.grid.setSpacing(12)
        self.grid_container.setLayout(self.grid)
        self.scroll.setWidget(self.grid_container)
        layout.addWidget(self.scroll)
        self.setLayout(layout)

        self.refresh_thumbs_timer = QTimer()
        self.refresh_thumbs_timer.timeout.connect(self._refresh_thumbnails)
        self.refresh_thumbs_timer.start(10000)

    def rebuild(self):
        """Call when the employee roster changes (e.g. tab opened, or after Employees tab refresh)."""
        try:
            employees = self.api.list_employees()
        except Exception:
            return
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.tiles = {}
        col_count = 5
        for i, emp in enumerate(employees):
            tile = LiveThumbnail(self.api, emp, self.open_remote_control)
            self.tiles[emp["employee_id"]] = tile
            self.grid.addWidget(tile, i // col_count, i % col_count)
        self._refresh_thumbnails()

    def _refresh_thumbnails(self):
        for tile in self.tiles.values():
            tile.refresh()


SEVERITY_COLORS = {
    "info": COLORS["primary"],
    "warn": COLORS["warning"],
    "critical": COLORS["danger"],
}
EVENT_TYPE_COLORS = {
    "BROWSER": COLORS["primary"],
    "USB": COLORS["warning"],
    "FILE": "#a855f7",       # purple-500, distinct from the others
    "APP": COLORS["danger"],
    "TELEMETRY": COLORS["text_dim"],
}


def _pill_label(text, color):
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"background-color: {color}; color: white; border-radius: 8px; "
        f"padding: 2px 8px; font-size: 11px; font-weight: 600;"
    )
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return lbl


class ActivityMonitorTab(QWidget):
    """
    Full activity log -- browser history, USB connect/disconnect, file
    operations, flagged-app launches -- filterable by type, severity,
    and employee. Backed by /activity, IT/SuperAdmin only (see the
    ActivityEvent docstring in backend/models.py for why this is gated
    tighter than the screenshot gallery).
    """
    def __init__(self, api):
        super().__init__()
        self.api = api
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("Activity Monitor")
        title.setProperty("role", "title")
        layout.addWidget(title)
        subtitle = QLabel("Complete log of employee endpoint activity — browser history, "
                           "file operations, USB events, and flagged applications.")
        subtitle.setProperty("role", "subtitle")
        layout.addWidget(subtitle)

        filters = QHBoxLayout()
        self.type_filter = QComboBox()
        self.type_filter.addItems(["All Types", "BROWSER", "USB", "FILE", "APP", "TELEMETRY"])
        self.severity_filter = QComboBox()
        self.severity_filter.addItems(["All Severities", "info", "warn", "critical"])
        self.employee_filter = QLineEdit()
        self.employee_filter.setPlaceholderText("employee_id (blank = everyone)")
        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.clicked.connect(self.load)
        for w in (self.type_filter, self.severity_filter, self.employee_filter, refresh_btn):
            filters.addWidget(w)
        layout.addLayout(filters)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Timestamp", "Type", "Employee", "Severity", "Details", "Log ID"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 160)
        self.table.setColumnWidth(3, 90)
        self.table.setColumnWidth(5, 60)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)
        self.setLayout(layout)

    def load(self):
        emp = self.employee_filter.text().strip() or "all"
        event_type = self.type_filter.currentText()
        severity = self.severity_filter.currentText()
        try:
            rows = self.api.get_activity(emp, event_type=event_type, severity=severity)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
            return
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            self.table.setCellWidget(i, 0, self._plain(row["occurred_at"].replace("T", "  ")[:19]))
            self.table.setCellWidget(i, 1, _pill_label(row["event_type"], EVENT_TYPE_COLORS.get(row["event_type"], COLORS["text_dim"])))
            self.table.setCellWidget(i, 2, self._plain(row["employee_id"]))
            self.table.setCellWidget(i, 3, _pill_label(row["severity"], SEVERITY_COLORS.get(row["severity"], COLORS["text_dim"])))
            self.table.setCellWidget(i, 4, self._plain(row["summary"]))
            self.table.setCellWidget(i, 5, self._plain(str(row["id"])))

    @staticmethod
    def _plain(text):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {COLORS['text']}; padding: 2px 6px;")
        lbl.setWordWrap(False)
        return lbl


class StatCard(QFrame):
    def __init__(self, label_text, accent_color):
        super().__init__()
        self.setStyleSheet(
            f"QFrame {{ background-color: {COLORS['surface']}; border: 1px solid {COLORS['border']}; "
            f"border-left: 4px solid {accent_color}; border-radius: 8px; }}"
        )
        self.setFixedHeight(90)
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 12, 16, 12)
        label = QLabel(label_text.upper())
        label.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 11px; font-weight: 600; letter-spacing: 1px;")
        layout.addWidget(label)
        self.value_label = QLabel("—")
        self.value_label.setStyleSheet(f"color: {COLORS['text']}; font-size: 28px; font-weight: 700;")
        layout.addWidget(self.value_label)
        self.setLayout(layout)

    def set_value(self, value):
        self.value_label.setText(str(value))


class NetworkActivityChart(QWidget):
    """
    Simple hand-drawn bar chart, pure QPainter -- no charting library
    dependency needed. NOTE: like the reference UI's own "(Simulated
    Mbps)" label, this is simulated data, not a real bandwidth reading
    -- this project has no network-throughput collector. Labeled
    honestly rather than implying it's real telemetry.
    """
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(160)
        self.values = [random.randint(20, 100) for _ in range(8)]
        self.labels = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "Now"]

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        bar_area_h = h - 20
        n = len(self.values)
        gap = 10
        bar_w = (w - gap * (n + 1)) / n
        max_val = max(self.values) or 1
        for i, v in enumerate(self.values):
            bar_h = (v / max_val) * (bar_area_h - 10)
            x = gap + i * (bar_w + gap)
            y = bar_area_h - bar_h
            painter.setBrush(QColor(COLORS["primary"]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(int(x), int(y), int(bar_w), int(bar_h), 4, 4)
            painter.setPen(QColor(COLORS["text_dim"]))
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(int(x), h - 4, int(bar_w), 14, Qt.AlignmentFlag.AlignCenter, self.labels[i])
        painter.end()


class OverviewDashboardTab(QWidget):
    """Landing tab -- stat cards, recent location/IP audits, a network
    activity chart. Backed by /overview/summary."""
    def __init__(self, api):
        super().__init__()
        self.api = api
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        title = QLabel("Overview Dashboard")
        title.setProperty("role", "title")
        layout.addWidget(title)
        subtitle = QLabel("Real-time status overview of monitored employee workstations.")
        subtitle.setProperty("role", "subtitle")
        layout.addWidget(subtitle)

        cards_row = QHBoxLayout()
        self.total_card = StatCard("Total Employees", COLORS["primary"])
        self.sessions_card = StatCard("Active Sessions", COLORS["warning"])
        self.online_card = StatCard("Online (Active)", COLORS["success"])
        self.alerts_card = StatCard("Critical Alerts (24H)", COLORS["danger"])
        for c in (self.total_card, self.sessions_card, self.online_card, self.alerts_card):
            cards_row.addWidget(c)
        layout.addLayout(cards_row)

        mid_row = QHBoxLayout()

        audits_box = QVBoxLayout()
        audits_label = QLabel("Recent Location & IP Audits")
        audits_label.setStyleSheet(f"color: {COLORS['text']}; font-weight: 600;")
        audits_box.addWidget(audits_label)
        self.audit_table = QTableWidget(0, 5)
        self.audit_table.setHorizontalHeaderLabels(["Employee", "IP Address", "IP Type", "Location", "Status"])
        self.audit_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        audits_box.addWidget(self.audit_table)
        mid_row.addLayout(audits_box, stretch=3)

        chart_box = QVBoxLayout()
        chart_label = QLabel("Network Activity Rate (Simulated Mbps)")
        chart_label.setStyleSheet(f"color: {COLORS['text']}; font-weight: 600;")
        chart_box.addWidget(chart_label)
        self.chart = NetworkActivityChart()
        chart_box.addWidget(self.chart)
        mid_row.addLayout(chart_box, stretch=2)

        layout.addLayout(mid_row)
        self.setLayout(layout)

    def load(self):
        try:
            summary = self.api.get_overview_summary()
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
            return
        self.total_card.set_value(summary["total_employees"])
        self.sessions_card.set_value(summary["active_sessions"])
        self.online_card.set_value(summary["online_active"])
        self.alerts_card.set_value(summary["critical_alerts_24h"])

        audits = summary["recent_ip_audits"]
        self.audit_table.setRowCount(len(audits))
        for i, a in enumerate(audits):
            self.audit_table.setCellWidget(i, 0, self._plain(a["display_name"]))
            self.audit_table.setCellWidget(i, 1, self._plain(a["ip_address"] or "—"))
            self.audit_table.setCellWidget(i, 2, self._plain(a["ip_type"]))
            loc_color = COLORS["success"] if a["location_status"] == "OFFICE" else COLORS["primary"]
            self.audit_table.setCellWidget(i, 3, _pill_label(a["location_status"], loc_color))
            status_color = COLORS["success"] if a["online_status"] == "ONLINE" else COLORS["text_dim"]
            self.audit_table.setCellWidget(i, 4, _pill_label(a["online_status"], status_color))

    @staticmethod
    def _plain(text):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {COLORS['text']}; padding: 2px 6px;")
        return lbl


class ITConsoleMainWindow(QMainWindow):
    def __init__(self, api):
        super().__init__()
        self.api = api
        self.setWindowTitle(f"IT Console - {api.display_name} ({api.role})")
        self.resize(1080, 680)

        central = QWidget()
        outer = QVBoxLayout()
        outer.setContentsMargins(20, 16, 20, 16)

        self.tabs = tabs = QTabWidget()
        self.overview_tab = OverviewDashboardTab(api)
        tabs.addTab(self.overview_tab, "Overview")
        self.status_tab = StatusGridTab(api)
        tabs.addTab(self.status_tab, "Live Status")
        tabs.addTab(EmployeesTab(api), "Employees")
        tabs.addTab(GalleryTab(api), "Screenshot Gallery")

        self.remote_tab = RemoteControlTab(api)
        self.live_view_tab = LiveViewTab(api, self._jump_to_remote_control)
        tabs.addTab(self.live_view_tab, "Live View")
        tabs.addTab(self.remote_tab, "Remote Control")
        self.activity_tab = ActivityMonitorTab(api)
        tabs.addTab(self.activity_tab, "Activity Monitor")
        tabs.addTab(AlertRulesTab(api), "Alert Rules")

        if api.role == "SuperAdmin":
            tabs.addTab(UserManagementTab(api), "Users & Roles")
            tabs.addTab(AuditLogTab(api), "Audit Log")

        outer.addWidget(tabs)
        central.setLayout(outer)
        self.setCentralWidget(central)

        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.status_tab.refresh)
        self.refresh_timer.timeout.connect(self.overview_tab.load)
        self.refresh_timer.start(5000)
        self.status_tab.refresh()
        self.overview_tab.load()

        self.live_view_tab.rebuild()
        self.roster_timer = QTimer()
        self.roster_timer.timeout.connect(self.live_view_tab.rebuild)
        self.roster_timer.start(60000)  # pick up newly-installed agents without a restart

        self.activity_tab.load()

    def _jump_to_remote_control(self, employee_id: str):
        self.tabs.setCurrentWidget(self.remote_tab)
        self.remote_tab.open_for_employee(employee_id)


def run_it_console(api):
    app = QApplication.instance() or QApplication(sys.argv)
    window = ITConsoleMainWindow(api)
    window.show()
    return app, window
