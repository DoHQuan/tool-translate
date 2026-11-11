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
    cmd = [sys.executable, "-m", "yt_dlp", "-f", "mp4/best", "-o", str(out_path), "--no-playlist", "--force-overwrites"]
    if cookies_browser:
        cmd += ["--cookies-from-browser", cookies_browser]
    run(cmd + [url])
    return ""


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
        download_ytdlp(url, out, cookies_browser)
    log(f"Đã tải xong ({os.path.getsize(out) / 1e6:.1f} MB)")
    return out, title


# ------------------------------------------------------------ 2. Transcribe
def transcribe(video, workdir, model_name, lang, hint=""):
    from faster_whisper import WhisperModel

    wav = workdir / "audio16k.wav"  # bản gốc (dùng để đo cao độ giọng)
    run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    # Whisper hay bỏ sót lời nói nằm ngay giây 0 -> đệm 1 giây im lặng ở đầu, rồi trừ lại ở mọi mốc thời gian
    PAD = 1.0
    wav_pad = workdir / "audio16k_pad.wav"
    run(["ffmpeg", "-y", "-i", str(wav), "-af", f"adelay={int(PAD * 1000)}", "-ac", "1", "-ar", "16000", str(wav_pad)])
    log(f"Nhận dạng giọng nói (model {model_name}, lần đầu sẽ tải model)...")
    model = WhisperModel(model_name, device="auto", compute_type="int8")
    segments, info = model.transcribe(
        str(wav_pad), language=lang, vad_filter=True, beam_size=5,
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
}
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
    if provider in AI_DEFAULT_MODELS and len(key) > 10:
        return {"provider": provider, "key": key, "model": model}
    return None


