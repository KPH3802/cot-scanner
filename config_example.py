# COT Scanner Configuration
# DO NOT commit this file to GitHub

# --- Database ---
DB_NAME = 'cot_scanner.db'

# --- Signal thresholds ---
BULL_THRESHOLD  = 80   # commercial net long >= 80th percentile
BEAR_THRESHOLD  = 20   # commercial net long <= 20th percentile
LOOKBACK_WEEKS  = 156  # 3-year rolling window

# --- Commodity universe ---
COMMODITIES = {
    'WTI Crude Oil': {'codes': ['067651', '06765A'], 'etf': 'USO', 'direction': 'inverse'},
    'Gold':          {'codes': ['088691'],            'etf': 'GLD', 'direction': 'inverse'},
    'Wheat (SRW)':   {'codes': ['001602'],            'etf': 'WEAT','direction': 'long'},
    'Corn':          {'codes': ['002602'],            'etf': 'CORN','direction': 'long'},
}

# --- Validated vehicles ---
CRUDE_BULL_VEHICLE    = 'XOP'
CRUDE_BULL_HOLD_WEEKS = 13
CRUDE_BEAR_VEHICLE    = 'USO'
CRUDE_BEAR_HOLD_WEEKS = 8

# --- Regime filters ---
CRUDE_OVX_MIN  = 40     # Only trade crude when OVX >= 40
GOLD_GVZ_MIN   = 15     # Only trade gold when GVZ >= 15
WHEAT_RVOL_MAX = 0.23   # Only trade wheat when realized vol <= 23%
CORN_RVOL_MAX  = 0.196  # Only trade corn when realized vol <= 19.6%

# --- Email ---
EMAIL_SENDER    = 'kph3802@gmail.com'
EMAIL_RECIPIENT = 'kph3802@gmail.com'
EMAIL_PASSWORD  = 'app_password_here'
SMTP_SERVER     = 'smtp.gmail.com'
SMTP_PORT       = 587
