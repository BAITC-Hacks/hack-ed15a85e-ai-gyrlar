"""Double-click launcher: reuse a running StockPilot instance, or start it hidden."""
import json
import hashlib
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
BASE_PORT = 8765


def expected_products():
    try:
        active = ROOT/'data/active-dataset.json'
        mode = json.loads(active.read_text(encoding='utf-8')).get('mode') if active.exists() else 'partner'
        if mode == 'demo':
            return 6
        if mode == 'upload':
            data = json.loads((ROOT/'data/uploaded-state.json').read_text(encoding='utf-8'))['dataset']
        else:
            data = json.loads((ROOT/'data/dataset.json').read_text(encoding='utf-8'))
        return len(data['items'])
    except (OSError, ValueError, KeyError, TypeError):
        return 6


def running(port):
    try:
        with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=1) as response:
            health = json.load(response)
            if health.get('workspace'):
                return health.get('service') == 'StockPilot' and health['workspace'] == hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()
            return health.get('service') == 'StockPilot' and health.get('products') == expected_products()
    except Exception:
        return False


def port_available(port):
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def main():
    ports = range(BASE_PORT, BASE_PORT+20)
    port = next((candidate for candidate in ports if running(candidate)), None)
    if port is None:
        port = next((candidate for candidate in ports if port_available(candidate)), None)
        if port is None:
            raise SystemExit('Не нашёл свободный порт для StockPilot.')
        (ROOT/'outputs').mkdir(exist_ok=True)
        with (ROOT/'outputs/server.log').open('a', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT/'app.py'), '--port', str(port)], cwd=ROOT,
                                       stdout=log, stderr=log,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for _ in range(40):
            if running(port):
                break
            if process.poll() is not None:
                raise SystemExit('StockPilot did not start. See outputs/server.log.')
            time.sleep(.3)
    if running(port):
        webbrowser.open(f'http://127.0.0.1:{port}')
    else:
        raise SystemExit(f'Server is still starting. Open http://127.0.0.1:{port} or inspect outputs/server.log.')


if __name__ == "__main__":
    main()
