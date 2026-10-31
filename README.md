# LED Matrix Web Controller

Web interface for controlling `rpi-rgb-led-matrix` on a Raspberry Pi.

Features:
- Upload and play images
- Multi-image playlists
- Scrolling text
- Solid color fill
- All major `--led-*` options
- systemd service

## Quick start

Run the following:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo venv/bin/python app.py
