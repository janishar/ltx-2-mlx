// ltx studio front end — vanilla JS, no build step.
// Task definitions live in tasks.js; this file renders them, builds the
// ltx-2-mlx argument list, and talks to server.py.

"use strict";

const $ = (id) => document.getElementById(id);
const TASKS = Object.fromEntries(LTX_TASKS.map((t) => [t.id, t]));

const S = {
  model: {},
  session: null,
  sessions: [],
  inputs: [],
  takes: [],
  taskId: "t2v",
  values: {},
  common: {
    prompt: "", width: 704, height: 448, frames: 49, fps: 24,
    autoDuration: false, autoMin: 1, autoMax: 8, seed: 42,
    quantize: "8", lowRam: false, tileFrames: 1, tileSpatial: 1, tileOverlap: 2,
    extraArgs: "", takeName: "",
  },
  queue: [],
  selectedTake: null,
  timeline: [],
  selectedTimeline: null,
  lastLogReplace: false,
  restoring: false,
};

// ── utilities ────────────────────────────────────────────────────────────

async function api(path, body, method) {
  const opts = body === undefined ? { method: method || "GET" } : { method: "POST", body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok || (data && data.error)) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "value") node.value = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k in node && typeof v !== "string") node[k] = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined) node.append(child);
  return node;
}

// num() comes from tasks.js (shared global).
const fmtSecs = (s) => (s >= 60 ? `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s` : `${s.toFixed(1)}s`);
const randomSeed = () => Math.floor(Math.random() * 2 ** 31);

function splitArgs(text) {
  const out = [];
  for (const m of (text || "").matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g)) out.push(m[1] ?? m[2] ?? m[3]);
  return out;
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function inputByName(name) { return S.inputs.find((i) => i.name === name); }
function inputMeta(name) { const i = inputByName(name); return (i && i.probe) || {}; }

// ── task values ──────────────────────────────────────────────────────────

function defaultsFor(task) {
  const v = {};
  for (const f of task.fields) {
    if (f.type === "rows") {
      v[f.key] = (f.defaultRows || []).map((r) => ({ ...r }));
      while (v[f.key].length < (f.minRows || 0)) v[f.key].push(rowDefaults(f));
    } else if (f.default !== undefined) v[f.key] = f.default;
  }
  return v;
}

function rowDefaults(field) {
  const row = {};
  for (const f of field.itemFields) if (f.default !== undefined) row[f.key] = f.default;
  return row;
}

function taskValues(taskId = S.taskId) {
  if (!S.values[taskId]) S.values[taskId] = defaultsFor(TASKS[taskId]);
  return S.values[taskId];
}

function visible(field, v) {
  if (!field.when) return true;
  return Object.entries(field.when).every(([k, vals]) => {
    const def = TASKS[S.taskId].fields.find((f) => f.key === k);
    const current = v[k] ?? (def ? def.default : undefined) ?? false;
    return vals.includes(current);
  });
}

// ── rendering: task form ─────────────────────────────────────────────────

function renderTaskSelect() {
  const select = $("taskSelect");
  select.innerHTML = "";
  const groups = {};
  for (const t of LTX_TASKS) (groups[t.category] ||= []).push(t);
  for (const [category, tasks] of Object.entries(groups)) {
    const group = el("optgroup", { label: category });
    for (const t of tasks) group.append(el("option", { value: t.id, text: t.label }));
    select.append(group);
  }
  select.value = S.taskId;
}

function renderTask() {
  const task = TASKS[S.taskId];
  const v = taskValues();
  const blocks = task.blocks || {};
  $("taskSelect").value = task.id;
  $("taskCmd").textContent = `ltx-2-mlx ${task.cmd}`;
  $("taskDescription").textContent = task.description;
  $("promptBlock").hidden = !blocks.prompt;
  $("canvasBlock").hidden = !blocks.canvas;
  $("durationBlock").hidden = !blocks.duration;
  $("autoDurationRow").hidden = blocks.duration !== "auto";
  $("seedBlock").hidden = !blocks.seed;
  $("quantizeField").hidden = !blocks.quantize;
  $("lowRamField").hidden = !blocks.lowRam;
  $("tilingFields").hidden = !blocks.tiling;
  $("performanceFields").hidden = !(blocks.quantize || blocks.lowRam || blocks.tiling);

  const main = task.fields.filter((f) => !f.advanced && visible(f, v));
  const advanced = task.fields.filter((f) => f.advanced && visible(f, v));
  $("taskFieldsBlock").hidden = main.length === 0;
  $("taskFields").replaceChildren(el("div", { class: "taskgrid" }, main.map((f) => renderField(f, v, () => renderTask()))));
  $("advancedFields").replaceChildren(
    advanced.length ? el("div", { class: "taskgrid" }, advanced.map((f) => renderField(f, v, () => renderTask()))) : "",
  );
  renderAvailability();
  renderCanvasWarn();
  refreshPreview();
}

