"""bench_twin -- see what the motor will do before it moves: the kit's firmware, run on a simulated Uno.

Part of the Bombyx bench kit. It takes the SAME arguments as bench_player.py and feeds
the SAME bytes, at the same times, to the real firmware image running on simavr (a cycle-accurate ATmega328P
simulator). It then reports what the chip did with them: the STEP edges on each slot, how many steps, how fast, the
Uno's own replies, and anything the twin cannot know.

  python bench_twin.py turn 12 --reverse
  python bench_twin.py move -800 --speed 400 --accel 800
  python bench_twin.py play shows/show_jitter.txt
  python bench_twin.py turn 1 --json            # the whole prediction
  python bench_twin.py turn 12 --show           # see it: a page with the shaft turning, opened in your browser

How close it is: on the bench, the twin predicted 395 steps where the chip counted 392 (2026-09-21), and 26.8 s for a
19,200-step move the metal made in 26.9 s (2026-09-28). What it cannot know -- torque, load, a stalled motor -- it
says it cannot know; a prediction is commanded motion, not a promise about the shaft.

Needs: arduino-cli 1.5.1 with the arduino:avr 1.8.7 core and the ConfigurableFirmata 3.3.0 library (the versions the
proven image was built with -- the build refuses others), and simavr 1.6 with libsimavr-dev, libelf-dev and gcc
(Linux or macOS; on Windows, inside WSL's Ubuntu-22.04).
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (os.path.join(HERE, "twin"), os.path.join(HERE, "..", "..", "tools", "bench_twin")):
    if os.path.isfile(os.path.join(_cand, "predict.py")):
        sys.path.insert(0, os.path.abspath(_cand))
        break
sys.path.insert(0, HERE)
import bench_player as player  # noqa: E402
import image  # noqa: E402
import predict  # noqa: E402

#: the firmware this twin runs: the kit's own copy, or in the Bombyx repo the sketch the kit is exported from
SKETCH_CANDIDATES = (os.path.join(HERE, "firmware", "bombyx_firmata"),
                     os.path.join(HERE, "..", "..", "os", "sel4", "arduino", "bombyx_firmata"))
#: the port open resets the Uno: Optiboot, then the firmware. The player waits 4 s before its first byte; so does this.
START_MS = 4000.0
SKETCH_HELP = ("the sketch folder your Uno was flashed from, when that is not this kit's firmware/bombyx_firmata "
               "(an older or edited build): the twin must run what the Uno runs")


def sketch_dir():
    for c in SKETCH_CANDIDATES:
        if os.path.isfile(os.path.join(c, "bombyx_firmata.ino")):
            return os.path.abspath(c)
    raise RuntimeError("no firmware/bombyx_firmata beside this file")


def sketch_key(d):
    h = hashlib.sha256()
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if os.path.isfile(p) and name.endswith((".ino", ".h", ".cpp", ".c")):
            with open(p, "rb") as fh:
                h.update(name.encode() + b"\0" + fh.read().replace(b"\r\n", b"\n") + b"\0")
    h.update(json.dumps(image.PINNED, sort_keys=True).encode())
    return h.hexdigest()[:16]


def firmware_image(sketch=None):
    """Build the firmware once per content (and the pinned toolchain), then reuse it while every artefact is there.
    sketch: another sketch folder to build instead of the kit's own -- the twin must run what your Uno runs."""
    d = os.path.abspath(sketch) if sketch else sketch_dir()
    if not os.path.isfile(os.path.join(d, os.path.basename(d) + ".ino")):
        raise RuntimeError("%s is not a sketch folder: it needs %s.ino inside" % (d, os.path.basename(d)))
    root = os.environ.get("BOMBYX_REAL_TEMP") or tempfile.gettempdir()
    out = os.path.join(root, "bombyx_bench_kit_images", sketch_key(d))
    manifest = os.path.join(out, "manifest.json")
    if os.path.exists(manifest):
        with open(manifest, encoding="utf-8") as fh:
            m = json.load(fh)
        if image.cache_incomplete(m) is None:
            return m
    os.makedirs(out, exist_ok=True)
    m = image.build(d, out)
    with open(manifest, "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=1)
    return m


def plan_for(job):
    """The player's stream, as the twin receives it: the same messages and permits, from START_MS. A counted move is
    open loop here (the twin cannot stop at MOVE COMPLETE mid-run), so the permits run to a little past its expected
    end -- permits after the move changes nothing, and the chip's own replies say when it completed."""
    until = job.end_ms if not job.wait_complete else job.expected_ms * 1.05 + 500.0
    sends = [{"t_ms": t, "hex": b.hex()} for t, b in player.with_permits(job, START_MS, until)]
    return {"sends": sends, "horizon_ms": START_MS + until + 1200.0, "lane": "exact", "port_open": True}


#: what the findings a first-time user will meet mean, and how each one clears -- in the user's words
EXPLAIN = {
    "enable_path_unmeasured": "Your shield's EN facts are not measured yet. While the Uno restarts, D8 is not driven, "
                              "and the shield decides whether the drivers are on. Press Measure my shield on the page "
                              "(or see the README, 'Measure your shield once') and this clears.",
    "enable_while_undriven": "Your shield does not hold EN high while the Uno restarts, so the drivers can be on for the "
                             "second or so it takes to start, every time a program opens the port. If there is a cap on "
                             "the EN/GND pins, take it off; if not, fit a 10 kOhm resistor from EN to 5 V. Then measure "
                             "again.",
    "chip_reset_while_running": "Opening the port restarts the Uno (its bootloader hands over with a watchdog reset). "
                                "Every run that opens the port shows this.",
    "slot_a_dir_line_changes": "D13 (the Uno's LED) blinks during the restart. On a CNC shield D13 can be slot A's DIR "
                               "if its jumpers say so; with nothing in slot A this moves nothing.",
    "rate_below_command": "The average speed includes the speed-up and slow-down, so the move takes longer than "
                          "steps / speed.",
}


def summary(job, p, steps_per_rev, reverse=False):
    """net_steps is in the COMMANDED sense: with --reverse the firmware inverts DIR, so a + move drives DIR low."""
    stepped = {k: v for k, v in p["slots"].items() if v["rising_edges"]}
    out = {"job": job.what, "image_hex_sha256": p["inputs"]["image"]["hex_sha256"][:16],
           "refuse": p["refuse"], "slots": {}, "uno": [], "findings": []}
    for slot, z in sorted(stepped.items()):
        net = z["microsteps_dir_high"] - z["microsteps_dir_low"]
        if reverse:
            net = -net
        first, last = z["first_edge_ms"], z["last_edge_ms"]
        out["slots"][slot] = {"steps": z["rising_edges"], "net_steps": net, "net_turns": round(net / steps_per_rev, 4),
                              "moving_s": round((last - first) / 1000.0, 3) if first is not None else None,
                              "max_rate_sps": z["max_rate_sps"], "mean_rate_sps": z["mean_rate_sps"]}
    for m in p["firmware_messages"]:
        if m["kind"] == "string":
            out["uno"].append(m.get("text"))
        elif m["kind"] in ("stepper_move_complete",):
            out["uno"].append("MOVE COMPLETE, position %s" % m.get("position"))
    out["findings"] = [{"id": f["id"], "severity": f["severity"], "text": f.get("text", "")} for f in p["findings"]]
    return out


VIEW_TEMPLATE = os.path.join(HERE, "twin_view_template.html")
VIEW_NOTE = ("Commanded motion, from the firmware's own STEP edges on a simulated ATmega328P (simavr): each step is a "
             "rising STEP edge while ENABLE (D8) is driven low, signed by the DIR line at that edge. The twin cannot "
             "know torque, load, supply or a stalled motor, so the real shaft can lag or stop where this one does not.")


def shaft_view(job, p, reverse, steps_per_rev, sample_ms=10.0):
    """The page's data: per stepped slot, the shaft's position (steps) and speed (steps/s) every sample_ms, from the
    trace the chip produced -- the same edges the text summary counts. Time 0 is the player's first byte."""
    import bisect
    import math
    b = predict.bench()
    pins = p["trace"]["pins"]
    en = pins.get(b["enable"], [])
    ns = predict.NS_PER_CYCLE
    tracks, last_ms = [], 0.0
    for slot, sp in sorted(b["slots"].items()):
        step, dirs = pins.get(sp.get("step"), []), pins.get(sp.get("dir"), [])
        times, signs, prev = [], [], None
        for c, s in step:
            rising, prev = (s == "1" and prev == "0"), s
            if not rising or predict.chip.level_at(en, c) != "0":
                continue                                      # not a step: not a rise, or the driver not enabled
            d = predict.chip.level_at(dirs, c)
            if d not in ("0", "1"):
                continue
            sign = 1 if d == "1" else -1
            times.append(c * ns / 1e6 - START_MS)
            signs.append(-sign if reverse else sign)          # in the COMMANDED sense, as the summary reports it
        if times:
            tracks.append((slot, times, signs))
            last_ms = max(last_ms, times[-1])
    events = []
    for msg in p["firmware_messages"]:
        t = msg["t_ms"] - START_MS
        if t < 0:
            continue
        if msg["kind"] == "string":
            events.append({"t_ms": round(t, 1), "text": msg.get("text", "")})
        elif msg["kind"] == "stepper_move_complete":
            events.append({"t_ms": round(t, 1), "text": "MOVE COMPLETE at position %s" % msg.get("position")})
    t_end = max(last_ms + 800.0, max((e["t_ms"] for e in events), default=0.0) + 300.0, 1000.0)
    n = int(math.ceil(t_end / sample_ms)) + 1
    slots = []
    for slot, times, signs in tracks:
        cum = [0]
        for s in signs:
            cum.append(cum[-1] + s)
        pos_at = lambda t: cum[bisect.bisect_right(times, t)]                 # noqa: E731
        pos = [pos_at(k * sample_ms) for k in range(n)]
        half = 25.0                                                            # a 50 ms window for the speed
        speed = [round((pos_at(k * sample_ms + half) - pos_at(k * sample_ms - half)) * 1000.0 / (2 * half), 1)
                 for k in range(n)]
        slots.append({"slot": slot, "steps_per_rev": steps_per_rev, "pos": pos, "speed": speed,
                      "net_turns": cum[-1] / float(steps_per_rev), "steps": len(times)})
    s = summary(job, p, steps_per_rev, reverse)
    return {"job": job.what, "image": s["image_hex_sha256"], "sample_ms": sample_ms, "n_samples": n, "t_end_ms": t_end,
            "slots": slots, "events": events, "refuse": s["refuse"], "note": VIEW_NOTE,
            "findings": [{"id": f["id"], "severity": f["severity"], "text": EXPLAIN.get(f["id"], f["text"])}
                         for f in s["findings"]]}


def render_page(data, config=None):
    """The page with its data and its config filled in. config None = a file on disk: no upload, no motor."""
    with open(VIEW_TEMPLATE, encoding="utf-8") as fh:
        page = fh.read()
    for mark in ("__TWIN_DATA__", "__TWIN_CONFIG__"):
        if page.count(mark) != 1:
            raise RuntimeError("the view template has lost its %s placeholder" % mark)
    safe = lambda obj: json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")   # noqa: E731 -- no string may close the script
    return page.replace("__TWIN_DATA__", safe(data)).replace("__TWIN_CONFIG__", safe(config or {"served": False}))


def write_view(data, path):
    page = render_page(data)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(page)
    return os.path.abspath(path)


# ---- the page as a local app: upload a script, see the twin, run it on the motor --------------------------------------
EXAMPLE_SCRIPT = """# One step per line (or several, separated by ;). The motor starts where it stands.
turn 1
rest 500
turn 1 ccw at 1200 accel 4000
move 400
to 0
"""
MAX_UPLOAD_BYTES = 64 * 1024
MAX_TWIN_MS = 180 * 1000.0              # the simulation runs about as long as the move: keep an upload to 3 minutes
HEARTBEAT_LIMIT_S = 0.6                 # the page says "still here" every 200 ms while the motor runs


def job_from_upload(req):
    """(job, reverse, steps_per_rev) from the page's request -- a script or a show file -- or ValueError in words."""
    text = req.get("text") or ""
    name = os.path.basename(str(req.get("name") or "script.txt"))[:80]
    if len(text.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise ValueError("the file is over %d KB" % (MAX_UPLOAD_BYTES // 1024))
    slot = str(req.get("slot") or "z").lower()
    if slot not in player.SLOTS:
        raise ValueError("slot must be x, y or z")
    reverse = bool(req.get("reverse"))
    try:
        spr = int(req.get("steps_per_rev") or 1600)
    except ValueError:
        raise ValueError("steps per turn must be a whole number")
    if not 1 <= spr <= 100000:
        raise ValueError("steps per turn must be between 1 and 100000")
    if player.looks_like_a_show(text):
        job, reverse = player.read_show_text(text, name), False      # a show carries its own CONFIG
    else:
        job = player.compile_script(text, name, slot=slot, reverse=reverse, steps_per_rev=spr)
    if job.end_ms > MAX_TWIN_MS:
        raise ValueError("it runs %.0f s; on this page the twin simulates at most %.0f s" % (job.end_ms / 1000.0,
                                                                                          MAX_TWIN_MS / 1000.0))
    return job, reverse, spr


def job_digest(job):
    h = hashlib.sha256(("%r" % job.end_ms).encode())
    for t, b in job.schedule:
        h.update(("%r:" % t).encode() + b)
    return h.hexdigest()[:24]


# ---- your Uno: which port, what it runs, and the shield's EN line measured by the Uno itself --------------------------
#: USB ids of an Uno, or of the chip most Uno clones use. Only these are picked for you, and finding them opens nothing.
UNO_USB_IDS = {(0x2341, 0x0043): "Arduino Uno", (0x2341, 0x0001): "Arduino Uno", (0x2341, 0x0243): "Arduino Uno",
               (0x2A03, 0x0043): "Arduino Uno", (0x1A86, 0x7523): "CH340, the chip most Uno clones use"}
PROBE_SKETCH = os.path.join(HERE, "firmware", "en_probe")
PROBE_READ_S = 8.0                      # the restart, then a line every 500 ms: two alike well inside this
_PIN = r"(up|down|none)\(r0=([01]),r1=([01]),r2=([01])\)"
PROBE_LINE = re.compile(r"EN_PROBE d8=%s d0=%s a5=%s controls=(ok|WRONG)" % (_PIN, _PIN, _PIN))
#: what the probe's answer about D8 (EN) means, in the user's words
MEASURE_SAYS = {
    "up": "EN is held high while the Uno restarts: the drivers stay off until the firmware turns them on.",
    "down": "EN is held low: the drivers are on while the Uno restarts. If there is a cap on the EN/GND pins, take it "
            "off; if not, fit a 10 kOhm resistor from EN to 5 V. Then measure again.",
    "none": "Nothing holds EN while the Uno restarts, so the drivers may be on then. Fit a 10 kOhm resistor from EN "
            "to 5 V, then measure again.",
}


def list_ports():
    """Every serial port, named by its USB id, the Uno-like ones marked. Listing opens nothing."""
    from serial.tools import list_ports as lp
    out = []
    for p in sorted(lp.comports(), key=lambda p: p.device):
        kind = UNO_USB_IDS.get((p.vid, p.pid))
        out.append({"device": p.device, "usb": "%04X:%04X" % (p.vid, p.pid) if p.vid is not None else None,
                    "what": kind or p.description or "", "uno_like": kind is not None})
    return out


def parse_probe(text):
    """The probe's answer once it has said the same thing twice in a row; None until then."""
    last = None
    for line in text.splitlines():
        m = PROBE_LINE.search(line)
        if not m:
            continue
        g = m.groups()
        got = {name: {"verdict": g[i], "r0": int(g[i + 1]), "r1": int(g[i + 2]), "r2": int(g[i + 3])}
               for name, i in (("d8", 0), ("d0", 4), ("a5", 8))}
        got["controls"] = g[12]
        if got == last:
            return got
        last = got
    return None


def controls_hold(result):
    """D0 is held high by the USB chip through 1 kOhm, and nothing is on A5: a probe that reads otherwise is wrong."""
    return result["controls"] == "ok" and result["d0"]["verdict"] == "up" and result["a5"]["verdict"] == "none"


def upload_hex(port, hex_path):
    """Flash an image through the Uno's bootloader with the arduino-cli the twin builds with, verified after writing."""
    image._run([image.cli(), "upload", "--fqbn", image.FQBN, "--port", port, "--input-file", hex_path, "--verify"],
               timeout=180)


def _kit_owns(path):
    try:
        return os.path.commonpath([os.path.abspath(path), HERE]) == HERE
    except ValueError:                                            # another drive
        return False


def facts_writable():
    """The page writes only a kit's own twin/bench_facts.json. In the Bombyx repo that file is the reference bench's
    record, and a measurement goes into it by hand."""
    return _kit_owns(predict.FACTS)


def wiring_writable():
    """Likewise twin/wiring.json: a kit's own, never the repo's reference bench."""
    return _kit_owns(predict.WIRING)


def slot_dir_invert():
    """{slot: dir_invert} from the wiring the twin reads -- the one place a motor's turning sense is written down."""
    try:
        with open(predict.WIRING, encoding="utf-8") as fh:
            w = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {str(a["slot"]).lower(): bool(a.get("dir_invert")) for a in w.get("actuators", []) if a.get("slot")}


def record_direction(slot, dir_invert):
    """One motor's dir_invert into the kit's twin/wiring.json, everything else in the file kept as it was."""
    with open(predict.WIRING, encoding="utf-8") as fh:
        w = json.load(fh)
    motors = [a for a in w.get("actuators", []) if str(a.get("slot", "")).lower() == slot]
    if len(motors) != 1:
        raise ValueError("twin/wiring.json has %s motor in slot %s" % ("no" if not motors else "more than one",
                                                                       slot.upper()))
    motors[0]["dir_invert"] = dir_invert
    tmp = predict.WIRING + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(w, indent=2) + "\n")
    os.replace(tmp, predict.WIRING)


def shield_state():
    try:
        with open(predict.FACTS, encoding="utf-8") as fh:
            shield = json.load(fh).get("shield", {})
    except (OSError, ValueError):
        shield = {}
    return {k: {f: shield.get(k, {}).get(f) for f in ("value", "provenance", "source")}
            for k in ("en_pull", "en_gnd_jumper_fitted")}


def record_shield(result, when):
    """The probe's answer into the kit's facts, everything else in the file kept as it was."""
    with open(predict.FACTS, encoding="utf-8") as fh:
        facts = json.load(fh)
    shield = facts.setdefault("shield", {})
    d8 = result["d8"]
    shield["en_pull"] = dict(
        shield.get("en_pull", {}), value=d8["verdict"], provenance="MEASURED",
        source="the Uno's own probe (firmware/en_probe) from the twin page, %s: D8 read %d as reset leaves it, %d with "
               "the chip's pull-up, %d 20 us after a 10 us low pulse; the controls read D0 %s, A5 %s"
               % (when, d8["r0"], d8["r1"], d8["r2"], result["d0"]["verdict"], result["a5"]["verdict"]))
    if d8["r1"]:
        # a cap from EN to GND holds the line at 0 V against the chip's pull-up: EN read high means there is none
        shield["en_gnd_jumper_fitted"] = dict(
            shield.get("en_gnd_jumper_fitted", {}), value=False, provenance="MEASURED",
            source="the same probe, %s: EN read high with the chip's pull-up on, which a cap from EN to GND would "
                   "not allow" % when)
    tmp = predict.FACTS + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(facts, indent=1) + "\n")
    os.replace(tmp, predict.FACTS)


class PageKeys:
    """What stops a motor run started from the page: the page's STOP (or any key there), or the page going quiet."""

    def __init__(self, run):
        self.run = run

    def pressed(self):
        return self.run["stop"].is_set() or time.monotonic() - self.run["last_seen"] > HEARTBEAT_LIMIT_S

    def close(self):
        pass


class Bench:
    """The server's state: the last preview (the only job the motor button may run) and the motor run, if any."""

    def __init__(self, uno_port=None, open_serial=None, predict_job=None, sketch=None, upload=None, ports=None):
        import secrets
        import threading
        self.uno_port = uno_port
        self.sketch = sketch
        self.token = secrets.token_hex(16)
        self.lock = threading.Lock()
        self.predicting = threading.Lock()
        self.preview = None
        self.run = None
        self.firmware = None            # what the Uno last said it runs: {"port", "name"}
        self.checking = False
        self.measure = None
        self.open_serial = open_serial or self._open_serial
        self.predict_job = predict_job or self._predict_job
        self.upload = upload or upload_hex
        self.ports = ports or list_ports

    def _busy(self):
        """Why the port may not be used now, or None. Everything that opens the port asks this, under the lock."""
        if self.run is not None and self.run["state"] in ("starting", "running"):
            return "the motor is running"
        if self.measure is not None and self.measure["state"] == "running":
            return "the shield is being measured"
        if self.checking:
            return "the Uno is being asked what it runs"
        return None

    def auto_pick(self):
        """With no port given, take the only Uno-like one; with none or several, the page asks."""
        if not self.uno_port:
            only = [p for p in self.ports() if p["uno_like"]]
            if len(only) == 1:
                self.uno_port = only[0]["device"]
        return self.uno_port

    def uno(self):
        ports = self.ports()
        with self.lock:
            m = self.measure
            return {"port": self.uno_port, "ports": ports, "firmware": self.firmware, "busy": self._busy(),
                    "shield": shield_state(), "can_record": facts_writable(),
                    "dir_invert": slot_dir_invert(), "can_set_direction": wiring_writable(),
                    "measure": None if m is None else {"state": m["state"], "lines": m["lines"][-60:],
                                                       "result": m["result"]}}

    def set_port(self, req):
        port = str(req.get("port") or "")
        if port not in [p["device"] for p in self.ports()]:
            raise ValueError("%s is not a serial port on this computer now" % (port or "no port"))
        with self.lock:
            busy = self._busy()
            if busy:
                raise ValueError("not now: %s" % busy)
            self.uno_port, self.firmware = port, None
        return self.uno()

    def set_direction(self, req):
        slot = str(req.get("slot") or "").lower()
        if slot not in player.SLOTS:
            raise ValueError("slot must be x, y or z")
        if not isinstance(req.get("dir_invert"), bool):
            raise ValueError("dir_invert must be true or false")
        if not wiring_writable():
            raise ValueError("this page's wiring is the Bombyx repo's reference bench (%s): change it there"
                             % predict.WIRING)
        with self.lock:
            busy = self._busy()
            if busy:
                raise ValueError("not now: %s" % busy)
            record_direction(slot, req["dir_invert"])
        return self.uno()

    def _ask_name(self):
        """Open the port (the Uno restarts), wait for its firmware, and ask its name: REPORT_FIRMWARE and nothing else."""
        port = self.open_serial()
        try:
            time.sleep(player.BOOT_WAIT_S)
            return player.Player(port, out=lambda s: None).firmware_name()
        finally:
            port.close()

    def check(self):
        with self.lock:
            if not self.uno_port:
                raise ValueError("choose your Uno's port first")
            busy = self._busy()
            if busy:
                raise ValueError("not now: %s" % busy)
            self.checking, port = True, self.uno_port
        try:
            name = self._ask_name()
        finally:
            with self.lock:
                self.checking = False
        with self.lock:
            self.firmware = {"port": port, "name": name}
        return self.uno()

    def measure_start(self):
        import threading
        with self.lock:
            if not self.uno_port:
                raise ValueError("choose your Uno's port first")
            busy = self._busy()
            if busy:
                raise ValueError("not now: %s" % busy)
            self.measure = {"state": "running", "lines": [], "result": None, "port": self.uno_port}
            m = self.measure
        threading.Thread(target=self._measure, args=(m,), daemon=True).start()
        return {"ok": True}

    def _read_probe(self):
        port = self.open_serial()                                  # the Uno restarts into the probe
        try:
            rx, deadline = bytearray(), time.monotonic() + PROBE_READ_S
            while time.monotonic() < deadline:
                n = port.in_waiting
                if n:
                    rx += port.read(n)
                    got = parse_probe(rx.decode("ascii", "replace"))
                    if got:
                        return got
                time.sleep(0.02)
            return None
        finally:
            port.close()

    def _put_back(self, m, back):
        say = m["lines"].append
        try:
            say("putting back the firmware this page twins (image %s)" % back["hex_sha256"][:16])
            self.upload(m["port"], back["hex"])
            name = self._ask_name()
        except Exception as e:                                    # noqa: BLE001 -- say it, whatever it is
            say("could not put the firmware back: %s. The Uno runs either the probe (which keeps the drivers off) or "
                "what it ran before; flash the firmware as in the README's step 1 before running the motor" % e)
            return False
        if name != "BombyxFirmata":
            say("after putting it back the Uno says it runs %r, not BombyxFirmata: flash it as in the README's step 1"
                % name)
            return False
        with self.lock:
            self.firmware = {"port": m["port"], "name": name}
        say("the Uno runs BombyxFirmata again")
        return True

    def _measure(self, m):
        """Probe on, read the shield, firmware back, whatever happens in between; record only what the controls back."""
        say = m["lines"].append
        try:
            say("building the probe and the firmware this page twins (the first time takes a minute or two)")
            probe, back = firmware_image(PROBE_SKETCH), firmware_image(self.sketch)
        except Exception as e:                                    # noqa: BLE001
            say("could not build them: %s. Nothing was sent to the Uno" % e)
            m["state"] = "failed"
            return
        result = None
        try:
            say("putting the probe on %s (nothing can move: it holds STEP and DIR low)" % m["port"])
            self.upload(m["port"], probe["hex"])
            say("reading the shield")
            result = self._read_probe()
            if result is None:
                say("the probe did not say the same thing twice within %.0f s" % PROBE_READ_S)
        except Exception as e:                                    # noqa: BLE001
            say("the probe failed: %s" % e)
        back_ok = self._put_back(m, back)                          # always: the probe was at least on its way
        try:
            self._conclude(m, result, back_ok)
        except Exception as e:                                    # noqa: BLE001 -- never leave the port "busy"
            say("could not finish: %s" % e)
            m["state"] = "failed"

    def _conclude(self, m, result, back_ok):
        say = m["lines"].append
        believed = result is not None and controls_hold(result)
        if result is not None and not believed:
            say("the controls read wrong (D0 %s, must be up; A5 %s, must be none), so the EN reading is not believed "
                "and nothing is recorded. Is something plugged into A5?" % (result["d0"]["verdict"],
                                                                         result["a5"]["verdict"]))
        recorded = False
        if believed:
            verdict = result["d8"]["verdict"]
            say(MEASURE_SAYS[verdict])
            if facts_writable():
                record_shield(result, time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
                recorded = True
                say("recorded in twin/bench_facts.json")
            else:
                say("not written: this page's facts are the Bombyx repo's reference bench (%s); record it there by "
                    "hand" % predict.FACTS)
        m["result"] = {"verdict": result["d8"]["verdict"] if believed else None,
                       "says": MEASURE_SAYS[result["d8"]["verdict"]] if believed else None,
                       "recorded": recorded, "firmware_back": back_ok}
        m["state"] = "done" if believed and back_ok else "failed"

    def _open_serial(self):
        import serial
        port = serial.Serial(self.uno_port, player.BAUD, timeout=0)
        port.dtr = True
        return port

    def _predict_job(self, job, reverse, spr):
        m = firmware_image(self.sketch)
        p = predict.predict(m["elf"], plan_for(job), keep_trace=True)
        return shaft_view(job, p, reverse, spr)

    def config(self):
        return {"served": True, "uno": bool(self.uno_port), "token": self.token, "example": EXAMPLE_SCRIPT,
                "dir_invert": slot_dir_invert()}

    def predict(self, req):
        job, reverse, spr = job_from_upload(req)
        if not self.predicting.acquire(blocking=False):
            raise ValueError("the twin is already running a script; wait for it")
        try:
            data = self.predict_job(job, reverse, spr)
        finally:
            self.predicting.release()
        run_id = job_digest(job)
        with self.lock:
            self.preview = {"run_id": run_id, "job": job, "refuse": data["refuse"]}
            busy = self._busy()
        why = (None if self.uno_port and not data["refuse"] and not busy else
               "the twin refuses this run: clear the refusals first" if data["refuse"] else
               "choose your Uno's port above to run it on your motor" if not self.uno_port else busy)
        data.update({"run_id": run_id, "motor_ready": why is None, "motor_why": why,
                     "seconds": round(job.end_ms / 1000.0, 1)})
        return data

    def start(self, req):
        import threading
        with self.lock:
            pv = self.preview
            if not self.uno_port:
                raise ValueError("no Uno port: choose your Uno's port on the page")
            if pv is None or req.get("run_id") != pv["run_id"]:
                raise ValueError("this is not the run the twin just showed: show it on the twin again first")
            if pv["refuse"]:
                raise ValueError("the twin refuses this run")
            busy = self._busy()
            if busy:
                raise ValueError("not now: %s" % busy)
            self.run = {"state": "starting", "lines": [], "code": None, "stop": threading.Event(),
                        "last_seen": time.monotonic(), "run_id": pv["run_id"]}
            run, job = self.run, pv["job"]
        threading.Thread(target=self._run_motor, args=(run, job), daemon=True).start()
        return {"ok": True}

    def _run_motor(self, run, job):
        keys = PageKeys(run)
        out = run["lines"].append
        try:
            port = self.open_serial()
        except Exception as e:                                    # noqa: BLE001 -- say it, whatever it is
            out("could not open %s: %s" % (self.uno_port, e))
            run.update(state="failed", code=2)
            return
        code = 2
        try:
            p = player.Player(port, out=out)
            p.say("opened %s: the Uno restarts; waiting %.0f s for its firmware" % (self.uno_port, player.BOOT_WAIT_S))
            until = time.monotonic() + player.BOOT_WAIT_S
            while time.monotonic() < until:
                if keys.pressed():
                    p.say("stopped before the move began")
                    code = 3
                    return
                time.sleep(0.02)
            name = p.firmware_name()
            if name != "BombyxFirmata":
                p.say("refused: the board says it runs %r, not BombyxFirmata" % name)
                return
            run["state"] = "running"
            code = p.run(job, keys)
        finally:
            try:
                port.write(fm_permit0())
                port.flush()
            except Exception:                                     # noqa: BLE001 -- a dead port: the Uno stops by itself
                pass
            try:
                port.close()
            except Exception:                                     # noqa: BLE001
                pass
            run.update(code=code, state={0: "done", 3: "stopped"}.get(code, "failed"))

    def heartbeat(self):
        with self.lock:
            if self.run is not None:
                self.run["last_seen"] = time.monotonic()
        return {"ok": True}

    def stop(self):
        with self.lock:
            if self.run is not None:
                self.run["stop"].set()
        return {"ok": True}

    def status(self):
        with self.lock:
            r = self.run
            return {"state": "idle"} if r is None else {"state": r["state"], "code": r["code"], "lines": r["lines"][-80:]}


def fm_permit0():
    return player.fm.permit(0)


def make_handler(bench, port):
    from http.server import BaseHTTPRequestHandler
    hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):                                # quiet: the terminal is for the motor's lines
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self, need_token):
            # only this machine, by name (a DNS-rebinding page arrives under another Host), and for anything that acts,
            # the page's own token (a cross-site page cannot read it, and cannot send the header without a preflight
            # this server never answers)
            if self.headers.get("Host") not in hosts:
                return False
            return not need_token or self.headers.get("X-Bench-Token") == bench.token

        def do_GET(self):
            if not self._allowed(False):
                return self._send(403, {"error": "this page answers on 127.0.0.1 only"})
            if self.path in ("/", "/index.html"):
                return self._send(200, render_page(None, bench.config()), "text/html")
            gets = {"/api/motor/status": bench.status, "/api/uno": bench.uno}
            if self.path in gets:
                if not self._allowed(True):
                    return self._send(403, {"error": "missing the page's token"})
                return self._send(200, gets[self.path]())
            return self._send(404, {"error": "not here"})

        def do_POST(self):
            # read the body BEFORE answering, even a refusal: a server that answers and closes with the body unread
            # resets the connection on Windows, and the caller sees a broken connection instead of the refusal
            # (found 2026-09-28: the no-token 403 arrived as ConnectionResetError one run in six)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_UPLOAD_BYTES * 2:
                self.close_connection = True
                return self._send(413, {"error": "too large"})
            body = self.rfile.read(n) if n else b""
            if not self._allowed(True):
                return self._send(403, {"error": "missing the page's token"})
            try:
                req = json.loads(body or b"{}")
                routes = {"/api/predict": lambda: bench.predict(req), "/api/motor/start": lambda: bench.start(req),
                          "/api/motor/heartbeat": bench.heartbeat, "/api/motor/stop": bench.stop,
                          "/api/uno/port": lambda: bench.set_port(req), "/api/uno/check": bench.check,
                          "/api/uno/measure": bench.measure_start,
                          "/api/uno/direction": lambda: bench.set_direction(req)}
                if self.path not in routes:
                    return self._send(404, {"error": "not here"})
                return self._send(200, routes[self.path]())
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:                                # noqa: BLE001 -- the page shows it
                return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})
    return Handler


