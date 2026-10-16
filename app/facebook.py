"""Facebook Page Reels: connect a page, upload now / schedule / draft, reschedule, status."""
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import httpx

G = "https://graph.facebook.com/v23.0"
MIN_AHEAD = 10 * 60
MAX_AHEAD = 29 * 86400  # Meta rejects Reels scheduled ~30 days ahead; 29 is the observed safe limit
RETRY_EVERY = 15 * 60


class FBError(Exception):
    pass


def _err(r: dict) -> str:
    e = r.get("error") or {}
    return e.get("error_user_msg") or e.get("message") or json.dumps(r)[:300]


def check_schedule(ts: int, now: float | None = None) -> str:
    """'ok' if Meta accepts ts now, 'too_soon' if under 10 minutes ahead, 'too_far' if beyond the window."""
    now = time.time() if now is None else now
    if ts < now + MIN_AHEAD:
        return "too_soon"
    if ts > now + MAX_AHEAD:
        return "too_far"
    return "ok"


class Facebook:
    def __init__(self, path: Path):
        self.path = path
        self.state = json.loads(path.read_text()) if path.exists() else {}
        self.state.setdefault("pages", {})
        self.state.setdefault("posts", {})
        env_tok = os.environ.get("FACEBOOK_ACCESS_TOKEN")
        if env_tok and not self.state["pages"]:
            self._pending_env = env_tok
        else:
            self._pending_env = None

    def save(self):
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ---------- page connection ----------
    def public(self) -> dict:
        pages = [{"id": i, "name": p["name"]} for i, p in self.state["pages"].items()]
        pid = self.state.get("page_id")
        return {"connected": bool(pid), "page_id": pid,
                "page_name": self.state["pages"].get(pid, {}).get("name"), "pages": pages}

    async def connect(self, token: str) -> dict:
        async with httpx.AsyncClient(timeout=60) as c:
            me = (await c.get(f"{G}/me", params={"fields": "id,name", "access_token": token})).json()
            if "id" not in me:
                raise FBError("Token không hợp lệ: " + _err(me))
            acc = (await c.get(f"{G}/me/accounts", params={"fields": "id,name,access_token", "limit": 100,
                                                            "access_token": token})).json()
        pages = {p["id"]: {"name": p["name"], "token": p["access_token"]} for p in acc.get("data", []) if p.get("access_token")}
        if not pages:  # already a Page token
            pages = {me["id"]: {"name": me["name"], "token": token}}
        self.state["pages"].update(pages)
        if self.state.get("page_id") not in self.state["pages"]:
            self.state["page_id"] = next(iter(pages))
        self.save()
        return self.public()

    async def ensure_env(self):
        if self._pending_env:
            tok, self._pending_env = self._pending_env, None
            try:
                await self.connect(tok)
            except Exception:
                pass

    def select(self, page_id: str) -> dict:
        if page_id not in self.state["pages"]:
            raise FBError("Không tìm thấy page")
        self.state["page_id"] = page_id
        self.save()
        return self.public()

    def disconnect(self):
        self.state["pages"], self.state["page_id"] = {}, None
        self.save()

    def _page(self, page_id: str | None = None) -> tuple[str, str]:
        pid = page_id or self.state.get("page_id")
        if not pid or pid not in self.state["pages"]:
            raise FBError("Chưa kết nối page Facebook")
        return pid, self.state["pages"][pid]["token"]

    # ---------- posts ----------
    def posts(self) -> list[dict]:
        return sorted(self.state["posts"].values(), key=lambda p: p["created"], reverse=True)

    def new_post(self, file: str, caption: str, mode: str, ts: int | None, title: str = "") -> dict:
        if mode not in ("now", "schedule", "draft"):
            raise FBError("Chế độ đăng không hợp lệ")
        if not Path(file).is_file():
            raise FBError("Không tìm thấy file video")
        if mode == "schedule":
            if not ts:
                raise FBError("Chưa chọn giờ đăng")
            if check_schedule(ts) == "too_soon":
                raise FBError("Giờ hẹn phải sau thời điểm hiện tại ít nhất 10 phút")
        pid, _ = self._page()
        post = {"id": uuid.uuid4().hex[:10], "page_id": pid, "file": file, "title": title, "caption": caption,
                "mode": mode, "ts": ts if mode == "schedule" else None, "state": "queued", "video_id": None,
                "error": "", "created": time.time(), "fb": {}}
        self.state["posts"][post["id"]] = post
        self.save()
        return post

    async def upload(self, post: dict):
        pid, tok = self._page(post["page_id"])
        post["state"] = "uploading"
        self.save()
        try:
            async with httpx.AsyncClient(timeout=600) as c:
                r = (await c.post(f"{G}/{pid}/video_reels", data={"upload_phase": "start", "access_token": tok})).json()
                if "video_id" not in r:
                    raise FBError("Không bắt đầu tải lên được: " + _err(r))
                vid = post["video_id"] = r["video_id"]
                data = Path(post["file"]).read_bytes()
                u = (await c.post(r["upload_url"], content=data, headers={
                    "Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(len(data))})).json()
                if not u.get("success"):
                    raise FBError("Tải file lên thất bại: " + _err(u))
                fin = {"upload_phase": "finish", "video_id": vid, "description": post["caption"], "access_token": tok,
                       "video_state": "PUBLISHED"}
                wait = False
                if post["mode"] == "draft":
                    fin["video_state"] = "DRAFT"
                elif post["mode"] == "schedule":
                    if check_schedule(post["ts"]) == "ok":
                        fin.update(video_state="SCHEDULED", scheduled_publish_time=str(post["ts"]))
                    else:
                        fin["video_state"], wait = "DRAFT", True
                f = (await c.post(f"{G}/{pid}/video_reels", data=fin)).json()
                if not f.get("success"):
                    raise FBError("Facebook từ chối: " + _err(f))
            post["state"] = "waiting" if wait else {"now": "published", "draft": "draft", "schedule": "scheduled"}[post["mode"]]
            post["link"] = f"https://www.facebook.com/reel/{vid}"
            await self.refresh(post)
        except Exception as e:
            post["state"], post["error"] = "error", str(e)
        self.save()

    async def refresh(self, post: dict) -> dict:
        if not post.get("video_id"):
            return post
        _, tok = self._page(post["page_id"])
        async with httpx.AsyncClient(timeout=60) as c:
            s = (await c.get(f"{G}/{post['video_id']}", params={
                "fields": "published,scheduled_publish_time,status,permalink_url", "access_token": tok})).json()
        if "error" in s:
            post["error"] = _err(s)
        else:
            st = s.get("status") or {}
            post["fb"] = {"video_status": st.get("video_status"), "published": s.get("published"),
                          "scheduled_publish_time": s.get("scheduled_publish_time"),
                          "publish_status": (st.get("publishing_phase") or {}).get("publish_status")}
            if s.get("published"):
                post["state"] = "published"
        self.save()
        return post

    async def reschedule(self, post: dict, ts: int) -> dict:
        if not post.get("video_id"):
            raise FBError("Video chưa tải lên Facebook")
        if check_schedule(ts) == "too_soon":
            raise FBError("Giờ hẹn phải sau thời điểm hiện tại ít nhất 10 phút")
        post["ts"], post["mode"] = ts, "schedule"
        if check_schedule(ts) == "too_far":
            post["state"], post["error"] = "waiting", ""
            self.save()
            return post
        _, tok = self._page(post["page_id"])
        async with httpx.AsyncClient(timeout=60) as c:
            r = (await c.post(f"{G}/{post['video_id']}", data={
                "published": "false", "scheduled_publish_time": str(ts), "access_token": tok})).json()
        if r.get("success"):
            post["state"], post["error"] = "scheduled", ""
        else:
            post["error"] = _err(r)
            if post["state"] != "waiting":
                post["state"] = "error"
        await self.refresh(post)
        return post

    async def retry_loop(self):
        """Schedule 'waiting' drafts once their time enters Meta's window."""
        while True:
            for p in list(self.state["posts"].values()):
                if p["state"] == "waiting" and p.get("ts") and check_schedule(p["ts"]) == "ok":
                    try:
                        await self.reschedule(p, p["ts"])
                    except Exception as e:
                        p["error"] = str(e)
            self.save()
            await asyncio.sleep(RETRY_EVERY)
