"""Tạo ảnh bìa dọc 9:16 + tiêu đề + hashtag để đăng TikTok cho video đã lồng tiếng.

- Tiêu đề, chữ trên ảnh bìa và hashtag do AI viết từ lời thoại đã dịch (không có AI thì dịch tiêu đề gốc).
- Ảnh bìa lấy khung hình nét và nhiều màu nhất trong video, thêm chữ lớn có viền.
  Video ngang: khung hình đặt giữa, chữ ở trên và dưới. Video dọc: khung hình phủ kín, chữ ở nửa trên.
"""
import json
import re
import subprocess
from pathlib import Path

W, H = 1080, 1920
FONTS = ["C:/Windows/Fonts/seguibl.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/tahomabd.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]
YELLOW, WHITE, RED, BLACK = (255, 214, 10, 255), (255, 255, 255, 255), (220, 30, 30, 255), (0, 0, 0, 255)

AI_META = """Bạn viết nội dung đăng TikTok tiếng Việt cho một video đã được dịch và lồng tiếng.
Dựa vào tiêu đề gốc và lời thoại tiếng Việt, trả về DUY NHẤT một JSON:
{"title": "...", "cover": "...", "badge": "...", "hashtags": ["...", "..."]}
- title: tiêu đề bài đăng, tối đa 90 ký tự, gây tò mò nhưng ĐÚNG nội dung video, tiếng Việt tự nhiên,
  không có hashtag, không viết hoa toàn bộ, không bịa chi tiết không có trong lời thoại.
- cover: chữ lớn trên ảnh bìa, 3 đến 9 từ, tối đa 40 ký tự, VIẾT HOA CÓ DẤU, là ý gây tò mò nhất (thường là câu hỏi).
  PHẢI là một ý TRỌN VẸN, đọc riêng vẫn hiểu ngay video nói về gì (vd "SAO LỢN, CHÓ, MÈO KHÔNG CẦN HỌC BƠI?"),
  không được cụt lủn giữa chừng (SAI: "SAO LỢN, CHÓ, MÈO").
- badge: một con số hoặc chi tiết ấn tượng CÓ THẬT trong lời thoại, tối đa 18 ký tự, VIẾT HOA CÓ DẤU.
  Không có chi tiết nào đáng nêu thì để chuỗi rỗng "".
- hashtags: 5 đến 8 hashtag tiếng Việt KHÔNG dấu, viết liền, chữ thường, không có dấu #, từ cụ thể đến chung."""


def _font(size):
    from PIL import ImageFont

    for f in FONTS:
        if Path(f).is_file():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def _clean_tag(t):
    import unicodedata

    t = unicodedata.normalize("NFD", str(t).lower().replace("đ", "d"))
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in t if not unicodedata.combining(c)))[:30]


