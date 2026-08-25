#!/usr/bin/env python3
import argparse
import fcntl
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(os.environ.get('WPPA_SOURCE_ROOT', '/mnt/1tb/onedrive/Pictures/Albums'))
IMPORTER = Path(os.environ.get('WPPA_IMPORTER', TOOL_ROOT / 'direct-import-album.py'))
STATE_FILE = Path(os.environ.get('WPPA_BATCH_STATE', TOOL_ROOT / 'public-albums.completed'))
LOCK_FILE = Path(os.environ.get('WPPA_BATCH_LOCK', TOOL_ROOT / 'public-albums.lock'))
WORDPRESS_ROOT = os.environ.get('WPPA_WP_ROOT', '/var/www/wordpress')
SUPPORTED_EXTS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp',
    '.mp4', '.m4v', '.ogv', '.webm', '.mov', '.avi', '.mkv', '.flv',
    '.mp3', '.wav', '.ogg', '.pdf',
}
ALREADY_COMPLETED = {
    '1990 - onwards - Highlights',
    '2016 - Wellington',
    '2025 - Iceland Faroe England',
}


def log(message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{timestamp}] {message}', flush=True)


def supported_count(folder):
    return sum(
        1 for item in folder.iterdir()
        if item.is_file() and item.suffix.lower() in SUPPORTED_EXTS
    )


def load_completed():
    completed = set(ALREADY_COMPLETED)
    if STATE_FILE.exists():
        completed.update(
            line.strip() for line in STATE_FILE.read_text(encoding='utf-8').splitlines()
            if line.strip()
        )
    return completed


def mark_completed(album_name):
    with STATE_FILE.open('a', encoding='utf-8') as state:
        state.write(f'{album_name}\n')
        state.flush()


def album_folders():
    folders = []
    for folder in SRC_ROOT.iterdir():
        if not folder.is_dir():
            continue
        count = supported_count(folder)
        if count:
            folders.append((folder, count))
    return sorted(folders, key=lambda item: item[0].name.casefold())


def main():
    parser = argparse.ArgumentParser(
        description='Import all OneDrive album folders into WPPA with resumable progress.'
    )
    parser.add_argument(
        '--retry-completed', action='store_true',
        help='Process albums already listed in the completion journal.',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Print the queue without importing.',
    )
    parser.add_argument(
        '--new-album-status', choices=['publish', 'private', 'hidden'],
        help='Status applied to albums created by this run.',
    )
    parser.add_argument(
        '--skip-existing', action='store_true',
        help='Skip metadata/thumbnail refresh of already-imported photos.',
    )
    args = parser.parse_args()

    LOCK_FILE.touch(exist_ok=True)
    lock_handle = LOCK_FILE.open('r+')
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another public album import is already running.')

    completed = load_completed()
    folders = album_folders()
    queue = [
        (folder, count) for folder, count in folders
        if args.retry_completed or folder.name not in completed
    ]

    log(
        f'Found {len(folders)} folders with supported media; '
        f'{len(queue)} queued and {len(folders) - len(queue)} already completed.'
    )
    for index, (folder, count) in enumerate(queue, 1):
        log(f'QUEUE {index}/{len(queue)}: {folder.name} ({count} supported files)')

    if args.dry_run:
        return 0

    importer_options = []
    if args.new_album_status:
        importer_options += ['--new-album-status', args.new_album_status]
    if args.skip_existing:
        importer_options.append('--skip-existing')

    failures = []
    for index, (folder, count) in enumerate(queue, 1):
        log(f'START {index}/{len(queue)}: {folder.name} ({count} files)')
        result = subprocess.run(
            [sys.executable, str(IMPORTER), folder.name, *importer_options],
            cwd=WORDPRESS_ROOT,
        )
        if result.returncode == 0:
            mark_completed(folder.name)
            log(f'DONE  {index}/{len(queue)}: {folder.name}')
        else:
            failures.append((folder.name, result.returncode))
            log(f'FAIL  {index}/{len(queue)}: {folder.name} (exit {result.returncode})')

    if failures:
        log(f'Finished with {len(failures)} failure(s):')
        for album_name, returncode in failures:
            log(f'  {album_name}: exit {returncode}')
        return 1

    log('All queued albums completed successfully.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
