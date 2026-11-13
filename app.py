#!/usr/bin/env python3
"""
LED Matrix Web Controller
Supports: image viewer, multi-image playlist, scrolling text, solid colour, live preview
"""

import glob
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, send_from_directory)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

# ---------------------------------------------------------------------------
# Logging – so we can actually see what is happening
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("led-matrix-web")

BASE_DIR = Path(__file__).parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
PLAYLIST_FOLDER = BASE_DIR / "playlists"
SETTINGS_FILE  = BASE_DIR / "settings.json"
UPLOAD_FOLDER.mkdir(exist_ok=True)
PLAYLIST_FOLDER.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ppm"}

# ---------------------------------------------------------------------------
# Matrix binary paths – auto-discovered, overridable via env var
# ---------------------------------------------------------------------------
def _discover_matrix_utils() -> Path | None:
    """
    Try to locate the rpi-rgb-led-matrix utils directory.

    Search order:
      1. MATRIX_UTILS_DIR environment variable (explicit override)
      2. led-image-viewer on $PATH
      3. Common home-directory clones  (~, /home/*, /root)
      4. /usr/local, /opt, /usr/share
    Returns the *utils* directory or None.
    """
    # 1 – explicit env var
    env_dir = os.environ.get("MATRIX_UTILS_DIR")
    if env_dir:
        p = Path(env_dir)
        if (p / "led-image-viewer").exists():
            log.info("MATRIX_UTILS_DIR from env: %s", p)
            return p
        log.warning("MATRIX_UTILS_DIR=%s but led-image-viewer not found there", p)

    # 2 – on PATH?
    viewer_on_path = shutil.which("led-image-viewer")
    if viewer_on_path:
        p = Path(viewer_on_path).parent
        log.info("Found led-image-viewer on PATH: %s", p)
        return p

    # 3 – common clone locations
    home_candidates = [
        Path.home(),
        Path("/home/pi"),
        Path("/root"),
    ]
    # also pick up any other /home/<user>
    home_candidates += [Path(d) for d in glob.glob("/home/*")]

    search_roots = home_candidates + [Path("/usr/local"), Path("/opt")]
    for root in search_roots:
        if not root.is_dir():
            continue
        # typical: <root>/rpi-rgb-led-matrix/utils/led-image-viewer
        candidate = root / "rpi-rgb-led-matrix" / "utils"
        if (candidate / "led-image-viewer").exists():
            log.info("Discovered matrix utils at: %s", candidate)
            return candidate

    # 4 – slightly deeper search (one level of subdirs under common roots)
    for root in search_roots:
        if not root.is_dir():
            continue
        for subdir in sorted(root.iterdir()):
            if not subdir.is_dir():
                continue
            candidate = subdir / "rpi-rgb-led-matrix" / "utils"
            if (candidate / "led-image-viewer").exists():
                log.info("Discovered matrix utils at: %s", candidate)
                return candidate

    log.warning("Could not auto-discover rpi-rgb-led-matrix utils. "
                "Set MATRIX_UTILS_DIR or add the utils dir to PATH.")
    return None


_discovered = _discover_matrix_utils()
if _discovered:
    MATRIX_UTILS       = _discovered
    LED_IMAGE_VIEWER   = MATRIX_UTILS / "led-image-viewer"
    LED_TEXT           = MATRIX_UTILS / "text-example"
else:
    # fall back to the old default so error messages still make sense
    MATRIX_UTILS       = Path.home() / "rpi-rgb-led-matrix" / "utils"
    LED_IMAGE_VIEWER   = MATRIX_UTILS / "led-image-viewer"
    LED_TEXT           = MATRIX_UTILS / "text-example"

# ---------------------------------------------------------------------------
# Persistent settings (saved to settings.json)
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "led_rows":            "64",
    "led_cols":            "64",
    "led_chain":           "1",
    "led_parallel":        "1",
    "led_brightness":      "80",
    "led_pwm_bits":        "",
    "led_gpio_mapping":    "adafruit-hat",
    "led_slowdown_gpio":   "2",
    "led_pixel_mapper":    "V-mapper",
    "led_scan_mode":       "",
    "led_row_addr_type":   "",
    "led_multiplexing":    "",
    "led_rgb_sequence":    "",
    "led_pwm_lsb_nanoseconds": "",
    "led_show_refresh":    False,
    "led_inverse":         False,
    "led_no_hardware_pulse": False,
}

