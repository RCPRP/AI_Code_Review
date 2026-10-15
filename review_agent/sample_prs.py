"""15 sample PRs against the toy repository, two planted issues each (mirrors the course's Lesson 4 set).

Each PR becomes a small git repo: `main` holds the toy repository, `feature` holds the change.
`changes` maps a path to code that is appended to the existing file, or becomes a new file.
"""

import shutil
import subprocess
from pathlib import Path
from textwrap import dedent

from .toy_repository import TOY_REPOSITORY

EVAL_ROOT = Path(__file__).resolve().parents[1] / ".eval_repos"

PROJECT_CONTEXT_HINT = "Team conventions are in docs/CONVENTIONS.md."

_AUTH_KW = ["get_current_user", "authenticat", "unauthenticated", "depends("]


def _issue(id_, description, file, keywords):
    return {"id": id_, "description": description, "file": file, "keywords": keywords}


SAMPLE_PRS = [
    {
        "id": "pr_001_auth_bypass", "title": "Add user profile update endpoint",
        "task_context": "Users can update their own profile (name, email, age).",
        "changes": {"app/routes/users.py": '''

            from app.schemas import UserProfile


            @router.put("/api/users/{user_id}")
            def update_user(user_id: int, profile: UserProfile):
                with get_db() as db:
                    db.execute("UPDATE users SET name = ?, email = ?, age = ? WHERE id = ?",
                               (profile.name, profile.email, profile.age, user_id))
                return {"status": "success"}
            '''},
        "expected_issues": [
            _issue("auth", "violates mandatory Depends(get_current_user) authentication pattern", "app/routes/users.py", _AUTH_KW + ["authoriz", "any user"]),
            _issue("errors", "missing try/except with logging around the database write, unlike other write endpoints", "app/routes/users.py", ["try", "except", "error handling", "logger.exception", "unhandled"]),
        ],
    },
    {
        "id": "pr_002_sql_injection", "title": "Search users by name",
        "task_context": "Logged-in users can search other users by partial name.",
        "changes": {"app/routes/users.py": '''

            @router.get("/api/users/search")
            def search_users(q: str):
                with get_db() as db:
                    rows = db.execute(f"SELECT id, name, email FROM users WHERE name LIKE '%{q}%'").fetchall()
                return [{"id": r[0], "name": r[1], "email": r[2]} for r in rows]
            '''},
        "expected_issues": [
            _issue("sqli", "SQL injection: user input interpolated into the query", "app/routes/users.py", ["injection", "placeholder", "parameteri", "f-string", "interpolat"]),
            _issue("auth", "search endpoint has no Depends(get_current_user)", "app/routes/users.py", _AUTH_KW),
        ],
    },
    {
        "id": "pr_003_hardcoded_secret", "title": "Integrate payments provider",
        "task_context": "Charge customers through the payments API.",
        "changes": {"app/payments.py": '''
            import requests

            PAYMENTS_API_KEY = "pay_live_7Qx2Lk9EXAMPLE0000"


            def charge(customer_id: str, cents: int) -> dict:
                return requests.post(
                    "https://api.payments.example/charge",
                    headers={"Authorization": f"Bearer {PAYMENTS_API_KEY}"},
                    json={"customer": customer_id, "amount": cents},
                ).json()
            '''},
        "expected_issues": [
            _issue("secret", "hardcoded live API key in source", "app/payments.py", ["hardcoded", "hard-coded", "secret", "credential", "api key"]),
            _issue("env", "violates environment variable pattern for secrets (app.config.settings)", "app/payments.py", ["settings", "environ", "app.config", "environment variable"]),
        ],
    },
    {
        "id": "pr_004_missing_error_handling", "title": "Cancel orders",
        "task_context": "Users can cancel their own pending orders; stock is returned to inventory.",
        "changes": {"app/routes/orders.py": '''

            @router.post("/api/orders/{order_id}/cancel")
            def cancel_order(order_id: int, current_user: User = Depends(get_current_user)):
                try:
                    with get_db() as db:
                        product_id, qty = db.execute(
                            "SELECT product_id, quantity FROM orders WHERE id = ? AND user_id = ?",
                            (order_id, current_user.id)).fetchone()
                        db.execute("UPDATE inventory SET stock = stock + ? WHERE product_id = ?", (qty, product_id))
                        db.execute("DELETE FROM orders WHERE id = ?", (order_id,))
                except Exception as e:
                    return {"error": str(e)}
                return {"status": "cancelled"}
            '''},
        "expected_issues": [
            _issue("leak", "returns raw exception text to the client instead of a generic 500", "app/routes/orders.py", ["str(e)", "exception text", "exception message", "leak", "internal", "disclos"]),
            _issue("notfound", "fetchone() can return None for a missing/other user's order and the error is swallowed as a 200", "app/routes/orders.py", ["none", "404", "not found", "status code", "200", "unpack"]),
        ],
    },
    {
        "id": "pr_005_race_condition", "title": "Reserve stock for carts",
        "task_context": "Reserve stock when an item is added to a cart.",
        "changes": {"app/routes/orders.py": '''

            @router.post("/api/cart/reserve")
            def reserve(product_id: int, quantity: int, current_user: User = Depends(get_current_user)):
                try:
                    with get_db() as db:
                        stock = db.execute("SELECT stock FROM inventory WHERE product_id = ?", (product_id,)).fetchone()[0]
                        if stock >= quantity:
                            db.execute("UPDATE inventory SET stock = ? WHERE product_id = ?", (stock - quantity, product_id))
                except Exception:
                    logger.exception("reserve failed")
                    raise HTTPException(status_code=500, detail="Could not reserve")
                return {"status": "reserved"}
            '''},
        "expected_issues": [
            _issue("lock", "violates mandatory FOR UPDATE locking pattern for inventory (race condition)", "app/routes/orders.py", ["for update", "race", "lock", "concurren", "atomic"]),
            _issue("qty", "quantity is not validated as positive, so a negative quantity increases stock", "app/routes/orders.py", ["negative", "positive", "quantity <= 0", "validat"]),
        ],
    },
    {
        "id": "pr_006_xss_vulnerability", "title": "Render user bio as HTML",
        "task_context": "Show a user's bio on a public profile page.",
        "changes": {"app/routes/users.py": '''

            from fastapi.responses import HTMLResponse


            @router.get("/profile/{user_id}", response_class=HTMLResponse)
            def profile_page(user_id: int, bio: str):
                return f"<html><body><h1>Profile</h1><div class='bio'>{bio}</div></body></html>"
            '''},
        "expected_issues": [
            _issue("xss", "unescaped user input rendered as HTML (XSS)", "app/routes/users.py", ["xss", "cross-site", "escape", "sanitiz"]),
            _issue("bio", "bio comes from a query parameter instead of the stored profile, so anyone can craft a page", "app/routes/users.py", ["query param", "query string", "from the request", "attacker-controlled", "crafted", "stored", "database"]),
        ],
    },
    {
        "id": "pr_007_weak_crypto", "title": "Password reset tokens",
        "task_context": "Email users a password reset token and store a hash of their new password.",
        "changes": {"app/security.py": '''

            import random


            def make_reset_token() -> str:
                return str(random.randint(100000, 999999))


            def quick_hash(password: str) -> str:
                return hashlib.md5(password.encode()).hexdigest()
            '''},
        "expected_issues": [
            _issue("random", "violates secrets module requirement for tokens (random is predictable)", "app/security.py", ["random", "secrets", "predictable", "token_urlsafe", "generate_token"]),
            _issue("md5", "MD5 used for password hashing instead of hash_password (PBKDF2)", "app/security.py", ["md5"]),
        ],
    },
    {
        "id": "pr_008_missing_rate_limit", "title": "Verify one-time codes",
        "task_context": "Users verify a 6-digit code sent by SMS. Codes expire after 5 minutes.",
        "changes": {"app/routes/auth_routes.py": '''

            OTP_STORE: dict[str, dict] = {}


            @router.post("/api/otp/verify")
            def verify_otp(phone: str, code: str):
                stored = OTP_STORE.get(phone)
                if stored and stored["code"] == code:
                    return {"token": generate_token()}
                raise HTTPException(status_code=401, detail="Invalid code")
            '''},
        "expected_issues": [
            _issue("ratelimit", "auth endpoint missing @rate_limit, so 6-digit codes can be brute-forced", "app/routes/auth_routes.py", ["rate limit", "rate_limit", "brute"]),
            _issue("expiry", "code expiry (5 minutes) is never checked", "app/routes/auth_routes.py", ["expir", "ttl", "5 minutes", "five minutes"]),
        ],
    },
    {
        "id": "pr_009_insecure_deserialization", "title": "Import saved carts",
        "task_context": "Users can upload a previously exported cart file to restore it.",
        "changes": {"app/routes/orders.py": '''

            import pickle

            from fastapi import Request


            @router.post("/api/cart/import")
            async def import_cart(request: Request, current_user: User = Depends(get_current_user)):
                cart = pickle.loads(await request.body())
                return {"items": len(cart)}
            '''},
        "expected_issues": [
            _issue("pickle", "pickle.loads on untrusted input allows remote code execution", "app/routes/orders.py", ["pickle", "deserializ", "remote code"]),
            _issue("size", "no limit on the uploaded body size (settings.MAX_UPLOAD_BYTES)", "app/routes/orders.py", ["size", "max_upload", "large", "memory", "dos", "denial"]),
        ],
    },
    {
        "id": "pr_010_missing_input_validation", "title": "Update age and email",
        "task_context": "Users can change their age and email address.",
        "changes": {"app/routes/users.py": '''

            @router.patch("/api/users/me")
            def update_me(age: int, email: str, current_user: User = Depends(get_current_user)):
                try:
                    with get_db() as db:
                        db.execute("UPDATE users SET age = ?, email = ? WHERE id = ?", (age, email, current_user.id))
                except Exception:
                    logger.exception("update_me failed")
                    raise HTTPException(status_code=500, detail="Could not update")
                return {"status": "ok"}
            '''},
        "expected_issues": [
            _issue("age", "age has no bounds (UserProfile requires 13-130)", "app/routes/users.py", ["age", "bound", "range", "negative"]),
            _issue("email", "email is a plain str, not validated as an email (EmailStr)", "app/routes/users.py", ["email"]),
        ],
    },
    {
        "id": "pr_011_info_disclosure", "title": "Debug-friendly errors and full user export",
        "task_context": "Make errors easier to debug, and let users export their account data.",
        "changes": {
            "app/errors.py": '''

                import traceback


                async def debug_error(request: Request, exc: Exception) -> JSONResponse:
                    return JSONResponse(status_code=500, content={"detail": str(exc), "trace": traceback.format_exc()})
                ''',
            "app/routes/users.py": '''

                @router.get("/api/users/me/export")
                def export_me(current_user: User = Depends(get_current_user)):
                    with get_db() as db:
                        row = db.execute("SELECT * FROM users WHERE id = ?", (current_user.id,)).fetchone()
                    return {"id": row[0], "name": row[1], "email": row[2], "password_hash": row[3]}
                ''',
        },
        "expected_issues": [
            _issue("trace", "stack traces and exception text returned to clients", "app/errors.py", ["traceback", "stack", "trace", "exception text", "str(exc)"]),
            _issue("hash", "password hash included in the export response (use UserPublic)", "app/routes/users.py", ["password_hash", "password hash", "hash", "userpublic"]),
        ],
    },
    {
        "id": "pr_012_path_traversal", "title": "Download invoices",
        "task_context": "Users download their invoice PDFs by file name.",
        "changes": {"app/routes/files.py": '''

            import os


            @router.get("/api/invoices/{name}")
            def invoice(name: str):
                return FileResponse(os.path.join(settings.UPLOAD_DIR, "invoices", name))
            '''},
        "expected_issues": [
            _issue("traversal", "path traversal: user path not passed through safe_join", "app/routes/files.py", ["traversal", "safe_join", "../", "escape"]),
            _issue("auth", "invoice download has no Depends(get_current_user)", "app/routes/files.py", _AUTH_KW + ["any user", "anyone"]),
        ],
    },
    {
        "id": "pr_013_cors_misconfiguration", "title": "Allow the partner widget to call our API",
        "task_context": "A partner's widget on partner.example.org needs to call our API.",
        "changes": {"app/main.py": '''

            app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
            '''},
        "expected_issues": [
            _issue("wildcard", "wildcard CORS origin instead of settings.ALLOWED_ORIGINS / the partner origin", "app/main.py", ["wildcard", "'*'", '"*"', "any origin", "all origins", "allowed_origins", "partner.example.org"]),
            _issue("creds", "credentials allowed together with a wildcard origin", "app/main.py", ["credential"]),
        ],
    },
    {
        "id": "pr_014_timing_attack", "title": "Check API keys for service accounts",
        "task_context": "Service accounts authenticate with a static API key.",
        "changes": {"app/auth.py": '''

            def verify_api_key(provided: str) -> bool:
                logger.info("checking api key %s", provided)
                return provided == settings.PAYMENTS_API_KEY
            '''},
        "expected_issues": [
            _issue("timing", "secret compared with == instead of secrets.compare_digest (timing attack)", "app/auth.py", ["timing", "compare_digest", "constant-time", "constant time"]),
            _issue("logged", "API key written to logs", "app/auth.py", ["log"]),
        ],
    },
    {
        "id": "pr_015_unvalidated_redirect", "title": "Passwordless login links",
        "task_context": "Users log in from an emailed link and are sent back to the page they came from.",
        "changes": {"app/routes/auth_routes.py": '''

            @router.get("/api/login/link")
            def login_link(token: str, next: str = "/"):
                response = RedirectResponse(next)
                response.set_cookie("session", token, httponly=True, secure=True, samesite="lax")
                return response
            '''},
        "expected_issues": [
            _issue("redirect", "open redirect: `next` not checked with is_safe_redirect", "app/routes/auth_routes.py", ["open redirect", "is_safe_redirect", "redirect"]),
            _issue("token", "login token is never verified before being set as the session", "app/routes/auth_routes.py", ["verif", "validat", "any token", "arbitrary token", "session fixation"]),
        ],
    },
]


def _materialise(pr: dict) -> Path:
    repo = EVAL_ROOT / pr["id"]
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    for rel, text in TOY_REPOSITORY.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "eval@example.com")
    git("config", "user.name", "eval")
    git("add", "-A"); git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "feature")
    for rel, snippet in pr["changes"].items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text() if path.exists() else ""
        path.write_text(existing.rstrip("\n") + "\n" + dedent(snippet).rstrip() + "\n" if existing else dedent(snippet).lstrip())
    git("add", "-A"); git("commit", "-q", "-m", pr["title"])
    return repo


def load_pr(pr_id: str) -> dict:
    pr = next(p for p in SAMPLE_PRS if p["id"] == pr_id)
    repo = _materialise(pr)
    diff = subprocess.run(["git", "-C", str(repo), "diff", "--unified=5", "main...feature"],
                          capture_output=True, text=True, check=True).stdout
    return {**pr, "repo": repo, "diff": diff}


def load_all_prs() -> list[dict]:
    return [load_pr(p["id"]) for p in SAMPLE_PRS]
