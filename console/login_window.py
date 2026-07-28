import sys
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton,
    QMessageBox, QFrame, QHBoxLayout
)
from PyQt6.QtCore import Qt

from api_client import ApiClient


class LoginWindow(QWidget):
    def __init__(self, on_success):
        super().__init__()
        self.on_success = on_success
        self.api = ApiClient()

        self.setWindowTitle("Monitoring Console")
        self.setFixedSize(420, 380)

        outer = QVBoxLayout()
        outer.addStretch()

        card = QFrame()
        card.setProperty("role", "card")
        card.setFixedWidth(340)
        card_layout = QVBoxLayout()
        card_layout.setContentsMargins(28, 28, 28, 28)
        card_layout.setSpacing(14)

        title = QLabel("Monitoring Console")
        title.setProperty("role", "title")
        card_layout.addWidget(title)

        subtitle = QLabel("Sign in with your IT or manager account")
        subtitle.setProperty("role", "subtitle")
        card_layout.addWidget(subtitle)
        card_layout.addSpacing(6)

        card_layout.addWidget(QLabel("Username"))
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("e.g. manager1")
        card_layout.addWidget(self.username_input)

        card_layout.addWidget(QLabel("Password"))
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.returnPressed.connect(self.attempt_login)
        card_layout.addWidget(self.password_input)

        card_layout.addSpacing(8)
        self.login_btn = QPushButton("Log In")
        self.login_btn.clicked.connect(self.attempt_login)
        card_layout.addWidget(self.login_btn)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #ef4444;")
        self.error_label.setWordWrap(True)
        card_layout.addWidget(self.error_label)

        card.setLayout(card_layout)

        center_row = QHBoxLayout()
        center_row.addStretch()
        center_row.addWidget(card)
        center_row.addStretch()
        outer.addLayout(center_row)
        outer.addStretch()

        self.setLayout(outer)
        self.username_input.setFocus()

    def attempt_login(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()
        self.error_label.setText("")
        if not username or not password:
            self.error_label.setText("Enter both username and password.")
            return
        self.login_btn.setEnabled(False)
        self.login_btn.setText("Signing in...")
        try:
            self.api.login(username, password)
        except Exception as e:
            self.error_label.setText(f"Could not log in: {e}")
            self.login_btn.setEnabled(True)
            self.login_btn.setText("Log In")
            return
        self.on_success(self.api)
        self.close()


def run_login(on_success):
    app = QApplication.instance() or QApplication(sys.argv)
    window = LoginWindow(on_success)
    window.show()
    return app, window
