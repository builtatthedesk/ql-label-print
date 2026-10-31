#!/usr/bin/env python3
"""A desktop front end for labelprint.py.

Type the text, see the label, click Print. Everything else -- model, roll,
transport, font -- comes from config.json, exactly as it does on the command
line; this window is only a nicer way in.

Launch it with pythonw.exe so no console flashes up:

    .venv\\Scripts\\pythonw.exe labelgui.py

install-shortcut.ps1 puts a Desktop shortcut on that command.
"""

from __future__ import annotations

import json
import queue
import re
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labelprint as lp  # noqa: E402  (needs the path fix above)

HERE = Path(__file__).resolve().parent
HISTORY_PATH = HERE / "gui-history.json"
HISTORY_MAX = 15
PREVIEW_W = 660          # px of window space the preview gets
DEBOUNCE_MS = 150        # re-render this long after the last keystroke
DOTS_PER_MM = 300 / 25.4     # 300 dpi head
MIN_ENDLESS_MM = 26          # the 301-dot floor, rounded up to whole mm
CAP_RATIO = 0.28             # auto length: font cap as a share of label width
MAX_AUTO_EXTRA_LINES = 3     # auto length: lines wrapping may add beyond your own
AUTO_MAX_MM = 150            # auto length: how much roll one label may spend


def capitalize_words(text: str) -> str:
    """Upper-case the first letter of each word, leaving the rest alone.

    Unlike str.title() this keeps "USB" as USB and "bob's" as Bob's.
    """
    return re.sub(r"(^|\s)([a-z])", lambda m: m.group(1) + m.group(2).upper(),
                  text)


class LabelApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cfg = lp.load_config()
        self.model = [m for m in lp.ALL_MODELS
                      if m.identifier == self.cfg["model"]][0]
        self.min_len, self.max_len = self.model.min_max_length_dots
        self.margin = int(self.cfg.get("margin", 12))  # dots around the text
        self._use_label(self.cfg["label"])

        self.results: queue.Queue = queue.Queue()
        self.printing = False
        self.render_job = None
        self.preview_img = None   # keep a reference or Tk drops the image
        self._echoing_length = False

        self._build_ui()
        self.root.after(50, self._render_preview)
        self.root.after(100, self._drain_results)

    # ------------------------------------------------------------- rolls

    def _use_label(self, identifier: str) -> None:
        """Point the app at a DK roll and work out how text sits on it."""
        self.lab = lp.label_spec(identifier)
        self.cfg["label"] = identifier

        # Same rule as the CLI: a die-cut label longer than it is wide reads
        # along its long edge, so build the canvas that way and rotate later.
        # An endless roll has no long edge until we pick one, so it starts
        # across the roll and the Along the roll tick is what turns it.
        pw, ph = self.lab.dots_printable
        self.landscape = self.lab.form_factor == 1 and ph > pw
        if getattr(self, "along_roll", None) is not None:
            self.along_roll.set(self.landscape)

    def _usable_labels(self) -> list:
        """The rolls this head will actually accept.

        brother_ql's 301-dot minimum only applies to endless rolls (which get
        padded up to it); die-cut rolls print at their own size, so the small
        ones such as 23x23 are fine. Only the maximum still filters.
        """
        out = []
        for lab in lp.ALL_LABELS:
            along = lab.dots_printable[1]
            if along <= self.max_len:
                out.append(lab)
        return out

    def _sync_length_state(self) -> None:
        self.length_auto.configure(state="normal" if self.endless else "disabled")
        self.length_box.configure(
            state="normal" if self.endless and not self.auto_length.get()
            else "disabled")
        self.length_lbl.configure(foreground="#000" if self.endless else "#aaa")

    def _on_orientation_toggle(self) -> None:
        self.landscape = self.along_roll.get()
        self._render_preview()

    def _on_auto_toggle(self) -> None:
        self._sync_length_state()
        self._render_preview()

    def _on_length_typed(self, *_args) -> None:
        # Ignore the writes we make ourselves when auto length reports back.
        if not self._echoing_length:
            self._schedule_render()

    def _show_length(self, px: int | None) -> None:
        """Report the computed length back into the box, without re-rendering."""
        if px is None or not self.auto_length.get():
            return
        shown = str(round(px / DOTS_PER_MM))
        if shown != self.length_mm.get():
            self._echoing_length = True
            try:
                self.length_mm.set(shown)
            finally:
                self._echoing_length = False

    def _on_roll_change(self, _event=None) -> None:
        chosen = self.roll.get().split()[0]
        if chosen == self.lab.identifier:
            return
        self._use_label(chosen)
        self._save_label(chosen)
        self._sync_length_state()
        self._render_preview()
        self._set_status("roll set to DK {} - make sure that is what is "
                         "loaded".format(chosen))

    def _save_label(self, identifier: str) -> None:
        """Write the roll back to config.json so the CLI agrees."""
        self._save_setting("label", identifier, "the roll")

    def _save_setting(self, key: str, value, what: str) -> None:
        try:
            raw = json.loads(lp.CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raw = {}
        raw[key] = value
        try:
            lp.CONFIG_PATH.write_text(json.dumps(raw, indent=2) + "\n",
                                      encoding="utf-8")
        except OSError as exc:
            self._set_status("could not save {} to config.json: "
                             "{}".format(what, exc), "err")

    def _on_qr_toggle(self) -> None:
        # Remembered, so a QR on every label can be the normal practice.
        self._save_setting("qr", self.qr.get(), "the QR choice")
        self._render_preview()

    # ---------------------------------------------------------------- ui

    def _build_ui(self) -> None:
        self.root.title("Print a label")
        self.root.minsize(700, 460)
        frm = ttk.Frame(self.root, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(0, weight=1)

        head = ttk.Frame(frm)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="Label text",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(head, text=self.cfg["model"],
                  foreground="#666").grid(row=0, column=1, sticky="e", padx=(0, 10))

        # Changing this writes config.json, so the CLI
        # follow the roll too. Nothing here can ask the printer what is really
        # loaded -- that needs the Bluetooth status channel.
        ttk.Label(head, text="Roll").grid(row=0, column=2, padx=(0, 4))
        self.roll_choices = ["{}  ({})".format(l.identifier, l.name)
                             for l in self._usable_labels()]
        self.roll = ttk.Combobox(head, values=self.roll_choices, width=30,
                                 state="readonly")
        # A config.json roll this head cannot take is still shown, so the
        # window says what it is about to attempt rather than quietly
        # switching to something else.
        self.roll.set(next((c for c in self.roll_choices
                            if c.split()[0] == self.lab.identifier),
                           self.lab.identifier))
        self.roll.grid(row=0, column=3, sticky="e")
        self.roll.bind("<<ComboboxSelected>>", self._on_roll_change)

        self.text = tk.Text(frm, height=3, wrap="word", font=("Segoe UI", 12),
                            undo=True)
        self.text.grid(row=1, column=0, sticky="ew", pady=(4, 8))
        self.text.focus_set()
        self.text.bind("<KeyRelease>", self._schedule_render)
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        ttk.Label(frm, text="Click Print to print  -  Enter adds a line  -  Esc closes",
                  foreground="#888").grid(row=2, column=0, sticky="w")

        # Applied to the preview and the print, never to the box you type in.
        opts = ttk.Frame(frm)
        opts.grid(row=2, column=0, sticky="e")
        self.qr = tk.BooleanVar(value=bool(self.cfg["qr"]))
        ttk.Checkbutton(opts, text="QR code", variable=self.qr,
                        command=self._on_qr_toggle).grid(row=0, column=0, padx=(0, 12))
        self.cap_words = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Capitalize Words", variable=self.cap_words,
                        command=self._render_preview).grid(row=0, column=1)

        self.canvas = tk.Canvas(frm, height=230, background="#e9e9e9",
                                highlightthickness=1,
                                highlightbackground="#c9c9c9")
        self.canvas.grid(row=3, column=0, sticky="ew", pady=10)

        ctl = ttk.Frame(frm)
        ctl.grid(row=4, column=0, sticky="ew")
        ctl.columnconfigure(7, weight=1)

        ttk.Label(ctl, text="Copies").grid(row=0, column=0, padx=(0, 4))
        self.copies = tk.IntVar(value=1)
        ttk.Spinbox(ctl, from_=1, to=50, width=4,
                    textvariable=self.copies).grid(row=0, column=1)

        # An endless roll has no length of its own -- we choose where to cut,
        # which is the whole point of one. Die-cut rolls ignore this.
        self.auto_length = tk.BooleanVar(value=True)
        self.length_auto = ttk.Checkbutton(ctl, text="Length auto",
                                           variable=self.auto_length,
                                           command=self._on_auto_toggle)
        self.length_auto.grid(row=0, column=2, padx=(12, 4))
        self.length_mm = tk.StringVar(value="40")
        self.length_box = ttk.Spinbox(ctl, from_=MIN_ENDLESS_MM, to=300, width=5,
                                      textvariable=self.length_mm,
                                      command=self._schedule_render)
        self.length_box.grid(row=0, column=3)
        self.length_lbl = ttk.Label(ctl, text="mm")
        self.length_lbl.grid(row=0, column=4, padx=(3, 0))

        # Turning the text 90 degrees is what makes a big label out of a
        # narrow roll: the 62 mm becomes the height and the length runs on
        # for as far as the words need.
        self.along_roll = tk.BooleanVar(value=self.landscape)
        ttk.Checkbutton(ctl, text="Along the roll", variable=self.along_roll,
                        command=self._on_orientation_toggle).grid(
                            row=0, column=5, padx=12)
        self.length_mm.trace_add("write", self._on_length_typed)
        self._sync_length_state()

        # The media check only answers over Bluetooth, which can take ~20 s
        # and is often unavailable. Off by default; tick it when BT status works.
        self.verify = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Check roll first (slow)",
                        variable=self.verify).grid(row=0, column=6, padx=12)

        self.history = self._load_history()
        self.recent = ttk.Combobox(ctl, values=self.history, width=28,
                                   state="readonly")
        self.recent.set("Recent...")
        self.recent.grid(row=0, column=7, sticky="e", padx=(0, 8))
        self.recent.bind("<<ComboboxSelected>>", self._pick_recent)

        ttk.Button(ctl, text="Clear", command=self._clear).grid(
            row=0, column=8, padx=(0, 6))

        self.print_btn = ttk.Button(ctl, text="Print", command=self.print_label)
        self.print_btn.grid(row=0, column=9)

        self.status = ttk.Label(frm, text="", foreground="#666", wraplength=660,
                                justify="left")
        self.status.grid(row=5, column=0, sticky="w", pady=(10, 0))

    def _set_status(self, msg: str, kind: str = "info") -> None:
        self.status.configure(
            text=msg,
            foreground={"info": "#666", "ok": "#1a7f37", "err": "#b3261e"}[kind])

    # ------------------------------------------------------------ preview

    def _current_text(self) -> str:
        text = self.text.get("1.0", "end-1c").strip()
        return capitalize_words(text) if self.cap_words.get() else text

    def _schedule_render(self, _event=None) -> None:
        if self.render_job:
            self.root.after_cancel(self.render_job)
        self.render_job = self.root.after(DEBOUNCE_MS, self._render_preview)

    def _render(self, text: str, length_px: int | None = None,
                qr: bool | None = None, serial: str | None = None) -> Image.Image:
        if qr is None:          # the print worker passes it; Tk vars are UI-thread only
            qr = self.qr.get()
            # The preview shows the next ID without using it up.
            if qr and self.cfg["qr_serial"]:
                serial = lp.next_ids(1)[0]
        return lp.render_text(text, self.lab, self.min_len, self.cfg["font"],
                              margin=self.margin, length_px=length_px, landscape=self.landscape,
                              qr=qr, qr_date=bool(self.cfg["qr_date"]), qr_serial=serial)

    @property
    def endless(self) -> bool:
        return self.lab.dots_printable[1] == 0

    def _length_px(self, text: str = "") -> int | None:
        """How long to cut an endless label. Die-cut labels decide for us."""
        if not self.endless:
            return None
        if self.auto_length.get():
            px = self._auto_length_px(text or self._current_text())
            if self.qr.get() and self.landscape:
                # Along the roll the QR can grow as tall as the roll is wide;
                # give it up to that much extra length so the words keep their size.
                px = min(self.max_len, px + self.lab.dots_printable[0])
            return px
        try:
            mm = float(self.length_mm.get())
        except ValueError:
            mm = MIN_ENDLESS_MM
        return max(self.min_len, round(mm * DOTS_PER_MM))

    def _auto_length_across(self, probe, paragraphs: list, width: int) -> int:
        """Auto length for text turned along the roll.

        Rotated, the roll's width caps the type instead of wrapping it: the
        lines have to stack inside 62 mm, and the label then runs on for as
        long as the longest line is. Nothing wraps -- an endless roll has the
        room, and a forced break here would only shrink the text.
        """
        from PIL import ImageFont

        max_h = width - 2 * self.margin
        # Type this big runs away with the roll -- 40 mm capitals turn eleven
        # characters into a 29 cm label -- so length is what is rationed, and
        # the size comes down until the label fits inside it.
        max_len = int(AUTO_MAX_MM * DOTS_PER_MM) - 2 * self.margin
        lines = [ln for ln in paragraphs if ln.strip()] or [""]

        best = None
        lo, hi = 6, 800
        while lo <= hi:
            mid = (lo + hi) // 2
            font = ImageFont.truetype(self.cfg["font"], mid)
            ascent, descent = font.getmetrics()
            stacked = int((ascent + descent) * lp.LINE_SPACING * len(lines))
            widest = max((probe.textbbox((0, 0), ln, font=font)[2]
                          for ln in lines), default=0)
            if stacked <= max_h and widest <= max_len:
                best = font
                lo = mid + 1
            else:
                hi = mid - 1
        font = best or ImageFont.truetype(self.cfg["font"], 6)

        widest = max((probe.textbbox((0, 0), ln, font=font)[2] for ln in lines),
                     default=0)
        return max(self.min_len,
                   min(self.max_len, widest + 2 * self.margin))

    def _auto_length_px(self, text: str) -> int:
        """Cut an endless label to the length this text actually needs.

        Height is what an endless roll leaves us to choose, so type size has to
        be pinned first or the answer is circular: cap the font at a share of
        the label width -- big enough to read across a room, not so big that
        one word eats 80 mm of roll -- wrap to the width at that size, and the
        length falls out of the line count.
        """
        from PIL import ImageDraw, ImageFont

        width = self.lab.dots_printable[0]
        max_w = width - 2 * self.margin
        cap = max(24, int(width * CAP_RATIO))
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        paragraphs = text.split("\n")

        if self.landscape:
            return self._auto_length_across(probe, paragraphs, width)

        def largest_at(max_lines: int):
            """Biggest size that fits the width in at most max_lines lines."""
            best = None
            lo, hi = 6, cap
            while lo <= hi:
                mid = (lo + hi) // 2
                font = ImageFont.truetype(self.cfg["font"], mid)
                lines = lp.wrap_at_size(probe, paragraphs, font, max_w)
                widest = max((probe.textbbox((0, 0), ln, font=font)[2]
                              for ln in lines), default=0)
                if widest <= max_w and len(lines) <= max_lines:
                    best = (font, lines)
                    lo = mid + 1
                else:
                    hi = mid - 1
            return best

        # Respect the breaks that were typed: a bigger font is not worth
        # exploding "SHELF 4" into "SHELF" over "4". Only when the text cannot
        # be set on its own lines at any size does wrapping buy extra ones.
        best = None
        for extra in range(0, MAX_AUTO_EXTRA_LINES + 1):
            best = largest_at(len(paragraphs) + extra)
            if best:
                break
        if best is None:                     # one unbreakable long word
            font = ImageFont.truetype(self.cfg["font"], 6)
            best = (font, paragraphs)

        font, lines = best
        ascent, descent = font.getmetrics()
        height = int((ascent + descent) * lp.LINE_SPACING * len(lines)) \
            + 2 * self.margin
        return max(self.min_len, min(self.max_len, height))

    def _render_preview(self) -> None:
        self.render_job = None
        text = self._current_text()
        cw = self.canvas.winfo_width() or PREVIEW_W
        if not text:
            self.canvas.delete("all")
            self.canvas.create_text(cw // 2, 115, text="type something",
                                    fill="#aaa", font=("Segoe UI", 11))
            return
        length_px = self._length_px(text)
        try:
            img = self._render(text, length_px)
        except Exception as exc:                       # bad font, odd glyph...
            self.canvas.delete("all")                  # never show a stale label
            self._set_status("preview failed: {}".format(exc), "err")
            self.preview_failed = True
            return
        if getattr(self, "preview_failed", False):
            self._set_status("")
            self.preview_failed = False

        scale = min(PREVIEW_W / img.width, 210 / img.height)
        shown = img.resize((max(1, int(img.width * scale)),
                            max(1, int(img.height * scale))), Image.LANCZOS)
        self.preview_img = ImageTk.PhotoImage(shown)
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, 115, image=self.preview_img)
        self._show_length(length_px)

    # -------------------------------------------------------------- print

    def print_label(self) -> None:
        if self.printing:
            return
        text = self._current_text()
        if not text:
            self._set_status("nothing to print", "err")
            return

        try:
            copies = max(1, int(self.copies.get()))
        except (tk.TclError, ValueError):
            copies = 1
        self.printing = True
        self.print_btn.configure(state="disabled")
        self._set_status("checking the roll..." if self.verify.get()
                         else "printing...")
        # Tk variables belong to the UI thread, so read the length here and
        # hand the worker a plain number.
        threading.Thread(target=self._print_worker,
                         args=(text, copies, self.verify.get(),
                               self._length_px(text), self.qr.get()),
                         daemon=True).start()

    def _print_worker(self, text: str, copies: int, verify: bool,
                      length_px: int | None = None, qr: bool = False) -> None:
        """Render, rasterise and send. Runs off the UI thread."""
        try:
            # With IDs every copy is a different label; otherwise one image.
            ids = lp.next_ids(copies) if qr and self.cfg["qr_serial"] else []
            imgs = [self._render(text, length_px, qr, serial)
                    for serial in (ids or [None])]
            if self.landscape:
                imgs = [im.rotate(-90, expand=True) for im in imgs]
            img = imgs[0]
            floor = self.min_len if self.endless else 1
            if not (floor <= img.height <= self.max_len):
                raise RuntimeError(
                    "label is {} dots; the {} accepts {}-{}".format(
                        img.height, self.model.identifier,
                        floor, self.max_len))

            qlr = lp.new_raster(self.cfg["model"], self.lab)
            qlr.exception_on_warning = True
            data = bytes(lp.convert(
                qlr=qlr, images=imgs if ids else imgs * copies, label=self.cfg["label"],
                rotate=0, threshold=70.0, dither=False, compress=True,
                red=(self.cfg["label"] == "62red"), dpi_600=False, hq=True,
                cut=True))

            warning = ""
            if verify:
                via = self.cfg.get("status_transport") or self.cfg["transport"]
                try:
                    st = lp.decode_status(lp.query_status(via))
                except lp.StatusUnavailable as exc:
                    warning = "roll not verified ({}) - ".format(
                        str(exc).splitlines()[0])
                else:
                    if st["errors"]:
                        raise RuntimeError("printer reports: "
                                           + ", ".join(st["errors"]))
                    loaded = lp.match_label(st["width_mm"], st["length_mm"],
                                            st["media_code"])
                    if loaded and self.cfg["label"] not in loaded:
                        raise RuntimeError(
                            "printer has DK {} loaded, not DK {} - nothing "
                            "printed".format(" or ".join(loaded),
                                             self.cfg["label"]))

            if ids:
                try:
                    lp.take_ids(ids)
                except SystemExit as exc:   # not an Exception; would kill the thread
                    raise RuntimeError(str(exc))
            lp.send(data, self.cfg["transport"])
            plural = "" if copies == 1 else " x{}".format(copies)
            if ids:
                plural += "  ID " + ids[0] + (" .. " + ids[-1] if len(ids) > 1 else "")
            self.results.put(("ok", "printed" + plural + "  " + warning, text))
        except Exception as exc:
            detail = str(exc) or exc.__class__.__name__
            traceback.print_exc()
            self.results.put(("err", detail, None))

    def _drain_results(self) -> None:
        try:
            while True:
                kind, msg, text = self.results.get_nowait()
                self.printing = False
                self.print_btn.configure(state="normal")
                self._set_status(msg, kind)
                if kind == "ok" and text:
                    self._remember(text)
                    self.text.tag_add("sel", "1.0", "end-1c")
                    self.text.focus_set()
        except queue.Empty:
            pass
        self.root.after(100, self._drain_results)

    # ------------------------------------------------------------ history

    def _load_history(self) -> list:
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))[:HISTORY_MAX]
        except Exception:
            return []

    def _remember(self, text: str) -> None:
        one_line = text.replace("\n", " \\n ")
        self.history = [one_line] + [h for h in self.history if h != one_line]
        self.history = self.history[:HISTORY_MAX]
        try:
            HISTORY_PATH.write_text(json.dumps(self.history, indent=2),
                                    encoding="utf-8")
        except OSError:
            pass
        self.recent.configure(values=self.history)

    def _clear(self) -> None:
        self.text.delete("1.0", "end")
        self.copies.set(1)
        self.recent.set("Recent...")
        self._set_status("")
        self.text.focus_set()
        self._render_preview()

    def _pick_recent(self, _event) -> None:
        chosen = self.recent.get().replace(" \\n ", "\n")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", chosen)
        self.recent.set("Recent...")
        self.text.focus_set()
        self._render_preview()


def main() -> int:
    root = tk.Tk()
    try:
        root.iconbitmap(str(HERE / "label.ico"))
    except tk.TclError:
        pass                                  # icon is optional
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    LabelApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
