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

# runtime-mac/ is the Mac runtime; runtime/ is the Windows one and is simply
# ignored here. A delivery can carry both, so one folder runs on either machine.
if [ -x "runtime-mac/bin/python3" ]; then
  exec "runtime-mac/bin/python3" desktop.py
fi
if [ -x "runtime/bin/python3" ]; then
  exec "runtime/bin/python3" desktop.py
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 desktop.py
fi

echo ""
echo "This app could not start because Python is not on this Mac and this"
echo "copy was delivered without its own runtime folder."
echo ""
echo "Contact Corder and ask for the Mac version."
echo ""
read -r _ 2>/dev/null
