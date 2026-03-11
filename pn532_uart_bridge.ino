#include <Arduino.h>

/*
 * PN532 UART Bridge for ESP32-DevKitC
 *
 * The PN532 in HSU mode drops back to sleep between UART transactions.
 * This bridge automatically prepends the wakeup preamble to every
 * command forwarded to the PN532, so the host doesn't need to worry
 * about wakeup timing.
 *
 * Wiring:
 *   PN532 VCC  -> ESP32 3.3V
 *   PN532 GND  -> ESP32 GND
 *   PN532 TXD  -> ESP32 GPIO16 (UART2 RX)
 *   PN532 RXD  -> ESP32 GPIO17 (UART2 TX)
 *
 * PN532 DIP switches: both OFF (HSU/UART mode)
 */

#define PN532_RX_PIN   16
#define PN532_TX_PIN   17
#define PN532_BAUD     115200
#define HOST_BAUD      115200

#define BUF_SIZE       512

// PN532 HSU wakeup preamble
static const uint8_t WAKEUP[] = {
  0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55,
  0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55,
  0x00, 0x00, 0x00
};

// PN532 frame start: 0x00 0x00 0xFF
static const uint8_t FRAME_START[] = {0x00, 0x00, 0xFF};

HardwareSerial pn532Serial(2);

// Forward PN532 -> host immediately
void onPN532Data() {
  uint8_t buf[BUF_SIZE];
  size_t count = pn532Serial.available();
  if (count > 0) {
    count = pn532Serial.readBytes(buf, min(count, (size_t)BUF_SIZE));
    Serial.write(buf, count);
    Serial.flush();
  }
}

// Check if buffer contains a PN532 frame start (00 00 FF)
bool hasFrameStart(const uint8_t* buf, size_t len) {
  for (size_t i = 0; i + 2 < len; i++) {
    if (buf[i] == 0x00 && buf[i+1] == 0x00 && buf[i+2] == 0xFF) {
      return true;
    }
  }
  return false;
}

// Check if buffer is all wakeup bytes (0x55 and 0x00)
bool isWakeupOnly(const uint8_t* buf, size_t len) {
  for (size_t i = 0; i < len; i++) {
    if (buf[i] != 0x55 && buf[i] != 0x00) return false;
  }
  return true;
}

void setup() {
  Serial.begin(HOST_BAUD);
  pn532Serial.begin(PN532_BAUD, SERIAL_8N1, PN532_RX_PIN, PN532_TX_PIN);
  pn532Serial.onReceive(onPN532Data);
  delay(100);
}

void loop() {
  if (Serial.available() > 0) {
    // Small delay to let full write arrive
    delay(20);

    uint8_t buf[BUF_SIZE];
    size_t count = Serial.readBytes(buf, min((size_t)Serial.available(), (size_t)BUF_SIZE));

    if (count > 0) {
      if (isWakeupOnly(buf, count)) {
        // Host sent just a wakeup preamble — absorb it silently.
        // We'll prepend our own wakeup when the actual command arrives.
      } else {
        // Prepend wakeup preamble before forwarding the command
        pn532Serial.write(WAKEUP, sizeof(WAKEUP));
        pn532Serial.write(buf, count);
        pn532Serial.flush();
      }
    }
  }

  delay(1);
}
