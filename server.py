import os
import time
import socket
import threading
import urllib.request
import email.utils
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="KakaoBank Golden Zone Trainer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

KST = timezone(timedelta(hours=9))

class KakaoBankClock:
    """www.kakaobank.com 실제 서버 시간 초정밀 추적기 (Edge Transition)"""
    def __init__(self):
        self.target_url = "https://www.kakaobank.com"
        self.offset_ms = 0.0
        self.last_rtt_ms = 0.0
        self.last_sync_time = 0.0
        self.sync_count = 0
        self.is_syncing = False
        self.status = "초기화 대기 중"

    def sync(self) -> bool:
        if self.is_syncing:
            return False
        self.is_syncing = True
        try:
            req = urllib.request.Request(self.target_url, method="HEAD")
            req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")
            req.add_header("Cache-Control", "no-cache")

            t0 = time.time()
            with urllib.request.urlopen(req, timeout=3) as resp:
                date_str = resp.headers.get("Date")
            t1 = time.time()
            init_epoch = email.utils.parsedate_to_datetime(date_str).timestamp()

            detected = False
            for _ in range(25):
                time.sleep(0.04)
                t_send = time.time()
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    cur_date = resp.headers.get("Date")
                t_recv = time.time()
                cur_epoch = email.utils.parsedate_to_datetime(cur_date).timestamp()

                if cur_epoch > init_epoch:
                    rtt_ms = (t_recv - t_send) * 1000.0
                    edge_local_ms = (t_recv - (rtt_ms / 2000.0)) * 1000.0
                    kakao_epoch_ms = cur_epoch * 1000.0

                    self.offset_ms = kakao_epoch_ms - edge_local_ms
                    self.last_rtt_ms = round(rtt_ms, 1)
                    self.last_sync_time = time.time()
                    self.sync_count += 1
                    self.status = "동기화 완료 (정밀)"
                    detected = True
                    break

            if not detected:
                single_rtt = (t1 - t0) * 1000.0
                approx_kakao_ms = (init_epoch + 0.5) * 1000.0
                approx_local_ms = (t1 - (single_rtt / 2000.0)) * 1000.0
                self.offset_ms = approx_kakao_ms - approx_local_ms
                self.last_rtt_ms = round(single_rtt, 1)
                self.last_sync_time = time.time()
                self.sync_count += 1
                self.status = "동기화 완료 (단일 추정)"

            return True
        except Exception as e:
            self.status = f"동기화 오류: {str(e)}"
            return False
        finally:
            self.is_syncing = False

    def get_time_ms(self) -> float:
        return (time.time() * 1000.0) + self.offset_ms

    def get_kst_datetime(self, timestamp_ms: Optional[float] = None) -> datetime:
        epoch_sec = (timestamp_ms if timestamp_ms else self.get_time_ms()) / 1000.0
        return datetime.fromtimestamp(epoch_sec, tz=KST)


kakao_clock = KakaoBankClock()


def background_sync_worker():
    while True:
        try:
            kakao_clock.sync()
        except Exception:
            pass
        time.sleep(15)


threading.Thread(target=background_sync_worker, daemon=True).start()


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.get("/api/time-sync")
async def time_sync(client_send: Optional[float] = Query(None)):
    kakao_ms = kakao_clock.get_time_ms()
    ago = round(time.time() - kakao_clock.last_sync_time, 1) if kakao_clock.last_sync_time else 999
    return {
        "client_send": client_send,
        "server_time": kakao_ms,
        "kakaobank_offset_ms": round(kakao_clock.offset_ms, 1),
        "kakaobank_rtt_ms": kakao_clock.last_rtt_ms,
        "sync_status": kakao_clock.status,
        "last_sync_ago_sec": ago,
    }


@app.post("/api/kakaobank/resync")
async def resync_kakaobank():
    success = kakao_clock.sync()
    return {
        "success": success,
        "status": kakao_clock.status,
        "offset_ms": round(kakao_clock.offset_ms, 1),
        "rtt_ms": kakao_clock.last_rtt_ms
    }