def write_meta(segs, title_zh, ai_cfg, part="", extra_tags=()):
    """Trả về {"title", "cover", "badge", "hashtags"}. Có AI thì AI viết, không thì dịch tiêu đề gốc.
    extra_tags: hashtag mặc định của kênh (hồ sơ kênh), luôn được thêm vào."""
    import douyin_dub as d

    zh = re.sub(r"\s*[#@].*", "", title_zh or "").strip()
    lines = [s["vi"].strip() for s in segs if s.get("vi", "").strip()]
    meta = None
    if ai_cfg and lines:
        text = "\n".join(lines)
        if len(text) > 6000:  # video dài: đầu + cuối là đủ để hiểu nội dung
            text = text[:4200] + "\n...\n" + text[-1500:]
        try:
            out = d.ai_chat(ai_cfg, AI_META, json.dumps({"tieu_de_goc": zh, "loi_thoai": text}, ensure_ascii=False), timeout=60)
            m = d._parse_json(out)
            if isinstance(m, dict) and str(m.get("title", "")).strip():
                meta = m
        except Exception as e:
            d.log(f"  AI không viết được tiêu đề ({str(e)[:100]}), dùng tiêu đề gốc đã dịch")
    if not meta:
        vi = ""
        if zh:
            import time

            for wait in (0, 2, 5):  # Google hay báo "too many requests" ngay sau khi vừa dịch cả video
                time.sleep(wait)
                try:
                    from deep_translator import GoogleTranslator

                    vi = (GoogleTranslator(source="auto", target="vi").translate(zh[:300]) or "").strip()
                    if vi:
                        break
                except Exception:
                    vi = ""
            vi = re.split(r"(?<=[.!?。])\s+", vi)[0][:90].strip() if vi else ""  # chỉ lấy câu đầu làm tiêu đề
        vi = vi or (lines[0] if lines else "Video lồng tiếng Việt")
        meta = {"title": vi, "cover": "", "badge": "", "hashtags": []}
    title = re.sub(r"\s+", " ", str(meta.get("title", ""))).strip().strip('"')[:100]
    # chữ bìa phải trọn ý: không có thì lấy cả tiêu đề; dài quá thì cắt ở ranh giới từ, không cắt giữa chừng
    cover = _fit_words(re.sub(r"\s+", " ", str(meta.get("cover") or "")).strip() or title, 44).upper()
    badge = re.sub(r"\s+", " ", str(meta.get("badge") or "")).strip().upper()[:22]
    tags = []
    own = [x for x in (_clean_tag(t) for t in extra_tags) if x]
    for t in list(meta.get("hashtags") or [])[:8 - min(len(own), 4)] + own + ([] if own else ["longtieng", "xuhuong"]):
        t = _clean_tag(t)
        if t and t not in tags:
            tags.append(t)
    if part:
        title = f"{title} (Phần {part.split('/')[0]})"
    return {"title": title, "cover": cover, "badge": badge, "hashtags": tags[:10]}


def title_overlay(out_png, text, area_h, part=""):
    """Lớp chữ trong suốt 1080x1920 cho video xuất dọc: tiêu đề lớn nằm trong vùng phía trên khung hình (cao area_h)."""
    from PIL import Image, ImageDraw

    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    top = 150 if part else 60
    _headline(d, _wrap(text), top, W - 90, max(120, area_h - top - 40), start=200)
    if part:
        _part_pill(im, part)
    im.save(out_png)


def pick_frame(video, total, samples=14):
    """Thời điểm của khung hình đẹp nhất: nét, nhiều màu, không quá tối / quá sáng."""
    import numpy as np

    best, best_t = -1.0, total * 0.3
    w, h = 320, 180
    for k in range(samples):
        t = total * (0.08 + 0.84 * k / max(1, samples - 1))
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
        if len(r.stdout) != w * h * 3:
            continue
        a = np.frombuffer(r.stdout, dtype=np.uint8).reshape(h, w, 3).astype(np.float32)
        g = a.mean(axis=2)
        lap = g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
        sharp = float(lap.var())
        sat = float((a.max(axis=2) - a.min(axis=2)).mean())
        lum = float(g.mean())
        expo = 1.0 if 55 <= lum <= 190 else 0.35  # cảnh đen / cháy sáng thì bỏ
        score = (sharp ** 0.5) * (20 + sat) * expo
        if score > best:
            best, best_t = score, t
    return best_t


def _grab(video, t, out):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                    "-vf", "scale='min(1920,iw)':-2", "-q:v", "2", str(out)], capture_output=True)
    return Path(out).is_file()


