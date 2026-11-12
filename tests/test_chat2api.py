"""Kiểm tra tích hợp chat2api, không gửi token ra mạng."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import douyin_dub as d


class Chat2ApiTests(unittest.TestCase):
    def test_urls_preserve_api_prefix(self):
        for url in ("http://localhost:5005/private", "http://localhost:5005/private/v1/",
                    "http://localhost:5005/private/v1/chat/completions"):
            self.assertEqual(d.normalize_chat2api_url(url), "http://localhost:5005/private/v1")
        for url in ("file:///tmp/api", "https://user:secret@example.com", "http://localhost/?token=secret"):
            with self.assertRaises(ValueError):
                d.normalize_chat2api_url(url)

    def test_short_gateway_key_and_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"ai_provider": "chat2api", "ai_key": "local",
                                          "ai_base_url": "http://localhost:5005/prefix"}), encoding="utf-8")
            with patch.object(d, "CONFIG_PATH", config), patch.dict(os.environ, {}, clear=True):
                cfg = d.load_ai_config()
                self.assertEqual(cfg["base_url"], "http://localhost:5005/prefix/v1")
                self.assertEqual(cfg["model"], "gpt-4o")
                with patch.dict(os.environ, {"DUB_AI_BASE_URL": "http://localhost:6000", "DUB_AI_MODEL": "custom"}):
                    cfg = d.load_ai_config()
                    self.assertEqual((cfg["base_url"], cfg["model"]), ("http://localhost:6000/v1", "custom"))

    def test_non_streaming_request_and_json_response(self):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": '{"lines":[{"i":0,"vi":"Chào anh Minh"}]}'}}]}
        cfg = {"provider": "chat2api", "key": "local", "model": "gpt-4o",
               "base_url": "http://localhost:5005/private/v1"}
        with patch.object(d.requests, "post", return_value=response) as post:
            result = d._ai_call(cfg, d.AI_SYSTEM, "你好，明哥", 30)
        self.assertEqual(d._parse_json(result)["lines"][0]["vi"], "Chào anh Minh")
        self.assertEqual(post.call_args.args[0], "http://localhost:5005/private/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer local")
        self.assertIs(post.call_args.kwargs["json"]["stream"], False)
        self.assertNotIn("response_format", post.call_args.kwargs["json"])
        response.raise_for_status.assert_called_once()

    def test_openai_retains_json_mode(self):
        with patch.object(d.requests, "post") as post:
            d._ai_call({"provider": "openai", "key": "test", "model": "test"}, "s", "u", 30)
        self.assertEqual(post.call_args.kwargs["json"]["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
