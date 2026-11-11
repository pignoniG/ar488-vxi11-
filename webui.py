"""Web interface for the AR488 VXI-11 gateway: monitoring, configuration and tools.

Runs inside the gateway process (so it sees live queues, locks and clients) on a
plain http.server; the page is web/index.html, everything else is a JSON API.

Reading is open to the LAN; anything that changes state (settings, console,
logger, adapter actions) needs the password set on the Settings page.
Changes made here are stored in <state_dir>/state.json on top of the .toml file.
"""
import collections
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

log = logging.getLogger("webui")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
SESSION_S = 12 * 3600
PBKDF2_ITER = 120_000
MAX_BODY = 1 << 20
ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
LOGGER_POINTS = 5000            # points kept in memory per logger job (the CSV has all)
FLOAT_RE = re.compile(rb"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")

GW = None                       # the gateway module (ar488_vxi11 running as __main__)
STATE = None
CONFIG_PATH = None
SCAN = {}                       # bus -> {"addrs": [...], "t": time, "error": str}
SESSIONS = {}                   # token -> expiry
SESSIONS_LOCK = threading.Lock()
_neg_ids = iter(range(-1, -(1 << 40), -1))
LABELS = {}                     # negative link id -> label (console, logger jobs)


# ---- persistent state --------------------------------------------------------
class State:
    """JSON file with aliases, instrument notes, setting overrides, logger jobs and
    the web password hash."""

    def __init__(self, state_dir=None):
        dirs = [state_dir] if state_dir else []
        dirs += [os.environ.get("STATE_DIRECTORY", "").split(":")[0] or "/var/lib/ar488-vxi11",
                 os.path.expanduser("~/.local/state/ar488-vxi11")]
        self.dir = None
        for d in dirs:
            if not d:
                continue
            try:
                os.makedirs(d, exist_ok=True)
                test = os.path.join(d, ".w")
                open(test, "w").close()
                os.remove(test)
                self.dir = d
                break
            except OSError:
                continue
        if self.dir is None:
            raise RuntimeError("no writable state directory")
        self.path = os.path.join(self.dir, "state.json")
        self.lock = threading.RLock()
        self.data = {}
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.error("can't read %s (%s), starting with empty web state", self.path, e)
        for k, v in (("aliases", {}), ("instruments", {}), ("overrides", {}),
                     ("logger", []), ("web", {})):
            self.data.setdefault(k, v)

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            # owner-only: it holds the web password hash
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def aliases(self):
        return dict(self.data["aliases"])

    def apply_overrides(self, server_cfg, ifaces):
        ov = self.data["overrides"]
        server_cfg = {**server_cfg, **ov.get("server", {})}
        out = []
        for i in ifaces:
            o = ov.get("interface", {}).get(i["name"].lower(), {})
            out.append({**i, **o})
        return server_cfg, out


# ---- authentication ----------------------------------------------------------
def has_password():
    return bool(STATE.data["web"].get("pw"))


def set_password(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITER)
    with STATE.lock:
        STATE.data["web"]["pw"] = {"salt": salt.hex(), "hash": h.hex(), "iter": PBKDF2_ITER}
        STATE.save()
    with SESSIONS_LOCK:
        SESSIONS.clear()


def check_password(pw):
    p = STATE.data["web"].get("pw")
    if not p:
        return False
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(p["salt"]), p["iter"])
    return hmac.compare_digest(h.hex(), p["hash"])


def new_session():
    tok = secrets.token_urlsafe(24)
    with SESSIONS_LOCK:
        now = time.time()
        for k in [k for k, exp in SESSIONS.items() if exp < now]:
            del SESSIONS[k]
        SESSIONS[tok] = now + SESSION_S
    return tok


def session_ok(tok):
    with SESSIONS_LOCK:
        exp = SESSIONS.get(tok)
        return bool(exp and exp > time.time())


