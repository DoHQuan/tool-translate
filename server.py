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
    ("split", ("Đang tách video",)),
    ("transcribe", ("Nhận dạng giọng nói",)),
    ("translate", ("Đang dịch", "Dịch bằng AI")),
    ("dub", ("Lồng tiếng",)),
    ("render", ("Đang xuất video",)),
]
GEMINI_PICK = ("Aoede", "Kore", "Zephyr", "Leda", "Despina", "Charon", "Puck", "Orus", "Fenrir", "Algenib")
PUBLIC = ("id", "source", "title", "title_vi", "status", "step", "created", "finished", "options", "files", "error", "progress",
          "queue_pos", "kind", "parts", "parent", "part", "estimate", "can_resume")
PROFILE_DIR = ROOT / "profile"
# Độ kỹ khi nghe trên máy (beam search). Đo trên máy người dùng: xem .tmp/bench_asr.py
# medium beam 2 cho chữ y hệt beam 5 mà nhanh hơn 2,2 lần (349 s -> 157 s cho 60 s tiếng trên Core i5-1235U)
ASR_BEAM = {"small": 2, "medium": 2}


def save(job):
    d = JOBS_DIR / job["id"]
    d.mkdir(exist_ok=True)
    data = {k: job.get(k) for k in PUBLIC if k != "queue_pos"}
    data["log"] = job.get("log", [])[-400:]
    for k in ("source_arg", "title_hint"):  # cần để chạy tiếp sau khi server khởi động lại
        data[k] = job.get(k)
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
        elif n.endswith(".bia.jpg"):
            out["cover"] = n
        elif n.endswith(".tiktok.json"):
            out["post"] = n
    return out


def parse_srt(path):
    if not path.is_file():
        return []
    blocks = re.split(r"\r?\n\r?\n", path.read_text(encoding="utf-8").strip())
    res = []
    for b in blocks:
        lines = b.splitlines()
        if len(lines) >= 2 and "-->" in lines[1]:
            try:
                n = int(lines[0].strip()) - 1  # số thứ tự trong file = vị trí câu trong bản dịch
            except ValueError:
                n = len(res)
            res.append({"n": n, "time": lines[1].split(" --> ")[0][:8], "text": " ".join(lines[2:])})
    return res


