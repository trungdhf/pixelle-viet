"""Pixelle Việt: Vietnamese web UI over the Pixelle-Video pipeline, powered by Google Vertex AI."""
import asyncio
import json
import os
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
PORT = int(os.environ.get("PORT", "8080"))
CHAT_MODEL = os.environ.get("CHAT_MODEL", "gemini-2.5-flash")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gemini-3.1-flash-image")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SELF_API = f"http://127.0.0.1:{PORT}/v1"

(ROOT / "config.yaml").write_text(f"""project_name: Pixelle-Viet
llm:
  api_key: "vertex"
  base_url: "{SELF_API}"
  model: "{CHAT_MODEL}"
api_providers:
  common: {{print_model_input: false, local_proxy: ""}}
  openai: {{api_key: "vertex", base_url: "{SELF_API}", use_proxy: false}}
  dashscope: {{api_key: "", base_url: "https://dashscope.aliyuncs.com/api/v1", use_proxy: false}}
  ark: {{api_key: "", base_url: "https://ark.cn-beijing.volces.com/api/v3", use_proxy: false}}
  kling: {{base_url: "https://api-beijing.klingai.com", access_key: "", secret_key: "", use_proxy: false}}
comfyui:
  comfyui_url: http://127.0.0.1:8188
  comfyui_api_key: ""
  runninghub_api_key: ""
  runninghub_concurrent_limit: 1
  tts: {{default_workflow: selfhost/tts_edge.json}}
  image: {{default_workflow: api/openai/gpt-image-2, prompt_prefix: ""}}
  video: {{default_workflow: runninghub/video_wan2.1_fusionx.json, prompt_prefix: ""}}
template:
  default_template: "1080x1920/image_default.html"
""", encoding="utf-8")

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app import drama, tts, vertex
from app import facebook as fbmod
from pixelle_video.services.tts_service import TTSService


async def _local_tts(self, text, voice=None, speed=None, output_path=None):
    return await tts.synthesize(text, voice or "", output_path or f"output/tts_{uuid.uuid4().hex}.mp3")


TTSService._call_local_tts = _local_tts

from pixelle_video.models.progress import ProgressEvent
from pixelle_video.service import pixelle_video
from pixelle_video.services.frame_html import HTMLFrameGenerator
from pixelle_video.utils.content_generators import generate_title

OUTPUT = ROOT / "output"
OUTPUT.mkdir(exist_ok=True)
JOBS_FILE = OUTPUT / "jobs.json"
jobs: dict[str, dict] = json.loads(JOBS_FILE.read_text()) if JOBS_FILE.exists() else {}
for j in jobs.values():
    if j["status"] in ("queued", "running"):
        j.update(status="error", error="Máy chủ khởi động lại khi đang tạo video")
run_lock = asyncio.Semaphore(1)
img_sem = asyncio.Semaphore(2)
SERIES_DIR = OUTPUT / "series"
SERIES_FILE = OUTPUT / "series.json"
series: dict[str, dict] = json.loads(SERIES_FILE.read_text()) if SERIES_FILE.exists() else {}
for j in series.values():
    if j["status"] in ("queued", "running"):
        j.update(status="error", error="Máy chủ khởi động lại khi đang tạo phim", message="Lỗi")
fb = fbmod.Facebook(OUTPUT / "facebook.json")
for p_ in fb.state["posts"].values():
    if p_["state"] in ("queued", "uploading"):
        p_.update(state="error", error="Máy chủ khởi động lại khi đang tải lên")

TEMPLATE_NAMES = {
    "image_default": "Mặc định", "image_modern": "Hiện đại", "image_elegant": "Thanh lịch",
    "image_blur_card": "Thẻ mờ", "image_book": "Sách", "image_cartoon": "Hoạt hình",
    "image_excerpt": "Trích dẫn", "image_fashion_vintage": "Cổ điển", "image_full": "Ảnh tràn màn hình",
    "image_healing": "Chữa lành", "image_health_preservation": "Sức khỏe", "image_life_insights": "Chiêm nghiệm (tối)",
    "image_life_insights_light": "Chiêm nghiệm (sáng)", "image_long_text": "Chữ dài", "image_neon": "Neon",
    "image_psychology_card": "Thẻ tâm lý", "image_purple": "Tím", "image_satirical_cartoon": "Biếm họa",
    "image_simple_black": "Đen tối giản", "image_simple_line_drawing": "Nét vẽ đơn giản",
    "image_minimal_framed": "Khung tối giản", "image_film": "Phim điện ảnh",
    "image_ultrawide_minimal": "Siêu rộng tối giản", "image_wide_darktech": "Công nghệ tối",
    "static_default": "Chỉ chữ (không ảnh)", "static_excerpt": "Trích dẫn chỉ chữ (không ảnh)",
}
SIZE_NAMES = {"1080x1920": "Dọc 9:16 (TikTok, Reels, Shorts)", "1920x1080": "Ngang 16:9 (YouTube)",
              "1080x1080": "Vuông 1:1"}
