#!/usr/bin/env python3
"""
COT Report Scanner
==================
Weekly scanner for CFTC Commitments of Traders signals.
Runs Friday nights on PythonAnywhere after CFTC publishes (~3:30 PM ET).

Validated signals (backtest 2015-2026, regime filters applied):
  Crude BULL  (OVX>=40, commercial 80th pct): BUY XOP,  13w hold, +9.68% alpha t=3.11***
  Crude BEAR  (OVX>=40, commercial 20th pct): SHORT USO,  8w hold, -10.07% alpha t=-5.52***
  Gold  BULL  (GVZ>=15, commercial 80th pct): BUY GLD,   8w hold, +1.70% alpha t=2.55***
  Wheat BULL  (rvol<=23%, commercial 80th pct): BUY WEAT, 8w hold, +2.92% alpha t=3.30***
  Corn  BULL  (rvol<=19.6%, commercial 80th pct): BUY CORN, 13w hold, +2.21% alpha t=1.80**

Natural gas: NO EDGE -- not deployed.

IB AutoTrader email subject format:
  'COT BULL: XOP, GLD | COT BEAR: USO'

Usage:
  python3 cot_scanner.py              # Normal weekly run
  python3 cot_scanner.py --test-email # Send test email
  python3 cot_scanner.py --status     # Show DB stats
  python3 cot_scanner.py --force      # Re-run even if this week already processed
  python3 cot_scanner.py --dry-run    # Detect signals, skip email
"""

import os
import sys
import csv
import io
import json
import sqlite3
import smtplib
import argparse
import time
from datetime import datetime, timedelta, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.request import Request, urlopen, urlretrieve
from urllib.error import HTTPError, URLError

import config

# ============================================================
# CONSTANTS
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(SCRIPT_DIR, config.DB_NAME)

# CFTC deahistfo URL -- annual zip files containing all COT data
# Format: https://www.cftc.gov/dea/newcot/deacot{YEAR}.zip
# Also: https://www.cftc.gov/dea/newcot/deahistfo.txt (full history flat file)
CFTC_CURRENT_URL = 'https://www.cftc.gov/dea/newcot/deacot{year}.zip'
CFTC_ANNUAL_URL  = 'https://www.cftc.gov/dea/newcot/deacot{year}.zip'

# Contract codes -> commodity name
CONTRACT_CODES = {}
for name, cfg in config.COMMODITIES.items():
    for code in cfg['codes']:
        CONTRACT_CODES[code] = name

# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS cot_data (
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date     TEXT NOT NULL,
            commodity       TEXT NOT NULL,
            direction       TEXT NOT NULL,
            vehicle         TEXT NOT NULL,
            hold_weeks      INTEGER NOT NULL,
            percentile      REAL,
            net_commercial  INTEGER,
            regime_ok       INTEGER DEFAULT 1,
            regime_note     TEXT,
            detected_date   TEXT NOT NULL,
            emailed         INTEGER DEFAULT 0,
            UNIQUE(report_date, commodity, direction)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS processed_weeks (
            report_date TEXT PRIMARY KEY,
            signals_found INTEGER DEFAULT 0,
            processed_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_date   TEXT,
            report_date TEXT,
            signals     INTEGER DEFAULT 0,
            emailed     INTEGER DEFAULT 0,
            errors      TEXT
        )
    """)
    conn.commit()
    return conn

# ============================================================
# CFTC DATA FETCH
# ============================================================

def fetch_cftc_year(year):
    """Download CFTC annual COT zip, return parsed rows as list of dicts."""
    import zipfile
    url = f'https://www.cftc.gov/files/dea/history/deacot{year}.zip'
    try:
        req = Request(url)
        req.add_header('User-Agent', 'COTScanner/1.0 kph3802@gmail.com')
        with urlopen(req, timeout=60) as resp:
            data = resp.read()
        zf = zipfile.ZipFile(io.BytesIO(data))
        # The zip contains a .txt file
        txt_name = [n for n in zf.namelist() if n.endswith('.txt')][0]
        content = zf.read(txt_name).decode('latin-1')
        rows = list(csv.DictReader(io.StringIO(content)))
        return rows
    except Exception as e:
        print(f'  CFTC fetch failed for {year}: {e}')
        return []

def parse_cot_row(row):
    """Extract fields from a raw CFTC COT CSV row."""
    code = row.get('CFTC Contract Market Code', '').strip()
    date_str = row.get('As of Date in Form YYYY-MM-DD', '') or row.get('Report_Date_as_YYYY-MM-DD', '')
    if not date_str:
        # Try alternative field name
        for k in row:
            if 'date' in k.lower() and 'yyyy' in k.lower():
                date_str = row[k]
                break
    long_comm  = _int(row.get('Commercial Long', row.get('Comm_Positions_Long_All', 0)))
    short_comm = _int(row.get('Commercial Short', row.get('Comm_Positions_Short_All', 0)))
    oi         = _int(row.get('Open Interest', row.get('Open_Interest_All', 0)))
    net        = (long_comm or 0) - (short_comm or 0)
    return {
        'report_date':      date_str.strip() if date_str else '',
        'contract_code':    code,
        'net_commercial':   net,
        'long_commercial':  long_comm,
        'short_commercial': short_comm,
        'open_interest':    oi,
    }

def _int(v):
    try: return int(str(v).replace(',', ''))
    except: return 0

def store_cot_rows(conn, rows):
    """Filter rows to tracked commodities and store in DB."""
    c = conn.cursor()
    new = 0
    collected_at = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
    for row in rows:
        parsed = parse_cot_row(row)
        code   = parsed['contract_code']
        if code not in CONTRACT_CODES:
            continue
        commodity = CONTRACT_CODES[code]
        if not parsed['report_date']:
            continue
        try:
            c.execute("""
                INSERT OR IGNORE INTO cot_data
                (report_date, commodity, contract_code, net_commercial,
                 long_commercial, short_commercial, open_interest, collected_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                parsed['report_date'], commodity, code,
                parsed['net_commercial'], parsed['long_commercial'],
                parsed['short_commercial'], parsed['open_interest'],
                collected_at
            ))
            if c.rowcount > 0:
                new += 1
        except Exception as e:
            pass
    conn.commit()
    return new

def get_latest_report_date(conn):
    """Get the most recent report_date in our DB."""
    c = conn.cursor()
    c.execute('SELECT MAX(report_date) FROM cot_data')
    row = c.fetchone()
    return row[0] if row and row[0] else None

def update_cot_db(conn, backfill=False):
    year = datetime.utcnow().year
    if backfill:
        print("BACKFILL: fetching 2015-" + str(year) + "...")
        for y in range(2015, year + 1):
            rows = fetch_cftc_year(y)
            if rows:
                n = store_cot_rows(conn, rows)
                print(str(y) + ": " + str(n) + " records")
            time.sleep(1)
        return
    rows = fetch_cftc_year(year)
    if rows:
        n = store_cot_rows(conn, rows)
        print("Stored " + str(n) + " new COT records")

# ============================================================
# SIGNAL LOGIC
# ============================================================

def get_rolling_data(conn, commodity, lookback_weeks=156):
    """Return the last N weeks of net_commercial for a commodity."""
    c = conn.cursor()
    c.execute("""
        SELECT report_date, net_commercial
        FROM cot_data
        WHERE commodity = ?
        ORDER BY report_date DESC
        LIMIT ?
    """, (commodity, lookback_weeks))
    rows = c.fetchall()
    return rows  # [(date, net), ...] newest first

def compute_percentile(value, series):
    """Rank value within series (0-100)."""
    if not series:
        return None
    below = sum(1 for v in series if v < value)
    return round(100.0 * below / len(series), 1)

def get_regime_vars():
    """Fetch current OVX, GVZ, and realized vols via yfinance."""
    try:
        import yfinance as yf
        ovx  = yf.Ticker('^OVX').history(period='5d')['Close'].iloc[-1]
        gvz  = yf.Ticker('^GVZ').history(period='5d')['Close'].iloc[-1]
    except Exception:
        ovx, gvz = None, None

    # Realized vol for wheat and corn (21-day annualized)
    def rvol(ticker):
        try:
            import yfinance as yf, numpy as np
            hist = yf.Ticker(ticker).history(period='60d')['Close']
            returns = hist.pct_change().dropna()
            return float(returns.tail(21).std() * (252**0.5))
        except:
            return None

    return {
        'OVX': float(ovx) if ovx is not None else None,
        'GVZ': float(gvz) if gvz is not None else None,
        'WEAT_RVOL': rvol('WEAT'),
        'CORN_RVOL': rvol('CORN'),
    }

