"""
Service dựng/lưu báo cáo debug cho Dreamina/Google Flow.

Thiết kế theo hàm thuần:
- Input: dữ liệu đã thu được trong runtime.
- Output: payload/timeline/path file báo cáo.
Mục tiêu: giảm độ phình của dreamina.py nhưng giữ nguyên hành vi cũ.
"""

import glob
import json
import os
import time
from datetime import datetime


def write_json_report(debug_session_dir: str, filename: str, payload) -> str:
    """
    Ghi payload JSON vào thư mục debug session.
    Trả về đường dẫn file nếu thành công, ngược lại trả chuỗi rỗng.
    """
    if not debug_session_dir:
        return ""
    path = os.path.join(debug_session_dir, filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return ""


def build_api_debug_payload(
    api_events: list,
    scene_to_task_ids: dict,
    task_to_image_urls: dict,
    scene_to_image_urls: dict,
    scene_to_media_ids: dict,
    scene_to_video_media_ids: dict,
    scene_to_video_ready_media_ids: dict,
    scene_to_video_failed_media_ids: dict,
    video_media_status_by_id: dict,
    video_download_events: list,
    flow_ui_error_events: list,
    upscale_success_by_media: dict,
    last_flow_client_context: dict,
    submit_to_scene: dict,
) -> dict:
    """Dựng payload debug API chi tiết để soi map scene/task/media."""
    return {
        "events": api_events,
        "scene_to_task_ids": scene_to_task_ids,
        "task_to_image_urls": task_to_image_urls,
        "scene_to_image_urls": scene_to_image_urls,
        "scene_to_media_ids": scene_to_media_ids,
        "scene_to_video_media_ids": scene_to_video_media_ids,
        "scene_to_video_ready_media_ids": scene_to_video_ready_media_ids,
        "scene_to_video_failed_media_ids": scene_to_video_failed_media_ids,
        "video_media_status_by_id": video_media_status_by_id,
        "video_download_events_count": len(video_download_events),
        "flow_ui_error_events_count": len(flow_ui_error_events),
        "upscale_success_media_ids": sorted(list(upscale_success_by_media.keys())),
        "last_flow_client_context_exists": bool(last_flow_client_context),
        "submit_to_scene": submit_to_scene,
    }


def build_upscale_debug_payload(upscale_events: list, upscale_success_by_media: dict) -> dict:
    """Dựng payload debug cho luồng upscale 2K."""
    return {
        "generated_at": datetime.now().isoformat(),
        "events_count": len(upscale_events),
        "events": upscale_events,
        "success_media_ids": sorted(list(upscale_success_by_media.keys())),
    }


def build_video_error_debug_payload(
    network_events: list,
    flow_ui_error_events: list,
    video_download_events: list,
    has_rate_limit_ui_error,
    has_audiovisual_load_ui_error,
    looks_like_flow_media_url,
) -> dict:
    """Dựng payload debug chuyên sâu cho lỗi video."""
    ui_messages = []
    for ev in flow_ui_error_events:
        ui_messages.extend(ev.get("messages", []) or [])
    is_rate_limit = bool(has_rate_limit_ui_error(ui_messages))
    is_audiovisual_load = bool(has_audiovisual_load_ui_error(ui_messages))

    media_error_events = []
    for ev in network_events:
        if ev.get("type") not in {"media_response", "request_failed"}:
            continue
        if not looks_like_flow_media_url(ev.get("url", "")):
            continue
        status = int(ev.get("status", 0) or 0) if str(ev.get("status", "")).isdigit() else 0
        body_size = int(ev.get("body_size", 0) or 0) if str(ev.get("body_size", "")).isdigit() else 0
        ct = str(ev.get("content_type", "") or "").lower()
        is_error_like = False
        if ev.get("type") == "request_failed":
            is_error_like = True
        elif status >= 400:
            is_error_like = True
        elif body_size and body_size < 1024 and ("text" in ct or "json" in ct or "xml" in ct or "html" in ct):
            is_error_like = True
        if is_error_like:
            media_error_events.append(ev)

    return {
        "generated_at": datetime.now().isoformat(),
        "error_classifier": {
            "has_rate_limit_ui_error": is_rate_limit,
            "has_audiovisual_load_ui_error": is_audiovisual_load,
        },
        "ui_error_events_count": len(flow_ui_error_events),
        "ui_error_events": flow_ui_error_events,
        "video_download_events_count": len(video_download_events),
        "video_download_events": video_download_events,
        "media_error_events_count": len(media_error_events),
        "media_error_events": media_error_events,
    }


def build_flow_video_scene_report_payload(
    prompts: list,
    download_hash_records: list,
    flow_ui_error_events: list,
    video_download_events: list,
    scene_to_video_media_ids: dict,
    scene_to_video_ready_media_ids: dict,
    scene_to_video_failed_media_ids: dict,
    video_media_status_by_id: dict,
    extract_scene_number,
    has_rate_limit_ui_error,
    has_audiovisual_load_ui_error,
) -> dict:
    """Dựng báo cáo scene video dạng dễ đọc."""
    success_by_scene = {}
    for row in download_hash_records:
        if not isinstance(row, dict):
            continue
        fname = str(row.get("filename", "") or "")
        if not fname.lower().endswith(".mp4"):
            continue
        scene_no = int(row.get("prompt_num", 0) or 0)
        if scene_no > 0:
            success_by_scene.setdefault(scene_no, []).append(row)

    ui_messages = []
    for ev in flow_ui_error_events:
        ui_messages.extend(ev.get("messages", []) or [])
    has_rate_limit = bool(has_rate_limit_ui_error(ui_messages))
    has_audiovisual = bool(has_audiovisual_load_ui_error(ui_messages))

    scenes = []
    prompt_scene_order = [extract_scene_number(p, i + 1) for i, p in enumerate(prompts or [])]
    scene_prompt_preview = {}
    for i, p in enumerate(prompts or []):
        sc = extract_scene_number(p, i + 1)
        if sc not in scene_prompt_preview:
            scene_prompt_preview[sc] = p[:180]

    for scene_no in prompt_scene_order:
        media_ids = scene_to_video_media_ids.get(scene_no, []) or []
        ready_ids = scene_to_video_ready_media_ids.get(scene_no, []) or []
        failed_ids = scene_to_video_failed_media_ids.get(scene_no, []) or []
        attempts = [ev for ev in video_download_events if int(ev.get("scene_no", 0) or 0) == scene_no]
        small_count = sum(1 for ev in attempts if str(ev.get("phase", "")) == "gcs_body_too_small")
        ok_count = sum(1 for ev in attempts if str(ev.get("phase", "")).startswith("download_ok"))
        success_records = success_by_scene.get(scene_no, [])

        reason = "unknown"
        if success_records:
            reason = "success"
        elif has_audiovisual:
            reason = "ui_audiovisual_load_error"
        elif has_rate_limit:
            reason = "rate_limit_or_throttle"
        elif attempts and small_count == len(attempts):
            reason = "all_attempts_returned_small_partial_mp4"
        elif attempts and ok_count == 0:
            reason = "download_attempted_but_not_ready"
        elif not attempts and media_ids:
            reason = "have_media_ids_but_no_download_attempts"
        elif not media_ids:
            reason = "no_media_id_detected_for_scene"

        scenes.append(
            {
                "scene_no": scene_no,
                "prompt_preview": scene_prompt_preview.get(scene_no, ""),
                "media_ids": media_ids,
                "ready_media_ids": ready_ids,
                "failed_media_ids": failed_ids,
                "media_status_by_id": {mid: video_media_status_by_id.get(mid, "") for mid in media_ids},
                "download_attempts_count": len(attempts),
                "small_partial_mp4_count": small_count,
                "download_ok_count": ok_count,
                "download_success_count": len(success_records),
                "download_success_records": success_records,
                "suspected_reason": reason,
            }
        )

    return {
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total_scenes": len(prompt_scene_order),
            "downloaded_scenes": len(success_by_scene),
            "failed_scenes": max(0, len(prompt_scene_order) - len(success_by_scene)),
            "downloaded_videos": sum(len(rows) for rows in success_by_scene.values()),
            "has_rate_limit_ui_error": has_rate_limit,
            "has_audiovisual_load_ui_error": has_audiovisual,
        },
        "scenes": scenes,
    }


