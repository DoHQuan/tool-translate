import unittest
from unittest.mock import patch

from video_search import canonical_video, search_url


class SearchTests(unittest.TestCase):
    def test_video_urls(self):
        self.assertEqual(canonical_video("douyin", "https://www.douyin.com/search/x?modal_id=12345"), "https://www.douyin.com/video/12345")
        self.assertEqual(canonical_video("bilibili", "https://www.bilibili.com/video/BV123abc/?from=search"), "https://www.bilibili.com/video/BV123abc/")
        self.assertIsNone(canonical_video("douyin", "https://evil.example/video/123"))
        self.assertIsNone(canonical_video("bilibili", "https://space.bilibili.com/123"))

    def test_query_is_encoded(self):
        self.assertIn("a%26b%2F", search_url("douyin", "a&b/"))
        with self.assertRaises(ValueError):
            search_url("other", "cat")

    def test_search_endpoint(self):
        import server
        client = server.app.test_client()
        for body in ({}, {"source": "other", "keyword": "cat"}, {"keyword": "x" * 151}):
            self.assertEqual(client.post("/api/search", json=body).status_code, 400)
        payload = {"items": [{"url": "https://www.douyin.com/video/123", "title": "猫"}], "query": "猫"}
        with patch("video_search.search_videos", return_value=payload) as search:
            response = client.post("/api/search", json={"source": "douyin", "keyword": "mèo", "translate": True})
            self.assertEqual(response.get_json(), payload)
            search.assert_called_once_with("douyin", "mèo", True)
        with patch("video_search.search_videos", side_effect=RuntimeError("private debug details")):
            response = client.post("/api/search", json={"source": "bilibili", "keyword": "cat"})
            self.assertEqual(response.status_code, 503)
            self.assertIn("search.bilibili.com", response.get_json()["search_url"])
            self.assertNotIn("private debug", response.get_data(as_text=True))
