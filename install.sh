#!/usr/bin/env bash
# Patch an existing howdy install with the MSXU IR reader and select it in the config.
# For users who keep their distro's howdy package. The howdy-px13-git package in pkg/
# applies the same patch at build time instead.
#
# Idempotent: safe to re-run, e.g. after a howdy upgrade overwrote the patched files.
#
#   sudo ./install.sh              install reader, patch howdy, set recording_plugin = msxu
#   sudo ./install.sh --uninstall  restore howdy's files and recording_plugin = opencv
set -euo pipefail

HOWDY_LIB=${HOWDY_LIB:-/usr/lib/howdy}
HOWDY_CONF=${HOWDY_CONF:-/etc/howdy/config.ini}
SRC_DIR=$(cd "$(dirname "$0")" && pwd)
PATCH="$SRC_DIR/patches/howdy-msxu.patch"
# Optional: fixes a crash in `howdy test` with OpenCV 5. Skipped when cli/test.py
# was already fixed some other way.
HIST_PATCH="$SRC_DIR/patches/howdy-test-opencv5-hist.patch"
# Paths in the patches are relative to the howdy source tree (a/howdy/src/...)
STRIP=-p3
PATCHED_FILES=(recorders/video_capture.py cli/test.py)

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
[[ -f $HOWDY_LIB/recorders/video_capture.py ]] || { echo "howdy not found at $HOWDY_LIB"; exit 1; }
command -v patch >/dev/null || { echo "'patch' is required (pacman -S patch)"; exit 1; }

set_plugin() {
	sed -i -E "s/^(recording_plugin[[:space:]]*=[[:space:]]*).*/\1$1/" "$HOWDY_CONF"
}

is_patched() {
	patch -d "$HOWDY_LIB" $STRIP -R --dry-run -s -f < "${1:-$PATCH}" >/dev/null 2>&1
}

if [[ ${1:-} == --uninstall ]]; then
	[[ -f $HOWDY_CONF ]] && set_plugin opencv
	rm -f "$HOWDY_LIB/recorders/msxu_reader.py"
	# Reverse in the opposite order of install
	if is_patched "$HIST_PATCH"; then
		patch -d "$HOWDY_LIB" $STRIP -R -s --no-backup-if-mismatch < "$HIST_PATCH"
	fi
	if is_patched; then
		patch -d "$HOWDY_LIB" $STRIP -R -s < "$PATCH"
	fi
	for f in "${PATCHED_FILES[@]}"; do
		rm -f "$HOWDY_LIB/$f.orig"
	done
	echo "reader removed, howdy files restored, recording_plugin reset to opencv"
	exit 0
fi

install -m 0644 "$SRC_DIR/msxu_reader.py" "$HOWDY_LIB/recorders/msxu_reader.py"

if is_patched; then
	echo "howdy already patched"
elif patch -d "$HOWDY_LIB" $STRIP --dry-run -s -f < "$PATCH" >/dev/null; then
	patch -d "$HOWDY_LIB" $STRIP -s -b -z .orig < "$PATCH"
	echo "patched ${PATCHED_FILES[*]} in $HOWDY_LIB (backups: *.orig)"
else
	echo "howdy's files differ from what the patch expects (different howdy version?)." >&2
	echo "Nothing was patched. Check: patch -d $HOWDY_LIB $STRIP --dry-run < $PATCH" >&2
	exit 1
fi

if is_patched "$HIST_PATCH"; then
	:
elif patch -d "$HOWDY_LIB" $STRIP --dry-run -s -f < "$HIST_PATCH" >/dev/null 2>&1; then
	# No backup: cli/test.py.orig already holds the pre-install file
	patch -d "$HOWDY_LIB" $STRIP -s --no-backup-if-mismatch < "$HIST_PATCH"
	echo "patched cli/test.py for OpenCV 5 (howdy test histogram)"
else
	echo "note: skipped OpenCV 5 histogram fix, cli/test.py already differs (fixed locally?)"
fi

if [[ -f $HOWDY_CONF ]]; then
	cp -n "$HOWDY_CONF" "$HOWDY_CONF.pre-msxu"
	set_plugin msxu
	# Re-enable howdy's dark frame skip (100 disables it); lit frames are never dark now
	sed -i -E 's/^(dark_threshold[[:space:]]*=[[:space:]]*)100[[:space:]]*$/\160/' "$HOWDY_CONF"
	echo "set recording_plugin = msxu in $HOWDY_CONF (backup: $HOWDY_CONF.pre-msxu)"
fi

echo "done. Test with: sudo howdy test   (debug: sudo HOWDY_MSXU_DEBUG=1 howdy test)"
