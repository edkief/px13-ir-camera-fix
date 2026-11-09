"""
howdy recorder for UVC 1.5 IR cameras that alternate the IR flood light
per frame and tag each frame with Microsoft (MSXU) FrameIllumination metadata.

Written for the ASUS ProArt PX13 IR camera (USB 3277:0059), see FINDINGS.md.

What it does, compared to plain OpenCV capture:

* streams the IR video node and its UVC metadata node together and pairs
  buffers by sequence number;
* returns only frames captured with the IR flood ON;
* detects the firmware's per-stream emitter cut-off (a black lit frame, or a
  flat constant frame) and restarts streaming, which clears it;
* while firmware auto exposure is still converging, holds back over-exposed
  lit frames (bounded by a warm-up time);
* optionally subtracts the neighbouring unlit frame to remove ambient IR;
* points firmware auto exposure at a UVC region of interest (default: the
  centre of the frame, where the face is), so a bright window or lamp behind
  the user no longer drives exposure down and leaves the face dark.

Exposes the subset of the cv2.VideoCapture API howdy uses:
read(), grab(), get(), set(), release().

Can also be run directly for testing:  python3 msxu_reader.py --help
"""

import ctypes
import errno
import struct
import fcntl
import mmap
import os
import select
import sys
import time

import numpy as np

try:
	import cv2
	CAP_PROP_FRAME_WIDTH = cv2.CAP_PROP_FRAME_WIDTH
	CAP_PROP_FRAME_HEIGHT = cv2.CAP_PROP_FRAME_HEIGHT
except ImportError:  # standalone use without OpenCV
	cv2 = None
	CAP_PROP_FRAME_WIDTH, CAP_PROP_FRAME_HEIGHT = 3, 4

# ---------------------------------------------------------------------------
# V4L2 / UVC ABI (64-bit Linux)
# ---------------------------------------------------------------------------

_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction, nr, size, typ='V'):
	return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


class timeval(ctypes.Structure):
	_fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class v4l2_timecode(ctypes.Structure):
	_fields_ = [("type", ctypes.c_uint32), ("flags", ctypes.c_uint32),
				("frames", ctypes.c_uint8), ("seconds", ctypes.c_uint8),
				("minutes", ctypes.c_uint8), ("hours", ctypes.c_uint8),
				("userbits", ctypes.c_uint8 * 4)]


class _buf_m(ctypes.Union):
	_fields_ = [("offset", ctypes.c_uint32), ("userptr", ctypes.c_ulong),
				("planes", ctypes.c_void_p), ("fd", ctypes.c_int32)]


class v4l2_buffer(ctypes.Structure):
	_fields_ = [("index", ctypes.c_uint32), ("type", ctypes.c_uint32),
				("bytesused", ctypes.c_uint32), ("flags", ctypes.c_uint32),
				("field", ctypes.c_uint32), ("timestamp", timeval),
				("timecode", v4l2_timecode), ("sequence", ctypes.c_uint32),
				("memory", ctypes.c_uint32), ("m", _buf_m),
				("length", ctypes.c_uint32), ("reserved2", ctypes.c_uint32),
				("request_fd", ctypes.c_int32)]


class v4l2_requestbuffers(ctypes.Structure):
	_fields_ = [("count", ctypes.c_uint32), ("type", ctypes.c_uint32),
				("memory", ctypes.c_uint32), ("capabilities", ctypes.c_uint32),
				("flags", ctypes.c_uint8), ("reserved", ctypes.c_uint8 * 3)]


class v4l2_pix_format(ctypes.Structure):
	_fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
				("pixelformat", ctypes.c_uint32), ("field", ctypes.c_uint32),
				("bytesperline", ctypes.c_uint32), ("sizeimage", ctypes.c_uint32)]


class v4l2_meta_format(ctypes.Structure):
	_fields_ = [("dataformat", ctypes.c_uint32), ("buffersize", ctypes.c_uint32)]


class _fmt_u(ctypes.Union):
	_fields_ = [("pix", v4l2_pix_format), ("meta", v4l2_meta_format),
				("raw", ctypes.c_uint8 * 200), ("_align", ctypes.c_uint64)]


class v4l2_format(ctypes.Structure):
	_fields_ = [("type", ctypes.c_uint32), ("fmt", _fmt_u)]


