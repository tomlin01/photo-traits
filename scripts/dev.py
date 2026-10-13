"""Offline, repeatable project setup and validation (standard library only)."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def preflight():
    if sys.version_info < (3, 12):
        raise ValueError('Python 3.12 or newer is required')
    for name in ['docs/source.json', 'docs/fields.json']:
        json.loads((ROOT / name).read_text(encoding='utf-8'))
    # Check names only; never open credentials or print their contents.
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    if any(n and (Path(n).name == '.env' or Path(n).name.startswith('.env.') or
                  (n.startswith(('data/raw/', 'data/derived/')) and not n.endswith('/.gitkeep')))
           for n in tracked):
        raise ValueError('Tracked credentials/data path detected; review Git index locally')
    probes = ['.env', '.env.local', '.venv/probe', 'data/raw/probe', 'data/derived/probe']
    result = subprocess.run(['git', 'check-ignore', '--no-index', '--stdin'], cwd=ROOT,
                            input='\n'.join(probes)+'\n', text=True, capture_output=True)
    if set(result.stdout.splitlines()) != set(probes):
        raise ValueError('Required credential/data ignore rules are missing')


def initialize():
    preflight()
    for directory in ['data/raw', 'data/derived']:
        path = ROOT / directory
        if path.is_symlink():
            raise ValueError('Data directories must not be symlinks')
        path.mkdir(parents=True, exist_ok=True)
    print('Init OK: Python, source config and Git ignore rules checked; data directories ready.')


def check():
    preflight()
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                   cwd=ROOT, check=True)
    # No fetch and no output files: verify existing pinned data in memory.
    from metadata import SOURCE, digest, inspect
    source = ROOT / 'data/raw' / SOURCE['metadata']['filename']
    if source.exists():
        with source.open('rb') as stream:
            data = stream.read(SOURCE['metadata']['max_bytes'] + 1)
        if len(data) > SOURCE['metadata']['max_bytes'] or digest(data, 'md5') != SOURCE['metadata']['md5']:
            raise ValueError('Local metadata exceeds limit or differs from pinned checksum')
        report, _, tables = inspect(data, 10)
        if report['errors']:
            raise ValueError('Local metadata has validation errors; run inspect to a new output directory')
        for table in tables:
            if any({'lat', 'lon', 'trapID', 'area'} & row.keys() for row in table):
                raise ValueError('Sensitive columns found in sample exports')
        print(json.dumps({'metadata': 'verified', 'image_rows': report['image_rows'],
                          'observations': report['observations'], 'warnings': report['warnings']}))
    else:
        print('Metadata SKIP: no local raw file; no network request made.')
    subprocess.run(['git', 'diff', '--check'], cwd=ROOT, check=True)
    print('Check OK (offline).')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'check'])
    args = parser.parse_args()
    try:
        initialize() if args.command == 'init' else check()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Development check failed: {error}\n')


if __name__ == '__main__':
    main()
