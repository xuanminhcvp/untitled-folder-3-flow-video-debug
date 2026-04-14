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
import argparse
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright


# ── Constants ─────────────────────────────────────────────────────────────────
# Đường dẫn tương đối so với file này (để hoạt động trên cả máy khác)
_SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH      = os.path.join(_SCRIPT_DIR, "config", "video_workers.json")
GOOGLE_FLOW_HOME = "https://labs.google/fx/vi/tools/flow"
VIEWPORT         = {"width": 1920, "height": 1080}

# Stagger delay giữa các Chrome Video để tránh khởi động đồng loạt (giây)
WORKER_STAGGER_SEC = 5


# ── Logging ─────────────────────────────────────────────────────────────────
def log(msg: str, level: str = "INFO", worker_id: str = ""):
    """In log có timestamp, level và worker_id."""
    now = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{worker_id}]" if worker_id else "[MAIN]"
    print(f"[{now}] [{level:<5}] {prefix} {msg}")


# ── Config helpers ─────────────────────────────────────────────────────────
def load_config() -> dict:
    """Đọc config từ config/video_workers.json."""
    if not os.path.exists(CONFIG_PATH):
        log(f"Không tìm thấy config: {CONFIG_PATH}", "ERR")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_proxy(proxy_str: str | None) -> dict | None:
    """
    Chuyển proxy string sang dict Playwright.

    Hỗ trợ 2 format:
    - Có auth:    socks5://USER:PASS@IP:PORT  (proxy thật)
    - Không auth: socks5://IP:PORT            (local bridge — khi có gost chạy sẵn)
    """
    if not proxy_str:
        return None
    try:
        proto_rest = proxy_str.split("://", 1)
        if len(proto_rest) < 2:
            return None
        proto = proto_rest[0]
        rest  = proto_rest[1]

        if "@" in rest:
            # Có auth: USER:PASS@IP:PORT
            creds_part, host_port = rest.split("@", 1)
            creds = creds_part.split(":", 1)
            return {
                "server":   f"{proto}://{host_port}",
                "username": creds[0],
                "password": creds[1] if len(creds) > 1 else "",
            }
        else:
            # Không auth (local bridge): chỉ cần server
            return {"server": f"{proto}://{rest}"}
    except Exception as e:
        log(f"Không parse được proxy '{proxy_str}': {e}", "WARN")
        return None


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
    return env


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
        return

    # Kiểm tra prompts.txt có phải dạng structured không (nhanh, check text)
    text = Path(prompts_path).read_text(encoding="utf-8")
    if "FULL VIDEO PROMPTS" not in text or "CHARACTER REFERENCE IMAGE PROMPTS" not in text:
        log(f"File prompts không ở dạng structured, bỏ qua bước ảnh.", "WARN", scenario_name)
        return

    log(f"Tạo ảnh reference cho '{scenario_dir}'...", "STEP", scenario_name)

    # Tạo page mới trên Chrome IMAGE đang mở
    page = await browser_ctx.new_page()
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
            return

        ref_prompts = structured_plan.get("reference_generation_prompts", [])
        scene_to_label = structured_plan.get("reference_scene_to_label", {})

        if not ref_prompts:
            log(f"Không có reference prompt, bỏ qua.", "INFO", scenario_name)
            return

        # Khởi tạo debug session cho bước image
        dreamina._init_debug_session()

        # Setup network debug cho page mới
        dreamina.setup_image_network_debug(page)

        log(f"Tạo {len(ref_prompts)} ảnh reference...", "INFO", scenario_name)
        saved = await dreamina.run_google_flow_auto_request_response(page, ref_prompts)
        log(f"Đã tải {saved} ảnh reference.", "OK", scenario_name)

        # Đổi tên ảnh theo label (character1.png, image1.png...)
        rename_report = dreamina.rename_reference_scene_images(scene_to_label)
        renamed = len(rename_report.get("renamed", []))
        missing = len(rename_report.get("missing", []))
        log(f"Đổi tên: {renamed} OK, {missing} thiếu/lỗi", "INFO", scenario_name)

    except Exception as e:
        log(f"Lỗi tạo ảnh reference: {e}", "ERR", scenario_name)
        import traceback
        traceback.print_exc()
    finally:
        await page.close()


