#!/bin/bash
# Install nfc_spoolman on a Klipper host
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME"
VENV_DIR="$INSTALL_DIR/nfc-spoolman-env"
CONFIG_DIR="$INSTALL_DIR/printer_data/config"
SERVICE_NAME="nfc-spoolman"

echo "=== NFC Spoolman Installer ==="

# Create venv and install dependencies
echo "Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install pyserial requests

# Copy script
echo "Installing nfc_spoolman.py..."
cp "$SCRIPT_DIR/nfc_spoolman.py" "$INSTALL_DIR/nfc_spoolman.py"

# Copy config if it doesn't exist
if [ ! -f "$CONFIG_DIR/nfc_spoolman.cfg" ]; then
    echo "Installing example config to $CONFIG_DIR/nfc_spoolman.cfg..."
    cp "$SCRIPT_DIR/nfc_spoolman.cfg.example" "$CONFIG_DIR/nfc_spoolman.cfg"
    echo ">>> Edit $CONFIG_DIR/nfc_spoolman.cfg with your Spoolman URL and NFC device <<<"
else
    echo "Config already exists at $CONFIG_DIR/nfc_spoolman.cfg — skipping."
fi

# Create log directory
mkdir -p "$INSTALL_DIR/printer_data/logs"

# Install systemd service
echo "Installing systemd service..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
sed "s|User=debian|User=$(whoami)|g; s|/home/debian|$INSTALL_DIR|g" \
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
echo "  1. Edit $CONFIG_DIR/nfc_spoolman.cfg"
echo "  2. Start with: sudo systemctl start $SERVICE_NAME"
echo "  3. Check logs: journalctl -u $SERVICE_NAME -f"
