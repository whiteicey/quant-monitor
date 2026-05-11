"""
A股多空信号监控系统 - Web GUI v2
功能: 股票搜索 / 日历选日期 / 参数可调 / 多周期切换
"""
import sys, os, webbrowser, threading, json, math
from datetime import datetime, timedelta
from dataclasses import asdict

from flask import Flask, request, jsonify

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.data import fetch_stock_daily, fetch_stock_info, search_stock, fetch_realtime_quote, fetch_realtime_quotes_batch, merge_realtime_bar
from src.signals import compute_signals, get_latest_signal, SignalParams
from src.backtest import backtest, get_strategy_list, get_preset_list, backtest_compare

app = Flask(__name__)

import time as _time

# Watchlist JSON file path (next to the exe/script)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# For PyInstaller, use the directory where the exe is located
if getattr(sys, 'frozen', False):
    _BASE_DIR = os.path.dirname(sys.executable)
WATCHLIST_FILE = os.path.join(_BASE_DIR, "watchlist.json")


def _load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_watchlist(data):
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# Signal cache: {symbol: {"signal": SignalResult dict, "timestamp": float}}
_signal_cache = {}
_SIGNAL_CACHE_TTL = 60  # seconds


# 让Flask的JSON序列化把NaN/Inf替换为null
import json as _json

class _SafeEncoder(_json.JSONEncoder):
    def default(self, o):
        return super().default(o)

    def encode(self, o):
        text = super().encode(o)
        text = text.replace('NaN', 'null').replace('Infinity', 'null').replace('-Infinity', 'null')
        return text

    def iterencode(self, o, _one_shot=False):
        for chunk in super().iterencode(o, _one_shot):
            chunk = chunk.replace('NaN', 'null').replace('Infinity', 'null').replace('-Infinity', 'null')
            yield chunk

app.json_encoder = _SafeEncoder


def _sanitize(val):
    if val is None:
        return None
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    return val


# ---------------------------------------------------------------------------
# Shared parameter parsing helpers
# ---------------------------------------------------------------------------

def _parse_signal_params():
    """Parse SignalParams from request.args (shared by all analysis routes)"""
    p = SignalParams()
    for k in ["fast_length", "slow_length", "signal_length", "rsi_length", "bb_length", "volume_length", "atr_length"]:
        v = request.args.get(k)
        if v:
            try: setattr(p, k, int(v))
            except: pass
    v = request.args.get("bb_mult")
    if v:
        try: p.bb_mult = float(v)
        except: pass
    v = request.args.get("price_mode")
    if v:
        p.price_mode = v
    return p

def _parse_stop_config():
    """Parse StopConfig from request.args"""
    try:
        _sl = float(request.args.get("stop_loss_pct", "0"))
        _tp = float(request.args.get("take_profit_pct", "0"))
        _ts = float(request.args.get("trailing_stop_pct", "0"))
        _atrsl = float(request.args.get("atr_stop_mult", "0"))
        if any(x > 0 for x in [_sl, _tp, _ts, _atrsl]):
            from src.extensions import StopConfig
            return StopConfig(stop_loss_pct=_sl, take_profit_pct=_tp,
                              trailing_stop_pct=_ts, atr_stop_mult=_atrsl)
    except (ValueError, TypeError):
        pass
    return None

def _parse_position_config():
    """Parse PositionConfig from request.args"""
    pos_mode = request.args.get("position_mode", "full").strip()
    try:
        pos_pct = float(request.args.get("position_pct", "1.0"))
    except (ValueError, TypeError):
        pos_pct = 1.0
    if pos_mode != "full":
        from src.extensions import PositionConfig
        return PositionConfig(mode=pos_mode, position_pct=pos_pct)
    return None

def _parse_capital_params():
    """Parse capital/commission/stamp_tax from request.args"""
    try:
        initial_capital = float(request.args.get("initial_capital", "1000000"))
        commission = float(request.args.get("commission", "0.001"))
        stamp_tax = float(request.args.get("stamp_tax", "0.001"))
    except (ValueError, TypeError):
        initial_capital, commission, stamp_tax = 1000000.0, 0.001, 0.001
    return initial_capital, commission, stamp_tax


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/api/search")
def api_search():
    kw = request.args.get("keyword", "").strip()
    if not kw:
        return jsonify([])
    return jsonify(search_stock(kw, limit=15))


@app.route("/api/realtime")
def api_realtime():
    """实时行情 + 基于最新价格重算信号"""
    symbol = request.args.get("symbol", "688110").strip()
    start = request.args.get("start", "20240101").strip()
    end = request.args.get("end", "").strip()
    period = request.args.get("period", "daily").strip()

    p = _parse_signal_params()

    try:
        quote = fetch_realtime_quote(symbol)
    except Exception as e:
        return jsonify({"error": f"实时行情获取失败: {e}"}), 400

    # 获取历史数据并用实时价格更新最后一根K线
    try:
        df = fetch_stock_daily(symbol, start, end, period=period)
    except Exception as e:
        return jsonify({"error": f"数据获取失败: {e}"}), 400

    df = merge_realtime_bar(df, quote)

    # 重算信号
    try:
        sig_df = compute_signals(df, p)
        signal = get_latest_signal(sig_df)
    except Exception as e:
        return jsonify({"error": f"信号计算失败: {e}"}), 500

    # 构建最后一根K线的图表数据
    last_row = sig_df.iloc[-1]
    ts = int(sig_df.index[-1].timestamp())
    o, h, l, c = float(last_row["open"]), float(last_row["high"]), float(last_row["low"]), float(last_row["close"])

    def _v(col):
        val = last_row.get(col)
        if val is not None and not (isinstance(val, float) and math.isnan(val)):
            return round(float(val), 4)
        return None

    last_bar = {
        "candle": {"time": ts, "open": o, "high": h, "low": l, "close": c},
        "volume": {"time": ts, "value": float(last_row["volume"]),
                   "color": "rgba(239,83,80,0.7)" if c >= o else "rgba(38,166,154,0.7)"},
        "fast_ema": {"time": ts, "value": _v("fast_ema")},
        "slow_ema": {"time": ts, "value": _v("slow_ema")},
        "bb_upper": {"time": ts, "value": _v("bb_upper")},
        "bb_basis": {"time": ts, "value": _v("bb_basis")},
        "bb_lower": {"time": ts, "value": _v("bb_lower")},
        "macd_line": {"time": ts, "value": _v("macd_line")},
        "signal_line": {"time": ts, "value": _v("signal_line")},
        "macd_hist": {"time": ts, "value": _v("macd_hist")},
        "rsi": {"time": ts, "value": _v("rsi")},
        "kdj_k": {"time": ts, "value": _v("kdj_k")},
        "kdj_d": {"time": ts, "value": _v("kdj_d")},
        "kdj_j": {"time": ts, "value": _v("kdj_j")},
        "obv": {"time": ts, "value": _v("obv")},
        "obv_ma": {"time": ts, "value": _v("obv_ma")},
        "vwap": {"time": ts, "value": _v("vwap")},
    }

    return jsonify({
        "quote": quote,
        "signal": asdict(signal),
        "last_bar": last_bar,
    })


@app.route("/api/analyze")
def api_analyze():
    symbol = request.args.get("symbol", "688110").strip()
    start = request.args.get("start", "20240101").strip()
    end = request.args.get("end", "").strip()
    period = request.args.get("period", "daily").strip()

    # 信号参数
    p = _parse_signal_params()

    try:
        name = fetch_stock_info(symbol)
    except:
        name = symbol

    try:
        df = fetch_stock_daily(symbol, start, end, period=period)
    except Exception as e:
        return jsonify({"error": f"数据获取失败: {e}"}), 400

    # 合并实时行情到最后一根K线（和TradingView barstate.islast保持一致）
    try:
        quote = fetch_realtime_quote(symbol)
        df = merge_realtime_bar(df, quote)
    except Exception:
        pass  # 实时行情获取失败不影响分析，用历史数据继续

    try:
        sig_df = compute_signals(df, p)
        signal = get_latest_signal(sig_df)
    except Exception as e:
        return jsonify({"error": f"信号计算失败: {e}"}), 500

    candles, volumes = [], []
    fast_ema, slow_ema, bb_upper, bb_basis, bb_lower = [], [], [], [], []
    macd_line, signal_line, macd_hist, rsi_arr = [], [], [], []
    sbuy_m, ssell_m, wbuy_m, wsell_m = [], [], [], []
    kdj_k_arr, kdj_d_arr, kdj_j_arr, obv_arr, obv_ma_arr, vwap_arr = [], [], [], [], [], []

    for i in range(len(sig_df)):
        row = sig_df.iloc[i]
        ts = int(sig_df.index[i].timestamp())
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        candles.append({"time": ts, "open": o, "high": h, "low": l, "close": c})
        vc = "rgba(239,83,80,0.7)" if c >= o else "rgba(38,166,154,0.7)"
        volumes.append({"time": ts, "value": float(row["volume"]), "color": vc})

        def _v(col):
            v = row.get(col)
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                return round(float(v), 4)
            return None

        for arr, col in [(fast_ema,"fast_ema"),(slow_ema,"slow_ema"),(bb_upper,"bb_upper"),(bb_basis,"bb_basis"),(bb_lower,"bb_lower")]:
            val = _v(col)
            if val is not None:
                arr.append({"time": ts, "value": val})

        for arr, col in [(macd_line,"macd_line"),(signal_line,"signal_line")]:
            val = _v(col)
            if val is not None:
                arr.append({"time": ts, "value": val})
        mh = _v("macd_hist")
        if mh is not None:
            macd_hist.append({"time": ts, "value": mh, "color": "rgba(239,83,80,0.8)" if mh >= 0 else "rgba(38,166,154,0.8)"})
        rv = _v("rsi")
        if rv is not None:
            rsi_arr.append({"time": ts, "value": rv})

        for arr, col in [(kdj_k_arr,"kdj_k"),(kdj_d_arr,"kdj_d"),(kdj_j_arr,"kdj_j"),(obv_arr,"obv"),(obv_ma_arr,"obv_ma"),(vwap_arr,"vwap")]:
            val = _v(col)
            if val is not None:
                arr.append({"time": ts, "value": val})

        if row.get("strong_buy", False) == True:
            sbuy_m.append({"time": ts, "position": "belowBar", "color": "#FFD700", "shape": "arrowUp", "text": "强买"})
        if row.get("strong_sell", False) == True:
            ssell_m.append({"time": ts, "position": "aboveBar", "color": "#FF4444", "shape": "arrowDown", "text": "强卖"})
        if row.get("weak_buy", False) == True and not row.get("strong_buy", False) == True:
            wbuy_m.append({"time": ts, "position": "belowBar", "color": "#ef5350", "shape": "circle", "text": "买"})
        if row.get("weak_sell", False) == True and not row.get("strong_sell", False) == True:
            wsell_m.append({"time": ts, "position": "aboveBar", "color": "#26a69a", "shape": "circle", "text": "卖"})

    return jsonify({
        "name": name, "symbol": symbol, "period": period,
        "signal": asdict(signal),
        "chart": {
            "candles": candles, "volumes": volumes,
            "fast_ema": fast_ema, "slow_ema": slow_ema,
            "bb_upper": bb_upper, "bb_basis": bb_basis, "bb_lower": bb_lower,
            "macd_line": macd_line, "signal_line": signal_line, "macd_hist": macd_hist,
            "rsi": rsi_arr,
            "kdj_k": kdj_k_arr, "kdj_d": kdj_d_arr, "kdj_j": kdj_j_arr,
            "obv": obv_arr, "obv_ma": obv_ma_arr, "vwap": vwap_arr,
            "strong_buy_markers": sbuy_m, "strong_sell_markers": ssell_m,
            "weak_buy_markers": wbuy_m, "weak_sell_markers": wsell_m,
        },
    })


@app.route("/api/strategies")
def api_strategies():
    return jsonify(get_strategy_list())


@app.route("/api/presets")
def api_presets():
    return jsonify(get_preset_list())


@app.route("/api/mtf")
def api_mtf():
    symbol = request.args.get("symbol", "688110").strip()
    start = request.args.get("start", "20240101").strip()
    end = request.args.get("end", "").strip()
    try:
        from src.extensions import MTFConfig, compute_mtf_signals
        config = MTFConfig(enabled=True, periods=["daily", "weekly"])
        result = compute_mtf_signals(symbol, start, end, config)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sector")
def api_sector():
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "缺少股票代码"}), 400
    try:
        from src.data import fetch_sector_info, fetch_sector_ranking
        info = fetch_sector_info(symbol)
        ranking = fetch_sector_ranking(5)
        return jsonify({
            "industry": info.get("industry", ""),
            "region": info.get("region", ""),
            "concepts": info.get("concepts", []),
            "strongest": ranking.get("strongest", []),
            "weakest": ranking.get("weakest", []),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/watchlist")
def api_watchlist_get():
    return jsonify(_load_watchlist())


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    data = request.get_json(force=True)
    symbol = data.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "缺少股票代码"}), 400
    
    # Get stock name
    name = data.get("name", "")
    if not name:
        try:
            name = fetch_stock_info(symbol)
        except Exception:
            name = symbol
    
    wl = _load_watchlist()
    # Check duplicate
    if any(item["symbol"] == symbol for item in wl):
        return jsonify({"error": "已在自选股中"}), 400
    
    wl.append({"symbol": symbol, "name": name})
    _save_watchlist(wl)
    return jsonify({"ok": True, "symbol": symbol, "name": name})


