"""Kiểm thử các chức năng: key riêng từng nhà cung cấp AI, sửa từng câu, hồ sơ phim nhiều tập, nghe qua đám mây.

Chạy cùng bộ kiểm thử chung:  python -m unittest discover -s tests
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import douyin_dub as d  # noqa: E402


class ServerBase(unittest.TestCase):
    """Server chạy trong thư mục tạm: không đụng tới job, config hay hàng chờ thật."""

    @classmethod
    def setUpClass(cls):
        import server

        cls.s = server
        cls.tmp = Path(tempfile.mkdtemp())
        cls.saved = (server.JOBS_DIR, server.CONFIG_PATH, server.PROFILE_DIR, server.work_q.put, dict(server.jobs))
        server.JOBS_DIR = cls.tmp / "jobs"
        server.JOBS_DIR.mkdir()
        server.CONFIG_PATH = cls.tmp / "config.json"
        server.PROFILE_DIR = cls.tmp / "profile"
        server.jobs.clear()
        cls.queued = []
        server.work_q.put = cls.queued.append
        cls.c = server.app.test_client()

    @classmethod
    def tearDownClass(cls):
        s = cls.s
        s.JOBS_DIR, s.CONFIG_PATH, s.PROFILE_DIR, s.work_q.put, old = cls.saved
        s.jobs.clear()
        s.jobs.update(old)
        shutil.rmtree(cls.tmp, ignore_errors=True)


class ProviderKeys(ServerBase):
    def cfg(self):
        return json.loads(self.s.CONFIG_PATH.read_text(encoding="utf-8"))

    def test_switching_provider_keeps_each_key(self):
        post = lambda body: self.c.post("/api/config", json=body).get_json()
        post({"provider": "gemini", "key": "GEMINI-KEY-1234567890", "model": ""})
        post({"provider": "deepseek", "key": "sk-DEEPSEEK-1234567890", "model": "deepseek-chat"})
        self.assertEqual(self.cfg()["ai_key"], "sk-DEEPSEEK-1234567890")
        p = post({"provider": "gemini", "key": "", "model": ""})  # đổi lại Gemini, không dán key
        self.assertEqual(self.cfg()["ai_key"], "GEMINI-KEY-1234567890")  # key Gemini cũ vẫn còn
        self.assertTrue(p["has_key"])
        self.assertEqual(p["saved"], ["deepseek", "gemini"])
        p = post({"provider": "deepseek", "key": "", "model": ""})
        self.assertEqual(self.cfg()["ai_model"], "deepseek-chat")  # model riêng của từng nhà cung cấp cũng được nhớ
        body = self.c.get("/api/config").get_data(as_text=True)
        self.assertNotIn("GEMINI-KEY-1234567890", body)  # không bao giờ trả key ra ngoài
        self.assertNotIn("sk-DEEPSEEK-1234567890", body)

    def test_clearing_one_key_keeps_the_others(self):
        post = lambda body: self.c.post("/api/config", json=body).get_json()
        post({"provider": "openai", "key": "sk-OPENAI-1234567890"})
        post({"provider": "claude", "key": "sk-ant-CLAUDE-1234567890"})
        p = post({"clear_key": True})
        self.assertFalse(p["has_key"])
        self.assertIn("openai", p["saved"])
        self.assertNotIn("claude", p["saved"])

    def test_old_single_key_config_is_migrated(self):
        self.s.CONFIG_PATH.write_text(json.dumps({"ai_provider": "gemini", "ai_key": "OLD-GEMINI-KEY-123456"}), encoding="utf-8")
        p = self.c.get("/api/config").get_json()
        self.assertIn("gemini", p["saved"])


class EditLines(ServerBase):
    def make_done_job(self, n=4):
        r = self.c.post("/api/jobs", data={"link": "https://v.douyin.com/edit1/", "tts": "vieneu"})
        jid = r.get_json()["id"]
        job = self.s.jobs[jid]
        job["status"] = "done"
        state = self.s.JOBS_DIR / jid / "_resume"
        state.mkdir()
        video = self.s.JOBS_DIR / jid / "v.goc.mp4"
        video.write_bytes(b"video" * 100)
        segs = [{"start": k * 2.0, "end": k * 2.0 + 1.5, "zh": f"中文{k}", "vi": f"Câu số {k}"} for k in range(n)]
        (state / "segs_vi.json").write_text(json.dumps({"segs": segs, "chars": []}, ensure_ascii=False), encoding="utf-8")
        (state / "segs_dub.json").write_text("{}", encoding="utf-8")
        (state / "source.json").write_text(json.dumps({"source": "x", "video": str(video)}), encoding="utf-8")
        srt = "".join(f"{k + 1}\n00:00:0{k},000 --> 00:00:0{k},900\nCâu số {k}\n\n" for k in range(n))
        (self.s.JOBS_DIR / jid / "v.vi.srt").write_text(srt, encoding="utf-8")
        (self.s.JOBS_DIR / jid / "v.vi.mp4").write_bytes(b"x")
        job["files"] = self.s.find_files(self.s.JOBS_DIR / jid)
        return jid, state

    def test_done_job_with_state_is_editable_and_lines_have_index(self):
        jid, _ = self.make_done_job()
        j = self.c.get(f"/api/jobs/{jid}").get_json()
        self.assertTrue(j["can_edit"])
        self.assertEqual([l["i"] for l in j["lines"]], [0, 1, 2, 3])

    def test_edit_updates_text_and_requeues(self):
        jid, state = self.make_done_job()
        n_before = self.queued.count(jid)
        r = self.c.post(f"/api/jobs/{jid}/edit", json={"lines": {"2": "  Câu   đã sửa  ", "99": "bỏ qua"}})
        self.assertEqual((r.status_code, r.get_json()["status"]), (200, "queued"))
        segs = json.loads((state / "segs_vi.json").read_text(encoding="utf-8"))["segs"]
        self.assertEqual([s["vi"] for s in segs], ["Câu số 0", "Câu số 1", "Câu đã sửa", "Câu số 3"])
        self.assertFalse((state / "segs_dub.json").exists())  # buộc lồng tiếng lại
        self.assertEqual(self.queued.count(jid), n_before + 1)
        self.assertIn("Sửa 1 câu", self.s.jobs[jid]["log"][-1])

    def test_edit_refused_without_state_or_changes(self):
        jid, state = self.make_done_job()
        self.assertEqual(self.c.post(f"/api/jobs/{jid}/edit", json={"lines": {}}).status_code, 400)
        shutil.rmtree(state)
        self.assertEqual(self.c.post(f"/api/jobs/{jid}/edit", json={"lines": {"0": "x"}}).status_code, 409)
        self.assertEqual(self.c.post("/api/jobs/khong-co/edit", json={"lines": {"0": "x"}}).status_code, 404)


class Reformat(EditLines):
    def test_reformat_switches_frame_and_requeues(self):
        jid, state = self.make_done_job()
        self.s.jobs[jid]["options"]["vertical"] = True
        r = self.c.post(f"/api/jobs/{jid}/reformat", json={"vertical": False})
        self.assertEqual((r.status_code, r.get_json()["status"]), (200, "queued"))
        self.assertFalse(self.s.jobs[jid]["options"]["vertical"])
        self.assertIn(jid, self.queued)
        self.assertTrue((state / "segs_dub.json").exists())  # không lồng tiếng lại

    def test_reformat_refused_without_state(self):
        jid, state = self.make_done_job()
        shutil.rmtree(state)
        self.assertEqual(self.c.post(f"/api/jobs/{jid}/reformat", json={"vertical": True}).status_code, 409)


class SeriesProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old = d.SERIES_DIR
        d.SERIES_DIR = self.tmp

    def tearDown(self):
        d.SERIES_DIR = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_series_is_empty_and_saved(self):
        s = d.load_series("Thú triều")
        self.assertEqual((s["characters"], s["address"], s["voices"]), ([], [], {}))
        self.assertEqual(d.series_seed(s), ([], []))
        d.save_series(s)
        self.assertTrue(d.series_file("Thú triều").is_file())

    def test_merge_then_seed_keeps_names_and_address(self):
        s = d.load_series("Phim A")
        chars = [{"id": "c1", "name": "Cô út", "gender": "female", "age": "young"},
                 {"id": "c2", "name": "Hỏa Hỏa", "gender": "male", "age": "child"}]
        d.series_merge(s, chars, [{"from": "c1", "to": "c2", "self": "cô", "call": "cháu"}])
        d.save_series(s)
        again = d.load_series("Phim A")
        seed_chars, seed_addr = d.series_seed(again)
        self.assertEqual([c["name"] for c in seed_chars], ["Cô út", "Hỏa Hỏa"])
        self.assertEqual(seed_addr, [{"from": "c1", "to": "c2", "self": "cô", "call": "cháu"}])
        # tập sau: nhân vật mới được thêm, nhân vật cũ giữ nguyên, cặp xưng hô cũ không bị ghi đè
        d.series_merge(again, seed_chars + [{"id": "c3", "name": "Bố", "gender": "male", "age": "adult"}],
                       seed_addr + [{"from": "c1", "to": "c2", "self": "tôi", "call": "nhóc"}])
        self.assertEqual([c["name"] for c in again["characters"]], ["Cô út", "Hỏa Hỏa", "Bố"])
        self.assertEqual(len(again["address"]), 1)
        self.assertEqual(again["address"][0]["self"], "cô")

    def test_series_file_name_is_safe(self):
        self.assertEqual(d.series_file('Phim: "A/B"?').parent, self.tmp)
        self.assertNotIn("/", d.series_file('Phim: "A/B"?').name.replace(".json", ""))

    def test_analyze_starts_from_series_profile(self):
        sent = {}

        def fake_chat(cfg, system, user, **k):
            sent["user"] = user
            return json.dumps({"characters": [], "address": [], "lines": [{"i": 0, "who": "c1", "to": "c2"}]})

        old = d.ai_chat
        d.ai_chat = fake_chat
        try:
            seed = ([{"id": "c1", "name": "Cô út", "gender": "female", "age": "young"},
                     {"id": "c2", "name": "Hỏa Hỏa", "gender": "male", "age": "child"}], [])
            segs = [{"start": 0.0, "end": 1.0, "zh": "火火"}]
            chars, _ = d.ai_analyze(segs, {"provider": "x", "model": "y", "key": "k"}, "t", seed=seed)
        finally:
            d.ai_chat = old
        self.assertIn("Cô út", sent["user"])  # AI được đưa hồ sơ các tập trước
        self.assertEqual([c["name"] for c in chars], ["Cô út", "Hỏa Hỏa"])
        self.assertEqual(segs[0]["who"], "c1")


class CloudListening(unittest.TestCase):
    def test_no_key_raises_so_tool_falls_back_to_local(self):
        import eleven_tts

        old = eleven_tts.load_config
        eleven_tts.load_config = lambda: None
        try:
            with self.assertRaises(RuntimeError):
                d.eleven_stt("khong-can-file.wav", "zh")
        finally:
            eleven_tts.load_config = old

    def test_words_are_grouped_into_whisper_like_segments(self):
        import eleven_tts

        words = [{"type": "word", "text": "大", "start": 0.0, "end": 0.2}, {"type": "spacing", "text": " ", "start": 0.2, "end": 0.2},
                 {"type": "word", "text": "家", "start": 0.2, "end": 0.4}, {"type": "word", "text": "好", "start": 0.4, "end": 0.6},
                 {"type": "word", "text": "今", "start": 3.0, "end": 3.2}, {"type": "word", "text": "天", "start": 3.2, "end": 3.4}]
        tmp = Path(tempfile.mkdtemp())
        wav = tmp / "a.wav"
        wav.write_bytes(b"RIFF")
        old_cfg, old_post = eleven_tts.load_config, d.requests.post
        eleven_tts.load_config = lambda: {"key": "k" * 20}
        d.requests.post = lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {"words": words})
        try:
            segs = d.eleven_stt(wav, "zh")
        finally:
            eleven_tts.load_config, d.requests.post = old_cfg, old_post
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual([s.text for s in segs], ["大家好", "今天"])  # tách ở chỗ ngừng dài
        self.assertEqual((segs[1].start, segs[1].end), (3.0, 3.4))
        self.assertEqual(len(segs[0].words), 3)  # bỏ khoảng trắng


class AsrOptions(ServerBase):
    def test_cloud_option_maps_to_eleven_with_local_fallback_model(self):
        o = self.c.post("/api/jobs", data={"link": "https://v.douyin.com/asr1/", "model": "cloud"}).get_json()["options"]
        self.assertEqual((o["asr"], o["model"]), ("eleven", "medium"))
        o = self.c.post("/api/jobs", data={"link": "https://v.douyin.com/asr2/", "model": "small"}).get_json()["options"]
        self.assertEqual((o["asr"], o["model"]), ("local", "small"))

    def test_series_option_and_page_controls(self):
        o = self.c.post("/api/jobs", data={"link": "https://v.douyin.com/s1/", "series": "  Thú   triều "}).get_json()["options"]
        self.assertEqual(o["series"], "Thú triều")
        html = self.c.get("/").get_data(as_text=True)
        for needle in ('id="series"', 'data-v="cloud"', 'id="aiSavedMsg"', "/api/jobs/${j.id}/edit"):
            self.assertIn(needle, html)
        self.assertIsInstance(self.c.get("/api/series").get_json(), list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
