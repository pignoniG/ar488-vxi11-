#!/usr/bin/env python3
"""Hardware tests for the gateway, against the instruments on the gpibpi bench.

    python3 tests/hw_tests.py [--host 127.0.0.1] [test ...]

Without test names, runs them all. Tests whose instrument doesn't answer are
reported as SKIP. Uses the VXI-11 client vendored in vxi11_server, so nothing
needs to be installed.

Instruments (change INSTR below for another bench):
  k2000  gpib0,16  Keithley 2000           h3456  gpib1,22  HP 3456A
  h3245  gpib1,9   HP 3245A (no EOI)       scope  gpib2,8   Agilent 54624A
  k2000s asrl0     Keithley 2000 on RS-232 (the same meter: one interface at a time)
"""
import argparse
import os
import re
import socket
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from vxi11_server import vxi11, rpc  # noqa: E402

INSTR = {"k2000": "gpib0,16", "h3456": "gpib1,22", "h3245": "gpib1,9", "scope": "gpib2,8",
         "k2000s": "asrl0"}
HOST = "127.0.0.1"


class Skip(Exception):
    pass


def dev(name, timeout=10):
    d = vxi11.Instrument(HOST, INSTR.get(name, name))
    d.timeout = timeout
    d.open()
    return d


def need(*names):
    """Skip the test unless every named instrument answers."""
    probes = {"k2000": "*IDN?", "scope": "*IDN?", "h3245": "ID?", "k2000s": "*IDN?"}
    for n in names:
        try:
            d = dev(n, 3)
            d.client.sock.settimeout(15)      # a write to an empty address can take a while
        except Exception:
            raise Skip(f"{n} ({INSTR[n]}) not available")
        try:
            if n == "h3456":
                d.read_stb()
            else:
                d.ask(probes[n])
        except Exception:
            raise Skip(f"{n} ({INSTR[n]}) not answering")
        finally:
            try:
                d.close()
            except Exception:
                pass


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def threads(*fns):
    ts = [threading.Thread(target=f) for f in fns]
    [t.start() for t in ts]
    [t.join() for t in ts]


# ---------------------------------------------------------------------------
def t_parallel_buses():
    """A slow read on gpib1 doesn't delay queries on gpib0 (separate connections)."""
    need("k2000", "h3456")
    h, k = dev("h3456", 4), dev("k2000")
    h.write("H F1 R1 T4")
    h.read()                                      # consume the reading: next read waits
    times = []

    def slow():
        try:
            h.read()
        except vxi11.Vxi11Exception:
            pass

    def fast():
        time.sleep(0.3)
        for _ in range(5):
            t = time.time()
            check("KEITHLEY" in k.ask("*IDN?"), "bad *IDN?")
            times.append(time.time() - t)
    threads(slow, fast)
    h.close()
    k.close()
    check(max(times) < 0.5, f"gpib0 queries delayed: {times}")
    return f"gpib0 *IDN? max {max(times)*1000:.0f} ms while gpib1 waits"


def t_shared_connection():
    """Two links on one TCP connection both work, pipelined calls run in parallel."""
    need("k2000", "h3456")
    a = dev("k2000")
    b = vxi11.Instrument(HOST, INSTR["h3456"])
    b.client = a.client
    b.open()
    check("KEITHLEY" in a.ask("*IDN?"), "first link broken")
    b.read_stb()
    b.write("H F1 R1 T4")
    b.read()
    sock = a.client.sock
    sock.settimeout(10)

    def call(xid, proc, packfn, params):
        p = vxi11.Packer()
        p.pack_callheader(xid, vxi11.DEVICE_CORE_PROG, vxi11.DEVICE_CORE_VERS, proc,
                          (rpc.AUTH_NULL, b""), (rpc.AUTH_NULL, b""))
        packfn(p, params)
        rpc.sendrecord(sock, p.get_buf())
    t0 = time.time()
    call(1, vxi11.DEVICE_READ, vxi11.Packer.pack_device_read_parms, (b.link, 1000, 3000, 0, 0, 0))
    call(2, vxi11.DEVICE_WRITE, vxi11.Packer.pack_device_write_parms, (a.link, 3000, 0, 8, b"*IDN?\n"))
    call(3, vxi11.DEVICE_READ, vxi11.Packer.pack_device_read_parms, (a.link, 1000, 3000, 0, 0, 0))
    at = {}
    for _ in range(3):
        u = vxi11.Unpacker(rpc.recvrecord(sock))
        xid = u.unpack_uint()
        at[xid] = time.time() - t0
    a.client.destroy_link(b.link)
    a.client.destroy_link(a.link)
    a.client.close()
    a.link = b.link = None
    check(at[3] < 1.0 and at[1] > 2.5, f"not parallel: {at}")
    return f"gpib0 answered after {at[3]:.2f} s, gpib1 read timed out after {at[1]:.2f} s"


