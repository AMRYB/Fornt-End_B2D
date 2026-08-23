const API_CONFIG_URL = '/api/config';
const SUPABASE_ESM_URL = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.112.3/+esm';

const card = document.getElementById('authCard');
const loginForm = document.getElementById('loginForm');
const signupForm = document.getElementById('signupForm');
const forgotForm = document.getElementById('forgotForm');
const forgotPasswordLink = document.getElementById('forgotPasswordLink');
const backToLogin = document.getElementById('backToLogin');
const forgotSuccess = document.getElementById('forgotSuccess');
const visualCopy = document.getElementById('visualCopy');
const visualKicker = document.getElementById('visualKicker');
const visualTitle = document.getElementById('visualTitle');
const visualText = document.getElementById('visualText');

let authClient = null;
let currentMode = 'login';
let authReady = false;
let recoveryMode = false;

const visualContent = {
  login: {
    kicker: 'Continue your blueprint',
    title: 'Pick up where you left off.',
    text: 'Return to your project context, requirements, architecture decisions, generated artifacts and review history without rebuilding the thinking from scratch.'
  },
  signup: {
    kicker: 'Build your workspace',
    title: 'Start with the idea.',
    text: 'Turn a raw business idea into a review-ready engineering blueprint across requirements, architecture, data, APIs, DevOps and technical review.'
  },
  forgot: {
    kicker: 'Account recovery',
    title: 'Reset access without losing momentum.',
    text: 'We will send a secure recovery link to the email address connected to your workspace.'
  },
  recovery: {
    kicker: 'Choose a new password',
    title: 'Secure your workspace.',
    text: 'Set a new password, then continue directly to your saved projects and generated blueprints.'
  }
};

function createRecoveryForm() {
  const form = document.createElement('form');
  form.className = 'form-view';
  form.id = 'recoveryForm';
  form.innerHTML = `
    <div class="brand"><img class="brand-logo" src="./assets/img/logo.png" alt="Business to Development"></div>
    <h1>Choose new password</h1>
    <p class="forgot-copy">Use at least 8 characters for your new password.</p>
    <div class="field">
      <label for="newPassword">New password</label>
      <div class="password-wrap">
        <input id="newPassword" type="password" autocomplete="new-password" minlength="8" required>
        <button class="password-toggle" type="button" data-password-toggle="newPassword">Show</button>
      </div>
    </div>
    <div class="field">
      <label for="confirmPassword">Confirm password</label>
      <div class="password-wrap">
        <input id="confirmPassword" type="password" autocomplete="new-password" minlength="8" required>
        <button class="password-toggle" type="button" data-password-toggle="confirmPassword">Show</button>
      </div>
    </div>
    <button class="primary-btn" type="submit">Update password</button>
  `;
  forgotForm.insertAdjacentElement('afterend', form);
  return form;
}

const recoveryForm = createRecoveryForm();
const forms = { login: loginForm, signup: signupForm, forgot: forgotForm, recovery: recoveryForm };

function ensureStatus(form) {
  let status = form.querySelector('.auth-status');
  if (!status) {
    status = document.createElement('div');
    status.className = 'auth-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    form.querySelector('.primary-btn')?.insertAdjacentElement('beforebegin', status);
  }
  return status;
}

Object.values(forms).forEach(ensureStatus);

function setStatus(form, message = '', kind = 'info') {
  const status = ensureStatus(form);
  status.textContent = message;
  status.className = `auth-status${message ? ' show' : ''} ${kind}`;
}

function setBusy(form, busy, label) {
  form.classList.toggle('form-loading', busy);
  const button = form.querySelector('.primary-btn');
  if (!button) return;
  if (!button.dataset.defaultLabel) button.dataset.defaultLabel = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? label : button.dataset.defaultLabel;
}