def detect_signals(conn, report_date):
    """
    For each commodity, compute rolling percentile and check regime filters.
    Returns list of signal dicts.
    """
    # PATH B SCORING RULES (derived Apr 10 2026 backtest):
    # Gold BULL:  suppress at 95th+ (inverse gradient -- overextended = mean revert)
    # Corn:       suppress at 95th+ (non-monotonic -- sweet spot is 80-95th)
    # Wheat BEAR: gradient confirmed at 95th+ (p=0.003) -- deploy scoring when Wheat BEAR vehicle added
    # All others: flat Score 3 (no gradient detected)
    signals = []
    regime  = get_regime_vars()
    print(f'  Regime: OVX={regime["OVX"]}, GVZ={regime["GVZ"]}, '
          f'WEAT_RVOL={regime["WEAT_RVOL"]}, CORN_RVOL={regime["CORN_RVOL"]}')

    for commodity, cfg in config.COMMODITIES.items():
        etf       = cfg['etf']
        direction = cfg['direction']  # 'long' or 'inverse'

        rows = get_rolling_data(conn, commodity, config.LOOKBACK_WEEKS)
        if len(rows) < 20:
            print(f'  {commodity}: insufficient data ({len(rows)} weeks)')
            continue

        # Latest net commercial
        latest_date, latest_net = rows[0]
        if latest_date != report_date:
            print(f'  {commodity}: latest data is {latest_date}, target {report_date} -- skipping')
            continue

        # Compute percentile against lookback window (excluding current)
        hist_series = [r[1] for r in rows[1:]]
        pct = compute_percentile(latest_net, hist_series)

        bull_threshold = config.BULL_THRESHOLD  # 80
        bear_threshold = config.BEAR_THRESHOLD  # 20

        signal_direction = None
        if pct is not None and pct >= bull_threshold:
            # Commercial extreme long -> BULL in the direction of the commodity
            if direction == 'long':
                signal_direction = 'BULL'  # buy ETF
            else:
                signal_direction = 'BULL'  # still BULL -- vehicle handles direction
        elif pct is not None and pct <= bear_threshold:
            if direction == 'long':
                signal_direction = 'BEAR'
            else:
                signal_direction = 'BEAR'

        if signal_direction is None:
            print(f'  {commodity}: pct={pct:.1f}% -- no signal')
            continue

        # PATH B SAFETY: Gold BULL at 95th+ percentile mean-reverts (p=0.063, inverse gradient)
        # Overextended commercial longs in gold = exhaustion, not momentum. Suppress.
        if commodity == 'Gold' and signal_direction == 'BULL' and pct >= 95:
            print(f'  {commodity}: pct={pct:.1f}% BULL -> SUPPRESSED (95th+ inverse gradient, Path B Apr 2026)')
            continue

        # PATH B SAFETY: Corn at 95th+ non-monotonic -- 90-95th is sweet spot, 95th+ collapses
        # Corn BULL p=0.002*** at 90-95th but negative at 95th+. Suppress extremes.
        if commodity == 'Corn' and pct >= 95:
            print(f'  {commodity}: pct={pct:.1f}% {signal_direction} -> SUPPRESSED (95th+ collapse, Path B Apr 2026)')
            continue

        # Assign vehicle and hold weeks
        vehicle    = None
        hold_weeks = None
        ib_dir     = None  # BUY or SHORT for IB

        if commodity == 'WTI Crude Oil':
            if signal_direction == 'BULL':
                vehicle, hold_weeks, ib_dir = config.CRUDE_BULL_VEHICLE, config.CRUDE_BULL_HOLD_WEEKS, 'BUY'
            else:
                vehicle, hold_weeks, ib_dir = config.CRUDE_BEAR_VEHICLE, config.CRUDE_BEAR_HOLD_WEEKS, 'SHORT'
        elif commodity == 'Gold':
            if signal_direction == 'BULL':
                vehicle, hold_weeks, ib_dir = 'GLD', 8, 'BUY'
            else:
                continue  # No validated bear vehicle for gold
        elif commodity == 'Wheat (SRW)':
            if signal_direction == 'BULL':
                vehicle, hold_weeks, ib_dir = 'WEAT', 8, 'BUY'
            else:
                continue
        elif commodity == 'Corn':
            if signal_direction == 'BULL':
                vehicle, hold_weeks, ib_dir = 'CORN', 13, 'BUY'
            else:
                continue
        else:
            continue  # Natural gas etc

        # Regime filter
        regime_ok   = True
        regime_note = ''
        if commodity == 'WTI Crude Oil':
            if regime['OVX'] is not None and regime['OVX'] < config.CRUDE_OVX_MIN:
                regime_ok   = False
                regime_note = f'OVX={regime["OVX"]:.1f} < {config.CRUDE_OVX_MIN} (low vol, skip)'
        elif commodity == 'Gold':
            if regime['GVZ'] is not None and regime['GVZ'] < config.GOLD_GVZ_MIN:
                regime_ok   = False
                regime_note = f'GVZ={regime["GVZ"]:.1f} < {config.GOLD_GVZ_MIN} (low vol, skip)'
        elif commodity == 'Wheat (SRW)':
            if regime['WEAT_RVOL'] is not None and regime['WEAT_RVOL'] > config.WHEAT_RVOL_MAX:
                regime_ok   = False
                regime_note = f'WEAT_RVOL={regime["WEAT_RVOL"]:.3f} > {config.WHEAT_RVOL_MAX} (high vol, skip)'
        elif commodity == 'Corn':
            if regime['CORN_RVOL'] is not None and regime['CORN_RVOL'] > config.CORN_RVOL_MAX:
                regime_ok   = False
                regime_note = f'CORN_RVOL={regime["CORN_RVOL"]:.3f} > {config.CORN_RVOL_MAX} (high vol, skip)'

        status = 'SIGNAL' if regime_ok else 'BLOCKED'
        print(f'  {commodity}: pct={pct:.1f}% {signal_direction} -> {vehicle} {ib_dir} [{status}] {regime_note}')

        signals.append({
            'report_date':    report_date,
            'commodity':      commodity,
            'signal_dir':     signal_direction,
            'vehicle':        vehicle,
            'hold_weeks':     hold_weeks,
            'ib_direction':   ib_dir,
            'percentile':     pct,
            'net_commercial': latest_net,
            'regime_ok':      regime_ok,
            'regime_note':    regime_note,
        })

    return signals

