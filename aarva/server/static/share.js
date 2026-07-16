// Share button (Section 5 — docs/session_plan_content_quality.md).
// Tries the Web Share API first (mobile system share sheet, all of
// the listener's messaging apps); falls back to copying the URL to
// the clipboard on browsers that don't support it (most desktops).
//
// Logs a 'share_clicked' analytics signal (fire-and-forget, best-
// effort — see aarva/services/share_analytics.py) only when the
// share/copy actually succeeds, not when the listener cancels the
// share sheet.
(function () {
  function logShare(contentType, contentId) {
    try {
      fetch('/api/v1/share-event', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content_type: contentType, content_id: contentId }),
        keepalive: true,
      }).catch(function () {});
    } catch (e) {}
  }

  function showCopiedToast(toast) {
    if (!toast) return;
    toast.classList.remove('hidden');
    setTimeout(function () { toast.classList.add('hidden'); }, 2000);
  }

  function wireShareButtons() {
    document.querySelectorAll('[data-share]').forEach(function (block) {
      var btn = block.querySelector('[data-share-button]');
      if (!btn || btn.dataset.shareWired) return;
      btn.dataset.shareWired = '1';

      var toast = block.querySelector('[data-share-toast]');
      var url = block.dataset.shareUrl;
      var title = block.dataset.shareTitle || '';
      var text = block.dataset.shareText || '';
      var contentType = block.dataset.shareContentType;
      var contentId = block.dataset.shareContentId;

      btn.addEventListener('click', function () {
        if (navigator.share) {
          navigator.share({ title: title, text: text, url: url })
            .then(function () { logShare(contentType, contentId); })
            .catch(function () { /* listener cancelled — don't log, don't fall back */ });
          return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(url)
            .then(function () {
              logShare(contentType, contentId);
              showCopiedToast(toast);
            })
            .catch(function () {});
        }
      });
    });
  }

  wireShareButtons();
  // htmx partial-navigation (base.html's hx-boost) swaps #main-content
  // without a full page reload, so freshly-navigated-to share buttons
  // need re-wiring — same pattern as the persistent player above.
  document.body.addEventListener('htmx:afterSwap', wireShareButtons);
})();
