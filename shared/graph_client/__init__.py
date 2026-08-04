"""
shared/graph_client/__init__.py

Single shared interface for all CinemaPilot agents to read and write the
Production Graph stored in BigQuery.

Rules enforced by this module:
  - No agent should ever construct its own ``bigquery.Client``.
  - No agent should ever interpolate values directly into SQL strings.
  - All Production Graph I/O must go through ``ProductionGraphClient``.

Authentication:
  Uses Application Default Credentials (ADC) exclusively.
  Run ``gcloud auth application-default login`` locally, or attach a
  service account in Cloud Run / Vertex AI Agent Engine deployments.
  No credential file paths are referenced anywhere in this module.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from google.api_core.exceptions import GoogleAPIError
from google.cloud import bigquery


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class GraphClientError(Exception):
    """
    Raised when any Production Graph read/write operation fails.

    Wraps underlying BigQuery API errors, network failures, or unexpected
    runtime errors so that agent code never sees raw BigQuery stack traces.

    Example::

        try:
            client.upsert_location(record)
        except GraphClientError as e:
            logger.error("Graph write failed: %s", e)
    """


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ProductionGraphClient:
    """
    Shared BigQuery client for the CinemaPilot Production Graph.

    Authenticates exclusively via Application Default Credentials (ADC).
    No credential file paths or service-account keys are embedded here.

    All write methods auto-manage ``version`` (incremented on each update,
    set to ``1`` on first insert) and ``updated_at`` (always current UTC).

    Usage::

        client = ProductionGraphClient()

        # Write
        client.upsert_location({
            "location_id": "loc_001",
            "name": "Warehouse A",
            "location_type": "interior",
            "cost_profile": 1200.0,
            "logistics_notes": "Loading bay on east side.",
            "weather_sensitivity": False,
        })

        # Read back
        loc = client.get_location("loc_001")

        # Audit trail
        client.log_event(
            actor_agent="budget_agent",
            entity_type="location",
            entity_id="loc_001",
            before_state={},
            after_state=loc,
            triggered_agents=["schedule_agent"],
        )

        # Change detection poll
        from datetime import timedelta
        recent = client.get_events_since(datetime.now(timezone.utc) - timedelta(hours=1))
    """

    PROJECT: str = "cinemapilot-2026"
    DATASET: str = "production_graph"

    def __init__(self) -> None:
        """
        Initialise the BigQuery client using Application Default Credentials.

        Raises:
            GraphClientError: If the underlying client cannot be created
                              (e.g. ADC not configured, network unreachable).
        """
        try:
            self._bq = bigquery.Client(project=self.PROJECT)
        except Exception as exc:
            raise GraphClientError(
                f"Failed to initialise BigQuery client. "
                f"Ensure ADC is configured (`gcloud auth application-default login`). "
                f"Underlying error: {exc}"
            ) from exc

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _table(self, name: str) -> str:
        """Return a fully-qualified, backtick-quoted BigQuery table reference."""
        return f"`{self.PROJECT}.{self.DATASET}.{name}`"

    def _run(
        self,
        sql: str,
        params: list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter],
    ) -> list[dict[str, Any]]:
        """
        Execute a parameterised Standard SQL statement.

        For SELECT statements, returns all matching rows as dicts.
        For DML statements (MERGE, INSERT), returns an empty list.

        Args:
            sql:    Standard SQL string using ``@named`` parameters.
            params: Ordered list of ``ScalarQueryParameter`` or
                    ``ArrayQueryParameter`` objects matching the SQL.

        Returns:
            List of row dicts (empty for DML).

        Raises:
            GraphClientError: On any BigQuery API error or unexpected failure.
        """
        try:
            cfg = bigquery.QueryJobConfig(query_parameters=params)
            job = self._bq.query(sql, job_config=cfg)
            return [dict(row) for row in job.result()]
        except GoogleAPIError as exc:
            msg = getattr(exc, "message", str(exc))
            raise GraphClientError(f"BigQuery API error: {msg}") from exc
        except GraphClientError:
            raise  # don't double-wrap our own errors
        except Exception as exc:
            raise GraphClientError(f"Unexpected error during query: {exc}") from exc

    @staticmethod
    def _s(name: str, type_: str, value: Any) -> bigquery.ScalarQueryParameter:
        """Shorthand factory for a scalar query parameter."""
        return bigquery.ScalarQueryParameter(name, type_, value)

    @staticmethod
    def _a(name: str, item_type: str, value: list) -> bigquery.ArrayQueryParameter:
        """Shorthand factory for an array query parameter (defaults to empty list)."""
        return bigquery.ArrayQueryParameter(name, item_type, value or [])

    # -----------------------------------------------------------------------
    # SCENES
    # -----------------------------------------------------------------------

    def upsert_scene(self, record: dict) -> None:
        """
        Insert or update a scene record in the Production Graph.

        ``version`` is set to ``1`` on first insert and incremented by 1
        on every subsequent update. ``updated_at`` is always overwritten
        with the current UTC timestamp.

        Args:
            record: Dict with fields matching the ``scenes`` table schema.
                    Must include ``scene_id``. Optional fields default to
                    ``None`` / empty list if omitted.

        Raises:
            GraphClientError: If the BigQuery MERGE operation fails.
        """
        sql = f"""
        MERGE {self._table('scenes')} AS target
        USING (SELECT @scene_id AS scene_id) AS source
        ON target.scene_id = source.scene_id
        WHEN MATCHED THEN UPDATE SET
            scene_number      = @scene_number,
            location_id       = @location_id,
            character_ids     = @character_ids,
            prop_ids          = @prop_ids,
            emotional_tone    = @emotional_tone,
            camera_cues       = @camera_cues,
            timeline_position = @timeline_position,
            status            = @status,
            version           = target.version + 1,
            updated_at        = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
            (scene_id, scene_number, location_id, character_ids,
             prop_ids, emotional_tone, camera_cues, timeline_position,
             status, version, updated_at)
        VALUES
            (@scene_id, @scene_number, @location_id, @character_ids,
             @prop_ids, @emotional_tone, @camera_cues, @timeline_position,
             @status, 1, CURRENT_TIMESTAMP())
        """
        self._run(sql, [
            self._s("scene_id",          "STRING",  record.get("scene_id")),
            self._s("scene_number",       "INT64",   record.get("scene_number")),
            self._s("location_id",        "STRING",  record.get("location_id")),
            self._a("character_ids",      "STRING",  record.get("character_ids", [])),
            self._a("prop_ids",           "STRING",  record.get("prop_ids", [])),
            self._s("emotional_tone",     "STRING",  record.get("emotional_tone")),
            self._a("camera_cues",        "STRING",  record.get("camera_cues", [])),
            self._s("timeline_position",  "INT64",   record.get("timeline_position")),
            self._s("status",             "STRING",  record.get("status", "draft")),
        ])

    def get_scene(self, scene_id: str) -> dict | None:
        """
        Fetch a single scene record by ID.

        Args:
            scene_id: The scene's unique identifier.

        Returns:
            A dict of the scene record, or ``None`` if not found.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('scenes')}
        WHERE scene_id = @scene_id
        LIMIT 1
        """
        rows = self._run(sql, [self._s("scene_id", "STRING", scene_id)])
        return rows[0] if rows else None

    def list_scenes(self) -> list[dict]:
        """
        Return all scene records ordered by timeline position.

        Returns:
            List of scene dicts, sorted ascending by ``timeline_position``.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"SELECT * FROM {self._table('scenes')} ORDER BY timeline_position ASC"
        return self._run(sql, [])

    # -----------------------------------------------------------------------
    # CHARACTERS
    # -----------------------------------------------------------------------

    def upsert_character(self, record: dict) -> None:
        """
        Insert or update a character record in the Production Graph.

        Args:
            record: Dict with fields matching the ``characters`` table schema.
                    Must include ``character_id``.

        Raises:
            GraphClientError: If the BigQuery MERGE operation fails.
        """
        sql = f"""
        MERGE {self._table('characters')} AS target
        USING (SELECT @character_id AS character_id) AS source
        ON target.character_id = source.character_id
        WHEN MATCHED THEN UPDATE SET
            name          = @name,
            description   = @description,
            costume_notes = @costume_notes,
            scene_ids     = @scene_ids,
            version       = target.version + 1,
            updated_at    = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
            (character_id, name, description, costume_notes,
             scene_ids, version, updated_at)
        VALUES
            (@character_id, @name, @description, @costume_notes,
             @scene_ids, 1, CURRENT_TIMESTAMP())
        """
        self._run(sql, [
            self._s("character_id",  "STRING",  record.get("character_id")),
            self._s("name",          "STRING",  record.get("name")),
            self._s("description",   "STRING",  record.get("description")),
            self._a("costume_notes", "STRING",  record.get("costume_notes", [])),
            self._a("scene_ids",     "STRING",  record.get("scene_ids", [])),
        ])

    def get_character(self, character_id: str) -> dict | None:
        """
        Fetch a single character record by ID.

        Args:
            character_id: The character's unique identifier.

        Returns:
            A dict of the character record, or ``None`` if not found.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('characters')}
        WHERE character_id = @character_id
        LIMIT 1
        """
        rows = self._run(sql, [self._s("character_id", "STRING", character_id)])
        return rows[0] if rows else None

    def list_characters(self) -> list[dict]:
        """
        Return all character records ordered alphabetically by name.

        Returns:
            List of character dicts.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"SELECT * FROM {self._table('characters')} ORDER BY name ASC"
        return self._run(sql, [])

    # -----------------------------------------------------------------------
    # LOCATIONS
    # -----------------------------------------------------------------------

    def upsert_location(self, record: dict) -> None:
        """
        Insert or update a location record in the Production Graph.

        ``version`` is set to ``1`` on first insert and incremented by 1
        on every subsequent update. ``updated_at`` is always overwritten
        with the current UTC timestamp.

        Args:
            record: Dict with fields matching the ``locations`` table schema.
                    Must include ``location_id``. Optional fields:
                    ``name``, ``location_type``, ``cost_profile`` (float,
                    defaults to 0.0), ``logistics_notes``,
                    ``weather_sensitivity`` (bool, defaults to False).

        Raises:
            GraphClientError: If the BigQuery MERGE operation fails.
        """
        sql = f"""
        MERGE {self._table('locations')} AS target
        USING (SELECT @location_id AS location_id) AS source
        ON target.location_id = source.location_id
        WHEN MATCHED THEN UPDATE SET
            name                = @name,
            location_type       = @location_type,
            cost_profile        = @cost_profile,
            logistics_notes     = @logistics_notes,
            weather_sensitivity = @weather_sensitivity,
            version             = target.version + 1,
            updated_at          = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
            (location_id, name, location_type, cost_profile,
             logistics_notes, weather_sensitivity, version, updated_at)
        VALUES
            (@location_id, @name, @location_type, @cost_profile,
             @logistics_notes, @weather_sensitivity, 1, CURRENT_TIMESTAMP())
        """
        self._run(sql, [
            self._s("location_id",          "STRING",  record.get("location_id")),
            self._s("name",                 "STRING",  record.get("name")),
            self._s("location_type",        "STRING",  record.get("location_type")),
            self._s("cost_profile",         "FLOAT64", record.get("cost_profile", 0.0)),
            self._s("logistics_notes",      "STRING",  record.get("logistics_notes")),
            self._s("weather_sensitivity",  "BOOL",    record.get("weather_sensitivity", False)),
        ])

    def get_location(self, location_id: str) -> dict | None:
        """
        Fetch a single location record by ID.

        Args:
            location_id: The location's unique identifier.

        Returns:
            A dict of the location record, or ``None`` if not found.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('locations')}
        WHERE location_id = @location_id
        LIMIT 1
        """
        rows = self._run(sql, [self._s("location_id", "STRING", location_id)])
        return rows[0] if rows else None

    def list_locations(self) -> list[dict]:
        """
        Return all location records ordered by most recently updated.

        Returns:
            List of location dicts.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"SELECT * FROM {self._table('locations')} ORDER BY updated_at DESC"
        return self._run(sql, [])

    # -----------------------------------------------------------------------
    # PROPS
    # -----------------------------------------------------------------------

    def upsert_prop(self, record: dict) -> None:
        """
        Insert or update a prop record in the Production Graph.

        Args:
            record: Dict with fields matching the ``props`` table schema.
                    Must include ``prop_id``.

        Raises:
            GraphClientError: If the BigQuery MERGE operation fails.
        """
        sql = f"""
        MERGE {self._table('props')} AS target
        USING (SELECT @prop_id AS prop_id) AS source
        ON target.prop_id = source.prop_id
        WHEN MATCHED THEN UPDATE SET
            name           = @name,
            scene_ids      = @scene_ids,
            sourcing_notes = @sourcing_notes,
            version        = target.version + 1,
            updated_at     = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
            (prop_id, name, scene_ids, sourcing_notes, version, updated_at)
        VALUES
            (@prop_id, @name, @scene_ids, @sourcing_notes, 1, CURRENT_TIMESTAMP())
        """
        self._run(sql, [
            self._s("prop_id",        "STRING",  record.get("prop_id")),
            self._s("name",           "STRING",  record.get("name")),
            self._a("scene_ids",      "STRING",  record.get("scene_ids", [])),
            self._s("sourcing_notes", "STRING",  record.get("sourcing_notes")),
        ])

    def get_prop(self, prop_id: str) -> dict | None:
        """
        Fetch a single prop record by ID.

        Args:
            prop_id: The prop's unique identifier.

        Returns:
            A dict of the prop record, or ``None`` if not found.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('props')}
        WHERE prop_id = @prop_id
        LIMIT 1
        """
        rows = self._run(sql, [self._s("prop_id", "STRING", prop_id)])
        return rows[0] if rows else None

    def list_props(self) -> list[dict]:
        """
        Return all prop records ordered alphabetically by name.

        Returns:
            List of prop dicts.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"SELECT * FROM {self._table('props')} ORDER BY name ASC"
        return self._run(sql, [])

    # -----------------------------------------------------------------------
    # BUDGET LINES
    # -----------------------------------------------------------------------

    def upsert_budget_line(self, record: dict) -> None:
        """
        Insert or update a budget line record in the Production Graph.

        Args:
            record: Dict with fields matching the ``budget_lines`` table schema.
                    Must include ``budget_line_id``. The ``last_changed_by_agent``
                    field should always be set to the calling agent's name.

        Raises:
            GraphClientError: If the BigQuery MERGE operation fails.
        """
        sql = f"""
        MERGE {self._table('budget_lines')} AS target
        USING (SELECT @budget_line_id AS budget_line_id) AS source
        ON target.budget_line_id = source.budget_line_id
        WHEN MATCHED THEN UPDATE SET
            category              = @category,
            amount                = @amount,
            linked_entity_id      = @linked_entity_id,
            last_changed_by_agent = @last_changed_by_agent,
            reason                = @reason,
            version               = target.version + 1,
            updated_at            = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
            (budget_line_id, category, amount, linked_entity_id,
             last_changed_by_agent, reason, version, updated_at)
        VALUES
            (@budget_line_id, @category, @amount, @linked_entity_id,
             @last_changed_by_agent, @reason, 1, CURRENT_TIMESTAMP())
        """
        self._run(sql, [
            self._s("budget_line_id",        "STRING",  record.get("budget_line_id")),
            self._s("category",              "STRING",  record.get("category")),
            self._s("amount",                "FLOAT64", record.get("amount", 0.0)),
            self._s("linked_entity_id",      "STRING",  record.get("linked_entity_id")),
            self._s("last_changed_by_agent", "STRING",  record.get("last_changed_by_agent")),
            self._s("reason",                "STRING",  record.get("reason")),
        ])

    def get_budget_line(self, budget_line_id: str) -> dict | None:
        """
        Fetch a single budget line record by ID.

        Args:
            budget_line_id: The budget line's unique identifier.

        Returns:
            A dict of the budget line record, or ``None`` if not found.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('budget_lines')}
        WHERE budget_line_id = @budget_line_id
        LIMIT 1
        """
        rows = self._run(sql, [self._s("budget_line_id", "STRING", budget_line_id)])
        return rows[0] if rows else None

    def list_budget_lines(self) -> list[dict]:
        """
        Return all budget line records ordered by category then updated_at.

        Returns:
            List of budget line dicts.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('budget_lines')}
        ORDER BY category ASC, updated_at DESC
        """
        return self._run(sql, [])

    # -----------------------------------------------------------------------
    # SCHEDULE BLOCKS
    # -----------------------------------------------------------------------

    def upsert_schedule_block(self, record: dict) -> None:
        """
        Insert or update a schedule block record in the Production Graph.

        Args:
            record: Dict with fields matching the ``schedule_blocks`` table schema.
                    Must include ``schedule_block_id``.

        Raises:
            GraphClientError: If the BigQuery MERGE operation fails.
        """
        sql = f"""
        MERGE {self._table('schedule_blocks')} AS target
        USING (SELECT @schedule_block_id AS schedule_block_id) AS source
        ON target.schedule_block_id = source.schedule_block_id
        WHEN MATCHED THEN UPDATE SET
            scene_id         = @scene_id,
            day_index        = @day_index,
            duration_minutes = @duration_minutes,
            constraints      = @constraints,
            version          = target.version + 1,
            updated_at       = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
            (schedule_block_id, scene_id, day_index,
             duration_minutes, constraints, version, updated_at)
        VALUES
            (@schedule_block_id, @scene_id, @day_index,
             @duration_minutes, @constraints, 1, CURRENT_TIMESTAMP())
        """
        self._run(sql, [
            self._s("schedule_block_id", "STRING",  record.get("schedule_block_id")),
            self._s("scene_id",          "STRING",  record.get("scene_id")),
            self._s("day_index",         "INT64",   record.get("day_index")),
            self._s("duration_minutes",  "INT64",   record.get("duration_minutes")),
            self._a("constraints",       "STRING",  record.get("constraints", [])),
        ])

    def get_schedule_block(self, schedule_block_id: str) -> dict | None:
        """
        Fetch a single schedule block record by ID.

        Args:
            schedule_block_id: The schedule block's unique identifier.

        Returns:
            A dict of the schedule block record, or ``None`` if not found.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('schedule_blocks')}
        WHERE schedule_block_id = @schedule_block_id
        LIMIT 1
        """
        rows = self._run(sql, [self._s("schedule_block_id", "STRING", schedule_block_id)])
        return rows[0] if rows else None

    def list_schedule_blocks(self) -> list[dict]:
        """
        Return all schedule block records ordered by shoot day then duration.

        Returns:
            List of schedule block dicts.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('schedule_blocks')}
        ORDER BY day_index ASC, duration_minutes ASC
        """
        return self._run(sql, [])

    def get_schedule_blocks_for_scene(self, scene_id: str) -> list[dict]:
        """
        Return all schedule block records for a given scene_id.

        Args:
            scene_id: The unique ID of the scene.

        Returns:
            List of schedule block dicts matching the scene_id.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('schedule_blocks')}
        WHERE scene_id = @scene_id
        ORDER BY day_index ASC
        """
        return self._run(sql, [self._s("scene_id", "STRING", scene_id)])

    # -----------------------------------------------------------------------
    # RISK FLAGS
    # -----------------------------------------------------------------------

    def upsert_risk_flag(self, record: dict) -> None:
        """
        Insert or update a risk flag record in the Production Graph.

        Args:
            record: Dict with fields matching the ``risk_flags`` table schema.
                    Must include ``risk_flag_id``.

        Raises:
            GraphClientError: If the BigQuery MERGE operation fails.
        """
        sql = f"""
        MERGE {self._table('risk_flags')} AS target
        USING (SELECT @risk_flag_id AS risk_flag_id) AS source
        ON target.risk_flag_id = source.risk_flag_id
        WHEN MATCHED THEN UPDATE SET
            linked_entity_id = @linked_entity_id,
            severity         = @severity,
            description      = @description,
            mitigation       = @mitigation,
            version          = target.version + 1,
            updated_at       = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
            (risk_flag_id, linked_entity_id, severity,
             description, mitigation, version, updated_at)
        VALUES
            (@risk_flag_id, @linked_entity_id, @severity,
             @description, @mitigation, 1, CURRENT_TIMESTAMP())
        """
        self._run(sql, [
            self._s("risk_flag_id",     "STRING", record.get("risk_flag_id")),
            self._s("linked_entity_id", "STRING", record.get("linked_entity_id")),
            self._s("severity",         "STRING", record.get("severity")),
            self._s("description",      "STRING", record.get("description")),
            self._s("mitigation",       "STRING", record.get("mitigation")),
        ])

    def get_risk_flag(self, risk_flag_id: str) -> dict | None:
        """
        Fetch a single risk flag record by ID.

        Args:
            risk_flag_id: The risk flag's unique identifier.

        Returns:
            A dict of the risk flag record, or ``None`` if not found.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('risk_flags')}
        WHERE risk_flag_id = @risk_flag_id
        LIMIT 1
        """
        rows = self._run(sql, [self._s("risk_flag_id", "STRING", risk_flag_id)])
        return rows[0] if rows else None

    def list_risk_flags(self) -> list[dict]:
        """
        Return all risk flag records ordered by severity (descending) then recency.

        Returns:
            List of risk flag dicts.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('risk_flags')}
        ORDER BY severity DESC, updated_at DESC
        """
        return self._run(sql, [])

    def get_unmitigated_risk_flags_for_entity(self, linked_entity_id: str) -> list[dict]:
        """
        Return unmitigated risk flags (where mitigation is empty or null) for a given linked_entity_id.

        Args:
            linked_entity_id: The ID of the linked entity (e.g. a location_id or scene_id).

        Returns:
            List of risk flag dicts matching the criteria.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('risk_flags')}
        WHERE linked_entity_id = @linked_entity_id
          AND (mitigation IS NULL OR TRIM(mitigation) = '')
        ORDER BY severity DESC, updated_at DESC
        """
        return self._run(sql, [self._s("linked_entity_id", "STRING", linked_entity_id)])

    # -----------------------------------------------------------------------
    # STORYBOARDS
    # -----------------------------------------------------------------------

    def upsert_storyboard(self, record: dict) -> None:
        """
        Insert or update a storyboard record in the Production Graph.

        Args:
            record: Dict with fields matching the ``storyboards`` table schema.
                    Must include ``storyboard_id`` and ``scene_id``.

        Raises:
            GraphClientError: If the BigQuery MERGE operation fails.
        """
        sql = f"""
        MERGE {self._table('storyboards')} AS target
        USING (SELECT @storyboard_id AS storyboard_id) AS source
        ON target.storyboard_id = source.storyboard_id
        WHEN MATCHED THEN UPDATE SET
            scene_id    = @scene_id,
            gs_uri      = @gs_uri,
            prompt_used = @prompt_used,
            version     = target.version + 1,
            updated_at  = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
            (storyboard_id, scene_id, gs_uri, prompt_used, version, updated_at)
        VALUES
            (@storyboard_id, @scene_id, @gs_uri, @prompt_used, 1, CURRENT_TIMESTAMP())
        """
        self._run(sql, [
            self._s("storyboard_id", "STRING", record.get("storyboard_id")),
            self._s("scene_id",      "STRING", record.get("scene_id")),
            self._s("gs_uri",        "STRING", record.get("gs_uri")),
            self._s("prompt_used",   "STRING", record.get("prompt_used")),
        ])

    def get_storyboard(self, storyboard_id: str) -> dict | None:
        """
        Fetch a single storyboard record by ID.

        Args:
            storyboard_id: The storyboard's unique identifier.

        Returns:
            A dict of the storyboard record, or ``None`` if not found.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('storyboards')}
        WHERE storyboard_id = @storyboard_id
        LIMIT 1
        """
        rows = self._run(sql, [self._s("storyboard_id", "STRING", storyboard_id)])
        return rows[0] if rows else None

    # -----------------------------------------------------------------------
    # MUSIC CUES
    # -----------------------------------------------------------------------

    def upsert_music_cue(self, record: dict) -> None:
        """
        Insert or update a music cue record in the Production Graph.

        Args:
            record: Dict with fields matching the ``music_cues`` table schema.
                    Must include ``music_cue_id`` and ``scene_id``.

        Raises:
            GraphClientError: If the BigQuery MERGE operation fails.
        """
        sql = f"""
        MERGE {self._table('music_cues')} AS target
        USING (SELECT @music_cue_id AS music_cue_id) AS source
        ON target.music_cue_id = source.music_cue_id
        WHEN MATCHED THEN UPDATE SET
            scene_id    = @scene_id,
            gs_uri      = @gs_uri,
            lyrics      = @lyrics,
            description = @description,
            prompt_used = @prompt_used,
            status      = @status,
            version     = target.version + 1,
            updated_at  = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
            (music_cue_id, scene_id, gs_uri, lyrics, description, prompt_used, status, version, updated_at)
        VALUES
            (@music_cue_id, @scene_id, @gs_uri, @lyrics, @description, @prompt_used, @status, 1, CURRENT_TIMESTAMP())
        """
        self._run(sql, [
            self._s("music_cue_id", "STRING", record.get("music_cue_id")),
            self._s("scene_id",     "STRING", record.get("scene_id")),
            self._s("gs_uri",       "STRING", record.get("gs_uri")),
            self._s("lyrics",       "STRING", record.get("lyrics")),
            self._s("description",  "STRING", record.get("description")),
            self._s("prompt_used",  "STRING", record.get("prompt_used")),
            self._s("status",       "STRING", record.get("status", "pending")),
        ])

    def get_music_cue(self, music_cue_id: str) -> dict | None:
        """
        Fetch a single music cue record by ID.

        Args:
            music_cue_id: The music cue's unique identifier.

        Returns:
            A dict of the music cue record, or ``None`` if not found.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT * FROM {self._table('music_cues')}
        WHERE music_cue_id = @music_cue_id
        LIMIT 1
        """
        rows = self._run(sql, [self._s("music_cue_id", "STRING", music_cue_id)])
        return rows[0] if rows else None

    # -----------------------------------------------------------------------
    # EVENTS (append-only audit log)
    # -----------------------------------------------------------------------

    def log_event(
        self,
        actor_agent: str,
        entity_type: str,
        entity_id: str,
        before_state: dict,
        after_state: dict,
        triggered_agents: list[str],
    ) -> str:
        """
        Append an immutable event record to the audit log.

        Generates a UUID v4 for ``event_id`` and sets ``event_timestamp``
        to the current UTC time. Events are **never updated** — they are
        an append-only record of every state change in the Production Graph.

        Args:
            actor_agent:      Name of the agent writing the change
                              (e.g. ``"budget_agent"``).
            entity_type:      Type of entity changed
                              (e.g. ``"location"``, ``"scene"``).
            entity_id:        Unique ID of the changed entity.
            before_state:     Dict of entity state *before* the change.
                              Pass ``{}`` for new entity insertions.
            after_state:      Dict of entity state *after* the change.
            triggered_agents: List of agent names to be notified as a
                              downstream consequence of this event.

        Returns:
            The generated ``event_id`` UUID string.

        Raises:
            GraphClientError: If the BigQuery INSERT operation fails.
        """
        event_id = str(uuid.uuid4())
        sql = f"""
        INSERT INTO {self._table('events')}
            (event_id, event_timestamp, actor_agent, entity_type,
             entity_id, before_state, after_state, triggered_agents)
        VALUES
            (@event_id, CURRENT_TIMESTAMP(), @actor_agent, @entity_type,
             @entity_id,
             PARSE_JSON(@before_state),
             PARSE_JSON(@after_state),
             @triggered_agents)
        """
        self._run(sql, [
            self._s("event_id",         "STRING",  event_id),
            self._s("actor_agent",      "STRING",  actor_agent),
            self._s("entity_type",      "STRING",  entity_type),
            self._s("entity_id",        "STRING",  entity_id),
            self._s("before_state",     "STRING",  json.dumps(before_state)),
            self._s("after_state",      "STRING",  json.dumps(after_state)),
            self._a("triggered_agents", "STRING",  triggered_agents),
        ])
        return event_id

    def get_event(self, event_id: str) -> dict | None:
        """
        Fetch a single event record by ID.

        JSON columns (``before_state``, ``after_state``) are returned as
        serialised JSON strings. Parse with ``json.loads()`` as needed.

        Args:
            event_id: The event's UUID identifier.

        Returns:
            A dict of the event record, or ``None`` if not found.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT
            event_id,
            event_timestamp,
            actor_agent,
            entity_type,
            entity_id,
            TO_JSON_STRING(before_state) AS before_state,
            TO_JSON_STRING(after_state)  AS after_state,
            triggered_agents
        FROM {self._table('events')}
        WHERE event_id = @event_id
        LIMIT 1
        """
        rows = self._run(sql, [self._s("event_id", "STRING", event_id)])
        return rows[0] if rows else None

    def list_events(self) -> list[dict]:
        """
        Return all event records, most recent first.

        JSON columns are returned as serialised strings.

        Returns:
            List of event dicts.

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        sql = f"""
        SELECT
            event_id,
            event_timestamp,
            actor_agent,
            entity_type,
            entity_id,
            TO_JSON_STRING(before_state) AS before_state,
            TO_JSON_STRING(after_state)  AS after_state,
            triggered_agents
        FROM {self._table('events')}
        ORDER BY event_timestamp DESC
        """
        return self._run(sql, [])

    def get_events_since(self, since: datetime) -> list[dict]:
        """
        Return all events that occurred after a given UTC timestamp.

        Designed for the Change Detection Agent to efficiently poll recent
        activity without a full table scan.

        Args:
            since: A ``datetime`` object representing the lower bound
                   (exclusive). Timezone-naive values are assumed UTC.
                   Any event with ``event_timestamp > since`` is returned.

        Returns:
            List of event dicts ordered by ``event_timestamp`` ascending
            (oldest-first, so callers can process in causal order).

        Raises:
            GraphClientError: If the BigQuery operation fails.
        """
        # Normalise to UTC before passing to BigQuery
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        else:
            since = since.astimezone(timezone.utc)

        sql = f"""
        SELECT
            event_id,
            event_timestamp,
            actor_agent,
            entity_type,
            entity_id,
            TO_JSON_STRING(before_state) AS before_state,
            TO_JSON_STRING(after_state)  AS after_state,
            triggered_agents
        FROM {self._table('events')}
        WHERE event_timestamp > @since
        ORDER BY event_timestamp ASC
        """
        return self._run(sql, [self._s("since", "TIMESTAMP", since)])
