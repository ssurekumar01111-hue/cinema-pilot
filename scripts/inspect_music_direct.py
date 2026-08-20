import sys
import os
import json
from google.cloud import bigquery, storage

# Add cinemapilot root to path
sys.path.insert(0, os.path.abspath("cinemapilot"))
from shared.graph_client import ProductionGraphClient
from shared.asset_storage import AssetStorageClient

def inspect_music_cues():
    print("=" * 80)
    print("1. QUERYING BIGQUERY `music_cues` TABLE DIRECTLY")
    print("=" * 80)
    
    bq = bigquery.Client(project="cinemapilot-2026")
    query = """
        SELECT 
            music_cue_id, 
            scene_id, 
            status, 
            gs_uri, 
            lyrics, 
            description, 
            prompt_used, 
            version, 
            updated_at
        FROM `cinemapilot-2026.production_graph.music_cues`
        ORDER BY scene_id
    """
    rows = list(bq.query(query).result())
    print(f"Total music_cues rows in BigQuery: {len(rows)}\n")

    records = []
    for row in rows:
        r = dict(row.items())
        # Format datetime
        if r.get("updated_at"):
            r["updated_at_str"] = r["updated_at"].isoformat()
        records.append(r)
        print(f"--- Record: {r.get('music_cue_id')} ---")
        print(f"  Scene ID:     {r.get('scene_id')}")
        print(f"  Status:       {r.get('status')}")
        print(f"  GS URI:       {r.get('gs_uri')}")
        print(f"  Updated At:   {r.get('updated_at_str')}")
        print(f"  Prompt Used:  {r.get('prompt_used')}")
        print(f"  Lyrics:       {r.get('lyrics')}")
        print(f"  Description:  {r.get('description')}")
        print(f"  Version:      {r.get('version')}")
        print()

    print("=" * 80)
    print("2. FETCHING & ANALYZING ACTUAL GCS AUDIO ASSETS DIRECTLY")
    print("=" * 80)

    gcs = storage.Client(project="cinemapilot-2026")
    storage_client = AssetStorageClient()

    for r in records:
        scene_id = r.get("scene_id")
        status = r.get("status")
        gs_uri = r.get("gs_uri")
        print(f"\n[Scene {scene_id}] Status in DB: '{status}' | URI: {gs_uri}")

        if not gs_uri or not gs_uri.startswith("gs://"):
            print(f"  -> No valid GCS URI present for {scene_id} (Status: {status})")
            continue

        parts = gs_uri.replace("gs://", "").split("/", 1)
        bucket_name = parts[0]
        blob_name = parts[1]

        bucket = gcs.bucket(bucket_name)
        blob = bucket.blob(blob_name)

        if not blob.exists():
            print(f"  -> ERROR: Blob {blob_name} does NOT exist in bucket {bucket_name}!")
            continue

        blob.reload()
        size_bytes = blob.size
        content_type = blob.content_type
        md5_hash = blob.md5_hash
        updated = blob.updated

        print(f"  GCS Bucket:       {bucket_name}")
        print(f"  Blob Name:        {blob_name}")
        print(f"  Size (bytes):     {size_bytes} bytes ({size_bytes / 1024:.2f} KB)")
        print(f"  Content-Type:     {content_type}")
        print(f"  MD5 Hash:         {md5_hash}")
        print(f"  Blob Updated:     {updated}")

        # Download raw bytes
        data = blob.download_as_bytes()
        print(f"  Downloaded bytes: {len(data)} bytes")

        # Sanity check: Check if all bytes are identical / zeros (silence/dummy)
        unique_bytes = len(set(data))
        print(f"  Unique byte values: {unique_bytes} / 256")
        if unique_bytes < 10:
            print("  ⚠️ WARNING: Audio file has suspiciously low byte entropy!")

        # Check MP3 header / ID3 tag / MPEG frames
        is_id3 = data.startswith(b"ID3")
        print(f"  Has ID3 tag:      {is_id3}")

        # Basic MPEG audio frame scan
        # MPEG audio sync word: 11 bits set (0xFF followed by 0xE0..0xFF)
        sync_count = 0
        for i in range(len(data) - 1):
            if data[i] == 0xFF and (data[i+1] & 0xE0) == 0xE0:
                sync_count += 1
        print(f"  MPEG Sync Frames: ~{sync_count}")

        # Calculate estimated duration based on 192kbps (24 KB/s) or 128kbps (16 KB/s)
        dur_192 = (size_bytes * 8) / (192 * 1000)
        dur_128 = (size_bytes * 8) / (128 * 1000)
        print(f"  Estimated duration: ~{dur_192:.2f}s (@192kbps) to ~{dur_128:.2f}s (@128kbps)")

        # Save audio file locally to inspect/verify
        local_dir = os.path.join("cinemapilot", "scripts", "audio_cues")
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, f"{scene_id}.mp3")
        with open(local_path, "wb") as f:
            f.write(data)
        print(f"  Saved locally to: {local_path}")

    print("\n" + "=" * 80)
    print("3. AUDIT EVENTS IN BIGQUERY FOR MUSIC AGENT")
    print("=" * 80)
    audit_query = """
        SELECT 
            event_id, 
            actor_agent, 
            entity_type, 
            entity_id, 
            event_timestamp, 
            before_state, 
            after_state, 
            triggered_agents
        FROM `cinemapilot-2026.production_graph.events`
        WHERE actor_agent = 'music_agent' OR entity_type = 'music_cue'
        ORDER BY event_timestamp DESC
        LIMIT 20
    """
    try:
        ev_rows = list(bq.query(audit_query).result())
        print(f"Found {len(ev_rows)} audit events for music_agent/music_cue:\n")
        for ev in ev_rows:
            d = dict(ev.items())
            print(f"Event ID:   {d.get('event_id')}")
            print(f"Timestamp:  {d.get('event_timestamp')}")
            print(f"Actor:      {d.get('actor_agent')}")
            print(f"Entity:     {d.get('entity_type')} / {d.get('entity_id')}")
            print(f"Before:     {d.get('before_state')}")
            print(f"After:      {d.get('after_state')}")
            print(f"Triggered:  {d.get('triggered_agents')}")
            print("-" * 60)
    except Exception as exc:
        print(f"Failed to query audit events: {exc}")

if __name__ == "__main__":
    inspect_music_cues()
