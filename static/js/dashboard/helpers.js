function getCookie(name) {
  const match = document.cookie.match(new RegExp('(^|;)\\s*' + name + '=([^;]+)'));
  return match ? decodeURIComponent(match[2]) : null;
}

export function getCSRFToken() {
  const token = getCookie('csrftoken');
  if (token) {
    return token;
  }
  const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
  return input ? input.value : '';
}

export function debounce(fn, wait = 300) {
  let timeout;
  return (...args) => {
    clearTimeout(timeout);
    timeout = setTimeout(() => fn.apply(null, args), wait);
  };
}

export async function requestJSON(url, options = {}) {
  const resp = await fetch(url, options);
  const contentType = resp.headers.get('content-type') || '';
  const redirectedToLogin =
    resp.redirected && /\/accounts\/login\/?(?:\?|$)/.test(new URL(resp.url).pathname);
  if (resp.status === 401 || redirectedToLogin) {
    try {
      if (window.showToast) {
        window.showToast('Session expired. Please sign in again.', 'error');
      }
    } catch (error) {
      /* ignore */
    }
    window.location.href =
      '/accounts/login/?next=' + encodeURIComponent(window.location.pathname);
    throw new Error('Unauthorized');
  }
  if (!resp.ok) {
    const text = await resp.text();
    let message = text || resp.statusText;
    if (contentType.includes('application/json') && text) {
      try {
        const payload = JSON.parse(text);
        message = payload.error || payload.detail || message;
      } catch (_) {
        // Keep the response text when a server labels malformed content as JSON.
      }
    }
    const error = new Error(message);
    error.status = resp.status;
    throw error;
  }
  if (!contentType.includes('application/json')) {
    throw new Error('Expected a JSON response from the server.');
  }
  return resp.json();
}

export function postJSON(url, payload, options = {}) {
  const headers = Object.assign(
    {
      'Content-Type': 'application/json',
      'X-CSRFToken': getCSRFToken(),
    },
    options.headers || {},
  );
  return requestJSON(
    url,
    Object.assign({}, options, {
      method: options.method || 'POST',
      headers,
      body: JSON.stringify(payload),
    }),
  );
}

export function toggleButtonLoading(button, isLoading, label = 'Loading...') {
  if (!button) return;
  if (isLoading) {
    button.dataset.originalText = button.dataset.originalText || button.textContent;
    button.disabled = true;
    button.classList.add('loading');
    button.textContent = label;
  } else {
    button.disabled = false;
    button.classList.remove('loading');
    if (button.dataset.originalText) {
      button.textContent = button.dataset.originalText;
      delete button.dataset.originalText;
    }
  }
}

function progressBar() {
  return document.getElementById('globalProgress');
}

export function startProgress() {
  const bar = progressBar();
  if (!bar) return;
  bar.style.opacity = '1';
  bar.style.width = '0%';
  window.requestAnimationFrame(() => {
    bar.style.width = '80%';
  });
}

export function endProgress() {
  const bar = progressBar();
  if (!bar) return;
  bar.style.width = '100%';
  setTimeout(() => {
    bar.style.opacity = '0';
    bar.style.width = '0%';
  }, 400);
}

export function emit(name, detail = {}) {
  document.dispatchEvent(new CustomEvent(name, { detail }));
}

export function safeNumber(value, fallback = 0) {
  const num = Number(value);
  return Number.isFinite(num) ? num : fallback;
}

export function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return '-';
  }
  return `${Number(value).toFixed(1)}%`;
}

export function glowElement(element, options = {}) {
  if (!element) return;
  const { className = 'pulse', duration = 1200 } = options;
  element.classList.add(className);
  setTimeout(() => element.classList.remove(className), duration);
}
