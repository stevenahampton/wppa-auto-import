-- Speed up EXIF date-taken recent gallery queries.
-- Added on 2026-08-25.
ALTER TABLE wpdl_wppa_photos
  ADD INDEX idx_status_exifdtm_id (status(6), exifdtm(19), id);