function renderField(field, values, rerender) {
  const label = el("span", {}, field.label, field.required ? el("span", { class: "req", text: " *" }) : null);
  const set = (value, structural) => {
    values[field.key] = value;
    if (structural) rerender();
    else refreshPreview();
    saveSettings();
  };
  switch (field.type) {
    case "select": {
      const select = el("select", { onchange: (e) => set(e.target.value, true) },
        field.options.map(([value, text]) => el("option", { value, text })));
      select.value = values[field.key] ?? field.default ?? field.options[0][0];
      return el("label", { class: "field" }, label, select);
    }
    case "check":
      return el("label", { class: "check full" },
        el("input", { type: "checkbox", checked: !!values[field.key], onchange: (e) => set(e.target.checked, true) }),
        field.label, field.hint ? el("em", { text: field.hint }) : null);
    case "number":
      return el("label", { class: "field" }, label,
        el("input", { type: "number", value: values[field.key] ?? "", step: field.step ?? "any", min: field.min, max: field.max,
          placeholder: field.placeholder ?? "", oninput: (e) => set(e.target.value === "" ? "" : Number(e.target.value)) }));
    case "text":
      return el("label", { class: "field" }, label,
        el("input", { type: "text", value: values[field.key] ?? "", placeholder: field.placeholder ?? "", spellcheck: false,
          oninput: (e) => set(e.target.value) }));
    case "textarea":
      return el("label", { class: "field" }, label,
        el("textarea", { rows: field.rows || 3, placeholder: field.placeholder ?? "", spellcheck: false,
          value: values[field.key] ?? "", oninput: (e) => set(e.target.value) }));
    case "media":
      return renderMedia(field, values, set, label);
    case "rows":
      return renderRows(field, values, rerender);
    default:
      return el("span", { text: `unknown field ${field.key}` });
  }
}

function renderMedia(field, values, set, label) {
  const current = values[field.key] || "";
  const compatible = S.inputs.filter((i) => field.accept === "file" || i.kind === field.accept);
  const item = inputByName(current);
  let preview = el("span", { text: field.accept });
  if (item && item.kind === "image") preview = el("img", { src: item.url, alt: "" });
  else if (item && item.kind === "video") preview = el("img", { src: item.thumb, alt: "" });
  else if (item && item.kind === "audio") preview = el("span", { text: "♪ audio" });
  const select = el("select", { onchange: (e) => set(e.target.value, true) },
    el("option", { value: "", text: compatible.length ? `Choose ${field.accept}…` : `Upload a ${field.accept} above` }),
    compatible.map((i) => el("option", { value: i.name, text: describeInput(i) })));
  select.value = current;
  const missing = current && !item;
  return el("div", { class: "media" }, label,
    el("div", { class: `slot${item ? " set" : ""}${missing ? " missing" : ""}` }, el("div", { class: "preview" }, preview), select));
}

function describeInput(i) {
  const p = i.probe || {};
  const bits = [i.name];
  if (p.width) bits.push(`${p.width}×${p.height}`);
  if (p.duration && i.kind !== "image") bits.push(`${p.duration.toFixed(1)}s`);
  return bits.join(" · ");
}

function renderRows(field, values, rerender) {
  const rows = (values[field.key] ||= []);
  const wrap = el("div", { class: "rows" },
    el("div", { class: "rowhead" }, el("span", { text: field.label }),
      el("button", { class: "ghost sm", type: "button", text: field.addLabel || "Add",
        onclick: () => { rows.push(rowDefaults(field)); rerender(); saveSettings(); } })));
  rows.forEach((row, index) => {
    const children = field.itemFields.map((f) => renderField(f, row, rerender));
    const canRemove = rows.length > (field.minRows || 0);
    wrap.append(el("div", { class: "row" }, children,
      canRemove ? el("button", { class: "remove", type: "button", title: "Remove", text: "×",
        onclick: () => { rows.splice(index, 1); rerender(); saveSettings(); } }) : null));
  });
  return wrap;
}

function renderAvailability() {
  const task = TASKS[S.taskId];
  const reason = S.model.configured ? task.requires(taskValues(), S.model) : "No model configured — click Model in the top bar.";
  $("taskUnavailable").hidden = !reason;
  $("taskUnavailable").textContent = reason || "";
}

function renderCanvasWarn() {
  const task = TASKS[S.taskId];
  const { width, height } = S.common;
  let message = "";
  if (task.blocks && task.blocks.canvas) {
    if (width % 32 || height % 32) message = "Width and height must be multiples of 32.";
    else if (["generate", "a2v", "keyframe", "ic-lora", "hdr-ic-lora"].includes(task.cmd) && (width % 64 || height % 64))
      message = "Two-stage pipelines render half size first — non-multiples of 64 snap down.";
    else if (width * height > 1920 * 1088) message = "Beyond 1080p memory grows fast — consider tiling in Advanced.";
  }
  $("sizeWarn").hidden = !message;
  $("sizeWarn").textContent = message;
  for (const b of $("sizePresets").children) b.classList.toggle("on", +b.dataset.w === width && +b.dataset.h === height);
}

function renderDuration() {
  const { frames, fps } = S.common;
  $("frameSlider").value = String((frames - 1) / 8);
  $("frameCount").textContent = frames;
  $("frameSecs").textContent = `${(frames / fps).toFixed(2)} s`;
  $("autoDurationRange").hidden = !S.common.autoDuration;
  $("frameSlider").disabled = S.common.autoDuration && TASKS[S.taskId].blocks.duration === "auto";
}

function syncCommonInputs() {
  const c = S.common;
  $("prompt").value = c.prompt;
  $("promptCount").textContent = c.prompt ? `${c.prompt.trim().split(/\s+/).length} words` : "";
  $("width").value = c.width;
  $("height").value = c.height;
  $("fps").value = c.fps;
  $("autoDuration").checked = c.autoDuration;
  $("autoMin").value = c.autoMin;
  $("autoMax").value = c.autoMax;
  $("seed").value = c.seed;
  $("quantize").value = c.quantize;
  $("lowRam").checked = c.lowRam;
  $("tileFrames").value = c.tileFrames;
  $("tileSpatial").value = c.tileSpatial;
  $("tileOverlap").value = c.tileOverlap;
  $("extraArgs").value = c.extraArgs;
  $("takeName").value = c.takeName;
  renderDuration();
}

// ── building the request ─────────────────────────────────────────────────

