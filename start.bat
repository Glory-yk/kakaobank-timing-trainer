@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================================
echo  🚀 Timing Workspace 서버시간 타이머 (Windows)
echo ========================================================

:: 1. 파이썬 설치 확인
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [오류] Python이 설치되어 있지 않거나 PATH에 등록되지 않았습니다.
    echo https://www.python.org 에서 Python을 설치(Add Python to PATH 체크 필수)해주세요.
    pause
    exit /b 1
)

:: 2. 가상환경(venv) 생성 및 라이브러리 자동 설치
if not exist "venv" (
    echo [INFO] 가상환경(venv) 생성 및 라이브러리 설치 중...
    python -m venv venv
    call venv\Scripts\pip install -r requirements.txt
)

:: 3. Tailscale Serve 등록 (설치된 경우 자동 실행)
where tailscale >nul 2>&1
if %ERRORLEVEL% equ 0 (
    tailscale serve --bg --https=8443 8080 >nul 2>&1
)

:: 4. 서버시간 타이머 실행
echo [INFO] 서버를 가동합니다...
call venv\Scripts\python server.py
pause
