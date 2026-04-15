import asyncio
import json
import os
from playwright.async_api import async_playwright

async def open_profiles():
    with open("config/video_workers.json") as f:
        config = json.load(f)
    print("Mở Chrome profile 6, 7, 8 để đăng nhập...")
    async with async_playwright() as p:
        browsers = []
        for worker in config.get("video_workers", []):
            wid = worker["worker_id"]
            if wid not in ["video_6", "video_7", "video_8"]:
                continue
            pdir = os.path.abspath(worker["profile_dir"])
            proxy = worker.get("proxy")
            proxy_cfg = {"server": proxy} if proxy else None
            print(f" - Đang mở {wid} qua cổng proxy {proxy}...")
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=pdir,
                headless=False,
                channel="chrome",
                args=[
                    "--start-maximized",
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                ignore_default_args=["--enable-automation"],
                proxy=proxy_cfg,
                viewport={"width": 1280, "height": 720}
            )
            page = await ctx.new_page()
            try:
                await page.goto("https://labs.google/fx/vi/tools/flow", wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                print(f"Lỗi load trang {wid}: {e}")
            browsers.append(ctx)
            await asyncio.sleep(1)
        
        print("\n=> Đã bật xong 3 Chrome (6, 7, 8)! Bạn đăng nhập tài khoản đi nha.")
        while True:
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(open_profiles())
