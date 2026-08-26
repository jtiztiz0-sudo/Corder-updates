"""
The web app. Flask serves it; desktop.py puts it in a window.

There is no per-tab code here. Every tab is the same three routes driven by
modules.py, which is why adding a tab is a data change and not a coding job.

Safety note: table and column names are interpolated into SQL (SQLite can't
parameterise identifiers), so they are ALWAYS taken from the module spec --
never from the request. A field name off the wire is matched against that
spec first and rejected if it isn't there. Values are parameterised normally.
"""

from __future__ import annotations

import os
import sys
import json
import time
import secrets
import hashlib
import threading
from datetime import date, datetime, timedelta

from flask import (Flask, render_template, request, abort, Response,
                   redirect, url_for)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db
import modules
import diagnostics

app = Flask(__name__)
app.secret_key = "local-desktop-app"
# desktop.py runs without Flask's reloader (it would spawn a second process
# and break the window), so template edits need this to show up on refresh.
app.config["TEMPLATES_AUTO_RELOAD"] = True

db.init_db()


# Nothing here has a console, so an unhandled error would otherwise vanish
# without trace and leave "it stopped working" as the only evidence.
@app.errorhandler(Exception)
def _record_and_reraise(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc                      # a 404 is not a fault worth logging
    diagnostics.log_exception(exc, request.path if request else "")
    return render_template("error.html", ref=datetime.now().strftime("%d %b %H:%M")), 500


def _installed_version() -> str:
    """Engine and config version, for the line below."""
    def read(name):
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   name), encoding="utf-8") as fh:
                return fh.read().strip() or "0"
        except OSError:
            return "0"
    return "%s (config %s)" % (read("version.txt"), read("app_version.txt"))


# One line every time it opens, so "has she actually run it since I sent that
# fix?" has an answer -- and a crash on startup shows up as a start with
# nothing after it.
diagnostics.log("start", "", "version %s" % _installed_version())


@app.route("/problem-report", methods=["POST"])
def problem_report():
    """Zip the logs onto their Desktop so they can email it over. Their
    records are not in it -- see diagnostics.export."""
    try:
        return {"ok": True, "file": diagnostics.export()}
    except Exception as exc:
        diagnostics.log_exception(exc, "/problem-report")
        return {"ok": False, "error": str(exc)}, 500


