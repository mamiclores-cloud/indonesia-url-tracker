async function api(url, data) {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data || {}),
  });
  try { return await r.json(); } catch (e) { return {ok: false, error: 'HTTP ' + r.status}; }
}

// Server-rendered data pages don't live-update; when a job we were watching
// finishes, reload once so new prices/statuses/candidates show up.
const DATA_PAGE = /^\/(products|product\/|review)/.test(location.pathname);
let watchedJobId = null;
let reloadedForJob = null;
const ACTIVE = ['running', 'pending', 'paused_captcha', 'paused_login', 'paused_user'];

async function pollStatus() {
  let s;
  try { s = await fetch('/api/status').then(r => r.json()); } catch (e) { return; }

  if (DATA_PAGE) {
    if (s.job && ACTIVE.includes(s.job.state)) {
      watchedJobId = s.job.id;
    } else if (watchedJobId && reloadedForJob !== watchedJobId) {
      reloadedForJob = watchedJobId;   // guard against reload loop
      location.reload();
      return;
    }
  }

  const bar = document.getElementById('statusbar');
  if (bar) {
    const parts = [];
    parts.push(s.chrome.chrome_alive ? 'Chrome ✅' : 'Chrome ⚫');
    parts.push(s.google.ok ? 'Google ✅' : 'Google ⚫');
    parts.push(s.dry_run ? 'dry-run' : '⚠️ 直接寫入');
    if (s.pacing_safe_until) parts.push(`🐢 保守節奏至 ${s.pacing_safe_until.slice(5, 16)}`);
    if (s.job) parts.push(`任務#${s.job.id} ${s.job.state} ${s.job.progress_done}/${s.job.progress_total}`);
    bar.textContent = parts.join('　');
  }
  const badge = document.getElementById('nav-review-badge');
  if (badge) {
    const n = (s.pending_writes || 0) + (s.proposed_candidates || 0);
    badge.hidden = n === 0;
    badge.textContent = n;
  }
  const banner = document.getElementById('banner');
  if (banner) {
    if (s.job && (s.job.state === 'paused_captcha' || s.job.state === 'paused_login')) {
      banner.hidden = false;
      banner.innerHTML = `⚠️ ${s.job.message}　` +
        `<button onclick="api('/api/jobs/${s.job.id}/resume',{}).then(()=>location.reload())">已處理，續跑</button>`;
      document.title = '⚠️ 需要人工處理';
    } else {
      banner.hidden = true;
    }
  }
}
pollStatus();
setInterval(pollStatus, 3000);
