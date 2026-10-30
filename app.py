#!/usr/bin/env python3
"""
LED Matrix Web Controller
Supports: image viewer, multi-image playlist, scrolling text, solid colour, live preview
"""

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, send_from_directory)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

BASE_DIR = Path(__file__).parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
PLAYLIST_FOLDER = BASE_DIR / "playlists"
UPLOAD_FOLDER.mkdir(exist_ok=True)
PLAYLIST_FOLDER.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ppm"}

# Adjust this path to where you compiled the utilities
MATRIX_UTILS = Path.home() / "rpi-rgb-led-matrix" / "utils"
LED_IMAGE_VIEWER = MATRIX_UTILS / "led-image-viewer"
LED_TEXT = MATRIX_UTILS / "text-example"          # or led-text if you built it
# Fallback if text-example is not present – we will use a simple Python fallback

current_process = None
preview_lock = threading.Lock()
last_preview = None          # holds a small JPEG of the last frame (for live preview)


def allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def kill_current():
    global current_process
    if current_process and current_process.poll() is None:
        current_process.send_signal(signal.SIGTERM)
        try:
            current_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            current_process.kill()
    current_process = None
    # safety net
    subprocess.run(["sudo", "pkill", "-f", "led-image-viewer"], check=False)
    subprocess.run(["sudo", "pkill", "-f", "text-example"], check=False)


def build_matrix_flags(data) -> list:
    """Convert form data into led-* flags."""
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
        val = data.get(key)
        if val not in (None, ""):
            flags.append(f"{flag}={val}")

    if data.get("led_show_refresh"):
        flags.append("--led-show-refresh")
    if data.get("led_inverse"):
        flags.append("--led-inverse")
    if data.get("led_no_hardware_pulse"):
        flags.append("--led-no-hardware-pulse")
    return flags


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.route("/")
def index():
    files = sorted(f.name for f in UPLOAD_FOLDER.iterdir() if f.is_file())
    playlists = sorted(p.stem for p in PLAYLIST_FOLDER.glob("*.txt"))
    return render_template("index.html", files=files, playlists=playlists)


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

    cmd = ["sudo", str(LED_IMAGE_VIEWER)]
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

    kill_current()
    try:
        current_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        flash(f"Playing {len(images)} image(s)", "success")
    except Exception as e:
        flash(f"Failed: {e}", "error")
    return redirect(url_for("index"))


@app.route("/play_text", methods=["POST"])
def play_text():
    """Scrolling text using text-example (or a simple fallback)."""
    global current_process
    data = request.form
    text = data.get("text", "Hello").strip()
    if not text:
        flash("No text entered", "error")
        return redirect(url_for("index"))

    # Preferred: use the official text-example binary if it exists
    if LED_TEXT.exists():
        cmd = ["sudo", str(LED_TEXT)]
        cmd += build_matrix_flags(data)
        # text-example specific
        cmd += ["-f", str(MATRIX_UTILS.parent / "fonts" / "9x18.bdf")]
        cmd += ["-C", "255,255,255"]          # white
        cmd.append(text)
    else:
        # Very simple fallback using Python + rgbmatrix (optional)
        flash("text-example binary not found – install it or use images", "error")
        return redirect(url_for("index"))

    kill_current()
    try:
        current_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        flash(f"Scrolling: {text}", "success")
    except Exception as e:
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

    # We generate a tiny 1×1 PNG of that colour and display it stretched
    from PIL import Image
    img = Image.new("RGB", (1, 1), (r, g, b))
    tmp = UPLOAD_FOLDER / "_solid.png"
    img.save(tmp)

    cmd = ["sudo", str(LED_IMAGE_VIEWER)]
    cmd += build_matrix_flags(data)
    # force full-screen (no centering)
    cmd.append(str(tmp))

    kill_current()
    try:
        current_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        flash(f"Solid colour #{colour}", "success")
    except Exception as e:
        flash(f"Failed: {e}", "error")
    return redirect(url_for("index"))


@app.route("/stop", methods=["POST"])
def stop():
    kill_current()
    flash("Stopped", "success")
    return redirect(url_for("index"))


@app.route("/status")
def status():
    running = current_process is not None and current_process.poll() is None
    return jsonify({"running": running})


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
