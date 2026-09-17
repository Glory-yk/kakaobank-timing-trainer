#!/bin/bash
cd "$(dirname "$0")"

# 가상환경 미존재 시 자동 생성 및 의존성 설치
if [ ! -d "venv" ]; then
    echo "📦 가상환경(venv) 생성 및 라이브러리 설치 중..."
    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt
fi

# Tailscale Serve 자동 백그라운드 등록 (LTE/외부 접속용 - 8443 포트로 외부 HTTPS 수신)
if command -v tailscale >/dev/null 2>&1; then
    tailscale serve --bg --https=8443 8080 >/dev/null 2>&1 || true
fi

echo "🚀 Timing Workspace 서버시간 타이머 실행 중..."
./venv/bin/python3 server.py
