"""Calibration for the macOS OOM guard's decision logic.

These are the tests the 2026-08-27 revision is answerable to. Each one encodes
a defect that was found by reading 82317 samples of the guard's own log after
it sat silent through a kernel panic, so a regression here is a regression to a
guard that watched a machine die.

    python3 -m pytest test/test_macos_oom_guard.py -q
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "macos_oom_guard.py"
_spec = importlib.util.spec_from_file_location("macos_oom_guard", GUARD)
oomg = importlib.util.module_from_spec(_spec)
sys.modules["macos_oom_guard"] = oomg
_spec.loader.exec_module(oomg)

#: The highest swap and the lowest availability this host has ever reported,
#: over 82317 samples between 2026-06-14 and 2026-08-27. Any absolute threshold
#: placed outside this envelope is unreachable by construction.
OBSERVED_MAX_SWAP_GB = 40.2
OBSERVED_MIN_LEVEL = 9
HOST_RAM_GB = 39.0


def cfg(**overrides):
    """A Cfg built from the shipped defaults, with explicit overrides."""
    keys = ("PANIC_LEVEL", "CRIT_LEVEL", "WARN_LEVEL", "SWAP_MULT", "SWAP_RATE_GB",
            "SWAP_RATE_S", "MIN_VICTIM_GB", "STRIKES", "POLL_S", "FAMILY_WINDOW_S", "DRY_RUN",
            "HOG_FRAC", "HOG_LEVEL", "FILL_MULT", "FILL_GB", "FILL_S", "FILL_LEVEL")
    saved = {f"OOMG_{k}": os.environ.pop(f"OOMG_{k}", None) for k in keys}
    try:
        for key, value in overrides.items():
            os.environ[f"OOMG_{key.upper()}"] = str(value)
        return oomg.Cfg()
    finally:
        for name, value in saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value
        for key in overrides:
            os.environ.pop(f"OOMG_{key.upper()}", None)


def drive(samples, config, swap_kill=1e9, growth=None):
    """Feed (level, swap) samples through evaluate; return the actions taken."""
    strikes, actions = 0, []
    for level, swap in samples:
        verdict = oomg.evaluate(level, swap, growth, strikes, config, swap_kill)
        strikes = verdict.strikes
        actions.append(verdict.action)
    return actions


# --------------------------------------------------------------------------- #
# the strike reset
# --------------------------------------------------------------------------- #
def test_a_flapping_burst_accumulates_strikes_instead_of_being_forgotten():
    """The defect: any clean sample reset strikes to zero.

    A burst that oscillates across the threshold is more dangerous than one that
    sits on it, and under the old rule it was the one case that could never
    reach a kill. Three trips separated by single clean samples must kill.
    """
    actions = drive([(5, 0.0), (50, 0.0), (5, 0.0), (50, 0.0), (5, 0.0)],
                    cfg(strikes=2, panic_level=0))
    assert actions.count("kill") == 1, actions
    # and it is the last sample that does it, not an earlier one
    assert actions[-1] == "kill"


def test_sustained_calm_still_clears_the_strike_count():
    """Decay must not become a ratchet: enough clean samples still disarm."""
    conf = cfg(strikes=3, panic_level=0)
    strikes = 0
    for level in (5, 5):                      # two trips, armed but not killing
        verdict = oomg.evaluate(level, 0.0, None, strikes, conf, 1e9)
        strikes = verdict.strikes
    assert strikes == pytest.approx(2.0)
    for _ in range(8):                        # sustained calm
        verdict = oomg.evaluate(80, 0.0, None, strikes, conf, 1e9)
        strikes = verdict.strikes
    assert strikes == 0
    assert verdict.action == "clear"


def test_the_bucket_leaks_by_the_decay_rate_not_by_a_reset():
    conf = cfg(strikes=5, panic_level=0)
    verdict = oomg.evaluate(80, 0.0, None, 4, conf, 1e9)
    assert verdict.strikes == pytest.approx(4 - conf.strike_decay)
    assert verdict.action == "clear"


def test_the_bucket_must_leak_slower_than_it_fills():
    """Draining one-for-one looks like a fix and is not.

    An alternating trip/clean sequence then cancels exactly and never arms --
    which is the very burst shape the reset was failing on. The first attempt at
    this fix used 1.0 and this test is why it is 0.5.
    """
    assert 0.0 < cfg().strike_decay < 1.0


def test_a_one_in_three_duty_cycle_is_the_break_even_point():
    conf = cfg(strikes=99, panic_level=0)          # never kills; watch the level
    strikes = 0.0
    for _ in range(30):                            # trip, clean, clean
        for level in (5, 80, 80):
            strikes = oomg.evaluate(level, 0.0, None, strikes, conf, 1e9).strikes
    assert strikes == pytest.approx(0.0, abs=1e-9)


def test_the_strike_count_never_goes_negative():
    conf = cfg(panic_level=0)
    assert oomg.evaluate(80, 0.0, None, 0, conf, 1e9).strikes == 0


# --------------------------------------------------------------------------- #
# severity outranks persistence
# --------------------------------------------------------------------------- #
def test_a_panic_level_reading_kills_on_the_first_sample():
    """Waiting for confirmation at 5% available is how the machine is lost."""
    conf = cfg()
    verdict = oomg.evaluate(conf.panic_level - 1, 0.0, None, 0, conf, 1e9)
    assert verdict.action == "kill"
    assert verdict.why == "panic"


def test_the_panic_arm_sits_below_anything_this_host_has_survived():
    """A first-sample kill must be a genuine emergency, not a routine reading.

    The lowest level ever recorded here is 9, and the guard killed and the box
    lived. The panic arm has to be below that or it is just a second crit arm
    with the safety removed.
    """
    assert cfg().panic_level < OBSERVED_MIN_LEVEL


def test_severity_is_monotone_in_the_level():
    """As availability falls the verdict must never become less severe."""
    conf = cfg(strikes=2)
    rank = {"clear": 0, "arm": 1, "kill": 2}
    worst = -1
    for level in range(60, -1, -1):
        verdict = oomg.evaluate(level, 0.0, None, 0, conf, 1e9)
        assert rank[verdict.action] >= worst, f"verdict softened at level={level}"
        worst = max(worst, rank[verdict.action])
    assert worst == rank["kill"]


# --------------------------------------------------------------------------- #
# the swap threshold
# --------------------------------------------------------------------------- #
def test_the_absolute_swap_arm_is_reachable_on_this_host():
    """The defect: 1.5 * RAM is 58.5 GB and swap has never passed 40.2 GB.

    An arm that cannot fire is not a backstop, it is decoration. It fired zero
    times in 82317 samples.
    """
    assert cfg().swap_mult * HOST_RAM_GB <= OBSERVED_MAX_SWAP_GB


def test_the_old_swap_threshold_is_recorded_as_unreachable():
    """Guards the reasoning, so nobody restores 1.5 without meeting this."""
    assert 1.5 * HOST_RAM_GB > OBSERVED_MAX_SWAP_GB


def test_high_but_stable_swap_is_not_distress():
    """40 GB of swap at level 33 is a normal working state on this box.

    Lowering the absolute arm until it fires would have tripped 2316 times on a
    healthy machine. The level gate is what keeps that from happening.
    """
    conf = cfg()
    verdict = oomg.evaluate(33, 40.0, 0.0, 0, conf, conf.swap_mult * HOST_RAM_GB)
    assert verdict.action == "clear"


def test_a_swap_burst_trips_the_rate_arm():
    conf = cfg()
    verdict = oomg.evaluate(20, 25.0, conf.swap_rate_gb + 1.0, 0, conf, 1e9)
    assert verdict.action == "arm"
    assert verdict.why == "rate"


def test_slow_swap_growth_does_not_trip_the_rate_arm():
    """The 23:02->23:08 climb before the panic: 2.4 GB over six minutes."""
    conf = cfg()
    verdict = oomg.evaluate(20, 18.3, 0.05, 0, conf, 1e9)
    assert verdict.action == "clear"


def test_the_rate_arm_needs_the_level_gate_too():
    """A burst on a machine with plenty of headroom is a job starting, not a death."""
    conf = cfg()
    verdict = oomg.evaluate(80, 25.0, conf.swap_rate_gb + 5.0, 0, conf, 1e9)
    assert verdict.action == "clear"


# --------------------------------------------------------------------------- #
# unevaluable input is not OK
# --------------------------------------------------------------------------- #
def test_growth_is_unevaluable_on_a_cold_start():
    assert oomg.swap_growth_gb([], 6.0, 100.0) is None
    assert oomg.swap_growth_gb([(100.0, 5.0)], 6.0, 100.0) is None


def test_growth_is_unevaluable_when_the_window_is_barely_covered():
    history = [(99.5, 5.0), (100.0, 5.0)]
    assert oomg.swap_growth_gb(history, 6.0, 100.0) is None


def test_a_stall_gap_does_not_manufacture_growth():
    """The failure mode this guard actually has.

    Two samples 180 s apart say nothing about a 6 s window. Reading the gap as
    growth would invent a huge rate out of the guard's own outage — and would
    then kill something the moment it woke up.
    """
    history = [(0.0, 18.0), (180.0, 35.0)]
    assert oomg.swap_growth_gb(history, 6.0, 180.0) is None


def test_unevaluable_growth_never_trips_and_never_clears_the_other_arms():
    conf = cfg()
    assert oomg.evaluate(50, 10.0, None, 0, conf, 1e9).action == "clear"
    # the level arm still works while the rate arm is abstaining
    assert oomg.evaluate(conf.panic_level - 1, 10.0, None, 0, conf, 1e9).action == "kill"


def test_growth_is_measured_over_a_properly_covered_window():
    history = [(94.0, 10.0), (96.0, 12.0), (98.0, 18.0), (100.0, 22.0)]
    assert oomg.swap_growth_gb(history, 6.0, 100.0) == pytest.approx(12.0)


def test_growth_ignores_samples_older_than_the_window():
    history = [(50.0, 1.0), (96.0, 12.0), (100.0, 14.0)]
    assert oomg.swap_growth_gb(history, 6.0, 100.0) == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# known-bad calibration: replay the excursion the guard let through
# --------------------------------------------------------------------------- #
def test_the_2026_08_26_2240_excursion_is_still_not_a_kill():
    """level 19/15/17 with swap 16.1 -> 19.2 GB, and the machine recovered.

    The old guard let this through for the wrong reason (it never reached the
    threshold). The new guard must also let it through, for the right one: this
    is a real excursion that resolved itself, and killing on it would have cost
    the user a process for nothing.
    """
    conf = cfg()
    samples = [(19, 16.1), (15, 17.3), (17, 19.2)]
    growth = 19.2 - 16.1
    strikes, actions = 0, []
    for level, swap in samples:
        verdict = oomg.evaluate(level, swap, growth, strikes, conf,
                                conf.swap_mult * HOST_RAM_GB)
        strikes = verdict.strikes
        actions.append(verdict.action)
    assert "kill" not in actions, actions


def test_a_burst_that_oscillates_across_crit_now_reaches_a_kill():
    """The constructed target failure, and the guard must fail it.

    Under the old reset rule this sequence produced no kill at all, because
    every second sample zeroed the count. It is the shape a fast allocator
    makes, and it is why the strike reset had to go.
    """
    conf = cfg(strikes=2, panic_level=0)
    strikes, killed = 0, False
    for level in (8, 12, 8, 12, 8):
        verdict = oomg.evaluate(level, 0.0, None, strikes, conf, 1e9)
        strikes = verdict.strikes
        killed |= verdict.action == "kill"
    assert killed

    # the same sequence under the old semantics: reset instead of decay
    strikes, killed_old = 0, False
    for level in (8, 12, 8, 12, 8):
        if level >= conf.crit_level:
            strikes = 0
            continue
        strikes += 1
        if strikes >= conf.strikes:
            killed_old = True
            strikes = 0
    assert not killed_old, "the old rule is supposed to miss this"


# --------------------------------------------------------------------------- #
# 2026-10-04: the family, not the child
# --------------------------------------------------------------------------- #
#: The process group of the benchmark that panicked the host: the agent's
#: wrapper shell, coreutils timeout, the baseline harness and its workers.
INCIDENT_PGID = 55900
UV_PY = "/Users/alice/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11"
INCIDENT_GROUP = [(55900, "/bin/zsh"), (55901, "/opt/homebrew/bin/timeout"),
                  (55976, UV_PY), (57368, UV_PY), (57565, UV_PY), (58285, UV_PY)]


#: The first two kills of the incident: worker 55980 at 20:38:05, 56438 at :29.
FIRST_KILL = (100.0, INCIDENT_PGID, 55980)


def test_the_first_victim_of_a_group_is_killed_alone():
    assert not oomg.repeat_offender(INCIDENT_PGID, 55980, [], 100.0, 300.0)


def test_a_second_victim_from_the_same_group_escalates():
    """The incident: five workers from one pool, killed 24-32 s apart."""
    assert oomg.repeat_offender(INCIDENT_PGID, 56438, [FIRST_KILL], 124.0, cfg().family_window_s)


def test_the_same_pid_again_is_not_a_repeat():
    """A victim still exiting (or never dying, in dry-run) is not a second one.

    Matching on the group alone turned one kill into a group kill two seconds
    later whenever the first victim was picked again.
    """
    assert not oomg.repeat_offender(INCIDENT_PGID, 55980, [FIRST_KILL], 102.0, 300.0)


def test_a_victim_from_another_group_does_not_escalate():
    assert not oomg.repeat_offender(4242, 56438, [FIRST_KILL], 124.0, 300.0)


def test_an_old_kill_has_aged_out_of_the_window():
    assert not oomg.repeat_offender(INCIDENT_PGID, 56438, [FIRST_KILL], 401.0, 300.0)


def test_an_unknown_group_never_escalates():
    """A None pgid matches a None from an earlier lookup failure; it must not."""
    assert not oomg.repeat_offender(None, 56438, [(100.0, None, 55980)], 101.0, 300.0)


def test_a_zero_window_disables_escalation():
    assert not oomg.repeat_offender(INCIDENT_PGID, 56438, [FIRST_KILL], 101.0, 0.0)


def test_a_resident_victim_is_reported_with_its_age():
    resident = oomg.still_exiting([FIRST_KILL], 106.0, 1.5, footprint=lambda p: 21.0)
    assert resident == {55980: (21.0, 6.0)}


def test_a_victim_that_freed_its_memory_is_not_resident():
    """Footprint decides, not liveness: a zombie is alive with nothing resident."""
    assert oomg.still_exiting([FIRST_KILL], 103.0, 1.5, footprint=lambda p: 0.0) == {}


def test_wait_while_a_fresh_victim_is_releasing_memory():
    assert oomg.should_wait({55980: (15.0, 1.0)}, {}, "level")             # first look
    assert oomg.should_wait({55980: (10.0, 2.0)}, {55980: 15.0}, "level")  # falling


def test_no_wait_once_the_victim_stops_releasing():
    """A teardown that stalls is not memory on its way back."""
    assert not oomg.should_wait({55980: (10.0, 3.0)}, {55980: 10.0}, "level")


def test_no_wait_past_the_cap():
    assert not oomg.should_wait({55980: (5.0, oomg.WAIT_CAP_S + 1)}, {55980: 9.0}, "level")


def test_a_rising_level_keeps_the_wait_through_a_flat_poll():
    """A teardown can sit flat for a poll while the system still recovers."""
    assert oomg.should_wait({55980: (10.0, 2.0)}, {55980: 10.0}, "level", level=9, prev_level=7)
    assert not oomg.should_wait({55980: (10.0, 2.0)}, {55980: 10.0}, "level", level=7, prev_level=7)
    assert not oomg.should_wait({55980: (10.0, 2.0)}, {55980: 10.0}, "level", level=9, prev_level=None)


def test_never_wait_at_panic_level():
    """The third review: below panic_level, a slow teardown plus growing
    siblings at ~1 GB/s each could cost 10 GB per grower during a 10 s wait."""
    assert not oomg.should_wait({55980: (15.0, 1.0)}, {}, "panic")


def test_the_incident_group_is_eligible_for_a_group_kill():
    assert oomg.group_kill_refusal(INCIDENT_PGID, INCIDENT_GROUP, {1, 99}, {2}) is None


@pytest.mark.parametrize("members, protect, own, pgid, fragment", [
    (INCIDENT_GROUP + [(600, "/usr/libexec/xpcproxy")], set(), set(), INCIDENT_PGID, "system process"),
    (INCIDENT_GROUP + [(601, "/Applications/X.app/Contents/MacOS/Finder")], set(), set(), INCIDENT_PGID,
     "protected"),
    (INCIDENT_GROUP + [(602, "")], set(), set(), INCIDENT_PGID, "no readable path"),
    # app bundles keep all their helpers in one group (measured: Chrome 668 with
    # 7 helpers, PyCharm 682, Paseo 643)
    ([(668, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
      (700, "/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/"
            "Versions/1/Helpers/Google Chrome Helper (Renderer).app/Contents/MacOS/"
            "Google Chrome Helper (Renderer)")], set(), set(), 668, "is an app"),
    ([(682, "/Users/alice/Applications/PyCharm.app/Contents/MacOS/pycharm"),
      (690, "/opt/homebrew/bin/node")], set(), set(), 682, "is an app"),
    ([(643, "/Applications/Paseo.app/Contents/MacOS/Paseo")], set(), set(), 643, "is an app"),
    (None, set(), set(), INCIDENT_PGID, "possibly truncated"),
    (INCIDENT_GROUP, {55976}, set(), INCIDENT_PGID, "protected pid"),
    (INCIDENT_GROUP, set(), {INCIDENT_PGID}, INCIDENT_PGID, "own group"),
    ([], set(), set(), INCIDENT_PGID, "no live members"),
    (INCIDENT_GROUP, set(), set(), 1, "no usable group"),
    (INCIDENT_GROUP, set(), set(), None, "no usable group"),
])
def test_group_kill_refuses_every_group_it_cannot_vouch_for(members, protect, own, pgid, fragment):
    refusal = oomg.group_kill_refusal(pgid, members, protect, own)
    assert refusal is not None and fragment in refusal, refusal


def test_a_job_piped_through_filters_is_still_one_job():
    """`job | tail -3` puts /usr/bin/tail in the job's group (measured)."""
    group = INCIDENT_GROUP + [(59000, "/usr/bin/tail"), (59001, "/usr/bin/tee")]
    assert oomg.group_kill_refusal(INCIDENT_PGID, group, set(), set()) is None


