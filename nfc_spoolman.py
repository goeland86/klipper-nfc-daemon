#!/usr/bin/env python3
"""
nfc_spoolman.py - NFC spool selection daemon for Klipper/Spoolman

Polls a PN532 NFC reader (UART) for TigerTag spool tags, looks up the spool
in Spoolman, and sets the active spool in Moonraker.

Uses pyserial to talk PN532 protocol directly — no nfcpy/libnfc dependency.

Configuration: ~/printer_data/config/nfc_spoolman.cfg
"""

import base64
import configparser
import logging
import os
import struct
import sys
import time

import serial
import requests

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


# ── PN532 UART Protocol ──────────────────────────────────────────────────────

PN532_PREAMBLE = 0x00
PN532_STARTCODE1 = 0x00
PN532_STARTCODE2 = 0xFF
PN532_HOSTTOPN532 = 0xD4
PN532_PN532TOHOST = 0xD5

# Commands
PN532_CMD_SAMCONFIGURATION = 0x14
PN532_CMD_INLISTPASSIVETARGET = 0x4A
PN532_CMD_INDATAEXCHANGE = 0x40
PN532_CMD_GETFIRMWAREVERSION = 0x02

# NTAG read command
NTAG_CMD_READ = 0x30


class PN532Uart:
    """Low-level PN532 driver over UART."""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        self.ser = serial.Serial(port, baudrate, timeout=timeout)
        self._wakeup()

    def _wakeup(self):
        """Send wakeup sequence (long preamble) to PN532."""
        self.ser.write(b'\x55' * 24 + b'\x00\x00\x00')
        time.sleep(0.5)
        self.ser.reset_input_buffer()

    def close(self):
        self.ser.close()

    def _write_frame(self, data: bytes):
        """Write a PN532 normal information frame."""
        length = len(data) + 1  # +1 for TFI byte
        lcs = (~length + 1) & 0xFF
        frame = bytearray([
            PN532_PREAMBLE,
            PN532_STARTCODE1,
            PN532_STARTCODE2,
            length & 0xFF,
            lcs,
            PN532_HOSTTOPN532,
        ])
        frame.extend(data)
        # DCS: checksum of TFI + data
        dcs = PN532_HOSTTOPN532
        for b in data:
            dcs += b
        dcs = (~dcs + 1) & 0xFF
        frame.append(dcs)
        frame.append(0x00)  # postamble
        self.ser.write(bytes(frame))

    def _read_response(self, timeout: float = 1.0) -> bytes | None:
        """Read ACK + response frame from PN532, return data after TFI byte."""
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                buf.extend(chunk)
            # Scan for all 00 FF start codes in the buffer
            raw = bytes(buf)
            pos = 0
            while True:
                idx = raw.find(b'\x00\xFF', pos)
                if idx < 0:
                    break
                remaining = raw[idx:]
                if len(remaining) < 4:
                    break  # need more data
                length = remaining[2]
                if length == 0:
                    # ACK frame (00 FF 00 FF 00) — skip it
                    pos = idx + 4
                    continue
                # Normal frame: 00 FF LEN LCS TFI DATA... DCS 00
                total = 2 + 1 + 1 + length + 1 + 1
                if len(remaining) < total:
                    break  # need more data
                # TFI is at offset 4, data starts at offset 5
                tfi = remaining[4]
                if tfi != PN532_PN532TOHOST:
                    pos = idx + 2
                    continue
                data = remaining[5:4 + length]  # length includes TFI
                return bytes(data)
            time.sleep(0.01)
        return None

    def send_command(self, cmd: int, params: bytes = b'', timeout: float = 1.0) -> bytes | None:
        """Send a command and return the response data."""
        self.ser.reset_input_buffer()
        data = bytes([cmd]) + params
        self._write_frame(data)

        resp = self._read_response(timeout=timeout)
        if resp is None:
            log.debug("No response received")
            return None

        # First byte of response should be cmd+1
        if len(resp) > 0 and resp[0] != (cmd + 1):
            log.debug(f"Unexpected response command: 0x{resp[0]:02x} (expected 0x{cmd+1:02x})")
            return None

        return resp[1:]  # skip command byte

    def get_firmware_version(self) -> tuple | None:
        """Get PN532 firmware version. Returns (IC, Ver, Rev, Support) or None."""
        resp = self.send_command(PN532_CMD_GETFIRMWAREVERSION)
        if resp and len(resp) >= 4:
            return (resp[0], resp[1], resp[2], resp[3])
        return None

    def sam_configuration(self) -> bool:
        """Configure the SAM (Security Access Module) for normal mode."""
        # Mode 0x01 = Normal, Timeout 0x14 = ~1s, IRQ = off
        resp = self.send_command(PN532_CMD_SAMCONFIGURATION, bytes([0x01, 0x14, 0x01]))
        return resp is not None

    def poll_for_tag(self, timeout: float = 1.0) -> bytes | None:
        """
        Poll for a single ISO14443A tag.
        Returns the UID bytes, or None if no tag found.
        """
        # MaxTg=1, BrTy=0x00 (106 kbps type A)
        resp = self.send_command(
            PN532_CMD_INLISTPASSIVETARGET,
            bytes([0x01, 0x00]),
            timeout=timeout,
        )
        if resp is None or len(resp) < 6:
            return None

        num_tags = resp[0]
        if num_tags == 0:
            return None

        # Parse target data: Tg(1) + SENS_RES(2) + SEL_RES(1) + NFCIDLength(1) + NFCID(n)
        nfcid_len = resp[4]
        if len(resp) < 5 + nfcid_len:
            return None
        uid = resp[5:5 + nfcid_len]
        return bytes(uid)

    def ntag_read_page(self, page: int) -> bytes | None:
        """
        Read 4 pages (16 bytes) starting at the given page number.
        NTAG READ command returns 16 bytes at a time.
        """
        # InDataExchange: Tg=0x01, NTAG READ cmd, page number
        resp = self.send_command(
            PN532_CMD_INDATAEXCHANGE,
            bytes([0x01, NTAG_CMD_READ, page]),
            timeout=1.0,
        )
        if resp is None or len(resp) < 1:
            return None
        status = resp[0]
        if status != 0x00:
            log.debug(f"NTAG read error at page {page}: status 0x{status:02x}")
            return None
        return resp[1:]  # 16 bytes

    def read_ntag_user_memory(self) -> bytes | None:
        """
        Read NTAG213 user memory: pages 4-39 (36 pages = 144 bytes).
        NTAG READ returns 4 pages (16 bytes) per call, so we need 9 reads.
        """
        data = bytearray()
        for start_page in range(4, 40, 4):
            chunk = self.ntag_read_page(start_page)
            if chunk is None:
                log.error(f"Failed to read pages {start_page}-{start_page+3}")
                return None
            data.extend(chunk)
        # We read pages 4-43 (40 pages = 160 bytes), trim to 4-39 (144 bytes)
        return bytes(data[:144])


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


