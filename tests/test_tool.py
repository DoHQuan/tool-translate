"""Bộ kiểm thử tự động cho tool lồng tiếng.

Chạy:  python -m unittest discover -s tests -v      (hoặc bấm đúp chay-kiem-thu.bat)

Không gọi mạng, không cần key, không cần model: chỉ kiểm tra phần logic của tool (chia dòng chữ bìa, chọn giọng,
chọn điểm cắt, dựng khung dọc, API của server...). Các bài cần ffmpeg sẽ tự bỏ qua nếu máy chưa cài ffmpeg.
Mất khoảng nửa phút.
"""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cover  # noqa: E402
import douyin_dub as d  # noqa: E402
import splitter  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg"))


def make_video(path, w=640, h=360, seconds=3):
    """Video thử nhỏ: hình màu chuyển động + tiếng bíp."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=25:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)


class CoverText(unittest.TestCase):
    def test_wrap_balanced_two_lines(self):
        self.assertEqual(cover._wrap("PHÁO MẠNH CỠ NÀO?"), ["PHÁO MẠNH", "CỠ NÀO?"])

    def test_wrap_single_short(self):
        self.assertEqual(cover._wrap("SỰ THẬT"), ["SỰ THẬT"])

    def test_wrap_long_title_keeps_every_word(self):
        text = "SAO LỢN, CHÓ, MÈO KHÔNG CẦN HỌC BƠI?"
        lines = cover._wrap(text)
        self.assertEqual(" ".join(lines), text)  # không rơi mất chữ nào
        self.assertLessEqual(len(lines), 4)

    def test_fit_words_never_cuts_inside_word(self):
        text = "Tại sao món ăn Đông Bắc lại có phần ăn khổng lồ và nhiều dầu mỡ đến vậy"
        out = cover._fit_words(text, 44)
        self.assertLessEqual(len(out), 44)
        self.assertTrue(text.startswith(out))
        self.assertIn(text[len(out):len(out) + 1], ("", " "))  # dừng đúng ở ranh giới từ

    def test_fit_words_keeps_short_text(self):
        self.assertEqual(cover._fit_words("Sao lợn, chó, mèo không cần học bơi", 44), "Sao lợn, chó, mèo không cần học bơi")

    def test_clean_tag(self):
        self.assertEqual(cover._clean_tag("#Kiến Thức Đời Sống"), "kienthucdoisong")


class CoverMeta(unittest.TestCase):
    """Không có AI: tiêu đề lấy từ câu thoại đầu, chữ bìa phải trọn ý (lỗi cũ: chỉ lấy 4 từ đầu -> 'SAO LỢN, CHÓ, MÈO')."""

    def test_fallback_cover_is_whole_title(self):
        m = cover.write_meta([{"vi": "Sao lợn, chó, mèo không cần học bơi"}], "", None)
        self.assertEqual(m["title"], "Sao lợn, chó, mèo không cần học bơi")
        self.assertEqual(m["cover"], "SAO LỢN, CHÓ, MÈO KHÔNG CẦN HỌC BƠI")

    def test_part_label_and_channel_tags(self):
        m = cover.write_meta([{"vi": "Câu mở đầu"}], "", None, part="2/7", extra_tags=["Kiến thức", "khoahoc"])
        self.assertTrue(m["title"].endswith("(Phần 2)"))
        self.assertIn("kienthuc", m["hashtags"])
        self.assertIn("khoahoc", m["hashtags"])
        self.assertEqual(len(m["hashtags"]), len(set(m["hashtags"])))

    def test_ai_cover_used_when_valid(self):
        old = d.ai_chat
        d.ai_chat = lambda *a, **k: json.dumps({"title": "Vì sao pháo mạnh đến vậy?", "cover": "pháo mạnh cỡ nào?",
                                                "badge": "đạn nặng 7 tấn", "hashtags": ["Pháo Binh", "quansu"]})
        try:
            m = cover.write_meta([{"vi": "Pháo rất mạnh"}], "火炮", {"provider": "x", "model": "y", "key": "k"})
        finally:
            d.ai_chat = old
        self.assertEqual(m["cover"], "PHÁO MẠNH CỠ NÀO?")
        self.assertEqual(m["badge"], "ĐẠN NẶNG 7 TẤN")
        self.assertEqual(m["hashtags"][:2], ["phaobinh", "quansu"])


class CoverImage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _frame(self, size):
        from PIL import Image

        p = self.tmp / "f.jpg"
        Image.new("RGB", size, (40, 90, 160)).save(p)
        return p

    def test_cover_is_1080x1920_for_landscape_and_portrait(self):
        from PIL import Image

        for size in ((1920, 1080), (1080, 1920)):
            out = self.tmp / f"c{size[0]}.jpg"
            cover.make_cover(self._frame(size), out, "SAO LỢN, CHÓ, MÈO KHÔNG CẦN HỌC BƠI?", "CAO 60 MÉT", "1/7", channel="kenh")
            self.assertEqual(Image.open(out).size, (1080, 1920))

    def test_title_overlay_is_transparent_layer(self):
        from PIL import Image

        out = self.tmp / "t.png"
        cover.title_overlay(out, "QUÁI THÚ BẰNG THÉP", 560, "1/7")
        im = Image.open(out)
        self.assertEqual((im.size, im.mode), ((1080, 1920), "RGBA"))
        self.assertEqual(im.getpixel((540, 1500))[3], 0)  # vùng dưới tiêu đề phải trong suốt
        self.assertGreater(max(im.crop((0, 0, 1080, 560)).getchannel("A").getextrema()), 0)  # vùng trên có chữ


class Voices(unittest.TestCase):
    def test_each_character_gets_own_voice(self):
        taken = []
        for key in (("male", "young", 0), ("male", "young", 1), ("male", "adult", 0), ("female", "young", 0), ("female", "child", 0)):
            taken.append(d.vieneu_pick(key, taken))
        self.assertEqual(len(set(taken)), 5)

    def test_voice_matches_gender(self):
        self.assertIn(d.vieneu_pick(("male", "adult", 0), []), d.VIENEU_VOICES["male"])
        self.assertIn(d.vieneu_pick(("female", "old", 0), []), d.VIENEU_VOICES["female"])
        self.assertIn(d.vieneu_pick(("male", "child", 0), []), d.VIENEU_VOICES["female"])  # trẻ con dùng giọng nữ trẻ

    def test_wraps_around_when_all_taken(self):
        everyone = d.VIENEU_VOICES["male"] + d.VIENEU_VOICES["female"]
        self.assertIn(d.vieneu_pick(("male", "adult", 0), everyone), d.VIENEU_VOICES["male"])

    def test_age_lists_only_use_real_voices(self):
        real = set(d.VIENEU_VOICES["male"]) | set(d.VIENEU_VOICES["female"])
        for names in d.VIENEU_BY_AGE.values():
            self.assertTrue(set(names) <= real, names)


class TextHelpers(unittest.TestCase):
    def test_syllables_count_numbers_as_longer(self):
        self.assertEqual(d._syllables("Cháu nhớ cô lắm"), 4)
        self.assertGreater(d._syllables("Pháo 155mm"), 2)

    def test_safe_name_strips_hashtags_and_bad_chars(self):
        self.assertEqual(d.safe_name('Tên: video? #tag1 #tag2', "x"), "Tên  video")
        self.assertEqual(d.safe_name("#chỉ có tag", "du-phong"), "du-phong")

    def test_estimates(self):
        segs = [{"vi": "Một câu thử"}, {"vi": ""}, {"vi": "Câu thứ hai dài hơn"}]
        e = d.dub_estimate(segs, "edge")
        self.assertEqual((e["lines"], e["chars"]), (2, len("Một câu thử") + len("Câu thứ hai dài hơn")))
        self.assertIn("Edge", d.estimate_text(e))
        self.assertIn("VieNeu", d.estimate_text(d.dub_estimate(segs, "vieneu")))
        self.assertIn("lượt gọi", d.estimate_text(d.dub_estimate(segs, "gemini")))

    def test_vertical_box_16x9(self):
        vy, vh = d.vertical_box(1920, 1080)
        self.assertEqual((vh % 2, abs(vh - 1080 * 1080 / 1920) <= 2), (0, True))  # đúng tỉ lệ 16:9, chiều cao chẵn
        self.assertTrue(400 <= vy and vy + vh <= 1400)  # còn chỗ cho tiêu đề phía trên và phụ đề phía dưới

    def test_srt_to_ass(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "a.srt").write_text("1\n00:00:01,500 --> 00:00:03,250\nXin chào {bạn}\n\n2\n00:01:02,000 --> 00:01:04,000\nDòng một\nDòng hai\n",
                                       encoding="utf-8")
            d.srt_to_ass(tmp / "a.srt", tmp / "a.ass", 1212)
            ass = (tmp / "a.ass").read_text(encoding="utf-8")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn("PlayResY: 1920", ass)
        self.assertIn("Dialogue: 0,0:00:01.50,0:00:03.25,Default,,0,0,0,,Xin chào (bạn)", ass)
        self.assertIn("0:01:02.00,0:01:04.00,Default,,0,0,0,,Dòng một Dòng hai", ass)
        self.assertIn(",1212,1", ass)  # mép trên của phụ đề

    def test_video_key_follows_content_not_name(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "a.bin").write_bytes(b"x" * 5000)
            (tmp / "b.bin").write_bytes(b"x" * 5000)
            (tmp / "c.bin").write_bytes(b"x" * 4999 + b"y")
            self.assertEqual(d.video_key(tmp / "a.bin"), d.video_key(tmp / "b.bin"))
            self.assertNotEqual(d.video_key(tmp / "a.bin"), d.video_key(tmp / "c.bin"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SplitPlan(unittest.TestCase):
    def test_short_video_is_not_split(self):
        self.assertEqual(splitter.plan_cuts(200.0, [(1, 190)], [0, 50, 100, 150], 5), ([0.0, 200.0], 0))

    def test_cuts_fall_in_silence_and_parts_are_even(self):
        # 20 phút, cứ 10 giây có 8 giây lời rồi 2 giây lặng; keyframe mỗi giây
        total = 1200.0
        speech = [(t, t + 8.0) for t in range(0, 1200, 10)]
        cuts, forced = splitter.plan_cuts(total, speech, [float(k) for k in range(1200)], 5)
        self.assertEqual((cuts[0], cuts[-1], len(cuts) - 1, forced), (0.0, total, 4, 0))
        for c in cuts[1:-1]:
            self.assertTrue(8.0 <= c % 10 <= 10.0, c)  # nằm trong khoảng lặng
        for a, b in zip(cuts, cuts[1:]):
            self.assertTrue(240 <= b - a <= 360, (a, b))

    def test_continuous_speech_still_returns_a_plan(self):
        cuts, forced = splitter.plan_cuts(900.0, [(0.0, 900.0)], [float(k) for k in range(0, 900, 5)], 5)
        self.assertEqual(len(cuts) - 1, 3)
        self.assertEqual(forced, 2)  # báo đúng là 2 điểm cắt phải nằm trong lời nói


@unittest.skipUnless(HAS_FFMPEG, "cần ffmpeg")
class Render(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PIL import Image

        cls.tmp = Path(tempfile.mkdtemp())
        make_video(cls.tmp / "v.mp4")
        (cls.tmp / "sub.srt").write_text("1\n00:00:00,200 --> 00:00:02,500\nCâu phụ đề thử\n", encoding="utf-8")
        Image.new("RGBA", (200, 200), (220, 30, 30, 255)).save(cls.tmp / "logo.png")
        cover.title_overlay(cls.tmp / "title.png", "TIÊU ĐỀ THỬ", d.vertical_box(640, 360)[0])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_plain_export_keeps_size(self):
        out = self.tmp / "plain.mp4"
        d.render(self.tmp / "v.mp4", "sub.srt", None, out, self.tmp, 13, False)
        self.assertEqual(d.video_size(out), (640, 360))

    def test_vertical_export_with_logo(self):
        out = self.tmp / "vert.mp4"
        d.render(self.tmp / "v.mp4", "sub.srt", None, out, self.tmp, 13, False,
                 vertical={"title_png": self.tmp / "title.png"}, logo={"path": self.tmp / "logo.png", "pos": "tr", "size": 12})
        self.assertEqual(d.video_size(out), (1080, 1920))
        self.assertAlmostEqual(d.media_duration(out), 3.0, delta=0.3)
        self.assertTrue((self.tmp / "sub.ass").is_file())  # phụ đề khung dọc dùng file .ass riêng

    def test_burned_subtitles_keep_size(self):
        out = self.tmp / "burn.mp4"
        d.render(self.tmp / "v.mp4", "sub.srt", None, out, self.tmp, 13, True)
        self.assertEqual(d.video_size(out), (640, 360))

    def test_keyframes_found(self):
        self.assertGreaterEqual(len(splitter.keyframes(self.tmp / "v.mp4")), 1)


class Server(unittest.TestCase):
    """API của trang web, chạy trong thư mục tạm: không đụng tới job thật, config thật hay hàng chờ thật."""

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
        server.work_q.put = cls.queued.append  # không chạy job thật
        cls.c = server.app.test_client()

    @classmethod
    def tearDownClass(cls):
        s = cls.s
        s.JOBS_DIR, s.CONFIG_PATH, s.PROFILE_DIR, s.work_q.put, old = cls.saved
        s.jobs.clear()
        s.jobs.update(old)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_job_needs_link_or_file(self):
        self.assertEqual(self.c.post("/api/jobs", data={}).status_code, 400)
        self.assertEqual(self.c.post("/api/jobs", data={"link": "không có link"}).status_code, 400)

    def test_job_options_are_parsed(self):
        r = self.c.post("/api/jobs", data={"link": "xem nè https://v.douyin.com/abc123/ hay lắm", "tts": "vieneu", "vieneu_voice": "Thiện Minh",
                                           "vertical": "1", "confirm": "1", "split": "5", "voice": "linh tinh", "font_size": "999"})
        self.assertEqual(r.status_code, 201)
        j = r.get_json()
        o = j["options"]
        self.assertEqual(j["source"], "https://v.douyin.com/abc123/")
        self.assertEqual((o["tts"], o["vieneu_voice"], o["vertical"], o["confirm"], o["split"]), ("vieneu", "Thiện Minh", True, True, 5.0))
        self.assertEqual((o["voice"], o["font_size"], j["kind"]), ("auto", 30, "split"))  # giá trị sai bị đưa về mặc định / giới hạn
        self.assertIn(j["id"], self.queued)
        self.assertTrue((self.s.JOBS_DIR / j["id"] / "job.json").is_file())

    def test_unknown_tts_falls_back_to_edge(self):
        j = self.c.post("/api/jobs", data={"link": "https://v.douyin.com/zzz/", "tts": "khong-co"}).get_json()
        self.assertEqual((j["options"]["tts"], j["kind"]), ("edge", ""))

    def test_resume_refused_for_running_or_unknown(self):
        j = self.c.post("/api/jobs", data={"link": "https://v.douyin.com/r1/"}).get_json()
        self.assertEqual(self.c.post(f"/api/jobs/{j['id']}/resume", json={}).status_code, 409)  # đang chờ, không phải lỗi
        self.assertEqual(self.c.post("/api/jobs/khong-co/resume", json={}).status_code, 404)

    def test_resume_requeues_failed_job(self):
        j = self.c.post("/api/jobs", data={"link": "https://v.douyin.com/r2/", "confirm": "1"}).get_json()
        job = self.s.jobs[j["id"]]
        job["status"] = "confirm"
        (self.s.JOBS_DIR / j["id"] / "_resume").mkdir()
        self.assertTrue(self.c.get(f"/api/jobs/{j['id']}").get_json()["can_resume"])
        r = self.c.post(f"/api/jobs/{j['id']}/resume", json={})
        self.assertEqual((r.status_code, r.get_json()["status"], job["options"]["confirm"]), (200, "queued", False))
        self.assertEqual(self.queued.count(j["id"]), 2)

    def test_profile_roundtrip_with_logo(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (900, 300), (10, 200, 10)).save(buf, "PNG")
        buf.seek(0)
        r = self.c.post("/api/profile", data={"channel": "@kenh thu", "hashtags": "#kienthuc, khoahoc", "logo_on": "1", "logo_pos": "bl",
                                              "logo_size": "99", "logo": (buf, "logo.png")}, content_type="multipart/form-data")
        p = r.get_json()
        self.assertEqual((p["channel"], p["hashtags"], p["logo_on"], p["logo_pos"], p["logo_size"], p["has_logo"]),
                         ("kenh thu", "kienthuc khoahoc", True, "bl", 30, True))
        self.assertLessEqual(max(Image.open(self.s.PROFILE_DIR / "logo.png").size), 600)  # logo được thu nhỏ
        self.assertEqual(self.c.get("/profile/logo.png").status_code, 200)
        p = self.c.post("/api/profile", data={"remove_logo": "1"}).get_json()
        self.assertFalse(p["has_logo"])
        self.assertEqual(self.c.get("/profile/logo.png").status_code, 404)

    def test_bad_logo_is_rejected(self):
        r = self.c.post("/api/profile", data={"logo": (io.BytesIO(b"day khong phai anh"), "x.png")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)

    def test_config_never_returns_keys(self):
        self.s.CONFIG_PATH.write_text(json.dumps({"ai_provider": "gemini", "ai_key": "SECRET-KEY-1234567890", "eleven_key": "sk_SECRET_ELEVEN_123456"}),
                                      encoding="utf-8")
        body = self.c.get("/api/config").get_data(as_text=True)
        self.assertNotIn("SECRET-KEY-1234567890", body)
        self.assertNotIn("sk_SECRET_ELEVEN_123456", body)
        self.assertTrue(json.loads(body)["has_key"])

    def test_chat2api_config_roundtrip(self):
        r = self.c.post("/api/config", json={"provider": "chat2api", "key": "short",
                                           "base_url": "http://localhost:5005/private", "model": "gpt-4o"})
        self.assertEqual(r.status_code, 200)
        cfg = r.get_json()
        self.assertEqual(cfg["base_url"], "http://localhost:5005/private/v1")
        self.assertTrue(cfg["has_key"])
        self.assertNotIn("short", r.get_data(as_text=True))
        self.assertEqual(self.c.post("/api/config", json={"base_url": "file:///tmp/api"}).status_code, 400)
        self.assertFalse(self.c.post("/api/config", json={"clear_key": True}).get_json()["has_key"])

    def test_index_page_has_main_controls(self):
        html = self.c.get("/").get_data(as_text=True)
        for needle in ('data-name="tts"', 'data-v="vieneu"', 'id="vertical"', 'id="confirm"', 'id="retranslate"', 'data-name="split"', 'id="profCard"'):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
