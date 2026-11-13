// Based on ESP32 iPhone Agent (https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan
// Copyright (c) 2026 Henry Yuan. Licensed under the MIT License (see LICENSE and NOTICE).

#include "ble_hid.h"
#include "ble_link.h"
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include "USBHIDKeyboard.h"

static NimBLEHIDDevice *hidDev;
static NimBLECharacteristic *kbdInput;
static NimBLECharacteristic *consumerInput;
static NimBLECharacteristic *mouseInput;

static volatile uint8_t hidSubByHandle[8] = {0};  // per-conn bitmask: bit0 kbd, bit1 consumer, bit2 mouse
static uint8_t mouseButtons = 0;
static uint16_t absX = 5000, absY = 5000;
static uint8_t kbdModifiers = 0;
static uint8_t kbdKeys[6] = {0};

// Composite HID Report Descriptor:
//   Report ID 1: Standard 8-byte Keyboard (modifiers + reserved + 6 keycodes)
//   Report ID 2: 16-bit Consumer Control (media / AC Home 0x0223)
//   Report ID 3: 7-byte Absolute Mouse (buttons + uint16 X/Y 0..10000 + wheel + AC Pan)
static const uint8_t kHidReportDescriptor[] = {
  // ---- Keyboard (Report ID 1) ----
  0x05, 0x01,        // Usage Page (Generic Desktop)
  0x09, 0x06,        // Usage (Keyboard)
  0xA1, 0x01,        // Collection (Application)
  0x85, 0x01,        //   Report ID (1)
  0x05, 0x07,        //   Usage Page (Key Codes)
  0x19, 0xE0,        //   Usage Minimum (224)
  0x29, 0xE7,        //   Usage Maximum (231)
  0x15, 0x00,        //   Logical Minimum (0)
  0x25, 0x01,        //   Logical Maximum (1)
  0x75, 0x01,        //   Report Size (1)
  0x95, 0x08,        //   Report Count (8)
  0x81, 0x02,        //   Input (Data, Variable, Absolute) - Modifier byte
  0x95, 0x01,        //   Report Count (1)
  0x75, 0x08,        //   Report Size (8)
  0x81, 0x01,        //   Input (Constant) - Reserved byte
  0x95, 0x06,        //   Report Count (6)
  0x75, 0x08,        //   Report Size (8)
  0x15, 0x00,        //   Logical Minimum (0)
  0x25, 0x65,        //   Logical Maximum (101)
  0x05, 0x07,        //   Usage Page (Key Codes)
  0x19, 0x00,        //   Usage Minimum (0)
  0x29, 0x65,        //   Usage Maximum (101)
  0x81, 0x00,        //   Input (Data, Array) - Key array (6 bytes)
  0xC0,              // End Collection

  // ---- Consumer Control (Report ID 2) ----
  0x05, 0x0C,        // Usage Page (Consumer)
  0x09, 0x01,        // Usage (Consumer Control)
  0xA1, 0x01,        // Collection (Application)
  0x85, 0x02,        //   Report ID (2)
  0x15, 0x00,        //   Logical Minimum (0)
  0x26, 0xFF, 0x03,  //   Logical Maximum (0x03FF)
  0x19, 0x00,        //   Usage Minimum (0)
  0x2A, 0xFF, 0x03,  //   Usage Maximum (0x03FF)
  0x75, 0x10,        //   Report Size (16)
  0x95, 0x01,        //   Report Count (1)
  0x81, 0x00,        //   Input (Data, Array, Absolute)
  0xC0,              // End Collection

  // ---- Mouse (Report ID 3) ----
  0x05, 0x01,        // Usage Page (Generic Desktop)
  0x09, 0x02,        // Usage (Mouse)
  0xA1, 0x01,        // Collection (Application)
  0x85, 0x03,        //   Report ID (3)
  0x09, 0x01,        //   Usage (Pointer)
  0xA1, 0x00,        //   Collection (Physical)
  0x05, 0x09,        //     Usage Page (Buttons)
  0x19, 0x01,        //     Usage Minimum (1)
  0x29, 0x05,        //     Usage Maximum (5)
  0x15, 0x00,        //     Logical Minimum (0)
  0x25, 0x01,        //     Logical Maximum (1)
  0x95, 0x05,        //     Report Count (5)
  0x75, 0x01,        //     Report Size (1)
  0x81, 0x02,        //     Input (Data, Variable, Absolute) - 5 buttons
  0x95, 0x01,        //     Report Count (1)
  0x75, 0x03,        //     Report Size (3)
  0x81, 0x01,        //     Input (Constant) - 3-bit padding
  0x05, 0x01,        //     Usage Page (Generic Desktop)
  0x09, 0x30,        //     Usage (X)
  0x09, 0x31,        //     Usage (Y)
  0x15, 0x00,        //     Logical Minimum (0)
  0x26, 0x10, 0x27,  //     Logical Maximum (10000 = 0x2710)
  0x75, 0x10,        //     Report Size (16)
  0x95, 0x02,        //     Report Count (2)
  0x81, 0x02,        //     Input (Data, Variable, Absolute) - X, Y
  0x09, 0x38,        //     Usage (Wheel)
  0x15, 0x81,        //     Logical Minimum (-127)
  0x25, 0x7F,        //     Logical Maximum (127)
  0x75, 0x08,        //     Report Size (8)
  0x95, 0x01,        //     Report Count (1)
  0x81, 0x06,        //     Input (Data, Variable, Relative) - Wheel
  0xA1, 0x02,        //     Collection (Logical)
  0x05, 0x0C,        //       Usage Page (Consumer)
  0x0A, 0x38, 0x02,  //       Usage (AC Pan = 0x0238)
  0x81, 0x06,        //       Input (Data, Variable, Relative) - Horizontal Pan
  0xC0,              //     End Collection (Logical)
  0xC0,              //   End Collection (Physical)
  0xC0               // End Collection (Application)
};

