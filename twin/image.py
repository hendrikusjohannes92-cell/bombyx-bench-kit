"""The exact image a prediction runs: built from pinned sources, and named by the hash of what was flashed.

A prediction is only about the Uno if it runs the bytes on the Uno. The flashed hex itself is not kept in git (*.hex is
ignored under os/sel4/arduino/), so the image is REBUILT from the commit's sketch with the toolchain that built it, and
its hash is compared with the hash the flash record states. On 2026-09-15 a clean desk build of 6abceaf's sketch gave
exactly the recorded 540b0f5b0a9b1622 -- the identity is reproducible, not assumed.

What this proves and does not: the rebuilt image equals the image that avrdude wrote and verified on 2026-09-13
13:47:51. Whether the chip still holds it needs a readback (Jan's go; the Uno resets on DTR), and fuses/EEPROM are not
readable through Optiboot at all (STK_UNIVERSAL answers 0x00) -- an ISP programmer is the only honest source for those.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FQBN = "arduino:avr:uno"
#: the toolchain that built the flashed image (the flash record of 2026-09-13)
PINNED = {"arduino-cli": "1.5.1", "arduino:avr": "1.8.7", "ConfigurableFirmata": "3.3.0"}
#: images whose identity a flash record states -- the sha256 prefix of the .ino.hex arduino-cli writes
RECORDED = {
    "540b0f5b0a9b1622": "BombyxFirmata as flashed and verified 2026-09-13 13:47:51 (A1 evidence :18), sketch 6abceaf",
    "d94eaf638211121c": "BombyxFirmata first flash 2026-09-13 13:35:10 (A1 evidence :12): FirmataExt's sysex dropped",
}
#: what is on the Uno now, per the last flash record: its sketch commit and the hash the rebuild must reproduce
FLASHED = {"commit": "6abceaf", "hex_sha256_prefix": "540b0f5b0a9b1622"}
CLI_CANDIDATES =(r"C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe", "arduino-cli")
#: every artefact a caller may need beside the image. A cached build is reused only when it holds ALL of them.
#: 2026-09-21 (from a full suite): a directory keyed on the SKETCH cannot notice that what callers NEED
#: changed. The optiboot work made predict.py require the with_bootloader hex for a port open; a cache built
#: 2026-09-15, before that hex was ever asked for, kept answering "valid" and 22 tests failed on the image it
#: handed back. The key covers the inputs; this covers the outputs.
CACHED_ARTEFACTS = ("hex", "elf", "with_bootloader_hex")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def cli():
    for c in CLI_CANDIDATES:
        p = c if os.path.isabs(c) else shutil.which(c)
        if p and os.path.exists(p):
            return p
    raise RuntimeError("no arduino-cli: the pinned image cannot be built here")


def _run(args, timeout=600):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("%s failed (%d): %s" % (args[1] if len(args) > 1 else args[0], r.returncode,
                                                   (r.stdout + r.stderr)[-2000:]))
    return r.stdout


def toolchain():
    """The versions actually present, read from arduino-cli itself -- never assumed to be the pinned ones."""
    c = cli()
    out = {"arduino-cli": None, "arduino:avr": None, "ConfigurableFirmata": None, "cli_path": c}
    v = json.loads(_run([c, "version", "--json"]))
    out["arduino-cli"] = v.get("VersionString") or v.get("version")
    for core in json.loads(_run([c, "core", "list", "--json"])).get("platforms", []):
        if core.get("id") == "arduino:avr":
            out["arduino:avr"] = core.get("installed_version") or core.get("installed")
    for lib in json.loads(_run([c, "lib", "list", "--json"])).get("installed_libraries", []):
        l = lib.get("library", {})
        if l.get("name") == "ConfigurableFirmata":
            out["ConfigurableFirmata"] = l.get("version")
    return out


def check_pins(tc):
    wrong = {k: (tc.get(k), v) for k, v in PINNED.items() if tc.get(k) != v}
    if wrong:
        raise RuntimeError("the toolchain is not the one that built the flashed image: %s" % wrong)


def build(sketch_dir, out_dir):
    """Compile a sketch directory with a clean, private build path. Returns the manifest of the result."""
    tc = toolchain()
    check_pins(tc)
    name = os.path.basename(os.path.normpath(sketch_dir))
    build_path = os.path.join(out_dir, "build")
    os.makedirs(build_path, exist_ok=True)
    _run([tc["cli_path"], "compile", "--fqbn", FQBN, "--clean", "--build-path", build_path, sketch_dir])
    hexf = os.path.join(build_path, name + ".ino.hex")
    elff = os.path.join(build_path, name + ".ino.elf")
    boot = os.path.join(build_path, name + ".ino.with_bootloader.hex")
    m = {"sketch": name, "fqbn": FQBN, "toolchain": {k: tc[k] for k in PINNED},
         "hex": hexf, "elf": elff, "with_bootloader_hex": boot if os.path.exists(boot) else None,
         "hex_sha256": sha256_file(hexf), "elf_sha256": sha256_file(elff)}
    m["recorded_as"] = RECORDED.get(m["hex_sha256"][:16])
    return m


def cache_incomplete(m):
    """Why a cached manifest may NOT be reused -- a sentence, or None when it may.

    Reused is the dangerous direction: a manifest that answers "valid" while an artefact a caller needs is absent
    hands back an image the caller cannot use, and the failure surfaces far from here (predict.py, 22 tests deep).
    So every artefact is required to be named AND on disk, and the hex must still hash to what the manifest says.
    """
    for k in CACHED_ARTEFACTS:
        p = m.get(k)
        if not p:
            return "the manifest names no %s -- it was built before that artefact was required" % k
        if not os.path.exists(p):
            return "%s is not in the build directory any more" % os.path.basename(p)
    if sha256_file(m["hex"]) != m["hex_sha256"]:
        return "the hex no longer hashes to what the manifest records -- the build directory was touched"
    return None


def build_commit(commit, sketch_rel="os/sel4/arduino/bombyx_firmata", out_dir=None, cache=False):
    """Build the sketch exactly as a commit holds it (git show, never the working tree). cache=True reuses a build of
    the same full commit id and sketch path (the manifest's hash is still what callers check)."""
    if cache and out_dir is None:
        full = _run(["git", "-C", ROOT, "rev-parse", commit]).strip()
        out_dir = os.path.join(os.environ.get("BOMBYX_REAL_TEMP") or tempfile.gettempdir(), "bench_twin_images", full[:16] + "_" + sketch_rel.replace("/", "_"))
        manifest = os.path.join(out_dir, "manifest.json")
        if os.path.exists(manifest):
            with open(manifest, encoding="utf-8") as fh:
                m = json.load(fh)
            why = cache_incomplete(m)
            if why is None:
                return m
            # say it out loud: a cache that rebuilds silently is how this bug stayed hidden from 2026-09-15 to 09-21.
            # The rebuild terminates -- a clean build of this sketch produces all of CACHED_ARTEFACTS (measured
            # 2026-09-23: 26.1 s cold, and the hex still reproduces the flash record's 540b0f5b0a9b1622).
            print("bench twin: rebuilding the cached image of %s -- %s" % (commit, why), file=sys.stderr)
        m = build_commit(commit, sketch_rel, out_dir)
        with open(manifest, "w", encoding="utf-8") as fh:
            json.dump(m, fh, indent=1)
        return m
    out_dir = out_dir or tempfile.mkdtemp(prefix="bench_twin_image_")
    name = os.path.basename(sketch_rel)
    src = os.path.join(out_dir, "src", name)
    os.makedirs(src, exist_ok=True)
    files = _run(["git", "-C", ROOT, "ls-tree", "--name-only", commit, sketch_rel + "/"]).split()
    for f in files:
        if f.endswith((".ino", ".h", ".cpp", ".c")):
            blob = subprocess.run(["git", "-C", ROOT, "show", "%s:%s" % (commit, f)], capture_output=True, check=True).stdout
            with open(os.path.join(src, os.path.basename(f)), "wb") as fh:
                fh.write(blob)
    m = build(src, out_dir)
    m["commit"] = commit
    return m


def flashed():
    """The image on the Uno per the flash record, rebuilt -- refused if the rebuild does not reproduce the record's hash."""
    m = build_commit(FLASHED["commit"], cache=True)
    if not m["hex_sha256"].startswith(FLASHED["hex_sha256_prefix"]):
        raise RuntimeError("the rebuild of %s gave %s, not the flashed %s -- the toolchain or the sketch changed; a "
                           "prediction would be about a different chip" % (FLASHED["commit"], m["hex_sha256"][:16],
                                                                           FLASHED["hex_sha256_prefix"]))
    return m


if __name__ == "__main__":
    import sys
    print(json.dumps(build_commit(sys.argv[1] if len(sys.argv) > 1 else "HEAD"), indent=1))