function collectErrors(task, v) {
  const errors = [];
  const blocks = task.blocks || {};
  if (blocks.prompt === "required" && !S.common.prompt.trim()) errors.push("Prompt is required.");
  const checkFields = (fields, values, prefix = "") => {
    for (const f of fields) {
      if (!visible(f, taskValues())) continue;
      if (f.type === "rows") {
        const rows = values[f.key] || [];
        if (rows.length < (f.minRows || 0)) errors.push(`${f.label}: add at least ${f.minRows}.`);
        rows.forEach((r, i) => checkFields(f.itemFields, r, `${f.label} ${i + 1} · `));
      } else if (f.required && (values[f.key] === undefined || values[f.key] === "" || values[f.key] === null)) {
        errors.push(`${prefix}${f.label} is required.`);
      } else if (f.type === "media" && values[f.key] && !inputByName(values[f.key])) {
        errors.push(`${prefix}${f.label}: "${values[f.key]}" is no longer in this session's inputs.`);
      }
    }
  };
  checkFields(task.fields, v);
  if (blocks.canvas && (S.common.width % 32 || S.common.height % 32)) errors.push("Width and height must be multiples of 32.");
  const reason = S.model.configured ? task.requires(v, S.model) : "No model configured.";
  if (reason) errors.push(reason);
  return errors;
}

function buildRequest(seed) {
  const task = TASKS[S.taskId];
  const v = taskValues();
  const c = S.common;
  const blocks = task.blocks || {};
  const ctx = { frames: c.frames, fps: c.fps, width: c.width, height: c.height, seed, autoDuration: c.autoDuration, inputMeta };
  const args = [];
  if (blocks.prompt && c.prompt.trim()) args.push("--prompt", c.prompt.trim());
  if (blocks.canvas) args.push("--width", String(c.width), "--height", String(c.height));
  if (blocks.duration) {
    if (blocks.duration === "auto" && c.autoDuration) args.push("--auto-duration", `${c.autoMin}:${c.autoMax}`);
    else args.push("--frames", String(c.frames));
    args.push("--frame-rate", String(c.fps));
  }
  if (blocks.seed) args.push("--seed", String(seed));
  args.push(...task.build(v, ctx));
  if (blocks.lowRam && c.lowRam) args.push("--low-ram");
  if (blocks.tiling) {
    if (c.tileFrames > 1) args.push("--tile-frames", String(c.tileFrames));
    if (c.tileSpatial > 1) args.push("--tile-spatial", String(c.tileSpatial));
    if (c.tileFrames > 1 || c.tileSpatial > 1) args.push("--tile-overlap", String(c.tileOverlap));
  }
  args.push(...splitArgs(c.extraArgs));
  return {
    session: S.session,
    task_id: task.id,
    label: task.label,
    subcommand: task.cmd,
    args,
    output: task.output,
    quantize: blocks.quantize ? c.quantize : undefined,
    take_name: c.takeName || task.id,
    seed: blocks.seed ? seed : null,
    params: { ...snapshot(), seed },
  };
}

function refreshPreview() {
  const task = TASKS[S.taskId];
  let req;
  try { req = buildRequest(S.common.seed); } catch (e) { $("cmdPreview").textContent = String(e); return; }
  const shown = req.args.map((a) => {
    const text = typeof a === "object" ? (a.input ? `inputs/${a.input || "?"}` : a.path) : a;
    return /^[\w./:=@+,-]+$/.test(text) ? text : `'${String(text).replace(/'/g, "'\\''")}'`;
  });
  const tail = [];
  if (task.cmd !== "enhance" && task.cmd !== "slice" && task.cmd !== "train") tail.push("--model <model>");
  if (req.quantize) tail.push(`--quantize-on-load ${req.quantize}`);
  if (task.output !== "none") tail.push("--output <session outputs>");
  $("cmdPreview").textContent = ["ltx-2-mlx", task.cmd, ...shown, ...tail].join(" ");
  const errors = collectErrors(task, taskValues());
  $("renderBtn").disabled = errors.length > 0;
  $("queueSeedsBtn").disabled = errors.length > 0 || !(task.blocks && task.blocks.seed);
}

async function submit(count) {
  const task = TASKS[S.taskId];
  const errors = collectErrors(task, taskValues());
  $("errors").hidden = errors.length === 0;
  $("errors").textContent = errors.join("\n");
  if (errors.length) return;
  const seeds = count === 1 ? [S.common.seed] : Array.from({ length: count }, randomSeed);
  try {
    await api("/api/render", { jobs: seeds.map((s) => buildRequest(s)) });
    saveSettings();
  } catch (e) {
    $("errors").hidden = false;
    $("errors").textContent = e.message;
  }
}

// ── settings persistence ─────────────────────────────────────────────────

function snapshot() {
  return { taskId: S.taskId, common: { ...S.common }, values: JSON.parse(JSON.stringify(S.values)) };
}

function restore(settings) {
  if (!settings || typeof settings !== "object") return;
  S.restoring = true;
  if (settings.taskId && TASKS[settings.taskId]) S.taskId = settings.taskId;
  if (settings.common) Object.assign(S.common, settings.common);
  if (settings.values) S.values = { ...S.values, ...settings.values };
  syncCommonInputs();
  renderTask();
  S.restoring = false;
}

const saveSettings = debounce(() => {
  if (S.restoring || !S.session) return;
  api("/api/session/save", { session: S.session, settings: snapshot() }).catch(() => {});
}, 700);

// ── inputs library ───────────────────────────────────────────────────────

async function loadInputs() {
  S.inputs = await api(`/api/inputs?session=${encodeURIComponent(S.session)}`);
  renderLibrary();
  renderTask();
}

