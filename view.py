"""Generate a single-file HTML viewer from fusion_versions.db.

Reads the DB read-only, so it's safe to run while the main walker is still
populating the DB. Output is a self-contained .html with the data embedded as
JSON -- double-click to open in any browser.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime


def load_data(db_path: str) -> list[dict]:
    """Load items + their versions into a list[dict], one entry per item."""
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    items_rows = conn.execute("""
        SELECT
          i.item_id,
          i.project_id,
          i.folder_id,
          i.display_name,
          i.extension_type,
          i.last_modified_time,
          i.fetch_error,
          h.name AS hub_name,
          p.name AS project_name,
          COALESCE(f.path, '/') AS folder_path
        FROM items i
        JOIN projects p ON p.project_id = i.project_id
        JOIN hubs     h ON h.hub_id     = p.hub_id
        LEFT JOIN folders f ON f.folder_id = i.folder_id
        ORDER BY i.display_name COLLATE NOCASE, i.item_id
    """).fetchall()

    versions_by_item: dict[str, list[dict]] = {}
    for row in conn.execute("""
        SELECT item_id, version_number, version_urn, display_name,
               created_time, created_user_name, last_modified_time, storage_size
        FROM versions
        ORDER BY item_id, version_number DESC, created_time DESC
    """):
        versions_by_item.setdefault(row["item_id"], []).append({
            "version_number":      row["version_number"],
            "version_urn":         row["version_urn"],
            "display_name":        row["display_name"],
            "created_time":        row["created_time"],
            "created_user_name":   row["created_user_name"],
            "last_modified_time":  row["last_modified_time"],
            "storage_size":        row["storage_size"],
        })

    conn.close()

    out: list[dict] = []
    for r in items_rows:
        versions = versions_by_item.get(r["item_id"], [])
        out.append({
            "item_id":            r["item_id"],
            "project_id":         r["project_id"],
            "folder_id":          r["folder_id"],
            "display_name":       r["display_name"] or "(unnamed)",
            "extension_type":     r["extension_type"],
            "last_modified_time": r["last_modified_time"],
            "fetch_error":        r["fetch_error"],
            "hub_name":           r["hub_name"],
            "project_name":       r["project_name"],
            "folder_path":        r["folder_path"],
            "version_count":      len(versions),
            "versions":           versions,
        })
    return out


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Fusion Version History</title>
<style>
:root {
  --bg: #fafafa;
  --fg: #222;
  --muted: #777;
  --accent: #0696d7;
  --accent-soft: #e3f2fd;
  --border: #e0e0e0;
  --row-hover: #eef;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  color: var(--fg);
  background: #fff;
}
header {
  padding: 10px 18px;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: baseline;
  gap: 14px;
}
header h1 { margin: 0; font-size: 16px; font-weight: 600; }
header .counts { color: var(--muted); font-size: 12px; }
header .gen { color: var(--muted); font-size: 11px; margin-left: auto; }
main {
  display: grid;
  grid-template-columns: 340px 1fr;
  height: calc(100vh - 45px);
}
aside {
  border-right: 1px solid var(--border);
  overflow-y: auto;
  background: var(--bg);
}
aside .search-row {
  position: sticky;
  top: 0;
  padding: 10px;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  z-index: 1;
  display: flex;
  gap: 6px;
  align-items: stretch;
}
aside input {
  flex: 1;
  min-width: 0;
  padding: 6px 8px;
  font: inherit;
  font-size: 13px;
  border: 1px solid var(--border);
  border-radius: 3px;
}
aside ul { list-style: none; margin: 0; padding: 0; }
.item {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  position: relative;
}
.item:hover { background: var(--row-hover); }
.item.selected { background: var(--accent); color: white; }
.item .name {
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding-right: 36px;
}
.item .sub {
  font-size: 11px;
  color: var(--muted);
  margin-top: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.item.selected .sub { color: rgba(255,255,255,0.85); }
.item .vcount {
  position: absolute;
  top: 8px;
  right: 12px;
  font-size: 11px;
  background: rgba(0,0,0,0.07);
  padding: 1px 6px;
  border-radius: 8px;
  color: var(--muted);
}
.item.selected .vcount { background: rgba(255,255,255,0.25); color: white; }
.item.err .vcount { background: #fbbcbc; color: #800; }
section { overflow-y: auto; padding: 20px 24px; }
.empty { color: var(--muted); }
.detail-head { padding-bottom: 12px; border-bottom: 1px solid var(--border); margin-bottom: 14px; }
.detail-head h2 { margin: 0 0 4px 0; font-size: 18px; font-weight: 600; }
.detail-head .path { color: var(--muted); font-size: 12px; }
.detail-head .meta { color: var(--muted); font-size: 12px; margin-top: 4px; }
.detail-head .err {
  color: #800;
  background: #fef0f0;
  padding: 4px 8px;
  border-left: 3px solid #c44;
  margin-top: 8px;
  font-size: 12px;
}
.date-group { margin-top: 18px; }
.date-group h3 {
  margin: 0 0 4px 0;
  font-size: 12px;
  font-weight: 500;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 4px;
}
table { width: 100%; border-collapse: collapse; }
td {
  padding: 6px 8px;
  border-bottom: 1px solid #f3f3f3;
  vertical-align: top;
  font-size: 13px;
}
tr:hover td { background: #fafafa; }
td.v {
  font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
  color: var(--accent);
  font-weight: 600;
  width: 56px;
  text-align: right;
}
td.t {
  width: 92px;
  color: var(--muted);
  font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
  font-size: 12px;
}
td.u { width: 170px; color: var(--muted); }
td.s { width: 80px; color: var(--muted); text-align: right; font-size: 12px; }
td.a { width: 64px; text-align: right; white-space: nowrap; padding-right: 4px; }
.act {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 22px;
  margin-left: 2px;
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 4px;
  cursor: pointer;
  color: var(--muted);
  font-size: 13px;
  line-height: 1;
  padding: 0;
}
.act:hover { background: var(--accent-soft); color: var(--accent); border-color: var(--accent); }
.act:active { transform: translateY(1px); }
.act-link { text-decoration: none; }
.act-fusion { text-decoration: none; font-weight: 700; font-size: 11px; color: #d97a1f; }
.act-fusion:hover { background: #fff3e0; color: #b85f10; border-color: #d97a1f; }
.act .col-a-wider { width: 90px; }
td.a { width: 90px; }
.no-results { padding: 20px; color: var(--muted); font-size: 13px; }
.match-mark { background: #ffe9a8; }
#toast {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%) translateY(20px);
  background: #222;
  color: white;
  padding: 8px 16px;
  border-radius: 4px;
  font-size: 13px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.18s, transform 0.18s;
  z-index: 100;
  box-shadow: 0 2px 8px rgba(0,0,0,0.2);
}
#toast.show {
  opacity: 1;
  transform: translateX(-50%) translateY(0);
}

.view-toggle {
  font: inherit;
  font-size: 12px;
  color: var(--fg);
  background: white;
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 3px 9px;
  cursor: pointer;
  white-space: nowrap;
  flex-shrink: 0;
}
.view-toggle:hover { background: #f0f0f0; }

.tree-row {
  padding: 4px 12px 4px 0;
  display: flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  border-bottom: 1px solid #f3f3f3;
  position: relative;
}
.tree-row .chevron {
  width: 12px;
  flex-shrink: 0;
  text-align: center;
  color: var(--muted);
  font-size: 9px;
}
.tree-row .label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1 1 auto;
  min-width: 0;
}
.tree-row .match-count { color: var(--muted); font-size: 11px; flex-shrink: 0; }
.tree-row.project {
  background: #ececec;
  font-weight: 600;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #555;
  border-bottom: 1px solid var(--border);
}
.tree-row.project:hover { background: #e0e0e0; }
.tree-row.folder { font-size: 13px; color: var(--fg); }
.tree-row.folder:hover { background: var(--row-hover); }
.tree-row.file { padding-right: 38px; font-size: 13px; }
.tree-row.file:hover { background: var(--row-hover); }
.tree-row.file.selected { background: var(--accent); color: white; }
.tree-row.file .vcount {
  position: absolute;
  top: 4px;
  right: 12px;
  font-size: 11px;
  background: rgba(0,0,0,0.07);
  padding: 1px 6px;
  border-radius: 8px;
  color: var(--muted);
}
.tree-row.file.selected .vcount { background: rgba(255,255,255,0.25); color: white; }
.tree-row.file.err .vcount { background: #fbbcbc; color: #800; }
</style>
</head>
<body>
<header>
  <h1>Fusion Version History</h1>
  <div class="counts" id="counts"></div>
  <div class="gen">generated __GENERATED_AT__</div>
</header>
<main>
  <aside>
    <div class="search-row">
      <input id="search" type="search" placeholder="Search files by name..." autofocus>
      <button class="view-toggle" id="view-toggle" type="button"></button>
    </div>
    <ul id="item-list"></ul>
  </aside>
  <section id="detail">
    <p class="empty">Select a file on the left to see its version history.</p>
    <p class="empty">Tip: search supports substring (case-insensitive). Sidebar shows version count per file.</p>
  </section>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<script>
const DATA = __DATA_JSON__;
const $list   = document.getElementById('item-list');
const $detail = document.getElementById('detail');
const $search = document.getElementById('search');
const $counts = document.getElementById('counts');

let selected = null;
let viewMode = localStorage.getItem('fvh.viewMode') || 'flat';
let expanded = new Set(JSON.parse(localStorage.getItem('fvh.expanded') || '[]'));

function saveExpanded() {
  localStorage.setItem('fvh.expanded', JSON.stringify([...expanded]));
}

const totalVersions = DATA.reduce((s, it) => s + it.version_count, 0);
$counts.textContent = `${DATA.length.toLocaleString()} items - ${totalVersions.toLocaleString()} versions`;

function escapeHTML(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  }[c]));
}

function highlight(text, term) {
  if (!term) return escapeHTML(text);
  const t = String(text ?? '');
  const i = t.toLowerCase().indexOf(term.toLowerCase());
  if (i < 0) return escapeHTML(t);
  return escapeHTML(t.slice(0, i))
       + '<span class="match-mark">' + escapeHTML(t.slice(i, i + term.length)) + '</span>'
       + escapeHTML(t.slice(i + term.length));
}

function formatBytes(n) {
  if (n == null) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
  if (n < 1024*1024*1024) return (n/1024/1024).toFixed(1) + ' MB';
  return (n/1024/1024/1024).toFixed(2) + ' GB';
}

function formatDayHeader(iso) {
  if (!iso) return 'Unknown date';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {
    weekday: 'short', year: 'numeric', month: 'short', day: 'numeric'
  });
}

function formatTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function dayKey(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return d.toISOString().slice(0, 10); // YYYY-MM-DD
}

function groupByDay(versions) {
  const groups = [];
  let curKey = null;
  for (const v of versions) {
    const k = dayKey(v.created_time);
    if (k !== curKey) {
      groups.push({ key: k, header: formatDayHeader(v.created_time), versions: [] });
      curKey = k;
    }
    groups[groups.length - 1].versions.push(v);
  }
  return groups;
}

function buildTree() {
  const root = { type: 'root', children: new Map(), items: [] };
  for (const item of DATA) {
    const proj = item.project_name || '(no project)';
    const folder = item.folder_path || '/';
    const segments = folder.split('/').filter(s => s.length > 0);

    if (!root.children.has(proj)) {
      root.children.set(proj, { type: 'project', name: proj, path: proj, children: new Map(), items: [] });
    }
    let node = root.children.get(proj);

    let pathSoFar = proj;
    for (const seg of segments) {
      pathSoFar = pathSoFar + ' / ' + seg;
      if (!node.children.has(seg)) {
        node.children.set(seg, { type: 'folder', name: seg, path: pathSoFar, children: new Map(), items: [] });
      }
      node = node.children.get(seg);
    }
    node.items.push(item);
  }
  return root;
}

function annotateMatches(node, termLower) {
  let count = 0;
  for (const child of node.children.values()) {
    count += annotateMatches(child, termLower);
  }
  for (const item of node.items) {
    if (!termLower || (item.display_name || '').toLowerCase().includes(termLower)) count++;
  }
  node.matchCount = count;
  return count;
}

function renderTreeNode(node, depth, termLower, autoExpand) {
  if (termLower && node.matchCount === 0) return '';
  const isExpanded = autoExpand || expanded.has(node.path);
  const hasChildren = node.children.size > 0 || node.items.length > 0;
  const chevron = hasChildren ? (isExpanded ? '▾' : '▸') : '';
  const indent = 8 + depth * 14;

  let html = `<div class="tree-row ${node.type}" data-folder="${escapeHTML(node.path)}" style="padding-left: ${indent}px">
    <span class="chevron">${chevron}</span>
    <span class="label">${escapeHTML(node.name)}</span>
    <span class="match-count">${termLower && node.matchCount ? node.matchCount : ''}</span>
  </div>`;

  if (isExpanded) {
    const folderChildren = [...node.children.values()].sort((a,b) => a.name.localeCompare(b.name));
    for (const c of folderChildren) {
      html += renderTreeNode(c, depth + 1, termLower, autoExpand);
    }
    const fileChildren = node.items
      .filter(it => !termLower || (it.display_name || '').toLowerCase().includes(termLower))
      .sort((a,b) => (a.display_name || '').localeCompare(b.display_name || ''));
    for (const item of fileChildren) {
      html += renderFileRow(item, depth + 1, termLower);
    }
  }
  return html;
}

function renderFileRow(item, depth, term) {
  const indent = 8 + depth * 14;
  const sel = selected === item.item_id ? 'selected' : '';
  const err = item.fetch_error ? 'err' : '';
  return `<div class="tree-row file ${err} ${sel}" data-id="${escapeHTML(item.item_id)}" style="padding-left: ${indent}px">
    <span class="chevron"></span>
    <span class="label">${highlight(item.display_name, term)}</span>
    <span class="vcount" title="${item.fetch_error ? 'fetch error' : 'version count'}">${item.version_count}v</span>
  </div>`;
}

function renderList() {
  const term = $search.value.trim();
  const termLower = term.toLowerCase();

  if (viewMode === 'tree') {
    const root = buildTree();
    annotateMatches(root, termLower);
    const autoExpand = !!termLower;
    const projects = [...root.children.values()].sort((a,b) => a.name.localeCompare(b.name));
    let html = '';
    for (const p of projects) html += renderTreeNode(p, 0, termLower, autoExpand);
    $list.innerHTML = html || '<li class="no-results">No files match.</li>';
    return;
  }

  const matched = termLower
    ? DATA.filter(it => (it.display_name || '').toLowerCase().includes(termLower))
    : DATA;

  if (matched.length === 0) {
    $list.innerHTML = '<li class="no-results">No files match.</li>';
    return;
  }

  $list.innerHTML = matched.map(it => `
    <li class="item ${it.fetch_error ? 'err' : ''}" data-id="${escapeHTML(it.item_id)}">
      <span class="vcount" title="${it.fetch_error ? 'fetch error' : 'version count'}">${it.version_count}v</span>
      <div class="name">${highlight(it.display_name, term)}</div>
      <div class="sub">${escapeHTML(it.project_name || '')} ${escapeHTML(it.folder_path || '')}</div>
    </li>
  `).join('');

  if (selected) {
    const sel = $list.querySelector(`[data-id="${CSS.escape(selected)}"]`);
    if (sel) sel.classList.add('selected');
  }
}

function setViewMode(mode) {
  viewMode = mode;
  localStorage.setItem('fvh.viewMode', mode);
  document.getElementById('view-toggle').textContent = mode === 'flat' ? 'Tree view' : 'Flat view';
  renderList();
}

// The 'a.'-prefixed project_id base64-decodes to 'business:<username>#<numericId>'.
// We need both the username (for the subdomain) and the numeric id (for the URL).
function parseProjectId(projectId) {
  if (!projectId || !projectId.startsWith('a.')) return null;
  try {
    const decoded = atob(projectId.slice(2).replace(/-/g, '+').replace(/_/g, '/'));
    const m = decoded.match(/^([^:]+):([^#]+)#(\d+)$/);
    if (!m) return null;
    return { context: m[1], subdomain: m[2], numericId: m[3] };
  } catch (_) { return null; }
}

// Folder and item URNs are base64url-encoded (URL-safe, no padding) in the
// web hub's URL path.
function b64urlEncode(s) {
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

// NOTE: a "fusion360://" deep-link button was prototyped and removed.
// Findings (so this isn't re-attempted):
//   1. The web hub's "Open in Desktop" button uses an internal JS-to-Fusion
//      channel (`_channel.invokeCommand(OPEN_DESIGN_COMMAND, ...)`), NOT a
//      URL navigation. The channel is only reachable from inside the bundle.
//   2. The bundle's `urlFor()` does build `fusion360://userEmail=...&...`
//      strings, but those strings are consumed by an internal launcher that
//      bypasses browser URL parsing.
//   3. From external HTML: anchor href, window.location.assign, and opaque
//      URI form (`fusion360:key=value...`) were all tried.
//      - `fusion360://key=value...` is malformed per RFC 3986; browsers
//        normalize it by appending `/`, and Fusion's protocol handler
//        rejects the result as invalid.
//      - `fusion360://host/path?key=value...` survives normalization but is
//        still rejected by Fusion's parser.
//      - `fusion360:key=value...` (opaque) doesn't fire the handler at all
//        (OS scheme registration matches only `fusion360://`).
//   4. Verdict: Fusion's protocol handler is gated to the internal channel.
//      External HTML cannot reach it. The web link (buildWebUrl below) is
//      the only deep-link surface that works.

function buildWebUrl(item, version) {
  // URL pattern confirmed from a real navigation:
  //   https://{sub}.autodesk360.com/g/projects/{numericId}/data/{folderB64}/{itemB64}/overview
  // Optional ?time=<ms> pins to a moment inside a specific version's active
  // window. We use createTime + 1s (1ms buffer to clear the save event, 1s for
  // safety). NOT lastModifiedTime -- the API's lastModifiedTime field on a
  // version is when the version's metadata was last touched, not when the
  // version was saved (it can be hours later if a subsequent edit touched it).
  //
  // The version-dropdown highlight requires `historyChangeId`, an internal
  // Apollo-assigned ID. Investigation findings:
  //   - The web hub bundle's only historyChange query is by-ID lookup
  //     (FT_GetVersionOrRevisionHistoryChange) -- one-way.
  //   - Mfg Data API public schema (introspected live via probe_mfg.py) has
  //     ZERO history-related root queries. DesignItem exposes versions but
  //     not historyChanges. The bundle's `historyChange()` query lives on
  //     Fusion Team's private internal GraphQL endpoint, not the public API.
  //   - Verdict: historyChangeId is unreachable from external code. The web
  //     link below correctly pins file + date via ?time=; the dropdown row
  //     will load unhighlighted. This is a hard limit, not a TODO.
  const parsed = parseProjectId(item.project_id);
  if (!parsed) return null;
  if (!item.item_id || !item.folder_id) return null;
  const folderB64 = b64urlEncode(item.folder_id);
  const itemB64   = b64urlEncode(item.item_id);
  let url = `https://${parsed.subdomain}.autodesk360.com/g/projects/`
          + `${parsed.numericId}/data/${folderB64}/${itemB64}/overview`;
  if (version && version.created_time) {
    const t = new Date(version.created_time).getTime();
    if (!isNaN(t)) url += `?time=${t + 1000}`;
  }
  return url;
}

function renderDetail(item) {
  if (!item) {
    $detail.innerHTML = '<p class="empty">Select a file on the left.</p>';
    return;
  }
  const groups = groupByDay(item.versions);
  const groupsHTML = groups.map(g => `
    <div class="date-group">
      <h3>${escapeHTML(g.header)}</h3>
      <table>
        <tbody>${g.versions.map(v => {
          const vUrl = buildWebUrl(item, v);
          return `
          <tr>
            <td class="v">v${v.version_number ?? '?'}</td>
            <td class="t">${escapeHTML(formatTime(v.created_time))}</td>
            <td>${escapeHTML(v.display_name || '')}</td>
            <td class="u">${escapeHTML(v.created_user_name || '')}</td>
            <td class="s">${escapeHTML(formatBytes(v.storage_size))}</td>
            <td class="a">
              ${vUrl ? `<a class="act act-link" href="${escapeHTML(vUrl)}" target="_blank" rel="noopener" title="Open this version in the Autodesk web hub (file + date land correctly; dropdown highlight requires historyChangeId we don't yet have)">&#x1f310;</a>` : ''}
              <button class="act act-copy" type="button" data-urn="${escapeHTML(v.version_urn || '')}" title="Copy version URN to clipboard">&#x1f4cb;</button>
            </td>
          </tr>`;
        }).join('')}</tbody>
      </table>
    </div>
  `).join('');

  const err = item.fetch_error
    ? `<div class="err">Fetch error on items endpoint: ${escapeHTML(item.fetch_error)}</div>`
    : '';

  $detail.innerHTML = `
    <div class="detail-head">
      <h2>${escapeHTML(item.display_name)}</h2>
      <div class="path">${escapeHTML(item.hub_name || '')} / ${escapeHTML(item.project_name || '')}${escapeHTML(item.folder_path || '')}</div>
      <div class="meta">${item.version_count} version${item.version_count === 1 ? '' : 's'} - last modified ${escapeHTML(item.last_modified_time || 'unknown')}</div>
      ${err}
    </div>
    ${groupsHTML || '<p class="empty">No versions recorded for this item.</p>'}
  `;
}

$list.addEventListener('click', e => {
  const folderRow = e.target.closest('.tree-row.folder, .tree-row.project');
  if (folderRow && folderRow.dataset.folder) {
    const path = folderRow.dataset.folder;
    if (expanded.has(path)) expanded.delete(path);
    else expanded.add(path);
    saveExpanded();
    renderList();
    return;
  }
  const fileRow = e.target.closest('.item, .tree-row.file');
  if (!fileRow) return;
  selected = fileRow.dataset.id;
  for (const el of $list.querySelectorAll('.selected')) el.classList.remove('selected');
  fileRow.classList.add('selected');
  const item = DATA.find(it => it.item_id === selected);
  renderDetail(item);
});

document.getElementById('view-toggle').addEventListener('click', () => {
  setViewMode(viewMode === 'flat' ? 'tree' : 'flat');
});

$search.addEventListener('input', renderList);
$search.addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    const first = $list.querySelector('.item, .tree-row.file');
    if (first) first.click();
  }
});

const $toast = document.getElementById('toast');
let toastTimer = null;
function showToast(msg) {
  $toast.textContent = msg;
  $toast.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $toast.classList.remove('show'), 1600);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    // Fallback for older browsers / file:// quirks
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) {}
    document.body.removeChild(ta);
    return ok;
  }
}

$detail.addEventListener('click', async (e) => {
  const copyBtn = e.target.closest('.act-copy');
  if (copyBtn) {
    e.preventDefault();
    const urn = copyBtn.dataset.urn || '';
    if (!urn) { showToast('No URN available'); return; }
    const ok = await copyText(urn);
    showToast(ok ? 'URN copied' : 'Copy failed');
    return;
  }
});

setViewMode(viewMode);
</script>
</body>
</html>
"""


def write_html(data: list[dict], out_path: str) -> None:
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    # </ inside JSON is the only thing that could break out of <script>.
    payload = payload.replace("</", "<\\/")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = (
        HTML_TEMPLATE
        .replace("__DATA_JSON__", payload)
        .replace("__GENERATED_AT__", generated_at)
    )
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Generate fusion_versions.html from fusion_versions.db."
    )
    p.add_argument("--db",  default="./fusion_versions.db",
                   help="Path to the SQLite DB written by fusion_version_history.py.")
    p.add_argument("--out", default="./fusion_versions.html",
                   help="Output HTML path.")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    if not os.path.exists(args.db):
        sys.exit(f"DB not found: {args.db}. Run fusion_version_history.py first.")

    data = load_data(args.db)
    write_html(data, args.out)
    total_versions = sum(it["version_count"] for it in data)
    print(f"Wrote {args.out}")
    print(f"  {len(data):,} items, {total_versions:,} versions")
    print(f"  Double-click the file to open in your browser.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