# ============================================================
# DEDUP + STORE
# ============================================================

def is_week_processed(conn, report_date):
    c = conn.cursor()
    c.execute('SELECT report_date FROM processed_weeks WHERE report_date=?', (report_date,))
    return c.fetchone() is not None

def mark_week_processed(conn, report_date, signals_found):
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO processed_weeks (report_date, signals_found, processed_at) VALUES (?,?,?)',
              (report_date, signals_found, datetime.utcnow().strftime('%Y-%m-%d %H:%M')))
    conn.commit()

def store_signal(conn, sig):
    c = conn.cursor()
    try:
        c.execute("""
            INSERT OR IGNORE INTO signals
            (report_date, commodity, direction, vehicle, hold_weeks,
             percentile, net_commercial, regime_ok, regime_note, detected_date, emailed)
            VALUES (?,?,?,?,?,?,?,?,?,?,0)
        """, (
            sig['report_date'], sig['commodity'], sig['signal_dir'],
            sig['vehicle'], sig['hold_weeks'], sig['percentile'],
            sig['net_commercial'], 1 if sig['regime_ok'] else 0,
            sig['regime_note'], datetime.utcnow().strftime('%Y-%m-%d')
        ))
        conn.commit()
        return c.rowcount > 0
    except Exception as e:
        print(f'  store_signal error: {e}')
        return False

# ============================================================
# EMAIL
# ============================================================

DIRECTION_COLOR = {'BULL': '#00c853', 'BEAR': '#f44336'}
HOLD_MAP = {8: '8 weeks', 13: '13 weeks', 26: '26 weeks'}

