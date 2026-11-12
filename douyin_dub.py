#!/usr/bin/env python3
"""
douyin_dub.py - Tải video Douyin, dịch phụ đề tiếng Việt và lồng tiếng Việt.

Quy trình:
  1. Tải video (không watermark) từ link Douyin (hoặc link TikTok/YouTube/file local)
  2. Nhận dạng giọng nói tiếng Trung bằng faster-whisper
  3. Dịch sang tiếng Việt (Google Translate miễn phí)
  4. Tạo giọng đọc tiếng Việt bằng Microsoft Edge TTS, căn đúng thời gian từng câu
  5. Trộn giọng Việt với âm thanh gốc (vặn nhỏ) + ghép phụ đề cứng vào video

Ví dụ:
  python douyin_dub.py "https://v.douyin.com/xxxxxx/"
  python douyin_dub.py "<link>" --voice nam --model medium
  python douyin_dub.py "<link>" --no-dub          # chỉ phụ đề
  python douyin_dub.py video.mp4                  # file có sẵn
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import warnings

import requests

warnings.filterwarnings("ignore")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

VOICES = {
    "nu": "vi-VN-HoaiMyNeural",
    "nam": "vi-VN-NamMinhNeural",
}
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def log(msg):
    print(f"[douyin-dub] {msg}", flush=True)


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"Lệnh lỗi: {' '.join(map(str, cmd))}\n{r.stderr[-2000:]}")
    return r.stdout


def media_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)])
    return float(json.loads(out)["format"]["duration"])


# ---------------------------------------------------------------- 1. Download
def extract_url(text):
    m = re.search(r"https?://[^\s，。]+", text)
    return m.group(0) if m else text.strip()


def douyin_video_id(url):
    s = requests.Session()
    s.headers["User-Agent"] = MOBILE_UA
    r = s.get(url, allow_redirects=True, timeout=20)
    for u in [r.url, url]:
        m = re.search(r"/(?:video|note|share/video|share/note)/(\d{10,})", u) or re.search(r"modal_id=(\d{10,})", u)
        if m:
            return m.group(1)
    raise RuntimeError(f"Không tìm thấy ID video trong link: {r.url}")


DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


def _save_stream(urls, out_path, headers):
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    last = None
    for u in urls:
        try:
            with requests.get(u, headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
            if os.path.getsize(out_path) > 50_000:
                return
        except Exception as e:
            last = e
    raise RuntimeError(f"Không tải được file video ({last})")


BROWSER_PROFILE = Path(__file__).resolve().parent / ".browser-profile"
# Trình duyệt ẩn nằm ngay trong thư mục tool: bản cài ở AppData có thể chỉ nhìn thấy được từ bên trong app Claude
# (AppData bị ảo hoá), nên chạy server từ cửa sổ lệnh thường sẽ báo "Executable doesn't exist".
_PW_DIR = Path(__file__).resolve().parent / ".pw-browsers"
if _PW_DIR.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_PW_DIR))
DETAIL_XHR_JS = """async (id) => new Promise((res) => {
  const x = new XMLHttpRequest();
  x.open('GET', '/aweme/v1/web/aweme/detail/?device_platform=webapp&aid=6383&channel=channel_pc_web&aweme_id=' + id);
  x.timeout = 20000;
  x.onload = () => res(x.responseText); x.onerror = () => res(''); x.ontimeout = () => res('');
  x.send();
})"""


def download_douyin_browser(url, out_path):
    """Mở trang video bằng Chromium ẩn (như người dùng thật) để lấy link video gốc không watermark."""
    from playwright.sync_api import sync_playwright

    vid = douyin_video_id(url)
    log(f"ID video: {vid} - mở trình duyệt ẩn để lấy link...")
    found = {}
    with sync_playwright() as p:
        # Dùng hồ sơ trình duyệt cố định (giữ cookie giữa các lần, như người dùng thật quay lại trang):
        # phiên mới tinh bị Douyin chặn ngẫu nhiên ~50%, phiên có hồ sơ thì gần như luôn qua.
        opts = dict(user_agent=DESKTOP_UA, locale="zh-CN", viewport={"width": 1280, "height": 800})
        browser = None
        try:
            ctx = p.chromium.launch_persistent_context(str(BROWSER_PROFILE), headless=True, **opts)
        except Exception:  # hồ sơ đang bị tiến trình khác dùng -> phiên tạm
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(**opts)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def keep(j):  # chỉ nhận dữ liệu có video
            if "detail" not in found and isinstance(j, dict) and ((j.get("aweme_detail") or {}).get("video")):
                found["detail"] = j

        def on_resp(r):
            if "aweme/detail" in r.url and "detail" not in found:
                try:
                    keep(r.json())
                except Exception:
                    pass

        page.on("response", on_resp)
        page.goto(f"https://www.douyin.com/video/{vid}", wait_until="domcontentloaded", timeout=60000)
        dom_srcs = []
        for _ in range(30):
            if "detail" in found:
                break
            # Douyin có lúc nhúng sẵn dữ liệu trong trang, không gọi API -> đọc link từ thẻ <video>
            try:
                dom_srcs = [s for s in page.eval_on_selector_all(
                    "video, video source", "els => els.map(e => e.currentSrc || e.src).filter(Boolean)")
                    if s.startswith("http") and "douyinstatic.com" not in s]
            except Exception:  # trang đang tự tải lại (bước xác minh chống bot)
                dom_srcs = []
            if dom_srcs:
                page.wait_for_timeout(1500)  # cho API kịp trả về (có chất lượng tốt nhất)
                break
            page.wait_for_timeout(500)
        # Trang không tự gọi API (player mới phát dạng blob:) -> tự gọi API từ BÊN TRONG trang,
        # để bộ bảo mật của chính Douyin ký request (gọi từ ngoài sẽ bị chặn 403).
        for attempt in range(3):
            if "detail" in found:
                break
            try:
                text = page.evaluate(DETAIL_XHR_JS, vid)
                keep(json.loads(text))
            except Exception:
                pass
            if "detail" not in found:
                page.wait_for_timeout(2500 * (attempt + 1))
        try:
            page_title = re.sub(r"\s*-\s*抖音\s*$", "", page.title() or "")
        except Exception:
            page_title = ""
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        ctx.close()
        if browser:
            browser.close()
    aweme = (found.get("detail") or {}).get("aweme_detail")
    if aweme:
        video = aweme.get("video") or {}
        urls = list((video.get("play_addr") or {}).get("url_list") or [])
        # ưu tiên bản chất lượng cao nhất nếu có
        for br in sorted(video.get("bit_rate") or [], key=lambda x: x.get("bit_rate", 0), reverse=True)[:1]:
            urls = list((br.get("play_addr") or {}).get("url_list") or []) + urls
        title = aweme.get("desc", "")
    else:
        urls = list(dict.fromkeys(dom_srcs))
        title = page_title
    if not urls:
        raise RuntimeError("Không lấy được link video (có thể video bị xoá / riêng tư / là bài ảnh)")
    log(f"Tiêu đề: {title[:80]}")
    headers = {"User-Agent": DESKTOP_UA, "Referer": "https://www.douyin.com/",
               "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
    _save_stream(urls, out_path, headers)
    return title


def download_douyin_direct(url, out_path):
    """Lấy link phát không watermark từ trang chia sẻ iesdouyin (không cần cookie)."""
    vid = douyin_video_id(url)
    log(f"ID video: {vid}")
    headers = {"User-Agent": MOBILE_UA, "Referer": "https://www.douyin.com/"}
    html = requests.get(f"https://www.iesdouyin.com/share/video/{vid}/", headers=headers, timeout=20).text
    m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})</script>", html, re.S)
    if not m:
        raise RuntimeError("Không đọc được dữ liệu trang chia sẻ Douyin")
    data = json.loads(m.group(1))
    item = None
    for v in data.get("loaderData", {}).values():
        if isinstance(v, dict) and v.get("videoInfoRes"):
            lst = v["videoInfoRes"].get("item_list") or []
            if lst:
                item = lst[0]
                break
    if not item:
        raise RuntimeError("Video không tồn tại / bị ẩn / là bài ảnh")
    uri = item["video"]["play_addr"]["uri"]
    title = item.get("desc", "")
    play_url = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0"
    log(f"Tiêu đề: {title[:80]}")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with requests.get(play_url, headers=headers, stream=True, timeout=60, allow_redirects=True) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    if os.path.getsize(out_path) < 50_000:
        raise RuntimeError("File tải về quá nhỏ, có thể bị chặn")
    return title


def download_ytdlp(url, out_path, cookies_browser=None):
    """Tải bằng yt-dlp. Bilibili / YouTube để hình và tiếng ở hai luồng riêng: phải tải CẢ HAI rồi ghép
    (định dạng cũ "mp4/best" chọn trúng luồng chỉ có hình -> video tải về không có tiếng)."""
    cmd = [sys.executable, "-m", "yt_dlp", "-f", "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b", "--merge-output-format", "mp4",
           "-o", str(out_path), "--no-playlist", "--force-overwrites", "--print", "after_move:title", "--no-simulate",
           "--encoding", "utf-8"]
    if cookies_browser:
        cmd += ["--cookies-from-browser", cookies_browser]
    r = subprocess.run(cmd + [url], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp lỗi: {r.stderr[-800:]}")
    out = r.stdout
    if not has_audio(out_path):
        raise RuntimeError("Video tải về không có tiếng (nguồn chỉ cho tải hình). Hãy thử link khác.")
    lines = [x.strip() for x in out.splitlines() if x.strip()]
    if lines:
        log(f"Tiêu đề: {lines[-1]}")
    return lines[-1] if lines else ""


def download(src, workdir, cookies_browser=None):
    if os.path.isfile(src):
        log("Dùng file video có sẵn")
        return Path(src).resolve(), Path(src).stem
    url = extract_url(src)
    out = workdir / "original.mp4"
    log(f"Đang tải: {url}")
    title = ""
    if "douyin.com" in url:
        methods = [("trình duyệt ẩn", download_douyin_browser), ("trình duyệt ẩn (lần 2)", download_douyin_browser),
                   ("trang chia sẻ", download_douyin_direct),
                   ("yt-dlp", lambda u, o: download_ytdlp(u, o, cookies_browser))]
        errors = []
        for i, (name, fn) in enumerate(methods):
            try:
                title = fn(url, out) or ""
                break
            except Exception as e:
                # bỏ các dòng chỉ là khung trang trí (Playwright in lỗi trong khung ╔═╗), lấy dòng có chữ
                lines = [x.strip(" ║╔╗╚╝═") for x in str(e).splitlines()]
                lines = [x for x in lines if x]
                errors.append(" | ".join(lines[:3])[:300] if lines else type(e).__name__)
                log(f"Cách '{name}' lỗi ({errors[-1]}); thử cách khác..." if i < len(methods) - 1 else f"Cách '{name}' lỗi.")
                if name.startswith("trình duyệt"):
                    import time

                    time.sleep(5)  # Douyin hay chặn tạm khi mở liên tục
        else:
            raise RuntimeError(f"Không tải được video Douyin. Kiểm tra link còn xem được không rồi thử lại. ({errors[0]})")
    else:
        title = download_ytdlp(url, out, cookies_browser) or ""
    log(f"Đã tải xong ({os.path.getsize(out) / 1e6:.1f} MB)")
    return out, title


# ------------------------------------------------------------ 2. Transcribe
def eleven_stt(wav, lang):
    """Nghe bằng ElevenLabs Scribe (đám mây): nhanh hơn nhiều so với chạy trên máy, tính credit ElevenLabs.
    Trả về các đoạn có cùng dạng với Whisper (start, end, text, words[start, end, word]) để xử lý tiếp như cũ."""
    from types import SimpleNamespace

    import eleven_tts

    kc = eleven_tts.load_config()
    if not kc:
        raise RuntimeError("chưa có API key ElevenLabs")
    with open(wav, "rb") as f:
        r = requests.post("https://api.elevenlabs.io/v1/speech-to-text", headers={"xi-api-key": kc["key"]},
                          data={"model_id": "scribe_v1", "language_code": {"zh": "zho"}.get(lang, lang),
                                "timestamps_granularity": "word", "tag_audio_events": "false"},
                          files={"file": ("audio.wav", f, "audio/wav")}, timeout=600)
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs từ chối ({r.status_code}): {eleven_tts._err(r)[:200]}")
    words = [SimpleNamespace(word=w.get("text", ""), start=float(w.get("start", 0)), end=float(w.get("end", 0)))
             for w in r.json().get("words", []) if w.get("type") == "word" and str(w.get("text", "")).strip()]
    segs, cur = [], []
    for w in words:  # chia thành đoạn ở chỗ ngừng dài; tách câu chi tiết làm ở split_by_words như với Whisper
        if cur and w.start - cur[-1].end > 1.2:
            segs.append(cur)
            cur = []
        cur.append(w)
    if cur:
        segs.append(cur)
    return [SimpleNamespace(start=g[0].start, end=g[-1].end, text="".join(x.word for x in g), words=g) for g in segs]


def transcribe(video, workdir, model_name, lang, hint="", engine="local", beam=2):
    from faster_whisper import WhisperModel

    wav = workdir / "audio16k.wav"  # bản gốc (dùng để đo cao độ giọng)
    run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    # Whisper hay bỏ sót lời nói nằm ngay giây 0 -> đệm 1 giây im lặng ở đầu, rồi trừ lại ở mọi mốc thời gian
    PAD = 1.0
    wav_pad = workdir / "audio16k_pad.wav"
    run(["ffmpeg", "-y", "-i", str(wav), "-af", f"adelay={int(PAD * 1000)}", "-ac", "1", "-ar", "16000", str(wav_pad)])
    segments = None
    if engine == "eleven":
        log("Nhận dạng giọng nói qua đám mây (ElevenLabs)...")
        try:
            segments = eleven_stt(wav_pad, lang)
        except Exception as e:
            log(f"Không nghe được qua đám mây ({str(e)[:160]}) -> nghe trên máy")
    if segments is None:
        log(f"Nhận dạng giọng nói (model {model_name}, lần đầu sẽ tải model)...")
        model = WhisperModel(model_name, device="auto", compute_type="int8")
        segments = whisper_segments(model, wav_pad, lang, beam)
    segs = _collect_segments(segments, hint)
    return _finish_transcribe(segs, wav)


def whisper_segments(model, wav_pad, lang, beam=5):
    segments, info = model.transcribe(
        str(wav_pad), language=lang, vad_filter=True, beam_size=beam,
        # VAD nhạy hơn mặc định: giọng trẻ con / nói nhỏ trên nền nhạc hay bị cắt mất
        # (0.1 bắt được cả tiếng gọi ngắn như "小姑"; câu ảo giác sinh ra từ nhạc nền được lọc bằng HALLUCINATIONS)
        vad_parameters={"min_silence_duration_ms": 400, "threshold": 0.1, "speech_pad_ms": 500},
        word_timestamps=True,  # lấy thời gian từng chữ -> không bị lệch qua khoảng lặng
        condition_on_previous_text=False,  # giảm lỗi lặp câu
        repetition_penalty=1.1,
        # KHÔNG đưa tiêu đề video vào gợi ý: Whisper hay chép lại nó vào đầu mọi câu (lỗi "lọt prompt"),
        # làm hỏng cả đoạn. Chữ nghe nhầm đã có bước dịch AI sửa theo ngữ cảnh.
        initial_prompt="以下是普通话的句子，使用简体中文。" if lang == "zh" else None,
    )
    return segments


def _collect_segments(segments, hint=""):
    PAD = 1.0
    title_norm = _norm(re.sub(r"#\S+", "", hint or ""))
    segs = []
    for s in segments:
        for line in split_by_words(s):
            line["start"] = round(max(0.0, line["start"] - PAD), 2)
            line["end"] = round(max(line["start"] + 0.2, line["end"] - PAD), 2)
            # phòng hờ: bỏ phần tiêu đề / câu gợi ý nếu model vẫn chép vào lời thoại
            line["zh"] = strip_leak(line["zh"], title_norm)
            if not _norm(line["zh"]) or is_hallucination(line["zh"]):
                continue
            if drop_repeats(segs + [line]) != segs + [line]:
                continue
            segs.append(line)
            print(f"   {line['start']:6.1f}s  {line['zh']}", flush=True)
    return segs


def _finish_transcribe(segs, wav):
    n0 = len(segs)
    # cao độ giọng từng câu: để không gộp nhầm lời của 2 người khác nhau thành 1 câu
    import wave

    import numpy as np

    with wave.open(str(wav), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    for s in segs:
        s["f0"] = estimate_f0(pcm, s["start"], s["end"])
    segs = merge_short(segs)
    for s in segs:  # đo lại sau khi gộp: dùng để AI biết câu nào giọng nam / nữ / trẻ con
        s["f0"] = estimate_f0(pcm, s["start"], s["end"])
    log(f"Được {len(segs)} câu" + (f" (gộp {n0 - len(segs)} câu vụn)" if n0 > len(segs) else ""))
    return segs


PUNCT = "，。！？、；：,.!?;:…﹐﹑﹒﹔﹕﹖﹗"
COMMA_RE = re.compile(r"[，、；：,;:﹐﹑﹔﹕]$")
STOP_RE = re.compile(r"[。！？.!?…﹒﹖﹗]$")


def _word_len(w):
    return len(re.sub(rf"[\s{PUNCT}]", "", w.word)) or 1


def _norm(t):
    return re.sub(rf"[\s{PUNCT}]", "", t)


def split_by_words(seg, gap=0.6, soft_chars=10, max_chars=22):
    """Tách một đoạn Whisper thành các câu theo thời gian từng chữ.

    Whisper hay gộp khoảng lặng dài (cảnh không thoại) vào câu bên cạnh, làm phụ đề/giọng lồng
    xuất hiện sớm hoặc kéo dài hàng chục giây. Ở đây cắt câu tại chỗ ngắt > gap giây và
    chặn những chữ bị kéo dài bất thường (chữ đầu sau khoảng lặng, chữ cuối trước khoảng lặng).
    """
    words = [w for w in (seg.words or []) if w.word.strip()]
    if not words:
        t = seg.text.strip()
        return [{"start": seg.start, "end": min(seg.end, seg.start + 0.35 * len(t) + 1), "zh": t}] if t else []

    def wstart(w):  # chữ đầu câu bị kéo dài -> dời điểm bắt đầu về gần lúc thật sự nói
        return max(w.start, w.end - (0.35 * _word_len(w) + 0.25))

    def wend(w):
        return min(w.end, w.start + 0.35 * _word_len(w) + 0.35)

    groups, cur = [], [words[0]]
    for k, w in enumerate(words[1:], 1):
        n = len(_norm("".join(x.word for x in cur)))
        last = cur[-1].word.strip()
        pause = wstart(w) - wend(cur[-1])
        # chữ ngắn đứng ngay trước một khoảng lặng dài thường là chữ đầu của câu sau bị lệch giờ
        orphan = (k + 1 < len(words) and _word_len(w) <= 2 and not re.search(rf"[{PUNCT}]$", w.word.strip())
                  and wstart(words[k + 1]) - wend(w) > 3 * gap)
        if (pause > gap or orphan or (STOP_RE.search(last) and n >= 4) or (COMMA_RE.search(last) and n >= soft_chars)
                or n >= max_chars):
            groups.append(cur)
            cur = [w]
        else:
            cur.append(w)
    groups.append(cur)
    out = []
    for g in groups:
        text = "".join(w.word for w in g).strip()
        if _norm(text):
            out.append({"start": wstart(g[0]), "end": wend(g[-1]), "zh": text})
    # mẩu 1-2 chữ chưa hết câu (thường do lệch thời gian từng chữ) -> gộp vào câu sau
    i = 0
    while i < len(out) - 1:
        if len(_norm(out[i]["zh"])) <= 2 and not STOP_RE.search(out[i]["zh"]):
            nxt = out.pop(i + 1)
            frag = out[i]["zh"].rstrip(PUNCT)
            out[i] = {"start": max(out[i]["start"], nxt["start"] - 0.35 * len(_norm(frag)) - 0.1),
                      "end": nxt["end"], "zh": frag + nxt["zh"]}
        else:
            i += 1
    for o in out:
        o["start"], o["end"], o["zh"] = round(o["start"], 2), round(o["end"], 2), o["zh"].rstrip("，,﹐、﹑ ")
    return out


# Câu Whisper hay "nghe ra" từ nhạc nền / khoảng lặng (do học từ phụ đề video trên mạng)
HALLUCINATIONS = re.compile(
    r"(谢谢(大家|您|你)?的?(观看|收看|支持)|感谢(您|你|大家)?的?(观看|收看|支持)|请?(不吝)?点赞|订阅|转发|打赏|"
    r"字幕(由|制作|提供)|明镜与点点栏目|优优独播剧场|YoYo Television|本节目|下期再见|我们下期见)")


# Câu 1 chữ hợp lệ: tiếng cảm thán + câu đáp ngắn hay gặp; chữ đơn khác (VD "家") thường là mảnh vụn nhận dạng sai
SINGLE_OK = set("啊哦噢嗯唉哎呀嘿喂哇咦欸诶哈嗨呃好走对是不行滚等来去停快谁嘘哼妈爸哥姐")


def is_hallucination(text):
    n = _norm(text)
    if len(n) == 1 and n not in SINGLE_OK:
        return True
    return bool(HALLUCINATIONS.search(n)) and len(n) <= 24


def strip_leak(text, title_norm, prompt_norm="以下是普通话的句子使用简体中文"):
    """Bỏ phần đầu câu nếu đó là tiêu đề video / câu gợi ý bị Whisper chép lại vào lời thoại."""
    norm = _norm(text)
    for leak in (title_norm, prompt_norm):
        if len(leak) < 4:
            continue
        # độ dài phần đầu câu trùng với phần đầu của leak (ít nhất 4 chữ mới tính là lọt)
        n = 0
        while n < min(len(norm), len(leak)) and norm[n] == leak[n]:
            n += 1
        if n >= 4:
            seen = 0  # cắt trên chuỗi gốc: bỏ qua đúng n chữ (không tính dấu câu, khoảng trắng, 《》)
            for i, ch in enumerate(text):
                if _norm(ch):
                    seen += 1
                if seen == n:
                    text = text[i + 1:].lstrip(" " + PUNCT + "《》<>")
                    break
            norm = _norm(text)
    return text


def merge_short(segs, max_gap=0.5, max_chars=18, max_dur=5.5, short_chars=5, short_dur=1.5):
    """Gộp câu vụn (VD '其他人' + '抓紧合眼') vào câu bên cạnh để phụ đề/giọng lồng không bị lắt nhắt.

    Chỉ gộp câu NGẮN, vào câu kề bên sát hơn (bằng nhau thì ưu tiên câu sau, vì mẩu ngắn
    thường là phần đầu của câu sau), không gộp qua dấu chấm câu.
    """
    out = [dict(s) for s in segs]

    def short(s):
        return len(_norm(s["zh"])) <= short_chars or s["end"] - s["start"] < short_dur

    def same_voice(a, b):  # cao độ chênh nhiều -> có lẽ đã đổi người nói
        fa, fb = a.get("f0"), b.get("f0")
        return not (fa and fb and max(fa, fb) / min(fa, fb) > 1.3)

    def can(a, b):  # gộp a (trước) + b (sau)
        return (b["start"] - a["end"] <= max_gap and not STOP_RE.search(a["zh"]) and same_voice(a, b)
                and len(_norm(a["zh"])) + len(_norm(b["zh"])) <= max_chars and b["end"] - a["start"] <= max_dur)

    changed = True
    while changed:
        changed = False
        for i, s in enumerate(out):
            if not short(s):
                continue
            opts = []
            if i + 1 < len(out) and can(s, out[i + 1]):
                opts.append((out[i + 1]["start"] - s["end"], 0, i))  # 0 = ưu tiên câu sau
            if i > 0 and can(out[i - 1], s):
                opts.append((s["start"] - out[i - 1]["end"], 1, i - 1))
            if not opts:
                continue
            j = min(opts)[2]
            a, b = out[j], out[j + 1]
            sep = "" if re.search(rf"[{PUNCT}]$", a["zh"]) else "，"
            out[j:j + 2] = [{**a, "end": b["end"], "zh": a["zh"] + sep + b["zh"]}]
            changed = True
            break
    return out


def drop_repeats(segs, window=6):
    """Whisper đôi khi 'ảo giác' lặp lại các câu trước đó -> bỏ câu trùng với vài câu gần nhất."""
    out = []
    for s in segs:
        k = _norm(s["zh"])
        recent = [_norm(x["zh"]) for x in out[-window:]]
        if len(k) >= 4 and any(k == r or (len(k) >= 6 and k in r) for r in recent):
            continue
        out.append(s)
    return out


# --------------------------------------------------------------- 3. Translate
class GoogleGtx:
    """Google Translate qua endpoint gtx (miễn phí, không cần key), có thử lại."""

    def __init__(self, source, target):
        self.source, self.target = source, target

    def _gtx(self, text):
        r = requests.post("https://translate.googleapis.com/translate_a/single", timeout=30,
                          params={"client": "gtx", "sl": self.source, "tl": self.target, "dt": "t"}, data={"q": text})
        r.raise_for_status()
        return "".join(part[0] for part in r.json()[0] if part[0])

    def _clients5(self, text):  # cổng dự phòng của Google
        r = requests.post("https://clients5.google.com/translate_a/t", timeout=30,
                          params={"client": "dict-chrome-ex", "sl": self.source, "tl": self.target}, data={"q": text})
        r.raise_for_status()
        j = r.json()
        first = j[0] if isinstance(j, list) and j else j
        return first[0] if isinstance(first, list) else str(first)

    def translate(self, text):
        """Google hay chặn tạm thời khi gọi nhiều -> luân phiên 2 cổng, chờ lâu dần (tối đa ~2 phút)."""
        import time

        last = None
        for attempt in range(10):
            try:
                out = (self._clients5 if attempt % 2 == 0 else self._gtx)(text)
                if out:
                    return out
            except Exception as e:
                last = e
            time.sleep(min(20, 2 * (attempt + 1)))
        raise RuntimeError(f"Google Dịch đang chặn tạm thời, hãy thử lại sau ít phút ({last})")


# ---------------- Dịch bằng AI (Gemini / Claude / OpenAI / DeepSeek)
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
AI_DEFAULT_MODELS = {
    "gemini": "gemini-3.5-flash",
    "claude": "claude-haiku-5-5",
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
    "chat2api": "gpt-4o",
}
# Giọng có sẵn của VieNeu-TTS (đọc ngay trên máy, miễn phí): theo giới tính, xếp sẵn thứ tự ưu tiên
VIENEU_VOICES = {
    "male": ['Hải Đăng', 'Thiện Minh', 'Thanh Bình', 'Minh Đức', 'Phạm Tuyên', 'Xuân Vĩnh', 'Quốc Tuấn', 'Thiền Tâm Đức', 'Adam bựa', 'Quang Sơn', 'Thái Sơn', 'Minh Triết', 'Đức Trí', 'Adam'],
    "female": ['Trúc Ly', 'Ngọc Huyền', 'Mai Anh', 'Ngọc Linh', 'Đoan Trang', 'Quỳnh Anh', 'Ngọc Trân', 'Thùy Dung', 'Thục Đoan', 'Mỹ Duyên', 'Kim Thanh'],
}
# Ưu tiên theo tuổi (xếp theo nhãn phong cách của từng giọng; VieNeu không có giọng trẻ con -> dùng giọng nữ trẻ)
VIENEU_BY_AGE = {
    ("male", "child"): ["Quỳnh Anh", "Đoan Trang", "Trúc Ly"],
    ("female", "child"): ["Quỳnh Anh", "Đoan Trang", "Trúc Ly"],
    ("male", "young"): ["Hải Đăng", "Phạm Tuyên", "Xuân Vĩnh", "Quốc Tuấn", "Adam", "Đức Trí"],
    ("male", "adult"): ["Thiện Minh", "Thanh Bình", "Minh Đức", "Thái Sơn", "Minh Triết", "Quang Sơn"],
    ("male", "old"): ["Thiền Tâm Đức", "Thanh Bình", "Thái Sơn"],
    ("female", "young"): ["Trúc Ly", "Đoan Trang", "Quỳnh Anh", "Mỹ Duyên", "Ngọc Trân"],
    ("female", "adult"): ["Ngọc Huyền", "Mai Anh", "Ngọc Linh", "Thùy Dung", "Thục Đoan", "Kim Thanh"],
    ("female", "old"): ["Ngọc Linh", "Thục Đoan", "Kim Thanh"],
}


def vieneu_pick(vkey, taken):
    """Chọn giọng VieNeu cho một nhân vật (giới tính, tuổi, thứ tự): hợp tuổi trước, rồi cùng giới tính, và chưa
    nhân vật nào dùng. Hết giọng trống thì xoay vòng trong nhóm cùng giới tính."""
    gender = "male" if vkey[0] == "male" else "female"
    age = vkey[1] if len(vkey) > 1 else "adult"
    pool = list(VIENEU_BY_AGE.get((gender, age), [])) + VIENEU_VOICES["female" if age == "child" else gender]
    pool = list(dict.fromkeys(pool))
    for name in pool:
        if name not in taken:
            return name
    return pool[len(taken) % len(pool)]


VIENEU_PY = Path(__file__).resolve().parent / ".vieneu-venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def vieneu_speak_all(items):
    """items: list (text, out_wav, tên giọng). Gọi vieneu_worker.py trong môi trường riêng. Trả về tập chỉ số đã đọc được."""
    if not items:
        return set()
    jobs = Path(items[0][1]).parent / "vieneu_jobs.json"
    jobs.write_text(json.dumps([{"text": t, "out": str(o), "voice": v} for t, o, v in items], ensure_ascii=False),
                    encoding="utf-8")
    for _, o, _ in items:
        Path(o).unlink(missing_ok=True)
    p = subprocess.Popen([str(VIENEU_PY), "-u", str(Path(__file__).resolve().parent / "vieneu_worker.py"), str(jobs)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                         env=dict(os.environ, PYTHONIOENCODING="utf-8", HF_HUB_DISABLE_SYMLINKS_WARNING="1"))
    tail = []
    for line in p.stdout:
        line = line.rstrip()
        if re.match(r"\s*\d+/\d+\s*$", line) or line.startswith("[vieneu]"):
            print(line, flush=True)
        elif line.strip():
            tail = (tail + [line])[-3:]
    if p.wait() != 0:
        log(f"VieNeu lỗi: {' | '.join(tail)[:200]}")
    return {n for n, (_, o, _) in enumerate(items) if Path(o).is_file() and Path(o).stat().st_size > 2000}


EL_SYLLABLES_PER_SEC = 3.8  # ElevenLabs đọc tiếng Việt chậm hơn Edge
VI_SYLLABLES_PER_SEC = 4.6  # tốc độ đọc thoải mái của giọng Edge TTS tiếng Việt (~x1.15)


def load_ai_config():
    cfg = {}
    if CONFIG_PATH.is_file():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    provider = os.environ.get("DUB_AI_PROVIDER") or cfg.get("ai_provider") or ""
    key = os.environ.get("DUB_AI_KEY") or cfg.get("ai_key") or ""
    model = os.environ.get("DUB_AI_MODEL") or cfg.get("ai_model") or AI_DEFAULT_MODELS.get(provider, "")
    if provider == "chat2api" and key.strip():
        base = os.environ.get("DUB_AI_BASE_URL") or cfg.get("ai_base_url") or "http://127.0.0.1:5005/v1"
        return {"provider": provider, "key": key, "model": model, "base_url": normalize_chat2api_url(base)}
    if provider in AI_DEFAULT_MODELS and provider != "chat2api" and len(key) > 10:
        return {"provider": provider, "key": key, "model": model}
    return None


def normalize_chat2api_url(value):
    from urllib.parse import urlsplit

    base = str(value).strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Địa chỉ chat2api phải là URL http/https, không chứa token, query hoặc mật khẩu.")
    if base.endswith("/chat/completions"):
        base = base[:-len("/chat/completions")]
    if not base.endswith("/v1"):
        base += "/v1"
    return base


GEMINI_FALLBACKS = ["gemini-3.5-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]


def ai_chat(cfg, system, user, timeout=90, strong_only=False):
    """Gọi LLM, trả về chuỗi JSON. Với Gemini: model đang quá tải / treo / không có -> tự chuyển model khác
    và nhớ model chạy được cho các lần gọi sau."""
    models = [cfg["model"]]
    if cfg["provider"] == "gemini":  # model đang dùng trước, quá tải thì xoay sang model khác (mỗi lần gọi)
        models += [m for m in GEMINI_FALLBACKS if m != cfg["model"]]
    if strong_only:  # việc cần suy luận kỹ (sửa lời nghe nhầm): model "lite" làm sai nhiều hơn đúng -> thà bỏ qua
        models = [m for m in models if "lite" not in m]
        if not models:
            raise RuntimeError("không có model đủ mạnh")
        cfg = dict(cfg)  # không đổi model đang dùng của các bước khác
    import time

    last = None
    for wait in ((0, 10) if strong_only else (0, 10, 30)):  # mọi model đều quá tải -> chờ rồi thử lại cả vòng
        if wait:
            log(f"AI đang quá tải, chờ {wait} giây rồi thử lại...")
            time.sleep(wait)
        for m in models:
            try:
                out = _ai_call({**cfg, "model": m}, system, user, timeout)
                if m != cfg["model"]:
                    log(f"Model {cfg['model']} không phản hồi, chuyển sang {m}")
                    cfg["model"] = m
                return out
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                if code in (400, 401, 403):  # key sai / hết quyền: đổi model cũng vô ích
                    raise
                last = e
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
    raise last


def _ai_call(cfg, system, user, timeout):
    p, key, model = cfg["provider"], cfg["key"], cfg["model"]
    if p == "gemini":
        headers = {"Content-Type": "application/json"}
        if key.startswith("ya29."):
            headers["Authorization"] = f"Bearer {key}"
        else:
            headers["x-goog-api-key"] = key
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers=headers, timeout=timeout,
            json={"systemInstruction": {"parts": [{"text": system}]},
                  "contents": [{"role": "user", "parts": [{"text": user}]}],
                  "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"}})
        r.raise_for_status()
        return "".join(part.get("text", "") for part in r.json()["candidates"][0]["content"]["parts"])
    if p == "claude":
        r = requests.post(
            "https://api.anthropic.com/v1/messages", timeout=timeout,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 8000, "temperature": 0.3, "system": system,
                  "messages": [{"role": "user", "content": user}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
    url = "https://api.deepseek.com/chat/completions" if p == "deepseek" else "https://api.openai.com/v1/chat/completions"
    payload = {"model": model, "temperature": 0.3,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if p == "chat2api":
        url = normalize_chat2api_url(cfg.get("base_url") or "http://127.0.0.1:5005/v1") + "/chat/completions"
        payload["stream"] = False
    else:
        payload["response_format"] = {"type": "json_object"}
    r = requests.post(url, timeout=timeout, headers={"Authorization": f"Bearer {key}"},
                      json=payload)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _parse_json(text):
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


AI_SYSTEM = """Bạn là biên dịch viên lồng tiếng phim Trung -> Việt chuyên nghiệp.
Đầu vào là lời thoại tiếng Trung do máy nhận dạng giọng nói (ASR) chép lại, nên CÓ THỂ SAI chữ đồng âm
(ví dụ 大皇 thật ra là 大黄 - tên con chó; 合演 thật ra là 合眼; 线城 thật ra là 县城). Hãy dựa vào ngữ cảnh để hiểu đúng ý rồi mới dịch.
Yêu cầu:
- Dịch thành lời thoại tiếng Việt tự nhiên, khẩu ngữ, đúng giọng nhân vật, như phim lồng tiếng Việt.
- Xưng hô nhất quán và hợp vai (anh/em/tôi/cậu/mày/tao...), không dùng "bạn" máy móc.
- Tên người đọc theo Hán-Việt ĐÚNG TỪNG CHỮ (明哥 -> anh Minh, 阿南 -> A Nam, 王婶 -> thím Vương, 苏 -> Tô, 顾 -> Cố).
  Không để phiên âm pinyin. Câu có gọi tên ai thì giữ tên đó.
