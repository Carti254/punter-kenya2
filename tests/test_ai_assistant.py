import unittest
from unittest.mock import patch

import ai_assistant


class FakeResponse:
    status_code = 200
    def __init__(self, text):
        self._body = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    def json(self):
        return self._body
    def raise_for_status(self):
        return None


class AIAssistantTests(unittest.TestCase):
    def setUp(self):
        self.key_patch = patch.dict("os.environ", {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "gemini-test"})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)

    def test_json_parser_accepts_plain_json(self):
        result = ai_assistant._json_from_response('{"action":"help"}')
        self.assertEqual(result["action"], "help")

    def test_interpret_request_sanitises_action_and_fixture(self):
        response = FakeResponse('{"action":"deepdive","fixture":"Arsenal vs Chelsea","date":"today","market":"all","target_odds":4.5}')
        with patch.object(ai_assistant.requests, "post", return_value=response) as post:
            result = ai_assistant.interpret_request("Analyse Arsenal vs Chelsea")
        self.assertEqual(result["action"], "deepdive")
        self.assertEqual(result["fixture"], "Arsenal vs Chelsea")
        self.assertEqual(result["date"], "today")
        self.assertEqual(result["target_odds"], 4.5)
        self.assertIn("models/gemini-test:generateContent", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "test-key")

    def test_unknown_action_and_invalid_fixture_fall_back_safely(self):
        response = FakeResponse('{"action":"place_bet","fixture":"somewhere","date":"yesterday","target_odds":0}')
        with patch.object(ai_assistant.requests, "post", return_value=response):
            result = ai_assistant.interpret_request("Do something")
        self.assertEqual(result["action"], "help")
        self.assertEqual(result["fixture"], "")
        self.assertEqual(result["date"], "both")
        self.assertEqual(result["target_odds"], 5.0)

    def test_image_reader_requires_complete_selection(self):
        response = FakeResponse('{"selections":[{"fixture":"Arsenal vs Chelsea","market":"1X2","selection":"Home"},{"fixture":"unknown","market":"1X2","selection":"Away"},{"fixture":"Liverpool vs City","market":"","selection":"Home"}]}')
        with patch.object(ai_assistant.requests, "post", return_value=response) as post:
            rows = ai_assistant.read_slip_image(b"image-bytes", "image/jpeg")
        self.assertEqual(rows, ["Arsenal vs Chelsea | 1X2 | Home"])
        parts = post.call_args.kwargs["json"]["contents"][0]["parts"]
        self.assertIn("inline_data", parts[1])

    def test_image_reader_rejects_oversized_file(self):
        with self.assertRaises(ValueError):
            ai_assistant.read_slip_image(b"x" * (12 * 1024 * 1024 + 1))

    def test_ai_requires_gemini_key(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            self.assertFalse(ai_assistant.ai_is_configured())


if __name__ == "__main__":
    unittest.main()
