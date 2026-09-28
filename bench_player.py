"""bench_player -- drive a stepper on an Arduino Uno running BombyxFirmata, and stop it the moment you want.

Part of the Bombyx bench kit.

WHAT IT DOES
  turn N        N full turns, as ONE counted move: the Uno counts every step and says MOVE COMPLETE
  move N        N steps (negative = the other way), the same way
  play FILE     a timed show (shows/show_*.txt): every message at its time, the same bytes the twin ran

WHY IT IS SAFE TO LEAVE A MOTOR TO A PROGRAM
  * The Uno keeps its drivers energized only while a PERMIT keeps arriving. This player sends one every 100 ms while
    a move runs, and nothing else counts as one. If this program hangs, crashes, or the cable is pulled, the permits
    stop and the Uno de-energizes after 500 ms by itself. That was measured on the chip (2026-09-13).
  * Any key (or Ctrl-C) stops the move at once: PERMIT 0 goes out before anything else, and the Uno de-energizes and
    resets its steppers. On the bench, the Uno's own word arrived 90 ms after a PERMIT 0 (2026-09-28).
  * It refuses to move a board that does not answer as BombyxFirmata: stock Firmata has no silence stop.
  * D8, the CNC shield's ENABLE for every driver, is the firmware's alone. No message this player can send reaches it.

  python bench_player.py --port COM6 turn 1
  python bench_player.py --port /dev/ttyACM0 turn 12 --slot z --reverse
  python bench_player.py --port COM6 move -800 --speed 400 --accel 800
  python bench_player.py --port COM6 play shows/show_jitter.txt
  python bench_player.py --dry-run turn 1          # print the exact bytes and their times; opens no port

Exit codes: 0 done, 1 the Uno never reported MOVE COMPLETE, 2 refused (arguments or not BombyxFirmata),
3 stopped by a key or Ctrl-C, 4 the Uno did not report de-energizing when the permits stopped (check it before the
next run: that stop is what makes the rest safe).

Run it in a normal terminal (PowerShell, cmd, Windows Terminal, or a POSIX terminal) so a key press reaches it;
Ctrl-C works everywhere.

The bytes are built by firmata.py -- the SAME module the bench twin uses to predict what the chip will do, so what
this player sends is what the twin ran.
"""
import argparse
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# In the kit, firmata.py sits in twin/. In the Bombyx repo, the kit's sources live in kit/bench/ and the twin in
# tools/bench_twin/. Both places are named, nothing else is searched.
for _cand in (os.path.join(HERE, "twin"), os.path.join(HERE, "..", "..", "tools", "bench_twin")):
    if os.path.isfile(os.path.join(_cand, "firmata.py")):
        sys.path.insert(0, os.path.abspath(_cand))
        break
import firmata as fm  # noqa: E402

BAUD = 57600
#: the CNC Shield V3's slot pins (STEP, DIR). D8 is ENABLE for all of them, and it is the firmware's.
SLOTS = {"x": (2, 5), "y": (3, 6), "z": (4, 7)}
DEVICE = 0
PERMIT_EVERY_MS = 100.0
#: the first move goes out after the permit, CONFIG and settings have landed (as the bench's counted move did)
FIRST_MOVE_MS = 300.0
#: the port open resets the Uno through DTR: Optiboot, then the firmware (the bench waited 4 s)
BOOT_WAIT_S = 4.0
#: what this player will ask for. The Uno realises about 3830 steps/s at most (measured); 4000 is the bench's limit.
MAX_SPEED = 4000.0
MAX_ACCEL = 20000.0
MAX_STEPS = 1_000_000


class Job:
    """A schedule of (t_ms, bytes) from the start, and how it ends: at end_ms, or when the Uno says MOVE COMPLETE."""

    def __init__(self, schedule, end_ms, wait_complete, what, expected_ms=None):
        self.schedule = sorted(schedule, key=lambda x: x[0])
        self.end_ms = end_ms
        self.wait_complete = wait_complete
        self.what = what
        self.expected_ms = expected_ms


