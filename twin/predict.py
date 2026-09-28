"""Predict what the bench does with a plan -- before it runs -- from the exact image, the bench description and the bytes.

Jan, 2026-09-15: the system must know exactly how the hardware works and predict all behaviour of code given to the
Arduino through ByxIn; a prediction INFORMS the preview and lets ByxIn REFUSE; it never gates the verified core.

THE LAYERS, each with the provenance of what it knows:
  chip      tools/bench_twin/chip_twin.c runs the exact image (image.py rebuilds it and checks the flash record's hash)
            on simavr, cycle by cycle: every pin change and every transmitted byte. EXACT for the digital logic of the
            modelled peripherals, given the stimuli -- validated against the board record by the tests.
  shield    wiring.json's pin roles (slot STEP/DIR pins, D8 = ENABLE, active low, all slots) -- read, never restated.
  driver    a TMC2208 in standalone STEP/DIR: a microstep on each RISING STEP edge while ENABLE is driven low, DIR read
            at the edge. Where ENABLE is not driven (an input pin), a pulse is shorter than the shortest one seen to move
            this motor, or the line rises undriven, the step is UNCERTAIN, not guessed. Only slots with an actuator in
            wiring.json have a driver and a motor.
  shaft     microsteps -> degrees through wiring.json's steps_per_rev, IF NO STEP IS MISSED: the no-slip envelope,
            torque, load and supply are UNKNOWN (bench_facts.json), so the shaft is commanded motion, labelled so.
  firmware  the transmitted bytes decoded as Firmata (firmata.py): versions, strings, capability, pin states, moves.
  findings  the invariants a plan is judged by, each with the test that trips it and the mutant that removes it
            (tools/test_bench_twin_refuses_what_it_cannot_know.py): ENABLE LOW at any instant not covered by a permit,
            driver lines not quiet or ENABLE open at the horizon, steps a driver model cannot count, a slot with no
            motor, a timer waveform on a driver line, SRAM exhausted, a UART at another baud, a chip that stopped, a
            peripheral simulated unfaithfully -- and, from the host's bytes (lint_stream), requests whose harm the pins
            cannot show.

Nothing here touches hardware: the chip is simulated, and the result is a dict ByxIn shows and binds to a plan.
"""
import hashlib
import json
import os
import re

import chip
import firmata
import image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
WIRING = os.path.join(HERE, "wiring.json")
FACTS = os.path.join(HERE, "bench_facts.json")
SAFETY_H = os.path.join(HERE, "..", "firmware", "bombyx_firmata", "bombyx_safety.h")
NS_PER_CYCLE = 62.5
SCHEMA = "bombyx.bench_twin.prediction/1"


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def silence_limit_ms(path=None):
    """The permit watchdog, read from the image's own source (one description): BOMBYX_SILENCE_LIMIT_MS."""
    with open(path or SAFETY_H, encoding="utf-8") as fh:
        m = re.search(r"#define\s+BOMBYX_SILENCE_LIMIT_MS\s+(\d+)", fh.read())
    if not m:
        raise RuntimeError("BOMBYX_SILENCE_LIMIT_MS is gone from bombyx_safety.h")
    return int(m.group(1))


def bench():
    w = _load(WIRING)
    slots = {}
    for pin, spec in w["pins"].items():
        if spec.get("role") in ("step", "dir"):
            slots.setdefault(spec["slot"], {})[spec["role"]] = pin
    enable = [p for p, s in w["pins"].items() if s.get("role") == "enable"]
    if len(enable) != 1 or not w["pins"][enable[0]].get("active_low"):
        raise RuntimeError("wiring.json no longer names exactly one active-low enable pin")
    fitted = {a["slot"]: a for a in w.get("actuators", [])}
    # wiring.json a_axis: "The D12/D13 entries in `pins` are the d12_d13 option's claim and count only when jumpers says
    # so" -- until then D12/D13 are no slot's STEP/DIR, and activity on them is refused as UNKNOWN (M-24)
    jumpers = next((sh.get("a_axis", {}).get("jumpers") for sh in w.get("shields", []) if "a_axis" in sh), None)
    if jumpers != "d12_d13":
        slots.pop("A", None)
    return {"slots": slots, "enable": enable[0], "fitted": fitted, "wiring": w, "slot_a_jumpers": jumpers}


# ---- the bytes on the wire --------------------------------------------------------------------------------------------
def schedule(sends):
    """sends: [(t_us, bytes)]. A UART places bytes one after another: a send that starts while an earlier one is still on
    the wire waits. Returns (uart_events [(t_us, byte)], messages [{t_start_us, t_end_us, bytes}]) where t_end_us is when
    the last byte's stop bit ends -- when the firmware can first see the whole message."""
    events, messages, free_at = [], [], 0.0
    for t, data in sorted(sends, key=lambda s: s[0]):
        start = max(float(t), free_at)
        for k, b in enumerate(data):
            events.append((start + k * firmata.BYTE_US, b))
        free_at = start + len(data) * firmata.BYTE_US
        messages.append({"t_start_us": start, "t_end_us": free_at, "bytes": bytes(data)})
    return events, messages


def sysex_frames(messages):
    """Every sysex frame the host sends, with the time its END_SYSEX byte's stop bit ends -- a send may carry several
    frames (raw bytes), and the firmware sees each one whole only then. Non-sysex bytes cannot contain 0xF0 or 0xF7
    (Firmata data bytes are 7-bit), so scanning for the frame bytes is exact."""
    out = []
    for m in sorted(messages, key=lambda m: m["t_start_us"]):
        data, start = m["bytes"], None
        for k, byte in enumerate(data):
            if byte == firmata.START_SYSEX:
                start = k
            elif byte == firmata.END_SYSEX and start is not None:
                out.append((m["t_start_us"] + (k + 1) * firmata.BYTE_US, bytes(data[start:k + 1])))
                start = None
    return out


def frame_read_times(messages, rxr):
    """For each frame of sysex_frames(), in its order: when the firmware READ the frame's END_SYSEX byte (the n-th byte
    on the wire is the n-th UDR0 read), or None if it never did."""
    out, index = [], 0
    for m in sorted(messages, key=lambda m: m["t_start_us"]):
        start = None
        for k, byte in enumerate(m["bytes"]):
            if byte == firmata.START_SYSEX:
                start = k
            elif byte == firmata.END_SYSEX and start is not None:
                g = index + k
                ok = g < len(rxr) and rxr[g][1] == firmata.END_SYSEX
                out.append(rxr[g][0] * NS_PER_CYCLE / 1e3 if ok else None)
                start = None
        index += len(m["bytes"])
    return out


def _is_permit(frame):
    """bombyx_firmata.ino: START_SYSEX, 0x01, <permit>, END_SYSEX -- 1 may energize, 0 stops now (argv[0] != 0)."""
    return len(frame) == 4 and frame[1] == firmata.BOMBYX_PERMIT and frame[2] != 0


def _is_withdrawal(frame):
    return len(frame) == 4 and frame[1] == firmata.BOMBYX_PERMIT and frame[2] == 0


