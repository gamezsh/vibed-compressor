"""
Video Compressor — bulk video compression to a target file size.
Run from source : uv run compressor.py
Run as built exe: ./dist/VideoCompressor
"""

import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Prevent CMD windows from flashing on Windows for every subprocess call
_SUBPROCESS_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


# ── ffmpeg helpers ────────────────────────────────────────────────────────────

def _get_ffmpeg_exe() -> str:
    if getattr(sys, "frozen", False):
        ext = ".exe" if os.name == "nt" else ""
        return os.path.join(sys._MEIPASS, f"ffmpeg{ext}")
    system = shutil.which("ffmpeg")
    if system:
        return system
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _check_ffmpeg() -> bool:
    try:
        exe = _get_ffmpeg_exe()
        return os.path.isfile(exe) or bool(shutil.which(exe))
    except Exception:
        return False


def _get_duration(path: str) -> float:
    result = subprocess.run(
        [_get_ffmpeg_exe(), "-i", path],
        capture_output=True, text=True, **_SUBPROCESS_FLAGS,
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", result.stderr)
    if not m:
        raise ValueError("Could not read video duration")
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _parse_time(text: str) -> float:
    parts = text.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def compress_video(input_path: str, output_path: str, target_mb: float,
                   on_progress=None, trim_start: float | None = None,
                   trim_end: float | None = None) -> tuple[bool, str]:
    """Compress video to at most target_mb. Returns (True, size_str) or (False, error_msg)."""
    tmpdir = None
    try:
        ffmpeg = _get_ffmpeg_exe()
        full_duration = _get_duration(input_path)
        if full_duration <= 0:
            return False, "Could not read video duration."

        t_start = trim_start or 0.0
        t_end = trim_end if trim_end is not None else full_duration
        duration = t_end - t_start
        if duration <= 0:
            return False, "Trim range produces zero duration."

        audio_kbps = 128
        target_bytes = int(target_mb * 1024 * 1024)

        # Start at 90% of budget — container/muxer overhead eats the rest
        video_kbps = max(50, (target_mb * 0.90 * 1024 * 8 / duration) - audio_kbps)

        tmpdir = tempfile.mkdtemp(prefix="vcmp_")
        log_prefix = os.path.join(tmpdir, "ffmpeg2pass")
        null_out = "/dev/null" if os.name != "nt" else "NUL"

        # Input-seek args (timestamps reset to 0 after seek, so -t is relative)
        seek_args = ["-ss", f"{t_start:.3f}"] if t_start > 0 else []
        dur_args = ["-t", f"{duration:.3f}"] if (trim_start is not None or trim_end is not None) else []

        def run(args):
            proc = subprocess.run(args, capture_output=True, text=True, **_SUBPROCESS_FLAGS)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr[-800:])

        def two_pass(label):
            if on_progress:
                on_progress(f"{label} 1/2…")
            run([
                ffmpeg, "-y", *seek_args, "-i", input_path, *dur_args,
                "-c:v", "libx264", "-b:v", f"{video_kbps:.0f}k",
                "-pass", "1", "-passlogfile", log_prefix,
                "-an", "-f", "null", null_out,
            ])
            if on_progress:
                on_progress(f"{label} 2/2…")
            run([
                ffmpeg, "-y", *seek_args, "-i", input_path, *dur_args,
                "-c:v", "libx264", "-b:v", f"{video_kbps:.0f}k",
                "-pass", "2", "-passlogfile", log_prefix,
                "-c:a", "aac", "-b:a", f"{audio_kbps}k",
                output_path,
            ])

        # Encode, then keep scaling down until the file fits (max 3 attempts)
        for attempt in range(3):
            label = "Pass" if attempt == 0 else f"Retry {attempt}"
            two_pass(label)

            actual_bytes = os.path.getsize(output_path)
            if actual_bytes <= target_bytes:
                break

            # Scale bitrate proportionally to the overage, with extra 5% safety margin
            video_kbps = max(50, video_kbps * (target_bytes / actual_bytes) * 0.95)

        actual_mb = os.path.getsize(output_path) / (1024 * 1024)
        return True, f"done  {actual_mb:.1f} MB"

    except Exception as exc:
        return False, str(exc)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── palette ───────────────────────────────────────────────────────────────────

