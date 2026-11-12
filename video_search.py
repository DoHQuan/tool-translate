"""Tìm video trên trang tìm kiếm công khai bằng Chromium của tool."""
import re
import threading
from urllib.parse import quote, urlsplit, parse_qs

SEARCH_LOCK = threading.Lock()


def search_url(source, keyword):
    if source == "douyin":
        return "https://www.douyin.com/search/" + quote(keyword, safe="") + "?type=video"
    if source == "bilibili":
        return "https://search.bilibili.com/video?keyword=" + quote(keyword, safe="")
    raise ValueError("Nguồn tìm kiếm không hợp lệ.")


def canonical_video(source, url):
    parsed = urlsplit(url)
    if source == "bilibili" and parsed.hostname in ("www.bilibili.com", "bilibili.com"):
        match = re.search(r"/video/(BV[A-Za-z0-9]+|av\d+)", parsed.path)
        if match:
            return "https://www.bilibili.com/video/" + match[1] + "/"
    if source == "douyin" and parsed.hostname in ("www.douyin.com", "douyin.com"):
        match = re.search(r"/video/(\d+)", parsed.path)
        vid = match[1] if match else (parse_qs(parsed.query).get("modal_id") or [""])[0]
        if vid.isdigit():
            return "https://www.douyin.com/video/" + vid
    return None


KEYWORD_PROMPT = """Dịch từ khóa tìm kiếm video sau sang tiếng Trung giản thể, theo cách người dùng Douyin / Bilibili hay gõ
khi tìm (ngắn gọn, từ khóa chứ không phải câu văn; giữ nguyên tên riêng phổ biến). Chỉ trả về JSON: {"zh": "..."}"""


def translate_keyword(keyword):
    """Từ khóa tiếng Việt -> tiếng Trung: dùng AI đã cài (chat2api...) trước, Google Dịch dự phòng.
    Không dịch được thì trả lại nguyên từ khóa."""
    import json

    import douyin_dub as d

    cfg = d.load_ai_config()
    if cfg:
        try:
            if cfg.get("provider") == "chat2api":
                from chat2api_runtime import ensure_chat2api

                ensure_chat2api()
            zh = str(d._parse_json(d.ai_chat(cfg, KEYWORD_PROMPT, json.dumps({"tu_khoa": keyword}, ensure_ascii=False),
                                              timeout=40)).get("zh") or "").strip()
            if zh and re.search(r"[一-鿿]", zh):
                return zh[:80]
        except Exception:
            pass
    try:
        from deep_translator import GoogleTranslator

        zh = (GoogleTranslator(source="auto", target="zh-CN").translate(keyword) or "").strip()
        if zh:
            return zh
    except Exception:
        pass
    return keyword


def search_videos(source, keyword, translate=False):
    from douyin_dub import DESKTOP_UA  # thiết lập đường dẫn Chromium đã cài
    from playwright.sync_api import sync_playwright

    query = keyword
    warning = ""
    if translate:
        query = translate_keyword(keyword)
        if query == keyword:
            warning = "Không dịch được từ khóa; đang tìm bằng từ khóa gốc. "
    url = search_url(source, query)
    if not SEARCH_LOCK.acquire(blocking=False):
        raise RuntimeError("Đang có lượt tìm kiếm khác. Chờ kết quả rồi thử lại.")
    items = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=DESKTOP_UA, locale="zh-CN", viewport={"width": 1366, "height": 900})
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=35000)
                for _ in range(10):
                    page.wait_for_timeout(1200)
                    rows = page.eval_on_selector_all("a[href]", """els => els.map(a => {
                      const card = a.closest('.bili-video-card, .video-item, li') || a;
                      const img = card.querySelector('img');
                      const heading = card.querySelector('h3, .bili-video-card__info--tit, [data-e2e="video-desc"]');
                      return {url:a.href, title:heading?.getAttribute('title') || heading?.innerText || a.getAttribute('title') || img?.alt || a.innerText || '',
                              thumbnail:img?.currentSrc || img?.src || ''};
                    })""")
                    for row in rows:
                        link = canonical_video(source, row["url"])
                        if not link:
                            continue
                        title = re.sub(r"\s+", " ", row["title"]).strip()[:250]
                        if link not in items or (not items[link]["title"] and title):
                            thumbnail = row.get("thumbnail", "")
                            items[link] = {"url": link, "title": title, "thumbnail": thumbnail if thumbnail.startswith("https://") else ""}
                    if len(items) >= 12:
                        break
                    page.mouse.wheel(0, 650)
            finally:
                browser.close()
    finally:
        SEARCH_LOCK.release()
    if not items:
        warning += "Không đọc được kết quả. Nguồn có thể yêu cầu đăng nhập/xác minh hoặc không có video phù hợp. Hãy mở trang nguồn để tìm trực tiếp rồi dán link vào tool."
    return {"items": list(items.values())[:12], "query": query, "source": source, "search_url": url, "warning": warning}
