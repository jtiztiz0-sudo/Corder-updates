"""
First run: move in properly, and put an icon where she can find it.

The person opening this has just unzipped a folder out of an email. Left to
itself that folder sits in Downloads -- which is where things go to be tidied
away -- and this folder is where all their records live.

So the first launch installs the app:

    %LOCALAPPDATA%\\Programs\\<folder>     per-user, needs no admin rights

then makes a Desktop shortcut and a Start-menu entry, and reopens itself from
there. The folder they unzipped becomes disposable.

It only does this once. Afterwards the app is already living in the right
place and this just checks the shortcuts are still there.

Nothing here is allowed to stop the app opening -- every step is best-effort.
Worst case it runs from where it is, exactly like before.
"""

from __future__ import annotations

import os
import re
import sys
import shlex
import shutil
import tempfile
import subprocess

# Set on the copy we relaunch, so an install can never trigger another one.
SKIP_ENV = "APP_SKIP_INSTALL"
CREATE_NO_WINDOW = 0x08000000
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"


BAD_IN_NAME = "[" + chr(92) + '/:*?"<>|' + "]"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "app").lower()).strip("_") or "app"


def folder_name(app_name: str, app_id: str = "") -> str:
    """Where this app lives, keyed on its PERMANENT id -- never on its name.

    Keying it on the name was a real bug: rename the business and the app
    looked for a folder that didn't exist, installed itself a second time,
    and left every record behind in the old one. From the owner's side their
    data had simply disappeared.
    """
    return _slug(app_id) if app_id else _slug(app_name)


def install_home(app_name: str, app_id: str = "") -> str:
    if IS_MAC:
        # ~/Applications is the per-user apps folder -- no admin rights, and
        # it survives an OS upgrade. /Applications would need a password.
        return os.path.join(os.path.expanduser("~"), "Applications",
                            folder_name(app_name, app_id))
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Programs", folder_name(app_name, app_id))


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _powershell() -> str:
    """Full path, not the bare name. PATH is not guaranteed to contain the
    PowerShell folder -- it isn't on a minimal one -- and a shortcut silently
    not appearing is exactly the sort of failure nobody reports."""
    root = os.environ.get("SystemRoot") or r"C:\\Windows"
    full = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return full if os.path.isfile(full) else "powershell"


# Desktop and Start-menu folders are ASKED FOR, not guessed. "~\\Desktop" is
# wrong on any machine where OneDrive has taken the Desktop over, which is most
# of them now -- the real one is under OneDrive and the guess doesn't exist, so
# the shortcut just never appears. Windows itself knows where they are.
_SHORTCUT_PS = """
$ErrorActionPreference = 'Stop'
$exePath  = %(exe)s
$argList  = %(args)s
$workDir  = %(work)s
$iconPath = %(icon)s
$linkName = %(name)s
$targets = @(
  [Environment]::GetFolderPath('Desktop'),
  (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs')
)
foreach ($dir in $targets) {
  if ([string]::IsNullOrEmpty($dir)) { continue }
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  $link = Join-Path $dir ($linkName + '.lnk')
  $s = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
  $s.TargetPath = $exePath
  $s.Arguments = $argList
  $s.WorkingDirectory = $workDir
  if (Test-Path $iconPath) { $s.IconLocation = $iconPath }
  $s.Save()
  Write-Output $link
}
"""


_MAC_LAUNCHER = """#!/bin/sh
# This is what the Dock/Applications icon runs -- the everyday way in. It calls
# the bundled python directly, so it never depends on the .command launcher.
# -f rather than -x, and put a lost execute bit back: see the same note in
# OPEN-THE-APP.command. Falling through to a system python3 gives an import
# error later instead of a working app.
cd "%(home)s" || exit 1
for py in "runtime-mac/bin/python3" "runtime/bin/python3"; do
  if [ -f "$py" ]; then
    [ -x "$py" ] || chmod +x "$py" 2>/dev/null
    if [ -x "$py" ]; then
      exec "$py" desktop.py
    fi
  fi
done
exec python3 desktop.py
"""

