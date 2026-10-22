#!/usr/bin/env python3
"""
omarchy-touchbar: a living Touch Bar for Omarchy on Apple Silicon MacBooks.

Replaces tiny-dfr with a four-view strip rendered directly to the Touch Bar's
DRM display (2008x60) and driven by its touch digitizer:

  FLOW  - ASCII energy field; vigor follows CPU / agents / system load
  KEYS  - Esc + F1..F12 injected via uinput (physical Fn summons this view)
  CTRL  - drag sliders: display brightness, volume, keyboard backlight + media
  LIVE  - tappable agent + window cards (tap focuses via Hyprland IPC)

Themes are read live from ~/.local/state/omarchy/current/theme/colors.toml.

Goals protocol: any harness may drop JSON files into
  ~/.local/state/omarchy-touchbar/goals/<id>.json
  {"label": "build", "percent": 42, "status": "running"}
Percent is shown when reported; otherwise elapsed time + an indeterminate
pulse is shown. No fake percentages are ever invented.

Modes:
  (default)        run the daemon on real hardware
  --snapshot DIR   render every view to PNGs in DIR (fake state) and exit
"""
import ctypes
import fcntl
import glob
import json
import math
import os
import re
import select
import socket
import struct
import subprocess
import sys
import threading
import time

import cairo

# ---------------------------------------------------------------- constants

W, H = 2008, 60
TOUCH_MAX_X, TOUCH_MAX_Y = 23044.0, 639.0
STATE_DIR = os.path.expanduser("~/.local/state/omarchy-touchbar")
GOALS_DIR = os.path.join(STATE_DIR, "goals")
THEME_FILE = os.path.expanduser("~/.local/state/omarchy/current/theme/colors.toml")
FONT = "JetBrainsMono Nerd Font"

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
ABS_X, ABS_Y, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0, 1, 53, 54, 57
BTN_TOUCH = 330
KEY_ESC = 1
KEY_F = {i: ([59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88][i - 1]) for i in range(1, 13)}
KEY_VOLUMEUP, KEY_VOLUMEDOWN, KEY_MUTE = 115, 114, 113
KEY_PLAYPAUSE, KEY_PREVIOUSSONG, KEY_NEXTSONG = 164, 165, 163
KEY_FN = 464

VIEWS = ["flow", "keys", "ctrl", "live", "dev"]
# (id, card label) in DEV-tab order; SSH-only is the pure-shell option
HARNESSES = [("ssh", "ssh only"), ("pi", "pi"), ("claude", "claude"),
             ("codex", "codex"), ("opencode", "opencode"), ("cursor", "cursor"),
             ("antigravity", "antigravity"), ("mcode", "mcode"),
             ("kimi", "kimi"), ("grok", "grok"), ("hermes", "hermes"),
             ("grokbot", "grokbot")]
HARNESS_CMD = {
    "pi": "pi",
    "claude": "claude --dangerously-skip-permissions",
    "codex": "codex --dangerously-bypass-approvals-and-sandbox",
    "opencode": "opencode",
    "cursor": "cursor-agent --yolo",
    "antigravity": "agy --dangerously-skip-permissions",
    "mcode": "mcode",
    "kimi": "kimi --yolo",
    "grok": "grok --always-approve",
    "hermes": "hermes",
}
LOCAL_PI_CMD = "pi -p"  # fleet.json "local_pi_cmd" overrides per machine
# Optional per-machine fleet naming, kept out of the public source:
# ~/.config/omarchy/touchbar/fleet.json may define
# {"node_aliases": {"dns-label": "Display Name"},
#  "ssh_aliases": {"dns-label": "ssh-config-host"}}
def _fleet_config():
    try:
        with open(os.path.expanduser(
                "~/.config/omarchy/touchbar/fleet.json")) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


_FLEET_CFG = _fleet_config()
# Friendly node names shown on the bar (ssh still uses the real host)
NODE_ALIASES = _FLEET_CFG.get("node_aliases") or {}
# tailscale DNS label -> fleet ssh alias (from ~/.ssh/config)
SSH_ALIASES = _FLEET_CFG.get("ssh_aliases") or {}
# DNS labels never shown (e.g. a machine's retired pre-Linux tailnet
# ghost: same hardware, dead entry, not a real second machine)
HIDE_NODES = {str(x).lower() for x in (_FLEET_CFG.get("hide_nodes") or [])}
# This machine's pi launch line (provider/model are personal taste)
LOCAL_PI_CMD = _FLEET_CFG.get("local_pi_cmd") or LOCAL_PI_CMD
# how to resume each harness's last session (used inside tmux on tap)
RESUME_CMD = {
    "pi": "pi --continue",
    "claude": "claude --continue",
    "codex": "codex resume --last",
    "opencode": "opencode --continue",
    "grok": "grok --always-approve --continue",
}


def node_alias(name, host=""):
    hay = f"{name} {host}".lower()
    for key, friendly in NODE_ALIASES.items():
        if key in hay:
            return friendly
    return name
RAIL_W = 292  # left tab rail width
# The DSI panel scans out portrait (60x2008); we render a logical 2008x60
# scene and blit it rotated. Flip with TOUCHBAR_ROTATE=ccw if upside down.
ROTATE_CW = os.environ.get("TOUCHBAR_ROTATE", "cw") != "ccw"
FLIP_TOUCH_X = os.environ.get("TOUCHBAR_FLIP_X", "0") == "1"


def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def lerp(a, b, t):
    return a + (b - a) * t


def fmt_elapsed(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m"


# ---------------------------------------------------------------- theme

class Theme:
    DEFAULTS = {
        "background": "#1a1b26", "dark_background": "#13141c",
        "darker_background": "#0e0e14", "lighter_background": "#24283b",
        "foreground": "#a9b1d6", "dark_foreground": "#565f89",
        "bright_foreground": "#c0caf5", "accent": "#7aa2f7",
        "muted": "#414868", "selection": "#292e42",
        "cyan": "#449dab", "bright_cyan": "#0db9d7", "green": "#9ece6a",
        "bright_green": "#b9f27c", "magenta": "#ad8ee6",
        "bright_magenta": "#bb9af7", "yellow": "#e0af68", "red": "#f7768e",
    }

    def __init__(self):
        self.mtime = 0
        self.c = dict(self.DEFAULTS)
        self.rgb_cache = {k: self.rgb(v) for k, v in self.c.items()}
        self.font = FONT  # follows `omarchy font` (fontconfig monospace)
        self.refresh_font()
        self.reload(force=True)

    def refresh_font(self):
        try:
            out = subprocess.run(["fc-match", "monospace", "-f", "%{family}"],
                                 capture_output=True, text=True, timeout=1.5).stdout
            fam = out.split(",")[0].strip()
            if fam and fam != self.font:
                self.font = fam
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def rgb(hexs):
        hexs = hexs.lstrip("#")
        return tuple(int(hexs[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

    def reload(self, force=False):
        try:
            m = os.path.getmtime(THEME_FILE)
        except OSError:
            return False
        if not force and m == self.mtime:
            return False
        self.mtime = m
        try:
            for line in open(THEME_FILE):
                mm = re.match(r'\s*(\w+)\s*=\s*"(#[0-9a-fA-F]{6})"', line)
                if mm and mm.group(1) in self.c:
                    self.c[mm.group(1)] = mm.group(2)
        except OSError:
            pass
        self.rgb_cache = {k: self.rgb(v) for k, v in self.c.items()}
        return True

    def __getitem__(self, key):
        return self.rgb_cache[key]


# ---------------------------------------------------------------- DRM

libdrm = ctypes.CDLL("libdrm.so.2")


class ModeInfo(ctypes.Structure):
    _fields_ = [("clock", ctypes.c_uint32), ("hdisplay", ctypes.c_uint16),
                ("hsync_start", ctypes.c_uint16), ("hsync_end", ctypes.c_uint16),
                ("htotal", ctypes.c_uint16), ("hskew", ctypes.c_uint16),
                ("vdisplay", ctypes.c_uint16), ("vsync_start", ctypes.c_uint16),
                ("vsync_end", ctypes.c_uint16), ("vtotal", ctypes.c_uint16),
                ("vscan", ctypes.c_uint16), ("vrefresh", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("name", ctypes.c_char * 32)]


class _Res(ctypes.Structure):  # drmModeRes: count precedes each pointer
    _fields_ = [("count_fbs", ctypes.c_int), ("fbs", ctypes.POINTER(ctypes.c_uint32)),
                ("count_crtcs", ctypes.c_int), ("crtcs", ctypes.POINTER(ctypes.c_uint32)),
                ("count_connectors", ctypes.c_int), ("connectors", ctypes.POINTER(ctypes.c_uint32)),
                ("count_encoders", ctypes.c_int), ("encoders", ctypes.POINTER(ctypes.c_uint32)),
                ("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
                ("min_height", ctypes.c_uint32), ("max_height", ctypes.c_uint32)]


class _Conn(ctypes.Structure):
    _fields_ = [("connector_id", ctypes.c_uint32), ("encoder_id", ctypes.c_uint32),
                ("connector_type", ctypes.c_uint32), ("connector_type_id", ctypes.c_uint32),
                ("connection", ctypes.c_uint32), ("mm_width", ctypes.c_uint32),
                ("mm_height", ctypes.c_uint32), ("subpixel", ctypes.c_uint32),
                ("count_modes", ctypes.c_int), ("modes", ctypes.POINTER(ModeInfo)),
                ("count_props", ctypes.c_int), ("props", ctypes.POINTER(ctypes.c_uint32)),
                ("prop_values", ctypes.POINTER(ctypes.c_uint64)),
                ("count_encoders", ctypes.c_int), ("encoders", ctypes.POINTER(ctypes.c_uint32))]


class _Enc(ctypes.Structure):
    _fields_ = [("encoder_id", ctypes.c_uint32), ("encoder_type", ctypes.c_uint32),
                ("crtc_id", ctypes.c_uint32), ("possible_crtcs", ctypes.c_uint32),
                ("possible_clones", ctypes.c_uint32)]


libdrm.drmModeGetResources.restype = ctypes.POINTER(_Res)
libdrm.drmModeGetConnector.restype = ctypes.POINTER(_Conn)
libdrm.drmModeGetEncoder.restype = ctypes.POINTER(_Enc)

DRM_IOCTL_MODE_CREATE_DUMB = (3 << 30) | (0x64 << 8) | 0xB2 | (32 << 16)
DRM_IOCTL_MODE_MAP_DUMB = (3 << 30) | (0x64 << 8) | 0xB3 | (16 << 16)
DRM_IOCTL_MODE_DESTROY_DUMB = (3 << 30) | (0x64 << 8) | 0xB4 | (4 << 16)


class DumbBuffer:
    def __init__(self, fd, w, h):
        create = bytearray(32)
        struct.pack_into("IIII", create, 0, h, w, 32, 0)
        fcntl.ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, create, True)
        # struct drm_mode_create_dumb: height,width,bpp,flags,handle,pitch,size
        self.handle = struct.unpack_from("I", create, 16)[0]
        self.pitch = struct.unpack_from("I", create, 20)[0]
        self.size = struct.unpack_from("Q", create, 24)[0]
        m = bytearray(16)
        struct.pack_into("I", m, 0, self.handle)
        fcntl.ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, m, True)
        offset = struct.unpack_from("Q", m, 8)[0]
        import mmap
        self.mmap = mmap.mmap(fd, self.size, offset=offset)
        fb = ctypes.c_uint32(0)
        if libdrm.drmModeAddFB(fd, w, h, 24, 32, self.pitch, self.handle, ctypes.byref(fb)) != 0:
            raise RuntimeError("drmModeAddFB failed")
        self.fb_id = fb.value
        self.surface = cairo.ImageSurface.create_for_data(
            self.mmap, cairo.FORMAT_RGB24, w, h, self.pitch)


class _ClipRect(ctypes.Structure):
    _fields_ = [("x1", ctypes.c_uint16), ("y1", ctypes.c_uint16),
                ("x2", ctypes.c_uint16), ("y2", ctypes.c_uint16)]


libdrm.drmModeDirtyFB.argtypes = [ctypes.c_int, ctypes.c_uint32,
                                  ctypes.POINTER(_ClipRect), ctypes.c_int]


class _PlaneRes(ctypes.Structure):
    _fields_ = [("count_planes", ctypes.c_uint32),
                ("planes", ctypes.POINTER(ctypes.c_uint32))]


class _Plane(ctypes.Structure):  # drmModePlane: formats come first
    _fields_ = [("count_format_types", ctypes.c_uint32),
                ("format_types", ctypes.POINTER(ctypes.c_uint32)),
                ("plane_id", ctypes.c_uint32), ("crtc_id", ctypes.c_uint32),
                ("fb_id", ctypes.c_uint32), ("crtc_x", ctypes.c_uint32),
                ("crtc_y", ctypes.c_uint32), ("x", ctypes.c_uint32),
                ("y", ctypes.c_uint32), ("possible_crtcs", ctypes.c_uint32),
                ("gamma_size", ctypes.c_uint32),
                ("gamma_values", ctypes.POINTER(ctypes.c_uint32))]


libdrm.drmModeAtomicAlloc.restype = ctypes.c_void_p
libdrm.drmModeAtomicAddProperty.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                            ctypes.c_uint32, ctypes.c_uint64]
libdrm.drmModeAtomicAddProperty.restype = ctypes.c_int
libdrm.drmModeAtomicCommit.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                        ctypes.c_uint32, ctypes.c_void_p]
libdrm.drmModeAtomicCommit.restype = ctypes.c_int
libdrm.drmModeAtomicFree.argtypes = [ctypes.c_void_p]
libdrm.drmModeCreatePropertyBlob.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                             ctypes.c_size_t,
                                             ctypes.POINTER(ctypes.c_uint32)]
libdrm.drmModeCreatePropertyBlob.restype = ctypes.c_int


class _ObjProps(ctypes.Structure):
    _fields_ = [("count_props", ctypes.c_uint32),
                ("props", ctypes.POINTER(ctypes.c_uint32)),
                ("prop_values", ctypes.POINTER(ctypes.c_uint64))]


class _PropRes(ctypes.Structure):
    _fields_ = [("prop_id", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("name", ctypes.c_char * 32), ("count_values", ctypes.c_int),
                ("values", ctypes.POINTER(ctypes.c_uint64)), ("count_enums", ctypes.c_int),
                ("enums", ctypes.c_void_p), ("count_blobs", ctypes.c_int),
                ("blob_ids", ctypes.POINTER(ctypes.c_uint32))]


libdrm.drmModeGetPlaneResources.restype = ctypes.POINTER(_PlaneRes)
libdrm.drmModeGetPlane.restype = ctypes.POINTER(_Plane)
libdrm.drmModeObjectGetProperties.restype = ctypes.POINTER(_ObjProps)
libdrm.drmModeGetProperty.restype = ctypes.POINTER(_PropRes)
OBJ_CRTC, OBJ_CONNECTOR, OBJ_PLANE = 0xCCCCCCCC, 0xC0C0C0C0, 0xEEEEEEEE


class DRMDisplay:
    """Owns /dev/dri/card2 (Touch Bar DSI-1).

    Mirrors tiny-dfr's proven pipeline for this command-mode DSI panel:
    one 60x2008 XRGB8888 dumb buffer, a single modeset, front-buffer
    rendering (rotated in-context), and DRM_IOCTL_MODE_DIRTYFB after
    every frame to make the display pipe upload it. No page flips:
    the adp driver is built on drm_gem_fb_create_with_dirty.
    """

    def __init__(self, path="/dev/dri/card2"):
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        res = libdrm.drmModeGetResources(self.fd)
        if not res:
            raise RuntimeError("drmModeGetResources failed")
        res_crtcs = [res.contents.crtcs[i] for i in range(res.contents.count_crtcs)]
        self.crtc_id = None
        self.conn_id = None
        self.mode = None
        for i in range(res.contents.count_connectors):
            cid = res.contents.connectors[i]
            conn = libdrm.drmModeGetConnector(self.fd, cid)
            if not conn:
                continue
            if conn.contents.connection == 1 and conn.contents.count_modes > 0:
                mode = conn.contents.modes[0]
                if mode.vdisplay >= 1000 and mode.hdisplay <= 100:  # portrait strip
                    self.conn_id = cid
                    self.mode = ModeInfo.from_buffer_copy(
                        ctypes.string_at(ctypes.addressof(mode), ctypes.sizeof(ModeInfo)))
                    enc_id = conn.contents.encoder_id
                    if enc_id:
                        enc = libdrm.drmModeGetEncoder(self.fd, enc_id)
                        self.crtc_id = enc.contents.crtc_id
                        libdrm.drmModeFreeEncoder(enc)
                    if not self.crtc_id:
                        for j in range(conn.contents.count_encoders):
                            enc = libdrm.drmModeGetEncoder(self.fd, conn.contents.encoders[j])
                            if enc and enc.contents.crtc_id:
                                self.crtc_id = enc.contents.crtc_id
                                libdrm.drmModeFreeEncoder(enc)
                                break
                            if enc:
                                libdrm.drmModeFreeEncoder(enc)
                    libdrm.drmModeFreeConnector(conn)
                    break
            libdrm.drmModeFreeConnector(conn)
        libdrm.drmModeFreeResources(res)
        if self.crtc_id is None:
            raise RuntimeError("no connected Touch Bar connector/CRTC found")
        self.fb_w, self.fb_h = self.mode.hdisplay, self.mode.vdisplay
        try:
            libdrm.drmSetMaster(self.fd)
        except Exception:
            pass
        # tiny-dfr geometry: the dumb buffer is 64 wide; the plane's SRC
        # rect crops the visible 60x2008 out of it. A 60-wide buffer
        # (pitch 240) shears on this display pipe's upload path.
        self.buf_w = 64
        self.buffer = DumbBuffer(self.fd, self.buf_w, self.fb_h)
        self.offscreen = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.buf_w, self.fb_h)
        self._clip = _ClipRect(0, 0, self.fb_w, self.fb_h)
        if not self._atomic_modeset(res_crtcs):
            # safety net: legacy modeset (known to shear; should not happen)
            conn_arr = (ctypes.c_uint32 * 1)(self.conn_id)
            if libdrm.drmModeSetCrtc(self.fd, self.crtc_id, self.buffer.fb_id,
                                     0, 0, conn_arr, 1, ctypes.byref(self.mode)) != 0:
                raise RuntimeError("modeset failed (atomic and legacy)")

    # -- atomic modeset, mirroring tiny-dfr's display.rs -------------------
    def _prop_id(self, obj_id, obj_type, name):
        props = libdrm.drmModeObjectGetProperties(self.fd, obj_id, obj_type)
        if not props:
            raise RuntimeError(f"no props for object {obj_id}")
        found = None
        for i in range(props.contents.count_props):
            pid = props.contents.props[i]
            info = libdrm.drmModeGetProperty(self.fd, pid)
            if info and info.contents.name.decode(errors="replace") == name:
                found = pid
                libdrm.drmModeFreeProperty(info)
                break
            if info:
                libdrm.drmModeFreeProperty(info)
        libdrm.drmModeFreeObjectProperties(props)
        if found is None:
            raise RuntimeError(f"property {name} not found")
        return found

    def _atomic_modeset(self, res_crtcs):
        try:
            libdrm.drmSetClientCap(self.fd, 2, 1)  # UNIVERSAL_PLANES
            libdrm.drmSetClientCap(self.fd, 3, 1)  # ATOMIC
            crtc_index = list(res_crtcs).index(self.crtc_id)
            planes = libdrm.drmModeGetPlaneResources(self.fd)
            plane_id = None
            for i in range(planes.contents.count_planes):
                pid = planes.contents.planes[i]
                pl = libdrm.drmModeGetPlane(self.fd, pid)
                if pl and (pl.contents.possible_crtcs >> crtc_index) & 1:
                    plane_id = pid
                    libdrm.drmModeFreePlane(pl)
                    break
                if pl:
                    libdrm.drmModeFreePlane(pl)
            libdrm.drmModeFreePlaneResources(planes)
            if plane_id is None:
                return False
            blob_id = ctypes.c_uint32(0)
            mode_buf = ctypes.create_string_buffer(
                ctypes.string_at(ctypes.byref(self.mode), ctypes.sizeof(ModeInfo)),
                ctypes.sizeof(ModeInfo))
            if libdrm.drmModeCreatePropertyBlob(self.fd, mode_buf, len(mode_buf),
                                                ctypes.byref(blob_id)) != 0:
                return False
            req = libdrm.drmModeAtomicAlloc()
            add = libdrm.drmModeAtomicAddProperty
            add(req, self.conn_id, self._prop_id(self.conn_id, OBJ_CONNECTOR, "CRTC_ID"),
                self.crtc_id)
            add(req, self.crtc_id, self._prop_id(self.crtc_id, OBJ_CRTC, "MODE_ID"),
                blob_id.value)
            add(req, self.crtc_id, self._prop_id(self.crtc_id, OBJ_CRTC, "ACTIVE"), 1)
            add(req, plane_id, self._prop_id(plane_id, OBJ_PLANE, "FB_ID"), self.buffer.fb_id)
            add(req, plane_id, self._prop_id(plane_id, OBJ_PLANE, "CRTC_ID"), self.crtc_id)
            add(req, plane_id, self._prop_id(plane_id, OBJ_PLANE, "SRC_X"), 0)
            add(req, plane_id, self._prop_id(plane_id, OBJ_PLANE, "SRC_Y"), 0)
            add(req, plane_id, self._prop_id(plane_id, OBJ_PLANE, "SRC_W"), self.fb_w << 16)
            add(req, plane_id, self._prop_id(plane_id, OBJ_PLANE, "SRC_H"), self.fb_h << 16)
            add(req, plane_id, self._prop_id(plane_id, OBJ_PLANE, "CRTC_X"), 0)
            add(req, plane_id, self._prop_id(plane_id, OBJ_PLANE, "CRTC_Y"), 0)
            add(req, plane_id, self._prop_id(plane_id, OBJ_PLANE, "CRTC_W"), self.fb_w)
            add(req, plane_id, self._prop_id(plane_id, OBJ_PLANE, "CRTC_H"), self.fb_h)
            rc = libdrm.drmModeAtomicCommit(self.fd, req, 0x0400, None)  # ALLOW_MODESET
            libdrm.drmModeAtomicFree(req)
            return rc == 0
        except Exception as exc:
            print(f"atomic modeset failed: {exc}", file=sys.stderr)
            return False

    def commit(self):
        """Copy the offscreen surface into the dumb buffer and push it to
        the panel via MODE_DIRTYFB (the adp upload trigger)."""
        self.offscreen.flush()
        self.upload_bytes(bytes(self.offscreen.get_data()),
                          self.offscreen.get_stride())

    def upload_bytes(self, data, stride):
        # data may be immutable bytes: memmove accepts it directly as a
        # char* source (c_char.from_buffer would demand a writable buffer)
        pitch = self.buffer.pitch
        dst_base = ctypes.addressof(ctypes.c_char.from_buffer(self.buffer.mmap))
        if pitch == stride:
            ctypes.memmove(dst_base, data, min(len(data), self.buffer.size))
        else:
            row = min(pitch, stride)
            mv = memoryview(data)
            for y in range(self.fb_h):
                ctypes.memmove(dst_base + y * pitch, mv[y * stride:y * stride + row], row)
        libdrm.drmModeDirtyFB(self.fd, self.buffer.fb_id,
                              ctypes.byref(self._clip), 1)

    @property
    def draw_surface(self):
        return self.offscreen


# ---------------------------------------------------------------- uinput

def _ui(code):
    return (1 << 30) | (ord('U') << 8) | code | (4 << 16)


class UInput:
    KEYS = [KEY_ESC] + list(KEY_F.values()) + [
        KEY_VOLUMEUP, KEY_VOLUMEDOWN, KEY_MUTE,
        KEY_PLAYPAUSE, KEY_PREVIOUSSONG, KEY_NEXTSONG]

    def __init__(self):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, _ui(100), 1)       # UI_SET_EVBIT EV_KEY
        for k in self.KEYS:
            fcntl.ioctl(self.fd, _ui(101), k)   # UI_SET_KEYBIT
        # legacy uinput_user_dev: name[80], input_id(4xu16), ff_effects_max,
        # absmax[64], absmin[64], absfuzz[64], absflat[64]  (all int32)
        dev = bytearray(80 + 8 + 4 + 64 * 4 * 4)
        name = b"Omarchy Touchbar"
        dev[0:len(name)] = name
        struct.pack_into("HHHH", dev, 80, 0x06, 0x0ACE, 0x0001, 1)
        os.write(self.fd, bytes(dev))
        fcntl.ioctl(self.fd, (0 << 30) | (ord('U') << 8) | 1)  # UI_DEV_CREATE

    def tap(self, code):
        ev = struct.pack("qqHHi", 0, 0, EV_KEY, code, 1)
        syn = struct.pack("qqHHi", 0, 0, EV_SYN, 0, 0)
        os.write(self.fd, ev + syn)
        ev = struct.pack("qqHHi", 0, 0, EV_KEY, code, 0)
        os.write(self.fd, ev + syn)


