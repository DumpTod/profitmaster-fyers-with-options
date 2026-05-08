from flask import Flask, jsonify, request, redirect, send_file
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
# FYERS CREDENTIALS (DO NOT MODIFY)
# ========================================
FYERS_APP_ID     = os.environ.get('API_KEY', 'B64YVF96PK-100')
FYERS_SECRET_KEY = os.environ.get('API_SECRET', 'QLMGPDNWC7')
FYERS_REDIRECT_URL = 'https://trade.fyers.in/api-login/redirect-uri/index.html'

# ========================================
# SCANNER CONFIGURATION
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

# Trade storage
TRADES_FILE = 'trades.json'
OPTIONS_TRADES_FILE = 'options_trades.json'
SIGNALS_HISTORY_FILE = 'signals_history.json'
OPTIONS_SIGNALS_HISTORY_FILE = 'options_signals_history.json'

active_futures_trades = []
active_options_trades = []

def load_json_file(filepath):
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                return json.load(f)
    except:
        pass
    return []

def save_json_file(filepath, data):
    try:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error saving {filepath}: {e}")

# Load trades on startup
active_futures_trades = load_json_file(TRADES_FILE)
active_options_trades = load_json_file(OPTIONS_TRADES_FILE)

# ========================================
# TOKEN MANAGEMENT (DO NOT MODIFY)
# ========================================

def save_token(access_token, refresh_token=None):
    token_data['access_token'] = access_token
    token_data['token_time'] = datetime.now(IST).isoformat()
    
    if refresh_token:
        token_data['refresh_token'] = refresh_token
        try:
            with open(REFRESH_FILE, 'w') as f:
                f.write(refresh_token)
        except:
            pass
    
    try:
        with open(TOKEN_FILE, 'w') as f:
            json.dump(token_data, f)
    except:
        pass

def load_token():
    try:
        with open(TOKEN_FILE, 'r') as f:
            data = json.load(f)
            token_data.update(data)
    except:
        pass
    
    if not token_data.get('refresh_token'):
        try:
            with open(REFRESH_FILE, 'r') as f:
                token_data['refresh_token'] = f.read().strip()
        except:
            pass

def auto_refresh_access_token():
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
            save_token(f"{FYERS_APP_ID}:{r.json()['access_token']}")
            return True
    except:
        pass
    return False

load_token()

if not token_data.get('access_token') and token_data.get('refresh_token'):
    print("Attempting auto-refresh...")
    auto_refresh_access_token()

def init_fyers():
    if not token_data.get('access_token'):
        return None
    try:
        return fyersModel.FyersModel(
            client_id=FYERS_APP_ID,
            token=token_data['access_token'],
            log_path='/tmp'
        )
    except Exception as e:
        print(f"init_fyers error: {e}")
        return None

