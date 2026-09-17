import email.utils
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import uvicorn


DEFAULT_TARGET_URL = "https://www.kakaobank.com"
KST = timezone(timedelta(hours=9))


def get_resource_path(relative_path: str) -> str:
    """PyInstaller 번들(_MEIPASS) 및 일반 개발 환경 모두에서 리소스 경로 탐색"""
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def _is_private_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        return any(
            [
                ip.is_private,
                ip.is_loopback,
                ip.is_link_local,
                ip.is_reserved,
                ip.is_multicast,
                ip.is_unspecified,
            ]
        )
    except ValueError:
        return True


def validate_target_url(raw_url: str) -> str:
    """사용자가 입력한 URL을 검증하고 SSRF 위험이 있는 목적지를 차단한다."""
    value = (raw_url or "").strip()
    if not value:
        raise ValueError("측정할 URL을 입력하세요.")
    if not re_has_scheme(value):
        value = f"https://{value}"

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("http 또는 https URL만 사용할 수 있습니다.")
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("사용자 정보가 포함되지 않은 공개 URL을 입력하세요.")

    hostname = parsed.hostname.rstrip(".").lower()
    blocked_names = {"localhost", "localhost.localdomain", "metadata.google.internal"}
    if hostname in blocked_names or hostname.endswith(".local") or hostname.endswith(".internal"):
        raise ValueError("로컬 또는 내부 네트워크 주소는 사용할 수 없습니다.")

    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("URL의 호스트를 찾을 수 없습니다.") from exc
    if not addresses or any(_is_private_address(address) for address in addresses):
        raise ValueError("공개 서버 주소만 사용할 수 있습니다.")

    normalized = parsed._replace(fragment="").geturl()
    return normalized


