"""Sign-in and per-tab access for delispice_app: one owner, and everyone else is a guest.

  WHO    /login checks the owner's username and password against ``state/auth.json`` (machine-local,
         git-ignored) and marks the browser as the owner's in Flask's signed session cookie. The owner is set
         from the command line only, never from the web page:  ``python -m delispice_app.auth --help``
  WHAT   The owner opens every tab. Everyone else is a guest and opens only the guest tabs: the list in the
         file, or the standard tabs on the tab bar until you set one.
  GATE   Two places enforce it. ``prune`` leaves a closed tab's contents out of the page the browser is sent,
         and ``_gate`` refuses any Dash callback that belongs to a closed tab. The second is the real lock:
         hiding a tab stops nobody who writes the request by hand.

Which tab a callback belongs to comes from the layout, not from naming: app.py says which block of the page
is which tab (``TAB_BLOCKS``), and a callback belongs to every tab one of its components sits in.

``DELISPICE_AUTH=off`` turns all of it off; the desktop launcher does that (one person, their own machine).
One thing stays visible to everyone: /_dash-dependencies lists every callback's component ids (names such as
``usc-upload``, never data).
"""
from __future__ import annotations

import argparse
import copy
import functools
import getpass
import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import timedelta
from pathlib import Path

import flask
from dash import html
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash, generate_password_hash

ENABLED = os.environ.get("DELISPICE_AUTH", "on").strip().lower() not in ("off", "0", "false", "no")
AUTH_FILE = Path(os.environ.get("DELISPICE_AUTH_FILE") or Path(__file__).resolve().parent / "state" / "auth.json")
SECRET_FILE = AUTH_FILE.with_name("auth_secret")       # signs the session cookie; made on the first run
NO_TAB = "(no tab)"                                     # owner of a callback the layout can't place in a tab
MIN_PASSWORD = 10
MAX_FAILS, FAIL_WINDOW = 8, 600                         # wrong passwords allowed per username tried, per 10 minutes
SESSION_DAYS = 30
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,31}")

_cfg: dict = {}                                         # filled by setup()


# ── The file ─────────────────────────────────────────────────────────────────────────────────────
# {"owner": "matt", "password": "<hash>", "guest_tabs": ["about", "season"]}   (no guest_tabs = the default)
_file = {"stamp": None, "data": {}}


def load() -> dict:
    """The file, re-read only when it changes on disk, so a command-line edit needs no restart."""
    try:
        stamp = AUTH_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        return {}
    if _file["stamp"] != stamp:
        _file.update(stamp=stamp, data=json.loads(AUTH_FILE.read_text()))
    return _file["data"]


