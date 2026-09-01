# CinemaPilot

**An autonomous AI production office for film — every department represented by a collaborating agent.**

Built for the [Agentic Cinema: The Blockbuster Hackathon](https://agentic-cinema.devpost.com/) — **Grafana partner track**.

## The 2 AM phone call this is meant to prevent

A location scout finds out the beach they booked for Scene 5 is closed for the week — maybe a permit fell through, maybe the tide schedule changed. It's a five-minute problem to *describe*. It is not a five-minute problem to *resolve*.

By morning, the budget lead needs new cost estimates. Scheduling needs to know if the shoot day still works. The storyboard artist has drawn the wrong location. The composer scored a scene that no longer exists in that setting. Risk and safety need to re-check weather and logistics for the new site. And somewhere in a group chat, someone is trying to explain *why* all of this is happening to eight different people who each only see their own corner of it.

Right now, a single screenplay gets manually re-read by 10–20 people across a production, each department reconstructing the same information independently, and every change ripples through by hand, department by department, Slack message by Slack message.

CinemaPilot is what happens if that screenplay only has to be read once — by a system that never forgets what it extracted, and that knows exactly which departments a change actually touches, so nobody has to manually chase down who needs to know.

## The problem

A single screenplay is currently manually re-read by 10–20 people across a film production — director, producer, budget, casting, storyboard, music, scheduling, location scouting — each department recreating the same information independently. CinemaPilot extracts a screenplay's structured data once and republishes it as department-specific, continuously-updated assets, automatically propagating downstream when anything changes.

## Architecture

```
Screenplay PDF/text
      │
      ▼
Script Intelligence Agent  (Gemini extraction → structured Production Graph)
      │
      ▼
Production Graph (BigQuery, single source of truth, event-sourced)
      │
      ▼
Change Detection Agent  (diffs the graph, computes exactly which agents are affected)
      │
      ├──► Budget      ├──► Location    ├──► Risk
      ├──► Schedule     ├──► Storyboard  ├──► Music
      │
      ▼
Producer Agent  (synthesizes budget/schedule/risk into an overview)
      │
      ▼
Explanation Agent  (pure synthesis of prior agents' reasoning into a plain-language narrative)
```

**14 agents total:** Script Intelligence, Change Detection, Budget, Location, Risk, Schedule, Storyboard, Music, Director, Casting, Voice, Producer, Explanation, and Trailer (on-demand via dashboard).

**Signature demo:** moving Scene 5 from an interior warehouse to an exterior beach location routes exactly 6 affected agents (not all 14) — Budget, Location, Storyboard, Schedule, Music, and Risk. Cost recalculation, risk assessment, rescheduling, and refreshed assets cascade automatically, all traceable through one causal audit log. Trailer generation (Veo 3.1 video + Lyria audio) is available on demand from the dashboard.

## Grafana integration (partner track requirement)

CinemaPilot actively calls the **Grafana Cloud MCP server** at runtime:

- **Risk Agent** (`agents/risk/agent.py`) — escalates high-severity, already-mitigated risks into real Grafana **Incidents** via `list_incidents`/`create_incident`, avoiding duplicates.
- **Location Agent** (`agents/location/agent.py`) — queries `list_datasources`, `list_prometheus_metric_names`, and `query_loki_logs` for observability context on a location.

Both use ADK's `McpToolset` over Streamable HTTP with OAuth 2.1 + Dynamic Client Registration against `https://mcp.grafana.com/mcp`.

## Tech stack

- **Reasoning:** Gemini (via `google-genai`)
- **Orchestration:** Google ADK (`google-adk`) for Grafana MCP tool calling
- **Data:** BigQuery (Production Graph — 16 tables, event-sourced, all queries parameterized)
- **Generative media:** `gemini-3.1-flash-image` (storyboards — [see model note](#model-note-storyboard-image-generation)), Veo 3.1 image-to-video (explicit trailer clips), Lyria 3 (music cues), Gemini TTS (multi-speaker dialogue previews)
- **Asset storage:** Google Cloud Storage, IAM-impersonated V4 signed URLs
- **Partner integration:** Grafana Cloud MCP (Incidents, Prometheus, Loki)

## Repo structure

```
agents/            One directory per agent
shared/
  graph_client/     BigQuery Production Graph interface
  asset_storage/    GCS asset upload + signed URLs
  grafana_client.py Shared Grafana MCP OAuth/toolset helper
infra/
  bigquery_ddl.sql        Full Production Graph schema
  demo_screenplay.txt     "STATIC" — the demo fixture screenplay
  grafana_oauth_bootstrap.py  One-time Grafana OAuth token setup
scripts/
  test_full_cascade.py    Full end-to-end integration test
```

## Setup

1. **Google Cloud project**
   ```
   gcloud projects create <your-project-id>
   gcloud services enable aiplatform.googleapis.com documentai.googleapis.com bigquery.googleapis.com bigqueryconnection.googleapis.com run.googleapis.com secretmanager.googleapis.com pubsub.googleapis.com
   ```
   Create a service account with `aiplatform.user`, `bigquery.dataEditor`, `secretmanager.secretAccessor`, `documentai.apiUser` roles, and run `gcloud auth application-default login`.

2. **BigQuery**
   ```
   bq mk --dataset --location=US <your-project-id>:production_graph
   bq query --use_legacy_sql=false < infra/bigquery_ddl.sql
   ```

3. **Python environment**
   ```
   python -m venv venv
   venv\Scripts\activate   (Windows)  /  source venv/bin/activate   (Mac/Linux)
   pip install -r requirements.txt
   ```

4. **Cloud Storage bucket for generated assets**
   ```
   gsutil mb -l US -b on gs://<your-bucket-name>
   ```

5. **Grafana Cloud & OpenTelemetry**
   - Create a free account at [grafana.com/products/cloud](https://grafana.com/products/cloud/)
   - As the stack admin, open the Grafana Assistant once to accept its terms (required before MCP access works)
   - Run the one-time OAuth bootstrap: `python infra/grafana_oauth_bootstrap.py` — this opens your browser once to authorize; the token is cached locally in `~/.cinemapilot/grafana_mcp_token.json` and refreshes automatically for 30 days.
   - **Environment Variables & Overrides**: Configure the following environment variables (sensible defaults are provided if unset):
     ```bash
     # Grafana Stack & MCP endpoint
     export GRAFANA_STACK_URL="https://daringhamster1557.grafana.net" # or your custom Grafana Cloud stack URL
     export GRAFANA_PROMETHEUS_UID="grafanacloud-prom"               # discovered dynamically if unset

     # OpenTelemetry Metrics Export
     export GRAFANA_CLOUD_OTLP_TOKEN="<your_grafana_cloud_access_policy_token>"
     export GRAFANA_CLOUD_INSTANCE_ID="3419920"                      # your Grafana instance ID
     export GRAFANA_CLOUD_OTLP_ENDPOINT="https://prometheus-prod-43-prod-ap-south-1.grafana.net/otlp/v1/metrics"
     ```
     *(If `GRAFANA_CLOUD_OTLP_TOKEN` is unset, telemetry automatically falls back to local console logging without interrupting execution).*

6. **Update project-specific constants** — `PROJECT`/`DATASET`/`BUCKET_NAME` in `shared/graph_client/__init__.py` and `shared/asset_storage/__init__.py`.

## Running it

**Ingest the demo screenplay:**
```
python agents/script_intelligence/agent.py
```

**Run the full end-to-end cascade demo** (the relocation scenario):
```
python scripts/test_full_cascade.py
```

This resets Scene 5 to its original location, fires a single relocation event, and shows Change Detection routing exactly the 6 affected agents in sequence, followed by Producer and Explanation synthesizing the results — with the full causal audit trail printed at the end.

**Generate a production trailer from the dashboard:**
```
uvicorn dashboard.backend:app --reload
```
Open `http://localhost:8000`, select a scene, then choose **Generate production trailer**. The selected scene supplies a completed Lyria cue when one exists. The trailer uses the first / middle / last ready storyboard panels across the timeline and may submit up to three Veo requests. If Veo is not configured or one clip fails, the dashboard clearly labels the storyboard MP4 fallback instead of calling it a Veo result.

**Deploy the dashboard to Cloud Run:**
```
gcloud run deploy cinemapilot-dashboard --source . --region <region> --timeout 900
```
Set `GEMINI_API_KEY` in the same Secret Manager flow used by storyboard generation before a live Veo demo. The 900-second request timeout is intentional because one dashboard click can wait for up to three Veo clips.

**Run an individual agent:**
```
python agents/budget/agent.py
python agents/risk/agent.py       # includes Grafana incident escalation
python agents/location/agent.py   # includes Grafana observability query
```

## Model note: storyboard image generation

The original project spec called for **Imagen 3** (`imagen-3.0-generate-002` via Vertex AI).
During development we confirmed that Imagen 3 is no longer available as a publisher model
on Vertex AI for new projects — every call returns `404 NOT_FOUND: Publisher Model
publishers/google/models/imagen-3.0-generate-002 is not found` regardless of region,
credential type, or SDK version. Google's own migration guidance (August 2025) confirms
Imagen 3 has been superseded by the Gemini native image generation family.

CinemaPilot uses **`gemini-3.1-flash-image`** — Google's current recommended image
generation model — via the Gemini Developer API. It is not a workaround or placeholder:
it produces full-resolution JPEG storyboard images (730–955 KB per scene) grounded in
real scene metadata (location, emotional tone, camera cues, character descriptions).
Google's own migration docs list `gemini-3.1-flash-image` as the recommended replacement
for teams previously on Imagen-based or `gemini-2.5-flash-image` workflows.

**One-line answer for judges:** *"Imagen 3 was deprecated by Google as of mid-2025 and
returns 404 on Vertex AI for all new projects. We use `gemini-3.1-flash-image`, Google's
current recommended generative image model, which produces equivalent quality output and
is the stated migration target in Google's own release guidance."*

## License

MIT — see [LICENSE](./LICENSE).
