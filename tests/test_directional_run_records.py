"""Item 2c (JOB-20260727-MONDAY): COT_BULL one-sided staleness.

**What this is NOT.** The 07-20 investigation already settled the obvious
hypothesis: `tests/test_bull_reachability.py` proves BULL is reachable at the
90th percentile for all four commodities, that the only BULL suppression is the
documented Path-B 95th+ rule for Gold and Corn, and that the threshold gates are
mirror images. There is no code-level directional asymmetry suppressing BULL, so
this is not a signal-generation fix.

**What the defect actually is — a visibility asymmetry, same class as item 2b.**
`COT_BULL` earns a `signal_log` row only when a BULL signal FIRES (`:455`) or hits
a Path-B suppression (`:370`, `:380`). In a week where no commodity sits at a bull
extreme, nothing at all is written under the `COT_BULL` name, so
`MAX(scan_date)` — the only thing the Studio's `ops/obs/pa_watch.py:111` looks at
— freezes at the last bull that fired.

Measured on the Studio 2026-07-27, read-only:

  COT_BULL   MAX(scan_date) = 2026-01-06   174 rows, ALL fired=1
  COT_BEAR   MAX(scan_date) = 2026-07-21   350 rows (342 fired=1, 8 fired=0)

**COT_BEAR's freshness is accidental, not designed.** Its recent scan_dates are
2026-07-21, -07-14, -07-07, -06-30, -06-23 — clean 7-day gaps produced by Wheat
sitting at a bear extreme nearly every week. The moment Wheat stops, COT_BEAR
goes stale exactly as COT_BULL did. Fixing only the bull side would leave the
same trap armed on the other one, so the fix has to be SYMMETRIC by construction.

**Two mechanisms that look like they should already cover this, and don't:**

  * the no-signal path (`:362`) logs under the scanner name `COT_NONE`, which
    pa_watch tracks as its own separate component rather than as evidence about
    COT_BULL — and `COT_NONE` has **never written a single row** to the synced DB
    (it holds no such scanner at all);
  * `log_scan_run` (`:667`) writes a genuine per-run heartbeat, but to a
    `scan_runs` table in `~/signal_intelligence.db`, and `scan_runs` **does not
    reach the Studio** — the synced database contains only `signal_log` and
    `ticker_sigma_history`. It also files under the scanner name `COT`, not the
    two names pa_watch actually watches.

So the fix is one `fired=0` run record per DIRECTION, written on the completed-scan
path, matching the farm convention item 2b established.
"""
import importlib.util
import os
import sqlite3
import sys

import pytest

HIST_0_TO_99 = list(range(100))
LATEST_MIDRANGE = 50      # 50th pct -> no signal either way
LATEST_90TH = 90          # BULL zone
LATEST_10TH = 10          # BEAR zone

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cot_scanner.py")


