/* TServer Downloader - Firefox (MV2) background script.
 * Uses the `browser.*` namespace with a `chrome.*` fallback so the same
 * logic works in both namespaces. Firefox has no declarativeNetRequest
 * service worker, so ad-blocking here uses webRequest blocking. */
'use strict';

const api = (typeof browser !== 'undefined' && browser.runtime) ? browser : chrome;
const NATIVE_HOST = 'com.tserver.downloader';
const ALLOWLISTED_HOST = 'thetechnicalserver.blogspot.com';
const AD_HOSTS = [
  'doubleclick.net', 'googlesyndication.com', 'googleadservices.com',
  'google-analytics.com', 'googletagmanager.com', 'ads.yahoo.com',
  'adnxs.com', 'amazon-adsystem.com', 'criteo.com', 'taboola.com',
  'outbrain.com', 'scorecardresearch.com', 'moatads.com'
];
const INTERCEPT_EXTENSIONS = new Set([
  'mp4', 'mkv', 'webm', 'mov', 'avi', 'zip', 'rar', '7z', 'tar', 'gz',
  'pdf', 'exe', 'msi', 'iso', 'img', 'dmg', 'mp3', 'flac', 'wav', 'ogg',
  'epub', 'apk', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'torrent'
]);
const DEFAULT_SETTINGS = { adblockEnabled: true, interceptEnabled: true, disabledSites: [] };

async function getSettings() {
  const stored = await api.storage.sync.get(DEFAULT_SETTINGS);
  return Object.assign({}, DEFAULT_SETTINGS, stored);
}

function isAllowlisted(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host === ALLOWLISTED_HOST || host.endsWith('.' + ALLOWLISTED_HOST);
  } catch (e) { return false; }
}

function filenameFromUrl(url) {
  try {
    const name = decodeURIComponent(new URL(url).pathname.split('/').pop() || '');
    return name || null;
  } catch (e) { return null; }
}

function isDownloadableUrl(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
    return INTERCEPT_EXTENSIONS.has((parsed.pathname.split('.').pop() || '').toLowerCase());
  } catch (e) { return false; }
}

function sendToNativeHost(message) {
  return new Promise((resolve) => {
    try {
      api.runtime.sendNativeMessage(NATIVE_HOST, message, (response) => {
        if (api.runtime.lastError) {
          resolve({ ok: false, error: api.runtime.lastError.message });
        } else if (response && response.status === 'ok') {
          resolve({ ok: true, response });
        } else {
          resolve({ ok: false, error: (response && response.message) || 'native host error' });
        }
      });
    } catch (e) { resolve({ ok: false, error: String(e) }); }
  });
}

async function sendDownload(url, referrer) {
  const settings = await getSettings();
  if (!settings.interceptEnabled) return { ok: false, error: 'interception disabled' };
  return sendToNativeHost({
    type: 'download', url, filename: filenameFromUrl(url),
    referrer: referrer || null, headers: {}
  });
}

async function getStatus() {
  const settings = await getSettings();
  const { blockedCount = 0 } = await api.storage.local.get('blockedCount');
  const probe = await sendToNativeHost({ type: 'ping' });
  return {
    connected: probe.ok, nativeHostError: probe.ok ? null : probe.error,
    adblockEnabled: settings.adblockEnabled, interceptEnabled: settings.interceptEnabled,
    blockedCount, version: api.runtime.getManifest().version
  };
}

function isAdUrl(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    if (isAllowlisted(url)) return false;
    return AD_HOSTS.some((h) => host === h || host.endsWith('.' + h));
  } catch (e) { return false; }
}

async function incrementBlocked() {
  const { blockedCount = 0 } = await api.storage.local.get('blockedCount');
  await api.storage.local.set({ blockedCount: blockedCount + 1 });
}

// webRequest-based ad blocking (Firefox MV2). Allowlisted blog is never blocked.
// Blocking must be synchronous, so the toggle is kept in a cached value
// refreshed from storage (and on storage changes).
if (api.webRequest && api.webRequest.onBeforeRequest) {
  let adblockOn = true;
  api.storage.onChanged.addListener((changes) => {
    if (changes.adblockEnabled) adblockOn = changes.adblockEnabled.newValue;
  });
  getSettings().then((s) => { adblockOn = s.adblockEnabled; });
  api.webRequest.onBeforeRequest.addListener(
    (details) => {
      const block = adblockOn && isAdUrl(details.url);
      if (block) incrementBlocked();
      return { cancel: block };
    },
    { urls: ['<all_urls>'] },
    ['blocking']
  );
}

api.runtime.onInstalled.addListener(async () => {
  await api.storage.sync.set(DEFAULT_SETTINGS);
  api.contextMenus.create({ id: 'tserver-download', title: 'Download with TServer Downloader', contexts: ['link', 'video', 'audio'] });
  api.contextMenus.create({ id: 'tserver-download-all', title: 'Download All Links', contexts: ['page'] });
});

api.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === 'tserver-download') {
    const url = info.linkUrl || info.srcUrl;
    if (url) await sendDownload(url, tab && tab.url);
  } else if (info.menuItemId === 'tserver-download-all' && tab && tab.id != null) {
    api.tabs.sendMessage(tab.id, { type: 'collect_links' });
  }
});

api.downloads.onCreated.addListener((item) => {
  if (!item || !item.url) return;
  getSettings().then((settings) => {
    if (!settings.interceptEnabled || !isDownloadableUrl(item.url)) return;
    sendDownload(item.url, item.referrer || null).then((r) => {
      if (r.ok) api.downloads.cancel(item.id, () => {});
    });
  });
});

api.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (!message || typeof message.type !== 'string') { sendResponse({ status: 'error', message: 'bad message' }); return; }
    if (message.type === 'media_detected') {
      const { mediaCount = 0 } = await api.storage.local.get('mediaCount');
      await api.storage.local.set({ mediaCount: mediaCount + 1, lastMedia: message });
      sendResponse({ status: 'ok' });
    } else if (message.type === 'download') {
      sendResponse(await sendDownload(message.url, message.referrer));
    } else if (message.type === 'download_all') {
      const results = [];
      for (const url of (message.urls || [])) results.push(await sendDownload(url, message.referrer));
      sendResponse({ status: 'ok', results });
    } else if (message.type === 'get_status') {
      sendResponse(await getStatus());
    } else if (message.type === 'set_settings') {
      const next = Object.assign({}, await getSettings(), message.settings || {});
      await api.storage.sync.set(next);
      sendResponse({ status: 'ok', settings: next });
    } else {
      sendResponse({ status: 'error', message: 'unknown message type' });
    }
  })();
  return true;
});
