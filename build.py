"""
Build script — produces a single self-contained executable in dist/.

Usage:
    uv run build.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

import imageio_ffmpeg

# ── locate the bundled ffmpeg binary ─────────────────────────────────────────
ffmpeg_src = imageio_ffmpeg.get_ffmpeg_exe()
print(f"ffmpeg source: {ffmpeg_src}")

# Copy it to a temp dir with the plain name "ffmpeg" (or "ffmpeg.exe" on Windows)
# so compressor.py can find it predictably at runtime inside the bundle.
tmpdir = tempfile.mkdtemp(prefix="vcmp_build_")
ext = ".exe" if sys.platform == "win32" else ""
ffmpeg_renamed = os.path.join(tmpdir, f"ffmpeg{ext}")
shutil.copy2(ffmpeg_src, ffmpeg_renamed)
if sys.platform != "win32":
    os.chmod(ffmpeg_renamed, 0o755)

# ── run PyInstaller ───────────────────────────────────────────────────────────
# --onefile       : single executable
# --windowed      : no console window (Windows / macOS)
# --add-binary    : bundle ffmpeg next to the app in _MEIPASS
cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--windowed",
    "--name", "VideoCompressor",
    "--add-binary", f"{ffmpeg_renamed}{os.pathsep}.",
    "--exclude-module", "imageio_ffmpeg",  # not needed at runtime
    "compressor.py",
]

print("Running:", " ".join(cmd))
result = subprocess.run(cmd)

shutil.rmtree(tmpdir, ignore_errors=True)

if result.returncode == 0:
    name = f"VideoCompressor{ext}"
    print(f"\nDone!  Executable: dist/{name}")
else:
    print("\nBuild failed.")
    sys.exit(result.returncode)
