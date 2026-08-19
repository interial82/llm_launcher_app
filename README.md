# 🤖 LLM Launcher

llama.cpp(`llama-server` / `llama-cli`)를 **한 번에 기동·관리**하고, **스마트폰(아이폰 PWA)에서 원격 제어**할 수 있는 로컬 LLM 런처/컨트롤 앱입니다.

- **데스크톱 GUI**(Tkinter) + **헤드리스 서버**(무음 콘솔) 두 가지 실행 모드
- 앱 기동과 동시에 **통합 웹 서버**가 뜨면서 → ① 모바일 웹 UI(PWA) ② 컨트롤 API ③ **OpenAI 호환 API 프록시**(VSCode 등 외부 클라이언트용)를 함께 제공합니다
- 외부 의존성 **0** — Python 표준 라이브러리만 사용합니다 (`pip install` 불필요)

---

## ✨ 주요 기능

| 구분 | 내용 |
|---|---|
| **기동 옵션** | NGL, 컨텍스트(32K~256K), Draft MTP(스펙추적 디코딩, `--spec-type draft-mtp` + `--spec-draft-n-max`), FlashAttn, KV 캐시 양자화(CTK/CTV: f32·f16·bf16·q8_0·q4_0·q4_1·iq4_nl·q5_0·q5_1), NP, N(512~4096) |
| **Vision** | mmproj 선택, `--image-min-tokens 1024 / --image-max-tokens 4096` 자동 적용 — 채팅에 **이미지·영상 첨부** 지원 |
| **추론 노력도** | `reasoning_effort`(minimal / low / medium / high / xhigh) — 프록시에서 요청 로깅 |
| **TTL 자동 언로드** | 무요청 상태가 TTL(분)을 넘으면 모델이 VRAM에서 언로드(수면), 다음 요청 시 자동 재로드 — GPU VRAM 절약 |
| **컨텍스트 컴프레셔** | 컨텍스트 사용 비율이 임계치(%)를 넘으면 LLM으로 이전 대화를 **system 요약**으로 자동 압축 (최근 N개 유지, 수동 🗜 압축 버튼도 제공) |
| **프리셋** | 모델+옵션+컴프레셔 설정을 이름으로 저장/적용/삭제 (`presets.json` — GUI·헤드리스·웹 UI 공유) |
| **모니터링** | GPU 율, GPU VRAM, 시스템 RAM, 컨텍스트 사용량 게이지 + 스파크라인, 누적 토큰(입력/출력), 모델 상태(로딩/실행/수면) |
| **모바일 PWA** | 상태·모델·채팅·로그 4탭, 아이폰 홈화면에 설치 가능, Service Worker(오프라인 폴백) |
| **원격 제어** | LAN / Tailscale IP 자동 감지·표시, API 키 인증(`X-Api-Key` 헤더 또는 `?api_key=`) |
| **OpenAI 호환** | `/v1/chat/completions` 등 — VSCode·다른 클라이언트가 GUI/서버의 모델을 바로 사용 |
| **고아 방지** | Windows Job Object(`KILL_ON_JOB_CLOSE`) — 앱 종료(강제종료·크래시 포함) 시 자식 llama 프로세스 자동 종료. 기동 시 잔여 고아 프로세스 스윕 |

---

## 📁 프로젝트 구조

```
llm_launcher_app/
├── llm_launcher.py      # 데스크톱 GUI (Tkinter) + 통합 웹 서버(8080) 내장
├── llm_server.py        # 헤드리스 서버 — 스마트폰 PWA 원격 제어용
├── launcher_web.py      # 공통 웹 서버 모듈 (컨트롤 API + OpenAI 프록시 + PWA 서빙)
├── web/                 # PWA 웹 UI (정적 자산)
│   ├── index.html       #   상태 / 모델 / 채팅 / 로그 탭 + 설정 모달
│   ├── app.js           #   UI 로직 (SSE 로그, 게이지, 채팅, 프리셋)
│   ├── style.css
│   ├── sw.js            #   Service Worker (캐시 + 연결 실패 폴백)
│   ├── manifest.webmanifest
│   └── icons/           #   PWA 아이콘 (192/512/apple-touch)
├── config.json          # 헤드리스 서버 설정 + 공통 설정 (compressor, last_launch 등)
├── presets.json         # 프리셋 (모델+옵션+컴프레셔)
└── .gitignore
```

