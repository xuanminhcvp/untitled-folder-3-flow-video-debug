"""
Dreamina Auto Image Generator — Text to Image (T2I)
Approach: Selector-based (ưu tiên) + pixel fallback

Flow:
  1. Mở browser với profile đã lưu
  2. Đến trang Home Dreamina → phát hiện tự động khi đăng nhập xong
  3. Navigate trực tiếp sang chế độ "Tạo hình ảnh bằng AI"
  4. Với mỗi prompt: tìm ô nhập bằng selector → type → click nút Tạo
  5. Chờ render → tải ảnh

Setup mặc định: tự setup 1 lần trên giao diện rồi để yên
Chạy: python dreamina.py
"""

import asyncio
import os
import base64
import time
import json
import random
import hashlib
import platform
import subprocess
import re
import glob
import shutil
from typing import Optional
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs, quote
from playwright.async_api import async_playwright

# ── Import service tham chiếu ảnh (reference image UI) ──
from flow_reference_attach_service import (
    verify_reference_image_attached,
    recover_flow_editor_if_in_scene_page,
    attach_reference_from_library_by_name,
    preload_reference_library_images,
    upload_reference_image_for_video,
    _accept_first_time_upload_consent,
)
from flow_reference_service import (
    get_reference_image_path,
    get_reference_search_name,
    list_reference_image_paths,
    count_reference_thumbs_in_composer,
    clear_reference_attachments_in_composer,
)

# ───────────────────────────────────────────────
PROMPTS_FILE   = "prompts.txt"
PROMPTS_DIR    = "prompts"
PROMPT_POOL_FILE = os.path.join(PROMPTS_DIR, "prompt_pool_1000.txt")
PROMPT_POOL_STATE_FILE = os.path.join(PROMPTS_DIR, "prompt_pool_state.json")
DREAMINA_HOME  = "https://dreamina.capcut.com/ai-tool/home"
DREAMINA_IMAGE = (
    "https://dreamina.capcut.com/ai-tool/generate"
    "?enter_from=ai_feature&ai_feature_name=image"
)
GOOGLE_FLOW_HOME = "https://labs.google/fx/vi/tools/flow"

# Chọn nền tảng chạy:
# - dreamina: chạy luồng tự động cũ (gửi prompt + tải ảnh)
# - google_flow: chỉ bật chế độ bắt request/response để bạn thao tác tay
TARGET_PLATFORM = os.environ.get("TARGET_PLATFORM", "google_flow").strip().lower()
# Mode Google Flow:
# - 1: chỉ bắt network thủ công (không tự gửi prompt)
# - 0: tự gửi prompt + tải ảnh bằng request/response API
GOOGLE_FLOW_MANUAL_CAPTURE = os.environ.get("GOOGLE_FLOW_MANUAL_CAPTURE", "0").strip() in {"1", "true", "yes"}
# Nếu bật, mỗi lần chạy Google Flow sẽ random prompt từ prompts.txt.
GOOGLE_FLOW_RANDOM_PROMPTS = os.environ.get("GOOGLE_FLOW_RANDOM_PROMPTS", "1").strip() in {"1", "true", "yes"}
# Số prompt random mỗi lần chạy (mặc định 5 theo yêu cầu test nhanh).
GOOGLE_FLOW_RANDOM_PROMPTS_COUNT = int(os.environ.get("GOOGLE_FLOW_RANDOM_PROMPTS_COUNT", "5"))
# Mỗi lần chạy Google Flow sẽ ép tạo project mới để test tách biệt.
GOOGLE_FLOW_FORCE_NEW_PROJECT = os.environ.get("GOOGLE_FLOW_FORCE_NEW_PROJECT", "1").strip() in {"1", "true", "yes"}
# Tự động upscale ảnh đã tạo lên 2K bằng API (không bấm UI).
# Mặc định tắt theo yêu cầu: dùng ảnh thường để giảm thời gian chờ.
GOOGLE_FLOW_AUTO_UPSCALE_2K = os.environ.get("GOOGLE_FLOW_AUTO_UPSCALE_2K", "0").strip() in {"1", "true", "yes"}
# Chọn loại media khi chạy auto Google Flow:
# - image: tạo ảnh như hiện tại
# - video: tạo video (test request/response video)
GOOGLE_FLOW_MEDIA_MODE = os.environ.get("GOOGLE_FLOW_MEDIA_MODE", "image").strip().lower()
# Chờ tối đa bao nhiêu giây để thu đủ response upscale 2K sau khi đã queue xong.
GOOGLE_FLOW_UPSCALE_WAIT_TIMEOUT_SEC = int(os.environ.get("GOOGLE_FLOW_UPSCALE_WAIT_TIMEOUT_SEC", "180"))
# Dọn thư mục output trước khi chạy Google Flow hay không.
# Mặc định tắt để giữ ảnh các lần chạy cũ cho bạn dễ đối chiếu.
GOOGLE_FLOW_CLEAR_OUTPUT_BEFORE_RUN = os.environ.get("GOOGLE_FLOW_CLEAR_OUTPUT_BEFORE_RUN", "0").strip() in {"1", "true", "yes"}
# Chờ thêm sau prompt cuối để Flow kịp trả kết quả API (giây).
# Theo yêu cầu hiện tại: mặc định 40s rồi bắt đầu tải ảnh thường.
GOOGLE_FLOW_WAIT_AFTER_LAST_PROMPT_SEC = int(os.environ.get("GOOGLE_FLOW_WAIT_AFTER_LAST_PROMPT_SEC", "40"))
# Chờ sau prompt cuối cho luồng video (thường cần lâu hơn ảnh).
# Video render trên Flow cần 2-5 phút, mặc định 180s để đủ thời gian.
GOOGLE_FLOW_VIDEO_WAIT_AFTER_LAST_PROMPT_SEC = int(os.environ.get("GOOGLE_FLOW_VIDEO_WAIT_AFTER_LAST_PROMPT_SEC", "180"))
# ── Config tham chiếu ảnh cho video pipeline ──────────────────────────────────
# Bật/tắt tính năng tham chiếu ảnh reference trong step video.
# Khi bật: mỗi prompt sẽ tự upload/search và attach ảnh character/image vào Flow trước khi gửi.
GOOGLE_FLOW_VIDEO_USE_REFERENCE_IMAGES = os.environ.get(
    "GOOGLE_FLOW_VIDEO_USE_REFERENCE_IMAGES", "1"
).strip() in {"1", "true", "yes"}
# Mode attach reference:
# - "library_search": tìm theo tên trong thư viện Flow (ảnh đã preload trước)
# - "upload": upload file trực tiếp khi gửi mỗi prompt
GOOGLE_FLOW_VIDEO_REFERENCE_MODE = os.environ.get(
    "GOOGLE_FLOW_VIDEO_REFERENCE_MODE", "library_search"
).strip().lower()
# Preload tất cả ảnh reference vào thư viện Flow ngay đầu session (chỉ làm 1 lần đầu).
GOOGLE_FLOW_VIDEO_PRELOAD_REFERENCE_LIBRARY = os.environ.get(
    "GOOGLE_FLOW_VIDEO_PRELOAD_REFERENCE_LIBRARY", "1"
).strip() in {"1", "true", "yes"}
# Số giây chờ sau khi preload để Flow kịp index ảnh.
GOOGLE_FLOW_VIDEO_PRELOAD_WAIT_SEC = int(
    os.environ.get("GOOGLE_FLOW_VIDEO_PRELOAD_WAIT_SEC", "15")
)
# Thư mục chứa ảnh reference (character1.png, character2.png, image1.png...)
# Mặc định dùng output_images/ — nơi ảnh được tải sang sau step 1.
GOOGLE_FLOW_VIDEO_REFERENCE_DIR = os.environ.get(
    "GOOGLE_FLOW_VIDEO_REFERENCE_DIR",
    os.path.abspath("output_images"),
).strip()
# Cho phép fallback set_input_files trực tiếp nếu file chooser thất bại.
GOOGLE_FLOW_VIDEO_ALLOW_DIRECT_FILE_INPUT = os.environ.get(
    "GOOGLE_FLOW_VIDEO_ALLOW_DIRECT_FILE_INPUT", "1"
).strip() in {"1", "true", "yes"}
# Nếu =True: dừng cảnh khi attach reference thất bại, không gửi prompt thiếu reference.
# Nếu =False: vẫn gửi prompt dù reference không attach được.
GOOGLE_FLOW_VIDEO_REQUIRE_REFERENCE_UPLOAD = os.environ.get(
    "GOOGLE_FLOW_VIDEO_REQUIRE_REFERENCE_UPLOAD", "1"
).strip() in {"1", "true", "yes"}
# Số lần thử preload/upload toàn bộ ảnh reference ở đầu phiên video.
# Ví dụ =3 nghĩa là: lần đầu + retry thêm 2 lần.
GOOGLE_FLOW_VIDEO_PRELOAD_MAX_ATTEMPTS = int(
    os.environ.get("GOOGLE_FLOW_VIDEO_PRELOAD_MAX_ATTEMPTS", "3")
)
# Số ảnh reference tối đa có thể attach cho mỗi prompt video.
# Mặc định nâng lên 4 để hỗ trợ prompt có cả:
# - character1/2 + image1
# - character1/2 + image1/2
GOOGLE_FLOW_VIDEO_MAX_REFERENCES_PER_PROMPT = int(
    os.environ.get("GOOGLE_FLOW_VIDEO_MAX_REFERENCES_PER_PROMPT", "4")
)

# Số vòng retry tối đa khi tải video theo mỗi cảnh.
# Khi debug nhanh nhiều cảnh, có thể hạ xuống (ví dụ 6) để ra báo cáo sớm.
GOOGLE_FLOW_VIDEO_DOWNLOAD_MAX_ATTEMPTS = int(os.environ.get("GOOGLE_FLOW_VIDEO_DOWNLOAD_MAX_ATTEMPTS", "20"))
# Cho phép reload trang trong lúc poll/tải video hay không.
# Mục tiêu:
# - =True: dễ thấy status media mới hơn từ Flow, nhưng UI sẽ bị refresh.
# - =False: giữ UI ổn định, tránh reload ngay trước lúc tải video.
GOOGLE_FLOW_VIDEO_ALLOW_RELOAD_DURING_DOWNLOAD = os.environ.get(
    "GOOGLE_FLOW_VIDEO_ALLOW_RELOAD_DURING_DOWNLOAD", "1"
).strip().lower() in {"1", "true", "yes"}
# ── Human-like pacing (giảm dấu hiệu automation, vẫn giữ tốc độ gần hiện tại) ──
# Bật/tắt nhịp gửi "lúc nhanh lúc chậm" có kiểm soát.
# Mặc định bật, nhưng dao động rất nhẹ để không làm tổng thời gian lệch nhiều.
FLOW_HUMANIZE_ENABLED = os.environ.get("FLOW_HUMANIZE_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
# Seed riêng theo worker để mỗi Chrome có nhịp khác nhau ổn định giữa các lần chạy.
# Nếu không truyền seed thì dùng ngẫu nhiên hệ thống.
FLOW_HUMANIZE_SEED = os.environ.get("FLOW_HUMANIZE_SEED", "").strip()
# Biên dao động delay sau khi gửi prompt (theo tỉ lệ của base delay).
# Ví dụ base=1.0, min=0.85, max=1.35 -> delay dao động 0.85s..1.35s.
FLOW_SEND_JITTER_MIN = float(os.environ.get("FLOW_SEND_JITTER_MIN", "0.85"))
FLOW_SEND_JITTER_MAX = float(os.environ.get("FLOW_SEND_JITTER_MAX", "1.35"))
# Xác suất chèn pause ngắn sau prompt để tránh nhịp quá đều.
# Mặc định 12% để tác động nhẹ.
FLOW_SOFT_PAUSE_PROB = float(os.environ.get("FLOW_SOFT_PAUSE_PROB", "0.12"))
FLOW_SOFT_PAUSE_MIN_SEC = float(os.environ.get("FLOW_SOFT_PAUSE_MIN_SEC", "1.2"))
FLOW_SOFT_PAUSE_MAX_SEC = float(os.environ.get("FLOW_SOFT_PAUSE_MAX_SEC", "3.2"))
# Delay "suy nghĩ" trước khi bấm gửi prompt video (vì video đi từng cảnh).
FLOW_VIDEO_PRE_SEND_BASE_SEC = float(os.environ.get("FLOW_VIDEO_PRE_SEND_BASE_SEC", "0.8"))
# Poll interval trạng thái video (giây) — cho lệch nhẹ thay vì cố định 10s.
FLOW_VIDEO_POLL_BASE_SEC = float(os.environ.get("FLOW_VIDEO_POLL_BASE_SEC", "10.0"))
FLOW_VIDEO_POLL_JITTER_SEC = float(os.environ.get("FLOW_VIDEO_POLL_JITTER_SEC", "1.2"))
# ── Scheduler mới cho video (không reload định kỳ) ────────────────────────────
# Nhịp gửi cảnh mới mặc định: random 60-90s.
FLOW_VIDEO_SEND_INTERVAL_FAST_MIN_SEC = float(os.environ.get("FLOW_VIDEO_SEND_INTERVAL_FAST_MIN_SEC", "60"))
FLOW_VIDEO_SEND_INTERVAL_FAST_MAX_SEC = float(os.environ.get("FLOW_VIDEO_SEND_INTERVAL_FAST_MAX_SEC", "90"))
# Nếu 2-3 vòng liền không có cảnh READY, tăng nhịp gửi lên 90-150s.
FLOW_VIDEO_SEND_INTERVAL_SLOW_MIN_SEC = float(os.environ.get("FLOW_VIDEO_SEND_INTERVAL_SLOW_MIN_SEC", "90"))
FLOW_VIDEO_SEND_INTERVAL_SLOW_MAX_SEC = float(os.environ.get("FLOW_VIDEO_SEND_INTERVAL_SLOW_MAX_SEC", "150"))
FLOW_VIDEO_READY_STALL_ROUNDS_FOR_SLOW = int(os.environ.get("FLOW_VIDEO_READY_STALL_ROUNDS_FOR_SLOW", "2"))
# Khi phát hiện rate-limit trên UI -> cooldown 3 phút.
FLOW_VIDEO_UNUSUAL_COOLDOWN_SEC = int(os.environ.get("FLOW_VIDEO_UNUSUAL_COOLDOWN_SEC", "180"))
# Delay ngẫu nhiên trước khi tải video READY để tránh pattern bot.
FLOW_VIDEO_DOWNLOAD_DELAY_MIN_SEC = float(os.environ.get("FLOW_VIDEO_DOWNLOAD_DELAY_MIN_SEC", "3"))
FLOW_VIDEO_DOWNLOAD_DELAY_MAX_SEC = float(os.environ.get("FLOW_VIDEO_DOWNLOAD_DELAY_MAX_SEC", "8"))
# Số media tối đa mỗi cảnh được probe trong một vòng sweep (để tránh spam).
FLOW_VIDEO_PROBE_PER_SCENE_PER_ROUND = int(
    os.environ.get("FLOW_VIDEO_PROBE_PER_SCENE_PER_ROUND", "4")
)
# Với media chưa READY, giới hạn tần suất probe theo mediaId.
FLOW_VIDEO_PENDING_PROBE_MIN_INTERVAL_SEC = float(
    os.environ.get("FLOW_VIDEO_PENDING_PROBE_MIN_INTERVAL_SEC", "45")
)
# Chu kỳ poll projectInitialData để thu media mới ngay cả khi generate response thiếu map.
FLOW_VIDEO_PROJECT_POLL_INTERVAL_SEC = float(
    os.environ.get("FLOW_VIDEO_PROJECT_POLL_INTERVAL_SEC", "25")
)
# Ngưỡng kích thước tối thiểu để coi file video tải về là hợp lệ.
FLOW_VIDEO_MIN_VALID_BYTES = int(os.environ.get("FLOW_VIDEO_MIN_VALID_BYTES", "500000"))
# Số lần probe lỗi liên tiếp cho 1 media trước khi bỏ qua media đó trong phiên.
FLOW_VIDEO_MEDIA_MAX_CONSECUTIVE_FAILS = int(
    os.environ.get("FLOW_VIDEO_MEDIA_MAX_CONSECUTIVE_FAILS", "4")
)
# Mã trả về đặc biệt để báo cho runner biết:
# luồng video dừng sớm vì preload/upload reference thất bại.
FLOW_VIDEO_PRELOAD_FAILED_CODE = -2
# Poll map API scene->image trong mode Google Flow (giây)
GOOGLE_FLOW_API_MAP_TIMEOUT_SEC = int(os.environ.get("GOOGLE_FLOW_API_MAP_TIMEOUT_SEC", "120"))
# Nếu bật, script sẽ mở Flow và chờ bạn setup xong rồi Enter mới bắt đầu gửi prompt.
# Mặc định tắt để lần sau chạy tự động luôn.
GOOGLE_FLOW_WAIT_FOR_READY_ENTER = os.environ.get("GOOGLE_FLOW_WAIT_FOR_READY_ENTER", "0").strip() in {"1", "true", "yes"}
# Giữ Chrome mở sau khi chạy xong để user kiểm tra trực quan.
GOOGLE_FLOW_KEEP_BROWSER_OPEN = os.environ.get("GOOGLE_FLOW_KEEP_BROWSER_OPEN", "1").strip() in {"1", "true", "yes"}
PROFILE_DIR    = os.path.expanduser("~/dreamina_playwright_profile")
# Profile riêng cho bước tạo ảnh reference (step 1).
# Mặc định tách hẳn để không dính session với bước video.
PROFILE_DIR_IMAGE = os.environ.get(
    "PROFILE_DIR_IMAGE",
    os.path.expanduser("~/dreamina_playwright_profile_image"),
).strip()
# Profile riêng cho bước tạo video (step 2).
PROFILE_DIR_VIDEO = os.environ.get(
    "PROFILE_DIR_VIDEO",
    os.path.expanduser("~/dreamina_playwright_profile_video"),
).strip()
# Cho phép bật/tắt chạy 2 Chrome khác nhau giữa step ảnh và step video.
# Mặc định bật theo yêu cầu mới.
GOOGLE_FLOW_SEPARATE_CHROME_FOR_IMAGE_VIDEO = os.environ.get(
    "GOOGLE_FLOW_SEPARATE_CHROME_FOR_IMAGE_VIDEO",
    "1",
).strip().lower() in {"1", "true", "yes"}
# Có auto đóng session Chrome automation cũ trước khi chạy hay không.
# Nếu bạn đã mở sẵn và login thủ công, nên đặt =0 để tránh bị đá văng phiên.
GOOGLE_FLOW_KILL_OLD_SESSION_BEFORE_RUN = os.environ.get(
    "GOOGLE_FLOW_KILL_OLD_SESSION_BEFORE_RUN",
    "0",
).strip().lower() in {"1", "true", "yes"}
OUTPUT_DIR     = os.path.abspath("output_images")
PROGRESS_FILE  = os.path.abspath("progress_dreamina.json")
DELAY_SEC      = 1           # Mục tiêu gửi nhanh: ~1 giây mỗi prompt
RENDER_TIMEOUT    = 120  # Timeout chờ ảnh render (giây)
LOGIN_TIMEOUT     = 300  # Timeout chờ đăng nhập (giây)
IMAGES_PER_PROMPT = 4    # Số ảnh mỗi prompt (khớp với cài đặt trên Dreamina)
AUTO_TEST_PROMPTS_COUNT = 10  # Số prompt test tự sinh mặc định
PROMPT_BATCH_SIZE = 200  # Mỗi lần chạy lấy đúng 200 prompt từ pool
WAIT_AFTER_LAST_PROMPT_SEC = 60  # Sau prompt cuối, chờ cố định 60 giây rồi tải
STRICT_API_ONLY = True  # Chỉ tải theo map API để tránh nhầm ảnh cũ trong DOM
API_MAP_POLL_TIMEOUT_SEC = 0  # Không chờ thêm sau mốc 30s; thiếu cảnh thì bỏ qua
API_MAP_POLL_INTERVAL_SEC = 2  # Chu kỳ poll map API (dùng khi timeout > 0)
API_HISTORY_MAX_AGE_SEC = 600  # Chỉ nhận record API trong vòng 10 phút gần nhất

VP_WIDTH  = 1440
VP_HEIGHT = 900

# RNG riêng cho pacing để không ảnh hưởng logic random khác trong file.
_flow_human_rng = random.Random(FLOW_HUMANIZE_SEED or None)

# ── Debug system ──────────────────────────────────
# Thư mục lưu screenshot và log debug mỗi lần chạy
DEBUG_DIR = os.path.abspath("debug_sessions")
# Giữ tối đa bao nhiêu session debug cũ (tự xóa cái cũ nhất)
DEBUG_MAX_SESSIONS = 5
# ───────────────────────────────────────────────

# Selector tìm ô nhập prompt (theo thứ tự ưu tiên)
PROMPT_SELECTORS = [
    'div[contenteditable="true"][data-placeholder]',
    'div[contenteditable="true"].public-DraftEditor-content',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    'textarea[placeholder]',
    '[role="textbox"]',
]

# Selector tìm nút Tạo/Generate/Send
SEND_SELECTORS = [
    'button[data-testid="generate-btn"]',
    'button[aria-label*="Generate"]',
    'button[aria-label*="Tạo"]',
    'button[aria-label*="generate"]',
    'button[type="submit"]',
]

# Text nội dung nút gửi (fallback)
SEND_BUTTON_TEXTS = ["Tạo", "Generate", "生成", "送信"]

# Selector phát hiện đang render (loading)
LOADING_SELECTORS = [
    '[class*="loading"]',
    '[class*="generating"]',
    '[class*="spinner"]',
    '[class*="progress"]',
    '[aria-busy="true"]',
    'div[class*="queue"]',
]

# Selector phát hiện đã đăng nhập (xuất hiện avatar/menu user)
LOGIN_DETECT_SELECTORS = [
    '[class*="avatar"]',
    '[class*="user-info"]',
    '[aria-label*="Account"]',
    '[data-testid*="user"]',
    'img[class*="avatar"]',
    '[class*="userAvatar"]',
]
# ───────────────────────────────────────────────


# ═══════════════════════════════════════════════
# DEBUG SYSTEM — học từ flow_debug của 3d-documentary
# Mỗi bước quan trọng:
#   1. Chụp screenshot thumbnail
#   2. Lấy DOM info (buttons, inputs, images, spinner)
#   3. Ghi vào file log JSON (xem lại dễ dàng)
#   4. In terminal màu sắc theo loại (info/error/warn)
# ═══════════════════════════════════════════════

# Biến global giữ session debug hiện tại
_debug_session_dir: str = ""
_debug_steps: list = []   # list các step đã debug trong session này
_step_counter: int = 0    # đếm số bước (để đặt tên file theo thứ tự)
_network_events: list = []  # log request/response ảnh
_network_req_start: dict = {}  # map request_id -> start_ts
_prompt_submission_trace: list = []  # map prompt gửi đi -> trạng thái DOM ngay lúc đó
_download_hash_records: list = []  # hash các file ảnh đã tải
_trace_zip_path: str = ""  # đường dẫn playwright trace.zip của phiên hiện tại
_api_events: list = []  # log API fetch/xhr chi tiết
_api_req_meta: dict = {}  # request_id -> metadata từ request
_scene_to_task_ids: dict = {}  # scene_no -> [task_id...]
_task_to_image_urls: dict = {}  # task_id -> [image_url...]
_scene_to_image_urls: dict = {}  # scene_no -> [image_url...] (nếu response trả trực tiếp)
_submit_to_scene: dict = {}  # submit_id -> scene_no
_trusted_submit_ids: set = set()  # submit_id phát sinh trong chính lần chạy hiện tại
_run_started_ts: float = 0.0  # mốc thời gian bắt đầu gửi prompt của lần chạy hiện tại
_pending_api_tasks: list = []  # danh sách task async parse response API
_upscale_events: list = []  # log riêng cho flow upscale 2K
_scene_to_media_ids: dict = {}  # scene_no -> [media_id...]
_scene_to_video_media_ids: dict = {}  # scene_no -> [video_media_id...]
_scene_to_video_ready_media_ids: dict = {}  # scene_no -> [video_media_id đã READY]
_scene_to_video_failed_media_ids: dict = {}  # scene_no -> [video_media_id đã FAILED]
_video_media_status_by_id: dict = {}  # media_id -> status text gần nhất
_video_sent_scene_history: list = []  # lịch sử scene đã gửi để fallback map khi response thiếu scene
_video_scene_sent_ts: dict = {}  # scene_no -> timestamp gửi prompt video gần nhất
_video_media_last_probe_ts: dict = {}  # media_id -> timestamp probe tải gần nhất
_orphan_video_media_ts: dict = {}  # media_id -> first_seen_ts (chưa map scene)
_video_media_fail_count: dict = {}  # media_id -> số lần probe lỗi liên tiếp
_video_media_terminal_skip: set = set()  # media_id bị bỏ qua hẳn trong phiên
_video_download_events: list = []  # log chi tiết từng attempt tải video
_flow_ui_error_events: list = []  # log text lỗi UI (toast/card) trên Flow
_last_scene_reference_attach_failed = False  # cảnh gần nhất fail vì thiếu/attach reference
_last_flow_client_context: dict = {}  # clientContext gần nhất từ request generate
_upscale_success_by_media: dict = {}  # media_id -> {"encoded_image": "...", "status": int, ...}


def _init_debug_session():
    """
    Tạo thư mục session debug mới dựa theo timestamp.
    Tự động xóa sessions cũ nếu vượt quá DEBUG_MAX_SESSIONS.
    """
    global _debug_session_dir, _debug_steps, _step_counter
    global _network_events, _network_req_start, _prompt_submission_trace, _download_hash_records
    global _trace_zip_path
    global _api_events, _api_req_meta, _scene_to_task_ids, _task_to_image_urls, _scene_to_image_urls
    global _submit_to_scene, _trusted_submit_ids, _run_started_ts, _pending_api_tasks, _upscale_events
    global _scene_to_media_ids, _scene_to_video_media_ids, _scene_to_video_ready_media_ids
    global _scene_to_video_failed_media_ids, _video_media_status_by_id, _video_sent_scene_history
    global _video_scene_sent_ts
    global _video_media_last_probe_ts, _orphan_video_media_ts
    global _video_media_fail_count, _video_media_terminal_skip
    global _video_download_events, _flow_ui_error_events, _last_scene_reference_attach_failed
    global _last_flow_client_context, _upscale_success_by_media
    Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)

    # ── Auto-cleanup: xóa sessions cũ nhất, giữ max N ──
    existing = sorted(glob.glob(os.path.join(DEBUG_DIR, "session_*")), key=os.path.getmtime)
    while len(existing) >= DEBUG_MAX_SESSIONS:
        oldest = existing.pop(0)
        shutil.rmtree(oldest, ignore_errors=True)
        log(f"  Xóa debug session cũ: {os.path.basename(oldest)}", "🗑️")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _debug_session_dir = os.path.join(DEBUG_DIR, f"session_{ts}")
    Path(_debug_session_dir).mkdir(parents=True, exist_ok=True)
    _debug_steps = []
    _step_counter = 0
    _network_events = []
    _network_req_start = {}
    _prompt_submission_trace = []
    _download_hash_records = []
    _api_events = []
    _api_req_meta = {}
    _scene_to_task_ids = {}
    _task_to_image_urls = {}
    _scene_to_image_urls = {}
    _submit_to_scene = {}
    _trusted_submit_ids = set()
    _run_started_ts = 0.0
    _pending_api_tasks = []
    _upscale_events = []
    _scene_to_media_ids = {}
    _scene_to_video_media_ids = {}
    _scene_to_video_ready_media_ids = {}
    _scene_to_video_failed_media_ids = {}
    _video_media_status_by_id = {}
    _video_sent_scene_history = []
    _video_scene_sent_ts = {}
    _video_media_last_probe_ts = {}
    _orphan_video_media_ts = {}
    _video_media_fail_count = {}
    _video_media_terminal_skip = set()
    _video_download_events = []
    _flow_ui_error_events = []
    _last_scene_reference_attach_failed = False
    _last_flow_client_context = {}
    _upscale_success_by_media = {}
    _trace_zip_path = os.path.join(_debug_session_dir, "playwright_trace.zip")
    log(f"Debug session: {_debug_session_dir}", "🗂️")


