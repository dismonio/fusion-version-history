"""Pull Fusion 360 file/version history from Autodesk Platform Services.

Phase 1 (diagnostic): --inspect "FileName" dumps raw /versions JSON for one file.
Phase 2 (bulk):       no flags -- walks the chosen hub and writes SQLite + CSV.

See README.md for setup.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import http.server
import json
import os
import secrets
import socket
import sqlite3
import sys
import time
import urllib.parse
import webbrowser
from typing import Iterable, Iterator

import requests


# ---- region: config ---------------------------------------------------------

APS_AUTH_BASE = "https://developer.api.autodesk.com/authentication/v2"
APS_API_BASE  = "https://developer.api.autodesk.com"
REDIRECT_URI  = "http://localhost:8080/"
SCOPE         = "data:read"
CALLBACK_PORT = 8080

FUSION_EXTENSIONS = {".f3d", ".f3z"}

VERBOSE = False


def _appdata_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "fusion-version-history")
    os.makedirs(path, exist_ok=True)
    return path


def _token_path() -> str:
    return os.path.join(_appdata_dir(), "token.json")


def _client_id() -> str:
    cid = os.environ.get("APS_CLIENT_ID")
    if not cid:
        sys.exit("APS_CLIENT_ID environment variable is not set. See README.md.")
    return cid


def _client_secret() -> str | None:
    """Optional. Set APS_CLIENT_SECRET when the APS app is a 'Traditional Web App'
    (confidential client). Leave unset when it's a 'Desktop, Mobile, Single-Page
    App' (public client) and PKCE alone is sufficient."""
    return os.environ.get("APS_CLIENT_SECRET")


def _token_endpoint_auth() -> tuple[dict, dict]:
    """Return (extra_headers, extra_form_fields) for token endpoint authentication.

    With a client secret set, send HTTP Basic auth (per OAuth2 RFC 6749). Without,
    rely on PKCE only and include client_id in the form body."""
    cid = _client_id()
    secret = _client_secret()
    if secret:
        creds = base64.b64encode(f"{cid}:{secret}".encode("ascii")).decode("ascii")
        return {"Authorization": f"Basic {creds}"}, {}
    return {}, {"client_id": cid}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _logv(msg: str) -> None:
    if VERBOSE:
        print(msg, flush=True)


# ---- region: PKCE auth ------------------------------------------------------

def _generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.server.auth_code  = params.get("code",  [None])[0]   # type: ignore[attr-defined]
        self.server.auth_state = params.get("state", [None])[0]   # type: ignore[attr-defined]
        self.server.auth_error = params.get("error", [None])[0]   # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = b"<!doctype html><meta charset=utf-8><title>OK</title>" \
               b"<h2>Authentication received. You can close this tab.</h2>"
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return  # silence default access log


def _make_callback_server() -> http.server.HTTPServer:
    # Prefer IPv6 dual-stack so 'localhost' resolves either way; fall back to IPv4.
    try:
        class _DualStack(http.server.HTTPServer):
            address_family = socket.AF_INET6
            def server_bind(self) -> None:
                try:
                    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except (AttributeError, OSError):
                    pass
                super().server_bind()
        srv = _DualStack(("::1", CALLBACK_PORT), _CallbackHandler)
        srv.auth_code = srv.auth_state = srv.auth_error = None  # type: ignore[attr-defined]
        return srv
    except OSError:
        srv = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
        srv.auth_code = srv.auth_state = srv.auth_error = None  # type: ignore[attr-defined]
        return srv