class v4l2_capability(ctypes.Structure):
	_fields_ = [("driver", ctypes.c_char * 16), ("card", ctypes.c_char * 32),
				("bus_info", ctypes.c_char * 32), ("version", ctypes.c_uint32),
				("capabilities", ctypes.c_uint32), ("device_caps", ctypes.c_uint32),
				("reserved", ctypes.c_uint32 * 3)]


class uvc_xu_control_query(ctypes.Structure):
	_fields_ = [("unit", ctypes.c_uint8), ("selector", ctypes.c_uint8),
				("query", ctypes.c_uint8), ("size", ctypes.c_uint16),
				("data", ctypes.c_void_p)]


RW = _IOC_READ | _IOC_WRITE
VIDIOC_QUERYCAP = _ioc(_IOC_READ, 0, ctypes.sizeof(v4l2_capability))
VIDIOC_G_FMT = _ioc(RW, 4, ctypes.sizeof(v4l2_format))
VIDIOC_S_FMT = _ioc(RW, 5, ctypes.sizeof(v4l2_format))
VIDIOC_REQBUFS = _ioc(RW, 8, ctypes.sizeof(v4l2_requestbuffers))
VIDIOC_QUERYBUF = _ioc(RW, 9, ctypes.sizeof(v4l2_buffer))
VIDIOC_QBUF = _ioc(RW, 15, ctypes.sizeof(v4l2_buffer))
VIDIOC_DQBUF = _ioc(RW, 17, ctypes.sizeof(v4l2_buffer))
VIDIOC_STREAMON = _ioc(_IOC_WRITE, 18, ctypes.sizeof(ctypes.c_int))
VIDIOC_STREAMOFF = _ioc(_IOC_WRITE, 19, ctypes.sizeof(ctypes.c_int))
UVCIOC_CTRL_QUERY = _ioc(RW, 0x21, ctypes.sizeof(uvc_xu_control_query), 'u')

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_BUF_TYPE_META_CAPTURE = 13
V4L2_MEMORY_MMAP = 1
V4L2_CAP_META_CAPTURE = 0x00800000
V4L2_PIX_FMT_GREY = int.from_bytes(b"GREY", "little")
V4L2_META_FMT_UVC_MSXU_1_5 = int.from_bytes(b"UVCM", "little")

UVC_SET_CUR = 0x01

# V4L2 extended controls (UVC 1.5 region of interest)
_EXT_CTRL_SIZE = 20  # struct v4l2_ext_control is packed: id, size, reserved2, union(8)
_EXT_CTRLS_SIZE = 32  # which, count, error_idx, request_fd, reserved, pad, controls ptr
VIDIOC_S_EXT_CTRLS = _ioc(RW, 72, _EXT_CTRLS_SIZE)
V4L2_CTRL_CLASS_USER = 0x00980000
V4L2_CID_UVC_REGION_OF_INTEREST_RECT = 0x00981ae1
V4L2_CID_UVC_REGION_OF_INTEREST_AUTO = 0x00981ae2
V4L2_UVC_REGION_OF_INTEREST_AUTO_EXPOSURE = 0x1

# Default AE metering region for 640x400: centre, lower 85% (excludes a window
# or ceiling light at the top edges). Tested 2026-11-10 against a daylit window
# behind the user: face mean 9 -> 90.
DEFAULT_ROI = (128, 60, 384, 340)

# MSXU metadata
MSXU_ID_FRAME_ILLUMINATION = 6
MSXU_ILLUMINATION_ON = 0x1

# ---------------------------------------------------------------------------


def _debug_enabled():
	return bool(os.environ.get("HOWDY_MSXU_DEBUG"))


def parse_illumination(meta):
	"""
	Parse one UVCM metadata buffer (struct uvc_meta_buf followed by the rest of
	the UVC payload header, which carries MSXU items).

	Returns True (flood on), False (flood off) or None (no FrameIllumination item).
	"""
	if len(meta) < 12:
		return None
	hdr_len = meta[10]  # UVC bHeaderLength, includes bHeaderLength + bmHeaderInfo
	info = meta[11]     # bmHeaderInfo
	off = 12
	end = min(len(meta), 12 + max(0, hdr_len - 2))
	if info & 0x04:  # PTS
		off += 4
	if info & 0x08:  # SCR
		off += 6
	while off + 8 <= end:
		item_id = int.from_bytes(meta[off:off + 4], "little")
		size = int.from_bytes(meta[off + 4:off + 8], "little")
		if size < 8 or off + size > end:
			break
		if item_id == MSXU_ID_FRAME_ILLUMINATION and size >= 12:
			flags = int.from_bytes(meta[off + 8:off + 12], "little")
			return bool(flags & MSXU_ILLUMINATION_ON)
		off += size
	return None


