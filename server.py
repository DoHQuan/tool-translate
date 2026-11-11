#!/usr/bin/env python3
"""
server.py - Giao diện web cho douyin_dub.py

Chạy:  python server.py   rồi mở http://localhost:5577
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / "output" / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("PORT", 5577))

app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # file tải lên tối đa 1 GB

jobs = {}  # id -> dict
lock = threading.Lock()
work_q = queue.Queue()
procs = {}  # id -> Popen

STEPS = [
    ("download", ("Đang tải", "Dùng file video có sẵn")),
    ("transcribe", ("Nhận dạng giọng nói",)),
    ("translate", ("Đang dịch", "Dịch bằng AI")),
    ("dub", ("Lồng tiếng",)),
    ("render", ("Đang xuất video",)),
]
GEMINI_PICK = ("Aoede", "Kore", "Zephyr", "Leda", "Despina", "Charon", "Puck", "Orus", "Fenrir", "Algenib")
PUBLIC = ("id", "source", "title", "status", "step", "created", "finished", "options", "files", "error", "progress", "queue_pos")


def save(job):
    d = JOBS_DIR / job["id"]
    d.mkdir(exist_ok=True)
    data = {k: job.get(k) for k in PUBLIC if k != "queue_pos"}
    data["log"] = job.get("log", [])[-400:]
    (d / "job.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_jobs():
    for d in JOBS_DIR.iterdir():
        f = d / "job.json"
        if f.is_file():
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if j.get("status") in ("queued", "running"):
                j["status"], j["error"] = "error", "Server đã tắt khi đang xử lý. Hãy chạy lại."
            jobs[j["id"]] = j


def find_files(job_dir):
    out = {}
    for p in job_dir.iterdir():
        n = p.name
        if n.endswith(".vi.mp4"):
            out["video"] = n
        elif n.endswith(".goc.mp4"):
            out["original"] = n
        elif n.endswith(".vi.srt"):
            out["srt_vi"] = n
        elif n.endswith(".zh.srt"):
            out["srt_zh"] = n
    return out


def parse_srt(path):
    if not path.is_file():
        return []
    blocks = re.split(r"\r?\n\r?\n", path.read_text(encoding="utf-8").strip())
    res = []
    for b in blocks:
        lines = b.splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            res.append({"time": lines[1].split(" --> ")[0][:8], "text": " ".join(lines[2:])})
    return res


def run_job(job):
    jid = job["id"]
    jdir = JOBS_DIR / jid
    opt = job["options"]
    cmd = [sys.executable, "-u", str(ROOT / "douyin_dub.py"), job["source_arg"], "-o", str(jdir),
           "--voice", opt["voice"], "--model", opt["model"], "--bg-volume", str(opt["bg_volume"]),
           "--font-size", str(opt["font_size"]), "--tts", opt.get("tts", "edge")]
    if opt.get("tts") == "gemini" and opt.get("gemini_voice"):
        cmd += ["--gemini-voice", opt["gemini_voice"]]
    if opt.get("tts") == "eleven":
        cmd += ["--eleven-model", opt.get("eleven_model", "v3")]
        if opt.get("eleven_voice"):
            cmd += ["--eleven-voice", opt["eleven_voice"]]
    if not opt["dub"]:
        cmd.append("--no-dub")
    if opt["burn"]:
        cmd.append("--subs")
    if not opt.get("cover", True):
        cmd += ["--cover-subs", "off"]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", HF_HUB_DISABLE_SYMLINKS_WARNING="1")
    with lock:
        job.update(status="running", step="download")
    save(job)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", env=env, cwd=str(ROOT),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    procs[jid] = p
    last_err = ""
    for raw in p.stdout:
        line = raw.rstrip()
        if not line.strip() or "huggingface" in line.lower() or "warnings.warn" in line or "HF_TOKEN" in line \
                or "symlink" in line.lower() or "developer mode" in line.lower():
            continue
        with lock:
            m = re.match(r"\s*(\d+)/(\d+)\s*$", line)
            if m:  # tiến độ lồng tiếng
                job["progress"] = f"{m.group(1)}/{m.group(2)}"
                continue
            job["log"].append(line)
            if len(job["log"]) > 2000:
                job["log"] = job["log"][-1500:]
            for key, marks in STEPS:
                if any(mk in line for mk in marks):
                    job["step"], job["progress"] = key, ""
            if "Tiêu đề:" in line:
                job["title"] = line.split("Tiêu đề:", 1)[1].strip()
            # lỗi Python ("RuntimeError: ...") hoặc thông báo sys.exit() tiếng Việt
            if re.match(r"^\w+(Error|Exception):", line) or (
                    not line.startswith((" ", "[douyin-dub]", "Traceback")) and line.startswith(("Không", "Chưa"))):
                last_err = line
    code = p.wait()
    procs.pop(jid, None)
    for t in jdir.glob("_tmp_*"):  # dọn thư mục tạm nếu bị huỷ / lỗi giữa chừng
        shutil.rmtree(t, ignore_errors=True)
    with lock:
        job["finished"] = time.time()
        job["files"] = find_files(jdir)
        if job.get("status") == "cancelled":
            pass
        elif code == 0 and job["files"].get("video"):
            job["status"], job["step"] = "done", "done"
            if not job.get("title"):
                job["title"] = job["files"]["video"].rsplit(".vi.mp4", 1)[0]
        else:
            job["status"] = "error"
            msg = last_err or next((l for l in reversed(job["log"]) if l.strip()), "Lỗi không rõ")
            job["error"] = re.sub(r"^\w+Error:\s*", "", msg)[:400]
    save(job)


def worker():
    while True:
        jid = work_q.get()
        job = jobs.get(jid)
        if job and job["status"] == "queued":
            try:
                run_job(job)
            except Exception as e:  # không để worker chết
                with lock:
                    job.update(status="error", error=str(e), finished=time.time())
                save(job)
        work_q.task_done()


def public(job, full=False):
    d = {k: job.get(k) for k in PUBLIC}
    if job["status"] == "queued":
        waiting = [j for j in jobs.values() if j["status"] == "queued" and j["created"] < job["created"]]
        d["queue_pos"] = len(waiting) + 1
    if full:
        d["log"] = job.get("log", [])[-300:]
        jdir = JOBS_DIR / job["id"]
        f = job.get("files") or {}
        if f.get("srt_vi"):
            vi, zh = parse_srt(jdir / f["srt_vi"]), parse_srt(jdir / f.get("srt_zh", "_"))
            d["lines"] = [{"time": v["time"], "vi": v["text"], "zh": zh[i]["text"] if i < len(zh) else ""}
                          for i, v in enumerate(vi)]
    return d


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/jobs")
def list_jobs():
    with lock:
        items = sorted(jobs.values(), key=lambda j: j["created"], reverse=True)
        return jsonify([public(j) for j in items])


@app.get("/api/jobs/<jid>")
def get_job(jid):
    with lock:
        job = jobs.get(jid) or abort(404)
        return jsonify(public(job, full=True))


@app.post("/api/jobs")
def create_job():
    f = request.form
    link = (f.get("link") or "").strip()
    upload = request.files.get("file")
    if not link and not (upload and upload.filename):
        return jsonify(error="Hãy dán link Douyin hoặc chọn một file video."), 400
    if link and not re.search(r"https?://", link):
        return jsonify(error="Không thấy link trong nội dung bạn dán."), 400
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    jdir = JOBS_DIR / jid
    jdir.mkdir(parents=True)
    if upload and upload.filename:
        ext = Path(upload.filename).suffix.lower() or ".mp4"
        src_path = jdir / f"upload{ext}"
        upload.save(src_path)
        source_arg, source, title = str(src_path), upload.filename, Path(upload.filename).stem
    else:
        # chỉ giữ phần link trong đoạn chia sẻ để hiển thị gọn
        source_arg = source = re.search(r"https?://[^\s，。]+", link).group(0)
        title = ""

    def num(name, default, lo, hi, cast=float):
        try:
            return min(max(cast(f.get(name, default)), lo), hi)
        except (TypeError, ValueError):
            return default

    model = f.get("model", "medium")
    options = {
        "voice": f.get("voice") if f.get("voice") in ("auto", "nam", "nu") else "auto",
        "tts": f.get("tts") if f.get("tts") in ("gemini", "eleven") else "edge",
        "eleven_voice": f.get("eleven_voice", "") if re.fullmatch(r"[A-Za-z0-9]{8,40}", f.get("eleven_voice", "")) else "",
        "eleven_model": "flash" if f.get("eleven_model") == "flash" else "v3",
        "gemini_voice": f.get("gemini_voice") if f.get("gemini_voice") in GEMINI_PICK else "",
        "model": model if model in ("tiny", "base", "small", "medium", "large-v3") else "medium",
        "dub": f.get("dub", "1") == "1",
        "burn": f.get("burn", "0") == "1",
        "cover": f.get("cover", "1") == "1",
        "bg_volume": num("bg_volume", 0.15, 0, 1),
        "font_size": num("font_size", 13, 8, 30, int),
    }
    job = {"id": jid, "source": source, "source_arg": source_arg, "title": title, "status": "queued",
           "step": "", "progress": "", "created": time.time(), "finished": None, "options": options,
           "files": {}, "error": "", "log": []}
    with lock:
        jobs[jid] = job
    save(job)
    work_q.put(jid)
    return jsonify(public(job)), 201


@app.post("/api/jobs/<jid>/cancel")
def cancel_job(jid):
    with lock:
        job = jobs.get(jid) or abort(404)
        if job["status"] not in ("queued", "running"):
            return jsonify(public(job))
        job.update(status="cancelled", error="Đã huỷ", finished=time.time())
    p = procs.get(jid)
    if p:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True) if os.name == "nt" else p.kill()
    save(job)
    return jsonify(public(job))


@app.delete("/api/jobs/<jid>")
def delete_job(jid):
    with lock:
        job = jobs.get(jid) or abort(404)
        if job["status"] == "running":
            return jsonify(error="Đang xử lý, hãy huỷ trước."), 409
        jobs.pop(jid)
    shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
    return jsonify(ok=True)


@app.post("/api/jobs/<jid>/open-folder")
def open_folder(jid):
    if jid not in jobs:
        abort(404)
    if os.name == "nt":
        os.startfile(str(JOBS_DIR / jid))
    return jsonify(ok=True)


CONFIG_PATH = ROOT / "config.json"
AI_PROVIDERS = ("gemini", "claude", "openai", "deepseek")


def read_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def public_config(cfg):
    key = cfg.get("ai_key", "")
    ek = cfg.get("eleven_key", "")
    return {"provider": cfg.get("ai_provider", "gemini"), "model": cfg.get("ai_model", ""),
            "has_key": len(key) > 10, "key_hint": (key[:4] + "••••" + key[-4:]) if len(key) > 10 else "",
            "eleven_has_key": len(ek) > 10, "eleven_hint": (ek[:4] + "••••" + ek[-4:]) if len(ek) > 10 else ""}


@app.get("/api/config")
def get_config():
    return jsonify(public_config(read_config()))


@app.post("/api/config")
def set_config():
    body = request.get_json(force=True) or {}
    cfg = read_config()
    if body.get("provider") in AI_PROVIDERS:
        cfg["ai_provider"] = body["provider"]
    if "model" in body:
        cfg["ai_model"] = str(body["model"]).strip()
    if body.get("clear_key"):
        cfg.pop("ai_key", None)
    elif str(body.get("key", "")).strip():
        cfg["ai_key"] = str(body["key"]).strip()
    if body.get("clear_eleven_key"):
        cfg.pop("eleven_key", None)
    elif str(body.get("eleven_key", "")).strip():
        cfg["eleven_key"] = str(body["eleven_key"]).strip()
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify(public_config(cfg))


@app.get("/api/eleven/voices")
def eleven_voices():
    """Danh sách giọng + số credit còn lại của tài khoản ElevenLabs (cũng là cách kiểm tra key)."""
    sys.path.insert(0, str(ROOT))
    import eleven_tts

    kc = eleven_tts.load_config()
    if not kc:
        return jsonify(ok=False, error="Chưa có API key ElevenLabs."), 400
    try:
        vs = eleven_tts.voices(kc["key"])
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 400
    c = eleven_tts.credits(kc["key"])
    return jsonify(ok=True, voices=vs, used=c[0] if c else None, limit=c[1] if c else None)


@app.post("/api/config/test")
def test_config():
    sys.path.insert(0, str(ROOT))
    import douyin_dub

    cfg = douyin_dub.load_ai_config()
    if not cfg:
        return jsonify(ok=False, error="Chưa có API key."), 400
    try:
        out = douyin_dub.ai_chat(cfg, 'Chỉ trả về JSON {"lines":[{"i":0,"vi":"..."}]}',
                                 '[{"i":0,"zh":"你好，明哥","max":6}]', timeout=30)
        if cfg["model"] != (read_config().get("ai_model") or ""):  # nhớ model chạy được
            c = read_config()
            c["ai_model"] = cfg["model"]
            CONFIG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
        vi = douyin_dub._parse_json(out)["lines"][0]["vi"]
        return jsonify(ok=True, sample=f"你好，明哥 → {vi}", model=cfg["model"])
    except Exception as e:
        msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            msg = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        return jsonify(ok=False, error=msg[:400]), 400


@app.get("/files/<jid>/<path:name>")
def files(jid, name):
    if jid not in jobs:
        abort(404)
    return send_from_directory(JOBS_DIR / jid, name, as_attachment=request.args.get("dl") == "1")


if __name__ == "__main__":
    load_jobs()
    threading.Thread(target=worker, daemon=True).start()
    print(f"Douyin Dub web đang chạy: http://localhost:{PORT}")
    if "--open" in sys.argv:
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)
