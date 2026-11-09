# ASUS ProArt PX13 (HN7306EAC) IR camera + howdy: findings

Investigation date: 2026-11-09. Kernel 7.3.3-1-cachyos, howdy-git r592.

## Hardware

| Item | Value |
|---|---|
| USB device | `3277:0059` Shinetech "ASUS FHD webcam", composite RGB + IR, IR function is UVC 1.50 |
| IR video node | `/dev/video2`, `GREY` 640x400 @ 15 fps (only mode) |
| IR metadata node | `/dev/video3`, formats `UVCH` and `UVCM` (UVC MSXU 1.5 metadata) |
| IR streaming interface | `bInterfaceNumber` 3 |
| HID interface 4 | HID sensor hub: human presence (`iio:device0` "prox"), ALS, accelerometer |

IR function control topology (from `lsusb -v`):

| Unit | GUID | Controls (selectors) |
|---|---|---|
| IT 9 | camera sensor | `bmControls 0x240000` (ROI + ...) |
| XU 11 | `45b5da73-23c1-4a3d-a368-610f078c4397` | none |
| XU 10 | `1229a78c-47b4-4094-b0ce-db07386fb938` (vendor) | 10, 11 (len 8, unknown) |
| XU 14 | `0f3f95dc-2632-4c4e-92c9-a04782f43bc8` (Microsoft MSXU) | 6 = FACE_AUTHENTICATION, 9 = METADATA |

The IR node exposes **no exposure, gain or emitter controls** through V4L2. Auto
exposure runs entirely in camera firmware. The only host-side levers are:

- UVC 1.5 region of interest: `region_of_interest_rectangle` + `region_of_interest_auto_ctrls`
  (bit 0 = auto exposure meters on ROI). Default auto bit is 1, but at runtime it reads 0.
- MSXU selector 6 (face auth mode). Payload `bNumEntries, {bInterface, bmMode}`:
  - `GET_CUR`/`GET_DEF` = `01 03 01` -> interface 3 in D0 (general purpose)
  - `GET_MAX` = `01 03 03` -> interface 3 supports D0 and D1 (alternate frame illumination)
  - `SET_CUR 01 03 02` accepted (this is the value tools like linux-enable-ir-emitter typically write on ASUS)
- MSXU selector 9 (metadata): `CUR = 1` (1 KiB per frame), already enabled by uvcvideo.

Vendor XU 10 selector 10 reads `00 08 00 bb bb bb bb bb`; selector 11 `GET_CUR` fails
(`EACCES`), `GET_MIN/MAX/RES` = 0 / ff.. / 1. Purpose unknown. **Not written to** (possible
emitter drive/eye-safety parameter).

## Per-frame metadata (UVCM on /dev/video3)

Each IR frame has a 38-byte metadata buffer, sequence number equal to the video buffer's:

```
00  u64  host timestamp ns
08  u16  USB SOF
0a  u8   UVC payload header length (0x1c = 28)
0b  u8   bmHeaderInfo (0x8c/0x8d, FID toggles)
0c  u32  PTS                       (present: bmHeaderInfo bit 2)
10  6B   SCR                       (present: bmHeaderInfo bit 3)
16  MSXU item: u32 MetadataId = 6 (FrameIllumination), u32 Size = 16,
               u32 Flags (bit 0 = illumination ON), u32 reserved
```

No CaptureStats (id 3) item is sent, so exposure time / gain cannot be read per frame.

## Issue 2: "flood light and capture not in sync"

Root cause is two separate firmware behaviours:

1. **Alternating illumination is by design.** The camera strobes the IR flood on every
   other frame (lit, unlit, lit, ...), in D0 and in D1 mode. Every frame is correctly
   tagged by the FrameIllumination flag: in every capture, flag=1 frames had mean
   40-250 and flag=0 frames had mean ~0.3-1.0. Windows uses this flag to drop or
   subtract unlit frames. howdy cannot see it, so half of its input is black.
2. **Per-stream emitter cut-off.** After 5-10 lit frames (0.7-1.4 s) one lit frame
   comes out black, then the camera outputs a **flat frame, every pixel = 144**, on every
   frame until streaming is stopped. Stopping and restarting the stream clears it at
   once (about 0.1 s gap). The cut-off came sooner when the scene was over-exposed
   (5 lit frames) than when exposure had converged (10 lit frames), which suggests an
   emitter energy / eye-safety budget, but this is not confirmed. Streaming the
   metadata node or switching to D1 mode does not change it.