- Giờ giấc: X点一刻 = X giờ mười lăm, X点半 = X giờ rưỡi, X点三刻 = X giờ bốn lăm, 差一刻X点 = X giờ kém mười lăm.
- MỖI CÂU PHẢI ĐỌC KỊP trong thời gian cho phép: không vượt quá số âm tiết "max" của câu đó. Khi phải rút gọn thì bỏ
  từ đệm, từ lặp, lời rào đón trước; KHÔNG bỏ tên riêng, địa danh, con số, đơn vị, tên món ăn / đồ vật, và không biến
  câu hỏi thành câu khẳng định.
- Thành ngữ, tục ngữ, câu đùa, chơi chữ, tiếng lóng mạng, từ game (掉血 = tụt máu, debuff, 副本 = phó bản / ải,
  开荒 = khai hoang server mới...): dịch THOÁT Ý sang cách nói tiếng Việt tương đương để người Việt hiểu ngay và vẫn
  thấy buồn cười; không dịch từng chữ. VD 放马过来 = "cứ mang hết ra đây"; 面朝黄土背朝天 = "bán mặt cho đất bán lưng
  cho trời"; 老天爷追着喂饭 = "ông trời đút cơm tận miệng". Chơi chữ không dịch được thì giữ ý gây cười.
- Triều đại, địa danh đọc Hán-Việt chuẩn, viết đúng chính tả: 民国 / 民初 = Dân quốc / đầu Dân quốc (KHÔNG phải nhà Minh),
  清末 = cuối Thanh, 哈尔滨 = Cáp Nhĩ Tân, 东北 = Đông Bắc, 山东 = Sơn Đông, 河北 = Hà Bắc. Không chêm chữ cái lạ vào từ.
