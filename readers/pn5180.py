"""PN5180 SPI NFC reader — ISO 14443A + ISO 15693."""

import logging
import time

from readers.base import NfcReader, TagRead

log = logging.getLogger(__name__)

# PN5180 command codes
_CMD_WRITE_REGISTER = 0x00
_CMD_WRITE_REGISTER_OR_MASK = 0x01
_CMD_WRITE_REGISTER_AND_MASK = 0x02
_CMD_READ_REGISTER = 0x04
_CMD_SEND_DATA = 0x09
_CMD_READ_DATA = 0x0A
_CMD_LOAD_RF_CONFIG = 0x11
_CMD_RF_ON = 0x16
_CMD_RF_OFF = 0x17

# ISO 15693 commands
_ISO15693_INVENTORY = 0x01
_ISO15693_READ_SINGLE_BLOCK = 0x20
_ISO15693_READ_MULTIPLE_BLOCKS = 0x23
_ISO15693_WRITE_SINGLE_BLOCK = 0x21

# ISO 14443A constants
_ISO14443A_REQA = 0x26
_ISO14443A_SELECT = 0x93

# RF config slots
_RF_CONFIG_ISO15693 = 0x0D
_RF_CONFIG_ISO14443A = 0x00

# ICODE SLIX2: 80 blocks x 4 bytes = 320 bytes
_SLIX2_BLOCK_COUNT = 80
_SLIX2_BLOCK_SIZE = 4


