import os
import json
import base64
import socket
import queue
import sys
import subprocess
import threading
import time
import ctypes
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from collections import deque
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_POINTS = 60


def _load_compressor_cfg():
    """config.json 'compressor' 섹션 읽기 (헤드리스 서버와 공유 설정)."""
    try:
        fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        with open(fp, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        c = cfg.get('compressor')
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _save_compressor_cfg(c):
    """config.json 'compressor' 섹션 저장 (나머지 키 보존)."""
    try:
        fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        cfg = {}
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            pass
        cfg['compressor'] = dict(c or {})
        tmp = fp + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, fp)   # 원자적 교체 — 쓰기 중 크래시 시 기존 파일 보존
    except Exception:
        pass

def fmt_tokens(n):
    """토큰 수를 K/M 단위로 포맷 (131072 -> '128.0K')."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return '--'
    if n >= 1_000_000:
        return f'{n / 1_000_000:.1f}M'
    if n >= 1000:
        return f'{n / 1000:.1f}K'
    return f'{int(n)}'

def get_system_memory_percent():
    if os.name == 'nt':
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return float(stat.dwMemoryLoad)
        except Exception:
            pass
    else:
        try:
            meminfo = {}
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if ':' in line:
                        k, v = line.split(':', 1)
                        parts = v.split()
                        if parts:
                            meminfo[k.strip()] = int(parts[0])
            total = meminfo.get('MemTotal', 0)
            avail = meminfo.get('MemAvailable', meminfo.get('MemFree', 0))
            if total > 0:
                return 100.0 * (total - avail) / total
        except Exception:
            pass
    return None

def get_nvidia_metrics():
    try:
        cmd = ['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total', '--format=csv,noheader,nounits']
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True, timeout=3)
        gpu_util = 0.0
        mem_used = 0.0
        mem_total = 0.0
        found = False
        for line in out.strip().splitlines():
            line = line.strip()
            if not line: continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) < 3: continue
            try:
                u = float(parts[0]); mu = float(parts[1]); mt = float(parts[2])
            except ValueError:
                continue
            found = True
            gpu_util = max(gpu_util, u)
            mem_used += mu
            mem_total += mt
        if not found or mem_total <= 0: return None
        return gpu_util, 100.0 * mem_used / mem_total, mem_used, mem_total
    except Exception:
        return None

def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith('127.'):
            return ip
    except Exception:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith('127.'):
            return ip
    except Exception:
        pass
    return '127.0.0.1'

def get_fresh_env():
    """자식 프로세스용 환경 반환 (Windows: 레지스트리의 최신 PATH를 반영)."""
    env = os.environ.copy()
    if os.name == 'nt':
        try:
            import winreg
            def _path_from(root, subkey, name):
                try:
                    with winreg.OpenKey(root, subkey) as k:
                        return winreg.QueryValueEx(k, name)[0] or ''
                except OSError:
                    return ''
            machine = _path_from(winreg.HKEY_LOCAL_MACHINE,
                                 r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'Path')
            user = _path_from(winreg.HKEY_CURRENT_USER, 'Environment', 'Path')
            merged, seen = [], set()
            for p in (machine + os.pathsep + user).split(os.pathsep):
                if p and p.lower() not in seen:
                    seen.add(p.lower())
                    merged.append(p)
            env['Path'] = os.pathsep.join(merged)
        except Exception:
            pass
    return env

class GPUMonitorCanvas(tk.Canvas):
    def __init__(self, master, **kwargs):
        super().__init__(master, width=150, height=120, bg='#0f141a', **kwargs)
        self.history = {'GPU': deque(maxlen=MAX_POINTS), 'GPU RAM': deque(maxlen=MAX_POINTS),
                        'System RAM': deque(maxlen=MAX_POINTS), 'Context': deque(maxlen=MAX_POINTS)}
        self.current = {'GPU': '--', 'GPU RAM': '--', 'System RAM': '--', 'Context': '--'}
        # 컨텍스트 사용량 (llama-server /slots 기준) — 오른쪽 축은 ctx_max를 최대치로 사용
        self.ctx_used = 0.0
        self.ctx_max = 131072.0
        self._after_id = None
        self._tick()

    def update_context(self, used, max_ctx):
        """llama-server의 현재 컨텍스트 점유량/최대 컨텍스트를 갱신."""
        try:
            self.ctx_used = max(0.0, float(used or 0))
        except (TypeError, ValueError):
            self.ctx_used = 0.0
        try:
            if max_ctx and float(max_ctx) > 0:
                self.ctx_max = float(max_ctx)
        except (TypeError, ValueError):
            pass

    def _tick(self):
        if not self.winfo_exists(): return
        self._collect()
        self._draw()
        self._after_id = self.after(1000, self._tick)

    def _collect(self):
        nvidia = get_nvidia_metrics()
        sys_mem = get_system_memory_percent()
        if nvidia is not None:
            gpu_util, mem_pct, used, total = nvidia
            gpu_val = float(gpu_util)
            mem_val = float(mem_pct)
            self.current['GPU'] = f'{gpu_val:.0f}%'
            self.current['GPU RAM'] = f'{mem_val:.0f}%'
        else:
            gpu_val = 0.0
            mem_val = 0.0
            self.current['GPU'] = '--'
            self.current['GPU RAM'] = '--'
        if sys_mem is None:
            sys_val = 0.0
            self.current['System RAM'] = '--'
        else:
            sys_val = float(sys_mem)
            self.current['System RAM'] = f'{sys_val:.0f}%'
        self.history['GPU'].append(gpu_val)
        self.history['GPU RAM'].append(mem_val)
        self.history['System RAM'].append(sys_val)
        self.history['Context'].append(min(self.ctx_used, self.ctx_max))
        self.current['Context'] = f'{fmt_tokens(self.ctx_used)}/{fmt_tokens(self.ctx_max)}'

    def _draw(self):
        self.delete('all')
        # 실제 표시 크기(winfo) 사용 — 요청 크기(cget)는 레이아웃이 캔버스보다 클 때 그래프가 채워지지 않음
        w = int(self.winfo_width())
        h = int(self.winfo_height())
        if w < 10 or h < 10: return
        colors = {'GPU': '#2f80ff', 'GPU RAM': '#33c17a', 'System RAM': '#f2a33c', 'Context': '#c792ea'}
        pad_l, pad_r, pad_t, pad_b = 44, 46, 26, 30
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b
        if plot_w <= 0 or plot_h <= 0: return
        # 색상 범례 (좌상단) — 외부 레전드 프레임 대신 캔버스 내부에 그려 좁은 열에도 맞춤
        key_x = pad_l
        for key, label in (('GPU', 'GPU'), ('GPU RAM', 'GPU RAM'), ('System RAM', '시스템'), ('Context', 'Ctx')):
            self.create_line(key_x, 12, key_x + 14, 12, fill=colors[key], width=3)
            self.create_text(key_x + 18, 12, anchor='w', fill='#93a7b8', font=('Consolas', 8), text=label)
            key_x += 18 + len(label) * 7 + 16
        for y in (0, 25, 50, 75, 100):
            yy = pad_t + plot_h - (y / 100.0) * plot_h
            self.create_line(pad_l, yy, w - pad_r, yy, fill='#26313d')
            self.create_text(pad_l - 6, yy, anchor='e', fill='#93a7b8', font=('Consolas', 8), text=str(y))
        # 오른쪽 축: 컨텍스트 사용량 (최대 컨텍스트 용량을 100%로)
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            yy = pad_t + plot_h - frac * plot_h
            self.create_line(w - pad_r, yy, w - pad_r + 3, yy, fill='#4a3f5c')
            self.create_text(w - pad_r + 6, yy, anchor='w', fill='#c792ea', font=('Consolas', 8),
                             text=fmt_tokens(self.ctx_max * frac))
        if len(self.history['GPU']) < 2:
            self.create_text(w / 2, h / 2, fill='#7f93a6', font=('Segoe UI', 10), text='Waiting for data...')
            return
        def x(i):
            return pad_l + (i / (len(self.history['GPU']) - 1)) * plot_w
        def y(v):
            v = max(0.0, min(100.0, float(v)))
            return pad_t + plot_h - (v / 100.0) * plot_h
        def y2(v):
            v = max(0.0, min(self.ctx_max, float(v)))
            return pad_t + plot_h - (v / self.ctx_max) * plot_h
        for key, hist in self.history.items():
            pts = []
            scale = y2 if key == 'Context' else y
            for i, val in enumerate(hist):
                pts.extend([x(i), scale(val)])
            if pts:
                self.create_line(*pts, fill=colors[key], width=2)
        self.create_text(w - pad_r - 6, h - 14, anchor='e', fill='#d7e1ea', font=('Consolas', 8),
                         text=f"GPU {self.current['GPU']} | GPU RAM {self.current['GPU RAM']} | System {self.current['System RAM']} | Ctx {self.current['Context']}")

    def destroy(self):
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
        super().destroy()

DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = '8080'

# ── GGUF 파싱: 모델의 계층 수(블록 수) 읽기 ─────────────────────────────────
# launcher_web의 공통 구현 재사용 (GGUF 바이너리에서 general.architecture + *.block_count 파싱).
def read_gguf_layer_count(path):
    """GGUF 파일에서 모델의 계층 수(block_count)를 읽음. 실패 시 None 반환."""
    if launcher_web is None:
        return None
    return launcher_web.read_gguf_layer_count(path)


# ── 웹 서버 공통 모듈 (헤드리스 서버 llm_server.py와 공유) ──────────────────
# 컨트롤 API + OpenAI 호환 프록시 + PWA 정적 서빙(WebHandler)을 두 애플리케이션이
# 공유하므로 기능 변경/유지보수는 launcher_web.py 한 곳에서만 하면 됩니다.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# launcher_web.py가 같은 폴더에 있으면 그 폴더를 사용 (없으면 이전 구조 ../llm_launcher_app)
_WEB_APP_DIR = _THIS_DIR if os.path.isfile(os.path.join(_THIS_DIR, 'launcher_web.py')) \
    else os.path.normpath(os.path.join(_THIS_DIR, os.pardir, 'llm_launcher_app'))
if _WEB_APP_DIR not in sys.path:
    sys.path.insert(0, _WEB_APP_DIR)
try:
    import launcher_web
except Exception:
    launcher_web = None
WEB_UI_DIR = os.path.join(_WEB_APP_DIR, 'web')
DEFAULT_MODELS_DIR = os.path.expanduser('~/.lmstudio/models')


def _models_dir_default():
    """모델 탐색 기본 디렉터리 — 웹 모델 탭과 동일한 기준:
    config.json의 'models_dir' (설정 없으면 폴백 ~/.lmstudio/models)."""
    try:
        fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        with open(fp, 'r', encoding='utf-8') as f:
            d = str(json.load(f).get('models_dir') or '').strip()
    except Exception:
        d = ''
    if not d:
        d = DEFAULT_MODELS_DIR
    return d if os.path.isdir(d) else DEFAULT_MODELS_DIR


def _mime_for(path):
    ext = os.path.splitext(path)[1].lower()
    table = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
        '.webp': 'image/webp', '.bmp': 'image/bmp', '.gif': 'image/gif',
        '.mp4': 'video/mp4', '.avi': 'video/x-msvideo', '.mkv': 'video/x-matroska',
        '.mov': 'video/quicktime', '.webm': 'video/webm',
    }
    return table.get(ext, 'application/octet-stream')

def find_ffprobe():
    """레지스트리 최신 PATH 기준 ffprobe.exe 경로 반환 (없으면 None)."""
    import shutil
    env = get_fresh_env() if os.name == 'nt' else os.environ
    return shutil.which('ffprobe', path=env.get('Path'))

def probe_video_info(path):
    """ffprobe로 영상 정보 조회. (duration_sec, fps) 반환, 실패 시 None."""
    exe = find_ffprobe()
    if not exe:
        return None
    try:
        out = subprocess.check_output(
            [exe, '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=r_frame_rate,duration:format=duration',
             '-of', 'default=noprint_wrappers=1', path],
            stderr=subprocess.DEVNULL, timeout=30)
        dur = fps = None
        for line in out.decode('utf-8', 'replace').splitlines():
            if '=' not in line:
                continue
            k, v = line.split('=', 1)
            k, v = k.strip(), v.strip()
            try:
                if k == 'duration' and dur is None and v not in ('N/A', ''):
                    dur = float(v)
                elif k == 'r_frame_rate':
                    num, _, den = v.partition('/')
                    d = float(den or 1)
                    fps = (float(num) / d) if d else None
            except ValueError:
                continue
        return (dur, fps)
    except Exception:
        return None

def _find_free_port():
    """127.0.0.1에서 일시적으로 할당되는 자유 포트 반환 (백엔드 llama-server용)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()