- Đơn vị: 两 = lạng, 斤 = cân (nửa ký), 公斤 = ký, 里 = dặm, 亩 = mẫu. Số đếm giữ nguyên giá trị.
- Một câu nói bị ngắt thành nhiều dòng liên tiếp: dịch sao cho các dòng nối lại thành câu tiếng Việt trọn nghĩa,
  không để dòng nào cụt lủn giữa chừng (vd "ăn no chính là").
- Giữ nguyên số câu và thứ tự; không gộp, không tách câu. Câu chỉ là tiếng cảm thán/vô nghĩa thì dịch ngắn gọn.
- Nếu có BẢNG NHÂN VẬT & XƯNG HÔ: mỗi câu đã ghi người nói ("who") và người nghe ("to").
  PHẢI dùng ĐÚNG cặp xưng hô trong bảng cho cặp người nói -> người nghe đó, giống nhau ở MỌI câu
  (không lúc "em" lúc "con" lúc "cháu"), kể cả khi nhân vật đang giận / quát / đùa: thể hiện cảm xúc bằng từ ngữ
  và ngữ điệu ("mau ra đây ngay!"), KHÔNG đổi sang "mày/tao". Chỉ dùng "mày/tao" nếu bảng ghi đúng như vậy
  (VD bạn thân, côn đồ) hoặc nhân vật đó trước giờ luôn nói vậy.
Chỉ trả về JSON dạng {"lines": [{"i": <số>, "vi": "<câu dịch>"}]}."""

AI_ANALYZE = """Bạn là biên tập viên lồng tiếng phim Trung -> Việt. Đọc TOÀN BỘ lời thoại (do máy nghe chép lại,
có thể sai chữ đồng âm) và lập hồ sơ để dịch + lồng tiếng nhất quán:
1. "characters": các nhân vật có nói. id ngắn (c1, c2...), "name" tên Hán-Việt hoặc vai ("Cô út", "Anh tài xế"),
   "gender": "male" | "female", "age": "child" (trẻ con) | "young" (thanh niên) | "adult" | "old" (người già).
   Suy giới tính/tuổi từ cách gọi (小姑 = cô, 哥哥 = anh trai, 妈 = mẹ, trẻ con xưng 我爸爸, đi học tiểu học...).
2. "address": quy ước xưng hô tiếng Việt CỐ ĐỊNH cho từng cặp (người nói -> người nghe):
   {"from": id, "to": id, "self": cách tự xưng, "call": cách gọi người kia}. VD cháu trai nói với cô:
   {"self": "cháu", "call": "cô"}; cô nói với cháu: {"self": "cô", "call": "cháu"}. Chọn MỘT cách hợp vai và giữ nguyên.
   Suy quan hệ họ hàng từ cách gọi rồi xưng hô theo đó: đứa trẻ gọi A là 小姑 (cô) -> A là em gái của bố đứa trẻ
   -> A nói với bố đứa trẻ xưng "em", gọi "anh" (không phải "tôi"); 舅舅 = cậu (em/anh trai của mẹ), v.v.
   Người lớn trong nhà nói với trẻ con: không dùng "mày/tao". Lập đủ cặp cho MỌI cặp nhân vật có nói với nhau.
   Giới tính: đừng mặc định là nam; xét cách xưng hô, cách người khác gọi, nội dung (đeo đồng hồ xem giờ không nói lên giới tính).
3. "lines": với MỖI câu (theo "i"), ai nói ("who") và nói với ai ("to"); không chắc thì đoán theo ngữ cảnh,
   không xác định được thì để "?". Mỗi câu có thể kèm "v" = cao độ giọng ĐO ĐƯỢC từ âm thanh gốc:
   "trầm" = giọng đàn ông trưởng thành; "cao" = phụ nữ HOẶC trẻ con (bé trai và bé gái đều giọng cao, máy không
   phân biệt được). "v" chỉ dùng để loại trừ: câu "cao" không thể do đàn ông trưởng thành nói, câu "trầm" không thể
   do phụ nữ hay trẻ con nói. Ai là trẻ con, trẻ là trai hay gái, và câu "cao" là của phụ nữ hay của đứa trẻ thì
   PHẢI suy từ nội dung (儿子 = con trai; trẻ con không nói chuyện đền tiền, xin WeChat, tán tỉnh).
   "hz" = cao độ đo được: các câu của CÙNG một người thường có hz gần nhau; trẻ con thường cao nhất (khoảng 300-400),
   phụ nữ khoảng 190-300, đàn ông dưới 150. Khi phân vân một câu "cao" là của phụ nữ hay đứa trẻ, so hz của câu đó
   với hz các câu đã chắc chắn của từng người. Câu gọi tên / gọi vai của X (vd "姜火火你出来", "小姑") thì người nói
   KHÔNG phải X. "emo": cảm xúc/giọng điệu khi nói, 1-3 từ TIẾNG ANH
   (vd "angry", "whiny", "scared", "teasing", "shouting", "sad", "excited", "calm").