def move_time_ms(steps, speed, accel):
    """How long AccelStepper takes for |steps| at max speed `speed` and acceleration `accel` (0 = constant speed):
    the trapezoid, or the triangle when the move is too short to reach full speed."""
    n = abs(steps)
    if n == 0:
        return 0.0
    if not accel:
        return 1000.0 * n / speed
    ramp_steps = speed * speed / (2.0 * accel)
    if n >= 2 * ramp_steps:
        return 1000.0 * (n / speed + speed / accel)
    return 1000.0 * 2.0 * math.sqrt(n / accel)


def check_motion(steps, speed, accel):
    if not 0 < speed <= MAX_SPEED:
        raise ValueError("speed must be above 0 and at most %d steps/s (got %s)" % (MAX_SPEED, speed))
    if not 0 <= accel <= MAX_ACCEL:
        raise ValueError("acceleration must be 0 (constant speed) to %d steps/s^2 (got %s)" % (MAX_ACCEL, accel))
    if steps == 0 or abs(steps) > MAX_STEPS:
        raise ValueError("steps must be 1 to %d either way (got %s)" % (MAX_STEPS, steps))


def counted_move(steps, slot="z", reverse=False, speed=739.0, accel=1500.0, what=None):
    """ONE counted AccelStepper move: CONFIG the slot, set acceleration and speed, then STEP n at FIRST_MOVE_MS.
    The defaults are the bench's proven counted move (19,200 steps in 26.9 s on the metal, the twin 26.8 s)."""
    check_motion(steps, speed, accel)
    step_pin, dir_pin = SLOTS[slot]
    sched = [(0.0, fm.stepper_config_driver(DEVICE, step_pin, dir_pin, invert=1 if reverse else 0))]
    if accel:
        sched.append((0.0, fm.stepper_set_acceleration(DEVICE, float(accel))))
    sched.append((0.0, fm.stepper_set_speed(DEVICE, float(speed))))
    sched.append((FIRST_MOVE_MS, fm.stepper_step(DEVICE, int(steps))))
    t = move_time_ms(steps, speed, accel)
    return Job(sched, None, True, what or "%d steps on slot %s" % (steps, slot.upper()), expected_ms=FIRST_MOVE_MS + t)


def turn(turns, steps_per_rev=1600, **kw):
    steps = int(round(turns * steps_per_rev))
    word = "turn" if abs(turns) == 1 else "turns"
    return counted_move(steps, what="%g %s (%d steps) on slot %s" % (turns, word, steps, kw.get("slot", "z").upper()),
                        **kw)


def read_show(path):
    """A show file: '# comment', 'end <ms>', then '<t_ms> <hex>' lines -- one Firmata message each. The permit is not
    in the file: the player keeps it alive from the start to the end."""
    end, sched = None, []
    with open(path, encoding="ascii") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            a, b = line.split(None, 1)
            if a == "end":
                end = float(b)
                continue
            data = bytes.fromhex(b)
            if not data or data[0] != fm.START_SYSEX or data[-1] != fm.END_SYSEX:
                raise ValueError("%s:%d is not one Firmata sysex message" % (path, n))
            if len(data) >= 3 and data[1] == fm.BOMBYX_PERMIT:
                raise ValueError("%s:%d carries a permit; the player owns the permit" % (path, n))
            sched.append((float(a), data))
    if end is None:
        raise ValueError("%s has no 'end <ms>' line" % path)
    return Job(sched, end, False, "show %s" % os.path.basename(path))


def with_permits(job, start_ms=0.0, until_ms=None):
    """The whole stream as sent: a permit at start and every 100 ms up to the end, the schedule's messages at their
    times -- in the order the player writes them (at one instant, the permit first). Used by the twin."""
    end = job.end_ms if until_ms is None else until_ms
    out, t = [], 0.0
    while t < end:
        out.append((start_ms + t, 0, fm.permit(1)))
        t += PERMIT_EVERY_MS
    out += [(start_ms + ts, 1, b) for ts, b in job.schedule]
    out.sort(key=lambda x: (x[0], x[1]))
    return [(t, b) for t, _, b in out]


