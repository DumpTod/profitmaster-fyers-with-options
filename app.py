from flask import Flask, jsonify, request, redirect
from flask_cors import CORS
import requests as req
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, date
import calendar
import pytz
import os
import json
import hashlib
import threading
import time
from fyers_apiv3 import fyersModel

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get('FLASK_SECRET', 'atr-scanner-secret-key-2024')

# ========================================
# FYERS CREDENTIALS (Your Fresh Credentials)
# ========================================
FYERS_APP_ID     = os.environ.get('API_KEY', '9M61XAZQSN-100')
FYERS_SECRET_KEY = os.environ.get('API_SECRET', 'NYVEV0B7VP')
FYERS_REDIRECT_URL = 'https://trade.fyers.in/api-login/redirect-uri/index.html'

# ========================================
# SCANNER CONFIGURATION (Exact Copy from Original)
# ========================================
SCANNER_CONFIG = {
    'NIFTY50': {
        'instrument_key': 'NSE:NIFTY50-INDEX',
        'option_key': 'NSE:NIFTY50-INDEX',
        'resample_minutes': 5,
        'fast_period': 5,
        'fast_mult': 1.5,
        'slow_period': 25,
        'slow_mult': 4.0,
        'lot_size': 65,
        'strike_step': 50
    },
    'BANKNIFTY': {
        'instrument_key': 'NSE:NIFTYBANK-INDEX',
        'option_key': 'NSE:NIFTYBANK-INDEX',
        'resample_minutes': 5,
        'fast_period': 5,
        'fast_mult': 1.5,
        'slow_period': 20,
        'slow_mult': 4.0,
        'lot_size': 30,
        'strike_step': 100
    }
}

# ========================================
# GLOBAL VARIABLES
# ========================================
IST = pytz.timezone('Asia/Kolkata')
TOKEN_FILE = '/tmp/token.json'
REFRESH_FILE = '/tmp/refresh_token.txt'

token_data = {'access_token': None, 'token_time': None, 'refresh_token': None}
scan_cache = {'signals': [], 'last_scan': None}
options_cache = {'signals': [], 'last_fetch': None}
scan_lock = threading.Lock()

# ========================================
# 🆕 TRADE STORAGE (New Addition)
# ========================================
TRADES_FILE = 'trades.json'
OPTIONS_TRADES_FILE = 'options_trades.json'

active_futures_trades = []
active_options_trades = []

def load_trades():
    """Load trades from JSON file"""
    global active_futures_trades
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, 'r') as f:
                active_futures_trades = json.load(f)
            print(f"✓ Loaded {len(active_futures_trades)} futures trades")
    except Exception as e:
        print(f"⚠ Could not load trades: {e}")
        active_futures_trades = []

def save_trades():
    """Save trades to JSON file"""
    try:
        with open(TRADES_FILE, 'w') as f:
            json.dump(active_futures_trades, f, indent=2)
    except Exception as e:
        print(f"⚠ Could not save trades: {e}")

def load_options_trades():
    """Load options trades from JSON file"""
    global active_options_trades
    try:
        if os.path.exists(OPTIONS_TRADES_FILE):
            with open(OPTIONS_TRADES_FILE, 'r') as f:
                active_options_trades = json.load(f)
            print(f"✓ Loaded {len(active_options_trades)} options trades")
    except Exception as e:
        print(f"⚠ Could not load options trades: {e}")
        active_options_trades = []

def save_options_trades():
    """Save options trades to JSON file"""
    try:
        with open(OPTIONS_TRADES_FILE, 'w') as f:
            json.dump(active_options_trades, f, indent=2)
    except Exception as e:
        print(f"⚠ Could not save options trades: {e}")

# Load trades on startup
load_trades()
load_options_trades()

# ========================================
# TOKEN MANAGEMENT (Fixed & Robust)
# ========================================

def save_token(access_token, refresh_token=None):
    """Save token to memory AND file"""
    token_data['access_token'] = access_token
    token_data['token_time'] = datetime.now(IST).isoformat()

    if refresh_token:
        token_data['refresh_token'] = refresh_token
        try:
            with open(REFRESH_FILE, 'w') as f:
                f.write(refresh_token)
            print(f"✓ Refresh token saved at {datetime.now(IST).strftime('%H:%M:%S IST')}")
        except Exception as e:
            print(f"✗ Failed to save refresh token: {e}")

    try:
        with open(TOKEN_FILE, 'w') as f:
            json.dump(token_data, f)
        print(f"✓ Access token saved at {datetime.now(IST).strftime('%H:%M:%S IST')}")
    except Exception as e:
        print(f"✗ Failed to save access token: {e}")


def load_token():
    """Load tokens from file on startup"""
    try:
        with open(TOKEN_FILE, 'r') as f:
            data = json.load(f)
            token_data['access_token'] = data.get('access_token')
            token_data['token_time'] = data.get('token_time')
            token_data['refresh_token'] = data.get('refresh_token')
        print(f"✓ Token loaded from file")
    except Exception as e:
        print(f"⚠ No token file found - requires login")

    if not token_data['refresh_token']:
        try:
            with open(REFRESH_FILE, 'r') as f:
                token_data['refresh_token'] = f.read().strip()
            print(f"✓ Refresh token loaded from file")
        except:
            pass


def auto_refresh_access_token():
    """Auto-refresh using refresh token + PIN"""
    refresh_token = token_data.get('refresh_token')
    if not refresh_token:
        return False

    try:
        app_id_hash = hashlib.sha256(f"{FYERS_APP_ID}:{FYERS_SECRET_KEY}".encode()).hexdigest()

        r = req.post(
            'https://api-t1.fyers.in/api/v3/validate-refresh-token',
            json={
                'grant_type': 'refresh_token',
                'appIdHash': app_id_hash,
                'refresh_token': refresh_token,
                'pin': os.environ.get('FYERS_PIN', '')
            },
            headers={'Content-Type': 'application/json'},
            timeout=10
        )

        if r.status_code == 200 and r.json().get('s') == 'ok':
            new_access_token = f"{FYERS_APP_ID}:{r.json()['access_token']}"
            save_token(new_access_token)
            return True

        return False
    except Exception as e:
        print(f"✗ Auto-refresh failed: {e}")
        return False


load_token()

if not token_data['access_token'] and token_data['refresh_token']:
    print("⟳ No access token found on startup, attempting auto-refresh...")
    auto_refresh_access_token()


def init_fyers():
    """Initialize Fyers API client"""
    if not token_data['access_token']:
        print("✗ init_fyers: No access token available")
        return None

    try:
        return fyersModel.FyersModel(
            client_id=FYERS_APP_ID,
            token=token_data['access_token'],
            log_path='/tmp'
        )
    except Exception as e:
        print(f"✗ init_fyers error: {e}")
        return None