LAPSE_SLACK_MS = 5          # the firmware lapses 500 millis() counts after reading a permit: measured within 2 ms
RESET_PHASE_MS = 5          # the reset pulse the flashed firmware makes on D8 falls at 0.4 ms
UART_TOLERANCE = 0.045      # 8N1's absolute receiver limit; the Uno's own 57600 is 58823.5 (+2.1 %) and works
#: chip_twin.c's UNMODELLED kinds: whether a prediction that relies on one refuses, and why
UNMODELLED = {
    "wdt_timeout_change": ("refuse", "the firmware changes a running watchdog's timeout; simavr 1.6 keeps the old one "
                                     "until it fires, so the reset would come at the wrong time"),
    "sleep_mode": ("refuse", "the firmware sleeps in a mode deeper than idle; simavr 1.6 wakes it on Timer0 as if idle"),
    "eeprom_read": ("refuse", "EECR's read strobe was set -- an EEPROM read, or a stray write to the register -- and the "
                              "Uno's EEPROM contents are UNKNOWN (not readable through Optiboot)"),
    "mcusr_read": ("refuse", "the firmware reads the reset cause; on the Uno Optiboot clears it before the application, "
                             "here it does not run"),
    "adc_read": ("warn", "the firmware reads the ADC; no analogue level is stimulated, so every reading is the "
                         "simulation's, not the bench's"),
}


UNO_PIN_COUNT = 20                                                     # D0..D13, A0..A5 (Firmata numbers A0 as 14)
SET_PIN_MODE, SET_DIGITAL_PIN_VALUE, PIN_STATE_QUERY, EXTENDED_ANALOG = 0xF4, 0xF5, 0x6D, 0x6F


def _digital_port_messages(messages):
    """(t_us, port) for each DIGITAL_MESSAGE (0x90 | port, two data bytes) outside sysex."""
    out = []
    for m in messages:
        data, k = m["bytes"], 0
        while k < len(data):
            byte = data[k]
            if byte == firmata.START_SYSEX:
                end = data.find(bytes([firmata.END_SYSEX]), k)
                k = len(data) if end < 0 else end + 1
            elif (byte & 0xF0) == 0x90 and k + 2 < len(data):
                out.append((m["t_start_us"] + (k + 3) * firmata.BYTE_US, byte & 0x0F))
                k += 3
            elif byte in (SET_PIN_MODE, SET_DIGITAL_PIN_VALUE, 0xE0) or (byte & 0xF0) == 0xE0:
                k += 3
            else:
                k += 1
    return out


def _non_stops(messages, frames):
    """What a host may take for a stop and is not, on the flashed image (M-33, red team): SYSTEM_RESET has no callback
    attached, and ACCELSTEPPER_ENABLE 0 does not stop a running move."""
    out = [(t, "ACCELSTEPPER_ENABLE") for t, fr in frames if len(fr) >= 5 and fr[1] == firmata.ACCELSTEPPER_DATA and fr[2] == 0x04]
    for m in messages:
        data, k = m["bytes"], 0
        while k < len(data):
            if data[k] == firmata.START_SYSEX:
                end = data.find(bytes([firmata.END_SYSEX]), k)
                k = len(data) if end < 0 else end + 1
            else:
                if data[k] == 0xFF:
                    out.append((m["t_start_us"] + (k + 1) * firmata.BYTE_US, "SYSTEM_RESET"))
                k += 1
    return sorted(out)


def _pin_messages(messages, frames):
    """(t_us, pin, name) for each host message that addresses a pin by number: SET_PIN_MODE and SET_DIGITAL_PIN_VALUE
    (three bytes, outside sysex), PIN_STATE_QUERY and EXTENDED_ANALOG (sysex)."""
    out = []
    for m in messages:
        data, k = m["bytes"], 0
        while k < len(data):
            byte = data[k]
            if byte == firmata.START_SYSEX:
                end = data.find(bytes([firmata.END_SYSEX]), k)
                k = len(data) if end < 0 else end + 1
            elif byte in (SET_PIN_MODE, SET_DIGITAL_PIN_VALUE) and k + 2 < len(data):
                out.append((m["t_start_us"] + (k + 3) * firmata.BYTE_US, data[k + 1],
                            "SET_PIN_MODE" if byte == SET_PIN_MODE else "SET_DIGITAL_PIN_VALUE"))
                k += 3
            else:
                k += 1
    for t_us, fr in frames:
        if len(fr) >= 4 and fr[1] in (PIN_STATE_QUERY, EXTENDED_ANALOG):
            out.append((t_us, fr[2], "PIN_STATE_QUERY" if fr[1] == PIN_STATE_QUERY else "EXTENDED_ANALOG"))
    return sorted(out)
ACCELSTEPPER_CONFIG, ACCELSTEPPER_SET_SPEED = 0x00, 0x09
MULTISTEPPER_CONFIG, MULTISTEPPER_TO, MULTISTEPPER_STOP = 0x20, 0x21, 0x23
MAX_ACCELSTEPPERS, MAX_GROUPS = 10, 5                                  # ConfigurableFirmata 3.3.0 AccelStepperFirmata.h
#: the bytes after the device each AccelStepper subcommand reads: CONFIG (a driver: interface, step, dir, invert --
#: AccelStepperFirmata.cpp:153 reads the invert byte one position early), ZERO, STEP and TO (a 5-byte integer),
#: ENABLE, STOP, REPORT_POSITION, SET_ACCELERATION and SET_SPEED (a 4-byte float)
STEPPER_ARGS = {0x00: 4, 0x01: 0, 0x02: 5, 0x03: 5, 0x04: 1, 0x05: 0, 0x06: 0, 0x08: 4, 0x09: 4}