def run_job(job):
    jid = job["id"]
    jdir = JOBS_DIR / jid
    opt = job["options"]
    is_split = job.get("kind") == "split"
    cmd = [sys.executable, "-u", str(ROOT / "douyin_dub.py"), job["source_arg"], "-o", str(jdir),
           "--voice", opt["voice"], "--model", opt["model"], "--bg-volume", str(opt["bg_volume"]),
           "--asr", opt.get("asr", "local"), "--beam", str(ASR_BEAM.get(opt["model"], 5)),
           "--font-size", str(opt["font_size"]), "--tts", opt.get("tts", "edge")]
    parts_found = []
    if opt.get("tts") == "gemini" and opt.get("gemini_voice"):
        cmd += ["--gemini-voice", opt["gemini_voice"]]
    if opt.get("tts") == "vieneu" and opt.get("vieneu_voice"):
        cmd += ["--vieneu-voice", opt["vieneu_voice"]]
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
    if not opt.get("post", True):
        cmd.append("--no-cover")
    if opt.get("vertical"):
        cmd.append("--vertical")
    if opt.get("series"):
        cmd += ["--series", opt["series"]]
    if opt.get("retranslate"):
        cmd.append("--retranslate")
    if opt.get("confirm") and opt["dub"]:
        cmd.append("--confirm-dub")
    if job.pop("fresh", False):
        cmd.append("--fresh")
    estimate = None
    if job.get("part"):
        cmd += ["--part", f"{job['part']['n']}/{job['part']['of']}"]
    if job.get("title_hint"):
        cmd += ["--title-hint", job["title_hint"]]
    if is_split:  # video dài: chỉ tải về và tách, mỗi phần sau đó thành một job riêng
        cmd = [sys.executable, "-u", str(ROOT / "splitter.py"), job["source_arg"], "-o", str(jdir),
               "--minutes", str(opt.get("split", 5))]
    if not is_split:  # chat2api có thể đã tắt từ lúc bật server -> bật lại trước mỗi video, nếu không sẽ rơi về Google Dịch
        try:
            from chat2api_runtime import ensure_chat2api

            ensure_chat2api()
        except Exception as e:
            with lock:
                job["log"].append(f"[douyin-dub] Không bật được chat2api ({str(e)[:150]}) -> dịch bằng Google Dịch")
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
        if line.startswith("ESTIMATE_JSON: "):
            try:
                estimate = json.loads(line[len("ESTIMATE_JSON: "):])
            except Exception:
                pass
            continue
        if line.startswith("PARTS_JSON: "):
            try:
                parts_found = json.loads(line[len("PARTS_JSON: "):])
            except Exception:
                pass
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
            if "Tiêu đề Việt:" in line:
                job["title_vi"] = line.split("Tiêu đề Việt:", 1)[1].strip()
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
        elif is_split and code == 0 and parts_found:
            job["status"], job["step"] = "done", "done"
        elif code == 0 and estimate and not job["files"].get("video"):  # dừng chờ duyệt bản dịch + chi phí
            job["status"], job["step"], job["estimate"] = "confirm", "translate", estimate
        elif code == 0 and job["files"].get("video"):
            job["status"], job["step"] = "done", "done"
            if not job.get("title"):
                job["title"] = job["files"]["video"].rsplit(".vi.mp4", 1)[0]
        else:
            job["status"] = "error"
            msg = last_err or next((l for l in reversed(job["log"]) if l.strip()), "Lỗi không rõ")
            job["error"] = re.sub(r"^\w+Error:\s*", "", msg)[:400]
    if is_split and job["status"] == "done":
        spawn_parts(job, parts_found)
    save(job)