def load_settings() -> dict:
    """Return saved settings merged over defaults."""
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text()))
        except Exception as e:
            log.warning("Could not read settings.json: %s", e)
    return s

def save_settings(data: dict):
    """Persist a subset of form fields to settings.json."""
    current = load_settings()
    for key in DEFAULT_SETTINGS:
        val = data.get(key)
        if val is not None and val != "":
            # booleans arrive as the string "on" from checkboxes
            if isinstance(DEFAULT_SETTINGS[key], bool):
                current[key] = (val == "on")
            else:
                current[key] = val
    SETTINGS_FILE.write_text(json.dumps(current, indent=2))
    log.info("Settings saved: %s", {k: v for k, v in current.items() if v})

# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------
current_process = None
preview_lock = threading.Lock()
last_preview = None          # holds a small JPEG of the last frame (for live preview)


def allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def kill_current():
    global current_process
    if current_process and current_process.poll() is None:
        log.info("Killing process PID %s", current_process.pid)
        current_process.send_signal(signal.SIGTERM)
        try:
            current_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            current_process.kill()
    current_process = None
    # safety net – skip sudo if we are already root
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    for pattern in ("led-image-viewer", "text-example"):
        subprocess.run(prefix + ["pkill", "-f", pattern],
                       check=False, capture_output=True)


def build_matrix_flags(data=None) -> list:
    """
    Build --led-* CLI flags.

    Priority: form fields (if provided) > saved settings.json > defaults.
    Passing data=None uses only the persisted settings.
    """
    s = load_settings()
    if data is not None:
        # overlay any values that came in with this specific request
        for k in s:
            v = data.get(k)
            if v not in (None, ""):
                s[k] = v

    flags = []
    mapping = {
        "led_rows": "--led-rows",
        "led_cols": "--led-cols",
        "led_chain": "--led-chain",
        "led_parallel": "--led-parallel",
        "led_brightness": "--led-brightness",
        "led_pwm_bits": "--led-pwm-bits",
        "led_gpio_mapping": "--led-gpio-mapping",
        "led_slowdown_gpio": "--led-slowdown-gpio",
        "led_pixel_mapper": "--led-pixel-mapper",
        "led_scan_mode": "--led-scan-mode",
        "led_row_addr_type": "--led-row-addr-type",
        "led_multiplexing": "--led-multiplexing",
        "led_rgb_sequence": "--led-rgb-sequence",
        "led_pwm_lsb_nanoseconds": "--led-pwm-lsb-nanoseconds",
    }
    for key, flag in mapping.items():
        val = s.get(key)
        if val not in (None, ""):
            flags.append(f"{flag}={val}")

    if s.get("led_show_refresh"):
        flags.append("--led-show-refresh")
    if s.get("led_inverse"):
        flags.append("--led-inverse")
    if s.get("led_no_hardware_pulse"):
        flags.append("--led-no-hardware-pulse")
    return flags


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.route("/")
def index():
    files = sorted(f.name for f in UPLOAD_FOLDER.iterdir() if f.is_file())
    playlists = sorted(p.stem for p in PLAYLIST_FOLDER.glob("*.txt"))
    settings = load_settings()
    return render_template("index.html", files=files, playlists=playlists,
                           settings=settings)


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        flash("No file part", "error")
        return redirect(url_for("index"))
    f = request.files["file"]
    if f.filename == "":
        flash("No selected file", "error")
        return redirect(url_for("index"))
    if f and allowed_file(f.filename):
        name = Path(f.filename).name
        f.save(UPLOAD_FOLDER / name)
        flash(f"Uploaded {name}", "success")
    else:
        flash("Unsupported file type", "error")
    return redirect(url_for("index"))


