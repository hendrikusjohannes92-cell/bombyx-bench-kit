"""Run the chip layer: chip_twin (simavr, a separate GPL-3 process) on an exact image, with timed UART bytes and pin levels.

On the desk the harness runs in WSL (Ubuntu-22.04 has simavr 1.6 and libsimavr-dev); on a Linux host it runs directly.
It is built from tools/bench_twin/chip_twin.c and cached by the source's hash, so a changed harness is never mistaken
for the one that produced an older prediction.
"""
import hashlib
import os
import platform
import re
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "chip_twin.c")
DISTRO = "Ubuntu-22.04"
CYCLES_PER_US = 16          # 16 MHz: one cycle is 62.5 ns
ENGINE = "simavr-1.6"
_LOADER_SAYS = re.compile(r"^Loaded \d+ \.\w+( at address 0x[0-9a-fA-F]+)?$")


def source_sha(source=SOURCE):
    with open(source, "rb") as fh:
        return hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()


def _on_windows():
    return platform.system() == "Windows"


def to_wsl(path):
    p = os.path.abspath(path).replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        return "/mnt/%s%s" % (p[0].lower(), p[2:])
    return p


def _sh(cmd, stdin_text=None, timeout=900):
    if _on_windows():
        args = ["wsl.exe", "-d", DISTRO, "-e", "bash", "-c", cmd]
    else:
        args = ["bash", "-c", cmd]
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    r = subprocess.run(args, input=stdin_text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env=env)
    return r.returncode, r.stdout, r.stderr


def binary(source=SOURCE):
    """The harness built from this source (or a test's mutant of it); built once per source hash."""
    tag = source_sha(source)[:12]
    out = "/tmp/bench_twin/chip_twin-%s" % tag
    src = to_wsl(source) if _on_windows() else source
    rc, so, se = _sh("test -x %s || { mkdir -p /tmp/bench_twin && gcc -O2 -Wall -Wextra -o %s '%s' -lsimavr -lelf; }; "
                     "test -x %s && echo ok" % (out, out, src, out))
    if rc != 0 or "ok" not in so:
        raise RuntimeError("chip_twin did not build (needs gcc, libsimavr-dev, libelf-dev): %s" % (so + se)[-2000:])
    return out


RESET_CAUSES = ("por", "ext", "bor", "wdt")


def run(image_path, uart_events, pin_events=(), horizon_us=3_000_000, reset_to_boot=False, source=SOURCE,
        reset_cause="por", symbols=None):
    """uart_events: [(t_us, byte)] -- the time each byte's START bit begins at the Uno's RX pin.
    pin_events: [(t_us, 'D9', 0|1)] -- external levels on INPUT pins.
    reset_to_boot: start in the boot section (a with_bootloader hex); reset_cause: what MCUSR says (a port open's DTR
    pulse is 'ext'); symbols: the application's ELF, for its heap symbols when the image is a hex.
    Returns {pins, ocm, mem, uart, resets, unmodelled, ext, app, flash, stderr, tx, rxq, rxr, cpu, end, ...}."""
    if reset_cause not in RESET_CAUSES:
        raise ValueError("a reset cause is one of %s" % (RESET_CAUSES,))
    lines = ["horizon_us %d" % int(horizon_us)]
    lines += ["uart %d %02x" % (int(t), b) for t, b in uart_events]
    lines += ["pin %d %s %d" % (int(t), p, v) for t, p, v in pin_events]
    wsl = to_wsl if _on_windows() else (lambda x: x)
    cmd = "%s --firmware '%s' --reset-cause %s%s%s" % (binary(source), wsl(image_path), reset_cause,
                                                        " --reset-to-boot" if reset_to_boot else "",
                                                        " --symbols '%s'" % wsl(symbols) if symbols else "")
    rc, so, se = _sh(cmd, stdin_text="\n".join(lines) + "\n")
    if rc != 0:
        raise RuntimeError("chip_twin failed (%d): %s" % (rc, (so + se)[-2000:]))
    trace = parse(so, horizon_us, source)
    # whatever simavr says on stderr is kept and judged, never dropped (found 2026-09-15: 'ihex contains more chunks
    # than loaded' went to stderr with rc 0 while the core ran into erased flash)
    trace["stderr"] = [l for l in se.splitlines() if l.strip()]
    return trace


def parse(text, horizon_us, source=SOURCE):
    pins, ocm, mem, tx, rxq, rxr, cpu, end = {}, {}, {}, [], [], [], None, {}
    uart, resets, unmodelled, ext, app, flash, wdt = [], [], [], [], [], {}, []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "PIN":
            pins.setdefault(parts[2], []).append((int(parts[1]), parts[3]))
        elif parts[0] == "OCM":
            ocm.setdefault(parts[2], []).append((int(parts[1]), parts[3] == "1"))
        elif parts[0] == "UART":
            uart.append({"cycle": int(parts[1]), **{k: int(v) for k, _, v in (kv.partition("=") for kv in parts[2:])}})
        elif parts[0] == "RST":
            resets.append({"cycle": int(parts[1]), "mcusr": int(parts[2].partition("=")[2], 16)})
        elif parts[0] == "WDT":
            wdt.append((int(parts[1]), int(parts[2], 16)))
        elif parts[0] == "EXT":
            ext.append((int(parts[1]), parts[2], int(parts[3])))
        elif parts[0] == "APP":
            app.append(int(parts[1]))
        elif parts[0] == "FLASH":
            flash = {k: int(v) for k, _, v in (kv.partition("=") for kv in parts[1:])}
        elif parts[0] == "UNMODELLED":
            unmodelled.append({"cycle": int(parts[1]), "what": parts[2]})
        elif parts[0] == "MEM":
            for kv in parts[1:]:
                k, _, v = kv.partition("=")
                mem[k] = int(v) if re.fullmatch(r"-?\d+", v) else v
        elif parts[0] == "TX":
            tx.append((int(parts[1]), int(parts[2], 16)))
        elif parts[0] == "RXQ":
            rxq.append((int(parts[1]), int(parts[2])))
        elif parts[0] == "RXR":
            rxr.append((int(parts[1]), int(parts[2], 16)))
        elif parts[0] == "CPU":
            cpu = {"cycle": int(parts[1]), "state": parts[2]}
        elif parts[0] == "END":
            for kv in parts[1:]:
                k, _, v = kv.partition("=")
                end[k] = v
        elif not _LOADER_SAYS.match(line):                  # simavr's ELF loader: "Loaded 1124 .text at address 0x0"
            raise RuntimeError("chip_twin said something this parser does not know: %r" % line[:200])
    if not end:
        raise RuntimeError("chip_twin produced no END line -- the run did not finish")
    if not mem:
        raise RuntimeError("chip_twin produced no MEM line -- the harness is not the one this parser reads")
    if not flash:
        raise RuntimeError("chip_twin produced no FLASH line -- the harness is not the one this parser reads")
    return {"pins": pins, "ocm": ocm, "mem": mem, "uart": uart, "resets": resets, "unmodelled": unmodelled, "ext": ext,
            "app": app, "flash": flash, "wdt": wdt, "tx": tx, "rxq": rxq, "rxr": rxr, "cpu": cpu, "end": end, "horizon_us": horizon_us,
            "engine": ENGINE,
            "harness_sha256": source_sha(source)}


def level_at(changes, cycle):
    """The pin state in force at `cycle` (changes are (cycle, state), in order)."""
    state = None
    for c, s in changes:
        if c > cycle:
            break
        state = s
    return state
