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
* Kill the family, not just the child: a harness that runs jobs in a pool (a
  ThreadPoolExecutor around subprocess.run, a multiprocessing pool, a browser) starts
  a fresh worker for every one we kill, and the fresh one regrows. A second,
  DIFFERENT victim from the same process group within OOMG_FAMILY_WINDOW_S kills
  the whole group. Never a group with an app bundle in it (browsers, IDEs
  and Electron apps keep all their helpers in one group), nor one with a system process
  other than a job's own wrappers and pipe stages (see group_kill_refusal).
  After a kill the guard waits while the victim is visibly releasing memory
  (footprint falling, at most WAIT_CAP_S, never at panic level) instead of
  killing the next-biggest process meanwhile; a victim that stays resident is
  then excluded rather than re-signalled.
* Self-protect: run as root at nice -20. There is NO page lock: macOS does not
  implement mlockall() (ENOSYS, measured 2026-10-04), so the guard's own pages are
  as compressible and swappable as anyone's. The hot poll loop does ZERO fork/exec
  and zero per-iteration allocation (pure ctypes sysctl); process enumeration
  happens only when we're already near the threshold.

USAGE
-----
  # validate plumbing + see what it WOULD kill right now (safe, read-only):
  python3 macos_oom_guard.py --status

  # watch it in the foreground in dry-run (logs decisions, never kills):
  python3 macos_oom_guard.py --run --dry-run

  # install as a boot-time root LaunchDaemon (armed). This copies the script to
  # /usr/local/libexec/macos-oom-guard/ and runs it with /usr/bin/python3, so the
  # daemon never executes a user-writable file; re-run after every change here:
  sudo /usr/bin/python3 macos_oom_guard.py --install
  sudo /usr/bin/python3 macos_oom_guard.py --uninstall

Tunables (env, also settable in the plist via --install flags):
  OOMG_PANIC_LEVEL  level below which we kill on the FIRST sample  (default 5)
  OOMG_CRIT_LEVEL   memorystatus_level below which we kill        (default 10)
  OOMG_WARN_LEVEL   level below which we start enumerating/logging (default 25)
  OOMG_SWAP_MULT    swap_used > MULT*RAM (with level<20) also kills (default 1.0)
  OOMG_SWAP_RATE_GB swap growth over the rate window that trips     (default 8.0)
  OOMG_SWAP_RATE_S  trailing window the growth is measured over     (default 6.0)
  OOMG_MIN_VICTIM_GB never kill a process smaller than this footprint (default 1.5)
  OOMG_STRIKES      trips before killing (leaky, not reset)        (default 2)
  OOMG_STRIKE_DECAY leak per clean sample; must be < 1             (default 0.5)
  OOMG_POLL_S       poll interval seconds                          (default 1.0)
  OOMG_FAMILY_WINDOW_S a repeat victim's process group within this many
                    seconds is killed whole; 0 disables             (default 300)
  OOMG_DRY_RUN      1 = log but never kill                         (default 0)

REVISION 2026-08-27 — three defects, found by reading 82317 samples of this
guard's own log (2026-06-14 .. 2026-08-27) after it sat silent through a kernel
panic. Corrected here; see `test_macos_oom_guard.py` for the calibration.

* The swap arm could not fire. `1.5 * RAM` is 58.5 GB on this 39 GB host and the
  highest swap ever recorded in 82317 samples is 40.2 GB, so the trigger was
  unreachable by construction and had never fired once. But simply lowering it
  is wrong too: swap at 0.75*RAM would have tripped 2316 times on a box that was
  perfectly healthy — 40 GB of swap at level 33 is a NORMAL state here. Absolute
  swap does not discriminate. The absolute arm is now a *reachable* last-resort
  backstop at 1.0*RAM, and the discrimination is done by a new growth-rate arm.
* Any single non-tripping sample reset `strikes` to zero, so a burst that
  oscillated across the threshold never accumulated. Strikes are now a leaky
  bucket that drains SLOWER than it fills (0.5 per clean sample against 1.0 per
  trip) — draining one-for-one looks like a fix and is not, because it cancels
  an alternating sequence exactly and still never arms. A level below
  `panic_level` bypasses the bucket entirely:
  requiring two seconds of confirmation at 5% memory available is how you arrive
  at the cliff with a well-confirmed diagnosis and a dead machine.