Chỉ trả về JSON: {"characters": [...], "address": [...], "lines": [{"i": 0, "who": "c1", "to": "c2", "emo": "calm"}]}"""


AI_FIX = """Dưới đây là lời thoại tiếng Trung do máy nhận dạng giọng nói (ASR) chép lại từ một video. Máy hay chép SAI
chữ đồng âm / gần âm, nhất là tên riêng, thành ngữ, tục ngữ, tiếng lóng, từ tiếng Anh và từ game
(vd 眼睛飞到饭锅里 thật ra là 野鸡飞到饭锅里; 小心蘑菇 thật ra là 小鸡炖蘑菇; 美丽洞人 thật ra là 美丽冻人;
持续调解的寒冷地吧 thật ra là 持续掉血的寒冷debuff; 共和国掌务 thật ra là 共和国长子).
Hãy đọc TOÀN BỘ để hiểu ngữ cảnh rồi sửa lại những câu bị chép sai cho đúng chữ người nói thật sự đã nói.
- Chỉ sửa chữ sai do nghe nhầm; KHÔNG viết lại, không thêm ý, không gộp / tách câu, giữ độ dài gần như cũ.
- Câu nào đã đúng hoặc bạn không chắc thì BỎ QUA, không đưa vào kết quả.
Chỉ trả về JSON: {"lines": [{"i": <số>, "zh": "<câu đã sửa>"}]} gồm RIÊNG các câu có sửa (không có câu nào thì "lines": [])."""


def ai_fix_asr(segs, cfg, title, chunk=150):
    """Nhờ AI sửa các câu tiếng Trung máy nghe nhầm (dựa vào ngữ cảnh) trước khi dịch. Trả về số câu đã sửa."""
    clean_title = re.sub(r"#\S+", "", title or "").strip() or "(không rõ)"
    fixed = 0
    for c0 in range(0, len(segs), chunk):
        idx = range(c0, min(c0 + chunk, len(segs)))
        user = f"Tên video: {clean_title}\n" + json.dumps([{"i": i, "zh": segs[i]["zh"]} for i in idx], ensure_ascii=False)
        res = _parse_json(ai_chat(cfg, AI_FIX, user, strong_only=True)).get("lines") or []
        for x in res:
            try:
                i, new = int(x["i"]), str(x["zh"]).strip()
            except Exception:
                continue
            old = segs[i]["zh"] if i in idx else ""
            # chỉ nhận sửa đổi nhỏ: độ dài gần như cũ, không phải viết lại cả câu
            if old and new and new != old and 0.6 <= len(new) / max(1, len(old)) <= 1.6:
                segs[i]["zh_raw"], segs[i]["zh"] = old, new
                fixed += 1
                if fixed <= 6:
                    print(f"      {old}  ->  {new}", flush=True)
    return fixed


def _pitch_label(f0):
    if not f0:
        return ""
    return "trầm" if f0 < 145 else "cao" if f0 > 190 else ""


def fix_speakers(segs, chars):
    """Câu có cao độ giọng gốc trái hẳn giới tính người nói AI gán -> thường là nhầm người nói với người nghe: đổi lại."""
    by = {c["id"]: c for c in chars}
    low = lambda c: c.get("gender") == "male" and c.get("age") != "child"  # nhân vật giọng trầm
    n = 0
    for s in segs:
        c, f0 = by.get(s.get("who")), s.get("f0")
        if not c or not f0 or not (f0 < 140 or f0 > 190):
            continue
        line_low = f0 < 140
        if low(c) == line_low:
            continue
        other = by.get(s.get("to"))
        if not other or low(other) != line_low:  # người nghe cũng không hợp -> tìm nhân vật duy nhất hợp giọng
            fit = [x for x in chars if low(x) == line_low]
            other = fit[0] if len(fit) == 1 else None
        if other:
            s["who"], s["to"] = other["id"], c["id"]
            n += 1
    if n:
        log(f"  {n} câu đổi người nói theo giọng gốc (AI gán nhầm nam/nữ)")


def ai_analyze(segs, cfg, title, chunk=150, seed=None):
    """AI xác định nhân vật, người nói từng câu và bảng xưng hô cố định. Trả về (characters, address).
    seed: (characters, address) của các tập trước cùng bộ phim -> giữ đúng tên, giới tính, xưng hô như cũ."""
    log("AI phân tích nhân vật, người nói và xưng hô...")
    chars, address = (list(seed[0]), list(seed[1])) if seed else ([], [])
    clean_title = re.sub(r"#\S+", "", title or "").strip() or "(không rõ)"
    for c0 in range(0, len(segs), chunk):
        idx = range(c0, min(c0 + chunk, len(segs)))
        user = (f"Tên video: {clean_title}\n"
                + (("Hồ sơ đã có từ đoạn trước (giữ nguyên id, bổ sung nếu có nhân vật mới):\n"
                    + json.dumps({"characters": chars, "address": address}, ensure_ascii=False) + "\n\n") if chars else "")
                + "Lời thoại:\n" + json.dumps([dict({"i": i, "t": f"{segs[i]['start']:.1f}s", "zh": segs[i]["zh"]},
                                                   **({"v": _pitch_label(segs[i].get("f0")), "hz": int(segs[i]["f0"])}
                                                      if _pitch_label(segs[i].get("f0")) else {}))
                                              for i in idx],
                                             ensure_ascii=False))
        res = _parse_json(ai_chat(cfg, AI_ANALYZE, user))
        known = {c["id"] for c in chars}
        for c in res.get("characters", []):
            if c.get("id") and c["id"] not in known:
                chars.append({k: c.get(k) for k in ("id", "name", "gender", "age")})
                known.add(c["id"])
        pairs = {(a["from"], a["to"]) for a in address}
        for a in res.get("address", []):
            if a.get("from") and a.get("to") and (a["from"], a["to"]) not in pairs:
                address.append({k: a.get(k) for k in ("from", "to", "self", "call")})
        for x in res.get("lines", []):
            try:
                i = int(x["i"])
            except (KeyError, TypeError, ValueError):
                continue
            if c0 <= i < c0 + chunk:
                segs[i]["who"] = x.get("who") if x.get("who") in known else None
                segs[i]["to"] = x.get("to") if x.get("to") in known else None
                emo = re.sub(r"[^A-Za-z ,-]", "", str(x.get("emo") or "")).strip()[:30]
                segs[i]["emo"] = emo if emo.lower() not in ("", "neutral", "normal", "calm") else ""
    names = {c["id"]: c["name"] for c in chars}
    for c in chars:
        log(f"  Nhân vật: {c['name']} ({'nam' if c.get('gender') == 'male' else 'nữ'}, {c.get('age')})")
    for a in address:
        if a["from"] in names and a["to"] in names:
            log(f"  Xưng hô: {names[a['from']]} -> {names[a['to']]}: tự xưng \"{a['self']}\", gọi \"{a['call']}\"")
    return chars, address


SERIES_DIR = Path(__file__).resolve().parent / "series"


def series_file(name):
    safe = re.sub(r"[^\w\- ]+", " ", name or "").strip()[:60]
    return SERIES_DIR / f"{safe or 'phim'}.json"


def load_series(name):
    """Hồ sơ một bộ phim: nhân vật (tên, giới tính, tuổi), xưng hô theo TÊN, giọng đã dùng cho từng nhân vật."""
    if not name:
        return None
    try:
        d = json.loads(series_file(name).read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("name", name)
    d.setdefault("characters", [])
    d.setdefault("address", [])
    d.setdefault("voices", {})
    return d


def save_series(series):
    if series:
        SERIES_DIR.mkdir(exist_ok=True)
        series_file(series["name"]).write_text(json.dumps(series, ensure_ascii=False, indent=2), encoding="utf-8")


def series_seed(series):
    """Hồ sơ phim -> (characters, address) dạng id để đưa vào bước phân tích như "hồ sơ đã có"."""
    if not series or not series["characters"]:
        return [], []
    chars = [{"id": f"c{k + 1}", "name": c["name"], "gender": c.get("gender"), "age": c.get("age")}
             for k, c in enumerate(series["characters"])]
    ids = {c["name"]: c["id"] for c in chars}
    address = [{"from": ids[a["from"]], "to": ids[a["to"]], "self": a.get("self"), "call": a.get("call")}
               for a in series["address"] if a.get("from") in ids and a.get("to") in ids]
    return chars, address


def series_merge(series, chars, address):
    """Thêm nhân vật / cặp xưng hô MỚI của tập này vào hồ sơ phim (cái đã có thì giữ nguyên như tập trước)."""
    if not series:
        return
    names = {c["id"]: c["name"] for c in chars}
    known = {c["name"] for c in series["characters"]}
    for c in chars:
        if c.get("name") and c["name"] not in known:
            series["characters"].append({k: c.get(k) for k in ("name", "gender", "age")})
            known.add(c["name"])
    pairs = {(a["from"], a["to"]) for a in series["address"]}
    for a in address:
        f, t = names.get(a.get("from")), names.get(a.get("to"))
        if f and t and (f, t) not in pairs:
            series["address"].append({"from": f, "to": t, "self": a.get("self"), "call": a.get("call")})
            pairs.add((f, t))


def _budget(segs, i, total):
    nxt = segs[i + 1]["start"] if i + 1 < len(segs) else total
    avail = max(segs[i]["end"] - segs[i]["start"], min(nxt - segs[i]["start"], segs[i]["end"] - segs[i]["start"] + 1.5))
    return max(3, int(avail * VI_SYLLABLES_PER_SEC * 1.12))  # đọc nhanh nhẹ ~1,1x còn hơn cắt mất ý


def ai_translate(segs, cfg, title, total, chunk=60, context=8, chars=None, address=None):
    log(f"Dịch bằng AI ({cfg['provider']} / {cfg['model']})...")
    clean_title = re.sub(r"#\S+", "", title or "").strip() or "(không rõ)"
    names = {c["id"]: c["name"] for c in (chars or [])}
    profile = ""
    if chars:
        profile = ("BẢNG NHÂN VẬT & XƯNG HÔ (bắt buộc tuân theo):\n"
                   + "\n".join(f"- {c['name']}: {'nam' if c.get('gender') == 'male' else 'nữ'}, {c.get('age')}" for c in chars)
                   + "\n" + "\n".join(f"- {names.get(a['from'], a['from'])} nói với {names.get(a['to'], a['to'])}: "
                                      f"tự xưng \"{a['self']}\", gọi \"{a['call']}\"" for a in (address or []))
                   + "\n\n")
    done = 0
    for c0 in range(0, len(segs), chunk):
        idx = list(range(c0, min(c0 + chunk, len(segs))))
        prev = [f"{segs[j]['zh']} => {segs[j].get('vi', '')}" for j in range(max(0, c0 - context), c0)]
        lines = []
        for i in idx:
            item = {"i": i, "zh": segs[i]["zh"], "t": f"{segs[i]['start']:.1f}s", "max": _budget(segs, i, total)}
            if segs[i].get("who"):
                item["who"] = names.get(segs[i]["who"], segs[i]["who"])
            if segs[i].get("to"):
                item["to"] = names.get(segs[i]["to"], segs[i]["to"])
            lines.append(item)
        user = (f"Tên video: {clean_title}\n" + profile
                + (("Các câu ngay trước (đã dịch, để giữ ngữ cảnh):\n" + "\n".join(prev) + "\n\n") if prev else "")
                + "Dịch các câu sau:\n" + json.dumps(lines, ensure_ascii=False))
        for attempt in range(3):
            try:
                res = {int(x["i"]): str(x["vi"]).strip() for x in _parse_json(ai_chat(cfg, AI_SYSTEM, user))["lines"]}
                missing = [i for i in idx if not res.get(i)]
                if len(missing) > len(idx) // 5:
                    raise ValueError(f"AI bỏ sót {len(missing)} câu")
                for i in idx:
                    if res.get(i):
                        segs[i]["vi"] = res[i]
                break
            except Exception as e:
                if attempt == 2:
                    raise
                log(f"AI trả lỗi ({str(e)[:100]}), thử lại...")
                import time

                time.sleep(4 * (attempt + 1))
        done += len(idx)
        print(f"   {done}/{len(segs)}", flush=True)
    return segs


def ai_shorten(items, cfg):
    """items: list (zh, vi, max_syllables) -> list câu ngắn hơn."""
    user = ("Các câu lồng tiếng sau quá dài so với thời gian nói. Viết lại NGẮN HƠN, không vượt quá số âm tiết 'max'. "
            "Bỏ từ đệm, từ lặp, lời rào đón trước; GIỮ tên riêng, địa danh, con số, tên món / đồ vật và ý gây cười; "
            "câu hỏi vẫn là câu hỏi; không để câu cụt lủn:\n"
            + json.dumps([{"i": k, "zh": z, "vi": v, "max": m} for k, (z, v, m) in enumerate(items)], ensure_ascii=False))
    res = {int(x["i"]): str(x["vi"]).strip() for x in _parse_json(ai_chat(cfg, AI_SYSTEM, user))["lines"]}
    return [res.get(k) or v for k, (z, v, m) in enumerate(items)]


def enforce_address(segs, address):
    """Chốt chặn sau AI: câu dùng "mày/tao" trong khi bảng xưng hô của cặp người nói -> người nghe không phải vậy
    thì thay bằng đúng cặp xưng hô trong bảng (AI yếu đôi khi bỏ qua quy tắc khi nhân vật đang quát)."""
    table = {(a.get("from"), a.get("to")): a for a in address or []}
    fixed = 0

    def sub(text, word, repl):
        def r(m):
            return repl[:1].upper() + repl[1:] if m.group(0)[0].isupper() else repl
        return re.sub(rf"\b{word}\b", r, text, flags=re.IGNORECASE)

    for s in segs:
        pair = table.get((s.get("who"), s.get("to")))
        if not pair or not s.get("vi"):
            continue
        me, you = (pair.get("self") or "").strip(), (pair.get("call") or "").strip()
        new = s["vi"]
        if me and me.lower() != "tao":
            new = sub(new, "tao", me)
        if you and you.lower() != "mày":
            new = sub(new, "mày", you)
        if new != s["vi"]:
            s["vi"], fixed = new, fixed + 1
    if fixed:
        log(f"  Sửa xưng hô mày/tao theo bảng ở {fixed} câu")


def translate(segs, src_lang, title="", total=0, ai_cfg=None, series=None):
    """Dịch; trả về (characters, address) nếu AI phân tích được nhân vật (dùng để chọn giọng)."""
    chars, address = [], []
    if ai_cfg:
        try:
            log("AI sửa lời tiếng Trung nghe nhầm theo ngữ cảnh...")
            n = ai_fix_asr(segs, ai_cfg, title)
            log(f"  Đã sửa {n} câu nghe nhầm" if n else "  Không có câu nào cần sửa")
        except Exception as e:
            log(f"  Bỏ qua bước sửa lời nghe nhầm (model mạnh không phản hồi: {str(e)[:80]}), dịch theo lời máy nghe")
        try:
            seed = series_seed(series)
            if seed[0]:
                log(f"Phim \"{series['name']}\": dùng lại {len(seed[0])} nhân vật và {len(seed[1])} cặp xưng hô của các tập trước")
            chars, address = ai_analyze(segs, ai_cfg, title, seed=seed)
            fix_speakers(segs, chars)
            series_merge(series, chars, address)
        except Exception as e:
            log(f"Không phân tích được nhân vật ({str(e)[:120]}), dịch không có bảng xưng hô")
        try:
            ai_translate(segs, ai_cfg, title, total, chars=chars, address=address)
            enforce_address(segs, address)
            missing = [s for s in segs if not s.get("vi")]
            if missing:
                _google_translate(missing, src_lang)
            names = {c["id"]: c["name"] for c in chars}
            for s in segs:
                who = names.get(s.get("who"), "")
                print(f"   {s['start']:6.1f}s  {(who + ': ') if who else ''}{s['vi']}", flush=True)
            globals()["LAST_TRANSLATOR"] = f"{ai_cfg['provider']}/{ai_cfg['model']}"
            return chars, address
        except Exception as e:
            log(f"Dịch bằng AI lỗi ({str(e)[:150]}) -> dùng Google Dịch")
    _google_translate(segs, src_lang)
    globals()["LAST_TRANSLATOR"] = "google"
    return chars, address


def _google_translate(segs, src_lang):
    tr = GoogleGtx("zh-CN" if src_lang == "zh" else src_lang, "vi")
    log("Đang dịch sang tiếng Việt (Google Dịch)...")
    # Dịch theo lô, mỗi câu một dòng để giữ ngữ cảnh mà vẫn tách được
    batch, size = [], 0
    batches = []
    for i, s in enumerate(segs):
        if size + len(s["zh"]) > 1500 and batch:
            batches.append(batch)
            batch, size = [], 0
        batch.append(i)
        size += len(s["zh"]) + 1
    if batch:
        batches.append(batch)
    for b in batches:
        text = "\n".join(segs[i]["zh"] for i in b)
        out = (tr.translate(text) or "").split("\n")
        if len(out) != len(b):  # lệch dòng -> dịch từng câu
            out = [tr.translate(segs[i]["zh"]) or "" for i in b]
        for i, v in zip(b, out):
            segs[i]["vi"] = v.strip()
    for s in segs:
        print(f"   {s['start']:6.1f}s  {s['vi']}", flush=True)
    return segs


def fmt_ts(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(segs, path, key):
    with open(path, "w", encoding="utf-8") as f:
        for i, s in enumerate(segs, 1):
            f.write(f"{i}\n{fmt_ts(s['start'])} --> {fmt_ts(s['end'])}\n{s[key]}\n\n")


# ------------------------------------------------------------------- 4. Dub
async def _tts(text, voice, rate, out, pitch=0):
    import edge_tts

    await edge_tts.Communicate(text, voice, rate=f"{rate:+d}%", pitch=f"{pitch:+d}Hz").save(str(out))


def _edge_try(text, spec, out, tries, wait):
    """Máy chủ Edge TTS từ chối ngẫu nhiên một phần yêu cầu -> thử lại nhiều lần, nghỉ ngắn.
    spec = (tên giọng, cao độ Hz, tốc độ %)."""
    import time

    voice, pitch, rate = spec
    for _ in range(tries):
        try:
            asyncio.run(_tts(text, voice, rate, out, pitch))
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return True
        except Exception:
            pass
        time.sleep(wait)
    return False


# ---------------- Chọn giọng theo nhân vật
MALE, FEMALE = "vi-VN-NamMinhNeural", "vi-VN-HoaiMyNeural"
VOICE_BASE = {  # (giới tính, tuổi) -> (giọng, cao độ Hz, tốc độ %)
    ("male", "child"): (FEMALE, 55, 6),     # tiếng Việt chỉ có 2 giọng -> trẻ con = giọng nữ nâng cao độ
    ("female", "child"): (FEMALE, 45, 6),
    ("male", "old"): (MALE, -12, -6),
    ("female", "old"): (FEMALE, -14, -6),
    ("male", "young"): (MALE, 4, 2),
    ("female", "young"): (FEMALE, 4, 2),
}
PITCH_SPREAD = [0, 12, -10, 22, -18]  # nhiều nhân vật cùng loại giọng -> lệch cao độ để phân biệt


def estimate_f0(pcm16k, start, end):
    """Cao độ giọng trung bình (Hz) của một đoạn audio 16 kHz (tự tương quan), None nếu không đo được."""
    import numpy as np

    a = pcm16k[int(start * 16000): int(end * 16000)].astype(np.float32)
    if len(a) < 1600:
        return None
    frame, hop, lo, hi = 640, 320, 16000 // 450, 16000 // 70
    rms = np.array([np.sqrt(np.mean(a[k:k + frame] ** 2)) for k in range(0, len(a) - frame, hop)])
    if not len(rms):
        return None
    thr = np.percentile(rms, 50)
    f0s = []
    for n, k in enumerate(range(0, len(a) - frame, hop)):
        if rms[n] < thr:
            continue
        x = a[k:k + frame] - a[k:k + frame].mean()
        ac = np.correlate(x, x, "full")[frame - 1:]
        if ac[0] <= 0:
            continue
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] / ac[0] > 0.35:
            f0s.append(16000 / lag)
    return float(np.median(f0s)) if len(f0s) >= 3 else None


def assign_voices(segs, chars, wav16k):
    """Gán (giọng, cao độ, tốc độ) cho từng câu theo nhân vật AI đã xác định; câu không rõ người nói
    thì đoán nam/nữ theo cao độ giọng gốc."""
    import wave

    import numpy as np

    with wave.open(str(wav16k), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    # cao độ giọng gốc của từng câu: nam ~80-150 Hz, nữ ~190-300 Hz, trẻ con ~300-380 Hz
    for s in segs:
        s["f0"] = estimate_f0(pcm, s["start"], s["end"])
    if not chars:
        # AI không phân tích được nhân vật -> không biết ai nói câu nào. Đoán theo cao độ từng câu sẽ ra lộn xộn
        # nhiều giọng, nên dùng MỘT giọng cho cả video, nam/nữ theo cao độ phổ biến nhất của giọng gốc.
        f0s = sorted(s["f0"] for s in segs if s.get("f0"))
        mid = f0s[len(f0s) // 2] if f0s else 0
        chars = [{"id": "c1", "name": "Người đọc", "gender": "female" if mid > 175 else "male", "age": "adult"}]
        for s in segs:
            s["who"] = "c1"
        log("  Không có bảng nhân vật -> dùng một giọng cho cả video")
    names = {c["id"]: c["name"] for c in chars}
    # 1) Sửa giới tính AI đoán sai cho CẢ nhân vật — chỉ khi gần như mọi câu của nhân vật đó đều trái giới tính
    #    (AI hay gán nhầm vài câu của người khác cho nhân vật, nên không dựa vào trung bình)
    for c in chars:
        f0s = [s["f0"] for s in segs if s.get("who") == c["id"] and s.get("f0")]
        if len(f0s) < 3:
            continue
        hi = sum(f > 175 for f in f0s) / len(f0s)
        lo = sum(f < 150 for f in f0s) / len(f0s)
        if c.get("gender") == "male" and hi >= 0.75 and c.get("age") != "child":
            log(f"  Sửa giới tính: {c['name']} -> nữ ({hi:.0%} số câu giọng gốc cao)")
            c["gender"] = "female"
        elif c.get("gender") == "female" and lo >= 0.75:
            log(f"  Sửa giới tính: {c['name']} -> nam ({lo:.0%} số câu giọng gốc trầm)")
            c["gender"] = "male"
    spec_of, used, vkey_of = {}, {}, {}
    for c in chars:
        g = "male" if c.get("gender") == "male" else "female"
        age = c.get("age") if c.get("age") in ("child", "old", "young") else "adult"
        voice, pitch, rate = VOICE_BASE.get((g, age), (MALE if g == "male" else FEMALE, 0, 0))
        k = used.get((g, age), 0)
        used[(g, age)] = k + 1
        spec_of[c["id"]] = (voice, pitch + PITCH_SPREAD[k % len(PITCH_SPREAD)], rate)
        vkey_of[c["id"]] = (g, age, k)
    def by_pitch(f0):  # giọng theo cao độ thật khi không biết / nghi ngờ người nói
        if f0 and f0 > 320:
            return VOICE_BASE[("female", "child")]
        return (FEMALE, 0, 0) if (f0 and f0 > 175) else (MALE, 0, 0)

    def key_by_pitch(f0):
        if f0 and f0 > 320:
            return ("female", "child", 0)
        return ("female", "adult", 0) if (f0 and f0 > 175) else ("male", "adult", 0)

    fixed = 0
    for s in segs:
        f0, spec = s.get("f0"), spec_of.get(s.get("who"))
        # 2) Câu có cao độ trái hẳn giới tính nhân vật được gán -> AI đoán nhầm người nói -> theo cao độ thật
        # (video chỉ có một người đọc thì không đổi: lên xuống giọng không phải là người khác)
        if len(spec_of) > 1 and spec and f0 and (
                (spec[0] == MALE and f0 > 180) or (spec[0] == FEMALE and spec[1] < 30 and f0 < 135)):
            spec, fixed = None, fixed + 1
        s["voice"] = spec or by_pitch(f0)
        s["vkey"] = vkey_of[s["who"]] if spec else key_by_pitch(f0)
        s["wname"] = names.get(s.get("who"), "") if spec else ""
    if fixed:
        log(f"  {fixed} câu đổi giọng theo cao độ giọng gốc (AI đoán nhầm người nói)")
    for cid, (v, p, r) in spec_of.items():
        log(f"  Giọng {names[cid]}: {'nữ' if v == FEMALE else 'nam'}, cao độ {p:+d}Hz, tốc độ {r:+d}%")


def tts_all(jobs, voice, workers=4):
    """jobs: list (text, out_path) hoặc (text, out_path, spec). Tạo song song; câu nào Edge lỗi mãi thì dùng Google TTS."""
    from concurrent.futures import ThreadPoolExecutor

    default = voice if isinstance(voice, tuple) else (voice, 0, 0)
    spec = [j[2] if len(j) > 2 and j[2] else default for j in jobs]
    done = [False] * len(jobs)
    passes = [(10, 0.5), (10, 3.0)]  # lượt nhanh, rồi lượt chậm cho các câu còn sót
    for tries, wait in passes:
        todo = [i for i, ok in enumerate(done) if not ok]
        if not todo:
            break
        with ThreadPoolExecutor(workers) as ex:
            res = ex.map(lambda i: _edge_try(jobs[i][0], spec[i], jobs[i][1], tries, wait), todo)
            for i, ok in zip(todo, res):
                done[i] = ok
                if ok:
                    print(f"   {sum(done)}/{len(jobs)}", end="\r", flush=True)
    print()
    for i, ok in enumerate(done):
        if not ok:
            from gtts import gTTS

            log(f"Edge TTS lỗi, dùng Google TTS cho câu: {jobs[i][0][:40]}")
            gTTS(jobs[i][0], lang="vi").save(str(jobs[i][1]))


SR = 24000

# ---------------- Giọng AI của Gemini (nhiều giọng thật, có cảm xúc)
GEMINI_TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts", "gemini-3.1-flash-tts-preview",
                     "gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts"]
GEMINI_VOICES = {  # (giới tính, tuổi) -> các giọng lần lượt cho nhân vật thứ 1, 2, 3...
    ("male", "child"): ["Leda", "Zephyr", "Puck"],
    ("female", "child"): ["Leda", "Zephyr", "Laomedeia"],
    ("male", "young"): ["Puck", "Umbriel", "Achird", "Fenrir"],
    ("female", "young"): ["Aoede", "Zephyr", "Laomedeia", "Despina"],
    ("male", "adult"): ["Charon", "Orus", "Iapetus", "Fenrir", "Algenib"],
    ("female", "adult"): ["Kore", "Aoede", "Despina", "Erinome"],
    ("male", "old"): ["Algenib", "Alnilam", "Charon"],
    ("female", "old"): ["Gacrux", "Sulafat", "Kore"],
}
# giọng cho người dùng tự chọn trên web (giọng cố định cho nhân vật chính / người dẫn chuyện)
GEMINI_PICK = ["Aoede", "Kore", "Zephyr", "Leda", "Despina", "Charon", "Puck", "Orus", "Fenrir", "Algenib"]
GEMINI_AGE_STYLE = {"child": "childlike voice", "old": "elderly voice"}


class GeminiQuotaOut(Exception):
    pass


def _gemini_tts_call(key, text, voice, state):
    """Gọi Gemini TTS, xoay model khi hết hạn mức ngày; trả về PCM int16 24 kHz (numpy)."""
    import base64
    import io
    import time
    import wave

    import numpy as np
    import requests

    body = {"contents": [{"parts": [{"text": text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
    waits = 0
    while state["models"]:
        model = state["models"][0]
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": key}, json=body, timeout=180)
        except requests.RequestException:
            waits += 1
            if waits > 3:
                state["models"].pop(0)
            continue
        if r.status_code == 200:
            try:
                raw = base64.b64decode(r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
            except (KeyError, IndexError, ValueError):
                state["models"].pop(0)
                continue
            state["calls"] += 1
            if raw[:4] == b"RIFF":
                with wave.open(io.BytesIO(raw)) as w:
                    sr, raw = w.getframerate(), w.readframes(w.getnframes())
                if sr != SR:
                    raise RuntimeError(f"Gemini TTS trả về {sr} Hz")
            return np.frombuffer(raw, dtype=np.int16)
        if r.status_code == 429:
            err = r.json().get("error", {})
            delay, daily = 0, False
            for d in err.get("details", []):
                for v in d.get("violations", []):
                    daily |= "PerDay" in (v.get("quotaId") or "")
                if d.get("retryDelay"):
                    delay = float(str(d["retryDelay"]).rstrip("s") or 0)
            if not daily and delay <= 70 and waits < 4:  # hạn mức theo phút -> chờ rồi gọi lại
                waits += 1
                time.sleep(delay + 2)
                continue
            log(f"   {model}: hết lượt miễn phí, chuyển model khác")
        elif r.status_code in (401, 403):
            raise GeminiQuotaOut(f"Key không dùng được Gemini TTS ({r.status_code})")
        elif r.status_code >= 500 and waits < 3:
            waits += 1
            time.sleep(3)
            continue
        state["models"].pop(0)
        waits = 0
    raise GeminiQuotaOut("Hết lượt Gemini TTS ở mọi model")


def _norm_syl(text):
    """Tách âm tiết tiếng Việt, bỏ dấu để so khớp mờ: 'Cháu nhớ cô' -> [('cháu','chau'), ...]."""
    import unicodedata

    out = []
    for w in re.findall(r"\w+", text.lower()):
        base = "".join(c for c in unicodedata.normalize("NFD", w.replace("đ", "d")) if not unicodedata.combining(c))
        out.append((w, base))
    return out


def _align_split(pcm, texts, state):
    """Cắt audio đọc liền nhiều câu thành từng câu bằng cách NGHE LẠI (Whisper, mốc thời gian từng chữ) rồi so chữ
    với lời thoại. Trả về list đoạn PCM, câu nào nghe không khớp thì None."""
    import numpy as np

    if "asr" not in state:
        from faster_whisper import WhisperModel

        state["asr"] = WhisperModel("small", device="cpu", compute_type="int8")
    x = pcm.astype(np.float32) / 32768
    x16 = np.interp(np.arange(0, len(x), SR / 16000), np.arange(len(x)), x).astype(np.float32)
    segs, _ = state["asr"].transcribe(x16, language="vi", word_timestamps=True, condition_on_previous_text=False)
    heard = []
    for sg in segs:
        for w in sg.words or []:
            for tok in _norm_syl(w.word):
                heard.append((w.start, w.end, tok))
    exp = [(li, tok) for li, t in enumerate(texts) for tok in _norm_syl(t)]
    n, m = len(exp), len(heard)
    if not n or not m:
        return [None] * len(texts)

    def sc(a, b):
        return 2 if a[0] == b[0] else 1 if a[1] == b[1] else -1

    dp = np.zeros((n + 1, m + 1), dtype=np.int32)
    dp[:, 0] = -np.arange(n + 1)
    dp[0, :] = -np.arange(m + 1)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i, j] = max(dp[i - 1, j - 1] + sc(exp[i - 1][1], heard[j - 1][2]), dp[i - 1, j] - 1, dp[i, j - 1] - 1)
    hit = {}  # chỉ số âm tiết lời thoại -> chỉ số chữ nghe được
    i, j = n, m
    while i > 0 and j > 0:
        v = sc(exp[i - 1][1], heard[j - 1][2])
        if dp[i, j] == dp[i - 1, j - 1] + v:
            if v > 0:
                hit[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif dp[i, j] == dp[i - 1, j] - 1:
            i -= 1
        else:
            j -= 1
    span = []
    for li, t in enumerate(texts):
        ids = [k for k, (l, _) in enumerate(exp) if l == li]
        got = [hit[k] for k in ids if k in hit]
        # chữ có số ("155mm", "800") máy nghe ra chữ khác hẳn -> không tính vào tỉ lệ khớp
        words = [k for k in ids if not re.search(r"\d", exp[k][1][0])]
        good = len([k for k in words if k in hit]) >= max(1, round(0.6 * len(words))) and got
        span.append((heard[min(got)][0], heard[max(got)][1]) if good else None)
    hop = int(0.02 * SR)
    fr = len(pcm) // hop
    rms = np.sqrt(np.mean(pcm[: fr * hop].astype(np.float32).reshape(fr, hop) ** 2, axis=1)) if fr else np.zeros(1)

    def quiet_cut(t0, t1):  # điểm lặng nhất giữa hai câu
        a, b = max(0, int(t0 / 0.02) - 3), min(fr, int(t1 / 0.02) + 4)
        return (a + int(np.argmin(rms[a:b]))) * hop if b > a else int(t0 * SR)

    out = []
    for li, sp in enumerate(span):
        before = span[li - 1] if li > 0 else None
        after = span[li + 1] if li + 1 < len(span) else None
        # câu không khớp chữ, hoặc đứng cạnh câu không khớp (ranh giới không chắc) -> bỏ, sẽ đọc bằng giọng Edge
        if not sp or (li > 0 and not before) or (li + 1 < len(span) and not after):
            out.append(None)
            continue
        c0 = quiet_cut(before[1], sp[0]) if before else 0
        c1 = quiet_cut(sp[1], after[0]) if after else len(pcm)
        out.append(pcm[c0:c1] if c1 - c0 > 0.1 * SR else None)
    return out


def _trim(pcm, thr=400):
    import numpy as np

    loud = np.where(np.abs(pcm) > thr)[0]
    return pcm[max(0, loud[0] - int(0.03 * SR)): loud[-1] + int(0.08 * SR)] if len(loud) else pcm


def _syllables(text):
    # con số đọc thành nhiều tiếng: "155mm" = "một trăm năm mươi lăm mi li mét"
    return max(1, len(re.findall(r"\w+", text)) + 2 * len(re.findall(r"\d", text)))


def gemini_tts_all(items, key, state, max_lines=8, max_chars=360):
    """items: list (text, out_wav, voice_name, style). Gộp các câu cùng giọng thành ít lần gọi (gói miễn phí chỉ
    ~10 lượt/ngày/model), rồi cắt lại theo khoảng lặng. Trả về tập chỉ số câu đã tạo được."""
    import wave

    groups = {}
    for i, it in enumerate(items):
        groups.setdefault(it[2], []).append(i)
    batches = []
    for voice, ids in groups.items():
        cur = []
        for i in ids:
            if cur and (len(cur) >= max_lines or sum(len(items[j][0]) for j in cur + [i]) > max_chars):
                batches.append((voice, cur))
                cur = []
            cur.append(i)
        if cur:
            batches.append((voice, cur))

    done = set()

    def run_batch(voice, ids):
        text = "\n[pause]\n".join((f"[{items[i][3]}] " if items[i][3] else "") + items[i][0] for i in ids)
        pcm = _gemini_tts_call(key, text, voice, state)
        parts = _align_split(pcm, [items[i][0] for i in ids], state) if len(ids) > 1 else [pcm]
        for p, i in zip(parts, ids):
            if p is None:
                continue
            p = _trim(p)
            if not 0.05 <= len(p) / SR / _syllables(items[i][0]) <= 0.8:
                continue
            with wave.open(str(items[i][1]), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(p.tobytes())
            done.add(i)

    out_of_quota = False
    for n, (voice, ids) in enumerate(batches, 1):
        print(f"   lượt {n}/{len(batches)} ({voice}, {len(ids)} câu)", end="\r", flush=True)
        try:
            run_batch(voice, ids)
        except GeminiQuotaOut as e:
            print()
            log(str(e))
            out_of_quota = True
            break
    print()
    # câu cắt ra không khớp -> đọc lại riêng từng câu (không cần cắt), miễn là còn lượt
    miss = [i for i in range(len(items)) if i not in done]
    if miss and not out_of_quota:
        log(f"Đọc lại riêng {len(miss)} câu cắt chưa khớp...")
        for i in miss:
            try:
                run_batch(items[i][2], [i])
            except GeminiQuotaOut as e:
                log(str(e))
                break
    return done

def dub_estimate(segs, tts, el_model="v3"):
    """Ước tính chi phí lồng tiếng trước khi đọc: số câu, số ký tự, credit ElevenLabs / lượt Gemini."""
    import math

    lines = [s["vi"].strip() for s in segs if s.get("vi", "").strip()]
    est = {"tts": tts, "lines": len(lines), "chars": sum(len(x) for x in lines)}
    if tts == "eleven":
        est["model"] = el_model
        est["credits"] = int(est["chars"] * (0.5 if el_model == "flash" else 1) * 1.08)  # +8% cho câu phải đọc lại
        try:
            import eleven_tts

            kc = eleven_tts.load_config()
            c = eleven_tts.credits(kc["key"]) if kc else None
            if c and c[1]:
                est["credits_left"] = int(c[1] - c[0])
        except Exception:
            pass
    elif tts == "gemini":
        est["calls"] = math.ceil(len(lines) / 8 * 1.3) + 1
    return est


def estimate_text(est):
    if est["tts"] == "eleven":
        left = f", tài khoản còn {est['credits_left']:,}".replace(",", ".") if "credits_left" in est else ""
        return (f"{est['lines']} câu, {est['chars']:,} ký tự -> khoảng {est['credits']:,} credit ElevenLabs "
                f"(model {est['model']})").replace(",", ".") + left
    if est["tts"] == "gemini":
        return f"{est['lines']} câu -> khoảng {est['calls']} lượt gọi giọng Gemini"
    if est["tts"] == "vieneu":
        return f"{est['lines']} câu, giọng VieNeu chạy trên máy (miễn phí, đọc mất khoảng {max(1, est['chars'] // 900)} phút)"
    return f"{est['lines']} câu, giọng Edge miễn phí"


def decode_pcm(path, tempo=None):
    """Giải mã audio thành mảng int16 mono 24 kHz (tuỳ chọn đọc nhanh hơn), bỏ khoảng lặng đầu/cuối."""
    import numpy as np

    cmd = ["ffmpeg", "-v", "error", "-i", str(path)]
    if tempo and abs(tempo - 1) > 0.01:
        cmd += ["-af", f"atempo={tempo:.3f}"]
    r = subprocess.run(cmd + ["-f", "s16le", "-ac", "1", "-ar", str(SR), "-"], capture_output=True)
    if r.returncode:
        raise RuntimeError(f"Không đọc được audio {path}: {r.stderr.decode(errors='replace')[-300:]}")
    pcm = np.frombuffer(r.stdout, dtype=np.int16)
    loud = np.where(np.abs(pcm) > 400)[0]
    if len(loud):
        pcm = pcm[max(0, loud[0] - int(0.03 * SR)): loud[-1] + int(0.08 * SR)]
    return pcm


def has_audio(video):
    out = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(video)])
    return bool(out.strip())


def dub(segs, video, workdir, voice, bg_volume, max_speed, ai_cfg=None, tts="edge", gem_fixed="",
        el_voice="", el_model="v3", vn_voice="", series=None):
    import wave

    import numpy as np

    total = media_duration(video)
    multi = voice == "auto"
    log("Lồng tiếng (nhiều giọng theo nhân vật)..." if multi else f"Lồng tiếng ({voice})...")
    tdir = workdir / "tts"
    tdir.mkdir(exist_ok=True)
    idx = [i for i, s in enumerate(segs) if s.get("vi", "").strip()]
    path = {i: tdir / f"{i:04}.mp3" for i in idx}
    # sổ ghi các câu đã đọc: chạy lại sau khi lỗi thì dùng lại clip cũ, không tốn lượt / credit lần nữa
    man_path = tdir / "manifest.json"
    try:
        manifest = json.loads(man_path.read_text(encoding="utf-8"))
    except Exception:
        manifest = {}

    def sig(i):
        return f"{tts}|{segs[i]['vi'].strip()}|{list(segs[i].get('voice') or [])}|{list(segs[i].get('vkey') or [])}"

    def save_manifest():
        manifest["_edge_only"] = [list(k) for k in edge_only]
        man_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    for i, v in (manifest.get("_short") or {}).items():  # câu đã rút gọn ở lần chạy trước -> dùng đúng câu đó
        if int(i) < len(segs) and segs[int(i)].get("vi", "").strip() == v.get("from"):
            segs[int(i)]["vi"] = v["to"]
    if not multi:
        for s in segs:
            s["voice"] = (voice, 0, 0)
            s["vkey"] = ("male" if voice == MALE else "female", "adult", 0)
    gem = None
    if tts == "gemini":
        kc = ai_cfg or load_ai_config() or {}
        if kc.get("provider") == "gemini" and kc.get("key"):
            gem = {"models": list(GEMINI_TTS_MODELS), "calls": 0, "key": kc["key"]}
        else:
            log("Giọng Gemini cần API key Gemini (cài trong mục AI) -> dùng giọng Edge")

    vn = None
    if tts == "vieneu":
        if VIENEU_PY.is_file():
            vn = {"map": {}, "main": vn_voice if any(vn_voice in v for v in VIENEU_VOICES.values()) else ""}
        else:
            log("Chưa cài VieNeu-TTS (thiếu thư mục .vieneu-venv) -> dùng giọng Edge")

    def series_voice(i, engine, taken):
        """Giọng nhân vật này đã dùng ở tập trước (hồ sơ phim), nếu chưa bị nhân vật khác trong tập này dùng."""
        name = segs[i].get("wname")
        v = ((series or {}).get("voices", {}).get(name) or {}).get(engine) if name else None
        return v if v and v not in taken else None

    def vn_item(i):
        key = who(i)
        if key not in vn["map"]:  # mỗi nhân vật một giọng riêng, hợp giới tính và tuổi (hoặc giọng của tập trước)
            vn["map"][key] = series_voice(i, "vieneu", vn["map"].values()) or vieneu_pick(key, list(vn["map"].values()))
        return (segs[i]["vi"].strip(), path[i], vn["map"][key])

    el = None
    if tts == "eleven":
        import eleven_tts

        kc = eleven_tts.load_config()
        if not kc:
            log("Giọng ElevenLabs cần API key ElevenLabs (dán ở mục Bộ đọc trên web) -> dùng giọng Edge")
        else:
            try:
                el = {"key": kc["key"], "model": el_model if el_model in eleven_tts.MODELS else "v3", "chars": 0,
                      "voices": eleven_tts.voices(kc["key"]), "map": {}}
            except Exception as e:
                log(f"{str(e)[:200]} -> dùng giọng Edge")

    def el_item(i):
        key = who(i)
        if key not in el["map"]:  # mỗi nhân vật một giọng riêng trong thư viện ElevenLabs của tài khoản
            vid = series_voice(i, "eleven", el["map"].values()) \
                or eleven_tts.pick_voice(el["voices"], key[0], key[1], set(el["map"].values()))
            el["map"][key] = vid or el.get("main") or (el["voices"][0]["id"] if el["voices"] else "")
        return (segs[i]["vi"].strip(), path[i], el["map"][key])

    gem_voice = {}

    def gem_item(i):
        g, age, k = segs[i].get("vkey") or ("male", "adult", 0)
        if (g, age, k) not in gem_voice:  # mỗi nhân vật một giọng riêng, không trùng nhân vật khác
            names = GEMINI_VOICES.get((g, age)) or GEMINI_VOICES[(g, "adult")]
            free = [n for n in names if n not in gem_voice.values()]
            gem_voice[(g, age, k)] = series_voice(i, "gemini", gem_voice.values()) \
                or (free[0] if free else names[k % len(names)])
        pace = _syllables(segs[i]["vi"]) / slot(i)  # âm tiết/giây cần đọc để kịp chỗ trống
        fast = "speaking very fast" if pace > 4.0 else "speaking quickly" if pace > 2.8 else ""
        style = ", ".join(x for x in (GEMINI_AGE_STYLE.get(age), segs[i].get("emo"), fast) if x)
        return (segs[i]["vi"].strip(), path[i], gem_voice[(g, age, k)], style)

    # nhân vật mà Gemini / ElevenLabs không đọc hết được -> cả video dùng Edge, không trộn 2 giọng
    edge_only = {tuple(k) for k in manifest.get("_edge_only") or []}

    def who(i):
        return segs[i].get("vkey") or ("male", "adult", 0)

    def synth(ids):
        """Tạo giọng cho các câu ids: Gemini (nếu chọn), nhưng mỗi nhân vật chỉ MỘT bộ đọc: Gemini thiếu dù một câu
        thì mọi câu của nhân vật đó đọc lại bằng Edge. Trả về danh sách câu vừa được tạo."""
        asked, reuse = list(ids), []
        for i in asked:
            m = manifest.get(str(i))
            if m and m.get("sig") == sig(i) and (tdir / m["file"]).is_file():
                path[i] = tdir / m["file"]
                reuse.append(i)
        if reuse:
            log(f"Dùng lại {len(reuse)} câu đã đọc ở lần chạy trước")
        ids = [i for i in asked if i not in reuse]
        rest = list(ids)
        if gem or el or vn:
            want = [i for i in ids if who(i) not in edge_only]
            got = set()
            if want and gem and gem["models"]:
                for i in want:
                    path[i] = tdir / f"{i:04}.wav"
                got = gemini_tts_all([gem_item(i) for i in want], gem["key"], gem)
            elif want and vn:
                for i in want:
                    path[i] = tdir / f"{i:04}.vn.wav"
                got = vieneu_speak_all([vn_item(i) for i in want])
            elif want and el and not el.get("dead"):
                for i in want:
                    path[i] = tdir / f"{i:04}.el.mp3"
                got = eleven_tts.speak_all([el_item(i) for i in want], el)
                if el.get("dead"):
                    log(el["dead"])
            lost = {who(i) for n, i in enumerate(want) if n not in got}
            if lost:
                edge_only.update(lost)
                log(f"{'Gemini' if gem else 'VieNeu' if vn else 'ElevenLabs'} không đọc hết được lời của {len(lost)} nhân vật -> nhân vật "
                    "đó dùng giọng Edge cho cả video (không trộn 2 giọng)")
            rest = [i for i in idx if who(i) in edge_only
                    and (i in ids or path[i].suffix == ".wav" or path[i].name.endswith(".el.mp3"))]
            for i in rest:
                path[i] = tdir / f"{i:04}.mp3"
        if rest:
            tts_all([(segs[i]["vi"].strip(), path[i], segs[i]["voice"]) for i in rest], MALE)
        for i in set(ids) | set(rest):
            if path[i].is_file():
                manifest[str(i)] = {"sig": sig(i), "file": path[i].name}
        save_manifest()
        return sorted(set(asked) | set(rest))

    def slot(i):  # thời gian có thể đọc: tới lúc câu sau bắt đầu
        nxt = segs[i + 1]["start"] if i + 1 < len(segs) else total
        return max(nxt - segs[i]["start"] - 0.05, segs[i]["end"] - segs[i]["start"], 0.3)

    if gem and gem_fixed and idx:  # giọng người dùng chọn -> dành cho người nói nhiều nhất (người dẫn / vai chính)
        keys = [who(i) for i in idx]
        main = max(set(keys), key=keys.count)
        gem_voice[main] = gem_fixed
        log(f"Giọng Gemini đã chọn: {gem_fixed}" + (" (cho nhân vật nói nhiều nhất)" if len(set(keys)) > 1 else ""))
    def shorten(ids, rate=VI_SYLLABLES_PER_SEC * 1.15):
        new = ai_shorten([(segs[i]["zh"], segs[i]["vi"], max(3, int(slot(i) * rate))) for i in ids], ai_cfg)
        book = manifest.setdefault("_short", {})
        for i, v in zip(ids, new):
            if v != segs[i]["vi"]:
                book[str(i)] = {"from": (book.get(str(i)) or {}).get("from", segs[i]["vi"].strip()), "to": v}
                segs[i]["vi"] = v
        save_manifest()

    if vn and idx:
        keys = [who(i) for i in idx]
        main = max(set(keys), key=keys.count)
        if vn["main"]:  # giọng người dùng chọn -> dành cho người nói nhiều nhất (người dẫn / vai chính)
            vn["map"][main] = vn["main"]
        for k in sorted(set(keys), key=lambda k: -keys.count(k)):  # gán theo thứ tự nói nhiều -> ít, rồi ghi rõ
            i0 = next(i for i in idx if who(i) == k)
            label = segs[i0].get("wname") or f"{'nam' if k[0] == 'male' else 'nữ'}, {k[1]} (chưa rõ tên)"
            log(f"  Giọng VieNeu cho {label}: {vn_item(i0)[2]} ({keys.count(k)} câu)")
        # VieNeu đọc chậm hơn Edge và đọc lại thì tốn thời gian máy -> rút gọn câu quá dài trước khi đọc
        # (đọc lại không tốn tiền nên chỉ rút gọn câu dài hẳn; câu hơi dài thì đọc nhanh lên để giữ con số, tên riêng)
        pre = [i for i in idx if _syllables(segs[i]["vi"]) / EL_SYLLABLES_PER_SEC / slot(i) > 1.5]
        if pre and ai_cfg and not manifest.get("_pre_done"):
            log(f"Rút gọn trước {len(pre)} câu dài...")
            try:
                shorten(pre, EL_SYLLABLES_PER_SEC * 1.3)
            except Exception as e:
                log(f"Không rút gọn trước được ({str(e)[:120]})")
        manifest["_pre_done"] = True
    if el and idx:
        keys = [who(i) for i in idx]
        main = max(set(keys), key=keys.count)
        names_el = {v["id"]: v["name"] for v in el["voices"]}
        if el_voice:  # giọng người dùng chọn -> dành cho người nói nhiều nhất (người dẫn / vai chính)
            el["map"][main] = el["main"] = el_voice
            log(f"Giọng ElevenLabs đã chọn: {names_el.get(el_voice, el_voice)}"
                + (" (cho nhân vật nói nhiều nhất)" if len(set(keys)) > 1 else ""))
        # ElevenLabs tính tiền theo ký tự và đọc chậm hơn Edge: rút gọn câu quá dài TRƯỚC khi đọc (ước lượng theo
        # tốc độ đọc của ElevenLabs), để gần như không phải trả tiền đọc lại lần hai
        pre = [i for i in idx if _syllables(segs[i]["vi"]) / EL_SYLLABLES_PER_SEC / slot(i) > 1.3]
        if pre and ai_cfg and not manifest.get("_pre_done"):
            log(f"Rút gọn trước {len(pre)} câu dài (đỡ tốn credit đọc lại)...")
            try:
                shorten(pre, EL_SYLLABLES_PER_SEC * 1.2)
            except Exception as e:
                log(f"Không rút gọn trước được ({str(e)[:120]})")
        manifest["_pre_done"] = True
    synth(idx)
    if gem:
        log(f"Giọng Gemini: {gem['calls']} lượt gọi")
    if el and el.get("note"):
        log(el["note"])
    clips = {i: decode_pcm(path[i]) for i in idx}

    # câu tiếng Việt dài hơn nhiều so với thời gian nói -> nhờ AI rút gọn rồi đọc lại. Dài hơn chút ít thì chỉ đọc
    # nhanh lên (giữ được ý). Với ElevenLabs chỉ đọc lại khi tăng tốc cũng không kịp, vì mỗi lần đọc lại là tốn credit.
    limit = max_speed if el else 1.3
    long = [i for i in idx if len(clips[i]) / SR / slot(i) > limit]
    if long and ai_cfg and not manifest.get("_post_done"):
        log(f"Rút gọn {len(long)} câu quá dài để đọc kịp...")
        try:
            shorten(long, (EL_SYLLABLES_PER_SEC if el else VI_SYLLABLES_PER_SEC) * 1.15)
            for i in synth(long):
                clips[i] = decode_pcm(path[i])
        except Exception as e:
            log(f"Không rút gọn được ({str(e)[:120]}), sẽ đọc nhanh hơn")
    manifest["_post_done"] = True
    save_manifest()

    if series is not None:  # ghi lại giọng từng nhân vật đã dùng -> tập sau dùng đúng giọng đó
        engine_map = {"vieneu": (vn or {}).get("map"), "eleven": (el or {}).get("map"), "gemini": gem_voice if gem else None}
        for engine, m in engine_map.items():
            for i in idx:
                name, key = segs[i].get("wname"), who(i)
                if m and name and key in m and m[key]:
                    series.setdefault("voices", {}).setdefault(name, {})[engine] = m[key]
    if el:
        cost = el["chars"] * (0.5 if el["model"] == "flash" else 1)
        log(f"Giọng ElevenLabs ({eleven_tts.MODELS[el['model']]}): đọc {el['chars']} ký tự, khoảng {cost:.0f} credit")
    track = np.zeros(int((total + 3) * SR), dtype=np.int32)
    fast = overlap = 0
    for i in idx:
        s, sl = segs[i], slot(i)
        clip = clips[i]
        need = len(clip) / SR / sl
        if need > 1.0:  # vẫn dài hơn chỗ trống -> đọc nhanh hơn (tối đa max_speed)
            clip = decode_pcm(path[i], tempo=min(need * 1.02, max_speed))
            fast += 1
        spoken = len(clip) / SR
        overlap += spoken > sl + 0.1
        pos = int(s["start"] * SR)
        end = min(pos + len(clip), len(track))
        track[pos:end] += clip[: end - pos]
        s["spoken"] = spoken
    if fast or overlap:
        log(f"{fast} câu phải đọc nhanh hơn, {overlap} câu vẫn dài hơn chỗ trống")
    voice_wav = workdir / "voice_vi.wav"
    with wave.open(str(voice_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.clip(track, -32768, 32767).astype(np.int16).tobytes())

    mixed = workdir / "mixed.m4a"
    if not has_audio(video):
        run(["ffmpeg", "-y", "-i", str(voice_wav), "-c:a", "aac", "-b:a", "192k", str(mixed)])
        return mixed
    # Chỉ vặn nhỏ tiếng gốc KHI giọng Việt đang nói (sidechain ducking);
    # lúc không có thoại thì giữ nguyên nhạc nền / tiếng động của video gốc.
    if bg_volume <= 0.01:
        bg = "[sc]anullsink;[orig]volume=0[bg]"
    else:
        import math

        reduce_db = -20 * math.log10(bg_volume)  # 0.15 -> giảm ~16 dB khi có giọng Việt
        ratio = min(20.0, max(1.0, 1 / max(0.05, 1 - reduce_db / 30)))
        bg = f"[orig][sc]sidechaincompress=threshold=0.003:ratio={ratio:.2f}:attack=15:release=350:makeup=1[bg]"
    mix_wav = workdir / "mix.wav"
    run([
        "ffmpeg", "-y", "-i", str(video), "-i", str(voice_wav), "-filter_complex",
        "[1:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[sc][vo];"
        "[0:a]aresample=48000,aformat=channel_layouts=stereo[orig];"
        f"{bg};[vo]volume=1.5[vo2];"
        "[bg][vo2]amix=inputs=2:duration=first:normalize=0[a]",
        "-map", "[a]", "-c:a", "pcm_s16le", str(mix_wav),
    ])
    normalize_audio(mix_wav, mixed)
    return mixed


def normalize_audio(src, dst, target=-14.0):
    """Đưa độ to tổng thể về -14 LUFS (chuẩn TikTok/YouTube/Douyin) bằng một mức tăng cố định + chặn đỉnh."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(src), "-af",
                        f"loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    gain = 0.0
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr)
    if m:
        try:
            measured = float(json.loads(m.group(0))["input_i"])
            if measured > -70:  # không phải im lặng
                gain = max(-12.0, min(12.0, target - measured))
        except (ValueError, KeyError):
            pass
    run(["ffmpeg", "-y", "-i", str(src), "-af", f"volume={gain:.2f}dB,alimiter=limit=0.79:level=false",
         "-c:a", "aac", "-b:a", "192k", str(dst)])


