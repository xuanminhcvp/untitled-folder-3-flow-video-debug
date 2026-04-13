"""
Mở Chrome video profile và theo dõi network khi bạn thao tác thêm ảnh tham chiếu.

Mục tiêu:
- Bắt đúng request/payload khi bạn click nút tham chiếu ảnh trong Flow UI
- Ghi ra file JSON để phân tích → sau đó code lại tự động

Cách dùng:
  1. Chạy script này
  2. Thao tác thủ công: mở project Flow, nhập prompt, click thêm ảnh tham chiếu
  3. Nhấn Enter trong terminal để kết thúc và xem log
"""

import asyncio
import os
import json
import time
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

PROFILE_DIR_VIDEO = os.environ.get(
    "PROFILE_DIR_VIDEO",
    os.path.expanduser("~/dreamina_playwright_profile_video"),
).strip()

GOOGLE_FLOW_HOME = "https://labs.google/fx/vi/tools/flow"

# Thư mục lưu log bắt được
LOG_DIR = os.path.abspath("debug_sessions/capture_reference_ui")
Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

# Các keyword nhận diện request liên quan đến reference image
REFERENCE_KEYWORDS = [
    "reference", "media", "upload", "image", "inject",
    "attach", "generate", "create", "project", "trpc",
]

captured_requests = []
captured_responses = []


def _is_relevant_url(url: str) -> bool:
    """Lọc request liên quan đến Flow API và tham chiếu ảnh."""
    low = url.lower()
    return any(k in low for k in REFERENCE_KEYWORDS) and "labs.google" in low


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def main():
    print("=" * 65)
    print("  CAPTURE REFERENCE UI — Theo dõi network khi bạn thao tác")
    print(f"  Profile: {PROFILE_DIR_VIDEO}")
    print(f"  Log dir: {LOG_DIR}")
    print("=" * 65)
    print()

    Path(PROFILE_DIR_VIDEO).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR_VIDEO,
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
            # Ghi HAR để có toàn bộ request/response
            record_har_path=os.path.join(LOG_DIR, "capture_reference.har"),
            viewport={"width": 1440, "height": 900},
        )

        page = await browser.new_page()

        # ── Bắt toàn bộ request ────────────────────────────────────────────
        async def on_request(req):
            if not _is_relevant_url(req.url):
                return
            entry = {
                "ts": _ts(),
                "type": "REQUEST",
                "method": req.method,
                "url": req.url,
                "post_data": None,
            }
            # Lấy POST body nếu có (quan trọng để xem payload attach ảnh)
            try:
                pd = req.post_data
                if pd:
                    try:
                        entry["post_data"] = json.loads(pd)
                    except Exception:
                        entry["post_data"] = pd[:2000]
            except Exception:
                pass
            captured_requests.append(entry)
            # In ngay lên terminal để bạn thấy realtime
            pd_preview = ""
            if entry.get("post_data"):
                raw = json.dumps(entry["post_data"])
                pd_preview = f"\n    POST: {raw[:300]}"
            print(f"[{entry['ts']}] ▶ {entry['method']} {entry['url'][:100]}{pd_preview}")

        async def on_response(resp):
            if not _is_relevant_url(resp.url):
                return
            entry = {
                "ts": _ts(),
                "type": "RESPONSE",
                "status": resp.status,
                "url": resp.url,
                "body": None,
            }
            try:
                ct = (resp.headers.get("content-type") or "").lower()
                if "json" in ct:
                    body = await resp.json()
                    entry["body"] = body
                    body_preview = json.dumps(body)[:400]
                    print(f"[{entry['ts']}] ◀ {resp.status} {resp.url[:100]}")
                    print(f"    BODY: {body_preview}")
            except Exception:
                pass
            captured_responses.append(entry)

        page.on("request", on_request)
        page.on("response", on_response)

        # ── Mở Flow ───────────────────────────────────────────────────────
        await page.goto(GOOGLE_FLOW_HOME, wait_until="domcontentloaded", timeout=60000)

        print()
        print("  ✅ Chrome đã mở Google Flow với profile VIDEO.")
        print()
        print("  👉 Hãy thao tác thủ công:")
        print("     1. Mở 1 project Flow (hoặc tạo mới)")
        print("     2. Click vào nút thêm ảnh tham chiếu")
        print("     3. Chọn ảnh (character1.png, v.v.)")
        print("     4. Làm đầy đủ 1 lần để mình thấy toàn bộ flow")
        print()
        print("  👉 Nhấn [Enter] khi xong để lưu kết quả phân tích.")
        print()

        # Chờ user thao tác
        input("  >>> Nhấn [Enter] khi đã xong... ")

        # ── Lưu kết quả ───────────────────────────────────────────────────
        print()
        print("  📦 Đang lưu log phân tích...")
        await browser.close()

    # Ghi JSON log
    out_req = os.path.join(LOG_DIR, "requests.json")
    out_resp = os.path.join(LOG_DIR, "responses.json")
    out_all = os.path.join(LOG_DIR, "all_events.json")

    with open(out_req, "w", encoding="utf-8") as f:
        json.dump(captured_requests, f, ensure_ascii=False, indent=2)

    with open(out_resp, "w", encoding="utf-8") as f:
        json.dump(captured_responses, f, ensure_ascii=False, indent=2)

    all_events = sorted(
        captured_requests + captured_responses,
        key=lambda x: x["ts"]
    )
    with open(out_all, "w", encoding="utf-8") as f:
        json.dump(all_events, f, ensure_ascii=False, indent=2)

    print(f"  ✅ Đã lưu {len(captured_requests)} request")
    print(f"  ✅ Đã lưu {len(captured_responses)} response")
    print(f"  📁 Log: {LOG_DIR}")
    print()
    print("  Mình sẽ đọc log để học cách inject ảnh tự động.")


if __name__ == "__main__":
    asyncio.run(main())