#: Kernel-reported paths of the interpreters on this host (measured 2026-10-04):
#: a homebrew venv, /opt/homebrew/bin/python3, and /usr/bin/python3 (Xcode).
FRAMEWORK_PYTHONS = [
    "/opt/homebrew/Cellar/python@3.13/3.13.8/Frameworks/Python.framework/Versions/3.13/"
    "Resources/Python.app/Contents/MacOS/Python",
    "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/"
    "Resources/Python.app/Contents/MacOS/Python",
    "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/"
    "Resources/Python.app/Contents/MacOS/Python",
]


@pytest.mark.parametrize("python", FRAMEWORK_PYTHONS)
def test_a_pool_run_on_a_framework_python_is_still_a_job(python):
    """An earlier version treated every framework python as an app.

    `.app/Contents/` anywhere in the path refused them all, so a respawning
    pool on the repo's own interpreter could never be killed as a group.
    """
    group = [(55900, "/bin/zsh"), (55976, python), (57368, python)]
    assert oomg.group_kill_refusal(INCIDENT_PGID, group, set(), set()) is None


@pytest.mark.parametrize("path, is_app", [
    ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", True),
    ("/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Versions/1/"
     "Helpers/Google Chrome Helper (Renderer).app/Contents/MacOS/Google Chrome Helper (Renderer)", True),
    ("/Users/alice/Applications/PyCharm.app/Contents/MacOS/pycharm", True),
    ("/System/Applications/Mail.app/Contents/MacOS/Mail", True),
    ("/Applications/Xcode.app/Contents/Developer/usr/bin/make", True),
    # apps one folder down (third review)
    ("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad", True),
    ("/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal", True),
    ("/Users/alice/Applications/Chrome Apps.localized/Docs.app/Contents/MacOS/app_mode_loader", True),
    ("/Applications/Paseo 0.8.0-beta.app/Contents/Frameworks/Paseo Helper (Renderer).app/Contents/MacOS/"
     "Paseo Helper (Renderer)", True),
    ("/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/"
     "Resources/Python.app/Contents/MacOS/Python", False),
    (FRAMEWORK_PYTHONS[2], False),
    (FRAMEWORK_PYTHONS[0], False),
    ("/Users/alice/Library/Caches/ms-playwright/chromium-1200/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
     False),
    (UV_PY, False),
])
def test_only_installed_apps_count_as_apps(path, is_app):
    assert oomg._is_app(path) is is_app


