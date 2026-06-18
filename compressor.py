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
        capture_output=True, text=True,
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", result.stderr)
    if not m:
        raise ValueError("Could not read video duration")
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def compress_video(input_path: str, output_path: str, target_mb: float,
                   on_progress=None) -> tuple[bool, str]:
    """Compress video to at most target_mb. Returns (True, size_str) or (False, error_msg)."""
    tmpdir = None
    try:
        ffmpeg = _get_ffmpeg_exe()
        duration = _get_duration(input_path)
        if duration <= 0:
            return False, "Could not read video duration."

        audio_kbps = 128
        target_bytes = int(target_mb * 1024 * 1024)

        # Start at 90% of budget — container/muxer overhead eats the rest
        video_kbps = max(50, (target_mb * 0.90 * 1024 * 8 / duration) - audio_kbps)

        tmpdir = tempfile.mkdtemp(prefix="vcmp_")
        log_prefix = os.path.join(tmpdir, "ffmpeg2pass")
        null_out = "/dev/null" if os.name != "nt" else "NUL"

        def run(args):
            proc = subprocess.run(args, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr[-800:])

        def two_pass(label):
            if on_progress:
                on_progress(f"{label} 1/2…")
            run([
                ffmpeg, "-y", "-i", input_path,
                "-c:v", "libx264", "-b:v", f"{video_kbps:.0f}k",
                "-pass", "1", "-passlogfile", log_prefix,
                "-an", "-f", "null", null_out,
            ])
            if on_progress:
                on_progress(f"{label} 2/2…")
            run([
                ffmpeg, "-y", "-i", input_path,
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


# ── file row widget ───────────────────────────────────────────────────────────

class FileRow:
    def __init__(self, parent: tk.Frame, path: str, remove_cb):
        self.path = path

        self.frame = tk.Frame(parent, bg=SURFACE, pady=4, padx=8, cursor="hand2")
        self.frame.pack(fill="x", pady=2)

        self.lbl_name = tk.Label(
            self.frame, text=Path(path).name, bg=SURFACE, fg=TEXT,
            font=("Segoe UI", 10), anchor="w", cursor="hand2",
        )
        self.lbl_name.pack(side="left", fill="x", expand=True)

        self.lbl_status = tk.Label(
            self.frame, text="queued", bg=SURFACE, fg=MUTED,
            font=("Segoe UI", 9), width=18, anchor="e", cursor="hand2",
        )
        self.lbl_status.pack(side="right")

        # Bind the whole row (frame + both labels) so clicking anywhere removes it
        for widget in (self.frame, self.lbl_name, self.lbl_status):
            widget.bind("<Button-1>", lambda _e, r=self: remove_cb(r))
            widget.bind("<Enter>", lambda _e: self.frame.config(bg="#333352") or
                        self.lbl_name.config(bg="#333352") or
                        self.lbl_status.config(bg="#333352"))
            widget.bind("<Leave>", lambda _e: self.frame.config(bg=SURFACE) or
                        self.lbl_name.config(bg=SURFACE) or
                        self.lbl_status.config(bg=SURFACE))

    def set_status(self, text: str, color: str = MUTED):
        self.lbl_status.config(text=text, fg=color)

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
        """Drain the queue on the main thread — the safe way to update UI from threads."""
        try:
            while True:
                fn, args, kwargs = self._ui_queue.get_nowait()
                fn(*args, **kwargs)
        except queue.Empty:
            pass
        self.after(30, self._poll_ui_queue)

    def _ui(self, fn, *args, **kwargs):
        """Schedule a UI update from any thread."""
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
        tk.Label(list_header, text="Files  (click a file to remove)",
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
                self._rows.append(FileRow(self.inner, p, self._remove_row))
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