def lint_stream(frames, b, lapses_us, messages=(), commanded=None):
    """What the host asks for, judged before the chip runs it -- the requests whose consequence the pins cannot show.
    Each rule was found by the 2026-09-15 verification probes on the flashed image (ConfigurableFirmata 3.3.0):
      stepper_pin_not_on_the_uno   CONFIG passes the raw pin to the AVR core, which reads past its pin tables: pin 127
                                   makes wild writes and stops the simulated core (a live permit then stays LOW for
                                   good); pin 50 does not crash but sets D1-D4, D6 and D7 to output LOW in one DDRD write
      stepper_on_the_serial_pins   CONFIG on D0/D1 steps the UART lines the plan itself arrives on
      stepper_reconfigured         a CONFIG over a live stepper leaks it (70 B); the 16th exhausts SRAM and the next
                                   AccelStepper is constructed on NULL -- unless a lapse freed every stepper in between
      group_out_of_range           MULTISTEPPER commands are bounded by 10 steppers, not 5 groups: 5..9 write past group[]
      group_member_out_of_range    a MULTISTEPPER_CONFIG member >= 10 reads past stepper[]
      group_reconfigured           the group's count grows without bound and MULTISTEPPER_TO sizes a stack array by it
      speed_over_wiring_limit      the COMMANDED speed, not the realised one: the Uno caps AccelStepper near 3830 steps/s,
                                   below wiring.json's 4000, so a realised rate never shows a 20000 steps/s request"""
    findings, configured, step_pin_of, groups = [], {}, {}, set()
    fitted_max = [a.get("max_rate_sps") for a in b["fitted"].values() if a.get("max_rate_sps")]
    step_slot = {int(sp["step"][1:]): slot for slot, sp in b["slots"].items() if sp.get("step", "").startswith("D")}

    def refuse(fid, t_us, text):
        findings.append({"id": fid, "severity": "refuse", "at_ms": round(t_us / 1e3, 3), "text": text})

    # every message that names a pin: ConfigurableFirmata's setPinMode/getPinMode index pinConfig[] with no bound, and
    # past it lie the Firmata object's callback pointers -- SET_PIN_MODE to pin 59 crashes the core (system_reset probe)
    driver_pins = {int(n[1:]): r for n, r in ((p, sp.get("role")) for p, sp in b["wiring"]["pins"].items())
                   if r in ("step", "dir", "enable") and n.startswith("D")}
    for t_us, pin, what in _pin_messages(messages, frames):
        if pin >= UNO_PIN_COUNT:
            refuse("pin_not_on_the_uno", t_us, "%s names pin %d; the Uno has %d, and the firmware indexes its pin tables "
                   "with it unchecked" % (what, pin, UNO_PIN_COUNT))
        elif pin in driver_pins and what != "PIN_STATE_QUERY":
            # motion only through the stepper interface: a pin mode or level written to a STEP/DIR/ENABLE line is raw
            # actuation the plan's moves do not describe (red team, 2026-09-15: 60 microsteps by SET_DIGITAL_PIN_VALUE)
            refuse("gpio_on_driver_pin", t_us, "%s writes D%d, a %s line: motion goes through the stepper interface only"
                   % (what, pin, driver_pins[pin]))
    for t_us, port in _digital_port_messages(messages):
        if port in (0, 1):
            refuse("gpio_on_driver_pin", t_us, "a DIGITAL_MESSAGE writes port %d (D%d-D%d), which carries the shield's "
                   "driver lines" % (port, port * 8, port * 8 + 7))
    for t_us, what in _non_stops(messages, frames):
        findings.append({"id": "not_a_stop", "severity": "warn", "at_ms": round(t_us / 1e3, 3),
                         "text": "%s does not stop a move on this firmware: only STOP or a permit withdrawal does" % what})
    for t_us, fr in frames:
        if len(fr) < 5 or fr[1] != firmata.ACCELSTEPPER_DATA:
            continue
        cmd, dev, argv = fr[2], fr[3], fr[4:-1]
        if cmd in (MULTISTEPPER_CONFIG, MULTISTEPPER_TO, MULTISTEPPER_STOP):
            if MAX_GROUPS <= dev < MAX_ACCELSTEPPERS:
                refuse("group_out_of_range", t_us, "a MULTISTEPPER command names group %d; the firmware has %d groups and "
                       "writes past them" % (dev, MAX_GROUPS))
            if cmd == MULTISTEPPER_CONFIG:
                for member in argv:
                    if member >= MAX_ACCELSTEPPERS:
                        refuse("group_member_out_of_range", t_us, "group %d names stepper %d; there are %d" % (dev, member, MAX_ACCELSTEPPERS))
                if dev in groups:
                    refuse("group_reconfigured", t_us, "group %d is configured again: its member count grows without bound" % dev)
                groups.add(dev)
            continue
        if dev >= MAX_ACCELSTEPPERS:
            continue                                                   # the firmware ignores it; the chip shows that
        # AccelStepperFirmata reads each subcommand's bytes whether they came or not, from a buffer still holding the
        # previous message: a short STEP repeated the last move (the firmware review, 2026-09-15, RAN)
        need = STEPPER_ARGS.get(cmd)
        if need is not None and len(argv) < need:
            refuse("stepper_message_short", t_us, "stepper %d's subcommand 0x%02x carries %d byte(s) of %d: the firmware "
                   "reads the rest from the previous message" % (dev, cmd, len(argv), need))
            continue
        if cmd == ACCELSTEPPER_CONFIG and len(argv) >= 3:
            interface = argv[0]
            wires = (interface & 0x70) >> 4
            pins = list(argv[1:3]) + list(argv[3:3 + max(0, min(wires, 4) - 2)])
            if interface & 0x01 and len(argv) > 1 + len(pins):
                pins.append(argv[1 + len(pins)])
            for p in pins:
                if p >= UNO_PIN_COUNT:
                    refuse("stepper_pin_not_on_the_uno", t_us, "stepper %d is configured on pin %d; the Uno has %d, and "
                           "the core reads past its pin tables for it" % (dev, p, UNO_PIN_COUNT))
                elif p in (0, 1):
                    refuse("stepper_on_the_serial_pins", t_us, "stepper %d is configured on D%d, a UART line the plan "
                           "arrives on" % (dev, p))
            prev = configured.get(dev)
            if prev is not None and not any(prev < lap <= t_us for lap in lapses_us):
                refuse("stepper_reconfigured", t_us, "stepper %d is configured again while the first still lives: "
                       "ConfigurableFirmata leaks it (70 B of 2048), and an exhausted heap constructs on NULL" % dev)
            configured[dev] = t_us
            step_pin_of[dev] = argv[1]
        elif cmd == ACCELSTEPPER_SET_SPEED and len(argv) >= 4:
            speed = firmata.decode_custom_float(*argv[:4])
            slot = step_slot.get(step_pin_of.get(dev))
            if commanded is not None and slot:
                commanded[slot] = max(commanded.get(slot, 0.0), abs(speed))
            limit = (b["fitted"].get(slot) or {}).get("max_rate_sps") or (min(fitted_max) if fitted_max else None)
            if limit and abs(speed) > limit:
                refuse("speed_over_wiring_limit", t_us, "stepper %d is commanded to %.0f steps/s; wiring.json's limit is %s"
                       % (dev, speed, limit))
    return findings


# ---- the layers above the chip ----------------------------------------------------------------------------------------
def _undriven(changes, end_cycle):
    """[(start, end)] cycles during which the pin was not driven 0 or 1 (an input, or before any record)."""
    out, start = [], 0
    for c, s in changes:
        if s in ("0", "1"):
            if start is not None and c > start:
                out.append((start, c))
            start = None
        elif start is None:
            start = c
    if start is not None and end_cycle > start:
        out.append((start, end_cycle))
    return out


def _intervals(changes, state, end_cycle):
    """[(start, end)] cycles during which the pin was in `state`."""
    out, start = [], None
    for c, s in changes:
        if s == state and start is None:
            start = c
        elif s != state and start is not None:
            out.append((start, c))
            start = None
    if start is not None:
        out.append((start, end_cycle))
    return out


