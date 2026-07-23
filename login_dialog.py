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

        session = require_login(app)
        if session is None:
            sys.exit(0)   # user closed the login dialog without logging in

        window = MainWindow(session)
        window.show()
        sys.exit(app.exec())

`session` is a dict: {"token", "email", "role", "tokens_balance"}.
`role` is "admin" or "user" — MainWindow uses it to decide whether the
headless checkbox is editable and whether to show the admin panel vs.
the buy-tokens panel.

Set AUTH_API_URL below to your deployed Render URL once you have it
(looks like https://lead-scraper-auth.onrender.com).
"""

import json
import os

import requests
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
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
AUTH_API_URL = os.environ.get("AUTH_API_URL", "https://lead-scraper-auth.onrender.com")
TOKEN_PATH = os.path.expanduser("~/.lead_scraper_auth.json")


# ---------------------------------------------------------------------------
# Stylesheet — same dark theme as the main app (main.py STYLE_SHEET), just
# scoped down to the widgets a QDialog actually uses. Keeping these in sync
# by hand is fine since the dialog is small; if the main palette ever
# changes, mirror the color values below.
# ---------------------------------------------------------------------------

LOGIN_STYLE_SHEET = """
QDialog { background-color: #14161c; }
QWidget { color: #e6e6e6; font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 13px; }

QLabel#loginTitle { font-size: 16px; font-weight: 700; color: #ffffff; }
QLabel#loginSubtitle { font-size: 11px; color: #7a8194; }

QTabWidget::pane { border: 1px solid #2a2f3a; border-radius: 8px; top: -1px; background-color: #181b22; }
QTabBar::tab {
    background-color: #1b1e26; color: #8b93a7; padding: 8px 18px;
    border: 1px solid #2a2f3a; border-bottom: none;
    border-top-left-radius: 8px; border-top-right-radius: 8px; margin-right: 2px;
    font-weight: 600;
}
QTabBar::tab:selected { background-color: #181b22; color: #ffffff; border-bottom: 2px solid #4c7cff; }
QTabBar::tab:hover:!selected { color: #c4c9d4; }

QLabel { color: #b8bfcf; }

QLineEdit {
    background-color: #20242e; border: 1px solid #2f3542; border-radius: 6px;
    padding: 6px 8px; selection-background-color: #3d6bff;
}
QLineEdit:focus { border: 1px solid #4c7cff; }

QPushButton {
    background-color: #3d6bff; color: white; border: none; border-radius: 6px;
    padding: 9px 18px; font-weight: 600;
}
QPushButton:hover { background-color: #5680ff; }
QPushButton:pressed { background-color: #2f57d6; }
QPushButton:disabled { background-color: #2b3040; color: #6b7180; }
"""


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

def logout():
    """Clears the locally saved session so the next launch (or a live
    logout) requires signing in again."""
    _clear_token()

def fetch_me(access_token: str):
    """Hits /me with the given token. Returns a dict with email/role/
    tokens_balance on success, or None if the token is invalid/expired
    or the backend is unreachable."""
    try:
        resp = requests.get(
            f"{AUTH_API_URL}/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except requests.RequestException:
        # Backend unreachable (e.g. Render free instance still waking up).
        # Fail closed — don't let a network hiccup silently bypass login.
        return None
    if resp.status_code != 200:
        return None
    return resp.json()


def fetch_settings(access_token: str | None = None):
    """Hits the public /settings endpoint for leads_per_token, the QR code
    reference, and payment instructions. Returns None on any failure so
    callers can fall back to sane defaults rather than crash."""
    try:
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        resp = requests.get(f"{AUTH_API_URL}/settings", headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None


def refresh_balance(access_token: str):
    """Lightweight poll for the current token balance. Returns a float or
    None if it couldn't be fetched."""
    try:
        resp = requests.get(
            f"{AUTH_API_URL}/balance",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("tokens_balance")
    except requests.RequestException:
        pass
    return None


def consume_tokens(access_token: str, tokens: float):
    """Reports a completed scrape's token cost to the backend. Returns the
    new balance (float) on success, or None on failure — callers should
    treat a failure as "couldn't sync, balance shown may be stale" rather
    than blocking the user, since the run already happened."""
    try:
        resp = requests.post(
            f"{AUTH_API_URL}/consume-tokens",
            json={"tokens": tokens},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("tokens_balance")
    except requests.RequestException:
        pass
    return None


class LoginDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sign in — Maps Lead Scraper")
        self.setFixedWidth(380)
        self.setStyleSheet(LOGIN_STYLE_SHEET)
        self.session = None  # filled in on success: {token, email, role, tokens_balance}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        # Header — mirrors the toolbar title/subtitle treatment from the
        # main window so the two screens read as the same product.
        header = QVBoxLayout()
        header.setSpacing(0)
        title = QLabel("🗺  Maps Lead Scraper")
        title.setObjectName("loginTitle")
        subtitle = QLabel("Sign in to continue")
        subtitle.setObjectName("loginSubtitle")
        header.addWidget(title)
        header.addWidget(subtitle)
        layout.addLayout(header)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        tabs.addTab(self._build_login_tab(), "Log in")
        tabs.addTab(self._build_signup_tab(), "Create account")

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #e5484d; font-size: 11px;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    def _build_login_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(10)
        form.setContentsMargins(4, 14, 4, 4)

        self.login_email = QLineEdit()
        self.login_email.setPlaceholderText("you@example.com")
        self.login_password = QLineEdit()
        self.login_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_password.setPlaceholderText("Password")

        form.addRow("Email", self.login_email)
        form.addRow("Password", self.login_password)

        btn = QPushButton("Log in")
        btn.clicked.connect(self._handle_login)
        form.addRow(btn)
        return w

    def _build_signup_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(10)
        form.setContentsMargins(4, 14, 4, 4)

        self.signup_email = QLineEdit()
        self.signup_email.setPlaceholderText("you@example.com")
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
        except requests.RequestException:
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
            self.session = {
                "token": data["access_token"],
                "email": data["email"],
                "role": data.get("role", "user"),
                "tokens_balance": data.get("tokens_balance", 0.0),
            }
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
            self.session = {
                "token": data["access_token"],
                "email": data["email"],
                "role": data.get("role", "user"),
                "tokens_balance": data.get("tokens_balance", 0.0),
            }
            self.accept()


def require_login(app):
    """Call this before creating MainWindow. Returns a session dict
    {"token", "email", "role", "tokens_balance"} if the user is
    authenticated (either via a valid saved token or by logging in through
    the dialog just now), or None if they closed the dialog without
    success."""
    saved = _load_token()
    if saved:
        me = fetch_me(saved["access_token"])
        if me:
            return {
                "token": saved["access_token"],
                "email": me["email"],
                "role": me.get("role", "user"),
                "tokens_balance": me.get("tokens_balance", 0.0),
            }
        _clear_token()  # saved token was missing/invalid/expired

    dialog = LoginDialog()
    dialog.exec()
    return dialog.session