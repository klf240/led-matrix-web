#!/bin/bash
# =============================================================================
# LED Matrix Web Controller – One-shot installer for a fresh Raspberry Pi
#
# Usage:
#   curl -sL https://raw.githubusercontent.com/klf240/led-matrix-web/main/install.sh | bash
#
# Or download and run:
#   wget https://raw.githubusercontent.com/klf240/led-matrix-web/main/install.sh
#   bash install.sh
# =============================================================================
set -euo pipefail

# --- colours for output -----------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }

# --- detect user / paths ----------------------------------------------------
USER_NAME="$(whoami)"
USER_HOME="$(eval echo "~${USER_NAME}")"
APP_DIR="${USER_HOME}/led-matrix-web"
MATRIX_REPO="${USER_HOME}/rgb-led-matrix"
MATRIX_UTILS="${MATRIX_REPO}/utils"

echo ""
echo "============================================================"
echo "  LED Matrix Web Controller – Installer"
echo "  User:   ${USER_NAME}"
echo "  Home:   ${USER_HOME}"
echo "============================================================"
echo ""

# =============================================================================
# Step 1 – System packages
# =============================================================================
info "Installing system packages (git, build tools, Python)..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    git python3 python3-venv python3-pip \
    build-essential autoconf libtool pkg-config \
    libudev-dev 2>/dev/null
ok "System packages installed"

# =============================================================================
# Step 2 – Build rpi-rgb-led-matrix (hardware driver)
# =============================================================================
if [ -x "${MATRIX_UTILS}/led-image-viewer" ]; then
    ok "rpi-rgb-led-matrix already built at ${MATRIX_REPO}"
else
    info "Cloning rpi-rgb-led-matrix..."
    if [ -d "${MATRIX_REPO}" ]; then
        warn "${MATRIX_REPO} exists but has no built binaries – re-cloning"
        sudo rm -rf "${MATRIX_REPO}"
    fi
    git clone --depth 1 https://github.com/hzeller/rpi-rgb-led-matrix.git "${MATRIX_REPO}"

    # Extra deps for the image viewer
    info "Installing matrix build dependencies..."
    sudo apt-get install -y -qq \
        libgraphicsmagick++-dev libwebp-dev 2>/dev/null

    info "Building led-image-viewer (this can take a few minutes)..."
    cd "${MATRIX_REPO}/utils"
    make led-image-viewer -j"$(nproc)"

    info "Building text-scroller..."
    make text-scroller -j"$(nproc)"
    cd "${USER_HOME}"

    # Verify
    [ -x "${MATRIX_UTILS}/led-image-viewer" ] \
        || die "led-image-viewer build failed – check output above"
    ok "rpi-rgb-led-matrix built: ${MATRIX_UTILS}"
fi

# =============================================================================
# Step 3 – Clone / update led-matrix-web
# =============================================================================
if [ -d "${APP_DIR}/.git" ]; then
    info "Updating existing led-matrix-web repo..."
    cd "${APP_DIR}"
    git pull origin main
else
    info "Cloning led-matrix-web..."
    git clone https://github.com/klf240/led-matrix-web.git "${APP_DIR}"
    cd "${APP_DIR}"
fi
ok "led-matrix-web ready at ${APP_DIR}"

# =============================================================================
# Step 4 – Python virtual environment
# =============================================================================
info "Creating Python virtual environment..."
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install --upgrade pip -q
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q
ok "Python dependencies installed"

# Create runtime directories
mkdir -p "${APP_DIR}/uploads" "${APP_DIR}/playlists"

# =============================================================================
# Step 4b – Passwordless sudo for matrix utilities
# =============================================================================
info "Configuring passwordless sudo for matrix binaries..."
SUDOERS_FILE="/etc/sudoers.d/led-matrix"
sudo tee "${SUDOERS_FILE}" > /dev/null <<EOF
# Allow ${USER_NAME} to run LED matrix utilities without password
${USER_NAME} ALL=(root) NOPASSWD: ${MATRIX_UTILS}/led-image-viewer, ${MATRIX_UTILS}/text-scroller
EOF
sudo chmod 0440 "${SUDOERS_FILE}"
ok "Sudoers rule installed (${SUDOERS_FILE})"

# =============================================================================
# Step 5 – systemd service
# =============================================================================
info "Installing systemd service..."

# Generate the service file with correct paths for THIS user
SERVICE_FILE="/etc/systemd/system/led-matrix-web.service"
sudo tee "${SERVICE_FILE}" > /dev/null <<EOF
[Unit]
Description=LED Matrix Web Controller
After=network.target

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=MATRIX_UTILS_DIR=${MATRIX_UTILS}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/app.py
Restart=always
RestartSec=5

# Give the process access to the matrix hardware
DeviceAllow=/dev/gpiomem rw
DeviceAllow=/dev/mem rw

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable led-matrix-web
ok "Service installed and enabled"

# =============================================================================
# Step 6 – Start & verify
# =============================================================================
info "Starting led-matrix-web..."
sudo systemctl restart led-matrix-web
sleep 2

if sudo systemctl is-active --quiet led-matrix-web; then
    ok "Service is running"
else
    warn "Service may have failed – check: journalctl -u led-matrix-web -n 30"
fi

# Grab the Pi's IP for the user
PI_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "${PI_IP}" ] && PI_IP="<pi-ip>"

echo ""
echo "============================================================"
echo " ✅  Installation complete!"
echo "============================================================"
echo ""
echo "  Open in your browser:"
echo "    http://${PI_IP}:8080"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status led-matrix-web     # check status"
echo "    journalctl -u led-matrix-web -f          # live logs"
echo "    sudo systemctl restart led-matrix-web    # restart"
echo "    sudo systemctl disable --now led-matrix-web  # stop & disable"
echo ""
if [ ! -x "${MATRIX_UTILS}/led-image-viewer" ]; then
    echo "  ⚠️  Matrix driver NOT found – the web UI will show an error."
echo "     Build it manually or set MATRIX_UTILS_DIR in the service file."
    echo ""
fi
echo "============================================================"
echo ""
