/*
 * chip_twin.c -- the bench Uno's ATmega328P, whole-chip, running the EXACT image: every pin and every UART byte.
 *
 * Jan, 2026-09-15: "make sure the system knows exactly how the hardware works internally and can predict all
 * behaviour if you give the arduino its code through a byxin command" -- for both Firmata streams sent to the
 * flashed-once BombyxFirmata and whole sketches (his call). This file is the CHIP layer of that prediction and
 * nothing else: simavr executes the same machine code the Uno executes, cycle by cycle at 16 MHz, and this harness
 * records what the chip does at its pins. Shield routing, the TMC2208, the motor and the shaft are separate layers
 * (tools/bench_twin/predict.py), because they depend on the bench, not on the code.
 *
 * WHAT IT RECORDS, every change with its cycle (1 cycle = 62.5 ns):
 *   PIN  <cycle> <pin> <state>    D0..D13, A0..A5; state 1/0 = driven (DDR=1), Z = input, P = input with pull-up,
 *                                 W = driven by a timer waveform whose level simavr does not produce (see effective()).
 *                                 A pin whose timer output compare is connected (COMnx != 0, analogWrite's PWM) is
 *                                 driven by the waveform, not by PORT -- simavr raises that waveform only on the
 *                                 timer's compare IRQ, never on the port's PIN_ALL, so it is tracked from there
 *                                 (found 2026-09-15 by the red team: analogWrite(3,128) recorded no edge at all)
 *   OCM  <cycle> <pin> <0|1>      the pin's timer output compare was connected (1) or disconnected (0)
 *   TX   <cycle> <byte>           every byte the firmware transmits on USART0
 *   RXR  <cycle> <byte>           the firmware READ UDR0 (a received byte consumed). Compared with each byte's wire time
 *                                 upstream: simavr raises RXC for the next queued byte only a byte-time after the
 *                                 previous READ, so under back-to-back bytes its delivery lags the wire, which the
 *                                 silicon's receiver (clocked by the wire) does not. The lag is measured, not hidden.
 *   RXQ  <cycle> <depth>          an injected byte found simavr's receive queue `depth` deep (simavr's own pacing; this
 *                                 is NOT a silicon overrun signal -- see RXR)
 *   CPU  <cycle> <state>          the core stopped (done/crashed) before the horizon
 *   MEM  sp_min=.. ...            the stack's low-water mark and, for an ELF, the heap top's high-water mark and the
 *                                 least distance between them (see watch_memory())
 *   UART <cycle> bit_cycles=.. word_bits=.. simavr_cycles_per_byte=..   the frame the firmware set, as silicon clocks it
 *   RST  <cycle> mcusr=<hex>      a reset while running (the watchdog); every pin is re-reported
 *   UNMODELLED <cycle> <what>     the firmware relied on something simulated unfaithfully (see unmodelled())
 *   EXT  <cycle> <pin> <0|1>      a stimulus put this level on an input from outside (PIN still says what the CHIP drives)
 *   WDT  <cycle> <value>          every write to WDTCSR (the watchdog's enable and timeout, as the firmware sets them)
 *   APP  <cycle>                  the bootloader handed over to the application (with --reset-to-boot)
 *   FLASH changed_bytes=.. first_changed=..   flash written during the run (compared with the image as loaded)
 *   END  cycle=<n> ...            the horizon, and a summary
 *
 * WHAT IT READS (stdin, one event per line, times in microseconds from power-on/reset):
 *   horizon_us <t>
 *   uart <t> <byte>                the byte's START bit begins at t (simavr adds the frame time before RXC)
 *   pin <t> <pin> <0|1>            an external level on an INPUT pin (a pin nothing drives is not modelled: say so
 *                                  upstream, do not invent a level)
 *   # comment
 *
 * WHAT IT DOES NOT MODEL, and says so in its END line: electrical levels (drive strength, floating inputs, analogue
 * values beyond what is injected), brown-out, fuses/CLKPR, clock error (it runs at exactly 16 MHz), the USB bridge.
 * The bootloader runs only when the image contains it (an arduino-cli with_bootloader hex: every chunk is loaded) and
 * --reset-to-boot is given; --reset-cause por|ext|bor|wdt sets MCUSR, which Optiboot reads (a port open is ext), and
 * --symbols <app.elf> names the application's heap symbols for an image that carries none.
 *
 * LICENSING: this file links libsimavr (GPL-3), so it is GPL-3. It is a separate program; nothing in Bombyx links it,
 * and Bombyx talks to it only through stdin/stdout (Jan, 2026-09-15: simavr as a separate process).
 *
 * Build (WSL/Linux): gcc -O2 -Wall -o chip_twin chip_twin.c -lsimavr -lelf
 * SPDX-License-Identifier: GPL-3.0-only
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <inttypes.h>

#include <simavr/sim_avr.h>
#include <simavr/sim_elf.h>
#include <simavr/sim_hex.h>
#include <simavr/sim_io.h>
#include <simavr/avr_ioport.h>
#include <simavr/avr_uart.h>
#include <simavr/avr_timer.h>

#define MCU   "atmega328p"
#define FREQ  16000000UL
#define CYCLES_PER_US 16ULL

/* ---- the Uno's pin names, by port and bit ---------------------------------------------------------------------- */
static const char *PIN_NAME[3][8] = {
    /* B */ { "D8", "D9", "D10", "D11", "D12", "D13", NULL, NULL },
    /* C */ { "A0", "A1", "A2", "A3", "A4", "A5", NULL, NULL },      /* PC6 is RESET, not a pin here */
    /* D */ { "D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7" },
};
static const char PORTS[3] = { 'B', 'C', 'D' };

