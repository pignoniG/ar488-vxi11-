# AR488 → VXI-11 network gateway

Exposes every instrument on the Pi's AR488 USB-GPIB adapters as a standard
VXI-11 (LXI) network instrument. Any VISA (NI-VISA, Keysight IO Libraries,
PyVISA/pyvisa-py, LabVIEW, MATLAB, python-vxi11, …) can reach them with:

```
TCPIP::<pi-ip>::gpib0,16::INSTR      # bus gpib0, GPIB address 16 (Keithley 2000)
TCPIP::<pi-ip>::gpib1,5::INSTR       # bus gpib1, GPIB address 5
TCPIP::<pi-ip>::inst0::INSTR         # the gateway itself (see "Gateway queries")
```

## Layout

| file | what |
|---|---|
| `ar488_vxi11.py` | the gateway (AR488 serial driver + VXI-11 device handlers) |
| `vxi11_server/` | vendored [python-vxi11-server](https://github.com/coburnw/python-vxi11-server) @52ecfba, patched (see below) |
| `ar488-vxi11.toml` | config → `/etc/ar488-vxi11.toml` (buses and per-bus options) |
| `ar488-vxi11.service` | systemd unit (runs as user `ar488gw`, group `dialout`) |
| `install.sh` | installs to `/opt/ar488-vxi11`, enables rpcbind + the service |
| `test_client.py` | PyVISA client for testing / scanning a bus over the network |

## Install / update

```
./install.sh
journalctl -u ar488-vxi11 -f
```

`install.sh` won't overwrite an existing `/etc/ar488-vxi11.toml`. Edit that file to change settings.

## Adapters and instruments

| bus | adapter | firmware |
|---|---|---|
| gpib0 | Nano clone, CH340 | AR488 0.53.46, patched |
| gpib1 | Nano original, FTDI FT232R `AC008SFE` | AR488 0.53.46, patched |
| gpib2 | Nano clone, CH340 | AR488 0.53.46, patched |

**Each adapter carries its bus name** in its own EEPROM (`++id name`). At
startup the gateway probes every USB serial port and assigns buses by the
stored name, so adapters can go in any USB socket and boot order doesn't
matter. An adapter that reports a different name from the bus it would be
opened as is refused (also when a `port` is pinned in the config).

To name a new adapter (once, with the gateway stopped):

```
sudo systemctl stop ar488-vxi11
python3 -c "import serial,time; s=serial.Serial('/dev/ttyUSB0',115200); time.sleep(2.5); s.write(b'++id name gpib3\n++savecfg\n'); time.sleep(0.5)"
```

then add `[[interface]] name = "gpib3"` to `/etc/ar488-vxi11.toml` and start the
gateway again.

All adapters use SN75160/SN75161 bus drivers (TE = D6, DC = D13). The firmware
source, build script, patches and backups of the original firmware are in
`~/Documents/ar488-firmware` on the Pi (not in this repo).

**Firmware requirements:** the gateway works with 0.51.x, and with 0.53.46 *only
with two patches*. Stock 0.53.46 sends the end-of-reply marker as the text `4`
instead of byte 0x04, so every read would hang until timeout. Its `++fndl` also
leaves the bus in a state where the next write is lost. `LISTENERS?` needs 0.53
or newer.

## Gateway queries (`inst0`)

| query | answer |
|---|---|
| `*IDN?` | `AR488-VXI11,Gateway,<host>,<version>` |
| `INTERFACES?` | `gpib0=<serial port>,gpib1=<serial port>` |
| `VERSIONS?` | AR488 firmware version of each adapter |
| `LISTENERS?` / `LISTENERS? gpib0` | addresses that acknowledge on the bus, e.g. `gpib0=16;gpib1=9,22`. This is a GPIB-level check, so it finds instruments that answer no queries (HP 3456A). All buses are scanned at once (~0.2 s). A bus with nothing powered on gives an empty list (`gpib2=`). An adapter on old firmware reports `UNSUPPORTED`. |

Tested through the gateway (PyVISA/pyvisa-py over the LAN, 2026-11-07, all
four in parallel as well):

| resource | instrument | notes |
|---|---|---|
| `gpib0,16` | Keithley 2000 | SCPI. ~100 ms per `READ?` at 1 PLC. 12 s averaged readings work. |
| `gpib1,9` | HP 3245A | `ID?`, `ERR?`. Sends no EOI by default, see below. |
| `gpib1,22` | HP 3456A | No queries: send e.g. `H F1 R1 T4`, then `T3` and read. ASCII and packed (`P1`) binary readings and serial poll work. |
| `gpib2,8` | Agilent 54624A | SCPI. 1000-byte binary waveform (`:WAV:DATA?`) in about 1 s. |

**HP 3245A:** it doesn't assert EOI unless told to. The gateway handles this
(`no_eoi = [9]` in the config) by ending its replies after 200 ms of silence,
so each query takes about 0.4 s. Send `END ON` once after power-up and its
replies end at normal speed. `no_eoi` can stay set either way.

## How it works

* The serial ports stay open the whole time. Opening one resets the Arduino, so
  per-connection bridges like ser2net would lose state on every connect.
* Adapter setup: `++mode 1`, `++auto 0`, `++eos 3` (data is passed through
  as-is, so the client's own `\n` terminator is used), `++eoi 1`, `++eot_enable 1`,
  `++eot_char 4`. Every read is `++read eoi`, and the 0x04 byte the adapter
  appends on EOI marks the end of the response.
* Data bytes LF/CR/ESC/`+` are ESC-escaped, so binary writes are safe. Writes
  go out in 48-byte lines, with EOI only on the last byte when the client
  sets END.
* **Several clients at once.** Each bus has its own lock, so instruments on
  different adapters are used fully in parallel: a slow measurement on gpib1
  doesn't delay gpib0 (tested: gpib0 `*IDN?` in 0.04 s while gpib1 waits on a
  read). This holds whether the client opens one TCP connection per instrument
  (pyvisa-py, python-vxi11) or puts several instruments on one connection (VISA
  libraries may do this). Instruments on the *same* adapter take turns, one
  GPIB transfer at a time. VXI-11 `lock()`/`unlock()` work per instrument.
* **FIFO per instrument.** When several clients use the same instrument, a
  client that writes gets the instrument's turn and keeps it until it has read
  its whole answer (or its read times out). The others wait in the order they
  arrived, up to their own VISA timeout. A turn also ends after `fifo_hold_s`
  (default 1 s) without activity, for clients that send a command and read
  nothing back, and a client that keeps writing while others wait goes to the
  back of the queue after `fifo_hold_s`. Turns are per instrument, so other
  instruments on the same bus are only delayed by actual bus traffic. Set
  `fifo_hold_s = 0` under `[server]` to turn this off.
  Tested 2026-11-10 without locks: four clients on the Keithley 2000 (60/60
  answers correct; the gateway before this change got 0/5 right), two clients
  each on the scope (1000-byte waveforms + `*IDN?`) and on the 3245A, all at
  the same time.
* If an adapter is unplugged, accesses to that bus fail with an I/O error. The
  first access after it is plugged back in (into any socket) finds it again by
  name and reopens it, which takes about 5 s. Only ports no other bus is using are
  probed, because opening a port resets its Arduino. Other buses keep working
  throughout (tested 2026-11-07 by detaching and re-attaching the USB device).
* After opening an adapter, each setup command is confirmed before the next is
  sent. If a write's confirmation ever comes back wrong, the gateway forgets the
  adapter's cached state and returns an I/O error rather than risk sending to
  the wrong address.
* VXI-11 ops → AR488: write/read, `read_stb` → `++spoll`, `clear` → `++clr`,
  `trigger` → `++trg`, `local` → `++loc`.

### Limitations

* **Several clients on the same instrument** don't need VISA locks for
  plain query/answer use: the FIFO (see "How it works") gives every answer to
  the client that asked. A client that only sends commands (no read) holds the
  instrument for `fifo_hold_s` (1 s) after each one, so while it keeps sending,
  other clients' queries on that instrument take about 1 s each. A client that
  writes a query and waits longer than `fifo_hold_s` before reading can still
  have its answer taken by another client. For multi-step sequences that must
  not be interleaved (configure, then measure), use VISA locks
  (`inst.lock_excl()` / `unlock()`). pyvisa-py never asks the server to wait
  for a lock, so with pyvisa-py `lock_excl()` fails at once while another
  client holds it: retry it in a loop. NI-VISA waits up to its lock timeout.
* Serial poll of the HP 3456A only works with AR488 0.53. Firmware 0.51.15
  never answered `++spoll` for it.
* The adapter has no serial flow control and a 64-byte buffer. The gateway
  therefore waits for each 48-byte chunk to be written (an `++addr` round trip)
  before sending more. This costs a few ms per 48 bytes (upload speed not measured).
* Instruments that send no EOI (`no_eoi`, or detected automatically: the first
  read then takes ~3 s) have their replies ended by 200 ms of silence. A binary
  reply that pauses longer than that mid-transfer would be cut short.
* No SRQ/service-request events (`enable_srq` returns "not supported"). Poll
  the STB instead.
* Reads end on EOI, or on silence for `no_eoi` instruments. The VISA
  termchar-on-read setting is ignored.
* Firmware read timeout is capped at 3 s per wait (0.51.15's limit; 0.53 allows more). The gateway re-issues `++read`
  until the client's VISA timeout expires, so long measurements still work.
* The FTDI adapter's device node belongs to group `plugdev` (udev rules from
  openocd/flashrom), so the service runs with `SupplementaryGroups=plugdev`.
* Binary responses containing byte 0x04 followed by a pause of more than 20 ms
  could be cut short. In practice the adapter streams continuously.
* Needs Python ≤ 3.12 (`xdrlib`). Bookworm's system Python 3.11 is fine.

### Library patches (in `vxi11_server/instrument_server.py`)

1. `DeviceLock` rewritten: two clients using the same instrument no longer get
   spurious "locked by another link" errors, and `WAITLOCK` really waits.
2. `device_remote` crashed (`lock.is_open` doesn't exist).
3. If a client drops its TCP connection without `destroy_link`, its links and
   locks are released.
4. Write chunk limit raised from 1 kB to 16 kB.
5. One TCP connection can carry several links. The original kept only the
   newest, so a client with two instruments on one connection got "invalid
   link identifier" on the first. Calls for different links on one connection
   also run in parallel (replies matched by RPC xid), while calls for the same
   link keep their order. A `create_link` whose lock request fails no longer
   leaks the link.
6. A `device_close()` hook is called on a device when its link is destroyed or
   its client drops off (frees the client's FIFO turn).

## Client examples

```python
import pyvisa
rm = pyvisa.ResourceManager()          # NI/Keysight VISA, or ResourceManager("@py")
dmm = rm.open_resource("TCPIP::192.168.1.187::gpib0,16::INSTR")
print(dmm.query("*IDN?"))
print(dmm.query("READ?"))
```

```
python3 test_client.py 192.168.1.187                 # gateway + gpib0,16 *IDN?
python3 test_client.py 192.168.1.187 --scan gpib1    # find instruments on bus 1
```

Firewall note: VXI-11 uses portmapper TCP/UDP 111 plus two dynamic TCP ports.
No firewall is active on this Pi.
