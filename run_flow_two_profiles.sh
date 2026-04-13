#!/bin/zsh

# Chay pipeline 2 step:
# - Step 1: dung Chrome profile IMAGE -> tao 4 anh reference (character1/2/3, image1)
# - Step 2: dung Chrome profile VIDEO -> render 25 video units
#
# Luu y:
# - KHONG kill session cu (giu login)
# - TAT upscale 2K (chi can anh thuong, nhanh hon)
# - Giu browser mo sau khi chay de kiem tra neu can

TARGET_PLATFORM=google_flow \
GOOGLE_FLOW_MEDIA_MODE=video \
GOOGLE_FLOW_RANDOM_PROMPTS=0 \
GOOGLE_FLOW_SEPARATE_CHROME_FOR_IMAGE_VIDEO=1 \
GOOGLE_FLOW_KILL_OLD_SESSION_BEFORE_RUN=0 \
GOOGLE_FLOW_KEEP_BROWSER_OPEN=1 \
GOOGLE_FLOW_AUTO_UPSCALE_2K=0 \
python3 dreamina.py