def analyse(trace, b, facts, messages, uart_events, safety_h=None, permits_honoured=True):
    end_cycle = int(trace["end"]["cycle"])
    pins = trace["pins"]
    en_pin = b["enable"]
    en_changes = pins.get(en_pin, [])
    drv = facts["driver_slot_z"]
    min_high_ns = drv["min_step_high_ns"]["value"]
    dir_setup_ns = drv["dir_setup_ns"]["value"]
    findings = []

    def enabled_at(c):
        s = chip.level_at(en_changes, c)
        return {"0": True, "1": False}.get(s)                  # Z/P/None: not driven -> unknown

    slots = {}
    for slot, sp in sorted(b["slots"].items()):
        st, dr = pins.get(sp.get("step"), []), pins.get(sp.get("dir"), [])
        rec = {"step_pin": sp.get("step"), "dir_pin": sp.get("dir"), "fitted": slot in b["fitted"],
               "rising_edges": 0, "microsteps_dir_high": 0, "microsteps_dir_low": 0, "edges_while_disabled": 0,
               "edges_enable_not_driven": 0, "edges_dir_uncertain": 0, "edges_below_min_high": 0, "rises_not_driven": 0,
               "min_high_ns": None, "max_rate_sps": None, "mean_rate_sps": None, "first_edge_ms": None, "last_edge_ms": None}
        prev, last_enabled_edge, enabled_edges = None, None, []
        for i, (c, s) in enumerate(st):
            rising = s == "1" and prev in ("0",)
            # a STEP line that rises without being driven both ways -- a pull-up switched on, or a floating line driven
            # high: whether the driver steps on it is UNKNOWN (found 2026-09-15: ten pull-up toggles on D4, 0 edges)
            if (prev, s) in (("Z", "P"), ("0", "P"), ("Z", "1"), ("P", "1")) and enabled_at(c) is not False:
                rec["rises_not_driven"] += 1
            prev = s
            if not rising:
                continue
            rec["rising_edges"] += 1
            t_ms = c * NS_PER_CYCLE / 1e6
            rec["first_edge_ms"] = rec["first_edge_ms"] if rec["first_edge_ms"] is not None else t_ms
            rec["last_edge_ms"] = t_ms
            nxt = st[i + 1][0] if i + 1 < len(st) else end_cycle
            high_ns = (nxt - c) * NS_PER_CYCLE
            rec["min_high_ns"] = high_ns if rec["min_high_ns"] is None else min(rec["min_high_ns"], high_ns)
            en = enabled_at(c)
            if en is None:
                rec["edges_enable_not_driven"] += 1
                continue
            if not en:
                rec["edges_while_disabled"] += 1
                continue
            if high_ns < min_high_ns:
                rec["edges_below_min_high"] += 1
                continue
            d = chip.level_at(dr, c)
            d_last = max((dc for dc, _ in dr if dc <= c), default=None)
            if d not in ("0", "1") or (d_last is not None and (c - d_last) * NS_PER_CYCLE < dir_setup_ns):
                rec["edges_dir_uncertain"] += 1
                continue
            rec["microsteps_dir_high" if d == "1" else "microsteps_dir_low"] += 1
            if last_enabled_edge is not None and c > last_enabled_edge:
                rate = 1e9 / ((c - last_enabled_edge) * NS_PER_CYCLE)
                rec["max_rate_sps"] = rate if rec["max_rate_sps"] is None else max(rec["max_rate_sps"], rate)
            last_enabled_edge = c
            enabled_edges.append(c)
        if len(enabled_edges) > 1:
            # the loop poll caps the realised rate below the commanded one (timing probe: 395.4 at 400, 3653 at 4000)
            rec["mean_rate_sps"] = round((len(enabled_edges) - 1) * 1e9 / ((enabled_edges[-1] - enabled_edges[0]) * NS_PER_CYCLE), 2)
        slots[slot] = rec
        moved = rec["microsteps_dir_high"] + rec["microsteps_dir_low"]
        if rec["rises_not_driven"]:
            findings.append({"id": "step_line_not_driven", "severity": "refuse", "slot": slot, "rises": rec["rises_not_driven"],
                             "text": "slot %s's STEP line rises %d time(s) without being driven (pull-up or floating) while "
                                     "the driver may be enabled: whether it steps is UNKNOWN" % (slot, rec["rises_not_driven"])})
        if not rec["fitted"] and rec["rising_edges"]:
            # wiring.json's $unwired: "ByxIn should refuse a request naming them rather than driving an empty slot -- a
            # frame sent to an empty slot looks exactly like a frame that worked" (a warning until the red team, 2026-09-15)
            findings.append({"id": "steps_on_unfitted_slot", "severity": "refuse", "slot": slot, "edges": rec["rising_edges"],
                             "text": "slot %s receives %d STEP edges and wiring.json has no actuator there" % (slot, rec["rising_edges"])})
        if rec["fitted"] and rec["edges_enable_not_driven"]:
            findings.append({"id": "steps_enable_not_driven", "severity": "refuse", "slot": slot, "edges": rec["edges_enable_not_driven"],
                             "text": "%d STEP edges while D8 is not driven: whether the driver is enabled depends on the shield's EN pull, which is UNKNOWN" % rec["edges_enable_not_driven"]})
        if rec["fitted"] and rec["edges_below_min_high"]:
            findings.append({"id": "step_pulse_below_minimum", "severity": "refuse", "slot": slot, "edges": rec["edges_below_min_high"],
                             "text": "%d enabled STEP pulses are shorter than the driver minimum (%s ns, DERIVED and contradicted on this bench)" % (rec["edges_below_min_high"], min_high_ns)})
        if rec["fitted"] and rec["edges_dir_uncertain"]:
            findings.append({"id": "dir_not_settled", "severity": "refuse", "slot": slot, "edges": rec["edges_dir_uncertain"],
                             "text": "%d enabled STEP edges with DIR undriven or changed within the setup time" % rec["edges_dir_uncertain"]})
        if rec["fitted"] and moved:
            max_rate = b["fitted"][slot].get("max_rate_sps")
            if rec["max_rate_sps"] and max_rate and rec["max_rate_sps"] > max_rate:
                findings.append({"id": "rate_over_wiring_limit", "severity": "refuse", "slot": slot,
                                 "text": "peak %.0f steps/s is over wiring.json's %s" % (rec["max_rate_sps"], max_rate)})

    # a timer waveform on a pin the bench wires to a driver: the level is not produced by simavr for phase-correct PWM
    # (chip_twin.c effective()), and on any timer it is a pulse train no stepper plan asks for
    roles = {p: s.get("role") for p, s in b["wiring"]["pins"].items()}
    for pin, conns in sorted(trace.get("ocm", {}).items()):
        on = [c for c, connected in conns if connected]
        if on and roles.get(pin) in ("step", "dir", "enable"):
            findings.append({"id": "timer_waveform_on_driver_pin", "severity": "refuse", "pin": pin,
                             "at_ms": round(on[0] * NS_PER_CYCLE / 1e6, 3),
                             "text": "%s (%s) is connected to a timer output (PWM): a pulse train on a driver line, whose "
                                     "level the simulation does not produce for every timer mode" % (pin, roles[pin])})

    # SRAM: the heap reaching malloc's margin below the stack means every further allocation returns NULL
    mem = trace.get("mem", {})
    if isinstance(mem.get("min_headroom"), int) and mem["min_headroom"] <= mem.get("malloc_margin", 0):
        findings.append({"id": "sram_exhausted", "severity": "refuse", "min_headroom": mem["min_headroom"],
                         "at_ms": round(mem["min_headroom_cycle"] * NS_PER_CYCLE / 1e6, 3),
                         "text": "the heap came within %d bytes of the stack (malloc's margin is %d): every further "
                                 "allocation fails, and ConfigurableFirmata constructs on the NULL it gets"
                                 % (mem["min_headroom"], mem["malloc_margin"])})

    # ENABLE against the permit stream -- the invariant BombyxFirmata implements and the bench relies on, judged for ANY
    # image whether or not it reads the permits: D8 may be LOW at an instant only if a permit reached the chip no more
    # than the silence limit (plus LAPSE_SLACK_MS) before it, with no withdrawal since. Found 2026-09-15 by the fidelity
    # and sketch probes: a window used to be judged only at its end, "live" meant any permit ever sent, a window open at
    # the horizon was never judged, and windows under 1 ms were warnings -- a sketch pulsing ENABLE for 500 us around
    # each STEP turned the motor a full revolution with refuse=False. The one exception is the firmware's own reset
    # pulse: a single window in the first RESET_PHASE_MS, under 1 ms, with no STEP edge in it (a warning, and a finding
    # for the firmware's owner -- pinMode(OUTPUT) before digitalWrite(HIGH)).
    limit_ms = silence_limit_ms(safety_h)
    frames = sysex_frames(messages)
    # a permit counts from when the firmware READ its last byte, and only on firmware that implements the permit (M-17:
    # credit went to frames the chip never read, and to images that ignore permits). A withdrawal ends the cover from the
    # earlier of its read and its wire time.
    read_us = frame_read_times(messages, trace["rxr"])
    permits = sorted(read_us[k] for k, (t, fr) in enumerate(frames)
                     if _is_permit(fr) and read_us[k] is not None) if permits_honoured else []
    withdrawals = sorted(t if read_us[k] is None else min(t, read_us[k]) for k, (t, fr) in enumerate(frames) if _is_withdrawal(fr))
    cover = []
    for p in permits:
        end = p + (limit_ms + LAPSE_SLACK_MS) * 1e3
        w = next((x for x in withdrawals if p < x < end), None)
        cover.append((p, end if w is None else w + LAPSE_SLACK_MS * 1e3))
    merged = []
    for a, z in cover:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], z))
        else:
            merged.append((a, z))

    def first_uncovered(s_us, e_us):
        t = s_us
        for a, z in merged:
            if z < t:
                continue
            if a > t:
                return t
            t = z
            if t >= e_us:
                return None
        return t if t < e_us else None

    step_edges = sorted(c for sp in b["slots"].values() for (c, s), (_, ps) in
                        zip(pins.get(sp.get("step"), [])[1:], pins.get(sp.get("step"), [])) if s == "1" and ps == "0")
    enable_windows = []
    open_at_horizon = bool(en_changes) and en_changes[-1][1] == "0"
    # where the firmware starts: power-on, each hand-over from the bootloader, each reset while running
    anchors = sorted({0, *trace.get("app", []), *(r["cycle"] for r in trace.get("resets", []))})
    low = _intervals(en_changes, "0", end_cycle)
    for k, (s, e) in enumerate(low):
        s_us, e_us = s * NS_PER_CYCLE / 1e3, e * NS_PER_CYCLE / 1e3
        live = [p for p in permits if p <= s_us]
        win = {"from_ms": round(s_us / 1e3, 4), "to_ms": round(e_us / 1e3, 4), "duration_us": round(e_us - s_us, 1),
               "last_permit_before_ms": round(live[-1] / 1e3, 3) if live else None}
        enable_windows.append(win)
        u = first_uncovered(s_us, e_us)
        if u is None:
            continue
        stepped = any(s <= c <= e for c in step_edges)
        anchor = max(a for a in anchors if a <= s)
        first_after_start = not any(ws >= anchor for ws, _ in low[:k])
        if first_after_start and (s - anchor) * NS_PER_CYCLE / 1e3 < RESET_PHASE_MS * 1e3 and e_us - s_us < 1000 and not stepped:
            findings.append({"id": "enable_pulse_at_reset", "severity": "warn", "from_ms": win["from_ms"],
                             "duration_us": win["duration_us"],
                             "text": "D8 is driven LOW for %.1f us at %.4f ms, before any permit: every driver is enabled "
                                     "for that long at each reset (the firmware sets the pin's direction before its level)"
                                     % (win["duration_us"], win["from_ms"])})
            continue
        before = [p for p in permits if p <= u]
        if not before:
            findings.append({"id": "enable_without_permit", "severity": "refuse", "from_ms": win["from_ms"],
                             "duration_us": win["duration_us"],
                             "text": "D8 is driven LOW for %.1f us at %.4f ms with no permit %s -- every driver is "
                                     "enabled%s" % (win["duration_us"], win["from_ms"],
                                                    "read before it" if permits_honoured else "(this firmware does not implement the permit)",
                                                    ", and STEP edges fall in it" if stepped else "")})
        else:
            findings.append({"id": "enable_outlived_permit", "severity": "refuse", "from_ms": win["from_ms"],
                             "at_ms": round(u / 1e3, 3),
                             "text": "D8 is still LOW at %.1f ms, %.0f ms after the last permit before it (the limit is %d ms)"
                                     % (u / 1e3, (u - before[-1]) / 1e3, limit_ms)})
    if open_at_horizon and not any(f["id"] in ("enable_without_permit", "enable_outlived_permit") for f in findings):
        findings.append({"id": "enable_open_at_horizon", "severity": "refuse",
                         "text": "the prediction ends with D8 LOW -- every driver still enabled; what follows is not "
                                 "predicted (the horizon must reach past the last permit and the silence limit)"})

    # the horizon: a prediction says nothing about what comes after it, so the driver lines must have been quiet for
    # the silence limit before it (found 2026-09-15: the same sketch was safe at a 2900 ms horizon, refused at 3100 ms)
    quiet_from = end_cycle - int(limit_ms * 1e3 * 1e3 / NS_PER_CYCLE)
    busy = sorted({pin for pin, r in roles.items() if r in ("step", "dir", "enable")
                   for c, _ in pins.get(pin, [])[1:] if c > quiet_from})
    if busy:
        findings.append({"id": "not_quiet_at_horizon", "severity": "refuse", "pins": busy,
                         "text": "%s still changed in the last %d ms of the prediction; what follows is not predicted"
                                 % (", ".join(busy), limit_ms)})

    # receive timing: the n-th byte on the wire is the n-th UDR0 read (the firmware drops none unread; a byte count that
    # disagrees is itself the finding). simavr hands a byte to the firmware only a byte-time after the previous read,
    # so its delivery can lag the wire; the silicon's receiver is clocked by the wire. The lag bounds how late the
    # prediction's firmware timing may run behind the real chip after a burst -- it is not an overrun.
    rx = {"bytes_on_wire": len(uart_events), "bytes_read": len(trace["rxr"]), "max_delivery_lag_us": None}
    lags = []
    for (t_start, byte), (c_read, got) in zip(uart_events, trace["rxr"]):
        complete_us = t_start + firmata.BYTE_US * 0.95                  # the stop bit sampled, 9.5 bits after the start
        lags.append(c_read * NS_PER_CYCLE / 1e3 - complete_us)
        if got != byte:
            findings.append({"id": "rx_byte_mismatch", "severity": "refuse",
                             "text": "the firmware read %02x where the wire carried %02x" % (got, byte)})
            break
    # a receive overrun as the silicon has it (ATmega328P datasheet, DORn): two unread characters in the receive buffer,
    # a third complete in the shift register, and a new start bit -- the shift register's character is lost. simavr
    # queues 64 and never sets DOR0 (fidelity probe, 2026-09-15), so it is judged here from the wire and the reads.
    # Conservative: simavr hands a queued byte over only a byte-time after the previous read, never earlier than silicon.
    reads_us = [c * NS_PER_CYCLE / 1e3 for c, _ in trace["rxr"]]
    done_us = [t + firmata.BYTE_US * 0.95 for t, _ in uart_events]
    for n, (t_start, _) in enumerate(uart_events):
        waiting = sum(1 for k in range(n) if done_us[k] <= t_start and (k >= len(reads_us) or reads_us[k] > t_start))
        if waiting >= 3:
            findings.append({"id": "rx_overrun", "severity": "refuse", "at_ms": round(t_start / 1e3, 3), "byte_index": n,
                             "text": "byte %d starts while three earlier bytes wait unread: the silicon's receiver "
                                     "overruns and loses one" % n})
            break
    if lags:
        rx["max_delivery_lag_us"] = round(max(lags), 1)
        rx["min_delivery_lag_us"] = round(min(lags), 1)
        if max(lags) > 2 * firmata.BYTE_US:
            findings.append({"id": "rx_delivery_lag", "severity": "warn", "max_lag_us": rx["max_delivery_lag_us"],
                             "text": "simavr delivered received bytes up to %.0f us behind the wire; firmware timing after that "
                                     "burst is uncertain by that much (the silicon receives on wire time)" % max(lags)})
    # the firmware's first word is the earliest the plan's clock may be trusted. On the bench a port open resets the Uno
    # into Optiboot 4.4 first (a plan with port_open models it: 1398 ms on a quiet open), and it erases flash page 0
    # after four stray bytes before checking the command's terminator -- the application dead until reflashed, D8
    # undriven (bootloader probe, 2026-09-15). So no host byte may come before the firmware has spoken.
    first_tx_us = trace["tx"][0][0] * NS_PER_CYCLE / 1e3 if trace["tx"] else None
    early = [t for t, _ in uart_events if first_tx_us is None or t < first_tx_us]
    if early:
        findings.append({"id": "bytes_before_firmware_ready", "severity": "refuse", "at_ms": round(early[0] / 1e3, 3),
                         "bytes": len(early),
                         "text": "%d byte(s) go out before the firmware %s: on the bench they could land in the "
                                 "bootloader, which erases the application's first flash page after four"
                                 % (len(early), "has said anything" if first_tx_us is None
                                    else "first speaks (%.1f ms)" % (first_tx_us / 1e3))})
    # D8 undriven -- from reset, through the bootloader, after a reset while running: whether the drivers are enabled
    # then is the shield's, not the firmware's. An EN/GND jumper ties ENABLE low whatever D8 does; without one, the EN
    # pull decides. Refused while either is UNKNOWN (M-14; Jan's rule: ByxIn refuses what is UNKNOWN).
    undriven = _undriven(en_changes, end_cycle)
    und_total = sum(e - s for s, e in undriven) * NS_PER_CYCLE / 1e3
    und_long = max((e - s for s, e in undriven), default=0) * NS_PER_CYCLE / 1e3
    shield = facts.get("shield", {})
    jumper, pull = shield.get("en_gnd_jumper_fitted", {}), shield.get("en_pull", {})
    measured = lambda fact: fact.get("provenance") == "MEASURED"                                     # noqa: E731
    fact_line = "D8 is undriven for %.1f ms in all, the longest %.1f ms" % (und_total / 1e3, und_long / 1e3)
    if measured(jumper) and jumper.get("value") is True:
        findings.append({"id": "enable_tied_low_by_jumper", "severity": "refuse",
                         "text": "the shield's EN/GND jumper is fitted: every driver is enabled whenever powered, whatever D8 does"})
    elif not (measured(jumper) and jumper.get("value") is False and measured(pull)):
        unknown = [n for n, fact in (("en_gnd_jumper_fitted", jumper), ("en_pull", pull)) if not measured(fact)]
        findings.append({"id": "enable_path_unmeasured", "severity": "refuse", "unknown": unknown,
                         "undriven_total_ms": round(und_total / 1e3, 3), "undriven_longest_ms": round(und_long / 1e3, 3),
                         "text": "whether D8 controls the drivers rests on the shield (%s UNKNOWN): an EN/GND jumper ties "
                                 "ENABLE low whatever D8 does, and the EN pull decides while D8 is undriven. %s. Measured "
                                 "at the bench, this clears." % (" and ".join(unknown), fact_line)})
    elif und_total > 0 and pull.get("value") != "up":
        findings.append({"id": "enable_while_undriven", "severity": "refuse",
                         "text": "the shield pulls ENABLE %s: every driver is enabled while D8 is undriven. %s"
                                 % (pull.get("value"), fact_line)})
    elif und_total > 0:
        findings.append({"id": "enable_undriven_at_reset", "severity": "warn",
                         "undriven_total_ms": round(und_total / 1e3, 3), "text": fact_line + " (the shield pulls ENABLE up)"})
    delivered = len([e for e in uart_events if e[0] * 1e3 / NS_PER_CYCLE <= end_cycle])
    if len(trace["rxr"]) < delivered:
        findings.append({"id": "rx_bytes_unread", "severity": "warn",
                         "text": "%d bytes reached the chip before the horizon and %d were read" % (delivered, len(trace["rxr"]))})
    if trace["cpu"]:
        findings.append({"id": "cpu_stopped", "severity": "refuse",
                         "text": "the core stopped: %s -- on silicon a wild operation has undefined results, and the "
                                 "simulation's trace after it is not trusted" % trace["cpu"]})

    # the UART the firmware set against the plan's 57600: the harness hands bytes over whole, so a receiver on another
    # baud would see nothing like them and the prediction could not show it (found 2026-09-15 by the sketch probe)
    uart_cfg = trace.get("uart") or []
    rx["firmware_baud"] = round(16e6 / uart_cfg[-1]["bit_cycles"], 1) if uart_cfg else None
    if uart_events and rx["firmware_baud"] and abs(rx["firmware_baud"] - firmata.BAUD) / firmata.BAUD > UART_TOLERANCE:
        findings.append({"id": "uart_baud_mismatch", "severity": "refuse", "baud": rx["firmware_baud"],
                         "text": "the firmware's UART runs at %.0f baud and the plan's bytes at %d: past a receiver's "
                                 "tolerance, the bytes would not arrive as sent" % (rx["firmware_baud"], firmata.BAUD)})
    for r in trace.get("resets", []):
        findings.append({"id": "chip_reset_while_running", "severity": "warn", "at_ms": round(r["cycle"] * NS_PER_CYCLE / 1e6, 3),
                         "text": "the chip reset itself at %.1f ms (MCUSR %02x): every pin became an input"
                                 % (r["cycle"] * NS_PER_CYCLE / 1e6, r["mcusr"])})
    for u in trace.get("unmodelled", []):
        severity, why = UNMODELLED.get(u["what"], ("refuse", "an unfaithfully simulated function"))
        findings.append({"id": "unmodelled_" + u["what"], "severity": severity,
                         "at_ms": round(u["cycle"] * NS_PER_CYCLE / 1e6, 3), "text": why})
    # a pin the bench does not describe, driven: a switch to GND on an endstop header shorts an output HIGH. Refused while
    # bench_facts cannot say nothing is attached (M-26). D1 is the UART's own TX; D0 driven fights the USB bridge.
    headers = shield.get("headers_attached", {})
    driven = sorted({pin for pin, ch in pins.items() if pin not in roles and pin != "D1"
                     and any(s in ("0", "1", "W") for _, s in ch)})
    if driven:
        clear = measured(headers) and headers.get("value") is False
        findings.append({"id": "drives_pins_the_bench_does_not_describe", "severity": "warn" if clear else "refuse", "pins": driven,
                         "text": "%s driven as output(s); wiring.json does not say what the shield connects there (endstop, "
                                 "abort/hold/resume/coolant, spindle headers)%s" % (", ".join(driven), "" if clear else
                                 ", and whether anything is attached is UNKNOWN")})
    if b.get("slot_a_jumpers") != "d12_d13":
        # a STEP rise on D12 may step slot A's driver; D13 is the Uno's LED, which Optiboot and the version blink toggle
        # at every start, and a DIR line alone moves nothing -- so D13 is named, D12 refuses
        for role, severity in (("step", "refuse"), ("dir", "warn")):
            a_pins = [p for p, sp in b["wiring"]["pins"].items() if sp.get("slot") == "A" and sp.get("role") == role]
            touched = sorted(p for p in a_pins if len(pins.get(p, [])) > 1)
            if touched:
                findings.append({"id": "slot_a_mapping_unknown" if role == "step" else "slot_a_dir_line_changes",
                                 "severity": severity, "pins": touched,
                                 "text": "%s (slot A %s if its jumpers say d12_d13) changes, and wiring.json's a_axis jumpers "
                                         "are %s" % (", ".join(touched), role.upper(), b.get("slot_a_jumpers"))})
    flash = trace.get("flash") or {}
    if flash.get("changed_bytes"):
        findings.append({"id": "flash_changed", "severity": "refuse", "bytes": flash["changed_bytes"],
                         "text": "%d flash byte(s) were written during the run, the first at 0x%04x: the firmware on the "
                                 "chip is no longer the image predicted" % (flash["changed_bytes"], flash["first_changed_bytes"]
                                                                            if "first_changed_bytes" in flash else flash.get("first_changed", 0))})
    if trace.get("stderr"):
        findings.append({"id": "simulator_warned", "severity": "refuse", "lines": trace["stderr"][:10],
                         "text": "the simulator reported: %s" % " | ".join(trace["stderr"][:3])})

    # the firmware's own words, with the time the first byte of each message left the chip
    tx = trace["tx"]
    msgs = [{"t_ms": round(tx[i][0] * NS_PER_CYCLE / 1e6, 3), "kind": kind, **fields}
            for i, kind, fields in firmata.decode(bytes(bt for _, bt in tx))]
    lapses_us = [m["t_ms"] * 1e3 for m in msgs if m["kind"] == "string" and "steppers reset" in m.get("text", "")]
    commanded = {}
    findings += lint_stream(frames, b, lapses_us, messages, commanded)
    for slot, speed in sorted(commanded.items()):
        rec = slots.get(slot) or {}
        if rec.get("mean_rate_sps") and speed and rec["mean_rate_sps"] < 0.9 * speed:
            findings.append({"id": "rate_below_command", "severity": "warn", "slot": slot,
                             "text": "slot %s realises %.1f steps/s on average against %.0f commanded: the move takes "
                                     "longer than planned" % (slot, rec["mean_rate_sps"], speed)})
        if slot in slots:
            slots[slot]["commanded_sps"] = speed

    # the shaft: commanded motion on fitted slots, if no step is missed
    motor = facts["motor_z"]
    shaft = {}
    for slot, a in b["fitted"].items():
        rec = slots.get(slot, {})
        net = rec.get("microsteps_dir_high", 0) - rec.get("microsteps_dir_low", 0)
        spr = a.get("steps_per_rev")
        shaft[slot] = {
            "actuator": a.get("id"), "net_microsteps_dir_high_positive": net,
            "degrees_if_no_slip": round(net / spr * 360.0, 3) if spr else None, "steps_per_rev": spr,
            "dir_high_rotation": motor["dir_high_rotation"]["value"],
            "provenance": {"microsteps": "EXACT (chip) given the driver model", "steps_per_rev": "INFERRED (bench_facts annotations)",
                           "rotation_sense": motor["dir_high_rotation"]["provenance"], "no_slip": "UNKNOWN (no measured envelope)"},
        }
    return {"slots": slots, "enable_windows": enable_windows, "firmware_messages": msgs, "shaft": shaft, "rx": rx,
            "findings": findings}


