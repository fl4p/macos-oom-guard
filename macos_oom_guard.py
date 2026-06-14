#!/usr/bin/env python3
"""System-wide macOS OOM killer (the one Apple doesn't ship).

WHY THIS EXISTS
---------------
macOS will not OOM-kill your foreground work. It compresses, then grows swapfiles
on the boot disk "forever", on the assumption pressure is transient. On Apple
Silicon the swap *segment* table is finite; when it saturates while a process is
still demanding anonymous pages, the VM deadlocks. WindowServer then misses its
userspace watchdog check-in and the kernel PANIC-REBOOTS the whole machine
(AppleARMWatchdogTimer) rather than let the GUI hang. This tool was born after
eating several of those panics running large data jobs.

Jetsam (the in-kernel killer, `kern.memorystatus_*`) exists but on desktop it only
aggressively kills sandboxed / idle apps; a long-running `python3` (or any heavy
CLI process) launched in a terminal has no jetsam high-watermark and survives right
up to the cliff.

In-process memory guards can't reliably save the box either: they only watch their
own process tree, they tend to lean on `kern.memorystatus_vm_pressure_level` (which,
measured live, stays at 1/NORMAL even in deep swap-death), and their daemon thread
gets CPU/GIL-starved during the exact freeze. This is a separate, self-protecting
process that does work.

DESIGN
------
* Trigger on `kern.memorystatus_level` — a 0..100 "% memory available" gauge that
  jetsam itself trends on and that drops smoothly as memory fills (UNLIKE the
  bucketed pressure level). Secondary trigger: swap used past a multiple of RAM.
  We NEVER trigger on footprint — a healthy box can sit at a huge footprint of
  compressed/sparse data while perfectly green; footprint only RANKS victims.
* Rank victims by `ri_phys_footprint` from proc_pid_rusage — the same metric
  Activity Monitor's "Memory" column and jetsam use. (RSS of a swapped hog
  collapses; outside-measured footprint overcounts — phys_footprint is the honest one.)
* Protect system processes two ways: a name denylist AND a location rule (only ever
  kill executables under /Applications, /Users, /opt/homebrew, /usr/local). So the
  victim is realistically only a browser, a batch compute job, or a similar user app
  — never a system daemon, WindowServer, or the kernel.
* Self-protect: run as root at nice -20, mlockall() so the killer itself never gets
  swapped out when it's needed most. The hot poll loop does ZERO fork/exec and zero
  per-iteration allocation (pure ctypes sysctl); process enumeration happens only
  when we're already near the threshold.

USAGE
-----
  # validate plumbing + see what it WOULD kill right now (safe, read-only):
  python3 macos_oom_guard.py --status

  # watch it in the foreground in dry-run (logs decisions, never kills):
  python3 macos_oom_guard.py --run --dry-run

  # install as a boot-time root LaunchDaemon (armed):
  sudo python3 macos_oom_guard.py --install
  sudo python3 macos_oom_guard.py --uninstall

Tunables (env, also settable in the plist via --install flags):
  OOMG_CRIT_LEVEL   memorystatus_level below which we kill        (default 10)
  OOMG_WARN_LEVEL   level below which we start enumerating/logging (default 25)
  OOMG_SWAP_MULT    swap_used > MULT*RAM (with level<20) also kills (default 1.5)
  OOMG_MIN_VICTIM_GB never kill a process smaller than this footprint (default 1.5)
  OOMG_STRIKES      consecutive trips before killing               (default 2)
  OOMG_POLL_S       poll interval seconds                          (default 1.0)
  OOMG_DRY_RUN      1 = log but never kill                         (default 0)
"""
import argparse
import ctypes
import ctypes.util
import os
import signal
import subprocess
import sys
import time

# --------------------------------------------------------------------------- #
# ctypes plumbing: sysctl + libproc, no fork/exec in the hot path.
# --------------------------------------------------------------------------- #
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

_libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
                               ctypes.c_size_t]
_libc.sysctlbyname.restype = ctypes.c_int

PROC_ALL_PIDS = 1
RUSAGE_INFO_V2 = 2
_libc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
_libc.proc_listpids.restype = ctypes.c_int
_libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_libc.proc_pid_rusage.restype = ctypes.c_int
_libc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
_libc.proc_pidpath.restype = ctypes.c_int


class _xsw_usage(ctypes.Structure):
    _fields_ = [("xsu_total", ctypes.c_uint64),
                ("xsu_avail", ctypes.c_uint64),
                ("xsu_used", ctypes.c_uint64),
                ("xsu_pagesize", ctypes.c_uint32),
                ("xsu_encrypted", ctypes.c_int)]


