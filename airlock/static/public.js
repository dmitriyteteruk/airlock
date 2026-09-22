"use strict";

const state = { status: null, activeTab: "public" };
const $ = (sel) => document.querySelector(sel);

const CHART_COLORS = ["#4ea1ff", "#46c08b", "#f0b429", "#c78bff", "#3cd0d0", "#ff8f6b", "#ef5f5f", "#9fb0c0"];

async function jget(path) {
  const res = await fetch(path, { headers: { "Cache-Control": "no-cache" } });
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

function fmtAge(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return seconds.toFixed(0) + " с";
  if (seconds < 3600) return (seconds / 60).toFixed(1) + " мин";
  return (seconds / 3600).toFixed(1) + " ч";
}

function fmtNum(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Number(value).toFixed(digits);
}

function esc(text) {
  return String(text == null ? "" : text).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function stateColor(s) {
  return { idle: "#46c08b", rising: "#f0b429", falling: "#3cd0d0",
           disturbed: "#ef5f5f", warmup: "#8b9bb0" }[s] || "#46c08b";
}

function sumValues(obj) {
  return Object.keys(obj || {}).reduce((s, k) => s + (obj[k] || 0), 0);
}

function stepLabel(min) {
  if (min < 60) return min + " мин";
  if (min < 1440) return (min / 60) + " ч";
  return (min / 1440) + " сут";
}

document.addEventListener("DOMContentLoaded", () => {
  refresh();
  setInterval(refresh, 5000);
  setInterval(tickClock, 1000);
  tickClock();
  window.addEventListener("load", () => setTimeout(startStream, 50));
});

function tickClock() {
  const el = $("#clock");
  if (el) el.textContent = new Date().toLocaleTimeString("ru-RU");
}

async function refresh() {
  try {
    const data = await jget("/public/api/status");
    state.status = data;
    renderCards(data.rois || []);
    renderAlerts(data);
    $("#cam-info").textContent = (data.camera.width || "?") + "×" + (data.camera.height || "?") +
      " · " + fmtNum(data.camera.read_fps, 0) + " к/с";
    $("#version").textContent = "airlock " + (data.version || "");
    setConn(true, "онлайн");
    drawTimeline();
  } catch (e) {
    setConn(false, "нет связи");
  }
}

function setConn(ok, text) {
  const el = $("#conn");
  el.className = "badge " + (ok ? "badge-ok" : "badge-err");
  el.textContent = text;
}

function renderAlerts(data) {
  const wrap = $("#alerts");
  wrap.innerHTML = "";
  if (!data.camera || !data.camera.ok) {
    const d = document.createElement("div");
    d.className = "alert alert-error";
    d.textContent = "Камера недоступна — данные могут не обновляться.";
    wrap.appendChild(d);
  }
  if (data.app && data.app.warmup) {
    const d = document.createElement("div");
    d.className = "alert alert-warning";
    d.textContent = "Идёт прогрев камеры, подсчёт начнётся автоматически.";
    wrap.appendChild(d);
  }
}

function renderCards(rois) {
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
    const pct = Math.min(100, (r.motion / 0.06) * 100);
    card.innerHTML =
      '<div class="zone-card-head">' +
        '<span class="zone-name" title="' + esc(r.name) + '">' + esc(r.name) + "</span>" +
        '<span class="pill pill-' + r.state + '">' + r.state + "</span>" +
      "</div>" +
      '<div class="zone-ferment ferm-' + ferm.level + '" title="За сутки: ' + Math.round(rates.day24 || 0) +
        ', за час: ' + Math.round(rates.bph || 0) + '">' +
        '<span class="m-label">брожение</span><span class="ferm-label">' + esc(ferm.label) + "</span>" +
      "</div>" +
      '<div class="zone-metrics">' +
        '<div><span class="m-label">бульков в час</span><span class="m-value">' + Math.round(rates.bph || 0) + "</span></div>" +
        '<div><span class="m-label">последний</span><span class="m-value">' + fmtAge(r.last_event_age_s) + "</span></div>" +
      "</div>" +
      '<div class="bar"><i style="width:' + pct.toFixed(1) + '%"></i></div>';
    wrap.appendChild(card);
  });
}

function startStream() {
  const img = $("#stream");
  const msg = $("#stream-msg");
  img.src = "/public/stream?t=" + Date.now();
  img.onload = () => { if (msg) msg.hidden = true; };
  img.onerror = () => { if (msg) { msg.hidden = false; msg.textContent = "Нет изображения с камеры"; } };
}

function fitCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const logicalH = parseInt(canvas.dataset.logicalH || canvas.getAttribute("height"), 10) || 220;
  canvas.dataset.logicalH = String(logicalH);
  const w = canvas.clientWidth || 600;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(logicalH * dpr);
  canvas.style.height = logicalH + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h: logicalH };
}

async function drawTimeline() {
  const canvas = $("#timeline-canvas");
  if (!canvas) return;
  const { ctx, w, h } = fitCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  let data;
  try { data = await jget("/public/api/timeline?minutes=1440"); } catch (e) { return; }
  const series = data.series || [];
  const stepMin = data.step_minutes || 1;
  if (!series.length) { hint(ctx, w, h, "Нет данных"); return; }

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
  const fmt = (m) => new Date(m * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  ctx.fillText(fmt(series[0].minute), pad, h - 26);
  const tail = fmt(series[series.length - 1].minute);
  ctx.fillText(tail, w - ctx.measureText(tail).width - pad, h - 26);
}

function hint(ctx, w, h, text) {
  ctx.fillStyle = "#8b9bb0";
  ctx.font = "13px system-ui";
  ctx.fillText(text, w / 2 - ctx.measureText(text).width / 2, h / 2);
}