# ---- the port ----------------------------------------------------------------------------------------------------
class Keys:
    """'Was a key pressed?' without blocking, on Windows and on a POSIX terminal. Without a terminal only Ctrl-C."""

    def __init__(self):
        self.win = os.name == "nt"
        self.old = None
        if not self.win and sys.stdin.isatty():
            import termios
            import tty
            self.old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())

    def pressed(self):
        if self.win:
            import msvcrt
            if msvcrt.kbhit():
                msvcrt.getwch()
                return True
            return False
        if self.old is None:
            return False
        import select
        if select.select([sys.stdin], [], [], 0)[0]:
            sys.stdin.read(1)
            return True
        return False

    def close(self):
        if self.old is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old)


class Player:
    def __init__(self, port, out=print):
        self.port = port
        self.out = out
        self.rx = bytearray()
        self.t0 = time.monotonic()
        self.complete = None
        self.de_energized = None

    def ms(self):
        return (time.monotonic() - self.t0) * 1000.0

    def say(self, text):
        self.out("%8.0f ms  %s" % (self.ms(), text))

    def send(self, data):
        self.port.write(data)

    def pump(self):
        """Decode every complete message received so far; keep an unfinished sysex for the next call."""
        n = self.port.in_waiting
        if n:
            self.rx += self.port.read(n)
        end = self.rx.rfind(bytes([fm.END_SYSEX]))
        if end < 0:
            if fm.START_SYSEX not in self.rx and len(self.rx) > 3:
                self.rx.clear()
            return
        chunk = bytes(self.rx[:end + 1])
        del self.rx[:end + 1]
        for _, kind, fields in fm.decode(chunk):
            if kind == "string":
                self.say("UNO: %s" % fields["text"])
                if "de-energized" in fields["text"] and self.de_energized is None:
                    self.de_energized = self.ms()
            elif kind == "stepper_move_complete":
                self.complete = (self.ms(), fields["position"])
                self.say("UNO: MOVE COMPLETE, position %d" % fields["position"])
            elif kind == "firmware":
                self.say("UNO: firmware %s %d.%d" % (fields["name"], fields["major"], fields["minor"]))

    def firmware_name(self, wait_s=2.0):
        """The name the board gives for itself: from its boot banner, or asked for once."""
        names = []

        def grab():
            n = self.port.in_waiting
            if n:
                self.rx += self.port.read(n)
            for _, kind, fields in fm.decode(bytes(self.rx)):
                if kind == "firmware":
                    names.append(fields["name"])
        grab()
        if not names:
            self.send(fm.report_firmware())
            deadline = time.monotonic() + wait_s
            while not names and time.monotonic() < deadline:
                time.sleep(0.02)
                grab()
        self.rx.clear()
        return names[-1] if names else None

    def stop_now(self, why):
        self.send(fm.permit(0))
        self.say("STOP (%s): PERMIT 0 sent -- the Uno de-energizes now" % why)
        self.listen(1000.0)

    def listen(self, ms):
        until = self.ms() + ms
        while self.ms() < until:
            self.pump()
            time.sleep(0.01)

    def run(self, job, keys):
        """Play a job. Returns 0 done, 1 no MOVE COMPLETE, 3 stopped."""
        self.t0 = time.monotonic()
        if job.wait_complete:
            limit = job.expected_ms * 1.15 + 3000.0
            self.say("%s, about %.1f s; any key stops it" % (job.what, job.expected_ms / 1000.0))
        else:
            limit = job.end_ms
            self.say("%s, %d messages over %.1f s; any key stops it" % (job.what, len(job.schedule), limit / 1000.0))
        i, next_permit = 0, 0.0
        try:
            while True:
                now = self.ms()
                if keys.pressed():
                    self.stop_now("a key")
                    return 3
                if now >= limit or (job.wait_complete and self.complete):
                    break
                if now >= next_permit:
                    self.send(fm.permit(1))
                    next_permit = max(next_permit + PERMIT_EVERY_MS, now)
                while i < len(job.schedule) and job.schedule[i][0] <= now:
                    self.send(job.schedule[i][1])
                    i += 1
                self.pump()
                time.sleep(0.002)
        except KeyboardInterrupt:
            self.stop_now("Ctrl-C")
            return 3
        silent_from = self.ms()
        self.say("the permits stop: the Uno must de-energize on its 500 ms silence limit")
        self.listen(1200.0)
        # the silence stop is exercised at the end of every run, so it is seen to work every time -- or seen not to
        if self.de_energized is None or self.de_energized < silent_from:
            self.say("WARNING: the Uno did not report de-energizing after the permits stopped -- check the firmware "
                     "and the cable before the next run")
            code = 4
        else:
            self.say("the Uno de-energized %.0f ms after the last permit window" % (self.de_energized - silent_from))
            code = 0
        if job.wait_complete:
            if not self.complete:
                self.say("RESULT: no MOVE COMPLETE within %.1f s" % (limit / 1000.0))
                return 1
            self.say("RESULT: move complete at position %d, %.0f ms after the move began"
                     % (self.complete[1], self.complete[0] - FIRST_MOVE_MS))
            return code
        self.say("RESULT: %d of %d messages sent on schedule" % (i, len(job.schedule)))
        return code