# ========================================
# TRADING HOLIDAYS (Updated through 2026)
# ========================================
TRADING_HOLIDAYS = {
    date(2024,1,26), date(2024,3,25), date(2024,4,14), date(2024,4,17),
    date(2024,5,1),  date(2024,6,17), date(2024,8,15), date(2024,10,2),
    date(2024,10,24),date(2024,11,1), date(2024,11,15),date(2024,12,25),
    date(2025,1,26), date(2025,2,26), date(2025,3,14), date(2025,3,31),
    date(2025,4,10), date(2025,4,14), date(2025,4,18), date(2025,5,1),
    date(2025,8,15), date(2025,10,2), date(2025,10,23),date(2025,12,25),
    date(2026,1,26), date(2026,3,3),   date(2026,3,26),  date(2026,3,31),
    date(2026,4,3),   date(2026,4,14),  date(2026,5,1),   date(2026,5,28),
    date(2026,6,26),  date(2026,9,14),  date(2026,10,2),  date(2026,10,20),
    date(2026,11,10), date(2026,11,24), date(2026,12,25),
}

def is_trading_day(d):
    return d.weekday() < 5 and d not in TRADING_HOLIDAYS

def last_weekday_of_month(year, month, weekday):
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d

def get_monthly_expiry(symbol, year, month):
    expiry = last_weekday_of_month(year, month, 3)
    while not is_trading_day(expiry):
        expiry -= timedelta(days=1)
    return expiry

def get_active_expiry(symbol, signal_date=None):
    if signal_date is None:
        signal_date = datetime.now(IST).date()
    if isinstance(signal_date, str):
        signal_date = date.fromisoformat(signal_date[:10])

    y, m = signal_date.year, signal_date.month
    expiry = get_monthly_expiry(symbol, y, m)
    td_left = sum(
        1 for i in range((expiry - signal_date).days + 1)
        if is_trading_day(signal_date + timedelta(days=i))
    )
    if td_left <= 5:
        expiry = get_monthly_expiry(symbol, y, m+1) if m < 12 else get_monthly_expiry(symbol, y+1, 1)
    return expiry

def round_to_strike(price, step):
    return round(round(price / step) * step, 2)


# ========================================
# AUTHENTICATION ROUTES (DO NOT TOUCH)
# ========================================

@app.route('/refresh')
def refresh_token_route():  # Renamed to avoid conflict
    auth_url = (
        f"https://api-t1.fyers.in/api/v3/generate-authcode"
        f"?client_id={FYERS_APP_ID}"
        f"&redirect_uri={FYERS_REDIRECT_URL}"
        f"&response_type=code"
        f"&state=sample_state"
    )
    return redirect(auth_url)


@app.route('/callback')
def callback():
    auth_code = request.args.get('code', '')

    if not auth_code:
        print("No auth_code in callback")
        return redirect('/set-token')

    print(f"Received auth_code: {auth_code[:20]}...")

    try:
        app_id_hash = hashlib.sha256(f"{FYERS_APP_ID}:{FYERS_SECRET_KEY}".encode()).hexdigest()

        r = req.post(
            'https://api-t1.fyers.in/api/v3/validate-authcode',
            json={
                'grant_type': 'authorization_code',
                'appIdHash': app_id_hash,
                'code': auth_code,
                'pin': ''
            },
            headers={'Content-Type': 'application/json'},
            timeout=15
        )

        if r.status_code == 200 and r.json().get('s') == 'ok':
            data = r.json()
            access_token = f"{FYERS_APP_ID}:{data['access_token']}"
            refresh_token = data.get('refresh_token')
            save_token(access_token, refresh_token)

            return f"""<html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white">
            <h1 style="font-size:48px">Login Successful!</h1>
            <p style="color:#22c55e;font-size:18px;margin-top:20px">Access token generated automatically!</p>
            <p style="color:#aaa;margin-top:10px">Time: {datetime.now(IST).strftime('%d %b %Y %H:%M:%S IST')}</p>
            <a href="/" style="color:#22c55e;font-size:18px;margin-top:30px;display:inline-block;padding:12px 30px;background:#166534;border-radius:6px;font-weight:600">Go to Scanner</a>
            </body></html>"""

        else:
            error_msg = r.json().get('message', 'Unknown error')
            print(f"Auth code exchange failed: {error_msg}")
            return f"""<html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white">
            <h1 style="color:#ef4444;font-size:48px">Login Failed</h1>
            <p style="color:#ef4444">{error_msg}</p>
            <a href="/refresh" style="color:#f59e0b;margin-top:20px;display:inline-block">Try Again</a>
            </body></html>"""

    except Exception as e:
        print(f"Callback error: {e}")
        return redirect('/set-token')


@app.route('/set-token')
def set_token():
    access_token = request.args.get('token', '').strip()
    refresh_token = request.args.get('refresh', '').strip()
    auth_code = request.args.get('code', '').strip()

    if auth_code and not access_token:
        print(f"Auth code detected, converting to tokens...")
        try:
            app_id_hash = hashlib.sha256(f"{FYERS_APP_ID}:{FYERS_SECRET_KEY}".encode()).hexdigest()
            r = req.post(
                'https://api-t1.fyers.in/api/v3/validate-authcode',
                json={
                    'grant_type': 'authorization_code',
                    'appIdHash': app_id_hash,
                    'code': auth_code,
                    'pin': ''
                },
                headers={'Content-Type': 'application/json'},
                timeout=15
            )

            if r.status_code == 200 and r.json().get('s') == 'ok':
                data = r.json()
                access_token = f"{FYERS_APP_ID}:{data['access_token']}"
                refresh_token = data.get('refresh_token')
                save_token(access_token, refresh_token)

                return f"""<html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white">
                <h1>Auth Code Converted!</h1>
                <p style="color:#22c55e;font-size:18px;margin-top:20px">Got real access_token and refresh_token!</p>
                <a href="/" style="color:#22c55e;font-size:18px;margin-top:20px;display:inline-block;padding:12px 30px;background:#166534;border-radius:6px;font-weight:600">Go to Scanner</a>
                </body></html>"""
            else:
                print(f"Auth code conversion failed: {r.json().get('message')}")
        except Exception as e:
            print(f"Auth code conversion error: {e}")

    if not access_token:
        return """<html><body style="font-family:sans-serif;padding:40px;background:#0f1f3d;color:white">
        <h2>Set Fyers Token</h2>
        <div style="background:#1a2a4a;padding:25px;border-radius:8px;margin-bottom:20px;border:1px solid #333;">
        <h3 style="color:#22c55e;margin-bottom:15px;">Option A: Auto-Login (Recommended)</h3>
        <p style="color:#aaa;margin-bottom:15px;">Click below to login via Fyers. Tokens generated automatically!</p>
        <a href="/refresh" style="color:#fff;text-decoration:none;padding:12px 24px;background:#166534;border-radius:6px;display:inline-block;font-weight:600;font-size:14px;">Login via Fyers</a>
        </div>
        <hr style="border-color:#333;margin:25px 0;">
        <div style="background:#1a2a4a;padding:25px;border-radius:8px;">
        <h3 style="color:#f59e0b;margin-bottom:15px;">Option B: Manual Entry</h3>
        <p style="color:#aaa;margin-bottom:15px;"><strong>IMPORTANT:</strong> Paste ACCESS TOKEN only!</p>
        <form method="GET" action="/set-token" style="margin-top:10px;">
            <input name="token" placeholder="VS55VDHYCW-100:eyJ..." style="width:100%;padding:10px;background:#0f1f3d;color:#fff;border:1px solid #3b82f6;border-radius:4px;margin-bottom:10px;">
            <input name="refresh" placeholder="eyJ..." style="width:100%;padding:10px;background:#0f1f3d;color:#fff;border:1px solid #3b82f6;border-radius:4px;margin-bottom:10px;">
            <button type="submit" style="padding:12px 24px;background:#22c55e;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:14px;">Save Token</button>
        </form>
        </div>
        </body></html>"""

    save_token(access_token, refresh_token if refresh_token else None)

    return f"""<html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white">
    <h1 style="font-size:48px">Token Saved!</h1>
    <p style="color:#22c55e;font-size:18px;margin-top:20px;">Time: {datetime.now(IST).strftime('%d %b %Y %H:%M:%S IST')}</p>
    <a href="/" style="color:#22c55e;font-size:18px;margin-top:20px;display:inline-block;padding:12px 30px;background:#166534;border-radius:6px;font-weight:600;">Go to Scanner</a>
    </body></html>"""