async def debug_step(page, step_name: str, job_id: str = "", is_error: bool = False, extra: dict = None):
    """
    Chụp 1 debug step — học từ flow_debug của 3d-documentary:
      - Chụp screenshot thumbnail (JPEG, quality 60)
      - Lấy DOM info: buttons, inputs, số ảnh, có spinner không
      - Ghi vào log JSON → xem lại khi debug lỗi
      - In terminal màu theo loại (error=🔴, warn=🟡, info=📸)

    Gọi tại: trước/sau type_prompt, send_prompt, download, lỗi
    """
    global _step_counter
    _step_counter += 1
    ts = time.time()
    timestamp_str = datetime.now().strftime("%H:%M:%S")

    # ── 1. Chụp screenshot thumbnail ──
    screenshot_path = ""
    screenshot_b64  = None
    if _debug_session_dir:
        fname = f"{_step_counter:03d}_{step_name}{'_ERR' if is_error else ''}.jpg"
        screenshot_path = os.path.join(_debug_session_dir, fname)
        try:
            await page.screenshot(
                path=screenshot_path,
                type="jpeg",
                quality=60,
                full_page=False,   # chỉ viewport, nhỏ gọn
            )
            # Encode base64 thumbnail để lưu trong JSON
            with open(screenshot_path, "rb") as f:
                screenshot_b64 = base64.b64encode(f.read()).decode()
        except Exception as e:
            log(f"  [debug] Không chụp được screenshot: {e}", "⚠️")

    # ── 2. Lấy DOM info (giống FlowDomInfo trong 3d-documentary) ──
    dom_info = {}
    try:
        dom_info = await page.evaluate("""
            () => {
                // Lấy tất cả buttons visible
                const btns = [];
                document.querySelectorAll('button, [role="button"]').forEach(b => {
                    const r = b.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        btns.push({
                            text: (b.innerText || b.textContent || '').trim().slice(0, 50),
                            aria: b.getAttribute('aria-label') || '',
                            disabled: b.disabled || false,
                            x: Math.round(r.x), y: Math.round(r.y),
                            w: Math.round(r.width), h: Math.round(r.height),
                        });
                    }
                });

                // Ô nhập liệu
                const inputs = [];
                document.querySelectorAll('input, textarea, [contenteditable="true"]').forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0) {
                        inputs.push({
                            tag: el.tagName.toLowerCase(),
                            visible: r.width > 0,
                            text_len: (el.value || el.innerText || '').length,
                            preview: (el.value || el.innerText || '').slice(0, 80),
                        });
                    }
                });

                // Đếm ảnh lớn (ảnh AI generate ra)
                const imgs = Array.from(document.querySelectorAll('img'))
                    .filter(i => i.complete && i.naturalWidth > 200 && i.naturalHeight > 200);

                // Có spinner/loading không?
                const spinnerSel = [
                    '[class*="skeleton"]', '[class*="shimmer"]',
                    '[class*="spinner"]', '[class*="generating"]', '[class*="pending"]'
                ];
                const hasSpinner = spinnerSel.some(s =>
                    [...document.querySelectorAll(s)].some(el => el.offsetWidth > 0)
                );

                // ==== POPUP/MODAL DETECTION ====
                // Dreamina hay popup: "hết lượt", "đăng nhập lại", "upgrade", "error"
                const popups = [];
                const popupSels = [
                    '[role="dialog"]',
                    '[role="alertdialog"]',
                    '[class*="modal"]',
                    '[class*="Modal"]',
                    '[class*="popup"]',
                    '[class*="Popup"]',
                    '[class*="toast"]',
                    '[class*="Toast"]',
                    '[class*="dialog"]',
                    '[class*="Dialog"]',
                ];
                for (const sel of popupSels) {
                    document.querySelectorAll(sel).forEach(el => {
                        if (el.offsetWidth > 0 && el.offsetHeight > 0) {
                            const txt = (el.innerText || '').trim().slice(0, 200);
                            const cls = (el.className || '').toString().slice(0, 80);
                            if (txt) popups.push({ class: cls, text: txt });
                        }
                    });
                }

                return {
                    url: location.href.slice(0, 100),
                    title: document.title.slice(0, 60),
                    buttons_count: btns.length,
                    buttons: btns.slice(0, 10),
                    inputs_count: inputs.length,
                    inputs: inputs.slice(0, 5),
                    large_images_count: imgs.length,
                    has_spinner: hasSpinner,
                    // Popup/modal info
                    popups_count: popups.length,
                    popups: popups.slice(0, 5),
                };
            }
        """)
    except Exception as e:
        dom_info = {"error": str(e)}

    # ── 3. Build step record ──
    step_record = {
        "step_index":       _step_counter,
        "step":             step_name,
        "job_id":           job_id,
        "timestamp":        ts,
        "timestamp_str":    timestamp_str,
        "is_error":         is_error,
        "screenshot_file":  os.path.basename(screenshot_path) if screenshot_path else None,
        "dom_info":         dom_info,
        "extra":            extra or {},
    }
    _debug_steps.append(step_record)

    # ── 4. Ghi JSONL (append mỗi dòng — crash-safe, không mất log) ──
    if _debug_session_dir:
        log_path = os.path.join(_debug_session_dir, "debug_log.jsonl")
        try:
            # Không lưu base64 vào JSONL (quá nặng) — chỉ lưu metadata
            log_entry = {
                "step": step_name,
                "step_index": _step_counter,
                "ts": timestamp_str,
                "job_id": job_id,
                "screenshot": os.path.basename(screenshot_path) if screenshot_path else None,
                "dom": {
                    "url": dom_info.get("url", ""),
                    "buttons_count": dom_info.get("buttons_count", 0),
                    "inputs_count": dom_info.get("inputs_count", 0),
                    "large_images": dom_info.get("large_images_count", 0),
                    "has_spinner": dom_info.get("has_spinner", False),
                    "popups_count": dom_info.get("popups_count", 0),
                    "popups": dom_info.get("popups", []),
                },
                "extra": extra or {},
                "is_error": is_error,
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ── 5. In terminal dạng gọn để dễ theo dõi realtime ──
    imgs_count   = dom_info.get("large_images_count", "?")
    btns_count   = dom_info.get("buttons_count", "?")
    inputs_count = dom_info.get("inputs_count", "?")
    popups_count = dom_info.get("popups_count", 0)
    spinner_txt  = "yes" if dom_info.get("has_spinner") else "no"
    url_short    = dom_info.get("url", "")[:58]
    level        = "ERR" if is_error else "DBG"

    # Dòng chính: gom các chỉ số quan trọng vào 1 dòng
    log(
        f"#{_step_counter:03d} {step_name}"
        f"{' | job=' + job_id if job_id else ''}"
        f" | img={imgs_count} btn={btns_count} input={inputs_count}"
        f" spinner={spinner_txt} popup={popups_count}",
        level,
    )
    # Dòng phụ: URL và extra ngắn gọn
    if url_short:
        log(f"url: {url_short}", "DBG")
    if extra:
        extra_txt = " | ".join(f"{k}={str(v)[:60]}" for k, v in extra.items())
        log(f"extra: {extra_txt}", "DBG")
    if screenshot_path:
        log(f"screenshot: {os.path.basename(screenshot_path)}", "DBG")


def log(msg, emoji=""):
    """
    In log thống nhất để người dùng dễ quét mắt trong terminal.
    - emoji có thể truyền icon cũ (📤, ✅, ⏳...) hoặc level text (INFO, DBG, ERR...)
    - output luôn theo format: [hh:mm:ss] [LEVEL] message
    """
    now = datetime.now().strftime("%H:%M:%S")
    level_map = {
        "❌": "ERR",
        "⚠️": "WARN",
        "✅": "OK",
        "📤": "SEND",
        "📥": "DOWN",
        "⏳": "WAIT",
        "🎯": "RULE",
        "🚀": "RUN",
        "🌐": "WEB",
        "🗂️": "DBG",
        "📋": "INFO",
        "📁": "PATH",
        "🔄": "STEP",
        "🎉": "DONE",
        "⏭️": "SKIP",
        "🖱️": "NAV",
        "🗑️": "CLEAN",
        "INFO": "INFO",
        "DBG": "DBG",
        "ERR": "ERR",
        "WARN": "WARN",
        "OK": "OK",
        "SEND": "SEND",
        "WAIT": "WAIT",
        "RUN": "RUN",
        "DOWN": "DOWN",
        "STEP": "STEP",
        "NAV": "NAV",
        "PATH": "PATH",
        "RULE": "RULE",
        "DONE": "DONE",
        "SKIP": "SKIP",
        "WEB": "WEB",
    }
    level = level_map.get(emoji, "INFO")
    print(f"[{now}] [{level:<5}] {msg}")


def is_google_flow_mode() -> bool:
    """
    Kiểm tra có đang chạy mode Google Flow hay không.
    Dùng biến môi trường TARGET_PLATFORM=google_flow.
    """
    return TARGET_PLATFORM in {"google_flow", "flow", "labs_flow"}


def get_target_home_url() -> str:
    """
    Trả URL trang đích theo mode hiện tại.
    """
    if is_google_flow_mode():
        return GOOGLE_FLOW_HOME
    return DREAMINA_HOME


def load_prompts_from_file(path: str = PROMPTS_FILE) -> list[str]:
    """
    Đọc danh sách prompt từ file text.
    Mỗi dòng là 1 prompt, bỏ dòng trống.
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return []


def parse_structured_story_input(path: str = PROMPTS_FILE) -> dict:
    """
    Parse file prompt dạng "FULL VIDEO PROMPTS" + "characterX/imageX".

    Mục tiêu:
    - Đọc được prompt reference ảnh nhân vật/background (step 1).
    - Đọc được từng block Video N để dựng prompt video (step 2).
    - Mỗi video prompt sẽ tự chèn tham chiếu đúng label xuất hiện trong "Reference guide".

    Trả về:
    {
      "is_structured": bool,
      "references": {label: prompt_reference_gốc},
      "reference_generation_prompts": [ "CẢNH 901: ...", ... ],
      "reference_scene_to_label": {901: "character1", ...},
      "video_prompts": [ "CẢNH 001: ...", ... ],
      "video_count": int,
    }
    """
    if not os.path.exists(path):
        return {"is_structured": False}

    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return {"is_structured": False}

    # Chỉ coi là structured khi có marker rõ ràng của bộ prompt đầy đủ.
    if "FULL VIDEO PROMPTS" not in text or "CHARACTER REFERENCE IMAGE PROMPTS" not in text:
        return {"is_structured": False}

    # Parse reference labels toàn cục (ưu tiên lần xuất hiện đầu tiên để giữ bản mô tả đầy đủ).
    references: dict[str, str] = {}
    for m in re.finditer(r"^\s*(character\d+|image\d+)\s*=\s*(.+?)\s*$", text, flags=re.IGNORECASE | re.MULTILINE):
        label = m.group(1).strip().lower()
        value = m.group(2).strip()
        if label not in references and value:
            references[label] = value

    # Parse từng block Video N.
    video_prompts: list[str] = []
    block_pattern = re.compile(
        r"---\s*Video\s+(\d+)\s*\([^\n]*\)\s*---\s*(.*?)(?=\n---\s*Video\s+\d+\s*\(|\n={10,}\nFINAL QUALITY CHECK|\Z)",
        flags=re.IGNORECASE | re.DOTALL,
    )

    for block in block_pattern.finditer(text):
        video_no = int(block.group(1))
        body = (block.group(2) or "").strip()
        if not body:
            continue

        # Tách phần "Reference guide" để biết video này dùng label nào.
        used_labels: list[str] = []
        ref_guide_match = re.search(
            r"Reference guide:\s*(.*?)(?:\n\s*\n|^0s-\d+s:)",
            body,
            flags=re.IGNORECASE | re.DOTALL | re.MULTILINE,
        )
        if ref_guide_match:
            ref_guide_text = ref_guide_match.group(1)
            for lm in re.finditer(r"^\s*(character\d+|image\d+)\s*=", ref_guide_text, flags=re.IGNORECASE | re.MULTILINE):
                lb = lm.group(1).strip().lower()
                if lb not in used_labels:
                    used_labels.append(lb)

        # Chuẩn hoá nội dung block: bỏ phần "Reference guide" khỏi body để đỡ trùng.
        body_without_ref = re.sub(
            r"Reference guide:\s*.*?(?:\n\s*\n|^0s-\d+s:)",
            "\n",
            body,
            flags=re.IGNORECASE | re.DOTALL | re.MULTILINE,
        ).strip()

        # Nếu không parse được label ở block, fallback dùng mọi label global.
        if not used_labels:
            used_labels = sorted(references.keys())

        # Dựng prompt video có phần tham chiếu rõ ràng theo label.
        lines = [f"CẢNH {video_no:03d}: VIDEO UNIT {video_no}"]
        lines.append("Reference labels phải giữ cố định:")
        for lb in used_labels:
            ref_text = references.get(lb, "")
            if ref_text:
                lines.append(f"- {lb}: {ref_text}")
            else:
                lines.append(f"- {lb}: (không tìm thấy mô tả reference)")
        lines.append("")
        lines.append("Nội dung shot/video cần tạo:")
        lines.append(body_without_ref)
        lines.append("")
        lines.append("Ràng buộc: giữ continuity nhân vật/background, ánh sáng sáng sớm, cinematic realism.")
        video_prompts.append("\n".join(lines).strip())

    # ── Fallback parser cho format mới: "Video 1: Reference guide: ..." ──
    # Mục tiêu:
    # - Không phá format cũ đã chạy ổn định.
    # - Nếu format cũ không bắt được block nào, tự động parse theo format mới.
    if not video_prompts:
        full_marker = "FULL VIDEO PROMPTS"
        full_idx = text.find(full_marker)
        video_zone = text[full_idx + len(full_marker):] if full_idx >= 0 else text

        # Bắt đầu của từng block video theo kiểu:
        #   Video 1:
        #   Video 12:
        # Cho phép có/không có khoảng trắng đầu dòng.
        video_headers = list(re.finditer(r"(?im)^\s*video\s+(\d+)\s*:\s*", video_zone))
        for i, h in enumerate(video_headers):
            video_no = int(h.group(1))
            body_start = h.end()
            body_end = video_headers[i + 1].start() if i + 1 < len(video_headers) else len(video_zone)
            body = (video_zone[body_start:body_end] or "").strip()
            if not body:
                continue

            # Parse label reference dùng trong block:
            # - CHARACTER1 = ...
            # - IMAGE2 = ...
            used_labels: list[str] = []
            for lm in re.finditer(r"\b(character\d+|image\d+)\b\s*=", body, flags=re.IGNORECASE):
                lb = lm.group(1).strip().lower()
                if lb not in used_labels:
                    used_labels.append(lb)

            # Cố gắng bỏ phần "Reference guide: ..." để prompt ngắn gọn hơn.
            body_without_ref = re.sub(
                r"^\s*reference\s+guide\s*:\s*",
                "",
                body,
                flags=re.IGNORECASE,
            ).strip()

            # Nếu có mốc shot đầu tiên (0s-3s:), ưu tiên lấy từ đó trở đi
            # để tránh lặp phần định nghĩa reference ở đầu.
            shot_anchor = re.search(r"\b0s\s*-\s*\d+s\s*:", body_without_ref, flags=re.IGNORECASE)
            if shot_anchor:
                body_without_ref = body_without_ref[shot_anchor.start():].strip()

            # Nếu không thấy label trong block, fallback dùng toàn bộ reference global.
            if not used_labels:
                used_labels = sorted(references.keys())

            lines = [f"CẢNH {video_no:03d}: VIDEO UNIT {video_no}"]
            lines.append("Reference labels phải giữ cố định:")
            for lb in used_labels:
                ref_text = references.get(lb, "")
                if ref_text:
                    lines.append(f"- {lb}: {ref_text}")
                else:
                    lines.append(f"- {lb}: (không tìm thấy mô tả reference)")
            lines.append("")
            lines.append("Nội dung shot/video cần tạo:")
            lines.append(body_without_ref)
            lines.append("")
            lines.append("Ràng buộc: giữ continuity nhân vật/background, ánh sáng sáng sớm, cinematic realism.")
            video_prompts.append("\n".join(lines).strip())

    # Dựng prompt step 1 để tạo ảnh reference.
    # Không dùng prefix "CẢNH 9xx" và cũng không lộ nhãn character/image
    # để giảm bias sinh ảnh sai ngữ cảnh.
    # Dùng ID trung tính: ref_01, ref_02, ...
    reference_generation_prompts: list[str] = []
    reference_scene_to_label: dict[int, str] = {}
    ref_index = 0
    # Ưu tiên character trước rồi image để output dễ đọc.
    ordered_labels = sorted(references.keys(), key=lambda x: (0 if x.startswith("character") else 1, x))
    for label in ordered_labels:
        ref_index += 1
        # Dùng index tuần tự làm scene id nội bộ của bước reference.
        # Vì prompt không còn "CẢNH xxx", hàm extract_scene_number sẽ fallback theo index này.
        scene_no = ref_index
        reference_scene_to_label[scene_no] = label
        ref_id = f"ref_{ref_index:02d}"
        reference_generation_prompts.append(f"{ref_id}: {references[label]}")

    return {
        "is_structured": True,
        "references": references,
        "reference_generation_prompts": reference_generation_prompts,
        "reference_scene_to_label": reference_scene_to_label,
        "video_prompts": video_prompts,
        "video_count": len(video_prompts),
    }


def pick_random_prompts(prompts: list[str], count: int) -> list[str]:
    """
    Chọn ngẫu nhiên `count` prompt không trùng nhau.
    Nếu danh sách đầu vào ít hơn `count` thì trả về toàn bộ.
    """
    if not prompts:
        return []
    n = max(1, int(count))
    if len(prompts) <= n:
        return prompts[:]
    return random.sample(prompts, n)


def save_selected_prompts_for_session(prompts: list[str], filename: str = "selected_prompts_google_flow.txt") -> str:
    """
    Lưu danh sách prompt đã chọn trong session hiện tại để debug đối chiếu.
    """
    if not _debug_session_dir:
        return ""
    path = os.path.join(_debug_session_dir, filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            for i, p in enumerate(prompts, start=1):
                f.write(f"{i:03d}. {p}\n")
        return path
    except Exception:
        return ""


def save_prompts_to_prompts_folder(prompts: list[str], filename: str) -> str:
    """
    Ghi danh sách prompt vào folder `prompts/` để dễ kiểm tra lại.
    """
    Path(PROMPTS_DIR).mkdir(parents=True, exist_ok=True)
    out = os.path.join(PROMPTS_DIR, filename)
    try:
        with open(out, "w", encoding="utf-8") as f:
            for i, p in enumerate(prompts, start=1):
                f.write(f"### PROMPT {i:03d}\n{p}\n\n")
        return os.path.abspath(out)
    except Exception:
        return ""


def rename_reference_scene_images(scene_to_label: dict[int, str]) -> dict:
    """
    Đổi tên file output step 1 từ `canh_9xx.png` sang `characterX.png`/`imageX.png`.
    Trả về map kết quả để in log dễ đọc.
    """
    result = {
        "renamed": [],
        "missing": [],
    }
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    for scene_no, label in (scene_to_label or {}).items():
        src = os.path.join(OUTPUT_DIR, f"canh_{scene_no:03d}.png")
        dst = os.path.join(OUTPUT_DIR, f"{label}.png")
        if not os.path.exists(src):
            result["missing"].append({"scene_no": scene_no, "label": label})
            continue
        try:
            # Nếu file label đã tồn tại thì ghi đè bằng bản mới nhất của lần chạy này.
            if os.path.exists(dst):
                os.remove(dst)
            os.replace(src, dst)
            result["renamed"].append({"scene_no": scene_no, "label": label, "file": dst})
        except Exception as e:
            result["missing"].append({"scene_no": scene_no, "label": label, "error": str(e)})
    return result


def _looks_like_image_url(url: str) -> bool:
    """Heuristic nhẹ để nhận diện URL ảnh."""
    low = (url or "").lower()
    if any(ext in low for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp"]):
        return True
    if any(k in low for k in ["image", "img", "render", "snapshot", "cdn"]):
        return True
    return False


def _looks_like_api_url(url: str) -> bool:
    """Heuristic nhận diện endpoint API có thể chứa task/result."""
    low = (url or "").lower()
    keys = [
        "/api/", "generate", "task", "result", "record", "history", "aigc",
        "submit", "create", "query", "flowmedia", "upsample", "upscale",
    ]
    return any(k in low for k in keys)


def _is_upscale_api_url(url: str) -> bool:
    """Nhận diện endpoint upscale/upsample (2K) của Flow."""
    low = (url or "").lower()
    return any(k in low for k in [
        "/upsampleimage",
        "upsampleimage",
        "upscale",
        "upsample",
    ])


def _is_flow_video_generate_api_url(url: str) -> bool:
    """
    Nhận diện endpoint generate video của Flow (nhiều biến thể endpoint).
    """
    low = (url or "").lower()
    return any(k in low for k in [
        "video:batchasyncgeneratevideotext",
        "video:batchasyncgeneratevideoreferenceimages",
        "video:batchasyncgeneratevideo",
        "/v1/video:",
    ])


def _looks_like_flow_video_api_url(url: str) -> bool:
    """
    Nhận diện endpoint API có khả năng chứa mapping video (scene/media/status).
    Dùng cho debug network mapping: in log ngắn, dễ đọc.
    """
    low = (url or "").lower()
    keys = [
        "flow.projectinitialdata",
        "video:",
        "batchasyncgeneratevideo",
        "batchgeneratevideo",
        "media.getmediaurlredirect",
        "project.searchuserprojects",
        "projectcontents",
    ]
    return any(k in low for k in keys)


def _collect_video_media_items_from_obj(obj, out: list, max_depth: int = 6):
    """
    Duyệt JSON để gom các object có thể là media video.
    Dùng khi response đổi format và không còn nằm ở key `media`.
    """
    if max_depth < 0:
        return
    if isinstance(obj, dict):
        name_val = obj.get("name")
        name_ok = isinstance(name_val, str) and bool(name_val.strip())
        name_like_media_id = bool(re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", str(name_val or ""), flags=re.IGNORECASE))
        has_video_hint = (
            isinstance(obj.get("video"), dict)
            or isinstance(obj.get("mediaMetadata"), dict)
            or isinstance(obj.get("mediaStatus"), dict)
            or isinstance(obj.get("generatedVideo"), dict)
        )
        if name_ok and (name_like_media_id or has_video_hint):
            out.append(obj)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _collect_video_media_items_from_obj(v, out, max_depth=max_depth - 1)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)):
                _collect_video_media_items_from_obj(v, out, max_depth=max_depth - 1)


def _extract_media_id_from_upscale_post_data(post_data: str) -> str:
    """
    Tách mediaId từ payload upsampleImage.
    Dùng để map chính xác response 2K về đúng ảnh nào.
    """
    if not post_data:
        return ""
    try:
        body = json.loads(post_data)
        media_id = str((body or {}).get("mediaId", "") or "")
        return media_id
    except Exception:
        return ""


def _extract_scene_numbers_from_text(text: str) -> list[int]:
    """
    Tách tất cả số cảnh trong text/body.
    Hỗ trợ 'CẢNH 041', 'canh 41',...
    """
    if not text:
        return []
    out = []
    for m in re.finditer(r"(?:cảnh|canh)\s*0*(\d+)", text, flags=re.IGNORECASE):
        try:
            out.append(int(m.group(1)))
        except Exception:
            pass
    # unique giữ thứ tự
    seen = set()
    uniq = []
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    return uniq


def _extract_scene_number_from_any_text(text: str, fallback: int = 0) -> int:
    """
    Tách scene number từ text theo nhiều kiểu:
    - CẢNH 012
    - CANH 12
    - SCENE 087
    """
    if not text:
        return fallback
    m = re.search(r"(?:cảnh|canh|scene)\s*0*(\d+)", text, flags=re.IGNORECASE)
    if not m:
        return fallback
    try:
        return int(m.group(1))
    except Exception:
        return fallback


def _collect_urls_from_obj(obj, out: list):
    """Duyệt đệ quy để gom URL ảnh từ JSON response."""
    if isinstance(obj, dict):
        for _, v in obj.items():
            _collect_urls_from_obj(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_urls_from_obj(v, out)
    elif isinstance(obj, str):
        if obj.startswith("http") and _looks_like_image_url(obj):
            out.append(obj)


def _collect_video_urls_from_obj(obj, out: list):
    """Duyệt JSON để gom URL video (mp4/webm/m3u8)."""
    if isinstance(obj, dict):
        for _, v in obj.items():
            _collect_video_urls_from_obj(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_video_urls_from_obj(v, out)
    elif isinstance(obj, str):
        low = obj.lower()
        if not low.startswith("http"):
            return
        if any(x in low for x in [".mp4", ".webm", ".m3u8", "/video/"]):
            out.append(obj)


def _collect_error_messages_from_obj(obj, out: list, max_depth: int = 6):
    """
    Duyệt JSON để gom thông điệp lỗi từ nhiều key thường gặp.
    Dùng cho debug khi API trả 4xx/5xx hoặc blocked.
    """
    if max_depth < 0:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if any(x in lk for x in ["error", "message", "reason", "detail", "statusmessage"]):
                if isinstance(v, str):
                    t = re.sub(r"\s+", " ", v).strip()
                    if t:
                        out.append(t)
            if isinstance(v, (dict, list)):
                _collect_error_messages_from_obj(v, out, max_depth=max_depth - 1)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)):
                _collect_error_messages_from_obj(v, out, max_depth=max_depth - 1)


def _looks_like_flow_media_url(url: str) -> bool:
    """
    Nhận diện URL liên quan phát media video/audio trong Flow.
    Dùng để bắt lỗi 'không tải được nội dung nghe nhìn'.
    """
    low = str(url or "").lower()
    if not low.startswith("http"):
        return False
    return (
        "/video/" in low
        or "/audio/" in low
        or "media.getmediaurlredirect" in low
        or low.endswith(".mp4")
        or low.endswith(".webm")
        or low.endswith(".m3u8")
        or ".mp4?" in low
        or ".webm?" in low
        or ".m3u8?" in low
    )


def _collect_task_ids_from_obj(obj, out: list):
    """Duyệt JSON để gom task/job id."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if any(x in lk for x in ["task_id", "taskid", "job_id", "jobid", "record_id", "recordid"]):
                if isinstance(v, (str, int)):
                    out.append(str(v))
            _collect_task_ids_from_obj(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_task_ids_from_obj(v, out)


def _append_unique_dict_list(dst: dict, key, values: list):
    """Append list value vào dict[key] nhưng không trùng."""
    cur = dst.setdefault(key, [])
    for v in values:
        if v not in cur:
            cur.append(v)


def _normalize_media_status_text(status) -> str:
    """Chuẩn hoá status media về chuỗi IN HOA để so sánh ổn định."""
    if status is None:
        return ""
    s = str(status).strip()
    return s.upper()


def _extract_video_media_status(item: dict, default_status: str = "") -> str:
    """
    Tách trạng thái render video từ nhiều vị trí có thể có trong response.
    Vì API thay đổi theo endpoint, cần fallback theo nhiều key.
    """
    if not isinstance(item, dict):
        return _normalize_media_status_text(default_status)

    # Ưu tiên status chi tiết trong mediaMetadata trước.
    media_meta = item.get("mediaMetadata", {}) or {}
    media_status_obj = media_meta.get("mediaStatus", {}) or {}
    candidates = [
        media_status_obj.get("mediaGenerationStatus"),
        media_status_obj.get("status"),
        item.get("status"),
        item.get("mediaStatus"),
        default_status,
    ]
    for c in candidates:
        n = _normalize_media_status_text(c)
        if n:
            return n
    return ""


def _is_video_media_ready_status(status: str) -> bool:
    """
    Đánh dấu media đã sẵn sàng tải khi status thuộc nhóm hoàn tất.
    """
    s = _normalize_media_status_text(status)
    if not s:
        return False
    return any(k in s for k in ["SUCCEEDED", "SUCCESSFUL", "COMPLETE", "COMPLETED", "READY", "AVAILABLE", "DONE"])


def _is_video_media_failed_status(status: str) -> bool:
    """
    Đánh dấu media thất bại rõ ràng để tránh retry vô ích.
    """
    s = _normalize_media_status_text(status)
    if not s:
        return False
    return any(k in s for k in ["FAILED", "ERROR", "CANCELLED", "REJECTED", "BLOCKED"])


def _register_scene_video_media(scene_no: int, media_id: str, status: str = ""):
    """
    Ghi map scene -> media video + trạng thái media.
    Mục tiêu:
    - giữ đủ tất cả mediaId đã thấy,
    - tách riêng media READY và FAILED để chọn đúng khi tải.
    """
    if not scene_no or not media_id:
        return
    _append_unique_dict_list(_scene_to_video_media_ids, scene_no, [media_id])

    normalized = _normalize_media_status_text(status)
    if normalized:
        _video_media_status_by_id[media_id] = normalized
        if _is_video_media_ready_status(normalized):
            _append_unique_dict_list(_scene_to_video_ready_media_ids, scene_no, [media_id])
        if _is_video_media_failed_status(normalized):
            _append_unique_dict_list(_scene_to_video_failed_media_ids, scene_no, [media_id])


def _guess_scene_for_unmapped_video_media() -> int:
    """
    Fallback map scene khi response không trả prompt/scene.
    Chiến thuật: ưu tiên scene gửi gần đây nhất còn ít/no media mapped.
    """
    if not _video_sent_scene_history:
        return 0
    for sc in reversed(_video_sent_scene_history):
        ids = _scene_to_video_media_ids.get(sc, []) or []
        if len(ids) == 0:
            return sc
    # Nếu tất cả scene đã có media, vẫn chọn scene gần nhất để tránh bỏ rơi media mới.
    return int(_video_sent_scene_history[-1] or 0)


def _extract_media_id_from_flow_redirect_url(url: str) -> str:
    """
    Tách media_id từ URL `media.getMediaUrlRedirect?name=<media_id>`.
    """
    try:
        parsed = urlparse(str(url or ""))
        qs = parse_qs(parsed.query or "")
        mid = str((qs.get("name", [""])[0] or "")).strip()
        return mid
    except Exception:
        return ""


def _is_flow_thumbnail_redirect_url(url: str) -> bool:
    """
    Kiểm tra redirect có phải loại thumbnail hay không.
    """
    try:
        parsed = urlparse(str(url or ""))
        qs = parse_qs(parsed.query or "")
        t = str((qs.get("mediaUrlType", [""])[0] or "")).upper()
        return "THUMBNAIL" in t
    except Exception:
        return False


def _has_scene_already_mapped_media(scene_no: int, media_id: str) -> bool:
    """Kiểm tra media_id đã nằm trong scene hay chưa."""
    ids = _scene_to_video_media_ids.get(scene_no, []) or []
    return media_id in ids


def _has_media_mapped_any_scene(media_id: str) -> bool:
    """Kiểm tra media_id đã thuộc scene nào chưa."""
    if not media_id:
        return False
    for ids in (_scene_to_video_media_ids or {}).values():
        if media_id in (ids or []):
            return True
    return False


def _register_orphan_video_media(media_id: str, status: str = "", ts: float = 0.0):
    """
    Đăng ký media chưa map scene vào orphan queue để probe/tải sau.
    """
    if not media_id:
        return
    if _has_media_mapped_any_scene(media_id):
        _orphan_video_media_ts.pop(media_id, None)
        return
    if status:
        _video_media_status_by_id[media_id] = _normalize_media_status_text(status)
    if media_id not in _orphan_video_media_ts:
        _orphan_video_media_ts[media_id] = float(ts or time.time())


def _mark_video_media_probe_fail(media_id: str, reason: str = "") -> int:
    """
    Tăng bộ đếm lỗi probe cho media_id. Trả về fail_count mới.
    Nếu vượt ngưỡng thì đánh dấu terminal skip.
    """
    if not media_id:
        return 0
    n = int(_video_media_fail_count.get(media_id, 0) or 0) + 1
    _video_media_fail_count[media_id] = n
    max_fails = max(1, FLOW_VIDEO_MEDIA_MAX_CONSECUTIVE_FAILS)
    if n >= max_fails:
        _video_media_terminal_skip.add(media_id)
        log(
            f"[FLOW-DL] media {media_id[:8]}... bị bỏ qua sau {n} lần lỗi liên tiếp ({reason}).",
            "WARN",
        )
    return n


def _mark_video_media_probe_success(media_id: str):
    """Reset bộ đếm lỗi khi media tải thành công."""
    if not media_id:
        return
    _video_media_fail_count.pop(media_id, None)
    _video_media_terminal_skip.discard(media_id)


def _guess_scene_for_redirect_discovered_media(ts: float) -> int:
    """
    Chọn scene nhận media_id mới bắt từ redirect request.
    Quy tắc:
    - ưu tiên scene gửi gần nhất (không ở tương lai so với ts),
    - ưu tiên scene còn ít media mapped (<4),
    - fallback scene mới nhất trong lịch sử gửi.
    """
    if not _video_sent_scene_history:
        return 0

    for sc in reversed(_video_sent_scene_history):
        sent_ts = float(_video_scene_sent_ts.get(sc, 0.0) or 0.0)
        if sent_ts > 0 and ts + 1.0 < sent_ts:
            continue
        # Media mới thường xuất hiện gần thời điểm gửi, giới hạn cửa sổ để giảm map nhầm dữ liệu cũ.
        if sent_ts > 0 and (ts - sent_ts) > 8 * 60:
            continue
        ids = _scene_to_video_media_ids.get(sc, []) or []
        if len(ids) < 4:
            return sc
    return int(_video_sent_scene_history[-1] or 0)


def _register_video_media_from_redirect_request(url: str, ts: float):
    """
    Bắt media_id từ request redirect của Flow và map về scene đã gửi.
    Mục tiêu: không phụ thuộc UI state/READY để có media_id tải.
    """
    low = str(url or "").lower()
    if "media.getmediaurlredirect" not in low:
        return
    media_id = _extract_media_id_from_flow_redirect_url(url)
    if not media_id:
        return
    # Tránh gom thumbnail/media cũ trước khi bắt đầu gửi cảnh ở phiên hiện tại.
    if not _video_sent_scene_history:
        return

    # Nếu đã map scene thì bỏ qua.
    if _has_media_mapped_any_scene(media_id):
        return

    # Redirect thumbnail/media có thể đến lệch thời điểm scene.
    # Ưu tiên đưa vào orphan queue, sau đó scheduler sẽ gán về cảnh pending.
    _register_orphan_video_media(media_id, status="", ts=ts)
    source = "thumbnail" if _is_flow_thumbnail_redirect_url(url) else "media"
    log(f"[FLOW-MAP] redirect->{source} discovered orphan media {media_id[:8]}...", "DBG")


def _parse_epoch_like(value) -> float:
    """
    Chuẩn hoá giá trị thời gian (sec/ms/chuỗi ISO) về unix timestamp (giây).
    Trả về 0.0 nếu không parse được.
    """
    if value is None:
        return 0.0
    # numeric trực tiếp
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return 0.0
        # Nếu là millisecond thì quy đổi về giây.
        if v > 1e12:
            v = v / 1000.0
        return v
    # chuỗi: có thể là số hoặc ISO datetime
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0.0
        if re.fullmatch(r"\d+(\.\d+)?", s):
            try:
                v = float(s)
                if v > 1e12:
                    v = v / 1000.0
                return v if v > 0 else 0.0
            except Exception:
                return 0.0
        try:
            iso = s.replace("Z", "+00:00")
            return datetime.fromisoformat(iso).timestamp()
        except Exception:
            return 0.0
    return 0.0


def _extract_recent_ts_from_obj(obj, max_depth: int = 3) -> float:
    """
    Tìm timestamp mới nhất từ object JSON, ưu tiên key chứa 'time/date/created/updated'.
    Dùng để loại record history cũ từ các lần chạy trước.
    """
    if max_depth < 0:
        return 0.0

    best = 0.0
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if any(tk in lk for tk in ["time", "date", "created", "updated", "modified", "timestamp"]):
                best = max(best, _parse_epoch_like(v))
            if isinstance(v, (dict, list)):
                best = max(best, _extract_recent_ts_from_obj(v, max_depth - 1))
    elif isinstance(obj, list):
        for v in obj[:30]:
            if isinstance(v, (dict, list)):
                best = max(best, _extract_recent_ts_from_obj(v, max_depth - 1))
    return best


def _extract_scene_cover_urls_from_history_json(body_json) -> dict:
    """
    Parse response get_history_by_ids:
      data -> <submit_id> -> item_list[] -> common_attr.description + common_attr.cover_url
    Trả về map scene_no -> [cover_url...]
    """
    out = {}
    if not isinstance(body_json, dict):
        return out
    data = body_json.get("data", {})
    if not isinstance(data, dict):
        return out

    for _, rec in data.items():
        if not isinstance(rec, dict):
            continue
        items = rec.get("item_list", [])
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            common = it.get("common_attr", {}) or {}
            desc = str(common.get("description", "") or "")
            scene_nums = _extract_scene_numbers_from_text(desc)
            cover = str(common.get("cover_url", "") or "")
            if not cover:
                # fallback lấy ảnh trong cover_url_map
                cmap = common.get("cover_url_map", {}) or {}
                if isinstance(cmap, dict):
                    for _, v in cmap.items():
                        if isinstance(v, str) and v.startswith("http"):
                            cover = v
                            break
            if not cover:
                continue
            for sc in scene_nums:
                _append_unique_dict_list(out, sc, [cover])
    return out


def _extract_scene_cover_and_submit_map_from_history_json(
    body_json,
    trusted_submit_ids: Optional[set] = None,
    run_started_ts: float = 0.0,
    max_age_sec: int = API_HISTORY_MAX_AGE_SEC,
) -> tuple[dict, dict]:
    """
    Parse get_history_by_ids và trả về:
    - scene_map: scene_no -> [cover_url...]
    - submit_map: submit_id(key trong data) -> scene_no
    """
    scene_map = {}
    submit_map = {}
    if not isinstance(body_json, dict):
        return scene_map, submit_map
    data = body_json.get("data", {})
    if not isinstance(data, dict):
        return scene_map, submit_map

    now_ts = time.time()
    min_allowed_ts = max(0.0, (run_started_ts or 0.0) - max(0, max_age_sec))
    trusted_set = trusted_submit_ids or set()

    for submit_id, rec in data.items():
        if not isinstance(rec, dict):
            continue
        submit_id_str = str(submit_id)
        rec_ts = _extract_recent_ts_from_obj(rec)
        submit_is_trusted = submit_id_str in trusted_set

        # Lọc record cũ:
        # - Nếu submit_id thuộc run hiện tại: luôn cho qua.
        # - Nếu không thuộc run hiện tại: chỉ cho qua khi có timestamp đủ mới.
        if not submit_is_trusted:
            if rec_ts <= 0:
                continue
            if rec_ts < min_allowed_ts:
                continue
            if rec_ts > now_ts + 3600:
                # dữ liệu thời gian lỗi/quá tương lai -> bỏ để tránh map nhầm
                continue

        items = rec.get("item_list", [])
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            common = it.get("common_attr", {}) or {}
            item_ts = max(
                _extract_recent_ts_from_obj(common),
                _extract_recent_ts_from_obj(it),
                rec_ts,
            )
            if not submit_is_trusted:
                if item_ts <= 0 or item_ts < min_allowed_ts or item_ts > now_ts + 3600:
                    continue
            desc = str(common.get("description", "") or "")
            scene_nums = _extract_scene_numbers_from_text(desc)
            if scene_nums:
                # ưu tiên cảnh đầu tiên trong mô tả
                submit_map[submit_id_str] = scene_nums[0]
            cover = str(common.get("cover_url", "") or "")
            if not cover:
                cmap = common.get("cover_url_map", {}) or {}
                if isinstance(cmap, dict):
                    for _, v in cmap.items():
                        if isinstance(v, str) and v.startswith("http"):
                            cover = v
                            break
            if not cover:
                continue
            for sc in scene_nums:
                _append_unique_dict_list(scene_map, sc, [cover])
    return scene_map, submit_map


async def wait_pending_api_tasks(timeout_sec: float = 4.0):
    """
    Chờ các task parse API response hoàn tất trước khi map tải ảnh.
    Tránh trường hợp map chưa kịp cập nhật.
    """
    if not _pending_api_tasks:
        return
    alive = [t for t in _pending_api_tasks if t and not t.done()]
    if not alive:
        return
    try:
        await asyncio.wait(alive, timeout=timeout_sec)
    except Exception:
        pass


def setup_image_network_debug(page):
    """
    Bắt request/response/failure liên quan ảnh để biết:
    - tải bằng URL nào
    - status trả về ra sao
    - mất bao lâu
    """
    async def handle_api_response(response):
        """Xử lý response fetch/xhr để trích task_id + image urls."""
        request = response.request
        req_id = id(request)
        meta = _api_req_meta.get(req_id, {})
        url = request.url
        ts = time.time()

        if request.resource_type not in {"fetch", "xhr"}:
            return
        if not (_looks_like_api_url(url) or _is_upscale_api_url(url) or meta.get("scene_numbers")):
            return

        content_type = ""
        try:
            content_type = response.headers.get("content-type", "")
        except Exception:
            pass

        body_text = ""
        body_sample = ""
        body_json = None
        try:
            # Chỉ đọc body cho API text/json để tránh nặng.
            if "json" in (content_type or "").lower() or "text" in (content_type or "").lower():
                body_text = await response.text()
                body_sample = body_text[:30000] + ("...<truncated>" if len(body_text) > 30000 else "")
                try:
                    body_json = json.loads(body_text)
                except Exception:
                    body_json = None
        except Exception:
            body_text = ""
            body_sample = ""
            body_json = None

        task_ids = []
        image_urls = []
        video_urls = []
        video_media_updates = []
        backend_error_messages = []
        parser_hits = []
        if body_json is not None:
            _collect_task_ids_from_obj(body_json, task_ids)
            _collect_urls_from_obj(body_json, image_urls)
            _collect_video_urls_from_obj(body_json, video_urls)
            _collect_error_messages_from_obj(body_json, backend_error_messages)

            # Parse riêng cho Flow generate: lấy mediaId để upscale 2K sau đó.
            if "flowmedia:batchgenerateimages" in (url or "").lower():
                parser_hits.append("flowmedia:batchgenerateimages")
                try:
                    media = (body_json or {}).get("media", []) or []
                    scenes_local = meta.get("scene_numbers", []) or []
                    for idx, item in enumerate(media):
                        if not isinstance(item, dict):
                            continue
                        media_id = str(item.get("name", "") or "")
                        if not media_id:
                            continue

                        scene_no = 0
                        # Ưu tiên map từ request scene_numbers.
                        if len(scenes_local) == 1:
                            scene_no = scenes_local[0]
                        elif idx < len(scenes_local):
                            scene_no = scenes_local[idx]

                        # Fallback: parse scene từ prompt trong response.
                        if not scene_no:
                            prompt_text = str(
                                (((item.get("image", {}) or {}).get("generatedImage", {}) or {}).get("prompt", ""))
                                or ""
                            )
                            scene_no = _extract_scene_number_from_any_text(prompt_text, 0)

                        if scene_no:
                            _append_unique_dict_list(_scene_to_media_ids, scene_no, [media_id])
                except Exception:
                    pass

            # Parse riêng cho Flow generate VIDEO: scene -> video mediaId.
            if _is_flow_video_generate_api_url(url):
                parser_hits.append("flow_video_generate")
                try:
                    media = (body_json or {}).get("media", []) or []
                    # Format mới có thể không để media ở root.
                    if not media:
                        media = []
                        _collect_video_media_items_from_obj(body_json, media)
                        dedup_media = []
                        seen_mid = set()
                        for it in media:
                            if not isinstance(it, dict):
                                continue
                            mid = str(it.get("name", "") or "")
                            if not mid or mid in seen_mid:
                                continue
                            seen_mid.add(mid)
                            dedup_media.append(it)
                        media = dedup_media
                    scenes_local = meta.get("scene_numbers", []) or []
                    operations = (body_json or {}).get("operations", []) or []
                    default_status = ""
                    # Chỉ lấy default_status nếu response thật sự chỉ có 1 media và 1 operation đi chung
                    if len(operations) == 1 and len(media) == 1 and isinstance(operations[0], dict):
                        default_status = _normalize_media_status_text(operations[0].get("status", ""))
                    for idx, item in enumerate(media):
                        if not isinstance(item, dict):
                            continue
                        media_id = str(item.get("name", "") or "")
                        if not media_id:
                            continue

                        scene_no = 0
                        if len(scenes_local) == 1:
                            scene_no = scenes_local[0]
                        elif idx < len(scenes_local):
                            scene_no = scenes_local[idx]

                        if not scene_no:
                            video_obj = item.get("video", {}) or {}
                            generated_video = video_obj.get("generatedVideo", {}) or {}
                            prompt_text = str(generated_video.get("prompt", "") or "")
                            if not prompt_text:
                                media_meta = (item.get("mediaMetadata", {}) or {})
                                prompt_text = str(media_meta.get("mediaTitle", "") or "")
                            scene_no = _extract_scene_number_from_any_text(prompt_text, 0)
                        if not scene_no:
                            # Thay vì gán mù, tìm xem ID này đã được map lúc PENDING (từ generate) hay chưa
                            for s_no, media_dict in _video_media_state.items():
                                if media_id in media_dict:
                                    scene_no = s_no
                                    break
                            
                            # Nếu VẪN không tìm thấy scene, bỏ qua (đây có thể là ảnh reference từ STEP 1)
                            if not scene_no:
                                _register_orphan_video_media(media_id, status="")
                                continue

                        if scene_no:
                            status = _extract_video_media_status(item, default_status=default_status)
                            _register_scene_video_media(scene_no, media_id, status)
                            video_media_updates.append({
                                "scene": scene_no,
                                "media_id": media_id,
                                "status": status,
                            })
                except Exception:
                    pass

            # Parse projectInitialData để cập nhật scene -> video mediaId khi job cập nhật trạng thái.
            # FIX: Video media nằm ở CẢ 2 vị trí:
            #   1. data_json.media[]  (root level — format cũ)
            #   2. data_json.projectContents.media[]  (format mới của Flow — chứa video thực tế)
            #   3. data_json.projectContents.workflows[].metadata.primaryMediaId  (mapping workflow -> media)
            if "flow.projectinitialdata" in (url or "").lower():
                parser_hits.append("flow.projectinitialdata")
                try:
                    data_json = ((((body_json or {}).get("result", {}) or {}).get("data", {}) or {}).get("json", {}) or {})
                    # Gom media từ cả root level VÀ projectContents.media[]
                    media_root = (data_json.get("media", []) or [])
                    project_contents = (data_json.get("projectContents", {}) or {})
                    media_pc = (project_contents.get("media", []) or [])
                    # Gộp cả 2 nguồn, dedupe theo name (mediaId)
                    seen_ids = set()
                    all_media = []
                    for item in media_pc + media_root:
                        if not isinstance(item, dict):
                            continue
                        mid = str(item.get("name", "") or "")
                        if mid and mid not in seen_ids:
                            seen_ids.add(mid)
                            all_media.append(item)
                    for item in all_media:
                        media_id = str(item.get("name", "") or "")
                        if not media_id:
                            continue
                        video_obj = item.get("video", {}) or {}
                        generated_video = video_obj.get("generatedVideo", {}) or {}
                        prompt_text = str(generated_video.get("prompt", "") or "")
                        if not prompt_text:
                            prompt_text = str(((item.get("mediaMetadata", {}) or {}).get("mediaTitle", "")) or "")
                        scene_no = _extract_scene_number_from_any_text(prompt_text, 0)
                        if scene_no:
                            status = _extract_video_media_status(item, default_status="")
                            _register_scene_video_media(scene_no, media_id, status)
                            video_media_updates.append({
                                "scene": scene_no,
                                "media_id": media_id,
                                "status": status,
                            })
                    # Parse thêm workflows[].metadata.primaryMediaId để backup mapping
                    workflows = (project_contents.get("workflows", []) or [])
                    for wf in workflows:
                        if not isinstance(wf, dict):
                            continue
                        wf_meta = (wf.get("metadata", {}) or {})
                        primary_mid = str(wf_meta.get("primaryMediaId", "") or "")
                        display_name = str(wf_meta.get("displayName", "") or "")
                        if primary_mid and display_name:
                            scene_no = _extract_scene_number_from_any_text(display_name, 0)
                            if scene_no:
                                # Workflow thường là nguồn map scene chính xác nhất.
                                _register_scene_video_media(scene_no, primary_mid, "")
                                video_media_updates.append({
                                    "scene": scene_no,
                                    "media_id": primary_mid,
                                    "status": "",
                                })
                except Exception:
                    pass

            # Parse chuyên biệt cho get_history_by_ids (scene -> cover_url)
            if "/get_history_by_ids" in url:
                parser_hits.append("get_history_by_ids")
                scene_cover_map, submit_scene_map = _extract_scene_cover_and_submit_map_from_history_json(
                    body_json,
                    trusted_submit_ids=_trusted_submit_ids,
                    run_started_ts=_run_started_ts,
                    max_age_sec=API_HISTORY_MAX_AGE_SEC,
                )
                for sc, urls in scene_cover_map.items():
                    _append_unique_dict_list(_scene_to_image_urls, sc, urls)
                for submit_id, sc in submit_scene_map.items():
                    _submit_to_scene[str(submit_id)] = sc

            # Parse generate response để map submit_id -> scene nhanh hơn
            if "/aigc_draft/generate" in url:
                parser_hits.append("aigc_draft/generate")
                try:
                    aigc = ((body_json or {}).get("data") or {}).get("aigc_data") or {}
                    task_obj = aigc.get("task", {}) or {}
                    submit_id = str(task_obj.get("submit_id", "") or "")
                    history_record_id = str(aigc.get("history_record_id", "") or "")
                    scenes_local = meta.get("scene_numbers", []) or []
                    if scenes_local:
                        sc = scenes_local[0]
                        if submit_id:
                            _trusted_submit_ids.add(submit_id)
                            _submit_to_scene[submit_id] = sc
                        if history_record_id:
                            _append_unique_dict_list(_scene_to_task_ids, sc, [history_record_id])
                except Exception:
                    pass

        # dedupe
        task_ids = list(dict.fromkeys(task_ids))
        image_urls = list(dict.fromkeys(image_urls))

        is_upscale = _is_upscale_api_url(url)
        request_post_data = (meta.get("post_data", "") or "")
        upscale_media_id = _extract_media_id_from_upscale_post_data(request_post_data) if is_upscale else ""
        encoded_image = ""
        if is_upscale and isinstance(body_json, dict):
            encoded_image = str((body_json or {}).get("encodedImage", "") or "")
        _api_events.append({
            "type": "api_response",
            "ts": ts,
            "url": url,
            "status": response.status,
            "resource_type": request.resource_type,
            "is_upscale": is_upscale,
            "upscale_media_id": upscale_media_id,
            "scene_numbers": meta.get("scene_numbers", []),
            "task_ids": task_ids,
            "image_urls_count": len(image_urls),
            "image_urls_sample": image_urls[:12],
            "video_urls_count": len(video_urls),
            "video_urls_sample": video_urls[:12],
            "video_media_updates_count": len(video_media_updates),
            "video_media_updates_sample": video_media_updates[:10],
            "backend_error_messages": list(dict.fromkeys(backend_error_messages))[:10],
            "parser_hits": parser_hits,
            "request_post_data_sample": request_post_data[:3000],
            "response_body_sample": body_sample[:3000] if body_sample else "",
        })

        # DEBUG mapping: in log endpoint thật + parser có hit hay không.
        # Mục tiêu: nếu UI có video mà không tải được, nhìn log sẽ biết parser miss ở đâu.
        if _looks_like_flow_video_api_url(url) or video_media_updates:
            endpoint = (url or "").split("/trpc/", 1)[-1] if "/trpc/" in (url or "") else (url or "")
            if len(endpoint) > 120:
                endpoint = endpoint[:120] + "...(cut)"
            status_sample = []
            for row in video_media_updates[:3]:
                st = str(row.get("status", "") or "")
                if st:
                    status_sample.append(st)
            err_sample = (list(dict.fromkeys(backend_error_messages))[:2] if backend_error_messages else [])
            log(
                "[FLOW-NET] "
                f"status={response.status} "
                f"endpoint={endpoint} "
                f"scene={meta.get('scene_numbers', [])} "
                f"video_updates={len(video_media_updates)} "
                f"video_urls={len(video_urls)} "
                f"status_sample={status_sample} "
                f"errors={err_sample} "
                f"parser_hits={parser_hits}",
                "DBG",
            )

        # Nếu response upscale có encodedImage thì giữ lại trong RAM để lưu file 2K theo scene.
        if is_upscale and upscale_media_id and encoded_image:
            _upscale_success_by_media[upscale_media_id] = {
                "encoded_image": encoded_image,
                "status": int(response.status),
                "url": url,
                "ts": ts,
                "media_id": upscale_media_id,
                "size_base64": len(encoded_image),
            }

        # Log riêng cho upscale để phân tích logic 2K.
        if is_upscale:
            _upscale_events.append({
                "type": "upscale_response",
                "ts": ts,
                "url": url,
                "status": response.status,
                "resource_type": request.resource_type,
                "media_id": upscale_media_id,
                "request_post_data_sample": request_post_data[:20000],
                "response_body_sample": body_sample[:20000] if body_sample else "",
                "has_encoded_image": bool(encoded_image),
                "encoded_image_size": len(encoded_image) if encoded_image else 0,
                "task_ids": task_ids,
                "image_urls_sample": image_urls[:20],
            })

        # Map scene -> task
        scenes = meta.get("scene_numbers", []) or []
        if scenes and task_ids:
            # Nếu 1 scene thì map scene đó với tất cả task_id thấy được.
            if len(scenes) == 1:
                _append_unique_dict_list(_scene_to_task_ids, scenes[0], task_ids)
            else:
                # nhiều scene: map theo vị trí tối thiểu
                for i, sc in enumerate(scenes):
                    if i < len(task_ids):
                        _append_unique_dict_list(_scene_to_task_ids, sc, [task_ids[i]])

        # Map task -> urls
        if task_ids and image_urls:
            for tid in task_ids:
                _append_unique_dict_list(_task_to_image_urls, tid, image_urls)

        # Một số API trả thẳng ảnh theo prompt/request scene mà không có task_id
        if scenes and image_urls and not task_ids:
            for sc in scenes:
                _append_unique_dict_list(_scene_to_image_urls, sc, image_urls)

        # Với get_history_by_ids: map submit_id (key data) -> scene đã biết -> cover/image urls
        if "/get_history_by_ids" in url and body_json is not None:
            try:
                data = (body_json or {}).get("data") or {}
                if isinstance(data, dict):
                    for submit_id, rec in data.items():
                        sc = _submit_to_scene.get(str(submit_id))
                        if not sc:
                            continue
                        if isinstance(rec, dict):
                            items = rec.get("item_list", []) or []
                            for it in items:
                                if not isinstance(it, dict):
                                    continue
                                common = it.get("common_attr", {}) or {}
                                cover = str(common.get("cover_url", "") or "")
                                if cover:
                                    _append_unique_dict_list(_scene_to_image_urls, sc, [cover])
                                cover_map = common.get("cover_url_map", {}) or {}
                                if isinstance(cover_map, dict):
                                    vals = [v for v in cover_map.values() if isinstance(v, str) and v.startswith("http")]
                                    if vals:
                                        _append_unique_dict_list(_scene_to_image_urls, sc, vals)
            except Exception:
                pass

    async def handle_media_playback_response(response):
        """
        Bắt chi tiết response media của Flow player:
        - status
        - content-type
        - body preview (nếu nhỏ/text)
        Mục tiêu: truy vết lỗi 'tải nội dung nghe nhìn'.
        """
        request = response.request
        url = request.url
        if not _looks_like_flow_media_url(url):
            return
        ts = time.time()
        content_type = ""
        content_length = ""
        try:
            content_type = response.headers.get("content-type", "")
            content_length = response.headers.get("content-length", "")
        except Exception:
            pass

        body_size = 0
        body_preview = ""
        should_read_body = False
        low_ct = str(content_type or "").lower()
        if response.status >= 400:
            should_read_body = True
        if "text" in low_ct or "json" in low_ct or "xml" in low_ct or "html" in low_ct:
            should_read_body = True
        if not content_length:
            should_read_body = True
        else:
            try:
                if int(content_length) <= 4096:
                    should_read_body = True
            except Exception:
                pass

        if should_read_body:
            try:
                body = await response.body()
                body_size = len(body)
                body_preview = _safe_decode_bytes_preview(body, max_len=280)
            except Exception:
                body_size = 0
                body_preview = ""

        _network_events.append({
            "type": "media_response",
            "ts": ts,
            "status": response.status,
            "resource_type": request.resource_type,
            "url": url,
            "content_type": content_type,
            "content_length": content_length,
            "body_size": body_size,
            "body_preview": body_preview,
        })

    def on_request(request):
        req_id = id(request)
        ts = time.time()
        _network_req_start[req_id] = ts
        # Bắt media_id từ redirect request để map scene dù generate response thiếu scene/media.
        _register_video_media_from_redirect_request(request.url, ts)
        is_image_or_media = (
            request.resource_type == "image"
            or _looks_like_image_url(request.url)
            or _looks_like_flow_media_url(request.url)
            or request.resource_type == "media"
        )
        if is_image_or_media:
            _network_events.append({
                "type": "request",
                "ts": ts,
                "method": request.method,
                "resource_type": request.resource_type,
                "url": request.url,
            })
        # Log request API để map prompt -> task
        if request.resource_type in {"fetch", "xhr"} and (_looks_like_api_url(request.url) or _is_upscale_api_url(request.url)):
            post_data = ""
            try:
                post_data = request.post_data or ""
            except Exception:
                post_data = ""
            scenes = _extract_scene_numbers_from_text(post_data)
            media_id = _extract_media_id_from_upscale_post_data(post_data) if _is_upscale_api_url(request.url) else ""
            _api_req_meta[req_id] = {
                "ts": ts,
                "url": request.url,
                "method": request.method,
                "post_data": post_data,
                "scene_numbers": scenes,
                "media_id": media_id,
            }
            _api_events.append({
                "type": "api_request",
                "ts": ts,
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "is_upscale": _is_upscale_api_url(request.url),
                "upscale_media_id": media_id,
                "scene_numbers": scenes,
                "post_data_sample": post_data[:3000],
            })

            # Lưu clientContext gần nhất từ request generate của Flow để gọi upsample API.
            if "flowmedia:batchgenerateimages" in (request.url or "").lower() and post_data:
                try:
                    body_json = json.loads(post_data)
                    cc = (body_json or {}).get("clientContext", {}) or {}
                    if isinstance(cc, dict) and cc:
                        _last_flow_client_context.clear()
                        _last_flow_client_context.update(cc)
                except Exception:
                    pass

            if _is_upscale_api_url(request.url):
                _upscale_events.append({
                    "type": "upscale_request",
                    "ts": ts,
                    "url": request.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "media_id": media_id,
                    "request_post_data_sample": post_data[:20000],
                })

    def on_response(response):
        request = response.request
        req_id = id(request)
        ts = time.time()
        started = _network_req_start.get(req_id, ts)
        elapsed_ms = int((ts - started) * 1000)
        content_type = ""
        content_length = ""
        try:
            content_type = response.headers.get("content-type", "")
            content_length = response.headers.get("content-length", "")
        except Exception:
            pass

        is_image_or_media = (
            request.resource_type == "image"
            or _looks_like_image_url(request.url)
            or _looks_like_flow_media_url(request.url)
            or request.resource_type == "media"
            or "image" in (content_type or "").lower()
            or "video" in (content_type or "").lower()
            or "audio" in (content_type or "").lower()
        )
        if is_image_or_media:
            _network_events.append({
                "type": "response",
                "ts": ts,
                "status": response.status,
                "resource_type": request.resource_type,
                "url": request.url,
                "content_type": content_type,
                "content_length": content_length,
                "elapsed_ms": elapsed_ms,
            })
        # API response xử lý async để có body/json
        if request.resource_type in {"fetch", "xhr"}:
            try:
                t = asyncio.create_task(handle_api_response(response))
                _pending_api_tasks.append(t)
            except Exception:
                pass
        # Media response xử lý async để đọc body nhỏ/lỗi.
        if _looks_like_flow_media_url(request.url) or request.resource_type == "media":
            try:
                t2 = asyncio.create_task(handle_media_playback_response(response))
                _pending_api_tasks.append(t2)
            except Exception:
                pass

    def on_request_failed(request):
        ts = time.time()
        if (
            request.resource_type == "image"
            or _looks_like_image_url(request.url)
            or _looks_like_flow_media_url(request.url)
            or request.resource_type == "media"
        ):
            failure = ""
            try:
                failure = request.failure or ""
            except Exception:
                pass
            _network_events.append({
                "type": "request_failed",
                "ts": ts,
                "resource_type": request.resource_type,
                "url": request.url,
                "failure": str(failure),
            })

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)


def save_network_debug() -> str:
    """Ghi log network ảnh ra file JSON để soi lỗi tải chi tiết."""
    if not _debug_session_dir:
        return ""
    path = os.path.join(_debug_session_dir, "network_images_debug.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_network_events, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return ""


def save_api_debug() -> str:
    """Lưu debug API chi tiết để soi mapping scene -> task -> image."""
    if not _debug_session_dir:
        return ""
    path = os.path.join(_debug_session_dir, "network_api_debug.json")
    payload = {
        "events": _api_events,
        "scene_to_task_ids": _scene_to_task_ids,
        "task_to_image_urls": _task_to_image_urls,
        "scene_to_image_urls": _scene_to_image_urls,
        "scene_to_media_ids": _scene_to_media_ids,
        "scene_to_video_media_ids": _scene_to_video_media_ids,
        "scene_to_video_ready_media_ids": _scene_to_video_ready_media_ids,
        "scene_to_video_failed_media_ids": _scene_to_video_failed_media_ids,
        "video_media_status_by_id": _video_media_status_by_id,
        "video_download_events_count": len(_video_download_events),
        "flow_ui_error_events_count": len(_flow_ui_error_events),
        "upscale_success_media_ids": sorted(list(_upscale_success_by_media.keys())),
        "last_flow_client_context_exists": bool(_last_flow_client_context),
        "submit_to_scene": _submit_to_scene,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return ""


def save_upscale_debug() -> str:
    """
    Lưu log riêng cho luồng upscale 2K của Flow.
    Dùng để soi:
    - request gửi gì khi bấm upscale
    - response trả gì, có URL ảnh 2K hay job id hay không
    """
    if not _debug_session_dir:
        return ""
    path = os.path.join(_debug_session_dir, "upscale_2k_debug.json")
    payload = {
        "generated_at": datetime.now().isoformat(),
        "events_count": len(_upscale_events),
        "events": _upscale_events,
        "success_media_ids": sorted(list(_upscale_success_by_media.keys())),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return ""


def save_video_error_debug() -> str:
    """
    Lưu debug chuyên sâu cho lỗi video Flow:
    - UI báo lỗi gì.
    - Mỗi attempt tải video nhận status/content-type/body ra sao.
    """
    if not _debug_session_dir:
        return ""
    path = os.path.join(_debug_session_dir, "video_error_debug.json")
    ui_messages = []
    for ev in _flow_ui_error_events:
        ui_messages.extend(ev.get("messages", []) or [])
    is_rate_limit = _has_rate_limit_ui_error(ui_messages)
    is_audiovisual_load = _has_audiovisual_load_ui_error(ui_messages)

    media_error_events = []
    for ev in _network_events:
        if ev.get("type") not in {"media_response", "request_failed"}:
            continue
        if not _looks_like_flow_media_url(ev.get("url", "")):
            continue
        status = int(ev.get("status", 0) or 0) if str(ev.get("status", "")).isdigit() else 0
        body_size = int(ev.get("body_size", 0) or 0) if str(ev.get("body_size", "")).isdigit() else 0
        ct = str(ev.get("content_type", "") or "").lower()
        is_error_like = False
        if ev.get("type") == "request_failed":
            is_error_like = True
        elif status >= 400:
            is_error_like = True
        elif body_size and body_size < 1024 and ("text" in ct or "json" in ct or "xml" in ct or "html" in ct):
            is_error_like = True
        if is_error_like:
            media_error_events.append(ev)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "error_classifier": {
            "has_rate_limit_ui_error": is_rate_limit,
            "has_audiovisual_load_ui_error": is_audiovisual_load,
        },
        "ui_error_events_count": len(_flow_ui_error_events),
        "ui_error_events": _flow_ui_error_events,
        "video_download_events_count": len(_video_download_events),
        "video_download_events": _video_download_events,
        "media_error_events_count": len(media_error_events),
        "media_error_events": media_error_events,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return ""


def save_flow_video_scene_report(prompts: list[str]) -> str:
    """
    Tạo báo cáo dễ đọc theo từng cảnh video:
    - Scene nào có bao nhiêu mediaId.
    - MediaId nào tải được / chưa tải được.
    - Gợi ý nguyên nhân chính (rate limit, audiovisual, pending lâu...).
    """
    if not _debug_session_dir:
        return ""
    path = os.path.join(_debug_session_dir, "flow_video_scene_report.json")

    # Map scene -> danh sách file video tải thành công để biết 1 prompt đã ra mấy video.
    success_by_scene = {}
    for row in _download_hash_records:
        if not isinstance(row, dict):
            continue
        fname = str(row.get("filename", "") or "")
        if not fname.lower().endswith(".mp4"):
            continue
        scene_no = int(row.get("prompt_num", 0) or 0)
        if scene_no > 0:
            success_by_scene.setdefault(scene_no, []).append(row)

    ui_messages = []
    for ev in _flow_ui_error_events:
        ui_messages.extend(ev.get("messages", []) or [])
    has_rate_limit = _has_rate_limit_ui_error(ui_messages)
    has_audiovisual = _has_audiovisual_load_ui_error(ui_messages)

    scenes = []
    prompt_scene_order = [extract_scene_number(p, i + 1) for i, p in enumerate(prompts or [])]
    scene_prompt_preview = {}
    for i, p in enumerate(prompts or []):
        sc = extract_scene_number(p, i + 1)
        if sc not in scene_prompt_preview:
            scene_prompt_preview[sc] = p[:180]
    for scene_no in prompt_scene_order:
        media_ids = _scene_to_video_media_ids.get(scene_no, []) or []
        ready_ids = _scene_to_video_ready_media_ids.get(scene_no, []) or []
        failed_ids = _scene_to_video_failed_media_ids.get(scene_no, []) or []
        attempts = [ev for ev in _video_download_events if int(ev.get("scene_no", 0) or 0) == scene_no]
        small_count = sum(1 for ev in attempts if str(ev.get("phase", "")) == "gcs_body_too_small")
        ok_count = sum(1 for ev in attempts if str(ev.get("phase", "")).startswith("download_ok"))
        success_records = success_by_scene.get(scene_no, [])

        # Quy tắc đoán nguyên nhân chính để người dùng dễ hiểu.
        reason = "unknown"
        if success_records:
            reason = "success"
        elif has_audiovisual:
            reason = "ui_audiovisual_load_error"
        elif has_rate_limit:
            reason = "rate_limit_or_throttle"
        elif attempts and small_count == len(attempts):
            reason = "all_attempts_returned_small_partial_mp4"
        elif attempts and ok_count == 0:
            reason = "download_attempted_but_not_ready"
        elif not attempts and media_ids:
            reason = "have_media_ids_but_no_download_attempts"
        elif not media_ids:
            reason = "no_media_id_detected_for_scene"

        scenes.append({
            "scene_no": scene_no,
            "prompt_preview": scene_prompt_preview.get(scene_no, ""),
            "media_ids": media_ids,
            "ready_media_ids": ready_ids,
            "failed_media_ids": failed_ids,
            "media_status_by_id": {mid: _video_media_status_by_id.get(mid, "") for mid in media_ids},
            "download_attempts_count": len(attempts),
            "small_partial_mp4_count": small_count,
            "download_ok_count": ok_count,
            "download_success_count": len(success_records),
            "download_success_records": success_records,
            "suspected_reason": reason,
        })

    payload = {
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total_scenes": len(prompt_scene_order),
            "downloaded_scenes": len(success_by_scene),
            "failed_scenes": max(0, len(prompt_scene_order) - len(success_by_scene)),
            "downloaded_videos": sum(len(rows) for rows in success_by_scene.values()),
            "has_rate_limit_ui_error": has_rate_limit,
            "has_audiovisual_load_ui_error": has_audiovisual,
        },
        "scenes": scenes,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return ""


def save_request_response_timeline() -> str:
    """
    Lưu timeline request/response dạng text dễ đọc.
    Mục tiêu: người không rành code vẫn thấy được:
    - Request gửi đi URL nào, method gì.
    - Response trả về status gì, có bao nhiêu URL ảnh.
    """
    if not _debug_session_dir:
        return ""

    path = os.path.join(_debug_session_dir, "request_response_timeline.txt")
    rows = []

    # ── 1) Timeline API request/response ──
    for ev in _api_events:
        ts = datetime.fromtimestamp(ev.get("ts", time.time())).strftime("%H:%M:%S")
        ev_type = ev.get("type", "")
        url = str(ev.get("url", ""))
        if len(url) > 140:
            url = url[:140] + "...(cut)"

        if ev_type == "api_request":
            prefix = "UPSCALE_REQUEST" if ev.get("is_upscale") else "API_REQUEST"
            rows.append(
                f"[{ts}] {prefix:<14} method={ev.get('method', '')} "
                f"scene={ev.get('scene_numbers', [])} url={url}"
            )
        elif ev_type == "api_response":
            prefix = "UPSCALE_RESPONSE" if ev.get("is_upscale") else "API_RESPONSE"
            video_updates = ev.get("video_media_updates_count", 0)
            rows.append(
                f"[{ts}] {prefix:<14} status={ev.get('status', '')} "
                f"scene={ev.get('scene_numbers', [])} task_ids={len(ev.get('task_ids', []))} "
                f"image_urls={ev.get('image_urls_count', 0)} "
                f"video_urls={ev.get('video_urls_count', 0)} "
                f"video_media_updates={video_updates} url={url}"
            )

    # ── 2) Timeline ảnh (image request/response/fail) ──
    for ev in _network_events:
        ts = datetime.fromtimestamp(ev.get("ts", time.time())).strftime("%H:%M:%S")
        ev_type = ev.get("type", "")
        url = str(ev.get("url", ""))
        if len(url) > 140:
            url = url[:140] + "...(cut)"

        if ev_type == "request":
            rows.append(
                f"[{ts}] IMG_REQUEST  method={ev.get('method', '')} "
                f"rtype={ev.get('resource_type', '')} url={url}"
            )
        elif ev_type == "response":
            rows.append(
                f"[{ts}] IMG_RESPONSE status={ev.get('status', '')} "
                f"rtype={ev.get('resource_type', '')} "
                f"elapsed_ms={ev.get('elapsed_ms', '')} ct={ev.get('content_type', '')} url={url}"
            )
        elif ev_type == "request_failed":
            rows.append(
                f"[{ts}] IMG_FAILED   rtype={ev.get('resource_type', '')} "
                f"error={ev.get('failure', '')} url={url}"
            )
        elif ev_type == "media_response":
            bsz = ev.get("body_size", "")
            rows.append(
                f"[{ts}] MEDIA_RESP   status={ev.get('status', '')} "
                f"rtype={ev.get('resource_type', '')} bytes={bsz} "
                f"ct={ev.get('content_type', '')} url={url}"
            )

    # ── 3) Timeline lỗi UI video và attempt tải video (debug chi tiết) ──
    for ev in _flow_ui_error_events:
        ts = datetime.fromtimestamp(ev.get("ts", time.time())).strftime("%H:%M:%S")
        msg = " | ".join(ev.get("messages", []) or [])
        if len(msg) > 180:
            msg = msg[:180] + "...(cut)"
        rows.append(f"[{ts}] UI_ERROR     label={ev.get('label', '')} msg={msg}")

    for ev in _video_download_events:
        ts = datetime.fromtimestamp(ev.get("ts", time.time())).strftime("%H:%M:%S")
        rows.append(
            f"[{ts}] VIDEO_DL     scene={ev.get('scene_no')} attempt={ev.get('attempt')} "
            f"media={ev.get('media_id_short', '')} status={ev.get('media_status', '')} "
            f"redirect={ev.get('redirect_status', '')} gcs={ev.get('gcs_status', '')} "
            f"bytes={ev.get('body_size', '')} ct={ev.get('content_type', '')}"
        )

    # Sắp xếp lại theo timestamp để dễ xem flow thật.
    rows_sorted = sorted(rows)
    lines = [
        "=== REQUEST / RESPONSE TIMELINE ===",
        f"platform={TARGET_PLATFORM}",
        f"generated_at={datetime.now().isoformat()}",
        "",
    ]
    lines.extend(rows_sorted)

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path
    except Exception:
        return ""


def build_api_scene_first_image_map(prompts: list[str]) -> dict:
    """
    Dựng map scene_no -> image_url đầu tiên từ dữ liệu API đã bắt.
    Ưu tiên:
    1) scene_to_image_urls (trực tiếp theo scene)
    2) scene_to_task_ids -> task_to_image_urls
    """
    out = {}
    for i, p in enumerate(prompts):
        scene_no = extract_scene_number(p, i + 1)
        # direct
        direct = _scene_to_image_urls.get(scene_no, [])
        if direct:
            out[scene_no] = direct[0]
            continue
        # via task
        tids = _scene_to_task_ids.get(scene_no, [])
        for tid in tids:
            urls = _task_to_image_urls.get(tid, [])
            if urls:
                out[scene_no] = urls[0]
                break
    return out


def get_scene_candidate_urls(scene_no: int, preferred_url: str = "") -> list[str]:
    """
    Trả về danh sách URL ảnh ứng viên cho 1 cảnh theo độ ưu tiên:
    1) preferred_url (thường là URL đầu tiên từ api_scene_map)
    2) _scene_to_image_urls[scene_no]
    3) _scene_to_task_ids[scene_no] -> _task_to_image_urls[task_id]
    Dùng để retry tải ảnh khi URL đầu tiên lỗi.
    """
    urls = []
    if preferred_url:
        urls.append(preferred_url)

    direct = _scene_to_image_urls.get(scene_no, []) or []
    urls.extend(direct)

    tids = _scene_to_task_ids.get(scene_no, []) or []
    for tid in tids:
        task_urls = _task_to_image_urls.get(tid, []) or []
        urls.extend(task_urls)

    # Dedupe, giữ nguyên thứ tự ưu tiên.
    return list(dict.fromkeys([u for u in urls if isinstance(u, str) and u.startswith("http")]))


def get_scene_candidate_video_media_ids(scene_no: int) -> list[str]:
    """
    Lấy danh sách mediaId video ứng viên theo thứ tự ưu tiên:
    1) media READY (mới nhất trước),
    2) media chưa rõ trạng thái (mới nhất trước),
    3) media FAILED (để cuối, chỉ thử khi hết lựa chọn).
    """
    ready = list(reversed(_scene_to_video_ready_media_ids.get(scene_no, []) or []))
    all_ids = list(reversed(_scene_to_video_media_ids.get(scene_no, []) or []))
    failed = set(_scene_to_video_failed_media_ids.get(scene_no, []) or [])

    ordered = []
    ordered.extend(ready)
    ordered.extend([mid for mid in all_ids if mid not in ready and mid not in failed])
    ordered.extend([mid for mid in all_ids if mid in failed])

    # Dedupe giữ nguyên thứ tự ưu tiên.
    return list(dict.fromkeys([mid for mid in ordered if isinstance(mid, str) and mid]))


def _extract_video_media_from_project_initial_data_body(body_json) -> list[dict]:
    """
    Tách danh sách media video từ response flow.projectInitialData.
    Mỗi phần tử: {media_id, status, scene_no}
    """
    out = []
    if not isinstance(body_json, dict):
        return out
    data_json = ((((body_json or {}).get("result", {}) or {}).get("data", {}) or {}).get("json", {}) or {})
    if not isinstance(data_json, dict):
        return out

    media_root = (data_json.get("media", []) or [])
    project_contents = (data_json.get("projectContents", {}) or {})
    media_pc = (project_contents.get("media", []) or [])
    seen_ids = set()
    for item in media_pc + media_root:
        if not isinstance(item, dict):
            continue
        media_id = str(item.get("name", "") or "")
        if not media_id or media_id in seen_ids:
            continue
        seen_ids.add(media_id)
        video_obj = item.get("video", {}) or {}
        generated_video = video_obj.get("generatedVideo", {}) or {}
        prompt_text = str(generated_video.get("prompt", "") or "")
        if not prompt_text:
            prompt_text = str(((item.get("mediaMetadata", {}) or {}).get("mediaTitle", "")) or "")
        scene_no = _extract_scene_number_from_any_text(prompt_text, 0)
        status = _extract_video_media_status(item, default_status="")
        out.append({"media_id": media_id, "status": status, "scene_no": scene_no})

    workflows = (project_contents.get("workflows", []) or [])
    for wf in workflows:
        if not isinstance(wf, dict):
            continue
        wf_meta = (wf.get("metadata", {}) or {})
        primary_mid = str(wf_meta.get("primaryMediaId", "") or "")
        display_name = str(wf_meta.get("displayName", "") or "")
        if not primary_mid:
            continue
        scene_no = _extract_scene_number_from_any_text(display_name, 0)
        out.append({"media_id": primary_mid, "status": "", "scene_no": scene_no})

    dedup = {}
    for row in out:
        mid = str(row.get("media_id", "") or "")
        if not mid:
            continue
        cur = dedup.get(mid, {"media_id": mid, "status": "", "scene_no": 0})
        sc = int(row.get("scene_no", 0) or 0)
        st = str(row.get("status", "") or "")
        if sc and not cur.get("scene_no"):
            cur["scene_no"] = sc
        if st and not cur.get("status"):
            cur["status"] = st
        dedup[mid] = cur
    return list(dedup.values())


async def _poll_flow_project_initial_data_for_video_media(page, project_id: str) -> dict:
    """
    Poll API flow.projectInitialData để lấy media mới.
    Trả về summary để log debug.
    """
    result = {"ok": False, "status": 0, "mapped_scene": 0, "orphan_added": 0, "error": ""}
    if not project_id:
        return result
    try:
        origin = f"{urlparse(page.url).scheme}://{urlparse(page.url).netloc}" if page.url else "https://labs.google"
        payload = {"json": {"projectId": project_id}}
        input_q = quote(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        api_url = f"{origin}/fx/api/trpc/flow.projectInitialData?input={input_q}"
        resp = await page.context.request.get(api_url, timeout=30000)
        result["status"] = int(resp.status)
        if not resp.ok:
            return result
        body_text = await resp.text()
        body_json = json.loads(body_text) if body_text else {}
        rows = _extract_video_media_from_project_initial_data_body(body_json)
        for row in rows:
            media_id = str(row.get("media_id", "") or "")
            status = str(row.get("status", "") or "")
            scene_no = int(row.get("scene_no", 0) or 0)
            if not media_id:
                continue
            if scene_no:
                before = _has_scene_already_mapped_media(scene_no, media_id)
                _register_scene_video_media(scene_no, media_id, status)
                _orphan_video_media_ts.pop(media_id, None)
                if not before:
                    result["mapped_scene"] += 1
            else:
                before = media_id in _orphan_video_media_ts
                _register_orphan_video_media(media_id, status=status)
                if not before:
                    result["orphan_added"] += 1
        result["ok"] = True
        return result
    except Exception as e:
        result["error"] = str(e)
        return result


def _assign_orphan_media_to_pending_scenes(
    sent_scene_order: list[int],
    downloaded_scene_set: set[int],
    failed_scene_set: set[int],
) -> int:
    """
    Gán orphan media vào cảnh pending cũ nhất để đi nhánh tải hiện có.
    """
    if not _orphan_video_media_ts:
        return 0
    pending_scenes = [sc for sc in sent_scene_order if sc not in downloaded_scene_set and sc not in failed_scene_set]
    if not pending_scenes:
        return 0

    assigned = 0
    for media_id, _ts in sorted(_orphan_video_media_ts.items(), key=lambda kv: kv[1]):
        target_scene = 0
        for sc in pending_scenes:
            ids = _scene_to_video_media_ids.get(sc, []) or []
            if media_id in ids:
                target_scene = 0
                break
            if len(ids) < 8:
                target_scene = sc
                break
        if not target_scene:
            continue
        _register_scene_video_media(target_scene, media_id, _video_media_status_by_id.get(media_id, ""))
        _orphan_video_media_ts.pop(media_id, None)
        assigned += 1
        log(f"[FLOW-MAP] orphan media {media_id[:8]}... -> canh_{target_scene:03d}", "DBG")
    return assigned


def _get_recent_backend_errors_for_scene(scene_no: int, limit: int = 2) -> list[str]:
    """
    Lấy vài lỗi backend gần nhất liên quan scene để log lúc timeout.
    """
    out = []
    for ev in reversed(_api_events):
        if ev.get("type") != "api_response":
            continue
        scenes = ev.get("scene_numbers", []) or []
        if scene_no not in scenes:
            continue
        errs = ev.get("backend_error_messages", []) or []
        for e in errs:
            if not isinstance(e, str):
                continue
            t = e.strip()
            if t and t not in out:
                out.append(t)
                if len(out) >= limit:
                    return out
    return out


def _build_scene_video_output_path(scene_no: int, variant_index: int) -> str:
    """
    Dựng tên file video theo cảnh + biến thể.
    Ví dụ:
    - video đầu tiên của cảnh 12  -> canh_012.mp4
    - video thứ hai của cảnh 12   -> canh_012_v2.mp4

    Mục tiêu:
    - giữ tương thích cũ cho cảnh chỉ có 1 video,
    - tránh ghi đè khi 1 prompt sinh nhiều video.
    """
    base_name = f"canh_{scene_no:03d}.mp4" if variant_index <= 1 else f"canh_{scene_no:03d}_v{variant_index}.mp4"
    return os.path.join(OUTPUT_DIR, base_name)


def _safe_decode_bytes_preview(body: bytes, max_len: int = 240) -> str:
    """
    Decode an toàn để đọc nhanh body text khi file video trả về quá nhỏ.
    Mục tiêu: nhìn được thông báo lỗi backend (nếu có) thay vì chỉ thấy số bytes.
    """
    if not body:
        return ""
    try:
        txt = body.decode("utf-8", errors="ignore").strip()
        if not txt:
            return ""
        txt = re.sub(r"\s+", " ", txt)
        if len(txt) > max_len:
            txt = txt[:max_len] + "...(cut)"
        return txt
    except Exception:
        return ""


async def capture_flow_ui_error_messages(page, label: str = "") -> list[str]:
    """
    Quét text lỗi đang hiển thị trên UI Flow để biết lỗi đến từ frontend hay backend.
    Ví dụ các câu:
    - "Không thành công"
    - "Đã xảy ra lỗi khi tải nội dung nghe nhìn của bạn."
    """
    keywords = [
        "không thành công",
        "đã xảy ra lỗi khi tải nội dung nghe nhìn",
        "nội dung nghe nhìn của bạn",
        "tải nội dung nghe nhìn",
        "không tạo được âm thanh",
        "an error occurred while loading your audiovisual content",
        "while loading your audiovisual content",
        "failed",
        "could not load",
    ]
    try:
        messages = await page.evaluate(
            """(keywords) => {
                const out = [];
                const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
                const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (!style || style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const nodes = document.querySelectorAll('div,span,p,[role="alert"],[aria-live],button');
                for (const el of nodes) {
                    if (!isVisible(el)) continue;
                    const t = norm(el.textContent || "");
                    if (!t || t.length > 220) continue;
                    const low = t.toLowerCase();
                    if (keywords.some(k => low.includes(k))) out.push(t);
                }
                return [...new Set(out)].slice(0, 12);
            }""",
            keywords,
        )
    except Exception:
        messages = []

    messages = [m for m in (messages or []) if isinstance(m, str) and m.strip()]
    if messages:
        evt = {
            "ts": time.time(),
            "label": label or "",
            "url": page.url,
            "messages": messages,
        }
        _flow_ui_error_events.append(evt)
        log(f"UI lỗi ({label or 'flow'}): {' | '.join(messages)}", "WARN")
    return messages


def _has_rate_limit_ui_error(messages: list[str]) -> bool:
    """
    Nhận diện lỗi giới hạn tần suất từ UI để dừng retry sớm.
    """
    for msg in messages or []:
        low = str(msg).lower()
        if "yêu cầu tạo quá nhanh" in low:
            return True
        if "requesting too quickly" in low:
            return True
        if "too many requests" in low:
            return True
    return False


def _has_unusual_activity_ui_error(messages: list[str]) -> bool:
    """
    Nhận diện thông báo bất thường/chống bot từ UI.
    Dùng để kích hoạt cooldown thay vì tiếp tục gửi dồn.
    """
    for msg in messages or []:
        low = str(msg).lower()
        if "unusual activity" in low:
            return True
        if "chúng tôi nhận thấy hoạt động bất thường" in low:
            return True
        if "we noticed some unusual activity" in low:
            return True
    return False


def _has_audiovisual_load_ui_error(messages: list[str]) -> bool:
    """
    Nhận diện lỗi UI: 'Đã xảy ra lỗi khi tải nội dung nghe nhìn của bạn.'
    """
    for msg in messages or []:
        low = str(msg).lower()
        if "nội dung nghe nhìn" in low:
            return True
        if "audiovisual content" in low:
            return True
        if "could not load your media" in low:
            return True
    return False


def _has_generic_failure_ui_error(messages: list[str]) -> bool:
    """
    Nhận diện lỗi UI chung kiểu "Không thành công"/"failed" để tránh vòng lặp cứng.
    """
    for msg in messages or []:
        low = str(msg).lower()
        if "không thành công" in low:
            return True
        if "failed" in low:
            return True
        if "could not load" in low:
            return True
    return False


def apply_event_order_fallback_scene_map(prompts: list[str], scene_map: dict) -> dict:
    """
    Fallback cho trường hợp API không map được scene rõ ràng (ví dụ prompt không có 'CẢNH 001').
    Chiến thuật:
    - Lấy URL ảnh theo thứ tự xuất hiện trong api_response.
    - Gán tuần tự cho các scene còn thiếu.
    """
    if not prompts:
        return scene_map or {}

    out = dict(scene_map or {})
    prompt_scene_order = [extract_scene_number(p, i + 1) for i, p in enumerate(prompts)]
    missing = [sc for sc in prompt_scene_order if not out.get(sc)]
    if not missing:
        return out

    # Strict mode cho STEP reference (scene 9xx):
    # Không được gán URL theo thứ tự event vì dễ đảo nhãn character/image.
    # Nếu thiếu map scene nào thì để thiếu thật để upstream retry/regenerate.
    if any(int(sc or 0) >= 900 for sc in prompt_scene_order):
        return out

    ordered_urls = []
    for ev in _api_events:
        if ev.get("type") != "api_response":
            continue
        sample = ev.get("image_urls_sample", []) or []
        for u in sample:
            if isinstance(u, str) and u.startswith("http"):
                ordered_urls.append(u)

    # Dedupe giữ thứ tự
    ordered_urls = list(dict.fromkeys(ordered_urls))
    if not ordered_urls:
        return out

    used = set(v for v in out.values() if isinstance(v, str))
    cursor = 0
    for sc in missing:
        while cursor < len(ordered_urls) and ordered_urls[cursor] in used:
            cursor += 1
        if cursor >= len(ordered_urls):
            break
        out[sc] = ordered_urls[cursor]
        used.add(ordered_urls[cursor])
        cursor += 1
    return out


async def build_api_scene_map_with_retry(prompts: list[str],
                                         timeout_sec: int = API_MAP_POLL_TIMEOUT_SEC,
                                         interval_sec: int = API_MAP_POLL_INTERVAL_SEC) -> tuple[dict, list[int]]:
    """
    Chờ thêm một khoảng ngắn để API trả đủ map scene -> image.
    Trả về:
    - scene_map: map scene_no -> first_image_url
    - missing: danh sách scene_no chưa có URL
    """
    prompt_scene_order = [extract_scene_number(p, i + 1) for i, p in enumerate(prompts)]
    deadline = time.time() + max(0, timeout_sec)
    scene_map = {}
    missing = prompt_scene_order[:]

    while True:
        await wait_pending_api_tasks(timeout_sec=2.0)
        scene_map = build_api_scene_first_image_map(prompts)
        missing = [sc for sc in prompt_scene_order if not scene_map.get(sc)]
        if not missing:
            return scene_map, []
        if time.time() >= deadline:
            return scene_map, missing
        await asyncio.sleep(max(0.2, interval_sec))


def notify(title, message):
    os_name = platform.system()
    try:
        if os_name == "Darwin":
            subprocess.run([
                "osascript", "-e",
                f'display notification "{message}" with title "{title}" sound name "Glass"'
            ], check=False)
        elif os_name == "Windows":
            try:
                from plyer import notification as n
                n.notify(title=title, message=message, timeout=5)
            except ImportError:
                pass
        elif os_name == "Linux":
            subprocess.run(["notify-send", "-t", "5000", title, message], check=False)
    except Exception:
        pass


def close_old_google_flow_automation_session():
    """
    Trước mỗi lần chạy test mới, đóng phiên Chrome automation cũ (nếu có)
    để tránh lỗi Playwright attach vào session cũ.
    Chỉ kill process chứa profile automation, không đụng cache user khác.
    """
    try:
        # Kill theo từng profile token để hỗ trợ mode 2 Chrome (ảnh/video tách riêng).
        tokens = {
            os.path.basename(PROFILE_DIR),        # profile legacy
            os.path.basename(PROFILE_DIR_IMAGE),  # profile step ảnh
            os.path.basename(PROFILE_DIR_VIDEO),  # profile step video
        }
        for profile_token in sorted([t for t in tokens if t]):
            subprocess.run(["pkill", "-f", profile_token], check=False)
        # Đợi ngắn để process cũ thoát hẳn trước khi launch phiên mới.
        time.sleep(1.0)
    except Exception:
        pass


async def launch_chrome_context(p, profile_dir: str, har_path: str):
    """
    Launch 1 Chrome persistent context với profile chỉ định.

    Lý do tách hàm:
    - Dùng lại cho mode chạy 2 Chrome khác nhau (step ảnh và step video).
    - Giữ cùng một cấu hình launch để hành vi ổn định.
    """
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    return await p.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=False,
        channel="chrome",
        args=[
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        ignore_default_args=["--enable-automation"],
        accept_downloads=True,
        record_har_path=har_path,
        viewport={"width": VP_WIDTH, "height": VP_HEIGHT},
    )


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"completed": [], "failed": [], "last_updated": None}


def save_progress(progress):
    progress["last_updated"] = datetime.now().isoformat()
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def reset_output_dir():
    """
    Dọn thư mục output trước mỗi lần chạy để không lẫn ảnh cũ.
    Chỉ xóa file trong OUTPUT_DIR, không đụng profile/cache.
    """
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    removed = 0
    failed = 0

    for entry in Path(OUTPUT_DIR).iterdir():
        if entry.is_file():
            try:
                entry.unlink()
                removed += 1
            except Exception:
                failed += 1

    if removed > 0:
        log(f"Đã reset output: xóa {removed} file cũ", "CLEAN")
    else:
        log("Output trống, không có file cũ để xóa", "CLEAN")

    if failed > 0:
        log(f"Có {failed} file trong output không xóa được", "WARN")


def safe_filename(text, max_len=40):
    safe = re.sub(r'[^\w\s-]', '_', text[:max_len]).strip().replace(' ', '_')
    return safe or "prompt"


def extract_scene_number(prompt_text: str, fallback: int) -> int:
    """
    Tách số cảnh từ nội dung prompt.
    Ví dụ:
    - "CẢNH 030: ..." -> 30
    - "CANH 12 ..."   -> 12
    Nếu không tách được thì dùng fallback.
    """
    if not prompt_text:
        return fallback
    m = re.search(r"(?:cảnh|canh)\s*0*(\d+)", prompt_text, flags=re.IGNORECASE)
    if not m:
        return fallback
    try:
        return int(m.group(1))
    except Exception:
        return fallback


def build_auto_test_prompts(count: int) -> list[str]:
    """
    Tạo prompt test ngẫu nhiên để mỗi lần chạy đều khác nhau (dễ debug).
    Format luôn bắt đầu bằng: "CẢNH X: ..."
    """
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    random.seed(time.time_ns())

    # Bộ từ khóa ngắn gọn để phối ngẫu nhiên, giúp prompt không bị trùng.
    locations = [
        "hành lang khách sạn sang trọng",
        "quán cafe kính nhìn ra phố mưa",
        "ga tàu điện ngầm giờ cao điểm",
        "sân thượng thành phố lúc bình minh",
        "nhà bếp công nghiệp ánh đèn vàng",
        "studio trắng tối giản",
    ]
    moods = [
        "căng thẳng nhưng kiểm soát",
        "bình tĩnh lạnh lùng",
        "ngạc nhiên nhẹ",
        "quyết đoán mạnh",
        "trầm tư sâu",
        "hy vọng trở lại",
    ]
    styles = [
        "cinematic realism",
        "photojournalistic",
        "high-detail editorial",
        "natural documentary style",
        "dramatic film still",
    ]
    camera_shots = [
        "medium shot, eye-level",
        "close-up portrait, shallow depth of field",
        "wide shot, leading lines",
        "over-shoulder composition",
        "low-angle dramatic shot",
    ]
    lighting = [
        "soft daylight through window",
        "moody low-key lighting",
        "neon rim light",
        "warm practical lights",
        "high contrast studio light",
    ]

    prompts = []
    for i in range(1, count + 1):
        loc = random.choice(locations)
        mood = random.choice(moods)
        style = random.choice(styles)
        shot = random.choice(camera_shots)
        light = random.choice(lighting)
        unique_tag = random.randint(1000, 9999)

        prompts.append(
            f"CẢNH {i}: TEST RUN {run_id}-{unique_tag}. "
            f"Nhân vật nữ đứng tại {loc}, cảm xúc {mood}. "
            f"Phong cách {style}, góc máy {shot}, ánh sáng {light}, "
            f"chi tiết da thật, texture quần áo rõ, background có chiều sâu, "
            f"không chữ, không watermark."
        )

    return prompts


def save_generated_prompts(prompts: list[str]) -> str:
    """
    Lưu bộ prompt test ra file để bạn mở lại kiểm tra khi cần.
    File lưu trong thư mục prompts/ theo đúng quy ước dễ quản lý.
    """
    Path(PROMPTS_DIR).mkdir(parents=True, exist_ok=True)
    out_path = os.path.abspath(
        os.path.join(
            PROMPTS_DIR,
            f"auto_test_prompts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        )
    )
    with open(out_path, "w", encoding="utf-8") as f:
        for p in prompts:
            f.write(p + "\n")
    return out_path


def load_prompt_pool() -> list[str]:
    """
    Đọc danh sách prompt pool.
    - Ưu tiên file mới: prompts/prompt_pool_1000.txt
    - Fallback file cũ: prompts/prompt_pool_100.txt
    Mỗi dòng là 1 prompt.
    """
    candidate_files = [
        PROMPT_POOL_FILE,
        os.path.join(PROMPTS_DIR, "prompt_pool_100.txt"),
    ]
    for file_path in candidate_files:
        if not os.path.exists(file_path):
            continue
        with open(file_path, "r", encoding="utf-8") as f:
            rows = [ln.strip() for ln in f if ln.strip()]
        if rows:
            return rows
    return []


def load_prompt_pool_state() -> dict:
    """
    Đọc trạng thái con trỏ pool:
    - next_index: vị trí bắt đầu lấy ở lần chạy kế tiếp
    """
    if not os.path.exists(PROMPT_POOL_STATE_FILE):
        return {"next_index": 0}
    try:
        with open(PROMPT_POOL_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"next_index": int(data.get("next_index", 0))}
    except Exception:
        return {"next_index": 0}


def save_prompt_pool_state(next_index: int):
    """Lưu trạng thái pool để lần sau lấy đúng block prompt kế tiếp."""
    Path(PROMPTS_DIR).mkdir(parents=True, exist_ok=True)
    payload = {
        "next_index": next_index,
        "updated_at": datetime.now().isoformat(),
    }
    with open(PROMPT_POOL_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def take_prompts_from_pool(batch_size: int = PROMPT_BATCH_SIZE) -> tuple[list[str], dict]:
    """
    Lấy đúng batch_size prompt từ pool theo cơ chế xoay vòng:
    - Ví dụ pool 100 prompt, batch 10:
      lần 1 lấy 1..10, lần 2 lấy 11..20, ...
      hết 100 thì quay lại từ đầu.
    """
    pool = load_prompt_pool()
    if not pool:
        return [], {"reason": "pool_empty"}

    total = len(pool)
    state = load_prompt_pool_state()
    start = state.get("next_index", 0) % total

    selected = []
    idx = start
    for _ in range(batch_size):
        selected.append(pool[idx])
        idx = (idx + 1) % total

    save_prompt_pool_state(idx)
    return selected, {
        "total": total,
        "start_index": start,
        "end_index_exclusive": idx,
    }


def write_prompts_file(prompts: list[str], out_file: str = PROMPTS_FILE):
    """
    Ghi prompt vào prompts.txt (mỗi dòng 1 prompt).
    """
    with open(out_file, "w", encoding="utf-8") as f:
        for p in prompts:
            f.write(p + "\n")


# ═══════════════════════════════════════════════
# ĐĂNG NHẬP — Phát hiện tự động
# ═══════════════════════════════════════════════

async def wait_for_login(page) -> bool:
    """
    Hiển thị thông báo yêu cầu đăng nhập.
    Phát hiện tự động khi user đã đăng nhập bằng cách check selector.
    Fallback: nhấn Enter trong terminal nếu auto-detect không hoạt động.
    """
    print("\n" + "=" * 60)
    print("  DREAMINA AUTO - CHO PHEP DANG NHAP")
    print("=" * 60)
    print("  1. Dang nhap Dreamina/CapCut tren browser dang mo")
    print("  2. Script tu dong phat hien khi ban dang nhap xong")
    print("  3. Neu khong tu dong → nhan Enter trong terminal")
    print("=" * 60)
    notify("Dreamina Auto", "Hãy đăng nhập Dreamina rồi chờ tự động tiếp tục")

    start = time.time()
    while time.time() - start < LOGIN_TIMEOUT:
        for sel in LOGIN_DETECT_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible(timeout=500):
                    log("Phát hiện đã đăng nhập!", "✅")
                    await asyncio.sleep(1)
                    return True
            except Exception:
                pass

        # Kiểm tra URL thay đổi (redirect sau login)
        if "login" not in page.url and "signin" not in page.url:
            # Thêm kiểm tra bằng JS: có cookie/token không?
            logged_in = await page.evaluate("""
                () => {
                    return document.cookie.includes('session')
                        || document.cookie.includes('token')
                        || document.cookie.includes('uid')
                        || !!document.querySelector('[class*="avatar"], [class*="userAvatar"]');
                }
            """)
            if logged_in:
                log("Phát hiện session đăng nhập!", "✅")
                await asyncio.sleep(1)
                return True

        elapsed = int(time.time() - start)
        if elapsed % 10 == 0 and elapsed > 0:
            log(f"  Đang chờ đăng nhập... ({elapsed}s/{LOGIN_TIMEOUT}s)", "⏳")

        await asyncio.sleep(2)

    # Fallback: hỏi terminal
    log("Không tự detect được. Nhấn Enter sau khi đăng nhập xong...", "⚠️")
    await asyncio.get_event_loop().run_in_executor(None, input, "  → Nhấn Enter: ")
    return True


# ═══════════════════════════════════════════════
# NAVIGATION
# ═══════════════════════════════════════════════

async def _safe_goto(page, url: str, timeout=30000):
    """goto bắt lỗi HTTP và chrome-error, không crash."""
    try:
        await page.goto(url, wait_until="commit", timeout=timeout)
    except Exception as e:
        log(f"  goto lỗi ({url[:60]}): {e}", "⚠️")
    await asyncio.sleep(2)


async def navigate_to_image_mode(page) -> bool:
    """
    Vào chế độ tạo hình ảnh bằng AI.
    Thử lần lượt nhiều URL format → click UI → giữ nguyên trang nếu đã đúng.
    """
    log("Đang vào chế độ tạo hình ảnh...", "🖱️")

    # Nếu đang ở trang generate rồi thì kiểm tra luôn
    if "generate" in page.url:
        if await _find_prompt_element(page) is not None:
            log("Đã ở chế độ tạo ảnh!", "✅")
            return True

    # Danh sách URL thử theo thứ tự
    candidate_urls = [
        DREAMINA_IMAGE,
        "https://dreamina.capcut.com/ai-tool/image/generate",
        "https://dreamina.capcut.com/ai-tool/generate",
        DREAMINA_HOME,
    ]

    for url in candidate_urls:
        await _safe_goto(page, url)
        await asyncio.sleep(2)

        # Nếu đang bị redirect sang chrome-error → bỏ qua
        if "chrome-error" in page.url or "about:blank" in page.url:
            continue

        # Chỉ coi là thành công khi URL thực sự là generate + có ô prompt.
        # Tránh trường hợp ở HOME có input nhưng baseline/tải ảnh lệch nhau.
        if "generate" in page.url and await _find_prompt_element(page) is not None:
            log(f"  Đã vào chế độ tạo ảnh ({page.url[:60]})", "✅")
            return True

        # Thử click menu để chuyển mode
        for text in ["Tạo hình ảnh bằng AI", "AI Image", "Image Generate", "Tạo ảnh"]:
            try:
                btn = page.get_by_text(text, exact=False).first
                if await btn.count() > 0 and await btn.is_visible(timeout=800):
                    await btn.click()
                    await asyncio.sleep(2)
                    if "generate" in page.url and await _find_prompt_element(page) is not None:
                        log(f"  Đã click '{text}'!", "✅")
                        return True
            except Exception:
                pass

    log(f"Không vào được URL generate (current: {page.url[:70]})", "⚠️")
    return True


# ═══════════════════════════════════════════════
# TÌM Ô NHẬP PROMPT
# ═══════════════════════════════════════════════

async def _find_prompt_element(page):
    """Tìm element ô nhập prompt bằng danh sách selector."""
    for sel in PROMPT_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible(timeout=800):
                return el
        except Exception:
            pass
    return None


async def find_and_focus_prompt(page):
    """
    Tìm và focus vào ô nhập prompt.
    Ưu tiên selector → fallback pixel click.
    """
    # Thử selector
    el = await _find_prompt_element(page)
    if el:
        try:
            await el.click()
            await asyncio.sleep(0.3)
            log("  Focus vào ô nhập (selector)", "✅")
            return el
        except Exception:
            pass

    # Fallback: pixel click
    vp = page.viewport_size
    w = vp["width"] if vp else VP_WIDTH
    h = vp["height"] if vp else VP_HEIGHT

    positions = [
        (w * 0.50, h - 130),
        (w * 0.50, h - 110),
        (w * 0.50, h - 155),
        (w * 0.45, h - 130),
        (w * 0.55, h - 130),
    ]

    for x, y in positions:
        try:
            await page.mouse.click(x, y)
            await asyncio.sleep(0.4)

            tag      = await page.evaluate("document.activeElement?.tagName || ''")
            editable = await page.evaluate("document.activeElement?.contentEditable || ''")
            role     = await page.evaluate("document.activeElement?.getAttribute('role') || ''")

            if (tag.lower() in ['textarea', 'input']
                    or editable == 'true'
                    or role == 'textbox'):
                log(f"  Focus vào ô nhập (pixel {int(x)},{int(y)})", "✅")
                return None  # đã focus, dùng keyboard từ đây
        except Exception:
            pass

    log("  Không tìm được ô nhập, thử tiếp...", "⚠️")
    return None


# ═══════════════════════════════════════════════
# NHẬP PROMPT
# ═══════════════════════════════════════════════

async def type_prompt(page, text: str):
    """Click ô nhập → xóa cũ → nhập text mới."""
    await find_and_focus_prompt(page)
    await asyncio.sleep(0.08)

    # Xóa nội dung cũ
    await page.keyboard.press("Meta+a" if platform.system() == "Darwin" else "Control+a")
    await asyncio.sleep(0.06)
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.08)

    # Paste prompt qua clipboard (nhanh hơn type từng ký tự)
    await page.evaluate("(text) => navigator.clipboard.writeText(text)", text)
    await asyncio.sleep(0.08)
    paste_key = "Meta+v" if platform.system() == "Darwin" else "Control+v"
    await page.keyboard.press(paste_key)
    await asyncio.sleep(0.12)
    log(f"  Đã paste prompt ({len(text)} ký tự)", "✅")


# ═══════════════════════════════════════════════
# GỬI PROMPT
# ═══════════════════════════════════════════════

async def send_prompt(page) -> bool:
    """
    Gửi prompt: click lại vào ô nhập để chắc focus, rồi nhấn Enter.
    Fallback: click nút ↑ góc phải input area.
    """
    await asyncio.sleep(0.08)

    # ── Bước 1: Đảm bảo focus vào ô nhập trước khi nhấn Enter ──
    el = await _find_prompt_element(page)
    if el:
        try:
            await el.click()
        except Exception:
            pass
    else:
        # pixel click vào giữa-dưới trang
        vp = page.viewport_size
        w = vp["width"] if vp else VP_WIDTH
        h = vp["height"] if vp else VP_HEIGHT
        await page.mouse.click(w * 0.5, h - 130)

    await asyncio.sleep(0.08)

    # ── Bước 2: Chỉ nhấn Enter để gửi (theo yêu cầu) ──
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.5)
    log("  Đã nhấn Enter gửi prompt", "✅")
    return True


# ═══════════════════════════════════════════════
# CHỜ RENDER ẢNH
# ═══════════════════════════════════════════════

async def scroll_to_load_all(page):
    """Scroll toàn trang để trigger lazy-load hết ảnh cũ."""
    try:
        await page.evaluate("""
            async () => {
                await new Promise(resolve => {
                    let last = 0;
                    const step = () => {
                        window.scrollBy(0, 600);
                        const cur = document.documentElement.scrollTop;
                        if (cur === last) { window.scrollTo(0, 0); resolve(); }
                        else { last = cur; setTimeout(step, 120); }
                    };
                    step();
                });
            }
        """)
        await asyncio.sleep(1)
    except Exception:
        pass


async def capture_stable_baseline_srcs(page, max_rounds: int = 4) -> set:
    """
    Chụp baseline ảnh cũ theo nhiều vòng để giảm nguy cơ nhầm ảnh cũ thành ảnh mới.
    Cách làm:
    1. Scroll để kích hoạt lazy-load
    2. Lấy tập src ảnh hiện có
    3. Lặp vài vòng đến khi số lượng không tăng thêm (ổn định)
    """
    baseline = set()
    prev_count = -1
    stable_rounds = 0

    for round_idx in range(1, max_rounds + 1):
        await scroll_to_load_all(page)
        await asyncio.sleep(1)
        current = await get_current_image_srcs(page)

        # Union để baseline tích lũy đầy đủ ảnh cũ đã thấy
        baseline |= current
        cur_count = len(baseline)
        log(f"Baseline round {round_idx}/{max_rounds}: {cur_count} ảnh", "DBG")

        if cur_count == prev_count:
            stable_rounds += 1
        else:
            stable_rounds = 0

        prev_count = cur_count
        # Ổn định 2 vòng liên tiếp thì dừng sớm
        if stable_rounds >= 1:
            break

    return baseline


async def get_current_image_srcs(page) -> set:
    """
    Lấy src của tất cả ảnh thực sự đã load xong trên trang.
    Dùng naturalWidth/naturalHeight thay vì getBoundingClientRect
    để bắt cả ảnh ngoài viewport.
    """
    srcs = set()
    try:
        result = await page.evaluate("""
            () => {
                const srcs = new Set();

                // 1. Thẻ <img> đã load xong
                document.querySelectorAll('img').forEach(img => {
                    const src = img.src || img.currentSrc || '';
                    if (src && img.complete && img.naturalWidth > 100 && img.naturalHeight > 100) {
                        srcs.add(src);
                    }
                });

                // 2. CSS background-image (Dreamina đôi khi dùng)
                document.querySelectorAll('[style*="background-image"]').forEach(el => {
                    const style = el.style.backgroundImage || '';
                    const match = style.match(/url\\(["']?([^"')]+)["']?\\)/);
                    if (match && match[1] && !match[1].startsWith('data:image/svg')) {
                        srcs.add(match[1]);
                    }
                });

                return Array.from(srcs);
            }
        """)
        srcs = set(result or [])
    except Exception:
        pass
    return srcs


async def get_current_image_entries(page) -> list:
    """
    Lấy danh sách ảnh theo thứ tự DOM hiện tại để giữ đúng thứ tự hiển thị.
    Mỗi phần tử gồm src + vị trí + kích thước để debug map tải ảnh.
    """
    try:
        entries = await page.evaluate("""
            () => {
                const out = [];
                const imgs = Array.from(document.querySelectorAll('img'));
                for (const img of imgs) {
                    const src = img.currentSrc || img.src || '';
                    if (!src) continue;
                    if (!img.complete) continue;
                    if ((img.naturalWidth || 0) <= 100 || (img.naturalHeight || 0) <= 100) continue;

                    const r = img.getBoundingClientRect();
                    out.push({
                        src,
                        top: Math.round(r.top),
                        left: Math.round(r.left),
                        width: img.naturalWidth || Math.round(r.width),
                        height: img.naturalHeight || Math.round(r.height),
                    });
                }
                return out;
            }
        """)
        return entries or []
    except Exception:
        return []


async def capture_prompt_submission_trace(page, prompt_index: int, prompt_text: str):
    """
    Ghi dấu vết ngay sau khi gửi prompt:
    - thời điểm gửi
    - URL + scroll hiện tại
    - trạng thái spinner/queue
    - vài src ảnh đang thấy trong DOM
    Dùng để map prompt -> bối cảnh DOM tại thời điểm submit.
    """
    ts = datetime.now().isoformat()
    try:
        dom_state = await page.evaluate("""
            () => {
                const hasSpinner = !!document.querySelector(
                    '[class*="spinner"], [class*="generating"], [class*="pending"], [class*="skeleton"]'
                );
                const queueText = Array.from(document.querySelectorAll('body *'))
                    .map(n => (n.innerText || '').trim())
                    .filter(t => t && /queue|đợi|waiting|rendering|generating/i.test(t))
                    .slice(0, 6);
                return {
                    url: location.href,
                    scrollTop: Math.round(window.scrollY || document.documentElement.scrollTop || 0),
                    hasSpinner,
                    queueHints: queueText,
                };
            }
        """)
    except Exception:
        dom_state = {"url": "", "scrollTop": 0, "hasSpinner": False, "queueHints": []}

    entries = await get_current_image_entries(page)
    sample_srcs = [e.get("src", "") for e in entries[:12] if e.get("src")]
    row = {
        "ts": ts,
        "prompt_index": prompt_index + 1,
        "prompt_preview": prompt_text[:140],
        "dom_state": dom_state,
        "mounted_images_count": len(entries),
        "sample_srcs": sample_srcs,
    }
    _prompt_submission_trace.append(row)


def save_prompt_submission_trace() -> str:
    """Lưu map prompt đã gửi để soi tương quan với ảnh trả về."""
    if not _debug_session_dir:
        return ""
    path = os.path.join(_debug_session_dir, "prompt_submission_trace.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_prompt_submission_trace, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return ""


def _sha256_file(path: str) -> str:
    """Tính SHA-256 của file ảnh để phát hiện tải trùng."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def save_download_hash_report() -> str:
    """
    Lưu hash của tất cả file đã tải + nhóm hash trùng lặp.
    Dùng để biết có bị tải trùng ảnh giữa các prompt không.
    """
    if not _debug_session_dir:
        return ""
    dup_map = {}
    for row in _download_hash_records:
        sha = row.get("sha256", "")
        if not sha:
            continue
        dup_map.setdefault(sha, []).append(row.get("filename", ""))

    payload = {
        "generated_at": datetime.now().isoformat(),
        "count": len(_download_hash_records),
        "records": _download_hash_records,
        "duplicates": {k: v for k, v in dup_map.items() if len(v) > 1},
    }
    path = os.path.join(_debug_session_dir, "download_hashes.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return ""


def _read_json_file(path: str):
    """Đọc JSON an toàn, lỗi thì trả None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _build_session_metrics(session_dir: str) -> dict:
    """
    Gom chỉ số debug chính của 1 session để phục vụ compare giữa 2 lần chạy.
    """
    gallery = _read_json_file(os.path.join(session_dir, "gallery_snapshot_before_download.json")) or {}
    hashes = _read_json_file(os.path.join(session_dir, "download_hashes.json")) or {}
    scroll = _read_json_file(os.path.join(session_dir, "scroll_trace_before_download.json")) or {}

    new_srcs = gallery.get("new_srcs", []) or []
    records = hashes.get("records", []) or []
    hash_values = [r.get("sha256", "") for r in records if r.get("sha256")]
    unique_hashes = set(hash_values)
    dup_hash_count = sum(1 for _, files in (hashes.get("duplicates", {}) or {}).items() if len(files) > 1)

    steps = scroll.get("steps", []) or []
    mounted_peaks = max([s.get("mounted_count", 0) for s in steps], default=0)
    unique_peaks = max([s.get("unique_src_count", 0) for s in steps], default=0)

    return {
        "session_dir": session_dir,
        "session_name": os.path.basename(session_dir),
        "new_srcs_count": len(new_srcs),
        "new_srcs": new_srcs,
        "download_count": len(records),
        "unique_hash_count": len(unique_hashes),
        "dup_hash_count": dup_hash_count,
        "scroll_steps": len(steps),
        "scroll_mounted_peak": mounted_peaks,
        "scroll_unique_peak": unique_peaks,
    }


def compare_with_previous_session() -> str:
    """
    So sánh session hiện tại với session gần nhất trước đó.
    Xuất cả JSON + TXT để đọc nhanh sự khác biệt.
    """
    if not _debug_session_dir:
        return ""

    sessions = sorted(glob.glob(os.path.join(DEBUG_DIR, "session_*")), key=os.path.getmtime)
    previous = [s for s in sessions if os.path.abspath(s) != os.path.abspath(_debug_session_dir)]
    if not previous:
        return ""
    prev_dir = previous[-1]

    current_metrics = _build_session_metrics(_debug_session_dir)
    prev_metrics = _build_session_metrics(prev_dir)

    cur_srcs = set(current_metrics.get("new_srcs", []))
    prev_srcs = set(prev_metrics.get("new_srcs", []))
    overlap_srcs = sorted(cur_srcs & prev_srcs)

    report = {
        "generated_at": datetime.now().isoformat(),
        "current": current_metrics,
        "previous": prev_metrics,
        "diff": {
            "new_srcs_count": current_metrics["new_srcs_count"] - prev_metrics["new_srcs_count"],
            "download_count": current_metrics["download_count"] - prev_metrics["download_count"],
            "unique_hash_count": current_metrics["unique_hash_count"] - prev_metrics["unique_hash_count"],
            "dup_hash_count": current_metrics["dup_hash_count"] - prev_metrics["dup_hash_count"],
            "scroll_mounted_peak": current_metrics["scroll_mounted_peak"] - prev_metrics["scroll_mounted_peak"],
            "scroll_unique_peak": current_metrics["scroll_unique_peak"] - prev_metrics["scroll_unique_peak"],
        },
        "overlap": {
            "new_srcs_overlap_count": len(overlap_srcs),
            "new_srcs_overlap_sample": overlap_srcs[:20],
        },
    }

    json_path = os.path.join(_debug_session_dir, "session_compare_with_previous.json")
    txt_path = os.path.join(_debug_session_dir, "session_compare_with_previous.txt")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        lines = [
            f"Current : {current_metrics['session_name']}",
            f"Previous: {prev_metrics['session_name']}",
            "",
            f"new_srcs_count : {current_metrics['new_srcs_count']} (diff {report['diff']['new_srcs_count']:+d})",
            f"download_count : {current_metrics['download_count']} (diff {report['diff']['download_count']:+d})",
            f"unique_hashes  : {current_metrics['unique_hash_count']} (diff {report['diff']['unique_hash_count']:+d})",
            f"dup_hash_count : {current_metrics['dup_hash_count']} (diff {report['diff']['dup_hash_count']:+d})",
            f"scroll_peak(m) : {current_metrics['scroll_mounted_peak']} (diff {report['diff']['scroll_mounted_peak']:+d})",
            f"scroll_peak(u) : {current_metrics['scroll_unique_peak']} (diff {report['diff']['scroll_unique_peak']:+d})",
            "",
            f"overlap new_srcs: {report['overlap']['new_srcs_overlap_count']}",
        ]
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return json_path
    except Exception:
        return ""


async def trace_scroll_behavior(page, stage: str, step_px: int = 700, max_steps: int = 24) -> str:
    """
    Trace hành vi lazy-load khi cuộn:
    - mỗi bước ghi scrollTop, số ảnh mount, số src unique
    - giúp biết ảnh có biến mất khỏi DOM khi cuộn hay không
    """
    if not _debug_session_dir:
        return ""

    rows = []
    try:
        await page.evaluate("() => window.scrollTo(0, 0)")
        await asyncio.sleep(0.4)
        for step_idx in range(max_steps):
            metrics = await page.evaluate("""
                () => ({
                    top: Math.round(window.scrollY || document.documentElement.scrollTop || 0),
                    scrollHeight: Math.round(document.documentElement.scrollHeight || 0),
                    viewport: Math.round(window.innerHeight || 0),
                })
            """)
            entries = await get_current_image_entries(page)
            srcs = [e.get("src", "") for e in entries if e.get("src")]
            rows.append({
                "step": step_idx + 1,
                "top": metrics.get("top", 0),
                "scrollHeight": metrics.get("scrollHeight", 0),
                "viewport": metrics.get("viewport", 0),
                "mounted_count": len(entries),
                "unique_src_count": len(set(srcs)),
                "first_src": srcs[0] if srcs else "",
                "last_src": srcs[-1] if srcs else "",
            })

            max_top = max(0, metrics.get("scrollHeight", 0) - metrics.get("viewport", 0))
            if metrics.get("top", 0) >= max_top:
                break
            await page.evaluate("(d) => window.scrollBy(0, d)", step_px)
            await asyncio.sleep(0.35)

        await page.evaluate("() => window.scrollTo(0, 0)")
        await asyncio.sleep(0.3)

        path = os.path.join(_debug_session_dir, f"scroll_trace_{stage}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "captured_at": datetime.now().isoformat(),
                "stage": stage,
                "steps": rows,
            }, f, ensure_ascii=False, indent=2)
        return path
    except Exception as e:
        log(f"Không ghi được scroll trace ({stage}): {e}", "WARN")
        return ""


async def is_generating(page) -> bool:
    """
    Kiểm tra trang có đang render không.
    Chỉ check các class rất đặc thù để tránh false-positive.
    """
    generating = await page.evaluate("""
        () => {
            // Chỉ tìm spinner/skeleton thực sự đang hiển thị
            const specific = [
                '[class*="skeleton"]',
                '[class*="shimmer"]',
                '[class*="spinner"]',
                'svg[class*="spin"]',
                '[class*="generating"]',
                '[class*="pending"]',
            ];
            for (const sel of specific) {
                const els = document.querySelectorAll(sel);
                for (const el of els) {
                    if (el.offsetWidth > 0 && el.offsetHeight > 0) return true;
                }
            }
            return false;
        }
    """)
    return bool(generating)


def _filter_srcs(srcs: set) -> set:
    """Lọc bỏ icon/logo/spinner."""
    return {s for s in srcs
            if not any(k in s.lower() for k in [
                "logo", "icon", "avatar", "favicon",
                "spinner", "loading", "placeholder",
            ])}


def _is_valid_generated_src(src: str) -> bool:
    """Kiểm tra src có phải ảnh generate hợp lệ để tải hay không."""
    low = src.lower()
    blocked = ["logo", "icon", "avatar", "favicon", "spinner", "loading", "placeholder"]
    return not any(k in low for k in blocked)


def _ordered_new_srcs(entries: list, before_srcs: set) -> list:
    """
    Lấy src ảnh mới theo đúng thứ tự DOM, bỏ trùng src.
    Đây là điểm quan trọng để map prompt -> ảnh ổn định hơn.
    """
    out = []
    seen = set()
    for e in entries:
        src = (e or {}).get("src", "")
        if not src:
            continue
        if src in before_srcs:
            continue
        if not _is_valid_generated_src(src):
            continue
        if src in seen:
            continue
        seen.add(src)
        out.append(src)
    return out


def save_gallery_snapshot(entries: list, new_srcs: list) -> str:
    """
    Lưu snapshot DOM gallery tại thời điểm chuẩn bị tải ảnh.
    File này giúp debug chính xác 'ảnh nào đứng trước/sau' khi tải bị lệch.
    """
    if not _debug_session_dir:
        return ""
    path = os.path.join(_debug_session_dir, "gallery_snapshot_before_download.json")
    payload = {
        "captured_at": datetime.now().isoformat(),
        "entries_count": len(entries),
        "new_srcs_count": len(new_srcs),
        "new_srcs": new_srcs,
        "entries": entries,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return ""


async def dump_detailed_dom(page, stage: str, new_srcs: Optional[list] = None) -> dict:
    """
    Dump DOM chi tiết để debug map tải ảnh:
    - Full HTML toàn trang
    - Danh sách toàn bộ img theo thứ tự DOM (index + src + class + size)
    - HTML của các container nghi ngờ là gallery/grid
    """
    if not _debug_session_dir:
        return {}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{ts}_{stage}"
    out: dict = {}

    # 1) Full HTML
    try:
        full_html = await page.content()
        full_html_path = os.path.join(_debug_session_dir, f"{base}_full.html")
        with open(full_html_path, "w", encoding="utf-8") as f:
            f.write(full_html)
        out["full_html"] = full_html_path
    except Exception as e:
        log(f"Không dump được full HTML ({stage}): {e}", "WARN")

    # 2) Danh sách ảnh theo thứ tự DOM
    images = []
    try:
        images = await page.evaluate("""
            () => {
                const rows = [];
                const all = Array.from(document.querySelectorAll('img'));
                all.forEach((img, idx) => {
                    const r = img.getBoundingClientRect();
                    rows.push({
                        idx: idx + 1,
                        src: img.currentSrc || img.src || '',
                        alt: (img.alt || '').slice(0, 120),
                        className: (img.className || '').toString().slice(0, 180),
                        complete: !!img.complete,
                        naturalWidth: img.naturalWidth || 0,
                        naturalHeight: img.naturalHeight || 0,
                        top: Math.round(r.top),
                        left: Math.round(r.left),
                        width: Math.round(r.width),
                        height: Math.round(r.height),
                    });
                });
                return rows;
            }
        """)
    except Exception:
        images = []

    try:
        img_json_path = os.path.join(_debug_session_dir, f"{base}_images.json")
        with open(img_json_path, "w", encoding="utf-8") as f:
            json.dump(images, f, ensure_ascii=False, indent=2)
        out["images_json"] = img_json_path

        # Ghi thêm text dễ đọc nhanh trong terminal/editor
        img_txt_path = os.path.join(_debug_session_dir, f"{base}_images.txt")
        new_src_set = set(new_srcs or [])
        with open(img_txt_path, "w", encoding="utf-8") as f:
            for row in images:
                src = row.get("src", "")
                mark = "[NEW]" if src in new_src_set else "     "
                f.write(
                    f"{mark} #{row.get('idx', 0):03d} "
                    f"{row.get('naturalWidth', 0)}x{row.get('naturalHeight', 0)} "
                    f"@({row.get('left', 0)},{row.get('top', 0)}) "
                    f"{src}\n"
                )
        out["images_txt"] = img_txt_path
    except Exception as e:
        log(f"Không ghi được danh sách ảnh ({stage}): {e}", "WARN")

    # 3) Dump các container khả nghi chứa gallery/grid
    try:
        containers = await page.evaluate("""
            () => {
                const selectors = [
                    '[class*="gallery"]',
                    '[class*="Gallery"]',
                    '[class*="grid"]',
                    '[class*="Grid"]',
                    '[class*="result"]',
                    '[class*="Result"]',
                    '[class*="image-list"]',
                    '[class*="ImageList"]',
                    'main',
                ];
                const out = [];
                selectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach((el, i) => {
                        const imgs = el.querySelectorAll('img').length;
                        const r = el.getBoundingClientRect();
                        if (imgs > 0 && r.width > 0 && r.height > 0) {
                            out.push({
                                selector: sel,
                                order: i + 1,
                                imgCount: imgs,
                                top: Math.round(r.top),
                                left: Math.round(r.left),
                                width: Math.round(r.width),
                                height: Math.round(r.height),
                                html: el.outerHTML,
                            });
                        }
                    });
                });
                out.sort((a, b) => b.imgCount - a.imgCount);
                return out.slice(0, 8);
            }
        """)

        meta_rows = []
        for i, c in enumerate(containers or [], start=1):
            c_path = os.path.join(_debug_session_dir, f"{base}_gallery_{i:02d}.html")
            with open(c_path, "w", encoding="utf-8") as f:
                f.write(c.get("html", ""))
            meta = {
                "file": os.path.basename(c_path),
                "selector": c.get("selector", ""),
                "order": c.get("order", 0),
                "imgCount": c.get("imgCount", 0),
                "left": c.get("left", 0),
                "top": c.get("top", 0),
                "width": c.get("width", 0),
                "height": c.get("height", 0),
            }
            meta_rows.append(meta)

        meta_path = os.path.join(_debug_session_dir, f"{base}_gallery_meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta_rows, f, ensure_ascii=False, indent=2)
        out["gallery_meta"] = meta_path
    except Exception as e:
        log(f"Không dump được gallery HTML ({stage}): {e}", "WARN")

    return out


async def diagnose_dom_virtualization(page, stage: str) -> str:
    """
    Chẩn đoán hiện tượng virtualized list:
    - Đo ở 3 vị trí cuộn: top/mid/bottom
    - Mỗi vị trí ghi số img đang mount + danh sách src
    => Giúp biết ảnh ở trên có bị unmount khỏi DOM khi cuộn xuống hay không.
    """
    if not _debug_session_dir:
        return ""

    report = {
        "captured_at": datetime.now().isoformat(),
        "stage": stage,
        "positions": [],
    }

    try:
        metrics = await page.evaluate("""
            () => ({
                scrollHeight: document.documentElement.scrollHeight || 0,
                viewport: window.innerHeight || 0,
            })
        """)
        scroll_height = int((metrics or {}).get("scrollHeight", 0))
        viewport = int((metrics or {}).get("viewport", 1))
        max_top = max(0, scroll_height - viewport)
        checkpoints = [
            ("top", 0),
            ("mid", max_top // 2),
            ("bottom", max_top),
        ]

        for name, y in checkpoints:
            await page.evaluate("(top) => window.scrollTo(0, top)", y)
            await asyncio.sleep(0.6)
            entries = await get_current_image_entries(page)
            srcs = [e.get("src", "") for e in entries if e.get("src")]
            report["positions"].append({
                "name": name,
                "scroll_top": y,
                "mounted_img_count": len(entries),
                "unique_src_count": len(set(srcs)),
                "srcs": srcs,
            })

        # Quay lại top để không ảnh hưởng thao tác sau
        await page.evaluate("() => window.scrollTo(0, 0)")
        await asyncio.sleep(0.3)

        path = os.path.join(_debug_session_dir, f"dom_virtualization_{stage}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return path
    except Exception as e:
        log(f"Không chạy được chẩn đoán virtualization ({stage}): {e}", "WARN")
        return ""


async def wait_for_all_images(page, before_srcs: set,
                               expected: int, timeout: int) -> list:
    """
    Chờ đến khi:
      - Không còn loading/generating trên trang
      - Số ảnh mới ổn định trong 10s liên tiếp
    expected: số prompt đã gửi (dùng để log tiến độ)
    """
    log(f"Chờ render tất cả {expected} prompt...", "⏳")
    start        = time.time()
    stable_count = 0
    last_count   = 0

    # Chờ bắt đầu generate (tối đa 15s)
    for _ in range(8):
        if await is_generating(page):
            break
        await asyncio.sleep(2)

    while time.time() - start < timeout:
        await asyncio.sleep(3)

        current  = await get_current_image_srcs(page)
        new_srcs = _filter_srcs(current - before_srcs)
        still_running = await is_generating(page)

        if len(new_srcs) > last_count:
            last_count   = len(new_srcs)
            stable_count = 0
            log(f"  Đang render... ({len(new_srcs)} ảnh mới)", "🖼️")
        else:
            stable_count += 1

        # Ổn định 12s → xong (không bắt buộc is_generating=False
        # để tránh bị kẹt do false-positive)
        if new_srcs and stable_count >= 4:
            if not still_running or stable_count >= 6:
                log(f"  Xong! {len(new_srcs)} ảnh", "✅")
                return list(new_srcs)

        elapsed = int(time.time() - start)
        if elapsed > 0 and elapsed % 20 == 0:
            status = "render" if still_running else "ổn định"
            log(f"  [{status}] {elapsed}s — {len(new_srcs)} ảnh mới", "⏳")

    log(f"  Timeout — lấy toàn bộ ảnh hiện có", "⚠️")
    current  = await get_current_image_srcs(page)
    return list(_filter_srcs(current - before_srcs))


# ═══════════════════════════════════════════════
# TẢI ẢNH
# ═══════════════════════════════════════════════

async def download_images(page, new_srcs: list, prompt_index: int, prompt_text: str) -> int:
    saved = 0
    safe  = safe_filename(prompt_text)

    for idx, src in enumerate(new_srcs):
        filepath = os.path.join(OUTPUT_DIR, f"p{prompt_index+1:02d}_{safe}_img{idx+1}.png")
        try:
            if src.startswith("http"):
                data = await page.evaluate("""
                    async (src) => {
                        try {
                            const r = await fetch(src);
                            const b = await r.blob();
                            return await new Promise(res => {
                                const reader = new FileReader();
                                reader.onloadend = () => res(reader.result);
                                reader.readAsDataURL(b);
                            });
                        } catch { return null; }
                    }
                """, src)

                if data and data.startswith("data:"):
                    raw = base64.b64decode(data.split(",", 1)[1])
                    with open(filepath, "wb") as f:
                        f.write(raw)
                    if os.path.getsize(filepath) > 5000:
                        log(f"  Đã lưu: {os.path.basename(filepath)} ({os.path.getsize(filepath)//1024}KB)", "✅")
                        saved += 1
                        continue

            # Fallback: canvas
            data = await page.evaluate("""
                (src) => {
                    const img = document.querySelector(`img[src="${src}"]`);
                    if (!img) return null;
                    const c = document.createElement('canvas');
                    c.width = img.naturalWidth || img.width;
                    c.height = img.naturalHeight || img.height;
                    c.getContext('2d').drawImage(img, 0, 0);
                    return c.toDataURL('image/png');
                }
            """, src)
            if data and data.startswith("data:"):
                raw = base64.b64decode(data.split(",", 1)[1])
                with open(filepath, "wb") as f:
                    f.write(raw)
                if os.path.getsize(filepath) > 5000:
                    log(f"  Đã lưu (canvas): {os.path.basename(filepath)}", "✅")
                    saved += 1

        except Exception as e:
            log(f"  Lỗi tải ảnh #{idx+1}: {e}", "⚠️")

    return saved


async def ensure_google_flow_editor(page, timeout_sec: int = 30) -> bool:
    """
    Đảm bảo đã vào màn editor của Google Flow (có ô nhập prompt).
    Nếu đang ở dashboard, thử bấm "Dự án mới"/"New project"/"Tạo dự án".
    """
    selectors = ["div[contenteditable='true']", "div[role='textbox']", "textarea"]
    start = time.time()

    while time.time() - start < timeout_sec:
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible(timeout=300):
                    return True
            except Exception:
                pass

        if int(time.time() - start) % 3 == 0:
            try:
                patt = re.compile(r"(dự án mới|new project|tạo dự án|create project)", re.IGNORECASE)
                btn = page.get_by_text(patt).first
                if await btn.count() > 0 and await btn.is_visible():
                    log("Đang click nút vào project mới của Google Flow...", "NAV")
                    await btn.click()
                    await asyncio.sleep(1.5)
            except Exception:
                pass

        await asyncio.sleep(1.0)

    return False


async def open_google_flow_new_project(page, timeout_sec: int = 45) -> bool:
    """
    Ép tạo project mới trên Google Flow để không dùng lại project cũ.
    Trả về True khi URL đã chuyển sang /project/... và có ô nhập prompt.
    """
    target_url = get_target_home_url()
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(1.5)
    except Exception:
        pass

    # Nếu đã ở project URL, quay về dashboard trước khi tạo project mới.
    try:
        if "/project/" in (page.url or ""):
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1.2)
    except Exception:
        pass

    start = time.time()
    new_project_patterns = [
        re.compile(r"dự án mới", re.IGNORECASE),
        re.compile(r"new project", re.IGNORECASE),
        re.compile(r"create project", re.IGNORECASE),
        re.compile(r"tạo dự án", re.IGNORECASE),
    ]

    while time.time() - start < timeout_sec:
        for patt in new_project_patterns:
            try:
                btn = page.get_by_text(patt).first
                if await btn.count() > 0 and await btn.is_visible():
                    log("Đang tạo project Flow mới...", "NAV")
                    await btn.click()
                    await asyncio.sleep(2.0)
                    # Chờ URL project mới
                    if "/project/" in (page.url or ""):
                        ready = await ensure_google_flow_editor(page, timeout_sec=15)
                        if ready:
                            log(f"Đã vào project mới: {page.url}", "OK")
                            return True
            except Exception:
                pass

        # Fallback click theo icon/button có chữ add_2 + label tạo project
        try:
            btn = page.locator("button").filter(
                has_text=re.compile(r"(add_2).*(dự án mới|new project|create project|tạo dự án)", re.IGNORECASE)
            ).first
            if await btn.count() > 0 and await btn.is_visible():
                log("Đang tạo project Flow mới (fallback button)...", "NAV")
                await btn.click()
                await asyncio.sleep(2.0)
                if "/project/" in (page.url or ""):
                    ready = await ensure_google_flow_editor(page, timeout_sec=15)
                    if ready:
                        log(f"Đã vào project mới: {page.url}", "OK")
                        return True
        except Exception:
            pass

        await asyncio.sleep(1.0)

    return False


def _flow_human_delay_after_send(base_delay_sec: float) -> float:
    """
    Tính delay sau khi gửi prompt theo kiểu "human-like" nhưng bảo thủ.

    Mục tiêu:
    - Không gửi đều tuyệt đối như máy.
    - Không làm tăng tổng thời gian quá nhiều.
    """
    # Fallback an toàn: giữ đúng delay gốc như hiện tại.
    base = max(0.2, float(base_delay_sec))
    if not FLOW_HUMANIZE_ENABLED:
        return base

    # Clamp cấu hình để tránh nhập nhầm làm delay quá lớn/nhỏ.
    jitter_min = min(max(FLOW_SEND_JITTER_MIN, 0.3), 3.0)
    jitter_max = min(max(FLOW_SEND_JITTER_MAX, jitter_min), 3.5)

    delay = base * _flow_human_rng.uniform(jitter_min, jitter_max)

    # Thỉnh thoảng chèn pause ngắn để nhịp không quá đều.
    pause_prob = min(max(FLOW_SOFT_PAUSE_PROB, 0.0), 0.8)
    if _flow_human_rng.random() < pause_prob:
        pause_min = min(max(FLOW_SOFT_PAUSE_MIN_SEC, 0.0), 30.0)
        pause_max = min(max(FLOW_SOFT_PAUSE_MAX_SEC, pause_min), 45.0)
        delay += _flow_human_rng.uniform(pause_min, pause_max)

    # Giới hạn trần bảo thủ để không đội thời gian quá nhiều do config lỗi.
    return min(delay, max(6.0, base * 4.0))


def _flow_human_video_poll_interval() -> float:
    """
    Tính chu kỳ poll video có dao động nhẹ quanh mốc 10s.
    Dao động nhỏ giúp nhịp request tự nhiên hơn mà không ảnh hưởng nhiều SLA.
    """
    base = max(3.0, FLOW_VIDEO_POLL_BASE_SEC)
    if not FLOW_HUMANIZE_ENABLED:
        return base

    jitter = min(max(FLOW_VIDEO_POLL_JITTER_SEC, 0.0), 5.0)
    low = max(2.0, base - jitter)
    high = max(low, base + jitter)
    return _flow_human_rng.uniform(low, high)


def _pick_flow_video_send_interval_sec(stall_rounds_without_ready: int) -> float:
    """
    Chọn khoảng chờ trước khi gửi cảnh video tiếp theo.

    Luật:
    - Bình thường: 60-90s.
    - Nếu nhiều vòng liền không có cảnh READY: tăng lên 90-150s.
    """
    use_slow = stall_rounds_without_ready >= max(1, FLOW_VIDEO_READY_STALL_ROUNDS_FOR_SLOW)
    if use_slow:
        low = min(FLOW_VIDEO_SEND_INTERVAL_SLOW_MIN_SEC, FLOW_VIDEO_SEND_INTERVAL_SLOW_MAX_SEC)
        high = max(FLOW_VIDEO_SEND_INTERVAL_SLOW_MIN_SEC, FLOW_VIDEO_SEND_INTERVAL_SLOW_MAX_SEC)
    else:
        low = min(FLOW_VIDEO_SEND_INTERVAL_FAST_MIN_SEC, FLOW_VIDEO_SEND_INTERVAL_FAST_MAX_SEC)
        high = max(FLOW_VIDEO_SEND_INTERVAL_FAST_MIN_SEC, FLOW_VIDEO_SEND_INTERVAL_FAST_MAX_SEC)
    low = max(5.0, low)
    high = max(low, high)
    return _flow_human_rng.uniform(low, high)


def _pick_flow_video_download_delay_sec() -> float:
    """
    Delay ngẫu nhiên nhẹ trước lúc tải video READY để giảm pattern bot.
    """
    low = min(FLOW_VIDEO_DOWNLOAD_DELAY_MIN_SEC, FLOW_VIDEO_DOWNLOAD_DELAY_MAX_SEC)
    high = max(FLOW_VIDEO_DOWNLOAD_DELAY_MIN_SEC, FLOW_VIDEO_DOWNLOAD_DELAY_MAX_SEC)
    low = max(0.5, low)
    high = max(low, high)
    return _flow_human_rng.uniform(low, high)


def _jitter_seconds(base_sec: float, low: float = 0.85, high: float = 1.2, floor: float = 0.8) -> float:
    """
    Jitter thời gian chờ/cooldown để tránh pattern cố định tuyệt đối.
    """
    b = max(float(floor), float(base_sec))
    lo = min(max(low, 0.5), 1.0)
    hi = max(high, lo)
    return max(float(floor), _flow_human_rng.uniform(b * lo, b * hi))


async def run_google_flow_auto_request_response(page, prompts: list[str]) -> int:
    """
    Auto mode cho Google Flow:
    - Tự nhập prompt + gửi.
    - Tải ảnh bằng URL bắt từ API response (KHÔNG tải theo DOM).
    Trả về tổng số ảnh tải thành công.
    """
    global _run_started_ts
    saved_total = 0

    target_url = get_target_home_url()
    log(f"Đang mở Google Flow: {target_url}", "WEB")
    await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)

    if GOOGLE_FLOW_FORCE_NEW_PROJECT:
        created = await open_google_flow_new_project(page, timeout_sec=45)
        if not created:
            log("Không ép tạo được project mới, fallback dùng editor hiện tại.", "WARN")
            editor_ready = await ensure_google_flow_editor(page, timeout_sec=35)
        else:
            editor_ready = True
    else:
        editor_ready = await ensure_google_flow_editor(page, timeout_sec=35)

    if not editor_ready:
        log("Không vào được editor Google Flow (chưa thấy ô nhập prompt).", "ERR")
        return 0

    # Giống Dreamina: cho user setup model/tỷ lệ/chất lượng trước khi gửi prompt.
    if GOOGLE_FLOW_WAIT_FOR_READY_ENTER:
        print("\n" + "=" * 60)
        print("  GOOGLE FLOW READY CHECK")
        print("  Bạn hãy setup model / ratio / style trên browser trước.")
        print("=" * 60)
        await asyncio.get_event_loop().run_in_executor(
            None, input, "  Setup xong -> Nhấn Enter để BẮT ĐẦU gửi prompt: "
        )

    await debug_step(page, "google_flow_editor_ready", extra={"url": page.url, "prompts": len(prompts)})

    # Snapshot baseline tương tự Dreamina để đối chiếu ảnh cũ/mới.
    baseline_scroll_trace = await trace_scroll_behavior(page, "flow_before_send_prompts")
    if baseline_scroll_trace:
        log(f"Flow baseline scroll trace: {baseline_scroll_trace}", "DBG")
    before_all = await capture_stable_baseline_srcs(page, max_rounds=4)
    log(f"Flow baseline ổn định: {len(before_all)} ảnh cũ", "DBG")
    await debug_step(page, "flow_before_send_prompts", extra={
        "prompts_count": len(prompts),
        "old_images": len(before_all),
    })

    _run_started_ts = time.time()

    for i, prompt in enumerate(prompts):
        scene_no = extract_scene_number(prompt, i + 1)
        log(f"[FLOW {i+1}/{len(prompts)}] Gửi: {prompt[:70]}", "SEND")
        try:
            await type_prompt(page, prompt)
            sent = await send_prompt(page)
            await debug_step(
                page,
                f"flow_sent_prompt_{i+1:02d}",
                job_id=f"canh_{scene_no:03d}",
                extra={"sent_ok": sent, "prompt_preview": prompt[:90]},
            )
            await capture_prompt_submission_trace(page, i, prompt)
        except Exception as e:
            log(f"Lỗi gửi prompt #{i+1}: {e}", "WARN")
        # Delay sau mỗi prompt có dao động nhẹ để tránh nhịp gửi quá đều.
        # Vẫn neo quanh DELAY_SEC để giữ tổng thời gian gần mức cũ.
        await asyncio.sleep(_flow_human_delay_after_send(DELAY_SEC))

    log(f"Đã gửi xong {len(prompts)} prompt, chờ {GOOGLE_FLOW_WAIT_AFTER_LAST_PROMPT_SEC}s...", "WAIT")
    for remaining in range(GOOGLE_FLOW_WAIT_AFTER_LAST_PROMPT_SEC, 0, -1):
        if remaining % 5 == 0:
            log(f"  Còn {remaining}s...", "WAIT")
        await asyncio.sleep(1)

    # Debug sau chờ render + dump DOM chi tiết.
    await debug_step(page, "flow_after_wait_render", extra={
        "waited_sec": GOOGLE_FLOW_WAIT_AFTER_LAST_PROMPT_SEC,
    })
    after_render_dump = await dump_detailed_dom(page, "flow_after_wait_render")
    if after_render_dump:
        log(
            "Flow DOM dump after_wait_render: "
            + ", ".join(os.path.basename(p) for p in after_render_dump.values()),
            "DBG",
        )

    # Snapshot gallery trước tải để phục vụ compare/report như Dreamina.
    await scroll_to_load_all(page)
    await asyncio.sleep(1.5)
    before_download_scroll_trace = await trace_scroll_behavior(page, "flow_before_download")
    if before_download_scroll_trace:
        log(f"Flow before_download scroll trace: {before_download_scroll_trace}", "DBG")
    gallery_entries = await get_current_image_entries(page)
    all_new = _ordered_new_srcs(gallery_entries, before_all)
    gallery_snapshot_path = save_gallery_snapshot(gallery_entries, all_new)
    if gallery_snapshot_path:
        log(f"Flow gallery snapshot: {gallery_snapshot_path}", "DBG")
    virtualization_report = await diagnose_dom_virtualization(page, "flow_before_download")
    if virtualization_report:
        log(f"Flow virtualization report: {virtualization_report}", "DBG")
    before_download_dump = await dump_detailed_dom(page, "flow_before_download", new_srcs=all_new)
    if before_download_dump:
        log(
            "Flow DOM dump before_download: "
            + ", ".join(os.path.basename(p) for p in before_download_dump.values()),
            "DBG",
        )
    await debug_step(page, "flow_before_download", extra={
        "new_images_found": len(all_new),
        "gallery_entries": len(gallery_entries),
        "expected": len(prompts),
        "snapshot_file": os.path.basename(gallery_snapshot_path) if gallery_snapshot_path else "",
        "virtualization": os.path.basename(virtualization_report) if virtualization_report else "",
        "dom_dump_images": os.path.basename(before_download_dump.get("images_txt", "")) if before_download_dump else "",
    })

    await wait_pending_api_tasks(timeout_sec=3.0)
    api_scene_map, _ = await build_api_scene_map_with_retry(
        prompts,
        timeout_sec=GOOGLE_FLOW_API_MAP_TIMEOUT_SEC,
        interval_sec=2,
    )
    api_scene_map = apply_event_order_fallback_scene_map(prompts, api_scene_map)
    prompt_scene_order = [extract_scene_number(p, i + 1) for i, p in enumerate(prompts)]
    missing_scenes = [sc for sc in prompt_scene_order if not api_scene_map.get(sc)]

    log(
        f"Google Flow API map: {len(api_scene_map)}/{len(prompts)} cảnh"
        + (f" | thiếu: {missing_scenes}" if missing_scenes else ""),
        "DBG",
    )

    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    saved_scenes = set()
    for scene_no in prompt_scene_order:
        candidate_urls = get_scene_candidate_urls(scene_no, api_scene_map.get(scene_no, ""))
        if not candidate_urls:
            log(f"  Thiếu URL API cho canh_{scene_no:03d}", "WARN")
            continue

        fname = f"canh_{scene_no:03d}.png"
        filepath = os.path.join(OUTPUT_DIR, fname)
        downloaded = False
        for src in candidate_urls:
            try:
                resp = await page.context.request.get(src, timeout=30000)
                if not resp.ok:
                    continue
                body = await resp.body()
                if len(body) <= 5000:
                    continue
                with open(filepath, "wb") as f:
                    f.write(body)
                log(f"  {fname} ({len(body)//1024}KB) [flow-api-map]", "OK")
                try:
                    sha = _sha256_file(filepath)
                except Exception:
                    sha = ""
                _download_hash_records.append({
                    "filename": fname,
                    "prompt_num": scene_no,
                    "prompt_index": scene_no,
                    "img_num": 1,
                    "src": src,
                    "method": "request.get_flow_api_map",
                    "size_bytes": len(body),
                    "sha256": sha,
                })
                saved_total += 1
                saved_scenes.add(scene_no)
                downloaded = True
                break
            except Exception:
                continue
        if not downloaded:
            log(f"  Không tải được canh_{scene_no:03d} theo API", "WARN")

    missing_after_download = [sc for sc in prompt_scene_order if sc not in saved_scenes]
    if missing_after_download:
        log(f"Flow cảnh chưa tải được: {missing_after_download}", "WARN")

    # Tự động upscale 2K:
    # - Script tự click UI để Flow tạo request hợp lệ.
    # - File 2K vẫn lấy từ response API (encodedImage), không tải theo DOM.
    if GOOGLE_FLOW_AUTO_UPSCALE_2K:
        upscaled = await auto_upscale_2k_by_api(page, prompt_scene_order)
        log(f"Đã upscale 2K thành công {upscaled}/{len(prompt_scene_order)} ảnh", "INFO")

    return saved_total


async def switch_google_flow_to_video_mode(page) -> bool:
    """
    Chuyển UI Flow sang chế độ tạo video.
    Trả về True nếu click thành công một trong các nút video mode.
    """
    patterns = [
        re.compile(r"trình tạo cảnh", re.IGNORECASE),
        re.compile(r"play_movies", re.IGNORECASE),
        re.compile(r"create video", re.IGNORECASE),
        re.compile(r"tạo video", re.IGNORECASE),
    ]
    for patt in patterns:
        try:
            btn = page.locator("button").filter(has_text=patt).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(0.8)
                return True
        except Exception:
            pass
    return False


async def send_video_prompt(page) -> bool:
    """
    Gửi prompt ở chế độ video:
    - ưu tiên nút Create video/Tạo video
    - fallback Enter
    """
    patterns = [
        re.compile(r"create video", re.IGNORECASE),
        re.compile(r"tạo video", re.IGNORECASE),
        re.compile(r"create videos", re.IGNORECASE),
    ]
    for patt in patterns:
        try:
            btn = page.locator("button").filter(has_text=patt).first
            if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                await btn.click()
                await asyncio.sleep(0.8)
                return True
        except Exception:
            pass

    # fallback dùng logic send chung
    return await send_prompt(page)


def _normalize_reference_token(raw: str) -> str:
    """Chuẩn hóa token về dạng UPPER_SNAKE để so khớp tên file."""
    text = str(raw or "").strip().upper()
    text = re.sub(r"[^A-Z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def _extract_reference_tokens_from_video_prompt(prompt_text: str) -> list:
    """
    Tách danh sách token reference (character1/image1...) từ text prompt video.
    Ví dụ: 'character1 đứng bên cạnh image1' -> ['CHARACTER1', 'IMAGE1']
    """
    text = str(prompt_text or "")
    out = []
    seen = set()
    patterns = [
        r"\b(character\d+)\b",
        r"\b(image\d+)\b",
        r"\b(nhan_vat_\d+)\b",
        r"\b(boi_canh_\d+)\b",
    ]
    for patt in patterns:
        for m in re.finditer(patt, text, flags=re.IGNORECASE):
            tok = _normalize_reference_token(m.group(1))
            if tok and tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def _collect_required_reference_tokens(prompts: list[str]) -> list[str]:
    """
    Gom tất cả token reference xuất hiện trong toàn bộ danh sách prompt video.
    Dùng để kiểm tra "đủ file reference" trước khi chạy preload/upload.
    """
    required: list[str] = []
    seen = set()
    for prompt in prompts or []:
        for token in _extract_reference_tokens_from_video_prompt(prompt):
            if token not in seen:
                seen.add(token)
                required.append(token)
    return required


def _find_reference_path_by_token_local(reference_dir: str, token: str) -> str:
    """
    Tìm file ảnh local theo token chuẩn hóa.
    Ví dụ: CHARACTER1 -> output_images/character1.png
    """
    token_norm = _normalize_reference_token(token)
    if not token_norm:
        return ""
    for ext in [".png", ".jpg", ".jpeg", ".webp"]:
        for candidate in [token_norm, token_norm.lower()]:
            p = os.path.join(reference_dir, candidate + ext)
            if os.path.exists(p):
                return os.path.abspath(p)
    return ""


def _count_existing_scene_video_files(scene_no: int) -> int:
    """
    Đếm số file video đã có của 1 cảnh để đặt tên file mới không bị đè/trùng.
    """
    patt = os.path.join(OUTPUT_DIR, f"canh_{scene_no:03d}*.mp4")
    return len(glob.glob(patt))


async def _send_one_flow_video_scene_prompt(
    page,
    prompt: str,
    scene_no: int,
    prompt_index: int,
    total_prompts: int,
) -> bool:
    """
    Gửi một prompt video cho một cảnh:
    - Attach reference theo token trong prompt.
    - Chỉ gửi 1 lần, không reload.
    """
    global _last_scene_reference_attach_failed
    _last_scene_reference_attach_failed = False

    if GOOGLE_FLOW_VIDEO_USE_REFERENCE_IMAGES:
        # Xóa reference cũ để tránh trộn sai cảnh.
        clear_info = await clear_reference_attachments_in_composer(
            page,
            focus_prompt_cb=lambda: find_and_focus_prompt(page),
            max_rounds=2,
        )
        if clear_info.get("before", 0) > 0 and not clear_info.get("cleared"):
            log(
                f"  Cảnh báo: chưa xóa sạch reference cũ (before={clear_info.get('before')}, "
                f"after={clear_info.get('after')})",
                "WARN",
            )

        # Attach token reference theo prompt.
        ref_tokens = _extract_reference_tokens_from_video_prompt(prompt)
        if ref_tokens:
            max_refs = max(1, int(GOOGLE_FLOW_VIDEO_MAX_REFERENCES_PER_PROMPT))
            original_tokens = ref_tokens[:]
            ref_tokens = ref_tokens[:max_refs]
            dropped_tokens = original_tokens[max_refs:]
            log(f"  cảnh_{scene_no:03d}: tokens reference: {original_tokens}", "DBG")
            if dropped_tokens:
                log(
                    f"  cảnh_{scene_no:03d}: vượt giới hạn {max_refs} reference/prompt, "
                    f"bỏ qua token dư: {dropped_tokens}",
                    "WARN",
                )
                # Chế độ strict: prompt yêu cầu bao nhiêu token thì phải attach đủ bấy nhiêu.
                # Nếu bị cắt token do giới hạn thì coi như attach fail.
                if GOOGLE_FLOW_VIDEO_REQUIRE_REFERENCE_UPLOAD:
                    _last_scene_reference_attach_failed = True
                    log(
                        f"  cảnh_{scene_no:03d}: strict reference bật -> fail vì token vượt giới hạn attach.",
                        "ERR",
                    )
                    return False
            attach_ok_all = True
            for token in ref_tokens:
                token_lower = token.lower()
                token_path = _find_reference_path_by_token_local(GOOGLE_FLOW_VIDEO_REFERENCE_DIR, token)
                token_ok = False
                ref_dbg = {}
                if GOOGLE_FLOW_VIDEO_REFERENCE_MODE == "library_search":
                    token_ok, ref_dbg = await attach_reference_from_library_by_name(
                        page,
                        search_name=token_lower,
                        vp_height=VP_HEIGHT,
                    )
                elif token_path:
                    token_ok, ref_dbg = await upload_reference_image_for_video(
                        page,
                        image_path=token_path,
                        allow_direct_file_input=GOOGLE_FLOW_VIDEO_ALLOW_DIRECT_FILE_INPUT,
                        verify_fn=verify_reference_image_attached,
                        log_cb=log,
                    )
                else:
                    ref_dbg = {"error": "reference_file_not_found_for_token", "token": token}
                log(
                    f"  cảnh_{scene_no:03d}: token={token_lower} -> "
                    f"{'attached ✅' if token_ok else 'FAIL ❌'} | {ref_dbg.get('error', '')}",
                    "OK" if token_ok else "WARN",
                )
                attach_ok_all = attach_ok_all and token_ok

            # Nếu bật strict reference thì bỏ cảnh khi attach fail.
            if not attach_ok_all and GOOGLE_FLOW_VIDEO_REQUIRE_REFERENCE_UPLOAD:
                log(f"  cảnh_{scene_no:03d}: Bỏ qua vì attach reference thất bại.", "WARN")
                _last_scene_reference_attach_failed = True
                return False
        else:
            log(f"  cảnh_{scene_no:03d}: Không tìm được token reference trong prompt.", "DBG")

    # Delay ngắn trước khi gửi để thao tác đỡ cứng.
    await asyncio.sleep(_flow_human_delay_after_send(FLOW_VIDEO_PRE_SEND_BASE_SEC))

    log(f"[FLOW-VIDEO {prompt_index+1}/{total_prompts}] Gửi prompt: {prompt[:70]}", "SEND")
    sent = False
    try:
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.5)
        except Exception:
            pass

        await type_prompt(page, prompt)
        sent = await send_video_prompt(page)
        await debug_step(
            page,
            f"flow_video_sent_prompt_{prompt_index+1:02d}",
            job_id=f"canh_{scene_no:03d}",
            extra={"sent_ok": sent, "prompt_preview": prompt[:90]},
        )
        await capture_prompt_submission_trace(page, prompt_index, prompt)
        if sent:
            _video_sent_scene_history.append(scene_no)
            _video_scene_sent_ts[scene_no] = time.time()
    except Exception as e:
        log(f"Lỗi gửi prompt video #{prompt_index+1}: {e}", "WARN")
    return sent


async def _download_ready_videos_for_scene(
    page,
    scene_no: int,
    downloaded_media_ids_global: set[str],
) -> tuple[str, int]:
    """
    Probe + tải video cho một cảnh bằng API-only (không phụ thuộc UI).
    - Có mediaId là có thể probe redirect/download.
    - READY được ưu tiên probe trước, nhưng PENDING cũng được probe theo nhịp giới hạn.

    Return:
    - ("downloaded", n): tải thành công n video
    - ("not_ready", 0): chưa tải được ở vòng này
    - ("failed", 0): tất cả media của cảnh đã FAILED
    - ("cooldown", 0): gặp rate-limit rõ ràng, cần cooldown
    """
    await wait_pending_api_tasks(timeout_sec=3.0)
    ui_errors = await capture_flow_ui_error_messages(page, f"before_download_canh{scene_no:03d}")
    if _has_rate_limit_ui_error(ui_errors):
        log(f"  canh_{scene_no:03d}: phát hiện rate-limit trước khi tải.", "WARN")
        return "cooldown", 0
    if _has_unusual_activity_ui_error(ui_errors):
        # Theo policy mới: unusual activity KHÔNG cooldown tại nhánh download.
        # Scheduler chính sẽ tự đếm attempt/reload ở vòng kế tiếp.
        log(f"  canh_{scene_no:03d}: phát hiện unusual activity trước khi tải (skip, không cooldown).", "WARN")
        return "not_ready", 0
    has_audiovisual_ui_error = _has_audiovisual_load_ui_error(ui_errors)

    candidate_ids = get_scene_candidate_video_media_ids(scene_no)
    if not candidate_ids:
        return "not_ready", 0

    ready_ids = []
    pending_ids = []
    for mid in candidate_ids:
        if mid in downloaded_media_ids_global:
            continue
        if mid in _video_media_terminal_skip:
            continue
        status = _video_media_status_by_id.get(mid, "")
        if _is_video_media_ready_status(status):
            ready_ids.append(mid)
        elif not _is_video_media_failed_status(status):
            pending_ids.append(mid)

    if candidate_ids and all(_is_video_media_failed_status(_video_media_status_by_id.get(mid, "")) for mid in candidate_ids):
        log(f"  canh_{scene_no:03d}: tất cả media đều FAILED.", "WARN")
        return "failed", 0

    # Bug fix: Chỉ duyệt tải những media đã READY, không trộn lẫn PENDING để gọi redirect sớm
    probe_ids = ready_ids
    if not probe_ids:
        return "not_ready", 0
    probe_limit = max(1, FLOW_VIDEO_PROBE_PER_SCENE_PER_ROUND)
    probe_ids = probe_ids[:probe_limit]

    # Không tải ngay lập tức sau vòng poll để giảm pattern bot.
    dl_delay = _pick_flow_video_download_delay_sec()
    ready_count = len(ready_ids)
    pending_count = len(pending_ids)
    log(
        f"  canh_{scene_no:03d}: probe {len(probe_ids)} media (ready={ready_count}, pending={pending_count}) "
        f"-> đợi {dl_delay:.1f}s rồi thử tải...",
        "WAIT",
    )
    await asyncio.sleep(dl_delay)

    downloaded_count = 0
    for media_id in probe_ids:
        if media_id in downloaded_media_ids_global:
            continue

        media_status = _video_media_status_by_id.get(media_id, "")
        is_ready_now = _is_video_media_ready_status(media_status)
        now_ts = time.time()
        if not is_ready_now:
            last_probe_ts = float(_video_media_last_probe_ts.get(media_id, 0.0) or 0.0)
            min_interval = max(5.0, FLOW_VIDEO_PENDING_PROBE_MIN_INTERVAL_SEC)
            if last_probe_ts > 0 and (now_ts - last_probe_ts) < min_interval:
                continue
        _video_media_last_probe_ts[media_id] = now_ts

        redirect_url = f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={media_id}"
        # Đặt tên file chắc chắn không trùng/đè.
        variant_index = _count_existing_scene_video_files(scene_no) + 1
        fpath = _build_scene_video_output_path(scene_no, variant_index)

        success = False
        # READY: cho retry nhiều hơn. PENDING: chỉ probe nhẹ 1 vòng.
        attempt_total = 3 if is_ready_now else 1
        for attempt in range(attempt_total):
            try:
                resp = await page.context.request.get(redirect_url, timeout=30000, max_redirects=0)
                location = str((resp.headers or {}).get("location", "")).strip()
                if resp.status in (301, 302, 307, 308) and location:
                    video_resp = await page.context.request.get(location, timeout=60000)
                    if not video_resp.ok:
                        _mark_video_media_probe_fail(media_id, reason=f"gcs_not_ok_{int(video_resp.status)}")
                        await asyncio.sleep(1.2)
                        continue
                    body = await video_resp.body()
                    if len(body) < max(50000, FLOW_VIDEO_MIN_VALID_BYTES):
                        # Có thể vẫn chưa fully ready ở storage.
                        _video_download_events.append({
                            "ts": time.time(),
                            "scene_no": scene_no,
                            "attempt": attempt + 1,
                            "media_id": media_id,
                            "media_status": media_status,
                            "phase": "probe_small_body_scheduler",
                            "redirect_status": int(resp.status),
                            "gcs_status": int(video_resp.status),
                            "body_size": len(body),
                        })
                        _mark_video_media_probe_fail(media_id, reason=f"small_body_{len(body)}")
                        await asyncio.sleep(1.2)
                        continue
                    with open(fpath, "wb") as f:
                        f.write(body)
                    try:
                        sha = _sha256_file(fpath)
                    except Exception:
                        sha = ""
                    _download_hash_records.append({
                        "filename": os.path.basename(fpath),
                        "prompt_num": scene_no,
                        "prompt_index": scene_no,
                        "img_num": variant_index,
                        "src": location,
                        "media_id": media_id,
                        "media_status": media_status,
                        "method": "request.get_flow_video_307_redirect",
                        "size_bytes": len(body),
                        "sha256": sha,
                    })
                    _video_download_events.append({
                        "ts": time.time(),
                        "scene_no": scene_no,
                        "attempt": attempt + 1,
                        "media_id": media_id,
                        "media_status": media_status,
                        "phase": "download_ok_redirect_scheduler",
                        "redirect_status": int(resp.status),
                        "gcs_status": int(video_resp.status),
                        "content_type": str((video_resp.headers or {}).get("content-type", "") or ""),
                        "body_size": len(body),
                    })
                    downloaded_media_ids_global.add(media_id)
                    _mark_video_media_probe_success(media_id)
                    downloaded_count += 1
                    log(f"  {os.path.basename(fpath)} ({len(body)//1024}KB) [flow-video-redirect]", "OK")
                    success = True
                    break
                else:
                    _video_download_events.append({
                        "ts": time.time(),
                        "scene_no": scene_no,
                        "attempt": attempt + 1,
                        "media_id": media_id,
                        "media_status": media_status,
                        "phase": "probe_redirect_not_ready_scheduler",
                        "redirect_status": int(resp.status),
                        "gcs_status": int(resp.status),
                        "content_type": str((resp.headers or {}).get("content-type", "") or ""),
                    })
                    _mark_video_media_probe_fail(media_id, reason=f"redirect_status_{int(resp.status)}")

                if resp.ok:
                    body = await resp.body()
                    ct = str((resp.headers or {}).get("content-type", "")).lower()
                    if ("video" in ct and len(body) >= max(50000, FLOW_VIDEO_MIN_VALID_BYTES)):
                        with open(fpath, "wb") as f:
                            f.write(body)
                        try:
                            sha = _sha256_file(fpath)
                        except Exception:
                            sha = ""
                        _download_hash_records.append({
                            "filename": os.path.basename(fpath),
                            "prompt_num": scene_no,
                            "prompt_index": scene_no,
                            "img_num": variant_index,
                            "src": redirect_url,
                            "media_id": media_id,
                            "media_status": media_status,
                            "method": "request.get_flow_video_direct_scheduler",
                            "size_bytes": len(body),
                            "sha256": sha,
                        })
                        _video_download_events.append({
                            "ts": time.time(),
                            "scene_no": scene_no,
                            "attempt": attempt + 1,
                            "media_id": media_id,
                            "media_status": media_status,
                            "phase": "download_ok_direct_scheduler",
                            "redirect_status": int(resp.status),
                            "gcs_status": int(resp.status),
                            "content_type": ct,
                            "body_size": len(body),
                        })
                        downloaded_media_ids_global.add(media_id)
                        _mark_video_media_probe_success(media_id)
                        downloaded_count += 1
                        log(f"  {os.path.basename(fpath)} ({len(body)//1024}KB) [flow-video-direct]", "OK")
                        success = True
                        break
                    _video_download_events.append({
                        "ts": time.time(),
                        "scene_no": scene_no,
                        "attempt": attempt + 1,
                        "media_id": media_id,
                        "media_status": media_status,
                        "phase": "probe_direct_not_video_scheduler",
                        "redirect_status": int(resp.status),
                        "gcs_status": int(resp.status),
                        "content_type": ct,
                        "body_size": len(body),
                    })
                    _mark_video_media_probe_fail(media_id, reason=f"direct_not_video_or_small_{len(body)}")
            except Exception as e:
                _video_download_events.append({
                    "ts": time.time(),
                    "scene_no": scene_no,
                    "attempt": attempt + 1,
                    "media_id": media_id,
                    "media_status": media_status,
                    "phase": "exception_scheduler",
                    "error": str(e),
                })
                _mark_video_media_probe_fail(media_id, reason="exception")
            await asyncio.sleep(1.2)

        if not success:
            # Bug fix: Không dùng snapshot lỗi quá cũ, mà chụp bảng lỗi ngay sau khi bị rớt tải
            fresh_errors = await capture_flow_ui_error_messages(page, f"after_probe_canh{scene_no:03d}")
            if _has_audiovisual_load_ui_error(fresh_errors):
                _mark_video_media_probe_fail(media_id, reason="ui_audiovisual_error")
            log(
                f"  canh_{scene_no:03d}: media {media_id[:8]}... ({media_status or 'UNKNOWN'}) "
                f"chưa tải được, sẽ probe lại sau.",
                "WARN",
            )

        # Không tải dồn quá nhanh nhiều video liên tiếp.
        await asyncio.sleep(_flow_human_rng.uniform(1.0, 2.5))

    if downloaded_count > 0:
        log(f"  canh_{scene_no:03d}: đã tải {downloaded_count} video.", "OK")
        return "downloaded", downloaded_count
    return "not_ready", 0


async def _run_google_flow_video_scheduler_no_reload(page, prompts: list[str]) -> int:
    """
    Scheduler video mới theo yêu cầu:
    - Không reload định kỳ.
    - Gửi cảnh mới theo nhịp random (60-90s; khi stall -> 90-150s).
    - API-only: có mediaId là probe được, không phụ thuộc UI/READY cứng.
    - Mỗi vòng quét probe/tải toàn bộ cảnh đã gửi (không ép theo 1 cảnh).
    - Không tải đồng thời với lúc gửi prompt (ưu tiên 1 tác vụ tại 1 thời điểm).
    - Khi thấy rate-limit rõ ràng -> cooldown 3 phút.
    - unusual activity chỉ log cảnh báo, không cooldown.
    """
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    scene_items: list[tuple[int, str]] = []
    for i, prompt in enumerate(prompts):
        scene_items.append((extract_scene_number(prompt, i + 1), prompt))

    total = len(scene_items)
    saved_total = 0
    next_send_index = 0
    sent_scene_order: list[int] = []
    sent_scene_set: set[int] = set()
    downloaded_scene_set: set[int] = set()
    failed_scene_set: set[int] = set()
    downloaded_media_ids_global: set[str] = set()
    scene_deadline_ts: dict[int, float] = {}

    stall_rounds_without_ready = 0
    # Unusual handling (theo rule mới):
    # - Không cooldown khi unusual toàn phần.
    # - Chỉ reload trước lần gửi prompt kế tiếp.
    # - Nếu 3 lần liên tiếp vẫn không tạo được video mới -> coi như bị ban.
    unusual_window_active = False
    unusual_window_baseline_saved = 0
    unusual_window_attempts = 0
    unusual_detected_after_last_send = False
    reload_before_next_send = False
    generic_ui_fail_streak = 0
    round_index = 0
    next_send_ts = time.time()
    project_id = _extract_flow_project_id_from_url(page.url)
    last_project_poll_ts = 0.0
    next_project_poll_sec = max(5.0, _jitter_seconds(FLOW_VIDEO_PROJECT_POLL_INTERVAL_SEC, low=0.8, high=1.25, floor=5.0))

    while len(downloaded_scene_set | failed_scene_set) < total:
        round_index += 1
        await wait_pending_api_tasks(timeout_sec=2.5)

        # Poll projectInitialData theo chu kỳ để thu media mới (API-only, không phụ thuộc UI).
        now_poll = time.time()
        if project_id and (now_poll - last_project_poll_ts) >= next_project_poll_sec:
            poll_info = await _poll_flow_project_initial_data_for_video_media(page, project_id)
            last_project_poll_ts = now_poll
            next_project_poll_sec = max(5.0, _jitter_seconds(FLOW_VIDEO_PROJECT_POLL_INTERVAL_SEC, low=0.8, high=1.25, floor=5.0))
            log(
                f"[FLOW-POLL] projectInitialData status={poll_info.get('status')} "
                f"mapped_scene={poll_info.get('mapped_scene')} orphan_added={poll_info.get('orphan_added')} "
                f"error={poll_info.get('error', '')}",
                "DBG",
            )

        # Gán orphan media vào cảnh pending để đi nhánh probe/tải hiện có.
        orphan_assigned = _assign_orphan_media_to_pending_scenes(
            sent_scene_order=sent_scene_order,
            downloaded_scene_set=downloaded_scene_set,
            failed_scene_set=failed_scene_set,
        )
        if orphan_assigned > 0:
            log(f"[FLOW-MAP] đã gán {orphan_assigned} orphan media vào cảnh pending.", "INFO")

        # Quét cảnh báo UI mỗi vòng scheduler.
        ui_messages = await capture_flow_ui_error_messages(page, f"scheduler_round_{round_index}")
        if _has_rate_limit_ui_error(ui_messages):
            cool = int(_jitter_seconds(max(30, FLOW_VIDEO_UNUSUAL_COOLDOWN_SEC), low=0.85, high=1.2, floor=30))
            log(f"Phát hiện rate-limit. Cooldown {cool}s trước khi tiếp tục.", "WARN")
            await asyncio.sleep(cool)
            # Sau cooldown, dời nhịp gửi tiếp theo để tránh burst.
            next_send_ts = max(next_send_ts, time.time() + _pick_flow_video_send_interval_sec(stall_rounds_without_ready))
            continue
        if _has_unusual_activity_ui_error(ui_messages):
            # Debounce: cùng 1 chu kỳ gửi prompt chỉ tính 1 lần unusual để tránh đếm trùng.
            if not unusual_detected_after_last_send:
                # Nếu đã có tiến triển (saved tăng) thì reset cửa sổ unusual cũ.
                if unusual_window_active and saved_total > unusual_window_baseline_saved:
                    log(
                        f"Unusual một phần: vẫn tạo thêm được video (saved +{saved_total - unusual_window_baseline_saved}). "
                        f"Reset bộ đếm unusual.",
                        "INFO",
                    )
                    unusual_window_active = False
                    unusual_window_attempts = 0
                    unusual_window_baseline_saved = saved_total

                if not unusual_window_active:
                    unusual_window_active = True
                    unusual_window_baseline_saved = saved_total
                    unusual_window_attempts = 1
                else:
                    # Không có video mới kể từ khi mở cửa sổ unusual -> tăng số lần thử.
                    if saved_total <= unusual_window_baseline_saved:
                        unusual_window_attempts += 1

                log(
                    f"Phát hiện unusual activity (attempt {unusual_window_attempts}/3, saved_baseline={unusual_window_baseline_saved}, saved_now={saved_total}). "
                    "Không cooldown; sẽ reload trước lần gửi prompt kế tiếp.",
                    "WARN",
                )
                unusual_detected_after_last_send = True
                reload_before_next_send = True

                if unusual_window_attempts >= 3 and saved_total <= unusual_window_baseline_saved:
                    log(
                        "Unusual toàn phần 3 lần liên tiếp không tạo được video mới. TẮT WORKER DO BỊ CHẶN.",
                        "ERROR",
                    )
                    return -1

            next_send_ts = max(next_send_ts, time.time() + _pick_flow_video_send_interval_sec(stall_rounds_without_ready))
            continue

        if _has_generic_failure_ui_error(ui_messages):
            generic_ui_fail_streak += 1
        else:
            generic_ui_fail_streak = 0
        if generic_ui_fail_streak >= 4:
            cool = int(_jitter_seconds(max(30, int(FLOW_VIDEO_UNUSUAL_COOLDOWN_SEC // 2)), low=0.85, high=1.2, floor=30))
            log(
                f"UI báo lỗi chung liên tiếp {generic_ui_fail_streak} vòng. "
                f"Cooldown {cool}s + F5 để cắt vòng lặp.",
                "WARN",
            )
            await asyncio.sleep(cool)
            try:
                await page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(_jitter_seconds(4, low=0.8, high=1.25, floor=2.0))
            except Exception as e:
                log(f"Lỗi reload sau generic-ui-fail: {e}", "WARN")
            generic_ui_fail_streak = 0
            next_send_ts = max(next_send_ts, time.time() + _pick_flow_video_send_interval_sec(stall_rounds_without_ready))
            continue

        now_ts = time.time()
        # Mark timeout cho cảnh đã gửi nhưng quá deadline vẫn chưa tải được.
        for sc in list(sent_scene_set):
            if sc in downloaded_scene_set or sc in failed_scene_set:
                continue
            deadline = scene_deadline_ts.get(sc, 0)
            if deadline > 0 and now_ts >= deadline:
                candidate_ids = get_scene_candidate_video_media_ids(sc)
                status_preview = []
                for mid in candidate_ids[:5]:
                    status_preview.append(f"{mid[:8]}...:{_video_media_status_by_id.get(mid, 'UNKNOWN')}")
                recent_backend_errors = _get_recent_backend_errors_for_scene(sc, limit=2)
                log(
                    f"  canh_{sc:03d}: quá timeout {GOOGLE_FLOW_VIDEO_WAIT_AFTER_LAST_PROMPT_SEC}s, đánh dấu fail. "
                    f"media={len(candidate_ids)} status={status_preview} backend_errors={recent_backend_errors}",
                    "WARN",
                )
                failed_scene_set.add(sc)

        # Ưu tiên tải trước khi gửi để tránh trùng lúc tải + gửi.
        # Global probe/download sweep: cảnh có mediaId chưa tải sẽ được probe theo nhịp.
        scenes_to_probe_download: list[int] = []
        for sc in sent_scene_order:
            if sc in downloaded_scene_set:
                continue
            if sc in failed_scene_set:
                continue
            ids = get_scene_candidate_video_media_ids(sc)
            if not ids:
                continue
            # Nếu toàn bộ media FAILED thì chốt fail sớm.
            if all(_is_video_media_failed_status(_video_media_status_by_id.get(mid, "")) for mid in ids):
                failed_scene_set.add(sc)
                log(f"  canh_{sc:03d}: toàn bộ media FAILED (API status).", "WARN")
                continue
            has_probe_candidate = any(
                (mid not in downloaded_media_ids_global)
                and (mid not in _video_media_terminal_skip)
                and (not _is_video_media_failed_status(_video_media_status_by_id.get(mid, "")))
                for mid in ids
            )
            if has_probe_candidate:
                scenes_to_probe_download.append(sc)

        if scenes_to_probe_download:
            stall_rounds_without_ready = 0
            log(
                f"Global probe sweep: {len(scenes_to_probe_download)} cảnh có media chưa tải.",
                "INFO",
            )
            for sc in scenes_to_probe_download:
                status, downloaded_count = await _download_ready_videos_for_scene(
                    page,
                    sc,
                    downloaded_media_ids_global,
                )
                if status == "cooldown":
                    cool = int(_jitter_seconds(max(30, FLOW_VIDEO_UNUSUAL_COOLDOWN_SEC), low=0.85, high=1.2, floor=30))
                    log(f"  canh_{sc:03d}: cooldown {cool}s do rate-limit.", "WARN")
                    await asyncio.sleep(cool)
                    # Cooldown xong thì dừng vòng sweep hiện tại để tránh burst tiếp.
                    break
                if status == "failed":
                    failed_scene_set.add(sc)
                elif downloaded_count > 0:
                    saved_total += downloaded_count
                    # Có video mới -> unusual dạng một phần, reset bộ đếm unusual toàn phần.
                    if unusual_window_active and saved_total > unusual_window_baseline_saved:
                        log(
                            f"Unusual một phần: cảnh {sc:03d} đã tải thêm {downloaded_count} video. Reset bộ đếm unusual.",
                            "INFO",
                        )
                        unusual_window_active = False
                        unusual_window_attempts = 0
                        unusual_window_baseline_saved = saved_total
                        reload_before_next_send = False
                    # Logic mới: Chỉ đánh dấu cảnh là HOÀN TẤT nếu không còn media nào đang PENDING/mới.
                    candidate_ids_check = get_scene_candidate_video_media_ids(sc)
                    pending_candidates = [
                        mid for mid in candidate_ids_check
                        if mid not in downloaded_media_ids_global
                        and mid not in _video_media_terminal_skip
                        and not _is_video_media_failed_status(_video_media_status_by_id.get(mid, ""))
                    ]
                    if not pending_candidates:
                        downloaded_scene_set.add(sc)
                        log(
                            f"[FLOW-VIDEO] === Chốt sổ cảnh {sc:03d} "
                            f"(đã tải trọn bộ, tổng saved {saved_total}) ===",
                            "STEP",
                        )
                    else:
                        log(
                            f"[FLOW-VIDEO] Cập nhật cảnh {sc:03d} "
                            f"(tải {downloaded_count} video, còn lại {len(pending_candidates)} đang chờ)",
                            "STEP",
                        )
                # Delay nhẹ giữa các cảnh để tránh tải dồn quá nhanh.
                await asyncio.sleep(_flow_human_rng.uniform(1.0, 2.0))
            continue

        # Không có cảnh nào để probe/tải trong vòng này.
        has_pending_sent = any(sc not in downloaded_scene_set and sc not in failed_scene_set for sc in sent_scene_set)
        if has_pending_sent:
            stall_rounds_without_ready += 1
        else:
            stall_rounds_without_ready = 0

        # Đến nhịp thì gửi cảnh tiếp theo.
        if next_send_index < total and now_ts >= next_send_ts:
            if reload_before_next_send:
                try:
                    log("Unusual pending: reload trước khi gửi prompt kế tiếp.", "WARN")
                    await page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(_jitter_seconds(4, low=0.8, high=1.25, floor=2.0))
                except Exception as e:
                    log(f"Lỗi reload trước khi gửi prompt kế tiếp: {e}", "WARN")
                reload_before_next_send = False

            scene_no, prompt = scene_items[next_send_index]
            log(f"[FLOW-VIDEO {next_send_index+1}/{total}] === Bắt đầu cảnh {scene_no:03d} ===", "STEP")
            sent_ok = await _send_one_flow_video_scene_prompt(
                page=page,
                prompt=prompt,
                scene_no=scene_no,
                prompt_index=next_send_index,
                total_prompts=total,
            )
            if sent_ok:
                sent_scene_set.add(scene_no)
                sent_scene_order.append(scene_no)
                scene_deadline_ts[scene_no] = time.time() + max(60, GOOGLE_FLOW_VIDEO_WAIT_AFTER_LAST_PROMPT_SEC)
            else:
                # Nếu fail do reference (strict mode) thì trả mã đặc biệt để runner
                # quay về luồng regenerate reference thay vì chỉ fail riêng 1 cảnh.
                if GOOGLE_FLOW_VIDEO_REQUIRE_REFERENCE_UPLOAD and _last_scene_reference_attach_failed:
                    log(
                        f"  canh_{scene_no:03d}: attach/upload reference fail trong strict mode. "
                        f"Trả mã {FLOW_VIDEO_PRELOAD_FAILED_CODE} để runner tạo lại reference.",
                        "ERR",
                    )
                    return FLOW_VIDEO_PRELOAD_FAILED_CODE
                failed_scene_set.add(scene_no)
                log(f"  canh_{scene_no:03d}: gửi thất bại, đánh dấu fail.", "WARN")

            next_send_index += 1
            unusual_detected_after_last_send = False
            next_interval = _pick_flow_video_send_interval_sec(stall_rounds_without_ready)
            next_send_ts = time.time() + next_interval
            log(
                f"  Cảnh tiếp theo sẽ gửi sau ~{next_interval:.1f}s "
                f"(stall_rounds={stall_rounds_without_ready}).",
                "INFO",
            )
            await asyncio.sleep(_flow_human_rng.uniform(0.8, 1.8))
            continue

        # Nếu đã gửi hết và không còn cảnh pending thì kết thúc.
        has_pending_after = any(sc not in downloaded_scene_set and sc not in failed_scene_set for sc in sent_scene_set)
        if next_send_index >= total and not has_pending_after:
            break

        await asyncio.sleep(_flow_human_video_poll_interval())

    if failed_scene_set:
        failed_sorted = sorted(list(failed_scene_set))
        log(f"Các cảnh không tải được video: {failed_sorted}", "WARN")
    return saved_total


async def run_google_flow_auto_video_request_response(page, prompts: list[str]) -> int:
    """
    Auto mode cho Google Flow VIDEO:
    - Tự gửi prompt video
    - Bắt request/response video API
    - Tải file mp4 theo scene từ mediaId.
    """
    global _run_started_ts
    saved_total = 0

    # Video mode: User đã setup sẵn project ở chế độ Veo 3.
    # Không tạo project mới, không click "Trình tạo cảnh" — cứ vào editor hiện tại.
    target_url = get_target_home_url()
    log(f"Đang mở Google Flow (video): {target_url}", "WEB")
    await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)

    # Chờ editor ready (project đã setup sẵn video mode bởi user)
    editor_ready = await ensure_google_flow_editor(page, timeout_sec=45)

    if not editor_ready:
        log("Không vào được editor Google Flow (video). Hãy mở Flow và setup project trước.", "ERR")
        return 0

    log("Editor Flow video đã sẵn sàng (user pre-setup).", "OK")

    await debug_step(page, "google_flow_video_editor_ready", extra={"url": page.url, "prompts": len(prompts)})
    _run_started_ts = time.time()

    # ── PRELOAD ẢNH REFERENCE VÀO THƯ VIỆN FLOW (1 LẦN ĐẦU SESSION) ──────────
    # Mục đích: upload tất cả ảnh character1/2/3, image1 vào thư viện một lần đầu.
    # Sau đó mỗi prompt chỉ cần tìm kiếm rồi click chọn, không cần upload lại.
    if GOOGLE_FLOW_VIDEO_USE_REFERENCE_IMAGES and GOOGLE_FLOW_VIDEO_PRELOAD_REFERENCE_LIBRARY:
        # Strict check: prompt nào yêu cầu token nào thì thư mục reference phải có file token đó.
        required_tokens = _collect_required_reference_tokens(prompts)
        if required_tokens:
            missing_tokens = []
            for token in required_tokens:
                if not _find_reference_path_by_token_local(GOOGLE_FLOW_VIDEO_REFERENCE_DIR, token):
                    missing_tokens.append(token)
            if missing_tokens:
                log(
                    f"Thiếu file reference theo token prompt: {missing_tokens}. "
                    "Dừng vòng video để runner quay lại tạo ảnh reference.",
                    "ERR",
                )
                return FLOW_VIDEO_PRELOAD_FAILED_CODE

        preload_paths = list_reference_image_paths(GOOGLE_FLOW_VIDEO_REFERENCE_DIR)
        log(f"Preload {len(preload_paths)} ảnh reference vào thư viện Flow...", "STEP")
        if not preload_paths:
            log(
                f"Không tìm thấy ảnh reference trong: {GOOGLE_FLOW_VIDEO_REFERENCE_DIR}. "
                "Dừng vòng video để runner quay lại bước tạo ảnh reference.",
                "ERR",
            )
            return FLOW_VIDEO_PRELOAD_FAILED_CODE

        ok_preload = False
        preload_dbg = {}
        max_preload_attempts = max(1, int(GOOGLE_FLOW_VIDEO_PRELOAD_MAX_ATTEMPTS))
        for preload_attempt in range(1, max_preload_attempts + 1):
            ok_preload, preload_dbg = await preload_reference_library_images(
                page,
                image_paths=preload_paths,
                vp_height=VP_HEIGHT,
                preload_wait_sec=GOOGLE_FLOW_VIDEO_PRELOAD_WAIT_SEC,
                log_cb=log,
            )
            if ok_preload:
                break
            if preload_attempt < max_preload_attempts:
                log(
                    f"Preload reference thất bại (lần {preload_attempt}/{max_preload_attempts}), thử lại...",
                    "WARN",
                )
                await asyncio.sleep(2.0)

        log(
            f"Preload thư viện reference: {'OK' if ok_preload else 'FAIL'} "
            f"({len(preload_paths)} ảnh, mode={GOOGLE_FLOW_VIDEO_REFERENCE_MODE})",
            "OK" if ok_preload else "WARN",
        )
        await debug_step(
            page,
            "flow_video_preload_reference",
            extra={"ok": ok_preload, "count": len(preload_paths), "debug": preload_dbg},
        )
        if not ok_preload:
            log(
                "Preload/upload ảnh reference thất bại sau toàn bộ retry. "
                "Dừng vòng video để runner quay lại bước tạo ảnh reference.",
                "ERR",
            )
            return FLOW_VIDEO_PRELOAD_FAILED_CODE

    # ── Scheduler mới (không reload định kỳ) ──────────────────────────────────
    # Theo yêu cầu hiện tại:
    # - Gửi cảnh theo nhịp 60-90s (stall -> 90-150s)
    # - Chỉ tải khi READY
    # - Cooldown khi unusual activity
    # - Không tải trùng thời điểm gửi prompt
    return await _run_google_flow_video_scheduler_no_reload(page, prompts)

    # ── GỬI TỪNG PROMPT 1, CHỜ RENDER XONG RỒI MỚI GỬI TIẾP ──────────────────
    # Lý do: gửi nhiều prompt cùng lúc làm Flow bị scroll quá mức,
    # dẫn đến mất ô nhập và nút gửi (btn=2, input=0).
    # Gửi từng cái một giúp UI ổn định và ô nhập luôn tìm được.

    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    saved_total = 0

    for i, prompt in enumerate(prompts):
        scene_no = extract_scene_number(prompt, i + 1)
        log(f"[FLOW-VIDEO {i+1}/{len(prompts)}] === Bắt đầu cảnh {scene_no:03d} ===", "STEP")

        # ── Bước 1a: Attach ảnh reference theo token trong prompt ──────────────
        # Tìm token character1/character2.../image1 trong prompt
        # rồi tìm ảnh tương ứng và attach vào Flow UI trước khi gửi.
        if GOOGLE_FLOW_VIDEO_USE_REFERENCE_IMAGES:
            # Xóa reference cũ trong composer trước khi attach mới.
            clear_info = await clear_reference_attachments_in_composer(
                page,
                focus_prompt_cb=lambda: find_and_focus_prompt(page),
                max_rounds=2,
            )
            if clear_info.get("before", 0) > 0 and not clear_info.get("cleared"):
                log(
                    f"  Cảnh báo: chưa xóa sạch reference cũ (before={clear_info.get('before')}, "
                    f"after={clear_info.get('after')})",
                    "WARN",
                )

            # Tìm tất cả token reference trong prompt này
            ref_tokens = _extract_reference_tokens_from_video_prompt(prompt)
            if ref_tokens:
                max_refs = max(1, int(GOOGLE_FLOW_VIDEO_MAX_REFERENCES_PER_PROMPT))
                original_tokens = ref_tokens[:]
                ref_tokens = ref_tokens[:max_refs]
                dropped_tokens = original_tokens[max_refs:]
                log(f"  cảnh_{scene_no:03d}: tokens reference: {original_tokens}", "DBG")
                if dropped_tokens:
                    log(
                        f"  cảnh_{scene_no:03d}: vượt giới hạn {max_refs} reference/prompt, "
                        f"bỏ qua token dư: {dropped_tokens}",
                        "WARN",
                    )
                attach_ok_all = True
                for token in ref_tokens:
                    token_lower = token.lower()
                    # Tìm đường dẫn file local theo token
                    token_path = _find_reference_path_by_token_local(GOOGLE_FLOW_VIDEO_REFERENCE_DIR, token)
                    token_ok = False
                    ref_dbg = {}
                    if GOOGLE_FLOW_VIDEO_REFERENCE_MODE == "library_search":
                        # Tìm trong thư viện Flow đã preload
                        token_ok, ref_dbg = await attach_reference_from_library_by_name(
                            page,
                            search_name=token_lower,
                            vp_height=VP_HEIGHT,
                        )
                    elif token_path:
                        # Upload trực tiếp từ file
                        token_ok, ref_dbg = await upload_reference_image_for_video(
                            page,
                            image_path=token_path,
                            allow_direct_file_input=GOOGLE_FLOW_VIDEO_ALLOW_DIRECT_FILE_INPUT,
                            verify_fn=verify_reference_image_attached,
                            log_cb=log,
                        )
                    else:
                        ref_dbg = {"error": "reference_file_not_found_for_token", "token": token}
                    log(
                        f"  cảnh_{scene_no:03d}: token={token_lower} -> "
                        f"{'attached ✅' if token_ok else 'FAIL ❌'} | {ref_dbg.get('error', '')}",
                        "OK" if token_ok else "WARN",
                    )
                    attach_ok_all = attach_ok_all and token_ok

                # Nếu require reference và attach thất bại → bỏ qua cảnh này
                if not attach_ok_all and GOOGLE_FLOW_VIDEO_REQUIRE_REFERENCE_UPLOAD:
                    log(f"  cảnh_{scene_no:03d}: Bỏ qua vì attach reference thất bại.", "WARN")
                    continue
            else:
                log(f"  cảnh_{scene_no:03d}: Không tìm được token reference trong prompt.", "DBG")

        # ── Bước 1b: Gửi prompt ────────────────────────────────────────────────
        # Delay "suy nghĩ" ngắn trước khi gửi để nhịp thao tác tự nhiên hơn.
        # Mặc định chỉ 0.x - vài giây nên không làm lệch thời gian tổng quá nhiều.
        await asyncio.sleep(_flow_human_delay_after_send(FLOW_VIDEO_PRE_SEND_BASE_SEC))
        log(f"[FLOW-VIDEO {i+1}/{len(prompts)}] Gửi prompt: {prompt[:70]}", "SEND")
        sent = False
        try:
            # Scroll xuống cuối trang trước khi type để luôn thấy ô nhập
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.5)
            except Exception:
                pass

            await type_prompt(page, prompt)
            sent = await send_video_prompt(page)
            await debug_step(
                page,
                f"flow_video_sent_prompt_{i+1:02d}",
                job_id=f"canh_{scene_no:03d}",
                extra={"sent_ok": sent, "prompt_preview": prompt[:90]},
            )
            await capture_prompt_submission_trace(page, i, prompt)
        except Exception as e:
            log(f"Lỗi gửi prompt video #{i+1}: {e}", "WARN")

        if not sent:
            log(f"  canh_{scene_no:03d}: Gửi thất bại, bỏ qua cảnh này.", "WARN")
            continue

        # ── Bước 2: Poll chờ render xong cho cảnh này ──────────────────────
        # Poll từng cảnh một, timeout = GOOGLE_FLOW_VIDEO_WAIT_AFTER_LAST_PROMPT_SEC
        log(
            f"  canh_{scene_no:03d}: Chờ render (tối đa {GOOGLE_FLOW_VIDEO_WAIT_AFTER_LAST_PROMPT_SEC}s)...",
            "WAIT",
        )
        _run_started_ts = time.time()  # reset mốc để bắt đúng event API của cảnh này
        poll_start = time.time()
        poll_reload_counter = 0
        scene_resolved = False

        while (time.time() - poll_start) < GOOGLE_FLOW_VIDEO_WAIT_AFTER_LAST_PROMPT_SEC:
            elapsed = int(time.time() - poll_start)

            # Reload trang để lấy projectInitialData mới (cập nhật status video).
            # KHÔNG reload trong 60s đầu — Flow đang render, reload sớm gây mất request.
            # Sau 60s mới reload, mỗi 60s 1 lần (6 vòng poll × 10s = 60s).
            poll_reload_counter += 1
            if (
                GOOGLE_FLOW_VIDEO_ALLOW_RELOAD_DURING_DOWNLOAD
                and elapsed >= 60
                and poll_reload_counter % 6 == 0
            ):
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(2)
                    await wait_pending_api_tasks(timeout_sec=3.0)
                    log(f"  [{elapsed}s] Reload trang cập nhật status cảnh {scene_no:03d}.", "DBG")
                except Exception:
                    pass


            candidates = get_scene_candidate_video_media_ids(scene_no)
            if candidates:
                has_ready = any(_is_video_media_ready_status(_video_media_status_by_id.get(m, "")) for m in candidates)
                all_failed = all(_is_video_media_failed_status(_video_media_status_by_id.get(m, "")) for m in candidates)
                best_status = _video_media_status_by_id.get(candidates[0], "UNKNOWN")
                log(f"  [{elapsed}s] canh_{scene_no:03d}: {best_status} ({len(candidates)} media)", "DBG")

                if has_ready:
                    log(f"  canh_{scene_no:03d}: READY sau {elapsed}s. Bắt đầu tải!", "OK")
                    scene_resolved = True
                    break
                elif all_failed:
                    log(f"  canh_{scene_no:03d}: tất cả FAILED sau {elapsed}s.", "WARN")
                    scene_resolved = True  # dù failed, vẫn tiếp tục
                    break

                # Kiểm tra lỗi UI
                ui_errors = await capture_flow_ui_error_messages(page, f"poll_{elapsed}s_canh{scene_no:03d}")
                if ui_errors:
                    ui_fail_count = sum(1 for m in ui_errors if "không thành công" in m.lower())
                    if ui_fail_count:
                        log(f"  [{elapsed}s] UI báo {ui_fail_count} lỗi 'Không thành công'.", "WARN")
            else:
                log(f"  [{elapsed}s] canh_{scene_no:03d}: chưa thấy mediaId...", "DBG")

            # Poll theo chu kỳ có dao động nhẹ thay vì đúng 10s tuyệt đối.
            await asyncio.sleep(_flow_human_video_poll_interval())

        if not scene_resolved:
            log(
                f"  canh_{scene_no:03d}: Hết timeout {GOOGLE_FLOW_VIDEO_WAIT_AFTER_LAST_PROMPT_SEC}s, thử tải dù chưa READY.",
                "WARN",
            )

        # ── Bước 3: Tải video cho cảnh này ────────────────────────────────
        await wait_pending_api_tasks(timeout_sec=3.0)
        after_wait_errors = await capture_flow_ui_error_messages(page, f"after_wait_canh{scene_no:03d}")

        candidate_ids = get_scene_candidate_video_media_ids(scene_no)
        if not candidate_ids:
            log(f"  Thiếu video mediaId cho canh_{scene_no:03d}", "WARN")
            continue

        if _has_rate_limit_ui_error(after_wait_errors):
            log(
                f"  canh_{scene_no:03d}: UI báo rate limit. Dừng tải để tránh retry vô ích.",
                "WARN",
            )
            continue

        log(f"  canh_{scene_no:03d}: tìm thấy {len(candidate_ids)} mediaId ứng viên", "DBG")
        log(f"  canh_{scene_no:03d}: retry tối đa {GOOGLE_FLOW_VIDEO_DOWNLOAD_MAX_ATTEMPTS} vòng", "DBG")

        downloaded_media_ids = set()
        downloaded_paths = []
        chosen_media_id = ""
        for attempt in range(GOOGLE_FLOW_VIDEO_DOWNLOAD_MAX_ATTEMPTS):
            loop_errors = await capture_flow_ui_error_messages(page, f"loop_attempt_{attempt+1}")
            if _has_audiovisual_load_ui_error(loop_errors):
                log(
                    f"  canh_{scene_no:03d}: phát hiện lỗi UI 'tải nội dung nghe nhìn' ở attempt {attempt+1}.",
                    "WARN",
                )

            candidate_ids = get_scene_candidate_video_media_ids(scene_no)
            if not candidate_ids:
                log(f"  [attempt {attempt+1}] chưa thấy mediaId cho canh_{scene_no:03d}", "WARN")
                await asyncio.sleep(3)
                continue

            attempt_success_count = 0
            for media_id in candidate_ids:
                if media_id in downloaded_media_ids:
                    # Media này đã tải xong ở vòng trước, bỏ qua để tránh tải trùng.
                    continue

                redirect_url = f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={media_id}"
                media_status = _video_media_status_by_id.get(media_id, "")
                variant_index = len(downloaded_paths) + 1
                fpath = _build_scene_video_output_path(scene_no, variant_index)

                if _is_video_media_failed_status(media_status):
                    log(
                        f"  [attempt {attempt+1}] media={media_id[:8]}... status={media_status} (đã FAILED)",
                        "DBG",
                    )

                try:
                    resp = await page.context.request.get(redirect_url, timeout=30000, max_redirects=0)
                    location = str((resp.headers or {}).get("location", "")).strip()
                    event_base = {
                        "ts": time.time(),
                        "scene_no": scene_no,
                        "attempt": attempt + 1,
                        "media_id": media_id,
                        "media_id_short": media_id[:8] + "...",
                        "media_status": media_status,
                        "redirect_url": redirect_url,
                        "redirect_status": int(resp.status),
                        "location_sample": (location[:140] + "...(cut)") if len(location) > 140 else location,
                    }

                    if resp.status in (301, 302, 307, 308) and location:
                        log(
                            f"  [attempt {attempt+1}] media={media_id[:8]}... status={media_status or 'UNKNOWN'} "
                            f"redirect={location[:70]}...",
                            "DBG",
                        )
                        video_resp = await page.context.request.get(location, timeout=60000)
                        if not video_resp.ok:
                            event_fail = dict(event_base)
                            event_fail.update({
                                "phase": "gcs_response_not_ok",
                                "gcs_status": int(video_resp.status),
                                "content_type": str((video_resp.headers or {}).get("content-type", "") or ""),
                                "body_size": 0,
                                "body_preview": "",
                            })
                            _video_download_events.append(event_fail)
                            log(
                                f"  [attempt {attempt+1}] media={media_id[:8]}... GCS status={video_resp.status}",
                                "WARN",
                            )
                            continue
                        body = await video_resp.body()
                        content_type = str((video_resp.headers or {}).get("content-type", "") or "")
                        body_preview = _safe_decode_bytes_preview(body, max_len=240)
                        if len(body) < 50000:
                            event_small = dict(event_base)
                            event_small.update({
                                "phase": "gcs_body_too_small",
                                "gcs_status": int(video_resp.status),
                                "content_type": content_type,
                                "body_size": len(body),
                                "body_preview": body_preview,
                            })
                            _video_download_events.append(event_small)
                            log(
                                f"  [attempt {attempt+1}] media={media_id[:8]}... file nhỏ {len(body)} bytes "
                                f"(chưa ready)",
                                "WARN",
                            )
                            continue
                        with open(fpath, "wb") as f:
                            f.write(body)
                        try:
                            sha = _sha256_file(fpath)
                        except Exception:
                            sha = ""
                        _download_hash_records.append({
                            "filename": os.path.basename(fpath),
                            "prompt_num": scene_no,
                            "prompt_index": scene_no,
                            "img_num": variant_index,
                            "src": location,
                            "media_id": media_id,
                            "media_status": media_status,
                            "method": "request.get_flow_video_307_redirect",
                            "size_bytes": len(body),
                            "sha256": sha,
                        })
                        event_ok = dict(event_base)
                        event_ok.update({
                            "phase": "download_ok_redirect",
                            "gcs_status": int(video_resp.status),
                            "content_type": content_type,
                            "body_size": len(body),
                            "body_preview": "",
                        })
                        _video_download_events.append(event_ok)
                        downloaded_media_ids.add(media_id)
                        downloaded_paths.append(fpath)
                        chosen_media_id = media_id
                        log(
                            f"  {os.path.basename(fpath)} ({len(body)//1024}KB) "
                            f"[flow-video-redirect]",
                            "OK",
                        )
                        saved_total += 1
                        attempt_success_count += 1
                        continue

                    elif resp.ok:
                        body = await resp.body()
                        ct = str((resp.headers or {}).get("content-type", "")).lower()
                        if ("video" in ct or len(body) > 200000):
                            with open(fpath, "wb") as f:
                                f.write(body)
                            try:
                                sha = _sha256_file(fpath)
                            except Exception:
                                sha = ""
                            _download_hash_records.append({
                                "filename": os.path.basename(fpath),
                                "prompt_num": scene_no,
                                "prompt_index": scene_no,
                                "img_num": variant_index,
                                "src": redirect_url,
                                "media_id": media_id,
                                "media_status": media_status,
                                "method": "request.get_flow_video_direct",
                                "size_bytes": len(body),
                                "sha256": sha,
                            })
                            event_ok = dict(event_base)
                            event_ok.update({
                                "phase": "download_ok_direct",
                                "gcs_status": int(resp.status),
                                "content_type": ct,
                                "body_size": len(body),
                                "body_preview": "",
                            })
                            _video_download_events.append(event_ok)
                            downloaded_media_ids.add(media_id)
                            downloaded_paths.append(fpath)
                            chosen_media_id = media_id
                            log(
                                f"  {os.path.basename(fpath)} ({len(body)//1024}KB) "
                                f"[flow-video-direct]",
                                "OK",
                            )
                            saved_total += 1
                            attempt_success_count += 1
                            continue
                        else:
                            event_small = dict(event_base)
                            event_small.update({
                                "phase": "redirect_response_small",
                                "gcs_status": int(resp.status),
                                "content_type": ct,
                                "body_size": len(body),
                                "body_preview": _safe_decode_bytes_preview(body, max_len=240),
                            })
                            _video_download_events.append(event_small)
                            log(
                                f"  [attempt {attempt+1}] media={media_id[:8]}... status={resp.status}, "
                                f"ct={ct[:30]}, body={len(body)} bytes",
                                "WARN",
                            )

                    else:
                        event_status = dict(event_base)
                        event_status.update({
                            "phase": "redirect_not_ok",
                            "gcs_status": int(resp.status),
                            "content_type": str((resp.headers or {}).get("content-type", "") or ""),
                            "body_size": 0,
                            "body_preview": "",
                        })
                        _video_download_events.append(event_status)
                        log(
                            f"  [attempt {attempt+1}] media={media_id[:8]}... status={resp.status}, chờ thêm...",
                            "WARN",
                        )

                except Exception as e:
                    _video_download_events.append({
                        "ts": time.time(),
                        "scene_no": scene_no,
                        "attempt": attempt + 1,
                        "media_id": media_id,
                        "media_id_short": media_id[:8] + "...",
                        "media_status": media_status,
                        "phase": "exception",
                        "error": str(e),
                        "redirect_status": "",
                        "gcs_status": "",
                        "content_type": "",
                        "body_size": 0,
                        "body_preview": "",
                    })
                    log(f"  [attempt {attempt+1}] media={media_id[:8]}... lỗi tải: {str(e)[:80]}", "WARN")

            remaining_ids = [mid for mid in candidate_ids if mid not in downloaded_media_ids]
            if not remaining_ids:
                break

            if attempt_success_count > 0:
                # Đã tải được ít nhất 1 video ở vòng này.
                # Chờ ngắn để Flow kịp update thêm media khác của cùng prompt rồi thử tiếp.
                await asyncio.sleep(2)
                continue

            await asyncio.sleep(3)

            if attempt >= 2:
                quick_errors = await capture_flow_ui_error_messages(page, f"download_check_{attempt+1}")
                has_scene_failed = any("không thành công" in m.lower() for m in quick_errors)
                all_api_failed = all(
                    _is_video_media_failed_status(_video_media_status_by_id.get(mid, ""))
                    for mid in candidate_ids
                ) if candidate_ids else False
                if has_scene_failed and all_api_failed:
                    log(
                        f"  canh_{scene_no:03d}: UI + API đều báo FAILED. Dừng retry sớm (attempt {attempt+1}).",
                        "WARN",
                    )
                    break
                if _has_rate_limit_ui_error(quick_errors):
                    log(f"  canh_{scene_no:03d}: vẫn dính rate limit, dừng retry.", "WARN")
                    break

            if GOOGLE_FLOW_VIDEO_ALLOW_RELOAD_DURING_DOWNLOAD and attempt in {4, 9, 14}:
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(2)
                    await wait_pending_api_tasks(timeout_sec=3.0)
                    log(f"  [attempt {attempt+1}] Đã reload trang để cập nhật status.", "DBG")
                except Exception:
                    pass

        if not downloaded_paths:
            await capture_flow_ui_error_messages(page, f"scene_{scene_no:03d}_download_failed")
            statuses = [f"{mid[:8]}...:{_video_media_status_by_id.get(mid, 'UNKNOWN')}" for mid in candidate_ids]
            status_desc = ", ".join(statuses)[:250]
            log(
                f"  Không tải được video canh_{scene_no:03d} "
                f"(media cuối={chosen_media_id[:8] + '...' if chosen_media_id else 'N/A'})"
                + (f" | statuses: {status_desc}" if status_desc else ""),
                "WARN",
            )
        else:
            log(
                f"  canh_{scene_no:03d}: đã tải {len(downloaded_paths)}/{len(candidate_ids)} video ứng viên.",
                "OK",
            )

        log(f"[FLOW-VIDEO {i+1}/{len(prompts)}] === Xong cảnh {scene_no:03d} ({saved_total} video đã tải) ===", "STEP")

        # ── Chờ ngắn trước khi gửi prompt tiếp để UI ổn định ──────────────
        await asyncio.sleep(2)

    return saved_total


# ───────────────────────────────────────────────
# MANUAL NETWORK CAPTURE (CHO SITE KHÁC DREAMINA)
# ───────────────────────────────────────────────

async def run_manual_network_capture(page):
    """
    Chế độ bắt mạng thủ công (dùng cho Google Flow hoặc khi cần soi API site khác).
    Luồng:
    1) Mở trang đích.
    2) Người dùng tự thao tác generate ảnh trên browser.
    3) Nhấn Enter trong terminal để kết thúc và lưu log request/response.
    """
    target_url = get_target_home_url()
    log(f"Đang mở trang đích để bắt network: {target_url}", "🌐")
    await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)

    # Chụp trạng thái ban đầu để đối chiếu khi debug.
    await debug_step(page, "manual_capture_started", extra={"url": page.url, "platform": TARGET_PLATFORM})

    print("\n" + "=" * 60)
    print("  CHẾ ĐỘ BẮT REQUEST/RESPONSE THỦ CÔNG")
    print("  1) Dùng browser vừa mở để login (nếu cần).")
    print("  2) Tự nhập prompt và bấm Generate trên trang.")
    print("  3) Chờ ảnh xuất hiện / request chạy xong.")
    print("  4) Quay lại terminal, nhấn Enter để lưu log.")
    print("=" * 60)
    await asyncio.get_event_loop().run_in_executor(
        None, input, "  Hoàn tất thao tác trên web -> Nhấn Enter để lưu request/response: "
    )

    await debug_step(page, "manual_capture_finished", extra={"url": page.url, "platform": TARGET_PLATFORM})
    await wait_pending_api_tasks(timeout_sec=5.0)


def _extract_flow_project_id_from_url(url: str) -> str:
    """Tách project_id từ URL Flow hiện tại để quay lại gallery khi cần."""
    if not url:
        return ""
    m = re.search(r"/project/([0-9a-fA-F-]{8,})", url)
    return str(m.group(1)) if m else ""


async def _open_flow_gallery_for_project(page, project_id: str) -> bool:
    """
    Mở lại trang gallery của project hiện tại.
    Hàm này giúp script không bị kẹt ở trang edit sau khi upscale từng ảnh.
    """
    if not project_id:
        return False
    gallery_url = f"{GOOGLE_FLOW_HOME}/project/{project_id}"
    try:
        await page.goto(gallery_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(1.2)
        return True
    except Exception:
        return False


async def _open_flow_edit_for_media(page, media_id: str) -> bool:
    """
    Từ gallery, mở trang edit của đúng ảnh theo media_id.
    Dùng selector src chứa `name=<media_id>` để click đúng tile.
    """
    if not media_id:
        return False

    # Đặt scroll về đầu danh sách trước mỗi lần tìm để tránh bỏ sót item nằm phía trên.
    try:
        await page.evaluate(
            """
            () => {
                const scroller =
                    document.querySelector('[data-virtuoso-scroller="true"]')
                    || document.querySelector('[data-testid="virtuoso-scroller"]');
                if (scroller) scroller.scrollTo({ top: 0, left: 0, behavior: "instant" });
                else window.scrollTo(0, 0);
            }
            """
        )
        await asyncio.sleep(0.2)
    except Exception:
        pass

    # Flow dùng virtualized list: ảnh ngoài viewport có thể chưa mount vào DOM.
    # Vì vậy cần quét + scroll nhiều nhịp để tìm đúng media_id.
    clicked = False
    for _ in range(18):
        try:
            clicked = await page.evaluate(
                """
                (mediaId) => {
                    const img = document.querySelector(`img[src*="name=${mediaId}"]`);
                    if (!img) return false;
                    img.scrollIntoView({ behavior: "instant", block: "center" });
                    const a = img.closest('a');
                    if (a) {
                        a.click();
                        return true;
                    }
                    img.click();
                    return true;
                }
                """,
                media_id,
            )
        except Exception:
            clicked = False

        if clicked:
            break

        # Scroll scroller của virtuoso để mount item tiếp theo.
        try:
            await page.evaluate(
                """
                () => {
                    const scroller =
                        document.querySelector('[data-virtuoso-scroller="true"]')
                        || document.querySelector('[data-testid="virtuoso-scroller"]');
                    if (scroller) {
                        scroller.scrollBy({ top: 600, left: 0, behavior: "instant" });
                    } else {
                        window.scrollBy(0, 600);
                    }
                }
                """
            )
        except Exception:
            pass
        await asyncio.sleep(0.25)

    if not clicked:
        return False

    try:
        await page.wait_for_url(re.compile(r"/edit/"), timeout=15000)
    except Exception:
        # Flow có thể chuyển trang chậm, chờ thêm rồi kiểm tra URL hiện tại.
        await asyncio.sleep(2.0)
    return "/edit/" in (page.url or "")


async def _click_upscale_2k_in_edit_page(page) -> bool:
    """
    Click UI để kích hoạt request upsampleImage hợp lệ.
    Chiến lược:
    1) Tìm trực tiếp nút/menuitem có text 2K/Upscale.
    2) Nếu chưa thấy, click các nút Download/More để mở menu rồi tìm lại.
    """
    direct_patterns = [
        re.compile(r"\b2k\b", re.IGNORECASE),
        re.compile(r"upscale", re.IGNORECASE),
        re.compile(r"upsample", re.IGNORECASE),
        re.compile(r"nâng", re.IGNORECASE),
    ]

    for patt in direct_patterns:
        try:
            btn = page.get_by_text(patt).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(0.5)
                return True
        except Exception:
            pass

    # Mở menu rồi thử lại.
    menu_open_patterns = [
        re.compile(r"download", re.IGNORECASE),
        re.compile(r"tải xuống", re.IGNORECASE),
        re.compile(r"khác", re.IGNORECASE),
        re.compile(r"more", re.IGNORECASE),
        re.compile(r"more_vert", re.IGNORECASE),
    ]
    for patt in menu_open_patterns:
        try:
            btn = page.locator("button").filter(has_text=patt).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(0.4)
                break
        except Exception:
            pass

    for patt in direct_patterns:
        try:
            btn = page.get_by_text(patt).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(0.5)
                return True
        except Exception:
            pass

    return False


async def _wait_upscale_result_for_media(media_id: str, prev_event_count: int, timeout_sec: int = 45) -> tuple[bool, str]:
    """
    Chờ request/response upsampleImage hoàn tất cho đúng media_id.
    Trả về:
    - (True, "ok") nếu có encodedImage.
    - (False, "...") nếu timeout hoặc có response fail.
    """
    deadline = time.time() + max(1, timeout_sec)
    last_status = ""

    while time.time() < deadline:
        if media_id in _upscale_success_by_media:
            return True, "ok"

        # Quét event mới để lấy status lỗi gần nhất theo media_id cho dễ debug.
        new_events = _upscale_events[prev_event_count:]
        for ev in new_events:
            if str(ev.get("media_id", "")) != media_id:
                continue
            if ev.get("type") == "upscale_response":
                status = ev.get("status", "")
                last_status = f"status={status}"
                if ev.get("has_encoded_image"):
                    return True, "ok"

        await asyncio.sleep(0.4)

    return False, (last_status or "timeout")


def _save_upscale_image_from_memory(scene_no: int, media_id: str) -> bool:
    """
    Lưu ảnh 2K từ cache response đã bắt được (_upscale_success_by_media).
    Trả về True nếu lưu thành công.
    """
    encoded = str((_upscale_success_by_media.get(media_id, {}) or {}).get("encoded_image", "") or "")
    if not encoded:
        return False

    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return False
    if len(raw) <= 5000:
        return False

    fname = f"canh_{scene_no:03d}_2k.jpg"
    fpath = os.path.join(OUTPUT_DIR, fname)
    with open(fpath, "wb") as f:
        f.write(raw)

    try:
        sha = _sha256_file(fpath)
    except Exception:
        sha = ""
    _download_hash_records.append({
        "filename": fname,
        "prompt_num": scene_no,
        "prompt_index": scene_no,
        "img_num": 1,
        "src": "flow_ui_upsample_response",
        "media_id": media_id,
        "method": "request_response_upsample_2k_ui_trigger_batch",
        "size_bytes": len(raw),
        "sha256": sha,
    })
    log(f"  {fname} ({len(raw)//1024}KB) [upscale-2k-batch-response]", "OK")
    return True


async def auto_upscale_2k_by_api(page, scene_order: list[int]) -> int:
    """
    Auto upscale 2K theo luồng chuẩn của Flow:
    - Kích hoạt bằng click UI (để request hợp lệ, tránh 401).
    - Bắt response `upsampleImage` và lấy `encodedImage`.
    - Lưu file 2K theo đúng tên cảnh.
    """
    success = 0
    if not scene_order:
        return 0

    project_id = _extract_flow_project_id_from_url(page.url) or str(_last_flow_client_context.get("projectId", "") or "")
    if not project_id:
        log("Không tìm được project_id để mở gallery upscale 2K.", "WARN")
        return 0

    # Đưa tab chính về gallery để có ngữ cảnh ổn định.
    await _open_flow_gallery_for_project(page, project_id)

    # Mỗi scene giữ 1 tab riêng để request 2K không bị hủy khi chuyển scene.
    # worker_tabs: scene_no -> tab object (để đóng tab khi scene đó xong).
    worker_tabs: dict[int, object] = {}
    queued_scene_to_media: dict[int, str] = {}

    for scene_no in scene_order:
        media_ids = _scene_to_media_ids.get(scene_no, []) or []
        if not media_ids:
            log(f"Upscale 2K: thiếu mediaId cho canh_{scene_no:03d}", "WARN")
            continue
        media_id = str(media_ids[0])

        tab = await page.context.new_page()
        try:
            gallery_url = f"{GOOGLE_FLOW_HOME}/project/{project_id}"
            await tab.goto(gallery_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(0.6)

            if not await _open_flow_edit_for_media(tab, media_id):
                log(f"Upscale 2K: tab riêng không mở được edit cho media {media_id[:8]}...", "WARN")
                await tab.close()
                continue

            await debug_step(
                tab,
                f"flow_upscale_open_edit_scene_{scene_no:03d}",
                job_id=f"canh_{scene_no:03d}",
                extra={"media_id": media_id, "url": tab.url, "mode": "multi_tab"},
            )

            clicked = await _click_upscale_2k_in_edit_page(tab)
            if not clicked:
                log(f"Upscale 2K: tab riêng không click được nút 2K cho canh_{scene_no:03d}", "WARN")
                await tab.close()
                continue

            queued_scene_to_media[scene_no] = media_id
            worker_tabs[scene_no] = tab
            log(f"Upscale 2K queued: canh_{scene_no:03d} (media={media_id[:8]}...) [tab riêng]", "DBG")
        except Exception as e:
            log(f"Upscale 2K exception canh_{scene_no:03d}: {e}", "WARN")
            try:
                await tab.close()
            except Exception:
                pass

    if not queued_scene_to_media:
        return 0

    # Chờ response theo kiểu event-driven:
    # scene nào có encodedImage trước -> lưu ngay -> đóng tab scene đó ngay.
    pending = set(queued_scene_to_media.keys())
    timeout_sec = max(30, GOOGLE_FLOW_UPSCALE_WAIT_TIMEOUT_SEC)
    deadline = time.time() + timeout_sec
    log(
        f"Đã queue {len(queued_scene_to_media)} request 2K (multi-tab), bắt đầu nhận và lưu ngay khi response về...",
        "WAIT",
    )

    while pending and time.time() < deadline:
        done_now = []
        for scene_no in list(pending):
            media_id = queued_scene_to_media.get(scene_no, "")
            if not media_id:
                done_now.append(scene_no)
                continue

            if media_id in _upscale_success_by_media:
                if _save_upscale_image_from_memory(scene_no, media_id):
                    success += 1
                else:
                    log(f"Upscale 2K fail canh_{scene_no:03d}: encodedImage lỗi/nhỏ", "WARN")
                done_now.append(scene_no)

        for scene_no in done_now:
            pending.discard(scene_no)
            tab = worker_tabs.pop(scene_no, None)
            if tab is not None:
                try:
                    await tab.close()
                except Exception:
                    pass

        if pending:
            await asyncio.sleep(0.35)

    if pending:
        left = sorted(list(pending))
        log(f"Upscale 2K timeout, còn thiếu: {left}", "WARN")

    # Dọn tab còn mở (nếu timeout/lỗi).
    for _, tab in list(worker_tabs.items()):
        try:
            await tab.close()
        except Exception:
            pass

    return success


# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

async def main():
    global _run_started_ts
    # Mode Google Flow:
    # - mặc định auto gửi prompt + tải ảnh qua request/response API.
    # - có thể bật manual capture bằng GOOGLE_FLOW_MANUAL_CAPTURE=1.
    if is_google_flow_mode():
        _init_debug_session()
        if GOOGLE_FLOW_KILL_OLD_SESSION_BEFORE_RUN:
            log(
                "Đang bật kill session cũ trước khi chạy (có thể làm mất login nếu Chrome chưa kịp lưu session).",
                "WARN",
            )
            close_old_google_flow_automation_session()
        else:
            log("Giữ nguyên session Chrome cũ (GOOGLE_FLOW_KILL_OLD_SESSION_BEFORE_RUN=0).", "DBG")
        structured_plan = {}
        if GOOGLE_FLOW_MANUAL_CAPTURE:
            log("Đang chạy mode GOOGLE_FLOW (manual capture request/response).", "INFO")
            prompts = []
        else:
            # Ưu tiên parse định dạng structured theo mẫu full-script (character/image + Video blocks).
            structured_plan = parse_structured_story_input(PROMPTS_FILE)
            if structured_plan.get("is_structured"):
                ref_count = len(structured_plan.get("reference_generation_prompts", []))
                video_count = len(structured_plan.get("video_prompts", []))
                log(
                    f"Phát hiện prompt structured: {ref_count} reference + {video_count} video units.",
                    "INFO",
                )
                if GOOGLE_FLOW_MEDIA_MODE == "video":
                    # Mode video: bước chính là chạy video prompts; step tạo ảnh reference xử lý sau.
                    prompts = structured_plan.get("video_prompts", [])
                else:
                    # Mode image: chỉ chạy step reference ảnh.
                    prompts = structured_plan.get("reference_generation_prompts", [])
                if not prompts:
                    log("File structured không có prompt khả dụng để chạy.", "ERR")
                    return
            else:
                prompts = load_prompts_from_file(PROMPTS_FILE)
                if not prompts:
                    log(f"Không có prompt trong {PROMPTS_FILE}.", "ERR")
                    return
                if GOOGLE_FLOW_RANDOM_PROMPTS:
                    prompts = pick_random_prompts(prompts, GOOGLE_FLOW_RANDOM_PROMPTS_COUNT)
                    log(
                        f"Đang chạy mode GOOGLE_FLOW {GOOGLE_FLOW_MEDIA_MODE.upper()} API-only với {len(prompts)} prompt random "
                        f"(target={GOOGLE_FLOW_RANDOM_PROMPTS_COUNT}).",
                        "INFO",
                    )
                else:
                    log(f"Đang chạy mode GOOGLE_FLOW {GOOGLE_FLOW_MEDIA_MODE.upper()} API-only với {len(prompts)} prompt.", "INFO")

            selected_prompts_path = save_selected_prompts_for_session(prompts)
            if selected_prompts_path:
                log(f"Selected prompts file: {selected_prompts_path}", "DBG")
            if structured_plan.get("is_structured"):
                # Lưu bản prompt đã parse vào folder prompts để bạn dễ review/chạy lại.
                structured_video_path = save_prompts_to_prompts_folder(
                    structured_plan.get("video_prompts", []),
                    "structured_video_prompts_compiled.txt",
                )
                if structured_video_path:
                    log(f"Structured video prompts: {structured_video_path}", "DBG")
                structured_ref_path = save_prompts_to_prompts_folder(
                    structured_plan.get("reference_generation_prompts", []),
                    "structured_reference_image_prompts.txt",
                )
                if structured_ref_path:
                    log(f"Structured reference prompts: {structured_ref_path}", "DBG")

            # Theo yêu cầu debug: mặc định KHÔNG xóa output cũ, trừ khi bật cờ môi trường.
            if GOOGLE_FLOW_CLEAR_OUTPUT_BEFORE_RUN:
                reset_output_dir()
            else:
                Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
                log("Giữ nguyên output_images cũ (GOOGLE_FLOW_CLEAR_OUTPUT_BEFORE_RUN=0).", "DBG")

        async with async_playwright() as p:
            # Lưu mọi HAR để đối chiếu request/response giữa các phase.
            har_paths: list[str] = []
            # Giữ browser phase cuối để phục vụ option KEEP_BROWSER_OPEN.
            active_browser = None
            active_trace_started = False
            active_trace_path = _trace_zip_path

            async def _open_phase_browser(profile_dir: str, phase_name: str):
                """Mở 1 Chrome context cho 1 phase (ảnh hoặc video)."""
                har_path = os.path.join(_debug_session_dir, f"session_network_{phase_name}.har")
                browser_ctx = await launch_chrome_context(p, profile_dir=profile_dir, har_path=har_path)
                page_ctx = await browser_ctx.new_page()
                setup_image_network_debug(page_ctx)
                trace_started_ctx = False
                trace_path_ctx = os.path.join(_debug_session_dir, f"playwright_trace_{phase_name}.zip")
                try:
                    await browser_ctx.tracing.start(screenshots=True, snapshots=True, sources=True)
                    trace_started_ctx = True
                    log(f"[{phase_name}] Playwright trace đang ghi: {trace_path_ctx}", "DBG")
                except Exception as e:
                    log(f"[{phase_name}] Không bật được Playwright tracing: {e}", "WARN")
                log(f"[{phase_name}] HAR đang ghi: {har_path}", "DBG")
                return browser_ctx, page_ctx, har_path, trace_started_ctx, trace_path_ctx

            # Chạy theo mode đã chọn.
            if GOOGLE_FLOW_MANUAL_CAPTURE:
                browser, page, har_path, trace_started, trace_path = await _open_phase_browser(
                    PROFILE_DIR,
                    "manual",
                )
                har_paths.append(har_path)
                active_browser = browser
                active_trace_started = trace_started
                active_trace_path = trace_path
                await run_manual_network_capture(page)
                saved_total = 0
            else:
                use_two_chrome = (
                    GOOGLE_FLOW_MEDIA_MODE == "video"
                    and structured_plan.get("is_structured")
                    and GOOGLE_FLOW_SEPARATE_CHROME_FOR_IMAGE_VIDEO
                )

                if use_two_chrome:
                    ref_prompts = structured_plan.get("reference_generation_prompts", [])
                    scene_to_label = structured_plan.get("reference_scene_to_label", {})

                    # ── STEP 1 / Chrome #1: tạo ảnh reference ───────────────────────────
                    browser_img, page_img, har_img, trace_img_started, trace_img_path = await _open_phase_browser(
                        PROFILE_DIR_IMAGE,
                        "image_step",
                    )
                    har_paths.append(har_img)
                    log(
                        f"STEP 1/2: dùng Chrome profile ảnh riêng: {PROFILE_DIR_IMAGE}",
                        "INFO",
                    )
                    reference_saved_total = 0
                    try:
                        if ref_prompts:
                            log(
                                f"STEP 1/2: Tạo {len(ref_prompts)} ảnh reference (character/image) trước khi render video...",
                                "STEP",
                            )
                            reference_saved_total = await run_google_flow_auto_request_response(page_img, ref_prompts)
                            log(f"STEP 1/2: Đã tải {reference_saved_total} ảnh reference theo API map", "OK")
                    finally:
                        try:
                            if trace_img_started:
                                await browser_img.tracing.stop(path=trace_img_path)
                                log(f"[image_step] Trace zip: {trace_img_path}", "DBG")
                        except Exception as e:
                            log(f"[image_step] Không ghi được trace zip: {e}", "WARN")
                        await browser_img.close()

                    rename_report = rename_reference_scene_images(scene_to_label)
                    renamed_count = len(rename_report.get("renamed", []))
                    missing_count = len(rename_report.get("missing", []))
                    log(
                        f"STEP 1/2: Đổi tên ảnh reference -> label: {renamed_count} thành công, {missing_count} thiếu/lỗi",
                        "INFO",
                    )
                    for row in rename_report.get("renamed", []):
                        log(
                            f"  canh_{int(row.get('scene_no', 0)):03d}.png -> {row.get('label', '')}.png",
                            "DBG",
                        )
                    for row in rename_report.get("missing", []):
                        err = row.get("error", "")
                        if err:
                            log(
                                f"  Thiếu/lỗi canh_{int(row.get('scene_no', 0)):03d} -> {row.get('label', '')}: {err}",
                                "WARN",
                            )
                        else:
                            log(
                                f"  Thiếu file canh_{int(row.get('scene_no', 0)):03d}.png để đổi thành {row.get('label', '')}.png",
                                "WARN",
                            )

                    # ── STEP 2 / Chrome #2: render video ────────────────────────────────
                    browser_vid, page_vid, har_vid, trace_vid_started, trace_vid_path = await _open_phase_browser(
                        PROFILE_DIR_VIDEO,
                        "video_step",
                    )
                    har_paths.append(har_vid)
                    active_browser = browser_vid
                    active_trace_started = trace_vid_started
                    active_trace_path = trace_vid_path
                    log(
                        f"STEP 2/2: dùng Chrome profile video riêng: {PROFILE_DIR_VIDEO}",
                        "INFO",
                    )
                    log(
                        f"STEP 2/2: Render {len(prompts)} video units với prompt đã gắn reference labels theo từng unit...",
                        "STEP",
                    )
                    saved_total = await run_google_flow_auto_video_request_response(page_vid, prompts)
                    log(f"STEP 2/2: Đã tải {saved_total} video bằng request/response API", "OK")
                else:
                    # Mode thường: dùng profile legacy như trước để giữ tương thích.
                    browser, page, har_path, trace_started, trace_path = await _open_phase_browser(
                        PROFILE_DIR,
                        "main",
                    )
                    har_paths.append(har_path)
                    active_browser = browser
                    active_trace_started = trace_started
                    active_trace_path = trace_path

                    if GOOGLE_FLOW_MEDIA_MODE == "video":
                        saved_total = await run_google_flow_auto_video_request_response(page, prompts)
                        log(f"Đã tải {saved_total} video bằng request/response API", "OK")
                    else:
                        saved_total = await run_google_flow_auto_request_response(page, prompts)
                        log(f"Đã tải {saved_total} ảnh bằng request/response API", "OK")

            # Lưu log debug dễ đọc + log JSON đầy đủ.
            await wait_pending_api_tasks(timeout_sec=3.0)
            prompt_trace_path = save_prompt_submission_trace()
            if prompt_trace_path:
                log(f"Prompt trace: {prompt_trace_path}", "DBG")
            network_debug_path = save_network_debug()
            if network_debug_path:
                log(f"Network image log: {network_debug_path}", "DBG")
            api_debug_path = save_api_debug()
            if api_debug_path:
                log(f"Network API log: {api_debug_path}", "DBG")
            timeline_path = save_request_response_timeline()
            if timeline_path:
                log(f"Request/Response timeline: {timeline_path}", "DBG")
            upscale_debug_path = save_upscale_debug()
            if upscale_debug_path:
                log(f"Upscale 2K debug: {upscale_debug_path}", "DBG")
            video_error_debug_path = save_video_error_debug()
            if video_error_debug_path:
                log(f"Video error debug: {video_error_debug_path}", "DBG")
            if GOOGLE_FLOW_MEDIA_MODE == "video":
                scene_report_path = save_flow_video_scene_report(prompts)
                if scene_report_path:
                    log(f"Flow video scene report: {scene_report_path}", "DBG")
            hash_report_path = save_download_hash_report()
            if hash_report_path:
                log(f"Download hash report: {hash_report_path}", "DBG")

            try:
                if active_browser and active_trace_started:
                    await active_browser.tracing.stop(path=active_trace_path)
                    log(f"Trace zip: {active_trace_path}", "DBG")
            except Exception as e:
                log(f"Không ghi được trace zip: {e}", "WARN")

            for har_path in har_paths:
                if os.path.exists(har_path):
                    log(f"HAR file: {har_path}", "DBG")

            compare_path = compare_with_previous_session()
            if compare_path:
                log(f"Session compare: {compare_path}", "DBG")
            else:
                log("Chưa có session trước để so sánh", "DBG")

            # Theo yêu cầu: giữ Chrome mở để user kiểm tra sau khi chạy xong.
            if GOOGLE_FLOW_KEEP_BROWSER_OPEN:
                log("Giữ Chrome mở để bạn kiểm tra. Đóng cửa sổ Chrome khi xem xong.", "INFO")
                while True:
                    try:
                        if not active_browser or len(active_browser.pages) == 0:
                            break
                    except Exception:
                        break
                    await asyncio.sleep(2)
            if active_browser:
                await active_browser.close()
        return

    # Cho phép chọn nhanh chế độ test:
    # - y: tự sinh prompt khác nhau mỗi lần để debug
    # - n: dùng prompts.txt như cũ
    use_auto_test = input(
        f"Dùng prompt test tự sinh ({AUTO_TEST_PROMPTS_COUNT} cảnh) để debug? (y/N): "
    ).strip().lower() in {"y", "yes"}

    if use_auto_test:
        prompts = build_auto_test_prompts(AUTO_TEST_PROMPTS_COUNT)
        generated_path = save_generated_prompts(prompts)
        log(f"Đã tạo {len(prompts)} prompt test tự sinh", "INFO")
        log(f"File prompt test: {generated_path}", "PATH")
    else:
        # Ưu tiên dùng pool 100 prompt:
        # Mỗi lần chạy lấy đúng 10 prompt và copy vào prompts.txt.
        prompts, pool_meta = take_prompts_from_pool(PROMPT_BATCH_SIZE)
        if prompts:
            write_prompts_file(prompts, PROMPTS_FILE)
            log(
                f"Lấy {len(prompts)} prompt từ pool ({pool_meta.get('start_index', 0)+1}.."
                f"{pool_meta.get('start_index', 0)+len(prompts)} / {pool_meta.get('total', 0)})",
                "INFO",
            )
            log(f"Đã ghi batch hiện tại vào: {os.path.abspath(PROMPTS_FILE)}", "PATH")
        else:
            # Fallback: nếu chưa có pool thì dùng prompts.txt như cũ.
            if not os.path.exists(PROMPTS_FILE):
                log(f"Không tìm thấy '{PROMPTS_FILE}' và cũng chưa có pool prompt!", "❌")
                notify("Dreamina Auto - LỖI", f"Không tìm thấy {PROMPTS_FILE}")
                return

            with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
                prompts = [ln.strip() for ln in f if ln.strip()]

    if not prompts:
        log("prompts.txt rỗng!", "❌")
        return

    reset_output_dir()

    # Khởi tạo debug session (tạo thư mục debug_sessions/session_TIMESTAMP/)
    _init_debug_session()

    log(f"Tìm thấy {len(prompts)} prompts", "📋")
    log(f"Ảnh lưu vào: {OUTPUT_DIR}/", "📁")

    trace_started = False
    async with async_playwright() as p:
        har_path = os.path.join(_debug_session_dir, "session_network.har")
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            channel="chrome",
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            ignore_default_args=["--enable-automation"],
            accept_downloads=True,
            record_har_path=har_path,
            viewport={"width": VP_WIDTH, "height": VP_HEIGHT},
        )

        page = await browser.new_page()
        setup_image_network_debug(page)
        try:
            await browser.tracing.start(screenshots=True, snapshots=True, sources=True)
            trace_started = True
            log(f"Playwright trace đang ghi: {_trace_zip_path}", "DBG")
            log(f"HAR đang ghi: {har_path}", "DBG")
        except Exception as e:
            log(f"Không bật được Playwright tracing: {e}", "WARN")

        # ══════════════════════════════════════════════
        # BƯỚC 1: Mở trang Home và chờ đăng nhập
        # ══════════════════════════════════════════════
        log("Đang mở Dreamina...", "🌐")
        await page.goto(DREAMINA_HOME, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        await wait_for_login(page)

        # 📸 DEBUG: xác nhận đã login xong
        await debug_step(page, "after_login", extra={"url": page.url})

        # ══════════════════════════════════════════════
        # BƯỚC 2: Vào trang tạo ảnh
        # ══════════════════════════════════════════════
        await navigate_to_image_mode(page)

        # 📸 DEBUG: kiểm tra đã vào đúng trang generate + có prompt box không
        await debug_step(page, "after_navigate_image_mode", extra={"url": page.url})

        print("\n" + "=" * 60)
        print("  Setup model, tỉ lệ, chất lượng theo ý muốn trên browser")
        print("  (Bỏ qua nếu đã setup sẵn)")
        print("=" * 60)
        await asyncio.get_event_loop().run_in_executor(
            None, input, "  Setup xong → Nhấn Enter để bắt đầu tự động: "
        )

        # Reload để đảm bảo đúng mode
        await navigate_to_image_mode(page)
        # Bắt buộc đứng ở generate trước khi chụp baseline, tránh lẫn ảnh cũ.
        if "generate" not in page.url:
            log("Chưa ở generate, ép điều hướng trực tiếp để đồng bộ baseline...", "WARN")
            await _safe_goto(page, DREAMINA_IMAGE)
            await asyncio.sleep(2)
        log(f"URL trước khi scan baseline: {page.url}", "DBG")

        # ══════════════════════════════════════════════
        # BƯỚC 3 — Phase 1: Scroll load hết ảnh cũ → snapshot → gửi hết prompts
        # ══════════════════════════════════════════════
        log("Bắt đầu gửi prompts!\n", "🚀")
        notify("Dreamina Auto", "Bắt đầu gửi prompts...")

        # Theo yêu cầu: từ lúc gửi prompt cuối, chờ cố định rồi tải.
        # Giá trị cấu hình ở hằng số global WAIT_AFTER_LAST_PROMPT_SEC.

        log("Đang scan ảnh hiện có...", "⏳")
        baseline_scroll_trace = await trace_scroll_behavior(page, "before_send_prompts")
        if baseline_scroll_trace:
            log(f"Scroll trace baseline: {baseline_scroll_trace}", "DBG")
        before_all = await capture_stable_baseline_srcs(page, max_rounds=4)
        log(f"Snapshot ổn định: {len(before_all)} ảnh cũ (sẽ bỏ qua)", "💾")

        # 📸 DEBUG: trạng thái trước khi bắt đầu gửi prompts
        await debug_step(page, "before_send_prompts", extra={
            "prompts_count": len(prompts),
            "old_images": len(before_all),
        })

        # Đánh dấu thời điểm bắt đầu run để loại API history quá cũ.
        _run_started_ts = time.time()
        log(
            f"Mốc run_start_ts={int(_run_started_ts)} | lọc history cũ hơn {API_HISTORY_MAX_AGE_SEC}s",
            "DBG",
        )

        for i, prompt in enumerate(prompts):
            # Giữ nhịp gửi đều theo DELAY_SEC, nhưng luôn gửi nhanh nhất có thể
            started_at = time.time()
            scene_no = extract_scene_number(prompt, i + 1)
            log(f"[{i+1}/{len(prompts)}] Gửi: {prompt[:70]}", "📤")
            await type_prompt(page, prompt)
            await send_prompt(page)
            # 📸 DEBUG sau mỗi prompt: thấy input đã clear + spinner bắt đầu
            await debug_step(page, f"sent_prompt_{i+1:02d}",
                             job_id=f"canh_{scene_no:03d}",
                             extra={"prompt_preview": prompt[:80]})
            await capture_prompt_submission_trace(page, i, prompt)
            elapsed = time.time() - started_at
            cooldown = max(0.0, DELAY_SEC - elapsed)
            if cooldown > 0:
                await asyncio.sleep(cooldown)

        # ══════════════════════════════════════════════
        # BƯỚC 3 — Phase 2: Chờ cố định 45 giây sau prompt cuối
        # ══════════════════════════════════════════════
        log(
            f"\nĐã gửi hết {len(prompts)} prompts — chờ {WAIT_AFTER_LAST_PROMPT_SEC}s rồi bắt đầu tải...",
            "⏳",
        )
        notify("Dreamina Auto", f"Đã gửi xong prompt, chờ {WAIT_AFTER_LAST_PROMPT_SEC}s trước khi tải")

        wait_start = time.time()
        for remaining in range(WAIT_AFTER_LAST_PROMPT_SEC, 0, -1):
            if remaining % 5 == 0:
                log(f"  Còn {remaining}s...", "WAIT")
            await asyncio.sleep(1)

        # Scroll để lazy-load ảnh chưa hiện trong viewport
        log("Scroll để load hết ảnh mới...", "🔄")
        await scroll_to_load_all(page)
        await asyncio.sleep(2)
        before_download_scroll_trace = await trace_scroll_behavior(page, "before_download")
        if before_download_scroll_trace:
            log(f"Scroll trace before_download: {before_download_scroll_trace}", "DBG")

        # 📸 DEBUG: sau chờ render 60s — kiểm tra ảnh mới xuất hiện chưa
        waited_sec = int(time.time() - wait_start)
        await debug_step(page, "after_wait_render", extra={"waited_sec": waited_sec})
        after_render_dump = await dump_detailed_dom(page, "after_wait_render")
        if after_render_dump:
            log(
                "DOM dump after_wait_render: "
                + ", ".join(os.path.basename(p) for p in after_render_dump.values()),
                "DBG",
            )

        # ══════════════════════════════════════════════
        # BƯỚC 3 — Phase 3: Lấy danh sách ảnh mới & tải về
        # Logic: current_srcs - before_all = chỉ ảnh MỚI sinh ra
        # _filter_srcs() lọc tiếp icon/logo/avatar/spinner
        # ══════════════════════════════════════════════
        # Lấy ảnh mới theo THỨ TỰ DOM (không dùng set) để giảm lệch map prompt->ảnh.
        gallery_entries = await get_current_image_entries(page)
        all_new = _ordered_new_srcs(gallery_entries, before_all)
        log(f"\nTìm thấy {len(all_new)} ảnh mới. Tải về...", "📥")
        notify("Dreamina Auto", "Đang tải ảnh về...")
        expected_total = len(prompts) * IMAGES_PER_PROMPT
        if len(all_new) > expected_total:
            log(
                f"Cảnh báo: ảnh mới ({len(all_new)}) > kỳ vọng ({expected_total}), có thể lẫn ảnh cũ.",
                "WARN",
            )

        gallery_snapshot_path = save_gallery_snapshot(gallery_entries, all_new)
        if gallery_snapshot_path:
            log(f"DOM snapshot: {gallery_snapshot_path}", "DBG")
        virtualization_report = await diagnose_dom_virtualization(page, "before_download")
        if virtualization_report:
            log(f"Virtualization report: {virtualization_report}", "DBG")
        before_download_dump = await dump_detailed_dom(page, "before_download", new_srcs=all_new)
        if before_download_dump:
            log(
                "DOM dump before_download: "
                + ", ".join(os.path.basename(p) for p in before_download_dump.values()),
                "DBG",
            )

        # 📸 DEBUG: số ảnh tìm được vs mong đợi — debug thiếu ảnh
        await debug_step(page, "before_download", extra={
            "new_images_found": len(all_new),
            "gallery_entries":  len(gallery_entries),
            "expected":         expected_total,
            "diff":             len(all_new) - expected_total,
            "snapshot_file":    os.path.basename(gallery_snapshot_path) if gallery_snapshot_path else "",
            "virtualization":   os.path.basename(virtualization_report) if virtualization_report else "",
            "dom_dump_full":    os.path.basename(before_download_dump.get("full_html", "")) if before_download_dump else "",
            "dom_dump_images":  os.path.basename(before_download_dump.get("images_txt", "")) if before_download_dump else "",
        })

        saved_total = 0
        prompt_scene_order = [extract_scene_number(p, i + 1) for i, p in enumerate(prompts)]
        scene_to_prompt_index = {sc: (i + 1) for i, sc in enumerate(prompt_scene_order)}
        saved_scenes = set()

        log(f"Chỉ tải ảnh đầu tiên (ảnh #1) mỗi prompt → tổng {len(prompts)} ảnh", "🎯")
        if STRICT_API_ONLY:
            log("Chế độ an toàn: chỉ tải theo API map (không fallback DOM)", "DBG")

        # Chờ/poll thêm để API map scene->image đủ ổn định trước khi tải.
        api_scene_map, missing_scenes = await build_api_scene_map_with_retry(
            prompts,
            timeout_sec=API_MAP_POLL_TIMEOUT_SEC,
            interval_sec=API_MAP_POLL_INTERVAL_SEC,
        )
        log(
            f"API map bắt được {len(api_scene_map)}/{len(prompts)} cảnh"
            + (f" | thiếu: {missing_scenes}" if missing_scenes else ""),
            "DBG",
        )

        for scene_no in prompt_scene_order:
            candidate_urls = get_scene_candidate_urls(scene_no, api_scene_map.get(scene_no, ""))
            if not candidate_urls:
                log(f"  Thiếu URL API cho canh_{scene_no:03d} -> bỏ qua", "WARN")
                continue

            fname = f"canh_{scene_no:03d}.png"
            filepath = os.path.join(OUTPUT_DIR, fname)
            downloaded = False
            last_error = ""
            for src in candidate_urls:
                try:
                    resp = await page.context.request.get(src, timeout=30000)
                    if not resp.ok:
                        continue
                    body = await resp.body()
                    if len(body) <= 5000:
                        continue
                    with open(filepath, "wb") as f:
                        f.write(body)
                    size_kb = len(body) // 1024
                    log(f"  {fname} ({size_kb}KB) [api-map]", "✅")
                    try:
                        sha = _sha256_file(filepath)
                    except Exception:
                        sha = ""
                    _download_hash_records.append({
                        "filename": fname,
                        "prompt_num": scene_no,
                        "prompt_index": scene_to_prompt_index.get(scene_no, 0),
                        "img_num": 1,
                        "src": src,
                        "method": "request.get_api_map",
                        "size_bytes": len(body),
                        "sha256": sha,
                    })
                    saved_total += 1
                    saved_scenes.add(scene_no)
                    downloaded = True
                    break
                except Exception as e:
                    last_error = str(e)

            if not downloaded:
                if last_error:
                    log(f"  Không tải được {fname} theo API: {last_error}", "WARN")
                else:
                    log(f"  Không tải được {fname} theo API (URL không hợp lệ hoặc file rỗng)", "WARN")

        # Fallback DOM có thể nhầm ảnh cũ khi trang có lịch sử dài.
        # Vì vậy mặc định bật STRICT_API_ONLY để ưu tiên độ chính xác scene.
        if not STRICT_API_ONLY:
            n = IMAGES_PER_PROMPT  # = 4, Dreamina sinh 4 ảnh/prompt
            map_newest_first = True
            if map_newest_first:
                log("Map cảnh theo thứ tự UI mới nhất trước (group#1 -> prompt cuối)", "DBG")

            for idx, src in enumerate(all_new):
                prompt_idx = idx // n       # Prompt index: 0,0,0,0,1,1,1,1,...
                prompt_num = prompt_idx + 1 # Prompt thứ mấy: 1,1,1,1,2,2,2,2,...
                img_num    = idx % n + 1    # Ảnh thứ mấy trong prompt: 1,2,3,4,...

                if prompt_idx >= len(prompts):
                    log(f"  Bỏ qua ảnh ngoài phạm vi prompt (idx={idx})", "WARN")
                    continue

                target_prompt_idx = (len(prompts) - 1 - prompt_idx) if map_newest_first else prompt_idx
                if target_prompt_idx < 0 or target_prompt_idx >= len(prompts):
                    log(f"  Bỏ qua ảnh vì không map được prompt (idx={idx})", "WARN")
                    continue
                target_prompt_num = target_prompt_idx + 1
                scene_no = extract_scene_number(prompts[target_prompt_idx], target_prompt_num)
                if scene_no in saved_scenes:
                    continue

                # ── CHỈ LẤY ẢNH ĐẦU TIÊN MỖI PROMPT ──
                if img_num != 1:
                    log(f"  Bỏ qua ảnh #{img_num} của prompt {prompt_num}", "⏭️")
                    continue

                # Đặt tên theo đúng số cảnh có trong prompt, ví dụ CẢNH 030 -> canh_030.png
                fname    = f"canh_{scene_no:03d}.png"
                filepath = os.path.join(OUTPUT_DIR, fname)
                try:
                    resp = await page.context.request.get(src, timeout=30000)
                    if resp.ok:
                        body = await resp.body()
                        if len(body) > 5000:
                            with open(filepath, "wb") as f:
                                f.write(body)
                            size_kb = len(body) // 1024
                            log(f"  {fname} ({size_kb}KB)", "✅")
                            try:
                                sha = _sha256_file(filepath)
                            except Exception:
                                sha = ""
                            _download_hash_records.append({
                                "filename": fname,
                                "prompt_num": scene_no,
                                "prompt_index": target_prompt_num,
                                "img_num": img_num,
                                "src": src,
                                "method": "request.get",
                                "size_bytes": len(body),
                                "sha256": sha,
                            })
                            saved_total += 1
                            continue
                    # Fallback: chụp screenshot element ảnh
                    el = page.locator(f'img[src="{src}"]').first
                    if await el.count() > 0:
                        await el.screenshot(path=filepath)
                        if os.path.exists(filepath) and os.path.getsize(filepath) > 5000:
                            log(f"  {fname} (screenshot)", "✅")
                            try:
                                sha = _sha256_file(filepath)
                            except Exception:
                                sha = ""
                            _download_hash_records.append({
                                "filename": fname,
                                "prompt_num": scene_no,
                                "prompt_index": target_prompt_num,
                                "img_num": img_num,
                                "src": src,
                                "method": "element.screenshot",
                                "size_bytes": os.path.getsize(filepath),
                                "sha256": sha,
                            })
                            saved_total += 1
                except Exception as e:
                    log(f"  Lỗi tải {fname}: {e}", "⚠️")
                    # 📸 DEBUG lỗi: chụp ngay tại thời điểm tải thất bại
                    await debug_step(page, f"error_download_{fname}",
                                     job_id=f"canh_{scene_no:03d}",
                                     is_error=True,
                                     extra={"error": str(e), "src": src[:80]})

        missing_after_download = [sc for sc in prompt_scene_order if sc not in saved_scenes]
        if missing_after_download:
            log(f"Cảnh chưa tải được: {missing_after_download}", "WARN")

        log(f"Đã lưu {saved_total} ảnh tổng cộng", "✅")
        notify("Dreamina Auto", f"Xong! {saved_total} ảnh đã lưu")

        # Ghi báo cáo debug chi tiết cuối phiên
        await wait_pending_api_tasks(timeout_sec=3.0)
        prompt_trace_path = save_prompt_submission_trace()
        if prompt_trace_path:
            log(f"Prompt trace: {prompt_trace_path}", "DBG")
        network_debug_path = save_network_debug()
        if network_debug_path:
            log(f"Network image log: {network_debug_path}", "DBG")
        api_debug_path = save_api_debug()
        if api_debug_path:
            log(f"Network API log: {api_debug_path}", "DBG")
        timeline_path = save_request_response_timeline()
        if timeline_path:
            log(f"Request/Response timeline: {timeline_path}", "DBG")
        upscale_debug_path = save_upscale_debug()
        if upscale_debug_path:
            log(f"Upscale 2K debug: {upscale_debug_path}", "DBG")
        video_error_debug_path = save_video_error_debug()
        if video_error_debug_path:
            log(f"Video error debug: {video_error_debug_path}", "DBG")
        if is_google_flow_mode() and GOOGLE_FLOW_MEDIA_MODE == "video":
            scene_report_path = save_flow_video_scene_report(prompts)
            if scene_report_path:
                log(f"Flow video scene report: {scene_report_path}", "DBG")
        hash_report_path = save_download_hash_report()
        if hash_report_path:
            log(f"Download hash report: {hash_report_path}", "DBG")

        # ── Tổng kết ──
        print("\n" + "=" * 60)
        log("HOÀN THÀNH!", "🎉")
        log(f"Tổng prompts : {len(prompts)}")
        log(f"Ảnh tại      : {OUTPUT_DIR}/")
        log(f"Debug log    : {_debug_session_dir}/debug_log.json")
        print("=" * 60)

        input("\nNhấn Enter để đóng browser...")
        try:
            if trace_started:
                await browser.tracing.stop(path=_trace_zip_path)
                log(f"Trace zip: {_trace_zip_path}", "DBG")
        except Exception as e:
            log(f"Không ghi được trace zip: {e}", "WARN")

        compare_path = compare_with_previous_session()
        if compare_path:
            log(f"Session compare: {compare_path}", "DBG")
        else:
            log("Chưa có session trước để so sánh", "DBG")

        if os.path.exists(har_path):
            log(f"HAR file: {har_path}", "DBG")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
