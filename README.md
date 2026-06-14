# macos-oom-guard

A system-wide **OOM killer for macOS** — the one Apple doesn't ship.

When a process eats all your memory, macOS doesn't kill it. It compresses, then
grows swapfiles on the boot disk *indefinitely*, on the assumption that memory
pressure is transient. On Apple Silicon the swap **segment table is finite**, so
when it saturates while a process keeps demanding pages, the whole VM deadlocks —
`WindowServer` misses its userspace watchdog check-in and the kernel
**panic-reboots the entire machine** (`AppleARMWatchdogTimer`) rather than let the
GUI hang. You lose everything, not just the runaway process.

This is a tiny, self-protecting root daemon that does what Linux's `earlyoom` /
`systemd-oomd` do: watch memory, and **SIGKILL the biggest offending process
*before* the box falls off the cliff** — so you lose one app instead of the machine.

```
$ python3 macos_oom_guard.py --status
physical RAM        : 38.7 GB
memorystatus_level  : 86   (0..100, % available; trigger when < crit)
swap used           : 2.6 GB

top eligible (killable) processes by phys_footprint:
     2.4 GB  pid    893  /Users/you/Applications/PyCharm.app/Contents/MacOS/pycharm
     1.5 GB  pid   1333  /Applications/Google Chrome.app/.../Google Chrome Helper
...
-> would kill first: pid 893 (2.4 GB) .../pycharm
```

## Why the built-in mechanisms don't save you

- **Jetsam** (`kern.memorystatus_*`, the in-kernel killer) exists, but on desktop it
  only aggressively targets sandboxed / idle apps. A long-running `python3` (or any
  heavy CLI process) launched from a terminal has no jetsam high-watermark and
  survives right up to the deadlock.
- **`memory_pressure`** (the only built-in CLI) is a pressure *generator* for
  testing — not a killer.
- **In-process guards** (a watchdog thread inside your own job) can't help: they only
  see their own process tree, they usually poll
  `kern.memorystatus_vm_pressure_level` — which, measured live, stays at `1/NORMAL`
  even in deep swap-death — and the watchdog thread itself gets CPU-starved during
  the exact freeze it's supposed to catch.

## How it works

- **Triggers on `kern.memorystatus_level`** — a `0..100` "% memory available" gauge
  that jetsam itself trends on and that falls *smoothly* as memory fills (unlike the
  bucketed pressure level). It **never** triggers on memory footprint: a healthy Mac
  can sit at a huge footprint of compressed/sparse data while perfectly green.
  Footprint is only used to *rank* victims. Secondary trigger: swap used past a
  configurable multiple of RAM.
- **Ranks victims by `ri_phys_footprint`** (`proc_pid_rusage`) — the same metric
  Activity Monitor's "Memory" column and jetsam use. (A swapped hog's RSS collapses,
  and footprint measured from outside overcounts compressed pages — `phys_footprint`
  is the honest one.)
- **Protects the system two ways**: a name denylist (`WindowServer`, `launchd`,
  `kernel_task`, `coreaudiod`, …) **and** a location rule — it will only ever kill
  executables under `/Applications`, `/Users`, `/opt/homebrew`, or `/usr/local`. A
  system daemon, WindowServer, or the kernel can never be the victim.
- **Self-protects**: runs as root at `nice -20` and calls `mlockall()` so the killer
  is never swapped out when it's needed most. The hot poll loop does **zero
  fork/exec and zero per-iteration allocation** (pure `ctypes` sysctl/libproc);
  full process enumeration happens only once it's already near the threshold.

No dependencies — pure Python 3 stdlib + `ctypes`. Single file. Apple Silicon and
Intel.

## Install

```bash
# 1. See what it reads and what it WOULD kill right now (read-only, safe):
python3 macos_oom_guard.py --status

# 2. Watch it in the foreground in dry-run — logs decisions, never kills.
#    Recommended: run a heavy job and confirm it would pick the right victim.
python3 macos_oom_guard.py --run --dry-run

# 3. Install as a boot-time root LaunchDaemon (armed):
sudo python3 macos_oom_guard.py --install

# Uninstall:
sudo python3 macos_oom_guard.py --uninstall
```

The installer writes `/Library/LaunchDaemons/io.github.fl4p.macos-oom-guard.plist`
and bootstraps it with `launchctl`. Logs go to `/var/log/macos-oom-guard.log`:

```bash
tail -f /var/log/macos-oom-guard.log
```

To install in dry-run mode (logs would-be kills but never acts), set
`OOMG_DRY_RUN=1` before `--install` (it's baked into the plist).

## Configuration

All tunable via environment variables (also baked into the plist at `--install`):

| Variable | Meaning | Default |
|---|---|---|
| `OOMG_CRIT_LEVEL` | kill when `memorystatus_level` drops below this | `10` |
| `OOMG_WARN_LEVEL` | start logging/enumerating below this | `25` |
| `OOMG_SWAP_MULT` | also kill when `swap_used > MULT × RAM` (and level < 20) | `1.5` |
| `OOMG_MIN_VICTIM_GB` | never kill a process smaller than this footprint | `1.5` |
| `OOMG_STRIKES` | consecutive trips required before killing | `2` |
| `OOMG_POLL_S` | poll interval, seconds | `1.0` |
| `OOMG_DRY_RUN` | `1` = log but never kill | `0` |

## Choosing what gets killed

The default policy is **"kill the biggest non-protected user process."** That's the
right call at a real cliff: by the time `memorystatus_level` hits single digits, the
actual runaway is tens of GB and dwarfs everything else, so it gets picked. But note
that *at idle* the biggest user process might be your editor — edit the
`PROTECT_NAMES` set in the script (e.g. add `"pycharm"`, `"Code"`) if you'd rather
shield specific apps, at the cost of not killing them if *they* are the one that
leaks.

## Safety notes

- This sends `SIGKILL` (no clean shutdown) — by design, because at the cliff a
  `SIGTERM` may never get serviced. Unsaved work in the victim is lost. That is still
  strictly better than a kernel panic, which loses **all** unsaved work everywhere.
- Run `--status` and `--run --dry-run` first to satisfy yourself the victim
  selection matches your expectations before arming it.
- It requires root to install (LaunchDaemon, `mlockall`, killing other users'
  processes).

## License

MIT — see [LICENSE](LICENSE).
