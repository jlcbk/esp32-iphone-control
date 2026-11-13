// Composite BLE HID (Mouse + Keyboard + Consumer Control) sharing the NimBLE server in ble_link.cpp.
// Compatible with both iOS (AssistiveTouch pointer + keyboard) and Android (native mouse cursor + keyboard).
//
// Based on ESP32 iPhone Agent (https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan
// Copyright (c) 2026 Henry Yuan. Licensed under the MIT License (see LICENSE and NOTICE).
#pragma once
#include <Arduino.h>

class NimBLEServer;

// Attach the BLE HID service (0x1812), Device Info (0x180A), and Battery (0x180F) to `server`.
// Must be called before `server->start()`.
void ble_hid_init(NimBLEServer *server);

// True when a central (iPhone or Android) has subscribed to any BLE HID input report (mouse/keyboard).
bool ble_hid_connected();

// Notify callback hook called from ble_link.cpp when a peer subscribes/unsubscribes or disconnects.
void ble_hid_on_subscribe(uint16_t attr_handle, uint16_t conn_handle, bool subscribed);
void ble_hid_on_disconnect(uint16_t conn_handle);

// Mouse actions (Report ID 3)
void ble_hid_mouse_move(int8_t dx, int8_t dy, int8_t wheel = 0, int8_t pan = 0);
void ble_hid_mouse_abs(uint16_t x, uint16_t y, int8_t wheel = 0, int8_t pan = 0);
void ble_hid_mouse_press(uint8_t button);
void ble_hid_mouse_release(uint8_t button);

// Keyboard actions (Report ID 1, accepts Arduino USBHIDKeyboard ASCII / KEY_* codes)
void ble_hid_key_press(uint8_t k);
void ble_hid_key_release(uint8_t k);
void ble_hid_key_release_all();
void ble_hid_key_print(const String &s);

// Consumer control actions (Report ID 2, e.g., 0x0223 AC Home for iOS/Android home screen)
void ble_hid_consumer_press(uint16_t usage);
void ble_hid_consumer_release();