def _run_auth_flow() -> dict:
    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)
    authorize_url = (
        f"{APS_AUTH_BASE}/authorize?"
        + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": _client_id(),
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        })
    )

    server = _make_callback_server()
    _log("Opening browser for Autodesk login...")
    _log(f"If the browser does not open, paste this URL into it manually:\n  {authorize_url}")
    webbrowser.open(authorize_url)
    server.handle_request()

    if server.auth_error:                                              # type: ignore[attr-defined]
        sys.exit(f"OAuth error from Autodesk: {server.auth_error}")    # type: ignore[attr-defined]
    if not server.auth_code:                                           # type: ignore[attr-defined]
        sys.exit("No authorization code received from callback.")
    if server.auth_state != state:                                     # type: ignore[attr-defined]
        sys.exit("OAuth state mismatch -- possible tampering. Aborting.")

    extra_headers, extra_form = _token_endpoint_auth()
    form = {
        "grant_type": "authorization_code",
        "code": server.auth_code,                                      # type: ignore[attr-defined]
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
        **extra_form,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded", **extra_headers}
    resp = requests.post(f"{APS_AUTH_BASE}/token", data=form, headers=headers, timeout=30)
    if not resp.ok:
        hint = ""
        if resp.status_code == 401 and not _client_secret():
            hint = (
                "\n\nThis usually means the APS app is registered as a 'Traditional Web App'\n"
                "(confidential client), which requires a client secret on the token exchange.\n"
                "Fix: in https://aps.autodesk.com/myapps copy the Client Secret for this app,\n"
                "then in PowerShell run:  setx APS_CLIENT_SECRET \"...\"  and open a new terminal.\n"
                "Alternative: re-register the app as 'Desktop, Mobile, Single-Page App' for\n"
                "pure PKCE without a secret."
            )
        sys.exit(f"Token exchange failed: {resp.status_code} {resp.text}{hint}")
    tokens = resp.json()
    tokens["expires_at"] = int(time.time()) + int(tokens["expires_in"]) - 60
    _save_tokens(tokens)
    _log("Auth successful; tokens cached.")
    return tokens


def _save_tokens(tokens: dict) -> None:
    path = _token_path()
    tmp_path = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(tokens, fh)
    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_tokens() -> dict | None:
    path = _token_path()
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _refresh_tokens(tokens: dict) -> dict:
    extra_headers, extra_form = _token_endpoint_auth()
    form = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "scope": SCOPE,
        **extra_form,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded", **extra_headers}
    resp = requests.post(f"{APS_AUTH_BASE}/token", data=form, headers=headers, timeout=30)
    if not resp.ok:
        _log(f"Refresh failed ({resp.status_code}); falling back to interactive auth.")
        return _run_auth_flow()
    new_tokens = resp.json()
    # Some APS responses omit the refresh_token on rotation; keep the existing one.
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = tokens["refresh_token"]
    new_tokens["expires_at"] = int(time.time()) + int(new_tokens["expires_in"]) - 60
    _save_tokens(new_tokens)
    return new_tokens


def _ensure_access_token(reauth: bool = False) -> str:
    if reauth:
        try:
            os.remove(_token_path())
        except FileNotFoundError:
            pass
    tokens = _load_tokens()
    if tokens is None:
        tokens = _run_auth_flow()
    if int(time.time()) >= tokens.get("expires_at", 0):
        tokens = _refresh_tokens(tokens)
    return tokens["access_token"]


# ---- region: HTTP client ----------------------------------------------------

_ACCESS_TOKEN: str | None = None


def _set_access_token(token: str) -> None:
    global _ACCESS_TOKEN
    _ACCESS_TOKEN = token


def _aps_request(url: str) -> dict:
    """GET a fully-formed APS URL with retry on 429/5xx. Returns parsed JSON."""
    backoff = [1, 2, 4, 8, 16]
    last_resp = None
    for attempt in range(5):
        _logv(f"  GET {url}")
        last_resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {_ACCESS_TOKEN}"},
            timeout=60,
        )
        if last_resp.status_code == 429:
            retry_after = int(last_resp.headers.get("Retry-After", "60"))
            _log(f"  429 Too Many Requests -- sleeping {retry_after}s")
            time.sleep(min(retry_after, 60))
            continue
        if 500 <= last_resp.status_code < 600 and attempt < 4:
            delay = backoff[attempt]
            _log(f"  HTTP {last_resp.status_code} -- backoff {delay}s")
            time.sleep(delay)
            continue
        break
    if last_resp is None:
        raise RuntimeError("No HTTP response received.")
    if not last_resp.ok:
        raise RuntimeError(f"APS GET {url} failed: {last_resp.status_code} {last_resp.text[:300]}")
    return last_resp.json()


