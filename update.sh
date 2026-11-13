#!/bin/bash
# =============================================================================
# LED Matrix Web Controller - Smart updater
#
# Usage:
#   ./update.sh              # Auto-detect what needs updating
#   ./update.sh --full       # Force complete reinstall (repair mode)
#   ./update.sh --matrix     # Force rebuild of rpi-rgb-led-matrix only
# =============================================================================
set -euo pipefail

# --- colours ----------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }
step()  { echo -e "\n${CYAN}--- $* ---${NC}"; }

# --- parse args ---------------------------------------------------------------
FULL=false
MATRIX_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --full|-f)   FULL=true ;;
        --matrix|-m) MATRIX_ONLY=true ;;
        --help|-h)
            echo "Usage: $0 [--full|--matrix]"
            echo "  (no flags)   Smart update - only does what is needed"
            echo "  --full, -f   Force complete reinstall (repair broken installs)"
            echo "  --matrix,-m  Rebuild rpi-rgb-led-matrix driver only"
            exit 0
            ;;
        *) die "Unknown option: $arg (see --help)" ;;
    esac
done

# --- paths -------------------------------------------------------------------
USER_NAME="$(whoami)"
USER_HOME="$(eval echo "~${USER_NAME}")"
APP_DIR="${USER_HOME}/led-matrix-web"
MATRIX_REPO="${USER_HOME}/rgb-led-matrix"
MATRIX_UTILS="${MATRIX_REPO}/utils"
SERVICE="led-matrix-web"

SUDO="sudo"
if [ "$(id -u)" = "0" ]; then SUDO=""; fi

echo ""
echo "============================================================"
echo "  LED Matrix Web Controller - Updater"
if $FULL; then echo "  Mode: FULL REINSTALL"; fi
echo "============================================================"

# =============================================================================
# Pre-flight checks
# =============================================================================
step "Pre-flight"

[ -d "${APP_DIR}/.git" ] || die "Repo not found at ${APP_DIR}. Run install.sh first."
cd "${APP_DIR}"
ok "Working in ${APP_DIR}"

# =============================================================================
# Matrix driver
# =============================================================================
NEED_MATRIX=false
if $MATRIX_ONLY; then
    step "Rebuilding rpi-rgb-led-matrix (forced)"
    NEED_MATRIX=true
elif $FULL; then
    step "Rebuilding rpi-rgb-led-matrix (full mode)"
    NEED_MATRIX=true
else
    step "Matrix driver"
    if [ -x "${MATRIX_UTILS}/led-image-viewer" ]; then
        ok "led-image-viewer present - skipping"
    else
        warn "led-image-viewer NOT found - will build"
        NEED_MATRIX=true
    fi
fi

if $NEED_MATRIX; then
    info "Installing build dependencies..."
    ${SUDO} apt-get update -qq
    ${SUDO} apt-get install -y -qq \
        git build-essential autoconf libtool pkg-config \
        libudev-dev libgraphicsmagick++-dev libwebp-dev 2>/dev/null

    if [ -d "${MATRIX_REPO}" ]; then
        info "Removing old build..."
        ${SUDO} rm -rf "${MATRIX_REPO}"
    fi

    info "Cloning hzeller/rpi-rgb-led-matrix..."
    git clone --depth 1 https://github.com/hzeller/rpi-rgb-led-matrix.git "${MATRIX_REPO}"

    info "Building led-image-viewer..."
    make -C "${MATRIX_REPO}/utils" led-image-viewer -j"$(nproc)"

    info "Building text-scroller..."
    make -C "${MATRIX_REPO}/utils" text-scroller -j"$(nproc)"

    [ -x "${MATRIX_UTILS}/led-image-viewer" ] || die "Build failed"
    ok "Matrix driver rebuilt"

    if $MATRIX_ONLY && ! $FULL; then
        echo ""
        ok "Done (matrix only). Restart service if needed:"
        echo "   ${SUDO} systemctl restart ${SERVICE}"
        exit 0
    fi
fi

# =============================================================================
# Code update
# =============================================================================
if $FULL; then
    step "Code update (full mode - hard reset)"
    git fetch origin
    git reset --hard origin/main
    ok "Reset to origin/main"
else
    step "Code update"
    git fetch origin -q
    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse origin/main)

    if [ "$LOCAL" = "$REMOTE" ]; then
        ok "Already up to date"
    else
        info "Updating $(git rev-parse --short $LOCAL) -> $(git rev-parse --short $REMOTE)..."
        git merge --ff-only origin/main -q
        ok "Code updated"
    fi
fi

# =============================================================================
# Python dependencies
# =============================================================================
if $FULL; then
    step "Python environment (full rebuild)"
    rm -rf "${APP_DIR}/venv"
    python3 -m venv "${APP_DIR}/venv"
    "${APP_DIR}/venv/bin/pip" install --upgrade pip -q
    "${APP_DIR}/venv/bin/pip" install -r requirements.txt -q
    ok "Virtual environment rebuilt"
else
    step "Python dependencies"
    if [ ! -d "${APP_DIR}/venv" ]; then
        info "Creating virtual environment..."
        python3 -m venv "${APP_DIR}/venv"
        "${APP_DIR}/venv/bin/pip" install --upgrade pip -q
        "${APP_DIR}/venv/bin/pip" install -r requirements.txt -q
        ok "Venv created"
    else
        if ! "${APP_DIR}/venv/bin/python" -c "import flask, PIL" 2>/dev/null; then
            info "Missing packages - reinstalling..."
            "${APP_DIR}/venv/bin/pip" install -r requirements.txt -q
            ok "Dependencies installed"
        else
            ok "Dependencies OK"
        fi
    fi
fi

# =============================================================================
# Runtime directories
# =============================================================================
mkdir -p "${APP_DIR}/uploads" "${APP_DIR}/playlists"

# =============================================================================
# systemd service
# =============================================================================
step "Service"

SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"
NEED_SERVICE_WRITE=false

if [ ! -f "${SERVICE_FILE}" ]; then
    info "Service file not found - installing..."
    NEED_SERVICE_WRITE=true
elif ! grep -q "MATRIX_UTILS_DIR=${MATRIX_UTILS}" "${SERVICE_FILE}" 2>/dev/null; then
    info "Service file path mismatch - updating..."
    NEED_SERVICE_WRITE=true
fi

if $NEED_SERVICE_WRITE || $FULL; then
    ${SUDO} tee "${SERVICE_FILE}" > /dev/null <<EOF
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

DeviceAllow=/dev/gpiomem rw
DeviceAllow=/dev/mem rw

[Install]
WantedBy=multi-user.target
EOF
    ${SUDO} systemctl daemon-reload
    ${SUDO} systemctl enable "${SERVICE}"
    ok "Service file written"
else
    ok "Service file OK"
fi

# =============================================================================
# Restart & verify
# =============================================================================
step "Restart"

${SUDO} systemctl restart "${SERVICE}"
sleep 2

if ${SUDO} systemctl is-active --quiet "${SERVICE}"; then
    ok "Service is running"
else
    warn "Service may have failed - last 10 log lines:"
    journalctl -u "${SERVICE}" -n 10 --no-pager 2>/dev/null || true
fi

# =============================================================================
# Summary
# =============================================================================
PI_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "${PI_IP}" ] && PI_IP="<pi-ip>"

echo ""
echo "============================================================"
echo " Done! Update complete."
echo "============================================================"
echo ""
echo "  Web UI:    http://${PI_IP}:8080"
echo "  Status:    ${SUDO} systemctl status ${SERVICE}"
echo "  Logs:      journalctl -u ${SERVICE} -f"
echo ""
