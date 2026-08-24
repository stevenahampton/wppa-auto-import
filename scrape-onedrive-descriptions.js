#!/usr/bin/env node
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const { chromium } = require('playwright-core');

const TOOL_ROOT = __dirname;
const WORDPRESS_ROOT = process.env.WPPA_WP_ROOT || '/var/www/wordpress';
const WP_CONFIG = path.join(WORDPRESS_ROOT, 'wp-config.php');
const STATE_DIR = process.env.WPPA_SCRAPER_STATE || path.join(TOOL_ROOT, 'onedrive-scraper-state');
const CHROME_PATH = process.env.WPPA_CHROME_PATH || '/usr/bin/google-chrome';

function usage(exitCode = 0) {
  const output = exitCode ? process.stderr : process.stdout;
  output.write(`Usage:
  scrape-onedrive-descriptions.js [options] ONEDRIVE_ALBUM_URL WPPA_ALBUM_NAME

Options:
  --apply          Update WordPress. Without this flag, only show proposed changes.
  --clear-empty    Replace a WPPA prefix with empty text when OneDrive has no description.
  --headed         Show Chrome while scraping (useful if Microsoft changes the page).
  --limit N        Process at most N items, useful for testing.
  --restart        Ignore the saved checkpoint and walk every item again.
  --help           Show this help.

The WPPA album name is required to prevent matching a duplicate filename in another album.
Everything from @@BR@@ onward in the existing WPPA description is preserved.
`);
  process.exit(exitCode);
}

function parseArgs(argv) {
  const options = {
    apply: false,
    clearEmpty: false,
    headed: false,
    limit: Infinity,
    restart: false,
    positional: [],
  };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--apply') options.apply = true;
    else if (arg === '--clear-empty') options.clearEmpty = true;
    else if (arg === '--headed') options.headed = true;
    else if (arg === '--restart') options.restart = true;
    else if (arg === '--help' || arg === '-h') usage(0);
    else if (arg === '--limit') {
      index += 1;
      const value = Number.parseInt(argv[index], 10);
      if (!Number.isInteger(value) || value < 1) {
        throw new Error('--limit requires a positive integer');
      }
      options.limit = value;
    } else if (arg.startsWith('-')) {
      throw new Error(`Unknown option: ${arg}`);
    } else {
      options.positional.push(arg);
    }
  }

  if (options.positional.length !== 2) usage(1);
  const [albumUrl, albumName] = options.positional;
  try {
    new URL(albumUrl);
  } catch {
    throw new Error(`Invalid OneDrive URL: ${albumUrl}`);
  }
  return { ...options, albumUrl, albumName };
}

function parseWpConfig() {
  const config = fs.readFileSync(WP_CONFIG, 'utf8');
  const constant = (name) => {
    const match = config.match(new RegExp(`define\\(\\s*['"]${name}['"]\\s*,\\s*['"]([^'"]*)['"]\\s*\\)`));
    if (!match) throw new Error(`Could not read ${name} from ${WP_CONFIG}`);
    return match[1];
  };
  const prefixMatch = config.match(/\$table_prefix\s*=\s*['"]([^'"]+)['"]/);
  if (!prefixMatch) throw new Error(`Could not read table_prefix from ${WP_CONFIG}`);
  return {
    host: constant('DB_HOST'),
    database: constant('DB_NAME'),
    user: constant('DB_USER'),
    password: constant('DB_PASSWORD'),
    prefix: prefixMatch[1],
  };
}

function mysqlQuery(db, sql) {
  const result = spawnSync(
    'mysql',
    ['--batch', '--raw', '--skip-column-names', '-h', db.host, '-u', db.user, db.database],
    {
      input: sql,
      encoding: 'utf8',
      env: { ...process.env, MYSQL_PWD: db.password },
      maxBuffer: 32 * 1024 * 1024,
    },
  );
  if (result.status !== 0) {
    throw new Error(`MySQL failed: ${(result.stderr || '').trim()}`);
  }
  return result.stdout.trim();
}

function sqlString(value) {
  return `'${String(value).replace(/\\/g, '\\\\').replace(/'/g, "''")}'`;
}

function mediaKey(value) {
  let stem = path.parse(String(value).trim()).name;
  if (/\.(?:jpg|jpeg|png|gif|webp|mp4|ogv|webm|mov|avi|mkv|flv|mp3|wav|ogg|pdf|xxx)$/i.test(path.extname(stem))) {
    stem = path.parse(stem).name;
  }
  return stem.toLocaleLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
}

function mediaKind(filename) {
  return /\.(?:mp4|ogv|webm|mov|avi|mkv|flv)$/i.test(filename) ? 'video' : 'other';
}

