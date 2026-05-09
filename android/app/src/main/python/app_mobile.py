"""Mobile entry point for Flask server on Android"""
import sys
import os

# Add the app's Python path
app_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, app_dir)

def start_server():
    """Start Flask server - called from Java"""
    # Import here to avoid import errors during class loading
    from flask import Flask, request, jsonify
    import json
    import math
    import threading
    from dataclasses import asdict
    
    from src.data import fetch_stock_daily, fetch_stock_info, search_stock, fetch_realtime_quote, fetch_realtime_quotes_batch
    from src.signals import compute_signals, get_latest_signal, SignalParams
    from src.backtest import backtest, get_strategy_list, get_preset_list, backtest_compare
    
    # Import the main app module to reuse all routes
    import app as main_app
    
    # Run without opening browser
    main_app.app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
