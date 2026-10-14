"""Google Vertex AI access: service-account JSON locally, metadata-server ADC on Cloud Run."""
import asyncio
import json
import os
import time

import httpx
import jwt

_SA = json.loads(os.environ["GCP_SA_KEY_JSON"]) if os.environ.get("GCP_SA_KEY_JSON") else None
_tok = {"v": None, "exp": 0.0}
_META = "http://metadata.google.internal/computeMetadata/v1"


def _project_from_metadata() -> str:
    r = httpx.get(f"{_META}/project/project-id", headers={"Metadata-Flavor": "Google"}, timeout=5)
    return r.text


PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT") or (_SA["project_id"] if _SA else None) or _project_from_metadata()
LOCATION = os.environ.get("VERTEX_LOCATION", "global")


async def token() -> str:
    if _tok["v"] and time.time() < _tok["exp"] - 120:
        return _tok["v"]
    async with httpx.AsyncClient(timeout=20) as c:
        if _SA:
            now = int(time.time())
            assertion = jwt.encode(
                {"iss": _SA["client_email"], "scope": "https://www.googleapis.com/auth/cloud-platform",
                 "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600},
                _SA["private_key"], algorithm="RS256")
            r = await c.post("https://oauth2.googleapis.com/token",
                             data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        else:
            r = await c.get(f"{_META}/instance/service-accounts/default/token", headers={"Metadata-Flavor": "Google"})
        r.raise_for_status()
        d = r.json()
    _tok["v"], _tok["exp"] = d["access_token"], time.time() + d.get("expires_in", 3600)
    return _tok["v"]


async def generate(model: str, body: dict) -> dict:
    host = "aiplatform.googleapis.com" if LOCATION == "global" else f"{LOCATION}-aiplatform.googleapis.com"
    url = f"https://{host}/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{model}:generateContent"
    r = None
    for attempt in range(6):
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(url, json=body, headers={"Authorization": f"Bearer {await token()}"})
        if r.status_code in (429, 500, 503):
            await asyncio.sleep(5 * (attempt + 1))
            continue
        break
    if r.status_code != 200:
        raise RuntimeError(f"Vertex {model} {r.status_code}: {r.text[:300]}")
    return r.json()