function loadAlbum(db, albumName) {
  const albumsTable = `${db.prefix}wppa_albums`;
  const photosTable = `${db.prefix}wppa_photos`;
  const albumRows = mysqlQuery(
    db,
    `SELECT JSON_OBJECT('id',id,'name',name) FROM ${albumsTable} WHERE name=${sqlString(albumName)} ORDER BY id;`,
  ).split('\n').filter(Boolean).map(JSON.parse);

  if (albumRows.length === 0) throw new Error(`WPPA album not found: ${albumName}`);
  if (albumRows.length > 1) throw new Error(`More than one WPPA album is named: ${albumName}`);
  const album = albumRows[0];

  const photoLines = mysqlQuery(
    db,
    `SELECT JSON_OBJECT('id',id,'filename',filename,'name',name,'description',description,'ext',ext) FROM ${photosTable} WHERE album=${Number(album.id)} ORDER BY id;`,
  );
  const photos = photoLines ? photoLines.split('\n').filter(Boolean).map(JSON.parse) : [];
  return { album, photos, photosTable };
}

function buildMatcher(photos) {
  const exact = new Map();
  const normalized = new Map();
  for (const photo of photos) {
    const kind = photo.ext === 'xxx' ? 'video' : 'other';
    const exactKey = `${kind}:${String(photo.filename).toLocaleLowerCase()}`;
    if (!exact.has(exactKey)) exact.set(exactKey, []);
    exact.get(exactKey).push(photo);

    for (const candidate of [photo.filename, photo.name]) {
      const key = `${kind}:${mediaKey(candidate)}`;
      if (!normalized.has(key)) normalized.set(key, []);
      if (!normalized.get(key).some((item) => item.id === photo.id)) normalized.get(key).push(photo);
    }
  }

  return (filename) => {
    const kind = mediaKind(filename);
    const exactMatches = exact.get(`${kind}:${filename.toLocaleLowerCase()}`) || [];
    if (exactMatches.length === 1) return { photo: exactMatches[0], method: 'exact' };
    if (exactMatches.length > 1) return { ambiguous: exactMatches, method: 'exact' };

    const matches = normalized.get(`${kind}:${mediaKey(filename)}`) || [];
    if (matches.length === 1) return { photo: matches[0], method: 'normalized' };
    if (matches.length > 1) return { ambiguous: matches, method: 'normalized' };
    return { missing: true };
  };
}

function loadState(stateFile, restart) {
  if (restart || !fs.existsSync(stateFile)) return { processedUrls: [] };
  try {
    const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
    return Array.isArray(state.processedUrls) ? state : { processedUrls: [] };
  } catch {
    return { processedUrls: [] };
  }
}

function saveState(stateFile, processedUrls) {
  fs.mkdirSync(path.dirname(stateFile), { recursive: true });
  const temporary = `${stateFile}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify({ processedUrls: [...processedUrls] }, null, 2)}\n`);
  fs.renameSync(temporary, stateFile);
}

function replacePrefix(existing, oneDriveDescription) {
  const delimiter = '@@BR@@';
  const index = String(existing || '').indexOf(delimiter);
  const suffix = index >= 0 ? String(existing).slice(index) : '';
  return `${oneDriveDescription}${suffix}`;
}

async function openFirstItem(page, albumUrl) {
  await page.goto(albumUrl, { waitUntil: 'domcontentloaded', timeout: 120000 });
  const firstItem = page.getByRole('link', { name: /^(Photo|Video) with tags:/ }).first();
  await firstItem.waitFor({ state: 'visible', timeout: 120000 });
  await firstItem.click();
  await page.getByRole('button', { name: 'View next photo' }).waitFor({ state: 'attached', timeout: 120000 });
}

async function ensureInfoPane(page) {
  const description = page.locator('#__description-input, input[placeholder="No description"], textarea[placeholder="No description"], [role="textbox"][aria-label="No description"]').first();
  if (!(await description.isVisible().catch(() => false))) {
    const showInfo = page.getByRole('menuitem', { name: 'Show detailed information' });
    await showInfo.waitFor({ state: 'visible', timeout: 30000 });
    await showInfo.click();
  }
  await description.waitFor({ state: 'visible', timeout: 30000 });
  return description;
}

async function readCurrentItem(page) {
  const descriptionField = await ensureInfoPane(page);
  const filenameField = page.locator('#__details-panel-title');
  await filenameField.waitFor({ state: 'visible', timeout: 30000 });
  const filename = String(await filenameField.textContent()).trim();
  const description = await descriptionField.evaluate((element) => (
    'value' in element ? element.value : element.textContent
  ));
  return {
    filename,
    description: String(description || '').trim(),
    url: page.url(),
  };
}

