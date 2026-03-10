# klipper-nfc

A daemon that runs on a Klipper host to automatically select the active Spoolman spool by scanning TigerTag NFC tags on a PN532 reader.

## How it works

1. The daemon polls a PN532 NFC reader connected via UART (e.g. `/dev/ttyUSB0`)
2. When an NTAG213 tag is detected, it reads the TigerTag binary data from the tag's user memory (pages 4-39, 144 bytes)
3. The raw binary is base64-encoded and POSTed to Spoolman's `/api/v1/nfc/lookup` endpoint
4. If Spoolman matches the tag to a spool, the daemon tells Moonraker to set that spool as active via `POST /server/spoolman/spool_id`

Tags are matched in Spoolman by:
- **External ID**: the tag's `id_product` field is matched against filaments with `external_id = "tigertag_{id_product}"` (for tags from the TigerTag product database)
- **Spool ID fallback**: if no external ID match, `id_product` is tried as a direct Spoolman spool ID (for tags written by Spoolman itself)

The daemon includes debouncing so the same tag won't re-trigger within a configurable window.

## Requirements

- A Klipper host (Raspberry Pi, BeagleBone, etc.) running Moonraker
- A **PN532 NFC reader** connected via UART (USB-UART adapter or GPIO)
- **Spoolman** with the TigerTag/NFC endpoints enabled (`SPOOLMAN_TIGERTAG_ENABLED=TRUE`)
- Python 3.10+
- NTAG213 tags encoded in TigerTag format

## Installation

Clone this repo onto your Klipper host and run the installer:

```bash
git clone <repo-url>
cd klipper-nfc
./install.sh
```

The installer will:
- Create a Python venv at `~/nfc-spoolman-env` with `pyserial` and `requests`
- Copy `nfc_spoolman.py` to your home directory
- Copy the example config to `~/printer_data/config/nfc_spoolman.cfg` (if it doesn't already exist)
- Install and enable a systemd service (`nfc-spoolman`)
- Add the service to `moonraker.asvc` so it appears in Mainsail/Fluidd

## Configuration

Edit `~/printer_data/config/nfc_spoolman.cfg`:

```ini
[nfc]
# Serial device and baud rate for the PN532 reader
device = /dev/ttyUSB0:115200

# How often to poll for a tag (seconds)
poll_interval = 0.5

# Ignore the same tag for this many seconds after activation
debounce_time = 5.0

[spoolman]
# URL of your Spoolman instance
url = http://localhost:7912

[moonraker]
# URL of the local Moonraker instance
url = http://localhost:7125
```

## Usage

```bash
# Start the service
sudo systemctl start nfc-spoolman

# Check status
sudo systemctl status nfc-spoolman

# Follow logs
journalctl -u nfc-spoolman -f

# Restart after config changes
sudo systemctl restart nfc-spoolman
```

Logs are also written to `~/printer_data/logs/nfc_spoolman.log`.

## Wiring

The PN532 must be set to UART mode (check the DIP switches or solder jumpers on your board). Connect it to the Klipper host via a USB-UART adapter, or directly to GPIO UART pins. The default expects the device at `/dev/ttyUSB0` at 115200 baud.

You can verify the reader works at the OS level with libnfc:

```bash
sudo apt install libnfc-bin
nfc-list
```