function renderLibrary() {
  $("inputCount").textContent = S.inputs.length ? String(S.inputs.length) : "";
  $("library").replaceChildren(...S.inputs.map((i) => {
    let thumb = el("div", { class: "glyph", text: i.name });
    if (i.kind === "image") thumb = el("img", { src: i.url, alt: i.name, loading: "lazy" });
    else if (i.kind === "video") thumb = el("img", { src: i.thumb, alt: i.name, loading: "lazy" });
    else if (i.kind === "audio") thumb = el("div", { class: "glyph", text: `♪ ${i.name}` });
    const p = i.probe || {};
    const badge = i.kind === "video" || i.kind === "audio" ? (p.duration ? `${p.duration.toFixed(1)}s` : i.kind) : p.width ? `${p.width}×${p.height}` : "";
    return el("figure", { title: describeInput(i), onclick: () => fillSlot(i) },
      el("div", { class: "thumb" }, thumb),
      badge ? el("span", { class: "badge", text: badge }) : null,
      el("figcaption", { text: i.name }),
      el("button", { class: "delete-file", type: "button", title: "Delete input", text: "×",
        onclick: async (e) => {
          e.stopPropagation();
          if (!confirm(`Delete ${i.name} from this session?`)) return;
          await api("/api/inputs/delete", { session: S.session, name: i.name });
          loadInputs();
        } }));
  }));
}

function fillSlot(input) {
  const task = TASKS[S.taskId];
  const v = taskValues();
  const tryFields = (fields, values) => {
    for (const f of fields) {
      if (!visible(f, v)) continue;
      if (f.type === "media" && (f.accept === input.kind || f.accept === "file") && !values[f.key]) { values[f.key] = input.name; return true; }
      if (f.type === "rows") {
        for (const row of values[f.key] || []) if (tryFields(f.itemFields, row)) return true;
      }
    }
    return false;
  };
  if (!tryFields(task.fields, v)) {
    const rowsField = task.fields.find((f) => f.type === "rows" && f.itemFields.some((i) => i.type === "media" && i.accept === input.kind));
    if (rowsField) {
      const row = rowDefaults(rowsField);
      row[rowsField.itemFields.find((i) => i.type === "media").key] = input.name;
      (v[rowsField.key] ||= []).push(row);
    } else {
      flashError(`${task.label} has no empty ${input.kind} slot.`);
      return;
    }
  }
  renderTask();
  saveSettings();
}

function flashError(message) {
  $("errors").hidden = false;
  $("errors").textContent = message;
  setTimeout(() => { $("errors").hidden = true; }, 4000);
}

