import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const projectRoot = new URL('../', import.meta.url);
const authSource = readFileSync(new URL('public/assets/js/auth.js', projectRoot), 'utf8');
const appSource = readFileSync(new URL('public/assets/js/app.js', projectRoot), 'utf8');

function evaluateSafeNext(href) {
  const start = authSource.indexOf('function safeNextLocation()');
  const end = authSource.indexOf('\nfunction goToWorkspace()', start);
  assert.notEqual(start, -1, 'safeNextLocation must exist');
  assert.notEqual(end, -1, 'safeNextLocation must remain independently testable');

  const context = {
    URL,
    URLSearchParams,
    location: new URL(href),
    result: null
  };
  vm.runInNewContext(`${authSource.slice(start, end)}\nresult = safeNextLocation();`, context);
  return context.result;
}

test('login next redirect accepts only canonical same-origin paths', () => {
  const loginUrl = next => `https://app.example/login?next=${encodeURIComponent(next)}`;

  assert.equal(evaluateSafeNext(loginUrl('/projects?project=abc#review')), '/projects?project=abc#review');
  assert.equal(evaluateSafeNext(loginUrl('/')), '/');

  for (const unsafe of [
    'https://evil.example/steal',
    '//evil.example/steal',
    '/\\evil.example/steal',
    '/%5cevil.example/steal',
    '/%255cevil.example/steal',
    '/%25255cevil.example/steal',
    '/%2f%2fevil.example/steal',
    '/projects\nhttps://evil.example',
    'projects'
  ]) {
    assert.equal(evaluateSafeNext(loginUrl(unsafe)), '/', unsafe);
  }
});

test('Supabase browser dependency is pinned to an exact version', () => {
  const pinnedUrl = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.112.3/+esm';
  assert.ok(authSource.includes(pinnedUrl));
  assert.ok(appSource.includes(pinnedUrl));
  assert.doesNotMatch(authSource, /supabase-js@2\/\+esm/);
  assert.doesNotMatch(appSource, /supabase-js@2\/\+esm/);
});

test('Vercel CSP allows required origins without allowing inline scripts', () => {
  const vercel = JSON.parse(readFileSync(new URL('vercel.json', projectRoot), 'utf8'));
  const headers = vercel.headers.flatMap(rule => rule.headers);
  const csp = headers.find(header => header.key.toLowerCase() === 'content-security-policy')?.value;

  assert.ok(csp, 'Content-Security-Policy header is required');
  assert.match(csp, /script-src 'self' https:\/\/cdn\.jsdelivr\.net/);
  assert.doesNotMatch(csp, /script-src[^;]*'unsafe-inline'/);
  assert.match(csp, /connect-src 'self' https:\/\/\*\.supabase\.co wss:\/\/\*\.supabase\.co/);
  assert.match(csp, /style-src 'self' 'unsafe-inline' https:\/\/fonts\.googleapis\.com/);
  assert.match(csp, /font-src 'self' https:\/\/fonts\.gstatic\.com/);
  assert.match(csp, /img-src 'self' data: blob: https:\/\/lh3\.googleusercontent\.com/);
  assert.match(csp, /object-src 'none'/);
  assert.match(csp, /frame-ancestors 'none'/);
});
