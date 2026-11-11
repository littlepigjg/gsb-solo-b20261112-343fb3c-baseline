"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {
  view: "editor", panel: "adjust", catalog: null, config: null,
  category: "color", operation: "color_levels", current: null, root: null,
  assets: [], assetMap: new Map(), assetTotal: 0, selected: new Set(),
  pipeline: [], presets: [], jobs: [], busy: false, comparing: false,
  selectionToken: 0, libraryToken: 0, exportIds: [], jobPoll: null,
};

const paths = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-6 5 7"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  sliders: '<path d="M4 6h5m4 0h7M4 12h10m4 0h2M4 18h2m4 0h10M9 3v6m5 0v6M6 15v6"/>',
  crop: '<path d="M6 3v15h15M3 6h15v15M9 6h9v9"/>',
  resize: '<path d="M4 9V4h5m6 0h5v5m0 6v5h-5M9 20H4v-5M4 4l5 5m11 11-5-5"/>',
  rotate: '<path d="M4 10a8 8 0 1 1 1 7M4 4v6h6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
  spark: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4m-2-2h4"/>',
  focus: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/><circle cx="12" cy="12" r="4"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5ZM3 12l9 5 9-5M3 16l9 5 9-5"/>',
  type: '<path d="M4 7V4h16v3M12 4v16M8 20h8"/>',
  file: '<path d="M14 3H5v18h14V8Zm0 0v5h5M8 13h8m-8 4h5"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  upload: '<path d="M12 16V4m-5 5 5-5 5 5M4 16v5h16v-5"/>',
  undo: '<path d="M9 4 4 9l5 5M4 9h9a6 6 0 0 1 0 12"/>',
  compare: '<path d="M12 2v20M8 5H3v14h5m8-14h5v14h-5"/>',
  expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m8 0h5v-5"/>',
  reset: '<path d="M3 10a9 9 0 1 1 2 8M3 4v6h6"/>',
  play: '<path d="m8 4 12 8-12 8Z"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="m10 3-1 3-3 1-3 3 2 2-1 4 3 1 2 4 3-1 4 1 1-3 4-2-1-3 1-4-3-1-2-3Z"/>',
};

