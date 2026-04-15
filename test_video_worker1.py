#!/usr/bin/env python3
"""
test_video_worker1.py
──────────────────────────────────────────────────────────────────────────────
Script chuyên test logic VIDEO cho duy nhất worker `video_1`.

Mục tiêu:
- Không chạy bước tạo ảnh reference.
- Chỉ chạy luồng render video của 1 Chrome profile: `chrome video 1`.
- Tận dụng lại logic đã ổn định trong `parallel_runner.py` (internal video worker).

Request/Response vận hành (dễ hiểu):
1) Script này đọc config `config/video_workers.json` để lấy đúng profile/proxy/scenario của `video_1`.
2) Script build payload JSON rồi gọi command:
   python3 parallel_runner.py --_internal-video-worker '<payload>'
3) Bên `parallel_runner.py` nhận payload và chạy trực tiếp hàm video worker:
   - mở Chrome profile video_1
   - gửi prompt video lên Flow
   - nhận response API/media từ Flow
   - tải file video về thư mục output của scenario

Lưu ý:
- Nếu bạn dùng proxy bridge, cần bật trước: `python3 proxy_bridge.py start`
- Script này KHÔNG đụng vào Media Import UI nội bộ của Flow ngoài luồng hiện có.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path


# ── Path chuẩn theo project root ──────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config" / "video_workers.json"
PARALLEL_RUNNER_PATH = SCRIPT_DIR / "parallel_runner.py"


def log(message: str, level: str = "INFO") -> None:
    """In log có timestamp để dễ theo dõi khi chạy test."""
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] [{level:<5}] [video_test] {message}")


def load_config() -> dict:
    """Đọc file config chứa danh sách video workers."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Không tìm thấy config: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def find_worker_video_1(config: dict) -> dict:
    """Tìm worker có worker_id = video_1 trong config."""
    workers = config.get("video_workers", [])
    for worker in workers:
        if worker.get("worker_id") == "video_1":
            return worker
    raise RuntimeError("Không tìm thấy worker `video_1` trong config/video_workers.json")


def is_local_port_open(port: int) -> bool:
    """Kiểm tra nhanh proxy bridge localhost:PORT có đang mở không."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except Exception:
        return False


def maybe_warn_proxy(proxy: str | None) -> None:
    """Cảnh báo nhẹ nếu proxy local bridge chưa mở."""
    if not proxy:
        log("Worker video_1 đang không dùng proxy.", "WARN")
        return

    # Trường hợp phổ biến: socks5://127.0.0.1:11001
    if proxy.startswith("socks5://127.0.0.1:"):
        port_text = proxy.rsplit(":", 1)[-1]
        try:
            port = int(port_text)
        except ValueError:
            log(f"Proxy format lạ: {proxy}", "WARN")
            return

        if is_local_port_open(port):
            log(f"Proxy bridge OK: 127.0.0.1:{port}", "OK")
        else:
            log(
                "Proxy bridge chưa mở. Nếu cần proxy, chạy trước: python3 proxy_bridge.py start",
                "WARN",
            )
    else:
        log(f"Worker dùng proxy khác local bridge: {proxy}", "INFO")


def build_internal_payload(worker: dict, scenario_override: str | None) -> dict:
    """Tạo payload đúng format mà parallel_runner internal worker cần."""
    scenario_dir = scenario_override or worker.get("scenario_dir", "")
    if not scenario_dir:
        raise RuntimeError("scenario_dir của video_1 đang trống trong config")

    profile_dir = worker.get("profile_dir", "")
    if not profile_dir:
        raise RuntimeError("profile_dir của video_1 đang trống trong config")

    payload = {
        "worker_id": worker.get("worker_id", "video_1"),
        "profile_dir": str((SCRIPT_DIR / profile_dir).resolve()),
        "proxy": worker.get("proxy"),
        "scenario_dir": str((SCRIPT_DIR / scenario_dir).resolve()),
        "output_dir": str((SCRIPT_DIR / scenario_dir / "output").resolve()),
    }
    return payload


def validate_inputs(payload: dict) -> None:
    """Kiểm tra file prompts/video trước khi chạy để lỗi rõ ràng cho người dùng."""
    scenario_dir = Path(payload["scenario_dir"])
    prompts_file = scenario_dir / "prompts.txt"

    if not scenario_dir.exists():
        raise FileNotFoundError(f"Không tìm thấy scenario_dir: {scenario_dir}")
    if not prompts_file.exists():
        raise FileNotFoundError(f"Không tìm thấy prompts.txt: {prompts_file}")

    # Tạo output dir nếu chưa có để tránh lỗi ghi file khi download video.
    Path(payload["output_dir"]).mkdir(parents=True, exist_ok=True)


def run_video_worker(payload: dict, dry_run: bool, keep_browser_open: bool) -> int:
    """Chạy internal video worker của parallel_runner cho duy nhất video_1."""
    command = [
        sys.executable,
        str(PARALLEL_RUNNER_PATH),
        "--_internal-video-worker",
        json.dumps(payload, ensure_ascii=False),
    ]

    log("Chuẩn bị chạy test VIDEO ONLY với worker video_1")
    log(f"Scenario: {payload['scenario_dir']}")
    log(f"Profile : {payload['profile_dir']}")
    log(f"Output  : {payload['output_dir']}")
    log(f"Proxy   : {payload.get('proxy')}")

    if dry_run:
        log("Dry-run: chỉ in lệnh, không chạy thật.", "INFO")
        print("\nLệnh sẽ chạy:")
        print(" ".join(command))
        return 0

    # Env riêng cho test video để kết quả dễ lặp lại và giữ đúng mode video.
    env = os.environ.copy()
    env["TARGET_PLATFORM"] = "google_flow"
    env["GOOGLE_FLOW_MEDIA_MODE"] = "video"
    env["GOOGLE_FLOW_RANDOM_PROMPTS"] = "0"
    env["GOOGLE_FLOW_KEEP_BROWSER_OPEN"] = "1" if keep_browser_open else "0"

    # Chạy blocking để terminal hiển thị toàn bộ log realtime từ parallel_runner.
    proc = subprocess.run(command, cwd=str(SCRIPT_DIR), env=env)
    return int(proc.returncode)


def parse_args() -> argparse.Namespace:
    """CLI options cho nhu cầu test nhanh."""
    parser = argparse.ArgumentParser(
        description="Test logic VIDEO only cho worker video_1"
    )
    parser.add_argument(
        "--scenario-dir",
        help="Override scenario_dir (mặc định lấy từ config của video_1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Chỉ in thông tin, không chạy thật",
    )
    parser.add_argument(
        "--close-browser-when-done",
        action="store_true",
        help="Nếu bật thì script tự đóng Chrome khi xong (mặc định giữ mở)",
    )
    return parser.parse_args()


def main() -> int:
    """Entry point chính của script test."""
    args = parse_args()

    try:
        config = load_config()
        worker = find_worker_video_1(config)
        maybe_warn_proxy(worker.get("proxy"))

        payload = build_internal_payload(worker, args.scenario_dir)
        validate_inputs(payload)

        return run_video_worker(
            payload=payload,
            dry_run=args.dry_run,
            keep_browser_open=(not args.close_browser_when_done),
        )
    except Exception as exc:
        log(str(exc), "ERR")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