def build_request_response_timeline_lines(
    api_events: list,
    network_events: list,
    flow_ui_error_events: list,
    video_download_events: list,
    target_platform: str,
) -> list[str]:
    """Dựng nội dung timeline request/response dạng text dễ đọc."""
    rows = []

    for ev in api_events:
        ts = datetime.fromtimestamp(ev.get("ts", time.time())).strftime("%H:%M:%S")
        ev_type = ev.get("type", "")
        url = str(ev.get("url", ""))
        if len(url) > 140:
            url = url[:140] + "...(cut)"

        if ev_type == "api_request":
            prefix = "UPSCALE_REQUEST" if ev.get("is_upscale") else "API_REQUEST"
            rows.append(
                f"[{ts}] {prefix:<14} method={ev.get('method', '')} "
                f"scene={ev.get('scene_numbers', [])} url={url}"
            )
        elif ev_type == "api_response":
            prefix = "UPSCALE_RESPONSE" if ev.get("is_upscale") else "API_RESPONSE"
            video_updates = ev.get("video_media_updates_count", 0)
            rows.append(
                f"[{ts}] {prefix:<14} status={ev.get('status', '')} "
                f"scene={ev.get('scene_numbers', [])} task_ids={len(ev.get('task_ids', []))} "
                f"image_urls={ev.get('image_urls_count', 0)} "
                f"video_urls={ev.get('video_urls_count', 0)} "
                f"video_media_updates={video_updates} url={url}"
            )

    for ev in network_events:
        ts = datetime.fromtimestamp(ev.get("ts", time.time())).strftime("%H:%M:%S")
        ev_type = ev.get("type", "")
        url = str(ev.get("url", ""))
        if len(url) > 140:
            url = url[:140] + "...(cut)"

        if ev_type == "request":
            rows.append(
                f"[{ts}] IMG_REQUEST  method={ev.get('method', '')} "
                f"rtype={ev.get('resource_type', '')} url={url}"
            )
        elif ev_type == "response":
            rows.append(
                f"[{ts}] IMG_RESPONSE status={ev.get('status', '')} "
                f"rtype={ev.get('resource_type', '')} "
                f"elapsed_ms={ev.get('elapsed_ms', '')} ct={ev.get('content_type', '')} url={url}"
            )
        elif ev_type == "request_failed":
            rows.append(
                f"[{ts}] IMG_FAILED   rtype={ev.get('resource_type', '')} "
                f"error={ev.get('failure', '')} url={url}"
            )
        elif ev_type == "media_response":
            bsz = ev.get("body_size", "")
            rows.append(
                f"[{ts}] MEDIA_RESP   status={ev.get('status', '')} "
                f"rtype={ev.get('resource_type', '')} bytes={bsz} "
                f"ct={ev.get('content_type', '')} url={url}"
            )

    for ev in flow_ui_error_events:
        ts = datetime.fromtimestamp(ev.get("ts", time.time())).strftime("%H:%M:%S")
        msg = " | ".join(ev.get("messages", []) or [])
        if len(msg) > 180:
            msg = msg[:180] + "...(cut)"
        rows.append(f"[{ts}] UI_ERROR     label={ev.get('label', '')} msg={msg}")

    for ev in video_download_events:
        ts = datetime.fromtimestamp(ev.get("ts", time.time())).strftime("%H:%M:%S")
        rows.append(
            f"[{ts}] VIDEO_DL     scene={ev.get('scene_no')} attempt={ev.get('attempt')} "
            f"media={ev.get('media_id_short', '')} status={ev.get('media_status', '')} "
            f"redirect={ev.get('redirect_status', '')} gcs={ev.get('gcs_status', '')} "
            f"bytes={ev.get('body_size', '')} ct={ev.get('content_type', '')}"
        )

    rows_sorted = sorted(rows)
    lines = [
        "=== REQUEST / RESPONSE TIMELINE ===",
        f"platform={target_platform}",
        f"generated_at={datetime.now().isoformat()}",
        "",
    ]
    lines.extend(rows_sorted)
    return lines


