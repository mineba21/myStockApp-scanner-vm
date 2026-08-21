#!/bin/bash
PROJECT="/Users/mac/Desktop/coding/myStockApp/stock-scanner"

# 포트 8000 이미 실행 중이면 브라우저만 열기
if lsof -i :8000 -sTCP:LISTEN > /dev/null 2>&1; then
    open "http://localhost:8000"
    exit 0
fi

echo "======================================"
echo "  Weinstein Stage Scanner 시작 중..."
echo "  http://localhost:8000"
echo "======================================"

cd "$PROJECT"
"$PROJECT/venv/bin/python" "$PROJECT/main.py" &

# 서버 뜰 때까지 대기
for i in $(seq 1 15); do
    sleep 1
    if lsof -i :8000 -sTCP:LISTEN > /dev/null 2>&1; then
        echo "서버 준비 완료! 브라우저를 엽니다..."
        open "http://localhost:8000"
        break
    fi
done

wait