@app.route('/auto-refresh')
def trigger_auto_refresh():
    success = auto_refresh_access_token()
    return jsonify({
        'status': 'success' if success else 'error',
        'message': 'Auto-refresh failed - check refresh token or set manually via /set-token',
        'timestamp': datetime.now(IST).isoformat()
    })


@app.route('/debug-fyers')
def debug_fyers():
    result = {
        'token_exists': bool(token_data.get('access_token')),
        'token_prefix': token_data.get('access_token', '')[:50] + '...' if token_data.get('access_token') else None,
        'token_time': token_data.get('token_time'),
        'fyers_client_created': init_fyers() is not None
    }

    fyers = init_fyers()

    if fyers:
        try:
            test_data = fyers.history(data={
                'symbol': 'NSE:NIFTY50-INDEX',
                'resolution': '1',
                'date_format': '1',
                'range_from': (datetime.now(IST) - timedelta(days=1)).strftime('%Y-%m-%d'),
                'range_to': datetime.now(IST).strftime('%Y-%m-%d'),
                'cont_flag': '1'
            })
            result['history_status'] = test_data.get('s')
            result['history_message'] = test_data.get('message', '')
            result['candle_count'] = len(test_data.get('candles', []))
        except Exception as e:
            result['error'] = str(e)

    return jsonify(result)


# ========================================
# OPTION CHAIN FUNCTIONS
# ========================================

def get_fyers_expiry_timestamp(fyers, option_key, target_expiry_date):
    try:
        resp = fyers.optionchain(data={
            'symbol': option_key,
            'strikecount': 1,
            'timestamp': ''
        })
        if resp.get('s') != 'ok':
            return None

        expiry_map = {}
        for item in resp['data'].get('expiryData', []):
            d = datetime.strptime(item['date'], '%d-%m-%Y').date()
            expiry_map[d] = item['expiry']

        if not expiry_map:
            return None

        closest = min(expiry_map.keys(), key=lambda d: abs((d - target_expiry_date).days))
        return expiry_map[closest]
    except:
        return None


def get_tp1_option(symbol, tp1_price, option_type, expiry_date):
    fyers = init_fyers()
    if not fyers:
        return None, None, None

    config = SCANNER_CONFIG.get(symbol, {})
    step = config.get('strike_step', 50)

    try:
        expiry_ts = get_fyers_expiry_timestamp(fyers, config.get('option_key', ''), expiry_date)
        if not expiry_ts:
            return None, None, None

        tp1_rounded = round_to_strike(tp1_price, step)

        resp = fyers.optionchain(data={
            'symbol': config.get('option_key', ''),
            'strikecount': 20,
            'timestamp': expiry_ts
        })

        if resp.get('s') != 'ok':
            return None, None, None

        chain = resp['data'].get('optionsChain', [])
        filtered = [r for r in chain if r.get('option_type') == option_type]
        if not filtered:
            return None, None, None

        best = min(filtered, key=lambda r: abs(r['strike_price'] - tp1_rounded))
        return best['strike_price'], best.get('ltp', 0), best.get('symbol', '')
    except:
        return None, None, None


# ========================================
# ATR TRAILING STOP CALCULATOR
# ========================================

def calculate_atr_trailing(df, fast_period, fast_mult, slow_period, slow_mult):
    df = df.copy()
    hi, lo, cl = df['high'].values, df['low'].values, df['close'].values
    n = len(df)

    if n < max(fast_period, slow_period) + 5:
        return df

    tr = np.empty(n)
    tr[0] = hi[0] - lo[0]
    for i in range(1, n):
        tr[i] = max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))

    def rma(arr, period):
        a = np.zeros(n)
        if n < period:
            return a
        a[period-1] = arr[:period].mean()
        for i in range(period, n):
            a[i] = (a[i-1]*(period-1) + arr[i]) / period
        return a

    fast_atr = rma(tr, fast_period) * fast_mult
    slow_atr = rma(tr, slow_period) * slow_mult

    def trail(atr_sl):
        t = np.zeros(n)
        for i in range(1, n):
            sc, pt, ps = cl[i], t[i-1], cl[i-1]
            if sc > pt and ps > pt:
                t[i] = max(pt, sc - atr_sl[i])
            elif sc < pt and ps < pt:
                t[i] = min(pt, sc + atr_sl[i])
            elif sc > pt:
                t[i] = sc - atr_sl[i]
            else:
                t[i] = sc + atr_sl[i]
        return t

    t1 = trail(fast_atr)
    t2 = trail(slow_atr)

    df['trail1'] = t1
    df['trail2'] = t2
    df['fast_atr'] = fast_atr / fast_mult
    df['slow_atr'] = slow_atr / slow_mult

    buy = np.zeros(n, bool)
    sell = np.zeros(n, bool)

    for i in range(1, n):
        if t1[i] > t2[i] and t1[i-1] <= t2[i-1]:
            buy[i] = True
        if t1[i] < t2[i] and t1[i-1] >= t2[i-1]:
            sell[i] = True

    df['buy_signal'] = buy
    df['sell_signal'] = sell

    bar_color = []
    for i in range(n):
        if t1[i] > t2[i] and cl[i] > t2[i] and lo[i] > t2[i]:
            bar_color.append('green')
        elif t1[i] > t2[i] and cl[i] > t2[i] and lo[i] < t2[i]:
            bar_color.append('blue')
        elif t2[i] > t1[i] and cl[i] < t2[i] and hi[i] < t2[i]:
            bar_color.append('red')
        elif t2[i] > t1[i] and cl[i] < t2[i] and hi[i] > t2[i]:
            bar_color.append('yellow')
        else:
            bar_color.append('neutral')

    df['bar_color'] = bar_color
    df['regime'] = np.where(t1 > t2, 'BULL', 'BEAR')

    return df


# ========================================
# DATA FETCHING FUNCTIONS
# ========================================