@app.route("/api/watchlist/<symbol>", methods=["DELETE"])
def api_watchlist_delete(symbol):
    wl = _load_watchlist()
    wl = [item for item in wl if item["symbol"] != symbol]
    _save_watchlist(wl)
    # Clear cache
    _signal_cache.pop(symbol, None)
    return jsonify({"ok": True})


@app.route("/api/watchlist/realtime")
def api_watchlist_realtime():
    """批量实时行情 + 带缓存的信号摘要"""
    wl = _load_watchlist()
    if not wl:
        return jsonify([])
    
    symbols = [item["symbol"] for item in wl]
    name_map = {item["symbol"]: item["name"] for item in wl}
    
    # Batch realtime quotes (1 HTTP request)
    try:
        quotes = fetch_realtime_quotes_batch(symbols)
    except Exception as e:
        return jsonify({"error": f"行情获取失败: {e}"}), 400
    
    # Signal calculation with cache
    now = _time.time()
    results = []
    for sym in symbols:
        q = quotes.get(sym)
        if not q:
            continue
        
        # Use cached signal or recalculate
        cached = _signal_cache.get(sym)
        signal_data = None
        if cached and (now - cached["timestamp"]) < _SIGNAL_CACHE_TTL:
            signal_data = cached["signal"]
        else:
            try:
                df = fetch_stock_daily(sym, "20240101", "")
                # Merge realtime price
                df = merge_realtime_bar(df, q)
                sig_df = compute_signals(df)
                sig = get_latest_signal(sig_df)
                signal_data = asdict(sig)
                _signal_cache[sym] = {"signal": signal_data, "timestamp": now}
            except Exception:
                signal_data = None
        
        chg = q["price"] - q["yesterday_close"] if q["yesterday_close"] else 0
        chg_pct = (chg / q["yesterday_close"] * 100) if q["yesterday_close"] else 0
        
        item = {
            "symbol": sym,
            "name": q.get("name") or name_map.get(sym, sym),
            "price": q["price"],
            "change": round(chg, 2),
            "change_pct": round(chg_pct, 2),
            "high": q["high"],
            "low": q["low"],
            "volume": q["volume"],
            "date": q.get("date", ""),
            "time": q.get("time", ""),
        }
        if signal_data:
            item["bull_score"] = signal_data.get("bullish_signals", 0)
            item["bear_score"] = signal_data.get("bearish_signals", 0)
            item["trend"] = signal_data.get("trend", "震荡")
            # Determine signal text
            if signal_data.get("strong_buy"):
                item["signal"] = "强烈买入"
            elif signal_data.get("strong_sell"):
                item["signal"] = "强烈卖出"
            elif signal_data.get("weak_buy"):
                item["signal"] = "买入"
            elif signal_data.get("weak_sell"):
                item["signal"] = "卖出"
            else:
                item["signal"] = "观望"
        else:
            item["bull_score"] = 0
            item["bear_score"] = 0
            item["trend"] = "--"
            item["signal"] = "--"
        
        results.append(item)
    
    return jsonify(results)


@app.route("/api/backtest")
def api_backtest():
    symbol = request.args.get("symbol", "688110").strip()
    start = request.args.get("start", "20240101").strip()
    end = request.args.get("end", "").strip()
    strategy = request.args.get("strategy", "macd_rsi").strip()

    p = _parse_signal_params()
    initial_capital, commission, stamp_tax = _parse_capital_params()
    stop_config = _parse_stop_config()
    position_config = _parse_position_config()

    try:
        df = fetch_stock_daily(symbol, start, end)
    except Exception as e:
        return jsonify({"error": f"数据获取失败: {e}"}), 400

    weekly_signals_df = None
    if strategy == "mtf_confirm":
        try:
            weekly_df = fetch_stock_daily(symbol, start, end, period="weekly")
            weekly_signals_df = compute_signals(weekly_df, p)
        except Exception:
            pass

    try:
        result = backtest(df, params=p, initial_capital=initial_capital,
                          commission=commission, stamp_tax=stamp_tax,
                          strategy=strategy, stop_config=stop_config,
                          position_config=position_config,
                          weekly_signals_df=weekly_signals_df)
    except Exception as e:
        return jsonify({"error": f"回测失败: {e}"}), 500

    trades_list = []
    if not result.trades.empty:
        for _, r in result.trades.iterrows():
            trades_list.append({
                "buy_date": str(r.get("buy_date", "")),
                "sell_date": str(r["sell_date"].date()) if hasattr(r.get("sell_date"), "date") else str(r.get("sell_date", "")),
                "entry_price": _sanitize(r["entry_price"]),
                "exit_price": _sanitize(r["exit_price"]),
                "shares": int(r["shares"]),
                "pnl": _sanitize(r["pnl"]),
                "return_pct": _sanitize(r["return_pct"]),
                "exit_type": r.get("exit_type", "signal"),
            })

    return jsonify({
        "strategy_name": result.strategy_name,
        "total_return": result.total_return, "annual_return": result.annual_return,
        "max_drawdown": result.max_drawdown, "win_rate": result.win_rate,
        "total_trades": result.total_trades, "profit_trades": result.profit_trades,
        "loss_trades": result.loss_trades, "sharpe_ratio": result.sharpe_ratio,
        "trades": trades_list,
        "equity_curve": result.equity_curve or [],
    })


@app.route("/api/backtest/compare")
def api_backtest_compare():
    symbol = request.args.get("symbol", "688110").strip()
    start = request.args.get("start", "20240101").strip()
    end = request.args.get("end", "").strip()
    strategies_str = request.args.get("strategies", "").strip()
    
    strategy_ids = [s.strip() for s in strategies_str.split(",") if s.strip()] if strategies_str else None

    p = _parse_signal_params()
    initial_capital, commission, stamp_tax = _parse_capital_params()
    stop_config = _parse_stop_config()
    position_config = _parse_position_config()

    try:
        df = fetch_stock_daily(symbol, start, end)
    except Exception as e:
        return jsonify({"error": f"数据获取失败: {e}"}), 400

    weekly_signals_df = None
    if strategy_ids is None or "mtf_confirm" in (strategy_ids or []):
        try:
            weekly_df = fetch_stock_daily(symbol, start, end, period="weekly")
            weekly_signals_df = compute_signals(weekly_df, p)
        except Exception:
            pass

    try:
        results = backtest_compare(df, params=p, initial_capital=initial_capital,
                                   commission=commission, stamp_tax=stamp_tax,
                                   strategy_ids=strategy_ids, stop_config=stop_config,
                                   position_config=position_config,
                                   weekly_signals_df=weekly_signals_df)
    except Exception as e:
        return jsonify({"error": f"策略对比失败: {e}"}), 500

    return jsonify(results)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="A股监控">
