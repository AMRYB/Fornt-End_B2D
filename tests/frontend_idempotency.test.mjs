import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const source = fs.readFileSync(new URL('../public/assets/js/app.js', import.meta.url), 'utf8');

test('paid browser mutations carry one retry-stable idempotency key', () => {
  assert.match(source, /headers\.set\('Idempotency-Key', pendingIdempotencyKey\(scope\)\)/);
  assert.match(source, /pendingIdempotencyKeys: new Map\(\)/);
  assert.match(source, /\[409, 425, 429, 502, 503, 504\]\.includes\(error\?\.status\)/);
  assert.match(source, /if \(!transient\) state\.pendingIdempotencyKeys\.delete\(scope\)/);
  assert.match(source, /idempotentApi\(\s*'\/api\/projects'/);
  assert.match(source, /idempotentApi\(\s*projectEndpoint\(state\.project\.project_id, '\/discovery\/message'\)/);
  assert.match(source, /idempotentApi\(\s*projectEndpoint\(state\.project\.project_id, '\/generation\/next'\)/);
  assert.match(source, /for \(let attempt = 0; attempt < 3; attempt \+= 1\)/);
});

test('malformed successful mutation responses retain the same idempotency key', () => {
  assert.match(source, /const expectJsonObject = request\.expectJsonObject === true/);
  assert.match(source, /delete request\.expectJsonObject/);
  assert.match(source, /response\.ok && expectJsonObject/);
  assert.match(source, /expectJsonObject && !isObject\(payload\)/);
  assert.match(source, /new TypeError\('The server returned an incomplete success response\.'\)/);
  assert.match(source, /const request = \{ \.\.\.options, headers, expectJsonObject: true \}/);
});

test('discovery confirmation trusts the canonical lifecycle status', () => {
  const helperSource = source.match(
    /function isDiscoveryReadyForConfirmation\(project\) \{[\s\S]*?\n\}/
  )?.[0];
  assert.ok(helperSource, 'confirmation-state helper is present');
  const isReady = Function(`${helperSource}; return isDiscoveryReadyForConfirmation;`)();

  assert.equal(isReady({ status: 'ready_for_confirmation', discovery: { status: 'ready' } }), true);
  assert.equal(isReady({ status: 'confirmed', discovery: { status: 'ready' } }), false);
  assert.equal(isReady({ status: 'generating', discovery: { status: 'ready' } }), false);
  assert.equal(isReady({ status: 'approved', discovery: { status: 'ready' } }), false);
  assert.equal(isReady({ discovery: { status: 'ready' } }), true);
});

test('action buttons resync after loading and confirmation reconciles uncertain responses', () => {
  assert.match(source, /\$\$\('#projectActionBar \.project-action-buttons button'\)\.forEach/);
  assert.match(source, /button\.disabled = busy \|\| state\.workflowRunning/);
  assert.match(source, /\/discovery\/confirm'[\s\S]*expectJsonObject: true/);
  assert.match(source, /await refreshActiveProject\(\)/);
  assert.match(source, /if \(discoveryHasBeenConfirmed\(state\.project\)\)/);
});