def fit_subtitle_times(segs, total):
    """Phụ đề hiện đủ lâu để đọc / khớp giọng lồng, nhưng không đè sang câu sau."""
    for i, s in enumerate(segs):
        nxt = segs[i + 1]["start"] if i + 1 < len(segs) else total
        want = max(s["end"], s["start"] + s.get("spoken", 0) + 0.2, s["start"] + 1.0)
        s["end"] = round(max(s["start"] + 0.3, min(want, nxt - 0.05)), 2)


# ------------------------------------------------------------- 5. Render
def video_size(video):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
               "-of", "json", str(video)])
    st = json.loads(out)["streams"][0]
    return int(st["width"]), int(st["height"])


def detect_hardsub_band(video, segs, samples=24):
    """Dò dải phụ đề in cứng có sẵn trong video gốc.

    Lấy khung hình giữa các câu thoại, tìm những hàng điểm ảnh ở nửa dưới có nhiều nét chữ sáng
    (sáng + tương phản mạnh theo chiều ngang) xuất hiện ở hơn một nửa số khung -> đó là dải chữ.
    Trả về (y0, y1) theo tỉ lệ chiều cao, hoặc None nếu không có.
    """
    import numpy as np

    w, h = video_size(video)
    W = 320
    H = max(2, int(round(h * W / w / 2)) * 2)
    mids = [(s["start"] + s["end"]) / 2 for s in segs]
    if len(mids) > samples:
        mids = [mids[int(k * len(mids) / samples)] for k in range(samples)]
    scores = []
    for t in mids:
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", f"scale={W}:{H},format=gray", "-f", "rawvideo", "-"], capture_output=True)
        if len(r.stdout) != W * H:
            continue
        f = np.frombuffer(r.stdout, dtype=np.uint8).reshape(H, W).astype(np.int16)
        # chữ phụ đề (mọi màu) luôn có viền/bóng tối -> rất nhiều cạnh tương phản mạnh theo chiều ngang
        edge = np.zeros((H, W), dtype=bool)
        edge[:, 1:] = np.abs(np.diff(f, axis=1)) > 70
        scores.append(edge.sum(axis=1) / W)
    if len(scores) < 3:
        return None
    med = np.median(np.array(scores), axis=0)  # hàng có chữ ở hơn nửa số khung hình
    rows = [y for y in np.where(med > 0.03)[0] if y > H * 0.55]
    if not rows:
        return None
    clusters, a, b = [], rows[0], rows[0]
    for y in rows[1:]:
        if y - b > 3:
            clusters.append((a, b))
            a = y
        b = y
    clusters.append((a, b))
    y0, y1 = max(clusters, key=lambda c: float(med[c[0]:c[1] + 1].sum()))
    band = y1 - y0 + 1
    if band < H * 0.012 or band > H * 0.2:
        return None
    pad = max(2, int(band * 0.45))
    return max(0, y0 - pad) / H, min(H, y1 + pad + 1) / H


