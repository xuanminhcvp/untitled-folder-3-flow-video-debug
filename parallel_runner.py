#!/usr/bin/env python3
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
"""

import asyncio
import json
import os
import sys
import time
import argparse
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright


# ── Constants ─────────────────────────────────────────────────────────────────
CONFIG_PATH      = os.path.join(os.path.dirname(__file__), "config", "video_workers.json")
GOOGLE_FLOW_HOME = "https://labs.google/fx/vi/tools/flow"
VIEWPORT         = {"width": 1920, "height": 1080}

# Stagger delay giữa các Chrome Video để tránh khởi động đồng loạt
WORKER_STAGGER_SEC = 5


# ── Logging ─────────────────────────────────────────────────────────────────
def log(msg: str, level: str = "INFO", worker_id: str = ""):
    """In log có timestamp, level và worker_id."""
    now = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{worker_id}]" if worker_id else "[MAIN] "
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
    Input:  socks5://USER:PASS@IP:PORT
    Output: {"server": "socks5://IP:PORT", "username": "USER", "password": "PASS"}
    """
    if not proxy_str:
        return None
    try:
        proto_rest = proxy_str.split("://", 1)
        proto = proto_rest[0]
        rest = proto_rest[1]
        creds_host = rest.split("@", 1)
        creds = creds_host[0].split(":", 1)
        host_port = creds_host[1]
        return {
            "server":   f"{proto}://{host_port}",
            "username": creds[0],
            "password": creds[1] if len(creds) > 1 else "",
        }
    except Exception as e:
        log(f"Không parse được proxy '{proxy_str}': {e}", "WARN")
        return None


def expand_path(p: str) -> str:
    """Mở rộng ~ thành đường dẫn thật của user."""
    return os.path.expanduser(p)


def load_scenario_prompts(scenario_dir: str) -> list[str]:
    """
    Đọc prompts.txt trong thư mục kịch bản.
    Trả về danh sách prompt (mỗi dòng 1 prompt), bỏ dòng trống.
    """
    prompts_path = os.path.join(scenario_dir, "prompts.txt")
    if not os.path.exists(prompts_path):
        log(f"Không tìm thấy: {prompts_path}", "WARN")
        return []
    with open(prompts_path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


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


# ── Image step (per scenario) ────────────────────────────────────────────────
async def run_image_step_for_scenario(
    p,
    image_worker_config: dict,
    scenario_dir: str,
    scenario_name: str,
    output_event: asyncio.Event,
):
    """
    Chạy Chrome IMAGE để tạo ảnh reference cho 1 kịch bản.
    Xong thì set output_event để Chrome Video tương ứng biết mà bắt đầu.
    
    Import hàm từ dreamina.py để tái dùng logic đã có sẵn.
    """
    # Import runtime để tránh circular import
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dreamina",
        os.path.join(os.path.dirname(__file__), "dreamina.py")
    )
    dreamina = importlib.util.load_from_spec(spec)
    spec.loader.exec_module(dreamina)

    profile_dir = expand_path(image_worker_config["profile_dir"])
    proxy_str   = image_worker_config.get("proxy")
    prompts_path = os.path.join(scenario_dir, "prompts.txt")
    output_dir   = os.path.join(scenario_dir, "output")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    log(f"[{scenario_name}] Bắt đầu tạo ảnh reference...", "STEP")

    har_path = os.path.join(scenario_dir, "debug_image.har")

    try:
        browser = await launch_browser(p, profile_dir, proxy_str, har_path)
        page    = await browser.new_page()

        # Parse structured plan để lấy reference prompts
        structured_plan = dreamina.parse_structured_story_input(prompts_path)
        if not structured_plan.get("is_structured"):
            log(f"[{scenario_name}] File prompts không ở dạng structured, bỏ qua bước ảnh.", "WARN")
            output_event.set()
            await browser.close()
            return

        ref_prompts    = structured_plan.get("reference_generation_prompts", [])
        scene_to_label = structured_plan.get("reference_scene_to_label", {})

        if ref_prompts:
            log(f"[{scenario_name}] Tạo {len(ref_prompts)} ảnh reference...", "INFO")
            # Override OUTPUT_DIR tạm thời cho kịch bản này
            dreamina.OUTPUT_DIR = output_dir
            saved = await dreamina.run_google_flow_auto_request_response(page, ref_prompts)
            log(f"[{scenario_name}] Đã tải {saved} ảnh reference.", "OK")

            # Đổi tên ảnh theo label (character1, image1...)
            dreamina.rename_reference_scene_images(scene_to_label)
        else:
            log(f"[{scenario_name}] Không có reference prompt, bỏ qua.", "INFO")

        await browser.close()

    except Exception as e:
        log(f"[{scenario_name}] Lỗi tạo ảnh reference: {e}", "ERR")
    finally:
        # Dù lỗi hay không cũng unlock Video worker để nó chạy
        output_event.set()
        log(f"[{scenario_name}] Ảnh reference xong → Chrome Video được unlock.", "OK")


