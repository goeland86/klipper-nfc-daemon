#!/usr/bin/env python3
"""
nfc_spoolman.py - NFC spool selection daemon for Klipper/Spoolman

Polls an NFC reader for TigerTag or OpenPrintTag spool tags, looks up the
spool in Spoolman, and sets the active spool in Moonraker.

Modes:
  - single (default): Sets the global active spool on scan (single-extruder)
  - multi_tool: Shows a KlipperScreen prompt to assign the scanned spool to a
    specific tool (StealthChanger, IDEX, multi-extruder)

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


# ── Klipper variables ────────────────────────────────────────────────────────

def run_gcode(moonraker_url: str, gcode: str) -> bool:
    """Execute a GCode command on Klipper via Moonraker."""
    try:
        resp = requests.post(
            f"{moonraker_url}/printer/gcode/script",
            json={"script": gcode},
            timeout=5,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error(f"GCode execution failed: {e}")
        return False


def push_klipper_variables(moonraker_url: str, spool_data: dict) -> bool:
    """Push filament metadata to Klipper via SAVE_VARIABLE commands.

    Stores variables that PRINT_START and other macros can reference:
      printer.save_variables.variables.nfc_spool_id
      printer.save_variables.variables.nfc_material
      printer.save_variables.variables.nfc_extruder_temp
      printer.save_variables.variables.nfc_bed_temp
      printer.save_variables.variables.nfc_vendor
      printer.save_variables.variables.nfc_filament_name
      printer.save_variables.variables.nfc_color_hex
      printer.save_variables.variables.nfc_diameter

    Requires [save_variables] in printer.cfg:
      [save_variables]
      filename: ~/printer_data/config/saved_variables.cfg
    """
    filament = spool_data.get("filament", {})
    vendor = filament.get("vendor", {}) or {}

    variables = {
        "nfc_spool_id": spool_data.get("id", 0),
        "nfc_material": filament.get("material") or "",
        "nfc_extruder_temp": int(filament.get("settings_extruder_temp") or 0),
        "nfc_bed_temp": int(filament.get("settings_bed_temp") or 0),
        "nfc_vendor": vendor.get("name") or "",
        "nfc_filament_name": filament.get("name") or "",
        "nfc_color_hex": filament.get("color_hex") or "",
        "nfc_diameter": float(filament.get("diameter") or 0),
    }

    cmds = []
    for key, val in variables.items():
        if isinstance(val, str):
            # Strings need nested quotes for SAVE_VARIABLE
            cmds.append(f"SAVE_VARIABLE VARIABLE={key} VALUE='\"{ val }\"'")
        elif isinstance(val, float):
            cmds.append(f"SAVE_VARIABLE VARIABLE={key} VALUE={val:.2f}")
        else:
            cmds.append(f"SAVE_VARIABLE VARIABLE={key} VALUE={val}")

    gcode = "\n".join(cmds)
    success = run_gcode(moonraker_url, gcode)
    if success:
        log.info(f"Klipper variables set: material={variables['nfc_material']}, "
                 f"extruder={variables['nfc_extruder_temp']}, bed={variables['nfc_bed_temp']}")
    return success


# ── Multi-tool support ───────────────────────────────────────────────────────

def discover_tools(moonraker_url: str) -> list[int]:
    """Query Moonraker for available tool indices.

    Tries the klipper_toolchanger 'toolchanger' object first (StealthChanger,
    TapChanger, etc.), then falls back to counting extruder objects.
    """
    # Try toolchanger object (klipper_toolchanger plugin)
    try:
        resp = requests.get(
            f"{moonraker_url}/printer/objects/query",
            params={"toolchanger": "tool_numbers"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        tool_numbers = (
            data.get("result", {})
            .get("status", {})
            .get("toolchanger", {})
            .get("tool_numbers")
        )
        if tool_numbers:
            tools = sorted(int(t) for t in tool_numbers)
            log.info(f"Tool discovery (toolchanger): {tools}")
            return tools
    except requests.RequestException:
        pass

    # Fall back to extruder enumeration
    try:
        resp = requests.get(
            f"{moonraker_url}/printer/objects/list",
            timeout=5,
        )
        resp.raise_for_status()
        objects = resp.json().get("result", {}).get("objects", [])
        tools = []
        for obj in objects:
            if obj == "extruder":
                tools.append(0)
            elif obj.startswith("extruder") and obj[8:].isdigit():
                tools.append(int(obj[8:]))
        if tools:
            tools.sort()
            log.info(f"Tool discovery (extruders): {tools}")
            return tools
    except requests.RequestException:
        pass

    log.warning("Tool discovery failed — defaulting to [0]")
    return [0]


def _spool_description(spool_data: dict) -> str:
    """Build a human-readable label from spool details."""
    filament = spool_data.get("filament", {})
    vendor = filament.get("vendor", {}) or {}
    parts = []
    if vendor.get("name"):
        parts.append(vendor["name"])
    if filament.get("material"):
        parts.append(filament["material"])
    if filament.get("name"):
        parts.append(filament["name"])
    return " ".join(parts) if parts else f"Spool #{spool_data.get('id', '?')}"


def send_nfc_prompt(
    moonraker_url: str, spool_id: int, spool_data: dict, tools: list[int]
) -> bool:
    """Store pending spool in Klipper macro variables and show a tool-selection
    prompt via Klipper's action:prompt system (displayed by KlipperScreen).
    """
    filament = spool_data.get("filament", {})
    vendor = filament.get("vendor", {}) or {}
    color = filament.get("color_hex", "")
    description = _spool_description(spool_data)

    extruder_temp = int(filament.get("settings_extruder_temp") or 0)
    bed_temp = int(filament.get("settings_bed_temp") or 0)

    # Store pending spool data in _NFC_STATE macro variables so
    # NFC_ASSIGN_TOOL can read them when the user picks a tool.
    cmds = [
        f"SET_GCODE_VARIABLE MACRO=_NFC_STATE VARIABLE=pending_spool_id VALUE={spool_id}",
        f"SET_GCODE_VARIABLE MACRO=_NFC_STATE VARIABLE=pending_material VALUE='\"{filament.get('material', '')}\"'",
        f"SET_GCODE_VARIABLE MACRO=_NFC_STATE VARIABLE=pending_vendor VALUE='\"{vendor.get('name', '')}\"'",
        f"SET_GCODE_VARIABLE MACRO=_NFC_STATE VARIABLE=pending_name VALUE='\"{filament.get('name', '')}\"'",
        f"SET_GCODE_VARIABLE MACRO=_NFC_STATE VARIABLE=pending_color VALUE='\"{color}\"'",
        f"SET_GCODE_VARIABLE MACRO=_NFC_STATE VARIABLE=pending_extruder_temp VALUE={extruder_temp}",
        f"SET_GCODE_VARIABLE MACRO=_NFC_STATE VARIABLE=pending_bed_temp VALUE={bed_temp}",
        f"SET_GCODE_VARIABLE MACRO=_NFC_STATE VARIABLE=pending_diameter VALUE={float(filament.get('diameter') or 1.75):.2f}",
    ]

    # Build the action:prompt dialog
    cmds.append(f'RESPOND TYPE=command MSG="action:prompt_begin Assign Spool: {description}"')
    cmds.append(f'RESPOND TYPE=command MSG="action:prompt_text Spool #{spool_id}"')
    if color:
        cmds.append(f'RESPOND TYPE=command MSG="action:prompt_text Color: #{color}"')
    if extruder_temp:
        cmds.append(f'RESPOND TYPE=command MSG="action:prompt_text Extruder: {extruder_temp}C  Bed: {bed_temp}C"')

    # One button per available tool
    cmds.append('RESPOND TYPE=command MSG="action:prompt_button_group_start"')
    for t in tools:
        cmds.append(f'RESPOND TYPE=command MSG="action:prompt_button T{t}|NFC_ASSIGN_TOOL TOOL={t}"')
    cmds.append('RESPOND TYPE=command MSG="action:prompt_button_group_end"')

    # Footer buttons
    cmds.append('RESPOND TYPE=command MSG="action:prompt_footer_button Cancel|NFC_CANCEL"')
    cmds.append('RESPOND TYPE=command MSG="action:prompt_show"')

    gcode = "\n".join(cmds)
    success = run_gcode(moonraker_url, gcode)
    if success:
        log.info(f"Prompt shown: '{description}' — waiting for tool selection")
    return success


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    cfg = load_config()

    poll_interval = cfg.getfloat("nfc", "poll_interval", fallback=0.5)
    debounce_time = cfg.getfloat("nfc", "debounce_time", fallback=5.0)
    auto_create = cfg.getboolean("nfc", "auto_create", fallback=False)
    mainsail_preset = cfg.getboolean("nfc", "mainsail_preset", fallback=True)
    klipper_variables = cfg.getboolean("nfc", "klipper_variables", fallback=True)
    mode = cfg.get("nfc", "mode", fallback="single").lower()
    spoolman_url = cfg.get("spoolman", "url", fallback="http://localhost:7912")
    moonraker_url = cfg.get("moonraker", "url", fallback="http://localhost:7125")

    reader = create_reader(cfg)

    # Discover tools for multi_tool mode
    tools: list[int] = []
    if mode == "multi_tool":
        tools = discover_tools(moonraker_url)

    log.info("Starting NFC Spoolman daemon")
    log.info(f"  Mode:          {mode}")
    log.info(f"  Reader:        {reader.name()}")
    log.info(f"  Spoolman:      {spoolman_url}")
    log.info(f"  Moonraker:     {moonraker_url}")
    log.info(f"  Poll interval: {poll_interval}s")
    log.info(f"  Debounce:      {debounce_time}s")
    log.info(f"  Auto-create:   {auto_create}")
    if mode == "single":
        log.info(f"  Mainsail preset: {mainsail_preset}")
        log.info(f"  Klipper vars:  {klipper_variables}")
    else:
        log.info(f"  Tools:         {tools}")

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

                if mode == "multi_tool":
                    # Multi-tool: show prompt for tool selection
                    spool_data = get_spool_details(spoolman_url, spool_id)
                    if spool_data:
                        if send_nfc_prompt(moonraker_url, spool_id, spool_data, tools):
                            last_uid = uid_hex
                            last_time = now
                    else:
                        log.warning("Could not fetch spool details for prompt")
                else:
                    # Single-tool: set spool directly (existing behavior)
                    if set_active_spool(moonraker_url, spool_id):
                        last_uid = uid_hex
                        last_time = now

                        spool_data = None
                        if mainsail_preset or klipper_variables:
                            spool_data = get_spool_details(spoolman_url, spool_id)

                        if spool_data:
                            if mainsail_preset:
                                update_mainsail_preset(moonraker_url, spool_data)
                            if klipper_variables:
                                push_klipper_variables(moonraker_url, spool_data)

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
