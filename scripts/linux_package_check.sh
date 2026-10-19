#!/bin/sh
# Install one Linux release file on a clean system and prove what D-037 promises
# (CS-421). Run as root inside a fresh container:
#
#   linux_package_check.sh deb|rpm|appimage|tarball <file> <expected version>
#
# For every format both programs report the expected version. For .deb/.rpm,
# which bring the system libraries they declare, also: no library a bundled
# file needs is missing (`ldd`), the window starts offscreen and stays up, the
# package manager removes it, nothing is left in /opt or /usr/bin, and no home
# directory gained or lost a file. The AppImage and the tar.gz carry the same
# folder and declare nothing; on a bare container only their start is checked.
set -eu
export DEBIAN_FRONTEND=noninteractive

kind="$1"
file="$(readlink -f "$2")"
expected="codexsync $3"

fail() { echo "FAIL: $*" >&2; exit 1; }

homes() { find /root /home -xdev 2>/dev/null | sort; }

check_version() {
    for program in "$@"; do
        actual="$("$program" --version)"
        [ "$actual" = "$expected" ] || fail "$program reports '$actual', expected '$expected'"
        echo "ok: $program --version"
    done
}

# Qt loads these plugins only when their own system libraries are there, and
# runs without them: a desktop theme (GTK), input devices read directly
# (evdev), GL buffer sharing on Wayland. Their gaps are reported, not refused.
OPTIONAL_PLUGINS='/plugins/platformthemes/|/plugins/generic/|/plugins/wayland-graphics-integration-client/'

check_libraries() {
    report="$(find "$1" -type f \( -name '*.so*' -o -perm -u+x \) -print0 \
        | xargs -0 -r sh -c 'for f; do file -b "$f" | grep -q ELF || continue; ldd "$f" 2>/dev/null | grep "not found" | sed "s|^|$f: |"; done' sh \
        | sort -u || true)"
    optional="$(echo "$report" | grep -E "$OPTIONAL_PLUGINS" || true)"
    missing="$(echo "$report" | grep -v -E "$OPTIONAL_PLUGINS" | grep . || true)"
    [ -z "$optional" ] || { echo "note: optional plugins without their libraries here:"; echo "$optional"; }
    [ -z "$missing" ] || { echo "$missing" >&2; fail "libraries missing on this system"; }
    echo "ok: every library the programs and the xcb/wayland/offscreen platforms need resolves"
}

check_window() {
    # A home of its own: the window keeps its size in ~/.config, which is
    # nobody's business in this check.
    smoke_home="$(mktemp -d)"
    status=0
    HOME="$smoke_home" QT_QPA_PLATFORM=offscreen timeout 15 "$1" >/tmp/window.log 2>&1 || status=$?
    [ "$status" = 124 ] || { cat /tmp/window.log >&2; fail "the window exited with $status instead of staying up"; }
    rm -rf "$smoke_home"
    echo "ok: the window starts and stays up"
}

case "$kind" in
    deb)
        apt-get update -qq
        apt-get install -y -qq file >/dev/null
        before="$(homes)"
        apt-get install -y -qq "$file" >/dev/null
        check_version /usr/bin/codexsync /usr/bin/codexsync-gui
        check_libraries /opt/codexsync
        [ -f /usr/share/applications/codexsync.desktop ] || fail "no menu entry"
        check_window /usr/bin/codexsync-gui
        apt-get remove -y -qq codexsync >/dev/null
        ;;
    rpm)
        dnf install -y -q file findutils diffutils >/dev/null
        before="$(homes)"
        dnf install -y -q "$file" >/dev/null
        check_version /usr/bin/codexsync /usr/bin/codexsync-gui
        check_libraries /opt/codexsync
        [ -f /usr/share/applications/codexsync.desktop ] || fail "no menu entry"
        check_window /usr/bin/codexsync-gui
        dnf remove -y -q codexsync >/dev/null
        ;;
    appimage)
        # No FUSE in a container: the runtime unpacks and runs instead.
        apt-get update -qq
        apt-get install -y -qq file >/dev/null
        chmod +x "$file"
        work="$(mktemp -d)"
        cd "$work"
        "$file" --appimage-extract >/dev/null
        check_version "$work/squashfs-root/AppRun"
        [ "$(APPIMAGE_EXTRACT_AND_RUN=1 "$file" --version)" = "$expected" ] || fail "the AppImage itself"
        echo "ok: the AppImage runs"
        exit 0
        ;;
    tarball)
        apt-get update -qq
        apt-get install -y -qq file >/dev/null
        work="$(mktemp -d)"
        tar -xzf "$file" -C "$work"
        folder="$(find "$work" -mindepth 1 -maxdepth 1 -type d)"
        check_version "$folder/codexsync" "$folder/codexsync-gui"
        echo "ok: the tar.gz runs"
        # A tar.gz installs no menu entry; `shortcut install` makes one in the
        # user's own folder, starting this very program (CS-399).
        data="$(mktemp -d)"
        XDG_DATA_HOME="$data" "$folder/codexsync" shortcut install >/dev/null || fail "shortcut install"
        grep -qx "TryExec=$folder/codexsync-gui" "$data/applications/codexsync.desktop" || fail "the menu entry does not start this program"
        [ -f "$data/icons/hicolor/256x256/apps/codexsync.png" ] || fail "no menu icon"
        XDG_DATA_HOME="$data" "$folder/codexsync" shortcut remove >/dev/null || fail "shortcut remove"
        [ -z "$(find "$data" -type f)" ] || fail "shortcut remove left files behind"
        echo "ok: the menu entry is made and removed"
        exit 0
        ;;
    *)
        fail "unknown kind $kind"
        ;;
esac

# Only the package formats reach here.
[ ! -e /opt/codexsync ] || fail "/opt/codexsync is still there after removal"
[ ! -e /usr/bin/codexsync ] || fail "/usr/bin/codexsync is still there after removal"
after="$(homes)"
if [ "$before" != "$after" ]; then
    echo "$before" >/tmp/homes.before
    echo "$after" >/tmp/homes.after
    diff /tmp/homes.before /tmp/homes.after >&2 || true
    fail "a home directory changed"
fi
echo "ok: installed and removed without touching any home directory"