With howdy's 4 s timeout, only about 5 of ~60 frames contain a usable lit image.

## Issue 1: exposure / face disappears

- The first lit frames of a stream are heavily **over-exposed** (face blown to white,
  up to 25% of pixels clipped at >=250).
- Firmware AE converges slowly and its state **persists across stream restarts**.
  Ten consecutive 9-frame sessions took clipping from 24% to 0% and lit-frame mean
  from ~160 to ~42, producing a clean face image.
- Within a single howdy attempt AE only sees ~5 lit frames before the cut-off, so it
  never converges.
- Bright backlight was not reproduced (tests were run in a dark room). ROI writes are
  accepted by firmware but showed no measurable effect in the dark scene; needs a
  bright-scene test.
- Unlit frames are near zero even with ambient light present in the room, so the
  sensor exposure is gated to the strobe; ambient IR (sun, halogen) only shows up
  in lit frames, on top of the flood.

## howdy side

- `compare.py` applies CLAHE and a darkness histogram check. The flat-144 frames pass
  the darkness check and are sent to face detection.
- Local config has `dark_threshold = 100`, which disables the dark-frame skip.
- The model in `/etc/howdy/models` was likely enrolled from over-exposed frames;
  re-enrol after the capture fix.

## Conclusion

No kernel driver is needed. Everything required is reachable from user space through
stock `uvcvideo`: illumination flag via `UVCM` metadata, mode via `UVCIOC_CTRL_QUERY`,
ROI via V4L2 controls. The fix is a howdy capture backend that:

1. streams video + metadata together and keeps only illuminated frames,
2. detects the cut-off (black lit frame or flat frame) and restarts the stream,
3. holds back over-exposed frames while firmware AE converges,
4. optionally subtracts the preceding unlit frame to remove ambient IR,
5. optionally steers AE with the UVC ROI (pending bright-scene test).

## Update: cut-off timing is variable

Later runs (same evening, user in frame but not looking at the screen) gave
9, 10, 21, 29, 52, 99 and 199+ lit frames before the cut-off. Polling the vendor
XU 10 / MSXU XU 14 controls (GET only) or reading the presence-sensor sysfs files
every frame made no consistent difference. The trigger is therefore probably the
scene (presence, gaze, distance or brightness) rather than a fixed time budget.
`tools/cutoff_test.py` runs a guided per-scenario measurement.

Vendor XU 10 selector 10 has read `03 08 00 bb bb bb bb bb` since the D1 mode test
(it was `00 08 ...` before) and stayed constant during streaming and after cut-off.
The presence sensor's sysfs values stay at `prox0=0 prox1=65535 attention=25500`,
so it does not seem to be active unless something enables it.

## Prototype: msxu_reader (phase 1/2)

`msxu_reader.py` implements points 1-4 of the fix. Measured in a dark room:

- 30 usable lit frames in 5.8 s (5.2 usable fps), 3 cut-offs recovered automatically,
  0 unpaired metadata buffers. Restarting the stream costs about one frame period.
- The warm-up gate held back 8 over-exposed frames (clipping 18% -> 7%); the first
  usable frame arrived after 1.3 s. When firmware AE is already converged, the first
  frame arrives after ~0.27 s.
- After each restart, AE briefly overshoots (mean ~45 -> ~70, ~1% clipping) and then
  settles back within ~4 lit frames.
- Works through howdy's own `VideoCapture` once `install.sh` patches it in.
- 2026-11-09: installed with `install.sh`; `howdy test` and face recognition work in a
  dark room with the existing model (no re-enrolment needed). Bright backlight still
  untested.

## Windows driver packages (static inspection, 2026-11-09)

- **ASUS support site** (model HN7306EAC) lists one camera package: "Realtek
  Camera_Extension" V10.0.22000.10002. It is an extension INF (`RtExtAsus.inf`) for the
  **RGB** interface only (`USB\VID_3277&PID_0059&MI_00`). It sets
  `FSColorInfoOverride = 0x01050102` (BT.709 colour info) and does nothing for IR.
