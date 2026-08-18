"""
agents/script_intelligence/agent.py

Script Intelligence Agent — Pass 1: Plain-text extraction via Gemini.

Reads a raw screenplay text file, sends it to Gemini for structured entity
extraction, validates the JSON response, writes all entities to the
Production Graph via ProductionGraphClient, and logs a single audit event.

Pass 2 (planned): Replace plain-text read with Document AI for native PDF
parsing, once this plain-text baseline is validated end-to-end.

Usage (standalone):
    python agents/script_intelligence/agent.py

Usage (imported):
    from agents.script_intelligence.agent import ingest_screenplay
    result = ingest_screenplay("infra/demo_screenplay.txt")
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo-root path resolution (works when run as __main__ or imported)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from google import genai
from google.genai import types

from shared.graph_client import ProductionGraphClient, GraphClientError
from shared.telemetry import instrument_agent

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GEMINI_MODEL  = "gemini-2.5-flash"
GCP_PROJECT   = "cinemapilot-2026"
GCP_LOCATION  = "us-central1"

# Words that should always be uppercased (not title-cased) in prop names.
# Extend this set if the screenplay domain introduces new acronyms.
KNOWN_ACRONYMS: frozenset[str] = frozenset({
    "suv", "usb", "tv", "gps", "uav", "atm", "id", "dna",
    "vcr", "dvd", "cd", "pc", "fm", "am", "rf", "ai", "vr", "ar",
    "led", "lcd", "ac", "dc", "uk", "us", "eu", "un",
})

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """\
You are a screenplay analysis assistant for a film production tool.

Read the screenplay text below and extract all entities into a single JSON
object that matches this exact schema. Return ONLY valid JSON — no markdown
fences, no explanation, no extra keys.

Schema:
{{
  "scenes": [
    {{
      "scene_number":      <integer, 1-indexed narrative order>,
      "location_name":     <string, location name as it appears in the screenplay heading>,
      "location_type":     <"interior" or "exterior">,
      "character_names":   [<string>, ...],
      "prop_names":        [<string>, ...],
      "emotional_tone":    <string, one or two words, e.g. "tense", "comedic", "hopeful">,
      "camera_cues":       [<string>, ...],
      "timeline_position": <integer, same value as scene_number>
    }}
  ],
  "characters": [
    {{
      "name":        <string, full character name as written in the screenplay>,
      "description": <string, one concise sentence describing the character>
    }}
  ],
  "locations": [
    {{
      "name":                <string, exact location name from scene headings>,
      "location_type":       <"interior" or "exterior">,
      "weather_sensitivity": <boolean, true if outdoors or weather-exposed>
    }}
  ],
  "props": [
    {{
      "name": <string, prop name exactly as it appears in the screenplay>
    }}
  ]
}}

Rules:
- scenes: include every distinct INT./EXT. scene heading in narrative order.
- characters: include every named character who speaks or is described.
  Do NOT include unnamed background figures (e.g. "TWO FIGURES").
- locations: deduplicate — each unique location name appears exactly once.
- props: DEDUPLICATED MASTER LIST ONLY. The props[] array must be a single
  deduplicated master list of all props in the entire screenplay. Do NOT
  include casing variants ("THERMOS" and "thermos" are the same prop),
  plural forms ("printouts" and "printout" are the same), or descriptive
  variants ("crumpled frequency printout" and "frequency printout" are the
  same prop). Pick ONE canonical Title Case name per distinct physical object.
- character_names per scene: EXACT MATCH REQUIRED. Every string in
  character_names must be the exact same string as an entry in the top-level
  characters[].name array. Do NOT use shortened forms ("Nadia" instead of
  "DR. NADIA VOSS"), abbreviations, or parentheticals like "(V.O.)".
- location_name per scene: EXACT MATCH REQUIRED. The location_name string
  must be the exact same string as an entry in the top-level locations[].name
  array. Do not paraphrase, abbreviate, or add punctuation.
- prop_names per scene: only props that appear or are used in that specific
  scene, using the exact canonical name chosen for the master props[] list.
- camera_cues: extract only if explicitly written (e.g. "CLOSE ON", "PAN TO").
  If none are written for a scene, return an empty list [].
