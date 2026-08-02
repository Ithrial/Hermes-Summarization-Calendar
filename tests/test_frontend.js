'use strict';

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function loadPlugin(options = {}) {
  const file = path.join(__dirname, '..', 'dashboard', 'dist', 'index.js');
  const source = fs.readFileSync(file, 'utf8');
  const marker = '  // -----------------------------------------------------------------------\n  // Components';
  assert.ok(source.includes(marker), 'frontend test instrumentation marker missing');
  const expose = [
    '  window.__DAILY_LEDGER_TEST__ = {',
    '    getChicagoNow: getChicagoNow,',
    '    formatDate: formatDate,',
    '    buildCalendarGrid: buildCalendarGrid,',
    '    sessionKey: sessionKey,',
    '    sessionChatHref: typeof sessionChatHref === \'function\' ? sessionChatHref : undefined,',
    '    pollArtifact: pollArtifact,',
    '    JOB_POLL_MAX_ATTEMPTS: JOB_POLL_MAX_ATTEMPTS,',
    '    SessionCard: SessionCard,',
    '    BatchToolbar: BatchToolbar,',
    '    DayDetailPanel: DayDetailPanel,',
    '    MonthGrid: MonthGrid,',
    '    RollupSection: RollupSection,',
    '    CalendarPage: CalendarPage,',
    '    isBatchLocked: isBatchLocked,',
    '    batchSelectionKey: batchSelectionKey,',
    '    buildBatchRequest: buildBatchRequest,',
    '    summarizeBatchProgress: summarizeBatchProgress,',
    '    batchMemberStatusMap: batchMemberStatusMap,',
    '    newestBatchForDate: newestBatchForDate,',
    '    pollBatchStatus: pollBatchStatus',
    '  };',
    '',
  ].join('\n');
  const instrumented = source.replace(marker, expose + marker);

  const captured = { component: null, initialState: null, effects: [] };
  let statefulValue;
  let statefulInitialized = false;
  const refSlots = [];
  let refCursor = 0;
  function applyState(update) {
    statefulValue = typeof update === 'function' ? update(statefulValue) : update;
    return statefulValue;
  }
  const React = {
    Fragment: 'fragment',
    createElement(type, props, ...children) {
      return { type, props: props || {}, children };
    },
  };
  const hooks = {
    useState(initial) {
      if (!options.stateful) {
        captured.initialState = initial;
        return [initial, function noop() {}];
      }
      if (!statefulInitialized) {
        captured.initialState = initial;
        statefulValue = initial;
        statefulInitialized = true;
      }
      return [statefulValue, applyState];
    },
    useEffect(fn) {
      if (options.captureEffects) captured.effects.push(fn);
    },
    useCallback(fn) { return fn; },
    useMemo(fn) { return fn(); },
    useRef(initial) {
      if (!options.stateful) return { current: initial };
      const slot = refCursor++;
      if (!refSlots[slot]) refSlots[slot] = { current: initial };
      return refSlots[slot];
    },
  };
  const window = {
    __HERMES_PLUGIN_SDK__: {
      React,
      hooks,
      components: { Button: 'button' },
      utils: {
        cn: (...parts) => parts.filter(Boolean).join(' '),
        isoTimeAgo: () => 'recently',
      },
      fetchJSON: options.fetchJSON || (() => Promise.resolve({})),
    },
    __HERMES_PLUGINS__: {
      register(name, component) {
        assert.equal(name, 'daily-ledger');
        captured.component = component;
      },
    },
  };

  vm.runInNewContext(instrumented, {
    window,
    console,
    Date,
    Intl,
    Promise,
    setTimeout: options.setTimeout || setTimeout,
    clearTimeout: options.clearTimeout || clearTimeout,
  }, { filename: file });

  if (options.stateful) {
    captured.getState = function () { return statefulValue; };
    captured.replaceState = applyState;
    captured.render = function () {
      refCursor = 0;
      if (options.captureEffects) captured.effects = [];
      return captured.component();
    };
    captured.runEffects = function () {
      return captured.effects.map(function (effect) { return effect(); });
    };
  }

  return { api: window.__DAILY_LEDGER_TEST__, captured, source };
}

function flattenText(node, out = []) {
  if (node == null || node === false) return out;
  if (typeof node === 'string' || typeof node === 'number') {
    out.push(String(node));
    return out;
  }
  if (Array.isArray(node)) {
    for (const item of node) flattenText(item, out);
    return out;
  }
  if (typeof node.type === 'function') {
    return flattenText(node.type(Object.assign({}, node.props, { children: node.children })), out);
  }
  if (node.children) flattenText(node.children, out);
  return out;
}

function findNodes(node, predicate, out = []) {
  if (node == null || node === false) return out;
  if (Array.isArray(node)) {
    for (const item of node) findNodes(item, predicate, out);
    return out;
  }
  if (typeof node !== 'object') return out;
  if (typeof node.type === 'function') {
    // Expand function types by calling them
    const expanded = node.type(Object.assign({}, node.props, { children: node.children }));
    findNodes(expanded, predicate, out);
    return out;
  }
  if (predicate(node)) out.push(node);
  if (node.children) findNodes(node.children, predicate, out);
  return out;
}

function findRawNodes(node, predicate, out = []) {
  if (node == null || node === false) return out;
  if (Array.isArray(node)) {
    for (const item of node) findRawNodes(item, predicate, out);
    return out;
  }
  if (typeof node !== 'object') return out;
  if (predicate(node)) out.push(node);
  if (node.children) findRawNodes(node.children, predicate, out);
  return out;
}

function sessionFixture() {
  return {
    profile: 'default',
    session_id: '20260727_010203_abc',
    title: 'Build the thing',
    source: 'cli',
    model: 'fixture-model',
    message_count: 12,
    tool_call_count: 4,
    summary_status: { exists: false, stale: false, job_status: null },
  };
}

test('plugin registers the Calendar page', () => {
  const { captured } = loadPlugin();
  assert.equal(typeof captured.component, 'function');
});

test('Calendar initial selection is the actual Chicago date', () => {
  const { api, captured } = loadPlugin();
  captured.component();
  const now = api.getChicagoNow();
  const expected = api.formatDate(now.year, now.month, now.day);
  assert.equal(captured.initialState.selectedDate, expected);
  assert.equal(captured.initialState.viewMonth, now.month);
  assert.equal(Object.keys(captured.initialState.sessionSummaries).length, 0);
  assert.equal(Object.keys(captured.initialState.activeJobs).length, 0);
});

test('December spillover cells roll into January of the next year', () => {
  const { api } = loadPlugin();
  const cells = api.buildCalendarGrid(2026, 11);
  const spillover = cells.filter((cell) => !cell.current && cell.year >= 2026 && cell.day <= 7);
  const january = spillover.filter((cell) => cell.year === 2027 && cell.month === 0);
  assert.ok(january.length > 0, 'expected January 2027 spillover cells');
  for (const cell of january) {
    assert.match(api.formatDate(cell.year, cell.month, cell.day), /^2027-01-/);
  }
});

test('frontend session job identity includes date, profile, and session id', () => {
  const { api } = loadPlugin();
  const session = sessionFixture();
  const first = api.sessionKey(session, '2026-07-26');
  const second = api.sessionKey(session, '2026-07-27');
  assert.notEqual(first, second);
  assert.match(first, /default/);
  assert.match(first, /20260727_010203_abc/);
});

