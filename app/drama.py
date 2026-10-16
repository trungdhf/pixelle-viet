"""Multi-episode photorealistic short dramas: recurring characters, per-character voices, optional English-learning cards."""
import asyncio
import base64
import glob
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app import tts, vertex

W, H, FPS = 1080, 1920, 30
IMG_MODEL = os.environ.get("IMAGE_MODEL", "gemini-3.1-flash-image")
WRITER_MODEL = os.environ.get("DRAMA_MODEL", os.environ.get("CHAT_MODEL", "gemini-2.5-flash"))
TTS_MODEL = os.environ.get("DRAMA_TTS_MODEL", "gemini-2.5-pro-tts")
ROOT = Path(__file__).resolve().parent.parent

LOOKS = {
    "drama": ("Tình cảm / đời thường", "warm cinematic drama look, natural vivid colors"),
    "comedy": ("Hài công sở", "bright warm lively comedy look, vivid natural colors"),
    "romance": ("Lãng mạn", "soft romantic film look, golden-hour light, dreamy bokeh"),
    "horror": ("Kinh dị / phim ma", "dark eerie atmospheric Asian horror film look, cold desaturated blue-green tones, mist, "
                                   "low-key lighting, deep shadows, suspenseful, no blood, no gore"),
}
ACCENT = {"horror": (239, 68, 68)}
BASE_STYLE = ("Photorealistic cinematic film still, shot on 35mm, real human actors, natural skin texture, realistic lighting, "
              "shallow depth of field, {look}, expressive faces, vertical composition. NOT anime, NOT cartoon, NOT illustration, "
              "NOT 3D render. One single frame, NOT a collage, NOT a grid, NOT multiple panels. No text, no letters, no captions, "
              "no speech bubbles.")
NAME_COLORS = [(37, 99, 235), (219, 39, 119), (22, 101, 52), (124, 58, 237), (180, 83, 9), (14, 116, 144), (190, 18, 60)]
FEMALE = ["Despina", "Aoede", "Kore", "Callirrhoe", "Autonoe"]
MALE = ["Puck", "Orus", "Iapetus", "Fenrir", "Charon"]

img_sem = asyncio.Semaphore(1)
tts_sem = asyncio.Semaphore(2)


# ---------- script writing ----------
WRITER_PROMPT = """Bạn là biên kịch phim ngắn dọc (Reels/TikTok) cho khán giả Việt Nam. Viết series {n_eps} tập, mỗi tập {n_scenes} cảnh (~60 giây).
Ý tưởng: {premise}
Thể loại: {genre}.
{english}
Yêu cầu:
- Nhân vật cố định qua các tập. Mỗi nhân vật có "key" là MỘT từ không dấu, viết hoa chữ đầu (vd "Vy", "Khoa"), không trùng từ tiếng Anh thông dụng.
- "look": mô tả ngoại hình bằng tiếng Anh (quốc tịch, tuổi, tóc, khuôn mặt, trang phục cố định).
- Mỗi cảnh: "image" là mô tả 1 khung hình bằng tiếng Anh, gọi nhân vật bằng đúng key, chỉ nhắc nhân vật có mặt trong cảnh. "lines" là 1–3 câu thoại ngắn; "spk" là "N" (người dẫn chuyện) hoặc key nhân vật.
- Mỗi tập kết bằng tình tiết gây tò mò và câu giới thiệu tập sau (trừ tập cuối).
- "caption": mô tả đăng Facebook 1–2 câu + 3–5 hashtag.
Trả về JSON đúng dạng:
{{"title": "...", "characters": [{{"key": "Vy", "name": "Vy", "gender": "female|male", "age": 21, "look": "..."}}],
 "episodes": [{{"title": "TIÊU ĐỀ TẬP VIẾT HOA", "caption": "...", "scenes": [{{"image": "...", "lines": [{{"spk": "N", "text": "...", "en": false, "answer": false}}]}}]{learn_schema}}}]}}"""
ENGLISH_ON = ("Mỗi tập dạy 1 câu tiếng Anh thực tế: nhân vật nói sai/lúng túng trước (en=true), rồi nói câu đúng ở 1 câu thoại có "
              "\"en\": true, \"answer\": true. Câu thoại tiếng Anh để en=true; còn lại tiếng Việt.")
