# AR488 firmware for the gpibpi adapters

All three adapters are an Arduino Nano with SN75160/SN75161 GPIB bus drivers
(Jay Diddy B style board). They use the UNO pin layout (the default for the
ATmega328P), with `SN7516X_TE 6` and `SN7516X_DC 13`. SC is unused, because
D12 is EOI.

They run AR488 **0.53.46**
([Twilight-Logic/AR488](https://github.com/Twilight-Logic/AR488) `src/AR488`,
GPL-3.0) with the three changes below. They are committed in the fork
[pignoniG/AR488](https://github.com/pignoniG/AR488):

* branch [`gpibpi`](https://github.com/pignoniG/AR488/tree/gpibpi): the firmware
  exactly as flashed (both fixes, the bus-driver config, and `build-nano-debian.sh`).
  Building it gives the same `.hex`, byte for byte (sha256 below).
* branch [`fix-eot-fndl`](https://github.com/pignoniG/AR488/tree/fix-eot-fndl): only
  the two bug fixes, on top of upstream `master`, for a pull request upstream.

On the Pi, the fork is cloned at `~/Documents/AR488`. The original working
copy, built `.hex` and the backups of the original firmware are in
`~/Documents/ar488-firmware`:

| folder | what |
|---|---|
| `AR488/` | AR488 0.53.46 source with the changes below |
| `build.sh` | build script (below), writes `build/*.hex` |
| `build/` | built firmware |
| `backup/` | original firmware read from each adapter before flashing (flash + EEPROM) |

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

Changes 2 and 3 are upstream bugs worth reporting.

## Adapters

| bus | USB | board | bootloader (upload baud) | original firmware | now |
|---|---|---|---|---|---|
| gpib0 | CH340, port 1.4.3 | Nano clone | old, 57600 | 0.51.15 | 0.53.46 patched |
| gpib1 | FTDI `AC008SFE` | Nano original | Optiboot, 115200 | 0.51.15 (byte-identical build to gpib0's) | 0.53.46 patched |
| gpib2 | CH340 ("USB Serial"), port 1.4.2 | Nano clone | old, 57600 | 0.51.18 | 0.53.46 patched |

Each adapter's EEPROM holds its bus name (`++id name gpib0` … `gpib2`, saved with
`++savecfg`), and the gateway finds the adapters by these names. Reflashing keeps
the EEPROM, because the bootloader only writes flash. An EEPROM restore from
`backup/` would erase the name.

All three were flashed on 2026-11-07 with `AR488_0.53.46_nano_sn7516x_te6_dc13.hex`
(sha256 `9e3a3b4a460c8614cafdd7e1c6744908883d201690a160610d99cc3dbb984a7f`,
24748 bytes of code). The bootloader only sets the upload speed. In operation all
three talk at 115200.

## Build

From the fork: `git clone -b gpibpi https://github.com/pignoniG/AR488.git`,
then `./build-nano-debian.sh`. This script is the same as the one below, with
paths for the repository layout (`src/AR488`).

With Debian's Arduino packages (bookworm: arduino-core-avr 1.8.7, avr-gcc 5.4):

```sh
sudo apt install arduino-builder arduino-core-avr gcc-avr avr-libc avrdude
```

`build.sh`, placed next to the `AR488/` source folder:

```sh
#!/bin/sh
# Usage: ./build.sh [old|new]   (Nano bootloader: old = 57600 baud clones, new = 115200)
set -e
cd "$(dirname "$0")"
CPU=atmega328old; [ "$1" = new ] && CPU=atmega328
OUT=$(mktemp -d)
VER=$(sed -n 's/.*ver\. \([0-9.]*\),.*/\1/p' AR488/AR488_Config.h | head -1)
# DECIMAL_DIG: Debian's core 1.8.7 needs it, avr-gcc 5.4 only defines it for C
arduino-builder -compile -hardware /usr/share/arduino/hardware \
  -tools /usr/share/arduino/hardware/tools -tools /usr/bin \
  -built-in-libraries /usr/share/arduino/hardware/arduino/avr/libraries \
  -fqbn arduino:avr:nano:cpu=$CPU -build-path "$OUT" \
  -prefs=runtime.tools.avr-gcc.path=/usr -prefs=runtime.tools.avrdude.path=/usr \
  -prefs=compiler.cpp.extra_flags=-DDECIMAL_DIG=__DECIMAL_DIG__ \
  AR488/AR488.ino | grep -E "Sketch uses|Global variables"
mkdir -p build
cp "$OUT/AR488.ino.hex" "build/AR488_${VER}_nano_sn7516x_te6_dc13.hex"
rm -rf "$OUT"
echo "-> build/AR488_${VER}_nano_sn7516x_te6_dc13.hex"
```

The same `.hex` works with both bootloaders.

## Flash

The gateway holds the serial ports open, so stop it first:

```sh
sudo systemctl stop ar488-vxi11
avrdude -p m328p -c arduino -P <port> -b <57600|115200> -D \
  -U flash:w:build/AR488_0.53.46_nano_sn7516x_te6_dc13.hex:i
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

A **new adapter** needs its bus name stored once (gateway stopped):

```sh
python3 -c "import serial,time; s=serial.Serial('/dev/ttyUSB0',115200); time.sleep(2.5); s.write(b'++id name gpib3\n++savecfg\n'); time.sleep(0.5)"
```

## Restore an original firmware

Flash the backup file the same way, for example for gpib2:

```sh
sudo systemctl stop ar488-vxi11
avrdude -p m328p -c arduino -P <gpib2 port> -b 57600 -D -U flash:w:backup/gpib2_0.51.18_flash.hex:i
sudo systemctl start ar488-vxi11
```

The 0.51 firmware works with the gateway, but `LISTENERS?` then reports
`UNSUPPORTED` and the HP 3456A serial poll doesn't answer.

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

## Tested with 0.53.46 (2026-11-07, through the gateway)

* `LISTENERS?` (`++fndl all`) → `gpib0=16;gpib1=9,22;gpib2=8`.
* gpib0 Keithley 2000: `*IDN?`, 20× `READ?` in 1.6 s, an 8.4 s averaged reading,
  a 250-byte write, chunked reads, timeouts, clear, serial poll, VXI-11 locks.
* gpib1 HP 3245A: `ID?`, serial poll. HP 3456A: triggered readings, and serial
  poll, which now works (0 idle, 68 = RQS + data ready with `SM004`). It never
  answered on 0.51.15.
* gpib2 Agilent 54624A: `*IDN?`, `:SYST:ERR?`, serial poll, and a 1000-byte
  binary waveform (`:WAV:DATA?`, BYTE format) in about 1 s.
* All four instruments in parallel across the three buses: 40/40.
