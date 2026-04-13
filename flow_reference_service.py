"""
Service cho phần reference image của Flow.

Tách khỏi dreamina.py để:
- code dễ đọc hơn
- giảm rủi ro sửa nhầm luồng video chính
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Awaitable, Callable


def get_reference_image_path(reference_images_dir: str, scene_no: int) -> str:
    """
    Tìm file ảnh tham chiếu cho 1 cảnh theo quy ước canh_XXX.
    """
    scene_label = f"canh_{scene_no:03d}"
    for ext in [".png", ".jpg", ".jpeg", ".webp"]:
        path = os.path.join(reference_images_dir, scene_label + ext)
        if os.path.exists(path):
            return os.path.abspath(path)
    return ""


def get_reference_search_name(scene_no: int, image_path: str = "") -> str:
    """
    Trả về tên dùng để search ảnh trong thư viện Flow.
    """
    if image_path:
        stem = os.path.splitext(os.path.basename(image_path))[0].strip()
        if stem:
            return stem
    return f"canh_{scene_no:03d}"


def get_character_reference_image_path(reference_images_dir: str, character_token: str) -> str:
    """
    Tìm file ảnh tham chiếu nhân vật theo token chuẩn `TEN_NHAN_VAT`.
    """
    token = (character_token or "").strip().upper()
    if not token:
        return ""
    for ext in [".png", ".jpg", ".jpeg", ".webp"]:
        path = os.path.join(reference_images_dir, token + ext)
        if os.path.exists(path):
            return os.path.abspath(path)
    return ""


def list_reference_image_paths(reference_images_dir: str, limit: int = 0) -> list[str]:
    """
    Lấy danh sách ảnh trong thư mục reference.
    """
    out: list[str] = []
    if not os.path.isdir(reference_images_dir):
        return out
    for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
        for p in sorted(Path(reference_images_dir).glob(ext)):
            try:
                out.append(str(p.resolve()))
            except Exception:
                continue
    out = sorted(list(dict.fromkeys(out)))
    if limit > 0:
        out = out[:limit]
    return out


async def count_reference_thumbs_in_composer(page) -> int:
    """
    Đếm số thumbnail reference trong vùng composer dưới màn hình.
    """
    js = """
    () => {
      const vh = window.innerHeight || 0;
      const imgs = Array.from(document.querySelectorAll('img'));
      let count = 0;
      for (const img of imgs) {
        const r = img.getBoundingClientRect();
        if (!r || r.width <= 0 || r.height <= 0) continue;
        if (r.y < vh * 0.58) continue;
        if (r.width < 18 || r.width > 90 || r.height < 18 || r.height > 90) continue;
        const src = (img.currentSrc || img.src || '').toLowerCase();
        if (!src) continue;
        if (src.includes('blob:') || src.includes('/image/') || src.includes('media.getmediaurlredirect')) {
          count += 1;
        }
      }
      return count;
    }
    """
    try:
        return int(await page.evaluate(js))
    except Exception:
        return 0


async def clear_reference_attachments_in_composer(
    page,
    focus_prompt_cb: Callable[[], Awaitable[object]],
    max_rounds: int = 2,
) -> dict:
    """
    Xóa reference cũ trong composer trước khi attach reference mới.
    """
    info = {"before": 0, "after": 0, "rounds": 0, "cleared": False}
    info["before"] = await count_reference_thumbs_in_composer(page)
    if info["before"] <= 0:
        info["cleared"] = True
        return info

    async def _handle_delete_confirmation_dialog() -> bool:
        """
        Flow đôi khi hiện popup xác nhận khi xóa reference.
        Hàm này tự xử lý popup để không kẹt pipeline.
        """
        try:
            dialog = page.locator("[role='dialog']").filter(has_text="Bạn có muốn xóa").first
            if await dialog.count() > 0 and await dialog.is_visible(timeout=250):
                for label in ["Xóa", "Xoá", "Delete"]:
                    try:
                        btn = dialog.get_by_role("button", name=label, exact=False).first
                        if await btn.count() > 0 and await btn.is_visible(timeout=120):
                            await btn.click()
                            await asyncio.sleep(0.2)
                            return True
                    except Exception:
                        continue
                # Fallback: bấm ESC để đóng dialog nếu không bắt được nút.
                try:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.15)
                    return True
                except Exception:
                    return False
        except Exception:
            return False
        return False

    for r in range(max_rounds):
        info["rounds"] = r + 1
        try:
            # Chỉ focus composer để giới hạn phạm vi thao tác xóa.
            # Không dùng Cmd/Ctrl+A vì dễ chọn nhầm nội dung ngoài composer.
            await focus_prompt_cb()
            await asyncio.sleep(0.08)
        except Exception:
            pass

        try:
            await page.evaluate(
                """
                () => {
                  const vh = window.innerHeight || 0;
                  const candidates = Array.from(document.querySelectorAll('button,[role="button"],i,span,div'));
                  for (const el of candidates) {
                    const r = el.getBoundingClientRect();
                    if (!r || r.width <= 0 || r.height <= 0) continue;
                    if (r.y < vh * 0.55) continue;
                    const txt = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
                    const cls = (el.className || '').toString().toLowerCase();
                    const looksClose = txt.includes('close') || txt.includes('remove') || txt.includes('xóa')
                      || txt.trim() === 'x' || cls.includes('close') || cls.includes('remove');
                    if (!looksClose) continue;
                    // Tránh xóa nhầm thẻ ảnh lớn/canvas: chỉ nhấn nút close nhỏ.
                    if (r.width > 80 || r.height > 80) continue;
                    try { el.click(); } catch (_) {}
                  }
                }
                """
            )
        except Exception:
            pass

        # Nếu click remove gây bật popup xác nhận thì xử lý ngay.
        try:
            await _handle_delete_confirmation_dialog()
        except Exception:
            pass

        await asyncio.sleep(0.35)
        after = await count_reference_thumbs_in_composer(page)
        if after <= 0:
            info["after"] = 0
            info["cleared"] = True
            return info

    info["after"] = await count_reference_thumbs_in_composer(page)
    info["cleared"] = info["after"] <= 0
    return info
