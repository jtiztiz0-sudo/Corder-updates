#!/bin/sh
# Double-click this to open the app.
#
# runtime/ is this app's own private Python -- used first and on its own, so
# the app cannot be broken by whatever Python is or isn't installed on this
# Mac, or by one being installed/removed later.
#
# If there is no runtime/ folder (a delivery that was not bundled), fall back
# to a system python3.

cd "$(dirname "$0")" || exit 1

# --- refuse to run from inside a zip / the quarantine unpack area ----------
# Double-clicking a .zip in Finder normally unpacks beside it, but opening one
# from Mail or a browser preview can leave it under /private/var/folders or
# /tmp, which macOS clears out. The app would run, they would type a week of
# records into it, and they would go with the temp folder.
case "$PWD" in
  /private/var/folders/*|/var/folders/*|/tmp/*|*/AppTranslocation/*)
    echo ""
    echo "  This is still inside the zip (or a temporary copy)."
    echo ""
    echo "  Drag the folder out to somewhere it can stay -- your"
    echo "  Applications folder or your home folder are both fine --"
    echo "  and open it from there."
    echo ""
    echo "  Anything typed in from here would be deleted by macOS, so the"
    echo "  app will not start until it has been moved out."
    echo ""
    read -r _ 2>/dev/null
    exit 1
    ;;
esac

# --- clear the quarantine flag off everything --------------------------------
# macOS tags every file that came from the internet, and Gatekeeper refuses to
# run a tagged program. Getting THIS file open takes a one-time right-click ->
# Open (there is no way round that without an Apple developer certificate) --
# but the bundled python next to it is tagged too, so without this the app
# would be blocked all over again the moment this script tried to start it.
#
# We are already running, so the person has consented. Strip the tag from the
# whole folder, once, and nothing else gets stopped. Errors ignored: if xattr
# is missing or a file is read-only, carry on and let the app try.
if [ -d "app" ]; then
  xattr -dr com.apple.quarantine "$PWD" 2>/dev/null || true
fi

# runtime-mac/ is the Mac runtime; runtime/ is the Windows one and is simply
# ignored here. A delivery can carry both, so one folder runs on either machine.
#
# Tested with -f (is it there?) and not -x (is it runnable?) on purpose. The
# execute bit is a thing that gets lost -- copied through a USB stick or a
# Windows machine, restored from a backup, unpacked by the wrong tool. The
# interpreter is sitting right there, so put the bit back instead of falling
# through to a system python3 that has none of the libraries this app needs:
# that path fails LATER, with an import error nobody can act on, or prints
# "Python is not on this Mac" when it plainly is.
for py in "runtime-mac/bin/python3" "runtime/bin/python3"; do
  if [ -f "$py" ]; then
    [ -x "$py" ] || chmod +x "$py" 2>/dev/null
    if [ -x "$py" ]; then
      exec "$py" desktop.py
    fi
  fi
done

if command -v python3 >/dev/null 2>&1; then
  exec python3 desktop.py
fi

echo ""
echo "This app could not start because Python is not on this Mac and this"
echo "copy was delivered without its own runtime folder."
echo ""
echo "Contact JTS and ask for the Mac version."
echo ""
read -r _ 2>/dev/null