def build_api_scene_first_image_map(
    prompts: list,
    extract_scene_number,
    scene_to_image_urls: dict,
    scene_to_task_ids: dict,
    task_to_image_urls: dict,
) -> dict:
    """Dựng map scene -> image_url đầu tiên theo ưu tiên direct rồi qua task."""
    out = {}
    for i, p in enumerate(prompts):
        scene_no = extract_scene_number(p, i + 1)
        direct = scene_to_image_urls.get(scene_no, [])
        if direct:
            out[scene_no] = direct[0]
            continue
        tids = scene_to_task_ids.get(scene_no, [])
        for tid in tids:
            urls = task_to_image_urls.get(tid, [])
            if urls:
                out[scene_no] = urls[0]
                break
    return out


def get_scene_candidate_urls(
    scene_no: int,
    preferred_url: str,
    scene_to_image_urls: dict,
    scene_to_task_ids: dict,
    task_to_image_urls: dict,
) -> list[str]:
    """Lấy danh sách URL ứng viên cho 1 scene theo độ ưu tiên."""
    urls = []
    if preferred_url:
        urls.append(preferred_url)

    direct = scene_to_image_urls.get(scene_no, []) or []
    urls.extend(direct)

    tids = scene_to_task_ids.get(scene_no, []) or []
    for tid in tids:
        task_urls = task_to_image_urls.get(tid, []) or []
        urls.extend(task_urls)

    return list(dict.fromkeys([u for u in urls if isinstance(u, str) and u.startswith("http")]))


