import os
import re

file_path = "/Users/may1/Desktop/untitled-folder-3-flow-video-debug/dreamina.py"
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# Replace the blind mapping block
old_block = """                        if not scene_no:
                            # Khôi phục gán mù: Mặc dù thiếu an toàn, nhưng API mới của Flow không trả đủ data, 
                            # ta bắt buộc phải dựa vào lịch sử gửi prompt gần nhất để gán ID
                            scene_no = _guess_scene_for_unmapped_video_media()"""

new_block = """                        if not scene_no:
                            # Thay vì gán mù, tìm xem ID này đã được map lúc PENDING (từ generate) hay chưa
                            for s_no, media_dict in _video_media_state.items():
                                if media_id in media_dict:
                                    scene_no = s_no
                                    break
                            
                            # Nếu VẪN không tìm thấy scene, bỏ qua (đây có thể là ảnh reference từ STEP 1)
                            if not scene_no:
                                _register_orphan_video_media(media_id, status="")
                                continue"""

content = content.replace(old_block, new_block)

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)
