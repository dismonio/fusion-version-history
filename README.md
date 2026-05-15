# Fusion 360 Version History Puller

Recover Fusion 360 file/version metadata from your Autodesk hub via the APS
Data Management API, and write it to a SQLite DB + CSV you can grep against
the labels on your physical parts.

Background: in March 2026 Autodesk migrated personal Fusion hubs onto the
Collaborative Editing Hub, which hid the integer version numbers (`v1, v2, ...`)
that used to be visible in the Data Panel and used to be auto-appended to
exported filenames. Pre-migration auto-saves still appear to live in the cloud
metadata; this script pulls them out.

**Don't run a full walk until the diagnostic confirms data is recoverable.**
See "Run the diagnostic first" below.

## One-time setup

You need an APS application registered against your Autodesk account.

### 1. Register the APS app

1. Go to <https://aps.autodesk.com/myapps> and sign in with the same Autodesk
   account that owns your Fusion files.
2. Click **Create Application**. **App type matters here -- pick one:**
   - **Traditional Web App** (confidential client). Easiest if you already
     have one. Requires both Client ID AND Client Secret in env vars.
   - **Desktop, Mobile, Single-Page App** (public client). PKCE-only, no
     secret needed. The "clean" choice for a CLI script like this.
3. Name it anything ("Fusion Version Puller"). Under APIs, enable
   **Data Management API**.
4. Set the Callback URL to exactly `http://localhost:8080/`. This exact string
   is the OAuth `redirect_uri` and must match byte-for-byte in three places
   (this APS registration, the script's authorize URL, the script's token
   exchange). Don't substitute `127.0.0.1`.
5. Save, then copy the **Client ID** (and **Client Secret** if you picked
   Traditional Web App) from the app detail page.

### 2. Set environment variables

In PowerShell:

```powershell
setx APS_CLIENT_ID "your-client-id-here"
# Only if your app is a Traditional Web App:
setx APS_CLIENT_SECRET "your-client-secret-here"
```

Open a new terminal afterward so the env vars are visible to the script.

If you accidentally run the script against a Traditional Web App without
setting the secret, you'll see `401 invalid_credentials` on the token
exchange. The script's error message will point you here.

### 3. Install dependencies

```powershell
pip install -r requirements.txt
```

## Easy ways to run it (no terminal thinking)

Once setup above is done, you usually don't need to remember the individual
commands. Two convenience wrappers:

- **`update_and_view.bat`** -- double-click. Runs an incremental refresh
  (`--since-last-run`), regenerates the HTML viewer, and opens it in your
  default browser. The terminal window stays open if anything fails so you
  can read the error.
- **`launcher.bat`** -- double-click. Wraps `python launcher.py` and keeps
  the console window open. Same web UI as below, no terminal required.
- **`launcher.py`** -- `python launcher.py` from any terminal. Boots a tiny
  local web server on `http://127.0.0.1:8765` and auto-opens it in your
  browser. You get a status panel (item / version counts, last walk time),
  a hub dropdown (no stdin prompt), buttons for incremental refresh, full
  walk, regenerate viewer, open viewer, re-authenticate, and a live log
  pane that streams subprocess output via Server-Sent Events. Stdlib only,
  no Flask. Bind is `127.0.0.1` only, never the network.

For initial setup or one-off diagnostics, the raw `python
fusion_version_history.py ...` commands documented below are still the
right entry point. Everything else can go through the launcher.

## Run the diagnostic first

Pick one design you remember saving a lot (e.g. "Bracket" you saved ~50
times before March 2026) and run:

```powershell
python fusion_version_history.py --inspect "Bracket"
```

The browser opens for a one-time consent. Then the script searches every
hub/project/folder for items matching `Bracket` (case-insensitive exact match
first; substring fallback if no exact match), prompts you if there are
multiple, and dumps the full `/versions` JSON to stdout plus
`inspect_<item_id>.json`.

**What you want to see:** ~50 entries with sequential `versionNumber` values
from `1` upward, and the diagnostic summary saying `missing/non-integer count: 0`.

**Failure modes the diagnostic flags explicitly:**

- Only 1-3 versions returned for a file you remember saving many times.
  The API may have been restricted post-migration to only return
  "Create version" snapshots, in which case pre-migration auto-save data is
  effectively gone. **Stop and reconsider before bulk-walking** -- the CSV
  won't have what you need.
- `missing/non-integer count > 0`. Some versions don't carry an integer
  `versionNumber`. Those rows are still recorded but won't sort into the
  obvious places in the CSV. Inspect the raw JSON to see what fields they do
  have.

