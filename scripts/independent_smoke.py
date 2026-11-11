"""Run inside a separately installed distribution; never imports original paths."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

root = Path(__file__).resolve().parents[1]
os.chdir(root)
import app.main
import agent

assert Path(app.main.__file__).resolve().is_relative_to(root)
assert Path(agent.__file__).resolve().is_relative_to(root)
assert (root/'assets/fonts/NotoSansKR-Regular.ttf').is_file()
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
env = os.environ.copy()
env['DATA_DIR'] = tempfile.mkdtemp(prefix='independent-data-', dir=root)
env['PASSWORD_RESET_DELIVERY'] = 'disabled'
env.pop('PYTHONPATH', None)
log = (root/'server-smoke.log').open('w', encoding='utf-8')
process = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)],
    cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
try:
    for _ in range(100):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=1) as response:
                health = json.load(response)
            break
        except (OSError, ValueError):
            if process.poll() is not None:
                raise RuntimeError('Server exited; inspect server-smoke.log')
            time.sleep(.1)
    else:
        raise RuntimeError('Server failed to start')
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/openapi.json', timeout=2) as response:
        schema = json.load(response)
    assert '/api/v1/comparison-groups/{group_id}/analyze' in schema['paths']
    assert health['data']['aiMode'] == 'direct'
    report={'root':str(root),'python':sys.version,'app':app.main.__file__,'agent':agent.__file__,
        'health':health,'api_paths':len(schema['paths']),'single_app_http_started':True}
    (root/'independent-smoke.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
finally:
    process.terminate()
    process.wait(timeout=10)
    log.close()
