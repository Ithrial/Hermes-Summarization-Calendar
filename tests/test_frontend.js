'use strict';

const assert = require('node:assert/strict');
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
    '    CalendarPage: CalendarPage',
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
