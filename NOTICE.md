# Notices

## This kit

| files | licence | text |
|---|---|---|
| `firmware/bombyx_firmata/` (the sketch and its two headers) | BSD-2-Clause | `LICENSES/BSD-2-Clause.txt` |
| `firmware/en_probe/` (the shield probe the page puts on the Uno for a moment) | BSD-2-Clause | `LICENSES/BSD-2-Clause.txt` |
| `twin/chip_twin.c` | GPL-3.0-only: it is compiled against libsimavr, which is GPL-3.0 | `LICENSES/GPL-3.0.txt` |
| everything else | Apache License 2.0 | `LICENSE` |

Each source file's own `SPDX-License-Identifier` line is authoritative. `chip_twin.c` runs as a separate program: the
Python files start it and talk to it over stdin and stdout, and they link nothing of it.

## The firmware is built on ConfigurableFirmata, which is not included here

The firmware is a sketch for [ConfigurableFirmata](https://github.com/firmata/ConfigurableFirmata), the Firmata
project's library. It uses the library **unmodified**: nothing in this kit changes or replaces a file of it, and none
of its code is in this kit. You install it yourself:

```bash
arduino-cli lib install ConfigurableFirmata@3.3.0
```

This kit was tested with ConfigurableFirmata **3.3.0**, the release arduino-cli installs from the Arduino library
index. The current release is 3.4.0; it has not been tested here.

**What a compiled firmware image contains.** This was checked in the image's own symbol table, not assumed from the
`#include` lines:

| part | licence | copyright |
|---|---|---|
| this kit's sketch and headers | BSD-2-Clause | the Bombyx authors |
| ConfigurableFirmata 3.3.0: the core (`FirmataClass`), Digital and Analog Input/Output, AccelStepperFirmata, FirmataExt, FirmataReporting, Frequency | GNU LGPL 2.1 (the library's `LICENSE.txt`) | Hans-Christoph Steiner (2006–2008) and the Firmata developers |
| AccelStepper 1.57, in ConfigurableFirmata's `src/utility`: it generates the motor's steps | **GNU GPL version 2**, or a commercial licence from its author | Mike McCauley (2009–2013) |
| the Arduino AVR core 1.8.7, and parts of its Wire and SoftwareSerial libraries | GNU LGPL 2.1 | Nicholas Zambetti, David A. Mellis and the Arduino developers |

Not in the image, though installed alongside: ConfigurableFirmata's MultiStepper, OneWire, scheduler, Encoder7Bit and
DHT support; the DHT sensor library and Adafruit Unified Sensor, which arduino-cli installs as dependencies.

The shield probe's image (`firmware/en_probe`) holds only its own sketch (BSD-2-Clause) and the Arduino AVR core
(LGPL 2.1): no Firmata and no AccelStepper.

**If you share a compiled firmware image** (a `.hex` built from this sketch), you share all of the above, and their
licences apply to it. Because AccelStepper is GPL-2.0, the image as a whole must be shared on GPL-2.0 terms:
- offer the complete corresponding source: this sketch (BSD-2-Clause, which is compatible), ConfigurableFirmata 3.3.0
  and the Arduino AVR core 1.8.7 as you built them, and the build instructions (README, step 1);
- keep these notices;
- for the LGPL-2.1 parts, make it possible to rebuild the image with a changed library. Publishing the sketch and
  naming the versions does that.

This kit itself ships source only, and ships no image.

## The twin runs simavr, which is not included here

- **simavr 1.6**: GNU GPL-3.0. `twin/chip_twin.c` (GPL-3.0-only, above) is compiled on your machine against your
  installed `libsimavr`. No simavr code is in this kit.
- **arduino-cli** and the **Arduino AVR core**: installed by you, under their own licences.
- **pyserial**: BSD-3-Clause, installed by `pip install -r requirements.txt`.

Thanks to the Firmata project, to Mike McCauley, and to the Arduino developers: the motor in this kit moves on their
work.
