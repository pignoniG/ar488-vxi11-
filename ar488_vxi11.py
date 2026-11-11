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
import collections
import contextlib
from concurrent.futures import ThreadPoolExecutor
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
LINE_MAX = 110                 # escaped bytes per line with gpibpi firmware (line buffer 128; 2 lines fit its 256-byte input buffer)
DLE = 0x10                     # gpibpi firmware with ++eot_escape 1: DLE x = data byte x
BAUDS = (500000, 115200)       # tried in this order when no baud is configured
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


def open_ar488(path, bauds=BAUDS, reset_delay=2.5, timeout=0.2):
    """Open path and find the baud rate its AR488 answers at.
    Returns (serial, version), or (None, None) if nothing answers like an AR488.
    Only the first open resets the Arduino; changing the baud rate afterwards doesn't."""
    ser = serial.Serial(path, bauds[0], timeout=timeout, write_timeout=5)
    try:
        time.sleep(reset_delay)               # Arduino resets on open (DTR)
        for b in bauds:
            if ser.baudrate != b:
                ser.baudrate = b
            # Bytes from a wrong-baud attempt can leave the adapter's line buffer
            # mid-line (or after an escape character), so the first command after a
            # switch may be lost: end the line with empty lines and ask twice.
            for attempt in range(2):
                ser.write(b"\n\n")
                time.sleep(0.1)
                ser.reset_input_buffer()
                ver = _line_query(ser, "++ver", 0.7)
                if "AR488" in ver:
                    time.sleep(0.05)
                    ser.reset_input_buffer()
                    return ser, ver.splitlines()[-1].strip()
        ser.close()
        return None, None
    except BaseException:
        ser.close()
        raise


def probe_port(path, reset_delay=2.5):
    """(version, stored name, baud) of the AR488 on path, or None if it isn't one."""
    try:
        ser, ver = open_ar488(path, BAUDS, reset_delay)
        if ser is None:
            return None
        with ser:
            return ver, _line_query(ser, "++id name"), ser.baudrate
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
    _discovered = {name.lower(): (p, baud) for p, (_, name, baud) in results.items() if name}
    _discovered_at = time.monotonic()
    for p, (ver, name, baud) in sorted(results.items()):
        log.info("found AR488 %r on %s at %d baud (%s)", name or "<no name>", p, baud, ver)
    return results


def locate(name):
    """(port, baud) of the adapter whose stored name is `name`."""
    with DISCOVERY_LOCK:
        fresh = time.monotonic() - _discovered_at < DISCOVERY_MAX_AGE_S
        hit = _discovered.get(name) if fresh else None
        if hit and hit[0] in free_ports():
            return hit
        discover()
        hit = _discovered.get(name)
        if not hit:
            raise AdapterError(f"{name}: no adapter with stored name {name!r} found")
        return hit


class ReadTimeout(Exception):
    pass


