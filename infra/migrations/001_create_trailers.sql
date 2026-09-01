CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.trailers` (
  trailer_id STRING NOT NULL,
  source_scene_id STRING,
  source_scene_ids ARRAY<STRING>,
  music_source_scene_id STRING,
  gs_uri STRING,
  status STRING,
  generation_mode STRING,
  clip_count INT64,
  fallback_scene_ids ARRAY<STRING>,
  captioned_scene_ids ARRAY<STRING>,
  error STRING,
  version INT64,
  updated_at TIMESTAMP
);
