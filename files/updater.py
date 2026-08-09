"""
Updates this app from the internet.

The client runs the app on their own machine; nothing about it is hosted.
This is the one thing that reaches out: on launch it asks a single URL whether
a newer version of the CODE exists, and if so replaces the code files before
the app starts. Their records are never involved.

What it will and won't touch
----------------------------
Replaced : the shared program files (app.py, db.py, templates, stylesheet...)
           modules.py           -- but ONLY this business's own entry, found
                                   by matching BUSINESS['id'] in the manifest
Never    : data/app.db          -- their records
           another business's anything
           anything not listed in the manifest

Two sections, two version numbers
---------------------------------
The manifest carries the ENGINE (one copy, shared by every business) and an
`apps` block holding each business's own modules.py -- its tabs, name and
colour. They are counted separately, so adding a tab for one customer doesn't
make everyone else re-download the whole program. Either section can update
on its own; whichever is newer than what's installed gets fetched.

Safety rules, in order:
  1. No URL configured -> it does nothing at all and never touches the network.
  2. HTTPS only. A plain-http manifest is refused outright -- anyone on the
     same wifi could otherwise swap the payload for their own code.
  3. Every file's sha256 must match the manifest before ANYTHING is written,
     so a half-finished download can't leave a broken app behind.
  4. The files being replaced are copied into backup/<old-version>/ first.
  5. Any failure = give up quietly and run the version already installed.
     A broken update must never stop someone opening their own business app.

Honest limit: the hash list proves the download wasn't corrupted or tampered
with in transit, not that whoever published it was who they claim. Whoever
controls that URL can run code on this machine -- keep the account locked down.
"""

from __future__ import annotations

import os
import json
import shutil
import hashlib
import tempfile
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_URL = "https://raw.githubusercontent.com/jtiztiz0-sudo/Corder-updates/main/manifest.json"          # blank = updates switched off
VERSION_FILE = os.path.join(HERE, "version.txt")
APP_VERSION_FILE = os.path.join(HERE, "app_version.txt")
LOG_FILE = os.path.join(HERE, "update.log")

CONNECT_TIMEOUT = 8          # seconds -- a dead URL must not delay the launch
MAX_BYTES = 5 * 1024 * 1024  # a whole app is well under this; refuse anything huge


def _log(msg: str) -> None:
    try:
        from datetime import datetime
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def _url_allowed(url: str) -> bool:
    """HTTPS only -- over plain http anyone sharing the client's wifi could
    swap the payload for their own code. Loopback is the one exception: that
    traffic never leaves the machine, and it's what the tests run against."""
    low = (url or "").lower()
    if low.startswith("https://"):
        return True
    return low.startswith("http://127.0.0.1") or low.startswith("http://localhost")