STYLES = [
    ("flat", "Minh họa phẳng ấm áp", "Warm modern flat illustration, soft pastel colors, clean shapes, no text, no letters, no words in image"),
    ("anime", "Hoạt hình anime", "Anime illustration style, vibrant colors, detailed background, no text, no letters in image"),
    ("photo", "Ảnh chân thực", "Photorealistic, natural lighting, high detail, cinematic composition, no text in image"),
    ("watercolor", "Màu nước", "Soft watercolor painting, gentle brush strokes, paper texture, no text in image"),
    ("3d", "3D dễ thương", "Cute 3D render, Pixar style, soft lighting, pastel colors, no text in image"),
    ("ink", "Thủy mặc Á Đông", "Traditional East Asian ink wash painting, elegant, minimal, no text in image"),
    ("stick", "Người que đen trắng", "Minimalist black-and-white matchstick figure style illustration, clean lines, simple sketch style, no text"),
]
LANGS = {
    "vi": ("Tiếng Việt", "\n\n(Viết toàn bộ lời thoại bằng tiếng Việt)", "vi-VN"),
    "ja": ("日本語", "\n\n(ナレーションはすべて日本語で書いてください)", "ja-JP"),
    "en": ("English", "\n\n(Write all narration in English)", "en-US"),
    "auto": ("Theo ngôn ngữ của chủ đề", "", ""),
}
STEP_VI = {"audio": "đọc lời thoại", "image": "vẽ ảnh", "compose": "ghép khung hình", "video": "dựng đoạn video"}
EVENT_VI = {
    "generating_narrations": "Đang viết kịch bản…", "splitting_script": "Đang chia kịch bản…",
    "generating_title": "Đang đặt tiêu đề…", "generating_image_prompts": "Đang lên ý tưởng hình ảnh…",
    "concatenating": "Đang ghép các cảnh thành video…", "adding_bgm": "Đang chèn nhạc nền…",
    "finalizing": "Đang hoàn tất…", "completed": "Xong!",
}


def _save_jobs():
    JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=1))


def _templates():
    out = []
    for f in sorted((ROOT / "templates").glob("*/*.html")):
        if f.stem.startswith(("asset_", "video_")):
            continue
        out.append({"key": f"{f.parent.name}/{f.name}", "size": f.parent.name,
                    "size_name": SIZE_NAMES.get(f.parent.name, f.parent.name),
                    "name": TEMPLATE_NAMES.get(f.stem, f.stem)})
    return out


def _check_pw(req: Request):
    if APP_PASSWORD and req.headers.get("x-app-password") != APP_PASSWORD:
        raise HTTPException(401, "Sai mật khẩu")


@asynccontextmanager
async def lifespan(_app):
    await pixelle_video.initialize()
    await fb.ensure_env()
    retry = asyncio.create_task(fb.retry_loop())
    yield
    retry.cancel()


app = FastAPI(lifespan=lifespan)