<meta name="theme-color" content="#080c14">
<link rel="manifest" href="/manifest.json">
<title>A股多空信号监控系统</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
  :root, [data-theme="dark"] {
    --bg:#080c14; --bg2:#0f1520; --bg3:#161d2e; --bg4:#1c2538;
    --border:#253044; --border2:#2f3d55;
    --text:#dce4f0; --text-dim:#7a8ba4; --text-xs:#5a6a80;
    --accent:#4f8ff7; --accent2:#6ba3ff;
    --green:#ff4757; --green-dim:rgba(255,71,87,0.15);
    --red:#00d98b; --red-dim:rgba(0,217,139,0.15);
    --gold:#ffb347; --gold-dim:rgba(255,179,71,0.12);
    --purple:#a78bfa;
    --chart-bg:#080c14; --chart-grid:rgba(37,48,68,0.5); --chart-text:#7a8ba4;
  }
  [data-theme="light"] {
    --bg:#f5f5f7; --bg2:#ffffff; --bg3:#e8eaed; --bg4:#dcdfe3;
    --border:#d0d4db; --border2:#b8bcc4;
    --text:#1a1a2e; --text-dim:#5a5a7a; --text-xs:#8a8aa0;
    --accent:#3b7dd8; --accent2:#2b6cc4;
    --green:#e63946; --green-dim:rgba(230,57,70,0.12);
    --red:#2d9d6c; --red-dim:rgba(45,157,108,0.12);
    --gold:#d4880f; --gold-dim:rgba(212,136,15,0.1);
    --purple:#7c5cbf;
    --chart-bg:#ffffff; --chart-grid:rgba(200,200,210,0.5); --chart-text:#5a5a7a;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:'Noto Sans SC','Microsoft YaHei',sans-serif; background:var(--bg); color:var(--text); font-size:14px; overflow-x:hidden; }
  input,select,button { font-family:inherit; }

  /* ---- Top Bar ---- */
  .topbar { display:flex; align-items:center; gap:10px; padding:10px 16px; background:var(--bg2); border-bottom:1px solid var(--border); flex-wrap:wrap; position:relative; z-index:100; }
  .brand { font-size:17px; font-weight:700; color:var(--accent); white-space:nowrap; letter-spacing:0.5px; }
  .stock-name { font-size:18px; font-weight:700; margin-left:4px; color:var(--gold); }

  /* Search box */
  .search-wrap { position:relative; }
  .search-wrap input { width:200px; background:var(--bg3); border:1px solid var(--border); color:var(--text); padding:7px 12px; border-radius:6px; font-size:14px; outline:none; }
  .search-wrap input:focus { border-color:var(--accent); }
  .search-dropdown { position:absolute; top:100%; left:0; width:320px; max-height:360px; overflow-y:auto; background:var(--bg3); border:1px solid var(--border2); border-radius:8px; display:none; z-index:200; box-shadow:0 8px 32px rgba(0,0,0,0.5); }
  .search-dropdown.show { display:block; }
  .search-item { padding:8px 12px; cursor:pointer; display:flex; justify-content:space-between; border-bottom:1px solid var(--border); font-size:14px; }
  .search-item:hover { background:var(--bg4); }
  .search-item .code { color:var(--accent); font-family:'JetBrains Mono',monospace; font-weight:600; }
  .search-item .name { color:var(--text); }
  .search-item .mkt { color:var(--text-dim); font-size:12px; }

  /* Date pickers */
  .date-group { display:flex; align-items:center; gap:4px; }
  .date-group label { color:var(--text-dim); font-size:12px; white-space:nowrap; }
  .date-group input[type="date"] { background:var(--bg3); border:1px solid var(--border); color:var(--text); padding:6px 8px; border-radius:6px; font-size:13px; outline:none; cursor:pointer; }
  .date-group input[type="date"]:focus { border-color:var(--accent); }
  [data-theme="dark"] .date-group input[type="date"] { color-scheme:dark; }
  [data-theme="light"] .date-group input[type="date"] { color-scheme:light; }

  /* Period tabs */
  .period-tabs { display:flex; gap:2px; background:var(--bg3); border-radius:6px; padding:2px; }
  .period-tab { padding:5px 10px; border-radius:4px; cursor:pointer; font-size:13px; color:var(--text-dim); transition:all .2s; border:none; background:transparent; font-weight:600; }
  .period-tab.active { background:var(--accent); color:#fff; }
  .period-tab:hover:not(.active) { color:var(--text); background:var(--bg4); }

  /* Buttons */
  .btn { padding:7px 18px; border:none; border-radius:6px; font-size:14px; font-weight:600; cursor:pointer; transition:all .15s; }
  .btn-primary { background:var(--accent); color:#fff; }
  .btn-primary:hover { background:#3a7ae0; }
  .btn-warn { background:var(--gold); color:#111; }
  .btn-warn:hover { background:#e09c30; }

  /* Alert banner */
  .alert-banner { padding:8px 20px; font-size:15px; font-weight:700; text-align:center; display:none; }
  .alert-buy { background:linear-gradient(90deg,#3b0a0a,#7f1d1d); color:#fca5a5; display:block; }
  .alert-sell { background:linear-gradient(90deg,#052e1e,#064e3b); color:#34d399; display:block; }

  /* Main layout */
  .main { display:flex; height:calc(100vh - 52px); overflow:hidden; }
  .chart-area { flex:1; min-width:0; overflow-y:auto; padding:6px 10px 10px; }
  .sidebar { width:340px; min-width:340px; background:var(--bg2); border-left:1px solid var(--border); overflow-y:auto; padding:10px; }

  /* Charts */
  .chart-box { width:100%; border-radius:6px; overflow:hidden; margin-bottom:4px; background:var(--bg); }
  #chart-main { height:400px; }
  #chart-macd { height:130px; }
  #chart-rsi { height:120px; }
  .chart-label { color:var(--text-dim); font-size:11px; padding:3px 8px; background:var(--bg2); letter-spacing:0.5px; }

  /* Sidebar cards */
  .card { background:var(--bg3); border:1px solid var(--border); border-radius:8px; padding:12px; margin-bottom:10px; }
  .card-title { font-size:13px; font-weight:700; color:var(--accent2); margin-bottom:8px; padding-bottom:5px; border-bottom:1px solid var(--border); display:flex; align-items:center; gap:6px; }
  .card-title .icon { font-size:15px; }
  .row { display:flex; justify-content:space-between; align-items:center; padding:3px 0; font-size:13px; }
  .row .lbl { color:var(--text-dim); }
  .row .val { font-weight:600; font-family:'JetBrains Mono',monospace; font-size:13px; }
  .val-green { color:var(--green) !important; }
  .val-red { color:var(--red) !important; }
  .val-gold { color:var(--gold) !important; }
  .val-purple { color:var(--purple) !important; }

  /* Score */
  .score-header { display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:4px; }
  .score-num { font-size:26px; font-weight:700; font-family:'JetBrains Mono',monospace; line-height:1; }
  .score-bar { height:8px; border-radius:4px; display:flex; overflow:hidden; background:var(--bg); gap:2px; }
  .score-fill-bull { background:var(--green); border-radius:4px; transition:width .5s; }
  .score-fill-bear { background:var(--red); border-radius:4px; transition:width .5s; }

  /* Params panel */
  .params-grid { display:grid; grid-template-columns:1fr 1fr; gap:6px; }
  .param-item { display:flex; flex-direction:column; gap:2px; }
  .param-item label { font-size:11px; color:var(--text-dim); }
  .param-item input, .param-item select { background:var(--bg); border:1px solid var(--border); color:var(--text); padding:5px 8px; border-radius:4px; font-size:13px; outline:none; font-family:'JetBrains Mono',monospace; width:100%; }
  .param-item input:focus { border-color:var(--accent); }

  /* Backtest panel */
  #backtest-panel { display:none; padding:8px 0; }
  .bt-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:6px; }
  .bt-cell { background:var(--bg3); border:1px solid var(--border); border-radius:6px; padding:10px; text-align:center; }
  .bt-cell .bt-val { font-size:18px; font-weight:700; font-family:'JetBrains Mono',monospace; }
  .bt-cell .bt-lbl { font-size:11px; color:var(--text-dim); margin-top:2px; }
  .bt-trades { margin-top:10px; width:100%; border-collapse:collapse; font-size:12px; }
  .bt-trades th { background:var(--bg3); color:var(--text-dim); padding:6px; text-align:center; border-bottom:1px solid var(--border); font-size:11px; }
  .bt-trades td { padding:5px 6px; text-align:center; border-bottom:1px solid rgba(255,255,255,.04); font-family:'JetBrains Mono',monospace; }

  /* Spinner */
  .spinner-overlay { position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,.6); display:none; z-index:999; justify-content:center; align-items:center; }
  .spinner-overlay.active { display:flex; }
  .spinner { width:44px; height:44px; border:3px solid var(--border); border-top-color:var(--accent); border-radius:50%; animation:spin .7s linear infinite; }
  @keyframes spin { to{transform:rotate(360deg)} }

  /* Collapse toggle */
  .collapse-btn { background:none; border:none; color:var(--text-dim); cursor:pointer; font-size:12px; float:right; }
  .collapse-btn:hover { color:var(--text); }
  .collapsible { overflow:hidden; transition:max-height .3s ease; }

  /* View tabs */
  .view-tabs { display:flex; gap:2px; background:var(--bg3); border-radius:6px; padding:2px; }
  .view-tab { padding:5px 14px; border-radius:4px; cursor:pointer; font-size:13px; color:var(--text-dim); transition:all .2s; border:none; background:transparent; font-weight:600; }
  .view-tab.active { background:var(--accent); color:#fff; }
  .view-tab:hover:not(.active) { color:var(--text); background:var(--bg4); }

  /* Watchlist view */
  #view-watchlist { display:block; padding:10px; width:100%; overflow-y:auto; }
  #view-detail { display:none; }
  .wl-header { display:flex; align-items:center; gap:10px; margin-bottom:12px; flex-wrap:wrap; }
  .wl-header h2 { font-size:16px; color:var(--accent2); font-weight:700; }
  .wl-add-wrap { position:relative; }
  .wl-add-wrap input { width:200px; background:var(--bg3); border:1px solid var(--border); color:var(--text); padding:7px 12px; border-radius:6px; font-size:13px; outline:none; }
  .wl-add-wrap input:focus { border-color:var(--accent); }
  .wl-add-dropdown { position:absolute; top:100%; left:0; width:320px; max-height:300px; overflow-y:auto; background:var(--bg3); border:1px solid var(--border2); border-radius:8px; display:none; z-index:200; box-shadow:0 8px 32px rgba(0,0,0,.5); }
  .wl-add-dropdown.show { display:block; }
  .wl-add-item { padding:8px 12px; cursor:pointer; display:flex; justify-content:space-between; border-bottom:1px solid var(--border); font-size:13px; }
  .wl-add-item:hover { background:var(--bg4); }
  .wl-status { font-size:12px; color:var(--text-dim); margin-left:auto; }

  .wl-table { width:100%; border-collapse:collapse; }
  .wl-table th { background:var(--bg3); color:var(--text-dim); padding:8px 10px; text-align:center; font-size:12px; font-weight:600; border-bottom:1px solid var(--border); position:sticky; top:0; z-index:1; }
  .wl-table td { padding:8px 10px; text-align:center; border-bottom:1px solid rgba(255,255,255,.04); font-size:13px; cursor:pointer; transition:background .15s; }
  .wl-table tr:hover td { background:var(--bg4); }
  .wl-table .td-name { text-align:left; font-weight:600; }
  .wl-table .td-code { text-align:left; color:var(--text-dim); font-family:'JetBrains Mono',monospace; font-size:12px; }
  .wl-table .td-price { font-family:'JetBrains Mono',monospace; font-weight:700; font-size:15px; }
  .wl-table .td-change { font-family:'JetBrains Mono',monospace; font-weight:600; }
  .wl-table .td-score { font-family:'JetBrains Mono',monospace; }
  .wl-table .td-signal { font-weight:700; font-size:12px; padding:2px 8px; border-radius:4px; display:inline-block; }
  .wl-del-btn { background:none; border:1px solid var(--border); color:var(--text-dim); border-radius:4px; padding:2px 8px; cursor:pointer; font-size:11px; }
  .wl-del-btn:hover { border-color:var(--red); color:var(--red); }

  .wl-empty { text-align:center; padding:60px 20px; color:var(--text-dim); }
  .wl-empty p { font-size:15px; margin-bottom:8px; }
  .wl-empty span { font-size:12px; }

  /* Equity chart */
  #equity-box { display:none; margin:12px 0; }
  #chart-equity { height:250px; }

  /* Compare modal */
  .modal-overlay { position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,.7); display:none; z-index:1000; justify-content:center; align-items:center; }
  .modal-overlay.show { display:flex; }
  .modal-box { background:var(--bg2); border:1px solid var(--border2); border-radius:12px; padding:20px; width:480px; max-width:90vw; max-height:80vh; overflow-y:auto; }
  .modal-title { font-size:16px; font-weight:700; color:var(--accent2); margin-bottom:14px; }
  .modal-actions { display:flex; gap:8px; margin-bottom:12px; }
  .modal-actions button { padding:4px 12px; border:1px solid var(--border); background:var(--bg3); color:var(--text-dim); border-radius:4px; cursor:pointer; font-size:12px; }
  .modal-actions button:hover { color:var(--text); border-color:var(--accent); }
  .modal-strategies { display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-bottom:16px; }
  .modal-strat-item { display:flex; align-items:center; gap:8px; padding:6px 8px; border-radius:6px; background:var(--bg3); cursor:pointer; font-size:13px; }
  .modal-strat-item:hover { background:var(--bg4); }
  .modal-strat-item input[type="checkbox"] { accent-color:var(--accent); width:16px; height:16px; }
  .modal-strat-item label { cursor:pointer; flex:1; }
  .modal-btns { display:flex; gap:10px; justify-content:flex-end; }
  .modal-btns button { padding:8px 20px; border:none; border-radius:6px; font-size:14px; font-weight:600; cursor:pointer; }

  /* Compare results */
  #compare-box { display:none; margin:12px 0; }
  #chart-compare { height:300px; }
  .compare-table { width:100%; border-collapse:collapse; margin-top:10px; font-size:12px; }
  .compare-table th { background:var(--bg3); color:var(--text-dim); padding:7px 8px; text-align:center; border-bottom:1px solid var(--border); font-size:11px; }
  .compare-table td { padding:6px 8px; text-align:center; border-bottom:1px solid rgba(255,255,255,.04); font-family:'JetBrains Mono',monospace; }
  .compare-table tr:first-child td { color:var(--gold); font-weight:700; }
  .compare-legend { display:flex; flex-wrap:wrap; gap:8px; margin:8px 0; font-size:12px; }
  .compare-legend-item { display:flex; align-items:center; gap:4px; cursor:pointer; padding:2px 6px; border-radius:4px; }
  .compare-legend-item:hover { background:var(--bg4); }
  .compare-legend-dot { width:12px; height:4px; border-radius:2px; }

  /* Alert toast */
  .alert-toast-container { position:fixed; top:60px; right:16px; z-index:1001; display:flex; flex-direction:column; gap:8px; pointer-events:none; }
  .alert-toast { background:var(--bg2); border:1px solid var(--border2); border-radius:var(--radius-sm); padding:12px 16px; min-width:280px; box-shadow:0 4px 24px rgba(0,0,0,.5); animation:toastIn .3s ease; pointer-events:auto; display:flex; align-items:center; gap:10px; }
  .alert-toast.fade-out { animation:toastOut .3s ease forwards; }
  @keyframes toastIn { from{opacity:0;transform:translateX(100px)} to{opacity:1;transform:none} }
  @keyframes toastOut { from{opacity:1;transform:none} to{opacity:0;transform:translateX(100px)} }
  .alert-toast-icon { font-size:20px; }
  .alert-toast-body { flex:1; }
  .alert-toast-title { font-size:13px; font-weight:600; }
  .alert-toast-msg { font-size:12px; color:var(--text2); margin-top:2px; }
  .alert-toast-close { background:none; border:none; color:var(--text3); cursor:pointer; font-size:16px; padding:4px; }
  /* Alert toggle */
  .alert-toggle { display:flex; align-items:center; gap:8px; padding:8px 0; font-size:13px; }
  .alert-toggle input[type="checkbox"] { accent-color:var(--accent); width:16px; height:16px; }

  /* Drawing toolbar */
  .draw-toolbar { position:absolute; top:28px; left:8px; z-index:10; display:flex; gap:4px; }
  .draw-btn { background:var(--bg3); border:1px solid var(--border); color:var(--text-dim); padding:4px 8px; border-radius:4px; cursor:pointer; font-size:11px; transition:.2s; }
  .draw-btn:hover { border-color:var(--accent); color:var(--text); }
  .draw-btn.active { background:var(--accent); color:#fff; border-color:var(--accent); }

  @media(max-width:1000px){
    .main { flex-direction:column; height:auto; overflow:visible; }
    .sidebar { width:100%; min-width:0; border-left:none; border-top:1px solid var(--border); max-height:none; }
    .bt-grid { grid-template-columns:repeat(2,1fr); }
    .topbar { gap:6px; padding:8px 10px; }
    .search-wrap input { width:140px; font-size:13px; }
    .date-group input[type="date"] { width:110px; font-size:12px; padding:5px 4px; }
    .date-group label { font-size:11px; }
    .brand { font-size:14px; }
    .stock-name { font-size:15px; }
    .period-tabs { flex-wrap:wrap; }
    .period-tab { padding:4px 8px; font-size:12px; }
    .btn { padding:6px 12px; font-size:13px; }
    #chart-main { height:300px; }
    #chart-macd { height:110px; }
    #chart-rsi { height:100px; }
    .chart-area { padding:4px 6px 6px; }
    #chart-kdj { height:100px; }
    #chart-obv { height:100px; }
    #realtime-price { font-size:16px; }
    #realtime-change { font-size:12px; }
    .wl-table .td-price { font-size:14px; }
    .wl-table th, .wl-table td { padding:6px 6px; font-size:12px; }
    .modal-strategies { grid-template-columns:1fr; }
    .modal-box { width:95vw; }
    #chart-equity { height:200px; }
    #chart-compare { height:220px; }
  }
  @media(max-width:500px){
    .topbar { gap:4px; padding:6px 6px; }
    .search-wrap input { width:120px; }
    .date-group { flex-wrap:wrap; }
    .bt-grid { grid-template-columns:1fr 1fr; }
    .bt-cell .bt-val { font-size:15px; }
    .params-grid { grid-template-columns:1fr; }
    .wl-header { gap:6px; }
    #chart-kdj { height:80px; }
    #chart-obv { height:80px; }
    .wl-add-wrap input { width:150px; }
    .compare-table th, .compare-table td { padding:4px 4px; font-size:11px; }
    .draw-toolbar { position:static; margin:4px 8px; }
    .draw-btn { padding:3px 6px; font-size:10px; }
  }
</style>
</head>
<body>

<!-- Top Bar -->
<div class="topbar">
  <span class="brand">A股信号监控</span>
  <div class="view-tabs">
    <button class="view-tab active" data-view="watchlist" onclick="switchView('watchlist')">自选股</button>
    <button class="view-tab" data-view="detail" onclick="switchView('detail')">个股详情</button>
  </div>

  <div class="search-wrap">
    <input id="inp-search" placeholder="输入代码或名称搜索..." autocomplete="off">
    <div class="search-dropdown" id="search-dropdown"></div>
  </div>

  <div class="date-group">
    <label>开始</label>
    <input type="date" id="inp-start" value="2024-01-01">
    <label>结束</label>
    <input type="date" id="inp-end">
  </div>

  <div class="period-tabs" id="period-tabs">
    <button class="period-tab" data-p="5min">5分</button>
    <button class="period-tab" data-p="15min">15分</button>
    <button class="period-tab" data-p="30min">30分</button>
    <button class="period-tab" data-p="1h">1时</button>
    <button class="period-tab active" data-p="daily">日线</button>
    <button class="period-tab" data-p="weekly">周线</button>
    <button class="period-tab" data-p="monthly">月线</button>
  </div>

  <button class="btn btn-primary" onclick="doAnalyze()">分析</button>
  <button class="btn btn-warn" onclick="doBacktest()">回测</button>
  <button class="btn" style="background:var(--purple);color:#fff;" onclick="showCompareModal()">策略对比</button>

  <span class="stock-name" id="stock-name"></span>
  <span id="realtime-price" style="font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;margin-left:8px;"></span>
  <span id="realtime-change" style="font-family:'JetBrains Mono',monospace;font-size:14px;margin-left:4px;"></span>
  <span id="realtime-status" style="font-size:11px;color:var(--text-xs);margin-left:auto;white-space:nowrap;"></span>
  <label class="alert-toggle" style="margin-left:8px;"><input type="checkbox" id="alert-enabled" onchange="toggleAlerts()"> <span style="font-size:12px;color:var(--text-dim);">提醒</span></label>
  <label class="alert-toggle" style="margin-left:2px;"><input type="checkbox" id="alert-sound" checked onchange="toggleAlertSound()"> <span style="font-size:12px;color:var(--text-dim);">声音</span></label>
  <label class="alert-toggle" style="margin-left:2px;"><input type="checkbox" id="mtf-toggle" onchange="toggleMTF()"> <span style="font-size:12px;color:var(--text-dim);">多周期</span></label>
  <button class="btn" id="theme-toggle" onclick="toggleTheme()" style="padding:4px 10px;font-size:12px;background:var(--bg3);color:var(--text-dim);border:1px solid var(--border);border-radius:4px;cursor:pointer;margin-left:4px;">☀</button>
</div>

<div class="alert-banner" id="alert-banner"></div>

<div class="main">
  <!-- 自选股视图 -->
  <div id="view-watchlist" style="width:100%;overflow-y:auto;">
    <div class="wl-header">
      <h2>自选股</h2>
      <div class="wl-add-wrap">
        <input id="wl-add-input" placeholder="输入代码或名称添加..." autocomplete="off">
        <div class="wl-add-dropdown" id="wl-add-dropdown"></div>
      </div>
      <span class="wl-status" id="wl-status"></span>
    </div>
    <div id="wl-content">
      <div class="wl-empty" id="wl-empty">
        <p>还没有自选股</p>
        <span>在上方搜索框输入股票代码或名称添加</span>
      </div>
      <table class="wl-table" id="wl-table" style="display:none;">
        <thead>
          <tr>
            <th>股票</th>
            <th>最新价</th>
            <th>涨跌幅</th>
            <th>多头</th>
            <th>空头</th>
            <th>趋势</th>
            <th>信号</th>
            <th></th>
          </tr>
        </thead>
        <tbody id="wl-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- 个股详情视图 -->
  <div id="view-detail" style="display:none;flex:1;min-width:0;overflow:hidden;">
    <div style="display:flex;width:100%;height:100%;">
  <div class="chart-area">
    <div class="chart-box" style="position:relative;"><div class="chart-label">K线 / EMA / 布林带</div><div class="draw-toolbar"><button class="draw-btn" id="draw-hline" onclick="setDrawMode('hline')">— 水平线</button><button class="draw-btn" id="draw-trendline" onclick="setDrawMode('trendline')">╱ 趋势线</button><button class="draw-btn" onclick="clearDrawings()">✕ 清除</button></div><div id="chart-main"></div></div>
    <div class="chart-box"><div class="chart-label">MACD</div><div id="chart-macd"></div></div>
    <div class="chart-box"><div class="chart-label">RSI</div><div id="chart-rsi"></div></div>
    <div class="chart-box"><div class="chart-label">KDJ</div><div id="chart-kdj" style="height:120px;"></div></div>
    <div class="chart-box"><div class="chart-label">OBV</div><div id="chart-obv" style="height:120px;"></div></div>
    <div id="backtest-panel">
      <h3 style="color:var(--gold);margin-bottom:10px;font-size:15px;">回测结果 <span id="bt-strategy-label" style="color:var(--accent);font-size:13px;"></span></h3>
      <div class="bt-grid" id="bt-grid"></div>
      <div id="equity-box">
        <div class="chart-label" style="margin-top:8px;">权益曲线</div>
        <div id="chart-equity"></div>
      </div>
      <h4 style="margin:10px 0 4px;color:var(--text-dim);font-size:13px;">交易明细</h4>
      <div style="overflow-x:auto;"><table class="bt-trades" id="bt-trades"></table></div>
      <div id="compare-box">
        <h3 style="color:var(--purple);margin:12px 0 8px;font-size:15px;">策略对比</h3>
        <div class="compare-legend" id="compare-legend"></div>
        <div class="chart-box"><div id="chart-compare"></div></div>
        <table class="compare-table" id="compare-table"></table>
      </div>
    </div>
  </div>

  <div class="sidebar">
    <!-- 多空评分 -->
    <div class="card">
      <div class="card-title"><span class="icon">&#x2696;</span> 多空评分</div>
      <div class="score-header">
        <div><span style="color:var(--green);font-size:11px;">多头</span><br><span class="score-num val-green" id="bull-score">0</span><span style="color:var(--text-dim);font-size:12px;"> /6</span></div>
        <div style="text-align:right;"><span style="color:var(--red);font-size:11px;">空头</span><br><span class="score-num val-red" id="bear-score">0</span><span style="color:var(--text-dim);font-size:12px;"> /6</span></div>
      </div>
      <div class="score-bar"><div class="score-fill-bull" id="bull-bar" style="width:0%"></div><div style="flex:1"></div><div class="score-fill-bear" id="bear-bar" style="width:0%"></div></div>
    </div>

    <!-- 信号状态 -->
    <div class="card">
      <div class="card-title"><span class="icon">&#x26A1;</span> 信号状态</div>
      <div class="row"><span class="lbl">趋势方向</span><span class="val" id="sig-trend">--</span></div>
      <div class="row"><span class="lbl">强烈买入</span><span class="val" id="sig-sbuy">--</span></div>
      <div class="row"><span class="lbl">强烈卖出</span><span class="val" id="sig-ssell">--</span></div>
      <div class="row"><span class="lbl">普通买入</span><span class="val" id="sig-wbuy">--</span></div>
      <div class="row"><span class="lbl">普通卖出</span><span class="val" id="sig-wsell">--</span></div>
    </div>

    <!-- 技术指标 -->
    <div class="card">
      <div class="card-title"><span class="icon">&#x1F4CA;</span> 技术指标</div>
      <div class="row"><span class="lbl">RSI</span><span class="val" id="sig-rsi">--</span></div>
      <div class="row"><span class="lbl">MACD</span><span class="val" id="sig-macd">--</span></div>
      <div class="row"><span class="lbl">价格位置</span><span class="val" id="sig-pos">--</span></div>
    </div>

    <!-- 关键价位 -->
    <div class="card">
      <div class="card-title"><span class="icon">&#x1F3AF;</span> 关键价位</div>
      <div class="row"><span class="lbl">建议买入</span><span class="val val-green" id="sig-rbuy">--</span></div>
      <div class="row"><span class="lbl">建议卖出</span><span class="val val-red" id="sig-rsell">--</span></div>
      <div class="row"><span class="lbl">止损价</span><span class="val val-gold" id="sig-stop">--</span></div>
      <div class="row"><span class="lbl">支撑位</span><span class="val" id="sig-support">--</span></div>
      <div class="row"><span class="lbl">阻力位</span><span class="val" id="sig-resist">--</span></div>
    </div>

    <div class="card" id="mtf-card" style="display:none;">
      <div class="card-title"><span class="icon">&#x1F310;</span> 多周期共振</div>
      <div class="row"><span class="lbl">周线趋势</span><span class="val" id="mtf-wk-trend">--</span></div>
      <div class="row"><span class="lbl">周线多头</span><span class="val" id="mtf-wk-bull">--</span></div>
      <div class="row"><span class="lbl">周线空头</span><span class="val" id="mtf-wk-bear">--</span></div>
      <div class="row"><span class="lbl">共振确认</span><span class="val" id="mtf-confirm">--</span></div>
    </div>

    <div class="card" id="sector-card">
      <div class="card-title"><span class="icon">&#x1F3ED;</span> 板块联动</div>
      <div class="row"><span class="lbl">所属行业</span><span class="val" id="sector-industry">--</span></div>
      <div class="row"><span class="lbl">所属地域</span><span class="val" id="sector-region" style="font-size:12px;">--</span></div>
      <div class="row"><span class="lbl">概念板块</span><span class="val" id="sector-concepts" style="font-size:11px;word-break:break-all;">--</span></div>
      <div style="margin-top:10px;font-size:11px;color:var(--text-dim);border-top:1px solid var(--border);padding-top:8px;">今日板块强弱 TOP5</div>
      <div id="sector-strongest" style="margin-top:4px;"></div>
      <div style="margin-top:8px;font-size:11px;color:var(--text-dim);">最弱 TOP5</div>
      <div id="sector-weakest" style="margin-top:4px;"></div>
    </div>

    <!-- 信号参数 -->
    <div class="card">
      <div class="card-title"><span class="icon">&#x2699;</span> 指标参数 <button class="collapse-btn" onclick="toggleCollapse('params-signal')">[展开/收起]</button></div>
      <div class="collapsible" id="params-signal" style="max-height:600px;">
        <div class="param-item" style="margin-bottom:8px;">
          <label>参数预设</label>
          <select id="p-preset" style="width:100%;padding:7px 8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;font-size:13px;"></select>
          <div id="p-preset-desc" style="font-size:11px;color:var(--text-dim);margin-top:3px;padding:4px 6px;background:var(--bg);border-radius:4px;min-height:16px;"></div>
        </div>
        <div class="params-grid">
          <div class="param-item"><label>快速EMA</label><input type="number" id="p-fast" value="6" min="1" max="200"></div>
          <div class="param-item"><label>慢速EMA</label><input type="number" id="p-slow" value="7" min="1" max="200"></div>
          <div class="param-item"><label>信号线</label><input type="number" id="p-signal" value="4" min="1" max="100"></div>
          <div class="param-item"><label>RSI周期</label><input type="number" id="p-rsi" value="14" min="1" max="200"></div>
          <div class="param-item"><label>布林带周期</label><input type="number" id="p-bb" value="20" min="1" max="200"></div>
          <div class="param-item"><label>布林带倍数</label><input type="number" id="p-bbmult" value="2.0" step="0.1" min="0.1" max="5"></div>
          <div class="param-item"><label>成交量周期</label><input type="number" id="p-vol" value="5" min="1" max="100"></div>
          <div class="param-item"><label>ATR周期</label><input type="number" id="p-atr" value="14" min="1" max="200"></div>
        </div>
      </div>
    </div>

    <!-- 回测参数 -->
    <div class="card">
      <div class="card-title"><span class="icon">&#x1F4B0;</span> 回测参数 <button class="collapse-btn" onclick="toggleCollapse('params-bt')">[展开/收起]</button></div>
      <div class="collapsible" id="params-bt" style="max-height:600px;">
        <div class="param-item" style="margin-bottom:8px;">
          <label>信号策略</label>
          <select id="bt-strategy" style="width:100%;padding:7px 8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;font-size:13px;"></select>
          <div id="bt-strategy-desc" style="font-size:11px;color:var(--text-dim);margin-top:3px;padding:4px 6px;background:var(--bg);border-radius:4px;min-height:16px;"></div>
        </div>
        <div class="params-grid">
          <div class="param-item"><label>初始资金</label><input type="number" id="bt-capital" value="1000000" step="100000" min="10000"></div>
          <div class="param-item"><label>手续费率</label><input type="number" id="bt-commission" value="0.001" step="0.0001" min="0"></div>
          <div class="param-item"><label>印花税率</label><input type="number" id="bt-tax" value="0.001" step="0.0001" min="0"></div>
        </div>
        <div class="params-grid" style="margin-top:8px;">
          <div class="param-item"><label>止损(%)</label><input type="number" id="bt-sl" value="0" step="1" min="0" max="50"></div>
          <div class="param-item"><label>止盈(%)</label><input type="number" id="bt-tp" value="0" step="1" min="0" max="100"></div>
          <div class="param-item"><label>移动止损(%)</label><input type="number" id="bt-tsl" value="0" step="1" min="0" max="50"></div>
          <div class="param-item"><label>ATR止损(倍)</label><input type="number" id="bt-atrsl" value="0" step="0.5" min="0" max="10"></div>
        </div>
        <div class="params-grid" style="margin-top:8px;">
          <div class="param-item"><label>仓位模式</label>
            <select id="bt-pos-mode" style="width:100%;padding:5px 8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;font-size:13px;">
              <option value="full">全仓(100%)</option>
              <option value="fixed_pct">固定比例</option>
              <option value="kelly">凯利公式</option>
            </select>
          </div>
          <div class="param-item"><label>仓位比例(%)</label><input type="number" id="bt-pos-pct" value="100" step="5" min="5" max="100"></div>
        </div>
      </div>
    </div>
  </div>
    </div>
  </div>
</div>

<div class="modal-overlay" id="compare-modal">
  <div class="modal-box">
    <div class="modal-title">选择要对比的策略</div>
    <div class="modal-actions">
      <button onclick="compareSelectAll()">全选</button>
      <button onclick="compareClearAll()">清空</button>
    </div>
    <div class="modal-strategies" id="compare-strategies"></div>
    <div class="modal-btns">
      <button style="background:var(--bg3);color:var(--text-dim);" onclick="hideCompareModal()">取消</button>
      <button style="background:var(--purple);color:#fff;" onclick="doCompare()">开始对比</button>
    </div>
  </div>
</div>

<div class="alert-toast-container" id="alert-container"></div>
<div class="spinner-overlay" id="spinner"><div class="spinner"></div></div>

<script>
// ---- State ----
let currentSymbol = '688110';
let currentPeriod = 'daily';
let mainChart, macdChart, rsiChart;
let candleSeries, volSeries, fastEmaSeries, slowEmaSeries, bbUpperSeries, bbBasisSeries, bbLowerSeries;
let macdLineSeries, macdSignalSeries, macdHistSeries;
let rsiSeries, rsiUpper, rsiLower;
let chartsReady = false;
let searchTimeout = null;

let CHART_BG = '#080c14';
let GRID_COLOR = 'rgba(37,48,68,0.5)';
let TEXT_COLOR = '#7a8ba4';

let kdjChart, kdjKSeries, kdjDSeries, kdjJSeries, kdjUpper, kdjLower;
let obvChart, obvSeries, obvMaSeries;
let vwapSeries;

// ---- Collapse ----
function toggleCollapse(id) {
  const el = document.getElementById(id);
  el.style.maxHeight = el.style.maxHeight === '0px' ? '500px' : '0px';
}

// ---- Search ----
const searchInput = document.getElementById('inp-search');
const searchDropdown = document.getElementById('search-dropdown');
searchInput.value = '688110';

searchInput.addEventListener('input', () => {
  clearTimeout(searchTimeout);
  const kw = searchInput.value.trim();
  if (kw.length < 1) { searchDropdown.classList.remove('show'); return; }
  searchTimeout = setTimeout(async () => {
    try {
      const r = await fetch('/api/search?keyword=' + encodeURIComponent(kw));
      const data = await r.json();
      if (!data.length) { searchDropdown.classList.remove('show'); return; }
      searchDropdown.innerHTML = data.map(d =>
        `<div class="search-item" data-code="${d.code}">
          <span><span class="code">${d.code}</span> <span class="name">${d.name}</span></span>
          <span class="mkt">${d.market}</span>
        </div>`
      ).join('');
      searchDropdown.classList.add('show');
      searchDropdown.querySelectorAll('.search-item').forEach(el => {
        el.addEventListener('click', () => {
          currentSymbol = el.dataset.code;
          searchInput.value = el.querySelector('.code').textContent + ' ' + el.querySelector('.name').textContent;
          searchDropdown.classList.remove('show');
          doAnalyze();
        });
      });
    } catch(e) {}
  }, 300);
});

searchInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    searchDropdown.classList.remove('show');
    // 如果输入的是纯数字，直接当代码用
    const val = searchInput.value.trim();
    const codeMatch = val.match(/^(\d{6})/);
    if (codeMatch) currentSymbol = codeMatch[1];
    doAnalyze();
  }
});

document.addEventListener('click', (e) => {
  if (!e.target.closest('.search-wrap')) searchDropdown.classList.remove('show');
});

// ---- Period tabs ----
document.querySelectorAll('.period-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.period-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    currentPeriod = tab.dataset.p;
    doAnalyze();
  });
});

// ---- Date helpers ----
function getStartDate() {
  const v = document.getElementById('inp-start').value;
  return v ? v.replace(/-/g, '') : '20240101';
}
function getEndDate() {
  const v = document.getElementById('inp-end').value;
  return v ? v.replace(/-/g, '') : '';
}

// Set default end date to today
document.getElementById('inp-end').value = new Date().toISOString().split('T')[0];

// ---- Sector Analysis ----
async function fetchSector() {
  if (!currentSymbol) return;
  try {
    const resp = await fetch('/api/sector?symbol=' + currentSymbol);
    const data = await resp.json();
    if (data.error) return;
    
    document.getElementById('sector-industry').textContent = data.industry || '--';
    document.getElementById('sector-region').textContent = data.region || '--';
    document.getElementById('sector-concepts').textContent = (data.concepts || []).slice(0, 5).join(', ') || '--';
    
    const renderRank = (items, containerId) => {
      const el = document.getElementById(containerId);
      if (!items || !items.length) { el.innerHTML = '<div style="color:var(--text-xs);font-size:11px;">暂无数据</div>'; return; }
      el.innerHTML = items.map(item => {
        const cls = item.change_pct >= 0 ? 'val-green' : 'val-red';
        const sign = item.change_pct >= 0 ? '+' : '';
        return `<div class="row" style="font-size:12px;"><span class="lbl">${item.name}</span><span class="val ${cls}">${sign}${item.change_pct}%</span></div>`;
      }).join('');
    };
    renderRank(data.strongest, 'sector-strongest');
    renderRank(data.weakest, 'sector-weakest');
  } catch(e) {}
}

// ---- Drawing Tools ----
let drawMode = null; // null, 'hline', 'trendline'
let drawings = []; // [{type, data, refs}]
let trendlineFirstPoint = null;

function setDrawMode(mode) {
  if (drawMode === mode) { drawMode = null; } else { drawMode = mode; }
  document.querySelectorAll('.draw-btn').forEach(b => b.classList.remove('active'));
  if (drawMode) document.getElementById('draw-' + drawMode).classList.add('active');
  trendlineFirstPoint = null;
}

function initDrawingEvents() {
  if (!mainChart || !candleSeries) return;
  mainChart.subscribeClick(param => {
    if (!drawMode || !param.point || !param.time) return;
    const price = candleSeries.coordinateToPrice(param.point.y);
    if (isNaN(price)) return;
    
    if (drawMode === 'hline') {
      const line = candleSeries.createPriceLine({
        price: price,
        color: '#ffb347',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: price.toFixed(2),
      });
      drawings.push({ type: 'hline', price: price, ref: line });
      saveDrawings();
      setDrawMode(null);
    }
    else if (drawMode === 'trendline') {
      if (!trendlineFirstPoint) {
        trendlineFirstPoint = { time: param.time, price: price };
      } else {
        const series = mainChart.addLineSeries({
          color: '#e056fd',
          lineWidth: 1.5,
          lineStyle: 0,
          lastValueVisible: false,
          priceLineVisible: false,
        });
        series.setData([
          { time: trendlineFirstPoint.time, value: trendlineFirstPoint.price },
          { time: param.time, value: price },
        ]);
        drawings.push({
          type: 'trendline',
          points: [
            { time: trendlineFirstPoint.time, price: trendlineFirstPoint.price },
            { time: param.time, price: price }
          ],
          ref: series
        });
        trendlineFirstPoint = null;
        saveDrawings();
        setDrawMode(null);
      }
    }
  });
}

function clearDrawings(skipSave) {
  drawings.forEach(d => {
    if (d.type === 'hline' && d.ref) {
      try { candleSeries.removePriceLine(d.ref); } catch(e) {}
    }
    if (d.type === 'trendline' && d.ref) {
      try { mainChart.removeSeries(d.ref); } catch(e) {}
    }
  });
  drawings = [];
  if (!skipSave) saveDrawings();
  setDrawMode(null);
}

function saveDrawings() {
  const data = drawings.map(d => {
    if (d.type === 'hline') return { type: 'hline', price: d.price };
    if (d.type === 'trendline') return { type: 'trendline', points: d.points };
    return null;
  }).filter(Boolean);
  localStorage.setItem('drawings_' + currentSymbol, JSON.stringify(data));
}

function loadDrawings() {
  clearDrawings(true); // Clear previous symbol's drawings (without saving)
  const raw = localStorage.getItem('drawings_' + currentSymbol);
  if (!raw) return;
  try {
    const saved = JSON.parse(raw);
    saved.forEach(d => {
      if (d.type === 'hline') {
        const line = candleSeries.createPriceLine({
          price: d.price,
          color: '#ffb347',
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: d.price.toFixed(2),
        });
        drawings.push({ type: 'hline', price: d.price, ref: line });
      }
      if (d.type === 'trendline' && d.points && d.points.length === 2) {
        const series = mainChart.addLineSeries({
          color: '#e056fd',
          lineWidth: 1.5,
          lineStyle: 0,
          lastValueVisible: false,
          priceLineVisible: false,
        });
        series.setData([
          { time: d.points[0].time, value: d.points[0].price },
          { time: d.points[1].time, value: d.points[1].price },
        ]);
        drawings.push({ type: 'trendline', points: d.points, ref: series });
      }
    });
  } catch(e) {}
}

// ---- Params helper ----
let currentPriceMode = 'default';
function getSignalParams() {
  return `&fast_length=${g('p-fast')}&slow_length=${g('p-slow')}&signal_length=${g('p-signal')}&rsi_length=${g('p-rsi')}&bb_length=${g('p-bb')}&bb_mult=${g('p-bbmult')}&volume_length=${g('p-vol')}&atr_length=${g('p-atr')}&price_mode=${currentPriceMode}`;
}
function g(id) { return document.getElementById(id).value; }

// ---- Charts ----
function initCharts() {
  if (chartsReady) return;
  const commonOpts = {
    layout: { background:{color:CHART_BG}, textColor:TEXT_COLOR, fontFamily:"'Noto Sans SC','JetBrains Mono',sans-serif", fontSize:12 },
    grid: { vertLines:{color:GRID_COLOR}, horzLines:{color:GRID_COLOR} },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor:GRID_COLOR },
    timeScale: { borderColor:GRID_COLOR, timeVisible: true, secondsVisible: false },
  };

  mainChart = LightweightCharts.createChart(document.getElementById('chart-main'), { ...commonOpts, width: document.getElementById('chart-main').clientWidth, height: 400 });
  candleSeries = mainChart.addCandlestickSeries({ upColor:'#ef5350', downColor:'#26a69a', borderUpColor:'#ef5350', borderDownColor:'#26a69a', wickUpColor:'#ef5350', wickDownColor:'#26a69a' });
  volSeries = mainChart.addHistogramSeries({ priceFormat:{type:'volume'}, priceScaleId:'vol' });
  mainChart.priceScale('vol').applyOptions({ scaleMargins:{top:0.85,bottom:0} });
  fastEmaSeries = mainChart.addLineSeries({ color:'#ffb347', lineWidth:1, title:'EMA快' });
  slowEmaSeries = mainChart.addLineSeries({ color:'#a78bfa', lineWidth:1, title:'EMA慢' });
  bbUpperSeries = mainChart.addLineSeries({ color:'rgba(79,143,247,0.7)', lineWidth:1, lineStyle:2 });
  bbBasisSeries = mainChart.addLineSeries({ color:'rgba(79,143,247,0.85)', lineWidth:1 });
  bbLowerSeries = mainChart.addLineSeries({ color:'rgba(79,143,247,0.7)', lineWidth:1, lineStyle:2 });

  macdChart = LightweightCharts.createChart(document.getElementById('chart-macd'), { ...commonOpts, width: document.getElementById('chart-macd').clientWidth, height:130 });
  macdHistSeries = macdChart.addHistogramSeries({ priceFormat:{type:'price',precision:4,minMove:0.0001} });
  macdLineSeries = macdChart.addLineSeries({ color:'#4f8ff7', lineWidth:1.5, title:'MACD' });
  macdSignalSeries = macdChart.addLineSeries({ color:'#ffb347', lineWidth:1.5, title:'Signal' });

  rsiChart = LightweightCharts.createChart(document.getElementById('chart-rsi'), { ...commonOpts, width: document.getElementById('chart-rsi').clientWidth, height:120 });
  rsiSeries = rsiChart.addLineSeries({ color:'#a78bfa', lineWidth:1.5, title:'RSI' });
  rsiUpper = rsiChart.addLineSeries({ color:'rgba(255,71,87,0.6)', lineWidth:1, lineStyle:2 });
  rsiLower = rsiChart.addLineSeries({ color:'rgba(0,217,139,0.6)', lineWidth:1, lineStyle:2 });

  vwapSeries = mainChart.addLineSeries({ color:'#e056fd', lineWidth:1.5, title:'VWAP', lineStyle:0 });

  kdjChart = LightweightCharts.createChart(document.getElementById('chart-kdj'), { ...commonOpts, width: document.getElementById('chart-kdj').clientWidth, height:120 });
  kdjKSeries = kdjChart.addLineSeries({ color:'#f7dc6f', lineWidth:1.5, title:'K' });
  kdjDSeries = kdjChart.addLineSeries({ color:'#85c1e9', lineWidth:1.5, title:'D' });
  kdjJSeries = kdjChart.addLineSeries({ color:'#e056fd', lineWidth:1.5, title:'J' });
  kdjUpper = kdjChart.addLineSeries({ color:'rgba(255,71,87,0.5)', lineWidth:1, lineStyle:2 });
  kdjLower = kdjChart.addLineSeries({ color:'rgba(0,217,139,0.5)', lineWidth:1, lineStyle:2 });

  obvChart = LightweightCharts.createChart(document.getElementById('chart-obv'), { ...commonOpts, width: document.getElementById('chart-obv').clientWidth, height:120 });
  obvSeries = obvChart.addLineSeries({ color:'#36d7b7', lineWidth:1.5, title:'OBV' });
  obvMaSeries = obvChart.addLineSeries({ color:'#ffb347', lineWidth:1, title:'OBV MA20', lineStyle:2 });

  initDrawingEvents();
  chartsReady = true;
  syncTimeScales();
}

function syncTimeScales() {
  let _syncing = false;
  const allCharts = [mainChart, macdChart, rsiChart, kdjChart, obvChart];
  allCharts.forEach(src => {
    if (!src) return;
    src.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (_syncing || !range) return;
      _syncing = true;
      allCharts.forEach(t => { if (t && t !== src) try { t.timeScale().setVisibleLogicalRange(range); } catch(e){} });
      _syncing = false;
    });
  });
}

