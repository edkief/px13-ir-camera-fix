# px13-ir-camera-fix

Makes [howdy](https://github.com/boltgolt/howdy) face login work reliably with the IR camera
of the ASUS ProArt PX13 (HN7306EAC, USB `3277:0059`).

## What's wrong with stock howdy on this camera

- The IR flood light is on only for every other frame, so half of the frames howdy sees are black.
- After some lit frames, the camera firmware turns the emitter off for the rest of the stream.
- Firmware auto exposure averages over the whole frame. A bright window or lamp behind you
  leaves your face too dark to recognise.

`msxu_reader.py` is a howdy recorder (`recording_plugin = msxu`) that handles all three
problems. It reads the per-frame Microsoft (MSXU) illumination metadata to keep only lit
frames, and restarts the stream when the emitter cuts off. It also points auto exposure at
the centre of the frame through the standard UVC region-of-interest control. It runs on the
stock `uvcvideo` driver: no kernel module, and nothing vendor-specific is written to the
camera.

[FINDINGS.md](FINDINGS.md) has the full investigation.

## Install

Pick one.

### Option 1: package (Arch)

`pkg/` builds `howdy-px13-git`. It is the `howdy-git` package with the patches applied at
build time, and it replaces `howdy` or `howdy-git`. Every file belongs to the package, and
howdy upgrades come through rebuilding it.

```sh
cd pkg && makepkg -si
```

The shipped `/etc/howdy/config.ini` defaults to the PX13 IR camera and the msxu recorder.
If you already have a config, pacman keeps it and writes the new defaults to
`config.ini.pacnew`. Set `recording_plugin = msxu` yourself.

### Option 2: patch an existing howdy install

For any distro, or to keep your current howdy package:

```sh
sudo ./install.sh              # install reader, patch howdy, set recording_plugin = msxu
sudo ./install.sh --uninstall  # undo
```

A howdy package upgrade overwrites the patched files. Re-run `install.sh` afterwards. It
detects whether the patch is already applied, and it refuses to touch files from a howdy
version the patch doesn't match.

## Configuration

Optional keys in the `[video]` section of `/etc/howdy/config.ini`:

| Key | Default | Meaning |
|---|---|---|
| `msxu_roi` | `128,60,384,340` | Auto exposure metering region (`left,top,width,height`), or `off` |
| `msxu_warmup` | `1.5` | Seconds after start during which over-exposed frames are skipped |
| `msxu_max_saturation` | `0.10` | Fraction of clipped pixels in the region that counts as over-exposed |
| `msxu_subtract_ambient` | `false` | Subtract the neighbouring unlit frame to remove ambient IR |
| `msxu_faceauth_mode` | unset | Write MSXU face-auth mode (e.g. `2`) to the camera at start |

## Troubleshooting

```sh
sudo HOWDY_MSXU_DEBUG=1 howdy test           # per-frame log: lit/unlit, brightness, cut-offs
python3 msxu_reader.py --frames 30 --save /tmp/ir   # standalone capture to PGM files
```

## Layout

- `msxu_reader.py`: the recorder
- `patches/howdy-msxu.patch`: howdy changes, used by `install.sh` and the package
- `patches/howdy-test-opencv5-hist.patch`: fixes a `howdy test` crash with OpenCV 5
  (`IndexError: invalid index to scalar variable`). Taken from upstream PR
  [boltgolt/howdy#1128](https://github.com/boltgolt/howdy/pull/1128); drop it once that is
  merged. Applied by the package; `install.sh` applies it when `cli/test.py` hasn't already
  been fixed some other way.
- `patches/howdy-msxu-package.patch`: package only (meson install list, config defaults)
- `pkg/`: Arch package (`howdy-px13-git`)
- `tools/`: probe scripts used during the investigation
