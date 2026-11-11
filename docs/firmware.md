# AR488 firmware for the gpibpi adapters

All three adapters are an Arduino Nano with SN75160/SN75161 GPIB bus drivers
(Jay Diddy B style board). They use the UNO pin layout (the default for the
ATmega328P), with `SN7516X_TE 6` and `SN7516X_DC 13`. SC is unused, because
D12 is EOI.

They run AR488 **0.53.46-gpibpi2**: upstream 0.53.46
([Twilight-Logic/AR488](https://github.com/Twilight-Logic/AR488) `src/AR488`,
GPL-3.0) with the six changes below. They are committed in the fork
[pignoniG/AR488](https://github.com/pignoniG/AR488):

* branch [`gpibpi`](https://github.com/pignoniG/AR488/tree/gpibpi), the fork's default:
  the firmware exactly as flashed, with `build-nano-debian.sh`. Building it gives
  the same `.hex`, byte for byte (sha256 below). All firmware changes go here.
* branch [`fix-eot-fndl`](https://github.com/pignoniG/AR488/tree/fix-eot-fndl): only
  the two bug fixes on top of upstream `master`. The upstream repository is
  archived, so the fixes live in this fork.

On the Pi, the fork is cloned at `~/Documents/AR488`. The backups of the
original firmware are in `~/Documents/ar488-firmware/backup` (flash + EEPROM read
from each adapter before the first flash). That folder also holds the first
working copy, from before the fork.

## Changes to the upstream source

### 1. Bus drivers: `AR488_Config.h`

```c
// Mod JDB: SN75160/SN75161 bus drivers (SC not used, pin 12 is EOI)
#define SN7516X
#ifdef SN7516X
/*** Jay Diddy B board ***/
  #define SN7516X_TE 6
  #define SN7516X_DC 13
//  #define SN7516X_SC 12
```

### 2. End-of-reply byte: `AR488_GPIBbus.cpp` line 733

```c
    // If eot_enabled then add EOT character
    if (cfg.eot_en) dataStream.write(cfg.eot_ch);  // patched: print() sent the value as decimal text
```

Upstream uses `dataStream.print(cfg.eot_ch)`, which sends the EOT value as
decimal text: `++eot_char 4` sends the characters `"4"` instead of byte 0x04,
so the end-of-reply marker is useless. The gateway needs the raw byte. Without
this change every read hangs until its timeout.

### 3. `++fndl` loses the next write: `AR488.ino`, end of `fndl_h()`

```c
  dataPort.println();
  gpibBus.cfg.rtmo = tmo;
  // patched: the scan leaves deviceAddressed == TOLISTEN, so the next data write
  // skipped addressing the selected device and its data was lost. Unaddress cleanly.
  gpibBus.unAddressDevice();
  gpibBus.setControls(CIDS);
```

`++fndl` addresses every address as a listener and leaves the "device
addressed" flag set to *listen*. The next data write then skips addressing the
selected device, and the data is lost. The query after a `LISTENERS?` would
time out.

Changes 2 and 3 fix bugs in upstream 0.53.46. Upstream is archived, so they
are kept in the fork.

### 4. Serial speed 500000 baud: `AR488_Config.h`

`AR_SERIAL_SPEED 500000` instead of 115200. On a 16 MHz AVR, 500000 baud is exact,
while 115200 is 2.1% off. It is also about 4× the bandwidth. The CH340 and FT232R
both handle it. The gateway finds the speed by itself (it tries 500000, then
115200), so adapters on stock firmware still work.

### 5. `++eot_escape 0|1`: binary-safe end of reply

With `++eot_escape 1`, every received data byte that equals the EOT character
(0x04) or DLE (0x10) is sent as DLE followed by that byte. An unescaped 0x04 then
can only be the end-of-reply marker, even in binary data. To decode, replace each
DLE x with x. The setting defaults to 0, so plain terminal use is unchanged, and it is
deliberately not part of the EEPROM config, so the saved layout (and the stored
adapter name) stays the same. The gateway switches it on when it opens an
adapter. With stock firmware it still has to guess: a 0x04 followed by 20 ms of
silence counts as the end.

### 6. 256-byte serial input buffer, `++rxbuf`

The adapter has no serial flow control. While it clocks one line out on GPIB,
what follows waits in the Arduino core's serial input buffer, which holds 64
bytes by default. `build-nano-debian.sh` builds with
`-DSERIAL_RX_BUFFER_SIZE=256`, and the new `++rxbuf` command reports the size.
With 256 the gateway sends two 110-byte lines per confirmation round trip
instead of one 48-byte line.

### Measured (through the gateway, 2026-11-11)

| | 0.53.46 patched, 115200 | gpibpi2, 500000 |
|---|---|---|
| Keithley `*IDN?` | 36 ms | 13–20 ms |
| scope 111-byte waveform | 148 ms | 126 ms |
| scope 2011-byte waveform | 330 ms | 252 ms |
| 999-byte write + `*OPC?` | 239 ms | 168 ms |

Most of a waveform's time is the scope preparing it (~0.12 s). Transfer now
costs about 61 µs per byte instead of 96 µs. The serial link is no longer the
limit; the firmware's per-byte GPIB handshake loop or the scope is.

## Adapters

| bus | USB | board | bootloader (upload baud) | original firmware | now |
|---|---|---|---|---|---|
| gpib0 | CH340, port 1.4.3 | Nano clone | old, 57600 | 0.51.15 | 0.53.46-gpibpi2 |
| gpib1 | FTDI `AC008SFE` | Nano original | Optiboot, 115200 | 0.51.15 (byte-identical build to gpib0's) | 0.53.46-gpibpi2 |
| gpib2 | CH340 ("USB Serial"), port 1.4.2 | Nano clone | old, 57600 | 0.51.18 | 0.53.46-gpibpi2 |

Each adapter's EEPROM holds its bus name (`++id name gpib0` … `gpib2`, saved with
`++savecfg`), and the gateway finds the adapters by these names. Reflashing keeps
the EEPROM, because the bootloader only writes flash. An EEPROM restore from
`backup/` would erase the name.

All three were flashed on 2026-11-11 with `AR488_0.53.46-gpibpi2_nano_sn7516x_te6_dc13.hex`
(sha256 `b05011baaaf14b41a522bc79a16f3ee5c90c3da1efe6aa765e247f2c86c9bf9d`,
25176 bytes of code, 662 bytes of RAM free). The bootloader's upload speed
(57600 or 115200) is unrelated to the speed the firmware runs at: all three talk at
500000 baud. Before that, from 2026-11-07, they ran 0.53.46 with changes 1–3 only
(fork commit `e74fde3`, sha256 `9e3a3b4a…`, 115200 baud).

## Build

With Debian's Arduino packages (bookworm: arduino-core-avr 1.8.7, avr-gcc 5.4):

```sh
sudo apt install arduino-builder arduino-core-avr gcc-avr avr-libc avrdude
git clone https://github.com/pignoniG/AR488.git     # default branch: gpibpi
cd AR488 && ./build-nano-debian.sh                   # -> build/AR488_<version>_nano_sn7516x_te6_dc13.hex
```

The script runs `arduino-builder` for `arduino:avr:nano` with two extra compiler
flags:
* `-DDECIMAL_DIG=__DECIMAL_DIG__`: Debian's core 1.8.7 needs it, and avr-gcc 5.4
  only defines it for C.
* `-DSERIAL_RX_BUFFER_SIZE=256`: change 6.

The Arduino IDE builds without the second flag, which gives a 64-byte buffer. That
also works, because the gateway reads `++rxbuf` and falls back to small writes. The
same `.hex` works with both bootloaders.

## Flash

The gateway holds the serial ports open, so stop it first:

```sh
sudo systemctl stop ar488-vxi11
avrdude -p m328p -c arduino -P <port> -b <57600|115200> -D \
  -U flash:w:build/AR488_0.53.46-gpibpi2_nano_sn7516x_te6_dc13.hex:i
sudo systemctl start ar488-vxi11
```

Use `-b 57600` for the clones (old bootloader) and `-b 115200` for the original
Nano (Optiboot). Ports:

```
gpib0  /dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.4.3:1.0-port0   -b 57600
gpib1  /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AC008SFE-if00-port0                       -b 115200
gpib2  /dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.4.2:1.0-port0   -b 57600
```

The by-path names depend on the USB socket. If an adapter has moved, the
gateway log shows where it was found (`found AR488 'gpibN' on ...`).

Back up an adapter before flashing it:

```sh
avrdude -p m328p -c arduino -P <port> -b <baud> \
  -U flash:r:backup/<name>_flash.hex:i -U eeprom:r:backup/<name>_eeprom.hex:i
```

A **new adapter** needs its bus name stored once (gateway stopped). Use 500000
baud with this firmware, or 115200 with stock firmware:

```sh
python3 -c "import serial,time; s=serial.Serial('/dev/ttyUSB0',500000); time.sleep(2.5); s.write(b'++id name gpib3\n++savecfg\n'); time.sleep(0.5)"
```

## Restore an original firmware

Flash the backup file the same way, for example for gpib2:

```sh
sudo systemctl stop ar488-vxi11
avrdude -p m328p -c arduino -P <gpib2 port> -b 57600 -D -U flash:w:backup/gpib2_0.51.18_flash.hex:i
sudo systemctl start ar488-vxi11
```

The 0.51 firmware works with the gateway at 115200, but `LISTENERS?` then
reports `UNSUPPORTED` and the HP 3456A serial poll doesn't answer. To go back
to 0.53.46 with only changes 1–3, build fork commit `e74fde3`.

Backup checksums (sha256):

```
7fd673c1c1917d81f3dbe05da744503b24e44b79c68a478a1030895159c264cb  gpib0_0.51.15_flash.hex
5211d9512adc5ab1e5480498f2ab826c3c26c23351b44c2e6949ae89c67ffa73  gpib0_0.51.15_eeprom.hex
de7975d682e92ef4fb5ed961c33baba6c1d8f5ba78e98450b063c22908ca7642  gpib1_0.51.15_flash.hex
3905f1ca60a33d650556b57c6fe3898d5cf4cf994756b45eea102f5cfe768ff9  gpib1_0.51.15_eeprom.hex
274e035ad3d73ab67fc0cb134c3c710397c1a3b283321df5badabf3f9e3c6b42  gpib2_0.51.18_flash.hex
5211d9512adc5ab1e5480498f2ab826c3c26c23351b44c2e6949ae89c67ffa73  gpib2_0.51.18_eeprom.hex
```

The flash backups include each board's bootloader in the last 2 KB, so gpib0's
and gpib1's files differ even though their application code is identical.

## Tested

With 0.53.46-gpibpi2 (2026-11-11): `tests/hw_tests.py` all pass. This covers
parallel buses, two instruments on one connection, the FIFO with four clients
on the Keithley, two clients each on the scope and the 3245A, locks, `LISTENERS?`,
and binary transfers. The scope's 1841-byte `:SYST:SET?` setup contains 38
bytes of 0x04 and 17 of 0x10: all reads were complete, and writing it back three
times read back identically.

With 0.53.46 and changes 1–3 (2026-11-07, through the gateway):

* `LISTENERS?` (`++fndl all`) → `gpib0=16;gpib1=9,22;gpib2=8`.
* gpib0 Keithley 2000: `*IDN?`, 20× `READ?` in 1.6 s, an 8.4 s averaged reading,
  a 250-byte write, chunked reads, timeouts, clear, serial poll, VXI-11 locks.
* gpib1 HP 3245A: `ID?`, serial poll. HP 3456A: triggered readings, and serial
  poll, which now works (0 idle, 68 = RQS + data ready with `SM004`). It never
  answered on 0.51.15.
* gpib2 Agilent 54624A: `*IDN?`, `:SYST:ERR?`, serial poll, and a 1000-byte
  binary waveform (`:WAV:DATA?`, BYTE format) in about 1 s.
* All four instruments in parallel across the three buses: 40/40.