- **Microsoft Update Catalog**, "Realtek DMFT - IR" 10.0.22000.20418: 21 OEM INFs plus
  `RsDMFTIR64.dll`. None of them lists `3277:0059` (siblings such as 3277:008B/0107/0108
  use `RS_IR_CAMERA_NO_DMFT`). On Windows, this IR interface is most likely driven by
  inbox `usbvideo.sys` with no vendor DMFT; Windows Hello then uses the standard MSXU
  controls and FrameIllumination metadata, which is what msxu_reader does too.
  Not every catalog package was checked: about 12 distinct packages of ~58 MB each.
- IR INF sections for related ASUS cameras only set standard Windows Hello keys:
  `SensorCameraMode=1`, `SkipCameraEnumeration=1`, `EnableDshowRedirection=1`,
  optional `UVC-CPV2FaceAuth=0xFFFF0000`, and a DMFT CLSID
  `{9ED13C9E-2FEE-4EDB-91F1-95B32AACF0F4}`.
- The Realtek IR DMFT logs `Write EXT_FACEAUTHMODE=2`, so on Windows the IR stream runs
  in MSXU mode D1 (alternate frame illumination). For older cameras it also reads an
  illumination flag embedded in the pixel data (`GetIRLEDFlag_Ytag/UVtag`); ours uses
  metadata.
- The DMFT's Realtek extension unit (GUID `1229a78c-...`) control name table maps
  selector = index + 1. Our XU 10 exposes selectors 10 and 11:
  - 10 = `RTK_EXT_PHOTO_SCENEMODE_CTL`
  - 11 = `RTK_EXT_MANUAL_FLASH_CAPTURE`

  The RGB function exposes the same pair. These are generic stills controls, not an IR
  emitter, AE or watchdog control. Other names in the table (not exposed by this
  camera) include `AE_LOCK`, `LED_TORCH`, `LED_FLASH`, `MIRAMATRIX_IR_LED` (IR LED for
  Mirametrix attention sensing), `ROI_MODE`/`ROI`, `3ASTS`, `IDEAEYE_*`, `IQ_MODE`.
- Conclusion: Windows has no hidden vendor command for this IR camera. The emitter
  cut-off is firmware behaviour that Windows Hello's short capture sessions never hit,
  so restarting the stream (as msxu_reader does) is the right handling.

## Open questions

- What triggers the emitter cut-off? Vendor XU 10 is ruled out (see the Windows driver
  section). The remaining option is a Windows VM with USB passthrough and `usbmon`
  capture, to see whether Hello hits the cut-off at all.
- Does the human presence sensor influence the IR pipeline?
- ~~Does ROI metering help with a bright backlight?~~ Yes, see below.
- ROI AE not yet re-checked in a dark room (expected fine: face is in the ROI).

## Bright backlight fix: ROI auto exposure (2026-11-10)

Reproduced with a daylit curtain/window behind the user (top right of the frame).
Firmware AE meters the whole frame, so it exposes for the window: lit-frame mean
~17, 0% clipping, face region mean ~9.5 (face barely visible). howdy failed.

- Setting `region_of_interest_rectangle` + `region_of_interest_auto_ctrls = 1`
  **does work in a bright scene**: face mean 9.5 -> ~90-120 within ~8 lit frames
  (~1-2 s from cold). The window clips (~14% of the frame), which is harmless.
- A broad centre ROI `(128, 60, 384, 340)` works as well as a tight face box.
- The write is **ignored by firmware while the stream is off** (control reads back
  set, AE does not react). It must be written after `STREAMON`.
- Once applied, firmware keeps ROI metering and AE state across stream close/reopen,
  so later howdy attempts start converged (first frame after ~0.2 s).
- With ROI AE, ~5% of the ROI can legitimately clip (bright curtain inside it). The
  old 2% warm-up saturation gate then held frames back for the full 1.5 s warm-up.
  The gate now measures clipping inside the ROI only, default threshold 10%
  (original over-exposure case was 18-25%).

msxu_reader now writes the ROI after every stream start (`msxu_roi = l,t,w,h` in
`[video]`, or `off`). After reinstalling, `compare.py` matched 3/3 against the
backlight, each on the second frame, with the existing model.
