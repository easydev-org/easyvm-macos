#!/bin/bash
set -euo pipefail

# AN SSH COMMAND THAT IS NOT A LOGIN SHELL HAS A PATH OFTEN MISSING /usr/bin, AND `xip` LIVES THERE.
#
# MEASURED ON RUN 44899198589, `macos-dev` step 6/21 "expand and select xcode":
#
#	ssh: bash /tmp/run-xcode-install.sh -> 127
#
# 127 IS "command not found", and the script's first real command is `xip -x`. `bash` itself ran -
# the shell was found - so what was missing is a command the script names and does not qualify.
# `set -e` then ends the script at the lookup, and the ssh wrapper reports only the number.
#
# THE FIX IS BOTH THE PATH AND THE DIAGNOSIS: `/usr/bin` is where Apple's own tools are, and the
# echo makes the next such failure name its command instead of a number.
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
for tool in xip ditto find xcode-select; do
  if ! command -v "$tool" > /dev/null 2>&1; then
    echo "MISSING TOOL: $tool (PATH=$PATH)" >&2
    exit 1
  fi
done
echo "PATH ok; xip at $(command -v xip)"

# THE PASSWORD IS AN ARGUMENT BECAUSE SUDO OVER SSH HAS NO TTY: `sudo` on this channel answers
# "a password is required" and has nowhere to read it from.
PASSWORD='{{ password }}'
XIP=/tmp/Xcode.xip
DEST=/Applications
WORK=/Users/{{ user }}/xcode-expand

# `xip -x` WRITES THE EXPANDED BUNDLE BESIDE A DIRECTORY, so the working directory decides
# where 15 GiB lands. It goes in the user's home, not /tmp, whose volume is small on macOS.
rm -rf "$WORK"
mkdir -p "$WORK"
cd "$WORK"
echo "expanding $XIP (this prints nothing for about twenty minutes)"
xip -x "$XIP"
echo "expanded"

APP="$(/usr/bin/find "$WORK" -maxdepth 2 -name 'Xcode*.app' -type d | head -1)"
if [ -z "$APP" ]; then
  echo "NO Xcode.app in $WORK after xip -x" >&2
  ls -la "$WORK" >&2
  exit 1
fi
echo "found $APP"

# MOVED WITH `ditto`, which preserves the code signature and the extended attributes that a
# plain `mv` across volumes can drop.
rm -rf "$DEST/Xcode.app"
/usr/bin/ditto "$APP" "$DEST/Xcode.app"

# THE LICENCE FIRST, BECAUSE EVERYTHING ELSE ASKS XCODE A QUESTION.
if [ -x /usr/bin/sudo ]; then
  printf '%s\n' "$PASSWORD" | sudo -S -p '' /usr/bin/xcode-select -s "$DEST/Xcode.app/Contents/Developer"
  printf '%s\n' "$PASSWORD" | sudo -S -p '' /usr/bin/xcodebuild -license accept
  printf '%s\n' "$PASSWORD" | sudo -S -p '' /usr/bin/xcodebuild -runFirstLaunch
else
  /usr/bin/xcode-select -s "$DEST/Xcode.app/Contents/Developer"
  /usr/bin/xcodebuild -license accept
  /usr/bin/xcodebuild -runFirstLaunch
fi
echo "selected: $(/usr/bin/xcode-select -p)"

# AND THE DISK IT WAS EXPANDED INTO IS REMOVED, because it is 15 GiB and nothing reads it again.
rm -rf "$WORK"