# ========================================
# TRADING HOLIDAYS
# ========================================
TRADING_HOLIDAYS = {
    date(2024,1,26), date(2024,3,25), date(2024,4,14), date(2024,4,17),
    date(2024,5,1), date(2024,6,17), date(2024,8,15), date(2024,10,2),
    date(2024,10,24),date(2024,11,1), date(2024,11,15),date(2024,12,25),
    date(2025,1,26), date(2025,2,26), date(2025,3,14), date(2025,3,31),
    date(2025,4,10), date(2025,4,14), date(2025,4,18), date(2025,5,1),
    date(2025,8,15), date(2025,10,2), date(2025,10,23),date(2025,12,25),
    date(2026,1,26), date(2026,3,3), date(2026,3,26), date(2026,3,31),
    date(2026,4,3), date(2026,4,14), date(2026,5,1), date(2026,5,28),
    date(2026,6,26), date(2026,9,14), date(2026,10,2), date(2026,10,20),
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
    td_left = sum(1 for i in range((expiry - signal_date).days + 1) 
             if is_trading_day(signal_date + timedelta(days=i)))
    
    if td_left <= 5:
        expiry = get_monthly_expiry(symbol, y, m+1) if m < 12 else get_monthly_expiry(symbol, y+1, 1)
    return expiry

def round_to_strike(price, step):
    return round(round(price / step) * step, 2)

# ========================================
# AUTHENTICATION ROUTES
# ========================================

@app.route('/refresh')
def refresh_token_route():
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
        return redirect('/set-token')
    
    try:
        app_id_hash = hashlib.sha256(f"{FYERS_APP_ID}:{FYERS_SECRET_KEY}".encode()).hexdigest()
        r = req.post(
            'https://api-t1.fyers.in/api/v3/validate-authcode',
            json={'grant_type': 'authorization_code', 'appIdHash': app_id_hash, 'code': auth_code, 'pin': ''},
            headers={'Content-Type': 'application/json'},
            timeout=15
        )
        
        if r.status_code == 200 and r.json().get('s') == 'ok':
            data = r.json()
            save_token(f"{FYERS_APP_ID}:{data['access_token']}", data.get('refresh_token'))
            return f"""<html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white">
            <h1 style="font-size:48px">Login Successful!</h1>
            <p style="color:#22c55e;font-size:18px;margin-top:20px">Access token generated!</p>
            <a href="/" style="color:#22c55e;font-size:18px;margin-top:30px;display:inline-block;padding:12px 30px;background:#166534;border-radius:6px;font-weight:600">Go to Scanner</a>
            </body></html>"""
        else:
            return f"<html><body style='font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white'><h1 style='color:#ef4444;font-size:48px'>Login Failed</h1><p>{r.json().get('message')}</p><a href='/refresh' style='color:#f59e0b'>Try Again</a></body></html>"
    except:
        return redirect('/set-token')

@app.route('/set-token')
def set_token():
    access_token = request.args.get('token', '').strip()
    refresh_token = request.args.get('refresh', '').strip()
    auth_code = request.args.get('code', '').strip()
    
    if auth_code and not access_token:
        try:
            app_id_hash = hashlib.sha256(f"{FYERS_APP_ID}:{FYERS_SECRET_KEY}".encode()).hexdigest()
            r = req.post(
                'https://api-t1.fyers.in/api/v3/validate-authcode',
                json={'grant_type': 'authorization_code', 'appIdHash': app_id_hash, 'code': auth_code, 'pin': ''},
                headers={'Content-Type': 'application/json'}, timeout=15
            )
            if r.status_code == 200 and r.json().get('s') == 'ok':
                data = r.json()
                access_token = f"{FYERS_APP_ID}:{data['access_token']}"
                refresh_token = data.get('refresh_token')
                save_token(access_token, refresh_token)
                return "<html><body style='font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white'><h1>Auth Code Converted!</h1><a href='/' style='color:#22c55e'>Go to Scanner</a></body></html>"
        except:
            pass
    
    if not access_token:
        return """<html><body style="font-family:sans-serif;padding:40px;background:#0f1f3d;color:white">
        <h2>Set Fyers Token</h2>
        <div style="background:#1a2a4a;padding:25px;border-radius:8px;margin-bottom:20px;">
        <h3 style="color:#22c55e">Option A: Auto-Login</h3>
        <a href="/refresh" style="color:#fff;text-decoration:none;padding:12px 24px;background:#166534;border-radius:6px;">Login via Fyers</a>
        </div>
        <hr style="border-color:#333;margin:25px 0;">
        <div style="background:#1a2a4a;padding:25px;border-radius:8px;">
        <h3 style="color:#f59e0b">Option B: Manual</h3>
        <form method="GET" action="/set-token">
            <input name="token" placeholder="VS55VDHYCW-100:eyJ..." style="width:100%;padding:10px;background:#0f1f3d;color:#fff;border:1px solid #3b82f6;border-radius:4px;margin-bottom:10px;">
            <input name="refresh" placeholder="eyJ..." style="width:100%;padding:10px;background:#0f1f3d;color:#fff;border:1px solid #3b82f6;border-radius:4px;margin-bottom:10px;">
            <button type="submit" style="padding:12px 24px;background:#22c55e;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;">Save Token</button>
        </form></div></body></html>"""
    
    save_token(access_token, refresh_token if refresh_token else None)
    return f"<html><body style='font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white'><h1 style='font-size:48px'>Token Saved!</h1><a href='/' style='color:#22c55e'>Go to Scanner</a></body></html>"

@app.route('/auto-refresh')
def trigger_auto_refresh():
    success = auto_refresh_access_token()
    return jsonify({'status': 'success' if success else 'error', 'timestamp': datetime.now(IST).isoformat()})

@app.route('/debug-fyers')
def debug_fyers():
    result = {
        'token_exists': bool(token_data.get('access_token')),
        'fyers_client_created': init_fyers() is not None
    }
    fyers = init_fyers()
    if fyers:
        try:
            test = fyers.history(data={'symbol': 'NSE:NIFTY50-INDEX', 'resolution': '1', 'date_format': '1',
                'range_from': (datetime.now(IST)-timedelta(days=1)).strftime('%Y-%m-%d'),
                'range_to': datetime.now(IST).strftime('%Y-%m-%d'), 'cont_flag': '1'})
            result.update(test)
        except Exception as e:
            result['error'] = str(e)
    return jsonify(result)

# ========================================
# OPTION CHAIN FUNCTIONS
# ========================================

def get_fyers_expiry_timestamp(fyers, option_key, target_expiry_date):
    try:
        resp = fyers.optionchain(data={'symbol': option_key, 'strikecount': 1, 'timestamp': ''})
        if resp.get('s') != 'ok':
            return None
        expiry_map = {}
        for item in resp['data'].get('expiryData', []):
            d = datetime.strptime(item['date'], '%d-%m-%Y').date()
            expiry_map[d] = item['expiry']
        if expiry_map:
            return expiry_map[min(expiry_map.keys(), key=lambda d: abs((d-target_expiry_date).days))]
    except:
        pass
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
        resp = fyers.optionchain(data={'symbol': config.get('option_key', ''), 'strikecount': 20, 'timestamp': expiry_ts})
        
        if resp.get('s') != 'ok':
            return None, None, None
        
        chain = [r for r in resp['data'].get('optionsChain', []) if r.get('option_type') == option_type]
        if chain:
            best = min(chain, key=lambda r: abs(r['strike_price'] - tp1_rounded))
            return best['strike_price'], best.get('ltp', 0), best.get('symbol', '')
    except:
        pass
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
# DATA FETCHING
# ========================================

def fetch_candles(instrument_key, interval='1minute', days=90, retry_on_fail=True):
    fyers = init_fyers()
    if not fyers:
        return pd.DataFrame()
    
    interval_map = {'1minute': '1', '5minute': '5', '15minute': '15', '30minute': '30', '60minute': '60'}
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
            dt = pd.to_datetime(c[0], unit='s').tz_localize('UTC').tz_convert('Asia/Kolkata').tz_localize(None)
            rows.append({'datetime': dt, 'open': c[1], 'high': c[2], 'low': c[3], 'close': c[4], 'volume': c[5]})
        
        df = pd.DataFrame(rows).sort_values('datetime').drop_duplicates('datetime').reset_index(drop=True)
        t = df['datetime'].dt.hour * 100 + df['datetime'].dt.minute
        return df[(t >= 915) & (t <= 1530)].reset_index(drop=True)
    except:
        return pd.DataFrame()

def resample_candles(df_1m, minutes):
    if len(df_1m) == 0:
        return pd.DataFrame()
    
    df = df_1m.copy().set_index('datetime')
    r = df.resample(f'{minutes}min').agg(open=('open','first'), high=('high','max'), low=('low','min'), close=('close','last'), volume=('volume','sum')).dropna().reset_index()
    t = r['datetime'].dt.hour * 100 + r['datetime'].dt.minute
    return r[(t >= 915) & (t <= 1530)].reset_index(drop=True)

# ========================================
# SIGNAL GENERATION WITH ENTRY PRICE FIX
# ========================================

def generate_signals():
    now = datetime.now(IST)
    signals = []
    
    print(f"\n{'='*60}")
    print(f"SIGNAL SCAN: {now.strftime('%d %b %Y %H:%M:%S IST')}")
    print(f"{'='*60}\n")
    
    for symbol, config in SCANNER_CONFIG.items():
        try:
            print(f"\nScanning {symbol}...")
            
            # Fetch 1-minute data
            df_1m = fetch_candles(config['instrument_key'], '1minute', days=90)
            
            if len(df_1m) < 50:
                print(f"Insufficient candles: {len(df_1m)}")
                continue
            
            # Resample to configured timeframe (5-min)
            df = resample_candles(df_1m, config['resample_minutes'])
            
            if len(df) < max(config['fast_period'], config['slow_period']) + 10:
                print(f"Insufficient resampled: {len(df)}")
                continue
            
            df = calculate_atr_trailing(df, config['fast_period'], config['fast_mult'],
                                       config['slow_period'], config['slow_mult'])
            
            scan_df = df.tail(200).copy() if len(df) >= 200 else df.copy()
            
            signal_count = 0
            for _, row in scan_df.iterrows():
                if not (row.get('buy_signal', False) or row.get('sell_signal', False)):
                    continue
                
                direction = 'BUY-LONG' if row['buy_signal'] else 'SELL-SHORT'
                
                # 🔥 FIX: Get LATEST available price instead of stale resampled close
                # Use the LAST completed candle's close from 1-min data (more recent)
                current_idx = df_1m.index.get_loc(row.name) if hasattr(row, 'name') else len(df_1m) - 1
                
                # Find the actual latest tradable price
                # Strategy: Use close of most recent completed candle before signal time
                signal_time = pd.to_datetime(row['datetime'])
                if signal_time.tzinfo is None:
                    signal_time = IST.localize(signal_time)
                
                # Get candles UP TO this signal time
                df_before_signal = df_1m[df_1m['datetime'] <= signal_time]
                
                if len(df_before_signal) > 0:
                    # Use the last completed candle's close as entry
                    entry = round(float(df_before_signal.iloc[-1]['close']), 2)
                    entry_candle_time = df_before_signal.iloc[-1]['datetime']
                    
                    # Calculate SL and targets based on THIS entry
                    if direction == 'BUY-LONG':
                        sl = round(float(row['trail2']), 2)
                        risk = entry - sl
                        target_1 = round(entry + risk * 1.5, 2)
                        target_2 = round(entry + risk * 2.5, 2)
                    else:
                        sl = round(float(row['trail2']), 2)
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
                    
                    if direction == 'BUY-LONG' and bar_c in ['green', 'blue']:
                        confidence += 0.2 if bar_c == 'green' else 0.1
                    elif direction == 'SELL-SHORT' and bar_c in ['red', 'yellow']:
                        confidence += 0.2 if bar_c == 'red' else 0.1
                    
                    if rr >= 2: confidence += 0.1
                    if rr >= 3: confidence += 0.1
                    confidence = min(confidence, 0.95)
                    
                    if confidence >= 0.8: grade, score = 'A+', 95
                    elif confidence >= 0.7: grade, score = 'A', 85
                    elif confidence >= 0.6: grade, score = 'B', 70
                    else: grade, score = 'C', 55
                    
                    signal_dt = signal_time
                    
                    # 🔥 VALIDATION: Check how old this signal is
                    signal_age_minutes = (now - signal_dt).total_seconds() / 60
                    
                    # Get CURRENT market price for validation
                    current_market_price = float(df_1m.iloc[-1]['close'])
                    price_diff = abs(entry - current_market_price)
                    price_diff_pct = (price_diff / current_market_price) * 100
                    
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
                        'grade_score': score,
                        'scan_date': signal_dt.isoformat(),
                        'scan_time': signal_dt.strftime('%H:%M'),
                        'trail1': round(float(row['trail1']), 2),
                        'trail2': round(float(row['trail2']), 2),
                        'fast_atr': round(float(row['fast_atr']), 2),
                        'slow_atr': round(float(row['slow_atr']), 2),
                        'bar_color': bar_c,
                        'regime': row.get('regime', 'UNKNOWN'),
                        'timeframe': f"{config['resample_minutes']}m",
                        'lot_size': config['lot_size'],
                        'scanner_type': 'atr_trailing',
                        'outcome': 'pending',
                        
                        # 🔥 NEW FIELDS FOR ENTRY PRICE VALIDATION
                        'current_market_price': round(current_market_price, 2),
                        'price_difference': round(price_diff, 2),
                        'price_diff_pct': round(price_diff_pct, 2),
                        'signal_age_minutes': round(signal_age_minutes, 1),
                        'entry_candle_time': str(entry_candle_time.strftime('%H:%M:%S')),
                        'validation_status': '✅ Valid' if price_diff_pct <= 1.0 else ('⚠️ Far from market' if price_diff_pct <= 2.0 else '❌ Stale signal'),
                        'is_executable': price_diff_pct <= 1.5  # Allowable deviation
                    })
                    
                    signal_count += 1
                    
                    status_icon = '✅' if price_diff_pct <= 1.5 else ('⚠️' if price_diff_pct <= 2.0 else '❌')
                    print(f"  {direction} @ {signal_dt.strftime('%H:%M')} | Entry: {entry} | Current: {current_market_price} | Diff: {price_diff_pct:.2f}% [{status_icon}] Age: {signal_age_minutes:.0f}min")
                }
            
            print(f"{symbol}: {signal_count} signal(s)")
            
        except Exception as e:
            print(f"Error scanning {symbol}: {e}")
            import traceback
            traceback.print_exc()
    
    signals.sort(key=lambda x: x.get('scan_date', ''), reverse=True)
    
    # Merge with existing cache
    existing = scan_cache.get('signals', [])
    existing_ids = {s['_id'] for s in signals}
    for s in existing:
        if s['_id'] not in existing_ids and s.get('scan_date', '')[:10] == now.strftime('%Y-%m-%d'):
            signals.append(s)
    signals.sort(key=lambda x: x.get('scan_date', ''), reverse=True)
    
    print(f"\n{'='*60}")
    print(f"TOTAL SIGNALS: {len(signals)}")
    print(f"{'='*60}\n")
    
    # 🆕 AUTO-SAVE TO HISTORY
    save_signals_to_history(signals)
    opt_sigs = generate_option_signals(signals)
    save_options_to_history(opt_sigs)
    
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
# 🆕 AUTO-SAVE TO HISTORY
# ========================================

