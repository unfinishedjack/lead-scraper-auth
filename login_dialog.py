"""
login_dialog.py
================
Drop this file next to your existing scraper script. It adds a login/
signup gate in front of your PyQt6 MainWindow, backed by the FastAPI
auth service in backend/main.py (deployed on Render).

Usage — replace the bottom of your existing main() with:

    from login_dialog import require_login

    def main():
        app = QApplication(sys.argv)
        app.setStyle("Fusion")

        if not require_login(app):
            sys.exit(0)   # user closed the login dialog without logging in

        window = MainWindow()
        window.show()
        sys.exit(app.exec())

Set AUTH_API_URL below to your deployed Render URL once you have it
(looks like https://lead-scraper-auth.onrender.com).
"""

import json
import os

import requests
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

# Point this at your deployed Render service. Keep the localhost fallback
# for testing the backend on your own machine before you deploy it.
AUTH_API_URL = os.environ.get("AUTH_API_URL", "http://127.0.0.1:8000")

TOKEN_PATH = os.path.expanduser("~/.lead_scraper_auth.json")


def _save_token(token: str, email: str):
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump({"access_token": token, "email": email}, f)


def _load_token():
    if not os.path.exists(TOKEN_PATH):
        return None
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _clear_token():
    if os.path.exists(TOKEN_PATH):
        os.remove(TOKEN_PATH)


def _verify_saved_token() -> bool:
    """Check whether a previously saved token is still valid, so returning
    users skip straight past the login screen."""
    saved = _load_token()
    if not saved:
        return False
    try:
        resp = requests.get(
            f"{AUTH_API_URL}/me",
            headers={"Authorization": f"Bearer {saved['access_token']}"},
            timeout=10,
        )
        return resp.status_code == 200
    except requests.RequestException:
        # Backend unreachable (e.g. Render free instance still waking up).
        # Fail closed — don't let a network hiccup silently bypass login.
        return False


class LoginDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sign in — Maps Lead Scraper")
        self.setFixedWidth(360)
        self.authenticated = False

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        tabs.addTab(self._build_login_tab(), "Log in")
        tabs.addTab(self._build_signup_tab(), "Create account")

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #e5484d;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    def _build_login_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        self.login_email = QLineEdit()
        self.login_password = QLineEdit()
        self.login_password.setEchoMode(QLineEdit.EchoMode.Password)

        form.addRow("Email", self.login_email)
        form.addRow("Password", self.login_password)

        btn = QPushButton("Log in")
        btn.clicked.connect(self._handle_login)
        form.addRow(btn)
        return w

    def _build_signup_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        self.signup_email = QLineEdit()
        self.signup_password = QLineEdit()
        self.signup_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.signup_password.setPlaceholderText("At least 8 characters")

        form.addRow("Email", self.signup_email)
        form.addRow("Password", self.signup_password)

        btn = QPushButton("Create account")
        btn.clicked.connect(self._handle_signup)
        form.addRow(btn)
        return w

    def _call_auth(self, endpoint: str, email: str, password: str):
        self.status_label.setText("Connecting…")
        try:
            resp = requests.post(
                f"{AUTH_API_URL}/{endpoint}",
                json={"email": email, "password": password},
                timeout=30,  # generous: Render free tier cold start can take ~60s
            )
        except requests.RequestException as e:
            self.status_label.setText(
                "Couldn't reach the login server. If it's been idle it can take "
                "up to a minute to wake up — try again shortly."
            )
            return None

        if resp.status_code in (200, 201):
            return resp.json()

        try:
            detail = resp.json().get("detail", "Login failed")
        except Exception:
            detail = f"Login failed ({resp.status_code})"
        self.status_label.setText(detail)
        return None

    def _handle_login(self):
        email = self.login_email.text().strip()
        password = self.login_password.text()
        if not email or not password:
            self.status_label.setText("Enter your email and password.")
            return
        data = self._call_auth("login", email, password)
        if data:
            _save_token(data["access_token"], data["email"])
            self.authenticated = True
            self.accept()

    def _handle_signup(self):
        email = self.signup_email.text().strip()
        password = self.signup_password.text()
        if not email or not password:
            self.status_label.setText("Enter an email and password.")
            return
        data = self._call_auth("signup", email, password)
        if data:
            _save_token(data["access_token"], data["email"])
            self.authenticated = True
            self.accept()


def require_login(app) -> bool:
    """Call this before creating MainWindow. Returns True if the user is
    authenticated (either via a valid saved token or by logging in through
    the dialog just now), False if they closed the dialog without success."""
    if _verify_saved_token():
        return True

    _clear_token()  # saved token was missing/invalid/expired
    dialog = LoginDialog()
    dialog.exec()
    return dialog.authenticated