# Hệ thống Multi-Account Google Flow

> Tài liệu kiến trúc cho việc chạy nhiều tài khoản Flow song song,
> phục vụ tạo ảnh (Imagen) và video (Veo 3) tự động theo yêu cầu.

---

## 1. Tổng quan mô hình

```
                         ┌──────────────┐
  Người dùng ──────────► │  API Server  │
  (gọi API tạo video)   │  (FastAPI)   │
                         └──────┬───────┘
                                │
                         ┌──────▼───────┐
                         │    Queue     │
                         │ (Redis/DB)   │
                         └──────┬───────┘
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                  ▼
     ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
     │   Worker 1     │ │   Worker 2     │ │   Worker 3     │
     │   Acc A        │ │   Acc B        │ │   Acc C        │
     │   Proxy IP 1   │ │   Proxy IP 2   │ │   Proxy IP 3   │
     │   Chrome ○     │ │   Chrome ○     │ │   Chrome ○     │
     └────────────────┘ └────────────────┘ └────────────────┘
```

**3 phần chính:**

| Thành phần | Vai trò | Số lượng |
|---|---|---|
| **API Server** | Nhận request, quản lý job, trả kết quả | 1 |
| **Queue** | Hàng đợi phân phối job cho worker | 1 |
| **Worker** | Mỗi worker = 1 browser + 1 acc Flow | Nhiều (tùy scale) |

---

## 2. Flow người dùng

```
1. User gọi API:    POST /generate {prompt: "...", type: "video"}
2. Server tạo job:  job_id = "abc123", status = "queued"
3. Queue phân phối: Worker rảnh nhất nhận job
4. Worker thực thi: Mở Flow → paste prompt → chờ render → tải video
5. Video xong:      Upload lên storage, cập nhật status = "done"
6. User poll:       GET /job/abc123 → nhận link download video
```

---

## 3. Proxy — Tại sao cần & cách dùng

### 3.1. Google kiểm tra gì?

Google **không chặn** chỉ vì nhiều acc cùng IP (gia đình/công ty dùng chung wifi là bình thường).
Họ chặn khi thấy **dấu hiệu bot hàng loạt**:

| Dấu hiệu | Rủi ro |
|---|---|
| 2-3 acc cùng IP, hành vi tự nhiên | 🟢 An toàn |
| 5-10 acc cùng IP, tạo video liên tục | ⚠️ Trung bình |
| Các acc có hành vi giống hệt nhau (timing, pattern) | 🔴 Cao |
| Browser fingerprint giống nhau | 🔴 Cao |
| 50+ acc cùng IP | 🔴 Rất cao |

### 3.2. Playwright hỗ trợ proxy gốc

Không cần extension, không cần trình duyệt đặc biệt.
Chỉ cần thêm tham số khi launch browser:

```python
# HTTP Proxy
browser = playwright.chromium.launch(
    proxy={
        "server": "http://proxy-server.com:8080",
        "username": "user123",
        "password": "pass456"
    }
)

# SOCKS5 Proxy
browser = playwright.chromium.launch(
    proxy={"server": "socks5://proxy-server.com:1080"}
)
```

Toàn bộ traffic của browser sẽ đi qua proxy đó.
Google chỉ thấy IP proxy, không thấy IP thật của máy.

### 3.3. Config multi-acc với proxy

Mỗi tài khoản gán 1 proxy riêng:

```
Acc A → Chrome profile A → Proxy http://1.1.1.1:8080 → IP riêng
Acc B → Chrome profile B → Proxy http://2.2.2.2:8080 → IP riêng
Acc C → Chrome profile C → Proxy http://3.3.3.3:8080 → IP riêng
```

File config dạng JSON hoặc env:
```json
{
  "workers": [
    {
      "account": "acc_a@gmail.com",
      "chrome_profile": "/profiles/acc_a",
      "proxy": "http://user:pass@proxy1.example.com:8080"
    },
    {
      "account": "acc_b@gmail.com",
      "chrome_profile": "/profiles/acc_b",
      "proxy": "http://user:pass@proxy2.example.com:8080"
    },
    {
      "account": "acc_c@gmail.com",
      "chrome_profile": "/profiles/acc_c",
      "proxy": "http://user:pass@proxy3.example.com:8080"
    }
  ]
}
```

### 3.4. Loại proxy nên dùng

| Loại | Giá | Độ an toàn | Ghi chú |
|---|---|---|---|
| **Residential** (IP nhà dân) | $2-5/tháng/IP | 🟢 Rất an toàn | Google không phân biệt được với người thật |
| **ISP Proxy** (IP tĩnh residential) | $3-8/tháng/IP | 🟢 Rất an toàn | Ổn định hơn residential thường |
| **Datacenter** (IP server) | $0.5-1/tháng/IP | 🔴 Rủi ro | Google dễ nhận ra, KHÔNG nên dùng cho Flow |
| **Mobile** (IP 4G/5G) | $5-15/tháng/IP | 🟢 An toàn nhất | Đắt nhất nhưng khó detect nhất |