@app.template_filter("shortdate")
def _shortdate(value):
    """2026-08-05 16:04 -> "5 Aug". A different year keeps it: "5 Aug 2025".

    Two reasons this isn't the raw date. It reads like a person wrote it rather
    than a database, and it has no hyphens -- an ISO date breaks across two
    lines at its hyphens when a column gets tight, which looked broken.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        d = datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return text[:10]
    if d.year == datetime.now().year:
        return "%d %s" % (d.day, d.strftime("%b"))
    return "%d %s %d" % (d.day, d.strftime("%b"), d.year)


def _which_copy() -> str:
    """Which copy of this app is running -- from its own folder.

    "customer"  a real install (theirs, or a delivered one)
    "master"    the copy CorderHome keeps and reads the wishlist from
    "test"      the throwaway sandbox used to try a fix before sending it

    Shown as a small dot in the corner. Deliberately quiet: on a customer's
    machine it is the same colour as the background and reads as nothing at
    all, so it never invites a question.
    """
    here = os.path.dirname(os.path.abspath(__file__)).lower()
    parts = [p for p in here.split(os.sep) if p]
    if "sandbox" in parts:
        return "test"
    if "generated" in parts:
        return "master"
    return "customer"


COPY_KIND = _which_copy()


# --------------------------------------------------------------------------
# whether this app is allowed to be used
# --------------------------------------------------------------------------
# The rules this follows, in order of how much damage getting them wrong would
# do:
#
# 1. IT FAILS OPEN. No internet, the site down, the answer unreadable -- the
#    app runs. A licence check that fails CLOSED means one bad afternoon on my
#    server stops every customer trading, which is a far worse outcome than
#    somebody using an app they haven't paid for for another day.
# 2. IT NEVER TOUCHES THEIR RECORDS. A lock hides the pages; the database is
#    untouched, so unlocking puts everything back exactly as it was.
# 3. THE LAST ANSWER IS REMEMBERED, so an app that is locked cannot be
#    unlocked simply by pulling the network cable -- but see 1: an answer that
#    was never received is not a lock.
# 4. It is honest about what it is: this stops an ordinary person using the
#    app. Anyone who can read Python can get round it. It is a lock on a door,
#    not a safe.
def _ssl_ctx():
    """Certificates for https.

    The bundled Python has no certificate authorities of its own, and Python
    on Windows does not fall back to the Windows store -- so without this
    every https call fails with "unable to get local issuer certificate" and,
    because these calls are all best-effort, fails SILENTLY. That one gap
    stopped updates, the wishlist and the licence check on every delivered
    copy at once.

    certifi ships in the runtime. If it is somehow missing, fall back to the
    default context rather than turning verification off -- an app that
    quietly stops checking who it is talking to is worse than one that cannot
    reach me.
    """
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


LICENCE_FILE = os.path.join(HERE, "data", "licence.json")
_licence = {"locked": False, "message": ""}


def local_version() -> int:
    """Which engine this copy is running -- the first thing worth knowing when
    something works here and not there."""
    try:
        with open(os.path.join(HERE, "version.txt"), encoding="utf-8") as fh:
            return int((fh.read() or "0").strip() or 0)
    except Exception:
        return 0


def _load_licence() -> None:
    global _licence
    try:
        with open(LICENCE_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        _licence = {"locked": bool(saved.get("locked")),
                    "message": str(saved.get("message") or "")}
    except Exception:
        _licence = {"locked": False, "message": ""}       # rule 1


def _check_licence() -> None:
    """Ask the site, on a background thread, once per launch."""
    base = (getattr(modules, "BUSINESS", {}) or {}).get("wish_url") or ""
    uid = (getattr(modules, "BUSINESS", {}) or {}).get("id") or ""
    if not (base and uid):
        return
    url = base.rsplit("/", 1)[0] + "/licence/" + uid
    local = url.startswith(("http://127.0.0.1", "http://localhost"))
    if not (url.startswith("https://") or local):
        return

    def go():
        import urllib.request as _u
        try:
            req = _u.Request(url, headers={"User-Agent": "jts-app"})
            with _u.urlopen(req, timeout=10, context=_ssl_ctx()) as r:
                answer = json.loads(r.read(4000).decode("utf-8"))
        except Exception as exc:
            diagnostics.log("licence", "", "could not check: %s" % exc)
            return                                        # rule 1
        if not answer.get("ok"):
            return
        state = {"locked": bool(answer.get("locked")),
                 "message": str(answer.get("message") or "")}
        global _licence
        _licence = state
        try:
            os.makedirs(os.path.dirname(LICENCE_FILE), exist_ok=True)
            with open(LICENCE_FILE, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
        except OSError:
            pass

    threading.Thread(target=go, daemon=True).start()


def _flush_wishes() -> None:
    """Send anything the site might not have yet, every time the app opens.

    _send_wish is fire-and-forget on a daemon thread: if there is no internet
    when they press it, or the site blinks, or they close the app before the
    thread finishes, the request is saved HERE and never sent again. They see
    it in their list and reasonably assume it was received. It never was.

    So on every launch the still-open requests go up again. That is safe to do
    blindly because the site refuses duplicates on (app_uid, local_id) -- a
    request it already has is ignored, not doubled.
    """
    base = (getattr(modules, "BUSINESS", {}) or {}).get("wish_url") or ""
    uid = (getattr(modules, "BUSINESS", {}) or {}).get("id") or ""
    m = modules.by_key("wishlist")
    if not (base and uid and m):
        return
    local = base.startswith(("http://127.0.0.1", "http://localhost"))
    if not (base.startswith("https://") or local):
        return

    def go():
        import json as _json
        import urllib.request as _u
        arch = m.get("archive") or {}
        status = m.get("status_field") or "status"
        done = arch.get("done_value") or "Done"
        try:
            rows = db.query(
                "SELECT id, request FROM %s WHERE COALESCE(%s,'') != ? "
                "ORDER BY id DESC LIMIT 50" % (m["table"], status), (done,))
        except Exception:
            return
        for r in rows:
            text = (r["request"] or "").strip()
            if not text:
                continue
            body = _json.dumps({"app_uid": uid, "local_id": int(r["id"]),
                                "text": text}).encode("utf-8")
            req = _u.Request(base, data=body, method="POST",
                             headers={"Content-Type": "application/json",
                                      "User-Agent": "jts-app"})
            try:
                with _u.urlopen(req, timeout=10, context=_ssl_ctx()) as resp:
                    resp.read(200)
            except Exception:
                return          # still no connection; try again next launch

    threading.Thread(target=go, daemon=True).start()


def _sync_fixed_requests() -> None:
    """Tick off the requests that have since been dealt with.

    Their app posts a request UP; the fix comes DOWN as an update. Without
    this last step nothing ever tells THEIR copy the request was answered, so
    it reads PENDING for ever on their screen while it says Done on mine --
    which looks exactly like being ignored.

    Only ever marks things DONE, never the other way, and only for rows this
    app itself created.
    """
    base = (getattr(modules, "BUSINESS", {}) or {}).get("wish_url") or ""
    uid = (getattr(modules, "BUSINESS", {}) or {}).get("id") or ""
    if not (base and uid and modules.by_key("wishlist")):
        return
    url = base.rsplit("/", 1)[0] + "/wishes/fixed/" + uid
    local = url.startswith(("http://127.0.0.1", "http://localhost"))
    if not (url.startswith("https://") or local):
        return

    def go():
        import urllib.request as _u
        try:
            req = _u.Request(url, headers={"User-Agent": "jts-app"})
            with _u.urlopen(req, timeout=10, context=_ssl_ctx()) as r:
                ids = (json.loads(r.read(20000).decode("utf-8")) or {}).get("ids") or []
        except Exception as exc:
            diagnostics.log("wish", "", "could not check fixed: %s" % exc)
            return
        ids = [int(i) for i in ids if str(i).isdigit()][:500]
        if not ids:
            return
        try:
            m = modules.by_key("wishlist")
            arch = m.get("archive") or {}
            status = m.get("status_field") or "status"
            done = arch.get("done_value") or "Done"
            stamp = arch.get("stamp_field") or "fixed_at"
            rows = db.query(
                "SELECT id FROM %s WHERE id IN (%s) AND %s IS NOT ?"
                % (m["table"], ",".join("?" * len(ids)), status),
                tuple(ids) + (done,))
            for r in rows:
                db.update(m["table"], r["id"],
                          **{status: done,
                             stamp: datetime.now().strftime("%Y-%m-%d %H:%M")})
        except Exception as exc:
            diagnostics.log("wish", "", "could not tick off: %s" % exc)

    threading.Thread(target=go, daemon=True).start()


_load_licence()
_check_licence()
_sync_fixed_requests()
_flush_wishes()


@app.before_request
def _guard_licence():
    # static stays served, or the lock screen comes out unstyled. system_reload
    # stays allowed on purpose: once it is lifted they can press the button on
    # the lock screen instead of having to know to close and reopen the app.
    if request.endpoint in ("static", "system_reload"):
        return None
    if _licence["locked"]:
        return render_template("locked.html", message=_licence["message"]), 403
    return None


# --------------------------------------------------------------------------
# signing in (hosted copies only)
# --------------------------------------------------------------------------
# A copy running on someone's own PC needs no password: it listens on that
# machine only, so anyone who could reach it is already sitting at it. Asking
# for a password there would be pure friction.
#
# A HOSTED copy is on the internet, where the address is the only thing
# standing between a stranger and the books. So: the password is what switches
# this on. Set APP_PASSWORD in the host's secrets and every page needs signing
# in; leave it unset and nothing below does anything at all.
#
# The password lives in the host's environment and NEVER in a file -- not in
# modules.py, not in the delivery zip, not in the repo. Nothing that ships can
# contain it.
LOGIN_PASSWORD = (os.environ.get("APP_PASSWORD") or "").strip()
SESSION_COOKIE = "jts_session"
SESSION_DAYS = 365
MAX_TRIES = 8              # per address, per quarter of an hour
TRY_WINDOW_MIN = 15


def hosted() -> bool:
    """Is this copy on the internet rather than on their own machine?"""
    return bool(LOGIN_PASSWORD)


def _fresh_token() -> str:
    return secrets.token_urlsafe(32)


def _device_label() -> str:
    """A rough name for the device, so a list of signed-in things is readable.
    Guessed from the browser's own description -- never anything identifying."""
    ua = (request.headers.get("User-Agent") or "").lower()
    if "iphone" in ua or "android" in ua or "mobile" in ua:
        kind = "Phone"
    elif "ipad" in ua or "tablet" in ua:
        kind = "Tablet"
    elif "mac" in ua:
        kind = "Mac"
    elif "windows" in ua:
        kind = "Windows PC"
    else:
        kind = "Device"
    return kind