class _rusage_info_v2(ctypes.Structure):
    # Layout from <libproc.h>/<sys/resource.h>. We only read ri_phys_footprint, but
    # the kernel writes the whole struct so the buffer must be the full V2 size.
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
        ("ri_cpu_time_qos_default", ctypes.c_uint64),
        ("ri_cpu_time_qos_maintenance", ctypes.c_uint64),
        ("ri_cpu_time_qos_background", ctypes.c_uint64),
        ("ri_cpu_time_qos_utility", ctypes.c_uint64),
        ("ri_cpu_time_qos_legacy", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_initiated", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_interactive", ctypes.c_uint64),
    ]


def _sysctl_int(name):
    val = ctypes.c_int()
    sz = ctypes.c_size_t(ctypes.sizeof(val))
    if _libc.sysctlbyname(name.encode(), ctypes.byref(val), ctypes.byref(sz), None, 0) != 0:
        raise OSError(ctypes.get_errno(), f"sysctl {name}")
    return val.value


def _sysctl_u64(name):
    val = ctypes.c_uint64()
    sz = ctypes.c_size_t(ctypes.sizeof(val))
    if _libc.sysctlbyname(name.encode(), ctypes.byref(val), ctypes.byref(sz), None, 0) != 0:
        raise OSError(ctypes.get_errno(), f"sysctl {name}")
    return val.value


def memorystatus_level():
    """0..100 'percent of memory available' as jetsam sees it. Falls smoothly as
    the box fills; this is the trigger signal."""
    return _sysctl_int("kern.memorystatus_level")


def swap_used_gb():
    u = _xsw_usage()
    sz = ctypes.c_size_t(ctypes.sizeof(u))
    if _libc.sysctlbyname(b"vm.swapusage", ctypes.byref(u), ctypes.byref(sz), None, 0) != 0:
        return 0.0
    return u.xsu_used / 1e9


def phys_ram_gb():
    return _sysctl_u64("hw.memsize") / 1e9


def _footprint_gb(pid):
    ri = _rusage_info_v2()
    if _libc.proc_pid_rusage(pid, RUSAGE_INFO_V2, ctypes.byref(ri)) != 0:
        return 0.0
    return ri.ri_phys_footprint / 1e9


def _pid_path(pid, _buf=ctypes.create_string_buffer(4096)):
    n = _libc.proc_pidpath(pid, _buf, 4096)
    return _buf.value.decode("utf-8", "replace") if n > 0 else ""


def _list_pids():
    need = _libc.proc_listpids(PROC_ALL_PIDS, 0, None, 0)
    if need <= 0:
        return []
    count = need // ctypes.sizeof(ctypes.c_int) + 64
    buf = (ctypes.c_int * count)()
    got = _libc.proc_listpids(PROC_ALL_PIDS, 0, buf, ctypes.sizeof(buf))
    if got <= 0:
        return []
    n = got // ctypes.sizeof(ctypes.c_int)
    return [buf[i] for i in range(n) if buf[i] > 1]


# --------------------------------------------------------------------------- #
# victim selection
# --------------------------------------------------------------------------- #
# Never kill these by name, even if they somehow live under a killable prefix.
PROTECT_NAMES = {
    "kernel_task", "launchd", "WindowServer", "loginwindow", "logd",
    "opendirectoryd", "configd", "powerd", "watchdogd", "UserEventAgent",
    "coreaudiod", "hidd", "bluetoothd", "mds", "mds_stores", "mdworker",
    "mdworker_shared", "cfprefsd", "distnoted", "securityd", "trustd", "syslogd",
    "SystemUIServer", "Dock", "Finder", "ControlCenter", "Spotlight",
    "NotificationCenter", "WindowManager", "backboardd", "sysmond",
    "nsurlsessiond", "sshd", "sshd-session", "tccd", "amfid", "diskarbitrationd",
    "fseventsd", "revisiond", "corebrightnessd", "sharingd", "bird",
}
# Only executables under these prefixes are ever eligible. This is the strong
# guarantee that we touch user apps (Chrome, a venv python) and never a system
# daemon, no matter what the name heuristic misses.
KILLABLE_PREFIXES = ("/Applications/", "/Users/", "/opt/homebrew/", "/usr/local/")


def _basename(path):
    # the leaf, then strip a trailing " (Renderer)"-style suffix-free comm; path leaf
    return path.rsplit("/", 1)[-1] if path else ""