function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.image}</svg>`;
}

function hydrateIcons(root = document) {
  $$('[data-icon]', root).forEach(element => { element.innerHTML = icon(element.dataset.icon); });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));
}

function humanBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function toast(message, type = "success") {
  const element = document.createElement("div");
  element.className = `toast ${type}`;
  element.textContent = message;
  $("#toast-container").append(element);
  setTimeout(() => element.remove(), type === "error" ? 8000 : 4200);
}

async function api(path, options = {}) {
  const settings = { ...options, headers: { ...options.headers } };
  if (settings.body && !(settings.body instanceof FormData)) {
    settings.headers["Content-Type"] = "application/json";
    settings.body = JSON.stringify(settings.body);
  }
  let response;
  try { response = await fetch(`/api${path}`, settings); }
  catch { throw new Error("无法连接本地服务，请运行 start.bat 后重试。"); }
  const result = await response.json().catch(() => null);
  if (!response.ok || !result?.ok) {
    throw new Error(result?.error?.message || `请求失败（${response.status}）`);
  }
  return result.data;
}

function on(selector, event, handler) {
  $(selector).addEventListener(event, async e => {
    try { await handler(e); }
    catch (error) { toast(error.message, "error"); }
  });
}

function rememberAssets(assets) {
  assets.forEach(asset => state.assetMap.set(asset.id, asset));
}

function findOperation(id) {
  return state.catalog.operations.find(operation => operation.id === id);
}

function requireImage() {
  if (!state.current) throw new Error("请先导入或选择一张图片。");
  return state.current;
}

function setBusy(value) {
  state.busy = value;
  $("#canvas-busy").hidden = !value;
  ["#apply-button", "#run-pipeline", "#undo-button", "#export-button"].forEach(selector => {
    $(selector).disabled = value;
  });
}

function setView(view) {
  state.view = view;
  $$(".view").forEach(element => element.classList.toggle("active", element.id === `${view}-view`));
  $$("[data-view]").forEach(button => button.classList.toggle("active", button.dataset.view === view));
  const titles = { editor: "图像工作台", library: "我的素材库", jobs: "批量任务" };
  $("#view-title").textContent = titles[view];
  if (view === "library") refreshLibrary().catch(error => toast(error.message, "error"));
  if (view === "jobs") { renderBatch(); refreshJobs().catch(error => toast(error.message, "error")); }
}

function setPanel(panel) {
  state.panel = panel;
  $$(".inspector-panel").forEach(element => element.classList.toggle("active", element.id === `${panel}-panel`));
  $$("[data-panel]").forEach(button => {
    const active = button.dataset.panel === panel;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  if (panel === "info") loadAnalysis().catch(error => toast(error.message, "error"));
}

function renderTools() {
  $("#tool-nav").innerHTML = state.catalog.categories.map(category => `
    <button class="tool-item ${state.category === category.id ? "active" : ""}" data-category="${category.id}" title="${escapeHtml(category.title)}" aria-label="${escapeHtml(category.title)}">
      ${icon(category.icon)}<span>${escapeHtml(category.title)}</span><span>${category.number}</span>
    </button>`).join("");
  $$("[data-category]").forEach(button => button.addEventListener("click", () => {
    if (button.dataset.category === "batch") { setView("jobs"); return; }
    chooseCategory(button.dataset.category);
  }));
}

function chooseCategory(category, operationId = null) {
  state.category = category;
  const operations = state.catalog.operations.filter(operation => operation.category === category);
  state.operation = operationId || operations[0].id;
  const metadata = state.catalog.categories.find(item => item.id === category);
  $("#tool-symbol").innerHTML = icon(metadata.icon);
  $("#category-title").textContent = metadata.title;
  $("#category-subtitle").textContent = metadata.subtitle;
  $("#category-number").textContent = metadata.number;
  $("#operation-select").innerHTML = operations.map(operation => `<option value="${operation.id}">${escapeHtml(operation.title)}</option>`).join("");
  $("#operation-select").value = state.operation;
  renderTools();
  renderParameters();
  setView("editor");
  setPanel("adjust");
}

function defaultValue(field) {
  if (state.operation === "crop_rect" && state.current) {
    if (field.name === "width") return Math.min(field.default, state.current.width);
    if (field.name === "height") return Math.min(field.default, state.current.height);
  }
  return field.default;
}

function renderParameters() {
  const operation = findOperation(state.operation);
  $("#operation-description").textContent = operation.description;
  $("#parameter-form").innerHTML = operation.parameters.map(field => {
    const id = `param-${field.name}`;
    const value = defaultValue(field);
    const label = escapeHtml(field.label);
    let control;
    if (["integer", "number"].includes(field.type)) {
      control = `<label for="${id}">${label}<span><input class="number-entry" id="${id}-number" type="number" min="${field.min}" max="${field.max}" step="${field.step}" value="${value}" aria-label="${label}数值"><span class="unit">${escapeHtml(field.unit)}</span></span></label>
        <input id="${id}" name="${field.name}" type="range" min="${field.min}" max="${field.max}" step="${field.step}" value="${value}">
        <div class="range-endpoints"><span>${field.min}</span><span>${field.max}</span></div>`;
    } else if (field.type === "select") {
      control = `<label for="${id}">${label}</label><select id="${id}" name="${field.name}">${field.options.map(option => `<option value="${escapeHtml(option.value)}" ${option.value === value ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}</select>`;
    } else if (field.type === "boolean") {
      control = `<label for="${id}" class="checkbox-label"><input id="${id}" name="${field.name}" type="checkbox" ${value ? "checked" : ""}>${label}</label><small class="field-help">${escapeHtml(field.help)}</small>`;
    } else {
      control = `<label for="${id}">${label}</label><input id="${id}" name="${field.name}" type="${field.type === "color" ? "color" : "text"}" value="${escapeHtml(value)}" ${field.max_length ? `maxlength="${field.max_length}"` : ""}>`;
    }
    return `<div class="parameter-field" title="${escapeHtml(field.help)}">${control}</div>`;
  }).join("");
  operation.parameters.filter(field => ["integer", "number"].includes(field.type)).forEach(field => {
    const range = $(`#param-${field.name}`);
    const number = $(`#param-${field.name}-number`);
    range.addEventListener("input", () => { number.value = range.value; });
    number.addEventListener("input", () => { range.value = number.value; });
  });
}

function readStep() {
  const operation = findOperation(state.operation);
  const params = {};
  for (const field of operation.parameters) {
    const input = $(`#param-${field.name}`);
    if (["integer", "number"].includes(field.type)) {
      const number = $(`#param-${field.name}-number`);
      if (!number.value.trim() || !number.checkValidity()) {
        number.reportValidity();
        throw new Error(`请检查“${field.label}”的数值范围。`);
      }
      params[field.name] = Number(number.value);
    } else if (field.type === "boolean") params[field.name] = input.checked;
    else params[field.name] = input.value;
  }
  return { operation: operation.id, params };
}