NOT_MODELLED = [
    "electrical levels: drive strength, floating inputs (a pin nothing drives reads what the stimuli say, or not at all)",
    "clock error: the chip runs at exactly 16 MHz; the Uno clone's crystal/resonator error is UNKNOWN",
    "brown-out, fuses, CLKPR, EEPROM contents (not readable through Optiboot)",
    "the USB-serial bridge and host latency: lane 'exact' delivers each byte when it is sent",
    "torque, inertia, load, resonance, missed steps: the shaft is commanded motion, not realised motion",
    "driver internals beyond STEP/DIR/ENN: stealthChop regulation, current, thermal limits, latched faults (DIAG is not routed)",
    "whether the driver counts STEP edges while disabled (decides the rotor snap on re-energize)",
    "the chip's state before the plan: every prediction starts from reset (the Uno resets when its port is opened); on "
    "a port already open, what earlier plans left -- configured steppers, leaked heap, positions -- is not in it",
    "the level of a pin under phase-correct timer PWM (analogWrite on D3, D9, D10, D11): shown as W, never guessed",
    "the heap of an image without symbols (a .hex): only the stack's low-water mark is known",
    "what follows the horizon: a prediction is refused unless the driver lines were quiet for the silence limit before "
    "it and ENABLE is high, which a sketch can still break later",
    "interrupt entry costs 0 cycles here and 4 on the silicon",
    "the bootloader only when a plan says port_open (Optiboot 4.4, an external reset, a quiet line); otherwise a "
    "prediction starts in the application. Optiboot's switch to a 16 ms watchdog when bytes arrive is not modelled "
    "(simavr keeps the 1 s timeout: reported as unmodelled_wdt_timeout_change)",
    "the reset cause an application reads without port_open (the bench's Optiboot clears MCUSR first)",
    "D1 (TXD) while the USART drives it: reported as the port register has it, not as the serial line's bits",
    "an external input level across a port write: simavr 1.6 overwrites it with PORTx, the harness restores it right "
    "after, and a pin-change interrupt may see the transient",
]


