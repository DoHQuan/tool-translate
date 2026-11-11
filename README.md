# Douyin Dub – Phụ đề + lồng tiếng Việt cho video Douyin

Dán link Douyin vào, tool sẽ tự động:

1. **Tải video** không watermark
2. **Nghe tiếng Trung** (faster-whisper, chạy offline trên máy, lấy thời gian theo từng chữ)
3. **Dịch sang tiếng Việt**: bằng AI (Gemini/Claude/OpenAI/DeepSeek) nếu có API key, không thì Google Dịch
4. **Lồng tiếng Việt** (giọng AI Microsoft Edge, căn khớp thời gian từng câu; tiếng gốc chỉ nhỏ đi khi giọng Việt đang nói)
5. **Giữ nguyên hình gốc** (kể cả phụ đề Trung in sẵn), chỉ thay tiếng bằng giọng Việt. Phụ đề Việt xuất ra file `.srt`;
   muốn ghép cứng vào video thì bật tuỳ chọn (khi đó tool tự làm mờ phụ đề Trung rồi đặt phụ đề Việt đè lên)

## Cài đặt (một lần)

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Cần có `ffmpeg` (máy này đã có sẵn). Lần chạy đầu sẽ tải model nhận dạng giọng nói (~1,5 GB với model `medium`).

Douyin chặn tải video tự động, nên tool mở trang video bằng một trình duyệt Chromium ẩn (giống người xem thật) để lấy link video gốc không watermark.

## Bật dịch bằng AI (nên làm)

Google Dịch dịch từng câu riêng lẻ nên hay sai ngữ cảnh, xưng hô cứng, câu dài hơn lời gốc (giọng lồng phải đọc vội). Dịch bằng AI thì:

- hiểu cả đoạn hội thoại, tự sửa chỗ máy nghe nhầm chữ đồng âm
- lập bảng nhân vật + xưng hô cố định cho từng cặp (VD cháu ↔ cô), dịch nhất quán cả video
- đoán ai nói câu nào để lồng mỗi nhân vật một giọng
- tên nhân vật đọc Hán-Việt (明哥 → anh Minh)
- viết câu vừa đủ thời gian nói; câu nào vẫn dài sẽ được rút gọn lại

Cách bật: trên web, mục **Dịch bằng AI** → chọn nhà cung cấp → dán API key → **Lưu** → **Kiểm tra**.
Gemini có gói miễn phí: lấy key ở https://aistudio.google.com/app/apikey

Key được lưu trong `config.json` cạnh tool (đã nằm trong `.gitignore`). Dùng dòng lệnh thì có thể đặt biến môi trường `DUB_AI_PROVIDER` (`gemini`/`claude`/`openai`/`deepseek`) và `DUB_AI_KEY`.

## Cách dùng

### Giao diện web (dễ nhất)

Nhấp đúp `mo-web.bat`. Trình duyệt sẽ tự mở trang http://localhost:5577:

- Dán link Douyin (hoặc tải file video lên), chọn giọng nam/nữ, bấm **Bắt đầu dịch**
- Xem tiến độ từng bước: tải video → nghe tiếng Trung → dịch → lồng tiếng → xuất video
- Xong thì xem video ngay trên web, tải video/phụ đề về, xem lời thoại song ngữ (bấm vào câu để tua tới đó)
- Có lịch sử các video đã làm; gửi nhiều link cùng lúc thì tool sẽ xếp hàng xử lý lần lượt

Web chỉ chạy trên máy bạn; đóng cửa sổ đen là tắt web. Kết quả lưu trong `output/jobs/`.

### Dòng lệnh

Nhấp đúp `dich.bat`, dán link và Enter. Hoặc:

```bash
python douyin_dub.py "https://v.douyin.com/xxxxxxx/"
```

Có thể dán nguyên đoạn chia sẻ của Douyin (kèm chữ Trung), tool tự lấy link ra.

Kết quả nằm trong thư mục `output/`:

| File | Nội dung |
|---|---|
| `<tên>.vi.mp4` | Video đã lồng tiếng + phụ đề Việt |
| `<tên>.goc.mp4` | Video gốc |
| `<tên>.vi.srt` / `<tên>.zh.srt` | Phụ đề Việt / Trung |

## Tuỳ chọn

| Tuỳ chọn | Ý nghĩa |
|---|---|
| `--voice nam` / `--voice nu` | Một giọng nam / nữ cho cả video. Mặc định `auto`: AI xác định nhân vật, mỗi nhân vật một giọng (nam, nữ, trẻ con = giọng nữ nâng cao độ, người già = hạ cao độ) |
| `--model small` | Nghe nhanh hơn ~2 lần nhưng hay nhầm chữ đồng âm (mặc định `medium`) |
| `--no-ai` | Không dùng AI, dịch bằng Google |
| `--subs` | Ghép cứng phụ đề Việt vào video (mặc định không ghép, giữ nguyên hình gốc) |
| `--cover-subs off` | Khi dùng `--subs`: không làm mờ phụ đề Trung in sẵn |
| `--no-dub --subs` | Chỉ thêm phụ đề Việt, giữ tiếng gốc |
| `--bg-volume 0.3` | Tiếng gốc to hơn khi đang lồng tiếng (0 = tắt hẳn) |
| `--max-speed 1.3` | Giới hạn tốc độ đọc khi câu dịch dài |
| `--font-size 16` | Chữ phụ đề to hơn |
| `-o D:\Videos` | Đổi thư mục lưu |
| `--cookies-from-browser chrome` | Dùng khi Douyin chặn tải |

Cũng dùng được với file video có sẵn: `python douyin_dub.py video.mp4`, và các link TikTok/YouTube/Bilibili.

## Lưu ý

- Máy không có GPU nên chạy bằng CPU: video 1 phút 46 giây mất khoảng 6 phút với model `medium`.
- Giọng đọc Microsoft và Google Dịch đôi khi tạm từ chối yêu cầu; tool tự thử lại.
- Tiếng Việt của Microsoft chỉ có 2 giọng (1 nam, 1 nữ): giọng trẻ con và nhiều nhân vật cùng giới được tạo bằng cách chỉnh cao độ.
- AI đoán người nói theo nội dung lời thoại nên đôi khi nhầm (câu của nhân vật này lồng giọng nhân vật khác).
- Video chỉ có nhạc, không có lời thoại thì không có gì để dịch.