def re_has_scheme(value: str) -> bool:
    return bool(urllib.parse.urlparse(value).scheme)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def http_error_301(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


class TargetClock:
    """공개 웹사이트의 Date 헤더를 기준으로 서버 시각을 추정하는 시계."""

    def __init__(self, target_url: str):
        self.target_url = target_url
        self.offset_ms = 0.0
        self.last_rtt_ms = 0.0
        self.last_sync_time = 0.0
        self.sync_count = 0
        self.is_syncing = False
        self.status = "초기화 대기 중"
        self.lock = threading.Lock()

    def _request_date(self, method: str = "HEAD") -> str:
        request = urllib.request.Request(self.target_url, method=method)
        request.add_header("User-Agent", "TimingWorkspace/1.0 (+https://github.com/Glory-yk/kakaobank-timing-trainer)")
        request.add_header("Cache-Control", "no-cache")
        if method == "GET":
            request.add_header("Range", "bytes=0-0")
        opener = urllib.request.build_opener(
            NoRedirectHandler,
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        with opener.open(request, timeout=3) as response:
            date_value = response.headers.get("Date")
            if not date_value:
                raise ValueError("응답에 HTTP Date 헤더가 없습니다.")
            return date_value

    def sync(self) -> bool:
        with self.lock:
            if self.is_syncing:
                return False
            self.is_syncing = True

        try:
            try:
                initial_method = "HEAD"
                initial_date = self._request_date(initial_method)
            except Exception:
                initial_method = "GET"
                initial_date = self._request_date(initial_method)

            initial_epoch = email.utils.parsedate_to_datetime(initial_date).timestamp()
            detected = False
            for _ in range(25):
                time.sleep(0.04)
                t_send = time.time()
                try:
                    current_date = self._request_date(initial_method)
                except Exception:
                    current_date = self._request_date("GET")
                t_recv = time.time()
                current_epoch = email.utils.parsedate_to_datetime(current_date).timestamp()

                if current_epoch > initial_epoch:
                    rtt_ms = (t_recv - t_send) * 1000.0
                    midpoint_ms = (t_recv - (rtt_ms / 2000.0)) * 1000.0
                    self.offset_ms = (current_epoch * 1000.0) - midpoint_ms
                    self.last_rtt_ms = round(rtt_ms, 1)
                    self.last_sync_time = time.time()
                    self.sync_count += 1
                    self.status = "동기화 완료 (초 경계 추정)"
                    detected = True
                    break

            if not detected:
                approx_server_ms = (initial_epoch + 0.5) * 1000.0
                self.offset_ms = approx_server_ms - (time.time() * 1000.0)
                self.last_rtt_ms = 0.0
                self.last_sync_time = time.time()
                self.sync_count += 1
                self.status = "동기화 완료 (1초 단위 추정)"
            return True
        except Exception as exc:
            self.status = f"동기화 오류: {exc}"
            return False
        finally:
            with self.lock:
                self.is_syncing = False

    def get_time_ms(self) -> float:
        return (time.time() * 1000.0) + self.offset_ms

    def get_datetime(self, timestamp_ms: Optional[float] = None) -> datetime:
        epoch_sec = (timestamp_ms if timestamp_ms is not None else self.get_time_ms()) / 1000.0
        return datetime.fromtimestamp(epoch_sec, tz=KST)


app = FastAPI(title="Timing Workspace")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

clocks: Dict[str, TargetClock] = {}
clocks_lock = threading.Lock()


def get_clock(raw_url: str) -> TargetClock:
    try:
        target_url = validate_target_url(raw_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with clocks_lock:
        clock = clocks.get(target_url)
        if clock is None:
            if len(clocks) >= 20:
                oldest_url = min(clocks, key=lambda key: clocks[key].last_sync_time)
                clocks.pop(oldest_url, None)
            clock = TargetClock(target_url)
            clocks[target_url] = clock

    if not clock.last_sync_time or time.time() - clock.last_sync_time > 12:
        synced = clock.sync()
        if not synced and not clock.last_sync_time:
            raise HTTPException(status_code=502, detail=clock.status)
    return clock


def clock_payload(clock: TargetClock, client_send: Optional[float] = None) -> dict:
    ago = round(time.time() - clock.last_sync_time, 1) if clock.last_sync_time else 999
    return {
        "client_send": client_send,
        "server_time": clock.get_time_ms(),
        "target_url": clock.target_url,
        "target_host": urllib.parse.urlparse(clock.target_url).netloc,
        "server_offset_ms": round(clock.offset_ms, 1),
        "server_rtt_ms": clock.last_rtt_ms,
        "sync_status": clock.status,
        "last_sync_ago_sec": ago,
    }


@app.get("/api/time-sync")
async def time_sync(
    url: str = Query(DEFAULT_TARGET_URL, description="서버시간을 확인할 공개 웹사이트 URL"),
    client_send: Optional[float] = Query(None),
):
    clock = get_clock(url)
    return clock_payload(clock, client_send)


@app.post("/api/resync")
async def resync(url: str = Query(DEFAULT_TARGET_URL)):
    clock = get_clock(url)
    success = clock.sync()
    return {
        "success": success,
        "status": clock.status,
        "target_url": clock.target_url,
        "offset_ms": round(clock.offset_ms, 1),
        "rtt_ms": clock.last_rtt_ms,
    }


@app.post("/api/click")
async def register_click(
    url: str = Query(DEFAULT_TARGET_URL),
    interval: int = Query(10, description="목표 주기(초): 5, 10, 60"),
    client_touch_ms: Optional[float] = Query(None, description="클라이언트의 동기화 서버 시각 기준 터치 시각"),
):
    if interval not in {5, 10, 60}:
        raise HTTPException(status_code=400, detail="주기는 5, 10, 60초 중 하나여야 합니다.")

    clock = get_clock(url)
    touch_epoch_ms = client_touch_ms if client_touch_ms is not None else clock.get_time_ms() - 10.0
    touch_dt = clock.get_datetime(touch_epoch_ms)
    ms_float = touch_dt.microsecond / 1000.0
    sec_in_cycle = (touch_dt.second % interval) + (ms_float / 1000.0)

    golden_start = interval - 0.600
    golden_end = interval - 0.380
    golden_center = interval - 0.500
    ultra_start = interval - 0.540
    ultra_end = interval - 0.460
    danger_start = interval - 0.750
    safe_end = interval - 0.250

    diff_from_center = round((sec_in_cycle - golden_center) * 1000.0, 1)
    if diff_from_center > interval * 500.0:
        diff_from_center -= interval * 1000.0
    elif diff_from_center < -(interval * 500.0):
        diff_from_center += interval * 1000.0

    simulated_arrive_diff_ms = round(diff_from_center + 90.0, 1)
    if ultra_start <= sec_in_cycle <= ultra_end:
        grade, grade_title, badge_color = "PERFECT", "👑 ULTRA PERFECT (초정밀 적중!)", "#ffb300"
        grade_desc = f"정확히 {sec_in_cycle:.3f}초 적중! 목표 서버에 약 +{int(simulated_arrive_diff_ms)}ms로 도착하는 타이밍입니다."
    elif golden_start <= sec_in_cycle <= golden_end:
        grade, grade_title, badge_color = "PERFECT", "🔥 GOLDEN ZONE (골든존 통과!)", "#20c997"
        grade_desc = f"골든존 {sec_in_cycle:.3f}초 적중! 목표 서버 도착 +{int(simulated_arrive_diff_ms)}ms로 안정적인 성공권입니다."
    elif danger_start <= sec_in_cycle < golden_start:
        grade, grade_title, badge_color = "DANGER", "⚠️ 튕김 위험 구간 (조기 클릭)", "#ff4757"
        grade_desc = f"{sec_in_cycle:.3f}초로 조금 빨랐습니다. 실제 서비스의 컷오프 시각 전에 도착할 수 있습니다."
    elif sec_in_cycle < danger_start:
        grade, grade_title, badge_color = "EARLY", "❌ 조기 탈락 (컷오프 이전)", "#ff3838"
        grade_desc = f"{sec_in_cycle:.3f}초는 너무 빠릅니다. 목표 시각까지 기다리세요."
    elif golden_end < sec_in_cycle <= safe_end:
        grade, grade_title, badge_color = "SAFE", "✅ SAFE (안정 통과권)", "#00d2d3"
        grade_desc = f"{sec_in_cycle:.3f}초에 터치했습니다. 통과권이지만 골든존보다 순번이 밀릴 수 있습니다."
    else:
        grade, grade_title, badge_color = "LATE", "🐢 LATE (지각 구간)", "#ffa502"
        grade_desc = "이미 앞선 요청이 큐를 채웠을 수 있는 지각 구간입니다."

    return {
        "touch_time_str": touch_dt.strftime("%H:%M:%S") + f".{int(ms_float):03d}",
        "sec_in_cycle": f"{int(sec_in_cycle):02d}.{int(ms_float):03d}초",
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
        "target_host": urllib.parse.urlparse(clock.target_url).netloc,
    }


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = get_resource_path("index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as file:
            return HTMLResponse(content=file.read())
    return HTMLResponse("<h3>index.html을 찾을 수 없습니다.</h3>", status_code=404)


def find_available_port(preferred_port: int = 8080) -> int:
    for port in [preferred_port, 8000, 8888, 8008, 9000]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0", port))
                return port
        except OSError:
            continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def get_local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_tailscale_info(target_port: int = 8080) -> dict:
    """Tailscale IP 및 MagicDNS 도메인 자동 조회"""
    info = {"ip": None, "domain": None, "https_port": "8443"}
    try:
        result = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=1)
        if result.returncode == 0 and result.stdout.strip():
            info["ip"] = result.stdout.strip()
    except Exception:
        pass
    try:
        result = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=1)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            dns_name = data.get("Self", {}).get("DNSName", "").rstrip(".")
            if dns_name:
                info["domain"] = dns_name
    except Exception:
        pass
    try:
        result = subprocess.run(["tailscale", "serve", "status", "--json"], capture_output=True, text=True, timeout=1)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for hostport, value in data.get("Web", {}).items():
                for handler in value.get("Handlers", {}).values():
                    if f":{target_port}" in handler.get("Proxy", ""):
                        info["https_port"] = hostport.split(":")[-1] if ":" in hostport else "443"
    except Exception:
        pass
    return info


if __name__ == "__main__":
    local_ip = get_local_ip()
    port = find_available_port(8080)
    ts_info = get_tailscale_info(port)

    print("\n" + "=" * 68)
    print("🚀 [Timing Workspace 서버시간 타이머 가동]")
    print("=" * 68)
    print(f"🖥️  PC 로컬 접속:       http://localhost:{port}")
    print(f"📱 같은 Wi-Fi 모바일:   http://{local_ip}:{port}")
    if ts_info["ip"]:
        print(f"🌐 Tailscale IP (LTE): http://{ts_info['ip']}:{port}")
    if ts_info["domain"]:
        print(f"🔒 Tailscale HTTPS:    https://{ts_info['domain']}:{ts_info['https_port']}")
    print("=" * 68 + "\n")

    def auto_open_browser():
        time.sleep(1.0)
        try:
            webbrowser.open(f"http://localhost:{port}")
        except Exception:
            pass

    threading.Thread(target=auto_open_browser, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
