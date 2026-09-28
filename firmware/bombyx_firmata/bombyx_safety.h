/* bombyx_safety.h -- the two safety properties BombyxFirmata adds to stock Firmata (A1).
 *
 * THERE IS NO SKETCH YET. Corrected 2026-09-09, an hour after this file was written: the
 * paragraph here said the header is "compiled by BOTH the sketch and the host harness".
 * Only the harness compiles it. `bombyx_firmata/` contains this file and nothing else --
 * no .ino, and the upstream Firmata library is not vendored. Jan asked "we are using
 * firmata right?" and the honest answer exposed the over-claim.
 *
 * WHY A HEADER, THEN. When the sketch IS written it will be built against the upstream
 * Firmata library, which cannot be compiled on the desk. Safety logic living inside the
 * .ino could only ever be proven by flashing, and flashing is Jan's call -- so it
 * would ship unproven. Pure C here, it is compiled by the host harness today and by the
 * sketch when that exists, so the thing tested is the thing flashed. One description per
 * object. That is the DESIGN; the second half of it is not built.
 *
 * WHAT IT REQUIRES:
 *   "StandardFirmata + 500 ms silence watchdog + D8 default-disabled and refused as a pin
 *    target; flashed once"
 * and its gate, which must be seen to go RED first:
 *   "pull USB mid-move -> stops within 500 ms; digital_write(D8, LOW) refused"
 *
 * THE TWO PROPERTIES, and why each is here rather than in the core:
 *
 *  1. THE SILENCE WATCHDOG. The permit is a stream, not an event: the core says how long a
 *     move may run and the firmware must stop when the stream stops. A watchdog on the
 *     ARDUINO survives things the core cannot reach -- an unplugged USB lead, a wedged host,
 *     a crashed transport. That is the point: it does not trust its own commander. 500 ms is
 *     the plan's number, not a guess of mine.
 *
 *  2. D8 IS NOT A PIN. On the CNC Shield V3, D8 is ENABLE, common to every driver, ACTIVE LOW
 *     (HIGH = all drivers de-energized = safe). It is the core's authority over whether the
 *     machine can move at all. A general pin-server whose whole selling point is "every pin,
 *     no reflash" would hand that authority to whatever composes a Firmata message. So D8 is
 *     refused as a target and the refusal is NAMED, never silent -- a pin write that quietly
 *     does nothing is indistinguishable from one that worked.
 *
 * THIS FILE DECIDES; IT DOES NOT ACT. It touches no pin and no serial port. The caller applies
 * the verdict. That keeps it host-testable without faking the world, and keeps the actuation
 * in one place in the sketch, when there is one.
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */
#ifndef BOMBYX_FIRMATA_SAFETY_H
#define BOMBYX_FIRMATA_SAFETY_H

#include <stdint.h>
#include <stdbool.h>

/* The plan's number. Named so the test and the sketch cannot disagree about it. */
#define BOMBYX_SILENCE_LIMIT_MS 500u

/* CNC Shield V3 / Uno: D8 is ENABLE, common to all drivers, active LOW.
 * Kept as a list rather than a single constant because "the pins the core owns" is the
 * concept; today it has one member. A second one must not require a new mechanism. */
#define BOMBYX_RESERVED_PIN_ENABLE 8

typedef enum {
    BOMBYX_PIN_OK = 0,              /* the composing half may drive this pin              */
    BOMBYX_PIN_RESERVED_ENABLE = 1, /* D8: the core's authority, never the composer's     */
    BOMBYX_PIN_OUT_OF_RANGE    = 2  /* not a pin on this board                            */
} bombyx_pin_verdict_t;

typedef struct {
    uint32_t last_msg_ms;   /* when a valid message last arrived                          */
    bool     seen_any_msg;  /* false until the first one -- boot is not "silent since 0"  */
    bool     permitted;     /* the composer's request: may the drivers be energized       */
} bombyx_safety_t;

/* SAFE AT POWER-ON, like the CNC doer: nothing is permitted until something says so. */
static inline void bombyx_safety_init(bombyx_safety_t *s)
{
    s->last_msg_ms  = 0u;
    s->seen_any_msg = false;
    s->permitted    = false;
}

/* Call on every VALID inbound message. Only valid ones: feeding the watchdog on garbage
 * would let a noisy line hold the machine energized, which is the failure the watchdog is
 * for. */
static inline void bombyx_safety_note_message(bombyx_safety_t *s, uint32_t now_ms)
{
    s->last_msg_ms  = now_ms;
    s->seen_any_msg = true;
}

static inline void bombyx_safety_set_permitted(bombyx_safety_t *s, bool p)
{
    s->permitted = p;
}

/* True once nothing valid has arrived for BOMBYX_SILENCE_LIMIT_MS.
 *
 * Before the FIRST message there is no stream to have stopped, so this is false -- but
 * `permitted` is also false then, so the machine is still de-energized. Boot safety comes
 * from the permit, not from the watchdog; conflating them would make a board that has simply
 * not been spoken to yet indistinguishable from one whose host died.
 *
 * Unsigned subtraction, so millis() rollover at ~49.7 days is handled without a special case. */
static inline bool bombyx_safety_is_silent(const bombyx_safety_t *s, uint32_t now_ms)
{
    if (!s->seen_any_msg) return false;
    return (uint32_t)(now_ms - s->last_msg_ms) >= BOMBYX_SILENCE_LIMIT_MS;
}

/* THE ONE QUESTION THE SKETCH ASKS: may the drivers be energized right now?
 * Both conditions must hold. Either alone is a way to move a machine nobody is commanding. */
static inline bool bombyx_safety_may_energize(const bombyx_safety_t *s, uint32_t now_ms)
{
    return s->permitted && !bombyx_safety_is_silent(s, now_ms);
}

/* Is this pin the composing half's to drive? */
static inline bombyx_pin_verdict_t bombyx_safety_pin_verdict(uint8_t pin)
{
    if (pin >= 32u)                        return BOMBYX_PIN_OUT_OF_RANGE;
    if (pin == BOMBYX_RESERVED_PIN_ENABLE) return BOMBYX_PIN_RESERVED_ENABLE;
    return BOMBYX_PIN_OK;
}

/* A refusal that can be sent back. Never NULL, so a caller cannot accidentally print
 * nothing and turn a refusal into silence. */
static inline const char *bombyx_safety_verdict_name(bombyx_pin_verdict_t v)
{
    switch (v) {
        case BOMBYX_PIN_OK:              return "ok";
        case BOMBYX_PIN_RESERVED_ENABLE: return "D8 is ENABLE and belongs to the core";
        case BOMBYX_PIN_OUT_OF_RANGE:    return "not a pin on this board";
        default:                         return "unknown verdict";
    }
}

#endif /* BOMBYX_FIRMATA_SAFETY_H */
