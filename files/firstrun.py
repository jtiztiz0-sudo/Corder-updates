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
import shutil
import subprocess

# Set on the copy we relaunch, so an install can never trigger another one.
SKIP_ENV = "APP_SKIP_INSTALL"
CREATE_NO_WINDOW = 0x08000000
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"


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
cd "%(home)s" || exit 1
if [ -x "runtime-mac/bin/python3" ]; then
  exec "runtime-mac/bin/python3" desktop.py
fi
if [ -x "runtime/bin/python3" ]; then
  exec "runtime/bin/python3" desktop.py
fi
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
    keep = re.sub(r'[\/:*?"<>|]', "", current_name or "").strip() + ".lnk"
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
    parts = [p for p in os.path.abspath(app_dir).lower().split(os.sep) if p]
    if "sandbox" in parts or "generated" in parts:
        return None
    home = install_home(app_name, app_id)
    if _same(app_dir, home):
        make_shortcuts(home, app_name)      # keep them alive if deleted
        _tidy_old_shortcuts(app_name, home)
        return None
    # Already installed: hand over to that copy. Never overwrite it -- their
    # records are in there.
    if os.path.isfile(os.path.join(home, "desktop.py")):
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