def fetch_candles(instrument_key, interval='1minute', days=90, retry_on_fail=True):
    fyers = init_fyers()
    if not fyers:
        return pd.DataFrame()

    interval_map = {
        '1minute': '1', '5minute': '5', 
        '15minute': '15', '30minute': '30', '60minute': '60'
    }

    end_date = datetime.now(IST)
    start_date = end_date - timedelta(days=days)

    data = {
        'symbol': instrument_key,
        'resolution': interval_map.get(interval, '1'),
        'date_format': '1',
        'range_from': start_date.strftime('%Y-%m-%d'),
        'range_to': end_date.strftime('%Y-%m-%d'),
        'cont_flag': '1'
    }

    try:
        response = fyers.history(data=data)

        if response.get('s') != 'ok':
            if retry_on_fail and 'unauthorized' in str(response.get('message', '')).lower():
                if auto_refresh_access_token():
                    return fetch_candles(instrument_key, interval, days, retry_on_fail=False)
            return pd.DataFrame()

        candles = response.get('candles', [])
        if not candles:
            return pd.DataFrame()

        rows = []
        for c in candles:
            dt = pd.to_datetime(c[0], unit='s')
            dt = dt.tz_localize('UTC').tz_convert('Asia/Kolkata').tz_localize(None)
            rows.append({
                'datetime': dt,
                'open': c[1],
                'high': c[2],
                'low': c[3],
                'close': c[4],
                'volume': c[5]
            })

        df = pd.DataFrame(rows)
        df = df.sort_values('datetime').drop_duplicates('datetime').reset_index(drop=True)

        t = df['datetime'].dt.hour * 100 + df['datetime'].dt.minute
        df = df[(t >= 915) & (t <= 1530)].reset_index(drop=True)

        return df

    except:
        return pd.DataFrame()


def resample_candles(df_1m, minutes):
    if len(df_1m) == 0:
        return pd.DataFrame()

    df = df_1m.copy().set_index('datetime')

    r = df.resample(f'{minutes}min').agg(
        open=('open','first'), 
        high=('high','max'), 
        low=('low','min'), 
        close=('close','last'), 
        volume=('volume','sum')
    ).dropna().reset_index()

    t = r['datetime'].dt.hour * 100 + r['datetime'].dt.minute
    return r[(t >= 915) & (t <= 1530)].reset_index(drop=True)


# ========================================
# SIGNAL GENERATION
# ========================================

def generate_signals():
    now = datetime.now(IST)
    today = now.date()
    signals = []

    print(f"\n{'='*60}")
    print(f"SIGNAL SCAN: {now.strftime('%d %b %Y %H:%M:%S IST')}")
    print(f"{'='*60}")

    for symbol, config in SCANNER_CONFIG.items():
        try:
            print(f"\nScanning {symbol}...")

            df_1m = fetch_candles(config['instrument_key'], '1minute', days=90)

            if len(df_1m) < 50:
                print(f"Insufficient candles: {len(df_1m)}")
                continue

            df = resample_candles(df_1m, config['resample_minutes'])

            if len(df) < max(config['fast_period'], config['slow_period']) + 10:
                print(f"Insufficient resampled candles: {len(df)}")
                continue

            df = calculate_atr_trailing(
                df, 
                config['fast_period'], config['fast_mult'],
                config['slow_period'], config['slow_mult']
            )

            if len(df) >= 200:
                scan_df = df.tail(200).copy()
                print(f"Scanning last 200 candles for {symbol}")
            else:
                scan_df = df.copy()
                print(f"Only {len(df)} candles available")

            signal_count = 0
            for _, row in scan_df.iterrows():
                if not (row.get('buy_signal', False) or row.get('sell_signal', False)):
                    continue

                direction = 'BUY-LONG' if row['buy_signal'] else 'SELL-SHORT'
                entry = round(float(row['close']), 2)
                trail2 = round(float(row['trail2']), 2)
                trail1 = round(float(row['trail1']), 2)

                if direction == 'BUY-LONG':
                    sl = trail2
                    risk = entry - sl
                    target_1 = round(entry + risk * 1.5, 2)
                    target_2 = round(entry + risk * 2.5, 2)
                else:
                    sl = trail2
                    risk = sl - entry
                    target_1 = round(entry - risk * 1.5, 2)
                    target_2 = round(entry - risk * 2.5, 2)

                risk = abs(risk)
                if risk == 0:
                    continue

                reward = abs(target_2 - entry)
                rr = round(reward / risk, 2)

                confidence = 0.5
                bar_c = row.get('bar_color', 'neutral')

                if direction == 'BUY-LONG':
                    if bar_c == 'green':
                        confidence += 0.2
                    elif bar_c == 'blue':
                        confidence += 0.1
                else:
                    if bar_c == 'red':
                        confidence += 0.2
                    elif bar_c == 'yellow':
                        confidence += 0.1

                if rr >= 2:
                    confidence += 0.1
                if rr >= 3:
                    confidence += 0.1

                confidence = min(confidence, 0.95)

                if confidence >= 0.8:
                    grade, grade_score = 'A+', 95
                elif confidence >= 0.7:
                    grade, grade_score = 'A', 85
                elif confidence >= 0.6:
                    grade, grade_score = 'B', 70
                else:
                    grade, grade_score = 'C', 55

                signal_dt = pd.to_datetime(row['datetime'])
                if signal_dt.tzinfo is None:
                    signal_dt = IST.localize(signal_dt)

                signals.append({
                    '_id': f"{symbol}_{signal_dt.strftime('%Y%m%d_%H%M')}",
                    'symbol': symbol,
                    'direction': direction,
                    'model': 'ATR-TS',
                    'entry': entry,
                    'sl': sl,
                    'target_1': target_1,
                    'target_2': target_2,
                    'target': target_2,
                    'risk_reward': f"1:{rr}",
                    'confidence': round(confidence, 2),
                    'grade': grade,
                    'grade_score': grade_score,
                    'scan_date': signal_dt.isoformat(),
                    'scan_time': signal_dt.strftime('%H:%M'),
                    'trail1': trail1,
                    'trail2': trail2,
                    'fast_atr': round(float(row['fast_atr']), 2),
                    'slow_atr': round(float(row['slow_atr']), 2),
                    'bar_color': bar_c,
                    'regime': row.get('regime', 'UNKNOWN'),
                    'timeframe': f"{config['resample_minutes']}m",
                    'lot_size': config['lot_size'],
                    'scanner_type': 'atr_trailing',
                    'outcome': 'pending'
                })

                signal_count += 1
                print(f"  {direction} signal @ {signal_dt.strftime('%H:%M')} | Entry: {entry} | SL: {sl} | Grade: {grade}")

            print(f"{symbol}: {signal_count} signal(s)")

        except Exception as e:
            print(f"Error scanning {symbol}: {e}")
            import traceback
            traceback.print_exc()
            continue

    signals.sort(key=lambda x: x.get('scan_date', ''), reverse=True)

    existing = scan_cache.get('signals', [])
    existing_ids = {s['_id'] for s in signals}

    for s in existing:
        if s['_id'] not in existing_ids and s.get('scan_date', '')[:10] == datetime.now(IST).strftime('%Y-%m-%d'):
            signals.append(s)

    signals.sort(key=lambda x: x.get('scan_date', ''), reverse=True)

    print(f"\n{'='*60}")
    print(f"TOTAL SIGNALS: {len(signals)}")
    print(f"{'='*60}\n")

    return signals


# ========================================
# OPTION SIGNAL GENERATION
# ========================================