# ---- Pi health ---------------------------------------------------------------
class Health(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="health")
        self.temp = None
        self.undervoltage_now = False
        self.uv_events = collections.deque(maxlen=2000)   # start times of undervoltage
        self.throttled = None
        self.cpu = 0.0
        self.uv_path = None
        for d in sorted(os.listdir("/sys/class/hwmon")) if os.path.isdir("/sys/class/hwmon") else []:
            try:
                if open(f"/sys/class/hwmon/{d}/name").read().strip() == "rpi_volt":
                    self.uv_path = f"/sys/class/hwmon/{d}/in0_lcrit_alarm"
            except OSError:
                pass

    def run(self):
        last_cpu = sum(os.times()[:2])
        last_t = time.monotonic()
        n = 0
        while True:
            time.sleep(1)
            n += 1
            try:
                self.temp = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
            except (OSError, ValueError):
                pass
            if self.uv_path:
                try:
                    uv = open(self.uv_path).read().strip() == "1"
                    if uv and not self.undervoltage_now:
                        self.uv_events.append(time.time())
                    self.undervoltage_now = uv
                except OSError:
                    pass
            now, cpu = time.monotonic(), sum(os.times()[:2])
            self.cpu = 100 * (cpu - last_cpu) / max(now - last_t, 1e-3)
            last_cpu, last_t = cpu, now
            if n % 10 == 1:
                try:
                    out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                                         text=True, timeout=3).stdout
                    m = re.search(r"0x[0-9a-fA-F]+", out)
                    self.throttled = int(m.group(0), 16) if m else None
                except (OSError, subprocess.SubprocessError):
                    self.throttled = None

    def snapshot(self):
        now = time.time()
        t = self.throttled
        load = os.getloadavg()
        try:
            rss = int(re.search(r"VmRSS:\s+(\d+)", open("/proc/self/status").read()).group(1)) // 1024
        except (OSError, AttributeError):
            rss = None
        return {
            "temp": self.temp, "cpu": round(self.cpu, 1), "load": [round(x, 2) for x in load],
            "rss_mb": rss,
            "undervoltage_now": self.undervoltage_now or bool(t is not None and t & 0x1),
            "undervoltage_since_boot": bool(t is not None and t & 0x10000),
            "throttled_now": bool(t is not None and t & 0x4),
            "throttled_since_boot": bool(t is not None and t & 0x40000),
            "throttled_raw": None if t is None else hex(t),
            "uv_last_hour": sum(1 for x in self.uv_events if now - x < 3600),
            "uv_last_day": sum(1 for x in self.uv_events if now - x < 86400),
            "uv_since_start": len(self.uv_events),
            "uv_sensor": bool(self.uv_path),
        }


HEALTH = Health()


# ---- talking to instruments from the web (console, identify, logger) ----------
def registry():
    return GW.IS.Vxi11Server._device_registry


def make_device(resource, label):
    """A link-like device for internal use. It shares the instrument's FIFO turn
    and VXI-11 lock with the real clients, under a negative link id."""
    reg = registry()
    key = reg.resolve(resource)
    dev = reg.factory(key)
    dev.client_label = label
    dev.link_id = next(_neg_ids)
    LABELS[dev.link_id] = label
    dev.device_init()
    return key, dev


def err_text(code):
    V = GW.V
    return {V.ERR_IO_TIMEOUT: "timeout", V.ERR_IO_ERROR: "I/O error (see events)",
            V.ERR_DEVICE_LOCKED_BY_ANOTHER_LINK: "locked by another client",
            V.ERR_OPERATION_NOT_SUPPORTED: "not supported"}.get(code, f"VXI-11 error {code}")


class IOError_(Exception):
    pass


def dev_io(dev, op, data=b"", timeout_ms=3000):
    """Run one console operation through the same path as a VXI-11 client.
    Returns (reply bytes or None, stb or None)."""
    V = GW.V
    with dev.lock(dev.link_id, 0, 0) as err:
        if err != V.ERR_NO_ERROR:
            raise IOError_(err_text(err))
        if op in ("write", "query"):
            e = dev.device_write(data, V.OP_FLAG_END, timeout_ms)
            if e != V.ERR_NO_ERROR:
                raise IOError_(err_text(e))
            if op == "write":
                return None, None
        if op in ("read", "query"):
            out = bytearray()
            while True:
                e, reason, chunk = dev.device_read(1 << 20, 0, 0, timeout_ms)
                if e != V.ERR_NO_ERROR:
                    raise IOError_(err_text(e))
                out += chunk
                if reason & GW.Vxi11.ReadRespReason.END or not chunk:
                    return bytes(out), None
        if op == "stb":
            e, stb = dev.device_readstb(0, timeout_ms)
            if e != V.ERR_NO_ERROR:
                raise IOError_(err_text(e))
            return None, stb
        fn = {"clear": dev.device_clear, "trigger": dev.device_trigger, "local": dev.device_local}.get(op)
        if not fn:
            raise IOError_(f"unknown operation {op}")
        e = fn(0, timeout_ms)
        if e != V.ERR_NO_ERROR:
            raise IOError_(err_text(e))
        return None, None


CONSOLE = {}                    # canonical name -> device
CONSOLE_LOCK = threading.Lock()


