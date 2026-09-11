#!/bin/bash
# Runs the nightly importer as www-data (owner of wp-content/uploads/wppa),
# so callers running as steven (e.g. photo-manager) can invoke it via sudo.
set -euo pipefail
exec /usr/bin/python3 /opt/wppa-auto-import/nightly-import-new-media.py "$@"