* The once-a-minute heartbeat logged an instantaneous reading, so every
  intra-minute excursion was invisible and none of the above was diagnosable
  from the log. The heartbeat now carries the worst level and peak swap seen
  since the previous one.

NOT FIXED, and it is the one that actually silenced the guard on 2026-08-26: the
loop stopped emitting its unconditional heartbeat at 23:08:09 and the panic came
around 23:11 — it was starved out despite root and nice -20 (the mlockall it was
also credited with never took effect; see below). No threshold change helps a
guard that is not being scheduled. A process on the contended resource cannot be
the arbiter for it; that needs a kernel mechanism (jetsam high-watermark, a
memory cgroup) or another machine.

REVISION 2026-10-04 — the guard fired five times and the host still panicked
(watchdog timeout, compressor at 100% of its segment limit).

* A benchmark ran 8 workers through a ThreadPoolExecutor around subprocess.run.
  The guard SIGKILLed 17-26 GB workers at 20:38:05, :29, 20:39:01, :22 and :51,
  and each time the pool started the next case, which regrew in ~20 s. Killing
  the biggest single process cannot win against a respawning parent that is
  itself tiny (it never reaches min_victim). A second victim from one process
  group within the window now kills the group.
* _self_protect called mlockall(MCL_CURRENT | MCL_FUTURE) through ctypes inside a
  try/except. macOS does not implement it: it returns -1 with errno ENOSYS, which
  ctypes does not raise, so the failure was silent from day one and the guard's
  pages were always pageable. The call is gone and the log says what protection
  is actually in place.
* Still open: the guard itself was SIGKILLed at ~20:39:55 (no traceback; jetsam
  was killing for vm-compressor-space-shortage at 20:39:57), and the launchd
  restart logged one sample at 20:40:12 and was never scheduled again. A restart
  also loses the kill history, so the first kill after it is a single one again.