static uint8_t g_port[3], g_ddr[3];
static char g_state[3][8];          /* last reported effective state per pin */

/* the ATmega328P's six output-compare pins (datasheet, "Alternate Functions of Port B/D"): which timer and compare
 * drives which pin, and where its COMnx bits sit in TCCRnA (data-space addresses) */
typedef struct { char timer; int comp; int port; int bit; uint16_t tccra; int com_shift; } oc_t;
static const oc_t OC_MAP[] = {
    { '0', AVR_TIMER_COMPA, 2, 6, 0x44, 6 },   /* OC0A = PD6 = D6 */
    { '0', AVR_TIMER_COMPB, 2, 5, 0x44, 4 },   /* OC0B = PD5 = D5 */
    { '1', AVR_TIMER_COMPA, 0, 1, 0x80, 6 },   /* OC1A = PB1 = D9 */
    { '1', AVR_TIMER_COMPB, 0, 2, 0x80, 4 },   /* OC1B = PB2 = D10 */
    { '2', AVR_TIMER_COMPA, 0, 3, 0xB0, 6 },   /* OC2A = PB3 = D11 */
    { '2', AVR_TIMER_COMPB, 2, 3, 0xB0, 4 },   /* OC2B = PD3 = D3 */
};
#define N_OC (sizeof OC_MAP / sizeof OC_MAP[0])
static uint8_t g_oc_on[N_OC], g_oc_val[N_OC], g_oc_seen[N_OC];

/* 'W': the pin is connected to a timer waveform that simavr has not (yet) produced a level for. Measured 2026-09-15:
 * simavr 1.6 raises Timer0's fast-PWM compare output exactly (976.56 Hz, 65/256 duty for analogWrite(6,64)) but raises
 * nothing at all for the phase-correct PWM Arduino sets on Timers 1 and 2 (D9, D10, D11, D3) -- such a pin toggles at
 * 490 Hz on the silicon, and a steady level here would be a lie. So the level is unknown until the first compare. */
static char effective(int p, int bit)
{
    if (g_ddr[p] & (1u << bit)) {
        for (size_t i = 0; i < N_OC; i++) {
            if (OC_MAP[i].port == p && OC_MAP[i].bit == bit && g_oc_on[i]) {
                return g_oc_seen[i] ? (g_oc_val[i] ? '1' : '0') : 'W';
            }
        }
        return (g_port[p] & (1u << bit)) ? '1' : '0';
    }
    return (g_port[p] & (1u << bit)) ? 'P' : 'Z';
}

static avr_t *g_avr;

static void report_pins(int p)
{
    for (int bit = 0; bit < 8; bit++) {
        if (!PIN_NAME[p][bit]) { continue; }
        char s = effective(p, bit);
        if (s != g_state[p][bit]) {
            g_state[p][bit] = s;
            printf("PIN %" PRIu64 " %s %c\n", (uint64_t)g_avr->cycle, PIN_NAME[p][bit], s);
        }
    }
}

/* ---- wrapping simavr's register handlers: the original always runs first, then what this harness records ---------- */
typedef void (*post_write_t)(avr_t *avr, avr_io_addr_t addr, uint8_t old, uint8_t v);
typedef void (*post_read_t)(avr_t *avr, avr_io_addr_t addr, uint8_t v);
static avr_io_write_t g_orig_w[MAX_IOs];
static void *g_orig_wp[MAX_IOs];
static post_write_t g_post_w[MAX_IOs];
static avr_io_read_t g_orig_r[MAX_IOs];
static void *g_orig_rp[MAX_IOs];
static post_read_t g_post_r[MAX_IOs];

static void on_wrapped_write(struct avr_t *avr, avr_io_addr_t addr, uint8_t v, void *param)
{
    (void)param;
    int ix = AVR_DATA_TO_IO(addr);
    uint8_t old = avr->data[addr];
    if (g_orig_w[ix]) { g_orig_w[ix](avr, addr, v, g_orig_wp[ix]); } else { avr->data[addr] = v; }
    g_post_w[ix](avr, addr, old, v);
}

static uint8_t on_wrapped_read(struct avr_t *avr, avr_io_addr_t addr, void *param)
{
    (void)param;
    int ix = AVR_DATA_TO_IO(addr);
    uint8_t v = g_orig_r[ix] ? g_orig_r[ix](avr, addr, g_orig_rp[ix]) : avr->data[addr];
    g_post_r[ix](avr, addr, v);
    return v;
}