async function goNext(page) {
  const next = page.getByRole('button', { name: 'View next photo' });
  if ((await next.count()) === 0 || await next.isDisabled().catch(() => false)) return false;
  const filenameField = page.locator('#__details-panel-title');
  const previousFilename = String(await filenameField.textContent()).trim();
  const previousUrl = page.url();
  await next.click();
  await page.waitForFunction(
    (filename) => {
      const field = document.querySelector('#__details-panel-title');
      return field && field.textContent.trim() !== filename;
    },
    previousFilename,
    { timeout: 30000 },
  ).catch(() => {});
  return page.url() !== previousUrl || String(await filenameField.textContent()).trim() !== previousFilename;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const db = parseWpConfig();
  const { album, photos, photosTable } = loadAlbum(db, options.albumName);
  if (photos.length === 0) throw new Error(`WPPA album is empty: ${options.albumName}`);

  const stateHash = crypto.createHash('sha256')
    .update(`${options.albumUrl}\n${options.albumName}`)
    .digest('hex')
    .slice(0, 16);
  const stateFile = path.join(STATE_DIR, `${stateHash}.json`);
  const state = loadState(stateFile, options.restart);
  const processedUrls = new Set(state.processedUrls);
  const matchPhoto = buildMatcher(photos);

  console.log(`WPPA album ${album.id}: ${options.albumName} (${photos.length} records)`);
  console.log(`${options.apply ? 'APPLY' : 'DRY RUN'} mode; checkpoint: ${stateFile}`);

  const browser = await chromium.launch({
    executablePath: CHROME_PATH,
    headless: !options.headed,
    args: ['--disable-dev-shm-usage'],
  });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.setDefaultTimeout(30000);

  const stats = { visited: 0, updated: 0, unchanged: 0, empty: 0, missing: 0, ambiguous: 0, resumed: 0 };
  const seenThisRun = new Set();

  try {
    await openFirstItem(page, options.albumUrl);
    while (stats.visited < options.limit) {
      const item = await readCurrentItem(page);
      if (seenThisRun.has(item.url)) {
        console.log(`STOP repeated viewer item: ${item.filename}`);
        break;
      }
      seenThisRun.add(item.url);
      stats.visited += 1;

      if (options.apply && processedUrls.has(item.url)) {
        stats.resumed += 1;
        console.log(`RESUME-SKIP ${item.filename}`);
      } else {
        const match = matchPhoto(item.filename);
        if (match.missing) {
          stats.missing += 1;
          console.log(`NO MATCH    ${item.filename}`);
        } else if (match.ambiguous) {
          stats.ambiguous += 1;
          console.log(`AMBIGUOUS   ${item.filename} -> IDs ${match.ambiguous.map((photo) => photo.id).join(', ')}`);
        } else if (!item.description && !options.clearEmpty) {
          stats.empty += 1;
          console.log(`NO DESC     ${item.filename} -> WPPA ${match.photo.id}`);
          if (options.apply) {
            processedUrls.add(item.url);
            saveState(stateFile, processedUrls);
          }
        } else {
          const replacement = replacePrefix(match.photo.description, item.description);
          if (replacement === String(match.photo.description || '')) {
            stats.unchanged += 1;
            console.log(`UNCHANGED   ${item.filename} -> WPPA ${match.photo.id}`);
          } else {
            const action = options.apply ? 'UPDATED' : 'WOULD UPDATE';
            console.log(`${action.padEnd(12)} ${item.filename} -> WPPA ${match.photo.id}: ${JSON.stringify(item.description)}`);
            if (options.apply) {
              mysqlQuery(
                db,
                `UPDATE ${photosTable} SET description=${sqlString(replacement)}, modified=UNIX_TIMESTAMP(), indexdtm='' WHERE id=${Number(match.photo.id)} AND album=${Number(album.id)};`,
              );
              match.photo.description = replacement;
              stats.updated += 1;
            }
          }
          if (options.apply) {
            processedUrls.add(item.url);
            saveState(stateFile, processedUrls);
          }
        }
      }

      if (stats.visited >= options.limit || !(await goNext(page))) break;
    }
  } finally {
    await browser.close();
  }

  console.log(`SUMMARY ${JSON.stringify(stats)}`);
  if (stats.missing || stats.ambiguous) process.exitCode = 2;
}

main().catch((error) => {
  console.error(`ERROR: ${error.stack || error.message}`);
  process.exitCode = 1;
});
