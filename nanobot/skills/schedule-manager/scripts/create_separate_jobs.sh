#!/usr/bin/env bash
set -euo pipefail

# Create two independent daily jobs:
# 1) daily-reminder
# 2) daily-research-doc

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <CHAT_ID> [--channel zalo] [--reminder-time '0 5 * * *'] [--research-time '10 5 * * *']"
  exit 1
fi

CHAT_ID="$1"
shift

CHANNEL="zalo"
REMINDER_TIME="0 5 * * *"
RESEARCH_TIME="10 5 * * *"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel)
      CHANNEL="$2"
      shift 2
      ;;
    --reminder-time)
      REMINDER_TIME="$2"
      shift 2
      ;;
    --research-time)
      RESEARCH_TIME="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

echo "[1/2] Creating daily-reminder..."
nanobot cron add \
  --name "daily-reminder" \
  --message "Nhắc lịch buổi sáng: kiểm tra kế hoạch trong ngày." \
  --cron "$REMINDER_TIME" \
  --tz "Asia/Ho_Chi_Minh" \
  --deliver \
  --channel "$CHANNEL" \
  --to "$CHAT_ID"

echo "[2/2] Creating daily-research-doc..."
nanobot cron add \
  --name "daily-research-doc" \
  --message "Research chủ đề thị trường AI hôm nay, publish vào Google Docs và gửi link đọc." \
  --cron "$RESEARCH_TIME" \
  --tz "Asia/Ho_Chi_Minh" \
  --deliver \
  --channel "$CHANNEL" \
  --to "$CHAT_ID"

echo "Done. Current jobs:"
nanobot cron list