@app.post("/api/click")
async def register_click(
    interval: int = Query(10, description="목표 주기(초): 10, 60, 5"),
    client_touch_ms: Optional[float] = Query(None, description="클라이언트 화면 터치 시각 (카카오뱅크 동기화 ms)")
):
    """
    [인간공학적 골든존 확장 모델링]
    - 골든존 윈도우 폭을 약 220ms로 넉넉하게 확장하여 웹 화면을 보며 폰을 누를 때 
      충분히 성취감 있게 적중할 수 있도록 최적화.
    - 골든존: 59.400s ~ 59.620s (10초 모드: 9.400s ~ 9.620s)
    - 울트라 퍼펙트: 59.460s ~ 59.540s (신의 타이밍)
    - 위험 구간 (튕김): 59.250s ~ 59.390s
    - 안전권 (세이프): 59.620s ~ 59.750s
    """
    if client_touch_ms:
        touch_epoch_ms = client_touch_ms
    else:
        touch_epoch_ms = kakao_clock.get_time_ms() - 10.0

    touch_dt = kakao_clock.get_kst_datetime(touch_epoch_ms)
    ms_float = touch_dt.microsecond / 1000.0
    sec_in_cycle = (touch_dt.second % interval) + (ms_float / 1000.0)

    # 타깃 윈도우 (확장된 골든존)
    # interval=10 -> 골든존: 9.400s ~ 9.620s (폭 220ms)
    # 울트라 퍼펙트: 9.460s ~ 9.540s (중심 9.500s)
    golden_start = interval - 0.600  # 9.400s
    golden_end = interval - 0.380    # 9.620s
    golden_center = interval - 0.500 # 9.500s

    ultra_start = interval - 0.540   # 9.460s
    ultra_end = interval - 0.460     # 9.540s

    danger_start = interval - 0.750  # 9.250s
    danger_end = golden_start        # 9.400s

    safe_end = interval - 0.250      # 9.750s

    diff_from_center = round((sec_in_cycle - golden_center) * 1000.0, 1)
    if diff_from_center > (interval * 500.0):
        diff_from_center -= (interval * 1000.0)
    elif diff_from_center < -(interval * 500.0):
        diff_from_center += (interval * 1000.0)

    # 실전 예상 서버 도착 시각 (+50ms ~ +250ms)
    simulated_arrive_diff_ms = round(diff_from_center + 90.0, 1)

    # 등급 판정
    if ultra_start <= sec_in_cycle <= ultra_end:
        grade = "PERFECT"
        grade_title = "👑 ULTRA PERFECT (초신급 적중!)"
        grade_desc = f"정확히 {sec_in_cycle:.3f}초 적중! 실전 카카오뱅크 서버에 약 +{int(simulated_arrive_diff_ms)}ms로 최우선 1등 안착합니다!"
        badge_color = "#ffb300"
    elif golden_start <= sec_in_cycle <= golden_end:
        grade = "PERFECT"
        grade_title = "🔥 GOLDEN ZONE (골든존 통과!)"
        grade_desc = f"골든존 {sec_in_cycle:.3f}초 적중! 실전 서버 도착 +{int(simulated_arrive_diff_ms)}ms로 매우 안정적인 성공권입니다."
        badge_color = "#20c997"
    elif danger_start <= sec_in_cycle < golden_start:
        grade = "DANGER"
        grade_title = "⚠️ 튕김 위험 구간 (조기 클릭)"
        grade_desc = f"{sec_in_cycle:.3f}초로 살짝 빨랐습니다! 회선이 빠르면 59.95s에 도착해 '신청 가능 시간이 아닙니다'로 튕깁니다."
        badge_color = "#ff4757"
    elif sec_in_cycle < danger_start:
        grade = "EARLY"
        grade_title = "❌ 조기 탈락 (100% 컷오프)"
        grade_desc = f"{sec_in_cycle:.3f}초는 너무 빠릅니다. 카운트다운 신호등을 보고 0.0초에 누르세요!"
        badge_color = "#ff3838"
    elif golden_end < sec_in_cycle <= safe_end:
        grade = "SAFE"
        grade_title = "✅ SAFE (안정 통과권)"
        grade_desc = f"{sec_in_cycle:.3f}초에 터치 (+{int(simulated_arrive_diff_ms)}ms). 통과는 되나 골든존보다는 선착순 순번이 살짝 밀릴 수 있습니다."
        badge_color = "#00d2d3"
    else:
        grade = "LATE"
        grade_title = "🐢 LATE (지각 구간)"
        grade_desc = "이미 앞선 트래픽이 큐를 채워 게이트웨이 병목 마감 위험이 큽니다."
        badge_color = "#ffa502"

    formatted_touch_time = touch_dt.strftime("%H:%M:%S") + f".{int(ms_float):03d}"
    sec_display = f"{int(sec_in_cycle):02d}.{int(ms_float):03d}초"

    return {
        "touch_time_str": formatted_touch_time,
        "sec_in_cycle": sec_display,
        "sec_float": round(sec_in_cycle, 3),
        "golden_start": golden_start,
        "golden_end": golden_end,
        "golden_center": golden_center,
        "diff_from_center": diff_from_center,
        "simulated_arrive_diff_ms": simulated_arrive_diff_ms,
        "grade": grade,
        "grade_title": grade_title,
        "grade_desc": grade_desc,
        "badge_color": badge_color,
        "interval": interval,
    }


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h3>index.html을 찾을 수 없습니다.</h3>", status_code=404)


