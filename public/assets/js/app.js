const STAGES = [
  { id: 'discovery', label: 'Discovery' },
  { id: 'requirements', label: 'Requirements' },
  { id: 'architecture', label: 'Architecture' },
  { id: 'database', label: 'Database' },
  { id: 'api', label: 'API' },
  { id: 'devops', label: 'DevOps' },
  { id: 'review', label: 'Review' }
];

const SUPABASE_ESM_URL = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.112.3/+esm';
const MERMAID_ESM_URL = 'https://cdn.jsdelivr.net/npm/mermaid@11.17.0/dist/mermaid.esm.min.mjs';

const STAGE_ACTIVITY = {
  discovery: 'Understanding the idea, capturing confirmed details, and resolving gaps.',
  requirements: 'Structuring functional requirements, user stories, constraints, and acceptance criteria.',
  architecture: 'Designing system boundaries, communication, security, and the deployment topology.',
  database: 'Modeling entities, relationships, indexes, constraints, and the SQL foundation.',
  api: 'Defining endpoints, authentication, schemas, validation, and error contracts.',
  devops: 'Preparing containers, CI/CD, environments, health checks, logging, and monitoring.',
  review: 'Checking every artifact for completeness, consistency, security, and deployability.'
};

const STAGE_STATE_CLASSES = ['pending', 'running', 'done', 'failed'];
const DOCKER_INSTRUCTIONS = new Set([
  'ADD', 'ARG', 'CMD', 'COPY', 'ENTRYPOINT', 'ENV', 'EXPOSE', 'FROM',
  'HEALTHCHECK', 'LABEL', 'MAINTAINER', 'ONBUILD', 'RUN', 'SHELL',
  'STOPSIGNAL', 'USER', 'VOLUME', 'WORKDIR'
]);

let mermaidRendererPromise = null;
let mermaidRenderSequence = 0;

const RESULT_TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'requirements', label: 'Requirements' },
  { id: 'architecture', label: 'Architecture' },
  { id: 'database', label: 'Database' },
  { id: 'api', label: 'API' },
  { id: 'devops', label: 'DevOps' },
  { id: 'review', label: 'Review' },
  { id: 'files', label: 'Files' }
];

const TERMINAL_STATUSES = new Set(['approved', 'revised', 'needs_attention']);
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];

const state = {
  config: null,
  authClient: null,
  session: null,
  user: null,
  projects: [],
  project: null,
  artifacts: [],
  activeTab: 'overview',
  currentView: 'chat',
  busy: false,
  workflowRunning: false,
  follow: null,
  search: '',
  bannerTimer: null,
  pendingIdempotencyKeys: new Map(),
  stageVisualProjectId: null,
  stageVisualStates: null,
  generationActivityPhase: 'idle'
};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function clear(element) {
  if (element) element.replaceChildren();
  return element;
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasValue(value) {
  if (value === null || value === undefined || value === '') return false;
  if (Array.isArray(value)) return value.length > 0;
  if (isObject(value)) return Object.keys(value).length > 0;
  return true;
}

function labelFor(key) {
  return String(key || '')
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, letter => letter.toUpperCase());
}

function textValue(value) {
  if (value === null || value === undefined) return 'Not provided';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  return JSON.stringify(value, null, 2);
}

function isKnownInformationKey(key) {
  return String(key || '').replace(/[^a-z0-9]/gi, '').toLowerCase() === 'knowninformation';
}

function withoutKnownInformation(value) {
  if (!isObject(value)) return value;
  return Object.fromEntries(
    Object.entries(value).filter(([key]) => !isKnownInformationKey(key))
  );
}

function structuredTextValue(value) {
  if (typeof value !== 'string') return value;
  const candidate = value.trim().replace(/^```json\s*/i, '').replace(/\s*```$/i, '');
  if (!candidate.startsWith('{')) return value;
  try {
    return JSON.parse(candidate);
  } catch {
    return value;
  }
}

function userFacingAgentMessage(value) {
  const parsed = structuredTextValue(value);
  if (!isObject(parsed) || !Object.keys(parsed).some(isKnownInformationKey)) return String(value ?? '');
  const summary = parsed.summary || parsed.message || parsed.response;
  if (typeof summary === 'string' && summary.trim()) return summary.trim();
  const cleaned = withoutKnownInformation(parsed);
  return hasValue(cleaned) ? JSON.stringify(cleaned, null, 2) : 'Known information updated.';
}

