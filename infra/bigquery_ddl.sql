CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.scenes` (
  scene_id STRING NOT NULL,
  scene_number INT64,
  location_id STRING,
  character_ids ARRAY<STRING>,
  prop_ids ARRAY<STRING>,
  emotional_tone STRING,
  camera_cues ARRAY<STRING>,
  timeline_position INT64,
  status STRING,
  version INT64,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.characters` (
  character_id STRING NOT NULL,
  name STRING,
  description STRING,
  costume_notes ARRAY<STRING>,
  scene_ids ARRAY<STRING>,
  version INT64,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.locations` (
  location_id STRING NOT NULL,
  name STRING,
  location_type STRING,
  cost_profile FLOAT64,
  logistics_notes STRING,
  weather_sensitivity BOOL,
  version INT64,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.props` (
  prop_id STRING NOT NULL,
  name STRING,
  scene_ids ARRAY<STRING>,
  sourcing_notes STRING,
  version INT64,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.budget_lines` (
  budget_line_id STRING NOT NULL,
  category STRING,
  amount FLOAT64,
  linked_entity_id STRING,
  last_changed_by_agent STRING,
  reason STRING,
  version INT64,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.schedule_blocks` (
  schedule_block_id STRING NOT NULL,
  scene_id STRING,
  day_index INT64,
  duration_minutes INT64,
  constraints ARRAY<STRING>,
  version INT64,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.risk_flags` (
  risk_flag_id STRING NOT NULL,
  linked_entity_id STRING,
  severity STRING,
  description STRING,
  mitigation STRING,
  version INT64,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.events` (
  event_id STRING NOT NULL,
  event_timestamp TIMESTAMP,
  actor_agent STRING,
  entity_type STRING,
  entity_id STRING,
  before_state JSON,
  after_state JSON,
  triggered_agents ARRAY<STRING>
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.storyboards` (
  storyboard_id STRING NOT NULL,
  scene_id STRING NOT NULL,
  gs_uri STRING,
  prompt_used STRING,
  version INT64,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.music_cues` (
  music_cue_id STRING NOT NULL,
  scene_id STRING NOT NULL,
  gs_uri STRING,
  lyrics STRING,
  description STRING,
  prompt_used STRING,
  status STRING,
  version INT64,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.director_notes` (
  director_note_id STRING NOT NULL,
  scene_id STRING NOT NULL,
  shot_suggestions ARRAY<STRING>,
  pacing_notes STRING,
  camera_plan STRING,
  version INT64,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.character_sheets` (
  character_sheet_id STRING NOT NULL,
  character_id STRING NOT NULL,
  summary STRING,
  personality_notes STRING,
  costume_considerations STRING,
  scene_count INT64,
  version INT64,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.voice_previews` (
  voice_preview_id STRING NOT NULL,
  scene_id STRING NOT NULL,
  dialogue_lines JSON,
  gs_uri STRING,
  version INT64,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.producer_overviews` (
  producer_overview_id STRING NOT NULL,
  scene_id STRING NOT NULL,
  overview_summary STRING,
  total_budget_impact FLOAT64,
  schedule_status STRING,
  outstanding_risks ARRAY<STRING>,
  recommendation STRING,
  version INT64,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.explanations` (
  explanation_id STRING NOT NULL,
  scene_id STRING NOT NULL,
  narrative STRING,
  sources_used ARRAY<STRING>,
  version INT64,
  updated_at TIMESTAMP
);







