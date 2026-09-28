"""Firmata as BombyxFirmata speaks it: build the exact bytes a ByxIn plan sends, and read the bytes the chip answers.

One description of the wire, taken from the code that runs on the Uno, not from a protocol summary:
  * ConfigurableFirmata 3.3.0 (ConfigurableFirmata.h, AccelStepperFirmata.h/.cpp) -- the command numbers, the 7-bit
    packing, AccelStepper's 32-bit integer and its "custom float" (23-bit significand, 4-bit exponent from -11, sign);
  * bombyx_firmata.ino -- the permit sysex F0 01 <permit> F7, whose only argument read is argv[0];
  * os/sel4/arduino/bombyx_firmata/bench/a1_gates.ps1 -- the byte sequences that ran against the chip on 2026-09-13,
    which the tests in tools/test_bench_twin_*.py hold this module to.
The prediction does not trust this module to be right about the FIRMWARE: the chip twin runs the real image, so a
message this module builds wrongly is predicted as whatever the real firmware does with it.
"""

START_SYSEX, END_SYSEX = 0xF0, 0xF7
DIGITAL_MESSAGE, ANALOG_MESSAGE = 0x90, 0xE0
REPORT_ANALOG, REPORT_DIGITAL = 0xC0, 0xD0
SET_PIN_MODE, SET_DIGITAL_PIN_VALUE = 0xF4, 0xF5
REPORT_VERSION, SYSTEM_RESET = 0xF9, 0xFF
ACCELSTEPPER_DATA = 0x62
CAPABILITY_QUERY, CAPABILITY_RESPONSE = 0x6B, 0x6C
PIN_STATE_QUERY, PIN_STATE_RESPONSE = 0x6D, 0x6E
STRING_DATA, REPORT_FIRMWARE, SAMPLING_INTERVAL = 0x71, 0x79, 0x7A
BOMBYX_PERMIT = 0x01

AS_CONFIG, AS_ZERO, AS_STEP, AS_TO, AS_ENABLE, AS_STOP = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
AS_REPORT_POSITION, AS_LIMIT, AS_SET_ACCELERATION, AS_SET_SPEED, AS_MOVE_COMPLETE = 0x06, 0x07, 0x08, 0x09, 0x0A
MULTISTEPPER_CONFIG, MULTISTEPPER_TO, MULTISTEPPER_STOP, MULTISTEPPER_MOVE_COMPLETE = 0x20, 0x21, 0x23, 0x24

PIN_MODE_NAMES = {0x00: "INPUT", 0x01: "OUTPUT", 0x02: "ANALOG", 0x03: "PWM", 0x04: "SERVO", 0x05: "SHIFT", 0x06: "I2C",
                  0x07: "ONEWIRE", 0x08: "STEPPER", 0x09: "ENCODER", 0x0A: "SERIAL", 0x0B: "PULLUP", 0x7F: "IGNORE"}

#: 57600 8N1 is ten bits a byte on the wire -- the fastest a sender can place bytes one after another
BAUD = 57600
BYTE_US = 10 * 1_000_000 / BAUD


def _b7(v):
    if not 0 <= v <= 0x7F:
        raise ValueError("a Firmata data byte is 7 bits, not %r" % v)
    return v


def sysex(command, *data):
    return bytes([START_SYSEX, _b7(command)] + [_b7(d) for d in data] + [END_SYSEX])


def permit(value=1):
    """The Bombyx permit: feeds the 500 ms watchdog. bombyx_firmata.ino reads only argv[0]."""
    return sysex(BOMBYX_PERMIT, value)


def encode_int32(value):
    """AccelStepperFirmata::encode32BitSignedInteger: magnitude in 28 bits of 7-bit groups, sign in bit 3 of byte 5.
    (Its DECODER drops magnitude bits 28..31 -- `((arg5 << 28) & 0x07)` is always 0 -- so the chip twin, which runs the
    real decoder, is what says what a large count becomes.)"""
    mag = -value if value < 0 else value
    if mag >= 1 << 32:
        raise ValueError("more than 32 bits: %r" % value)
    d = [mag & 0x7F, (mag >> 7) & 0x7F, (mag >> 14) & 0x7F, (mag >> 21) & 0x7F, (mag >> 28) & 0x7F]
    if value < 0:
        d[4] |= 0x08
    return d


def encode_custom_float(value):
    """The inverse of AccelStepperFirmata::decodeCustomFloat: value = significand * 10^(exponent), significand < 2^23,
    exponent in -11..4. Chooses the exact representation with the exponent NEAREST ZERO (400.0 -> 400 x 10^0, the
    bytes a1_gates.ps1 sent; firmata.js would send 4 x 10^2). The chip multiplies by a 32-bit powf(10, exponent), and
    far exponents like 4000000 x 10^-4 decode to a slightly different float -- a different speed. If no exponent is
    exact, the finest representation that fits."""
    sign = 1 if value < 0 else 0
    v = abs(float(value))
    for exp in sorted(range(-11, 5), key=lambda e: (abs(e), e)):
        sig = v / (10.0 ** exp)
        r = round(sig)
        if r < (1 << 23) and abs(r - sig) <= 1e-9 * max(1.0, sig):
            break
    else:
        exp = next(e for e in range(-11, 5) if v / (10.0 ** e) < (1 << 23))
        r = round(v / (10.0 ** exp))
    b4 = ((r >> 21) & 0x03) | (((exp + 11) & 0x0F) << 2) | (sign << 6)
    return [r & 0x7F, (r >> 7) & 0x7F, (r >> 14) & 0x7F, b4]