"""
import argparse
import collections
import ctypes
import ctypes.util
import os
import re
import signal
import stat
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
PROC_PGRP_ONLY = 2
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


def _list_pids(kind=PROC_ALL_PIDS, arg=0, complete=False):
    """Pids from proc_listpids. With `complete`, None unless the list is whole.

    A buffer the kernel filled to the brim may have been cut short; the +64 slack
    makes that rare for a full scan, where a missed pid is harmless, but a group
    kill must not be decided on a list that might be missing a member.
    """
    unknown = None if complete else []
    need = _libc.proc_listpids(kind, arg, None, 0)
    if need < 0:
        return unknown
    if need == 0:
        return []
    count = need // ctypes.sizeof(ctypes.c_int) + 64
    buf = (ctypes.c_int * count)()
    got = _libc.proc_listpids(kind, arg, buf, ctypes.sizeof(buf))
    if got < 0 or (complete and got >= ctypes.sizeof(buf)):
        return unknown
    if got == 0:
        return []
    n = got // ctypes.sizeof(ctypes.c_int)
    return [buf[i] for i in range(n) if buf[i] > 1]


def _group_members(pgid):
    """Live pids in process group `pgid`, or None if the list is not known whole."""
    return _list_pids(PROC_PGRP_ONLY, pgid, complete=True)


def _pgid(pid):
    """Process group of `pid`, or None if it is gone or unreadable."""
    try:
        return os.getpgid(pid)
    except OSError:
        return None


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


# A job's own wrapper processes live in its group but outside KILLABLE_PREFIXES:
# the shell an agent or a script runs the job under, and the launchers in front
# of it. They may die with the job. Anything else from the system means the
# group is not one job, and it is never killed as a whole.
JOB_WRAPPERS = {
    "/bin/sh", "/bin/bash", "/bin/zsh", "/bin/dash", "/bin/ksh", "/bin/csh", "/bin/tcsh",
    "/usr/bin/env", "/usr/bin/nice", "/usr/bin/nohup", "/usr/bin/time",
    "/usr/bin/xargs", "/usr/bin/caffeinate",
    # pipeline stages share the job's group: `job | tail`, `job | tee log`
    "/bin/cat", "/bin/sleep", "/usr/bin/tail", "/usr/bin/head", "/usr/bin/tee",
    "/usr/bin/grep", "/usr/bin/sed", "/usr/bin/awk", "/usr/bin/sort", "/usr/bin/uniq",
    "/usr/bin/wc", "/usr/bin/cut", "/usr/bin/tr",
}

#: An installed app runs its helpers in its own group (a browser and
#: its renderers; an IDE; an Electron app). Two kills there
#: are two hogs in a long-lived app, not a pool refilling itself. Only bundles
#: under an Applications folder count (at any depth, e.g. /Applications/KiCad/
#: KiCad.app), never one elsewhere: Homebrew's framework python runs as
#: .../Python.framework/.../Python.app/Contents/MacOS/Python and is an
#: interpreter, not an app. A python.app under /Applications (miniconda) does
#: count, which only makes the group kill more conservative.
_APP_BUNDLE = re.compile(r"^(/System)?(/Users/[^/]+)?/Applications/([^/]+/)*?[^/]+\.app/Contents/")
#: The framework python launcher, as the kernel reports it (measured: Homebrew
#: .../Python.framework/..., Xcode's /usr/bin/python3 .../Python3.framework/...).
_INTERPRETER = re.compile(
    r"/Python3?\.framework/Versions/[^/]+/Resources/Python\.app/Contents/MacOS/Python$")


def _is_app(path):
    """True for an executable inside an installed app, python launchers excepted."""
    return bool(_APP_BUNDLE.match(path)) and not _INTERPRETER.search(path)


#: A pid we SIGKILLed stays resident while it tears down (a 21 GB victim was
#: still listed 6 s after its kill on 2026-10-04, though the level recovered in
#: 1-2 s). Wait at most this long, and only while its footprint keeps falling.
WAIT_CAP_S = 4.0
#: How long a killed-but-resident victim is kept out of victim selection.
#: Signalling it again does nothing; picking it again would stall the guard.
RESIDENT_EXCLUDE_S = 30.0


def repeat_offender(pgid, pid, kills, now, window_s):
    """True if a DIFFERENT victim from group `pgid` was killed within `window_s`.

    `kills` holds (monotonic time, pgid, pid) per kill. The same pid again is
    not a repeat: it is the first victim still exiting (or never dying, in
    dry-run). An unknown group (None) is never a repeat: it cannot be matched,
    and must not escalate on a guess.
    """
    if pgid is None or window_s <= 0:
        return False
    return any(k_pgid == pgid and k_pid != pid and now - when <= window_s
               for when, k_pgid, k_pid in kills)


def still_exiting(kills, now, min_gb, footprint=None, grace_s=RESIDENT_EXCLUDE_S):
    """{pid: (gb, seconds since kill)} of victims killed within `grace_s` that
    still hold >= min_gb. Footprint, not liveness, decides: a zombie is alive
    with nothing resident."""
    footprint = footprint or _footprint_gb
    out = {}
    for when, _pgid, pid in kills:
        if now - when <= grace_s and pid not in out:
            gb = footprint(pid)
            if gb >= min_gb:
                out[pid] = (gb, now - when)
    return out


def should_wait(resident, prev, why, level=None, prev_level=None, cap_s=WAIT_CAP_S):
    """Hold this kill because a victim's memory is on its way back?

    Waiting beats the two alternatives: skipping the dying pid (an earlier version) killed
    the next-biggest process -- the browser, the IDE -- every second while
    memory was already returning, and re-picking it stalls on a pid that is
    already dead. But waiting is only right while it is actually returning, so:
    never at panic level, only for a victim killed within `cap_s`, and only
    while there is evidence of recovery since the previous kill poll: the
    victim's footprint fell (`prev`, {pid: gb}; a victim not seen yet counts as
    falling, which buys it one poll), or memorystatus_level rose. A teardown
    can sit flat for a poll while the system is still recovering.
    """
    if why == "panic":
        return False
    rising = level is not None and prev_level is not None and level > prev_level
    return any(age <= cap_s and (rising or pid not in prev or gb < prev[pid])
               for pid, (gb, age) in resident.items())


def _alive(pid):
    """False only when the pid is provably gone; any doubt is 'alive'."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def read_group(pgid):
    """[(pid, path)] of group `pgid`, minus members that exited mid-read.

    A member whose path is unreadable but which is still alive stays in, with
    an empty path, so group_kill_refusal refuses on it. None if the member list
    itself could not be read whole.
    """
    pids = _group_members(pgid)
    if pids is None:
        return None
    members = []
    for pid in pids:
        path = _pid_path(pid)
        if path or _alive(pid):
            members.append((pid, path))
    return members