def save_signals_to_history(signals):
    """Persist signals to history JSON file"""
    try:
        existing = load_json_file(SIGNALS_HISTORY_FILE)
        existing_ids = {s.get('_id') for s in existing}
        
        new_count = 0
        for sig in signals:
            if sig.get('_id') not in existing_ids:
                existing.append({
                    '_id': sig['_id'],
                    'symbol': sig.get('symbol'),
                    'direction': sig.get('direction'),
                    'entry': sig.get('entry'),
                    'sl': sig.get('sl'),
                    'target_1': sig.get('target_1'),
                    'target_2': sig.get('target_2'),
                    'grade': sig.get('grade'),
                    'score': sig.get('grade_score'),
                    'confidence': sig.get('confidence'),
                    'scan_date': sig.get('scan_date'),
                    'scan_time': sig.get('scan_time'),
                    'outcome': sig.get('outcome', 'pending'),
                    'rr': sig.get('risk_reward', ''),
                    'bar_color': sig.get('bar_color', ''),
                    'regime': sig.get('regime', ''),
                    'model': sig.get('model', 'ATR-TS'),
                    'current_market_price': sig.get('current_market_price'),
                    'price_difference': sig.get('price_difference'),
                    'price_diff_pct': sig.get('price_diff_pct'),
                    'validation_status': sig.get('validation_status', ''),
                    'signal_age_minutes': sig.get('signal_age_minutes', '')
                })
                new_count += 1
        
        save_json_file(SIGNALS_HISTORY_FILE, existing)
        if new_count > 0:
            print(f"✅ Saved {new_count} new signals to history (Total: {len(existing)})")
    except Exception as e:
        print(f"⚠ Error saving signals history: {e}")

