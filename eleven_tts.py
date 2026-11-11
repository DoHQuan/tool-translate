"""Bộ đọc ElevenLabs cho douyin-dub: mỗi câu một lần gọi, trả về file mp3.

Key lưu trong config.json (eleven_key) hoặc biến môi trường DUB_ELEVEN_KEY. Tính tiền theo ký tự:
model v3 = 1 credit/ký tự (hay nhất), flash v2.5 = 0,5 credit/ký tự. Tiếng Việt chỉ có ở v3 và các model v2.5.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

API = "https://api.elevenlabs.io/v1"
MODELS = {"v3": "eleven_v3", "flash": "eleven_flash_v2_5"}
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


class ElevenOut(Exception):
    """Không dùng tiếp được ElevenLabs (sai key, hết credit, gói không có quyền API...)."""


def load_config():
    cfg = {}
    if CONFIG_PATH.is_file():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    key = os.environ.get("DUB_ELEVEN_KEY") or cfg.get("eleven_key") or ""
    return {"key": key} if len(key) > 10 else None


def _err(r):
    try:
        d = r.json().get("detail")
        if isinstance(d, dict):
            return f"{d.get('status', '')}: {d.get('message', '')}".strip(": ")
        return str(d)
    except Exception:
        return r.text[:200]


def voices(key):
    """Các giọng trong thư viện của tài khoản: [{id, name, gender, age, category}]."""
    r = requests.get(f"{API}/voices", headers={"xi-api-key": key}, timeout=30)
    if r.status_code != 200:
        raise ElevenOut(f"ElevenLabs từ chối ({r.status_code}): {_err(r)}")
    out = []
    for v in r.json().get("voices", []):
        lb = v.get("labels") or {}
        out.append({"id": v["voice_id"], "name": v.get("name", ""), "gender": (lb.get("gender") or "").lower(),
                    "age": (lb.get("age") or "").lower(), "category": v.get("category", "")})
    return out


def credits(key):
    """(đã dùng, tổng) số ký tự của kỳ này, hoặc None nếu key không có quyền xem."""
    try:
        r = requests.get(f"{API}/user/subscription", headers={"xi-api-key": key}, timeout=20)
        if r.status_code == 200:
            d = r.json()
            return d.get("character_count"), d.get("character_limit")
    except Exception:
        pass
    return None


def pick_voice(all_voices, gender, age, taken):
    """Chọn giọng hợp giới tính (và tuổi nếu có) mà chưa nhân vật nào dùng."""
    want_age = {"child": "young", "young": "young", "adult": "middle", "old": "old"}.get(age, "")
    same = [v for v in all_voices if v["gender"] == gender]
    free = [v for v in same if v["id"] not in taken]
    for pool in ([v for v in free if want_age and want_age in v["age"]], free, same):
        if pool:
            return pool[0]["id"]
    return None


def speak(state, text, voice_id, out_path):
    """Đọc một câu ra file mp3. state: {"key", "model", "chars"}. Lỗi không cứu được -> ElevenOut."""
    body = {"text": text, "model_id": MODELS[state["model"]], "language_code": "vi"}
    for attempt in range(5):
        if state.get("dead"):
            raise ElevenOut(state["dead"])
        try:
            r = requests.post(f"{API}/text-to-speech/{voice_id}", params={"output_format": "mp3_44100_128"},
                              headers={"xi-api-key": state["key"]}, json=body, timeout=120)
        except requests.RequestException:
            time.sleep(2 + attempt * 2)
            continue
        if r.status_code == 200 and len(r.content) > 500:
            Path(out_path).write_bytes(r.content)
            state["chars"] = state.get("chars", 0) + len(text)
            return
        msg = _err(r)
        if r.status_code == 429 and "quota" not in msg.lower():  # quá nhiều yêu cầu cùng lúc -> chờ
            time.sleep(3 + attempt * 3)
            continue
        if r.status_code >= 500:
            time.sleep(2 + attempt * 2)
            continue
        if r.status_code in (400, 422) and "language_code" in body and "language" in msg.lower():
            body.pop("language_code")  # model không nhận tham số ngôn ngữ -> để tự nhận diện
            continue
        if r.status_code in (400, 403, 404) and state["model"] == "v3" and "model" in msg.lower():
            state["model"] = "flash"  # tài khoản chưa dùng được v3 -> model flash
            state["note"] = "Tài khoản chưa dùng được model v3 -> dùng Flash v2.5"
            body["model_id"] = MODELS["flash"]
            continue
        state["dead"] = f"ElevenLabs từ chối ({r.status_code}): {msg[:200]}"
        raise ElevenOut(state["dead"])
    raise ElevenOut("ElevenLabs không phản hồi sau nhiều lần thử")


def speak_all(items, state, workers=3):
    """items: list (text, out_path, voice_id). Trả về tập chỉ số câu đã tạo được."""
    done = set()

    def one(n):
        try:
            speak(state, items[n][0], items[n][2], items[n][1])
            done.add(n)
        except ElevenOut:
            pass

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, range(len(items))))
    return done
