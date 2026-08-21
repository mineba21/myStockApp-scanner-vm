#!/bin/bash
# Weinstein Scanner 자동 시작 스크립트

PROJECT="/Users/mac/Desktop/coding/myStockApp/stock-scanner"
PYTHON="$PROJECT/venv/bin/python"

# venv가 준비될 때까지 대기 (부팅 직후 iCloud 동기화 등)
for i in $(seq 1 30); do
    [ -f "$PYTHON" ] && break
    sleep 1
done

# 포트 8000이 이미 사용 중이면 대기
for i in $(seq 1 10); do
    ! lsof -i :8000 -sTCP:LISTEN > /dev/null 2>&1 && break
    sleep 3
done

cd "$PROJECT"
exec "$PYTHON" "$PROJECT/main.py"
