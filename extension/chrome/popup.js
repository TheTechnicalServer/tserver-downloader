/* TServer Downloader - popup. */
'use strict';

const statusEl = document.getElementById('status');
const errorEl = document.getElementById('error');
const adblockEl = document.getElementById('adblock');
const interceptEl = document.getElementById('intercept');
const disableSiteEl = document.getElementById('disableSite');
const blockedEl = document.getElementById('blocked');
const mediaEl = document.getElementById('media');

function hostOf(url) {
  try { return new URL(url).hostname.toLowerCase(); } catch (e) { return ''; }
}

async function refresh() {
  let status;
  try {
    status = await chrome.runtime.sendMessage({ type: 'get_status' });
  } catch (e) {
    status = { connected: false, nativeHostError: String(e) };
  }
  if (status && status.connected) {
    statusEl.textContent = 'Connected to TServer Downloader ✓';
    statusEl.className = 'status ok';
    errorEl.textContent = '';
  } else {
    statusEl.textContent = 'Not connected — is the desktop app installed?';
    statusEl.className = 'status bad';
    errorEl.textContent = status && status.nativeHostError ? status.nativeHostError : '';
  }
  if (status) {
    adblockEl.checked = !!status.adblockEnabled;
    interceptEl.checked = !!status.interceptEnabled;
    blockedEl.textContent = String(status.blockedCount || 0);
  }
  const { mediaCount = 0 } = await chrome.storage.local.get('mediaCount');
  mediaEl.textContent = String(mediaCount);
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tabs[0] && tabs[0].url) {
    const host = hostOf(tabs[0].url);
    const { disabledSites = [] } = await chrome.storage.sync.get('disabledSites');
    disableSiteEl.checked = disabledSites.includes(host);
    disableSiteEl.dataset.host = host;
  }
}

async function saveSettings(patch) {
  await chrome.runtime.sendMessage({ type: 'set_settings', settings: patch });
  await refresh();
}

adblockEl.addEventListener('change', () => saveSettings({ adblockEnabled: adblockEl.checked }));
interceptEl.addEventListener('change', () => saveSettings({ interceptEnabled: interceptEl.checked }));
disableSiteEl.addEventListener('change', async () => {
  const host = disableSiteEl.dataset.host;
  if (!host) return;
  const { disabledSites = [] } = await chrome.storage.sync.get('disabledSites');
  const next = disableSiteEl.checked
    ? Array.from(new Set([...disabledSites, host]))
    : disabledSites.filter((d) => d !== host);
  await saveSettings({ disabledSites: next });
});
document.getElementById('settings').addEventListener('click', () => {
  chrome.runtime.openOptionsPage ? chrome.runtime.openOptionsPage() : chrome.tabs.create({ url: 'popup.html' });
});

refresh();
