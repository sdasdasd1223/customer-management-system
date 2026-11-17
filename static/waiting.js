'use strict';
const meta = name => document.querySelector(`meta[name="${name}"]`)?.content || '';
const base = meta('app-base-path');
const statusNode = document.querySelector('#waiting-status');
let checking = false;
async function checkStatus() {
  if (checking || document.hidden) return;
  checking = true;
  try {
    const response = await fetch(`${base}/api/v1/auth/me`, {cache:'no-store'});
    if (response.status === 401) { location.replace(`${base}/login?reason=expired`); return; }
    if (!response.ok) throw new Error('暫時無法檢查身分，請稍後重試。');
    const result = await response.json();
    if (result.approved) { location.replace(meta('next-url') || `${base}/`); return; }
    statusNode.textContent = '尚未分配身分，此頁每 10 秒自動檢查。';
  } catch (error) { statusNode.textContent = error.message; }
  finally { checking = false; }
}
document.querySelector('#check-status').addEventListener('click', checkStatus);
document.querySelector('#waiting-logout').addEventListener('click', async event => {
  event.target.disabled = true;
  try {
    const response = await fetch(`${base}/api/v1/auth/logout`, {method:'POST', headers:{'X-CSRF-Token':meta('csrf-token')}});
    if (response.ok || response.status === 401) location.replace(`${base}/login?reason=logged_out`);
    else throw new Error('登出未完成，請重新整理後再試。');
  } catch(error) { statusNode.textContent = error.message; event.target.disabled = false; }
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) checkStatus(); });
setInterval(checkStatus, 10000);
checkStatus();