ENGLISH_OFF = "Toàn bộ lời thoại và lời dẫn bằng tiếng Việt, KHÔNG có tiếng Anh, không có phần học từ vựng."
LEARN_SCHEMA = (', "learn": {"hint": "gợi ý tiếng Việt của câu đúng", "en": "câu tiếng Anh đúng", "vi": "nghĩa tiếng Việt", '
                '"note": "ghi chú ngữ pháp ngắn", "extra": [["câu tương tự 1", "nghĩa"], ["câu tương tự 2", "nghĩa"]]}')


async def write_series(premise: str, n_eps: int, n_scenes: int, look: str, english: bool) -> dict:
    prompt = WRITER_PROMPT.format(n_eps=n_eps, n_scenes=n_scenes, premise=premise, genre=LOOKS.get(look, LOOKS["drama"])[0],
                                  english=ENGLISH_ON if english else ENGLISH_OFF, learn_schema=LEARN_SCHEMA if english else "")
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}}
    data = await _retry(lambda: vertex.generate(WRITER_MODEL, body))
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    s = json.loads(text[text.find("{"): text.rfind("}") + 1])
    s.update(look=look, english=english, premise=premise)
    return normalize(s)


def normalize(s: dict) -> dict:
    """Validate a series script and fill derived fields (voices, colors)."""
    chars = s.get("characters") or []
    if not s.get("episodes"):
        raise ValueError("Kịch bản không có tập nào")
    seen, fi, mi = set(), 0, 0
    for i, c in enumerate(chars):
        c["key"] = re.sub(r"[^A-Za-z]", "", c.get("key") or c.get("name") or f"P{i}")[:12] or f"P{i}"
        if c["key"] in seen:
            raise ValueError(f"Trùng key nhân vật {c['key']}")
        seen.add(c["key"])
        c.setdefault("name", c["key"])
        if not c.get("voice"):
            age, fem = int(c.get("age") or 30), str(c.get("gender", "")).lower().startswith("f")
            if age <= 12:
                c["voice"] = "Leda" if fem else "Puck"
            elif age >= 55:
                c["voice"] = "Gacrux" if fem else "Algieba"
            elif fem:
                c["voice"], fi = FEMALE[fi % len(FEMALE)], fi + 1
            else:
                c["voice"], mi = MALE[mi % len(MALE)], mi + 1
        c.setdefault("color", list(NAME_COLORS[i % len(NAME_COLORS)]))
    s.setdefault("narrator", "Enceladus" if s.get("look") == "horror" else "Charon")
    keys = {c["key"] for c in chars} | {"N"}
    for e in s["episodes"]:
        for sc in e.get("scenes", []):
            for ln in sc.get("lines", []):
                if ln.get("spk") not in keys:
                    ln["spk"] = "N"
    s["english"] = bool(s.get("english"))
    return s


def expand(ep: dict, english: bool) -> list[tuple[str, list[dict]]]:
    """Scenes as (image prompt, lines); with English on, insert the 3-2-1 quiz before the answer line and learning cards after it."""
    scenes = [(sc["image"], [dict(ln, mode="") for ln in sc.get("lines", [])]) for sc in ep["scenes"]]
    learn = ep.get("learn") if english else None
    if not learn or not scenes:
        return scenes
    pos = next(((i, j) for i, (_, ls) in enumerate(scenes) for j, ln in enumerate(ls) if ln.get("answer")), None)
    if pos is None:
        pos = (len(scenes) - 1, max(0, len(scenes[-1][1]) - 1))
    i, j = pos
    quiz = [dict(spk="N", text="Nếu là bạn, bạn sẽ nói câu gì? Dừng lại nghĩ một chút nhé!", mode="QUIZ")] + \
           [dict(spk="N", text=t, mode=f"CD{k}") for k, t in ((3, "Ba."), (2, "Hai."), (1, "Một."))]
    cards = [dict(spk="N", text=f"Ghi nhớ nhé: {learn.get('en', '')} Nghĩa là: {learn.get('vi', '')}.", mode="CARD")]
    if learn.get("extra"):
        cards.append(dict(spk="N", text="Nói thêm hai câu tương tự: " + " ".join(f"{a}. {b}." for a, b in learn["extra"][:2]),
                          mode="EXTRA"))
    lines = scenes[i][1]
    scenes[i] = (scenes[i][0], lines[:j] + quiz + lines[j:j + 1] + cards + lines[j + 1:])
    return scenes


# ---------- media ----------
async def _retry(fn, tries=8):
    for k in range(tries):
        try:
            return await fn()
        except Exception as e:
            if ("429" not in str(e) and "Quota" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e)) or k == tries - 1:
                raise
            await asyncio.sleep(20 + 15 * k)


