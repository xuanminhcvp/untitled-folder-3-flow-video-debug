#!/usr/bin/env python3
"""
proxy_bridge.py
───────────────
Khởi động nhiều local proxy bridge dùng gost.

Vấn đề: Chromium/Playwright KHÔNG hỗ trợ SOCKS5 proxy có authentication.
Giải pháp: Chạy gost làm proxy trung gian:
  Chrome → localhost:LOCAL_PORT → gost → socks5://USER:PASS@REAL_IP:REAL_PORT

Cách dùng:
  python3 proxy_bridge.py start   → khởi động tất cả bridge
  python3 proxy_bridge.py stop    → dừng tất cả bridge
  python3 proxy_bridge.py status  → kiểm tra trạng thái
  python3 proxy_bridge.py test    → test kết nối qua bridge

Bridge mapping:
  video_1: localhost:11001 → socks5://HPseFo:IpmDzM@118.70.187.141:55508
  video_2: localhost:11002 → socks5://cQPhPD:psxyrr@118.70.171.107:12055
  video_3: localhost:11003 → socks5://dyiuTU:jkkAOj@118.70.187.141:57444
  video_4: localhost:11004 → socks5://zZdAHY:fDWwOC@113.160.166.150:55923
  video_5: localhost:11005 → socks5://BRBJmF:dBTPyg@14.241.72.152:19437
"""

from __future__ import annotations
import json, os, sys, subprocess, time, socket
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_SCRIPT_DIR, "config", "video_workers.json")
GOST_PATH   = "/tmp/gost"          # đường dẫn gost binary
PID_FILE    = "/tmp/proxy_bridge_pids.json"  # lưu PID để stop sau

# Port local bắt đầu từ 11001
LOCAL_PORT_START = 11001
TEST_URLS = [
    "https://labs.google/fx/vi/tools/flow",
    "https://accounts.google.com/",
    "https://api.ipify.org?format=json",
]


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_proxy_real_parts(proxy_str: str) -> tuple[str, str, str, str, str]:
    """
    Parse proxy THẬT dạng 'socks5://USER:PASS@IP:PORT'
    Trả về: (proto, user, password, ip, port)
    Chỉ dùng với proxy có auth — đây là proxy gost sẽ forward tới.
    """
    proto_rest = proxy_str.split("://", 1)
    proto = proto_rest[0]
    rest  = proto_rest[1]
    if "@" not in rest:
        raise ValueError(f"proxy_real phải có auth (USER:PASS@IP:PORT), nhận được: '{proxy_str}'")
    creds_host = rest.split("@", 1)
    user, password = creds_host[0].split(":", 1)
    ip, port = creds_host[1].rsplit(":", 1)
    return proto, user, password, ip, port


def get_bridge_configs(workers: list[dict]) -> list[dict]:
    """
    Build danh sách bridge config từ danh sách worker.
    Đọc field 'proxy_real' (proxy thật có auth) để gost forward.
    Field 'proxy' là local bridge (socks5://127.0.0.1:1100X) mà Chrome sẽ dùng.
    """
    bridges = []
    for i, w in enumerate(workers):
        # Ưu tiên proxy_real; nếu không có thì thử proxy (format cũ trước khi cập nhật)
        proxy_real_str = w.get("proxy_real") or w.get("proxy", "")
        if not proxy_real_str or "@" not in proxy_real_str:
            # Không phải proxy có auth → không cần bridge
            continue
        local_port = LOCAL_PORT_START + i
        try:
            proto, user, password, ip, port = parse_proxy_real_parts(proxy_real_str)
        except Exception as e:
            print(f"  [WARN] Bỏ qua {w['worker_id']}: {e}")
            continue
        bridges.append({
            "worker_id":   w["worker_id"],
            "local_port":  local_port,
            "local_addr":  f"socks5://:{local_port}",
            "remote":      f"{proto}://{user}:{password}@{ip}:{port}",
            "proxy_real":  proxy_real_str,
            "proxy_local": w.get("proxy", f"socks5://127.0.0.1:{local_port}"),
        })
    return bridges


def save_pids(pid_map: dict):
    with open(PID_FILE, "w") as f:
        json.dump(pid_map, f)


def load_pids() -> dict:
    if os.path.exists(PID_FILE):
        with open(PID_FILE, "r") as f:
            return json.load(f)
    return {}


def is_port_open(port: int) -> bool:
    """Kiểm tra port local có đang listen không."""
    try:
        s = socket.socket()
        s.settimeout(0.5)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except Exception:
        return False


