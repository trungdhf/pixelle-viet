"""TTS backends: ElevenLabs (eleven_v4) and Gemini TTS on Vertex.

Voice spec strings passed through the Pixelle pipeline as `tts_voice`:
  eleven|<voice_id>|<speed>
  gemini|<model>|<voice_name>|<language_code or ''>
"""
import asyncio
import base64
import os
import tempfile
from pathlib import Path

import httpx

from app import vertex

ELEVEN_VOICES = [
    ("cgSgspJ2msm6clMCkdW9", "Jessica (nữ, trẻ trung)"),
    ("EXAVITQu4vr4xnSDxMaL", "Sarah (nữ, nhẹ nhàng)"),
    ("XrExE9yKIg1WjnnlVkGX", "Matilda (nữ, ấm áp)"),
    ("pFZP5JQG7iQjIQuC4Bku", "Lily (nữ, rõ ràng)"),
    ("Xb7hH8MSUJpSbSDYk0k2", "Alice (nữ, tự tin)"),
    ("FGY2WhTYpPnrIDTdsKH5", "Laura (nữ, vui vẻ)"),
    ("JBFqnCBsd6RMkjVDRZzb", "George (nam, trầm ấm)"),
    ("TX3LPaxmHKxFdv7VOQHJ", "Liam (nam, trẻ)"),
    ("nPczCjzI2devNBz1zQrb", "Brian (nam, sâu)"),
    ("onwK4e9ZLuTAKqWW03F9", "Daniel (nam, phát thanh viên)"),
    ("IKne3meq5aSn9XLyUdCD", "Charlie (nam, tự nhiên)"),
    ("cjVigY5qzO86Huf0OWal", "Eric (nam, thân thiện)"),
]

GEMINI_VOICES = [
    ("Kore", "Kore (nữ, rõ ràng)"), ("Aoede", "Aoede (nữ, nhẹ nhàng)"), ("Leda", "Leda (nữ, trẻ)"),
    ("Zephyr", "Zephyr (nữ, tươi sáng)"), ("Callirrhoe", "Callirrhoe (nữ, thoải mái)"),
    ("Despina", "Despina (nữ, mượt)"), ("Sulafat", "Sulafat (nữ, ấm)"),
    ("Puck", "Puck (nam, vui tươi)"), ("Charon", "Charon (nam, kể chuyện)"),
    ("Fenrir", "Fenrir (nam, hào hứng)"), ("Orus", "Orus (nam, chắc chắn)"),
    ("Iapetus", "Iapetus (nam, rõ ràng)"), ("Algieba", "Algieba (nam, mượt)"),
    ("Achird", "Achird (nam, thân thiện)"),
]

GEMINI_TTS_MODELS = [
    ("gemini-2.5-flash-tts", "Gemini 2.5 Flash TTS (nhanh)"),
    ("gemini-2.5-pro-tts", "Gemini 2.5 Pro TTS (hay hơn)"),
]


def _eleven_keys():
    return [os.environ[k] for k in ("ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY2") if os.environ.get(k)]


async def eleven(text: str, voice_id: str, speed: float, out: str) -> str:
    errors = []
    for key in _eleven_keys():
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128",
                headers={"xi-api-key": key},
                json={"text": text, "model_id": os.environ.get("ELEVENLABS_MODEL", "eleven_v4"),
                      "voice_settings": {"speed": max(0.7, min(1.2, speed))}})
        if r.status_code == 200:
            Path(out).write_bytes(r.content)
            return out
        errors.append(f"{r.status_code}: {r.text[:150]}")
    raise RuntimeError("ElevenLabs lỗi: " + (" | ".join(errors) or "chưa cấu hình ELEVENLABS_API_KEY"))


async def gemini(text: str, model: str, voice: str, lang: str, out: str) -> str:
    speech = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}
    if lang:
        speech["languageCode"] = lang
    data = await vertex.generate(model, {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": speech}})
    part = next(p for p in data["candidates"][0]["content"]["parts"] if "inlineData" in p)
    pcm = base64.b64decode(part["inlineData"]["data"])
    rate = "24000"
    mime = part["inlineData"].get("mimeType", "")
    if "rate=" in mime:
        rate = mime.split("rate=")[1].split(";")[0]
    with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
        f.write(pcm)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", rate, "-ac", "1", "-i", f.name,
        "-b:a", "128k", out)
    await proc.wait()
    os.unlink(f.name)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg chuyển PCM sang mp3 thất bại")
    return out


async def synthesize(text: str, spec: str, out: str) -> str:
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    parts = (spec or "").split("|")
    if parts[0] == "gemini":
        _, model, voice, lang = (parts + ["", "", "", ""])[:4]
        return await gemini(text, model or "gemini-2.5-flash-tts", voice or "Kore", lang, out)
    _, voice_id, speed = (parts + ["", "", ""])[:3] if parts[0] == "eleven" else ("eleven", "", "")
    return await eleven(text, voice_id or ELEVEN_VOICES[0][0], float(speed or 1.0), out)