function formatDate(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function projectTitle(project) {
  return project?.title || project?.business_idea || 'Untitled project';
}

function readableError(error) {
  if (!error) return 'Something went wrong. Please try again.';
  if (typeof error === 'string') return error;
  return error.detail || error.message || error.error_description || 'Something went wrong. Please try again.';
}

function sleep(milliseconds) {
  return new Promise(resolve => window.setTimeout(resolve, milliseconds));
}

function showBanner(message, kind = 'error', persistent = false) {
  const banner = $('#globalBanner');
  if (!banner) return;
  clearTimeout(state.bannerTimer);
  banner.querySelector('span').textContent = message;
  banner.className = `global-banner show ${kind}`;
  if (!persistent) state.bannerTimer = window.setTimeout(() => banner.classList.remove('show'), 5000);
}

function hideBanner() {
  clearTimeout(state.bannerTimer);
  $('#globalBanner')?.classList.remove('show');
}

function setAppLoading(show, message = 'Loading your workspace…') {
  const loader = $('#appLoader');
  if (!loader) return;
  loader.querySelector('p').textContent = message;
  loader.hidden = !show;
}

function setBusy(busy) {
  state.busy = busy;
  document.body.classList.toggle('busy', busy);
  const send = $('#sendBtn');
  if (send) send.disabled = busy || state.workflowRunning;
  $$('#projectActionBar .project-action-buttons button').forEach(button => {
    button.disabled = busy || state.workflowRunning;
  });
  inputChanged();
}

function prepareShell() {
  document.title = 'Business to Development · AI Project Workspace';
  document.body.classList.add('project-empty');

  const loader = node('div', 'app-loader');
  loader.id = 'appLoader';
  const loaderCard = node('div', 'loader-card');
  loaderCard.append(node('div', 'loader-ring'), node('strong', '', 'Business to Development'), node('p', 'muted', 'Loading your workspace…'));
  loader.append(loaderCard);
  document.body.append(loader);

  const banner = node('div', 'global-banner');
  banner.id = 'globalBanner';
  banner.setAttribute('role', 'alert');
  banner.setAttribute('aria-live', 'assertive');
  banner.append(node('span'));
  const close = node('button', '', '×');
  close.type = 'button';
  close.setAttribute('aria-label', 'Dismiss');
  close.addEventListener('click', hideBanner);
  banner.append(close);
  document.body.append(banner);

  $$('.chat-link').forEach(element => element.remove());
  const sections = $$('.section');
  if (sections[0]) sections[0].remove();
  const recentLabel = sections.find(section => section.isConnected)?.querySelector('span');
  if (recentLabel) recentLabel.textContent = 'Your projects';
  $('#clearRecent')?.remove();

  const topLeft = $('.top-left');
  const viewSwitch = node('div', 'view-switch');
  viewSwitch.id = 'viewSwitch';
  const chatButton = node('button', 'view-toggle active', 'Chat');
  chatButton.type = 'button';
  chatButton.dataset.view = 'chat';
  const resultsButton = node('button', 'view-toggle', 'Blueprint');
  resultsButton.type = 'button';
  resultsButton.dataset.view = 'results';
  resultsButton.disabled = true;
  viewSwitch.append(chatButton, resultsButton);
  topLeft?.append(viewSwitch);
  const titleChip = node('div', 'project-title-chip', 'New project');
  titleChip.id = 'projectTitleChip';
  topLeft?.append(titleChip);

  $('#tempBtn')?.setAttribute('hidden', '');
  $('#shareBtn')?.setAttribute('hidden', '');

  const actionBar = node('div', 'project-action-bar');
  actionBar.id = 'projectActionBar';
  actionBar.innerHTML = '<div class="project-action-copy"><strong></strong><span></span></div><div class="project-action-buttons"></div>';
  $('.composer-stack')?.insertBefore(actionBar, $('#followWaiting'));

  const workView = $('#workView');
  if (workView) {
    workView.className = 'work-view results-view';
    workView.innerHTML = `
      <div class="results-header">
        <div class="results-title"><h1 id="resultsTitle">Project blueprint</h1><p id="resultsSubtitle">Generated by seven coordinated AI agents.</p></div>
        <span class="status-badge" id="resultsStatus">Discovery</span>
      </div>
      <div class="results-progress" id="resultsProgress"></div>
      <div class="result-tabs" id="resultTabs" role="tablist"></div>
      <div class="result-panel" id="resultPanel" role="tabpanel"></div>
    `;
  }

  const accountMenu = $('#accountMenu');
  if (accountMenu) {
    accountMenu.replaceChildren();
    const logout = node('button', 'danger', 'Log out');
    logout.id = 'logoutButton';
    accountMenu.append(logout);
  }

  const topMenu = $('#topMenu');
  if (topMenu) {
    topMenu.replaceChildren();
    const exportButton = node('button', '', 'Export conversation');
    exportButton.id = 'exportConversation';
    const copyLink = node('button', '', 'Copy project link');
    copyLink.id = 'copyProjectLink';
    topMenu.append(exportButton, copyLink);
  }

  $('#greeting').textContent = 'What business idea are we turning into a blueprint?';
  $('#prompt').placeholder = 'Describe your business idea';
  $('#attachBtn').title = 'Attachments are not enabled for this workflow';
  $('#toolsBtn').title = 'The seven-agent workflow selects its own tools';
  renderResultTabs();
  renderProgress();
  setChatStarted(false);
}

function placeMenu(menu, anchor, align = 'left') {
  closeMenus();
  if (!menu || !anchor) return;
  const rect = anchor.getBoundingClientRect();
  menu.classList.add('show');
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  let left = align === 'right' ? rect.right - width : rect.left;
  let top = rect.top - height - 8;
  if (top < 8) top = rect.bottom + 8;
  left = Math.max(8, Math.min(left, innerWidth - width - 8));
  menu.style.left = `${left}px`;
  menu.style.top = `${Math.min(top, innerHeight - height - 8)}px`;
}

function closeMenus() {
  $$('.menu.show').forEach(menu => menu.classList.remove('show'));
}

async function loadPublicConfig() {
  const response = await fetch('/api/config', { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error(`Could not load application configuration (${response.status}).`);
  return response.json();
}

async function initializeAuthentication() {
  state.config = await loadPublicConfig();
  if (!state.config.auth_enabled) {
    updateAccount(null);
    return;
  }
  if (!state.config.supabase_url || !state.config.supabase_anon_key) {
    throw new Error('Authentication is enabled, but Supabase public configuration is incomplete.');
  }

  const { createClient } = await import(SUPABASE_ESM_URL);
  state.authClient = createClient(state.config.supabase_url, state.config.supabase_anon_key, {
    auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true }
  });
  const { data, error } = await state.authClient.auth.getSession();
  if (error) throw error;
  if (!data.session) {
    const next = `${location.pathname}${location.search}`;
    location.replace(`/login?next=${encodeURIComponent(next)}`);
    throw new Error('Redirecting to sign in.');
  }
  state.session = data.session;
  state.user = data.session.user;
  updateAccount(state.user);
  state.authClient.auth.onAuthStateChange((event, session) => {
    state.session = session;
    state.user = session?.user || null;
    updateAccount(state.user);
    if (event === 'SIGNED_OUT') location.replace('/login');
  });
}

function updateAccount(user) {
  const name = user?.user_metadata?.full_name || user?.email?.split('@')[0] || 'Local workspace';
  const email = user?.email || (state.config?.auth_enabled ? 'Signed out' : 'Authentication off');
  const avatar = $('.avatar');
  const accountName = $('.acct-name');
  const badge = $('.badge');
  if (avatar) avatar.textContent = name.trim().charAt(0).toUpperCase() || 'B';
  if (accountName) accountName.textContent = name;
  if (badge) badge.textContent = email;
  const logout = $('#logoutButton');
  if (logout) logout.hidden = !state.config?.auth_enabled;
}

async function accessToken() {
  if (!state.config?.auth_enabled || !state.authClient) return null;
  const { data, error } = await state.authClient.auth.getSession();
  if (error) throw error;
  state.session = data.session;
  if (!data.session) {
    location.replace('/login');
    throw new Error('Your session has expired. Please sign in again.');
  }
  return data.session.access_token;
}

async function authorizedFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set('Accept', options.accept || 'application/json');
  const token = await accessToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (options.body !== undefined && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401 && state.config?.auth_enabled) {
    await state.authClient?.auth.signOut();
    location.replace('/login');
  }
  return response;
}

async function api(path, options = {}) {
  const request = { ...options };
  const expectJsonObject = request.expectJsonObject === true;
  delete request.expectJsonObject;
  if (request.body !== undefined && !(request.body instanceof FormData) && typeof request.body !== 'string') {
    request.body = JSON.stringify(request.body);
  }
  const response = await authorizedFetch(path, request);
  const contentType = response.headers.get('content-type') || '';
  let payload = null;
  if (response.status !== 204) {
    if (contentType.includes('application/json')) {
      try {
        payload = await response.json();
      } catch (error) {
        if (response.ok && expectJsonObject) {
          throw new TypeError('The server returned an incomplete JSON response.', { cause: error });
        }
      }
    } else {
      payload = await response.text().catch(() => '');
    }
  }
  if (!response.ok) {
    const message = isObject(payload) ? payload.detail || payload.message || payload.error : payload;
    const error = new Error(message || `Request failed (${response.status}).`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  // Paid mutations always return an object. A committed serverless request can
  // still reach the browser with a truncated, empty, or proxy-generated 2xx
  // body. Treat that as an uncertain transport result so its idempotency key is
  // retained and the exact same operation can be reconciled safely.
  if (expectJsonObject && !isObject(payload)) {
    throw new TypeError('The server returned an incomplete success response.');
  }
  return payload;
}

function newIdempotencyKey() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function pendingIdempotencyKey(scope) {
  const existing = state.pendingIdempotencyKeys.get(scope);
  if (existing) return existing;
  if (state.pendingIdempotencyKeys.size >= 64) {
    state.pendingIdempotencyKeys.delete(state.pendingIdempotencyKeys.keys().next().value);
  }
  const key = newIdempotencyKey();
  state.pendingIdempotencyKeys.set(scope, key);
  return key;
}

async function idempotentApi(path, options = {}, scope = path) {
  const headers = new Headers(options.headers || {});
  headers.set('Idempotency-Key', pendingIdempotencyKey(scope));
  const request = { ...options, headers, expectJsonObject: true };
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const result = await api(path, request);
      state.pendingIdempotencyKeys.delete(scope);
      return result;
    } catch (error) {
      // Network loss and serverless/upstream uncertainty can hide a successful
      // commit. Keep this key beyond the automatic retries, so a later manual
      // retry reconciles the same logical operation as well.
      const transient = error instanceof TypeError || [409, 425, 429, 502, 503, 504].includes(error?.status);
      if (!transient) state.pendingIdempotencyKeys.delete(scope);
      if (!transient || attempt === 2) throw error;
      await sleep(750 * (attempt + 1));
    }
  }
  throw new Error('The request could not be reconciled. Please retry.');
}

function normalizeProject(payload) {
  if (!payload) return null;
  const source = payload.project || payload.data || payload;
  if (!isObject(source)) return null;
  const blueprint = source.blueprint || {
    requirements: source.requirements || null,
    architecture: source.architecture || null,
    database: source.database || null,
    api: source.api || null,
    devops: source.devops || null,
    review: source.review || null
  };
  return {
    ...source,
    blueprint,
    summary: source.summary || {},
    known_information: source.known_information || {},
    transcript: Array.isArray(source.transcript) ? source.transcript : [],
    generation: source.generation || payload.generation || {}
  };
}

function projectEndpoint(projectId, suffix = '') {
  return `/api/projects/${encodeURIComponent(projectId)}${suffix}`;
}

function setChatStarted(started) {
  $('#home')?.classList.toggle('started', started);
  document.body.classList.toggle('project-empty', !started);
  if (started) $('#composerWrap')?.style.removeProperty('top');
  syncComposerSpace();
  positionEmptyComposer();
}

function switchView(view) {
  const target = view === 'results' && state.project ? 'results' : 'chat';
  state.currentView = target;
  $('#chatShell')?.classList.toggle('hidden', target !== 'chat');
  $('#workView')?.classList.toggle('show', target === 'results');
  $('#composerWrap')?.classList.toggle('hidden', target !== 'chat');
  $$('[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === target));
  if (target === 'results') renderActiveResult();
  else {
    syncComposerSpace();
    scrollBottom();
  }
}

function setResultsAvailable(available) {
  const button = $('[data-view="results"]');
  if (button) button.disabled = !available;
}

function inputChanged() {
  const prompt = $('#prompt');
  const composer = $('#composer');
  if (!prompt || !composer) return;
  prompt.style.setProperty('overflow-y', 'hidden', 'important');
  composer.classList.remove('multiline');
  prompt.style.setProperty('height', '0px', 'important');
  const multiline = Boolean(prompt.value) && (prompt.scrollHeight > 30 || prompt.value.includes('\n'));
  composer.classList.toggle('multiline', multiline);
  prompt.style.setProperty('height', '0px', 'important');
  prompt.style.setProperty('height', `${Math.max(26, prompt.scrollHeight)}px`, 'important');
  $('#sendBtn')?.classList.toggle('ready', Boolean(prompt.value.trim()) && !state.busy && !state.workflowRunning);
  syncComposerSpace();
  positionEmptyComposer();
}

function positionEmptyComposer() {
  if (!document.body.classList.contains('project-empty')) return;
  requestAnimationFrame(() => {
    const wrap = $('#composerWrap');
    if (!wrap) return;
    const height = wrap.offsetHeight;
    const topbar = Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--top')) || 58;
    const desired = innerHeight * .55 - height / 2;
    const safeTop = Math.max(topbar + 18, Math.min(desired, innerHeight - height - 18));
    wrap.style.setProperty('top', `${safeTop}px`, 'important');
  });
}

function syncComposerSpace() {
  requestAnimationFrame(() => {
    const shell = $('#chatShell');
    const wrap = $('#composerWrap');
    if (!shell || !wrap) return;
    if (document.body.classList.contains('project-empty')) {
      shell.style.paddingBottom = '0px';
      return;
    }
    shell.style.paddingBottom = `${Math.max(120, wrap.offsetHeight + 24)}px`;
  });
}

function scrollBottom() {
  window.setTimeout(() => $('#content')?.scrollTo({ top: $('#content').scrollHeight, behavior: 'smooth' }), 20);
}

function assistantMark() {
  const mark = node('div', 'assistant-mark');
  mark.textContent = 'B2D';
  mark.setAttribute('aria-hidden', 'true');
  return mark;
}

function addUser(text, timestamp) {
  setChatStarted(true);
  const row = node('div', 'msg user');
  const wrap = node('div');
  wrap.append(node('div', 'user-bubble', text));
  if (timestamp) wrap.append(node('div', 'message-meta', formatDate(timestamp)));
  row.append(wrap);
  $('#messages')?.append(row);
  return row;
}

function addAssistant(text, options = {}) {
  setChatStarted(true);
  const row = node('div', 'msg assistant');
  const body = node('div', 'assistant-body');
  body.append(node('div', 'assistant-text', text || ''));
  if (options.notice) body.append(node('div', 'assistant-notice', options.notice));
  if (options.timestamp) body.append(node('div', 'message-meta', formatDate(options.timestamp)));
  const actions = node('div', 'msg-actions');
  const copy = node('button', '', 'Copy');
  copy.type = 'button';
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(text || '');
      showBanner('Response copied.', 'success');
    } catch {
      showBanner('Copy is unavailable in this browser.');
    }
  });
  actions.append(copy);
  body.append(actions);
  row.append(assistantMark(), body);
  $('#messages')?.append(row);
  return row;
}

function addTyping(label = 'The discovery agent is thinking') {
  setChatStarted(true);
  const row = node('div', 'msg assistant typing-row');
  const body = node('div', 'assistant-body');
  const thinking = node('div', 'thinking-label');
  thinking.append(node('span', '', label), node('i'), node('i'), node('i'));
  body.append(thinking);
  row.append(assistantMark(), body);
  $('#messages')?.append(row);
  scrollBottom();
  return row;
}

function renderTranscript(project) {
  const messages = clear($('#messages'));
  const transcript = project?.transcript || [];
  transcript.forEach(turn => {
    const role = String(turn.role || '').toLowerCase();
    const message = turn.message ?? turn.text ?? turn.content ?? '';
    if (role === 'user') addUser(message, turn.timestamp || turn.created_at);
    else addAssistant(userFacingAgentMessage(message), { timestamp: turn.timestamp || turn.created_at });
  });
  if (!transcript.length && project?.business_idea) addUser(project.business_idea, project.created_at);
  setChatStarted(Boolean(project));
  scrollBottom();
  return messages;
}

function renderSidebarState(message, kind = '') {
  const recent = clear($('#recentList'));
  recent?.append(node('div', `sidebar-state ${kind}`.trim(), message));
}

function statusClass(status) {
  return String(status || 'discovery').toLowerCase().replace(/[^a-z0-9_]+/g, '_');
}

function renderProjects() {
  const container = clear($('#recentList'));
  if (!container) return;
  const search = state.search.trim().toLowerCase();
  const projects = state.projects.filter(project => {
    if (!search) return true;
    return `${projectTitle(project)} ${project.status || ''}`.toLowerCase().includes(search);
  });
  if (!projects.length) {
    container.append(node('div', 'sidebar-state', search ? 'No matching projects.' : 'No projects yet. Start with a business idea.'));
    return;
  }
  projects.forEach(project => {
    const button = node('button', `side-row project-row${state.project?.project_id === project.project_id ? ' active' : ''}`);
    button.type = 'button';
    const icon = node('span', 'project-status-dot');
    icon.classList.add(statusClass(project.status));
    const copy = node('span', 'project-row-copy');
    copy.append(node('span', 'project-row-title', projectTitle(project)));
    copy.append(node('span', 'project-row-time', formatDate(project.updated_at)));
    button.append(copy, icon);
    button.title = `${projectTitle(project)} · ${labelFor(project.status || 'discovery')}`;
    button.addEventListener('click', () => openProject(project.project_id));
    container.append(button);
  });
}

async function loadProjects({ quiet = false } = {}) {
  if (!quiet) renderSidebarState('Loading projects…');
  try {
    const payload = await api('/api/projects');
    state.projects = Array.isArray(payload?.projects) ? payload.projects : Array.isArray(payload) ? payload : [];
    state.projects.sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0));
    renderProjects();
  } catch (error) {
    renderSidebarState(readableError(error), 'error');
    if (!quiet) showBanner(readableError(error));
    throw error;
  }
}

