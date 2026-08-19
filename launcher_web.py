#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launcher_web.py — LLM Launcher 공통 웹 서버 모듈
================================================
헤드리스 서버(llm_server.py)와 데스크톱 GUI(llm_launcher.py)가 공유하는
웹 서버(컨트롤 API + OpenAI 호환 프록시 + PWA 정적 서빙) 구현.

- /api/*      : 컨트롤 API (기동/중지/상태/모델목록/로그/SSE)
- /v1/* 등    : OpenAI 호환 API 프록시 → llama-server (127.0.0.1:<동적 포트>)
- /           : Web UI (PWA) 정적 파일 서빙

Node 인터페이스 (Handler가 호출 — 각 애플리케이션이 구현):
    node.log(msg)                      — 로그 추가 (앱 콘솔에도 반영)
    node.logs                          — LogBuffer (tail/since)
    node.status()                      — 상태 dict (LLMNode.status와 동일한 형태)
    node.launch(lc) -> (ok, err)       — 기동 요청 (비동기: 즉시 (True, None) 반환 가능)
    node.stop()    -> (ok, err)        — 중지 요청
    node.process_running() -> bool
    node.model_state                 — None | 'loading' | 'loaded' | 'sleeping'
    node.backend_port                — llama-server 포트 (0 = 중지)
    node.gen_request_started() / node.gen_request_finished()  — 생성 요청 추적 (TTL)
"""
import os
import re
import json
import queue
import socket
import struct
import sys
import time
import ctypes
import subprocess
import http.client
import threading
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

DEFAULT_MODELS_DIR = os.path.expanduser('~/.lmstudio/models')

# llama-server로 포워딩할 API 경로 (OpenAI 호환 등)
PROXY_PREFIXES = ('/v1/', '/completions', '/tokenize', '/metrics', '/slots', '/model', '/health')
# 생성 요청 경로 — reasoning_effort 로깅 + 자동 언로드 TTL 추적 대상
GEN_PATHS = ('/v1/chat/completions', '/v1/completions', '/completions')


# ── IP 유틸 ─────────────────────────────────────────────────────────────────

def _is_tailnet_ip(ip):
    """Tailscale CGNAT(100.64.0.0/10) 범위 여부."""
    parts = ip.split('.')
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
        return a == 100 and 64 <= b <= 127
    except ValueError:
        return False


def get_all_ipv4():
    """모든 non-loopback IPv4 (LAN + Tailscale 등) 목록 반환.

    어댑터의 실제 주소만 수집 (서브넷 마스크/게이트웨이/169.254.* APIPA 제외).
    """
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        if os.name == 'nt':
            out = subprocess.check_output(['ipconfig'], stderr=subprocess.DEVNULL, text=True, timeout=5)
            for line in out.splitlines():
                # "IPv4 주소. . . . . . . . . . . : 192.168.1.161" (영어: "IPv4 Address")
                if 'IPv4' in line:
                    m = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', line)
                    if m:
                        ips.add(m.group(1))
        else:
            out = subprocess.check_output(['ifconfig'], stderr=subprocess.DEVNULL, text=True, timeout=5)
            for m in re.finditer(r'^\s*inet (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', out, re.M):
                ips.add(m.group(1))
    except Exception:
        pass
    ips = {ip for ip in ips
           if not ip.startswith('127.') and not ip.startswith('169.254.') and not ip.startswith('255.')}
    return sorted(ips, key=lambda x: tuple(int(p) for p in x.split('.')))


# ── 로그 버퍼 (SSE 스트림용, 시퀀스 번호 기반) ──────────────────────────────

class LogBuffer:
    def __init__(self, maxlen=3000):
        self.buf = deque(maxlen=maxlen)
        self.lock = threading.Lock()
        self.seq = 0

    def add(self, line, ts=None):
        with self.lock:
            self.seq += 1
            self.buf.append((self.seq, ts or time.time(), line))

    def since(self, last_seq):
        with self.lock:
            return [e for e in self.buf if e[0] > last_seq]

    def tail(self, n):
        with self.lock:
            return list(self.buf)[-n:]


# ── 서버 → 웹 클라이언트 이벤트 버스 (SSE /api/events) ───────────────────────

class EventBus:
    """스레드 간 단순 pub/sub.

    발행자(상태 폴링 스레드 등)는 subscribe된 콜백만 호출하고,
    각 SSE 연결은 자체 큐에 담아 핸들러 스레드에서 wfile에 쓴다.
    (wfile은 각 SSE 핸들러 스레드에서만 접근 — 경합 없음)
    """

    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self, cb):
        with self._lock:
            self._subs.append(cb)

    def unsubscribe(self, cb):
        with self._lock:
            self._subs = [s for s in self._subs if s is not cb]

    def publish(self, event_type, data=None):
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(event_type, data)
            except Exception:
                pass


EVENT_BUS = EventBus()


# ── 컨텍스트 컴프레셔 (헤드리스 LLMNode / 데스크톱 GUI App 공통) ─────────────

def compress_chat_via_llm(api_base, messages, keep_last=6, log=None):
    """LLM 요약으로 대화를 (system 요약 + 최근 N개)로 압축.

    최근 keep_last 개는 그대로 유지하고, 이전 메시지는 system 요약 메시지로
    압축한다. 이미지/영상(list content)은 [이미지/영상 첨부]로 표시하며,
    기존 요약(이전 압축)이 있으면 재요약에 포함(병합)한다.

    api_base: llama-server(또는 그 프록시) 기본 URL, 예: http://127.0.0.1:58427
    반환: (ok, result_dict)
      ok=True  : {'ok': True, 'compressed': [...], 'original_count', 'compressed_count', 'summary'}
      ok=False : {'ok': False, 'error': str}
    """
    def _log(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    try:
        keep_last = max(1, int(keep_last))
    except (TypeError, ValueError):
        keep_last = 6
    msgs = [m for m in (messages or [])
            if isinstance(m, dict) and m.get('role') in ('system', 'user', 'assistant')]
    if len(msgs) <= keep_last + 1:
        return False, {'ok': False,
                       'error': f'압축할 메시지가 부족합니다 (최근 {keep_last}개 유지 + 1개 이상 필요)'}

    to_summarize = msgs[:-keep_last]
    recent = msgs[-keep_last:]

    def content_text(m):
        c = m.get('content')
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for p in c:
                if isinstance(p, dict):
                    if p.get('type') == 'text' and p.get('text'):
                        parts.append(str(p['text']))
                    else:
                        parts.append('[이미지/영상 첨부]')
            return ' '.join(parts) if parts else '[첨부파일]'
        if c is None:
            return ''
        return str(c)

    lines, qn = [], 0
    for m in to_summarize:
        role, txt = m.get('role'), content_text(m)
        if role == 'user':
            qn += 1
            lines.append(f'Q{qn}: {txt}')
        elif role == 'assistant':
            lines.append(f'A{qn or 1}: {txt}')
        elif role == 'system':
            lines.append(f'[기존 요약]: {txt}')
    conversation = '\n'.join(lines)

    payload = {
        'model': 'default',
        'messages': [
            {'role': 'system', 'content':
                '다음 대화 기록을 간결하고 정확하게 요약하세요. '
                '주요 주제, 결정 사항, 중요한 사실, 사용자의 목표/선호를 포함하세요. '
                '3~5문장으로, 이후 대화가 맥락을 이어갈 수 있도록 요약해 주세요.'},
            {'role': 'user', 'content': f'다음 대화 기록을 요약해 주세요:\n\n{conversation}'},
        ],
        'temperature': 0.3,
        'top_p': 0.9,
        'max_tokens': 1024,
        'stream': False,
    }
    req = urllib.request.Request(
        api_base.rstrip('/') + '/v1/chat/completions',
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return False, {'ok': False, 'error': f'LLM 호출 실패: {e}'}
    summary = ''
    try:
        summary = (result['choices'][0]['message'].get('content') or '').strip()
    except (KeyError, IndexError, TypeError):
        pass
    if not summary:
        return False, {'ok': False, 'error': '요약 결과가 비어 있습니다'}
    compressed = [{'role': 'system', 'content': f'[이전 대화 요약]\n{summary}'}] + recent
    _log(f'[Compressor] 요약 생성 ({len(summary)}자)')
    return True, {'ok': True, 'compressed': compressed,
                  'original_count': len(msgs), 'compressed_count': len(compressed),
                  'summary': summary}


# ── GGUF 파싱: 모델의 계층 수(블록 수) 읽기 ─────────────────────────────────
# GGUF 헤더: magic(4) + version(4) + n_tensors(8) + n_kv(8), 그 뒤 n_kv개의 키/값 쌍.
# general.architecture + *.block_count 추출. struct만 사용하는 순수 Python 구현.
def _gguf_read_str(f):
    # GGUF의 문자열은 uint64 길이 + 바이트 (레거시 GGML만 uint32)
    n = struct.unpack('<Q', f.read(8))[0]
    return f.read(n).decode('utf-8', errors='replace')


def _gguf_skip_value(f, ty):
    if ty in (0, 1, 7):       # UINT8/INT8/BOOL
        f.read(1)
    elif ty in (2, 3):        # UINT16/INT16
        f.read(2)
    elif ty in (4, 5, 6):     # UINT32/INT32/FLOAT32
        f.read(4)
    elif ty in (10, 11, 12):  # UINT64/INT64/FLOAT64
        f.read(8)
    elif ty == 8:             # STRING
        _gguf_read_str(f)
    elif ty == 9:             # ARRAY
        ety = struct.unpack('<I', f.read(4))[0]
        count = struct.unpack('<Q', f.read(8))[0]
        for _ in range(count):
            _gguf_skip_value(f, ety)
    else:
        raise ValueError('unknown GGUF value type: %d' % ty)


def _gguf_read_kvs(f, n_kv):
    """n_kv개 메타데이터 쌍을 dict로 반환 (ARRAY 값은 None)."""
    fmts = {0: '<B', 1: '<b', 2: '<H', 3: '<h', 4: '<I', 5: '<i',
            6: '<f', 7: '?', 10: '<Q', 11: '<q', 12: '<d'}
    kvs = {}
    for _ in range(n_kv):
        key = _gguf_read_str(f)
        ty = struct.unpack('<I', f.read(4))[0]
        if ty in fmts:
            kvs[key] = struct.unpack(fmts[ty], f.read(struct.calcsize(fmts[ty])))[0]
        elif ty == 8:  # STRING
            kvs[key] = _gguf_read_str(f)
        else:
            _gguf_skip_value(f, ty)
            kvs[key] = None
    return kvs


def read_gguf_layer_count(path):
    """GGUF 파일에서 모델의 계층 수(block_count)를 읽음. 실패 시 None 반환."""
    try:
        with open(path, 'rb') as f:
            magic = f.read(4)
            if magic == b'GGUF':
                version = struct.unpack('<I', f.read(4))[0]
                if version < 2:
                    return None
                struct.unpack('<Q', f.read(8))[0]  # n_tensors
                n_kv = struct.unpack('<Q', f.read(8))[0]
                kvs = _gguf_read_kvs(f, n_kv)
            elif magic == b'GGML':  # 레거시 GGML v3 (값은 원시 바이트로 저장)
                struct.unpack('<I', f.read(4))[0]   # version
                struct.unpack('<Q', f.read(8))[0]   # tensor_count
                n_kv = struct.unpack('<Q', f.read(8))[0]
                kvs = {}
                for _ in range(n_kv):
                    n = struct.unpack('<I', f.read(4))[0]
                    key = f.read(n).decode('utf-8', errors='replace')
                    vsize = struct.unpack('<Q', f.read(8))[0]
                    kvs[key] = f.read(vsize)
            else:
                return None
    except Exception:
        return None
    if not kvs:
        return None
    arch = kvs.get('general.architecture')
    if isinstance(arch, bytes):
        arch = arch.decode('utf-8', errors='replace')
    if not isinstance(arch, str) or not arch:
        return None
    bc = kvs.get(arch + '.block_count')
    if isinstance(bc, bytes):  # GGML 원시 값
        try:
            bc = struct.unpack('<I', bc[:4])[0] if len(bc) >= 4 else struct.unpack('<Q', bc[:8])[0]
        except (struct.error, ValueError):
            return None
    if isinstance(bc, int) and not isinstance(bc, bool) and bc > 0:
        return bc
    return None


# ── 모델 디렉터리 탐색 ──────────────────────────────────────────────────────

def list_models(d):
    d = os.path.abspath(os.path.expanduser(d))
    out = {'dir': d, 'parent': None, 'dirs': [], 'files': [], 'mmproj_hint': None}
    if os.path.isdir(os.path.dirname(d)):
        out['parent'] = os.path.dirname(d)
    if not os.path.isdir(d):
        out['error'] = '디렉터리를 찾을 수 없습니다'
        return out
    try:
        for name in sorted(os.listdir(d)):
            fp = os.path.join(d, name)
            if os.path.isdir(fp):
                out['dirs'].append(name)
            elif name.lower().endswith('.gguf'):
                try:
                    size = os.path.getsize(fp)
                except OSError:
                    size = 0
                is_mm = name.lower().startswith('mmproj')
                layer_count = read_gguf_layer_count(fp) if not is_mm else None
                out['files'].append({
                    'name': name,
                    'path': fp,
                    'size_mb': round(size / 1048576, 1),
                    'is_mmproj': is_mm,
                    'layer_count': layer_count,
                })
                if is_mm and out['mmproj_hint'] is None:
                    out['mmproj_hint'] = fp
    except Exception as e:
        out['error'] = str(e)
    return out


# ── 프리셋(설정 저장) ────────────────────────────────────────────────────
# 저장소: 앱 디렉터리 presets.json (GUI와 헤드리스 서버가 공유).
# 필드: name, model, mmproj, ngl, ctx, mtp, fa, ctk, ctv, np, n, mtp_max,
#       server_exe, cli_exe (llama.cpp exe 경로 — 자동 탐색 제거 후 명시 저장), compressor

PRESETS_LOCK = threading.Lock()


def _presets_path(cfg=None):
    return (cfg or {}).get('presets_file') or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'presets.json')


def load_presets(cfg=None):
    """presets.json에서 프리셋 목록 읽기 (파일 없거나 손상 시 [])."""
    try:
        fp = _presets_path(cfg)
        with open(fp, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('presets'), list):
            return [p for p in data['presets']
                    if isinstance(p, dict) and str(p.get('name') or '').strip()]
    except Exception:
        pass
    return []


def save_presets(cfg, presets):
    """프리셋 목록을 presets.json에 쓰기 (원자 교체로 파일 손상 방지)."""
    fp = _presets_path(cfg)
    tmp = fp + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'presets': presets}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, fp)


# ── HTTP 핸들러 (API + 프록시 + 정적 파일) ─────────────────────────────────

class WebHandler(BaseHTTPRequestHandler):
    server_version = 'LLMLauncher/2.0'
    node = None          # start_web_server()에서 설정
    cfg = {}
    web_dir = ''
    # False: /api·프록시 폴링을 앱 로그에 남기지 않음 (GUI 콘솔 위생용).
    #         정적 자산 요청(/, /app.js?v=N)은 여전히 기록 — 캐시 진단에 사용.
    log_all_requests = True

    def log_message(self, fmt, *args):
        # HTTP 요청을 노드 로그에 기록 (클라이언트 접근 진단: 어느 IP가 어떤 버전의 자산을 요청했는지)
        node = self.node
        if node is None:
            return
        try:
            if not self.log_all_requests:
                path = urlsplit(self.path).path
                if path.startswith('/api/') or path.startswith(PROXY_PREFIXES):
                    return
            node.log('[REQ %s] %s' % (self.client_address[0], fmt % args))
        except Exception:
            pass

    # ── 유틸 ──
    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _auth_ok(self):
        key = self.cfg.get('api_key') or ''
        if not key:
            return True
        q = parse_qs(urlsplit(self.path).query)
        provided = self.headers.get('X-Api-Key') or (q.get('api_key') or [''])[0]
        return provided == key

    def _read_body(self):
        te = (self.headers.get('Transfer-Encoding') or '').lower()
        if 'chunked' in te:
            return self._read_chunked_body()
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b''

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

    # ── 라우팅 ──
    def _route(self):
        path = urlsplit(self.path).path
        node = self.node
        try:
            if path.startswith('/api/'):
                if not self._auth_ok():
                    return self._send_json(401, {'error': 'API 키가 없거나 올바르지 않습니다 (X-Api-Key 헤더 또는 ?api_key=)'})
                return self._api(self.command, path)
            if path.startswith(PROXY_PREFIXES):
                if not self._auth_ok():
                    return self._send_json(401, {'error': 'API 키가 없거나 올바르지 않습니다 (X-Api-Key 헤더 또는 ?api_key=)'})
                if not (node.backend_port and node.process_running()):
                    return self._send_json(503, {'error': 'llama-server가 실행 중이 아닙니다. 모델 탭에서 먼저 기동하세요.'})
                return self._proxy(self.command, path)
            return self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._send_json(500, {'error': f'서버 오류: {e}'})
            except Exception:
                pass

    do_GET = _route
    do_POST = _route
    do_PUT = _route
    do_DELETE = _route
    do_PATCH = _route
    do_HEAD = _route
    do_OPTIONS = _route

    # ── 컨트롤 API ──
    def _api(self, method, path):
        node = self.node
        q = parse_qs(urlsplit(self.path).query)

        if path == '/api/health' and method in ('GET', 'HEAD'):
            return self._send_json(200, {'ok': True,
                                         'process': node.process_running(),
                                         'model_state': node.model_state})

        if path == '/api/lan-info' and method == 'GET':
            return self._send_json(200, {
                'host': self.cfg.get('host'),
                'port': self.cfg.get('port'),
                'ips': [{'ip': ip, 'tailscale': _is_tailnet_ip(ip)} for ip in get_all_ipv4()],
                'process': node.process_running(),
                'model_state': node.model_state,
            })

        if path == '/api/status' and method == 'GET':
            return self._send_json(200, node.status())

        if path == '/api/models' and method == 'GET':
            d = (q.get('dir') or [self.cfg.get('models_dir') or DEFAULT_MODELS_DIR])[0]
            return self._send_json(200, list_models(d))

        if path == '/api/launch' and method == 'POST':
            try:
                body = json.loads(self._read_body() or b'{}')
            except Exception:
                return self._send_json(400, {'error': '요청 본문이 유효한 JSON이 아닙니다'})
            node.log(f'[기동 요청] {self.client_address[0]} → model={body.get("model") or "(미지정 → 마지막 설정)"}')
            ok, err = node.launch(body)
            if ok:
                return self._send_json(200, {'ok': True,
                                             'message': '기동 요청 접수 — 모델 로드가 진행 중입니다. 상태 탭/로그 탭에서 확인하세요.'})
            node.log('[기동 실패] ' + (err or '알 수 없는 오류'))
            return self._send_json(400, {'ok': False, 'error': err})

        if path == '/api/stop' and method == 'POST':
            node.stop()
            return self._send_json(200, {'ok': True})

        if path == '/api/logs' and method == 'GET':
            try:
                n = int((q.get('lines') or ['300'])[0] or 300)
            except ValueError:
                n = 300
            n = max(1, min(3000, n))
            return self._send_json(200, {
                'lines': [{'seq': s, 'ts': t, 'line': l} for s, t, l in node.logs.tail(n)]})

        if path == '/api/logs/stream' and method == 'GET':
            return self._sse_logs()

        if path == '/api/presets' and method == 'GET':
            return self._send_json(200, {'presets': load_presets(self.cfg)})

        if path == '/api/presets' and method == 'POST':
            try:
                body = json.loads(self._read_body() or b'{}')
            except Exception:
                return self._send_json(400, {'error': '요청 본문이 유효한 JSON이 아닙니다'})
            name = str(body.get('name') or '').strip()
            if not name:
                return self._send_json(400, {'error': '프리셋 이름을 입력하세요'})
            if len(name) > 50:
                return self._send_json(400, {'error': '프리셋 이름은 50자 이내로 입력하세요'})
            p = {'name': name}
            for k in ('model', 'mmproj', 'ngl', 'ctx', 'ctk', 'ctv', 'np', 'n',
                      'server_exe', 'cli_exe'):
                p[k] = str(body.get(k) or '').strip()
            p['mtp'] = bool(body.get('mtp'))
            p['fa'] = bool(body.get('fa'))
            try:
                p['mtp_max'] = str(max(0, min(32, int(float(body.get('mtp_max') or 0)))))
            except (TypeError, ValueError):
                p['mtp_max'] = '0'
            # 컴프레셔 옵션 (자동/트리거비율/유지개수) — 프리셋에 함께 저장
            comp = body.get('compressor')
            if not isinstance(comp, dict):
                comp = {}

            def _cint(v, lo, hi, d):
                try:
                    return max(lo, min(hi, int(float(v))))
                except (TypeError, ValueError):
                    return d

            p['compressor'] = {
                'enabled': bool(comp.get('enabled', True)),
                'auto_trigger_pct': _cint(comp.get('auto_trigger_pct'), 10, 99, 75),
                'keep_last_msgs': _cint(comp.get('keep_last_msgs'), 1, 50, 6),
            }
            with PRESETS_LOCK:
                presets = [x for x in load_presets(self.cfg) if x.get('name') != name]
                presets.append(p)
                try:
                    save_presets(self.cfg, presets)
                except Exception as e:
                    return self._send_json(500, {'error': f'프리셋 저장 실패: {e}'})
            node.log(f'[프리셋] "{name}" 저장 (전체 {len(presets)}개)')
            return self._send_json(200, {'ok': True, 'presets': presets})

        if path == '/api/presets' and method == 'DELETE':
            name = str((q.get('name') or [''])[0])
            if not name:
                return self._send_json(400, {'error': '프리셋 이름이 필요합니다'})
            with PRESETS_LOCK:
                presets = [x for x in load_presets(self.cfg) if x.get('name') != name]
                try:
                    save_presets(self.cfg, presets)
                except Exception as e:
                    return self._send_json(500, {'error': f'프리셋 삭제 실패: {e}'})
            node.log(f'[프리셋] "{name}" 삭제 (전체 {len(presets)}개)')
            return self._send_json(200, {'ok': True, 'presets': presets})

        if path == '/api/compress' and method == 'POST':
            try:
                body = json.loads(self._read_body() or b'{}')
            except Exception:
                return self._send_json(400, {'error': '요청 본문이 유효한 JSON이 아닙니다'})
            messages = body.get('messages')
            if not isinstance(messages, list):
                return self._send_json(400, {'error': '"messages" 배열이 필요합니다'})
            if len(messages) < 2:
                return self._send_json(400, {'error': '최소 2개 이상의 메시지가 필요합니다'})
            keep_last = body.get('keep_last')
            if not isinstance(keep_last, int) or keep_last < 1:
                keep_last = (self.cfg.get('compressor') or {}).get('keep_last_msgs', 6)
            # compress_chat 메서드가 있는 경우 (LLMNode)
            if hasattr(node, 'compress_chat'):
                result = node.compress_chat(messages, keep_last=keep_last)
                code = 200 if result.get('ok') else 400
                return self._send_json(code, result)
            return self._send_json(501, {'ok': False, 'error': 'compress_chat 메서드를 찾을 수 없습니다'})

        if path == '/api/compressor':
            if method == 'GET':
                return self._send_json(200, self._compressor_cfg())
            if method == 'POST':
                try:
                    body = json.loads(self._read_body() or b'{}')
                except Exception:
                    return self._send_json(400, {'error': '요청 본문이 유효한 JSON이 아닙니다'})
                c = self._compressor_cfg()
                changed = False
                if 'enabled' in body:
                    c['enabled'] = bool(body['enabled'])
                    changed = True
                if 'auto_trigger_pct' in body:
                    try:
                        c['auto_trigger_pct'] = max(10, min(99, int(float(body['auto_trigger_pct']))))
                        changed = True
                    except (TypeError, ValueError):
                        pass
                if 'keep_last_msgs' in body:
                    try:
                        c['keep_last_msgs'] = max(1, min(50, int(float(body['keep_last_msgs']))))
                        changed = True
                    except (TypeError, ValueError):
                        pass
                self.cfg['compressor'] = c
                if changed:
                    node.log(f"[Compressor] 설정 변경: enabled={c['enabled']}, "
                             f"비율={c['auto_trigger_pct']}%, 유지={c['keep_last_msgs']}개")
                    if hasattr(node, 'save_config'):
                        try:
                            node.save_config(self.cfg)
                        except Exception:
                            pass
                return self._send_json(200, c)

        if path == '/api/events' and method == 'GET':
            return self._sse_events()

        return self._send_json(404, {'error': '존재하지 않는 API 엔드포인트입니다'})

    # ── SSE: 실시간 로그 ──
    def _sse_logs(self):
        node = self.node
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            hist = node.logs.tail(300)
            self._sse_write({'type': 'init', 'lines': [[s, t, l] for s, t, l in hist]})
            last = hist[-1][0] if hist else 0
            idle = 0
            while True:
                new = node.logs.since(last)
                if new:
                    idle = 0
                    for s, t, l in new:
                        self._sse_write({'type': 'log', 'seq': s, 'ts': t, 'line': l})
                        last = s
                else:
                    idle += 1
                    if idle % 15 == 0:
                        self.wfile.write(b': keepalive\n\n')
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _sse_write(self, obj):
        self.wfile.write(('data: ' + json.dumps(obj, ensure_ascii=False) + '\n\n').encode('utf-8'))

    # ── SSE: 서버 → 클라이언트 제어 이벤트 (컴프레셔 강제 요청 등) ──
    def _sse_events(self):
        """SSE 제어 이벤트 스트림 (/api/events).

        /api/logs/stream은 로그 텍스트용이고, 이 스트림은 제어 신호용이다.
        'compress' 이벤트: 서버가 컨텍스트 사용 비율(설정값) 초과를 감지했을 때
        발행 — 웹 PWA·GUI 클라이언트는 자신의 대화 기록을 압축한다.
        발행 스레드는 이 연결의 큐에만 put하고, wfile 쓰기는 이 핸들러 스레드에서만 수행.
        """
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            q = queue.Queue()

            def on_event(etype, data):
                q.put((etype, data))

            EVENT_BUS.subscribe(on_event)
            try:
                self.wfile.write(b': connected\n\n')
                self.wfile.flush()
                while True:
                    try:
                        etype, data = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b': ping\n\n')
                        self.wfile.flush()
                        continue
                    payload = json.dumps({'event': etype, 'data': data or {}}, ensure_ascii=False)
                    self.wfile.write(('event: ' + str(etype) + '\ndata: ' + payload + '\n\n').encode('utf-8'))
                    self.wfile.flush()
            finally:
                EVENT_BUS.unsubscribe(on_event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _compressor_cfg(self):
        """웹 서버의 컴프레셔 설정 (기본값 보정)."""
        c = dict(self.cfg.get('compressor') or {})
        c.setdefault('enabled', True)
        c.setdefault('auto_trigger_pct', 75)
        c.setdefault('keep_last_msgs', 6)
        return c

    # ── 프록시 → llama-server (GUI _ApiInspectProxyHandler 로직 재사용) ──
    def _proxy(self, method, path):
        node = self.node
        body = self._read_body()
        is_gen = path in GEN_PATHS
        if is_gen:
            req = None
            if body:
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        req = parsed
                except Exception:
                    req = None
            eff = (req or {}).get('reasoning_effort')
            temp = (req or {}).get('temperature')
            top_p = (req or {}).get('top_p')
            # 요청 키/크기 로깅 (디버깅용)
            req_keys = list((req or {}).keys())
            node.log(f'[API 수신] {self.client_address[0]} {path} body={len(body)}B keys={req_keys}'
                     + (f' reasoning_effort={eff}' if eff else '')
                     + (f' temperature={temp:g}' if isinstance(temp, (int, float)) else '')
                     + (f' top_p={top_p:g}' if isinstance(top_p, (int, float)) else ''))
            node.gen_request_started()
        elif self.log_all_requests:
            node.log(f'[API 수신] {self.client_address[0]} {path}')

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ('host', 'transfer-encoding', 'connection',
                                        'keep-alive', 'proxy-connection')}
        if body:
            headers['Content-Length'] = str(len(body))
        def _forward():
            c = http.client.HTTPConnection('127.0.0.1', node.backend_port, timeout=3600)
            c.request(method, path, body=(body if body else None), headers=headers)
            return c, c.getresponse()

        try:
            conn, resp = _forward()
        except Exception as e:
            if is_gen:
                node.gen_request_finished()
            node.log(f'[API 수신] {path} 포워딩 실패: {e}')
            try:
                self.send_error(502, 'backend unreachable')
            except Exception:
                pass
            return

        err_body = None
        if resp.status >= 400:
            err_body = resp.read()
            if not err_body:
                # 빈 바디 에러 = 백엔드의 상세 없는 에러 (일시적 상태 가능).
                # 참고: llama.cpp의 실제 컨텍스트 오버플로우는 JSON 바디
                # (exceed_context_size_error)를 반환하므로 빈 바디는 포화가 아님.
                # → 1회 자동 재시도 (성공하면 클라이언트에 그대로 전달).
                node.log(f'[경고] {path} → {resp.status} 빈 바디 에러 — 1회 자동 재시도')
                conn.close()
                try:
                    conn, resp = _forward()
                except Exception as e:
                    if is_gen:
                        node.gen_request_finished()
                    node.log(f'[경고] 재시도 포워딩 실패: {e}')
                    try:
                        self.send_error(502, 'backend unreachable on retry')
                    except Exception:
                        pass
                    return
                if resp.status >= 400:
                    err_body = resp.read()
                    if not err_body:
                        # 재시도에도 빈 바디 에러 → 원인을 알 수 있는 JSON으로 반환
                        node.log(f'[오류] {path} 재시도 후에도 {resp.status} 빈 바디 에러 — '
                                 f'백엔드 상태 확인 필요 (모델 재시작 권장)')
                        err_json = json.dumps({
                            "error": {
                                "message": "llama-server rejected the request without details "
                                           "(empty error body). This is a backend error, not a "
                                           "context overflow. Please retry; if it persists, "
                                           "restart the model from the launcher.",
                                "type": "backend_error",
                                "code": resp.status
                            }
                        }, ensure_ascii=False).encode('utf-8')
                        conn.close()
                        if is_gen:
                            node.gen_request_finished()
                        try:
                            self.send_response(resp.status)
                            self.send_header('Content-Type', 'application/json')
                            self.send_header('Content-Length', str(len(err_json)))
                            self.send_header('Connection', 'close')
                            self.end_headers()
                            self.wfile.write(err_json)
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            pass
                        return

        try:
            self.send_response(resp.status)
        except Exception:
            conn.close()
            if is_gen:
                node.gen_request_finished()
            return
        # ── 에러 응답(4xx/5xx): 바디를 읽어 로깅 후 클라이언트로 반환 ──
        if resp.status >= 400:
            resp_headers = dict(resp.getheaders())
            node.log(f'[API 응답 오류] {path} → {resp.status} body={len(err_body)}B '
                     f'headers={resp_headers}')
            if err_body:
                node.log(f'[API 응답 오류 바디] {err_body.decode("utf-8", errors="replace")[:500]}')
                # 정상 에러 바디가 있으면 그대로 전달
                # (컨텍스트 오버플로우: exceed_context_size_error JSON — 클라이언트가 확인)
                self.send_header('Content-Type', resp.getheader('Content-Type') or 'application/json')
                self.send_header('Content-Length', str(len(err_body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                try:
                    self.wfile.write(err_body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
            conn.close()
            if is_gen:
                node.gen_request_finished()
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
            conn.close()
            if is_gen:
                node.gen_request_finished()

    # ── 정적 파일 (Web UI) ──
    _MIME = {
        '.html': 'text/html; charset=utf-8',
        '.js': 'text/javascript; charset=utf-8',
        '.css': 'text/css; charset=utf-8',
        '.json': 'application/json',
        '.webmanifest': 'application/manifest+json',
        '.png': 'image/png',
        '.svg': 'image/svg+xml',
        '.ico': 'image/x-icon',
    }

    def _static(self, path):
        if path in ('', '/'):
            path = '/index.html'
        rel = path.lstrip('/')
        fp = os.path.realpath(os.path.join(self.web_dir, rel))
        web_root = os.path.realpath(self.web_dir)
        if not fp.startswith(web_root) or not os.path.isfile(fp):
            return self._send_json(404, {'error': 'not found'})
        with open(fp, 'rb') as f:
            data = f.read()
        ext = os.path.splitext(fp)[1].lower()
        self.send_response(200)
        self.send_header('Content-Type', self._MIME.get(ext, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache' if ext == '.html' else 'max-age=300')
        self.end_headers()
        self.wfile.write(data)


# ── Job Object: 부모 프로세스 종료 시 자식 프로세스 자동 종료 (고아 방지) ──
# KILL_ON_JOB_CLOSE: Job 핸들이 닫히는 순간(정상 종료, 작업 관리자 강제 종료,
# 크래시 모두 포함) Job에 속한 프로세스가 OS에 의해 강제 종료된다.
# GUI(llm_launcher.py)와 헤드리스 서버(llm_server.py)가 공유한다.
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


def create_kill_on_close_job():
    """KILL_ON_JOB_CLOSE 속성 Job Object 생성 (Windows만, 실패 시 None).
    반환 핸들은 절대 닫지 않아야 한다 — 부모 프로세스 종료 시 함께 닫히며,
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


def assign_process_to_job(job, proc):
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


class HTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer + 연결 단절 계열 예외의 traceback 스팸 억제.

    클라이언트(모바일 등)가 연결을 끊으면 handle_one_request 단계에서
    ConnectionAbortedError/ConnectionResetError 등이 발생하는데, 기본
    handle_error는 전체 traceback을 stderr에 기록한다. 정상적인 연결
    종료이므로 조용히 무시하고, 그 외 예외는 기본 동작으로 남긴다.
    """
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError, ConnectionError)):
            return
        super().handle_error(request, client_address)


# ── 서버 기동 헬퍼 ──────────────────────────────────────────────────────────

def start_web_server(node, host, port, web_dir, cfg, log_all_requests=True):
    """node 인터페이스로 웹 서버 기동 (데몬 스레드). OSError(포트 점유) 발생 가능."""
    WebHandler.node = node
    WebHandler.cfg = cfg
    WebHandler.web_dir = web_dir
    WebHandler.log_all_requests = log_all_requests
    server = HTTPServer((host, port), WebHandler)
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.2},
                     daemon=True, name='web-server').start()
    return server