function renderFilmstrip() {
  const recent = [...state.assetMap.values()].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 10);
  $("#filmstrip").innerHTML = recent.map(asset => `<button class="film-item ${state.current?.id === asset.id ? "active" : ""}" data-asset="${asset.id}" title="${escapeHtml(asset.name)}"><img src="${asset.thumbnail_url}" alt="${escapeHtml(asset.name)}"><span class="film-tag">${asset.kind === "result" ? "处理副本" : asset.kind === "sample" ? "示例" : "原图"}</span></button>`).join("") + `<button class="film-item film-add" id="film-upload">${icon("plus")}<span>导入</span></button>`;
  $$("[data-asset]", $("#filmstrip")).forEach(button => button.addEventListener("click", () => {
    selectAsset(button.dataset.asset).catch(error => toast(error.message, "error"));
  }));
  $("#film-upload").addEventListener("click", () => $("#file-input").click());
}

async function selectAsset(id, switchView = false) {
  if (state.busy) return;
  const token = ++state.selectionToken;
  const asset = state.assetMap.get(id) || await api(`/assets/${id}`);
  if (token !== state.selectionToken) return;
  state.current = asset;
  state.assetMap.set(id, asset);
  $("#preview-image").src = asset.url;
  $("#preview-image").alt = asset.name;
  $("#empty-state").hidden = true;
  $("#image-stage").hidden = false;
  $("#active-name").textContent = asset.name;
  $("#active-kind").textContent = asset.kind === "result" ? "副本" : asset.kind === "sample" ? "示例" : "原图";
  $("#image-meta").textContent = `${asset.width} × ${asset.height} px · ${humanBytes(asset.bytes)} · ${asset.format}`;
  $("#save-status").textContent = "已保存到本机";
  $("#undo-button").disabled = !asset.parent_id;
  localStorage.setItem("studio-current", id);
  setComparison(false);
  state.root = null;
  renderFilmstrip();
  if (state.operation === "crop_rect") renderParameters();
  if (switchView) setView("editor");
  const root = asset.root_id === id ? asset : state.assetMap.get(asset.root_id) || await api(`/assets/${asset.root_id}`);
  if (token !== state.selectionToken) return;
  state.root = root;
  $("#original-image").src = root.url;
  if (state.panel === "info") await loadAnalysis();
}

function setComparison(enabled) {
  state.comparing = enabled;
  $("#original-layer").hidden = !enabled;
  $("#comparison-line").hidden = !enabled;
  $("#comparison-labels").hidden = !enabled;
  $("#comparison-control").hidden = !enabled;
  $("#compare-button").setAttribute("aria-pressed", String(enabled));
}

async function processSteps(steps) {
  const asset = requireImage();
  if (state.busy) return;
  if (!steps.length) throw new Error("请先添加处理步骤，或选择一个预设。");
  setBusy(true);
  try {
    const result = await api("/process", { method: "POST", body: { asset_id: asset.id, steps } });
    state.assetMap.set(result.id, result);
    setBusy(false);
    await selectAsset(result.id);
    await refreshStats();
    toast(`已完成处理，保存为新副本 · ${result.metadata.elapsed_ms} ms`);
  } finally { setBusy(false); $("#undo-button").disabled = !state.current?.parent_id; }
}

async function importFiles(files) {
  if (!files.length) return;
  if (files.length > 24) throw new Error("一次最多导入 24 张图片。");
  let success = 0;
  let last = null;
  for (const file of files) {
    if (file.size > state.config.max_upload_mb * 1024 * 1024) {
      toast(`${file.name} 超过 ${state.config.max_upload_mb} MB，已跳过。`, "error"); continue;
    }
    const body = new FormData();
    body.append("file", file);
    try {
      last = await api("/assets", { method: "POST", body });
      state.assetMap.set(last.id, last);
      success += 1;
      if (last.metadata.notice) toast(last.metadata.notice, "info");
    } catch (error) { toast(`${file.name}：${error.message}`, "error"); }
  }
  if (last) {
    await selectAsset(last.id, true);
    await refreshStats();
    toast(`成功导入 ${success} 张图片`);
  }
  $("#file-input").value = "";
}

async function refreshStats() {
  const stats = await api("/stats");
  $("#library-count").textContent = stats.assets;
  $("#job-count").textContent = stats.active_jobs;
}

