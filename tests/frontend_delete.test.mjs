import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const projectRoot = new URL('../', import.meta.url);
const appSource = readFileSync(new URL('public/assets/js/app.js', projectRoot), 'utf8');
const cssSource = readFileSync(new URL('public/assets/css/app.css', projectRoot), 'utf8');

test('delete option exists in the sidebar rows and the overflow menu', () => {
  assert.match(appSource, /async function deleteProject\(/);
  assert.match(appSource, /function deleteCurrentProject\(\)/);
  assert.match(appSource, /node\('button', 'project-row-delete', '×'\)/);
  assert.match(appSource, /node\('button', 'danger', 'Delete project'\)/);
  assert.match(appSource, /deleteButton\.id = 'deleteChat'/);
  assert.match(appSource, /\$\('#deleteChat'\)\?\.addEventListener\('click', deleteCurrentProject\)/);
  assert.match(cssSource, /\.project-row-delete \{/);
  assert.match(cssSource, /\.menu button\.danger \{/);
});

test('delete calls the DELETE endpoint behind a confirmation and resets the open chat', () => {
  assert.match(appSource, /await api\(projectEndpoint\(projectId\), \{ method: 'DELETE' \}\)/);
  assert.match(appSource, /window\.confirm\(/);
  assert.match(appSource, /This permanently removes the conversation/);
  assert.match(appSource, /if \(state\.project\?\.project_id === projectId\) resetProject\(\)/);
  assert.match(appSource, /showBanner\('Project deleted\.', 'success'\)/);
});

test('delete is blocked while a generation is running', () => {
  assert.match(appSource, /if \(state\.workflowRunning\) \{\s*\n\s*showBanner\('Wait for generation to finish before deleting this project\.'\)/);
});

test('app.js parses as valid JavaScript so every wiring actually loads', () => {
  assert.doesNotThrow(() => new Function(appSource));
});