async def _image(parts: list, out: Path, ratio="9:16"):
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": ratio}}}
    async with img_sem:
        data = await _retry(lambda: vertex.generate(IMG_MODEL, body))
    for p in data["candidates"][0]["content"].get("parts", []):
        if "inlineData" in p:
            tmp = out.with_suffix(".raw")
            tmp.write_bytes(base64.b64decode(p["inlineData"]["data"]))
            Image.open(tmp).convert("RGB").save(out)
            tmp.unlink()
            return
    raise RuntimeError("Vertex không trả về ảnh (có thể bị chặn nội dung) – hãy sửa mô tả cảnh")


async def make_ref(c: dict, refs: Path):
    out, look = refs / f"{c['key']}.png", refs / f"{c['key']}.txt"
    if out.exists() and (not look.exists() or look.read_text(encoding="utf-8") == c["look"]):
        return
    refs.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    look.write_text(c["look"], encoding="utf-8")
    await _image([{"text": "Photorealistic full-body photo of ONE real person standing and facing the camera, neutral light grey "
                           "studio background, natural skin texture, realistic lighting. NOT anime, NOT cartoon, NOT 3D render. "
                           "Exactly one person, no text. Person: " + c["look"]}], out)


def named_in(prompt: str, keys: list[str]) -> list[str]:
    return [k for k in keys if re.search(r"\b" + re.escape(k) + r"\b", prompt)]


async def scene_image(s: dict, prompt: str, out: Path, refs: Path):
    if out.exists():
        return
    chars = s.get("characters") or []
    keys = [c["key"] for c in chars]
    named = named_in(prompt, keys)
    parts = []
    if named:
        ims = [Image.open(refs / f"{n}.png").convert("RGB") for n in named]
        ims = [i.resize((round(i.width * 900 / i.height), 900)) for i in ims]
        sheet = Image.new("RGB", (sum(i.width for i in ims), 900), "white")
        x = 0
        for i in ims:
            sheet.paste(i, (x, 0))
            x += i.width
        ref = out.with_suffix(".ref.png")
        sheet.save(ref)
        parts += [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(ref.read_bytes()).decode()}},
                  {"text": f"The attached image shows the reference look of {', '.join(named)} (left to right). "
                           "Keep each person's face, hair and outfit exactly consistent with it."}]
    desc = " ".join(f"{c['key']}: {c['look']}" for c in chars if c["key"] in named)
    excl = [k for k in keys if k not in named]
    look = LOOKS.get(s.get("look"), LOOKS["drama"])[1]
    parts.append({"text": f"{BASE_STYLE.format(look=look)}\nCharacters in this frame: {desc or 'none of the main cast'}\n"
                          f"Scene: {prompt}\nThe frame contains ONLY these main characters: {', '.join(named) or 'none'} "
                          f"({len(named)} person(s)) plus any anonymous people the scene explicitly describes. "
                          + (f"Do NOT draw {', '.join(excl)}; they are absent from this scene. " if excl else "")
                          + "Any background people are anonymous strangers who look clearly different from the main characters."})
    await _image(parts, out)


async def line_audio(s: dict, ln: dict, out: Path):
    if out.exists():
        return
    voice = s["narrator"] if ln["spk"] == "N" else next(c["voice"] for c in s["characters"] if c["key"] == ln["spk"])
    lang = "en-US" if ln.get("en") else "vi-VN"
    async with tts_sem:
        await _retry(lambda: tts.synthesize(ln["text"], f"gemini|{TTS_MODEL}|{voice}|{lang}", str(out)))


def _font(sz, bold=True):
    names = (["NotoSans-Bold.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf", "segoeuib.ttf"] if bold
             else ["NotoSans-Regular.ttf", "DejaVuSans.ttf", "arial.ttf", "segoeui.ttf"])
    for n in names:
        for d in ("/usr/share/fonts", "C:/Windows/Fonts", "/Library/Fonts", "/System/Library/Fonts"):
            g = glob.glob(f"{d}/**/{n}", recursive=True)
            if g:
                return ImageFont.truetype(g[0], sz)
    return ImageFont.load_default(sz)


_FONTS: dict = {}


def font(sz, bold=True):
    if (sz, bold) not in _FONTS:
        _FONTS[(sz, bold)] = _font(sz, bold)
    return _FONTS[(sz, bold)]


