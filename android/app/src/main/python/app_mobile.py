"""Mobile entry point for Flask server on Android"""
import sys
import os

# Add the app's Python path
app_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, app_dir)

# Mock matplotlib before anything imports it (not available on Android)
import types
mock_mpl = types.ModuleType("matplotlib")
mock_mpl.use = lambda *a, **k: None
sys.modules["matplotlib"] = mock_mpl
sys.modules["matplotlib.pyplot"] = types.ModuleType("matplotlib.pyplot")
sys.modules["matplotlib.dates"] = types.ModuleType("matplotlib.dates")
sys.modules["matplotlib.patches"] = types.ModuleType("matplotlib.patches")

# Mock mplfinance
sys.modules["mplfinance"] = types.ModuleType("mplfinance")

# Mock tabulate
sys.modules["tabulate"] = types.ModuleType("tabulate")


def start_server():
    """Start Flask server - called from Java"""
    import app as main_app
    main_app.app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