**아키텍처**

```
 스마트폰(아이폰 PWA) / VSCode / 브라우저
        │  http://<PC IP>:8080
        ▼
 ┌──────────────────────────────────────────────────────┐
 │  통합 웹 서버 (launcher_web.WebHandler)               │
 │   /        → PWA 정적 서빙 (web/)                     │
 │   /api/*   → 컨트롤 API (기동/중지/상태/모델/로그/SSE) │
 │   /v1/* …  → OpenAI 호환 프록시 ──────────────┐       │
 └──────────────────────────────────────────────┼───────┘
                                                 ▼
                              llama-server (127.0.0.1:<동적 포트>)
                              (GUI: llm_launcher.py / 헤드리스: llm_server.py)
```

---

## 🚀 실행 방법

### 요구 사항

- **Python 3.10+** (Tkinter 포함 — `python llm_launcher.py`만 있으면 됨, 별도 설치 없음)
- **llama.cpp** 이진 파일 (`llama-server.exe`, `llama-cli.exe` — CUDA 빌드 권장, 예: `llama-b10453-bin-win-cuda-13.3-x64`)
- **GGUF 모델** 파일 + (Vision 사용 시) `mmproj-*.gguf`
  - 기본 모델 탐색 위치: `~/.lmstudio/models` (`config.json` `models_dir`으로 변경 가능)

> `llama-server`/`llama-cli` 경로는 **자동 탐색하지 않습니다** — GUI에서 "찾기"로 선택하거나 `config.json` / 프리셋에 명시합니다.

### 1) 데스크톱 GUI

```bat
python llm_launcher.py
```

1. 기동 시 포트 8080 충돌·고아 프로세스를 확인하고 웹 서버를 엽니다
2. **실행 & 설정** 탭에서 프리셋을 적용하거나 모델/옵션을 직접 지정 → **시작**
   - 모드: `CLI(채팅)` — llama-cli 인터랙티브 / `서버(네트워크)` — OpenAI API 서버
3. 콘솔에 LAN/Tailscale 접속 주소가 표시됩니다
4. **채팅** 탭에서 바로 대화(이미지·영상 첨부 가능)

### 2) 헤드리스 서버 (스마트폰 PWA 원격 제어)

```bat
python llm_server.py
python llm_server.py --host 0.0.0.0 --port 8091 --key "my-secret"
```

| 옵션 | 설명 |
|---|---|
| `--host` | 바인딩 호스트 (기본: `config.json` 또는 `0.0.0.0`) |
| `--port` | 포트 (기본: `config.json` 또는 `8080`) |
| `--key` | API 키 (`config.json`의 `api_key` 덮어쓰기) |
| `--no-sweep` | 기동 시 고아 `llama*.exe` 정리 스킵 (llama-server를 별도로 직접 실행 중일 때) |

> ⚠️ **GUI와 헤드리스를 동시에 실행하지 마세요.** 둘은 각각 **별개의 모델 서버**가 되며 포트가 충돌합니다.
> 모바일 + VSCode가 **같은 1개 모델**을 공유하려면 **GUI만** 실행하세요 (GUI가 PWA·OpenAI 웹서버를 이미 내장).

### 3) 접속

```
- LAN   : http://<PC LAN IP>:8080
- 외부   : http://<PC Tailscale IP (100.x.y.z)>:8080   (PC/아이폰 모두 Tailscale 가입)
```

- 기동 로그(콘솔)에 감지된 IP가 자동으로 표시됩니다
- **아이폰**: Safari에서 열기 → 공유 → "홈 화면에 추가" → PWA로 설치
- 설정 모달(⚙)에서 서버 주소/API 키를 변경하고 **연결 테스트**가 가능합니다

---

## 🔌 OpenAI 호환 API

