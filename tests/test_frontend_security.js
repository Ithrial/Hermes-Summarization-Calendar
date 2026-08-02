'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { describe, it } = require('node:test');

const INDEX_PATH = path.resolve(__dirname, '..', 'dashboard', 'dist', 'index.js');
const SOURCE = fs.readFileSync(INDEX_PATH, 'utf-8');

describe('Source security bans', () => {
  it('bans innerHTML entirely', () => {
    assert.ok(!SOURCE.includes('innerHTML'),
      'Must not use innerHTML — all rendering via React.createElement');
  });
  it('bans dangerouslySetInnerHTML entirely', () => {
    assert.ok(!SOURCE.includes('dangerouslySetInnerHTML'),
      'Must not use dangerouslySetInnerHTML');
  });
  it('bans document.write', () => {
    assert.ok(!SOURCE.includes('document.write'),
      'Must not use document.write');
  });
  it('bans raw eval() calls (outside of comments)', () => {
    var lines = SOURCE.split('\n');
    for (var i = 0; i < lines.length; i++) {
      var trimmed = lines[i].trim();
      if (/^\/\//.test(trimmed) || /^(\*|\/\*)/.test(trimmed)) continue;
      assert.ok(!/\beval\s*\(/.test(trimmed),
        'Line ' + (i + 1) + ': eval() detected: ' + trimmed.substring(0, 80));
    }
  });
  it('bans new Function()', () => {
    assert.ok(!SOURCE.includes('new Function'),
      'Must not use new Function()');
  });
});

describe('Registration failure handling', () => {
  it('logs console.error on missing SDK', () => {
    assert.ok(SOURCE.includes("console.error('[daily-ledger]"),
      'Must log console.error when SDK is unavailable');
  });
  it('returns early without calling register when SDK missing', () => {
    var returnIdx = SOURCE.indexOf('return;');
    var registerIdx = SOURCE.indexOf("window.__HERMES_PLUGINS__.register");
    assert.ok(returnIdx > 0 && registerIdx > returnIdx,
      'Early return must precede register call');
  });
  it('wraps register call in try/catch', () => {
    assert.ok(SOURCE.includes('try') && SOURCE.includes('catch'),
      'Register must be wrapped in try/catch');
    assert.ok(SOURCE.match(/catch\s*\([^)]*\)\s*{[\s\S]*?console\.error/),
      'Catch block must log the error');
  });
  it('does not throw unhandled errors during IIFE execution', () => {
    assert.ok(SOURCE.match(/try\s*{[\s\S]*?register[\s\S]*?}\s*catch/),
      'Register call must be inside try block');
  });
});

describe('Data sanitization', () => {
  it('has a sanitize function that strips control characters', () => {
    assert.ok(SOURCE.includes('function sanitize'),
      'Must have sanitize function');
    assert.ok(SOURCE.includes('\\x00') || SOURCE.includes('[\\x00-'),
      'sanitize must strip control characters');
  });
  it('uses sanitize on user-facing content', () => {
    var sanitizeCount = (SOURCE.match(/sanitize\(/g) || []).length;
    assert.ok(sanitizeCount >= 5,
      'sanitize() should be called at least 5 times: got ' + sanitizeCount);
  });
  it('sanitizes session titles', () => {
    assert.ok(SOURCE.includes('sanitize(session.title'),
      'Session titles must be sanitized');
  });
  it('sanitizes cron job names or IDs', () => {
    assert.ok(SOURCE.includes('sanitize(cron.job_name') || SOURCE.includes('sanitize(cron.job_id'),
      'Cron job names/IDs must be sanitized');
  });
  it('does not reference raw system prompts or transcripts', () => {
    assert.ok(!SOURCE.includes('system_prompt') && !SOURCE.includes('transcript'),
      'Must not reference raw system prompts or transcripts');
  });
});

describe('API error handling', () => {
  it('uses .catch() on API calls to surface errors visibly', () => {
    var catchCount = (SOURCE.match(/\.catch\s*\(/g) || []).length;
    assert.ok(catchCount >= 3,
      'At least 3 API calls should have .catch() handlers: got ' + catchCount);
  });
  it('sanitizes error messages before display', () => {
    assert.ok(SOURCE.match(/sanitize\s*\(\s*String\s*\(\s*(err|errMsg)/),
      'Error messages must be sanitized before display');
  });
  it('has bounded backoff for recap polling', () => {
    assert.ok(SOURCE.includes('maxAttempts') || SOURCE.includes('max'),
      'Polling should have a maximum attempt limit');
    assert.ok(SOURCE.match(/Math\.min\s*\(/),
      'Backoff should be capped (bounded)');
  });
  it('handles 404/no-recap gracefully', () => {
    assert.ok(SOURCE.includes('{ exists: false }'),
      'Must handle missing recap (404) by setting exists: false');
  });
});

describe('Accessibility and keyboard support', () => {
  it('day cells have tabIndex for keyboard focus', () => {
    assert.ok(SOURCE.includes('tabIndex'),
      'Day cells must have tabIndex for keyboard navigation');
  });
  it('day cells have role button', () => {
    assert.ok(SOURCE.includes("role: 'button'"),
      'Interactive day cells must have role="button"');
  });
  it('navigation buttons have aria-label', () => {
    assert.ok(SOURCE.includes("'aria-label'"),
      'Navigation buttons must have aria-label for screen readers');
  });
  it('selection state communicated via aria-pressed', () => {
    assert.ok(SOURCE.includes("aria-pressed"),
      'Selected day cells should use aria-pressed');
  });
  it('keyboard Enter/Space triggers day selection', () => {
    assert.ok(SOURCE.includes("onKeyDown"),
      'Day cells should handle keyboard events for accessibility');
    assert.ok(SOURCE.includes("'Enter'") && SOURCE.includes("' '"),
      'Should handle both Enter and Space keys');
  });
  it('uses role alert for error messages', () => {
    assert.ok(SOURCE.includes("role: 'alert'"),
      'Error messages should use role="alert"');
  });
  it('uses role status for loading indicators', () => {
    assert.ok(SOURCE.includes("'status'"),
      'Loading indicators should use role="status"');
  });
  it('session title links have a visible keyboard focus style', () => {
    const cssPath = path.resolve(__dirname, '..', 'dashboard', 'dist', 'style.css');
    const css = fs.readFileSync(cssPath, 'utf-8');
    assert.ok(css.includes('.dl-session-title:focus-visible'),
      'Session title links must have a visible focus style');
  });
});

describe('Mobile responsiveness in CSS', () => {
  const cssPath = path.resolve(__dirname, '..', 'dashboard', 'dist', 'style.css');
  const CSS = fs.readFileSync(cssPath, 'utf-8');

  it('has mobile breakpoint media query', () => {
    assert.ok(CSS.includes('@media (max-width:'),
      'CSS should have responsive media queries for mobile');
  });
  it('uses theme-aware CSS custom properties', () => {
    assert.ok(CSS.includes('var(--color-'),
      'Should reference dashboard theme CSS variables');
  });
});