@app.route("/delete/<filename>", methods=["POST"])
def delete(filename):
    path = UPLOAD_FOLDER / Path(filename).name
    if path.exists():
        path.unlink()
        flash(f"Deleted {filename}", "success")
    return redirect(url_for("index"))


@app.route("/play_images", methods=["POST"])
def play_images():
    """Play one or more images (playlist)."""
    global current_process
    data = request.form
    images = request.form.getlist("images")
    if not images:
        flash("No images selected", "error")
        return redirect(url_for("index"))

    # -- validate binary exists ------------------------------------------
    if not LED_IMAGE_VIEWER.exists():
        msg = (f"led-image-viewer not found (searched PATH, ~/rpi-rgb-led-matrix, /home/*, /usr/local, /opt). "
               f"Build rpi-rgb-led-matrix or set MATRIX_UTILS_DIR.")
        log.error(msg)
        flash(msg, "error")
        return redirect(url_for("index"))

    # -- validate every uploaded file actually exists ---------------------
    missing = [i for i in images if not (UPLOAD_FOLDER / i).exists()]
    if missing:
        flash(f"File(s) not found on disk: {', '.join(missing)}", "error")
        return redirect(url_for("index"))

    # -- build command ----------------------------------------------------
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    cmd = prefix + [str(LED_IMAGE_VIEWER)]
    cmd += build_matrix_flags(data)

    if data.get("center"):
        cmd.append("-C")
    if data.get("wait"):
        cmd += [f"-w{data['wait']}"]
    if data.get("loop"):
        cmd += [f"-l{data['loop']}"]
    if data.get("delay") and data["delay"] != "-1":
        cmd += [f"-D{data['delay']}"]

    for img in images:
        cmd.append(str(UPLOAD_FOLDER / img))

    log.info("Command: %s", " ".join(cmd))

    kill_current()
    try:
        current_process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # give it a moment to die on startup errors (bad binary, missing libs)
        time.sleep(0.5)
        if current_process.poll() is not None:
            err = (current_process.stderr.read().decode(errors="replace")
                   if current_process.stderr else "")
            log.error("led-image-viewer exited immediately (rc=%s): %s",
                      current_process.returncode, err.strip())
            flash(f"Process died on start: {err.strip()[:300]}", "error")
            current_process = None
        else:
            flash(f"Playing {len(images)} image(s)", "success")
    except Exception as e:
        log.exception("Failed to start led-image-viewer")
        flash(f"Failed: {e}", "error")
    return redirect(url_for("index"))


@app.route("/play_text", methods=["POST"])
def play_text():
    """Scrolling text using text-example."""
    global current_process
    data = request.form
    text = data.get("text", "Hello").strip()
    if not text:
        flash("No text entered", "error")
        return redirect(url_for("index"))

    if not LED_TEXT.exists():
        msg = (f"text-example not found at {LED_TEXT}. "
               f"Rebuild rpi-rgb-led-matrix with utils enabled.")
        log.error(msg)
        flash(msg, "error")
        return redirect(url_for("index"))

    prefix = [] if os.geteuid() == 0 else ["sudo"]
    cmd = prefix + [str(LED_TEXT)]
    cmd += build_matrix_flags(data)
    font_path = MATRIX_UTILS.parent / "fonts" / "9x18.bdf"
    if font_path.exists():
        cmd += ["-f", str(font_path)]
    cmd += ["-C", "255,255,255"]          # white
    cmd.append(text)

    log.info("Command: %s", " ".join(cmd))

    kill_current()
    try:
        current_process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)
        if current_process.poll() is not None:
            err = (current_process.stderr.read().decode(errors="replace")
                   if current_process.stderr else "")
            flash(f"text-example died on start: {err.strip()[:300]}", "error")
            current_process = None
        else:
            flash(f"Scrolling: {text}", "success")
    except Exception as e:
        log.exception("Failed to start text-example")
        flash(f"Failed: {e}", "error")
    return redirect(url_for("index"))


