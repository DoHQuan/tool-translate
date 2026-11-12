"""Khởi động chat2api cục bộ cùng tool, dùng lại server đang chạy."""
import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def ensure_chat2api():
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    provider = os.environ.get("DUB_AI_PROVIDER") or cfg.get("ai_provider")
    if provider != "chat2api":
        return
    url = os.environ.get("DUB_AI_BASE_URL") or cfg.get("ai_base_url") or "http://127.0.0.1:5005/v1"
    parsed = urlsplit(url)
    if parsed.hostname not in ("localhost", "127.0.0.1") or parsed.port != 5005 or parsed.scheme != "http":
        return  # Server tùy chỉnh do người dùng tự quản lý.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def ready():
        try:
            with opener.open("http://127.0.0.1:5005/None/openapi.json", timeout=1) as response:
                return "/v1/chat/completions" in json.load(response).get("paths", {})
        except (OSError, ValueError):
            return False

    if ready():
        return
    with socket.socket() as sock:
        sock.settimeout(1)
        busy = sock.connect_ex(("127.0.0.1", 5005)) == 0
    process = None
    if not busy:
        folder = ROOT / ".chat2api"
        python = folder / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.is_file():
            raise RuntimeError("Chưa cài chat2api trong .chat2api/.venv.")
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        with (folder / "server.auto.log").open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                [str(python), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "5005"],
                cwd=str(folder), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                creationflags=flags, close_fds=True)
    for _ in range(40):
        if ready():
            return
        if process is not None and process.poll() is not None:
            break
        time.sleep(0.5)
    raise RuntimeError("Không bật được chat2api ở cổng 5005. Xem .chat2api/server.auto.log; kiểm tra cổng có bị ứng dụng khác dùng không.")