ACCENT  = "#4F8EF7"
BG      = "#1E1E2E"
SURFACE = "#2A2A3E"
TEXT    = "#CDD6F4"
MUTED   = "#6C7086"
GREEN   = "#A6E3A1"
RED     = "#F38BA8"
YELLOW  = "#F9E2AF"


# ── trim dialog ───────────────────────────────────────────────────────────────

class TrimDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, row):
        super().__init__(parent)
        self.row = row
        self.title(f"Trim — {Path(row.path).name}")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()

        self._tmpdir = tempfile.mkdtemp(prefix="vcmp_trim_")
        self._duration = 0.0
        self._q: queue.Queue = queue.Queue()
        self._anim_frames: list = []
        self._anim_index = 0
        self._anim_job = None

        self._start_var = tk.DoubleVar(value=0.0)
        self._start_entry_var = tk.StringVar(value="00:00:00")
        self._end_var = tk.DoubleVar(value=100.0)
        self._end_entry_var = tk.StringVar(value="00:01:40")

        self._build_ui()
        self._center()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()

        threading.Thread(target=self._fetch_duration, daemon=True).start()

    def _poll(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                fn, args, kwargs = self._q.get_nowait()
                fn(*args, **kwargs)
        except queue.Empty:
            pass
        self.after(30, self._poll)

    def _ui(self, fn, *args, **kwargs):
        self._q.put((fn, args, kwargs))

    # ── duration loading ──────────────────────────────────────────────────────

    def _fetch_duration(self):
        try:
            dur = _get_duration(self.row.path)
        except Exception as e:
            self._ui(self._dur_lbl.config, text=f"Error reading duration: {e}")
            return
        self._ui(self._on_duration_loaded, dur)

    def _on_duration_loaded(self, dur: float):
        self._duration = dur
        self._dur_lbl.config(text=f"Duration: {_fmt_time(dur)}")

        init_start = self.row.trim_start or 0.0
        init_end = self.row.trim_end if self.row.trim_end is not None else dur

        self._start_slider.config(to=dur)
        self._end_slider.config(to=dur)

        self._start_var.set(init_start)
        self._end_var.set(init_end)
        self._start_entry_var.set(_fmt_time(init_start))
        self._end_entry_var.set(_fmt_time(init_end))
        self._update_segment_label()

        self._preview_at(init_start)

    # ── layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        tk.Label(
            self, text=Path(self.row.path).name, bg=BG, fg=TEXT,
            font=("Segoe UI", 11, "bold"), wraplength=480, anchor="w",
        ).pack(fill="x", padx=16, pady=(12, 2))

        self._dur_lbl = tk.Label(self, text="Loading…", bg=BG, fg=MUTED,
                                  font=("Segoe UI", 9), anchor="w")
        self._dur_lbl.pack(fill="x", padx=16, pady=(0, 6))

        # Preview area — fixed 480×270
        preview_box = tk.Frame(self, bg=SURFACE, width=480, height=270)
        preview_box.pack(padx=16, pady=(0, 8))
        preview_box.pack_propagate(False)

        self._preview_lbl = tk.Label(
            preview_box, bg=SURFACE, fg=MUTED,
            text="Preview will appear here\n(click a Preview button below)",
            font=("Segoe UI", 10),
        )
        self._preview_lbl.pack(fill="both", expand=True)

        # Start and end time rows
        self._start_slider, self._end_slider = self._make_time_rows()

        # Segment info
        self._segment_lbl = tk.Label(self, text="", bg=BG, fg=YELLOW,
                                      font=("Segoe UI", 9))
        self._segment_lbl.pack(pady=(2, 0))

        # Action buttons
        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=16, pady=(8, 12))

        tk.Button(btn_row, text="Reset trim", bg=SURFACE, fg=MUTED,
                  relief="flat", cursor="hand2", font=("Segoe UI", 10),
                  command=self._reset).pack(side="left")

        tk.Button(btn_row, text="Cancel", bg=SURFACE, fg=TEXT,
                  relief="flat", cursor="hand2", font=("Segoe UI", 10),
                  command=self._on_close).pack(side="right", padx=(6, 0))

        tk.Button(btn_row, text="Apply trim", bg=ACCENT, fg="white",
                  relief="flat", cursor="hand2",
                  font=("Segoe UI", 10, "bold"), padx=14,
                  command=self._apply).pack(side="right")

    def _make_time_rows(self):
        """Build start/end time rows; returns (start_slider, end_slider)."""
        sliders = []
        specs = [
            ("Start:", self._start_var, self._start_entry_var, "start",
             lambda: self._preview_at(self._start_var.get())),
            ("End:",   self._end_var,   self._end_entry_var,   "end",
             lambda: self._preview_at(self._end_var.get())),
        ]
        for label, dvar, evar, which, preview_fn in specs:
            row = tk.Frame(self, bg=BG)
            row.pack(fill="x", padx=16, pady=3)

            tk.Label(row, text=label, bg=BG, fg=TEXT,
                     font=("Segoe UI", 10), width=6, anchor="w").pack(side="left")

            slider = tk.Scale(
                row, variable=dvar, from_=0, to=100,
                orient="horizontal", bg=BG, fg=TEXT, troughcolor=SURFACE,
                activebackground=ACCENT, highlightthickness=0,
                length=288, resolution=1, showvalue=False,
                command=lambda v, w=which: self._on_slider(w, float(v)),
            )
            slider.pack(side="left", padx=(0, 6))
            sliders.append(slider)

            entry = tk.Entry(row, textvariable=evar, width=9,
                             bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                             relief="flat", font=("Segoe UI", 10))
            entry.pack(side="left", padx=(0, 6))
            entry.bind("<Return>",   lambda _e, w=which: self._on_entry(w))
            entry.bind("<FocusOut>", lambda _e, w=which: self._on_entry(w))

            tk.Button(row, text="Preview", bg=ACCENT, fg="white",
                      relief="flat", cursor="hand2", font=("Segoe UI", 9),
                      command=preview_fn).pack(side="left")

        return sliders[0], sliders[1]

    # ── interaction ───────────────────────────────────────────────────────────

    def _on_slider(self, which: str, val: float):
        if which == "start":
            self._start_entry_var.set(_fmt_time(val))
        else:
            self._end_entry_var.set(_fmt_time(val))
        self._update_segment_label()

    def _on_entry(self, which: str):
        evar = self._start_entry_var if which == "start" else self._end_entry_var
        dvar = self._start_var if which == "start" else self._end_var
        try:
            val = _parse_time(evar.get())
        except ValueError:
            return
        val = max(0.0, min(val, self._duration))
        dvar.set(val)
        evar.set(_fmt_time(val))
        self._update_segment_label()

    def _update_segment_label(self):
        start = self._start_var.get()
        end = self._end_var.get()
        if end > start:
            self._segment_lbl.config(
                text=f"{_fmt_time(start)} → {_fmt_time(end)}  (duration: {_fmt_time(end - start)})",
                fg=YELLOW,
            )
        else:
            self._segment_lbl.config(text="End must be after start", fg=RED)

    # ── frame animation preview ───────────────────────────────────────────────

    def _preview_at(self, t: float):
        self._stop_anim()
        frames_dir = os.path.join(self._tmpdir, "frames")
        shutil.rmtree(frames_dir, ignore_errors=True)
        os.makedirs(frames_dir)
        self._preview_lbl.config(text="Extracting frames…", image="")
        threading.Thread(target=self._extract_frames, args=(t, frames_dir), daemon=True).start()

    def _extract_frames(self, t: float, frames_dir: str):
        out_pattern = os.path.join(frames_dir, "frame_%04d.png")
        try:
            subprocess.run(
                [
                    _get_ffmpeg_exe(), "-y",
                    "-ss", f"{t:.3f}",
                    "-i", self.row.path,
                    "-t", "3",
                    "-vf", "fps=10,scale=480:-2",
                    out_pattern,
                ],
                capture_output=True, **_SUBPROCESS_FLAGS,
            )
        except Exception as e:
            self._ui(self._preview_lbl.config, text=f"Error: {e}", image="")
            return

        frame_paths = sorted(Path(frames_dir).glob("frame_*.png"))
        if not frame_paths:
            self._ui(self._preview_lbl.config, text="Could not extract frames", image="")
            return

        self._ui(self._start_anim, frame_paths)

    def _start_anim(self, frame_paths):
        try:
            self._anim_frames = [tk.PhotoImage(file=str(p)) for p in frame_paths]
        except Exception as e:
            self._preview_lbl.config(text=f"Could not load frames: {e}", image="")
            return
        self._anim_index = 0
        self._tick_anim()

    def _tick_anim(self):
        if not self._anim_frames or not self.winfo_exists():
            return
        img = self._anim_frames[self._anim_index % len(self._anim_frames)]
        self._preview_lbl.config(image=img, text="")
        self._anim_index += 1
        self._anim_job = self.after(100, self._tick_anim)

    def _stop_anim(self):
        if self._anim_job:
            self.after_cancel(self._anim_job)
            self._anim_job = None
        self._anim_frames = []
        self._anim_index = 0

    # ── actions ───────────────────────────────────────────────────────────────

    def _apply(self):
        if self._duration <= 0:
            messagebox.showwarning("Not ready", "Still loading video info, please wait.", parent=self)
            return
        start = self._start_var.get()
        end = self._end_var.get()
        if end <= start:
            messagebox.showerror("Invalid trim", "End time must be after start time.", parent=self)
            return

        self.row.trim_start = start if start > 0 else None
        self.row.trim_end = end if end < self._duration else None

        if self.row.trim_start is not None or self.row.trim_end is not None:
            s = _fmt_time(self.row.trim_start or 0.0)
            e = _fmt_time(self.row.trim_end or self._duration)
            self.row.set_trim_label(f"✂ {s}–{e}")
        else:
            self.row.set_trim_label("")

        self._on_close()

    def _reset(self):
        self.row.trim_start = None
        self.row.trim_end = None
        self.row.set_trim_label("")
        self._on_close()

    def _center(self):
        self.update_idletasks()
        w, h = 540, 490
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _on_close(self):
        self._stop_anim()
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        self.destroy()