def build_email_subject(tradeable):
    """Build subject parseable by IB AutoTrader.
    Format: 'COT BULL: XOP, GLD | COT BEAR: USO'
    """
    bulls = [s['vehicle'] for s in tradeable if s['ib_direction'] == 'BUY']
    bears = [s['vehicle'] for s in tradeable if s['ib_direction'] == 'SHORT']
    parts = []
    if bulls: parts.append(f'COT BULL: {chr(44).join(bulls)}')
    if bears: parts.append(f'COT BEAR: {chr(44).join(bears)}')
    return ' | '.join(parts) if parts else f'COT Scanner -- No signals'

def build_email_html(signals, report_date, recent):
    today = datetime.utcnow().strftime('%Y-%m-%d')
    tradeable = [s for s in signals if s['regime_ok']]
    blocked   = [s for s in signals if not s['regime_ok']]

    html = f"""<!DOCTYPE html>
<html><head><style>
body{{font-family:'Segoe UI',Arial,sans-serif;background:#1a1a2e;color:#e0e0e0;margin:0;padding:0}}
.wrap{{max-width:700px;margin:0 auto;padding:20px}}
h1{{color:#ff9800;font-size:22px;border-bottom:2px solid #333;padding-bottom:10px;margin-top:0}}
h2{{font-size:17px;margin-top:24px;margin-bottom:10px}}
.summary{{background:#16213e;border-radius:8px;padding:14px;margin:14px 0;font-size:14px}}
.card{{background:#16213e;border-radius:8px;padding:14px;margin:10px 0}}
.card-bull{{border-left:4px solid #00c853}}
.card-bear{{border-left:4px solid #f44336}}
.card-blocked{{border-left:4px solid #555;opacity:0.7}}
.ticker{{font-size:20px;font-weight:bold;color:#fff}}
.badge{{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:bold;color:#fff;margin-left:8px;vertical-align:middle}}
.metrics{{display:flex;gap:16px;margin:10px 0;flex-wrap:wrap}}
.metric{{text-align:center}}
.mv{{font-size:16px;font-weight:bold}}
.ml{{font-size:11px;color:#888}}
.meta{{font-size:12px;color:#aaa;margin-top:6px}}
.blocked-label{{color:#888;font-size:12px;font-style:italic}}
.backtest{{background:#0d2137;border:1px solid #1a5276;border-radius:8px;padding:12px;margin:20px 0;font-size:12px;color:#7fb3d8}}
table{{width:100%;border-collapse:collapse;margin-top:8px}}
th{{background:#0f3460;color:#e0e0e0;padding:7px;text-align:left;font-size:12px}}
td{{padding:7px;border-bottom:1px solid #333;font-size:12px}}
.footer{{color:#555;font-size:11px;margin-top:28px;border-top:1px solid #333;padding-top:10px}}
</style></head><body><div class='wrap'>
<h1>COT REPORT SCANNER &mdash; {today}</h1>
<div class='summary'>
  CFTC report date: <strong>{report_date}</strong> &nbsp;|&nbsp;
  Tradeable: <strong>{len(tradeable)}</strong> &nbsp;|&nbsp;
  Regime-blocked: <strong>{len(blocked)}</strong>
</div>"""

    if tradeable:
        html += "<h2 style='color:#00e676;'>&#9654; TRADEABLE SIGNALS</h2>"
        for s in tradeable:
            col = '#00c853' if s['ib_direction']=='BUY' else '#f44336'
            css = 'card-bull' if s['ib_direction']=='BUY' else 'card-bear'
            hold_str = HOLD_MAP.get(s['hold_weeks'], f"{s['hold_weeks']}w")
            html += f"""
<div class='card {css}'>
  <span class='ticker'>{s['vehicle']}</span>
  <span class='badge' style='background:{col};'>{s['ib_direction']}</span>
  <div class='metrics'>
    <div class='metric'><div class='mv' style='color:{col};'>{s['percentile']:.0f}th pct</div><div class='ml'>COMM NET LONG</div></div>
    <div class='metric'><div class='mv'>{hold_str}</div><div class='ml'>HOLD PERIOD</div></div>
    <div class='metric'><div class='mv'>{s['signal_dir']}</div><div class='ml'>SIGNAL</div></div>
  </div>
  <div class='meta'>{s['commodity']} &bull; Net commercial: {s['net_commercial']:,}</div>
</div>"""

    if blocked:
        html += "<h2 style='color:#888;'>&#8203; REGIME-BLOCKED (signal exists, filter failed)</h2>"
        for s in blocked:
            col = '#888'
            hold_str = HOLD_MAP.get(s['hold_weeks'], f"{s['hold_weeks']}w")
            html += f"""
<div class='card card-blocked'>
  <span class='ticker' style='color:#888;'>{s['vehicle']}</span>
  <span class='badge' style='background:#555;'>{s['ib_direction']} BLOCKED</span>
  <div class='meta'>{s['commodity']} &bull; pct={s['percentile']:.0f}% &bull; {s['regime_note']}</div>
</div>"""

    html += """
<div class='backtest'>
  <strong>Backtest Reference (2015-2026, regime filters applied):</strong><br>
  Crude BULL (OVX&ge;40): BUY XOP 13w +9.68% t=3.11*** &nbsp;|&nbsp;
  Crude BEAR (OVX&ge;40): SHORT USO 8w -10.07% t=-5.52***<br>
  Gold BULL (GVZ&ge;15): BUY GLD 8w +1.70% t=2.55*** &nbsp;|&nbsp;
  Wheat BULL (rvol&le;23%): BUY WEAT 8w +2.92% t=3.30***<br>
  Corn BULL (rvol&le;19.6%): BUY CORN 13w +2.21% t=1.80**
</div>"""

    if recent:
        html += """
<h2 style='color:#64b5f6;'>Recent Signal History</h2>
<table>
<tr><th>Date</th><th>Commodity</th><th>Vehicle</th><th>Direction</th><th>Pct</th><th>Regime</th></tr>"""
        for r in recent[:15]:
            rc = '#00c853' if r[3]=='BUY' else '#f44336'
            ok = 'OK' if r[6] else 'BLOCKED'
            html += f"<tr><td>{r[0]}</td><td>{r[1]}</td><td><b>{r[2]}</b></td><td style='color:{rc};'>{r[3]}</td><td>{r[4]:.0f}%</td><td>{ok}</td></tr>"
        html += '</table>'

    html += f"""
<div class='footer'>
  COT Scanner v1.0 &nbsp;|&nbsp; CFTC Commitments of Traders (free) &nbsp;|&nbsp;
  IB AutoTrader subject: 'COT BULL: XOP | COT BEAR: USO'<br>
  Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
</div>
</div></body></html>"""
    return html