async function openProject(projectId, { updateUrl = true } = {}) {
  if (!projectId || state.busy || state.workflowRunning) return;
  setBusy(true);
  hideFollow();
  const typing = addTyping('Loading project');
  try {
    const payload = await api(projectEndpoint(projectId));
    const project = normalizeProject(payload);
    if (!project) throw new Error('The project response was empty.');
    state.project = project;
    state.artifacts = [];
    if (updateUrl) {
      const url = new URL(location.href);
      url.searchParams.set('project', project.project_id);
      history.pushState({ projectId: project.project_id }, '', url);
    }
    renderProject();
    await loadArtifacts({ quiet: true });
    await loadProjects({ quiet: true });
    if (project.status === 'generating') showBanner('This project has an unfinished generation. Use Resume generation to continue.', 'success');
  } catch (error) {
    typing?.remove();
    showBanner(readableError(error));
  } finally {
    setBusy(false);
  }
}

function resetProject({ updateUrl = true } = {}) {
  state.project = null;
  state.artifacts = [];
  state.follow = null;
  state.activeTab = 'overview';
  state.workflowRunning = false;
  state.stageVisualProjectId = null;
  state.stageVisualStates = null;
  state.generationActivityPhase = 'idle';
  clear($('#messages'));
  hideFollow();
  renderActionBar();
  setResultsAvailable(false);
  $('#projectTitleChip').textContent = 'New project';
  $('#prompt').value = '';
  $('#prompt').placeholder = 'Describe your business idea';
  setChatStarted(false);
  switchView('chat');
  if (updateUrl) {
    const url = new URL(location.href);
    url.searchParams.delete('project');
    history.pushState({}, '', url);
  }
  renderProjects();
  inputChanged();
  $('#prompt')?.focus();
}

function renderProject() {
  const project = state.project;
  if (!project) {
    resetProject({ updateUrl: false });
    return;
  }
  renderTranscript(project);
  $('#projectTitleChip').textContent = projectTitle(project);
  $('#prompt').placeholder = project.status === 'discovery' || project.status === 'ready_for_confirmation'
    ? 'Answer the discovery agent'
    : 'This blueprint is ready to review';
  renderDiscovery(project.discovery);
  renderActionBar();
  renderProgress();
  renderResultTabs();
  renderResultsHeader();
  renderActiveResult();
  const hasBlueprint = Object.values(project.blueprint || {}).some(hasValue) || TERMINAL_STATUSES.has(project.status) || project.status === 'generating';
  setResultsAvailable(hasBlueprint);
  $('#shareBtn')?.toggleAttribute('hidden', !project.project_id);
  renderProjects();
}

function hideFollow(clearState = true) {
  $('#followup')?.classList.remove('show');
  $('#followWaiting')?.classList.remove('show');
  $('#followResume')?.classList.remove('show');
  clear($('#followOptions'));
  $('#customAnswer')?.classList.remove('show');
  $('#textAnswer')?.classList.remove('show');
  if (clearState) state.follow = null;
  syncComposerSpace();
}

function renderDiscovery(discovery) {
  hideFollow();
  const questions = Array.isArray(discovery?.questions) ? discovery.questions : [];
  if (!questions.length || discovery?.status === 'ready') return;
  state.follow = { questions, answers: {}, index: 0, discovery, paused: false };
  renderFollowQuestion();
}

function questionIsRequired(question) {
  const missing = state.follow?.discovery?.missing_information || [];
  const record = missing.find(item => item.field === question.id || item.field === question.field);
  return record?.importance === 'critical';
}

function renderFollowQuestion() {
  const follow = state.follow;
  if (!follow || follow.paused) return;
  const question = follow.questions[follow.index];
  if (!question) return;
  $('#followup')?.classList.add('show');
  $('#followWaiting')?.classList.add('show');
  $('#followResume')?.classList.remove('show');
  $('#followTitle').textContent = question.question || question.title || 'A quick question';
  $('#followSub').textContent = question.reason || '';
  $('#followSub').style.display = question.reason ? 'block' : 'none';
  $('#followProgress').textContent = `${follow.index + 1} of ${follow.questions.length}`;
  $('#followPrev').disabled = follow.index === 0;
  $('#followNext').disabled = follow.index >= follow.questions.length - 1;
  $('#followSkip').style.display = questionIsRequired(question) ? 'none' : 'inline-flex';
  $('#followContinue').style.display = 'inline-flex';
  $('#followContinue').textContent = follow.index === follow.questions.length - 1 ? 'Send answers' : 'Continue';
  $('#followHint').textContent = questionIsRequired(question) ? 'Required' : 'Choose or write an answer';
  $('#customAnswer')?.classList.remove('show');
  const options = Array.isArray(question.options) ? question.options : [];
  const optionsBox = clear($('#followOptions'));
  const answerKey = question.id || `question_${follow.index}`;
  options.forEach((option, index) => {
    const button = node('button', 'choice');
    button.type = 'button';
    button.append(node('span', 'num', index + 1), node('span', 'choice-label', option), node('span', 'tick', '✓'));
    button.classList.toggle('selected', follow.answers[answerKey] === option);
    button.addEventListener('click', () => {
      follow.answers[answerKey] = option;
      renderFollowQuestion();
    });
    optionsBox.append(button);
  });
  if (options.length) {
    const custom = node('button', 'choice');
    custom.type = 'button';
    custom.append(node('span', 'num', options.length + 1), node('span', 'choice-label', 'Write another answer'), node('span', 'tick', '✓'));
    custom.addEventListener('click', () => {
      $('#customAnswerInput').value = follow.answers[answerKey] || '';
      $('#customAnswer')?.classList.add('show');
      $('#customAnswerInput')?.focus();
    });
    optionsBox.append(custom);
    $('#textAnswer')?.classList.remove('show');
  } else {
    $('#textAnswer')?.classList.add('show');
    $('#textAnswerInput').value = follow.answers[answerKey] || '';
    $('#textAnswerInput').placeholder = 'Type your answer…';
  }
  updateFollowControls();
  syncComposerSpace();
}

function currentFollowAnswer() {
  if (!state.follow) return '';
  const question = state.follow.questions[state.follow.index];
  const key = question.id || `question_${state.follow.index}`;
  if (!question.options?.length) return $('#textAnswerInput')?.value.trim() || state.follow.answers[key] || '';
  return state.follow.answers[key] || '';
}

function saveCurrentFollowAnswer() {
  if (!state.follow) return false;
  const question = state.follow.questions[state.follow.index];
  const key = question.id || `question_${state.follow.index}`;
  const value = currentFollowAnswer();
  if (!value) return false;
  state.follow.answers[key] = value;
  return true;
}

function updateFollowControls() {
  const hasAnswer = Boolean(currentFollowAnswer());
  if ($('#followContinue')) $('#followContinue').disabled = !hasAnswer;
  if ($('#followNext')) $('#followNext').disabled = state.follow?.index >= state.follow?.questions.length - 1 || !hasAnswer;
}

async function advanceFollow() {
  if (!saveCurrentFollowAnswer()) {
    showBanner('Answer this question before continuing.');
    return;
  }
  if (state.follow.index < state.follow.questions.length - 1) {
    state.follow.index += 1;
    renderFollowQuestion();
    return;
  }
  await submitFollowAnswers();
}

async function submitFollowAnswers() {
  if (!state.project || !state.follow) return;
  const message = state.follow.questions.map((question, index) => {
    const key = question.id || `question_${index}`;
    return `${question.question || question.title}: ${state.follow.answers[key] || 'Not specified'}`;
  }).join('\n');
  hideFollow(false);
  await sendDiscoveryMessage(message);
}

function pauseFollow() {
  if (!state.follow) return;
  saveCurrentFollowAnswer();
  state.follow.paused = true;
  $('#followup')?.classList.remove('show');
  $('#followWaiting')?.classList.remove('show');
  $('#followResume')?.classList.add('show');
  syncComposerSpace();
}

function resumeFollow() {
  if (!state.follow) return;
  state.follow.paused = false;
  renderFollowQuestion();
}

function skipFollow() {
  if (!state.follow) return;
  const question = state.follow.questions[state.follow.index];
  if (questionIsRequired(question)) return;
  const key = question.id || `question_${state.follow.index}`;
  state.follow.answers[key] = 'Not specified';
  advanceFollow();
}

function actionButton(label, handler, primary = false) {
  const button = node('button', primary ? 'primary' : '', label);
  button.type = 'button';
  button.disabled = state.busy || state.workflowRunning;
  button.addEventListener('click', handler);
  return button;
}

function isDiscoveryReadyForConfirmation(project) {
  if (!project) return false;
  // The top-level project status is the canonical lifecycle state. The stored
  // discovery snapshot intentionally remains `ready` after confirmation, so it
  // must only be used as a legacy fallback when no lifecycle status exists.
  if (project.status) return project.status === 'ready_for_confirmation';
  return project.discovery?.status === 'ready';
}

function discoveryHasBeenConfirmed(project) {
  return Boolean(project) && (
    project.status === 'confirmed'
    || project.status === 'generating'
    || TERMINAL_STATUSES.has(project.status)
  );
}

function renderActionBar() {
  const bar = $('#projectActionBar');
  if (!bar) return;
  bar.classList.remove('show');
  const title = bar.querySelector('strong');
  const description = bar.querySelector('span');
  const actions = clear(bar.querySelector('.project-action-buttons'));
  const project = state.project;
  if (!project) return;

  const discoveryReady = isDiscoveryReadyForConfirmation(project);
  if (discoveryReady) {
    title.textContent = 'Discovery is ready for your approval';
    description.textContent = 'Review the known information, then confirm it before the engineering agents start.';
    actions.append(actionButton('Review information', () => { state.activeTab = 'overview'; switchView('results'); }), actionButton('Confirm information', confirmDiscovery, true));
    bar.classList.add('show');
  } else if (project.status === 'confirmed') {
    title.textContent = 'Ready to generate the blueprint';
    description.textContent = 'The six engineering and review stages will run one at a time with visible progress.';
    actions.append(actionButton('Generate blueprint', () => runGeneration({ initialize: true }), true));
    bar.classList.add('show');
  } else if (project.status === 'generating') {
    title.textContent = state.workflowRunning ? 'Generating your blueprint' : 'Generation is waiting to continue';
    description.textContent = state.workflowRunning ? 'Keep this page open while each stage completes.' : 'Resume from the next unfinished agent stage.';
    if (!state.workflowRunning) actions.append(actionButton('Resume generation', () => runGeneration({ initialize: false }), true));
    actions.append(actionButton('View progress', () => switchView('results')));
    bar.classList.add('show');
  } else if (TERMINAL_STATUSES.has(project.status)) {
    title.textContent = project.status === 'needs_attention' ? 'Blueprint needs review' : 'Blueprint is ready';
    description.textContent = project.status === 'needs_attention' ? 'The reviewer found issues that need attention. Open the Review tab for details.' : 'Explore every agent output and download the generated files.';
    actions.append(actionButton(project.status === 'needs_attention' ? 'Review issues' : 'Open blueprint', () => {
      if (project.status === 'needs_attention') state.activeTab = 'review';
      switchView('results');
    }, true));
    bar.classList.add('show');
  }
  syncComposerSpace();
}