def spawn_parts(parent, parts):
    """Mỗi phần đã tách thành một job lồng tiếng riêng, xếp hàng chạy lần lượt với cùng tuỳ chọn."""
    n = len(parts)
    opt = {k: v for k, v in parent["options"].items() if k != "split"}
    title = parent.get("title") or Path(parts[0]["path"]).stem
    title = re.sub(r"\s*[#@].*", "", title).strip()[:60] or "Video"
    # các phần của cùng một video dài = cùng một bộ phim -> giữ tên, xưng hô, giọng giống nhau giữa các phần
    opt["series"] = opt.get("series") or title
    ids = []
    for k, part in enumerate(parts, 1):
        jid = f"{parent['id']}-p{k:02d}"
        jdir = JOBS_DIR / jid
        jdir.mkdir(parents=True, exist_ok=True)
        src = Path(part["path"])
        # tên file theo tiêu đề (file tải lên vốn chỉ tên là 'upload')
        safe = re.sub(r'[^\w \-.()]+', ' ', title)[:50].strip() or 'Video'
        dst = jdir / ((f'{safe} - Phan {k:02d}' if n > 1 else safe) + src.suffix)
        shutil.move(str(src), str(dst))
        job = {"id": jid, "source": parent["source"], "source_arg": str(dst),
               "title": f"{title} — Phần {k}/{n}" if n > 1 else title, "status": "queued", "step": "",
               "progress": "", "created": parent["created"] + k * 0.001, "finished": None, "options": dict(opt),
               "files": {}, "error": "", "log": [], "kind": "part", "parent": parent["id"], "title_hint": title,
               "part": {"n": k, "of": n, "start": part.get("start"), "end": part.get("end"), "speech": part.get("speech")}}
        with lock:
            jobs[jid] = job
        save(job)
        work_q.put(jid)
        ids.append(jid)
    shutil.rmtree(JOBS_DIR / parent["id"] / "parts", ignore_errors=True)
    with lock:
        parent["parts"] = ids


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
    d["can_resume"] = bool(job.get("source_arg")) and job.get("kind") != "split" \
        and job["status"] in ("error", "cancelled", "confirm") and (JOBS_DIR / job["id"] / "_resume").is_dir()
    if job["status"] == "queued":
        waiting = [j for j in jobs.values() if j["status"] == "queued" and j["created"] < job["created"]]
        d["queue_pos"] = len(waiting) + 1
    if full and job.get("parts"):
        d["part_jobs"] = [{"id": i, "title": jobs[i].get("title"), "status": jobs[i].get("status"),
                           "step": jobs[i].get("step"), "part": jobs[i].get("part")} for i in job["parts"] if i in jobs]
    if full:
        d["log"] = job.get("log", [])[-300:]
        jdir = JOBS_DIR / job["id"]
        f = job.get("files") or {}
        if job["status"] == "done" and jdir.is_dir():  # file sinh thêm sau (ảnh bìa, tiêu đề) cũng hiện ra
            f = job["files"] = d["files"] = find_files(jdir)
        if f.get("post"):
            try:
                d["post"] = json.loads((jdir / f["post"]).read_text(encoding="utf-8"))
            except Exception:
                pass
        if f.get("srt_vi"):
            vi, zh = parse_srt(jdir / f["srt_vi"]), parse_srt(jdir / f.get("srt_zh", "_"))
            zh_by = {z["n"]: z["text"] for z in zh}
            d["lines"] = [{"i": v["n"], "time": v["time"], "vi": v["text"], "zh": zh_by.get(v["n"], "")}
                          for v in vi]
        d["can_edit"] = job["status"] == "done" and job.get("kind") != "split" \
            and (jdir / "_resume" / "segs_vi.json").is_file()
    return d


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/jobs")
def list_jobs():
    with lock:
        items = sorted(jobs.values(), key=lambda j: j["created"], reverse=True)
        return jsonify([public(j) for j in items])


@app.post("/api/search")
def search_video():
    from video_search import search_videos

    body = request.get_json(silent=True) or {}
    keyword = str(body.get("keyword") or "").strip()
    source = body.get("source", "douyin")
    if source not in ("douyin", "bilibili") or not keyword or len(keyword) > 150:
        return jsonify(error="Chọn Douyin/Bilibili và nhập chủ đề từ 1–150 ký tự."), 400
    try:
        return jsonify(search_videos(source, keyword, body.get("translate") is True))
    except Exception:
        from video_search import search_url
        return jsonify(error="Không kết nối được nguồn tìm kiếm hoặc Chromium đang bận. Thử lại hoặc mở trang nguồn.",
                       search_url=search_url(source, keyword)), 503


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
    asr = "eleven" if model == "cloud" else "local"  # "Đám mây": nghe bằng ElevenLabs, lỗi thì tự nghe trên máy (medium)
    if model == "cloud":
        model = "medium"
    options = {
        "asr": asr,
        "voice": f.get("voice") if f.get("voice") in ("auto", "nam", "nu") else "auto",
        "tts": f.get("tts") if f.get("tts") in ("gemini", "eleven", "vieneu") else "edge",
        "vieneu_voice": f.get("vieneu_voice", "")[:30] if re.fullmatch(r"[\w ]{2,30}", f.get("vieneu_voice", "")) else "",
        "eleven_voice": f.get("eleven_voice", "") if re.fullmatch(r"[A-Za-z0-9]{8,40}", f.get("eleven_voice", "")) else "",
        "eleven_model": "flash" if f.get("eleven_model") == "flash" else "v3",
        "gemini_voice": f.get("gemini_voice") if f.get("gemini_voice") in GEMINI_PICK else "",
        "model": model if model in ("tiny", "base", "small", "medium", "large-v3") else "medium",
        "dub": f.get("dub", "1") == "1",
        "burn": f.get("burn", "0") == "1",
        "cover": f.get("cover", "1") == "1",
        "post": f.get("post", "1") == "1",
        "vertical": f.get("vertical", "0") == "1",
        "retranslate": f.get("retranslate", "0") == "1",
        "series": re.sub(r"\s+", " ", f.get("series", "")).strip()[:60],
        "confirm": f.get("confirm", "0") == "1",
        "bg_volume": num("bg_volume", 0.15, 0, 1),
        "font_size": num("font_size", 13, 8, 30, int),
    }
    split = num("split", 0, 0, 30)
    if split >= 1:
        options["split"] = split
    job = {"id": jid, "source": source, "source_arg": source_arg, "title": title, "status": "queued",
           "step": "", "progress": "", "created": time.time(), "finished": None, "options": options,
           "files": {}, "error": "", "log": [], "kind": "split" if options.get("split") else ""}
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