모델 기동 후 다음 주소를 사용할 수 있습니다 (VSCode, openai SDK, curl 등).

```
Base URL : http://<PC IP>:8080/v1
모델 ID  : <모델 .gguf 파일명>   (또는 "default")
API 키   : 헤드리스 서버 api_key 설정 시 동일 값 필요 (GUI는 미설정)
```

```python
# 예: Python openai SDK
from openai import OpenAI

client = OpenAI(
    base_url="http://192.168.0.12:8080/v1",
    api_key="my-secret",   # config.json의 api_key와 동일 (미설정 시 아무 값)
)

resp = client.chat.completions.create(
    model="Qwen3.8-27B-IQ4_XS.gguf",
    messages=[{"role": "user", "content": "안녕하세요"}],
    extra_body={"reasoning_effort": "medium"},   # 추론 노력도 (선택)
)
print(resp.choices[0].message.content)
```

프록시 대상 경로: `/v1/*`, `/completions`, `/tokenize`, `/metrics`, `/slots`, `/model`, `/health`
— `reasoning_effort` 값 로깅과 TTL(자동 언로드) 추적도 프록시에서 수행됩니다.

---

## 📡 컨트롤 API

> `api_key`가 설정된 경우(헤드리스) 모든 `/api/*`, 프록시 요청에
> `X-Api-Key: <키>` 헤더 또는 `?api_key=<키>` 쿼리가 필요합니다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/health` | 프로세스/모델 상태 스냅샷 |
| GET | `/api/lan-info` | 바인딩 호스트/포트, 감지된 LAN·Tailscale IP 목록 |
| GET | `/api/status` | 전체 상태 (모델, GPU, RAM, 컨텍스트, 토큰, TTL, 컴프레셔 설정…) |
| GET | `/api/models?dir=<경로>` | 모델 디렉터리 탐색 (`.gguf`/`mmproj` 파일 목록 + 상위 경로) |
| POST | `/api/launch` | 모델 기동 `{model, mmproj?, ngl, ctx, fa, ctk, ctv, np, mtp, mtp_max, vision, ttl_min, server_exe?}` |
| POST | `/api/stop` | llama-server 중지 |
| GET | `/api/logs?lines=300` | 최근 로그 (시퀀스 번호 포함) |
| GET | `/api/logs/stream` | **SSE** — 실시간 로그 스트림 |
| GET | `/api/events` | **SSE** — 상태 변경 이벤트 |
| GET | `/api/presets` | 프리셋 목록 |
| POST | `/api/presets` | 프리셋 저장 (이름 동일 시 덮어씀) |
| DELETE | `/api/presets?name=<이름>` | 프리셋 삭제 |
| POST | `/api/compress` | 대화 압축 `{messages: [...], keep_last?}` → 요약+최근 N개 반환 |
| GET | `/api/compressor` | 컨텍스트 컴프레셔 설정 |
| POST | `/api/compressor` | 컴프레셔 설정 변경 `{enabled, auto_trigger_pct, keep_last_msgs}` (config.json 영속화) |

**모델 상태** : `loading` → `loaded` ⇄ `sleeping` (TTL 수면) → (중지 시 `null`)

---

## ⚙️ 설정

### `config.json`

```jsonc
{
  "host": "0.0.0.0",
  "port": 8091,              // 헤드리스 서버 포트 (기본 8080, --port로 덮어쓰기 가능)
  "api_key": "",             // 비워두면 인증 없음. 설정 시 X-Api-Key 헤더 또는 ?api_key= 필요
  "server_exe": "",          // llama-server.exe 경로 (빈 값이면 기동 옵션에서 지정)
  "models_dir": "",          // 빈 값이면 ~/.lmstudio/models
  "compressor": {            // 컨텍스트 컴프레셔 (GUI와 공유)
    "enabled": true,
    "auto_trigger_pct": 80,  // 컨텍스트 사용 비율(%)이 이 값을 넘으면 자동 압축
    "keep_last_msgs": 6      // 압축 시 최근 유지 메시지 수
  },
  "defaults": { ... },       // 헤드리스 기본 기동 옵션
  "last_launch": { ... }     // 마지막 기동 설정 (자동 기록/복원)
}
```

### `presets.json`

프리셋 하나당: `name`, `model`, `mmproj`, `ngl`, `ctx`, `mtp`, `fa`, `ctk`, `ctv`, `np`, `n`, `mtp_max`, `server_exe`, `cli_exe`, `compressor`
— GUI·헤드리스·웹 UI가 **같은 파일**을 공유하므로 어디서 저장해도 어디서 적용할 수 있습니다.

---

## 🎛 기동 옵션

| 옵션 | 항목 | 값 |
|---|---|---|
| **NGL** | GPU 오프로드 계층 수 | `999`(모두) / `0`(CPU) / 자유 |
| **Context** | 컨텍스트 길이 | 32K ~ 256K (K 단위) |
| **Draft MTP** | 스펙추적 디코딩 | on/off — MTP 지원 모델에서 `--spec-type draft-mtp` |
| **MTP max** | draft 토큰 상한 | `--spec-draft-n-max` (0=미사용, 1~32) |
| **FlashAttn** | 플래시 어텐션 | on/off (`-fa`) |
| **CTK / CTV** | KV 캐시 양자화 | f32, f16, bf16, q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1 |
| **NP** | 병렬 슬롯 수 | 기본 1 |
| **N** | 최대 생성 토큰 | 512 ~ 4096 (512 단위) |
| **Vision** | 이미지/영상 분석 | on/off — mmproj 자동 포함 |
| **추론 노력도** | reasoning_effort | minimal / low / medium / high / xhigh |
| **TTL** | 자동 언로드 | 0=해제, N분 — 무요청 시 VRAM 해제(수면), 요청 시 재로드 |

---

## 🛡 안정성 관련 동작

- **고아 프로세스 방지**: llama 프로세스는 Windows Job Object에 등록되어 앱이 종료(강제종료·크래시 포함)되면 OS가 함께 종료시킵니다. 기동 시에도 이전 인스턴스 잔여 `llama*.exe`를 스윕합니다 (헤드리스는 `--no-sweep`로 스킵).
- **포트 충돌**:
  - GUI — 기동 시 8080이 점유 중이면 기존 인스턴스(PID 표시)를 종료하고 진행할지 확인
  - 헤드리스 — 즉시 종료(fail-fast)하고 안내 출력
- **설정 원자적 쓰기**: `config.json`/`presets.json`은 tmp 파일 + `os.replace`로 저장해 쓰기 중 크래시에 기존 파일이 깨지지 않습니다.
- **프록시 우회 방지**: 서버 모드에서 llama-server는 `127.0.0.1`에만 바인딩되고, 모든 외부 요청이 통합 웹 서버 프록시를 경유합니다 (reasoning_effort 로깅·인증·TTL 추적 적용).

---

## 🩺 문제 해결

| 증상 | 확인 사항 |
|---|---|
| "포트 8080이 이미 사용 중" | 기존 GUI/헤드리스 인스턴스를 종료 (작업 관리자에서 `python` 프로세스 확인) |
| llama-server 기동 실패 | `server_exe`/`cli_exe` 경로, CUDA DLL(빌드 폴더의 `*.dll`), 모델 파일 존재 확인 |
| PWA가 연결되지 않음 | PC와 스마트폰이 같은 LAN/Tailscale인지, 기동 로그의 IP가 올바른지 확인 |
| "llama-server가 실행 중이 아닙니다" (503) | 모델 탭에서 먼저 **기동**한 뒤 API 호출 |
| 모델 로딩이 느림 | 컨텍스트 축소, `CTK/CTV` 양자화 유지, NGL 확인. 수면(sleeping) 상태면 재로딩 중일 수 있음 |

---

## 📝 참고

- llama.cpp은 [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) 프로젝트의 이진 파일을 사용합니다
- 웹 서버는 표준 라이브러리 `http.server`(ThreadingHTTPServer) 기반 — 별도 웹 프레임워크/빌드 과정 없음