function resizeCharts() {
  if (!chartsReady) return;
  const w = document.getElementById('chart-main').clientWidth;
  const mh = document.getElementById('chart-main').clientHeight;
  const dh = document.getElementById('chart-macd').clientHeight;
  const rh = document.getElementById('chart-rsi').clientHeight;
  mainChart.applyOptions({width:w, height:mh}); macdChart.applyOptions({width:w, height:dh}); rsiChart.applyOptions({width:w, height:rh});
  const kh = document.getElementById('chart-kdj').clientHeight;
  const oh = document.getElementById('chart-obv').clientHeight;
  kdjChart.applyOptions({width:w, height:kh}); obvChart.applyOptions({width:w, height:oh});
}
window.addEventListener('resize', resizeCharts);

function showSpinner() { document.getElementById('spinner').classList.add('active'); }
function hideSpinner() { document.getElementById('spinner').classList.remove('active'); }

// ---- Signal Panel Update ----
function updateSignalPanel(s) {
  document.getElementById('bull-score').textContent = s.bullish_signals;
  document.getElementById('bear-score').textContent = s.bearish_signals;
  document.getElementById('bull-bar').style.width = (s.bullish_signals/6*50)+'%';
  document.getElementById('bear-bar').style.width = (s.bearish_signals/6*50)+'%';
  const trendEl = document.getElementById('sig-trend');
  trendEl.textContent = s.trend;
  trendEl.className = 'val ' + (s.trend==='上涨'?'val-green':s.trend==='下跌'?'val-red':'val-gold');
  document.getElementById('sig-sbuy').innerHTML = s.strong_buy ? '<span class="val-green">YES</span>' : '<span style="color:var(--text-xs)">—</span>';
  document.getElementById('sig-ssell').innerHTML = s.strong_sell ? '<span class="val-red">YES</span>' : '<span style="color:var(--text-xs)">—</span>';
  document.getElementById('sig-wbuy').innerHTML = s.weak_buy ? '<span class="val-green">YES</span>' : '<span style="color:var(--text-xs)">—</span>';
  document.getElementById('sig-wsell').innerHTML = s.weak_sell ? '<span class="val-red">YES</span>' : '<span style="color:var(--text-xs)">—</span>';
  const rsiEl = document.getElementById('sig-rsi');
  rsiEl.textContent = s.rsi_value;
  rsiEl.className = 'val ' + (s.rsi_value>70?'val-red':s.rsi_value<30?'val-green':'');
  document.getElementById('sig-macd').textContent = s.macd_value;
  document.getElementById('sig-pos').textContent = s.price_position + '%';
  document.getElementById('sig-rbuy').textContent = s.recommended_buy;
  document.getElementById('sig-rsell').textContent = s.recommended_sell;
  document.getElementById('sig-stop').textContent = s.stop_loss;
  document.getElementById('sig-support').textContent = s.support;
  document.getElementById('sig-resist').textContent = s.resistance;
  const banner = document.getElementById('alert-banner');
  banner.className='alert-banner'; banner.style.display='none';
  if (s.strong_buy) { banner.className='alert-banner alert-buy'; banner.textContent='强烈买入信号！多头评分 '+s.bullish_signals+'/6，建议关注买入机会'; }
  else if (s.strong_sell) { banner.className='alert-banner alert-sell'; banner.textContent='强烈卖出信号！空头评分 '+s.bearish_signals+'/6，注意风险'; }
}

