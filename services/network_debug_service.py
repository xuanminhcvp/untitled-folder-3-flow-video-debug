"""
Service đăng ký listener network cho luồng Dreamina/Google Flow.

Thiết kế:
- Giữ nguyên logic parse request/response như bản cũ.
- Nhận state/helpers/config từ caller để tránh phụ thuộc biến global.
"""

import asyncio
import json
import time


def setup_image_network_debug(page, state: dict, helpers: dict, config: dict | None = None):
    """
    Bắt request/response/failure liên quan ảnh/video để biết:
    - request gửi đi URL nào, post_data nào
    - response trả về status gì, body có gì quan trọng
    - map scene -> task -> media/image để tải file ổn định hơn
    """
    config = config or {}

    # ── State containers (tham chiếu mutable để cập nhật trực tiếp) ──
    _api_req_meta = state["_api_req_meta"]
    _api_events = state["_api_events"]
    _scene_to_media_ids = state["_scene_to_media_ids"]
    _scene_to_video_media_ids = state["_scene_to_video_media_ids"]
    _scene_to_video_ready_media_ids = state["_scene_to_video_ready_media_ids"]
    _scene_to_video_failed_media_ids = state["_scene_to_video_failed_media_ids"]
    _video_media_status_by_id = state["_video_media_status_by_id"]
    _video_download_events = state["_video_download_events"]
    _flow_ui_error_events = state["_flow_ui_error_events"]
    _upscale_success_by_media = state["_upscale_success_by_media"]
    _upscale_events = state["_upscale_events"]
    _last_flow_client_context = state["_last_flow_client_context"]
    _submit_to_scene = state["_submit_to_scene"]
    _trusted_submit_ids = state["_trusted_submit_ids"]
    _scene_to_image_urls = state["_scene_to_image_urls"]
    _scene_to_task_ids = state["_scene_to_task_ids"]
    _task_to_image_urls = state["_task_to_image_urls"]
    _network_events = state["_network_events"]
    _network_req_start = state["_network_req_start"]
    _pending_api_tasks = state["_pending_api_tasks"]
    _video_media_state = state["_video_media_state"]

    # ── Helper functions từ caller ──
    _looks_like_api_url = helpers["_looks_like_api_url"]
    _is_upscale_api_url = helpers["_is_upscale_api_url"]
    _is_flow_video_generate_api_url = helpers["_is_flow_video_generate_api_url"]
    _looks_like_flow_video_api_url = helpers["_looks_like_flow_video_api_url"]
    _collect_task_ids_from_obj = helpers["_collect_task_ids_from_obj"]
    _collect_urls_from_obj = helpers["_collect_urls_from_obj"]
    _collect_video_urls_from_obj = helpers["_collect_video_urls_from_obj"]
    _collect_error_messages_from_obj = helpers["_collect_error_messages_from_obj"]
    _collect_video_media_items_from_obj = helpers["_collect_video_media_items_from_obj"]
    _extract_scene_number_from_any_text = helpers["_extract_scene_number_from_any_text"]
    _append_unique_dict_list = helpers["_append_unique_dict_list"]
    _normalize_media_status_text = helpers["_normalize_media_status_text"]
    _extract_video_media_status = helpers["_extract_video_media_status"]
    _register_scene_video_media = helpers["_register_scene_video_media"]
    _register_orphan_video_media = helpers["_register_orphan_video_media"]
    _extract_scene_cover_and_submit_map_from_history_json = helpers[
        "_extract_scene_cover_and_submit_map_from_history_json"
    ]
    _extract_media_id_from_upscale_post_data = helpers["_extract_media_id_from_upscale_post_data"]
    _extract_scene_numbers_from_text = helpers["_extract_scene_numbers_from_text"]
    _looks_like_flow_media_url = helpers["_looks_like_flow_media_url"]
    _register_video_media_from_redirect_request = helpers["_register_video_media_from_redirect_request"]
    _looks_like_image_url = helpers["_looks_like_image_url"]
    _safe_decode_bytes_preview = helpers["_safe_decode_bytes_preview"]
    log = helpers["log"]

    # ── Config runtime ──
    _run_started_ts = config.get("_run_started_ts", 0.0)
    api_history_max_age_sec = config.get("API_HISTORY_MAX_AGE_SEC", 86400)

    async def handle_api_response(response):
        """Xử lý response fetch/xhr để trích task_id + image urls."""
        request = response.request
        req_id = id(request)
        meta = _api_req_meta.get(req_id, {})
        url = request.url
        ts = time.time()

        if request.resource_type not in {"fetch", "xhr"}:
            return
        if not (_looks_like_api_url(url) or _is_upscale_api_url(url) or meta.get("scene_numbers")):
            return

        content_type = ""
        try:
            content_type = response.headers.get("content-type", "")
        except Exception:
            pass

        body_text = ""
        body_sample = ""
        body_json = None
        try:
            # Chỉ đọc body cho API text/json để tránh nặng.
            if "json" in (content_type or "").lower() or "text" in (content_type or "").lower():
                body_text = await response.text()
                body_sample = body_text[:30000] + ("...<truncated>" if len(body_text) > 30000 else "")
                try:
                    body_json = json.loads(body_text)
                except Exception:
                    body_json = None
        except Exception:
            body_text = ""
            body_sample = ""
            body_json = None

        task_ids = []
        image_urls = []
        video_urls = []
        video_media_updates = []
        backend_error_messages = []
        parser_hits = []
        if body_json is not None:
            _collect_task_ids_from_obj(body_json, task_ids)
            _collect_urls_from_obj(body_json, image_urls)
            _collect_video_urls_from_obj(body_json, video_urls)
            _collect_error_messages_from_obj(body_json, backend_error_messages)

            # Parse riêng cho Flow generate: lấy mediaId để upscale 2K sau đó.
            if "flowmedia:batchgenerateimages" in (url or "").lower():
                parser_hits.append("flowmedia:batchgenerateimages")
                try:
                    media = (body_json or {}).get("media", []) or []
                    scenes_local = meta.get("scene_numbers", []) or []
                    for idx, item in enumerate(media):
                        if not isinstance(item, dict):
                            continue
                        media_id = str(item.get("name", "") or "")
                        if not media_id:
                            continue

                        scene_no = 0
                        # Ưu tiên map từ request scene_numbers.
                        if len(scenes_local) == 1:
                            scene_no = scenes_local[0]
                        elif idx < len(scenes_local):
                            scene_no = scenes_local[idx]

                        # Fallback: parse scene từ prompt trong response.
                        if not scene_no:
                            prompt_text = str(
                                (((item.get("image", {}) or {}).get("generatedImage", {}) or {}).get("prompt", ""))
                                or ""
                            )
                            scene_no = _extract_scene_number_from_any_text(prompt_text, 0)

                        if scene_no:
                            _append_unique_dict_list(_scene_to_media_ids, scene_no, [media_id])
                except Exception:
                    pass

            # Parse riêng cho Flow generate VIDEO: scene -> video mediaId.
            if _is_flow_video_generate_api_url(url):
                parser_hits.append("flow_video_generate")
                try:
                    media = (body_json or {}).get("media", []) or []
                    # Format mới có thể không để media ở root.
                    if not media:
                        media = []
                        _collect_video_media_items_from_obj(body_json, media)
                        dedup_media = []
                        seen_mid = set()
                        for it in media:
                            if not isinstance(it, dict):
                                continue
                            mid = str(it.get("name", "") or "")
                            if not mid or mid in seen_mid:
                                continue
                            seen_mid.add(mid)
                            dedup_media.append(it)
                        media = dedup_media
                    scenes_local = meta.get("scene_numbers", []) or []
                    operations = (body_json or {}).get("operations", []) or []
                    default_status = ""
                    # Chỉ lấy default_status nếu response thật sự chỉ có 1 media và 1 operation đi chung
                    if len(operations) == 1 and len(media) == 1 and isinstance(operations[0], dict):
                        default_status = _normalize_media_status_text(operations[0].get("status", ""))
                    for idx, item in enumerate(media):
                        if not isinstance(item, dict):
                            continue
                        media_id = str(item.get("name", "") or "")
                        if not media_id:
                            continue

                        scene_no = 0
                        if len(scenes_local) == 1:
                            scene_no = scenes_local[0]
                        elif idx < len(scenes_local):
                            scene_no = scenes_local[idx]

                        if not scene_no:
                            video_obj = item.get("video", {}) or {}
                            generated_video = video_obj.get("generatedVideo", {}) or {}
                            prompt_text = str(generated_video.get("prompt", "") or "")
                            if not prompt_text:
                                media_meta = (item.get("mediaMetadata", {}) or {})
                                prompt_text = str(media_meta.get("mediaTitle", "") or "")
                            scene_no = _extract_scene_number_from_any_text(prompt_text, 0)
                        if not scene_no:
                            # Thay vì gán mù, tìm xem ID này đã được map lúc PENDING (từ generate) hay chưa
                            for s_no, media_dict in _video_media_state.items():
                                if media_id in media_dict:
                                    scene_no = s_no
                                    break

                            # Nếu VẪN không tìm thấy scene, bỏ qua (đây có thể là ảnh reference từ STEP 1)
                            if not scene_no:
                                _register_orphan_video_media(media_id, status="")
                                continue

                        if scene_no:
                            status = _extract_video_media_status(item, default_status=default_status)
                            _register_scene_video_media(scene_no, media_id, status)
                            video_media_updates.append({
                                "scene": scene_no,
                                "media_id": media_id,
                                "status": status,
                            })
                except Exception:
                    pass

            # Parse projectInitialData để cập nhật scene -> video mediaId khi job cập nhật trạng thái.
            # FIX: Video media nằm ở CẢ 2 vị trí:
            #   1. data_json.media[]  (root level — format cũ)
            #   2. data_json.projectContents.media[]  (format mới của Flow — chứa video thực tế)
            #   3. data_json.projectContents.workflows[].metadata.primaryMediaId  (mapping workflow -> media)
            if "flow.projectinitialdata" in (url or "").lower():
                parser_hits.append("flow.projectinitialdata")
                try:
                    data_json = ((((body_json or {}).get("result", {}) or {}).get("data", {}) or {}).get("json", {}) or {})
                    # Gom media từ cả root level VÀ projectContents.media[]
                    media_root = (data_json.get("media", []) or [])
                    project_contents = (data_json.get("projectContents", {}) or {})
                    media_pc = (project_contents.get("media", []) or [])
                    # Gộp cả 2 nguồn, dedupe theo name (mediaId)
                    seen_ids = set()
                    all_media = []
                    for item in media_pc + media_root:
                        if not isinstance(item, dict):
                            continue
                        mid = str(item.get("name", "") or "")
                        if mid and mid not in seen_ids:
                            seen_ids.add(mid)
                            all_media.append(item)
                    for item in all_media:
                        media_id = str(item.get("name", "") or "")
                        if not media_id:
                            continue
                        video_obj = item.get("video", {}) or {}
                        generated_video = video_obj.get("generatedVideo", {}) or {}
                        prompt_text = str(generated_video.get("prompt", "") or "")
                        if not prompt_text:
                            prompt_text = str(((item.get("mediaMetadata", {}) or {}).get("mediaTitle", "")) or "")
                        scene_no = _extract_scene_number_from_any_text(prompt_text, 0)
                        if scene_no:
                            status = _extract_video_media_status(item, default_status="")
                            _register_scene_video_media(scene_no, media_id, status)
                            video_media_updates.append({
                                "scene": scene_no,
                                "media_id": media_id,
                                "status": status,
                            })
                    # Parse thêm workflows[].metadata.primaryMediaId để backup mapping
                    workflows = (project_contents.get("workflows", []) or [])
                    for wf in workflows:
                        if not isinstance(wf, dict):
                            continue
                        wf_meta = (wf.get("metadata", {}) or {})
                        primary_mid = str(wf_meta.get("primaryMediaId", "") or "")
                        display_name = str(wf_meta.get("displayName", "") or "")
                        if primary_mid and display_name:
                            scene_no = _extract_scene_number_from_any_text(display_name, 0)
                            if scene_no:
                                # Workflow thường là nguồn map scene chính xác nhất.
                                _register_scene_video_media(scene_no, primary_mid, "")
                                video_media_updates.append({
                                    "scene": scene_no,
                                    "media_id": primary_mid,
                                    "status": "",
                                })
                except Exception:
                    pass

            # Parse chuyên biệt cho get_history_by_ids (scene -> cover_url)
            if "/get_history_by_ids" in url:
                parser_hits.append("get_history_by_ids")
                scene_cover_map, submit_scene_map = _extract_scene_cover_and_submit_map_from_history_json(
                    body_json,
                    trusted_submit_ids=_trusted_submit_ids,
                    run_started_ts=_run_started_ts,
                    max_age_sec=api_history_max_age_sec,
                )
                for sc, urls in scene_cover_map.items():
                    _append_unique_dict_list(_scene_to_image_urls, sc, urls)
                for submit_id, sc in submit_scene_map.items():
                    _submit_to_scene[str(submit_id)] = sc

            # Parse generate response để map submit_id -> scene nhanh hơn
            if "/aigc_draft/generate" in url:
                parser_hits.append("aigc_draft/generate")
                try:
                    aigc = ((body_json or {}).get("data") or {}).get("aigc_data") or {}
                    task_obj = aigc.get("task", {}) or {}
                    submit_id = str(task_obj.get("submit_id", "") or "")
                    history_record_id = str(aigc.get("history_record_id", "") or "")
                    scenes_local = meta.get("scene_numbers", []) or []
                    if scenes_local:
                        sc = scenes_local[0]
                        if submit_id:
                            _trusted_submit_ids.add(submit_id)
                            _submit_to_scene[submit_id] = sc
                        if history_record_id:
                            _append_unique_dict_list(_scene_to_task_ids, sc, [history_record_id])
                except Exception:
                    pass

        # dedupe
        task_ids = list(dict.fromkeys(task_ids))
        image_urls = list(dict.fromkeys(image_urls))

        is_upscale = _is_upscale_api_url(url)
        request_post_data = (meta.get("post_data", "") or "")
        upscale_media_id = _extract_media_id_from_upscale_post_data(request_post_data) if is_upscale else ""
        encoded_image = ""
        if is_upscale and isinstance(body_json, dict):
            encoded_image = str((body_json or {}).get("encodedImage", "") or "")
        _api_events.append({
            "type": "api_response",
            "ts": ts,
            "url": url,
            "status": response.status,
            "resource_type": request.resource_type,
            "is_upscale": is_upscale,
            "upscale_media_id": upscale_media_id,
            "scene_numbers": meta.get("scene_numbers", []),
            "task_ids": task_ids,
            "image_urls_count": len(image_urls),
            "image_urls_sample": image_urls[:12],
            "video_urls_count": len(video_urls),
            "video_urls_sample": video_urls[:12],
            "video_media_updates_count": len(video_media_updates),
            "video_media_updates_sample": video_media_updates[:10],
            "backend_error_messages": list(dict.fromkeys(backend_error_messages))[:10],
            "parser_hits": parser_hits,
            "request_post_data_sample": request_post_data[:3000],
            "response_body_sample": body_sample[:3000] if body_sample else "",
        })

        # DEBUG mapping: in log endpoint thật + parser có hit hay không.
        # Mục tiêu: nếu UI có video mà không tải được, nhìn log sẽ biết parser miss ở đâu.
        if _looks_like_flow_video_api_url(url) or video_media_updates:
            endpoint = (url or "").split("/trpc/", 1)[-1] if "/trpc/" in (url or "") else (url or "")
            if len(endpoint) > 120:
                endpoint = endpoint[:120] + "...(cut)"
            status_sample = []
            for row in video_media_updates[:3]:
                st = str(row.get("status", "") or "")
                if st:
                    status_sample.append(st)
            err_sample = (list(dict.fromkeys(backend_error_messages))[:2] if backend_error_messages else [])
            log(
                "[FLOW-NET] "
                f"status={response.status} "
                f"endpoint={endpoint} "
                f"scene={meta.get('scene_numbers', [])} "
                f"video_updates={len(video_media_updates)} "
                f"video_urls={len(video_urls)} "
                f"status_sample={status_sample} "
                f"errors={err_sample} "
                f"parser_hits={parser_hits}",
                "DBG",
            )

        # Nếu response upscale có encodedImage thì giữ lại trong RAM để lưu file 2K theo scene.
        if is_upscale and upscale_media_id and encoded_image:
            _upscale_success_by_media[upscale_media_id] = {
                "encoded_image": encoded_image,
                "status": int(response.status),
                "url": url,
                "ts": ts,
                "media_id": upscale_media_id,
                "size_base64": len(encoded_image),
            }

        # Log riêng cho upscale để phân tích logic 2K.
        if is_upscale:
            _upscale_events.append({
                "type": "upscale_response",
                "ts": ts,
                "url": url,
                "status": response.status,
                "resource_type": request.resource_type,
                "media_id": upscale_media_id,
                "request_post_data_sample": request_post_data[:20000],
                "response_body_sample": body_sample[:20000] if body_sample else "",
                "has_encoded_image": bool(encoded_image),
                "encoded_image_size": len(encoded_image) if encoded_image else 0,
                "task_ids": task_ids,
                "image_urls_sample": image_urls[:20],
            })

        # Map scene -> task
        scenes = meta.get("scene_numbers", []) or []
        if scenes and task_ids:
            # Nếu 1 scene thì map scene đó với tất cả task_id thấy được.
            if len(scenes) == 1:
                _append_unique_dict_list(_scene_to_task_ids, scenes[0], task_ids)
            else:
                # nhiều scene: map theo vị trí tối thiểu
                for i, sc in enumerate(scenes):
                    if i < len(task_ids):
                        _append_unique_dict_list(_scene_to_task_ids, sc, [task_ids[i]])

        # Map task -> urls
        if task_ids and image_urls:
            for tid in task_ids:
                _append_unique_dict_list(_task_to_image_urls, tid, image_urls)

        # Một số API trả thẳng ảnh theo prompt/request scene mà không có task_id
        if scenes and image_urls and not task_ids:
            for sc in scenes:
                _append_unique_dict_list(_scene_to_image_urls, sc, image_urls)

        # Với get_history_by_ids: map submit_id (key data) -> scene đã biết -> cover/image urls
        if "/get_history_by_ids" in url and body_json is not None:
            try:
                data = (body_json or {}).get("data") or {}
                if isinstance(data, dict):
                    for submit_id, rec in data.items():
                        sc = _submit_to_scene.get(str(submit_id))
                        if not sc:
                            continue
                        if isinstance(rec, dict):
                            items = rec.get("item_list", []) or []
                            for it in items:
                                if not isinstance(it, dict):
                                    continue
                                common = it.get("common_attr", {}) or {}
                                cover = str(common.get("cover_url", "") or "")
                                if cover:
                                    _append_unique_dict_list(_scene_to_image_urls, sc, [cover])
                                cover_map = common.get("cover_url_map", {}) or {}
                                if isinstance(cover_map, dict):
                                    vals = [v for v in cover_map.values() if isinstance(v, str) and v.startswith("http")]
                                    if vals:
                                        _append_unique_dict_list(_scene_to_image_urls, sc, vals)
            except Exception:
                pass

    async def handle_media_playback_response(response):
        """
        Bắt chi tiết response media của Flow player:
        - status
        - content-type
        - body preview (nếu nhỏ/text)
        Mục tiêu: truy vết lỗi 'tải nội dung nghe nhìn'.
        """
        request = response.request
        url = request.url
        if not _looks_like_flow_media_url(url):
            return
        ts = time.time()
        content_type = ""
        content_length = ""
        try:
            content_type = response.headers.get("content-type", "")
            content_length = response.headers.get("content-length", "")
        except Exception:
            pass

        body_size = 0
        body_preview = ""
        should_read_body = False
        low_ct = str(content_type or "").lower()
        if response.status >= 400:
            should_read_body = True
        if "text" in low_ct or "json" in low_ct or "xml" in low_ct or "html" in low_ct:
            should_read_body = True
        if not content_length:
            should_read_body = True
        else:
            try:
                if int(content_length) <= 4096:
                    should_read_body = True
            except Exception:
                pass

        if should_read_body:
            try:
                body = await response.body()
                body_size = len(body)
                body_preview = _safe_decode_bytes_preview(body, max_len=280)
            except Exception:
                body_size = 0
                body_preview = ""

        _network_events.append({
            "type": "media_response",
            "ts": ts,
            "status": response.status,
            "resource_type": request.resource_type,
            "url": url,
            "content_type": content_type,
            "content_length": content_length,
            "body_size": body_size,
            "body_preview": body_preview,
        })

    def on_request(request):
        req_id = id(request)
        ts = time.time()
        _network_req_start[req_id] = ts
        # Bắt media_id từ redirect request để map scene dù generate response thiếu scene/media.
        _register_video_media_from_redirect_request(request.url, ts)
        is_image_or_media = (
            request.resource_type == "image"
            or _looks_like_image_url(request.url)
            or _looks_like_flow_media_url(request.url)
            or request.resource_type == "media"
        )
        if is_image_or_media:
            _network_events.append({
                "type": "request",
                "ts": ts,
                "method": request.method,
                "resource_type": request.resource_type,
                "url": request.url,
            })
        # Log request API để map prompt -> task
        if request.resource_type in {"fetch", "xhr"} and (_looks_like_api_url(request.url) or _is_upscale_api_url(request.url)):
            post_data = ""
            try:
                post_data = request.post_data or ""
            except Exception:
                post_data = ""
            scenes = _extract_scene_numbers_from_text(post_data)
            media_id = _extract_media_id_from_upscale_post_data(post_data) if _is_upscale_api_url(request.url) else ""
            _api_req_meta[req_id] = {
                "ts": ts,
                "url": request.url,
                "method": request.method,
                "post_data": post_data,
                "scene_numbers": scenes,
                "media_id": media_id,
            }
            _api_events.append({
                "type": "api_request",
                "ts": ts,
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "is_upscale": _is_upscale_api_url(request.url),
                "upscale_media_id": media_id,
                "scene_numbers": scenes,
                "post_data_sample": post_data[:3000],
            })

            # Lưu clientContext gần nhất từ request generate của Flow để gọi upsample API.
            if "flowmedia:batchgenerateimages" in (request.url or "").lower() and post_data:
                try:
                    body_json = json.loads(post_data)
                    cc = (body_json or {}).get("clientContext", {}) or {}
                    if isinstance(cc, dict) and cc:
                        _last_flow_client_context.clear()
                        _last_flow_client_context.update(cc)
                except Exception:
                    pass

            if _is_upscale_api_url(request.url):
                _upscale_events.append({
                    "type": "upscale_request",
                    "ts": ts,
                    "url": request.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "media_id": media_id,
                    "request_post_data_sample": post_data[:20000],
                })

    def on_response(response):
        request = response.request
        req_id = id(request)
        ts = time.time()
        started = _network_req_start.get(req_id, ts)
        elapsed_ms = int((ts - started) * 1000)
        content_type = ""
        content_length = ""
        try:
            content_type = response.headers.get("content-type", "")
            content_length = response.headers.get("content-length", "")
        except Exception:
            pass

        is_image_or_media = (
            request.resource_type == "image"
            or _looks_like_image_url(request.url)
            or _looks_like_flow_media_url(request.url)
            or request.resource_type == "media"
            or "image" in (content_type or "").lower()
            or "video" in (content_type or "").lower()
            or "audio" in (content_type or "").lower()
        )
        if is_image_or_media:
            _network_events.append({
                "type": "response",
                "ts": ts,
                "status": response.status,
                "resource_type": request.resource_type,
                "url": request.url,
                "content_type": content_type,
                "content_length": content_length,
                "elapsed_ms": elapsed_ms,
            })
        # API response xử lý async để có body/json
        if request.resource_type in {"fetch", "xhr"}:
            try:
                t = asyncio.create_task(handle_api_response(response))
                _pending_api_tasks.append(t)
            except Exception:
                pass
        # Media response xử lý async để đọc body nhỏ/lỗi.
        if _looks_like_flow_media_url(request.url) or request.resource_type == "media":
            try:
                t2 = asyncio.create_task(handle_media_playback_response(response))
                _pending_api_tasks.append(t2)
            except Exception:
                pass

    def on_request_failed(request):
        ts = time.time()
        if (
            request.resource_type == "image"
            or _looks_like_image_url(request.url)
            or _looks_like_flow_media_url(request.url)
            or request.resource_type == "media"
        ):
            failure = ""
            try:
                failure = request.failure or ""
            except Exception:
                pass
            _network_events.append({
                "type": "request_failed",
                "ts": ts,
                "resource_type": request.resource_type,
                "url": request.url,
                "failure": str(failure),
            })

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)