async function refreshLibrary(append = false) {
  const token = ++state.libraryToken;
  const query = new URLSearchParams({
    q: $("#asset-search").value,
    kind: $("#asset-kind").value,
    limit: "60",
    offset: String(append ? state.assets.length : 0),
  });
  const result = await api(`/assets?${query}`);
  if (token !== state.libraryToken) return;
  state.assets = append ? [...state.assets, ...result.items] : result.items;
  state.assetTotal = result.total;
  rememberAssets(result.items);
  renderLibrary();
}

function renderLibrary() {
  $("#asset-grid").innerHTML = state.assets.length ? state.assets.map(asset => `
    <article class="asset-card ${state.selected.has(asset.id) ? "selected" : ""}">
      <label class="asset-checkbox"><input type="checkbox" data-select="${asset.id}" aria-label="选择 ${escapeHtml(asset.name)}" ${state.selected.has(asset.id) ? "checked" : ""}></label>
      <button class="asset-open" data-open="${asset.id}" title="打开 ${escapeHtml(asset.name)}"><img src="${asset.thumbnail_url}" alt="${escapeHtml(asset.name)}" loading="lazy"></button>
      <div class="asset-card-info"><h3 title="${escapeHtml(asset.name)}">${escapeHtml(asset.name)}</h3><p>${asset.width} × ${asset.height} · ${humanBytes(asset.bytes)}</p></div>
      <div class="asset-card-actions"><span>${asset.kind === "result" ? "处理副本" : asset.kind === "sample" ? "示例素材" : "导入原图"}</span><div><button data-favorite="${asset.id}" class="${asset.favorite ? "favorite-on" : ""}" aria-label="${asset.favorite ? "取消收藏" : "收藏"}">${asset.favorite ? "★" : "☆"}</button><button data-delete="${asset.id}" aria-label="删除 ${escapeHtml(asset.name)}" title="删除素材">×</button></div></div>
    </article>`).join("") : '<div class="full-empty">这里还没有匹配的图片。试试导入新素材，或修改搜索条件。</div>';
  $("#load-more").hidden = state.assets.length >= state.assetTotal;
  $("#selection-count").textContent = `已选 ${state.selected.size} 张`;
  $$("[data-select]").forEach(input => input.addEventListener("change", () => {
    if (input.checked && state.selected.size >= 24) { input.checked = false; toast("每批最多选择 24 张图片。", "error"); return; }
    if (input.checked) state.selected.add(input.dataset.select);
    else state.selected.delete(input.dataset.select);
    renderLibrary();
  }));
  $$("[data-open]").forEach(button => button.addEventListener("click", () => selectAsset(button.dataset.open, true).catch(error => toast(error.message, "error"))));
  $$("[data-favorite]").forEach(button => button.addEventListener("click", async () => {
    try {
      const asset = state.assetMap.get(button.dataset.favorite);
      const updated = await api(`/assets/${asset.id}`, { method: "PATCH", body: { favorite: !asset.favorite } });
      state.assetMap.set(updated.id, updated);
      state.assets = state.assets.map(item => item.id === updated.id ? updated : item);
      renderLibrary();
    } catch (error) { toast(error.message, "error"); }
  }));
  $$("[data-delete]").forEach(button => button.addEventListener("click", () => confirmDelete(button.dataset.delete)));
}

function confirmDelete(id) {
  const asset = state.assetMap.get(id);
  $("#confirm-message").textContent = `删除“${asset.name}”及其本地图片文件？如有处理副本，需先删除副本。`;
  $("#confirm-dialog").showModal();
  $("#confirm-action").onclick = async () => {
    $("#confirm-action").disabled = true;
    try {
      await api(`/assets/${id}`, { method: "DELETE" });
      state.assetMap.delete(id);
      state.selected.delete(id);
      if (state.current?.id === id) {
        state.current = null;
        state.root = null;
        state.selectionToken += 1;
        $("#image-stage").hidden = true;
        $("#empty-state").hidden = false;
        $("#active-name").textContent = "尚未选择图片";
        $("#image-meta").textContent = "等待导入图片";
        setComparison(false);
      }
      $("#confirm-dialog").close();
      await refreshLibrary();
      await refreshStats();
      renderFilmstrip();
      toast("素材已删除");
    } catch (error) { toast(error.message, "error"); }
    finally { $("#confirm-action").disabled = false; }
  };
}