def aps_get_path(path_template: str, *path_ids: str) -> dict:
    """Build an APS URL by URL-encoding each path-position ID, then GET it.

    Path-position IDs (item URNs, version URNs) contain ':', '?', '=', '/' --
    every one must go through quote(id, safe='') or the API returns 404.
    """
    encoded = [urllib.parse.quote(str(p), safe="") for p in path_ids]
    url = APS_API_BASE + path_template.format(*encoded)
    return _aps_request(url)


def aps_get_url(url: str) -> dict:
    """GET a complete URL (e.g. links.next.href) without re-encoding it."""
    parsed = urllib.parse.urlparse(url)
    api_base = urllib.parse.urlparse(APS_API_BASE)
    if parsed.scheme != "https" or parsed.netloc.lower() != api_base.netloc.lower():
        raise RuntimeError(f"Refusing to follow non-APS pagination URL: {url}")
    return _aps_request(url)


def _paginate(first: dict) -> Iterator[dict]:
    """Yield .data entries across all pages, following links.next.href."""
    page = first
    while True:
        for entry in page.get("data", []) or []:
            yield entry
        next_link = (((page.get("links") or {}).get("next") or {}).get("href"))
        if not next_link:
            return
        page = aps_get_url(next_link)


def _paginate_path(path_template: str, *path_ids: str) -> Iterator[dict]:
    yield from _paginate(aps_get_path(path_template, *path_ids))


# ---- region: API walkers ----------------------------------------------------

def list_hubs() -> list[dict]:
    return list(_paginate_path("/project/v1/hubs"))


def list_projects(hub_id: str) -> list[dict]:
    return list(_paginate_path("/project/v1/hubs/{0}/projects", hub_id))


def list_top_folders(hub_id: str, project_id: str) -> list[dict]:
    return list(_paginate_path(
        "/project/v1/hubs/{0}/projects/{1}/topFolders", hub_id, project_id
    ))


def list_folder_contents(project_id: str, folder_id: str) -> list[dict]:
    return list(_paginate_path(
        "/data/v1/projects/{0}/folders/{1}/contents", project_id, folder_id
    ))


def list_versions(project_id: str, item_id: str) -> list[dict]:
    return list(_paginate_path(
        "/data/v1/projects/{0}/items/{1}/versions", project_id, item_id
    ))


def walk_folder(project_id: str, folder_id: str,
                path_parts: tuple[str, ...]) -> Iterator[tuple[str, dict, str, str]]:
    """Recursively walk a folder, yielding (kind, entry, path_str, parent_folder_id).

    kind is 'folder' (subfolder discovered) or 'item' (file discovered).
    For 'folder' events, path_str is the new folder's full path; the caller
    should upsert it before relying on foreign-key joins.
    For 'item' events, path_str is the parent folder's full path.
    parent_folder_id is the folder_id passed into this walk call.
    """
    folder_path_here = "/" + "/".join(path_parts) if path_parts else "/"
    for entry in list_folder_contents(project_id, folder_id):
        kind = entry.get("type")
        attrs = entry.get("attributes") or {}
        name = attrs.get("displayName") or attrs.get("name") or "(unnamed)"
        if kind == "folders":
            sub_path = path_parts + (name,)
            sub_path_str = "/" + "/".join(sub_path)
            yield "folder", entry, sub_path_str, folder_id
            yield from walk_folder(project_id, entry["id"], sub_path)
        elif kind == "items":
            yield "item", entry, folder_path_here, folder_id