**Nhà cung cấp phổ biến:** Bright Data, IPRoyal, Smartproxy, Webshare, Proxy-Cheap

---

## 4. Tối ưu RAM — Headless + Block Resource

Google Flow là app web nặng (React, WebSocket, video preview) → bắt buộc dùng browser thật.
Nhưng có thể **giảm RAM từ ~800MB xuống ~150-200MB** mỗi instance.

### 4.1. Chạy Headless (không hiển thị giao diện)

| Mode | RAM/instance |
|---|---|
| Chrome có giao diện (hiện tại) | ~600MB - 1GB |
| Chrome headless | ~200 - 400MB |

Giảm **gần một nửa** RAM chỉ bằng 1 flag.

> **Headless mới ("new headless") có an toàn không?**
>
> Playwright mặc định dùng headless mới — fingerprint **giống 100% Chrome thường**:
> - User-Agent: không có chữ `HeadlessChrome`
> - `navigator.webdriver`: trả về `false` (như người thật)
> - WebGL, Audio API: đầy đủ
>
> → Google Flow **không phân biệt được** với Chrome có giao diện.

### 4.2. Block resource không cần thiết

Flow tải rất nhiều thứ mà script không cần:
- Video preview/thumbnail trên UI
- Ảnh banner quảng cáo
- Font, icon, animation CSS
- Google Analytics, tracking scripts

Block hết → mỗi instance giảm thêm ~100-200MB.

> **Google có phát hiện block resource không?**
>
> **Không.** Block resource xảy ra phía client (interceptor Playwright).
> Server chỉ gửi HTML+JS về, browser tự quyết định có tải thêm ảnh/font hay không.
> Server **không nhận được thông báo** rằng browser đã bỏ qua resource nào.
>
> | Hành động | Google thấy? |
> |---|---|
> | Block ảnh thumbnail | ❌ Không |
> | Block Google Analytics | ❌ Không |
> | Block banner video | ❌ Không |
> | Block font/CSS | ❌ Không |
>
> **Cẩn thận:** Đừng block API quan trọng (`projectInitialData`, `batchAsyncGenerateVideoText`...)

### 4.3. Chrome flags giảm RAM

| Flag | Tác dụng |
|---|---|
| `--disable-gpu` | Tắt GPU rendering |
| `--disable-dev-shm-usage` | Không dùng shared memory (quan trọng trên VPS) |
| `--disable-extensions` | Tắt extension |
| `--disable-background-networking` | Tắt sync nền |
| `--js-flags="--max-old-space-size=256"` | Giới hạn JS heap 256MB |
| `--single-process` | Gộp process (tiết kiệm nhưng kém ổn định) |

### 4.4. So sánh trình duyệt

Playwright hỗ trợ 3 engine:

| Engine | RAM/instance | Tương thích Flow | Ghi chú |
|---|---|---|---|
| **Chromium** (Chrome) | ~300-500MB | ✅ Hoàn hảo | Mặc định, ổn định nhất |
| **Firefox** | ~200-350MB | ✅ Tốt | Nhẹ hơn Chrome ~30-40% |
| **WebKit** (Safari) | ~100-200MB | ⚠️ Có thể lỗi UI | Nhẹ nhất nhưng rủi ro |

### 4.5. Gọi API trực tiếp — không cần browser (tương lai)

Script đã bắt được API endpoint của Flow:

```
POST aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText
→ Response: mediaId, status PENDING

GET labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={mediaId}
→ 307 redirect → download video
```

Nếu gọi trực tiếp bằng Python (không browser): **RAM chỉ ~5-10MB/acc**, chạy **500+ acc trên 1 máy 8GB**.

Nhưng khó vì cần xử lý:
- `access_token` Google OAuth (login 1 lần, refresh khi hết hạn)
- reCAPTCHA token trong mỗi request

---

## 5. Giới hạn phần cứng & Ước tính số acc

### 5.1. Tài nguyên mỗi worker

| Tài nguyên | Không tối ưu | Có tối ưu (headless + block) |
|---|---|---|
| RAM | ~600MB - 1GB | ~150 - 250MB |
| CPU | ~20-30% | ~5-10% |
| Disk | ~200MB / profile | ~200MB / profile |
| Bandwidth | ~5-50MB / video | ~5-50MB / video |

### 5.2. Số acc trên mỗi máy

| Cấu hình máy | Không tối ưu | Headless + Block resource |
|---|---|---|
| MacBook 8GB RAM | 3-4 worker | 15-20 worker |
| MacBook 16GB RAM | 6-8 worker | 35-45 worker |
| VPS 4GB RAM | 2-3 worker | 10-12 worker |
| VPS 8GB RAM | 5-6 worker | 20-25 worker |
| Server 32GB RAM | 15-20 worker | 80-100 worker |

### 5.3. So sánh tổng hợp các phương pháp