def find_victim(min_gb, exclude_pids):
    """Return (pid, footprint_gb, path) of the biggest eligible hog, or (None, 0, '')."""
    best_pid, best_gb, best_path = None, 0.0, ""
    for pid in _list_pids():
        if pid in exclude_pids:
            continue
        path = _pid_path(pid)
        if not path or not path.startswith(KILLABLE_PREFIXES):
            continue
        if _basename(path) in PROTECT_NAMES:
            continue
        gb = _footprint_gb(pid)
        if gb > best_gb:
            best_pid, best_gb, best_path = pid, gb, path
    if best_pid is not None and best_gb >= min_gb:
        return best_pid, best_gb, best_path
    return None, best_gb, best_path


# --------------------------------------------------------------------------- #
# daemon
# --------------------------------------------------------------------------- #
class Cfg:
    def __init__(self):
        self.crit_level = int(os.environ.get("OOMG_CRIT_LEVEL", 10))
        self.warn_level = int(os.environ.get("OOMG_WARN_LEVEL", 25))
        self.swap_mult = float(os.environ.get("OOMG_SWAP_MULT", 1.5))
        self.min_victim_gb = float(os.environ.get("OOMG_MIN_VICTIM_GB", 1.5))
        self.strikes = int(os.environ.get("OOMG_STRIKES", 2))
        self.poll_s = float(os.environ.get("OOMG_POLL_S", 1.0))
        self.dry_run = os.environ.get("OOMG_DRY_RUN", "0") not in ("0", "", "false", "False")