# THE PASSWORD ITSELF. APP_PASSWORD is only the FIRST one -- what JT hands over
# on the day. The moment she sets her own it is stored here, hashed, and hers
# wins from then on. It is her business; she should not have to ring anybody to
# change who can get in.
#
# Stored as a salted PBKDF2 hash, so what sits in the database is not the
# password and cannot be read back out of a backup.
#
# IF SHE FORGETS IT: delete the `password_hash` row from app_state on the
# server and it falls back to APP_PASSWORD again. That is the reset, and it
# needs someone with access to the server -- i.e. JT.
PW_ROUNDS = 200_000


def _hash_password(plain: str, salt: bytes = b"") -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, PW_ROUNDS)
    return "%s$%s" % (salt.hex(), dk.hex())


def _stored_hash() -> str:
    row = db.query_one("SELECT value FROM app_state WHERE key = 'password_hash'")
    return (row["value"] if row else "") or ""


def _password_ok(typed: str) -> bool:
    stored = _stored_hash()
    if stored and "$" in stored:
        salt_hex, want = stored.split("$", 1)
        try:
            got = _hash_password(typed, bytes.fromhex(salt_hex)).split("$", 1)[1]
        except ValueError:
            return False
        return secrets.compare_digest(got, want)
    # never set one of her own -- the one it was handed over with
    return bool(LOGIN_PASSWORD) and secrets.compare_digest(typed, LOGIN_PASSWORD)


def _set_password(plain: str) -> None:
    db.write("INSERT INTO app_state (key, value) VALUES ('password_hash', ?) "
             "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
             (_hash_password(plain),))


def _signed_in() -> bool:
    token = request.cookies.get(SESSION_COOKIE) or ""
    if not token:
        return False
    row = db.query_one("SELECT token FROM sessions WHERE token = ?", (token,))
    if not row:
        return False
    # Touched on every visit, so the year runs from LAST USE rather than from
    # the day they signed in -- somebody who opens it weekly is never asked
    # again, which is the whole point.
    try:
        db.write("UPDATE sessions SET last_seen = ? WHERE token = ?",
                 (datetime.now().strftime("%Y-%m-%d %H:%M"), token))
    except Exception:
        pass
    return True


def _too_many_tries() -> bool:
    since = (datetime.now() - timedelta(minutes=TRY_WINDOW_MIN)).strftime(
        "%Y-%m-%d %H:%M")
    row = db.query_one("SELECT COUNT(*) n FROM login_attempts "
                       "WHERE ip = ? AND at > ?",
                       (request.remote_addr or "?", since))
    return bool(row and row["n"] >= MAX_TRIES)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not hosted():
        return redirect(url_for("dashboard"))
    if request.method == "GET":
        if _signed_in():
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="")

    if _too_many_tries():
        return render_template("login.html",
                               error="Too many tries. Wait a few minutes."), 429
    typed = (request.form.get("password") or "")
    if not _password_ok(typed):
        db.insert("login_attempts", ip=request.remote_addr or "?",
                  at=datetime.now().strftime("%Y-%m-%d %H:%M"))
        return render_template("login.html",
                               error="That password isn't right."), 401

    token = _fresh_token()
    db.insert("sessions", token=token, label=_device_label(),
              created_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
              last_seen=datetime.now().strftime("%Y-%m-%d %H:%M"))
    resp = redirect(url_for("dashboard"))
    # Set by the SERVER, not by script. Safari bins script-created storage
    # after a couple of weeks of not being opened, so a cookie written in
    # JavaScript would quietly log her out every so often and look broken.
    resp.set_cookie(SESSION_COOKIE, token,
                    max_age=SESSION_DAYS * 24 * 3600,
                    httponly=True, samesite="Lax",
                    secure=request.url.startswith("https://"))
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    token = request.cookies.get(SESSION_COOKIE) or ""
    if token:
        db.write("DELETE FROM sessions WHERE token = ?", (token,))
    resp = redirect(url_for("login") if hosted() else url_for("dashboard"))
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.route("/password", methods=["POST"])
def change_password():
    """She sets her own. Needs the current one, so somebody who wandered up to
    an unlocked screen still can't lock her out of her own books."""
    if not hosted():
        return {"ok": False, "error": "no password on this copy"}, 400
    if not _signed_in():
        return {"ok": False, "error": "sign in first"}, 403
    payload = request.get_json(silent=True) or {}
    now = payload.get("current") or ""
    new = (payload.get("new") or "").strip()
    if not _password_ok(now):
        return {"ok": False, "error": "that isn't the current password"}, 400
    if len(new) < 6:
        return {"ok": False, "error": "make it at least 6 characters"}, 400
    _set_password(new)

    # Everything ELSE gets signed out. Changing the password is what you do
    # when somebody shouldn't be getting in any more, so leaving their phone
    # signed in would make it pointless. This device stays in, so she isn't
    # thrown out of the screen she's looking at.
    mine = request.cookies.get(SESSION_COOKIE) or ""
    others = db.write("DELETE FROM sessions WHERE token != ?", (mine,))
    return {"ok": True, "signed_out": others}


@app.route("/devices", methods=["GET", "POST"])
def devices():
    """What is signed in, and a way to boot all of it off -- for a lost phone."""
    if not hosted():
        return {"ok": True, "devices": []}
    if not _signed_in():
        return {"ok": False, "error": "sign in first"}, 403
    if request.method == "POST":
        mine = request.cookies.get(SESSION_COOKIE) or ""
        n = db.write("DELETE FROM sessions WHERE token != ?", (mine,))
        return {"ok": True, "signed_out": n}
    mine = request.cookies.get(SESSION_COOKIE) or ""
    rows = db.query("SELECT token, label, last_seen FROM sessions "
                    "ORDER BY last_seen DESC")
    return {"ok": True, "devices": [
        {"label": r["label"], "last_seen": r["last_seen"],
         "this_one": r["token"] == mine} for r in rows]}


