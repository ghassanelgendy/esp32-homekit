#!/bin/bash
ACTION="$1"
TARGET="ghass@192.168.1.105"

case "$ACTION" in
  restart)
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=4 "$TARGET" "shutdown /r /f /t 0"
    ;;
  shutdown|poweroff)
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=4 "$TARGET" "shutdown /s /f /t 0"
    ;;
  *)
    echo "Usage: $0 {restart|shutdown}"
    exit 1
    ;;
esac
