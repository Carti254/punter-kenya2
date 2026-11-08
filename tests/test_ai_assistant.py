import unittest
from types import SimpleNamespace
from unittest.mock import patch

import ai_assistant


class FakeResponses:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.output)


class FakeClient:
    def __init__(self, output):
        self.responses = FakeResponses(output)


class AIAssistantTests(unittest.TestCase):
    def test_json_parser_accepts_json_fence(self):
        result = ai_assistant._json_from_response(
            SimpleNamespace(output_text='\`\`\`json\\n{"action":"help"}\\n\`\`\`')
        )
        self.assertEqual(result["action"], "help")

    def test_interpret_request_sanitises_action_and_fixture(self):
        fake = FakeClient('{"action":"deepdive","fixture":"Arsenal vs Chelsea","date":"today","market":"all","target_odds":4.5}')
        with patch.object(ai_assistant, "_client", return_value=fake):
            result = ai_assistant.interpret_request("Analyse Arsenal vs Chelsea")
        self.assertEqual(result["action"], "deepdive")
        self.assertEqual(result["fixture"], "Arsenal vs Chelsea")
        self.assertEqual(result["date"], "today")
        self.assertEqual(result["target_odds"], 4.5)
        self.assertEqual(fake.responses.calls[0]["model"], "gpt-4.1-mini")

    def test_unknown_action_and_invalid_fixture_fall_back_safely(self):
        fake = FakeClient('{"action":"place_bet","fixture":"somewhere","date":"yesterday","target_odds":0}')
        with patch.object(ai_assistant, "_client", return_value=fake):
            result = ai_assistant.interpret_request("Do something")
        self.assertEqual(result["action"], "help")
        self.assertEqual(result["fixture"], "")
        self.assertEqual(result["date"], "both")
        self.assertEqual(result["target_odds"], 5.0)

    def test_image_reader_requires_complete_selection(self):
        fake = FakeClient('{"selections":[{"fixture":"Arsenal vs Chelsea","market":"1X2","selection":"Home"},{"fixture":"unknown","market":"1X2","selection":"Away"},{"fixture":"Liverpool vs City","market":"","selection":"Home"}]}')
        with patch.object(ai_assistant, "_client", return_value=fake):
            rows = ai_assistant.read_slip_image(b"image-bytes", "image/jpeg")
        self.assertEqual(rows, ["Arsenal vs Chelsea | 1X2 | Home"])
        self.assertEqual(fake.responses.calls[0]["input"][0]["content"][1]["type"], "input_image")

    def test_image_reader_rejects_oversized_file(self):
        with self.assertRaises(ValueError):
            ai_assistant.read_slip_image(b"x" * (12 * 1024 * 1024 + 1))


if __name__ == "__main__":
    unittest.main()