# ── file row widget ───────────────────────────────────────────────────────────

class FileRow:
    def __init__(self, parent: tk.Frame, path: str, remove_cb, trim_cb):
        self.path = path
        self.trim_start: float | None = None
        self.trim_end: float | None = None

        self.frame = tk.Frame(parent, bg=SURFACE, pady=4, padx=8)
        self.frame.pack(fill="x", pady=2)

        self.lbl_name = tk.Label(
            self.frame, text=Path(path).name, bg=SURFACE, fg=TEXT,
            font=("Segoe UI", 10), anchor="w", cursor="hand2",
        )
        self.lbl_name.pack(side="left", fill="x", expand=True)

        self.lbl_status = tk.Label(
            self.frame, text="queued", bg=SURFACE, fg=MUTED,
            font=("Segoe UI", 9), width=18, anchor="e",
        )
        self.lbl_status.pack(side="right")

        self.lbl_trim = tk.Label(
            self.frame, text="", bg=SURFACE, fg=YELLOW,
            font=("Segoe UI", 8),
        )
        self.lbl_trim.pack(side="right", padx=(0, 4))

        self.btn_trim = tk.Button(
            self.frame, text="✂", bg=SURFACE, fg=MUTED,
            relief="flat", cursor="hand2", font=("Segoe UI", 12),
            bd=0, padx=2, command=lambda: trim_cb(self),
        )
        self.btn_trim.pack(side="right", padx=(0, 2))

        def _enter(_e):
            for w in (self.frame, self.lbl_name, self.lbl_status, self.lbl_trim):
                w.config(bg="#333352")
            self.btn_trim.config(bg="#333352")

        def _leave(_e):
            for w in (self.frame, self.lbl_name, self.lbl_status, self.lbl_trim):
                w.config(bg=SURFACE)
            self.btn_trim.config(bg=SURFACE)

        for widget in (self.frame, self.lbl_name, self.lbl_status, self.lbl_trim):
            widget.bind("<Enter>", _enter)
            widget.bind("<Leave>", _leave)

        # Only lbl_name triggers removal; btn_trim has its own command
        self.lbl_name.bind("<Button-1>", lambda _e, r=self: remove_cb(r))

    def set_status(self, text: str, color: str = MUTED):
        self.lbl_status.config(text=text, fg=color)

    def set_trim_label(self, text: str):
        self.lbl_trim.config(text=text)
        self.btn_trim.config(fg=ACCENT if text else MUTED)

    def destroy(self):
        self.frame.destroy()


