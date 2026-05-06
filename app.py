from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import json
import os
import threading
import time
from datetime import datetime
import binance.client
from binance.client import Client
from binance.enums import *
import logging
from functools import wraps

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# ==================== TOKENS SECTION - DO NOT MODIFY ====================
CONFIG_FILE = 'config.json'

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {
        'binance_api_key': '',
        'binance_api_secret': '',
        'testnet': True,
        'webhook_token': 'your-secure-token-here'
    }

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

config = load_config()

def get_binance_client():
    if config['binance_api_key'] and config['binance_api_secret']:
        return Client(config['binance_api_key'], config['binance_api_secret'], testnet=config['testnet'])
    return None

client = get_binance_client()
# ==================== END TOKENS SECTION ====================

# Trade storage files
trades_file = 'trades.json'
options_trades_file = 'options_trades.json'

def load_json(filepath):
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            return json.load(f)
    return []

def save_json(filepath, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=4)

# Load existing trades (PRESERVES OLD DATA)
active_trades = load_json(trades_file)
active_options_trades = load_json(options_trades_file)

# Helper: Find closest strike price
def find_closest_strike(price, symbol):
    """NIFTY50: ±50, BANKNIFTY: ±100"""
    if 'NIFTY' in symbol.upper() and 'BANK' not in symbol.upper():
        interval = 50
    elif 'BANKNIFTY' in symbol.upper():
        interval = 100
    else:
        interval = 100
    
    return int(round(price / interval) * interval)

# Helper: Get option symbol
def get_option_symbol(base_symbol, strike, option_type):
    """Format: NIFTY24JAN2422500CE or BANKNIFTY24JAN2445000PE"""
    today = datetime.now()
    days_ahead = (3 - today.weekday()) % 7 or 7
    expiry = (today + __import__('datetime').timedelta(days=days_ahead)).strftime('%d%b%y').upper()
    return f"{base_symbol}{expiry}{strike}{option_type}"

# Execute Options Trade (Auto-called when Futures trade opens)
def execute_options_trade(futures_trade):
    try:
        client = get_binance_client()
        if not client:
            return None
        
        symbol = futures_trade.get('symbol', '')
        side = futures_trade.get('side', '').upper()
        quantity = futures_trade.get('quantity', 0)
        tp1 = float(futures_trade.get('tp1', futures_trade.get('t1', 0)))
        
        if not all([symbol, tp1 > 0, quantity > 0]):
            return None
        
        # CE for BUY, PE for SELL
        option_type = 'CE' if side == 'BUY' else 'PE'
        strike_price = find_closest_strike(tp1, symbol)
        option_symbol = get_option_symbol(symbol, strike_price, option_type)
        
        logger.info(f"🎯 Options: {option_symbol} | {option_type} | Strike: {strike_price}")
        
        # Place MARKET order (always BUY)
        order = client.create_order(
            symbol=option_symbol,
            side=SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=quantity
        )
        
        entry_price = float(order.get('avgPrice') or order.get('price') or 0)
        
        options_trade = {
            'id': f"OPT-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
            'symbol': option_symbol,
            'original_symbol': symbol,
            'side': 'BUY',
            'option_type': option_type,
            'strike_price': strike_price,
            'quantity': quantity,
            'entry_price': entry_price,
            'entry_time': datetime.now().strftime('%d %b %H:%M'),
            'status': 'OPEN',
            'pnl': 0,
            'pnl_percentage': 0,
            'linked_futures_id': futures_trade.get('id', ''),
            'tp1': tp1,
            'sl': float(futures_trade.get('sl', 0)),
            'grade': futures_trade.get('grade', 'A'),
            'score': futures_trade.get('score', 85),
            'confidence': futures_trade.get('confidence', '80%')
        }
        
        active_options_trades.append(options_trade)
        save_json(options_trades_file, active_options_trades)
        socketio.emit('options_update', options_trade)
        
        return options_trade
        
    except Exception as e:
        logger.error(f"❌ Options execution failed: {e}")
        return None