function setMode(mode, { updateHistory = true } = {}) {
  currentMode = forms[mode] ? mode : 'login';
  const content = visualContent[currentMode] || visualContent.login;
  card.classList.toggle('signup-mode', currentMode === 'signup');
  Object.entries(forms).forEach(([name, form]) => form.classList.toggle('active', name === currentMode));
  forgotSuccess.classList.remove('show');

  visualCopy.classList.add('switching');
  window.setTimeout(() => {
    visualKicker.textContent = content.kicker;
    visualTitle.textContent = content.title;
    visualText.textContent = content.text;
    visualCopy.classList.remove('switching');
  }, 120);

  const titles = {
    login: 'Business to Development | Login',
    signup: 'Business to Development | Sign up',
    forgot: 'Business to Development | Reset password',
    recovery: 'Business to Development | New password'
  };
  document.title = titles[currentMode];
  if (updateHistory && !recoveryMode) history.pushState({ mode: currentMode }, '', `#${currentMode}`);
}

function bindPasswordToggles() {
  document.querySelectorAll('[data-password-toggle]').forEach(button => {
    button.addEventListener('click', () => {
      const input = document.getElementById(button.dataset.passwordToggle);
      if (!input) return;
      const showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      button.textContent = showing ? 'Show' : 'Hide';
      button.setAttribute('aria-pressed', String(!showing));
    });
  });
}

function safeNextLocation() {
  const raw = new URLSearchParams(location.search).get('next') || '/';
  let decoded = raw;
  for (let pass = 0; pass < 5; pass += 1) {
    if (!decoded.startsWith('/') || decoded.startsWith('//') || /[\\\u0000-\u001f\u007f]/u.test(decoded)) return '/';
    try {
      const nextDecoded = decodeURIComponent(decoded);
      if (nextDecoded === decoded) break;
      decoded = nextDecoded;
      if (pass === 4) return '/';
    } catch {
      return '/';
    }
  }

  try {
    const target = new URL(raw, location.origin);
    if (target.origin !== location.origin || target.username || target.password) return '/';
    return `${target.pathname}${target.search}${target.hash}` || '/';
  } catch {
    return '/';
  }
}

function goToWorkspace() {
  location.replace(safeNextLocation());
}

function readableError(error) {
  if (!error) return 'Something went wrong. Please try again.';
  return error.message || error.error_description || String(error);
}

async function loadConfig() {
  const response = await fetch(API_CONFIG_URL, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error(`Could not load authentication settings (${response.status}).`);
  return response.json();
}

async function initializeAuth() {
  Object.values(forms).forEach(form => setBusy(form, true, 'Connecting…'));
  try {
    const config = await loadConfig();
    if (!config.auth_enabled) {
      setStatus(loginForm, 'Authentication is disabled for this environment. Opening the workspace…', 'success');
      window.setTimeout(goToWorkspace, 450);
      return;
    }
    if (!config.supabase_url || !config.supabase_anon_key) {
      throw new Error('Authentication is enabled, but the public Supabase configuration is incomplete.');
    }

    const { createClient } = await import(SUPABASE_ESM_URL);
    authClient = createClient(config.supabase_url, config.supabase_anon_key, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true }
    });

    authClient.auth.onAuthStateChange((event, session) => {
      if (event === 'PASSWORD_RECOVERY') {
        recoveryMode = true;
        setMode('recovery', { updateHistory: false });
        return;
      }
      if (event === 'SIGNED_IN' && session && !recoveryMode && currentMode === 'login') goToWorkspace();
    });

    const recoveryHash = /type=recovery|#recovery/i.test(location.hash);
    const { data, error } = await authClient.auth.getSession();
    if (error) throw error;
    if (recoveryHash) {
      recoveryMode = true;
      setMode('recovery', { updateHistory: false });
    } else if (data.session) {
      goToWorkspace();
      return;
    }
    authReady = true;
  } catch (error) {
    setStatus(loginForm, readableError(error), 'error');
  } finally {
    Object.values(forms).forEach(form => setBusy(form, false));
  }
}

document.querySelectorAll('[data-switch]').forEach(button => {
  button.addEventListener('click', () => setMode(button.dataset.switch));
});

forgotPasswordLink.addEventListener('click', () => {
  const email = document.getElementById('loginEmail').value.trim();
  if (email) document.getElementById('resetEmail').value = email;
  setMode('forgot');
});

backToLogin.addEventListener('click', () => setMode('login'));
window.addEventListener('popstate', event => {
  if (!recoveryMode) setMode(event.state?.mode || 'login', { updateHistory: false });
});