def console_device(resource):
    key = registry().resolve(resource)
    with CONSOLE_LOCK:
        if key not in CONSOLE:
            CONSOLE[key] = make_device(key, "web console")[1]
        return CONSOLE[key]


# ---- logger ------------------------------------------------------------------
class LoggerJob(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True, name=f"logger-{cfg['id']}")
        self.cfg = cfg
        self.stop_ev = threading.Event()
        self.points = collections.deque(maxlen=LOGGER_POINTS)   # (t, value or None, reply)
        self.count = 0
        self.errors = 0
        self.last_error = ""
        self.last_reply = ""
        self.started = None
        self.csv_path = os.path.join(STATE.dir, "logs", f"{cfg['id']}.csv")
        self._load_tail()

    def _load_tail(self):
        try:
            with open(self.csv_path, newline="") as f:
                rows = list(csv.reader(f))[1:]
            for r in rows[-LOGGER_POINTS:]:
                try:
                    v = float(r[2]) if r[2] != "" else None
                    self.points.append((float(r[1]), v, r[3] if len(r) > 3 else ""))
                except (ValueError, IndexError):
                    pass
        except OSError:
            pass

    def run(self):
        c = self.cfg
        self.started = time.time()
        os.makedirs(os.path.dirname(self.csv_path), exist_ok=True)
        try:
            key, dev = make_device(c["resource"], f"logger: {c['name']}")
        except KeyError:
            self.last_error = f"unknown instrument {c['resource']}"
            return
        tmo = int(float(c.get("timeout_s", 5)) * 1000)
        if c.get("setup"):
            try:
                dev_io(dev, "write", unescape(c["setup"]), tmo)
            except IOError_ as e:
                self.errors += 1
                self.last_error = f"{time.strftime('%H:%M:%S')} setup: {e}"
        interval = max(0.2, float(c.get("interval_s", 1)))
        nxt = time.monotonic()
        while not self.stop_ev.is_set():
            t = time.time()
            try:
                reply, _ = dev_io(dev, "query", unescape(c["query"]), tmo)
                m = FLOAT_RE.search(reply or b"")
                v = float(m.group(0)) if m else None
                txt = (reply or b"").decode("latin-1").strip()
            except IOError_ as e:
                v, txt = None, ""
                self.errors += 1
                self.last_error = f"{time.strftime('%H:%M:%S')} {e}"
            self.points.append((t, v, txt))
            self.count += 1
            self.last_reply = txt[:80]
            self._append(t, v, txt)
            nxt += interval
            delay = nxt - time.monotonic()
            if delay < 0:                       # fell behind (slow instrument): don't burst
                nxt = time.monotonic()
                delay = 0
            self.stop_ev.wait(delay)
        dev.device_close()

    def _append(self, t, v, txt):
        new = not os.path.exists(self.csv_path)
        try:
            with open(self.csv_path, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["time", "unix", "value", "reply"])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}",
                            f"{t:.3f}", "" if v is None else repr(v), txt[:200]])
        except OSError as e:
            self.last_error = f"CSV: {e}"

    def info(self):
        return {**self.cfg, "running": self.is_alive(), "count": self.count, "errors": self.errors,
                "last_error": self.last_error, "last_reply": self.last_reply,
                "started": self.started,
                "csv_bytes": os.path.getsize(self.csv_path) if os.path.exists(self.csv_path) else 0}


JOBS = {}                       # id -> LoggerJob (running or last run)
JOBS_LOCK = threading.Lock()


def job_cfgs():
    return {j["id"]: j for j in STATE.data["logger"]}


def start_job(jid):
    with JOBS_LOCK:
        old = JOBS.get(jid)
        if old and old.is_alive():
            return
        cfg = job_cfgs()[jid]
        j = LoggerJob(cfg)
        JOBS[jid] = j
        j.start()


def stop_job(jid):
    with JOBS_LOCK:
        j = JOBS.get(jid)
    if j:
        j.stop_ev.set()
        j.join(timeout=15)


def unescape(s):
    """Console/logger text: \\n \\r \\t \\xHH \\\\ escapes -> bytes."""
    out = bytearray()
    i = 0
    b = s.encode("latin-1", "replace")
    while i < len(b):
        c = b[i]
        if c == 0x5C and i + 1 < len(b):
            n = chr(b[i + 1])
            if n in "nrt\\":
                out.append({"n": 10, "r": 13, "t": 9, "\\": 92}[n])
                i += 2
                continue
            if n == "x" and i + 3 < len(b) + 0 and re.fullmatch(rb"[0-9a-fA-F]{2}", b[i + 2:i + 4]):
                out.append(int(b[i + 2:i + 4], 16))
                i += 4
                continue
        out.append(c)
        i += 1
    return bytes(out)