async function createProject(businessIdea) {
  const typing = addTyping('Starting discovery');
  setBusy(true);
  try {
    const payload = await idempotentApi(
      '/api/projects',
      { method: 'POST', body: { business_idea: businessIdea } },
      `project.create:${businessIdea}`
    );
    const project = normalizeProject(payload);
    if (!project) throw new Error('The server did not return the new project.');
    state.project = project;
    state.artifacts = [];
    const url = new URL(location.href);
    url.searchParams.set('project', project.project_id);
    history.pushState({ projectId: project.project_id }, '', url);
    renderProject();
    await loadProjects({ quiet: true });
  } catch (error) {
    typing?.remove();
    addAssistant('I could not start this project.', { notice: readableError(error) });
    showBanner(readableError(error));
  } finally {
    setBusy(false);
  }
}

async function sendDiscoveryMessage(message) {
  if (!state.project) return createProject(message);
  if (!['discovery', 'ready_for_confirmation'].includes(state.project.status)) {
    showBanner('Discovery is closed for this project. Open the blueprint or start a new project.');
    return;
  }
  const typing = addTyping('Updating project discovery');
  setBusy(true);
  try {
    const payload = await idempotentApi(
      projectEndpoint(state.project.project_id, '/discovery/message'),
      { method: 'POST', body: { message } },
      `discovery.message:${state.project.project_id}:${message}`
    );
    const project = normalizeProject(payload);
    if (!project) throw new Error('The discovery response was empty.');
    state.project = project;
    renderProject();
    await loadProjects({ quiet: true });
  } catch (error) {
    typing?.remove();
    addAssistant('The discovery message could not be processed.', { notice: readableError(error) });
    showBanner(readableError(error));
  } finally {
    setBusy(false);
  }
}

async function sendPrompt() {
  const prompt = $('#prompt');
  const message = prompt?.value.trim();
  if (!message || state.busy || state.workflowRunning) return;
  prompt.value = '';
  inputChanged();
  if (!state.project) {
    addUser(message);
    await createProject(message);
    return;
  }
  await sendDiscoveryMessage(message);
}

async function confirmDiscovery() {
  if (!state.project || state.busy || state.workflowRunning) return;
  setBusy(true);
  renderActionBar();
  try {
    const payload = await api(
      projectEndpoint(state.project.project_id, '/discovery/confirm'),
      { method: 'POST', expectJsonObject: true }
    );
    const project = normalizeProject(payload);
    if (!project) throw new TypeError('The server returned an incomplete confirmation response.');
    state.project = project;
    renderProject();
    showBanner('Discovery confirmed. The blueprint is ready to generate.', 'success');
    await loadProjects({ quiet: true });
  } catch (error) {
    // A serverless response can be lost after Supabase has committed the
    // confirmation. Re-read the canonical project before showing a retry error.
    try {
      await refreshActiveProject();
    } catch { /* keep the original error when reconciliation is unavailable */ }
    if (discoveryHasBeenConfirmed(state.project)) {
      renderProject();
      showBanner('Discovery is confirmed. Continuing from the current project state.', 'success');
      await loadProjects({ quiet: true }).catch(() => {});
      return;
    }
    showBanner(readableError(error));
  } finally {
    setBusy(false);
    renderActionBar();
  }
}

function stageId(value) {
  const normalized = String(value || '').toLowerCase().replace(/[\s-]+/g, '_');
  if (normalized.includes('discover')) return 'discovery';
  if (normalized.includes('require')) return 'requirements';
  if (normalized.includes('architect')) return 'architecture';
  if (normalized.includes('database') || normalized === 'db') return 'database';
  if (normalized === 'api' || normalized.includes('api_')) return 'api';
  if (normalized.includes('devops') || normalized.includes('deploy')) return 'devops';
  if (normalized.includes('review')) return 'review';
  return normalized;
}

function inferStageStates(project = state.project, event = null) {
  const result = Object.fromEntries(STAGES.map(stage => [stage.id, 'pending']));
  if (!project) return result;
  if (project.status !== 'discovery') result.discovery = 'done';
  const blueprint = project.blueprint || {};
  ['requirements', 'architecture', 'database', 'api', 'devops', 'review'].forEach(id => {
    if (hasValue(blueprint[id])) result[id] = 'done';
  });
  const generation = project.generation || {};
  const completed = generation.completed_agents || generation.completed_stages || generation.completed || event?.completed_agents || event?.completed_stages || [];
  if (Array.isArray(completed)) completed.map(stageId).forEach(id => { if (id in result) result[id] = 'done'; });
  const failed = generation.failed_stages || event?.failed_stages || [];
  if (Array.isArray(failed)) failed.map(stageId).forEach(id => { if (id in result) result[id] = 'failed'; });
  const currentValue = event?.next_stage || generation.next_stage || event?.current_stage || event?.agent || generation.current_stage || generation.stage || event?.stage;
  const current = stageId(currentValue);
  if (current in result && result[current] !== 'done') result[current] = event?.status === 'failed' ? 'failed' : 'running';
  if (String(currentValue || '').toLowerCase().includes('database_api')) {
    if (result.database !== 'done') result.database = 'running';
    if (result.api !== 'done') result.api = 'running';
  }
  if (project.status === 'generating' && !Object.values(result).includes('running')) {
    const next = STAGES.find(stage => result[stage.id] === 'pending');
    if (next) result[next.id] = 'running';
  }
  return result;
}

function progressMessage(project = state.project, event = null) {
  if (!project) return 'Start with discovery.';
  if (state.workflowRunning) {
    const stageValue = event?.next_stage || project.generation?.next_stage || event?.current_stage || event?.agent || project.generation?.current_stage || project.generation?.stage || event?.stage;
    if (String(stageValue || '').toLowerCase().includes('database_api')) return 'Database and API agents are running…';
    const stage = stageId(stageValue);
    const label = STAGES.find(item => item.id === stage)?.label;
    return label ? `${label} agent is running…` : 'The next agent stage is running…';
  }
  if (project.status === 'approved') return 'All seven stages completed and the blueprint passed review.';
  if (project.status === 'revised') return 'The blueprint completed after a targeted revision.';
  if (project.status === 'needs_attention') return 'Generation completed with review issues that need attention.';
  if (project.status === 'confirmed') return 'Discovery is confirmed. Generation has not started yet.';
  if (project.status === 'ready_for_confirmation') return 'Discovery is ready for confirmation.';
  if (project.status === 'generating') return 'Generation can resume from the next unfinished stage.';
  return 'Discovery is gathering the information the engineering agents need.';
}

function buildStageStrip() {
  const strip = node('div', 'stage-strip');
  strip.setAttribute('role', 'list');
  strip.setAttribute('aria-label', 'Seven-agent workflow checkpoints');
  STAGES.forEach((stage, index) => {
    const item = node('div', 'stage-pill pending');
    item.dataset.stage = stage.id;
    item.setAttribute('role', 'listitem');
    const dot = node('div', 'stage-dot');
    dot.append(
      node('span', 'checkpoint-number', index + 1),
      node('span', 'checkpoint-check', '✓')
    );
    item.append(dot, node('span', 'stage-label', stage.label));
    strip.append(item);
  });
  return strip;
}

function animateCheckpoint(item, className) {
  item.classList.remove('just-completed', 'just-started', 'just-failed');
  void item.offsetWidth;
  item.classList.add(className);
  window.setTimeout(() => item.classList.remove(className), 950);
}

function updateStageStrip(strip, states, previousStates = null) {
  STAGES.forEach(stage => {
    const item = strip.querySelector(`[data-stage="${stage.id}"]`);
    if (!item) return;
    const current = states[stage.id] || 'pending';
    const previous = item.dataset.state || previousStates?.[stage.id] || current;
    item.classList.remove(...STAGE_STATE_CLASSES);
    item.classList.add(current);
    item.dataset.state = current;
    item.title = `${stage.label}: ${labelFor(current)}`;
    item.setAttribute('aria-label', `${stage.label}: ${labelFor(current)}`);
    if (previous !== current) {
      if (current === 'done') animateCheckpoint(item, 'just-completed');
      else if (current === 'running') animateCheckpoint(item, 'just-started');
      else if (current === 'failed') animateCheckpoint(item, 'just-failed');
    }
  });
  strip.dataset.completed = String(Object.values(states).filter(value => value === 'done').length);
}

function stageLabels(ids) {
  return ids.map(id => STAGES.find(stage => stage.id === id)?.label || labelFor(id));
}

function workflowActivity(states, event = null) {
  const project = state.project;
  const completed = STAGES.filter(stage => states[stage.id] === 'done');
  const running = STAGES.filter(stage => states[stage.id] === 'running').map(stage => stage.id);
  const completedNow = Array.isArray(event?.completed_now)
    ? event.completed_now.map(stageId).filter(id => STAGES.some(stage => stage.id === id))
    : [];
  const counter = `${completed.length} of ${STAGES.length} checkpoints saved`;

  if (!project) {
    return { tone: 'waiting', badge: 'Waiting', title: 'Start with your business idea', detail: STAGE_ACTIVITY.discovery, counter };
  }
  if (TERMINAL_STATUSES.has(project.status)) {
    return {
      tone: project.status === 'needs_attention' ? 'failed' : 'complete',
      badge: project.status === 'needs_attention' ? 'Review' : 'Ready',
      title: project.status === 'needs_attention' ? 'The review needs attention' : 'The blueprint is ready',
      detail: project.status === 'needs_attention'
        ? 'The workflow stopped safely with review findings available in the Review tab.'
        : 'Every agent checkpoint is saved and the cross-artifact review is complete.',
      counter
    };
  }
  if (!state.workflowRunning && project.status === 'generating') {
    return {
      tone: state.generationActivityPhase === 'failed' ? 'failed' : 'waiting',
      badge: state.generationActivityPhase === 'failed' ? 'Paused' : 'Resume',
      title: state.generationActivityPhase === 'failed' ? 'The current checkpoint paused safely' : 'Generation is waiting to continue',
      detail: 'The saved workflow can resume from the next unfinished agent without repeating completed checkpoints.',
      counter
    };
  }
  if (state.generationActivityPhase === 'starting') {
    return { tone: 'working', badge: 'Starting', title: 'Preparing the agent workflow', detail: 'Loading the confirmed discovery context and opening the first engineering checkpoint.', counter };
  }
  if (state.generationActivityPhase === 'transitioning' && completedNow.length) {
    const finished = stageLabels(completedNow).join(' + ');
    const next = stageLabels(running).join(' + ');
    return {
      tone: 'transitioning',
      badge: 'Saved',
      title: `${finished} checkpoint saved`,
      detail: next ? `Moving to ${next}. The next agent receives the saved upstream output.` : 'Moving to the final workflow state.',
      counter
    };
  }
  if (running.length) {
    const labels = stageLabels(running);
    return {
      tone: 'working',
      badge: 'Live',
      title: `${labels.join(' + ')} ${running.length > 1 ? 'agents' : 'agent'}`,
      detail: running.map(id => STAGE_ACTIVITY[id]).join(' '),
      counter
    };
  }
  if (project.status === 'confirmed') {
    return { tone: 'waiting', badge: 'Ready', title: 'Waiting to generate', detail: 'Discovery is confirmed. Start generation when you are ready.', counter };
  }
  if (project.status === 'ready_for_confirmation') {
    return { tone: 'waiting', badge: 'Confirm', title: 'Discovery checkpoint is ready', detail: 'Review the known information and confirm it before engineering begins.', counter };
  }
  return { tone: 'working', badge: 'Live', title: 'Discovery agent', detail: STAGE_ACTIVITY.discovery, counter };
}

