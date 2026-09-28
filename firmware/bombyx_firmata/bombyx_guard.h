/* bombyx_guard.h -- what BombyxFirmata lets through to ConfigurableFirmata, and what it refuses by name.
 *
 * WHY. The bench twin (tools/bench_twin: the flashed image, cycle by cycle on simavr) and its adversarial
 * verification (2026-09-15) found that ConfigurableFirmata 3.3.0
 * trusts every index a host sends:
 *   M-01  SET_PIN_MODE writes pinConfig[pin] and pinState[pin] with no bound (20 entries on the Uno), and past them lie
 *         the Firmata object's callback pointers: one message to pin 48 or 59-62 stopped the core. A stepper CONFIG
 *         hands its raw pins to the AVR core's pinMode/digitalWrite, which read past their pin tables (pin 127: wild
 *         writes; pin 50: one DDRD write switching D1-D4, D6 and D7 to output LOW -- M-03).
 *   M-02  a core stopped that way under a live permit leaves ENABLE LOW for good: the silence watchdog lives in loop().
 *   M-04  MULTISTEPPER commands are bounded by 10 steppers, not the 5 groups they index; a TO on a group never
 *         configured calls through a NULL MultiStepper.
 *   M-05  a group's member list indexes stepper[] unbounded and grows a count that sizes a stack array -- a count
 *         AccelStepperFirmata::reset() never zeroes (the review of v2: after a lapse or SYSTEM_RESET a guard's
 *         own bookkeeping is clean while the library's count keeps growing; 250 regroups took the stack to 54 bytes
 *         above the heap). The bench runs one stepper, so every MULTISTEPPER command is refused by name; groups come
 *         back only as a reviewed change together with a library-side reset of that count.
 *   M-06  a stepper CONFIG on D0/D1 steps the UART lines the host talks on.
 *   M-07  a CONFIG over a live device leaks the old AccelStepper; the 16th malloc returns NULL and the next one is
 *         constructed on it, writing 68 bytes over the register file.
 * The upstream library stays unmodified. The guard stands in front of it, in two places:
 *   - bombyx_filter_byte(): the byte stream BEFORE Firmata.parse() -- SET_PIN_MODE and SET_DIGITAL_PIN_VALUE are
 *     held until their pin is known, because Firmata's setPinMode() writes its tables before any callback runs;
 *   - bombyx_guard_sysex(): each sysex BEFORE FirmataExt's dispatcher -- pins, slots, stepper bounds, whole lengths,
 *     occupancy, and no MULTISTEPPER at all.
 * A refusal is NAMED, never silent (bombyx_safety.h's rule): the sketch sends the verdict back as a string.
 *
 * THIS FILE DECIDES; IT DOES NOT ACT, like bombyx_safety.h: plain C, no Arduino, compiled by the desk test
 * (tools/test_bombyx_firmata_guard.py) and by the sketch, so the thing tested is the thing flashed.
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */
#ifndef BOMBYX_FIRMATA_GUARD_H
#define BOMBYX_FIRMATA_GUARD_H

#include <stdint.h>
#include <stdbool.h>

/* ConfigurableFirmata 3.3.0 on the Uno: utility/Boards.h TOTAL_PINS, AccelStepperFirmata.h MAX_ACCELSTEPPERS,
 * ConfigurableFirmata.h MAX_DATA_BYTES (a desk test reads the pinned library's headers and holds these equal). */
#define BOMBYX_GUARD_TOTAL_PINS       20u
#define BOMBYX_GUARD_MAX_STEPPERS     10u
#define BOMBYX_GUARD_MAX_DATA_BYTES   64u

/* The CNC Shield V3's slots as os/sel4/arduino/wiring.json has them (a desk test holds these equal to it): a stepper
 * may be configured only on one of these STEP/DIR pairs. Slot A (D12/D13) is absent while its jumpers are unconfirmed. */
#define BOMBYX_GUARD_SLOTS 3u
static const uint8_t BOMBYX_GUARD_SLOT_STEP[BOMBYX_GUARD_SLOTS] = { 2u, 3u, 4u };
static const uint8_t BOMBYX_GUARD_SLOT_DIR[BOMBYX_GUARD_SLOTS]  = { 5u, 6u, 7u };