def find_meta_node(video_path):
	"""Find the metadata node that belongs to the same USB interface as video_path."""
	name = os.path.basename(os.path.realpath(video_path))
	sys_base = "/sys/class/video4linux"
	try:
		iface = os.path.realpath(os.path.join(sys_base, name, "device"))
	except OSError:
		return None
	for entry in sorted(os.listdir(sys_base)):
		if entry == name:
			continue
		if os.path.realpath(os.path.join(sys_base, entry, "device")) != iface:
			continue
		path = "/dev/" + entry
		try:
			fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
		except OSError:
			continue
		try:
			cap = v4l2_capability()
			fcntl.ioctl(fd, VIDIOC_QUERYCAP, cap)
			if cap.device_caps & V4L2_CAP_META_CAPTURE:
				return path
		except OSError:
			pass
		finally:
			os.close(fd)
	return None


def xu_set(fd, unit, selector, payload):
	buf = (ctypes.c_uint8 * len(payload))(*payload)
	q = uvc_xu_control_query(unit, selector, UVC_SET_CUR, len(payload), ctypes.addressof(buf))
	fcntl.ioctl(fd, UVCIOC_CTRL_QUERY, q)


def set_ae_roi(fd, rect):
	"""Make firmware auto exposure meter on rect = (left, top, width, height)."""
	l, t, w, h = rect
	r = ctypes.create_string_buffer(struct.pack("<iiII", l, t, w, h), 16)
	ctrls = ctypes.create_string_buffer(2 * _EXT_CTRL_SIZE)
	struct.pack_into("<IIIQ", ctrls, 0, V4L2_CID_UVC_REGION_OF_INTEREST_RECT, 16, 0, ctypes.addressof(r))
	struct.pack_into("<IIIq", ctrls, _EXT_CTRL_SIZE, V4L2_CID_UVC_REGION_OF_INTEREST_AUTO, 0, 0,
					 V4L2_UVC_REGION_OF_INTEREST_AUTO_EXPOSURE)
	arg = ctypes.create_string_buffer(struct.pack("<IIIiI4xQ", V4L2_CTRL_CLASS_USER, 2, 0, 0, 0,
												  ctypes.addressof(ctrls)), _EXT_CTRLS_SIZE)
	fcntl.ioctl(fd, VIDIOC_S_EXT_CTRLS, arg)


class _Stream:
	"""One mmap streaming V4L2 node."""

	def __init__(self, path, buf_type, nbufs=4):
		self.path = path
		self.type = buf_type
		self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
		self.maps = []
		self.nbufs = nbufs
		self.streaming = False

	def set_meta_format(self, fourcc):
		fmt = v4l2_format()
		fmt.type = self.type
		fmt.fmt.meta.dataformat = fourcc
		fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)
		return fmt.fmt.meta.dataformat == fourcc

	def get_pix_format(self):
		fmt = v4l2_format()
		fmt.type = self.type
		fcntl.ioctl(self.fd, VIDIOC_G_FMT, fmt)
		return fmt.fmt.pix

	def alloc(self):
		req = v4l2_requestbuffers(count=self.nbufs, type=self.type, memory=V4L2_MEMORY_MMAP)
		fcntl.ioctl(self.fd, VIDIOC_REQBUFS, req)
		for i in range(req.count):
			b = v4l2_buffer(index=i, type=self.type, memory=V4L2_MEMORY_MMAP)
			fcntl.ioctl(self.fd, VIDIOC_QUERYBUF, b)
			self.maps.append(mmap.mmap(self.fd, b.length, mmap.MAP_SHARED,
									   mmap.PROT_READ | mmap.PROT_WRITE, offset=b.m.offset))

	def queue_all(self):
		for i in range(len(self.maps)):
			self.qbuf(i)

	def qbuf(self, index):
		b = v4l2_buffer(index=index, type=self.type, memory=V4L2_MEMORY_MMAP)
		fcntl.ioctl(self.fd, VIDIOC_QBUF, b)

	def dqbuf(self):
		"""Return (sequence, bytes) or None when no buffer is ready."""
		b = v4l2_buffer(type=self.type, memory=V4L2_MEMORY_MMAP)
		try:
			fcntl.ioctl(self.fd, VIDIOC_DQBUF, b)
		except OSError as e:
			if e.errno == errno.EAGAIN:
				return None
			raise
		data = bytes(self.maps[b.index][:b.bytesused])
		self.qbuf(b.index)
		return b.sequence, data

	def on(self):
		fcntl.ioctl(self.fd, VIDIOC_STREAMON, ctypes.c_int(self.type))
		self.streaming = True

	def off(self):
		if self.streaming:
			fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, ctypes.c_int(self.type))
			self.streaming = False

	def close(self):
		try:
			self.off()
		except OSError:
			pass
		for m in self.maps:
			m.close()
		self.maps = []
		try:
			req = v4l2_requestbuffers(count=0, type=self.type, memory=V4L2_MEMORY_MMAP)
			fcntl.ioctl(self.fd, VIDIOC_REQBUFS, req)
		except OSError:
			pass
		os.close(self.fd)