def test_a_truncated_member_list_is_unevaluable(monkeypatch):
    monkeypatch.setattr(oomg, "_group_members", lambda pgid: None)
    assert oomg.read_group(55900) is None


def test_doubt_about_a_member_counts_as_alive(monkeypatch):
    def boom(pid, sig):
        raise OSError(5, "EIO")
    monkeypatch.setattr(oomg.os, "kill", boom)
    assert oomg._alive(12345) is True


def test_a_member_that_exited_mid_read_is_dropped_not_refused(monkeypatch):
    monkeypatch.setattr(oomg, "_group_members", lambda pgid: [55900, 59002, 59003])
    paths = {55900: "/bin/zsh", 59002: "", 59003: ""}
    monkeypatch.setattr(oomg, "_pid_path", lambda pid: paths[pid])
    monkeypatch.setattr(oomg, "_alive", lambda pid: pid == 59003)
    # 59002 is gone: dropped. 59003 is alive but unreadable: kept, so it refuses.
    members = oomg.read_group(55900)
    assert members == [(55900, "/bin/zsh"), (59003, "")]
    assert "no readable path" in oomg.group_kill_refusal(55900, members, set(), set())


def test_group_plumbing_and_group_kill_on_a_live_group():
    """Preconditions on the real syscalls: the guard can read a group and kill it.

    A shell in its own session with two background children stands in for a
    harness and its workers. Killing the group must take all three.
    """
    import subprocess
    import time

    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & sleep 30 & wait"], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while len(oomg._group_members(proc.pid)) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        members = oomg._group_members(proc.pid)
        assert oomg._pgid(proc.pid) == proc.pid
        assert proc.pid in members and len(members) == 3, members
        paths = [oomg._pid_path(m) for m in members]
        assert all(paths), paths
        assert oomg.kill_victim(proc.pid, proc.pid, whole_group=True) == (True, None)
        proc.wait(timeout=5)
        deadline = time.monotonic() + 5
        while oomg._group_members(proc.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert oomg._group_members(proc.pid) == []
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, 9)


