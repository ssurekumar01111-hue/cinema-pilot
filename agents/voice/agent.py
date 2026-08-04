"""
agents/voice/agent.py

Voice Agent for CinemaPilot.
Generates multi-speaker dialogue audio previews for production scenes.
Uses Gemini (gemini-2.5-flash) to write grounded preview dialogue lines,
synthesizes audio per character voice via Cloud Text-to-Speech,
stores generated audio assets in GCS, upserts metadata into BigQuery Production Graph,
and logs state change audit events.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from google import genai
from google.cloud import texttospeech
from google.genai import types

# Ensure shared package is importable when running directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from shared.asset_storage import AssetStorageClient
from shared.graph_client import ProductionGraphClient

# Prebuilt voice map for characters to ensure distinct voices
PREBUILT_VOICES = [
    {"language_code": "en-US", "name": "en-US-Journey-F"},
    {"language_code": "en-US", "name": "en-US-Journey-O"},
    {"language_code": "en-US", "name": "en-US-Neural2-D"},
    {"language_code": "en-US", "name": "en-US-Neural2-F"},
]


def generate_dialogue_preview(scene_id: str) -> dict[str, Any]:
    """
    Generate a multi-speaker audio dialogue preview for a given scene ID.

    Steps:
      a. Fetches scene and its characters (via character_ids).
      b. Uses Gemini to write 3-5 lines of plausible short dialogue grounded in character names/descriptions & tone.
      c. Synthesizes multi-speaker audio preview using Text-to-Speech API with distinct prebuilt voices.
      d. Uploads audio bytes to GCS via AssetStorageClient.
      e. Upserts voice_previews record into BigQuery voice_previews table.
      f. Logs state change event to Production Graph audit log.

    Returns:
      Dict with voice_preview_id, scene_id, dialogue_lines, gs_uri, and signed_url.
    """
    graph_client = ProductionGraphClient()
    storage_client = AssetStorageClient()

    # a. Fetch scene and characters
    scene = graph_client.get_scene(scene_id)
    if not scene:
        raise ValueError(f"Scene with ID '{scene_id}' not found in Production Graph.")

    character_ids = scene.get("character_ids") or []
    characters = [graph_client.get_character(cid) for cid in character_ids if graph_client.get_character(cid)]

    if not characters:
        raise ValueError(f"No characters assigned to scene '{scene_id}'.")

    char_descriptions = []
    for c in characters:
        name = c.get("name", "Unknown Character")
        desc = c.get("description", "")
        if desc:
            char_descriptions.append(f"{name}: {desc}")
        else:
            char_descriptions.append(name)
    char_info_str = "\n".join(char_descriptions)

    emotional_tone = scene.get("emotional_tone", "neutral")

    # b. Generate dialogue lines via Gemini
    prompt = f"""
You are a script assistant generating a SHORT 3 to 5 line placeholder dialogue preview for a scene.

SCENE DATA:
- Scene ID: {scene_id}
- Emotional Tone: {emotional_tone}
- Characters Present:
{char_info_str}