// ---- Analyze ----
async function doAnalyze() {
  const start = getStartDate();
  const end = getEndDate();
  if (!currentSymbol) { alert('请选择或输入股票'); return; }

  // 确保详情视图可见（图表需要可见容器才能正确计算尺寸）
  if (currentView !== 'detail') {
    document.getElementById('view-watchlist').style.display = 'none';
    document.getElementById('view-detail').style.display = 'flex';
    currentView = 'detail';
    document.querySelectorAll('.view-tab').forEach(t => t.classList.remove('active'));
    const dtTab = document.querySelector('.view-tab[data-view="detail"]');
    if (dtTab) dtTab.classList.add('active');
    stopWatchlistRefresh();
  }

  showSpinner();
  try {
    const url = `/api/analyze?symbol=${currentSymbol}&start=${start}&end=${end}&period=${currentPeriod}${getSignalParams()}`;
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.error) { alert(data.error); hideSpinner(); return; }

    initCharts();
    document.getElementById('stock-name').textContent = `${data.name} (${data.symbol}) ${periodLabel(data.period)}`;

    const c = data.chart;
    candleSeries.setData(c.candles);
    volSeries.setData(c.volumes);
    fastEmaSeries.setData(c.fast_ema);
    slowEmaSeries.setData(c.slow_ema);
    bbUpperSeries.setData(c.bb_upper);
    bbBasisSeries.setData(c.bb_basis);
    bbLowerSeries.setData(c.bb_lower);

    const markers = [...c.strong_buy_markers,...c.strong_sell_markers,...c.weak_buy_markers,...c.weak_sell_markers].sort((a,b)=>a.time-b.time);
    candleSeries.setMarkers(markers);

    macdHistSeries.setData(c.macd_hist);
    macdLineSeries.setData(c.macd_line);
    macdSignalSeries.setData(c.signal_line);

    rsiSeries.setData(c.rsi);
    if (c.rsi.length > 1) {
      const t0 = c.rsi[0].time, t1 = c.rsi[c.rsi.length-1].time;
      rsiUpper.setData([{time:t0,value:70},{time:t1,value:70}]);
      rsiLower.setData([{time:t0,value:30},{time:t1,value:30}]);
    }

    // KDJ
    kdjKSeries.setData(c.kdj_k||[]);
    kdjDSeries.setData(c.kdj_d||[]);
    kdjJSeries.setData(c.kdj_j||[]);
    if ((c.kdj_k||[]).length > 1) {
      const kt0 = c.kdj_k[0].time, kt1 = c.kdj_k[c.kdj_k.length-1].time;
      kdjUpper.setData([{time:kt0,value:80},{time:kt1,value:80}]);
      kdjLower.setData([{time:kt0,value:20},{time:kt1,value:20}]);
    }

    // OBV
    obvSeries.setData(c.obv||[]);
    obvMaSeries.setData(c.obv_ma||[]);

    // VWAP
    vwapSeries.setData(c.vwap||[]);

    mainChart.timeScale().fitContent();
    resizeCharts();
    // 延迟再resize一次，确保display:none->block后尺寸正确
    setTimeout(() => { resizeCharts(); mainChart.timeScale().fitContent(); }, 50);

    loadDrawings();

    // Sidebar
    const s = data.signal;
    updateSignalPanel(s);
    if (mtfEnabled) fetchMTF();
    fetchSector();

  } catch(e) { alert('请求失败: '+e.message); } finally { hideSpinner(); startRealtime(); }
}

