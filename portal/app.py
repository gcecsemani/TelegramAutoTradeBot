"""
portal/app.py
=============
Register, link a broker, renew the daily access token, and set risk limits.

The bot process reads the same database. Saving a kill switch here stops the
next buy; it does not wait for a restart.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import config
from brokers.registry import BROKERS
from core import clock
from core.crypto import encrypt
from execution import store
from execution.engine import CopyEngine, buy_block_reason
from portal.passwords import hash_password, verify_password

_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(_DIR, "templates"))
_SYMBOL = re.compile(r"[A-Z0-9&\-*]+")
_FAILS: dict = {}


def _session_secret() -> str:
    env = os.getenv("PORTAL_SECRET", "").strip()
    if env:
        return env
    path = os.path.join(config.STATE_DIR, "portal.secret")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read().decode().strip()
    os.makedirs(config.STATE_DIR, exist_ok=True)
    secret = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(path, "rb") as fh:
            return fh.read().decode().strip()
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secret)
    return secret


app = FastAPI(title="Candle Crusher ALGO", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    same_site="lax",
    https_only=config.PORTAL_HTTPS,
    max_age=14 * 24 * 3600,
)
app.mount("/static", StaticFiles(directory=os.path.join(_DIR, "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    store.init()


@app.get("/health")
def health() -> dict:
    return {"ok": True}


def _csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def _check_csrf(request: Request, token: str) -> Optional[str]:
    if not token or token != request.session.get("csrf"):
        return "That form expired. Reload the page and try again."
    return None


def _user(request: Request) -> Optional[dict]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return store.user_by_id(int(user_id))


def _scope(user: dict) -> Optional[int]:
    """Admin sees every account. Everyone else is limited to rows they own."""
    if user.get("role") == "admin":
        return None
    return int(user["id"])


@app.on_event("startup")
def _boot() -> None:
    store.init()
    if os.environ.get("PORTAL_DB"):
        return
    from portal.passwords import hash_password
    store.ensure_admin("admin@cca.com", hash_password("admin"), "localtest@example.com")


def _ctx(request: Request, **extra) -> dict:
    user = _user(request)
    return {"request": request, "user": user, "csrf": _csrf(request), "msg": request.query_params.get("msg", ""), **extra}


def _redirect(path: str, msg: str = "") -> RedirectResponse:
    if msg:
        join = "&" if "?" in path else "?"
        path = f"{path}{join}msg={quote(msg)}"
    return RedirectResponse(path, status_code=303)


def _present(account: dict) -> dict:
    block = buy_block_reason(account) if account["enabled"] else "disabled"
    row = dict(account)
    row["has_key"] = bool(row.pop("api_key_enc", ""))
    row["has_secret"] = bool(row.pop("api_secret_enc", ""))
    row["has_token"] = bool(row.pop("access_token_enc", ""))
    row["broker_label"] = BROKERS.get(row["broker"], {}).get("label", row["broker"])
    row["live_ready"] = bool(BROKERS.get(row["broker"], {}).get("live"))
    row["block"] = block
    row["token_today"] = row.get("token_date") == clock.today().isoformat() and row["has_token"]
    row["day_pnl"] = store.day_realized(row["id"])
    return row


def _locked(request: Request) -> bool:
    ip = request.client.host if request.client else "local"
    now = time.time()
    recent = [stamp for stamp in _FAILS.get(ip, []) if now - stamp < 600]
    _FAILS[ip] = recent
    return len(recent) >= 8


def _fail(request: Request) -> None:
    ip = request.client.host if request.client else "local"
    _FAILS.setdefault(ip, []).append(time.time())


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if _user(request):
        return _redirect("/dashboard")
    return _redirect("/login")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", _ctx(request))


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(""), password: str = Form(""), csrf: str = Form("")):
    error = _check_csrf(request, csrf)
    if error:
        return templates.TemplateResponse(request, "login.html", _ctx(request, error=error), status_code=400)
    if _locked(request):
        return templates.TemplateResponse(
            request, "login.html", _ctx(request, error="Too many tries. Wait ten minutes."), status_code=429)
    user = store.user_by_email(email)
    if user is None or not verify_password(password, user["password_hash"]):
        _fail(request)
        return templates.TemplateResponse(
            request, "login.html", _ctx(request, error="Email or password is wrong.", email=email), status_code=400)
    request.session["user_id"] = user["id"]
    return _redirect("/dashboard")


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    if _check_csrf(request, csrf):
        return _redirect("/login", "Reload and try again")
    request.session.clear()
    return _redirect("/login")


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    closed = store.count_users() > 0 and not config.PORTAL_INVITE_CODE
    return templates.TemplateResponse(request, "register.html", _ctx(request, closed=closed, needs_invite=store.count_users() > 0))


@app.post("/register", response_class=HTMLResponse)
def register(request: Request, email: str = Form(""), password: str = Form(""),
             invite: str = Form(""), csrf: str = Form("")):
    error = _check_csrf(request, csrf)
    closed = store.count_users() > 0 and not config.PORTAL_INVITE_CODE
    if error or closed:
        return templates.TemplateResponse(
            request, "register.html",
            _ctx(request, error=error or "Registration is closed.", closed=closed, needs_invite=True),
            status_code=400)
    if store.count_users() and invite.strip() != config.PORTAL_INVITE_CODE:
        return templates.TemplateResponse(
            request, "register.html",
            _ctx(request, error="Invite code is wrong.", closed=False, needs_invite=True, email=email),
            status_code=400)
    email = email.strip().lower()
    if "@" not in email or len(password) < 8:
        return templates.TemplateResponse(
            request, "register.html",
            _ctx(request, error="Use a real email and a password of at least 8 characters.",
                 closed=False, needs_invite=store.count_users() > 0, email=email),
            status_code=400)
    if store.user_by_email(email):
        return templates.TemplateResponse(
            request, "register.html",
            _ctx(request, error="That email is already registered.", closed=False,
                 needs_invite=store.count_users() > 0, email=email),
            status_code=400)
    user_id = store.create_user(email, hash_password(password))
    request.session["user_id"] = user_id
    return _redirect("/dashboard", "Account created")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = _user(request)
    if user is None:
        return _redirect("/login")
    accounts = [_present(row) for row in store.list_accounts(_scope(user))]
    return templates.TemplateResponse(request, "dashboard.html", _ctx(
        request, accounts=accounts, live_trading=config.LIVE_TRADING, brokers=BROKERS))


@app.get("/accounts/new", response_class=HTMLResponse)
def new_account_form(request: Request):
    if _user(request) is None:
        return _redirect("/login")
    return templates.TemplateResponse(request, "account_new.html", _ctx(request, brokers=BROKERS))


@app.post("/accounts/new")
def new_account(request: Request, label: str = Form(""), broker: str = Form(""),
                client_id: str = Form(""), api_key: str = Form(""), api_secret: str = Form(""),
                csrf: str = Form("")):
    user = _user(request)
    if user is None:
        return _redirect("/login")
    error = _check_csrf(request, csrf) or _account_error(label, broker, api_key, api_secret, creating=True)
    if error:
        return templates.TemplateResponse(
            request, "account_new.html",
            _ctx(request, brokers=BROKERS, error=error, label=label, broker=broker, client_id=client_id),
            status_code=400)
    account_id = store.create_account(
        user["id"], broker, label, client_id, encrypt(api_key.strip()), encrypt(api_secret.strip()))
    return _redirect(f"/accounts/{account_id}", "Broker saved. It stays on paper until you arm it.")


@app.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_page(request: Request, account_id: int):
    user = _user(request)
    if user is None:
        return _redirect("/login")
    account = store.get_account(account_id, _scope(user))
    if account is None:
        return _redirect("/dashboard", "That account was not found")
    return templates.TemplateResponse(request, "account.html", _ctx(
        request, account=_present(account), rules=store.rules_for(account_id),
        brokers=BROKERS, live_trading=config.LIVE_TRADING))


@app.post("/accounts/{account_id}")
def save_account(request: Request, account_id: int, label: str = Form(""), broker: str = Form(""),
                 client_id: str = Form(""), api_key: str = Form(""), api_secret: str = Form(""),
                 product: str = Form("MIS"), mode: str = Form("paper"), enabled: str = Form(""),
                 auto_kill: str = Form(""), flatten_on_kill: str = Form(""),
                 max_loss_trade: str = Form(""), max_loss_day: str = Form(""),
                 confirm_live: str = Form(""), csrf: str = Form("")):
    user = _user(request)
    if user is None:
        return _redirect("/login")
    account = store.get_account(account_id, _scope(user))
    if account is None:
        return _redirect("/dashboard")
    error = _check_csrf(request, csrf)
    if mode == "live" and confirm_live != "1":
        error = error or "Tick the confirmation before arming live orders."
    if product not in ("MIS", "NRML"):
        error = error or "Product must be MIS or NRML."
    if broker not in BROKERS:
        error = error or "Unknown broker."
    try:
        trade_loss = _optional_loss(max_loss_trade)
        day_loss = _optional_loss(max_loss_day)
    except ValueError:
        error = error or "Loss limits must be blank or a positive number."
    if not label.strip():
        error = error or "Label is required."
    if error:
        shown = _present(account)
        return templates.TemplateResponse(
            request, "account.html",
            _ctx(request, account=shown, rules=store.rules_for(account_id), brokers=BROKERS,
                 live_trading=config.LIVE_TRADING, error=error),
            status_code=400)
    fields = {
        "label": label.strip(), "broker": broker, "client_id": client_id.strip(),
        "product": product, "mode": "live" if mode == "live" else "paper",
        "enabled": 1 if enabled == "1" else 0,
        "auto_kill": 1 if auto_kill == "1" else 0,
        "flatten_on_kill": 1 if flatten_on_kill == "1" else 0,
        "max_loss_trade": trade_loss, "max_loss_day": day_loss,
    }
    if api_key.strip():
        fields["api_key_enc"] = encrypt(api_key.strip())
    if api_secret.strip():
        fields["api_secret_enc"] = encrypt(api_secret.strip())
    store.update_account(account_id, _scope(user), fields)
    return _redirect(f"/accounts/{account_id}", "Saved")


@app.post("/accounts/{account_id}/rules")
def save_rule(request: Request, account_id: int, symbol: str = Form("*"), lots: str = Form("1"),
              min_lots: str = Form("1"), max_lots: str = Form("1"), slippage_mode: str = Form("PERCENT"),
              slippage_value: str = Form("0"), csrf: str = Form("")):
    user = _user(request)
    if user is None or store.get_account(account_id, _scope(user)) is None:
        return _redirect("/dashboard")
    error = _check_csrf(request, csrf)
    symbol = symbol.strip().upper() or "*"
    try:
        lots_n, min_n, max_n = int(lots), int(min_lots), int(max_lots)
        slip = float(slippage_value)
    except ValueError:
        lots_n = min_n = max_n = 0
        slip = -1
    if error or not _SYMBOL.fullmatch(symbol) or min_n < 1 or max_n < min_n or not min_n <= lots_n <= max_n:
        error = error or "Lots must sit between min and max, and min must be at least 1."
    if slippage_mode not in ("PERCENT", "POINTS") or slip < 0:
        error = error or "Slippage must be PERCENT or POINTS, and not negative."
    if error:
        return _redirect(f"/accounts/{account_id}", error)
    store.upsert_rule(account_id, symbol, lots_n, min_n, max_n, slippage_mode, slip)
    return _redirect(f"/accounts/{account_id}", f"Rule saved for {symbol}")


@app.post("/accounts/{account_id}/rules/delete")
def remove_rule(request: Request, account_id: int, symbol: str = Form(""), csrf: str = Form("")):
    user = _user(request)
    if user is None or store.get_account(account_id, _scope(user)) is None:
        return _redirect("/dashboard")
    if _check_csrf(request, csrf):
        return _redirect(f"/accounts/{account_id}", "Reload and try again")
    store.delete_rule(account_id, symbol.strip().upper())
    return _redirect(f"/accounts/{account_id}", "Rule removed")


@app.post("/accounts/{account_id}/delete")
def delete_account(request: Request, account_id: int, csrf: str = Form("")):
    user = _user(request)
    if user is None or store.get_account(account_id, _scope(user)) is None:
        return _redirect("/dashboard")
    if _check_csrf(request, csrf):
        return _redirect(f"/accounts/{account_id}", "Reload and try again")
    reason = store.delete_account(account_id, _scope(user))
    if reason == "open":
        return _redirect(f"/accounts/{account_id}", "Flatten the open lots before removing this account.")
    if reason == "busy":
        return _redirect(f"/accounts/{account_id}", "An order is still being confirmed. Remove the account after it finishes.")
    if reason:
        return _redirect("/dashboard", "That account was not found")
    return _redirect("/dashboard", "Account removed")


@app.post("/accounts/{account_id}/kill")
def kill(request: Request, account_id: int, action: str = Form("on"), csrf: str = Form("")):
    user = _user(request)
    if user is None or store.get_account(account_id, _scope(user)) is None:
        return _redirect("/dashboard")
    if _check_csrf(request, csrf):
        return _redirect(f"/accounts/{account_id}", "Reload and try again")
    if action == "off":
        store.set_kill(account_id, False, "")
        return _redirect(f"/accounts/{account_id}", "Kill switch cleared")
    store.set_kill(account_id, True, "manual kill switch")
    return _redirect(f"/accounts/{account_id}", "Kill switch is on. New buys are stopped.")


@app.post("/accounts/{account_id}/flatten")
def flatten(request: Request, account_id: int, csrf: str = Form("")):
    user = _user(request)
    if user is None or store.get_account(account_id, _scope(user)) is None:
        return _redirect("/dashboard")
    if _check_csrf(request, csrf):
        return _redirect(f"/accounts/{account_id}", "Reload and try again")
    sent = CopyEngine().flatten_account(account_id)
    store.set_kill(account_id, True, "flattened from the portal")
    return _redirect(f"/accounts/{account_id}", f"Flatten sent for {sent} open leg(s). Kill switch is on.")


@app.get("/accounts/{account_id}/auth", response_class=HTMLResponse)
def auth_form(request: Request, account_id: int):
    user = _user(request)
    if user is None:
        return _redirect("/login")
    account = store.get_account(account_id, _scope(user))
    if account is None:
        return _redirect("/dashboard")
    login_url = ""
    error = ""
    if account["broker"] == "zerodha":
        try:
            login_url = _zerodha(account).login_url()
        except Exception as exc:
            error = str(exc)
    return templates.TemplateResponse(request, "auth.html", _ctx(
        request, account=_present(account), login_url=login_url, error=error))


@app.post("/accounts/{account_id}/auth", response_class=HTMLResponse)
def auth_submit(request: Request, account_id: int, request_token: str = Form(""), csrf: str = Form("")):
    user = _user(request)
    if user is None:
        return _redirect("/login")
    account = store.get_account(account_id, _scope(user))
    if account is None:
        return _redirect("/dashboard")
    error = _check_csrf(request, csrf)
    token = _request_token(request_token)
    if not error and account["broker"] != "zerodha":
        error = f"{BROKERS.get(account['broker'], {}).get('label', account['broker'])} login is not available yet."
    if not error and not token:
        error = "Paste the redirect URL or the request token."
    if error:
        return templates.TemplateResponse(
            request, "auth.html",
            _ctx(request, account=_present(account), login_url="", error=error), status_code=400)
    try:
        session = _zerodha(account).exchange(token)
    except Exception as exc:
        return templates.TemplateResponse(
            request, "auth.html",
            _ctx(request, account=_present(account), login_url="", error=str(exc)), status_code=400)
    store.set_token(account_id, encrypt(session["access_token"]), session.get("user_id") or account.get("client_id") or "")
    who = session.get("user_name") or session.get("user_id") or "your account"
    return _redirect(f"/accounts/{account_id}", f"Zerodha login saved for {who}")


@app.get("/orders", response_class=HTMLResponse)
def orders(request: Request):
    user = _user(request)
    if user is None:
        return _redirect("/login")
    return templates.TemplateResponse(request, "orders.html", _ctx(request, orders=store.recent_intents(_scope(user))))


def _account_error(label: str, broker: str, api_key: str, api_secret: str, creating: bool) -> str:
    if broker not in BROKERS:
        return "Choose a broker."
    if not label.strip():
        return "Give the account a label."
    if creating and (not api_key.strip() or not api_secret.strip()):
        return "API key and secret are required."
    return ""


def _optional_loss(raw: str) -> Optional[float]:
    raw = (raw or "").strip()
    if not raw:
        return None
    value = float(raw)
    if value < 0:
        raise ValueError(raw)
    return value


def _request_token(raw: str) -> str:
    raw = (raw or "").strip()
    match = re.search(r"request_token=([A-Za-z0-9]+)", raw)
    return match.group(1) if match else raw


def _zerodha(account: dict):
    from core.crypto import decrypt
    from brokers.zerodha import ZerodhaBroker
    return ZerodhaBroker(
        decrypt(account.get("api_key_enc") or ""),
        decrypt(account.get("api_secret_enc") or ""),
        decrypt(account.get("access_token_enc") or ""),
    )