test('session chat link is same-origin, profile-scoped, and URL encoded', () => {
  const { api } = loadPlugin();
  const href = api.sessionChatHref({
    profile: 'research/profile',
    session_id: 'session id?next=/settings',
  });
  assert.equal(
    href,
    '/chat?resume=session%20id%3Fnext%3D%2Fsettings&profile=research%2Fprofile',
  );
  assert.ok(href.startsWith('/chat?'));
  assert.doesNotMatch(href, /^https?:/);
});

test('session title renders as the sanitized exact-session link', () => {
  const { api } = loadPlugin();
  const session = Object.assign(sessionFixture(), {
    profile: 'named profile',
    session_id: 'session/id',
    title: 'Build\u0000 the thing',
  });
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session,
    summaryData: null,
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: null,
    error: '',
    onToggleSelect() {},
  });
  const links = findNodes(tree, (node) =>
    node.type === 'a' && node.props.className === 'dl-session-title');
  assert.equal(links.length, 1);
  assert.equal(links[0].props.href, '/chat?resume=session%2Fid&profile=named%20profile');
  assert.equal(flattenText(links[0]).join(''), 'Build the thing');
});

test('session card shows the sanitized session model in its detail row', () => {
  const { api } = loadPlugin();
  const session = Object.assign(sessionFixture(), { model: 'Qwen3-Coder\u0000' });
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session,
    summaryData: null,
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: null,
    error: '',
    onToggleSelect() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /Model: Qwen3-Coder/);
  assert.doesNotMatch(text, /\u0000/);
});

test('session card omits the model label when inventory has no model', () => {
  const { api } = loadPlugin();
  const session = Object.assign(sessionFixture(), { model: null });
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session,
    summaryData: null,
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: null,
    error: '',
    onToggleSelect() {},
  });
  assert.doesNotMatch(flattenText(tree).join(' '), /Model:/);
});

test('session card checkbox is controlled by selected prop', () => {
  const { api } = loadPlugin();
  const session = sessionFixture();
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session,
    summaryData: null,
    selected: true,
    selectionDisabled: false,
    batchMemberStatus: null,
    error: '',
    onToggleSelect() {},
  });
  const checkboxes = findNodes(tree, (node) => node.type === 'input' && node.props.type === 'checkbox');
  assert.equal(checkboxes.length, 1);
  assert.equal(checkboxes[0].props.checked, true);
  assert.equal(checkboxes[0].props.disabled, false);
  assert.match(checkboxes[0].props['aria-label'] || '', /Select Build the thing for batch summary/);
});

test('session card checkbox is disabled when selectionDisabled prop is true', () => {
  const { api } = loadPlugin();
  const cronSession = { job_id: 'cron1', job_name: 'My Cron Job' };
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session: cronSession,
    summaryData: null,
    selected: false,
    selectionDisabled: true,
    batchMemberStatus: null,
    error: '',
    onToggleSelect() {},
  });
  const checkboxes = findNodes(tree, (node) => node.type === 'input' && node.props.type === 'checkbox');
  assert.equal(checkboxes.length, 1);
  assert.equal(checkboxes[0].props.disabled, true);
});

test('session card renders batch member status label', () => {
  const { api } = loadPlugin();
  const session = sessionFixture();
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session,
    summaryData: null,
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: 'completed',
    error: '',
    onToggleSelect() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /Completed/);
  assert.doesNotMatch(text, /with model/);
  assert.doesNotMatch(text, /with worker/);
});

test('session card renders sanitized unknown batch member status', () => {
  const { api } = loadPlugin();
  const session = sessionFixture();
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session,
    summaryData: null,
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: 'unknown_status\u0000bad',
    error: '',
    onToggleSelect() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /unknown_statusbad/);
  assert.doesNotMatch(text, /with model/);
  assert.doesNotMatch(text, /with worker/);
});

test('session card renders queued batch member status', () => {
  const { api } = loadPlugin();
  const session = sessionFixture();
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session,
    summaryData: null,
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: 'queued',
    error: '',
    onToggleSelect() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /Queued/);
});

test('session card renders running batch member status', () => {
  const { api } = loadPlugin();
  const session = sessionFixture();
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session,
    summaryData: null,
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: 'running',
    error: '',
    onToggleSelect() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /Running/);
});

test('session card renders failed batch member status', () => {
  const { api } = loadPlugin();
  const session = sessionFixture();
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session,
    summaryData: null,
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: 'failed',
    error: '',
    onToggleSelect() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /Failed/);
});

test('session card renders partial batch member status', () => {
  const { api } = loadPlugin();
  const session = sessionFixture();
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session,
    summaryData: null,
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: 'partial',
    error: '',
    onToggleSelect() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /Completed \(partial\)/);
});

test('session card renders skipped_current batch member status', () => {
  const { api } = loadPlugin();
  const session = sessionFixture();
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session,
    summaryData: null,
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: 'skipped_current',
    error: '',
    onToggleSelect() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /Skipped/);
});

test('session card renders skipped_running batch member status', () => {
  const { api } = loadPlugin();
  const session = sessionFixture();
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session,
    summaryData: null,
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: 'skipped_running',
    error: '',
    onToggleSelect() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /Skipped/);
});

test('no per-card Generate or Regenerate button in session card', () => {
  const { api } = loadPlugin();
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session: sessionFixture(),
    summaryData: null,
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: null,
    error: '',
    onToggleSelect() {},
  });
  const text = flattenText(tree).join(' ');
  assert.doesNotMatch(text, /Generate summary/);
  assert.doesNotMatch(text, /Generate recap/);
  assert.doesNotMatch(text, /Regenerate/);
});

test('session card renders stored summary, stale warning, and immutable versions', () => {
  const { api } = loadPlugin();
  const version = '20260727T010203Z_000001_deadbeefcafe';
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session: sessionFixture(),
    summaryData: {
      exists: true,
      stale: true,
      data: { summary: 'One canonical session summary.' },
      meta: { generated_at: '2026-07-27T01:02:03Z', model: 'compression', version_id: version },
      versions: [{ version_id: version, generated_at: '2026-07-27T01:02:03Z' }],
    },
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: null,
    error: '',
    onToggleSelect() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /One canonical session summary/);
  assert.match(text, /Stale/);
  assert.match(text, new RegExp('Restore: ' + version));
  assert.doesNotMatch(text, /Regenerate/);
});