| Phương pháp | RAM/acc | Số acc (máy 16GB) | Độ khó |
|---|---|---|---|
| Chrome có giao diện (hiện tại) | ~800MB | 10-15 | ✅ Đang chạy |
| Chrome headless + block resource | ~200MB | 35-45 | 🟡 Dễ |
| Firefox headless + block resource | ~150MB | 50-60 | 🟡 Dễ |
| Gọi API trực tiếp (không browser) | ~5MB | 500+ | 🔴 Khó (reCAPTCHA) |

---

## 6. Kế hoạch triển khai theo giai đoạn

### Giai đoạn 1 — Chạy local (1 máy, 2-3 acc)

**Mục tiêu:** Test ổn định, không cần proxy

- Chạy trên máy Mac hiện tại
- 2-3 tài khoản Flow, mỗi acc 1 Chrome profile riêng
- Cùng IP (chấp nhận được với 2-3 acc)
- Script `dreamina.py` chạy tuần tự: acc A xong → acc B → acc C
- Lưu video local vào `output_images/`

**Cần làm:**
- Tạo thêm Chrome profile cho mỗi acc
- Config file liệt kê danh sách acc + profile path
- Script chạy vòng lặp qua từng acc

**Chi phí:** $0

---

### Giai đoạn 2 — Thêm API + Queue (1 máy, 5-10 acc)

**Mục tiêu:** Nhận request từ bên ngoài, chạy song song

- Thêm FastAPI server nhận request tạo video
- Queue đơn giản (SQLite hoặc Redis)
- Nhiều worker chạy song song (mỗi acc 1 process)
- Thêm proxy residential cho mỗi acc
- Upload video lên cloud storage (Google Drive / S3)

**Cần làm:**
- Build API server (FastAPI)
- Build queue system
- Wrap `dreamina.py` thành worker service
- Mua proxy residential
- Config proxy cho từng acc

**Chi phí:** ~$20-50/tháng (proxy)

---

### Giai đoạn 3 — Scale lên cloud (nhiều VPS, 20+ acc)

**Mục tiêu:** Chạy production, phục vụ nhiều người

- API server trên 1 VPS chính
- Worker phân tán trên nhiều VPS nhỏ
- Mỗi VPS chạy 2-3 acc, IP riêng tự nhiên (không cần proxy)
- Dashboard quản lý: xem acc nào còn credit, job nào đang chạy
- Auto health check: restart worker khi crash, cảnh báo khi acc bị limit

**Cần làm:**
- Deploy API server lên VPS
- Script tự động setup worker trên VPS mới
- Dashboard monitoring
- Hệ thống cảnh báo (Telegram bot)

**Chi phí:** ~$100-300/tháng (VPS + domain)

---

## 7. Theo dõi credit & auto-switch acc

Mỗi response từ Flow API trả về `remainingCredits`:

```json
{
  "remainingCredits": 24790
}
```

Hệ thống cần:
- Ghi lại credit còn lại sau mỗi job
- Khi credit < ngưỡng (vd: 100) → tự chuyển job sang acc khác
- Cảnh báo khi acc sắp hết credit
- Dashboard hiển thị credit từng acc realtime

---

## 8. Đa dạng hóa hành vi (Anti-detection)

Để Google không phát hiện nhiều acc là bot:

| Kỹ thuật | Mô tả |
|---|---|
| **Random delay** | Thời gian chờ giữa các prompt: 2-8s (random) |
| **Random typing speed** | Tốc độ gõ phím khác nhau mỗi lần |
| **Random viewport** | Mỗi acc dùng resolution khác: 1440x900, 1920x1080, 1366x768 |
| **Random user-agent** | Chrome version hơi khác nhau |
| **Không chạy 24/7** | Mỗi acc chỉ hoạt động 8-12h/ngày, giờ khác nhau |
| **Session gap** | Sau mỗi batch, nghỉ 5-15 phút rồi chạy tiếp |

---

## 9. Xử lý lỗi & Recovery

| Tình huống | Xử lý |
|---|---|
| Browser crash | Worker tự restart, job quay lại queue |
| Login hết hạn (cookie expired) | Health check phát hiện → cảnh báo cần re-login |
| Acc bị rate limit | Tạm dừng acc đó 1-2h, chuyển job sang acc khác |
| Acc bị khóa | Đánh dấu disabled, không assign job nữa |
| Proxy die | Fallback sang proxy dự phòng hoặc direct |
| Video render thất bại | Retry 1-2 lần, nếu vẫn fail → báo user |

---

## 10. Cấu trúc thư mục dự kiến

```
flow-system/
├── api_server/           # FastAPI server nhận request
│   ├── main.py
│   ├── routes/
│   └── models/
├── queue/                # Job queue management
│   ├── queue_manager.py
│   └── job_model.py
├── workers/              # Worker chạy browser
│   ├── worker.py         # Main worker loop
│   ├── dreamina.py       # Core automation (hiện tại)
│   └── profiles/         # Chrome profiles cho từng acc
│       ├── acc_a/
│       ├── acc_b/
│       └── acc_c/
├── config/
│   ├── accounts.json     # Danh sách acc + proxy
│   └── settings.json     # Cấu hình chung
├── storage/              # Video output tạm
├── prompts/              # Prompt templates
└── logs/                 # Log từng worker
```
