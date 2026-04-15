#!/usr/bin/env python3
from __future__ import annotations
"""
parallel_runner.py
──────────────────
Script điều phối chạy nhiều kịch bản song song.

Flow:
  1. Đọc config/video_workers.json
  2. Chrome IMAGE: tạo ảnh reference cho TỪNG kịch bản (tuần tự)
     - Kịch bản nào xong ảnh → unlock Chrome Video tương ứng chạy ngay (pipeline)
  3. Chrome VIDEO (N workers): mỗi worker chạy 1 kịch bản song song
     - Đọc scenarios/<ten_kich_ban>/prompts.txt
     - Output vào scenarios/<ten_kich_ban>/output/

Cách dùng:
  python3 parallel_runner.py                    → chạy tất cả kịch bản
  python3 parallel_runner.py --dry-run          → chỉ in config, không chạy
  python3 parallel_runner.py --scenario A B     → chỉ chạy kịch bản A và B
  python3 parallel_runner.py --video-only       → bỏ qua bước tạo ảnh (ảnh đã có sẵn)

Giải quyết vấn đề state toàn cục:
  - Mỗi video worker chạy trong subprocess riêng (multiprocessing)
  - Tránh conflict biến global của dreamina.py giữa các worker
  - Mỗi subprocess import dreamina.py riêng → state độc lập
"""

import asyncio
import json
import os
import sys
import time
import socket
import argparse
import subprocess
import tempfile
import random
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse
from playwright.async_api import async_playwright


# ── Constants ─────────────────────────────────────────────────────────────────
# Đường dẫn tương đối so với file này (để hoạt động trên cả máy khác)
_SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH      = os.path.join(_SCRIPT_DIR, "config", "video_workers.json")
GOOGLE_FLOW_HOME = "https://labs.google/fx/vi/tools/flow"
VIEWPORT         = {"width": 1920, "height": 1080}

# Stagger delay giữa các Chrome Video để tránh khởi động đồng loạt (giây)
WORKER_STAGGER_SEC = 5
# Retry tạo ảnh reference ở STEP 1 (thiếu bất kỳ ảnh nào sẽ thử lại đến ngưỡng này).
IMAGE_REFERENCE_MAX_ATTEMPTS = 3
# Video có <=2 biến thể sẽ được thử tạo bổ sung thêm (số vòng tối đa).
VIDEO_LOW_VARIANT_MAX_RETRY_ROUNDS = 3
# Nếu một scene không tạo được bất kỳ biến thể nào quá ngưỡng này thì fail scene.
VIDEO_ZERO_VARIANT_MAX_FAILS = 3
# Upload/preload reference fail: cho phép quay lại tạo ảnh tối đa 2 vòng.
REFERENCE_REGEN_MAX_ROUNDS = 2
# Nếu 1 worker có N vòng chạy liên tiếp không tải thêm được bất kỳ video nào
# thì trả kịch bản về hàng đợi để worker khác (đang rảnh) nhận.
VIDEO_FAILOVER_CONSECUTIVE_ZERO_DOWNLOAD_ROUNDS = 3
# Mã thoát subprocess để báo orchestrator kích hoạt failover worker.
VIDEO_WORKER_FAILOVER_EXIT_CODE = 88
# Worker bị failover sẽ nghỉ tạm để tránh nhận lại đúng kịch bản vừa fail ngay lập tức.
WORKER_FAILOVER_COOLDOWN_SEC = 30 * 60
# Proxy health-check trước khi giao worker video.
PROXY_PRECHECK_MAX_ATTEMPTS = 2
PROXY_PRECHECK_RETRY_DELAY_SEC = 2.0
PROXY_OPEN_CHECK_TIMEOUT_SEC = 1.2
PROXY_HTTP_CHECK_TIMEOUT_SEC = 8
PROXY_TEST_URLS = [
    "https://labs.google/fx/vi/tools/flow",
    "https://accounts.google.com/",
    "https://api.ipify.org?format=json",
]


# ── Logging ─────────────────────────────────────────────────────────────────
def log(msg: str, level: str = "INFO", worker_id: str = ""):
    """In log có timestamp, level và worker_id."""
    now = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{worker_id}]" if worker_id else "[MAIN]"
    print(f"[{now}] [{level:<5}] {prefix} {msg}")


async def _sleep_jitter(base_sec: float, low: float = 0.85, high: float = 1.2, floor: float = 0.2):
    """
    Sleep có jitter nhẹ để tránh nhịp cố định tuyệt đối.
    """
    b = max(float(floor), float(base_sec))
    lo = min(max(low, 0.5), 1.0)
    hi = max(high, lo)
    await asyncio.sleep(max(float(floor), random.uniform(b * lo, b * hi)))


# ── Config helpers ─────────────────────────────────────────────────────────
def load_config() -> dict:
    """Đọc config từ config/video_workers.json."""
    if not os.path.exists(CONFIG_PATH):
        log(f"Không tìm thấy config: {CONFIG_PATH}", "ERR")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_proxy_url(proxy_str: str | None) -> dict | None:
    """
    Parse proxy URL mixed mode:
    - socks5://127.0.0.1:11009
    - socks5://user:pass@host:port
    - http://user:pass@host:port
    - http://host:port
    """
    if not proxy_str:
        return None
    try:
        u = urlparse(proxy_str)
        if not u.scheme or not u.hostname or not u.port:
            return None
        return {
            "scheme": (u.scheme or "").lower(),
            "host": u.hostname,
            "port": int(u.port),
            "username": unquote(u.username or ""),
            "password": unquote(u.password or ""),
            "raw": proxy_str,
        }
    except Exception as e:
        log(f"parse_proxy_url fail for '{proxy_str}': {e}", "WARN")
        return None


def parse_proxy(proxy_str: str | None) -> dict | None:
    """
    Trả về dict đúng format Playwright; backward-compatible với worker cũ.
    """
    p = parse_proxy_url(proxy_str)
    if not p:
        return None
    cfg = {"server": f"{p['scheme']}://{p['host']}:{p['port']}"}
    if p["username"]:
        cfg["username"] = p["username"]
        cfg["password"] = p["password"]
    return cfg