loginForm.addEventListener('submit', async event => {
  event.preventDefault();
  setStatus(loginForm);
  if (!authReady || !authClient) {
    setStatus(loginForm, 'Authentication is still connecting. Please try again.', 'error');
    return;
  }
  setBusy(loginForm, true, 'Signing in…');
  try {
    const email = document.getElementById('loginEmail').value.trim();
    const password = document.getElementById('loginPassword').value;
    const { error } = await authClient.auth.signInWithPassword({ email, password });
    if (error) throw error;
    setStatus(loginForm, 'Signed in. Opening your workspace…', 'success');
    goToWorkspace();
  } catch (error) {
    setStatus(loginForm, readableError(error), 'error');
  } finally {
    setBusy(loginForm, false);
  }
});

signupForm.addEventListener('submit', async event => {
  event.preventDefault();
  setStatus(signupForm);
  if (!authReady || !authClient) {
    setStatus(signupForm, 'Authentication is still connecting. Please try again.', 'error');
    return;
  }
  setBusy(signupForm, true, 'Creating account…');
  try {
    const fullName = document.getElementById('fullName').value.trim();
    const email = document.getElementById('signupEmail').value.trim();
    const password = document.getElementById('signupPassword').value;
    const emailRedirectTo = `${location.origin}/login`;
    const { data, error } = await authClient.auth.signUp({
      email,
      password,
      options: { data: { full_name: fullName }, emailRedirectTo }
    });
    if (error) throw error;
    if (data.session) {
      setStatus(signupForm, 'Account created. Opening your workspace…', 'success');
      goToWorkspace();
    } else {
      setStatus(signupForm, 'Account created. Check your email to confirm your address, then sign in.', 'success');
    }
  } catch (error) {
    setStatus(signupForm, readableError(error), 'error');
  } finally {
    setBusy(signupForm, false);
  }
});

forgotForm.addEventListener('submit', async event => {
  event.preventDefault();
  setStatus(forgotForm);
  if (!authReady || !authClient) {
    setStatus(forgotForm, 'Authentication is still connecting. Please try again.', 'error');
    return;
  }
  setBusy(forgotForm, true, 'Sending link…');
  try {
    const email = document.getElementById('resetEmail').value.trim();
    const redirectTo = `${location.origin}/login#recovery`;
    const { error } = await authClient.auth.resetPasswordForEmail(email, { redirectTo });
    if (error) throw error;
    forgotSuccess.textContent = 'If an account exists for this address, a secure reset link has been sent.';
    forgotSuccess.classList.add('show');
    setStatus(forgotForm, 'Check your inbox and follow the reset link.', 'success');
  } catch (error) {
    setStatus(forgotForm, readableError(error), 'error');
  } finally {
    setBusy(forgotForm, false);
  }
});

recoveryForm.addEventListener('submit', async event => {
  event.preventDefault();
  setStatus(recoveryForm);
  const password = document.getElementById('newPassword').value;
  const confirmation = document.getElementById('confirmPassword').value;
  if (password !== confirmation) {
    setStatus(recoveryForm, 'The two passwords do not match.', 'error');
    return;
  }
  if (!authClient) {
    setStatus(recoveryForm, 'The recovery session is unavailable. Open the latest reset link again.', 'error');
    return;
  }
  setBusy(recoveryForm, true, 'Updating…');
  try {
    const { error } = await authClient.auth.updateUser({ password });
    if (error) throw error;
    recoveryMode = false;
    setStatus(recoveryForm, 'Password updated. Opening your workspace…', 'success');
    window.setTimeout(goToWorkspace, 400);
  } catch (error) {
    setStatus(recoveryForm, readableError(error), 'error');
  } finally {
    setBusy(recoveryForm, false);
  }
});

bindPasswordToggles();
history.replaceState({ mode: 'login' }, '', /#(signup|forgot)$/i.test(location.hash) ? location.hash : '#login');
if (/#signup$/i.test(location.hash)) setMode('signup', { updateHistory: false });
if (/#forgot$/i.test(location.hash)) setMode('forgot', { updateHistory: false });
initializeAuth();
