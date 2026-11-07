#!/usr/bin/env python3
"""AR488 -> VXI-11 gateway.

Holds one or more AR488 USB-GPIB adapters open and exposes every GPIB address
on them as a VXI-11 network instrument, so any VISA library can reach them as

    TCPIP::<pi-address>::gpib0,16::INSTR
    TCPIP::<pi-address>::gpib1,5::INSTR

The serial ports stay open for the life of the service, which matters because
opening the port resets the Arduino inside the adapter.
"""

import argparse
import logging
import os
import re
import signal
import termios
import sys
import threading
import time
import tomllib

import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vxi11_server as Vxi11  # noqa: E402  (vendored, patched)
from vxi11_server import vxi11 as V  # noqa: E402
from vxi11_server import instrument_server as IS  # noqa: E402

log = logging.getLogger("ar488-vxi11")

VERSION = "1.0"
EOT = 0x04                     # byte AR488 appends when it sees EOI (++eot_char)
ESCAPE_BYTES = {0x0A, 0x0D, 0x1B, 0x2B}   # LF CR ESC '+' must be escaped in data
WRITE_CHUNK = 48               # raw bytes per serial line (escaped worst case 96 + LF)
SYNC_TIMEOUT_S = 10            # max time for the adapter to push one chunk onto the bus
AR488_MAX_TMO_MS = 3000        # firmware limit for ++read_tmo_ms
EOT_SETTLE_S = 0.02            # quiet time after EOT before we trust it is the end
RETRY_MARGIN_S = 0.15          # slack after the firmware read timeout before acting
NO_EOI_TMO_MS = 200            # firmware read timeout for instruments that never send EOI


class AdapterError(Exception):
    pass


# termios.error (e.g. EIO on an unplugged port) is not an OSError subclass
SERIAL_ERRORS = (serial.SerialException, OSError, termios.error)


# ---- finding adapters by the name stored in them (++id name) ---------------
# Opening a serial port resets the Arduino in an AR488, so never probe a port
# that an adapter is using: PORTS_IN_USE holds their real device paths.
SERIAL_DIR = "/dev/serial/by-path"
PORT_LOCK = threading.Lock()
PORTS_IN_USE = set()
DISCOVERY_LOCK = threading.Lock()
_discovered = {}            # stored name -> port path, from the last probe
_discovered_at = 0.0
DISCOVERY_MAX_AGE_S = 15


def _line_query(ser, line, timeout=1.0):
    ser.reset_input_buffer()
    ser.write(line.encode("ascii") + b"\n")
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        buf += ser.read(ser.in_waiting or 1)
        if buf.endswith(b"\n"):
            break
    return buf.decode("ascii", "replace").strip()


def probe_port(path, baud=115200, reset_delay=2.5):
    """(version, stored name) of the AR488 on path, or None if it isn't one."""
    try:
        with serial.Serial(path, baud, timeout=0.2, write_timeout=2) as s:
            time.sleep(reset_delay)
            ver = _line_query(s, "++ver", 2.0)
            if "AR488" not in ver:
                return None
            return ver, _line_query(s, "++id name")
    except SERIAL_ERRORS:
        return None


def free_ports():
    if not os.path.isdir(SERIAL_DIR):
        return []
    with PORT_LOCK:
        busy = set(PORTS_IN_USE)
    ports = (os.path.join(SERIAL_DIR, p) for p in sorted(os.listdir(SERIAL_DIR)))
    return [p for p in ports if os.path.realpath(p) not in busy]


def discover(force=False):
    """Probe all free USB serial ports in parallel. Returns {path: (version, name)}
    and refreshes the name -> path cache used by locate()."""
    global _discovered, _discovered_at
    ports = free_ports()
    results = {}

    def worker(p):
        r = probe_port(p)
        if r:
            results[p] = r
    threads = [threading.Thread(target=worker, args=(p,)) for p in ports]
    [t.start() for t in threads]
    [t.join() for t in threads]
    _discovered = {name.lower(): p for p, (_, name) in results.items() if name}
    _discovered_at = time.monotonic()
    for p, (ver, name) in sorted(results.items()):
        log.info("found AR488 %r on %s (%s)", name or "<no name>", p, ver)
    return results


def locate(name):
    """Port of the adapter whose stored name is `name`."""
    with DISCOVERY_LOCK:
        fresh = time.monotonic() - _discovered_at < DISCOVERY_MAX_AGE_S
        p = _discovered.get(name) if fresh else None
        if p and p in free_ports():
            return p
        discover()
        p = _discovered.get(name)
        if not p:
            raise AdapterError(f"{name}: no adapter with stored name {name!r} found")
        return p