def get_scene_candidate_video_media_ids(
    scene_no: int,
    scene_to_video_ready_media_ids: dict,
    scene_to_video_media_ids: dict,
    scene_to_video_failed_media_ids: dict,
) -> list[str]:
    """Lấy danh sách mediaId video theo thứ tự READY -> pending -> FAILED."""
    ready = list(reversed(scene_to_video_ready_media_ids.get(scene_no, []) or []))
    all_ids = list(reversed(scene_to_video_media_ids.get(scene_no, []) or []))
    failed = set(scene_to_video_failed_media_ids.get(scene_no, []) or [])

    ordered = []
    ordered.extend(ready)
    ordered.extend([mid for mid in all_ids if mid not in ready and mid not in failed])
    ordered.extend([mid for mid in all_ids if mid in failed])

    return list(dict.fromkeys([mid for mid in ordered if isinstance(mid, str) and mid]))


def build_download_hash_payload(download_hash_records: list) -> dict:
    """Dựng payload hash file tải về để phát hiện trùng ảnh/video."""
    dup_map = {}
    for row in download_hash_records:
        sha = row.get("sha256", "")
        if not sha:
            continue
        dup_map.setdefault(sha, []).append(row.get("filename", ""))

    return {
        "generated_at": datetime.now().isoformat(),
        "count": len(download_hash_records),
        "records": download_hash_records,
        "duplicates": {k: v for k, v in dup_map.items() if len(v) > 1},
    }