class AR488Adapter:
    """One AR488 adapter = one GPIB bus. All access is serialised by self.lock."""

    def __init__(self, name, port, baud=None, reset_delay=2.5, ifc_on_open=True,
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
        self.eot_esc = False      # firmware escapes EOT/DLE in replies (gpibpi build)
        self._busy_until = 0.0    # a ++read abandoned on client timeout may still run until then
        self.rxbuf = 64           # firmware serial receive buffer

    # ---- low level -------------------------------------------------------
    def open(self):
        """Open the adapter. With no fixed port, find it by the name stored in
        it (++id name). Either way, refuse an adapter that reports another name."""
        self.close()
        port, found_baud = (self.port, None) if self.port else locate(self.name)
        bauds = (self.baud,) if self.baud else \
            ((found_baud,) + tuple(b for b in BAUDS if b != found_baud) if found_baud else BAUDS)
        real = os.path.realpath(port)
        with PORT_LOCK:
            if real in PORTS_IN_USE:
                raise AdapterError(f"{self.name}: {port} is already used by another bus")
            PORTS_IN_USE.add(real)
        self._real = real
        try:
            log.info("%s: opening %s", self.name, port)
            ser, ver = open_ar488(port, bauds, self.reset_delay, timeout=0.5)
            if ser is None:
                raise AdapterError(f"{self.name}: no AR488 answers on {port} "
                                   f"at {', '.join(map(str, bauds))} baud")
            self.ser = ser
            self._addr = self._eoi = self._tmo = None
            self.version = ver
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
            # gpibpi firmware extras: escaped EOT in replies and a bigger input buffer.
            # Other firmware doesn't know these commands and answers with an error or nothing.
            esc = self._query("++eot_escape", 0.5) in ("0", "1")
            rx = self._query("++rxbuf", 0.5) if esc else ""
            self.rxbuf = int(rx) if rx.isdigit() and int(rx) >= 64 else 64
            setup = ["++mode 1", "++auto 0", "++eos 3", "++eot_enable 1", f"++eot_char {EOT}", "++eoi 1"]
            want = ["1", "0", "3", "1", str(EOT), "1"]
            checks = ["++mode", "++auto", "++eos", "++eot_enable", "++eot_char", "++eoi"]
            if esc:
                setup.append("++eot_escape 1")
                want.append("1")
                checks.append("++eot_escape")
            if self.ifc_on_open:
                setup.append("++ifc")
            for c in setup:
                self._cmd(c)
                if not self._query("++addr", 2.0).isdigit():
                    raise AdapterError(f"{self.name}: no answer during setup ({c})")
            got = [self._query(q) for q in checks]
            if got != want:
                raise AdapterError(f"{self.name}: setup not applied, adapter reports {got}")
            self.eot_esc = esc
            self._eoi = True
        except BaseException:
            self.close()
            raise
        log.info("%s: ready, %s, %d baud%s", self.name, self.version, self.ser.baudrate,
                 f", escaped EOT, {self.rxbuf}-byte input buffer" if self.eot_esc else "")

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
                if time.monotonic() < self._busy_until:
                    # The firmware can't be interrupted inside ++read: anything sent
                    # now would only be handled after its timeout. Wait for that.
                    self._sync()
                    self.ser.reset_input_buffer()
                self._busy_until = 0.0
                return fn(*args)
            except SERIAL_ERRORS as e:
                log.error("%s: serial error: %s", self.name, e)
                self.close()
                raise AdapterError(str(e))

    # ---- operations ------------------------------------------------------
    def write(self, addr, data, end=True):
        return self._run(self._write, addr, data, end)

    @staticmethod
    def _escaped_lines(data, line_max):
        """Split data into ESC-escaped lines of at most line_max bytes (plus LF)."""
        lines, cur = [], bytearray()
        for b in data:
            n = 2 if b in ESCAPE_BYTES else 1
            if len(cur) + n > line_max:
                lines.append(cur)
                cur = bytearray()
            if n == 2:
                cur.append(0x1B)
            cur.append(b)
        lines.append(cur)
        return [bytes(l) for l in lines]

    def _write(self, addr, data, end):
        self.ser.reset_input_buffer()
        self._set_addr(addr)
        # The AR488 has no flow control: while it is clocking one line out on
        # GPIB, what follows waits in its serial input buffer (64 bytes on stock
        # firmware) and anything beyond that is dropped. So never have more
        # unconfirmed bytes outstanding than fit; _sync() confirms.
        if self.rxbuf > 64:
            lines = self._escaped_lines(data, LINE_MAX)
            budget = self.rxbuf - 16           # room for ++eoi / ++addr lines
        else:
            lines = [l for i in range(0, max(len(data), 1), WRITE_CHUNK)
                     for l in self._escaped_lines(data[i:i + WRITE_CHUNK], 2 * WRITE_CHUNK)]
            budget = 0                         # one line, then sync
        outstanding = 0
        for i, line in enumerate(lines):
            last = i == len(lines) - 1
            if self._eoi != (end and last):
                cmd = f"++eoi {1 if end and last else 0}\n".encode()
            else:
                cmd = b""
            if outstanding and outstanding + len(cmd) + len(line) + 1 > budget:
                self._sync()
                outstanding = 0
            self.ser.write(cmd + line + b"\n")
            self._eoi = end and last if cmd else self._eoi
            outstanding += len(cmd) + len(line) + 1
            if budget == 0:
                self._sync()
                outstanding = 0
        if outstanding:
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
        dle = False                   # escaped mode: previous byte was DLE
        while True:
            self._cmd("++read eoi")
            issued = time.monotonic()
            # AR488 gives up silently after read_tmo_ms with no data; if the
            # client allows longer than that, ask again.
            retry_at = time.monotonic() + wait
            while True:
                n = self.ser.in_waiting
                if n and self.eot_esc:
                    # gpibpi firmware: data EOT/DLE bytes arrive as DLE x, so an
                    # unescaped EOT is always the end of the reply
                    for b in self.ser.read(n):
                        if dle:
                            buf.append(b)
                            dle = False
                        elif b == DLE:
                            dle = True
                        elif b == EOT:
                            self.eoi_seen.add(addr)
                            return bytes(buf)
                        else:
                            buf.append(b)
                    retry_at = time.monotonic() + wait
                    continue
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
                    self._busy_until = issued + self._tmo / 1000 + RETRY_MARGIN_S
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
            if not r or r.startswith("Transmit failed"):
                return []          # nothing powered on the bus acknowledges at all
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


# ---- FIFO turns: each answer goes to the client that asked -----------------
FIFO_HOLD_S = 1.0          # [server] fifo_hold_s; 0 turns the FIFO off


class InstrumentTurn:
    """Queues the links (clients) of one instrument so query/answer pairs don't mix.

    A link that writes takes the instrument's turn and keeps it until it has
    read a complete answer (or its read times out). Other links wait in the
    order they arrived. A link that writes without reading back (a plain
    command) loses the turn once it has been idle for FIFO_HOLD_S, and a link
    that keeps writing while others wait goes to the back of the queue after
    FIFO_HOLD_S. Never held while waiting for the bus, so other instruments
    are not delayed.
    """

    def __init__(self, name):
        self.name = name
        self.cond = threading.Condition()
        self.owner = None
        self.waiters = collections.deque()
        self.in_flight = 0
        self.granted_at = 0.0
        self.idle_since = 0.0

    def _expired(self, now):
        return (self.owner is not None and self.in_flight == 0
                and now - self.idle_since >= FIFO_HOLD_S)

    def _release(self):
        self.owner = None
        self.cond.notify_all()

    def begin(self, who, timeout_s, may_yield=False):
        """Wait for this link's turn. False on timeout."""
        deadline = time.monotonic() + timeout_s
        with self.cond:
            now = time.monotonic()
            if self.owner is who:
                if not (may_yield and self.waiters and now - self.granted_at >= FIFO_HOLD_S):
                    self.in_flight += 1
                    return True
                self._release()                     # had it long enough: go to the back
            self.waiters.append(who)
            try:
                while True:
                    now = time.monotonic()
                    if self._expired(now):
                        log.debug("%s: turn of an idle client expired", self.name)
                        self._release()
                    if self.owner is None and self.waiters[0] is who:
                        self.waiters.popleft()
                        self.owner, self.granted_at, self.in_flight = who, now, 1
                        if self.waiters:
                            self.cond.notify_all()
                        return True
                    left = deadline - now
                    if left <= 0:
                        return False
                    wait = left
                    if self.owner is not None and self.in_flight == 0:
                        wait = min(wait, self.idle_since + FIFO_HOLD_S - now + 0.001)
                    self.cond.wait(max(wait, 0.001))
            finally:
                if who in self.waiters:              # timed out: leave the queue
                    self.waiters.remove(who)
                    self.cond.notify_all()

    def end(self, who, done=False):
        """An operation finished; done=True when the link's answer is complete."""
        with self.cond:
            if self.owner is not who:
                return
            self.in_flight -= 1
            self.idle_since = time.monotonic()
            if done and self.in_flight == 0:
                self._release()
            elif self.in_flight == 0:
                self.cond.notify_all()              # waiters recompute the idle deadline

    def drop(self, who):
        with self.cond:
            if who in self.waiters:
                self.waiters.remove(who)
            if self.owner is who:
                self._release()


TURNS = {}
TURNS_LOCK = threading.Lock()


def instrument_turn(name):
    with TURNS_LOCK:
        if name not in TURNS:
            TURNS[name] = InstrumentTurn(name)
        return TURNS[name]


class GpibDevice(Vxi11.InstrumentDevice):
    """VXI-11 link to one primary address on one AR488 bus.
    One instance per link; all links to the same address share one InstrumentTurn."""

    def device_init(self):
        m = NAME_RE.match(self.device_name)
        self.adapter = ADAPTERS[m.group(1).lower()]
        self.addr = int(m.group(2))
        self.pending = b""
        self.turn = instrument_turn(self.device_name)
        self.write_ended = True     # last write carried END (message complete)

    def device_close(self):
        self.turn.drop(self)

    @contextlib.contextmanager
    def _turn(self, io_timeout, may_yield=False):
        """Yields False if the turn didn't come within the client's timeout.
        The body sets self._done = True when this link's exchange is complete."""
        self._done = False
        if FIFO_HOLD_S <= 0:
            yield True
            return
        if not self.turn.begin(self, max(io_timeout, 1) / 1000, may_yield):
            log.info("%s: timed out waiting for other clients' queries", self.device_name)
            yield False
            return
        try:
            yield True
        finally:
            self.turn.end(self, self._done)

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
        end = bool(flags & V.OP_FLAG_END)
        # a link writing again while others wait yields its turn, but never
        # in the middle of a message split over several writes
        with self._turn(io_timeout, may_yield=self.write_ended) as ok:
            if not ok:
                return V.ERR_IO_TIMEOUT
            self.pending = b""
            r = self._guard(self.adapter.write, self.addr, opaque_data, end)
            self.write_ended = end
            if r is not None:
                self._done = True
        return V.ERR_NO_ERROR if r is None else r

    def device_read(self, request_size, term_char, flags, io_timeout):
        if self.pending:            # rest of an answer already fetched for this link
            return self._deliver(request_size)
        with self._turn(io_timeout) as ok:
            if not ok:
                return V.ERR_IO_TIMEOUT, 0, b""
            try:
                self.pending = self.adapter.read(self.addr, max(io_timeout, 1) / 1000)
            except ReadTimeout:
                self._done = True
                return V.ERR_IO_TIMEOUT, 0, b""
            except AdapterError as e:
                self._done = True
                log.error("%s: %s", self.device_name, e)
                return V.ERR_IO_ERROR, 0, b""
            log.debug("%s -> %r", self.device_name, self.pending[:200])
            # the whole answer is now in this link's buffer: the instrument is free
            self._done = True
        return self._deliver(request_size)

    def _deliver(self, request_size):
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
        self.write_ended = True
        with self._turn(io_timeout) as ok:
            if not ok:
                return V.ERR_IO_TIMEOUT
            r = self._guard(self.adapter.simple, self.addr, "++clr")
            self._done = True       # clear ends any exchange in progress
        return V.ERR_NO_ERROR if r is None else r

    def device_trigger(self, flags, io_timeout):
        # a trigger usually produces a reading for this link: take the turn
        with self._turn(io_timeout) as ok:
            if not ok:
                return V.ERR_IO_TIMEOUT
            r = self._guard(self.adapter.simple, self.addr, "++trg")
            if r is not None:
                self._done = True
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

            def scan(n, a):
                try:
                    return f"{n}=" + ",".join(a.find_listeners())
                except AdapterError as e:
                    log.info("%s", e)
                    return f"{n}=UNSUPPORTED" if "not supported" in str(e) else f"{n}=ERROR"

            buses = [(n, a) for n, a in sorted(ADAPTERS.items()) if not want or n == want]
            with ThreadPoolExecutor(max(1, len(buses))) as pool:     # all buses at once
                parts = list(pool.map(lambda na: scan(*na), buses))
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
        self._lock = threading.Lock()   # create_links arrive on parallel threads

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
        with self._lock:
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
        names = {n.lower() for _, n, _ in found.values() if n}
        ifaces = [{"name": n} for n in sorted(names)]
        i = 0
        for p, (_, n, _) in sorted(found.items()):
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
    global FIFO_HOLD_S
    FIFO_HOLD_S = float(server_cfg.get("fifo_hold_s", FIFO_HOLD_S))
    for lib in ("vxi11_server", "vxi11_server.rpc", "vxi11_server.instrument_server",
                "vxi11_server.instrument_device", "vxi11_server.vxi11"):
        logging.getLogger(lib).setLevel(logging.DEBUG if args.verbose else logging.WARNING)

    for i in ifaces:
        a = AR488Adapter(i["name"].lower(), i.get("port"), i.get("baud"),
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