function renderAgentActivity(container, states, event = null) {
  const activity = workflowActivity(states, event);
  clear(container);
  container.className = `agent-activity ${activity.tone}`;
  container.setAttribute('aria-live', 'polite');

  const head = node('div', 'activity-head');
  const label = node('div', 'activity-label');
  label.append(node('i', 'activity-signal'), node('span', '', 'Agent activity'));
  head.append(label, node('span', 'activity-badge', activity.badge));

  const copy = node('div', 'activity-copy');
  copy.append(node('strong', '', activity.title), node('p', '', activity.detail));

  const motion = node('div', 'activity-motion');
  motion.setAttribute('aria-hidden', 'true');
  motion.append(node('i'), node('i'), node('i'));

  container.append(head, copy, motion, node('div', 'activity-counter', activity.counter));
}

function ensureProgressSurface(container, { chat = false } = {}) {
  let layout = container.querySelector('.workflow-progress-layout');
  if (layout) {
    return {
      copy: layout.querySelector('[data-progress-copy]'),
      status: layout.querySelector('[data-progress-status]'),
      strip: layout.querySelector('.stage-strip'),
      activity: layout.querySelector('.agent-activity')
    };
  }

  clear(container);
  layout = node('div', 'workflow-progress-layout');
  const main = node('div', 'workflow-progress-main');
  if (chat) {
    const head = node('div', 'generation-head');
    const copy = node('span');
    copy.dataset.progressCopy = '';
    head.append(node('strong', '', 'Seven-agent progress'), copy);
    main.append(head);
  } else {
    const message = node('div', 'progress-message');
    const copy = node('span');
    copy.dataset.progressCopy = '';
    const status = node('span');
    status.dataset.progressStatus = '';
    message.append(copy, status);
    main.append(message);
  }
  main.append(buildStageStrip());
  const activity = node('aside', 'agent-activity');
  layout.append(main, activity);
  container.append(layout);
  return {
    copy: layout.querySelector('[data-progress-copy]'),
    status: layout.querySelector('[data-progress-status]'),
    strip: layout.querySelector('.stage-strip'),
    activity
  };
}

function updateProgressSurface(container, states, previousStates, event, options = {}) {
  const surface = ensureProgressSurface(container, options);
  surface.copy.textContent = progressMessage(state.project, event);
  if (surface.status) surface.status.textContent = state.project ? labelFor(state.project.status || 'discovery') : 'New project';
  updateStageStrip(surface.strip, states, previousStates);
  renderAgentActivity(surface.activity, states, event);
}

function renderProgress(event = null) {
  const container = $('#resultsProgress');
  if (!container) return;
  const projectId = state.project?.project_id || null;
  const sameProject = projectId && state.stageVisualProjectId === projectId;
  const previousStates = sameProject ? state.stageVisualStates : null;
  const states = inferStageStates(state.project, event);
  updateProgressSurface(container, states, previousStates, event);

  let workspace = $('#generationWorkspace');
  if (state.project?.status === 'generating' || state.workflowRunning) {
    if (!workspace) {
      workspace = node('div', 'generation-workspace');
      workspace.id = 'generationWorkspace';
      const chatCard = node('div', 'generation-card');
      chatCard.id = 'generationCard';
      workspace.append(chatCard);
      $('#messages')?.append(workspace);
    }
    updateProgressSurface($('#generationCard'), states, previousStates, event, { chat: true });
    scrollBottom();
  } else {
    workspace?.remove();
  }

  state.stageVisualProjectId = projectId;
  state.stageVisualStates = { ...states };
}

function eventContainsProject(payload) {
  const candidate = payload?.project || payload?.data || payload;
  return isObject(candidate) && Boolean(candidate.project_id) && (candidate.blueprint || candidate.transcript || candidate.business_idea);
}

function generationComplete(payload) {
  return payload?.complete === true || payload?.generation?.complete === true || payload?.project?.generation?.complete === true;
}

async function refreshActiveProject() {
  if (!state.project?.project_id) return;
  const payload = await api(projectEndpoint(state.project.project_id));
  const project = normalizeProject(payload);
  if (project) state.project = project;
}

async function runGeneration({ initialize }) {
  if (!state.project || state.workflowRunning || state.busy) return;
  state.workflowRunning = true;
  state.generationActivityPhase = initialize ? 'starting' : 'working';
  setBusy(true);
  switchView('results');
  renderActionBar();
  renderProgress();
  let lastEvent = null;
  try {
    if (initialize) {
      lastEvent = await api(projectEndpoint(state.project.project_id, '/generate'), { method: 'POST' });
      if (eventContainsProject(lastEvent)) state.project = normalizeProject(lastEvent);
      else state.project = { ...state.project, status: 'generating', generation: lastEvent?.generation || state.project.generation };
      state.generationActivityPhase = 'working';
      renderProject();
      switchView('results');
    }

    let complete = generationComplete(lastEvent);
    for (let step = 0; !complete && step < 24; step += 1) {
      const stageScope = state.project?.generation?.next_stage || `step-${step}`;
      state.generationActivityPhase = 'working';
      renderProgress(lastEvent);
      lastEvent = await idempotentApi(
        projectEndpoint(state.project.project_id, '/generation/next'),
        { method: 'POST' },
        `generation.next:${state.project.project_id}:${stageScope}`
      );
      if (eventContainsProject(lastEvent)) state.project = normalizeProject(lastEvent);
      else await refreshActiveProject();
      complete = generationComplete(lastEvent) || TERMINAL_STATUSES.has(state.project?.status);
      state.generationActivityPhase = complete ? 'complete' : 'transitioning';
      renderProject();
      switchView('results');
      renderProgress(lastEvent);
      await sleep(650);
    }
    if (!complete) throw new Error('Generation paused after the safety limit. You can resume it from this project.');
    await refreshActiveProject();
    await loadArtifacts({ quiet: true });
    renderProject();
    switchView('results');
    showBanner(state.project.status === 'needs_attention' ? 'Blueprint generated with review issues.' : 'Blueprint generation completed.', state.project.status === 'needs_attention' ? 'error' : 'success');
  } catch (error) {
    state.generationActivityPhase = 'failed';
    try { await refreshActiveProject(); } catch { /* preserve the last visible state */ }
    renderProject();
    switchView('results');
    showBanner(readableError(error), 'error', true);
  } finally {
    state.workflowRunning = false;
    if (state.generationActivityPhase !== 'failed') {
      state.generationActivityPhase = TERMINAL_STATUSES.has(state.project?.status) ? 'complete' : 'idle';
    }
    setBusy(false);
    renderActionBar();
    renderProgress(lastEvent);
    await loadProjects({ quiet: true }).catch(() => {});
  }
}

function tabHasData(tabId) {
  if (!state.project) return false;
  if (tabId === 'overview') return true;
  if (tabId === 'files') return state.artifacts.length > 0 || TERMINAL_STATUSES.has(state.project.status);
  return hasValue(state.project.blueprint?.[tabId]);
}

function renderResultTabs() {
  const container = clear($('#resultTabs'));
  if (!container) return;
  RESULT_TABS.forEach(tab => {
    const button = node('button', `result-tab${state.activeTab === tab.id ? ' active' : ''}`, tab.label);
    button.type = 'button';
    button.role = 'tab';
    button.dataset.tab = tab.id;
    button.disabled = Boolean(state.project) && !tabHasData(tab.id) && !['overview', 'files'].includes(tab.id);
    button.setAttribute('aria-selected', String(state.activeTab === tab.id));
    button.addEventListener('click', () => {
      state.activeTab = tab.id;
      renderResultTabs();
      renderActiveResult();
    });
    container.append(button);
  });
}

function renderResultsHeader() {
  const project = state.project;
  $('#resultsTitle').textContent = project ? projectTitle(project) : 'Project blueprint';
  $('#resultsSubtitle').textContent = project?.business_idea || 'Generated by seven coordinated AI agents.';
  const badge = $('#resultsStatus');
  if (badge) {
    badge.textContent = labelFor(project?.status || 'discovery');
    badge.className = `status-badge ${statusClass(project?.status)}`;
  }
}

function resultEmpty(message) {
  return node('div', 'result-empty', message);
}

function resultGrid() {
  return node('div', 'result-grid');
}

function resultCard(title, { full = false } = {}) {
  const card = node('section', `result-card${full ? ' full' : ''}`);
  if (title) card.append(node('h3', '', title));
  return card;
}

function appendTextCard(grid, title, value, options = {}) {
  if (!hasValue(value)) return;
  const card = resultCard(title, options);
  card.append(node('p', '', textValue(value)));
  grid.append(card);
}

function appendListCard(grid, title, values, options = {}) {
  if (!Array.isArray(values) || !values.length) return;
  const card = resultCard(title, options);
  const list = node('ul', 'result-list');
  values.forEach(value => {
    const item = node('li');
    if (isObject(value)) item.textContent = Object.entries(value).map(([key, itemValue]) => `${labelFor(key)}: ${textValue(itemValue)}`).join(' · ');
    else item.textContent = textValue(value);
    list.append(item);
  });
  card.append(list);
  grid.append(card);
}

function appendKeyValues(container, values, { redactSecrets = false } = {}) {
  const list = node('div', 'kv-list');
  Object.entries(values || {}).forEach(([key, value]) => {
    if (!hasValue(value)) return;
    const row = node('div', 'kv-row');
    row.append(node('div', 'kv-key', labelFor(key)));
    let display = textValue(value);
    if (redactSecrets && /secret|password|token|credential|private.?key|api.?key/i.test(key)) display = 'Server secret (hidden)';
    row.append(node('div', 'kv-value', display));
    list.append(row);
  });
  if (!list.children.length) list.append(node('div', 'muted', 'No information available.'));
  container.append(list);
}

function appendSyntaxToken(container, value, className = '') {
  if (!value) return;
  if (!className) {
    container.append(document.createTextNode(value));
    return;
  }
  container.append(node('span', className, value));
}

function appendInlineCodeTokens(container, value) {
  const source = String(value || '');
  const pattern = /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|#[^\n]*|\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*|--[A-Za-z0-9][A-Za-z0-9_-]*|\b(?:true|false|null|yes|no|on|off)\b|\b\d+(?:\.\d+)*\b|&&|\|\||\\$)/gi;
  let cursor = 0;
  for (const match of source.matchAll(pattern)) {
    appendSyntaxToken(container, source.slice(cursor, match.index));
    const token = match[0];
    let className = 'syntax-operator';
    if (token.startsWith('#')) className = 'syntax-comment';
    else if (token.startsWith('"') || token.startsWith("'")) className = 'syntax-string';
    else if (token.startsWith('$')) className = 'syntax-variable';
    else if (token.startsWith('--')) className = 'syntax-flag';
    else if (/^\d/.test(token)) className = 'syntax-number';
    else if (/^(?:true|false|null|yes|no|on|off)$/i.test(token)) className = 'syntax-boolean';
    appendSyntaxToken(container, token, className);
    cursor = match.index + token.length;
  }
  appendSyntaxToken(container, source.slice(cursor));
}