class HidReportCb : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic *c, NimBLEConnInfo &ci, uint16_t sub) override {
    uint16_t h = ci.getConnHandle();
    if (h >= 8) return;
    uint8_t bit = 0;
    if (c == kbdInput) bit = 1;
    else if (c == consumerInput) bit = 2;
    else if (c == mouseInput) bit = 4;
    if (sub & 1) hidSubByHandle[h] |= bit;
    else hidSubByHandle[h] &= ~bit;
  }
};

static HidReportCb hidReportCb;

void ble_hid_init(NimBLEServer *server) {
  hidDev = new NimBLEHIDDevice(server);
  hidDev->setManufacturer("Espressif");
  hidDev->setPnp(0x02, 0x303A, 0x8201, 0x0110);
  hidDev->setHidInfo(0x00, 0x01);
  hidDev->setBatteryLevel(100);
  hidDev->setReportMap((uint8_t *)kHidReportDescriptor, sizeof(kHidReportDescriptor));

  kbdInput = hidDev->getInputReport(1);
  consumerInput = hidDev->getInputReport(2);
  mouseInput = hidDev->getInputReport(3);

  kbdInput->setCallbacks(&hidReportCb);
  consumerInput->setCallbacks(&hidReportCb);
  mouseInput->setCallbacks(&hidReportCb);
}

bool ble_hid_connected() {
  for (int i = 0; i < 8; i++) {
    if (hidSubByHandle[i] != 0) return true;
  }
  return ble_app_connected();
}

void ble_hid_on_disconnect(uint16_t conn_handle) {
  if (conn_handle < 8) hidSubByHandle[conn_handle] = 0;
  if (!ble_hid_connected()) {
    mouseButtons = 0;
    kbdModifiers = 0;
    memset(kbdKeys, 0, sizeof(kbdKeys));
  }
}

