# Pixelle Việt

Web tạo video ngắn bằng AI, giao diện tiếng Việt, dựa trên [Pixelle-Video](https://github.com/AIDC-AI/Pixelle-Video) (Apache-2.0).

Nhập chủ đề (hoặc dán kịch bản) → AI viết lời thoại → vẽ ảnh từng cảnh → đọc lời → ghép nhạc nền → ra file MP4.

| Phần | Dịch vụ |
|---|---|
| Kịch bản, tiêu đề | Vertex AI `gemini-2.5-flash` |
| Ảnh minh họa | Vertex AI `gemini-3.1-flash-image` |
| Giọng đọc | **ElevenLabs** `eleven_v4` hoặc **Gemini TTS** (Vertex `gemini-2.5-flash-tts` / `gemini-2.5-pro-tts`) |
| Dựng video | Pixelle-Video (HTML template + Chromium + ffmpeg) |

## Chạy trên máy

```bash
pip install -r requirements.txt
playwright install --with-deps chromium
export GCP_SA_KEY_JSON="$(cat service-account.json)"   # SA có quyền roles/aiplatform.user
export ELEVENLABS_API_KEY=...                           # tùy chọn
uvicorn app.server:app --port 8080
```

Cần có `ffmpeg`. Mở http://localhost:8080

## Deploy lên Cloud Run (chạy trong Cloud Shell)

```bash
PROJECT=gen-lang-client-0686202573
gcloud config set project $PROJECT

# 1. Key ElevenLabs lưu trong Secret Manager (không ghi vào code)
echo -n "KEY_ELEVENLABS" | gcloud secrets create elevenlabs-key --data-file=-

# 2. Cấp quyền cho service account của Cloud Run (mặc định: <số project>-compute@developer.gserviceaccount.com)
SA=$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$SA --role=roles/aiplatform.user
gcloud secrets add-iam-policy-binding elevenlabs-key --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor

# 3. Deploy
gcloud run deploy pixelle-viet --source . --region asia-northeast1 --allow-unauthenticated \
  --memory 4Gi --cpu 2 --timeout 900 --max-instances 1 --no-cpu-throttling \
  --set-secrets ELEVENLABS_API_KEY=elevenlabs-key:latest \
  --set-env-vars APP_PASSWORD=matkhau-cua-ban
```

- Vertex: trên Cloud Run app tự lấy token từ metadata server, không cần file JSON.
- `APP_PASSWORD`: đặt mật khẩu để người lạ không dùng tốn tiền Vertex/ElevenLabs.
- `--max-instances 1 --no-cpu-throttling`: video tạo chạy nền, trạng thái lưu trong bộ nhớ instance, nên cần đúng 1 instance và CPU luôn bật.
- Video lưu trên đĩa tạm của instance; instance tắt thì mất. Hãy tải video về ngay sau khi tạo.

## Biến môi trường

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `GCP_SA_KEY_JSON` | – | JSON service account (chỉ dùng khi chạy local) |
| `GOOGLE_CLOUD_PROJECT` | project của SA / metadata | Project Vertex |
| `VERTEX_LOCATION` | `global` | Vùng Vertex |
| `CHAT_MODEL` | `gemini-2.5-flash` | Model viết kịch bản |
| `IMAGE_MODEL` | `gemini-3.1-flash-image` | Model vẽ ảnh |
| `ELEVENLABS_API_KEY` | – | Key ElevenLabs |
| `APP_PASSWORD` | – | Mật khẩu web (để trống = không cần) |
