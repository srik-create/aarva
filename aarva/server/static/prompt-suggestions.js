// Header prompt-input suggestions dropdown — Feature A of
// docs/session_plan_search_suggestions.md. Shows a static list of
// example prompts when the listener focuses an EMPTY input; clicking
// one pre-fills (does NOT submit) so they can still edit before
// hitting Enter/Create.
(function () {
  function wirePromptSuggestions() {
    document.querySelectorAll('[data-prompt-suggestions-wrapper]').forEach(function (wrapper) {
      if (wrapper.dataset.suggestionsWired) return;
      wrapper.dataset.suggestionsWired = '1';

      var input = wrapper.querySelector('[data-prompt-input]');
      var dropdown = wrapper.querySelector('[data-prompt-suggestions]');
      if (!input || !dropdown) return;

      function show() {
        if (input.value.trim() === '') dropdown.hidden = false;
      }
      function hide() {
        dropdown.hidden = true;
      }

      input.addEventListener('focus', show);
      // Any typing means the listener is on their own path — dismiss
      // immediately rather than waiting for blur.
      input.addEventListener('input', hide);

      dropdown.querySelectorAll('[data-prompt-suggestion]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          input.value = btn.textContent.trim();
          hide();
          input.focus();
          // Explicit cursor-to-end after focus — some browsers reset
          // selection on programmatic focus() otherwise.
          var len = input.value.length;
          input.setSelectionRange(len, len);
        });
      });

      document.addEventListener('click', function (e) {
        if (!dropdown.hidden && !wrapper.contains(e.target)) hide();
      });

      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !dropdown.hidden) hide();
      });
    });
  }

  wirePromptSuggestions();
  // The header (and this wrapper) lives outside #main-content, so
  // htmx's partial-navigation swap never touches it in practice —
  // this only ever runs once. Listening anyway costs nothing and
  // matches the re-wiring pattern used elsewhere in base.html, in
  // case that assumption ever changes.
  document.body.addEventListener('htmx:afterSwap', wirePromptSuggestions);
})();