@app.get("/api/series")
def list_series():
    """Các bộ phim đã có hồ sơ (tên + số nhân vật), để chọn lại cho tập sau."""
    out = []
    folder = ROOT / "series"
    for f in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"name": d.get("name") or f.stem, "characters": [c.get("name") for c in d.get("characters", [])],
                        "updated": f.stat().st_mtime})
        except Exception:
            continue
    return jsonify(sorted(out, key=lambda x: -x["updated"]))


@app.post("/api/jobs/<jid>/reformat")
def reformat_job(jid):
    """Xuất lại video đã xong với khung khác (ngang <-> dọc 9:16): dùng lại bản dịch, tiếng đã lồng, tiêu đề."""
    body = request.get_json(silent=True) or {}
    with lock:
        job = jobs.get(jid) or abort(404)
        state = JOBS_DIR / jid / "_resume"
        if job["status"] != "done" or job.get("kind") == "split" or not (state / "segs_vi.json").is_file():
            return jsonify(error="Video này chưa xuất lại được (làm trước khi có chức năng này, hoặc đang xử lý). "
                                 "Hãy tạo lại video một lần, sau đó đổi khung được."), 409
        job["options"]["vertical"] = bool(body.get("vertical"))
        job["options"]["confirm"] = False
        job.update(status="queued", step="", progress="", error="", finished=None)
        job.setdefault("log", []).append("[douyin-dub] ---- Xuất lại: "
                                         + ("khung dọc 9:16" if job["options"]["vertical"] else "khung gốc (ngang)") + " ----")
    save(job)
    work_q.put(jid)
    return jsonify(public(job))


