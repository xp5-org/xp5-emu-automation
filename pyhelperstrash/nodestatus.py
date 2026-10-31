"""Per-host status for the Instances tab's "Hosts" section: hostname, OS, mem, CPU%, disk free (local disk and shared NAS mount separately), system clock (UTC).
start_reporter_thread() writes /testrunnerapp/nodestatus/{hostname}.json every REPORT_INTERVAL s; list_nodes() reads all and drops records older than STALE_AFTER s.
A record older than that is kept with node flag TIME_ERR if it is still being rewritten (_clock_alive): wrong-clock host vs dead host.
No psutil: /proc (Linux), sysctl/vm_stat (macOS), Win32 API (Windows).
"""
import ctypes
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time

# testrunnerapp path on the shared mount (/testrunnerapp, X:\\testrunnerapp)
_TESTSRC = os.environ.get("TESTSRC_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPBASE_DIR = os.environ.get("TESTRUNNERAPP_ROOT") or os.path.join(os.path.dirname(_TESTSRC), "testrunnerapp")
NODESTATUS_DIR = os.path.join(APPBASE_DIR, "nodestatus")
REPORT_INTERVAL = 5
STALE_AFTER = 30
TIME_ERR = "TIME_ERR"
CLOCK_SKEW_TOLERANCE = 10
RECHECK_AFTER = 2 * REPORT_INTERVAL

_reporter_started = False
_reporter_lock = threading.Lock()
_last_cpu_snapshot = None

# hostname -> [last timestamp seen, local monotonic time it last changed | None]
_observed = {}
_observed_lock = threading.Lock()


def _os_description():
    if platform.system() == "SunOS":
        # illumos/Solaris: human name from first line of /etc/release
        try:
            with open("/etc/release", encoding="utf-8") as f:
                first = f.readline().strip()
            if first:
                return first
        except OSError:
            pass
        return platform.platform()
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def _read_proc_stat_times():
    with open("/proc/stat", encoding="utf-8") as f:
        fields = [int(x) for x in f.readline().split()[1:]]
    idle = fields[3] + fields[4]
    total = sum(fields)
    return total, idle


def _kstat(module, name, stats):
    """{stat: int} from `kstat -p -m module -n name` (illumos/Solaris), summed across instances; non-integers skipped."""
    out = subprocess.check_output(["kstat", "-p", "-m", module, "-n", name],
                                  encoding="utf-8")
    totals = {}
    for line in out.splitlines():
        key, _, value = line.partition("\t")
        stat = key.rsplit(":", 1)[-1]
        if stat not in stats:
            continue
        try:
            totals[stat] = totals.get(stat, 0) + int(value.strip())
        except ValueError:
            continue
    return totals


def _read_sunos_times():
    t = _kstat("cpu", "sys", ("cpu_ticks_idle", "cpu_ticks_kernel",
                              "cpu_ticks_user", "cpu_ticks_wait"))
    idle = t.get("cpu_ticks_idle", 0) + t.get("cpu_ticks_wait", 0)
    return sum(t.values()), idle


def _read_windows_times():
    idle, kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    total = kernel.value + user.value
    return total, idle.value


def _cpu_percent():
    """% CPU busy since the last call. None on the first call (no baseline yet)."""
    global _last_cpu_snapshot
    system = platform.system()
    if system == "Linux":
        total, idle = _read_proc_stat_times()
    elif system == "Windows":
        total, idle = _read_windows_times()
    elif system == "SunOS":
        try:
            total, idle = _read_sunos_times()
        except (OSError, subprocess.SubprocessError):
            return None
    else:
        # macOS: approximate CPU from load average
        try:
            load1 = os.getloadavg()[0]
        except (AttributeError, OSError):
            return None
        return round(min(load1 / (os.cpu_count() or 1) * 100, 100.0), 1)
    if _last_cpu_snapshot is None:
        _last_cpu_snapshot = (total, idle)
        return None
    last_total, last_idle = _last_cpu_snapshot
    _last_cpu_snapshot = (total, idle)
    dtotal = total - last_total
    didle = idle - last_idle
    if dtotal <= 0:
        return 0.0
    return round((1 - didle / dtotal) * 100, 1)


def _mac_vm_stat_pages():
    out = subprocess.check_output(["vm_stat"], encoding="utf-8")
    page_size = 4096
    stats = {}
    for line in out.splitlines():
        if line.startswith("Mach Virtual Memory Statistics"):
            page_size = int(line.split("page size of ")[1].split(" bytes")[0])
            continue
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        try:
            stats[key.strip()] = int(rest.strip().rstrip("."))
        except ValueError:
            continue
    return page_size, stats.get("Pages free", 0), stats.get("Pages inactive", 0)


class _WindowsMemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _mem_stats():
    system = platform.system()
    if system == "Linux":
        info = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    info[key] = int(rest.strip().split()[0])
        total_mb = info.get("MemTotal", 0) / 1024
        avail_mb = info.get("MemAvailable", 0) / 1024
    elif system == "Windows":
        stat = _WindowsMemoryStatusEx()
        stat.dwLength = ctypes.sizeof(_WindowsMemoryStatusEx)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        total_mb = stat.ullTotalPhys / (1024 ** 2)
        avail_mb = stat.ullAvailPhys / (1024 ** 2)
    elif system == "SunOS":
        # freemem; overstates "used" (ZFS ARC reclaimable)
        pages = _kstat("unix", "system_pages", ("physmem", "freemem"))
        page_size = os.sysconf("SC_PAGE_SIZE")
        total_mb = pages.get("physmem", 0) * page_size / (1024 ** 2)
        avail_mb = pages.get("freemem", 0) * page_size / (1024 ** 2)
    elif system == "FreeBSD":
        # free + inactive pages
        def _sysctl_int(name):
            return int(subprocess.check_output(["sysctl", "-n", name]))
        page_size = _sysctl_int("hw.pagesize")
        total_mb = _sysctl_int("hw.physmem") / (1024 ** 2)
        free_pages = (_sysctl_int("vm.stats.vm.v_free_count")
                      + _sysctl_int("vm.stats.vm.v_inactive_count"))
        avail_mb = free_pages * page_size / (1024 ** 2)
    else:
        # hw.memsize total; free + inactive = "available"
        total_mb = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) / (1024 ** 2)
        page_size, free_pages, inactive_pages = _mac_vm_stat_pages()
        avail_mb = (free_pages + inactive_pages) * page_size / (1024 ** 2)
    used_mb = max(total_mb - avail_mb, 0)
    percent = round(used_mb / total_mb * 100, 1) if total_mb else 0.0
    return {"total_mb": round(total_mb), "used_mb": round(used_mb), "percent": percent}


