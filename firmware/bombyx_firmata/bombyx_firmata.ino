/* bombyx_firmata -- ConfigurableFirmata, plus the two things the core will not delegate (A1).
 *
 * The requirement: "StandardFirmata + 500 ms silence watchdog + D8 default-disabled and
 * refused as a pin target; flashed once". Jan, 2026-09-09: "that makes it possible to
 * control the full arduino over USB".
 *
 * WHY ConfigurableFirmata AND NOT StandardFirmata. Stock Firmata drives pins by USB
 * round-trip, so it cannot make the stepper's 600 us pulses at 1600 steps/rev -- flashing it
 * would buy every pin and lose the motor path that H5 first metal proved. ConfigurableFirmata
 * ships AccelStepperFirmata, which keeps step GENERATION on the Arduino and takes "move N
 * steps" as one message. Breadth and the motor, instead of a trade.
 *
 * The upstream library is used unmodified. Everything Bombyx adds is in this file and in
 * bombyx_safety.h, which the host harness compiles too -- so the safety decisions are proven
 * on the desk (tools/test_firmata_safety_gate.py) and only their WIRING waits on a flash.
 *
 * ── THE TWO ADDITIONS ────────────────────────────────────────────────────────────────────
 *
 * 1. D8 IS NOT A PIN. On the CNC Shield V3 it is ENABLE, common to every driver, ACTIVE LOW
 *    (HIGH = all drivers de-energized = safe). It is the core's authority over whether the
 *    machine can move at all. Handing it to a general pin-server would mean whatever composes
 *    a Firmata message could take that authority. Firmata's own PIN_MODE_IGNORE does exactly
 *    the right thing: digitalWrite refuses it AND the capability response stops advertising
 *    it, so a client cannot even discover it as a target. Upstream mechanism, not a bolt-on.
 *
 * 2. THE PERMIT IS A STREAM, NOT AN EVENT. The drivers stay energized only while a permit
 *    keeps arriving. Silence for 500 ms de-energizes -- and silence is what an unplugged USB
 *    lead, a wedged host or a crashed transport all look like from here. The watchdog runs on
 *    the ARDUINO precisely so it survives things the core cannot reach; it does not trust its
 *    own commander.
 *
 *    ONLY THE PERMIT SYSEX FEEDS IT -- deliberately, not incidentally. Feeding the watchdog on
 *    "any traffic" would let a noisy line, a chatty client, or a client polling analog inputs
 *    hold a machine energized with nobody commanding it. So the permit is its own message and
 *    nothing else counts.
 *
 * ── WIRE FORMAT ──────────────────────────────────────────────────────────────────────────
 *    START_SYSEX, 0x01, <permit>, END_SYSEX      permit: 1 = may energize, 0 = stop now
 * 0x01 is inside Firmata's user-defined sysex range (0x00-0x0F), so it cannot collide with a
 * feature command.
 *
 * ── WHAT IS PHYSICALLY ON THIS BOARD ─────────────────────────────────────────────────────
 * Jan, 2026-09-09: "remember the shield and driver are on the board -- so when testing the
 * board keep the shield in mind."
 *
 * A CNC Shield V3 and a TMC2208 are MOUNTED. These are not free pins (os/sel4/arduino/
 * wiring.json is the source; this is a reader's note, not a second copy of the truth):
 *
 *     D2 / D5   STEP / DIR   slot X        D8   ENABLE, all drivers, ACTIVE LOW
 *     D3 / D6   STEP / DIR   slot Y        1/8 microstepping, 1600 steps/rev
 *     D4 / D7   STEP / DIR   slot Z
 *
 * So "blink a pin to see if Firmata works" is not harmless here: D2-D7 are step and
 * direction lines into a real driver with a real motor on it. D13 is the safe one.
 *
 * AND THIS IS EXACTLY WHY D8 IS THE CORE'S. With ENABLE de-asserted the drivers are
 * de-energized and STEP/DIR are inert -- a client can toggle them all day and nothing turns.
 * The single guarded pin is what makes it safe to offer every OTHER pin over USB. Hand D8 to
 * the composing half and that property is gone, along with the reason the gate exists.
 *
 * Per-pin policy for D2-D7 is a separate job ("per-pin policy from wiring.json"),
 * not A1's. A1's contract is narrower and it is met: ENABLE is unreachable, and silence
 * de-energizes.
 *
 * FLASHED at 6abceaf (2026-09-13, A1). What follows that line is NOT flashed: flashing is Jan's
 * call, and this file being correct is not the same as it running.
 *
 * ── THE GUARD (2026-09-15, reviewed) ─────────────────────────────
 * The bench twin ran the flashed image cycle by cycle and an adversarial verification found that
 * ConfigurableFirmata trusts every index a host sends (the twin's firmware
 * findings). One SET_PIN_MODE to pin 48 stopped the core; under a live permit that leaves
 * ENABLE LOW for good, because the silence watchdog lives in loop() (M-01, M-02). So:
 *   - bombyx_guard.h stands in front of the library, byte by byte before Firmata.parse() and
 *     sysex by sysex before FirmataExt: pins on the board, steppers only on the shield's slot
 *     pairs, stepper bounds and whole lengths, no CONFIG over a live device, no MULTISTEPPER
 *     at all -- the library never resets its group count (M-01, M-03..M-07, the review). A
 *     refusal is named;
 *   - the AVR's own watchdog backs the loop: a core that stops resets within a second, and
 *     setup() drives ENABLE HIGH before anything else (M-02);
 *   - ENABLE's level is written before its direction, so a reset no longer pulses it LOW (M-15);
 *   - SYSTEM_RESET runs the features' resets and withdraws the permit (M-33).
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */
#include <ConfigurableFirmata.h>