class PN5180Reader(NfcReader):
    """PN5180 over SPI. Reads both ISO 14443A and ISO 15693 tags."""

    def __init__(self, spi_bus: int = 0, spi_cs: int = 0,
                 busy_pin: int = 25, reset_pin: int = 24):
        self._spi_bus = spi_bus
        self._spi_cs = spi_cs
        self._busy_pin = busy_pin
        self._reset_pin = reset_pin
        self._spi = None
        self._gpio = None

    def name(self) -> str:
        return f"PN5180 SPI (bus={self._spi_bus} cs={self._spi_cs})"

    def open(self) -> bool:
        try:
            import spidev  # noqa: PLC0415
            try:
                import gpiod  # noqa: PLC0415
                self._gpio_lib = "gpiod"
            except ImportError:
                import RPi.GPIO as GPIO  # noqa: PLC0415, N811
                self._gpio_lib = "rpigpio"

            self._spi = spidev.SpiDev()
            self._spi.open(self._spi_bus, self._spi_cs)
            self._spi.max_speed_hz = 1000000  # 1 MHz
            self._spi.mode = 0

            self._setup_gpio()
            self._reset()

            # Verify communication by reading firmware version register
            version = self._read_register(0x12)  # VERSION register
            if version is None or version == 0:
                log.error("PN5180: could not read firmware version")
                self.close()
                return False

            log.info(f"PN5180 firmware version: 0x{version:08x}")
            return True

        except ImportError as e:
            log.error(f"PN5180: missing dependency: {e} — install spidev and gpiod (or RPi.GPIO)")
            return False
        except Exception as e:
            log.error(f"PN5180: init failed: {e}")
            self.close()
            return False

    def close(self) -> None:
        if self._spi is not None:
            try:
                self._spi.close()
            except Exception:
                pass
            self._spi = None
        self._cleanup_gpio()

    def poll(self, timeout: float = 1.0) -> TagRead | None:
        if self._spi is None:
            return None

        # Try ISO 15693 first (OpenPrintTag)
        result = self._poll_iso15693()
        if result is not None:
            return result

        # Then try ISO 14443A (TigerTag/NTAG)
        return self._poll_iso14443a()

    def write_blocks(self, uid: bytes, offset: int, data: bytes) -> bool:
        """Write data to ISO 15693 tag blocks."""
        if self._spi is None:
            return False

        self._load_rf_config(_RF_CONFIG_ISO15693)
        self._rf_on()

        block_start = offset // _SLIX2_BLOCK_SIZE
        try:
            pos = 0
            while pos < len(data):
                block_num = block_start + (pos // _SLIX2_BLOCK_SIZE)
                block_data = data[pos:pos + _SLIX2_BLOCK_SIZE]
                if len(block_data) < _SLIX2_BLOCK_SIZE:
                    block_data = block_data + b'\x00' * (_SLIX2_BLOCK_SIZE - len(block_data))

                cmd = bytes([0x22, _ISO15693_WRITE_SINGLE_BLOCK]) + uid + bytes([block_num]) + block_data
                self._transceive(cmd)
                pos += _SLIX2_BLOCK_SIZE

            return True
        except Exception as e:
            log.error(f"PN5180: write failed: {e}")
            return False
        finally:
            self._rf_off()

    # ── ISO 15693 ────────────────────────────────────────────────────────

    def _poll_iso15693(self) -> TagRead | None:
        try:
            self._load_rf_config(_RF_CONFIG_ISO15693)
            self._rf_on()

            # Inventory command: flags=0x26 (high data rate, 1 slot), cmd=0x01
            inventory_cmd = bytes([0x26, _ISO15693_INVENTORY, 0x00])
            resp = self._transceive(inventory_cmd)
            if resp is None or len(resp) < 10:
                return None

            # Response: flags(1) + DSFID(1) + UID(8)
            uid = bytes(resp[2:10])

            # Read full tag memory
            data = self._read_iso15693_memory(uid)
            if data is None:
                return None

            return TagRead(uid=uid, protocol="iso15693", data=data)

        except Exception as e:
            log.debug(f"PN5180: ISO 15693 poll failed: {e}")
            return None
        finally:
            self._rf_off()

    def _read_iso15693_memory(self, uid: bytes) -> bytes | None:
        """Read all blocks from an ISO 15693 tag."""
        data = bytearray()
        # Read in chunks of 16 blocks using Read Multiple Blocks
        for start_block in range(0, _SLIX2_BLOCK_COUNT, 16):
            num_blocks = min(16, _SLIX2_BLOCK_COUNT - start_block)
            # flags=0x22 (addressed, high data rate), cmd, uid, start, count-1
            cmd = bytes([0x22, _ISO15693_READ_MULTIPLE_BLOCKS]) + uid + bytes([start_block, num_blocks - 1])
            resp = self._transceive(cmd)
            if resp is None or len(resp) < 2:
                log.error(f"PN5180: failed to read blocks {start_block}-{start_block + num_blocks - 1}")
                return None
            # Response: flags(1) + data(num_blocks * 4)
            block_data = resp[1:1 + num_blocks * _SLIX2_BLOCK_SIZE]
            data.extend(block_data)

        return bytes(data)

    # ── ISO 14443A ───────────────────────────────────────────────────────

    def _poll_iso14443a(self) -> TagRead | None:
        try:
            self._load_rf_config(_RF_CONFIG_ISO14443A)
            self._rf_on()

            # Send REQA
            resp = self._transceive(bytes([_ISO14443A_REQA]), bits=7)
            if resp is None or len(resp) < 2:
                return None

            # Anticollision + select (simplified — single-size UID)
            resp = self._transceive(bytes([_ISO14443A_SELECT, 0x20]))
            if resp is None or len(resp) < 5:
                return None

            uid = bytes(resp[:4])

            # Select the tag
            select_cmd = bytes([_ISO14443A_SELECT, 0x70]) + resp[:5]
            resp = self._transceive(select_cmd)
            if resp is None:
                return None

            # Read NTAG user memory (pages 4-39)
            data = bytearray()
            for page in range(4, 40, 4):
                resp = self._transceive(bytes([0x30, page]))
                if resp is None or len(resp) < 16:
                    log.error(f"PN5180: failed to read NTAG page {page}")
                    return None
                data.extend(resp[:16])

            return TagRead(uid=uid, protocol="iso14443a", data=bytes(data[:144]))

        except Exception as e:
            log.debug(f"PN5180: ISO 14443A poll failed: {e}")
            return None
        finally:
            self._rf_off()

    # ── SPI / GPIO low-level ─────────────────────────────────────────────

    def _setup_gpio(self):
        if self._gpio_lib == "gpiod":
            import gpiod  # noqa: PLC0415
            chip = gpiod.Chip("/dev/gpiochip0")
            self._busy_line = chip.get_line(self._busy_pin)
            self._reset_line = chip.get_line(self._reset_pin)
            self._busy_line.request(consumer="nfc_spoolman", type=gpiod.LINE_REQ_DIR_IN)
            self._reset_line.request(consumer="nfc_spoolman", type=gpiod.LINE_REQ_DIR_OUT, default_val=1)
        else:
            import RPi.GPIO as GPIO  # noqa: PLC0415, N811
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self._busy_pin, GPIO.IN)
            GPIO.setup(self._reset_pin, GPIO.OUT, initial=GPIO.HIGH)
            self._gpio = GPIO

    def _cleanup_gpio(self):
        if self._gpio_lib == "rpigpio" and self._gpio is not None:
            try:
                self._gpio.cleanup([self._busy_pin, self._reset_pin])
            except Exception:
                pass
            self._gpio = None

    def _wait_busy(self, timeout: float = 1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._gpio_lib == "gpiod":
                if self._busy_line.get_value() == 0:
                    return
            else:
                if self._gpio.input(self._busy_pin) == 0:
                    return
            time.sleep(0.001)

    def _reset(self):
        if self._gpio_lib == "gpiod":
            self._reset_line.set_value(0)
            time.sleep(0.01)
            self._reset_line.set_value(1)
        else:
            self._gpio.output(self._reset_pin, 0)
            time.sleep(0.01)
            self._gpio.output(self._reset_pin, 1)
        time.sleep(0.1)
        self._wait_busy()

    def _spi_transfer(self, data: bytes) -> bytes:
        self._wait_busy()
        return bytes(self._spi.xfer2(list(data)))

    def _read_register(self, addr: int) -> int | None:
        try:
            self._spi_transfer(bytes([_CMD_READ_REGISTER, addr]))
            self._wait_busy()
            resp = self._spi_transfer(bytes([0x00] * 4))
            return int.from_bytes(resp, 'little')
        except Exception:
            return None

    def _load_rf_config(self, tx_config: int, rx_config: int | None = None):
        if rx_config is None:
            rx_config = tx_config + 0x80
        self._spi_transfer(bytes([_CMD_LOAD_RF_CONFIG, tx_config, rx_config]))

    def _rf_on(self):
        self._spi_transfer(bytes([_CMD_RF_ON, 0x00]))
        time.sleep(0.01)

    def _rf_off(self):
        self._spi_transfer(bytes([_CMD_RF_OFF, 0x00]))

    def _transceive(self, data: bytes, bits: int | None = None) -> bytes | None:
        # Write data to send buffer
        cmd = bytes([_CMD_SEND_DATA, len(data)]) + data
        self._spi_transfer(cmd)
        self._wait_busy(timeout=0.5)

        # Read response
        resp = self._spi_transfer(bytes([_CMD_READ_DATA, 0x00]))
        if resp is None or len(resp) < 2:
            return None
        resp_len = resp[1]
        if resp_len == 0:
            return None
        data_resp = self._spi_transfer(bytes([0x00] * resp_len))
        return bytes(data_resp)
