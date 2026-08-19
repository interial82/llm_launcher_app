/* ── LLM Launcher PWA — Service Worker ──
   - 정적 자산(앱 셸): 캐시 + 백그라운드 갱신 (stale-while-revalidate)
   - 페이지 내비게이션: 네트워크 우선, 서버 미응답 시 "연결 실패" 폴백 페이지
   - /api, /v1 (실시간 데이터): 캐시하지 않음
   */
'use strict';
const CACHE = 'llm-launcher-v1';

const OFFLINE_PAGE = `<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLM Launcher — 연결 실패</title>
<style>
  body{font-family:-apple-system,'Segoe UI',sans-serif;background:#0f1115;color:#e6e6e6;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
  .card{max-width:420px;padding:32px;text-align:center}
  h1{font-size:18px} p{color:#9aa0a6;font-size:14px;line-height:1.6}
  button{margin-top:16px;padding:10px 22px;border:0;border-radius:8px;
         background:#4a86e8;color:#fff;font-size:14px}
</style></head><body><div class="card">
<h1>⚠️ LLM Launcher에 연결할 수 없습니다</h1>
<p>PC의 LLM Launcher 서버가 실행 중인지, LAN/Tailscale 네트워크가 연결되어 있는지 확인한 뒤 다시 시도하세요.</p>
<button onclick="location.reload()">다시 시도</button>
</div></body></html>`;

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  let u;
  try { u = new URL(req.url); } catch (err) { return; }
  if (u.origin !== self.location.origin) return;
  // 실시간 데이터(API/프록시)는 캐시 금지 — 항상 서버로
  if (u.pathname.startsWith('/api') || u.pathname.startsWith('/v1')) return;

  if (req.mode === 'navigate') {
    // 페이지 로드: 네트워크 우선, 서버가 안 열면 연결 실패 화면
    e.respondWith(
      fetch(req).then(r => {
        if (r.ok) {
          const c = r.clone();
          caches.open(CACHE).then(x => x.put(req, c));
        }
        return r;
      }).catch(() => new Response(OFFLINE_PAGE, {
        status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' },
      }))
    );
    return;
  }

  // 정적 자산: 캐시가 있으면 즉시 반환 + 백그라운드 갱신, 없으면 네트워크 대기
  e.respondWith(
    caches.open(CACHE).then(async cache => {
      const cached = await cache.match(req);
      const network = fetch(req).then(r => {
        if (r.ok) cache.put(req, r.clone());
        return r;
      }).catch(() => cached);
      return cached || network;
    })
  );
});