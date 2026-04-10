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
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

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
PROFILE_DIR    = os.path.expanduser("~/dreamina_playwright_profile")
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


def _init_debug_session():
    """
    Tạo thư mục session debug mới dựa theo timestamp.
    Tự động xóa sessions cũ nếu vượt quá DEBUG_MAX_SESSIONS.
    """
    global _debug_session_dir, _debug_steps, _step_counter
    global _network_events, _network_req_start, _prompt_submission_trace, _download_hash_records
    global _trace_zip_path
    global _api_events, _api_req_meta, _scene_to_task_ids, _task_to_image_urls, _scene_to_image_urls
    global _submit_to_scene, _trusted_submit_ids, _run_started_ts, _pending_api_tasks
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
        "submit", "create", "query",
    ]
    return any(k in low for k in keys)


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
    trusted_submit_ids: set | None = None,
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
        if not (_looks_like_api_url(url) or meta.get("scene_numbers")):
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
        if body_json is not None:
            _collect_task_ids_from_obj(body_json, task_ids)
            _collect_urls_from_obj(body_json, image_urls)

            # Parse chuyên biệt cho get_history_by_ids (scene -> cover_url)
            if "/get_history_by_ids" in url:
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

        _api_events.append({
            "type": "api_response",
            "ts": ts,
            "url": url,
            "status": response.status,
            "resource_type": request.resource_type,
            "scene_numbers": meta.get("scene_numbers", []),
            "task_ids": task_ids,
            "image_urls_count": len(image_urls),
            "image_urls_sample": image_urls[:12],
            "request_post_data_sample": (meta.get("post_data", "") or "")[:3000],
            "response_body_sample": body_sample[:3000] if body_sample else "",
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

    def on_request(request):
        req_id = id(request)
        ts = time.time()
        _network_req_start[req_id] = ts
        if request.resource_type == "image" or _looks_like_image_url(request.url):
            _network_events.append({
                "type": "request",
                "ts": ts,
                "method": request.method,
                "resource_type": request.resource_type,
                "url": request.url,
            })
        # Log request API để map prompt -> task
        if request.resource_type in {"fetch", "xhr"} and _looks_like_api_url(request.url):
            post_data = ""
            try:
                post_data = request.post_data or ""
            except Exception:
                post_data = ""
            scenes = _extract_scene_numbers_from_text(post_data)
            _api_req_meta[req_id] = {
                "ts": ts,
                "url": request.url,
                "method": request.method,
                "post_data": post_data,
                "scene_numbers": scenes,
            }
            _api_events.append({
                "type": "api_request",
                "ts": ts,
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "scene_numbers": scenes,
                "post_data_sample": post_data[:3000],
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

        is_image_like = (
            request.resource_type == "image"
            or _looks_like_image_url(request.url)
            or "image" in (content_type or "").lower()
        )
        if is_image_like:
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

    def on_request_failed(request):
        ts = time.time()
        if request.resource_type == "image" or _looks_like_image_url(request.url):
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
        "submit_to_scene": _submit_to_scene,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
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

    # ── Bước 2: Nhấn Enter để gửi ──
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.25)
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


async def dump_detailed_dom(page, stage: str, new_srcs: list | None = None) -> dict:
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


# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

async def main():
    global _run_started_ts
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