@app.route("/solid", methods=["POST"])
def solid():
    """Fill the whole matrix with a solid colour."""
    global current_process
    data = request.form
    colour = data.get("colour", "#00ff00").lstrip("#")
    try:
        r = int(colour[0:2], 16)
        g = int(colour[2:4], 16)
        b = int(colour[4:6], 16)
    except Exception:
        flash("Invalid colour", "error")
        return redirect(url_for("index"))

    if not LED_IMAGE_VIEWER.exists():
        msg = (f"led-image-viewer not found at {LED_IMAGE_VIEWER}. "
               f"Build rpi-rgb-led-matrix or set MATRIX_UTILS_DIR.")
        log.error(msg)
        flash(msg, "error")
        return redirect(url_for("index"))

    # Generate a tiny 1x1 PNG of that colour and display it stretched
    from PIL import Image
    img = Image.new("RGB", (1, 1), (r, g, b))
    tmp = UPLOAD_FOLDER / "_solid.png"
    img.save(tmp)

    prefix = [] if os.geteuid() == 0 else ["sudo"]
    cmd = prefix + [str(LED_IMAGE_VIEWER)]
    cmd += build_matrix_flags(data)
    cmd.append(str(tmp))

    log.info("Command: %s", " ".join(cmd))

    kill_current()
    try:
        current_process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)
        if current_process.poll() is not None:
            err = (current_process.stderr.read().decode(errors="replace")
                   if current_process.stderr else "")
            flash(f"Process died on start: {err.strip()[:300]}", "error")
            current_process = None
        else:
            flash(f"Solid colour #{colour}", "success")
    except Exception as e:
        log.exception("Failed to start solid colour")
        flash(f"Failed: {e}", "error")
    return redirect(url_for("index"))


@app.route("/stop", methods=["POST"])
def stop():
    kill_current()
    flash("Stopped", "success")
    return redirect(url_for("index"))


@app.route("/save_settings", methods=["POST"])
def save_settings_route():
    """Persist matrix settings so they survive page reloads."""
    data = request.form
    save_settings(data)
    flash("Settings saved", "success")
    return redirect(url_for("index"))


@app.route("/status")
def status():
    running = current_process is not None and current_process.poll() is None
    viewer_ok = LED_IMAGE_VIEWER.exists()
    text_ok   = LED_TEXT.exists()
    return jsonify({
        "running":  running,
        "viewer":   str(LED_IMAGE_VIEWER),
        "viewer_ok": viewer_ok,
        "text_ok":   text_ok,
    })


@app.route("/save_playlist", methods=["POST"])
def save_playlist():
    name = request.form.get("playlist_name", "").strip()
    images = request.form.getlist("images")
    if not name or not images:
        flash("Name and at least one image required", "error")
        return redirect(url_for("index"))
    path = PLAYLIST_FOLDER / f"{name}.txt"
    path.write_text("\n".join(images))
    flash(f"Playlist '{name}' saved", "success")
    return redirect(url_for("index"))


@app.route("/load_playlist/<name>")
def load_playlist(name):
    path = PLAYLIST_FOLDER / f"{name}.txt"
    if not path.exists():
        flash("Playlist not found", "error")
        return redirect(url_for("index"))
    images = path.read_text().splitlines()
    # Re-use the play_images logic by faking a form
    # (simplest is to redirect with query params or just call the same code)
    flash(f"Loaded playlist {name} – select the images and press Play", "success")
    return redirect(url_for("index"))


# ------------------------------------------------------------------
# Live preview – very lightweight
# We keep a small JPEG that the UI can poll.
# (A full framebuffer grab would need extra code; this is a placeholder
#  that shows the last solid colour / a status image.)
# ------------------------------------------------------------------

@app.route("/preview.jpg")
def preview_jpg():
    # For a real live preview you would grab the current matrix frame.
    # Here we return a simple status image so the UI has something to show.
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (320, 180), (20, 20, 30))
    d = ImageDraw.Draw(img)
    status = "RUNNING" if (current_process and current_process.poll() is None) else "IDLE"
    d.text((20, 70), f"Matrix: {status}", fill=(0, 255, 120))
    d.text((20, 100), "Live preview placeholder", fill=(180, 180, 180))
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    buf.seek(0)
    return app.response_class(buf.read(), mimetype="image/jpeg")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