def test_a_vanished_victim_is_not_an_error():
    assert oomg._pgid(2 ** 22 + 12345) is None
    assert oomg.kill_victim(2 ** 22 + 12345, None, whole_group=False) == (True, None)


def test_a_kill_that_could_not_be_sent_is_reported_as_not_signalled(monkeypatch):
    """The guard must not then wait on a victim that is still running."""
    def deny(pid, sig):
        raise PermissionError(1, "EPERM")
    monkeypatch.setattr(oomg.os, "kill", deny)
    signalled, error = oomg.kill_victim(4242, None, whole_group=False)
    assert signalled is False and "kill(4242)" in error


# --------------------------------------------------------------------------- #
# self-protection must not claim what it does not have
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS syscall semantics")
def test_macos_has_no_mlockall():
    """The fact the old _self_protect hid: -1/ENOSYS, which ctypes does not raise.

    If Apple ever implements it this fails, and page locking is worth revisiting.
    """
    import ctypes
    import errno

    assert oomg._libc.mlockall(0) == -1
    assert ctypes.get_errno() == errno.ENOSYS


def test_the_guard_no_longer_calls_mlockall_or_claims_locked_pages():
    assert "_libc.mlockall" not in GUARD.read_text()
    assert "pages unlocked" in oomg._self_protect()


