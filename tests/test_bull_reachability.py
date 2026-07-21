"""
Reproduction / regression tests for the reported symptom:
  "COT_BULL one-sided staleness vs fresh COT_BEAR"

Goal: determine whether detect_signals contains a *code-level* directional
asymmetry that systematically suppresses BULL signals, or whether BULL is
reachable for every commodity and the observed silence is data-driven.

Source of truth for intended direction is the module docstring backtest table:
  Crude BULL = commercial 80th pct -> BUY XOP
  Crude BEAR = commercial 20th pct -> SHORT USO
  Gold  BULL = commercial 80th pct -> BUY GLD
  Wheat BULL = commercial 80th pct -> BUY WEAT
  Corn  BULL = commercial 80th pct -> BUY CORN
i.e. HIGH commercial percentile -> BULL for ALL commodities (including the
'inverse'-tagged Crude/Gold). The 'direction' config field is therefore
expected to be vestigial; ignoring it is correct, not a bug.

History construction: 100 older weeks with net_commercial = 0..99. A latest
value of 90 lands at the 90th percentile (BULL zone, >=80); a latest value of
10 lands at the 10th percentile (BEAR zone, <=20).
"""
import pytest

HIST_0_TO_99 = list(range(100))          # 100 older weeks
LATEST_90TH = 90                          # compute_percentile -> 90.0 (BULL)
LATEST_10TH = 10                          # compute_percentile -> 10.0 (BEAR)
LATEST_96TH = 96                          # compute_percentile -> 96.0 (95+ zone)


def _run(scanner, conn, commodity, latest_net):
    from conftest import insert_history
    rd = insert_history(conn, commodity, latest_net, HIST_0_TO_99)
    sigs = scanner.detect_signals(conn, rd)
    return [s for s in sigs if s['commodity'] == commodity]


# ---------------------------------------------------------------------------
# BULL reachability -- every commodity must emit a BULL/BUY at the 90th pct.
# This is the core of the investigation: if BULL were structurally suppressed
# for any commodity, that commodity would return no BULL signal here.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('commodity,vehicle', [
    ('WTI Crude Oil', 'XOP'),
    ('Gold',          'GLD'),
    ('Wheat (SRW)',   'WEAT'),
    ('Corn',          'CORN'),
])
def test_bull_reachable_at_90th_percentile(scanner, conn, commodity, vehicle):
    got = _run(scanner, conn, commodity, LATEST_90TH)
    assert len(got) == 1, f'{commodity}: expected exactly one BULL signal, got {got}'
    s = got[0]
    assert s['signal_dir'] == 'BULL', f'{commodity}: expected BULL, got {s["signal_dir"]}'
    assert s['ib_direction'] == 'BUY', f'{commodity}: expected BUY, got {s["ib_direction"]}'
    assert s['vehicle'] == vehicle, f'{commodity}: expected {vehicle}, got {s["vehicle"]}'
    assert s['percentile'] == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# BEAR side -- documents the (intended) asymmetry in *vehicle availability*:
# Crude/Wheat have a validated BEAR vehicle; Gold/Corn BEAR is dropped
# (`continue`). This asymmetry is on the BEAR side, not the BULL side.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('commodity,vehicle,ib', [
    ('WTI Crude Oil', 'USO',  'SHORT'),
    ('Wheat (SRW)',   'WEAT', 'SHORT'),
])
def test_bear_emitted_for_crude_and_wheat(scanner, conn, commodity, vehicle, ib):
    got = _run(scanner, conn, commodity, LATEST_10TH)
    assert len(got) == 1
    assert got[0]['signal_dir'] == 'BEAR'
    assert got[0]['ib_direction'] == ib
    assert got[0]['vehicle'] == vehicle


@pytest.mark.parametrize('commodity', ['Gold', 'Corn'])
def test_bear_dropped_for_gold_and_corn(scanner, conn, commodity):
    # No validated bear vehicle -> detect_signals hits `continue`, emits nothing.
    got = _run(scanner, conn, commodity, LATEST_10TH)
    assert got == []


# ---------------------------------------------------------------------------
# The ONLY BULL suppression is the documented Path-B 95th+ safety rule for
# Gold and Corn -- not a systematic asymmetry. Confirm it is scoped to those
# two commodities at the extreme, and that Crude/Wheat BULL still fire at 96th.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('commodity', ['Gold', 'Corn'])
def test_bull_suppressed_only_at_95plus_for_gold_and_corn(scanner, conn, commodity):
    got = _run(scanner, conn, commodity, LATEST_96TH)
    assert got == [], f'{commodity}: expected 95+ Path-B suppression, got {got}'


@pytest.mark.parametrize('commodity,vehicle', [
    ('WTI Crude Oil', 'XOP'),
    ('Wheat (SRW)',   'WEAT'),
])
def test_bull_still_fires_at_96th_for_crude_and_wheat(scanner, conn, commodity, vehicle):
    got = _run(scanner, conn, commodity, LATEST_96TH)
    assert len(got) == 1
    assert got[0]['signal_dir'] == 'BULL'
    assert got[0]['vehicle'] == vehicle


# ---------------------------------------------------------------------------
# Threshold symmetry: the BULL (>=80) and BEAR (<=20) gates are mirror images.
# A value at the 50th pct produces nothing; the BULL boundary (exactly 80) is
# inclusive just as the BEAR boundary (exactly 20) is.
# ---------------------------------------------------------------------------
def test_no_signal_midrange(scanner, conn):
    got = _run(scanner, conn, 'WTI Crude Oil', 50)  # pct 50.0
    assert got == []


def test_bull_boundary_inclusive_at_80(scanner, conn):
    got = _run(scanner, conn, 'WTI Crude Oil', 80)  # pct exactly 80.0
    assert len(got) == 1 and got[0]['signal_dir'] == 'BULL'


def test_bear_boundary_inclusive_at_20(scanner, conn):
    got = _run(scanner, conn, 'WTI Crude Oil', 20)  # pct exactly 20.0
    assert len(got) == 1 and got[0]['signal_dir'] == 'BEAR'
