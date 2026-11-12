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
# Dịch qua chat2api

## Tìm video theo chủ đề

Ở đầu giao diện, nhập chủ đề, chọn Douyin hoặc Bilibili và bấm **Tìm video**. Tùy chọn dịch từ khóa dùng Google Translate để đổi chủ đề tiếng Việt sang tiếng Trung; bỏ chọn nếu bạn nhập từ khóa Trung trực tiếp. Kết quả có ảnh (nếu nguồn cung cấp), tiêu đề, nút **Xem gốc** và **Chọn để dịch**. Chọn video đưa link vào biểu mẫu hiện có; chỉnh tùy chọn rồi bấm Bắt đầu dịch.

Mỗi lần hiển thị tối đa 12 video. Tìm kiếm dùng Chromium của tool, không yêu cầu API key riêng. Nguồn có thể yêu cầu đăng nhập/xác minh hoặc chặn truy cập: khi đó tool hiển thị thông báo và nút mở trang tìm kiếm nguồn để bạn tìm rồi dán link thủ công.

Khi đã chọn chat2api cục bộ ở cổng 5005, mở tool bằng biểu tượng ứng dụng hoặc `chay-server.bat` sẽ tự bật chat2api chạy nền. Server đang chạy sẽ được dùng lại. Đóng cửa sổ giao diện vẫn giữ các server nền như trước; không cần mở riêng `chay-chat2api.bat`. Server ở địa chỉ tùy chỉnh vẫn do người dùng tự chạy.

Tool hỗ trợ server [chat2api](https://github.com/lanqian528/chat2api) trong phần **Dịch bằng AI**.

1. Chạy server chat2api riêng theo README của repo đó; mặc định server nghe cổng `5005`.
2. Trong tool, chọn nhà cung cấp **chat2api (server ChatGPT web)**.
3. Nhập địa chỉ `http://127.0.0.1:5005/v1`. Nếu server dùng `API_PREFIX`, nhập dạng `http://127.0.0.1:5005/prefix/v1`. Dùng HTTPS khi kết nối server từ xa.
4. Ô API key: nhập `AUTHORIZATION` đã cấu hình trên server có token tài khoản được nạp, hoặc AccessToken/RefreshToken tài khoản ChatGPT. Đây không phải mật khẩu ChatGPT.
5. Nhập tên model server/tài khoản hỗ trợ; mặc định tool dùng `gpt-4o`. Bấm **Lưu**, rồi **Kiểm tra** để thử dịch một câu.
6. Tạo video mới để dùng cấu hình này. Với video từng dịch trước đây, bật **Dịch lại từ đầu** khi tạo công việc mới để bỏ bản dịch trong bộ nhớ.

CLI cũng đọc `DUB_AI_PROVIDER=chat2api`, `DUB_AI_KEY`, `DUB_AI_MODEL` và `DUB_AI_BASE_URL`; biến môi trường ưu tiên hơn cấu hình giao diện. Key/token được lưu trong `config.json` trên máy như các nhà cung cấp hiện có; không chia sẻ file này.

chat2api được dùng cho sửa ASR, phân tích nhân vật/xưng hô, dịch và viết tiêu đề. Giọng đọc tiếp tục dùng bộ TTS bạn chọn; chat2api không cung cấp Gemini TTS. Khả năng hoạt động và chất lượng phụ thuộc server, model, quyền tài khoản và lời nhận dạng đầu vào. Việc có mã nguồn chat2api không tự tạo ra token hoặc quyền dùng model.

