"""
Offline test harness for cot_scanner.

cot_scanner does `import config` at module import time and get_regime_vars()
hits yfinance/network. To run detect_signals fully offline we:
  1. Inject a synthetic `config` module into sys.modules BEFORE importing
     cot_scanner (mirrors config_example.py placeholder values -- NO secrets).
  2. Import cot_scanner once and expose it as a fixture.

No real config.py is created; nothing here contains a real credential.
"""
import sys
import types
import sqlite3
import importlib

import pytest


def _make_config():
    """Build an in-memory config module mirroring config_example.py."""
    cfg = types.ModuleType('config')
    cfg.DB_NAME = ':memory:'
    cfg.BULL_THRESHOLD = 80
    cfg.BEAR_THRESHOLD = 20
    cfg.LOOKBACK_WEEKS = 156
    cfg.COMMODITIES = {
        'WTI Crude Oil': {'codes': ['067651', '06765A'], 'etf': 'USO', 'direction': 'inverse'},
        'Gold':          {'codes': ['088691'],            'etf': 'GLD', 'direction': 'inverse'},
        'Wheat (SRW)':   {'codes': ['001602'],            'etf': 'WEAT', 'direction': 'long'},
        'Corn':          {'codes': ['002602'],            'etf': 'CORN', 'direction': 'long'},
    }
    cfg.CRUDE_BULL_VEHICLE = 'XOP'
    cfg.CRUDE_BULL_HOLD_WEEKS = 13
    cfg.CRUDE_BEAR_VEHICLE = 'USO'
    cfg.CRUDE_BEAR_HOLD_WEEKS = 8
    cfg.CRUDE_OVX_MIN = 40
    cfg.GOLD_GVZ_MIN = 15
    cfg.WHEAT_RVOL_MAX = 0.23
    cfg.CORN_RVOL_MAX = 0.196
    # Email placeholders (never used in these tests, no real secret)
    cfg.EMAIL_SENDER = 'placeholder@example.com'
    cfg.EMAIL_RECIPIENT = 'placeholder@example.com'
    cfg.EMAIL_PASSWORD = 'placeholder'
    cfg.SMTP_SERVER = 'smtp.example.com'
    cfg.SMTP_PORT = 587
    return cfg


@pytest.fixture(scope='session')
def scanner():
    sys.modules['config'] = _make_config()
    import cot_scanner
    importlib.reload(cot_scanner)
    return cot_scanner


@pytest.fixture()
def conn(scanner):
    """In-memory sqlite DB with the cot_data schema."""
    c = sqlite3.connect(':memory:')
    c.execute("""
        CREATE TABLE cot_data (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date         TEXT NOT NULL,
            commodity           TEXT NOT NULL,
            contract_code       TEXT,
            net_commercial      INTEGER NOT NULL,
            long_commercial     INTEGER,
            short_commercial    INTEGER,
            open_interest       INTEGER,
            collected_at        TEXT NOT NULL,
            UNIQUE(report_date, commodity)
        )
    """)
    c.commit()
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _no_network_no_side_effects(scanner, monkeypatch):
    """Stub network (regime) and the signal-intelligence side-effect writer."""
    monkeypatch.setattr(scanner, 'get_regime_vars', lambda: {
        'OVX': None, 'GVZ': None, 'WEAT_RVOL': None, 'CORN_RVOL': None,
    })
    monkeypatch.setattr(scanner, 'log_signal_intelligence', lambda *a, **k: None)


def insert_history(conn, commodity, latest_net, hist_values):
    """
    Insert a COT history for `commodity`.

    The latest row (newest report_date) gets `latest_net`; `hist_values` are
    the older weeks. Returns the report_date of the latest row.

    Dates are weekly ISO strings descending from 2026-07-17 so lexical order
    == chronological order (matches get_rolling_data ORDER BY report_date DESC).
    """
    from datetime import date, timedelta
    all_vals = [latest_net] + list(hist_values)  # index 0 = newest
    base = date(2026, 7, 17)
    latest_date = None
    cur = conn.cursor()
    for i, net in enumerate(all_vals):
        d = (base - timedelta(weeks=i)).isoformat()
        if i == 0:
            latest_date = d
        cur.execute(
            "INSERT INTO cot_data (report_date, commodity, contract_code, "
            "net_commercial, long_commercial, short_commercial, open_interest, "
            "collected_at) VALUES (?,?,?,?,?,?,?,?)",
            (d, commodity, 'X', int(net), None, None, None, '2026-07-18 00:00'),
        )
    conn.commit()
    return latest_date