def _read_int(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as fh:
            return int((fh.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


def local_version() -> int:
    """Which build of the shared ENGINE is installed."""
    return _read_int(VERSION_FILE)


def local_app_version() -> int:
    """Which build of THIS business's own config (modules.py) is installed.

    Missing file = 0, which is right for an app delivered before per-business
    updates existed: the first published entry is version 1 and lands.
    """
    return _read_int(APP_VERSION_FILE)


def app_id() -> str:
    """This app's permanent id, read from its own modules.py.

    Deliberately not the folder name or the business name -- the business gets
    renamed and the id must not move, or the app would start collecting
    somebody else's config (or nobody's).
    """
    try:
        import modules
        raw = str(modules.BUSINESS.get("id") or "").strip()
    except Exception:
        return ""
    # It becomes a URL path segment, so keep it to characters that cannot
    # steer the request somewhere else.
    return raw if raw and all(c.isalnum() or c in "-_" for c in raw) else ""


def _my_entry(manifest):
    """This business's slot in the manifest, or None. Looked up BY OUR OWN ID
    -- we never iterate the apps block, so another business's entry can never
    be picked up by accident."""
    mine = app_id()
    if not mine:
        return None
    apps = manifest.get("apps")
    if not isinstance(apps, dict):
        return None
    entry = apps.get(mine)
    return entry if isinstance(entry, dict) and isinstance(entry.get("files"), dict) else None


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "jts-app-updater"})
    with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("file bigger than expected -- refusing it")
    return data


def check():
    """Is anything newer waiting? Returns the manifest, or None.

    Newer EITHER in the shared engine OR in this business's own entry -- a new
    tab for this shop must arrive even when the engine hasn't changed at all.
    """
    if not MANIFEST_URL:
        return None
    if not _url_allowed(MANIFEST_URL):
        _log("refused: update URL is not https")
        return None
    try:
        manifest = json.loads(_get(MANIFEST_URL).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log("check failed: %s" % exc)
        return None
    if not isinstance(manifest.get("files"), dict) or not manifest["files"]:
        _log("manifest has no files -- ignoring")
        return None
    try:
        engine_new = int(manifest.get("version", 0)) > local_version()
    except (TypeError, ValueError):
        return None
    mine = _my_entry(manifest)
    try:
        app_new = bool(mine) and int(mine.get("version", 0)) > local_app_version()
    except (TypeError, ValueError):
        app_new = False
    return manifest if (engine_new or app_new) else None


def _safe_relpath(rel: str) -> str | None:
    r"""Reject anything that would write outside this folder (../, C:\..., /etc)."""
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or ".." in rel.split("/") or ":" in rel:
        return None
    return os.path.normpath(os.path.join(HERE, *rel.split("/")))


def apply(manifest) -> bool:
    """Download everything, verify it, then swap it in. All or nothing --
    across BOTH sections, so the program and this business's own config can
    never end up from different releases."""
    base = manifest.get("base") or MANIFEST_URL.rsplit("/", 1)[0] + "/files/"

    engine_new = int(manifest.get("version", 0)) > local_version()
    mine = _my_entry(manifest)
    app_new = bool(mine) and int(mine.get("version", 0)) > local_app_version()

    # (path within the payload, where it lands here, expected sha256)
    jobs = []
    app_dests = set()
    if engine_new:
        for rel, meta in manifest["files"].items():
            dest = _safe_relpath(rel)
            if dest is None:
                _log("refused suspicious path in manifest: %s" % rel)
                return False
            jobs.append((rel, dest, (meta or {}).get("sha256", "")))
    if app_new:
        for name, meta in mine["files"].items():
            dest = _safe_relpath(name)
            if dest is None:
                _log("refused suspicious path in my entry: %s" % name)
                return False
            jobs.append(("apps/%s/%s" % (app_id(), name), dest,
                         (meta or {}).get("sha256", "")))
            app_dests.add(dest)
    if not jobs:
        return False

    staged = {}
    tmp = tempfile.mkdtemp(prefix="app-update-")
    try:
        for rel, dest, want in jobs:
            # A file name can legally contain spaces ("OPEN-THE-APP.bat"
            # does) and a raw space is not valid in a URL -- urllib rejects
            # it outright and the whole update fails, silently. Quote it,
            # keeping the "/" separators intact.
            blob = _get(base + urllib.parse.quote(rel))
            got = hashlib.sha256(blob).hexdigest()
            if want != got:
                _log("checksum mismatch on %s -- update abandoned" % rel)
                return False
            hold = os.path.join(tmp, rel.replace("/", "__"))
            with open(hold, "wb") as fh:
                fh.write(blob)
            staged[dest] = hold

        # everything downloaded and verified -- now it's safe to write.
        # Engine and config are backed up under their own version numbers, so
        # updating one never overwrites the other's saved copy.
        eng_backup = os.path.join(HERE, "backup", str(local_version()))
        app_backup = os.path.join(HERE, "backup", "app-%d" % local_app_version())
        for dest, hold in staged.items():
            rel = os.path.relpath(dest, HERE)
            if os.path.exists(dest):
                root = app_backup if dest in app_dests else eng_backup
                keep = os.path.join(root, rel)
                os.makedirs(os.path.dirname(keep), exist_ok=True)
                shutil.copy2(dest, keep)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(hold, dest)
            # copy2 carries the staged file's own mode across, and that was
            # made by tempfile -- 0644, no execute bit. On a Mac a launcher
            # that arrives in an update would come out unrunnable: the .app
            # icon calls python directly and is fine, but the folder's own
            # OPEN-THE-APP.command is the way back in when the icon is broken,
            # which is exactly when you need it. Windows has no execute bit,
            # so this is a no-op there.
            if dest.endswith((".command", ".sh")):
                try:
                    os.chmod(dest, 0o755)
                except OSError:
                    pass

        if engine_new:
            with open(VERSION_FILE, "w", encoding="utf-8") as fh:
                fh.write(str(int(manifest["version"])))
        if app_new:
            with open(APP_VERSION_FILE, "w", encoding="utf-8") as fh:
                fh.write(str(int(mine["version"])))
            _log("this shop's own settings updated to version %s" % mine["version"])
        # A file that used to ship but no longer does is never overwritten by
        # an update -- it just sits there. For the launcher that matters: two
        # of them side by side and the customer has to guess which to open.
        # Only names on this explicit list are ever removed.
        for stale in ("run.bat", "OPEN THE APP.bat"):
            if stale in manifest["files"]:
                continue
            gone = os.path.join(HERE, stale)
            if os.path.isfile(gone):
                try:
                    os.remove(gone)
                    _log("removed the old launcher %s" % stale)
                except OSError:
                    pass
        if engine_new:
            _log("updated to version %s (%d files)" % (manifest["version"], len(staged)))
        return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log("update failed, keeping the version already installed: %s" % exc)
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def update_if_available() -> bool:
    """Called on launch, before the app loads. True = files changed."""
    manifest = check()
    if not manifest:
        return False
    return apply(manifest)


if __name__ == "__main__":
    print("installed version:", local_version())
    m = check()
    print("update available:", m["version"] if m else "no")
    if m and input("install it? [y/N] ").strip().lower() == "y":
        print("updated" if apply(m) else "failed -- see update.log")