INSTRUCTIONS:
1. Write 3 to 5 short lines of dialogue exchanged between the characters present.
2. Ground the dialogue strictly in the real character names and their described personalities/professions.
3. Match the '{emotional_tone}' emotional tone.
4. Return ONLY a valid JSON object matching this schema:
{{
  "dialogue_lines": [
    {{"character_name": "EXACT_CHARACTER_NAME", "line": "spoken line of dialogue"}}
  ]
}}
"""

    genai_client = genai.Client(vertexai=True, project=ProductionGraphClient.PROJECT, location="us-central1")
    gen_response = genai_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.3,
        ),
    )

    try:
        dialogue_data = json.loads(gen_response.text)
        dialogue_lines = dialogue_data.get("dialogue_lines", [])
    except Exception as exc:
        raise RuntimeError(f"Failed to parse Gemini dialogue response as JSON: {gen_response.text}") from exc

    if not dialogue_lines:
        raise RuntimeError("Gemini generated empty dialogue lines list.")

    # Assign a distinct prebuilt voice per character
    char_names = list(dict.fromkeys([line.get("character_name", "") for line in dialogue_lines if line.get("character_name")]))
    voice_assignment = {}
    for idx, cname in enumerate(char_names):
        voice_assignment[cname] = PREBUILT_VOICES[idx % len(PREBUILT_VOICES)]

    # c. Synthesize multi-speaker audio using Cloud Text-to-Speech API
    tts_client = texttospeech.TextToSpeechClient()
    combined_audio_bytes = bytearray()

    for item in dialogue_lines:
        cname = item.get("character_name", "")
        line_text = item.get("line", "")
        if not line_text:
            continue

        voice_params = voice_assignment.get(cname, PREBUILT_VOICES[0])
        synthesis_input = texttospeech.SynthesisInput(text=line_text)
        voice_config = texttospeech.VoiceSelectionParams(
            language_code=voice_params["language_code"],
            name=voice_params["name"],
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3
        )

        try:
            tts_response = tts_client.synthesize_speech(
                request=texttospeech.SynthesizeSpeechRequest(
                    input=synthesis_input,
                    voice=voice_config,
                    audio_config=audio_config,
                )
            )
            combined_audio_bytes.extend(tts_response.audio_content)
        except Exception as tts_err:
            raise RuntimeError(f"Text-to-Speech synthesis failed for line '{line_text}': {tts_err}") from tts_err

    if not combined_audio_bytes:
        raise RuntimeError("Synthesized audio result is empty.")

    # d. Upload audio bytes via AssetStorageClient
    gs_uri = storage_client.upload_asset(
        entity_type="voice_preview",
        entity_id=scene_id,
        asset_bytes=bytes(combined_audio_bytes),
        content_type="audio/mpeg",
        extension="mp3",
    )

    # e. Upsert record in BigQuery voice_previews table
    voice_preview_id = f"vp_{scene_id}"
    before_state = graph_client.get_voice_preview(voice_preview_id) or {}

    voice_record = {
        "voice_preview_id": voice_preview_id,
        "scene_id": scene_id,
        "dialogue_lines": dialogue_lines,
        "gs_uri": gs_uri,
    }

    graph_client.upsert_voice_preview(voice_record)
    after_state = graph_client.get_voice_preview(voice_preview_id) or voice_record

    # Helper to serialize state for audit logging
    def sanitize_state(state: dict | None) -> dict:
        if not state:
            return {}
        cleaned = {}
        for k, v in state.items():
            if hasattr(v, "isoformat"):
                cleaned[k] = v.isoformat()
            else:
                cleaned[k] = v
        return cleaned

    cleaned_before = sanitize_state(before_state)
    cleaned_after = sanitize_state(after_state)

    # f. Log audit event
    graph_client.log_event(
        actor_agent="voice_agent",
        entity_type="voice_preview",
        entity_id=scene_id,
        before_state=cleaned_before,
        after_state=cleaned_after,
        triggered_agents=[],
    )

    signed_url = storage_client.get_signed_url(gs_uri)

    return {
        "voice_preview_id": voice_preview_id,
        "scene_id": scene_id,
        "dialogue_lines": dialogue_lines,
        "gs_uri": gs_uri,
        "signed_url": signed_url,
    }


if __name__ == "__main__":
    result = generate_dialogue_preview("scene_005")
    print("=" * 80)
    print("VOICE DIALOGUE PREVIEW GENERATED SUCCESSFULLY")
    print("=" * 80)
    print(f"Scene ID:          {result['scene_id']}")
    print(f"Voice Preview ID:  {result['voice_preview_id']}")
    print(f"GS URI:            {result['gs_uri']}")
    print(f"Signed URL:        {result['signed_url']}")
    print("\nGenerated Dialogue Lines:")
    for line in result["dialogue_lines"]:
        print(f"  {line.get('character_name')}: \"{line.get('line')}\"")
    print("=" * 80)
