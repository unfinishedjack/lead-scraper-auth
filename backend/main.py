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
"""

import os
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy import Column, DateTime, Integer, String, create_engine
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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


class MeResponse(BaseModel):
    email: str


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
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    token = create_access_token(user.email)
    return TokenResponse(access_token=token, email=user.email)


@app.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.lower()).first()
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    token = create_access_token(user.email)
    return TokenResponse(access_token=token, email=user.email)


@app.get("/me", response_model=MeResponse)
def me(email: str = Depends(get_current_user_email)):
    """The desktop app calls this on launch with its saved token to check
    the session is still valid before skipping straight to the main window."""
    return MeResponse(email=email)

