import re

with open("parallel_runner.py", "r", encoding="utf-8") as f:
    text = f.read()

# PATCH 1: the return 99 for worker logic
target1 = """                    try:
                        saved = await dreamina.run_google_flow_auto_video_request_response(page, run_prompts)
                    finally:
                        await page.close()

                    # Nếu preload"""

replacement1 = """                    try:
                        saved = await dreamina.run_google_flow_auto_video_request_response(page, run_prompts)
                    finally:
                        await page.close()

                    if int(saved) == -1:
                        dreamina.log(
                            f"[{worker_id}] Worker bị khóa 5 tiếng (Unusual Activity strikes). TẮT WORKER.",
                            "ERR",
                        )
                        return 99

                    # Nếu preload"""

text = text.replace(target1, replacement1)


# PATCH 2: Orchestrator Loop
target2 = """    # ────────────────────────────────────────────────────────────────────────
    # STEP 1: Tạo ảnh reference tuần tự bằng Chrome IMAGE"""

target2_end = """    print("="*65 + "\\n")"""

start_idx = text.find(target2)
if start_idx == -1:
    print("Cannot find target2")
    exit(1)
end_idx = text.find(target2_end, start_idx) + len(target2_end)

new_orchestrator = """    # ────────────────────────────────────────────────────────────────────────
    # STEP 1: Lấy danh sách kịch bản (Tuần tự hoặc có ảnh sẵn)
    # ────────────────────────────────────────────────────────────────────────
    pending_scenarios = []

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
                if not scenario_dir: continue
                scenario_name = worker.get("worker_id", f"scenario_{i+1}")
                log(f"[{i+1}/{len(video_workers)}] Tạo ảnh cho: {scenario_dir}", "STEP")
                image_ok = await run_image_step_for_scenario(p, browser_img, scenario_dir, scenario_name)
                if image_ok:
                    pending_scenarios.append(scenario_dir)
                else:
                    log(f"Bỏ qua kịch bản {scenario_name} vì tạo ảnh thất bại.", "ERR")

            await browser_img.close()
            log("Chrome IMAGE đã đóng.", "OK")
    else:
        log("--video-only: Bỏ qua tạo ảnh reference...", "INFO")
        for worker in video_workers:
            if worker.get("scenario_dir"):
                pending_scenarios.append(worker.get("scenario_dir"))

    # ────────────────────────────────────────────────────────────────────────
    # STEP 2: Orchestrator Loop điều phối Worker & Scenario
    # ────────────────────────────────────────────────────────────────────────
    import time
    banned_workers = {}
    free_workers = list(video_workers)
    running_subprocesses = {}

    log(f"Bắt đầu điều phối {len(pending_scenarios)} kịch bản với {len(free_workers)} workers...", "INFO")

    while pending_scenarios or running_subprocesses:
        # 1. Thu hồi workers
        for proc in list(running_subprocesses.keys()):
            ret = proc.poll()
            if ret is not None:
                worker, s_dir = running_subprocesses.pop(proc)
                w_id = worker["worker_id"]
                if ret == 99:
                    log(f"Worker {w_id} BỊ BAN BỞI GOOGLE! Sắp xếp vào danh sách nghỉ 5 tiếng.", "WARN")
                    banned_workers[w_id] = time.time() + 5 * 3600
                    if pending_scenarios:
                        pending_scenarios.insert(1, s_dir)  # Trả lại kịch bản vào queue (vị trí thứ 2 để worker trống khác nhận)
                    else:
                        pending_scenarios.insert(0, s_dir)
                elif ret != 0:
                    log(f"Worker {w_id} kết thúc lỗi ({ret}) ở {s_dir}. Bỏ qua kịch bản này.", "ERR")
                    free_workers.append(worker)
                else:
                    log(f"Worker {w_id} hoàn thành thành công kịch bản {s_dir}.", "OK")
                    free_workers.append(worker)

        # 2. Phục hồi workers hết hạn Ban
        now = time.time()
        for w_id in list(banned_workers.keys()):
            if now > banned_workers[w_id]:
                log(f"Worker {w_id} đã hết án phạt nghỉ 5 tiếng. Quay lại làm việc.", "OK")
                del banned_workers[w_id]
                orig = next((w for w in video_workers if w["worker_id"] == w_id), None)
                if orig: free_workers.append(orig)

        # 3. Giao việc cho free workers
        while pending_scenarios and free_workers:
            s_dir = pending_scenarios.pop(0)
            worker = free_workers.pop(0)
            w_id = worker["worker_id"]
            output_dir = os.path.join(s_dir, "output")

            log(f"Giao kịch bản {s_dir} cho worker trống: {w_id} ...", "RUN")
            worker_env = build_worker_env(worker_id=w_id, worker_index=len(video_workers))
            proc = subprocess.Popen(
                [
                    sys.executable,
                    os.path.join(_SCRIPT_DIR, "parallel_runner.py"),
                    "--_internal-video-worker",
                    json.dumps({
                        "worker_id":    w_id,
                        "profile_dir":  expand_path(worker["profile_dir"]),
                        "proxy":        worker.get("proxy"),
                        "scenario_dir": s_dir,
                        "output_dir":   output_dir,
                    }),
                ],
                cwd=_SCRIPT_DIR,
                env=worker_env,
            )
            running_subprocesses[proc] = (worker, s_dir)
            
            # Stagger nhẹ để tránh bật cùng 1 ms
            await asyncio.sleep(WORKER_STAGGER_SEC)

        await asyncio.sleep(5)

    print("\\n" + "="*65)
    log(f"TẤT CẢ KỊCH BẢN HOÀN THÀNH HOẶC ĐÃ XỬ LÝ LỖI!", "DONE")
    print("="*65 + "\\n")"""

text = text[:start_idx] + new_orchestrator + text[end_idx:]

with open("parallel_runner.py", "w", encoding="utf-8") as f:
    f.write(text)

print("Patching complete!")
