"""Desktop GUI for the TRYX Panorama AIO display.

A dark-themed Tkinter front-end around :class:`tryx.client.TryxClient`. The
daemon owns the serial connection, so the display persists after this app is
closed. All device I/O runs on a background thread; log/status updates are
marshalled back to the UI thread through a queue.
"""

from __future__ import annotations

import glob
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from . import __version__
from .client import TryxClient
from .device import DEFAULT_PORT, VALID_RATIOS

# ---------------------------------------------------------------------------
# Theme palette
# ---------------------------------------------------------------------------

BG = "#14151c"
CARD = "#1c1e27"
CARD2 = "#252837"
FIELD = "#20222d"
FG = "#e8e9f0"
MUTED = "#8b90a5"
ACCENT = "#6ea8fe"
OK = "#7ee2b8"
ERR = "#f4708a"
WARN = "#e0b45c"

RATIO_LABELS = [("Native (full panel)", ""), ("1:1 (square)", "1:1"),
                ("3:2", "3:2"), ("4:3", "4:3"), ("16:9", "16:9")]

IMAGE_EXT = ("*.jpg", "*.jpeg", "*.png", "*.bmp", "*.gif", "*.webp")
VIDEO_EXT = ("*.mp4", "*.avi", "*.mkv", "*.mov")


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class TryxApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.client: TryxClient | None = None
        self.busy = False
        self.log_q: "queue.Queue[tuple]" = queue.Queue()
        self._preview_ref = None  # keep a ref so the thumbnail isn't GC'd
        self._action_buttons: list[ttk.Button] = []

        self._setup_style()
        self._build_ui()
        self._drain_log()
        self._autodetect_port()
        # Auto-connect on startup (async, non-blocking).
        self.root.after(300, self._connect_async)

    # -- theme --------------------------------------------------------------

    def _setup_style(self) -> None:
        self.root.title("TRYX Panorama Control")
        self.root.configure(bg=BG)
        self.root.minsize(760, 780)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=FG,
                        fieldbackground=FIELD, bordercolor=CARD,
                        lightcolor=CARD, darkcolor=CARD,
                        troughcolor=CARD2, selectbackground=ACCENT,
                        selectforeground="#0c0d12", font=("Inter", 10))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Card.TLabel", background=CARD, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=FG,
                        font=("Inter", 16, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED,
                        font=("Inter", 9))

        style.configure("TButton", background=CARD2, foreground=FG,
                        bordercolor=CARD2, focusthickness=0,
                        padding=(14, 8), font=("Inter", 10))
        style.map("TButton",
                  background=[("active", "#2e3348"), ("disabled", CARD)],
                  foreground=[("disabled", "#5b5f70")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#0c0d12",
                        bordercolor=ACCENT, padding=(16, 9),
                        font=("Inter", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#8bbaff"),
                                                ("disabled", CARD)],
                  foreground=[("disabled", "#5b5f70")])

        style.configure("TCheckbutton", background=BG, foreground=FG,
                        focusthickness=0)
        style.map("TCheckbutton", background=[("active", BG)])

        style.configure("TCombobox", background=FIELD, foreground=FG,
                        fieldbackground=FIELD, arrowcolor=FG,
                        bordercolor=CARD2, padding=6)
        style.map("TCombobox",
                  fieldbackground=[("readonly", FIELD)],
                  selectbackground=[("readonly", FIELD)],
                  selectforeground=[("readonly", FG)])

        style.configure("TLabelframe", background=BG, foreground=MUTED,
                        bordercolor=CARD, padding=6)
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED,
                        font=("Inter", 9, "bold"))

        style.configure("Vertical.TScrollbar", background=CARD2,
                        troughcolor=CARD, arrowcolor=MUTED, bordercolor=CARD)

    # -- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=(18, 16))
        main.pack(fill="both", expand=True)

        # Header
        header = ttk.Frame(main)
        header.pack(fill="x")
        ttk.Label(header, text="TRYX Panorama", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="AIO display controller", style="Sub.TLabel").pack(anchor="w")

        # Status pill
        self.status_frame = ttk.Frame(header, style="Card.TFrame", padding=(10, 6))
        self.status_frame.pack(anchor="w", pady=(8, 0))
        self.status_dot = tk.Label(self.status_frame, text="●", bg=CARD, fg=WARN,
                                   font=("Inter", 10))
        self.status_dot.pack(side="left")
        self.status_text = tk.Label(self.status_frame, text="Disconnected",
                                    bg=CARD, fg=MUTED, font=("Inter", 10))
        self.status_text.pack(side="left", padx=(6, 12))
        self.device_info = tk.Label(self.status_frame, text="", bg=CARD, fg=MUTED,
                                    font=("Inter", 9))
        self.device_info.pack(side="left")

        # Device section
        dev = ttk.LabelFrame(main, text="DEVICE", padding=12)
        dev.pack(fill="x", pady=(14, 10))

        row = ttk.Frame(dev)
        row.pack(fill="x")
        ttk.Label(row, text="Serial port", style="Muted.TLabel").pack(side="left")
        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, width=22)
        self.port_box.pack(side="left", padx=(10, 6))
        ttk.Button(row, text="↻", width=3, command=self._autodetect_port).pack(side="left")
        self.connect_btn = ttk.Button(row, text="Connect", style="Accent.TButton",
                                      command=self._toggle_connect)
        self.connect_btn.pack(side="right")

        opts = ttk.Frame(dev)
        opts.pack(fill="x", pady=(10, 0))
        self.adb_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Use ADB for fast upload", variable=self.adb_var).pack(side="left")
        ttk.Label(opts, text=f"v{__version__}", style="Muted.TLabel").pack(side="right")

        # Display section
        disp = ttk.LabelFrame(main, text="DISPLAY", padding=12)
        disp.pack(fill="x", pady=(0, 10))

        body = ttk.Frame(disp)
        body.pack(fill="x")

        # Preview pane
        self.preview = tk.Label(body, text="No image\nselected", bg=CARD2, fg=MUTED,
                                width=46, height=24, font=("Inter", 9))
        self.preview.pack(side="left", padx=(0, 14))

        controls = ttk.Frame(body)
        controls.pack(side="left", fill="both", expand=True)

        for label, cmd in (("Set Image…", self._pick_image),
                           ("Set Video…", self._pick_video),
                           ("Slideshow…", self._pick_slideshow),
                           ("Hardware Monitor", self._set_sysinfo)):
            b = ttk.Button(controls, text=label, command=cmd)
            b.pack(fill="x", pady=2)
            self._action_buttons.append(b)

        ratio_row = ttk.Frame(controls)
        ratio_row.pack(fill="x", pady=(14, 0))
        ttk.Label(ratio_row, text="Aspect ratio", style="Muted.TLabel").pack(side="left")
        self.ratio_var = tk.StringVar(value="1:1")
        ratio_box = ttk.Combobox(ratio_row, textvariable=self.ratio_var,
                                 values=[lbl for lbl, _ in RATIO_LABELS],
                                 state="readonly", width=20)
        ratio_box.pack(side="left", padx=(10, 0))
        ratio_box.current(1)

        self.selected_label = ttk.Label(controls, text="", style="Muted.TLabel",
                                        wraplength=260)
        self.selected_label.pack(fill="x", pady=(12, 0))

        # Activity log
        act = ttk.LabelFrame(main, text="ACTIVITY", padding=6)
        act.pack(fill="both", expand=True)
        self.log = tk.Text(act, height=10, bg=CARD, fg=FG, insertbackground=FG,
                           relief="flat", padx=10, pady=8, wrap="word",
                           font=("JetBrains Mono", 9))
        self.log.pack(side="left", fill="both", expand=True)
        self.log.configure(state="disabled")
        scroll = ttk.Scrollbar(act, command=self.log.yview)
        scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scroll.set)

        self._log("Ready. Click Connect to talk to the AIO.", "muted")

    # -- logging ------------------------------------------------------------

    def _log(self, msg: str, level: str = "info") -> None:
        self.log_q.put((level, msg))

    _TAG_COLORS = {"info": FG, "ok": OK, "err": ERR, "warn": WARN, "muted": MUTED}

    def _write_log(self, level: str, msg: str) -> None:
        color = self._TAG_COLORS.get(level, FG)
        tag = f"t_{level}"
        self.log.tag_configure(tag, foreground=color)
        self.log.configure(state="normal")
        self.log.insert("end", f"{msg}\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- connection ---------------------------------------------------------

    def _autodetect_port(self) -> None:
        ports = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
        if ports:
            self.port_box["values"] = ports
            if self.port_var.get() not in ports and ports:
                self.port_var.set(ports[0])

    def _toggle_connect(self) -> None:
        if self.busy:
            return
        self._connect_async()

    def _connect_async(self) -> None:
        if self.busy:
            return
        port = self.port_var.get().strip()
        self._set_status("Connecting…", WARN, "")
        self._run_task("connect", lambda: self._do_connect(port))

    def _do_connect(self, port: str) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        client = TryxClient(port)   # auto-starts the daemon if needed
        info = client.info()
        self.client = client
        return info

    # -- task runner --------------------------------------------------------

    def _run_task(self, name: str, fn) -> None:
        """Run ``fn`` on a worker thread; log progress and route the result."""
        self.busy = True
        self._set_controls_enabled(False)
        self._log(f"{name}…", "info")

        def worker():
            try:
                result = fn()
                self.log_q.put(("task_ok", (name, result)))
            except Exception as e:  # noqa: BLE001
                self.log_q.put(("task_err", (name, e)))
            finally:
                self.busy = False

        threading.Thread(target=worker, daemon=True).start()

    def _drain_log(self) -> None:
        try:
            while True:
                item = self.log_q.get_nowait()
                kind = item[0]
                if kind == "task_ok":
                    name, result = item[1]
                    self._on_task_ok(name, result)
                elif kind == "task_err":
                    name, err = item[1]
                    self._on_task_err(name, err)
                else:
                    self._write_log(kind, item[1])
        except queue.Empty:
            pass
        self.root.after(50, self._drain_log)

    def _on_task_ok(self, name: str, result) -> None:
        self._set_controls_enabled(True)
        if name == "connect":
            info = result or {}
            self._set_status("Connected", OK, self._describe(info))
            self.connect_btn.configure(text="Refresh")
            self._log("Connected (daemon owns the connection).", "ok")
            self._log(self._describe(info), "muted")
        elif name in ("image", "video", "slideshow"):
            self._log(f"{name} applied.", "ok")
        elif name == "sysinfo":
            self._log("Hardware monitor screen applied.", "ok")

    def _on_task_err(self, name: str, err: Exception) -> None:
        self._set_controls_enabled(True)
        if name == "connect":
            self.client = None
            self._set_status("Disconnected", ERR, "")
            self.connect_btn.configure(text="Connect")
        msg = str(err)
        self._log(f"{name} failed: {msg}", "err")
        if "No such file or directory" in msg or "could not open port" in msg:
            self._log("AIO not detected on USB. Fully power off the PC "
                      "(PSU off/unplug, 30 s) — a normal restart does NOT "
                      "reset the display board.", "warn")
        self._set_status("Error", ERR, "")

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for b in self._action_buttons:
            b.configure(state=state)

    def _set_status(self, text: str, color: str, info: str) -> None:
        self.status_dot.configure(fg=color)
        self.status_text.configure(text=text, fg=color)
        self.device_info.configure(text=info)

    @staticmethod
    def _describe(info: dict) -> str:
        if not info:
            return ""
        parts = []
        for k in ("productId", "sn", "OS"):
            if info.get(k):
                parts.append(str(info[k]))
        ver = info.get("version")
        if isinstance(ver, dict):
            parts.append(f"fw {ver.get('firmware', '?')}")
        return "  ·  ".join(parts)

    # -- display actions ----------------------------------------------------

    def _ratio(self) -> str:
        label = self.ratio_var.get()
        for lbl, val in RATIO_LABELS:
            if lbl == label:
                return val
        return "1:1"

    def _require_device(self) -> bool:
        if self.client is None:
            messagebox.showwarning("Not connected",
                                   "Connect to the AIO first (Device → Connect).")
            return False
        return True

    def _pick_image(self) -> None:
        if not self._require_device():
            return
        path = filedialog.askopenfilename(
            title="Choose an image",
            filetypes=[("Images", IMAGE_EXT), ("All files", "*.*")])
        if not path:
            return
        self._show_preview(path)
        self.selected_label.configure(text=os.path.basename(path))
        self._run_task("image", lambda: self._apply_image(path))

    def _pick_video(self) -> None:
        if not self._require_device():
            return
        path = filedialog.askopenfilename(
            title="Choose a video",
            filetypes=[("Videos", VIDEO_EXT), ("All files", "*.*")])
        if not path:
            return
        self.selected_label.configure(text=os.path.basename(path))
        self._run_task("video", lambda: self._apply_video(path))

    def _pick_slideshow(self) -> None:
        if not self._require_device():
            return
        paths = filedialog.askopenfilenames(
            title="Choose images for the slideshow",
            filetypes=[("Images", IMAGE_EXT), ("All files", "*.*")])
        if not paths:
            return
        self._show_preview(paths[0])
        self.selected_label.configure(
            text=f"{len(paths)} images selected")
        self._run_task("slideshow", lambda: self._apply_slideshow(paths))

    def _set_sysinfo(self) -> None:
        if not self._require_device():
            return
        self.selected_label.configure(text="Hardware monitor")
        self._run_task("sysinfo", lambda: self._apply_sysinfo())

    # -- display operations (run on worker thread; delegate to the daemon) --

    @staticmethod
    def _check(r: dict) -> None:
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "unknown error"))

    def _apply_image(self, path: str) -> None:
        assert self.client is not None
        self._check(self.client.set_image(path, ratio=self._ratio(),
                                          use_adb=self.adb_var.get()))

    def _apply_video(self, path: str) -> None:
        assert self.client is not None
        self._check(self.client.set_video(path, ratio=self._ratio(),
                                          use_adb=self.adb_var.get()))

    def _apply_slideshow(self, paths: list[str]) -> None:
        assert self.client is not None
        self._check(self.client.set_slideshow(list(paths), ratio=self._ratio(),
                                              use_adb=self.adb_var.get()))

    def _apply_sysinfo(self) -> None:
        assert self.client is not None
        self._check(self.client.set_sysinfo())

    # -- preview ------------------------------------------------------------

    def _show_preview(self, path: str) -> None:
        try:
            img = Image.open(path)
            img.thumbnail((480, 400))
            photo = ImageTk.PhotoImage(img)
            self._preview_ref = photo
            self.preview.configure(image=photo, text="", bg=CARD2)
        except Exception:
            self.preview.configure(image="", text="(no preview)", bg=CARD2)


def main() -> None:
    root = tk.Tk()
    TryxApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
