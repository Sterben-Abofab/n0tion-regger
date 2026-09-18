from __future__ import annotations

import unittest

from server import responses_message_text, responses_planner_prompt


class ResponsesTextRegressionTests(unittest.TestCase):
    def test_input_image_does_not_replace_or_mutate_text(self) -> None:
        message = {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "keep this exact request"},
                {"type": "input_image", "image_url": "data:image/png;base64,ignored-here"},
            ],
        }
        self.assertEqual(responses_message_text(message), "[user]\nkeep this exact request")

    def test_text_only_planner_prompt_remains_stable(self) -> None:
        body = {
            "instructions": "cwd: /root/project",
            "input": [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "list files"}],
            }],
            "tools": [],
        }
        prompt = responses_planner_prompt(body)
        self.assertIn("The local operator's current working directory is /root/project.", prompt)
        self.assertIn("[user]\nlist files", prompt)


if __name__ == "__main__":
    unittest.main()