static void wrap_write(avr_t *avr, uint16_t addr, post_write_t post)
{
    int ix = AVR_DATA_TO_IO(addr);
    g_orig_w[ix] = avr->io[ix].w.c;
    g_orig_wp[ix] = avr->io[ix].w.param;
    g_post_w[ix] = post;
    avr->io[ix].w.c = on_wrapped_write;
    avr->io[ix].w.param = NULL;
}

static void wrap_read(avr_t *avr, uint16_t addr, post_read_t post)
{
    int ix = AVR_DATA_TO_IO(addr);
    g_orig_r[ix] = avr->io[ix].r.c;
    g_orig_rp[ix] = avr->io[ix].r.param;
    g_post_r[ix] = post;
    avr->io[ix].r.c = on_wrapped_read;
    avr->io[ix].r.param = NULL;
}

/* ---- the ports, from their registers ----------------------------------------------------------------------------
 * Found 2026-09-15 by the fidelity probe: simavr's PIN_ALL IRQ carries the PIN latch (external levels on inputs) and
 * fires only when that changes, so a pull-up switched on an input could go unrecorded or stale. The driven state is
 * PORTx and DDRx; they are read from the registers after every write to PORTx, DDRx or PINx (a PINx write toggles). */
static const uint16_t REG_PIN[3] = { 0x23, 0x26, 0x29 }, REG_DDR[3] = { 0x24, 0x27, 0x2A }, REG_PORT[3] = { 0x25, 0x28, 0x2B };
static uint8_t g_ext_mask[3], g_ext_val[3];            /* the levels the stimuli put on input pins */

static void restore_external(avr_t *avr, int p)
{
    /* simavr 1.6 copies PORTx into the PIN latch on a port write, overwriting a level driven from outside (found by the
     * sketch probe). Put the outside level back on every input bit that has one. */
    for (int bit = 0; bit < 8; bit++) {
        uint8_t m = (uint8_t)(1u << bit);
        if ((g_ext_mask[p] & m) && !(avr->data[REG_DDR[p]] & m) && ((avr->data[REG_PIN[p]] ^ g_ext_val[p]) & m)) {
            avr_raise_irq(avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ(PORTS[p]), bit), (g_ext_val[p] >> bit) & 1);
        }
    }
}

static void port_post(avr_t *avr, avr_io_addr_t addr, uint8_t old, uint8_t v)
{
    (void)old; (void)v;
    for (int p = 0; p < 3; p++) {
        if (addr == REG_PIN[p] || addr == REG_DDR[p] || addr == REG_PORT[p]) {
            g_port[p] = avr->data[REG_PORT[p]];
            g_ddr[p] = avr->data[REG_DDR[p]];
            restore_external(avr, p);
            report_pins(p);
        }
    }
}

/* ---- what the simulation does not model faithfully, reported the first time the firmware relies on it ------------- */
enum { U_WDT_TIMEOUT_CHANGE, U_SLEEP_MODE, U_EEPROM_READ, U_MCUSR_READ, U_ADC_READ, U_COUNT };
static const char *UNMODELLED_NAME[U_COUNT] = { "wdt_timeout_change", "sleep_mode", "eeprom_read", "mcusr_read", "adc_read" };
static uint8_t g_unmodelled_seen[U_COUNT];

static void unmodelled(avr_t *avr, int kind)
{
    if (g_unmodelled_seen[kind]) { return; }
    g_unmodelled_seen[kind] = 1;
    printf("UNMODELLED %" PRIu64 " %s\n", (uint64_t)avr->cycle, UNMODELLED_NAME[kind]);
}

/* simavr 1.6 ignores a timeout change on a running watchdog until the old timeout fires (sketch probe: 256 ms, not 125) */
#define WDTCSR_ADDR 0x60
static uint8_t g_wdt_on, g_wdt_prescaler;               /* as the last write outside the WDCE sequence set them */
static void wdt_post(avr_t *avr, avr_io_addr_t addr, uint8_t old, uint8_t v)
{
    (void)addr; (void)old;
    const uint8_t WDIE = 0x40, WDCE = 0x10, WDE = 0x08, WDP = 0x27;
    printf("WDT %" PRIu64 " %02x\n", (uint64_t)avr->cycle, v);
    if (v & WDCE) { return; }                              /* the timed sequence's first write carries no prescaler */
    uint8_t on = (v & (WDE | WDIE)) != 0;
    if (g_wdt_on && on && (v & WDP) != g_wdt_prescaler) { unmodelled(avr, U_WDT_TIMEOUT_CHANGE); }
    g_wdt_on = on;
    g_wdt_prescaler = (uint8_t)(v & WDP);
}

/* EEPROM contents on the Uno are UNKNOWN (not readable through Optiboot): a read makes behaviour depend on them */
#define EECR_ADDR 0x3F
static void eecr_post(avr_t *avr, avr_io_addr_t addr, uint8_t old, uint8_t v)
{
    (void)addr; (void)old;
    if (v & 0x01) { unmodelled(avr, U_EEPROM_READ); }
}