# ---- status ------------------------------------------------------------------
def rate(st):
    now = time.time()
    return round(sum(1 for t in st["recent"] if now - t < 10) / 10, 1)


def lock_holders():
    """canonical name -> client label of whoever holds its VXI-11 lock."""
    IS = GW.IS
    reg = registry()
    out = {}
    with IS.CONNECTIONS_LOCK:
        conns = list(IS.CONNECTIONS.values())
    link_client = {}
    for c in conns:
        for lid in list(c.links):
            link_client[lid] = c.client_address[0]
    for key, item in list(reg._registry.items()):
        lid = getattr(item.lock, "lock_id", 0)
        if lid:
            out[key] = link_client.get(lid) or LABELS.get(lid, f"link {lid}")
    return out


def instruments_on(bus):
    addrs = set(SCAN.get(bus, {}).get("addrs", []))
    for key in list(GW.STATS) + list(STATE.data["instruments"]) + list(STATE.data["aliases"].values()):
        b, _, a = key.partition(",")
        if b == bus and a.isdigit():
            addrs.add(int(a))
    return sorted(addrs)


def status():
    holders = lock_holders()
    aliases_by_key = collections.defaultdict(list)
    for al, key in STATE.data["aliases"].items():
        aliases_by_key[key].append(al)
    buses = []
    for name, a in sorted(GW.ADAPTERS.items()):
        insts = []
        scan = SCAN.get(name, {})
        for addr in instruments_on(name):
            key = f"{name},{addr}"
            st = GW.STATS.get(key)
            info = STATE.data["instruments"].get(key, {})
            turn = GW.TURNS.get(key)
            owner = waiting = None
            if turn:
                with turn.cond:
                    owner = turn.owner.client if turn.owner is not None else None
                    waiting = len(turn.waiters)
            insts.append({
                "key": key, "addr": addr, "aliases": sorted(aliases_by_key.get(key, [])),
                "idn": info.get("idn", ""), "note": info.get("note", ""),
                "listening": addr in scan.get("addrs", []) if scan else None,
                "no_eoi": addr in a.no_eoi,
                "ops": st["ops"] if st else 0, "errors": st["errors"] if st else 0,
                "rate": rate(st) if st else 0, "last": st["last"] if st else None,
                "last_client": st["last_client"] if st else "",
                "last_error": st["last_error"] if st else "",
                "bytes_in": st["bytes_in"] if st else 0, "bytes_out": st["bytes_out"] if st else 0,
                "turn_owner": owner, "waiting": waiting or 0, "locked_by": holders.get(key),
            })
        ser = a.ser
        buses.append({
            "name": name, "state": "ready" if ser is not None else "not connected",
            "port": a.current_port or a.port, "pinned": bool(a.port),
            "version": a.version, "baud": ser.baudrate if ser is not None else a.baud,
            "eot_esc": a.eot_esc, "rxbuf": a.rxbuf, "opens": a.opens,
            "serial_errors": a.serial_errors, "last_error": a.last_error,
            "opened_at": a.opened_at or None, "busy": a.lock.locked(),
            "scan_t": scan.get("t"), "scan_error": scan.get("error", ""),
            "instruments": insts,
        })
    return {
        "host": os.uname().nodename, "version": GW.VERSION, "started": GW.STARTED_AT,
        "now": time.time(), "buses": buses, "clients": clients(),
        "health": HEALTH.snapshot(), "events": list(GW.EVENTS)[-8:],
        "fifo_hold_s": GW.FIFO_HOLD_S,
    }


def clients():
    IS = GW.IS
    with IS.CONNECTIONS_LOCK:
        conns = list(IS.CONNECTIONS.values())
    out = []
    for c in conns:
        links = []
        for lid, dev in list(c.links.items()):
            links.append({"link": lid, "device": dev.device_name,
                          "locked": getattr(dev.lock, "lock_id", 0) == lid})
        out.append({"id": c.conn_id, "ip": c.client_address[0], "port": c.client_address[1],
                    "connected_at": c.connected_at, "last_activity": c.last_activity,
                    "calls": c.calls, "links": links, "internal": False})
    with JOBS_LOCK:
        for j in JOBS.values():
            if j.is_alive():
                out.append({"id": None, "ip": f"logger: {j.cfg['name']}", "port": None,
                            "connected_at": j.started, "last_activity": j.points[-1][0] if j.points else j.started,
                            "calls": j.count, "links": [{"link": None, "device": j.cfg["resource"], "locked": False}],
                            "internal": True})
    return out