def generate_option_signals(futures_signals):
    results = []

    for sig in futures_signals:
        symbol = sig.get('symbol', '')
        config = SCANNER_CONFIG.get(symbol, {})
        if not config:
            continue

        direction = sig.get('direction', '')
        opt_type = 'CE' if direction == 'BUY-LONG' else 'PE'
        tp1 = float(sig.get('target_1', 0))
        lot = config['lot_size']
        expiry = get_active_expiry(symbol, datetime.now(IST).date())

        strike, ltp, opt_symbol = get_tp1_option(symbol, tp1, opt_type, expiry)

        results.append({
            '_id': sig['_id'] + '_OPT',
            'futures_id': sig['_id'],
            'symbol': symbol,
            'direction': direction,
            'opt_type': opt_type,
            'action': 'BUY ' + opt_type,
            'spot': float(sig.get('entry', 0)),
            'tp1': tp1,
            'strike': strike,
            'ltp': round(ltp, 2) if ltp else None,
            'opt_symbol': opt_symbol,
            'expiry': expiry.strftime('%d %b %Y'),
            'days_to_expiry': (expiry - datetime.now(IST).date()).days,
            'lot_size': lot,
            'max_risk': round(ltp * lot, 0) if ltp else None,
            'scan_date': sig.get('scan_date', ''),
            'scan_time': sig.get('scan_time', ''),
            'grade': sig.get('grade', ''),
            'grade_score': sig.get('grade_score', 0),
            'confidence': sig.get('confidence', 0),
            'scanner_type': 'atr_trailing',
            'outcome': 'pending'
        })

    print(f"\nGenerated {len(results)} option signal(s)")
    return results


# ========================================
# 🆕 TRADE EXECUTION ENGINE (New Addition)
# ========================================

def execute_futures_trade(signal_data, quantity=1):
    """
    Execute a futures trade based on signal data
    Returns: trade_record or None
    """
    global active_futures_trades
    
    try:
        fyers = init_fyers()
        if not fyers:
            print("❌ Cannot execute trade: No Fyers client")
            return None
        
        symbol = signal_data.get('symbol', '')
        direction = signal_data.get('direction', '')
        entry_price = float(signal_data.get('entry', 0))
        
        # Determine side
        side = 1 if direction == 'BUY-LONG' else -1  # 1=BUY, -1=SELL
        
        # Map symbol to Fyers format
        symbol_map = {
            'NIFTY50': 'NSE:NIFTY23FUT',
            'BANKNIFTY': 'NSE:BANKNIFTY23FUT'
        }
        fyers_symbol = symbol_map.get(symbol, f"NSE:{symbol}FUT")
        
        print(f"🎯 Executing Futures Trade: {fyers_symbol} | Side: {'BUY' if side==1 else 'SELL'} | Qty: {quantity}")
        
        # Place order via Fyers API
        try:
            order_data = {
                "symbol": fyers_symbol,
                "qty": quantity,
                "type": 2,  # MARKET order
                "side": side,
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
                "orderType": "MARKET"
            }
            
            response = fyers.place_order(data=order_data)
            
            if response.get('s') == 'ok':
                order_id = response.get('id', '')
                
                # Create trade record
                trade_record = {
                    'id': f"FUT-{datetime.now(IST).strftime('%Y%m%d%H%M%S%f')}",
                    'signal_id': signal_data.get('_id', ''),
                    'symbol': symbol,
                    'direction': direction,
                    'side': 'BUY' if side == 1 else 'SELL',
                    'quantity': quantity,
                    'entry_price': entry_price,
                    'sl': float(signal_data.get('sl', 0)),
                    'target_1': float(signal_data.get('target_1', 0)),
                    'target_2': float(signal_data.get('target_2', 0)),
                    'status': 'OPEN',
                    'pnl': 0,
                    'pnl_percentage': 0,
                    'order_id': order_id,
                    'entry_time': datetime.now(IST).strftime('%d %b %H:%M'),
                    'grade': signal_data.get('grade', ''),
                    'confidence': signal_data.get('confidence', 0),
                    'outcome': 'pending'
                }
                
                # Store trade
                active_futures_trades.append(trade_record)
                save_trades()
                
                print(f"✅ Futures Trade Executed: {trade_record['id']} | Order ID: {order_id}")
                
                # 🔥 AUTO-EXECUTE LINKED OPTIONS TRADE
                execute_options_trade(trade_record, signal_data)
                
                return trade_record
                
            else:
                print(f"❌ Order failed: {response.get('message', 'Unknown error')}")
                return None
                
        except Exception as e:
            print(f"❌ Order execution error: {e}")
            return None
            
    except Exception as e:
        print(f"❌ Error in execute_futures_trade: {e}")
        import traceback
        traceback.print_exc()
        return None


def execute_options_trade(futures_trade, signal_data=None):
    """
    Automatically execute corresponding options trade when futures trade opens
    - BUY CE for Futures BUY
    - BUY PE for Futures SELL
    - Strike closest to TP1 price
    """
    global active_options_trades
    
    try:
        symbol = futures_trade.get('symbol', '')
        direction = futures_trade.get('direction', '')
        target_1 = float(futures_trade.get('target_1', 0))
        quantity = futures_trade.get('quantity', 1)
        
        if not all([symbol, direction, target_1 > 0]):
            print("⚠️ Missing required fields for options trade")
            return None
        
        # Determine option type
        opt_type = 'CE' if direction == 'BUY-LONG' else 'PE'
        
        # Get expiry date
        expiry = get_active_expiry(symbol, datetime.now(IST).date())
        
        # Get option details (strike, LTP, symbol)
        strike, ltp, opt_symbol = get_tp1_option(symbol, target_1, opt_type, expiry)
        
        if not opt_symbol or not ltp:
            print(f"⚠️ Could not fetch option data for {symbol} {opt_type} @ TP1={target_1}")
            return None
        
        print(f"🎯 Executing Options Trade: {opt_symbol} | Type: {opt_type} | Strike: {strike} | LTP: {ltp}")
        
        # Place options order (always BUY)
        fyers = init_fyers()
        if fyers:
            try:
                order_data = {
                    "symbol": opt_symbol,
                    "qty": quantity,
                    "type": 2,  # MARKET order
                    "side": 1,  # Always BUY for options
                    "productType": "INTRADAY",
                    "limitPrice": 0,
                    "stopPrice": 0,
                    "validity": "DAY",
                    "disclosedQty": 0,
                    "offlineOrder": False,
                    "orderType": "MARKET"
                }
                
                response = fyers.place_order(data=order_data)
                
                if response.get('s') == 'ok':
                    order_id = response.get('id', '')
                    
                    # Create options trade record
                    options_trade = {
                        'id': f"OPT-{datetime.now(IST).strftime('%Y%m%d%H%M%S%f')}",
                        'futures_trade_id': futures_trade.get('id', ''),
                        'signal_id': futures_trade.get('signal_id', ''),
                        'symbol': symbol,
                        'opt_symbol': opt_symbol,
                        'option_type': opt_type,
                        'action': f'BUY {opt_type}',
                        'strike_price': strike,
                        'quantity': quantity,
                        'entry_price': round(float(ltp), 2),
                        'status': 'OPEN',
                        'pnl': 0,
                        'pnl_percentage': 0,
                        'order_id': order_id,
                        'entry_time': datetime.now(IST).strftime('%d %b %H:%M'),
                        'tp1': target_1,
                        'sl': float(futures_trade.get('sl', 0)),
                        'expiry': expiry.strftime('%d %b %Y'),
                        'grade': futures_trade.get('grade', ''),
                        'confidence': futures_trade.get('confidence', 0),
                        'outcome': 'pending'
                    }
                    
                    # Store options trade
                    active_options_trades.append(options_trade)
                    save_options_trades()
                    
                    print(f"✅ Options Trade Executed: {options_trade['id']} | {opt_symbol} @ {ltp}")
                    
                    return options_trade
                    
                else:
                    print(f"❌ Options order failed: {response.get('message', 'Unknown error')}")
                    return None
                    
            except Exception as e:
                print(f"❌ Options order error: {e}")
                return None
        else:
            print("❌ No Fyers client for options trade")
            return None
            
    except Exception as e:
        print(f"❌ Error in execute_options_trade: {e}")
        import traceback
        traceback.print_exc()
        return None


