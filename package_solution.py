"""Build a source-only submission archive without partner data or credentials."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent
files = [ROOT/name for name in ['app.py', 'launcher.py', 'START.cmd', 'IMPORT_DATA.cmd', 'README.md', 'requirements.txt',
                               '.gitignore', '.env.example', 'package_solution.py', 'data/README.md']]
for directory in ['replenishment', 'static', 'tests', 'templates']:
    files.extend(p for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
out = ROOT/'outputs/StockPilot_MVP.zip'
out.parent.mkdir(exist_ok=True)
with ZipFile(out, 'w', ZIP_DEFLATED) as archive:
    for path in sorted(files):
        archive.write(path, 'StockPilot/'+path.relative_to(ROOT).as_posix())
print(f'{out}: {len(files)} files, {out.stat().st_size} bytes')
