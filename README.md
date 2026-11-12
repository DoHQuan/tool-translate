# Lồng Tiếng Việt – dịch và lồng tiếng Việt cho video Douyin / Bilibili

Dán link video (hoặc tải file lên), tool tự động:

1. **Tải video** không watermark (Douyin, Bilibili, TikTok, YouTube, hoặc file có sẵn)
2. **Nghe tiếng Trung** bằng faster-whisper chạy trên máy (hoặc qua đám mây ElevenLabs)
3. **Dịch sang tiếng Việt** bằng AI: hiểu cả đoạn hội thoại, chốt bảng nhân vật và xưng hô, tên đọc Hán-Việt
4. **Lồng tiếng Việt**, mỗi nhân vật một giọng, khớp thời gian từng câu; tiếng gốc chỉ nhỏ đi khi giọng Việt đang nói
5. **Xuất video** giữ hình gốc (hoặc khung dọc 9:16 cho TikTok), kèm phụ đề Việt/Trung, ảnh bìa và tiêu đề + hashtag TikTok

Mọi thứ chạy trên máy bạn qua một giao diện web ở `http://localhost:5577`.

---

## Mục lục

- [Cài đặt](#cài-đặt)
- [Mở và tắt app](#mở-và-tắt-app)
- [Dịch bằng AI](#dịch-bằng-ai)
- [Giọng đọc](#giọng-đọc)
- [Nghe tiếng Trung](#nghe-tiếng-trung)
- [Dùng hằng ngày](#dùng-hằng-ngày)
- [Sửa sau khi đã xong](#sửa-sau-khi-đã-xong)
- [Phim nhiều tập và video dài](#phim-nhiều-tập-và-video-dài)
- [Tìm video theo chủ đề](#tìm-video-theo-chủ-đề)
- [Dòng lệnh](#dòng-lệnh)
- [Thư mục và file](#thư-mục-và-file)
- [Kiểm thử](#kiểm-thử)
- [Xử lý sự cố](#xử-lý-sự-cố)

---

## Cài đặt

Cần Windows 10/11, **Python 3.10+** và **ffmpeg** (có trong PATH). Không cần GPU.

### 1. Thư viện chính (bắt buộc)

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Douyin chặn tải tự động, nên tool mở trang video bằng một Chromium ẩn để lấy link video gốc. Lần đầu nghe tiếng Trung, tool tự tải model nhận dạng giọng nói (khoảng 1,5 GB với chế độ "Chính xác").

### 2. Giọng VieNeu (không bắt buộc, nên cài)

VieNeu-TTS là giọng Việt chạy offline, miễn phí, nhiều giọng hơn hẳn giọng Microsoft. Nó cần môi trường Python riêng tên `.vieneu-venv` đặt trong thư mục tool:

```bash
python -m venv .vieneu-venv
.vieneu-venv\Scripts\python -m pip install vieneu
```

Không có thư mục này thì tool tự dùng giọng Microsoft Edge. Lần đầu dùng VieNeu sẽ tải model (vài trăm MB).

### 3. chat2api (không bắt buộc)

Dùng khi muốn dịch bằng tài khoản ChatGPT thay vì API key trả phí. Xem mục [chat2api](#chat2api).

### 4. Biểu tượng trên Desktop (không bắt buộc)

Chạy lệnh PowerShell sau trong thư mục tool để tạo biểu tượng **Long Tieng Viet** trên Desktop:

```powershell
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$env:USERPROFILE\Desktop\Long Tieng Viet.lnk")
$s.TargetPath = (Get-Command pythonw).Source
$s.Arguments = "`"$PWD\launcher.pyw`""
$s.WorkingDirectory = "$PWD"
$s.IconLocation = "$PWD\static\app.ico"
$s.Save()
```

---

## Mở và tắt app

| Cách | Dùng khi |
|---|---|
| Bấm đúp biểu tượng **Long Tieng Viet** trên Desktop | Hằng ngày. Tự bật chat2api (nếu dùng) và server ở chế độ ẩn, rồi mở cửa sổ app |
| `chay-server.bat` hoặc `mo-web.bat` | Muốn thấy cửa sổ log. Giữ cửa sổ đen mở suốt lúc dùng |
| `tat-app.bat` | Tắt app. Đóng cửa sổ app thôi thì server vẫn chạy ngầm |

Bấm biểu tượng khi app đang chạy chỉ mở lại cửa sổ, không bật server thứ hai. Tắt máy không mất gì: key, hồ sơ kênh, hồ sơ phim và lịch sử video đều lưu trên ổ cứng.

---

## Dịch bằng AI

Không có AI, tool dịch bằng Google Dịch: từng câu riêng lẻ, hay sai ngữ cảnh, xưng hô cứng. Có AI thì tool:

- sửa chỗ máy nghe nhầm chữ đồng âm
- lập bảng nhân vật và xưng hô cố định cho từng cặp (cháu ↔ cô), dịch nhất quán cả video
- đoán ai nói câu nào để mỗi nhân vật một giọng
- đọc tên nhân vật theo Hán-Việt (明哥 → anh Minh)
- viết câu vừa thời gian nói, câu nào dài quá thì rút gọn
- viết tiêu đề tiếng Việt, chữ ảnh bìa và hashtag TikTok

**Cách bật:** trong app, mục **Dịch bằng AI** → chọn nhà cung cấp → dán key → **Lưu** → **Kiểm tra**.

| Nhà cung cấp | Lấy key ở |
|---|---|
| Gemini (có gói miễn phí) | https://aistudio.google.com/app/apikey |
| Claude | https://console.anthropic.com/settings/keys |
| OpenAI | https://platform.openai.com/api-keys |
| DeepSeek | https://platform.deepseek.com/api_keys |
| chat2api | xem bên dưới |

**Mỗi nhà cung cấp lưu key và model riêng.** Đổi qua lại chỉ cần chọn rồi bấm Lưu, không phải dán lại; xoá key của một nhà cung cấp không ảnh hưởng các nhà cung cấp khác.

Key lưu trong `config.json` cạnh tool (đã nằm trong `.gitignore`, không bị đẩy lên GitHub). Không chia sẻ file này.

### chat2api

[chat2api](https://github.com/lanqian528/chat2api) biến tài khoản ChatGPT thành API dùng được cho tool.

1. Clone chat2api vào thư mục `.chat2api` trong thư mục tool và cài vào môi trường riêng:

   ```bash
   git clone https://github.com/lanqian528/chat2api .chat2api
   python -m venv .chat2api\.venv
   .chat2api\.venv\Scripts\python -m pip install -r .chat2api\requirements.txt
   ```

2. Trong app chọn **chat2api**, địa chỉ `http://127.0.0.1:5005/v1`, ô key nhập AccessToken/RefreshToken tài khoản ChatGPT (không phải mật khẩu), model `gpt-4o`. Bấm **Lưu** → **Kiểm tra**.

Khi đã chọn chat2api ở cổng 5005, tool **tự bật chat2api** lúc mở app, trước mỗi video và trước nút Kiểm tra, nên không cần chạy `chay-chat2api.bat` riêng. Server chat2api ở địa chỉ khác thì bạn tự chạy.

> **Lưu ý:** chat2api là dự án không chính thức. Dùng tài khoản ChatGPT theo cách này có thể vi phạm điều khoản của OpenAI và tài khoản có thể bị khoá; nó cũng có thể ngừng chạy khi OpenAI thay đổi.

---

## Giọng đọc

Chọn ở mục **Bộ đọc** khi tạo video:

| Bộ đọc | Giá | Ghi chú |
|---|---|---|
| **Edge** (mặc định) | Miễn phí | Chỉ 2 giọng Việt (nam, nữ); trẻ con và người già được tạo bằng chỉnh cao độ |
| **VieNeu** | Miễn phí, chạy trên máy | Nhiều giọng thật (nam, nữ, nhiều độ tuổi), mỗi nhân vật một giọng. Cần cài `.vieneu-venv` |
| **Gemini** | Theo key Gemini | Giọng AI tự nhiên, có cảm xúc. Dùng key Gemini trong mục Dịch bằng AI |
| **ElevenLabs** | Theo credit ElevenLabs | Chất lượng cao nhất. Dán key ở mục ElevenLabs; app hiện số credit còn lại và ước tính chi phí trước khi lồng |

Với chế độ **Theo nhân vật**, giọng bạn chọn dành cho nhân vật nói nhiều nhất; các nhân vật khác được gán giọng hợp giới tính và tuổi, không trùng nhau.

Bật **Duyệt bản dịch + chi phí trước khi lồng tiếng** để tool dừng sau bước dịch, cho bạn xem bản dịch và chi phí rồi mới lồng.

---

## Nghe tiếng Trung

| Chế độ | Tốc độ (máy Core i5 không GPU) | Ghi chú |
|---|---|---|
| **Nhanh** | ~0,7 phút cho mỗi phút video | Hay nghe nhầm chữ đồng âm |
| **Chính xác** (mặc định) | ~2,5 phút cho mỗi phút video | Nên dùng |
| **Đám mây** | Vài chục giây cho cả video | Qua ElevenLabs, tốn credit. Key phải bật quyền **Speech to Text**; lỗi thì tự quay về Chính xác |

Tool **nhớ lời đã nghe và bản dịch của từng video** (theo nội dung, không theo tên file). Làm lại cùng video thì bỏ qua bước nghe và dịch. Muốn dịch lại thì bật **Dịch lại từ đầu**.

---

## Dùng hằng ngày

1. Dán link Douyin/Bilibili (dán nguyên đoạn chia sẻ cũng được, hoặc **nhiều link mỗi dòng một link**), hoặc tải file video lên.
2. Chọn giọng, bộ đọc, chế độ nghe. Các tuỳ chọn khác:
   - **Khung dọc 9:16 cho TikTok**: video ngang được đặt giữa nền mờ, tiêu đề phía trên, phụ đề Việt phía dưới
   - **Ghép phụ đề Việt vào video**: in cứng phụ đề Việt vào video, tự làm mờ phụ đề Trung in sẵn
   - **Bộ phim**: xem [Phim nhiều tập](#phim-nhiều-tập-và-video-dài)
   - **Video dài** (3 / 5 / 10 phút…): tách thành nhiều phần dài cỡ đó, cắt ở chỗ im lặng
3. Bấm **Bắt đầu dịch**. Nhiều video thì xếp hàng làm lần lượt.

Xong thì xem ngay trong app: video, lời thoại song ngữ (bấm câu để tua), tải video, phụ đề, ảnh bìa, tiêu đề và hashtag TikTok.

**Video và file kết quả được đặt tên tiếng Việt** theo tiêu đề AI viết, ví dụ `Vì sao lợn, chó, mèo không cần học bơi.vi.mp4`. Rê chuột vào tên trong danh sách để xem tên gốc tiếng Trung.

**Hồ sơ kênh** (tên kênh, hashtag cố định, logo và vị trí logo) được áp vào mọi video.

**Video bị lỗi hoặc bị huỷ** có nút **Chạy tiếp từ chỗ dừng**: dùng lại video đã tải, lời đã nghe, bản dịch và các câu đã đọc.

---

## Sửa sau khi đã xong

### Sửa từng câu

Trên trang kết quả, rê chuột vào một câu → **✎ Sửa** → gõ lại lời Việt → Enter. Sửa xong các câu cần sửa thì bấm **Lưu và đọc lại N câu**. Tool chỉ đọc lại những câu bị sửa, các câu khác giữ tiếng cũ, rồi xuất lại video. Lời đã sửa được nhớ cho video đó.

### Đổi khung ngang ↔ dọc

Nút **↔ Xuất lại bản ngang** / **↕ Xuất lại khung dọc 9:16** dưới video. Tool chỉ xuất lại, không nghe, không dịch, không đọc lại (khoảng nửa phút). Bản mới thay bản cũ; muốn giữ bản cũ thì tải về trước.

---

## Phim nhiều tập và video dài

Điền cùng một tên vào ô **Bộ phim** cho các tập của một bộ phim. Tool giữ giống nhau giữa các tập:

- **tên nhân vật, giới tính, tuổi**: tập sau bắt đầu từ danh sách nhân vật các tập trước, chỉ thêm nhân vật mới
- **xưng hô**: cặp nào đã chốt thì giữ nguyên
- **giọng đọc**: nhân vật nào đã dùng giọng nào thì tập sau dùng đúng giọng đó

Ô này gợi ý các phim đã có và cho biết đã nhớ những nhân vật nào. Hồ sơ lưu trong thư mục `series/` (sửa tay được).

Video dài khi chọn tách ở mục **Video dài** được chia thành nhiều phần, mỗi phần là một video riêng; **các phần tự dùng chung một hồ sơ phim**.

---

## Tìm video theo chủ đề

Tab **Tìm video**: nhập chủ đề, chọn Bilibili hoặc Douyin, bấm **Tìm video**. Bật dịch từ khoá để gõ tiếng Việt: tool dịch sang tiếng Trung bằng AI đang dùng (Google Dịch dự phòng). Mỗi kết quả có **Xem gốc** và **Chọn để dịch** (đưa link vào form).

Douyin thường bắt đăng nhập nên hay không ra kết quả; khi đó tool có nút mở trang tìm kiếm của nguồn để bạn tự tìm rồi dán link.

---

## Dòng lệnh

```bash
python douyin_dub.py "https://v.douyin.com/xxxxxxx/"
```

Hoặc bấm đúp `dich.bat` rồi dán link. Kết quả nằm trong `output/`.

| Tuỳ chọn | Ý nghĩa |
|---|---|
| `--voice auto` / `nam` / `nu` | Mỗi nhân vật một giọng (mặc định) / một giọng nam / một giọng nữ cho cả video |
| `--tts edge` / `vieneu` / `gemini` / `eleven` | Bộ đọc |
| `--vieneu-voice`, `--gemini-voice`, `--eleven-voice` | Giọng cho nhân vật nói nhiều nhất |
| `--eleven-model v3` / `flash` | Model ElevenLabs |
| `--model small` / `medium` | Nghe Nhanh / Chính xác |
| `--asr eleven` | Nghe qua đám mây ElevenLabs |
| `--beam 2` | Độ kỹ khi nghe trên máy (1–5, mặc định 2) |
| `--series "Tên phim"` | Hồ sơ phim nhiều tập |
| `--vertical` | Khung dọc 9:16 cho TikTok |
| `--subs` | Ghép cứng phụ đề Việt; `--cover-subs off` để không làm mờ phụ đề Trung |
| `--no-dub` | Chỉ làm phụ đề, giữ tiếng gốc |
| `--no-cover` | Không tạo ảnh bìa và tiêu đề TikTok |
| `--no-ai` | Dịch bằng Google Dịch |
| `--confirm-dub` | Dừng sau bước dịch để duyệt; chạy lại lệnh để lồng tiếp |
| `--retranslate` | Dùng lại lời đã nghe nhưng dịch lại |
| `--no-cache` | Không dùng / không lưu lời nghe và bản dịch đã nhớ |
| `--fresh` | Bỏ kết quả dở của lần chạy trước |
| `--bg-volume 0.3` | Âm lượng tiếng gốc khi đang lồng (0 = tắt) |
| `--max-speed 1.3` | Giới hạn tốc độ đọc khi câu dịch dài |
| `--font-size 16` | Cỡ chữ phụ đề |
| `--no-profile` | Không áp hồ sơ kênh (logo, hashtag, tên kênh) |
| `-o D:\Videos` | Đổi thư mục lưu |
| `--cookies-from-browser chrome` | Dùng cookie trình duyệt khi nguồn chặn tải |

Biến môi trường `DUB_AI_PROVIDER`, `DUB_AI_KEY`, `DUB_AI_MODEL`, `DUB_AI_BASE_URL` ưu tiên hơn cấu hình trong app.

---

## Thư mục và file

| Đường dẫn | Nội dung |
|---|---|
| `output/jobs/<mã>/` | Mỗi video một thư mục: `<tên>.vi.mp4` (video tiếng Việt), `<tên>.goc.mp4` (gốc), `.vi.srt` / `.zh.srt` (phụ đề), `.bia.jpg` (ảnh bìa), `.tiktok.json` (tiêu đề, hashtag) |
| `output/jobs/<mã>/_resume/` | Dữ liệu để chạy tiếp, sửa câu và đổi khung |
| `config.json` | Key AI, key ElevenLabs, hồ sơ kênh. **Không chia sẻ** |
| `series/` | Hồ sơ phim nhiều tập |
| `profile/` | Logo kênh |
| `.models/` | Model nghe tiếng Trung và VieNeu |
| `.vieneu-venv/`, `.chat2api/` | Môi trường riêng của VieNeu và chat2api |
| `.tmp/` | File tạm, xoá thoải mái |

Tất cả thư mục trên đều nằm trong `.gitignore`.

---

## Kiểm thử

Bấm đúp `chay-kiem-thu.bat`, hoặc:

```bash
python -m unittest discover -s tests
```

Khoảng 1 phút, không dùng mạng, không đụng tới video, key hay cấu hình thật.

---

## Xử lý sự cố

| Hiện tượng | Cách xử lý |
|---|---|
| `mkl_malloc: failed to allocate memory` | Máy hết bộ nhớ. Đóng bớt Chrome/Discord, giải phóng ổ C (Windows cần chỗ trống ở ổ C làm bộ nhớ ảo), hoặc chọn nghe **Nhanh** |
| Video Bilibili báo "không có tiếng" | Nguồn chỉ cho tải hình. Thử link khác |
| Dịch ra bằng Google dù đã bật AI | Bấm **Kiểm tra** trong mục Dịch bằng AI để xem lỗi; với chat2api xem `.chat2api/server.auto.log` |
| Biểu tượng Desktop báo cổng 5577 đang bị dùng | Chạy `tat-app.bat` rồi mở lại |
| Douyin không tải được | Thử `--cookies-from-browser chrome`, hoặc tải video về rồi dùng **Tải file lên** |
| Nhân vật bị lồng nhầm giọng | AI đoán người nói theo nội dung nên đôi khi nhầm. Sửa lời bằng **✎ Sửa** hoặc chạy lại với **Dịch lại từ đầu** |

Video chỉ có nhạc, không có lời thoại thì không có gì để dịch.