def save(data: dict) -> None:
    """Write the file whole, then swap it in, so the running app never reads half a file."""
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_suffix(".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, AUTH_FILE)


def _secret() -> str:
    """The key that signs session cookies: made once, kept beside the file, never in git."""
    try:
        return SECRET_FILE.read_text().strip()
    except FileNotFoundError:
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(secrets.token_hex(32))
        return SECRET_FILE.read_text().strip()


# ── Who is asking, and which tabs they may open ──────────────────────────────────────────────────
def _mark(pw_hash: str) -> str:
    """A fingerprint of the stored password hash. The cookie carries it, so a new password signs old browsers out."""
    return hashlib.sha256(pw_hash.encode()).hexdigest()[:16]


def current_user() -> str | None:
    """The owner's username when this browser is signed in as the owner, else None. Checked against the file on
    every request, so a new username or password signs old browsers out at once."""
    if not ENABLED or not flask.has_request_context():
        return None
    data = load()
    owner, pw_hash = data.get("owner"), data.get("password")
    if not owner or not pw_hash or flask.session.get("u") != owner:
        return None
    if not secrets.compare_digest(str(flask.session.get("k")), _mark(pw_hash)):
        return None
    return owner


def guest_tabs() -> frozenset:
    """The tabs a guest may open: the file's list, or the default from setup() when the file has none."""
    tabs = load().get("guest_tabs")
    return _cfg.get("guests", frozenset()) if tabs is None else frozenset(tabs)


def allowed_tabs() -> frozenset | None:
    """The tabs this request may open: None (every tab) for the owner or with auth off, else the guest tabs."""
    if not ENABLED or current_user():
        return None
    return guest_tabs()


def can(tabs) -> bool:
    allowed = allowed_tabs()
    return allowed is None or set(tabs) <= allowed


# ── Which tab each callback belongs to ───────────────────────────────────────────────────────────
def _flat(x) -> list:
    if isinstance(x, dict):
        x = list(x.values())
    if isinstance(x, (list, tuple)):
        return [leaf for item in x for leaf in _flat(item)]
    return [] if x is None else [x]


@functools.cache
def _plan() -> tuple[dict, frozenset]:
    """(owners, shells), worked out once from the layout.

    owners  {callback key: the tabs that callback belongs to}. Walk the full page top-down, remembering which
            tab's block we are inside: that gives every component a tab. A callback then belongs to each tab
            one of its outputs, inputs or states sits in. Two things belong to no tab: the ids in ``shared``
            (the tab bar) and a block's own ``style`` (that is how tabs show and hide).
    shells  the blocks whose empty container stays on the page even when their tab is closed, because a
            callback everyone runs (the tab show/hide) writes to it."""
    blocks, shared = _cfg["blocks"], _cfg["shared"]
    where: dict = {}

    def walk(node, tab):
        cid = getattr(node, "id", None)
        if isinstance(cid, str):
            tab = blocks.get(cid, tab)
            where[cid] = tab
        kids = getattr(node, "children", None)
        for kid in kids if isinstance(kids, (list, tuple)) else [kids]:
            if hasattr(kid, "to_plotly_json"):          # a component; strings and None carry no ids
                walk(kid, tab)

    walk(_cfg["layout"], NO_TAB)

    def tab_of(cid, prop):
        if not isinstance(cid, str) or cid.startswith("{"):
            return None                                 # a pattern-matching id: only on components a callback makes
        if cid in shared or (cid in blocks and prop == "style"):
            return ""
        return where.get(cid)                           # None: made by a callback, so not in the static page

    owners = {}
    for key, cb in _cfg["app"].callback_map.items():
        deps = [(o.component_id, o.component_property) for o in _flat(cb.get("output"))]
        deps += [(d["id"], d["property"]) for d in cb["inputs"] + cb["state"]]
        found = {tab_of(cid, prop) for cid, prop in deps}
        tabs = found - {None, ""}
        if not tabs and None in found:                  # nothing but made-later components: can't place it, so lock it
            tabs = {NO_TAB}
        owners[key] = frozenset(tabs)
    if loose := sorted(key for key, tabs in owners.items() if NO_TAB in tabs):
        print(f"[delispice] auth: {len(loose)} callback(s) sit outside every tab block, so only the owner can "
              f"run them: {loose}", flush=True)
    if unknown := sorted(guest_tabs() - set(blocks.values())):
        print(f"[delispice] auth: the guest tabs name tabs this machine does not have: {unknown}", flush=True)
    open_outputs = {o.component_id for key, cb in _cfg["app"].callback_map.items() if not owners[key]
                    for o in _flat(cb.get("output")) if isinstance(o.component_id, str)}
    return owners, frozenset(cid for cid in blocks if cid in open_outputs)


def prune(node, allowed, swap=None):
    """A copy of the page without the blocks ``allowed`` doesn't cover, and with the components in ``swap``
    {id: replacement} exchanged. A closed block is dropped whole, unless it is a shell (see _plan): then its
    empty, hidden container stays. The original tree is never changed."""
    blocks, swap, shells, gone = _cfg["blocks"], swap or {}, _plan()[1], object()

    def go(n):
        cid = getattr(n, "id", None)
        if isinstance(cid, str):
            if cid in swap:
                return swap[cid]
            if allowed is not None and cid in blocks and blocks[cid] not in allowed:
                return html.Div(id=cid, style={"display": "none"}) if cid in shells else gone
        kids = getattr(n, "children", None)
        many = isinstance(kids, (list, tuple))
        old = list(kids) if many else [kids]
        new = [go(k) if hasattr(k, "to_plotly_json") else k for k in old]
        if any(a is not b for a, b in zip(new, old)):
            new = [k for k in new if k is not gone]
            n = copy.copy(n)
            n.children = new if many else (new[0] if new else None)
        return n

    return go(node)


def badge(style=None):
    """The small sign-in line across the top of the page, with ``style`` laid over its own. None when auth is off."""
    if not ENABLED:
        return None
    link = {"color": "#8a1520", "textDecoration": "none"}
    user = current_user()
    line = ([f"Signed in as {user} · ", html.A("Sign out", href="/logout", style=link)] if user
            else [html.A("Sign in", href="/login", style=link)])
    return html.Div(line, style={"textAlign": "right", "padding": "4px 16px 0", "fontSize": "12px", "color": "#666",
                                 **(style or {})})


# ── The Flask side: session cookie, /login, /logout, and the gate in front of every callback ─────
class _Session(SecureCookieSessionInterface):
    """Marks the cookie HTTPS-only when the visitor came in over HTTPS (the public site, through Cloudflare),
    and leaves it usable over plain HTTP on localhost."""

    def get_cookie_secure(self, app):
        req = flask.request
        return (req.is_secure or req.headers.get("X-Forwarded-Proto") == "https"
                or '"https"' in req.headers.get("CF-Visitor", ""))


_fails: dict[str, list[float]] = {}                     # username tried -> times of recent wrong passwords


def _recent_fails(name: str) -> list[float]:
    now = time.time()
    return [t for t in _fails.get(name, []) if now - t < FAIL_WINDOW]


@functools.cache
def _decoy() -> str:
    """A hash nobody's password matches: checking it makes a wrong username take as long as the right one."""
    return generate_password_hash(secrets.token_hex(16))


_LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · delispice</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #faf7f7; margin: 0; }
  form { width: 280px; margin: 14vh auto 0; padding: 24px 26px; background: #fff; border: 1px solid #e2c9cc;
         border-radius: 8px; }
  h1 { font-size: 18px; margin: 0 0 16px; color: #8a1520; }
  label { display: block; font-size: 13px; font-weight: 600; margin: 12px 0 4px; }
  input { width: 100%; box-sizing: border-box; padding: 8px; font-size: 16px; border: 1px solid #bbb; border-radius: 4px; }
  button { margin-top: 18px; width: 100%; padding: 9px; font-size: 14px; color: #fff; background: #8a1520; border: 0;
           border-radius: 4px; cursor: pointer; }
  .error { color: #8a1520; font-size: 13px; margin: 0 0 4px; }
  a { display: block; margin-top: 14px; font-size: 12px; color: #666; text-align: center; }
</style></head>
<body><form method="post" action="/login">
  <h1>Sign in</h1>
  {% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}
  <label for="username">Username</label>
  <input id="username" name="username" value="{{ name }}" autocomplete="username" autocapitalize="none" required autofocus>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Sign in</button>
  <a href="/">Back to the site</a>
</form></body></html>"""


def _login():
    name, error = "", None
    if flask.request.method == "POST":
        name = flask.request.form.get("username", "").strip().lower()[:64]
        password = flask.request.form.get("password", "")
        data = load()
        owner, pw_hash = data.get("owner"), data.get("password")
        is_owner = bool(owner and pw_hash) and secrets.compare_digest(name.encode(), owner.encode())
        if len(_recent_fails(name)) >= MAX_FAILS:
            error = "Too many tries. Wait a few minutes and try again."
        elif check_password_hash(pw_hash if is_owner else _decoy(), password) and is_owner:
            _fails.pop(name, None)
            flask.session.clear()
            flask.session.permanent = True              # keep the cookie for SESSION_DAYS, not just this browser window
            flask.session.update(u=owner, k=_mark(pw_hash))
            return flask.redirect("/")
        else:
            if len(_fails) > 5000:                      # a flood of made-up usernames must not grow this forever
                _fails.clear()
            _fails[name] = _recent_fails(name) + [time.time()]
            error = "Wrong username or password."
    return flask.render_template_string(_LOGIN_PAGE, name=name, error=error), (401 if error else 200)


def _logout():
    flask.session.clear()
    return flask.redirect("/")


def _gate():
    """Before every request: refuse a callback from a closed tab, and send a guest with no tabs to /login."""
    req = flask.request
    if req.path.endswith("/_dash-update-component"):
        key = (req.get_json(silent=True) or {}).get("output")
        need = _plan()[0].get(key)
        if need is None and key in _cfg["app"].callback_map:    # a callback registered after the map was built
            _plan.cache_clear()
            need = _plan()[0].get(key)
        if need and not can(need):
            return flask.jsonify(error="You don't have access to this tab."), 403
    elif (req.method == "GET" and not req.path.startswith(("/_", "/assets/", "/static/", "/login", "/logout"))
          and allowed_tabs() == frozenset()):
        return flask.redirect("/login")
    return None


def _no_store(response):
    """The page now differs per visitor, so nothing in between may keep a copy of it."""
    if flask.request.path.endswith(("/_dash-layout", "/login")):
        response.headers["Cache-Control"] = "no-store"
    return response


def setup(dash_app, *, layout, blocks: dict, shared=(), guests=(), tabs=(), retired=()) -> None:
    """Hook auth into the app. ``layout`` is the full page, ``blocks`` {block id: tab} says which part of it is
    which tab, ``shared`` are the ids that belong to no tab, ``guests`` the tabs a guest gets when the file
    doesn't say, and ``tabs`` [(label, value)] (with ``retired``, the ones off the tab bar) is every tab, for
    the command line."""
    _cfg.update(app=dash_app, layout=layout, blocks=dict(blocks), shared=frozenset(shared),
                guests=frozenset(guests), tabs=list(tabs), retired=frozenset(retired))
    _plan.cache_clear()
    if not ENABLED:
        return
    server = dash_app.server
    server.secret_key = _secret()
    server.session_interface = _Session()
    server.config.update(SESSION_COOKIE_NAME="delispice", SESSION_COOKIE_HTTPONLY=True,
                         SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS))
    server.add_url_rule("/login", "auth_login", _login, methods=["GET", "POST"])
    server.add_url_rule("/logout", "auth_logout", _logout)
    server.before_request(_gate)
    server.after_request(_no_store)


# ── Command line: python -m delispice_app.auth <command> ─────────────────────────────────────────
def _app_cfg() -> dict:
    """The app's tab settings, read by importing the app itself (takes a couple of seconds)."""
    import delispice_app.app                            # noqa: F401  (its import calls setup() on the real module)
    return sys.modules["delispice_app.auth"]._cfg


def _new_password() -> str:
    password = getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD:
        sys.exit(f"Use at least {MIN_PASSWORD} characters. Nothing was changed.")
    if password != getpass.getpass("Repeat  : "):
        sys.exit("The two didn't match. Nothing was changed.")
    return generate_password_hash(password)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m delispice_app.auth",
                                description="The site's sign-in: one owner who sees every tab, and the tabs "
                                            "guests see. Kept in delispice_app/state/auth.json.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("owner", help="set the owner's username and password (asks for the password; "
                                 "signs every browser out)").add_argument("name")
    g = sub.add_parser("guests", help="set the tabs guests see, e.g.  guests about season game lab")
    g.add_argument("tabs", nargs="*")
    which = g.add_mutually_exclusive_group()
    which.add_argument("--none", action="store_true", help="guests see nothing: the whole site needs a sign-in")
    which.add_argument("--default", action="store_true", help="back to the standard tabs on the tab bar")
    sub.add_parser("show", help="show the owner, every tab, and what guests see")
    args = p.parse_args(argv)
    data = dict(load())                                 # a copy to edit

    if args.cmd == "owner":
        name = args.name.lower()
        if not _NAME.fullmatch(name):
            sys.exit("A username is 2-32 characters: lowercase letters, digits, '.', '_' or '-'.")
        data.update(owner=name, password=_new_password())
        print(f'The owner is now "{name}". Any browser signed in before is signed out.')
    elif args.cmd == "guests":
        if bool(args.tabs) == (args.none or args.default):
            sys.exit("Name the tabs guests see, or use --none or --default (not both).")
        if args.default:
            data.pop("guest_tabs", None)
            print("Guests see the standard tabs again.")
        else:
            known = [value for _, value in _app_cfg()["tabs"]]
            if unknown := [t for t in args.tabs if t not in known]:
                sys.exit(f'Unknown tab(s): {", ".join(unknown)}. Tabs here: {", ".join(known)}.')
            data["guest_tabs"] = sorted(set(args.tabs), key=known.index)
            print("Guests see: " + (", ".join(data["guest_tabs"]) or "nothing. The whole site needs a sign-in."))
    else:
        cfg = _app_cfg()
        chosen = data.get("guest_tabs")
        guests = chosen if chosen is not None else [v for _, v in cfg["tabs"] if v in cfg["guests"]]
        print("Owner:  " + (data.get("owner") or "not set yet, so nobody can sign in"))
        print("Tabs:   " + ", ".join(f"{value} ({label}{', retired' if value in cfg['retired'] else ''})"
                                     for label, value in cfg["tabs"]))
        print("Guests: " + (", ".join(guests) or "nothing (the whole site needs a sign-in)")
              + ("" if chosen is not None else "   [default]"))
        return
    save(data)


if __name__ == "__main__":
    main()
