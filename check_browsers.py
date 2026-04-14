import asyncio
import json
import os
from playwright.async_api import async_playwright

async def open_all():
    with open("config/video_workers.json") as f:
        config = json.load(f)
    print("Mở 5 Chrome profile để kiểm tra tài khoản...")
    async with async_playwright() as p:
        browsers = []
        for worker in config.get("video_workers", []):
            wid = worker["worker_id"]
            pdir = os.path.abspath(worker["profile_dir"])
            proxy = worker.get("proxy")
            proxy_cfg = {"server": proxy} if proxy else None
            print(f" - Đang mở {wid}...")
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
        
        print("\n=> Đã bật xong 5 Chrome! Bạn check tài khoản đi nha. Terminal này sẽ treo giữ Chrome không bị tắt.")
        print("Khi nào check xong bạn cứ bảo tôi đóng lại.")
        while True:
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(open_all())
