# WPPA Auto Import

Tools for importing local OneDrive album folders directly into [WP Photo Album Plus (WPPA+)](https://wordpress.org/plugins/wp-photo-album-plus/) and enriching imported records from OneDrive shared-album descriptions.

The importer bypasses WPPA's browser uploader. It writes WPPA database rows and managed media files directly, so **back up WordPress and its database before use**.

## Features

- Creates or refreshes one WPPA album per source folder.
- Keeps albums under a configurable parent album.
- Imports photos and videos using WPPA's `ext=xxx` multimedia convention.
- Applies per-item visibility from the `wppa_status` tag written by the photo manager.
- Generates full-size images, video posters and high-resolution thumbnail assets.
- Preserves/imports standard EXIF, GPS, Panasonic maker notes and useful video metadata.
- Stores description/date separators as `@@BR@@` for reliable frontend line breaks.
- Orders albums by capture time ascending.
- Supports resumable batch imports.
- Imports newly synced media nightly via a systemd service chained to the OneDrive sync.
- Scrapes descriptions from a shared OneDrive album with Playwright and updates only the matching WPPA album.
- Includes an optional update-safe WordPress MU plugin for custom EXIF labels and thumbnail overlays.

## Requirements

- Linux with Python 3.11+
- WordPress with WP Photo Album Plus
- MySQL/MariaDB command-line client
- `ffmpeg`
- `exiftool`
- Google Chrome or Chromium
- Node.js 18+

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
npm install
```

Frontend integration is maintained separately in the WordPress site's
`wp-content/mu-plugins/site-tweaks.php` file and is versioned in `~/config`.

## Configuration

Runtime defaults match the original deployment. Override them with environment variables as needed:

```bash
export WPPA_WP_ROOT=/var/www/wordpress
export WPPA_SOURCE_ROOT=/mnt/1tb/onedrive/Pictures/Albums
export WPPA_PARENT_ALBUM_ID=12
export WPPA_PARENT_ALBUM_NAME='Photo Albums 2015 onwards'
export WPPA_OWNER=wordpress-user-login
export WPPA_CHROME_PATH=/usr/bin/google-chrome
export WPPA_NEW_ALBUM_STATUS=publish
export WPPA_NIGHTLY_NEW_ALBUM_STATUS=hidden
```

Database settings and the WordPress table prefix are read from `WPPA_WP_ROOT/wp-config.php`. They may instead be supplied as `WPPA_DB_HOST`, `WPPA_DB_NAME`, `WPPA_DB_USER`, `WPPA_DB_PASSWORD` and `WPPA_TABLE_PREFIX`.

See [.env.example](.env.example) for the full list.

## Import One Album

```bash
./direct-import-album.py '2025 - Iceland Faroe England'
```

The command is idempotent. Existing records are matched by exact filename and then by a normalized, media-type-aware key. Reruns refresh metadata, descriptions and thumbnails without duplicating matched items.

Options:

- `--new-album-status publish|private|hidden`: status given to the album if this run creates it. Existing albums keep their current status. Defaults to `WPPA_NEW_ALBUM_STATUS`, or `publish`.
- `--skip-existing`: skip the metadata, description and thumbnail refresh of already-imported photos. Photo status is still synced.

## Photo Visibility (`wppa_status`)

The photo manager that triages OneDrive uploads embeds a custom XMP tag, `XMP-pm:wppa_status`, in each file. The importer reads it and writes it to the `status` column of `<prefix>wppa_photos`:

| `wppa_status` | Photo status |
| --- | --- |
| `publish` | `publish` |
| `private` | `private` |
| `hidden` | `hidden` |
| absent or unrecognized | `publish` |

Visibility is therefore per photo, not per folder: a year folder such as `2026` holds both public and non-public items. Reruns re-read the tag and update the stored status, so retriaging a file in the photo manager takes effect on the next import.

Read the tag from a file with:

```bash
exiftool -s3 -XMP-pm:wppa_status '/mnt/1tb/onedrive/Pictures/Albums/2026/IMG_0001.jpg'
```

## Batch Import

Preview the queue:

```bash
./batch-import-public-albums.py --dry-run
```

Run it in the foreground:

```bash
./batch-import-public-albums.py
```

Run it detached:

```bash
log="all-albums-$(date +%Y%m%d-%H%M%S).log"
nohup ./batch-import-public-albums.py > "$log" 2>&1 &
echo "PID=$! LOG=$log"
```

Successful albums are recorded in `public-albums.completed`. Rerunning skips completed albums. Use `--retry-completed` to refresh all folders again. `--new-album-status` and `--skip-existing` are passed through to the importer.

## Nightly Incremental Import

`nightly-import-new-media.py` scans `WPPA_SOURCE_ROOT` for folders whose media inventory changed since the last run and imports only those:

- A folder matching an existing WPPA album gets its new files added to that album.
- A folder with no matching album is created as a new album with status `hidden` (`WPPA_NIGHTLY_NEW_ALBUM_STATUS`), so it can be reviewed before publishing.
- Already-imported photos are not re-processed, but their status is re-synced from `wppa_status`.

Per-folder inventories (name, size, mtime) are journalled in `nightly-import.state.json`. A lock file prevents overlapping runs.

```bash
./nightly-import-new-media.py --dry-run      # report pending work only
./nightly-import-new-media.py                # import
./nightly-import-new-media.py --full-refresh # also refresh existing photos
./nightly-import-new-media.py --reset-state  # adopt current tree as baseline
```

Run `--reset-state` once after the initial bulk import so the first scheduled run does not requeue every folder.

### Scheduling

Systemd units and the rclone drop-in are maintained separately in `~/config/systemd`.
The nightly import runs after `rclone-backup.service` finishes successfully, using an
`OnSuccess=` drop-in rather than a separate timer:

```bash
cd ~/config
sudo ./install.sh
```

The service is triggered by the sync, so it is not enabled against a target. Check it with:

```bash
systemctl start wppa-nightly-import.service
journalctl -u wppa-nightly-import.service -n 100
```

Adjust `User=`, `WorkingDirectory=` and paths in the unit if your deployment differs. The service user needs read access to `wp-config.php`, write access to the WPPA uploads directory and the MySQL client credentials it implies.

## Import OneDrive Descriptions

The scraper requires the shared OneDrive album URL and the exact WPPA album name. Album scoping prevents a repeated filename from matching a record in another album.

Always audit first:

```bash
./scrape-onedrive-descriptions.js \
  'https://1drv.ms/a/...' \
  '2025 - Iceland Faroe England'
```

Apply the audited changes:

```bash
./scrape-onedrive-descriptions.js --apply \
  'https://1drv.ms/a/...' \
  '2025 - Iceland Faroe England'
```

Useful options:

- `--limit N`: process only the first `N` viewer items.
- `--restart`: ignore the saved checkpoint.
- `--headed`: show Chrome for debugging.
- `--clear-empty`: clear prefixes when OneDrive has an empty description; empty descriptions are skipped by default.

The scraper:

1. Opens the first album item.
2. Opens OneDrive's details pane.
3. Reads `#__details-panel-title` and `#__description-input`.
4. Matches the filename only within the named WPPA album.
5. Replaces only the description prefix and preserves everything from `@@BR@@` onward.
6. Advances with OneDrive's **View next photo** button.
7. Checkpoints processed viewer URLs for safe resume.

## Notes

- Legacy folders containing `(private)` are imported like any other folder. New uploads are no longer split into `(private)` folders; use the `wppa_status` tag instead.
- Source sidecars and unsupported file types are ignored.
- Binary EXIF blobs, serial numbers, embedded previews and internal byte offsets are excluded.
- For videos with no usable timestamp, the importer may use a closely matching same-scene photo's timestamp. It does not use filesystem sync timestamps.
- Plugin updates do not overwrite the optional MU-plugin integration.
