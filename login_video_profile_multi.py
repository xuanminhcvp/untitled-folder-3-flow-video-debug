#!/usr/bin/env python3
from __future__ import annotations
"""
login_video_profile_multi.py
────────────────────────────
Script hỗ trợ login thủ công từng Chrome video profile.

Cách dùng:
  python3 login_video_profile_multi.py          → hiện menu chọn worker
  python3 login_video_profile_multi.py 1        → mở thẳng worker số 1
  python3 login_video_profile_multi.py all      → mở TẤT CẢ worker (tuần tự)

Sau khi Chrome mở ra:
  1. Login tài khoản Google tại labs.google/fx/vi/tools/flow
  2. Đóng Chrome → cookie được lưu vào profile
  3. Lần sau chạy auto sẽ không cần login lại
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from playwright.async_api import async_playwright


# ── Đường dẫn config (relative so với file này) ──────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "video_workers.json")
GOOGLE_FLOW_HOME = "https://labs.google/fx/vi/tools/flow"


def load_config() -> dict:
    """Đọc config từ config/video_workers.json."""
    if not os.path.exists(CONFIG_PATH):
        print(f"[ERROR] Không tìm thấy config: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_proxy(proxy_str: str | None) -> dict | None:
    """
    Chuyển proxy string sang dict cho Playwright.

    Hỗ trợ 2 format:
    - Có auth:  socks5://USER:PASS@IP:PORT  (proxy thật)
    - Không auth: socks5://IP:PORT          (local bridge — dùng khi đã có gost)
    """
    if not proxy_str:
        return None
    try:
        proto_rest = proxy_str.split("://", 1)
        if len(proto_rest) < 2:
            return None
        proto = proto_rest[0]  # socks5 hoặc http
        rest  = proto_rest[1]  # phần sau ://

        # Kiểm tra có phần auth (có dấu @) hay không
        if "@" in rest:
            # Format: USER:PASS@IP:PORT
            creds_part, host_port = rest.split("@", 1)
            creds = creds_part.split(":", 1)
            username = creds[0]
            password = creds[1] if len(creds) > 1 else ""
            return {
                "server":   f"{proto}://{host_port}",
                "username": username,
                "password": password,
            }
        else:
            # Format không auth: IP:PORT (dùng cho local bridge)
            return {"server": f"{proto}://{rest}"}
    except Exception as e:
        print(f"[WARN] Không parse được proxy '{proxy_str}': {e}")
        return None


async def open_login_browser(worker: dict, worker_label: str):
    """
    Mở Chrome với profile + proxy của worker để user login tay.
    
    - headless=False để user thấy và thao tác
    - Chờ user đóng Chrome (hoặc nhấn Enter) mới kết thúc
    """
    profile_dir = os.path.expanduser(worker["profile_dir"])
    if not os.path.isabs(profile_dir):
        profile_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), profile_dir)
    proxy_str = worker.get("proxy")
    proxy_config = parse_proxy(proxy_str)
    worker_id = worker.get("worker_id", worker_label)
    scenario_dir = worker.get("scenario_dir", "")

    print(f"\n{'='*55}")
    print(f"  Worker: {worker_id}")
    print(f"  Profile: {profile_dir}")
    print(f"  Proxy: {proxy_str or 'Không dùng proxy'}")
    print(f"  Kịch bản: {scenario_dir}")
    print(f"{'='*55}")
    print(f"\n  → Chrome sẽ mở ra, hãy login Google tại:")
    print(f"    {GOOGLE_FLOW_HOME}")
    print(f"\n  → Sau khi login xong, ĐÓNG Chrome rồi nhấn Enter ở đây.")
    print()

    # Tạo thư mục profile nếu chưa có
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # Build launch options
        launch_kwargs = {
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
            # viewport=None → Chrome tự dùng kích thước màn hình thật
            # Không cố định để login thủ công thoải mái scroll/tương tác
            "viewport": None,
        }
        
        # Thêm proxy nếu có
        if proxy_config:
            launch_kwargs["proxy"] = proxy_config
            print(f"  [INFO] Proxy đã được áp dụng: {proxy_config['server']}")
        else:
            print(f"  [INFO] Không dùng proxy (chạy thẳng IP máy)")

        try:
            browser = await p.chromium.launch_persistent_context(**launch_kwargs)
            page = await browser.new_page()
            
            # Mở thẳng trang Google Flow để user login
            await page.goto(GOOGLE_FLOW_HOME, timeout=30000)
            print(f"  [OK] Chrome đã mở — hãy login Google tại trang vừa mở.")
            print(f"  [INFO] Sau khi login xong, đóng Chrome và nhấn Enter ở đây...")
            
            # Chờ user đóng Chrome hoặc nhấn Enter
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, input, "\n  [>>] Nhấn Enter khi đã login xong: ")
            
        except Exception as e:
            print(f"  [ERROR] Lỗi khi mở Chrome: {e}")
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    print(f"  [DONE] Profile '{worker_id}' đã lưu login session. ✅")


def print_menu(workers: list) -> None:
    """In menu chọn worker."""
    print("\n" + "="*55)
    print("  LOGIN CHROME VIDEO PROFILE — Multi Worker")
    print("="*55)
    print(f"\n  Config: {CONFIG_PATH}")
    print(f"\n  Danh sách worker:")
    for i, w in enumerate(workers, start=1):
        proxy_note = w.get("_note", "") or (w.get("proxy") or "Không proxy")
        scenario = w.get("scenario_dir", "")
        print(f"    [{i}] {w['worker_id']:12s} | {scenario:20s} | {proxy_note}")
    print(f"\n  [A] Mở TẤT CẢ theo thứ tự (tuần tự)")
    print(f"  [Q] Thoát")
    print()


async def main():
    config = load_config()
    workers = config.get("video_workers", [])
    
    if not workers:
        print("[ERROR] Không có video_workers trong config!")
        return

    # ── Xử lý tham số dòng lệnh ──────────────────────────────────────────────
    args = sys.argv[1:]
    
    if args:
        arg = args[0].strip().lower()
        if arg == "all":
            # Mở tất cả tuần tự
            print(f"\n  → Sẽ mở tuần tự {len(workers)} Chrome profile...")
            for i, w in enumerate(workers, start=1):
                print(f"\n  [{i}/{len(workers)}] Chuẩn bị mở worker '{w['worker_id']}'...")
                await open_login_browser(w, f"worker_{i}")
        elif arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(workers):
                await open_login_browser(workers[idx], f"worker_{int(arg)}")
            else:
                print(f"[ERROR] Không có worker số {arg} (chỉ có {len(workers)} worker)")
        else:
            print(f"[ERROR] Tham số không hợp lệ: '{arg}'")
            print(f"  Dùng: python3 {sys.argv[0]} [số 1-{len(workers)}|all]")
        return

    # ── Menu tương tác nếu không có tham số ──────────────────────────────────
    while True:
        print_menu(workers)
        choice = input("  Chọn [1-{}|A|Q]: ".format(len(workers))).strip().lower()
        
        if choice in ("q", "quit", "exit"):
            print("\n  Thoát.\n")
            break
        elif choice in ("a", "all"):
            for i, w in enumerate(workers, start=1):
                print(f"\n  [{i}/{len(workers)}] Mở worker '{w['worker_id']}'...")
                await open_login_browser(w, f"worker_{i}")
            print("\n  ✅ Đã login xong tất cả worker!\n")
            break
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(workers):
                await open_login_browser(workers[idx], f"worker_{int(choice)}")
            else:
                print(f"\n  [ERROR] Không có worker số {choice}")
        else:
            print(f"\n  [ERROR] Lựa chọn không hợp lệ: '{choice}'")


if __name__ == "__main__":
    asyncio.run(main())