class _ApiInspectProxyHandler(BaseHTTPRequestHandler):
    """API 인스펙션용 리버스 프록시 핸들러.

    모든 요청을 백엔드 llama-server로 그대로 전달하면서, 생성 엔드포인트
    (/v1/chat/completions, /v1/completions)의 reasoning_effort 값을 런처 로그에
    남긴다. 외부 클라이언트가 서버 API를 직접 호출한 경우에도 어떤 노력도 값으로
    요청됐는지 확인할 수 있다.
    """
    protocol_version = 'HTTP/1.1'
    launcher = None            # App 인스턴스 백레퍼런스 (로그 출력용)
    backend_host = '127.0.0.1'
    backend_port = 0
    _GEN_PATHS = ('/v1/chat/completions', '/v1/completions', '/completions')

    def log_message(self, fmt, *args):
        pass  # http.server 기본 접근 로그 억제

    # ── 요청 본문 읽기 ──
    def _read_body(self):
        te = (self.headers.get('Transfer-Encoding') or '').lower()
        if 'chunked' in te:
            return self._read_chunked_body()
        try:
            cl = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            cl = 0
        return self.rfile.read(cl) if cl > 0 else b''

    def _read_chunked_body(self):
        data = b''
        while True:
            line = self.rfile.readline(65538).strip()
            if not line:
                break
            try:
                size = int(line.split(b';')[0], 16)
            except ValueError:
                break
            if size == 0:
                self.rfile.readline()  # 마지막 CRLF
                break
            remaining = size
            while remaining > 0:
                block = self.rfile.read(remaining)
                if not block:
                    return data
                data += block
                remaining -= len(block)
            self.rfile.readline()  # 청크 종료 CRLF
        return data

    # ── reasoning_effort 로깅 ──
    def _log_reasoning_effort(self, body):
        try:
            data = json.loads(body.decode('utf-8'))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        effort = data.get('reasoning_effort')
        if effort in (None, ''):
            kw = data.get('chat_template_kwargs')
            if isinstance(kw, dict):
                effort = kw.get('reasoning_effort')
        client = f'{self.client_address[0]}:{self.client_address[1]}'
        temp = data.get('temperature')
        top_p = data.get('top_p')
        self.launcher.log(
            f'[API 수신] {client} {self.path} model={data.get("model", "?")} '
            f'stream={data.get("stream", False)} reasoning_effort={effort or "미설정"}'
            + (f' temperature={temp:g}' if isinstance(temp, (int, float)) else '')
            + (f' top_p={top_p:g}' if isinstance(top_p, (int, float)) else ''))

    # ── 프록시 포워딩 ──
    def _forward(self):
        # 생성 요청: 진행 상태 추적. 자동 언로드(TTL)가 켜져 있으면 어떤 요청이든
        # llama-server의 유휴 타이머를 리셋(수면 중이면 모델이 즉시 재로드)하므로,
        # 런처의 통계 폴링은 생성 요청 진행 중/직후에만 허용된다.
        path = self.path.split('?', 1)[0]
        is_gen = any(path == p or path.startswith(p + '/') for p in self._GEN_PATHS)
        launcher = self.launcher
        if is_gen and launcher is not None:
            launcher._gen_request_started()
        try:
            self._forward_impl()
        finally:
            if is_gen and launcher is not None:
                launcher._gen_request_finished()

    def _forward_impl(self):
        path = self.path.split('?', 1)[0]
        body = self._read_body()
        if any(path == p or path.startswith(p + '/') for p in self._GEN_PATHS):
            self._log_reasoning_effort(body)

        headers = {}
        for k, v in self.headers.items():
            if k.lower() in ('host', 'transfer-encoding', 'connection',
                             'content-length', 'keep-alive', 'proxy-connection'):
                continue
            headers[k] = v
        if body:
            headers['Content-Length'] = str(len(body))

        try:
            conn = http.client.HTTPConnection(self.backend_host, self.backend_port, timeout=3600)
            conn.request(self.command, self.path, body=(body if body else None), headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            # send_error의 reason phrase는 ASCII만 허용(한글 OS 오류 포함 시
            # UnicodeEncodeError) → 상세 오류는 런처 로그로, 상태행은 순수 ASCII
            self.launcher.log(f'[API 수신] {self.client_address[0]}:{self.client_address[1]} '
                              f'{self.path} 포워딩 실패: {e}')
            try:
                self.send_error(502, 'backend unreachable')
            except Exception:
                pass
            return

        try:
            self.send_response(resp.status)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return
        resp_te = (resp.getheader('Transfer-Encoding') or '').lower()
        resp_cl = resp.getheader('Content-Length')
        for k, v in resp.getheaders():
            if k.lower() in ('transfer-encoding', 'connection', 'content-length', 'date', 'keep-alive'):
                continue
            self.send_header(k, v)
        chunked = 'chunked' in resp_te
        if chunked:
            self.send_header('Transfer-Encoding', 'chunked')
        elif resp_cl is not None:
            self.send_header('Content-Length', resp_cl)
        self.send_header('Connection', 'close')
        self.end_headers()

        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(b'%x\r\n' % len(chunk))
                    self.wfile.write(chunk)
                    self.wfile.write(b'\r\n')
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_DELETE = _forward
    do_PATCH = _forward
    do_HEAD = _forward
    do_OPTIONS = _forward

class GuiWebNode:
    """GUI App을 launcher_web.WebHandler의 노드 인터페이스로 노출하는 어댑터.

    WebHandler는 HTTP 워커 스레드에서 노드 메서드를 호출하므로,
    Tk 변수 읽기는 1초 주기의 _refresh() 캐시를 통해 메인 스레드에서만 수행한다.
    launch()/stop()은 root.after()로 메인 스레드에 위임한다.
    """

    def __init__(self, app):
        self.app = app
        self.logs = launcher_web.LogBuffer()
        self._backend_port = 0
        self._port = int(DEFAULT_PORT)
        self._cache = {}
        self._refresh()
        app.root.after(1000, self._refresh_loop)

    def _refresh_loop(self):
        try:
            self._refresh()
            self.app.root.after(1000, self._refresh_loop)
        except Exception:
            pass

    # ── 로깅 (Handler 워커 스레드 → GUI 로그 큐) ──
    def log(self, msg):
        self.app.log(str(msg))

    # ── WebHandler 인터페이스 ──
    @property
    def backend_port(self):
        return self._backend_port

    def set_backend_port(self, p):
        self._backend_port = int(p or 0)

    def process_running(self):
        a = self.app
        return a.server_mode_active and a.proc is not None and a.proc.poll() is None

    @property
    def model_state(self):
        return self.app.model_state

    def gen_request_started(self):
        self.app._gen_request_started()

    def gen_request_finished(self):
        self.app._gen_request_finished()

    # ── 메인 스레드 상태 캐시 (Tk 변수 읽기) ──
    def _refresh(self):
        a = self.app
        running = a.server_mode_active and a.proc is not None and a.proc.poll() is None
        model = a.model_var.get().strip()
        c = {
            'running': running,
            'model_state': a.model_state if running else None,
            'model_path': model if running else '',
            'server_exe': a.server_var.get().strip(),
            'mmproj': a.mmproj_var.get().strip(),
            'ngl': a.ngl_var.get().strip() or '999',
            'ctx': a.ctx_var.get().strip() or '128K',
            'fa': bool(a.fa_on_var.get()),
            'ctk': a.ctk_var.get().strip() or 'q8_0',
            'ctv': a.ctv_var.get().strip() or 'q8_0',
            'np': a.np_var.get().strip() or '1',
            'mtp': bool(a.mtp_on_var.get()),
            'vision': bool(a.vision_var.get()),
            'ttl_min': (a._ttl_at_start or 0) if running else 0,
            'ctx_max': a._ctx_to_int(),
            'ctx_used': getattr(a, '_last_ctx_used', 0) if running else 0,
            'tokens_in': getattr(a, '_last_tokens_in', None) if running else None,
            'tokens_out': getattr(a, '_last_tokens_out', None) if running else None,
            'compressor': a._compressor_cfg(),
        }
        c['last_launch'] = (None if not running else {
            'model': model, 'mmproj': c['mmproj'], 'ngl': c['ngl'], 'ctx': c['ctx'],
            'fa': c['fa'], 'ctk': c['ctk'], 'ctv': c['ctv'], 'np': c['np'],
            'mtp': c['mtp'], 'vision': c['vision'], 'ttl_min': c['ttl_min'],
        })
        self._cache = c

    def status(self):
        c = dict(self._cache)
        running = c['running']
        gpu = get_nvidia_metrics()
        ram = get_system_memory_percent()
        return {
            'process': 'running' if running else 'stopped',
            'model_state': c['model_state'],
            'model': os.path.basename(c['model_path']) if (running and c['model_path']) else None,
            'model_path': c['model_path'] or None,
            'ttl_min': c['ttl_min'] or None,
            'gpu': ({'util': gpu[0], 'mem_pct': gpu[1],
                     'mem_used_mb': gpu[2], 'mem_total_mb': gpu[3]} if gpu else None),
            'sys_ram': ram,
            'context': {'used': c['ctx_used'], 'max': c['ctx_max'] if running else None},
            'tokens': {'input': c['tokens_in'], 'output': c['tokens_out']},
            'last_launch': c['last_launch'],
            'host': DEFAULT_HOST,
            'port': self._port,
            'server_exe': c['server_exe'],
            'compressor': c.get('compressor') or {'enabled': True, 'auto_trigger_pct': 75, 'keep_last_msgs': 6},
        }

    def launch(self, lc):
        """웹 UI 기동 요청 → 옵션을 GUI에 반영(메인 스레드) 후 start_process()."""
        a = self.app
        lc = lc or {}

        def _do():
            if not a.mode_var.get().startswith('서버'):
                a.mode_var.set('서버(네트워크)')
            if lc.get('model'):
                a.model_var.set(str(lc['model']))
            if lc.get('server_exe'):
                a.server_var.set(str(lc['server_exe']))
            if lc.get('ngl'):
                a.ngl_var.set(str(lc['ngl']))
            if lc.get('ctx'):
                a.ctx_var.set(str(lc['ctx']))
            if 'fa' in lc:
                a.fa_on_var.set(bool(lc['fa']))
            if lc.get('ctk'):
                a.ctk_var.set(str(lc['ctk']))
            if lc.get('ctv'):
                a.ctv_var.set(str(lc['ctv']))
            if lc.get('np'):
                a.np_var.set(str(lc['np']))
            if 'mtp' in lc:
                a.mtp_on_var.set(bool(lc['mtp']))
            if 'mtp_max' in lc:
                a.mtp_max_var.set(str(lc.get('mtp_max') or 0))
            if lc.get('mmproj'):
                a.mmproj_var.set(str(lc['mmproj']))
            if lc.get('n'):
                a.n_var.set(str(lc['n']))
            if 'vision' in lc:
                a.vision_var.set(bool(lc['vision']))
            if lc.get('ttl_min') is not None:
                try:
                    a.ttl_var.set(max(0, min(999, int(lc['ttl_min']))))
                except (TypeError, ValueError):
                    pass
            a.start_process()

        try:
            a.root.after(0, _do)
        except Exception as e:
            return False, str(e)
        return True, None

    def stop(self):
        try:
            self.app.root.after(0, self.app.stop_process)
        except Exception as e:
            return False, str(e)
        return True, None

    def compress_chat(self, messages, keep_last=6):
        """웹 /api/compress → App.compress_chat 위임 (로컬 API 경유)."""
        return self.app.compress_chat(messages, keep_last=keep_last)

    def save_config(self, cfg):
        """웹 /api/compressor POST → config.json 영속화 + GUI 변수 반영."""
        c = (cfg or {}).get('compressor') or {}
        _save_compressor_cfg(c)
        try:
            self.app.root.after(0, lambda: self.app._apply_compressor_cfg(c))
        except Exception:
            pass

class App:
    def __init__(self, root):
        self.root = root
        root.title('LLM Launcher - llama.cpp GUI')
        root.geometry('1240x820')
        root.minsize(980, 640)
        self.proc = None
        self._job = _create_kill_on_close_job()  # Job Object: GUI 종료 시 자식 llama 프로세스 자동 종료 (고아 방지)
        self._job_announced = False
        self.proxy_server = None        # API 인스펙션 리버스 프록시 (외부 클라이언트 요청 로깅용)
        self.reader_thread = None
        self.log_queue = queue.Queue()
        self.cli_var = tk.StringVar(value='')
        self.server_var = tk.StringVar(value='')
        self.mode_var = tk.StringVar(value='서버(네트워크)')
        self.vision_var = tk.BooleanVar(value=True)
        self.url_var = tk.StringVar(value='')
        self.server_mode_active = False
        self.model_var = tk.StringVar(value='')   # 기본값 없음 — '찾기'로 선택 (기본 탐색 위치: config.json models_dir)
        self.mmproj_var = tk.StringVar(value='')
        self.ngl_var = tk.StringVar(value='999')
        self.ctx_var = tk.StringVar(value='128K')          # 32K~256K 드롭박스 (K 단위 표시)
        self.mtp_on_var = tk.BooleanVar(value=False)      # Draft MTP (스펙추적 디코딩) 체크박스
        self.fa_on_var = tk.BooleanVar(value=True)         # FlashAttn 체크박스
        self.ctk_var = tk.StringVar(value='q8_0')
        self.ctv_var = tk.StringVar(value='q8_0')
        self.np_var = tk.StringVar(value='1')
        self.n_var = tk.StringVar(value='2048')            # 512~4096 드롭박스 (512 단위)
        self.reasoning_var = tk.StringVar(value='medium')  # 추론 노력도 (reasoning_effort): minimal/low/medium/high/xhigh
        self.mtp_max_var = tk.StringVar(value='0')         # MTP max: 스펙추적 디코딩 draft 토큰 상한 (--spec-draft-n-max, 0=미사용)
        self.presets_var = tk.StringVar()          # 선택된 프리셋 이름 (적용/삭제)
        self.preset_name_var = tk.StringVar()      # 저장할 프리셋 이름
        self.status_var = tk.StringVar(value='준비됨')
        self.model_state = None                    # llama-server 모델 상태: 'loading' | 'loaded' | 'sleeping' | None
        self.ttl_var = tk.IntVar(value=10)         # 자동 언로드 TTL(분): 0=해제
        self.ttl_var.trace_add('write', self._on_ttl_change)
        # ── 컨텍스트 컴프레셔 (헤드리스 서버와 공유: config.json 'compressor') ──
        self.compress_auto_var = tk.BooleanVar(value=True)  # 자동 압축 (서버 컨텍스트 비율 초과 시)
        self.compress_pct_var = tk.IntVar(value=75)         # 자동 트리거 비율(%)
        self.compress_keep_var = tk.IntVar(value=6)         # 최근 유지 메시지 수 (요약 제외)
        self._compress_armed = False   # 히스테리시스 플래그 (한 번 트리거 후 재반복 방지)
        self._compressing = False      # 압축 진행 중
        _cc = _load_compressor_cfg()
        if _cc:
            self.compress_auto_var.set(bool(_cc.get('enabled', True)))
            try:
                self.compress_pct_var.set(int(_cc.get('auto_trigger_pct', 75)))
            except (TypeError, ValueError):
                pass
            try:
                self.compress_keep_var.set(int(_cc.get('keep_last_msgs', 6)))
            except (TypeError, ValueError):
                pass
        for _v in (self.compress_auto_var, self.compress_pct_var, self.compress_keep_var):
            _v.trace_add('write', self._on_compressor_change)
        # 채팅 탭 상태 (서버 모드: 로컬 API 호출 / CLI 모드: stdin)
        self.chat_history = []          # OpenAI 형식 메시지 히스토리 (서버 모드)
        self.attached_file = ''         # 첨부된 이미지/영상 파일 경로
        self.attach_var = tk.StringVar(value='')
        self.chat_status_var = tk.StringVar(value='')
        self.chat_mode_var = tk.StringVar(value='')
        self.chat_queue = queue.Queue()
        self.chat_busy = False
        self.token_stats_var = tk.StringVar(value='')   # llama-server 누적 토큰 (입력/출력)
        # 생성 요청 추적 (자동 언로드 TTL): 수면이 활성화된 서버에서는 /slots·/metrics
        # 요청이 전부 서버의 유휴 타이머를 리셋(수면 중이면 즉시 깨어나 재로드)하므로,
        # 통계 폴링은 생성 요청 진행 중/직후에만 수행한다.
        self._gen_lock = threading.Lock()
        self._gen_active = 0          # 진행 중인 생성 요청 수 (프록시 + 로컬 채팅)
        self.last_gen_done = 0.0      # 마지막 생성 요청 완료 시각
        self._ttl_at_start = None     # 서버 기동 시 적용된 --sleep-idle-seconds 값(분)
        self._ttl_warned = None       # 실행 중 서버에 대해 TTL 변경 경고를 낸 값
        # ── 통합 웹 서버(모바일 PWA) 상태 ──
        self.web_server = None       # launcher_web.WebHandler 서버 (앱 실행 내내 상시 기동)
        self._web_node = None        # GuiWebNode (App → WebHandler 어댑터)
        self._last_ctx_used = 0
        self._last_tokens_in = None
        self._last_tokens_out = None
        self._build_ui()
        self._poll_log_queue()
        self._poll_chat_queue()
        self._poll_server_stats()
        self._start_web_server()

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use('vista')
        except tk.TclError:
            pass

        nb = ttk.Notebook(self.root)
        nb.grid(row=0, column=0, sticky='nsew', padx=6, pady=6)
        tab_run = ttk.Frame(nb)
        tab_chat = ttk.Frame(nb)
        nb.add(tab_run, text='  실행 & 설정  ')
        nb.add(tab_chat, text='  채팅  ')

        tab_run.columnconfigure(0, weight=2)   # 왼쪽 2/3 (4번째 줄까지 입력창)
        tab_run.columnconfigure(1, weight=1)   # 오른쪽 1/3 (이용률 그래픽)
        tab_run.rowconfigure(5, weight=3)      # 콘솔

        top = ttk.LabelFrame(tab_run, text='llama.cpp / 모델 / 옵션')
        top.grid(row=0, column=0, sticky='nsew', padx=(6, 3), pady=(6, 3))
        for c in range(1, 8):
            top.columnconfigure(c, weight=1)

        r = 0
        ttk.Label(top, text='llama-cli.exe').grid(row=r, column=0, sticky='w', padx=(6, 3), pady=3)
        ttk.Entry(top, textvariable=self.cli_var).grid(row=r, column=1, columnspan=6, sticky='ew', padx=3, pady=3)
        ttk.Button(top, text='찾기', width=6, command=self.browse_cli).grid(row=r, column=7, sticky='w', padx=(2, 6), pady=3)

        r = 1
        ttk.Label(top, text='llama-server.exe').grid(row=r, column=0, sticky='w', padx=(6, 3), pady=3)
        ttk.Entry(top, textvariable=self.server_var).grid(row=r, column=1, columnspan=6, sticky='ew', padx=3, pady=3)
        ttk.Button(top, text='찾기', width=6, command=self.browse_server).grid(row=r, column=7, sticky='w', padx=(2, 6), pady=3)

        r = 2
        ttk.Label(top, text='모델(.gguf)').grid(row=r, column=0, sticky='w', padx=(6, 3), pady=3)
        ttk.Entry(top, textvariable=self.model_var).grid(row=r, column=1, columnspan=6, sticky='ew', padx=3, pady=3)
        ttk.Button(top, text='찾기/스캔', width=8, command=self.browse_model).grid(row=r, column=7, sticky='w', padx=(2, 6), pady=3)

        r = 3
        ttk.Label(top, text='mmproj(.gguf)').grid(row=r, column=0, sticky='w', padx=(6, 3), pady=3)
        ttk.Entry(top, textvariable=self.mmproj_var).grid(row=r, column=1, columnspan=6, sticky='ew', padx=3, pady=3)
        ttk.Button(top, text='찾기', width=6, command=self.browse_mmproj).grid(row=r, column=7, sticky='w', padx=(2, 6), pady=3)

        gpu_frame = ttk.LabelFrame(tab_run, text='GPU / RAM / 메모리 사용률')
        gpu_frame.grid(row=0, column=1, sticky='nsew', padx=(3, 6), pady=(6, 3))
        self.gpu_canvas = GPUMonitorCanvas(gpu_frame)
        self.gpu_canvas.pack(fill='both', expand=True)
        self.gpu_canvas.update_context(0, self._ctx_to_int())
        # llama-server 누적 토큰 (입력/출력) — 서버 실행 중 2초마다 갱신
        ttk.Label(gpu_frame, textvariable=self.token_stats_var, foreground='#8fa8bd',
                  font=('Consolas', 9), justify='left').pack(fill='x', padx=2, pady=(0, 5))

        opts = ttk.LabelFrame(tab_run, text='llama-cli 실행 옵션')
        opts.grid(row=1, column=0, columnspan=2, sticky='ew', padx=6, pady=3)
        for c in range(8):
            opts.columnconfigure(c, weight=2)
        opts.columnconfigure(3, weight=1)  # FlashAttn 컬럼 절반 너비 (정수 비율 1:2)

        option_labels = ['NGL', 'Context', 'Draft MTP', 'FlashAttn', 'CTK', 'CTV', 'NP', 'N']
        for i, label in enumerate(option_labels):
            ttk.Label(opts, text=label).grid(row=0, column=i, sticky='w', padx=(4, 2), pady=2)

        # NGL: 자유 입력 (유지)
        ttk.Entry(opts, textvariable=self.ngl_var, width=5).grid(row=1, column=0, sticky='ew', padx=2, pady=2)
        # Context: 32K~256K 드롭박스 (32K 단위)
        ctx_values = [f'{k}K' for k in range(32, 257, 32)]
        ttk.Combobox(opts, textvariable=self.ctx_var, values=ctx_values, state='readonly', width=6).grid(row=1, column=1, sticky='ew', padx=2, pady=2)
        # Draft MTP: 체크박스 (on/off, --spec-type draft-mtp)
        ttk.Checkbutton(opts, variable=self.mtp_on_var).grid(row=1, column=2, sticky='w', padx=(8, 2), pady=2)
        # FlashAttn: 체크박스 (on/off)
        ttk.Checkbutton(opts, variable=self.fa_on_var).grid(row=1, column=3, sticky='w', padx=(8, 2), pady=2)
        # CTK / CTV: 드롭박스 (b10453 kv_cache_types)
        cache_types = ['f32', 'f16', 'bf16', 'q8_0', 'q4_0', 'q4_1', 'iq4_nl', 'q5_0', 'q5_1']
        ttk.Combobox(opts, textvariable=self.ctk_var, values=cache_types, state='readonly', width=7).grid(row=1, column=4, sticky='ew', padx=2, pady=2)
        ttk.Combobox(opts, textvariable=self.ctv_var, values=cache_types, state='readonly', width=7).grid(row=1, column=5, sticky='ew', padx=2, pady=2)
        # NP: 자유 입력 (유지)
        ttk.Entry(opts, textvariable=self.np_var, width=5).grid(row=1, column=6, sticky='ew', padx=2, pady=2)
        # N: 512~4096 드롭박스 (512 단위)
        n_values = [str(v) for v in range(512, 4097, 512)]
        ttk.Combobox(opts, textvariable=self.n_var, values=n_values, state='readonly', width=6).grid(row=1, column=7, sticky='ew', padx=2, pady=2)

        srv = ttk.LabelFrame(tab_run, text='서버(네트워크) 옵션')
        srv.grid(row=2, column=0, columnspan=2, sticky='ew', padx=6, pady=3)
        for c in range(1, 9):
            srv.columnconfigure(c, weight=1)
        ttk.Label(srv, text='모드').grid(row=0, column=0, sticky='w', padx=(4, 2), pady=2)
        self.mode_combo = ttk.Combobox(srv, textvariable=self.mode_var, values=['CLI(채팅)', '서버(네트워크)'], width=13, state='readonly')
        self.mode_combo.grid(row=0, column=1, sticky='w', padx=2, pady=2)
        self.vision_check = ttk.Checkbutton(srv, text='이미지/영상 분석(Vision)', variable=self.vision_var)
        self.vision_check.grid(row=0, column=2, sticky='w', padx=(16, 2), pady=2)
        ttk.Label(srv, textvariable=self.url_var, foreground='#1a5fb4').grid(row=0, column=3, columnspan=6, sticky='w', padx=(12, 6), pady=2)
        # 추론 모드: reasoning_effort 드롭박스 (서버 모드 /v1/chat/completions 요청에 포함됨)
        ttk.Label(srv, text='추론 노력도 (reasoning_effort)').grid(row=1, column=0, sticky='w', padx=(4, 2), pady=2)
        ttk.Combobox(srv, textvariable=self.reasoning_var,
                     values=['minimal', 'low', 'medium', 'high', 'xhigh'],
                     width=10, state='readonly').grid(row=1, column=1, sticky='w', padx=2, pady=2)
        # MTP max: 스펙추적 디코딩 draft 토큰 상한 (--spec-draft-n-max, 0=미사용) — 추론 노력도 오른쪽
        ttk.Label(srv, text='MTP max').grid(row=1, column=2, sticky='w', padx=(16, 2), pady=2)
        ttk.Spinbox(srv, from_=0, to=32, increment=1, width=5, textvariable=self.mtp_max_var).grid(row=1, column=3, sticky='w', padx=2, pady=2)
        # 컨텍스트 컴프레셔 (헤드리스 서버와 config.json 공유): 자동 토글 / 트리거 비율 / 최근 유지 개수
        ttk.Label(srv, text='컨텍스트 컴프레셔').grid(row=2, column=0, sticky='w', padx=(4, 2), pady=2)
        ttk.Checkbutton(srv, text='자동', variable=self.compress_auto_var).grid(row=2, column=1, sticky='w', padx=2, pady=2)
        ttk.Label(srv, text='비율(%)').grid(row=2, column=2, sticky='w', padx=(16, 2), pady=2)
        ttk.Spinbox(srv, from_=10, to=99, increment=5, width=5,
                    textvariable=self.compress_pct_var).grid(row=2, column=3, sticky='w', padx=2, pady=2)
        ttk.Label(srv, text='유지').grid(row=2, column=4, sticky='w', padx=(12, 2), pady=2)
        ttk.Spinbox(srv, from_=1, to=50, increment=1, width=5,
                    textvariable=self.compress_keep_var).grid(row=2, column=5, sticky='w', padx=2, pady=2)
        # 수동 압축 버튼 — 유지 개수 오른쪽 (채팅 탭 '압축(요약)' 버튼과 동일 동작)
        ttk.Button(srv, text='🗜 압축', command=self.compress_chat_manual).grid(row=2, column=6, sticky='w', padx=(12, 2), pady=2)

        # 프리셋 — 현재 옵션을 presets.json에 저장/복원 (통합 웹 서버와 공유)
        pre = ttk.LabelFrame(tab_run, text='프리셋 (설정 저장/적용)')
        pre.grid(row=3, column=0, columnspan=2, sticky='ew', padx=6, pady=3)
        ttk.Label(pre, text='이름').grid(row=0, column=0, sticky='w', padx=(6, 3), pady=3)
        ttk.Entry(pre, textvariable=self.preset_name_var, width=14).grid(row=0, column=1, sticky='w', padx=3, pady=3)
        ttk.Button(pre, text='현재 값 저장', width=10, command=self.save_preset).grid(row=0, column=2, sticky='w', padx=3, pady=3)
        self.preset_combo = ttk.Combobox(pre, textvariable=self.presets_var, state='readonly', width=16)
        self.preset_combo.grid(row=0, column=3, sticky='w', padx=(12, 3), pady=3)
        # 프리셋 선택 시 저장 이름 칸에도 반영 (수정 후 저장 시 같은 이름에 덮어쓸 수 있음)
        self.preset_combo.bind('<<ComboboxSelected>>',
                               lambda e: self.preset_name_var.set(self.presets_var.get().strip()))
        ttk.Button(pre, text='적용', width=6, command=self.load_preset).grid(row=0, column=4, sticky='w', padx=3, pady=3)
        ttk.Button(pre, text='삭제', width=6, command=self.delete_preset).grid(row=0, column=5, sticky='w', padx=3, pady=3)

        # 8행 2분할 — 왼쪽: 기존 버튼/서버 상태, 오른쪽: 신규 기능
        btns = ttk.Frame(tab_run)
        btns.grid(row=4, column=0, columnspan=2, sticky='ew', padx=6, pady=(3, 6))
        btns.columnconfigure(0, weight=1, uniform='half')
        btns.columnconfigure(1, weight=1, uniform='half')

        btns_left = ttk.Frame(btns)
        btns_left.grid(row=0, column=0, sticky='ew')
        ttk.Button(btns_left, text='시작', command=self.start_process).pack(side='left', padx=2)
        ttk.Button(btns_left, text='중지', command=self.stop_process).pack(side='left', padx=2)
        ttk.Button(btns_left, text='재시작', command=self.restart_process).pack(side='left', padx=2)
        ttk.Button(btns_left, text='콘솔 지우기', command=self.clear_console).pack(side='left', padx=2)
        ttk.Label(btns_left, textvariable=self.status_var).pack(side='left', padx=10)

        # 오른쪽: 모델 언로드/재로드 + TTL 자동 언로드 영역
        self.new_feature_frame = ttk.Frame(btns)
        self.new_feature_frame.grid(row=0, column=1, sticky='ew')
        self.btn_unload = ttk.Button(self.new_feature_frame, text='언로드', command=self.unload_model)
        self.btn_unload.pack(side='left', padx=(6, 2))
        self.btn_reload = ttk.Button(self.new_feature_frame, text='재로드', command=self.reload_model)
        self.btn_reload.pack(side='left', padx=2)
        ttk.Label(self.new_feature_frame, text='TTL(분)').pack(side='left', padx=(12, 4))
        ttk.Spinbox(self.new_feature_frame, from_=0, to=999, width=4, textvariable=self.ttl_var).pack(side='left')
        self.ttl_info_label = ttk.Label(self.new_feature_frame,
                                        text='0=해제 / 무요청 시 자동 언로드·재로드',
                                        foreground='#57606a')
        self.ttl_info_label.pack(side='left', padx=6)
        self.new_feature_frame.bind('<Configure>', self._fit_ttl_label)

        console = ttk.LabelFrame(tab_run, text='llama.cpp 콘솔')
        console.grid(row=5, column=0, columnspan=2, sticky='nsew', padx=6, pady=(3, 6))
        self.console_text = tk.Text(console, bg='#0b0f14', fg='#e6edf3', insertbackground='#ffffff', wrap='none', font=('Consolas', 9), padx=6, pady=6)
        scroll = ttk.Scrollbar(console, command=self.console_text.yview)
        self.console_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.console_text.pack(side='left', fill='both', expand=True)
        self.console_text.configure(state='disabled')

        # ── 채팅 탭 (서버 모드: 로컬 API / CLI 모드: stdin) ────────────────
        tab_chat.columnconfigure(0, weight=1)
        tab_chat.rowconfigure(1, weight=1)

        ttk.Label(tab_chat, textvariable=self.chat_mode_var, foreground='#57606a').grid(row=0, column=0, sticky='w', padx=8, pady=(6, 2))

        msgs = ttk.LabelFrame(tab_chat, text='대화')
        msgs.grid(row=1, column=0, sticky='nsew', padx=8, pady=3)
        self.chat_text = tk.Text(msgs, bg='#0b0f14', fg='#e6edf3', insertbackground='#ffffff', wrap='word', font=('Consolas', 10), padx=8, pady=8, state='disabled')
        mscroll = ttk.Scrollbar(msgs, command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=mscroll.set)
        mscroll.pack(side='right', fill='y')
        self.chat_text.pack(side='left', fill='both', expand=True)
        self.chat_text.tag_configure('user', foreground='#7ab8ff')
        self.chat_text.tag_configure('model', foreground='#d7e1ea')
        self.chat_text.tag_configure('note', foreground='#f2a33c')
        self.chat_text.tag_configure('error', foreground='#ff7b72')

        attach_bar = ttk.Frame(tab_chat)
        attach_bar.grid(row=2, column=0, sticky='ew', padx=8, pady=(4, 2))
        ttk.Button(attach_bar, text='이미지 첨부', command=self.attach_image).pack(side='left', padx=(0, 4))
        ttk.Button(attach_bar, text='영상 첨부', command=self.attach_video).pack(side='left', padx=(0, 8))
        ttk.Label(attach_bar, textvariable=self.attach_var, foreground='#1a7f37').pack(side='left', padx=4)
        ttk.Button(attach_bar, text='제거', width=5, command=self._clear_attachment).pack(side='left')

        input_bar = ttk.Frame(tab_chat)
        input_bar.grid(row=3, column=0, sticky='ew', padx=8, pady=(2, 4))
        self.chat_entry = ttk.Entry(input_bar, font=('Consolas', 10))
        self.chat_entry.pack(side='left', fill='x', expand=True, padx=(0, 6))
        self.chat_entry.bind('<Return>', lambda e: self.send_chat_tab())
        ttk.Button(input_bar, text='전송', command=self.send_chat_tab).pack(side='left')

        bottom_bar = ttk.Frame(tab_chat)
        bottom_bar.grid(row=4, column=0, sticky='ew', padx=8, pady=(0, 8))
        ttk.Button(bottom_bar, text='대화 초기화', command=self.reset_chat).pack(side='left')
        ttk.Button(bottom_bar, text='압축(요약)', command=self.compress_chat_manual).pack(side='left', padx=(6, 0))
        ttk.Label(bottom_bar, textvariable=self.chat_status_var, foreground='#57606a').pack(side='left', padx=12)

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.mode_var.trace_add('write', self._on_mode_change)
        self.refresh_preset_list()

        self.log('프로그램 시작. 실행 모드(CLI/서버), 바이너리 경로, 모델 경로를 확인하세요.')

    def _log(self, message):
        self.log_queue.put(str(message))

    def log(self, message):
        self._log(message)

    def _append_console(self, line):
        self.console_text.configure(state='normal')
        ts = datetime.now().strftime('%H:%M:%S')
        self.console_text.insert('end', f'[{ts}] {line}\n')
        line_count = int(self.console_text.index('end-1c').split('.')[0])
        if line_count > 3000:
            self.console_text.delete('1.0', '1500.0')
        self.console_text.see('end')
        self.console_text.configure(state='disabled')

    def _poll_log_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                if self._web_node is not None:
                    # GUI 로그를 웹 서버 로그 버퍼에도 동기화 (/api/logs, SSE용)
                    self._web_node.logs.add(line)
                self._append_console(line)
                if self.server_mode_active:
                    # llama-server 수면(자동 언로드) 상태 변화 추적
                    if 'entering sleeping state' in line:
                        self.model_state = 'sleeping'
                        self.log('[TTL] 모델이 VRAM에서 언로드됨 (수면) — 다음 요청 시 자동 재로드됩니다')
                    elif 'exiting sleeping state' in line:
                        self.model_state = 'loaded'
                        self.log('[TTL] 수면 종료 — 모델 재로드 시작 (시간이 걸릴 수 있습니다)')
                        # 토큰 라벨의 언로드 표시 해제 (누적 값은 계속 유지)
                        cur = self.token_stats_var.get()
                        if '언로드' in cur:
                            self.token_stats_var.set(cur.replace(' (모델 언로드됨)', ''))
                    elif 'model loaded' in line:
                        self.model_state = 'loaded'
        except queue.Empty:
            pass
        if self.proc is not None:
            if self.proc.poll() is None:
                if self.server_mode_active:
                    self.status_var.set({
                        'loading': '모델 로드 중...',
                        'loaded': '서버 실행 중',
                        'sleeping': '수면 중 — 모델 VRAM 해제',
                    }.get(self.model_state, '서버 실행 중'))
                else:
                    self.status_var.set('실행 중')
            else:
                self.status_var.set('중지됨')
        self.root.after(120, self._poll_log_queue)

    # ── llama-server 통계 (누적 토큰 / 컨텍스트 사용량) ──────────────
    def _poll_server_stats(self):
        """2초마다 llama-server의 누적 토큰/컨텍스트 사용량을 조회해 그래프·라벨 갱신.

        자동 언로드(TTL)가 켜진 서버에서는 /slots·/metrics 요청이 서버의
        유휴 타이머를 리셋(수면 중이면 즉시 깨어나 모델 재로드)하므로,
        생성 요청 진행 중/직후에만 폴링한다.
        """
        try:
            if self.server_mode_active and self.proc is not None and self.proc.poll() is None:
                sleep_enabled = (self._ttl_at_start or 0) > 0
                if not sleep_enabled or self._gen_activity_recent():
                    self._fetch_server_stats()
                elif self.model_state == 'sleeping':
                    # 모델이 이미 언로드(수면)됨 — 컨텍스트 사용량 0, 토큰 라벨에 표시
                    self.gpu_canvas.update_context(0, self._ctx_to_int())
                    cur = self.token_stats_var.get()
                    if cur.startswith('토큰 누적:') and '언로드' not in cur:
                        self.token_stats_var.set(cur + ' (모델 언로드됨)')
            else:
                self.gpu_canvas.update_context(0, self._ctx_to_int())
                if self.token_stats_var.get():
                    self.token_stats_var.set('')
                self._last_ctx_used = 0
                self._last_tokens_in = None
                self._last_tokens_out = None
        except Exception:
            pass
        self.root.after(2000, self._poll_server_stats)

    def _fetch_server_stats(self):
        base = f'http://127.0.0.1:{DEFAULT_PORT}'
        # 1) 현재 컨텍스트 사용량: /slots의 n_prompt_tokens 합 (프롬프트+생성 토큰 = 슬롯 컨텍스트 점유량)
        #    조회 실패(모델 로딩 중 등) 시 이전 값을 유지 (0으로 되돌리지 않음)
        used_ctx, max_ctx, slots_ok = 0, self._ctx_to_int(), False
        try:
            with urllib.request.urlopen(base + '/slots', timeout=2) as r:
                slots = json.loads(r.read().decode('utf-8'))
            for s in slots:
                used_ctx += int(s.get('n_prompt_tokens') or 0)
                n_ctx = int(s.get('n_ctx') or 0)
                if n_ctx > max_ctx:
                    max_ctx = n_ctx
            slots_ok = True
        except Exception:
            pass
        if slots_ok:
            self.gpu_canvas.update_context(used_ctx, max_ctx)
            # 웹 서버(/api/status)용 캐시 — 메인 스레드에서만 갱신
            self._last_ctx_used = used_ctx
            # ── 컴프레셔: 컨텍스트 비율 임계값 초과 감지 (서버 측 강제 트리거) ──
            self._check_compress_threshold(used_ctx, max_ctx)
        # 2) 누적 토큰: /metrics (--metrics 옵션 필요)의 Prometheus 카운터
        #    HTTP 오류 = 엔드포인트 미지원(--metrics 없음), 타임아웃/네트워크 = 일시적 실패(이전 값 유지)
        prompt_total = None
        gen_total = None
        metrics_unsupported = False
        try:
            with urllib.request.urlopen(base + '/metrics', timeout=2) as r:
                text = r.read().decode('utf-8')
            for line in text.splitlines():
                if not line.startswith('llamacpp:'):
                    continue
                name, _, val = line.rpartition(' ')
                if not val:
                    continue
                try:
                    f = float(val)
                except ValueError:
                    continue
                if name == 'llamacpp:prompt_tokens_total':
                    # 총 입력 = 캐시 재사용 포함 전체 프롬프트.
                    # (prompt_tokens_cached_total은 이 합계의 하위 집합이라 더하면 이중 계산)
                    prompt_total = f
                elif name == 'llamacpp:tokens_predicted_total':
                    gen_total = f
        except urllib.error.HTTPError:
            metrics_unsupported = True
        except Exception:
            pass
        if prompt_total is not None and gen_total is not None:
            self.token_stats_var.set(
                f'토큰 누적: 입력 {int(prompt_total):,} | 출력 {int(gen_total):,}')
            self._last_tokens_in = int(prompt_total)
            self._last_tokens_out = int(gen_total)
        elif metrics_unsupported:
            self.token_stats_var.set('토큰 누적: — (이 서버에서 --metrics 미활성, 재시작 필요)')

    def browse_cli(self):
        initial = ''
        if self.cli_var.get():
            d = os.path.dirname(self.cli_var.get())
            if d and os.path.isdir(d):
                initial = d
        p = filedialog.askopenfilename(title='llama-cli 선택', initialdir=initial or None, filetypes=[('Executable', '*.exe'), ('All files', '*.*')])
        if p:
            self.cli_var.set(p)

    def browse_server(self):
        initial = ''
        if self.server_var.get():
            d = os.path.dirname(self.server_var.get())
            if d and os.path.isdir(d):
                initial = d
        p = filedialog.askopenfilename(title='llama-server 선택', initialdir=initial or None, filetypes=[('Executable', '*.exe'), ('All files', '*.*')])
        if p:
            self.server_var.set(p)

    def _on_mode_change(self, *args):
        if not self.mode_var.get().startswith('서버'):
            self.url_var.set('')
        self._update_chat_tab_state()

    def browse_model(self):
        initial = _models_dir_default()
        if self.model_var.get():
            d = os.path.dirname(self.model_var.get())
            if d and os.path.isdir(d):
                initial = d
        p = filedialog.askopenfilename(title='GGUF 모델 선택', initialdir=initial, filetypes=[('GGUF files', '*.gguf'), ('All files', '*.*')])
        if p:
            self.model_var.set(p)
            self._auto_find_mmproj(p)
            self._auto_set_ngl(p)
            self.log(f'모델 선택: {p}')

    def browse_mmproj(self):
        initial = _models_dir_default()
        if self.mmproj_var.get():
            d = os.path.dirname(self.mmproj_var.get())
            if d and os.path.isdir(d):
                initial = d
        p = filedialog.askopenfilename(title='mmproj 선택', initialdir=initial, filetypes=[('GGUF files', '*.gguf'), ('All files', '*.*')])
        if p:
            self.mmproj_var.set(p)

    def _auto_find_mmproj(self, model_path):
        if not model_path or not os.path.isfile(model_path):
            return
        d = os.path.dirname(model_path)
        try:
            for f in os.listdir(d):
                if f.lower().startswith('mmproj') and f.lower().endswith('.gguf'):
                    self.mmproj_var.set(os.path.join(d, f))
                    self.log(f'mmproj 자동 발견: {os.path.join(d, f)}')
                    return
        except Exception:
            pass

    def _auto_set_ngl(self, model_path):
        """선택된 GGUF 모델의 계층 수(블록 수)를 읽어서 NGL 기본값으로 설정."""
        layers = read_gguf_layer_count(model_path)
        if layers and layers > 0:
            self.ngl_var.set(str(layers))
            self.log(f'NGL 자동 설정: {layers} (모델 계층 수)')

    def _ctx_to_int(self):
        """Context 드롭박스 값('128K')을 정수(131072)로 변환."""
        s = self.ctx_var.get().strip().upper()
        try:
            return int(float(s[:-1]) * 1024) if s.endswith('K') else int(s)
        except ValueError:
            return 4096

    def _start_web_server(self):
        """0.0.0.0:DEFAULT_PORT — 통합 웹 서버 (모바일 UI + 컨트롤 API + OpenAI 프록시).

        모델 기동 여부와 무관하게 앱 기동 시 항상 실행 (헤드리스 서버 llm_server.py와 동일):
          - 모바일(아이폰 PWA)에서 모델 기동 전에도 UI 열람/기동 요청 가능
          - /v1/* 등은 _start_api_proxy()가 인계한 백엔드 포트에 동적 프록시
        launcher_web.WebHandler를 헤드리스 서버와 공유해 기능을 한 곳에서 관리.
        """
        if launcher_web is None:
            self.log('웹 서버 모듈을 찾을 수 없습니다 (../llm_launcher_app/launcher_web.py) — 기동별 레거시 프록시만 사용')
            return
        node = GuiWebNode(self)
        cfg = {
            'host': DEFAULT_HOST,
            'port': int(DEFAULT_PORT),
            'api_key': '',
            'models_dir': _models_dir_default(),
            'compressor': self._compressor_cfg(),
        }
        try:
            server = launcher_web.HTTPServer((DEFAULT_HOST, int(DEFAULT_PORT)), launcher_web.WebHandler)
        except OSError as e:
            self.log(f'웹 서버 바인딩 실패 (0.0.0.0:{DEFAULT_PORT}): {e}')
            self.log('다른 인스턴스(헤드리스 서버, 기존 GUI 인스턴스 등)가 포트를 점유 중인지 확인한 뒤 다시 시작하세요.')
            return
        launcher_web.WebHandler.node = node
        launcher_web.WebHandler.cfg = cfg
        launcher_web.WebHandler.web_dir = WEB_UI_DIR
        launcher_web.WebHandler.log_all_requests = False  # /api 폴링은 콘솔에 남기지 않음 (자산 요청은 기록)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.2},
                         daemon=True, name='web-server').start()
        self.web_server = server
        self._web_node = node
        self.log(f'웹 서버 기동: http://<PC 주소>:{DEFAULT_PORT} (모바일 UI /, OpenAI API /v1, 제어 /api/*)')
        self.log(f'※ 단일 모델 서버: 모바일(PWA)과 VSCode(OpenAI)가 모두 이 {DEFAULT_PORT} 포트를 공유해 같은 모델을 씁니다. '
                 f'별도 헤드리스 서버(llm_server.py)를 함께 띄우면 모델이 2개로 분리됩니다.')
        try:
            for ip in launcher_web.get_all_ipv4():
                tag = ' (Tailscale)' if launcher_web._is_tailnet_ip(ip) else ''
                self.log(f'  → http://{ip}:{DEFAULT_PORT}{tag}')
        except Exception:
            pass

    def _start_api_proxy(self, frontend_port):
        """llama-server용 백엔드 포트 배정 후 통합 웹 서버에 등록.

        통합 웹 서버가 실행 중이면 백엔드 포트 인계만 수행 (웹 서버가 동적 프록시).
        미실행(모듈缺失/바인딩 실패)이면 레거시 기동별 프록시로 폴백.
        """
        backend_port = _find_free_port()
        if self._web_node is not None:
            self._web_node.set_backend_port(backend_port)
            return backend_port
        # ── 폴백: 레거시 기동별 프록시 ──
        _ApiInspectProxyHandler.launcher = self
        _ApiInspectProxyHandler.backend_host = '127.0.0.1'
        _ApiInspectProxyHandler.backend_port = backend_port
        try:
            _Srv = launcher_web.HTTPServer if launcher_web is not None else ThreadingHTTPServer
            server = _Srv(('0.0.0.0', frontend_port), _ApiInspectProxyHandler)
        except OSError as e:
            self.log(f'프록시 바인딩 실패 (0.0.0.0:{frontend_port}): {e}')
            return 0
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.2}, daemon=True).start()
        self.proxy_server = server
        return backend_port

    # ── 프리셋(설정 저장/적용) — 웹 서버와 launcher_web의 presets.json 공유 ──
    def _load_preset_list(self):
        if launcher_web is None:
            return []
        return launcher_web.load_presets()

    def refresh_preset_list(self):
        if getattr(self, 'preset_combo', None) is None:
            return
        names = [str(p.get('name') or '') for p in self._load_preset_list()]
        cur = self.presets_var.get()
        self.preset_combo['values'] = names
        if cur not in names:
            self.presets_var.set('')

    def _collect_preset(self):
        try:
            mtp_max = int(float(self.mtp_max_var.get().strip() or 0))
        except (TypeError, ValueError):
            mtp_max = 0
        mtp_max = max(0, min(32, mtp_max))
        return {
            'name': self.preset_name_var.get().strip(),
            'model': self.model_var.get().strip(),
            'mmproj': self.mmproj_var.get().strip(),
            'ngl': self.ngl_var.get().strip(),
            'ctx': self.ctx_var.get().strip(),
            'mtp': bool(self.mtp_on_var.get()),
            'fa': bool(self.fa_on_var.get()),
            'ctk': self.ctk_var.get().strip(),
            'ctv': self.ctv_var.get().strip(),
            'np': self.np_var.get().strip(),
            'n': self.n_var.get().strip(),
            'mtp_max': str(mtp_max),
            # exe 경로 (llama-server/llama-cli) — 자동 탐색이 없어 명시 경로를 프리셋에 함께 저장
            'server_exe': self.server_var.get().strip(),
            'cli_exe': self.cli_var.get().strip(),
            # 컨텍스트 컴프레셔 옵션 (자동/트리거비율/유지개수) — 프리셋에 함께 저장
            'compressor': self._compressor_cfg(),
        }

    def save_preset(self):
        if launcher_web is None:
            messagebox.showerror('프리셋', '프리셋 기능을 사용할 수 없습니다 (launcher_web 모듈 없음).')
            return
        p = self._collect_preset()
        if not p['name']:
            messagebox.showwarning('프리셋', '프리셋 이름을 입력하세요.')
            return
        if len(p['name']) > 50:
            messagebox.showwarning('프리셋', '프리셋 이름은 50자 이내로 입력하세요.')
            return
        if not p['model']:
            messagebox.showwarning('프리셋', '모델(.gguf) 경로를 먼저 설정하세요.')
            return
        try:
            with launcher_web.PRESETS_LOCK:
                presets = [x for x in self._load_preset_list() if x.get('name') != p['name']]
                presets.append(p)
                launcher_web.save_presets({}, presets)
        except Exception as e:
            messagebox.showerror('프리셋', '저장 실패: ' + str(e))
            return
        self.presets_var.set(p['name'])
        self.refresh_preset_list()
        self.log('[프리셋] "%s" 저장됨 (모델: %s)' % (p['name'], os.path.basename(p['model']) or '?'))

    def load_preset(self):
        if launcher_web is None:
            messagebox.showerror('프리셋', '프리셋 기능을 사용할 수 없습니다 (launcher_web 모듈 없음).')
            return
        name = self.presets_var.get().strip()
        p = next((x for x in self._load_preset_list() if x.get('name') == name), None)
        if not p:
            messagebox.showinfo('프리셋', '적용할 프리셋을 선택하세요.')
            return
        self.model_var.set(str(p.get('model') or ''))
        self.mmproj_var.set(str(p.get('mmproj') or ''))
        self.ngl_var.set(str(p.get('ngl') or '999'))
        self.ctx_var.set(str(p.get('ctx') or '128K'))
        self.mtp_on_var.set(bool(p.get('mtp')))
        self.fa_on_var.set(bool(p.get('fa')))
        self.ctk_var.set(str(p.get('ctk') or 'q8_0'))
        self.ctv_var.set(str(p.get('ctv') or 'q8_0'))
        self.np_var.set(str(p.get('np') or '1'))
        self.n_var.set(str(p.get('n') or '2048'))
        self.mtp_max_var.set(str(p.get('mtp_max') or '0'))
        # exe 경로 (llama-server/llama-cli) — 프리셋에 저장된 값이 있으면 채움 (오래된 프리셋이면 현재 값 유지)
        if 'server_exe' in p:
            self.server_var.set(str(p['server_exe'] or ''))
        if 'cli_exe' in p:
            self.cli_var.set(str(p['cli_exe'] or ''))
        # 컴프레셔 옵션 (오래된 프리셋에 compressor 없으면 현재 값 유지)
        if isinstance(p.get('compressor'), dict):
            self._apply_compressor_cfg(p['compressor'])
        # 저장 이름 칸에 적용한 프리셋 이름 반영 (수정 후 저장 시 같은 이름에 덮어쓸 수 있음)
        self.preset_name_var.set(name)
        self.log('[프리셋] "%s" 적용됨 — 다음 기동에 위 값이 사용됩니다 (컴프레셔 포함)' % name)

    def delete_preset(self):
        if launcher_web is None:
            messagebox.showerror('프리셋', '프리셋 기능을 사용할 수 없습니다 (launcher_web 모듈 없음).')
            return
        name = self.presets_var.get().strip()
        if not name:
            messagebox.showinfo('프리셋', '삭제할 프리셋을 선택하세요.')
            return
        if not messagebox.askyesno('프리셋', '프리셋 "%s"을(를) 삭제할까요?' % name):
            return
        try:
            with launcher_web.PRESETS_LOCK:
                presets = [x for x in self._load_preset_list() if x.get('name') != name]
                launcher_web.save_presets({}, presets)
        except Exception as e:
            messagebox.showerror('프리셋', '삭제 실패: ' + str(e))
            return
        self.presets_var.set('')
        self.refresh_preset_list()
        self.log('[프리셋] "%s" 삭제됨' % name)

    def start_process(self):
        self.stop_process()
        server_mode = self.mode_var.get().startswith('서버')
        exe = (self.server_var.get().strip() if server_mode else self.cli_var.get().strip())
        exe_name = 'llama-server.exe' if server_mode else 'llama-cli.exe'
        model = self.model_var.get().strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror('오류', f'{exe_name} 파일을 찾을 수 없습니다.\n"{exe_name}" 경로를 확인하거나 "찾기"로 선택하세요.')
            return
        if not model or not os.path.isfile(model):
            messagebox.showerror('오류', '모델 .gguf 파일을 찾을 수 없습니다.\n"모델(.gguf)" 경로를 확인하세요.')
            return

        mmproj = self.mmproj_var.get().strip()

        if server_mode:
            host = DEFAULT_HOST
            port = DEFAULT_PORT
            # ── API 인스펙션 리버스 프록시 ──
            # 외부 클라이언트 요청의 reasoning_effort를 로깅하려면 프록시를
            # llama-server 앞에 두어야 한다 (llama-server는 이 값을 자체 로깅하지 않음).
            #   외부 클라이언트 → 프록시(0.0.0.0:8080) → llama-server(127.0.0.1:<백엔드 포트>)
            backend_port = self._start_api_proxy(frontend_port=int(port))
            if backend_port:
                srv_host = '127.0.0.1'   # 프록시 경유로만 외부 노출 (프록시 우회 방지)
            else:
                backend_port = int(port)
                srv_host = host
                self.log('경고: API 인스펙션 프록시 기동 실패 (포트 점유?) — 외부 클라이언트 요청 로깅 비활성화')
            cmd = [
                exe,
                '-m', model,
                '-ngl', self.ngl_var.get().strip() or '0',
                '-c', str(self._ctx_to_int()),
                '-fa', 'on' if self.fa_on_var.get() else 'off',
                '-ctk', self.ctk_var.get().strip() or 'q8_0',
                '-ctv', self.ctv_var.get().strip() or 'q8_0',
                '-np', self.np_var.get().strip() or '1',
                '--host', srv_host,
                '--port', str(backend_port),
                '--metrics',
            ]
            self.log('메트릭 엔드포인트 활성화: --metrics (누적 토큰/컨텍스트 사용량 표시용)')
            if backend_port != int(port):
                if self._web_node is not None:
                    self.log(f'OpenAI API 프록시: 0.0.0.0:{port} → 127.0.0.1:{backend_port} '
                             f'(통합 웹 서버가 동적 프록시 — 별도 바인딩 없음, [API 수신] 로그 기록)')
                else:
                    self.log(f'API 인스펙션 프록시 기동: 0.0.0.0:{port} → 127.0.0.1:{backend_port} '
                             f'(외부 클라이언트 요청의 reasoning_effort가 [API 수신] 로그로 기록됩니다)')
            if mmproj:
                if os.path.isfile(mmproj):
                    cmd += ['--mmproj', mmproj]
                else:
                    self.log(f'경고: mmproj 파일을 찾지 못해 무시합니다. {mmproj}')
            if self.vision_var.get():
                cmd += ['--image-min-tokens', '1024', '--image-max-tokens', '4096']
                self.log('이미지/영상 분석(Vision) 모드 활성화: --image-min-tokens 1024, --image-max-tokens 4096')
            ttl = self._ttl_minutes()
            self._ttl_at_start = ttl
            self._ttl_warned = None
            if ttl > 0:
                cmd += ['--sleep-idle-seconds', str(ttl * 60)]
                self.log(f'자동 언로드(TTL) 활성화: {ttl}분간 요청 없으면 모델이 VRAM에서 언로드됩니다 '
                         f'(--sleep-idle-seconds {ttl * 60}), 이후 요청 시 자동 재로드')
                self.log('참고: 자동 언로드 중에는 요청 진행 중/직후에만 토큰·컨텍스트 통계를 '
                         '갱신합니다 (통계 요청도 서버의 유휴 타이머를 리셋하므로)')
        else:
            cmd = [
                exe,
                '-m', model,
                '-ngl', self.ngl_var.get().strip() or '0',
                '-c', str(self._ctx_to_int()),
                '-fa', 'on' if self.fa_on_var.get() else 'off',
                '-ctk', self.ctk_var.get().strip() or 'q8_0',
                '-ctv', self.ctv_var.get().strip() or 'q8_0',
                '-np', self.np_var.get().strip() or '1',
                '-n', self.n_var.get().strip() or '2048',
            ]
            if mmproj:
                if os.path.isfile(mmproj):
                    cmd += ['--mmproj', mmproj]
                else:
                    self.log(f'경고: mmproj 파일을 찾지 못해 무시합니다. {mmproj}')
        if self.mtp_on_var.get():
            cmd += ['--spec-type', 'draft-mtp']
            try:
                mtp_max = int(float(self.mtp_max_var.get().strip() or 0))
            except ValueError:
                mtp_max = 0
            mtp_max = max(0, min(32, mtp_max))
            if mtp_max > 0:
                cmd += ['--spec-draft-n-max', str(mtp_max)]
            self.log('Draft MTP(스펙추적 디코딩) 활성화: --spec-type draft-mtp'
                     + (f' --spec-draft-n-max {mtp_max}' if mtp_max > 0 else ''))
        self.log('실행 명령: ' + ' '.join(cmd))
        kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL if server_mode else subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
        )
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        env = get_fresh_env()
        if env.get('Path') != os.environ.get('Path'):
            self.log('참고: 자식 프로세스 환경의 PATH를 레지스트리 최신 값으로 갱신했습니다.')
        kwargs['env'] = env
        try:
            self.proc = subprocess.Popen(cmd, **kwargs)
            if self._job:
                if _assign_process_to_job(self._job, self.proc):
                    if not self._job_announced:
                        self.log('Job Object 등록: GUI가 종료되면 자식 llama 프로세스도 함께 자동 종료됩니다 (고아 방지).')
                        self._job_announced = True
                else:
                    self.log('경고: Job Object 등록 실패 — GUI 강제 종료 시 자식 프로세스가 고아로 남을 수 있습니다.')
            self.server_mode_active = server_mode
            if server_mode:
                self.model_state = 'loading'
            self.status_var.set('서버 시작 중' if server_mode else '실행 중')
            self.log(('llama-server 프로세스 시작됨.' if server_mode else 'llama-cli 프로세스 시작됨.'))
            if server_mode:
                ip = get_lan_ip()
                url = f'http://{ip}:{port}'
                self.url_var.set(f'접속: {url}   (API: {url}/v1)')
                model_name = os.path.basename(model) or 'default'
                self.log(f'서버 접속 주소: {url}')
                self.log('다른 컴퓨터에서 위 주소로 웹 UI에, 또는 OpenAI 호환 API({0}/v1)에 접속할 수 있습니다.'.format(url))
                self.log('────────────────────────────────────────────────────')
                self.log('모바일 · VSCode 공용 접속 (이 서버가 유일한 모델 — 둘이 같은 모델을 공유)')
                self.log(f'  웹 UI (모바일에서 열기)   : {url}')
                self.log(f'  OpenAI Base URL (VSCode)  : {url}/v1')
                self.log(f'  모델 ID                   : {model_name}   (또는 "default")')
                self.log('  API 키                    : 미설정 — 확장 설정에 아무 값(예: key) 입력')
                self.log(f'  VSCode 예: Base URL={url}/v1, Model={model_name}, API Key=key')
                self.log('────────────────────────────────────────────────────')
            self.reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
            self.reader_thread.start()
        except Exception as e:
            messagebox.showerror('오류', f'{exe_name} 실행 실패:\n{e}')
            self.status_var.set('오류')
            self.proc = None

    def _read_stdout(self):
        proc = self.proc
        if proc is None:
            return
        try:
            while True:
                line = proc.stdout.readline()
                if line:
                    self.log_queue.put(line.rstrip('\n'))
                else:
                    break
        except Exception as e:
            self.log_queue.put(f'콘솔 읽기 오류: {e}')
        finally:
            try:
                code = proc.wait(timeout=1)
            except Exception:
                code = -1
            self.log_queue.put(f'프로세스 종료: exit code {code}')

    def stop_process(self):
        if self.proc is not None and self.proc.poll() is None:
            self.log(('llama-server' if self.server_mode_active else 'llama-cli') + ' 중지 요청...')
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        if self.proxy_server is not None:
            try:
                self.proxy_server.shutdown()
                self.proxy_server.server_close()
            except Exception:
                pass
            self.proxy_server = None
        self.server_mode_active = False
        self.model_state = None
        if self._web_node is not None:
            self._web_node.set_backend_port(0)
        self.url_var.set('')
        self.status_var.set('중지됨')

    def restart_process(self):
        self.stop_process()
        self.start_process()

    def clear_console(self):
        self.console_text.configure(state='normal')
        self.console_text.delete('1.0', 'end')
        self.console_text.configure(state='disabled')

    # ── 모델 언로드 / 재로드 / TTL(자동 언로드) ─────────────────────────
    def _fit_ttl_label(self, _event=None):
        """8행 우측 폭에 맞춰 TTL 설명 라벨의 줄바꿈 폭 조정 (좁은 창에서도 잘리지 않음)."""
        try:
            total_w = self.new_feature_frame.winfo_width()
            used = sum(c.winfo_reqwidth() for c in self.new_feature_frame.winfo_children()
                       if c is not self.ttl_info_label)
            self.ttl_info_label.configure(wraplength=max(100, total_w - used - 40))
        except Exception:
            pass

    def _ttl_minutes(self):
        """TTL 스펠박스 값(분) → int (0~999), 0=해제."""
        try:
            return max(0, min(999, int(self.ttl_var.get())))
        except (tk.TclError, ValueError):
            return 0

    def _on_ttl_change(self, *args):
        """서버 실행 중 TTL 스펠박스가 바뀌면 재시작 필요 안내 (--sleep-idle-seconds는 기동 옵션)."""
        running = self.server_mode_active and self.proc is not None and self.proc.poll() is None
        if not running or self._ttl_at_start is None:
            return
        try:
            v = int(self.ttl_var.get())
        except (tk.TclError, ValueError):
            return
        if v != self._ttl_at_start and getattr(self, '_ttl_warned', None) != v:
            self._ttl_warned = v
            self.log(f'[TTL] {v}분으로 변경 감지 — 실행 중인 서버에는 재시작 후 반영됩니다')

    def _gen_request_started(self):
        """생성 요청 시작 (프록시/로컬 채팅에서 호출)."""
        with self._gen_lock:
            self._gen_active += 1

    def _gen_request_finished(self):
        """생성 요청 완료 (프록시/로컬 채팅에서 호출)."""
        with self._gen_lock:
            if self._gen_active > 0:
                self._gen_active -= 1
            self.last_gen_done = time.time()

    def _gen_activity_recent(self, window=10.0):
        """생성 요청이 진행 중이거나 window 초 이내에 완료된 상태인지.

        자동 언로드(TTL)가 켜진 llama-server에서는 /slots·/metrics도 유휴
        타이머를 리셋하므로, 통계 폴링을 이 윈도우로만 제한한다.
        """
        with self._gen_lock:
            active = self._gen_active
        return active > 0 or (time.time() - self.last_gen_done) < window

    def unload_model(self):
        """언로드: 모델을 VRAM에서 해제 (이미 해제된 상태이면 무시)."""
        running = self.proc is not None and self.proc.poll() is None
        if not running:
            self.log('[언로드] 서버가 실행 중이 아닙니다 — 모델이 VRAM에 없습니다.')
            self.status_var.set('중지됨 — 모델 미로드')
            return
        if not self.server_mode_active:
            self.log('[언로드] CLI 모드에는 언로드 기능이 없습니다 (서버 모드 전용).')
            return
        if self.model_state == 'sleeping':
            self.log('[언로드] 이미 수면 중입니다 — 모델이 VRAM에서 해제된 상태입니다.')
            return
        self.log('[언로드] 모델을 VRAM에서 해제합니다: 서버를 중지합니다 '
                 '(b10453 단일 모델 모드에는 모델 언로드 API가 없어 프로세스 중지 시 VRAM이 해제됩니다). '
                 '"재로드" 버튼으로 서버를 다시 시작하세요.')
        self.stop_process()
        self.status_var.set('언로드됨 — VRAM 해제')

    def reload_model(self):
        """재로드: 수면 중이면 API로 깨우기(모델 자동 로드), 미실행이면 서버 시작."""
        running = self.proc is not None and self.proc.poll() is None
        if running and self.server_mode_active:
            if self.model_state == 'sleeping':
                self.status_var.set('재로드 중...')
                self.log('[재로드] 수면 중인 서버를 깨웁니다 — 모델 재로드 시작...')
                threading.Thread(target=self._reload_worker, daemon=True).start()
            else:
                self.log('[재로드] 모델이 이미 로드되어 있습니다 (또는 로드 중).')
            return
        if running:
            self.log('[재로드] CLI 모드에는 재로드 기능이 없습니다 (서버 모드 전용).')
            return
        if not self.mode_var.get().startswith('서버'):
            self.log('[재로드] CLI 모드에는 재로드 기능이 없습니다 (서버 모드 전용).')
            return
        self.log('[재로드] 서버를 시작합니다 (모델 로드)...')
        self.start_process()

    def _reload_worker(self):
        """수면 중인 서버를 POST /tokenize로 깨웁니다 (모델 자동 재로드를 트리거)."""
        t0 = time.time()
        try:
            url = f'http://127.0.0.1:{DEFAULT_PORT}/tokenize'
            req = urllib.request.Request(
                url,
                data=json.dumps({'content': 'llm_launcher reload'}).encode('utf-8'),
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=900) as r:
                r.read()
            self.root.after(0, lambda: (
                self.status_var.set('서버 실행 중'),
                self.log(f'[재로드] 모델 재로드 완료 ({time.time() - t0:.1f}초)')))
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                body = json.loads(e.read().decode('utf-8'))
                err = body.get('error')
                if isinstance(err, dict):
                    detail = err.get('message', '')
                elif isinstance(err, str):
                    detail = err
            except Exception:
                pass
            self.root.after(0, lambda: (
                self.status_var.set('재로드 실패'),
                self.log(f'[재로드 오류] API 오류 (HTTP {e.code}): {detail or e.reason}')))
        except Exception as e:
            self.root.after(0, lambda: (
                self.status_var.set('재로드 실패'),
                self.log(f'[재로드 오류] 깨우기 요청 실패: {e}')))

    # ── 채팅 탭 ────────────────────────────────────────────────
    def _update_chat_tab_state(self):
        running = self.proc is not None and self.proc.poll() is None
        if self.mode_var.get().startswith('서버'):
            note = '서버 모드 — 텍스트·이미지·영상 채팅 가능 (로컬 API: /v1/chat/completions)'
        else:
            note = 'CLI 모드 — 텍스트 채팅만 가능하며 응답은 "실행 & 설정" 탭의 llama.cpp 콘솔에 표시됩니다. 이미지/영상 첨부는 서버 모드에서 가능합니다.'
        if not running:
            note += '   [프로세스가 실행 중이 아닙니다 — "실행 & 설정" 탭에서 시작하세요]'
        self.chat_mode_var.set(note)

    def attach_image(self):
        p = filedialog.askopenfilename(title='이미지 선택', initialdir=os.path.expanduser('~'),
                                       filetypes=[('Images', '*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif')])
        if p:
            self._set_attachment(p)

    def attach_video(self):
        p = filedialog.askopenfilename(title='영상 선택', initialdir=os.path.expanduser('~'),
                                       filetypes=[('Videos', '*.mp4;*.avi;*.mkv;*.mov;*.webm')])
        if p:
            self._set_attachment(p)

    def _set_attachment(self, path):
        try:
            size_mb = os.path.getsize(path) / (1024 * 1024)
        except OSError:
            return
        self.attached_file = path
        base = f'첨부됨: {os.path.basename(path)} ({size_mb:.1f} MB)'
        self.attach_var.set(base)
        self.log(f'[첨부] {base}')
        if _mime_for(path).startswith('video/'):
            threading.Thread(target=self._probe_video_attach, args=(path, base), daemon=True).start()

    def _probe_video_attach(self, path, base_label):
        info = probe_video_info(path)
        if self.attached_file != path:  # 탐지 중 제거/교체됨
            return
        if not (info and info[0]):
            return
        dur, fps = info
        # b10453 MTMD는 기본 fps_target=4.0으로 프레임 추출 (mtmd_helper_video_init_params_default)
        eff_fps = min(fps, 4.0) if fps else 4.0
        frames = int(dur * eff_fps + 0.5)
        extra = f', {dur:.1f}초' + (f', 추출 약 {frames}프레임(4fps)' if frames else '')
        label = base_label + extra
        self.root.after(0, lambda: self.attach_var.set(label))
        if dur > 30:
            self.log(f'[경고] 긴 영상({dur:.0f}초, 추출 약 {frames}프레임) — 프레임이 이미지 토큰으로 변환되어 '
                     '추론에 매우 오래 걸리거나 실패할 수 있습니다. 10초 이내 클립을 권장합니다.')

    def _clear_attachment(self):
        self.attached_file = ''
        self.attach_var.set('')

    def reset_chat(self):
        self.chat_history.clear()
        self._clear_attachment()
        self.chat_text.configure(state='normal')
        self.chat_text.delete('1.0', 'end')
        self.chat_text.configure(state='disabled')
        self.chat_status_var.set('')

    # ── 컨텍스트 컴프레셔 ─────────────────────────────────────────────
    def _compressor_cfg(self):
        """현재 GUI 변수값을 컴프레셔 설정 dict로 (clamp 적용)."""
        try:
            pct = int(self.compress_pct_var.get())
        except (TypeError, ValueError):
            pct = 75
        try:
            keep = int(self.compress_keep_var.get())
        except (TypeError, ValueError):
            keep = 6
        return {'enabled': bool(self.compress_auto_var.get()),
                'auto_trigger_pct': max(10, min(99, pct)),
                'keep_last_msgs': max(1, min(50, keep))}

    def _on_compressor_change(self, *args):
        """변경 시 config.json에 저장 (헤드리스 서버와 공유)."""
        _save_compressor_cfg(self._compressor_cfg())

    def _apply_compressor_cfg(self, c):
        """웹 /api/compressor 변경 → Tk 변수에 반영 (메인 스레드)."""
        try:
            self.compress_auto_var.set(bool(c.get('enabled', True)))
        except Exception:
            pass
        try:
            self.compress_pct_var.set(int(c.get('auto_trigger_pct', 75)))
        except (TypeError, ValueError):
            pass
        try:
            self.compress_keep_var.set(int(c.get('keep_last_msgs', 6)))
        except (TypeError, ValueError):
            pass

    def compress_chat(self, messages, keep_last=6):
        """대화 압축 (웹 /api/compress, 수동/자동 버튼 공용). 결과 dict 반환.

        llama-server 호출은 통합 웹 서버 프록시(127.0.0.1:DEFAULT_PORT) 경유 —
        _api_chat_worker와 동일한 경로.
        """
        if launcher_web is None:
            return {'ok': False, 'error': '컴프레셔 사용 불가 (launcher_web 모듈 없음)'}
        if not (self.server_mode_active and self.proc is not None and self.proc.poll() is None):
            return {'ok': False, 'error': '서버가 기동되지 않았습니다'}
        try:
            keep_last = max(1, int(keep_last))
        except (TypeError, ValueError):
            keep_last = 6
        self.log(f'[컴프레셔] 요약 요청 (원본 {len(messages)}개 → 최근 {keep_last}개 유지)')
        ok, result = launcher_web.compress_chat_via_llm(
            f'http://127.0.0.1:{DEFAULT_PORT}', messages, keep_last, log=self.log)
        if ok:
            self.log(f"[컴프레셔] 완료: {result['original_count']}개 → {result['compressed_count']}개")
        else:
            self.log(f"[컴프레셔] 실패: {result.get('error')}")
        return result

    def compress_chat_manual(self):
        """채팅 탭 수동 압축 버튼."""
        if self._compressing:
            self._append_chat('[안내]', '압축이 진행 중입니다. 완료까지 기다려 주세요.', 'note')
            return
        if self.chat_busy:
            self._append_chat('[안내]', '이전 응답을 받는 중입니다. 완료 후 다시 시도해 주세요.', 'note')
            return
        if not (self.server_mode_active and self.proc is not None and self.proc.poll() is None):
            self._append_chat('[안내]', '서버를 먼저 기동해 주세요 ("실행 & 설정" 탭).', 'note')
            return
        if len(self.chat_history) <= self._compressor_cfg()['keep_last_msgs'] + 1:
            self._append_chat('[안내]', '압축할 메시지가 부족합니다.', 'note')
            return
        self._start_compress('manual')

    def _start_compress(self, trigger):
        """압축 워커 스레드 시작 (trigger: 'manual' | 'auto')."""
        if self._compressing or self.chat_busy:
            return
        self._compressing = True
        self.chat_busy = True
        self.chat_status_var.set('대화 압축 중…')
        messages = [dict(m) for m in self.chat_history]
        keep = self._compressor_cfg()['keep_last_msgs']
        threading.Thread(target=self._compress_worker,
                         args=(messages, keep, trigger), daemon=True).start()

    def _compress_worker(self, messages, keep, trigger):
        try:
            result = self.compress_chat(messages, keep_last=keep)
        except Exception as e:
            result = {'ok': False, 'error': str(e)}
        result['trigger'] = trigger
        self.chat_queue.put(('compressed', result))

    def _check_compress_threshold(self, used, maxc):
        """컴프레셔: 서버 측 컨텍스트 사용 비율이 설정 임계값을 넘으면 자동 트리거.

        - 임계값 초과 시 로그 + 'compress' 이벤트를 SSE 연결 웹(PWA) 클라이언트에 발행
          (PWA는 이 이벤트를 받아 자기 대화를 압축).
        - 히스테리시스(임계값-10%p)로 재트리거 방지.
        - GUI 자체 대화 기록도 메시지 수가 충분하면 바로 압축.
        """
        if not self.compress_auto_var.get():
            self._compress_armed = False
            return
        c = self._compressor_cfg()
        if maxc is None or maxc <= 0 or used <= 0:
            self._compress_armed = False
            return
        pct = 100.0 * used / maxc
        if pct >= c['auto_trigger_pct']:
            if not self._compress_armed:
                self._compress_armed = True
                self.log(f'[컴프레셔] 컨텍스트 사용 {pct:.0f}% ≥ 임계값 {c["auto_trigger_pct"]}% — '
                         f'자동 압축 (서버 트리거)')
                if launcher_web is not None:
                    try:
                        launcher_web.EVENT_BUS.publish('compress', {
                            'reason': 'auto', 'pct': round(pct),
                            'threshold': c['auto_trigger_pct']})
                    except Exception:
                        pass
                if len(self.chat_history) > c['keep_last_msgs'] + 1:
                    self._start_compress('auto')
        elif pct < c['auto_trigger_pct'] - 10:
            self._compress_armed = False

    def _redraw_chat_history(self):
        """압축 후 chat_history 기준으로 대화창 다시 그리기."""
        self.chat_text.configure(state='normal')
        self.chat_text.delete('1.0', 'end')
        self.chat_text.configure(state='disabled')
        for m in self.chat_history:
            role = m.get('role')
            c = m.get('content')
            if role == 'system':
                self._append_chat('[요약]', c if isinstance(c, str) else str(c), 'note')
            elif role == 'user':
                if isinstance(c, list):
                    c = ' '.join((p.get('text') or '[이미지/영상]')
                                 for p in c if isinstance(p, dict))
                elif not isinstance(c, str):
                    c = str(c)
                self._append_chat('나 >', c, 'user')
            elif role == 'assistant':
                self._append_chat('모델 >', c if isinstance(c, str) else str(c), 'model')

    def _append_chat(self, prefix, text, tag='model'):
        at_start = self.chat_text.get('1.0', 'end-1c') == ''
        nl = '' if at_start else '\n'
        self.chat_text.configure(state='normal')
        self.chat_text.insert('end', f'{nl}{prefix} {text}\n', tag)
        self.chat_text.see('end')
        self.chat_text.configure(state='disabled')

    def send_chat_tab(self):
        text = self.chat_entry.get().strip()
        attached = self.attached_file
        if not text and not attached:
            return
        if self.chat_busy:
            self._append_chat('[안내]', '이전 응답을 받는 중입니다. 완료 후 다시 시도해 주세요.', 'note')
            return
        running = self.proc is not None and self.proc.poll() is None
        if not running:
            self._append_chat('[안내]', '프로세스가 실행 중이 아닙니다. "실행 & 설정" 탭에서 시작 버튼을 먼저 눌러 주세요.', 'note')
            return
        server_mode = self.mode_var.get().startswith('서버')

        if attached and not os.path.isfile(attached):
            self._append_chat('[오류]', f'첨부 파일을 찾을 수 없습니다: {attached}', 'error')
            self._clear_attachment()
            return

        display = text or '(파일만 전송)'
        if attached:
            display += f'  [첨부: {os.path.basename(attached)}]'
        self._append_chat('나 >', display, 'user')
        self.chat_entry.delete(0, 'end')

        if server_mode:
            self.chat_busy = True
            if attached and _mime_for(attached).startswith('video/'):
                self.chat_status_var.set('영상 처리 중... (프레임 인코딩으로 인해 영상 길이에 따라 매우 오래 걸릴 수 있습니다)')
            else:
                self.chat_status_var.set('응답 생성 중... (모델 추론에 시간이 걸릴 수 있습니다)')
            reasoning_effort = self.reasoning_var.get().strip()
            threading.Thread(target=self._api_chat_worker,
                             args=(text, attached, reasoning_effort), daemon=True).start()
            self.log(f'[채팅 요청] {display}')
        else:
            # CLI 모드: stdin으로 텍스트 전송 (첨부는 서버 모드 전용)
            if attached:
                self._append_chat('[안내]', '이미지/영상 첨부는 서버 모드에서만 사용할 수 있습니다.', 'note')
                self._clear_attachment()
            if not text:
                return
            try:
                self.proc.stdin.write(text + '\n')
                self.proc.stdin.flush()
                self.log(f'USER> {text}')
                self.chat_status_var.set('전송됨 — 응답은 "실행 & 설정" 탭의 llama.cpp 콘솔에서 확인하세요')
            except Exception as e:
                self._append_chat('[오류]', f'채팅 전송 실패: {e}', 'error')

    def _chat_heartbeat(self, stop_event):
        """채팅 응답 대기 중 콘솔에 경과 시간을 주기적으로 기록(명령 수행 중임을 확인)."""
        start = time.time()
        while not stop_event.wait(30):
            elapsed = time.time() - start
            self.log(f'[진행 중] 채팅 요청 처리 중... 경과 {elapsed:.0f}초 (영상 추론은 매우 오래 걸릴 수 있습니다)')
            try:
                self.root.after(0, lambda e=elapsed: self.chat_status_var.set(f'처리 중... 경과 {e:.0f}초'))
            except Exception:
                return

    def _api_chat_worker(self, text, attached, reasoning_effort='', thinking_on=True, temp=0.7, top_p=0.95):
        stop_event = threading.Event()
        threading.Thread(target=self._chat_heartbeat, args=(stop_event,), daemon=True).start()
        t0 = time.time()
        try:
            content = []
            if text:
                content.append({'type': 'text', 'text': text})
            if attached:
                mime = _mime_for(attached)
                is_video = mime.startswith('video/')
                size_mb = os.path.getsize(attached) / (1024 * 1024)
                if is_video:
                    self.log(f'[영상 처리] base64 인코딩 시작: {os.path.basename(attached)} ({size_mb:.1f} MB)')
                enc_t0 = time.time()
                with open(attached, 'rb') as f:
                    b64 = base64.b64encode(f.read()).decode('ascii')
                self.log(f'[영상 처리] 인코딩 완료 (base64 {len(b64) / (1024 * 1024):.1f} MB, {time.time() - enc_t0:.1f}초 소요)')
                if is_video:
                    # llama-server(b10453): 영상은 비표준 input_video 파트로 전송 (data: 접두사 없이 raw base64만 허용)
                    content.append({'type': 'input_video', 'input_video': {'data': b64}})
                else:
                    content.append({'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}})
            if len(content) == 1 and content[0].get('type') == 'text':
                msg_content = content[0]['text']
            else:
                msg_content = content
            self.chat_history.append({'role': 'user', 'content': msg_content})
            params = {'model': 'default', 'messages': list(self.chat_history),
                      'temperature': temp, 'top_p': top_p}
            chat_kwargs = {'enable_thinking': bool(thinking_on)}
            if reasoning_effort:
                # 추론(reasoning) 모델의 effort 전달: llama.cpp는 chat_template_kwargs를
                # Jinja 채팅 템플릿 변수로 주입하므로 reasoning_effort를 템플릿에 넘긴다
                # (해당 변수가 없는 템플릿은 단순히 무시하므로 안전).
                params['reasoning_effort'] = reasoning_effort
                chat_kwargs['reasoning_effort'] = reasoning_effort
            params['chat_template_kwargs'] = chat_kwargs
            payload = json.dumps(params).encode('utf-8')
            api_url = f'http://127.0.0.1:{DEFAULT_PORT}/v1/chat/completions'
            self.log(f'[API] 요청 전송: {api_url} (payload {len(payload) / (1024 * 1024):.1f} MB, '
                     f'reasoning_effort={reasoning_effort or "미설정"}, thinking={"on" if thinking_on else "off"}, '
                     f'temperature={temp:g}, top_p={top_p:g}, 최대 대기 600초)')
            # 로컬 채팅도 "생성 요청"으로 추적 (프록시 없는 직결 모드에서는 프록시가
            # 이 요청을 볼 수 없으므로) — 자동 언로드(TTL) 시 통계 폴링 윈도우 결정
            self._gen_request_started()
            req = urllib.request.Request(
                api_url,
                data=payload, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=600) as r:
                resp = json.loads(r.read().decode('utf-8'))
            self.log(f'[API] 응답 수신 완료 (총 {time.time() - t0:.1f}초 소요)')
            reply = (resp['choices'][0]['message'].get('content') or '').strip() or '(비어 있는 응답)'
            self.chat_queue.put(('ok', reply))
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                body = json.loads(e.read().decode('utf-8'))
                err = body.get('error')
                if isinstance(err, dict):
                    detail = err.get('message', '')
                elif isinstance(err, str):
                    detail = err
            except Exception:
                pass
            msg = f'API 오류 (HTTP {e.code}): {detail or e.reason}'
            has_video = bool(attached) and _mime_for(attached).startswith('video/')
            if 'video input is not supported' in detail:
                msg += ' — 현재 모델/mmproj이 영상 입력을 지원하지 않습니다(이미지 전용 비전 모델일 수 있음)'
            elif has_video and ('failed to load' in detail.lower() or 'image or audio' in detail.lower()):
                msg += (' — 서버가 영상을 디코딩하지 못했습니다. llama.cpp 영상 처리는 PATH에 ffmpeg/ffprobe가 있어야 하며, '
                        '설치(또는 PATH 변경) 후에는 llama-server를 재시작해야 반영됩니다.')
            self.log(f'[API 오류] {msg} ({time.time() - t0:.1f}초 소요 후 실패)')
            self.chat_queue.put(('error', msg))
        except Exception as e:
            self.log(f'[API 오류] API 요청 실패: {e} ({time.time() - t0:.1f}초 소요 후 실패)')
            self.chat_queue.put(('error', f'API 요청 실패: {e}'))
        finally:
            stop_event.set()
            self._gen_request_finished()

    def _poll_chat_queue(self):
        try:
            while True:
                kind, payload = self.chat_queue.get_nowait()
                if kind == 'ok':
                    self.chat_history.append({'role': 'assistant', 'content': payload})
                    self._append_chat('모델 >', payload, 'model')
                    self.log(f'[모델 응답] {payload}')
                elif kind == 'compressed':
                    r = payload
                    self._compressing = False
                    if r.get('ok'):
                        self.chat_history = r['compressed']
                        self._redraw_chat_history()
                        trig = '자동' if r.get('trigger') == 'auto' else '수동'
                        self._append_chat('[압축됨]',
                                          f"대화가 압축되었습니다: {r.get('original_count', '?')}개 → "
                                          f"{r.get('compressed_count', '?')}개 ({trig} 트리거)", 'note')
                    else:
                        self._append_chat('[압축 실패]', r.get('error') or '알 수 없는 오류', 'error')
                else:
                    self._append_chat('[오류]', payload, 'error')
                self.chat_busy = False
                self.chat_status_var.set('')
        except queue.Empty:
            pass
        self._update_chat_tab_state()
        self.root.after(120, self._poll_chat_queue)

    def on_closing(self):
        if self.web_server is not None:
            try:
                self.web_server.shutdown()
                self.web_server.server_close()
            except Exception:
                pass
            self.web_server = None
        self.stop_process()
        self.root.destroy()

def _port_in_use(host, port):
    """(host, port)가 이미 다른 프로세스에 바인딩되어 있는지 검사 (테스트 바인드)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, int(port)))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _find_port_pid(port):
    """지정 포트를 LISTENING 중인 프로세스 PID 반환 (netstat -ano 기반, 없으면 None)."""
    try:
        out = subprocess.check_output(['netstat', '-ano'], stderr=subprocess.DEVNULL, text=True, timeout=8)
    except Exception:
        return None
    target = f':{int(port)}'
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == 'LISTENING' and parts[1].endswith(target):
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


def _kill_pid(pid):
    """PID 프로세스 강제 종료 (Windows: taskkill /T → 자식 프로세스 포함)."""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(int(pid))],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            import signal
            os.kill(int(pid), signal.SIGTERM)
        return True
    except Exception:
        return False


# ── Job Object: GUI 종료 시 자식 llama 프로세스 자동 종료 (고아 방지) ──
# KILL_ON_JOB_CLOSE: Job 핸들이 닫히는 순간(정상 종료, 작업 관리자 강제 종료,
# 크래시 모두 포함) Job에 속한 프로세스가 OS에 의해 강제 종료된다.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_SYNCHRONIZE = 0x0010


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_longlong),
        ('PerJobUserTimeLimit', ctypes.c_longlong),
        ('LimitAction', ctypes.c_uint32),
        ('SchedulingClass', ctypes.c_uint32),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


class _JOBOBJECT_IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('ReadOperationCount', ctypes.c_ulonglong),
        ('WriteOperationCount', ctypes.c_ulonglong),
        ('OtherOperationCount', ctypes.c_ulonglong),
        ('ReadTransferCount', ctypes.c_ulonglong),
        ('WriteTransferCount', ctypes.c_ulonglong),
        ('OtherTransferCount', ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ('IoInfo', _JOBOBJECT_IO_COUNTERS),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


def _create_kill_on_close_job():
    """KILL_ON_JOB_CLOSE 속성 Job Object 생성 (Windows만, 실패 시 None).
    반환 핸들은 절대 닫지 않아야 한다 — GUI 프로세스 종료 시 함께 닫히며,
    그때 Job 내 자식 프로세스가 OS에 의해 강제 종료된다."""
    if os.name != 'nt':
        return None
    try:
        k32 = ctypes.windll.kernel32
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitAction = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _assign_process_to_job(job, proc):
    """Popen 자식 프로세스를 Job에 등록 (성공 시 True).
    실패해도 기동 자체는 계속 진행 — 기존 동작과 동일하게 종료 처리된다."""
    if not job or proc is None:
        return False
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(_PROCESS_SET_QUOTA | _SYNCHRONIZE, False, proc.pid)
        if not h:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(job, h))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


# 공용 모듈이 사용 가능하면 헤드리스 서버(llm_server.py)와 동일한 Job Object
# 구현을 공유한다 (동일 로직의 복제/이탈 방지).
if launcher_web is not None and hasattr(launcher_web, 'create_kill_on_close_job'):
    _create_kill_on_close_job = launcher_web.create_kill_on_close_job
    _assign_process_to_job = launcher_web.assign_process_to_job


def _find_orphan_llama_pids():
    """tasklist로 llama-server.exe / llama-cli.exe 프로세스 [(이름, PID)] 반환."""
    found = []
    try:
        out = subprocess.check_output(
            ['tasklist', '/FO', 'CSV', '/NH', '/FI', 'IMAGENAME eq llama*.exe'],
            stderr=subprocess.DEVNULL, text=True, timeout=8)
    except Exception:
        return found
    for line in out.splitlines():
        parts = [p.strip('"').strip() for p in line.split(',"')]
        if len(parts) >= 2 and parts[0] in ('llama-server.exe', 'llama-cli.exe'):
            try:
                found.append((parts[0], int(parts[1])))
            except ValueError:
                continue
    return found


def _sweep_orphan_processes(root):
    """기동 시 고아 llama.cpp 프로세스(이전 인스턴스 잔재)를 확인 후 일괄 종료.
    이 시점에서는 이번 인스턴스가 자식을 아직 기동하지 않았으므로
    발견되는 프로세스는 모두 이전 잔재(예: 작업 관리자에서 GUI만 종료한 경우)."""
    if os.name != 'nt':
        return
    found = _find_orphan_llama_pids()
    if not found:
        return
    detail = '\n'.join(f'  {name}  PID {pid}' for name, pid in found)
    if not messagebox.askyesno(
        '고아 프로세스 감지',
        f'이전 실행 잔재로 추정되는 llama.cpp 프로세스 {len(found)}개를 찾았습니다.\n\n'
        f'{detail}\n\n'
        'VRAM을 점유 중일 수 있으므로 함께 강제 종료할까요?\n(아니오 선택 시 그대로 유지합니다.)'
    ):
        return
    for _name, pid in found:
        _kill_pid(pid)
    time.sleep(1)  # GPU 메모리 해제에 필요한 시간 확보


def main():
    root = tk.Tk()
    root.withdraw()  # 초기 비공개: 포트 확인/확인 대화상표 표시용
    # 포트(8080)가 이미 사용 중이면(기존 GUI/헤드리스 인스턴스) — 종료 후 진행 여부 확인
    if _port_in_use(DEFAULT_HOST, int(DEFAULT_PORT)):
        pid = _find_port_pid(int(DEFAULT_PORT))
        detail = f' (PID {pid})' if pid else ''
        if not messagebox.askyesno(
            '실행 중인 인스턴스 감지',
            f'포트 {DEFAULT_PORT}가 이미 사용 중입니다{detail}.\n\n'
            '기존 LLM Launcher(데스크톱 GUI 또는 헤드리스 llm_server.py)가\n'
            '실행 중인 것 같습니다. 기존 실행을 종료하고 새로 시작할까요?\n\n'
            '(아니오 선택 시 이번 실행을 취소합니다.)'
        ):
            root.destroy()
            sys.exit(1)
        # 기존 인스턴스 종료 후 포트가 실제로 해제될 때까지 대기 (최대 ~5초)
        if pid:
            _kill_pid(pid)
        for _ in range(20):
            if not _port_in_use(DEFAULT_HOST, int(DEFAULT_PORT)):
                break
            time.sleep(0.25)
        if _port_in_use(DEFAULT_HOST, int(DEFAULT_PORT)):
            messagebox.showerror(
                '포트 사용 중',
                f'기존 인스턴스를 종료하려 했으나 포트 {DEFAULT_PORT}가 여전히 점유 중입니다.\n'
                f'작업 관리자에서 관련 프로세스를 수동 종료한 뒤 다시 실행해 주세요.'
                + (f'\n\n(참고: PID {pid})' if pid else '')
            )
            root.destroy()
            sys.exit(1)
    # 고아 llama.cpp 프로세스(이전 인스턴스 잔재) 확인 후 정리
    _sweep_orphan_processes(root)
    app = App(root)
    root.protocol('WM_DELETE_WINDOW', app.on_closing)
    root.deiconify()  # 확인 절차 통과 → 실제 창 표시
    root.mainloop()

if __name__ == '__main__':
    main()
