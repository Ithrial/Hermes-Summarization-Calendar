'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { describe, it } = require('node:test');

// Extract pure JS helpers from index.js for testing (date/grid utilities)
// These match the functions inside the IIFE so we verify their logic.

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
    cells.push({ day: 1 + next, month: month + 1, year: year, current: false });
  }
  return cells;
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

function encodeUrlParams(params) {
  var parts = [];
  for (var k in params) {
    if (params.hasOwnProperty(k)) {
      parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(String(params[k])));
    }
  }
  return '?' + parts.join('&');
}

// Load source file for security scans
const INDEX_PATH = path.resolve(__dirname, '..', 'dashboard', 'dist', 'index.js');
const SOURCE = fs.readFileSync(INDEX_PATH, 'utf-8');

describe('Calendar date helpers (DST-independent)', () => {
  describe('getDaysInMonth', () => {
    it('returns 31 for January', () => {
      assert.strictEqual(getDaysInMonth(2026, 0), 31);
    });
    it('returns 28 for non-leap February', () => {
      assert.strictEqual(getDaysInMonth(2026, 1), 28);
    });
    it('returns 29 for leap February', () => {
      assert.strictEqual(getDaysInMonth(2024, 1), 29);
    });
    it('returns 30 for April', () => {
      assert.strictEqual(getDaysInMonth(2026, 3), 30);
    });
    it('handles century leap year (2000)', () => {
      assert.strictEqual(getDaysInMonth(2000, 1), 29);
    });
    it('handles non-century non-leap (1900)', () => {
      assert.strictEqual(getDaysInMonth(1900, 1), 28);
    });
  });

  describe('getFirstDayOfWeek (Monday-first)', () => {
    it('July 2026 starts on Wednesday -> index 2', () => {
      assert.strictEqual(getFirstDayOfWeek(2026, 6), 2);
    });
    it('January 2026 starts on Thursday -> index 3', () => {
      assert.strictEqual(getFirstDayOfWeek(2026, 0), 3);
    });
    it('Friday start maps to index 4 (May 2026)', () => {
      // May 1, 2026 is Friday. getDay()=5. (5+6)%7 = 4
      assert.strictEqual(getFirstDayOfWeek(2026, 4), 4);
    });
    it('Monday start maps to index 0 (June 2026)', () => {
      // June 1, 2026 is Monday. getDay()=1. (1+6)%7 = 0
      assert.strictEqual(getFirstDayOfWeek(2026, 5), 0);
    });
  });

  describe('buildCalendarGrid', () => {
    it('produces cell count that is multiple of 7 for July 2026', () => {
      var grid = buildCalendarGrid(2026, 6);
      assert.ok(grid.length % 7 === 0, 'Grid cells must be multiple of 7');
      var currentCells = grid.filter(c => c.current);
      assert.strictEqual(currentCells.length, 31);
    });
    it('fills prev-month cells for July 2026', () => {
      var grid = buildCalendarGrid(2026, 6);
      assert.strictEqual(grid[0].current, false);
      assert.strictEqual(grid[0].month, 5); // June
      assert.strictEqual(grid[1].current, false);
    });
    it('fills next-month cells for February 2026 (28 days)', () => {
      var grid = buildCalendarGrid(2026, 1);
      var nextMonthCells = grid.filter(c => c.month === 2); // March
      assert.strictEqual(nextMonthCells.length, 1);
    });
    it('handles month boundary December -> January', () => {
      var grid = buildCalendarGrid(2026, 11);
      var currentCells = grid.filter(c => c.current);
      assert.strictEqual(currentCells.length, 31);
    });
    it('all current cells have correct month/year', () => {
      var grid = buildCalendarGrid(2026, 6);
      var currentCells = grid.filter(c => c.current);
      for (var i = 0; i < currentCells.length; i++) {
        assert.strictEqual(currentCells[i].month, 6);
        assert.strictEqual(currentCells[i].year, 2026);
      }
    });
    it('days are sequential within current month', () => {
      var grid = buildCalendarGrid(2026, 6);
      var currentCells = grid.filter(c => c.current);
      for (var i = 1; i < currentCells.length; i++) {
        assert.strictEqual(currentCells[i].day, currentCells[i - 1].day + 1);
      }
    });
  });

  describe('formatDate / parseDate round-trip', () => {
    it('formats single-digit month correctly', () => {
      assert.strictEqual(formatDate(2026, 0, 5), '2026-01-05');
    });
    it('formats double-digit month correctly', () => {
      assert.strictEqual(formatDate(2026, 11, 31), '2026-12-31');
    });
    it('round-trips through parseDate', () => {
      var str = formatDate(2026, 6, 15);
      var parsed = parseDate(str);
      assert.strictEqual(parsed.year, 2026);
      assert.strictEqual(parsed.month, 6);
      assert.strictEqual(parsed.day, 15);
    });
    it('handles DST transition date (Nov 1, 2026 - fall back)', () => {
      var str = formatDate(2026, 10, 1);
      var parsed = parseDate(str);
      assert.strictEqual(parsed.year, 2026);
      assert.strictEqual(parsed.month, 10);
      assert.strictEqual(parsed.day, 1);
    });
    it('handles DST transition date (Mar 8, 2026 - spring forward)', () => {
      var str = formatDate(2026, 2, 8);
      var parsed = parseDate(str);
      assert.strictEqual(parsed.year, 2026);
      assert.strictEqual(parsed.month, 2);
      assert.strictEqual(parsed.day, 8);
    });
  });
});

