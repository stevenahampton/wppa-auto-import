# WPPA Auto Import

Tools for importing local OneDrive album folders directly into [WP Photo Album Plus (WPPA+)](https://wordpress.org/plugins/wp-photo-album-plus/) and enriching imported records from OneDrive shared-album descriptions.

The importer bypasses WPPA's browser uploader. It writes WPPA database rows and managed media files directly, so **back up WordPress and its database before use**.

## Features

- Creates or refreshes one WPPA album per source folder.
- Keeps albums under a configurable parent album.
- Imports photos and videos using WPPA's `ext=xxx` multimedia convention.
- Generates full-size images, video posters and high-resolution thumbnail assets.
- Preserves/imports standard EXIF, GPS, Panasonic maker notes and useful video metadata.
- Stores description/date separators as `@@BR@@` for reliable frontend line breaks.
- Orders albums by capture time ascending.
- Supports resumable batch imports.
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

Install the optional frontend integration:

```bash
cp wordpress/wppa-auto-import-tweaks.php \
  /path/to/wordpress/wp-content/mu-plugins/
```

If an existing MU plugin already implements these display tweaks, merge the relevant functions instead of loading both.

## Configuration

Runtime defaults match the original deployment. Override them with environment variables as needed:

```bash
export WPPA_WP_ROOT=/var/www/wordpress
export WPPA_SOURCE_ROOT=/mnt/1tb/onedrive/Pictures/Albums
export WPPA_PARENT_ALBUM_ID=12
export WPPA_PARENT_ALBUM_NAME='Photo Albums 2015 onwards'
export WPPA_OWNER=wordpress-user-login
export WPPA_CHROME_PATH=/usr/bin/google-chrome
```

Database settings and the WordPress table prefix are read from `WPPA_WP_ROOT/wp-config.php`. They may instead be supplied as `WPPA_DB_HOST`, `WPPA_DB_NAME`, `WPPA_DB_USER`, `WPPA_DB_PASSWORD` and `WPPA_TABLE_PREFIX`.

See [.env.example](.env.example) for the full list.

## Import One Album

```bash
./direct-import-album.py '2025 - Iceland Faroe England'
```

The command is idempotent. Existing records are matched by exact filename and then by a normalized, media-type-aware key. Reruns refresh metadata, descriptions and thumbnails without duplicating matched items.

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

Successful albums are recorded in `public-albums.completed`. Rerunning skips completed albums. Use `--retry-completed` to refresh all folders again.

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

- Folders containing `(private)` are imported like any other folder. Organize visibility and hierarchy in WPPA afterward.
- Source sidecars and unsupported file types are ignored.
- Binary EXIF blobs, serial numbers, embedded previews and internal byte offsets are excluded.
- For videos with no usable timestamp, the importer may use a closely matching same-scene photo's timestamp. It does not use filesystem sync timestamps.
- Plugin updates do not overwrite the optional MU-plugin integration.