UNHASHED = ("trace", "build")


def image_safety_h(image_path):
    """The image's own bombyx_safety.h: arduino-cli copies a sketch's headers into <build>/sketch/ beside the ELF, so the
    silence limit comes from what the compiler saw -- the commit the image was built from, not today's working tree
    (found 2026-09-15 by the fidelity probe); build_commit's <out>/src/<sketch>/ copy is the fallback. None when the
    image carries no such header (a sketch that is not BombyxFirmata)."""
    build = os.path.dirname(os.path.abspath(image_path))
    candidates = [os.path.join(build, "sketch", "bombyx_safety.h")]
    src = os.path.join(os.path.dirname(build), "src")
    if os.path.isdir(src):
        candidates += [os.path.join(src, name, "bombyx_safety.h") for name in sorted(os.listdir(src))]
    return next((h for h in candidates if os.path.exists(h)), None)


def implements_the_permit(image_path, own_h):
    """Permits are credited only to BombyxFirmata: an image built from a sketch named bombyx_firmata that carries its
    bombyx_safety.h. Any other firmware gets no cover for D8 LOW (M-17, M-19)."""
    return bool(own_h) and os.path.basename(image_path).split(".")[0] == "bombyx_firmata"


def predict(image_path, plan, keep_trace=False, safety_h=None):
    """plan: {"sends": [{"t_ms": float, "hex": "f00101f7"}], "horizon_ms": float, "pins": [{"t_ms", "pin", "level"}],
    "reset_to_boot": bool, "lane": "exact"}. image_path: the .elf (its .hex sibling names the image). safety_h: the
    header the silence limit is read from -- by default the image's own, else the bench's (the repo's)."""
    if plan.get("lane", "exact") != "exact":
        raise ValueError("only lane 'exact' is modelled yet: transport latency bounds are not measured")
    port_open = bool(plan.get("port_open") or plan.get("reset_to_boot"))
    boot_hex = None
    if port_open:
        # a port open pulses DTR: an external reset into Optiboot, which blinks, waits out its 1 s watchdog on a quiet
        # line and hands over to the application -- the with_bootloader image arduino-cli builds beside the ELF (its
        # boot section equals the bench Uno's 2026-09-13 readback: Optiboot 4.4)
        # a .hex read back from the chip holds the bootloader itself (the harness refuses one whose boot section is erased)
        boot_hex = image_path[:-4] + ".with_bootloader.hex" if image_path.endswith(".elf") else image_path
        if not os.path.exists(boot_hex):
            raise ValueError("port_open needs the with_bootloader hex beside %s" % image_path)
    own_h = safety_h or image_safety_h(image_path)
    sends = [(float(s["t_ms"]) * 1e3, bytes.fromhex(s["hex"])) for s in plan.get("sends", [])]
    uart_events, messages = schedule(sends)
    pin_events = [(float(p["t_ms"]) * 1e3, p["pin"], int(p["level"])) for p in plan.get("pins", [])]
    horizon_us = float(plan["horizon_ms"]) * 1e3
    if port_open:
        trace = chip.run(boot_hex, uart_events, pin_events, horizon_us, True, reset_cause="ext",
                         symbols=image_path if image_path.endswith(".elf") else None)
    else:
        trace = chip.run(image_path, uart_events, pin_events, horizon_us)
    b, facts = bench(), _load(FACTS)
    # the permit is credited only to firmware that implements it: the image's own sources are BombyxFirmata's
    honoured = bool(safety_h) or implements_the_permit(image_path, own_h)
    result = analyse(trace, b, facts, messages, uart_events, own_h, honoured)
    hex_path = image_path[:-4] + ".hex" if image_path.endswith(".elf") else image_path
    hex_sha = image.sha256_file(hex_path) if os.path.exists(hex_path) else None
    out = {
        "schema": SCHEMA,
        "inputs": {
            "image": {"hex_sha256": hex_sha or image.sha256_file(image_path),
                      "recorded_as": image.RECORDED.get((hex_sha or "")[:16])},
            "wiring_sha256": _sha(WIRING), "bench_facts_sha256": _sha(FACTS),
            "plan_sha256": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
            "harness_sha256": trace["harness_sha256"], "engine": trace["engine"], "lane": "exact",
            "horizon_ms": plan["horizon_ms"], "silence_limit_ms": silence_limit_ms(own_h), "port_open": port_open,
            "permits_honoured": honoured,
            "silence_limit_from": "the image's own source" if own_h else "the bench (repo bombyx_safety.h)",
        },
        # the ELF carries its build path, so two builds of one image differ here: kept, never hashed (fidelity probe)
        "build": {"elf_sha256": image.sha256_file(image_path)},
        "chip": {"end": trace["end"], "tx_bytes": len(trace["tx"]), "cpu": trace["cpu"], "memory": trace["mem"],
                 "timer_outputs": trace["ocm"], "uart": trace["uart"], "resets": trace["resets"],
                 "unmodelled": trace["unmodelled"], "app_starts_ms": [round(c * NS_PER_CYCLE / 1e6, 3) for c in trace["app"]],
                 "flash": trace["flash"], "stderr": trace["stderr"], "wdt": trace["wdt"]},
        **result,
        "refuse": any(f["severity"] == "refuse" for f in result["findings"]),
        "not_modelled": NOT_MODELLED,
    }
    if keep_trace:
        out["trace"] = {"pins": trace["pins"], "tx": trace["tx"], "rxq": trace["rxq"], "rxr": trace["rxr"]}
    out["prediction_sha256"] = hashlib.sha256(json.dumps({k: v for k, v in out.items() if k not in UNHASHED},
                                                         sort_keys=True, default=str).encode()).hexdigest()
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--image", help="the image .elf; or --commit to rebuild and check it")
    ap.add_argument("--commit", default=None)
    ap.add_argument("--plan", required=True, help="a plan JSON file")
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args()
    img = a.image or image.build_commit(a.commit or "HEAD")["elf"]
    print(json.dumps(predict(img, _load(a.plan), keep_trace=a.trace), indent=1, default=str))
