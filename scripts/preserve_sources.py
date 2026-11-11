"""Read-only file manifests for the two original trees; never import their code."""
import argparse
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parents[1] / 'docs' / 'preservation'
SOURCES = {'backend': ROOT / 'backend' / 'woowangthon-be-main', 'agent': ROOT / 'agent'}

def manifest():
    records = {}
    for label, source in SOURCES.items():
        for path in sorted(source.rglob('*')):
            if path.is_file():
                records[f'{label}/{path.relative_to(source).as_posix()}'] = {
                    'bytes': path.stat().st_size,
                    'sha256': hashlib.file_digest(path.open('rb'), 'sha256').hexdigest(),
                }
    return records

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['before', 'after'])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    destination = OUT / f'{args.phase}.json'
    if args.phase == 'before' and destination.exists():
        raise SystemExit('Baseline already exists; refusing to replace it.')
    files = manifest()
    destination.write_text(json.dumps({'recordedAt': datetime.now(timezone.utc).isoformat(), 'files': files}, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.phase == 'after':
        before = json.loads((OUT / 'before.json').read_text(encoding='utf-8'))['files']
        report = {'unchanged': before == files, 'beforeCount': len(before), 'afterCount': len(files),
                  'added': sorted(files.keys() - before.keys()), 'removed': sorted(before.keys() - files.keys()),
                  'modified': sorted(k for k in before.keys() & files.keys() if before[k] != files[k])}
        (OUT / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
        if not report['unchanged']:
            raise SystemExit(1)
    else:
        print(f'Baseline recorded: {len(files)} files; contents are not copied or displayed.')

if __name__ == '__main__':
    main()
