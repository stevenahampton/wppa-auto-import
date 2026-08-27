# Photo Journey: Phone to WPPA

This describes the current path for a photo or video taken on the phone and eventually displayed in a WPPA album.

## 1. Phone upload to OneDrive

The phone first uploads the original file to:

```text
Pictures/Eigene Aufnahmen/<filename>
```

For example:

```text
Pictures/Eigene Aufnahmen/IMG_20260823_123616.jpg
```

The original remains full resolution in OneDrive.

## 2. Photo Manager moves the file into an album

The `/opt/photo-manager` app uses the Microsoft Graph API to move the file from `Pictures/Eigene Aufnahmen` into a folder below:

```text
Pictures/Albums
```

The current default destination is:

```text
Pictures/Albums/2026/<filename>
```

A more specific album might be:

```text
Pictures/Albums/2026 - Milford Track/<filename>
```

This is an in-OneDrive move. The app does not resize the OneDrive original. It can also write descriptions, upload to Google Photos, or delete files when those actions are selected.

## 3. Nightly rclone sync to the server

The `rclone-backup.timer` starts the OneDrive sync at approximately 03:00:

```text
onedrive: -> /mnt/1tb/onedrive
```

The file then exists locally at, for example:

```text
/mnt/1tb/onedrive/Pictures/Albums/2026/IMG_20260823_123616.jpg
```

The OneDrive Personal Vault is excluded because it can be locked and cause listing errors.

The OneDrive sync must finish successfully before the import stage is triggered. This prevents a partial or stale OneDrive mirror from being imported.

## 4. WPPA import detection

After a successful OneDrive sync, `wppa-nightly-import.service` runs:

```text
/opt/wppa-auto-import/nightly-import-new-media.py
```

The detector compares each album folder's filename, file size, and modification-time inventory against:

```text
/opt/wppa-auto-import/nightly-import.state.json
```

Unchanged folders are skipped. Changed folders are passed to:

```text
/opt/wppa-auto-import/direct-import-album.py
```

The importer:

- Finds or creates the matching WPPA album.
- Places new albums below `Photo Albums 2015 onwards`.
- Creates a WPPA photo row.
- Stores the original filename in the `filename` column.
- Extracts EXIF date/time and GPS metadata.
- Writes descriptions and EXIF rows.
- Creates the resized display image.
- Creates the small thumbnail/poster image.
- Stores the media dimensions in WPPA.
- Applies the configured album and photo status.

The current importer avoids duplicating dates when an image's EXIF description is `w#exiftaken`. WPPA expands that marker to the EXIF date, so the importer stores only the marker rather than appending the same date again.

## 5. Image storage and display

WPPA's display directory is:

```text
/var/www/wordpress/wp-content/uploads/wppa
```

That directory is itself a symlink to:

```text
/mnt/1tb/wppa
```

For an image, WPPA keeps the resized display copy, for example:

```text
/mnt/1tb/wppa/18632.jpg
```

The current display limits are approximately 1920 pixels wide by 1440 pixels high. These copies are used for normal galleries and lightboxes so browsing does not need to load the full archival original.

## 6. Image original/download path

WPPA can use a separate source path for the original download. The configured settings are:

```text
wppa_keep_source = no
wppa_keep_sync = no
wppa_download_album_source = yes
```

Because `wppa_keep_source` is disabled, WPPA does not create a second source-file copy. Instead, the hourly link job creates a symlink such as:

```text
/mnt/1tb/wppa-source/album-257/IMG_20260823_123616.jpg
  -> /mnt/1tb/onedrive/Pictures/Albums/2026/IMG_20260823_123616.jpg
```

The two paths therefore have different jobs:

```text
Display:
/mnt/1tb/wppa/<photo-id>.jpg

Original/download:
/mnt/1tb/wppa-source/album-<album-id>/<original-filename>
```

The display image stays small and fast. A download-original action follows the source symlink to the full-resolution OneDrive file. OneDrive remains the only real original-file store.

## 7. Video storage and display

WPPA identifies videos with `ext = 'xxx'`. A video normally has:

```text
/mnt/1tb/wppa/<photo-id>.mp4
/mnt/1tb/wppa/<photo-id>.jpg
```

The JPG is the local poster image. The MP4 is the video payload.

The hourly link job replaces the duplicate MP4 with a symlink, for example:

```text
/mnt/1tb/wppa/18634.mp4
  -> /mnt/1tb/onedrive/Pictures/Albums/2026/VID_20260822_154801.mp4
```

The poster remains local, while the video itself streams from the OneDrive mirror. This removes the duplicate video payload without changing the WPPA video record.

## 8. Hourly OneDrive link sync

The user cron job runs hourly:

```cron
0 * * * * /usr/bin/php /var/www/wordpress/bin/sync-wppa-onedrive.php >> /var/www/wordpress/bin/sync-wppa-onedrive.log 2>&1
```

The script scans albums below album 12, `Photo Albums 2015 onwards`, and:

- Creates image source symlinks when the matching OneDrive original exists.
- Replaces duplicate video payloads with symlinks when the matching OneDrive original exists.
- Leaves unmatched files untouched.
- Preserves conflicting regular source files rather than overwriting them.
- Uses a lock file to prevent overlapping runs.

## 9. Router backup

The router backup is intended to run independently after the OneDrive sync:

```text
/mnt/1tb/onedrive/Pictures -> /mnt/router_g/Media/Pictures
/mnt/1tb/onedrive/Videos   -> /mnt/router_g/Media/Videos
/mnt/1tb/onedrive/Music    -> /mnt/router_g/Media/Music
```

Router permission or filesystem errors should not prevent the WPPA import from running, nor should they obscure whether the OneDrive sync itself completed successfully.

## Overall flow

```text
Phone
  -> OneDrive/Pictures/Eigene Aufnahmen
  -> Photo Manager moves file to OneDrive/Pictures/Albums/<album>
  -> nightly rclone sync copies OneDrive to /mnt/1tb/onedrive
  -> successful OneDrive sync triggers WPPA import
  -> WPPA creates resized display image, thumbnail, and database metadata
  -> hourly link job points the original/download path at OneDrive
  -> video duplicate payloads are replaced with OneDrive symlinks
  -> router backup runs independently
```

## Timing

A newly moved file normally becomes available to WPPA after the next successful nightly OneDrive sync and import. The original/download symlink is then created by the hourly link job. The router backup has its own success/failure status and is not part of the WPPA media-processing path.
