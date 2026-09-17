// Dark mode toggle persists
(function () {
  const root = document.documentElement;
  const body = document.body;
  const key = "predictmygrade-theme";
  const toggles = document.querySelectorAll("[data-theme-toggle]");

  function updateToggleVisual(btn, isDark) {
    if (!btn) return;
    btn.setAttribute("aria-label", isDark ? "Switch to light mode" : "Switch to dark mode");
    const textLabel = btn.querySelector("[data-theme-toggle-label]");
    if (textLabel) {
      textLabel.textContent = isDark ? "Dark mode" : "Light mode";
    }
    const symbol = btn.querySelector("[data-theme-toggle-icon]");
    if (symbol) {
      symbol.textContent = isDark ? "☾" : "☼";
    }
    const icon = btn.querySelector("i");
    if (icon) {
      icon.classList.toggle("fa-moon", !isDark);
      icon.classList.toggle("fa-sun", isDark);
    }
  }

  function applyTheme(theme, { persist = true } = {}) {
    const resolved = theme === "light" ? "light" : "dark";
    const isDark = resolved !== "light";
    root.classList.remove("theme-dark", "theme-light");
    root.classList.add(isDark ? "theme-dark" : "theme-light");
    if (body) {
      body.classList.remove("theme-dark", "theme-light");
      body.classList.add(isDark ? "theme-dark" : "theme-light");
    }
    toggles.forEach((btn) => updateToggleVisual(btn, isDark));
    if (persist) {
      try {
        localStorage.setItem(key, resolved);
      } catch (error) {
        console.warn("PredictMyGrade: unable to persist theme", error);
      }
    }
  }

  const initial =
    (root.classList.contains("theme-light") || body?.classList.contains("theme-light")
      ? "light"
      : "dark");
  applyTheme(initial, { persist: false });

  const toggleTheme = async () => {
    const previousTheme = root.classList.contains("theme-light") ? "light" : "dark";
    const nextTheme = root.classList.contains("theme-light") ? "dark" : "light";
    applyTheme(nextTheme);
    const select = document.getElementById('themeSelect');
    if (select) select.value = nextTheme;
    const url = root.dataset.settingsUrl;
    if (!url) return;
    toggles.forEach(btn => { btn.disabled = true; });
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content },
        body: new URLSearchParams({action: 'theme', theme: nextTheme}),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error('Theme was not saved. Please try again.');
    } catch (error) {
      applyTheme(previousTheme);
      if (select) select.value = previousTheme;
      window.showToast(error.message, 'error');
    } finally {
      toggles.forEach(btn => { btn.disabled = false; });
    }
  };

  toggles.forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.preventDefault();
      toggleTheme();
    });
  });
})();

// ML demo on dashboard
(function () {
  const forms = document.querySelectorAll("[data-ml-form]");
  if (!forms.length) return;

  forms.forEach((form) => {
    const container = form.closest("[data-ml-card]") || form.parentElement || form;
    const avgField = container?.querySelector("[data-avg-input]");
    const creditsField = container?.querySelector("[data-credits-input]");
    const resultNode = container?.querySelector("[data-ml-result]");
    if (!avgField || !creditsField || !resultNode) {
      return;
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();

      const avg = parseFloat(avgField.value);
      const credits = parseFloat(creditsField.value);
      if (Number.isNaN(avg) || Number.isNaN(credits)) {
        resultNode.textContent = "Enter both average and credits to run a prediction.";
        return;
      }

      resultNode.textContent = "Predicting...";
      avgField.disabled = true;
      creditsField.disabled = true;
      const submitBtn = form.querySelector("button");
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.dataset.originalText = submitBtn.dataset.originalText || submitBtn.textContent;
        submitBtn.textContent = "Predicting...";
      }
      try {
        const body = new URLSearchParams();
        body.set("avg_so_far", avg.toString());
        body.set("credits_done", credits.toString());
        body.set("difficulty_index", "0.6");
        body.set("performance_variance", "0.3");
        body.set("engagement_score", "0.7");

        const headers = {};
        const csrfToken = document.querySelector('[name="csrfmiddlewaretoken"]')?.value || getCookie("csrftoken");
        if (csrfToken) {
          headers["X-CSRFToken"] = csrfToken;
        }

        const res = await fetch("/ai/predict/", {
          method: "POST",
          headers,
          body,
        });
        let payload = null;
        try {
          payload = await res.json();
        } catch {
          payload = null;
        }
        if (!res.ok) {
          const message =
            payload?.limit_note ||
            payload?.error ||
            `Request failed with status ${res.status}`;
          resultNode.textContent = message;
          if (payload?.limit_note && window.showToast) {
            window.showToast(payload.limit_note, "info");
          }
          return;
        }
        if (payload?.error) {
          resultNode.textContent = `Prediction unavailable (${payload.error})`;
          if (payload.limit_note && window.showToast) {
            window.showToast(payload.limit_note, "info");
          }
          return;
        }
        resultNode.innerHTML = `
          <strong>${payload.predicted_classification}</strong><br>
          Predicted Average: ${payload.predicted_average}%<br>
          Confidence: ${payload.confidence ?? "n/a"}%<br>
          Model: ${payload.mode || "Adaptive model"}
        `;
        if (payload?.limit_note) {
          const note = document.createElement("p");
          note.className = "muted small mt-0-4";
          note.textContent = payload.limit_note;
          resultNode.appendChild(note);
          if (window.showToast) {
            window.showToast(payload.limit_note, "info");
          }
        }
      } catch (err) {
        resultNode.textContent = "Prediction error.";
      } finally {
        avgField.disabled = false;
        creditsField.disabled = false;
        const submitBtnReset = form.querySelector("button");
        if (submitBtnReset) {
          const label = submitBtnReset.dataset.originalText || "Run Prediction";
          submitBtnReset.disabled = false;
          submitBtnReset.textContent = label;
        }
      }
    });
  });

  // CSRF util
  function getCookie(name) {
    const m = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
    if (m) return m[2];
  }
})();

// === Toastify helper ===
function showToast(msg, type = "info") {
  const colors = {
    info: "linear-gradient(to right, #6366f1, #8b5cf6)",
    success: "linear-gradient(to right, #22c55e, #16a34a)",
    error: "linear-gradient(to right, #ef4444, #dc2626)",
  };
  if (typeof Toastify !== "function") {
    const notification = document.createElement("div");
    notification.setAttribute("role", type === "error" ? "alert" : "status");
    notification.style.cssText = "position:fixed;bottom:1rem;right:1rem;z-index:10000;max-width:min(28rem,calc(100vw - 2rem));padding:1rem;border-radius:8px;background:#20243b;color:white;box-shadow:0 8px 24px #0005";
    const message = document.createElement("span");
    message.textContent = msg;
    const close = document.createElement("button");
    close.type = "button";
    close.textContent = "Dismiss";
    close.style.cssText = "margin-left:1rem;color:inherit;background:transparent;border:1px solid currentColor;border-radius:4px;padding:.25rem .5rem";
    close.addEventListener("click", () => notification.remove());
    notification.append(message, close);
    document.body.appendChild(notification);
    if (type !== "error") window.setTimeout(() => notification.remove(), 5000);
    return;
  }
  Toastify({
    text: msg,
    duration: 3200,
    stopOnFocus: false,
    close: true,
    gravity: "top",
    position: "right",
    style: {
      background: colors[type] || colors.info,
      borderRadius: "8px",
      fontWeight: "500",
      boxShadow: "0 12px 24px rgba(8,7,19,0.5)",
    },
  }).showToast();
}
window.showToast = showToast;
