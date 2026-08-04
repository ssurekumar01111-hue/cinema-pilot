"""
agents/music/agent.py

Music Agent for CinemaPilot.
Generates musical score cues for production scenes using Lyria 3 (lyria-3-clip-preview),
stores generated audio assets in GCS, upserts metadata into BigQuery Production Graph,
and logs state change events.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

import google.auth
import google.auth.transport.requests

# Ensure shared package is importable when running directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from shared.asset_storage import AssetStorageClient
from shared.graph_client import ProductionGraphClient


def construct_lyria_prompt(scene: dict, location: dict | None) -> str:
    """
    Construct a Lyria 3 prompt grounded strictly in scene emotional_tone and location context.

    Does not invent genre/instrumentation specifics not implied by the actual tone/setting data.
    """
    prompt_parts = ["Film music score cue."]

    emotional_tone = scene.get("emotional_tone")
    if emotional_tone:
        prompt_parts.append(f"Emotional tone and mood: {emotional_tone}.")

    if location:
        loc_name = location.get("name", "")
        loc_type = location.get("location_type", "")
        if loc_name or loc_type:
            prompt_parts.append(f"Setting: {loc_type} setting at {loc_name}.")

    prompt_parts.append("Cinematic atmosphere suitable for film soundtrack background cue.")
    return " ".join(prompt_parts)


def generate_music_cue(scene_id: str) -> dict[str, Any]:
    """
    Generate a music cue audio clip for a given scene ID using Lyria 3.

    Steps:
      a. Fetches scene and location.
      b. Immediately writes a music_cues row with status="pending" (async pattern).
      c. Constructs grounded Lyria 3 prompt.
      d. Calls Lyria 3 REST endpoint using google.auth token.
      e. On success: decodes base64 audio bytes, uploads to GCS, updates music_cues to status="completed".
      f. On failure: updates music_cues to status="failed" with error reason, and re-raises exception.
      g. Logs state change event to Production Graph audit log.

    Returns:
      Dict with music cue metadata including music_cue_id, scene_id, status, gs_uri, signed_url,
      lyrics, description, prompt_used, and elapsed_seconds.
    """
    graph_client = ProductionGraphClient()
    storage_client = AssetStorageClient()
    music_cue_id = f"mc_{scene_id}"

    # a. Fetch scene and location
    scene = graph_client.get_scene(scene_id)
    if not scene:
        raise ValueError(f"Scene with ID '{scene_id}' not found in Production Graph.")

    location_id = scene.get("location_id")
    location = graph_client.get_location(location_id) if location_id else None

    # c. Construct grounded prompt
    prompt = construct_lyria_prompt(scene, location)

    # b. Write pending row before calling Lyria
    pending_record = {
        "music_cue_id": music_cue_id,
        "scene_id": scene_id,
        "gs_uri": None,
        "lyrics": None,
        "description": None,
        "prompt_used": prompt,
        "status": "pending",
    }
    graph_client.upsert_music_cue(pending_record)

    start_time = time.time()

    try:
        # d. Fetch access token programmatically via google.auth
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(google.auth.transport.requests.Request())
        access_token = credentials.token

        # Call Lyria 3 REST interactions endpoint
        endpoint_url = (
            f"https://aiplatform.googleapis.com/v1beta1/"
            f"projects/{ProductionGraphClient.PROJECT}/locations/global/interactions"
        )
        payload = {
            "model": "lyria-3-clip-preview",
            "input": prompt,
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        req = urllib.request.Request(
            endpoint_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                resp_bytes = response.read()
                resp_json = json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as http_err:
            error_body = http_err.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Lyria 3 API HTTP Error {http_err.code}: {http_err.reason}. Details: {error_body}"
            ) from http_err
        except Exception as api_err:
            raise RuntimeError(f"Lyria 3 API call failed: {api_err}") from api_err

        # Check interaction status / outputs
        outputs = resp_json.get("outputs", [])
        if not outputs:
            raise RuntimeError(f"Lyria 3 returned no outputs in response: {resp_json}")

        lyrics = ""
        description = ""
        audio_bytes = None

        for output in outputs:
            data_val = output.get("data")
            mime_type = output.get("mime_type", "")
            text_val = output.get("text", "")

            if data_val and ("audio" in mime_type or output.get("type") == "audio"):
                audio_bytes = base64.b64decode(data_val)
            elif text_val:
                if text_val.startswith("Caption:"):
                    description = text_val
                elif not lyrics:
                    lyrics = text_val
                else:
                    description = f"{description}\n{text_val}".strip()

        if not audio_bytes:
            raise RuntimeError(
                f"Lyria 3 response did not contain valid audio data. Response outputs: {outputs}"
            )

        # e. Upload audio asset bytes to GCS
        gs_uri = storage_client.upload_asset(
            entity_type="music",
            entity_id=scene_id,
            asset_bytes=audio_bytes,
            content_type="audio/mpeg",
            extension="mp3",
        )

        # Update music_cues row to completed
        completed_record = {
            "music_cue_id": music_cue_id,
            "scene_id": scene_id,
            "gs_uri": gs_uri,
            "lyrics": lyrics,
            "description": description,
            "prompt_used": prompt,
            "status": "completed",
        }
        graph_client.upsert_music_cue(completed_record)

        elapsed = time.time() - start_time

        # g. Log audit event
        graph_client.log_event(
            actor_agent="music_agent",
            entity_type="music_cue",
            entity_id=scene_id,
            before_state={"status": "pending"},
            after_state={
                "status": "completed",
                "gs_uri": gs_uri,
                "lyrics": lyrics,
                "description": description,
                "prompt_used": prompt,
            },
            triggered_agents=[],
        )

        signed_url = storage_client.get_signed_url(gs_uri)

        return {
            "music_cue_id": music_cue_id,
            "scene_id": scene_id,
            "status": "completed",
            "gs_uri": gs_uri,
            "signed_url": signed_url,
            "lyrics": lyrics,
            "description": description,
            "prompt_used": prompt,
            "elapsed_seconds": elapsed,
        }

    except Exception as exc:
        elapsed = time.time() - start_time
        # f. On failure: update music_cues row to failed and re-raise
        failed_record = {
            "music_cue_id": music_cue_id,
            "scene_id": scene_id,
            "gs_uri": None,
            "lyrics": None,
            "description": f"Failed: {exc}",
            "prompt_used": prompt,
            "status": "failed",
        }
        try:
            graph_client.upsert_music_cue(failed_record)
            graph_client.log_event(
                actor_agent="music_agent",
                entity_type="music_cue",
                entity_id=scene_id,
                before_state={"status": "pending"},
                after_state={"status": "failed", "error": str(exc)},
                triggered_agents=[],
            )
        except Exception:
            pass  # Ensure original exception is re-raised even if DB update fails

        raise exc


if __name__ == "__main__":
    print("Starting music cue generation for scene_005...")
    result = generate_music_cue("scene_005")
    print("=" * 80)
    print("MUSIC CUE GENERATED SUCCESSFULLY")
    print("=" * 80)
    print(f"Elapsed Time: {result['elapsed_seconds']:.2f}s")
    print(f"Status:       {result['status']}")
    print(f"GS URI:       {result['gs_uri']}")
    print(f"Signed URL:   {result['signed_url']}")
    print(f"Prompt:       {result['prompt_used']}")
    print(f"Lyrics:       {result['lyrics']}")
    print(f"Description:  {result['description']}")
    print("=" * 80)