def find_listen_pid_by_port(port: int) -> int | None:
    """Tìm PID process đang listen port local (nếu có)."""
    try:
        proc = subprocess.run(
            ["lsof", "-nP", "-iTCP:%d" % int(port), "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode != 0:
            return None
        out = (proc.stdout or "").strip().splitlines()
        if not out:
            return None
        return int(out[0].strip())
    except Exception:
        return None


def cmd_start():
    """Khởi động tất cả gost bridge."""
    if not os.path.exists(GOST_PATH):
        print(f"[ERROR] Không tìm thấy gost tại {GOST_PATH}")
        print(f"  Chạy: curl -sL https://github.com/go-gost/gost/releases/download/v3.0.0-rc10/gost_3.0.0-rc10_darwin_amd64.tar.gz -o /tmp/gost.tar.gz && tar -xzf /tmp/gost.tar.gz -C /tmp && chmod +x /tmp/gost")
        sys.exit(1)

    config   = load_config()
    workers  = config.get("video_workers", [])
    bridges  = get_bridge_configs(workers)
    # Giữ lại PID map cũ để không làm mất thông tin worker đã chạy từ trước.
    pid_map  = load_pids()

    print(f"\n  Khởi động {len(bridges)} proxy bridge...\n")

    for b in bridges:
        worker_id  = b["worker_id"]
        local_port = b["local_port"]

        # Nếu port đã mở → skip (đã chạy rồi)
        if is_port_open(local_port):
            print(f"  ⚠️  {worker_id}: port {local_port} đã mở, bỏ qua.")
            live_pid = find_listen_pid_by_port(local_port)
            if live_pid:
                pid_map[worker_id] = {
                    "pid":        live_pid,
                    "local_port": local_port,
                    "remote":     b["remote"],
                }
            continue

        # Khởi động gost: lắng nghe localhost:local_port → forward đến remote proxy
        # gost -L socks5://:11001 -F socks5://USER:PASS@IP:PORT
        cmd = [
            GOST_PATH,
            "-L", f"socks5://:{local_port}",
            "-F", b["remote"],
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        pid_map[worker_id] = {
            "pid":        proc.pid,
            "local_port": local_port,
            "remote":     b["remote"],
        }
        # Chờ lâu hơn một chút để phát hiện trường hợp process bind xong rồi chết ngay.
        time.sleep(0.8)

        if is_port_open(local_port):
            print(f"  ✅ {worker_id}: localhost:{local_port} → {b['proxy_real']} (PID={proc.pid})")
        else:
            print(f"  ❌ {worker_id}: port {local_port} không mở được!")

    save_pids(pid_map)
    print(f"\n  Bridge đang chạy. PID lưu tại: {PID_FILE}")
    print(f"  Dừng tất cả: python3 proxy_bridge.py stop\n")


def cmd_stop():
    """Dừng tất cả gost bridge."""
    pid_map = load_pids()
    if not pid_map:
        print("  Không có bridge nào đang chạy.")
        return
    for worker_id, info in pid_map.items():
        pid = info.get("pid")
        try:
            os.kill(pid, 15)  # SIGTERM
            print(f"  ✅ Đã dừng {worker_id} (PID={pid})")
        except ProcessLookupError:
            print(f"  ⚠️  {worker_id} (PID={pid}) đã dừng rồi.")
        except Exception as e:
            print(f"  ❌ {worker_id}: {e}")
    os.remove(PID_FILE)
    print()


def cmd_status():
    """Kiểm tra trạng thái tất cả bridge."""
    config  = load_config()
    workers = config.get("video_workers", [])
    bridges = get_bridge_configs(workers)
    pid_map = load_pids()

    print(f"\n  {'Worker':<12} {'Local Port':<12} {'Status':<10} {'Remote'}")
    print(f"  {'-'*70}")
    for b in bridges:
        worker_id  = b["worker_id"]
        local_port = b["local_port"]
        is_up      = is_port_open(local_port)
        pid_cached = pid_map.get(worker_id, {}).get("pid", "?")
        pid_live = find_listen_pid_by_port(local_port)
        pid_show = pid_live if pid_live else pid_cached
        status     = f"✅ UP  (PID={pid_show})" if is_up else "❌ DOWN"
        print(f"  {worker_id:<12} :{local_port:<11} {status:<22} {b['proxy_real']}")
    print()


def probe_bridge_http(local_port: int, url: str, timeout_sec: int = 8) -> tuple[bool, str]:
    """
    Probe HTTPS tunnel qua SOCKS5 local bridge tới URL đích.
    """
    try:
        result = subprocess.run(
            [
                "curl",
                "-I",
                "-sS",
                "-L",
                "--proxy", f"socks5h://127.0.0.1:{local_port}",
                "--connect-timeout", "4",
                "--max-time", str(int(timeout_sec)),
                url,
            ],
            capture_output=True,
            text=True,
        )
        out = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        ok = result.returncode == 0 and ("HTTP/" in out or "location:" in out.lower())
        return ok, out[-400:]
    except Exception as e:
        return False, str(e)


def cmd_test():
    """Test HTTPS tunnel usable qua từng bridge."""
    config  = load_config()
    workers = config.get("video_workers", [])
    bridges = get_bridge_configs(workers)

    print(f"\n  Test HTTPS tunnel qua {len(bridges)} bridge...\n")
    for b in bridges:
        worker_id  = b["worker_id"]
        local_port = b["local_port"]

        if not is_port_open(local_port):
            print(f"  ❌ {worker_id}: port {local_port} chưa mở")
            continue

        ok_any = False
        for url in TEST_URLS:
            ok, detail = probe_bridge_http(local_port, url)
            if ok:
                print(f"  ✅ {worker_id}: tunnel OK via {url}")
                ok_any = True
                break
            print(f"  ⚠️  {worker_id}: fail via {url} :: {detail[:120]}")

        if not ok_any:
            print(f"  ❌ {worker_id}: bridge LISTEN nhưng HTTPS tunnel không usable")
    print()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        cmd_start()
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "status":
        cmd_status()
    elif cmd == "test":
        cmd_test()
    else:
        print(f"Dùng: python3 proxy_bridge.py [start|stop|status|test]")
