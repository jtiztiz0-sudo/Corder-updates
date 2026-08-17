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
import threading
from datetime import date, datetime, timedelta

from flask import Flask, render_template, request, abort, Response

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
LICENCE_FILE = os.path.join(HERE, "data", "licence.json")
_licence = {"locked": False, "message": ""}


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
            with _u.urlopen(req, timeout=10) as r:
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
            with _u.urlopen(req, timeout=10) as r:
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
            with _u.urlopen(req, timeout=10) as r:
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
                           wishes=wishes, just_fixed=just_fixed,
                           done_status=DONE_STATUS, notes=_state(NOTES_KEY))


@app.route("/m/<key>")
def records(key: str):
    m = _module_or_404(key)
    rows = db.query("SELECT * FROM %s ORDER BY %s" % (m["table"], m["sort"]))
    rows, retired = _split_archive(m, rows)
    total = _sum(m) if m["sum_field"] else None
    return render_template("records.html", m=m, rows=rows, retired=retired, total=total)


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