@app.before_request
def _guard_login():
    if not hosted():
        return None                    # their own PC: nothing to sign in to
    if request.endpoint in ("static", "login", "logout"):
        return None
    if _signed_in():
        return None
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# removing the app from this computer
# --------------------------------------------------------------------------
# Deliberately awkward to reach and impossible to do by accident: it is at the
# bottom of a page nothing links to prominently, and it will not run until the
# business name has been typed out in full.
#
# The rule it is built around: THEIR RECORDS ARE NOT MINE TO DELETE. Everything
# in data/ is copied to their Desktop BEFORE anything is removed, and they are
# told where it went. Uninstalling the program must never be the same act as
# losing the work.
def _install_home() -> str:
    """The folder this copy actually lives in, if it is an installed one."""
    return HERE


def _backup_dir() -> str:
    name = "%s backup %s" % (modules.BUSINESS.get("name") or "app",
                             datetime.now().strftime("%d %b %Y"))
    for base in (os.path.join(os.path.expanduser("~"), "Desktop"),
                 os.path.expanduser("~")):
        if os.path.isdir(base):
            # built with no escapes at all: this string has to survive
            # appgen's quoting AND the generated file's, and a backslash
            # written here arrives mangled at the other end
            bad = set('/:*?"<>|') | {chr(92)}
            return os.path.join(base, "".join(c for c in name if c not in bad))
    return os.path.join(os.path.expanduser("~"), "app backup")


@app.route("/connection-check", methods=["POST"])
def connection_check():
    """"Is this app able to reach JTS, and are any of my requests stuck?"

    Built because "it isn't showing up on my end" is impossible to diagnose
    from the other end of a phone. This answers it ON the machine that has the
    problem: it tries the connection for real, says what went wrong in the
    app's own words if it failed, and pushes anything stuck while it is there.
    """
    import urllib.request as _u
    base = (getattr(modules, "BUSINESS", {}) or {}).get("wish_url") or ""
    uid = (getattr(modules, "BUSINESS", {}) or {}).get("id") or ""
    if not (base and uid):
        return {"ok": False, "reachable": False,
                "error": "this copy wasn't set up to send anything"}

    waiting = 0
    m = modules.by_key("wishlist")
    if m:
        arch = m.get("archive") or {}
        status = m.get("status_field") or "status"
        done = arch.get("done_value") or "Done"
        try:
            waiting = len(db.query(
                "SELECT id FROM %s WHERE COALESCE(%s,'') != ?"
                % (m["table"], status), (done,)))
        except Exception:
            waiting = 0

    url = base.rsplit("/", 1)[0] + "/licence/" + uid
    try:
        req = _u.Request(url, headers={"User-Agent": "jts-app"})
        with _u.urlopen(req, timeout=12, context=_ssl_ctx()) as r:
            r.read(400)
    except Exception as exc:
        diagnostics.log("connection", "", "check failed: %s" % exc)
        return {"ok": True, "reachable": False, "waiting": waiting,
                "error": str(exc)[:160]}

    _flush_wishes()                       # it's reachable -- push anything stuck
    return {"ok": True, "reachable": True, "waiting": waiting,
            "version": local_version()}


# ---- how this app is painted -------------------------------------------
# Every palette, so the app can repaint itself with nothing to fetch and no
# code generation on their machine. Injected from the generator's own THEMES.
PALETTES = {'default': {'dark': {'--bg': '#0f1a2e',
                      '--blue': '#5aa9ff',
                      '--ink': '#e4ecf7',
                      '--line': '#27405f',
                      '--muted': '#93a8c4',
                      '--shadow': 'none',
                      '--surface': '#16243d',
                      '--surface-2': '#1d2f4d'},
             'label': 'Blue (default)',
             'swatch': '#1565c0',
             'vars': {'--bg': '#eef4fc',
                      '--blue': '#1565c0',
                      '--ink': '#16233a',
                      '--line': '#cfdcee',
                      '--muted': '#5d7292',
                      '--surface': '#ffffff',
                      '--surface-2': '#e3ecf9'}},
 'green': {'dark': {},
           'label': 'Green',
           'swatch': '#1f5c3d',
           'vars': {'--bg': '#0f2318',
                    '--blue': '#4fc98a',
                    '--green': '#5fd68f',
                    '--ink': '#e7f2ea',
                    '--line': '#28553c',
                    '--muted': '#9dbfab',
                    '--shadow': 'none',
                    '--surface': '#163024',
                    '--surface-2': '#1d3f2f'}},
 'mono': {'dark': {},
          'label': 'Grey',
          'swatch': '#5b6470',
          'vars': {'--bg': '#f2f3f5',
                   '--blue': '#4a5568',
                   '--ink': '#23272c',
                   '--line': '#d6d9dd',
                   '--muted': '#6b7280',
                   '--surface': '#ffffff',
                   '--surface-2': '#e8eaed'}},
 'pink': {'dark': {},
          'label': 'Pink',
          'swatch': '#e5559a',
          'vars': {'--bg': '#fff0f6',
                   '--blue': '#c9266f',
                   '--green': '#2e7d5b',
                   '--ink': '#3a1d2b',
                   '--line': '#f3c2d8',
                   '--muted': '#8b5f74',
                   '--surface': '#ffffff',
                   '--surface-2': '#ffe1ee'}},
 'warm': {'dark': {},
          'label': 'Warm',
          'swatch': '#b5651d',
          'vars': {'--bg': '#fdf6ee',
                   '--blue': '#b5651d',
                   '--ink': '#3b2a1a',
                   '--line': '#e6d2ba',
                   '--muted': '#8a6f56',
                   '--surface': '#ffffff',
                   '--surface-2': '#f6e9da'}}}

# Real need, this one: a man in a truck with gloves on, and an owner who is 58
# and squinting, want opposite things from the same screen.
DENSITIES = {"normal": "Normal", "compact": "Compact", "large": "Big text"}

# Three ways to arrange the same screen. CSS only, over ONE set of markup --
# separate templates per layout would mean every feature built from here on
# has to be built three times.
LAYOUTS = {"top": "Menu across the top",
           "side": "Menu down the side",
           "tiles": "Big tiles"}


