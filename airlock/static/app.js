"use strict";

const TOKEN_KEY = "airlock_token";
const REFRESH_KEY = "airlock_refresh";
const REFRESH_DEFAULT = "5";

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  status: null,
  rois: [],
  snapshot: null,
  drawing: null,
  activeTab: "live",
  signalRoi: "",
  tuneRoi: "*",
  suggest: null,
  maxRois: 12,
  camCaps: null,
  streamActive: false,
  params: null,
  chartsTimer: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function apiUrl(path) {
  if (!state.token) return path;
  return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(state.token);
}

function cacheBuster(url) {
  return url + (url.includes("?") ? "&" : "?") + "t=" + Date.now();
}

async function api(path, options = {}) {
  const opts = Object.assign({}, options);
  opts.headers = Object.assign({}, opts.headers);
  if (state.token) opts.headers["X-Airlock-Token"] = state.token;
  if (opts.body && !opts.headers["Content-Type"]) opts.headers["Content-Type"] = "application/json";
  const res = await fetch(path, opts);
  if (res.status === 401) {
    askToken();
    throw new Error("Требуется токен доступа");
  }
  return res;
}

async function jget(path) {
  const res = await api(path);
  if (!res.ok) throw new Error((await safeJson(res)).error || res.statusText);
  return res.json();
}

async function jpost(path, body) {
  const res = await api(path, { method: "POST", body: JSON.stringify(body || {}) });
  const data = await safeJson(res);
  if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
  return data;
}

async function safeJson(res) {
  try { return await res.json(); } catch (e) { return {}; }
}

function askToken() {
  const value = prompt("Токен доступа к интерфейсу:");
  if (value === null) return;
  state.token = value.trim();
  localStorage.setItem(TOKEN_KEY, state.token);
  location.reload();
}

function fmtAge(seconds) {
  if (seconds === null || seconds === undefined) return "00:00:00";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = Math.floor(seconds % 60);
  return String(hours).padStart(2, "0") + ":" + String(minutes).padStart(2, "0") + ":" + String(secs).padStart(2, "0");
}

function fmtNum(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Number(value).toFixed(digits);
}

function fmtClock(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function setStateBadge(ok, text) {
  const el = $("#conn");
  el.className = "badge " + (ok ? "badge-ok" : "badge-err");
  el.textContent = text;
}

function showMsg(sel, text, kind) {
  const el = $(sel);
  if (!el) return;
  el.textContent = text || "";
  el.className = "msg" + (kind ? " msg-" + kind : "");
}

document.addEventListener("DOMContentLoaded", init);

function init() {
  bindTabs();
  bindStream();
  bindButtons();
  buildSettingsForm();
  bindRoiEditor();
  bindCamera();
  refreshStatus();
  setInterval(refreshStatus, 1000);
  setInterval(() => { if (state.activeTab === "log") refreshEvents(); }, 2000);
  setupRefreshRate();
  $("#btn-token").addEventListener("click", askToken);
  window.addEventListener("load", () => setTimeout(updateStream, 50));
}

function bindTabs() {
  $$(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab-btn").forEach((b) => b.classList.remove("active"));
      $$(".tab").forEach((t) => t.classList.remove("active"));
      btn.classList.add("active");
      const name = btn.dataset.tab;
      state.activeTab = name;
      $("#tab-" + name).classList.add("active");
      if (name === "charts") refreshCharts();
      if (name === "log") refreshEvents();
      if (name === "roi") loadSnapshot();
      if (name === "camera") loadCamera();
      if (name === "tuning") refreshTuning();
      updateStream();
    });
  });
}

function bindStream() {
  $("#stream-on").addEventListener("change", updateStream);
}

function updateStream() {
  const img = $("#stream");
  const on = $("#stream-on").checked && state.activeTab === "live";
  if (on && !state.streamActive) {
    img.src = cacheBuster(apiUrl("/stream"));
    state.streamActive = true;
    img.onload = () => { $("#stream-msg").hidden = true; };
    img.onerror = () => {
      state.streamActive = false;
      const msg = $("#stream-msg");
      msg.hidden = false;
      msg.textContent = "Поток недоступен. Проверьте камеру и перезапустите службу.";
    };
  } else if (!on && state.streamActive) {
    img.src = "";
    state.streamActive = false;
    const msg = $("#stream-msg");
    msg.hidden = false;
    msg.textContent = "Поток выключен";
  }
}

function bindButtons() {
  $("#btn-reset").addEventListener("click", async () => {
    if (!confirm("Сбросить все счётчики бульков?")) return;
    try {
      await jpost("/api/reset");
      showMsg("#roi-msg", "", "");
      refreshStatus();
    } catch (e) { alert(e.message); }
  });

  $("#btn-save-rois").addEventListener("click", saveRois);
  $("#btn-clear-rois").addEventListener("click", () => { state.rois = []; renderRoiEditor(); });
  $("#btn-reload-snap").addEventListener("click", loadSnapshot);

  $("#btn-apply").addEventListener("click", applySettings);
  $("#btn-suggest").addEventListener("click", suggestThresholds);
  $("#btn-reset-override").addEventListener("click", resetOverride);
  $("#tune-roi").addEventListener("change", (e) => {
    state.tuneRoi = e.target.value;
    fillForm(tuneParams());
    renderTargetHint();
  });

  $("#signal-roi").addEventListener("change", (e) => { state.signalRoi = e.target.value; refreshCharts(); });
  $("#timeline-range").addEventListener("change", refreshCharts);
}

