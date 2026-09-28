// SPDX-License-Identifier: BSD-2-Clause
// en_probe -- what holds the CNC shield's EN line (D8) while the Uno is NOT driving it, measured by the Uno itself.
//
// Each pin is read three ways, from the state reset leaves it in:
//   r0  as reset left it (input, no pull-up)
//   r1  with the chip's own weak pull-up on (20-50 k)
//   r2  20 us after a 10 us low pulse, released to input without pull-up
// up   = r2 is high: something pulls the line high again after it was emptied
// down = r1 is low: something holds it low even against the chip's pull-up
// none = neither: the line only keeps whatever drove it last (or a pull-down weaker than the chip's pull-up)
//
// Controls, so the D8 answer can be believed: D0 (RX) is held high by the USB chip through 1 k and must read "up";
// A5 has nothing attached on a CNC Shield V3 and must read "none". If either is wrong, the probe is wrong.
//
// Safety: STEP/DIR (D2-D7) are driven low before anything else, so no step can happen. D8 is driven low for 10 us
// (the drivers are enabled for that long, with no step); afterwards D8 is set HIGH (drivers off) level first, then
// direction, so it never glitches low. The motor cannot move.

struct Reading { uint8_t r0, r1, r2; };

static Reading probe(volatile uint8_t *ddr, volatile uint8_t *port, volatile uint8_t *pin, uint8_t m) {
  Reading r;
  *ddr &= ~m; *port &= ~m;              // input, no pull-up: as reset leaves it
  delay(5);
  r.r0 = (*pin & m) ? 1 : 0;
  *port |= m;                           // the chip's weak pull-up
  delay(5);
  r.r1 = (*pin & m) ? 1 : 0;
  *port &= ~m;                          // pull-up off (still an input)
  *ddr |= m;                            // drive low
  delayMicroseconds(10);
  *ddr &= ~m;                           // release: input, no pull-up
  delayMicroseconds(20);
  r.r2 = (*pin & m) ? 1 : 0;
  return r;
}

static const char *verdict(Reading r) {
  if (r.r2) return "up";
  if (!r.r1) return "down";
  return "none";
}

static Reading d8, d0, a5;

void setup() {
  PORTD &= ~0xFC; DDRD |= 0xFC;         // D2-D7 (STEP X/Y/Z, DIR X/Y/Z): outputs, low
  d8 = probe(&DDRB, &PORTB, &PINB, 1 << 0);
  PORTB |= 1 << 0; DDRB |= 1 << 0;      // D8 HIGH = drivers off: level first, then direction
  d0 = probe(&DDRD, &PORTD, &PIND, 1 << 0);
  a5 = probe(&DDRC, &PORTC, &PINC, 1 << 5);
  Serial.begin(57600);
}

static void show(const char *name, Reading r) {
  Serial.print(name); Serial.print('='); Serial.print(verdict(r));
  Serial.print("(r0="); Serial.print(r.r0); Serial.print(",r1="); Serial.print(r.r1);
  Serial.print(",r2="); Serial.print(r.r2); Serial.print(") ");
}

void loop() {
  Serial.print("EN_PROBE ");
  show("d8", d8); show("d0", d0); show("a5", a5);
  bool ok = verdict(d0)[0] == 'u' && verdict(a5)[0] == 'n';
  Serial.print("controls="); Serial.println(ok ? "ok" : "WRONG");
  delay(500);
}
