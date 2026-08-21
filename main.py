"""Weinstein Stage Scanner - 진입점"""
import uvicorn
import logging
import sys
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler("scanner.log", encoding="utf-8")],
)

if __name__ == "__main__":
    if not os.path.exists(".env"):
        print("⚠️  .env 없음 → .env.example 복사 후 설정하세요")
        print("   cp .env.example .env")

    print("=" * 55)
    print("  Weinstein Stage Scanner")
    print("  http://localhost:8000")
    print("=" * 55)

    uvicorn.run("web.app:app", host="127.0.0.1", port=8000,
                reload=False, log_level="info")
