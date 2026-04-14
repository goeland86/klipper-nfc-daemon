# klipper-nfc

A daemon that runs on a Klipper host to automatically select the active Spoolman spool by scanning NFC tags. Supports both TigerTag (NTAG213) and OpenPrintTag (NFC-V / ICODE SLIX2) formats.

## How it works

1. The daemon polls a configured NFC reader for tags
2. When a tag is detected, it reads the raw tag memory and sends it to Spoolman's `/api/v1/nfc/lookup` endpoint
3. Spoolman auto-detects the tag format, decodes it, and matches it to a spool
4. If a match is found, the daemon:
   - Sets the active spool in Moonraker
   - Pushes filament metadata to Klipper via `SAVE_VARIABLE` (for use in macros)
   - Updates a Mainsail preheat preset with the filament's temperatures

Tag format is auto-detected by Spoolman:
- **TigerTag** (ISO 14443A / NTAG213): matched by `id_product` field against filament `external_id`
- **OpenPrintTag** (ISO 15693 / NFC-V): matched by `instance_uuid` derived from the tag's hardware UID

The daemon includes debouncing so the same tag won't re-trigger within a configurable window.

## Supported readers

| Reader | Interface | TigerTag | OpenPrintTag | Dependencies |
|--------|-----------|----------|--------------|--------------|
| **PN532** | UART | Yes | No | `pyserial` |
| **PN5180** | SPI + GPIO | Yes | Yes | `spidev`, `gpiod` or `RPi.GPIO` |
| **ACR1552U** | USB | Yes | Yes | `pyscard`, `pcscd` |

## Requirements

- A Klipper host (Raspberry Pi, BeagleBone, etc.) running Moonraker
- One of the supported NFC readers (see above)
- **Spoolman** with NFC endpoints enabled (`SPOOLMAN_TIGERTAG_ENABLED=TRUE`, `SPOOLMAN_NFC_ENABLED=TRUE`)
- Python 3.10+

## Installation

Clone this repo onto your Klipper host and run the installer:

```bash
git clone <repo-url>
cd klipper-nfc
./install.sh
```

