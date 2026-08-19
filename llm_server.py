#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM Launcher Server (헤드리스)
==============================
데스크톱 GUI(llm_launcher.py)의 모델 로드/관리 로직을 재사용하고,
스마트폰(아이폰 PWA)에서 원격 제어할 수 있는 컨트롤 API를 추가한 서버.

- /api/*      : 컨트롤 API (기동/중지/상태/모델목록/로그)
- /v1/* 등    : OpenAI 호환 API 프록시 → llama-server (127.0.0.1:<동적 포트>)
- /           : Web UI (PWA) 정적 파일 서빙

사용법:
    python llm_server.py [--host 0.0.0.0] [--port 8080] [--key API키]

접속:
    - LAN   : http://<PC LAN IP>:8080
    - 외부   : http://<PC Tailscale IP (100.x.y.z)>:8080   (PC/아이폰 모두 Tailscale 가입)
"""
import os
import re
import sys
import json
import socket
import ctypes
import argparse
import subprocess
import threading
import time
import urllib.request
import http.client
from datetime import datetime
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, 'web')
CONFIG_PATH = os.path.join(APP_DIR, 'config.json')

# 공통 웹 서버 모듈 (데스크톱 GUI llm_launcher.py와 공유)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
from launcher_web import (  # noqa: E402
    LogBuffer, WebHandler, list_models, get_all_ipv4, _is_tailnet_ip,
    start_web_server, DEFAULT_MODELS_DIR,
    EVENT_BUS, compress_chat_via_llm,
    create_kill_on_close_job, assign_process_to_job,
)

# ── 공통 유틸 (llm_launcher.py에서 재사용) ──────────────────────────────────

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
            if not line:
                continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) < 3:
                continue
            try:
                u = float(parts[0]); mu = float(parts[1]); mt = float(parts[2])
            except ValueError:
                continue
            found = True
            gpu_util = max(gpu_util, u)
            mem_used += mu
            mem_total += mt
        if not found or mem_total <= 0:
            return None
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
        return ip
    except Exception:
        return '127.0.0.1'


def _find_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_in_use(host, port):
    """(host, port)가 이미 다른 프로세스에 바인딩되어 있는지 검사 (테스트 바인드).

    SO_REUSEADDR 없이 바인드를 시도한다 — Windows에서는 ThreadingHTTPServer가
    SO_REUSEADDR를 쓰기 때문에 실제 서버 바인드는 이미 점유된 포트에도 성공할 수
    있어, 점유 여부를 판단하려면 SO_REUSEADDR 미설정 소켓으로 미리 확인해야 한다.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, int(port)))
        return False
    except OSError:
        return True
    finally:
        s.close()


def ctx_to_int(v):
    """'128K' → 131072, 숫자 문자열/정수는 그대로."""
    s = str(v).strip().upper()
    try:
        return int(float(s[:-1]) * 1024) if s.endswith('K') else int(s)
    except ValueError:
        return 4096


# ── 설정 ────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    'host': '0.0.0.0',
    'port': 8080,
    'api_key': '',          # 비워두면 인증 없음. 설정 시 /api·프록시 요청에 X-Api-Key 헤더 필요
    'server_exe': '',       # llama-server.exe 경로 — 반드시 명시적으로 설정 (자동 탐색 없음)
    'models_dir': '',       # 비워두면 ~/.lmstudio/models
    'compressor': {
        'enabled': True,          # 자동 컴프레셔 활성화
        'auto_trigger_pct': 75,   # 컨텍스트 사용 비율이 이 값(%)을 넘으면 자동 압축 트리거
        'keep_last_msgs': 6,      # 압축 시 최근 유지 메시지 수 (요약 제외)
    },
    'defaults': {
        'ngl': '999', 'ctx': '128K', 'fa': True, 'ctk': 'q8_0', 'ctv': 'q8_0',
        'np': '1', 'mtp': False, 'vision': True, 'ttl_min': 0,
    },
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                user = json.load(f)
            for k, v in user.items():
                if k == 'defaults' and isinstance(v, dict):
                    cfg['defaults'].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f'[설정] config.json 로드 실패, 기본값 사용: {e}')
    return cfg


def save_config(cfg):
    """config.json 원자적 쓰기 (tmp + os.replace) — 쓰기 중 크래시 시 기존 파일 보존."""
    try:
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass


# ── 고아 프로세스 정리 (이전 인스턴스 잔재) ─────────────────────────────────
# Job Object가 생성되지 않는 환경(또는 작업 관리자로만 종료한 경우)에서
# llama-server.exe가 고아로 남아 VRAM을 점유하는 것을 방지한다.

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


def _kill_pid(pid):
    """PID 프로세스 강제 종료 (Windows: taskkill /T → 자식 프로세스 포함)."""
    try:
        if os.name == 'nt':
            subprocess.call(['taskkill', '/F', '/T', '/PID', str(pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import signal
            os.kill(int(pid), signal.SIGTERM)
    except Exception:
        pass


def sweep_orphan_processes(log):
    """기동 시 고아 llama.cpp 프로세스(이전 인스턴스 잔재)를 찾아 강제 종료.

    헤드리스 서버는 GUI와 달리 확인 대화상자가 없어 자동 처리한다 —
    이 시점에서 이번 인스턴스는 아직 자식을 기동하지 않았으므로 발견되는
    프로세스는 모두 이전 잔재다. (--no-sweep 지정 시 스킵)"""
    if os.name != 'nt':
        return
    found = _find_orphan_llama_pids()
    if not found:
        return
    for name, pid in found:
        log(f'고아 프로세스 정리: {name} (PID {pid}) 종료 — 이전 인스턴스 잔재')
        _kill_pid(pid)
    time.sleep(1)  # GPU 메모리 해제에 필요한 시간 확보


# ── 모델 노드 (프로세스/상태/통계 관리) ─────────────────────────────────────

class LLMNode:
    def __init__(self, cfg):
        self.cfg = cfg
        self.logs = LogBuffer()
        self._op_lock = threading.Lock()
        self._job = create_kill_on_close_job()   # 서버 프로세스 강제 종료 시 자식 llama-server 고아 방지
        if os.name == 'nt' and self._job is None:
            self.log('경고: Job Object 생성 실패 — 이 프로세스가 강제 종료(작업 관리자 등)되면 '
                     'llama-server가 고아로 남을 수 있습니다. 기동 시 고아 스윕이 이전 잔재는 정리합니다.')
        self.proc = None
        self.backend_port = 0
        self.model_state = None        # None | 'loading' | 'loaded' | 'sleeping'
        self.model_path = ''
        self.launch_cfg = {}
        self.ttl_at_start = 0
        self._ctx_max = 4096
        self._gen_lock = threading.Lock()
        self._gen_active = 0
        self.last_gen_done = 0.0
        self._status_cache = {}
        self._stop_evt = threading.Event()
        self._compress_armed = False    # 컴프레셔 히스테리시스 (한 번 트리거 후 재반복 방지)
        threading.Thread(target=self._poll_loop, daemon=True, name='stats-poll').start()

    # ── 로그 ──
    def log(self, msg):
        self.logs.add(msg)
        print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)

    # ── 프로세스 ──
    def process_running(self):
        p = self.proc
        if p is None:
            return False
        if p.poll() is None:
            return True
        if self.model_state is not None:
            self.model_state = None
        return False

    def launch(self, lc):
        """lc: {model, mmproj?, server_exe?, ngl, ctx, fa, ctk, ctv, np, mtp, mtp_max, vision, ttl_min}
        반환 (ok, err)."""
        with self._op_lock:
            self._stop_locked()
            exe = (lc.get('server_exe') or self.cfg.get('server_exe') or '').strip()
            model = (lc.get('model') or '').strip()
            if not model:
                last = self.cfg.get('last_launch') or {}
                if last.get('model'):
                    self.log('모델 미지정 — 마지막 기동 설정을 재사용합니다: ' + last['model'])
                    lc = dict(last)
                    model = (lc.get('model') or '').strip()
            if not exe:
                return False, 'llama-server.exe 경로가 설정되지 않았습니다. config.json의 "server_exe" 또는 기동 요청 server_exe를 설정하세요.'
            if not os.path.isfile(exe):
                return False, f'llama-server.exe 파일을 찾을 수 없습니다: {exe}'
            if not model:
                return False, '모델이 지정되지 않았습니다. 모델 탭에서 모델을 선택해 주세요.'
            if not os.path.isfile(model):
                return False, f'모델 .gguf 파일을 찾을 수 없습니다: {model}'

            ctx_int = ctx_to_int(lc.get('ctx', '128K'))
            backend_port = _find_free_port()
            cmd = [
                exe,
                '-m', model,
                '-ngl', str(lc.get('ngl', '999') or '0'),
                '-c', str(ctx_int),
                '-fa', 'on' if lc.get('fa', True) else 'off',
                '-ctk', str(lc.get('ctk', 'q8_0') or 'q8_0'),
                '-ctv', str(lc.get('ctv', 'q8_0') or 'q8_0'),
                '-np', str(lc.get('np', '1') or '1'),
                '--host', '127.0.0.1',
                '--port', str(backend_port),
                '--metrics',
            ]
            # mmproj 자동 발견 (모델 디렉터리 내 mmproj*.gguf)
            mmproj = (lc.get('mmproj') or '').strip()
            if not mmproj:
                try:
                    d = os.path.dirname(model)
                    for f in sorted(os.listdir(d)):
                        if f.lower().startswith('mmproj') and f.lower().endswith('.gguf'):
                            mmproj = os.path.join(d, f)
                            break
                except Exception:
                    pass
            if mmproj:
                if os.path.isfile(mmproj):
                    cmd += ['--mmproj', mmproj]
                    self.log(f'mmproj 자동 적용: {mmproj}')
                else:
                    self.log(f'경고: mmproj 파일을 찾지 못해 무시합니다. {mmproj}')
            if lc.get('vision', True):
                cmd += ['--image-min-tokens', '1024', '--image-max-tokens', '4096']
                self.log('Vision 모드 활성화: --image-min-tokens 1024, --image-max-tokens 4096')
            try:
                ttl = max(0, min(999, int(lc.get('ttl_min', 0) or 0)))
            except (TypeError, ValueError):
                ttl = 0
            self.ttl_at_start = ttl
            if ttl > 0:
                cmd += ['--sleep-idle-seconds', str(ttl * 60)]
                self.log(f'자동 언로드(TTL) 활성화: {ttl}분간 요청 없으면 VRAM에서 언로드됩니다')
            if lc.get('mtp'):
                cmd += ['--spec-type', 'draft-mtp']
                try:
                    mtp_max = int(lc.get('mtp_max') or 0)
                except (TypeError, ValueError):
                    mtp_max = 0
                mtp_max = max(0, min(32, mtp_max))
                if mtp_max > 0:
                    cmd += ['--spec-draft-n-max', str(mtp_max)]
                self.log('Draft MTP(스펙추적 디코딩) 활성화: --spec-type draft-mtp'
                         + (f' --spec-draft-n-max {mtp_max}' if mtp_max > 0 else ''))

            self.log(f'llama-server 기동: 127.0.0.1:{backend_port} (프록시 0.0.0.0:{self.cfg.get("port")})')
            self.log('실행 명령: ' + ' '.join(cmd))

            kwargs = dict(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
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
            except Exception as e:
                return False, f'llama-server 실행 실패: {e}'
            if self._job:
                if assign_process_to_job(self._job, self.proc):
                    self.log('Job Object 등록: 서버 프로세스가 종료(강제 종료 포함)되면 llama-server도 함께 종료됩니다 (고아 방지).')
                else:
                    self.log('경고: Job Object 등록 실패 — 서버 프로세스 강제 종료 시 llama-server가 고아로 남을 수 있습니다.')

            self.backend_port = backend_port
            self.model_state = 'loading'
            self.model_path = model
            self._ctx_max = ctx_int
            self.launch_cfg = dict(lc)
            self.cfg['last_launch'] = dict(lc)
            save_config(self.cfg)
            threading.Thread(target=self._read_stdout, daemon=True, name='stdout-reader').start()
            ip = get_lan_ip()
            self.log(f'서버 접속 주소: http://{ip}:{self.cfg.get("port")}   (웹 UI: /, API: /v1)')
            return True, None

    def stop(self):
        with self._op_lock:
            self._stop_locked()

    def _stop_locked(self):
        """_op_lock을 이미 보유한 상태에서 호출 (launch()가 기동 전 정지 시 재사용)."""
        proc = self.proc
        if proc is not None and proc.poll() is None:
            self.log('llama-server 프로세스 중지 요청...')
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            except Exception as e:
                self.log(f'프로세스 중지 중 오류: {e}')
        self.proc = None
        self.backend_port = 0
        self.model_state = None
        self._gen_active = 0
        self.last_gen_done = 0.0

    def _read_stdout(self):
        proc = self.proc
        if proc is None:
            return
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.rstrip('\n')
                if not line:
                    continue
                self.logs.add(line)
                print(f'[{datetime.now().strftime("%H:%M:%S")}] [llama] {line}', flush=True)
                # llama-server 상태 변화 추적
                if 'entering sleeping state' in line:
                    self.model_state = 'sleeping'
                    self.log('[TTL] 모델이 VRAM에서 언로드됨 (수면) — 다음 요청 시 자동 재로드됩니다')
                elif 'exiting sleeping state' in line:
                    self.model_state = 'loaded'
                    self.log('[TTL] 수면 종료 — 모델 재로드 시작 (시간이 걸릴 수 있습니다)')
                elif 'model loaded' in line:
                    self.model_state = 'loaded'
        except Exception as e:
            self.logs.add(f'콘솔 읽기 오류: {e}')
        finally:
            try:
                code = proc.wait(timeout=1)
            except Exception:
                code = -1
            self.log(f'프로세스 종료: exit code {code}')

    # ── TTL 생성 요청 추적 (기존 로직 재사용) ──
    def gen_request_started(self):
        with self._gen_lock:
            self._gen_active += 1

    def gen_request_finished(self):
        with self._gen_lock:
            if self._gen_active > 0:
                self._gen_active -= 1
            self.last_gen_done = time.time()

    def _gen_activity_recent(self, window=10.0):
        """생성 요청이 진행 중이거나 window 초 이내에 완료된 상태인지."""
        with self._gen_lock:
            if self._gen_active > 0:
                return True
            return (time.time() - self.last_gen_done) < window

    # ── 컨텍스트 컴프레셔 ──
    def compress_chat(self, messages, keep_last=6):
        """
        대화 메시지를 LLM 기반 요약으로 압축합니다 (launcher_web.compress_chat_via_llm 공용).

        Args:
            messages: OpenAI 형식 메시지 배열 [{'role': 'user'|'assistant', 'content': ...}, ...]
            keep_last: 요약에서 제외하고 유지할 최근 메시지 수

        Returns:
            {'ok': True, 'compressed': [...], 'original_count': N, 'compressed_count': N}
            또는 {'ok': False, 'error': '...'}
        """
        if not self.backend_port:
            return {'ok': False, 'error': '서버가 기동되지 않았습니다'}
        try:
            keep_last = max(1, int(keep_last))
        except (TypeError, ValueError):
            keep_last = 6
        self.log(f'[컴프레셔] 요약 요청 (원본 {len(messages)}개 → 최근 {keep_last}개 유지)')
        ok, result = compress_chat_via_llm(
            f'http://127.0.0.1:{self.backend_port}', messages, keep_last, log=self.log)
        if ok:
            self.log(f"[컴프레셔] 완료: {result['original_count']}개 → {result['compressed_count']}개")
        else:
            self.log(f"[컴프레셔] 실패: {result.get('error')}")
        return result

    def save_config(self, cfg=None):
        """웹 /api/compressor POST → 컴프레셔 설정 config.json 영속화.

        WebHandler는 node.save_config(self.cfg) 시그니처로 호출하므로
        cfg 인자를 선택적으로 받는다 (None이면 self.cfg)."""
        save_config(cfg or self.cfg)

    def _check_compress_threshold(self, used, maxc):
        """컴프레셔: 서버 측에서 컨텍스트 사용 비율이 설정 임계값을 넘으면 자동 트리거.

        - 임계값 초과 시 로그 기록 + 'compress' 이벤트를 모든 SSE 연결 웹 클라이언트에
          발행 — 웹 PWA는 이 이벤트를 받아 자기 대화 기록을 압축한다.
        - 히스테리시스(임계값-10%p)로 사용률이 떨어질 때까지 재트리거를 막는다.
        - VSCode 등 외부 클라이언트 대화는 서버에 없어 로그로만 알린다.
        """
        c = self.cfg.get('compressor') or {}
        if not c.get('enabled', True) or maxc is None or maxc <= 0 or used <= 0:
            self._compress_armed = False
            return
        try:
            pct_t = int(c.get('auto_trigger_pct', 75))
        except (TypeError, ValueError):
            pct_t = 75
        pct = 100.0 * used / maxc
        if pct >= pct_t:
            if not self._compress_armed:
                self._compress_armed = True
                self.log(f'[컴프레셔] 컨텍스트 사용 {pct:.0f}% ≥ 임계값 {pct_t}% — '
                         f'자동 압축 요청 발행 (SSE 이벤트)')
                try:
                    EVENT_BUS.publish('compress', {
                        'reason': 'auto',
                        'pct': round(pct),
                        'threshold': pct_t,
                        'keep_last': c.get('keep_last_msgs', 6),
                    })
                except Exception:
                    pass
        elif pct < pct_t - 10:
            self._compress_armed = False

    # ── 상태/통계 (2초 폴링) ──
    def _poll_loop(self):
        while not self._stop_evt.is_set():
            try:
                self._refresh_status()
            except Exception:
                pass
            time.sleep(2)

    def _refresh_status(self):
        running = self.process_running()
        gpu = get_nvidia_metrics()
        ram = get_system_memory_percent()
        ctx_used, ctx_max = 0, (self._ctx_max if running else None)
        tin, tout = None, None
        if running and self.backend_port:
            # 자동 언로드(TTL)가 켜진 서버에서는 /slots·/metrics도 유휴 타이머를
            # 리셋하므로, 생성 요청 진행 중/직후에만 폴링 (기존 GUI와 동일 전략)
            if self.model_state == 'loaded' and (self.ttl_at_start <= 0 or self._gen_activity_recent()):
                base = f'http://127.0.0.1:{self.backend_port}'
                try:
                    with urllib.request.urlopen(base + '/slots', timeout=2) as r:
                        slots = json.loads(r.read().decode('utf-8'))
                    used, mctx = 0, 0
                    for s in slots:
                        used += int(s.get('n_prompt_tokens') or 0)
                        mctx = max(mctx, int(s.get('n_ctx') or 0))
                    ctx_used = used
                    if mctx:
                        ctx_max = mctx
                except Exception:
                    pass
                # ── 컴프레셔: 컨텍스트 비율 임계값 초과 감지 (서버 측 강제 트리거) ──
                self._check_compress_threshold(ctx_used, ctx_max)
                try:
                    with urllib.request.urlopen(base + '/metrics', timeout=2) as r:
                        text = r.read().decode('utf-8')
                    p, g = None, None
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
                            p = f  # 캐시 재사용은 총계의 하위 집합 (더하지 않음)
                        elif name == 'llamacpp:tokens_predicted_total':
                            g = f
                    if p is not None and g is not None:
                        tin, tout = int(p), int(g)
                except Exception:
                    pass
            elif self.model_state == 'sleeping':
                ctx_used = 0
        self._status_cache = {
            'process': 'running' if running else 'stopped',
            'model_state': self.model_state if running else None,
            'model': os.path.basename(self.model_path) if (running and self.model_path) else None,
            'model_path': self.model_path if running else None,
            'ttl_min': self.ttl_at_start if running else None,
            'gpu': ({'util': gpu[0], 'mem_pct': gpu[1], 'mem_used_mb': gpu[2], 'mem_total_mb': gpu[3]}
                    if gpu else None),
            'sys_ram': ram,
            'context': {'used': ctx_used, 'max': ctx_max},
            'tokens': {'input': tin, 'output': tout},
            'last_launch': self.cfg.get('last_launch'),
        }

    def status(self):
        st = dict(self._status_cache)
        st.update({
            'host': self.cfg.get('host'),
            'port': self.cfg.get('port'),
            'server_exe': (self.launch_cfg.get('server_exe') or self.cfg.get('server_exe') or ''),
            'compressor': self.cfg.get('compressor') or {
                'enabled': True,
                'auto_trigger_pct': 75,
                'keep_last_msgs': 6,
            },
        })
        return st

    def shutdown(self):
        self._stop_evt.set()
        self.stop()


# ── 메인 ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='LLM Launcher 헤드리스 서버 (아이폰 PWA 원격 제어)')
    ap.add_argument('--host', help='바인딩 호스트 (기본: config.json 또는 0.0.0.0)')
    ap.add_argument('--port', type=int, help='포트 (기본: config.json 또는 8080)')
    ap.add_argument('--key', help='API 키 (config.json의 api_key 덮어쓰기)')
    ap.add_argument('--no-sweep', action='store_true',
                    help='기동 시 고아(llama*.exe) 프로세스 정리 스킵 (llama-server를 별도로 직접 실행 중일 때)')
    args = ap.parse_args()

    # 콘솔(cp949 등)에서 인코딩 못 하는 문자가 있어도 프로세스가 죽지 않도록
    for _st in (sys.stdout, sys.stderr):
        try:
            _st.reconfigure(errors='replace')
        except Exception:
            pass

    cfg = load_config()
    if args.host:
        cfg['host'] = args.host
    if args.port:
        cfg['port'] = args.port
    if args.key:
        cfg['api_key'] = args.key

    # 포트 충돌 fail-fast: 8080이 이미 사용 중이면(기존 GUI/헤드리스 인스턴스 등) 즉시 종료
    if port_in_use(cfg['host'], cfg['port']):
        print(f'에러: 포트 {cfg["port"]}가 이미 사용 중입니다.')
        print('LLM Launcher(데스크톱 GUI llm_launcher.py 또는 기존 헤드리스 인스턴스)가')
        print(f'이미 {cfg["port"]} 포트를 점유 중인 것 같습니다. 먼저 종료한 뒤 다시 실행해 주세요.')
        sys.exit(1)

    node = LLMNode(cfg)
    if not args.no_sweep:
        sweep_orphan_processes(node.log)

    try:
        httpd = start_web_server(node, cfg['host'], cfg['port'], WEB_DIR, cfg,
                                 log_all_requests=True)
    except OSError as e:
        print(f'에러: 포트 {cfg["port"]}가 이미 사용 중입니다 — {e}')
        print('LLM Launcher(데스크톱 GUI llm_launcher.py 또는 기존 헤드리스 인스턴스)가')
        print(f'이미 {cfg["port"]} 포트를 점유 중인 것 같습니다. 먼저 종료한 뒤 다시 실행해 주세요.')
        sys.exit(1)

    node.log(f'LLM Launcher 서버 기동: http://{cfg["host"]}:{cfg["port"]}')
    for ip in get_all_ipv4():
        tag = ' (Tailscale)' if _is_tailnet_ip(ip) else ''
        node.log(f'  → http://{ip}:{cfg["port"]}{tag}')
    if cfg.get('api_key'):
        node.log('API 키 인증 활성화됨 (X-Api-Key 헤더 또는 ?api_key=)')
    else:
        node.log('경고: API 키 미설정 — 같은 네트워크의 모든 기기가 제어할 수 있습니다')
    node.log('참고: 데스크톱 GUI(llm_launcher.py, 8080)를 함께 실행하면 별개의 2번째 모델 서버가 됩니다.')
    node.log('      모바일+VSCode가 같은 1개 모델을 쓰려면 GUI만 실행하세요 (GUI가 PWA·/v1 웹서버를 이미 내장).')

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        node.log('서버 종료 중...')
        node.shutdown()
        httpd.server_close()


if __name__ == '__main__':
    main()