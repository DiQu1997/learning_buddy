from __future__ import annotations

import unittest

from learning_buddy.config import DEFAULT_NOTE_PROMPT
from learning_buddy.nlm_client import NLMCLI


class NLMClientTests(unittest.TestCase):
    def test_note_command_uses_custom_report_prompt(self) -> None:
        client = NLMCLI()

        command = client._build_create_command(
            notebook_id="nb-123",
            artifact_type="note",
            source_id="src-456",
            report_format="Study Guide",
            note_prompt=DEFAULT_NOTE_PROMPT,
        )

        self.assertEqual(
            command,
            [
                "report",
                "create",
                "nb-123",
                "--format",
                "Create Your Own",
                "--prompt",
                DEFAULT_NOTE_PROMPT,
                "--source-ids",
                "src-456",
                "--confirm",
            ],
        )


if __name__ == "__main__":
    unittest.main()
