#!/usr/bin/env python3
"""Print labels on a Brother QL-series printer without the vendor GUI.

Renders a label to a PIL image, converts it to Brother raster commands with
brother_ql (the `brother-ql-next` fork, which is the one that knows the
QL-1100 / QL-1110NWB family), and pushes the bytes down whichever transport
the printer happens to be on.

Transports:
    net://192.168.1.50[:9100]   TCP, the JetDirect-style raw port
    bt://COM5                   Bluetooth SPP over a paired outgoing COM port
    bt://00:11:22:33:44:55      Bluetooth SPP over a raw RFCOMM socket, no pairing
    usb://0x04f9:0x20c0         pyusb (needs a WinUSB/libusb driver on Windows)
    spool://Brother QL-1110NWB  a Windows print queue, raw job through the spooler
    file://out.bin              dry run -- write the raster to disk, print nothing
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from brother_ql.conversion import convert
from brother_ql.labels import ALL_LABELS, LabelsManager
from brother_ql.models import ALL_MODELS
from brother_ql.raster import BrotherQLRaster

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

DEFAULTS = {
    "model": "QL-1110NWB",
    "transport": "file://label.bin",
    # Raw port 9100 on this printer is print-only -- it never answers a status
    # request. Bluetooth SPP is bidirectional, so status can come from there even
    # when jobs go out over the network.
    "status_transport": None,
    "label": "62",
    "font": "C:/Windows/Fonts/arialbd.ttf",
    "dpi_600": False,
    "cut": True,
}


class StatusUnavailable(Exception):
    """The printer could not be asked what it has loaded."""


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
    apply_calibration(cfg.get("calibration", {}))
    return cfg


DOTS_PER_MM = 300 / 25.4
TEXT_SHIFT_Y: dict[str, int] = {}  # label id -> dots to move text down


def apply_calibration(cal: dict) -> None:
    """Widen and re-centre die-cut rolls from a measured calibration print.

    brother_ql's printable box for some rolls is narrower than the label and
    off-centre on the QL-1110 (23x23: 4 mm blank left, 1.5 mm right). Each
    entry in config.json's "calibration" holds the gaps measured around a
    full-printable-area box ("left_mm", "right_mm", "top_mm", "bottom_mm") and
    the blank edge wanted ("edge_mm"). Sideways, the printable box is grown /
    moved so both gaps become edge_mm. Along the feed the raster height is
    fixed, so text is instead nudged to sit centred on the label.
    """
    global ALL_LABELS
    labels = list(ALL_LABELS)
    for ident, c in cal.items():
        if ident.startswith("_"):
            continue
        i = next((n for n, lab in enumerate(labels) if lab.identifier == ident), None)
        if i is None:
            continue
        lab, edge = labels[i], c.get("edge_mm", 1.0)
        grow_l = round((c.get("left_mm", edge) - edge) * DOTS_PER_MM)
        grow_r = round((c.get("right_mm", edge) - edge) * DOTS_PER_MM)
        grow_r = min(grow_r, lab.offset_r)  # can't move past the head's edge
        w, h = lab.dots_printable
        labels[i] = attr.evolve(lab, dots_printable=(w + grow_l + grow_r, h),
                                offset_r=lab.offset_r - grow_r)
        TEXT_SHIFT_Y[ident] = round(
            (c.get("top_mm", 0) - c.get("bottom_mm", 0)) / 2 * DOTS_PER_MM) * -1
    ALL_LABELS = tuple(labels)
    LabelsManager.elements = list(labels)


import attr  # noqa: E402  (brother_ql's own dependency)


def new_raster(model_id: str, lab, force_length: bool = False) -> BrotherQLRaster:
    """A raster for this model, without brother_ql's length floor on die-cut rolls.

    brother_ql gives every QL-1100-series head a 301-dot (~25.5 mm) minimum.
    That holds for endless rolls (they are padded up to it) but not for die-cut
    ones: a DK-1221 23x23 (202 dots) printed fine in testing. Die-cut labels
    have a fixed size, so the floor is relaxed for them on a copy of the model.
    """
    qlr = BrotherQLRaster(model_id)
    if force_length or lab.dots_printable[1] != 0:
        qlr.model = attr.evolve(
            qlr.model, min_max_length_dots=(1, qlr.model.min_max_length_dots[1]))
    return qlr


def label_spec(identifier: str):
    for lab in ALL_LABELS:
        if lab.identifier == identifier:
            return lab
    raise SystemExit(f"unknown label {identifier!r}; see --list-labels")


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

LINE_SPACING = 1.18


def wrap_at_size(probe, paragraphs: list[str], font, max_w: int) -> list[str]:
    """Greedy word wrap of each paragraph at a fixed font size."""
    lines: list[str] = []
    for para in paragraphs:
        words = para.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            if probe.textbbox((0, 0), trial, font=font)[2] <= max_w:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def fit_text(path: str, text: str, box: tuple[int, int]):
    """Largest font size that fits, choosing line breaks to help it get there.

    Wrapping and sizing have to be solved together: a bigger font needs more
    lines, more lines need more height, and the tallest font that still fits is
    whichever one balances those. Fit is monotonic in font size, so binary search
    it, re-wrapping at each candidate. Explicit newlines are kept as hard breaks.
    """
    max_w, max_h = box
    paragraphs = text.split("\n")
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))

    best = None
    lo, hi = 6, 800
    while lo <= hi:
        mid = (lo + hi) // 2
        font = ImageFont.truetype(path, mid)
        lines = wrap_at_size(probe, paragraphs, font, max_w)
        widest = max((probe.textbbox((0, 0), ln, font=font)[2] for ln in lines),
                     default=0)
        ascent, descent = font.getmetrics()
        total_h = int((ascent + descent) * LINE_SPACING * len(lines))

        if widest <= max_w and total_h <= max_h:
            best = (font, lines)
            lo = mid + 1
        else:
            hi = mid - 1

    if best is None:  # even the floor size overflows; use it anyway
        font = ImageFont.truetype(path, 6)
        best = (font, wrap_at_size(probe, paragraphs, font, max_w))
    return best


QR_MIN_DOTS = 4   # smallest QR square, in dots (~0.34 mm at 300 dpi), we will print
QR_QUIET = 2      # quiet-zone squares around the code; phones manage with 2


def render_qr(text: str, side: int) -> Image.Image:
    """A QR code encoding text, as large as fits in a side x side square.

    Raises ValueError when the text needs so many squares that each would be
    under QR_MIN_DOTS -- an unscannable code is worse than none.
    """
    import segno
    if not text:
        raise ValueError("a QR code needs some text")
    if side // (21 + 2 * QR_QUIET) < QR_MIN_DOTS:   # 21 = the smallest QR
        raise ValueError(f"this label is too small for a readable QR code "
                         f"({side} dots)")
    qr = segno.make(text, error="m", micro=False)
    modules = qr.symbol_size(border=QR_QUIET)[0]
    scale = side // modules
    if scale < QR_MIN_DOTS:
        raise ValueError(f"text too long for a readable QR code on this label "
                         f"({modules} squares in {side} dots); shorten it or "
                         f"use a wider roll")
    rows = list(qr.matrix_iter(scale=scale, border=QR_QUIET))
    img = Image.new("L", (len(rows[0]), len(rows)), 255)
    img.putdata([0 if dark else 255 for row in rows for dark in row])
    return img


def render_text(text: str, lab, min_length: int, font_path: str,
                margin: int = 12, length_px: int | None = None,
                landscape: bool = False, qr: bool = False) -> Image.Image:
    """Render text onto a white label-sized canvas.

    Die-cut labels have a fixed printable box. Endless labels have a fixed
    width and whatever length we choose, so the caller can pass length_px.

    In landscape mode the canvas is built the way you hold the finished label --
    long edge horizontal -- and the caller rotates it before rasterising. That
    keeps the preview honest: what you see is the label in reading orientation.

    qr=True puts a QR code of the exact text at the left end, as tall as the
    label, and fits the words into what is left.
    """
    width, height = lab.dots_printable
    if height == 0:  # endless roll
        if length_px is None and qr and landscape:
            # Along the roll, leave room for a QR as tall as the roll is wide.
            length_px = width + width // 3
        height = max(length_px or width // 3, min_length)
    if landscape:
        width, height = height, width

    canvas = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(canvas)

    left = 0
    if qr:
        code = render_qr(text, min(height, width // 2))
        qy = (height - code.height) // 2
        if not landscape:  # same calibration nudge the text gets below
            qy = max(0, min(height - code.height, qy + TEXT_SHIFT_Y.get(lab.identifier, 0)))
        canvas.paste(code, (0, qy))
        left = code.width
        width -= left

    box = (width - 2 * margin, height - 2 * margin)
    font, lines = fit_text(font_path, text, box)

    ascent, descent = font.getmetrics()
    step = int((ascent + descent) * LINE_SPACING)
    y = (height - step * len(lines)) // 2
    if not landscape:  # centre on the label, not the printable box, if it fits
        y = max(0, min(height - step * len(lines), y + TEXT_SHIFT_Y.get(lab.identifier, 0)))

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        x = left + (width - (bbox[2] - bbox[0])) // 2 - bbox[0]
        draw.text((x, y), line, font=font, fill=0, anchor="la")
        y += step

    return canvas.convert("RGB")


def load_image(path: str, lab) -> Image.Image:
    img = Image.open(path)
    width, _ = lab.dots_printable
    if img.width != width:
        ratio = width / img.width
        img = img.resize((width, max(1, int(img.height * ratio))), Image.LANCZOS)
    return img.convert("RGB")


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

# 200 nulls clear any half-finished job, ESC @ resets, ESC i S asks for status.
STATUS_REQUEST = b"\x00" * 200 + b"\x1b\x40" + b"\x1b\x69\x53"

MEDIA_TYPES = {
    0x00: "no media",
    0x0A: "continuous length tape",
    0x0B: "die-cut labels",
    0x4A: "continuous length tape",
    0x4B: "die-cut labels",
}

ERROR_BITS_1 = {
    0x01: "no media", 0x02: "end of media", 0x04: "cutter jam",
    0x08: "weak batteries", 0x40: "printer in use", 0x80: "printer turned off",
}
ERROR_BITS_2 = {
    0x01: "replace media", 0x04: "expansion buffer full", 0x08: "communication error",
    0x20: "cover open", 0x40: "cancel key pressed", 0x80: "media cannot be fed",
}


def query_status(transport: str) -> bytes:
    """Ask the printer for its 32-byte status reply."""
    scheme, _, target = transport.partition("://")

    if scheme == "file":
        raise SystemExit("file:// is a dry-run transport and cannot report status")

    if scheme == "net":
        host, _, port = target.partition(":")
        try:
            with socket.create_connection((host, int(port or 9100)), timeout=15) as sock:
                sock.sendall(STATUS_REQUEST)
                sock.settimeout(10)
                buf = b""
                while len(buf) < 32:
                    chunk = sock.recv(32 - len(buf))
                    if not chunk:
                        break
                    buf += chunk
        except (socket.timeout, TimeoutError):
            raise StatusUnavailable(
                f"{target} accepted the connection but never answered a status "
                f"request; raw port 9100 on this printer is print-only. Set "
                f'"status_transport" in config.json to a bidirectional transport '
                f"(e.g. the bt:// address) to keep the media check working.")
        except OSError as exc:
            raise StatusUnavailable(f"cannot reach {target}: {exc}")
        if not buf:
            raise StatusUnavailable(f"{target} returned no status data")
        return buf

    if scheme == "bt":
        if re.fullmatch(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", target):
            helper = HERE / "btspp.exe"
            if not helper.exists():
                raise StatusUnavailable(f"{helper} missing; build it from btspp.cs")
            tmp = Path(tempfile.gettempdir()) / "labelprint-status.bin"
            tmp.unlink(missing_ok=True)
            proc = subprocess.run([str(helper), "status", target, str(tmp)],
                                  capture_output=True, text=True, timeout=90)
            if not tmp.exists():
                raise StatusUnavailable("status query failed:\n"
                                        + (proc.stdout + proc.stderr).strip())
            try:
                return tmp.read_bytes()
            finally:
                tmp.unlink(missing_ok=True)

        import serial
        try:
            with serial.Serial(target, baudrate=115200, timeout=15, write_timeout=30) as ser:
                ser.write(STATUS_REQUEST)
                ser.flush()
                return ser.read(32)
        except serial.SerialException as exc:
            raise StatusUnavailable(f"cannot reach {target}: {exc}")

    raise SystemExit(f"status is not supported over {scheme}://")


def decode_status(buf: bytes) -> dict:
    if len(buf) < 32:
        raise SystemExit(f"short status reply ({len(buf)} bytes); expected 32")

    errors = [name for bit, name in ERROR_BITS_1.items() if buf[8] & bit]
    errors += [name for bit, name in ERROR_BITS_2.items() if buf[9] & bit]

    return {
        "model_code": buf[4],
        "error_bytes": (buf[8], buf[9]),
        "errors": errors,
        "width_mm": buf[10],
        "media_code": buf[11],
        "media": MEDIA_TYPES.get(buf[11], f"unknown (0x{buf[11]:02X})"),
        "length_mm": buf[17],
        "raw": buf.hex(" "),
    }


def match_label(width_mm: int, length_mm: int, media_code: int) -> list[str]:
    """Map the printer's reported media onto brother_ql label identifiers.

    The printer reports physical dimensions, not a DK part number, so this can
    legitimately return more than one candidate.
    """
    die_cut = media_code in (0x0B, 0x4B)
    want = (width_mm, length_mm if die_cut else 0)
    return [lab.identifier for lab in ALL_LABELS if tuple(lab.tape_size) == want]


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------

def spool_raw(data: bytes, queue: str) -> str:
    """Hand the raster to the Windows spooler as a RAW job.

    This is the transport for a printer that is cabled up but off the network.
    Windows binds the QL to its own usbprint driver, so pyusb cannot claim the
    interface without swapping in WinUSB; the spooler already owns it, and a
    RAW job passes the bytes through untouched.
    """
    import ctypes
    from ctypes import wintypes

    if sys.platform != "win32":
        raise SystemExit("spool:// is a Windows-only transport")

    winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)

    class DOC_INFO_1(ctypes.Structure):
        _fields_ = [("pDocName", wintypes.LPWSTR),
                    ("pOutputFile", wintypes.LPWSTR),
                    ("pDatatype", wintypes.LPWSTR)]

    winspool.OpenPrinterW.argtypes = [wintypes.LPWSTR,
                                      ctypes.POINTER(wintypes.HANDLE),
                                      ctypes.c_void_p]
    winspool.StartDocPrinterW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(DOC_INFO_1)]
    winspool.StartPagePrinter.argtypes = [wintypes.HANDLE]
    winspool.WritePrinter.argtypes = [wintypes.HANDLE, ctypes.c_char_p,
                                      wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD)]
    winspool.EndPagePrinter.argtypes = [wintypes.HANDLE]
    winspool.EndDocPrinter.argtypes = [wintypes.HANDLE]
    winspool.ClosePrinter.argtypes = [wintypes.HANDLE]

    def fail(what: str) -> None:
        raise SystemExit(f"{what} failed on print queue {queue!r} "
                         f"(win32 error {ctypes.get_last_error()}). "
                         f"Check the name against: wmic printer get name")

    handle = wintypes.HANDLE()
    if not winspool.OpenPrinterW(queue, ctypes.byref(handle), None):
        fail("OpenPrinter")
    try:
        info = DOC_INFO_1("labelprint", None, "RAW")
        if not winspool.StartDocPrinterW(handle, 1, ctypes.byref(info)):
            fail("StartDocPrinter")
        try:
            if not winspool.StartPagePrinter(handle):
                fail("StartPagePrinter")
            written = wintypes.DWORD(0)
            if not winspool.WritePrinter(handle, data, len(data),
                                         ctypes.byref(written)):
                fail("WritePrinter")
            if written.value != len(data):
                raise SystemExit(f"short write to {queue!r}: "
                                 f"{written.value} of {len(data)} bytes")
            winspool.EndPagePrinter(handle)
        finally:
            winspool.EndDocPrinter(handle)
    finally:
        winspool.ClosePrinter(handle)

    return f"spooled {len(data)} bytes to {queue!r} (Windows RAW job)"


def send(data: bytes, transport: str) -> str:
    scheme, _, target = transport.partition("://")
    if not target:
        raise SystemExit(f"malformed transport {transport!r}")

    if scheme == "file":
        out = Path(target)
        if not out.is_absolute():
            out = HERE / out
        out.write_bytes(data)
        return f"wrote {len(data)} bytes to {out} (dry run, nothing printed)"

    if scheme == "net":
        host, _, port = target.partition(":")
        port = int(port or 9100)
        with socket.create_connection((host, port), timeout=15) as sock:
            sock.sendall(data)
        return f"sent {len(data)} bytes to {host}:{port}"

    if scheme == "bt":
        # A MAC goes over a raw RFCOMM socket via btspp.exe, which needs no
        # pairing at all. Anything else is treated as a paired COM port.
        if re.fullmatch(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", target):
            helper = HERE / "btspp.exe"
            if not helper.exists():
                raise SystemExit(f"{helper} missing; build it with:\n"
                                 f"  csc.exe /out:{helper} {HERE / 'btspp.cs'}")
            tmp = Path(tempfile.gettempdir()) / "labelprint-rfcomm.bin"
            tmp.write_bytes(data)
            try:
                proc = subprocess.run([str(helper), "send", target, str(tmp)],
                                      capture_output=True, text=True, timeout=120)
            finally:
                tmp.unlink(missing_ok=True)
            out = (proc.stdout + proc.stderr).strip()
            if proc.returncode != 0:
                raise SystemExit(f"RFCOMM send failed:\n{out}")
            return f"sent {len(data)} bytes to {target} (raw RFCOMM)\n{out}"

        import serial  # pyserial, only needed on the COM-port path
        with serial.Serial(target, baudrate=115200, timeout=15, write_timeout=30) as ser:
            ser.write(data)
            ser.flush()
        return f"sent {len(data)} bytes to {target} (Bluetooth SPP on {target})"

    if scheme == "spool":
        return spool_raw(data, target)

    if scheme == "usb":
        from brother_ql.backends.helpers import send as ql_send
        ql_send(instructions=data, printer_identifier=transport,
                backend_identifier="pyusb", blocking=True)
        return f"sent {len(data)} bytes over USB to {target}"

    raise SystemExit(f"unknown transport scheme {scheme!r}")


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    cfg = load_config()

    # Shared options live on a parent parser so they work on either side of
    # the subcommand -- `text "hi" --label 62` reads better than forcing every
    # flag ahead of the verb.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default=cfg["model"])
    common.add_argument("--label", default=cfg["label"],
                        help="DK size id, e.g. 62, 62x29, 102x152")
    common.add_argument("--transport", default=cfg["transport"])
    common.add_argument("--status-transport", default=cfg.get("status_transport"),
                        help="bidirectional transport used for status queries when "
                             "the printing transport cannot answer them")
    common.add_argument("--font", default=cfg["font"])
    common.add_argument("--margin", type=int, default=cfg.get("margin", 12),
                        help="blank dots kept between the text and the printable "
                             "edge (default from config.json, else 12)")
    common.add_argument("--length", type=int, default=None,
                        help="endless-roll label length in dots (300dpi: ~11.8 dots/mm)")
    common.add_argument("--copies", type=int, default=1)
    common.add_argument("--no-cut", action="store_true")
    common.add_argument("--rotate", default="auto", choices=["auto", "0", "90", "270"])
    orient = common.add_mutually_exclusive_group()
    orient.add_argument("--landscape", dest="landscape", action="store_true", default=None,
                        help="text along the long edge (default for die-cut rolls)")
    orient.add_argument("--portrait", dest="landscape", action="store_false",
                        help="text across the short edge")
    common.add_argument("--preview", metavar="PNG", help="also save the rendered bitmap here")
    common.add_argument("--qr", action="store_true",
                        help="add a QR code that encodes the label text")
    common.add_argument("--dry-run", action="store_true",
                        help="build the raster but write it to a file instead of "
                             "printing; the transport is still used for --label auto")
    common.add_argument("--no-verify", action="store_true",
                        help="skip the pre-flight media check (saves a round trip, "
                             "risks a rejected job and a wasted label)")
    common.add_argument("--force-length", action="store_true",
                        help="ignore the model's minimum label length (the 301-dot "
                             "floor comes from brother_ql, not from testing this printer)")

    p = argparse.ArgumentParser(description=__doc__, parents=[common],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list-labels", action="store_true")
    p.add_argument("--list-models", action="store_true")

    sub = p.add_subparsers(dest="cmd")
    t = sub.add_parser("text", parents=[common], help="print a text label")
    t.add_argument("text", help=r"label text; use \n for multiple lines")
    i = sub.add_parser("image", parents=[common], help="print an image file")
    i.add_argument("path")
    sub.add_parser("status", parents=[common],
                   help="ask the printer what roll is loaded and whether it is happy")
    return p


def main() -> int:
    args = build_parser().parse_args()

    if args.list_labels:
        for lab in ALL_LABELS:
            print(f"{lab.identifier:<10} {lab.name:<40} printable={lab.dots_printable}")
        return 0
    if args.list_models:
        print(" ".join(sorted(m.identifier for m in ALL_MODELS)))
        return 0
    if not args.cmd:
        build_parser().print_help()
        return 2

    if args.model not in {m.identifier for m in ALL_MODELS}:
        print(f"unknown model {args.model!r}; try --list-models", file=sys.stderr)
        return 2

    status_via = args.status_transport or args.transport

    if args.cmd == "status":
        try:
            st = decode_status(query_status(status_via))
        except StatusUnavailable as exc:
            print(f"status unavailable: {exc}", file=sys.stderr)
            return 1
        candidates = match_label(st["width_mm"], st["length_mm"], st["media_code"])
        print(f"media       : {st['width_mm']} mm {st['media']}"
              + (f", {st['length_mm']} mm long" if st["length_mm"] else ""))
        print(f"label id    : {' or '.join(candidates) if candidates else 'no match'}")
        print(f"errors      : {', '.join(st['errors']) if st['errors'] else 'none'}"
              f"  (0x{st['error_bytes'][0]:02X} 0x{st['error_bytes'][1]:02X})")
        print(f"model code  : 0x{st['model_code']:02X}")
        print(f"raw         : {st['raw']}")
        return 1 if st["errors"] else 0

    # --label auto asks the printer instead of trusting the config file.
    if args.label == "auto":
        try:
            st = decode_status(query_status(status_via))
        except StatusUnavailable as exc:
            raise SystemExit(f"--label auto needs to query the printer, but: {exc}")
        candidates = match_label(st["width_mm"], st["length_mm"], st["media_code"])
        if not candidates:
            raise SystemExit(f"printer reports {st['width_mm']} mm {st['media']}, "
                             f"which matches no known DK size")
        args.label = candidates[0]
        print(f"detected {st['width_mm']} mm {st['media']} -> DK {args.label}")

    lab = label_spec(args.label)
    model = [m for m in ALL_MODELS if m.identifier == args.model][0]
    min_len, max_len = model.min_max_length_dots

    # An address label like DK-1201 is 29x90 mm: the 90 mm runs along the roll,
    # so text should follow the long edge. Default to landscape whenever a
    # die-cut label is longer than it is wide.
    pw, ph = lab.dots_printable
    landscape = args.landscape
    if landscape is None:
        landscape = args.cmd == "text" and lab.form_factor == 1 and ph > pw

    if args.cmd == "text":
        text = args.text.replace("\\n", "\n")
        try:
            img = render_text(text, lab, min_len, args.font, margin=args.margin,
                              length_px=args.length, landscape=landscape, qr=args.qr)
        except ValueError as exc:
            raise SystemExit(f"refusing to print: {exc}")
    else:
        img = load_image(args.path, lab)
        landscape = False

    # Preview the label as it reads in the hand, before rotating for the head.
    if args.preview:
        img.save(args.preview)

    if landscape:
        img = img.rotate(-90, expand=True)

    # The 301-dot floor only binds endless rolls; die-cut rolls print at their
    # own size (see new_raster).
    endless = lab.dots_printable[1] == 0
    if endless and img.height < min_len and not args.force_length:
        print(f"DK {args.label} gives only {img.height} printable dots, but the "
              f"{args.model} needs at least {min_len} (~{min_len / 11.81:.0f} mm). "
              f"That roll is too short for this printer.", file=sys.stderr)
        return 2
    if img.height > max_len:
        print(f"label is {img.height} dots, over the {args.model} maximum of {max_len}",
              file=sys.stderr)
        return 2

    rotate = 0 if args.rotate == "auto" else int(args.rotate)

    qlr = new_raster(args.model, lab, args.force_length)
    qlr.exception_on_warning = True
    instructions = convert(
        qlr=qlr,
        images=[img] * max(1, args.copies),
        label=args.label,
        rotate=rotate,
        threshold=70.0,
        dither=False,
        compress=True,
        red=(args.label == "62red"),
        dpi_600=False,
        hq=True,
        cut=not args.no_cut,
    )

    print(f"{len(instructions)} bytes of raster for {args.model} / DK {args.label} "
          f"({img.width}x{img.height} px)")
    destination = "file://out.bin" if args.dry_run else args.transport

    # Pre-flight: sending a job that does not match the loaded roll gets rejected,
    # prints nothing, and leaves the printer blinking red until its cover is
    # cycled. One status round trip is cheaper than that.
    if not args.dry_run and not args.no_verify and not destination.startswith("file://"):
        try:
            st = decode_status(query_status(status_via))
        except StatusUnavailable as exc:
            # Not being able to ask is not a reason to refuse to print -- it just
            # means printing unguarded, which is what every other tool does.
            print(f"warning: skipping the media check ({exc})", file=sys.stderr)
        else:
            if st["errors"]:
                print(f"printer reports: {', '.join(st['errors'])}", file=sys.stderr)
                return 1
            loaded = match_label(st["width_mm"], st["length_mm"], st["media_code"])
            if loaded and args.label not in loaded:
                print(f"refusing to print: asked for DK {args.label} but the printer has "
                      f"{st['width_mm']} mm {st['media']}"
                      + (f" x {st['length_mm']} mm" if st["length_mm"] else "")
                      + f" loaded (DK {' or '.join(loaded)}).\n"
                      f"Use --label {loaded[0]}, or --label auto, or --no-verify to override.",
                      file=sys.stderr)
                return 2

    print(send(bytes(instructions), destination))
    return 0


if __name__ == "__main__":
    sys.exit(main())
