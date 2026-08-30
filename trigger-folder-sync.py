#!/usr/bin/env python3
"""Trigger OneDrive sync and WPPA import for specific album folders.

Called by photo-manager after uploading photos. Accepts a list of folder names
(e.g., "2026 - Germany", "2026 - Iceland"), syncs those exact folders from 
OneDrive via rclone, then triggers WPPA direct import to update the albums 
with new media.

Usage:
    trigger-folder-sync.py --folders "2026 - Germany" "2026 - Iceland"
    trigger-folder-sync.py --folders 2026/Iceland 2025/Travel
"""
import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger(__name__)

# Configuration from environment or defaults
SRC_ROOT = Path(os.environ.get('WPPA_SOURCE_ROOT', '/mnt/1tb/onedrive/Pictures/Albums'))
ONEDRIVE_PATH = os.environ.get('ONEDRIVE_PATH', 'onedrive:/Pictures/Albums')
IMPORTER = Path(os.environ.get(
    'WPPA_IMPORTER',
    Path(__file__).parent / 'nightly-import-new-media.py'
))
RCLONE_LOG = Path(os.environ.get('RCLONE_LOG', '/opt/wppa-auto-import/logs/rclone-photo-manager-sync.log'))
PYTHON = sys.executable


def run_rclone_sync(folder_names: list[str]) -> bool:
    """Sync specified folders from OneDrive using rclone."""
    if not folder_names:
        log.warning("No folders provided; skipping rclone sync")
        return True
    
    # Build rclone filter to include only the specified folders
    # Include exact folder matches and exclude everything else
    include_filters = []
    for folder in folder_names:
        # Escape special regex characters and match exact folder or subfolder content
        folder_clean = folder.rstrip("/")
        include_filters.append(f"+ /{folder_clean}/**")
    include_filters.append("- **")  # Exclude everything else
    
    cmd = [
        "rclone",
        "sync",
        ONEDRIVE_PATH,
        str(SRC_ROOT),
    ]
    
    # Add all include/exclude filters
    for filter_expr in include_filters:
        cmd.extend(["--filter", filter_expr])
    
    cmd.extend([
        f"--log-file={RCLONE_LOG}",
        "--log-level=INFO",
    ])
    
    log.info("Running rclone sync for folders: %s", ", ".join(folder_names))
    log.info("Command: %s", " ".join(cmd))
    
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=6*3600)
        log.info("Rclone sync completed successfully")
        if result.stdout:
            log.debug("Rclone stdout: %s", result.stdout)
        return True
    except subprocess.TimeoutExpired:
        log.error("Rclone sync timed out after 6 hours")
        return False
    except subprocess.CalledProcessError as e:
        log.error("Rclone sync failed with exit code %d", e.returncode)
        if e.stdout:
            log.error("Stdout: %s", e.stdout)
        if e.stderr:
            log.error("Stderr: %s", e.stderr)
        return False
    except Exception as e:
        log.error("Failed to run rclone: %s", e)
        return False


def run_wppa_import(folder_names: list[str]) -> bool:
    """Trigger WPPA direct import for the specified folders."""
    if not folder_names or not IMPORTER.exists():
        log.warning("Cannot run WPPA import: folders=%s, importer_exists=%s",
                   folder_names, IMPORTER.exists())
        return True
    
    # Run the importer for each folder
    for folder in folder_names:
        cmd = [
            PYTHON,
            str(IMPORTER),
            "--folder-prefix", folder,
        ]
        
        log.info("Running WPPA import for folder: %s", folder)
        log.info("Command: %s", " ".join(cmd))
        
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=6*3600)
            log.info("WPPA import for %s completed successfully", folder)
            if result.stdout:
                log.debug("Importer stdout: %s", result.stdout[:500])
        except subprocess.TimeoutExpired:
            log.error("WPPA import for %s timed out after 6 hours", folder)
            return False
        except subprocess.CalledProcessError as e:
            log.error("WPPA import for %s failed with exit code %d", folder, e.returncode)
            if e.stdout:
                log.error("Stdout: %s", e.stdout[:500])
            if e.stderr:
                log.error("Stderr: %s", e.stderr[:500])
            return False
        except Exception as e:
            log.error("Failed to run WPPA import for %s: %s", folder, e)
            return False
    
    return True


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--folders',
        nargs='+',
        help='Folder names to sync and import (e.g., "2026 - Germany" "2026 - Iceland")',
    )
    parser.add_argument(
        '--skip-sync',
        action='store_true',
        help='Skip rclone sync and only run WPPA import',
    )
    parser.add_argument(
        '--skip-import',
        action='store_true',
        help='Skip WPPA import and only run rclone sync',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Log what would be done without executing',
    )
    
    args = parser.parse_args()
    
    folders = args.folders or []
    
    if not folders:
        log.error("No folders provided; use --folders")
        return 1
    
    # Deduplicate and sort
    folders = sorted(set(folders))
    log.info("Starting sync/import for folders: %s", ", ".join(folders))
    
    if args.dry_run:
        log.info("[DRY RUN] Would sync and import: %s", ", ".join(folders))
        return 0
    
    # Run rclone sync if not skipped
    if not args.skip_sync:
        success = run_rclone_sync(folders)
        if not success:
            log.error("Rclone sync failed; aborting")
            return 1
    
    # Run WPPA import if not skipped
    if not args.skip_import:
        success = run_wppa_import(folders)
        if not success:
            log.error("WPPA import failed")
            return 1
    
    log.info("Sync and import completed successfully")
    return 0


if __name__ == '__main__':
    sys.exit(main())