# Close Options when Futures closes
def close_linked_options(futures_id, reason='TP/SL Hit'):
    global active_options_trades
    
    for opt_trade in active_options_trades:
        if opt_trade.get('linked_futures_id') == futures_id and opt_trade['status'] == 'OPEN':
            try:
                client = get_binance_client()
                close_order = client.create_order(
                    symbol=opt_trade['symbol'],
                    side=SIDE_SELL,
                    type=ORDER_TYPE_MARKET,
                    quantity=opt_trade['quantity']
                )
                
                exit_price = float(close_order.get('avgPrice') or close_order.get('price') or 0)
                pnl = (exit_price - opt_trade['entry_price']) * opt_trade['quantity']
                pnl_pct = (pnl / (opt_trade['entry_price'] * opt_trade['quantity'])) * 100
                
                opt_trade.update({
                    'exit_price': exit_price,
                    'exit_time': datetime.now().strftime('%d %b %H:%M'),
                    'status': 'CLOSED',
                    'pnl': round(pnl, 2),
                    'pnl_percentage': round(pnl_pct, 2),
                    'close_reason': reason
                })
                
                save_json(options_trades_file, active_options_trades)
                socketio.emit('options_closed', opt_trade)
                logger.info(f"✅ Options Closed: {opt_trade['symbol']} | {reason}")
                
            except Exception as e:
                logger.error(f"❌ Options close error: {e}")

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get('X-API-Key') != config['webhook_token']:
            return jsonify({'error': 'Invalid API key'}), 401
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/history')
def history():
    return render_template('history.html')

@app.route('/webhook', methods=['POST'])
@require_api_key
def webhook():
    try:
        data = request.json
        symbol = data.get('symbol', '')
        action = data.get('action', '').upper()
        quantity = float(data.get('quantity', 0))
        leverage = int(data.get('leverage', 1))
        entry = float(data.get('entry', 0))
        sl = float(data.get('sl', 0))
        t1 = float(data.get('t1', 0))
        t2 = float(data.get('t2', 0))
        grade = data.get('grade', 'A')
        score = int(data.get('score', 85))
        confidence = data.get('confidence', '80%')
        
        if not all([symbol, action, quantity > 0, entry > 0]):
            return jsonify({'error': 'Missing params'}), 400
        
        side = SIDE_BUY if action == 'BUY' else SIDE_SELL
        
        try:
            client.futures_change_leverage(symbol=symbol, leverage=leverage)
        except:
            pass
        
        order = client.futures_create_order(
            symbol=symbol, side=side, type=ORDER_TYPE_MARKET, quantity=quantity
        )
        
        actual_entry = float(order.get('avgPrice') or order.get('price') or entry)
        
        trade = {
            'id': f"FUT-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
            'date': datetime.now().strftime('%d %b %H:%M'),
            'symbol': symbol,
            'dir': action,
            'grade': grade,
            'score': score,
            'entry': actual_entry,
            'sl': sl,
            't1': t1,
            't2': t2,
            'rr': '1:2.5',
            'conf': confidence,
            'outcome': 'pending',
            'status': 'OPEN',
            'pnl': 0,
            'quantity': quantity,
            'side': side,
            'tp1': t1  # For options calculation
        }
        
        active_trades.append(trade)
        save_json(trades_file, active_trades)
        socketio.emit('new_signal', trade)
        
        # AUTO-EXECUTE OPTIONS TRADE
        execute_options_trade(trade)
        
        return jsonify({'success': True, 'trade_id': trade['id']})
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/update-outcome/<trade_id>', methods=['POST'])
def update_outcome(trade_id):
    data = request.json
    outcome = data.get('outcome', '')
    exit_price = float(data.get('exit_price', 0))
    
    for trade in active_trades:
        if trade['id'] == trade_id:
            trade['outcome'] = outcome
            if exit_price > 0:
                if trade['dir'] == 'BUY':
                    pnl = (exit_price - trade['entry']) * trade['quantity']
                else:
                    pnl = (trade['entry'] - exit_price) * trade['quantity']
                trade['pnl'] = round(pnl, 2)
                trade['status'] = 'CLOSED'
            
            save_json(trades_file, active_trades)
            
            # CLOSE LINKED OPTIONS IF FUTURES CLOSED
            if trade['status'] == 'CLOSED':
                close_linked_options(trade_id, reason=outcome)
            
            socketio.emit('outcome_updated', trade)
            break
    
    return jsonify({'success': True})

@app.route('/delete-trade/<trade_id>', methods=['DELETE'])
def delete_trade(trade_id):
    global active_trades
    active_trades = [t for t in active_trades if t['id'] != trade_id]
    save_json(trades_file, active_trades)
    socketio.emit('trade_deleted', {'id': trade_id})
    return jsonify({'success': True})

@app.route('/clear-history', methods=['POST'])
def clear_history():
    global active_trades
    active_trades = []
    save_json(trades_file, [])
    socketio.emit('history_cleared')
    return jsonify({'success': True})

@app.route('/api/signals')
def get_signals():
    return jsonify(active_trades)

@app.route('/api/options-signals')
def get_options_signals():
    return jsonify(active_options_trades)

@socketio.on('connect')
def handle_connect():
    emit('initial_data', {'signals': active_trades, 'options': active_options_trades})

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
