"""A small FastAPI service with established team conventions. Every sample PR branches from this.

The conventions (docs/CONVENTIONS.md) are what make context matter: several planted bugs look fine in
the diff alone and only break a project pattern.
"""

from textwrap import dedent

_d = dedent

TOY_REPOSITORY: dict[str, str] = {
    "app/__init__.py": "",
    "app/config.py": _d('''\
        """All secrets and environment-specific settings come from environment variables."""
        import os


        class Settings:
            SECRET_KEY: str = os.environ["SECRET_KEY"]
            PAYMENTS_API_KEY: str = os.environ["PAYMENTS_API_KEY"]
            ALLOWED_ORIGINS: list[str] = os.environ.get("ALLOWED_ORIGINS", "https://app.example.com").split(",")
            UPLOAD_DIR: str = os.environ.get("UPLOAD_DIR", "/srv/uploads")
            MAX_UPLOAD_BYTES: int = int(os.environ.get("MAX_UPLOAD_BYTES", "1048576"))


        settings = Settings()
        '''),
    "app/db.py": _d('''\
        import sqlite3
        from contextlib import contextmanager


        @contextmanager
        def get_db():
            """Yield a connection inside a transaction. Commits on success, rolls back on error."""
            conn = sqlite3.connect("app.db")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        '''),
    "app/auth.py": _d('''\
        import logging
        import secrets

        from fastapi import Depends, Header, HTTPException

        from app.config import settings
        from app.schemas import User

        logger = logging.getLogger(__name__)


        def get_current_user(authorization: str = Header(...)) -> User:
            """Every endpoint that touches user data MUST depend on this."""
            user = User.from_token(authorization.removeprefix("Bearer "), settings.SECRET_KEY)
            if user is None:
                raise HTTPException(status_code=401, detail="Not authenticated")
            return user


        def require_admin(user: User = Depends(get_current_user)) -> User:
            if not user.is_admin:
                raise HTTPException(status_code=403, detail="Admin only")
            return user


        def verify_webhook_signature(received: str, expected: str) -> bool:
            # Constant-time comparison for anything secret. Never use == on secrets.
            return secrets.compare_digest(received, expected)
        '''),
    "app/security.py": _d('''\
        import hashlib
        import os
        import secrets
        import time
        from collections import defaultdict
        from functools import wraps
        from urllib.parse import urlparse

        from fastapi import HTTPException

        _hits: dict[str, list[float]] = defaultdict(list)


        def generate_token() -> str:
            """Tokens MUST come from the secrets module."""
            return secrets.token_urlsafe(32)


        def hash_password(password: str, salt: bytes | None = None) -> str:
            salt = salt or os.urandom(16)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
            return salt.hex() + ":" + digest.hex()


        def rate_limit(max_calls: int, per_seconds: int):
            """Required on every authentication-related endpoint."""
            def deco(fn):
                @wraps(fn)
                def wrapper(*args, client_ip: str = "unknown", **kwargs):
                    now = time.time()
                    _hits[client_ip] = [t for t in _hits[client_ip] if now - t < per_seconds]
                    if len(_hits[client_ip]) >= max_calls:
                        raise HTTPException(status_code=429, detail="Too many requests")
                    _hits[client_ip].append(now)
                    return fn(*args, **kwargs)
                return wrapper
            return deco


        def safe_join(base: str, user_path: str) -> str:
            """Join a user-supplied path to base, refusing anything that escapes it."""
            full = os.path.realpath(os.path.join(base, user_path))
            if not full.startswith(os.path.realpath(base) + os.sep):
                raise HTTPException(status_code=400, detail="Invalid path")
            return full


        def is_safe_redirect(url: str) -> bool:
            """Only relative URLs or our own host may be redirect targets."""
            parsed = urlparse(url)
            return not parsed.netloc or parsed.netloc == "app.example.com"
        '''),
    "app/schemas.py": _d('''\
        from pydantic import BaseModel, EmailStr, Field


        class User(BaseModel):
            id: int
            email: EmailStr
            is_admin: bool = False

            @classmethod
            def from_token(cls, token: str, key: str) -> "User | None":
                ...


        class UserProfile(BaseModel):
            """All request bodies are Pydantic models with explicit constraints."""
            name: str = Field(min_length=1, max_length=100)
            email: EmailStr
            age: int = Field(ge=13, le=130)


        class UserPublic(BaseModel):
            """What we return to clients. Never includes password_hash."""
            id: int
            name: str
            email: EmailStr
        '''),
    "app/errors.py": _d('''\
        import logging

        from fastapi import Request
        from fastapi.responses import JSONResponse

        logger = logging.getLogger(__name__)


        async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
            """Log details server-side. Clients only ever get a generic message."""
            logger.exception("unhandled error on %s", request.url.path)
            return JSONResponse(status_code=500, content={"detail": "Internal server error"})
        '''),
    "app/main.py": _d('''\
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware

        from app.config import settings
        from app.errors import unhandled_error
        from app.routes import auth_routes, files, orders, users

        app = FastAPI()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.ALLOWED_ORIGINS,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )
        app.add_exception_handler(Exception, unhandled_error)
        for r in (auth_routes, files, orders, users):
            app.include_router(r.router)
        '''),
    "app/routes/__init__.py": "",
    "app/routes/users.py": _d('''\
        import logging

        from fastapi import APIRouter, Depends, HTTPException

        from app.auth import get_current_user
        from app.db import get_db
        from app.schemas import User, UserPublic

        router = APIRouter()
        logger = logging.getLogger(__name__)


        @router.get("/api/users/{user_id}", response_model=UserPublic)
        def get_user(user_id: int, current_user: User = Depends(get_current_user)):
            with get_db() as db:
                row = db.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Not found")
            return UserPublic(id=row[0], name=row[1], email=row[2])


        @router.delete("/api/users/{user_id}")
        def delete_user(user_id: int, current_user: User = Depends(get_current_user)):
            if current_user.id != user_id and not current_user.is_admin:
                raise HTTPException(status_code=403, detail="Forbidden")
            try:
                with get_db() as db:
                    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            except Exception:
                logger.exception("delete_user failed for %s", user_id)
                raise HTTPException(status_code=500, detail="Could not delete user")
            return {"status": "deleted"}
        '''),
    "app/routes/orders.py": _d('''\
        import logging

        from fastapi import APIRouter, Depends, HTTPException

        from app.auth import get_current_user
        from app.db import get_db
        from app.schemas import User

        router = APIRouter()
        logger = logging.getLogger(__name__)


        @router.post("/api/orders")
        def create_order(product_id: int, quantity: int, current_user: User = Depends(get_current_user)):
            if quantity <= 0:
                raise HTTPException(status_code=400, detail="quantity must be positive")
            try:
                with get_db() as db:
                    # Inventory rows MUST be locked with FOR UPDATE before a read-modify-write.
                    stock = db.execute(
                        "SELECT stock FROM inventory WHERE product_id = ? FOR UPDATE", (product_id,)
                    ).fetchone()[0]
                    if stock < quantity:
                        raise HTTPException(status_code=409, detail="Insufficient stock")
                    db.execute("UPDATE inventory SET stock = stock - ? WHERE product_id = ?", (quantity, product_id))
                    db.execute("INSERT INTO orders (user_id, product_id, quantity) VALUES (?, ?, ?)",
                               (current_user.id, product_id, quantity))
            except HTTPException:
                raise
            except Exception:
                logger.exception("create_order failed")
                raise HTTPException(status_code=500, detail="Could not create order")
            return {"status": "created"}
        '''),
    "app/routes/auth_routes.py": _d('''\
        from fastapi import APIRouter, HTTPException
        from fastapi.responses import RedirectResponse

        from app.security import generate_token, is_safe_redirect, rate_limit

        router = APIRouter()


        @router.post("/api/login")
        @rate_limit(max_calls=5, per_seconds=60)
        def login(email: str, password: str, next: str = "/"):
            token = generate_token()
            if not is_safe_redirect(next):
                raise HTTPException(status_code=400, detail="Invalid redirect")
            response = RedirectResponse(next)
            response.set_cookie("session", token, httponly=True, secure=True, samesite="lax")
            return response
        '''),
    "app/routes/files.py": _d('''\
        from fastapi import APIRouter, Depends
        from fastapi.responses import FileResponse

        from app.auth import get_current_user
        from app.config import settings
        from app.schemas import User
        from app.security import safe_join

        router = APIRouter()


        @router.get("/api/files/{name}")
        def download(name: str, current_user: User = Depends(get_current_user)):
            return FileResponse(safe_join(settings.UPLOAD_DIR, name))
        '''),
    "docs/CONVENTIONS.md": _d('''\
        # Team conventions

        1. Every endpoint that touches user data depends on `get_current_user` (app/auth.py).
        2. Database writes run inside `get_db()` and a try/except that logs with `logger.exception`
           and returns a generic HTTPException(500). Never return exception text to clients.
        3. Secrets and environment-specific values come from `app.config.settings`, never literals.
        4. SQL always uses `?` placeholders. No string formatting.
        5. Inventory read-modify-write must lock rows with `SELECT ... FOR UPDATE`.
        6. Tokens come from `secrets` (`generate_token`). Passwords use `hash_password` (PBKDF2).
        7. Compare secrets with `secrets.compare_digest`, never `==`. Never log secrets.
        8. Authentication-related endpoints use `@rate_limit`.
        9. Request bodies are Pydantic models with explicit constraints (app/schemas.py).
        10. Responses use `UserPublic` and never include password hashes.
        11. User-supplied file paths go through `safe_join`. Redirect targets through `is_safe_redirect`.
        12. CORS origins come from `settings.ALLOWED_ORIGINS`. Never `*` with credentials.
        13. Untrusted data is parsed with `json` or Pydantic. Never `pickle`.
        14. HTML output must be escaped.
        '''),
    "tests/test_users.py": _d('''\
        def test_get_user_requires_auth(client):
            assert client.get("/api/users/1").status_code == 401
        '''),
    ".env": "SECRET_KEY=do-not-read-this-secret\nPAYMENTS_API_KEY=do-not-read-this-either\n",
}


def get_toy_repository() -> dict[str, str]:
    """Code files only, as the context engine sees them (no .env)."""
    return {p: c for p, c in TOY_REPOSITORY.items() if not p.startswith(".env")}
