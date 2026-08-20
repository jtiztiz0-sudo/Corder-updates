"""
Checks this app and prints a report.

Written for the moment somebody says "it isn't working" and neither of us can
see the other's screen. It runs on the app's OWN Python, tests each thing in
turn, and says in plain words which step failed -- so one photo of this window
answers what an evening of guessing could not.

Reads only. It changes nothing except sending requests that were already
waiting to go.
"""

import io
import os
import sys
import json
import sqlite3
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

LINE = "-" * 62
ok_count = [0]
bad = []


def head(text):
    print("")
    print(text)
    print(LINE)


def good(label, detail=""):
    ok_count[0] += 1
    print("  OK    %-34s %s" % (label, detail))


def fail(label, detail=""):
    bad.append(label)
    print("  FAIL  %-34s %s" % (label, detail))


def note(label, detail=""):
    print("        %-34s %s" % (label, detail))


print(LINE)
print(" CHECKING THIS APP")
print(" %s" % datetime.datetime.now().strftime("%d %b %Y  %H:%M"))
print(LINE)

head("1. WHICH COPY THIS IS")
note("folder", HERE)
for f, what in (("version.txt", "program version"),
                ("app_version.txt", "your settings version")):
    try:
        with open(os.path.join(HERE, f), encoding="utf-8") as fh:
            note(what, fh.read().strip())
    except OSError:
        note(what, "(none)")

try:
    import modules
    b = getattr(modules, "BUSINESS", {}) or {}
    note("business", b.get("name") or "?")
    note("app id", b.get("id") or "?")
    WISH = b.get("wish_url") or ""
    note("sends requests to", WISH or "NOTHING SET")
except Exception as exc:
    fail("could not read modules.py", str(exc)[:60])
    WISH = ""

head("2. CAN IT DO SECURE INTERNET AT ALL")
try:
    import certifi
    pem = certifi.where()
    if os.path.isfile(pem):
        good("certificates", "%.0f KB" % (os.path.getsize(pem) / 1024))
    else:
        fail("certificates", "certifi is installed but the file is missing")
except Exception:
    fail("certificates", "MISSING - this app cannot use https at all")

ctx = None
try:
    import ssl
    import certifi
    ctx = ssl.create_default_context(cafile=certifi.where())
    good("secure connections", "ready")
except Exception as exc:
    fail("secure connections", str(exc)[:60])

head("3. CAN IT REACH THE OUTSIDE WORLD")
import urllib.request


def reach(name, url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "jts-check"})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            good(name, "reached (%s)" % r.status)
            return r.read(4000)
    except Exception as exc:
        fail(name, str(exc)[:80])
        return None


if WISH:
    root = WISH.rsplit("/", 1)[0]
    reach("your website", root + "/healthz")
else:
    fail("your website", "no address set in this copy")

try:
    import updater
    if updater.MANIFEST_URL:
        raw = reach("the update service", updater.MANIFEST_URL)
        if raw:
            try:
                mf = json.loads(raw.decode("utf-8"))
                here_v = updater.local_version()
                there = mf.get("version")
                if there and here_v and there > here_v:
                    note("an update is waiting", "%s -> %s" % (here_v, there))
                else:
                    note("up to date", "version %s" % here_v)
            except Exception:
                pass
    else:
        fail("the update service", "no address set in this copy")
except Exception as exc:
    fail("the update service", str(exc)[:60])

head("4. YOUR REQUESTS")
try:
    import modules
    m = modules.by_key("wishlist")
    dbf = os.path.join(HERE, "data", "app.db")
    if not m:
        note("no Wishlist tab in this app", "")
    elif not os.path.isfile(dbf):
        note("nothing typed in yet", "")
    else:
        con = sqlite3.connect(dbf)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT id, request, status FROM %s ORDER BY id" % m["table"])]
        con.close()
        opn = [r for r in rows if (r.get("status") or "") != "Done"]
        note("requests in this copy", "%d (%d still open)" % (len(rows), len(opn)))
        for r in opn[:5]:
            note("  waiting", (r["request"] or "")[:44])
except Exception as exc:
    fail("could not read your requests", str(exc)[:60])

head("5. SENDING ANYTHING THAT IS STUCK")
sent = 0
try:
    import app as _a
    before = None
    _a._flush_wishes()
    import time
    time.sleep(4)
    good("tried to send", "see your dashboard in a moment")
except Exception as exc:
    fail("could not send", str(exc)[:80])

head("6. WHAT THE APP ITSELF RECORDED")
for f, what in (("update.log", "update log"), (os.path.join("logs", "app.log"), "app log")):
    path = os.path.join(HERE, f)
    if not os.path.isfile(path):
        note(what, "(empty)")
        continue
    try:
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            lines = [l.strip() for l in fh if l.strip()][-3:]
        note(what, "")
        for l in lines:
            print("          " + l[:110])
    except OSError:
        note(what, "(could not read)")

print("")
print(LINE)
if bad:
    print(" SOMETHING IS WRONG. The failed steps above are:")
    for b in bad:
        print("   - %s" % b)
    print("")
    print(" Send JT a photo of this window.")
else:
    print(" EVERYTHING PASSED (%d checks)." % ok_count[0])
    print(" If a request still hasn't reached him, tell him what this says.")
print(LINE)
print("")
input("Press Enter to close.")