/* Optiboot clears MCUSR before the application starts; a prediction without the bootloader runs without it, so the
 * reset cause an application reads differs. The bootloader's own read (pc in the boot section) is modelled. */
#define MCUSR_ADDR 0x54
#define BOOT_START 0x7e00
static void mcusr_read_post(avr_t *avr, avr_io_addr_t addr, uint8_t v)
{
    (void)addr; (void)v;
    if (avr->pc < BOOT_START) { unmodelled(avr, U_MCUSR_READ); }
}

/* the flash as loaded, to say at the end whether anything wrote it (Optiboot 4.4 erases page 0 after four stray bytes) */
static uint8_t *g_flash0;
static size_t g_flash_len;
static int g_in_boot;

/* analogRead has no stimulus path: the conversion runs, its value is not the bench's */
#define ADCL_ADDR 0x78
static void adc_read_post(avr_t *avr, avr_io_addr_t addr, uint8_t v) { (void)addr; (void)v; unmodelled(avr, U_ADC_READ); }

/* ---- the UART's byte time, as the silicon clocks it ---------------------------------------------------------------
 * Found 2026-09-15 by the fidelity and timing probes: simavr 1.6 takes 187 us per byte for the Uno's 57600 8N1, the
 * silicon 175.0 us -- UBRR0 34 with U2X0 (as Arduino's HardwareSerial chooses) is 280 cycles a bit, and 8N1 is ten
 * bits. So after every write that sets the frame, the byte time is recomputed from the registers. */
#define UCSR0A_ADDR 0xC0
#define UCSR0B_ADDR 0xC1
#define UCSR0C_ADDR 0xC2
#define UBRR0L_ADDR 0xC4
#define UBRR0H_ADDR 0xC5
static avr_uart_t *g_uart;
static void uart_post(avr_t *avr, avr_io_addr_t addr, uint8_t old, uint8_t v)
{
    (void)addr; (void)old; (void)v;
    if (!g_uart) { return; }
    uint8_t a = avr->data[UCSR0A_ADDR], b = avr->data[UCSR0B_ADDR], c = avr->data[UCSR0C_ADDR];
    uint32_t ubrr = ((uint32_t)(avr->data[UBRR0H_ADDR] & 0x0F) << 8) | avr->data[UBRR0L_ADDR];
    uint64_t bit_cycles = ((a & 0x02) ? 8u : 16u) * (ubrr + 1u);
    unsigned ucsz = ((c >> 1) & 3u) | ((b & 0x04) ? 4u : 0u);
    unsigned data_bits = (ucsz == 7) ? 9 : 5 + (ucsz & 3u);
    unsigned word_bits = 1 + data_bits + (((c >> 4) & 3u) ? 1 : 0) + ((c & 0x08) ? 2 : 1);
    uint64_t cpb = bit_cycles * word_bits;
    if (cpb != g_uart->cycles_per_byte) {
        printf("UART %" PRIu64 " bit_cycles=%" PRIu64 " word_bits=%u simavr_cycles_per_byte=%" PRIu64 "\n",
               (uint64_t)avr->cycle, bit_cycles, word_bits, (uint64_t)g_uart->cycles_per_byte);
        g_uart->cycles_per_byte = cpb;
    }
}

/* ---- a reset while running (the watchdog): every register cleared, every pin an input again ----------------------
 * Found 2026-09-15 by the sketch probe: the pins kept their pre-reset states through a watchdog reset. */
static void (*g_core_reset)(avr_t *);
static int g_reset_pending;
static void on_core_reset(avr_t *avr)
{
    if (g_core_reset) { g_core_reset(avr); }
    g_reset_pending = 1;
}

/* the connection of each output compare to its pin follows TCCRnA's COMnx bits, re-read after every TCCRnA write */
static void refresh_oc(avr_t *avr)
{
    for (size_t i = 0; i < N_OC; i++) {
        uint8_t on = ((avr->data[OC_MAP[i].tccra] >> OC_MAP[i].com_shift) & 3) != 0;
        if (on != g_oc_on[i]) {
            g_oc_on[i] = on;
            g_oc_seen[i] = 0;
            printf("OCM %" PRIu64 " %s %u\n", (uint64_t)avr->cycle, PIN_NAME[OC_MAP[i].port][OC_MAP[i].bit], on);
            report_pins(OC_MAP[i].port);
        }
    }
}

static void on_oc(struct avr_irq_t *irq, uint32_t value, void *param)
{
    (void)irq;
    size_t i = (size_t)(intptr_t)param;
    g_oc_val[i] = (uint8_t)(value & 1);                    /* simavr may flag AVR_IOPORT_OUTPUT in the upper bits */
    g_oc_seen[i] = 1;
    report_pins(OC_MAP[i].port);
}

/* after TCCR0A/TCCR1A/TCCR2A writes */
static const uint16_t TCCRA[3] = { 0x44, 0x80, 0xB0 };
static void tccra_post(avr_t *avr, avr_io_addr_t addr, uint8_t old, uint8_t v)
{
    (void)addr; (void)old; (void)v;
    refresh_oc(avr);
}