def _parse_proxy_endpoint(proxy_str: str | None) -> tuple[str, int] | None:
    """
    Parse proxy endpoint từ chuỗi mixed mode.
    """
    p = parse_proxy_url(proxy_str)
    if not p:
        return None
    return p["host"], p["port"]


def _is_tcp_open(host: str, port: int, timeout_sec: float = PROXY_OPEN_CHECK_TIMEOUT_SEC) -> bool:
    """Kiểm tra cổng TCP có mở hay không."""
    sock = socket.socket()
    sock.settimeout(timeout_sec)
    try:
        sock.connect((host, int(port)))
        return True
    except Exception:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _curl_proxy_args(proxy_str: str) -> list[str]:
    """
    Chuẩn hóa cách curl dùng proxy.
    socks5 -> socks5h để DNS đi qua proxy
    http/https -> --proxy trực tiếp
    """
    p = parse_proxy_url(proxy_str)
    if not p:
        return []

    if p["scheme"].startswith("socks5"):
        auth = ""
        if p["username"]:
            auth = f"{p['username']}:{p['password']}@"
        return ["--proxy", f"socks5h://{auth}{p['host']}:{p['port']}"]

    auth = ""
    if p["username"]:
        auth = f"{p['username']}:{p['password']}@"
    return ["--proxy", f"{p['scheme']}://{auth}{p['host']}:{p['port']}"]


