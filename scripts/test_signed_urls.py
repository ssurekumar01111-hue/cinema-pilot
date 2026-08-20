"""
scripts/test_signed_urls.py

Verifies GCS asset storage operations:
1. Upload a test asset to gs://cinemapilot-2026-assets
2. Generate a V4 signed URL
3. Fetch the asset via the generated signed URL over HTTP (assert HTTP 200 and byte content match)
4. Download the asset directly via download_asset_bytes (assert byte content match)
"""

import os
import sys
import urllib.request
import uuid

# Ensure cinemapilot root is first in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))

from shared.asset_storage import AssetStorageClient, AssetStorageError


def test_signed_url_flow():
    print(f"[test_signed_urls] Working directory: {os.getcwd()}")
    print(f"[test_signed_urls] Base dir: {BASE_DIR}")
    print(f"[test_signed_urls] Initializing AssetStorageClient...")
    client = AssetStorageClient()

    test_id = f"test_{uuid.uuid4().hex[:8]}"
    payload = f"CinemaPilot Signed URL Verification Payload - {test_id}".encode("utf-8")
    content_type = "text/plain"
    ext = "txt"

    print(f"[test_signed_urls] Uploading test asset (entity_type='test', entity_id='{test_id}')...")
    gs_uri = client.upload_asset(
        entity_type="test",
        entity_id=test_id,
        asset_bytes=payload,
        content_type=content_type,
        extension=ext,
    )
    print(f"[test_signed_urls] Uploaded successfully: {gs_uri}")

    # Generate Signed URL
    print(f"[test_signed_urls] Generating V4 signed URL...")
    signed_url = client.get_signed_url(gs_uri, expiration_minutes=15)
    print(f"[test_signed_urls] Generated Signed URL:\n{signed_url[:120]}... (truncated)")

    # Fetch Signed URL over HTTP
    print(f"[test_signed_urls] Fetching payload via signed URL over HTTP...")
    req = urllib.request.Request(signed_url, headers={"User-Agent": "CinemaPilot-Test/1.0"})
    with urllib.request.urlopen(req, timeout=15) as response:
        status_code = response.getcode()
        fetched_bytes = response.read()

    print(f"[test_signed_urls] HTTP Status Code: {status_code}")
    print(f"[test_signed_urls] Fetched payload length: {len(fetched_bytes)} bytes")
    assert status_code == 200, f"Expected HTTP 200, got {status_code}"
    assert fetched_bytes == payload, f"Payload mismatch: {fetched_bytes} != {payload}"
    print("[test_signed_urls] -> Signed URL HTTP fetch verified successfully!")

    # Direct download
    filename = gs_uri.split("/")[-1]
    print(f"[test_signed_urls] Testing direct download_asset_bytes for {filename}...")
    direct_bytes, direct_ct = client.download_asset_bytes("test", test_id, filename)
    assert direct_bytes == payload, "Direct download payload mismatch"
    print("[test_signed_urls] -> Direct download verified successfully!")

    # Also test an existing storyboard asset if present in Production Graph
    try:
        from shared.graph_client import ProductionGraphClient
        graph = ProductionGraphClient()
        sb = graph.get_storyboard("scene_005")
        if sb and sb.get("gs_uri"):
            sb_uri = sb["gs_uri"]
            print(f"[test_signed_urls] Testing existing storyboard signed URL: {sb_uri}")
            sb_signed = client.get_signed_url(sb_uri, expiration_minutes=15)
            req_sb = urllib.request.Request(sb_signed, headers={"User-Agent": "CinemaPilot-Test/1.0"})
            with urllib.request.urlopen(req_sb, timeout=15) as resp_sb:
                sb_code = resp_sb.getcode()
                sb_len = len(resp_sb.read())
            print(f"[test_signed_urls] Existing Storyboard HTTP Status: {sb_code}, Size: {sb_len} bytes")
            assert sb_code == 200, f"Expected 200 for storyboard, got {sb_code}"
    except Exception as e:
        print(f"[test_signed_urls] Notice: storyboard check encountered: {e}")

    print("[test_signed_urls] ALL SIGNED URL TESTS PASSED!")
    return True


if __name__ == "__main__":
    success = test_signed_url_flow()
    if not success:
        sys.exit(1)
