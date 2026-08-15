(function () {
  'use strict';

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) {
    console.error('[summarization-calendar] Plugin SDK not available — cannot register Calendar plugin.');
    return;
  }

  var React = SDK.React;
  var hooks = SDK.hooks;
  var useState = hooks.useState;
  var useEffect = hooks.useEffect;
  var useCallback = hooks.useCallback;
  var useMemo = hooks.useMemo;
  var useRef = hooks.useRef;
  var C = SDK.components;
  var cn = SDK.utils.cn;
  var fetchJSON = SDK.fetchJSON;
  var isoTimeAgo = SDK.utils.isoTimeAgo;

  // -----------------------------------------------------------------------
  // Utility helpers
  // -----------------------------------------------------------------------

  var CHICAGO_TZ = 'America/Chicago';
  // Local multi-chunk inference can legitimately take tens of minutes.
  // Recursive polling is still bounded and keeps at most one request in flight.
  var JOB_POLL_MAX_ATTEMPTS = 180;

  function getChicagoNow() {
    var formatter = new Intl.DateTimeFormat('en-US', {
      timeZone: CHICAGO_TZ,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit'
    });
    var parts = formatter.formatToParts(new Date());
    var vals = {};
    for (var i = 0; i < parts.length; i++) {
      vals[parts[i].type] = parseInt(parts[i].value, 10);
    }
    return { year: vals.year, month: vals.month - 1, day: vals.day };
  }

  function formatDate(year, month, day) {
    var m = String(month + 1).padStart(2, '0');
    var d = String(day).padStart(2, '0');
    return year + '-' + m + '-' + d;
  }

  function parseDate(str) {
    var p = str.split('-');
    return { year: parseInt(p[0], 10), month: parseInt(p[1], 10) - 1, day: parseInt(p[2], 10) };
  }

  function getDaysInMonth(year, month) {
    return new Date(year, month + 1, 0).getDate();
  }

  function getFirstDayOfWeek(year, month) {
    var dow = new Date(year, month, 1).getDay(); // 0=Sun
    return (dow + 6) % 7; // Monday-first: Mon=0 ... Sun=6
  }

  function buildCalendarGrid(year, month) {
    var daysInMonth = getDaysInMonth(year, month);
    var firstDow = getFirstDayOfWeek(year, month);
    var prevMonth = month === 0 ? 11 : month - 1;
    var prevYear = month === 0 ? year - 1 : year;
    var nextMonth = month === 11 ? 0 : month + 1;
    var nextYear = month === 11 ? year + 1 : year;
    var daysInPrev = getDaysInMonth(prevYear, prevMonth);

    var cells = [];
    for (var i = 0; i < firstDow; i++) {
      cells.push({ day: daysInPrev - firstDow + 1 + i, month: prevMonth, year: prevYear, current: false });
    }
    for (var d = 1; d <= daysInMonth; d++) {
      cells.push({ day: d, month: month, year: year, current: true });
    }
    var remaining = Math.ceil(cells.length / 7) * 7 - cells.length;
    for (var next = 0; next < remaining; next++) {
      cells.push({ day: 1 + next, month: nextMonth, year: nextYear, current: false });
    }
    return cells;
  }

  var MONTH_NAMES = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'
  ];

  var WEEKDAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  function monthTitle(year, month) {
    return MONTH_NAMES[month] + ' ' + year;
  }

  // Sanitize text: convert to plain string, strip control chars (not just XSS — all non-printable)
  function sanitize(str) {
    if (typeof str !== 'string') return '';
    return str.replace(/[\x00-\x1f\x7f-\x9f]/g, '');
  }

  // Truncate string to max length with ellipsis
  function truncate(str, maxLen) {
    if (!str || str.length <= maxLen) return str;
    return str.substring(0, maxLen) + '\u2026';
  }

  var SHOW_AUTO_TITLED_STORAGE_KEY = 'hermes.summarization-calendar.showAutoTitled';
  // Legacy key from the v1.1.0-and-earlier "daily-ledger" naming. Read-only
  // fallback so a preference set before the rename survives it; saves always
  // write the new key, which shadows the legacy one.
  var LEGACY_SHOW_AUTO_TITLED_STORAGE_KEY = 'hermes.daily-ledger.showAutoTitled';
  var AUTO_TITLED_SESSION_RE = /^Session \d{8}_\d{6}_[A-Za-z0-9_-]+$/;

  function isAutoTitledSession(title) {
    return typeof title === 'string' && AUTO_TITLED_SESSION_RE.test(title);
  }

  function getVisibleSessions(sessions, showAutoTitled) {
    if (!Array.isArray(sessions)) return [];
    if (showAutoTitled !== false) return sessions.slice();
    return sessions.filter(function (session) {
      return !isAutoTitledSession(session && session.title);
    });
  }

  function filterSelectedSessionKeys(dateStr, sessions, selectedKeys, showAutoTitled) {
    var visible = getVisibleSessions(sessions, showAutoTitled);
    var allowed = {};
    for (var i = 0; i < visible.length; i++) {
      allowed[batchSelectionKey(dateStr, visible[i])] = true;
    }
    var retained = {};
    Object.keys(selectedKeys || {}).forEach(function (key) {
      if (allowed[key]) retained[key] = true;
    });
    return retained;
  }

  function loadShowAutoTitled(storage) {
    try {
      if (!storage || typeof storage.getItem !== 'function') return true;
      var value = storage.getItem(SHOW_AUTO_TITLED_STORAGE_KEY);
      if (value === null || value === undefined) {
        // Fall back to the pre-rename key so an existing preference
        // (e.g. "hide auto-titled rows") survives the plugin rename.
        value = storage.getItem(LEGACY_SHOW_AUTO_TITLED_STORAGE_KEY);
      }
      if (value === 'false') return false;
      if (value === 'true') return true;
    } catch (_err) { /* browser storage may be unavailable */ }
    return true;
  }

  function saveShowAutoTitled(value, storage) {
    try {
      if (storage && typeof storage.setItem === 'function') {
        storage.setItem(SHOW_AUTO_TITLED_STORAGE_KEY, value ? 'true' : 'false');
      }
    } catch (_err) { /* preference persistence must never break the page */ }
  }

  function loadBrowserShowAutoTitled() {
    try { return loadShowAutoTitled(window.localStorage); }
    catch (_err) { return true; }
  }

  function saveBrowserShowAutoTitled(value) {
    try { saveShowAutoTitled(value, window.localStorage); }
    catch (_err) { /* fail open when localStorage property access is blocked */ }
  }

  // -----------------------------------------------------------------------
  // API helpers
  // -----------------------------------------------------------------------

  var API_BASE = '/api/plugins/summarization-calendar';

  function apiGet(path) {
    return fetchJSON(API_BASE + path);
  }

  function apiPost(path, body) {
    return fetchJSON(API_BASE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: typeof body === 'string' ? body : JSON.stringify(body)
    });
  }

  function encodeUrlParams(params) {
    var parts = [];
    for (var k in params) {
      if (params.hasOwnProperty(k)) {
        parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(String(params[k])));
      }
    }
    return '?' + parts.join('&');
  }

  function sessionKey(session, dateStr) {
    return encodeURIComponent(String(dateStr || '')) + '/' +
      encodeURIComponent(String(session.profile || '')) + '/' +
      encodeURIComponent(String(session.session_id || ''));
  }

  function sessionChatHref(session) {
    return '/chat?resume=' + encodeURIComponent(String(session.session_id || '')) +
      '&profile=' + encodeURIComponent(String(session.profile || ''));
  }

  // Poll any immutable summary artifact with bounded exponential backoff.
  // During regeneration the previous artifact remains visible, so success
  // requires a version different from previousVersion.
  function pollArtifact(path, previousVersion, onResult, onError, maxAttempts, intervalMs) {
    var attempt = 0;
    var currentDelay = intervalMs || 2000;
    var max = maxAttempts || JOB_POLL_MAX_ATTEMPTS;
    var cancelled = false;
    var timerId = null;

    function scheduleTick() {
      if (cancelled) return;
      // Double delay, capped at 15s
      currentDelay = Math.min(currentDelay * 2, 15000);
      timerId = setTimeout(doTick, currentDelay);
    }

    function doTick() {
      timerId = null;
      if (cancelled) return;
      attempt++;

      apiGet(path)
        .then(function (data) {
          if (cancelled) return;
          var job = data.job_status || null;
          if (job && job.status === 'failed') {
            onError(sanitize(job.error || 'Generation failed'));
            return;
          }
          var currentVersion = data.meta && data.meta.version_id;
          var newVersion = !previousVersion || currentVersion !== previousVersion;
          var jobComplete = !job || job.status === 'completed';
          if (data.exists && newVersion && jobComplete) {
            onResult(data);
          } else if (attempt >= max) {
            onError('Generation timed out after ' + max + ' attempts');
          } else {
            scheduleTick();
          }
        })
        .catch(function (err) {
          if (cancelled) return;
          if (attempt >= max) {
            onError('Polling failed: ' + sanitize(String(err.message || err)));
          } else {
            scheduleTick();
          }
        });
    }

    // First tick starts after the initial delay
    timerId = setTimeout(doTick, currentDelay);

    return function cancel() {
      cancelled = true;
      if (timerId !== null) {
        clearTimeout(timerId);
        timerId = null;
      }
    };
  }

  // -----------------------------------------------------------------------
  // Batch helpers (pure, testable)
  // -----------------------------------------------------------------------

  function batchSelectionKey(dateStr, session) {
    return encodeURIComponent(String(dateStr || '')) + '/' +
      encodeURIComponent(String(session.profile || '')) + '/' +
      encodeURIComponent(String(session.session_id || ''));
  }

  function buildBatchRequest(dateStr, sessions, selectedKeys, regenerateCurrent) {
    var resultSessions = [];
    for (var i = 0; i < sessions.length; i++) {
      var s = sessions[i];
      var key = batchSelectionKey(dateStr, s);
      if (selectedKeys.hasOwnProperty(key)) {
        resultSessions.push({
          profile: s.profile,
          session_id: s.session_id
        });
      }
    }
    return {
      sessions: resultSessions,
      regenerate_current: !!regenerateCurrent
    };
  }

  function summarizeBatchProgress(batch) {
    var total = 0, finished = 0, completed = 0, failed = 0, skipped = 0, active = 0;
    if (!batch || !Array.isArray(batch.members)) {
      return { total: 0, finished: 0, completed: 0, failed: 0, skipped: 0, active: 0 };
    }
    for (var i = 0; i < batch.members.length; i++) {
      var m = batch.members[i];
      if (!m || !m.session_id || !m.profile) {
        continue; // skip malformed
      }
      total++;
      var status = (m.status || '').toLowerCase();
      if (status === 'completed' || status === 'partial' || status === 'failed') {
        finished++;
        if (status === 'completed') completed++;
        else if (status === 'failed') failed++;
        else if (status === 'partial') completed++; // partial counts as finished with success
      } else if (status === 'skipped_current' || status === 'skipped_running') {
        finished++;
        skipped++;
      } else {
        active++;
      }
    }
    return { total: total, finished: finished, completed: completed, failed: failed, skipped: skipped, active: active };
  }

  function batchMemberStatusMap(dateStr, batch) {
    var map = {};
    if (!batch || !Array.isArray(batch.members)) {
      return map;
    }
    for (var i = 0; i < batch.members.length; i++) {
      var m = batch.members[i];
      if (!m || !m.session_id || !m.profile) {
        continue; // skip malformed
      }
      var key = batchSelectionKey(dateStr, m);
      map[key] = m.status || 'unknown';
    }
    return map;
  }

  function newestBatchForDate(response, dateStr) {
    if (!response || !Array.isArray(response.batches)) {
      return null;
    }
    for (var i = 0; i < response.batches.length; i++) {
      var b = response.batches[i];
      if (!b || !b.date) {
        continue;
      }
      var batchDate = String(b.date).split('T')[0]; // strip time portion if present
      if (batchDate === dateStr) {
        return b;
      }
    }
    return null;
  }

  function pollBatchStatus(path, onUpdate, onTerminal, onError, maxAttempts, intervalMs) {
    var attempt = 0;
    var currentDelay = intervalMs || 2000;
    var max = maxAttempts || JOB_POLL_MAX_ATTEMPTS;
    var cancelled = false;
    var timerId = null;

    function scheduleTick() {
      if (cancelled) return;
      currentDelay = Math.min(currentDelay * 2, 15000);
      timerId = setTimeout(doTick, currentDelay);
    }

    function doTick() {
      timerId = null;
      if (cancelled) return;
      attempt++;

      apiGet(path)
        .then(function (data) {
          if (cancelled) return;
          if (data && onUpdate) onUpdate(data);
          var status = data ? String(data.status || '').toLowerCase() : '';
          if (status === 'completed' || status === 'partial' || status === 'failed') {
            if (onTerminal) onTerminal(data);
            return;
          }
          if (attempt >= max) {
            if (onError) onError('Batch polling timed out after ' + max + ' attempts');
          } else {
            scheduleTick();
          }
        })
        .catch(function (err) {
          if (cancelled) return;
          if (attempt >= max) {
            if (onError) onError('Batch polling failed: ' + sanitize(String(err.message || err)));
          } else {
            scheduleTick();
          }
        });
    }

    timerId = setTimeout(doTick, currentDelay);

    return function cancel() {
      cancelled = true;
      if (timerId !== null) {
        clearTimeout(timerId);
        timerId = null;
      }
    };
  }

  // -----------------------------------------------------------------------
  // Calendar batch selection helpers (pure, testable)
  // -----------------------------------------------------------------------

  // Returns true if batch actions should be disabled (queued or running)
  function isBatchLocked(batchStatus) {
    if (!batchStatus || !batchStatus.status) return false;
    var status = String(batchStatus.status).toLowerCase();
    return status === 'queued' || status === 'running';
  }

  // -----------------------------------------------------------------------
  // Components
  // -----------------------------------------------------------------------

  function LoadingBar() {
    return React.createElement('div', { className: 'dl-loading-bar', role: 'status', 'aria-label': 'Loading' });
  }

  function ErrorMessage({ message }) {
    return React.createElement('div', { className: 'dl-error-msg', role: 'alert' }, sanitize(message));
  }

  function Placeholder({ text }) {
    return React.createElement('div', { className: 'dl-placeholder' }, sanitize(text || 'No data available'));
  }

  // Controlled batch selection toolbar
  function BatchToolbar({ dateStr, sessions, selectedKeys, regenerateCurrent, batchStatus, batchError, onSelectAll, onClear, onToggleRegenerate, onSubmit }) {
    // Count selected visible sessions only (non-cron sessions have session_id)
    var visibleSessions = sessions || [];
    var selectedCount = 0;
    for (var i = 0; i < visibleSessions.length; i++) {
      var s = visibleSessions[i];
      if (s && (s.session_id || s.profile)) {
        var key = batchSelectionKey(dateStr, s);
        if (selectedKeys.hasOwnProperty(key)) selectedCount++;
      }
    }

    var totalVisible = visibleSessions.length;
    var batchLocked = isBatchLocked(batchStatus);
    var batchDisabled = batchLocked;
    var isBatchRunning = (batchStatus && String(batchStatus.status || '').toLowerCase() === 'running');
    var isBatchQueued = (batchStatus && String(batchStatus.status || '').toLowerCase() === 'queued');

    var progressText = 'Idle';
    if (batchStatus) {
      var p = summarizeBatchProgress(batchStatus);
      var totalP = Number(batchStatus.total || p.total || 0);
      var finishedP = p.finished || 0;
      var completedP = p.completed || 0;
      var failedP = p.failed || 0;
      var skippedP = p.skipped || 0;
      if (totalP > 0) {
        progressText = finishedP + '/' + totalP + ' finished' +
          ' — ' + completedP + ' completed, ' + failedP + ' failed, ' + skippedP + ' skipped';
      } else if (isBatchQueued) {
        progressText = 'Queued';
      } else if (isBatchRunning) {
        progressText = 'Running';
      }
    }

    return React.createElement('div', { className: 'dl-batch-toolbar' },
      React.createElement('span', { className: 'dl-batch-count', role: 'status' }, selectedCount + ' selected'),
      React.createElement('button', {
        type: 'button',
        className: 'dl-batch-btn',
        onClick: function () { onSelectAll && onSelectAll(); },
        disabled: batchDisabled,
        'aria-label': 'Select all visible sessions',
      }, 'Select all'),
      React.createElement('button', {
        type: 'button',
        className: 'dl-batch-btn',
        onClick: function () { onClear && onClear(); },
        disabled: batchDisabled,
        'aria-label': 'Clear selection',
      }, 'Clear selection'),
      React.createElement('label', { className: 'dl-batch-option' },
        React.createElement('input', {
          type: 'checkbox',
          className: 'dl-batch-checkbox',
          checked: !!regenerateCurrent,
          onChange: function (e) { onToggleRegenerate && onToggleRegenerate(e.target.checked); },
          disabled: batchDisabled,
        }),
        'Regenerate already current'
      ),
      React.createElement('button', {
        type: 'button',
        className: 'dl-batch-btn dl-batch-submit',
        onClick: function () { onSubmit && onSubmit(); },
        disabled: selectedCount === 0 || batchDisabled,
        'aria-label': 'Summarize selected sessions',
      }, 'Summarize selected (' + selectedCount + ')'),
      React.createElement('span', { className: 'dl-batch-progress', role: 'status' }, progressText),
      batchError && React.createElement('span', { className: 'dl-batch-error', role: 'alert' }, sanitize(batchError))
    );
  }

  // Session detail card with read-only summary rendering and controlled selection.
  function SessionCard({ dateStr, session, summaryData, selected, selectionDisabled, batchMemberStatus, onToggleSelect, onRollback, error }) {
    var title = sanitize(session.title || 'Untitled session');
    var profile = sanitize(session.profile || '-');
    var source = sanitize(session.source || '-');
    var model = session.model ? sanitize(session.model) : '';
    var msgs = session.message_count != null ? session.message_count + ' messages' : '';
    var tools = session.tool_call_count != null ? session.tool_call_count + ' tool calls' : '';

    var timeRange = '';
    if (session.first_active_utc && session.last_active_utc) {
      var firstTime = new Date(session.first_active_utc).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', timeZone: CHICAGO_TZ });
      var lastTime = new Date(session.last_active_utc).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', timeZone: CHICAGO_TZ });
      timeRange = firstTime + ' - ' + lastTime;
    }

    var metaParts = [];
    if (timeRange) metaParts.push(timeRange);
    if (msgs) metaParts.push(msgs);
    if (tools) metaParts.push(tools);
    if (model) metaParts.push('Model: ' + model);

    var status = summaryData || session.summary_status || {};
    var exists = !!status.exists;
    var stale = !!status.stale;
    var summaryText = summaryData && summaryData.data ? sanitize(summaryData.data.summary || '') : '';
    var versions = summaryData && summaryData.versions ? summaryData.versions : [];
    var job = status.job_status || null;
    var jobError = job && job.status === 'failed' ? sanitize(job.error || 'Summary generation failed') : '';

    // Sanitize batch member status text (queued/running/completed/failed/skipped_current/skipped_running)
    var memberStatusText = '';
    if (batchMemberStatus) {
      var mStatus = String(batchMemberStatus).toLowerCase();
      if (mStatus === 'queued') memberStatusText = 'Queued';
      else if (mStatus === 'running') memberStatusText = 'Running';
      else if (mStatus === 'completed') memberStatusText = 'Completed';
      else if (mStatus === 'partial') memberStatusText = 'Completed (partial)';
      else if (mStatus === 'failed') memberStatusText = 'Failed';
      else if (mStatus === 'skipped_current') memberStatusText = 'Skipped';
      else if (mStatus === 'skipped_running') memberStatusText = 'Skipped';
      else memberStatusText = sanitize(batchMemberStatus || 'Unknown');
    }

    var label = 'Select ' + title + ' for batch summary';
    var statusLabel = memberStatusText ? ' ' + memberStatusText : '';

    return React.createElement('div', { className: 'dl-session-card' },
      React.createElement('div', { className: 'dl-session-heading' },
        React.createElement('input', {
          type: 'checkbox',
          className: 'dl-session-select',
          checked: !!selected,
          disabled: !!selectionDisabled,
          onChange: function (e) { if (typeof onToggleSelect === 'function') { onToggleSelect(session); } },
          'aria-label': label + statusLabel,
        }),
        React.createElement('a', {
          className: 'dl-session-title',
          href: sessionChatHref(session),
          title: label,
        }, title),
        memberStatusText && React.createElement('span', { className: 'dl-session-member-status', role: 'status' }, ' (' + memberStatusText + ')')
      ),
      React.createElement('div', { className: 'dl-meta' },
        React.createElement('span', { className: 'dl-badge dl-badge-profile' }, profile),
        React.createElement('span', { className: 'dl-badge dl-badge-source' }, source),
        exists && React.createElement('span', { className: 'dl-badge dl-badge-completed' }, stale ? 'Stale' : 'Summarized')
      ),
      metaParts.length > 0 && React.createElement('div', { className: 'dl-meta', style: { marginTop: '0.2rem' } }, metaParts.map(function (m) {
        return React.createElement('span', { key: m }, m);
      })),
      stale && React.createElement('div', { className: 'dl-stale-banner' }, 'Stale: source activity changed after this summary was generated.'),
      summaryText && React.createElement('div', { className: 'dl-recap-content dl-session-summary' }, summaryText),
      (error || jobError) && ErrorMessage({ message: error || jobError }),
      versions.length > 0 && React.createElement(VersionButtons, {
        versions: versions,
        onRestore: function (versionId) {
          if (typeof onRollback === 'function') onRollback(dateStr, session, versionId);
        },
      })
    );
  }

  function VersionButtons({ versions, onRestore }) {
    if (!versions || versions.length === 0) return null;
    return React.createElement('div', { className: 'dl-version-history' },
      React.createElement('div', { className: 'dl-section-title' }, 'Version History'),
      React.createElement('div', { className: 'dl-rollback-list' },
        versions.map(function (version) {
          return React.createElement(C.Button || 'button', {
            key: version.version_id,
            className: 'dl-rollback-btn',
            onClick: function () { onRestore(version.version_id); },
          }, 'Restore: ' + sanitize(version.version_id) + (version.generated_at ? ' (' + isoTimeAgo(version.generated_at) + ')' : ''));
        })
      )
    );
  }

  // Cron run card
  function CronCard({ cron }) {
    var name = sanitize(cron.job_name || 'Job ' + sanitize(cron.job_id));
    var status = cron.status || 'unknown';
    var profile = cron.profile ? sanitize(cron.profile) : '';
    var source = cron.source ? sanitize(cron.source) : '';

    // Time info
    var timeInfo = [];
    if (cron.started_at) {
      var started = new Date(cron.started_at).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: CHICAGO_TZ });
      timeInfo.push('Started: ' + started);
    } else if (cron.claimed_at) {
      var claimed = new Date(cron.claimed_at).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', timeZone: CHICAGO_TZ });
      timeInfo.push('Claimed: ' + claimed);
    }
    if (cron.finished_at) {
      var finished = new Date(cron.finished_at).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: CHICAGO_TZ });
      timeInfo.push('Finished: ' + finished);
    }

    // Status badge
    var badgeClass = status === 'completed' ? 'dl-badge-completed' :
                     status === 'failed' ? 'dl-badge-failed' :
                     'dl-badge-unknown';

    // Error summary (sanitized)
    var errorText = cron.error_summary ? truncate(sanitize(cron.error_summary), 200) : '';

    return React.createElement('div', { className: 'dl-cron-card' },
      React.createElement('div', { style: { display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' } },
        React.createElement('span', { style: { fontWeight: 600, color: 'var(--dl-fg)' } }, name),
        React.createElement('span', { className: 'dl-badge ' + badgeClass }, status)
      ),
      (profile || source) && React.createElement('div', { className: 'dl-meta', style: { marginTop: '0.15rem' } },
        profile && React.createElement('span', { className: 'dl-badge dl-badge-profile' }, profile),
        source && React.createElement('span', { className: 'dl-badge dl-badge-source' }, source)
      ),
      timeInfo.length > 0 && React.createElement('div', { className: 'dl-meta', style: { marginTop: '0.15rem' } },
        timeInfo.map(function (t) { return React.createElement('span', { key: t }, t); })
      ),
      errorText && React.createElement('div', { className: 'dl-meta', style: { marginTop: '0.15rem', color: 'var(--dl-destructive)' } }, '[error] ' + errorText)
    );
  }

  // Activity dots for a day cell
  function ActivityDots({ sessionCount, cronCount, hasRecap, recapStale }) {
    var dots = [];
    if (sessionCount > 0) {
      dots.push(React.createElement('span', { key: 'sess-dot', className: 'dl-dot dl-dot-sessions', title: sessionCount + ' sessions' }));
    }
    if (cronCount > 0) {
      dots.push(React.createElement('span', { key: 'cron-dot', className: 'dl-dot dl-dot-cron', title: cronCount + ' cron runs' }));
    }
    if (hasRecap) {
      var recapClass = recapStale ? 'dl-dot-stale' : 'dl-dot-recap';
      dots.push(React.createElement('span', { key: 'recap-dot', className: 'dl-dot ' + recapClass, title: recapStale ? 'Stale recap' : 'Has recap' }));
    }
    if (dots.length === 0) return null;

    var countText = [];
    if (sessionCount > 0) countText.push(sessionCount + 's');
    if (cronCount > 0) countText.push(cronCount + 'c');

    return React.createElement('div', { className: 'dl-dots' },
      dots,
      React.createElement('span', { className: 'dl-count' }, countText.join(' '))
    );
  }

  // Day cell
  function DayCell({ cell, dayData, isToday, isSelected, onClick }) {
    var dateStr = formatDate(cell.year, cell.month, cell.day);
    var isActive = cell.current && dayData;
    var sessionCount = isActive ? (dayData.session_count || 0) : 0;
    var cronCount = isActive ? (dayData.cron_run_count || 0) : 0;
    var hasRecap = isActive ? !!dayData.has_recap : false;
    var recapStale = isActive ? !!dayData.recap_stale : false;

    var classes = 'dl-cell';
    if (!cell.current) classes += ' dl-cell-empty';
    if (isToday) classes += ' dl-cell-today';
    if (isSelected) classes += ' dl-cell-selected';

    return React.createElement('div', {
      className: classes,
      tabIndex: isActive ? 0 : -1,
      role: 'button',
      'aria-label': (isActive ? MONTH_NAMES[cell.month] + ' ' + cell.day + ', ' + cell.year + ': ' + sessionCount + ' sessions, ' + cronCount + ' cron runs' : MONTH_NAMES[cell.month] + ' ' + cell.day),
      'aria-pressed': isSelected || null,
      onClick: isActive ? function (e) { e.stopPropagation(); onClick(dateStr); } : undefined,
      onKeyDown: isActive ? function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); onClick(dateStr); } } : undefined,
    },
      React.createElement('span', { className: 'dl-day-number' }, cell.day),
      isActive && ActivityDots({ sessionCount: sessionCount, cronCount: cronCount, hasRecap: hasRecap, recapStale: recapStale })
    );
  }

  // Month grid
  function MonthGrid({ cells, year, month, dayMap, selectedDate, onDayClick }) {
    var chicag = getChicagoNow();
    var todayStr = formatDate(chicag.year, chicag.month, chicag.day);

    return React.createElement('div', { className: 'dl-grid', role: 'grid', 'aria-label': 'Calendar for ' + monthTitle(year, month) },
      cells.map(function (cell, idx) {
        var dateStr = formatDate(cell.year, cell.month, cell.day);
        var dayData = cell.current && dayMap ? dayMap[dateStr] : null;
        var isToday = dateStr === todayStr;
        var isSelected = dateStr === selectedDate;
        return React.createElement(DayCell, {
          key: dateStr + '-' + idx,
          cell: cell,
          dayData: dayData,
          isToday: isToday,
          isSelected: isSelected,
          onClick: onDayClick,
        });
      })
    );
  }

  // Optional daily roll-up built only from saved current session summaries.
  function RollupSection({ dateStr, rollupData, generating, currentSummaryCount, activeSessionCount, error, onGenerate, onRollback }) {
    var exists = !!(rollupData && rollupData.exists);
    var stale = !!(rollupData && rollupData.stale);
    var data = exists && rollupData.data ? rollupData.data : {};
    var overall = sanitize(data.overall_recap || '');
    var coverage = data.coverage || {
      included: currentSummaryCount || 0,
      active: activeSessionCount || 0,
    };
    var included = Number(coverage.included || 0);
    var active = Number(coverage.active || 0);
    var versions = rollupData && rollupData.versions ? rollupData.versions : [];
    var metaInfo = [];
    if (rollupData && rollupData.meta && rollupData.meta.generated_at) {
      metaInfo.push('Generated: ' + isoTimeAgo(rollupData.meta.generated_at));
    }
    if (rollupData && rollupData.meta && rollupData.meta.model) {
      metaInfo.push(sanitize(rollupData.meta.model));
    }
    var jobError = rollupData && rollupData.job_status && rollupData.job_status.status === 'failed'
      ? sanitize(rollupData.job_status.error || 'Roll-up generation failed')
      : '';
    var disabled = !!generating || currentSummaryCount < 1;
    var buttonLabel = exists
      ? (stale ? '\u21bb Regenerate stale roll-up' : '\u21bb Force regenerate roll-up')
      : 'Generate daily roll-up';

    return React.createElement('div', { className: 'dl-recap-section dl-rollup-section' },
      React.createElement('div', { className: 'dl-session-heading' },
        React.createElement('div', { className: 'dl-section-title' }, 'Daily Roll-up'),
        React.createElement(C.Button || 'button', {
          className: 'dl-generate-btn',
          disabled: disabled,
          onClick: onGenerate,
          'aria-label': buttonLabel + ' for ' + dateStr,
        }, generating ? 'Generating roll-up\u2026' : buttonLabel)
      ),
      React.createElement('div', { className: 'dl-coverage' },
        'Roll-up covers ' + included + ' of ' + active + ' sessions.'
      ),
      currentSummaryCount < 1 && React.createElement('div', { className: 'dl-placeholder' },
        'Generate at least one current session summary before creating a daily roll-up.'
      ),
      generating && React.createElement('div', { className: 'dl-generating' },
        React.createElement('span', { className: 'dl-spinner' }),
        'Generating roll-up\u2026'
      ),
      metaInfo.length > 0 && React.createElement('div', { className: 'dl-meta' },
        metaInfo.map(function (item) { return React.createElement('span', { key: item }, item); })
      ),
      stale && React.createElement('div', { className: 'dl-stale-banner' },
        'Stale: the active session set or one of its saved summaries changed after this roll-up.'
      ),
      overall && React.createElement('div', { className: 'dl-recap-content' }, overall),
      (error || jobError) && ErrorMessage({ message: error || jobError }),
      versions.length > 0 && React.createElement(VersionButtons, {
        versions: versions,
        onRestore: function (versionId) { onRollback(dateStr, versionId); },
      })
    );
  }

  // Day detail panel with controlled batch selection rendering
  function DayDetailPanel({ dateStr, dayData, sessionSummaries, rollupData, activeJobs, jobErrors, loadingDay, error, onGenerateSession, onRollbackSession, onGenerateRollup, onRollbackRollup, selectedKeys, onSelectAll, onClear, batchStatus, batchError, regenerateCurrent, onSubmitBatch, onSelectSession, onDeselectSession, onToggleRegenerate, showAutoTitled, onToggleShowAutoTitled }) {
    var batchLocked = isBatchLocked(batchStatus);
    var isBatchRunning = (batchStatus && String(batchStatus.status || '').toLowerCase() === 'running');
    var isBatchQueued = (batchStatus && String(batchStatus.status || '').toLowerCase() === 'queued');
    if (loadingDay) {
      return React.createElement('div', { className: 'dl-detail-panel' },
        React.createElement('div', { className: 'dl-detail-date' }, dateStr),
        LoadingBar()
      );
    }

    if (!dayData) {
      return React.createElement('div', { className: 'dl-detail-panel' },
        React.createElement(Placeholder, { text: 'No data available for this day.' })
      );
    }

    var sessions = dayData.sessions || [];
    var cronRuns = dayData.cron_runs || [];
    var showAllSessions = showAutoTitled !== false;
    var visibleSessions = getVisibleSessions(sessions, showAllSessions);
    var hiddenAutoTitledCount = sessions.length - visibleSessions.length;
    var visibleSelectedKeys = filterSelectedSessionKeys(
      dateStr, sessions, selectedKeys || {}, showAllSessions
    );
    var currentSummaryCount = 0;
    for (var i = 0; i < sessions.length; i++) {
      var key = sessionKey(sessions[i], dateStr);
      var detail = sessionSummaries[key] || sessions[i].summary_status || {};
      if (detail.exists && !detail.stale) currentSummaryCount++;
    }

    // Build member status map from batch status
    var memberStatusMap = batchMemberStatusMap(dateStr, batchStatus);

    return React.createElement('div', { className: 'dl-detail-panel' },
      React.createElement('div', { className: 'dl-detail-date' }, dateStr),
      error && ErrorMessage({ message: error }),
      React.createElement('div', { className: 'dl-auto-titled-control' },
        React.createElement('label', { className: 'dl-auto-titled-label' },
          React.createElement('input', {
            type: 'checkbox',
            className: 'dl-auto-titled-checkbox',
            checked: showAllSessions,
            disabled: batchLocked,
            onChange: function (event) {
              if (onToggleShowAutoTitled) onToggleShowAutoTitled(!!event.target.checked);
            },
            'aria-label': 'Show agent-generated sessions',
          }),
          'Show agent-generated sessions'
        ),
        React.createElement('span', { className: 'dl-auto-titled-count', role: 'status' },
          visibleSessions.length + ' shown · ' + hiddenAutoTitledCount + ' auto-titled hidden'
        )
      ),
      // Render BatchToolbar before session cards whenever dayData exists (including zero sessions)
      React.createElement(BatchToolbar, {
        dateStr: dateStr,
        sessions: visibleSessions,
        selectedKeys: visibleSelectedKeys,
        regenerateCurrent: !!regenerateCurrent,
        batchStatus: batchStatus,
        batchError: batchError,
        onSelectAll: onSelectAll,
        onClear: onClear,
        onToggleRegenerate: onToggleRegenerate,
        onSubmit: onSubmitBatch,
      }),
      visibleSessions.length > 0 && React.createElement(React.Fragment, null,
        React.createElement('div', { className: 'dl-section-title' }, visibleSessions.length + (visibleSessions.length === 1 ? ' Session' : ' Sessions')),
        visibleSessions.map(function (s) {
          var key = sessionKey(s, dateStr);
          var isSelected = visibleSelectedKeys.hasOwnProperty(key);
          var isCron = !s.session_id; // cron cards have no session_id
          var selectionDisabled = isCron;
          var memberStatus = memberStatusMap[key] || null;
          return React.createElement(SessionCard, {
            key: key,
            dateStr: dateStr,
            session: s,
            summaryData: sessionSummaries[key] || null,
            selected: isSelected,
            selectionDisabled: selectionDisabled,
            batchMemberStatus: memberStatus,
            error: jobErrors[key] || '',
            onRollback: onRollbackSession,
            onToggleSelect: function (session) {
              if (isSelected) {
                onDeselectSession && onDeselectSession(session);
              } else {
                onSelectSession && onSelectSession(session);
              }
            },
          });
        })
      ),
      cronRuns.length > 0 && React.createElement(React.Fragment, null,
        React.createElement('div', { className: 'dl-section-title' }, cronRuns.length + (cronRuns.length === 1 ? ' Cron Run' : ' Cron Runs')),
        cronRuns.map(function (c) { return React.createElement(CronCard, { key: c.execution_id || c.job_id, cron: c }); })
      ),
      visibleSessions.length === 0 && cronRuns.length === 0 && React.createElement(Placeholder, {
        text: sessions.length > 0 ? 'All sessions for this day are hidden by the visibility filter.' : 'No activity recorded for this day.'
      }),
      RollupSection({
        dateStr: dateStr,
        rollupData: rollupData,
        generating: !!activeJobs['rollup:' + dateStr],
        currentSummaryCount: currentSummaryCount,
        activeSessionCount: sessions.length,
        error: jobErrors['rollup:' + dateStr] || '',
        onGenerate: function () { onGenerateRollup(dateStr, !!(rollupData && rollupData.exists)); },
        onRollback: onRollbackRollup,
      })
    );
  }

  // Health status bar
  function HealthBar({ health }) {
    if (!health) return null;
    var statusClass = health.status === 'ok' ? 'dl-health-ok' :
                      health.status === 'degraded' ? 'dl-health-degraded' :
                      'dl-health-error';
    var statusText = health.status === 'ok' ? 'Plugin OK' :
                     health.status === 'degraded' ? 'Degraded: some sources unreadable' :
                     'Error: no readable sources';

    return React.createElement('div', { className: 'dl-health-bar ' + statusClass, role: 'status', 'aria-label': statusText },
      React.createElement('span', { className: 'dl-health-dot' }),
      sanitize(statusText),
      health.unreadable_sources && health.unreadable_sources.length > 0 &&
        React.createElement('span', { style: { fontSize: '0.72rem', color: 'var(--dl-destructive)' } }, '(missing: ' + health.unreadable_sources.map(sanitize).join(', ') + ')')
    );
  }

  // -----------------------------------------------------------------------
  // Main Calendar Page
  // -----------------------------------------------------------------------

  function CalendarPage() {
    var chicagoNow = getChicagoNow();
    var todayStr = formatDate(chicagoNow.year, chicagoNow.month, chicagoNow.day);
    var initialShowAutoTitled = loadBrowserShowAutoTitled();

    var state = useState({
      viewYear: chicagoNow.year,
      viewMonth: chicagoNow.month, // 0-indexed
      selectedDate: todayStr,
      monthData: null,
      dayData: null,
      sessionSummaries: {},
      rollupData: null,
      activeJobs: {},
      jobErrors: {},
      health: null,
      loadingMonth: true,
      loadingDay: false,
      error: '',
      selectedSessions: {},
      regenerateCurrent: false,
      batchStatus: null,
      batchError: '',
      showAutoTitled: initialShowAutoTitled,
    });

    var setState = state[1];
    var stateVal = state[0];

    // One cancellable poll per composite session identity or daily roll-up.
    var pollRefs = useRef({});
    var selectedDateRef = useRef(stateVal.selectedDate);
    var batchSubmitRef = useRef(false);
    selectedDateRef.current = stateVal.selectedDate;

    var updateState = useCallback(function (patch) {
      setState(function (s) { return Object.assign({}, s, patch); });
    }, [setState]);

    // Month data as a lookup map
    var monthDayMap = useMemo(function () {
      if (!stateVal.monthData || !Array.isArray(stateVal.monthData.days)) return {};
      var map = {};
      for (var i = 0; i < stateVal.monthData.days.length; i++) {
        map[stateVal.monthData.days[i].date] = stateVal.monthData.days[i];
      }
      return map;
    }, [stateVal.monthData]);

    // Load health
    var loadHealth = useCallback(function () {
      apiGet('/health').then(function (data) {
        setState(function (s) { return Object.assign({}, s, { health: data }); });
      }).catch(function (err) {
        console.warn('[summarization-calendar] Health check failed:', err);
      });
    }, [setState]);

    // Load month grid data
    var loadMonth = useCallback(function (year, month) {
      setState(function (s) { return Object.assign({}, s, { loadingMonth: true }); });
      apiGet('/month' + encodeUrlParams({ year: String(year), month: String(month + 1) }))
        .then(function (data) {
          setState(function (s) { return Object.assign({}, s, {
            monthData: data,
            loadingMonth: false,
            error: '',
          }); });
        })
        .catch(function (err) {
          setState(function (s) { return Object.assign({}, s, {
            loadingMonth: false,
            error: 'Failed to load month: ' + sanitize(String(err.message || err)),
          }); });
        });
    }, [setState]);

    function batchPollKey(dateStr) {
      return 'batch:' + String(dateStr || '');
    }

    function cancelBatchPoll(dateStr) {
      var key = batchPollKey(dateStr);
      if (pollRefs.current[key]) {
        pollRefs.current[key]();
        delete pollRefs.current[key];
      }
    }

    function cancelAllBatchPolls() {
      Object.keys(pollRefs.current).forEach(function (key) {
        if (key.indexOf('batch:') === 0) {
          pollRefs.current[key]();
          delete pollRefs.current[key];
        }
      });
    }

    function beginBatchPoll(dateStr, batchId) {
      if (!dateStr || !batchId) return;
      var key = batchPollKey(dateStr);
      cancelBatchPoll(dateStr);
      var path = '/session-summary/batch' + encodeUrlParams({
        date: dateStr,
        batch_id: batchId,
      });
      pollRefs.current[key] = pollBatchStatus(
        path,
        function (batch) {
          if (selectedDateRef.current !== dateStr) return;
          setState(function (s) {
            if (s.selectedDate !== dateStr) return s;
            return Object.assign({}, s, { batchStatus: batch, batchError: '' });
          });
        },
        function (batch) {
          delete pollRefs.current[key];
          if (selectedDateRef.current === dateStr) {
            setState(function (s) {
              if (s.selectedDate !== dateStr) return s;
              return Object.assign({}, s, { batchStatus: batch, batchError: '' });
            });
            loadDay(dateStr, { skipBatchDiscovery: true });
            var parsed = parseDate(dateStr);
            loadMonth(parsed.year, parsed.month);
          }
        },
        function (message) {
          delete pollRefs.current[key];
          if (selectedDateRef.current !== dateStr) return;
          setState(function (s) {
            if (s.selectedDate !== dateStr) return s;
            return Object.assign({}, s, { batchError: sanitize(message) });
          });
        },
        JOB_POLL_MAX_ATTEMPTS,
        2000
      );
    }

    function discoverLatestBatch(dateStr) {
      apiGet('/session-summary/batches' + encodeUrlParams({ date: dateStr, limit: '1' }))
        .then(function (response) {
          if (selectedDateRef.current !== dateStr) return;
          var batch = newestBatchForDate(response, dateStr);
          if (!batch) return;
          setState(function (s) {
            if (s.selectedDate !== dateStr) return s;
            return Object.assign({}, s, { batchStatus: batch, batchError: '' });
          });
          if (isBatchLocked(batch)) beginBatchPoll(dateStr, batch.batch_id);
        })
        .catch(function (err) {
          if (selectedDateRef.current !== dateStr) return;
          setState(function (s) {
            if (s.selectedDate !== dateStr) return s;
            return Object.assign({}, s, {
              batchError: 'Failed to recover batch status: ' + sanitize(String(err.message || err)),
            });
          });
        });
    }

    // Load day metadata, current summary artifacts, and summary-only roll-up.
    var loadDay = useCallback(function (dateStr, options) {
      if (!dateStr) return;
      if (!(options && options.skipBatchDiscovery)) discoverLatestBatch(dateStr);
      setState(function (s) { return Object.assign({}, s, {
        loadingDay: true,
        selectedDate: dateStr,
        dayData: null,
        sessionSummaries: {},
        rollupData: null,
      }); });

      apiGet('/day' + encodeUrlParams({ date: dateStr }))
        .then(function (data) {
          setState(function (s) {
            if (s.selectedDate !== dateStr) return s;
            return Object.assign({}, s, { dayData: data, loadingDay: false, error: '' });
          });
          (data.sessions || []).forEach(function (session) {
            var status = session.summary_status || {};
            if (!status.exists && !(status.job_status && status.job_status.status === 'running')) return;
            var key = sessionKey(session, dateStr);
            var path = '/session-summary' + encodeUrlParams({
              date: dateStr,
              profile: session.profile,
              session_id: session.session_id,
            });
            apiGet(path).then(function (detail) {
              setState(function (s) {
                if (s.selectedDate !== dateStr) return s;
                var summaries = Object.assign({}, s.sessionSummaries);
                summaries[key] = detail;
                return Object.assign({}, s, { sessionSummaries: summaries });
              });
              if (detail.job_status && detail.job_status.status === 'running') {
                beginSessionPoll(dateStr, session, detail.meta && detail.meta.version_id);
              }
            }).catch(function () { /* per-card status remains available */ });
          });
          apiGet('/rollup' + encodeUrlParams({ date: dateStr }))
            .then(function (rollup) {
              setState(function (s) {
                return s.selectedDate === dateStr ? Object.assign({}, s, { rollupData: rollup }) : s;
              });
              if (rollup.job_status && rollup.job_status.status === 'running') {
                beginRollupPoll(dateStr, rollup.meta && rollup.meta.version_id);
              }
            })
            .catch(function () {
              setState(function (s) {
                return s.selectedDate === dateStr ? Object.assign({}, s, { rollupData: { exists: false } }) : s;
              });
            });
        })
        .catch(function (err) {
          setState(function (s) {
            if (s.selectedDate !== dateStr) return s;
            return Object.assign({}, s, {
              loadingDay: false,
              error: 'Failed to load day data: ' + sanitize(String(err.message || err)),
            });
          });
        });
    }, [setState]);

    function updateJobState(key, active, errorMessage) {
      setState(function (s) {
        var activeJobs = Object.assign({}, s.activeJobs);
        var jobErrors = Object.assign({}, s.jobErrors);
        if (active) activeJobs[key] = true;
        else delete activeJobs[key];
        if (errorMessage) jobErrors[key] = sanitize(errorMessage);
        else delete jobErrors[key];
        return Object.assign({}, s, { activeJobs: activeJobs, jobErrors: jobErrors });
      });
    }

    function beginSessionPoll(dateStr, session, previousVersion) {
      var key = sessionKey(session, dateStr);
      if (pollRefs.current[key]) pollRefs.current[key]();
      updateJobState(key, true, '');
      var path = '/session-summary' + encodeUrlParams({
        date: dateStr,
        profile: session.profile,
        session_id: session.session_id,
      });
      pollRefs.current[key] = pollArtifact(path, previousVersion, function (detail) {
        delete pollRefs.current[key];
        setState(function (s) {
          var activeJobs = Object.assign({}, s.activeJobs);
          var jobErrors = Object.assign({}, s.jobErrors);
          var summaries = Object.assign({}, s.sessionSummaries);
          delete activeJobs[key];
          delete jobErrors[key];
          if (s.selectedDate === dateStr) summaries[key] = detail;
          return Object.assign({}, s, {
            activeJobs: activeJobs,
            jobErrors: jobErrors,
            sessionSummaries: summaries,
          });
        });
      }, function (message) {
        delete pollRefs.current[key];
        updateJobState(key, false, 'Session summary failed: ' + message);
      }, JOB_POLL_MAX_ATTEMPTS, 2000);
    }

    function beginRollupPoll(dateStr, previousVersion) {
      var key = 'rollup:' + dateStr;
      if (pollRefs.current[key]) pollRefs.current[key]();
      updateJobState(key, true, '');
      var path = '/rollup' + encodeUrlParams({ date: dateStr });
      pollRefs.current[key] = pollArtifact(path, previousVersion, function (detail) {
        delete pollRefs.current[key];
        setState(function (s) {
          var activeJobs = Object.assign({}, s.activeJobs);
          var jobErrors = Object.assign({}, s.jobErrors);
          delete activeJobs[key];
          delete jobErrors[key];
          return Object.assign({}, s, {
            activeJobs: activeJobs,
            jobErrors: jobErrors,
            rollupData: s.selectedDate === dateStr ? detail : s.rollupData,
          });
        });
        loadMonth(stateVal.viewYear, stateVal.viewMonth);
      }, function (message) {
        delete pollRefs.current[key];
        updateJobState(key, false, 'Daily roll-up failed: ' + message);
      }, JOB_POLL_MAX_ATTEMPTS, 2000);
    }

    var generateSessionSummary = useCallback(function (dateStr, session, exists) {
      var key = sessionKey(session, dateStr);
      if (pollRefs.current[key]) return;
      pollRefs.current[key] = function () {};
      var current = stateVal.sessionSummaries[key] || session.summary_status || {};
      var previousVersion = current.meta && current.meta.version_id
        ? current.meta.version_id
        : current.version_id || null;
      updateJobState(key, true, '');
      apiPost('/session-summary' + encodeUrlParams({
        date: dateStr,
        profile: session.profile,
        session_id: session.session_id,
      }), { force_regenerate: !!exists })
        .then(function () {
          beginSessionPoll(dateStr, session, previousVersion);
        })
        .catch(function (err) {
          delete pollRefs.current[key];
          updateJobState(key, false, 'Failed to start session summary: ' + sanitize(String(err.message || err)));
        });
    }, [setState, stateVal.sessionSummaries]);

    var generateRollup = useCallback(function (dateStr, exists) {
      var key = 'rollup:' + dateStr;
      if (pollRefs.current[key]) return;
      pollRefs.current[key] = function () {};
      var previousVersion = stateVal.rollupData && stateVal.rollupData.meta
        ? stateVal.rollupData.meta.version_id
        : null;
      updateJobState(key, true, '');
      apiPost('/rollup' + encodeUrlParams({ date: dateStr }), {
        force_regenerate: !!exists,
      }).then(function () {
        beginRollupPoll(dateStr, previousVersion);
      }).catch(function (err) {
        delete pollRefs.current[key];
        updateJobState(key, false, 'Failed to start daily roll-up: ' + sanitize(String(err.message || err)));
      });
    }, [setState, stateVal.rollupData]);

    var rollbackSessionSummary = useCallback(function (dateStr, session, versionId) {
      var key = sessionKey(session, dateStr);
      var params = {
        date: dateStr,
        profile: session.profile,
        session_id: session.session_id,
        version: versionId,
      };
      apiPost('/session-summary/rollback' + encodeUrlParams(params), {})
        .then(function () {
          return apiGet('/session-summary' + encodeUrlParams({
            date: dateStr,
            profile: session.profile,
            session_id: session.session_id,
          }));
        })
        .then(function (detail) {
          setState(function (s) {
            var summaries = Object.assign({}, s.sessionSummaries);
            summaries[key] = detail;
            return Object.assign({}, s, { sessionSummaries: summaries });
          });
          return apiGet('/rollup' + encodeUrlParams({ date: dateStr }));
        })
        .then(function (rollup) {
          setState(function (s) { return Object.assign({}, s, { rollupData: rollup }); });
        })
        .catch(function (err) {
          updateJobState(key, false, 'Session restore failed: ' + sanitize(String(err.message || err)));
        });
    }, [setState]);

    var rollbackRollup = useCallback(function (dateStr, versionId) {
      var key = 'rollup:' + dateStr;
      apiPost('/rollup/rollback' + encodeUrlParams({ date: dateStr, version: versionId }), {})
        .then(function () { return apiGet('/rollup' + encodeUrlParams({ date: dateStr })); })
        .then(function (rollup) {
          setState(function (s) { return Object.assign({}, s, { rollupData: rollup }); });
          loadMonth(stateVal.viewYear, stateVal.viewMonth);
        })
        .catch(function (err) {
          updateJobState(key, false, 'Roll-up restore failed: ' + sanitize(String(err.message || err)));
        });
    }, [setState, stateVal.viewYear, stateVal.viewMonth]);

    // Navigation
    var navigatePrev = useCallback(function () {
      cancelAllBatchPolls();
      setState(function (s) {
        var newMonth = s.viewMonth - 1;
        var newYear = s.viewYear;
        if (newMonth < 0) { newMonth = 11; newYear--; }
        return Object.assign({}, s, {
          viewMonth: newMonth,
          viewYear: newYear,
          selectedDate: null,
          selectedSessions: {},
          regenerateCurrent: false,
          batchStatus: null,
          batchError: '',
        });
      });
    }, [setState]);

    var navigateNext = useCallback(function () {
      cancelAllBatchPolls();
      setState(function (s) {
        var newMonth = s.viewMonth + 1;
        var newYear = s.viewYear;
        if (newMonth > 11) { newMonth = 0; newYear++; }
        return Object.assign({}, s, {
          viewMonth: newMonth,
          viewYear: newYear,
          selectedDate: null,
          selectedSessions: {},
          regenerateCurrent: false,
          batchStatus: null,
          batchError: '',
        });
      });
    }, [setState]);

    var navigateToday = useCallback(function () {
      if (stateVal.selectedDate !== todayStr) cancelAllBatchPolls();
      setState(function (s) {
        var next = {
          viewYear: chicagoNow.year,
          viewMonth: chicagoNow.month,
          selectedDate: todayStr,
        };
        if (s.selectedDate !== todayStr) {
          next.selectedSessions = {};
          next.regenerateCurrent = false;
          next.batchStatus = null;
          next.batchError = '';
        }
        return Object.assign({}, s, next);
      });
    }, [setState, stateVal.selectedDate, chicagoNow.year, chicagoNow.month, todayStr]);

    var handleDayClick = useCallback(function (dateStr) {
      if (stateVal.selectedDate !== dateStr) cancelAllBatchPolls();
      setState(function (s) {
        // If date is changing, clear selection/regenerate/batch/error
        if (s.selectedDate !== dateStr) {
          return Object.assign({}, s, { selectedDate: dateStr, selectedSessions: {}, regenerateCurrent: false, batchStatus: null, batchError: '' });
        }
        return Object.assign({}, s, { selectedDate: dateStr });
      });
    }, [setState, stateVal.selectedDate]);

    // Batch selection helpers
    var onSelectAllSessions = useCallback(function (dateStr) {
      setState(function (s) {
        if (isBatchLocked(s.batchStatus)) return s;
        if (!s.dayData || !Array.isArray(s.dayData.sessions)) return s;
        var visibleSessions = getVisibleSessions(s.dayData.sessions, s.showAutoTitled);
        var newSelected = {};
        for (var i = 0; i < visibleSessions.length; i++) {
          var session = visibleSessions[i];
          var key = batchSelectionKey(dateStr, session);
          newSelected[key] = true;
        }
        return Object.assign({}, s, { selectedSessions: newSelected });
      });
    }, [setState]);

    var onClearSelection = useCallback(function () {
      setState(function (s) {
        if (isBatchLocked(s.batchStatus)) return s;
        return Object.assign({}, s, { selectedSessions: {} });
      });
    }, [setState]);

    var onSelectSession = useCallback(function (dateStr, session) {
      setState(function (s) {
        if (isBatchLocked(s.batchStatus)) return s;
        if (s.showAutoTitled === false && isAutoTitledSession(session && session.title)) return s;
        var key = batchSelectionKey(dateStr, session);
        var newSelected = Object.assign({}, s.selectedSessions);
        newSelected[key] = true;
        return Object.assign({}, s, { selectedSessions: newSelected });
      });
    }, [setState]);

    var onDeselectSession = useCallback(function (dateStr, session) {
      setState(function (s) {
        if (isBatchLocked(s.batchStatus)) return s;
        var key = batchSelectionKey(dateStr, session);
        var newSelected = Object.assign({}, s.selectedSessions);
        delete newSelected[key];
        return Object.assign({}, s, { selectedSessions: newSelected });
      });
    }, [setState]);

    var onToggleRegenerate = useCallback(function (value) {
      setState(function (s) {
        if (isBatchLocked(s.batchStatus)) return s;
        return Object.assign({}, s, { regenerateCurrent: !!value });
      });
    }, [setState]);

    var onToggleShowAutoTitled = useCallback(function (value) {
      var nextValue = !!value;
      if (isBatchLocked(stateVal.batchStatus)) return;
      saveBrowserShowAutoTitled(nextValue);
      setState(function (s) {
        if (isBatchLocked(s.batchStatus)) return s;
        var sessions = s.dayData && Array.isArray(s.dayData.sessions) ? s.dayData.sessions : [];
        var selectedSessions = nextValue
          ? Object.assign({}, s.selectedSessions)
          : filterSelectedSessionKeys(s.selectedDate, sessions, s.selectedSessions, false);
        return Object.assign({}, s, {
          showAutoTitled: nextValue,
          selectedSessions: selectedSessions,
        });
      });
    }, [setState, stateVal.batchStatus]);

    var onSubmitBatch = useCallback(function () {
      var dateStr = stateVal.selectedDate;
      if (!dateStr || batchSubmitRef.current || isBatchLocked(stateVal.batchStatus)) return;
      var visibleSessions = stateVal.dayData && Array.isArray(stateVal.dayData.sessions)
        ? getVisibleSessions(stateVal.dayData.sessions, stateVal.showAutoTitled) : [];
      var payload = buildBatchRequest(
        dateStr,
        visibleSessions,
        stateVal.selectedSessions,
        stateVal.regenerateCurrent
      );
      if (!payload.sessions.length) {
        setState(function (s) {
          return Object.assign({}, s, { batchError: 'Select at least one session.' });
        });
        return;
      }

      batchSubmitRef.current = true;
      var queuedMembers = payload.sessions.map(function (session) {
        return { profile: session.profile, session_id: session.session_id, status: 'queued' };
      });
      setState(function (s) {
        if (s.selectedDate !== dateStr) return s;
        return Object.assign({}, s, {
          batchStatus: {
            status: 'queued',
            date: dateStr,
            total: queuedMembers.length,
            members: queuedMembers,
          },
          batchError: '',
        });
      });

      apiPost('/session-summary/batch' + encodeUrlParams({ date: dateStr }), payload)
        .then(function (batch) {
          batchSubmitRef.current = false;
          if (!batch || !batch.batch_id || !batch.status) {
            throw new Error('Batch submission returned an invalid response');
          }
          if (selectedDateRef.current !== dateStr) return;
          setState(function (s) {
            if (s.selectedDate !== dateStr) return s;
            return Object.assign({}, s, { batchStatus: batch, batchError: '' });
          });
          if (isBatchLocked(batch)) {
            beginBatchPoll(dateStr, batch.batch_id);
          } else {
            loadDay(dateStr, { skipBatchDiscovery: true });
            var parsed = parseDate(dateStr);
            loadMonth(parsed.year, parsed.month);
          }
        })
        .catch(function (err) {
          batchSubmitRef.current = false;
          if (selectedDateRef.current !== dateStr) return;
          setState(function (s) {
            if (s.selectedDate !== dateStr) return s;
            return Object.assign({}, s, {
              batchStatus: null,
              batchError: 'Batch submission failed: ' + sanitize(String(err.message || err)),
            });
          });
        });
    }, [
      setState,
      stateVal.selectedDate,
      stateVal.dayData,
      stateVal.selectedSessions,
      stateVal.regenerateCurrent,
      stateVal.batchStatus,
      stateVal.showAutoTitled,
    ]);

    // Initial health load. Month data is handled by the navigation effect
    // below so the first render issues exactly one month request.
    useEffect(function () {
      loadHealth();
    }, []);

    // Reload month when navigation changes
    useEffect(function () {
      loadMonth(stateVal.viewYear, stateVal.viewMonth);
    }, [stateVal.viewYear, stateVal.viewMonth]);

    // Reload day when selection changes
    useEffect(function () {
      if (stateVal.selectedDate) {
        loadDay(stateVal.selectedDate);
      }
    }, [stateVal.selectedDate]);

    // Cleanup UI polling on unmount. Server jobs are intentionally not cancelled.
    useEffect(function () {
      return function () {
        Object.keys(pollRefs.current).forEach(function (key) {
          try { pollRefs.current[key](); } catch (_err) { /* ignore cleanup */ }
        });
        pollRefs.current = {};
      };
    }, []);

    // Build calendar grid cells
    var cells = buildCalendarGrid(stateVal.viewYear, stateVal.viewMonth);

    // Render
    return React.createElement('div', { className: 'dl-calendar' },
      // Health bar
      HealthBar({ health: stateVal.health }),

      // Error banner
      stateVal.error && ErrorMessage({ message: stateVal.error }),

      // Month navigation header
      React.createElement('div', { className: 'dl-month-header' },
        React.createElement(C.Button || 'button', {
          className: 'dl-nav-btn',
          onClick: navigatePrev,
          'aria-label': 'Previous month',
        }, '\u25C0'),
        React.createElement('span', { className: 'dl-month-title' }, monthTitle(stateVal.viewYear, stateVal.viewMonth)),
        React.createElement(C.Button || 'button', {
          className: 'dl-nav-btn',
          onClick: navigateNext,
          'aria-label': 'Next month',
        }, '\u25B6'),
        React.createElement(C.Button || 'button', {
          className: 'dl-today-btn',
          onClick: navigateToday,
          'aria-label': 'Go to current month',
        }, 'Today')
      ),

      // Loading overlay for month data
      stateVal.loadingMonth && React.createElement('div', { style: { padding: '0.5rem 0' } }, LoadingBar()),

      // Weekday headers (Monday first)
      React.createElement('div', { className: 'dl-weekdays' },
        WEEKDAY_SHORT.map(function (d) {
          return React.createElement('div', { key: d, className: 'dl-weekday' }, d);
        })
      ),

      // Calendar grid
      !stateVal.loadingMonth && React.createElement(MonthGrid, {
        cells: cells,
        year: stateVal.viewYear,
        month: stateVal.viewMonth,
        dayMap: monthDayMap,
        selectedDate: stateVal.selectedDate,
        onDayClick: handleDayClick,
      }),

      // Day detail panel (shown when a day is selected)
      stateVal.selectedDate && React.createElement(DayDetailPanel, {
        dateStr: stateVal.selectedDate,
        dayData: stateVal.dayData,
        sessionSummaries: stateVal.sessionSummaries,
        rollupData: stateVal.rollupData,
        activeJobs: stateVal.activeJobs,
        jobErrors: stateVal.jobErrors,
        loadingDay: stateVal.loadingDay,
        error: stateVal.error,
        onGenerateSession: generateSessionSummary,
        onRollbackSession: rollbackSessionSummary,
        onGenerateRollup: generateRollup,
        onRollbackRollup: rollbackRollup,
        selectedKeys: stateVal.selectedSessions,
        onSelectAll: function () { onSelectAllSessions(stateVal.selectedDate); },
        onClear: function () { onClearSelection(); },
        batchStatus: stateVal.batchStatus,
        batchError: stateVal.batchError,
        regenerateCurrent: stateVal.regenerateCurrent,
        onSubmitBatch: function () { onSubmitBatch(); },
        onSelectSession: function (session) { onSelectSession(stateVal.selectedDate, session); },
        onDeselectSession: function (session) { onDeselectSession(stateVal.selectedDate, session); },
        onToggleRegenerate: function (value) { onToggleRegenerate(value); },
        showAutoTitled: stateVal.showAutoTitled,
        onToggleShowAutoTitled: function (value) { onToggleShowAutoTitled(value); },
      })
    );
  }

  // -----------------------------------------------------------------------
  // Registration
  // -----------------------------------------------------------------------

  try {
    window.__HERMES_PLUGINS__.register('summarization-calendar', CalendarPage);
  } catch (e) {
    console.error('[summarization-calendar] Failed to register plugin:', String(e.message || e));
  }

})();
