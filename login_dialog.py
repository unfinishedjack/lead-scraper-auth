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

`session` is a dict:
    {
        "token": str,
        "email": str,
        "role": "admin" | "user",
        "tokens_balance": float,             # PAID tokens only
        "free_tokens_remaining_today": float, # resets daily, never carries over
        "available_tokens": float,            # paid + free remaining today — show THIS as "balance"
    }

Signup is now a two-step, OTP-gated flow: the user enters email/password,
gets emailed a 6-digit code, then enters that code to actually create the
account. Nothing is created server-side until the code is verified.

Set AUTH_API_URL below to your deployed Render URL once you have it
(looks like https://lead-scraper-auth.onrender.com).
"""

import base64
import json
import mimetypes
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
QLineEdit:disabled { color: #6b7180; }

QPushButton {
    background-color: #3d6bff; color: white; border: none; border-radius: 6px;
    padding: 9px 18px; font-weight: 600;
}
QPushButton:hover { background-color: #5680ff; }
QPushButton:pressed { background-color: #2f57d6; }
QPushButton:disabled { background-color: #2b3040; color: #6b7180; }

QPushButton#linkButton {
    background-color: transparent; color: #4c7cff; border: none;
    padding: 2px 0; font-weight: 600; text-decoration: underline;
}
QPushButton#linkButton:hover { color: #5680ff; background-color: transparent; }
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


def _session_from_token_response(data: dict) -> dict:
    """Normalizes a /login, /signup/verify-otp, or /me response into the
    session dict shape the rest of the app expects."""
    return {
        "token": data.get("access_token"),
        "email": data["email"],
        "role": data.get("role", "user"),
        "tokens_balance": data.get("tokens_balance", 0.0),
        "free_tokens_remaining_today": data.get("free_tokens_remaining_today", 0.0),
        "available_tokens": data.get(
            "available_tokens",
            data.get("tokens_balance", 0.0) + data.get("free_tokens_remaining_today", 0.0),
        ),
    }


def fetch_me(access_token: str):
    """Hits /me with the given token. Returns a session dict on success, or
    None if the token is invalid/expired or the backend is unreachable."""
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
    data = resp.json()
    data["access_token"] = access_token
    return _session_from_token_response(data)


def fetch_settings(access_token: str | None = None):
    """Hits the public /settings endpoint for leads_per_token, the QR code
    reference, payment instructions, and the free daily token allowance.
    Returns None on any failure so callers can fall back to sane defaults
    rather than crash."""
    try:
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        resp = requests.get(f"{AUTH_API_URL}/settings", headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None


def refresh_balance(access_token: str):
    """Lightweight poll for the current balance breakdown. Returns a dict
    {"tokens_balance", "free_tokens_remaining_today", "available_tokens"}
    or None if it couldn't be fetched."""
    try:
        resp = requests.get(
            f"{AUTH_API_URL}/balance",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None


def consume_tokens(access_token: str, tokens: float):
    """Reports a completed scrape's token cost to the backend (spent from
    today's free allowance first, then paid balance). Returns the balance
    breakdown dict on success, or None on failure — callers should treat a
    failure as "couldn't sync, balance shown may be stale" rather than
    blocking the user, since the run already happened."""
    try:
        resp = requests.post(
            f"{AUTH_API_URL}/consume-tokens",
            json={"tokens": tokens},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None


# ---------------------------------------------------------------------------
# Payment proof submissions (user side)
# ---------------------------------------------------------------------------

def submit_payment_proof(access_token: str, image_path: str, tokens_requested: float,
                          note: str | None = None):
    """Reads an image file from disk, base64-encodes it, and submits it as
    proof of payment for admin review. Returns the created submission dict
    on success, or (None, error_message) on failure."""
    try:
        with open(image_path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return None, f"Couldn't read file: {e}"

    mime, _ = mimetypes.guess_type(image_path)
    mime = mime or "image/png"
    image_data = base64.b64encode(raw).decode("ascii")

    try:
        resp = requests.post(
            f"{AUTH_API_URL}/payment-submissions",
            json={
                "image_data": image_data,
                "image_mime": mime,
                "tokens_requested": tokens_requested,
                "note": note,
            },
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
    except requests.RequestException as e:
        return None, f"Couldn't reach the server: {e}"

    if resp.status_code == 201:
        return resp.json(), None
    try:
        detail = resp.json().get("detail", "Submission failed")
    except Exception:
        detail = f"Submission failed ({resp.status_code})"
    return None, detail


def get_my_payment_submissions(access_token: str):
    """Returns the current user's own submission history (list of dicts),
    or [] on failure."""
    try:
        resp = requests.get(
            f"{AUTH_API_URL}/my/payment-submissions",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return []


# ---------------------------------------------------------------------------
# Payment proof submissions (admin side)
# ---------------------------------------------------------------------------

def admin_get_payment_submissions(access_token: str, status_filter: str = "pending"):
    """status_filter: 'pending' | 'approved' | 'declined' | 'all'."""
    try:
        resp = requests.get(
            f"{AUTH_API_URL}/admin/payment-submissions",
            params={"status": status_filter},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return []


def admin_get_payment_submission_image(access_token: str, submission_id: int):
    """Returns (image_bytes, mime) or (None, None) on failure. Fetched
    separately from the list endpoint since images are heavy."""
    try:
        resp = requests.get(
            f"{AUTH_API_URL}/admin/payment-submissions/{submission_id}/image",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return base64.b64decode(data["image_data"]), data.get("image_mime", "image/png")
    except requests.RequestException:
        pass
    return None, None


def admin_review_payment_submission(access_token: str, submission_id: int, approve: bool,
                                     tokens: float | None = None, admin_note: str | None = None):
    """approve=True credits `tokens` (or the user's requested amount if
    tokens is None) to the user's paid balance; approve=False just marks it
    declined. Returns (submission_dict, None) on success or (None, error)."""
    action = "approve" if approve else "decline"
    payload = {"admin_note": admin_note}
    if approve:
        payload["tokens"] = tokens
    try:
        resp = requests.post(
            f"{AUTH_API_URL}/admin/payment-submissions/{submission_id}/{action}",
            json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
    except requests.RequestException as e:
        return None, f"Couldn't reach the server: {e}"
    if resp.status_code == 200:
        return resp.json(), None
    try:
        detail = resp.json().get("detail", "Review failed")
    except Exception:
        detail = f"Review failed ({resp.status_code})"
    return None, detail


class LoginDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sign in — Maps Lead Scraper")
        self.setFixedWidth(380)
        self.setStyleSheet(LOGIN_STYLE_SHEET)
        self.session = None  # filled in on success: session dict (see module docstring)

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

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self.tabs.addTab(self._build_login_tab(), "Log in")
        self.tabs.addTab(self._build_signup_tab(), "Create account")

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #e5484d; font-size: 11px;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    # -- Log in tab -----------------------------------------------------------

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
        self.login_password.returnPressed.connect(self._handle_login)

        form.addRow("Email", self.login_email)
        form.addRow("Password", self.login_password)

        btn = QPushButton("Log in")
        btn.clicked.connect(self._handle_login)
        form.addRow(btn)
        return w

    # -- Create account tab (two-step: request code, then verify) -------------

    def _build_signup_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 14, 4, 4)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(10)
        layout.addLayout(form)

        self.signup_email = QLineEdit()
        self.signup_email.setPlaceholderText("you@example.com")
        self.signup_password = QLineEdit()
        self.signup_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.signup_password.setPlaceholderText("At least 8 characters")

        form.addRow("Email", self.signup_email)
        form.addRow("Password", self.signup_password)

        # OTP row — hidden until a code has been requested.
        self.signup_otp_label = QLabel("Verification code")
        self.signup_otp = QLineEdit()
        self.signup_otp.setPlaceholderText("6-digit code from your email")
        self.signup_otp.setMaxLength(6)
        self.signup_otp.returnPressed.connect(self._handle_signup_click)
        form.addRow(self.signup_otp_label, self.signup_otp)
        self.signup_otp_label.setVisible(False)
        self.signup_otp.setVisible(False)

        self.signup_hint = QLabel(
            "We'll email you a 6-digit code to verify this address before "
            "your account is created."
        )
        self.signup_hint.setWordWrap(True)
        self.signup_hint.setStyleSheet("color: #7a8194; font-size: 11px;")
        layout.addWidget(self.signup_hint)

        btn_row = QHBoxLayout()
        self.signup_btn = QPushButton("Send Verification Code")
        self.signup_btn.clicked.connect(self._handle_signup_click)
        btn_row.addWidget(self.signup_btn)
        layout.addLayout(btn_row)

        self.resend_btn = QPushButton("Resend code")
        self.resend_btn.setObjectName("linkButton")
        self.resend_btn.setVisible(False)
        self.resend_btn.clicked.connect(self._handle_resend_code)
        layout.addWidget(self.resend_btn)

        self._otp_stage = False  # False = collecting email/password, True = collecting code
        return w

    def _set_otp_stage(self, active: bool):
        """Switches the signup tab between "enter details" and "enter code"."""
        self._otp_stage = active
        self.signup_otp_label.setVisible(active)
        self.signup_otp.setVisible(active)
        self.resend_btn.setVisible(active)
        self.signup_email.setEnabled(not active)
        self.signup_password.setEnabled(not active)
        self.signup_btn.setText("Verify && Create Account" if active else "Send Verification Code")
        if active:
            self.signup_otp.setFocus()
            self.signup_hint.setText(
                f"Enter the code sent to {self.signup_email.text().strip()}. "
                f"It expires in 10 minutes."
            )
        else:
            self.signup_otp.clear()
            self.signup_hint.setText(
                "We'll email you a 6-digit code to verify this address before "
                "your account is created."
            )

    def _handle_signup_click(self):
        if self._otp_stage:
            self._handle_signup_verify()
        else:
            self._handle_signup_request()

    def _handle_signup_request(self):
        email = self.signup_email.text().strip()
        password = self.signup_password.text()
        if not email or not password:
            self.status_label.setText("Enter an email and password.")
            return
        if len(password) < 8:
            self.status_label.setText("Password must be at least 8 characters.")
            return

        self.status_label.setText("")
        self.signup_btn.setEnabled(False)
        self.signup_btn.setText("Sending…")
        try:
            resp = requests.post(
                f"{AUTH_API_URL}/signup/request-otp",
                json={"email": email, "password": password},
                timeout=30,  # generous: Render free tier cold start can take ~60s
            )
        except requests.RequestException:
            self.status_label.setText(
                "Couldn't reach the server. If it's been idle it can take up "
                "to a minute to wake up — try again shortly."
            )
            self.signup_btn.setEnabled(True)
            self.signup_btn.setText("Send Verification Code")
            return

        self.signup_btn.setEnabled(True)
        if resp.status_code == 200:
            self._set_otp_stage(True)
        else:
            try:
                detail = resp.json().get("detail", "Couldn't send verification code")
            except Exception:
                detail = f"Couldn't send verification code ({resp.status_code})"
            self.status_label.setText(detail)
            self.signup_btn.setText("Send Verification Code")

    def _handle_resend_code(self):
        self.resend_btn.setEnabled(False)
        self.resend_btn.setText("Resending…")
        self._handle_signup_request()
        self.resend_btn.setEnabled(True)
        self.resend_btn.setText("Resend code")
        if self._otp_stage:
            self.status_label.setText("")
            self.status_label.setStyleSheet("color: #4c7cff; font-size: 11px;")
            self.status_label.setText("A new code has been sent.")
            self.status_label.setStyleSheet("color: #e5484d; font-size: 11px;")

    def _handle_signup_verify(self):
        email = self.signup_email.text().strip()
        code = self.signup_otp.text().strip()
        if not code:
            self.status_label.setText("Enter the code from your email.")
            return

        self.status_label.setText("")
        self.signup_btn.setEnabled(False)
        self.signup_btn.setText("Verifying…")
        try:
            resp = requests.post(
                f"{AUTH_API_URL}/signup/verify-otp",
                json={"email": email, "code": code},
                timeout=30,
            )
        except requests.RequestException:
            self.status_label.setText("Couldn't reach the server — try again shortly.")
            self.signup_btn.setEnabled(True)
            self.signup_btn.setText("Verify && Create Account")
            return

        self.signup_btn.setEnabled(True)
        self.signup_btn.setText("Verify && Create Account")

        if resp.status_code == 201:
            data = resp.json()
            _save_token(data["access_token"], data["email"])
            self.session = _session_from_token_response(data)
            self.accept()
        else:
            try:
                detail = resp.json().get("detail", "Verification failed")
            except Exception:
                detail = f"Verification failed ({resp.status_code})"
            self.status_label.setText(detail)

    # -- Log in handling --------------------------------------------------------

    def _handle_login(self):
        email = self.login_email.text().strip()
        password = self.login_password.text()
        if not email or not password:
            self.status_label.setText("Enter your email and password.")
            return

        self.status_label.setText("Connecting…")
        try:
            resp = requests.post(
                f"{AUTH_API_URL}/login",
                json={"email": email, "password": password},
                timeout=30,  # generous: Render free tier cold start can take ~60s
            )
        except requests.RequestException:
            self.status_label.setText(
                "Couldn't reach the login server. If it's been idle it can take "
                "up to a minute to wake up — try again shortly."
            )
            return

        if resp.status_code == 200:
            data = resp.json()
            _save_token(data["access_token"], data["email"])
            self.session = _session_from_token_response(data)
            self.accept()
            return

        try:
            detail = resp.json().get("detail", "Login failed")
        except Exception:
            detail = f"Login failed ({resp.status_code})"
        self.status_label.setText(detail)


def require_login(app):
    """Call this before creating MainWindow. Returns a session dict (see
    module docstring) if the user is authenticated (either via a valid
    saved token or by logging in through the dialog just now), or None if
    they closed the dialog without success."""
    saved = _load_token()
    if saved:
        session = fetch_me(saved["access_token"])
        if session:
            return session
        _clear_token()  # saved token was missing/invalid/expired

    dialog = LoginDialog()
    dialog.exec()
    return dialog.session