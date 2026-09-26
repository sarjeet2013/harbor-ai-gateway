/* Harbor mini Admin UI — vanilla JS, no build step */
(function () {
  "use strict";

  const state = {
    cheap: [],
    expensive: [],
  };

  function $(id) {
    return document.getElementById(id);
  }

  function toast(msg, ok) {
    const el = $("toast");
    el.textContent = msg;
    el.className = "show " + (ok ? "ok" : "err");
    clearTimeout(toast._t);
    toast._t = setTimeout(function () {
      el.className = "";
    }, 6000);
  }

  function setBadge(el, isSet) {
    el.textContent = isSet ? "set" : "unset";
    el.className = "badge " + (isSet ? "set" : "unset");
  }

  function modelBlock(pool, index, row) {
    const wrap = document.createElement("div");
    wrap.className = "model-block";
    wrap.dataset.pool = pool;
    wrap.dataset.index = String(index);
    wrap.innerHTML =
      '<div class="title"><span>' +
      pool +
      " #" +
      (index + 1) +
      '</span><button type="button" class="danger-outline btn-remove">Remove</button></div>' +
      '<div class="row">' +
      '<div><label>name (alias)</label><input type="text" data-f="name" /></div>' +
      '<div><label>litellm_model</label><input type="text" data-f="litellm_model" /></div>' +
      '<div><label>api_base</label><input type="text" data-f="api_base" placeholder="http://host:11434" /></div>' +
      "</div>" +
      '<div class="row tight">' +
      '<label class="check"><input type="checkbox" data-f="enabled" /> enabled</label>' +
      '<label class="check"><input type="checkbox" data-f="think_false" /> think: false (chat)</label>' +
      "</div>";

    wrap.querySelector('[data-f="name"]').value = row.name || "";
    wrap.querySelector('[data-f="litellm_model"]').value = row.litellm_model || "";
    wrap.querySelector('[data-f="api_base"]').value = row.api_base || "";
    wrap.querySelector('[data-f="enabled"]').checked = row.enabled !== false;
    wrap.querySelector('[data-f="think_false"]').checked = !!row.think_false;

    wrap.querySelector(".btn-remove").addEventListener("click", function () {
      state[pool].splice(index, 1);
      renderModels();
    });
    return wrap;
  }

  function readModelBlocks(pool) {
    const host = $(pool + "-models");
    const rows = [];
    host.querySelectorAll(".model-block").forEach(function (block) {
      rows.push({
        name: block.querySelector('[data-f="name"]').value.trim(),
        litellm_model: block.querySelector('[data-f="litellm_model"]').value.trim() || null,
        api_base: block.querySelector('[data-f="api_base"]').value.trim() || null,
        enabled: block.querySelector('[data-f="enabled"]').checked,
        think_false: block.querySelector('[data-f="think_false"]').checked,
      });
    });
    return rows;
  }

  function renderModels() {
    ["cheap", "expensive"].forEach(function (pool) {
      const host = $(pool + "-models");
      host.innerHTML = "";
      state[pool].forEach(function (row, i) {
        host.appendChild(modelBlock(pool, i, row));
      });
    });
  }

  function renderStatus(data) {
    const s = data.status || {};
    const budget = s.budget || {};
    const cache = s.cache || {};
    const items = [
      ["upstream_mode", s.upstream_mode],
      ["router", s.router_strategy],
      ["budget spent", budget.spent_usd != null ? "$" + Number(budget.spent_usd).toFixed(4) : "—"],
      ["budget remaining", budget.remaining_usd != null ? "$" + Number(budget.remaining_usd).toFixed(4) : "—"],
      ["cache mode", cache.mode],
      ["cache entries", cache.entries != null ? String(cache.entries) : "—"],
      ["cache enabled", String(cache.enabled)],
    ];
    const grid = $("status-grid");
    grid.innerHTML = "";
    items.forEach(function (pair) {
      const div = document.createElement("div");
      div.className = "stat";
      div.innerHTML =
        '<div class="label">' +
        pair[0] +
        '</div><div class="value">' +
        (pair[1] == null ? "—" : pair[1]) +
        "</div>";
      grid.appendChild(div);
    });

    // Model alias summary stats
    (data.models.cheap || []).concat(data.models.expensive || []).forEach(function (m) {
      const div = document.createElement("div");
      div.className = "stat";
      div.innerHTML =
        '<div class="label">' +
        (m.name || "?") +
        '</div><div class="value">' +
        (m.api_base || m.litellm_model || "—") +
        "</div>";
      grid.appendChild(div);
    });
  }

  function fillForm(data) {
    $("config-path").textContent = data.config_path || "";
    renderStatus(data);

    state.cheap = (data.models && data.models.cheap) || [];
    state.expensive = (data.models && data.models.expensive) || [];
    renderModels();

    const c = data.cache || {};
    $("cache-enabled").value = String(!!c.enabled);
    $("cache-mode").value = c.mode || "exact";
    $("cache-threshold").value = c.similarity_threshold != null ? c.similarity_threshold : 0.92;
    $("cache-scope").value = c.semantic_scope || "full";
    $("cache-embed-model").value = c.embedding_model || "";
    $("cache-embed-base").value = c.embedding_api_base || "";

    const r = data.router || {};
    const j = r.judge || {};
    $("router-strategy").value = r.strategy || "judge_then_heuristic";
    $("judge-enabled").value = String(!!j.enabled);
    $("judge-model").value = j.litellm_model || "";
    $("judge-base").value = j.api_base || "";
    $("judge-timeout").value = j.timeout_ms != null ? j.timeout_ms : 400;
    $("judge-conf").value = j.min_confidence != null ? j.min_confidence : 0.55;

    const secrets = data.secrets || {};
    setBadge($("badge-openai"), !!secrets.OPENAI_API_KEY);
    setBadge($("badge-xai"), !!secrets.XAI_API_KEY);
    $("key-openai").value = "";
    $("key-xai").value = "";
    $("apply-hint").textContent = data.restart_note || "";
  }

  async function loadSettings() {
    const res = await fetch("/admin/api/settings", { credentials: "same-origin" });
    if (!res.ok) throw new Error("GET settings failed: " + res.status);
    const data = await res.json();
    fillForm(data);
  }

  function collectPayload() {
    // Re-sync from DOM (user may have edited without re-render)
    state.cheap = readModelBlocks("cheap");
    state.expensive = readModelBlocks("expensive");

    const secrets = {};
    const openai = $("key-openai").value.trim();
    const xai = $("key-xai").value.trim();
    if (openai) secrets.OPENAI_API_KEY = openai;
    if (xai) secrets.XAI_API_KEY = xai;

    return {
      models: {
        cheap: state.cheap.filter(function (r) {
          return r.name;
        }),
        expensive: state.expensive.filter(function (r) {
          return r.name;
        }),
      },
      cache: {
        enabled: $("cache-enabled").value === "true",
        mode: $("cache-mode").value,
        similarity_threshold: Number($("cache-threshold").value),
        embedding_model: $("cache-embed-model").value.trim() || null,
        embedding_api_base: $("cache-embed-base").value.trim() || null,
        semantic_scope: $("cache-scope").value,
      },
      router: {
        strategy: $("router-strategy").value,
        judge: {
          enabled: $("judge-enabled").value === "true",
          litellm_model: $("judge-model").value.trim(),
          api_base: $("judge-base").value.trim(),
          timeout_ms: Number($("judge-timeout").value),
          min_confidence: Number($("judge-conf").value),
        },
      },
      secrets: secrets,
    };
  }

  async function applySettings() {
    const body = collectPayload();
    const res = await fetch("/admin/api/settings", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(function () {
      return {};
    });
    if (!res.ok) {
      const detail = data.detail || res.statusText || "save failed";
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    setBadge($("badge-openai"), !!(data.secrets && data.secrets.OPENAI_API_KEY));
    setBadge($("badge-xai"), !!(data.secrets && data.secrets.XAI_API_KEY));
    $("key-openai").value = "";
    $("key-xai").value = "";
    $("apply-hint").textContent = data.message || "";
    toast(data.message || "Saved", true);
    // Refresh status from disk-backed GET
    await loadSettings();
  }

  $("add-cheap").addEventListener("click", function () {
    state.cheap = readModelBlocks("cheap");
    state.cheap.push({
      name: "",
      litellm_model: "ollama_chat/qwen3:8b",
      api_base: "http://127.0.0.1:11434",
      enabled: true,
      think_false: true,
    });
    renderModels();
  });
  $("add-expensive").addEventListener("click", function () {
    state.expensive = readModelBlocks("expensive");
    state.expensive.push({
      name: "openai-expensive",
      litellm_model: "openai/gpt-4o-mini",
      api_base: null,
      enabled: true,
      think_false: false,
    });
    renderModels();
  });
  $("btn-reload").addEventListener("click", function () {
    loadSettings().then(
      function () {
        toast("Reloaded", true);
      },
      function (e) {
        toast(String(e.message || e), false);
      }
    );
  });
  $("btn-apply").addEventListener("click", function () {
    applySettings().catch(function (e) {
      toast(String(e.message || e), false);
    });
  });

  loadSettings().catch(function (e) {
    toast(String(e.message || e), false);
  });
})();