def speech_intervals(segs, before=0.3, after=0.8, join=1.2):
    """Khoảng thời gian có thoại (phụ đề Trung in cứng thường hiện trong lúc này), đã gộp các khoảng sát nhau."""
    out = []
    for s in segs:
        a, b = max(0.0, s["start"] - before), s["end"] + after
        if out and a - out[-1][1] < join:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def srt_to_ass(srt_path, ass_path, top, size=64):
    """Đổi phụ đề .srt sang .ass cho khung dọc 1080x1920: chữ to, canh giữa, mép trên của chữ nằm ở toạ độ y = top."""
    def ts(t):  # 00:01:02,345 -> 0:01:02.34
        h, m, rest = t.strip().split(":")
        sec, ms = rest.split(",")
        return f"{int(h)}:{m}:{sec}.{ms[:2]}"

    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n"
            "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            f"Style: Default,Arial,{size},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,5,2,8,"
            f"60,60,{int(top)},1\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    out = []
    for block in re.split(r"\r?\n\r?\n", Path(srt_path).read_text(encoding="utf-8").strip()):
        lines = block.splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            a, b = lines[1].split("-->")
            text = " ".join(x.strip() for x in lines[2:]).replace("{", "(").replace("}", ")")
            out.append(f"Dialogue: 0,{ts(a)},{ts(b)},Default,,0,0,0,,{text}")
    Path(ass_path).write_text(head + "\n".join(out) + "\n", encoding="utf-8")