#include <DigitalInputFirmata.h>
DigitalInputFirmata digitalInput;

#include <DigitalOutputFirmata.h>
DigitalOutputFirmata digitalOutput;

#include <AnalogInputFirmata.h>
AnalogInputFirmata analogInput;

#include <AnalogOutputFirmata.h>
AnalogOutputFirmata analogOutput;

/* Keeps step generation on the Arduino -- see the note above. */
#include <AccelStepperFirmata.h>
AccelStepperFirmata accelStepper;

#include <FirmataExt.h>
FirmataExt firmataExt;

#include <FirmataReporting.h>
FirmataReporting reporting;

#include <avr/wdt.h>

#include "bombyx_safety.h"
#include "bombyx_guard.h"

/* The CNC Shield V3's ENABLE line. bombyx_safety.h owns the number; this is the same pin the
 * doer firmware calls PIN_ENABLE, and it is active LOW. */
static const uint8_t PIN_ENABLE = BOMBYX_RESERVED_PIN_ENABLE;

/* Bombyx's permit command, in Firmata's user-defined sysex range. */
static const byte BOMBYX_SYSEX_PERMIT = 0x01;

static bombyx_safety_t g_safety;
static bool            g_energized_now = false;
static bombyx_guard_t  g_guard;
static bombyx_filter_t g_filter;

/* A refusal, by name (bombyx_safety.h's rule: a refusal is never silent). Flash strings: SRAM is
 * what the leak in M-07 ran out of. */
static void sendRefusal(bombyx_guard_verdict_t v)
{
    switch (v) {
        case BOMBYX_GUARD_PIN_OFF_BOARD:        Firmata.sendString(F("bombyx: refused: not a pin on this board")); break;
        case BOMBYX_GUARD_STEPPER_NOT_A_SLOT:   Firmata.sendString(F("bombyx: refused: a stepper goes on a shield slot's STEP/DIR pair")); break;
        case BOMBYX_GUARD_STEPPER_NOT_A_DRIVER: Firmata.sendString(F("bombyx: refused: a stepper here is a STEP/DIR driver")); break;
        case BOMBYX_GUARD_STEPPER_ENABLE_PIN:   Firmata.sendString(F("bombyx: refused: ENABLE is D8 and the core's")); break;
        case BOMBYX_GUARD_STEPPER_OCCUPIED:     Firmata.sendString(F("bombyx: refused: that stepper is configured; SYSTEM_RESET first")); break;
        case BOMBYX_GUARD_DEVICE_RANGE:         Firmata.sendString(F("bombyx: refused: no such stepper device")); break;
        case BOMBYX_GUARD_GROUPS_NOT_OFFERED:   Firmata.sendString(F("bombyx: refused: stepper groups are not offered on this bench")); break;
        case BOMBYX_GUARD_MESSAGE_SHORT:        Firmata.sendString(F("bombyx: refused: message too short")); break;
        default:                                Firmata.sendString(F("bombyx: refused")); break;
    }
}

