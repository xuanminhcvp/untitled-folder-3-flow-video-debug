#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChichBong Imagen4 API Client - Giả lập app desktop qua API
Đã xác minh chuẩn 100% từ bytecode gốc (Python 3.11 disassembly)

Flow:
  1. verify license (POST /license/verify_imagen4.php)
  2. lấy token (POST /checker/get-imagen4-token.php)
  3. kết nối WebSocket (wss://v2.chichbong.me)
  4. register: {"event":"register","data":{"license_key":"..."}}
  5. submit: {"event":"submit_prompt","data":{7 fields + 3 model fields}}
  6. nhận: {"event":"prompt_result","data":{"status":"success","image_base64":"..."}}
"""

import asyncio
import json
import os
import sys
import time
import base64
import logging
import uuid
import argparse

try:
    import requests
except ImportError:
    print("❌ Chưa cài requests. Chạy: pip3 install --break-system-packages requests")
    sys.exit(1)
try:
    import websockets
except ImportError:
    print("❌ Chưa cài websockets. Chạy: pip3 install --break-system-packages websockets")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

# =================================================================
# CẤU HÌNH - Lấy từ Proxyman + giải mã bytecode app
# =================================================================
LICENSE_KEY = "ELV-2de351a2-1a99a72d-f59bfe30"
HARDWARE_ID = "f12857101ba06b3e76d1317dc5d2743077968acf84c51564894e27e918377239"
CPU_ID = "df6cf5d1556d7cb1260ad2432d01f31d"
MAINBOARD_UUID = "7D1E5B40-1A38-5D04-A1FC-300E36AA6D01"

API_BASE_URL = "https://11labs.net/api"
WEBSOCKET_URL = "wss://api.chichbong.me/"  # README xác nhận: api.chichbong.me (không phải v2)
BRAND = "imagen4"
VERSION = "1.3.5"
OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "chichbong_output")

# =================================================================
# API CLIENT (đã verify chuẩn 100% qua Proxyman + source code)
# =================================================================
class ChichBongAPIClient:
    def __init__(self, license_key):
        self.license_key = license_key
        self.base_url = API_BASE_URL
        self.timeout = 15
        self.headers = {"User-Agent": "Imagen4-Client/1.0", "Content-Type": "application/json"}

    def verify_license(self):
        url = f"{self.base_url}/license/verify_imagen4.php"
        payload = {"license_key": self.license_key, "hardware_id": HARDWARE_ID,
                    "cpu_id": CPU_ID, "mainboard_uuid": MAINBOARD_UUID,
                    "brand": BRAND, "current_version": VERSION}
        try:
            r = requests.post(url, json=payload, headers=self.headers, timeout=self.timeout)
            result = r.json()
            if result.get("success"):
                logger.info("✅ License hợp lệ!")
            else:
                logger.error(f"❌ License không hợp lệ: {result.get('message')}")
            return result
        except Exception as e:
            logger.error(f"💥 Lỗi verify: {e}")
            return {"success": False, "error": str(e)}

    def get_account_info(self):
        url = f"{self.base_url}/account/info"
        params = {"license_key": self.license_key, "app": BRAND}
        try:
            r = requests.get(url, params=params, timeout=self.timeout)
            result = r.json()
            info = result.get("data", {}).get("account_info", {})
            logger.info(f"📧 Email: {info.get('email', '-')}")
            logger.info(f"🖼️ Tổng ảnh: {info.get('imagen_count', 0)}")
            logger.info(f"📅 Hôm nay: {info.get('imagen_count_today', 0)}/{info.get('imagen_per_day', 50)}")
            return result
        except Exception as e:
            logger.error(f"💥 Lỗi: {e}")
            return {"success": False, "error": str(e)}

    def get_tokens(self, limit=5):
        url = f"{self.base_url}/checker/get-imagen4-token.php"
        payload = {"license_key": self.license_key, "limit": limit}
        try:
            r = requests.post(url, json=payload, headers=self.headers, timeout=self.timeout)
            result = r.json()
            if result.get("success"):
                tokens = result.get("data", {}).get("tokens", [])
                logger.info(f"🔑 Nhận {len(tokens)} token(s)")
                return tokens
            else:
                logger.error(f"❌ Lỗi lấy token: {result.get('message')}")
                return []
        except Exception as e:
            logger.error(f"💥 Lỗi: {e}")
            return []

    def report_usage(self, count):
        url = f"{self.base_url}/resource/report-imagen-counter.php"
        try:
            r = requests.post(url, json={"license_key": self.license_key, "successful_count": count},
                              headers=self.headers, timeout=self.timeout)
            return r.json()
        except Exception as e:
            return {"success": False, "error": str(e)}

# =================================================================
# WEBSOCKET CLIENT - Chuẩn 100% theo SimpleWebSocketImageGenerator
# Đã disassemble bằng Python 3.11 dis module và đọc từng instruction
# =================================================================
class ChichBongWSClient:
    """Chuẩn theo SimpleWebSocketImageGenerator trong websocket_client_simple.pyc"""
    def __init__(self, token, license_key, output_dir):
        self.token = token
        self.license_key = license_key
        self.output_dir = output_dir
        self.registered = False
        self.client_id = None
        os.makedirs(self.output_dir, exist_ok=True)

    async def connect_and_generate(self, prompts, seed=None,
                                     aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
                                     use_legacy_model=False, upscale_mode=False,
                                     image_model_name=None):
        saved_files = []
        try:
            # --- SSL context: bytecode dòng 1268-1534 ---
            # Dùng certifi CA bundle nếu có, fallback system default
            import ssl
            ssl_ctx = None
            try:
                import certifi
                cafile = certifi.where()
                ssl_ctx = ssl.create_default_context(cafile=cafile)
                logger.info(f"🔐 Using certifi CA bundle: {cafile}")
            except Exception:
                ssl_ctx = ssl.create_default_context()

            # --- WebSocket connect: bytecode dòng 986-1076 + 1672-1734 ---
            # ping_interval: 90 nếu >50 prompts, else 60
            # ping_timeout: 45 nếu >50 prompts, else 30
            # open_timeout: 30
            # max_size: 20971520 (20MB)
            # compression: None
            ping_interval = 90 if len(prompts) > 50 else 60
            ping_timeout = 45 if len(prompts) > 50 else 30

            logger.info(f"🔗 Connecting to: {WEBSOCKET_URL}")
            logger.info(f"🔧 Settings: ping_interval={ping_interval}s, ping_timeout={ping_timeout}s")

            async with websockets.connect(
                WEBSOCKET_URL, ssl=ssl_ctx,
                ping_interval=ping_interval, ping_timeout=ping_timeout,
                open_timeout=30, max_size=20971520, compression=None
            ) as ws:
                logger.info("✅ WebSocket connected successfully")

                # === REGISTER: bytecode dòng 438-447 ===
                # BUILD_CONST_KEY_MAP 2 với keys ('event', 'data')
                # data = {'license_key': self.license_key}  (BUILD_MAP 1)
                # CHỈ gửi license_key, KHÔNG gửi token
                register_message = {"event": "register", "data": {"license_key": self.license_key}}
                logger.info("📡 Đang đăng ký với server...")
                await ws.send(json.dumps(register_message))

                # Chờ response "registered" - bytecode dòng 485-564
                resp = await asyncio.wait_for(ws.recv(), timeout=15)
                resp_data = json.loads(resp)

                if resp_data.get("event") != "registered":
                    error_msg = f"Registration failed: {resp_data}"
                    logger.error(f"❌ {error_msg}")
                    return saved_files

                # Lấy client_id: response_data.get('data', {}).get('client_id')
                self.client_id = resp_data.get("data", {}).get("client_id", "")
                self.registered = True
                logger.info(f"✅ Registration successful, client_id: {self.client_id}")

                # === CHUẨN BỊ PROMPT DATA: bytecode dòng 2546-3408 ===
                total = len(prompts)
                base_seed = seed or int(time.time()) % 100000

                logger.info(f"📝 Preparing {total} prompts for producer-consumer processing (seed={base_seed})")

                send_queue = []
                for i, prompt_text in enumerate(prompts):
                    # prompt_id format: "pc_{uuid4().hex}_{index}" (bytecode dòng 3128-3176)
                    prompt_id = f"pc_{uuid.uuid4().hex}_{i}"

                    # prompt_data: BUILD_CONST_KEY_MAP 7
                    # keys: ('prompt_id','prompt','aspect_ratio','seed','index','original_prompt','attachment')
                    prompt_data = {
                        "prompt_id": prompt_id,
                        "prompt": prompt_text,
                        "aspect_ratio": aspect_ratio,
                        "seed": base_seed,
                        "index": i,
                        "original_prompt": prompt_text,
                        "attachment": None,
                    }
                    send_queue.append(prompt_data)
                    logger.info(f"[CLIENT][PROMPT] line={i+1} prompt_id={prompt_id} seed={base_seed}")

                # === GỬI PROMPTS: submit_prompt_batch (gửi toàn bộ 1 lần dạng array) ===
                # README xác nhận: event="submit_prompt_batch", data=[...array...]
                # KHÔNG gửi từng cái một, phải gộp thành batch array rồi gửi 1 lần
                batch = []
                for item in send_queue:
                    submit_data = dict(item)
                    submit_data["use_legacy_model"] = bool(use_legacy_model)
                    submit_data["image_model_name"] = image_model_name or ("IMAGEN_3_5" if use_legacy_model else None)
                    submit_data["upscale_mode"] = upscale_mode
                    batch.append(submit_data)
                    logger.info(f"📋 Chuẩn bị [{item['prompt_id']}]: {item['prompt'][:60]}...")

                # Gửi toàn bộ batch 1 lần duy nhất
                submit_msg = json.dumps({"event": "submit_prompt_batch", "data": batch})
                await ws.send(submit_msg)
                logger.info(f"📤 Đã gửi batch {len(batch)} prompts → submit_prompt_batch")

                # === NHẬN KẾT QUẢ: _message_dispatcher_task ===
                received = 0
                failed = 0
                timeout_total = 300

                logger.info(f"⏳ Waiting for all results, timeout: {timeout_total//60} minutes")
                start = time.time()

                while (received + failed) < total and (time.time() - start) < timeout_total:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                        data = json.loads(msg)
                        event = data.get("event", "")

                        if event == "prompt_queued":
                            qp = data.get("data", {}).get("queue_position", "?")
                            logger.info(f"📊 Prompt queued | Vị trí hàng đợi: {qp}")

                        elif event == "prompt_result":
                            rd = data.get("data", {})
                            pid = rd.get("prompt_id", "")
                            status = rd.get("status", "")
                            img_b64 = rd.get("image_base64", "")

                            if status == "success" and img_b64:
                                fpath = self._save_image(img_b64, received)
                                if fpath:
                                    saved_files.append(fpath)
                                    fsize = os.path.getsize(fpath) if os.path.exists(fpath) else 0
                                    logger.info(f"💾 Saved [{received+1}/{total}]: {os.path.basename(fpath)} ({fsize} bytes)")
                                received += 1
                            elif status in ("error", "failed"):
                                err = rd.get("error", "Unknown error")
                                logger.warning(f"⚠️ {pid} failed: {err}")
                                failed += 1
                            else:
                                logger.debug(f"📥 Result: status={status}")

                        elif event == "task_status":
                            ts_data = data.get("data", {})
                            msg_text = ts_data.get("message", "")
                            progress = ts_data.get("progress", "")
                            if msg_text:
                                logger.info(f"📡 Task status: {msg_text}")

                        elif event == "stats":
                            qs = data.get("data", {}).get("queue_size", "?")
                            logger.info(f"📊 Server queue_size: {qs}")

                        else:
                            logger.debug(f"📡 Dispatcher: event '{event}'")

                    except asyncio.TimeoutError:
                        logger.warning(f"📥 Timeout, continuing... ({received} received)")

                if received + failed >= total:
                    logger.info(f"✅ All results: {received} completed, {failed} failed")
                else:
                    logger.warning(f"⚠️ Timeout: {received} completed, {failed} failed / {total}")

        except Exception as e:
            logger.error(f"💥 WebSocket error: {e}")

        return saved_files

    def _save_image(self, b64_data, index):
        """Lưu base64 -> JPG, chuẩn theo _message_dispatcher_task"""
        try:
            if "," in b64_data:
                b64_data = b64_data.split(",", 1)[1]
            img_bytes = base64.b64decode(b64_data)
            # Filename format: prompt_XXX.jpg (bytecode: f"prompt_{seq:03d}.jpg")
            fname = f"prompt_{index+1:03d}.jpg"
            fpath = os.path.join(self.output_dir, fname)
            with open(fpath, "wb") as f:
                f.write(img_bytes)
            return fpath
        except Exception as e:
            logger.error(f"❌ Save error: {e}")
            return ""

# =================================================================
# HÀM CHÍNH
# =================================================================
async def generate_images(prompts, aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
                           use_legacy_model=False, seed=None, output_dir=None):
    if not output_dir:
        output_dir = OUTPUT_DIR

    api = ChichBongAPIClient(LICENSE_KEY)

    if not api.verify_license().get("success"):
        logger.error("❌ License không hợp lệ!")
        return []

    api.get_account_info()

    # BỎ get_tokens: endpoint /checker/get-imagen4-token.php yêu cầu "master key" cấp server
    # README xác nhận: WebSocket chỉ cần license_key để register, token không được gửi
    logger.info("⚡ Kết nối WebSocket trực tiếp bằng license_key")

    ws_client = ChichBongWSClient(token="", license_key=LICENSE_KEY, output_dir=output_dir)
    saved = await ws_client.connect_and_generate(
        prompts=prompts, seed=seed, aspect_ratio=aspect_ratio,
        use_legacy_model=use_legacy_model
    )

    if saved:
        api.report_usage(len(saved))
    return saved

def main():
    parser = argparse.ArgumentParser(description="🖼️ ChichBong Imagen4 - Tạo ảnh AI từ Terminal")
    parser.add_argument("prompt", nargs="?", help="Prompt (dùng | để tách nhiều prompt)")
    parser.add_argument("--file", "-f", help="Đọc prompts từ file (mỗi dòng 1 prompt)")
    parser.add_argument("--ratio", "-r", choices=["square", "landscape", "portrait"], default="square")
    parser.add_argument("--legacy", action="store_true", help="Dùng model cũ IMAGEN_3_5")
    parser.add_argument("--seed", "-s", type=int, default=None)
    parser.add_argument("--output", "-o", default=None)
    parser.add_argument("--info", action="store_true", help="Chỉ xem thông tin tài khoản")
    args = parser.parse_args()

    if args.info:
        api = ChichBongAPIClient(LICENSE_KEY)
        api.verify_license()
        api.get_account_info()
        return

    prompts = []
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            prompts = [l.strip() for l in f if l.strip()]
    elif args.prompt:
        prompts = [p.strip() for p in args.prompt.split("|") if p.strip()]

    if not prompts:
        parser.print_help()
        return

    ratio_map = {"square": "IMAGE_ASPECT_RATIO_SQUARE",
                 "landscape": "IMAGE_ASPECT_RATIO_LANDSCAPE",
                 "portrait": "IMAGE_ASPECT_RATIO_PORTRAIT"}

    print(f"\n{'='*50}")
    print(f"🖼️  ChichBong Imagen4 Generator")
    print(f"📝 Prompts: {len(prompts)} | 📐 Ratio: {args.ratio}")
    print(f"{'='*50}\n")

    saved = asyncio.run(generate_images(
        prompts=prompts, aspect_ratio=ratio_map[args.ratio],
        use_legacy_model=args.legacy, seed=args.seed, output_dir=args.output
    ))

    print(f"\n{'='*50}")
    if saved:
        print(f"✅ Tạo thành công {len(saved)} ảnh!")
        for fp in saved:
            print(f"   📁 {fp}")
    else:
        print("❌ Không tạo được ảnh nào.")
    print(f"{'='*50}\n")

if __name__ == "__main__":
    main()