class msxu_reader:
	"""cv2.VideoCapture-like reader that only returns IR-illuminated frames."""

	def __init__(self, device_path, meta_path=None, subtract_ambient=False,
				 max_saturation=0.10, warmup=1.5, black_level=4.0,
				 faceauth_mode=None, frame_timeout=1.0, max_restarts=20, roi=DEFAULT_ROI):
		self.device_path = device_path
		self.meta_path = meta_path or find_meta_node(device_path)
		self.subtract_ambient = subtract_ambient
		self.max_saturation = max_saturation
		self.warmup = warmup
		self.black_level = black_level
		self.frame_timeout = frame_timeout
		self.max_restarts = max_restarts

		self.stats = {"lit": 0, "unlit": 0, "returned": 0, "overexposed": 0,
					  "cutoffs": 0, "restarts": 0, "unpaired": 0}
		self._video = None
		self._meta = None
		self._pending_v = {}
		self._pending_m = {}
		self._last_unlit = None  # (sequence, image)
		self._opened_at = None
		self._released = False

		self._video = _Stream(device_path, V4L2_BUF_TYPE_VIDEO_CAPTURE)
		pix = self._video.get_pix_format()
		if pix.pixelformat != V4L2_PIX_FMT_GREY:
			raise RuntimeError("msxu_reader expects a GREY IR node, got fourcc %08x" % pix.pixelformat)
		self.width, self.height, self.stride = pix.width, pix.height, pix.bytesperline or pix.width

		if faceauth_mode is not None:
			# MSXU XU 14 selector 6: bNumEntries=1, interface 3, mode bitmap
			unit, iface, mode = faceauth_mode
			xu_set(self._video.fd, unit, 6, [1, iface, mode, 0, 0, 0, 0, 0, 0])

		# Region used for AE metering and for the warm-up saturation check
		self.roi = None
		if roi:
			l, t, w, h = roi
			l, t = max(0, min(l, self.width - 1)), max(0, min(t, self.height - 1))
			w, h = max(1, min(w, self.width - l)), max(1, min(h, self.height - t))
			self.roi = (l, t, w, h)

		if self.meta_path:
			self._meta = _Stream(self.meta_path, V4L2_BUF_TYPE_META_CAPTURE)
			if not self._meta.set_meta_format(V4L2_META_FMT_UVC_MSXU_1_5):
				self._log("metadata node %s does not support UVCM, falling back to brightness" % self.meta_path)
				self._meta.close()
				self._meta = None
		else:
			self._log("no metadata node found, falling back to brightness classification")

		if self._meta:
			self._meta.alloc()
		self._video.alloc()
		self._start()
		self._opened_at = time.monotonic()

	# -- cv2.VideoCapture compatible surface --------------------------------

	def get(self, prop):
		if prop == CAP_PROP_FRAME_WIDTH:
			return self.width
		if prop == CAP_PROP_FRAME_HEIGHT:
			return self.height
		return 0

	def set(self, prop, value):
		# Resolution and frame rate are fixed on this camera
		return False

	def grab(self):
		ok, _ = self.read()
		return ok

	def read(self):
		"""Return (True, BGR frame) for the next usable illuminated frame."""
		img = self.read_gray()
		if img is None:
			return False, None
		if cv2 is not None:
			return True, cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
		return True, np.repeat(img[:, :, None], 3, axis=2)

	def release(self):
		if self._released:
			return
		self._released = True
		self._log("stats %s" % self.stats)
		for s in (self._video, self._meta):
			if s:
				s.close()

	def __del__(self):
		try:
			self.release()
		except Exception:
			pass

	# -- internals -------------------------------------------------------------

	def _log(self, msg):
		if _debug_enabled():
			print("[msxu] %s" % msg, file=sys.stderr)

	def _start(self):
		for s in (self._meta, self._video):
			if s:
				s.queue_all()
		for s in (self._meta, self._video):
			if s:
				s.on()
		if self.roi:
			# Firmware ignores ROI writes made while the stream is off
			try:
				set_ae_roi(self._video.fd, self.roi)
				self._log("AE region of interest %s" % (self.roi,))
			except OSError as e:
				self._log("could not set AE region of interest: %s" % e)
		self._pending_v.clear()
		self._pending_m.clear()
		self._last_unlit = None
		self._last_frame_at = time.monotonic()

	def _restart(self):
		self.stats["restarts"] += 1
		for s in (self._video, self._meta):
			if s:
				s.off()
		self._start()

	def _next_pair(self):
		"""Return (sequence, image, lit) or None on timeout."""
		while True:
			# Emit the oldest complete pair first
			if self._pending_v:
				if self._meta is None:
					seq = min(self._pending_v)
					img = self._pending_v.pop(seq)
					return seq, img, None
				common = set(self._pending_v) & set(self._pending_m)
				if common:
					seq = min(common)
					img = self._pending_v.pop(seq)
					lit = self._pending_m.pop(seq)
					# Drop stale halves that will never pair
					for d in (self._pending_v, self._pending_m):
						for k in [k for k in d if k < seq]:
							del d[k]
							self.stats["unpaired"] += 1
					return seq, img, lit

			fds = [self._video.fd] + ([self._meta.fd] if self._meta else [])
			r, _, _ = select.select(fds, [], [], self.frame_timeout)
			if not r:
				return None
			if self._meta and self._meta.fd in r:
				while True:
					got = self._meta.dqbuf()
					if got is None:
						break
					self._pending_m[got[0]] = parse_illumination(got[1])
			if self._video.fd in r:
				while True:
					got = self._video.dqbuf()
					if got is None:
						break
					seq, data = got
					img = np.frombuffer(data, np.uint8)
					img = img[:self.stride * self.height].reshape(self.height, self.stride)[:, :self.width]
					self._pending_v[seq] = img
			# Bound memory if the metadata stream stalls
			for d in (self._pending_v, self._pending_m):
				while len(d) > 16:
					del d[min(d)]
					self.stats["unpaired"] += 1

	def read_gray(self):
		restarts_here = 0
		while True:
			pair = self._next_pair()
			if pair is None:
				# No frames at all: try restarting a few times, then give up
				if restarts_here >= 3:
					self._log("camera stopped delivering frames")
					return None
				restarts_here += 1
				self._restart()
				continue
			seq, img, lit = pair
			mean = float(img.mean())

			# Flat frame = firmware placeholder after emitter cut-off
			flat = int(img.max()) - int(img.min()) <= 1 and mean > self.black_level
			if lit is None:  # no metadata: classify by brightness
				lit = mean > self.black_level and not flat

			if flat or (lit and mean <= self.black_level):
				self.stats["cutoffs"] += 1
				self._log("seq %d cut-off detected (mean %.1f, flat=%s), restarting" % (seq, mean, flat))
				if self.stats["restarts"] >= self.max_restarts and restarts_here:
					return None
				restarts_here += 1
				self._restart()
				continue

			if not lit:
				self.stats["unlit"] += 1
				self._last_unlit = (seq, img)
				continue

			self.stats["lit"] += 1
			# Judge clipping where AE meters: a bright background outside the ROI
			# is expected to clip and must not hold frames back
			if self.roi:
				l, t, w, h = self.roi
				region = img[t:t + h, l:l + w]
			else:
				region = img
			sat = float(np.count_nonzero(region >= 250)) / region.size
			in_warmup = time.monotonic() - self._opened_at < self.warmup
			if in_warmup and sat > self.max_saturation:
				self.stats["overexposed"] += 1
				self._log("seq %d over-exposed (mean %.1f, sat %.1f%%), skipping" % (seq, mean, sat * 100))
				continue

			out = img
			if self.subtract_ambient and self._last_unlit and abs(self._last_unlit[0] - seq) == 1:
				if cv2 is not None:
					out = cv2.subtract(img, self._last_unlit[1])
				else:
					out = np.clip(img.astype(np.int16) - self._last_unlit[1], 0, 255).astype(np.uint8)

			self.stats["returned"] += 1
			self._log("seq %d lit mean %.1f sat %.2f%%" % (seq, mean, sat * 100))
			return np.ascontiguousarray(out)


