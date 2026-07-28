"""
One shared stylesheet applied to the whole QApplication so both the
login screen, IT console, and Manager console look like one coherent
product instead of default battleship-gray Qt widgets.

Pure Qt/QSS -- no web tech, no JS, per the original "Python and/or C++
only" constraint. QSS is Qt's own CSS-like styling language.
"""

COLORS = {
    "bg": "#0f172a",            # slate-900, app background
    "surface": "#1e293b",       # slate-800, panels/cards
    "surface_alt": "#273449",   # slightly lighter panel
    "border": "#334155",        # slate-600
    "text": "#e2e8f0",          # slate-200
    "text_dim": "#94a3b8",      # slate-400
    "primary": "#3b82f6",       # blue-500
    "primary_hover": "#2563eb", # blue-600
    "success": "#22c55e",       # green-500 (active)
    "warning": "#f59e0b",       # amber-500 (idle)
    "danger": "#ef4444",        # red-500 (offline / destructive)
    "danger_hover": "#dc2626",
}

QSS = f"""
* {{
    font-family: "Segoe UI", "Ubuntu", "Helvetica Neue", sans-serif;
    font-size: 13px;
    color: {COLORS['text']};
}}

QMainWindow, QWidget {{
    background-color: {COLORS['bg']};
}}

QLabel {{
    background: transparent;
}}

QLabel[role="title"] {{
    font-size: 18px;
    font-weight: 600;
    color: {COLORS['text']};
}}

QLabel[role="subtitle"] {{
    font-size: 12px;
    color: {COLORS['text_dim']};
}}

QLineEdit, QComboBox {{
    background-color: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    padding: 8px 10px;
    color: {COLORS['text']};
    selection-background-color: {COLORS['primary']};
}}

QLineEdit:focus, QComboBox:focus {{
    border: 1px solid {COLORS['primary']};
}}

QComboBox::drop-down {{
    border: none;
    width: 24px;
}}

QComboBox QAbstractItemView {{
    background-color: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    selection-background-color: {COLORS['primary']};
}}

QPushButton {{
    background-color: {COLORS['primary']};
    color: white;
    border: none;
    border-radius: 6px;
    padding: 9px 16px;
    font-weight: 600;
}}

QPushButton:hover {{
    background-color: {COLORS['primary_hover']};
}}

QPushButton:disabled {{
    background-color: {COLORS['border']};
    color: {COLORS['text_dim']};
}}

QPushButton[role="secondary"] {{
    background-color: {COLORS['surface_alt']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
}}

QPushButton[role="secondary"]:hover {{
    background-color: {COLORS['border']};
}}

QPushButton[role="danger"] {{
    background-color: {COLORS['danger']};
}}

QPushButton[role="danger"]:hover {{
    background-color: {COLORS['danger_hover']};
}}

QPushButton:checkable:checked {{
    background-color: {COLORS['danger']};
}}

QTabWidget::pane {{
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    background-color: {COLORS['surface']};
    top: -1px;
}}

QTabBar::tab {{
    background-color: transparent;
    color: {COLORS['text_dim']};
    padding: 10px 18px;
    margin-right: 2px;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}}

QTabBar::tab:selected {{
    color: {COLORS['text']};
    border-bottom: 2px solid {COLORS['primary']};
}}

QTabBar::tab:hover {{
    color: {COLORS['text']};
}}

QTableWidget, QListWidget {{
    background-color: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    gridline-color: {COLORS['border']};
    outline: none;
}}

QTableWidget::item, QListWidget::item {{
    padding: 6px;
    border: none;
}}

QTableWidget::item:selected, QListWidget::item:selected {{
    background-color: {COLORS['primary']};
    color: white;
}}

QHeaderView::section {{
    background-color: {COLORS['surface_alt']};
    color: {COLORS['text_dim']};
    padding: 8px;
    border: none;
    border-bottom: 1px solid {COLORS['border']};
    font-weight: 600;
}}

QScrollArea {{
    border: none;
    background: transparent;
}}

QScrollBar:vertical {{
    background: {COLORS['bg']};
    width: 10px;
}}

QScrollBar::handle:vertical {{
    background: {COLORS['border']};
    border-radius: 5px;
    min-height: 24px;
}}

QScrollBar::handle:vertical:hover {{
    background: {COLORS['text_dim']};
}}

QFrame[role="card"] {{
    background-color: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 10px;
}}

QFrame[role="card"]:hover {{
    border: 1px solid {COLORS['primary']};
}}

QMessageBox {{
    background-color: {COLORS['surface']};
}}
"""


def apply_theme(app):
    app.setStyleSheet(QSS)