def _probe_url_through_proxy(
    proxy_str: str,
    url: str,
    timeout_sec: int = PROXY_HTTP_CHECK_TIMEOUT_SEC,
) -> tuple[bool, str]:
    """
    Probe HTTPS tunnel thực tới URL đích.
    """
    try:
        cmd = [
            "curl",
            "-I",
            "-sS",
            "-L",
            "--max-time",
            str(int(timeout_sec)),
            "--connect-timeout",
            "4",
            *(_curl_proxy_args(proxy_str)),
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        ok = proc.returncode == 0 and ("HTTP/" in out or "location:" in out.lower())
        return ok, out[-500:]
    except Exception as e:
        return False, str(e)


def _probe_proxy_tunnel(proxy_str: str, worker_id: str = "") -> tuple[bool, str]:
    """
    Pass khi proxy usable với URL quan trọng, không chỉ ipify.
    """
    for url in PROXY_TEST_URLS:
        ok, detail = _probe_url_through_proxy(proxy_str, url)
        log(f"proxy_probe worker={worker_id} proxy={proxy_str} url={url} ok={ok}", "INFO")
        if ok:
            return True, f"usable via {url}"
    return False, f"all test urls failed for proxy={proxy_str}"


def build_proxy_candidates(worker: dict) -> list[dict]:
    """
    Chọn candidate theo policy:
    - HTTP proxy thật: thử trực tiếp trước, fail thì fallback local bridge.
    - Worker cũ: local bridge như hiện tại.
    """
    proxy_local = str(worker.get("proxy", "") or "").strip()
    proxy_real = str(worker.get("proxy_real", "") or "").strip()
    out: list[dict] = []

    real_parsed = parse_proxy_url(proxy_real)
    local_parsed = parse_proxy_url(proxy_local)

    if real_parsed and real_parsed["scheme"] in {"http", "https"}:
        out.append({"name": "direct_http_real", "proxy": proxy_real})
    if local_parsed:
        out.append({"name": "local_bridge", "proxy": proxy_local})
    elif real_parsed:
        out.append({"name": "direct_real", "proxy": proxy_real})

    seen = set()
    uniq = []
    for item in out:
        proxy = item.get("proxy")
        if not proxy or proxy in seen:
            continue
        seen.add(proxy)
        uniq.append(item)
    return uniq


def _restart_proxy_bridge() -> bool:
    """
    Khởi động/khôi phục proxy bridge qua script sẵn có.
    """
    try:
        cmd = [sys.executable, os.path.join(_SCRIPT_DIR, "proxy_bridge.py"), "start"]
        proc = subprocess.run(cmd, cwd=_SCRIPT_DIR, capture_output=True, text=True, timeout=45)
        if proc.returncode != 0:
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            log(f"proxy_bridge start lỗi (rc={proc.returncode}): {out or err}", "WARN")
            return False
        return True
    except Exception as e:
        log(f"Không thể restart proxy bridge: {e}", "WARN")
        return False


def _ensure_worker_proxy_ready(worker: dict) -> bool:
    """
    Đảm bảo worker có ít nhất 1 proxy candidate usable thật sự.
    """
    worker_id = str(worker.get("worker_id", ""))
    candidates = build_proxy_candidates(worker)
    if not candidates:
        return True

    for cand in candidates:
        proxy_str = str(cand.get("proxy", "") or "").strip()
        endpoint = _parse_proxy_endpoint(proxy_str)
        if not endpoint:
            log(f"Worker {worker_id}: candidate proxy parse fail: {proxy_str}", "WARN")
            continue

        host, port = endpoint
        is_local = host in ("127.0.0.1", "localhost")
        tcp_ok = _is_tcp_open(host, port) if is_local else True

        if not tcp_ok and cand.get("name") == "local_bridge":
            log(f"Worker {worker_id}: local bridge chưa mở -> restart bridge.", "WARN")
            _restart_proxy_bridge()
            time.sleep(PROXY_PRECHECK_RETRY_DELAY_SEC)
            tcp_ok = _is_tcp_open(host, port)

        if not tcp_ok:
            log(f"Worker {worker_id}: {cand.get('name')} tcp not open: {proxy_str}", "WARN")
            continue

        tunnel_ok, detail = _probe_proxy_tunnel(proxy_str, worker_id=worker_id)
        log(
            f"Worker {worker_id}: precheck candidate={cand.get('name')} tcp={tcp_ok} "
            f"tunnel={tunnel_ok} detail={detail}",
            "INFO",
        )
        if tunnel_ok:
            worker["_selected_proxy_name"] = cand.get("name")
            worker["_selected_proxy"] = proxy_str
            return True

    log(f"Worker {worker_id}: không có proxy candidate nào usable.", "ERR")
    return False


def expand_path(p: str) -> str:
    """Mở rộng ~ thành đường dẫn thật của user. Dùng đường dẫn tương đối theo project root nếu cần."""
    p_expanded = os.path.expanduser(p)
    if not os.path.isabs(p_expanded):
        p_expanded = os.path.join(_SCRIPT_DIR, p_expanded)
    return p_expanded


def load_scenario_prompts(scenario_dir: str) -> list[str]:
    """
    Đọc prompts.txt trong thư mục kịch bản.
    Trả về danh sách prompt (mỗi dòng 1 prompt), bỏ dòng trống và comment #.
    """
    prompts_path = os.path.join(scenario_dir, "prompts.txt")
    if not os.path.exists(prompts_path):
        log(f"Không tìm thấy: {prompts_path}", "WARN")
        return []
    with open(prompts_path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]


def build_worker_env(worker_id: str, worker_index: int) -> dict:
    """
    Build env riêng cho mỗi video worker để nhịp gửi/poll khác nhau nhẹ.

    Mục tiêu:
    - Giảm việc 5 worker có pattern thời gian giống hệt nhau.
    - Không đổi mạnh tốc độ tổng (chỉ dao động nhỏ, bảo thủ).
    """
    env = os.environ.copy()

    # Seed ổn định theo worker để mỗi worker có "tempo" riêng giữa các lần chạy.
    env["FLOW_HUMANIZE_ENABLED"] = env.get("FLOW_HUMANIZE_ENABLED", "1")
    env["FLOW_HUMANIZE_SEED"] = env.get("FLOW_HUMANIZE_SEED", f"{worker_id}_seed_{worker_index}")

    # Dao động quanh nhịp hiện tại, kéo giãn thêm để bot "thở" nhiều hơn tránh bão rate limit.
    env.setdefault("FLOW_SEND_JITTER_MIN", "1.15")
    env.setdefault("FLOW_SEND_JITTER_MAX", "1.75")
    env.setdefault("FLOW_SOFT_PAUSE_PROB", "0.20")
    env.setdefault("FLOW_SOFT_PAUSE_MIN_SEC", "2.5")
    env.setdefault("FLOW_SOFT_PAUSE_MAX_SEC", "5.0")

    # Video: thời gian "suy nghĩ" trước khi gửi và poll lệch nhẹ quanh 10s.
    env.setdefault("FLOW_VIDEO_PRE_SEND_BASE_SEC", "1.2")
    env.setdefault("FLOW_VIDEO_POLL_BASE_SEC", "10.0")
    env.setdefault("FLOW_VIDEO_POLL_JITTER_SEC", "2.0")
    # Upload reference: lần đầu + retry thêm 2 lần.
    env.setdefault("GOOGLE_FLOW_VIDEO_PRELOAD_MAX_ATTEMPTS", "3")
    # Bật strict reference: thiếu/attach fail token nào thì không gửi prompt video.
    env.setdefault("GOOGLE_FLOW_VIDEO_REQUIRE_REFERENCE_UPLOAD", "1")
    # Hỗ trợ prompt có 4 token reference (ví dụ character1/2 + image1/2).
    env.setdefault("GOOGLE_FLOW_VIDEO_MAX_REFERENCES_PER_PROMPT", "4")
    return env


def _collect_scene_variant_count(output_dir: str, scene_no: int) -> int:
    """
    Đếm số file mp4 đã có của một scene:
    - canh_001.mp4
    - canh_001_v2.mp4
    - canh_001_v3.mp4
    ...
    """
    prefix = f"canh_{scene_no:03d}"
    count = 0
    for p in Path(output_dir).glob(f"{prefix}*.mp4"):
        name = p.name.lower()
        if name == f"{prefix}.mp4" or name.startswith(f"{prefix}_v"):
            count += 1
    return count


def _validate_reference_labels(output_dir: str, scene_to_label: dict) -> list[str]:
    """
    Kiểm tra ảnh reference đã có đủ theo label hay chưa.
    Trả về danh sách label còn thiếu (character1, character2, image1...).
    """
    expected_labels = sorted(set(str(v).strip() for v in (scene_to_label or {}).values() if str(v).strip()))
    missing = []
    for label in expected_labels:
        path = os.path.join(output_dir, f"{label}.png")
        if not os.path.exists(path):
            missing.append(label)
    return missing


def _reset_reference_attempt_files(output_dir: str, scene_to_label: dict) -> None:
    """
    Xóa các file reference mục tiêu trước mỗi attempt để tránh dính file cũ.

    Lý do:
    - Nếu lần trước có đủ file nhưng lần này generate thiếu 1 ảnh,
      file cũ còn lại có thể làm hệ thống hiểu nhầm là "đủ ảnh".
    - Bắt buộc mỗi attempt phải sinh ra bộ file mới hoàn toàn.
    """
    # Xóa file scene tạm của step image: canh_901.png, canh_902.png...
    for scene_no in (scene_to_label or {}).keys():
        try:
            p = os.path.join(output_dir, f"canh_{int(scene_no):03d}.png")
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            continue

    # Xóa file label cuối cùng: character1.png, image1.png...
    for label in set(str(v).strip() for v in (scene_to_label or {}).values() if str(v).strip()):
        try:
            p = os.path.join(output_dir, f"{label}.png")
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            continue


async def _generate_reference_images_with_retry(
    browser_ctx,
    dreamina,
    structured_plan: dict,
    scenario_name: str,
    output_dir: str,
    max_attempts: int = IMAGE_REFERENCE_MAX_ATTEMPTS,
) -> bool:
    """
    Tạo ảnh reference với retry:
    - Nếu thiếu bất kỳ label reference nào thì retry.
    - Hết max_attempts vẫn thiếu -> fail.
    """
    ref_prompts = structured_plan.get("reference_generation_prompts", []) or []
    scene_to_label = structured_plan.get("reference_scene_to_label", {}) or {}
    if not ref_prompts:
        log("Không có reference prompt, bỏ qua bước ảnh.", "INFO", scenario_name)
        return True

    for attempt in range(1, max(1, int(max_attempts)) + 1):
        page = await browser_ctx.new_page()
        try:
            # Ràng buộc bắt buộc: mỗi vòng attempt phải là kết quả mới 100%,
            # không được tái dùng file cũ vì sẽ che mất lỗi thiếu ảnh.
            _reset_reference_attempt_files(output_dir, scene_to_label)

            # Tạo session debug mới cho mỗi vòng để dễ đọc log theo từng lần retry.
            dreamina._init_debug_session()
            dreamina.setup_image_network_debug(page)

            log(
                f"Tạo {len(ref_prompts)} ảnh reference (lần {attempt}/{max_attempts})...",
                "INFO",
                scenario_name,
            )
            saved = await dreamina.run_google_flow_auto_request_response(page, ref_prompts)
            log(f"Đã tải {saved} ảnh reference.", "OK", scenario_name)

            rename_report = dreamina.rename_reference_scene_images(scene_to_label)
            renamed = len(rename_report.get("renamed", []))
            missing_rows = rename_report.get("missing", []) or []
            missing_labels = _validate_reference_labels(output_dir, scene_to_label)
            log(
                f"Đổi tên: {renamed} OK, {len(missing_rows)} thiếu/lỗi, "
                f"thiếu label={missing_labels}",
                "INFO",
                scenario_name,
            )

            # Điều kiện pass: không còn missing theo report và đủ file label trong output.
            expected_count = len(ref_prompts)
            if (
                int(saved) >= expected_count
                and (len(missing_rows) == 0)
                and (len(missing_labels) == 0)
            ):
                return True

            if attempt < max_attempts:
                log(
                    f"Thiếu ảnh reference sau lần {attempt}, sẽ retry lại toàn bộ.",
                    "WARN",
                    scenario_name,
                )
                await asyncio.sleep(1.5)
            else:
                log(
                    "Đã retry tối đa nhưng vẫn thiếu ảnh reference. Đánh dấu kịch bản lỗi.",
                    "ERR",
                    scenario_name,
                )
                return False
        except Exception as e:
            log(f"Lỗi tạo ảnh reference ở lần {attempt}: {e}", "ERR", scenario_name)
            if attempt >= max_attempts:
                return False
        finally:
            await page.close()

    return False


# ── Browser launcher ────────────────────────────────────────────────────────
async def launch_browser(p, profile_dir: str, proxy_str: str | None, har_path: str):
    """
    Mở 1 Chrome persistent context với profile + proxy chỉ định.
    Viewport cố định 1920×1080 theo yêu cầu.
    """
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    proxy_config = parse_proxy(proxy_str)

    kwargs = {
        "user_data_dir"   : profile_dir,
        "headless"        : False,       # Hiện giao diện để bạn nhìn thấy
        "channel"         : "chrome",
        "args"            : [
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        "ignore_default_args": ["--enable-automation"],
        "accept_downloads": True,
        "record_har_path" : har_path,
        "viewport"        : VIEWPORT,
    }
    if proxy_config:
        kwargs["proxy"] = proxy_config

    return await p.chromium.launch_persistent_context(**kwargs)


async def _open_flow_probe_page(browser, worker_id: str, proxy_name: str):
    """
    Mở 1 page probe để xác nhận proxy usable thật sự với Google Flow.
    """
    page = await browser.new_page()

    def _on_req_failed(req):
        try:
            failure = req.failure
            failure_text = str(failure or "")
        except Exception:
            failure_text = ""
        log(
            f"[proxy={proxy_name}] requestfailed url={req.url} method={req.method} failure={failure_text}",
            "WARN",
            worker_id,
        )

    page.on("requestfailed", _on_req_failed)
    try:
        log(f"[proxy={proxy_name}] goto start: {GOOGLE_FLOW_HOME}", "INFO", worker_id)
        resp = await page.goto(GOOGLE_FLOW_HOME, wait_until="domcontentloaded", timeout=45000)
        final_url = str(page.url or "")
        status = resp.status if resp else None
        log(
            f"[proxy={proxy_name}] goto ok status={status} final_url={final_url}",
            "OK",
            worker_id,
        )
        if final_url.startswith("about:blank"):
            raise RuntimeError("goto returned but final_url is still about:blank")
        return page
    except Exception as e:
        final_url = str(page.url or "")
        log(
            f"[proxy={proxy_name}] goto fail final_url={final_url} err={type(e).__name__}: {e}",
            "ERR",
            worker_id,
        )
        await page.close()
        raise


async def launch_browser_with_fallback(p, worker: dict, profile_dir: str, har_path: str):
    """
    Launch browser với fallback proxy candidates.
    """
    worker_id = str(worker.get("worker_id", ""))
    candidates = build_proxy_candidates(worker)
    last_err = None

    if not candidates:
        log("No proxy candidate -> launch without proxy", "INFO", worker_id)
        browser = await launch_browser(p, profile_dir, None, har_path)
        return browser, None

    for cand in candidates:
        proxy_name = str(cand.get("name", ""))
        proxy_str = str(cand.get("proxy", "") or "").strip()
        proxy_cfg = parse_proxy(proxy_str)
        if not proxy_cfg:
            continue
        browser = None
        try:
            log(f"[proxy={proxy_name}] launch start proxy={proxy_cfg.get('server')}", "INFO", worker_id)
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            kwargs = {
                "user_data_dir": profile_dir,
                "headless": False,
                "channel": "chrome",
                "args": [
                    "--start-maximized",
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                "ignore_default_args": ["--enable-automation"],
                "accept_downloads": True,
                "record_har_path": har_path,
                "viewport": {"width": 1920, "height": 1080},
                "proxy": proxy_cfg,
            }
            browser = await p.chromium.launch_persistent_context(**kwargs)
            probe_page = await _open_flow_probe_page(browser, worker_id, proxy_name)
            await probe_page.close()
            worker["_selected_proxy_name"] = proxy_name
            worker["_selected_proxy"] = proxy_str
            return browser, proxy_name
        except Exception as e:
            last_err = e
            log(f"[proxy={proxy_name}] launch/probe fail: {e}", "ERR", worker_id)
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            continue

    raise RuntimeError(f"All proxy candidates failed for {worker_id}: {last_err}")


# ── Image step cho 1 kịch bản ─────────────────────────────────────────────────
async def run_image_step_for_scenario(
    p,
    browser_ctx,
    scenario_dir: str,
    scenario_name: str,
):
    """
    Dùng Chrome IMAGE (đã mở sẵn) để tạo ảnh reference cho 1 kịch bản.

    Thay vì import dreamina.py trực tiếp (sẽ gây conflict global state),
    gọi dreamina.py qua subprocess với env vars đúng.
    """
    output_dir = os.path.join(scenario_dir, "output")
    prompts_path = os.path.join(scenario_dir, "prompts.txt")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Kiểm tra prompts.txt có tồn tại không
    if not os.path.exists(prompts_path):
        log(f"Không tìm thấy {prompts_path}, bỏ qua.", "WARN", scenario_name)
        return False

    # Kiểm tra prompts.txt có phải dạng structured không (nhanh, check text)
    text = Path(prompts_path).read_text(encoding="utf-8")
    if "FULL VIDEO PROMPTS" not in text or "CHARACTER REFERENCE IMAGE PROMPTS" not in text:
        log(f"File prompts không ở dạng structured, bỏ qua bước ảnh.", "WARN", scenario_name)
        return False

    log(f"Tạo ảnh reference cho '{scenario_dir}'...", "STEP", scenario_name)

    try:
        # Import dreamina để dùng các hàm core
        # LƯU Ý: import ở đây an toàn vì image step chạy tuần tự (không song song)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dreamina_img",
            os.path.join(_SCRIPT_DIR, "dreamina.py")
        )
        dreamina = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dreamina)

        # Override OUTPUT_DIR cho kịch bản này
        dreamina.OUTPUT_DIR = os.path.abspath(output_dir)

        # Parse structured để lấy reference prompts
        structured_plan = dreamina.parse_structured_story_input(prompts_path)
        if not structured_plan.get("is_structured"):
            log(f"Parse structured thất bại, bỏ qua.", "WARN", scenario_name)
            return False

        ok = await _generate_reference_images_with_retry(
            browser_ctx=browser_ctx,
            dreamina=dreamina,
            structured_plan=structured_plan,
            scenario_name=scenario_name,
            output_dir=output_dir,
            max_attempts=IMAGE_REFERENCE_MAX_ATTEMPTS,
        )
        return ok

    except Exception as e:
        log(f"Lỗi tạo ảnh reference: {e}", "ERR", scenario_name)
        import traceback
        traceback.print_exc()
        return False


# ── Video worker: chạy trong subprocess riêng ──────────────────────────────
def _run_video_worker_subprocess(
    worker_id: str,
    profile_dir: str,
    proxy_str: str | None,
    proxy_real: str | None,
    scenario_dir: str,
    output_dir: str,
):
    """
    Hàm chạy TRONG subprocess riêng (không async).
    Import dreamina.py mới hoàn toàn → state global sạch.
    
    Giải quyết: biến global trong dreamina.py (_scene_to_task_ids, OUTPUT_DIR...)
    không bị conflict giữa các worker.
    """
    import asyncio

    async def _worker_main():
        # Import dreamina trong process riêng → state sạch hoàn toàn
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dreamina",
            os.path.join(_SCRIPT_DIR, "dreamina.py")
        )
        dreamina = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dreamina)

        # Override config cho worker này
        dreamina.OUTPUT_DIR = os.path.abspath(output_dir)
        dreamina.GOOGLE_FLOW_VIDEO_REFERENCE_DIR = os.path.abspath(output_dir)
        dreamina.VP_WIDTH = 1920
        dreamina.VP_HEIGHT = 1080

        # Khởi tạo debug session riêng cho worker này
        dreamina._init_debug_session()

        # Parse prompt của kịch bản
        prompts_path = os.path.join(scenario_dir, "prompts.txt")
        structured_plan = dreamina.parse_structured_story_input(prompts_path)
        if structured_plan.get("is_structured"):
            video_prompts = structured_plan.get("video_prompts", [])
        else:
            # Fallback: đọc từng dòng
            if os.path.exists(prompts_path):
                with open(prompts_path, "r", encoding="utf-8") as f:
                    video_prompts = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
            else:
                video_prompts = []

        if not video_prompts:
            dreamina.log(f"[{worker_id}] Không có video prompt, bỏ qua.", "WARN")
            return 0

        dreamina.log(f"[{worker_id}] Sẽ render {len(video_prompts)} video...", "INFO")

        # Mở Chrome riêng cho worker này
        har_path = os.path.join(scenario_dir, f"debug_video_{worker_id}.har")

        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            worker_cfg = {
                "worker_id": worker_id,
                "proxy": proxy_str,
                "proxy_real": proxy_real,
            }
            browser, selected_proxy_name = await launch_browser_with_fallback(
                p=p,
                worker=worker_cfg,
                profile_dir=profile_dir,
                har_path=har_path,
            )
            dreamina.log(
                f"[{worker_id}] Browser launched via proxy={selected_proxy_name} "
                f"raw={worker_cfg.get('_selected_proxy')}",
                "INFO",
            )
            try:
                # Map scene -> prompt để có thể rerun riêng các scene thiếu biến thể.
                prompt_by_scene: dict[int, str] = {}
                scene_order: list[int] = []
                for i, prompt in enumerate(video_prompts):
                    scene_no = int(dreamina.extract_scene_number(prompt, i + 1) or 0)
                    if scene_no <= 0:
                        continue
                    if scene_no not in prompt_by_scene:
                        prompt_by_scene[scene_no] = prompt
                        scene_order.append(scene_no)

                if not scene_order:
                    dreamina.log(f"[{worker_id}] Không parse được scene number từ video prompts.", "WARN")
                    return 0

                # Resume thông minh khi failover sang worker khác:
                # - Scene đã có >2 biến thể trong output thì xem như đã đủ, không gửi lại.
                # - Chỉ giữ scene còn thiếu (<=2) để tiếp tục phần dang dở.
                pending_scene_nos = [
                    sc for sc in scene_order
                    if _collect_scene_variant_count(output_dir, sc) <= 2
                ]
                if len(pending_scene_nos) < len(scene_order):
                    done_like = [sc for sc in scene_order if sc not in pending_scene_nos]
                    dreamina.log(
                        f"[{worker_id}] Resume mode: bỏ qua {len(done_like)} scene đã có >2 biến thể: "
                        f"{[f'canh_{s:03d}' for s in done_like]}",
                        "INFO",
                    )
                if not pending_scene_nos:
                    dreamina.log(f"[{worker_id}] Không còn scene dang dở (tất cả đã >2 biến thể).", "DONE")
                    return 0
                low_variant_retry_round: dict[int, int] = {}
                zero_variant_fail_count: dict[int, int] = {}
                reference_regen_round = 0
                total_saved = 0
                consecutive_zero_download_rounds = 0

                while pending_scene_nos:
                    run_prompts = [prompt_by_scene[sc] for sc in pending_scene_nos if sc in prompt_by_scene]
                    if not run_prompts:
                        break

                    page = await browser.new_page()
                    # Setup network debug theo page hiện tại.
                    dreamina.setup_image_network_debug(page)
                    try:
                        saved = await dreamina.run_google_flow_auto_video_request_response(page, run_prompts)
                    finally:
                        await page.close()

                    if int(saved) == -1:
                        dreamina.log(
                            f"[{worker_id}] Worker bị khóa 5 tiếng (Unusual Activity strikes). TẮT WORKER.",
                            "ERR",
                        )
                        return 99

                    # Nếu preload/upload reference fail sau retry upload nội bộ:
                    # quay lại bước tạo ảnh reference theo quy tắc người dùng.
                    if saved == int(getattr(dreamina, "FLOW_VIDEO_PRELOAD_FAILED_CODE", -2)):
                        if reference_regen_round >= REFERENCE_REGEN_MAX_ROUNDS:
                            dreamina.log(
                                f"[{worker_id}] Upload reference vẫn lỗi sau {REFERENCE_REGEN_MAX_ROUNDS} lần "
                                f"quay lại tạo ảnh. Đánh dấu kịch bản FAIL.",
                                "ERR",
                            )
                            return 2
                        reference_regen_round += 1
                        dreamina.log(
                            f"[{worker_id}] Upload reference lỗi -> quay lại tạo ảnh reference "
                            f"(vòng {reference_regen_round}/{REFERENCE_REGEN_MAX_ROUNDS}).",
                            "WARN",
                        )
                        ok_ref = await _generate_reference_images_with_retry(
                            browser_ctx=browser,
                            dreamina=dreamina,
                            structured_plan=structured_plan,
                            scenario_name=worker_id,
                            output_dir=output_dir,
                            max_attempts=IMAGE_REFERENCE_MAX_ATTEMPTS,
                        )
                        if not ok_ref:
                            dreamina.log(
                                f"[{worker_id}] Tạo lại ảnh reference thất bại. Đánh dấu kịch bản FAIL.",
                                "ERR",
                            )
                            return 2
                        # Upload fail => chưa chạy được cảnh nào, giữ nguyên pending để thử lại.
                        continue

                    # Reset cờ regen khi đã chạy video thành công.
                    reference_regen_round = 0
                    saved_int = int(saved or 0)
                    total_saved += max(0, saved_int)
                    if saved_int > 0:
                        consecutive_zero_download_rounds = 0
                    else:
                        consecutive_zero_download_rounds += 1
                        dreamina.log(
                            f"[{worker_id}] Vòng render không tải thêm video nào "
                            f"({consecutive_zero_download_rounds}/"
                            f"{VIDEO_FAILOVER_CONSECUTIVE_ZERO_DOWNLOAD_ROUNDS}).",
                            "WARN",
                        )
                        # Khi 3 vòng liên tiếp đều không tải thêm được video:
                        # chuyển kịch bản sang worker rảnh khác.
                        if consecutive_zero_download_rounds >= VIDEO_FAILOVER_CONSECUTIVE_ZERO_DOWNLOAD_ROUNDS:
                            dreamina.log(
                                f"[{worker_id}] Kích hoạt failover: 3 vòng liên tiếp không tải được video.",
                                "ERR",
                            )
                            return VIDEO_WORKER_FAILOVER_EXIT_CODE

                    next_pending: list[int] = []
                    for scene_no in pending_scene_nos:
                        variant_count = _collect_scene_variant_count(output_dir, scene_no)

                        # Rule 3:
                        # - Scene <=2 biến thể: tạo lại để cộng thêm biến thể.
                        # - Scene 0 biến thể lỗi >=3 lần: fail scene, ngừng cố.
                        if variant_count <= 2:
                            if variant_count == 0:
                                n_fail = int(zero_variant_fail_count.get(scene_no, 0) or 0) + 1
                                zero_variant_fail_count[scene_no] = n_fail
                                if n_fail >= VIDEO_ZERO_VARIANT_MAX_FAILS:
                                    dreamina.log(
                                        f"[{worker_id}] canh_{scene_no:03d} không tạo được biến thể nào "
                                        f"sau {n_fail} lần. Đánh dấu FAIL scene.",
                                        "ERR",
                                    )
                                else:
                                    dreamina.log(
                                        f"[{worker_id}] canh_{scene_no:03d} vẫn 0 biến thể "
                                        f"(lỗi {n_fail}/{VIDEO_ZERO_VARIANT_MAX_FAILS}) -> sẽ tạo lại.",
                                        "WARN",
                                    )
                                    next_pending.append(scene_no)
                            else:
                                n_round = int(low_variant_retry_round.get(scene_no, 0) or 0) + 1
                                low_variant_retry_round[scene_no] = n_round
                                if n_round <= VIDEO_LOW_VARIANT_MAX_RETRY_ROUNDS:
                                    dreamina.log(
                                        f"[{worker_id}] canh_{scene_no:03d} hiện có {variant_count} biến thể "
                                        f"(<=2), retry bổ sung vòng {n_round}/{VIDEO_LOW_VARIANT_MAX_RETRY_ROUNDS}.",
                                        "WARN",
                                    )
                                    next_pending.append(scene_no)
                                else:
                                    dreamina.log(
                                        f"[{worker_id}] canh_{scene_no:03d} vẫn <=2 biến thể sau "
                                        f"{n_round - 1} vòng retry bổ sung, giữ kết quả hiện tại.",
                                        "WARN",
                                    )
                        else:
                            dreamina.log(
                                f"[{worker_id}] canh_{scene_no:03d} đã đủ {variant_count} biến thể.",
                                "OK",
                            )

                    if not next_pending:
                        break

                    dreamina.log(
                        f"[{worker_id}] Sẽ tạo lại {len(next_pending)} scene còn thiếu biến thể: "
                        f"{[f'canh_{s:03d}' for s in next_pending]}",
                        "INFO",
                    )
                    pending_scene_nos = next_pending

                total_variants = sum(_collect_scene_variant_count(output_dir, sc) for sc in scene_order)
                dreamina.log(
                    f"[{worker_id}] Hoàn thành! saved={total_saved}, tổng biến thể trong output={total_variants} "
                    f"→ {output_dir}",
                    "DONE",
                )
                return 0
            except Exception as e:
                dreamina.log(f"[{worker_id}] Lỗi render video: {e}", "ERR")
                import traceback
                traceback.print_exc()
                return 1
            finally:
                # Runner song song cần giải phóng worker dứt điểm.
                # Không giữ Chrome mở trong mode này để tránh kẹt slot và treo process.
                try:
                    await browser.close()
                except Exception:
                    pass

    rc = asyncio.run(_worker_main())
    if int(rc or 0) != 0:
        raise SystemExit(int(rc))