def wrap(d, text, f, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return [x for x in lines if x]


def _fit(d, text, start, stop, maxw):
    return next((font(z) for z in range(start, stop, -2) if d.textlength(text, font=font(z)) <= maxw), font(stop))


def overlay(s: dict, ep: dict, n: int, ln: dict, title: bool) -> Image.Image:
    acc = ACCENT.get(s.get("look"), (250, 204, 21))
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if s.get("watermark"):
        d.text((40, 50), s["watermark"], font=font(34), fill=(255, 255, 255, 230), stroke_width=3, stroke_fill=(0, 0, 0, 160))
    if title:
        t1, t2 = f"{s.get('title', '').upper()} • TẬP {n}", ep.get("title", "")
        d.rounded_rectangle((60, 170, W - 60, 430), 30, fill=(0, 0, 0, 150))
        d.text((W // 2, 235), t1, font=_fit(d, t1, 52, 28, W - 160), fill=acc, anchor="mm")
        d.text((W // 2, 335), t2, font=_fit(d, t2, 76, 30, W - 160), fill=(255, 255, 255), anchor="mm")
    mode, learn = ln.get("mode", ""), ep.get("learn") or {}
    if mode in ("CARD", "QUIZ", "EXTRA") or mode.startswith("CD"):
        rows = []  # (text, font, color, line height)
        if mode == "CARD":
            head = "HỌC TỪ TẬP NÀY"
            rows += [(t, font(62), (255, 255, 255), 80) for t in wrap(d, learn.get("en", ""), font(62), W - 200)]
            rows += [("", font(20), None, 20)] + [(t, font(46, False), (226, 232, 240), 62) for t in wrap(d, learn.get("vi", ""), font(46, False), W - 200)]
            rows += [("", font(20), None, 25)] + [(t, font(36), acc, 50) for t in wrap(d, learn.get("note", ""), font(36), W - 180)]
        elif mode == "EXTRA":
            head = "NÓI THÊM 2 CÂU TƯƠNG TỰ"
            for a, b in (learn.get("extra") or [])[:2]:
                rows += [(t, font(54), (255, 255, 255), 70) for t in wrap(d, a, font(54), W - 200)]
                rows += [(t, font(42, False), (226, 232, 240), 56) for t in wrap(d, b, font(42, False), W - 200)] + [("", font(20), None, 40)]
        else:
            head = "NẾU LÀ BẠN, BẠN SẼ NÓI GÌ?"
            rows += [(t, font(44, False), (226, 232, 240), 62) for t in wrap(d, "Gợi ý: " + learn.get("hint", ""), font(44, False), W - 200)]
        extra_h = 300 if mode.startswith("CD") else (120 if mode == "QUIZ" else 40)
        bot = max(1180 if mode == "CARD" else 0, 700 + sum(r[3] for r in rows) + extra_h)
        d.rounded_rectangle((60, 560, W - 60, bot), 36, fill=(15, 23, 42, 215), outline=acc, width=5)
        d.text((W // 2, 630), head, font=_fit(d, head, 46, 30, W - 180), fill=acc, anchor="mm")
        y = 700
        for t, f, col, lh in rows:
            if t:
                d.text((W // 2, y), t, font=f, fill=col, anchor="ma")
            y += lh
        if mode.startswith("CD"):
            d.text((W // 2, y + 140), mode[2], font=font(200), fill=(255, 255, 255), anchor="mm")
        elif mode == "QUIZ":
            d.text((W // 2, y + 50), "Bình luận câu của bạn trước khi xem đáp án!", font=font(36), fill=acc, anchor="mm")
        return im
    f = font(54)
    lines = wrap(d, ln["text"], f, W - 160)
    bh = 70 * len(lines) + 70
    top = H - 330 - bh
    d.rounded_rectangle((50, top, W - 50, top + bh), 28, fill=(0, 0, 0, 165))
    c = next((c for c in s.get("characters", []) if c["key"] == ln["spk"]), None)
    if c:
        tw = d.textlength(c["name"], font=font(40)) + 50
        d.rounded_rectangle((70, top - 40, 70 + tw, top + 22), 18, fill=tuple(c["color"]))
        d.text((95, top - 9), c["name"], font=font(40), fill=(255, 255, 255), anchor="lm")
    color = acc if ln.get("en") else (255, 255, 255)
    y = top + 45
    for t in lines:
        d.text((W // 2, y), t, font=f, fill=color, anchor="ma")
        y += 70
    return im


def _dur(p) -> float:
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)]))


def _h(*a) -> str:
    return hashlib.sha1(json.dumps(a, ensure_ascii=False).encode()).hexdigest()[:10]


def horror_bgm(out: Path) -> Path:
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=color=brown:amplitude=0.4:duration=150",
                        "-f", "lavfi", "-i", "sine=f=55:duration=150", "-f", "lavfi", "-i", "sine=f=82.4:duration=150",
                        "-f", "lavfi", "-i", "sine=f=116.5:duration=150", "-filter_complex",
                        "[0]lowpass=f=350,volume=0.7[n];[1]volume=0.6,tremolo=f=0.2:d=0.7[a];[2]volume=0.35,tremolo=f=0.13:d=0.8[b];"
                        "[3]volume=0.15,tremolo=f=0.31:d=0.9[c];[n][a][b][c]amix=inputs=4,afade=t=in:d=3,volume=3",
                        "-ar", "44100", str(out)], check=True)
    return out


def contact_sheet(images: list[Path], out: Path):
    ims = [Image.open(p).convert("RGB").resize((270, 480)) for p in images]
    cols = min(4, len(ims)) or 1
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 270, rows * 480), "black")
    for k, im in enumerate(ims):
        sheet.paste(im, ((k % cols) * 270, (k // cols) * 480))
    sheet.save(out, quality=85)


async def render_episode(s: dict, n: int, d: Path, progress=lambda frac, msg: None) -> dict:
    """Render episode n (1-based) into d; returns {video, sheet, duration}. Cached by content hash, so edits re-render only what changed."""
    ep = s["episodes"][n - 1]
    work, refs = d / f"ep{n}", d / "refs"
    work.mkdir(parents=True, exist_ok=True)
    scenes = expand(ep, s.get("english"))
    imgs = [work / f"s{i}_{_h(p, s.get('look'))}.png" for i, (p, _) in enumerate(scenes)]
    auds = [[work / f"a{i}_{j}_{_h(ln['spk'], ln['text'], ln.get('en'), s['narrator'])}.mp3" for j, ln in enumerate(ls)]
            for i, (_, ls) in enumerate(scenes)]
    total = len(imgs) + sum(map(len, auds))
    done = 0

    async def tick(coro, msg):
        nonlocal done
        await coro
        done += 1
        progress(0.8 * done / total, msg)

    await asyncio.gather(*[tick(scene_image(s, p, imgs[i], refs), f"Tập {n}: vẽ cảnh…") for i, (p, _) in enumerate(scenes)],
                         *[tick(line_audio(s, ln, auds[i][j]), f"Tập {n}: đọc lời thoại…")
                           for i, (_, ls) in enumerate(scenes) for j, ln in enumerate(ls)])
    progress(0.85, f"Tập {n}: dựng video…")
    segs = []
    for i, (_, ls) in enumerate(scenes):
        bg = work / f"bg{i}.png"
        Image.open(imgs[i]).convert("RGB").resize((W, H)).save(bg)
        ds = [_dur(a) + 0.35 for a in auds[i]]
        tot, t0 = sum(ds), 0.0
        for j, ln in enumerate(ls):
            ov = work / f"o{i}_{j}.png"
            overlay(s, ep, n, ln, title=(i == 0)).save(ov)
            seg = work / f"seg{i}_{j}.mp4"
            z = f"1+0.07*({t0:.3f}+on/{FPS})/{tot:.3f}"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-t", f"{ds[j]:.3f}", "-i", str(bg),
                            "-loop", "1", "-framerate", str(FPS), "-t", f"{ds[j]:.3f}", "-i", str(ov), "-i", str(auds[i][j]),
                            "-filter_complex",
                            f"[0]scale=1620:2880,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS}[b];"
                            "[b][1]overlay=0:0,format=yuv420p[v];[2]aresample=44100,apad[a]",
                            "-map", "[v]", "-map", "[a]", "-t", f"{ds[j]:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                            "-c:a", "aac", "-ac", "2", "-ar", "44100", str(seg)], check=True)
            segs.append(seg)
            t0 += ds[j]
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in segs))
    joined = work / "joined.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)], check=True)
    if s.get("look") == "horror":
        bgm, vol = horror_bgm(ROOT / "output" / "bgm_horror.mp3"), 0.35
    else:
        bgm, vol = ROOT / "bgm" / "default.mp3", 0.07
    video = d / f"tap{n}.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(joined), "-stream_loop", "-1", "-i", str(bgm), "-filter_complex",
                    f"[1]volume={vol}[b];[0:a][b]amix=inputs=2:duration=first:dropout_transition=0,volume=2[a]",
                    "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", str(video)], check=True)
    sheet = d / f"tap{n}_sheet.jpg"
    contact_sheet(imgs, sheet)
    progress(1.0, f"Tập {n}: xong")
    return {"video": video, "sheet": sheet, "duration": round(_dur(video), 1)}