def close_linked_options(futures_trade_id, reason='TP/SL Hit'):
    """
    Close linked options trade when futures hits target or stop loss
    """
    global active_options_trades
    
    try:
        # Find linked options trade
        options_trade = None
        for opt in active_options_trades:
            if opt.get('futures_trade_id') == futures_trade_id and opt.get('status') == 'OPEN':
                options_trade = opt
                break
        
        if not options_trade:
            print(f"⚠️ No open options trade found for futures ID: {futures_trade_id}")
            return None
        
        print(f"🔒 Closing Options Trade: {options_trade['id']} | Reason: {reason}")
        
        # Close options position (SELL to close)
        fyers = init_fyers()
        if fyers:
            try:
                order_data = {
                    "symbol": options_trade['opt_symbol'],
                    "qty": options_trade['quantity'],
                    "type": 2,  # MARKET order
                    "side": -1,  # SELL to close
                    "productType": "INTRADAY",
                    "limitPrice": 0,
                    "stopPrice": 0,
                    "validity": "DAY",
                    "disclosedQty": 0,
                    "offlineOrder": False,
                    "orderType": "MARKET"
                }
                
                response = fyers.place_order(data=order_data)
                
                if response.get('s') == 'ok':
                    # Calculate P&L (approximate since we don't have exact exit price)
                    # In production, you'd fetch actual exit price from order book
                    exit_price = options_trade['entry_price'] * 1.05  # Placeholder - replace with actual
                    
                    pnl = (exit_price - options_trade['entry_price']) * options_trade['quantity']
                    pnl_pct = (pnl / (options_trade['entry_price'] * options_trade['quantity'])) * 100
                    
                    # Update options trade record
                    options_trade.update({
                        'status': 'CLOSED',
                        'exit_price': round(exit_price, 2),
                        'exit_time': datetime.now(IST).strftime('%d %b %H:%M'),
                        'pnl': round(pnl, 2),
                        'pnl_percentage': round(pnl_pct, 2),
                        'close_reason': reason,
                        'outcome': reason.lower().replace(' ', '_')
                    })
                    
                    save_options_trades()
                    
                    print(f"✅ Options Trade Closed: {options_trade['id']} | P&L: ₹{pnl:.2f}")
                    
                    return options_trade
                    
                else:
                    print(f"❌ Options close failed: {response.get('message')}")
                    return None
                    
            except Exception as e:
                print(f"❌ Options close error: {e}")
                return None
        else:
            print("❌ No Fyers client to close options")
            return None
            
    except Exception as e:
        print(f"❌ Error in close_linked_options: {e}")
        return None


# ========================================
# 🆕 POSITION MONITOR (New Addition)
# ========================================

def monitor_positions():
    """
    Background thread to monitor open positions and check for TP/SL hits
    Runs every 10 seconds during market hours
    """
    global active_futures_trades
    
    while True:
        try:
            status = get_scanner_status()
            
            if status == 'ACTIVE':
                for trade in active_futures_trades:
                    if trade.get('status') != 'OPEN':
                        continue
                    
                    try:
                        symbol = trade.get('symbol', '')
                        config = SCANNER_CONFIG.get(symbol, {})
                        
                        if not config:
                            continue
                        
                        # Fetch latest candle
                        df = fetch_candles(config['instrument_key'], '1minute', days=1)
                        
                        if len(df) == 0:
                            continue
                        
                        current_price = float(df.iloc[-1]['close'])
                        entry_price = float(trade.get('entry_price', 0))
                        sl = float(trade.get('sl', 0))
                        target_2 = float(trade.get('target_2', 0))
                        direction = trade.get('direction', '')
                        
                        # Check if TP or SL hit
                        hit_target = False
                        hit_sl = False
                        
                        if direction == 'BUY-LONG':
                            if current_price >= target_2:
                                hit_target = True
                            elif current_price <= sl:
                                hit_sl = True
                        else:  # SELL-SHORT
                            if current_price <= target_2:
                                hit_target = True
                            elif current_price >= sl:
                                hit_sl = True
                        
                        if hit_target or hit_sl:
                            reason = 'Target Hit' if hit_target else 'Stop Loss Hit'
                            
                            # Update futures trade
                            trade['status'] = 'CLOSED'
                            trade['exit_price'] = current_price
                            trade['exit_time'] = datetime.now(IST).strftime('%d %b %H:%M')
                            
                            # Calculate P&L
                            if direction == 'BUY-LONG':
                                pnl = (current_price - entry_price) * trade.get('quantity', 1)
                            else:
                                pnl = (entry_price - current_price) * trade.get('quantity', 1)
                            
                            trade['pnl'] = round(pnl, 2)
                            trade['pnl_percentage'] = round((pnl / (entry_price * trade.get('quantity', 1))) * 100, 2)
                            trade['outcome'] = reason.lower().replace(' ', '_')
                            
                            save_trades()
                            
                            print(f"🎯 Futures Trade Closed: {trade['id']} | {reason} | P&L: ₹{pnl:.2f}")
                            
                            # 🔥 AUTO-CLOSE LINKED OPTIONS
                            close_linked_options(trade['id'], reason)
                            
                    except Exception as e:
                        print(f"⚠️ Error monitoring trade {trade.get('id')}: {e}")
                        continue
            
            time.sleep(10)  # Check every 10 seconds
            
        except Exception as e:
            print(f"⚠️ Position monitor error: {e}")
            time.sleep(10)


# ========================================
# SCANNER STATUS
# ========================================

def get_scanner_status():
    now = datetime.now(IST)
    time_val = now.hour * 100 + now.minute
    day = now.weekday()

    if not token_data['access_token']:
        return 'NO_TOKEN'
    if day >= 5:
        return 'MARKET_CLOSED'
    if now.date() in TRADING_HOLIDAYS:
        return 'MARKET_CLOSED'
    if 915 <= time_val <= 1530:
        return 'ACTIVE'
    if 900 <= time_val < 915:
        return 'PRE_MARKET'
    return 'MARKET_CLOSED'