def _mac_df_bytes(path):
    """macOS df: shells out to `df -k` (statvfs reports bogus sizes on NFS)."""
    out = subprocess.check_output(["df", "-k", path], encoding="utf-8")
    fields = out.splitlines()[-1].split()
    total_kb, used_kb, avail_kb = int(fields[1]), int(fields[2]), int(fields[3])
    return total_kb * 1024, avail_kb * 1024, used_kb * 1024


def _disk_stats(path):
    if platform.system() == "Darwin":
        total, free, used = _mac_df_bytes(path)
    else:
        usage = shutil.disk_usage(path)
        total, free, used = usage.total, usage.free, usage.used
    percent = round(used / total * 100, 1) if total else 0.0
    return {
        "path": path,
        "free_gb": round(free / (1024 ** 3), 1),
        "total_gb": round(total / (1024 ** 3), 1),
        "percent_used": percent,
    }


def _local_disk_path():
    """The host's own physical/system disk, as opposed to the shared NAS mount."""
    if platform.system() == "Windows":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


def _disk_pair():
    """Two filesystems: the host's local disk and the shared NAS mount (resolved from NODESTATUS_DIR's real path); identical numbers if they are the same filesystem."""
    nas_path = os.path.realpath(NODESTATUS_DIR)
    return {
        "local": _disk_stats(_local_disk_path()),
        "nas": _disk_stats(nas_path),
    }


