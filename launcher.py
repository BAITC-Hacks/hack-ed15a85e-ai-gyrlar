"""Double-click launcher: reuse a running StockPilot instance, or start it hidden."""
import json
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8765'


def running():
    try:
        with urlopen(URL+'/api/health', timeout=1) as response:
            return json.load(response).get('service') == 'StockPilot'
    except Exception:
        return False


if not running():
    (ROOT/'outputs').mkdir(exist_ok=True)
    with (ROOT/'outputs/server.log').open('a', encoding='utf-8') as log:
        process = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT/'app.py')], cwd=ROOT,
                                   stdout=log, stderr=log,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for _ in range(40):
        if running():
            break
        if process.poll() is not None:
            raise SystemExit('StockPilot did not start. See outputs/server.log; port 8765 may be busy.')
        time.sleep(.3)
if running():
    webbrowser.open(URL)
else:
    raise SystemExit('Server is still starting. Open '+URL+' or inspect outputs/server.log.')
