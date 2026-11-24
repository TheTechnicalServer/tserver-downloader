/* TServer Downloader - MV3 service worker background script. */
'use strict';

const NATIVE_HOST = 'com.tserver.downloader';
const ALLOWLISTED_HOST = 'thetechnicalserver.blogspot.com';
const INTERCEPT_EXTENSIONS = new Set([
  'mp4', 'mkv', 'webm', 'mov', 'avi', 'zip', 'rar', '7z', 'tar', 'gz',
  'pdf', 'exe', 'msi', 'iso', 'img', 'dmg', 'mp3', 'flac', 'wav', 'ogg',
  'epub', 'apk', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'torrent'
]);

const DEFAULT_SETTINGS = {
  adblockEnabled: true,
  interceptEnabled: true,
  disabledSites: []
};

async function getSettings() {
  const stored = await chrome.storage.sync.get(DEFAULT_SETTINGS);
  return { ...DEFAULT_SETTINGS, ...stored };
}

function isAllowlisted(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host === ALLOWLISTED_HOST || host.endsWith('.' + ALLOWLISTED_HOST);
  } catch (e) { return false; }
}

function isSiteDisabled(url, settings) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return (settings.disabledSites || []).some(
      (d) => host === d || host.endsWith('.' + d));
  } catch (e) { return false; }
}

function filenameFromUrl(url) {
  try {
    const path = new URL(url).pathname;
    const name = decodeURIComponent(path.split('/').pop() || '');
    return name || null;
  } catch (e) { return null; }
}

function isDownloadableUrl(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
    const ext = (parsed.pathname.split('.').pop() || '').toLowerCase().split('?')[0];
    return INTERCEPT_EXTENSIONS.has(ext);
  } catch (e) { return false; }
}

/** Send a download request to the desktop app via the native host. */
function sendToNativeHost(message) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(NATIVE_HOST, message, (response) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: chrome.runtime.lastError.message });
        } else if (response && response.status === 'ok') {
          resolve({ ok: true, response });
        } else {
          resolve({ ok: false, error: (response && response.message) || 'native host error' });
        }
      });
    } catch (e) {
      resolve({ ok: false, error: String(e) });
    }
  });
}

/** Fallback: localhost IPC (POST JSON to the app's ephemeral port). Not
 *  used unless the native host is unavailable; the port is discovered
 *  from storage (written by the desktop app) if present. */