static uint64_t g_tx_bytes;
static void on_tx(struct avr_irq_t *irq, uint32_t value, void *param)
{
    (void)irq; (void)param;
    g_tx_bytes++;
    printf("TX %" PRIu64 " %02x\n", (uint64_t)g_avr->cycle, value & 0xff);
}

/* every firmware read of UDR0, passed through to simavr's own UART handler */
#define UDR0_ADDR 0xC6
static avr_io_read_t g_udr_read;
static void *g_udr_param;
static uint64_t g_rx_reads;
static uint8_t on_udr_read(struct avr_t *avr, avr_io_addr_t addr, void *param)
{
    (void)param;
    uint8_t v = g_udr_read ? g_udr_read(avr, addr, g_udr_param) : avr->data[addr];
    g_rx_reads++;
    printf("RXR %" PRIu64 " %02x\n", (uint64_t)avr->cycle, v);
    return v;
}

/* ---- SRAM: how close the heap came to the stack ------------------------------------------------------------------
 * Found 2026-09-15 by the verification probes: sixteen ACCELSTEPPER_CONFIGs on one device leak sixteen 70-byte
 * AccelSteppers, avr-libc's malloc then returns NULL, and ConfigurableFirmata constructs on this==0 -- 68 bytes written
 * across the register file, pins changing that nothing commanded, and the core still running. The pins alone cannot
 * say that happened. So the harness watches the stack pointer on every instruction and, from the ELF's own symbols,
 * the top of the heap (__brkval, or __heap_start before the first allocation) and malloc's margin. */
static uint16_t g_brkval_addr, g_heap_start, g_margin_addr;
static int g_have_heap;
static uint16_t g_sp_min = 0xffff;
static int32_t g_min_headroom = INT32_MAX;
static uint64_t g_min_headroom_cycle;
static uint16_t g_heap_top_max;

static uint16_t g_prev_sp, g_prev_brk;

static void watch_memory(avr_t *avr)
{
    /* both are 16-bit values the firmware writes one byte per instruction, so for exactly one instruction the pair
     * reads half old, half new (measured: a heap top of 2275 above a 2208 stack that never happened). A value is
     * taken only when it read the same on two consecutive instructions. */
    uint16_t sp = (uint16_t)(avr->data[R_SPL] | (avr->data[R_SPH] << 8));
    int sp_stable = (sp == g_prev_sp);
    g_prev_sp = sp;
    if (sp < 0x100 || !sp_stable) { return; }              /* not yet set by the startup code, or mid-write */
    if (sp < g_sp_min) { g_sp_min = sp; }
    if (!g_have_heap) { return; }
    uint16_t brk = (uint16_t)(avr->data[g_brkval_addr] | (avr->data[g_brkval_addr + 1] << 8));
    int brk_stable = (brk == g_prev_brk);
    g_prev_brk = brk;
    if (!brk_stable) { return; }
    uint16_t top = brk ? brk : g_heap_start;
    if (top > g_heap_top_max) { g_heap_top_max = top; }
    int32_t headroom = (int32_t)sp - (int32_t)top;
    if (headroom < g_min_headroom) { g_min_headroom = headroom; g_min_headroom_cycle = avr->cycle; }
}

/* simavr would otherwise usleep() while the firmware sleeps; a prediction runs as fast as the host can */
static void no_sleep(avr_t *avr, avr_cycle_count_t how_long) { (void)avr; (void)how_long; }

/* ---- the input events -------------------------------------------------------------------------------------------- */
typedef struct { uint64_t cycle; int kind; int port; int bit; uint32_t value; } ev_t;   /* kind 0 uart, 1 pin */
static ev_t *g_ev;
static size_t g_nev, g_cap;

static int pin_lookup(const char *name, int *port, int *bit)
{
    for (int p = 0; p < 3; p++) {
        for (int b = 0; b < 8; b++) {
            if (PIN_NAME[p][b] && !strcmp(PIN_NAME[p][b], name)) { *port = p; *bit = b; return 0; }
        }
    }
    return -1;
}

static int by_cycle(const void *a, const void *b)
{
    const ev_t *x = a, *y = b;
    return (x->cycle > y->cycle) - (x->cycle < y->cycle);
}