/* Firmata's wire bytes the guard reads */
#define BOMBYX_FM_START_SYSEX      0xF0u
#define BOMBYX_FM_END_SYSEX        0xF7u
#define BOMBYX_FM_SET_PIN_MODE     0xF4u
#define BOMBYX_FM_SET_PIN_VALUE    0xF5u
#define BOMBYX_FM_SYSTEM_RESET     0xFFu
#define BOMBYX_FM_PIN_STATE_QUERY  0x6Du
#define BOMBYX_FM_EXTENDED_ANALOG  0x6Fu
#define BOMBYX_FM_ACCELSTEPPER     0x62u
#define BOMBYX_FM_AS_CONFIG        0x00u
#define BOMBYX_FM_MS_CONFIG        0x20u
#define BOMBYX_FM_MS_TO            0x21u
#define BOMBYX_FM_MS_STOP          0x23u

typedef enum {
    BOMBYX_GUARD_OK = 0,
    BOMBYX_GUARD_PIN_OFF_BOARD,          /* a pin index the Uno does not have                         */
    BOMBYX_GUARD_STEPPER_NOT_A_SLOT,     /* STEP/DIR are not one of the shield's slot pairs           */
    BOMBYX_GUARD_STEPPER_NOT_A_DRIVER,   /* 2-, 3- or 4-wire: pins the shield does not route          */
    BOMBYX_GUARD_STEPPER_ENABLE_PIN,     /* an enable pin: ENABLE is the core's (D8), nobody else's   */
    BOMBYX_GUARD_STEPPER_OCCUPIED,       /* CONFIG over a live device leaks it: SYSTEM_RESET first    */
    BOMBYX_GUARD_DEVICE_RANGE,           /* a stepper device the firmware does not have               */
    BOMBYX_GUARD_GROUPS_NOT_OFFERED,     /* MULTISTEPPER: not on this bench (M-04, M-05, the review)  */
    BOMBYX_GUARD_MESSAGE_SHORT           /* fewer bytes than the command reads                        */
} bombyx_guard_verdict_t;

/* What the guard has let through, so occupancy can be refused: which stepper devices are configured. Cleared whenever
 * the firmware frees its steppers (a permit lapse, SYSTEM_RESET). */
typedef struct {
    uint16_t steppers;
} bombyx_guard_t;

static inline void bombyx_guard_reset(bombyx_guard_t *g)
{
    g->steppers = 0u;
}

static inline bombyx_guard_verdict_t bombyx_guard_pin(uint8_t pin)
{
    return pin < BOMBYX_GUARD_TOTAL_PINS ? BOMBYX_GUARD_OK : BOMBYX_GUARD_PIN_OFF_BOARD;
}

/* One sysex, as Firmata hands it to the sketch's callback (command, then its data bytes). Commits occupancy only for
 * what it lets through. */
static inline bombyx_guard_verdict_t bombyx_guard_sysex(bombyx_guard_t *g, uint8_t command, uint8_t argc, const uint8_t *argv)
{
    if (command == BOMBYX_FM_PIN_STATE_QUERY || command == BOMBYX_FM_EXTENDED_ANALOG) {
        if (argc < 1u) { return BOMBYX_GUARD_MESSAGE_SHORT; }
        return bombyx_guard_pin(argv[0]);
    }
    if (command != BOMBYX_FM_ACCELSTEPPER) { return BOMBYX_GUARD_OK; }
    if (argc < 2u) { return BOMBYX_GUARD_MESSAGE_SHORT; }
    uint8_t cmd = argv[0], dev = argv[1];

    if (cmd == BOMBYX_FM_MS_CONFIG || cmd == BOMBYX_FM_MS_TO || cmd == BOMBYX_FM_MS_STOP) {
        return BOMBYX_GUARD_GROUPS_NOT_OFFERED;
    }

    if (dev >= BOMBYX_GUARD_MAX_STEPPERS) { return BOMBYX_GUARD_DEVICE_RANGE; }
    /* Every AccelStepper subcommand reads its bytes whether they came or not, from Firmata's input buffer, where the
     * previous message's bytes are still lying: a short STEP (F0 62 02 00 F7) repeated the last move's 400 steps on
     * both images (the review, lead (b), RAN on the twin), and a CONFIG without its invert byte reads a stale
     * one (AccelStepperFirmata.cpp:153 tests argc >= index, one short). Each needs its whole length: CONFIG with the
     * invert byte, ZERO, STEP, TO (5-byte position), ENABLE, STOP, REPORT_POSITION, LIMIT (unhandled), SET_ACCELERATION
     * and SET_SPEED (4-byte float). */
    static const uint8_t NEED[10] = { 6u, 2u, 7u, 7u, 3u, 2u, 2u, 2u, 6u, 6u };
    if (cmd < 10u && argc < NEED[cmd]) { return BOMBYX_GUARD_MESSAGE_SHORT; }
    if (cmd != BOMBYX_FM_AS_CONFIG) { return BOMBYX_GUARD_OK; }  /* the library null-checks every other command */
    uint8_t iface = argv[2], step = argv[3], dir = argv[4];
    if (((iface & 0x70u) >> 4) != 1u) { return BOMBYX_GUARD_STEPPER_NOT_A_DRIVER; }
    if (iface & 0x01u) { return BOMBYX_GUARD_STEPPER_ENABLE_PIN; }
    bool slot = false;
    for (uint8_t i = 0u; i < BOMBYX_GUARD_SLOTS; i++) {
        if (step == BOMBYX_GUARD_SLOT_STEP[i] && dir == BOMBYX_GUARD_SLOT_DIR[i]) { slot = true; }
    }
    if (!slot) { return BOMBYX_GUARD_STEPPER_NOT_A_SLOT; }
    uint16_t bit = (uint16_t)(1u << dev);
    if (g->steppers & bit) { return BOMBYX_GUARD_STEPPER_OCCUPIED; }
    g->steppers |= bit;
    return BOMBYX_GUARD_OK;
}

