#!/usr/bin/env python3
"""
LED Matrix Web Controller
Supports: image viewer, multi-image playlist, scrolling text, solid color, live preview
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
PLAYLIST_STATE_FILE = BASE_DIR / "playlist_state.json"
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
    LED_TEXT           = MATRIX_UTILS / "text-scroller"
else:
    # fall back to the old default so error messages still make sense
    MATRIX_UTILS       = Path.home() / "rpi-rgb-led-matrix" / "utils"
    LED_IMAGE_VIEWER   = MATRIX_UTILS / "led-image-viewer"
    LED_TEXT           = MATRIX_UTILS / "text-scroller"

# ---------------------------------------------------------------------------
# Persistent settings (saved to settings.json)
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    # --- Matrix hardware settings (saved via "Save Settings" button) ---
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
    # --- Action defaults (auto-saved on each play/solid/text action) ---
    "solid_color":         "#00ff00",
    "center":              True,
    "wait":                "4",
    "loop":                "0",
    "text":                "Hello from the Pi!",
    "speed":               "15",
    "direction":           "right-left",
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
            # booleans arrive as the string "on" or "1" from checkboxes
            if isinstance(DEFAULT_SETTINGS[key], bool):
                current[key] = (val in ("on", "1", True))
            else:
                current[key] = val
    SETTINGS_FILE.write_text(json.dumps(current, indent=2))
    log.info("Settings saved: %s", {k: v for k, v in current.items() if v})


def _load_playlist_state() -> dict:
    """Load saved image selections from disk."""
    if PLAYLIST_STATE_FILE.exists():
        try:
            return json.loads(PLAYLIST_STATE_FILE.read_text())
        except Exception:
            pass
    return {"selected": {}, "center": True}


def _save_playlist_state(data):
    """Save current image selections to disk for persistence."""
    all_files = sorted(f.name for f in UPLOAD_FOLDER.iterdir() if f.is_file() and not f.name.startswith("_"))
    selected = {}
    for i, fname in enumerate(all_files):
        mode = data.get(f"img_mode_{i}", "delay")
        val = data.get(f"img_val_{i}", "4")
        is_checked = fname in data.getlist("images")
        if is_checked:
            selected[fname] = {"mode": mode, "val": val}
    state = {
        "selected": selected,
        "center": bool(data.get("center")),
    }
    PLAYLIST_STATE_FILE.write_text(json.dumps(state, indent=2))


def _persist_action(data):
    """
    Auto-save action-specific form fields to settings.json.
    Called at the start of play_images / play_text / solid so that
    the user's last-used values survive a reboot.
    """
    # request.form is an ImmutableMultiDict – make a mutable copy
    d = dict(data)
    if "color" in d:
        d["solid_color"] = d["color"]
    save_settings(d)

# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------
current_process = None
preview_lock = threading.Lock()
last_preview = None          # holds a small JPEG of the last frame (for live preview)

# ---------------------------------------------------------------------------
# Display state – tracks what is currently on the matrix for live preview
# ---------------------------------------------------------------------------
display_state = {
    "mode": "idle",       # idle | solid | image | text
    "color": (0, 0, 0),   # for solid mode
    "images": [],         # list of file paths for image mode
    "wait": 4.0,          # seconds between images
    "loop": 0,            # 0 = forever
    "center": True,
    "text": "",
    "speed": 15,
    "direction": "right-left",
    "start_time": None,   # time.time() when display started
}


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
    for pattern in ("led-image-viewer", "text-scroller"):
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

def _autostart_enabled() -> bool:
    """Check if the systemd service is enabled (starts at boot)."""
    try:
        r = subprocess.run(
            ["systemctl", "is-enabled", "led-matrix-web"],
            capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "enabled"
    except Exception:
        return False


@app.route("/")
def index():
    files = sorted(f.name for f in UPLOAD_FOLDER.iterdir() if f.is_file() and not f.name.startswith("_"))
    streams = sorted(p.name for p in PLAYLIST_FOLDER.glob("*.stream"))
    playlists = sorted(p.stem for p in PLAYLIST_FOLDER.glob("*.txt"))
    settings = load_settings()
    autostart = _autostart_enabled()
    playlist_state = _load_playlist_state()
    return render_template("index.html", files=files, playlists=playlists,
                           streams=streams, settings=settings,
                           autostart_enabled=autostart,
                           playlist_state=playlist_state)


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
    _persist_action(data)
    _save_playlist_state(data)
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

    # -- build command with per-image positional flags ----------------------
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    cmd = prefix + [str(LED_IMAGE_VIEWER)]
    cmd += build_matrix_flags(data)

    if data.get("center"):
        cmd.append("-C")

    # -f keeps the process alive (cycle forever through provided images)
    cmd.append("-f")

    # Per-image flags: -w<sec> for delay, -l<n> for animation loops
    # Indices in form fields refer to position in the FULL file list,
    # so we must look up each selected image's original index.
    all_files = sorted(f.name for f in UPLOAD_FOLDER.iterdir() if f.is_file() and not f.name.startswith("_"))
    for img in images:
        try:
            idx = all_files.index(img)
        except ValueError:
            idx = 0
        mode = data.get(f"img_mode_{idx}", "delay")
        val = data.get(f"img_val_{idx}", "4")
        if mode == "loops":
            cmd.append(f"-l{val}")
        else:
            cmd.append(f"-w{val}")
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
            display_state.update({
                "mode": "image",
                "images": [str(UPLOAD_FOLDER / i) for i in images],
                "wait": float(data.get("wait", 4) or 4),
                "loop": int(data.get("loop", 0) or 0),
                "center": bool(data.get("center")),
                "start_time": time.time(),
            })
            flash(f"Playing {len(images)} image(s)", "success")
    except Exception as e:
        log.exception("Failed to start led-image-viewer")
        flash(f"Failed: {e}", "error")
    return redirect(url_for("index"))


@app.route("/play_text", methods=["POST"])
def play_text():
    """Scrolling text using text-scroller."""
    global current_process
    data = request.form
    _persist_action(data)
    text = data.get("text", "Hello").strip()
    if not text:
        flash("No text entered", "error")
        return redirect(url_for("index"))

    if not LED_TEXT.exists():
        msg = (f"text-scroller not found at {LED_TEXT}. "
               f"Rebuild rpi-rgb-led-matrix with utils enabled.")
        log.error(msg)
        flash(msg, "error")
        return redirect(url_for("index"))

    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    cmd = prefix + [str(LED_TEXT)]
    cmd += build_matrix_flags(data)
    font_path = MATRIX_UTILS.parent / "fonts" / "9x18.bdf"
    if font_path.exists():
        cmd += ["-f", str(font_path)]
    cmd += ["-C", "255,255,255"]          # white

    # Speed & direction → -s flag
    #   positive = right→left, negative = left→right, 0 = static
    try:
        speed = int(data.get("speed", "15"))
    except (ValueError, TypeError):
        speed = 15
    direction = data.get("direction", "right-left")
    if direction == "left-right":
        speed = -speed
    elif direction == "static":
        speed = 0
    cmd += ["-s", str(speed)]

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
            flash(f"text-scroller died on start: {err.strip()[:300]}", "error")
            current_process = None
        else:
            display_state.update({
                "mode": "text",
                "text": text,
                "speed": abs(speed),
                "direction": direction,
                "start_time": time.time(),
            })
            flash(f"Scrolling: {text}", "success")
    except Exception as e:
        log.exception("Failed to start text-scroller")
        flash(f"Failed: {e}", "error")
    return redirect(url_for("index"))


@app.route("/solid", methods=["POST"])
def solid():
    """Fill the whole matrix with a solid color."""
    global current_process
    data = request.form
    _persist_action(data)
    color = data.get("color", "#00ff00").lstrip("#")
    try:
        r = int(color[0:2], 16)
        g = int(color[2:4], 16)
        b = int(color[4:6], 16)
    except Exception:
        flash("Invalid color", "error")
        return redirect(url_for("index"))

    if not LED_IMAGE_VIEWER.exists():
        msg = (f"led-image-viewer not found at {LED_IMAGE_VIEWER}. "
               f"Build rpi-rgb-led-matrix or set MATRIX_UTILS_DIR.")
        log.error(msg)
        flash(msg, "error")
        return redirect(url_for("index"))

    # Generate a tiny 1x1 PNG of that color and display it stretched
    from PIL import Image
    img = Image.new("RGB", (1, 1), (r, g, b))
    tmp = UPLOAD_FOLDER / "_solid.png"
    img.save(tmp)

    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    cmd = prefix + [str(LED_IMAGE_VIEWER)]
    cmd += build_matrix_flags(data)
    cmd.append("-f")  # keep displaying forever (single image loop)
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
            display_state.update({
                "mode": "solid",
                "color": (r, g, b),
                "start_time": time.time(),
            })
            flash(f"Solid color #{color}", "success")
    except Exception as e:
        log.exception("Failed to start solid color")
        flash(f"Failed: {e}", "error")
    return redirect(url_for("index"))


@app.route("/stop", methods=["POST"])
def stop():
    kill_current()
    display_state.update({"mode": "idle", "start_time": None})
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

    # Check if we can actually run the viewer (sudo works without password)
    sudo_ok = True
    if viewer_ok and os.geteuid() != 0:
        test = subprocess.run(
            ["sudo", "-n", str(LED_IMAGE_VIEWER), "--help"],
            capture_output=True, timeout=5)
        # If sudo itself failed, stderr will say "password is required"
        # or "a terminal is required". Any other output means sudo worked.
        err = test.stderr.decode(errors="replace").lower()
        sudo_ok = ("password is required" not in err
                   and "a terminal is required" not in err
                   and "no askpass program" not in err)

    return jsonify({
        "running":   running,
        "viewer":    str(LED_IMAGE_VIEWER),
        "viewer_ok": viewer_ok,
        "text_ok":   text_ok,
        "sudo_ok":   sudo_ok,
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


@app.route("/play_stream", methods=["POST"])
def play_stream():
    """Play a pre-rendered stream file on the matrix."""
    global current_process
    data = request.form
    _persist_action(data)
    streams = data.getlist("streams")
    if not streams:
        flash("No stream selected", "error")
        return redirect(url_for("index"))

    if not LED_IMAGE_VIEWER.exists():
        flash("led-image-viewer not found", "error")
        return redirect(url_for("index"))

    # Build command for first selected stream (or cycle through multiple)
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    cmd = prefix + [str(LED_IMAGE_VIEWER)]
    cmd += build_matrix_flags(data)

    if data.get("center"):
        cmd.append("-C")
    cmd.append("-f")  # cycle forever through provided streams

    for stream_name in streams:
        # Loop count control per stream
        s_idx = streams.index(stream_name)
        loops = data.get(f"stream_loops_{s_idx}", "0")
        if loops and loops != "0":
            cmd.append(f"-l{loops}")
        cmd.append(str(PLAYLIST_FOLDER / stream_name))

    log.info("Command: %s", " ".join(cmd))
    kill_current()
    try:
        current_process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)
        if current_process.poll() is not None:
            err = (current_process.stderr.read().decode(errors="replace")
                   if current_process.stderr else "")
            flash(f"Stream playback failed: {err.strip()[:300]}", "error")
            current_process = None
        else:
            display_state.update({
                "mode": "image",
                "images": [str(PLAYLIST_FOLDER / s) for s in streams],
                "wait": 1.0,
                "center": bool(data.get("center")),
                "start_time": time.time(),
            })
            flash(f"Playing {len(streams)} stream(s)", "success")
    except Exception as e:
        log.exception("Failed to start stream playback")
        flash(f"Failed: {e}", "error")
    return redirect(url_for("index"))


@app.route("/create_stream", methods=["POST"])
def create_stream():
    """Create a pre-rendered stream file from selected images for fast playback."""
    data = request.form
    images = data.getlist("images")
    if not images:
        flash("No images selected", "error")
        return redirect(url_for("index"))

    name = data.get("stream_name", "").strip() or "playlist"
    # Sanitize name
    name = "".join(c for c in name if c.isalnum() or c in "-_").lower() or "playlist"
    out_path = PLAYLIST_FOLDER / f"{name}.stream"

    if not LED_IMAGE_VIEWER.exists():
        flash("led-image-viewer not found – cannot create stream", "error")
        return redirect(url_for("index"))

    # Build command: led-image-viewer -O<output> [matrix flags] [per-image flags] <images>
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    cmd = prefix + [str(LED_IMAGE_VIEWER), f"-O{out_path}"]
    cmd += build_matrix_flags(data)

    all_files = sorted(f.name for f in UPLOAD_FOLDER.iterdir() if f.is_file() and not f.name.startswith("_"))
    for img in images:
        try:
            idx = all_files.index(img)
        except ValueError:
            idx = 0
        mode = data.get(f"img_mode_{idx}", "delay")
        val = data.get(f"img_val_{idx}", "4")
        if mode == "loops":
            cmd.append(f"-l{val}")
        else:
            cmd.append(f"-w{val}")
        cmd.append(str(UPLOAD_FOLDER / img))

    log.info("Creating stream: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode == 0:
            size_kb = out_path.stat().st_size // 1024
            flash(f"Stream '{name}.stream' created ({size_kb} KB)", "success")
        else:
            err = result.stderr.decode(errors="replace").strip()[:300]
            flash(f"Stream creation failed: {err}", "error")
    except subprocess.TimeoutExpired:
        flash("Stream creation timed out (120s limit)", "error")
    except Exception as e:
        log.exception("Stream creation failed")
        flash(f"Failed: {e}", "error")
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


@app.route("/shutdown", methods=["POST"])
def shutdown():
    """Properly shut down the Raspberry Pi."""
    log.info("Shutdown requested via web UI")
    def _do_shutdown():
        time.sleep(1)
        if os.geteuid() == 0:
            subprocess.run(["shutdown", "-h", "now"], check=False)
        else:
            subprocess.run(["sudo", "-n", "shutdown", "-h", "now"], check=False)
    t = threading.Thread(target=_do_shutdown, daemon=True)
    t.start()
    return "Shutting down Pi... You can close this tab."


@app.route("/toggle_autostart", methods=["POST"])
def toggle_autostart():
    """Enable or disable the service at boot."""
    enabled = request.form.get("autostart") == "on"
    action = "enable" if enabled else "disable"
    log.info("Autostart %s", action)
    try:
        prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
        subprocess.run(prefix + ["systemctl", action, "led-matrix-web"],
                       capture_output=True, timeout=10, check=True)
        msg = f"Server will {'start' if enabled else 'NOT start'} at boot"
        flash(msg, "success")
    except Exception as e:
        log.error("Failed to %s service: %s", action, e)
        flash(f"Failed: {e}", "error")
    return redirect(url_for("index"))


# ------------------------------------------------------------------
# Live preview – very lightweight
# We keep a small JPEG that the UI can poll.
# (A full framebuffer grab would need extra code; this is a placeholder
#  that shows the last solid color / a status image.)
# ------------------------------------------------------------------

@app.route("/preview.jpg")
def preview_jpg():
    """
    Render a live preview of what is currently on the LED matrix.
    Uses PIL to approximate the display at 4x scale for visibility.
    """
    import io
    from PIL import Image, ImageDraw, ImageFont

    # Matrix dimensions from saved settings
    s = load_settings()
    rows = int(s.get("led_rows", 64) or 64)
    cols = int(s.get("led_cols", 64) or 64)
    chain = int(s.get("led_chain", 1) or 1)
    total_cols = cols * chain

    # Scale factor for preview (make it visible in browser)
    SCALE = 4
    pw, ph = total_cols * SCALE, rows * SCALE

    with preview_lock:
        mode = display_state["mode"]
        running = current_process is not None and current_process.poll() is None

        if not running or mode == "idle":
            # Idle screen
            img = Image.new("RGB", (pw, ph), (15, 15, 25))
            d = ImageDraw.Draw(img)
            d.text((10, ph // 2 - 10), "IDLE – no display active",
                   fill=(100, 100, 120))
        elif mode == "solid":
            r, g, b = display_state["color"]
            img = Image.new("RGB", (pw, ph), (r, g, b))
        elif mode == "image":
            img = _render_image_preview(pw, ph, total_cols, rows)
        elif mode == "text":
            img = _render_text_preview(pw, ph, total_cols, rows)
        else:
            img = Image.new("RGB", (pw, ph), (15, 15, 25))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    buf.seek(0)
    return app.response_class(buf.read(), mimetype="image/jpeg",
                              headers={"Cache-Control": "no-store"})


def _render_image_preview(pw, ph, total_cols, rows):
    """Render the current image in the playlist based on elapsed time."""
    from PIL import Image as PILImage

    images = display_state["images"]
    wait = display_state["wait"]
    center = display_state["center"]
    start = display_state["start_time"] or time.time()

    if not images:
        return PILImage.new("RGB", (pw, ph), (0, 0, 0))

    # Determine which image is currently showing.
    # led-image-viewer has ~1s startup before first frame is visible.
    STARTUP_OFFSET = 1.0
    elapsed = max(0, time.time() - start - STARTUP_OFFSET)
    idx = int(elapsed / wait) % len(images)
    path = images[idx]

    try:
        src = PILImage.open(path).convert("RGB")
    except Exception as e:
        log.warning("Preview: cannot open %s: %s", path, e)
        return PILImage.new("RGB", (pw, ph), (40, 0, 0))

    # Scale to fit matrix area
    if center:
        # Fit within matrix bounds, centered
        src = src.thumbnail((total_cols * 4, rows * 4), PILImage.LANCZOS)
        canvas = PILImage.new("RGB", (pw, ph), (0, 0, 0))
        x = (pw - src.width) // 2
        y = (ph - src.height) // 2
        canvas.paste(src, (x, y))
        return canvas
    else:
        # Stretch to fill
        return src.resize((pw, ph), PILImage.LANCZOS)


def _render_text_preview(pw, ph, total_cols, rows):
    """Render scrolling text with animated position based on elapsed time."""
    from PIL import Image as PILImage, ImageDraw, ImageFont

    text = display_state["text"] or ""
    speed = display_state["speed"] or 15
    direction = display_state["direction"]
    start = display_state["start_time"] or time.time()

    img = PILImage.new("RGB", (pw, ph), (0, 0, 0))
    d = ImageDraw.Draw(img)

    if not text:
        return img

    # Try to load a font that matches the matrix size
    font = None
    font_path = MATRIX_UTILS.parent / "fonts" / "9x18.bdf"
    # BDF fonts can't be loaded by PIL directly – use a bitmap-style approach
    # Scale: each "matrix pixel" is SCALE px in preview
    scale = 4
    try:
        font = ImageFont.load_default(size=rows * scale // 2)
    except Exception:
        font = ImageFont.load_default()

    # Calculate text width and position
    bbox = d.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_y = (ph - text_h) // 2

    # Animate x position based on elapsed time and speed
    # speed = chars/sec, so pixels/sec ≈ speed * (text_w / len(text))
    if text:
        px_per_sec = speed * (text_w / max(len(text), 1))
    else:
        px_per_sec = 0

    elapsed = time.time() - start
    offset = int(elapsed * px_per_sec)

    if direction == "right-left":
        # Text starts off-screen right, scrolls left
        x = pw - (offset % (pw + text_w))
    elif direction == "left-right":
        # Text starts off-screen left, scrolls right
        x = -text_w + (offset % (pw + text_w))
    else:  # static
        x = (pw - text_w) // 2

    d.text((x, text_y), text, fill=(255, 255, 255), font=font)
    return img


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