/* The ONE place that drives ENABLE. Everything else asks bombyx_safety_may_energize(). */
static void applyEnable(bool energize)
{
    /* ACTIVE LOW: LOW energizes, HIGH is safe. */
    digitalWrite(PIN_ENABLE, energize ? LOW : HIGH);
    g_energized_now = energize;
}

static void bombyxSysexCallback(byte command, byte argc, byte *argv)
{
    /* Firmata holds ONE sysex callback (FirmataClass::attach ignores the command and overwrites
     * currentSysexCallback). FirmataExt's constructor registered handleSysexCallback there, and
     * attaching this function in setup() REPLACED it -- so until 2026-09-13 every other sysex
     * was dropped on the floor: CAPABILITY_QUERY, PIN_STATE_QUERY and ACCELSTEPPER_DATA, i.e. the
     * very stepper path this sketch chose ConfigurableFirmata for. Found on the Uno, not the desk:
     * the flashed sketch answered REPORT_FIRMWARE (handled by the core before callbacks) and was
     * silent to a capability query. Everything that is not Bombyx's permit goes to FirmataExt.
     * D8 stays out of reach on that path too: setPinMode refuses a PIN_MODE_IGNORE pin, and
     * AccelStepperFirmata refuses an IGNORE pin as step, direction or ENABLE. */
    if (command != BOMBYX_SYSEX_PERMIT) {
        /* ...once the guard has let it through: pins, slots, bounds and occupancy (M-01, M-03..M-07). */
        const bombyx_guard_verdict_t v = bombyx_guard_sysex(&g_guard, command, argc, argv);
        if (v != BOMBYX_GUARD_OK) sendRefusal(v);
        else handleSysexCallback(command, argc, argv);
        return;
    }
    if (argc < 1) {
        Firmata.sendString(F("bombyx: permit needs 1 byte"));
        return;
    }
    /* A permit refreshes the watchdog; a withdrawal stops immediately rather than waiting
     * out the 500 ms, because a stop that is asked for should not be slower than one caused
     * by a broken cable. */
    bombyx_safety_set_permitted(&g_safety, argv[0] != 0);
    bombyx_safety_note_message(&g_safety, millis());
}

/* SYSTEM_RESET, which this sketch used to leave unattached: Firmata then reset only its parser,
 * so the features' resets never ran and a host's 0xFF did not stop a move (M-33). Now it resets
 * every feature (steppers freed, reporting back to its default), and a reset is a stop: the permit
 * is withdrawn at once and ENABLE driven HIGH -- level before direction (M-15). */
static void bombyxSystemResetCallback(void)
{
    firmataExt.reset();
    bombyx_guard_reset(&g_guard);
    bombyx_safety_set_permitted(&g_safety, false);
    Firmata.setPinMode(PIN_ENABLE, PIN_MODE_IGNORE);
    applyEnable(false);
    pinMode(PIN_ENABLE, OUTPUT);
}