function renderPipeline() {
  $("#pipeline-count").textContent = state.pipeline.length;
  $("#pipeline-list").innerHTML = state.pipeline.length ? state.pipeline.map((step, index) => `
    <div class="pipeline-step"><span class="step-number">${String(index + 1).padStart(2, "0")}</span><span class="step-title">${escapeHtml(findOperation(step.operation).title)}</span><div class="step-buttons"><button data-move="${index}" ${index === 0 ? "disabled" : ""} title="向上移动" aria-label="向上移动">↑</button><button data-remove="${index}" title="移除步骤" aria-label="移除步骤">×</button></div></div>`).join("") : '<div class="empty-hint">还没有处理步骤<br>在“调整”中点击“加入流程”，<br>或选择上方的预设。</div>';
  $$("[data-remove]").forEach(button => button.addEventListener("click", () => {
    state.pipeline.splice(Number(button.dataset.remove), 1); renderPipeline();
  }));
  $$("[data-move]").forEach(button => button.addEventListener("click", () => {
    const index = Number(button.dataset.move);
    [state.pipeline[index - 1], state.pipeline[index]] = [state.pipeline[index], state.pipeline[index - 1]];
    renderPipeline();
  }));
  renderBatch();
}

function renderPresets() {
  const options = state.presets.map(preset => `<option value="${preset.id}">${escapeHtml(preset.name)} · ${preset.steps.length} 步</option>`).join("");
  $("#preset-select").innerHTML = '<option value="">选择一个预设流程</option>' + options;
  $("#batch-preset").innerHTML = '<option value="">使用当前编辑流程</option>' + options;
}

function loadPreset(id) {
  const preset = state.presets.find(item => item.id === id);
  if (!preset) return;
  state.pipeline = structuredClone(preset.steps);
  renderPipeline();
  toast(`已载入“${preset.name}”，点击应用后执行。`, "info");
}