def send_email(subject, html_body):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = config.EMAIL_SENDER
    msg['To']      = config.EMAIL_RECIPIENT
    msg.attach(MIMEText(html_body, 'html'))
    try:
        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as srv:
            srv.starttls()
            srv.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
            srv.sendmail(config.EMAIL_SENDER, config.EMAIL_RECIPIENT, msg.as_string())
        print('  Email sent successfully')
        return True
    except Exception as e:
        print(f'  Email error: {e}')
        return False

def get_recent_signals(conn, n=20):
    c = conn.cursor()
    c.execute("""
        SELECT report_date, commodity, vehicle, direction, percentile, hold_weeks, regime_ok
        FROM signals ORDER BY detected_date DESC, report_date DESC LIMIT ?
    """, (n,))
    return c.fetchall()

def log_scan(conn, report_date, signals, emailed, errors=''):
    c = conn.cursor()
    c.execute('INSERT INTO scan_log (scan_date, report_date, signals, emailed, errors) VALUES (?,?,?,?,?)',
              (datetime.utcnow().strftime('%Y-%m-%d %H:%M'), report_date, signals, 1 if emailed else 0, errors))
    conn.commit()

# ============================================================
# MAIN
# ============================================================

def run_scan(force=False, dry_run=False, backfill=False):
    print(f"{'='*60}")
    print(f'COT SCANNER -- {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}')
    print(f"{'='*60}")

    conn = init_db()

    # Step 1: Update COT DB from CFTC
    update_cot_db(conn, backfill=backfill)

    # Step 2: Get latest report date
    report_date = get_latest_report_date(conn)
    if not report_date:
        print('ERROR: No COT data in DB after update')
        conn.close()
        return
    print(f'Latest report date in DB: {report_date}')

    # Step 3: Check if already processed
    if not force and is_week_processed(conn, report_date):
        print(f'Week {report_date} already processed.')
        conn.close()
        return

    # Step 4: Detect signals
    print('Detecting signals...')
    all_signals = detect_signals(conn, report_date)

    tradeable = [s for s in all_signals if s['regime_ok']]
    blocked   = [s for s in all_signals if not s['regime_ok']]
    print(f'Signals: {len(tradeable)} tradeable, {len(blocked)} regime-blocked')

    # Step 5: Store
    for s in all_signals:
        store_signal(conn, s)

    # Step 6: Email
    recent = get_recent_signals(conn)
    email_sent = False
    if all_signals:
        subject = build_email_subject(tradeable)
        html    = build_email_html(all_signals, report_date, recent)
        if dry_run:
            print(f'DRY RUN: subject would be: {subject}')
        else:
            print(f'Subject: {subject}')
            email_sent = send_email(subject, html)
    else:
        subj = f'COT Scanner -- No signals ({report_date})'
        html = build_email_html([], report_date, recent)
        if not dry_run:
            send_email(subj, html)

    mark_week_processed(conn, report_date, len(all_signals))
    log_scan(conn, report_date, len(all_signals), email_sent)

    print(f"{'='*60}")
    print(f'COMPLETE: {len(tradeable)} tradeable, {len(blocked)} blocked')
    print(f"{'='*60}")
    conn.close()