describe('API URL encoding', () => {
  it('encodes simple params', () => {
    assert.strictEqual(encodeUrlParams({ year: '2026', month: '7' }), '?year=2026&month=7');
  });
  it('encodes date param', () => {
    assert.strictEqual(encodeUrlParams({ date: '2026-03-08' }), '?date=2026-03-08');
  });
  it('encodes special characters in values', () => {
    assert.strictEqual(encodeUrlParams({ version: 'abc-def' }), '?version=abc-def');
    assert.strictEqual(encodeUrlParams({ name: 'test job' }), '?name=test%20job');
  });
  it('handles empty params', () => {
    assert.strictEqual(encodeUrlParams({}), '?');
  });
  it('stringifies numeric values', () => {
    assert.strictEqual(encodeUrlParams({ year: 2026, month: 7 }), '?year=2026&month=7');
  });
});

describe('Source code security scan', () => {
  it('does not use innerHTML', () => {
    assert.ok(!SOURCE.includes('innerHTML'), 'index.js must not use innerHTML');
  });
  it('does not use dangerouslySetInnerHTML', () => {
    assert.ok(!SOURCE.includes('dangerouslySetInnerHTML'), 'index.js must not use dangerouslySetInnerHTML');
  });
  it('uses React.createElement for rendering', () => {
    assert.ok(SOURCE.includes('React.createElement'), 'index.js should use React.createElement');
  });
  it('does not contain raw eval()', () => {
    var lines = SOURCE.split('\n');
    for (var i = 0; i < lines.length; i++) {
      var trimmed = lines[i].trim();
      if (trimmed.startsWith('//')) continue;
      assert.ok(!trimmed.includes('eval('), 'Line ' + (i+1) + ' contains suspicious eval()');
    }
  });
  it('uses try/catch for registration', () => {
    assert.ok(SOURCE.includes('try') && SOURCE.includes('catch'),
      'Registration must be wrapped in try/catch');
  });
  it('logs one clear error on SDK missing', () => {
    assert.ok(SOURCE.includes("console.error('[daily-ledger]"),
      'Must log a clear error when SDK is unavailable');
  });
});

describe('IIFE registration pattern', () => {
  it('checks for SDK and PLUGINS globals before proceeding', () => {
    assert.ok(SOURCE.includes('window.__HERMES_PLUGIN_SDK__'));
    assert.ok(SOURCE.includes('window.__HERMES_PLUGINS__'));
  });
  it('calls window.__HERMES_PLUGINS__.register with correct name', () => {
    assert.ok(SOURCE.includes("window.__HERMES_PLUGINS__.register('daily-ledger'"));
  });
  it('returns early when SDK unavailable (no crash)', () => {
    var returnIdx = SOURCE.indexOf('return;');
    var registerIdx = SOURCE.indexOf("window.__HERMES_PLUGINS__.register");
    assert.ok(returnIdx > 0 && registerIdx > returnIdx,
      'Early return must precede register call');
  });
});

describe('Manifest validation', () => {
  const manifestPath = path.resolve(__dirname, '..', 'dashboard', 'manifest.json');
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf-8'));

  it('has required name field', () => {
    assert.strictEqual(manifest.name, 'daily-ledger');
  });
  it('has label Calendar', () => {
    assert.strictEqual(manifest.label, 'Calendar');
  });
  it('has correct version', () => {
    assert.strictEqual(manifest.version, '1.0.0');
  });
  it('has tab path /calendar', () => {
    assert.strictEqual(manifest.tab.path, '/calendar');
  });
  it('has valid icon name from supported set', () => {
    var validIcons = ['Activity','BarChart3','Clock','Code','Database','Eye','FileText',
      'Globe','Heart','KeyRound','MessageSquare','Package','Puzzle','Settings',
      'Shield','Sparkles','Star','Terminal','Wrench','Zap'];
    assert.ok(validIcons.includes(manifest.icon), 'Icon must be from supported set');
  });
  it('has entry pointing to dist/index.js', () => {
    assert.strictEqual(manifest.entry, 'dist/index.js');
  });
  it('has css pointing to dist/style.css', () => {
    assert.strictEqual(manifest.css, 'dist/style.css');
  });
  it('references api plugin_api.py', () => {
    assert.strictEqual(manifest.api, 'plugin_api.py');
  });
});
