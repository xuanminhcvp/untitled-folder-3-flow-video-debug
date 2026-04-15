"""
Login profile IMAGE bằng Playwright.

Mở Chrome đúng profile mà pipeline sẽ dùng cho step tạo ảnh reference.
Login xong → đóng bằng Cmd+Q (hoặc nhấn Enter trong terminal) → profile sẽ lưu session.

Lần sau chạy pipeline, Playwright đọc lại đúng session từ profile này → không cần login lại.
"""

import asyncio
import os
from pathlib import Path
from playwright.async_api import async_playwright

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Đường dẫn profile được thiết lập để lưu vào ngay trong thư mục clone
PROFILE_DIR_IMAGE = os.environ.get(
    "PROFILE_DIR_IMAGE",
    os.path.join(_SCRIPT_DIR, "chrome_profiles", "dreamina_playwright_profile_image"),
).strip()

GOOGLE_FLOW_HOME = "https://labs.google/fx/vi/tools/flow"


async def main():
    print("=" * 60)
    print("  LOGIN PROFILE IMAGE (dùng cho step tạo ảnh reference)")
    print(f"  Profile: {PROFILE_DIR_IMAGE}")
    print("=" * 60)

    # Tạo folder profile nếu chưa có
    Path(PROFILE_DIR_IMAGE).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # Mở Chrome persistent context — cùng config với pipeline
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR_IMAGE,
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
            viewport={"width": 1440, "height": 900},
        )

        # Mở trang Flow
        page = await browser.new_page()
        await page.goto(GOOGLE_FLOW_HOME, wait_until="domcontentloaded", timeout=30000)

        print()
        print("  ✅ Chrome đã mở — Hãy đăng nhập tài khoản Google tại đây.")
        print("  ✅ Sau khi login xong, quay lại terminal và nhấn [Enter].")
        print("  ✅ Script sẽ tự đóng Chrome và lưu session cho pipeline dùng.")
        print()

        # Chờ user login xong và nhấn Enter
        input("  👉 Nhấn [Enter] khi bạn đã login xong... ")

        print()
        print("  📦 Đang lưu session...")

        # Đóng browser context (Playwright sẽ ghi cookie/storage xuống profile dir)
        await browser.close()

    print("  ✅ Profile IMAGE đã lưu session thành công!")
    print(f"  📁 {PROFILE_DIR_IMAGE}")
    print()
    print("  Pipeline sẽ tự dùng lại session này khi chạy step ảnh.")
    print("  Không cần login lại nữa (trừ khi session hết hạn).")


if __name__ == "__main__":
    asyncio.run(main())
