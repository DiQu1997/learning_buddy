from __future__ import annotations

import unittest

from learning_buddy.config import EngineConfig


class ConfigTests(unittest.TestCase):
    def test_default_artifacts(self) -> None:
        cfg = EngineConfig.from_dict({})
        self.assertEqual(cfg.resolve_artifacts(None), ["report", "slide_deck"])

    def test_artifact_normalization(self) -> None:
        cfg = EngineConfig.from_dict({})
        resolved = cfg.resolve_artifacts(["slides", "mindmap", "audio"])
        self.assertEqual(resolved, ["slide_deck", "mind_map", "audio"])

    def test_invalid_artifact(self) -> None:
        cfg = EngineConfig.from_dict({})
        with self.assertRaises(ValueError):
            cfg.resolve_artifacts(["unknown"])

    def test_rate_control_defaults(self) -> None:
        cfg = EngineConfig.from_dict({})
        self.assertEqual(cfg.max_concurrent_generations, 2)
        self.assertEqual(cfg.courtesy_delay_seconds, 5)
        self.assertEqual(cfg.nlm_min_request_interval_seconds, 1.0)
        self.assertTrue(cfg.cleanup_failed_remote_artifacts)


if __name__ == "__main__":
    unittest.main()
