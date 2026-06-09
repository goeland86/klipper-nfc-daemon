#!/bin/bash
# Install nfc_spoolman on a Klipper host
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME"
VENV_DIR="$INSTALL_DIR/nfc-spoolman-env"
CONFIG_DIR="$INSTALL_DIR/printer_data/config"
SERVICE_NAME="nfc-spoolman"

echo "=== NFC Spoolman Installer ==="
echo "  Repo:    $SCRIPT_DIR"
echo "  Venv:    $VENV_DIR"

# Create venv and install dependencies
echo "Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

# Copy config if it doesn't exist
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/nfc_spoolman.cfg" ]; then
    echo "Installing example config to $CONFIG_DIR/nfc_spoolman.cfg..."
    cp "$SCRIPT_DIR/nfc_spoolman.cfg.example" "$CONFIG_DIR/nfc_spoolman.cfg"
    echo ">>> Edit $CONFIG_DIR/nfc_spoolman.cfg with your Spoolman URL and NFC device <<<"
else
    echo "Config already exists at $CONFIG_DIR/nfc_spoolman.cfg — skipping."
fi

# Create log directory
mkdir -p "$INSTALL_DIR/printer_data/logs"

# Install systemd service — replace placeholders:
#   /home/debian/klipper-nfc-daemon  →  SCRIPT_DIR (repo root, run in-place)
#   /home/debian                     →  INSTALL_DIR (home dir, for venv path)
#   User=debian                      →  current user
echo "Installing systemd service..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
sed "s|User=debian|User=$(whoami)|g; \
     s|/home/debian/klipper-nfc-daemon|$SCRIPT_DIR|g; \
     s|/home/debian|$INSTALL_DIR|g" \
    "$SCRIPT_DIR/nfc-spoolman.service" | sudo tee "$SERVICE_FILE" > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# Add to moonraker.asvc if not already present
ASVC_FILE="$INSTALL_DIR/printer_data/moonraker.asvc"
if [ -f "$ASVC_FILE" ]; then
    if ! grep -q "$SERVICE_NAME" "$ASVC_FILE"; then
        echo "$SERVICE_NAME" >> "$ASVC_FILE"
        echo "Added $SERVICE_NAME to moonraker.asvc (restart Moonraker to pick it up)."
    fi
fi

echo ""
echo "=== Installation complete ==="
echo "  Daemon runs from: $SCRIPT_DIR"
echo "  1. Edit $CONFIG_DIR/nfc_spoolman.cfg"
echo "  2. Start with: sudo systemctl start $SERVICE_NAME"
echo "  3. Check logs: journalctl -u $SERVICE_NAME -f"
echo ""
echo "  To enable update notifications in Mainsail/Fluidd, add to moonraker.conf:"
echo "    [update_manager nfc-spoolman]"
echo "    type: git_repo"
echo "    path: $SCRIPT_DIR"
echo "    origin: https://github.com/goeland86/klipper-nfc-daemon.git"
echo "    primary_branch: main"
echo "    virtualenv: $VENV_DIR"
echo "    requirements: requirements.txt"
echo "    managed_services: $SERVICE_NAME"