def t_fifo():
    """Four clients query the Keithley at once without locks: every answer is theirs."""
    need("k2000")
    queries = [("*IDN?", r"KEITHLEY"), (":SENS:FUNC?", r'^"[A-Z:]+"$'),
               (":FETCH?", r"^[+-]\d\.\d+E[+-]\d+$"), ("*OPC?", r"^1$")]
    bad = []

    def client(cmd, pat):
        d = dev("k2000")
        for _ in range(15):
            r = d.ask(cmd).strip()
            if not re.search(pat, r):
                bad.append((cmd, r))
        d.close()
    threads(*[lambda c=c, p=p: client(c, p) for c, p in queries])
    check(not bad, f"answers to the wrong client: {bad[:3]}")
    k = dev("k2000")
    err = k.ask(":SYST:ERR?").strip()
    k.close()
    check(err.startswith(("0", "+0")), f"Keithley reports {err}")
    return "60/60 answers to the right client, no instrument errors"


def t_fifo_mixed():
    """Two clients each on the scope (waveform + *IDN?) and on the 3245A, plus the Keithley."""
    need("k2000", "scope", "h3245")
    bad = []

    def run(name, n, fn, ok, setup=None):
        d = dev(name, 15)
        if setup:
            d.write(setup)
        for _ in range(n):
            try:
                r = fn(d)
                if not ok(r):
                    bad.append((name, r[:40]))
            except Exception as e:
                bad.append((name, str(e)))
        d.close()

    def wave(d):
        d.write(":WAV:DATA?")
        return d.read_raw()
    threads(lambda: run("scope", 6, wave, lambda r: r[:1] == b"#" and len(r) > 1000,
                        ":WAV:FORM BYTE;:WAV:POIN 1000"),
            lambda: run("scope", 15, lambda d: d.ask("*IDN?"), lambda r: "54624A" in r),
            lambda: run("h3245", 10, lambda d: d.ask("ID?"), lambda r: "3245" in r),
            lambda: run("h3245", 10, lambda d: d.ask("ERR?"), lambda r: re.fullmatch(r"\s*[+-]?\d+\s*", r)),
            lambda: run("k2000", 15, lambda d: d.ask("*IDN?"), lambda r: "KEITHLEY" in r))
    check(not bad, f"wrong answers: {bad[:3]}")
    return "56/56 correct across three buses"


def t_command_only_client():
    """A client streaming commands (no reads) doesn't starve a querying client."""
    need("k2000")
    stop = []

    def writer():
        d = dev("k2000")
        while not stop:
            d.write(":SENS:FUNC 'VOLT:DC'")
            time.sleep(0.05)
        d.close()
    w = threading.Thread(target=writer)
    w.start()
    time.sleep(0.3)
    q = dev("k2000")
    times = []
    for _ in range(4):
        t = time.time()
        check("KEITHLEY" in q.ask("*IDN?"), "bad answer")
        times.append(time.time() - t)
    stop.append(1)
    w.join()
    q.close()
    check(max(times) < 2.0, f"querier starved: {times}")
    return "querier waits " + ", ".join(f"{t:.2f}" for t in times) + " s (fifo_hold_s)"


def t_locks():
    """Explicit locks exclude other connections; a dropped client releases them."""
    need("k2000", "h3456")
    a = dev("k2000")
    a.lock()
    b = vxi11.Instrument(HOST, INSTR["h3456"])
    b.client = a.client
    b.open()
    c = dev("k2000")
    c.lock_timeout = 0
    try:
        c.ask("*IDN?")
        raise AssertionError("got through another client's lock")
    except vxi11.Vxi11Exception as e:
        check("locked" in str(e).lower(), str(e))
    b.read_stb()
    check("KEITHLEY" in a.ask("*IDN?"), "lock holder failed")
    a.client.sock.close()                         # vanish without unlock/destroy_link
    a.link = b.link = None
    time.sleep(0.5)
    c.lock()
    check("KEITHLEY" in c.ask("*IDN?"), "lock not released")
    c.unlock()
    c.close()
    return "locked out, released on client drop"


