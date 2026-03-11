#!/usr/bin/env python3
"""
nfc_spoolman.py - NFC spool selection daemon for Klipper/Spoolman

Polls an NFC reader for TigerTag or OpenPrintTag spool tags, looks up the
spool in Spoolman, and sets the active spool in Moonraker.

Supports multiple reader backends:
  - pn532:    PN532 over UART (ISO 14443A only — TigerTag)
  - pn5180:   PN5180 over SPI (ISO 14443A + ISO 15693 — TigerTag + OpenPrintTag)
  - acr1552u: ACR1552U over USB/PC/SC (ISO 14443A + ISO 15693 — TigerTag + OpenPrintTag)

Configuration: ~/printer_data/config/nfc_spoolman.cfg
"""

import base64
import configparser
import logging
import os
import sys
import time
import uuid

import requests

from readers.base import NfcReader, TagRead

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.expanduser("~/printer_data/logs/nfc_spoolman.log")
        ),
    ],
)
log = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    config_path = os.path.expanduser(
        "~/printer_data/config/nfc_spoolman.cfg"
    )
    if not os.path.exists(config_path):
        log.error(f"Config file not found: {config_path}")
        sys.exit(1)

    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    return cfg


def create_reader(cfg: configparser.ConfigParser) -> NfcReader:
    """Create the appropriate NFC reader from config."""
    reader_type = cfg.get("nfc", "reader", fallback="pn532").lower()

    if reader_type == "pn532":
        from readers.pn532 import PN532Reader  # noqa: PLC0415

        device_str = cfg.get("nfc", "pn532_device", fallback="/dev/ttyUSB0")
        baudrate = cfg.getint("nfc", "pn532_baudrate", fallback=115200)

        # Backwards compatibility: parse "device = /dev/ttyUSB0:115200"
        if cfg.has_option("nfc", "device") and not cfg.has_option("nfc", "pn532_device"):
            device_str = cfg.get("nfc", "device")
            if ":" in device_str:
                device_str, baud_str = device_str.rsplit(":", 1)
                baudrate = int(baud_str)

        return PN532Reader(port=device_str, baudrate=baudrate)

    if reader_type == "pn5180":
        from readers.pn5180 import PN5180Reader  # noqa: PLC0415

        return PN5180Reader(
            spi_bus=cfg.getint("nfc", "pn5180_spi_bus", fallback=0),
            spi_cs=cfg.getint("nfc", "pn5180_spi_cs", fallback=0),
            busy_pin=cfg.getint("nfc", "pn5180_busy_pin", fallback=25),
            reset_pin=cfg.getint("nfc", "pn5180_reset_pin", fallback=24),
        )

    if reader_type == "acr1552u":
        from readers.acr1552u import ACR1552UReader  # noqa: PLC0415

        return ACR1552UReader(
            reader_name=cfg.get("nfc", "acr1552u_reader_name", fallback="ACS ACR1552U"),
        )

    log.error(f"Unknown reader type: {reader_type}")
    sys.exit(1)


# ── Spoolman ──────────────────────────────────────────────────────────────────