_MAC_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>%(name)s</string>
  <key>CFBundleDisplayName</key><string>%(name)s</string>
  <key>CFBundleIdentifier</key><string>com.jts.%(slug)s</string>
  <key>CFBundleExecutable</key><string>%(exe)s</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
"""


def _mac_shortcuts(home: str, app_name: str) -> list:
    """A real .app bundle in ~/Applications, so it turns up in Launchpad and
    the Dock like anything else, plus an alias on the Desktop.

    A .app is only a folder with a known shape:
        <Name>.app/Contents/Info.plist
        <Name>.app/Contents/MacOS/<exe>     a shell script, marked executable

    The plain OPEN-THE-APP.command inside the app folder still works and is
    the fallback if anything here fails -- nothing depends on the bundle.

    Double quotes on this docstring are deliberate: the whole file is embedded
    in appgen.py inside a triple-single-quoted literal, so a triple single
    quote anywhere in here would close it early.
    """
    made = []
    safe = re.sub(r"[/:]", "", app_name or "App").strip() or "App"
    apps = os.path.join(os.path.expanduser("~"), "Applications")
    bundle = os.path.join(apps, safe + ".app")
    macos = os.path.join(bundle, "Contents", "MacOS")
    exe_name = "run"
    try:
        os.makedirs(macos, exist_ok=True)
        os.makedirs(os.path.join(bundle, "Contents", "Resources"), exist_ok=True)
        with open(os.path.join(bundle, "Contents", "Info.plist"), "w",
                  encoding="utf-8") as fh:
            fh.write(_MAC_PLIST % {"name": safe, "exe": exe_name,
                                   "slug": _slug(app_name) or "app"})
        launcher = os.path.join(macos, exe_name)
        with open(launcher, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_MAC_LAUNCHER % {"home": home.replace('"', '\\"')})
        os.chmod(launcher, 0o755)
        made.append(bundle)
    except OSError:
        return made

    # an alias on the Desktop, the same convenience the Windows copy gets
    try:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        if os.path.isdir(desktop):
            link = os.path.join(desktop, safe + ".app")
            if os.path.islink(link) or os.path.exists(link):
                if os.path.islink(link):
                    os.unlink(link)
            if not os.path.exists(link):
                os.symlink(bundle, link)
                made.append(link)
    except OSError:
        pass
    return made


def make_shortcuts(home: str, app_name: str) -> list:
    """Desktop + Start menu. Points at the app's own pythonw, not the .bat --
    a .bat shortcut flashes a black console box open every single time."""
    if IS_MAC:
        return _mac_shortcuts(home, app_name)
    if not IS_WINDOWS:
        return []
    exe = os.path.join(home, "runtime", "pythonw.exe")
    if os.path.isfile(exe):
        args = '"%s"' % os.path.join(home, "desktop.py")
    else:
        exe, args = os.path.join(home, "OPEN-THE-APP.bat"), ""
    icon = os.path.join(home, "static", "icon.ico")
    safe = re.sub(r'[\\/:*?"<>|]', "", app_name or "App").strip() or "App"

    script = _SHORTCUT_PS % {"exe": _ps_quote(exe), "args": _ps_quote(args),
                             "work": _ps_quote(home), "icon": _ps_quote(icon),
                             "name": _ps_quote(safe)}
    try:
        r = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-Command", script],
                           capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW)
    except OSError:
        return []
    return [line.strip() for line in (r.stdout or "").splitlines()
            if line.strip() and os.path.isfile(line.strip())]


def _tidy_old_shortcuts(current_name: str, home: str) -> None:
    """Delete Desktop/Start-menu shortcuts this app made under a previous
    name. Only ones whose target is inside our own install folder, so it can
    never remove somebody else's shortcut."""
    keep = re.sub(BAD_IN_NAME, "", current_name or "").strip() + ".lnk"
    mine = os.path.abspath(home)
    script = (
        "$keep = %s; $mine = %s;"
        "foreach ($d in @([Environment]::GetFolderPath('Desktop'),"
        " (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'))) {"
        "  if (-not (Test-Path $d)) { continue }"
        "  foreach ($f in Get-ChildItem $d -Filter *.lnk) {"
        "    if ($f.Name -eq $keep) { continue }"
        "    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($f.FullName);"
        "    if ($s.WorkingDirectory -and $s.WorkingDirectory -eq $mine) { Remove-Item $f.FullName -Force }"
        "  } }"
    ) % (_ps_quote(keep), _ps_quote(mine))
    try:
        subprocess.run([_powershell(), "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    except OSError:
        pass


def schedule_removal(home: str, pid: int, app_name: str) -> None:
    """Take the app off this computer, once it has stopped running.

    It cannot delete its own folder while it is sitting in it, so the work is
    handed to a small script OUTSIDE the folder, which waits for this process
    to end, removes the shortcuts and then the folder, and finally deletes
    itself.

    It only ever touches THIS app's own install folder and the shortcuts
    pointing into it. No path comes from a page and no other folder can be
    named.

    NB every line is assembled with chr() rather than written as an escape:
    this text has to survive the generator's quoting AND the generated file's,
    and a backslash written here arrives mangled at the other end.
    """
    nl = chr(10)
    q = chr(34)
    home = os.path.abspath(home)
    tmp = tempfile.gettempdir()

    if IS_MAC:
        name = app_name or "App"
        script = os.path.join(tmp, "remove-%d.sh" % pid)
        lines = [
            "#!/bin/sh",
            "while kill -0 %d 2>/dev/null; do sleep 1; done" % pid,
            "rm -rf %s" % shlex.quote(home),
            "rm -rf %s %s" % (
                shlex.quote(os.path.expanduser("~/Applications/%s.app" % name)),
                shlex.quote(os.path.expanduser("~/Desktop/%s.app" % name))),
            "rm -f " + q + "$0" + q,
        ]
        with open(script, "w", encoding="utf-8", newline=nl) as fh:
            fh.write(nl.join(lines) + nl)
        os.chmod(script, 0o755)
        subprocess.Popen(["/bin/sh", script], start_new_session=True)
        return

    # Windows: a batch file, because cmd.exe lives outside the folder being
    # removed and needs nothing installed to run.
    lnk = re.sub(BAD_IN_NAME, "", app_name or "App") + ".lnk"
    script = os.path.join(tmp, "remove-%d.cmd" % pid)
    sep = chr(92)
    lines = [
        "@echo off",
        ":wait",
        'tasklist /FI "PID eq %d" 2>nul | find "%d" >nul' % (pid, pid),
        "if not errorlevel 1 (ping -n 2 127.0.0.1 >nul & goto wait)",
        'rmdir /s /q "%s"' % home,
        'del /q "%%USERPROFILE%%' + sep + 'Desktop' + sep + '%s" 2>nul' % lnk,
        'del /q "%%APPDATA%%' + sep + 'Microsoft' + sep + 'Windows' + sep
        + 'Start Menu' + sep + 'Programs' + sep + '%s" 2>nul' % lnk,
        'del /q "%~f0"',
    ]
    with open(script, "w", encoding="utf-8", newline=chr(13) + nl) as fh:
        fh.write(nl.join(lines) + nl)
    subprocess.Popen(["cmd", "/c", script], creationflags=CREATE_NO_WINDOW)


def _unmark(folder: str) -> int:
    """Strip the "came from the internet" tag off the installed copy.

    Windows tags every file that arrives from the web, and copying a file
    copies the tag with it -- so the install inherited it from the download.
    Smart App Control then refuses to let the shortcut run the app: a signed
    interpreter being pointed at a marked script is exactly what it stops.

    Once the app is in Programs it IS a local program, and the person already
    decided to run it. The tag is an NTFS side-stream, so it deletes like a
    file. Windows only; every failure is ignored.
    """
    if IS_MAC:
        # macOS calls it com.apple.quarantine, and Gatekeeper enforces it the
        # same way -- the installed copy inherits it from the download.
        try:
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", folder],
                           capture_output=True, timeout=180)
        except Exception:
            pass
        return 0
    if not IS_WINDOWS:
        return 0
    cleared = 0
    for root, _dirs, files in os.walk(folder):
        for f in files:
            try:
                os.remove(os.path.join(root, f) + ":Zone.Identifier")
                cleared += 1
            except OSError:
                pass                     # not tagged, or in use -- both fine
    return cleared


def ensure_installed(app_dir: str, app_name: str, app_id: str = ""):
    """Returns the folder the app should actually be running from, or None if
    that's already here."""
    if os.environ.get(SKIP_ENV) or not (IS_WINDOWS or IS_MAC):
        return None
    # Never install one of the developer's own copies. The master under
    # generated/ and the throwaway under sandbox/ are meant to run exactly
    # where they are -- installing them made "Test copy" quietly hand over to
    # the customer's installed app, so a fix under test opened the wrong app
    # and looked like it had never been applied.
    # demos/ is the same: a demo app shown to a prospect must not plant itself
    # in Programs and put a desktop icon on the machine doing the showing.
    parts = [p for p in os.path.abspath(app_dir).lower().split(os.sep) if p]
    if {"sandbox", "generated", "demos"} & set(parts):
        return None
    home = install_home(app_name, app_id)
    if _same(app_dir, home):
        make_shortcuts(home, app_name)      # keep them alive if deleted
        _tidy_old_shortcuts(app_name, home)
        return None
    # Already installed: hand over to that copy. Never overwrite it -- their
    # records are in there.
    if os.path.isfile(os.path.join(home, "desktop.py")):
        _unmark(home)                    # an install made before this fix
        # Rename the icon if the business has been renamed. The copy we're
        # about to hand over to runs with setup skipped, so if this doesn't
        # happen here it never happens, and the customer keeps clicking an
        # icon with the old name on it.
        make_shortcuts(home, app_name)
        _tidy_old_shortcuts(app_name, home)
        return home
    # An install made before this app had a permanent id -- its folder is
    # named after what the business used to be called. Adopt it, records and
    # all, instead of starting an empty one beside it.
    if app_id:
        legacy = install_home(app_name, "")
        if not _same(legacy, home) and os.path.isfile(os.path.join(legacy, "desktop.py")):
            try:
                os.rename(legacy, home)
                make_shortcuts(home, app_name)
                _tidy_old_shortcuts(app_name, home)
                return home
            except OSError:
                return legacy               # in use -- keep using it as it is
    try:
        shutil.copytree(app_dir, home, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "backup", "*.pyc"))
    except OSError:
        return None                          # couldn't install -- run in place
    _unmark(home)
    # copytree keeps the mode bits, but a copy that came off a Windows-built
    # zip may never have had them -- make sure the launcher can still be run.
    if not IS_WINDOWS:
        for rel in ("OPEN-THE-APP.command",
                    os.path.join("runtime-mac", "bin", "python3"),
                    os.path.join("runtime", "bin", "python3")):
            p = os.path.join(home, rel)
            if os.path.exists(p):
                try:
                    os.chmod(p, os.stat(p).st_mode | 0o111)
                except OSError:
                    pass
    make_shortcuts(home, app_name)
    return home


def relaunch(home: str) -> bool:
    if IS_MAC:
        exe = os.path.join(home, "runtime-mac", "bin", "python3")
        if not os.path.isfile(exe):
            exe = os.path.join(home, "runtime", "bin", "python3")
        if not os.path.isfile(exe):
            exe = sys.executable
    else:
        exe = os.path.join(home, "runtime", "pythonw.exe")
        if not os.path.isfile(exe):
            exe = sys.executable
    env = os.environ.copy()
    env[SKIP_ENV] = "1"
    kw = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}
    try:
        subprocess.Popen([exe, os.path.join(home, "desktop.py")],
                         cwd=home, env=env, **kw)
        return True
    except OSError:
        return False
