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
| `ar488-vxi11.toml` | config → `/etc/ar488-vxi11.toml` (bus names ↔ serial ports) |
| `ar488-vxi11.service` | systemd unit (runs as user `ar488gw`, group `dialout`) |
| `install.sh` | installs to `/opt/ar488-vxi11`, enables rpcbind + the service |
| `test_client.py` | PyVISA client for testing / scanning a bus over the network |

## Install / update

```
./install.sh
journalctl -u ar488-vxi11 -f
```

`install.sh` won't overwrite an existing `/etc/ar488-vxi11.toml`. Edit that file to change mappings.

## Adapters and instruments

| bus | adapter | firmware | pinned by |
|---|---|---|---|
| gpib0 | Nano clone, CH340 (no USB serial no.) | AR488 0.53.46, patched (see ../ar488-firmware) | physical USB port `1.4.3`, so keep it in that socket |
| gpib1 | Nano original, FTDI FT232R `AC008SFE` | AR488 0.51.15 | USB serial number, any socket works |

Both adapters use SN75160/SN75161 bus drivers (TE = D6, DC = D13).

**Firmware requirements:** the gateway works with 0.51.15 and with 0.53.46
*only with the EOT patch*. Stock 0.53.46 sends the end-of-reply marker as the
text `4` instead of byte 0x04, so every read would hang until timeout.
`LISTENERS?` needs 0.53 or newer.

## Gateway queries (`inst0`)

| query | answer |
|---|---|
| `*IDN?` | `AR488-VXI11,Gateway,<host>,<version>` |
| `INTERFACES?` | `gpib0=<serial port>,gpib1=<serial port>` |
| `VERSIONS?` | AR488 firmware version of each adapter |
| `LISTENERS?` / `LISTENERS? gpib0` | addresses that acknowledge on the bus, e.g. `gpib0=16;gpib1=9,22`. This is a GPIB-level check, so it finds instruments that answer no queries (HP 3456A). Takes ~0.15 s per bus. An adapter on old firmware reports `UNSUPPORTED`. |

Tested through the gateway (PyVISA/pyvisa-py over the LAN, 2026-11-07):

| resource | instrument | notes |
|---|---|---|
| `gpib0,16` | Keithley 2000 | SCPI. ~100 ms per `READ?` at 1 PLC. 12 s averaged readings work. |
| `gpib1,9` | HP 3245A | `ID?`, `ERR?`. Sends no EOI by default, see below. |
| `gpib1,22` | HP 3456A | No queries: send e.g. `H F1 R1 T4`, then `T3` and read. ASCII and packed (`P1`) binary readings work. **Serial poll does not work** (see limitations). |

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
* A lock per bus serialises access, so several clients and instruments can share
  an adapter. VXI-11 `lock()`/`unlock()` work per instrument.
* If an adapter is unplugged, the next access fails with an I/O error, and the
  port reopens automatically once it is back.
* VXI-11 ops → AR488: write/read, `read_stb` → `++spoll`, `clear` → `++clr`,
  `trigger` → `++trg`, `local` → `++loc`.

### Limitations

* **Several clients on the same instrument must use VISA locks**
  (`inst.lock_excl()` / `unlock()`), as with any real GPIB or LAN instrument.
  Otherwise client A's query reply can be read by client B. Different
  instruments, on the same bus or on different buses, can be used at the same time.
  pyvisa-py never asks the server to wait for a lock, so with pyvisa-py
  `lock_excl()` fails at once while another client holds it: retry it in a loop.
  NI-VISA waits up to its lock timeout.
* **Serial poll (`read_stb`) of the HP 3456A fails** with an I/O error after
  2 s. The AR488 firmware (0.51.15) never answers `++spoll 22`, even on the bare
  serial port, while polls of other instruments on the same bus work. Not yet
  tested with 0.53.46 (gpib1 still runs 0.51.15). Until then, just trigger and
  read the 3456A.
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
3. If a client drops its TCP connection without `destroy_link`, its link and lock
   are released.
4. Write chunk limit raised from 1 kB to 16 kB.

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
