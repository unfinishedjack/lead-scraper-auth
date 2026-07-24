"""
Auth backend for the Maps Lead Scraper desktop app.
=====================================================
Minimal signup/login service the PyQt6 app calls before it lets the
user into the main window. Issues JWTs; the desktop app stores the
token locally and sends it back on future launches to skip re-login.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload

Deploy on Render:
    See render.yaml in this folder, or create a Web Service manually
    pointing at this repo with start command:
        uvicorn main:app --host 0.0.0.0 --port $PORT

Required environment variables (set these in Render's dashboard):
    JWT_SECRET          - any long random string (Render can auto-generate one)
    DATABASE_URL        - Postgres URL from a Render free Postgres instance.
                          Falls back to a local sqlite file if unset, which
                          is fine for quick local testing but NOT persistent
                          on Render's free web service (disk resets on deploy).
    ADMIN_EMAILS        - comma-separated list of emails that should be
                          promoted to "admin" role automatically on signup/
                          login (e.g. "you@example.com,partner@example.com").
    GMAIL_ADDRESS       - Gmail account used to SEND mail (OTP codes, payment
                          notifications to admins). e.g. "yourapp@gmail.com".
    GMAIL_APP_PASSWORD  - a Gmail *App Password* (NOT your normal Gmail
                          password). Generate one at
                          https://myaccount.google.com/apppasswords
                          (requires 2-Step Verification to be enabled).
    ADMIN_NOTIFY_EMAILS - comma-separated list of admin emails that receive
                          "new payment proof submitted" notifications with
                          one-click approve/decline links. Defaults to
                          ADMIN_EMAILS if unset.
    APP_BASE_URL        - the public base URL of this deployed backend
                          (e.g. "https://lead-scraper-auth.onrender.com"),
                          used to build the one-click email action links.
"""

import base64
import hashlib
import hmac
import os
import secrets
import smtplib
import ssl
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText

import bcrypt
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 30  # desktop app tokens live a while so users aren't
                      # forced to log in every single launch

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./users.db")
# Render's Postgres URLs sometimes start with postgres:// — SQLAlchemy
# 2.x wants postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ADMIN_EMAILS", "").split(",")
    if e.strip()
}

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

ADMIN_NOTIFY_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ADMIN_NOTIFY_EMAILS", "").split(",")
    if e.strip()
} or ADMIN_EMAILS

APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")

OTP_EXPIRE_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
EMAIL_ACTION_EXPIRE_HOURS = 72  # one-click approve/decline links stay valid this long

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

bearer_scheme = HTTPBearer()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def hash_otp(code: str, email: str) -> str:
    # OTPs are short-lived and low-entropy (6 digits), so a salted HMAC is
    # plenty — no need for bcrypt's deliberate slowness here.
    return hmac.new(JWT_SECRET.encode(), f"{email.lower()}:{code}".encode(), hashlib.sha256).hexdigest()


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


# --------------------------------------------------------------------------
# Email sending (Gmail SMTP)
# --------------------------------------------------------------------------

