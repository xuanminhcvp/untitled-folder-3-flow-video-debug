"""
Service tách riêng toàn bộ logic attach/upload/search reference trên Flow.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
from typing import Awaitable, Callable


async def verify_reference_image_attached(page, image_path: str) -> dict:
    """
    Xác minh ở mức UI rằng file ảnh tham chiếu đã được gắn vào form.
    """
    filename = os.path.basename(image_path or "").strip()
    filename_low = filename.lower()
    stem_low = os.path.splitext(filename_low)[0]
    result = {
        "verified": False,
        "filename": filename,
        "matched_input_indexes": [],
        "dom_has_filename": False,
        "dom_has_stem": False,
        "attachment_hint_count": 0,
    }
    if not filename:
        return result

    try:
        ui = await page.evaluate(
            """
            ({ filename, stem }) => {
                const low = (v) => String(v || '').toLowerCase();
                const out = {
                    matched_input_indexes: [],
                    dom_has_filename: false,
                    dom_has_stem: false,
                    attachment_hint_count: 0,
                };
                const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                for (let i = 0; i < inputs.length; i++) {
                    const el = inputs[i];
                    const val = low(el.value || '');
                    const files = Array.from(el.files || []).map(f => low(f.name));
                    if (val.includes(filename) || files.some(n => n === filename)) {
                        out.matched_input_indexes.push(i);
                    }
                }
                const text = low(document.body ? document.body.innerText : '');
                out.dom_has_filename = text.includes(filename);
                out.dom_has_stem = stem && text.includes(stem);
                const hintSelectors = [
                    '[class*="attach"]',
                    '[class*="reference"]',
                    '[data-testid*="attach"]',
                    '[data-testid*="reference"]',
                    '[aria-label*="attach"]',
                    '[aria-label*="reference"]',
                    '[aria-label*="image"]',
                ];
                out.attachment_hint_count = hintSelectors
                    .map((s) => document.querySelectorAll(s).length)
                    .reduce((acc, n) => acc + n, 0);
                return out;
            }
            """,
            {"filename": filename_low, "stem": stem_low},
        )
        if isinstance(ui, dict):
            result.update(ui)
    except Exception:
        pass

    has_input = len(result.get("matched_input_indexes", [])) > 0
    has_dom = bool(result.get("dom_has_filename")) or (
        bool(result.get("dom_has_stem")) and int(result.get("attachment_hint_count", 0)) > 0
    )
    result["verified"] = bool(has_input or has_dom)
    return result


async def recover_flow_editor_if_in_scene_page(
    page,
    google_flow_home: str,
    extract_project_id_cb: Callable[[str], str],
    ensure_editor_cb: Callable[..., Awaitable[bool]],
    log_cb: Callable[[str, str], None],
) -> bool:
    """
    Nếu bị đẩy sang URL `/scene/...` thì quay về project editor.
    """
    cur = str(page.url or "")
    if "/scene/" not in cur:
        return True
    project_id = extract_project_id_cb(cur)
    target = f"{google_flow_home}/project/{project_id}" if project_id else google_flow_home
    log_cb(f"  Phát hiện đang ở scene page, quay lại editor: {target}", "WARN")
    try:
        await page.goto(target, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1.0)
        return await ensure_editor_cb(page, timeout_sec=12)
    except Exception:
        return False


async def _safe_inner_text(el) -> str:
    try:
        t = await el.inner_text()
        return (t or "").strip()
    except Exception:
        return ""


async def _safe_attr(el, name: str) -> str:
    try:
        v = await el.get_attribute(name)
        return (v or "").strip()
    except Exception:
        return ""


async def _click_force(page, el) -> bool:
    for mode in ("normal", "force", "js"):
        try:
            if mode == "normal":
                await el.click(timeout=1200)
            elif mode == "force":
                await el.click(force=True, timeout=1200)
            else:
                await page.evaluate("(node) => node.click()", el)
            await asyncio.sleep(0.2)
            return True
        except Exception:
            continue
    return False


async def _accept_first_time_upload_consent(page, log_cb: Callable[[str, str], None] | None = None) -> bool:
    """
    Flow có thể hiện popup điều khoản lần đầu upload reference.
    Nếu không bấm "Tôi đồng ý / I agree / Đồng ý" thì mọi thao tác upload sẽ fail.
    Hàm này xử lý popup đó theo best-effort và trả về True nếu đã bấm được.
    """
    _log = log_cb or (lambda *_args, **_kwargs: None)
    # Pattern text của nút chấp thuận điều khoản.
    # Mở rộng nhiều biến thể vì popup có thể đổi ngôn ngữ/account.
    patterns = [
        re.compile(r"t[ôo]i\s+đ[ồo]ng\s+[ýy]", re.IGNORECASE),
        re.compile(r"\bđ[ồo]ng\s+[ýy]\b", re.IGNORECASE),
        re.compile(r"\bt[ôo]i\s+ch[ấa]p\s+nh[ậa]n\b", re.IGNORECASE),
        re.compile(r"\bch[ấa]p\s+nh[ậa]n\b", re.IGNORECASE),
        re.compile(r"\bti[eê]p\s+t[uụ]c\b", re.IGNORECASE),
        re.compile(r"\bi\s*agree\b", re.IGNORECASE),
        re.compile(r"\bagree\b", re.IGNORECASE),
        re.compile(r"\bi\s+accept\b", re.IGNORECASE),
        re.compile(r"\baccept\b", re.IGNORECASE),
        re.compile(r"\bok\b", re.IGNORECASE),
    ]
    selectors = ["button", "[role='button']", "div[role='button']", "span[role='button']"]

    async def _scan_click(scope, scope_name: str) -> bool:
        for sel in selectors:
            try:
                nodes = scope.locator(sel)
                count = await nodes.count()
                for i in range(min(count, 260)):
                    b = nodes.nth(i)
                    try:
                        if not await b.is_visible(timeout=80):
                            continue
                        text = (await _safe_inner_text(b)).strip()
                        aria = (await _safe_attr(b, "aria-label")).strip()
                        title = (await _safe_attr(b, "title")).strip()
                        mix = f"{text} {aria} {title}".strip()
                        if not mix:
                            continue
                        if not any(p.search(mix) for p in patterns):
                            continue
                        if await _click_force(page, b):
                            _log(
                                f"  Đã bấm nút chấp thuận popup upload ({scope_name}): '{mix[:80]}'",
                                "OK",
                            )
                            await asyncio.sleep(0.45)
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    # Ưu tiên quét trong dialog/modal trước để tránh bấm nhầm nút khác trên trang.
    dialog_scopes = [
        ("[role='dialog']", "role=dialog"),
        ("[aria-modal='true']", "aria-modal"),
        ("div[class*='modal']", "class*=modal"),
        ("div[class*='dialog']", "class*=dialog"),
        ("div[class*='popup']", "class*=popup"),
    ]
    for sel, name in dialog_scopes:
        try:
            scope = page.locator(sel)
            if await scope.count() <= 0:
                continue
            visible_count = 0
            for i in range(min(await scope.count(), 8)):
                try:
                    box = scope.nth(i)
                    if await box.is_visible(timeout=80):
                        visible_count += 1
                        if await _scan_click(box, name):
                            return True
                except Exception:
                    continue
            if visible_count > 0:
                _log(f"  Phát hiện popup khả nghi ({name}) nhưng chưa tìm thấy nút đồng ý phù hợp.", "DBG")
        except Exception:
            continue

    # Fallback: quét toàn trang.
    if await _scan_click(page, "page"):
        return True
    return False


async def _find_reference_plus_button(page, vp_height: int):
    low_screen_btns = []
    try:
        btns = page.locator("button")
        count = await btns.count()
        vh = (page.viewport_size or {}).get("height", vp_height)
        for i in range(min(count, 220)):
            b = btns.nth(i)
            try:
                if not await b.is_visible(timeout=80):
                    continue
                box = await b.bounding_box()
                if not box:
                    continue
                if box["y"] < vh * 0.60:
                    continue
                text = (await _safe_inner_text(b)).lower()
                aria = (await _safe_attr(b, "aria-label")).lower()
                if "add" in text or "add_2" in text or "thêm" in aria or "add" in aria:
                    return b
                low_screen_btns.append((box["y"], box["x"], b))
            except Exception:
                continue
    except Exception:
        pass
    if low_screen_btns:
        low_screen_btns.sort(key=lambda t: (t[0], -t[1]), reverse=True)
        return low_screen_btns[0][2]
    return None


async def _find_reference_search_box(page):
    selectors = [
        "input[placeholder*='Tìm kiếm' i]",
        "input[placeholder*='tìm' i]",
        "input[placeholder*='search' i]",
        "input[type='search']",
        "input[class*='gemGik']",
        "input[class*='sc-68b42f2']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            for i in range(min(count, 80)):
                c = loc.nth(i)
                try:
                    if not await c.is_visible(timeout=100):
                        continue
                    if not await c.is_enabled():
                        continue
                    box = await c.bounding_box()
                    if not box or box["width"] < 80 or box["height"] < 20:
                        continue
                    return c
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _normalize_stem(text: str) -> str:
    t = (text or "").strip().lower()
    for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov"]:
        if t.endswith(ext):
            t = t[:-len(ext)]
            break
    return t


async def _find_reference_dropdown_result(page, search_box, search_name: str):
    rect = await search_box.bounding_box()
    if not rect:
        return None
    min_y = rect["y"] + rect["height"] - 4
    max_y = rect["y"] + 620
    target = _normalize_stem(search_name)
    try:
        nodes = await page.query_selector_all("div, span, li, [role='option'], [role='listitem']")
        for el in nodes:
            try:
                box = await el.bounding_box()
                if not box:
                    continue
                if box["y"] < min_y or box["y"] > max_y:
                    continue
                if box["height"] < 8 or box["height"] > 90:
                    continue
                raw = await _safe_inner_text(el)
                if not raw:
                    raw = await _safe_attr(el, "aria-label")
                if not raw:
                    continue
                if _normalize_stem(raw) == target:
                    return el
            except Exception:
                continue
    except Exception:
        pass
    return None


async def _find_reference_result_by_text_locator(page, search_name: str):
    name = (search_name or "").strip()
    if not name:
        return None
    patterns = [
        re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE),
        re.compile(rf"\b{re.escape(name)}\.png\b", re.IGNORECASE),
        re.compile(rf"\b{re.escape(name)}\.(jpg|jpeg|webp)\b", re.IGNORECASE),
    ]
    selectors = ["div", "span", "li", "[role='option']", "[role='listitem']"]
    for sel in selectors:
        for patt in patterns:
            try:
                loc = page.locator(sel).filter(has_text=patt)
                cnt = await loc.count()
                for i in range(min(cnt, 20)):
                    c = loc.nth(i)
                    try:
                        if await c.is_visible(timeout=80):
                            return c
                    except Exception:
                        continue
            except Exception:
                continue
    return None


async def attach_reference_from_library_by_name(
    page,
    search_name: str,
    vp_height: int,
) -> tuple[bool, dict]:
    dbg = {
        "mode": "library_search",
        "search_name": search_name,
        "opened_panel": False,
        "typed_search": False,
        "clicked_result": False,
        "result_found": False,
        "url_after_attach": "",
    }
    name = (search_name or "").strip()
    if not name:
        dbg["error"] = "empty_search_name"
        return False, dbg

    plus_btn = await _find_reference_plus_button(page, vp_height)
    if not plus_btn:
        dbg["error"] = "plus_button_not_found"
        return False, dbg
    if not await _click_force(page, plus_btn):
        dbg["error"] = "plus_button_click_failed"
        return False, dbg
    dbg["opened_panel"] = True
    await asyncio.sleep(0.9)

    search_box = await _find_reference_search_box(page)
    if not search_box:
        dbg["error"] = "search_box_not_found"
        return False, dbg

    queries = [name, f"{name}.png"]
    result_el = None
    try:
        await search_box.click()
    except Exception:
        pass
    for q in queries:
        try:
            try:
                await search_box.fill("")
            except Exception:
                await page.keyboard.press("Meta+a" if platform.system() == "Darwin" else "Control+a")
                await page.keyboard.press("Backspace")
            await asyncio.sleep(0.08)
            await search_box.fill(q)
            dbg["typed_search"] = True
            await asyncio.sleep(1.0)
            result_el = await _find_reference_dropdown_result(page, search_box, name)
            if result_el:
                break
        except Exception:
            continue
    if not result_el:
        result_el = await _find_reference_result_by_text_locator(page, name)
    if not result_el:
        dbg["error"] = "search_result_not_found"
        return False, dbg
    dbg["result_found"] = True
    if not await _click_force(page, result_el):
        dbg["error"] = "result_click_failed"
        return False, dbg
    dbg["clicked_result"] = True
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await asyncio.sleep(0.5)
    dbg["url_after_attach"] = str(page.url or "")
    return True, dbg


async def preload_reference_library_images(
    page,
    image_paths: list[str],
    vp_height: int,
    preload_wait_sec: int,
    log_cb: Callable[[str, str], None],
) -> tuple[bool, dict]:
    dbg = {
        "mode": "library_preload",
        "requested_count": len(image_paths or []),
        "valid_count": 0,
        "upload_triggered": False,
        "waited_sec": 0,
        "url_after_preload": "",
        "error": "",
    }
    valid = [p for p in (image_paths or []) if p and os.path.exists(p)]
    dbg["valid_count"] = len(valid)
    if not valid:
        dbg["error"] = "no_valid_images"
        return False, dbg

    # Lần đầu vào account có thể phải bấm đồng ý điều khoản upload.
    # Nếu không xử lý trước, các bước click upload phía dưới sẽ fail ngầm.
    await _accept_first_time_upload_consent(page, log_cb)

    plus_btn = await _find_reference_plus_button(page, vp_height)
    if not plus_btn:
        dbg["error"] = "plus_button_not_found"
        return False, dbg
    if not await _click_force(page, plus_btn):
        dbg["error"] = "plus_button_click_failed"
        return False, dbg
    await asyncio.sleep(0.6)
    # Một số account chỉ hiện popup điều khoản sau khi bấm dấu "+".
    # Vì vậy xử lý thêm 1 lần nữa ở đây để tránh click upload bị block.
    await _accept_first_time_upload_consent(page, log_cb)

    upload_selectors = [
        "button:has-text('Tải hình ảnh lên')",
        "button:has-text('Upload image')",
        "[role='button']:has-text('Tải hình ảnh lên')",
        "[role='button']:has-text('Upload image')",
        "label:has-text('Tải hình ảnh lên')",
        "label:has-text('Upload image')",
    ]
    for sel in upload_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() <= 0 or not await el.is_visible(timeout=120):
                continue
            async with page.expect_file_chooser(timeout=2000) as chooser_info:
                await _click_force(page, el)
            chooser = await chooser_info.value
            await chooser.set_files(valid)
            dbg["upload_triggered"] = True
            await asyncio.sleep(1.5)
            break
        except Exception:
            continue

    # Fallback retry: có trường hợp lần click đầu bị popup che, cần xử lý consent rồi thử lại.
    if not dbg["upload_triggered"]:
        await _accept_first_time_upload_consent(page, log_cb)
        for sel in upload_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() <= 0 or not await el.is_visible(timeout=120):
                    continue
                async with page.expect_file_chooser(timeout=2000) as chooser_info:
                    await _click_force(page, el)
                chooser = await chooser_info.value
                await chooser.set_files(valid)
                dbg["upload_triggered"] = True
                dbg["retry_after_consent"] = True
                await asyncio.sleep(1.5)
                break
            except Exception:
                continue

    if not dbg["upload_triggered"]:
        try:
            file_inputs = page.locator("input[type='file']")
            count = await file_inputs.count()
            for idx in range(min(count, 8)):
                try:
                    await file_inputs.nth(idx).set_input_files(valid)
                    dbg["upload_triggered"] = True
                    await asyncio.sleep(1.5)
                    break
                except Exception:
                    continue
        except Exception:
            pass

    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    wait_sec = max(0, int(preload_wait_sec))
    if wait_sec > 0:
        log_cb(f"  Chờ {wait_sec}s để Flow upload/index ảnh thư viện...", "WAIT")
        for remain in range(wait_sec, 0, -1):
            if remain % 5 == 0:
                log_cb(f"    preload còn {remain}s...", "WAIT")
            await asyncio.sleep(1)
    dbg["waited_sec"] = wait_sec
    dbg["url_after_preload"] = str(page.url or "")
    if not dbg["upload_triggered"]:
        dbg["error"] = "upload_trigger_not_found"
        return False, dbg
    return True, dbg


async def upload_reference_image_for_video(
    page,
    image_path: str,
    allow_direct_file_input: bool,
    verify_fn: Callable[[object, str], Awaitable[dict]],
    log_cb: Callable[[str, str], None],
) -> tuple[bool, dict]:
    debug_info = {
        "image_path": os.path.abspath(image_path) if image_path else "",
        "image_name": os.path.basename(image_path) if image_path else "",
        "attempts": [],
        "verified": False,
        "method": "",
        "verify": {},
    }
    if not image_path or not os.path.exists(image_path):
        log_cb(f"  Thiếu ảnh tham chiếu: {image_path}", "WARN")
        debug_info["error"] = "missing_file"
        return False, debug_info

    abs_path = os.path.abspath(image_path)

    # Lần đầu vào account có popup "Tôi đồng ý"; cần xử lý trước khi mở file chooser.
    await _accept_first_time_upload_consent(page, log_cb)

    chooser_patterns = [
        re.compile(r"reference", re.IGNORECASE),
        re.compile(r"upload", re.IGNORECASE),
        re.compile(r"tham chiếu", re.IGNORECASE),
        re.compile(r"thêm ảnh", re.IGNORECASE),
        re.compile(r"add reference", re.IGNORECASE),
        re.compile(r"upload reference", re.IGNORECASE),
        re.compile(r"upload image", re.IGNORECASE),
        re.compile(r"tải lên ảnh", re.IGNORECASE),
    ]
    clickable_selectors = ["button", "[role='button']", "label", "[aria-label]"]

    for selector in clickable_selectors:
        for pattern in chooser_patterns:
            try:
                target = page.locator(selector).filter(has_text=pattern).first
                if await target.count() <= 0:
                    continue
                if not await target.is_visible(timeout=400):
                    continue
                async with page.expect_file_chooser(timeout=2000) as chooser_info:
                    await target.click()
                chooser = await chooser_info.value
                await chooser.set_files(abs_path)
                await asyncio.sleep(1.2)
                verify = await verify_fn(page, abs_path)
                debug_info["attempts"].append({
                    "method": "file_chooser",
                    "selector": selector,
                    "pattern": pattern.pattern,
                    "verify": verify,
                    "url_after_set": str(page.url or ""),
                })
                if verify.get("verified"):
                    if "/scene/" in str(page.url or ""):
                        verify["verified"] = False
                        debug_info["attempts"][-1]["guard_fail"] = "moved_to_scene_page"
                        log_cb("  Upload file xong nhưng bị chuyển sang scene page, bỏ attempt này.", "WARN")
                        continue
                    debug_info["verified"] = True
                    debug_info["method"] = "file_chooser"
                    debug_info["verify"] = verify
                    log_cb(f"  Đã upload ảnh tham chiếu bằng file chooser: {os.path.basename(abs_path)}", "OK")
                    return True, debug_info
                log_cb("  File chooser đã set file nhưng chưa xác minh được trạng thái attach trên UI.", "WARN")
            except Exception:
                continue

    # Retry sau khi ép xử lý popup consent thêm lần nữa, phòng trường hợp popup xuất hiện muộn.
    await _accept_first_time_upload_consent(page, log_cb)
    for selector in clickable_selectors:
        for pattern in chooser_patterns:
            try:
                target = page.locator(selector).filter(has_text=pattern).first
                if await target.count() <= 0:
                    continue
                if not await target.is_visible(timeout=400):
                    continue
                async with page.expect_file_chooser(timeout=2000) as chooser_info:
                    await target.click()
                chooser = await chooser_info.value
                await chooser.set_files(abs_path)
                await asyncio.sleep(1.2)
                verify = await verify_fn(page, abs_path)
                debug_info["attempts"].append({
                    "method": "file_chooser_retry_after_consent",
                    "selector": selector,
                    "pattern": pattern.pattern,
                    "verify": verify,
                    "url_after_set": str(page.url or ""),
                })
                if verify.get("verified"):
                    if "/scene/" in str(page.url or ""):
                        verify["verified"] = False
                        debug_info["attempts"][-1]["guard_fail"] = "moved_to_scene_page"
                        log_cb("  Retry upload sau consent nhưng bị chuyển sang scene page, bỏ attempt này.", "WARN")
                        continue
                    debug_info["verified"] = True
                    debug_info["method"] = "file_chooser_retry_after_consent"
                    debug_info["verify"] = verify
                    log_cb(f"  Retry upload sau consent thành công: {os.path.basename(abs_path)}", "OK")
                    return True, debug_info
            except Exception:
                continue

    if allow_direct_file_input:
        try:
            file_inputs = page.locator('input[type="file"]')
            count = await file_inputs.count()
            for idx in range(min(count, 12)):
                try:
                    await file_inputs.nth(idx).set_input_files(abs_path)
                    await asyncio.sleep(1.2)
                    verify = await verify_fn(page, abs_path)
                    debug_info["attempts"].append({
                        "method": "set_input_files",
                        "input_index": idx,
                        "verify": verify,
                        "url_after_set": str(page.url or ""),
                    })
                    if verify.get("verified"):
                        if "/scene/" in str(page.url or ""):
                            verify["verified"] = False
                            debug_info["attempts"][-1]["guard_fail"] = "moved_to_scene_page"
                            log_cb("  set_input_files làm nhảy sang scene page, bỏ attempt này.", "WARN")
                            continue
                        debug_info["verified"] = True
                        debug_info["method"] = f"set_input_files#{idx}"
                        debug_info["verify"] = verify
                        log_cb(f"  Đã upload ảnh tham chiếu bằng input file #{idx}: {os.path.basename(abs_path)}", "OK")
                        return True, debug_info
                except Exception:
                    continue
        except Exception:
            pass

    log_cb(f"  Không upload/xác minh được ảnh tham chiếu cho file {os.path.basename(abs_path)}", "WARN")
    return False, debug_info