function periodLabel(p) {
  return {'5min':'5分钟','15min':'15分钟','30min':'30分钟','1h':'1小时','daily':'日线','weekly':'周线','monthly':'月线'}[p]||p;
}

// ---- Backtest ----
async function doBacktest() {
  const start = getStartDate();
  const end = getEndDate();
  if (!currentSymbol) { alert('请选择或输入股票'); return; }

  showSpinner();
  try {
    const url = `/api/backtest?symbol=${currentSymbol}&start=${start}&end=${end}` +
      `&strategy=${g('bt-strategy')}` +
      `&initial_capital=${g('bt-capital')}&commission=${g('bt-commission')}&stamp_tax=${g('bt-tax')}` +
      `&stop_loss_pct=${parseFloat(g('bt-sl'))/100}&take_profit_pct=${parseFloat(g('bt-tp'))/100}&trailing_stop_pct=${parseFloat(g('bt-tsl'))/100}&atr_stop_mult=${g('bt-atrsl')}` +
      `&position_mode=${g('bt-pos-mode')}&position_pct=${parseFloat(g('bt-pos-pct'))/100}` +
      getSignalParams();
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.error) { alert(data.error); hideSpinner(); return; }

    const panel = document.getElementById('backtest-panel');
    panel.style.display = 'block';
    document.getElementById('bt-strategy-label').textContent = '— ' + (data.strategy_name || '');

    const items = [
      {lbl:'总收益率', val:data.total_return+'%', cls:data.total_return>=0?'val-green':'val-red'},
      {lbl:'年化收益', val:data.annual_return+'%', cls:data.annual_return>=0?'val-green':'val-red'},
      {lbl:'最大回撤', val:data.max_drawdown+'%', cls:'val-red'},
      {lbl:'胜率', val:data.win_rate+'%', cls:data.win_rate>=50?'val-green':'val-red'},
      {lbl:'交易次数', val:data.total_trades, cls:''},
      {lbl:'盈/亏', val:data.profit_trades+'/'+data.loss_trades, cls:''},
      {lbl:'Sharpe', val:data.sharpe_ratio, cls:data.sharpe_ratio>=1?'val-green':''},
    ];
    document.getElementById('bt-grid').innerHTML = items.map(i=>`<div class="bt-cell"><div class="bt-val ${i.cls}">${i.val}</div><div class="bt-lbl">${i.lbl}</div></div>`).join('');

    const table = document.getElementById('bt-trades');
    if (data.trades.length) {
      let html = '<thead><tr><th>买入日期</th><th>卖出日期</th><th>买入价</th><th>卖出价</th><th>数量</th><th>盈亏</th><th>收益率</th><th>类型</th></tr></thead><tbody>';
      data.trades.forEach(t => {
        const cls = t.pnl>=0?'val-green':'val-red';
        const exitBadge = t.exit_type === 'stop_loss' ? '<span style="color:var(--red);font-size:11px;">止损</span>' : 
                  t.exit_type === 'take_profit' ? '<span style="color:var(--green);font-size:11px;">止盈</span>' :
                  t.exit_type === 'trailing_stop' ? '<span style="color:var(--gold);font-size:11px;">移动止损</span>' : '';
        html += `<tr><td>${t.buy_date||''}</td><td>${t.sell_date}</td><td>${t.entry_price}</td><td>${t.exit_price}</td><td>${t.shares}</td><td class="${cls}">${t.pnl}</td><td class="${cls}">${t.return_pct}%</td><td>${exitBadge}</td></tr>`;
      });
      table.innerHTML = html + '</tbody>';
    } else {
      table.innerHTML = '<tr><td style="padding:20px;color:var(--text-dim)">无交易记录</td></tr>';
    }

    // 画权益曲线
    drawEquityCurve(data.equity_curve);
    // 隐藏对比区域
    document.getElementById('compare-box').style.display = 'none';

    panel.scrollIntoView({behavior:'smooth'});
  } catch(e) { alert('回测失败: '+e.message); } finally { hideSpinner(); }
}

// ---- Realtime refresh ----
let realtimeTimer = null;
let realtimeEnabled = false;
const REFRESH_INTERVAL = 5000; // 5秒

function startRealtime() {
  if (realtimeTimer) clearInterval(realtimeTimer);
  realtimeEnabled = true;
  realtimeTimer = setInterval(fetchRealtime, REFRESH_INTERVAL);
  updateStatusText('实时刷新中...');
}

function stopRealtime() {
  if (realtimeTimer) { clearInterval(realtimeTimer); realtimeTimer = null; }
  realtimeEnabled = false;
  updateStatusText('已暂停');
}

function updateStatusText(msg) {
  const el = document.getElementById('realtime-status');
  const now = new Date();
  const t = now.getHours().toString().padStart(2,'0') + ':' + now.getMinutes().toString().padStart(2,'0') + ':' + now.getSeconds().toString().padStart(2,'0');
  el.textContent = msg + ' ' + t;
}

function isMarketOpen() {
  const now = new Date();
  const day = now.getDay();
  if (day === 0 || day === 6) return false; // 周末
  const hhmm = now.getHours() * 100 + now.getMinutes();
  // A股交易时间 9:15-11:30, 13:00-15:00 (加上集合竞价)
  return (hhmm >= 915 && hhmm <= 1130) || (hhmm >= 1300 && hhmm <= 1500);
}