If the diagnostic looks good, proceed.

## Full bulk run

```powershell
python fusion_version_history.py
```

If you have more than one hub (Personal + Team you've been invited to), the
script prompts. Otherwise it auto-selects. It then walks every project, every
folder, every Fusion item (`.f3d` / `.f3z`), and writes:

- `fusion_versions.db` - SQLite, the source of truth. Updated in place across
  runs, never dropped.
- `fusion_versions.csv` - regenerated from the DB on every successful run.
  One row per version. This is the table you grep against physical-part labels.

To include uploaded non-Fusion files (STEP/IGES/etc.) as well, add
`--include-non-fusion`.

## Re-running

The DB is cumulative. Subsequent runs upsert by ID, so any new files / new
versions get added, and existing rows update with the latest metadata.

To save API calls when nothing big has changed, add `--since-last-run`:

```powershell
python fusion_version_history.py --since-last-run
```

This still walks the folder tree (to pick up new files), but skips the
`/versions` fetch for items whose `lastModifiedTime` matches what's already
in the DB.

## Browse the results in a viewer

After (or during!) a bulk run, generate a self-contained HTML viewer:

```powershell
python view.py
```

This reads the DB read-only and writes `fusion_versions.html`. Double-click
it to open in any browser. Layout mimics Fusion's HISTORY pane (date-grouped,
time + description + Changed by columns) but **with version numbers visible**
-- which is the whole point.

The sidebar is a searchable file list (substring match, case-insensitive),
with a toggle between flat and folder-nested tree views. Click a file to see
its version history. The view is purely client-side and opens from `file://`,
so no server is needed.

Each version row has two action icons:

- 🌐 **Open in web hub** -- builds a deep link of the form
  `https://{your-subdomain}.autodesk360.com/g/projects/{numericId}/data/{folderB64}/{itemB64}/overview?time={ms}`
  and opens it in a new tab. The dropdown should highlight the matching
  version when the page loads.
- 📋 **Copy URN** -- copies the version URN to clipboard, e.g.
  `urn:adsk.wipprod:fs.file:vf.cwa8_LWfR8i2N2iidk3M0w?version=137`. Useful as a
  rock-solid fallback if the web link doesn't work for some reason.

**Heads-up on the web-link pattern:** the URL format was reverse-engineered
from one user's personal hub (`a.`-prefixed project IDs that decode to
`business:{username}#{numericId}`). It is **not** guaranteed to work for
Autodesk Construction Cloud or Fusion Team hubs (`b.`-prefixed project IDs).
If `parseProjectId` returns null for your project IDs, the 🌐 icon simply
won't appear -- the 📋 Copy URN button is the fallback that always works.

Safe to regenerate at any time -- the viewer reads the DB read-only, so it
won't interfere with a walker that's still running.

## Reconciling a physical part

Suppose you have a part stamped or labeled "Baseplate v7":

```powershell
sqlite3 fusion_versions.db "SELECT version_created_time, version_created_user, version_display_name FROM versions v JOIN items i ON i.item_id = v.item_id WHERE i.display_name LIKE 'Baseplate%' AND v.version_number = 7"
```

Or open `fusion_versions.csv` and filter for `item_name = Baseplate` and
`version_number = 7`. The `version_created_time` on that row is when that
revision was saved -- the answer you're looking for.

## CLI flags

| Flag | Purpose |
|---|---|
| `--inspect ITEM_NAME` | Diagnostic mode (by name). Two-pass match: exact, then substring. |
| `--inspect-item ITEM_ID` | Diagnostic mode (by exact item URN/ID). |
| `--hub NAME` | Pick a hub by case-insensitive substring instead of prompting. |
| `--db PATH` | Override DB path (default `./fusion_versions.db`). |
| `--csv PATH` | Override CSV path (default `./fusion_versions.csv`). |
| `--reauth` | Delete cached token and re-run the browser auth flow. |
| `--since-last-run` | Skip `/versions` for items whose `lastModifiedTime` is unchanged. |
| `--include-non-fusion` | Include STEP/IGES/etc. (default filters to `.f3d`/`.f3z`). |
| `--dry-run` | Walk folders and count items, no `/versions` calls, no DB writes. |
| `--verbose` | Log every HTTP request and retry. |
| `--list-hubs-json` | Print hubs as JSON to stdout and exit (used by `launcher.py`; no interactive prompt). |

## Where things live