int main(int argc, char **argv)
{
    const char *fw = NULL, *symbols = NULL;
    int reset_to_boot = 0, reset_cause = 0x01;              /* MCUSR: PORF 0x01, EXTRF 0x02, BORF 0x04, WDRF 0x08 */
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--firmware") && i + 1 < argc) { fw = argv[++i]; }
        else if (!strcmp(argv[i], "--symbols") && i + 1 < argc) { symbols = argv[++i]; }
        else if (!strcmp(argv[i], "--reset-to-boot")) { reset_to_boot = 1; }
        else if (!strcmp(argv[i], "--reset-cause") && i + 1 < argc) {
            const char *c = argv[++i];
            reset_cause = !strcmp(c, "por") ? 0x01 : !strcmp(c, "ext") ? 0x02 : !strcmp(c, "bor") ? 0x04 : !strcmp(c, "wdt") ? 0x08 : -1;
            if (reset_cause < 0) { fprintf(stderr, "chip_twin: a reset cause is por, ext, bor or wdt, not %s\n", c); return 2; }
        }
        else { fprintf(stderr, "chip_twin: unknown argument %s\n", argv[i]); return 2; }
    }
    if (!fw) { fprintf(stderr, "chip_twin: --firmware <image.elf|image.hex> is required\n"); return 2; }

    uint64_t horizon = 0;
    char line[512];
    int lineno = 0;
    while (fgets(line, sizeof line, stdin)) {
        lineno++;
        char *s = line;
        while (*s == ' ' || *s == '\t') { s++; }
        if (*s == '#' || *s == '\n' || *s == '\r' || *s == 0) { continue; }
        char kw[32], a[64], b[64];
        uint64_t t;
        int n = sscanf(s, "%31s %" SCNu64 " %63s %63s", kw, &t, a, b);
        if (n >= 2 && !strcmp(kw, "horizon_us")) { horizon = t * CYCLES_PER_US; continue; }
        if (g_nev == g_cap) {
            g_cap = g_cap ? g_cap * 2 : 1024;
            g_ev = realloc(g_ev, g_cap * sizeof *g_ev);
            if (!g_ev) { fprintf(stderr, "chip_twin: out of memory\n"); return 1; }
        }
        ev_t *e = &g_ev[g_nev];
        e->cycle = t * CYCLES_PER_US;
        if (n == 3 && !strcmp(kw, "uart")) {
            char *end = NULL;
            long v = strtol(a, &end, 16);
            if (end == a || *end || v < 0 || v > 0xff) { fprintf(stderr, "chip_twin: line %d: not a hex byte: %s\n", lineno, a); return 2; }
            e->kind = 0; e->value = (uint32_t)v;
        } else if (n == 4 && !strcmp(kw, "pin")) {
            if (pin_lookup(a, &e->port, &e->bit) != 0) { fprintf(stderr, "chip_twin: line %d: no such pin %s\n", lineno, a); return 2; }
            if (strcmp(b, "0") && strcmp(b, "1")) { fprintf(stderr, "chip_twin: line %d: a pin level is 0 or 1, not %s\n", lineno, b); return 2; }
            e->kind = 1; e->value = (uint32_t)(b[0] - '0');
        } else {
            fprintf(stderr, "chip_twin: line %d: cannot read: %s", lineno, s);
            return 2;
        }
        g_nev++;
    }
    if (!horizon) { fprintf(stderr, "chip_twin: horizon_us is required (a prediction ends somewhere, and says where)\n"); return 2; }
    qsort(g_ev, g_nev, sizeof *g_ev, by_cycle);

    avr_t *avr = avr_make_mcu_by_name(MCU);
    if (!avr) { fprintf(stderr, "chip_twin: cannot make %s\n", MCU); return 1; }
    g_avr = avr;
    avr_init(avr);
    avr->frequency = FREQ;
    avr->sleep = no_sleep;

    size_t fwlen = strlen(fw);
    const char *elf_for_symbols = symbols;
    if (fwlen > 4 && !strcmp(fw + fwlen - 4, ".hex")) {
        /* every chunk: an arduino-cli with_bootloader hex is three (the application at 0, Optiboot at 0x7E00, its
         * version at 0x7FFE), and simavr's read_ihex_file loads only the first -- found 2026-09-15 by the bootloader
         * probe: the core ran into erased flash at cycle 256 */
        ihex_chunk_p chunks = NULL;
        int nchunks = read_ihex_chunks(fw, &chunks);
        if (nchunks <= 0) { fprintf(stderr, "chip_twin: cannot read %s\n", fw); return 1; }
        avr->codeend = 0;
        for (int k = 0; k < nchunks; k++) {
            if ((uint64_t)chunks[k].baseaddr + chunks[k].size > (uint64_t)avr->flashend + 1) {
                fprintf(stderr, "chip_twin: %s: a chunk at 0x%x (%u B) does not fit the flash\n", fw, chunks[k].baseaddr, chunks[k].size);
                return 1;
            }
            memcpy(avr->flash + chunks[k].baseaddr, chunks[k].data, chunks[k].size);
            if (chunks[k].baseaddr + chunks[k].size > avr->codeend) { avr->codeend = chunks[k].baseaddr + chunks[k].size; }
        }
        free_ihex_chunks(chunks);
    } else {
        elf_for_symbols = symbols ? symbols : fw;
    }
    if (elf_for_symbols) {
        elf_firmware_t f;
        memset(&f, 0, sizeof f);
        if (elf_read_firmware(elf_for_symbols, &f) != 0) { fprintf(stderr, "chip_twin: cannot read firmware %s\n", elf_for_symbols); return 1; }
        if (elf_for_symbols == fw) { avr_load_firmware(avr, &f); }
        int got = 0;
        for (uint32_t i = 0; i < f.symbolcount; i++) {
            const char *name = f.symbol[i]->symbol;
            uint32_t a = f.symbol[i]->addr;
            if (a < 0x800000 || a > 0x80ffff) { continue; }  /* data-space symbols carry avr-gcc's 0x800000 offset */
            if (!strcmp(name, "__brkval"))        { g_brkval_addr = (uint16_t)(a - 0x800000); got |= 1; }
            if (!strcmp(name, "__heap_start"))    { g_heap_start = (uint16_t)(a - 0x800000); got |= 2; }
            if (!strcmp(name, "__malloc_margin")) { g_margin_addr = (uint16_t)(a - 0x800000); got |= 4; }
        }
        g_have_heap = (got == 7);
    }
    if (reset_to_boot) {                                   /* BOOTRST programmed: the reset vector is the boot section */
        if (avr->flash[0x7e00] == 0xff && avr->flash[0x7e01] == 0xff) {
            fprintf(stderr, "chip_twin: --reset-to-boot, but the boot section of %s is erased (no bootloader in the image)\n", fw);
            return 1;
        }
        avr->reset_pc = 0x7e00;
        avr_reset(avr);
    }
    /* simavr 1.6 leaves MCUSR at 0 after a reset; Optiboot decides by it whether to wait for a programmer (only after
     * an external reset -- the DTR pulse of a port open). Found 2026-09-15 by the bootloader probe. */
    avr->data[MCUSR_ADDR] = (uint8_t)reset_cause;
    g_flash_len = (size_t)avr->flashend + 1;
    g_flash0 = malloc(g_flash_len);
    if (!g_flash0) { fprintf(stderr, "chip_twin: out of memory\n"); return 1; }
    memcpy(g_flash0, avr->flash, g_flash_len);
    g_in_boot = reset_to_boot;

    for (int p = 0; p < 3; p++) {
        wrap_write(avr, REG_PIN[p], port_post);
        wrap_write(avr, REG_DDR[p], port_post);
        wrap_write(avr, REG_PORT[p], port_post);
        g_port[p] = avr->data[REG_PORT[p]];
        g_ddr[p] = avr->data[REG_DDR[p]];
        for (int bit = 0; bit < 8; bit++) { g_state[p][bit] = 0; }
        report_pins(p);                                    /* the reset state: every pin an input without pull-up */
    }
    avr_irq_register_notify(avr_io_getirq(avr, AVR_IOCTL_UART_GETIRQ('0'), UART_IRQ_OUTPUT), on_tx, NULL);
    avr_irq_t *uart_in = avr_io_getirq(avr, AVR_IOCTL_UART_GETIRQ('0'), UART_IRQ_INPUT);

    avr_uart_t *uart = NULL;                               /* for simavr's receive-queue depth and its byte time */
    for (avr_io_t *io = avr->io_port; io; io = io->next) {
        if (io->kind && !strcmp(io->kind, "uart") && ((avr_uart_t *)io)->name == '0') { uart = (avr_uart_t *)io; }
    }
    if (!uart) { fprintf(stderr, "chip_twin: simavr has no USART0\n"); return 1; }
    g_uart = uart;
    {
        /* simavr echoes the UART to its console and sleeps on polled reads by default: neither is the chip's, and the
         * console echo on stderr would read as a simulator warning */
        uint32_t flags = 0;
        avr_ioctl(avr, AVR_IOCTL_UART_GET_FLAGS('0'), &flags);
        flags &= ~(uint32_t)(AVR_UART_FLAG_STDIO | AVR_UART_FLAG_POLL_SLEEP);
        avr_ioctl(avr, AVR_IOCTL_UART_SET_FLAGS('0'), &flags);
    }
    wrap_write(avr, UCSR0A_ADDR, uart_post);
    wrap_write(avr, UCSR0B_ADDR, uart_post);
    wrap_write(avr, UCSR0C_ADDR, uart_post);
    wrap_write(avr, UBRR0L_ADDR, uart_post);
    wrap_write(avr, UBRR0H_ADDR, uart_post);
    wrap_write(avr, WDTCSR_ADDR, wdt_post);
    wrap_write(avr, EECR_ADDR, eecr_post);
    wrap_read(avr, MCUSR_ADDR, mcusr_read_post);
    wrap_read(avr, ADCL_ADDR, adc_read_post);
    g_core_reset = avr->reset;
    avr->reset = on_core_reset;
    {
        int ix = AVR_DATA_TO_IO(UDR0_ADDR);                /* wrap, never replace, the UART's read of UDR0 */
        g_udr_read = avr->io[ix].r.c;
        g_udr_param = avr->io[ix].r.param;
        if (!g_udr_read) { fprintf(stderr, "chip_twin: simavr registered no UDR0 read handler\n"); return 1; }
        avr->io[ix].r.c = on_udr_read;
        avr->io[ix].r.param = NULL;
    }
    for (int k = 0; k < 3; k++) { wrap_write(avr, TCCRA[k], tccra_post); }
    for (size_t i = 0; i < N_OC; i++) {
        avr_irq_t *oc = avr_io_getirq(avr, AVR_IOCTL_TIMER_GETIRQ(OC_MAP[i].timer), TIMER_IRQ_OUT_COMP + OC_MAP[i].comp);
        if (!oc) { fprintf(stderr, "chip_twin: simavr has no compare output for timer %c\n", OC_MAP[i].timer); return 1; }
        avr_irq_register_notify(oc, on_oc, (void *)(intptr_t)i);
    }

    size_t next = 0;
    int state = cpu_Running;
    while (avr->cycle < horizon) {
        while (next < g_nev && g_ev[next].cycle <= avr->cycle) {
            ev_t *e = &g_ev[next++];
            if (e->kind == 0) {
                if (uart) {
                    /* the FIFO's own cursors (simavr defines its accessors only inside avr_uart.c) */
                    unsigned depth = (unsigned)((uart->input.write - uart->input.read) & (uart_fifo_fifo_size - 1));
                    if (depth) { printf("RXQ %" PRIu64 " %u\n", (uint64_t)avr->cycle, depth); }
                }
                avr_raise_irq(uart_in, e->value);
            } else {
                g_ext_mask[e->port] |= (uint8_t)(1u << e->bit);
                g_ext_val[e->port] = (uint8_t)((g_ext_val[e->port] & ~(1u << e->bit)) | (e->value ? (1u << e->bit) : 0));
                /* told to the port as its external level, so simavr's pull-up update does not overwrite it */
                avr_ioport_external_t ext = { .name = PORTS[e->port], .mask = g_ext_mask[e->port], .value = g_ext_val[e->port] };
                avr_ioctl(avr, AVR_IOCTL_IOPORT_SET_EXTERNAL(PORTS[e->port]), &ext);
                avr_raise_irq(avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ(PORTS[e->port]), e->bit), e->value);
                printf("EXT %" PRIu64 " %s %u\n", (uint64_t)avr->cycle, PIN_NAME[e->port][e->bit], e->value);
            }
        }
        state = avr_run(avr);
        {
            int boot = avr->pc >= BOOT_START;
            if (g_in_boot && !boot) { printf("APP %" PRIu64 "\n", (uint64_t)avr->cycle); }  /* the bootloader jumped to the app */
            g_in_boot = boot;
        }
        if (!g_in_boot) { watch_memory(avr); }             /* the heap symbols are the application's */
        if (g_reset_pending) {                             /* the reset has run: registers cleared, pins inputs */
            g_reset_pending = 0;
            g_wdt_on = (avr->data[WDTCSR_ADDR] & 0x48) != 0;   /* the watchdog as the reset left it */
            g_wdt_prescaler = (uint8_t)(avr->data[WDTCSR_ADDR] & 0x27);
            printf("RST %" PRIu64 " mcusr=%02x\n", (uint64_t)avr->cycle, avr->data[MCUSR_ADDR]);
            refresh_oc(avr);
            for (int p = 0; p < 3; p++) {
                g_port[p] = avr->data[REG_PORT[p]];
                g_ddr[p] = avr->data[REG_DDR[p]];
                restore_external(avr, p);
                report_pins(p);
            }
            uart_post(avr, 0, 0, 0);
        }
        if (state == cpu_Sleeping && (avr->data[0x53] & 0x0E)) { unmodelled(avr, U_SLEEP_MODE); }  /* SMCR SM != idle */
        if (state == cpu_Done || state == cpu_Crashed) {
            printf("CPU %" PRIu64 " %s\n", (uint64_t)avr->cycle, state == cpu_Done ? "done" : "crashed");
            break;
        }
    }
    if (g_have_heap) {
        printf("MEM sp_min=%u heap_start=%u heap_top_max=%u min_headroom=%" PRId32 " min_headroom_cycle=%" PRIu64
               " malloc_margin=%u\n", g_sp_min, g_heap_start, g_heap_top_max, g_min_headroom, g_min_headroom_cycle,
               (unsigned)(avr->data[g_margin_addr] | (avr->data[g_margin_addr + 1] << 8)));
    } else {
        printf("MEM sp_min=%u heap=unknown\n", g_sp_min);  /* a .hex carries no symbols: the heap top is not known */
    }
    {
        size_t changed = 0, first = 0;
        for (size_t k = 0; k < g_flash_len; k++) {
            if (avr->flash[k] != g_flash0[k]) { if (!changed) { first = k; } changed++; }
        }
        printf("FLASH changed_bytes=%zu first_changed=%zu\n", changed, first);
    }
    printf("END cycle=%" PRIu64 " horizon_cycle=%" PRIu64 " tx_bytes=%" PRIu64 " rx_events=%zu rx_reads=%" PRIu64
           " state=%d mcu=%s freq=%lu engine=simavr-1.6"
           " not_modelled=electrical_levels,floating_inputs,brownout,fuses_clkpr,clock_error,usb_bridge,uart_rx_wire_timing,"
           "phase_correct_pwm_levels\n",
           (uint64_t)avr->cycle, horizon, g_tx_bytes, g_nev, g_rx_reads, state, MCU, FREQ);
    return 0;
}