# ── Video step (per worker) ──────────────────────────────────────────────────
async def run_video_worker(
    p,
    worker: dict,
    ready_event: asyncio.Event,
    worker_index: int,
):
    """
    Chạy 1 Chrome Video worker cho 1 kịch bản.
    Chờ ready_event (ảnh reference xong) rồi mới bắt đầu.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dreamina",
        os.path.join(os.path.dirname(__file__), "dreamina.py")
    )
    dreamina = importlib.util.load_from_spec(spec)
    spec.loader.exec_module(dreamina)

    worker_id    = worker["worker_id"]
    profile_dir  = expand_path(worker["profile_dir"])
    proxy_str    = worker.get("proxy")
    scenario_dir = worker.get("scenario_dir", "")
    output_dir   = os.path.join(scenario_dir, "output")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Stagger: worker N chờ thêm N*5 giây để không mở đồng loạt
    stagger = worker_index * WORKER_STAGGER_SEC
    if stagger > 0:
        log(f"Stagger {stagger}s trước khi bắt đầu...", "INFO", worker_id)
        await asyncio.sleep(stagger)

    # Chờ ảnh reference của kịch bản này sẵn sàng
    log(f"Chờ ảnh reference của kịch bản '{scenario_dir}' xong...", "WAIT", worker_id)
    await ready_event.wait()
    log(f"Ảnh reference đã sẵn sàng. Bắt đầu render video!", "OK", worker_id)

    # Load prompts của kịch bản này
    prompts_path = os.path.join(scenario_dir, "prompts.txt")
    structured_plan = dreamina.parse_structured_story_input(prompts_path)
    if structured_plan.get("is_structured"):
        video_prompts = structured_plan.get("video_prompts", [])
    else:
        video_prompts = load_scenario_prompts(scenario_dir)

    if not video_prompts:
        log(f"Không có video prompt trong '{scenario_dir}', bỏ qua.", "WARN", worker_id)
        return

    log(f"Sẽ render {len(video_prompts)} video cho kịch bản '{scenario_dir}'.", "INFO", worker_id)

    har_path = os.path.join(scenario_dir, "debug_video.har")

    try:
        browser = await launch_browser(p, profile_dir, proxy_str, har_path)
        page    = await browser.new_page()

        # Override các path toàn cục của dreamina cho worker này
        dreamina.OUTPUT_DIR = output_dir
        dreamina.GOOGLE_FLOW_VIDEO_REFERENCE_DIR = os.path.abspath(output_dir)

        saved = await dreamina.run_google_flow_auto_video_request_response(page, video_prompts)
        log(f"Hoàn thành! Đã tải {saved} video → {output_dir}", "DONE", worker_id)

        await browser.close()

    except Exception as e:
        log(f"Lỗi render video: {e}", "ERR", worker_id)


# ── Main orchestrator ─────────────────────────────────────────────────────────
async def run_parallel(args):
    """
    Hàm điều phối chính:
    - Chạy Chrome IMAGE tuần tự cho từng kịch bản
    - Mỗi kịch bản xong ảnh → Chrome Video của nó bắt đầu (pipeline)
    - Tất cả Chrome Video chạy song song với nhau
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

    # In tóm tắt
    print("\n" + "="*60)
    print("  PARALLEL RUNNER — Multi Kịch Bản")
    print("="*60)
    for w in video_workers:
        proxy_server = parse_proxy(w.get("proxy") or "")
        proxy_display = proxy_server["server"] if proxy_server else "Không proxy"
        print(f"  [{w['worker_id']}] {w.get('scenario_dir','')} | {proxy_display}")
    print("="*60)

    if args.dry_run:
        log("Dry-run mode — không chạy thật. Thoát.", "INFO")
        return

    # Tạo 1 asyncio.Event cho mỗi worker để đồng bộ ảnh xong → video bắt đầu
    ready_events = [asyncio.Event() for _ in video_workers]

    if args.video_only:
        # Bỏ qua bước ảnh, set tất cả event ngay
        log("--video-only: bỏ qua bước tạo ảnh reference.", "INFO")
        for ev in ready_events:
            ev.set()

    async with async_playwright() as p:
        # ── Task image: tạo ảnh tuần tự, mỗi kịch bản xong → set event ───────
        async def image_pipeline():
            if args.video_only:
                return
            for i, (worker, ev) in enumerate(zip(video_workers, ready_events)):
                scenario_dir  = worker.get("scenario_dir", "")
                scenario_name = worker.get("worker_id", f"scenario_{i+1}")
                log(f"[{i+1}/{len(video_workers)}] Tạo ảnh reference: {scenario_dir}", "STEP")
                await run_image_step_for_scenario(
                    p, image_worker, scenario_dir, scenario_name, ev
                )

        # ── Tasks video: mỗi worker chờ event rồi chạy song song ─────────────
        video_tasks = [
            run_video_worker(p, worker, ev, i)
            for i, (worker, ev) in enumerate(zip(video_workers, ready_events))
        ]

        # Chạy image pipeline và tất cả video tasks cùng lúc (pipeline)
        await asyncio.gather(
            image_pipeline(),
            *video_tasks,
        )

    print("\n" + "="*60)
    log(f"TẤT CẢ {len(video_workers)} KỊCH BẢN HOÀN THÀNH!", "DONE")
    print("="*60 + "\n")


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
    args = parser.parse_args()
    asyncio.run(run_parallel(args))


if __name__ == "__main__":
    main()