def _host_info():
    """This host's host_arch_gating flags and their source (detected / manual map / env) from /testrunnerapp/hostflags.py; {} if no hostflags module."""
    try:
        import hostflags
    except ImportError:
        return {}
    return hostflags.host_info()


def collect_status():
    now = time.time()
    info = _host_info()
    return {
        "hostname": socket.gethostname(),
        "os": _os_description(),
        "cpu_percent": _cpu_percent(),
        "mem": _mem_stats(),
        "disk": _disk_pair(),
        "host_flags": info.get("flags"),
        "host_flags_source": info.get("source"),
        "timestamp": now,
        # UTC, so only a wrong system clock shows as skew
        "system_clock": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
    }


def write_status_once():
    os.makedirs(NODESTATUS_DIR, exist_ok=True)
    status = collect_status()
    dest = os.path.join(NODESTATUS_DIR, f"{status['hostname']}.json")
    tmp = f"{dest}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f)
    try:
        os.replace(tmp, dest)
    except OSError:
        # SMB: os.replace can fail over a stale unmapped dest; clear it and retry as a plain create
        try:
            os.remove(dest)
        except OSError:
            pass
        os.replace(tmp, dest)
    return status


_last_report_error = None


def _report_loop():
    global _last_report_error
    while True:
        try:
            write_status_once()
            if _last_report_error is not None:
                print(f"nodestatus: reporting recovered for {socket.gethostname()}")
                _last_report_error = None
        except Exception as e:
            # log failures; a silent write failure hides the host from the Instances tab
            msg = f"{type(e).__name__}: {e}"
            if msg != _last_report_error:
                print(f"nodestatus: write_status_once failed for "
                      f"{socket.gethostname()}: {msg}")
                _last_report_error = msg
        time.sleep(REPORT_INTERVAL)


def start_reporter_thread():
    """Idempotent: safe to call more than once, only the first call starts a thread."""
    global _reporter_started
    with _reporter_lock:
        if _reporter_started:
            return
        _reporter_started = True
    threading.Thread(target=_report_loop, daemon=True, name="nodestatus-reporter").start()


def _clock_alive(hostname, timestamp):
    """For a record with an untrustworthy timestamp: is the host still rewriting it? Compares against the value seen last time; first sight only sets the baseline. Needs a long-lived module (apiv1._load_nodestatus)."""
    mono = time.monotonic()
    with _observed_lock:
        entry = _observed.get(hostname)
        if entry is None or entry[0] != timestamp:
            _observed[hostname] = [timestamp, mono if entry is not None else None]
            entry = _observed[hostname]
        changed_at = entry[1]
    return changed_at is not None and mono - changed_at <= max(STALE_AFTER, RECHECK_AFTER)


def list_nodes(stale_after=STALE_AFTER):
    """Hosts that are up, by hostname: timestamp within stale_after of now (and at most CLOCK_SKEW_TOLERANCE ahead), or further off but still being rewritten (_clock_alive) -> node_flags ["TIME_ERR"] and age_seconds = clock skew."""
    nodes = []
    now = time.time()
    try:
        names = os.listdir(NODESTATUS_DIR)
    except OSError:
        return nodes
    for name in names:
        if not name.endswith(".json") or ".tmp." in name:
            continue
        try:
            with open(os.path.join(NODESTATUS_DIR, name), encoding="utf-8") as f:
                status = json.load(f)
            ts = status["timestamp"]
            age = now - ts
        except (OSError, ValueError, KeyError):
            continue
        if (age > stale_after or age < -CLOCK_SKEW_TOLERANCE) and not _clock_alive(status.get("hostname", name), ts):
            continue
        status["age_seconds"] = round(age, 1)
        status["node_flags"] = [TIME_ERR] if abs(age) > CLOCK_SKEW_TOLERANCE else []
        nodes.append(status)
    nodes.sort(key=lambda s: s.get("hostname", ""))
    return nodes