# ---- region: persistence ----------------------------------------------------

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS hubs (
  hub_id TEXT PRIMARY KEY, name TEXT, hub_type TEXT, region TEXT
);
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY, hub_id TEXT REFERENCES hubs(hub_id),
  name TEXT, project_type TEXT
);
CREATE TABLE IF NOT EXISTS folders (
  folder_id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(project_id),
  parent_folder_id TEXT, name TEXT, path TEXT
);
CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY, project_id TEXT, folder_id TEXT,
  display_name TEXT, extension_type TEXT, file_type TEXT,
  last_modified_time TEXT, hidden INTEGER, fetch_error TEXT
);
CREATE TABLE IF NOT EXISTS versions (
  item_id TEXT, version_number INTEGER, version_urn TEXT PRIMARY KEY,
  display_name TEXT, created_time TEXT, created_user_name TEXT,
  last_modified_time TEXT, storage_size INTEGER, file_type TEXT,
  raw_attributes_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_versions_item ON versions(item_id, version_number);
CREATE INDEX IF NOT EXISTS idx_items_name    ON items(display_name);
"""


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA_DDL)
    conn.commit()
    return conn


def upsert_hub(conn, h: dict) -> None:
    a = h.get("attributes") or {}
    conn.execute(
        """INSERT INTO hubs(hub_id, name, hub_type, region) VALUES(?,?,?,?)
           ON CONFLICT(hub_id) DO UPDATE SET name=excluded.name,
             hub_type=excluded.hub_type, region=excluded.region""",
        (h["id"], a.get("name"), a.get("extension", {}).get("type"), a.get("region")),
    )


def upsert_project(conn, hub_id: str, p: dict) -> None:
    a = p.get("attributes") or {}
    conn.execute(
        """INSERT INTO projects(project_id, hub_id, name, project_type) VALUES(?,?,?,?)
           ON CONFLICT(project_id) DO UPDATE SET hub_id=excluded.hub_id,
             name=excluded.name, project_type=excluded.project_type""",
        (p["id"], hub_id, a.get("name"), a.get("extension", {}).get("type")),
    )


def upsert_folder(conn, project_id: str, folder_id: str, parent_folder_id: str | None,
                  name: str, path: str) -> None:
    conn.execute(
        """INSERT INTO folders(folder_id, project_id, parent_folder_id, name, path)
           VALUES(?,?,?,?,?)
           ON CONFLICT(folder_id) DO UPDATE SET project_id=excluded.project_id,
             parent_folder_id=excluded.parent_folder_id, name=excluded.name,
             path=excluded.path""",
        (folder_id, project_id, parent_folder_id, name, path),
    )


def upsert_item(conn, project_id: str, folder_id: str | None, item: dict,
                fetch_error: str | None = None) -> None:
    a = item.get("attributes") or {}
    ext = a.get("extension") or {}
    conn.execute(
        """INSERT INTO items(item_id, project_id, folder_id, display_name,
             extension_type, file_type, last_modified_time, hidden, fetch_error)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(item_id) DO UPDATE SET project_id=excluded.project_id,
             folder_id=excluded.folder_id, display_name=excluded.display_name,
             extension_type=excluded.extension_type, file_type=excluded.file_type,
             last_modified_time=excluded.last_modified_time, hidden=excluded.hidden,
             fetch_error=excluded.fetch_error""",
        (
            item["id"], project_id, folder_id,
            a.get("displayName"),
            ext.get("type"),
            a.get("fileType"),
            a.get("lastModifiedTime"),
            1 if a.get("hidden") else 0,
            fetch_error,
        ),
    )


def upsert_version(conn, item_id: str, v: dict) -> None:
    a = v.get("attributes") or {}
    ext = a.get("extension") or {}
    raw_version = a.get("versionNumber")
    try:
        version_number = int(raw_version) if raw_version is not None else None
    except (TypeError, ValueError):
        version_number = None
    conn.execute(
        """INSERT INTO versions(item_id, version_number, version_urn, display_name,
             created_time, created_user_name, last_modified_time, storage_size,
             file_type, raw_attributes_json)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(version_urn) DO UPDATE SET item_id=excluded.item_id,
             version_number=excluded.version_number, display_name=excluded.display_name,
             created_time=excluded.created_time, created_user_name=excluded.created_user_name,
             last_modified_time=excluded.last_modified_time, storage_size=excluded.storage_size,
             file_type=excluded.file_type, raw_attributes_json=excluded.raw_attributes_json""",
        (
            item_id,
            version_number,
            v["id"],
            a.get("displayName"),
            a.get("createTime") or a.get("createdTime"),
            a.get("createUserName") or a.get("createdUserName"),
            a.get("lastModifiedTime"),
            a.get("storageSize"),
            ext.get("type"),
            json.dumps(a, separators=(",", ":")),
        ),
    )


CSV_COLUMNS = [
    "hub_name", "project_name", "folder_path", "item_name", "item_extension",
    "version_number", "version_display_name", "version_created_time",
    "version_created_user", "version_last_modified", "version_storage_bytes",
    "item_id", "version_urn",
]

CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe_cell(value):
    if isinstance(value, str) and value.startswith(CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def _csv_safe_row(row: Iterable) -> list:
    return [_csv_safe_cell(value) for value in row]


def export_csv(db_path: str, csv_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT
              h.name              AS hub_name,
              p.name              AS project_name,
              COALESCE(f.path, '/') AS folder_path,
              i.display_name      AS item_name,
              i.extension_type    AS item_extension,
              v.version_number    AS version_number,
              v.display_name      AS version_display_name,
              v.created_time      AS version_created_time,
              v.created_user_name AS version_created_user,
              v.last_modified_time AS version_last_modified,
              v.storage_size      AS version_storage_bytes,
              v.item_id           AS item_id,
              v.version_urn       AS version_urn
            FROM versions v
            JOIN items    i ON i.item_id    = v.item_id
            JOIN projects p ON p.project_id = i.project_id
            JOIN hubs     h ON h.hub_id     = p.hub_id
            LEFT JOIN folders f ON f.folder_id = i.folder_id
            ORDER BY hub_name, project_name, folder_path, item_name,
                     COALESCE(v.version_number, 999999), v.created_time
        """).fetchall()
    finally:
        conn.close()

    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(_csv_safe_row(row) for row in rows)
    return len(rows)


