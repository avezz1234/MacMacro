#!/bin/bash
# XTernal for Mac -- one-line installer/updater/launcher.
#
#   curl -fsSL https://raw.githubusercontent.com/avezz1234/MacMacro/main/get.sh | bash
#
# Downloads the latest version into ~/XTernal (keeping the private Python
# and packages, so updates are quick), puts an "XTernal" launcher on the
# Desktop, and runs it. Running the same line again updates and starts it.
#
# Paste ONLY the curl line above into Terminal -- not the contents of this file.
# Settings and logs live in ~/Library and are never touched by updates.

# Everything lives in main(), called on the last line: pasting part of this
# file into a terminal (instead of the curl line) defines it and does nothing.
main() {
    REPO="${XTERNAL_REPO:-avezz1234/MacMacro}"        # <-- set this to your GitHub repo
    BRANCH="${XTERNAL_BRANCH:-main}"
    DEST="$HOME/XTernal"

    say() { printf '\033[1m%s\033[0m\n' "$*"; }
    die() { printf '\033[31m%s\033[0m\n' "$*"; exit 1; }

    [ "$(uname)" = "Darwin" ] || die "This is for Macs only."

    say "Downloading XTernal..."
    TMP="$(mktemp -d)" && [ -n "$TMP" ] || die "Couldn't make a temporary folder."
    trap '[ -n "$TMP" ] && rm -rf "$TMP"' EXIT
    curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.zip" -o "$TMP/x.zip" \
        || die "Download failed. Check your internet connection and try again."
    unzip -q "$TMP/x.zip" -d "$TMP/src" || die "The download was damaged. Try again."
    FOUND="$(find "$TMP/src" -maxdepth 4 -name XTernal.command | head -n 1)"
    [ -n "$FOUND" ] || die "XTernal.command missing from the download (wrong repo?)."
    SRC="$(dirname "$FOUND")"

    # Never sync with an empty or unexpected path: --delete on "/" would be a disaster.
    [ -n "$SRC" ] && [ -d "$SRC" ] && [ -f "$SRC/XTernal.command" ] \
        || die "Download looks wrong; stopping before copying anything."
    [ -n "$HOME" ] && [ "$DEST" = "$HOME/XTernal" ] || die "Unexpected install folder; stopping."
    # Swap folders instead of syncing in place. Files the macro created while
    # running as root can sit inside the old copy; a sync can't delete them,
    # but renaming the folder only needs permission on the home folder. The
    # macro removes leftover XTernal.old-* folders itself the next time it
    # runs as root.
    NEWDIR="$DEST.new-$$"
    cp -R "$SRC" "$NEWDIR" || die "Couldn't copy files. Send a screenshot of this window."
    if [ -d "$DEST" ]; then
        for keep in .venv .python .uv; do
            if [ -e "$DEST/$keep" ]; then
                mv "$DEST/$keep" "$NEWDIR/$keep" \
                    || die "Couldn't move $keep. Send a screenshot of this window."
            fi
        done
        OLD="$DEST.old-$(date +%s)"
        if ! mv "$DEST" "$OLD"; then
            for keep in .venv .python .uv; do      # put things back as they were
                [ -e "$NEWDIR/$keep" ] && mv "$NEWDIR/$keep" "$DEST/$keep"
            done
            die "Couldn't replace the old version. Send a screenshot of this window."
        fi
        rm -rf "$OLD" 2>/dev/null || true
    fi
    mv "$NEWDIR" "$DEST" || die "Couldn't finish installing. Send a screenshot of this window."
    chmod +x "$DEST/XTernal.command" "$DEST/xternal-mac" "$DEST/run.py" "$DEST/install.sh"

    # A launcher on the Desktop: fetches the newest get.sh (so every launch is
    # also an update), falling back to the installed copy when offline.
    LAUNCHER="$HOME/Desktop/XTernal.command"
    if [ -d "$HOME/Desktop" ]; then
        cat > "$LAUNCHER" <<LAUNCH
#!/bin/bash
# XTernal launcher -- updates to the newest version, then starts it.
NEW="\$(mktemp)"
if curl -fsSL --max-time 20 "https://raw.githubusercontent.com/$REPO/$BRANCH/get.sh" -o "\$NEW" \\
   && grep -q '^main "\$@"' "\$NEW"; then
    exec bash "\$NEW"
fi
echo "(Couldn't check for updates -- starting the installed version.)"
exec bash "$DEST/XTernal.command"
LAUNCH
        chmod +x "$LAUNCHER"
    fi

    say "Installed in $DEST. Next time just double-click 'XTernal' on your Desktop."
    echo
    [ -n "$TMP" ] && rm -rf "$TMP"
    trap - EXIT
    exec bash "$DEST/XTernal.command" </dev/tty
}

main "$@"