def decode_custom_float(b1, b2, b3, b4):
    sig = b1 | (b2 << 7) | (b3 << 14) | ((b4 & 0x03) << 21)
    exp = ((b4 >> 2) & 0x0F) - 11
    return (-sig if (b4 >> 6) & 1 else sig) * (10.0 ** exp)


# ---- the commands a plan is made of ----------------------------------------------------------------------------------
def stepper_config_driver(device, step_pin, dir_pin, enable_pin=None, invert=0):
    """ACCELSTEPPER CONFIG, interface DRIVER (wire count 1, whole steps), as a1_gates.ps1 sends it."""
    interface = 0x10 | (0x01 if enable_pin is not None else 0x00)
    data = [AS_CONFIG, device, interface, step_pin, dir_pin]
    if enable_pin is not None:
        data.append(enable_pin)
    data.append(invert)
    return sysex(ACCELSTEPPER_DATA, *data)


def stepper_set_speed(device, steps_per_s):
    return sysex(ACCELSTEPPER_DATA, AS_SET_SPEED, device, *encode_custom_float(steps_per_s))


def stepper_set_acceleration(device, steps_per_s2):
    return sysex(ACCELSTEPPER_DATA, AS_SET_ACCELERATION, device, *encode_custom_float(steps_per_s2))


def stepper_step(device, steps):
    return sysex(ACCELSTEPPER_DATA, AS_STEP, device, *encode_int32(steps))


def stepper_to(device, position):
    return sysex(ACCELSTEPPER_DATA, AS_TO, device, *encode_int32(position))


def stepper_stop(device):
    return sysex(ACCELSTEPPER_DATA, AS_STOP, device)


def stepper_zero(device):
    return sysex(ACCELSTEPPER_DATA, AS_ZERO, device)


def report_firmware():
    return sysex(REPORT_FIRMWARE)


def capability_query():
    return sysex(CAPABILITY_QUERY)


def pin_state_query(pin):
    return sysex(PIN_STATE_QUERY, pin)


def set_digital_pin_value(pin, value):
    return bytes([SET_DIGITAL_PIN_VALUE, _b7(pin), _b7(value)])


def digital_message(port, mask):
    return bytes([DIGITAL_MESSAGE | (port & 0x0F), mask & 0x7F, (mask >> 7) & 0x7F])


def set_pin_mode(pin, mode):
    return bytes([SET_PIN_MODE, _b7(pin), _b7(mode)])


def system_reset():
    return bytes([SYSTEM_RESET])


# ---- reading what the chip sends -------------------------------------------------------------------------------------
def decode(stream):
    """Split a transmitted byte stream into messages: [(index_of_first_byte, kind, fields)]. Unknown bytes are kept as
    ('raw', ...) -- a decoder that drops what it does not recognise would hide exactly the surprises a twin is for."""
    out, i, n = [], 0, len(stream)
    while i < n:
        b = stream[i]
        if b == START_SYSEX:
            j = i + 1
            while j < n and stream[j] != END_SYSEX:
                j += 1
            if j >= n:
                out.append((i, "sysex_unterminated", {"bytes": list(stream[i:])}))
                break
            body = list(stream[i + 1:j])
            out.append((i, *_decode_sysex(body)))
            i = j + 1
        elif b == REPORT_VERSION and i + 2 < n:
            out.append((i, "report_version", {"major": stream[i + 1], "minor": stream[i + 2]}))
            i += 3
        elif (b & 0xF0) == DIGITAL_MESSAGE and i + 2 < n:
            out.append((i, "digital_message", {"port": b & 0x0F, "mask": stream[i + 1] | (stream[i + 2] << 7)}))
            i += 3
        elif (b & 0xF0) == ANALOG_MESSAGE and i + 2 < n:
            out.append((i, "analog_message", {"pin": b & 0x0F, "value": stream[i + 1] | (stream[i + 2] << 7)}))
            i += 3
        else:
            out.append((i, "raw", {"byte": b}))
            i += 1
    return out


def _text14(data):
    return "".join(chr(data[k] | (data[k + 1] << 7)) for k in range(0, len(data) - 1, 2))


def _decode_sysex(body):
    if not body:
        return "sysex_empty", {}
    cmd, data = body[0], body[1:]
    if cmd == STRING_DATA:
        return "string", {"text": _text14(data)}
    if cmd == REPORT_FIRMWARE and len(data) >= 2:
        return "firmware", {"major": data[0], "minor": data[1], "name": _text14(data[2:])}
    if cmd == CAPABILITY_RESPONSE:
        pins, modes, k = [], [], 0
        while k < len(data):
            if data[k] == 0x7F:
                pins.append(modes)
                modes = []
                k += 1
            else:
                modes.append({"mode": PIN_MODE_NAMES.get(data[k], hex(data[k])),
                              "resolution": data[k + 1] if k + 1 < len(data) else None})
                k += 2
        return "capabilities", {"pins": pins, "length": len(body) + 2}
    if cmd == PIN_STATE_RESPONSE and len(data) >= 2:
        return "pin_state", {"pin": data[0], "mode": PIN_MODE_NAMES.get(data[1], hex(data[1])), "mode_byte": data[1],
                             "state": sum(v << (7 * i) for i, v in enumerate(data[2:]))}
    if cmd == ACCELSTEPPER_DATA and len(data) >= 7 and data[0] in (AS_MOVE_COMPLETE, AS_REPORT_POSITION):
        d = data[2:7]
        mag = d[0] | (d[1] << 7) | (d[2] << 14) | (d[3] << 21)
        return ("stepper_move_complete" if data[0] == AS_MOVE_COMPLETE else "stepper_position"), {
            "device": data[1], "position": -mag if d[4] >> 3 == 1 else mag}
    return "sysex", {"command": cmd, "data": data}