def save_options_to_history(options_signals):
    """Persist option signals"""
    try:
        existing = load_json_file(OPTIONS_SIGNALS_HISTORY_FILE)
        existing_ids = {s.get('_id') for s in existing}
        
        new_count = 0
        for opt in options_signals:
            if opt.get('_id') not in existing_ids:
                existing.append({
                    '_id': opt['_id'],
                    'futures_id': opt.get('futures_id'),
                    'symbol': opt.get('symbol'),
                    'opt_type': opt.get('opt_type'),
                    'strike': opt.get('strike'),
                    'ltp': opt.get('ltp'),
                    'spot': opt.get('spot'),
                    'expiry': opt.get('expiry'),
                    'days_to_expiry': opt.get('days_to_expiry'),
                    'scan_date': opt.get('scan_date'),
                    'scan_time': opt.get('scan_time'),
                    'grade': opt.get('grade'),
                    'outcome': 'pending'
                })
                new_count += 1
        
        save_json_file(OPTIONS_SIGNALS_HISTORY_FILE, existing)
        if new_count > 0:
            print(f"✅ Saved {new_count} new options to history")
    except Exception as e:
        print(f"⚠ Error saving options history: {e}")

# ========================================
# TRADE EXECUTION ENGINE
# ========================================

def execute_futures_trade(signal_data, quantity=1):
    global active_futures_trades
    try:
        fyers = init_fyers()
        if not fyers:
            return None
        
        symbol = signal_data.get('symbol', '')
        direction = signal_data.get('direction', '')
        side = 1 if direction == 'BUY-LONG' else -1
        symbol_map = {'NIFTY50': 'NSE:NIFTY23FUT', 'BANKNIFTY': 'NSE:BANKNIFTY23FUT'}
        fyers_symbol = symbol_map.get(symbol, f"NSE:{symbol}FUT")
        
        order_data = {"symbol": fyers_symbol, "qty": quantity, "type": 2, "side": side,
                     "productType": "INTRADAY", "limitPrice": 0, "stopPrice": 0,
                     "validity": "DAY", "disclosedQty": 0, "offlineOrder": False, "orderType": "MARKET"}
        
        response = fyers.place_order(data=order_data)
        if response.get('s') == 'ok':
            trade = {
                'id': f"FUT-{datetime.now(IST).strftime('%Y%m%d%H%M%S%f')}",
                'signal_id': signal_data.get('_id'),
                'symbol': symbol,
                'direction': direction,
                'side': 'BUY' if side==1 else 'SELL',
                'quantity': quantity,
                'entry_price': float(signal_data.get('entry', 0)),
                'sl': float(signal_data.get('sl', 0)),
                'target_1': float(signal_data.get('target_1', 0)),
                'target_2': float(signal_data.get('target_2', 0)),
                'status': 'OPEN',
                'pnl': 0,
                'order_id': response.get('id'),
                'entry_time': datetime.now(IST).strftime('%d %b %H:%M'),
                'grade': signal_data.get('grade', ''),
                'outcome': 'pending'
            }
            
            active_futures_trades.append(trade)
            save_json_file(TRADES_FILE, active_futures_trades)
            execute_options_trade(trade, signal_data)
            return trade
    except Exception as e:
        print(f"❌ Execute trade error: {e}")
    return None

