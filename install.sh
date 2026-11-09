#!/usr/bin/env bash
# Install the MSXU IR reader into howdy and select it in the config.
# Idempotent: safe to re-run (e.g. after a howdy package upgrade).
#
#   sudo ./install.sh            install + patch + set recording_plugin=msxu
#   sudo ./install.sh --uninstall
set -euo pipefail

HOWDY_LIB=${HOWDY_LIB:-/usr/lib/howdy}
HOWDY_CONF=${HOWDY_CONF:-/etc/howdy/config.ini}
SRC_DIR=$(cd "$(dirname "$0")" && pwd)
VC="$HOWDY_LIB/recorders/video_capture.py"

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
[[ -f $VC ]] || { echo "howdy not found at $HOWDY_LIB"; exit 1; }

set_plugin() {
	sed -i -E "s/^(recording_plugin[[:space:]]*=[[:space:]]*).*/\1$1/" "$HOWDY_CONF"
}

if [[ ${1:-} == --uninstall ]]; then
	set_plugin opencv
	rm -f "$HOWDY_LIB/recorders/msxu_reader.py"
	for f in "$HOWDY_LIB/recorders/video_capture.py" "$HOWDY_LIB/cli/test.py"; do
		[[ -f $f.orig ]] && mv "$f.orig" "$f"
	done
	echo "recording_plugin reset to opencv; reader removed; howdy files restored"
	exit 0
fi

install -m 0644 "$SRC_DIR/msxu_reader.py" "$HOWDY_LIB/recorders/msxu_reader.py"

cp -n "$VC" "$VC.orig"
python3 - "$VC" <<'EOF'
import sys
p = sys.argv[1]
s = open(p).read()
# Upgrade the first version of this patch, which returned early and left self.fw unset
s = s.replace('\t\t\tself.internal = from_config(self.config)\n\t\t\treturn\n',
			  '\t\t\tself.internal = from_config(self.config)\n')
if 'recording_plugin == "msxu"' not in s:
	anchor = '\t\telif recording_plugin == "pyv4l2":'
	add = (
		'\t\telif recording_plugin == "msxu":\n'
		'\t\t\t# IR camera with MSXU frame illumination metadata (alternating flood light)\n'
		'\t\t\tfrom recorders.msxu_reader import from_config\n'
		'\t\t\tself.internal = from_config(self.config)\n\n'
	)
	if anchor not in s:
		sys.exit("could not find insertion point in video_capture.py")
	s = s.replace(anchor, add + anchor, 1)
open(p, "w").write(s)
EOF
echo "patched $VC (backup: $VC.orig)"

# 'howdy test' refuses any recorder but opencv; msxu supports everything it uses
TEST="$HOWDY_LIB/cli/test.py"
if [[ -f $TEST ]] && ! grep -q '"msxu"' "$TEST"; then
	cp -n "$TEST" "$TEST.orig"
	sed -i 's/if config.get("video", "recording_plugin", fallback="opencv") != "opencv":/if config.get("video", "recording_plugin", fallback="opencv") not in ("opencv", "msxu"):/' "$TEST"
	grep -q '"msxu"' "$TEST" && echo "patched $TEST (backup: $TEST.orig)" || echo "WARN: could not patch $TEST"
fi

if [[ -f $HOWDY_CONF ]]; then
	cp -n "$HOWDY_CONF" "$HOWDY_CONF.pre-msxu"
	set_plugin msxu
	# Re-enable howdy's dark frame skip (100 disables it); lit frames are never dark now
	sed -i -E 's/^(dark_threshold[[:space:]]*=[[:space:]]*)100[[:space:]]*$/\160/' "$HOWDY_CONF"
	echo "set recording_plugin = msxu in $HOWDY_CONF (backup: $HOWDY_CONF.pre-msxu)"
fi

echo "done. Test with: sudo howdy test   (debug: sudo HOWDY_MSXU_DEBUG=1 howdy test)"
echo "Then re-enrol: sudo howdy clear && sudo howdy add"