# ---- configuration -----------------------------------------------------------
IFACE_KEYS = {"port": str, "baud": int, "ifc_on_open": bool, "no_eoi": list,
              "no_eoi_tmo_ms": int, "auto_no_eoi": bool}


def effective_config():
    ov = STATE.data["overrides"]
    ifaces = []
    for name, a in sorted(GW.ADAPTERS.items()):
        ifaces.append({"name": name, "port": a.port or "", "baud": a.baud or 0,
                       "ifc_on_open": a.ifc_on_open, "no_eoi": sorted(a.no_eoi),
                       "no_eoi_tmo_ms": a.no_eoi_tmo_ms, "auto_no_eoi": a.auto_no_eoi,
                       "overridden": sorted(ov.get("interface", {}).get(name, {}))})
    return {
        "config_file": CONFIG_PATH, "state_file": STATE.path,
        "server": {"fifo_hold_s": GW.FIFO_HOLD_S,
                   "log_level": logging.getLevelName(logging.getLogger().level),
                   "overridden": sorted(ov.get("server", {}))},
        "interfaces": ifaces,
    }


def apply_settings(body):
    """Validate, apply live where possible, store as overrides. Returns notes."""
    notes = []
    ov = STATE.data["overrides"]
    srv = body.get("server") or {}
    new_srv = {}
    if "fifo_hold_s" in srv:
        v = float(srv["fifo_hold_s"])
        if not 0 <= v <= 60:
            raise ValueError("fifo_hold_s must be 0 to 60")
        GW.FIFO_HOLD_S = v
        new_srv["fifo_hold_s"] = v
    if "log_level" in srv:
        lv = str(srv["log_level"]).upper()
        if lv not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ValueError("log_level must be DEBUG, INFO, WARNING or ERROR")
        logging.getLogger().setLevel(lv)
        new_srv["log_level"] = lv
    reopen = []
    new_if = {}
    for name, vals in (body.get("interface") or {}).items():
        a = GW.ADAPTERS.get(name)
        if not a:
            raise ValueError(f"no bus {name}")
        o = {}
        for k, v in vals.items():
            if k not in IFACE_KEYS:
                raise ValueError(f"unknown setting {k}")
            if k == "no_eoi":
                v = sorted({int(x) for x in v})
                if any(not 0 <= x <= 30 for x in v):
                    raise ValueError("no_eoi addresses must be 0 to 30")
                a.no_eoi = set(v)
            elif k == "no_eoi_tmo_ms":
                v = int(v)
                if not 20 <= v <= 3000:
                    raise ValueError("no_eoi_tmo_ms must be 20 to 3000")
                a.no_eoi_tmo_ms = v
                a._tmo = None
            elif k == "auto_no_eoi":
                a.auto_no_eoi = v = bool(v)
            elif k == "ifc_on_open":
                a.ifc_on_open = v = bool(v)
            elif k == "baud":
                v = int(v or 0)
                if v not in (0, 115200, 500000):
                    raise ValueError("baud must be automatic, 115200 or 500000")
                if (a.baud or 0) != v:
                    reopen.append(name)
                a.baud = v or None
            elif k == "port":
                v = str(v or "").strip()
                if v and not v.startswith("/dev/"):
                    raise ValueError("port must be a /dev/... path or empty")
                if (a.port or "") != v:
                    reopen.append(name)
                a.port = v or None
            o[k] = v
        new_if[name] = o
    with STATE.lock:
        ov.setdefault("server", {}).update(new_srv)
        for name, o in new_if.items():
            ov.setdefault("interface", {}).setdefault(name, {}).update(o)
        STATE.save()
    if reopen:
        notes.append("Port or speed changed: reopen " + ", ".join(sorted(set(reopen))) + " to use it")
    return notes


def reset_overrides(section):
    with STATE.lock:
        ov = STATE.data["overrides"]
        if section == "server":
            ov.pop("server", None)
        else:
            ov.get("interface", {}).pop(section, None)
        STATE.save()
    return ["Overrides removed. The values from the config file apply after a restart"]


def adapter_action(bus, action):
    a = GW.ADAPTERS.get(bus)
    if not a:
        raise ValueError(f"no bus {bus}")
    if action == "reopen":
        with a.lock:
            a.close()
            a._ensure_open()
        return f"{bus} reopened: {a.version}, {a.ser.baudrate} baud"
    if action == "ifc":
        a._run(lambda: (a._cmd("++ifc"), a._sync()))
        return f"IFC pulsed on {bus}"
    if action == "store_name":
        def f():
            a._cmd(f"++id name {bus}")
            a._cmd("++savecfg")
            time.sleep(0.3)
            a.ser.reset_input_buffer()
            return a._query("++id name")
        got = a._run(f)
        if got.lower() != bus:
            raise ValueError(f"adapter reports name {got!r} after saving")
        return f"name {bus!r} stored in the adapter"
    raise ValueError(f"unknown action {action}")