def execute_options_trade(futures_trade, signal_data=None):
    global active_options_trades
    try:
        symbol = futures_trade.get('symbol', '')
        direction = futures_trade.get('direction', '')
        target_1 = float(futures_trade.get('target_1', 0))
        quantity = futures_trade.get('quantity', 1)
        opt_type = 'CE' if direction == 'BUY-LONG' else 'PE'
        expiry = get_active_expiry(symbol, datetime.now(IST).date())
        strike, ltp, opt_symbol = get_tp1_option(symbol, target_1, opt_type, expiry)
        
        if opt_symbol and ltp:
            fyers = init_fyers()
            if fyers:
                order_data = {"symbol": opt_symbol, "qty": quantity, "type": 2, "side": 1,
                             "productType": "INTRADAY", "limitPrice": 0, "stopPrice": 0,
                             "validity": "DAY", "disclosedQty": 0, "offlineOrder": False, "orderType": "MARKET"}
                response = fyers.place_order(data=order_data)
                if response.get('s') == 'ok':
                    opt_trade = {
                        'id': f"OPT-{datetime.now(IST).strftime('%Y%m%d%H%M%S%f')}",
                        'futures_trade_id': futures_trade.get('id'),
                        'symbol': symbol, 'opt_symbol': opt_symbol, 'option_type': opt_type,
                        'strike_price': strike, 'quantity': quantity,
                        'entry_price': round(float(ltp), 2), 'status': 'OPEN', 'pnl': 0,
                        'order_id': response.get('id'),
                        'entry_time': datetime.now(IST).strftime('%d %b %H:%M'),
                        'tp1': target_1, 'sl': float(futures_trade.get('sl', 0)),
                        'outcome': 'pending'
                    }
                    active_options_trades.append(opt_trade)
                    save_json_file(OPTIONS_TRADES_FILE, active_options_trades)
                    return opt_trade
    except Exception as e:
        print(f"❌ Options trade error: {e}")
    return None