# ── Video worker: chạy trong subprocess riêng ──────────────────────────────
def _run_video_worker_subprocess(
    worker_id: str,
    profile_dir: str,
    proxy_str: str | None,
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
            # Build proxy kwargs
            proxy_config = None
            if proxy_str:
                proxy_config = parse_proxy(proxy_str)

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
            }
            if proxy_config:
                kwargs["proxy"] = proxy_config

            browser = await p.chromium.launch_persistent_context(**kwargs)
            page = await browser.new_page()

            # Setup network debug
            dreamina.setup_image_network_debug(page)

            try:
                saved = await dreamina.run_google_flow_auto_video_request_response(page, video_prompts)
                dreamina.log(f"[{worker_id}] Hoàn thành! {saved} video → {output_dir}", "DONE")
            except Exception as e:
                dreamina.log(f"[{worker_id}] Lỗi render video: {e}", "ERR")
                import traceback
                traceback.print_exc()
            finally:
                # Giữ browser mở nếu cần (theo config dreamina)
                if dreamina.GOOGLE_FLOW_KEEP_BROWSER_OPEN:
                    dreamina.log(f"[{worker_id}] Giữ Chrome mở. Đóng thủ công khi xong.", "INFO")
                    while True:
                        try:
                            if len(browser.pages) == 0:
                                break
                        except Exception:
                            break
                        await asyncio.sleep(2)
                await browser.close()

    asyncio.run(_worker_main())


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
    # STEP 1: Tạo ảnh reference tuần tự bằng Chrome IMAGE
    # (giữ Chrome IMAGE mở xuyên suốt, chỉ tạo page mới cho từng kịch bản)
    # ────────────────────────────────────────────────────────────────────────
    video_subprocesses = []

    if not args.video_only:
        log("STEP 1: Tạo ảnh reference bằng Chrome IMAGE...", "STEP")

        image_profile_dir = expand_path(image_worker.get("profile_dir", "~/dreamina_playwright_profile_image"))
        image_proxy       = image_worker.get("proxy")
        har_img_path      = os.path.join(_SCRIPT_DIR, "debug_sessions", "parallel_image.har")
        Path(os.path.dirname(har_img_path)).mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            browser_img = await launch_browser(p, image_profile_dir, image_proxy, har_img_path)

            for i, worker in enumerate(video_workers):
                scenario_dir  = worker.get("scenario_dir", "")
                scenario_name = worker.get("worker_id", f"scenario_{i+1}")

                log(f"[{i+1}/{len(video_workers)}] Tạo ảnh cho: {scenario_dir}", "STEP")
                await run_image_step_for_scenario(
                    p, browser_img, scenario_dir, scenario_name
                )

                # Sau khi ảnh xong → spawn subprocess video cho kịch bản này
                output_dir  = os.path.join(scenario_dir, "output")
                profile_dir = expand_path(worker["profile_dir"])
                proxy_str   = worker.get("proxy")
                worker_id   = worker["worker_id"]

                # Stagger delay giữa các video worker
                stagger = i * WORKER_STAGGER_SEC
                if stagger > 0:
                    log(f"Stagger {stagger}s trước khi spawn {worker_id}...", "INFO")
                    await asyncio.sleep(stagger)

                log(f"Spawn subprocess video: {worker_id}", "RUN")
                worker_env = build_worker_env(worker_id=worker_id, worker_index=i)
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        os.path.join(_SCRIPT_DIR, "parallel_runner.py"),
                        "--_internal-video-worker",
                        json.dumps({
                            "worker_id":    worker_id,
                            "profile_dir":  profile_dir,
                            "proxy":        proxy_str,
                            "scenario_dir": scenario_dir,
                            "output_dir":   output_dir,
                        }),
                    ],
                    cwd=_SCRIPT_DIR,
                    env=worker_env,
                )
                video_subprocesses.append((worker_id, proc))

            # Chrome IMAGE đã xong việc, đóng lại
            await browser_img.close()
            log("Chrome IMAGE đã đóng.", "OK")

    else:
        # --video-only: bỏ qua bước ảnh, spawn tất cả video subprocess ngay
        log("--video-only: bỏ qua bước ảnh, khởi động tất cả Chrome Video...", "INFO")
        for i, worker in enumerate(video_workers):
            scenario_dir = worker.get("scenario_dir", "")
            output_dir   = os.path.join(scenario_dir, "output")
            profile_dir  = expand_path(worker["profile_dir"])
            proxy_str    = worker.get("proxy")
            worker_id    = worker["worker_id"]

            stagger = i * WORKER_STAGGER_SEC
            if stagger > 0:
                log(f"Stagger {stagger}s trước khi spawn {worker_id}...", "INFO")
                await asyncio.sleep(stagger)

            log(f"Spawn subprocess video: {worker_id}", "RUN")
            worker_env = build_worker_env(worker_id=worker_id, worker_index=i)
            proc = subprocess.Popen(
                [
                    sys.executable,
                    os.path.join(_SCRIPT_DIR, "parallel_runner.py"),
                    "--_internal-video-worker",
                    json.dumps({
                        "worker_id":    worker_id,
                        "profile_dir":  profile_dir,
                        "proxy":        proxy_str,
                        "scenario_dir": scenario_dir,
                        "output_dir":   output_dir,
                    }),
                ],
                cwd=_SCRIPT_DIR,
                env=worker_env,
            )
            video_subprocesses.append((worker_id, proc))

    # ────────────────────────────────────────────────────────────────────────
    # Chờ tất cả subprocess video hoàn tất
    # ────────────────────────────────────────────────────────────────────────
    if video_subprocesses:
        log(f"Đang chờ {len(video_subprocesses)} video worker hoàn thành...", "WAIT")
        for worker_id, proc in video_subprocesses:
            returncode = proc.wait()
            if returncode == 0:
                log(f"Worker {worker_id} hoàn thành.", "OK")
            else:
                log(f"Worker {worker_id} kết thúc với lỗi (code={returncode}).", "ERR")

    print("\n" + "="*65)
    log(f"TẤT CẢ {len(video_workers)} KỊCH BẢN HOÀN THÀNH!", "DONE")
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
            scenario_dir = worker_data["scenario_dir"],
            output_dir   = worker_data["output_dir"],
        )
        return

    # Luồng chính
    asyncio.run(run_parallel(args))


if __name__ == "__main__":
    main()
