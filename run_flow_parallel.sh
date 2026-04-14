#!/bin/zsh

# run_flow_parallel.sh
# ────────────────────
# Chạy nhiều kịch bản song song:
# - Bước 1: Chrome IMAGE tạo ảnh reference tuần tự từng kịch bản
# - Bước 2: Các Chrome VIDEO chạy song song (mỗi Chrome 1 kịch bản)
#
# Lưu ý:
# - Mỗi Chrome Video có proxy riêng (xem config/video_workers.json)
# - Kịch bản nào xong ảnh → Chrome Video đó bắt đầu ngay (pipeline)
# - Output của mỗi kịch bản lưu vào scenarios/<tên>/output/
#
# Cách dùng:
#   ./run_flow_parallel.sh             → chạy tất cả kịch bản
#   ./run_flow_parallel.sh --dry-run   → xem config không chạy thật
#   ./run_flow_parallel.sh --video-only → bỏ qua bước ảnh (ảnh đã có)

python3 parallel_runner.py "$@"