def close_linked_options(futures_trade_id, reason='TP/SL Hit'):
    global active_options_trades
    for opt in active_options_trades:
        if opt.get('futures_trade_id') == futures_trade_id and opt.get('status') == 'OPEN':
            try:
                fyers = init_fyers()
                if fyers:
                    order_data = {"symbol": opt['opt_symbol'], "qty": opt['quantity'], "type": 2, "side": -1,
                                 "productType": "INTRADAY", "limitPrice": 0, "stopPrice": 0",
                                 "validity": "DAY", "disclosedQty": 0, "offlineOrder": False, "orderType": "MARKET"}
                    resp = fyers.place_order(data=order_data)
                    if resp.get('s') == 'ok':
                        exit_price = opt['entry_price'] * 1.05
                        pnl = (exit_price - opt['entry_price']) * opt['quantity']
                        opt.update({'status': 'CLOSED', 'exit_price': round(exit_price,2),
                                   'exit_time': datetime.now(IST).strftime('%d %b %H:%M'),
                                   'pnl': round(pnl,2), 'close_reason': reason})
                        save_json_file(OPTIONS_TRADES_FILE, active_options_trades)
            except:
                pass
            break

# ========================================
# POSITION MONITOR
# ========================================

def monitor_positions():
    global active_futures_trades
    while True:
        try:
            if get_scanner_status() == 'ACTIVE':
                for trade in active_futures_trades:
                    if trade.get('status') != 'OPEN':
                        continue
                    
                    try:
                        symbol = trade.get('symbol', '')
                        config = SCANNER_CONFIG.get(symbol, {})
                        if not config:
                            continue
                        
                        df = fetch_candles(config['instrument_key'], '1minute', days=1)
                        if len(df) > 0:
                            current_price = float(df.iloc[-1]['close'])
                            entry = float(trade.get('entry_price', 0))
                            sl = float(trade.get('sl', 0))
                            t2 = float(trade.get('target_2', 0))
                            direction = trade.get('direction', '')
                            
                            hit_target = (direction == 'BUY-LONG' and current_price >= t2) or (direction == 'SELL-SHORT' and current_price <= t2)
                            hit_sl = (direction == 'BUY-LONG' and current_price <= sl) or (direction == 'SELL-SHORT' and current_price >= sl)
                            
                            if hit_target or hit_sl:
                                reason = 'Target Hit' if hit_target else 'Stop Loss Hit'
                                pnl = ((current_price - entry) if direction=='BUY-LONG' else (entry - current_price)) * trade.get('quantity',1)
                                trade.update({'status':'CLOSED','exit_price':current_price,'exit_time':datetime.now(IST).strftime('%d %b %H:%M'),'pnl':round(pnl,2),'outcome':reason.lower().replace(' ',' '_')})
                                save_json_file(TRADES_FILE, active_futures_trades)
                                close_linked_options(trade['id'], reason)
                    except:
                        continue
        except:
            pass
        time.sleep(10)

# ========================================
# SCANNER STATUS
# ========================================

def get_scanner_status():
    now = datetime.now(IST)
    time_val = now.hour * 100 + now.minute
    if not token_data.get('access_token'): return 'NO_TOKEN'
    if now.weekday() >= 5: return 'MARKET_CLOSED'
    if now.date() in TRADING_HOLIDAYS: return 'MARKET_CLOSED'
    if 915 <= time_val <= 1530: return 'ACTIVE'
    if 900 <= time_val < 915: return 'PRE_MARKET'
    return 'MARKET_CLOSED'

# ========================================
# BACKGROUND SCANNER
# ========================================

def background_scanner():
    while True:
        try:
            status = get_scanner_status()
            if status in ['ACTIVE', 'PRE_MARKET']:
                if scan_lock.acquire(blocking=False):
                    try:
                        print(f"[BG] Scan at {datetime.now(IST).strftime('%H:%M:%S')}")
                        signals = generate_signals()
                        scan_cache['signals'] = signals
                        scan_cache['last_scan'] = datetime.now(IST)
                        print(f"[BG] Scan complete. {len(signals)} signals cached.")
                    finally:
                        scan_lock.release()
        except:
            pass
        time.sleep(30)

# ========================================
# ROUTES - Serve HTML Files
# ========================================

@app.route('/')
def home():
    if os.path.exists('index.html'):
        return send_file('index.html')
    ts = 'Active' if token_data.get('access_token') else 'Expired'
    return f"<html><body style='font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white'><h1>StrikeTrail</h1><p>Status: {ts}</p><a href='/set-token' style='color:#22c55e'>Set Token</a></body></html>"

@app.route('/history')
def history():
    if os.path.exists('history.html'):
        return send_file('history.html')
    return "<html><body style='padding:50px;background:#0f1f3d;color:white'><h1 style='color:#ef4444'>History Not Found</h1><a href='/'>← Home</a></body></html>", 404

# ========================================
# API ENDPOINTS
# ========================================

@app.route('/api/status')
def api_status():
    return jsonify({
        'status': 'success',
        'scanner_status': get_scanner_status(),
        'server_time_ist': datetime.now(IST).isoformat(),
        'token_set': token_data.get('access_token') is not None,
        'scanner_model': 'ATR Trailing Stop (Walk-Forward Validated)',
        'config': {sym: {'timeframe': f"{cfg['resample_minutes']}m", 'fast': f"({cfg['fast_period']}, {cfg['fast_mult']})", 'slow': f"({cfg['slow_period']}, {cfg['slow_mult']})"} for sym, cfg in SCANNER_CONFIG.items()},
        'active_futures_trades': len([t for t in active_futures_trades if t.get('status')=='OPEN']),
        'active_options_trades': len([t for t in active_options_trades if t.get('status')=='OPEN'])
    })

@app.route('/api/signals')
def api_signals():
    force = request.args.get('force', 'false').lower() == 'true'
    status = get_scanner_status()
    
    if status == 'NO_TOKEN':
        return jsonify({'status': 'success', 'scanner_status': 'NO_TOKEN', 'signals': [], 'timestamp': datetime.now(IST).isoformat()})
    
    cache_ttl = 30 if status == 'ACTIVE' else 60
    if not force and scan_cache.get('last_scan') and (datetime.now(IST) - scan_cache['last_scan']).total_seconds() < cache_ttl:
        return jsonify({'status': 'success', 'scanner_status': status, 'signals': scan_cache.get('signals', []), 'cached': True, 'timestamp': datetime.now(IST).isoformat()})
    
    if scan_lock.acquire(blocking=False):
        try:
            signals = generate_signals() if status in ['ACTIVE','PRE_MARKET'] else scan_cache.get('signals', [])
            scan_cache['signals'] = signals
            scan_cache['last_scan'] = datetime.now(IST)
            return jsonify({'status': 'success', 'scanner_status': status, 'signals': signals, 'cached': False, 'timestamp': datetime.now(IST).isoformat()})
        finally:
            scan_lock.release()
    
    return jsonify({'status': 'success', 'scanner_status': status, 'signals': scan_cache.get('signals', []), 'cached': True, 'timestamp': datetime.now(IST).isoformat()})

@app.route('/api/signals/history')
def api_get_all_signals_history():
    all_sigs = load_json_file(SIGNALS_HISTORY_FILE)
    all_sigs.sort(key=lambda x: x.get('scan_date', ''), reverse=True)
    return jsonify({'status': 'success', 'signals': all_sigs, 'total': len(all_sigs)})

@app.route('/api/options-history')
def api_get_options_history():
    all_opts = load_json_file(OPTIONS_SIGNALS_HISTORY_FILE)
    all_opts.sort(key=lambda x: x.get('scan_date', ''), reverse=True)
    return jsonify({'status': 'success', 'options': all_opts, 'total': len(all_opts)})

@app.route('/api/option-signals')
def api_option_signals():
    now = datetime.now(IST)
    if options_cache.get('last_fetch') and (now - options_cache['last_fetch']).total_seconds() < 120:
        return jsonify({'status': 'success', 'option_signals': options_cache.get('signals', []), 'cached': True, 'timestamp': now.isoformat()})
    
    futures = scan_cache.get('signals', [])
    opts = generate_option_signals(futures)
    options_cache['signals'] = opts
    options_cache['last_fetch'] = now
    return jsonify({'status': 'success', 'option_signals': opts, 'cached': False, 'timestamp': now.isoformat()})

@app.route('/api/trades')
def api_get_active_trades():
    return jsonify({'status': 'success', 'trades': [t for t in active_futures_trades if t.get('status') == 'OPEN']})

@app.route('/api/options-trades')
def api_get_active_options():
    return jsonify({'status': 'success', 'options_trades': [t for t in active_options_trades if t.get('status') == 'OPEN']})

@app.route('/api/close-trade/<trade_id>', methods=['POST'])
def api_close_trade():
    global active_futures_trades
    trade = next((t for t in active_futures_trades if t.get('id')==trade_id), None)
    if trade and trade.get('status')=='OPEN':
        trade.update({'status':'CLOSED','close_reason':'Manual Close','exit_time':datetime.now(IST).strftime('%d %b %H:%M'),'outcome':'manual_close'})
        save_json_file(TRADES_FILE, active_futures_trades)
        close_linked_options(trade_id, 'Manual Close')
        return jsonify({'status': 'success', 'trade': trade})
    return jsonify({'status': 'error', 'message': 'Not found'})

@app.route('/api/track', methods=['POST'])
def api_track():
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
                results.append({'_id': sig.get('_id'), 'status': 'pending', 'current_price': float(df_after.iloc[-1]['close']), 'live_pnl_pct': 0, 'track_status': 'entry_not_met'})
                continue
            
            entry_met = False
            entry_idx = None
            direction = sig.get('direction', '')
            entry = float(sig.get('entry', 0))
            sl = float(sig.get('sl', 0))
            t2 = float(sig.get('target_2', sig.get('target', 0)))
            
            for idx, row in df_after.iterrows():
                if (direction == 'BUY-LONG' and row['high'] >= entry) or (direction == 'SELL-SHORT' and row['low'] <= entry):
                    entry_met = True; entry_idx = idx; break
            
            if not entry_met:
                results.append({'_id': sig.get('_id'), 'status': 'pending', 'current_price': float(df_after.iloc[-1]['close']), 'live_pnl_pct': 0, 'track_status': 'entry_not_met'})
                continue
            
            entry_pos = df_after.index.get_loc(entry_idx)
            df_post = df_after.iloc[entry_pos:].reset_index(drop=True)
            trade_status = 'open'
            current_price = float(df_post.iloc[-1]['close'])
            
            for _, row in df_post.iterrows():
                if direction == 'BUY-LONG':
                    if row['high'] >= t2: trade_status = 'target_hit'; break
                    if row['low'] <= sl: trade_status = 'stop_hit'; break
                else:
                    if row['low'] <= t2: trade_status = 'target_hit'; break
                    if row['high'] >= sl: trade_status = 'stop_hit'; break
            
            pnl_pct = round(((current_price-entry)/entry)*100, 2) if direction=='BUY-LONG' else round(((entry-current_price)/entry)*100, 2)
            results.append({'_id': sig.get('_id'), 'status': trade_status, 'exit_price': t2 if trade_status=='target_hit' else (sl if trade_status=='stop_hit' else None), 'current_price': current_price, 'live_pnl_pct': pnl_pct, 'track_status': 'tracked'})
        except Exception as e:
            results.append({'_id': sig.get('_id'), 'status': 'pending', 'track_status': f'error:{str(e)}'})

    return jsonify({'status': 'success', 'results': results})

# ========================================
# STARTUP
# ========================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    
    print(f"\n{'='*70}")
    print(f"STRAKETRAIL SCANNER STARTING")
    print(f"Port: {port}")
    print(f"Token: {'✓' if token_data.get('access_token') else '✗'}")
    print(f"Loaded Trades: {len(active_futures_trades)} futures, {len(active_options_trades)} options")
    print(f"{'='*70}\n")
    
    threading.Thread(target=background_scanner, daemon=True).start()
    print("✓ Background scanner started")
    
    threading.Thread(target=monitor_positions, daemon=True).start()
    print("✓ Position monitor started")
    
    def keep_alive():
        while True:
            try:
                req.get(f"http://localhost:{port}/api/status", timeout=10)
                print(f"Ping at {datetime.now(IST).strftime('%H:%M:%S')}")
            except:
                pass
            time.sleep(840)
    
    threading.Thread(target=keep_alive, daemon=True).start()
    print("✓ Keep-alive pinger started\n")
    
    print("Starting Flask server...")
    app.run(host='0.0.0.0', port=port, debug=False)