function highlightedDockerfile(content) {
  const code = node('code', 'syntax-code language-dockerfile');
  String(content ?? '').split('\n').forEach((line, index, lines) => {
    if (/^\s*#/.test(line)) {
      appendSyntaxToken(code, line, 'syntax-comment');
    } else {
      const match = line.match(/^(\s*)([A-Za-z]+)(\s*)(.*)$/);
      const instruction = match?.[2]?.toUpperCase();
      if (match && DOCKER_INSTRUCTIONS.has(instruction)) {
        appendSyntaxToken(code, match[1]);
        appendSyntaxToken(code, match[2], 'syntax-instruction');
        appendSyntaxToken(code, match[3]);
        appendInlineCodeTokens(code, match[4]);
      } else {
        appendInlineCodeTokens(code, line);
      }
    }
    if (index < lines.length - 1) code.append(document.createTextNode('\n'));
  });
  return code;
}

function highlightedYaml(content) {
  const code = node('code', 'syntax-code language-yaml');
  String(content ?? '').split('\n').forEach((line, index, lines) => {
    if (/^\s*#/.test(line)) {
      appendSyntaxToken(code, line, 'syntax-comment');
    } else {
      const match = line.match(/^(\s*)(-\s+)?([A-Za-z0-9_.-]+)(\s*:)(.*)$/);
      if (match) {
        appendSyntaxToken(code, match[1]);
        appendSyntaxToken(code, match[2] || '', 'syntax-operator');
        appendSyntaxToken(code, match[3], 'syntax-key');
        appendSyntaxToken(code, match[4], 'syntax-operator');
        appendInlineCodeTokens(code, match[5]);
      } else {
        appendInlineCodeTokens(code, line);
      }
    }
    if (index < lines.length - 1) code.append(document.createTextNode('\n'));
  });
  return code;
}

function highlightedCode(content, language) {
  if (language === 'dockerfile') return highlightedDockerfile(content);
  if (language === 'yaml') return highlightedYaml(content);
  return node('code', `syntax-code language-${language || 'plain'}`, String(content ?? ''));
}

function codeBlock(content, label = 'Code', { language = 'plain' } = {}) {
  const block = node('div', 'code-block');
  const toolbar = node('div', 'code-toolbar');
  toolbar.append(node('span', '', label));
  const copy = node('button', '', 'Copy');
  copy.type = 'button';
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(String(content ?? ''));
      copy.textContent = 'Copied';
      window.setTimeout(() => { copy.textContent = 'Copy'; }, 1200);
    } catch {
      showBanner('Copy is unavailable in this browser.');
    }
  });
  toolbar.append(copy);
  const pre = node('pre');
  pre.append(highlightedCode(content, language));
  block.append(toolbar, pre);
  return block;
}

function normalizeMermaidSource(value) {
  let source = String(value || '').trim();
  source = source.replace(/^```(?:mermaid)?\s*/i, '').replace(/\s*```$/i, '').trim();
  const lines = source.split(/\r?\n/);
  if (lines[0]?.trim().toLowerCase() === 'mermaid') lines.shift();
  return lines.join('\n').trim();
}

function loadMermaidRenderer() {
  if (!mermaidRendererPromise) {
    mermaidRendererPromise = import(MERMAID_ESM_URL).then(module => {
      const mermaid = module.default || module;
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        theme: 'base',
        fontFamily: 'Segoe UI, Arial, sans-serif',
        flowchart: { htmlLabels: false, useMaxWidth: true },
        themeVariables: {
          background: '#ffffff',
          primaryColor: '#eef9fc',
          primaryTextColor: '#084c75',
          primaryBorderColor: '#00a2c8',
          secondaryColor: '#fff5d9',
          secondaryTextColor: '#084c75',
          secondaryBorderColor: '#ffca33',
          tertiaryColor: '#fbf0f6',
          tertiaryTextColor: '#084c75',
          tertiaryBorderColor: '#9e1f63',
          lineColor: '#084c75',
          edgeLabelBackground: '#ffffff',
          clusterBkg: '#f4f8fa',
          clusterBorder: '#bfd2dd'
        }
      });
      return mermaid;
    }).catch(error => {
      mermaidRendererPromise = null;
      throw error;
    });
  }
  return mermaidRendererPromise;
}

