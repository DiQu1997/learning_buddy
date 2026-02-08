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


if __name__ == "__main__":
    unittest.main()