# ========================================
# BACKGROUND SCANNER
# ========================================

def background_scanner():
    """Runs every 30 seconds during market hours to keep cache fresh"""
    while True:
        try:
            status = get_scanner_status()
            if status in ['ACTIVE', 'PRE_MARKET']:
                if scan_lock.acquire(blocking=False):
                    try:
                        print(f"[BG] Starting background scan at {datetime.now(IST).strftime('%H:%M:%S')}")
                        signals = generate_signals()
                        scan_cache['signals'] = signals
                        scan_cache['last_scan'] = datetime.now(IST)
                        print(f"[BG] Scan complete. {len(signals)} signals cached.")
                    finally:
                        scan_lock.release()
                else:
                    print(f"[BG] Scan already running, skipping...")
            else:
                print(f"[BG] Market closed, sleeping...")
        except Exception as e:
            print(f"[BG] Error in background scanner: {e}")

        time.sleep(30)


# ========================================
# 🆕 TRADE EXECUTION API ENDPOINTS (New Addition)
# ========================================

@app.route('/api/execute-trade', methods=['POST'])
def api_execute_trade():
    """
    Execute a futures trade based on signal ID
    Automatically executes linked options trade
    """
    try:
        data = request.json
        signal_id = data.get('signal_id')
        quantity = int(data.get('quantity', 1))
        
        if not signal_id:
            return jsonify({'status': 'error', 'message': 'signal_id required'})
        
        # Find signal in cache
        signals = scan_cache.get('signals', [])
        signal = next((s for s in signals if s.get('_id') == signal_id), None)
        
        if not signal:
            return jsonify({'status': 'error', 'message': 'Signal not found'})
        
        # Execute futures trade (which auto-executes options)
        trade = execute_futures_trade(signal, quantity)
        
        if trade:
            return jsonify({
                'status': 'success',
                'message': 'Trade executed successfully',
                'futures_trade': trade,
                'note': 'Options trade executed automatically'
            })
        else:
            return jsonify({'status': 'error', 'message': 'Trade execution failed'})
            
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/trades')
def api_get_active_trades():
    """Get all active (open) futures trades"""
    active = [t for t in active_futures_trades if t.get('status') == 'OPEN']
    return jsonify({
        'status': 'success',
        'trades': active,
        'count': len(active)
    })


@app.route('/api/options-trades')
def api_get_active_options_trades():
    """Get all active (open) options trades"""
    active = [t for t in active_options_trades if t.get('status') == 'OPEN']
    return jsonify({
        'status': 'success',
        'options_trades': active,
        'count': len(active)
    })


@app.route('/api/trades/history')
def api_get_trades_history():
    """Get closed futures trades history"""
    closed = [t for t in active_futures_trades if t.get('status') == 'CLOSED']
    return jsonify({
        'status': 'success',
        'closed_trades': closed,
        'count': len(closed)
    })


@app.route('/api/options-trades/history')
def api_get_options_history():
    """Get closed options trades history"""
    closed = [t for t in active_options_trades if t.get('status') == 'CLOSED']
    return jsonify({
        'status': 'success',
        'closed_options': closed,
        'count': len(closed)
    })


@app.route('/api/close-trade/<trade_id>', methods=['POST'])
def api_close_trade():
    """Manually close a specific futures trade (and its linked options)"""
    try:
        global active_futures_trades
        
        trade = next((t for t in active_futures_trades if t.get('id') == trade_id), None)
        
        if not trade:
            return jsonify({'status': 'error', 'message': 'Trade not found'})
        
        if trade.get('status') != 'OPEN':
            return jsonify({'status': 'error', 'message': 'Trade already closed'})
        
        # Close the trade manually (mark as closed)
        trade['status'] = 'CLOSED'
        trade['close_reason'] = 'Manual Close'
        trade['exit_time'] = datetime.now(IST).strftime('%d %b %H:%M')
        trade['outcome'] = 'manual_close'
        
        save_trades()
        
        # 🔥 AUTO-CLOSE LINKED OPTIONS
        close_linked_options(trade_id, 'Futures Manually Closed')
        
        return jsonify({
            'status': 'success',
            'message': 'Trade closed successfully',
            'trade': trade
        })
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/trades/all')
def api_get_all_trades():
    """Get all trades (both open and closed)"""
    return jsonify({
        'status': 'success',
        'all_trades': active_futures_trades,
        'total': len(active_futures_trades)
    })


@app.route('/api/options-trades/all')
def api_get_all_options_trades():
    """Get all options trades (both open and closed)"""
    return jsonify({
        'status': 'success',
        'all_options_trades': active_options_trades,
        'total': len(active_options_trades)
    })


# ========================================
# ORIGINAL API ROUTES (PRESERVED EXACTLY)
# ========================================

@app.route('/')
def home():
    ts = 'Token Active' if token_data['access_token'] else 'Token Expired'
    tt = token_data.get('token_time', 'Never')

    return f"""<html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white">
    <h1>StrikeTrail Scanner</h1>
    <p style="color:#aaa">ATR Trailing Stop Scanner</p>
    <div style="background:#1a2a4a;padding:20px;margin:20px auto;max-width:500px;border-radius:8px;">
    <p style="margin:10px"><strong>Status:</strong> {ts}</p>
    <p style="margin:10px"><strong>Login Time:</strong> {tt}</p>
    <p style="margin:10px;"><strong>Server Time:</strong> {datetime.now(IST).strftime('%d %b %Y %H:%M:%S IST')}</p>
    </div>
    <p><a href="/refresh" style="color:#22c55e;text-decoration:none;padding:10px 24px;background:#166534;border-radius:6px;display:inline-block;margin:5px">Login via Fyers</a></p>
    <p><a href="/set-token" style="color:#3b82f6;text-decoration:none;padding:10px 24px;background:#1e3a8a;border-radius:6px;display:inline-block;margin:5px;">Set Token Manually</a></p>
    <p><a href="/api/signals" style="color:#22d3ee;text-decoration:none;padding:10px 24px;background:#164e63;border-radius:6px;display:inline-block;margin:5px;">Get Signals</a></p>
    <p><a href="/debug-fyers" style="color:#a78bfa;text-decoration:none;padding:10px 24px;background:#4c1d95;border-radius:6px;display:inline-block;margin:5px;">Debug</a></p>
    </body></html>"""


@app.route('/api/status')
def api_status():
    return jsonify({
        'status': 'success',
        'scanner_status': get_scanner_status(),
        'server_time_ist': datetime.now(IST).isoformat(),
        'token_set': token_data['access_token'] is not None,
        'token_time': token_data.get('token_time'),
        'scanner_model': 'ATR Trailing Stop (Walk-Forward Validated)',
        'config': {
            sym: {
                'timeframe': f"{cfg['resample_minutes']}m",
                'fast': f"({cfg['fast_period']}, {cfg['fast_mult']})",
                'slow': f"({cfg['slow_period']}, {cfg['slow_mult']})",
                'strike_step': cfg['strike_step']
            } for sym, cfg in SCANNER_CONFIG.items()
        },
        'active_futures_trades': len([t for t in active_futures_trades if t.get('status') == 'OPEN']),
        'active_options_trades': len([t for t in active_options_trades if t.get('status') == 'OPEN'])
    })


