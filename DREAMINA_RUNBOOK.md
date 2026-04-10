# Dreamina Runbook (Vận hành Chuẩn)

## 1) Mục tiêu của script
Script `dreamina.py` tự động:

1. Mở Dreamina bằng profile Chrome đã lưu (không phải đăng nhập lại mỗi lần).
2. Gửi prompt hàng loạt.
3. Chờ cố định sau prompt cuối.
4. Tải **1 ảnh / 1 cảnh** theo đúng số cảnh (`canh_0001.png`, `canh_0002.png`, ...).
5. Tránh nhầm ảnh cũ bằng cách ưu tiên dữ liệu API.

## 2) Cấu hình hiện tại (đã chốt)
- Tốc độ gửi: `1s/prompt`
- Chờ sau prompt cuối: `60s`
- Pool prompt: `prompts/prompt_pool_1000.txt`
- Mỗi lần chạy lấy: `200` prompt
- Chỉ tải theo API map (an toàn): `STRICT_API_ONLY = True`
- Lọc API cũ: `API_HISTORY_MAX_AGE_SEC = 600` (10 phút)

## 3) Luồng hoạt động dễ hiểu (Request/Response)
### 3.1 Request gửi đi
Khi script nhấn Enter để gửi prompt, trang Dreamina gửi request API kiểu:
- `.../aigc_draft/generate`

Request này chứa nội dung prompt (ví dụ có `"CẢNH 0099: ..."`).

### 3.2 Response nhận về
API `generate` trả về dữ liệu như:
- `submit_id`
- `history_record_id`

Script dùng các ID này để biết “đây là job của lần chạy hiện tại”.

### 3.3 API lịch sử ảnh
Sau đó trang gọi API kiểu:
- `.../get_history_by_ids`

Response thường có:
- `common_attr.description` (chứa text cảnh)
- `common_attr.cover_url` (URL ảnh)
- `created_time` / `create_time` (thời điểm tạo)

Script map theo logic:
- `scene_number -> image_url`
- rồi tải đúng URL đó về file ảnh tương ứng.

## 4) Vì sao không nhầm ảnh cũ
Script đang chặn nhầm theo 3 lớp:

1. **Theo run hiện tại**: tin tưởng `submit_id` vừa phát sinh từ request `generate` trong phiên này.
2. **Theo thời gian**: record cũ quá 10 phút sẽ bị loại.
3. **Không fallback DOM** (khi `STRICT_API_ONLY=True`): nếu thiếu URL thì bỏ qua, không đoán ảnh theo vị trí UI.

Kết quả thực tế:
- Có thể thiếu ảnh nếu server trả chậm.
- Nhưng giảm mạnh việc lấy nhầm ảnh từ lần chạy cũ.

## 5) Cách chạy chuẩn
Chạy:

```bash
cd "/Users/may1/Desktop/untitled folder 3"
python3 dreamina.py
```

Khi script hỏi:
- `Dùng prompt test tự sinh...`: chọn `n` để dùng pool thật.
- Setup model/tỷ lệ trên UI xong, nhấn Enter để bắt đầu.

## 6) Kết quả/đầu ra nằm ở đâu
- Ảnh tải về: `output_images/`
- Prompt batch hiện tại: `prompts.txt`
- Log debug phiên chạy: `debug_sessions/session_YYYYMMDD_HHMMSS/`

File debug quan trọng:
- `network_api_debug.json`: request/response API
- `download_hashes.json`: danh sách file đã tải + hash
- `debug_log.json`: log tổng hợp

## 7) Đọc log để biết có ổn không
Các dòng bạn cần nhìn nhanh:

- `Lấy 200 prompt từ pool (x..y / 1000)`  
  -> xác nhận đúng batch.
- `Đã gửi hết ... prompts — chờ 60s`  
  -> xác nhận đúng mốc chờ.
- `API map bắt được A/B cảnh`  
  -> biết tỷ lệ map API.
- `Thiếu URL API cho canh_xxxx -> bỏ qua`  
  -> cảnh bị thiếu (không tải nhầm).
- `Đã lưu N ảnh tổng cộng`  
  -> kết quả cuối cùng.

## 8) Xử lý sự cố nhanh (playbook)
### Trường hợp A: Tải thiếu nhiều ảnh
Nguyên nhân thường gặp:
- Server trả chậm hơn 60s.
- Hàng đợi Dreamina đang đông.

Cách xử lý:
1. Chạy lại batch đó.
2. Giảm batch size (ví dụ 200 -> 100 hoặc 50) để ổn định hơn.
3. Giữ `STRICT_API_ONLY=True` để không đổi thiếu ảnh thành nhầm ảnh.

### Trường hợp B: Nghi ngờ nhầm ảnh cũ
Checklist:
1. Kiểm tra log có `STRICT_API_ONLY=True`.
2. Kiểm tra có dòng `lọc history cũ hơn 600s`.
3. Mở `network_api_debug.json` xem cảnh đó có `cover_url` mới không.

### Trường hợp C: Gửi prompt không đều
Checklist:
1. Kiểm tra `DELAY_SEC = 1`.
2. Nếu UI lag mạnh, tốc độ thực tế có thể >1s/prompt (bình thường).

## 9) Ghi chú vận hành
- Mỗi lần chạy script đã reset `output_images` để tránh lẫn file cũ.
- Script không xóa cache/profile của bạn.
- Nếu cần an toàn tối đa về đúng cảnh: chấp nhận thiếu ảnh, không bật fallback DOM.
