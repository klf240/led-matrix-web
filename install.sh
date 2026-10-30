#!/bin/bash
set -e
echo "=== LED Matrix Web Controller Installer ==="

if [ ! -f app.py ]; then
  echo "Cloning repository..."
  git clone https://github.com/klf240/led-matrix-web.git
  cd led-matrix-web
fi

echo "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "Installing Python packages..."
pip install --upgrade pip
pip install -r requirements.txt

mkdir -p uploads playlists

echo ""
echo "============================================================"
echo " Installation complete!"
echo "============================================================"
echo ""
echo "To start now:"
echo "  cd $(pwd)"
echo "  source venv/bin/activate"
echo "  sudo venv/bin/python app.py"
echo ""
echo "Then open:  http://$(hostname -I | awk '{print $1}'):8080"
echo ""
echo "To install as a system service:"
echo "  sudo cp led-matrix-web.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now led-matrix-web"
echo ""