def find_available_port(preferred_port: int = 8080) -> int:
    for port in [preferred_port, 8000, 8888, 8008, 9000]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(('0.0.0.0', port))
                return port
        except OSError:
            continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('0.0.0.0', 0))
        return s.getsockname()[1]


import subprocess
import json

def get_tailscale_info(target_port: int = 8080) -> dict:
    """Tailscale IP 및 MagicDNS 도메인 자동 조회"""
    info = {"ip": None, "domain": None, "https_port": "8443"}
    try:
        res = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=1)
        if res.returncode == 0 and res.stdout.strip():
            info["ip"] = res.stdout.strip()
    except Exception:
        pass

    try:
        res2 = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=1)
        if res2.returncode == 0:
            data = json.loads(res2.stdout)
            self_node = data.get("Self", {})
            dns_name = self_node.get("DNSName", "").rstrip(".")
            if dns_name:
                info["domain"] = dns_name
    except Exception:
        pass

    try:
        res3 = subprocess.run(["tailscale", "serve", "status", "--json"], capture_output=True, text=True, timeout=1)
        if res3.returncode == 0:
            sdata = json.loads(res3.stdout)
            for hostport, val in sdata.get("Web", {}).items():
                for _, h_info in val.get("Handlers", {}).items():
                    proxy = h_info.get("Proxy", "")
                    if f":{target_port}" in proxy:
                        if ":" in hostport:
                            info["https_port"] = hostport.split(":")[-1]
                        else:
                            info["https_port"] = "443"
    except Exception:
        pass
    return info


if __name__ == "__main__":
    local_ip = get_local_ip()
    port = find_available_port(8080)
    ts_info = get_tailscale_info(port)
    
    print("\n" + "=" * 68)
    print("🚀 [kakaobank.com 오픈런 타이밍 트레이너 가동]")
    print("=" * 68)
    print(f"🖥️  PC 로컬 접속:       http://localhost:{port}")
    print(f"📱 같은 Wi-Fi 모바일:   http://{local_ip}:{port}")
    if ts_info["ip"]:
        print(f"🌐 Tailscale IP (LTE): http://{ts_info['ip']}:{port}")
    if ts_info["domain"]:
        https_p = ts_info.get("https_port", "8443")
        print(f"🔒 Tailscale HTTPS:    https://{ts_info['domain']}:{https_p}")
    print("-" * 68)
    print("💡 [Tailscale 연결 완료]: 스마트폰에서 Tailscale 앱이 켜져 있으면,")
    print("   PC와 같은 Wi-Fi가 아니어도(LTE/5G/외부 어디서든) 위 주소로 바로 접속됩니다!")
    print("=" * 68 + "\n")

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