@pytest.fixture()
def fresh(scanner):
    """A pristine second copy of cot_scanner, with its REAL writers intact.

    The suite's autouse `_no_network_no_side_effects` fixture replaces
    `log_signal_intelligence` with a no-op on the shared module object, which is
    right for every other test and wrong for these: the whole question here is what
    actually lands in `signal_log`. Loading a separate module object leaves the
    shared one stubbed for everyone else while giving these tests the real writer.
    Depends on `scanner` so the synthetic `config` module is already in sys.modules.
    """
    spec = importlib.util.spec_from_file_location("cot_scanner_fresh", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cot_scanner_fresh"] = mod
    try:
        spec.loader.exec_module(mod)
        # Network stub only — the DB writers stay real.
        mod.get_regime_vars = lambda: {
            "OVX": None, "GVZ": None, "WEAT_RVOL": None, "CORN_RVOL": None}
        yield mod
    finally:
        sys.modules.pop("cot_scanner_fresh", None)


@pytest.fixture()
def signal_db(fresh, monkeypatch, tmp_path):
    """Redirect the real writer's DB path at a temp file."""
    db = tmp_path / "signal_intelligence.db"
    monkeypatch.setattr(fresh.os.path, "expanduser", lambda p: str(db))
    return db


def _view(db):
    """pa_watch's own query, verbatim from ops/obs/pa_watch.py:111."""
    if not db.exists():
        return {}
    con = sqlite3.connect(str(db))
    try:
        return dict(con.execute(
            "SELECT scanner, MAX(scan_date) FROM signal_log "
            "WHERE scanner IS NOT NULL GROUP BY scanner"
        ).fetchall())
    finally:
        con.close()


def _rows(db, scanner_name):
    con = sqlite3.connect(str(db))
    try:
        return con.execute(
            "SELECT scan_date, ticker, direction, fired, signal_bucket FROM signal_log "
            "WHERE scanner=? ORDER BY id", (scanner_name,)
        ).fetchall()
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# The fix: both directions get a run record on every completed scan            #
# --------------------------------------------------------------------------- #

def test_a_week_with_no_signal_at_all_still_records_both_directions(
        fresh, conn, signal_db):
    """THE defect. A mid-range week must still prove BOTH sides ran."""
    from conftest import insert_history
    rd = insert_history(conn, "Wheat (SRW)", LATEST_MIDRANGE, HIST_0_TO_99)

    fresh.log_directional_scan_runs(rd)

    view = _view(signal_db)
    assert view.get("COT_BULL") == rd, (
        "COT_BULL has no record of having run — pa_watch reads MAX(scan_date) and "
        "would report the last BULL that fired as the last time the scanner lived")
    assert view.get("COT_BEAR") == rd


def test_the_run_records_are_symmetric_by_construction(fresh, signal_db):
    """Neither direction can go stale while the other stays fresh.

    This is the actual content of the word "one-sided": COT_BEAR looked healthy
    only because Wheat fired nearly every week. A fix that advanced only the bull
    side would leave the identical trap armed on the bear side.
    """
    fresh.log_directional_scan_runs("2026-07-21")

    view = _view(signal_db)
    assert view["COT_BULL"] == view["COT_BEAR"] == "2026-07-21"


def test_a_run_record_does_not_claim_a_signal_fired(fresh, signal_db):
    """fired=0 and a reserved sentinel vehicle — it is liveness, not a trade."""
    fresh.log_directional_scan_runs("2026-07-21")

    for name in ("COT_BULL", "COT_BEAR"):
        rows = _rows(signal_db, name)
        assert len(rows) == 1, f"{name}: expected exactly one run record, got {rows}"
        scan_date, ticker, direction, fired, bucket = rows[0]
        assert fired == 0, f"{name}: a run record must never claim fired=1"
        assert ticker == fresh.RUN_SENTINEL_VEHICLE
        assert bucket == "RUN"
        assert direction == "NONE"


def test_run_records_do_not_displace_a_real_firing_signal(fresh, conn, signal_db):
    """Additive. A bull week must show BOTH the fired=1 row and the run rows."""
    from conftest import insert_history
    rd = insert_history(conn, "Wheat (SRW)", LATEST_90TH, HIST_0_TO_99)

    fresh.detect_signals(conn, rd)
    fresh.log_directional_scan_runs(rd)

    bull = _rows(signal_db, "COT_BULL")
    fired = [r for r in bull if r[3] == 1]
    runs = [r for r in bull if r[1] == fresh.RUN_SENTINEL_VEHICLE]
    assert len(fired) == 1 and fired[0][1] == "WEAT", f"lost the real signal: {bull}"
    assert len(runs) == 1, "a firing week must ALSO record that the scan ran"


def test_the_completed_scan_path_writes_the_run_records(scanner):
    """Wire-up guard: run_scan's OK path must actually call it.

    A correct helper nobody invokes is the failure mode the deferred-activation
    register exists for, so the call site is pinned, not just the function.
    """
    src = open(scanner.__file__).read()
    ok_path = src.split("log_scan_run('COT', 'OK'")[0]
    assert "log_directional_scan_runs(report_date)" in ok_path or \
           "log_directional_scan_runs(report_date)" in src.split("mark_week_processed")[1], \
        "log_directional_scan_runs is never called from run_scan's completed-scan path"


# --------------------------------------------------------------------------- #
# Anti-vacuous rails (constitution A6)                                        #
# --------------------------------------------------------------------------- #

def test_no_run_record_without_a_report_date(fresh, signal_db):
    """A scan with no CFTC data must not publish proof of life.

    run_scan's FETCH_FAIL path returns before detect_signals; a liveness record
    there would mark a scanner alive precisely when its input pipeline is broken.
    """
    fresh.log_directional_scan_runs(None)
    assert _view(signal_db) == {}

    fresh.log_directional_scan_runs("")
    assert _view(signal_db) == {}


def test_the_fetch_fail_path_does_not_write_run_records(scanner):
    """The FETCH_FAIL branch must return before the run records, not after."""
    src = open(scanner.__file__).read()
    before_fetch_fail, after = src.split("'FETCH_FAIL'", 1)
    # everything up to and including the FETCH_FAIL early return
    fetch_fail_block = after.split("Latest report date in DB")[0]
    assert "log_directional_scan_runs" not in fetch_fail_block, \
        "FETCH_FAIL writes a liveness record for a scan that fetched nothing (A6)"


def test_a_run_record_write_failure_is_loud(fresh, monkeypatch, capsys):
    """Rides the existing [SIGNAL_LOG_FAIL] visibility contract."""
    monkeypatch.setattr(fresh.os.path, "expanduser",
                        lambda p: "/does/not/exist/nope/signal_intelligence.db")

    fresh.log_directional_scan_runs("2026-07-21")

    out = capsys.readouterr().out
    assert "[SIGNAL_LOG_FAIL]" in out
    assert "COT_BULL" in out


def test_the_sentinel_cannot_collide_with_a_real_vehicle(fresh):
    """The real vehicles are XOP/USO/GLD/WEAT/CORN — the sentinel must not look
    like any ticker, or a consumer could mistake liveness for a position."""
    assert fresh.RUN_SENTINEL_VEHICLE.startswith("__")
    assert not fresh.RUN_SENTINEL_VEHICLE.isalnum()