async function refreshStatus() {
  try {
    const data = await jget("/api/status");
    state.status = data;
    state.params = data.params;
    if (data.max_rois) state.maxRois = data.max_rois;
    renderStatus(data);
    setStateBadge(true, "онлайн");
    const msg = $("#stream-msg");
    if (state.streamActive && msg && !msg.hidden && data.camera && data.camera.ok) msg.hidden = true;
  } catch (e) {
    setStateBadge(false, "нет связи");
  }
}

function renderStatus(data) {
  $("#sum-cam").textContent = "камера " + fmtNum(data.camera.read_fps, 0) + " к/с · " +
    (data.camera.width || "?") + "×" + (data.camera.height || "?");
  $("#sum-proc").textContent = "обработка " + fmtNum(data.app.process_fps, 0) + " к/с";
  $("#sum-session").textContent = "с " + fmtClock(data.app.since) + " · сессия №" + data.app.sessions;
  $("#version").textContent = "airlock " + data.version;

  renderAlerts(data);
  renderZoneStats(data.rois || []);
  renderSysInfo(data);
  syncRoiSelects(data.rois || []);
}

function renderAlerts(data) {
  const alerts = $("#alerts");
  alerts.innerHTML = "";
  (data.alerts || []).forEach((a) => {
    const div = document.createElement("div");
    div.className = "alert alert-" + (a.level === "error" ? "error" : "warning");
    div.textContent = a.message;
    alerts.appendChild(div);
  });
  if (data.app.warmup) {
    const div = document.createElement("div");
    div.className = "alert alert-warning";
    div.textContent = "Прогрев модели фона, осталось кадров: " + data.app.warmup_frames_left +
      ". Подсчёт начнётся автоматически.";
    alerts.appendChild(div);
  }
}

function renderZoneStats(rois) {
  const wrap = $("#zone-stats");
  const empty = $("#zone-stats-empty");
  empty.hidden = rois.length > 0;
  wrap.innerHTML = "";
  rois.forEach((r) => {
    const rates = r.rates || {};
    const ferm = r.fermentation || { level: "none", label: "—" };
    const card = document.createElement("div");
    card.className = "zone-card";
    card.style.borderLeftColor = stateColor(r.state);
    const thr = r.params ? r.params.on_threshold : 0.004;
    const pct = Math.min(100, (r.motion / 0.06) * 100);
    const thrPos = Math.min(100, (thr / 0.06) * 100);
    const day24 = Math.round(rates.day24 || 0);
    card.innerHTML =
      '<div class="zone-card-head">' +
        '<span class="zone-name" title="' + esc(r.name) + '">' + esc(r.name) + "</span>" +
        '<span class="pill pill-' + r.state + '">' + r.state + "</span>" +
        (r.has_override ? '<span class="zone-badge" title="Свои пороги для этой зоны">override</span>' : "") +
      "</div>" +
      '<div class="zone-ferment ferm-' + ferm.level + '" title="Бульков за сутки: ' + day24 +
        ', за час: ' + Math.round(rates.bph || 0) + '">' +
        '<span class="m-label">брожение</span><span class="ferm-label">' + esc(ferm.label) + "</span>" +
      "</div>" +
      '<div class="zone-metrics">' +
        '<div><span class="m-label" title="Бульков за последний час (с историей)">бульков в час</span><span class="m-value">' + Math.round(rates.bph || 0) + "</span></div>" +
        '<div><span class="m-label">последний</span><span class="m-value">' + fmtAge(r.last_event_age_s) + "</span></div>" +
      "</div>" +
      '<div class="bar" title="сигнал ' + Number(r.motion).toFixed(5) + ', порог ' + Number(thr).toFixed(5) + '">' +
        '<i style="width:' + pct.toFixed(1) + '%"></i><u style="left:' + thrPos.toFixed(1) + '%"></u>' +
      "</div>";
    card.addEventListener("click", () => {
      state.signalRoi = r.name;
      if (state.activeTab === "charts") refreshCharts();
    });
    wrap.appendChild(card);
  });
}

function stateColor(state) {
  return { idle: "#46c08b", rising: "#f0b429", falling: "#3cd0d0",
           disturbed: "#ef5f5f", warmup: "#8b9bb0" }[state] || "#46c08b";
}

