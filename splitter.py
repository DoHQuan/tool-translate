#!/usr/bin/env python3
"""
splitter.py - Tách một video dài thành nhiều phần ngắn, cắt ở chỗ KHÔNG có lời nói.

    python splitter.py <link hoặc file> -o <thư mục> --minutes 5

Cách làm: dò các đoạn có tiếng người (Silero VAD, không phụ thuộc nhạc nền), chỉ cho cắt tại các khung hình khoá
(keyframe) nằm trong khoảng lặng lời, rồi chọn bộ điểm cắt sao cho các phần dài gần bằng nhau. Cắt kiểu sao chép
luồng (không nén lại) nên nhanh và giữ nguyên chất lượng hình.
"""
import argparse
import bisect
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import douyin_dub as d

SPEECH_PENALTY = 1e6  # cắt ngang lời nói: chỉ khi không còn cách nào khác


def speech_gaps(video, workdir):
    """Trả về (tổng thời lượng, danh sách đoạn có lời [(bắt đầu, kết thúc)])."""
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    wav = Path(workdir) / "split16k.wav"
    d.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    with wave.open(str(wav), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    wav.unlink(missing_ok=True)
    ts = get_speech_timestamps(pcm, VadOptions(min_silence_duration_ms=400, speech_pad_ms=100))
    return len(pcm) / 16000, [(t["start"] / 16000, t["end"] / 16000) for t in ts]


def keyframes(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags",
                          "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout
    ks = []
    for line in out.splitlines():
        p = line.split(",")
        if len(p) >= 2 and "K" in p[1]:
            try:
                ks.append(float(p[0]))
            except ValueError:
                pass
    return sorted(ks)


def plan_cuts(total, speech, kfs, minutes):
    """Chọn điểm cắt. Trả về (danh sách mốc [0, c1, ..., total], số điểm phải cắt ngang lời)."""
    target = minutes * 60
    n = max(1, round(total / target))
    if n == 1 or total < target * 1.4:
        return [0.0, total], 0
    ideal = total / n
    starts = [s for s, _ in speech]

    def margin(t):  # khoảng cách tới lời nói gần nhất; âm = đang nằm trong lời nói
        i = bisect.bisect_right(starts, t) - 1
        if i >= 0 and t < speech[i][1]:
            return -1.0
        prev_end = speech[i][1] if i >= 0 else -1e9
        next_start = speech[i + 1][0] if i + 1 < len(speech) else 1e9
        return min(t - prev_end, next_start - t)

    pts = [(0.0, 99.0)] + [(k, margin(k)) for k in kfs if 5 < k < total - 5] + [(total, 99.0)]
    times = [p[0] for p in pts]

    def pen(m):
        if m < 0:
            return SPEECH_PENALTY
        if m < 0.4:
            return SPEECH_PENALTY / 4  # sát mép lời nói
        return -40 * min(m, 10)  # càng xa lời nói càng tốt

    lo, hi = ideal * 0.6, ideal * 1.45
    last = len(pts) - 1
    best = [dict() for _ in range(n + 1)]
    best[0][0] = (0.0, None)
    for j in range(1, n + 1):
        cand = [last] if j == n else range(1, last)
        for i in cand:
            t = times[i]
            a, b = bisect.bisect_left(times, t - hi), bisect.bisect_right(times, t - lo)
            bc = None
            for p in range(a, min(b, i)):
                if p not in best[j - 1]:
                    continue
                cost = best[j - 1][p][0] + (t - times[p] - ideal) ** 2 + (pen(pts[i][1]) if i != last else 0)
                if bc is None or cost < bc[0]:
                    bc = (cost, p)
            if bc:
                best[j][i] = bc
    if last not in best[n]:  # không xếp được (video quá ít keyframe) -> chia đều theo keyframe gần nhất
        cuts = [0.0] + [min(kfs, key=lambda k: abs(k - ideal * j)) for j in range(1, n)] + [total]
        cuts = sorted(set(cuts))
        return cuts, sum(margin(c) < 0 for c in cuts[1:-1])
    cuts, i = [], last
    for j in range(n, 0, -1):
        cuts.append(times[i])
        i = best[j][i][1]
    cuts = [0.0] + cuts[::-1]
    return cuts, sum(margin(c) < 0.4 for c in cuts[1:-1])


def fmt(s):
    return f"{int(s // 60)}:{int(s % 60):02d}"


def split(video, outdir, base, minutes):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    d.log("Đang tách video: dò các đoạn có lời nói...")
    total, speech = speech_gaps(video, outdir)
    kfs = keyframes(video)
    cuts, forced = plan_cuts(total, speech, kfs, minutes)
    n = len(cuts) - 1
    if n == 1:
        d.log(f"Video dài {fmt(total)}, không cần tách")
    else:
        d.log(f"Video dài {fmt(total)} -> tách thành {n} phần"
              + (f" ({forced} điểm cắt phải nằm sát lời nói vì không có khoảng lặng phù hợp)" if forced else ""))
    parts = []
    ext = Path(video).suffix or ".mp4"
    for k in range(n):
        a, b = cuts[k], cuts[k + 1]
        out = outdir / (f"{base} - Phan {k + 1:02d}{ext}" if n > 1 else f"{base}{ext}")
        d.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.3f}", "-i", str(video), "-t", f"{b - a:.3f}", "-c", "copy",
               "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(out)])
        talk = sum(max(0.0, min(e, b) - max(s, a)) for s, e in speech)
        d.log(f"  Phần {k + 1:02d}: {fmt(a)} - {fmt(b)} (dài {fmt(b - a)}, lời thoại khoảng {talk:.0f} giây)")
        parts.append({"path": str(out), "start": round(a, 2), "end": round(b, 2), "speech": round(talk, 1)})
    return parts


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Tách video dài thành nhiều phần, cắt ở chỗ không có lời nói")
    ap.add_argument("source", help="Link (Douyin/TikTok/YouTube...) hoặc đường dẫn file video")
    ap.add_argument("-o", "--outdir", default="output", help="Thư mục lưu các phần")
    ap.add_argument("--minutes", type=float, default=5, help="Độ dài mong muốn của mỗi phần, tính bằng phút (mặc định 5)")
    ap.add_argument("--cookies-from-browser", help="Dùng cookie trình duyệt cho yt-dlp nếu cần")
    args = ap.parse_args()
    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="_tmp_", dir=outdir))
    try:
        video, title = d.download(args.source, work, args.cookies_from_browser)
        base = d.safe_name(title, "video")[:40].strip() or "video"
        parts = split(video, outdir / "parts", base, max(0.5, args.minutes))
        print("PARTS_JSON: " + json.dumps(parts, ensure_ascii=False), flush=True)
        d.log(f"XONG! Đã tách thành {len(parts)} phần")
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
