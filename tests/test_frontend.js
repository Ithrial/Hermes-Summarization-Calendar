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
    '    RollupSection: RollupSection,',
    '    CalendarPage: CalendarPage,',
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

  const captured = { component: null, initialState: null };
  const React = {
    Fragment: 'fragment',
    createElement(type, props, ...children) {
      return { type, props: props || {}, children };
    },
  };
  const hooks = {
    useState(initial) {
      captured.initialState = initial;
      return [initial, function noop() {}];
    },
    useEffect() {},
    useCallback(fn) { return fn; },
    useMemo(fn) { return fn(); },
    useRef(value) { return { current: value }; },
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
  if (predicate(node)) out.push(node);
  if (node.children) findNodes(node.children, predicate, out);
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
    generating: false,
    error: '',
    onGenerate() {},
    onRollback() {},
  });
  const links = findNodes(tree, (node) =>
    node.type === 'a' && node.props.className === 'dl-session-title');
  assert.equal(links.length, 1);
  assert.equal(links[0].props.href, '/chat?resume=session%2Fid&profile=named%20profile');
  assert.equal(flattenText(links[0]).join(''), 'Build the thing');
});

test('each session card offers isolated summary generation', () => {
  const { api } = loadPlugin();
  const tree = api.SessionCard({
    dateStr: '2026-07-27',
    session: sessionFixture(),
    summaryData: null,
    generating: false,
    error: '',
    onGenerate() {},
    onRollback() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /Generate summary/);
  assert.doesNotMatch(text, /Generate recap/);
  assert.doesNotMatch(text, /with model/);
  assert.doesNotMatch(text, /with worker/);
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
    generating: false,
    error: '',
    onGenerate() {},
    onRollback() {},
  });
  const text = flattenText(tree).join(' ');
  assert.match(text, /One canonical session summary/);
  assert.match(text, /Stale/);
  assert.match(text, /Regenerate/);
  assert.match(text, new RegExp('Restore: ' + version));
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
  assert.equal(summary.finished, 3); // completed + partial + failed
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
    fetchJSON: () => Promise.resolve({ job_status: { status: 'completed' } }),
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
  assert.ok(results[0].job_status.status === 'completed');
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
