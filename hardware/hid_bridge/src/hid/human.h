// Based on ESP32 iPhone Agent (https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan
// Copyright (c) 2026 Henry Yuan. Licensed under the MIT License (see LICENSE and NOTICE).
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Human-like mouse trajectory generator (Cubic Bezier + WindMouse micro-tremor + Minimum-Jerk timing).
// Generates relative HID steps (sx, sy) and inter-report delays (dt_ms) whose telescoping sum
// is guaranteed to equal (dx, dy) with zero cumulative rounding drift.

#define HUMAN_MAX_STEPS 256

typedef struct {
  long dx, dy;
  long cur_x, cur_y;
  int n, i;
  // Cubic Bezier control points in mouse units relative to start (0,0)
  double p1x, p1y;
  double p2x, p2y;
  // WindMouse state for organic micro-tremor perpendicular/along path
  double wind_x, wind_y;
  // Overshoot vector at the tail end of long moves (retracted at u=1.0)
  double over_x, over_y;
  uint32_t rng;
  int base_step_ms;
  int human_enabled;
} human_move_t;

// Initialize a human-like move from (0,0) to (dx, dy).
// step: target average report magnitude (e.g. 10 to match iOS calibration).
// base_step_ms: base report interval in ms (e.g. 8).
// seed: random seed (e.g. esp_random() or micros()).
// human_enabled: 1 for curved/variable-timing human move, 0 for linear constant-paced move.
void human_move_start(human_move_t *m, long dx, long dy, int step, int base_step_ms, uint32_t seed, int human_enabled);

// Produces the next relative HID report (sx, sy) and the delay after it (dt_ms).
// Returns 1 while steps remain, 0 when the move is complete.
int human_move_next(human_move_t *m, int *sx, int *sy, int *dt_ms);

// Generate human-like click timing parameters from a seed:
// pre_delay_ms: pause after cursor stops before pressing button (12..38 ms)
// hold_ms: button press duration (52..128 ms, bell-shaped distribution)
// jitter_x, jitter_y: optional micro-tremor (-1..1) during press (retracted before release)
void human_click_params(uint32_t seed, int *pre_delay_ms, int *hold_ms, int *jitter_x, int *jitter_y);

// Generate human-like keystroke timing for character `ch`:
// hold_ms: key down duration (22..58 ms)
// flight_ms: delay before next key (45..165 ms, longer on transitions/punctuation)
void human_key_params(uint32_t seed, char ch, int *hold_ms, int *flight_ms);

#ifdef __cplusplus
}
#endif
