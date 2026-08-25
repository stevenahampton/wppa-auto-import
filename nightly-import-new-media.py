#!/usr/bin/env python3
"""Import media that appeared in the OneDrive album tree since the last run.

Designed to run unattended after the nightly rclone sync. Folders whose file
inventory is unchanged are skipped entirely; changed folders are handed to the
direct importer, which creates the WPPA album when it does not exist yet.
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(os.environ.get('WPPA_SOURCE_ROOT', '/mnt/1tb/onedrive/Pictures/Albums'))
IMPORTER = Path(os.environ.get('WPPA_IMPORTER', TOOL_ROOT / 'direct-import-album.py'))
STATE_FILE = Path(os.environ.get('WPPA_NIGHTLY_STATE', TOOL_ROOT / 'nightly-import.state.json'))
LOCK_FILE = Path(os.environ.get('WPPA_NIGHTLY_LOCK', TOOL_ROOT / 'nightly-import.lock'))
WORDPRESS_ROOT = os.environ.get('WPPA_WP_ROOT', '/var/www/wordpress')
NEW_ALBUM_STATUS = os.environ.get('WPPA_NIGHTLY_NEW_ALBUM_STATUS', 'hidden')
SUPPORTED_EXTS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp',
    '.mp4', '.m4v', '.ogv', '.webm', '.mov', '.avi', '.mkv', '.flv',
    '.mp3', '.wav', '.ogg', '.pdf',
}


def log(message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{timestamp}] {message}', flush=True)


def folder_inventory(folder):
    inventory = {}
    for item in sorted(folder.iterdir()):
        if not item.is_file() or item.suffix.lower() not in SUPPORTED_EXTS:
            continue
        try:
            stat = item.stat()
        except OSError:
            continue
        inventory[item.name] = f'{stat.st_size}:{int(stat.st_mtime)}'
    return inventory


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        log(f'warning: unreadable state file {STATE_FILE}; treating all folders as new')
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state):
    tmp_path = STATE_FILE.with_suffix('.tmp')
    tmp_path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding='utf-8')
    tmp_path.replace(STATE_FILE)


def changed_folders(state):
    pending = []
    for folder in sorted(SRC_ROOT.iterdir(), key=lambda item: item.name.casefold()):
        if not folder.is_dir():
            continue
        inventory = folder_inventory(folder)
        if not inventory:
            continue
        known = state.get(folder.name)
        if known == inventory:
            continue
        added = sorted(set(inventory) - set(known or {}))
        pending.append((folder, inventory, added, known is None))
    return pending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Report what would be imported without touching WPPA.',
    )
    parser.add_argument(
        '--full-refresh', action='store_true',
        help='Refresh metadata and thumbnails of already-imported photos too.',
    )
    parser.add_argument(
        '--reset-state', action='store_true',
        help='Record the current inventory as the baseline without importing.',
    )
    args = parser.parse_args()

    if not SRC_ROOT.is_dir():
        raise SystemExit(f'Missing source root: {SRC_ROOT}')

    LOCK_FILE.touch(exist_ok=True)
    lock_handle = LOCK_FILE.open('r+')
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another nightly import is already running.')

    state = load_state()
    pending = changed_folders(state)

    if args.reset_state:
        for folder, inventory, _added, _is_new in pending:
            state[folder.name] = inventory
        save_state(state)
        log(f'Baseline recorded for {len(state)} folder(s); nothing imported.')
        return 0

    if not pending:
        log('No new media found.')
        return 0

    log(f'{len(pending)} folder(s) with new or changed media:')
    for folder, inventory, added, is_new in pending:
        kind = 'NEW FOLDER' if is_new else 'UPDATED'
        log(f'  {kind}: {folder.name} ({len(added)} new of {len(inventory)} files)')

    if args.dry_run:
        return 0

    failures = []
    for index, (folder, inventory, added, is_new) in enumerate(pending, 1):
        options = ['--new-album-status', NEW_ALBUM_STATUS]
        if not args.full_refresh:
            options.append('--skip-existing')
        log(f'START {index}/{len(pending)}: {folder.name}')
        result = subprocess.run(
            [sys.executable, str(IMPORTER), folder.name, *options],
            cwd=WORDPRESS_ROOT,
        )
        if result.returncode == 0:
            state[folder.name] = inventory
            save_state(state)
            log(f'DONE  {index}/{len(pending)}: {folder.name}')
        else:
            failures.append((folder.name, result.returncode))
            log(f'FAIL  {index}/{len(pending)}: {folder.name} (exit {result.returncode})')

    if failures:
        log(f'Finished with {len(failures)} failure(s):')
        for album_name, returncode in failures:
            log(f'  {album_name}: exit {returncode}')
        return 1

    log('All new media imported successfully.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
