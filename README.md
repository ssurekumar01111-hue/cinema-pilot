# CinemaPilot

**An autonomous AI production office for film — every department represented by a collaborating agent.**

Built for the [Agentic Cinema: The Blockbuster Hackathon](https://agentic-cinema.devpost.com/) — **Grafana partner track**.

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

**13 agents total:** Script Intelligence, Change Detection, Budget, Location, Risk, Schedule, Storyboard, Music, Director, Casting, Voice, Producer, Explanation.

**Signature demo:** moving Scene 5 from an interior warehouse to an exterior beach location triggers exactly 6 affected agents (not all 13), cascading real cost recalculation, risk assessment, rescheduling, and freshly generated storyboard/music assets — all traceable through one causal audit log.

## Grafana integration (partner track requirement)

CinemaPilot actively calls the **Grafana Cloud MCP server** at runtime:

- **Risk Agent** (`agents/risk/agent.py`) — escalates high-severity, already-mitigated risks into real Grafana **Incidents** via `list_incidents`/`create_incident`, avoiding duplicates.
- **Location Agent** (`agents/location/agent.py`) — queries `list_datasources`, `list_prometheus_metric_names`, and `query_loki_logs` for observability context on a location.

Both use ADK's `McpToolset` over Streamable HTTP with OAuth 2.1 + Dynamic Client Registration against `https://mcp.grafana.com/mcp`.

## Tech stack

- **Reasoning:** Gemini (via `google-genai`)
- **Orchestration:** Google ADK (`google-adk`) for Grafana MCP tool calling
- **Data:** BigQuery (Production Graph — 13 tables, event-sourced, all queries parameterized)
- **Generative media:** Imagen 3 (storyboards), Lyria 3 (music cues), Gemini TTS (multi-speaker dialogue previews)
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

This resets Scene 5 to its original location, fires a single relocation event, and shows Change Detection triggering exactly the 6 affected agents in sequence, followed by Producer and Explanation synthesizing the results — with the full causal audit trail printed at the end.

**Run an individual agent:**
```
python agents/budget/agent.py
python agents/risk/agent.py       # includes Grafana incident escalation
python agents/location/agent.py   # includes Grafana observability query
```

## License

MIT — see [LICENSE](./LICENSE).