def group_kill_refusal(pgid, members, protect_pids, own_pgids):
    """Why process group `pgid` must not be killed whole, or None if it may.

    `members` is [(pid, executable path)] as read at decision time. Every way of
    not knowing is a refusal, never a pass: the fallback is the single kill the
    guard did before, so refusing costs one more round, while a wrong group
    kill costs whatever else was in the group.
    """
    if pgid is None or pgid <= 1:
        return f"no usable group ({pgid})"
    if pgid in own_pgids:
        return "the guard's own group"
    if members is None:
        return "member list unreadable or possibly truncated"
    if not members:
        return "group has no live members"
    for pid, path in members:
        if pid in protect_pids:
            return f"group contains protected pid {pid}"
        if not path:
            return f"member {pid} has no readable path"
        if _basename(path) in PROTECT_NAMES:
            return f"member {pid} is protected ({_basename(path)})"
        if _is_app(path):
            return f"member {pid} is an app ({path})"
        if not path.startswith(KILLABLE_PREFIXES) and path not in JOB_WRAPPERS:
            return f"member {pid} is a system process ({path})"
    return None


def kill_victim(pid, pgid, whole_group):
    """SIGKILL the victim, or its whole process group.

    Returns (signalled, error): `signalled` is False only when the victim could
    not be signalled at all, so the caller does not wait on a kill that never
    happened. The victim is killed by pid as well, so it dies even if it left
    the group between the decision and the signal.
    """
    errors, group_ok, pid_ok = [], False, True
    if whole_group:
        try:
            os.killpg(pgid, signal.SIGKILL)
            group_ok = True
        except ProcessLookupError:
            pass
        except Exception as e:
            errors.append(f"killpg({pgid}) failed: {e}")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception as e:
        errors.append(f"kill({pid}) failed: {e}")
        pid_ok = False
    return pid_ok or group_ok, "; ".join(errors) or None


# --------------------------------------------------------------------------- #
# daemon
# --------------------------------------------------------------------------- #
#: level below which the absolute swap arm is allowed to fire. Swap alone does
#: not mean distress on this host — 40 GB at level 33 is a normal working state.
SWAP_LEVEL_GATE = 20


class Cfg:
    def __init__(self):
        self.panic_level = int(os.environ.get("OOMG_PANIC_LEVEL", 5))
        self.crit_level = int(os.environ.get("OOMG_CRIT_LEVEL", 10))
        self.warn_level = int(os.environ.get("OOMG_WARN_LEVEL", 25))
        self.swap_mult = float(os.environ.get("OOMG_SWAP_MULT", 1.0))
        self.swap_rate_gb = float(os.environ.get("OOMG_SWAP_RATE_GB", 8.0))
        self.swap_rate_s = float(os.environ.get("OOMG_SWAP_RATE_S", 6.0))
        self.min_victim_gb = float(os.environ.get("OOMG_MIN_VICTIM_GB", 1.5))
        self.strikes = int(os.environ.get("OOMG_STRIKES", 2))
        self.strike_decay = float(os.environ.get("OOMG_STRIKE_DECAY", 0.5))
        self.poll_s = float(os.environ.get("OOMG_POLL_S", 1.0))
        self.family_window_s = float(os.environ.get("OOMG_FAMILY_WINDOW_S", 300.0))
        self.dry_run = os.environ.get("OOMG_DRY_RUN", "0") not in ("0", "", "false", "False")


#: One poll's verdict. `action` is "clear", "arm" or "kill"; `why` names the arm
#: that tripped, or the reason no verdict could be reached.
Verdict = collections.namedtuple("Verdict", "strikes action why")


def swap_growth_gb(history, window_s, now):
    """GB of swap added over the trailing window, or None if not evaluable.

    None means "cannot tell" and must never be read as "no growth". It happens
    on a cold start, and — importantly — after the loop has been stalled: two
    samples 180 s apart say nothing about a 6 s window, and pretending they do
    would manufacture an enormous fake growth out of a gap. The caller falls
    back to the absolute arms and logs that this one abstained.
    """
    inside = [(when, value) for when, value in history if now - when <= window_s]
    if len(inside) < 2:
        return None
    span = inside[-1][0] - inside[0][0]
    if span < window_s * 0.5:
        return None
    return inside[-1][1] - inside[0][1]