async function sendViaLocalhostIpc(message) {
  const { ipcPort, ipcToken } = await chrome.storage.local.get(['ipcPort', 'ipcToken']);
  if (!ipcPort) return { ok: false, error: 'no localhost IPC port known' };
  try {
    const res = await fetch(`http://127.0.0.1:${ipcPort}/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'handshake', token: ipcToken || '', app: 'TServer Downloader', version: '1.0.0' })
    });
    if (!res.ok) return { ok: false, error: 'IPC handshake HTTP ' + res.status };
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

async function sendDownload(url, referrer) {
  const settings = await getSettings();
  if (!settings.interceptEnabled) return { ok: false, error: 'interception disabled' };
  if (isAllowlisted(url)) {
    // The blog is allowlisted for ads, but its own downloads still go
    // through TServer Downloader like any other site.
  }
  const message = {
    type: 'download',
    url,
    filename: filenameFromUrl(url),
    referrer: referrer || null,
    headers: {}
  };
  const viaNative = await sendToNativeHost(message);
  if (viaNative.ok) return viaNative;
  const viaIpc = await sendViaLocalhostIpc(message);
  if (viaIpc.ok) return viaIpc;
  return { ok: false, error: 'native host failed: ' + viaNative.error + '; IPC failed: ' + viaIpc.error };
}

async function incrementBlocked() {
  const { blockedCount = 0 } = await chrome.storage.local.get('blockedCount');
  await chrome.storage.local.set({ blockedCount: blockedCount + 1 });
}

/** Keep the declarativeNetRequest allowlist for the blog in sync with
 *  the adblock toggle. The static ruleset always allowlists the blog;
 *  this dynamic rule is the per-toggle layer. */
async function applyAdblockRules(settings) {
  if (!chrome.declarativeNetRequest || !chrome.declarativeNetRequest.updateDynamicRules) return;
  try {
    if (settings.adblockEnabled) {
      await chrome.declarativeNetRequest.updateDynamicRules({
        removeRuleIds: [1001],
        addRules: [{
          id: 1001, priority: 100,
          action: { type: 'allow' },
          condition: { urlFilter: '||' + ALLOWLISTED_HOST + '^', resourceTypes: ['main_frame', 'sub_frame', 'script', 'image', 'stylesheet', 'xmlhttprequest', 'font', 'media', 'other'] }
        }]
      });
    } else {
      await chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds: [1001], addRules: [] });
      // Disabling adblock also disables the static ruleset.
      await chrome.declarativeNetRequest.updateEnabledRulesets({ disableRulesetIds: ['ruleset_1'] });
      return;
    }
    await chrome.declarativeNetRequest.updateEnabledRulesets({ enableRulesetIds: ['ruleset_1'] });
  } catch (e) { /* DNR unavailable in this context; static rules still apply. */ }
}

async function getStatus() {
  const settings = await getSettings();
  const { blockedCount = 0 } = await chrome.storage.local.get('blockedCount');
  let nativeHostOk = false;
  let nativeHostError = null;
  try {
    const probe = await sendToNativeHost({ type: 'ping' });
    nativeHostOk = probe.ok;
    nativeHostError = probe.ok ? null : probe.error;
  } catch (e) { nativeHostError = String(e); }
  return {
    connected: nativeHostOk,
    nativeHostError,
    adblockEnabled: settings.adblockEnabled,
    interceptEnabled: settings.interceptEnabled,
    blockedCount,
    version: chrome.runtime.getManifest().version
  };
}

chrome.runtime.onInstalled.addListener(async (details) => {
  await chrome.storage.sync.set(DEFAULT_SETTINGS);
  chrome.contextMenus.create({
    id: 'tserver-download', title: 'Download with TServer Downloader',
    contexts: ['link', 'video', 'audio']
  });
  chrome.contextMenus.create({
    id: 'tserver-download-all', title: 'Download All Links',
    contexts: ['page']
  });
  const settings = await getSettings();
  await applyAdblockRules(settings);
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === 'tserver-download') {
    const url = info.linkUrl || info.srcUrl;
    if (url) await sendDownload(url, tab && tab.url);
  } else if (info.menuItemId === 'tserver-download-all') {
    if (tab && tab.id != null) {
      chrome.tabs.sendMessage(tab.id, { type: 'collect_links' });
    }
  }
});

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  // Intercept configured extensions and hand them to TServer Downloader.
  // The browser download is cancelled; the app takes over.
  if (!item || !item.url) return;
  getSettings().then((settings) => {
    if (!settings.interceptEnabled) return;
    if (!isDownloadableUrl(item.url)) return;
    if (item.byExtensionName === 'TServer Downloader') return;
    sendDownload(item.url, item.referrer || null).then((result) => {
      if (result.ok) chrome.downloads.cancel(item.id, () => {});
    });
  });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (!message || typeof message.type !== 'string') {
      sendResponse({ status: 'error', message: 'bad message' });
      return;
    }
    if (message.type === 'media_detected') {
      const { mediaCount = 0 } = await chrome.storage.local.get('mediaCount');
      await chrome.storage.local.set({ mediaCount: mediaCount + 1, lastMedia: message });
      sendResponse({ status: 'ok' });
    } else if (message.type === 'download') {
      sendResponse(await sendDownload(message.url, message.referrer));
    } else if (message.type === 'download_all') {
      const urls = Array.isArray(message.urls) ? message.urls : [];
      const results = [];
      for (const url of urls) results.push(await sendDownload(url, message.referrer));
      sendResponse({ status: 'ok', results });
    } else if (message.type === 'get_status') {
      sendResponse(await getStatus());
    } else if (message.type === 'set_settings') {
      const settings = await getSettings();
      const next = { ...settings, ...(message.settings || {}) };
      await chrome.storage.sync.set(next);
      await applyAdblockRules(next);
      sendResponse({ status: 'ok', settings: next });
    } else if (message.type === 'blocked') {
      await incrementBlocked();
      sendResponse({ status: 'ok' });
    } else {
      sendResponse({ status: 'error', message: 'unknown message type' });
    }
  })();
  return true; // async response
});

// Count declarativeNetRequest blocks via feedback (best-effort).
if (chrome.declarativeNetRequest && chrome.declarativeNetRequest.onRuleMatchedDebug) {
  chrome.declarativeNetRequest.onRuleMatchedDebug.addListener((info) => {
    if (info && info.rule && info.rule.ruleId >= 2) incrementBlocked();
  });
}