class ReadTimeout(Exception):
    pass


class AR488Adapter:
    """One AR488 adapter = one GPIB bus. All access is serialised by self.lock."""

    def __init__(self, name, port, baud=115200, reset_delay=2.5, ifc_on_open=True,
                 no_eoi=(), auto_no_eoi=True, no_eoi_tmo_ms=NO_EOI_TMO_MS):
        self.name = name
        self.port = port
        self.baud = baud
        self.reset_delay = reset_delay
        self.ifc_on_open = ifc_on_open
        # Addresses whose instrument does not assert EOI at the end of a reply
        # (e.g. HP 3245A with END OFF). The AR488 can only notice the end of
        # such a reply by its inter-character timeout, so use a short one.
        self.no_eoi = set(no_eoi)
        self.auto_no_eoi = auto_no_eoi
        self.no_eoi_tmo_ms = no_eoi_tmo_ms
        self.eoi_seen = set()
        self.lock = threading.Lock()
        self.ser = None
        self.version = None
        self.current_port = None  # port actually in use (self.port may be None)
        self._real = None
        self._addr = None
        self._eoi = None
        self._tmo = None

    # ---- low level -------------------------------------------------------
    def open(self):
        """Open the adapter. With no fixed port, find it by the name stored in
        it (++id name). Either way, refuse an adapter that reports another name."""
        self.close()
        port = self.port or locate(self.name)
        real = os.path.realpath(port)
        with PORT_LOCK:
            if real in PORTS_IN_USE:
                raise AdapterError(f"{self.name}: {port} is already used by another bus")
            PORTS_IN_USE.add(real)
        self._real = real
        try:
            log.info("%s: opening %s", self.name, port)
            ser = serial.Serial(port, self.baud, timeout=0.5, write_timeout=5)
            self.ser = ser
            time.sleep(self.reset_delay)          # Arduino resets on open (DTR)
            ser.reset_input_buffer()
            self._addr = self._eoi = self._tmo = None
            self.version = self._query("++ver", 2.0)
            if "AR488" not in self.version:
                log.warning("%s: unexpected ++ver reply %r", self.name, self.version)
            stored = self._query("++id name")
            if stored and stored.lower() != self.name:
                raise AdapterError(f"{self.name}: the adapter on {port} is named {stored!r}, "
                                   f"not {self.name!r} (moved to another USB socket?)")
            if not stored:
                log.warning("%s: adapter on %s has no stored name; to pin it, send "
                            "'++id name %s' and '++savecfg' to it", self.name, port, self.name)
            self.current_port = port
        except BaseException:
            self.close()
            raise
        # controller mode, no auto-read, assert EOI on last byte, send data
        # verbatim (client supplies its own terminator), mark EOI with EOT byte.
        # One command at a time, each confirmed: the AR488 has no flow control
        # and a 64-byte input buffer, so a burst can be partly dropped.
        try:
            setup = ["++mode 1", "++auto 0", "++eos 3", "++eot_enable 1", f"++eot_char {EOT}", "++eoi 1"]
            if self.ifc_on_open:
                setup.append("++ifc")
            for c in setup:
                self._cmd(c)
                if not self._query("++addr", 2.0).isdigit():
                    raise AdapterError(f"{self.name}: no answer during setup ({c})")
            got = [self._query(q) for q in ("++mode", "++auto", "++eos", "++eot_enable", "++eot_char", "++eoi")]
            if got != ["1", "0", "3", "1", str(EOT), "1"]:
                raise AdapterError(f"{self.name}: setup not applied, adapter reports {got}")
            self._eoi = True
        except BaseException:
            self.close()
            raise
        log.info("%s: ready, %s", self.name, self.version)

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.current_port = None
        if self._real:
            with PORT_LOCK:
                PORTS_IN_USE.discard(self._real)
            self._real = None

    def _ensure_open(self):
        if self.ser is None:
            try:
                self.open()
            except SERIAL_ERRORS as e:
                raise AdapterError(f"{self.name}: cannot open {self.port or 'adapter'}: {e}")

    def _cmd(self, line):
        self.ser.write(line.encode("ascii") + b"\n")

    def _query(self, line, timeout=1.0):
        return _line_query(self.ser, line, timeout)

    def _set_addr(self, addr):
        if self._addr != addr:
            self._cmd(f"++addr {addr}")
            self._addr = addr

    def _set_eoi(self, on):
        if self._eoi != on:
            self._cmd(f"++eoi {1 if on else 0}")
            self._eoi = on

    def _set_tmo(self, ms):
        ms = max(1, min(AR488_MAX_TMO_MS, int(ms)))
        if self._tmo != ms:
            self._cmd(f"++read_tmo_ms {ms}")
            self._tmo = ms

    def _run(self, fn, *args):
        """Run fn under the bus lock; on serial failure drop the port so the
        next call reopens it (handles unplug / replug)."""
        with self.lock:
            self._ensure_open()
            try:
                return fn(*args)
            except SERIAL_ERRORS as e:
                log.error("%s: serial error: %s", self.name, e)
                self.close()
                raise AdapterError(str(e))

    # ---- operations ------------------------------------------------------
    def write(self, addr, data, end=True):
        return self._run(self._write, addr, data, end)

    def _write(self, addr, data, end):
        self.ser.reset_input_buffer()
        self._set_addr(addr)
        chunks = [data[i:i + WRITE_CHUNK] for i in range(0, len(data), WRITE_CHUNK)] or [b""]
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            self._set_eoi(end and last)
            esc = bytearray()
            for b in chunk:
                if b in ESCAPE_BYTES:
                    esc.append(0x1B)
                esc.append(b)
            self.ser.write(bytes(esc) + b"\n")
            # The AR488 has no flow control and only a 64-byte UART buffer:
            # anything sent while it is still clocking this line out on GPIB
            # can be dropped. Wait until it has finished before sending more.
            self._sync()
        self._set_eoi(True)

    def _sync(self):
        """Barrier: the AR488 executes commands in order, so once it answers
        '++addr' every byte sent before it has been handled."""
        self._cmd("++addr")
        deadline = time.monotonic() + SYNC_TIMEOUT_S
        buf = b""
        while not buf.endswith(b"\n"):
            if time.monotonic() > deadline:
                raise AdapterError(f"{self.name}: adapter stopped responding (GPIB handshake stuck?)")
            buf += self.ser.read(self.ser.in_waiting or 1)
        if buf.strip() != str(self._addr).encode():
            # Some bytes were lost or garbled: the adapter's state is unknown.
            # Forget the cached settings so they are sent again, and fail this call.
            self._addr = self._eoi = self._tmo = None
            raise AdapterError(f"{self.name}: unexpected sync reply {buf!r}, data may not have arrived")

    def read(self, addr, timeout_s):
        return self._run(self._read, addr, timeout_s)

    def _read(self, addr, timeout_s):
        self.ser.reset_input_buffer()
        self._set_addr(addr)
        no_eoi = addr in self.no_eoi
        self._set_tmo(self.no_eoi_tmo_ms if no_eoi else timeout_s * 1000)
        wait = self._tmo / 1000 + RETRY_MARGIN_S
        deadline = time.monotonic() + timeout_s
        buf = bytearray()
        while True:
            self._cmd("++read eoi")
            # AR488 gives up silently after read_tmo_ms with no data; if the
            # client allows longer than that, ask again.
            retry_at = time.monotonic() + wait
            while True:
                n = self.ser.in_waiting
                if n:
                    buf += self.ser.read(n)
                    retry_at = time.monotonic() + wait
                    if buf and buf[-1] == EOT:
                        time.sleep(EOT_SETTLE_S)
                        if not self.ser.in_waiting:
                            self.eoi_seen.add(addr)
                            return bytes(buf[:-1])
                    continue
                now = time.monotonic()
                if now >= deadline or (now >= retry_at and buf):
                    if buf:   # firmware timed out after data without EOI
                        if not no_eoi:
                            if self.auto_no_eoi and addr not in self.eoi_seen:
                                self.no_eoi.add(addr)
                                log.info("%s: addr %d does not assert EOI; using %d ms "
                                         "end-of-reply timeout from now on",
                                         self.name, addr, self.no_eoi_tmo_ms)
                            else:
                                log.warning("%s: addr %d read ended without EOI", self.name, addr)
                        return bytes(buf)
                    raise ReadTimeout()
                if now >= retry_at:
                    break     # firmware timed out with nothing: re-issue ++read
                time.sleep(0.002)

    def spoll(self, addr):
        def f():
            self._set_addr(addr)
            r = self._query("++spoll", 2.0)
            try:
                return int(r)
            except ValueError:
                raise AdapterError(f"bad spoll reply {r!r}")
        return self._run(f)

    def find_listeners(self):
        """Addresses that acknowledge as listeners on the bus (GPIB-level check,
        works for instruments that answer no queries). Needs AR488 >= 0.53."""
        def f():
            r = self._query("++fndl all", 5.0)
            if not r:
                return []
            if not re.fullmatch(r"[0-9:,]+", r):
                raise AdapterError(f"++fndl not supported by {self.version or 'firmware'} ({r!r})")
            return r.split(",")
        return self._run(f)

    def simple(self, addr, cmd):
        def f():
            self._set_addr(addr)
            self._cmd(cmd)
            self.ser.flush()
        return self._run(f)