# ---- region: diagnostic mode ------------------------------------------------

def _fusion_filter(item: dict) -> bool:
    ext_type = ((item.get("attributes") or {}).get("extension") or {}).get("type", "")
    # APS returns extension.type like 'items:autodesk.fusion360:Design' for Fusion;
    # uploaded files have other types. We match by filename extension too.
    name = ((item.get("attributes") or {}).get("displayName") or "").lower()
    if "fusion360" in (ext_type or "").lower():
        return True
    for ext in FUSION_EXTENSIONS:
        if name.endswith(ext):
            return True
    return False


def _find_items_by_name(name: str) -> list[dict]:
    """Walk every hub/project/folder collecting items matching name.

    Two-pass: case-insensitive exact match on displayName first; if zero
    matches, fall back to case-insensitive substring match.
    """
    target = name.casefold()
    exact: list[dict] = []
    substring: list[dict] = []

    for hub in list_hubs():
        hub_name = (hub.get("attributes") or {}).get("name", "?")
        for proj in list_projects(hub["id"]):
            proj_name = (proj.get("attributes") or {}).get("name", "?")
            for top in list_top_folders(hub["id"], proj["id"]):
                top_attrs = top.get("attributes") or {}
                top_name = top_attrs.get("displayName") or top_attrs.get("name") or "(root)"
                for event_kind, entry, folder_path, _parent in walk_folder(
                    proj["id"], top["id"], (top_name,)
                ):
                    if event_kind != "item":
                        continue
                    display = ((entry.get("attributes") or {}).get("displayName") or "")
                    cf = display.casefold()
                    record = {
                        "hub_name": hub_name,
                        "project_id": proj["id"],
                        "project_name": proj_name,
                        "folder_path": folder_path,
                        "item": entry,
                    }
                    if cf == target:
                        exact.append(record)
                    elif target in cf:
                        substring.append(record)

    return exact if exact else substring