# --------------------------------------------------------------------------- #
# the run loop's wiring of the wait (third review: only the helper was tested)
# --------------------------------------------------------------------------- #
def drive_loop(monkeypatch, tmp_path, levels, sizes, conf, kill_result=(True, None)):
    """Run the real loop over scripted polls; return (find_victim calls, kills).

    `levels[i]` is memorystatus_level at poll i and `sizes[i]` the footprints
    {pid: GB} then. find_victim picks the biggest pid not excluded. Every pid
    is its own group, so no group kill muddies the wait.
    """
    clock, poll = [0.0], [0]
    finds, killed = [], []

    def sleep(_s):
        if poll[0] >= len(levels):
            raise StopIteration
        clock[0] += 1.0

    def level():
        poll[0] += 1
        return levels[poll[0] - 1]

    def footprint(pid):
        return sizes[poll[0] - 1].get(pid, 0.0)

    def find(min_gb, excl):
        finds.append((poll[0], set(excl)))
        live = {p: g for p, g in sizes[poll[0] - 1].items() if p not in excl and g >= min_gb}
        if not live:
            return None, 0.0, ""
        pid = max(live, key=live.get)
        return pid, live[pid], "/Users/alice/x/python"

    def kill(pid, pgid, whole_group):
        killed.append((poll[0], pid, whole_group))
        return kill_result

    for name, fn in [("memorystatus_level", level), ("swap_used_gb", lambda: 0.0),
                     ("phys_ram_gb", lambda: 39.0), ("_footprint_gb", footprint),
                     ("find_victim", find), ("kill_victim", kill), ("_pgid", lambda p: p),
                     ("_self_protect", lambda: "test")]:
        monkeypatch.setattr(oomg, name, fn)
    monkeypatch.setattr(oomg.time, "sleep", sleep)
    monkeypatch.setattr(oomg.time, "monotonic", lambda: clock[0])
    with pytest.raises(StopIteration):
        oomg.run(conf, logfile=str(tmp_path / "guard.log"))
    return finds, killed


def test_loop_waits_while_the_victim_drains_then_moves_on(monkeypatch, tmp_path):
    """Kill A; A drains 20 -> 15 -> 10 (wait, wait); A stalls at 10 (stop
    waiting, pick B with A excluded rather than re-signalled)."""
    sizes = [{1: 20.0, 2: 8.0}, {1: 15.0, 2: 8.0}, {1: 10.0, 2: 8.0}, {1: 10.0, 2: 8.0}]
    finds, killed = drive_loop(monkeypatch, tmp_path, [8] * 4, sizes, cfg(strikes=1))
    assert [k[:2] for k in killed] == [(1, 1), (4, 2)]
    assert [f[0] for f in finds] == [1, 4], "find_victim must not run while waiting"
    assert 1 in finds[1][1], "the stalled victim is excluded, not picked again"


def test_loop_never_waits_at_panic_level(monkeypatch, tmp_path):
    sizes = [{1: 20.0, 2: 8.0}, {1: 15.0, 2: 8.0}]
    finds, killed = drive_loop(monkeypatch, tmp_path, [3, 3], sizes, cfg(strikes=1))
    assert [k[:2] for k in killed] == [(1, 1), (2, 2)]