def vertical_box(w, h):
    """Vị trí (y, cao) của video ngang khi đặt vào khung dọc 1080x1920 (hơi lệch lên trên, chừa chỗ cho phụ đề)."""
    vh = int(1080 * h / w / 2) * 2
    vy = max(420, (1920 - vh) // 2 - 96) // 2 * 2
    return vy, vh


def render(video, srt_name, audio, out_path, workdir, font_size, burn, band=None, speech=None, vertical=None, logo=None):
    """Xuất video. Không có gì chạm vào hình thì sao chép luồng hình (nhanh, giữ nguyên chất lượng).
    vertical: None hoặc {"title_png": file | None} -> khung dọc 1080x1920: nền mờ, video ở giữa, tiêu đề phía trên,
              phụ đề Việt chữ to phía dưới (không đè lên hình gốc).
    logo:     None hoặc {"path", "pos": "tl|tr|bl|br", "size": % bề ngang video} -> chèn logo kênh vào góc video."""
    cmd = ["ffmpeg", "-y", "-i", str(video)]
    n_in = 1
    if audio:
        cmd += ["-i", str(audio)]
        n_in += 1
    w, h = video_size(video)
    graph, cur = [], "[0:v]"
    margin_v = 40
    if burn and band:
        y = int(h * band[0]) // 2 * 2
        bh = max(2, (int(h * band[1]) - y) // 2 * 2)
        # chỉ che khi đang có thoại -> cảnh không thoại không bị vệt tối vắt ngang
        # (quá nhiều đoạn thì biểu thức quá dài -> che suốt video)
        enable = ""
        if speech and len(speech) <= 400:
            enable = ":enable='" + "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in speech) + "'"
        # làm mờ mạnh + phủ tối dải chữ Trung in cứng (không còn bóng chữ), rồi đặt phụ đề Việt đè lên
        graph.append(f"{cur}split[base][cut];[cut]crop=iw:{bh}:0:{y},gblur=sigma=30:steps=3,"
                     f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.72:t=fill[blur];"
                     f"[base][blur]overlay=0:{y}{enable}[cov]")
        cur = "[cov]"
        margin_v = max(4, int((h - y - bh) / h * 288) + 2)
    box = (0, 0, w, h)  # vùng hình gốc trong khung xuất (x, y, rộng, cao)
    if vertical:
        vy, vh = vertical_box(w, h)
        graph.append(f"{cur}split=2[vb][vf];"
                     f"[vb]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=8,"
                     f"scale=1080:1920,eq=brightness=-0.2:saturation=0.9,"
                     f"drawbox=x=0:y={vy - 8}:w=1080:h={vh + 16}:color=0xFFD60A:t=fill[bg];"
                     f"[vf]scale=1080:{vh}[fg];[bg][fg]overlay=0:{vy}[vv]")
        cur, box = "[vv]", (0, vy, 1080, vh)
        if vertical.get("title_png"):
            cmd += ["-i", str(vertical["title_png"])]
            graph.append(f"{cur}[{n_in}:v]overlay=0:0[vt]")
            cur, n_in = "[vt]", n_in + 1
    if logo:
        cmd += ["-i", str(logo["path"])]
        lw = max(40, int(box[2] * float(logo.get("size", 12)) / 100) // 2 * 2)
        pad = int(box[2] * 0.02)
        pos = logo.get("pos", "tr")
        x = str(box[0] + pad) if pos[1] == "l" else f"{box[0] + box[2] - pad}-w"
        yy = str(box[1] + pad) if pos[0] == "t" else f"{box[1] + box[3] - pad}-h"
        graph.append(f"[{n_in}:v]scale={lw}:-1[lg];{cur}[lg]overlay={x}:{yy}[vl]")
        cur, n_in = "[vl]", n_in + 1
    if vertical:
        # phụ đề nằm DƯỚI khung hình (không đè lên hình gốc): file .ass riêng, toạ độ tính theo điểm ảnh
        ass = Path(srt_name).with_suffix(".ass").name
        srt_to_ass(Path(workdir) / srt_name, Path(workdir) / ass, box[1] + box[3] + 44)
        graph.append(f"{cur}subtitles={ass}[v]")
        cur = "[v]"
    elif burn:
        style = (
            f"FontName=Arial,FontSize={font_size},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            f"BackColour=&H99000000,BorderStyle=3,Outline=2,Shadow=0,Alignment=2,MarginV={margin_v},Bold=1"
        )
        # chạy trong workdir để tránh lỗi đường dẫn Windows trong filter subtitles
        graph.append(f"{cur}subtitles={srt_name}:force_style='{style}'[v]")
        cur = "[v]"
    if graph:
        cmd += ["-filter_complex", ";".join(graph), "-map", cur,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-map", "0:v:0", "-c:v", "copy"]
    cmd += ["-map", "1:a:0" if audio else "0:a:0?"]
    if audio:
        cmd += ["-c:a", "copy"]
    cmd += ["-movflags", "+faststart", str(out_path)]
    run(cmd, cwd=workdir)


def load_profile():
    """Hồ sơ kênh lưu trong config.json: tên kênh, hashtag mặc định, logo (profile/logo.png) và vị trí đặt."""
    try:
        pr = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("profile") or {}
    except Exception:
        pr = {}
    out = {"channel": str(pr.get("channel") or "").strip(),
           "hashtags": [t for t in re.split(r"[\s,#]+", str(pr.get("hashtags") or "")) if t], "logo": None}
    logo = CONFIG_PATH.parent / "profile" / "logo.png"
    if pr.get("logo_on") and logo.is_file():
        pos = pr.get("logo_pos") if pr.get("logo_pos") in ("tl", "tr", "bl", "br") else "tr"
        try:
            size = min(30.0, max(5.0, float(pr.get("logo_size", 12))))
        except (TypeError, ValueError):
            size = 12.0
        out["logo"] = {"path": logo, "pos": pos, "size": size}
    return out


CACHE_DIR = Path(__file__).resolve().parent / "output" / "_cache"
LAST_TRANSLATOR = ""  # ai đã dịch lần gần nhất: "gemini/model..." hoặc "google"


def video_key(path):
    """Mã nhận diện video theo nội dung (kích thước + 4 MB đầu + 4 MB cuối): cùng file thì cùng mã, dù đổi tên."""
    import hashlib

    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with open(p, "rb") as f:
        h.update(f.read(4 << 20))
        if size > (8 << 20):
            f.seek(size - (4 << 20))
            h.update(f.read(4 << 20))
    return h.hexdigest()[:20]


def _jload(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _jdump(path, obj):
    def enc(o):  # số numpy, Path, tuple lạ...
        try:
            return float(o)
        except (TypeError, ValueError):
            return str(o)

    Path(path).write_text(json.dumps(obj, ensure_ascii=False, default=enc), encoding="utf-8")


def rename_outputs(outdir, base, vi_base, out_path, video):
    """Đổi tên các file kết quả "<base>.*" thành "<vi_base>.*" (ghi đè bản cũ cùng tên khi xuất lại)."""
    if not vi_base or vi_base == base:
        return out_path, video
    video = Path(video)
    for p in list(Path(outdir).glob("*")):
        if p.is_file() and p.name.startswith(base + "."):
            new = p.with_name(vi_base + p.name[len(base):])
            try:
                os.replace(p, new)
            except OSError:
                continue
            if p == Path(out_path):
                out_path = new
            if p == video:
                video = new
    return out_path, video


def vi_file_name(title, fallback, part=""):
    """Tên file từ tiêu đề Việt: bỏ ký tự Windows cấm, cắt ở ranh giới từ (<= 80 ký tự), giữ số phần."""
    s = re.sub(r"[#@].*", "", title or "")
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    n = (part or "").split("/")[0].strip()
    tail = f" - Phần {n}" if n and not re.search(rf"Phần\s*{re.escape(n)}", s) else ""
    limit = 80 - len(tail)
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(" ,.;-–—")
    return (s + tail).strip() or fallback


def safe_name(s, fallback):
    s = re.sub(r"[#@].*", "", s or "").strip()
    s = re.sub(r'[\\/:*?"<>|\r\n]+', " ", s)[:50].strip()
    return s or fallback


def main():
    ap = argparse.ArgumentParser(description="Tải video Douyin + phụ đề & lồng tiếng Việt")
    ap.add_argument("source", help="Link Douyin (có thể dán cả đoạn chia sẻ) hoặc đường dẫn file video")
    ap.add_argument("-o", "--outdir", default="output", help="Thư mục lưu kết quả (mặc định: output)")
    ap.add_argument("--model", default="medium", help="Whisper model: small (nhanh) / medium (chính xác, mặc định) / large-v3")
    ap.add_argument("--lang", default="zh", help="Ngôn ngữ gốc (mặc định zh)")
    ap.add_argument("--voice", default="auto",
                    help="auto (mặc định): mỗi nhân vật một giọng theo giới tính/tuổi; nam / nu: một giọng cho cả video")
    ap.add_argument("--eleven-voice", default="", help="ID giọng ElevenLabs cho người nói nhiều nhất (trống = tự chọn)")
    ap.add_argument("--eleven-model", choices=["v3", "flash"], default="v3",
                    help="Model ElevenLabs: v3 (hay nhất, 1 credit/ký tự) / flash (0,5 credit/ký tự)")
    ap.add_argument("--vieneu-voice", default="", help="Tên giọng VieNeu cho người nói nhiều nhất (trống = tự chọn)")
    ap.add_argument("--tts", choices=["edge", "gemini", "eleven", "vieneu"], default="edge",
                    help="Bộ đọc: edge (miễn phí, 2 giọng) / gemini (giọng AI nhiều giọng, có cảm xúc; dùng key Gemini)")
    ap.add_argument("--gemini-voice", default="", choices=[""] + GEMINI_PICK,
                    help="Giọng Gemini cố định cho người nói nhiều nhất (bỏ trống = tự chọn theo nhân vật)")
    ap.add_argument("--bg-volume", type=float, default=0.15, help="Âm lượng tiếng gốc khi lồng tiếng (0-1, mặc định 0.15)")
    ap.add_argument("--max-speed", type=float, default=1.5, help="Tốc độ đọc tối đa khi câu dịch dài (mặc định 1.5x)")
    ap.add_argument("--font-size", type=int, default=13, help="Cỡ chữ phụ đề")
    ap.add_argument("--no-dub", action="store_true", help="Chỉ làm phụ đề, giữ tiếng gốc")
    ap.add_argument("--subs", action="store_true",
                    help="Ghép cứng phụ đề Việt vào video. Mặc định KHÔNG ghép: giữ nguyên hình gốc (kể cả phụ đề Trung), "
                         "chỉ thay tiếng bằng giọng Việt; file .srt vẫn được xuất")
    ap.add_argument("--no-burn", action="store_true", help=argparse.SUPPRESS)  # giữ cho lệnh cũ, nay là mặc định
    ap.add_argument("--cover-subs", choices=["auto", "off"], default="auto",
                    help="Khi dùng --subs: tự dò và làm mờ phụ đề Trung in cứng (mặc định auto)")
    ap.add_argument("--no-cover", action="store_true", help="Không tạo ảnh bìa + tiêu đề TikTok")
    ap.add_argument("--part", default="", help="Video là phần k/n của một video dài, ví dụ 2/7 (ghi lên ảnh bìa và tiêu đề)")
    ap.add_argument("--title-hint", default="", help="Tiêu đề gốc của video (khi nguồn là file không có tiêu đề)")
    ap.add_argument("--no-ai", action="store_true", help="Không dịch bằng AI, dùng Google Dịch")
    ap.add_argument("--cookies-from-browser", help="Dùng cookie trình duyệt cho yt-dlp nếu cần (chrome/edge/firefox)")
    ap.add_argument("--vertical", action="store_true",
                    help="Video ngang -> xuất khung dọc 9:16 cho TikTok (nền mờ, tiêu đề trên, phụ đề Việt dưới)")
    ap.add_argument("--confirm-dub", action="store_true",
                    help="Dừng sau bước dịch để duyệt bản dịch + chi phí; chạy lại lệnh (không có cờ này) để lồng tiếng tiếp")
    ap.add_argument("--no-cache", action="store_true",
                    help="Không dùng (và không lưu) lời nghe / bản dịch đã nhớ của video này")
    ap.add_argument("--retranslate", action="store_true", help="Vẫn dùng lại lời đã nghe nhưng dịch lại từ đầu")
    ap.add_argument("--asr", choices=["local", "eleven"], default="local",
                    help="Nghe tiếng Trung: local (trên máy, miễn phí) / eleven (đám mây ElevenLabs, nhanh, tốn credit)")
    ap.add_argument("--beam", type=int, default=2, help="Độ kỹ khi nghe trên máy (1-5; nhỏ hơn = nhanh hơn)")
    ap.add_argument("--series", default="", help="Tên bộ phim: giữ tên nhân vật, xưng hô và giọng giống nhau giữa các tập")
    ap.add_argument("--fresh", action="store_true", help="Bỏ kết quả dở của lần chạy trước, làm lại từ đầu")
    ap.add_argument("--no-profile", action="store_true", help="Không áp dụng hồ sơ kênh (logo, hashtag, tên kênh)")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("Chưa cài ffmpeg (winget install Gyan.FFmpeg)")

    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    # Thư mục làm việc nằm trong thư mục kết quả và ĐƯỢC GIỮ LẠI khi lỗi / tạm dừng: lần chạy sau với cùng nguồn sẽ
    # dùng lại video đã tải, lời đã nghe, bản dịch và các câu đã đọc -> không làm lại, không tốn credit lần nữa.
    state = outdir / "_resume"
    if args.fresh:
        shutil.rmtree(state, ignore_errors=True)
    state.mkdir(exist_ok=True)
    workdir = state
    profile = {"channel": "", "hashtags": [], "logo": None} if args.no_profile else load_profile()
    series = load_series(args.series.strip()) if args.series.strip() else None
    finished = False
    try:
        src = _jload(state / "source.json")
        if src and src.get("source") == args.source and Path(src.get("video") or "_").is_file()                 and has_audio(src["video"]):  # bản tải lỗi trước đây (không có tiếng) thì tải lại
            video, title, base = Path(src["video"]), src.get("title", ""), src["base"]
            log("Chạy tiếp: dùng lại video đã tải")
        else:
            video, title = download(args.source, workdir, args.cookies_from_browser)
            base = safe_name(title, f"douyin_{os.getpid()}")
            _jdump(state / "source.json", {"source": args.source, "video": str(video), "title": title, "base": base})
        total = media_duration(video)
        cdir = None
        if not args.no_cache:
            try:
                cdir = CACHE_DIR / video_key(video)
                cdir.mkdir(parents=True, exist_ok=True)
            except Exception:
                cdir = None

        asr = _jload(state / "segs_asr.json")
        if asr and asr.get("key") == [args.model, args.lang] and asr.get("segs") and (state / "audio16k.wav").is_file():
            segs = asr["segs"]
            log(f"Chạy tiếp: dùng lại {len(segs)} câu đã nghe được")
        else:
            # Bộ nhớ theo nội dung video: video này từng được nghe (cùng chế độ nghe) thì khỏi nghe lại
            cached = _jload(cdir / f"asr_{args.model}_{args.lang}.json") if cdir else None
            if cached and cached.get("segs"):
                segs = cached["segs"]
                run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(state / "audio16k.wav")])
                log(f"Video này đã được nghe trước đây -> dùng lại {len(segs)} câu, bỏ qua bước nghe tiếng Trung")
            else:
                segs = transcribe(video, workdir, args.model, args.lang, title, engine=args.asr,
                                  beam=max(1, min(5, args.beam)))
                if not segs:
                    sys.exit("Không nhận dạng được lời thoại nào trong video.")
                if cdir:
                    _jdump(cdir / f"asr_{args.model}_{args.lang}.json", {"segs": segs, "title": title})
            _jdump(state / "segs_asr.json", {"key": [args.model, args.lang], "segs": segs})

        ai_cfg = None if args.no_ai else load_ai_config()
        if not ai_cfg and not args.no_ai:
            log("Chưa cài API key AI -> dịch bằng Google Dịch (kém tự nhiên hơn). Xem README để bật dịch AI.")
        tr = _jload(state / "segs_vi.json")
        if tr and len(tr.get("segs") or []) == len(segs):
            segs, chars = tr["segs"], tr.get("chars") or []
            log(f"Chạy tiếp: dùng lại bản dịch {len(segs)} câu")
        else:
            vfile = cdir / f"vi_{args.model}_{args.lang}.json" if cdir else None
            cached = _jload(vfile) if vfile and not args.retranslate else None
            # chỉ dùng lại bản dịch do AI dịch; bản Google Dịch (dự phòng khi AI lỗi) thì dịch lại nếu giờ đã có AI
            if cached and len(cached.get("segs") or []) == len(segs) and (cached.get("by") != "google" or not ai_cfg):
                segs, chars = cached["segs"], cached.get("chars") or []
                log(f"Video này đã được dịch trước đây ({cached.get('by') or '?'}) -> dùng lại bản dịch {len(segs)} câu. "
                    "Muốn dịch lại thì bật \"Dịch lại từ đầu\"")
            else:
                chars, _ = translate(segs, args.lang, title, total, ai_cfg, series=series)
                save_series(series)
                old = _jload(vfile) if vfile else None
                # không để bản Google Dịch ghi đè một bản AI đã lưu
                if vfile and not (LAST_TRANSLATOR == "google" and old and old.get("by") != "google"):
                    _jdump(vfile, {"segs": segs, "chars": chars, "by": LAST_TRANSLATOR})
            _jdump(state / "segs_vi.json", {"segs": segs, "chars": chars})

        audio = None
        if not args.no_dub:
            est = dub_estimate(segs, args.tts, args.eleven_model)
            log("Ước tính lồng tiếng: " + estimate_text(est))
            if args.confirm_dub:
                write_srt(segs, outdir / f"{base}.vi.srt", "vi")
                write_srt(segs, outdir / f"{base}.zh.srt", "zh")
                print("ESTIMATE_JSON: " + json.dumps(est, ensure_ascii=False), flush=True)
                log("Tạm dừng: chờ bạn duyệt bản dịch và chi phí rồi bấm lồng tiếng")
                return
            dkey = [args.tts, args.voice, args.gemini_voice, args.eleven_voice, args.eleven_model, args.bg_volume,
                    args.vieneu_voice, args.series]
            dubbed = _jload(state / "segs_dub.json")
            if dubbed and dubbed.get("key") == dkey and len(dubbed.get("segs") or []) == len(segs) \
                    and Path(dubbed.get("audio") or "_").is_file():
                segs, audio = dubbed["segs"], Path(dubbed["audio"])
                log("Chạy tiếp: dùng lại phần tiếng đã lồng")
            else:
                if args.voice == "auto":
                    assign_voices(segs, chars, workdir / "audio16k.wav")
                audio = dub(segs, video, workdir, VOICES.get(args.voice, args.voice), args.bg_volume, args.max_speed,
                            ai_cfg, tts=args.tts, gem_fixed=args.gemini_voice,
                            el_voice=args.eleven_voice, el_model=args.eleven_model, vn_voice=args.vieneu_voice,
                            series=series)
                save_series(series)
                _jdump(state / "segs_dub.json", {"key": dkey, "segs": segs, "audio": str(audio)})
        fit_subtitle_times(segs, total)

        write_srt(segs, workdir / "sub.srt", "vi")
        write_srt(segs, outdir / f"{base}.vi.srt", "vi")
        write_srt(segs, outdir / f"{base}.zh.srt", "zh")

        burn = args.subs and not args.no_burn
        band = None
        if burn and args.cover_subs == "auto":
            band = detect_hardsub_band(video, segs)
            log(f"Phát hiện phụ đề in cứng ở {band[0]:.0%}-{band[1]:.0%} chiều cao -> làm mờ" if band
                else "Không thấy phụ đề in cứng trong video gốc")

        # tiêu đề + chữ ảnh bìa: cần trước khi xuất vì khung dọc ghi tiêu đề lên video
        meta = None
        if True:  # luôn cần: tiêu đề Việt dùng làm tên video / tên file
            try:
                import cover

                meta = _jload(state / "meta.json")
                if not meta:
                    log("Viết tiêu đề và hashtag TikTok...")
                    meta = cover.write_meta(segs, args.title_hint or title, ai_cfg, args.part, profile["hashtags"])
                    _jdump(state / "meta.json", meta)
            except Exception as e:
                log(f"Không viết được tiêu đề ({str(e)[:150]})")

        vertical = None
        if args.vertical:
            w, h = video_size(video)
            if w > h * 1.15:
                vertical = {"title_png": None}
                try:
                    if meta:
                        cover.title_overlay(workdir / "title.png", meta["cover"], vertical_box(w, h)[0], args.part)
                        vertical["title_png"] = workdir / "title.png"
                except Exception as e:
                    log(f"Không vẽ được tiêu đề lên khung dọc ({str(e)[:120]})")
            else:
                log("Video gốc đã là khung dọc -> giữ nguyên khung hình")

        out_path = outdir / f"{base}.vi.mp4"
        log("Đang xuất video (khung dọc 9:16 cho TikTok)..." if vertical
            else "Đang xuất video..." if (burn or profile["logo"])
            else "Đang xuất video (giữ nguyên hình gốc, chỉ thay tiếng)...")
        render(video, "sub.srt", audio, out_path, workdir, args.font_size, burn, band, speech_intervals(segs),
               vertical=vertical, logo=profile["logo"])
        if not args.no_cover and meta:
            log("Tạo ảnh bìa TikTok...")
            try:
                try:
                    cband = band or detect_hardsub_band(video, segs)
                except Exception:
                    cband = None
                cover.build(video, segs, args.title_hint or title, ai_cfg, outdir, base, total, band=cband,
                            part=args.part, meta=meta, channel=profile["channel"])
            except Exception as e:
                log(f"Không tạo được ảnh bìa ({str(e)[:150]})")
        if Path(video).parent == workdir:
            shutil.move(str(video), str(outdir / f"{base}.goc.mp4"))
            video = outdir / f"{base}.goc.mp4"
        # đặt tên file theo tiêu đề tiếng Việt (tên gốc tiếng Trung khó đọc khi lưu / tìm lại)
        vi_title = re.sub(r"\s+", " ", (meta or {}).get("title") or "").strip()
        if vi_title:
            log(f"Tiêu đề Việt: {vi_title}")
            vi_base = vi_file_name(vi_title, base, args.part)
            out_path, video = rename_outputs(outdir, base, vi_base, out_path, video)
        _jdump(state / "source.json", {"source": args.source, "video": str(video), "title": title, "base": base})
        log(f"XONG! -> {out_path}")
        finished = True
    finally:
        if finished:
            # Giữ lại phần nhỏ (lời nghe, bản dịch, các câu đã đọc) để SỬA TỪNG CÂU sau này: chỉ đọc lại câu bị sửa.
            # Bỏ các file tiếng lớn, lần sau tạo lại từ các câu đã đọc trong vài giây.
            # giữ mixed.m4a (tiếng đã lồng, vài MB): đổi khung ngang/dọc sau này chỉ cần xuất lại, không lồng tiếng lại
            for name in ("audio16k_pad.wav", "voice_vi.wav", "mix.wav", "sub.ass"):
                (state / name).unlink(missing_ok=True)
            for f in (state / "tts").glob("vieneu_jobs.json"):
                f.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
