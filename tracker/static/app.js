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
    if (s.block && s.block.blocked) parts.push('🚫 已被擋，停止中');
    if (s.block && s.block.count_today) parts.push(`今日被擋 ${s.block.count_today} 次`);
    // 顯示「實際在執行的那一個」，不是 id 最大的那一個——後者會讓正在跑的
    // 長任務隱形，看起來像整個系統卡住。
    const j = s.running_job || s.job;
    if (j) parts.push(`任務#${j.id} ${j.state} ${j.progress_done}/${j.progress_total}`);
    if (s.queued_jobs > 1) parts.push(`佇列 ${s.queued_jobs - 1}`);
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
    const warn = s.block && s.block.warn
      ? `<br>⚠️ 今日已被擋 ${s.block.count_today} 次，建議停止排程並檢查帳號狀態。` : '';
    if (s.job && (s.job.state === 'paused_captcha' || s.job.state === 'paused_login')) {
      banner.hidden = false;
      banner.innerHTML = `⚠️ ${s.job.message}　` +
        `<button onclick="api('/api/jobs/${s.job.id}/resume',{}).then(()=>location.reload())">已處理，續跑</button>` +
        (s.block && s.block.probe_fails ? `　<span class="dim">自動探測已失敗 ${s.block.probe_fails} 次</span>` : '') +
        warn;
      document.title = '⚠️ 需要人工處理';
    } else if (s.status_error) {
      banner.hidden = false;
      banner.innerHTML = `⚠️ 狀態查詢異常：${s.status_error}`;
    } else if (warn) {
      banner.hidden = false;
      banner.innerHTML = warn;
    } else {
      banner.hidden = true;
    }
  }
}
pollStatus();
setInterval(pollStatus, 3000);