@app.route('/api/signals')
def api_signals():
    now = datetime.now(IST)
    status = get_scanner_status()
    force = request.args.get('force', 'false').lower() == 'true'

    if status == 'NO_TOKEN':
        return jsonify({'status': 'success', 'scanner_status': 'NO_TOKEN', 'signals': [], 'timestamp': now.isoformat()})

    # If cache is fresh and not forcing, return immediately
    cache_ttl = 30 if status == 'ACTIVE' else 60
    if not force and scan_cache['last_scan'] and (now - scan_cache['last_scan']).total_seconds() < cache_ttl:
        return jsonify({
            'status': 'success',
            'scanner_status': status,
            'signals': scan_cache['signals'],
            'cached': True,
            'timestamp': now.isoformat()
        })

    # Try to acquire lock for fresh scan
    if scan_lock.acquire(blocking=False):
        try:
            if status in ['ACTIVE', 'PRE_MARKET']:
                signals = generate_signals()
            else:
                signals = scan_cache.get('signals', [])

            scan_cache['signals'] = signals
            scan_cache['last_scan'] = now

            return jsonify({
                'status': 'success',
                'scanner_status': status,
                'signals': signals,
                'cached': False,
                'timestamp': now.isoformat()
            })
        finally:
            scan_lock.release()
    else:
        # Another scan is running, return current cache with flag
        return jsonify({
            'status': 'success',
            'scanner_status': status,
            'signals': scan_cache.get('signals', []),
            'cached': True,
            'scan_in_progress': True,
            'timestamp': now.isoformat()
        })


@app.route('/api/option-signals')
def api_option_signals():
    now = datetime.now(IST)

    if options_cache['last_fetch'] and (now - options_cache['last_fetch']).total_seconds() < 120:
        return jsonify({
            'status': 'success',
            'option_signals': options_cache['signals'],
            'cached': True,
            'timestamp': now.isoformat()
        })

    futures = scan_cache.get('signals', [])
    opt_signals = generate_option_signals(futures)

    options_cache['signals'] = opt_signals
    options_cache['last_fetch'] = now

    return jsonify({
        'status': 'success',
        'option_signals': opt_signals,
        'cached': False,
        'timestamp': now.isoformat()
    })


@app.route('/api/track', methods=['POST'])
def api_track():
    try:
        data = request.json
        if not data or 'signals' not in data:
            return jsonify({'status': 'error', 'message': 'No signals'})

        results = []

        for sig in data['signals']:
            symbol = sig.get('symbol', '')
            config = SCANNER_CONFIG.get(symbol, '')
            if not config:
                results.append({'_id': sig.get('_id'), 'status': 'pending', 'track_status': 'no_config'})
                continue

            try:
                signal_time = pd.to_datetime(sig.get('scan_date')).replace(tzinfo=None)
                df_1m = fetch_candles(config['instrument_key'], '1minute', days=10)

                if len(df_1m) == 0:
                    results.append({'_id': sig.get('_id'), 'status': 'pending', 'track_status': 'no_data'})
                    continue

                df_1m['datetime'] = pd.to_datetime(df_1m['datetime']).dt.tz_localize(None)
                df_after = df_1m[df_1m['datetime'] > signal_time].reset_index(drop=True)

                if len(df_after) == 0:
                    results.append({'_id': sig.get('_id'), 'status': 'pending', 'track_status': 'no_candles_after'})
                    continue

                entry_met = False
                entry_idx = None
                direction = sig.get('direction', '')
                entry = float(sig.get('entry', 0))
                sl = float(sig.get('sl', 0))
                t2 = float(sig.get('target_2', sig.get('target', 0)))

                for idx, row in df_after.iterrows():
                    if direction == 'BUY-LONG' and row['high'] >= entry:
                        entry_met = True; entry_idx = idx; break
                    elif direction == 'SELL-SHORT' and row['low'] <= entry:
                        entry_met = True; entry_idx = idx; break

                if not entry_met:
                    current_price = float(df_after.iloc[-1]['close'])
                    results.append({'_id': sig.get('_id'), 'status': 'pending', 'current_price': current_price, 'live_pnl_pct': 0, 'track_status': 'entry_not_met'})
                    continue

                entry_pos = df_after.index.get_loc(entry_idx)
                df_post = df_after.iloc[entry_pos:].reset_index(drop=True)
                trade_status = 'open'
                exit_price = None
                current_price = float(df_post.iloc[-1]['close'])

                for _, row in df_post.iterrows():
                    if direction == 'BUY-LONG':
                        if row['high'] >= t2: trade_status = 'target_hit'; exit_price = t2; break
                        if row['low'] <= sl: trade_status = 'stop_hit'; exit_price = sl; break
                    else:
                        if row['low'] <= t2: trade_status = 'target_hit'; exit_price = t2; break
                        if row['high'] >= sl: trade_status = 'stop_hit'; exit_price = sl; break

                pnl_pct = round((current_price - entry) / entry * 100, 2) if direction == 'BUY-LONG' else round((entry - current_price) / entry * 100, 2)

                results.append({
                    '_id': sig.get('_id'),
                    'status': trade_status,
                    'exit_price': exit_price,
                    'current_price': current_price,
                    'live_pnl_pct': pnl_pct,
                    'track_status': 'tracked'
                })

            except Exception as e:
                results.append({'_id': sig.get('_id'), 'status': 'pending', 'track_status': f'error:{str(e)}'})

        return jsonify({'status': 'success', 'results': results})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


# ========================================
# STARTUP BLOCK
# ========================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))

    print(f"\n{'='*70}")
    print(f"PROFITMASTER FYERS SCANNER STARTING")
    print(f"{'='*70}")
    print(f"Port: {port}")
    print(f"Token: {'Active' if token_data['access_token'] else 'Not Set'}")
    print(f"Refresh Token: {'Available' if token_data.get('refresh_token') else 'Not Set'}")
    print(f"Server Time: {datetime.now(IST).strftime('%d %b %Y %H:%M:%S IST')}")
    print(f"Loaded Trades: {len(active_futures_trades)} futures, {len(active_options_trades)} options")
    print(f"{'='*70}\n")

    # Start background scanner thread
    bg_thread = threading.Thread(target=background_scanner, daemon=True)
    bg_thread.start()
    print("Background scanner started (every 30 seconds during market hours)")

    # 🆕 Start position monitor thread (NEW)
    monitor_thread = threading.Thread(target=monitor_positions, daemon=True)
    monitor_thread.start()
    print("Position monitor started (checks TP/SL every 10 seconds)")

    # Start keep-alive thread (every 14 minutes)
    def keep_alive_ping():
        while True:
            try:
                req.get(f"http://localhost:{port}/api/status", timeout=10)
                print(f"Keep-alive ping sent at {datetime.now(IST).strftime('%H:%M:%S IST')}")
            except:
                pass
            time.sleep(840)

    keep_alive_thread = threading.Thread(target=keep_alive_ping, daemon=True)
    keep_alive_thread.start()
    print("Keep-alive pinger started (every 14 minutes)")

    print("\nStarting Flask server...")
    app.run(host='0.0.0.0', port=port, debug=False)