function esc(text) {
  return String(text == null ? "" : text).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function syncRoiSelects(rois) {
  const names = rois.map((r) => r.name);

  const signal = $("#signal-roi");
  if (signal) {
    if (signal.dataset.names !== JSON.stringify(names)) {
      signal.dataset.names = JSON.stringify(names);
      signal.innerHTML = names.map((n) => '<option value="' + esc(n) + '">' + esc(n) + "</option>").join("");
    }
    if (!state.signalRoi || names.indexOf(state.signalRoi) < 0) state.signalRoi = names[0] || "";
    signal.value = state.signalRoi;
  }

  const tune = $("#tune-roi");
  if (tune) {
    if (tune.dataset.names !== JSON.stringify(names)) {
      tune.dataset.names = JSON.stringify(names);
      tune.innerHTML = '<option value="*">★ базовые (все зоны)</option>' +
        names.map((n) => '<option value="' + esc(n) + '">' + esc(n) + "</option>").join("");
    }
    if (state.tuneRoi !== "*" && names.indexOf(state.tuneRoi) < 0) state.tuneRoi = "*";
    tune.value = state.tuneRoi;
  }
}

function renderSysInfo(data) {
  const el = $("#sysinfo");
  const rois = data.rois || [];
  const overrides = rois.filter((r) => r.has_override).length;
  const rows = [
    ["Аптайм процесса", fmtAge(data.app.uptime_s)],
    ["Кадров обработано", data.app.frames_processed],
    ["Зон / с override", rois.length + " / " + overrides],
    ["Переподключений камеры", data.camera.reconnects],
    ["Возраст кадра", fmtAge(data.camera.last_frame_age_s)],
    ["Помех (засветка)", rois.reduce((s, r) => s + (r.disturbances || 0), 0)],
    ["Модель фона", data.params.vision.background],
    ["Базовый порог on / off", data.params.detector.on_threshold.toFixed(5) + " / " + data.params.detector.off_threshold.toFixed(5)],
    ["Порог простоя", data.params.alerts.stall_minutes > 0 ? data.params.alerts.stall_minutes + " мин" : "выключен"],
  ];
  el.innerHTML = rows.map((r) => "<dt>" + r[0] + "</dt><dd>" + r[1] + "</dd>").join("");
}

const FIELDS = [
  { group: "detector", key: "on_threshold", label: "on_threshold", min: 0.0002, max: 0.08, step: 0.0002, digits: 5, desc: "Доля пикселей зоны, при которой движение считается началом булька." },
  { group: "detector", key: "off_threshold", label: "off_threshold", min: 0, max: 0.05, step: 0.0001, digits: 5, desc: "Уровень, ниже которого сигнал должен упасть для завершения булька." },
  { group: "detector", key: "drop_ratio", label: "drop_ratio", min: 0, max: 0.95, step: 0.05, digits: 2, desc: "Спад от пика (доля), тоже завершает бульк. Помогает при частом брожении." },
  { group: "detector", key: "min_off_frames", label: "min_off_frames", min: 1, max: 15, step: 1, digits: 0, desc: "Сколько кадров сигнал должен быть ниже порога, чтобы засчитать бульк." },
  { group: "detector", key: "min_peak_frames", label: "min_peak_frames", min: 1, max: 10, step: 1, digits: 0, desc: "Минимум кадров выше on_threshold. Ставьте 2, если лезет шум." },
  { group: "detector", key: "refractory_s", label: "refractory_s", min: 0, max: 3, step: 0.01, digits: 2, desc: "Минимальная пауза между бульками. Ограничивает максимальный темп счёта." },
  { group: "detector", key: "max_hump_s", label: "max_hump_s", min: 0.3, max: 30, step: 0.1, digits: 1, desc: "Принудительно закрыть событие длиннее этого — защита от залипания." },
  { group: "detector", key: "saturate_ratio", label: "saturate_ratio", min: 0.02, max: 1, step: 0.01, digits: 2, desc: "Если двигается больше этой доли зоны — это не бульк, а помеха (свет, толчок)." },
  { group: "vision", key: "background", label: "background", type: "select", options: ["mog2", "mean"], desc: "mog2 устойчивее к дрейфу освещения, mean проще и легче для Pi 3." },
  { group: "vision", key: "mog2_threshold", label: "mog2_threshold", min: 4, max: 200, step: 1, digits: 0, desc: "Чувствительность MOG2. Меньше — чувствительнее и шумнее." },
  { group: "vision", key: "mog2_learning_rate", label: "mog2_learning_rate", min: -1, max: 0.2, step: 0.001, digits: 4, desc: "Скорость адаптации фона. -1 — автоматическая." },
  { group: "vision", key: "despeckle", label: "despeckle", min: 0, max: 9, step: 2, digits: 0, desc: "Медианный фильтр маски (0 — выключен). Убирает одиночные шумные пиксели." },
  { group: "vision", key: "process_scale", label: "process_scale", min: 0.2, max: 1, step: 0.05, digits: 2, desc: "Уменьшение зоны перед анализом. 0.5 заметно экономит CPU на Pi 3." },
  { group: "vision", key: "warmup_frames", label: "warmup_frames", min: 0, max: 600, step: 10, digits: 0, desc: "Кадров прогрева модели фона перед началом подсчёта." },
  { group: "alerts", key: "stall_minutes", label: "stall_minutes", min: 0, max: 720, step: 5, digits: 0, desc: "Тревога, если нет бульков дольше N минут. 0 — выключено." },
];

function buildSettingsForm() {
  const form = $("#settings-form");
  form.innerHTML = "";
  FIELDS.forEach((f) => {
    const wrap = document.createElement("div");
    wrap.className = "field";
    const id = "f-" + f.key;
    let control;
    if (f.type === "select") {
      control = '<select id="' + id + '">' + f.options.map((o) => '<option value="' + o + '">' + o + "</option>").join("") + "</select>";
    } else {
      control =
        '<div class="row">' +
        '<input type="range" id="' + id + '-r" min="' + f.min + '" max="' + f.max + '" step="' + f.step + '">' +
        '<input type="number" id="' + id + '" min="' + f.min + '" max="' + f.max + '" step="' + f.step + '">' +
        "</div>";
    }
    wrap.innerHTML = '<label for="' + id + '">' + f.label + "</label>" + control + '<span class="desc">' + f.desc + "</span>";
    form.appendChild(wrap);

    if (f.type !== "select") {
      const range = $("#" + id + "-r");
      const num = $("#" + id);
      range.addEventListener("input", () => { num.value = range.value; });
      num.addEventListener("input", () => { range.value = num.value; });
    }
  });
  form.addEventListener("submit", (e) => e.preventDefault());
}

function fillForm(params) {
  FIELDS.forEach((f) => {
    const src = params[f.group] || {};
    const value = src[f.key];
    if (value === undefined) return;
    const el = $("#f-" + f.key);
    if (!el) return;
    el.value = f.type === "select" ? String(value) : Number(value).toFixed(f.digits);
    if (f.type !== "select") {
      const range = $("#f-" + f.key + "-r");
      if (range) range.value = value;
    }
  });
}

function readForm() {
  const patch = { detector: {}, vision: {}, alerts: {} };
  FIELDS.forEach((f) => {
    const el = $("#f-" + f.key);
    if (!el) return;
    if (f.type === "select") {
      patch[f.group][f.key] = el.value;
      return;
    }
    const value = parseFloat(el.value);
    if (!Number.isNaN(value)) patch[f.group][f.key] = value;
  });
  Object.keys(patch).forEach((k) => { if (!Object.keys(patch[k]).length) delete patch[k]; });
  patch.persist = true;
  return patch;
}

function selectedZoneParams() {
  const rois = (state.status && state.status.rois) || [];
  if (state.tuneRoi && state.tuneRoi !== "*") {
    const r = rois.find((x) => x.name === state.tuneRoi);
    if (r && r.params) return r.params;
  }
  return state.params ? state.params.detector : {};
}

function tuneParams() {
  const g = state.params || { vision: {}, alerts: {} };
  return { detector: selectedZoneParams(), vision: g.vision, alerts: g.alerts };
}

function renderTargetHint() {
  const el = $("#tune-target");
  if (!el) return;
  if (state.tuneRoi === "*") {
    el.textContent = "Правка применяется ко всем зонам, у которых нет своего override.";
    el.className = "msg";
    return;
  }
  const r = ((state.status && state.status.rois) || []).find((x) => x.name === state.tuneRoi);
  el.textContent = "Зона «" + state.tuneRoi + "»" +
    (r && r.has_override ? " — со своими порогами (override)." : " — наследует базовые пороги.");
  el.className = "msg" + (r && r.has_override ? " msg-ok" : "");
}

async function refreshTuning() {
  if (!state.status) {
    try { const d = await jget("/api/status"); state.status = d; state.params = d.params; } catch (e) { return; }
  }
  fillForm(tuneParams());
  renderTargetHint();
  renderStatsInfo();
  renderCamInfo();
  if (state.suggest) renderSuggestTable(state.suggest);
}

function renderStatsInfo() {
  const el = $("#stats-info");
  const rois = (state.status && state.status.rois) || [];
  if (!rois.length) { el.innerHTML = "<dt>Нет зон</dt><dd>—</dd>"; return; }
  const rows = [];
  rois.forEach((r) => {
    const s = r.stats || {};
    rows.push(["Шум " + r.name, "p50 " + fmtNum(s.noise_p50, 5) + " · p99 " + fmtNum(s.noise_p99, 5)]);
    rows.push(["Пики " + r.name, "медиана " + fmtNum(s.peak_median, 5) + " · max " + fmtNum(s.peak_max, 5)]);
    rows.push(["Отсчётов " + r.name, "шум " + ((s.samples || {}).noise || 0) + " · пики " + ((s.samples || {}).peaks || 0)]);
  });
  el.innerHTML = rows.map((r) => "<dt>" + r[0] + "</dt><dd>" + r[1] + "</dd>").join("");
}

function renderCamInfo() {
  const el = $("#cam-info");
  const c = (state.status && state.status.camera) || {};
  const rows = [
    ["Устройство", c.device || "—"],
    ["Фактический размер", (c.width || "?") + "×" + (c.height || "?")],
    ["Частота чтения", fmtNum(c.read_fps, 1) + " к/с"],
    ["Формат", c.fourcc || "—"],
    ["Кадров получено", c.frames || 0],
    ["Ошибка", c.error || "нет"],
  ];
  el.innerHTML = rows.map((r) => "<dt>" + r[0] + "</dt><dd>" + r[1] + "</dd>").join("");
}

async function applySettings() {
  try {
    const patch = readForm();
    patch.roi = state.tuneRoi;
    const data = await jpost("/api/settings", patch);
    state.params = data.params;
    await refreshStatus();
    fillForm(tuneParams());
    renderTargetHint();
    showMsg("#suggest-out",
      "Применено к " + (state.tuneRoi === "*" ? "базовым настройкам" : "зоне «" + state.tuneRoi + "»") + " и сохранено.", "ok");
  } catch (e) {
    showMsg("#suggest-out", e.message, "err");
  }
}

async function resetOverride() {
  if (state.tuneRoi === "*") { showMsg("#suggest-out", "Сначала выберите конкретную зону.", "err"); return; }
  try {
    await jpost("/api/roi/params/reset", { roi: state.tuneRoi, persist: true });
    await refreshStatus();
    fillForm(tuneParams());
    renderTargetHint();
    showMsg("#suggest-out", "Override снят — зона снова наследует базовые пороги.", "ok");
  } catch (e) {
    showMsg("#suggest-out", e.message, "err");
  }
}

async function suggestThresholds() {
  try {
    const data = await jpost("/api/suggest", { apply: false });
    state.suggest = data.by_roi;
    renderSuggestTable(state.suggest);
    const sel = state.tuneRoi !== "*" ? state.suggest[state.tuneRoi] : Object.values(state.suggest)[0];
    if (sel && (sel.noise_samples || sel.peak_samples)) {
      $("#f-on_threshold").value = sel.on_threshold.toFixed(5);
      $("#f-off_threshold").value = sel.off_threshold.toFixed(5);
      $("#f-on_threshold-r").value = sel.on_threshold;
      $("#f-off_threshold-r").value = sel.off_threshold;
    }
    showMsg("#suggest-out", "Подобрано независимо для зон: " + Object.keys(state.suggest).length +
      ". Нажмите «применить» у нужной строки.", "ok");
  } catch (e) {
    showMsg("#suggest-out", e.message, "err");
  }
}

function renderSuggestTable(byRoi) {
  const el = $("#suggest-table");
  if (!el) return;
  const names = Object.keys(byRoi || {});
  if (!names.length) { el.innerHTML = ""; return; }
  let html = "";
  if (names.length > 1) {
    html += "<div class='actions' style='margin-top:8px'><button class='primary' id='btn-apply-all'>Применить ко всем зонам</button></div>";
  }
  html += "<table><thead><tr><th>зона</th><th>шум p99</th><th>пики медиана</th><th>предл. on</th><th>предл. off</th><th></th></tr></thead><tbody>";
  names.forEach((n) => {
    const s = byRoi[n];
    const noData = !s.noise_samples && !s.peak_samples;
    html += "<tr><td>" + esc(n) + "</td><td>" + fmtNum(s.noise_p99, 5) + "</td><td>" + fmtNum(s.peak_median, 5) +
      "</td><td>" + fmtNum(s.on_threshold, 5) + "</td><td>" + fmtNum(s.off_threshold, 5) + "</td><td>" +
      (noData ? "<span style='color:var(--muted)'>нет данных</span>"
              : "<button class='ghost' data-applyroi='" + esc(n) + "'>применить</button>") +
      "</td></tr>";
  });
  html += "</tbody></table>";
  el.innerHTML = html;
  el.querySelectorAll("button[data-applyroi]").forEach((btn) =>
    btn.addEventListener("click", () => applySuggestOne(btn.dataset.applyroi)));
  const all = el.querySelector("#btn-apply-all");
  if (all) all.addEventListener("click", applySuggestAll);
}

async function applySuggestOne(name) {
  try {
    await jpost("/api/suggest", { apply: true, roi: name });
    await refreshStatus();
    if (state.tuneRoi === name || state.tuneRoi === "*") fillForm(tuneParams());
    renderTargetHint();
    showMsg("#suggest-out", "Пороги применены к зоне «" + name + "».", "ok");
  } catch (e) {
    showMsg("#suggest-out", e.message, "err");
  }
}

async function applySuggestAll() {
  try {
    const data = await jpost("/api/suggest", { apply: true });
    await refreshStatus();
    fillForm(tuneParams());
    renderTargetHint();
    showMsg("#suggest-out", "Пороги применены к зонам: " + (data.applied || []).join(", "), "ok");
  } catch (e) {
    showMsg("#suggest-out", e.message, "err");
  }
}

async function refreshEvents() {
  try {
    const data = await jget("/api/events?limit=150");
    const tbody = $("#events-table tbody");
    tbody.innerHTML = "";
    $("#events-empty").style.display = data.events.length ? "none" : "block";
    data.events.forEach((e) => {
      const tr = document.createElement("tr");
      tr.innerHTML = "<td>" + e.id + "</td><td>" + (e.wall_time || "") + "</td><td>" + e.roi +
        "</td><td>" + Number(e.peak).toFixed(5) + "</td><td>" + Number(e.duration_s).toFixed(2) +
        "</td><td>" + (e.kind === "long" ? "длинное" : "бульк") + "</td>";
      tbody.appendChild(tr);
    });
    $("#btn-csv").href = apiUrl("/api/events.csv");
  } catch (e) { /* тихо */ }
}

function refreshLabel(sec) {
  if (sec < 60) return "каждые " + sec + " с";
  return "каждые " + (sec / 60) + " мин";
}

function setupRefreshRate() {
  const sel = $("#refresh-rate");
  if (!sel) return;
  const valid = Array.from(sel.options).map((o) => o.value);
  const saved = localStorage.getItem(REFRESH_KEY);
  sel.value = valid.indexOf(saved) >= 0 ? saved : REFRESH_DEFAULT;
  sel.addEventListener("change", () => {
    localStorage.setItem(REFRESH_KEY, sel.value);
    applyRefreshRate();
    if (state.activeTab === "charts") refreshCharts();
  });
  const now = $("#btn-refresh-now");
  if (now) now.addEventListener("click", () => refreshCharts());
  applyRefreshRate();
}

function applyRefreshRate() {
  const sel = $("#refresh-rate");
  const sec = parseInt(sel ? sel.value : REFRESH_DEFAULT, 10) || 5;
  if (state.chartsTimer) clearInterval(state.chartsTimer);
  state.chartsTimer = setInterval(() => {
    if (state.activeTab === "charts") refreshCharts();
  }, sec * 1000);
  const hint = $("#refresh-hint");
  if (hint) hint.textContent = "обновление " + refreshLabel(sec);
}

async function refreshCharts() {
  await Promise.all([drawSignal(), drawTimeline()]);
}

function fitCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const logicalH = parseInt(canvas.dataset.logicalH || canvas.getAttribute("height"), 10) || 180;
  canvas.dataset.logicalH = String(logicalH);
  const w = canvas.clientWidth || 600;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(logicalH * dpr);
  canvas.style.height = logicalH + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h: logicalH };
}