async function uploadFiles(files) {
  const added = [];
  for (const file of files) {
    try {
      const res = await fetch(`/api/upload?session=${encodeURIComponent(S.session)}&name=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || `upload failed (${res.status})`);
      added.push(data);
    } catch (e) {
      flashError(`${file.name}: ${e.message}`);
    }
  }
  await loadInputs();
  for (const a of added) { const item = inputByName(a.name); if (item) fillSlot(item); }
}

// ── takes ────────────────────────────────────────────────────────────────

async function loadTakes(selectNewest = false) {
  S.takes = await api(`/api/takes?session=${encodeURIComponent(S.session)}`);
  if (selectNewest && S.takes.length) selectTake(S.takes[0].name);
  renderTakes();
}

function takeMeta(t) {
  const p = t.probe || {};
  const bits = [];
  if (t.label) bits.push(t.label);
  if (p.width) bits.push(`${p.width}×${p.height}`);
  if (p.frames) bits.push(`${p.frames}f`);
  else if (p.duration) bits.push(`${p.duration.toFixed(1)}s`);
  if (t.seed !== undefined && t.seed !== null) bits.push(`seed ${t.seed}`);
  if (t.elapsed) bits.push(fmtSecs(t.elapsed));
  return bits.join(" · ");
}

function opButton(text, title, fn) {
  return el("button", { class: "ghost", type: "button", text, title,
    onclick: async (e) => { e.stopPropagation(); try { await fn(); } catch (err) { flashError(err.message); } } });
}

function renderTakes() {
  $("takeCount").textContent = S.takes.length ? String(S.takes.length) : "";
  if (!S.takes.length) {
    $("takeList").replaceChildren(el("li", { class: "take-empty", text: "No takes yet in this session." }));
    return;
  }
  $("takeList").replaceChildren(...S.takes.map((t) => el("li", { class: t.name === S.selectedTake ? "on" : "", onclick: () => selectTake(t.name) },
    el("div", { class: "row" },
      el("div", { class: "thumb" }, el("img", { src: t.thumb, alt: "", loading: "lazy" })),
      el("div", { class: "info" },
        el("div", { class: "nm", text: t.name, title: t.name }),
        el("div", { class: "meta", text: takeMeta(t) }))),
    el("div", { class: "ops" },
      t.params ? opButton("Reuse settings", "Restore the task, inputs and settings of this take", async () => restore(t.params)) : null,
      opButton("Chain →", "Use the last frame as the start image of Image → Video", async () => {
        const { name } = await api("/api/frame", { session: S.session, take: t.name, position: "last" });
        await loadInputs();
        S.taskId = "i2v";
        taskValues("i2v").image = name;
        renderTask();
        saveSettings();
      }),
      opButton("Last frame", "Add the last frame to inputs", async () => { await api("/api/frame", { session: S.session, take: t.name, position: "last" }); await loadInputs(); }),
      opButton("First frame", "Add the first frame to inputs", async () => { await api("/api/frame", { session: S.session, take: t.name, position: "first" }); await loadInputs(); }),
      opButton("Use video", "Copy this take into inputs (retake, extend, control)", async () => { await api("/api/use-video", { session: S.session, take: t.name }); await loadInputs(); }),
      t.probe && t.probe.has_audio ? opButton("Use audio", "Extract the audio track into inputs", async () => { await api("/api/audio", { session: S.session, take: t.name }); await loadInputs(); }) : null,
      opButton("Delete", "Delete this take", async () => {
        if (!confirm(`Delete ${t.name}?`)) return;
        await api("/api/takes/delete", { session: S.session, name: t.name });
        if (S.selectedTake === t.name) showInViewer(null);
        await loadTakes();
      })))));
}

function showInViewer(item, caption = "") {
  const player = $("player");
  if (item) {
    player.src = item.url;
    player.classList.add("on");
    $("viewerEmpty").hidden = true;
    $("viewerCaption").hidden = false;
    $("viewerCaption").textContent = caption;
  } else {
    S.selectedTake = null;
    S.selectedTimeline = null;
    player.removeAttribute("src");
    player.load();
    player.classList.remove("on");
    $("viewerEmpty").hidden = false;
    $("viewerCaption").hidden = true;
  }
  document.querySelectorAll("#takeList li").forEach((li, i) => li.classList.toggle("on", !!S.takes[i] && S.takes[i].name === S.selectedTake));
  document.querySelectorAll("#timelineList li").forEach((li, i) => li.classList.toggle("on", !!S.timeline[i] && S.timeline[i].name === S.selectedTimeline));
}

function selectTake(name) {
  const take = S.takes.find((t) => t.name === name);
  S.selectedTake = take ? name : null;
  S.selectedTimeline = null;
  if (!take) return showInViewer(null);
  const prompt = take.params && take.params.common && take.params.common.prompt;
  showInViewer(take, [take.name, takeMeta(take), prompt ? `“${prompt}”` : ""].filter(Boolean).join("  ·  "));
}

// ── timeline (combined clips, follows h3 studio) ─────────────────────────

const TL = { seq: [], browse: null, dragFrom: null };

async function loadTimeline(selectName = null) {
  S.timeline = await api(`/api/timeline?session=${encodeURIComponent(S.session)}`);
  renderTimelineList();
  if (selectName) selectTimeline(selectName);
}

function timelineMeta(t) {
  const p = t.probe || {};
  const bits = [];
  if (t.clips) bits.push(`${t.clips.length} clip${t.clips.length === 1 ? "" : "s"}`);
  if (p.width) bits.push(`${p.width}×${p.height}`);
  if (p.duration) bits.push(`${p.duration.toFixed(1)}s`);
  return bits.join(" · ") || "combined";
}

function renderTimelineList() {
  $("timelineCount").textContent = S.timeline.length ? String(S.timeline.length) : "";
  if (!S.timeline.length) {
    $("timelineList").replaceChildren(el("li", { class: "take-empty", text: "No combined videos yet. Use Create Timeline above to build one." }));
    return;
  }
  $("timelineList").replaceChildren(...S.timeline.map((t) => el("li", { class: t.name === S.selectedTimeline ? "on" : "", onclick: () => selectTimeline(t.name) },
    el("div", { class: "row" },
      el("div", { class: "thumb" }, el("img", { src: t.thumb, alt: "", loading: "lazy" })),
      el("div", { class: "info" },
        el("div", { class: "nm", text: t.name, title: (t.clips || []).join("\n") || t.name }),
        el("div", { class: "meta", text: timelineMeta(t) }))),
    el("div", { class: "ops" },
      opButton("Use video", "Copy this combined video into inputs (retake, extend, control)", async () => { await api("/api/use-video", { session: S.session, kind: "timeline", name: t.name }); await loadInputs(); }),
      opButton("Delete", "Delete this combined video", async () => {
        if (!confirm(`Delete ${t.name}?`)) return;
        await api("/api/timeline/delete", { session: S.session, name: t.name });
        if (S.selectedTimeline === t.name) showInViewer(null);
        await loadTimeline();
      })))));
}

function selectTimeline(name) {
  const item = S.timeline.find((t) => t.name === name);
  S.selectedTimeline = item ? name : null;
  S.selectedTake = null;
  if (!item) return showInViewer(null);
  showInViewer(item, [item.name, timelineMeta(item), ...(item.clips || []).map((c, i) => `${i + 1}. ${c}`)].join("  ·  "));
  $("player").play().catch(() => {});
}

function openTimelineModal() {
  TL.seq = [];
  renderSequence();
  $("timelineOutputName").value = "";
  $("timelineRenderStatus").textContent = "";
  if (S.timeline[0]) showReview(S.timeline[0]);
  else clearReview();
  $("timelineModal").hidden = false;
  browseTo("");
}

function closeTimelineModal() {
  $("timelineModal").hidden = true;
  $("timelineReviewVideo").pause();
}

function showReview(item) {
  const video = $("timelineReviewVideo");
  video.src = item.url;
  video.load();
  video.classList.add("on");
  $("timelineReviewEmpty").hidden = true;
}

function clearReview() {
  const video = $("timelineReviewVideo");
  video.pause();
  video.removeAttribute("src");
  video.load();
  video.classList.remove("on");
  $("timelineReviewEmpty").hidden = false;
}

async function browseTo(path) {
  try {
    TL.browse = await api(`/api/timeline/browse?session=${encodeURIComponent(S.session)}&path=${encodeURIComponent(path)}`);
  } catch (e) {
    $("timelineRenderStatus").textContent = e.message;
    return;
  }
  renderBreadcrumb();
  renderBrowser();
}

function renderBreadcrumb() {
  const crumbs = [el("button", { type: "button", text: "sessions", onclick: () => browseTo(".") })];
  let acc = "";
  for (const part of TL.browse.path === "." ? [] : TL.browse.path.split("/").filter(Boolean)) {
    acc = acc ? `${acc}/${part}` : part;
    const target = acc;
    crumbs.push(el("span", { class: "sep", text: "/" }), el("button", { type: "button", text: part, onclick: () => browseTo(target) }));
  }
  $("timelineBreadcrumb").replaceChildren(...crumbs);
}

function renderBrowser() {
  const data = TL.browse;
  const items = [];
  if (data.parent !== null && data.parent !== undefined) {
    items.push(el("div", { class: "browse-dir", onclick: () => browseTo(data.parent) }, el("div", { class: "icon", text: "⬅" }), el("div", { class: "nm", text: ".." })));
  }
  for (const d of data.dirs) {
    items.push(el("div", { class: "browse-dir", onclick: () => browseTo(d.path) }, el("div", { class: "icon", text: "📁" }), el("div", { class: "nm", text: d.name })));
  }
  for (const f of data.files) {
    const count = TL.seq.filter((c) => c.path === f.path).length;
    const meta = [f.duration ? `${f.duration.toFixed(2)}s` : "", f.width ? `${f.width}×${f.height}` : ""].filter(Boolean).join(" · ");
    items.push(el("div", { class: `browse-file${count ? " selected" : ""}`, title: f.path, onclick: () => addClip(f) },
      el("div", { class: "thumb" }, el("img", { src: f.thumb, alt: "", loading: "lazy" })),
      el("div", { class: "nm", text: f.name }),
      el("div", { class: "meta", text: meta }),
      count ? el("div", { class: "pickcount", text: String(count) }) : null));
  }
  if (!data.dirs.length && !data.files.length) items.push(el("div", { class: "browser-empty", text: "No videos in this directory." }));
  $("timelineBrowserList").replaceChildren(...items);
}

function addClip(f) {
  TL.seq.push({ path: f.path, name: f.name, duration: f.duration, thumb: f.thumb });
  renderSequence();
  renderBrowser();
}

function removeClip(index) {
  TL.seq.splice(index, 1);
  renderSequence();
  if (TL.browse) renderBrowser();
}

function reorderClip(from, to) {
  if (from === to || from === null || to === null) return;
  const [moved] = TL.seq.splice(from, 1);
  TL.seq.splice(to, 0, moved);
  renderSequence();
}

function renderSequence() {
  const track = $("timelineTrack");
  $("timelinePlaceholder").hidden = TL.seq.length > 0;
  const total = TL.seq.reduce((sum, c) => sum + (c.duration || 0), 0);
  $("timelineQueueTotal").textContent = TL.seq.length ? `${TL.seq.length} · ${total.toFixed(1)}s` : "";
  const nodes = TL.seq.map((item, index) => {
    const node = el("div", { class: "timeline-item", draggable: "true" },
      el("div", { class: "drag-handle", text: "⠿" }),
      el("div", { class: "seq", text: String(index + 1) }),
      el("img", { src: item.thumb, alt: "" }),
      el("div", { class: "info" },
        el("div", { class: "nm", text: item.name, title: item.path }),
        el("div", { class: "meta", text: item.duration ? `${item.duration.toFixed(2)}s` : "" })),
      el("button", { class: "remove", type: "button", text: "✕", title: "Remove from queue", onclick: () => removeClip(index) }));
    node.addEventListener("dragstart", (e) => { TL.dragFrom = index; node.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(index)); });
    node.addEventListener("dragend", () => { node.classList.remove("dragging"); TL.dragFrom = null; [...track.children].forEach((c) => c.classList.remove("drag-over")); });
    node.addEventListener("dragover", (e) => { if (TL.dragFrom === null) return; e.preventDefault(); e.dataTransfer.dropEffect = "move"; node.classList.add("drag-over"); });
    node.addEventListener("dragleave", () => node.classList.remove("drag-over"));
    node.addEventListener("drop", (e) => { e.preventDefault(); node.classList.remove("drag-over"); reorderClip(TL.dragFrom, index); });
    return node;
  });
  nodes.push(el("div", { class: "timeline-add-slot", text: TL.seq.length ? "+ pick another clip on the left" : "+ pick a clip on the left to start" }));
  track.replaceChildren(...nodes);
  $("renderTimeline").disabled = TL.seq.length === 0;
}

async function combineTimeline() {
  if (!TL.seq.length) return;
  const clips = TL.seq.map((c) => c.path);
  $("renderTimeline").disabled = true;
  $("timelineRenderStatus").textContent = `Combining ${clips.length} clip${clips.length > 1 ? "s" : ""}…`;
  appendLog(`$ combine ${clips.join(" + ")}`, false, "cmd");
  try {
    const { name } = await api("/api/timeline/render", { session: S.session, clips, name: $("timelineOutputName").value.trim() });
    appendLog(`[studio] combined video saved as timeline/${name}`, false, "done");
    $("timelineRenderStatus").textContent = `Saved ${name}`;
    await loadTimeline();
    const item = S.timeline.find((t) => t.name === name);
    if (item) { showReview(item); $("timelineReviewVideo").play().catch(() => {}); }
    TL.seq = [];
    renderSequence();
    if (TL.browse) renderBrowser();
  } catch (e) {
    appendLog(`[studio] combine failed: ${e.message}`, false, "failed");
    $("timelineRenderStatus").textContent = `Failed: ${e.message}`;
  } finally {
    $("renderTimeline").disabled = TL.seq.length === 0;
  }
}

// ── queue, progress, terminal ────────────────────────────────────────────

let elapsedTimer = null;

function renderQueue(items) {
  S.queue = items;
  const running = items.find((j) => j.status === "running" || j.status === "cancelling");
  const pending = items.filter((j) => j.status === "queued");
  $("lamp").classList.toggle("busy", !!running);
  $("lampText").textContent = running ? (pending.length ? `busy · ${pending.length} queued` : "busy") : "idle";
  $("running").hidden = !running;
  if (running) renderRunning(running);
  clearInterval(elapsedTimer);
  if (running) elapsedTimer = setInterval(() => renderRunning(S.queue.find((j) => j.id === running.id) || running), 1000);
  $("queueWrap").hidden = pending.length === 0;
  $("queueList").replaceChildren(...pending.map((j) => el("li", {},
    `${j.label}${j.seed !== null && j.seed !== undefined ? ` · seed ${j.seed}` : ""} · queued`,
    el("button", { class: "ghost sm", type: "button", text: "Remove", onclick: () => api("/api/cancel", { id: j.id }) }))));
}

function renderRunning(job) {
  const p = job.progress || {};
  const pct = p.total ? Math.min(100, Math.round((100 * p.step) / p.total)) : null;
  $("phaseName").textContent = `${job.label} — ${job.status === "cancelling" ? "stopping…" : p.phase || "starting"}`;
  $("phasePct").textContent = p.total ? `${p.step}/${p.total}` : "";
  $("phaseBar").style.width = pct === null ? "" : `${pct}%`;
  $("phaseBar").parentElement.classList.toggle("indeterminate", pct === null);
  const started = job.started ? Date.parse(job.started) : null;
  $("elapsed").textContent = started ? `${fmtSecs((Date.now() - started) / 1000)} elapsed${p.note ? ` · ${p.note}` : ""}` : "";
  $("stopBtn").onclick = () => api("/api/cancel", { id: job.id });
}

function appendLog(line, replace, kind) {
  const out = $("terminalOutput");
  if (replace && S.lastLogReplace && out.lastChild) {
    out.lastChild.textContent = `${line}\n`;
  } else {
    out.append(el("span", { class: kind === "cmd" ? "cmdline" : kind || "", text: `${line}\n` }));
    while (out.childNodes.length > 4000) out.firstChild.remove();
  }
  S.lastLogReplace = !!replace;
  if ($("followLog").checked) out.scrollTop = out.scrollHeight;
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (event) => {
    const { type, data } = JSON.parse(event.data);
    if (type === "queue") renderQueue(data);
    else if (type === "progress") {
      const job = S.queue.find((j) => j.id === data.id);
      if (job) { job.progress = data.progress; renderRunning(job); }
    } else if (type === "log") appendLog(data.line, data.replace, data.kind);
    else if (type === "job") {
      const idx = S.queue.findIndex((j) => j.id === data.id);
      if (idx >= 0) { S.queue[idx] = data; renderQueue(S.queue); }
      if (data.status === "failed" && data.error) flashError(`${data.label} failed: ${data.error}`);
    } else if (type === "takes" && data.session === S.session) loadTakes(true);
    else if (type === "timeline" && data.session === S.session) loadTimeline();
  };
  es.onerror = () => { $("lampText").textContent = "reconnecting…"; };
}

// ── sessions + model ─────────────────────────────────────────────────────

async function activateSession(name) {
  const res = await api("/api/session/activate", { session: name });
  S.session = res.name;
  S.values = {};
  S.selectedTake = null;
  S.selectedTimeline = null;
  const cfg = await api("/api/sessions");
  S.sessions = cfg.sessions;
  renderSessions();
  showInViewer(null);
  await Promise.all([loadInputs(), loadTakes(), loadTimeline()]);
  restore(res.settings);
  if (!res.settings || !res.settings.taskId) { syncCommonInputs(); renderTask(); }
}

function renderSessions() {
  $("sessionSelect").replaceChildren(...S.sessions.map((s) => el("option", { value: s, text: s })));
  $("sessionSelect").value = S.session;
}

function openSessionModal(mode) {
  $("sessionModalTitle").textContent = mode === "duplicate" ? `Duplicate ${S.session}` : "New session";
  $("confirmSession").textContent = mode === "duplicate" ? "Duplicate" : "Create";
  let n = S.sessions.length + 1;
  while (S.sessions.includes(`session-${n}`)) n += 1;
  $("sessionNameInput").value = mode === "duplicate" ? `${S.session}-copy` : `session-${n}`;
  $("sessionError").hidden = true;
  $("sessionModal").hidden = false;
  $("sessionNameInput").focus();
  $("confirmSession").onclick = async () => {
    const name = $("sessionNameInput").value.trim();
    try {
      if (!name) throw new Error("Enter a name.");
      if (S.sessions.includes(name)) throw new Error("That session already exists.");
      if (mode === "duplicate") await api("/api/session/duplicate", { session: S.session, new_name: name });
      $("sessionModal").hidden = true;
      await activateSession(name);
    } catch (e) {
      $("sessionError").hidden = false;
      $("sessionError").textContent = e.message;
    }
  };
}

function renderModelCaps(info) {
  const row = (label, ok, text) => [el("b", { text: label }), el("span", { class: ok === null ? "" : ok ? "yes" : "no", text })];
  if (!info.configured) {
    $("modelCaps").replaceChildren(el("span", { text: "not configured" }));
    return;
  }
  $("modelCaps").replaceChildren(
    ...row("model", null, info.model),
    ...row("type", null, info.local ? (info.is_25 ? "LTX-2.5" : "LTX-2.3 / other") : "Hugging Face repo (not inspected)"),
    ...row("distilled", info.has_distilled, info.has_distilled ? "yes" : "no"),
    ...row("dev", info.has_dev, info.has_dev ? "yes — two-stage, a2v, retake, extend, keyframe" : "no — dev-model tasks unavailable"),
    ...row("IC-LoRA", !info.is_25, info.is_25 ? "not on LTX-2.5 packs" : "available"),
  );
}

function applyModel(info) {
  S.model = info;
  $("modelButton").classList.toggle("warn-on", !info.configured);
  $("modelButton").textContent = info.configured ? `Model · ${info.model.split("/").filter(Boolean).pop()}` : "Set model";
  renderModelCaps(info);
  renderTask();
}

// ── wiring ───────────────────────────────────────────────────────────────

function bindCommon() {
  const c = S.common;
  const bindNum = (id, key, after) => $(id).addEventListener("input", (e) => {
    const value = num(e.target.value);
    if (value !== null) { c[key] = value; if (after) after(); refreshPreview(); saveSettings(); }
  });
  $("prompt").addEventListener("input", (e) => {
    c.prompt = e.target.value;
    $("promptCount").textContent = c.prompt ? `${c.prompt.trim().split(/\s+/).length} words` : "";
    refreshPreview();
    saveSettings();
  });
  bindNum("width", "width", renderCanvasWarn);
  bindNum("height", "height", renderCanvasWarn);
  bindNum("fps", "fps", renderDuration);
  bindNum("seed", "seed");
  bindNum("autoMin", "autoMin");
  bindNum("autoMax", "autoMax");
  bindNum("tileFrames", "tileFrames");
  bindNum("tileSpatial", "tileSpatial");
  bindNum("tileOverlap", "tileOverlap");
  $("frameSlider").addEventListener("input", (e) => { c.frames = Number(e.target.value) * 8 + 1; renderDuration(); refreshPreview(); saveSettings(); });
  $("autoDuration").addEventListener("change", (e) => { c.autoDuration = e.target.checked; renderDuration(); refreshPreview(); saveSettings(); });
  $("quantize").addEventListener("change", (e) => { c.quantize = e.target.value; refreshPreview(); saveSettings(); });
  $("lowRam").addEventListener("change", (e) => { c.lowRam = e.target.checked; refreshPreview(); saveSettings(); });
  $("extraArgs").addEventListener("input", (e) => { c.extraArgs = e.target.value; refreshPreview(); saveSettings(); });
  $("takeName").addEventListener("input", (e) => { c.takeName = e.target.value; saveSettings(); });
  $("dice").addEventListener("click", () => { c.seed = randomSeed(); $("seed").value = c.seed; refreshPreview(); saveSettings(); });

  $("sizePresets").replaceChildren(...SIZE_PRESETS.map(([id, label, w, h]) => el("button", {
    type: "button", "data-w": String(w), "data-h": String(h), text: `${label} ${w}×${h}`,
    onclick: () => { c.width = w; c.height = h; $("width").value = w; $("height").value = h; renderCanvasWarn(); refreshPreview(); saveSettings(); },
  })));
}

function bindChrome() {
  $("taskSelect").addEventListener("change", (e) => { S.taskId = e.target.value; $("errors").hidden = true; renderTask(); saveSettings(); });
  $("renderBtn").addEventListener("click", () => submit(1));
  $("queueSeedsBtn").addEventListener("click", () => submit(3));
  $("clearLog").addEventListener("click", () => { $("terminalOutput").replaceChildren(); S.lastLogReplace = false; });
  $("timelineButton").addEventListener("click", openTimelineModal);
  $("closeTimeline").addEventListener("click", closeTimelineModal);
  $("clearTimeline").addEventListener("click", () => { TL.seq = []; renderSequence(); if (TL.browse) renderBrowser(); });
  $("renderTimeline").addEventListener("click", combineTimeline);

  $("sessionSelect").addEventListener("change", (e) => activateSession(e.target.value));
  $("newSession").addEventListener("click", () => openSessionModal("new"));
  $("duplicateSession").addEventListener("click", () => openSessionModal("duplicate"));
  $("cancelSession").addEventListener("click", () => { $("sessionModal").hidden = true; });
  $("deleteSession").addEventListener("click", async () => {
    if (!confirm(`Delete session "${S.session}" and all its inputs and takes?`)) return;
    const res = await api("/api/session/delete", { session: S.session });
    await activateSession(res.name);
  });

  $("modelButton").addEventListener("click", () => {
    $("modelInput").value = S.model.model || "";
    $("gemmaInput").value = S.model.gemma || "";
    $("modelError").hidden = true;
    $("modelModal").hidden = false;
  });
  $("cancelModel").addEventListener("click", () => { $("modelModal").hidden = true; });
  $("saveModel").addEventListener("click", async () => {
    try {
      const info = await api("/api/model", { model: $("modelInput").value, gemma: $("gemmaInput").value });
      applyModel(info);
      $("modelModal").hidden = true;
    } catch (e) {
      $("modelError").hidden = false;
      $("modelError").textContent = e.message;
    }
  });

  const drop = $("drop");
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => { e.preventDefault(); uploadFiles([...e.dataTransfer.files]); });
  $("fileInput").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });

  document.querySelectorAll("#themeSwitch button").forEach((b) => b.addEventListener("click", () => setTheme(b.dataset.theme)));
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit(1);
    if (e.key === "Escape") { document.querySelectorAll(".modal").forEach((m) => { m.hidden = true; }); $("timelineReviewVideo").pause(); }
  });
}

function setTheme(theme) {
  try { localStorage.setItem("ltxstudio-theme", theme); } catch (e) { /* storage unavailable */ }
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  document.querySelectorAll("#themeSwitch button").forEach((b) => b.classList.toggle("on", b.dataset.theme === theme));
}

async function init() {
  let theme = "system";
  try { theme = localStorage.getItem("ltxstudio-theme") || "system"; } catch (e) { /* storage unavailable */ }
  setTheme(theme);
  renderTaskSelect();
  bindCommon();
  bindChrome();
  syncCommonInputs();
  const cfg = await api("/api/config");
  S.sessions = cfg.sessions;
  applyModel(cfg.model);
  if (!cfg.ffmpeg) appendLog("[studio] ffmpeg not found on PATH — frame/audio extraction, thumbnails and the timeline are disabled.", false, "failed");
  connectEvents();
  await activateSession(cfg.active);
  if (!cfg.model.configured) $("modelButton").click();
}

init().catch((e) => { console.error(e); flashError(`Failed to start: ${e.message}`); });