def read_json_file(path: str):
    """Đọc JSON an toàn, lỗi thì trả None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def build_session_metrics(session_dir: str) -> dict:
    """Gom các chỉ số debug chính của một session."""
    gallery = read_json_file(os.path.join(session_dir, "gallery_snapshot_before_download.json")) or {}
    hashes = read_json_file(os.path.join(session_dir, "download_hashes.json")) or {}
    scroll = read_json_file(os.path.join(session_dir, "scroll_trace_before_download.json")) or {}

    new_srcs = gallery.get("new_srcs", []) or []
    records = hashes.get("records", []) or []
    hash_values = [r.get("sha256", "") for r in records if r.get("sha256")]
    unique_hashes = set(hash_values)
    dup_hash_count = sum(1 for _, files in (hashes.get("duplicates", {}) or {}).items() if len(files) > 1)

    steps = scroll.get("steps", []) or []
    mounted_peaks = max([s.get("mounted_count", 0) for s in steps], default=0)
    unique_peaks = max([s.get("unique_src_count", 0) for s in steps], default=0)

    return {
        "session_dir": session_dir,
        "session_name": os.path.basename(session_dir),
        "new_srcs_count": len(new_srcs),
        "new_srcs": new_srcs,
        "download_count": len(records),
        "unique_hash_count": len(unique_hashes),
        "dup_hash_count": dup_hash_count,
        "scroll_steps": len(steps),
        "scroll_mounted_peak": mounted_peaks,
        "scroll_unique_peak": unique_peaks,
    }


def compare_with_previous_session(debug_dir: str, debug_session_dir: str) -> tuple[str, dict | None]:
    """
    So sánh session hiện tại với session gần nhất trước đó.
    Trả về (previous_dir, report_dict). Nếu không đủ dữ liệu thì trả ("", None).
    """
    if not debug_session_dir:
        return "", None

    sessions = sorted(glob.glob(os.path.join(debug_dir, "session_*")), key=os.path.getmtime)
    previous = [s for s in sessions if os.path.abspath(s) != os.path.abspath(debug_session_dir)]
    if not previous:
        return "", None
    prev_dir = previous[-1]

    current_metrics = build_session_metrics(debug_session_dir)
    prev_metrics = build_session_metrics(prev_dir)

    cur_srcs = set(current_metrics.get("new_srcs", []))
    prev_srcs = set(prev_metrics.get("new_srcs", []))
    overlap_srcs = sorted(cur_srcs & prev_srcs)

    report = {
        "generated_at": datetime.now().isoformat(),
        "current": current_metrics,
        "previous": prev_metrics,
        "diff": {
            "new_srcs_count": current_metrics["new_srcs_count"] - prev_metrics["new_srcs_count"],
            "download_count": current_metrics["download_count"] - prev_metrics["download_count"],
            "unique_hash_count": current_metrics["unique_hash_count"] - prev_metrics["unique_hash_count"],
            "dup_hash_count": current_metrics["dup_hash_count"] - prev_metrics["dup_hash_count"],
            "scroll_steps": current_metrics["scroll_steps"] - prev_metrics["scroll_steps"],
            "scroll_mounted_peak": current_metrics["scroll_mounted_peak"] - prev_metrics["scroll_mounted_peak"],
            "scroll_unique_peak": current_metrics["scroll_unique_peak"] - prev_metrics["scroll_unique_peak"],
            "overlap_new_srcs_count": len(overlap_srcs),
            "overlap_new_srcs_sample": overlap_srcs[:30],
        },
    }
    return prev_dir, report


def render_compare_text_report(report: dict, prev_dir: str) -> str:
    """Render báo cáo compare session dạng text dễ đọc."""
    cur = report.get("current", {}) or {}
    prv = report.get("previous", {}) or {}
    d = report.get("diff", {}) or {}

    lines = [
        "=== SESSION COMPARE REPORT ===",
        f"generated_at={report.get('generated_at', '')}",
        "",
        f"Current : {cur.get('session_name', '')}",
        f"Previous: {os.path.basename(prev_dir)}",
        "",
        "[COUNTS]",
        f"new_srcs      : {prv.get('new_srcs_count', 0)} -> {cur.get('new_srcs_count', 0)} (diff {d.get('new_srcs_count', 0):+d})",
        f"downloads     : {prv.get('download_count', 0)} -> {cur.get('download_count', 0)} (diff {d.get('download_count', 0):+d})",
        f"unique_hashes : {prv.get('unique_hash_count', 0)} -> {cur.get('unique_hash_count', 0)} (diff {d.get('unique_hash_count', 0):+d})",
        f"dup_hashes    : {prv.get('dup_hash_count', 0)} -> {cur.get('dup_hash_count', 0)} (diff {d.get('dup_hash_count', 0):+d})",
        "",
        "[SCROLL]",
        f"steps         : {prv.get('scroll_steps', 0)} -> {cur.get('scroll_steps', 0)} (diff {d.get('scroll_steps', 0):+d})",
        f"mounted_peak  : {prv.get('scroll_mounted_peak', 0)} -> {cur.get('scroll_mounted_peak', 0)} (diff {d.get('scroll_mounted_peak', 0):+d})",
        f"unique_peak   : {prv.get('scroll_unique_peak', 0)} -> {cur.get('scroll_unique_peak', 0)} (diff {d.get('scroll_unique_peak', 0):+d})",
        "",
        "[OVERLAP new_srcs]",
        f"count={d.get('overlap_new_srcs_count', 0)}",
    ]

    sample = d.get("overlap_new_srcs_sample", []) or []
    for i, src in enumerate(sample, start=1):
        lines.append(f"{i:02d}. {src}")

    return "\n".join(lines) + "\n"
