"""bench_twin -- see what the motor will do before it moves: the kit's firmware, run on a simulated Uno.

Part of the Bombyx bench kit. It takes the SAME arguments as bench_player.py and feeds
the SAME bytes, at the same times, to the real firmware image running on simavr (a cycle-accurate ATmega328P
simulator). It then reports what the chip did with them: the STEP edges on each slot, how many steps, how fast, the
Uno's own replies, and anything the twin cannot know.

  python bench_twin.py turn 12 --reverse
  python bench_twin.py move -800 --speed 400 --accel 800
  python bench_twin.py play shows/show_jitter.txt
  python bench_twin.py turn 1 --json            # the whole prediction

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
import sys
import tempfile

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


def firmware_image():
    """Build the firmware once per content (and the pinned toolchain), then reuse it while every artefact is there."""
    d = sketch_dir()
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
    "enable_path_unmeasured": "Your shield's EN facts are not measured yet (README, 'Measure your shield once'). While "
                              "the Uno restarts, D8 is not driven, and the shield decides whether the drivers are on. "
                              "Measure it once and this clears.",
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


def main(argv=None):
    ap = player.arg_parser()
    ap.description = __doc__.splitlines()[0]
    ap.add_argument("--json", action="store_true", help="print the whole prediction")
    a = ap.parse_args(argv)
    try:
        job = player.build_job(a)
    except (ValueError, OSError) as e:
        print("refused: %s" % e)
        return 2
    m = firmware_image()
    p = predict.predict(m["elf"], plan_for(job))
    if a.json:
        print(json.dumps(p, indent=1, default=str))
        return 0
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