def from_config(config):
	"""Build a reader from a howdy configparser object."""
	def opt(key, conv, default):
		try:
			raw = config.get("video", key, fallback=None)
			return default if raw in (None, "") else conv(raw)
		except ValueError:
			return default

	def boolean(v):
		return str(v).strip().lower() in ("1", "true", "yes", "on")

	def rect(v):
		v = str(v).strip().lower()
		if v in ("off", "none", "no", "false", "0"):
			return None
		parts = [int(x) for x in v.replace(" ", "").split(",")]
		if len(parts) != 4:
			raise ValueError(v)
		return tuple(parts)

	mode = opt("msxu_faceauth_mode", int, -1)
	return msxu_reader(
		config.get("video", "device_path"),
		meta_path=opt("msxu_meta_path", str, None),
		subtract_ambient=opt("msxu_subtract_ambient", boolean, False),
		max_saturation=opt("msxu_max_saturation", float, 0.10),
		warmup=opt("msxu_warmup", float, 1.5),
		black_level=opt("msxu_black_level", float, 4.0),
		faceauth_mode=(14, 3, mode) if mode > 0 else None,
		roi=opt("msxu_roi", rect, DEFAULT_ROI),
	)


def _main():
	import argparse
	ap = argparse.ArgumentParser(description="Test the MSXU IR reader")
	ap.add_argument("--device", default="/dev/video2")
	ap.add_argument("--meta", default=None)
	ap.add_argument("--frames", type=int, default=30, help="usable frames to collect")
	ap.add_argument("--timeout", type=float, default=10.0)
	ap.add_argument("--save", default=None, help="directory to write PGM frames")
	ap.add_argument("--subtract", action="store_true")
	ap.add_argument("--warmup", type=float, default=1.5)
	ap.add_argument("--max-sat", type=float, default=0.10)
	ap.add_argument("--roi", default="%d,%d,%d,%d" % DEFAULT_ROI,
					help="AE metering region l,t,w,h, or 'off'")
	args = ap.parse_args()

	os.environ.setdefault("HOWDY_MSXU_DEBUG", "1")
	t0 = time.monotonic()
	roi = None if args.roi.lower() == "off" else tuple(int(x) for x in args.roi.split(","))
	r = msxu_reader(args.device, meta_path=args.meta, subtract_ambient=args.subtract,
					warmup=args.warmup, max_saturation=args.max_sat, roi=roi)
	print("meta node: %s, %dx%d, open %.0f ms" % (r.meta_path, r.width, r.height, (time.monotonic() - t0) * 1000))
	if args.save:
		os.makedirs(args.save, exist_ok=True)
	n = 0
	first = None
	while n < args.frames and time.monotonic() - t0 < args.timeout:
		img = r.read_gray()
		if img is None:
			print("reader gave up")
			break
		n += 1
		first = first or time.monotonic() - t0
		if args.save:
			with open(os.path.join(args.save, "f%03d.pgm" % n), "wb") as f:
				f.write(b"P5 %d %d 255\n" % (img.shape[1], img.shape[0]))
				f.write(img.tobytes())
	dt = time.monotonic() - t0
	r.release()
	print("usable frames: %d in %.2f s (%.1f fps), first after %.0f ms" % (n, dt, n / dt, (first or 0) * 1000))
	print("stats: %s" % r.stats)


if __name__ == "__main__":
	_main()