async function loadAnalysis() {
  if (!state.current) { $("#analysis-content").textContent = "选择一张图片查看信息。"; return; }
  const id = state.current.id;
  $("#analysis-content").innerHTML = '<p class="muted">正在分析画面…</p>';
  const [analysis, history] = await Promise.all([api(`/assets/${id}/analysis`), api(`/assets/${id}/history`)]);
  if (state.current?.id !== id) return;
  const values = [
    ["图像尺寸", `${analysis.width} × ${analysis.height}`], ["宽高比例", analysis.aspect_ratio],
    ["像素数量", `${analysis.megapixels} MP`], ["平均亮度", `${analysis.brightness} / 255`],
    ["亮度标准差", analysis.contrast], ["信息熵", `${analysis.entropy_bits} bit`],
    ["透明像素", `${analysis.transparent_percent}%`],
  ];
  $("#analysis-content").innerHTML = `<dl>${values.map(([key, value]) => `<dt>${key}</dt><dd>${escapeHtml(value)}</dd>`).join("")}</dl><h3 class="analysis-heading">RGB 直方图</h3><canvas class="histogram" id="histogram" width="480" height="190" aria-label="RGB 通道直方图"></canvas><h3 class="analysis-heading">画面色谱</h3><div class="swatches">${analysis.palette.map(color => `<span class="swatch" style="background:${color.hex}" title="${color.hex} · ${color.percentage}%"></span>`).join("")}</div><p class="suggestion">${escapeHtml(analysis.suggestions[0].message)}</p><h3 class="analysis-heading">处理历史 · ${history.length} 个版本</h3><div class="history-list">${history.map((item, index) => `<button class="history-item" data-history="${item.id}">${String(index + 1).padStart(2, "0")} / ${escapeHtml(item.name)}</button>`).join("")}</div>`;
  const canvas = $("#histogram");
  const ctx = canvas.getContext("2d");
  const channels = [["red", "#c17e67"], ["green", "#729e55"], ["blue", "#739da8"]];
  const maximum = Math.max(...Object.values(analysis.histograms).slice(0, 3).flat(), 1);
  ctx.globalAlpha = 0.65;
  for (const [key, color] of channels) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    analysis.histograms[key].forEach((value, index) => {
      const x = index / 255 * canvas.width;
      const y = canvas.height - 8 - Math.sqrt(value / maximum) * (canvas.height - 20);
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  $$("[data-history]").forEach(button => button.addEventListener("click", () => selectAsset(button.dataset.history).catch(error => toast(error.message, "error"))));
}

function batchSteps() {
  const preset = state.presets.find(item => item.id === $("#batch-preset").value);
  return preset ? preset.steps : state.pipeline;
}

function renderBatch() {
  $("#batch-selected-count").textContent = state.selected.size;
  $("#batch-images").innerHTML = state.selected.size ? [...state.selected].map(id => {
    const asset = state.assetMap.get(id);
    return asset ? `<div class="batch-thumb"><img src="${asset.thumbnail_url}" alt="${escapeHtml(asset.name)}"><button data-unselect="${id}" aria-label="移除 ${escapeHtml(asset.name)}">×</button></div>` : "";
  }).join("") : '<p class="muted">还没有待处理图片，从素材库选择几张吧。</p>';
  const steps = batchSteps();
  $("#batch-steps").innerHTML = steps.length ? steps.map((step, index) => `<div class="batch-step">${index + 1}. ${escapeHtml(findOperation(step.operation).title)}</div>`).join("") : '<p class="muted">请选择预设，或先在工作台建立处理流程。</p>';
  $$("[data-unselect]").forEach(button => button.addEventListener("click", () => { state.selected.delete(button.dataset.unselect); renderBatch(); }));
}

async function refreshJobs() {
  state.jobs = await api("/jobs");
  renderJobs();
  const active = state.jobs.filter(job => ["queued", "running", "cancelling"].includes(job.status));
  $("#job-count").textContent = active.length;
}

function renderJobs() {
  const statusNames = { queued: "排队中", running: "处理中", cancelling: "取消中", completed: "已完成", partial: "部分完成", failed: "失败", cancelled: "已取消", interrupted: "已中断" };
  $("#jobs-list").innerHTML = state.jobs.length ? state.jobs.map(job => `
    <article class="job-card"><div class="job-card-head"><h3>${escapeHtml(job.title)}</h3><span class="job-status ${job.status}">${statusNames[job.status]}</span></div><div class="job-progress"><div style="width:${job.progress}%"></div></div><p>${escapeHtml(job.message)} · ${job.done}/${job.total} 张 · ${job.steps.length} 个步骤</p>${job.errors.length ? `<details class="job-errors"><summary>${job.errors.length} 个处理错误</summary>${job.errors.map(error => `<p>${escapeHtml(error.message)}</p>`).join("")}</details>` : ""}<div class="job-card-foot"><span>${new Date(job.created_at).toLocaleString("zh-CN", { hour12: false })}</span><div>${["queued", "running"].includes(job.status) ? `<button class="text-button" data-cancel-job="${job.id}">取消任务</button>` : ""}${job.results.length ? `<button class="text-button" data-job-results="${job.id}">查看结果 ↗</button> <button class="text-button" data-job-export="${job.id}">打包导出</button>` : ""}</div></div></article>`).join("") : '<div class="empty-hint">还没有批量任务。<br>选择图片与流程，开始第一组创作。</div>';
  $$("[data-cancel-job]").forEach(button => button.addEventListener("click", async () => {
    try { await api(`/jobs/${button.dataset.cancelJob}/cancel`, { method: "POST", body: {} }); await refreshJobs(); }
    catch (error) { toast(error.message, "error"); }
  }));
  $$("[data-job-results]").forEach(button => button.addEventListener("click", async () => {
    try {
      const job = state.jobs.find(item => item.id === button.dataset.jobResults);
      const assets = await Promise.all(job.results.map(result => api(`/assets/${result.asset_id}`)));
      rememberAssets(assets);
      state.selected = new Set(assets.map(asset => asset.id));
      $("#asset-kind").value = "result";
      $("#asset-search").value = "";
      setView("library");
      toast(`已选中 ${assets.length} 张任务结果`, "info");
    } catch (error) { toast(error.message, "error"); }
  }));
  $$("[data-job-export]").forEach(button => button.addEventListener("click", () => {
    const job = state.jobs.find(item => item.id === button.dataset.jobExport);
    openExport(job.results.map(result => result.asset_id));
  }));
}

function openExport(ids) {
  if (!ids.length) { toast("请先选择要导出的图片。", "error"); return; }
  state.exportIds = ids;
  $("#export-target").textContent = ids.length > 1 ? `将 ${ids.length} 张图片打包为 ZIP 下载。` : `导出：${state.assetMap.get(ids[0])?.name || "处理结果"}`;
  $$(".download-ready", $("#export-form")).forEach(element => element.remove());
  $("#export-dialog").showModal();
}

async function exportImages(event) {
  event.preventDefault();
  const button = $("#confirm-export");
  button.disabled = true;
  button.textContent = "正在编码并生成文件…";
  try {
    const options = {
      format: $("#export-format").value,
      quality: Number($("#export-quality").value),
      background: $("#export-background").value,
      lossless: $("#export-lossless").checked,
      dpi: Number($("#export-dpi").value),
    };
    const batch = state.exportIds.length > 1;
    const body = batch ? { asset_ids: state.exportIds, options } : { asset_id: state.exportIds[0], options };
    const result = await api(batch ? "/exports/archive" : "/exports", { method: "POST", body });
    const link = document.createElement("a");
    link.href = result.url;
    link.download = result.download_name;
    link.className = "button wide download-ready";
    link.textContent = `下载已生成的文件 · ${humanBytes(result.bytes)}`;
    $("#export-form").append(link);
    link.click();
    toast(`导出文件已生成 · ${humanBytes(result.bytes)}`);
  } finally {
    button.disabled = false;
    button.innerHTML = `${icon("download")}重新生成并下载`;
  }
}

async function showSystem() {
  $("#system-dialog").showModal();
  $("#system-content").innerHTML = '<p class="muted">正在读取运行状态…</p>';
  const [stats, health, diagnostics] = await Promise.all([api("/stats"), api("/health"), api("/diagnostics")]);
  const rows = [
    ["服务状态", "运行中"], ["版本", health.version], ["Python", diagnostics.python],
    ["图像处理", `${health.features} 项功能 / ${health.operations} 种操作`],
    ["业务数据存储", "JSON 文件"], ["图像数量", stats.assets], ["处理副本", stats.results],
    ["图像占用", humanBytes(stats.bytes)], ["已完成批次", stats.completed_jobs],
    ["允许单图大小", `${state.config.max_upload_mb} MB`],
    ["最大像素", `${state.config.max_pixels / 1000000} MP`], ["空闲磁盘", humanBytes(diagnostics.disk_free_bytes)],
  ];
  $("#system-content").innerHTML = `<dl class="system-data">${rows.map(([label, value]) => `<dt>${label}</dt><dd>${escapeHtml(value)}</dd>`).join("")}</dl>${diagnostics.issues.length ? `<p class="system-alert">${diagnostics.issues.length} 项数据检查提示，请查看 /api/diagnostics。</p>` : '<p class="muted">JSON 数据与图片文件检查正常。</p>'}`;
}

function bindEvents() {
  $$("[data-view]").forEach(button => button.addEventListener("click", () => setView(button.dataset.view)));
  $$("[data-panel]").forEach(button => button.addEventListener("click", () => setPanel(button.dataset.panel)));
  $$(".close-dialog").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
  ["#top-upload", "#empty-upload", "#library-upload"].forEach(selector => on(selector, "click", () => $("#file-input").click()));
  on("#file-input", "change", event => importFiles([...event.target.files]));
  on("#operation-select", "change", () => { state.operation = $("#operation-select").value; renderParameters(); });
  on("#parameter-form", "submit", event => event.preventDefault());
  on("#reset-params", "click", () => { renderParameters(); toast("参数已重置", "info"); });
  on("#apply-button", "click", () => processSteps([readStep()]));
  on("#run-pipeline", "click", () => processSteps(state.pipeline));
  on("#add-step", "click", () => {
    if (state.pipeline.length >= 12) throw new Error("每个流程最多 12 个步骤。");
    state.pipeline.push(readStep()); renderPipeline(); toast("已加入处理流程", "info");
  });
  on("#clear-pipeline", "click", () => { state.pipeline = []; $("#preset-select").value = ""; renderPipeline(); });
  on("#preset-select", "change", event => loadPreset(event.target.value));
  on("#batch-preset", "change", renderBatch);
  on("#save-preset", "click", () => {
    if (!state.pipeline.length) throw new Error("请先添加处理步骤。");
    $("#preset-dialog").showModal();
  });
  on("#preset-form", "submit", async event => {
    event.preventDefault();
    const preset = await api("/presets", { method: "POST", body: { name: $("#preset-name").value, description: $("#preset-description").value, steps: state.pipeline } });
    state.presets.push(preset); renderPresets(); $("#preset-dialog").close(); toast("处理预设已保存到 JSON");
  });
  on("#undo-button", "click", async () => {
    if (state.current?.parent_id) await selectAsset(state.current.parent_id);
  });
  on("#compare-button", "click", () => {
    requireImage();
    if (!state.root) throw new Error("原图正在载入，请稍候。");
    setComparison(!state.comparing);
  });
  on("#comparison-range", "input", event => {
    $("#original-layer").style.clipPath = `inset(0 ${100 - Number(event.target.value)}% 0 0)`;
    $("#comparison-line").style.left = `${event.target.value}%`;
  });
  on("#fullscreen-button", "click", async () => {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await $("#canvas-area").requestFullscreen();
  });
  on("#demo-button", "click", async () => {
    const samples = await api("/samples", { method: "POST", body: {} });
    rememberAssets(samples); await selectAsset(samples[0].id); await refreshStats(); toast("示例素材已就绪，试试调色或滤镜", "info");
  });
  on("#show-library", "click", () => setView("library"));
  on("#batch-pick", "click", () => setView("library"));
  on("#go-batch", "click", () => { if (!state.selected.size && state.current) state.selected.add(state.current.id); setView("jobs"); });
  on("#library-batch", "click", () => setView("jobs"));
  on("#library-export", "click", () => openExport([...state.selected]));
  on("#export-button", "click", () => openExport([requireImage().id]));
  on("#export-form", "submit", exportImages);
  on("#export-quality", "input", event => { $("#quality-value").textContent = event.target.value; });
  on("#select-all", "click", () => {
    if (state.assets.length && state.assets.every(asset => state.selected.has(asset.id))) {
      state.assets.forEach(asset => state.selected.delete(asset.id));
    } else {
      state.assets.forEach(asset => { if (state.selected.size < 24) state.selected.add(asset.id); });
      if (state.assets.length > 24) toast("已选择前 24 张，达到单批上限。", "info");
    }
    renderLibrary();
  });
  let searchTimeout;
  on("#asset-search", "input", () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => refreshLibrary().catch(error => toast(error.message, "error")), 250);
  });
  on("#asset-kind", "change", () => refreshLibrary());
  on("#load-more", "click", () => refreshLibrary(true));
  on("#refresh-jobs", "click", refreshJobs);
  on("#submit-job", "click", async () => {
    if (!state.selected.size) throw new Error("请先选择待处理图片。");
    const steps = batchSteps();
    if (!steps.length) throw new Error("请先选择预设或建立流程。");
    $("#submit-job").disabled = true;
    try {
      await api("/jobs", { method: "POST", body: { asset_ids: [...state.selected], steps, title: $("#job-title").value } });
      await refreshJobs(); toast("批量任务已提交");
    } finally { $("#submit-job").disabled = false; }
  });
  on("#system-button", "click", showSystem);
  document.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "o") {
      event.preventDefault(); $("#file-input").click();
    }
  });
  let dragDepth = 0;
  document.addEventListener("dragenter", event => {
    if ([...event.dataTransfer.types].includes("Files")) { event.preventDefault(); dragDepth++; $("#drop-overlay").hidden = false; }
  });
  document.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("#drop-overlay").hidden = true; });
  document.addEventListener("dragover", event => { if ([...event.dataTransfer.types].includes("Files")) event.preventDefault(); });
  document.addEventListener("drop", event => {
    if (!event.dataTransfer.files.length) return;
    event.preventDefault(); dragDepth = 0; $("#drop-overlay").hidden = true;
    importFiles([...event.dataTransfer.files]).catch(error => toast(error.message, "error"));
  });
}