def _prompt_choice(candidates: list[dict]) -> dict:
    for idx, c in enumerate(candidates, start=1):
        display = (c["item"].get("attributes") or {}).get("displayName", "?")
        print(f"  [{idx}] {c['hub_name']} / {c['project_name']} / {c['folder_path']} / {display}")
    while True:
        raw = input(f"Pick a number 1-{len(candidates)}: ").strip()
        try:
            n = int(raw)
            if 1 <= n <= len(candidates):
                return candidates[n - 1]
        except ValueError:
            pass
        print("  Invalid choice; try again.")


def cmd_inspect(name_or_id: str, by_id: bool) -> int:
    if by_id:
        # We don't know the project_id; ask the user via search over hubs.
        # Cheap: walk hubs/projects looking for an item with matching id.
        match = None
        for hub in list_hubs():
            for proj in list_projects(hub["id"]):
                try:
                    versions = list_versions(proj["id"], name_or_id)
                except RuntimeError:
                    continue
                if versions is not None:
                    match = {"project_id": proj["id"], "item_id": name_or_id, "versions": versions}
                    break
            if match:
                break
        if not match:
            print(f"Could not locate item {name_or_id} in any project.")
            return 2
        chosen_project = match["project_id"]
        chosen_item    = match["item_id"]
        chosen_display = name_or_id
    else:
        print(f"Searching for items matching: {name_or_id!r} ...")
        candidates = _find_items_by_name(name_or_id)
        if not candidates:
            print("No matching items found.")
            return 2
        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            print(f"Found {len(candidates)} matches:")
            chosen = _prompt_choice(candidates)
        chosen_project = chosen["project_id"]
        chosen_item    = chosen["item"]["id"]
        chosen_display = (chosen["item"].get("attributes") or {}).get("displayName", "?")
        print(f"Selected: {chosen['hub_name']} / {chosen['project_name']} "
              f"/ {chosen['folder_path']} / {chosen_display}")

    versions = list_versions(chosen_project, chosen_item)
    # safe filename: replace anything weird with _
    safe_id = "".join(c if c.isalnum() else "_" for c in chosen_item)[:120]
    out_path = f"inspect_{safe_id}.json"
    payload = {
        "item_id": chosen_item,
        "project_id": chosen_project,
        "display_name": chosen_display,
        "version_count": len(versions),
        "versions": versions,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nWrote raw JSON to: {out_path}")
    print(json.dumps(payload, indent=2)[:8000])
    print("..." if len(json.dumps(payload)) > 8000 else "")

    nums: list[int] = []
    missing = 0
    for v in versions:
        n = (v.get("attributes") or {}).get("versionNumber")
        if isinstance(n, int):
            nums.append(n)
        else:
            try:
                nums.append(int(n))
            except (TypeError, ValueError):
                missing += 1

    print("\n--- diagnostic summary ---")
    print(f"  total versions returned   : {len(versions)}")
    if nums:
        print(f"  min versionNumber         : {min(nums)}")
        print(f"  max versionNumber         : {max(nums)}")
        gap_count = max(nums) - min(nums) + 1 - len(set(nums))
        if gap_count:
            print(f"  gaps in numbering         : {gap_count} (expected 0 if all auto-saves retained)")
    print(f"  missing/non-integer count : {missing}")
    if missing:
        print("  WARNING: at least one version has no integer versionNumber.")
        print("  This is the failure mode we're checking for. If most versions")
        print("  are missing numbers, the pre-migration history is likely gone")
        print("  from the API and the full walk will not help reconciliation.")
    elif len(versions) < 3:
        print("  WARNING: very few versions returned for a file you remember")
        print("  saving many times -- the API may be returning only post-migration")
        print("  'Create version' snapshots. Confirm against memory before bulk run.")
    return 0


# ---- region: bulk walk ------------------------------------------------------

def _select_hub(hubs: list[dict], hub_filter: str | None) -> dict:
    if hub_filter:
        substr = hub_filter.casefold()
        matches = [
            h for h in hubs
            if substr in ((h.get("attributes") or {}).get("name", "")).casefold()
        ]
        if not matches:
            sys.exit(f"No hubs matched --hub {hub_filter!r}.")
        if len(matches) > 1:
            sys.exit(f"--hub {hub_filter!r} matched {len(matches)} hubs; be more specific.")
        return matches[0]
    if len(hubs) == 1:
        return hubs[0]
    print(f"Found {len(hubs)} hubs:")
    for idx, h in enumerate(hubs, start=1):
        a = h.get("attributes") or {}
        print(f"  [{idx}] {a.get('name', '?')}  ({(a.get('extension') or {}).get('type', '?')})")
    while True:
        raw = input(f"Pick a hub 1-{len(hubs)}: ").strip()
        try:
            n = int(raw)
            if 1 <= n <= len(hubs):
                return hubs[n - 1]
        except ValueError:
            pass
        print("  Invalid choice; try again.")


def cmd_walk(db_path: str, csv_path: str, hub_filter: str | None,
             include_non_fusion: bool, since_last_run: bool, dry_run: bool) -> int:
    conn = None if dry_run else init_db(db_path)

    hubs = list_hubs()
    if not hubs:
        sys.exit("No hubs returned. Check that APS_CLIENT_ID has Data Management API access.")
    hub = _select_hub(hubs, hub_filter)
    hub_attrs = hub.get("attributes") or {}
    _log(f"\nUsing hub: {hub_attrs.get('name')}  ({hub['id']})")
    if conn:
        upsert_hub(conn, hub)
        conn.commit()

    # Cache of items.last_modified_time keyed by item_id for --since-last-run.
    prev_modified: dict[str, str] = {}
    if since_last_run and conn:
        for row in conn.execute("SELECT item_id, last_modified_time FROM items"):
            if row[1]:
                prev_modified[row[0]] = row[1]
        if not prev_modified:
            _log("--since-last-run: existing DB is empty; doing full fetch instead.")

    projects = list_projects(hub["id"])
    _log(f"Found {len(projects)} project(s) in hub.\n")

    total_items = 0
    total_versions = 0
    total_failed   = 0
    total_skipped  = 0

    for pi, proj in enumerate(projects, start=1):
        proj_attrs = proj.get("attributes") or {}
        proj_name  = proj_attrs.get("name", "?")
        _log(f"=== [{pi}/{len(projects)}] Project: {proj_name} ({proj['id']}) ===")
        if conn:
            upsert_project(conn, hub["id"], proj)
            conn.commit()

        top_folders = list_top_folders(hub["id"], proj["id"])
        if conn:
            for tf in top_folders:
                tf_attrs = tf.get("attributes") or {}
                tf_name  = tf_attrs.get("displayName") or tf_attrs.get("name") or "(root)"
                upsert_folder(conn, proj["id"], tf["id"], None, tf_name, "/" + tf_name)
            conn.commit()

        # Walk every top folder, collecting items and upserting subfolders inline.
        items_in_project: list[tuple[dict, str, str]] = []   # (item, folder_path, parent_folder_id)
        for tf in top_folders:
            tf_attrs = tf.get("attributes") or {}
            tf_name  = tf_attrs.get("displayName") or tf_attrs.get("name") or "(root)"
            for event_kind, entry, path_str, parent_folder_id in walk_folder(
                proj["id"], tf["id"], (tf_name,)
            ):
                if event_kind == "folder":
                    if conn:
                        sub_attrs = entry.get("attributes") or {}
                        sub_name = sub_attrs.get("displayName") or sub_attrs.get("name") or "(unnamed)"
                        upsert_folder(conn, proj["id"], entry["id"], parent_folder_id, sub_name, path_str)
                elif event_kind == "item":
                    items_in_project.append((entry, path_str, parent_folder_id))
        if conn:
            conn.commit()

        if not include_non_fusion:
            items_in_project = [t for t in items_in_project if _fusion_filter(t[0])]

        for ii, (item, fpath, parent_folder_id) in enumerate(items_in_project, start=1):
            attrs = item.get("attributes") or {}
            display = attrs.get("displayName", "?")
            if conn:
                upsert_item(conn, proj["id"], parent_folder_id, item)
            total_items += 1

            if dry_run:
                _log(f"  [{ii}/{len(items_in_project)}] {fpath} {display} (dry-run)")
                continue

            cur_modified = attrs.get("lastModifiedTime")
            if since_last_run and prev_modified.get(item["id"]) == cur_modified and cur_modified:
                total_skipped += 1
                _log(f"  [{ii}/{len(items_in_project)}] {fpath} {display} -- unchanged, skipped")
                continue

            try:
                versions = list_versions(proj["id"], item["id"])
            except RuntimeError as e:
                msg = str(e)[:300]
                if conn:
                    upsert_item(conn, proj["id"], parent_folder_id, item, fetch_error=msg)
                    conn.commit()
                _log(f"  [{ii}/{len(items_in_project)}] {fpath} {display} -- FAILED: {msg[:120]}")
                total_failed += 1
                continue

            for v in versions:
                if conn:
                    upsert_version(conn, item["id"], v)
            total_versions += len(versions)
            if conn:
                conn.commit()
            _log(f"  [{ii}/{len(items_in_project)}] {fpath} {display} ({len(versions)} versions)")

    if conn:
        conn.close()

    if dry_run:
        _log(f"\nDry-run summary: walked {total_items} item(s) across {len(projects)} project(s). "
             f"No versions fetched, no CSV written.")
        return 0

    n_csv = export_csv(db_path, csv_path)
    _log(f"\n--- summary ---")
    _log(f"  hub               : {hub_attrs.get('name')}")
    _log(f"  projects          : {len(projects)}")
    _log(f"  items processed   : {total_items}")
    _log(f"  versions fetched  : {total_versions}")
    _log(f"  items skipped     : {total_skipped}")
    _log(f"  items failed      : {total_failed}")
    _log(f"  db                : {os.path.abspath(db_path)}")
    _log(f"  csv               : {os.path.abspath(csv_path)} ({n_csv} rows)")
    return 0


# ---- region: main -----------------------------------------------------------

def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Pull Fusion 360 file/version history from Autodesk Platform Services.",
    )
    p.add_argument("--inspect", metavar="ITEM_NAME",
                   help="Diagnostic mode: dump raw /versions JSON for one item by display name.")
    p.add_argument("--inspect-item", metavar="ITEM_ID",
                   help="Diagnostic mode: dump raw /versions JSON for one item by item URN/ID.")
    p.add_argument("--hub", metavar="NAME",
                   help="Case-insensitive substring match for hub name (prompts if omitted and >1 hub).")
    p.add_argument("--db",  metavar="PATH", default="./fusion_versions.db",
                   help="SQLite DB path (updated in place across runs).")
    p.add_argument("--csv", metavar="PATH", default="./fusion_versions.csv",
                   help="CSV path (regenerated from DB each run).")
    p.add_argument("--reauth", action="store_true",
                   help="Delete the cached token and re-run the browser auth flow.")
    p.add_argument("--since-last-run", action="store_true",
                   help="Skip /versions for items whose last_modified_time is unchanged.")
    p.add_argument("--include-non-fusion", action="store_true",
                   help="Include STEP/IGES/etc. (default filters to .f3d/.f3z and Fusion designs).")
    p.add_argument("--dry-run", action="store_true",
                   help="Walk folders and count items, skip /versions and DB writes.")
    p.add_argument("--verbose", action="store_true",
                   help="Log every HTTP request and retry.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    VERBOSE = args.verbose

    token = _ensure_access_token(reauth=args.reauth)
    _set_access_token(token)

    if args.inspect_item:
        return cmd_inspect(args.inspect_item, by_id=True)
    if args.inspect:
        return cmd_inspect(args.inspect, by_id=False)

    return cmd_walk(
        db_path=args.db,
        csv_path=args.csv,
        hub_filter=args.hub,
        include_non_fusion=args.include_non_fusion,
        since_last_run=args.since_last_run,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