# ── main window ───────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video Compressor")
        self.configure(bg=BG)
        self.minsize(620, 540)

        self._rows: list[FileRow] = []
        self._running = False
        self._ui_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._center()
        self._poll_ui_queue()

    def _poll_ui_queue(self):
        try:
            while True:
                fn, args, kwargs = self._ui_queue.get_nowait()
                fn(*args, **kwargs)
        except queue.Empty:
            pass
        self.after(30, self._poll_ui_queue)

    def _ui(self, fn, *args, **kwargs):
        self._ui_queue.put((fn, args, kwargs))

    def _center(self):
        self.update_idletasks()
        w, h = 680, 580
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    # ── layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        pad = dict(padx=16, pady=8)

        hdr = tk.Frame(self, bg=BG)
        hdr.pack(fill="x", **pad)
        tk.Label(hdr, text="Video Compressor", bg=BG, fg=TEXT,
                 font=("Segoe UI", 16, "bold")).pack(side="left")

        drop_outer = tk.Frame(self, bg=ACCENT)
        drop_outer.pack(fill="x", padx=16, pady=(0, 4))

        self.drop_zone = tk.Frame(drop_outer, bg=SURFACE, cursor="hand2")
        self.drop_zone.pack(fill="both", expand=True, padx=2, pady=2)

        self.drop_label = tk.Label(
            self.drop_zone,
            text="+ Click to add videos",
            bg=SURFACE, fg=MUTED, font=("Segoe UI", 12), pady=22,
        )
        self.drop_label.pack(fill="x")

        for w in (self.drop_zone, self.drop_label):
            w.bind("<Button-1>", lambda _e: self._browse_files())
            w.bind("<Enter>", lambda _e: self.drop_zone.config(bg="#333352") or
                   self.drop_label.config(bg="#333352"))
            w.bind("<Leave>", lambda _e: self.drop_zone.config(bg=SURFACE) or
                   self.drop_label.config(bg=SURFACE))

        list_frame = tk.Frame(self, bg=BG)
        list_frame.pack(fill="both", expand=True, padx=16, pady=4)

        list_header = tk.Frame(list_frame, bg=BG)
        list_header.pack(fill="x")
        tk.Label(list_header, text="Files  (click filename to remove · ✂ to trim)",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
        tk.Button(list_header, text="Clear all", bg=BG, fg=MUTED,
                  relief="flat", cursor="hand2", font=("Segoe UI", 9),
                  command=self._clear_all).pack(side="right")

        canvas_frame = tk.Frame(list_frame, bg=SURFACE)
        canvas_frame.pack(fill="both", expand=True, pady=(4, 0))

        self.canvas = tk.Canvas(canvas_frame, bg=SURFACE, highlightthickness=0)
        sb = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=SURFACE)
        self._cwin = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfig(self._cwin, width=e.width))

        settings = tk.Frame(self, bg=BG)
        settings.pack(fill="x", padx=16, pady=6)

        tk.Label(settings, text="Target size (MB):", bg=BG, fg=TEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.size_var = tk.StringVar(value="10")
        tk.Entry(settings, textvariable=self.size_var, width=7,
                 bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=("Segoe UI", 10)).pack(side="left", padx=(4, 20))

        tk.Label(settings, text="Output folder:", bg=BG, fg=TEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.outdir_var = tk.StringVar(value="Same as source")
        tk.Entry(settings, textvariable=self.outdir_var, width=22,
                 bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=("Segoe UI", 10)).pack(side="left", padx=(4, 4))
        tk.Button(settings, text="Browse", bg=ACCENT, fg="white",
                  relief="flat", cursor="hand2", font=("Segoe UI", 9),
                  command=self._browse_outdir).pack(side="left")

        actions = tk.Frame(self, bg=BG)
        actions.pack(fill="x", padx=16, pady=(4, 12))

        self.status_lbl = tk.Label(actions, text="", bg=BG, fg=MUTED,
                                   font=("Segoe UI", 9))
        self.status_lbl.pack(side="left")

        self.compress_btn = tk.Button(
            actions, text="Compress all", bg=ACCENT, fg="white",
            relief="flat", cursor="hand2",
            font=("Segoe UI", 11, "bold"), padx=18, pady=6,
            command=self._start_compression,
        )
        self.compress_btn.pack(side="right")

    # ── file management ───────────────────────────────────────────────────────

    def _browse_files(self):
        paths = filedialog.askopenfilenames(
            title="Select videos",
            filetypes=[
                ("Video files", "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v"),
                ("All files", "*.*"),
            ],
        )
        self._add_files(paths)

    def _add_files(self, paths):
        existing = {r.path for r in self._rows}
        video_exts = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}
        for p in paths:
            p = str(p).strip()
            if p and p not in existing and Path(p).suffix.lower() in video_exts:
                self._rows.append(FileRow(self.inner, p, self._remove_row, self._open_trim))
                existing.add(p)
        self._update_drop_label()

    def _remove_row(self, row: FileRow):
        if self._running:
            return
        if row not in self._rows:
            return
        row.destroy()
        self._rows.remove(row)
        self._update_drop_label()

    def _clear_all(self):
        if self._running:
            return
        for row in list(self._rows):
            row.destroy()
        self._rows.clear()
        self._update_drop_label()

    def _update_drop_label(self):
        n = len(self._rows)
        if n == 0:
            self.drop_label.config(text="+ Click to add videos")
        else:
            self.drop_label.config(text=f"+ Add more  ({n} file{'s' if n != 1 else ''} queued)")

    def _browse_outdir(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self.outdir_var.set(d)

    def _open_trim(self, row: FileRow):
        if self._running:
            return
        TrimDialog(self, row)

    # ── compression ───────────────────────────────────────────────────────────

    def _start_compression(self):
        if not _check_ffmpeg():
            messagebox.showerror(
                "ffmpeg not found",
                "Could not locate ffmpeg. Try rebuilding the app or reinstalling.",
            )
            return

        if not self._rows:
            messagebox.showwarning("No files", "Add at least one video first.")
            return

        try:
            target_mb = float(self.size_var.get())
            if target_mb <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid size", "Enter a positive number for the target size.")
            return

        self._running = True
        self.compress_btn.config(state="disabled", bg=MUTED)

        threading.Thread(
            target=self._run_compression,
            args=(list(self._rows), target_mb, self.outdir_var.get()),
            daemon=True,
        ).start()

    def _run_compression(self, rows, target_mb, outdir_setting):
        total = len(rows)
        errors = []
        try:
            for i, row in enumerate(rows):
                self._ui(self.status_lbl.config,
                         text=f"Compressing {i+1}/{total}: {Path(row.path).name}")
                self._ui(row.set_status, "working…", YELLOW)

                src = Path(row.path)
                out_dir = (src.parent if outdir_setting == "Same as source"
                           else Path(outdir_setting))
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = str(out_dir / (src.stem + "_compressed" + src.suffix))

                ok, result = compress_video(
                    row.path, out_path, target_mb,
                    on_progress=lambda msg, r=row: self._ui(r.set_status, msg, YELLOW),
                    trim_start=row.trim_start,
                    trim_end=row.trim_end,
                )

                if ok:
                    self._ui(row.set_status, result, GREEN)
                else:
                    errors.append((Path(row.path).name, result))
                    self._ui(row.set_status, "error", RED)
        finally:
            self._ui(self._on_done, total, errors)

    def _on_done(self, total, errors):
        self._running = False
        self.compress_btn.config(state="normal", bg=ACCENT)
        if errors:
            self.status_lbl.config(text=f"Done with {len(errors)} error(s).")
            msg = "\n\n".join(f"{name}:\n{err}" for name, err in errors)
            messagebox.showerror("Compression errors", msg)
        else:
            self.status_lbl.config(text=f"Finished {total} file{'s' if total != 1 else ''}.")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