async function bootstrap() {
  hydrateIcons();
  const [catalog, config, library, presets] = await Promise.all([api("/catalog"), api("/config"), api("/assets?limit=60"), api("/presets")]);
  Object.assign(state, { catalog, config, presets, assets: library.items, assetTotal: library.total });
  rememberAssets(library.items);
  bindEvents();
  chooseCategory("color", "color_levels");
  renderPresets();
  renderPipeline();
  $("#operation-total").textContent = catalog.operation_count;
  $("#connection-status").innerHTML = "<i></i>本地服务已连接";
  let initialId = localStorage.getItem("studio-current");
  if (!library.items.length) {
    const samples = await api("/samples", { method: "POST", body: {} });
    rememberAssets(samples); initialId = samples[0].id;
  } else if (!state.assetMap.has(initialId)) initialId = library.items[0].id;
  if (initialId) await selectAsset(initialId);
  await Promise.all([refreshStats(), refreshJobs()]);
  state.jobPoll = setInterval(async () => {
    const active = state.jobs.some(job => ["queued", "running", "cancelling"].includes(job.status));
    if (!active && state.view !== "jobs") return;
    try {
      const previous = new Map(state.jobs.map(job => [job.id, job.status]));
      await refreshJobs();
      for (const job of state.jobs) {
        if (["completed", "partial", "failed", "cancelled", "interrupted"].includes(job.status) && ["queued", "running", "cancelling"].includes(previous.get(job.id))) {
          const type = ["failed", "interrupted"].includes(job.status) ? "error" : "success";
          toast(`“${job.title}”：${job.message}`, type);
          await refreshStats();
        }
      }
    } catch { /* Keep the current job snapshot while the local server reconnects. */ }
  }, 2000);
}

bootstrap().catch(error => {
  $("#connection-status").textContent = "连接失败";
  const banner = document.createElement("div");
  banner.className = "error-banner";
  banner.textContent = `启动未完成：${error.message} 请确认 start.bat 正在运行，然后刷新页面。`;
  $("#editor-view").prepend(banner);
  toast(error.message, "error");
});