async function fetchRealtime() {
  if (!chartsReady || !currentSymbol) return;
  try {
    const start = getStartDate();
    const end = getEndDate();
    const url = `/api/realtime?symbol=${currentSymbol}&start=${start}&end=${end}&period=${currentPeriod}${getSignalParams()}`;
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.error) { updateStatusText('错误: ' + data.error); return; }

    const q = data.quote;
    const lb = data.last_bar;

    // 更新实时价格显示
    const priceEl = document.getElementById('realtime-price');
    const changeEl = document.getElementById('realtime-change');
    priceEl.textContent = q.price.toFixed(2);
    const chg = q.price - q.yesterday_close;
    const chgPct = (chg / q.yesterday_close * 100);
    const sign = chg >= 0 ? '+' : '';
    changeEl.textContent = `${sign}${chg.toFixed(2)} (${sign}${chgPct.toFixed(2)}%)`;
    priceEl.style.color = chg >= 0 ? 'var(--green)' : 'var(--red)';
    changeEl.style.color = chg >= 0 ? 'var(--green)' : 'var(--red)';

    // 增量更新图表最后一根bar（update而非setData，性能好）
    if (lb.candle) candleSeries.update(lb.candle);
    if (lb.volume) volSeries.update(lb.volume);
    if (lb.fast_ema && lb.fast_ema.value != null) fastEmaSeries.update(lb.fast_ema);
    if (lb.slow_ema && lb.slow_ema.value != null) slowEmaSeries.update(lb.slow_ema);
    if (lb.bb_upper && lb.bb_upper.value != null) bbUpperSeries.update(lb.bb_upper);
    if (lb.bb_basis && lb.bb_basis.value != null) bbBasisSeries.update(lb.bb_basis);
    if (lb.bb_lower && lb.bb_lower.value != null) bbLowerSeries.update(lb.bb_lower);

    if (lb.macd_line && lb.macd_line.value != null) macdLineSeries.update(lb.macd_line);
    if (lb.signal_line && lb.signal_line.value != null) macdSignalSeries.update(lb.signal_line);
    if (lb.macd_hist && lb.macd_hist.value != null) {
      const mhv = lb.macd_hist.value;
      macdHistSeries.update({time: lb.macd_hist.time, value: mhv, color: mhv >= 0 ? 'rgba(239,83,80,0.8)' : 'rgba(38,166,154,0.8)'});
    }
    if (lb.rsi && lb.rsi.value != null) rsiSeries.update(lb.rsi);

    if (lb.kdj_k && lb.kdj_k.value != null) kdjKSeries.update(lb.kdj_k);
    if (lb.kdj_d && lb.kdj_d.value != null) kdjDSeries.update(lb.kdj_d);
    if (lb.kdj_j && lb.kdj_j.value != null) kdjJSeries.update(lb.kdj_j);
    if (lb.obv && lb.obv.value != null) obvSeries.update(lb.obv);
    if (lb.obv_ma && lb.obv_ma.value != null) obvMaSeries.update(lb.obv_ma);
    if (lb.vwap && lb.vwap.value != null) vwapSeries.update(lb.vwap);

    // 更新信号面板
    const s = data.signal;
    updateSignalPanel(s);

    updateStatusText(isMarketOpen() ? '交易中' : '已收盘');
  } catch(e) {
    updateStatusText('刷新失败');
  }
}

// Load strategies & presets
let presetData = [];
let strategyData = [];
let initializing = true;

async function loadStrategies() {
  try {
    // 并行加载策略和预设
    const [sr, pr] = await Promise.all([fetch('/api/strategies'), fetch('/api/presets')]);
    strategyData = await sr.json();
    presetData = await pr.json();

    // 填充策略下拉
    const sel = document.getElementById('bt-strategy');
    const desc = document.getElementById('bt-strategy-desc');
    sel.innerHTML = strategyData.map(s => `<option value="${s.id}">${s.name}</option>`).join('');
    sel._descs = {}; sel._presets = {};
    strategyData.forEach(s => { sel._descs[s.id] = s.desc; sel._presets[s.id] = s.recommended_preset; });
    let _syncLock = false;  // 防止双向同步循环
    sel.addEventListener('change', () => {
      desc.textContent = sel._descs[sel.value] || '';
      if (_syncLock) return;
      // 切换策略时自动切换推荐预设
      const recPreset = sel._presets[sel.value];
      if (recPreset) {
        _syncLock = true;
        document.getElementById('p-preset').value = recPreset;
        applyPreset(recPreset);
        _syncLock = false;
      }
    });
    sel.value = 'macd_rsi';
    desc.textContent = sel._descs['macd_rsi'] || '';

    // 填充参数预设下拉
    const psel = document.getElementById('p-preset');
    const pdesc = document.getElementById('p-preset-desc');
    psel.innerHTML = presetData.map(p => `<option value="${p.id}">${p.name}</option>`).join('');
    psel._map = {}; psel._strategies = {};
    presetData.forEach(p => { psel._map[p.id] = p; if(p.recommended_strategy) psel._strategies[p.id] = p.recommended_strategy; });
    psel.addEventListener('change', () => {
      if (_syncLock) return;
      _syncLock = true;
      applyPreset(psel.value);
      _syncLock = false;
    });
    psel.value = 'default';
    pdesc.textContent = psel._map['default'] ? psel._map['default'].desc : '';

    // 自动选macd_rsi的推荐预设
    const recPreset = sel._presets['macd_rsi'];
    if (recPreset) { psel.value = recPreset; applyPreset(recPreset); }
  } catch(e) { console.error('Failed to load strategies/presets:', e); }
}

function applyPreset(presetId) {
  const psel = document.getElementById('p-preset');
  const pdesc = document.getElementById('p-preset-desc');
  const preset = psel._map[presetId];
  if (!preset) return;
  pdesc.textContent = preset.desc;
  const p = preset.params;
  document.getElementById('p-fast').value = p.fast_length;
  document.getElementById('p-slow').value = p.slow_length;
  document.getElementById('p-signal').value = p.signal_length;
  document.getElementById('p-rsi').value = p.rsi_length;
  document.getElementById('p-bb').value = p.bb_length;
  document.getElementById('p-bbmult').value = p.bb_mult;
  document.getElementById('p-vol').value = p.volume_length;
  document.getElementById('p-atr').value = p.atr_length;
  currentPriceMode = p.price_mode || 'default';
  // 同步回测策略下拉框（不触发change事件，由_syncLock控制）
  const recStrategy = psel._strategies && psel._strategies[presetId];
  if (recStrategy) {
    const bsel = document.getElementById('bt-strategy');
    if (bsel) {
      bsel.value = recStrategy;
      const bdesc = document.getElementById('bt-strategy-desc');
      if (bdesc && bsel._descs) bdesc.textContent = bsel._descs[recStrategy] || '';
    }
  }
  // 参数变化后自动重新分析（初始化阶段跳过）
  if (!initializing) doAnalyze();
}

// ---- Equity & Compare Charts ----
let equityChart = null;
let compareChart = null;
let compareSeries = [];

const COMPARE_COLORS = [
  '#4f8ff7', '#ff4757', '#00d98b', '#ffb347', '#a78bfa', '#ff6b9d',
  '#36d7b7', '#f7dc6f', '#bb8fce', '#85c1e9', '#f0b27a', '#73c6b6'
];

function drawEquityCurve(data) {
  const box = document.getElementById('equity-box');
  if (!data || !data.length) { box.style.display = 'none'; return; }
  box.style.display = 'block';

  const el = document.getElementById('chart-equity');
  if (equityChart) { equityChart.remove(); equityChart = null; }
  equityChart = LightweightCharts.createChart(el, {
    layout: { background:{color:CHART_BG}, textColor:TEXT_COLOR, fontFamily:"'Noto Sans SC','JetBrains Mono',sans-serif", fontSize:12 },
    grid: { vertLines:{color:GRID_COLOR}, horzLines:{color:GRID_COLOR} },
    rightPriceScale: { borderColor:GRID_COLOR },
    timeScale: { borderColor:GRID_COLOR, timeVisible:false },
    width: el.clientWidth, height: 250,
  });

  const areaSeries = equityChart.addAreaSeries({
    lineColor: '#4f8ff7', topColor: 'rgba(79,143,247,0.3)', bottomColor: 'rgba(79,143,247,0.02)', lineWidth: 2,
  });
  areaSeries.setData(data);

  // 初始资金基准线
  const baseline = equityChart.addLineSeries({ color:TEXT_COLOR, lineWidth:1.5, lineStyle:2 });
  baseline.setData([
    { time: data[0].time, value: data[0].value },
    { time: data[data.length-1].time, value: data[0].value },
  ]);

  equityChart.timeScale().fitContent();
}

// ---- Compare Modal ----
function showCompareModal() {
  const modal = document.getElementById('compare-modal');
  const container = document.getElementById('compare-strategies');
  // Populate checkboxes from strategyData
  container.innerHTML = strategyData.map(s =>
    `<div class="modal-strat-item">
      <input type="checkbox" id="cmp-${s.id}" value="${s.id}" checked>
      <label for="cmp-${s.id}">${s.name}</label>
    </div>`
  ).join('');
  modal.classList.add('show');
}
function hideCompareModal() { document.getElementById('compare-modal').classList.remove('show'); }
function compareSelectAll() { document.querySelectorAll('#compare-strategies input').forEach(cb => cb.checked = true); }
function compareClearAll() { document.querySelectorAll('#compare-strategies input').forEach(cb => cb.checked = false); }

async function doCompare() {
  const selected = [];
  document.querySelectorAll('#compare-strategies input:checked').forEach(cb => selected.push(cb.value));
  if (selected.length < 1) { alert('请至少选择一个策略'); return; }
  hideCompareModal();

  const start = getStartDate();
  const end = getEndDate();
  if (!currentSymbol) { alert('请选择股票'); return; }

  showSpinner();
  try {
    const url = `/api/backtest/compare?symbol=${currentSymbol}&start=${start}&end=${end}&strategies=${selected.join(',')}` +
      `&initial_capital=${g('bt-capital')}&commission=${g('bt-commission')}&stamp_tax=${g('bt-tax')}` +
      `&stop_loss_pct=${parseFloat(g('bt-sl'))/100}&take_profit_pct=${parseFloat(g('bt-tp'))/100}&trailing_stop_pct=${parseFloat(g('bt-tsl'))/100}&atr_stop_mult=${g('bt-atrsl')}` +
      `&position_mode=${g('bt-pos-mode')}&position_pct=${parseFloat(g('bt-pos-pct'))/100}` +
      getSignalParams();
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.error) { alert(data.error); hideSpinner(); return; }

    // Show results
    const panel = document.getElementById('backtest-panel');
    panel.style.display = 'block';
    document.getElementById('equity-box').style.display = 'none';
    document.getElementById('bt-grid').innerHTML = '';
    document.getElementById('bt-trades').innerHTML = '';
    document.getElementById('bt-strategy-label').textContent = '';

    const cbox = document.getElementById('compare-box');
    cbox.style.display = 'block';

    // Draw compare chart
    const cel = document.getElementById('chart-compare');
    if (compareChart) { compareChart.remove(); compareChart = null; }
    compareSeries = [];
    compareChart = LightweightCharts.createChart(cel, {
      layout: { background:{color:CHART_BG}, textColor:TEXT_COLOR, fontFamily:"'Noto Sans SC','JetBrains Mono',sans-serif", fontSize:12 },
      grid: { vertLines:{color:GRID_COLOR}, horzLines:{color:GRID_COLOR} },
      rightPriceScale: { borderColor:GRID_COLOR },
      timeScale: { borderColor:GRID_COLOR, timeVisible:false },
      width: cel.clientWidth, height: 300,
    });

    // Legend
    const legendEl = document.getElementById('compare-legend');
    legendEl.innerHTML = '';

    data.forEach((item, idx) => {
      const color = COMPARE_COLORS[idx % COMPARE_COLORS.length];
      const series = compareChart.addLineSeries({ color: color, lineWidth: 2, title: item.strategy_name });
      series.setData(item.equity_curve);
      compareSeries.push({ series, visible: true });

      // Clickable legend
      const leg = document.createElement('div');
      leg.className = 'compare-legend-item';
      leg.innerHTML = `<span class="compare-legend-dot" style="background:${color}"></span>${item.strategy_name}`;
      leg.style.opacity = '1';
      leg.addEventListener('click', () => {
        const s = compareSeries[idx];
        s.visible = !s.visible;
        s.series.applyOptions({ visible: s.visible });
        leg.style.opacity = s.visible ? '1' : '0.3';
      });
      legendEl.appendChild(leg);
    });

    compareChart.timeScale().fitContent();

    // Rank table
    const table = document.getElementById('compare-table');
    let html = '<thead><tr><th>#</th><th>策略</th><th>总收益</th><th>年化</th><th>最大回撤</th><th>胜率</th><th>Sharpe</th><th>交易次数</th></tr></thead><tbody>';
    data.forEach((item, idx) => {
      const retCls = item.total_return >= 0 ? 'val-green' : 'val-red';
      const annCls = item.annual_return >= 0 ? 'val-green' : 'val-red';
      const wrCls = item.win_rate >= 50 ? 'val-green' : 'val-red';
      const spCls = item.sharpe_ratio >= 1 ? 'val-green' : '';
      const dot = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${COMPARE_COLORS[idx % COMPARE_COLORS.length]};margin-right:4px;"></span>`;
      html += `<tr>
        <td>${idx+1}</td>
        <td style="text-align:left;">${dot}${item.strategy_name}</td>
        <td class="${retCls}">${item.total_return}%</td>
        <td class="${annCls}">${item.annual_return}%</td>
        <td class="val-red">${item.max_drawdown}%</td>
        <td class="${wrCls}">${item.win_rate}%</td>
        <td class="${spCls}">${item.sharpe_ratio}</td>
        <td>${item.total_trades}</td>
      </tr>`;
    });
    html += '</tbody>';
    table.innerHTML = html;

    panel.scrollIntoView({behavior:'smooth'});
  } catch(e) { alert('策略对比失败: '+e.message); } finally { hideSpinner(); }
}

// ---- Theme ----
function getChartTheme() {
  const cs = getComputedStyle(document.documentElement);
  return {
    bg: cs.getPropertyValue('--chart-bg').trim() || '#080c14',
    grid: cs.getPropertyValue('--chart-grid').trim() || 'rgba(37,48,68,0.5)',
    text: cs.getPropertyValue('--chart-text').trim() || '#7a8ba4',
  };
}

