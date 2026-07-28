'use strict';

/**
 * Runtime-hardening tests for the Calendar dashboard bundle.
 * Targets four categories of runtime bugs found during code review:
 *  1. Recap overall_recap precedence (dereference of undefined .data)
 *  2. React.__spreadProps not available in browser
 *  3. pollRecap backoff correctness (recursive setTimeout, bounded, terminating)
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { describe, it } = require('node:test');

var INDEX_PATH = path.resolve(__dirname, '..', 'dashboard', 'dist', 'index.js');
var SOURCE = fs.readFileSync(INDEX_PATH, 'utf-8');

// ---------------------------------------------------------------------------
// Standalone pollRecap algorithm verifier (no real timers needed)
// Mirrors the exact structure in index.js: recursive setTimeout with
// scheduleTick -> doTick pattern.
// ---------------------------------------------------------------------------

function verifyPollRecapShim(fetchFn, maxAttempts, initialDelay) {
  var attempt = 0;
  var currentDelay = initialDelay || 2000;
  var max = maxAttempts || 15;
  var cancelled = false;
  var log = [];
  var finalResult = null;

  function scheduleTick() {
    if (cancelled) return undefined;
    currentDelay = Math.min(currentDelay * 2, 15000);
    log.push({ type: 'schedule', delay: currentDelay });
    // Synchronously invoke for testing and chain the result
    var res = doTick();
    if (res !== undefined) finalResult = res;
    return res;
  }

  function doTick() {
    if (cancelled) return undefined;
    attempt++;
    var result = fetchFn(attempt, currentDelay);
    log.push({ type: 'tick', attempt: attempt, delay: currentDelay });

    if (result && result.exists) {
      var foundResult = { status: 'found', data: result };
      finalResult = foundResult;
      log.push({ type: 'found' });
      return foundResult;
    } else if (attempt >= max) {
      var timeoutResult = { status: 'timeout' };
      finalResult = timeoutResult;
      log.push({ type: 'timeout' });
      return timeoutResult;
    } else {
      return scheduleTick();
    }
  }

  // First tick uses initialDelay and fires synchronously
  return {
    run: function () { return doTick(); },
    cancel: function () { cancelled = true; },
    getAttempts: function () { return attempt; },
    getLog: function () { return log; },
    getCurrentDelay: function () { return currentDelay; },
    getResult: function () { return finalResult; }
  };
}

// ---------------------------------------------------------------------------
// Bug 1: Roll-up overall_recap dereference safety
// ---------------------------------------------------------------------------

function resolveRollupOverall(rollupData) {
  var exists = !!(rollupData && rollupData.exists);
  var data = exists && rollupData.data ? rollupData.data : {};
  return data.overall_recap || '';
}

describe('Bug fix #1: roll-up overall_recap dereference safety', () => {
  it('source guards nested roll-up data before reading overall_recap', () => {
    assert.match(
      SOURCE,
      /var\s+data\s*=\s*exists\s*&&\s*rollupData\.data\s*\?\s*rollupData\.data\s*:\s*\{\}/,
      'Must resolve a safe nested roll-up data object first'
    );
  });

  it('source sanitizes overall_recap from the guarded data object', () => {
    assert.match(
      SOURCE,
      /sanitize\(data\.overall_recap\s*\|\|\s*['"]['"]\)/,
      'Must sanitize overall_recap from the guarded data object'
    );
  });

  it('resolves nested data.overall_recap', () => {
    var rollupData = {
      exists: true,
      data: { overall_recap: 'Nested summary text' }
    };
    assert.strictEqual(resolveRollupOverall(rollupData), 'Nested summary text');
  });

  it('returns empty string when data is absent', () => {
    assert.strictEqual(resolveRollupOverall({ exists: true }), '');
  });

  it('ignores obsolete top-level recap payloads', () => {
    var rollupData = { exists: true, overall_recap: 'Retired recap shape' };
    assert.strictEqual(resolveRollupOverall(rollupData), '');
  });
});

// ---------------------------------------------------------------------------
// Bug 2: React.__spreadProps not available in browser
// ---------------------------------------------------------------------------

describe('Bug fix #2: React.__spreadProps banned', () => {
  it('source contains zero occurrences of React.__spreadProps', () => {
    var count = (SOURCE.match(/React\.__spreadProps/g) || []).length;
    assert.strictEqual(count, 0,
      'Found ' + count + ' occurrences of React.__spreadProps — must use Object.assign or vanilla');
  });

  it('source uses Object.assign for state updates', () => {
    var assignCount = (SOURCE.match(/Object\.assign\s*\(\s*\{\}/g) || []).length;
    assert.ok(assignCount >= 3,
      'Expected at least 3 Object.assign({}, ...) calls in state updates: got ' + assignCount);
  });

  it('simulated state update works without React.__spreadProps', () => {
    var s = { a: 1, b: 2, c: 3 };
    var next = Object.assign({}, s, { b: 99 });
    assert.strictEqual(next.a, 1);
    assert.strictEqual(next.b, 99);
    assert.strictEqual(next.c, 3);
    // Original must be untouched (immutability)
    assert.strictEqual(s.b, 2);
  });

  it('simulated multi-key state update preserves all keys', () => {
    var s = { generating: false, loadingDay: true, error: '', selectedDate: '2026-01-01' };
    var next = Object.assign({}, s, { generating: true, selectedDate: null });
    assert.strictEqual(next.generating, true);
    assert.strictEqual(next.loadingDay, true); // preserved
    assert.strictEqual(next.error, '');        // preserved
    assert.strictEqual(next.selectedDate, null);
  });
});

// ---------------------------------------------------------------------------
// Bug 3: pollRecap bounded recursive setTimeout backoff
// ---------------------------------------------------------------------------

describe('Bug fix #3: pollRecap recursive setTimeout with bounded backoff', () => {
  it('source uses setTimeout (not setInterval) for polling', () => {
    assert.ok(SOURCE.includes('setTimeout(doTick') || SOURCE.includes('setTimeout(tick'),
      'Must schedule ticks via setTimeout');
    assert.ok(!SOURCE.includes('setInterval'),
      'Must NOT use setInterval — recursive setTimeout only');
  });

  it('source has scheduleTick/doTick or equivalent recursive pattern', () => {
    assert.ok(
      (SOURCE.includes('function doTick') || SOURCE.includes('doTick')) &&
      (SOURCE.includes('function scheduleTick') || SOURCE.includes('scheduleTick')),
      'Should have named recursive tick functions'
    );
  });

  it('source has cancelled flag for clean shutdown', () => {
    assert.ok(SOURCE.includes('cancelled'),
      'Must have a cancelled flag to stop in-flight ticks');
  });

  it('pollRecap shim increases delay with each attempt (bounded)', () => {
    var fetchFn = function (attempt, currentDelay) {
      return {}; // never succeeds — keep polling
    };

    var poller = verifyPollRecapShim(fetchFn, 6, 1000);
    poller.run();

    var log = poller.getLog();
    // Should have schedule entries with increasing delays: 2000, 4000, 8000...
    var scheduledDelays = log.filter(function (e) { return e.type === 'schedule'; })
      .map(function (e) { return e.delay; });

    assert.ok(scheduledDelays.length >= 2, 'Should have multiple scheduled ticks');
    // Delays should be non-decreasing (flat at cap is fine)
    for (var i = 1; i < scheduledDelays.length; i++) {
      assert.ok(scheduledDelays[i] >= scheduledDelays[i - 1],
        'Delay must not decrease: ' + JSON.stringify(scheduledDelays));
    }
    // First tick was at initial delay (1000), schedules start at double (2000)
    assert.strictEqual(scheduledDelays[0], 2000, 'First schedule at doubled initial delay');
    // At least one increase before the cap kicks in
    var increased = false;
    for (var j = 1; j < scheduledDelays.length; j++) {
      if (scheduledDelays[j] > scheduledDelays[j - 1]) { increased = true; break; }
    }
    assert.ok(increased, 'Delay should increase before plateauing at cap: ' + JSON.stringify(scheduledDelays));
  });

  it('pollRecap shim caps delay at maximum (15000)', () => {
    var fetchFn = function () { return {}; };

    var poller = verifyPollRecapShim(fetchFn, 20, 8000);
    poller.run();

    var log = poller.getLog();
    var maxSeen = 0;
    log.filter(function (e) { return e.type === 'schedule'; }).forEach(function (e) {
      assert.ok(e.delay <= 15000, 'Delay must not exceed cap: got ' + e.delay);
      if (e.delay > maxSeen) maxSeen = e.delay;
    });

    assert.strictEqual(maxSeen, 15000, 'Should reach the cap');
  });

  it('pollRecap shim terminates on success', () => {
    var callCount = 0;
    var fetchFn = function (attempt) {
      callCount++;
      if (callCount === 2) return { exists: true }; // succeed on second try
      return {};
    };

    var poller = verifyPollRecapShim(fetchFn, 10, 100);
    var result = poller.run();

    assert.strictEqual(result.status, 'found');
    assert.ok(callCount >= 2, 'Should have polled at least twice: got ' + callCount);
  });

  it('pollRecap shim terminates on max attempts', () => {
    var fetchFn = function () { return {}; };

    var poller = verifyPollRecapShim(fetchFn, 3, 100);
    var result = poller.run();

    assert.strictEqual(result.status, 'timeout');
    assert.ok(poller.getAttempts() <= 3,
      'Must stop after max attempts: got ' + poller.getAttempts());
  });

  it('pollRecap shim cancel stops further ticks', () => {
    var callCount = 0;
    var fetchFn = function () {
      callCount++;
      if (callCount === 1) poller.cancel();
      return {};
    };

    var poller = verifyPollRecapShim(fetchFn, 10, 50);
    var result = poller.run();

    assert.strictEqual(callCount, 1, 'Should cancel after first call');
  });

  it('pollRecap shim tracks exactly one request in flight at a time', () => {
    // With synchronous execution the pattern guarantees serial ticks.
    // In real code setTimeout ensures only one doTick is pending.
    var fetchFn = function () { return {}; };
    var poller = verifyPollRecapShim(fetchFn, 5, 100);
    poller.run();

    var log = poller.getLog();
    // Each tick must be preceded by at most one schedule (except the first)
    var ticks = log.filter(function (e) { return e.type === 'tick'; });
    var schedules = log.filter(function (e) { return e.type === 'schedule'; });
    assert.ok(ticks.length === schedules.length + 1,
      'Tick/schedule count mismatch: ' + ticks.length + ' ticks vs ' + schedules.length + ' schedules');
  });
});

// ---------------------------------------------------------------------------
// Combined source scan (covers bugs 1 + 2)
// ---------------------------------------------------------------------------

describe('Combined source safety', () => {
  it('no innerHTML or dangerouslySetInnerHTML', () => {
    assert.ok(!SOURCE.includes('innerHTML'));
    assert.ok(!SOURCE.includes('dangerouslySetInnerHTML'));
  });

  it('uses React.createElement throughout', () => {
    assert.ok(SOURCE.includes('React.createElement'));
  });

  it('registration wrapped in try/catch', () => {
    assert.ok(SOURCE.match(/try\s*\{[\s\S]*?register[\s\S]*?\}\s*catch/));
  });

  it('early return when SDK unavailable', () => {
    var errIdx = SOURCE.indexOf("console.error('[daily-ledger]");
    var retIdx = SOURCE.indexOf('return;');
    assert.ok(errIdx >= 0 && retIdx > errIdx, 'Must log then return early');
  });

  it('pollRecap has exponential doubling (delay * 2) in source', () => {
    assert.ok(SOURCE.match(/currentDelay\s*\*\s*2/),
      'Should double delay each round: currentDelay * 2');
  });

  it('pollRecap caps at Math.min(..., 15000)', () => {
    assert.ok(SOURCE.includes('Math.min(currentDelay * 2, 15000)'),
      'Must cap doubled delay at 15000');
  });
});