def _look() -> dict:
    """The palette and text size in use right now.

    Their choice normally wins. It is cleared whenever JTS sends the app out
    again -- modules.py is rewritten every time, so BUSINESS['generated']
    moves, and that is the signal. Deliberate: the look JTS sets on a delivery
    is the one they get. A one-time note tells them it happened, so it can
    never look like the app changed colour by itself.

    Never raises -- this runs on every page, and a settings read must not be
    able to stop one rendering.
    """
    # same accessor the rest of this file uses -- modules is imported, the
    # names inside it are not
    biz = getattr(modules, "BUSINESS", {}) or {}
    key = biz.get("theme_key") or "default"
    density, note = "normal", False
    layout = "top"
    logo = bool(biz.get("logo"))
    try:
        stamp = str(biz.get("generated") or "")
        if stamp and _state("look_stamp") != stamp:
            if _state("look_theme") or _state("look_density"):
                note = True                 # they had picked something
            _set_state("look_stamp", stamp)
            _set_state("look_theme", "")
            _set_state("look_density", "")
            _set_state("look_layout", "")
            # their own logo goes too -- a delivery carries the one JTS set,
            # and half-and-half would be worse than either
            _set_state("look_logo", "")
        theirs = _state("look_theme")
        if theirs in PALETTES:
            key = theirs
        d = _state("look_density")
        if d in DENSITIES:
            density = d
        lay = _state("look_layout")
        if lay in LAYOUTS:
            layout = lay
        if _state("look_logo") == "1":
            logo = True
    except Exception:
        pass
    return {"key": key, "vars": PALETTES.get(key, {}).get("vars", {}),
            "dark": PALETTES.get(key, {}).get("dark", {}),
            "density": density, "layout": layout, "logo": logo, "note": note}


@app.context_processor
def _look_ctx():
    return {"LOOK": _look(), "PALETTES": PALETTES, "DENSITIES": DENSITIES,
            "LAYOUTS": LAYOUTS}


@app.route("/look")
def look_page():
    """Colours and text size. Reached by the gear in the header."""
    return render_template("look.html")


@app.route("/look", methods=["POST"])
def look_save():
    """Save a choice. No Save button and no restart -- the next page render
    uses it, the same way the dark-mode toggle already behaves."""
    # Clear any pending reset FIRST. _look() is what notices that JTS has sent
    # the app out again and wipes their choices -- if that ran afterwards it
    # would wipe the change being made right now, so the first thing they ever
    # picked would silently not take.
    _look()
    payload = request.get_json(silent=True) or {}
    what = (payload.get("what") or "").strip()
    value = (payload.get("value") or "").strip()
    if what == "theme":
        if value not in PALETTES:
            return {"ok": False, "error": "no such colour"}, 400
        _set_state("look_theme", value)
    elif what == "density":
        if value not in DENSITIES:
            return {"ok": False, "error": "no such size"}, 400
        _set_state("look_density", value)
    elif what == "layout":
        if value not in LAYOUTS:
            return {"ok": False, "error": "no such layout"}, 400
        _set_state("look_layout", value)
    elif what == "reset":
        _set_state("look_theme", "")
        _set_state("look_density", "")
        _set_state("look_layout", "")
    else:
        return {"ok": False, "error": "nothing to change"}, 400
    return {"ok": True, "look": _look()}


@app.route("/look/logo", methods=["POST"])
def look_logo():
    """Put their own logo in the header.

    The FIRST BYTES decide whether it is really a picture -- a filename is
    whatever the sender says it is. Written to the one place the header reads,
    so nothing has to be told where it went.
    """
    f = request.files.get("logo")
    if f is None:
        return {"ok": False, "error": "no picture"}, 400
    blob = f.read(4 * 1024 * 1024 + 1)
    if len(blob) > 4 * 1024 * 1024:
        return {"ok": False, "error": "that picture is too big (4MB limit)"}, 400
    if not _sniff(blob):
        return {"ok": False, "error": "that is not a picture"}, 400
    target = os.path.join(HERE, "static", "logo.png")
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(blob)
        _set_state("look_logo", "1")
    except OSError as exc:
        return {"ok": False, "error": str(exc)[:120]}, 500
    return {"ok": True}


@app.route("/look/logo/remove", methods=["POST"])
def look_logo_remove():
    """Take it back off. The file stays -- an update only ever writes, so a
    flag is what actually hides it, the same way BUSINESS['logo'] works."""
    _set_state("look_logo", "")
    return {"ok": True}


@app.route("/look/seen", methods=["POST"])
def look_seen():
    """Tick off the "JTS updated your colours" note so it shows once."""
    try:
        biz = getattr(modules, "BUSINESS", {}) or {}
        _set_state("look_stamp", str(biz.get("generated") or ""))
    except Exception:
        pass
    return {"ok": True}


@app.route("/about")
def about():
    return render_template("about.html", kind=COPY_KIND, hosted=hosted(),
                           here=HERE, backup=_backup_dir())


@app.route("/uninstall", methods=["POST"])
def uninstall():
    import shutil
    import firstrun          # imported here: the app must still open even on a
                             # copy where this file is somehow missing
    payload = request.get_json(silent=True) or {}
    typed = (payload.get("confirm") or "").strip().lower()
    name = (modules.BUSINESS.get("name") or "").strip().lower()

    # Only a real installed copy may remove itself. The master and the test
    # copy live inside the workshop and are not anybody's to delete.
    if COPY_KIND != "customer":
        return {"ok": False, "error": "this copy can't remove itself"}, 400
    if not name or typed != name:
        return {"ok": False, "error": "type the name exactly as it appears"}, 400

    # 1. their records, first and always
    saved = ""
    try:
        src = os.path.join(HERE, "data")
        if os.path.isdir(src):
            saved = _backup_dir()
            n = 1
            while os.path.exists(saved):
                n += 1
                saved = "%s (%d)" % (_backup_dir(), n)
            shutil.copytree(src, saved)
    except Exception as exc:
        # if their work cannot be put somewhere safe, nothing is removed
        diagnostics.log("uninstall", "", "backup failed: %s" % exc)
        return {"ok": False, "error": "couldn't save a copy of your records, "
                                      "so nothing has been removed"}, 500

    # 2. the shortcuts, then the folder -- from OUTSIDE, since this program is
    #    sitting in the folder being removed and cannot delete itself
    try:
        firstrun.schedule_removal(HERE, os.getpid(),
                                  modules.BUSINESS.get("name") or "App")
    except Exception as exc:
        diagnostics.log("uninstall", "", "could not schedule removal: %s" % exc)
        return {"ok": False, "error": "your records are saved at %s, but the "
                                      "app couldn't remove itself" % saved}, 500

    def bye():
        time.sleep(1.0)
        os._exit(0)

    threading.Thread(target=bye, daemon=True).start()
    return {"ok": True, "saved": saved}