function applyThemeToCharts() {
  const t = getChartTheme();
  const opts = { layout:{background:{color:t.bg},textColor:t.text}, grid:{vertLines:{color:t.grid},horzLines:{color:t.grid}} };
  if (mainChart) mainChart.applyOptions(opts);
  if (macdChart) macdChart.applyOptions(opts);
  if (rsiChart) rsiChart.applyOptions(opts);
  if (kdjChart) kdjChart.applyOptions(opts);
  if (obvChart) obvChart.applyOptions(opts);
  if (equityChart) equityChart.applyOptions(opts);
  if (compareChart) compareChart.applyOptions(opts);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  document.getElementById('theme-toggle').textContent = next === 'dark' ? '☀' : '🌙';
  localStorage.setItem('theme', next);
  const t = getChartTheme();
  CHART_BG = t.bg; GRID_COLOR = t.grid; TEXT_COLOR = t.text;
  applyThemeToCharts();
}

(function(){
  const saved = localStorage.getItem('theme');
  if (saved === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
    document.addEventListener('DOMContentLoaded', () => {
      document.getElementById('theme-toggle').textContent = '🌙';
      const t = getChartTheme();
      CHART_BG = t.bg; GRID_COLOR = t.grid; TEXT_COLOR = t.text;
    });
  }
})();

// ---- MTF ----
let mtfEnabled = false;

function toggleMTF() {
  mtfEnabled = document.getElementById('mtf-toggle').checked;
  localStorage.setItem('mtfEnabled', mtfEnabled);
  document.getElementById('mtf-card').style.display = mtfEnabled ? 'block' : 'none';
  if (mtfEnabled && currentSymbol) fetchMTF();
}

async function fetchMTF() {
  if (!mtfEnabled || !currentSymbol) return;
  try {
    const start = getStartDate();
    const resp = await fetch(`/api/mtf?symbol=${currentSymbol}&start=${start}`);
    const data = await resp.json();
    if (data.error) return;
    const wk = data.details && data.details.weekly;
    if (wk) {
      const tEl = document.getElementById('mtf-wk-trend');
      tEl.textContent = wk.trend;
      tEl.className = 'val ' + (wk.trend==='上涨'?'val-green':wk.trend==='下跌'?'val-red':'val-gold');
      document.getElementById('mtf-wk-bull').textContent = wk.bull_score + '/6';
      document.getElementById('mtf-wk-bear').textContent = wk.bear_score + '/6';
    }
    const cfEl = document.getElementById('mtf-confirm');
    if (data.confirmed_bull) { cfEl.textContent = '多头共振'; cfEl.className = 'val val-green'; }
    else if (data.confirmed_bear) { cfEl.textContent = '空头共振'; cfEl.className = 'val val-red'; }
    else { cfEl.textContent = '未确认'; cfEl.className = 'val val-gold'; }
  } catch(e) {}
}

// Restore MTF state
(function(){
  const saved = localStorage.getItem('mtfEnabled');
  if (saved === 'true') {
    mtfEnabled = true;
    document.addEventListener('DOMContentLoaded', () => {
      const cb = document.getElementById('mtf-toggle');
      if (cb) cb.checked = true;
      document.getElementById('mtf-card').style.display = 'block';
    });
  }
})();

// ---- Alert Engine ----
const alertCooldown = {};
const ALERT_COOLDOWN_MS = 300000; // 5分钟冷却
let alertTimer = null;
let alertEnabled = false;
let alertSoundEnabled = true;
let audioCtx = null;

function toggleAlerts() {
  alertEnabled = document.getElementById('alert-enabled').checked;
  localStorage.setItem('alertEnabled', alertEnabled);
  if (alertEnabled) {
    startAlertEngine();
    if (!audioCtx) { try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch(e){} }
  } else {
    stopAlertEngine();
  }
}

function toggleAlertSound() {
  alertSoundEnabled = document.getElementById('alert-sound').checked;
  localStorage.setItem('alertSoundEnabled', alertSoundEnabled);
}

function startAlertEngine() {
  if (alertTimer) clearInterval(alertTimer);
  alertTimer = setInterval(checkAlerts, 10000);
}

function stopAlertEngine() {
  if (alertTimer) { clearInterval(alertTimer); alertTimer = null; }
}

async function checkAlerts() {
  if (!alertEnabled) return;
  try {
    const resp = await fetch('/api/watchlist/realtime');
    const data = await resp.json();
    if (!data || data.error) return;
    data.forEach(stock => {
      if (stock.signal === '强烈买入' || stock.signal === '买入') {
        fireAlert(stock.symbol, stock.name, stock.signal, 'buy');
      } else if (stock.signal === '强烈卖出' || stock.signal === '卖出') {
        fireAlert(stock.symbol, stock.name, stock.signal, 'sell');
      }
    });
  } catch(e) {}
}

function fireAlert(symbol, name, signal, type) {
  const key = symbol + '_' + signal;
  const last = alertCooldown[key] || 0;
  if (Date.now() - last < ALERT_COOLDOWN_MS) return;
  alertCooldown[key] = Date.now();
  
  showToast(name + '(' + symbol + ')', signal, type);
  
  if (alertSoundEnabled && audioCtx) {
    try {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.frequency.value = type === 'buy' ? 800 : 400;
      gain.gain.value = 0.3;
      osc.start();
      osc.stop(audioCtx.currentTime + 0.2);
    } catch(e) {}
  }
}

function showToast(title, msg, type) {
  const container = document.getElementById('alert-container');
  const toast = document.createElement('div');
  toast.className = 'alert-toast';
  const icon = type === 'buy' ? '📈' : '📉';
  const color = type === 'buy' ? 'var(--green)' : 'var(--red)';
  toast.innerHTML = `<span class="alert-toast-icon">${icon}</span><div class="alert-toast-body"><div class="alert-toast-title" style="color:${color}">${title}</div><div class="alert-toast-msg">${msg}</div></div><button class="alert-toast-close" onclick="this.parentElement.remove()">×</button>`;
  container.appendChild(toast);
  setTimeout(() => { toast.classList.add('fade-out'); setTimeout(() => toast.remove(), 300); }, 5000);
}

(function() {
  const saved = localStorage.getItem('alertEnabled');
  const soundSaved = localStorage.getItem('alertSoundEnabled');
  if (soundSaved === 'false') alertSoundEnabled = false;
  if (saved === 'true') {
    alertEnabled = true;
    document.addEventListener('DOMContentLoaded', () => {
      const cb = document.getElementById('alert-enabled');
      if (cb) cb.checked = true;
      const scb = document.getElementById('alert-sound');
      if (scb) scb.checked = alertSoundEnabled;
      startAlertEngine();
    });
  } else {
    document.addEventListener('DOMContentLoaded', () => {
      const scb = document.getElementById('alert-sound');
      if (scb) scb.checked = alertSoundEnabled;
    });
  }
})();

// ---- Watchlist ----
let currentView = 'watchlist';
let watchlistTimer = null;
const WL_REFRESH_INTERVAL = 5000;
let wlAddTimeout = null;

function switchView(view) {
  currentView = view;
  document.querySelectorAll('.view-tab').forEach(t => t.classList.remove('active'));
  document.querySelector(`.view-tab[data-view="${view}"]`).classList.add('active');
  
  const wlView = document.getElementById('view-watchlist');
  const dtView = document.getElementById('view-detail');
  
  if (view === 'watchlist') {
    wlView.style.display = 'block';
    dtView.style.display = 'none';
    stopRealtime();
    startWatchlistRefresh();
    refreshWatchlist();
  } else {
    wlView.style.display = 'none';
    dtView.style.display = 'flex';
    stopWatchlistRefresh();
    // 延迟一帧让DOM渲染完再resize图表
    requestAnimationFrame(() => { resizeCharts(); });
    if (chartsReady) startRealtime();
  }
}

function startWatchlistRefresh() {
  if (watchlistTimer) clearInterval(watchlistTimer);
  watchlistTimer = setInterval(refreshWatchlist, WL_REFRESH_INTERVAL);
}

function stopWatchlistRefresh() {
  if (watchlistTimer) { clearInterval(watchlistTimer); watchlistTimer = null; }
}

async function refreshWatchlist() {
  try {
    const resp = await fetch('/api/watchlist/realtime');
    const data = await resp.json();
    if (data.error) return;
    renderWatchlist(data);
    const now = new Date();
    const t = now.getHours().toString().padStart(2,'0') + ':' + now.getMinutes().toString().padStart(2,'0') + ':' + now.getSeconds().toString().padStart(2,'0');
    document.getElementById('wl-status').textContent = '更新于 ' + t;
  } catch(e) {}
}

function renderWatchlist(data) {
  const empty = document.getElementById('wl-empty');
  const table = document.getElementById('wl-table');
  const tbody = document.getElementById('wl-tbody');
  
  if (!data || data.length === 0) {
    empty.style.display = 'block';
    table.style.display = 'none';
    return;
  }
  empty.style.display = 'none';
  table.style.display = 'table';
  
  tbody.innerHTML = data.map(d => {
    const chgCls = d.change >= 0 ? 'val-green' : 'val-red';
    const sign = d.change >= 0 ? '+' : '';
    const sigCls = (d.signal === '强烈买入' || d.signal === '买入') ? 'val-green' : (d.signal === '强烈卖出' || d.signal === '卖出') ? 'val-red' : '';
    const sigBg = (d.signal === '强烈买入') ? 'background:var(--green-dim)' : (d.signal === '强烈卖出') ? 'background:var(--red-dim)' : 'background:var(--bg4)';
    const trendCls = d.trend === '上涨' ? 'val-green' : d.trend === '下跌' ? 'val-red' : '';
    return `<tr onclick="goDetail('${d.symbol}')">
      <td><span class="td-name">${d.name}</span><br><span class="td-code">${d.symbol}</span></td>
      <td class="td-price ${chgCls}">${d.price.toFixed(2)}</td>
      <td class="td-change ${chgCls}">${sign}${d.change_pct.toFixed(2)}%</td>
      <td class="td-score">${d.bull_score}/6</td>
      <td class="td-score">${d.bear_score}/6</td>
      <td class="${trendCls}">${d.trend}</td>
      <td><span class="td-signal ${sigCls}" style="${sigBg}">${d.signal}</span></td>
      <td><button class="wl-del-btn" onclick="event.stopPropagation();delWatchlist('${d.symbol}')">删除</button></td>
    </tr>`;
  }).join('');
}

function goDetail(symbol) {
  currentSymbol = symbol;
  document.getElementById('inp-search').value = symbol;
  switchView('detail');
  doAnalyze();
}

async function delWatchlist(symbol) {
  try {
    await fetch('/api/watchlist/' + symbol, {method: 'DELETE'});
    refreshWatchlist();
  } catch(e) {}
}

// Watchlist add search
const wlAddInput = document.getElementById('wl-add-input');
const wlAddDropdown = document.getElementById('wl-add-dropdown');

wlAddInput.addEventListener('input', () => {
  clearTimeout(wlAddTimeout);
  const kw = wlAddInput.value.trim();
  if (kw.length < 1) { wlAddDropdown.classList.remove('show'); return; }
  wlAddTimeout = setTimeout(async () => {
    try {
      const r = await fetch('/api/search?keyword=' + encodeURIComponent(kw));
      const data = await r.json();
      if (!data.length) { wlAddDropdown.classList.remove('show'); return; }
      wlAddDropdown.innerHTML = data.map(d =>
        `<div class="wl-add-item" data-code="${d.code}" data-name="${d.name}">
          <span><span style="color:var(--accent);font-family:'JetBrains Mono',monospace;font-weight:600;">${d.code}</span> ${d.name}</span>
          <span style="color:var(--text-dim);font-size:12px;">${d.market}</span>
        </div>`
      ).join('');
      wlAddDropdown.classList.add('show');
      wlAddDropdown.querySelectorAll('.wl-add-item').forEach(el => {
        el.addEventListener('click', async () => {
          const code = el.dataset.code;
          const name = el.dataset.name;
          wlAddDropdown.classList.remove('show');
          wlAddInput.value = '';
          try {
            const r = await fetch('/api/watchlist', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({symbol: code, name: name})
            });
            const res = await r.json();
            if (res.error) { alert(res.error); return; }
            refreshWatchlist();
          } catch(e) { alert('添加失败'); }
        });
      });
    } catch(e) {}
  }, 300);
});

document.addEventListener('click', (e) => {
  if (!e.target.closest('.wl-add-wrap')) wlAddDropdown.classList.remove('show');
});

// Auto-load, then start realtime
window.addEventListener('DOMContentLoaded', async () => {
  await loadStrategies();
  initializing = false;
  // Start in watchlist view
  switchView('watchlist');
});

// 页面不可见时暂停，可见时恢复
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopRealtime();
    stopWatchlistRefresh();
  } else {
    if (currentView === 'watchlist') startWatchlistRefresh();
    else if (chartsReady) startRealtime();
  }
});
</script>
</body>
</html>"""


@app.route("/")
def index():
    return HTML_PAGE


@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "A股多空信号监控系统",
        "short_name": "A股监控",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#080c14",
        "theme_color": "#080c14",
        "orientation": "any",
        "icons": []
    })


if __name__ == "__main__":
    import socket

    def get_local_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    port = 5000
    local_ip = get_local_ip()
    local_url = f"http://{local_ip}:{port}"

    print()
    print("=" * 50)
    print("  A股多空信号监控系统")
    print("=" * 50)
    print(f"  本机访问: http://localhost:{port}")
    print(f"  局域网访问: {local_url}")
    print()
    print(f"  手机请连接同一WiFi，浏览器打开:")
    print(f"  {local_url}")
    print()
    print("  提示: 手机浏览器打开后，可点击")
    print("  '添加到主屏幕'获得APP体验")
    print("=" * 50)
    print()

    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(host="0.0.0.0", port=port, debug=False)