async function drawSignal() {
  const canvas = $("#signal-canvas");
  const { ctx, w, h } = fitCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  if (!state.signalRoi) { hint(ctx, w, h, "Нет зон — выделите гидрозатвор"); return; }
  let data;
  try { data = await jget("/api/signal?roi=" + encodeURIComponent(state.signalRoi) + "&limit=900"); }
  catch (e) { return; }
  const pts = data.series || [];
  if (pts.length < 2) { hint(ctx, w, h, "Накопление сигнала…"); return; }

  const on = data.thresholds.on;
  const sat = data.thresholds.saturate;
  const maxV = Math.max(on * 2.5, ...pts.map((p) => p[1])) || 1;
  const t0 = pts[0][0], t1 = pts[pts.length - 1][0];
  const span = Math.max(1e-6, t1 - t0);
  const pad = 6;
  const X = (t) => pad + ((t - t0) / span) * (w - pad * 2);
  const Y = (v) => h - pad - (v / maxV) * (h - pad * 2);

  ctx.strokeStyle = "rgba(70,192,139,.55)";
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(pad, Y(on)); ctx.lineTo(w - pad, Y(on)); ctx.stroke();
  ctx.strokeStyle = "rgba(239,95,95,.45)";
  ctx.beginPath(); ctx.moveTo(pad, Y(sat)); ctx.lineTo(w - pad, Y(sat)); ctx.stroke();
  ctx.setLineDash([]);

  ctx.strokeStyle = "#4ea1ff";
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  pts.forEach((p, i) => { const x = X(p[0]), y = Y(p[1]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.stroke();

  ctx.fillStyle = "#8b9bb0";
  ctx.font = "11px system-ui";
  ctx.fillText("порог on " + on.toFixed(5), pad + 4, Math.max(12, Y(on) - 4));
  ctx.fillText("макс " + maxV.toFixed(4), pad + 4, 14);
  ctx.fillText(fmtAge(span) + " окно", w - 90, h - 8);
}

const CHART_COLORS = ["#4ea1ff", "#46c08b", "#f0b429", "#c78bff", "#3cd0d0", "#ff8f6b", "#ef5f5f", "#9fb0c0"];

function sumValues(obj) {
  return Object.keys(obj || {}).reduce((s, k) => s + (obj[k] || 0), 0);
}

function stepLabel(min) {
  if (min < 60) return min + " мин";
  if (min < 1440) return (min / 60) + " ч";
  return (min / 1440) + " сут";
}

function axisLabel(minute, stepMin) {
  const d = new Date(minute * 1000);
  if (stepMin >= 1440) return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
  if (stepMin >= 60) {
    return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }) +
      " " + d.toLocaleTimeString("ru-RU", { hour: "2-digit" }) + ":00";
  }
  return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

async function drawTimeline() {
  const canvas = $("#timeline-canvas");
  const { ctx, w, h } = fitCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  const minutes = parseInt($("#timeline-range").value, 10);
  let data;
  try { data = await jget("/api/timeline?minutes=" + minutes); } catch (e) { return; }
  const series = data.series || [];
  const stepMin = data.step_minutes || 1;
  const badge = $("#timeline-step");
  if (badge) badge.textContent = "· шаг " + stepLabel(stepMin);
  if (!series.length) return;

  const names = ((state.status && state.status.rois) || []).map((r) => r.name);
  const colorFor = {};
  names.forEach((n, i) => { colorFor[n] = CHART_COLORS[i % CHART_COLORS.length]; });

  const pad = 6;
  const legendH = 16;
  const plotH = h - pad * 2 - 14 - legendH;
  const maxV = Math.max(1, ...series.map((s) => sumValues(s.by_roi)));
  const bw = (w - pad * 2) / series.length;
  const baseY = h - pad - 14;

  series.forEach((s, i) => {
    const x = pad + i * bw;
    let yBase = baseY;
    const by = s.by_roi || {};
    names.forEach((n) => {
      const v = by[n] || 0;
      if (!v) return;
      const seg = (v / maxV) * plotH;
      ctx.fillStyle = colorFor[n];
      ctx.fillRect(x, yBase - seg, Math.max(1, bw - 0.6), seg);
      yBase -= seg;
    });
    if (!sumValues(by)) {
      ctx.fillStyle = "#222c38";
      ctx.fillRect(x, baseY - 1, Math.max(1, bw - 0.6), 1);
    }
  });

  ctx.font = "11px system-ui";
  let lx = pad;
  const ly = h - 4;
  names.forEach((n) => {
    const total = series.reduce((s, pt) => s + ((pt.by_roi || {})[n] || 0), 0);
    const label = n + ": " + total;
    ctx.fillStyle = colorFor[n];
    ctx.fillRect(lx, ly - 8, 9, 9);
    ctx.fillStyle = "#c8d3e0";
    ctx.fillText(label, lx + 12, ly);
    lx += 12 + ctx.measureText(label).width + 12;
  });

  ctx.fillStyle = "#8b9bb0";
  const ylab = "макс " + maxV + " за " + stepLabel(stepMin);
  ctx.fillText(ylab, w - ctx.measureText(ylab).width - pad, 12);
  const first = series[0].minute, last = series[series.length - 1].minute;
  ctx.fillText(axisLabel(first, stepMin), pad, h - 26);
  const tail = axisLabel(last, stepMin);
  ctx.fillText(tail, w - ctx.measureText(tail).width - pad, h - 26);
}

function hint(ctx, w, h, text) {
  ctx.fillStyle = "#8b9bb0";
  ctx.font = "13px system-ui";
  ctx.fillText(text, w / 2 - ctx.measureText(text).width / 2, h / 2);
}

function bindCamera() {
  const sel = $("#cam-res");
  if (!sel) return;
  ["exposure", "brightness", "focus"].forEach((k) => {
    const range = $("#cam-" + k + "-r");
    const num = $("#cam-" + k);
    range.addEventListener("input", () => { num.value = range.value; });
    num.addEventListener("input", () => { range.value = num.value; });
  });
  $("#btn-cam-apply").addEventListener("click", () => submitCamera(false));
  $("#btn-cam-save").addEventListener("click", () => submitCamera(true));
  $("#btn-cam-preview").addEventListener("click", refreshCamPreview);
}

function setSlider(id, meta, value) {
  const range = $("#" + id + "-r");
  const num = $("#" + id);
  range.min = meta.min; range.max = meta.max; range.step = meta.step || 1;
  num.min = meta.min; num.max = meta.max; num.step = meta.step || 1;
  const v = Math.max(meta.min, Math.min(meta.max, value == null ? meta.default : value));
  range.value = v; num.value = v;
}

async function loadCamera() {
  try {
    const data = await jget("/api/camera");
    const meta = data.capabilities;
    const cfg = data.config;
    state.camCaps = meta;

    const resSel = $("#cam-res");
    const cur = cfg.width + "x" + cfg.height;
    resSel.innerHTML = meta.resolutions.map((r) => {
      const val = r.w + "x" + r.h;
      return '<option value="' + val + '"' + (val === cur ? " selected" : "") + ">" + r.w + "×" + r.h + "</option>";
    }).join("");

    const modeSel = $("#cam-exp-mode");
    const ae = cfg.controls.auto_exposure;
    modeSel.innerHTML = meta.exposure_modes.map((m) =>
      '<option value="' + m.id + '"' + (m.value === ae ? " selected" : "") + ">" + m.label + "</option>").join("");

    setSlider("cam-exposure", meta.exposure, cfg.controls.exposure);
    setSlider("cam-brightness", meta.brightness, cfg.controls.brightness);
    setSlider("cam-focus", meta.focus, cfg.controls.focus);
    $("#cam-autofocus").value = (Number(cfg.controls.autofocus || 0) ? "1" : "0");

    $("#cam-exposure-range").textContent = "диапазон " + meta.exposure.min + ".." + meta.exposure.max +
      (meta.exposure.detected ? " (из камеры)" : " (по умолчанию)");
    $("#cam-brightness-range").textContent = "диапазон " + meta.brightness.min + ".." + meta.brightness.max +
      (meta.brightness.detected ? " (из камеры)" : " (по умолчанию)");
    $("#cam-focus-range").textContent = "диапазон " + meta.focus.min + ".." + meta.focus.max +
      (meta.focus.detected ? " (из камеры)" : " (по умолчанию; v4l2-ctl не найден)");
    $("#cam-caps").textContent = meta.has_v4l2
      ? "Возможности считаны через v4l2-ctl."
      : "v4l2-ctl недоступен — показаны типовые диапазоны. Установите v4l-utils для точных значений.";

    refreshCamPreview();
  } catch (e) {
    showMsg("#cam-msg", e.message, "err");
  }
}

function cameraFormBody() {
  const [w, h] = ($("#cam-res").value || "640x480").split("x").map(Number);
  return {
    width: w, height: h,
    exposure_mode: $("#cam-exp-mode").value,
    exposure: Number($("#cam-exposure").value),
    brightness: Number($("#cam-brightness").value),
    autofocus: Number($("#cam-autofocus").value),
    focus: Number($("#cam-focus").value),
  };
}

async function submitCamera(save) {
  const btn = save ? $("#btn-cam-save") : $("#btn-cam-apply");
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Применяю…";
  try {
    await jpost(save ? "/api/camera/save" : "/api/camera/apply", cameraFormBody());
    showMsg("#cam-msg", save ? "Сохранено в конфиг и применено." : "Применено (без сохранения).", "ok");
    await new Promise((r) => setTimeout(r, 1200));
    refreshCamPreview();
  } catch (e) {
    showMsg("#cam-msg", e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

function refreshCamPreview() {
  const img = $("#cam-preview");
  const msg = $("#cam-preview-msg");
  if (msg) { msg.hidden = false; msg.textContent = "Загрузка…"; }
  img.onload = () => { if (msg) msg.hidden = true; };
  img.onerror = () => { if (msg) { msg.hidden = false; msg.textContent = "Нет кадра с камеры"; } };
  img.src = cacheBuster(apiUrl("/api/snapshot.jpg"));
}

async function loadSnapshot() {
  showMsg("#roi-msg", "Загрузка снимка…", "");
  try {
    const res = await api("/api/snapshot.jpg?scale=1");
    if (!res.ok) throw new Error((await safeJson(res)).error || "снимок недоступен");
    const blob = await res.blob();
    const img = new Image();
    await new Promise((resolve, reject) => { img.onload = resolve; img.onerror = reject; img.src = URL.createObjectURL(blob); });
    state.snapshot = img;
    if (!state.rois.length && state.status) {
      state.rois = (state.status.rois || []).map((r) => ({
        name: r.name, x: r.rect[0], y: r.rect[1], w: r.rect[2], h: r.rect[3],
        detector: r.detector_override || {},
      }));
    }
    renderRoiEditor();
    showMsg("#roi-msg", "Снимок " + img.naturalWidth + "×" + img.naturalHeight + ". Протяните рамку вокруг гидрозатвора.", "");
  } catch (e) {
    showMsg("#roi-msg", "Не удалось получить снимок: " + e.message, "err");
  }
}

function bindRoiEditor() {
  const canvas = $("#roi-canvas");
  const toFrame = (ev) => {
    const rect = canvas.getBoundingClientRect();
    return {
      x: Math.round(((ev.clientX - rect.left) / rect.width) * canvas.width),
      y: Math.round(((ev.clientY - rect.top) / rect.height) * canvas.height),
    };
  };
  const onMove = (ev) => {
    if (!state.drawing) return;
    const p = toFrame(ev);
    state.drawing.x1 = p.x;
    state.drawing.y1 = p.y;
    renderRoiEditor();
  };
  const onUp = () => {
    const d = state.drawing;
    state.drawing = null;
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onUp);
    if (!d || d.x1 === undefined) { renderRoiEditor(); return; }
    const x = Math.min(d.x0, d.x1), y = Math.min(d.y0, d.y1);
    const w = Math.abs(d.x1 - d.x0), h = Math.abs(d.y1 - d.y0);
    if (w < 5 || h < 5) { renderRoiEditor(); return; }
    if (state.rois.length >= state.maxRois) { showMsg("#roi-msg", "Максимум " + state.maxRois + " зон", "err"); renderRoiEditor(); return; }
    state.rois.push({ name: "airlock-" + (state.rois.length + 1), x, y, w, h });
    renderRoiEditor();
  };
  canvas.addEventListener("pointerdown", (ev) => {
    if (!state.snapshot) return;
    ev.preventDefault();
    const p = toFrame(ev);
    state.drawing = { x0: p.x, y0: p.y };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  });
}

function renderRoiEditor() {
  const canvas = $("#roi-canvas");
  if (!state.snapshot) return;
  canvas.width = state.snapshot.naturalWidth;
  canvas.height = state.snapshot.naturalHeight;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(state.snapshot, 0, 0);

  const drawRect = (r, color, label) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.strokeRect(r.x, r.y, r.w, r.h);
    if (label) {
      ctx.font = "13px system-ui";
      const tw = ctx.measureText(label).width + 8;
      ctx.fillStyle = color;
      ctx.fillRect(r.x, Math.max(0, r.y - 17), tw, 17);
      ctx.fillStyle = "#0d1117";
      ctx.fillText(label, r.x + 4, Math.max(12, r.y - 4));
    }
  };
  state.rois.forEach((r) => drawRect(r, "#46c08b", r.name + " " + r.w + "×" + r.h));
  if (state.drawing && state.drawing.x1 !== undefined) {
    const d = state.drawing;
    drawRect({
      x: Math.min(d.x0, d.x1), y: Math.min(d.y0, d.y1),
      w: Math.abs(d.x1 - d.x0), h: Math.abs(d.y1 - d.y0),
    }, "#f0b429", "");
  }
  renderRoiTable();
}

function renderRoiTable() {
  const tbody = $("#roi-editor-table tbody");
  tbody.innerHTML = "";
  state.rois.forEach((roi, idx) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      "<td><input type='text' value='" + String(roi.name).replace(/'/g, "&#39;") + "' data-i='" + idx + "' data-k='name'></td>" +
      ["x", "y", "w", "h"].map((k) => "<td><input type='number' value='" + roi[k] + "' data-i='" + idx + "' data-k='" + k + "'></td>").join("") +
      "<td><button class='danger' data-del='" + idx + "'>удалить</button></td>";
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll("input").forEach((input) => {
    input.addEventListener("change", () => {
      const i = parseInt(input.dataset.i, 10);
      const k = input.dataset.k;
      state.rois[i][k] = k === "name" ? input.value : parseInt(input.value, 10) || 0;
      renderRoiEditor();
    });
  });
  tbody.querySelectorAll("button[data-del]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.rois.splice(parseInt(btn.dataset.del, 10), 1);
      renderRoiEditor();
    });
  });
}

async function saveRois() {
  if (!state.rois.length) {
    if (!confirm("Зоны не заданы. Подсчёт будет остановлен. Продолжить?")) return;
  }
  try {
    const data = await jpost("/api/rois", { rois: state.rois, persist: true });
    showMsg("#roi-msg", "Сохранено зон: " + data.rois.length + ". Идёт прогрев модели фона, подсчёт начнётся автоматически.", "ok");
    refreshStatus();
  } catch (e) {
    showMsg("#roi-msg", e.message, "err");
  }
}
