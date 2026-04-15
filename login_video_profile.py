"""
Login profile VIDEO bằng Playwright.

Mở Chrome đúng profile mà pipeline sẽ dùng cho step tạo video.
Login xong → nhấn Enter trong terminal → profile sẽ lưu session.

Lần sau chạy pipeline, Playwright đọc lại đúng session từ profile này → không cần login lại.
"""

import asyncio
import os
from pathlib import Path
from playwright.async_api import async_playwright

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Đường dẫn profile được thiết lập để lưu vào ngay trong thư mục clone
PROFILE_DIR_VIDEO = os.environ.get(
    "PROFILE_DIR_VIDEO",
    os.path.join(_SCRIPT_DIR, "chrome_profiles", "dreamina_playwright_profile_video"),
).strip()

GOOGLE_FLOW_HOME = "https://labs.google/fx/vi/tools/flow"


async def main():
    print("=" * 60)
    print("  LOGIN PROFILE VIDEO (dùng cho step tạo video)")
    print(f"  Profile: {PROFILE_DIR_VIDEO}")
    print("=" * 60)

    # Tạo folder profile nếu chưa có
    Path(PROFILE_DIR_VIDEO).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # Mở Chrome persistent context — cùng config với pipeline
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

    print("  ✅ Profile VIDEO đã lưu session thành công!")
    print(f"  📁 {PROFILE_DIR_VIDEO}")
    print()
    print("  Pipeline sẽ tự dùng lại session này khi chạy step video.")
    print("  Không cần login lại nữa (trừ khi session hết hạn).")


if __name__ == "__main__":
    asyncio.run(main())