void setup()
{
    /* A watchdog reset leaves the watchdog running; clear it before the version blink, which
     * waits longer than any timeout here. (Optiboot does this too, on the bench; not every
     * start comes through Optiboot.) */
    MCUSR = 0;
    wdt_disable();

    /* SAFE BEFORE ANYTHING ELSE -- before Firmata, before Serial. The doer firmware does the
     * same, and for the same reason: a board that is powering up must not be a board that is
     * briefly energized. The LEVEL before the DIRECTION: pinMode(OUTPUT) first drove ENABLE LOW
     * for 4 us at every reset, PORTB0 still 0 (M-15). */
    digitalWrite(PIN_ENABLE, HIGH);
    pinMode(PIN_ENABLE, OUTPUT);
    bombyx_safety_init(&g_safety);
    bombyx_guard_reset(&g_guard);
    bombyx_filter_init(&g_filter);
    g_energized_now = false;

    Firmata.setFirmwareNameAndVersion("BombyxFirmata", FIRMATA_FIRMWARE_MAJOR_VERSION,
                                      FIRMATA_FIRMWARE_MINOR_VERSION);

    /* D8 is refused as a target AND withheld from the capability response, so a client cannot
     * discover it, let alone drive it. */
    Firmata.setPinMode(PIN_ENABLE, PIN_MODE_IGNORE);

    firmataExt.addFeature(digitalInput);
    firmataExt.addFeature(digitalOutput);
    firmataExt.addFeature(analogInput);
    firmataExt.addFeature(analogOutput);
    firmataExt.addFeature(accelStepper);
    firmataExt.addFeature(reporting);

    Firmata.attach(START_SYSEX, bombyxSysexCallback);
    Firmata.attach(SYSTEM_RESET, bombyxSystemResetCallback);

    Firmata.begin(57600);
    Firmata.parse(SYSTEM_RESET);

    /* SYSTEM_RESET runs the features' reset handlers, which may re-touch pins. Re-assert the
     * safe state and the ignore afterwards so the reset cannot leave ENABLE driveable. */
    Firmata.setPinMode(PIN_ENABLE, PIN_MODE_IGNORE);
    digitalWrite(PIN_ENABLE, HIGH);
    pinMode(PIN_ENABLE, OUTPUT);

    /* The loop's backstop (M-02): if loop() stops -- a wild jump, a hang -- the AVR resets within
     * a second and this setup() drives ENABLE HIGH again. Armed last: the version blink above
     * waits ~1.9 s. One timeout, set once. */
    wdt_enable(WDTO_1S);
}

void loop()
{
    /* The byte stream reaches Firmata only through the guard's filter: Firmata's setPinMode()
     * writes its pin tables before any callback could look at the pin (M-01). One message per
     * pass, as before, so the watchdog and the steppers run between messages. */
    while (Serial.available()) {
        uint8_t out[3];
        bombyx_guard_verdict_t why;
        const uint8_t n = bombyx_filter_byte(&g_filter, (uint8_t)Serial.read(), out, &why);
        for (uint8_t i = 0; i < n; i++) Firmata.parse(out[i]);
        if (why != BOMBYX_GUARD_OK) sendRefusal(why);
        if (!Firmata.isParsingMessage() && g_filter.held_n == 0) break;
    }

    firmataExt.report(reporting.elapsed());

    /* The watchdog is evaluated EVERY pass, not only when something arrives -- a watchdog that
     * only runs when the host speaks cannot notice the host going away, which is the entire
     * case it exists for. */
    const bool want = bombyx_safety_may_energize(&g_safety, millis());
    if (want != g_energized_now) {
        applyEnable(want);
        if (!want) {
            /* A lapse must STOP the steppers, not pause them (review, 2026-09-13).
             * De-energizing alone leaves AccelStepper holding its move; when the permit stream
             * returns, the OLD move would resume from an unknown position -- motion nobody asked
             * for at that moment. The doer firmware answers the same case by zeroing every rate
             * in its stop frames. Here: free every stepper, so a new permit moves nothing until
             * the host configures and commands again. */
            accelStepper.reset();
            bombyx_guard_reset(&g_guard);           /* the steppers it counted are freed */
            Firmata.sendString(F("bombyx: de-energized and steppers reset (permit withdrawn or silent)"));
        }
    }
    /* This pass evaluated the permit: the loop is alive (M-02). */
    wdt_reset();
    /* Hardening: while not permitted, ENABLE is re-asserted HIGH on EVERY pass, not only on the
     * transition -- so D8's safety does not rest on every library path honouring PIN_MODE_IGNORE;
     * a future feature that raw-writes the pin is undone within one loop pass. */
    if (!want) digitalWrite(PIN_ENABLE, HIGH);
}