GEMINI_FALLBACKS = ["gemini-3.5-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]


def ai_chat(cfg, system, user, timeout=90):
    """Gọi LLM, trả về chuỗi JSON. Với Gemini: model đang quá tải / treo / không có -> tự chuyển model khác
    và nhớ model chạy được cho các lần gọi sau."""
    models = [cfg["model"]]
    if cfg["provider"] == "gemini":  # model đang dùng trước, quá tải thì xoay sang model khác (mỗi lần gọi)
        models += [m for m in GEMINI_FALLBACKS if m != cfg["model"]]
    import time

    last = None
    for wait in (0, 10, 30):  # mọi model đều quá tải -> chờ rồi thử lại cả vòng
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
    r = requests.post(url, timeout=timeout, headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "temperature": 0.3, "response_format": {"type": "json_object"},
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
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
- MỖI CÂU PHẢI ĐỌC KỊP trong thời gian cho phép: không vượt quá số âm tiết "max" của câu đó. Câu dài thì rút gọn, giữ ý chính.
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


def ai_analyze(segs, cfg, title, chunk=150):
    """AI xác định nhân vật, người nói từng câu và bảng xưng hô cố định. Trả về (characters, address)."""
    log("AI phân tích nhân vật, người nói và xưng hô...")
    chars, address = [], []
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


def _budget(segs, i, total):
    nxt = segs[i + 1]["start"] if i + 1 < len(segs) else total
    avail = max(segs[i]["end"] - segs[i]["start"], min(nxt - segs[i]["start"], segs[i]["end"] - segs[i]["start"] + 1.5))
    return max(3, int(avail * VI_SYLLABLES_PER_SEC))


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
    user = ("Các câu lồng tiếng sau quá dài so với thời gian nói. Viết lại NGẮN HƠN, giữ ý chính và giọng văn, "
            "không vượt quá số âm tiết 'max':\n"
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


def translate(segs, src_lang, title="", total=0, ai_cfg=None):
    """Dịch; trả về (characters, address) nếu AI phân tích được nhân vật (dùng để chọn giọng)."""
    chars, address = [], []
    if ai_cfg:
        try:
            chars, address = ai_analyze(segs, ai_cfg, title)
            fix_speakers(segs, chars)
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
            return chars, address
        except Exception as e:
            log(f"Dịch bằng AI lỗi ({str(e)[:150]}) -> dùng Google Dịch")
    _google_translate(segs, src_lang)
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
        el_voice="", el_model="v3"):
    import wave

    import numpy as np

    total = media_duration(video)
    multi = voice == "auto"
    log("Lồng tiếng (nhiều giọng theo nhân vật)..." if multi else f"Lồng tiếng ({voice})...")
    tdir = workdir / "tts"
    tdir.mkdir(exist_ok=True)
    idx = [i for i, s in enumerate(segs) if s.get("vi", "").strip()]
    path = {i: tdir / f"{i:04}.mp3" for i in idx}
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
            vid = eleven_tts.pick_voice(el["voices"], key[0], key[1], set(el["map"].values()))
            el["map"][key] = vid or el.get("main") or (el["voices"][0]["id"] if el["voices"] else "")
        return (segs[i]["vi"].strip(), path[i], el["map"][key])

    gem_voice = {}

    def gem_item(i):
        g, age, k = segs[i].get("vkey") or ("male", "adult", 0)
        if (g, age, k) not in gem_voice:  # mỗi nhân vật một giọng riêng, không trùng nhân vật khác
            names = GEMINI_VOICES.get((g, age)) or GEMINI_VOICES[(g, "adult")]
            free = [n for n in names if n not in gem_voice.values()]
            gem_voice[(g, age, k)] = free[0] if free else names[k % len(names)]
        pace = _syllables(segs[i]["vi"]) / slot(i)  # âm tiết/giây cần đọc để kịp chỗ trống
        fast = "speaking very fast" if pace > 4.0 else "speaking quickly" if pace > 2.8 else ""
        style = ", ".join(x for x in (GEMINI_AGE_STYLE.get(age), segs[i].get("emo"), fast) if x)
        return (segs[i]["vi"].strip(), path[i], gem_voice[(g, age, k)], style)

    edge_only = set()  # nhân vật mà Gemini không đọc hết được -> cả video dùng Edge, không trộn 2 giọng

    def who(i):
        return segs[i].get("vkey") or ("male", "adult", 0)

    def synth(ids):
        """Tạo giọng cho các câu ids: Gemini (nếu chọn), nhưng mỗi nhân vật chỉ MỘT bộ đọc: Gemini thiếu dù một câu
        thì mọi câu của nhân vật đó đọc lại bằng Edge. Trả về danh sách câu vừa được tạo."""
        rest = list(ids)
        if gem or el:
            want = [i for i in ids if who(i) not in edge_only]
            got = set()
            if want and gem and gem["models"]:
                for i in want:
                    path[i] = tdir / f"{i:04}.wav"
                got = gemini_tts_all([gem_item(i) for i in want], gem["key"], gem)
            elif want and el and not el.get("dead"):
                for i in want:
                    path[i] = tdir / f"{i:04}.el.mp3"
                got = eleven_tts.speak_all([el_item(i) for i in want], el)
                if el.get("dead"):
                    log(el["dead"])
            lost = {who(i) for n, i in enumerate(want) if n not in got}
            if lost:
                edge_only.update(lost)
                log(f"{'Gemini' if gem else 'ElevenLabs'} không đọc hết được lời của {len(lost)} nhân vật -> nhân vật "
                    "đó dùng giọng Edge cho cả video (không trộn 2 giọng)")
            rest = [i for i in idx if who(i) in edge_only
                    and (i in ids or path[i].suffix == ".wav" or path[i].name.endswith(".el.mp3"))]
            for i in rest:
                path[i] = tdir / f"{i:04}.mp3"
        if rest:
            tts_all([(segs[i]["vi"].strip(), path[i], segs[i]["voice"]) for i in rest], MALE)
        return sorted(set(ids) | set(rest))

    def slot(i):  # thời gian có thể đọc: tới lúc câu sau bắt đầu
        nxt = segs[i + 1]["start"] if i + 1 < len(segs) else total
        return max(nxt - segs[i]["start"] - 0.05, segs[i]["end"] - segs[i]["start"], 0.3)

    if gem and gem_fixed and idx:  # giọng người dùng chọn -> dành cho người nói nhiều nhất (người dẫn / vai chính)
        keys = [who(i) for i in idx]
        main = max(set(keys), key=keys.count)
        gem_voice[main] = gem_fixed
        log(f"Giọng Gemini đã chọn: {gem_fixed}" + (" (cho nhân vật nói nhiều nhất)" if len(set(keys)) > 1 else ""))
    def shorten(ids):
        new = ai_shorten([(segs[i]["zh"], segs[i]["vi"], max(3, int(slot(i) * VI_SYLLABLES_PER_SEC))) for i in ids], ai_cfg)
        for i, v in zip(ids, new):
            segs[i]["vi"] = v

    if el and idx:
        keys = [who(i) for i in idx]
        main = max(set(keys), key=keys.count)
        ids = {v["id"]: v["name"] for v in el["voices"]}
        if el_voice:  # giọng người dùng chọn -> dành cho người nói nhiều nhất (người dẫn / vai chính)
            el["map"][main] = el["main"] = el_voice
            log(f"Giọng ElevenLabs đã chọn: {ids.get(el_voice, el_voice)}"
                + (" (cho nhân vật nói nhiều nhất)" if len(set(keys)) > 1 else ""))
        # ElevenLabs tính tiền theo ký tự: rút gọn câu quá dài TRƯỚC khi đọc, khỏi phải trả tiền đọc hai lần
        pre = [i for i in idx if _syllables(segs[i]["vi"]) / VI_SYLLABLES_PER_SEC / slot(i) > 1.2]
        if pre and ai_cfg:
            log(f"Rút gọn trước {len(pre)} câu dài (đỡ tốn credit đọc lại)...")
            try:
                shorten(pre)
            except Exception as e:
                log(f"Không rút gọn trước được ({str(e)[:120]})")
    synth(idx)
    if gem:
        log(f"Giọng Gemini: {gem['calls']} lượt gọi")
    if el and el.get("note"):
        log(el["note"])
    clips = {i: decode_pcm(path[i]) for i in idx}

    # câu tiếng Việt dài hơn nhiều so với thời gian nói -> nhờ AI rút gọn rồi đọc lại
    long = [i for i in idx if len(clips[i]) / SR / slot(i) > 1.2]
    if long and ai_cfg:
        log(f"Rút gọn {len(long)} câu quá dài để đọc kịp...")
        try:
            shorten(long)
            for i in synth(long):
                clips[i] = decode_pcm(path[i])
        except Exception as e:
            log(f"Không rút gọn được ({str(e)[:120]}), sẽ đọc nhanh hơn")

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


def render(video, srt_name, audio, out_path, workdir, font_size, burn, band=None, speech=None):
    cmd = ["ffmpeg", "-y", "-i", str(video)]
    if audio:
        cmd += ["-i", str(audio)]
    if burn:
        margin_v = 40
        chain = "[0:v]"
        if band:
            w, h = video_size(video)
            y = int(h * band[0]) // 2 * 2
            bh = max(2, (int(h * band[1]) - y) // 2 * 2)
            # chỉ che khi đang có thoại -> cảnh không thoại không bị vệt tối vắt ngang
            # (quá nhiều đoạn thì biểu thức quá dài -> che suốt video)
            enable = ""
            if speech and len(speech) <= 400:
                enable = ":enable='" + "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in speech) + "'"
            # làm mờ mạnh + phủ tối dải chữ Trung in cứng (không còn bóng chữ), rồi đặt phụ đề Việt đè lên
            chain = (f"[0:v]split[base][cut];[cut]crop=iw:{bh}:0:{y},gblur=sigma=30:steps=3,"
                     f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.72:t=fill[blur];"
                     f"[base][blur]overlay=0:{y}{enable}[cov];[cov]")
            margin_v = max(4, int((h - y - bh) / h * 288) + 2)
        style = (
            f"FontName=Arial,FontSize={font_size},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            f"BackColour=&H99000000,BorderStyle=3,Outline=2,Shadow=0,Alignment=2,MarginV={margin_v},Bold=1"
        )
        # chạy trong workdir để tránh lỗi đường dẫn Windows trong filter subtitles
        cmd += ["-filter_complex", f"{chain}subtitles={srt_name}:force_style='{style}'[v]", "-map", "[v]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
    else:
        cmd += ["-map", "0:v:0", "-c:v", "copy"]
    cmd += ["-map", "1:a:0" if audio else "0:a:0?"]
    if audio:
        cmd += ["-c:a", "copy"]
    cmd += ["-movflags", "+faststart", str(out_path)]
    run(cmd, cwd=workdir)


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
    ap.add_argument("--tts", choices=["edge", "gemini", "eleven"], default="edge",
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
    ap.add_argument("--no-ai", action="store_true", help="Không dịch bằng AI, dùng Google Dịch")
    ap.add_argument("--cookies-from-browser", help="Dùng cookie trình duyệt cho yt-dlp nếu cần (chrome/edge/firefox)")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("Chưa cài ffmpeg (winget install Gyan.FFmpeg)")

    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    # thư mục làm việc nằm trong thư mục kết quả (tránh lỗi TEMP khác nhau khi chạy từ web)
    workdir = Path(tempfile.mkdtemp(prefix="_tmp_", dir=outdir))
    try:
        video, title = download(args.source, workdir, args.cookies_from_browser)
        base = safe_name(title, f"douyin_{os.getpid()}")
        total = media_duration(video)
        segs = transcribe(video, workdir, args.model, args.lang, title)
        if not segs:
            sys.exit("Không nhận dạng được lời thoại nào trong video.")
        ai_cfg = None if args.no_ai else load_ai_config()
        if not ai_cfg and not args.no_ai:
            log("Chưa cài API key AI -> dịch bằng Google Dịch (kém tự nhiên hơn). Xem README để bật dịch AI.")
        chars, _ = translate(segs, args.lang, title, total, ai_cfg)

        audio = None
        if not args.no_dub and args.voice == "auto":
            assign_voices(segs, chars, workdir / "audio16k.wav")
        if not args.no_dub:
            audio = dub(segs, video, workdir, VOICES.get(args.voice, args.voice), args.bg_volume, args.max_speed, ai_cfg,
                        tts=args.tts, gem_fixed=args.gemini_voice,
                        el_voice=args.eleven_voice, el_model=args.eleven_model)
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

        out_path = outdir / f"{base}.vi.mp4"
        log("Đang xuất video..." if burn else "Đang xuất video (giữ nguyên hình gốc, chỉ thay tiếng)...")
        render(video, "sub.srt", audio, out_path, workdir, args.font_size, burn, band, speech_intervals(segs))
        shutil.copy(video, outdir / f"{base}.goc.mp4") if Path(video).parent == workdir else None
        log(f"XONG! -> {out_path}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