def lookup_spool(spoolman_url: str, tag: TagRead, auto_create: bool = False) -> int | None:
    """POST tag data to Spoolman's /api/v1/nfc/lookup and return matched spool_id."""
    payload = {
        "raw_data_b64": base64.b64encode(tag.data).decode("ascii"),
        "nfc_tag_uid": tag.uid.hex(),
        "auto_create": auto_create,
    }

    # Help Spoolman with tag type detection
    if tag.protocol == "iso15693":
        payload["tag_type"] = "openprinttag"
    elif tag.protocol == "iso14443a":
        payload["tag_type"] = "tigertag"

    try:
        resp = requests.post(
            f"{spoolman_url}/api/v1/nfc/lookup",
            json=payload,
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("success") and data.get("spool_id") is not None:
            tag_fmt = data.get("tag_format", "unknown")
            log.info(f"Spoolman: matched spool ID {data['spool_id']} (format: {tag_fmt})")
            return int(data["spool_id"])

        log.warning(f"Spoolman lookup: {data.get('message', 'no match')}")
        return None

    except requests.RequestException as e:
        log.error(f"Spoolman request failed: {e}")
        return None


# ── Spoolman spool details ────────────────────────────────────────────────────

def get_spool_details(spoolman_url: str, spool_id: int) -> dict | None:
    """Fetch full spool details from Spoolman (filament, vendor, temps, etc.)."""
    try:
        resp = requests.get(
            f"{spoolman_url}/api/v1/spool/{spool_id}",
            timeout=5,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.error(f"Spoolman spool detail request failed: {e}")
        return None


# ── Moonraker ─────────────────────────────────────────────────────────────────

def set_active_spool(moonraker_url: str, spool_id: int) -> bool:
    """Tell Moonraker which spool is now active."""
    try:
        resp = requests.post(
            f"{moonraker_url}/server/spoolman/spool_id",
            json={"spool_id": spool_id},
            timeout=5,
        )
        resp.raise_for_status()
        log.info(f"Moonraker: active spool set to ID {spool_id}")
        return True

    except requests.RequestException as e:
        log.error(f"Moonraker request failed: {e}")
        return False


# ── Mainsail preset ──────────────────────────────────────────────────────────

# Fixed UUID so we always update the same preset entry rather than creating new ones
MAINSAIL_NFC_PRESET_ID = "nfc-filament-00000000"


def update_mainsail_preset(moonraker_url: str, spool_data: dict) -> bool:
    """Create or update a Mainsail preheat preset from spool filament data.

    Writes to Moonraker's database under the 'mainsail' namespace so Mainsail
    picks it up as a preheat preset (visible in the temperature panel).
    """
    filament = spool_data.get("filament", {})
    vendor = filament.get("vendor", {}) or {}

    extruder_temp = filament.get("settings_extruder_temp")
    bed_temp = filament.get("settings_bed_temp")

    if not extruder_temp and not bed_temp:
        log.debug("Spool has no temperature settings — skipping Mainsail preset")
        return False

    # Build preset name from filament info
    parts = []
    if vendor.get("name"):
        parts.append(vendor["name"])
    if filament.get("material"):
        parts.append(filament["material"])
    if filament.get("name"):
        parts.append(filament["name"])
    preset_name = " ".join(parts) if parts else f"Spool #{spool_data.get('id', '?')}"

    # Build Mainsail preset values
    # Format: {heater_name: {bool: enabled, type: "heater"|"temperature_fan", value: temp}}
    values = {}
    if extruder_temp:
        values["extruder"] = {"bool": True, "type": "heater", "value": int(extruder_temp)}
    if bed_temp:
        values["heater_bed"] = {"bool": True, "type": "heater", "value": int(bed_temp)}

    preset = {
        "name": f"NFC: {preset_name}",
        "gcode": "",
        "values": values,
    }

    try:
        resp = requests.post(
            f"{moonraker_url}/server/database/item",
            json={
                "namespace": "mainsail",
                "key": f"presets.presets.{MAINSAIL_NFC_PRESET_ID}",
                "value": preset,
            },
            timeout=5,
        )
        resp.raise_for_status()
        log.info(f"Mainsail preset updated: '{preset['name']}' "
                 f"(extruder={extruder_temp}, bed={bed_temp})")
        return True

    except requests.RequestException as e:
        log.error(f"Mainsail preset update failed: {e}")
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    cfg = load_config()

    poll_interval = cfg.getfloat("nfc", "poll_interval", fallback=0.5)
    debounce_time = cfg.getfloat("nfc", "debounce_time", fallback=5.0)
    auto_create = cfg.getboolean("nfc", "auto_create", fallback=False)
    mainsail_preset = cfg.getboolean("nfc", "mainsail_preset", fallback=True)
    spoolman_url = cfg.get("spoolman", "url", fallback="http://localhost:7912")
    moonraker_url = cfg.get("moonraker", "url", fallback="http://localhost:7125")

    reader = create_reader(cfg)

    log.info("Starting NFC Spoolman daemon")
    log.info(f"  Reader:        {reader.name()}")
    log.info(f"  Spoolman:      {spoolman_url}")
    log.info(f"  Moonraker:     {moonraker_url}")
    log.info(f"  Poll interval: {poll_interval}s")
    log.info(f"  Debounce:      {debounce_time}s")
    log.info(f"  Auto-create:   {auto_create}")
    log.info(f"  Mainsail preset: {mainsail_preset}")

    last_uid: str | None = None
    last_time: float = 0.0

    while True:
        try:
            if not reader.open():
                log.error(f"{reader.name()}: open failed — retrying in 5s")
                time.sleep(5)
                continue

            log.info(f"{reader.name()}: ready, polling for tags...")

            while True:
                tag = reader.poll(timeout=1.0)
                if tag is None:
                    time.sleep(poll_interval)
                    continue

                uid_hex = tag.uid.hex()
                now = time.monotonic()

                # Debounce
                if uid_hex == last_uid and (now - last_time) < debounce_time:
                    log.debug(f"Debouncing tag {uid_hex}")
                    time.sleep(poll_interval)
                    continue

                log.info(f"Tag detected: UID={uid_hex} protocol={tag.protocol} ({len(tag.data)} bytes)")

                # Look up spool in Spoolman
                spool_id = lookup_spool(spoolman_url, tag, auto_create=auto_create)
                if spool_id is None:
                    log.warning(f"No spool found for tag {uid_hex}")
                    last_uid = uid_hex
                    last_time = now
                    time.sleep(poll_interval)
                    continue

                # Set active spool in Moonraker
                if set_active_spool(moonraker_url, spool_id):
                    last_uid = uid_hex
                    last_time = now

                    # Update Mainsail preheat preset with filament temps
                    if mainsail_preset:
                        spool_data = get_spool_details(spoolman_url, spool_id)
                        if spool_data:
                            update_mainsail_preset(moonraker_url, spool_data)

                time.sleep(poll_interval)

        except KeyboardInterrupt:
            log.info("Shutting down")
            reader.close()
            sys.exit(0)
        except Exception as e:
            log.exception(f"Unexpected error: {e} — retrying in 5s")
            time.sleep(5)
        finally:
            reader.close()


if __name__ == "__main__":
    main()