ADAPTERS = {}       # "gpib0" -> AR488Adapter
NAME_RE = re.compile(r"^\s*(gpib\d+)\s*,\s*(\d+)\s*(?:,\s*\d+\s*)?$", re.I)


class GpibDevice(Vxi11.InstrumentDevice):
    """VXI-11 link to one primary address on one AR488 bus."""

    def device_init(self):
        m = NAME_RE.match(self.device_name)
        self.adapter = ADAPTERS[m.group(1).lower()]
        self.addr = int(m.group(2))
        self.pending = b""

    def _guard(self, fn, *a):
        try:
            return fn(*a)
        except ReadTimeout:
            return V.ERR_IO_TIMEOUT
        except AdapterError as e:
            log.error("%s: %s", self.device_name, e)
            return V.ERR_IO_ERROR
        except Exception:
            log.exception("%s: unexpected error", self.device_name)
            return V.ERR_IO_ERROR

    def device_write(self, opaque_data, flags, io_timeout):
        log.debug("%s <- %r", self.device_name, opaque_data[:200])
        self.pending = b""
        end = bool(flags & V.OP_FLAG_END)
        r = self._guard(self.adapter.write, self.addr, opaque_data, end)
        return V.ERR_NO_ERROR if r is None else r

    def device_read(self, request_size, term_char, flags, io_timeout):
        if not self.pending:
            try:
                self.pending = self.adapter.read(self.addr, max(io_timeout, 1) / 1000)
            except ReadTimeout:
                return V.ERR_IO_TIMEOUT, 0, b""
            except AdapterError as e:
                log.error("%s: %s", self.device_name, e)
                return V.ERR_IO_ERROR, 0, b""
            log.debug("%s -> %r", self.device_name, self.pending[:200])
        data, self.pending = self.pending[:request_size], self.pending[request_size:]
        reason = Vxi11.ReadRespReason.REQCNT if self.pending else Vxi11.ReadRespReason.END
        return V.ERR_NO_ERROR, reason, data

    def device_readstb(self, flags, io_timeout):
        try:
            return V.ERR_NO_ERROR, self.adapter.spoll(self.addr)
        except AdapterError as e:
            log.error("%s: %s", self.device_name, e)
            return V.ERR_IO_ERROR, 0

    def device_clear(self, flags, io_timeout):
        self.pending = b""
        r = self._guard(self.adapter.simple, self.addr, "++clr")
        return V.ERR_NO_ERROR if r is None else r

    def device_trigger(self, flags, io_timeout):
        r = self._guard(self.adapter.simple, self.addr, "++trg")
        return V.ERR_NO_ERROR if r is None else r

    def device_local(self, flags, io_timeout):
        r = self._guard(self.adapter.simple, self.addr, "++loc")
        return V.ERR_NO_ERROR if r is None else r

    def device_remote(self, flags, io_timeout):
        return V.ERR_NO_ERROR       # AR488 holds REN asserted; addressing = remote

    def device_enable_srq(self, enable, handle):
        return V.ERR_OPERATION_NOT_SUPPORTED


