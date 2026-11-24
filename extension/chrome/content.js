/* TServer Downloader - content script: media/link detection. */
(function () {
  'use strict';

  var DOWNLOADABLE = /\.(mp4|mkv|webm|mov|avi|zip|rar|7z|tar|gz|pdf|exe|msi|iso|img|dmg|mp3|flac|wav|ogg|epub|apk|docx?|xlsx?|pptx?)(\?|#|$)/i;
  var BUTTON_ID = 'tserver-download-button';
  var notified = new Set();

  function isDownloadable(url) {
    return typeof url === 'string' && DOWNLOADABLE.test(url);
  }

  function collectMedia() {
    var found = [];
    document.querySelectorAll('video, audio').forEach(function (el) {
      var src = el.currentSrc || el.src || '';
      var source = el.querySelector('source');
      if (!src && source) src = source.src || '';
      if (src && src.indexOf('blob:') !== 0) {
        found.push({ url: src, title: document.title, kind: el.tagName.toLowerCase() });
      }
    });
    return found;
  }

  function collectLinks() {
    var urls = [];
    document.querySelectorAll('a[href]').forEach(function (a) {
      if (isDownloadable(a.href)) urls.push(a.href);
    });
    return Array.from(new Set(urls));
  }

  function notifyMedia(item) {
    if (notified.has(item.url)) return;
    notified.add(item.url);
    try {
      chrome.runtime.sendMessage({ type: 'media_detected', url: item.url, title: item.title || document.title, kind: item.kind });
    } catch (e) { /* extension context invalidated */ }
  }

  function showFloatingButton(item) {
    if (document.getElementById(BUTTON_ID)) return;
    var btn = document.createElement('button');
    btn.id = BUTTON_ID;
    btn.textContent = '⬇ Download with TServer';
    btn.title = item.url;
    btn.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;' +
      'background:#1565c0;color:#fff;border:none;border-radius:24px;padding:10px 16px;' +
      'font:600 13px system-ui,sans-serif;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.35);';
    btn.addEventListener('click', function () {
      try {
        chrome.runtime.sendMessage({ type: 'download', url: item.url, referrer: location.href });
      } catch (e) { /* ignore */ }
      btn.textContent = 'Sent to TServer Downloader ✓';
      setTimeout(function () { btn.remove(); }, 2500);
    });
    document.body.appendChild(btn);
  }

  function scan() {
    var media = collectMedia();
    media.forEach(function (item) {
      notifyMedia(item);
      showFloatingButton(item);
    });
  }

  chrome.runtime.onMessage.addListener(function (message, sender, sendResponse) {
    if (message && message.type === 'collect_links') {
      var urls = collectLinks();
      try {
        chrome.runtime.sendMessage({ type: 'download_all', urls: urls, referrer: location.href });
      } catch (e) { /* ignore */ }
      sendResponse({ status: 'ok', count: urls.length });
    }
    return true;
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scan);
  } else {
    scan();
  }
  var observer = new MutationObserver(function () { scan(); });
  observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['src'] });
  window.addEventListener('beforeunload', function () { observer.disconnect(); });
})();