function safeMermaidSvg(svgText, label) {
  const parsed = new DOMParser().parseFromString(svgText, 'image/svg+xml');
  if (parsed.querySelector('parsererror')) throw new Error('Invalid Mermaid SVG');
  const svg = parsed.documentElement;
  if (svg.localName !== 'svg' || svg.namespaceURI !== 'http://www.w3.org/2000/svg') throw new Error('Unexpected Mermaid output');

  // Mermaid may use foreignObject for node labels even when htmlLabels is
  // disabled. Rebuild those labels from plain text so generated markup cannot
  // execute while the diagram remains readable.
  svg.querySelectorAll('foreignObject').forEach(element => {
    const text = String(element.textContent || '').replace(/\s+/g, ' ').trim();
    while (element.firstChild) element.firstChild.remove();
    const safeLabel = parsed.createElementNS('http://www.w3.org/1999/xhtml', 'div');
    safeLabel.setAttribute('class', 'safe-mermaid-label');
    safeLabel.textContent = text;
    element.append(safeLabel);
  });
  svg.querySelectorAll('script, iframe, object, embed, audio, video').forEach(element => element.remove());
  svg.querySelectorAll('style').forEach(style => {
    if (/@import|url\s*\(\s*['"]?(?:https?:|data:|javascript:)/i.test(style.textContent || '')) style.remove();
  });
  [svg, ...svg.querySelectorAll('*')].forEach(element => {
    [...element.attributes].forEach(attribute => {
      const name = attribute.name.toLowerCase();
      const value = attribute.value.trim();
      if (name.startsWith('on') || /javascript:|data:text\/html|expression\s*\(/i.test(value)) {
        element.removeAttribute(attribute.name);
        return;
      }
      if ((name === 'href' || name === 'xlink:href') && value && !value.startsWith('#')) {
        element.removeAttribute(attribute.name);
      }
    });
  });
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', label);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  return document.importNode(svg, true);
}

async function renderMermaidDiagram(container, source, label) {
  const requestId = String(++mermaidRenderSequence);
  container.dataset.renderRequest = requestId;
  container.className = 'mermaid-canvas loading';
  const loading = node('div', 'diagram-loading');
  loading.append(node('i'), node('span', '', 'Rendering architecture diagram…'));
  clear(container).append(loading);
  try {
    const normalized = normalizeMermaidSource(source);
    if (!normalized) throw new Error('Empty Mermaid source');
    const mermaid = await loadMermaidRenderer();
    await mermaid.parse(normalized);
    const renderId = `b2d-architecture-${Date.now()}-${mermaidRenderSequence}`;
    const result = await mermaid.render(renderId, normalized);
    if (!container.isConnected || container.dataset.renderRequest !== requestId) return;
    const svg = safeMermaidSvg(result.svg, label);
    container.className = 'mermaid-canvas ready';
    clear(container).append(svg);
  } catch {
    if (!container.isConnected || container.dataset.renderRequest !== requestId) return;
    container.className = 'mermaid-canvas failed';
    const error = node('div', 'diagram-error');
    error.append(node('strong', '', 'The architecture diagram could not be rendered.'), node('span', '', 'The generated Mermaid syntax may need another pass.'));
    const retry = node('button', '', 'Retry diagram');
    retry.type = 'button';
    retry.addEventListener('click', () => renderMermaidDiagram(container, source, label));
    error.append(retry);
    clear(container).append(error);
  }
}

function mermaidDiagramCard(title, source) {
  const card = resultCard(title, { full: true });
  card.classList.add('architecture-diagram-card');
  const canvas = node('div', 'mermaid-canvas loading');
  card.append(canvas);
  requestAnimationFrame(() => renderMermaidDiagram(canvas, source, title));
  return card;
}

function genericSection(title, value) {
  const card = resultCard(title, { full: typeof value === 'string' && value.length > 500 });
  if (Array.isArray(value)) {
    const list = node('ul', 'result-list');
    value.forEach(item => list.append(node('li', '', textValue(item))));
    card.append(list);
  } else if (isObject(value)) {
    appendKeyValues(card, value);
  } else {
    card.append(node('p', '', textValue(value)));
  }
  return card;
}

function renderOverview(panel) {
  const project = state.project;
  if (!project) {
    panel.append(resultEmpty('Start a project to see its structured overview.'));
    return;
  }
  const grid = resultGrid();
  appendTextCard(grid, 'Business idea', project.business_idea, { full: true });
  const summary = withoutKnownInformation(structuredTextValue(project.summary));
  if (typeof summary === 'string') appendTextCard(grid, 'Discovery summary', summary, { full: true });
  else Object.entries(summary || {}).forEach(([key, value]) => {
    if (isKnownInformationKey(key)) return;
    if (Array.isArray(value)) appendListCard(grid, labelFor(key), value);
    else if (isObject(value)) grid.append(genericSection(labelFor(key), value));
    else appendTextCard(grid, labelFor(key), value);
  });
  const known = project.known_information || {};
  if (hasValue(known)) {
    const card = resultCard('Known information', { full: true });
    appendKeyValues(card, known);
    grid.append(card);
  }
  const agentSummary = withoutKnownInformation(structuredTextValue(project.discovery?.summary));
  if (typeof agentSummary === 'string' && hasValue(agentSummary)) appendTextCard(grid, 'Agent summary', agentSummary, { full: true });
  else if (hasValue(agentSummary)) grid.append(genericSection('Agent summary', agentSummary));
  if (!grid.children.length) grid.append(resultEmpty('Discovery has not produced structured information yet.'));
  panel.append(grid);
}

function renderRequirements(panel, requirements) {
  if (!hasValue(requirements)) {
    panel.append(resultEmpty('Requirements have not been generated yet.'));
    return;
  }
  const grid = resultGrid();
  const preferredOrder = ['functional_requirements', 'non_functional_requirements', 'user_stories', 'acceptance_criteria', 'constraints', 'assumptions'];
  const keys = [...preferredOrder, ...Object.keys(requirements).filter(key => !preferredOrder.includes(key))];
  keys.forEach(key => {
    const value = requirements[key];
    if (Array.isArray(value)) appendListCard(grid, labelFor(key), value, { full: ['user_stories', 'acceptance_criteria'].includes(key) });
    else if (hasValue(value)) grid.append(genericSection(labelFor(key), value));
  });
  panel.append(grid.children.length ? grid : resultEmpty('Requirements are empty.'));
}

function renderArchitecture(panel, architecture) {
  if (!hasValue(architecture)) {
    panel.append(resultEmpty('Architecture has not been generated yet.'));
    return;
  }
  const grid = resultGrid();
  const mermaid = architecture.mermaid_diagram || architecture.diagram;
  if (mermaid) grid.append(mermaidDiagramCard('Architecture diagram', mermaid));

  const components = architecture.system_components || architecture.components;
  if (Array.isArray(components) && components.length) {
    const card = resultCard('System components', { full: true });
    const componentGrid = node('div', 'component-grid');
    components.forEach(component => {
      const item = node('div', 'component-card');
      item.append(node('strong', '', component.name || component.type || 'Component'));
      if (component.description) item.append(node('p', '', component.description));
      const meta = [component.type, component.technology].filter(Boolean).join(' · ');
      if (meta) item.append(node('div', 'component-meta', meta));
      componentGrid.append(item);
    });
    card.append(componentGrid);
    grid.append(card);
  }
  if (hasValue(architecture.technology_stack)) {
    const card = resultCard('Technology stack');
    appendKeyValues(card, architecture.technology_stack);
    grid.append(card);
  }
  ['communication', 'security', 'scalability'].forEach(key => appendListCard(grid, labelFor(key), architecture[key]));
  appendTextCard(grid, 'Authentication', architecture.authentication);
  appendTextCard(grid, 'Deployment architecture', architecture.deployment_architecture, { full: true });
  const handled = new Set(['system_components', 'components', 'technology_stack', 'communication', 'security', 'scalability', 'authentication', 'deployment_architecture', 'mermaid_diagram', 'diagram']);
  Object.entries(architecture).filter(([key, value]) => !handled.has(key) && hasValue(value)).forEach(([key, value]) => grid.append(genericSection(labelFor(key), value)));
  panel.append(grid);
}

function renderDatabase(panel, database) {
  if (!hasValue(database)) {
    panel.append(resultEmpty('Database design has not been generated yet.'));
    return;
  }
  const grid = resultGrid();
  appendTextCard(grid, 'Database technology', database.database_technology || database.technology);
  const entities = database.entities;
  if (Array.isArray(entities) && entities.length) {
    const card = resultCard('Entities and fields', { full: true });
    const entityList = node('div', 'entity-list');
    entities.forEach(entity => {
      const item = node('div', 'entity-card');
      item.append(node('strong', '', entity.name || 'Entity'));
      if (entity.description) item.append(node('p', 'component-meta', entity.description));
      if (Array.isArray(entity.fields) && entity.fields.length) {
        const wrap = node('div', 'data-table-wrap');
        const table = node('table', 'data-table');
        const head = node('thead');
        const headRow = node('tr');
        ['Field', 'Type', 'PK', 'FK', 'Nullable', 'Unique', 'Indexed'].forEach(label => headRow.append(node('th', '', label)));
        head.append(headRow);
        const body = node('tbody');
        entity.fields.forEach(field => {
          const row = node('tr');
          [field.name, field.type, field.primary_key, field.foreign_key, field.nullable, field.unique, field.indexed].forEach(value => row.append(node('td', '', textValue(value))));
          body.append(row);
        });
        table.append(head, body);
        wrap.append(table);
        item.append(wrap);
      }
      entityList.append(item);
    });
    card.append(entityList);
    grid.append(card);
  }
  ['relationships', 'indexes', 'constraints'].forEach(key => appendListCard(grid, labelFor(key), database[key], { full: true }));
  if (database.sql_schema) {
    const card = resultCard('SQL schema', { full: true });
    card.append(codeBlock(database.sql_schema, 'SQL'));
    grid.append(card);
  }
  if (database.erd_mermaid) {
    const card = resultCard('Entity relationship diagram · Mermaid source', { full: true });
    card.append(codeBlock(database.erd_mermaid, 'Mermaid · rendered as safe text'));
    grid.append(card);
  }
  const handled = new Set(['database_technology', 'technology', 'entities', 'relationships', 'indexes', 'constraints', 'sql_schema', 'erd_mermaid']);
  Object.entries(database).filter(([key, value]) => !handled.has(key) && hasValue(value)).forEach(([key, value]) => grid.append(genericSection(labelFor(key), value)));
  panel.append(grid);
}

function renderApi(panel, apiDesign) {
  if (!hasValue(apiDesign)) {
    panel.append(resultEmpty('API design has not been generated yet.'));
    return;
  }
  const grid = resultGrid();
  const endpoints = apiDesign.endpoints;
  if (Array.isArray(endpoints) && endpoints.length) {
    const card = resultCard('Endpoints', { full: true });
    const list = node('div', 'endpoint-list');
    endpoints.forEach(endpoint => {
      const item = node('div', 'endpoint-card');
      const line = node('div');
      const method = String(endpoint.method || 'GET').toLowerCase();
      line.append(node('span', `method ${method}`, method.toUpperCase()), node('span', 'endpoint-path', endpoint.path || '/'));
      item.append(line);
      if (endpoint.summary || endpoint.description) item.append(node('p', 'component-meta', endpoint.summary || endpoint.description));
      const detailsData = {
        Authentication: endpoint.auth,
        Pagination: endpoint.pagination,
        Filters: endpoint.filters,
        'Request schema': endpoint.request_schema || endpoint.request,
        'Response schema': endpoint.response_schema || endpoint.response
      };
      if (Object.values(detailsData).some(hasValue)) {
        const details = node('details');
        details.append(node('summary', '', 'Request and response details'));
        const values = Object.fromEntries(Object.entries(detailsData).filter(([, value]) => hasValue(value)));
        appendKeyValues(details, values);
        item.append(details);
      }
      list.append(item);
    });
    card.append(list);
    grid.append(card);
  }
  appendTextCard(grid, 'Authentication', apiDesign.authentication);
  appendTextCard(grid, 'Authorization', apiDesign.authorization);
  appendListCard(grid, 'Error handling', apiDesign.error_handling);
  appendTextCard(grid, 'Pagination', apiDesign.pagination);
  appendTextCard(grid, 'Filtering', apiDesign.filtering);
  const openapi = apiDesign.openapi_spec || apiDesign.openapi;
  if (hasValue(openapi)) {
    const card = resultCard('OpenAPI specification', { full: true });
    card.append(codeBlock(typeof openapi === 'string' ? openapi : JSON.stringify(openapi, null, 2), 'OpenAPI'));
    grid.append(card);
  }
  const handled = new Set(['endpoints', 'authentication', 'authorization', 'error_handling', 'pagination', 'filtering', 'openapi_spec', 'openapi']);
  Object.entries(apiDesign).filter(([key, value]) => !handled.has(key) && hasValue(value)).forEach(([key, value]) => grid.append(genericSection(labelFor(key), value)));
  panel.append(grid);
}

function renderDevops(panel, devops) {
  if (!hasValue(devops)) {
    panel.append(resultEmpty('DevOps configuration has not been generated yet.'));
    return;
  }
  const grid = resultGrid();
  const codeFields = [
    ['dockerfile', 'Dockerfile', 'dockerfile'],
    ['docker_compose', 'Docker Compose', 'yaml'],
    ['ci_cd_pipeline', 'CI/CD pipeline', 'yaml'],
    ['github_actions', 'GitHub Actions', 'yaml']
  ];
  codeFields.forEach(([key, title, language]) => {
    if (!hasValue(devops[key])) return;
    const card = resultCard(title, { full: true });
    card.append(codeBlock(devops[key], title, { language }));
    grid.append(card);
  });
  if (hasValue(devops.environment_variables)) {
    const card = resultCard('Environment variables', { full: true });
    card.append(node('p', 'component-meta', 'Sensitive values are intentionally hidden in the browser.'));
    appendKeyValues(card, devops.environment_variables, { redactSecrets: true });
    grid.append(card);
  }
  appendTextCard(grid, 'Deployment strategy', devops.deployment_strategy, { full: true });
  ['health_checks', 'logging', 'monitoring'].forEach(key => appendListCard(grid, labelFor(key), devops[key]));
  appendTextCard(grid, 'Secrets management', devops.secrets_management, { full: true });
  const handled = new Set([...codeFields.map(([key]) => key), 'environment_variables', 'deployment_strategy', 'health_checks', 'logging', 'monitoring', 'secrets_management']);
  Object.entries(devops).filter(([key, value]) => !handled.has(key) && hasValue(value)).forEach(([key, value]) => grid.append(genericSection(labelFor(key), value)));
  panel.append(grid);
}

function renderReview(panel, review) {
  if (!hasValue(review)) {
    panel.append(resultEmpty('The review agent has not completed yet.'));
    return;
  }
  const grid = resultGrid();
  if (review.score !== null && review.score !== undefined) {
    const scoreCard = resultCard('Review score');
    const numeric = Number(review.score);
    const display = Number.isFinite(numeric) ? `${Math.round((numeric <= 1 ? numeric * 100 : numeric))}%` : textValue(review.score);
    scoreCard.append(node('div', 'review-score', display), node('p', '', labelFor(review.status || state.project?.status || 'reviewed')));
    grid.append(scoreCard);
  }
  appendTextCard(grid, 'Review status', review.status);
  const issues = Array.isArray(review.issues) ? review.issues : [];
  if (issues.length) {
    const card = resultCard('Review issues', { full: true });
    const list = node('div', 'issue-list');
    issues.forEach(issue => {
      const severity = String(issue.severity || 'warning').toLowerCase();
      const item = node('div', `issue-card ${severity}`);
      const head = node('div', 'issue-head');
      head.append(node('strong', '', issue.artifact || issue.source_artifact || 'Blueprint'), node('span', 'severity', severity));
      item.append(head);
      const fields = ['problem', 'expected', 'actual', 'fix', 'source_decision', 'conflicting_decision'];
      const visible = Object.fromEntries(fields.filter(key => hasValue(issue[key])).map(key => [key, issue[key]]));
      appendKeyValues(item, visible);
      list.append(item);
    });
    card.append(list);
    grid.append(card);
  } else {
    appendTextCard(grid, 'Issues', 'No review issues were reported.', { full: true });
  }
  appendListCard(grid, 'Artifacts to regenerate', review.artifacts_to_regenerate, { full: true });
  const handled = new Set(['score', 'status', 'issues', 'artifacts_to_regenerate']);
  Object.entries(review).filter(([key, value]) => !handled.has(key) && hasValue(value)).forEach(([key, value]) => grid.append(genericSection(labelFor(key), value)));
  panel.append(grid);
}

function normalizeArtifact(item) {
  if (typeof item === 'string') return { name: item };
  if (isObject(item)) return { ...item, name: item.name || item.filename || item.artifact || item.type };
  return { name: String(item) };
}

function artifactExtension(name) {
  const part = String(name || '').split('.').pop();
  return part && part !== name ? part.toUpperCase().slice(0, 5) : 'FILE';
}

function renderFiles(panel) {
  if (!state.artifacts.length) {
    panel.append(resultEmpty(TERMINAL_STATUSES.has(state.project?.status) ? 'No artifact files are available for this project.' : 'Artifact files will appear after generation completes.'));
    return;
  }
  const card = resultCard('Generated files', { full: true });
  const list = node('div', 'artifact-list');
  state.artifacts.forEach(raw => {
    const artifact = normalizeArtifact(raw);
    const item = node('div', 'artifact-card');
    item.append(node('div', 'artifact-icon', artifactExtension(artifact.name)));
    const copy = node('div', 'artifact-copy');
    copy.append(node('strong', '', artifact.name || 'Artifact'));
    const meta = [artifact.type, artifact.size ? `${artifact.size} bytes` : '', artifact.updated_at ? formatDate(artifact.updated_at) : ''].filter(Boolean).join(' · ');
    copy.append(node('small', '', meta || 'Generated artifact'));
    const actions = node('div', 'artifact-actions');
    const preview = node('button', '', 'Preview');
    preview.type = 'button';
    preview.addEventListener('click', () => previewArtifact(artifact));
    const copyButton = node('button', '', 'Copy');
    copyButton.type = 'button';
    copyButton.addEventListener('click', () => copyArtifact(artifact));
    const download = node('button', '', 'Download');
    download.type = 'button';
    download.addEventListener('click', () => downloadArtifact(artifact));
    actions.append(preview, copyButton, download);
    item.append(copy, actions);
    list.append(item);
  });
  card.append(list);
  panel.append(card);
}

function renderActiveResult() {
  const panel = clear($('#resultPanel'));
  if (!panel) return;
  if (!state.project) {
    panel.append(resultEmpty('Start or open a project to view its blueprint.'));
    return;
  }
  const blueprint = state.project.blueprint || {};
  switch (state.activeTab) {
    case 'requirements': renderRequirements(panel, blueprint.requirements); break;
    case 'architecture': renderArchitecture(panel, blueprint.architecture); break;
    case 'database': renderDatabase(panel, blueprint.database); break;
    case 'api': renderApi(panel, blueprint.api); break;
    case 'devops': renderDevops(panel, blueprint.devops); break;
    case 'review': renderReview(panel, blueprint.review); break;
    case 'files': renderFiles(panel); break;
    default: renderOverview(panel);
  }
}

async function loadArtifacts({ quiet = false } = {}) {
  if (!state.project?.project_id) {
    state.artifacts = [];
    return;
  }
  try {
    const payload = await api(projectEndpoint(state.project.project_id, '/artifacts'));
    const list = Array.isArray(payload?.artifacts) ? payload.artifacts : Array.isArray(payload) ? payload : [];
    state.artifacts = list.map(normalizeArtifact).filter(item => item.name);
    renderResultTabs();
    if (state.activeTab === 'files') renderActiveResult();
  } catch (error) {
    state.artifacts = [];
    if (!quiet && error.status !== 404) showBanner(readableError(error));
  }
}

function artifactUrl(artifact) {
  return projectEndpoint(state.project.project_id, `/artifacts/${encodeURIComponent(artifact.name)}`);
}

async function artifactResponse(artifact) {
  const response = await authorizedFetch(artifactUrl(artifact), { accept: '*/*' });
  if (!response.ok) {
    const message = await response.text().catch(() => '');
    throw new Error(message || `Could not load ${artifact.name}.`);
  }
  return response;
}

async function previewArtifact(artifact) {
  openModal(`Preview · ${artifact.name}`, node('div', 'artifact-preview'), { artifact: true });
  const body = $('#modalBody');
  body.append(node('div', 'result-empty', 'Loading preview…'));
  try {
    const response = await artifactResponse(artifact);
    const text = await response.text();
    clear(body).append(codeBlock(text, artifact.name));
  } catch (error) {
    clear(body).append(node('div', 'result-error', readableError(error)));
  }
}

async function copyArtifact(artifact) {
  try {
    const response = await artifactResponse(artifact);
    const text = await response.text();
    await navigator.clipboard.writeText(text);
    showBanner(`${artifact.name} copied.`, 'success');
  } catch (error) {
    showBanner(readableError(error));
  }
}

async function downloadArtifact(artifact) {
  try {
    const response = await artifactResponse(artifact);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = artifact.name || 'artifact.txt';
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    showBanner(readableError(error));
  }
}

function openModal(titleText, content, options = {}) {
  closeMenus();
  const wrap = $('#modalWrap');
  const modal = $('#modal');
  const title = $('#modalTitle');
  const body = clear($('#modalBody'));
  if (!wrap || !modal || !title || !body) return;
  title.textContent = titleText;
  modal.className = `modal${options.artifact ? ' artifact-modal' : ''}`;
  $('#modalTabs')?.classList.add('hidden');
  $('#modalFoot')?.classList.add('hidden');
  if (content) body.append(content);
  wrap.classList.add('show');
}

function closeModal() {
  $('#modalWrap')?.classList.remove('show');
}

function openSearch() {
  const content = node('div');
  const box = node('div', 'search-box');
  const input = node('input');
  input.type = 'search';
  input.placeholder = 'Search your projects';
  input.value = state.search;
  box.append(input);
  const results = node('div', 'list');
  results.style.marginTop = '10px';
  const render = () => {
    clear(results);
    const query = input.value.trim().toLowerCase();
    const matches = state.projects.filter(project => !query || `${projectTitle(project)} ${project.status || ''}`.toLowerCase().includes(query));
    if (!matches.length) {
      results.append(node('div', 'result-empty', 'No matching projects.'));
      return;
    }
    matches.forEach(project => {
      const button = node('button', 'card-row');
      button.type = 'button';
      button.style.width = '100%';
      const copy = node('div', 'grow');
      copy.append(node('strong', '', projectTitle(project)), node('small', '', `${labelFor(project.status || 'discovery')} · ${formatDate(project.updated_at)}`));
      button.append(copy);
      button.addEventListener('click', () => {
        state.search = '';
        closeModal();
        openProject(project.project_id);
      });
      results.append(button);
    });
  };
  input.addEventListener('input', render);
  content.append(box, results);
  openModal('Search projects', content);
  render();
  window.setTimeout(() => input.focus(), 0);
}

function exportConversation() {
  closeMenus();
  const transcript = state.project?.transcript || [];
  if (!transcript.length) {
    showBanner('This project does not have a conversation to export.');
    return;
  }
  const text = transcript.map(turn => `${String(turn.role || 'agent').toUpperCase()}: ${turn.message || turn.text || ''}`).join('\n\n');
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `${state.project.project_id}-conversation.txt`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

async function copyProjectLink() {
  closeMenus();
  if (!state.project) return;
  const url = new URL(location.href);
  url.searchParams.set('project', state.project.project_id);
  try {
    await navigator.clipboard.writeText(url.href);
    showBanner('Project link copied.', 'success');
  } catch {
    showBanner('Copy is unavailable in this browser.');
  }
}

async function logout() {
  closeMenus();
  if (!state.authClient || !state.config?.auth_enabled) {
    location.replace('/login');
    return;
  }
  try {
    const { error } = await state.authClient.auth.signOut();
    if (error) throw error;
    location.replace('/login');
  } catch (error) {
    showBanner(readableError(error));
  }
}

function openVoiceInput() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    showBanner('Voice input is not supported in this browser.');
    return;
  }
  const screen = $('#voiceScreen');
  const label = $('#voiceLabel');
  const recognition = new SpeechRecognition();
  recognition.lang = navigator.language || 'en-US';
  recognition.interimResults = true;
  recognition.onresult = event => {
    const transcript = [...event.results].map(result => result[0].transcript).join('');
    $('#prompt').value = transcript;
    inputChanged();
  };
  recognition.onerror = event => {
    label.textContent = event.error === 'not-allowed' ? 'Microphone permission was denied' : 'Voice input stopped';
  };
  recognition.onend = () => screen?.classList.remove('show');
  state.recognition = recognition;
  screen?.classList.add('show');
  label.textContent = 'Listening…';
  recognition.start();
}

function bindEvents() {
  $('#newChat')?.addEventListener('click', () => resetProject());
  $('[data-view="chat"]')?.addEventListener('click', () => switchView('chat'));
  $('[data-view="results"]')?.addEventListener('click', () => switchView('results'));
  $('#prompt')?.addEventListener('input', inputChanged);
  $('#prompt')?.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      sendPrompt();
    }
  });
  $('#sendBtn')?.addEventListener('click', sendPrompt);

  $('#followPrev')?.addEventListener('click', () => {
    if (!state.follow || state.follow.index === 0) return;
    saveCurrentFollowAnswer();
    state.follow.index -= 1;
    renderFollowQuestion();
  });
  $('#followNext')?.addEventListener('click', advanceFollow);
  $('#followContinue')?.addEventListener('click', advanceFollow);
  $('#followSkip')?.addEventListener('click', skipFollow);
  $('#followClose')?.addEventListener('click', pauseFollow);
  $('#followResume')?.addEventListener('click', resumeFollow);
  $('#textAnswerInput')?.addEventListener('input', updateFollowControls);
  $('#textAnswerInput')?.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      advanceFollow();
    }
  });
  $('#customSave')?.addEventListener('click', () => {
    if (!state.follow) return;
    const question = state.follow.questions[state.follow.index];
    const key = question.id || `question_${state.follow.index}`;
    const value = $('#customAnswerInput').value.trim();
    if (!value) {
      showBanner('Write an answer first.');
      return;
    }
    state.follow.answers[key] = value;
    $('#customAnswer')?.classList.remove('show');
    renderFollowQuestion();
  });
  $('#customCancel')?.addEventListener('click', () => $('#customAnswer')?.classList.remove('show'));
  $('#customAnswerInput')?.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      $('#customSave')?.click();
    }
  });

  $('#searchBtn')?.addEventListener('click', openSearch);
  $('#mobileMenu')?.addEventListener('click', () => $('#sidebar')?.classList.add('open'));
  $('#collapseSide')?.addEventListener('click', () => {
    if (innerWidth < 901) $('#sidebar')?.classList.remove('open');
    else document.documentElement.style.setProperty('--side', getComputedStyle(document.documentElement).getPropertyValue('--side').trim() === '0px' ? '236px' : '0px');
  });

  $('#accountBtn')?.addEventListener('click', event => {
    event.stopPropagation();
    placeMenu($('#accountMenu'), event.currentTarget, 'right');
  });
  $('#topMore')?.addEventListener('click', event => {
    event.stopPropagation();
    placeMenu($('#topMenu'), event.currentTarget, 'right');
  });
  $('#shareBtn')?.addEventListener('click', copyProjectLink);
  $('#logoutButton')?.addEventListener('click', logout);
  $('#exportConversation')?.addEventListener('click', exportConversation);
  $('#copyProjectLink')?.addEventListener('click', copyProjectLink);
  $('#attachBtn')?.addEventListener('click', () => showBanner('File attachments are not part of the current backend contract yet.'));
  $('#toolsBtn')?.addEventListener('click', () => showBanner('The coordinated seven-agent workflow selects the required tools automatically.', 'success'));
  $('#effortBtn')?.addEventListener('click', () => state.project ? switchView('results') : showBanner('Start a project to see agent progress.'));
  $('#effortLabel').textContent = '7 agents';
  $('#voiceBtn')?.addEventListener('click', openVoiceInput);
  $('#voiceEnd')?.addEventListener('click', () => {
    state.recognition?.stop();
    $('#voiceScreen')?.classList.remove('show');
  });
  $('#voiceMute')?.addEventListener('click', () => {
    state.recognition?.stop();
    $('#voiceLabel').textContent = 'Microphone paused';
  });

  $('#modalClose')?.addEventListener('click', closeModal);
  $('#modalWrap')?.addEventListener('click', event => { if (event.target === $('#modalWrap')) closeModal(); });
  document.addEventListener('click', event => {
    if (!event.target.closest('.menu') && !event.target.closest('#topMore') && !event.target.closest('#accountBtn')) closeMenus();
  });
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      openSearch();
    }
    if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'o') {
      event.preventDefault();
      resetProject();
    }
    if (event.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
      event.preventDefault();
      switchView('chat');
      $('#prompt')?.focus();
    }
    if (event.key === 'Escape') {
      closeMenus();
      closeModal();
      $('#sidebar')?.classList.remove('open');
    }
  });
  window.addEventListener('resize', () => { syncComposerSpace(); positionEmptyComposer(); });
  window.addEventListener('popstate', event => {
    const projectId = event.state?.projectId || new URL(location.href).searchParams.get('project');
    if (projectId) openProject(projectId, { updateUrl: false });
    else resetProject({ updateUrl: false });
  });
}

async function initialize() {
  prepareShell();
  bindEvents();
  try {
    await initializeAuthentication();
    await loadProjects();
    const projectId = new URL(location.href).searchParams.get('project');
    if (projectId) await openProject(projectId, { updateUrl: false });
    else resetProject({ updateUrl: false });
  } catch (error) {
    renderSidebarState(readableError(error), 'error');
    showBanner(readableError(error), 'error', true);
  } finally {
    setAppLoading(false);
    inputChanged();
  }
}

initialize();