# ============================================================
# CLI
# ============================================================

def show_status():
    conn = init_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM cot_data')
    total_cot = c.fetchone()[0]
    c.execute('SELECT MIN(report_date), MAX(report_date) FROM cot_data')
    dr = c.fetchone()
    c.execute('SELECT COUNT(*) FROM signals')
    total_sigs = c.fetchone()[0]
    c.execute('SELECT * FROM scan_log ORDER BY id DESC LIMIT 5')
    scans = c.fetchall()
    c.execute('SELECT report_date, commodity, vehicle, direction, percentile, hold_weeks, regime_ok FROM signals ORDER BY detected_date DESC LIMIT 10')
    recent = c.fetchall()
    print(f"{'='*50}")
    print('COT SCANNER STATUS')
    print(f"{'='*50}")
    print(f'COT records: {total_cot} ({dr[0]} to {dr[1]})')
    print(f'Signals: {total_sigs}')
    if scans:
        print('Last scans:')
        for s in scans:
            print(f'  {s[1]} | signals:{s[3]} | emailed:{s[4]}')
    if recent:
        print('Recent signals:')
        for r in recent:
            ok = 'OK' if r[6] else 'BLK'
            print(f'  {r[0]}  {r[1]:<18} {r[2]:<6} {r[3]:5} pct:{r[4]:.0f}% {r[5]}w [{ok}]')
    conn.close()

def send_test_email():
    html = f"""<html><body style='font-family:Arial;background:#1a1a2e;color:#e0e0e0;padding:20px;'>
<h1 style='color:#ff9800;'>COT Scanner -- Test Email</h1>
<p>Configuration working correctly.</p>
<ul>
  <li>BULL threshold: {config.BULL_THRESHOLD}th percentile</li>
  <li>BEAR threshold: {config.BEAR_THRESHOLD}th percentile</li>
  <li>Lookback: {config.LOOKBACK_WEEKS} weeks</li>
  <li>Commodities: {', '.join(config.COMMODITIES.keys())}</li>
</ul>
<p>IB AutoTrader subject: 'COT BULL: XOP, GLD | COT BEAR: USO'</p>
<p style='color:#666;'>Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
</body></html>"""
    send_email('COT Scanner -- Test Email', html)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='COT Report Scanner')
    parser.add_argument('--test-email', action='store_true')
    parser.add_argument('--status',     action='store_true')
    parser.add_argument('--force',      action='store_true')
    parser.add_argument('--backfill',   action='store_true', help='Fetch all years 2015-present')
    parser.add_argument('--dry-run',    action='store_true')
    args = parser.parse_args()

    if args.test_email:
        send_test_email()
    elif args.status:
        show_status()
    else:
        run_scan(force=args.force, dry_run=args.dry_run, backfill=args.backfill)