def _log(fh, msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    fh.write(line)
    fh.flush()


def _self_protect():
    """Pin priority high and lock our pages so we survive the thrash we police."""
    try:
        os.nice(-20)
    except Exception:
        pass
    try:
        MCL_CURRENT, MCL_FUTURE = 1, 2
        _libc.mlockall(MCL_CURRENT | MCL_FUTURE)
    except Exception:
        pass


def run(cfg, logfile="/var/log/macos-oom-guard.log"):
    try:
        fh = open(logfile, "a", buffering=1)
    except PermissionError:
        fh = sys.stderr
    ram = phys_ram_gb()
    swap_kill = cfg.swap_mult * ram
    exclude = {os.getpid(), os.getppid()}
    _self_protect()
    _log(fh, f"[oom-guard] started pid={os.getpid()} ram={ram:.0f}GB "
             f"crit_level<{cfg.crit_level} warn<{cfg.warn_level} "
             f"swap_kill>{swap_kill:.0f}GB min_victim={cfg.min_victim_gb}GB "
             f"strikes={cfg.strikes} dry_run={cfg.dry_run}")
    strikes = 0
    last_heartbeat = 0.0
    while True:
        time.sleep(cfg.poll_s)
        try:
            level = memorystatus_level()
            swap = swap_used_gb()
        except OSError:
            continue
        now = time.monotonic()
        level_trip = level < cfg.crit_level
        swap_trip = swap > swap_kill and level < 20
        tripped = level_trip or swap_trip

        if level < cfg.warn_level or tripped:
            why = "level" if level_trip else ("swap" if swap_trip else "warn")
            _log(fh, f"[oom-guard] {why}: memorystatus_level={level} swap_used={swap:.1f}GB "
                     f"strikes={strikes}/{cfg.strikes}")
        elif now - last_heartbeat > 60:
            last_heartbeat = now
            _log(fh, f"[oom-guard] ok: level={level} swap={swap:.1f}GB")

        if not tripped:
            strikes = 0
            continue
        strikes += 1
        if strikes < cfg.strikes:
            continue

        pid, gb, path = find_victim(cfg.min_victim_gb, exclude)
        if pid is None:
            _log(fh, f"[oom-guard] TRIP but no eligible victim >= {cfg.min_victim_gb}GB "
                     f"(biggest seen {gb:.1f}GB {path or '-'}); holding")
            strikes = 0
            continue
        if cfg.dry_run:
            _log(fh, f"[oom-guard] DRY-RUN would SIGKILL pid={pid} {gb:.1f}GB {path}")
            strikes = 0
            continue
        _log(fh, f"[oom-guard] KILLING pid={pid} {gb:.1f}GB {path} "
                 f"(level={level} swap={swap:.1f}GB)")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as e:
            _log(fh, f"[oom-guard] kill failed pid={pid}: {e}")
        strikes = 0


def status():
    ram = phys_ram_gb()
    level = memorystatus_level()
    swap = swap_used_gb()
    print(f"physical RAM        : {ram:.1f} GB")
    print(f"memorystatus_level  : {level}   (0..100, % available; trigger when < crit)")
    print(f"swap used           : {swap:.1f} GB")
    print()
    # show the top eligible candidates and the would-be victim
    rows = []
    me = {os.getpid(), os.getppid()}
    for pid in _list_pids():
        if pid in me:
            continue
        path = _pid_path(pid)
        if not path or not path.startswith(KILLABLE_PREFIXES):
            continue
        if _basename(path) in PROTECT_NAMES:
            continue
        gb = _footprint_gb(pid)
        if gb >= 0.5:
            rows.append((gb, pid, path))
    rows.sort(reverse=True)
    print("top eligible (killable) processes by phys_footprint:")
    for gb, pid, path in rows[:12]:
        print(f"  {gb:6.1f} GB  pid {pid:>6}  {path}")
    if rows:
        gb, pid, path = rows[0]
        print(f"\n-> would kill first: pid {pid} ({gb:.1f} GB) {path}")
    else:
        print("  (none >= 0.5 GB)")


PLIST_LABEL = "io.github.fl4p.macos-oom-guard"
PLIST_PATH = f"/Library/LaunchDaemons/{PLIST_LABEL}.plist"


def _plist(python_bin, script_path, cfg):
    env = {
        "OOMG_CRIT_LEVEL": str(cfg.crit_level),
        "OOMG_WARN_LEVEL": str(cfg.warn_level),
        "OOMG_SWAP_MULT": str(cfg.swap_mult),
        "OOMG_MIN_VICTIM_GB": str(cfg.min_victim_gb),
        "OOMG_STRIKES": str(cfg.strikes),
        "OOMG_POLL_S": str(cfg.poll_s),
        "OOMG_DRY_RUN": "1" if cfg.dry_run else "0",
    }
    env_xml = "".join(f"    <key>{k}</key><string>{v}</string>\n" for k, v in env.items())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python_bin}</string>
    <string>{script_path}</string>
    <string>--run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>Nice</key><integer>-20</integer>
  <key>StandardOutPath</key><string>/var/log/macos-oom-guard.log</string>
  <key>StandardErrorPath</key><string>/var/log/macos-oom-guard.log</string>
  <key>EnvironmentVariables</key>
  <dict>
{env_xml}  </dict>
</dict>
</plist>
"""


def install(cfg):
    if os.geteuid() != 0:
        sys.exit("install requires root: re-run with sudo")
    python_bin = sys.executable
    script_path = os.path.abspath(__file__)
    with open(PLIST_PATH, "w") as f:
        f.write(_plist(python_bin, script_path, cfg))
    os.chmod(PLIST_PATH, 0o644)
    # list-form subprocess (no shell); PLIST_PATH is a module constant, not user input.
    subprocess.run(["launchctl", "bootout", "system", PLIST_PATH],
                   stderr=subprocess.DEVNULL, check=False)
    rc = subprocess.run(["launchctl", "bootstrap", "system", PLIST_PATH], check=False).returncode
    print(f"installed {PLIST_PATH} (bootstrap rc={rc}); dry_run={cfg.dry_run}")
    print("tail -f /var/log/macos-oom-guard.log")


def uninstall():
    if os.geteuid() != 0:
        sys.exit("uninstall requires root: re-run with sudo")
    subprocess.run(["launchctl", "bootout", "system", PLIST_PATH],
                   stderr=subprocess.DEVNULL, check=False)
    try:
        os.remove(PLIST_PATH)
    except FileNotFoundError:
        pass
    print(f"removed {PLIST_PATH}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run", action="store_true", help="run the daemon loop")
    g.add_argument("--status", action="store_true", help="print readings + would-be victim, exit")
    g.add_argument("--install", action="store_true", help="install boot-time root LaunchDaemon")
    g.add_argument("--uninstall", action="store_true", help="remove the LaunchDaemon")
    ap.add_argument("--dry-run", action="store_true", help="log decisions but never kill")
    args = ap.parse_args()

    if args.dry_run:
        os.environ["OOMG_DRY_RUN"] = "1"
    cfg = Cfg()

    if args.status:
        status()
    elif args.install:
        install(cfg)
    elif args.uninstall:
        uninstall()
    elif args.run:
        # foreground --run without root still works (just no mlockall/nice); logs to
        # stderr if it can't open /var/log.
        run(cfg)


if __name__ == "__main__":
    main()
