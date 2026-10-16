import asyncio
import json

import httpx
import pytest

from app import drama, facebook

NOW = 1_800_000_000


def test_check_schedule_window():
    assert facebook.check_schedule(NOW + 60, NOW) == "too_soon"
    assert facebook.check_schedule(NOW + 3600, NOW) == "ok"
    assert facebook.check_schedule(NOW + 29 * 86400, NOW) == "ok"
    assert facebook.check_schedule(NOW + 30 * 86400, NOW) == "too_far"


def _script(english):
    return drama.normalize({"title": "T", "english": english, "characters": [
        {"key": "Vy", "name": "Vy", "gender": "female", "age": 21, "look": "x"},
        {"key": "Tu", "name": "Bà Tư", "gender": "female", "age": 70, "look": "y"},
        {"key": "Mai", "name": "Mai", "gender": "female", "age": 8, "look": "z"}],
        "episodes": [{"title": "A", "scenes": [
            {"image": "Vy alone", "lines": [{"spk": "N", "text": "a"}, {"spk": "Bob", "text": "b"}]},
            {"image": "Vy and Tu", "lines": [{"spk": "VY", "text": "wrong", "en": True},
                                             {"spk": "Vy", "text": "right", "en": True, "answer": True},
                                             {"spk": "Tu", "text": "ok"}]}],
            "learn": {"hint": "h", "en": "right", "vi": "đúng", "note": "n", "extra": [["e1", "v1"], ["e2", "v2"]]}}]})


def test_normalize_voices_and_unknown_speakers():
    s = _script(False)
    v = {c["key"]: c["voice"] for c in s["characters"]}
    assert v == {"Vy": "Despina", "Tu": "Gacrux", "Mai": "Leda"}
    assert s["episodes"][0]["scenes"][0]["lines"][1]["spk"] == "N"


def test_expand_without_english_keeps_lines():
    sc = drama.expand(_script(False)["episodes"][0], False)
    assert [ln["mode"] for _, ls in sc for ln in ls] == [""] * 5


def test_expand_with_english_inserts_quiz_before_answer():
    sc = drama.expand(_script(True)["episodes"][0], True)
    modes = [(ln["text"], ln["mode"]) for ln in sc[1][1]]
    assert [m for _, m in modes] == ["", "QUIZ", "CD3", "CD2", "CD1", "", "CARD", "EXTRA", ""]
    assert modes[5][0] == "right"


def test_named_in_word_boundaries():
    assert drama.named_in("Mai under the tree, Vy watching", ["Vy", "Tu", "Mai"]) == ["Vy", "Mai"]
    assert drama.named_in("Tuesday morning", ["Tu"]) == []


def test_overlay_renders_all_modes():
    s = _script(True)
    ep = s["episodes"][0]
    for _, ls in drama.expand(ep, True):
        for ln in ls:
            assert drama.overlay(s, ep, 1, ln, title=True).size == (drama.W, drama.H)


def test_upload_far_schedule_becomes_waiting_draft(tmp_path, monkeypatch):
    calls = []

    def handler(req: httpx.Request):
        body = dict(httpx.QueryParams(req.content.decode())) if req.method == "POST" and b"upload_phase" in req.content else {}
        calls.append((req.method, req.url.path, body))
        if body.get("upload_phase") == "start":
            return httpx.Response(200, json={"video_id": "v1", "upload_url": "https://rupload.facebook.com/x"})
        if req.url.host == "rupload.facebook.com" or body.get("upload_phase") == "finish":
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"published": False, "status": {"video_status": "ready"}})

    real = httpx.AsyncClient
    monkeypatch.setattr(facebook.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    fb = facebook.Facebook(tmp_path / "fb.json")
    fb.state["pages"] = {"p1": {"name": "Page", "token": "secret"}}
    fb.state["page_id"] = "p1"
    vid = tmp_path / "a.mp4"
    vid.write_bytes(b"0" * 10)
    import time
    post = fb.new_post(str(vid), "cap", "schedule", int(time.time()) + 40 * 86400)
    asyncio.run(fb.upload(post))
    fin = next(b for _, _, b in calls if b.get("upload_phase") == "finish")
    assert fin["video_state"] == "DRAFT" and "scheduled_publish_time" not in fin
    assert post["state"] == "waiting" and post["fb"]["video_status"] == "ready"
    assert "secret" not in json.dumps(fb.public())


def test_new_post_rejects_too_soon(tmp_path):
    fb = facebook.Facebook(tmp_path / "fb.json")
    fb.state["pages"] = {"p1": {"name": "Page", "token": "t"}}
    fb.state["page_id"] = "p1"
    vid = tmp_path / "a.mp4"
    vid.write_bytes(b"0")
    import time
    with pytest.raises(facebook.FBError):
        fb.new_post(str(vid), "c", "schedule", int(time.time()) + 30)


def test_picked_ref_is_kept(tmp_path, monkeypatch):
    from PIL import Image
    calls = []

    async def fake_image(parts, out, ratio="9:16"):
        calls.append(out)
        Image.new("RGB", (8, 8)).save(out)

    monkeypatch.setattr(drama, "_image", fake_image)
    c = {"key": "Vy", "name": "Vy", "look": "a"}
    src = tmp_path / "me.png"
    Image.new("RGB", (40, 60), "red").save(src)
    drama.set_ref(c, tmp_path, src)
    asyncio.run(drama.make_ref({**c, "look": "changed"}, tmp_path))
    assert not calls and Image.open(tmp_path / "Vy.png").size == (40, 60)
    drama.reset_ref(c, tmp_path)
    asyncio.run(drama.make_ref(c, tmp_path))
    assert len(calls) == 1
    opts = asyncio.run(drama.ref_options(c, tmp_path, 3))
    assert len(opts) == 3 and all(o.parent == tmp_path / "cand" for o in opts)
