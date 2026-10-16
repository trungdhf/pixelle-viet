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

## Deploy lên Cloud Run

Mọi lệnh chạy trong **Cloud Shell** (https://shell.cloud.google.com), không cần cài gì trên máy.

### 1. Lấy code
```bash
git clone https://github.com/trungdhf/pixelle-viet.git
cd pixelle-viet
```
(Hoặc Upload file zip trong menu ⋮ của Cloud Shell rồi `unzip pixelle-viet.zip && cd pixelle-viet`.)

### 2. Chọn project, bật API (1 lần)
```bash
PROJECT=gen-lang-client-0686202573
gcloud config set project $PROJECT
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com aiplatform.googleapis.com
```

### 3. Lưu key ElevenLabs vào Secret Manager (1 lần)
```bash
echo -n "KEY_ELEVENLABS" | gcloud secrets create elevenlabs-key --data-file=-
# đổi key sau này:
# echo -n "KEY_MOI" | gcloud secrets versions add elevenlabs-key --data-file=-
```

### 4. Cấp quyền cho service account của Cloud Run (1 lần)
```bash
SA=$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$SA --role=roles/aiplatform.user
gcloud secrets add-iam-policy-binding elevenlabs-key --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$SA --role=roles/cloudbuild.builds.builder
```

### 5. Deploy (5–10 phút)
```bash
gcloud run deploy pixelle-viet --source . --region asia-northeast1 --allow-unauthenticated \
  --memory 4Gi --cpu 2 --timeout 900 --max-instances 1 --no-cpu-throttling \
  --set-secrets ELEVENLABS_API_KEY=elevenlabs-key:latest \
  --set-env-vars APP_PASSWORD=dat-mat-khau-o-day
```
Hỏi tạo Artifact Registry thì gõ `Y`. Xong sẽ có link `https://pixelle-viet-xxxx.asia-northeast1.run.app`.

### 6. Cập nhật code mới
```bash
cd pixelle-viet && git pull
# rồi chạy lại lệnh ở bước 5
```

### Ghi chú
- Vertex không cần key: trên Cloud Run app tự lấy token của service account từ metadata server.
- `APP_PASSWORD`: mật khẩu để tạo video / nghe thử giọng, tránh người lạ dùng tốn tiền.
- `--max-instances 1 --no-cpu-throttling`: video tạo chạy nền, trạng thái lưu trong bộ nhớ instance, nên cần đúng 1 instance và CPU luôn bật. Không ai dùng thì instance tự tắt.
- Video lưu trên đĩa tạm của instance; instance tắt thì mất. Tải video về ngay sau khi tạo.

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

## Phim ngắn nhiều tập + đăng Facebook (`/series`)

Mở http://localhost:8080/series:

1. **Ý tưởng:** nhập cốt truyện, số tập, số cảnh, thể loại (tình cảm, hài, lãng mạn, kinh dị). Tick **"Dạy tiếng Anh"** nếu muốn mỗi tập có đếm ngược 3-2-1, thẻ "Học từ tập này" và 2 câu tương tự. Bỏ tick thì phim chỉ có tiếng Việt.
2. **Kịch bản:** AI (Vertex Gemini) viết cả series ra JSON, có nhân vật cố định. Sửa được trước khi làm video.
3. **Video:** app vẽ ảnh mẫu cho từng nhân vật, rồi vẽ từng cảnh (ảnh người thật, dùng ảnh mẫu để giữ mặt), đọc thoại bằng Gemini TTS (mỗi nhân vật 1 giọng), dựng MP4 dọc 1080x1920. Mỗi tập có 1 ảnh tổng hợp các cảnh để kiểm tra. Sửa kịch bản rồi làm lại thì chỉ cảnh bị sửa mới được vẽ lại.
4. **Facebook:** dán Page access token (hoặc user token có quyền `pages_manage_posts`, `pages_read_engagement`, `pages_show_list`) rồi chọn page. Mỗi tập có nút **Đăng ngay / Hẹn giờ / Lưu nháp**, hoặc dùng **Hẹn giờ tất cả tập** (VD `06:00,12:00,19:00`, giờ VN hoặc giờ Nhật).
   - Facebook chỉ cho hẹn giờ Reel trong khoảng 29 ngày tới. Nếu xa hơn, app tải lên dạng bản nháp (trạng thái "Nháp – chờ hẹn giờ") và tự hẹn giờ khi tới hạn, miễn là app còn chạy.
   - Token lưu ở `output/facebook.json` trên máy chạy app (không đưa lên git). Cũng có thể đặt biến môi trường `FACEBOOK_ACCESS_TOKEN`.
   - Tag @người theo dõi không làm qua API được, phải thêm tay trong app Facebook.

Kiểm thử: `pip install pytest && python -m pytest tests`

## Chạy trên Windows

1. Cài **Python 3.12** (python.org, tick "Add python.exe to PATH"), **Git**, và **ffmpeg**: mở cmd và chạy `winget install Gyan.FFmpeg`.
2. Tải code và đặt key Google Cloud:
   ```bat
   git clone https://github.com/trungdhf/pixelle-viet
   cd pixelle-viet
   ```
   Chép file key service account (quyền `roles/aiplatform.user`) vào thư mục `pixelle-viet`, đặt tên `service-account.json`.
3. Nhấp đúp `run_windows.bat`. Lần đầu script tự tạo `.venv` và cài thư viện, mất vài phút. Sau đó mở http://localhost:8080/series
   - Tùy chọn: trước khi chạy, mở cmd và gõ `set ELEVENLABS_API_KEY=...` (giọng ElevenLabs cho video đơn) hoặc `set APP_PASSWORD=...`.