# ── Main orchestrator ─────────────────────────────────────────────────────────
async def run_parallel(args):
    """
    Hàm điều phối chính:
    1. Mở Chrome IMAGE 1 lần, tạo ảnh tuần tự cho từng kịch bản
    2. Mỗi kịch bản xong ảnh → spawn subprocess cho Chrome Video của nó
    3. Tất cả subprocess Video chạy song song, state độc lập hoàn toàn
    """
    config        = load_config()
    image_worker  = config.get("image_worker", {})
    video_workers = config.get("video_workers", [])

    # Lọc kịch bản nếu có --scenario flag
    if args.scenario:
        video_workers = [
            w for w in video_workers
            if w["worker_id"] in args.scenario
            or os.path.basename(w.get("scenario_dir", "")) in args.scenario
        ]
        log(f"Chỉ chạy {len(video_workers)} kịch bản: {args.scenario}")

    if not video_workers:
        log("Không có worker nào để chạy!", "ERR")
        return

    # In tóm tắt config
    print("\n" + "="*65)
    print("  ✦ PARALLEL RUNNER — Multi Kịch Bản Song Song ✦")
    print("="*65)
    for w in video_workers:
        proxy_cfg = parse_proxy(w.get("proxy"))
        proxy_display = proxy_cfg["server"] if proxy_cfg else "Không proxy"
        scenario = w.get("scenario_dir", "")
        print(f"  [{w['worker_id']:<10}] {scenario:<25} | {proxy_display}")
    print("="*65)

    if args.dry_run:
        log("Dry-run mode — không chạy thật. Thoát.", "INFO")
        return

    # ────────────────────────────────────────────────────────────────────────
    # Pipeline Orchestrator:
    # - Ảnh kịch bản nào xong => đưa ngay vào queue video
    # - Worker video rảnh sẽ nhận ngay, không chờ tạo ảnh xong toàn bộ.
    # ────────────────────────────────────────────────────────────────────────
    import time
    pending_scenarios: list[str] = []
    banned_workers: dict[str, float] = {}
    failover_cooldown_workers: dict[str, float] = {}
    free_workers: list[dict] = list(video_workers)
    running_subprocesses: dict[subprocess.Popen, tuple[dict, str]] = {}
    worker_index_map = {str(w.get("worker_id", "")): i + 1 for i, w in enumerate(video_workers)}

    def _is_worker_running(worker_id: str) -> bool:
        for running_worker, _ in running_subprocesses.values():
            if str(running_worker.get("worker_id", "")) == str(worker_id):
                return True
        return False

    def _push_free_worker(worker: dict) -> None:
        if worker not in free_workers and not _is_worker_running(str(worker.get("worker_id", ""))):
            free_workers.append(worker)

    async def _collect_finished_workers() -> None:
        for proc in list(running_subprocesses.keys()):
            ret = proc.poll()
            if ret is None:
                continue

            worker, s_dir = running_subprocesses.pop(proc)
            w_id = str(worker.get("worker_id", ""))
            if ret == 99:
                log(f"Worker {w_id} BỊ BAN BỞI GOOGLE! Sắp xếp vào danh sách nghỉ 5 tiếng.", "WARN")
                banned_workers[w_id] = time.time() + 5 * 3600
                if pending_scenarios:
                    pending_scenarios.insert(1, s_dir)
                else:
                    pending_scenarios.insert(0, s_dir)
                continue

            if ret == VIDEO_WORKER_FAILOVER_EXIT_CODE:
                log(
                    f"Worker {w_id} không tải được video sau "
                    f"{VIDEO_FAILOVER_CONSECUTIVE_ZERO_DOWNLOAD_ROUNDS} vòng liên tiếp. "
                    f"Đưa kịch bản về queue để worker rảnh khác nhận.",
                    "WARN",
                )
                failover_cooldown_workers[w_id] = time.time() + WORKER_FAILOVER_COOLDOWN_SEC
                pending_scenarios.insert(0, s_dir)
                continue

            if ret != 0:
                log(f"Worker {w_id} kết thúc lỗi ({ret}) ở {s_dir}. Bỏ qua kịch bản này.", "ERR")
                _push_free_worker(worker)
                continue

            log(f"Worker {w_id} hoàn thành thành công kịch bản {s_dir}.", "OK")
            _push_free_worker(worker)

    async def _restore_cooled_workers() -> None:
        now = time.time()
        for w_id in list(banned_workers.keys()):
            if now <= banned_workers[w_id]:
                continue
            log(f"Worker {w_id} đã hết án phạt nghỉ 5 tiếng. Quay lại làm việc.", "OK")
            del banned_workers[w_id]
            orig = next((w for w in video_workers if str(w.get("worker_id", "")) == w_id), None)
            if orig:
                _push_free_worker(orig)

        for w_id in list(failover_cooldown_workers.keys()):
            if now <= failover_cooldown_workers[w_id]:
                continue
            log(
                f"Worker {w_id} đã hết cooldown failover "
                f"({WORKER_FAILOVER_COOLDOWN_SEC // 60} phút). Quay lại làm việc.",
                "OK",
            )
            del failover_cooldown_workers[w_id]
            orig = next((w for w in video_workers if str(w.get("worker_id", "")) == w_id), None)
            if orig:
                _push_free_worker(orig)

    async def _dispatch_pending_scenarios() -> None:
        while pending_scenarios and free_workers:
            s_dir = pending_scenarios.pop(0)
            worker = free_workers.pop(0)
            w_id = str(worker.get("worker_id", ""))
            output_dir = os.path.join(s_dir, "output")

            if not _ensure_worker_proxy_ready(worker):
                log(
                    f"Worker {w_id} proxy chưa sẵn sàng, tạm hoãn giao việc và đưa kịch bản về queue.",
                    "WARN",
                )
                pending_scenarios.insert(0, s_dir)
                failover_cooldown_workers[w_id] = time.time() + 300
                continue

            log(f"Giao kịch bản {s_dir} cho worker trống: {w_id} ...", "RUN")
            worker_env = build_worker_env(
                worker_id=w_id,
                worker_index=worker_index_map.get(w_id, len(video_workers)),
            )
            proc = subprocess.Popen(
                [
                    sys.executable,
                    os.path.join(_SCRIPT_DIR, "parallel_runner.py"),
                    "--_internal-video-worker",
                    json.dumps({
                        "worker_id": w_id,
                        "profile_dir": expand_path(worker["profile_dir"]),
                        "proxy": worker.get("proxy"),
                        "proxy_real": worker.get("proxy_real"),
                        "scenario_dir": s_dir,
                        "output_dir": output_dir,
                    }),
                ],
                cwd=_SCRIPT_DIR,
                env=worker_env,
            )
            running_subprocesses[proc] = (worker, s_dir)
            await _sleep_jitter(WORKER_STAGGER_SEC, low=0.8, high=1.35, floor=1.0)

    try:
        if not args.video_only:
            log("STEP 1: Tạo ảnh reference bằng Chrome IMAGE (pipeline mở video ngay khi ảnh xong)...", "STEP")
            image_profile_dir = expand_path(image_worker.get("profile_dir", "~/dreamina_playwright_profile_image"))
            image_proxy = image_worker.get("proxy")
            har_img_path = os.path.join(_SCRIPT_DIR, "debug_sessions", "parallel_image.har")
            Path(os.path.dirname(har_img_path)).mkdir(parents=True, exist_ok=True)

            async with async_playwright() as p:
                browser_img = await launch_browser(p, image_profile_dir, image_proxy, har_img_path)
                try:
                    for i, worker in enumerate(video_workers):
                        await _collect_finished_workers()
                        await _restore_cooled_workers()
                        await _dispatch_pending_scenarios()

                        scenario_dir = worker.get("scenario_dir", "")
                        if not scenario_dir:
                            continue
                        scenario_name = worker.get("worker_id", f"scenario_{i+1}")
                        log(f"[{i+1}/{len(video_workers)}] Tạo ảnh cho: {scenario_dir}", "STEP")
                        image_ok = await run_image_step_for_scenario(p, browser_img, scenario_dir, scenario_name)
                        if image_ok:
                            pending_scenarios.append(scenario_dir)
                            log(
                                f"{scenario_dir}: ảnh đã xong, đưa ngay vào hàng chờ video.",
                                "INFO",
                            )
                        else:
                            log(f"Bỏ qua kịch bản {scenario_name} vì tạo ảnh thất bại.", "ERR")

                        await _collect_finished_workers()
                        await _restore_cooled_workers()
                        await _dispatch_pending_scenarios()
                finally:
                    try:
                        await browser_img.close()
                    except Exception:
                        pass
                    log("Chrome IMAGE đã đóng.", "OK")
        else:
            log("--video-only: Bỏ qua tạo ảnh reference...", "INFO")
            for worker in video_workers:
                s_dir = worker.get("scenario_dir", "")
                if s_dir:
                    pending_scenarios.append(s_dir)

        log(
            f"Bắt đầu/tiếp tục điều phối video: pending={len(pending_scenarios)}, "
            f"running={len(running_subprocesses)}, free={len(free_workers)}",
            "INFO",
        )

        while pending_scenarios or running_subprocesses:
            await _collect_finished_workers()
            await _restore_cooled_workers()
            await _dispatch_pending_scenarios()
            await _sleep_jitter(5, low=0.8, high=1.3, floor=1.0)
    finally:
        # Dọn sạch subprocess khi dừng đột ngột (Ctrl+C, lỗi runtime...)
        if running_subprocesses:
            log(f"Đang dừng {len(running_subprocesses)} worker subprocess...", "WARN")
        for proc in list(running_subprocesses.keys()):
            try:
                proc.terminate()
            except Exception:
                pass
        await _sleep_jitter(1.5, low=0.8, high=1.25, floor=0.6)
        for proc in list(running_subprocesses.keys()):
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    print("\n" + "="*65)
    log(f"TẤT CẢ KỊCH BẢN HOÀN THÀNH HOẶC ĐÃ XỬ LÝ LỖI!", "DONE")
    print("="*65 + "\n")