class GatewayInfoDevice(Vxi11.InstrumentDevice):
    """inst0: identifies the gateway and lists its buses."""

    def device_init(self):
        self.result = b""

    def device_write(self, opaque_data, flags, io_timeout):
        cmd = opaque_data.decode("ascii", "replace").strip().upper()
        if cmd == "*IDN?":
            self.result = f"AR488-VXI11,Gateway,{os.uname().nodename},{VERSION}\n"
        elif cmd in ("*DEVICE_LIST?", "INTERFACES?"):
            self.result = ",".join(f"{n}={a.current_port or 'NOT FOUND'}" for n, a in sorted(ADAPTERS.items())) + "\n"
        elif cmd == "VERSIONS?":
            self.result = ";".join(f"{n}={a.version}" for n, a in sorted(ADAPTERS.items())) + "\n"
        elif cmd.startswith("LISTENERS?"):
            # LISTENERS?            -> gpib0=16;gpib1=9,22
            # LISTENERS? gpib1      -> gpib1=9,22
            want = cmd[len("LISTENERS?"):].strip().lower()
            parts = []
            for n, a in sorted(ADAPTERS.items()):
                if want and n != want:
                    continue
                try:
                    parts.append(f"{n}=" + ",".join(a.find_listeners()))
                except AdapterError as e:
                    log.info("%s", e)
                    parts.append(f"{n}=UNSUPPORTED" if "not supported" in str(e) else f"{n}=ERROR")
            self.result = (";".join(parts) if parts else f"ERROR: no interface {want}") + "\n"
        else:
            self.result = "ERROR: supported: *IDN?, INTERFACES?, VERSIONS?, LISTENERS? [gpibN]\n"
        self.result = self.result.encode()
        return V.ERR_NO_ERROR

    def device_read(self, request_size, term_char, flags, io_timeout):
        data, self.result = self.result[:request_size], self.result[request_size:]
        reason = Vxi11.ReadRespReason.REQCNT if self.result else Vxi11.ReadRespReason.END
        return V.ERR_NO_ERROR, reason, data