/* The byte stream in front of Firmata.parse(): what may pass, byte by byte, mirroring Firmata's own framing so the two
 * never disagree about where a message starts.
 *   - SYSTEM_RESET passes at once and drops whatever is held (Firmata resets its parser on it in any state);
 *   - inside a sysex every byte passes (the sysex is judged whole by bombyx_guard_sysex); after MAX_DATA_BYTES data
 *     bytes Firmata discards the sysex and leaves it, and so does the filter;
 *   - SET_PIN_MODE / SET_DIGITAL_PIN_VALUE are held with their two data bytes and released only for a pin on the board;
 *     a command byte arriving first drops the held part, as Firmata drops an unfinished message;
 *   - everything else passes. */
typedef struct {
    uint8_t in_sysex;
    uint8_t sysex_n;
    uint8_t held[3];
    uint8_t held_n;
} bombyx_filter_t;

static inline void bombyx_filter_init(bombyx_filter_t *f)
{
    f->in_sysex = 0u;
    f->sysex_n = 0u;
    f->held_n = 0u;
}

/* Feed one byte. Writes the bytes to hand to Firmata.parse() into out (at most 3) and returns how many; sets *why to
 * the verdict when a held message is refused (else BOMBYX_GUARD_OK). */
static inline uint8_t bombyx_filter_byte(bombyx_filter_t *f, uint8_t b, uint8_t out[3], bombyx_guard_verdict_t *why)
{
    *why = BOMBYX_GUARD_OK;
    if (b == BOMBYX_FM_SYSTEM_RESET) {
        f->held_n = 0u;
        f->in_sysex = 0u;
        f->sysex_n = 0u;
        out[0] = b;
        return 1u;
    }
    if (f->held_n > 0u) {
        if (b < 0x80u) {
            f->held[f->held_n++] = b;
            if (f->held_n < 3u) { return 0u; }
            f->held_n = 0u;
            if (bombyx_guard_pin(f->held[1]) != BOMBYX_GUARD_OK) { *why = BOMBYX_GUARD_PIN_OFF_BOARD; return 0u; }
            out[0] = f->held[0]; out[1] = f->held[1]; out[2] = f->held[2];
            return 3u;
        }
        f->held_n = 0u;                                  /* an unfinished message, dropped as Firmata drops it */
    }
    if (f->in_sysex) {
        if (b == BOMBYX_FM_END_SYSEX) {
            f->in_sysex = 0u;
            f->sysex_n = 0u;
        } else if (f->sysex_n == BOMBYX_GUARD_MAX_DATA_BYTES) {
            f->in_sysex = 0u;                            /* Firmata: "Discarding input message, out of buffer" */
            f->sysex_n = 0u;
        } else {
            f->sysex_n++;
        }
        out[0] = b;
        return 1u;
    }
    if (b == BOMBYX_FM_START_SYSEX) {
        f->in_sysex = 1u;
        f->sysex_n = 0u;
        out[0] = b;
        return 1u;
    }
    if (b == BOMBYX_FM_SET_PIN_MODE || b == BOMBYX_FM_SET_PIN_VALUE) {
        f->held[0] = b;
        f->held_n = 1u;
        return 0u;
    }
    out[0] = b;
    return 1u;
}

#endif /* BOMBYX_FIRMATA_GUARD_H */