static void sendMouseReport(uint16_t x, uint16_t y, int8_t wheel, int8_t pan) {
  if (!ble_hid_connected() || !mouseInput) return;
  uint8_t r[7] = {
    mouseButtons,
    (uint8_t)(x & 0xFF),
    (uint8_t)((x >> 8) & 0xFF),
    (uint8_t)(y & 0xFF),
    (uint8_t)((y >> 8) & 0xFF),
    (uint8_t)wheel,
    (uint8_t)pan,
  };
  mouseInput->setValue(r, sizeof(r));
  ble_notify_hid(mouseInput->getHandle(), r, sizeof(r));
}

void ble_hid_mouse_abs(uint16_t x, uint16_t y, int8_t wheel, int8_t pan) {
  if (x > 10000) x = 10000;
  if (y > 10000) y = 10000;
  absX = x;
  absY = y;
  sendMouseReport(absX, absY, wheel, pan);
}

void ble_hid_mouse_move(int8_t dx, int8_t dy, int8_t wheel, int8_t pan) {
  absX = (uint16_t)constrain((int)absX + (int)dx * 8, 0, 10000);
  absY = (uint16_t)constrain((int)absY + (int)dy * 4, 0, 10000);
  sendMouseReport(absX, absY, wheel, pan);
}

void ble_hid_mouse_press(uint8_t button) {
  mouseButtons |= button;
  sendMouseReport(absX, absY, 0, 0);
}

void ble_hid_mouse_release(uint8_t button) {
  mouseButtons &= ~button;
  sendMouseReport(absX, absY, 0, 0);
}

static void sendKbdReport() {
  if (!ble_hid_connected() || !kbdInput) return;
  uint8_t r[8] = {kbdModifiers, 0, kbdKeys[0], kbdKeys[1], kbdKeys[2], kbdKeys[3], kbdKeys[4], kbdKeys[5]};
  kbdInput->setValue(r, sizeof(r));
  ble_notify_hid(kbdInput->getHandle(), r, sizeof(r));
}

#define SHIFT_FLAG 0x80
static const uint8_t kAsciiToHid[128] = {
  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
  0x2A, 0x2B, 0x28, 0x00, 0x00, 0x00, 0x00, 0x00, // BS, TAB, ENTER
  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
  0x00, 0x00, 0x00, 0x29, 0x00, 0x00, 0x00, 0x00, // ESC
  0x2C, 0x1E|SHIFT_FLAG, 0x34|SHIFT_FLAG, 0x20|SHIFT_FLAG, 0x21|SHIFT_FLAG, 0x22|SHIFT_FLAG, 0x24|SHIFT_FLAG, 0x34, // ' ' .. '\''
  0x26|SHIFT_FLAG, 0x27|SHIFT_FLAG, 0x25|SHIFT_FLAG, 0x2E|SHIFT_FLAG, 0x36, 0x2D, 0x37, 0x38, // '(' .. '/'
  0x27, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, // '0' .. '7'
  0x25, 0x26, 0x33|SHIFT_FLAG, 0x33, 0x36|SHIFT_FLAG, 0x2E, 0x37|SHIFT_FLAG, 0x38|SHIFT_FLAG, // '8' .. '?'
  0x1F|SHIFT_FLAG, 0x04|SHIFT_FLAG, 0x05|SHIFT_FLAG, 0x06|SHIFT_FLAG, 0x07|SHIFT_FLAG, 0x08|SHIFT_FLAG, 0x09|SHIFT_FLAG, 0x0A|SHIFT_FLAG,
  0x0B|SHIFT_FLAG, 0x0C|SHIFT_FLAG, 0x0D|SHIFT_FLAG, 0x0E|SHIFT_FLAG, 0x0F|SHIFT_FLAG, 0x10|SHIFT_FLAG, 0x11|SHIFT_FLAG, 0x12|SHIFT_FLAG,
  0x13|SHIFT_FLAG, 0x14|SHIFT_FLAG, 0x15|SHIFT_FLAG, 0x16|SHIFT_FLAG, 0x17|SHIFT_FLAG, 0x18|SHIFT_FLAG, 0x19|SHIFT_FLAG, 0x1A|SHIFT_FLAG,
  0x1B|SHIFT_FLAG, 0x1C|SHIFT_FLAG, 0x1D|SHIFT_FLAG, 0x2F, 0x31, 0x30, 0x23|SHIFT_FLAG, 0x2D|SHIFT_FLAG,
  0x35, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A,
  0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12,
  0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A,
  0x1B, 0x1C, 0x1D, 0x2F|SHIFT_FLAG, 0x31|SHIFT_FLAG, 0x30|SHIFT_FLAG, 0x35|SHIFT_FLAG, 0x4C
};

