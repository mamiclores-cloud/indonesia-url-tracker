async function api(url, data) {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data || {}),
  });
  try { return await r.json(); } catch (e) { return {ok: false, error: 'HTTP ' + r.status}; }
}

async function pollStatus() {
  let s;
  try { s = await fetch('/api/status').then(r => r.json()); } catch (e) { return; }
  const bar = document.getElementById('statusbar');
  if (bar) {
    const parts = [];
    parts.push(s.chrome.chrome_alive ? 'Chrome ✅' : 'Chrome ⚫');
    parts.push(s.google.ok ? 'Google ✅' : 'Google ⚫');
    parts.push(s.dry_run ? 'dry-run' : '⚠️ 直接寫入');
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