def test_loop_wait_is_capped_even_while_draining(monkeypatch, tmp_path):
    cap = int(oomg.WAIT_CAP_S)
    sizes = [{1: 30.0 - i, 2: 8.0} for i in range(cap + 3)]
    finds, killed = drive_loop(monkeypatch, tmp_path, [8] * (cap + 3), sizes, cfg(strikes=1))
    second = [k for k in killed if k[1] == 2]
    assert second and second[0][0] <= cap + 2, killed


def test_loop_does_not_wait_on_a_kill_that_failed(monkeypatch, tmp_path):
    sizes = [{1: 20.0, 2: 8.0}, {1: 20.0, 2: 8.0}]
    finds, killed = drive_loop(monkeypatch, tmp_path, [8, 8], sizes, cfg(strikes=1),
                          kill_result=(False, "kill(1) failed: EPERM"))
    assert [f[0] for f in finds] == [1, 2]


def test_dry_run_neither_waits_nor_skips_its_victim(monkeypatch, tmp_path):
    """A dry-run victim never dies; the log must keep naming it, as live would."""
    sizes = [{1: 20.0, 2: 8.0}] * 3
    finds, killed = drive_loop(monkeypatch, tmp_path, [8] * 3, sizes, cfg(strikes=1, dry_run=1))
    assert [f[0] for f in finds] == [1, 2, 3]
    assert all(1 not in excl for _poll, excl in finds)
    log = (tmp_path / "guard.log").read_text()
    assert log.count("DRY-RUN would SIGKILL pid=1 ") == 3 and "waiting" not in log


def test_loop_waits_poll_by_poll_with_the_default_strikes(monkeypatch, tmp_path):
    """Fourth review: a wait spent the strikes, so with strikes=2 each wait poll
    was followed by a re-arm poll and the 4 s cap became ~6 s.

    Arm at 1, kill A at 2 (the kill resets the strikes), re-arm at 3, then A
    drains every poll: waits at 4, 5, 6 (age 2..4, within the cap) back to back,
    and B dies at 7. With the strikes spent on each wait, B died at 8.
    """
    n = 8
    sizes = [{1: 30.0 - 2 * i, 2: 8.0} for i in range(n)]
    finds, killed = drive_loop(monkeypatch, tmp_path, [8] * n, sizes, cfg(strikes=2))
    assert [k[:2] for k in killed][:2] == [(2, 1), (7, 2)], killed
    log = (tmp_path / "guard.log").read_text()
    assert log.count("waiting for killed pid=1") == 3


def test_loop_keeps_waiting_while_the_level_recovers(monkeypatch, tmp_path):
    """A flat footprint alone would end the wait at poll 3; the rising level
    (7 -> 8 -> 9) keeps it until the cap."""
    sizes = [{1: 20.0, 2: 8.0}] * 7
    levels = [6, 7, 8, 9, 9, 9, 9]
    finds, killed = drive_loop(monkeypatch, tmp_path, levels, sizes, cfg(strikes=1))
    assert killed[0][:2] == (1, 1)
    assert killed[1][0] >= 4, killed


def test_install_refuses_a_user_owned_install_dir(tmp_path):
    """The daemon runs as root, so --install must not drop its script into a
    directory a user can write to (they could swap the script under it)."""
    with pytest.raises(SystemExit, match="not a root-owned directory"):
        oomg._copy_root_owned(str(GUARD), str(tmp_path), str(tmp_path / "g.py"))
    assert not (tmp_path / "g.py").exists()


def test_plist_runs_the_root_owned_copy_with_the_system_python():
    xml = oomg._plist(oomg.DAEMON_PYTHON, oomg.INSTALL_SCRIPT, oomg.Cfg())
    assert "<string>/usr/bin/python3</string>" in xml
    assert "<string>/usr/local/libexec/macos-oom-guard/macos_oom_guard.py</string>" in xml
    assert "/Users/" not in xml


def test_plist_pins_the_license_free_developer_dir():
    xml = oomg._plist(oomg.DAEMON_PYTHON, oomg.INSTALL_SCRIPT, oomg.Cfg())
    assert "<key>DEVELOPER_DIR</key><string>/Library/Developer/CommandLineTools</string>" in xml


def test_install_refuses_an_interpreter_that_does_not_run(monkeypatch):
    monkeypatch.setattr(oomg, "DAEMON_PYTHON", "/usr/bin/false")
    with pytest.raises(SystemExit, match="unusable"):
        oomg._check_daemon_python()
    monkeypatch.setattr(oomg, "DAEMON_PYTHON", "/nonexistent/python3")
    with pytest.raises(SystemExit, match="did not run"):
        oomg._check_daemon_python()


def test_the_daemon_python_runs_with_the_pinned_developer_dir():
    oomg._check_daemon_python()


def test_install_refuses_a_user_writable_ancestor(tmp_path):
    """The final dir being root-owned is not enough: whoever can write its parent can
    rename it away and substitute their own after the check."""
    inner = tmp_path / "libexec" / "macos-oom-guard"
    with pytest.raises(SystemExit, match="not a root-owned directory"):
        oomg._copy_root_owned(str(GUARD), str(inner), str(inner / "g.py"))
    assert not (tmp_path / "libexec").exists()


def test_the_real_install_ancestors_are_root_only():
    for p in ("/", "/usr", "/usr/local", "/usr/local/libexec"):
        oomg._require_root_only(p)


