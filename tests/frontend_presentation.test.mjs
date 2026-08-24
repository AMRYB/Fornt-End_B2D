import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const projectRoot = new URL('../', import.meta.url);
const appSource = readFileSync(new URL('public/assets/js/app.js', projectRoot), 'utf8');
const cssSource = readFileSync(new URL('public/assets/css/app.css', projectRoot), 'utf8');

test('known information is omitted from the overview while legacy JSON is cleaned', () => {
  assert.match(appSource, /function isKnownInformationKey\(key\)/);
  assert.match(appSource, /withoutKnownInformation\(structuredTextValue\(project\.summary\)\)/);
  assert.match(appSource, /userFacingAgentMessage\(message\)/);
  assert.doesNotMatch(appSource, /resultCard\('Known information', \{ full: true \}\)/);
});

test('architecture renders a pinned, strict Mermaid diagram before other cards', () => {
  assert.match(appSource, /mermaid@11\.17\.0\/dist\/mermaid\.esm\.min\.mjs/);
  assert.match(appSource, /securityLevel: 'strict'/);
  assert.match(appSource, /new DOMParser\(\)\.parseFromString\(svgText, 'image\/svg\+xml'\)/);
  assert.match(appSource, /safeLabel\.textContent = text/);
  assert.doesNotMatch(appSource, /querySelectorAll\('script, foreignObject/);
  assert.doesNotMatch(appSource, /Architecture diagram · Mermaid source/);

  const start = appSource.indexOf('function renderArchitecture(panel, architecture)');
  const end = appSource.indexOf('\nfunction renderDatabase(', start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const renderArchitecture = appSource.slice(start, end);
  assert.ok(
    renderArchitecture.indexOf("mermaidDiagramCard('Architecture diagram', mermaid)")
      < renderArchitecture.indexOf('const components ='),
    'the rendered diagram must be the first architecture result card'
  );
});

test('Docker and YAML artifacts use themed syntax tokenization', () => {
  assert.match(appSource, /function highlightedDockerfile\(content\)/);
  assert.match(appSource, /'dockerfile', 'Dockerfile', 'dockerfile'/);
  assert.match(appSource, /'docker_compose', 'Docker Compose', 'yaml'/);
  assert.match(cssSource, /\.syntax-instruction \{ color: var\(--brand-yellow\)/);
  assert.match(cssSource, /\.syntax-variable \{ color: #ff84bd/);
  assert.match(cssSource, /\.syntax-key \{ color: #43d6f5/);
});

test('seven checkpoints have transition motion without an agent activity panel', () => {
  assert.doesNotMatch(appSource, /agent-activity|renderAgentActivity|workflowActivity/);
  assert.match(appSource, /animateCheckpoint\(item, 'just-completed'\)/);
  assert.match(cssSource, /@keyframes checkpointTravel/);
  assert.match(cssSource, /@keyframes checkpointComplete/);
  assert.doesNotMatch(cssSource, /agent-activity|activityBars|activitySignal/);
  assert.match(cssSource, /grid-template-columns: repeat\(7, minmax\(0, 1fr\)\)/);
  assert.match(cssSource, /@media \(prefers-reduced-motion: reduce\)/);
});