@app.route("/system/reload", methods=["POST"])
def system_reload():
    """Restart the app.

    Worth having for them, not just for me: an update is applied when the app
    STARTS, so this is how a fix that has just been sent actually arrives
    without them having to close it and find the icon again. It is also the
    first thing to try if the app ever goes odd.

    Nothing is saved on the way out because nothing needs to be -- every page
    writes as you go, so there is no unsaved state to lose.
    """
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)                     # local app; nothing else may restart it

    def go():
        import subprocess
        time.sleep(0.35)               # let this response reach the window
        env = dict(os.environ)
        env["APP_SKIP_INSTALL"] = "1"  # already installed; don't do it again
        try:
            subprocess.Popen([sys.executable, os.path.join(HERE, "desktop.py")],
                             cwd=HERE, env=env)
        except OSError as exc:
            diagnostics.log("reload", "", "could not restart: %s" % exc)
            return
        os._exit(0)

    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


@app.context_processor
def _inject():
    return {"MODULES": modules.MODULES, "BUSINESS": modules.BUSINESS,
            "COPY_KIND": COPY_KIND}


def _module_or_404(key: str) -> dict:
    m = modules.by_key(key)
    if m is None:
        abort(404)
    return m


def _clean(field: dict, value):
    """Coerce one posted value to what its column expects."""
    if field["type"] in ("money", "number"):
        if value in (None, "", "-"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if field["type"] == "check":
        return 1 if value in (True, 1, "1", "true", "on") else 0
    if field["type"] == "photo":
        # A photo column holds a filename WE chose, written only by the upload
        # route below. Whatever arrived on the ordinary form is ignored, so
        # nobody can point a record at a file by posting its name.
        return ""
    return ("" if value is None else str(value)).strip()


# --- photos -----------------------------------------------------------------
# A photo field's COLUMN holds a filename. The picture itself lives in
# data/photos/ on this machine, and never goes in the database: a few hundred
# photos would make app.db hundreds of times larger than the records
# themselves, and every backup would then carry every picture again. Keeping
# them in their own folder also means they can be deliberately LEFT OUT of a
# backup -- they are the one thing here that is big.
#
# Nothing about an uploaded file is trusted. The name they came with is
# discarded and we choose our own; the type is decided by the first few BYTES
# rather than the extension; and serving looks the filename up in the
# database, never taking a path from the URL.

HERE = os.path.dirname(os.path.abspath(__file__))
PHOTO_DIR = os.path.join(HERE, "data", "photos")
MAX_PHOTO_BYTES = 12 * 1024 * 1024

_MAGIC = [(b"\xff\xd8\xff", "jpg", "image/jpeg"),
          (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
          (b"GIF87a", "gif", "image/gif"),
          (b"GIF89a", "gif", "image/gif")]


def _sniff(blob: bytes):
    """(extension, content type) for a real picture, else None. An extension is
    a claim; this is evidence. WebP needs two separate checks -- "RIFF", then
    "WEBP" four bytes later -- so it can't live in the table above."""
    for magic, ext, ctype in _MAGIC:
        if blob.startswith(magic):
            return ext, ctype
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def _photo_field(m: dict, name: str):
    """The named field, but only if it really is a photo box on this tab."""
    f = next((x for x in m["fields"] if x["name"] == name), None)
    return f if f is not None and f["type"] == "photo" else None


def _photo_path(m: dict, stored: str) -> str:
    """Where a stored filename lives. basename() is the guard -- the value came
    out of our own database, but a bare name can never climb out of the folder
    even if one day something else writes to that column."""
    return os.path.join(PHOTO_DIR, m["table"], os.path.basename(stored))


def _drop_photos(m: dict, row) -> None:
    """Delete every picture belonging to a row, so a deleted record doesn't
    leave its photos behind forever."""
    for f in m["fields"]:
        if f["type"] != "photo":
            continue
        stored = (row[f["name"]] if row else "") or ""
        if stored:
            try:
                os.remove(_photo_path(m, stored))
            except OSError:
                pass


def _row_or_none(m: dict, row_id: int):
    return db.query_one("SELECT * FROM %s WHERE id = ?" % m["table"], (row_id,))


@app.route("/m/<key>/<int:row_id>/photo/<field>", methods=["POST"])
def photo_upload(key: str, row_id: int, field: str):
    m = _module_or_404(key)
    if _photo_field(m, field) is None:
        return {"ok": False, "error": "unknown field"}, 400
    row = _row_or_none(m, row_id)
    if row is None:
        return {"ok": False, "error": "no such record"}, 404

    upload = request.files.get("photo")
    if upload is None:
        return {"ok": False, "error": "no picture was sent"}, 400
    # read one byte past the limit, so "too big" is caught without pulling a
    # huge file into memory first
    blob = upload.read(MAX_PHOTO_BYTES + 1)
    if len(blob) > MAX_PHOTO_BYTES:
        return {"ok": False, "error": "that picture is too big (12 MB limit)"}, 400
    kind = _sniff(blob)
    if kind is None:
        return {"ok": False, "error": "that file isn't a picture"}, 400

    folder = os.path.join(PHOTO_DIR, m["table"])
    os.makedirs(folder, exist_ok=True)
    name = "%d-%s-%s.%s" % (row_id, field, secrets.token_hex(6), kind[0])
    with open(os.path.join(folder, name), "wb") as fh:
        fh.write(blob)

    old = row[field] or ""
    db.update(m["table"], row_id, **{field: name})
    # the old one only goes once the new one is safely written and recorded
    if old and old != name:
        try:
            os.remove(_photo_path(m, old))
        except OSError:
            pass
    return {"ok": True, "file": name}


@app.route("/m/<key>/<int:row_id>/photo/<field>/remove", methods=["POST"])
def photo_delete(key: str, row_id: int, field: str):
    m = _module_or_404(key)
    if _photo_field(m, field) is None:
        return {"ok": False, "error": "unknown field"}, 400
    row = _row_or_none(m, row_id)
    if row is None:
        return {"ok": False, "error": "no such record"}, 404
    stored = row[field] or ""
    db.update(m["table"], row_id, **{field: ""})
    if stored:
        try:
            os.remove(_photo_path(m, stored))
        except OSError:
            pass
    return {"ok": True}


@app.route("/photo/<key>/<int:row_id>/<field>")
def photo_show(key: str, row_id: int, field: str):
    m = _module_or_404(key)
    if _photo_field(m, field) is None:
        abort(404)
    row = _row_or_none(m, row_id)
    stored = (row[field] if row else "") or ""
    if not stored:
        abort(404)
    try:
        with open(_photo_path(m, stored), "rb") as fh:
            blob = fh.read()
    except OSError:
        abort(404)
    kind = _sniff(blob)
    if kind is None:                        # not a picture any more -- refuse
        abort(404)
    resp = Response(blob, mimetype=kind[1])
    resp.headers["Content-Disposition"] = "inline"
    # the type is worked out from the bytes above; stop the browser second-
    # guessing it from anything else
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _send_wish(row_id: int, values: dict) -> None:
    """Post a new request up so it reaches the person who builds this app.

    Updates already come DOWN to this machine; this is the way back, so a
    request typed in here is seen wherever the app happens to be. Only the
    sentence they typed is sent -- never a record, never a customer, never
    anything out of their database.

    Deliberately fire-and-forget on a background thread: the request has
    already been saved locally by the time this runs, so no network problem
    may ever slow down or fail the thing they actually pressed. If it cannot
    get through, it is simply not sent -- their copy still has it.
    """
    url = (getattr(modules, "BUSINESS", {}) or {}).get("wish_url") or ""
    uid = (getattr(modules, "BUSINESS", {}) or {}).get("id") or ""
    text = ""
    for f in (modules.by_key("wishlist") or {}).get("fields", []):
        if f["type"] == "textarea" or f["name"] in ("request", "text", "what"):
            text = str(values.get(f["name"]) or "").strip()
            if text:
                break
    # https only, so a request cannot be read off the wire on their shop wifi.
    # Loopback is exempt because that traffic never leaves the machine, and it
    # is what the tests drive.
    local = url.startswith(("http://127.0.0.1", "http://localhost"))
    if not ((url.startswith("https://") or local) and uid and text):
        return

    def go():
        import json as _json
        import urllib.request as _u
        body = _json.dumps({"app_uid": uid, "local_id": int(row_id),
                            "text": text}).encode("utf-8")
        req = _u.Request(url, data=body, method="POST",
                         headers={"Content-Type": "application/json",
                                  "User-Agent": "jts-app"})
        try:
            with _u.urlopen(req, timeout=10, context=_ssl_ctx()) as r:
                r.read(200)
        except Exception as exc:                 # never surfaces to them
            diagnostics.log("wish", "", "could not send: %s" % exc)

    threading.Thread(target=go, daemon=True).start()


def _sum(m: dict) -> float:
    row = db.query_one("SELECT SUM(%s) s FROM %s" % (m["sum_field"], m["table"]))
    return (row["s"] or 0.0) if row else 0.0


def _split_archive(m, rows):
    """A module can retire finished rows after a while (the Wishlist does, at
    24 hours). Returns (still showing, retired). Anything finished before this
    existed has no stamp, so it gets one now and its day starts from here --
    better than having it vanish the moment the feature arrives."""
    rule = m.get("archive")
    if not rule:
        return list(rows), []
    stamp, done = rule["stamp_field"], rule["done_value"]
    cutoff = (datetime.now() - timedelta(hours=rule["hours"])).strftime("%Y-%m-%d %H:%M")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    current, retired = [], []
    for r in rows:
        if (r[m["status_field"]] or "") != done:
            current.append(r)
            continue
        when = r[stamp] or ""
        if not when:
            db.update(m["table"], r["id"], **{stamp: now})
            when = now
        (retired if when < cutoff else current).append(r)
    return current, retired


DONE_STATUS = "Done"
SEEN_KEY = "wishlist_announced"


def _state(key: str, default: str = "") -> str:
    row = db.query_one("SELECT value FROM app_state WHERE key = ?", (key,))
    return row["value"] if row and row["value"] is not None else default


def _set_state(key: str, value: str) -> None:
    conn = db.get_conn()
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()
    conn.close()


def _wishlist():
    """Their requests, and any that have been finished since they last looked."""
    if modules.by_key("wishlist") is None:
        return [], []
    m = modules.by_key("wishlist")
    rows, _retired = _split_archive(m, db.query("SELECT * FROM wishlist ORDER BY id DESC"))
    seen = {s for s in _state(SEEN_KEY).split(",") if s}
    fresh = [r for r in rows
             if (r["status"] or "") == DONE_STATUS and str(r["id"]) not in seen]
    return rows, fresh


NOTES_KEY = "dashboard_notes"


@app.route("/notes", methods=["POST"])
def dashboard_notes():
    """The scratchpad on the dashboard. Saves as it's typed -- no Save button
    to forget about."""
    payload = request.get_json(silent=True) or {}
    _set_state(NOTES_KEY, payload.get("text") or "")
    return {"ok": True}


@app.route("/wishlist/seen", methods=["POST"])
def wishlist_seen():
    """Dismissing the "we fixed this" note. Remembers which ones were in it, so
    it never shows the same news twice."""
    rows, _ = _wishlist()
    done = [str(r["id"]) for r in rows if (r["status"] or "") == DONE_STATUS]
    _set_state(SEEN_KEY, ",".join(done))
    return {"ok": True}




def _soon(today, days=14, limit=6):
    """What is coming up, so an empty day does not leave an empty card.

    "Nothing dated today" is true most days and tells you nothing. The next
    thing due is what you actually wanted to know.
    """
    out = []
    for m in modules.MODULES:
        df = m.get("date_field")
        if not df:
            continue
        try:
            rows = db.query("SELECT * FROM %s WHERE %s > ? AND %s <= date(?, '+%d day') "
                            "ORDER BY %s LIMIT ?"
                            % (m["table"], df, df, int(days), df),
                            (today, today, limit))
        except Exception:
            continue
        for r in rows:
            out.append({"m": m, "r": r, "when": r[df]})
    out.sort(key=lambda x: str(x["when"] or ""))
    return out[:limit]


def _recent(limit=6):
    """The last few things put in, across every tab.

    Gives a quiet week something true to show, and on a busy one it is the
    pulse of the place -- who added what, without opening anything.
    """
    out = []
    for m in modules.MODULES:
        try:
            rows = db.query("SELECT * FROM %s WHERE COALESCE(created_at,'') != '' "
                            "ORDER BY created_at DESC LIMIT ?" % m["table"], (limit,))
        except Exception:
            continue
        for r in rows:
            out.append({"m": m, "r": r, "when": r["created_at"]})
    out.sort(key=lambda x: str(x["when"] or ""), reverse=True)
    return out[:limit]


@app.route("/")
def dashboard():
    today = date.today().isoformat()
    cards, today_rows = [], []
    for m in modules.MODULES:
        count = db.query_one("SELECT COUNT(*) c FROM %s" % m["table"])["c"]
        total = _sum(m) if m["sum_field"] else None
        due = 0
        if m["date_field"]:
            rows = db.query("SELECT * FROM %s WHERE %s = ? ORDER BY id"
                            % (m["table"], m["date_field"]), (today,))
            due = len(rows)
            for r in rows:
                today_rows.append({"m": m, "r": r})
        cards.append({"m": m, "count": count, "total": total, "today": due})
    wishes, just_fixed = _wishlist()
    return render_template("dashboard.html", cards=cards,
                           today_rows=today_rows, today=today,
                           soon=_soon(today), recent=_recent(),
                           wishes=wishes, just_fixed=just_fixed,
                           done_status=DONE_STATUS, notes=_state(NOTES_KEY))


# CAL_TABS: the only tabs that get a calendar at all -- ones where the date is
# something you plan around. Invoices, Time Clock, Expenses, Daily log and
# Notes all have dates too, and a month view on any of them is just clutter
# over a list you were going to read as a list.
# CAL_FIRST: of those, the ones that ARE a diary, so they open on it.
CAL_TABS = {"scheduling", "deliveries", "jobs", "rentals"}
CAL_FIRST = {"scheduling", "deliveries"}


def _cal_rows(m, rows):
    """The rows shaped for a month view, or None if this tab has no dates.

    Built here rather than in the page so the labels come from the module's own
    field list: whatever the business renamed its boxes to is what shows on the
    calendar, with no second copy of that decision to drift.
    """
    df = m.get("date_field")
    if not df or m["key"] not in CAL_TABS:
        return None
    tf = ""
    for f in m["fields"]:
        if f.get("type") == "time":
            tf = f["name"]
            break
    sf = m.get("status_field") or ""
    skip = {df, tf, sf, "id", "created_at"}
    wordy = [f["name"] for f in m["fields"]
             if f["name"] not in skip and not f.get("owner_only")
             and f.get("type") in ("text", "textarea", "email", "phone", "select")]
    out = []
    for r in rows:
        day = (r[df] or "")
        day = day.strip()[:10] if isinstance(day, str) else ""
        # only a real yyyy-mm-dd goes on the grid; anything else stays in the
        # list rather than being guessed at and put on the wrong day
        if len(day) != 10 or day[4] != "-" or day[7] != "-":
            continue
        said = []
        for n in wordy:
            v = r[n]
            if v not in (None, "") and str(v).strip():
                said.append(str(v).strip())
            if len(said) == 2:
                break
        out.append({
            "id": r["id"], "d": day,
            "t": (r[tf] or "") if tf else "",
            "l": said[0] if said else ("#%s" % r["id"]),
            "s": said[1] if len(said) > 1 else "",
            "st": (r[sf] or "") if sf else "",
        })
    return out


@app.route("/m/<key>")
def records(key: str):
    m = _module_or_404(key)
    rows = db.query("SELECT * FROM %s ORDER BY %s" % (m["table"], m["sort"]))
    rows, retired = _split_archive(m, rows)
    total = _sum(m) if m["sum_field"] else None
    return render_template("records.html", m=m, rows=rows, retired=retired,
                           total=total, cal=_cal_rows(m, rows),
                           cal_first=m["key"] in CAL_FIRST)


@app.route("/m/<key>/add", methods=["POST"])
def record_add(key: str):
    m = _module_or_404(key)
    payload = request.get_json(silent=True) or {}
    values = {}
    for f in m["fields"]:
        raw = payload.get(f["name"])
        # not on the form (or left blank) -> whatever the field says it starts as
        if raw in (None, "") and f.get("default") is not None:
            raw = f["default"]
        values[f["name"]] = _clean(f, raw)
    for f in m["fields"]:
        if f["required"] and not str(values.get(f["name"]) or "").strip():
            return {"ok": False, "error": f["label"] + " is required"}, 400
    new_id = db.insert(m["table"], created_at=db.now(), **values)
    if key == "wishlist":
        _send_wish(new_id, values)
    return {"ok": True, "id": new_id}


@app.route("/m/<key>/update", methods=["POST"])
def record_update(key: str):
    m = _module_or_404(key)
    payload = request.get_json(silent=True) or {}
    name = payload.get("field")
    field = next((f for f in m["fields"] if f["name"] == name), None)
    if field is None:                       # not a real column on this tab
        return {"ok": False, "error": "unknown field"}, 400
    if field["type"] == "photo":
        # photo columns are written only by the upload/remove routes. Allowing
        # them here would blank the column and strand the file on disk.
        return {"ok": False, "error": "use the photo upload"}, 400
    try:
        row_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad id"}, 400
    db.update(m["table"], row_id, **{name: _clean(field, payload.get("value"))})
    return {"ok": True}


@app.route("/m/<key>/delete", methods=["POST"])
def record_delete(key: str):
    m = _module_or_404(key)
    payload = request.get_json(silent=True) or {}
    try:
        row_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad id"}, 400
    # pictures first, while the row can still tell us which ones are its own
    _drop_photos(m, _row_or_none(m, row_id))
    db.delete(m["table"], row_id)
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