def send_email(to_addrs: list[str], subject: str, body_html: str) -> bool:
    """Best-effort send; returns False (and logs) instead of raising, so a
    flaky SMTP connection never 500s an otherwise-successful DB operation."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print(f"[email] GMAIL_ADDRESS/GMAIL_APP_PASSWORD not set — skipping send: {subject!r}")
        return False
    if not to_addrs:
        return False
    try:
        msg = MIMEText(body_html, "html")
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = ", ".join(to_addrs)
        context = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls(context=context)
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to_addrs, msg.as_string())
        return True
    except Exception as e:
        print(f"[email] send failed: {e!r}")
        return False


def send_otp_email(to_email: str, code: str) -> bool:
    return send_email(
        [to_email],
        subject="Your Maps Lead Scraper verification code",
        body_html=(
            f"<p>Your verification code is:</p>"
            f"<h2 style='letter-spacing:4px'>{code}</h2>"
            f"<p>This code expires in {OTP_EXPIRE_MINUTES} minutes. "
            f"If you didn't request this, you can ignore this email.</p>"
        ),
    )


# --------------------------------------------------------------------------
# DB model
# --------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False, default="user")  # "user" | "admin"
    tokens_balance = Column(Float, nullable=False, default=0.0)  # paid tokens — persist forever
    # Free daily allowance tracking — resets every day, never carries over.
    free_tokens_used_today = Column(Float, nullable=False, default=0.0)
    free_tokens_date = Column(DateTime, nullable=True)  # UTC date of last reset, stored at midnight
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AppSettings(Base):
    """Single-row table of admin-controlled knobs the desktop app reads
    on startup. Row id is always 1."""
    __tablename__ = "app_settings"
    id = Column(Integer, primary_key=True, default=1)
    leads_per_token = Column(Float, nullable=False, default=10.0)
    qr_code_url = Column(String, nullable=True)
    payment_instructions = Column(String, nullable=True)
    # 0 = free daily tokens disabled entirely.
    free_daily_tokens = Column(Float, nullable=False, default=0.0)


class PendingSignup(Base):
    """Holds a not-yet-verified signup until the OTP is confirmed. Nothing
    here becomes a real User until verify-otp succeeds."""
    __tablename__ = "pending_signups"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    otp_hash = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class PaymentSubmission(Base):
    """A user's uploaded proof-of-payment, pending admin review. Image is
    stored as base64 text directly in the DB — deliberately not on local
    disk, since Render's free web service disk is not persistent."""
    __tablename__ = "payment_submissions"
    id = Column(Integer, primary_key=True)
    user_email = Column(String, index=True, nullable=False)
    image_data = Column(Text, nullable=False)       # base64-encoded image bytes
    image_mime = Column(String, nullable=False, default="image/png")
    tokens_requested = Column(Float, nullable=False)  # what the user claims they paid for
    note = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending")  # pending | approved | declined
    admin_note = Column(String, nullable=True)
    tokens_granted = Column(Float, nullable=True)     # actual amount credited, if approved
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(String, nullable=True)


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_or_create_settings(db: Session) -> AppSettings:
    settings = db.query(AppSettings).filter(AppSettings.id == 1).first()
    if not settings:
        settings = AppSettings(
            id=1,
            leads_per_token=10.0,
            qr_code_url=None,
            payment_instructions=(
                "Scan the QR code, pay for the number of tokens you want, "
                "then upload your proof of payment in the Buy Tokens tab. "
                "Tokens are added after an admin reviews it."
            ),
            free_daily_tokens=0.0,
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


# --------------------------------------------------------------------------
# Free daily token helpers
# --------------------------------------------------------------------------

def _today_utc() -> datetime:
    d = date.today()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _reset_free_tokens_if_new_day(user: User):
    """Lazily resets the free daily allowance the first time the user is
    touched on a new UTC day. No cron job needed — this runs inline on
    every request that reads or spends balance."""
    today = _today_utc()
    last = user.free_tokens_date
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last is None or last < today:
        user.free_tokens_used_today = 0.0
        user.free_tokens_date = today


def free_tokens_remaining(user: User, settings: AppSettings) -> float:
    if user.role == "admin":
        return 0.0  # admins are unlimited via role, not the free-tier mechanism
    return max(0.0, settings.free_daily_tokens - user.free_tokens_used_today)


def available_tokens(user: User, settings: AppSettings) -> float:
    """Paid balance plus whatever's left of today's free allowance."""
    return user.tokens_balance + free_tokens_remaining(user, settings)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class RequestOtpRequest(BaseModel):
    email: EmailStr
    password: str


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    code: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str
    role: str
    tokens_balance: float          # paid tokens only
    free_tokens_remaining_today: float
    available_tokens: float        # paid + free remaining today — what to show as "your balance"


class MeResponse(BaseModel):
    email: str
    role: str
    tokens_balance: float
    free_tokens_remaining_today: float
    available_tokens: float


class SettingsResponse(BaseModel):
    leads_per_token: float
    qr_code_url: str | None = None
    payment_instructions: str | None = None
    free_daily_tokens: float = 0.0


class SettingsUpdateRequest(BaseModel):
    leads_per_token: float | None = None
    qr_code_url: str | None = None
    payment_instructions: str | None = None
    free_daily_tokens: float | None = None


class CreditTokensRequest(BaseModel):
    email: EmailStr
    tokens: float  # positive to add, negative to deduct/correct


class ConsumeTokensRequest(BaseModel):
    tokens: float  # amount to deduct for a completed scrape


class UserSummary(BaseModel):
    email: str
    role: str
    tokens_balance: float
    free_tokens_remaining_today: float
    created_at: datetime


class BalanceResponse(BaseModel):
    tokens_balance: float
    free_tokens_remaining_today: float
    available_tokens: float


class PaymentSubmissionCreate(BaseModel):
    image_data: str          # base64-encoded image bytes (no data: prefix)
    image_mime: str = "image/png"
    tokens_requested: float
    note: str | None = None


class PaymentSubmissionResponse(BaseModel):
    id: int
    user_email: str
    tokens_requested: float
    note: str | None
    status: str
    admin_note: str | None
    tokens_granted: float | None
    created_at: datetime
    reviewed_at: datetime | None
    # image_data intentionally omitted from the list response (heavy) —
    # fetched individually via /admin/payment-submissions/{id}/image


class PaymentSubmissionReview(BaseModel):
    tokens: float | None = None   # approve only: amount to credit (defaults to tokens_requested)
    admin_note: str | None = None


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------

def create_access_token(email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS)
    payload = {"sub": email, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_email_action_token(submission_id: int, action: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=EMAIL_ACTION_EXPIRE_HOURS)
    payload = {"sub_id": submission_id, "action": action, "scope": "email_action", "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_user_email(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> str:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return email
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_current_user(
    email: str = Depends(get_current_user_email),
    db: Session = Depends(get_db),
) -> User:
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _apply_admin_role(user: User):
    """Promote known admin emails automatically; safe to call repeatedly."""
    if user.email.lower() in ADMIN_EMAILS and user.role != "admin":
        user.role = "admin"


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

app = FastAPI(title="Maps Lead Scraper Auth")


@app.get("/health")
def health():
    return {"status": "ok"}


# -- Signup (OTP-gated) ------------------------------------------------------

@app.post("/signup/request-otp", status_code=status.HTTP_200_OK)
def signup_request_otp(req: RequestOtpRequest, db: Session = Depends(get_db)):
    """Step 1 of signup: validate + stash the (hashed) password and email
    an OTP. No User row is created yet."""
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    email = req.email.lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    code = generate_otp()
    pending = db.query(PendingSignup).filter(PendingSignup.email == email).first()
    if not pending:
        pending = PendingSignup(email=email)
        db.add(pending)
    pending.hashed_password = hash_password(req.password)
    pending.otp_hash = hash_otp(code, email)
    pending.expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES)
    pending.attempts = 0
    db.commit()

    sent = send_otp_email(email, code)
    if not sent:
        # Don't leak whether email delivery succeeded to an unauthenticated
        # caller in detail, but do surface a generic failure so the GUI can
        # tell the user to retry rather than silently hang.
        raise HTTPException(status_code=502, detail="Couldn't send the verification email — try again shortly")

    return {"detail": f"Verification code sent to {email}"}


@app.post("/signup/verify-otp", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup_verify_otp(req: VerifyOtpRequest, db: Session = Depends(get_db)):
    """Step 2 of signup: check the code, then actually create the User."""
    email = req.email.lower()
    pending = db.query(PendingSignup).filter(PendingSignup.email == email).first()
    if not pending:
        raise HTTPException(status_code=400, detail="No pending signup for this email — request a new code")

    if datetime.now(timezone.utc) > pending.expires_at.replace(tzinfo=timezone.utc):
        db.delete(pending)
        db.commit()
        raise HTTPException(status_code=400, detail="Code expired — request a new one")

    if pending.attempts >= OTP_MAX_ATTEMPTS:
        db.delete(pending)
        db.commit()
        raise HTTPException(status_code=429, detail="Too many incorrect attempts — request a new code")

    if hash_otp(req.code.strip(), email) != pending.otp_hash:
        pending.attempts += 1
        db.commit()
        raise HTTPException(status_code=400, detail="Incorrect code")

    user = User(
        email=email,
        hashed_password=pending.hashed_password,
        role="admin" if email in ADMIN_EMAILS else "user",
        tokens_balance=0.0,
    )
    db.add(user)
    db.delete(pending)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    db.refresh(user)

    settings = _get_or_create_settings(db)
    _reset_free_tokens_if_new_day(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user.email)
    return TokenResponse(
        access_token=token, email=user.email, role=user.role,
        tokens_balance=user.tokens_balance,
        free_tokens_remaining_today=free_tokens_remaining(user, settings),
        available_tokens=available_tokens(user, settings),
    )


@app.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.lower()).first()
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    _apply_admin_role(user)
    _reset_free_tokens_if_new_day(user)
    db.commit()
    db.refresh(user)

    settings = _get_or_create_settings(db)
    token = create_access_token(user.email)
    return TokenResponse(
        access_token=token, email=user.email, role=user.role,
        tokens_balance=user.tokens_balance,
        free_tokens_remaining_today=free_tokens_remaining(user, settings),
        available_tokens=available_tokens(user, settings),
    )


@app.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The desktop app calls this on launch with its saved token to check
    the session is still valid before skipping straight to the main window,
    and to pick up the user's current role and token balance."""
    _reset_free_tokens_if_new_day(user)
    db.commit()
    db.refresh(user)
    settings = _get_or_create_settings(db)
    return MeResponse(
        email=user.email, role=user.role, tokens_balance=user.tokens_balance,
        free_tokens_remaining_today=free_tokens_remaining(user, settings),
        available_tokens=available_tokens(user, settings),
    )


@app.get("/settings", response_model=SettingsResponse)
def get_settings(db: Session = Depends(get_db)):
    """Public: leads-per-token ratio, QR code, payment instructions, and
    the free daily token allowance that the desktop app displays."""
    settings = _get_or_create_settings(db)
    return SettingsResponse(
        leads_per_token=settings.leads_per_token,
        qr_code_url=settings.qr_code_url,
        payment_instructions=settings.payment_instructions,
        free_daily_tokens=settings.free_daily_tokens,
    )


@app.put("/admin/settings", response_model=SettingsResponse)
def update_settings(
    req: SettingsUpdateRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    settings = _get_or_create_settings(db)
    if req.leads_per_token is not None:
        if req.leads_per_token <= 0:
            raise HTTPException(status_code=400, detail="leads_per_token must be > 0")
        settings.leads_per_token = req.leads_per_token
    if req.qr_code_url is not None:
        settings.qr_code_url = req.qr_code_url
    if req.payment_instructions is not None:
        settings.payment_instructions = req.payment_instructions
    if req.free_daily_tokens is not None:
        if req.free_daily_tokens < 0:
            raise HTTPException(status_code=400, detail="free_daily_tokens must be >= 0")
        settings.free_daily_tokens = req.free_daily_tokens
    db.commit()
    db.refresh(settings)
    return SettingsResponse(
        leads_per_token=settings.leads_per_token,
        qr_code_url=settings.qr_code_url,
        payment_instructions=settings.payment_instructions,
        free_daily_tokens=settings.free_daily_tokens,
    )


@app.get("/admin/users", response_model=list[UserSummary])
def list_users(db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    settings = _get_or_create_settings(db)
    users = db.query(User).order_by(User.created_at.desc()).all()
    out = []
    for u in users:
        _reset_free_tokens_if_new_day(u)
        out.append(UserSummary(
            email=u.email, role=u.role, tokens_balance=u.tokens_balance,
            free_tokens_remaining_today=free_tokens_remaining(u, settings),
            created_at=u.created_at,
        ))
    db.commit()  # persist any lazy resets triggered above
    return out


@app.post("/admin/credit-tokens", response_model=UserSummary)
def credit_tokens(
    req: CreditTokensRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Manual step after the admin sees a payment come in — credits (or
    corrects) a user's PAID token balance by email. Positive tokens add,
    negative tokens deduct. Does not touch the free daily allowance."""
    user = db.query(User).filter(User.email == req.email.lower()).first()
    if not user:
        raise HTTPException(status_code=404, detail="No account with this email")
    user.tokens_balance = max(0.0, user.tokens_balance + req.tokens)
    _reset_free_tokens_if_new_day(user)
    db.commit()
    db.refresh(user)
    settings = _get_or_create_settings(db)
    return UserSummary(
        email=user.email, role=user.role, tokens_balance=user.tokens_balance,
        free_tokens_remaining_today=free_tokens_remaining(user, settings),
        created_at=user.created_at,
    )


@app.post("/consume-tokens", response_model=BalanceResponse)
def consume_tokens(
    req: ConsumeTokensRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Called by the desktop app right after a scrape finishes, with the
    token cost computed client-side from the leads actually found
    (email found = 1 token, phone-only = 0.5, neither = 0.2). Admin
    accounts are never charged. Spends today's free allowance first (it
    expires anyway), then draws down the paid balance. Never goes below
    zero on either bucket."""
    settings = _get_or_create_settings(db)
    _reset_free_tokens_if_new_day(user)

    if user.role == "admin":
        db.commit()
        db.refresh(user)
        return BalanceResponse(
            tokens_balance=user.tokens_balance,
            free_tokens_remaining_today=0.0,
            available_tokens=available_tokens(user, settings),
        )
    if req.tokens < 0:
        raise HTTPException(status_code=400, detail="tokens must be >= 0")

    remaining_cost = req.tokens
    free_left = free_tokens_remaining(user, settings)
    from_free = min(free_left, remaining_cost)
    user.free_tokens_used_today += from_free
    remaining_cost -= from_free
    user.tokens_balance = max(0.0, user.tokens_balance - remaining_cost)

    db.commit()
    db.refresh(user)
    return BalanceResponse(
        tokens_balance=user.tokens_balance,
        free_tokens_remaining_today=free_tokens_remaining(user, settings),
        available_tokens=available_tokens(user, settings),
    )


@app.get("/balance", response_model=BalanceResponse)
def get_balance(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Lightweight poll the GUI can use to refresh the on-screen balance
    without re-doing a full /me call."""
    _reset_free_tokens_if_new_day(user)
    db.commit()
    db.refresh(user)
    settings = _get_or_create_settings(db)
    return BalanceResponse(
        tokens_balance=user.tokens_balance,
        free_tokens_remaining_today=free_tokens_remaining(user, settings),
        available_tokens=available_tokens(user, settings),
    )


# -- Payment proof submissions ------------------------------------------------

@app.post("/payment-submissions", response_model=PaymentSubmissionResponse, status_code=status.HTTP_201_CREATED)
def create_payment_submission(
    req: PaymentSubmissionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if req.tokens_requested <= 0:
        raise HTTPException(status_code=400, detail="tokens_requested must be > 0")
    try:
        base64.b64decode(req.image_data, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="image_data must be valid base64")

    sub = PaymentSubmission(
        user_email=user.email,
        image_data=req.image_data,
        image_mime=req.image_mime or "image/png",
        tokens_requested=req.tokens_requested,
        note=req.note,
        status="pending",
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)

    approve_url = f"{APP_BASE_URL}/admin/email-action?token={create_email_action_token(sub.id, 'approve')}"
    decline_url = f"{APP_BASE_URL}/admin/email-action?token={create_email_action_token(sub.id, 'decline')}"
    send_email(
        list(ADMIN_NOTIFY_EMAILS),
        subject=f"New payment proof from {user.email} ({req.tokens_requested:g} tokens)",
        body_html=(
            f"<p><b>{user.email}</b> submitted proof of payment for "
            f"<b>{req.tokens_requested:g} tokens</b>.</p>"
            f"<p>Note: {req.note or '(none)'}</p>"
            f"<p>"
            f"<a href='{approve_url}' style='background:#3d6bff;color:white;"
            f"padding:8px 16px;border-radius:6px;text-decoration:none;'>Approve</a>"
            f"&nbsp;&nbsp;"
            f"<a href='{decline_url}' style='background:#e5484d;color:white;"
            f"padding:8px 16px;border-radius:6px;text-decoration:none;'>Decline</a>"
            f"</p>"
            f"<p style='color:#888;font-size:12px'>Or review it from the app's Admin tab "
            f"(submission #{sub.id}). Approving here credits exactly the requested amount; "
            f"use the app if you need to grant a different amount.</p>"
        ),
    )

    return PaymentSubmissionResponse(
        id=sub.id, user_email=sub.user_email, tokens_requested=sub.tokens_requested,
        note=sub.note, status=sub.status, admin_note=sub.admin_note,
        tokens_granted=sub.tokens_granted, created_at=sub.created_at, reviewed_at=sub.reviewed_at,
    )


@app.get("/my/payment-submissions", response_model=list[PaymentSubmissionResponse])
def my_payment_submissions(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    subs = (
        db.query(PaymentSubmission)
        .filter(PaymentSubmission.user_email == user.email)
        .order_by(PaymentSubmission.created_at.desc())
        .all()
    )
    return [
        PaymentSubmissionResponse(
            id=s.id, user_email=s.user_email, tokens_requested=s.tokens_requested,
            note=s.note, status=s.status, admin_note=s.admin_note,
            tokens_granted=s.tokens_granted, created_at=s.created_at, reviewed_at=s.reviewed_at,
        )
        for s in subs
    ]


@app.get("/admin/payment-submissions", response_model=list[PaymentSubmissionResponse])
def admin_list_payment_submissions(
    status_filter: str | None = Query(default="pending", alias="status"),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    q = db.query(PaymentSubmission)
    if status_filter and status_filter != "all":
        q = q.filter(PaymentSubmission.status == status_filter)
    subs = q.order_by(PaymentSubmission.created_at.desc()).all()
    return [
        PaymentSubmissionResponse(
            id=s.id, user_email=s.user_email, tokens_requested=s.tokens_requested,
            note=s.note, status=s.status, admin_note=s.admin_note,
            tokens_granted=s.tokens_granted, created_at=s.created_at, reviewed_at=s.reviewed_at,
        )
        for s in subs
    ]


@app.get("/admin/payment-submissions/{submission_id}/image")
def admin_get_payment_submission_image(
    submission_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Returns the raw base64 + mime so the GUI can render it. Kept out of
    the list endpoint since images can be large and the list is polled."""
    sub = db.query(PaymentSubmission).filter(PaymentSubmission.id == submission_id).first()
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    return {"image_data": sub.image_data, "image_mime": sub.image_mime}


def _apply_review(db: Session, sub: PaymentSubmission, action: str, tokens: float | None,
                   admin_note: str | None, reviewer_email: str) -> User | None:
    if sub.status != "pending":
        return None  # already reviewed — caller decides how to report this

    user = db.query(User).filter(User.email == sub.user_email).first()
    sub.status = "approved" if action == "approve" else "declined"
    sub.admin_note = admin_note
    sub.reviewed_at = datetime.now(timezone.utc)
    sub.reviewed_by = reviewer_email

    if action == "approve" and user is not None:
        grant = tokens if tokens is not None else sub.tokens_requested
        user.tokens_balance = max(0.0, user.tokens_balance + grant)
        sub.tokens_granted = grant

    db.commit()
    if user is not None:
        db.refresh(user)
    return user


@app.post("/admin/payment-submissions/{submission_id}/approve", response_model=PaymentSubmissionResponse)
def admin_approve_payment_submission(
    submission_id: int,
    req: PaymentSubmissionReview,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    sub = db.query(PaymentSubmission).filter(PaymentSubmission.id == submission_id).first()
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    if sub.status != "pending":
        raise HTTPException(status_code=409, detail=f"Already reviewed (status: {sub.status})")
    _apply_review(db, sub, "approve", req.tokens, req.admin_note, admin.email)
    return PaymentSubmissionResponse(
        id=sub.id, user_email=sub.user_email, tokens_requested=sub.tokens_requested,
        note=sub.note, status=sub.status, admin_note=sub.admin_note,
        tokens_granted=sub.tokens_granted, created_at=sub.created_at, reviewed_at=sub.reviewed_at,
    )


@app.post("/admin/payment-submissions/{submission_id}/decline", response_model=PaymentSubmissionResponse)
def admin_decline_payment_submission(
    submission_id: int,
    req: PaymentSubmissionReview,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    sub = db.query(PaymentSubmission).filter(PaymentSubmission.id == submission_id).first()
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    if sub.status != "pending":
        raise HTTPException(status_code=409, detail=f"Already reviewed (status: {sub.status})")
    _apply_review(db, sub, "decline", None, req.admin_note, admin.email)
    return PaymentSubmissionResponse(
        id=sub.id, user_email=sub.user_email, tokens_requested=sub.tokens_requested,
        note=sub.note, status=sub.status, admin_note=sub.admin_note,
        tokens_granted=sub.tokens_granted, created_at=sub.created_at, reviewed_at=sub.reviewed_at,
    )


@app.get("/admin/email-action", response_class=HTMLResponse)
def admin_email_action(token: str = Query(...), db: Session = Depends(get_db)):
    """One-click approve/decline landing page for the links sent in the
    admin notification email — no login required in the browser, since the
    signed token itself proves it came from that email and scopes it to
    exactly one submission + action. Approving here always grants the
    exact tokens_requested amount; use the app's Admin tab for a custom
    amount."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("scope") != "email_action":
            raise JWTError("wrong scope")
        submission_id = payload["sub_id"]
        action = payload["action"]
    except (JWTError, KeyError):
        return HTMLResponse("<h3>This link is invalid or has expired.</h3>", status_code=400)

    sub = db.query(PaymentSubmission).filter(PaymentSubmission.id == submission_id).first()
    if not sub:
        return HTMLResponse("<h3>Submission not found.</h3>", status_code=404)

    if sub.status != "pending":
        return HTMLResponse(
            f"<h3>Already reviewed.</h3><p>Submission #{sub.id} from {sub.user_email} "
            f"was already marked <b>{sub.status}</b>"
            f"{f' by {sub.reviewed_by}' if sub.reviewed_by else ''}.</p>"
        )

    user = _apply_review(db, sub, action, None, "email-link", "email-link")
    if action == "approve":
        new_balance = user.tokens_balance if user else None
        return HTMLResponse(
            f"<h3>Approved ✅</h3>"
            f"<p>Credited {sub.tokens_granted:g} tokens to {sub.user_email}."
            f"{f' New balance: {new_balance:g}.' if new_balance is not None else ''}</p>"
        )
    return HTMLResponse(f"<h3>Declined</h3><p>Submission #{sub.id} from {sub.user_email} marked declined.</p>")