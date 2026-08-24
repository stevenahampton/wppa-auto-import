#!/usr/bin/env python3
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from difflib import get_close_matches
from fractions import Fraction
from pathlib import Path

from PIL import ExifTags, Image

BASE = Path(os.environ.get('WPPA_WP_ROOT', '/var/www/wordpress'))
UPLOADS = BASE / 'wp-content' / 'uploads' / 'wppa'
THUMBS = UPLOADS / 'thumbs'
SRC_ROOT = Path(os.environ.get('WPPA_SOURCE_ROOT', '/mnt/1tb/onedrive/Pictures/Albums'))
PARENT_ALBUM_ID = int(os.environ.get('WPPA_PARENT_ALBUM_ID', '12'))
PARENT_ALBUM_NAME = os.environ.get('WPPA_PARENT_ALBUM_NAME', 'Photo Albums 2015 onwards')
OWNER = os.environ.get('WPPA_OWNER', 'gullisland')
PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
VIDEO_EXTS = {'.mp4', '.ogv', '.webm', '.mov', '.avi', '.mkv', '.flv'}
AUDIO_EXTS = {'.mp3', '.wav', '.ogg'}
DOC_EXTS = {'.pdf'}
SUPPORTED_EXTS = PHOTO_EXTS | VIDEO_EXTS | AUDIO_EXTS | DOC_EXTS
FULLSIZE_W = 1920
FULLSIZE_H = 1440
THUMBSIZE = 90
THUMB_FILE_SIZE = 240
CUSTOM_EXIF_TAGS = None
CUSTOM_EXIF_LABELS = None


def wordpress_config():
    config = (BASE / 'wp-config.php').read_text(encoding='utf-8')

    def constant(name, default=''):
        match = re.search(
            rf"define\(\s*['\"]{name}['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*\)",
            config,
        )
        return match.group(1) if match else default

    prefix_match = re.search(r"\$table_prefix\s*=\s*['\"]([^'\"]+)['\"]", config)
    return {
        'host': os.environ.get('WPPA_DB_HOST', constant('DB_HOST', 'localhost')),
        'database': os.environ.get('WPPA_DB_NAME', constant('DB_NAME')),
        'user': os.environ.get('WPPA_DB_USER', constant('DB_USER')),
        'password': os.environ.get('WPPA_DB_PASSWORD', constant('DB_PASSWORD')),
        'prefix': os.environ.get('WPPA_TABLE_PREFIX', prefix_match.group(1) if prefix_match else 'wp_'),
    }


WP_CONFIG = wordpress_config()
DB = WP_CONFIG['database']
DB_HOST = WP_CONFIG['host']
DB_USER = WP_CONFIG['user']
DB_PASS = WP_CONFIG['password']
TABLE_PREFIX = WP_CONFIG['prefix']
ALBUMS_TABLE = f'{TABLE_PREFIX}wppa_albums'
PHOTOS_TABLE = f'{TABLE_PREFIX}wppa_photos'
EXIF_TABLE = f'{TABLE_PREFIX}wppa_exif'


def mysql_command(extra_args=None):
    return [
        'mysql', '--batch', '--raw', '-h', DB_HOST, '-u', DB_USER,
        *(extra_args or []), DB,
    ]


def mysql_environment():
    return {**os.environ, 'MYSQL_PWD': DB_PASS}


def mysql_rows(sql):
    out = subprocess.check_output(
        mysql_command(['--skip-column-names', '-e', sql]),
        env=mysql_environment(), text=True, stderr=subprocess.DEVNULL,
    )
    return [line.split('\t') for line in out.strip().splitlines() if line.strip()]


def mysql_scalar(sql):
    rows = mysql_rows(sql)
    return rows[0][0] if rows and rows[0] else ''


def mysql_exec(sql):
    subprocess.check_call(
        mysql_command(['-e', sql]),
        env=mysql_environment(), stderr=subprocess.DEVNULL,
    )


def mysql_exec_file(sql):
    with tempfile.NamedTemporaryFile('w', suffix='.sql', delete=False, encoding='utf-8') as fh:
        fh.write(sql)
        tmp_path = fh.name
    try:
        with open(tmp_path, 'r', encoding='utf-8') as sql_input:
            subprocess.check_call(
                mysql_command(), stdin=sql_input,
                env=mysql_environment(), stderr=subprocess.DEVNULL,
            )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def clean_text(value):
    return str(value).replace('\x00', '').strip()


