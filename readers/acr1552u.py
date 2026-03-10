"""ACR1552U USB NFC reader — ISO 14443A + ISO 15693 via PC/SC."""

import logging
import time

from readers.base import NfcReader, TagRead

log = logging.getLogger(__name__)

# NTAG READ command
_NTAG_CMD_READ = 0x30

# ISO 15693 commands
_ISO15693_READ_SINGLE_BLOCK = 0x20
_ISO15693_READ_MULTIPLE_BLOCKS = 0x23
_ISO15693_WRITE_SINGLE_BLOCK = 0x21

# ICODE SLIX2: 80 blocks x 4 bytes = 320 bytes
_SLIX2_BLOCK_COUNT = 80
_SLIX2_BLOCK_SIZE = 4

# PC/SC pseudo-APDU for direct transparent exchange (ACR1552U)
_APDU_DIRECT_TRANSMIT = [0xFF, 0x00, 0x00, 0x00]


class ACR1552UReader(NfcReader):
    """ACR1552U USB reader via PC/SC (pyscard). Supports ISO 14443A + ISO 15693."""

    def __init__(self, reader_name: str = "ACS ACR1552U"):
        self._reader_name = reader_name
        self._connection = None
        self._card_type: str | None = None

    def name(self) -> str:
        return f"ACR1552U ({self._reader_name})"

    def open(self) -> bool:
        try:
            from smartcard.System import readers  # noqa: PLC0415
            from smartcard.Exceptions import NoCardException, CardConnectionException  # noqa: PLC0415

            available = readers()
            if not available:
                log.error("ACR1552U: no PC/SC readers found. Is pcscd running?")
                return False

            # Find matching reader
            self._reader = None
            for r in available:
                if self._reader_name.lower() in str(r).lower():
                    self._reader = r
                    break

            if self._reader is None:
                names = [str(r) for r in available]
                log.error(f"ACR1552U: reader '{self._reader_name}' not found. Available: {names}")
                return False

            log.info(f"ACR1552U: using reader: {self._reader}")
            return True

        except ImportError:
            log.error("ACR1552U: pyscard not installed — pip install pyscard")
            return False
        except Exception as e:
            log.error(f"ACR1552U: init failed: {e}")
            return False

    def close(self) -> None:
        self._disconnect()

    def poll(self, timeout: float = 1.0) -> TagRead | None:
        try:
            from smartcard.Exceptions import NoCardException, CardConnectionException  # noqa: PLC0415

            connection = self._reader.createConnection()
            connection.connect()

            # Get ATR to determine card type
            atr = bytes(connection.getATR())
            self._connection = connection

            uid = self._get_uid()
            if uid is None:
                self._disconnect()
                return None

            # Detect protocol from ATR
            protocol, data = self._detect_and_read(atr)
            if data is None:
                self._disconnect()
                return None

            self._disconnect()
            return TagRead(uid=uid, protocol=protocol, data=data)

        except Exception:
            self._disconnect()
            return None

    def write_blocks(self, uid: bytes, offset: int, data: bytes) -> bool:
        """Write to ISO 15693 tag via PC/SC transparent commands."""
        if self._connection is None:
            return False

        block_start = offset // _SLIX2_BLOCK_SIZE
        try:
            pos = 0
            while pos < len(data):
                block_num = block_start + (pos // _SLIX2_BLOCK_SIZE)
                block_data = data[pos:pos + _SLIX2_BLOCK_SIZE]
                if len(block_data) < _SLIX2_BLOCK_SIZE:
                    block_data = block_data + b'\x00' * (_SLIX2_BLOCK_SIZE - len(block_data))

                apdu = self._build_iso15693_apdu(
                    bytes([0x22, _ISO15693_WRITE_SINGLE_BLOCK]) + uid + bytes([block_num]) + block_data,
                )
                resp, sw1, sw2 = self._connection.transmit(list(apdu))
                if sw1 != 0x90:
                    log.error(f"ACR1552U: write block {block_num} failed: SW={sw1:02x}{sw2:02x}")
                    return False
                pos += _SLIX2_BLOCK_SIZE

            return True
        except Exception as e:
            log.error(f"ACR1552U: write failed: {e}")
            return False

    # ── Internal ─────────────────────────────────────────────────────────

    def _disconnect(self):
        if self._connection is not None:
            try:
                self._connection.disconnect()
            except Exception:
                pass
            self._connection = None

    def _get_uid(self) -> bytes | None:
        """Get card UID via standard PC/SC GetUID APDU."""
        apdu = [0xFF, 0xCA, 0x00, 0x00, 0x00]
        try:
            resp, sw1, sw2 = self._connection.transmit(apdu)
            if sw1 == 0x90 and sw2 == 0x00:
                return bytes(resp)
        except Exception:
            pass
        return None

    def _detect_and_read(self, atr: bytes) -> tuple[str, bytes | None]:
        """Detect tag type from ATR and read full memory."""
        # ATR for ISO 15693 tags typically contains 0x00 0x0C in the historical bytes
        # ATR for ISO 14443A NTAG typically contains tag type indicators
        # A simpler heuristic: try ISO 15693 read first, fall back to NTAG

        # Try reading as ISO 15693 (NFC-V)
        data = self._try_read_iso15693()
        if data is not None:
            return ("iso15693", data)

        # Fall back to NTAG (ISO 14443A)
        data = self._try_read_ntag()
        if data is not None:
            return ("iso14443a", data)

        return ("unknown", None)

    def _try_read_iso15693(self) -> bytes | None:
        """Try to read all blocks as ISO 15693."""
        try:
            uid = self._get_uid()
            if uid is None or len(uid) != 8:
                return None

            data = bytearray()
            for start_block in range(0, _SLIX2_BLOCK_COUNT, 4):
                num_blocks = min(4, _SLIX2_BLOCK_COUNT - start_block)
                apdu = self._build_iso15693_apdu(
                    bytes([0x22, _ISO15693_READ_MULTIPLE_BLOCKS]) + uid + bytes([start_block, num_blocks - 1]),
                )
                resp, sw1, sw2 = self._connection.transmit(list(apdu))
                if sw1 != 0x90:
                    return None
                # Skip flags byte in response
                block_data = bytes(resp[1:1 + num_blocks * _SLIX2_BLOCK_SIZE])
                if len(block_data) < num_blocks * _SLIX2_BLOCK_SIZE:
                    return None
                data.extend(block_data)

            return bytes(data)
        except Exception:
            return None

    def _try_read_ntag(self) -> bytes | None:
        """Try to read pages 4-39 as NTAG via standard READ BINARY."""
        try:
            data = bytearray()
            for page in range(4, 40, 4):
                # Standard PC/SC read binary for NTAG: FF B0 00 PAGE LEN
                apdu = [0xFF, 0xB0, 0x00, page, 0x10]  # read 16 bytes
                resp, sw1, sw2 = self._connection.transmit(apdu)
                if sw1 != 0x90 or len(resp) < 16:
                    return None
                data.extend(resp[:16])
            return bytes(data[:144])
        except Exception:
            return None

    def _build_iso15693_apdu(self, payload: bytes) -> bytes:
        """Wrap an ISO 15693 command in a PC/SC transparent APDU."""
        # ACR1552U transparent command: FF 00 00 00 Lc [payload]
        return bytes([0xFF, 0x00, 0x00, 0x00, len(payload)]) + payload