# --------------------------------------------------------------------------- #
# the hog arm (2026-10-11)
# --------------------------------------------------------------------------- #
#: Every sample the guard logged while one python job grew to 54.6 GB, as
#: (level, swap GB, growth GB/6 s or None). The heartbeat lines carry the
#: interval's worst level, which is the reading used here.
RUNAWAY_2026_10_11 = [
    (34, 18.2, None),   # 11:53:08 heartbeat, worst of the minute
    (40, 18.0, None),   # 11:54:09
    (23, 21.5, 1.1),    # 11:54:23 warn
    (23, 34.4, 2.6),    # 11:55:02 warn
    (23, 36.5, None),   # 11:55:09 heartbeat
    (30, 38.7, None),   # 11:56:10
    (32, 38.7, None),   # 11:57:10
]
#: The runaway's footprint in the first jetsam report (11:55:08), and the
#: biggest legitimate job measured on this host (Palace solve, whole tree).
RUNAWAY_GB = 53.2
LARGEST_LEGIT_JOB_GB = 22.6


def drive_hog(samples, config, hog_gb, swap_kill=None):
    swap_kill = config.swap_mult * HOST_RAM_GB if swap_kill is None else swap_kill
    hog_kill = config.hog_frac * HOST_RAM_GB
    strikes, actions = 0, []
    for level, swap, growth in samples:
        verdict = oomg.evaluate(level, swap, growth, strikes, config, swap_kill, hog_gb, hog_kill)
        strikes = verdict.strikes
        actions.append((verdict.action, verdict.why))
    return actions


def test_the_2026_10_11_runaway_reached_none_of_the_old_arms():
    """The defect, recorded: with no hog input the guard watched it all happen."""
    actions = drive_hog(RUNAWAY_2026_10_11, cfg(), hog_gb=None)
    assert all(a == "clear" for a, _why in actions), actions


def test_the_2026_10_11_runaway_is_killed_by_the_hog_arm():
    actions = drive_hog(RUNAWAY_2026_10_11, cfg(), hog_gb=RUNAWAY_GB)
    assert actions[:2] == [("arm", "hog"), ("kill", "hog")], actions


def test_the_hog_threshold_sits_between_legit_jobs_and_the_runaway():
    hog_kill = cfg().hog_frac * HOST_RAM_GB
    assert LARGEST_LEGIT_JOB_GB < hog_kill < RUNAWAY_GB


def test_the_largest_legitimate_job_never_trips_the_hog_arm():
    # even at the runaway's own pressure readings
    actions = drive_hog(RUNAWAY_2026_10_11, cfg(), hog_gb=LARGEST_LEGIT_JOB_GB)
    assert all(a == "clear" for a, _why in actions), actions


def test_a_big_job_with_memory_to_spare_is_left_alone():
    conf = cfg()
    actions = drive_hog([(conf.hog_level, 5.0, 0.0)] * 10, conf, hog_gb=RUNAWAY_GB)
    assert all(a == "clear" for a, _why in actions), actions


def test_the_hog_arm_is_monotone():
    """A bigger hog or a lower level must never turn a trip back into a clear."""
    conf = cfg()
    hog_kill = conf.hog_frac * HOST_RAM_GB
    for level in range(conf.panic_level, conf.hog_level):
        for hog in (hog_kill + 0.1, RUNAWAY_GB, 200.0, 1e4):
            verdict = oomg.evaluate(level, 0.0, None, 0, conf, 1e9, hog, hog_kill)
            assert verdict.action != "clear", (level, hog)


def test_an_unmeasured_hog_never_trips_and_never_clears_the_other_arms():
    conf = cfg()
    assert oomg.evaluate(30, 0.0, None, 0, conf, 1e9, None, 1.0).action == "clear"
    assert oomg.evaluate(conf.crit_level - 1, 0.0, None, 0, conf, 1e9, None, 1.0).why == "level"


def test_the_hog_arm_names_itself_when_the_level_arm_also_fires():
    """So the run loop kills the hog, not whatever else is biggest."""
    conf = cfg()
    verdict = oomg.evaluate(conf.crit_level - 1, 0.0, None, 0, conf, 1e9, RUNAWAY_GB, 29.0)
    assert verdict.why == "hog"


def test_largest_job_skips_apps_and_system_processes(monkeypatch):
    procs = {
        101: ("/Applications/VMware Fusion.app/Contents/Library/vmware-vmx", 60.0),
        102: ("/usr/libexec/someDaemon", 70.0),
        103: ("/opt/homebrew/Cellar/python@3.13/3.13.1/Frameworks/Python.framework/"
              "Versions/3.13/Resources/Python.app/Contents/MacOS/Python", 54.6),
        104: ("/Users/alice/.venv/bin/python3", 3.0),
    }
    monkeypatch.setattr(oomg, "_list_pids", lambda *a, **k: list(procs))
    monkeypatch.setattr(oomg, "_pid_path", lambda pid: procs[pid][0])
    monkeypatch.setattr(oomg, "_footprint_gb", lambda pid: procs[pid][1])
    assert oomg.largest_job(set())[:2] == (103, 54.6)
    assert oomg.largest_job({103})[:2] == (104, 3.0)


def test_plist_carries_the_hog_tunables():
    xml = oomg._plist(oomg.DAEMON_PYTHON, oomg.INSTALL_SCRIPT, cfg(hog_frac=0.6, hog_level=40))
    assert "<key>OOMG_HOG_FRAC</key><string>0.6</string>" in xml
    assert "<key>OOMG_HOG_LEVEL</key><string>40</string>" in xml


