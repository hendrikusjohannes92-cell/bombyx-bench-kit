"""The two figures in README.md, drawn here so a number in them can be changed and both redrawn: python docs/figures.py

Each comes out twice, in the twin page's own palette, light and dark; the README's <picture> lets GitHub pick one.
Everything written on them is what README.md and the firmware's header state, with the measured numbers and dates."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
LIGHT = dict(ground="#ffffff", panel="#f6f7f2", hair="#c9cdbf", ink="#1f1b16", muted="#5b574d", faint="#7d786c",
             accent="#a4532f", good="#4f6639", bad="#a13d31", on_accent="#ffffff")
DARK = dict(ground="#0d1117", panel="#1f1b16", hair="#3a352c", ink="#ede4da", muted="#a39c8d", faint="#8a8274",
            accent="#cf8563", good="#9aad86", bad="#ca6b5c", on_accent="#161310")
FONT = "system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,'Cascadia Mono',Consolas,Menlo,monospace"


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x, y, s, size=13, fill="#000", weight="normal", anchor="start", family=FONT, style="normal"):
    return ('<text x="%s" y="%s" font-family="%s" font-size="%s" font-weight="%s" font-style="%s" fill="%s" '
            'text-anchor="%s">%s</text>' % (x, y, family, size, weight, style, fill, anchor, esc(s)))


def head(w, h, p, title):
    arrow = ('<marker id="%s" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
             'orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="%s"/></marker>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
            'aria-labelledby="t">\n<title id="t">%s</title>\n<defs>\n%s\n%s\n</defs>\n'
            '<rect x="0.5" y="0.5" width="%d" height="%d" rx="10" fill="%s" stroke="%s"/>\n'
            % (w, h, w, h, esc(title), arrow % ("ak", p["ink"]), arrow % ("aa", p["accent"]),
               w - 1, h - 1, p["panel"], p["hair"]))


def line(x1, y1, x2, y2, stroke, width=1.5, dash=None, end=None):
    return '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="%s"%s%s/>' % (
        x1, y1, x2, y2, stroke, width, ' stroke-dasharray="%s"' % dash if dash else "",
        ' marker-end="url(#%s)"' % end if end else "")


def box(x, y, w, h, p, stroke=None):
    return '<rect x="%s" y="%s" width="%s" height="%s" rx="8" fill="%s" stroke="%s" stroke-width="1.5"/>' % (
        x, y, w, h, p["ground"], stroke or p["hair"])


# ---- figure 1: the permit stream, and the 500 ms silence stop -------------------------------------------------------
def permit_stream(p):
    W, H = 880, 360
    x0, x1, span = 180.0, 850.0, 2.4                              # the time axis: 0 to 2.4 s
    x = lambda t: x0 + (x1 - x0) * t / span                       # noqa: E731
    hang, stop = 1.2, 1.7                                          # the last permit, and 500 ms later
    o = [head(W, H, p, "The permit is a stream, not an event: 500 ms of silence de-energizes the drivers.")]
    o.append(text(24, 36, "The permit is a stream, not an event.", 18, p["ink"], "600"))
    o.append(text(24, 58, "The Uno keeps the drivers energized only while a permit keeps arriving. Nothing else counts as one.",
                  13, p["muted"]))
    ay = 290                                                       # the time axis
    o.append(line(x0, ay, x1, ay, p["hair"], 1))
    for k in range(5):
        t = k * 0.5
        o.append(line(x(t), ay, x(t), ay + 5, p["hair"], 1))
        o.append(text(x(t), ay + 19, "%g s" % t, 11, p["faint"], anchor="middle"))
    L1 = 108                                                       # lane 1: the permits
    o.append(text(24, L1 - 2, "your program", 13, p["ink"], "600"))
    o.append(text(24, L1 + 15, "a permit every 100 ms", 11, p["muted"]))
    for k in range(int(round(hang / 0.1)) + 1):
        o.append('<rect x="%.1f" y="%s" width="3" height="26" rx="1" fill="%s"/>' % (x(k * 0.1) - 1.5, L1 - 13, p["accent"]))
    o.append(line(x(hang) + 12, L1, x1, L1, p["hair"], 1.5, "3 5"))
    o.append(text(x(hang) + 8, L1 - 20, "the program hangs, or the cable is pulled", 12, p["bad"], "600"))
    o.append(text(x(hang) + 8, L1 + 22, "no permit arrives", 11, p["faint"], style="italic"))
    by = 152                                                       # the 500 ms bracket
    o.append('<path d="M%s %s v-6 M%s %s h%s M%s %s v-6" fill="none" stroke="%s" stroke-width="1.5"/>'
             % (x(hang), by, x(hang), by - 3, x(stop) - x(hang), x(stop), by, p["ink"]))
    o.append(text((x(hang) + x(stop)) / 2, by + 15, "500 ms of silence", 12, p["ink"], "600", "middle"))
    L2 = 205                                                       # lane 2: ENABLE
    lo, hi = L2 + 10, L2 - 12
    o.append(text(24, L2 - 2, "D8, ENABLE", 13, p["ink"], "600"))
    o.append(text(24, L2 + 15, "every driver, active low", 11, p["muted"]))
    o.append('<path d="M%s %s H%s V%s H%s" fill="none" stroke="%s" stroke-width="2.5" stroke-linejoin="round"/>'
             % (x0, lo, x(stop), hi, x1, p["ink"]))
    o.append(text(x0 + 4, lo + 15, "LOW: the drivers are energized", 11, p["muted"]))
    o.append(text(x(stop) + 8, hi - 6, "HIGH: de-energized", 11, p["good"], "600"))
    o.append(text(x(stop) + 8, hi + 17, "by the Uno itself:", 11, p["good"]))
    o.append(text(x(stop) + 8, hi + 31, "no host, no cable needed", 11, p["good"]))
    L3 = 262                                                       # lane 3: the motor
    o.append(text(24, L3 + 5, "the motor", 13, p["ink"], "600"))
    o.append('<rect x="%s" y="%s" width="%.1f" height="24" rx="4" fill="%s"/>' % (x0, L3 - 12, x(stop) - x0, p["accent"]))
    o.append(text(x0 + 10, L3 + 4, "moving", 12, p["on_accent"], "600"))
    o.append(text(x(stop) + 8, L3 + 4, "stopped, and stays stopped", 11, p["ink"]))
    o.append(line(x(stop), L1 - 30, x(stop), L3 + 14, p["bad"], 1.5, "4 4"))    # the moment the Uno acts
    o.append(text(24, 336, "Measured on the Uno (2026-09-13): silence for 500 ms stopped the motor, and a returning permit did "
                  "not restart it. Any key sends PERMIT 0:", 11.5, p["muted"]))
    o.append(text(24, 351, "the Uno confirmed it had de-energized 90 ms later, USB round trip included (2026-09-28).",
                  11.5, p["muted"]))
    o.append("</svg>\n")
    return "\n".join(o)


# ---- figure 2: the twin runs the same bytes on the same firmware -----------------------------------------------------
def twin_pipeline(p):
    W, H = 880, 372
    o = [head(W, H, p, "The twin runs the same bytes on the same firmware, so what the page shows is what your Uno gets.")]
    o.append(text(24, 36, "The twin runs the same bytes on the same firmware.", 18, p["ink"], "600"))
    o.append(text(24, 58, "So what you see on the page is what the player sends to your Uno, byte for byte.", 13, p["muted"]))
    ax, ay, aw, ah = 24, 118, 196, 132                             # your script
    o.append(box(ax, ay, aw, ah, p))
    o.append(text(ax + 14, ay + 24, "your script", 13, p["ink"], "600"))
    for i, s in enumerate(("turn 1", "rest 500", "turn 2 ccw at 1200", "move 400", "to 0")):
        o.append(text(ax + 14, ay + 48 + i * 17, s, 12.5, p["ink"], family=MONO))
    my = ay + ah / 2
    o.append(line(ax + aw, my, 262, my, p["ink"], end="ak"))
    bx, by, bw, bh = 264, 138, 172, 92                             # the same bytes
    o.append(box(bx, by, bw, bh, p))
    o.append(text(bx + 14, by + 24, "the same bytes", 13, p["ink"], "600"))
    for i, s in enumerate(("Firmata messages and", "the permits, built once", "by twin/firmata.py")):
        o.append(text(bx + 14, by + 45 + i * 16, s, 11.5, p["muted"]))
    fx = 462                                                       # the fork
    cx, cy, cw, ch = 492, 76, 364, 112
    dx, dy, dw, dh = 492, 216, 364, 112
    o.append('<path d="M%s %s H%s V%s H%s" fill="none" stroke="%s" stroke-width="1.5" marker-end="url(#aa)"/>'
             % (bx + bw, my, fx, cy + ch / 2, cx - 2, p["accent"]))
    o.append('<path d="M%s %s H%s V%s H%s" fill="none" stroke="%s" stroke-width="1.5" marker-end="url(#ak)"/>'
             % (bx + bw, my, fx, dy + dh / 2, dx - 2, p["ink"]))
    o.append(box(cx, cy, cw, ch, p, stroke=p["accent"]))          # the twin
    o.append(text(cx + 14, cy + 24, "the twin", 13, p["accent"], "600"))
    o.append(text(cx + 82, cy + 24, "no motor, no Uno needed", 11, p["faint"], style="italic"))
    for i, s in enumerate(("the real firmware image on a simulated ATmega328P",
                           "(simavr), cycle by cycle: every STEP edge, every reply",
                           "the page: a NEMA 17 turning as the chip steps it,",
                           "the speed over time, the Uno's replies, its refusals")):
        o.append(text(cx + 14, cy + 45 + i * 16, s, 11.5, p["ink"] if i < 2 else p["muted"]))
    o.append(line(cx + 22, cy + ch, cx + 22, dy - 2, p["accent"], end="aa"))      # the motor button
    o.append(text(cx + 34, dy - 10, "one button runs exactly the run the twin showed", 11.5,
                  p["accent"], "600"))
    o.append(box(dx, dy, dw, dh, p))                               # your Uno
    o.append(text(dx + 14, dy + 24, "your Uno", 13, p["ink"], "600"))
    for i, s in enumerate(("the player sends them, with a permit every 100 ms",
                           "the Uno counts every step, reports MOVE COMPLETE",
                           "any key, STOP or a quiet page stops it; under all of it,",
                           "the Uno's own 500 ms stop")):
        o.append(text(dx + 14, dy + 45 + i * 16, s, 11.5, p["ink"] if i < 2 else p["muted"]))
    o.append(text(24, 348, "Measured: the twin predicted 26.8 s for a 19,200-step move the metal made in 26.9 s (2026-09-28),",
                  11.5, p["muted"]))
    o.append(text(24, 363, "and 395 steps where the chip counted 392 (2026-09-21).", 11.5, p["muted"]))
    o.append("</svg>\n")
    return "\n".join(o)


if __name__ == "__main__":
    for name, draw in (("permit-stream", permit_stream), ("twin-pipeline", twin_pipeline)):
        for suffix, palette in (("", LIGHT), ("-dark", DARK)):
            path = os.path.join(HERE, "%s%s.svg" % (name, suffix))
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(draw(palette))
            print("wrote", os.path.relpath(path, os.path.dirname(HERE)))