- emotional_tone: one or two words capturing the dominant mood of the scene.
- weather_sensitivity: true for any exterior location; false for all interiors.
- Return ONLY the JSON object. No ```json fences. No prose. No explanation.

SCREENPLAY:
-----------
{screenplay_text}
-----------
"""


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, prefix: str) -> str:
    """
    Generate a stable, lowercase, underscore-delimited ID from a name.

    Args:
        text:   Human-readable name (e.g. "Felix Crane").
        prefix: Short entity-type prefix (e.g. "char", "loc", "prop").

    Returns:
        A stable ID string (e.g. "char_felix_crane").

    Examples::

        _slugify("Felix Crane", "char")           -> "char_felix_crane"
        _slugify("Millbrook Storage Unit", "loc") -> "loc_millbrook_storage_unit"
        _slugify("Red USB Drive", "prop")         -> "prop_red_usb_drive"
    """
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return f"{prefix}_{slug}"


def _strip_code_fences(text: str) -> str:
    """
    Remove Markdown code fences if the model wrapped its output in them.

    Handles both `` ```json ... ``` `` and `` ``` ... ``` `` variants.

    Args:
        text: Raw string returned by the model.

    Returns:
        The inner content with fences and surrounding whitespace removed.
    """
    text = text.strip()
    fence = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)
    match = fence.match(text)
    return match.group(1).strip() if match else text


def _parse_gemini_json(raw: str) -> dict:
    """
    Strip code fences and parse the model's response as strict JSON.

    Args:
        raw: Raw text returned by the Gemini model.

    Returns:
        Parsed dict matching the extraction schema.

    Raises:
        ValueError: If the response cannot be decoded as valid JSON.
    """
    cleaned = _strip_code_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Gemini returned invalid JSON at offset {exc.pos}: {exc.msg}\n"
            f"--- First 600 chars of raw response ---\n{raw[:600]}"
        ) from exc


def _validate_schema(extracted: dict) -> None:
    """
    Check that the parsed dict contains the four required top-level keys.

    Args:
        extracted: Parsed dict from Gemini.

    Raises:
        ValueError: If any required key is missing.
    """
    required = ("scenes", "characters", "locations", "props")
    missing = [k for k in required if k not in extracted]
    if missing:
        raise ValueError(
            f"Gemini JSON is missing required key(s): {missing}. "
            f"Keys present: {list(extracted.keys())}"
        )


def _build_gemini_client() -> genai.Client:
    """
    Construct a Gemini client using Vertex AI + Application Default Credentials.

    No API keys are used. Authentication flows via ADC, consistent with
    the rest of the project (``gcloud auth application-default login``
    locally; service account in Cloud Run / Agent Engine).

    Returns:
        A configured ``genai.Client`` instance.

    Raises:
        RuntimeError: If the client cannot be initialised.
    """
    try:
        return genai.Client(
            vertexai=True,
            project=GCP_PROJECT,
            location=GCP_LOCATION,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Failed to initialise Gemini client (Vertex AI / ADC): {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Normalization helpers (code-side safety net — prompt rules are primary)
# ---------------------------------------------------------------------------

def _norm_key(text: str) -> str:
    """
    Produce a normalized comparison key from a name string.

    Strips parentheticals (e.g. "(V.O.)"), lowercases, removes all
    non-alphanumeric characters, collapses whitespace, and strips a
    trailing 's' for simple plural forms.

    Used internally by character/location fuzzy-matching and prop dedup.
    """
    s = re.sub(r"\(.*?\)", "", text)   # remove (V.O.), (CONT'D) etc.
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s) # punctuation -> space
    s = re.sub(r"\s+", " ", s).strip()
    # strip trailing 's' for basic plural normalization
    words = s.split()
    if words and words[-1].endswith("s") and len(words[-1]) > 2:
        words[-1] = words[-1][:-1]
    return " ".join(words)


def _smart_title(text: str) -> str:
    """
    Title-case a string while preserving known acronyms in ALL CAPS.

    Each word is capitalised normally unless it appears (case-insensitively)
    in ``KNOWN_ACRONYMS``, in which case it is fully uppercased.

    Args:
        text: The raw prop name string (any casing).

    Returns:
        A display-ready string with correct casing.

    Examples::

        _smart_title("red usb drive")      -> "Red USB Drive"
        _smart_title("black suv")          -> "Black SUV"
        _smart_title("transistor radio")   -> "Transistor Radio"
        _smart_title("RED USB DRIVE")      -> "Red USB Drive"
    """
    return " ".join(
        word.upper() if word.lower() in KNOWN_ACRONYMS else word.capitalize()
        for word in text.split()
    )


def _normalize_char_and_loc_names(extracted: dict) -> None:
    """
    Fuzzy-match character and location names used inside scenes against
    the canonical lists in ``characters[]`` and ``locations[]``, and
    replace any mismatched strings in-place.

    Strategy:
    - For characters: a scene name matches a canonical name when every word
      in the scene name's norm key is present in the canonical norm key
      (i.e. "felix" matches "felix crane", "nadia" matches "nadia voss").
    - For locations: use word-overlap ratio; accept match if ≥ 0.5.

    All replacements are logged to stdout.

    Args:
        extracted: The parsed Gemini dict, modified in-place.
    """
    # Build canonical lookup tables
    canon_chars = {c["name"]: _norm_key(c["name"]) for c in extracted["characters"]}
    canon_locs  = {l["name"]: _norm_key(l["name"]) for l in extracted["locations"]}

    char_fixes = loc_fixes = 0

    for scene in extracted["scenes"]:
        # --- Character names ---
        resolved_chars = []
        for raw in scene.get("character_names", []):
            norm_raw  = _norm_key(raw)
            raw_words = set(norm_raw.split())
            match = None
            # Exact norm match first
            for canon, norm_canon in canon_chars.items():
                if norm_raw == norm_canon:
                    match = canon
                    break
            # Subset match: all words in raw appear in canonical
            if not match:
                for canon, norm_canon in canon_chars.items():
                    canon_words = set(norm_canon.split())
                    if raw_words and raw_words.issubset(canon_words):
                        match = canon
                        break
            if match:
                if raw != match:
                    print(f"  [norm:char] scene {scene['scene_number']}: "
                          f"{raw!r} -> {match!r}")
                    char_fixes += 1
                resolved_chars.append(match)
            else:
                # No canonical match — keep original (will miss ID lookup,
                # which is intentional: better to surface than silently wrong)
                print(f"  [norm:char] scene {scene['scene_number']}: "
                      f"no canonical match for {raw!r} — kept as-is")
                resolved_chars.append(raw)
        scene["character_names"] = resolved_chars

        # --- Location name ---
        raw_loc   = scene.get("location_name", "")
        norm_raw  = _norm_key(raw_loc)
        raw_words = set(norm_raw.split())
        best_match, best_score = None, 0.0
        for canon, norm_canon in canon_locs.items():
            canon_words = set(norm_canon.split())
            if not raw_words or not canon_words:
                continue
            overlap = len(raw_words & canon_words)
            score   = overlap / max(len(raw_words), len(canon_words))
            if score > best_score:
                best_score, best_match = score, canon
        if best_match and best_score >= 0.5:
            if raw_loc != best_match:
                print(f"  [norm:loc]  scene {scene['scene_number']}: "
                      f"{raw_loc!r} -> {best_match!r} (overlap={best_score:.0%})")
                loc_fixes += 1
            scene["location_name"] = best_match
        else:
            print(f"  [norm:loc]  scene {scene['scene_number']}: "
                  f"no canonical match for {raw_loc!r} — kept as-is")

    print(f"  [norm] character fixes: {char_fixes}, location fixes: {loc_fixes}")


def _deduplicate_props(extracted: dict) -> None:
    """
    Deduplicate the master props list by normalized key, collapsing variants
    into a single canonical Title Case entry and updating all scene prop_names
    references.

    Deduplication key: lowercase + strip parentheticals + strip punctuation +
    strip trailing 's'.

    When multiple names share a key, the canonical name is chosen as:
    - The shortest name (fewest words) among the group, in Title Case.
    This prefers "Thermos" over "Crumpled Frequency Printout" variants.

    All merges are logged to stdout.

    Args:
        extracted: The parsed Gemini dict, modified in-place.
    """
    # Group all prop names by their normalized key
    groups: dict[str, list[str]] = {}
    for prop in extracted["props"]:
        key = _norm_key(prop["name"])
        groups.setdefault(key, []).append(prop["name"])

    # Build canonical name map: original_name -> canonical_name
    canonical_map: dict[str, str] = {}
    deduped_props: list[dict] = []

    print(f"\n  [norm:props] Deduplicating {len(extracted['props'])} raw prop entries...")
    for key, variants in groups.items():
        # Canonical = shortest variant, smart-title-cased (acronyms uppercased)
        canonical_raw = min(variants, key=lambda v: len(v))
        canonical     = _smart_title(canonical_raw)
        for v in variants:
            canonical_map[v] = canonical
            if v != canonical:
                print(f"  [norm:props] MERGE: {v!r} -> {canonical!r}")
        deduped_props.append({"name": canonical})

    n_before = len(extracted["props"])
    n_after  = len(deduped_props)
    print(f"  [norm:props] {n_before} raw -> {n_after} deduplicated prop(s).")

    # Update master list in-place
    extracted["props"] = deduped_props

    # Update all scene prop_names to use canonical names
    for scene in extracted["scenes"]:
        scene["prop_names"] = [
            canonical_map.get(p, _smart_title(p)) for p in scene.get("prop_names", [])
        ]


# ---------------------------------------------------------------------------
# Main ingestion function
# ---------------------------------------------------------------------------

@instrument_agent("script_intelligence_agent")
def ingest_screenplay(filepath: str, cascade_id: str | None = None) -> dict:
    """
    Read a screenplay file, extract entities via Gemini, and write to the
    Production Graph.

    This is Pass 1 — plain text file reading. Document AI (PDF-native
    parsing) is the planned Pass 2 once this baseline is validated.

    Steps
    -----
    1. Read raw UTF-8 text from ``filepath``.
    2. Send to Gemini with a structured extraction prompt.
    3. Parse and validate the JSON response (raises on malformed output).
    3b. Normalization pass (code-side safety net):
        - Fuzzy-match scene character_names to canonical characters[].name.
        - Fuzzy-match scene location_name to canonical locations[].name.
        - Deduplicate props[] by normalized key; log all merges.
    4. Generate stable entity IDs via ``_slugify``.
    5. Write characters → locations → props → scenes to the Production Graph
       in dependency order. Fail loudly on any write error; do not silently
       swallow partial writes.
    6. Log a single audit event summarising the full ingestion.
    7. Return the extracted JSON dict.

    Args:
        filepath: Path to the screenplay text file (relative or absolute).
                  Relative paths are resolved from the current working directory.

    Returns:
        Dict with keys ``scenes``, ``characters``, ``locations``, ``props``
        matching the Gemini extraction schema.

    Raises:
        FileNotFoundError: If ``filepath`` does not exist.
        ValueError:        If Gemini's response is not valid JSON or the schema
                           is missing required keys.
        RuntimeError:      If the Gemini client cannot be initialised.
        GraphClientError:  If any Production Graph write fails.
    """
    filepath = str(filepath)
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Screenplay file not found: {filepath!r}")

    # ------------------------------------------------------------------
    # 1. Read raw screenplay text
    # ------------------------------------------------------------------
    print(f"\n[script_intelligence] Reading screenplay: {filepath}")
    screenplay_text = path.read_text(encoding="utf-8")
    print(f"[script_intelligence] Read {len(screenplay_text):,} characters.")

    # ------------------------------------------------------------------
    # 2. Call Gemini for extraction
    # ------------------------------------------------------------------
    print(f"[script_intelligence] Sending to Gemini ({GEMINI_MODEL}) for extraction...")
    gemini = _build_gemini_client()
    prompt = EXTRACTION_PROMPT.format(screenplay_text=screenplay_text)

    try:
        response = gemini.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,   # fully deterministic — extraction, not creativity
                thinking_config=types.ThinkingConfig(
                    thinking_budget=0,   # disable thinking tokens; we want raw JSON
                ),
                response_mime_type="application/json",
            ),
        )
    except Exception as exc:
        raise RuntimeError(
            f"Gemini generate_content failed: {exc}"
        ) from exc

    raw_response = response.text
    print(f"[script_intelligence] Received {len(raw_response):,} chars from Gemini.")

    # ------------------------------------------------------------------
    # 3. Parse and validate JSON
    # ------------------------------------------------------------------
    print("[script_intelligence] Parsing and validating JSON response...")
    extracted = _parse_gemini_json(raw_response)
    _validate_schema(extracted)

    n_raw_props = len(extracted["props"])
    print(
        f"[script_intelligence] Extracted (raw): "
        f"{len(extracted['scenes'])} scene(s), {len(extracted['characters'])} character(s), "
        f"{len(extracted['locations'])} location(s), {n_raw_props} prop(s) (pre-dedup)."
    )

    # ------------------------------------------------------------------
    # 3b. Normalization pass (code-side safety net)
    # ------------------------------------------------------------------
    print("[script_intelligence] Running normalization pass...")
    _normalize_char_and_loc_names(extracted)
    _deduplicate_props(extracted)
    print()

    n_scenes = len(extracted["scenes"])
    n_chars  = len(extracted["characters"])
    n_locs   = len(extracted["locations"])
    n_props  = len(extracted["props"])
    print(
        f"[script_intelligence] After normalization: "
        f"{n_scenes} scene(s), {n_chars} character(s), "
        f"{n_locs} location(s), {n_props} prop(s)."
    )

    # ------------------------------------------------------------------
    # 4. Generate stable IDs
    # ------------------------------------------------------------------
    char_id_map  = {c["name"]: _slugify(c["name"], "char") for c in extracted["characters"]}
    loc_id_map   = {l["name"]: _slugify(l["name"], "loc")  for l in extracted["locations"]}
    prop_id_map  = {p["name"]: _slugify(p["name"], "prop") for p in extracted["props"]}

    print(f"[script_intelligence] Generated IDs:")
    for name, id_ in {**char_id_map, **loc_id_map, **prop_id_map}.items():
        print(f"  {name!r:40s} -> {id_!r}")

    # ------------------------------------------------------------------
    # 5. Write to Production Graph (dependency order: chars → locs → props → scenes)
    # ------------------------------------------------------------------
    print("\n[script_intelligence] Connecting to Production Graph...")
    graph = ProductionGraphClient()
    write_errors: list[str] = []

    # --- Characters -------------------------------------------------
    print(f"[script_intelligence] Writing {n_chars} character(s)...")
    for char in extracted["characters"]:
        cid = char_id_map[char["name"]]
        try:
            graph.upsert_character({
                "character_id":  cid,
                "name":          char["name"],
                "description":   char.get("description", ""),
                "costume_notes": [],
                "scene_ids":     [],   # back-filled in future passes
            })
            print(f"  + [char]  {cid!r}  —  {char['name']}")
        except GraphClientError as exc:
            err = f"character '{char['name']}': {exc}"
            write_errors.append(err)
            print(f"  ! [char]  ERROR: {err}")

    # --- Locations --------------------------------------------------
    print(f"[script_intelligence] Writing {n_locs} location(s)...")
    for loc in extracted["locations"]:
        lid = loc_id_map[loc["name"]]
        try:
            graph.upsert_location({
                "location_id":          lid,
                "name":                 loc["name"],
                "location_type":        loc.get("location_type", "interior"),
                "cost_profile":         0.0,
                "logistics_notes":      "",
                "weather_sensitivity":  loc.get("weather_sensitivity", False),
            })
            print(f"  + [loc]   {lid!r}  —  {loc['name']}  "
                  f"(weather={loc.get('weather_sensitivity', False)})")
        except GraphClientError as exc:
            err = f"location '{loc['name']}': {exc}"
            write_errors.append(err)
            print(f"  ! [loc]   ERROR: {err}")

    # --- Props ------------------------------------------------------
    print(f"[script_intelligence] Writing {n_props} prop(s)...")
    for prop in extracted["props"]:
        pid = prop_id_map[prop["name"]]
        try:
            graph.upsert_prop({
                "prop_id":        pid,
                "name":           prop["name"],
                "scene_ids":      [],
                "sourcing_notes": "",
            })
            print(f"  + [prop]  {pid!r}  —  {prop['name']}")
        except GraphClientError as exc:
            err = f"prop '{prop['name']}': {exc}"
            write_errors.append(err)
            print(f"  ! [prop]  ERROR: {err}")

    # --- Scenes -----------------------------------------------------
    print(f"[script_intelligence] Writing {n_scenes} scene(s)...")
    for scene in extracted["scenes"]:
        scene_num = scene["scene_number"]
        scene_id  = f"scene_{scene_num:03d}"

        # Resolve character/prop names to IDs — best-effort (skip unknowns)
        resolved_char_ids = [
            char_id_map[n]
            for n in scene.get("character_names", [])
            if n in char_id_map
        ]
        resolved_prop_ids = [
            prop_id_map[n]
            for n in scene.get("prop_names", [])
            if n in prop_id_map
        ]
        loc_name = scene.get("location_name", "")
        resolved_loc_id = loc_id_map.get(loc_name) or (
            _slugify(loc_name, "loc") if loc_name else None
        )

        try:
            graph.upsert_scene({
                "scene_id":          scene_id,
                "scene_number":      scene_num,
                "location_id":       resolved_loc_id,
                "character_ids":     resolved_char_ids,
                "prop_ids":          resolved_prop_ids,
                "emotional_tone":    scene.get("emotional_tone", ""),
                "camera_cues":       scene.get("camera_cues", []),
                "timeline_position": scene.get("timeline_position", scene_num),
                "status":            "draft",
            })
            print(
                f"  + [scene] {scene_id!r}  "
                f"loc={resolved_loc_id!r}  "
                f"tone={scene.get('emotional_tone', '?')!r}  "
                f"chars={len(resolved_char_ids)}  props={len(resolved_prop_ids)}"
            )
        except GraphClientError as exc:
            err = f"scene {scene_num} ('{loc_name}'): {exc}"
            write_errors.append(err)
            print(f"  ! [scene] ERROR: {err}")

    # Surface any write failures loudly — never swallow partial writes
    if write_errors:
        error_block = "\n  - ".join(write_errors)
        raise GraphClientError(
            f"Ingestion completed with {len(write_errors)} write error(s). "
            f"The following entities may be missing from the Production Graph:\n"
            f"  - {error_block}"
        )

    # ------------------------------------------------------------------
    # 6. Log single audit event for the full ingestion
    # ------------------------------------------------------------------
    print("\n[script_intelligence] Logging ingestion audit event...")
    event_id = graph.log_event(
        actor_agent="script_intelligence_agent",
        entity_type="screenplay",
        entity_id=filepath,
        before_state={},
        after_state={
            "filepath":   filepath,
            "scenes":     n_scenes,
            "characters": n_chars,
            "locations":  n_locs,
            "props":      n_props,
            "entity_ids": {
                "characters": list(char_id_map.values()),
                "locations":  list(loc_id_map.values()),
                "props":      list(prop_id_map.values()),
                "scenes":     [f"scene_{s['scene_number']:03d}" for s in extracted["scenes"]],
            },
        },
        triggered_agents=[],   # first ingestion — no downstream triggers yet
    )
    print(f"[script_intelligence] Audit event logged: {event_id}")

    # ------------------------------------------------------------------
    # 7. Return
    # ------------------------------------------------------------------
    print(f"[script_intelligence] Ingestion complete. "
          f"({n_scenes} scenes, {n_chars} chars, {n_locs} locs, {n_props} props)\n")
    return extracted


# ---------------------------------------------------------------------------
# Standalone entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _screenplay = _REPO_ROOT / "infra" / "demo_screenplay.txt"

    print("=" * 70)
    print("  Script Intelligence Agent  —  Demo Run")
    print(f"  Model  : {GEMINI_MODEL}")
    print(f"  Project: {GCP_PROJECT} / {GCP_LOCATION}")
    print(f"  File   : {_screenplay}")
    print("=" * 70)

    _result = ingest_screenplay(str(_screenplay))

    print("\n" + "=" * 70)
    print("  Extracted JSON")
    print("=" * 70)
    print(json.dumps(_result, indent=2))