# --------------------------------------------------------------------------- #
# the fill arm (2026-11-21)
# --------------------------------------------------------------------------- #
#: The guard's own heartbeats (instantaneous level, swap GB), one a minute,
#: 13:02-13:14, while four ngspice workers filled swap. Jetsam started killing
#: for vm-compressor-space-shortage at 13:14:45.
FILL_2026_11_21 = [
    (36, 21.5), (35, 24.1), (50, 24.6), (39, 24.5), (36, 24.9), (36, 28.5),
    (46, 29.1), (50, 28.9), (37, 28.8), (35, 31.7), (35, 34.6), (35, 36.7),
    (35, 39.0),
]
FILL_2026_11_21_JETSAM_INDEX = 12  # the 13:14 sample


def drive_fill(samples, config, ram=HOST_RAM_GB, step_s=60.0):
    """Replay (level, swap) samples taken every step_s; growth over fill_s from them."""
    lag = max(1, round(config.fill_s / step_s))
    swap_kill = config.swap_mult * ram
    fill_kill = config.fill_mult * ram
    strikes, actions = 0, []
    for i, (level, swap) in enumerate(samples):
        fill_growth = swap - samples[i - lag][1] if i >= lag else None
        verdict = oomg.evaluate(level, swap, None, strikes, config, swap_kill,
                                None, float("inf"), fill_growth, fill_kill)
        strikes = verdict.strikes
        actions.append((verdict.action, verdict.why))
    return actions


def test_the_2026_11_21_fill_reached_none_of_the_old_arms():
    """The defect, recorded: the level stayed in the 30s, so nothing fired."""
    conf = cfg()
    strikes = 0
    for level, swap in FILL_2026_11_21:
        verdict = oomg.evaluate(level, swap, 0.3, strikes, conf, conf.swap_mult * HOST_RAM_GB)
        strikes = verdict.strikes
        assert verdict.action == "clear", (level, swap, verdict)


def test_the_2026_11_21_fill_is_killed_before_jetsam():
    actions = drive_fill(FILL_2026_11_21, cfg())
    kills = [i for i, (a, why) in enumerate(actions) if a == "kill"]
    assert kills and kills[0] < FILL_2026_11_21_JETSAM_INDEX, actions
    assert actions[kills[0]][1] == "fill"


def test_the_fill_threshold_sits_below_every_ceiling_this_host_reached():
    """Excursions that ended in a kill or crash peaked at 36-40 GB of swap."""
    assert cfg().fill_mult * HOST_RAM_GB < 36.0


def test_a_high_but_flat_swap_plateau_is_left_alone():
    """2026-09-30 12:31-13:18: 30-33 GB, creeping, and the machine was fine."""
    conf = cfg()
    plateau = [(35, 31.0 + 0.04 * i) for i in range(48)]
    assert all(a == "clear" for a, _why in drive_fill(plateau, conf))
    # and a stable 40 GB at level 33, the documented normal working state
    assert oomg.evaluate(33, 40.0, 0.0, 0, conf, 1e9, None, float("inf"),
                         0.0, conf.fill_mult * HOST_RAM_GB).action == "clear"


def test_stale_swap_after_the_pressure_has_passed_is_left_alone():
    """After a kill swap stays high while the level recovers; that is not distress."""
    conf = cfg()
    v = oomg.evaluate(conf.fill_level, 37.0, None, 0, conf, 1e9, None, float("inf"),
                      conf.fill_gb + 3.0, conf.fill_mult * HOST_RAM_GB)
    assert v.action == "clear"


def test_unevaluable_fill_growth_never_trips():
    conf = cfg()
    v = oomg.evaluate(30, 38.0, None, 0, conf, 1e9, None, float("inf"),
                      None, conf.fill_mult * HOST_RAM_GB)
    assert v.action == "clear"


def test_the_fill_arm_is_monotone():
    """Worse input (more swap, faster growth, lower level) never clears a trip."""
    conf = cfg()
    fill_kill = conf.fill_mult * HOST_RAM_GB
    base = (35, fill_kill + 0.5, conf.fill_gb + 0.5)
    assert oomg.evaluate(base[0], base[1], None, 0, conf, 1e9, None, float("inf"),
                         base[2], fill_kill).action == "arm"
    for level in range(0, base[0] + 1):
        for swap in (base[1], base[1] + 5, base[1] + 50):
            for g in (base[2], base[2] * 4, 100.0):
                v = oomg.evaluate(level, swap, None, 0, conf, 1e9, None, float("inf"), g, fill_kill)
                assert v.action != "clear", (level, swap, g)


def test_the_fill_window_survives_the_history_trim():
    """The run loop keeps 2x the longest window, so fill growth is evaluable."""
    conf = cfg()
    history = [(t, 20.0 + 0.02 * t) for t in range(0, int(conf.fill_s * 2) + 1)]
    now = history[-1][0]
    keep = [(w, v) for w, v in history if now - w <= max(conf.swap_rate_s, conf.fill_s) * 2]
    assert oomg.swap_growth_gb(keep, conf.fill_s, now) == pytest.approx(0.02 * conf.fill_s)


def test_the_plist_carries_the_fill_tunables():
    conf = cfg(fill_mult=0.8, fill_gb=2.0, fill_s=90, fill_level=45)
    import plistlib
    env = plistlib.loads(oomg._plist("/usr/bin/python3", "/x.py", conf).encode())[
        "EnvironmentVariables"]
    assert (env["OOMG_FILL_MULT"], env["OOMG_FILL_GB"], env["OOMG_FILL_S"],
            env["OOMG_FILL_LEVEL"]) == ("0.8", "2.0", "90.0", "45")