# ── Spoolman ──────────────────────────────────────────────────────────────────

def lookup_spool(spoolman_url: str, raw_pages: bytes) -> int | None:
    """
    POST the raw NTAG213 user memory (pages 4-39, 144 bytes) to Spoolman's
    /api/v1/nfc/lookup endpoint and return the matched spool_id, or None.
    """
    payload = {
        "raw_data_b64": base64.b64encode(raw_pages).decode("ascii")
    }

    try:
        resp = requests.post(
            f"{spoolman_url}/api/v1/nfc/lookup",
            json=payload,
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("success") and data.get("spool_id") is not None:
            return int(data["spool_id"])
        else:
            log.warning(f"Spoolman lookup: {data.get('message', 'no match')}")
            return None

    except requests.RequestException as e:
        log.error(f"Spoolman request failed: {e}")
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


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    cfg = load_config()

    device_str = cfg.get("nfc", "device", fallback="/dev/ttyUSB0:115200")
    # Parse device:baudrate
    if ":" in device_str:
        device, baud_str = device_str.rsplit(":", 1)
        baudrate = int(baud_str)
    else:
        device = device_str
        baudrate = 115200

    poll_interval = cfg.getfloat("nfc", "poll_interval", fallback=0.5)
    debounce_time = cfg.getfloat("nfc", "debounce_time", fallback=5.0)
    spoolman_url = cfg.get("spoolman", "url", fallback="http://localhost:7912")
    moonraker_url = cfg.get("moonraker", "url", fallback="http://localhost:7125")

    log.info("Starting NFC Spoolman daemon")
    log.info(f"  NFC device:    {device} @ {baudrate}")
    log.info(f"  Spoolman:      {spoolman_url}")
    log.info(f"  Moonraker:     {moonraker_url}")
    log.info(f"  Poll interval: {poll_interval}s")
    log.info(f"  Debounce:      {debounce_time}s")

    last_uid: str | None = None
    last_time: float = 0.0

    while True:
        pn532 = None
        try:
            pn532 = PN532Uart(device, baudrate)
            fw = pn532.get_firmware_version()
            if fw is None:
                log.error("Could not communicate with PN532 — retrying in 5s")
                pn532.close()
                time.sleep(5)
                continue
            log.info(f"PN532 firmware: IC=0x{fw[0]:02x} Ver={fw[1]}.{fw[2]} Support=0x{fw[3]:02x}")

            if not pn532.sam_configuration():
                log.error("SAM configuration failed — retrying in 5s")
                pn532.close()
                time.sleep(5)
                continue

            log.info("NFC reader ready, polling for tags...")

            while True:
                uid = pn532.poll_for_tag(timeout=1.0)
                if uid is None:
                    time.sleep(poll_interval)
                    continue

                uid_hex = uid.hex()
                now = time.monotonic()

                # Debounce
                if uid_hex == last_uid and (now - last_time) < debounce_time:
                    log.debug(f"Debouncing tag {uid_hex}")
                    time.sleep(poll_interval)
                    continue

                log.info(f"Tag detected: UID={uid_hex}")

                # Read NTAG user memory (pages 4-39)
                raw_data = pn532.read_ntag_user_memory()
                if raw_data is None:
                    log.error("Failed to read tag memory")
                    time.sleep(poll_interval)
                    continue

                log.info(f"Read {len(raw_data)} bytes from tag")
                log.debug(f"Raw: {raw_data[:36].hex()}")

                # Look up spool in Spoolman
                spool_id = lookup_spool(spoolman_url, raw_data)
                if spool_id is None:
                    log.warning(f"No spool found for tag {uid_hex}")
                    last_uid = uid_hex
                    last_time = now
                    time.sleep(poll_interval)
                    continue

                log.info(f"Matched spool ID: {spool_id}")

                # Set active spool in Moonraker
                if set_active_spool(moonraker_url, spool_id):
                    last_uid = uid_hex
                    last_time = now

                time.sleep(poll_interval)

        except serial.SerialException as e:
            log.error(f"Serial error: {e} — retrying in 5s")
            time.sleep(5)
        except KeyboardInterrupt:
            log.info("Shutting down")
            sys.exit(0)
        except Exception as e:
            log.exception(f"Unexpected error: {e} — retrying in 5s")
            time.sleep(5)
        finally:
            if pn532 is not None:
                try:
                    pn532.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