def esc(s):
    return clean_text(s).replace('\\', '\\\\').replace("'", "\\'")


def slugify(text):
    text = text.strip().lower()
    out = []
    last_dash = False
    for ch in text:
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        else:
            if not last_dash:
                out.append('-')
                last_dash = True
    return ''.join(out).strip('-') or 'album'


def media_key(value):
    stem = Path(clean_text(value)).stem
    if Path(stem).suffix.lower() in SUPPORTED_EXTS | {'.xxx'}:
        stem = Path(stem).stem
    return ''.join(character.lower() for character in stem if character.isalnum())


def display_text(value):
    return clean_text(value).replace('0022', '"')


def is_original_filename(value):
    return bool(re.fullmatch(
        r'(?:\d{8,14}[_-])?(?:IMG|VID|MVI|MOV|DSC|PICT)[_-]?[A-Z0-9]{0,14}(?:[ _-]\d+)?|(?:\d{8,14}[_-])?P\d{4,8}|\d{10,14}(?:[ _-]\d+)?|copilot_image_\d{6,20}|[0-9a-f]{8,20}|[0-9a-f]{8}-[0-9a-f-]{27,}|image(?:[ _-]\d+)?|\d{8}_\d{6}(?:_[0-9a-f]{8,})?|\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}|\d{8}\(\d{3}\)',
        display_text(value),
        re.IGNORECASE,
    ))


def find_existing_photo(album_id, source_file):
    exact = mysql_scalar(
        f"SELECT id FROM {PHOTOS_TABLE} WHERE album={album_id} "
        f"AND filename='{esc(source_file.name)}' LIMIT 1"
    )
    if exact:
        return int(exact)

    source_key = media_key(source_file.name)
    def canonical_extension(value):
        extension = clean_text(value).lower().lstrip('.')
        return 'jpg' if extension in {'jpg', 'jpeg'} else extension

    source_is_video = source_file.suffix.lower() in VIDEO_EXTS
    source_extension = canonical_extension(source_file.suffix)
    for photo_id, name, filename, ext in mysql_rows(
        f"SELECT id,name,filename,ext FROM {PHOTOS_TABLE} WHERE album={album_id}"
    ):
        if source_is_video != (ext == 'xxx'):
            continue
        if not source_is_video and canonical_extension(ext) != source_extension:
            continue
        if source_key in {media_key(name), media_key(filename)}:
            return int(photo_id)
    return None


def random_crypt():
    import time, random
    seed = f'{time.time()}-{random.random()}-{os.getpid()}'.encode()
    return hashlib.md5(seed).hexdigest()[:16]


def custom_exif_tag(label):
    global CUSTOM_EXIF_TAGS, CUSTOM_EXIF_LABELS

    if CUSTOM_EXIF_TAGS is None:
        CUSTOM_EXIF_TAGS = {}
        CUSTOM_EXIF_LABELS = {}
        for tag, description in mysql_rows(
            f"SELECT tag,description FROM {EXIF_TABLE} WHERE photo=0 AND tag LIKE 'X#%'"
        ):
            stored_label = description.rstrip(':')
            CUSTOM_EXIF_TAGS[stored_label] = tag
            CUSTOM_EXIF_LABELS[tag] = stored_label

    if label in CUSTOM_EXIF_TAGS:
        return CUSTOM_EXIF_TAGS[label]

    attempt = 0
    while True:
        seed = label if attempt == 0 else f'{label}:{attempt}'
        tag = f"X#{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:4].upper()}"
        if tag not in CUSTOM_EXIF_LABELS or CUSTOM_EXIF_LABELS[tag] == label:
            CUSTOM_EXIF_TAGS[label] = tag
            CUSTOM_EXIF_LABELS[tag] = label
            return tag
        attempt += 1


def readable_exif_label(group, name):
    words = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', name).replace('_', ' ')
    words = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', words)
    prefixes = {
        'ExifIFD': '',
        'IFD0': '',
        'InteropIFD': 'Interop ',
        'Panasonic': 'Panasonic ',
        'GPS': 'GPS ',
    }
    prefix = prefixes.get(group, f'{group} ')
    if words.lower().startswith(group.lower()):
        prefix = ''
    return f'{prefix}{words}'.strip()


