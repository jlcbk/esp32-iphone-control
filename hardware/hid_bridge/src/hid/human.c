// Based on ESP32 iPhone Agent (https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan
// Copyright (c) 2026 Henry Yuan. Licensed under the MIT License (see LICENSE and NOTICE).

#include "human.h"

#include <math.h>

static uint32_t xorshift32(uint32_t *state) {
  uint32_t x = *state;
  if (x == 0) x = 0x9E3779B9u;
  x ^= x << 13;
  x ^= x >> 17;
  x ^= x << 5;
  *state = x;
  return x;
}

// Uniform float in [0.0, 1.0)
static double rand_unit(uint32_t *state) {
  return (double)(xorshift32(state) & 0xFFFFFFu) / (double)0x1000000u;
}

// Uniform float in [lo, hi]
static double rand_range(uint32_t *state, double lo, double hi) {
  return lo + (hi - lo) * rand_unit(state);
}

// Approximate Gaussian in [-1.0, 1.0] (sum of 3 uniforms, centered at 0)
static double rand_bell(uint32_t *state) {
  return (rand_unit(state) + rand_unit(state) + rand_unit(state)) / 1.5 - 1.0;
}

static double clamp_box(double v, double a, double b) {
  double lo = a < b ? a : b;
  double hi = a < b ? b : a;
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

void human_move_start(human_move_t *m, long dx, long dy, int step, int base_step_ms, uint32_t seed, int human_enabled) {
  if (step < 1) step = 1;
  if (base_step_ms < 2) base_step_ms = 2;
  m->dx = dx;
  m->dy = dy;
  m->cur_x = 0;
  m->cur_y = 0;
  m->i = 0;
  m->rng = seed ? seed : 0xA53C9E17u;
  m->base_step_ms = base_step_ms;
  m->wind_x = 0.0;
  m->wind_y = 0.0;
  m->over_x = 0.0;
  m->over_y = 0.0;

  double len = hypot((double)dx, (double)dy);
  if (len <= 0.0) {
    m->n = 0;
    m->human_enabled = 0;
    return;
  }

  // Short nudges (<= 15 units, e.g., Nudge=12 pointer finder) stay crisp and linear.
  if (!human_enabled || len <= 15.0) {
    m->human_enabled = 0;
    long n = (long)ceil(len / (double)step);
    if (n < 1) n = 1;
    m->n = (int)n;
    return;
  }

  m->human_enabled = 1;

  // Path arc-length is slightly longer than chord (~5%), plus extra fine aiming steps near target.
  double raw_steps = ceil((len * 1.05) / (double)step) + 2.0;
  if (raw_steps < 4.0) raw_steps = 4.0;
  if (raw_steps > (double)HUMAN_MAX_STEPS) raw_steps = (double)HUMAN_MAX_STEPS;
  m->n = (int)raw_steps;

  // Unit vector along chord (ux, uy) and perpendicular normal (nx, ny)
  double ux = (double)dx / len;
  double uy = (double)dy / len;
  double nx = -uy;
  double ny = ux;

  // Natural wrist arc amplitude: 6%..14% of distance, capped at 36 units
  double arc_sign = (xorshift32(&m->rng) & 1u) ? 1.0 : -1.0;
  double arc_mag = len * rand_range(&m->rng, 0.06, 0.14);
  if (arc_mag > 36.0) arc_mag = 36.0;

  double t1 = rand_range(&m->rng, 0.22, 0.36);
  double t2 = rand_range(&m->rng, 0.64, 0.78);
  double n1 = arc_sign * arc_mag * rand_range(&m->rng, 0.75, 1.15);
  // Slight S-curve (20% chance) or C-curve (80% chance, most common in human wrist motion)
  double s_factor = (rand_unit(&m->rng) < 0.20) ? -0.35 : 0.85;
  double n2 = arc_sign * arc_mag * s_factor * rand_range(&m->rng, 0.65, 1.10);

  m->p1x = (double)dx * t1 + nx * n1;
  m->p1y = (double)dy * t1 + ny * n1;
  m->p2x = (double)dx * t2 + nx * n2;
  m->p2y = (double)dy * t2 + ny * n2;

  // When both axes move, clamp control points to the [0, dx] x [0, dy] convex hull
  // so a move starting from (0,0) after `home` never clips against the iPhone screen border.
  if (dx != 0 && dy != 0) {
    m->p1x = clamp_box(m->p1x, 0.0, (double)dx);
    m->p1y = clamp_box(m->p1y, 0.0, (double)dy);
    m->p2x = clamp_box(m->p2x, 0.0, (double)dx);
    m->p2y = clamp_box(m->p2y, 0.0, (double)dy);
  } else {
    // Purely axial move (e.g., vertical swipe dx=0, dy=-400): cap lateral bow to 10 units
    if (dx == 0) {
      m->p1x = clamp_box(m->p1x, -10.0, 10.0);
      m->p2x = clamp_box(m->p2x, -10.0, 10.0);
    } else {
      m->p1y = clamp_box(m->p1y, -10.0, 10.0);
      m->p2y = clamp_box(m->p2y, -10.0, 10.0);
    }
  }

  // Subtle Fitts's Law overshoot on medium/long moves (30% chance when len > 80)
  if (len > 80.0 && rand_unit(&m->rng) < 0.30) {
    double ov = rand_range(&m->rng, 1.5, 3.5);
    m->over_x = ux * ov;
    m->over_y = uy * ov;
  }
}

// Minimum-jerk ease-in-out curve blended with linear progression so per-report step size
// stays within iOS's calibrated acceleration band while wall-clock timing (dt_ms) and
// step modulation produce a natural human velocity profile.
static double human_progress(double tau) {
  if (tau <= 0.0) return 0.0;
  if (tau >= 1.0) return 1.0;
  double mj = tau * tau * tau * (10.0 + tau * (-15.0 + 6.0 * tau));
  return 0.25 * mj + 0.75 * tau;
}

int human_move_next(human_move_t *m, int *sx, int *sy, int *dt_ms) {
  if (m->i >= m->n) return 0;
  int idx = m->i++;

  if (!m->human_enabled) {
    long next_x = lround((double)m->dx * (double)(idx + 1) / (double)m->n);
    long next_y = lround((double)m->dy * (double)(idx + 1) / (double)m->n);
    *sx = (int)(next_x - m->cur_x);
    *sy = (int)(next_y - m->cur_y);
    m->cur_x = next_x;
    m->cur_y = next_y;
    *dt_ms = m->base_step_ms;
    return 1;
  }

  double tau = (double)(idx + 1) / (double)m->n;
  long next_x, next_y;

  if (idx + 1 == m->n) {
    // Final step always lands on exact (dx, dy)
    next_x = m->dx;
    next_y = m->dy;
  } else {
    double u = human_progress(tau);
    double inv = 1.0 - u;

    // Cubic Bezier evaluation B(u) with P0=(0,0), P3=(dx,dy)
    double b1 = 3.0 * inv * inv * u;
    double b2 = 3.0 * inv * u * u;
    double b3 = u * u * u;

    double bx = b1 * m->p1x + b2 * m->p2x + b3 * (double)m->dx;
    double by = b1 * m->p1y + b2 * m->p2y + b3 * (double)m->dy;

    // WindMouse damped stochastic drift (decays to 0 at start and end via sin(pi*u))
    m->wind_x = 0.70 * m->wind_x + rand_bell(&m->rng) * 0.75;
    m->wind_y = 0.70 * m->wind_y + rand_bell(&m->rng) * 0.75;
    double env = sin(3.14159265358979323846 * u);

    // Overshoot bump peaked near u = 0.86, smoothly returning to 0 at u = 1.0
    double ov_env = 0.0;
    if (u > 0.70) {
      double w = (u - 0.70) / 0.30;  // 0..1
      ov_env = sin(3.14159265358979323846 * w);
    }

    double px = bx + m->wind_x * env + m->over_x * ov_env;
    double py = by + m->wind_y * env + m->over_y * ov_env;
    if (m->dx != 0) px = clamp_box(px, 0.0, (double)m->dx + m->over_x);
    if (m->dy != 0) py = clamp_box(py, 0.0, (double)m->dy + m->over_y);

    next_x = lround(px);
    next_y = lround(py);
  }

  long step_x = next_x - m->cur_x;
  long step_y = next_y - m->cur_y;
  if (step_x > 127) step_x = 127;
  if (step_x < -127) step_x = -127;
  if (step_y > 127) step_y = 127;
  if (step_y < -127) step_y = -127;

  *sx = (int)step_x;
  *sy = (int)step_y;
  m->cur_x += step_x;
  m->cur_y += step_y;

  // Human timing profile: slower initiation (tau < 0.2) and slower Fitts's Law aiming (tau > 0.78),
  // faster ballistic phase in the middle, plus per-packet physiological timing jitter.
  double speed_bell = sin(3.14159265358979323846 * tau);  // 0 at ends, 1 in middle
  double mult = 1.65 - 0.85 * speed_bell;                 // ~1.65x delay at ends, ~0.80x in middle
  if (tau > 0.80) {
    mult += (tau - 0.80) * 2.5;  // extra deceleration while settling onto target
  }
  double jitter = rand_bell(&m->rng) * 1.8;
  int d = (int)lround((double)m->base_step_ms * mult + jitter);
  if (d < 4) d = 4;
  if (d > 24) d = 24;
  *dt_ms = d;

  return 1;
}

void human_click_params(uint32_t seed, int *pre_delay_ms, int *hold_ms, int *jitter_x, int *jitter_y) {
  uint32_t rng = seed ? seed : 0xC11C7EEDu;
  // Pause between cursor stopping and finger pressing down: 14..38 ms
  *pre_delay_ms = 14 + (int)(xorshift32(&rng) % 25u);
  // Bell-shaped press hold duration: 52..126 ms (centered around ~82 ms, never fixed 60 ms)
  double b = (rand_unit(&rng) + rand_unit(&rng) + rand_unit(&rng)) / 3.0;  // 0..1 bell
  *hold_ms = 52 + (int)lround(b * 74.0);
  // 25% chance of a 1-unit physiological micro-tremor during press
  if ((xorshift32(&rng) & 3u) == 0u) {
    *jitter_x = (xorshift32(&rng) & 1u) ? 1 : -1;
    *jitter_y = 0;
  } else {
    *jitter_x = 0;
    *jitter_y = 0;
  }
}

void human_key_params(uint32_t seed, char ch, int *hold_ms, int *flight_ms) {
  uint32_t rng = (seed ? seed : 0x719E5EEDu) ^ ((uint32_t)(unsigned char)ch * 2654435761u);
  double b1 = (rand_unit(&rng) + rand_unit(&rng)) * 0.5;
  double b2 = (rand_unit(&rng) + rand_unit(&rng)) * 0.5;
  *hold_ms = 24 + (int)lround(b1 * 34.0);    // 24..58 ms key hold
  *flight_ms = 48 + (int)lround(b2 * 82.0);  // 48..130 ms inter-key delay
  if (ch == ' ' || ch == '.' || ch == ',' || ch == '\n') {
    *flight_ms += 35 + (int)(xorshift32(&rng) % 45u);  // natural pause at word/clause boundaries
  }
}