def _fit_words(text, limit):
    """Giữ nguyên nếu vừa limit ký tự; dài hơn thì bỏ bớt TỪ ở cuối (ưu tiên dừng ở dấu câu), không cắt giữa từ."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit + 1]
    for mark in ("?", "!", ".", ",", ":", ";"):  # dừng ở dấu câu gần cuối nhất nếu còn giữ được quá nửa
        k = cut.rfind(mark)
        if k >= limit * 0.55:
            return cut[:k + (1 if mark in "?!" else 0)].strip()
    return cut[:cut.rfind(" ")].strip() if " " in cut else cut[:limit]


def _wrap(text, max_chars=12):
    """Chia chữ thành ít dòng nhất mà mỗi dòng không quá max_chars ký tự, các dòng dài gần bằng nhau."""
    from itertools import combinations

    words = text.split()
    if not words:
        return [""]
    if len(text) > max_chars * 3:  # câu dài: cho mỗi dòng rộng hơn (chữ sẽ tự nhỏ lại) thay vì thêm quá nhiều dòng
        max_chars = max(max_chars, -(-len(text) // 4) + 2)
    best = None
    for k in range(1, min(4, len(words)) + 1):
        for cut in combinations(range(1, len(words)), k - 1):
            b = (0,) + cut + (len(words),)
            lines = [" ".join(words[b[i]:b[i + 1]]) for i in range(k)]
            longest = max(len(s) for s in lines)
            # ưu tiên: vừa max_chars, rồi cân đối (dòng dài nhất ngắn nhất), dòng cuối không quá cụt
            over = longest > max_chars
            key = (over, longest if over else longest - min(len(s) for s in lines))
            if best is None or key < best[0]:
                best = (key, lines)
        if best and not best[0][0]:
            break  # đã có cách chia k dòng vừa khung -> không cần thêm dòng
    return best[1]


def _headline(draw, lines, top, max_w, max_h, start=250):
    """Vẽ các dòng chữ lớn canh giữa, tự thu nhỏ cho vừa khung. Dòng cuối màu vàng. Trả về toạ độ đáy."""
    size = start
    while size > 60:
        f = _font(size)
        gap = int(size * 1.24)  # đủ chỗ cho dấu tiếng Việt, không đè lên dòng trên
        if max(draw.textlength(s, font=f) for s in lines) <= max_w and gap * len(lines) <= max_h:
            break
        size -= 6
    f, gap, stroke = _font(size), int(size * 1.24), max(6, size // 20)
    y = top + int(size * 1.02)
    for i, s in enumerate(lines):
        fill = YELLOW if (i == len(lines) - 1 and len(lines) > 1) else WHITE
        draw.text((W // 2 + 6, y + 8), s, font=f, fill=BLACK, stroke_width=stroke, stroke_fill=BLACK, anchor="ms")
        draw.text((W // 2, y), s, font=f, fill=fill, stroke_width=stroke, stroke_fill=BLACK, anchor="ms")
        y += gap
    return y - gap + int(size * 0.3)


def _badge(im, text, cy, size=92):
    from PIL import Image, ImageDraw

    d = ImageDraw.Draw(im)
    f = _font(size)
    while d.textlength(text, font=f) > W - 220 and size > 44:
        size -= 4
        f = _font(size)
    tw = int(d.textlength(text, font=f))
    lab = Image.new("RGBA", (tw + 100, int(size * 1.8)), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lab)
    ld.rounded_rectangle([0, 0, lab.width - 1, lab.height - 1], 24, fill=RED, outline=BLACK, width=8)
    ld.text((lab.width // 2, lab.height // 2 - size * 0.06), text, font=f, fill=WHITE, anchor="mm")
    lab = lab.rotate(4, expand=True, resample=Image.BICUBIC)
    im.alpha_composite(lab, ((W - lab.width) // 2, int(cy - lab.height / 2)))


def _part_pill(im, part):
    from PIL import ImageDraw

    d = ImageDraw.Draw(im)
    f = _font(54)
    s = f"PHẦN {part.split('/')[0]}"
    tw = int(d.textlength(s, font=f))
    d.rounded_rectangle([40, 40, 40 + tw + 56, 40 + 92], 46, fill=YELLOW, outline=BLACK, width=6)
    d.text((40 + (tw + 56) // 2, 40 + 44), s, font=f, fill=BLACK, anchor="mm")


def _channel_pill(im, channel):
    from PIL import ImageDraw

    d = ImageDraw.Draw(im)
    f = _font(46)
    s = channel if channel.startswith("@") else "@" + channel
    tw = int(d.textlength(s, font=f))
    x0, y0 = (W - tw - 60) // 2, H - 330  # trên vùng TikTok phủ chú thích ở đáy
    d.rounded_rectangle([x0, y0, x0 + tw + 60, y0 + 78], 39, fill=(0, 0, 0, 170))
    d.text((W // 2, y0 + 37), s, font=f, fill=WHITE, anchor="mm")


def make_cover(frame_path, out_path, cover, badge="", part="", band=None, channel=""):
    """Ghép ảnh bìa 1080x1920. band = (y0, y1) dải phụ đề in cứng của video gốc (tỉ lệ chiều cao) để cắt bỏ."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    src = Image.open(frame_path).convert("RGB")
    src = ImageEnhance.Contrast(ImageEnhance.Color(src).enhance(1.2)).enhance(1.06)
    lines = _wrap(cover)
    landscape = src.width > src.height * 1.15
    if landscape:
        if band and band[0] > 0.6:  # bỏ dải phụ đề gốc ở đáy khung hình
            src = src.crop((0, 0, src.width, int(src.height * band[0])))
        bw = int(src.width * H / src.height)
        bg = src.resize((bw, H)).crop(((bw - W) // 2, 0, (bw + W) // 2, H))
        bg = ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(28))).enhance(0.38)
        im = bg.convert("RGBA")
        fr = src.resize((W, int(src.height * W / src.width)), Image.LANCZOS)
        fy = 770
        d = ImageDraw.Draw(im)
        d.rectangle([0, fy - 10, W, fy + fr.height + 10], fill=YELLOW)
        im.paste(fr, (0, fy))
        _headline(d, lines, 150 if part else 70, W - 90, fy - (190 if part else 110))
        if badge:
            _badge(im, badge, fy + fr.height + 170)
    else:
        bh = int(src.height * W / src.width)
        base = src.resize((W, bh), Image.LANCZOS)
        if bh >= H:
            base = base.crop((0, (bh - H) // 2, W, (bh - H) // 2 + H))
        else:
            canvas = base.resize((int(W * H / bh), H)).crop(((int(W * H / bh) - W) // 2, 0, (int(W * H / bh) + W) // 2, H))
            canvas = ImageEnhance.Brightness(canvas.filter(ImageFilter.GaussianBlur(28))).enhance(0.4)
            canvas.paste(base, (0, (H - bh) // 2))
            base = canvas
        im = base.convert("RGBA")
        shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shade)
        for y in range(0, int(H * 0.5)):  # làm tối dần nửa trên để chữ nổi
            sd.line([(0, y), (W, y)], fill=(8, 10, 14, int(170 * (1 - y / (H * 0.5)) ** 0.7)))
        im = Image.alpha_composite(im, shade)
        d = ImageDraw.Draw(im)
        bottom = _headline(d, lines, 230, W - 110, 620, start=210)
        if badge:
            _badge(im, badge, bottom + 150, size=80)
    if part:
        _part_pill(im, part)
    if channel:
        _channel_pill(im, channel)
    im.convert("RGB").save(out_path, quality=92)


def build(video, segs, title_zh, ai_cfg, outdir, base, total, band=None, part="", meta=None, channel="", extra_tags=()):
    """Tạo <base>.bia.jpg, <base>.tiktok.json và <base>.tiktok.txt trong outdir. Trả về meta."""
    import douyin_dub as d

    outdir = Path(outdir)
    meta = meta or write_meta(segs, title_zh, ai_cfg, part, extra_tags)
    post = meta["title"] + "\n\n" + " ".join("#" + t for t in meta["hashtags"])
    (outdir / f"{base}.tiktok.txt").write_text(post + "\n", encoding="utf-8")
    (outdir / f"{base}.tiktok.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    frame = outdir / f"_frame_{base[:20]}.jpg"
    try:
        if _grab(video, pick_frame(video, total), frame):
            make_cover(frame, outdir / f"{base}.bia.jpg", meta["cover"], meta["badge"], part, band, channel)
    finally:
        frame.unlink(missing_ok=True)
    d.log(f"  Tiêu đề: {meta['title']}".replace("Tiêu đề:", "Tiêu đề TikTok:"))
    return meta
