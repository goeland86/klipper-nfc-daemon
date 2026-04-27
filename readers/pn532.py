"""PN532 UART NFC reader — ISO 14443A (NTAG) only."""

import logging
import time

import serial

from readers.base import NfcReader, TagRead

log = logging.getLogger(__name__)

# PN532 protocol constants
_HOSTTOPN532 = 0xD4
_PN532TOHOST = 0xD5
_CMD_GETFIRMWAREVERSION = 0x02
_CMD_SAMCONFIGURATION = 0x14
_CMD_INLISTPASSIVETARGET = 0x4A
_CMD_INDATAEXCHANGE = 0x40
_NTAG_CMD_READ = 0x30

# PN532 HSU wakeup preamble — the PN532 drops back to sleep between UART
# transactions in HSU mode; this must be sent before every command.
_HSU_WAKEUP = bytes([0x55] * 16 + [0x00, 0x00, 0x00])


class PN532Reader(NfcReader):
    """PN532 over UART. Reads ISO 14443A tags (NTAG213/215/216)."""

    def __init__(self, port: str = "/dev/ttyUSB0", baudrate: int = 115200):
        self._port = port
        self._baudrate = baudrate
        self._ser: serial.Serial | None = None

    def name(self) -> str:
        return f"PN532 UART ({self._port})"

    def open(self) -> bool:
        try:
            # Suppress DTR/RTS toggle on open — some USB-UART adapters
            # (PL2303, FTDI) pulse these lines, which can momentarily disturb
            # power or reset signaling on the PN532.
            self._ser = serial.Serial()
            self._ser.port = self._port
            self._ser.baudrate = self._baudrate
            self._ser.timeout = 1.0
            self._ser.dtr = False
            self._ser.rts = False
            self._ser.open()
            time.sleep(0.5)
            self._wakeup()
            fw = self._get_firmware_version()
            if fw is None:
                log.error("PN532: no response to firmware version query")
                self.close()
                return False
            log.info(f"PN532 firmware: IC=0x{fw[0]:02x} Ver={fw[1]}.{fw[2]} Support=0x{fw[3]:02x}")
            if not self._sam_configuration():
                log.error("PN532: SAM configuration failed")
                self.close()
                return False
            return True
        except serial.SerialException as e:
            log.error(f"PN532: serial error: {e}")
            return False

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def poll(self, timeout: float = 1.0) -> TagRead | None:
        if self._ser is None:
            return None

        uid = self._poll_for_tag(timeout=timeout)
        if uid is None:
            return None

        data = self._read_ntag_user_memory()
        if data is None:
            return None

        return TagRead(uid=uid, protocol="iso14443a", data=data)

    # ── Low-level PN532 protocol ─────────────────────────────────────────

    def _wakeup(self):
        self._ser.write(_HSU_WAKEUP)
        time.sleep(0.5)
        self._ser.reset_input_buffer()

    def _write_frame(self, data: bytes):
        length = len(data) + 1
        lcs = (~length + 1) & 0xFF
        frame = bytearray([0x00, 0x00, 0xFF, length & 0xFF, lcs, _HOSTTOPN532])
        frame.extend(data)
        dcs = _HOSTTOPN532
        for b in data:
            dcs += b
        dcs = (~dcs + 1) & 0xFF
        frame.append(dcs)
        frame.append(0x00)
        self._ser.write(bytes(frame))

    def _read_response(self, timeout: float = 1.0) -> bytes | None:
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self._ser.read(self._ser.in_waiting or 1)
            if chunk:
                buf.extend(chunk)
            raw = bytes(buf)
            pos = 0
            while True:
                idx = raw.find(b'\x00\xFF', pos)
                if idx < 0:
                    break
                remaining = raw[idx:]
                if len(remaining) < 4:
                    break
                length = remaining[2]
                if length == 0:
                    pos = idx + 4
                    continue
                total = 2 + 1 + 1 + length + 1 + 1
                if len(remaining) < total:
                    break
                tfi = remaining[4]
                if tfi != _PN532TOHOST:
                    pos = idx + 2
                    continue
                data = remaining[5:4 + length]
                return bytes(data)
            time.sleep(0.01)
        return None

    def _send_command(self, cmd: int, params: bytes = b'', timeout: float = 1.0) -> bytes | None:
        self._ser.reset_input_buffer()
        self._ser.write(_HSU_WAKEUP)
        self._write_frame(bytes([cmd]) + params)
        resp = self._read_response(timeout=timeout)
        if resp is None:
            return None
        if len(resp) > 0 and resp[0] != (cmd + 1):
            return None
        return resp[1:]

    def _get_firmware_version(self) -> tuple | None:
        resp = self._send_command(_CMD_GETFIRMWAREVERSION)
        if resp and len(resp) >= 4:
            return (resp[0], resp[1], resp[2], resp[3])
        return None

    def _sam_configuration(self) -> bool:
        resp = self._send_command(_CMD_SAMCONFIGURATION, bytes([0x01, 0x14, 0x01]))
        return resp is not None

    def _poll_for_tag(self, timeout: float = 1.0) -> bytes | None:
        resp = self._send_command(_CMD_INLISTPASSIVETARGET, bytes([0x01, 0x00]), timeout=timeout)
        if resp is None or len(resp) < 6:
            return None
        if resp[0] == 0:
            return None
        nfcid_len = resp[4]
        if len(resp) < 5 + nfcid_len:
            return None
        return bytes(resp[5:5 + nfcid_len])

    def _ntag_read_page(self, page: int) -> bytes | None:
        resp = self._send_command(_CMD_INDATAEXCHANGE, bytes([0x01, _NTAG_CMD_READ, page]), timeout=1.0)
        if resp is None or len(resp) < 1:
            return None
        if resp[0] != 0x00:
            return None
        return resp[1:]

    def _read_ntag_user_memory(self) -> bytes | None:
        data = bytearray()
        for start_page in range(4, 40, 4):
            chunk = self._ntag_read_page(start_page)
            if chunk is None:
                log.error(f"PN532: failed to read pages {start_page}-{start_page + 3}")
                return None
            data.extend(chunk)
        return bytes(data[:144])