# ── CLI entry point ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Chạy nhiều kịch bản Google Flow song song"
    )
    parser.add_argument(
        "--scenario", nargs="+", metavar="ID",
        help="Chỉ chạy các kịch bản có worker_id hoặc tên folder khớp"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Chỉ in config, không chạy thật"
    )
    parser.add_argument(
        "--video-only", action="store_true",
        help="Bỏ qua bước tạo ảnh reference (ảnh đã có sẵn trong output/)"
    )
    # Tham số nội bộ: chạy 1 video worker trong subprocess
    parser.add_argument(
        "--_internal-video-worker", dest="internal_worker_json",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    # Nếu là subprocess video worker → chạy luồng riêng
    if args.internal_worker_json:
        worker_data = json.loads(args.internal_worker_json)
        log(f"Subprocess video worker: {worker_data['worker_id']}", "RUN")
        _run_video_worker_subprocess(
            worker_id    = worker_data["worker_id"],
            profile_dir  = worker_data["profile_dir"],
            proxy_str    = worker_data.get("proxy"),
            proxy_real   = worker_data.get("proxy_real"),
            scenario_dir = worker_data["scenario_dir"],
            output_dir   = worker_data["output_dir"],
        )
        return

    # Luồng chính
    try:
        asyncio.run(run_parallel(args))
    except KeyboardInterrupt:
        log("Nhận tín hiệu dừng từ người dùng. Đã thoát an toàn.", "WARN")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