# ---------------------------------------------------------------- state

AGENT_BINS = {"pi": "π", "claude": "✳", "codex": "⬢", "opencode": "⌬",
              "hermes": "☿", "grok": "✦", "antigravity": "◈", "cursor": "⌁",
              "kimi": "◎", "mcode": "⬣"}
# argv tokens that mean a different kind id (DEV/REMOTE naming)
BIN_ALIASES = {"agy": "antigravity", "cursor-agent": "cursor"}
INTERPRETERS = {"python", "python3", "node", "bun", "deno"}


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.cpu = 0.0
        self.mem = 0.0
        self.load1 = 0.0
        self.battery = -1
        self.charging = False
        self.brightness = 0.5   # main display 0..1
        self.volume = 0.7
        self.muted = False
        self.kbd = 0.0
        self.tb = 0.6           # touch bar backlight 0..1 (user-set level)
        self.clients = []       # hyprland clients
        self.active_addr = ""
        self.workspaces = []
        self.agents = []        # {kind,glyph,label,elapsed,active,percent}
        self.dpms_on = True
        self.voice_live = False  # voice agent service state (voice.json)
        self.events = []        # completion/start events for FLOW flashes
        self._prev_agent_map = {}
        self._pending_starts = {}
        self._prev_remote_map = {}
        self._pending_remote = {}
        self._remote_scan_running = False
        self._ssh_fail_until = {}  # alias -> monotonic deadline (backoff)
        self._absent_local = {}
        self._absent_remote = {}
        self._prev_goal_map = {}
        self._events_baseline = False
        self.nodes = []         # fleet: online node names (tailscale)
        self.nodes_detail = []  # fleet/dev: [{name, host, os, ip, online}]
        self._remote_agents = []  # sessions running on other fleet nodes
        self.sessions_detail = {}  # fleet: harness -> [(label, HH:MM)] today
        self.disk_pct = 0
        self.net_ip = ""
        self.mem_used_gb = 0.0
        self.mem_total_gb = 0.0
        self.sessions_today = {}  # fleet: harness -> session files touched today
        self.uptime_sec = 0
        self._prev_cpu = None
        self.energy = 0.10      # smoothed flow energy 0..1

    # -- hyprland ------------------------------------------------------
    def _hypr(self, req):
        return hypr_request(req)

    # -- agents ---------------------------------------------------------
    @staticmethod
    def _classify(args):
        """Harness kind for a cmdline, or None. Smarter than argv[0]:
        interpreter-hosted harnesses (python hermes, node grok) carry
        the harness as their script argument, and wrapper shims use
        alias names (agy, cursor-agent)."""
        if not args:
            return None
        base0 = os.path.basename(args[0])
        if base0 in BIN_ALIASES:
            return BIN_ALIASES[base0]
        if base0 in AGENT_BINS or args[0].endswith(tuple("/" + k for k in AGENT_BINS)):
            return base0 if base0 in AGENT_BINS else next(
                k for k in AGENT_BINS if args[0].endswith("/" + k))
        if "/pi/pi" in args[0]:
            return "pi"
        if base0 in INTERPRETERS or base0.startswith("python"):
            for a in args[1:3]:
                if "/hermes-agent/hermes" in a:
                    # `hermes gateway run` is a background service,
                    # not a CLI session (it has no terminal to focus)
                    return None if "gateway" in args else "hermes"
                b = os.path.basename(a)
                if b in BIN_ALIASES:
                    return BIN_ALIASES[b]
                if b in AGENT_BINS:
                    return b
        if any("/hermes-agent/hermes" in a for a in args[1:3]):
            return None if "gateway" in args else "hermes"
        return None

    def _scan_agents(self):
        # one /proc pass, then tree reasoning: a session is the
        # OUTERMOST harness process in its tree (node->grok shims and
        # interpreter children are the same session, not new ones)
        btime = 0
        try:
            for line in open("/proc/stat"):
                if line.startswith("btime"):
                    btime = int(line.split()[1])
                    break
        except OSError:
            pass
        hz = os.sysconf("SC_CLK_TCK")
        procs = {}  # pid -> (args, ppid, elapsed, cwd)
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                raw = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
                args = [a.decode(errors="replace") for a in raw if a]
                if not args:
                    continue
                fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
                ppid = int(fields[1])
                elapsed = time.time() - (btime + int(fields[19]) / hz)
                cwd = os.readlink(f"/proc/{pid}/cwd")
                procs[int(pid)] = (args, ppid, elapsed, cwd)
            except (OSError, ValueError, IndexError):
                continue
        classified = {pid: self._classify(v[0]) for pid, v in procs.items()}
        classified = {p: k for p, k in classified.items() if k}

        def has_agent_ancestor(pid):
            cur = procs[pid][1]
            for _ in range(12):
                if cur in classified:
                    return True
                if cur <= 1 or cur not in procs:
                    return False
                cur = procs[cur][1]
            return False

        agents = []
        for pid, kind in classified.items():
            if has_agent_ancestor(pid):
                continue
            _, _, elapsed, cwd = procs[pid]
            active, extra = self._agent_activity(kind, str(pid), cwd)
            agents.append({"kind": kind, "glyph": AGENT_BINS[kind],
                           "label": os.path.basename(cwd) or cwd,
                           "elapsed": elapsed, "active": active,
                           "percent": None, "pid": pid, **extra})
        if hasattr(self, "_jiffies"):
            live = {str(p) for p in procs}
            self._jiffies = {k: v for k, v in self._jiffies.items() if k in live}
        agents.sort(key=lambda a: a["elapsed"], reverse=True)
        return agents[:8]

    def _agent_activity(self, kind, pid, cwd):
        active = False
        extra = {}
        try:  # cpu jiffies delta handled coarsely: check utime growth is
            pass  # expensive; use session-file mtime for pi instead
        except Exception:
            pass
        if kind == "pi":
            enc = "--" + cwd.strip("/").replace("/", "-") + "--"  # ~/.pi/agent/sessions/--cwd--
            d = os.path.expanduser(f"~/.pi/agent/sessions/{enc}")
            files = glob.glob(os.path.join(d, "*.jsonl"))
            if files:
                latest = max(files, key=os.path.getmtime)
                age = time.time() - os.path.getmtime(latest)
                active = age < 5
                extra["detail"] = "pi · " + os.path.basename(cwd)
                try:  # model: model_change entries sit at the file head;
                    # a mid-session switch appends another - check both ends
                    size = os.path.getsize(latest)
                    with open(latest, "rb") as fh:
                        head = fh.read(131072).decode(errors="replace")
                        fh.seek(max(0, size - 131072))
                        tail = fh.read().decode(errors="replace")
                    mh = re.findall(r'"modelId"\s*:\s*"([^"]+)"', head)
                    mt = re.findall(r'"modelId"\s*:\s*"([^"]+)"', tail)
                    chosen = (mt or mh)
                    if chosen:
                        extra["model"] = chosen[-1]
                except OSError:
                    pass
        else:
            # generic activity = CPU actually consumed since the last
            # scan (utime+stime delta), not an instantaneous R/D peek
            extra["detail"] = f"{kind} \u00b7 {os.path.basename(cwd)}"
            try:
                fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
                jiff = int(fields[11]) + int(fields[12])
                prev = getattr(self, "_jiffies", {}).get(str(pid))
                if not hasattr(self, "_jiffies"):
                    self._jiffies = {}
                self._jiffies[str(pid)] = jiff
                active = prev is not None and jiff > prev
            except (OSError, ValueError, IndexError):
                pass
            if kind == "hermes":
                # cross-check Hermes' own session registry: confirms
                # this pid is a real surface session, names it
                try:
                    reg = json.load(open(os.path.expanduser(
                        "~/.hermes/runtime/active_sessions.json")))
                    for e in reg.get("entries", []):
                        if e.get("pid") == int(pid):
                            extra["detail"] = "hermes \u00b7 " + str(
                                e.get("surface", "cli"))
                            break
                except Exception:
                    pass
        return active, extra

    REMOTE_PROCS = {"pi": "pi", "claude": "claude", "codex": "codex",
                    "opencode": "opencode", "cursor-agent": "cursor",
                    "agy": "antigravity", "mcode": "mcode", "kimi": "kimi",
                    "grok": "grok", "hermes": "hermes"}

    # One multiplexed connection per node (ControlPersist) reused by
    # every scan: the old code opened a fresh sshd login per node every
    # 20 s (~18 logins/min fleet-wide), which trips sshd rate limits
    # and made remote sessions look flaky. Scans are also single-flight
    # and parallel, with per-node failure backoff.
    SSH_SCAN_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                     "-o", "ControlMaster=auto",
                     "-o", "ControlPath=" + os.path.expanduser("~/.ssh/cm-%C"),
                     "-o", "ControlPersist=180",
                     "-o", "ServerAliveInterval=15"]

    def _scan_one_node(self, e):
        try:
            proc = subprocess.run(
                ["ssh"] + self.SSH_SCAN_OPTS + [e["ssh"],
                 "ps -eo pid=,ppid=,etimes=,args=; echo __TBSESS__; "
                 "tmux list-sessions -F '#{session_name} #{session_attached}' 2>/dev/null; "
                 "echo __TBPANES__; "
                 "tmux list-panes -a -F '#{pane_pid} #{session_name}' 2>/dev/null"],
                capture_output=True, text=True, timeout=6)
            if proc.returncode != 0:
                raise OSError("ssh scan failed")
            return e, proc.stdout, False
        except Exception:
            return e, "", True  # unreachable != "sessions ended"

    @staticmethod
    def _parse_remote(e, out):
        """(agents, {tmux: attached}) from one node's scan output.
        A remote card must be a session you can land in: the harness
        has to live inside a tmux pane. Harness-named daemons running
        loose on a node (agent services, forgotten desktop terminals)
        are not sessions — they can neither be focused nor closed."""
        ps_part, _, rest = out.partition("__TBSESS__")
        sess_part, _, pane_part = rest.partition("__TBPANES__")
        attached = {}
        for line in sess_part.splitlines():
            p = line.rsplit(None, 1)
            if len(p) == 2:
                try:
                    attached[p[0]] = int(p[1])
                except ValueError:
                    pass
        pane_sess = {}
        for line in pane_part.splitlines():
            p = line.split(None, 1)
            if len(p) == 2 and p[0].isdigit():
                pane_sess[int(p[0])] = p[1]
        procs = {}
        for line in ps_part.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 4 or not parts[0].isdigit():
                continue
            try:
                procs[int(parts[0])] = (int(parts[1]), float(parts[2]), parts[3])
            except ValueError:
                continue

        def tmux_of(pid):
            cur = pid
            for _ in range(12):
                if cur in pane_sess:
                    return pane_sess[cur]
                if cur not in procs or procs[cur][0] <= 1:
                    return None
                cur = procs[cur][0]
            return None

        best = {}
        for pid, (ppid, elapsed, args) in procs.items():
            hid = None
            for tok in args.split()[:3]:
                hid = State.REMOTE_PROCS.get(os.path.basename(tok))
                if hid:
                    break
            if not hid and "/hermes-agent/hermes" in args:
                hid = "hermes"
            if not hid:
                continue
            sess = tmux_of(pid)
            if not sess:
                continue  # loose daemon/process: not a landable session
            key = (e["ssh"], sess)
            if key not in best or elapsed > best[key]["elapsed"]:
                best[key] = {
                    "kind": hid, "glyph": "◆", "label": e["name"],
                    "detail": f"{hid} \u00b7 {e['name']}",
                    "elapsed": elapsed, "active": False, "percent": None,
                    "pid": 0, "remote": True, "node": e["ssh"],
                    "node_name": e["name"], "window": None, "tmux": sess,
                }
        return list(best.values()), attached

    def _scan_remote_agents(self):
        """Running harness sessions on the other fleet nodes, via ssh.
        Runs in a side thread; results land in self._remote_agents."""
        found = {}
        with self.lock:
            targets = [e for e in self.nodes_detail if e.get("online") and e.get("ssh")]
        now = time.monotonic()
        # backed-off nodes count as poll failures: their last-known
        # sessions are kept below, never read as "ended"
        failed = {e["ssh"] for e in targets
                  if self._ssh_fail_until.get(e["ssh"], 0) > now}
        targets = [e for e in targets if e["ssh"] not in failed]
        results = []
        if targets:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(6, len(targets))) as ex:
                results = list(ex.map(self._scan_one_node, targets))
        for e, out, is_failed in results:
            if is_failed:
                failed.add(e["ssh"])
                # back off a dead node instead of retrying every cycle
                self._ssh_fail_until[e["ssh"]] = time.monotonic() + 120
                continue
            self._ssh_fail_until.pop(e["ssh"], None)
            agents, attached = self._parse_remote(e, out)
            for a in agents:
                a["attached"] = bool(attached.get(a.get("tmux"), 0))
                key = (a["node"], a.get("tmux") or a["kind"])
                if key not in found or a["elapsed"] > found[key]["elapsed"]:
                    found[key] = a
        if failed:  # keep last-known sessions for nodes we couldn't poll
            with self.lock:
                for a in self._remote_agents:
                    if a.get("node") in failed:
                        found[(a["node"], a.get("tmux") or a["kind"])] = a
        with self.lock:
            self._remote_agents = list(found.values())
        self._diff_remote_events()

    @staticmethod
    def _window_for_pid(pid, by_pid):
        cur = pid
        for _ in range(12):
            if cur in by_pid:
                return by_pid[cur]
            try:
                stat = open(f"/proc/{cur}/stat").read()
                cur = int(stat.rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                return None
            if cur <= 1:
                return None
        return None

    def _scan_goals(self):
        goals = []
        for f in glob.glob(os.path.join(GOALS_DIR, "*.json")):
            try:
                g = json.load(open(f))
                started = g.get("started", os.path.getmtime(f))
                goals.append({
                    "id": os.path.basename(f)[:-5],
                    "node": g.get("node"), "harness": g.get("harness"),
                    "status": g.get("status", "running"),
                    "kind": "goal", "glyph": "◎",
                    "label": g.get("label", os.path.basename(f)[:-5]),
                    "elapsed": time.time() - started,
                    "active": g.get("status", "running") == "running",
                    "percent": g.get("percent"),
                    "pid": g.get("pid", 0), "detail": g.get("detail", g.get("label", "goal")),
                })
            except (OSError, ValueError):
                continue
        return goals

    def _diff_events(self, agents, goals):
        """Turn agent/goal lifecycle changes into FLOW flash events.
        Events carry their focus target (window / pid / node+harness) so
        the notification pill can jump straight to the session.

        Signal quality rules (learned the hard way: `pi --help` probes
        flashed 'done' every minute): a session must live >= 90 s for its
        completion to be notification-worthy, and a start must survive
        two consecutive scans to count."""
        cur_map = {(a["kind"], a["pid"]): (a.get("detail", a["kind"]),
                                            a.get("window"), a.get("elapsed", 0))
                   for a in agents}
        cur_goals = {g["id"]: (g.get("percent"), g.get("status"), g["label"],
                                (g.get("node"), g.get("harness"), g.get("pid")))
                     for g in goals}
        if self._events_baseline:
            ev = []
            # completion requires TWO consecutive absences (a single
            # missed scan must never read as "session finished")
            for k, (detail, window, elapsed) in self._prev_agent_map.items():
                if k not in cur_map:
                    cnt = self._absent_local.get(k, 0) + 1
                    self._absent_local[k] = cnt
                    if cnt >= 2 and elapsed >= 90:
                        ev.append(("agent_done", detail, window, k[1], None, None))
            for k in cur_map:
                self._absent_local.pop(k, None)
            for k, v in cur_map.items():
                if k in self._prev_agent_map:
                    continue
                if k in self._pending_starts:  # survived a second scan: real
                    ev.append(("agent_start", v[0], v[1], k[1], None, None))
                    del self._pending_starts[k]
                else:
                    self._pending_starts[k] = v
            for k in list(self._pending_starts):
                if k not in cur_map:
                    del self._pending_starts[k]  # transient probe: silent
            for gid, (pct, status, label, target) in cur_goals.items():
                prev = self._prev_goal_map.get(gid, (None, None, label, (None, None, 0)))
                done_now = status in ("done", "complete", "completed") or \
                    (pct is not None and pct >= 100)
                done_prev = prev[1] in ("done", "complete", "completed") or \
                    (prev[0] is not None and prev[0] >= 100)
                # event target: (window, pid, node_alias, harness)
                gnode, gharness, gpid = target
                if done_now and not done_prev:
                    ev.append(("goal_done", label, None, gpid, gnode, gharness))
                if status in ("failed", "error") and prev[1] not in ("failed", "error"):
                    ev.append(("goal_failed", label, None, gpid, gnode, gharness))
            if ev:
                with self.lock:
                    self.events.extend(ev)
        self._events_baseline = True
        # confirmed sessions = what we have actually been tracking;
        # just-vanished ones are retained while their absence debounces
        new_prev = {k: v for k, v in cur_map.items()
                    if k in self._prev_agent_map or k not in self._pending_starts}
        for k, v in self._prev_agent_map.items():
            if k not in cur_map and self._absent_local.get(k, 0) < 2:
                new_prev[k] = v
        self._prev_agent_map = new_prev
        self._prev_goal_map = cur_goals

    def _diff_remote_events(self):
        """Same lifecycle treatment for sessions on the other nodes:
        completion/start there flashes here, tap resumes over ssh."""
        with self.lock:
            remote = list(self._remote_agents)
        cur = {(a["node"], a.get("tmux") or a["kind"]):
               (a["detail"], a.get("elapsed", 0), a["kind"])
               for a in remote}
        if getattr(self, "_remote_baseline", False):
            ev = []
            for k, (detail, elapsed, kind) in self._prev_remote_map.items():
                if k not in cur:
                    cnt = self._absent_remote.get(k, 0) + 1
                    self._absent_remote[k] = cnt
                    if cnt >= 2 and elapsed >= 90:
                        ev.append(("agent_done", detail, None, 0, k[0], kind))
            for k in cur:
                self._absent_remote.pop(k, None)
            for k, (detail, elapsed, kind) in cur.items():
                if k not in self._prev_remote_map and \
                        k not in getattr(self, "_pending_remote", {}):
                    self._pending_remote[k] = (detail, elapsed, kind)
                elif k in getattr(self, "_pending_remote", {}):
                    ev.append(("agent_start", detail, None, 0, k[0], kind))
                    del self._pending_remote[k]
            for k in list(getattr(self, "_pending_remote", {})):
                if k not in cur:
                    del self._pending_remote[k]
            if ev:
                with self.lock:
                    self.events.extend(ev)
        self._remote_baseline = True
        new_prev = {k: v for k, v in cur.items()
                    if k in getattr(self, "_prev_remote_map", {})
                    or k not in getattr(self, "_pending_remote", {})}
        for k, v in getattr(self, "_prev_remote_map", {}).items():
            if k not in cur and self._absent_remote.get(k, 0) < 2:
                new_prev[k] = v
        self._prev_remote_map = new_prev

    def _scan_fleet(self):
        # nodes up (tailscale mesh; fall back to just this Mac)
        nodes = []
        detail = []
        try:
            out = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                                 text=True, timeout=2.5).stdout
            ts = json.loads(out)
            self_name = ts.get("Self", {}).get("HostName", "")
            if self_name:
                nodes.append(self_name)
            self.net_ip = (ts.get("Self", {}).get("TailscaleIPs") or [""])[0]
            for peer in (ts.get("Peer") or {}).values():
                if peer.get("OS", "").lower() in ("ios", "android"):
                    continue  # phones: not ssh targets
                name = peer.get("HostName") or peer.get("DNSName", "").split(".")[0]
                host = (peer.get("DNSName") or name).rstrip(".")
                if host.split(".")[0].lower() in HIDE_NODES:
                    continue  # retired ghost of a live machine (fleet.json)
                friendly = node_alias(name, host)
                online = bool(peer.get("Online"))
                if online:
                    nodes.append(friendly)
                dns_label = host.split(".")[0]
                detail.append({"name": friendly, "host": host, "online": online,
                               "ssh": SSH_ALIASES.get(dns_label, dns_label),
                               "os": peer.get("OS", ""),
                               "ip": (peer.get("TailscaleIPs") or [""])[0]})
        except Exception:
            pass
        self.nodes_detail = detail
        if not nodes:
            try:
                nodes = [open("/etc/hostname").read().strip()]
            except OSError:
                nodes = ["this mac"]
        self.nodes = nodes
        # sessions run today, per harness (session files touched since 00:00)
        lt = time.localtime()
        day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        counts = {}
        pats = {"pi": os.path.expanduser("~/.pi/agent/sessions/*/*.jsonl"),
                "claude": os.path.expanduser("~/.claude/projects/*/*.jsonl"),
                "codex": os.path.expanduser("~/.codex/sessions/**/*.jsonl"),
                "opencode": os.path.expanduser("~/.local/share/opencode/**/*.jsonl")}
        for kind, pat in pats.items():
            try:
                c = sum(1 for f in glob.glob(pat, recursive=True)
                        if os.path.getmtime(f) >= day0)
                if c:
                    counts[kind] = c
            except OSError:
                continue
        self.sessions_today = counts
        # per-harness session detail for the drill-down (label, time)
        detail_sessions = {}
        for kind, pat in pats.items():
            try:
                files = [(os.path.getmtime(f), f) for f in glob.glob(pat, recursive=True)
                         if os.path.getmtime(f) >= day0]
                files.sort(reverse=True)
                rows = []
                for (mt, f) in files[:10]:
                    proj = os.path.basename(os.path.dirname(f)).strip("-")
                    label = proj.rsplit("-", 1)[-1] or proj or kind
                    rows.append((f"{kind} · {label}", time.strftime("%H:%M", time.localtime(mt))))
                if rows:
                    detail_sessions[kind] = rows
            except OSError:
                continue
        self.sessions_detail = detail_sessions
        try:
            self.uptime_sec = int(float(open("/proc/uptime").read().split()[0]))
        except OSError:
            pass
        try:
            sv = os.statvfs("/")
            self.disk_pct = int(100 - sv.f_bavail * 100 / max(sv.f_blocks, 1))
        except OSError:
            pass

    # -- main loop -------------------------------------------------------
    def collect_loop(self):
        n = 0
        while True:
            try:
                self._collect(n)
            except Exception:
                pass
            n += 1
            time.sleep(1.0)

    def _collect(self, n):
        # voice liveness ground truth is the service itself (voice.json
        # can go stale if the process dies hard); the file only enriches
        if n % 5 == 0:
            try:
                rc = subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                                     "omarchy-voice.service"], timeout=2).returncode
                self.voice_live = (rc == 0)
            except Exception:
                pass
        # cpu
        try:
            parts = [int(x) for x in open("/proc/stat").readline().split()[1:]]
            total = sum(parts)
            idle = parts[3] + parts[4]
            if self._prev_cpu:
                dt, di = total - self._prev_cpu[0], idle - self._prev_cpu[1]
                self.cpu = clamp(1 - di / max(dt, 1))
            self._prev_cpu = (total, idle)
            mi = {}
            for line in open("/proc/meminfo"):
                k, v = line.split(":")
                mi[k] = int(v.split()[0])
            self.mem = 1 - mi.get("MemAvailable", 0) / max(mi.get("MemTotal", 1), 1)
            self.mem_total_gb = mi.get("MemTotal", 0) / 1048576
            self.mem_used_gb = self.mem_total_gb * self.mem
            self.load1 = float(open("/proc/loadavg").read().split()[0])
        except OSError:
            pass
        # battery
        try:
            self.battery = int(open("/sys/class/power_supply/macsmc-battery/capacity").read())
            self.charging = open("/sys/class/power_supply/macsmc-battery/status").read().strip() in ("Charging", "Full")
        except OSError:
            pass
        # brightness / kbd via sysfs (free) ; volume via wpctl every 3s
        try:
            cur = int(open("/sys/class/backlight/apple-panel-bl/brightness").read())
            mx = int(open("/sys/class/backlight/apple-panel-bl/max_brightness").read())
            self.brightness = cur / max(mx, 1)
        except OSError:
            pass
        try:
            cur = int(open("/sys/class/leds/kbd_backlight/brightness").read())
            mx = int(open("/sys/class/leds/kbd_backlight/max_brightness").read())
            self.kbd = cur / max(mx, 1)
        except OSError:
            pass
        # touch-bar level is a USER setting (slider-owned). Seed it from
        # sysfs once at startup; never read the dimmed value back into it
        # (that ratcheted the bar to darkness).
        if not getattr(self, "_tb_seeded", False):
            try:
                cur = int(open("/sys/class/backlight/228600000.dsi.0/brightness").read())
                mx = int(open("/sys/class/backlight/228600000.dsi.0/max_brightness").read())
                self.tb = clamp(cur / max(mx, 1), 0.1, 1.0)
                self._tb_seeded = True
            except OSError:
                pass
        if n % 3 == 0:
            try:
                out = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                                     capture_output=True, text=True, timeout=1.5).stdout
                m = re.search(r"Volume:\s*([0-9.]+)", out)
                if m:
                    self.volume = float(m.group(1))
                self.muted = "[MUTED]" in out
            except Exception:
                pass
        if n % 2 == 0:
            raw = self._hypr("j/clients")
            if raw:
                try:
                    cl = json.loads(raw)
                    with self.lock:
                        self.clients = [c for c in cl if c.get("mapped", True)]
                except ValueError:
                    pass
            raw = self._hypr("j/monitors")
            if raw:
                try:
                    mons = json.loads(raw)
                    self.dpms_on = all(m.get("dpmsStatus", 1) == 1 for m in mons) if mons else True
                except ValueError:
                    pass
            raw = self._hypr("j/activewindow")
            if raw:
                try:
                    self.active_addr = json.loads(raw).get("address", "")
                except ValueError:
                    pass
        if n % 10 == 5:
            self._scan_fleet()
        if n % 45 == 10:  # remote sessions: multiplexed ssh fan-out,
            # single-flight (a slow cycle never stacks up more scanners)
            if not self._remote_scan_running:
                self._remote_scan_running = True
                def _scan_and_clear():
                    try:
                        self._scan_remote_agents()
                    finally:
                        self._remote_scan_running = False
                threading.Thread(target=_scan_and_clear, daemon=True).start()
        if n % 2 == 1:
            goals = self._scan_goals()
            agents = self._scan_agents()
            self._diff_events(agents, goals)
            # merge reported progress onto the agent it belongs to; goals
            # without a matching live agent stay as their own cards
            leftover = []
            for g in goals:
                match = next((a for a in agents
                              if (g.get("pid") and g["pid"] == a.get("pid"))
                              or g["label"] == a.get("detail")), None)
                if match:
                    match["percent"] = g["percent"]
                    match["active"] = match["active"] or g["active"]
                else:
                    leftover.append(g)
            # resolve each agent's hosting window so LIVE can dedupe:
            # a session shows once (agent card), never again as a window
            with self.lock:
                clients_now = list(self.clients)
            by_pid = {c.get("pid"): c.get("address") for c in clients_now}
            for a in agents:
                a["window"] = self._window_for_pid(a.get("pid", 0), by_pid)
            # one card per hosting window: two outermost sessions in
            # one terminal are one place to land (longest-lived wins)
            _seen_win = set()
            _deduped = []
            for a in agents:
                w = a.get("window")
                if w:
                    if w in _seen_win:
                        continue
                    _seen_win.add(w)
                _deduped.append(a)
            agents = _deduped
            # a local session earns a card when it has a terminal
            # window or survives two consecutive scans: transient
            # delegates (seconds-long sub-calls) never flicker in
            _counts = getattr(self, "_seen_counts", {})
            _new_counts, _kept = {}, []
            for a in agents:
                k = (a["kind"], a["pid"])
                _new_counts[k] = _counts.get(k, 0) + 1
                if a.get("window") or _new_counts[k] >= 2:
                    _kept.append(a)
            self._seen_counts = _new_counts
            agents = _kept
            with self.lock:
                self.agents = leftover + agents + list(self._remote_agents)
        # energy: the flow's heartbeat
        with self.lock:
            active_agents = sum(1 for a in self.agents if a["active"])
            n_agents = len(self.agents)
        target = clamp(0.10 + self.cpu * 0.45 + self.mem * 0.10
                       + active_agents * 0.16 + min(n_agents, 4) * 0.03
                       + clamp(self.load1 / 8.0) * 0.15)
        self.energy = lerp(self.energy, target, 0.06)