def unix_now():
    return str(int(datetime.now().timestamp()))


def sortable_datetime(path):
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if exif:
                raw_dt = exif.get(36867) or exif.get(306)
                if raw_dt:
                    return datetime.strptime(
                        str(raw_dt).strip(), '%Y:%m:%d %H:%M:%S'
                    ).strftime('%Y:%m:%d %H:%M:%S')
    except Exception:
        pass

    try:
        output = subprocess.check_output(
            ['exiftool', '-j', '-n', '-EXIF:DateTimeOriginal', str(path)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        raw_dt = json.loads(output)[0].get('DateTimeOriginal', '')
        if raw_dt:
            return datetime.strptime(
                str(raw_dt).strip(), '%Y:%m:%d %H:%M:%S'
            ).strftime('%Y:%m:%d %H:%M:%S')
    except (IndexError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        pass

    for width, date_format in (
        (14, '%Y%m%d%H%M%S'),
        (8, '%Y%m%d'),
        (6, '%Y%m'),
        (4, '%Y'),
    ):
        match = re.match(rf'^(\d{{{width}}})(?!\d)', path.stem)
        if not match:
            continue
        try:
            return datetime.strptime(
                match.group(1), date_format
            ).strftime('%Y:%m:%d %H:%M:%S')
        except ValueError:
            pass

    month_year = re.search(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(19\d{2}|20\d{2})\b',
        path.stem,
        re.IGNORECASE,
    )
    if month_year:
        return datetime.strptime(
            f'{month_year.group(1)} {month_year.group(2)}', '%B %Y'
        ).strftime('%Y:%m:%d %H:%M:%S')

    if path.suffix.lower() in VIDEO_EXTS:
        photo_stems = {
            candidate.stem: candidate
            for candidate in path.parent.iterdir()
            if candidate.is_file() and candidate.suffix.lower() in PHOTO_EXTS
        }
        matches = get_close_matches(path.stem, photo_stems, n=1, cutoff=0.45)
        if matches:
            return sortable_datetime(photo_stems[matches[0]])
    return ''


def friendly_dt_from_exif(path):
    raw_dt = sortable_datetime(path)
    if raw_dt:
        return datetime.strptime(raw_dt, '%Y:%m:%d %H:%M:%S').strftime('%a %d %b %Y %H:%M')
    return ''


def exif_description(path):
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return ''
            raw_desc = exif.get(270)
            return clean_text(raw_desc) if raw_desc else ''
    except Exception:
        return ''


def image_size(path):
    with Image.open(path) as img:
        return img.size


def resize_image(src, dst, max_w, max_h):
    with Image.open(src) as img:
        exif_bytes = img.getexif().tobytes()
        img.thumbnail((max_w, max_h))
        dst.parent.mkdir(parents=True, exist_ok=True)
        save_kwargs = {}
        if dst.suffix.lower() in {'.jpg', '.jpeg'}:
            save_kwargs['quality'] = 90
            if exif_bytes:
                save_kwargs['exif'] = exif_bytes
        img.save(dst, **save_kwargs)
        return img.size


def ensure_album(album_name):
    existing = mysql_scalar(
        f"SELECT id FROM {ALBUMS_TABLE} WHERE name='{esc(album_name)}' ORDER BY id DESC LIMIT 1"
    )
    if existing:
        album_id = int(existing)
        mysql_exec(
            f"UPDATE {ALBUMS_TABLE} SET a_parent={PARENT_ALBUM_ID},p_order_by=7 WHERE id={album_id}"
        )
        return album_id, False

    album_id = int(mysql_scalar(f"SELECT COALESCE(MAX(id),0)+1 FROM {ALBUMS_TABLE}"))
    sql = f"""
INSERT INTO {ALBUMS_TABLE}
(id,name,description,a_order,main_photo,a_parent,p_order_by,cover_linktype,cover_linkpage,owner,timestamp,modified,upload_limit,alt_thumbsize,default_tags,cover_type,suba_order_by,views,cats,scheduledtm,crypt,custom,treecounts,wmfile,wmpos,indexdtm,sname,zoomable,displayopts,upload_limit_tree,scheduledel,status,cover_link,max_children,rml_id,usedby,capability)
VALUES
({album_id},'{esc(album_name)}','',0,0,{PARENT_ALBUM_ID},7,'content',0,'{OWNER}',UNIX_TIMESTAMP(),UNIX_TIMESTAMP(),'0/0','0','','','',2,'','', '{random_crypt()}','', 'a:11:{{i:0;s:1:"0";i:1;s:1:"0";i:2;s:1:"0";i:3;s:1:"0";i:4;s:1:"0";i:5;s:1:"0";i:6;s:1:"0";i:7;s:1:"0";i:8;s:1:"0";i:9;s:1:"0";i:10;s:1:"0";}}','','',UNIX_TIMESTAMP(),'{slugify(album_name)}','','0,0,0,0','0','','publish','','0','','','');
"""
    mysql_exec_file(sql)
    return album_id, True


def next_photo_id():
    return int(mysql_scalar(f"SELECT COALESCE(MAX(id),0)+1 FROM {PHOTOS_TABLE}"))


def insert_photo(photo_id, album_id, ext, name, description, filename, exifdtm, photox=0, photoy=0, thumbx=0, thumby=0, videox=0, videoy=0, duration=''):
    description = description.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '@@BR@@')
    desc_expr = f"'{esc(description)}'"

    sql = f"""
INSERT INTO {PHOTOS_TABLE}
(id,album,ext,name,description,p_order,mean_rating,linkurl,linktitle,linktarget,owner,timestamp,status,rating_count,tags,alt,filename,modified,location,views,page_id,exifdtm,videox,videoy,thumbx,thumby,photox,photoy,scheduledtm,custom,stereo,crypt,clicks,magickstack,scheduledel,indexdtm,panorama,sname,dlcount,thumblock,duration,angle,rml_id,usedby,misc,sourcex,sourcey)
VALUES
({photo_id},{album_id},'{esc(ext)}','{esc(name)}',{desc_expr},0,'','','','_self','{OWNER}',UNIX_TIMESTAMP(),'publish',0,'','','{esc(filename)}',UNIX_TIMESTAMP(),'',0,0,'{esc(exifdtm)}',{videox},{videoy},{thumbx},{thumby},{photox},{photoy},'','',0,'{random_crypt()}',0,'','','',0,'{slugify(name)}',0,0,'{esc(duration)}',0,'','','',0,0);
"""
    mysql_exec_file(sql)


def insert_exif_rows(photo_id, source_file):
    if photo_id < 1:
        raise ValueError('photo_id must be a positive WPPA photo ID')

    requested_fields = [
        'EXIF:Make', 'EXIF:Model', 'EXIF:LensModel', 'EXIF:ExposureTime',
        'EXIF:FNumber', 'EXIF:ISO', 'EXIF:FocalLength',
        'EXIF:FocalLengthIn35mmFormat', 'EXIF:DateTimeOriginal',
        'EXIF:MeteringMode', 'EXIF:Flash', 'EXIF:WhiteBalance',
        'GPSLatitude', 'GPSLongitude',
    ]
    try:
        output = subprocess.check_output(
            ['exiftool', '-j', '-n', *(f'-{field}' for field in requested_fields), str(source_file)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        metadata = json.loads(output)[0]
    except (IndexError, TypeError, ValueError, subprocess.SubprocessError):
        metadata = {}

    grouped_metadata = {}
    try:
        output = subprocess.check_output(
            ['exiftool', '-j', '-G1', '-a', '-s', str(source_file)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        grouped_metadata = json.loads(output)[0]
    except (IndexError, TypeError, ValueError, subprocess.SubprocessError):
        pass

    gps = None
    if source_file.suffix.lower() in {'.jpg', '.jpeg', '.tif', '.tiff'}:
        try:
            with Image.open(source_file) as img:
                gps = img.getexif().get_ifd(ExifTags.IFD.GPSInfo)
        except Exception:
            pass

    if not gps:
        try:
            latitude_decimal = float(metadata['GPSLatitude'])
            longitude_decimal = float(metadata['GPSLongitude'])

            def decimal_to_dms(value):
                absolute = abs(value)
                degrees = int(absolute)
                minutes_float = (absolute - degrees) * 60
                minutes = int(minutes_float)
                seconds = (minutes_float - minutes) * 60
                return degrees, minutes, seconds

            gps = {
                1: 'S' if latitude_decimal < 0 else 'N',
                2: decimal_to_dms(latitude_decimal),
                3: 'W' if longitude_decimal < 0 else 'E',
                4: decimal_to_dms(longitude_decimal),
            }
        except (KeyError, TypeError, ValueError):
            gps = None

    def rational(value):
        numerator = getattr(value, 'numerator', None)
        denominator = getattr(value, 'denominator', None)
        if numerator is None or denominator is None:
            fraction = Fraction(str(float(value))).limit_denominator(1000000)
            numerator, denominator = fraction.numerator, fraction.denominator
        if not denominator:
            raise ValueError('Invalid zero-denominator rational')
        return f'{numerator}/{denominator}'

    def php_serialize(values):
        parts = []
        for index, value in enumerate(values):
            encoded = rational(value)
            parts.append(f'i:{index};s:{len(encoded)}:"{encoded}";')
        return f'a:{len(parts)}:{{{"".join(parts)}}}'

    def decimal_degrees(values, ref):
        decimal = float(values[0]) + float(values[1]) / 60 + float(values[2]) / 3600
        return -decimal if ref in {'S', 'W'} else decimal

    def formatted_degrees(values, ref):
        return (
            f'{ref} {int(float(values[0]))}&deg;'
            f'{int(float(values[1]))}&#x27;{float(values[2]):.4f}&#x22;'
        )

    labels = {
        'E#010F': 'Camera make:',
        'E#0110': 'Camera model:',
        'E#829A': 'Shutter speed:',
        'E#829D': 'Aperture:',
        'E#8827': 'ISO:',
        'E#9003': 'Date taken:',
        'E#9207': 'Metering mode:',
        'E#9209': 'Flash:',
        'E#920A': 'Focal length:',
        'E#A403': 'White balance:',
        'E#A405': '35mm focal length:',
        'E#A434': 'Lens:',
    }
    rows = {}

    direct_fields = {
        'Make': 'E#010F',
        'Model': 'E#0110',
        'LensModel': 'E#A434',
        'ISO': 'E#8827',
        'DateTimeOriginal': 'E#9003',
    }
    for field, tag in direct_fields.items():
        value = metadata.get(field)
        if value not in (None, ''):
            rows[tag] = (clean_text(value), clean_text(value))

    numeric_fields = {
        'ExposureTime': ('E#829A', lambda value: f'{rational(value)} s.'),
        'FNumber': ('E#829D', lambda value: f'f/{float(value):g}'),
        'FocalLength': ('E#920A', lambda value: f'{float(value):g} mm.'),
    }
    for field, (tag, formatter) in numeric_fields.items():
        value = metadata.get(field)
        if value not in (None, ''):
            try:
                rows[tag] = (rational(value), formatter(value))
            except (TypeError, ValueError, ZeroDivisionError):
                pass

    focal_35mm = metadata.get('FocalLengthIn35mmFormat')
    if focal_35mm not in (None, ''):
        try:
            rows['E#A405'] = (
                str(int(round(float(focal_35mm)))),
                f'{float(focal_35mm):g} mm.',
            )
        except (TypeError, ValueError):
            pass

    metering_modes = {
        0: 'Unknown', 1: 'Average', 2: 'Center-weighted average', 3: 'Spot',
        4: 'Multi-spot', 5: 'Multi-segment', 6: 'Partial', 255: 'Other',
    }
    metering = metadata.get('MeteringMode')
    if metering not in (None, ''):
        try:
            rows['E#9207'] = (str(int(metering)), metering_modes.get(int(metering), 'Unknown'))
        except (TypeError, ValueError):
            pass

    flash_modes = {
        0: 'No Flash', 1: 'Fired', 5: 'Fired, Return not detected',
        7: 'Fired, Return detected', 16: 'Off, Did not fire',
        24: 'Auto, Did not fire', 25: 'Auto, Fired',
    }
    flash = metadata.get('Flash')
    if flash not in (None, ''):
        try:
            rows['E#9209'] = (str(int(flash)), flash_modes.get(int(flash), 'Unknown'))
        except (TypeError, ValueError):
            pass

    white_balance = metadata.get('WhiteBalance')
    if white_balance not in (None, ''):
        try:
            rows['E#A403'] = (
                str(int(white_balance)),
                'Auto' if int(white_balance) == 0 else 'Manual',
            )
        except (TypeError, ValueError):
            pass

    standard_fields = {
        'IFD0:Make', 'IFD0:Model', 'ExifIFD:LensModel',
        'ExifIFD:ExposureTime', 'ExifIFD:FNumber', 'ExifIFD:ISO',
        'ExifIFD:FocalLength', 'ExifIFD:FocalLengthIn35mmFormat',
        'ExifIFD:DateTimeOriginal', 'ExifIFD:MeteringMode', 'ExifIFD:Flash',
        'ExifIFD:WhiteBalance', 'GPS:GPSLatitudeRef', 'GPS:GPSLatitude',
        'GPS:GPSLongitudeRef', 'GPS:GPSLongitude',
        'Composite:GPSLatitude', 'Composite:GPSLongitude',
        'Composite:GPSPosition', 'UserData:GPSCoordinates',
    }
    excluded_names = {
        'Artist', 'DataDump', 'InternalSerialNumber', 'SerialNumber',
        'ThumbnailImage', 'PreviewImage', 'OtherImage',
    }
    excluded_fragments = ('Offset', 'Length', 'Binary data')
    if source_file.suffix.lower() in VIDEO_EXTS:
        allowed_groups = {
            'File', 'QuickTime', 'UserData', 'Keys', 'Track1', 'Track2',
            'Track3', 'Track4', 'Composite',
        }
    else:
        allowed_groups = {'IFD0', 'ExifIFD', 'InteropIFD', 'Panasonic', 'GPS'}
    for grouped_name, value in grouped_metadata.items():
        if ':' not in grouped_name or grouped_name in standard_fields:
            continue
        group, name = grouped_name.split(':', 1)
        if group not in allowed_groups or name in excluded_names:
            continue
        if any(fragment in name for fragment in excluded_fragments):
            continue
        if isinstance(value, dict) or value in (None, '', '---', '(not set)', 'n/a'):
            continue
        if isinstance(value, list):
            value = ', '.join(clean_text(item) for item in value)
        display_value = clean_text(value)
        if not display_value or 'Binary data' in display_value or len(display_value) > 255:
            continue
        label = readable_exif_label(group, name)
        tag = custom_exif_tag(label)
        labels[tag] = f'{label}:'
        rows[tag] = (display_value, display_value)

    location = None
    if gps and all(tag in gps for tag in (1, 2, 3, 4)):
        try:
            latitude_ref = clean_text(gps[1]).upper()
            longitude_ref = clean_text(gps[3]).upper()
            latitude = tuple(gps[2])
            longitude = tuple(gps[4])
            coordinates = [float(value) for value in (*latitude, *longitude)]
            if latitude_ref not in {'N', 'S'} or longitude_ref not in {'E', 'W'}:
                raise ValueError('Invalid GPS reference')
            if len(latitude) != 3 or len(longitude) != 3 or not all(math.isfinite(value) for value in coordinates):
                raise ValueError('Invalid GPS coordinates')
            latitude_formatted = formatted_degrees(latitude, latitude_ref)
            longitude_formatted = formatted_degrees(longitude, longitude_ref)
            latitude_decimal = decimal_degrees(latitude, latitude_ref)
            longitude_decimal = decimal_degrees(longitude, longitude_ref)
            location = (
                f'{latitude_formatted}/{longitude_formatted}/'
                f'{latitude_decimal:.7f}/{longitude_decimal:.7f}'
            )
            labels.update({
                'G#0001': 'GPSLatitudeRef:',
                'G#0002': 'GPSLatitude:',
                'G#0003': 'GPSLongitudeRef:',
                'G#0004': 'GPSLongitude:',
            })
            rows.update({
                'G#0001': (latitude_ref, 'North' if latitude_ref == 'N' else 'South'),
                'G#0002': (php_serialize(latitude), latitude_formatted[2:]),
                'G#0003': (longitude_ref, 'East' if longitude_ref == 'E' else 'West'),
                'G#0004': (php_serialize(longitude), longitude_formatted[2:]),
            })
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            gps = None

    if not rows:
        return False

    statements = ['START TRANSACTION;']
    statements.append(f"DELETE FROM {EXIF_TABLE} WHERE photo={photo_id} AND tag LIKE 'X#%';")
    imported_tags = ','.join(f"'{tag}'" for tag in rows)
    statements.append(
        f'DELETE FROM {EXIF_TABLE} WHERE photo={photo_id} AND tag IN ({imported_tags});'
    )
    for tag, label in labels.items():
        statements.append(
            f"INSERT INTO {EXIF_TABLE} (photo,tag,description,status,f_description,brand) "
            f"SELECT 0,'{tag}','{esc(label)}','display','','' WHERE NOT EXISTS "
            f"(SELECT 1 FROM {EXIF_TABLE} WHERE photo=0 AND tag='{tag}');"
        )
    for tag, (description, formatted) in rows.items():
        statements.append(
            f"INSERT INTO {EXIF_TABLE} (photo,tag,description,status,f_description,brand) VALUES "
            f"({photo_id},'{tag}','{esc(description)}','default','{esc(formatted)}','');"
        )
    if location:
        statements.append(
            f"UPDATE {PHOTOS_TABLE} SET location='{esc(location)}' WHERE id={photo_id};"
        )
    statements.append('COMMIT;')
    mysql_exec_file('\n'.join(statements))
    return True


def process_photo(photo_id, source_file, ext):
    managed_photo = UPLOADS / f'{photo_id}.{ext}'
    managed_thumb = THUMBS / f'{photo_id}.{ext}'
    photox = photoy = thumbx = thumby = 0
    try:
        photox, photoy = resize_image(source_file, managed_photo, FULLSIZE_W, FULLSIZE_H)
        resize_image(source_file, managed_thumb, THUMB_FILE_SIZE, THUMB_FILE_SIZE)
        with Image.open(source_file) as img:
            img.thumbnail((THUMBSIZE, THUMBSIZE))
            thumbx, thumby = img.size
    except Exception:
        shutil.copy2(source_file, managed_photo)
        managed_photo.parent.mkdir(parents=True, exist_ok=True)
        managed_thumb.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, managed_thumb)
        try:
            photox, photoy = image_size(managed_photo)
            thumbx, thumby = image_size(managed_thumb)
        except Exception:
            pass
    return photox, photoy, thumbx, thumby


def process_video(photo_id, source_file, ext):
    managed_video = UPLOADS / f'{photo_id}.{ext}'
    managed_poster = UPLOADS / f'{photo_id}.jpg'
    managed_thumb = THUMBS / f'{photo_id}.jpg'
    managed_video.parent.mkdir(parents=True, exist_ok=True)
    managed_poster.parent.mkdir(parents=True, exist_ok=True)
    managed_thumb.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_file, managed_video)

    videox = 1280
    videoy = 720
    duration = ''

    try:
        subprocess.check_call([
            'ffmpeg', '-y', '-i', str(source_file), '-ss', '00:00:01.000', '-vframes', '1', str(managed_poster)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with Image.open(managed_poster) as img:
            videox, videoy = img.size
        with Image.open(managed_poster) as img:
            thumb_w = min(THUMB_FILE_SIZE, img.width)
            thumb_h = min(THUMB_FILE_SIZE, img.height)
            img.thumbnail((thumb_w, thumb_h))
            img.save(managed_thumb, quality=95)
    except Exception:
        pass

    return videox, videoy, duration


def refresh_thumbnail(photo_id, source_file):
    managed_thumb = THUMBS / f'{photo_id}.jpg'
    try:
        if managed_thumb.exists():
            with Image.open(managed_thumb) as existing_thumb:
                if max(existing_thumb.size) >= THUMB_FILE_SIZE:
                    return
        if source_file.suffix.lower() in PHOTO_EXTS:
            resize_image(source_file, managed_thumb, THUMB_FILE_SIZE, THUMB_FILE_SIZE)
        elif source_file.suffix.lower() in VIDEO_EXTS:
            managed_poster = UPLOADS / f'{photo_id}.jpg'
            if not managed_poster.exists():
                subprocess.check_call([
                    'ffmpeg', '-y', '-i', str(source_file), '-ss', '00:00:01.000',
                    '-vframes', '1', str(managed_poster),
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            resize_image(managed_poster, managed_thumb, THUMB_FILE_SIZE, THUMB_FILE_SIZE)
        else:
            return

        with Image.open(managed_thumb) as img:
            logical = img.copy()
            logical.thumbnail((THUMBSIZE, THUMBSIZE))
            thumbx, thumby = logical.size
        mysql_exec(
            f'UPDATE {PHOTOS_TABLE} SET thumbx={thumbx},thumby={thumby} WHERE id={photo_id}'
        )
    except Exception as exc:
        print(f'warning: thumbnail refresh failed for {source_file.name}: {exc}', file=sys.stderr)


def refresh_default_description(photo_id, source_file):
    rows = mysql_rows(f'SELECT name,description FROM {PHOTOS_TABLE} WHERE id={photo_id}')
    if not rows:
        return
    current_name, current = rows[0]
    raw_stem = clean_text(source_file.stem)
    stem = display_text(raw_stem)
    friendly_date = friendly_dt_from_exif(source_file)
    current_base = current.split('@@BR@@', 1)[0]
    if media_key(current_name) == media_key(raw_stem) and current_name != stem:
        mysql_exec(f"UPDATE {PHOTOS_TABLE} SET name='{esc(stem)}',sname='{slugify(stem)}' WHERE id={photo_id}")
    if friendly_date and '@@BR@@' not in current:
        description_base = '' if is_original_filename(current_base) else (current or stem)
        description = f'{description_base}@@BR@@{friendly_date}' if description_base else 'w#exiftaken'
        mysql_exec(
            f"UPDATE {PHOTOS_TABLE} SET description='{esc(description)}' WHERE id={photo_id}"
        )


def refresh_sortable_datetime(photo_id, source_file):
    exifdtm = sortable_datetime(source_file)
    if exifdtm:
        mysql_exec(
            f"UPDATE {PHOTOS_TABLE} SET exifdtm='{esc(exifdtm)}' WHERE id={photo_id}"
        )


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: direct-import-album.py "Album Folder Name"')

    album_name = sys.argv[1]
    src_dir = SRC_ROOT / album_name
    if not src_dir.is_dir():
        raise SystemExit(f'Missing source album: {src_dir}')

    album_id, created = ensure_album(album_name)
    print(f'album_id={album_id} created={created}')

    media_files = sorted([p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS])
    if not media_files:
        raise SystemExit('No supported media files found')

    imported = 0
    skipped = 0

    for source_file in media_files:
        ext = source_file.suffix.lower().lstrip('.')
        stem = display_text(source_file.stem)
        existing = find_existing_photo(album_id, source_file)
        if existing:
            refresh_sortable_datetime(existing, source_file)
            refresh_default_description(existing, source_file)
            refresh_thumbnail(existing, source_file)
            insert_exif_rows(existing, source_file)
            skipped += 1
            continue

        exif_dt = friendly_dt_from_exif(source_file)
        exif_desc = exif_description(source_file)
        description_parts = [part for part in [exif_desc, exif_dt] if part]
        description = '\n'.join(description_parts)
        exifdtm = sortable_datetime(source_file)

        photo_id = next_photo_id()

        photox = photoy = thumbx = thumby = videox = videoy = 0
        duration = ''
        db_ext = ext
        if source_file.suffix.lower() in PHOTO_EXTS:
            photox, photoy, thumbx, thumby = process_photo(photo_id, source_file, ext)
        elif source_file.suffix.lower() in VIDEO_EXTS:
            videox, videoy, duration = process_video(photo_id, source_file, ext)
            db_ext = 'xxx'
        else:
            # Keep unsupported-but-listed types as plain managed files
            managed_photo = UPLOADS / f'{photo_id}.{ext}'
            managed_photo.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, managed_photo)

        insert_photo(
            photo_id=photo_id,
            album_id=album_id,
            ext=db_ext,
            name=stem,
            description=description,
            filename=source_file.name,
            exifdtm=exifdtm,
            photox=photox,
            photoy=photoy,
            thumbx=thumbx,
            thumby=thumby,
            videox=videox,
            videoy=videoy,
            duration=duration,
        )
        insert_exif_rows(photo_id, source_file)
        imported += 1

    album_count = mysql_scalar(f"SELECT COUNT(*) FROM {PHOTOS_TABLE} WHERE album={album_id}")
    print(f'imported={imported} skipped={skipped} album_count={album_count}')


if __name__ == '__main__':
    main()