def serve(port=8740, uno_port=None, open_browser=True, sketch=None):
    from http.server import ThreadingHTTPServer
    bench = Bench(uno_port, sketch=sketch)
    picked = bench.auto_pick() if not uno_port else None
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(bench, port))
    url = "http://127.0.0.1:%d/" % port
    print("the bench twin page: %s   (Ctrl-C ends it)" % url)
    print("the twin runs the firmware built from: %s" % (os.path.abspath(sketch) if sketch else sketch_dir()))
    print("your Uno: %s" % (("%s -- the page's motor button runs only what the twin has just shown" % bench.uno_port
                             + (" (picked: the only Uno-like port here; change it on the page)" if picked else ""))
                            if bench.uno_port else "none chosen yet -- choose its port on the page"))
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        bench.stop()
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["serve"]:
        import argparse
        sp = argparse.ArgumentParser(description="the bench twin as a page: upload a script, see it, run it on the motor")
        sp.add_argument("serve")
        sp.add_argument("--port", type=int, default=8740, help="the page's port on 127.0.0.1 (default 8740)")
        sp.add_argument("--uno", help="the Uno's serial port (COM6, /dev/ttyACM0); without it the page takes the only "
                                      "Uno-like port, or lets you choose")
        sp.add_argument("--no-open", action="store_true", help="do not open the browser")
        sp.add_argument("--sketch", metavar="DIR", help=SKETCH_HELP)
        s = sp.parse_args(argv)
        return serve(s.port, s.uno, not s.no_open, s.sketch)
    ap = player.arg_parser()
    ap.description = __doc__.splitlines()[0]
    ap.add_argument("--sketch", metavar="DIR", help=SKETCH_HELP)
    ap.add_argument("--json", action="store_true", help="print the whole prediction")
    ap.add_argument("--show", nargs="?", const="twin_view.html", metavar="FILE",
                    help="write the twin as a page (default twin_view.html) and open it in your browser")
    ap.add_argument("--no-open", action="store_true", help="with --show: write the page, do not open it")
    a = ap.parse_args(argv)
    try:
        job = player.build_job(a)
    except (ValueError, OSError) as e:
        print("refused: %s" % e)
        return 2
    m = firmware_image(a.sketch)
    p = predict.predict(m["elf"], plan_for(job), keep_trace=bool(a.show))
    if a.json:
        print(json.dumps({k: v for k, v in p.items() if k != "trace"}, indent=1, default=str))
        return 0
    if a.show:
        path = write_view(shaft_view(job, p, a.reverse, a.steps_per_rev), a.show)
        print("the twin, as a page: %s" % path)
        if not a.no_open:
            import pathlib
            import webbrowser
            webbrowser.open(pathlib.Path(path).as_uri())
    s = summary(job, p, a.steps_per_rev, a.reverse)
    print("the twin ran: %s on firmware %s" % (s["job"], s["image_hex_sha256"]))
    if not s["slots"]:
        print("  no STEP edges on any slot: nothing would move")
    for slot, z in s["slots"].items():
        print("  slot %s: %d steps, net %+d (%+.3f turns), moving %.2f s, peak %.0f steps/s"
              % (slot, z["steps"], z["net_steps"], z["net_turns"], z["moving_s"] or 0.0, z["max_rate_sps"] or 0.0))
    for line in s["uno"]:
        print("  the Uno would say: %s" % line)
    for f in s["findings"]:
        print("  %s [%s]: %s" % (f["id"], f["severity"], EXPLAIN.get(f["id"], f["text"])))
    print("  verdict: %s" % ("REFUSE -- do not run this on the metal until the refusals above are cleared"
                             if s["refuse"] else "nothing the twin refuses"))
    return 1 if s["refuse"] else 0


if __name__ == "__main__":
    sys.exit(main())