# ---------- OpenAI-compatible shim used by the Pixelle pipeline (LLM + images via Vertex) ----------
@app.post("/v1/chat/completions")
async def chat(req: Request):
    b = await req.json()
    system = [m["content"] for m in b["messages"] if m["role"] == "system"]
    contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                for m in b["messages"] if m["role"] != "system"]
    body = {"contents": contents, "generationConfig": {"temperature": b.get("temperature", 0.7)}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": "\n".join(system)}]}
    data = await vertex.generate(CHAT_MODEL, body)
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"] if not p.get("thought"))
    return {"id": "vertex", "object": "chat.completion", "created": int(time.time()), "model": CHAT_MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


@app.post("/v1/images/generations")
async def images(req: Request):
    b = await req.json()
    w, h = (int(x) for x in str(b.get("size", "1024x1024")).lower().replace("*", "x").split("x"))
    ratio = "1:1" if w == h else ("9:16" if h > w else "16:9")
    body = {"contents": [{"role": "user", "parts": [{"text": b["prompt"]}]}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": ratio}}}
    async with img_sem:
        data = await vertex.generate(IMAGE_MODEL, body)
    for p in data["candidates"][0]["content"]["parts"]:
        if "inlineData" in p:
            return {"created": int(time.time()), "data": [{"b64_json": p["inlineData"]["data"]}]}
    raise HTTPException(500, "Vertex không trả về ảnh")


# ---------- App API ----------
@app.get("/api/options")
async def options():
    return {
        "templates": _templates(),
        "styles": [{"key": k, "name": n} for k, n, _ in STYLES],
        "languages": [{"key": k, "name": v[0]} for k, v in LANGS.items()],
        "eleven_voices": [{"id": i, "name": n} for i, n in tts.ELEVEN_VOICES],
        "gemini_voices": [{"id": i, "name": n} for i, n in tts.GEMINI_VOICES],
        "gemini_models": [{"id": i, "name": n} for i, n in tts.GEMINI_TTS_MODELS],
        "eleven_ready": bool(tts._eleven_keys()),
        "password_required": bool(APP_PASSWORD),
    }


def _voice_spec(p: dict) -> str:
    lang_code = LANGS.get(p.get("language", "vi"), LANGS["vi"])[2]
    if p.get("voice_provider") == "gemini":
        return f"gemini|{p.get('gemini_model') or 'gemini-2.5-flash-tts'}|{p.get('voice') or 'Kore'}|{lang_code}"
    return f"eleven|{p.get('voice') or tts.ELEVEN_VOICES[0][0]}|{float(p.get('speed') or 1.0)}"


@app.post("/api/tts-preview")
async def tts_preview(req: Request):
    _check_pw(req)
    p = await req.json()
    out = OUTPUT / "previews" / f"{uuid.uuid4().hex}.mp3"
    text = (p.get("text") or "").strip() or "Xin chào! Đây là giọng đọc mẫu cho video của bạn."
    try:
        await tts.synthesize(text[:300], _voice_spec(p), str(out))
    except Exception as e:
        raise HTTPException(500, str(e))
    data = out.read_bytes()
    out.unlink()
    return Response(data, media_type="audio/mpeg")


@app.post("/api/jobs")
async def create_job(req: Request):
    _check_pw(req)
    p = await req.json()
    text = (p.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Hãy nhập chủ đề hoặc kịch bản")
    tpl = p.get("template") or "1080x1920/image_default.html"
    if tpl not in {t["key"] for t in _templates()}:
        raise HTTPException(400, "Khung hình không hợp lệ")
    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    jobs[jid] = {"id": jid, "status": "queued", "progress": 0, "message": "Đang chờ tới lượt…",
                 "created": time.time(), "text": text[:200], "title": p.get("title") or "", "params": p}
    _save_jobs()
    asyncio.create_task(_run_job(jid))
    return jobs[jid]


@app.get("/api/jobs")
async def list_jobs():
    return sorted(jobs.values(), key=lambda j: j["created"], reverse=True)[:50]


@app.get("/api/jobs/{jid}")
async def get_job(jid: str):
    if jid not in jobs:
        raise HTTPException(404, "Không tìm thấy")
    return jobs[jid]


async def _run_job(jid: str):
    job = jobs[jid]
    p = job["params"]
    async with run_lock:
        job.update(status="running", message="Bắt đầu…", started=time.time())
        _save_jobs()

        def on_progress(ev: ProgressEvent):
            if ev.event_type == "frame_step" and ev.frame_current:
                msg = f"Cảnh {ev.frame_current}/{ev.frame_total}: {STEP_VI.get(ev.action, ev.action or '')}…"
            else:
                msg = EVENT_VI.get(ev.event_type, job["message"])
            job.update(progress=round(ev.progress * 100), message=msg)

        try:
            mode = "fixed" if p.get("mode") == "fixed" else "generate"
            lang = p.get("language", "vi")
            text = p["text"].strip()
            title = (p.get("title") or "").strip()
            if not title:
                job["message"] = "Đang đặt tiêu đề…"
                title = text if len(text) <= 40 and mode == "generate" else await generate_title(
                    pixelle_video.llm, text, strategy="llm", max_length=40)
            job["title"] = title
            if mode == "generate":
                text += LANGS.get(lang, LANGS["vi"])[1]
            tpl = p.get("template") or "1080x1920/image_default.html"
            w, h = HTMLFrameGenerator(str(ROOT / "templates" / tpl)).get_media_size()
            style = p.get("style_custom") or dict((k, s) for k, _, s in STYLES).get(p.get("style"), STYLES[0][2])
            sig = (p.get("signature") or "").strip()
            kwargs = dict(
                text=text, mode=mode, title=title, n_scenes=max(1, min(int(p.get("n_scenes") or 5), 12)),
                split_mode=p.get("split_mode") or "paragraph",
                min_narration_words=15, max_narration_words=40,
                tts_inference_mode="local", tts_voice=_voice_spec(p), tts_speed=1.0,
                media_workflow="api/openai/gpt-image-2", media_width=w, media_height=h,
                frame_template=tpl, prompt_prefix=style,
                template_params={"author": sig, "brand": "", "describe": "", "signature": sig},
                progress_callback=on_progress,
            )
            if p.get("bgm", True):
                kwargs.update(bgm_path="bgm/default.mp3", bgm_volume=float(p.get("bgm_volume") or 0.12))
            res = await pixelle_video.generate_video(**kwargs)
            rel = Path(res.video_path).resolve().relative_to(OUTPUT.resolve())
            job.update(status="done", progress=100, message="Xong!", video_url=f"/output/{rel.as_posix()}",
                       duration=round(res.storyboard.total_duration or 0, 1),
                       narrations=[f.narration for f in res.storyboard.frames], finished=time.time())
        except Exception as e:
            logger.error(traceback.format_exc())
            job.update(status="error", error=str(e)[:500], message="Lỗi")
        _save_jobs()


# ---------- multi-episode series ----------
def _save_series():
    SERIES_FILE.write_text(json.dumps(series, ensure_ascii=False, indent=1))


def _out_url(p: Path) -> str:
    return "/output/" + p.resolve().relative_to(OUTPUT.resolve()).as_posix()


def _out_file(url: str) -> Path:
    f = (OUTPUT / url.split("/output/", 1)[-1].split("?")[0]).resolve()
    if not f.is_file() or OUTPUT.resolve() not in f.parents:
        raise HTTPException(400, "Không tìm thấy file video")
    return f


@app.get("/series")
async def series_page():
    return FileResponse(ROOT / "static" / "series.html")


@app.get("/api/series/options")
async def series_options():
    return {"looks": [{"key": k, "name": v[0]} for k, v in drama.LOOKS.items()], "password_required": bool(APP_PASSWORD)}


@app.post("/api/series/script")
async def series_script(req: Request):
    _check_pw(req)
    p = await req.json()
    premise = (p.get("premise") or "").strip()
    if not premise:
        raise HTTPException(400, "Hãy nhập ý tưởng phim")
    try:
        s = await drama.write_series(premise, max(1, min(int(p.get("n_eps") or 6), 12)),
                                     max(3, min(int(p.get("n_scenes") or 7), 12)), p.get("look") or "drama", bool(p.get("english")))
    except Exception as e:
        logger.error(traceback.format_exc())
        raise HTTPException(500, f"Không viết được kịch bản: {e}")
    s["watermark"] = (p.get("watermark") or "").strip()
    return s


@app.post("/api/series")
async def create_series(req: Request):
    _check_pw(req)
    p = await req.json()
    try:
        script = drama.normalize(p.get("script") or {})
    except Exception as e:
        raise HTTPException(400, f"Kịch bản không hợp lệ: {e}")
    sid = p.get("id") if p.get("id") in series else time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    n = len(script["episodes"])
    only = [int(x) for x in (p.get("only") or []) if 1 <= int(x) <= n] or list(range(1, n + 1))
    old = series.get(sid, {}).get("episodes") or []
    eps = [old[i] if i < len(old) and (i + 1) not in only else {"n": i + 1, "status": "queued"} for i in range(n)]
    for i, e in enumerate(eps):
        e.update(title=script["episodes"][i].get("title", ""), caption=script["episodes"][i].get("caption", ""))
    series[sid] = {"id": sid, "status": "queued", "progress": 0, "message": "Đang chờ tới lượt…", "created": time.time(),
                   "title": script.get("title", ""), "script": script, "episodes": eps, "only": only, "refs": []}
    _save_series()
    asyncio.create_task(_run_series(sid))
    return series[sid]


@app.get("/api/series")
async def list_series():
    return [{k: v for k, v in j.items() if k != "script"} for j in sorted(series.values(), key=lambda j: j["created"], reverse=True)[:30]]


@app.get("/api/series/{sid}")
async def get_series(sid: str):
    if sid not in series:
        raise HTTPException(404, "Không tìm thấy")
    return series[sid]


async def _run_series(sid: str):
    job = series[sid]
    s, d = job["script"], SERIES_DIR / sid
    async with run_lock:
        job.update(status="running", message="Đang tạo ảnh mẫu nhân vật…")
        _save_series()
        try:
            await asyncio.gather(*[drama.make_ref(c, d / "refs") for c in s["characters"]])
            job["refs"] = [{"key": c["key"], "name": c["name"], "url": _out_url(d / "refs" / f"{c['key']}.png")} for c in s["characters"]]
            for k, n in enumerate(job["only"]):
                ep = job["episodes"][n - 1]
                ep.update(status="running", error="")

                def prog(frac, msg, k=k):
                    job.update(progress=round(100 * (k + frac) / len(job["only"])), message=msg)

                try:
                    r = await drama.render_episode(s, n, d, prog)
                    ep.update(status="done", video_url=_out_url(r["video"]) + f"?v={int(time.time())}",
                              sheet_url=_out_url(r["sheet"]) + f"?v={int(time.time())}", duration=r["duration"])
                except Exception as e:
                    logger.error(traceback.format_exc())
                    ep.update(status="error", error=str(e)[:400])
                _save_series()
            bad = [e["n"] for e in job["episodes"] if e["status"] == "error"]
            job.update(status="error" if bad else "done", progress=100,
                       message=f"Lỗi ở tập {', '.join(map(str, bad))}" if bad else "Xong!", error="")
        except Exception as e:
            logger.error(traceback.format_exc())
            job.update(status="error", error=str(e)[:500], message="Lỗi")
        _save_series()


# ---------- Facebook ----------
def _fb(fn):
    try:
        return fn()
    except fbmod.FBError as e:
        raise HTTPException(400, str(e))


@app.get("/api/fb")
async def fb_info(req: Request):
    _check_pw(req)
    return fb.public()


@app.post("/api/fb/connect")
async def fb_connect(req: Request):
    _check_pw(req)
    tok = ((await req.json()).get("token") or "").strip()
    if not tok:
        raise HTTPException(400, "Hãy dán token")
    try:
        return await fb.connect(tok)
    except fbmod.FBError as e:
        raise HTTPException(400, str(e))


@app.post("/api/fb/select")
async def fb_select(req: Request):
    _check_pw(req)
    pid = (await req.json()).get("page_id") or ""
    return _fb(lambda: fb.select(pid))


@app.post("/api/fb/disconnect")
async def fb_disconnect(req: Request):
    _check_pw(req)
    fb.disconnect()
    return fb.public()


@app.get("/api/fb/posts")
async def fb_posts(req: Request):
    _check_pw(req)
    return [{k: v for k, v in p.items() if k != "file"} for p in fb.posts()[:100]]


@app.post("/api/fb/posts")
async def fb_new_post(req: Request):
    _check_pw(req)
    p = await req.json()
    f = _out_file(p.get("video_url") or "")
    ts = int(p["ts"]) if p.get("ts") else None
    post = _fb(lambda: fb.new_post(str(f), p.get("caption") or "", p.get("mode") or "draft", ts, p.get("title") or ""))
    asyncio.create_task(fb.upload(post))
    return post


def _post(pid: str) -> dict:
    if pid not in fb.state["posts"]:
        raise HTTPException(404, "Không tìm thấy bài")
    return fb.state["posts"][pid]


@app.post("/api/fb/posts/{pid}/refresh")
async def fb_refresh(pid: str, req: Request):
    _check_pw(req)
    try:
        return await fb.refresh(_post(pid))
    except fbmod.FBError as e:
        raise HTTPException(400, str(e))


@app.post("/api/fb/posts/{pid}/schedule")
async def fb_schedule(pid: str, req: Request):
    _check_pw(req)
    ts = int((await req.json()).get("ts") or 0)
    try:
        return await fb.reschedule(_post(pid), ts)
    except fbmod.FBError as e:
        raise HTTPException(400, str(e))


@app.get("/output/{path:path}")
async def output_file(path: str, download: int = 0):
    f = (OUTPUT / path).resolve()
    if not f.is_file() or OUTPUT.resolve() not in f.parents:
        raise HTTPException(404)
    headers = {"Content-Disposition": f'attachment; filename="{f.parent.name}.mp4"'} if download else None
    return FileResponse(f, headers=headers)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