static bool mapKey(uint8_t k, uint8_t *usage, uint8_t *mod) {
  *usage = 0;
  *mod = 0;
  if (k >= KEY_LEFT_CTRL && k <= KEY_RIGHT_GUI) {
    *mod = (uint8_t)(1u << (k - KEY_LEFT_CTRL));
    return true;
  }
  switch (k) {
    case KEY_UP_ARROW:    *usage = 0x52; return true;
    case KEY_DOWN_ARROW:  *usage = 0x51; return true;
    case KEY_LEFT_ARROW:  *usage = 0x50; return true;
    case KEY_RIGHT_ARROW: *usage = 0x4F; return true;
    case KEY_BACKSPACE:   *usage = 0x2A; return true;
    case KEY_TAB:         *usage = 0x2B; return true;
    case KEY_RETURN:      *usage = 0x28; return true;
    case KEY_ESC:         *usage = 0x29; return true;
    case KEY_CAPS_LOCK:   *usage = 0x39; return true;
    default: break;
  }
  if (k < 128) {
    uint8_t v = kAsciiToHid[k];
    if (!v) return false;
    if (v & SHIFT_FLAG) *mod = 0x02;  // Left Shift
    *usage = v & 0x7F;
    return true;
  }
  return false;
}

void ble_hid_key_press(uint8_t k) {
  uint8_t usage = 0, mod = 0;
  if (!mapKey(k, &usage, &mod)) return;
  kbdModifiers |= mod;
  if (usage) {
    for (int i = 0; i < 6; i++) {
      if (kbdKeys[i] == usage) break;
      if (kbdKeys[i] == 0) { kbdKeys[i] = usage; break; }
    }
  }
  sendKbdReport();
}

void ble_hid_key_release(uint8_t k) {
  uint8_t usage = 0, mod = 0;
  if (!mapKey(k, &usage, &mod)) return;
  kbdModifiers &= ~mod;
  if (usage) {
    for (int i = 0; i < 6; i++) {
      if (kbdKeys[i] == usage) kbdKeys[i] = 0;
    }
  }
  sendKbdReport();
}

void ble_hid_key_release_all() {
  kbdModifiers = 0;
  memset(kbdKeys, 0, sizeof(kbdKeys));
  sendKbdReport();
}

void ble_hid_key_print(const String &s) {
  for (size_t i = 0; i < s.length(); i++) {
    ble_hid_key_press((uint8_t)s[i]);
    delay(15);
    ble_hid_key_release_all();
    delay(15);
  }
}

void ble_hid_consumer_press(uint16_t usage) {
  if (!ble_hid_connected() || !consumerInput) return;
  uint8_t r[2] = {(uint8_t)(usage & 0xFF), (uint8_t)((usage >> 8) & 0xFF)};
  consumerInput->setValue(r, sizeof(r));
  ble_notify_hid(consumerInput->getHandle(), r, sizeof(r));
}

void ble_hid_consumer_release() {
  ble_hid_consumer_press(0);
}