test('daily roll-up button omits worker name', () => {
  const { api } = loadPlugin();
  const tree = api.RollupSection({
    dateStr: '2026-07-27',
    rollupData: { exists: false },
    generating: false,
    currentSummaryCount: 1,
    activeSessionCount: 2,
    error: '',
    onGenerate() {},
    onRollback() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /Generate daily roll-up/);
  assert.doesNotMatch(text, /with model/);
  assert.doesNotMatch(text, /with worker/);
});

test('daily roll-up shows explicit partial coverage and only summary-derived output', () => {
  const { api } = loadPlugin();
  const tree = api.RollupSection({
    dateStr: '2026-07-27',
    rollupData: {
      exists: true,
      stale: false,
      data: {
        overall_recap: 'Summary-only daily narrative.',
        coverage: { included: 2, active: 5 },
      },
      meta: { generated_at: '2026-07-27T02:00:00Z', model: 'compression' },
      versions: [],
    },
    generating: false,
    currentSummaryCount: 2,
    activeSessionCount: 5,
    error: '',
    onGenerate() {},
    onRollback() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /Daily Roll-up/);
  assert.match(text, /covers 2 of 5 sessions/i);
  assert.match(text, /Summary-only daily narrative/);
});

test('artifact polling stops immediately on durable failed job status', async () => {
  let scheduledTick = null;
  let timerCalls = 0;
  const { api } = loadPlugin({
    fetchJSON: () => Promise.resolve({
      exists: false,
      job_status: { status: 'failed', error: 'summary worker failed cleanly' },
    }),
    setTimeout(fn) {
      timerCalls++;
      scheduledTick = fn;
      return timerCalls;
    },
    clearTimeout() {},
  });
  const errors = [];

  api.pollArtifact('/session-summary?date=2026-07-27', null, () => {
    assert.fail('failed generation must not call onResult');
  }, (message) => errors.push(message), 15, 1);

  scheduledTick();
  await Promise.resolve();
  await Promise.resolve();

  assert.deepEqual(errors, ['summary worker failed cleanly']);
  assert.equal(timerCalls, 1);
});

test('artifact polling waits for a new immutable version during regeneration', async () => {
  let scheduledTick = null;
  let timerCalls = 0;
  const responses = [
    { exists: true, meta: { version_id: 'old' }, job_status: { status: 'running' } },
    { exists: true, meta: { version_id: 'new' }, job_status: { status: 'completed' } },
  ];
  const results = [];
  const { api } = loadPlugin({
    fetchJSON: () => Promise.resolve(responses.shift()),
    setTimeout(fn) {
      timerCalls++;
      scheduledTick = fn;
      return timerCalls;
    },
    clearTimeout() {},
  });

  api.pollArtifact('/session-summary?date=2026-07-27', 'old', (data) => {
    results.push(data.meta.version_id);
  }, () => assert.fail('must not fail'), 15, 1);

  scheduledTick();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(results.length, 0);
  scheduledTick();
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(results, ['new']);
  assert.equal(timerCalls, 2);
});

test('active bundle never calls the retired raw whole-day recap API', () => {
  const { source } = loadPlugin();
  assert.doesNotMatch(source, /api(?:Get|Post)\('\/recap/);
  assert.doesNotMatch(source, /Generate recap/);
  assert.doesNotMatch(source, /with model/);
  assert.doesNotMatch(source, /with worker/);
});

test('polling window accommodates long local inference', () => {
  const { api } = loadPlugin();
  assert.equal(api.JOB_POLL_MAX_ATTEMPTS, 180);
});

// ---------------------------------------------------------------------
// Batch helpers tests
// ---------------------------------------------------------------------

test('batchSelectionKey builds exact composite key with encoded date/profile/session_id', () => {
  const { api } = loadPlugin();
  const dateStr = '2026-07-27';
  const session = {
    profile: 'default',
    session_id: 'abc123',
  };
  const key = api.batchSelectionKey(dateStr, session);
  assert.equal(key, '2026-07-27/default/abc123');
});

test('batchSelectionKey encodes special characters correctly', () => {
  const { api } = loadPlugin();
  const dateStr = '2026-07-27';
  const session = {
    profile: 'research/profile',
    session_id: 'session id?next=/settings',
  };
  const key = api.batchSelectionKey(dateStr, session);
  assert.equal(key, '2026-07-27/research%2Fprofile/session%20id%3Fnext%3D%2Fsettings');
});

test('buildBatchRequest filters sessions by selectedKeys', () => {
  const { api } = loadPlugin();
  const dateStr = '2026-07-27';
  const sessions = [
    { profile: 'p1', session_id: 's1' },
    { profile: 'p2', session_id: 's2' },
    { profile: 'p3', session_id: 's3' },
  ];
  const selectedKeys = {
    '2026-07-27/p1/s1': true,
    '2026-07-27/p3/s3': true,
  };
  const req = api.buildBatchRequest(dateStr, sessions, selectedKeys, false);
  assert.deepEqual(req.sessions, [
    { profile: 'p1', session_id: 's1' },
    { profile: 'p3', session_id: 's3' },
  ]);
  assert.equal(req.regenerate_current, false);
});

test('buildBatchRequest includes selectedKeys only and respects visible order', () => {
  const { api } = loadPlugin();
  const dateStr = '2026-07-27';
  const sessions = [
    { profile: 'a', session_id: '1' },
    { profile: 'b', session_id: '2' },
    { profile: 'c', session_id: '3' },
  ];
  const selectedKeys = {
    '2026-07-27/c/3': true,
    '2026-07-27/a/1': true,
  };
  const req = api.buildBatchRequest(dateStr, sessions, selectedKeys, true);
  assert.equal(req.regenerate_current, true);
  assert.ok(req.sessions.length === 2);
  assert.equal(req.sessions[0].profile, 'a');
  assert.equal(req.sessions[1].profile, 'c');
});

test('buildBatchRequest ignores keys not in visible sessions', () => {
  const { api } = loadPlugin();
  const dateStr = '2026-07-27';
  const sessions = [
    { profile: 'p1', session_id: 's1' },
  ];
  const selectedKeys = {
    '2026-07-27/p1/s1': true,
    '2026-07-27/pX/sX': true,
    '2026-07-26/p1/s1': true,
  };
  const req = api.buildBatchRequest(dateStr, sessions, selectedKeys, false);
  assert.equal(req.sessions.length, 1);
  assert.equal(req.sessions[0].profile, 'p1');
});

test('summarizeBatchProgress counts all fields correctly', () => {
  const { api } = loadPlugin();
  const batch = {
    members: [
      { session_id: 's1', profile: 'p1', status: 'completed' },
      { session_id: 's2', profile: 'p2', status: 'partial' },
      { session_id: 's3', profile: 'p3', status: 'failed' },
      { session_id: 's4', profile: 'p4', status: 'skipped_current' },
      { session_id: 's5', profile: 'p5', status: 'skipped_running' },
      { session_id: 's6', profile: 'p6', status: 'running' },
      { session_id: 's7', profile: 'p7', status: 'pending' },
    ],
  };
  const summary = api.summarizeBatchProgress(batch);
  assert.equal(summary.total, 7);
  assert.equal(summary.finished, 5); // completed + partial + failed + both skips
  assert.equal(summary.completed, 2); // completed + partial counts
  assert.equal(summary.failed, 1);
  assert.equal(summary.skipped, 2);
  assert.equal(summary.active, 2);
});

test('summarizeBatchProgress skips malformed members', () => {
  const { api } = loadPlugin();
  const batch = {
    members: [
      { session_id: 's1', profile: 'p1', status: 'completed' },
      { session_id: 's2' }, // missing profile
      { profile: 'p3', status: 'running' }, // missing session_id
      null,
      { session_id: 's4', profile: 'p4', status: 'pending' },
    ],
  };
  const summary = api.summarizeBatchProgress(batch);
  assert.equal(summary.total, 2); // only s1 and s4
  assert.equal(summary.active, 1);
});

test('summarizeBatchProgress handles null/empty batch', () => {
  const { api } = loadPlugin();
  assert.deepEqual(api.summarizeBatchProgress(null), { total: 0, finished: 0, completed: 0, failed: 0, skipped: 0, active: 0 });
  assert.deepEqual(api.summarizeBatchProgress({}), { total: 0, finished: 0, completed: 0, failed: 0, skipped: 0, active: 0 });
  assert.deepEqual(api.summarizeBatchProgress({ members: null }), { total: 0, finished: 0, completed: 0, failed: 0, skipped: 0, active: 0 });
  assert.deepEqual(api.summarizeBatchProgress({ members: [] }), { total: 0, finished: 0, completed: 0, failed: 0, skipped: 0, active: 0 });
});

test('batchMemberStatusMap builds status map by exact composite identity', () => {
  const { api } = loadPlugin();
  const dateStr = '2026-07-27';
  const batch = {
    members: [
      { session_id: 's1', profile: 'p1', status: 'running' },
      { session_id: 's2', profile: 'p2', status: 'completed' },
    ],
  };
  const map = api.batchMemberStatusMap(dateStr, batch);
  assert.ok(map.hasOwnProperty('2026-07-27/p1/s1'));
  assert.ok(map.hasOwnProperty('2026-07-27/p2/s2'));
  assert.equal(map['2026-07-27/p1/s1'], 'running');
  assert.equal(map['2026-07-27/p2/s2'], 'completed');
});

test('batchMemberStatusMap ignores malformed members safely', () => {
  const { api } = loadPlugin();
  const dateStr = '2026-07-27';
  const batch = {
    members: [
      { session_id: 's1', profile: 'p1', status: 'running' },
      { session_id: 's2' },
      null,
    ],
  };
  const map = api.batchMemberStatusMap(dateStr, batch);
  assert.equal(Object.keys(map).length, 1);
  assert.ok(map.hasOwnProperty('2026-07-27/p1/s1'));
});

test('newestBatchForDate returns first valid batch for exact date', () => {
  const { api } = loadPlugin();
  const response = {
    batches: [
      { date: '2026-07-28T00:01:00Z', id: 'b2' },
      { date: '2026-07-27T23:59:59Z', id: 'b1' },
      { date: '2026-07-27T12:00:00Z', id: 'b0' },
      { date: '2026-07-26T00:00:00Z', id: 'b3' },
    ],
  };
  const result = api.newestBatchForDate(response, '2026-07-27');
  assert.equal(result && result.id, 'b1'); // newest first
});

test('newestBatchForDate returns null if no batch for date', () => {
  const { api } = loadPlugin();
  const response = {
    batches: [
      { date: '2026-07-28T00:01:00Z', id: 'b2' },
      { date: '2026-07-26T00:00:00Z', id: 'b3' },
    ],
  };
  const result = api.newestBatchForDate(response, '2026-07-27');
  assert.equal(result, null);
});

test('newestBatchForDate handles malformed batches', () => {
  const { api } = loadPlugin();
  const response = {
    batches: [
      { date: '2026-07-27T00:00:00Z', id: 'b0' },
      { id: 'b1' },
      { date: '2026-07-27T00:00:00Z' },
      null,
      { date: '2026-07-26T00:00:00Z', id: 'b2' },
    ],
  };
  const result = api.newestBatchForDate(response, '2026-07-27');
  assert.equal(result && result.id, 'b0');
});

test('newestBatchForDate returns null for null/empty response', () => {
  const { api } = loadPlugin();
  assert.equal(api.newestBatchForDate(null, '2026-07-27'), null);
  assert.equal(api.newestBatchForDate({ batches: null }, '2026-07-27'), null);
  assert.equal(api.newestBatchForDate({ batches: [] }, '2026-07-27'), null);
});

test('pollBatchStatus cancels and clears timer on return', async () => {
  let timerCalls = 0;
  let timerId = null;
  const { api } = loadPlugin({
    fetchJSON: () => Promise.resolve({ job_status: { status: 'running' } }),
    setTimeout(fn, ms) {
      timerCalls++;
      timerId = { id: timerCalls, fn: fn, ms: ms };
      return timerCalls;
    },
    clearTimeout(id) {
      if (timerId && timerId.id === id) {
        timerId = null;
      }
      return undefined;
    },
  });

  const onCancel = api.pollBatchStatus('/batch/1', () => {}, () => {}, () => {}, 2, 100);
  assert.ok(timerId !== null);
  onCancel();
  assert.ok(timerId === null);
  assert.equal(timerCalls, 1);
});

test('pollBatchStatus stops on terminal status and calls onTerminal', async () => {
  let scheduledTick = null;
  let timerCalls = 0;
  const { api } = loadPlugin({
    fetchJSON: () => Promise.resolve({ status: 'completed', members: [] }),
    setTimeout(fn, ms) {
      timerCalls++;
      scheduledTick = fn;
      return timerCalls;
    },
    clearTimeout() {},
  });

  const results = [];
  const onCancel = api.pollBatchStatus('/batch/1', () => {}, (data) => results.push(data), () => {}, 5, 100);
  scheduledTick();
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(results.length, 1);
  assert.equal(results[0].status, 'completed');
  assert.equal(timerCalls, 1);
  onCancel();
});

test('pollBatchStatus retries fetch errors until max then calls onError', async () => {
  let scheduledTick = null;
  let timerCalls = 0;
  const { api } = loadPlugin({
    fetchJSON: () => Promise.reject(new Error('network fail')),
    setTimeout(fn, ms) {
      timerCalls++;
      scheduledTick = fn;
      return timerCalls;
    },
    clearTimeout() {},
  });

  const errors = [];
  const onCancel = api.pollBatchStatus('/batch/1', () => {}, () => {}, (msg) => errors.push(msg), 3, 100);
  for (let i = 0; i < 3; i++) {
    scheduledTick();
    await Promise.resolve();
    await Promise.resolve();
  }

  assert.equal(errors.length, 1);
  assert.match(errors[0], /Batch polling failed/);
  assert.equal(timerCalls, 3);
  onCancel();
});

test('pollBatchStatus respects cancellation mid-flight', async () => {
  let tickCount = 0;
  let scheduledTick = null;
  const { api } = loadPlugin({
    fetchJSON: () => {
      tickCount++;
      return Promise.resolve({ job_status: { status: 'running' } });
    },
    setTimeout(fn, ms) {
      scheduledTick = fn;
      return tickCount;
    },
    clearTimeout() {},
  });

  const onCancel = api.pollBatchStatus('/batch/1', () => {}, () => {}, () => {}, 5, 100);
  scheduledTick();
  onCancel();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(tickCount, 1); // only the first tick ran
  onCancel(); // safe to call again
});

test('pollBatchStatus doubles delay capped at 15000ms', async () => {
  let delays = [];
  let scheduledTick = null;
  const { api } = loadPlugin({
    fetchJSON: () => Promise.resolve({ job_status: { status: 'running' } }),
    setTimeout(fn, ms) {
      delays.push(ms);
      scheduledTick = fn;
      return delays.length;
    },
    clearTimeout() {},
  });

  api.pollBatchStatus('/batch/1', () => {}, () => {}, () => {}, 10, 100);
  for (let i = 0; i < 10; i++) {
    scheduledTick();
    await Promise.resolve();
    await Promise.resolve();
  }

  // 100 -> 200 -> 400 -> 800 -> 1600 -> 3200 -> 6400 -> 12800 -> 15000 -> 15000
  assert.deepEqual(delays, [100, 200, 400, 800, 1600, 3200, 6400, 12800, 15000, 15000]);
});

// ---------------------------------------------------------------------
// BatchToolbar tests
// ---------------------------------------------------------------------

test('BatchToolbar renders exact labels and count', () => {
  const { api } = loadPlugin();
  const tree = api.BatchToolbar({
    dateStr: '2026-07-27',
    sessions: [
      { profile: 'p1', session_id: 's1' },
      { profile: 'p2', session_id: 's2' },
    ],
    selectedKeys: { '2026-07-27/p1/s1': true },
    regenerateCurrent: false,
    batchStatus: null,
    batchError: '',
    onSelectAll() {},
    onClear() {},
    onToggleRegenerate() {},
    onSubmit() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /1 selected/);
  assert.match(text, /Select all/);
  assert.match(text, /Clear selection/);
  assert.match(text, /Regenerate already current/);
  assert.match(text, /Summarize selected \(\d\)/);
  assert.doesNotMatch(text, /with model/);
  assert.doesNotMatch(text, /with worker/);
});

test('BatchToolbar disables controls while batch queued/running', () => {
  const { api } = loadPlugin();
  const treeQueued = api.BatchToolbar({
    dateStr: '2026-07-27',
    sessions: [{ profile: 'p1', session_id: 's1' }],
    selectedKeys: {},
    regenerateCurrent: false,
    batchStatus: { status: 'queued', total: 0, members: [] },
    batchError: '',
    onSelectAll() {},
    onClear() {},
    onToggleRegenerate() {},
    onSubmit() {},
  });
  const selectAll = findNodes(treeQueued, (n) => n.type === 'button' && n.children && n.children[0] === 'Select all')[0];
  assert.equal(selectAll.props.disabled, true);

  const treeRunning = api.BatchToolbar({
    dateStr: '2026-07-27',
    sessions: [{ profile: 'p1', session_id: 's1' }],
    selectedKeys: {},
    regenerateCurrent: false,
    batchStatus: { status: 'running', total: 0, members: [] },
    batchError: '',
    onSelectAll() {},
    onClear() {},
    onToggleRegenerate() {},
    onSubmit() {},
  });
  const selectAllRunning = findNodes(treeRunning, (n) => n.type === 'button' && n.children && n.children[0] === 'Select all')[0];
  assert.equal(selectAllRunning.props.disabled, true);
});

test('BatchToolbar progress shows finished counts', () => {
  const { api } = loadPlugin();
  const tree = api.BatchToolbar({
    dateStr: '2026-07-27',
    sessions: [
      { profile: 'p1', session_id: 's1' },
      { profile: 'p2', session_id: 's2' },
    ],
    selectedKeys: {},
    regenerateCurrent: false,
    batchStatus: {
      status: 'running',
      total: 2,
      members: [
        { profile: 'p1', session_id: 's1', status: 'failed' },
        { profile: 'p2', session_id: 's2', status: 'running' },
      ],
    },
    batchError: '',
    onSelectAll() {},
    onClear() {},
    onToggleRegenerate() {},
    onSubmit() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /1\/2 finished/);
  assert.doesNotMatch(text, /with model/);
  assert.doesNotMatch(text, /with worker/);
});

test('BatchToolbar progress counts both skip kinds as skipped', () => {
  const { api } = loadPlugin();
  const tree = api.BatchToolbar({
    dateStr: '2026-07-27',
    sessions: [
      { profile: 'p1', session_id: 's1' },
      { profile: 'p2', session_id: 's2' },
      { profile: 'p3', session_id: 's3' },
    ],
    selectedKeys: {},
    regenerateCurrent: false,
    batchStatus: {
      status: 'running',
      total: 3,
      members: [
        { profile: 'p1', session_id: 's1', status: 'completed' },
        { profile: 'p2', session_id: 's2', status: 'skipped_current' },
        { profile: 'p3', session_id: 's3', status: 'skipped_running' },
      ],
    },
    batchError: '',
    onSelectAll() {},
    onClear() {},
    onToggleRegenerate() {},
    onSubmit() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /3\/3 finished/);
  assert.match(text, /2 skipped/);
});

test('BatchToolbar sanitizes batch error in role=alert', () => {
  const { api } = loadPlugin();
  const tree = api.BatchToolbar({
    dateStr: '2026-07-27',
    sessions: [{ profile: 'p1', session_id: 's1' }],
    selectedKeys: {},
    regenerateCurrent: false,
    batchStatus: { status: 'running', total: 0, members: [] },
    batchError: 'batch failed\u0000bad',
    onSelectAll() {},
    onClear() {},
    onToggleRegenerate() {},
    onSubmit() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /batch failedbad/);
  assert.doesNotMatch(text, /with model/);
  assert.doesNotMatch(text, /with worker/);
});

test('BatchToolbar primary button disabled when N=0', () => {
  const { api } = loadPlugin();
  const tree = api.BatchToolbar({
    dateStr: '2026-07-27',
    sessions: [{ profile: 'p1', session_id: 's1' }],
    selectedKeys: {},
    regenerateCurrent: false,
    batchStatus: null,
    batchError: '',
    onSelectAll() {},
    onClear() {},
    onToggleRegenerate() {},
    onSubmit() {},
  });
  const primary = findNodes(tree, (n) => n.type === 'button' && n.props['aria-label'] && n.props['aria-label'].includes('Summarize selected'))[0];
  assert.equal(primary.props.disabled, true);
});

// ---------------------------------------------------------------------
// DayDetailPanel controlled wiring tests
// ---------------------------------------------------------------------

test('DayDetailPanel renders BatchToolbar when dayData exists', () => {
  const { api } = loadPlugin();
  const tree = api.DayDetailPanel({
    dateStr: '2026-07-27',
    dayData: { sessions: [], cron_runs: [] },
    sessionSummaries: {},
    rollupData: null,
    activeJobs: {},
    jobErrors: {},
    loadingDay: false,
    error: '',
    onGenerateSession() {},
    onRollbackSession() {},
    onGenerateRollup() {},
    onRollbackRollup() {},
    selectedKeys: {},
    onSelectAll() {},
    onClear() {},
    batchStatus: null,
    batchError: '',
    regenerateCurrent: false,
    onSubmitBatch() {},
    onSelectSession() {},
    onDeselectSession() {},
  });
  const toolbar = findNodes(tree, (n) => n.type === 'div' && n.props.className && n.props.className.includes('dl-batch-toolbar'));
  assert.equal(toolbar.length, 1);
});

test('DayDetailPanel maps exact composite selection and member status to SessionCard', () => {
  const { api } = loadPlugin();
  const sessions = [
    { profile: 'p1', session_id: 's1', title: 'Session 1' },
    { profile: 'p2', session_id: 's2', title: 'Session 2' },
  ];
  const tree = api.DayDetailPanel({
    dateStr: '2026-07-27',
    dayData: { sessions, cron_runs: [] },
    sessionSummaries: {},
    rollupData: null,
    activeJobs: {},
    jobErrors: {},
    loadingDay: false,
    error: '',
    onGenerateSession() {},
    onRollbackSession() {},
    onGenerateRollup() {},
    onRollbackRollup() {},
    selectedKeys: { '2026-07-27/p1/s1': true },
    onSelectAll() {},
    onClear() {},
    batchStatus: {
      members: [
        { session_id: 's1', profile: 'p1', status: 'completed' },
        { session_id: 's2', profile: 'p2', status: 'failed' },
      ],
    },
    batchError: '',
    regenerateCurrent: false,
    onSubmitBatch() {},
    onSelectSession() {},
    onDeselectSession() {},
  });
  // Find session-selection checkboxes (aria-label begins with 'Select ... for batch summary')
  const checkboxes = findNodes(tree, (n) => n.type === 'input' && n.props.type === 'checkbox' && n.props['aria-label'] && n.props['aria-label'].startsWith('Select'));
  assert.equal(checkboxes.length, sessions.length);
  // Check first checkbox: selected=true and member status 'completed'
  assert.equal(checkboxes[0].props.checked, true);
  assert.match(checkboxes[0].props['aria-label'] || '', /Session 1/);
  // Find the member status text in the SessionCard's children
  const cards = findNodes(tree, (n) => n.type === 'div' && n.props.className && n.props.className.includes('dl-session-card'));
  const firstCardText = flattenText(cards[0]).join(' ');
  assert.match(firstCardText, /Completed/);
  // Check second checkbox: selected=false and member status 'failed'
  assert.equal(checkboxes[1].props.checked, false);
  assert.match(checkboxes[1].props['aria-label'] || '', /Session 2/);
  const secondCardText = flattenText(cards[1]).join(' ');
  assert.match(secondCardText, /Failed/);
});

test('DayDetailPanel cron cards receive no checkbox (selectionDisabled=true)', () => {
  const { api } = loadPlugin();
  const cronRuns = [{ job_id: 'cron1', job_name: 'My Cron' }];
  const tree = api.DayDetailPanel({
    dateStr: '2026-07-27',
    dayData: { sessions: [], cron_runs: cronRuns },
    sessionSummaries: {},
    rollupData: null,
    activeJobs: {},
    jobErrors: {},
    loadingDay: false,
    error: '',
    onGenerateSession() {},
    onRollbackSession() {},
    onGenerateRollup() {},
    onRollbackRollup() {},
    selectedKeys: {},
    onSelectAll() {},
    onClear() {},
    batchStatus: null,
    batchError: '',
    regenerateCurrent: false,
    onSubmitBatch() {},
    onSelectSession() {},
    onDeselectSession() {},
  });
  // Cron cards have no session-selection checkbox (they render via CronCard, not SessionCard)
  // Find all session-selection checkboxes (aria-label begins with 'Select ... for batch summary')
  const checkboxes = findNodes(tree, (n) => n.type === 'input' && n.props.type === 'checkbox' && n.props['aria-label'] && n.props['aria-label'].startsWith('Select'));
  // There should be zero session-selection checkboxes
  assert.equal(checkboxes.length, 0);
  // Verify the cron card renders via CronCard
  const cronCards = findNodes(tree, (n) => n.type === 'div' && n.props.className && n.props.className.includes('dl-cron-card'));
  assert.equal(cronCards.length, 1);
});

test('DayDetailPanel passes onToggleSelect callback that toggles select/deselect', () => {
  const { api } = loadPlugin();
  const sessions = [{ profile: 'p1', session_id: 's1' }];
  let toggleCalled = false;
  const onSelectSession = () => { toggleCalled = true; };
  const tree = api.DayDetailPanel({
    dateStr: '2026-07-27',
    dayData: { sessions, cron_runs: [] },
    sessionSummaries: {},
    rollupData: null,
    activeJobs: {},
    jobErrors: {},
    loadingDay: false,
    error: '',
    onGenerateSession() {},
    onRollbackSession() {},
    onGenerateRollup() {},
    onRollbackRollup() {},
    selectedKeys: {},
    onSelectAll() {},
    onClear() {},
    batchStatus: null,
    batchError: '',
    regenerateCurrent: false,
    onSubmitBatch() {},
    onSelectSession,
    onDeselectSession() {},
  });
  // Find the session-selection checkbox and invoke its onChange
  const checkboxes = findNodes(tree, (n) => n.type === 'input' && n.props.type === 'checkbox' && n.props['aria-label'] && n.props['aria-label'].startsWith('Select'));
  assert.equal(checkboxes.length, 1);
  // Call the onChange handler (simulating a click)
  checkboxes[0].props.onChange({ target: { checked: true } });
  assert.ok(toggleCalled);
});

test('DayDetailPanel renders BatchToolbar before session cards', () => {
  const { api } = loadPlugin();
  const sessions = [{ profile: 'p1', session_id: 's1', title: 'Session 1' }];
  const tree = api.DayDetailPanel({
    dateStr: '2026-07-27',
    dayData: { sessions, cron_runs: [] },
    sessionSummaries: {},
    rollupData: null,
    activeJobs: {},
    jobErrors: {},
    loadingDay: false,
    error: '',
    onGenerateSession() {},
    onRollbackSession() {},
    onGenerateRollup() {},
    onRollbackRollup() {},
    selectedKeys: {},
    onSelectAll() {},
    onClear() {},
    batchStatus: null,
    batchError: '',
    regenerateCurrent: false,
    onSubmitBatch() {},
    onSelectSession() {},
    onDeselectSession() {},
  });
  // Flatten to text order
  const flat = flattenText(tree);
  // Toolbar should render before session cards - look for "1 selected" (N selected pattern) and "Session 1"
  const selectedIdx = flat.findIndex((t) => typeof t === 'string' && t.includes('selected'));
  const sessionIdx = flat.findIndex((t) => typeof t === 'string' && t.includes('Session 1'));
  // Toolbar should appear before session title
  assert.ok(selectedIdx >= 0 && sessionIdx >= 0);
  assert.ok(selectedIdx < sessionIdx, 'BatchToolbar should render before session cards');
});

function setupStatefulCalendar(options = {}) {
  const loaded = loadPlugin(Object.assign({ stateful: true }, options));
  loaded.captured.render();
  return loaded;
}

function rawCalendarChild(api, captured, type) {
  const tree = captured.render();
  const nodes = findRawNodes(tree, (node) => node.type === type);
  assert.equal(nodes.length, 1);
  return nodes[0];
}

function setBatchUiState(captured, overrides = {}) {
  captured.replaceState(function (state) {
    return Object.assign({}, state, {
      loadingDay: false,
      loadingMonth: false,
      dayData: { sessions: [], cron_runs: [] },
    }, overrides);
  });
}

test('Calendar state initializes exact batch UI fields', () => {
  const { captured } = setupStatefulCalendar();
  const state = captured.getState();
  assert.deepEqual(Object.keys(state.selectedSessions), []);
  assert.equal(state.regenerateCurrent, false);
  assert.equal(state.batchStatus, null);
  assert.equal(state.batchError, '');
});

test('Calendar callbacks select and deselect exact composite session identities', () => {
  const { api, captured } = setupStatefulCalendar();
  const first = { profile: 'default', session_id: 'one', title: 'Same title' };
  const second = { profile: 'other', session_id: 'two', title: 'Same title' };
  setBatchUiState(captured, { dayData: { sessions: [first, second], cron_runs: [] } });

  let panel = rawCalendarChild(api, captured, api.DayDetailPanel);
  panel.props.onSelectSession(first);
  panel = rawCalendarChild(api, captured, api.DayDetailPanel);
  panel.props.onSelectSession(second);

  const firstKey = api.batchSelectionKey(captured.getState().selectedDate, first);
  const secondKey = api.batchSelectionKey(captured.getState().selectedDate, second);
  assert.deepEqual(Object.keys(captured.getState().selectedSessions).sort(), [firstKey, secondKey].sort());

  panel = rawCalendarChild(api, captured, api.DayDetailPanel);
  panel.props.onDeselectSession(first);
  assert.deepEqual(Object.keys(captured.getState().selectedSessions), [secondKey]);
});

test('Calendar select all uses sessions only and clear empties selection', () => {
  const { api, captured } = setupStatefulCalendar();
  const sessions = [
    { profile: 'default', session_id: 'one' },
    { profile: 'other', session_id: 'two' },
  ];
  const cron = { profile: 'default', job_id: 'cron-only', job_name: 'Cron' };
  setBatchUiState(captured, { dayData: { sessions, cron_runs: [cron] } });

  let panel = rawCalendarChild(api, captured, api.DayDetailPanel);
  panel.props.onSelectAll();
  const keys = Object.keys(captured.getState().selectedSessions);
  assert.equal(keys.length, 2);
  assert.deepEqual(keys.sort(), sessions.map((session) =>
    api.batchSelectionKey(captured.getState().selectedDate, session)).sort());
  assert.doesNotMatch(keys.join(' '), /cron-only/);

  panel = rawCalendarChild(api, captured, api.DayDetailPanel);
  panel.props.onClear();
  assert.deepEqual(Object.keys(captured.getState().selectedSessions), []);
});

test('Calendar regenerate callback updates real state', () => {
  const { api, captured } = setupStatefulCalendar();
  setBatchUiState(captured);
  let panel = rawCalendarChild(api, captured, api.DayDetailPanel);
  panel.props.onToggleRegenerate(true);
  assert.equal(captured.getState().regenerateCurrent, true);
  panel = rawCalendarChild(api, captured, api.DayDetailPanel);
  panel.props.onToggleRegenerate(false);
  assert.equal(captured.getState().regenerateCurrent, false);
});

test('queued and running batches lock every selection mutation', () => {
  for (const status of ['queued', 'running']) {
    const { api, captured } = setupStatefulCalendar();
    const existing = { profile: 'default', session_id: 'existing' };
    const added = { profile: 'default', session_id: 'added' };
    const date = captured.getState().selectedDate;
    const existingKey = api.batchSelectionKey(date, existing);
    setBatchUiState(captured, {
      dayData: { sessions: [existing, added], cron_runs: [] },
      selectedSessions: { [existingKey]: true },
      regenerateCurrent: false,
      batchStatus: { status },
      batchError: 'held',
    });
    const before = JSON.stringify(captured.getState());
    const panel = rawCalendarChild(api, captured, api.DayDetailPanel);
    panel.props.onSelectSession(added);
    panel.props.onDeselectSession(existing);
    panel.props.onSelectAll();
    panel.props.onClear();
    panel.props.onToggleRegenerate(true);
    assert.equal(JSON.stringify(captured.getState()), before);
  }
});

test('same date preserves batch UI state and a different date clears it', () => {
  const { api, captured } = setupStatefulCalendar();
  const selectedDate = captured.getState().selectedDate;
  setBatchUiState(captured, {
    selectedDate,
    selectedSessions: { keep: true },
    regenerateCurrent: true,
    batchStatus: { status: 'completed' },
    batchError: 'visible',
  });

  let grid = rawCalendarChild(api, captured, api.MonthGrid);
  grid.props.onDayClick(selectedDate);
  assert.deepEqual(Object.keys(captured.getState().selectedSessions), ['keep']);
  assert.equal(captured.getState().regenerateCurrent, true);
  assert.equal(captured.getState().batchError, 'visible');

  grid = rawCalendarChild(api, captured, api.MonthGrid);
  grid.props.onDayClick('2026-01-02');
  const changed = captured.getState();
  assert.equal(changed.selectedDate, '2026-01-02');
  assert.deepEqual(Object.keys(changed.selectedSessions), []);
  assert.equal(changed.regenerateCurrent, false);
  assert.equal(changed.batchStatus, null);
  assert.equal(changed.batchError, '');
});

test('previous and next month navigation clear batch UI state', () => {
  for (const label of ['Previous month', 'Next month']) {
    const { captured } = setupStatefulCalendar();
    setBatchUiState(captured, {
      selectedSessions: { selected: true },
      regenerateCurrent: true,
      batchStatus: { status: 'completed' },
      batchError: 'visible',
    });
    const tree = captured.render();
    const button = findRawNodes(tree, (node) =>
      node.type === 'button' && node.props['aria-label'] === label)[0];
    assert.ok(button);
    button.props.onClick();
    const state = captured.getState();
    assert.equal(state.selectedDate, null);
    assert.deepEqual(Object.keys(state.selectedSessions), []);
    assert.equal(state.regenerateCurrent, false);
    assert.equal(state.batchStatus, null);
    assert.equal(state.batchError, '');
  }
});

test('Today preserves same-date batch UI state and clears it when changing dates', () => {
  const { captured } = setupStatefulCalendar();
  const today = captured.getState().selectedDate;
  const retained = {
    selectedSessions: { selected: true },
    regenerateCurrent: true,
    batchStatus: { status: 'completed' },
    batchError: 'visible',
  };
  setBatchUiState(captured, Object.assign({ selectedDate: today }, retained));
  let tree = captured.render();
  let button = findRawNodes(tree, (node) =>
    node.type === 'button' && node.props['aria-label'] === 'Go to current month')[0];
  button.props.onClick();
  assert.deepEqual(Object.keys(captured.getState().selectedSessions), ['selected']);
  assert.equal(captured.getState().regenerateCurrent, true);
  assert.equal(captured.getState().batchError, 'visible');

  setBatchUiState(captured, Object.assign({ selectedDate: '2026-01-02' }, retained));
  tree = captured.render();
  button = findRawNodes(tree, (node) =>
    node.type === 'button' && node.props['aria-label'] === 'Go to current month')[0];
  button.props.onClick();
  const changed = captured.getState();
  assert.equal(changed.selectedDate, today);
  assert.deepEqual(Object.keys(changed.selectedSessions), []);
  assert.equal(changed.regenerateCurrent, false);
  assert.equal(changed.batchStatus, null);
  assert.equal(changed.batchError, '');
});

async function flushPromises(rounds = 8) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

test('Calendar submits one exact batch, polls top-level status, and refreshes artifacts', async () => {
  const calls = [];
  const timers = [];
  const batch = {
    status: 'queued', batch_id: 'batch-one', date: '2026-07-27', total: 2,
    members: [
      { profile: 'default', session_id: 'one', status: 'queued' },
      { profile: 'other', session_id: 'two', status: 'queued' },
    ],
  };
  const loaded = setupStatefulCalendar({
    fetchJSON(url, options) {
      calls.push({ url, options });
      if (options && options.method === 'POST') return Promise.resolve(batch);
      if (url.includes('/session-summary/batch?')) {
        return Promise.resolve(Object.assign({}, batch, {
          status: 'completed',
          members: batch.members.map((member) => Object.assign({}, member, { status: 'completed' })),
        }));
      }
      if (url.includes('/day?')) return Promise.resolve({ sessions: [], cron_runs: [] });
      if (url.includes('/session-summary/rollup?')) return Promise.resolve({ exists: false });
      if (url.includes('/month?')) return Promise.resolve({ days: [] });
      throw new Error('Unexpected request ' + url);
    },
    setTimeout(fn) { timers.push(fn); return timers.length; },
    clearTimeout() {},
  });
  const { api, captured } = loaded;
  const sessions = [
    { profile: 'default', session_id: 'one' },
    { profile: 'other', session_id: 'two' },
  ];
  const date = '2026-07-27';
  const selectedSessions = {};
  for (const session of sessions) selectedSessions[api.batchSelectionKey(date, session)] = true;
  setBatchUiState(captured, {
    selectedDate: date,
    dayData: { sessions, cron_runs: [] },
    selectedSessions,
    regenerateCurrent: true,
  });

  const panel = rawCalendarChild(api, captured, api.DayDetailPanel);
  panel.props.onSubmitBatch();
  panel.props.onSubmitBatch();
  assert.equal(calls.filter((call) => call.options && call.options.method === 'POST').length, 1);
  assert.equal(captured.getState().batchStatus.status, 'queued');

  const post = calls.find((call) => call.options && call.options.method === 'POST');
  assert.match(post.url, /\/session-summary\/batch\?date=2026-07-27$/);
  assert.deepEqual(JSON.parse(post.options.body), {
    sessions: [
      { profile: 'default', session_id: 'one' },
      { profile: 'other', session_id: 'two' },
    ],
    regenerate_current: true,
  });

  await flushPromises();
  assert.equal(captured.getState().batchStatus.batch_id, 'batch-one');
  assert.equal(timers.length, 1);
  timers.shift()();
  await flushPromises();

  assert.equal(captured.getState().batchStatus.status, 'completed');
  assert.ok(calls.some((call) => call.url.includes('/session-summary/batch?date=2026-07-27&batch_id=batch-one')));
  assert.ok(calls.some((call) => call.url.includes('/day?date=2026-07-27')));
  assert.ok(calls.some((call) => call.url.includes('/month?year=2026&month=7')));
});

test('Calendar surfaces sanitized batch submission failure and unlocks the toolbar', async () => {
  let posts = 0;
  const { api, captured } = setupStatefulCalendar({
    fetchJSON(url, options) {
      if (options && options.method === 'POST') {
        posts++;
        return Promise.reject(new Error('submit failed\u0000bad'));
      }
      throw new Error('Unexpected request ' + url);
    },
  });
  const session = { profile: 'default', session_id: 'one' };
  const date = '2026-07-27';
  setBatchUiState(captured, {
    selectedDate: date,
    dayData: { sessions: [session], cron_runs: [] },
    selectedSessions: { [api.batchSelectionKey(date, session)]: true },
  });

  rawCalendarChild(api, captured, api.DayDetailPanel).props.onSubmitBatch();
  await flushPromises();
  assert.equal(posts, 1);
  assert.equal(captured.getState().batchStatus, null);
  assert.match(captured.getState().batchError, /Batch submission failed: submit failedbad/);
});

test('Calendar recovers latest running batch after refresh and cancels its poll on date change', async () => {
  const calls = [];
  const timers = [];
  const cleared = [];
  const running = {
    status: 'running', batch_id: 'batch-recovered', date: '2026-07-27', total: 1,
    members: [{ profile: 'default', session_id: 'one', status: 'running' }],
  };
  const { api, captured } = setupStatefulCalendar({
    captureEffects: true,
    fetchJSON(url) {
      calls.push(url);
      if (url.endsWith('/health')) return Promise.resolve({ status: 'ok' });
      if (url.includes('/month?')) return Promise.resolve({ days: [] });
      if (url.includes('/day?')) return Promise.resolve({ sessions: [], cron_runs: [] });
      if (url.includes('/session-summary/batches?')) return Promise.resolve({ batches: [running] });
      if (url.includes('/session-summary/rollup?')) return Promise.resolve({ exists: false });
      throw new Error('Unexpected request ' + url);
    },
    setTimeout(fn) { timers.push(fn); return timers.length; },
    clearTimeout(id) { cleared.push(id); },
  });
  captured.replaceState((state) => Object.assign({}, state, {
    selectedDate: '2026-07-27', viewYear: 2026, viewMonth: 6,
  }));
  captured.render();
  captured.runEffects();
  await flushPromises();

  assert.ok(calls.some((url) => url.includes('/session-summary/batches?date=2026-07-27&limit=1')));
  assert.equal(captured.getState().batchStatus.batch_id, 'batch-recovered');
  assert.equal(captured.getState().batchStatus.status, 'running');
  assert.equal(timers.length, 1);

  const grid = rawCalendarChild(api, captured, api.MonthGrid);
  grid.props.onDayClick('2026-07-28');
  assert.deepEqual(cleared, [1]);
  assert.equal(captured.getState().selectedDate, '2026-07-28');
  assert.equal(captured.getState().batchStatus, null);
});

test('SessionCard immutable version restore invokes the supplied rollback callback', () => {
  const { api } = loadPlugin();
  const calls = [];
  const session = { profile: 'default', session_id: 'one', title: 'Restore me' };
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session,
    summaryData: {
      exists: true,
      data: { summary: 'Stored summary' },
      versions: [{ version_id: 'version-one', generated_at: '2026-07-27T12:00:00Z' }],
    },
    selected: false,
    selectionDisabled: false,
    batchMemberStatus: null,
    onToggleSelect() {},
    onRollback(date, restoredSession, versionId) {
      calls.push({ date, restoredSession, versionId });
    },
    error: '',
  });
  const restore = findNodes(tree, (node) =>
    node.type === 'button' && node.props.className === 'dl-rollback-btn')[0];
  assert.ok(restore);
  restore.props.onClick();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].date, '2026-07-27');
  assert.equal(calls[0].restoredSession, session);
  assert.equal(calls[0].versionId, 'version-one');
});

test('batch control stylesheet includes focus, disabled, and mobile states', () => {
  const css = fs.readFileSync(path.join(__dirname, '..', 'dashboard', 'dist', 'style.css'), 'utf8');
  for (const className of [
    '.dl-batch-toolbar', '.dl-batch-btn', '.dl-batch-submit', '.dl-batch-option',
    '.dl-batch-progress', '.dl-batch-error', '.dl-session-select',
  ]) {
    assert.match(css, new RegExp(className.replace('.', '\\.')));
  }
  assert.match(css, /\.dl-batch-btn:focus-visible/);
  assert.match(css, /\.dl-batch-btn:disabled/);
  assert.match(css, /@media\s*\(max-width:\s*640px\)[\s\S]*\.dl-batch-toolbar/);
});