def t_binary():
    """Binary blocks containing EOT (0x04) and DLE (0x10) bytes arrive complete, and a
    binary setup written back three times reads back the same each time."""
    need("scope")
    s = dev("scope", 20)

    def block():
        s.write(":SYST:SET?")
        r = s.read_raw()
        nd = int(r[1:2])
        n = int(r[2:2 + nd])
        return r, r[2 + nd:2 + nd + n], n
    for _ in range(5):
        r, body, n = block()
        check(len(body) == n and len(r) <= 3 + int(r[1:2]) + n, f"block {len(body)} != {n}")
    eot, dle = body.count(0x04), body.count(0x10)
    outs = []
    for _ in range(3):
        s.write_raw(b":SYST:SET " + r.rstrip(b"\n"))
        time.sleep(1.5)
        outs.append(block()[0])
    err = s.ask(":SYST:ERR?").strip()
    s.close()
    check(outs[0] == outs[1] == outs[2], "setup read back differently after identical writes")
    check(err.startswith("+0"), err)
    return f"{n}-byte setup with {eot}x 0x04 and {dle}x 0x10: 5/5 reads complete, writes consistent"


def t_speed():
    """Timing, for comparison between firmware and gateway versions."""
    need("scope", "k2000")
    s = dev("scope", 20)
    s.write(":WAV:FORM BYTE")
    out = []
    for pts in (100, 2000):
        s.write(f":WAV:POIN {pts}")
        best = 99
        for _ in range(3):
            t = time.time()
            s.write(":WAV:DATA?")
            d = s.read_raw()
            best = min(best, time.time() - t)
        out.append(f"{len(d)} B waveform {best*1000:.0f} ms")
    msg = ";".join(["*CLS"] * 200)
    t = time.time()
    s.write(msg)
    s.ask("*OPC?")
    out.append(f"{len(msg)} B write {(time.time()-t)*1000:.0f} ms")
    s.close()
    k = dev("k2000")
    t = time.time()
    for _ in range(10):
        k.ask("*IDN?")
    out.append(f"K2000 *IDN? {(time.time()-t)*100:.0f} ms")
    k.close()
    return ", ".join(out)


def t_serial():
    """RS-232 Keithley: queries, readings, emulated serial poll, local/remote, clear,
    three clients at once through the FIFO, and a clean error queue afterwards."""
    need("k2000s")
    k = dev("k2000s")
    check("KEITHLEY" in k.ask("*IDN?"), "bad *IDN?")
    t = time.time()
    for _ in range(10):
        check(re.match(r"^[+-]\d\.\d+E[+-]\d+$", k.ask(":FETCH?").strip()), "bad reading")
    per = (time.time() - t) / 10
    stb = k.read_stb()
    k.local()
    k.write(":SYST:REM")
    k.clear()
    check("KEITHLEY" in k.ask("*IDN?"), "no answer after clear")
    bad = []

    def client(cmd, pat):
        d = dev("k2000s")
        for _ in range(10):
            r = d.ask(cmd).strip()
            if not re.search(pat, r):
                bad.append((cmd, r))
        d.close()
    threads(lambda: client("*IDN?", "KEITHLEY"), lambda: client(":FETCH?", r"E[+-]\d+$"),
            lambda: client(":SENS:FUNC?", r'^"[A-Z:]+"$'))
    err = k.ask(":SYST:ERR?").strip()
    k.close()
    check(not bad, f"wrong answers: {bad[:3]}")
    check(err.startswith(("0", "+0")), f"Keithley reports {err}")
    return f":FETCH? {per*1000:.0f} ms, stb {stb}, 30/30 FIFO answers correct, no instrument errors"


def t_listeners():
    """LISTENERS? scans all buses at once."""
    i = dev("inst0", 20)
    t = time.time()
    r = i.ask("LISTENERS?").strip()
    dt = time.time() - t
    i.close()
    check(r.startswith("gpib0="), r)
    return f"{r} in {dt:.2f} s"


TESTS = {n[2:]: f for n, f in globals().items() if n.startswith("t_")}


def main():
    global HOST
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("tests", nargs="*", choices=[[]] + list(TESTS), metavar="test",
                    help="any of: " + ", ".join(TESTS))
    args = ap.parse_args()
    HOST = args.host
    failed = 0
    for name in args.tests or TESTS:
        t = time.time()
        try:
            msg = TESTS[name]()
            status = "PASS"
        except Skip as e:
            status, msg = "SKIP", str(e)
        except (AssertionError, vxi11.Vxi11Exception, OSError, socket.timeout) as e:
            status, msg = "FAIL", f"{type(e).__name__}: {e}"
            failed += 1
        print(f"{status}  {name:22s} {time.time()-t:5.1f} s  {msg}", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
