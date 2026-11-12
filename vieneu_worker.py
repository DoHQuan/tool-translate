"""Chạy trong môi trường riêng .vieneu-venv (không dùng chung thư viện với tool): đọc một loạt câu bằng VieNeu-TTS.

    .vieneu-venv/Scripts/python vieneu_worker.py jobs.json

jobs.json: [{"text": "...", "out": "đường dẫn .wav", "voice": "Tên giọng có sẵn"}]. Nạp model một lần, gom các câu
cùng giọng để đọc theo lô. In tiến độ "k/n" từng lô; câu nào lỗi thì bỏ qua (tool sẽ thấy thiếu file).
"""
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    jobs = json.load(open(sys.argv[1], encoding="utf-8"))
    from vieneu import Vieneu

    tts = Vieneu()
    known = {name for _, name in tts.list_preset_voices()}
    groups = {}
    for k, j in enumerate(jobs):
        groups.setdefault(j["voice"] if j.get("voice") in known else None, []).append(k)
    done, batch = 0, 8
    for voice, ids in groups.items():
        for c in range(0, len(ids), batch):
            part = ids[c:c + batch]
            try:
                auds = tts.infer_batch([jobs[k]["text"] for k in part], voice=voice)
                for k, a in zip(part, auds):
                    if a is not None and len(a) > tts.sample_rate * 0.1:
                        tts.save(a, jobs[k]["out"])
            except Exception as e:  # một lô hỏng thì đọc lại từng câu
                print(f"[vieneu] lô lỗi ({str(e)[:100]}), đọc lại từng câu", flush=True)
                for k in part:
                    try:
                        tts.save(tts.infer(jobs[k]["text"], voice=voice), jobs[k]["out"])
                    except Exception as e2:
                        print(f"[vieneu] bỏ qua 1 câu ({str(e2)[:80]})", flush=True)
            done += len(part)
            print(f"   {done}/{len(jobs)}", flush=True)


if __name__ == "__main__":
    main()