def scan_buses():
    def one(item):
        name, a = item
        try:
            SCAN[name] = {"addrs": [int(x) for x in a.find_listeners()], "t": time.time(), "error": ""}
        except Exception as e:
            SCAN[name] = {"addrs": SCAN.get(name, {}).get("addrs", []), "t": time.time(), "error": str(e)}
    with ThreadPoolExecutor(max(1, len(GW.ADAPTERS))) as pool:
        list(pool.map(one, sorted(GW.ADAPTERS.items())))
    return {n: SCAN[n] for n in sorted(SCAN)}


def set_alias(key, alias, note):
    key = registry().resolve(key)
    if key == "inst0":
        raise ValueError("the gateway itself can't get an alias")
    with STATE.lock:
        al = STATE.data["aliases"]
        for k in [k for k, v in al.items() if v == key]:
            del al[k]
            GW.ALIASES.pop(k, None)
        for a in [x.strip().lower() for x in (alias or "").split(",") if x.strip()]:
            if not ALIAS_RE.match(a) or GW.NAME_RE.match(a) or a == "inst0" or a.startswith("gpib"):
                raise ValueError(f"alias {a!r}: use letters, digits, - and _ (not gpib..., inst0)")
            if a in al and al[a] != key:
                raise ValueError(f"alias {a!r} is already used for {al[a]}")
            al[a] = key
            GW.ALIASES[a] = key
        if note is not None:
            STATE.data["instruments"].setdefault(key, {})["note"] = note.strip()[:200]
        STATE.save()


