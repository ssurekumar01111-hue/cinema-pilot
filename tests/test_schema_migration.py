from __future__ import annotations

from pathlib import Path
import unittest


class TrailerSchemaMigrationTests(unittest.TestCase):
    def test_existing_dataset_migration_creates_trailers_table(self):
        migration = Path("infra/migrations/001_create_trailers.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS `cinemapilot-2026.production_graph.trailers`", migration)
        self.assertIn("source_scene_ids ARRAY<STRING>", migration)
        self.assertIn("captioned_scene_ids ARRAY<STRING>", migration)