The installer will:
- Create a Python venv at `~/nfc-spoolman-env` with base dependencies (`pyserial`, `requests`)
- Copy the daemon and readers to your home directory
- Copy the example config to `~/printer_data/config/nfc_spoolman.cfg` (if it doesn't already exist)
- Install and enable a systemd service (`nfc-spoolman`)
- Add the service to `moonraker.asvc` so it appears in Mainsail/Fluidd

For PN5180 or ACR1552U readers, install additional dependencies:

```bash
# PN5180 (SPI)
~/nfc-spoolman-env/bin/pip install spidev gpiod

# ACR1552U (USB/PC/SC)
sudo apt install pcscd libpcsclite-dev
~/nfc-spoolman-env/bin/pip install pyscard
```

## Configuration

Edit `~/printer_data/config/nfc_spoolman.cfg`:

```ini
[nfc]
# Reader type: pn532, pn5180, acr1552u
reader = pn532

# PN532 (UART)
pn532_device = /dev/ttyUSB0
pn532_baudrate = 115200

# PN5180 (SPI) — uncomment if using
# pn5180_spi_bus = 0
# pn5180_spi_cs = 0
# pn5180_busy_pin = 25
# pn5180_reset_pin = 24

# ACR1552U (USB) — uncomment if using
# acr1552u_reader_name = ACS ACR1552U

# Common
poll_interval = 0.5
debounce_time = 5.0

# Auto-create spool when scanning an unrecognized OpenPrintTag
auto_create = false

# Update a Mainsail preheat preset with filament temps on spool detect
mainsail_preset = true

# Push filament metadata to Klipper SAVE_VARIABLE (see below)
klipper_variables = true

[spoolman]
url = http://localhost:7912

[moonraker]
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

## Multi-tool mode (StealthChanger, IDEX, etc.)

For printers with multiple tools, set `mode = multi_tool` in the config. Instead of silently setting the global spool, the daemon will show a **KlipperScreen prompt** each time a spool is scanned, letting you assign it to a specific tool.

### Setup

1. Add `[respond]` to your `printer.cfg` (required for the prompt dialog):

   ```ini
   [respond]
   ```

2. Include the NFC macros in your `printer.cfg`:

   ```ini
   [include nfc_macros.cfg]
   ```

   Copy `nfc_macros.cfg` from this repo to `~/printer_data/config/`.

3. Set the mode in `nfc_spoolman.cfg`:

   ```ini
   [nfc]
   mode = multi_tool
   ```

4. Restart the daemon:

   ```bash
   sudo systemctl restart nfc-spoolman
   ```

### How it works

1. You scan a spool on the NFC reader
2. The daemon looks up the spool in Spoolman
3. A KlipperScreen dialog appears: **"Assign Spool: PolyTerra PLA Red"** with buttons **[T0] [T1] [T2]** ...
4. You tap a tool button on the touchscreen
5. The spool is assigned to that tool in Moonraker's Spoolman integration
6. Per-tool filament metadata is saved to Klipper variables (if `[save_variables]` is configured)

The daemon auto-discovers available tools at startup by querying Moonraker — it checks for a `toolchanger` object (klipper_toolchanger plugin) first, then falls back to counting extruder objects.

### Per-tool saved variables

When `[save_variables]` is configured, `NFC_ASSIGN_TOOL` saves per-tool variables with a `nfc_tN_` prefix:

| Variable | Example (T0) | Example (T2) |
|----------|-------------|---------------|
| `nfc_t0_spool_id` | `42` | `nfc_t2_spool_id = 87` |
| `nfc_t0_material` | `"PLA"` | `nfc_t2_material = "ASA"` |
| `nfc_t0_extruder_temp` | `210` | `nfc_t2_extruder_temp = 260` |
| `nfc_t0_bed_temp` | `60` | `nfc_t2_bed_temp = 100` |
| `nfc_t0_vendor` | `"PolyTerra"` | `nfc_t2_vendor = "eSun"` |

Use them in macros:

```ini
[gcode_macro PRINT_START]
gcode:
    {% set svv = printer.save_variables.variables %}
    ; Get temps for the first tool being used
    {% set extruder = svv.nfc_t0_extruder_temp|default(200)|int %}
    {% set bed = svv.nfc_t0_bed_temp|default(60)|int %}
    M140 S{bed}
    M109 S{extruder}
    M190 S{bed}
```

### Useful macros

- `NFC_STATUS` — print current per-tool spool assignments to the console
- `NFC_CANCEL` — dismiss the prompt without assigning
- `_NFC_STATE` — show the pending (just-scanned) spool info

## Klipper integration (single-tool mode)

### Klipper variables (`klipper_variables = true`)

When enabled, the daemon pushes filament metadata to Klipper via `SAVE_VARIABLE` commands. This requires a `[save_variables]` section in your `printer.cfg`:

```ini
[save_variables]
filename: ~/printer_data/config/saved_variables.cfg
```

The following variables are set on each NFC scan:

| Variable | Type | Example |
|----------|------|---------|
| `nfc_spool_id` | int | `42` |
| `nfc_material` | string | `"PLA"` |
| `nfc_extruder_temp` | int | `210` |
| `nfc_bed_temp` | int | `60` |
| `nfc_vendor` | string | `"Rosa3D"` |
| `nfc_filament_name` | string | `"PLA Starter"` |
| `nfc_color_hex` | string | `"ff9724"` |
| `nfc_diameter` | float | `1.75` |

Use them in your `PRINT_START` macro:

```ini
[gcode_macro PRINT_START]
gcode:
  {% set svv = printer.save_variables.variables %}
  {% set extruder = svv.nfc_extruder_temp|default(200)|int %}
  {% set bed = svv.nfc_bed_temp|default(60)|int %}
  M140 S{bed}       ; start bed heating
  M109 S{extruder}  ; wait for extruder
  M190 S{bed}       ; wait for bed
```

### Mainsail preset (`mainsail_preset = true`)

When enabled, the daemon creates/updates a preheat preset named "NFC: Vendor Material Name" in Mainsail. This appears in the temperature panel as a one-click preheat button. The preset is always updated in-place (same ID) so you won't get duplicate entries.

## Wiring

### PN532 (UART)
Set the PN532 to UART mode (DIP switches / solder jumpers). Connect via a USB-UART adapter or directly to GPIO UART pins.

### PN5180 (SPI)
Connect to SPI0 (or your chosen bus) plus two GPIO pins for BUSY and RESET. The PN5180 needs 5V for the RF antenna and 3.3V for logic.

### ACR1552U (USB)
Plug in the USB reader. Ensure `pcscd` is running (`sudo systemctl start pcscd`).

Verify any reader works at the OS level:

```bash
# For PN532 with libnfc
sudo apt install libnfc-bin
nfc-list

# For ACR1552U with pcsc-tools
sudo apt install pcsc-tools
pcsc_scan
```