def evaluate(level, swap, growth, strikes, cfg, swap_kill):
    """Decide on one sample. Pure, so the calibration is testable.

    Three arms, cheapest first. `growth` is the value from swap_growth_gb and
    may be None; None never trips and never clears anything else.
    """
    if level < cfg.panic_level:
        # Severity outranks persistence. Confirming this over two more polls is
        # not caution, it is the extra two seconds that loses the machine.
        return Verdict(0.0, "kill", "panic")
    if level < cfg.crit_level:
        why = "level"
    elif swap > swap_kill and level < SWAP_LEVEL_GATE:
        why = "swap"
    elif growth is not None and growth > cfg.swap_rate_gb and level < cfg.warn_level:
        why = "rate"
    else:
        # Leak, do not reset. A burst that oscillates across the threshold is
        # more dangerous than one that sits on it, and a reset made exactly that
        # case unreachable. Note the leak must be SLOWER than the fill or the
        # fix is cosmetic: at one-for-one, an alternating trip/clear sequence
        # cancels exactly and still never arms. At the default 0.5 a duty cycle
        # above one-in-three accumulates and anything below it drains.
        return Verdict(max(0.0, strikes - cfg.strike_decay), "clear", None)
    strikes += 1.0
    if strikes < cfg.strikes:
        return Verdict(strikes, "arm", why)
    return Verdict(0.0, "kill", why)