@app.post("/api/jobs/<jid>/edit")
def edit_lines(jid):
    """Sửa lời Việt của một số câu rồi lồng tiếng lại: chỉ những câu bị sửa được đọc lại, các câu khác dùng lại."""
    body = request.get_json(silent=True) or {}
    raw = body.get("lines") or {}
    with lock:
        job = jobs.get(jid) or abort(404)
        state = JOBS_DIR / jid / "_resume"
        if job["status"] != "done" or not (state / "segs_vi.json").is_file():
            return jsonify(error="Video này chưa sửa được từng câu (làm trước khi có chức năng này, hoặc đang xử lý). "
                                 "Hãy chạy lại video một lần, sau đó sửa được."), 409
        data = json.loads((state / "segs_vi.json").read_text(encoding="utf-8"))
        segs = data.get("segs") or []
        edits = {}
        for k, v in raw.items():
            try:
                i = int(k)
            except (TypeError, ValueError):
                continue
            text = re.sub(r"\s+", " ", str(v or "")).strip()[:400]
            if 0 <= i < len(segs):
                edits[i] = text
        if not edits:
            return jsonify(error="Không có câu nào được sửa."), 400
        for i, text in edits.items():
            segs[i]["vi"] = text
        (state / "segs_vi.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (state / "segs_dub.json").unlink(missing_ok=True)  # buộc lồng tiếng lại; câu không sửa vẫn dùng lại tiếng cũ
        try:  # nhớ bản đã sửa cho video này, lần sau chạy lại cùng video vẫn giữ lời đã sửa
            import douyin_dub

            src = json.loads((state / "source.json").read_text(encoding="utf-8"))
            cache = douyin_dub.CACHE_DIR / douyin_dub.video_key(src["video"]) / f"vi_{job['options'].get('model', 'medium')}_zh.json"
            if cache.is_file():
                c = json.loads(cache.read_text(encoding="utf-8"))
                if len(c.get("segs") or []) == len(segs):
                    for i, text in edits.items():
                        c["segs"][i]["vi"] = text
                    c["edited"] = True
                    cache.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        job["options"]["confirm"] = False
        job.update(status="queued", step="", progress="", error="", finished=None)
        job.setdefault("log", []).append(f"[douyin-dub] ---- Sửa {len(edits)} câu, đọc lại riêng các câu đó ----")
    save(job)
    work_q.put(jid)
    return jsonify(public(job))


@app.post("/api/jobs/<jid>/resume")
def resume_job(jid):
    """Chạy tiếp một video bị lỗi / bị huỷ / đang chờ duyệt: dùng lại phần đã làm (video, lời nghe, bản dịch, câu đã đọc)."""
    body = request.get_json(silent=True) or {}
    with lock:
        job = jobs.get(jid) or abort(404)
        src = job.get("source_arg") or ""
        if job["status"] not in ("error", "cancelled", "confirm") or not src or job.get("kind") == "split":
            return jsonify(error="Video này không chạy tiếp được, hãy tạo lại từ đầu."), 409
        if not re.match(r"https?://", src) and not Path(src).is_file():
            return jsonify(error="Không còn file video gốc, hãy tải file lên lại."), 409
        if job["status"] == "confirm":
            job["options"]["confirm"] = False
        if body.get("fresh"):
            job["fresh"] = True
        job.update(status="queued", step="", progress="", error="", finished=None)
        job.setdefault("log", []).append("[douyin-dub] ---- Chạy tiếp ----")
    save(job)
    work_q.put(jid)
    return jsonify(public(job))


def public_profile(cfg):
    pr = cfg.get("profile") or {}
    return {"channel": pr.get("channel", ""), "hashtags": pr.get("hashtags", ""), "logo_on": bool(pr.get("logo_on")),
            "logo_pos": pr.get("logo_pos", "tr"), "logo_size": pr.get("logo_size", 12),
            "has_logo": (PROFILE_DIR / "logo.png").is_file()}


@app.get("/api/profile")
def get_profile():
    return jsonify(public_profile(read_config()))


@app.post("/api/profile")
def set_profile():
    """Hồ sơ kênh: tên kênh, hashtag mặc định, logo và vị trí đặt. Áp dụng cho mọi video chạy sau đó."""
    f = request.form
    cfg = read_config()
    pr = cfg.get("profile") or {}
    pr["channel"] = f.get("channel", "").strip().lstrip("@")[:40]
    pr["hashtags"] = " ".join(re.split(r"[\s,#]+", f.get("hashtags", "").strip()))[:200].strip()
    pr["logo_on"] = f.get("logo_on") == "1"
    pr["logo_pos"] = f.get("logo_pos") if f.get("logo_pos") in ("tl", "tr", "bl", "br") else "tr"
    try:
        pr["logo_size"] = min(30, max(5, int(float(f.get("logo_size", 12)))))
    except (TypeError, ValueError):
        pr["logo_size"] = 12
    up = request.files.get("logo")
    if up and up.filename:
        try:
            from PIL import Image

            im = Image.open(up.stream).convert("RGBA")
            im.thumbnail((600, 600))
            PROFILE_DIR.mkdir(exist_ok=True)
            im.save(PROFILE_DIR / "logo.png")
        except Exception:
            return jsonify(error="Không đọc được file logo. Hãy dùng ảnh PNG hoặc JPG."), 400
    if f.get("remove_logo") == "1":
        (PROFILE_DIR / "logo.png").unlink(missing_ok=True)
    cfg["profile"] = pr
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify(public_profile(cfg))


@app.get("/profile/logo.png")
def profile_logo():
    if not (PROFILE_DIR / "logo.png").is_file():
        abort(404)
    r = send_from_directory(PROFILE_DIR, "logo.png")
    r.headers["Cache-Control"] = "no-store"
    return r


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
AI_PROVIDERS = ("gemini", "claude", "openai", "deepseek", "chat2api")


def read_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    # chuyển cấu hình cũ (một key duy nhất) sang dạng mỗi nhà cung cấp một key
    keys, models = cfg.setdefault("ai_keys", {}), cfg.setdefault("ai_models", {})
    p = cfg.get("ai_provider")
    if p and cfg.get("ai_key") and p not in keys:
        keys[p] = cfg["ai_key"]
    if p and cfg.get("ai_model") and p not in models:
        models[p] = cfg["ai_model"]
    return cfg


def public_config(cfg):
    key = cfg.get("ai_key", "")
    ek = cfg.get("eleven_key", "")
    return {"provider": cfg.get("ai_provider", "gemini"), "model": cfg.get("ai_model", ""),
            "base_url": cfg.get("ai_base_url", "http://127.0.0.1:5005/v1"),
            "has_key": bool(key) if cfg.get("ai_provider") == "chat2api" else len(key) > 10,
            "key_hint": "••••" if cfg.get("ai_provider") == "chat2api" and key else (key[:4] + "••••" + key[-4:]) if len(key) > 10 else "",
            "eleven_has_key": len(ek) > 10, "eleven_hint": (ek[:4] + "••••" + ek[-4:]) if len(ek) > 10 else "",
            # nhà cung cấp nào đã có key lưu sẵn (không trả key ra ngoài)
            "saved": sorted(p for p, k in (cfg.get("ai_keys") or {}).items() if k)}


@app.get("/api/config")
def get_config():
    return jsonify(public_config(read_config()))


@app.post("/api/config")
def set_config():
    body = request.get_json(force=True) or {}
    cfg = read_config()
    if "base_url" in body:
        from douyin_dub import normalize_chat2api_url
        try:
            cfg["ai_base_url"] = normalize_chat2api_url(body["base_url"] or "http://127.0.0.1:5005/v1")
        except ValueError as e:
            return jsonify(error=str(e)), 400
    keys, models = cfg.setdefault("ai_keys", {}), cfg.setdefault("ai_models", {})
    switched = body.get("provider") in AI_PROVIDERS and body["provider"] != cfg.get("ai_provider")
    if body.get("provider") in AI_PROVIDERS:
        cfg["ai_provider"] = body["provider"]
    p = cfg.get("ai_provider", "gemini")
    # vừa đổi nhà cung cấp mà ô model để trống -> giữ model đã lưu của nhà cung cấp đó (ô model bị xoá khi đổi)
    if "model" in body and (str(body["model"]).strip() or not switched):
        models[p] = str(body["model"]).strip()
    if body.get("clear_key"):
        keys.pop(p, None)
    elif str(body.get("key", "")).strip():
        keys[p] = str(body["key"]).strip()
    # key / model đang dùng = của nhà cung cấp đang chọn (key của nhà cung cấp khác vẫn được giữ)
    cfg["ai_key"] = keys.get(p, "")
    cfg["ai_model"] = models.get(p, "")
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

    try:
        from chat2api_runtime import ensure_chat2api

        ensure_chat2api()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 400
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
    from chat2api_runtime import ensure_chat2api

    try:
        ensure_chat2api()
    except Exception as e:
        print(f"Không khởi động được dịch AI: {e}", flush=True)
    load_jobs()
    threading.Thread(target=worker, daemon=True).start()
    print(f"Douyin Dub web đang chạy: http://localhost:{PORT}")
    if "--open" in sys.argv:
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)
