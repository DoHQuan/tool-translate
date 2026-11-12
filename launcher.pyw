"""Mở app Lồng Tiếng Việt bằng một cú bấm (dùng cho biểu tượng ngoài màn hình).

- Server chưa chạy thì bật nó ở chế độ ẩn (không có cửa sổ đen), nhật ký ghi vào output/server.log.
- Server đã chạy rồi thì chỉ mở cửa sổ app.
- Cửa sổ app mở bằng Edge / Chrome ở chế độ ứng dụng (không có thanh địa chỉ); không có thì mở trình duyệt mặc định.

Chạy:  pythonw launcher.pyw            (thêm --no-window để chỉ bật server, không mở cửa sổ)
Tắt server: bấm đúp tat-app.bat
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", 5577))
URL = f"http://localhost:{PORT}"


def server_up():
    try:
        with urllib.request.urlopen(URL + "/api/config", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def port_busy():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def start_server():
    exe = Path(sys.executable)
    py = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe  # server cần python.exe để ghi log
    (ROOT / "output").mkdir(exist_ok=True)
    log = open(ROOT / "output" / "server.log", "a", encoding="utf-8", errors="replace")
    log.write(f"\n==== Bật server lúc {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
    log.flush()
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(py), "-u", str(ROOT / "server.py")], cwd=str(ROOT), stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)


def open_window():
    for exe in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if os.path.isfile(exe):
            subprocess.Popen([exe, f"--app={URL}", "--window-size=1320,900"], close_fds=True)
            return
    webbrowser.open(URL)


def alert(msg):
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, msg, "Lồng Tiếng Việt", 0x30)
    except Exception:
        pass


def main():
    from chat2api_runtime import ensure_chat2api

    try:
        ensure_chat2api()
    except Exception as e:
        return alert(str(e))
    if not server_up():
        if port_busy():  # cổng đang bị chương trình khác chiếm, hoặc server đang khởi động
            for _ in range(20):
                if server_up():
                    break
                time.sleep(0.5)
            else:
                return alert(f"Cổng {PORT} đang bị một chương trình khác dùng nên không bật được app.")
        else:
            start_server()
            for _ in range(60):
                if server_up():
                    break
                time.sleep(0.5)
            else:
                return alert("Không bật được server. Xem lỗi trong file output\\server.log trong thư mục tool.")
    if "--no-window" not in sys.argv:
        open_window()


if __name__ == "__main__":
    main()