class GatewayRegistry(IS.DeviceRegistry):
    """Resolves 'inst0' and any 'gpibN,addr' name on the fly."""

    def __init__(self):
        self._registry = {}

    def register(self, name, device_class):
        self._registry[name] = IS.DeviceItem(device_class)
        self._registry[name].lock = IS.DeviceLock(name)

    def directory(self):
        return [f"{n},<addr>" for n in sorted(ADAPTERS)]

    def factory(self, name):
        key = name.strip().lower()
        if key != "inst0":
            m = NAME_RE.match(key)
            if not m or m.group(1) not in ADAPTERS or not 0 <= int(m.group(2)) <= 30:
                log.info("create_link for unknown device %r", name)
                raise KeyError(name)
            key = f"{m.group(1)},{int(m.group(2))}"
            if key not in self._registry:
                self.register(key, GpibDevice)
        item = self._registry[key]
        dev = item.device_class(key, item.lock)
        dev.device_list = self.directory()
        return dev


def load_config(path):
    cfg = {}
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    ifaces = cfg.get("interface", [])
    if not ifaces:
        # auto-detect every AR488: named ones by their stored name, unnamed
        # ones get the next free gpibN, pinned to their physical USB port
        with DISCOVERY_LOCK:
            found = discover()
        names = {n.lower() for _, n in found.values() if n}
        ifaces = [{"name": n} for n in sorted(names)]
        i = 0
        for p, (_, n) in sorted(found.items()):
            if not n:
                while f"gpib{i}" in names:
                    i += 1
                names.add(f"gpib{i}")
                ifaces.append({"name": f"gpib{i}", "port": p})
        log.info("no [[interface]] in config, auto-detected %d adapter(s)", len(ifaces))
    return cfg.get("server", {}), ifaces


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="/etc/ar488-vxi11.toml")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    server_cfg, ifaces = load_config(args.config)
    if not args.verbose:
        logging.getLogger().setLevel(server_cfg.get("log_level", "INFO").upper())
    for lib in ("vxi11_server", "vxi11_server.rpc", "vxi11_server.instrument_server",
                "vxi11_server.instrument_device", "vxi11_server.vxi11"):
        logging.getLogger(lib).setLevel(logging.DEBUG if args.verbose else logging.WARNING)

    for i in ifaces:
        a = AR488Adapter(i["name"].lower(), i.get("port"), i.get("baud", 115200),
                         i.get("reset_delay", 2.5), i.get("ifc_on_open", True),
                         i.get("no_eoi", []), i.get("auto_no_eoi", True),
                         i.get("no_eoi_tmo_ms", NO_EOI_TMO_MS))
        ADAPTERS[a.name] = a
        try:
            a.open()
        except Exception as e:      # keep going; it will be retried on first use
            log.error("%s: %s (will retry on first access)", a.name, e)
            a.close()
    if not ADAPTERS:
        log.error("no adapters configured or found")
        return 1

    IS.Vxi11Server._device_registry = GatewayRegistry()
    server = Vxi11.InstrumentServer(GatewayInfoDevice)
    server.listen()
    log.info("VXI-11 gateway up: %s", ", ".join(f"{n} -> {a.current_port or 'NOT FOUND'}" for n, a in sorted(ADAPTERS.items())))

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    stop.wait()
    server.close()
    for a in ADAPTERS.values():
        a.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