def export_state():
    d = json.loads(json.dumps(STATE.data))
    d.get("web", {}).pop("pw", None)
    d["exported"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return d


def import_state(d):
    if not isinstance(d, dict):
        raise ValueError("not a gateway settings file")
    with STATE.lock:
        for k in ("aliases", "instruments", "overrides"):
            if k in d:
                if not isinstance(d[k], dict):
                    raise ValueError(f"{k} must be an object")
                STATE.data[k] = d[k]
        if "logger" in d:
            if not isinstance(d["logger"], list):
                raise ValueError("logger must be a list")
            STATE.data["logger"] = d["logger"]
        STATE.save()
    GW.ALIASES.clear()
    GW.ALIASES.update(STATE.aliases())
    return ["Imported. Restart the gateway to apply bus settings and logger jobs"]


# ---- HTTP ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ar488-vxi11-web"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    # helpers
    def _send(self, code, body, ctype="application/json", headers=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _logged_in(self):
        return session_ok(self._cookie("gw_session") or "")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    # GET
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/status":
                d = status()
                d["auth"] = {"has_password": has_password(), "logged_in": self._logged_in()}
                return self._send(200, d)
            if u.path == "/api/trace":
                since = int(q.get("since", 0))
                with GW.TRACE_LOCK:
                    rows = [r for r in GW.TRACE if r["seq"] > since]
                return self._send(200, {"rows": rows[-500:]})
            if u.path == "/api/events":
                return self._send(200, {"events": list(GW.EVENTS)})
            if u.path == "/api/config":
                return self._send(200, effective_config())
            if u.path == "/api/logger":
                with JOBS_LOCK:
                    jobs = [JOBS[j["id"]].info() if j["id"] in JOBS else
                            {**j, "running": False, "count": 0, "errors": 0, "last_error": "",
                             "last_reply": "", "started": None,
                             "csv_bytes": _csv_size(j["id"])}
                            for j in STATE.data["logger"]]
                return self._send(200, {"jobs": jobs})
            m = re.fullmatch(r"/api/logger/([\w-]+)/(data|csv)", u.path)
            if m:
                jid, what = m.groups()
                if what == "csv":
                    path = os.path.join(STATE.dir, "logs", f"{jid}.csv")
                    if not os.path.exists(path):
                        return self._send(404, {"error": "no data yet"})
                    with open(path, "rb") as f:
                        data = f.read()
                    name = re.sub(r"[^\w-]+", "_", job_cfgs().get(jid, {}).get("name", jid))
                    return self._send(200, data, "text/csv",
                                      {"Content-Disposition": f'attachment; filename="{name}.csv"'})
                since = float(q.get("since", 0))
                with JOBS_LOCK:
                    j = JOBS.get(jid)
                pts = [p for p in (j.points if j else []) if p[0] > since]
                if not j:
                    pts = _csv_points(jid, since)
                return self._send(200, {"points": pts[-LOGGER_POINTS:]})
            if u.path == "/api/export":
                return self._send(200, json.dumps(export_state(), indent=1).encode(), "application/json",
                                  {"Content-Disposition": 'attachment; filename="ar488-vxi11-settings.json"'})
            return self._send(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as e:
            log.exception("GET %s", u.path)
            self._send(500, {"error": str(e)})

    # POST
    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send(400, {"error": f"bad request: {e}"})
        p = u.path
        try:
            if p == "/api/login":
                if check_password(str(body.get("password", ""))):
                    tok = new_session()
                    return self._send(200, {"ok": True}, headers={
                        "Set-Cookie": f"gw_session={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_S}"})
                time.sleep(1)
                return self._send(403, {"error": "Wrong password"})
            if p == "/api/logout":
                with SESSIONS_LOCK:
                    SESSIONS.pop(self._cookie("gw_session") or "", None)
                return self._send(200, {"ok": True}, headers={"Set-Cookie": "gw_session=; Path=/; Max-Age=0"})
            if p == "/api/password":
                new = str(body.get("new", ""))
                if has_password() and not self._logged_in():
                    return self._send(403, {"error": "Log in first"})
                if len(new) < 6:
                    return self._send(400, {"error": "Use at least 6 characters"})
                set_password(new)
                tok = new_session()
                log.info("web password %s from %s", "changed" if body.get("change") else "set",
                         self.client_address[0])
                return self._send(200, {"ok": True}, headers={
                    "Set-Cookie": f"gw_session={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_S}"})
            # everything else changes something: password required
            if not has_password():
                return self._send(403, {"error": "Set a password on the Settings page first"})
            if not self._logged_in():
                return self._send(403, {"error": "Log in to make changes"})
            return self._send(200, self._post(p, body))
        except (ValueError, KeyError, IOError_) as e:
            msg = f"unknown instrument {e}" if isinstance(e, KeyError) else str(e)
            return self._send(400, {"error": msg})
        except GW.AdapterError as e:
            return self._send(502, {"error": str(e)})
        except BrokenPipeError:
            pass
        except Exception as e:
            log.exception("POST %s", p)
            return self._send(500, {"error": str(e)})

    def _post(self, p, body):
        who = self.client_address[0]
        if p == "/api/io":
            op = body.get("op", "query")
            dev = console_device(str(body.get("resource", "")))
            data = unescape(str(body.get("data", "")))
            if body.get("newline", True) and op in ("write", "query") and not data.endswith(b"\n"):
                data += b"\n"
            tmo = int(body.get("timeout_ms", 3000))
            t0 = time.monotonic()
            reply, stb = dev_io(dev, op, data, max(100, min(tmo, 120000)))
            return {"reply": None if reply is None else reply.decode("latin-1"),
                    "reply_hex": None if reply is None else reply.hex(" "),
                    "stb": stb, "ms": round((time.monotonic() - t0) * 1000, 1)}
        if p == "/api/scan":
            return {"scan": scan_buses()}
        if p == "/api/identify":
            key = registry().resolve(str(body["resource"]))
            cmd = str(body.get("cmd") or "*IDN?")
            reply, _ = dev_io(console_device(key), "query", unescape(cmd) + b"\n",
                              int(body.get("timeout_ms", 2000)))
            idn = (reply or b"").decode("latin-1").strip()
            with STATE.lock:
                STATE.data["instruments"].setdefault(key, {}).update({"idn": idn, "id_cmd": cmd})
                STATE.save()
            return {"idn": idn}
        if p == "/api/instrument":
            set_alias(str(body["resource"]), body.get("aliases", ""), body.get("note"))
            if body.get("forget"):
                key = registry().resolve(str(body["resource"]))
                with STATE.lock:
                    STATE.data["instruments"].pop(key, None)
                    STATE.save()
            return {"ok": True}
        if p == "/api/settings":
            notes = apply_settings(body)
            log.info("settings changed from %s: %s", who, json.dumps(body))
            return {"ok": True, "notes": notes, "config": effective_config()}
        if p == "/api/settings/reset":
            return {"ok": True, "notes": reset_overrides(str(body["section"]))}
        if p == "/api/adapter":
            msg = adapter_action(str(body["bus"]), str(body["action"]))
            log.info("%s (from %s)", msg, who)
            return {"ok": True, "message": msg}
        if p == "/api/client/disconnect":
            IS = GW.IS
            with IS.CONNECTIONS_LOCK:
                c = IS.CONNECTIONS.get(int(body["id"]))
            if not c:
                raise ValueError("client already gone")
            log.info("disconnecting client %s (from web, %s)", c.client_address[0], who)
            c.disconnect()
            return {"ok": True}
        if p == "/api/restart":
            log.info("restart requested from %s", who)
            threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
            return {"ok": True}
        if p == "/api/import":
            return {"ok": True, "notes": import_state(body)}
        if p == "/api/logger/save":
            return self._logger_save(body)
        m = re.fullmatch(r"/api/logger/([\w-]+)/(start|stop|delete|clear)", p)
        if m:
            jid, act = m.groups()
            if jid not in job_cfgs():
                raise ValueError("no such logger job")
            if act == "start":
                _job_enabled(jid, True)
                start_job(jid)
            elif act == "stop":
                _job_enabled(jid, False)
                stop_job(jid)
            elif act == "clear":
                path = os.path.join(STATE.dir, "logs", f"{jid}.csv")
                if os.path.exists(path):
                    os.remove(path)
                with JOBS_LOCK:
                    if jid in JOBS:
                        JOBS[jid].points.clear()
            elif act == "delete":
                stop_job(jid)
                with STATE.lock:
                    STATE.data["logger"] = [j for j in STATE.data["logger"] if j["id"] != jid]
                    STATE.save()
                with JOBS_LOCK:
                    JOBS.pop(jid, None)
                path = os.path.join(STATE.dir, "logs", f"{jid}.csv")
                if os.path.exists(path):
                    os.remove(path)
            return {"ok": True}
        raise ValueError(f"unknown request {p}")

    def _logger_save(self, body):
        name = str(body.get("name", "")).strip()[:60]
        if not name:
            raise ValueError("Give the job a name")
        key = registry().resolve(str(body.get("resource", "")))
        if key == "inst0":
            raise ValueError("pick an instrument")
        query = str(body.get("query", "")).strip()
        if not query:
            raise ValueError("Enter the query to send, e.g. READ?")
        interval = float(body.get("interval_s", 1))
        if not 0.2 <= interval <= 86400:
            raise ValueError("interval must be 0.2 s to 1 day")
        timeout = float(body.get("timeout_s", 5))
        if not 0.1 <= timeout <= 120:
            raise ValueError("timeout must be 0.1 to 120 s")
        jid = str(body.get("id") or "") or secrets.token_hex(4)
        cfg = {"id": jid, "name": name, "resource": str(body["resource"]).strip(),
               "setup": str(body.get("setup", "")), "query": query, "interval_s": interval,
               "timeout_s": timeout, "enabled": bool(body.get("enabled", False))}
        was_running = False
        with JOBS_LOCK:
            j = JOBS.get(jid)
            was_running = bool(j and j.is_alive())
        if was_running:
            stop_job(jid)
        with STATE.lock:
            jobs = [j for j in STATE.data["logger"] if j["id"] != jid]
            jobs.append(cfg)
            STATE.data["logger"] = jobs
            STATE.save()
        with JOBS_LOCK:
            JOBS.pop(jid, None)
        if cfg["enabled"] or was_running:
            _job_enabled(jid, True)
            start_job(jid)
        return {"ok": True, "id": jid}


def _job_enabled(jid, on):
    with STATE.lock:
        for j in STATE.data["logger"]:
            if j["id"] == jid:
                j["enabled"] = on
        STATE.save()


def _csv_size(jid):
    p = os.path.join(STATE.dir, "logs", f"{jid}.csv")
    return os.path.getsize(p) if os.path.exists(p) else 0


def _csv_points(jid, since):
    path = os.path.join(STATE.dir, "logs", f"{jid}.csv")
    pts = []
    try:
        with open(path, newline="") as f:
            for r in list(csv.reader(f))[1:][-LOGGER_POINTS:]:
                try:
                    t = float(r[1])
                    if t > since:
                        pts.append((t, float(r[2]) if r[2] else None, r[3] if len(r) > 3 else ""))
                except (ValueError, IndexError):
                    pass
    except OSError:
        pass
    return pts


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(gw, state, config_path, server_cfg, bind="0.0.0.0", port=8080):
    global GW, STATE, CONFIG_PATH
    GW, STATE, CONFIG_PATH = gw, state, config_path
    HEALTH.start()
    srv = Server((bind, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    log.info("web interface on http://%s:%d/ (settings in %s)", os.uname().nodename, port, state.dir)
    for j in STATE.data["logger"]:
        if j.get("enabled"):
            try:
                start_job(j["id"])
            except Exception:
                log.exception("logger job %s didn't start", j.get("name"))
    return srv
