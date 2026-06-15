"""
Service xử lý prompt cho luồng Dreamina/Google Flow.

Mục tiêu module này:
- Gom toàn bộ logic đọc/ghi/parse prompt vào một nơi.
- Giữ hành vi cũ, chỉ tách cấu trúc file để dễ bảo trì.
"""

import json
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path

# Hằng số mặc định để module có thể dùng độc lập.
DEFAULT_PROMPTS_FILE = "prompts.txt"
DEFAULT_PROMPTS_DIR = "prompts"
DEFAULT_PROMPT_POOL_FILE = os.path.join(DEFAULT_PROMPTS_DIR, "prompt_pool_1000.txt")
DEFAULT_PROMPT_POOL_STATE_FILE = os.path.join(DEFAULT_PROMPTS_DIR, "prompt_pool_state.json")


def load_prompts_from_file(path: str = DEFAULT_PROMPTS_FILE) -> list[str]:
    """
    Đọc danh sách prompt từ file text.
    Mỗi dòng là 1 prompt, bỏ dòng trống.
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return []


def parse_structured_story_input(path: str = DEFAULT_PROMPTS_FILE) -> dict:
    """
    Parse file prompt dạng "FULL VIDEO PROMPTS" + "characterX/imageX".

    Trả về cấu trúc gồm:
    - references: map label -> mô tả reference
    - reference_generation_prompts: prompt dùng cho bước tạo ảnh reference
    - reference_scene_to_label: map scene index nội bộ -> label
    - video_prompts: prompt đã chuẩn hoá cho từng cảnh video
    """
    if not os.path.exists(path):
        return {"is_structured": False}

    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return {"is_structured": False}

    # Chỉ coi là structured khi có marker rõ ràng của bộ prompt đầy đủ.
    if "FULL VIDEO PROMPTS" not in text or "CHARACTER REFERENCE IMAGE PROMPTS" not in text:
        return {"is_structured": False}

    def _extract_reference_candidates_from_zone(zone_text: str) -> dict[str, list[str]]:
        """
        Parse candidate reference theo cả 2 định dạng:
        - CHARACTER1 = ...
        - CHARACTER1. ...
        Hỗ trợ mô tả kéo dài nhiều dòng liên tiếp.
        """
        out: dict[str, list[str]] = {}
        if not zone_text:
            return out

        lines = zone_text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            m = re.match(
                r"^\s*(character\d+|image\d+)\s*(=|\.)\s*(.+?)\s*$",
                line,
                flags=re.IGNORECASE,
            )
            if not m:
                i += 1
                continue

            label = m.group(1).strip().lower()
            chunk = [m.group(3).strip()]
            i += 1
            while i < len(lines):
                nxt = lines[i]
                nxt_strip = nxt.strip()
                if not nxt_strip:
                    break
                if re.match(r"^\s*(character\d+|image\d+)\s*(=|\.)\s*", nxt, flags=re.IGNORECASE):
                    break
                if nxt_strip.startswith("===") or re.match(r"(?i)^video\s+\d+\s*:", nxt_strip):
                    break
                if re.match(r"(?i)^(character mapping|background mapping)\s*:", nxt_strip):
                    break
                chunk.append(nxt_strip)
                i += 1

            value = " ".join([c for c in chunk if c]).strip()
            if value:
                out.setdefault(label, []).append(value)

            while i < len(lines) and not lines[i].strip():
                i += 1
        return out

    references: dict[str, str] = {}
    full_marker = "FULL VIDEO PROMPTS"
    full_idx = text.find(full_marker)
    pre_full_zone = text[:full_idx] if full_idx >= 0 else text

    fixed_mapping_marker = "FIXED CHARACTER AND BACKGROUND MAPPING"
    mapping_idx = pre_full_zone.find(fixed_mapping_marker)
    rich_reference_zone = pre_full_zone[:mapping_idx] if mapping_idx >= 0 else pre_full_zone

    rich_candidates = _extract_reference_candidates_from_zone(rich_reference_zone)
    fallback_candidates = _extract_reference_candidates_from_zone(pre_full_zone)

    all_labels = sorted(set(list(rich_candidates.keys()) + list(fallback_candidates.keys())))
    for label in all_labels:
        cands = (rich_candidates.get(label, []) or []) + (fallback_candidates.get(label, []) or [])
        best = ""
        for c in cands:
            t = (c or "").strip()
            if len(t) > len(best):
                best = t
        if best:
            references[label] = best

    video_prompts: list[str] = []
    block_pattern = re.compile(
        r"---\s*Video\s+(\d+)\s*\([^\n]*\)\s*---\s*(.*?)(?=\n---\s*Video\s+\d+\s*\(|\n={10,}\nFINAL QUALITY CHECK|\Z)",
        flags=re.IGNORECASE | re.DOTALL,
    )

    for block in block_pattern.finditer(text):
        video_no = int(block.group(1))
        body = (block.group(2) or "").strip()
        if not body:
            continue

        used_labels: list[str] = []
        ref_guide_match = re.search(
            r"Reference guide:\s*(.*?)(?:\n\s*\n|^0s-\d+s:)",
            body,
            flags=re.IGNORECASE | re.DOTALL | re.MULTILINE,
        )
        if ref_guide_match:
            ref_guide_text = ref_guide_match.group(1)
            for lm in re.finditer(r"^\s*(character\d+|image\d+)\s*=", ref_guide_text, flags=re.IGNORECASE | re.MULTILINE):
                lb = lm.group(1).strip().lower()
                if lb not in used_labels:
                    used_labels.append(lb)

        body_without_ref = re.sub(
            r"Reference guide:\s*.*?(?:\n\s*\n|^0s-\d+s:)",
            "\n",
            body,
            flags=re.IGNORECASE | re.DOTALL | re.MULTILINE,
        ).strip()

        if not used_labels:
            used_labels = sorted(references.keys())

        lines = [f"CẢNH {video_no:03d}: VIDEO UNIT {video_no}"]
        lines.append("Reference labels phải giữ cố định:")
        for lb in used_labels:
            ref_text = references.get(lb, "")
            if ref_text:
                lines.append(f"- {lb}: {ref_text}")
            else:
                lines.append(f"- {lb}: (không tìm thấy mô tả reference)")
        lines.append("")
        lines.append("Nội dung shot/video cần tạo:")
        lines.append(body_without_ref)
        lines.append("")
        lines.append("Ràng buộc: giữ continuity nhân vật/background, ánh sáng sáng sớm, cinematic realism.")
        video_prompts.append("\n".join(lines).strip())

    # Fallback parser cho format "Video 1: ..."
    if not video_prompts:
        full_idx = text.find(full_marker)
        video_zone = text[full_idx + len(full_marker):] if full_idx >= 0 else text
        video_headers = list(re.finditer(r"(?im)^\s*video\s+(\d+)\s*:\s*", video_zone))
        for i, h in enumerate(video_headers):
            video_no = int(h.group(1))
            body_start = h.end()
            body_end = video_headers[i + 1].start() if i + 1 < len(video_headers) else len(video_zone)
            body = (video_zone[body_start:body_end] or "").strip()
            if not body:
                continue

            used_labels: list[str] = []
            for lm in re.finditer(r"\b(character\d+|image\d+)\b\s*=", body, flags=re.IGNORECASE):
                lb = lm.group(1).strip().lower()
                if lb not in used_labels:
                    used_labels.append(lb)

            body_without_ref = re.sub(
                r"^\s*reference\s+guide\s*:\s*",
                "",
                body,
                flags=re.IGNORECASE,
            ).strip()

            shot_anchor = re.search(r"\b0s\s*-\s*\d+s\s*:", body_without_ref, flags=re.IGNORECASE)
            if shot_anchor:
                body_without_ref = body_without_ref[shot_anchor.start():].strip()

            if not used_labels:
                used_labels = sorted(references.keys())

            lines = [f"CẢNH {video_no:03d}: VIDEO UNIT {video_no}"]
            lines.append("Reference labels phải giữ cố định:")
            for lb in used_labels:
                ref_text = references.get(lb, "")
                if ref_text:
                    lines.append(f"- {lb}: {ref_text}")
                else:
                    lines.append(f"- {lb}: (không tìm thấy mô tả reference)")
            lines.append("")
            lines.append("Nội dung shot/video cần tạo:")
            lines.append(body_without_ref)
            lines.append("")
            lines.append("Ràng buộc: giữ continuity nhân vật/background, ánh sáng sáng sớm, cinematic realism.")
            video_prompts.append("\n".join(lines).strip())

    reference_generation_prompts: list[str] = []
    reference_scene_to_label: dict[int, str] = {}
    ref_index = 0
    ordered_labels = sorted(references.keys(), key=lambda x: (0 if x.startswith("character") else 1, x))
    for label in ordered_labels:
        ref_index += 1
        scene_no = ref_index
        reference_scene_to_label[scene_no] = label
        ref_id = f"ref_{ref_index:02d}"
        reference_generation_prompts.append(f"{ref_id}: {references[label]}")

    return {
        "is_structured": True,
        "references": references,
        "reference_generation_prompts": reference_generation_prompts,
        "reference_scene_to_label": reference_scene_to_label,
        "video_prompts": video_prompts,
        "video_count": len(video_prompts),
    }


def pick_random_prompts(prompts: list[str], count: int) -> list[str]:
    """Chọn ngẫu nhiên `count` prompt không trùng nhau."""
    if not prompts:
        return []
    n = max(1, int(count))
    if len(prompts) <= n:
        return prompts[:]
    return random.sample(prompts, n)


def save_selected_prompts_for_session(
    prompts: list[str],
    debug_session_dir: str,
    filename: str = "selected_prompts_google_flow.txt",
) -> str:
    """
    Lưu danh sách prompt đã chọn trong session hiện tại để debug đối chiếu.
    """
    if not debug_session_dir:
        return ""
    path = os.path.join(debug_session_dir, filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            for i, p in enumerate(prompts, start=1):
                f.write(f"{i:03d}. {p}\n")
        return path
    except Exception:
        return ""


def save_prompts_to_prompts_folder(
    prompts: list[str],
    filename: str,
    prompts_dir: str = DEFAULT_PROMPTS_DIR,
) -> str:
    """
    Ghi danh sách prompt vào folder `prompts/` để dễ kiểm tra lại.
    """
    Path(prompts_dir).mkdir(parents=True, exist_ok=True)
    out = os.path.join(prompts_dir, filename)
    try:
        with open(out, "w", encoding="utf-8") as f:
            for i, p in enumerate(prompts, start=1):
                f.write(f"### PROMPT {i:03d}\n{p}\n\n")
        return os.path.abspath(out)
    except Exception:
        return ""


def safe_filename(text: str, max_len: int = 40) -> str:
    """Chuyển text tự do thành chuỗi an toàn để làm tên file."""
    safe = re.sub(r"[^\w\s-]", "_", text[:max_len]).strip().replace(" ", "_")
    return safe or "prompt"


def extract_scene_number(prompt_text: str, fallback: int) -> int:
    """
    Tách số cảnh từ nội dung prompt.
    Ví dụ: "CẢNH 030" -> 30; không tách được thì trả `fallback`.
    """
    if not prompt_text:
        return fallback
    m = re.search(r"(?:cảnh|canh)\s*0*(\d+)", prompt_text, flags=re.IGNORECASE)
    if not m:
        return fallback
    try:
        return int(m.group(1))
    except Exception:
        return fallback


def build_auto_test_prompts(count: int) -> list[str]:
    """
    Tạo prompt test ngẫu nhiên để mỗi lần chạy đều khác nhau (dễ debug).
    Format luôn bắt đầu bằng: "CẢNH X: ..."
    """
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    random.seed(time.time_ns())

    locations = [
        "hành lang khách sạn sang trọng",
        "quán cafe kính nhìn ra phố mưa",
        "ga tàu điện ngầm giờ cao điểm",
        "sân thượng thành phố lúc bình minh",
        "nhà bếp công nghiệp ánh đèn vàng",
        "studio trắng tối giản",
    ]
    moods = [
        "căng thẳng nhưng kiểm soát",
        "bình tĩnh lạnh lùng",
        "ngạc nhiên nhẹ",
        "quyết đoán mạnh",
        "trầm tư sâu",
        "hy vọng trở lại",
    ]
    styles = [
        "cinematic realism",
        "photojournalistic",
        "high-detail editorial",
        "natural documentary style",
        "dramatic film still",
    ]
    camera_shots = [
        "medium shot, eye-level",
        "close-up portrait, shallow depth of field",
        "wide shot, leading lines",
        "over-shoulder composition",
        "low-angle dramatic shot",
    ]
    lighting = [
        "soft daylight through window",
        "moody low-key lighting",
        "neon rim light",
        "warm practical lights",
        "high contrast studio light",
    ]

    prompts = []
    for i in range(1, count + 1):
        loc = random.choice(locations)
        mood = random.choice(moods)
        style = random.choice(styles)
        shot = random.choice(camera_shots)
        light = random.choice(lighting)
        unique_tag = random.randint(1000, 9999)

        prompts.append(
            f"CẢNH {i}: TEST RUN {run_id}-{unique_tag}. "
            f"Nhân vật nữ đứng tại {loc}, cảm xúc {mood}. "
            f"Phong cách {style}, góc máy {shot}, ánh sáng {light}, "
            f"chi tiết da thật, texture quần áo rõ, background có chiều sâu, "
            f"không chữ, không watermark."
        )

    return prompts


def save_generated_prompts(prompts: list[str], prompts_dir: str = DEFAULT_PROMPTS_DIR) -> str:
    """
    Lưu bộ prompt test ra file để có thể đối chiếu sau khi chạy.
    """
    Path(prompts_dir).mkdir(parents=True, exist_ok=True)
    out_path = os.path.abspath(
        os.path.join(
            prompts_dir,
            f"auto_test_prompts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
        )
    )
    with open(out_path, "w", encoding="utf-8") as f:
        for p in prompts:
            f.write(p + "\n")
    return out_path


def load_prompt_pool(
    prompt_pool_file: str = DEFAULT_PROMPT_POOL_FILE,
    prompts_dir: str = DEFAULT_PROMPTS_DIR,
) -> list[str]:
    """
    Đọc danh sách prompt pool.
    - Ưu tiên file mới: prompt_pool_1000.txt
    - Fallback file cũ: prompt_pool_100.txt
    """
    candidate_files = [
        prompt_pool_file,
        os.path.join(prompts_dir, "prompt_pool_100.txt"),
    ]
    for file_path in candidate_files:
        if not os.path.exists(file_path):
            continue
        with open(file_path, "r", encoding="utf-8") as f:
            rows = [ln.strip() for ln in f if ln.strip()]
        if rows:
            return rows
    return []


def load_prompt_pool_state(prompt_pool_state_file: str = DEFAULT_PROMPT_POOL_STATE_FILE) -> dict:
    """
    Đọc trạng thái con trỏ pool (next_index).
    """
    if not os.path.exists(prompt_pool_state_file):
        return {"next_index": 0}
    try:
        with open(prompt_pool_state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"next_index": int(data.get("next_index", 0))}
    except Exception:
        return {"next_index": 0}


def save_prompt_pool_state(
    next_index: int,
    prompt_pool_state_file: str = DEFAULT_PROMPT_POOL_STATE_FILE,
    prompts_dir: str = DEFAULT_PROMPTS_DIR,
) -> None:
    """Lưu trạng thái pool để lần sau lấy đúng block prompt kế tiếp."""
    Path(prompts_dir).mkdir(parents=True, exist_ok=True)
    payload = {
        "next_index": next_index,
        "updated_at": datetime.now().isoformat(),
    }
    with open(prompt_pool_state_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def take_prompts_from_pool(
    batch_size: int,
    prompt_pool_file: str = DEFAULT_PROMPT_POOL_FILE,
    prompt_pool_state_file: str = DEFAULT_PROMPT_POOL_STATE_FILE,
    prompts_dir: str = DEFAULT_PROMPTS_DIR,
) -> tuple[list[str], dict]:
    """
    Lấy đúng batch_size prompt từ pool theo cơ chế xoay vòng.
    """
    pool = load_prompt_pool(prompt_pool_file=prompt_pool_file, prompts_dir=prompts_dir)
    if not pool:
        return [], {"reason": "pool_empty"}

    total = len(pool)
    state = load_prompt_pool_state(prompt_pool_state_file=prompt_pool_state_file)
    start = state.get("next_index", 0) % total

    selected = []
    idx = start
    for _ in range(batch_size):
        selected.append(pool[idx])
        idx = (idx + 1) % total

    save_prompt_pool_state(
        idx,
        prompt_pool_state_file=prompt_pool_state_file,
        prompts_dir=prompts_dir,
    )
    return selected, {
        "total": total,
        "start_index": start,
        "end_index_exclusive": idx,
    }


def write_prompts_file(prompts: list[str], out_file: str = DEFAULT_PROMPTS_FILE) -> None:
    """
    Ghi prompt vào file đích (mỗi dòng 1 prompt).
    """
    with open(out_file, "w", encoding="utf-8") as f:
        for p in prompts:
            f.write(p + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# ĐỊNH NGHĨA TÊN 2 FILE MỚI (dùng chung toàn dự án)
# ─────────────────────────────────────────────────────────────────────────────
SCENARIO_CHARACTER_FILE = "prompt_character.txt"  # chứa [TÊN] + prompt nhân vật
SCENARIO_IMAGE_FILE     = "prompt_image.txt"      # chứa Video 1: "..." ảnh cảnh


def parse_character_file(path: str) -> dict[str, str]:
    """
    Đọc file prompt_character.txt với format:

        [TÊN NHÂN VẬT]
        "Mô tả nhân vật..."

    Trả về dict: key = tên file an toàn (NADINE_LECLERC), value = nội dung prompt.
    Ví dụ đầu ra:
        {
            "NADINE_LECLERC": "Ultra-realistic portrait...",
            "RICHARD_ASHWORTH": "Ultra-realistic portrait...",
        }
    """
    if not os.path.exists(path):
        return {}

    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return {}

    result: dict[str, str] = {}
    # Tách theo block bắt đầu bằng [TÊN]
    blocks = re.split(r"\n(?=\[)", text.strip())
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        # Dòng đầu phải là [TÊN NHÂN VẬT]
        header_match = re.match(r"^\[([^\]]+)\]", block)
        if not header_match:
            continue
        raw_name = header_match.group(1).strip()

        # Chuyển tên thành key file an toàn: "Nadine LeClerc" → "NADINE_LECLERC"
        file_key = re.sub(r"\s+", "_", raw_name.upper().strip())
        file_key = re.sub(r"[^\w]", "", file_key)  # chỉ giữ chữ, số, _

        # Nội dung prompt: phần còn lại sau dòng header
        body = block[header_match.end():].strip()
        # Bỏ dấu ngoặc kép bọc ngoài nếu có
        if body.startswith('"') and body.endswith('"'):
            body = body[1:-1].strip()
        if body:
            result[file_key] = body

    return result


def parse_image_prompts_file(path: str) -> list[str]:
    """
    Đọc file prompt_image.txt với format:

        Video 1: "Mô tả ảnh cảnh 1..."

        Video 2: "Mô tả ảnh cảnh 2..."

    Trả về danh sách nội dung prompt theo thứ tự Video 1, 2, 3...
    Chỉ lấy nội dung, KHÔNG bao gồm "Video X:" prefix.
    """
    if not os.path.exists(path):
        return []

    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return []

    # Tìm tất cả block "Video N: ..."
    # Hỗ trợ cả 2 kiểu: có dấu ngoặc kép và không có
    headers = list(re.finditer(r"(?im)^\s*video\s+(\d+)\s*:\s*", text))
    prompts: list[str] = []

    for i, h in enumerate(headers):
        body_start = h.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[body_start:body_end].strip()

        # Bỏ dấu ngoặc kép bọc ngoài nếu có
        if body.startswith('"') and body.endswith('"'):
            body = body[1:-1].strip()

        if body:
            prompts.append(body)

    return prompts


def parse_scenario_two_files(scenario_dir: str) -> dict:
    """
    Đọc và parse 2 file mới trong thư mục kịch bản:
        - prompt_character.txt  → danh sách nhân vật cần tạo ảnh
        - prompt_image.txt      → danh sách cảnh cần tạo ảnh

    Trả về cấu trúc tương thích với parse_structured_story_input() cũ:
    {
        "is_two_file": True,
        "characters": {                   # key = file_key, value = prompt text
            "NADINE_LECLERC": "...",
            "RICHARD_ASHWORTH": "...",
        },
        "image_prompts": ["...", "..."],  # prompt ảnh cảnh theo thứ tự
        # Tương thích ngược với format cũ:
        "reference_generation_prompts": ["NADINE_LECLERC: ...", ...],
        "reference_scene_to_label": {1: "NADINE_LECLERC", 2: "RICHARD_ASHWORTH", ...},
        "video_prompts": [],              # rỗng vì không có video
        "is_structured": True,
    }
    """
    char_path  = os.path.join(scenario_dir, SCENARIO_CHARACTER_FILE)
    image_path = os.path.join(scenario_dir, SCENARIO_IMAGE_FILE)

    # Đọc nhân vật
    characters = parse_character_file(char_path)

    # Đọc ảnh cảnh
    image_prompts = parse_image_prompts_file(image_path)

    # Kiểm tra ít nhất 1 file có dữ liệu
    if not characters and not image_prompts:
        return {"is_two_file": False, "is_structured": False}

    # Tạo reference_generation_prompts tương thích với code cũ
    # Thứ tự: character trước (sắp xếp abc), rồi đến image (nếu có)
    reference_generation_prompts: list[str] = []
    reference_scene_to_label: dict[int, str] = {}

    ref_index = 0
    for file_key, prompt_text in sorted(characters.items()):
        ref_index += 1
        reference_scene_to_label[ref_index] = file_key
        reference_generation_prompts.append(f"ref_{ref_index:02d}: {prompt_text}")

    return {
        "is_two_file": True,
        "is_structured": True,       # tương thích với code cũ
        "characters": characters,
        "image_prompts": image_prompts,
        # Tương thích ngược với interface cũ
        "references": {k: v for k, v in characters.items()},
        "reference_generation_prompts": reference_generation_prompts,
        "reference_scene_to_label": reference_scene_to_label,
        "video_prompts": [],         # không có video nữa
        "video_count": 0,
    }