- **Credentials**: `%APPDATA%\fusion-version-history\token.json`. Treat like a
  password -- it grants read access to your Fusion files. Delete to force a
  fresh login.
- **Project outputs**: `fusion_versions.db`, `fusion_versions.csv`,
  `inspect_*.json`. All in the script's working directory.

## Troubleshooting

- **Browser opens but the callback page never loads.** The local HTTP server
  binds port 8080 (IPv6 dual-stack with v4 fallback). Make sure nothing else
  on this machine is listening on 8080.
- **`OAuth state mismatch` error.** Someone or something redirected the
  callback. Re-run and try again; if it persists, check for browser extensions
  rewriting URLs.
- **Item fetch fails with 404 for one specific file.** Logged with
  `fetch_error` populated in the `items` table. Run continues. Usually means
  the item was deleted or moved; the existing CSV row carries the error.
- **API returns 429 (rate limit).** Script honors the `Retry-After` header
  and resumes; just wait it out. For a large hub the full walk may take a
  while.
- **Token says expired but refresh fails.** Run with `--reauth` to force a
  fresh interactive login (or click **Re-authenticate** in the launcher).
- **Launcher says "Could not bind 127.0.0.1:8765".** Another process is
  already on that port (or another instance of the launcher is running).
  Close the duplicate and try again, or edit `LAUNCHER_PORT` near the top of
  `launcher.py`.
- **Launcher's "Open viewer" button does nothing.** The viewer is served
  inline at `http://127.0.0.1:8765/viewer` (browsers block `file://` from
  `http://` origins). If the page is blank, check the launcher's terminal
  for errors -- the regen step may have failed.

## Why this tool matters (and why to re-run it)

The March 2026 Collaborative Editing Hub migration **hid** integer version
numbers in the UI but did **not** remove them from the underlying storage.
The Data Management API still returns every version with its original
`versionNumber` field intact (confirmed against files with 100+ pre-migration
saves). The new "Create version" feature appears to be a UI/branding rework
of the legacy "Milestone" tag -- the URL-level `historyChangeId` token still
base64-decodes to a string ending in `milestone`, so the underlying data
model hasn't actually changed.

**That said, there is no public commitment from Autodesk to preserve this
behavior.** The Data Management API was last updated 2025-08-04 and the
Collaborative Editing Hub is only a couple of months old. If Autodesk ever
removes the `versionNumber` field in favor of an event-sourced model with
opaque change IDs, the mapping from physical part labels back to specific
saves is gone forever.

**The CSV / SQLite this script produces is your backstop.** Even if the API
shape changes tomorrow, your local lookup table still works for everything
saved before that point. Plan to re-run periodically (e.g. quarterly) to
capture new history; back up `fusion_versions.db` alongside any physical
parts inventory you're keeping.

## Ideas, deliberately not implemented

A few things that came up during the build that I decided NOT to do, with
reasoning, in case future-you (or another user) is tempted:

### Writing version numbers back into Fusion's milestone system

It is *probably* possible to script-create milestones via the Data Management
API such that the integer version numbers appear again in Fusion's UI. I
recommend against it:

- The exact endpoint and payload for milestone creation in the post-migration
  Collaborative Editing Hub is **not publicly documented** -- you'd be
  reverse-engineering an internal API.
- Mistakes are not cleanly reversible at scale. A typical personal hub has
  ~2,000 versions across hundreds of files; a wrong payload format could
  permanently litter your UI with garbage milestones.
- Bulk writes would almost certainly trigger rate limits or account flags;
  this is a use pattern Autodesk has not optimized for and likely hasn't
  observed at any meaningful volume from third-party tools.
- Even on success, the rendered milestone labels may not look like the
  pre-migration `v7` style; you'd be testing into the unknown.

If you really want this anyway, the safest path is:
1. Create a throwaway test design.
2. Manually create a milestone via the Fusion UI on it.
3. Diff `/versions` JSON before and after to see what fields the milestone
   adds.
4. Watch browser DevTools while Fusion's UI makes the milestone POST to
   capture the actual endpoint and payload.
5. Test write + delete on the throwaway file until reliable.
6. **Only then** consider running it for real -- and accept the risk.

### Live / real-time mode

The current design is a snapshot. A `--watch N` flag that loops
`--since-last-run` every N seconds (60+ to respect rate limits) would give
"live-ish" behavior. Skipped for now because the use case (reconciling
physical part labels) doesn't need real-time. If your hub becomes
high-velocity, this is the path -- not webhooks (which need a public HTTPS
callback) but polling.
