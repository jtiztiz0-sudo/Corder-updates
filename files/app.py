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
from datetime import date, datetime, timedelta

from flask import Flask, render_template, request, abort

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import modules

app = Flask(__name__)
app.secret_key = "local-desktop-app"
# desktop.py runs without Flask's reloader (it would spawn a second process
# and break the window), so template edits need this to show up on refresh.
app.config["TEMPLATES_AUTO_RELOAD"] = True

db.init_db()


@app.context_processor
def _inject():
    return {"MODULES": modules.MODULES, "BUSINESS": modules.BUSINESS}


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
    return ("" if value is None else str(value)).strip()


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
    return {"ok": True, "id": new_id}


@app.route("/m/<key>/update", methods=["POST"])
def record_update(key: str):
    m = _module_or_404(key)
    payload = request.get_json(silent=True) or {}
    name = payload.get("field")
    field = next((f for f in m["fields"] if f["name"] == name), None)
    if field is None:                       # not a real column on this tab
        return {"ok": False, "error": "unknown field"}, 400
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
    db.delete(m["table"], row_id)
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
