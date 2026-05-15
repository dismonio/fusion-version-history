"""Introspect Autodesk Manufacturing Data API GraphQL schema.

Probes the live API using the cached APS token to discover the right field
names for fetching historyChanges of a design. Writes results to stdout.

This is a one-shot diagnostic; once we know the schema we'll fold the real
walker into fusion_version_history.py.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import requests

# Reuse the main script's auth so we honor refresh tokens automatically.
import fusion_version_history as fvh

MFG_GRAPHQL = "https://developer.api.autodesk.com/mfg/graphql"


def _token() -> str:
    return fvh._ensure_access_token(reauth=False)


def gql(query: str, variables: dict | None = None) -> dict:
    resp = requests.post(
        MFG_GRAPHQL,
        json={"query": query, "variables": variables or {}},
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    return {"status": resp.status_code, "body": resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else resp.text}


def main() -> int:
    # 1. Introspect the Query root to confirm we can talk to the API.
    print("=" * 60)
    print("STEP 1: Query root fields")
    print("=" * 60)
    r = gql("""{ __schema { queryType { fields { name args { name type { name kind ofType { name kind } } } } } } }""")
    if r["status"] != 200:
        print(f"HTTP {r['status']}")
        print(json.dumps(r["body"], indent=2)[:3000])
        return 1
    fields = r["body"].get("data", {}).get("__schema", {}).get("queryType", {}).get("fields", [])
    history_related = [f for f in fields if "histor" in f["name"].lower()]
    item_related    = [f for f in fields if f["name"].lower() in ("item", "items", "design", "designs", "component", "components")]
    print(f"Total query fields: {len(fields)}")
    print(f"History-related root queries:")
    for f in history_related:
        args = ", ".join(f"{a['name']}: {(a['type'].get('name') or a['type'].get('ofType', {}).get('name'))}" for a in f["args"])
        print(f"  - {f['name']}({args})")
    print(f"Item/design-related root queries:")
    for f in item_related:
        args = ", ".join(f"{a['name']}: {(a['type'].get('name') or a['type'].get('ofType', {}).get('name'))}" for a in f["args"])
        print(f"  - {f['name']}({args})")

    # 2. Inspect the DesignItem type for any history-related fields.
    print()
    print("=" * 60)
    print("STEP 2: DesignItem type fields")
    print("=" * 60)
    r = gql("""{ __type(name: "DesignItem") { name fields { name type { name kind ofType { name kind } } } } }""")
    t = r["body"].get("data", {}).get("__type")
    if t:
        for f in t["fields"]:
            tn = f["type"].get("name") or f["type"].get("ofType", {}).get("name") or f["type"]["kind"]
            marker = "  <-- HISTORY" if "histor" in f["name"].lower() else ""
            print(f"  {f['name']}: {tn}{marker}")
    else:
        print("Type DesignItem not found. Body:")
        print(json.dumps(r["body"], indent=2)[:2000])

    # 3. Inspect Component, Model types too -- the history list may live there.
    for type_name in ("Component", "Model", "DesignItemVersion", "Lineage", "DesignItemHistory"):
        print()
        print("=" * 60)
        print(f"STEP 3: {type_name} type fields")
        print("=" * 60)
        r = gql(f"""{{ __type(name: "{type_name}") {{ name fields {{ name type {{ name kind ofType {{ name kind }} }} }} }} }}""")
        t = r["body"].get("data", {}).get("__type")
        if not t:
            print(f"  (no such type)")
            continue
        for f in t["fields"]:
            tn = f["type"].get("name") or f["type"].get("ofType", {}).get("name") or f["type"]["kind"]
            marker = "  <-- HISTORY" if "histor" in f["name"].lower() else ""
            print(f"  {f['name']}: {tn}{marker}")

    # 4. One real-data probe: use a known item from the DB and try the simplest history-ish query.
    db_path = "./fusion_versions.db"
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT i.item_id, h.hub_id FROM items i "
            "JOIN projects p ON p.project_id=i.project_id "
            "JOIN hubs h ON h.hub_id=p.hub_id LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            item_id, dm_hub_id = row
            print()
            print("=" * 60)
            print("STEP 4: Real-data probe -- get GraphQL hubId from DM hubId, then query item")
            print("=" * 60)
            print(f"DM hub_id : {dm_hub_id}")
            print(f"item_id   : {item_id}")
            r = gql(
                "query($forgeId: ID!) { hubByDataManagementAPIId(dataManagementAPIHubId: $forgeId) { id name } }",
                {"forgeId": dm_hub_id},
            )
            print("hubByDataManagementAPIId response:")
            print(json.dumps(r["body"], indent=2)[:1500])
            gql_hub = r["body"].get("data", {}).get("hubByDataManagementAPIId", {}).get("id")
            if gql_hub:
                print(f"GraphQL hubId: {gql_hub}")
                r2 = gql(
                    "query($hubId: ID!, $itemId: ID!) { item(hubId: $hubId, itemId: $itemId) { __typename ... on DesignItem { name tipRootModel { id component { id } } } } }",
                    {"hubId": gql_hub, "itemId": item_id},
                )
                print("\nitem() response:")
                print(json.dumps(r2["body"], indent=2)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