def build_job(a):
    if a.what == "turn":
        return turn(float(a.value), steps_per_rev=a.steps_per_rev, slot=a.slot, reverse=a.reverse,
                    speed=a.speed, accel=a.accel)
    if a.what == "move":
        return counted_move(int(a.value), slot=a.slot, reverse=a.reverse, speed=a.speed, accel=a.accel)
    return read_show(a.value)


def arg_parser():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("what", choices=("turn", "move", "play"))
    ap.add_argument("value", help="turns, steps, or a show file")
    ap.add_argument("--port", help="the Uno's serial port (COM6, /dev/ttyACM0, ...)")
    ap.add_argument("--slot", choices=sorted(SLOTS), default="z", help="the CNC shield slot (default z)")
    ap.add_argument("--reverse", action="store_true", help="invert DIR: + turns the other way")
    ap.add_argument("--speed", type=float, default=739.0, help="max speed, steps/s (default 739)")
    ap.add_argument("--accel", type=float, default=1500.0, help="acceleration, steps/s^2; 0 = constant speed")
    ap.add_argument("--steps-per-rev", type=int, default=1600, help="1600 = a TMC2208 at 1/8 step (default)")
    ap.add_argument("--dry-run", action="store_true", help="print the bytes and their times; open no port")
    return ap


def main(argv=None):
    a = arg_parser().parse_args(argv)
    try:
        job = build_job(a)
    except (ValueError, OSError) as e:
        print("refused: %s" % e)
        return 2
    if a.dry_run:
        end = job.end_ms if not job.wait_complete else job.expected_ms * 1.15 + 3000.0
        print("# %s -- the stream as sent, permits included, until %.0f ms (the Uno may finish sooner)" % (job.what, end))
        for t, b in with_permits(job, 0.0, end):
            print("%9.1f %s" % (t, b.hex()))
        return 0
    if not a.port:
        print("refused: --port is required unless --dry-run")
        return 2
    import serial  # pyserial
    port = serial.Serial(a.port, BAUD, timeout=0)
    port.dtr = True
    keys = Keys()
    try:
        p = Player(port)
        p.say("opened %s: the Uno restarts; waiting %.0f s for its firmware" % (a.port, BOOT_WAIT_S))
        time.sleep(BOOT_WAIT_S)
        name = p.firmware_name()
        if name != "BombyxFirmata":
            p.say("refused: the board says it runs %r, not BombyxFirmata -- without its silence stop this player will "
                  "not move a motor" % name)
            return 2
        p.say("BombyxFirmata answered")
        return p.run(job, keys)
    finally:
        try:
            port.write(fm.permit(0))        # whatever happened above, the last word on the wire is "stop"
            port.flush()
        except Exception:
            pass                            # a dead port: the Uno's own 500 ms silence limit stops it anyway
        try:
            port.close()
        finally:
            keys.close()


if __name__ == "__main__":
    sys.exit(main())