# ---------------------------------------------------------------- actions

def hypr_socket_path():
    runtime = os.environ.get('XDG_RUNTIME_DIR', f"/run/user/{os.getuid()}")
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if sig:
        return f"{runtime}/hypr/{sig}/.socket.sock"
    cands = sorted(glob.glob(os.path.join(runtime, "hypr", "*", ".socket.sock")),
                   key=os.path.getmtime, reverse=True)
    return cands[0] if cands else ""


def hypr_request(req):
    """Raw Hyprland IPC. Used instead of the hyprctl CLI, which cannot
    find the socket from a systemd user service (no instance env)."""
    path = hypr_socket_path()
    if not path:
        return ""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(path)
        s.sendall(req.encode())
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
        s.close()
        return out.decode(errors="replace")
    except OSError:
        return ""


class Actions:
    def __init__(self, state, uinput):
        self.st = state
        self.ui = uinput
        self._last_set = {}
        self._launched = {}  # (host-key, harness) -> foot pid (focus-or-launch)

    def _throttled(self, key, interval=0.05):
        now = time.monotonic()
        if now - self._last_set.get(key, 0) < interval:
            return False
        self._last_set[key] = now
        return True

    def key(self, code):
        if self.ui:
            self.ui.tap(code)

    def set_brightness(self, frac):
        frac = clamp(frac, 0.08, 1.0)  # SAFETY: never black out the main display
        self.st.brightness = frac
        if self._throttled("bri"):
            subprocess.Popen(["brightnessctl", "-d", "apple-panel-bl", "set", f"{int(frac * 100)}%"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def set_volume(self, frac):
        self.st.volume = frac
        if self._throttled("vol"):
            subprocess.Popen(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{frac:.2f}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def set_kbd(self, frac):
        self.st.kbd = frac
        if self._throttled("kbd"):
            subprocess.Popen(["brightnessctl", "-d", "kbd_backlight", "set", f"{int(frac * 100)}%"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def set_tb(self, frac):
        self.st.tb = frac
        if self._throttled("tb"):
            subprocess.Popen(["brightnessctl", "-d", "228600000.dsi.0", "set", f"{int(frac * 100)}%"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def toggle_voice(self, live_now):
        self.st.voice_live = not live_now
        subprocess.Popen(["systemctl", "--user", "stop" if live_now else "start",
                          "omarchy-voice.service"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def toggle_mute(self):
        subprocess.Popen(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def focus_client(self, addr):
        # This Hyprland build speaks Lua at the IPC layer: raw socket
        # "dispatch focuswindow" is rejected, but hyprctl with the
        # instance signature in env + an hl.dsp.focus call works.
        path = hypr_socket_path()
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        if path:
            env["HYPRLAND_INSTANCE_SIGNATURE"] = \
                os.path.basename(os.path.dirname(path))
        subprocess.Popen(
            ["hyprctl", "dispatch", f'hl.dsp.focus({{ window = "address:{addr}" }})'],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _focus_launched(self, key):
        """If a terminal we launched for key is still open, focus it
        instead of opening a duplicate (tmux is shared, but a second
        foot viewer per tap is just clutter)."""
        rec = self._launched.get(key)
        pid = rec.get("pid") if isinstance(rec, dict) else rec
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            del self._launched[key]
            return False
        try:
            by_pid = {c["pid"]: c["address"]
                      for c in json.loads(hypr_request("j/clients"))}
        except Exception:
            return False
        addr = by_pid.get(pid)
        if not addr:  # foot alive but window gone: stale entry
            del self._launched[key]
            return False
        self.focus_client(addr)
        return True

    def _open_foot(self, key, title, inner, owned=False, session=None):
        if self._focus_launched(key):
            return
        proc = subprocess.Popen(["foot", "--app-id=org.omarchy.dev",
                                 f"--title={title}", "-e"] + inner,
                                cwd=os.path.expanduser("~"),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Ownership comes from this runtime action, never from a
        # session-name pattern: only a session we created may be
        # closed when its viewer closes.
        self._launched[key] = {"pid": proc.pid, "owned": owned,
                               "session": session}

    def reap_closed(self):
        """When a viewer dies, close only the exact remote session
        this daemon created with it. Existing system sessions are
        detached, never killed based on their name."""
        for key, rec in list(self._launched.items()):
            pid = rec.get("pid") if isinstance(rec, dict) else rec
            try:
                os.kill(pid, 0)
                continue
            except OSError:
                pass
            del self._launched[key]
            if not isinstance(rec, dict) or not rec.get("owned"):
                continue
            host = key[0]
            sess = rec.get("session")
            if host == "local" or not sess:
                continue
            safe = sess.replace("'", "'\\''")
            subprocess.Popen(["ssh", "-o", "BatchMode=yes",
                              "-o", "ConnectTimeout=4", host,
                              f"tmux kill-session -t '{safe}'"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with self.st.lock:
                self.st._remote_agents = [
                    a for a in self.st._remote_agents
                    if not (a.get("node") == host and a.get("tmux") == sess)]

    @staticmethod
    def _ssh_loop(alias, remote_cmd):
        """Interactive ssh wrapped in a reconnect loop: if the tunnel
        drops (Wi-Fi, tailnet rebind, sleep), it reattaches to the same
        remote tmux instead of leaving a dead terminal. A connection
        that exits within seconds is failing, not dropping: back off
        exponentially (2s -> 60s cap) so a bad alias can never hammer
        sshd into rate-limiting the whole fleet."""
        script = (
            "n=0; while true; do s=$(date +%s); "
            "ssh -t -o ServerAliveInterval=10 -o ServerAliveCountMax=3 "
            "-o ConnectTimeout=8 " + alias + " '" +
            remote_cmd.replace("'", "'\\''") + "'; "
            "d=$(( $(date +%s) - s )); "
            "if [ $d -lt 5 ]; then n=$((n+1)); w=$((2**n)); "
            "[ $w -gt 60 ] && w=60; "
            "echo \"[touchbar] ssh failed fast - retry in ${w}s (attempt $n)\"; "
            "sleep $w; else n=0; "
            "echo '[touchbar] tunnel dropped - reconnecting...'; sleep 2; fi; done")
        return ["bash", "-lc", script]

    def launch_dev(self, host, node_name, harness):
        """Open a terminal: local harness, pure ssh, or harness-over-ssh.
        Harnesses always launch in their yolo modes (owner's fleet, owner's
        explicit standing instruction)."""
        title = f"{harness or 'ssh'}@{node_name}"
        if harness == "grokbot":
            if host is None:  # the desktop app only exists here
                subprocess.Popen(["/opt/Grok Bot/grok-bot"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            harness = "grok"  # on a node, grokbot means the Grok Build CLI
        if harness == "ssh":
            harness = None  # pure shell
        if host is None:  # this Mac
            if harness is None:
                inner = ["bash", "-l"]
            else:
                cmd = LOCAL_PI_CMD if harness == "pi" else HARNESS_CMD[harness]
                inner = ["bash", "-lc", cmd]
        else:
            if harness is None:
                # even a plain ssh lives in tmux: a dropped connection
                # must never kill the session (reattach is one tap)
                inner = self._ssh_loop(host, "tmux new-session -A -s tb-ssh")
            else:
                # remote harnesses live in a named tmux session: attachable
                # and resumable from any machine, forever
                remote_cmd = "tmux new-session -A -s tb-" + harness + \
                    " 'bash -lic \"" + HARNESS_CMD[harness] + "\"'"
                inner = self._ssh_loop(host, remote_cmd)
        key = (host or "local", harness or "ssh")
        created = None
        if host is not None:
            created = "tb-ssh" if harness is None else "tb-" + harness
        self._open_foot(key, title, inner,
                        owned=host is not None, session=created)

    def launch_remote_resume(self, ssh_alias, node_name, harness, tmux=None):
        """Jump into a session on another machine: attach its tmux if it
        is alive; if not, relaunch the harness with its --continue flag
        inside a fresh tmux, which resumes where it left off."""
        if tmux:
            safe = tmux.replace("'", "'\\''")
            inner = self._ssh_loop(ssh_alias, f"tmux new-session -A -s '{safe}'")
            title = (harness or "ssh") + "@" + node_name
            self._open_foot((ssh_alias, tmux), title, inner,
                            owned=False, session=tmux)
            return
        if harness and harness in HARNESS_CMD:
            cmd = RESUME_CMD.get(harness, HARNESS_CMD[harness])
            remote_cmd = "tmux new-session -A -s tb-" + harness + \
                " 'bash -lic \"" + cmd + "\"'"
            inner = self._ssh_loop(ssh_alias, remote_cmd)
            title = harness + "@" + node_name
        else:
            inner = self._ssh_loop(ssh_alias, "tmux new-session -A -s tb-ssh")
            title = "ssh@" + node_name
        # same key as launch_dev: tapping a LIVE card for a session
        # already open in a terminal focuses it, never duplicates it
        key = (ssh_alias, harness if harness in HARNESS_CMD else "ssh")
        created = "tb-" + harness if harness in HARNESS_CMD else "tb-ssh"
        self._open_foot(key, title, inner, owned=True, session=created)

    def focus_agent(self, pid):
        """Focus the terminal window hosting an agent process: walk the
        ppid chain until it meets a Hyprland client's pid."""
        try:
            by_pid = {c["pid"]: c["address"]
                      for c in json.loads(hypr_request("j/clients"))}
        except Exception:
            return
        cur = pid
        for _ in range(12):
            if cur in by_pid:
                self.focus_client(by_pid[cur])
                return
            try:
                stat = open(f"/proc/{cur}/stat").read()
                cur = int(stat.rsplit(")", 1)[1].split()[1])  # ppid
            except (OSError, ValueError, IndexError):
                return
            if cur <= 1:
                return


# ---------------------------------------------------------------- renderer

# ASCII density ramp (after Paul Bourke's classic ramp, trimmed
# before the shouty capitals/blocks): fine gradations let spiral
# wavefronts shade smoothly without turning the strip into noise.
RAMP = " .'`^\",:;Il!i><~+_-?][}{1)(|\\/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#"
# FLOW style presets for owner A/B testing: TOUCHBAR_FLOW env picks one
FLOW_PRESETS = {
    "calm": {"ramp": RAMP, "fsize": 11, "spiral_w": 0.22,
             "spiral_spd": (0.12, 0.18), "inten": (0.36, 0.30)},
    "bourke": {"ramp": RAMP + "MW&8%B@$", "fsize": 11, "spiral_w": 0.34,
               "spiral_spd": (0.22, 0.30), "inten": (0.30, 0.48)},
    "braille": {"ramp": "⠀⠁⠉⠋⠛⠟⠿⡿⣿", "fsize": 16, "spiral_w": 0.22,
                "spiral_spd": (0.12, 0.18), "inten": (0.32, 0.42)},
    "blocks": {"ramp": " ▁▂▃▄▅▆▇█", "fsize": 11, "spiral_w": 0.22,
               "spiral_spd": (0.12, 0.18), "inten": (0.30, 0.48),
               "smooth": 0.07, "current": 0.55},
    "waves": {"ramp": RAMP, "fsize": 11, "spiral_w": 0.0,
              "spiral_spd": (0.12, 0.18), "inten": (0.36, 0.30)},
    "waveglyphs": {"ramp": " .·:≈≋~", "fsize": 11, "spiral_w": 0.22,
                   "spiral_spd": (0.12, 0.18), "inten": (0.30, 0.48)},
    "motion": {"ramp": RAMP, "fsize": 11, "spiral_w": 0.26,
               "spiral_spd": (0.38, 0.45), "inten": (0.30, 0.44)},
    "braille-smooth": {"ramp": "⠀⠁⠉⠋⠛⠟⠿⡿⣿", "fsize": 16,
                       "spiral_w": 0.16, "spiral_spd": (0.16, 0.22),
                       "inten": (0.20, 0.32), "smooth": 0.08, "current": 0.7},
    "drift": {"ramp": RAMP, "fsize": 11, "spiral_w": 0.0,
              "spiral_spd": (0.12, 0.18), "inten": (0.26, 0.34),
              "smooth": 0.09, "current": 0.42},
    # OWNER-FROZEN 2026-10-22: slow streaming waves, mostly dead
    # space at rest, density follows fleet agent work. Do not retune
    # without the owner watching (pinned by test_flow_owner_frozen).
    "classic": {"ramp": " ·:∙~+=×*#%@", "fsize": 11, "spiral_w": 0.0,
                "spiral_spd": (0.05, 0.08), "inten": (-0.06, 0.95),
                "smooth": 0.08, "current": 0.13, "levels": 1.0},
    "drift2": {"ramp": RAMP, "fsize": 11, "spiral_w": 0.0,
               "spiral_spd": (0.12, 0.18), "inten": (0.20, 0.26),
               "smooth": 0.07, "current": 0.30},
}
FLOW_COLORS = 14


class Renderer:
    def __init__(self, theme, state):
        self.th = theme
        self.st = state
        self.style = FLOW_PRESETS.get(os.environ.get("TOUCHBAR_FLOW", "classic"),
                                      FLOW_PRESETS["classic"])
        self._build_atlas()
        self.col_phase = [0.0] * 260  # per-column turbulence walk
        self._smooth = [0.0] * (223 * 5)  # temporal smoothing grid for FLOW
        self.dev_state = ("host", None, None)  # (step, node dict|None=local, harness id)
        self.live_sel = None  # LIVE two-tap selection key, set by the daemon
        self.live_sel_at = 0.0
        self.ripples = []     # FLOW touch ripples [(x, y, t0)]
        self.touch_xy = None  # FLOW finger position while touching
        self.touch_t = 0.0
        self.slide_dx = 0  # DEV step transition offset, set by the daemon
        self.pod_open = False  # FLOW: quiet right folder expanded?
        self.flashes = []  # [(theme color key, label, t0)] completion flashes
        self.notification = None  # actionable pill: {label,color,window,pid,t0}
        self._notif_rect = None   # last drawn pill rect (top-most hit zone)

    # -- helpers ---------------------------------------------------------
    def _build_atlas(self):
        """Pre-render every ramp glyph in FLOW_COLORS gradient steps."""
        stops = [self.th["dark_foreground"], self.th["cyan"], self.th["bright_cyan"],
                 self.th["accent"], self.th["bright_magenta"], self.th["bright_foreground"]]
        self.atlas = []
        cw, ch = 9, 12
        for ci in range(FLOW_COLORS):
            t = ci / (FLOW_COLORS - 1)
            # walk the stop list
            pos = t * (len(stops) - 1)
            i, f = int(pos), pos - int(pos)
            col = tuple(lerp(stops[i][k], stops[min(i + 1, len(stops) - 1)][k], f) for k in range(3))
            row = []
            for glyph in self.style["ramp"]:
                s = cairo.ImageSurface(cairo.FORMAT_ARGB32, cw, ch)
                ctx = cairo.Context(s)
                ctx.set_source_rgb(*col)
                ctx.select_font_face(self.th.font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                ctx.set_font_size(self.style["fsize"])
                ext = ctx.text_extents(glyph)
                ctx.move_to((cw - ext.width) / 2 - ext.x_bearing, (ch + ext.height) / 2 - ext.y_bearing - 1)
                ctx.show_text(glyph)
                s.flush()
                row.append(s)
            self.atlas.append(row)

    @staticmethod
    def media_icon(ctx, name, cx, cy, s, color):
        """Vector media glyphs: crisp at 60px, no font dependency."""
        ctx.set_source_rgb(*color)
        if name == "play":
            ctx.new_path()
            ctx.move_to(cx - s * 0.45, cy - s * 0.62)
            ctx.line_to(cx - s * 0.45, cy + s * 0.62)
            ctx.line_to(cx + s * 0.68, cy)
            ctx.close_path()
            ctx.fill()
        elif name == "pause":
            ctx.rectangle(cx - s * 0.55, cy - s * 0.6, s * 0.38, s * 1.2)
            ctx.rectangle(cx + s * 0.17, cy - s * 0.6, s * 0.38, s * 1.2)
            ctx.fill()
        elif name in ("prev", "next"):
            d = 1 if name == "next" else -1
            ctx.rectangle(cx + d * s * 0.42 - s * 0.09, cy - s * 0.6, s * 0.18, s * 1.2)
            ctx.fill()
            ctx.new_path()
            ctx.move_to(cx - d * s * 0.38, cy - s * 0.62)
            ctx.line_to(cx - d * s * 0.38, cy + s * 0.62)
            ctx.line_to(cx + d * s * 0.55, cy)
            ctx.close_path()
            ctx.fill()
        elif name == "mic":
            ctx.new_path()
            ctx.set_line_width(1.8)
            rr2 = Renderer.rr
            rr2(ctx, cx - s * 0.16, cy - s * 0.55, s * 0.32, s * 0.62, s * 0.16)
            ctx.fill()
            ctx.new_path()
            ctx.arc(cx, cy + s * 0.02, s * 0.38, 0.15 * 3.14159, 0.85 * 3.14159)
            ctx.stroke()
            ctx.new_path()
            ctx.move_to(cx, cy + s * 0.4)
            ctx.line_to(cx, cy + s * 0.62)
            ctx.stroke()
        elif name in ("mute", "unmute"):
            ctx.new_path()  # speaker body
            ctx.move_to(cx - s * 0.62, cy - s * 0.22)
            ctx.line_to(cx - s * 0.28, cy - s * 0.22)
            ctx.line_to(cx + s * 0.05, cy - s * 0.55)
            ctx.line_to(cx + s * 0.05, cy + s * 0.55)
            ctx.line_to(cx - s * 0.28, cy + s * 0.22)
            ctx.line_to(cx - s * 0.62, cy + s * 0.22)
            ctx.close_path()
            ctx.fill()
            ctx.set_line_width(2.0)
            if name == "unmute":
                for rad in (0.42, 0.68):
                    ctx.new_path()
                    ctx.arc(cx + s * 0.05, cy, s * rad, -0.9, 0.9)
                    ctx.stroke()
            else:
                ctx.new_path()
                ctx.move_to(cx + s * 0.22, cy - s * 0.34)
                ctx.line_to(cx + s * 0.62, cy + s * 0.34)
                ctx.move_to(cx + s * 0.62, cy - s * 0.34)
                ctx.line_to(cx + s * 0.22, cy + s * 0.34)
                ctx.stroke()

    @staticmethod
    def rr(ctx, x, y, w, h, r):
        r = min(r, w / 2, h / 2)
        ctx.new_path()
        ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        ctx.close_path()

    def text(self, ctx, txt, x, y, size, color, weight=cairo.FONT_WEIGHT_NORMAL, alpha=1.0):
        ctx.select_font_face(self.th.font, cairo.FONT_SLANT_NORMAL, weight)
        ctx.set_font_size(size)
        r, g, b = color
        ctx.set_source_rgba(r, g, b, alpha)
        ctx.move_to(x, y)
        ctx.show_text(txt)

    def bg(self, ctx):
        if getattr(self, "_bg_cache", None) is None:
            s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
            c = cairo.Context(s)
            grad = cairo.LinearGradient(0, 0, W, 0)
            d = self.th["darker_background"]
            b = self.th["background"]
            grad.add_color_stop_rgb(0, *d)
            grad.add_color_stop_rgb(0.5, *b)
            grad.add_color_stop_rgb(1, *d)
            c.set_source(grad)
            c.paint()
            c.set_source_rgba(1, 1, 1, 0.016)
            for y in range(0, H, 3):
                c.rectangle(0, y, W, 1)
                c.fill()
            s.flush()
            self._bg_cache = s
        ctx.set_source_surface(self._bg_cache, 0, 0)
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)

    # -- rail --------------------------------------------------------------
    def rail(self, ctx, view, hits, t, pressed=None):
        # translucent backdrop so the FLOW field never bleeds through labels
        self.rr(ctx, 4, 3, RAIL_W - 2, H - 6, 12)
        ctx.set_source_rgba(*self.th["darker_background"], 0.88)
        ctx.fill()
        x0, y0, seg_h, gap = 8, 6, 48, 5
        seg_w = (RAIL_W - 2 * x0 - (len(VIEWS) - 1) * gap) / len(VIEWS)
        icons = {"flow": "flow", "keys": "keys", "ctrl": "ctrl", "live": "live", "dev": "dev"}
        labels = {"flow": "FLOW", "keys": "KEYS", "ctrl": "CTRL", "live": "LIVE", "dev": "DEV"}
        for i, v in enumerate(VIEWS):
            x = x0 + i * (seg_w + gap)
            sel = (v == view)
            if pressed and pressed.get("rail:" + v, 0) > time.monotonic() and not sel:
                self.rr(ctx, x, y0, seg_w, seg_h, 10)
                ctx.set_source_rgba(*self.th["accent"], 0.30)
                ctx.fill()
            if sel:
                self.rr(ctx, x, y0, seg_w, seg_h, 10)
                a = self.th["accent"]
                ctx.set_source_rgba(*a, 0.16 + 0.04 * math.sin(t * 2))
                ctx.fill()
                self.rr(ctx, x, y0, seg_w, seg_h, 10)
                ctx.set_source_rgba(*a, 0.75)
                ctx.set_line_width(1)
                ctx.stroke()
            col = self.th["bright_foreground"] if sel else self.th["dark_foreground"]
            cx = x + seg_w / 2
            ctx.set_source_rgba(*col, 0.95 if sel else 0.8)
            ctx.set_line_width(1.6)
            # vector glyphs
            if icons[v] == "flow":
                ctx.new_path()
                for px in range(-14, 15):
                    py = 16 + 4.5 * math.sin(px * 0.30 + t * (2.2 if sel else 0.8))
                    ctx.line_to(cx + px, y0 + py) if px > -14 else ctx.move_to(cx + px, y0 + py)
                ctx.stroke()
            elif icons[v] == "keys":
                self.text(ctx, "F1", cx - 8, y0 + 23, 12, col, cairo.FONT_WEIGHT_BOLD)
            elif icons[v] == "ctrl":
                for j, yy in enumerate((11, 19, 27)):
                    ctx.new_path()
                    ctx.move_to(cx - 14, y0 + yy)
                    ctx.line_to(cx + 14, y0 + yy)
                    ctx.set_line_width(1.4)
                    ctx.stroke()
                    kx = cx + (-6, 5, -2)[j]
                    ctx.new_path()
                    ctx.arc(kx, y0 + yy, 3, 0, 6.29)
                    ctx.set_source_rgba(*col, 0.95)
                    ctx.fill()
                    ctx.set_source_rgba(*col, 0.95 if sel else 0.8)
            elif icons[v] == "dev":
                self.text(ctx, ">_", cx - 10, y0 + 25, 15, col, cairo.FONT_WEIGHT_BOLD)
            elif icons[v] == "live":
                ctx.new_path()
                pts = [(-14, 18), (-8, 18), (-5, 9), (-1, 26), (3, 18), (14, 18)]
                for j, (px, py) in enumerate(pts):
                    ctx.line_to(cx + px, y0 + py) if j else ctx.move_to(cx + px, y0 + py)
                ctx.stroke()
                # agent count badge
                with self.st.lock:
                    na = len(self.st.agents)
                if na:
                    ctx.set_source_rgb(*self.th["green"])
                    ctx.arc(x + seg_w - 9, y0 + 8, 3.4, 0, 6.29)
                    ctx.fill()
            # label
            lbl = labels[v]
            ctx.select_font_face(self.th.font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(11)
            ext = ctx.text_extents(lbl)
            self.text(ctx, lbl, cx - ext.width / 2, y0 + 42, 11, col, cairo.FONT_WEIGHT_BOLD)
            hits.append((x, y0, seg_w, seg_h, "view", v))
        # divider
        ctx.set_source_rgba(*self.th["muted"], 0.5)
        ctx.rectangle(RAIL_W - 5, 8, 1, H - 16)
        ctx.fill()

    # -- FLOW --------------------------------------------------------------
    def add_ripple(self, x, y):
        now = time.monotonic()
        self.ripples.append((x, y, now))
        self.ripples = [r for r in self.ripples if now - r[2] < 1.8][-6:]

    def set_touch(self, x, y, active=True):
        self.touch_xy = (x, y) if active else None
        self.touch_t = time.monotonic()

    def _touch_wave(self, x, y, t):
        """Water response: a standing wave under the finger plus
        expanding rings from taps/drags. Zero when untouched."""
        w = 0.0
        if self.touch_xy and t - self.touch_t < 2.5:
            d = math.hypot(x - self.touch_xy[0], y - self.touch_xy[1])
            w += math.sin(d * 0.10 - t * 12.0) * math.exp(-d / 150.0) * 0.85
        for (rx, ry, rt) in self.ripples:
            age = t - rt
            if 0 <= age < 1.8:
                dd = abs(math.hypot(x - rx, y - ry) - age * 300.0)
                w += math.sin(dd * 0.14) * math.exp(-dd / 34.0) \
                    * math.exp(-age * 1.5) * 0.8
        return w

    def view_flow(self, ctx, hits, t, pressed, background=False):
        # smooth energy per frame (the collector steps it once a second;
        # per-frame easing keeps the current from micro-lurching)
        e = self._e_smooth = lerp(getattr(self, "_e_smooth", self.st.energy),
                                  self.st.energy, 0.04)
        # field phases run on wall time, not process time: a daemon
        # restart rejoins the same river at the same phase instead of
        # resetting to a bare dot grid (owner-approved look, frozen).
        # `t` (monotonic) still drives ripples/touch expiry.
        ft = time.time()
        ctx.set_source_rgba(*self.th["darker_background"], 1)
        ctx.rectangle(0, 0, W, H)
        ctx.fill()
        cw, ch = 9, 12
        cols = W // cw
        rows = 5
        # water, not a pulse: energy sets the current's pace and swell,
        # never the brightness (the old 0.35..1.0 brightness swing was
        # the in-and-out breathing the owner hated). Kept deliberately
        # quiet: slow phases, compressed contrast, heavy easing.
        lvl = self.style.get("levels", 0.0)  # energy leverage per style
        speed = (0.85 + 0.75 * e) * self.style.get("current", 1.0) \
            * (1.0 + lvl * 1.1 * e)
        amp = 0.75 + 0.7 * e
        # pinwheel vortices anchored at bar thirds with a slow wander;
        # teleporting centers were a major source of visual jumping
        # spiral wavefronts: two slow vortices above/below the strip,
        # so their arms sweep the bar as curved, rotating waves
        spirals = [(W * 0.30, -H * 1.7, 1, 2, 0.95),
                   (W * 0.72, H * 2.7, -1, 3, 0.75)]
        _spd = self.style["spiral_spd"]
        spiral_t = ft * (_spd[0] + _spd[1] * e)
        self.ripples = [r for r in self.ripples if t - r[2] < 1.8]
        touch_on = bool(self.ripples) or \
            (self.touch_xy and t - self.touch_t < 2.5)
        # per-row trig factors for the base current: sin(p + o) factorization
        row_f = []
        for r in range(rows):
            # static row phase: rows are offset copies of one traveling
            # wave (a river surface), not independently wobbling rows —
            # the old time-varying row phase was a shimmer source.
            b = r * 0.55
            row_f.append((math.sin(b), math.cos(b), math.sin(r * 1.7), math.cos(r * 1.7)))
        for cxi in range(cols):
            x = cxi * cw
            p1 = x * 0.006 + ft * speed
            p2 = x * 0.013 - ft * speed * 0.62
            s1, c1 = math.sin(p1), math.cos(p1)
            s2, c2 = math.sin(p2), math.cos(p2)
            for r in range(rows):
                ybase = r * ch
                y = ybase + ch / 2
                sb, cb, so, co = row_f[r]
                height = 0.34 * (s1 * cb + c1 * sb) + 0.06 * (s2 * co + c2 * so)
                for (cx, cy, dirn, arms, wgt) in spirals:
                    dx, dy = x + cw / 2 - cx, y - cy
                    rad = math.hypot(dx, dy)
                    ang = math.atan2(dy, dx)
                    height += wgt * self.style["spiral_w"] * (1.0 + lvl * 2.4 * e) * math.sin(
                        arms * ang * dirn + rad * 0.040 - spiral_t * dirn) \
                        * math.exp(-rad / 1100.0)
                if touch_on:  # the water answers the finger
                    height += self._touch_wave(x + cw / 2, y, t)
                _ia, _ib = self.style["inten"]
                # dead space by default: only wave crests clear the
                # skip threshold when energy is low. As agent work
                # across the fleet raises energy, the base and swing
                # grow, so waves fill in with denser glyphs instead
                # of the whole strip lighting up.
                inten = clamp(_ia + lvl * 0.24 * e
                              + (_ib + lvl * 0.36 * e) * height) ** 1.1
                # temporal smoothing: cells ease toward their target instead
                # of snapping every frame (kills the jumping/flicker);
                # faster while touching so ripples track the finger.
                # First frame after start snaps: no warm-up fade, the
                # river is simply there (pairs with wall-time phases).
                key = r * cols + cxi
                prev = self._smooth[key]
                factor = 1.0 if not getattr(self, "_flow_warm", False) \
                    else (0.30 if touch_on else self.style.get("smooth", 0.14))
                inten = prev + (inten - prev) * factor
                if getattr(self, "_flow_warm", False):
                    # rate-limit the DISPLAYED level: a glyph band is
                    # ~0.09 wide, so capping travel at 0.006/frame
                    # means a cell holds each glyph ~0.75s minimum.
                    # Without this, cells hovering at a band edge
                    # flip-flop every frame — that flip-flop, not the
                    # wave drift, is what reads as jitter on the bar.
                    step = 0.05 if touch_on else 0.006
                    inten = prev + max(-step, min(step, inten - prev))
                self._smooth[key] = inten
                if inten < 0.11:
                    continue
                gi = min(int(inten * len(self.style["ramp"])), len(self.style["ramp"]) - 1)
                ci = min(int(inten * (FLOW_COLORS - 1)), FLOW_COLORS - 1)
                ctx.set_source_surface(self.atlas[ci][gi], x, ybase)
                ctx.paint_with_alpha(clamp(0.25 + inten))
        self._flow_warm = True
        # vignette edges
        for side in (0, 1):
            grad = cairo.LinearGradient(W * side, 0, W * side + (90 if side == 0 else -90), 0) \
                if side == 0 else cairo.LinearGradient(W, 0, W - 90, 0)
            grad.add_color_stop_rgba(0, *self.th["darker_background"], 0.9)
            grad.add_color_stop_rgba(1, *self.th["darker_background"], 0)
            ctx.set_source(grad)
            ctx.rectangle(W - 90 if side else 0, 0, 90, H)
            ctx.fill()
        if not background:
            if self._notif_active(t):
                self._notify_pill(ctx, hits, t)
            else:
                self._flow_chips(ctx, hits, t)
            self._voice_switch(ctx, hits, t)
            if self.pod_open:
                self._flow_pod(ctx, hits, t, pressed)
            else:
                self._flow_handle(ctx, hits)

    def _voice_switch(self, ctx, hits, t):
        """Persistent voice-agent switch, bottom-right of FLOW. Shows live
        state from the voice service's state file; tap toggles the service."""
        live = self.st.voice_live
        if self.pod_open:
            return  # the open pod owns the right side
        x, y, w, h = W - 124, 7, 52, 46  # left of the controls folder tab
        self.rr(ctx, x, y, w, h, 10)
        ctx.set_source_rgba(*self.th["darker_background"], 0.85)
        ctx.fill()
        self.rr(ctx, x, y, w, h, 10)
        col = self.th["bright_green"] if live else self.th["muted"]
        ctx.set_source_rgba(*col, 0.8 if live else 0.55)
        ctx.set_line_width(1.2)
        ctx.stroke()
        cx = x + w / 2
        # mic glyph (vector): capsule + arc + stem
        mcol = self.th["bright_foreground"] if live else self.th["dark_foreground"]
        ctx.set_source_rgb(*mcol)
        self.rr(ctx, cx - 4, 15, 8, 15, 4)
        ctx.fill()
        ctx.set_line_width(1.7)
        ctx.new_path()
        ctx.arc(cx, 29, 9, 0.15 * math.pi, 0.85 * math.pi)
        ctx.stroke()
        ctx.new_path()
        ctx.move_to(cx, 38)
        ctx.line_to(cx, 43)
        ctx.stroke()
        if live:  # pulsing live dot
            pulse = 0.55 + 0.45 * math.sin(t * 4)
            ctx.set_source_rgba(*self.th["bright_green"], pulse)
            ctx.arc(x + w - 11, 15, 3.5, 0, 6.29)
            ctx.fill()
        hits.append((x, y, w, h, "voice", None))

    def _flow_handle(self, ctx, hits):
        """The quiet right folder, collapsed: a slim dim tab. Tap to expand
        the brightness/volume/playback pod; it folds itself away again."""
        x = W - 64
        self.rr(ctx, x, 7, 52, 46, 10)
        ctx.set_source_rgba(*self.th["darker_background"], 0.85)
        ctx.fill()
        self.rr(ctx, x, 7, 52, 46, 10)
        ctx.set_source_rgba(*self.th["muted"], 0.55)
        ctx.set_line_width(1)
        ctx.stroke()
        cx = x + 26
        col = self.th["dark_foreground"]
        ctx.set_source_rgba(*col, 0.95)
        ctx.set_line_width(1.6)
        # sun and speaker share one optical size (~17px boxes): the sun
        # used to dwarf the speaker glyph in this little tab
        ctx.new_path()  # sun
        ctx.arc(cx, 20, 4.2, 0, 6.29)
        ctx.stroke()
        for a in range(8):
            ang = a * 0.785
            ctx.new_path()
            ctx.move_to(cx + 6.2 * math.cos(ang), 20 + 6.2 * math.sin(ang))
            ctx.line_to(cx + 8.4 * math.cos(ang), 20 + 8.4 * math.sin(ang))
            ctx.stroke()
        self.media_icon(ctx, "unmute", cx, 39, 12, col)
        hits.append((x, 7, 52, 46, "podopen", None))

    POD_W = 1006  # functional pod floating on the flow (right side)

    def _flow_pod(self, ctx, hits, t, pressed):
        """Real, readable controls on the home view: BRIGHT + VOLUME sliders
        and media buttons, on a solid pod so they read over the flow."""
        px = W - self.POD_W - 8
        self.rr(ctx, px, 5, self.POD_W, 50, 12)
        ctx.set_source_rgba(*self.th["darker_background"], 0.88)
        ctx.fill()
        self.rr(ctx, px, 5, self.POD_W, 50, 12)
        ctx.set_source_rgba(*self.th["muted"], 0.6)
        ctx.set_line_width(1)
        ctx.stroke()
        sliders = [("bri", "DISPLAY", self.st.brightness), ("vol", "VOLUME", self.st.volume)]
        sx = px + 16
        for name, label, val in sliders:
            tw = 272
            self.text(ctx, label, sx, 22, 12, self.th["dark_foreground"], cairo.FONT_WEIGHT_BOLD)
            pct = f"{int(val * 100)}%"
            ctx.select_font_face(self.th.font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(13)
            ext = ctx.text_extents(pct)
            self.text(ctx, pct, sx + tw - ext.width, 22, 13, self.th["accent"], cairo.FONT_WEIGHT_BOLD)
            self.rr(ctx, sx, 33, tw, 6, 3)
            ctx.set_source_rgba(*self.th["muted"], 0.65)
            ctx.fill()
            if val > 0.005:
                self.rr(ctx, sx, 33, max(tw * val, 6), 6, 3)
                grad = cairo.LinearGradient(sx, 0, sx + tw, 0)
                grad.add_color_stop_rgb(0, *self.th["cyan"])
                grad.add_color_stop_rgb(1, *self.th["accent"])
                ctx.set_source(grad)
                ctx.fill()
            kx = sx + tw * val
            ctx.set_source_rgb(*self.th["bright_foreground"])
            ctx.arc(kx, 36, 7, 0, 6.29)
            ctx.fill()
            ctx.set_source_rgb(*self.th["accent"])
            ctx.arc(kx, 36, 3, 0, 6.29)
            ctx.fill()
            hits.append((sx - 8, 10, tw + 16, 42, "slider", name))
            sx += tw + 28
        media = [("prev", "prev", KEY_PREVIOUSSONG), ("play", "play", KEY_PLAYPAUSE),
                 ("next", "next", KEY_NEXTSONG),
                 ("mute", "mute" if self.st.muted else "unmute", None),
                 ("voice", "mic", None)]
        for i, (name, icon, code) in enumerate(media):
            bx = sx + 4 + i * 68
            is_pressed = pressed.get(name, 0) > time.monotonic()
            self.rr(ctx, bx, 9, 60, 42, 9)
            ctx.set_source_rgba(*(self.th["accent"] if is_pressed else self.th["lighter_background"]),
                                0.9 if is_pressed else 0.65)
            ctx.fill()
            self.media_icon(ctx, icon, bx + 30, 30, 13,
                            self.th["darker_background"] if is_pressed else self.th["bright_foreground"])
            hits.append((bx, 9, 60, 42, "media", (name, code)))
        # visible fold-away button at the far right: this corner used
        # to close the pod invisibly via the field-tap catch-all
        bx = px + self.POD_W - 52
        is_pressed = pressed.get("podclose", 0) > time.monotonic()
        self.rr(ctx, bx, 9, 46, 42, 9)
        ctx.set_source_rgba(*(self.th["accent"] if is_pressed else self.th["lighter_background"]),
                            0.9 if is_pressed else 0.65)
        ctx.fill()
        cc = self.th["darker_background"] if is_pressed else self.th["bright_foreground"]
        ctx.set_source_rgb(*cc)
        ctx.set_line_width(2.4)
        ctx.new_path()  # right chevron: folds back toward the edge
        ctx.move_to(bx + 18, 21)
        ctx.line_to(bx + 29, 30)
        ctx.line_to(bx + 18, 39)
        ctx.stroke()
        hits.append((bx, 9, 46, 42, "podclose", None))

    @staticmethod
    def batt_color(pct, charging):
        """Traffic-light battery color: green charging or healthy,
        amber under half, red at 20% and below."""
        if charging or pct > 50:
            return (0.35, 0.85, 0.45)
        if pct <= 20:
            return (0.95, 0.36, 0.33)
        return (0.98, 0.76, 0.28)

    def _batt_icon(self, ctx, x, cy, pct, charging):
        """Small battery glyph: outline + level fill in batt_color,
        lightning bolt cut in when charging. Returns icon width."""
        col = self.batt_color(pct, charging)
        bw, bh = 26, 13
        y = cy - bh / 2
        self.rr(ctx, x, y, bw, bh, 3.5)
        ctx.set_source_rgba(*col, 0.95)
        ctx.set_line_width(1.4)
        ctx.stroke()
        ctx.rectangle(x + bw + 1.5, cy - 3, 3, 6)
        ctx.set_source_rgba(*col, 0.95)
        ctx.fill()
        fillw = max((bw - 4) * max(pct, 0) / 100, 2 if pct > 0 else 0)
        if fillw:
            self.rr(ctx, x + 2, y + 2, fillw, bh - 4, 2)
            ctx.set_source_rgba(*col, 0.9)
            ctx.fill()
        if charging:
            cx = x + bw / 2
            # dark bolt over the colored fill; colored bolt where the
            # fill has not reached yet (low charge would hide it)
            if fillw >= bw / 2:
                ctx.set_source_rgb(*self.th["darker_background"])
            else:
                ctx.set_source_rgba(*col, 0.95)
            ctx.new_path()
            ctx.move_to(cx + 1.5, cy - 5.5)
            ctx.line_to(cx - 3.2, cy + 0.8)
            ctx.line_to(cx - 0.4, cy + 0.8)
            ctx.line_to(cx - 1.5, cy + 5.5)
            ctx.line_to(cx + 3.2, cy - 0.8)
            ctx.line_to(cx + 0.4, cy - 0.8)
            ctx.close_path()
            ctx.fill()
        return bw + 5

    def _flow_chips(self, ctx, hits, t):
        # compact status pill, centered, hugging its content so the
        # ASCII field stays visible around it: utilization, time,
        # battery icon + percentage in a row (tap: drill down)
        now = time.localtime()
        zone_r = W - (self.POD_W + 26) if self.pod_open else W - 140
        zx0, zx1 = RAIL_W + 12, zone_r
        if zx1 - zx0 < 320:
            return
        with self.st.lock:
            na = sum(1 for a in self.st.agents if a["active"])
            nt = len(self.st.agents)
        util = f"CPU {int(self.st.cpu * 100)}%"
        if nt:
            util += f"  {na}/{nt}"
        tstr = time.strftime("%H:%M", now)
        ctx.select_font_face(self.th.font, cairo.FONT_SLANT_NORMAL,
                             cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(15)
        uw = ctx.text_extents(util).width
        ctx.set_font_size(19)
        tw = ctx.text_extents(tstr).width
        batt_w = 0
        pstr, col = "", None
        if self.st.battery >= 0:
            pstr = f"{self.st.battery}%"
            col = self.batt_color(self.st.battery, self.st.charging)
            ctx.set_font_size(15)
            batt_w = 31 + 6 + ctx.text_extents(pstr).width
        gap = 26
        content = uw + gap + tw + (gap + batt_w if batt_w else 0)
        pw = content + 36
        px = (zx0 + zx1) / 2 - pw / 2
        self.rr(ctx, px, 13, pw, 34, 9)
        ctx.set_source_rgba(*self.th["darker_background"], 0.78)
        ctx.fill()
        self.rr(ctx, px, 13, pw, 34, 9)
        ctx.set_source_rgba(*self.th["muted"], 0.55)
        ctx.set_line_width(1)
        ctx.stroke()
        x = px + 18
        self.text(ctx, util, x, 37, 15, self.th["bright_foreground"],
                  cairo.FONT_WEIGHT_BOLD)
        x += uw + gap
        self.text(ctx, tstr, x, 37, 19, self.th["bright_foreground"],
                  cairo.FONT_WEIGHT_BOLD)
        x += tw + gap
        if batt_w:
            iw = self._batt_icon(ctx, x, 30, self.st.battery,
                                 self.st.charging)
            self.text(ctx, pstr, x + iw + 6, 37, 15, col,
                      cairo.FONT_WEIGHT_BOLD)
        hits.append((px, 13, pw, 34, "view", "fleet"))

    # -- KEYS ---------------------------------------------------------------
    def view_keys(self, ctx, hits, t, pressed):
        keys = [("esc", "ESC", KEY_ESC)] + [(f"F{i}", f"F{i}", KEY_F[i]) for i in range(1, 13)]
        x = RAIL_W + 6
        avail = W - x - 10
        kw = (avail - (len(keys) - 1) * 6) / len(keys)
        for i, (name, label, code) in enumerate(keys):
            bx = x + i * (kw + 6)
            is_pressed = pressed.get(name, 0) > time.monotonic()
            self.rr(ctx, bx, 8, kw, 44, 9)
            if is_pressed:
                ctx.set_source_rgba(*self.th["accent"], 0.85)
                ctx.fill()
                col = self.th["darker_background"]
            else:
                ctx.set_source_rgba(*self.th["lighter_background"], 0.55)
                ctx.fill()
                self.rr(ctx, bx, 8, kw, 44, 9)
                ctx.set_source_rgba(*self.th["muted"], 0.8)
                ctx.set_line_width(1)
                ctx.stroke()
                col = self.th["bright_foreground"]
            ctx.select_font_face(self.th.font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(20)
            ext = ctx.text_extents(label)
            self.text(ctx, label, bx + (kw - ext.width) / 2, 39, 20,
                      col, cairo.FONT_WEIGHT_BOLD)
            hits.append((bx, 8, kw, 44, "key", (name, code)))

    # -- CTRL ---------------------------------------------------------------
    SLIDERS = [("bri", "DISPLAY", "brightness"), ("vol", "VOLUME", "volume"),
               ("kbd", "KEY BD", "kbd"), ("tb", "TOUCHBAR", "tb")]

    def view_ctrl(self, ctx, hits, t, pressed):
        x = RAIL_W + 14
        # media cluster at right
        media = [("prev", "prev", KEY_PREVIOUSSONG), ("play", "play", KEY_PLAYPAUSE),
                 ("next", "next", KEY_NEXTSONG),
                 ("mute", "mute" if self.st.muted else "unmute", None)]
        mw = 64
        mx = W - 10 - len(media) * (mw + 6)
        for i, (name, icon, code) in enumerate(media):
            bx = mx + i * (mw + 6)
            is_pressed = pressed.get(name, 0) > time.monotonic()
            self.rr(ctx, bx, 8, mw, 44, 9)
            ctx.set_source_rgba(*(self.th["accent"] if is_pressed else self.th["lighter_background"]),
                                0.85 if is_pressed else 0.55)
            ctx.fill()
            if not is_pressed:
                self.rr(ctx, bx, 8, mw, 44, 9)
                ctx.set_source_rgba(*self.th["muted"], 0.8)
                ctx.set_line_width(1)
                ctx.stroke()
            self.media_icon(ctx, icon, bx + mw / 2, 30, 14,
                            self.th["darker_background"] if is_pressed else self.th["bright_foreground"])
            hits.append((bx, 8, mw, 44, "media", (name, code)))
        # sliders
        avail = mx - x - 30
        sw = avail / len(self.SLIDERS)
        vals = {"bri": self.st.brightness, "vol": self.st.volume,
                "kbd": self.st.kbd, "tb": self.st.tb}
        for i, (name, label, attr) in enumerate(self.SLIDERS):
            bx = x + i * sw
            tw = sw - 36
            val = vals[name]
            pct = f"{int(val * 100)}%"
            ctx.select_font_face(self.th.font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(12)
            ext = ctx.text_extents(pct)
            # label must never collide with the right-aligned percentage
            label_fit = label
            while label_fit and ctx.text_extents(label_fit).width > tw - ext.width - 10:
                label_fit = label_fit[:-1]
            self.text(ctx, label_fit, bx, 22, 12, self.th["dark_foreground"], cairo.FONT_WEIGHT_BOLD)
            self.text(ctx, pct, bx + tw - ext.width, 22, 12, self.th["accent"], cairo.FONT_WEIGHT_BOLD)
            ty = 38
            self.rr(ctx, bx, ty - 3, tw, 6, 3)
            ctx.set_source_rgba(*self.th["muted"], 0.65)
            ctx.fill()
            if val > 0.005:
                self.rr(ctx, bx, ty - 3, max(tw * val, 6), 6, 3)
                grad = cairo.LinearGradient(bx, 0, bx + tw, 0)
                grad.add_color_stop_rgb(0, *self.th["cyan"])
                grad.add_color_stop_rgb(1, *self.th["accent"])
                ctx.set_source(grad)
                ctx.fill()
            # knob
            kx = bx + tw * val
            ctx.set_source_rgb(*self.th["bright_foreground"])
            ctx.arc(kx, ty, 7, 0, 6.29)
            ctx.fill()
            ctx.set_source_rgb(*self.th["accent"])
            ctx.arc(kx, ty, 3, 0, 6.29)
            ctx.fill()
            hits.append((bx - 8, 14, tw + 16, 38, "slider", name))

    # -- LIVE ---------------------------------------------------------------
    def view_live(self, ctx, hits, t, pressed, scroll):
        with self.st.lock:
            agents = list(self.st.agents)
            clients = list(self.st.clients)
        agent_windows = {a.get("window") for a in agents if a.get("window")}
        cards = []
        for a in agents:
            cards.append(("agent", a))
        for c in clients:  # a session already shown as an agent card hides here
            if c.get("address") not in agent_windows:
                cards.append(("win", c))
        x0 = RAIL_W + 10 - scroll
        cw, gap = 330, 8
        if not cards:
            self.text(ctx, "no sessions — the bar is quiet", RAIL_W + 24, 37, 13,
                      self.th["dark_foreground"])
        for i, (kind, obj) in enumerate(cards):
            bx = x0 + i * (cw + gap)
            if bx + cw < RAIL_W or bx > W:
                continue
            self.rr(ctx, bx, 7, cw, 46, 10)
            ctx.set_source_rgba(*self.th["lighter_background"], 0.5)
            ctx.fill()
            self.rr(ctx, bx, 7, cw, 46, 10)
            ctx.set_source_rgba(*self.th["muted"], 0.7)
            ctx.set_line_width(1)
            ctx.stroke()
            _key = ("win", obj.get("address")) if kind == "win" else \
                ("agent", obj.get("node"), obj.get("kind"),
                 obj.get("pid"), obj.get("tmux"))
            if getattr(self, "live_sel", None) == _key:
                # two-tap selection: first tap arms, second tap opens;
                # the outline fades as the 6 s arm window runs out
                age = t - getattr(self, "live_sel_at", t)
                alpha = 0.95 - 0.65 * min(max(age, 0), 6) / 6.0
                self.rr(ctx, bx, 7, cw, 46, 10)
                ctx.set_source_rgba(*self.th["accent"], alpha)
                ctx.set_line_width(2)
                ctx.stroke()
                self.text(ctx, "TAP AGAIN TO OPEN", bx + cw - 122, 20, 9,
                          self.th["accent"], cairo.FONT_WEIGHT_BOLD)
            if kind == "agent":
                col = self.th["green"] if obj["active"] else self.th["dark_foreground"]
                if obj["active"]:  # pulse
                    pulse = 0.55 + 0.45 * math.sin(t * 4)
                    ctx.set_source_rgba(*col, pulse)
                else:
                    ctx.set_source_rgba(*col, 0.9)
                ctx.arc(bx + 16, 22, 4, 0, 6.29)
                ctx.fill()
                title = obj.get('detail', obj['kind']) if obj['kind'] != 'goal' \
                    else f"{obj['glyph']} {obj.get('detail', obj['kind'])}"
                self.text(ctx, title[:26], bx + 28, 27, 14, self.th["bright_foreground"],
                          cairo.FONT_WEIGHT_BOLD)
                parts = []
                if obj.get("model"):
                    parts.append(obj["model"])
                parts.append(fmt_elapsed(obj["elapsed"]))
                if obj["percent"] is not None:
                    parts.append(f"{int(obj['percent'])}%")
                self.text(ctx, "  ·  ".join(parts), bx + 28, 46, 12,
                          self.th["dark_foreground"])
                # progress track
                tx, tw = bx + 150, cw - 166
                self.rr(ctx, tx, 36, tw, 5, 2.5)
                ctx.set_source_rgba(*self.th["muted"], 0.6)
                ctx.fill()
                if obj["percent"] is not None:
                    self.rr(ctx, tx, 36, max(tw * clamp(obj["percent"] / 100.0), 5), 5, 2.5)
                    ctx.set_source_rgb(*self.th["accent"])
                    ctx.fill()
                elif obj["active"]:
                    wseg = tw * 0.3
                    off = (math.sin(t * 2.2) * 0.5 + 0.5) * (tw - wseg)
                    self.rr(ctx, tx + off, 36, wseg, 5, 2.5)
                    ctx.set_source_rgba(*self.th["accent"], 0.9)
                    ctx.fill()
                hits.append((bx, 7, cw, 46, "agent", obj))
            else:
                if obj.get("address") and obj["address"] == self.st.active_addr:
                    self.rr(ctx, bx, 7, cw, 46, 10)
                    ctx.set_source_rgba(*self.th["accent"], 0.85)
                    ctx.set_line_width(1.6)
                    ctx.stroke()
                ws = obj.get("workspace", {}).get("id", "?")
                self.rr(ctx, bx + 8, 15, 30, 30, 7)
                ctx.set_source_rgba(*self.th["accent"], 0.25)
                ctx.fill()
                self.text(ctx, str(ws), bx + 16, 36, 13, self.th["accent"], cairo.FONT_WEIGHT_BOLD)
                cls = (obj.get("class") or "app").replace("org.omarchy.", "").replace("google-", "")
                cls = {"foot": "terminal"}.get(cls, cls)
                self.text(ctx, cls[:24], bx + 46, 27, 14, self.th["bright_foreground"],
                          cairo.FONT_WEIGHT_BOLD)
                title = (obj.get("title") or "")[:30]
                self.text(ctx, title, bx + 46, 46, 12, self.th["dark_foreground"])
                hits.append((bx, 7, cw, 46, "win", obj.get("address", "")))
        max_scroll = max(0, len(cards) * (cw + gap) - (W - RAIL_W - 20))
        if max_scroll > 0 and scroll < max_scroll - 4:
            self.text(ctx, "›", W - 26, 40, 22, self.th["accent"], cairo.FONT_WEIGHT_BOLD)
        if scroll > 4:
            self.text(ctx, "‹", RAIL_W + 14, 40, 22, self.th["accent"], cairo.FONT_WEIGHT_BOLD)
        return max_scroll

    # -- DEV (hosts -> harnesses -> confirm -> launch) -----------------------
    @staticmethod
    def _node_name(node_sel):
        return "LOCAL" if node_sel is None else node_sel["name"]

    def view_dev(self, ctx, hits, t, scroll=0):
        step, node_sel, harness_sel = self.dev_state
        if step == "host":
            self._dev_hosts(ctx, hits)
            return 0
        if step == "harness":
            return self._dev_harnesses(ctx, hits, node_sel, scroll)
        self._dev_confirm(ctx, hits, node_sel, harness_sel)
        return 0

    def _dev_card(self, ctx, hits, bx, by, bw, bh, label, kind, data, sel=False, size=13):
        self.rr(ctx, bx, by, bw, bh, 9)
        if sel:
            ctx.set_source_rgba(*self.th["accent"], 0.30)
            ctx.fill()
            self.rr(ctx, bx, by, bw, bh, 9)
            ctx.set_source_rgba(*self.th["accent"], 0.9)
            ctx.set_line_width(1.4)
            ctx.stroke()
        else:
            ctx.set_source_rgba(*self.th["lighter_background"], 0.55)
            ctx.fill()
            self.rr(ctx, bx, by, bw, bh, 9)
            ctx.set_source_rgba(*self.th["muted"], 0.6)
            ctx.set_line_width(1)
            ctx.stroke()
        ctx.select_font_face(self.th.font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(size)
        ext = ctx.text_extents(label)
        self.text(ctx, label, bx + (bw - ext.width) / 2, by + bh / 2 + size * 0.36, size,
                  self.th["bright_foreground"], cairo.FONT_WEIGHT_BOLD)
        hits.append((bx, by, bw, bh, kind, data))

    def _dev_hosts(self, ctx, hits):
        with self.st.lock:
            nodes = list(self.st.nodes_detail)
        entries = [{"name": "LOCAL", "host": None, "online": True}] + nodes
        self.text(ctx, "CHOOSE A HOST", RAIL_W + 16, 17, 11,
                  self.th["dark_foreground"], cairo.FONT_WEIGHT_BOLD)
        n = len(entries)
        avail = W - (RAIL_W + 16) - 12
        cw = min(320, (avail - (n - 1) * 8) / max(n, 1))
        for i, e in enumerate(entries[:8]):
            bx = RAIL_W + 16 + i * (cw + 8)
            if e.get("online", True):
                self._dev_card(ctx, hits, bx, 22, cw, 32,
                               e["name"][:18], "node", e, size=13)
            else:  # offline: shown, but never a launch target
                self.rr(ctx, bx, 22, cw, 32, 9)
                ctx.set_source_rgba(*self.th["lighter_background"], 0.22)
                ctx.fill()
                self.text(ctx, e["name"][:14] + " (off)", bx + 12, 43, 12,
                          self.th["dark_foreground"])

    def _dev_harnesses(self, ctx, hits, node_sel, scroll):
        name = self._node_name(node_sel)
        # fixed back pill + heading; harness cards scroll beneath
        self._dev_card(ctx, hits, RAIL_W + 16, 10, 196, 40, f"< {name}", "devback", None, size=14)
        self.text(ctx, "RUN WHAT?", RAIL_W + 228, 17, 11,
                  self.th["dark_foreground"], cairo.FONT_WEIGHT_BOLD)
        x0 = RAIL_W + 228 - scroll
        cw, gap = 176, 8
        for i, (hid, label) in enumerate(HARNESSES):
            bx = x0 + i * (cw + gap)
            if bx + cw < RAIL_W + 220 or bx > W:
                continue
            self._dev_card(ctx, hits, bx, 22, cw, 32, label, "harness", hid, size=13)
        total = len(HARNESSES) * (cw + gap)
        max_scroll = max(0, int(total - (W - (RAIL_W + 228))))
        if max_scroll > 0 and scroll < max_scroll - 4:
            self.text(ctx, "›", W - 26, 40, 22, self.th["accent"], cairo.FONT_WEIGHT_BOLD)
        if scroll > 4:
            self.text(ctx, "‹", RAIL_W + 222, 40, 22, self.th["accent"], cairo.FONT_WEIGHT_BOLD)
        return max_scroll

    def _dev_confirm(self, ctx, hits, node_sel, harness_sel):
        name = self._node_name(node_sel)
        host = None if node_sel is None else node_sel["host"]
        label = dict(HARNESSES).get(harness_sel, "ssh only")
        if harness_sel in (None, "ssh"):
            summary = f"ssh @ {name}" if host else "terminal @ LOCAL"
            cmd = f"ssh {host}" if host else "local shell"
        else:
            summary = f"{label} @ {name}"
            base = LOCAL_PI_CMD if (host is None and harness_sel == "pi") else \
                HARNESS_CMD.get(harness_sel, "grokbot desktop app")
            cmd = base if host is None else f"ssh {host} \u00b7 {base}"
        cw, chh = 760, 46
        bx = RAIL_W + (W - RAIL_W - cw) // 2 - 130
        self.rr(ctx, bx, 7, cw, chh, 11)
        ctx.set_source_rgba(*self.th["lighter_background"], 0.6)
        ctx.fill()
        self.rr(ctx, bx, 7, cw, chh, 11)
        ctx.set_source_rgba(*self.th["muted"], 0.7)
        ctx.set_line_width(1)
        ctx.stroke()
        self.text(ctx, summary, bx + 20, 32, 18, self.th["bright_foreground"],
                  cairo.FONT_WEIGHT_BOLD)
        ctx.select_font_face(self.th.font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(18)
        sw = ctx.text_extents(summary).width
        self.text(ctx, cmd[:52], bx + 34 + sw, 32, 11, self.th["dark_foreground"])
        self.text(ctx, "yolo mode \u00b7 tap LAUNCH to open in a terminal", bx + 20, 47, 10,
                  self.th["dark_foreground"])
        # buttons
        lx = bx + cw + 14
        self.rr(ctx, lx, 7, 210, 46, 11)
        ctx.set_source_rgba(*self.th["accent"], 0.9)
        ctx.fill()
        self.text(ctx, "LAUNCH >", lx + 52, 38, 16, self.th["darker_background"],
                  cairo.FONT_WEIGHT_BOLD)
        hits.append((lx, 7, 210, 46, "launch", None))
        self._dev_card(ctx, hits, lx + 222, 7, 120, 46, "< BACK", "devback", None, size=13)

    def view_fleet_detail(self, ctx, hits, t, which):
        """Second-level drill-downs for the four fleet categories."""
        with self.st.lock:
            nodes = list(self.st.nodes_detail)
            sess = {k: list(v) for k, v in self.st.sessions_detail.items()}
        x0 = RAIL_W + 16
        if which == "fleet-nodes":
            self.text(ctx, "NODES \u00b7 tap one to open DEV on it", x0, 17, 11,
                      self.th["dark_foreground"], cairo.FONT_WEIGHT_BOLD)
            entries = [{"name": "LOCAL", "host": None, "online": True, "os": "this mac", "ip": ""}] + nodes
            n = len(entries)
            cw = min(238, (W - x0 - 12 - (n - 1) * 8) / max(n, 1))
            for i, e in enumerate(entries[:8]):
                bx = x0 + i * (cw + 8)
                self.rr(ctx, bx, 22, cw, 32, 8)
                ctx.set_source_rgba(*self.th["lighter_background"], 0.55)
                ctx.fill()
                dot = self.th["bright_green"] if e.get("online") else self.th["dark_foreground"]
                ctx.set_source_rgb(*dot)
                ctx.arc(bx + 14, 38, 4, 0, 6.29)
                ctx.fill()
                self.text(ctx, e["name"][:16], bx + 26, 36, 13, self.th["bright_foreground"],
                          cairo.FONT_WEIGHT_BOLD)
                sub = " \u00b7 ".join(s for s in [e.get("os", ""), e.get("ip", "")] if s) or (e["host"] or "")
                self.text(ctx, sub[:26], bx + 26, 49, 10, self.th["dark_foreground"])
                if e.get("online"):
                    hits.append((bx, 22, cw, 32, "node", e))
        elif which == "fleet-sessions":
            self.text(ctx, "SESSIONS RUN TODAY", x0, 17, 11,
                      self.th["dark_foreground"], cairo.FONT_WEIGHT_BOLD)
            flat = [(lbl, tm) for rows in sess.values() for (lbl, tm) in rows]
            if not flat:
                self.text(ctx, "no harness sessions yet today", x0, 40, 13,
                          self.th["dark_foreground"])
            cw, gap = 330, 8
            for i, (lbl, tm) in enumerate(flat[:5]):
                bx = x0 + i * (cw + gap)
                self.rr(ctx, bx, 22, cw, 32, 8)
                ctx.set_source_rgba(*self.th["lighter_background"], 0.55)
                ctx.fill()
                self.text(ctx, lbl[:24], bx + 14, 43, 12, self.th["bright_foreground"],
                          cairo.FONT_WEIGHT_BOLD)
                ctx.select_font_face(self.th.font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                ctx.set_font_size(12)
                ext = ctx.text_extents(tm)
                self.text(ctx, tm, bx + cw - ext.width - 14, 43, 12, self.th["accent"],
                          cairo.FONT_WEIGHT_BOLD)
        else:  # fleet-system
            batt = f"{self.st.battery}%" + (" charging" if self.st.charging else "")
            blocks = [
                ("CPU", f"{int(self.st.cpu * 100)}%", f"load {self.st.load1:.2f}"),
                ("MEMORY", f"{int(self.st.mem * 100)}%",
                 f"{self.st.mem_used_gb:.1f} / {self.st.mem_total_gb:.0f} GB"),
                ("DISK /", f"{self.st.disk_pct}%", "used"),
                ("BATTERY", batt, "macsmc"),
                ("UPTIME", fmt_elapsed(self.st.uptime_sec), "this Mac"),
                ("TAILSCALE", self.st.net_ip or "\u2014", "local"),
            ]
            bw = (W - x0 - 12) / len(blocks)
            for i, (label, value, sub) in enumerate(blocks):
                bx = x0 + i * bw
                if i:
                    ctx.set_source_rgba(*self.th["muted"], 0.45)
                    ctx.rectangle(bx - 10, 10, 1, H - 20)
                    ctx.fill()
                self.text(ctx, label, bx, 21, 11, self.th["dark_foreground"], cairo.FONT_WEIGHT_BOLD)
                self.text(ctx, value[:14], bx, 43, 16, self.th["bright_foreground"],
                          cairo.FONT_WEIGHT_BOLD)
                ctx.select_font_face(self.th.font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                ctx.set_font_size(16)
                vw = ctx.text_extents(value[:14]).width
                self.text(ctx, sub[:20], bx + vw + 10, 43, 11, self.th["dark_foreground"])
        hits.append((RAIL_W, 0, W - RAIL_W, H, "view", "fleet"))  # tap anywhere else: back

    # -- FLEET (drill-down from the FLOW status chips) -----------------------
    def view_fleet(self, ctx, hits, t):
        # the back catch-all is appended at the END: registered first, it
        # shadowed every category block (drill-downs were unreachable)
        with self.st.lock:
            nodes = list(self.st.nodes)
            today = dict(self.st.sessions_today)
            agents = list(self.st.agents)
        total_today = sum(today.values())
        per = "  ".join(f"{k} {v}" for k, v in sorted(today.items())) or "none yet"
        running = "  ·  ".join(
            f"{a.get('detail', a['kind'])} {fmt_elapsed(a['elapsed'])}" for a in agents[:3]) or "idle"
        blocks = [
            ("NODES UP >", f"{len(nodes)}", ", ".join(nodes)[:44] or "—", "fleet-nodes"),
            ("SESSIONS TODAY >", f"{total_today}", per[:44], "fleet-sessions"),
            ("RUNNING NOW >", f"{len(agents)}", running[:44], "live"),
            ("SYSTEM >", f"{self.st.load1:.1f}",
             f"mem {int(self.st.mem * 100)}%  ·  up {fmt_elapsed(self.st.uptime_sec)}  ·  batt {self.st.battery}%",
             "fleet-system"),
        ]
        x = RAIL_W + 18
        bw = (W - x - 14) / len(blocks)
        for i, (label, value, sub, target) in enumerate(blocks):
            bx = x + i * bw
            if i:
                ctx.set_source_rgba(*self.th["muted"], 0.45)
                ctx.rectangle(bx - 12, 10, 1, H - 20)
                ctx.fill()
            hits.append((bx - 10, 6, bw - 4, 48, "view", target))
            self.text(ctx, label, bx, 21, 11, self.th["dark_foreground"], cairo.FONT_WEIGHT_BOLD)
            self.text(ctx, value, bx, 44, 19, self.th["bright_foreground"], cairo.FONT_WEIGHT_BOLD)
            ctx.select_font_face(self.th.font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(19)
            vw = ctx.text_extents(value).width
            self.text(ctx, sub, bx + vw + 12, 44, 12, self.th["dark_foreground"])
        self.text(ctx, "‹", W - 30, 40, 20, self.th["accent"], cairo.FONT_WEIGHT_BOLD)
        hits.append((RAIL_W, 0, W - RAIL_W, H, "view", "flow"))  # back (lowest priority)

    def add_flash(self, color_key, label, window=None, pid=None,
                  node=None, harness=None):
        self.flashes.append((color_key, label, time.monotonic()))
        self.flashes = self.flashes[-3:]
        # the latest event also becomes a tappable notification (12 s):
        # tap it and you land in the session that notified you
        self.notification = {"label": label, "color": color_key,
                             "window": window, "pid": pid,
                             "node": node, "harness": harness,
                             "t0": time.monotonic()}

    def _notif_active(self, t):
        return self.notification is not None and (t - self.notification["t0"]) < 12

    def _notify_pill(self, ctx, hits, t):
        n = self.notification
        label = f"{n['label']} \u2014 tap to jump"
        ctx.select_font_face(self.th.font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(15)
        ext = ctx.text_extents(label)
        w = ext.width + 58
        x = (RAIL_W + W - 84) / 2 - w / 2
        col = self.th[n["color"]]
        self.rr(ctx, x, 10, w, 40, 11)
        ctx.set_source_rgba(*self.th["darker_background"], 0.92)
        ctx.fill()
        self.rr(ctx, x, 10, w, 40, 11)
        ctx.set_source_rgba(*col, 0.85)
        ctx.set_line_width(1.4)
        ctx.stroke()
        pulse = 0.6 + 0.4 * math.sin(t * 5)
        ctx.set_source_rgba(*col, pulse)
        ctx.arc(x + 20, 30, 5, 0, 6.29)
        ctx.fill()
        self.text(ctx, label, x + 34, 38, 15, self.th["bright_foreground"], cairo.FONT_WEIGHT_BOLD)
        # the pill floats ABOVE other views: remember its rect so the
        # daemon can give it first claim on any tap (see Daemon._hit_at)
        self._notif_rect = (x, 10, w, 40)
        hits.append((x, 10, w, 40, "notify", n))

    def _draw_flashes(self, ctx, t):
        """Completion events sweep a colored wave across the ASCII field."""
        alive = []
        for (key, label, t0) in self.flashes:
            age = t - t0
            if age > 1.8:
                continue
            alive.append((key, label, t0))
            col = self.th[key]
            p = age / 1.8
            env = math.sin(p * math.pi)
            ctx.set_source_rgba(*col, 0.10 * env)  # whole-field tint pulse
            ctx.rectangle(0, 0, W, H)
            ctx.fill()
            sweep_x = -180 + (W + 360) * p  # traveling wavefront
            grad = cairo.LinearGradient(sweep_x - 200, 0, sweep_x + 80, 0)
            grad.add_color_stop_rgba(0, *col, 0)
            grad.add_color_stop_rgba(0.75, *col, 0.34 * env)
            grad.add_color_stop_rgba(1, *col, 0)
            ctx.set_source(grad)
            ctx.rectangle(sweep_x - 200, 0, 280, H)
            ctx.fill()
            lx = min(max(sweep_x - 190, RAIL_W + 14), W - 420)  # event label rides the front
            ctx.set_source_rgba(*col, env)
            ctx.arc(lx + 5, 17, 4, 0, 6.29)
            ctx.fill()
            self.text(ctx, label[:44], lx + 16, 22, 13, col, cairo.FONT_WEIGHT_BOLD)
        self.flashes = alive

    # -- compose -------------------------------------------------------------
    def render(self, surface, view, t, pressed, scroll=0, fb_size=None):
        ctx = cairo.Context(surface)
        if fb_size:  # drawing straight onto the portrait panel: rotate into logical space
            if ROTATE_CW:
                ctx.translate(fb_size[0], 0)
                ctx.rotate(math.pi / 2)
            else:
                ctx.translate(0, fb_size[1])
                ctx.rotate(-math.pi / 2)
        self.bg(ctx)
        hits = []
        max_scroll = 0
        if view != "flow":
            # the ASCII field lives underneath every view, dimmed for legibility
            self.view_flow(ctx, [], t, {}, background=True)
            ctx.set_source_rgba(*self.th["darker_background"], 0.55)
            ctx.rectangle(0, 0, W, H)
            ctx.fill()
        if view == "flow":
            self.view_flow(ctx, hits, t, pressed)
        elif view == "dev":
            ctx.save()
            ctx.translate(self.slide_dx, 0)
            _n0 = len(hits)
            max_scroll = self.view_dev(ctx, hits, t, scroll) or 0
            ctx.restore()
            if self.slide_dx:  # hit rects must follow the slid drawing
                for _i in range(_n0, len(hits)):
                    _x, _y, _w, _h, _k, _d = hits[_i]
                    hits[_i] = (_x + self.slide_dx, _y, _w, _h, _k, _d)
        elif view == "keys":
            self.view_keys(ctx, hits, t, pressed)
        elif view == "ctrl":
            self.view_ctrl(ctx, hits, t, pressed)
        elif view == "live":
            max_scroll = self.view_live(ctx, hits, t, pressed, scroll) or 0
        elif view == "fleet":
            self.view_fleet(ctx, hits, t)
        elif view in ("fleet-nodes", "fleet-sessions", "fleet-system"):
            self.view_fleet_detail(ctx, hits, t, view)
        if view != "flow" and self._notif_active(t):
            self._notify_pill(ctx, hits, t)
        self._draw_flashes(ctx, t)
        self.rail(ctx, view, hits, t, pressed)
        surface.flush()
        return hits, max_scroll


# ---------------------------------------------------------------- daemon

class Daemon:
    def __init__(self, display, snapshot_only=False):
        self.logical = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
        self.th = Theme()
        self.st = State()
        self.display = display
        self.uinput = None if snapshot_only else UInput()
        self.act = Actions(self.st, self.uinput)
        self.renderer = Renderer(self.th, self.st)
        self.view = "flow"
        self.live_sel = None     # LIVE two-tap: armed selection key
        self.live_sel_at = 0.0   # ... and when it was armed (6 s expiry)
        self.dev_node = None     # DEV tab: selected node dict (None = LOCAL)
        self.dev_harness = None  # DEV tab: selected harness id (None until picked)
        self.dev_step = "host"   # DEV tab: host -> harness -> confirm
        self._slide_t0 = 0.0
        self.pod_open_until = 0.0  # FLOW quiet folder auto-collapse deadline
        self.view_before_fn = "flow"
        self.fn_held = False
        self.pressed = {}
        self.hits = []
        self.scroll = 0.0
        self.max_scroll = 0
        self.touch = None  # (x, y, start_time, hit, moved)
        self.last_touch = time.monotonic()
        self.running = True
        self.frame = 0
        self._stat_frames = 0
        self._stat_render = self._stat_blit = self._stat_commit = 0.0
        self._next_frame = time.monotonic()
        # panel upload runs on its own thread (latest frame wins) so a slow
        # DSI upload can never jitter the render loop
        self._upload_slot = None
        self._upload_event = threading.Event()
        self._upload_lock = threading.Lock()
        self._up_count = 0
        self._up_ms = 0.0

    # -- input ------------------------------------------------------------
    def input_loop(self):
        fd = os.open("/dev/input/event1", os.O_RDONLY)
        fds = [fd]
        # SPI keyboard (Fn summon + global activity) and trackpad (activity).
        # Like tiny-dfr, ANY system input counts as activity for dimming;
        # watching only the bar itself dimmed it while the user typed.
        for cand in ("/dev/input/event3", "/dev/input/event4"):
            try:
                kfd = os.open(cand, os.O_RDONLY | os.O_NONBLOCK)
                fds.append(kfd)
            except OSError:
                pass
        cur_x = cur_y = 0
        down = False
        while self.running:
            r, _, _ = select.select(fds, [], [], 0.25)
            for f in r:
                try:
                    data = os.read(f, 24 * 64)
                except OSError:
                    continue
                for off in range(0, len(data) - 23, 24):
                    _, _, typ, code, val = struct.unpack_from("qqHHi", data, off)
                    if f != fd:  # keyboard/trackpad: activity + Fn summon
                        self.last_touch = time.monotonic()
                        if typ == EV_KEY and code == KEY_FN:
                            if val == 1 and not self.fn_held:
                                self.fn_held = True
                                self.view_before_fn = self.view
                                self.view = "keys"
                            elif val == 0 and self.fn_held:
                                self.fn_held = False
                                self.view = self.view_before_fn
                        continue
                    if typ == EV_ABS and code in (ABS_X, ABS_MT_POSITION_X):
                        cur_x = int(val / TOUCH_MAX_X * W)
                        if FLIP_TOUCH_X:
                            cur_x = W - 1 - cur_x
                    elif typ == EV_ABS and code in (ABS_Y, ABS_MT_POSITION_Y):
                        cur_y = int(val / TOUCH_MAX_Y * H)
                    elif typ == EV_KEY and code == BTN_TOUCH:
                        if val == 1 and not down:
                            down = True
                            self._touch_down(cur_x, cur_y)
                        elif val == 0 and down:
                            down = False
                            self._touch_up(cur_x, cur_y)
                    elif typ == EV_SYN and down:
                        self._touch_move(cur_x, cur_y)

    def _hit_at(self, x, y):
        # the notification pill overlays every view: it gets first claim on
        # taps inside its rect, never the content underneath it
        r = self.renderer
        if r._notif_rect and r._notif_active(time.monotonic()):
            hx, hy, hw, hh = r._notif_rect
            if hx <= x <= hx + hw and hy <= y <= hy + hh:
                return ("notify", r.notification, r._notif_rect)
        # the left rail always wins over content: scrolled cards slide
        # underneath it, and a tab tap must never trigger the session
        # hidden behind the rail
        for (hx, hy, hw, hh, kind, data) in self.hits:
            if kind == "view" and data in VIEWS and hx < RAIL_W and \
                    hx <= x <= hx + hw and hy <= y <= hy + hh:
                return (kind, data, (hx, hy, hw, hh))
        generic = None
        for (hx, hy, hw, hh, kind, data) in self.hits:
            if hx <= x <= hx + hw and hy <= y <= hy + hh:
                generic = (kind, data, (hx, hy, hw, hh))
                break
        # While a notification is up, tapping the ASCII field around it
        # dismisses it. Controls keep their jobs; only background art
        # (no hit, or a full-field back catch-all) counts as the field.
        if r._notif_rect and r._notif_active(time.monotonic()) and x >= RAIL_W:
            if generic is None or (generic[0] == "view"
                                   and generic[1] in ("flow", "fleet")
                                   and generic[2][0] >= RAIL_W):
                return ("dismiss", r.notification, r._notif_rect)
        return generic

    def _touch_down(self, x, y):
        self.last_touch = time.monotonic()
        self.renderer.add_ripple(x, y)
        self.renderer.set_touch(x, y)
        hit = self._hit_at(x, y)
        self.touch = {"x0": x, "y0": y, "x": x, "hit": hit,
                      "t": time.monotonic(), "scroll0": self.scroll}
        if not hit and self.view == "flow":
            self.pod_open_until = 0.0  # tap the field: folder folds away
        if hit:
            kind, data = hit[0], hit[1]
            if kind == "slider":
                self.pod_open_until = time.monotonic() + 7
                self._slider_to(data, x, hit[2])
            elif kind == "view":
                self.pressed["rail:" + str(data)] = time.monotonic() + 0.25

    def _touch_move(self, x, y):
        if not self.touch:
            return
        self.last_touch = time.monotonic()
        self.renderer.set_touch(x, y)  # drag wake follows the finger
        if abs(x - getattr(self, "_last_ripple_x", -999)) > 90:
            self._last_ripple_x = x
            self.renderer.add_ripple(x, y)
        dx = x - self.touch["x0"]
        self.touch["x"] = x
        hit = self.touch["hit"]
        if hit and hit[0] == "slider":
            self._slider_to(hit[1], x, hit[2])
        elif (self.view == "live" or (self.view == "dev" and self.dev_step == "harness")) \
                and abs(dx) > 6:
            self.scroll = clamp(self.touch["scroll0"] - dx, 0, max(self.max_scroll, 0))

    def _touch_up(self, x, y):
        if not self.touch:
            return
        self.last_touch = time.monotonic()
        self.renderer.add_ripple(x, y)
        self.renderer.set_touch(x, y, active=False)
        hit = self.touch["hit"]
        x0 = self.touch["x0"]
        self.touch = None
        if not hit:
            return
        # Activate when the finger lifts on the SAME target it pressed
        # (or barely drifted). The old "14px = scroll" rule swallowed most
        # real taps on this strip, which read as "touch doesn't work".
        up_hit = self._hit_at(x, y)
        if up_hit and up_hit[0] == hit[0] and up_hit[1] == hit[1]:
            pass  # confirmed tap
        elif abs(x - x0) <= 14:
            pass  # tiny drift, no other target under the finger
        else:
            return  # genuine drag/scroll
        kind, data = hit[0], hit[1]
        if kind == "dismiss":
            if abs(x - x0) <= 14:  # a tap, not a scroll across the field
                self.renderer.notification = None
                self.renderer._notif_rect = None
            return
        if kind == "voice":
            self.act.toggle_voice(self.st.voice_live)
        elif kind == "notify":
            if data.get("node"):  # another machine: ssh in and resume it
                self.act.launch_remote_resume(data["node"], data["node"],
                                              data.get("harness"))
            elif data.get("window"):
                self.act.focus_client(data["window"])
            elif data.get("pid"):
                self.act.focus_agent(data["pid"])
            self.renderer.notification = None
            self.renderer._notif_rect = None
            self.view = "flow"
        elif kind == "podopen":
            self.pod_open_until = time.monotonic() + 7
        elif kind == "podclose":
            self.pressed["podclose"] = time.monotonic() + 0.18
            self.pod_open_until = 0.0
        elif kind == "view":
            self.view = data
            self.live_sel = None
            self.scroll = 0
        elif kind == "key":
            name, code = data
            self.pressed[name] = time.monotonic() + 0.18
            self.act.key(code)
        elif kind == "media":
            name, code = data
            self.pod_open_until = time.monotonic() + 7
            self.pressed[name] = time.monotonic() + 0.18
            if name == "mute":
                self.act.toggle_mute()
            elif name == "voice":
                self.act.toggle_voice(self.st.voice_live)
            elif code:
                self.act.key(code)
        elif kind == "win" and data:
            # two-tap: first tap arms the card, second tap on the
            # SAME card opens it (a stray touch never yanks focus)
            key = ("win", data)
            if self.live_sel != key:
                self.live_sel, self.live_sel_at = key, time.monotonic()
                self.pressed["win"] = time.monotonic() + 0.2
                return
            self.live_sel = None
            self.act.focus_client(data)
            self.pressed["win"] = time.monotonic() + 0.2
        elif kind == "agent" and (data.get("pid") or data.get("remote")):
            key = ("agent", data.get("node"), data.get("kind"),
                   data.get("pid"), data.get("tmux"))
            if self.live_sel != key:
                self.live_sel, self.live_sel_at = key, time.monotonic()
                self.pressed["agent"] = time.monotonic() + 0.2
                return
            self.live_sel = None
            if data.get("remote"):  # on another node: ssh in, attach/resume
                self.act.launch_remote_resume(data["node"], data["node_name"],
                                              data["kind"], data.get("tmux"))
            elif data.get("window"):
                self.act.focus_client(data["window"])
            else:
                self.act.focus_agent(data["pid"])
            self.pressed["agent"] = time.monotonic() + 0.2
        elif kind == "node":
            self.dev_node = None if data.get("host") is None else data
            self._set_dev_step("harness")
            self.view = "dev"  # arriving from fleet-nodes drill-down
        elif kind == "harness":
            self.dev_harness = data
            self._set_dev_step("confirm")
        elif kind == "devback":
            self._set_dev_step("host" if self.dev_step == "harness" else "harness")
        elif kind == "launch":
            node = self.dev_node
            self.act.launch_dev((node.get("ssh") or node["host"]) if node else None,
                                node["name"] if node else "LOCAL",
                                self.dev_harness)
            self.renderer.add_flash("accent",
                                    f"launched {self.dev_harness or 'ssh'} @ "
                                    f"{node['name'] if node else 'LOCAL'}")
            self.dev_harness = None
            self._set_dev_step("host")
            self.view = "flow"

    def _set_dev_step(self, step):
        self.dev_step = step
        self._slide_t0 = time.monotonic()
        self.scroll = 0.0

    def _slider_to(self, name, x, rect):
        hx, _, hw, _ = rect
        frac = clamp((x - (hx + 8)) / max(hw - 16, 1))
        if name == "bri":
            self.act.set_brightness(frac)
        elif name == "vol":
            self.act.set_volume(frac)
        elif name == "kbd":
            self.act.set_kbd(frac)
        elif name == "tb":
            self.act.set_tb(frac)

    # -- touchbar backlight --------------------------------------------------
    def set_tb_backlight(self, val):
        # val is 0..255; go through brightnessctl (logind) like the sliders
        subprocess.Popen(["brightnessctl", "-d", "228600000.dsi.0", "set",
                          f"{int(val * 100 / 255)}%"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _uploader(self):
        while self.running:
            self._upload_event.wait(0.5)
            self._upload_event.clear()
            with self._upload_lock:
                slot, self._upload_slot = self._upload_slot, None
            if slot is not None:
                try:
                    t_u0 = time.monotonic()
                    self.display.upload_bytes(slot, self.display.offscreen.get_stride())
                    self._up_count += 1
                    self._up_ms += (time.monotonic() - t_u0) * 1000
                except Exception as exc:  # never fail silently again
                    if not getattr(self, "_upload_err_logged", False):
                        print(f"touchbar upload failed: {exc}", file=sys.stderr, flush=True)
                        self._upload_err_logged = True

    # -- main loop ---------------------------------------------------------
    def run(self):
        threading.Thread(target=self.st.collect_loop, daemon=True).start()
        threading.Thread(target=self.input_loop, daemon=True).start()
        threading.Thread(target=self._uploader, daemon=True).start()
        os.makedirs(STATE_DIR, exist_ok=True)
        last_bl = None
        while self.running:
            # lockstep with the panel: don't render frame N+1 until frame N
            # has been uploaded. The DSI upload (~30ms) is the real frame
            # clock; racing ahead of it only produced judder.
            for _ in range(60):
                with self._upload_lock:
                    if self._upload_slot is None:
                        break
                time.sleep(0.001)
            t0 = time.monotonic()
            if self.th.reload():  # theme changed: rebuild the glyph atlas
                self.renderer._build_atlas()
                self.renderer._bg_cache = None
            # auto-return to FLOW
            if self.view != "flow" and not self.fn_held and t0 - self.last_touch > 8:
                self.view = "flow"
            # backlight: off when screen sleeps; dim on long idle; otherwise
            # the user's TOUCHBAR slider level rules
            idle = t0 - self.last_touch
            manual = int(clamp(self.st.tb) * 255)
            want_bl = 0 if not self.st.dpms_on else \
                (max(12, manual // 3) if idle > 120 and self.view == "flow" else manual)
            if want_bl != last_bl:
                self.set_tb_backlight(want_bl)
                last_bl = want_bl
            if self.frame % 600 == 0 and self.th.refresh_font():
                self.renderer._build_atlas()  # `omarchy font set` re-skins us too
            # completion/start flashes from the collector's lifecycle diff
            with self.st.lock:
                pending_events, self.st.events = self.st.events, []
            for (kind_ev, label, window, pid, node, harness) in pending_events:
                if kind_ev == "agent_start":
                    continue  # starts are ambient (see LIVE); only finishes notify
                # only real flashes are logged as flash: (acceptance greps it)
                print(f"flash: {kind_ev} {label} (node={node} harness={harness})",
                      file=sys.stderr, flush=True)
                color, suffix = {
                    "agent_done": ("bright_green", "done"),
                    "goal_done": ("bright_green", "complete"),
                    "goal_failed": ("red", "failed"),
                    "agent_start": ("bright_cyan", "started"),
                }.get(kind_ev, ("accent", ""))
                self.renderer.add_flash(color, f"{label} {suffix}".strip(),
                                        window=window, pid=pid or None,
                                        node=node, harness=harness)
            self.renderer.pod_open = t0 < self.pod_open_until
            if self.live_sel and t0 - self.live_sel_at > 6:
                self.live_sel = None  # armed selection expires
            self.renderer.live_sel = self.live_sel
            self.renderer.live_sel_at = self.live_sel_at
            self.renderer.dev_state = (self.dev_step, self.dev_node, self.dev_harness)
            slide_age = t0 - self._slide_t0
            self.renderer.slide_dx = int(170 * max(0.0, 1 - slide_age / 0.22) ** 2) \
                if self.view == "dev" else 0
            if self.frame % 120 == 0:  # ~5 s: reap closed ssh terminals
                self.act.reap_closed()
            disp = self.display
            t_pre = time.monotonic()
            # render to the flat logical surface (fast unrotated blits), then
            # a single rotated blit onto the portrait offscreen buffer
            hits, max_scroll = self.renderer.render(
                self.logical, self.view, t0, self.pressed, int(self.scroll))
            self.hits = hits
            self.max_scroll = max_scroll
            if not self.st.dpms_on:  # black frame while main screen sleeps
                ctx = cairo.Context(self.logical)
                ctx.set_source_rgb(0, 0, 0)
                ctx.paint()
                self.logical.flush()
            if self.frame % 150 == 0:  # periodic live preview for verification
                # snapshot the surface (~1 ms) and encode PNG off-thread:
                # write_to_png on the render thread hitched a frame ~6 s
                try:
                    snap = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
                    sctx = cairo.Context(snap)
                    sctx.set_source_surface(self.logical, 0, 0)
                    sctx.paint()
                    snap.flush()
                    threading.Thread(
                        target=lambda s=snap: s.write_to_png(
                            os.path.join(STATE_DIR, "preview.png")),
                        daemon=True).start()
                except Exception:
                    pass
            t_render = time.monotonic()
            fctx = cairo.Context(disp.draw_surface)
            if ROTATE_CW:
                fctx.translate(disp.fb_w, 0)
                fctx.rotate(math.pi / 2)
            else:
                fctx.translate(0, disp.fb_h)
                fctx.rotate(-math.pi / 2)
            fctx.set_operator(cairo.OPERATOR_SOURCE)
            fctx.set_source_surface(self.logical, 0, 0)
            fctx.get_source().set_filter(cairo.FILTER_NEAREST)
            fctx.paint()
            disp.offscreen.flush()
            t_blit = time.monotonic()
            with self._upload_lock:  # hand off; upload thread does the slow push
                self._upload_slot = bytes(disp.offscreen.get_data())
            self._upload_event.set()
            t_commit = time.monotonic()
            # frame stats for tuning (rewritten every ~240 frames)
            if self._stat_frames == 0:
                self._stat_t0 = t0
            self._stat_frames += 1
            self._stat_render += (t_render - t_pre) * 1000
            self._stat_pre = getattr(self, "_stat_pre", 0.0) + (t_pre - t0) * 1000
            self._stat_blit += (t_blit - t_render) * 1000
            self._stat_commit += (t_commit - t_blit) * 1000
            if self._stat_frames >= 240:
                n = self._stat_frames
                wall = max(time.monotonic() - self._stat_t0, 0.001)
                try:
                    json.dump({"view": self.view, "fps": round(n / wall, 1),
                               "upload_fps": round(self._up_count / wall, 1),
                               "upload_ms": round(self._up_ms / max(self._up_count, 1), 2),
                               "pre_ms": round(self._stat_pre / n, 2),
                               "render_ms": round(self._stat_render / n, 2),
                               "blit_ms": round(self._stat_blit / n, 2),
                               "commit_ms": round(self._stat_commit / n, 2),
                               "energy": round(self.st.energy, 3)},
                              open(os.path.join(STATE_DIR, "stats.json"), "w"))
                except Exception:
                    pass
                self._stat_frames = 0
                self._stat_render = self._stat_blit = self._stat_commit = 0.0
                self._stat_pre = 0.0
                self._up_count = 0
                self._up_ms = 0.0
            self.frame += 1
            # deadline pacing: even frame intervals, no drift accumulation
            target = 1 / 20  # a steady 20 fps beats a jittery 24 under load
            self._next_frame += target
            delay = self._next_frame - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -target:  # fell a full frame behind: resync
                self._next_frame = time.monotonic()


# ---------------------------------------------------------------- snapshot

def snapshot(outdir):
    os.makedirs(outdir, exist_ok=True)
    th = Theme()
    st = State()
    st.cpu, st.mem, st.battery, st.charging = 0.62, 0.48, 86, False
    st.brightness, st.volume, st.kbd = 0.28, 0.70, 0.0
    st.energy = 0.85
    st.agents = [
        {"kind": "pi", "glyph": "π", "label": "work", "detail": "pi · work",
         "elapsed": 754, "active": True, "percent": None, "pid": 1},
        {"kind": "goal", "glyph": "◎", "label": "project build", "detail": "project build",
         "elapsed": 1860, "active": True, "percent": 42, "pid": 0},
        {"kind": "claude", "glyph": "✳", "label": "brain", "detail": "claude · brain",
         "elapsed": 322, "active": False, "percent": None, "pid": 2},
    ]
    st.nodes_detail = [{"name": "Node A", "host": "node-a.example.net", "online": True, "os": "linux", "ip": "100.64.0.2"},
                       {"name": "Node B", "host": "node-b.example.net", "online": True, "os": "linux", "ip": "100.64.0.3"},
                       {"name": "Node C", "host": "node-c.example.net", "online": False, "os": "macOS", "ip": "100.64.0.4"},
                       {"name": "Node D", "host": "node-d.example.net", "online": True, "os": "linux", "ip": "100.64.0.5"}]
    st.sessions_detail = {"pi": [("pi · work", "21:02"), ("pi · notes", "19:44")],
                          "claude": [("claude · notes", "20:15")]}
    st.disk_pct = 43
    st.net_ip = "100.64.0.1"
    st.mem_used_gb, st.mem_total_gb = 4.0, 8.0
    st.clients = [
        {"class": "org.omarchy.pi", "title": "π - work", "address": "0x1",
         "workspace": {"id": 2}},
        {"class": "google-chrome", "title": "Sign in - Google Accounts", "address": "0x2",
         "workspace": {"id": 2}},
        {"class": "org.omarchy.terminal", "title": "Omarchy", "address": "0x3",
         "workspace": {"id": 1}},
    ]
    r = Renderer(th, st)
    for view in VIEWS:
        s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
        if view == "flow":
            for k in range(40):  # settle the field
                r.render(s, view, 100 + k * 0.033, {})
        else:
            r.render(s, view, 100.0, {})
        s.write_to_png(os.path.join(outdir, f"{view}.png"))
        print("wrote", view)
    st.nodes = ["local", "node-a", "node-b"]
    st.sessions_today = {"pi": 4, "claude": 2, "codex": 1}
    st.uptime_sec = 7200
    st.load1 = 1.29
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    r.render(s, "fleet", 100.0, {})
    s.write_to_png(os.path.join(outdir, "fleet.png"))
    print("wrote fleet")
    for dv in ("fleet-nodes", "fleet-sessions", "fleet-system"):
        s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
        r.render(s, dv, 100.0, {})
        s.write_to_png(os.path.join(outdir, f"{dv}.png"))
    print("wrote fleet details")
    r.dev_state = ("harness", {"name": "Node A", "host": "node-a"}, None)
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    r.render(s, "dev", 100.0, {})
    s.write_to_png(os.path.join(outdir, "dev-harness.png"))
    r.dev_state = ("confirm", {"name": "Node A", "host": "node-a"}, "grok")
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    r.render(s, "dev", 100.0, {})
    s.write_to_png(os.path.join(outdir, "dev-confirm.png"))
    print("wrote dev steps")
    r.pod_open = True
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    r.render(s, "flow", 100.0, {})
    s.write_to_png(os.path.join(outdir, "flow-pod.png"))
    r.pod_open = False
    r.add_flash("bright_green", "pi · work done")
    r.flashes = [(k, lbl, 99.3) for (k, lbl, _) in r.flashes]
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    r.render(s, "flow", 100.0, {})
    s.write_to_png(os.path.join(outdir, "flow-flash.png"))
    print("wrote pod/flash variants")
    # idle flow variant
    st.energy = 0.08
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    r.render(s, "flow", 100.0, {})
    s.write_to_png(os.path.join(outdir, "flow-idle.png"))
    print("wrote flow-idle")


def testpattern(seconds=90):
    """Orientation + touch calibration. Draws labelled markers in logical
    space, logs every touch (raw + mapped) to touch-test.log. Judge from
    what the human sees/taps which rotation/flip/swap is correct."""
    logpath = os.path.join(STATE_DIR, "touch-test.log")
    os.makedirs(STATE_DIR, exist_ok=True)
    log = open(logpath, "a")
    log.write(f"\n=== testpattern {time.strftime('%H:%M:%S')} ROTATE_CW={ROTATE_CW} ===\n")
    log.flush()
    disp = DRMDisplay()
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    dots = []

    def draw():
        ctx = cairo.Context(surf)
        ctx.set_source_rgb(0.02, 0.02, 0.05)
        ctx.paint()
        # corner markers: TL red, TR green, BL blue, BR yellow
        for (x, y, col) in [(0, 0, (1, 0, 0)), (W - 60, 0, (0, 1, 0)),
                            (0, H - 26, (0, 0.3, 1)), (W - 60, H - 26, (1, 1, 0))]:
            ctx.set_source_rgb(*col)
            ctx.rectangle(x, y, 60, 26)
            ctx.fill()
        ctx.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_source_rgb(1, 1, 1)
        ctx.set_font_size(34)
        ctx.move_to(80, 42)
        ctx.show_text("L")
        ext = ctx.text_extents("R")
        ctx.move_to(W - 80 - ext.width, 42)
        ctx.show_text("R")
        ctx.set_font_size(20)
        ctx.move_to(W / 2 - 90, 24)
        ctx.show_text("TOP ^ 12345")
        ctx.set_font_size(15)
        ctx.move_to(W / 2 - 40, H - 8)
        ctx.show_text("BOTTOM")
        for (dx, dy) in dots[-24:]:
            ctx.set_source_rgb(1, 0, 1)
            ctx.arc(dx, dy, 9, 0, 6.29)
            ctx.fill()
        surf.flush()
        fctx = cairo.Context(disp.draw_surface)
        if ROTATE_CW:
            fctx.translate(disp.fb_w, 0)
            fctx.rotate(math.pi / 2)
        else:
            fctx.translate(0, disp.fb_h)
            fctx.rotate(-math.pi / 2)
        fctx.set_source_surface(surf, 0, 0)
        fctx.paint()
        disp.draw_surface.flush()
        disp.commit()

    draw()
    fd = os.open("/dev/input/event1", os.O_RDONLY | os.O_NONBLOCK)
    raw_x = raw_y = 0
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.25)
        if not r:
            continue
        try:
            data = os.read(fd, 24 * 64)
        except OSError:
            continue
        changed = False
        for off in range(0, len(data) - 23, 24):
            _, _, typ, code, val = struct.unpack_from("qqHHi", data, off)
            if typ == EV_ABS and code in (ABS_X, ABS_MT_POSITION_X):
                raw_x = val
            elif typ == EV_ABS and code in (ABS_Y, ABS_MT_POSITION_Y):
                raw_y = val
            elif typ == EV_KEY and code == BTN_TOUCH and val == 1:
                mx = int(raw_x / TOUCH_MAX_X * W)
                my = int(raw_y / TOUCH_MAX_Y * H)
                dots.append((mx, my))
                log.write(f"touch raw=({raw_x},{raw_y}) mapped=({mx},{my})\n")
                log.flush()
                changed = True
        if changed:
            draw()
    log.write("=== end ===\n")
    log.close()


def main():
    if "--testpattern" in sys.argv:
        secs = int(sys.argv[sys.argv.index("--testpattern") + 1]) \
            if len(sys.argv) > sys.argv.index("--testpattern") + 1 else 90
        testpattern(secs)
        return
    if "--snapshot" in sys.argv:
        snapshot(sys.argv[sys.argv.index("--snapshot") + 1])
        return
    disp = DRMDisplay()
    d = Daemon(disp)
    try:
        d.run()
    except KeyboardInterrupt:
        d.running = False


if __name__ == "__main__":
    main()
