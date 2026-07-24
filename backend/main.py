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
    JWT_SECRET     - any long random string (Render can auto-generate one)
    DATABASE_URL   - Postgres URL from a Render free Postgres instance.
                     Falls back to a local sqlite file if unset, which
                     is fine for quick local testing but NOT persistent
                     on Render's free web service (disk resets on deploy).
    ADMIN_EMAILS   - comma-separated list of emails that should be
                     promoted to "admin" role automatically on signup/
                     login (e.g. "you@example.com,partner@example.com").
"""

import os
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy import Column, DateTime, Float, Integer, String, create_engine
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

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

bearer_scheme = HTTPBearer()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


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
    tokens_balance = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AppSettings(Base):
    """Single-row table of admin-controlled knobs the desktop app reads
    on startup. Row id is always 1."""
    __tablename__ = "app_settings"
    id = Column(Integer, primary_key=True, default=1)
    leads_per_token = Column(Float, nullable=False, default=10.0)
    qr_code_url = Column(String, nullable=True)
    payment_instructions = Column(String, nullable=True)


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
                "then message the admin with your proof of payment and the "
                "email you signed up with. Tokens are added manually."
            ),
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class SignupRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str
    role: str
    tokens_balance: float


class MeResponse(BaseModel):
    email: str
    role: str
    tokens_balance: float


class SettingsResponse(BaseModel):
    leads_per_token: float
    qr_code_url: str | None = None
    payment_instructions: str | None = None


class SettingsUpdateRequest(BaseModel):
    leads_per_token: float | None = None
    qr_code_url: str | None = None
    payment_instructions: str | None = None


class CreditTokensRequest(BaseModel):
    email: EmailStr
    tokens: float  # positive to add, negative to deduct/correct


class ConsumeTokensRequest(BaseModel):
    tokens: float  # amount to deduct for a completed scrape


class UserSummary(BaseModel):
    email: str
    role: str
    tokens_balance: float
    created_at: datetime


class BalanceResponse(BaseModel):
    tokens_balance: float


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------

def create_access_token(email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS)
    payload = {"sub": email, "exp": expire}
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


@app.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(req: SignupRequest, db: Session = Depends(get_db)):
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    user = User(
        email=req.email.lower(),
        hashed_password=hash_password(req.password),
        role="admin" if req.email.lower() in ADMIN_EMAILS else "user",
        tokens_balance=0.0,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    db.refresh(user)

    token = create_access_token(user.email)
    return TokenResponse(
        access_token=token, email=user.email, role=user.role,
        tokens_balance=user.tokens_balance,
    )


@app.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.lower()).first()
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    _apply_admin_role(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user.email)
    return TokenResponse(
        access_token=token, email=user.email, role=user.role,
        tokens_balance=user.tokens_balance,
    )


@app.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user)):
    """The desktop app calls this on launch with its saved token to check
    the session is still valid before skipping straight to the main window,
    and to pick up the user's current role and token balance."""
    return MeResponse(email=user.email, role=user.role, tokens_balance=user.tokens_balance)


@app.get("/settings", response_model=SettingsResponse)
def get_settings(db: Session = Depends(get_db)):
    """Public: leads-per-token ratio, QR code, and payment instructions
    that the desktop app displays on its Buy Tokens panel."""
    settings = _get_or_create_settings(db)
    return SettingsResponse(
        leads_per_token=settings.leads_per_token,
        qr_code_url=settings.qr_code_url,
        payment_instructions=settings.payment_instructions,
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
    db.commit()
    db.refresh(settings)
    return SettingsResponse(
        leads_per_token=settings.leads_per_token,
        qr_code_url=settings.qr_code_url,
        payment_instructions=settings.payment_instructions,
    )


@app.get("/admin/users", response_model=list[UserSummary])
def list_users(db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    users = db.query(User).order_by(User.created_at.desc()).all()
    return [
        UserSummary(
            email=u.email, role=u.role, tokens_balance=u.tokens_balance,
            created_at=u.created_at,
        )
        for u in users
    ]


@app.post("/admin/credit-tokens", response_model=UserSummary)
def credit_tokens(
    req: CreditTokensRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Manual step after the admin sees a payment come in outside the app
    (bank/e-wallet notification) — credits (or corrects) a user's token
    balance by email. Positive tokens add, negative tokens deduct."""
    user = db.query(User).filter(User.email == req.email.lower()).first()
    if not user:
        raise HTTPException(status_code=404, detail="No account with this email")
    user.tokens_balance = max(0.0, user.tokens_balance + req.tokens)
    db.commit()
    db.refresh(user)
    return UserSummary(
        email=user.email, role=user.role, tokens_balance=user.tokens_balance,
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
    (email found = 1 token, phone-only = 0.5, neither = free). Admin
    accounts are never charged. Balance never goes below zero — if the
    computed cost exceeds the remaining balance, the user simply lands
    at 0 rather than going negative (the running low-balance check in the
    scraper is what's supposed to stop the run before this point anyway)."""
    if user.role == "admin":
        return BalanceResponse(tokens_balance=user.tokens_balance)
    if req.tokens < 0:
        raise HTTPException(status_code=400, detail="tokens must be >= 0")
    user.tokens_balance = max(0.0, user.tokens_balance - req.tokens)
    db.commit()
    db.refresh(user)
    return BalanceResponse(tokens_balance=user.tokens_balance)


@app.get("/balance", response_model=BalanceResponse)
def get_balance(user: User = Depends(get_current_user)):
    """Lightweight poll the GUI can use to refresh the on-screen balance
    without re-doing a full /me call."""
    return BalanceResponse(tokens_balance=user.tokens_balance)