def _log(fh, msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    fh.write(line)
    fh.flush()


def _self_protect():
    """Pin our priority high; return what protection is actually in place.

    No page locking: macOS's mlockall() returns ENOSYS (measured 2026-10-04), and
    the old call to it failed silently since ctypes does not raise on -1.
    """
    try:
        os.nice(-20)
    except OSError as e:
        return f"nice failed ({e}); pages unlocked (no mlockall on macOS)"
    return f"nice={os.getpriority(os.PRIO_PROCESS, 0)}; pages unlocked (no mlockall on macOS)"


def run(cfg, logfile="/var/log/macos-oom-guard.log"):
    try:
        fh = open(logfile, "a", buffering=1)
    except PermissionError:
        fh = sys.stderr
    ram = phys_ram_gb()
    swap_kill = cfg.swap_mult * ram
    exclude = {os.getpid(), os.getppid()}
    own_pgids = {os.getpgrp(), _pgid(os.getppid())}
    protection = _self_protect()
    _log(fh, f"[oom-guard] started pid={os.getpid()} ram={ram:.0f}GB "
             f"panic_level<{cfg.panic_level} crit_level<{cfg.crit_level} "
             f"warn<{cfg.warn_level} swap_kill>{swap_kill:.0f}GB "
             f"swap_rate>{cfg.swap_rate_gb:.0f}GB/{cfg.swap_rate_s:.0f}s "
             f"min_victim={cfg.min_victim_gb}GB "
             f"strikes={cfg.strikes} family_window={cfg.family_window_s:.0f}s "
             f"dry_run={cfg.dry_run} self_protect=[{protection}]")
    strikes = 0.0
    # (monotonic time, pgid, pid) of recent victims: a second one from the same group
    # inside the family window means something keeps refilling it.
    kills = collections.deque(maxlen=64)
    prev_resident = {}  # {pid: gb} of killed victims at the previous kill poll
    prev_kill_level = None  # memorystatus_level at the previous kill poll
    last_heartbeat = 0.0
    # The old heartbeat logged an instantaneous reading once a minute, which hid
    # every intra-minute excursion — including all of the ones that preceded a
    # reboot. Carry the worst of the interval instead.
    worst_level, peak_swap = 100, 0.0
    history = collections.deque(maxlen=4096)
    last_poll = None
    while True:
        time.sleep(cfg.poll_s)
        try:
            level = memorystatus_level()
            swap = swap_used_gb()
        except OSError:
            continue
        now = time.monotonic()
        # A gap far beyond the poll interval means we were not scheduled. That
        # is itself the failure mode this guard has, so say so rather than
        # letting it vanish into a quiet log.
        if last_poll is not None and now - last_poll > max(5.0, cfg.poll_s * 5):
            _log(fh, f"[oom-guard] STALLED: {now - last_poll:.1f}s between polls "
                     f"(interval {cfg.poll_s}s) — the guard was not scheduled")
        last_poll = now
        history.append((now, swap))
        while history and now - history[0][0] > cfg.swap_rate_s * 2:
            history.popleft()
        growth = swap_growth_gb(history, cfg.swap_rate_s, now)
        worst_level = min(worst_level, level)
        peak_swap = max(peak_swap, swap)

        verdict = evaluate(level, swap, growth, strikes, cfg, swap_kill)
        strikes = verdict.strikes

        if level < cfg.warn_level or verdict.action != "clear":
            rate = "n/a" if growth is None else f"{growth:+.1f}GB/{cfg.swap_rate_s:.0f}s"
            _log(fh, f"[oom-guard] {verdict.why or 'warn'}: memorystatus_level={level} "
                     f"swap_used={swap:.1f}GB rate={rate} "
                     f"strikes={strikes:.1f}/{cfg.strikes}")
        elif now - last_heartbeat > 60:
            last_heartbeat = now
            _log(fh, f"[oom-guard] ok: level={level} swap={swap:.1f}GB "
                     f"(worst level={worst_level} peak swap={peak_swap:.1f}GB)")
            worst_level, peak_swap = 100, 0.0

        if verdict.action != "kill":
            continue

        victims_exclude = exclude
        if not cfg.dry_run:
            # (a dry-run victim never dies: waiting on it or skipping it would
            # only make the log diverge from what a live guard would do)
            resident = still_exiting(kills, now, cfg.min_victim_gb)
            wait = should_wait(resident, prev_resident, verdict.why, level, prev_kill_level)
            prev_resident = {p: gb for p, (gb, _age) in resident.items()}
            prev_kill_level = level
            if wait:
                _log(fh, "[oom-guard] waiting for killed " + ", ".join(
                    f"pid={p} ({gb:.1f}GB, {age:.0f}s)" for p, (gb, age) in resident.items())
                    + f" to free memory (level={level})")
                # Stay armed: evaluate() spent the strikes on this kill verdict,
                # and re-arming would make every wait two polls long (the 4 s cap
                # became ~6 s, and "falling" was judged over 2 s).
                strikes = max(strikes, cfg.strikes - 1)
                continue
            victims_exclude = exclude | set(resident)
        pid, gb, path = find_victim(cfg.min_victim_gb, victims_exclude)
        if pid is None:
            _log(fh, f"[oom-guard] TRIP but no eligible victim >= {cfg.min_victim_gb}GB "
                     f"(biggest seen {gb:.1f}GB {path or '-'}); holding")
            strikes = 0
            continue
        pgid = _pgid(pid)
        whole_group = repeat_offender(pgid, pid, kills, now, cfg.family_window_s)
        target = f"pid={pid}"
        if whole_group:
            members = read_group(pgid)
            refusal = group_kill_refusal(pgid, members, exclude, own_pgids)
            if refusal:
                _log(fh, f"[oom-guard] repeat victim from pgid={pgid} but group kill "
                         f"refused: {refusal}; killing the pid alone")
                whole_group = False
            else:
                names = sorted({_basename(p) for _m, p in members})
                target = (f"GROUP pgid={pgid} ({len(members)} procs: "
                          f"{', '.join(names[:8])}) incl. pid={pid}")
        if cfg.dry_run:
            _log(fh, f"[oom-guard] DRY-RUN would SIGKILL {target} {gb:.1f}GB {path}")
            kills.append((now, pgid, pid))
            strikes = 0
            continue
        _log(fh, f"[oom-guard] KILLING {target} {gb:.1f}GB {path} "
                 f"(why={verdict.why} level={level} swap={swap:.1f}GB)")
        signalled, error = kill_victim(pid, pgid, whole_group)
        if error:
            _log(fh, f"[oom-guard] kill failed: {error}")
        if signalled:
            kills.append((now, pgid, pid))
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
# The daemon runs as root, so neither the script nor the interpreter it runs may be
# writable by a user: --install copies the script here, and the plist names the
# SIP-protected system python (the guard is stdlib-only and runs on 3.9). That shim
# resolves through xcode-select, and Xcode.app's tools refuse to run until each new
# Xcode version's license is accepted, which would leave the guard dead at boot; the
# Command Line Tools need no license, so the plist pins DEVELOPER_DIR to them.
INSTALL_DIR = "/usr/local/libexec/macos-oom-guard"
INSTALL_SCRIPT = f"{INSTALL_DIR}/macos_oom_guard.py"
DAEMON_PYTHON = "/usr/bin/python3"
DAEMON_DEVELOPER_DIR = "/Library/Developer/CommandLineTools"


def _plist(python_bin, script_path, cfg):
    env = {
        "OOMG_PANIC_LEVEL": str(cfg.panic_level),
        "OOMG_CRIT_LEVEL": str(cfg.crit_level),
        "OOMG_WARN_LEVEL": str(cfg.warn_level),
        "OOMG_SWAP_MULT": str(cfg.swap_mult),
        "OOMG_SWAP_RATE_GB": str(cfg.swap_rate_gb),
        "OOMG_SWAP_RATE_S": str(cfg.swap_rate_s),
        "OOMG_MIN_VICTIM_GB": str(cfg.min_victim_gb),
        "OOMG_STRIKES": str(cfg.strikes),
        "OOMG_STRIKE_DECAY": str(cfg.strike_decay),
        "OOMG_POLL_S": str(cfg.poll_s),
        "OOMG_FAMILY_WINDOW_S": str(cfg.family_window_s),
        "OOMG_DRY_RUN": "1" if cfg.dry_run else "0",
        "DEVELOPER_DIR": DAEMON_DEVELOPER_DIR,
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


def _require_root_only(path):
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
        sys.exit(f"{path} is not a root-owned directory, or is group/world-writable; refusing")


def _copy_root_owned(src, dst_dir, dst):
    """Copy src to dst, both dst and its directory owned by root and not writable by
    anyone else. Refuses an existing directory that a user could write to."""
    # Every ancestor too: a user who can write /usr/local (Homebrew on Intel owns it)
    # can rename libexec away and put their own tree in its place after the check.
    # Check the existing ones before creating anything, and the whole chain after.
    chain = [dst_dir]
    while chain[-1] != "/":
        chain.append(os.path.dirname(chain[-1]))
    for d in chain:
        if os.path.lexists(d):
            _require_root_only(d)
    os.makedirs(dst_dir, mode=0o755, exist_ok=True)
    for d in chain:
        _require_root_only(d)
    if os.path.realpath(src) != os.path.realpath(dst):
        with open(src, "rb") as f:
            data = f.read()
        tmp = f"{dst}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
    os.chown(dst, 0, 0)
    os.chmod(dst, 0o644)


def _check_daemon_python():
    """Fail the install, not the boot: run the plist's interpreter in the plist's
    environment and require a python that can run this module."""
    env = {"PATH": "/usr/bin:/bin", "DEVELOPER_DIR": DAEMON_DEVELOPER_DIR}
    try:
        out = subprocess.run([DAEMON_PYTHON, "-c", "import sys; print(sys.version_info >= (3, 9))"],
                             env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        sys.exit(f"{DAEMON_PYTHON} with DEVELOPER_DIR={DAEMON_DEVELOPER_DIR} did not run: {e}")
    if out.returncode != 0 or out.stdout.strip() != "True":
        sys.exit(f"{DAEMON_PYTHON} with DEVELOPER_DIR={DAEMON_DEVELOPER_DIR} is unusable "
                 f"(rc={out.returncode}): {out.stderr.strip()[-300:]}\n"
                 "install the Command Line Tools: xcode-select --install")


def install(cfg):
    if os.geteuid() != 0:
        sys.exit("install requires root: re-run with sudo")
    _check_daemon_python()
    _copy_root_owned(os.path.abspath(__file__), INSTALL_DIR, INSTALL_SCRIPT)
    with open(PLIST_PATH, "w") as f:
        f.write(_plist(DAEMON_PYTHON, INSTALL_SCRIPT, cfg))
    os.chmod(PLIST_PATH, 0o644)
    # list-form subprocess (no shell); PLIST_PATH is a module constant, not user input.
    subprocess.run(["launchctl", "bootout", "system", PLIST_PATH],
                   stderr=subprocess.DEVNULL, check=False)
    rc = subprocess.run(["launchctl", "bootstrap", "system", PLIST_PATH], check=False).returncode
    print(f"installed {INSTALL_SCRIPT} and {PLIST_PATH} (bootstrap rc={rc}); dry_run={cfg.dry_run}")
    print("tail -f /var/log/macos-oom-guard.log")


def uninstall():
    if os.geteuid() != 0:
        sys.exit("uninstall requires root: re-run with sudo")
    subprocess.run(["launchctl", "bootout", "system", PLIST_PATH],
                   stderr=subprocess.DEVNULL, check=False)
    for path in (PLIST_PATH, INSTALL_SCRIPT, f"{INSTALL_SCRIPT}.tmp"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    print(f"removed {PLIST_PATH} and {INSTALL_SCRIPT}")
    try:
        os.rmdir(INSTALL_DIR)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"left {INSTALL_DIR} in place: {e}")


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
