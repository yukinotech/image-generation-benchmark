"use strict";

const $ = id => document.getElementById(id);
const route = new URLSearchParams(location.search);
const caseId = route.get("case");
let data;
let selectedModels = new Set();
let copyTimer;
let metadataRequest = 0;
let previewEntries = [];
let previewIndex = 0;
let lastPreviewWheel = -Infinity;

function options(id, items, selected) {
  const select = $(id);
  select.replaceChildren(...items.map(([value, label]) => new Option(label, value)));
  if (items.some(([value]) => value === selected)) select.value = selected;
}

function showMessage(message = "") {
  $("message").textContent = message;
  $("message").hidden = !message;
}

function fillModels(models, onChange) {
  for (const model of models) {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = selectedModels.has(model.id);
    input.addEventListener("change", () => {
      if (input.checked) selectedModels.add(model.id);
      else selectedModels.delete(model.id);
      onChange();
    });
    const label = document.createElement("label");
    label.append(input, model.label);
    $("models").append(label);
  }
}

async function load() {
  try {
    const response = await fetch("./results.json", { cache: "no-cache" });
    if (!response.ok) throw new Error("请稍后再试。");
    data = await response.json();
    selectedModels = new Set(data.models.map(model => model.id));
    render();
  } catch {
    showMessage("无法加载对比结果，请稍后再试。");
  }
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function pageUrl(values) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value) query.set(key, value);
  }
  return query.size ? `?${query}` : "./";
}

function caseLabel({ ratio, width, height, seed }) {
  const size = width ? `${width} × ${height}` : "尺寸见各模型";
  return `${ratio} · ${size} · Seed ${seed}`;
}

function sharedSize(item) {
  const sizes = Object.values(item.image_sizes || {});
  const first = sizes[0] || item;
  return sizes.every(size => size.width === first.width && size.height === first.height) ? first : {};
}

function imageSize(item, model) {
  return item.image_sizes?.[model] || { width: item.width, height: item.height };
}

function imageDescription(item, model) {
  const size = imageSize(item, model.id);
  return model.reference
    ? `${size.width} × ${size.height} · 闭源参考 · Seed 未提供`
    : caseLabel({ ...item, ...size });
}

function imageDetails(item, model, size) {
  const details = element("div", "", "image-details");
  details.append(element("span", `${size.width} × ${size.height}`, "row-meta"));
  const source = item.image_metadata?.[model.id];
  if (!source) return details;
  const title = model.reference ? "参考图信息" : "复现元信息";
  const trigger = element("button", "", "metadata-trigger");
  trigger.type = "button";
  trigger.dataset.tooltip = `查看${title}`;
  trigger.setAttribute("aria-label", `查看${title} · ${model.label}`);
  trigger.setAttribute("aria-haspopup", "dialog");
  const icon = element("span", "{}", "metadata-icon");
  icon.setAttribute("aria-hidden", "true");
  trigger.append(icon);
  trigger.addEventListener("click", () => openMetadata(source, model.label, title));
  details.append(trigger);
  return details;
}

async function openMetadata(source, label, title) {
  const request = ++metadataRequest;
  const dialog = $("metadata-dialog");
  const copy = $("metadata-copy");
  const status = $("metadata-status");
  $("metadata-title").textContent = title;
  $("metadata-model").textContent = label;
  $("metadata-code").textContent = "";
  $("metadata-block").hidden = true;
  copy.textContent = "复制 JSON";
  copy.disabled = true;
  status.hidden = false;
  status.textContent = "正在读取 JSON…";
  dialog.showModal();
  try {
    const response = await fetch(source);
    if (!response.ok) throw new Error("Metadata request failed");
    const metadata = await response.json();
    if (request !== metadataRequest || !dialog.open) return;
    $("metadata-code").textContent = JSON.stringify(metadata, null, 2);
    $("metadata-block").hidden = false;
    status.hidden = true;
    copy.disabled = false;
  } catch {
    if (request === metadataRequest && dialog.open) status.textContent = `无法读取${title}。`;
  }
}

function renderOverview() {
  const ratio = $("ratio").value;
  history.replaceState(null, "", pageUrl({ ratio }));
  const cases = data.cases.filter(item => !ratio || item.ratio === ratio);
  const header = element("tr");
  for (const label of ["提示词与生成条件", "提示词中文含义（翻译）",
    ...data.models.map(model => model.label)]) {
    const cell = element("th", label);
    cell.scope = "col";
    header.append(cell);
  }
  $("comparison-head").replaceChildren(header);
  $("table-header-row").replaceChildren(
    ...Array.from(header.cells, cell => element("div", cell.textContent, "table-header-cell")),
  );

  const rows = document.createDocumentFragment();
  const gallery = [];
  for (const item of cases) {
    const href = pageUrl({ case: item.id, ratio });
    const row = element("tr");
    const promptCell = element("th");
    promptCell.scope = "row";
    const promptLink = element("a", "", "prompt-link");
    promptLink.href = href;
    promptLink.append(
      element("span", item.prompt, "prompt-excerpt"),
      element("span", item.ratio),
      element("span", `Seed ${item.seed}`, "row-meta"),
    );
    promptCell.append(promptLink);
    const translationCell = element("td", "", "translation-cell");
    if (item.translation) {
      const translation = element("a", item.translation, "translation-text");
      translation.href = `${href}#translation`;
      translationCell.append(translation);
    } else {
      translationCell.append(element("span", "—", "row-meta"));
    }
    row.append(promptCell, translationCell);

    for (const model of data.models) {
      const cell = element("td");
      const original = item.images[model.id];
      if (original) {
        const { thumbnail, preview } = item.image_previews[model.id];
        const actual = imageSize(item, model.id);
        const description = imageDescription(item, model);
        const button = element("button", "", "result-link");
        button.type = "button";
        button.setAttribute("aria-label", `放大查看 ${model.label} · ${description}`);
        const position = gallery.length;
        gallery.push({ image: preview, original, model: model.label, description });
        const image = element("img");
        image.alt = model.label + " 生成图";
        image.loading = "lazy";
        image.decoding = "async";
        image.addEventListener("error", () => image.replaceWith(element("span", "图片加载失败")));
        image.src = thumbnail;
        button.append(image);
        button.addEventListener("click", () => openPreview(gallery, position));
        cell.append(button, imageDetails(item, model, actual));
      } else {
        cell.append(element("span", model.reference ? "—" : "暂无图片", "result-link"));
      }
      row.append(cell);
    }
    rows.append(row);
  }
  $("comparison-body").replaceChildren(rows);
  const localModels = data.models.filter(model => !model.reference).length;
  const referenceCount = data.models.length - localModels;
  const reference = referenceCount ? ` · ${referenceCount} 个闭源参考` : "";
  $("table-summary").textContent = `${cases.length} 组对比 · ${localModels} 个模型${reference}`;
  updateComparisonLayout();
}

function updateComparisonLayout() {
  if ($("overview").hidden) return;
  const header = $("comparison-head");
  if (!header.rows.length) return;
  // The accessible header still sizes the table. Its visual copy lives in the sticky toolbar.
  const scroller = $("table-scroll");
  const height = header.getBoundingClientRect().height;
  const widths = Array.from(header.rows[0].cells, cell => cell.getBoundingClientRect().width);
  const visual = $("table-header-row");
  const overlap = height + scroller.clientTop;
  $("overview").style.setProperty("--table-header-height", `${overlap}px`);
  visual.style.height = `${height}px`;
  visual.style.width = `${scroller.scrollWidth}px`;
  widths.forEach((width, index) => { visual.children[index].style.width = `${width}px`; });
  updateHorizontalScroll();
}

function syncHorizontalScroll(source, target) {
  if (target.scrollLeft !== source.scrollLeft) target.scrollLeft = source.scrollLeft;
  const table = $("table-scroll");
  $("table-header-scroll").scrollLeft = table.scrollLeft;
  const limit = table.scrollWidth - table.clientWidth;
  $("table-left").disabled = table.scrollLeft <= 1;
  $("table-right").disabled = table.scrollLeft >= limit - 1;
}

function updateHorizontalScroll() {
  const table = $("table-scroll");
  const top = $("table-scroll-top");
  const limit = table.scrollWidth - table.clientWidth;
  $("table-navigation").hidden = limit <= 0;
  if (limit > 0) $("table-scroll-track").style.width = `${top.clientWidth + limit}px`;
  syncHorizontalScroll(table, top);
}

function scrollModelColumns(direction) {
  const column = $("comparison-head").querySelector("th:nth-child(3)");
  if (!column) return;
  const table = $("table-scroll");
  table.scrollLeft += column.offsetWidth * direction;
  syncHorizontalScroll(table, $("table-scroll-top"));
}

function openPreview(entries, index) {
  previewEntries = entries;
  previewIndex = index;
  lastPreviewWheel = -Infinity;
  renderPreview();
  $("preview").showModal();
}

function renderPreview() {
  const entry = previewEntries[previewIndex];
  $("preview-title").textContent = entry.model;
  $("preview-caption").textContent = entry.description;
  $("preview-position").textContent = `${previewIndex + 1} / ${previewEntries.length}`;
  const image = $("preview-image");
  $("preview-error").hidden = true;
  image.hidden = false;
  image.alt = `${entry.model} · ${entry.description}`;
  image.src = entry.image;
  $("original").href = entry.original;
  $("preview-prev").setAttribute("aria-disabled", String(previewIndex === 0));
  $("preview-next").setAttribute("aria-disabled", String(previewIndex === previewEntries.length - 1));
}

function stepPreview(direction) {
  const next = previewIndex + direction;
  if (next < 0 || next >= previewEntries.length) return;
  previewIndex = next;
  renderPreview();
}

function setPreviewFullscreen(expanded) {
  $("preview").classList.toggle("is-fullscreen", expanded);
  $("preview-fullscreen").textContent = expanded ? "退出全屏" : "全屏";
}

function exitPreviewFullscreen() {
  setPreviewFullscreen(false);
  if (document.fullscreenElement === $("preview-view")) {
    document.exitFullscreen().catch(() => {});
  }
}

function card(source, model, size, details, showPreview) {
  const item = element("article", "", "card");
  item.style.setProperty("--ratio", `${size.width} / ${size.height}`);
  const button = element("button", "", "image-area");
  button.type = "button";
  button.setAttribute("aria-label", `放大查看 ${model.label} 输出`);
  const image = element("img");
  image.alt = model.label + " 生成图";
  image.width = size.width;
  image.height = size.height;
  image.decoding = "async";
  image.addEventListener("error", () => {
    button.replaceWith(element("div", "图片加载失败", "image-area"));
  });
  image.src = source;
  button.append(image);
  button.addEventListener("click", showPreview);
  const body = element("div", "", "card-body");
  body.append(element("h3", model.label), details);
  item.append(button, body);
  return item;
}

function render() {
  if (!data.cases.length) {
    $("workspace").hidden = true;
    showMessage("暂无对比结果。");
    return;
  }
  $("workspace").hidden = false;
  showMessage();
  const current = data.cases.find(item => item.id === caseId);
  if (!current) {
    const ratios = [...new Set(data.cases.map(item => item.ratio))];
    options("ratio", [["", "全部比例"], ...ratios.map(ratio => [ratio, ratio])], route.get("ratio"));
    renderOverview();
    return;
  }

  $("filters").hidden = $("overview").hidden = $("table-summary").hidden = true;
  $("resolution-guide").hidden = true;
  $("back-to-list").hidden = $("prompt-panel").hidden = $("detail-results").hidden = false;
  $("page-title").textContent = "模型对比详情";
  $("page-description").textContent = "查看同一条提示词下各模型的生成结果。";
  $("back-to-list").href = pageUrl({ ratio: route.get("ratio") });
  $("prompt-text").textContent = current.prompt;
  const translation = $("translation");
  translation.hidden = !current.translation;
  $("translation-text").textContent = current.translation || "";
  if (current.translation && location.hash === "#translation") {
    translation.open = true;
    translation.scrollIntoView();
  }
  $("case-summary").textContent = caseLabel({ ...sharedSize(current), ratio: current.ratio, seed: current.seed });
  const models = data.models.filter(model => current.images[model.id]);
  if (models.some(model => model.reference)) {
    $("page-description").textContent = "本地模型共用同一提示词、目标画幅和 Seed。闭源参考图使用同一提示词，Seed 和生成参数未提供。";
  }
  fillModels(models, () => renderCards(current, models));
  renderCards(current, models);
}

function renderCards(current, models) {
  const visible = models.filter(model => selectedModels.has(model.id));
  const gallery = visible.map(model => ({
    image: current.image_previews[model.id].preview,
    original: current.images[model.id],
    model: model.label,
    description: imageDescription(current, model),
  }));
  const cards = visible.map((model, index) => {
    const size = imageSize(current, model.id);
    return card(current.image_previews[model.id].thumbnail, model, size, imageDetails(current, model, size),
      () => openPreview(gallery, index));
  });
  $("grid").replaceChildren(...(cards.length ? cards : [element("p", "请选择要显示的模型。")]));
}

$("ratio").addEventListener("change", renderOverview);
const comparisonScroll = $("table-scroll");
const topComparisonScroll = $("table-scroll-top");
comparisonScroll.addEventListener("scroll", () => syncHorizontalScroll(comparisonScroll, topComparisonScroll));
topComparisonScroll.addEventListener("scroll", () => syncHorizontalScroll(topComparisonScroll, comparisonScroll));
$("table-left").addEventListener("click", () => scrollModelColumns(-1));
$("table-right").addEventListener("click", () => scrollModelColumns(1));
const comparisonResize = new ResizeObserver(updateComparisonLayout);
comparisonResize.observe(comparisonScroll);
comparisonResize.observe($("comparison"));
comparisonResize.observe($("comparison-head"));
$("preview-prev").addEventListener("click", () => stepPreview(-1));
$("preview-next").addEventListener("click", () => stepPreview(1));
$("preview-fullscreen").addEventListener("click", async () => {
  const dialog = $("preview");
  if (dialog.classList.contains("is-fullscreen")) {
    exitPreviewFullscreen();
    return;
  }
  setPreviewFullscreen(true);
  const view = $("preview-view");
  if (!document.fullscreenEnabled) return;
  try {
    await view.requestFullscreen();
    if (!dialog.open || !dialog.classList.contains("is-fullscreen")) exitPreviewFullscreen();
  } catch {
    // Embedded browsers may block native fullscreen. Keep the viewport-filling layout.
  }
});
$("preview-view").addEventListener("fullscreenchange", () => {
  if (document.fullscreenElement !== $("preview-view")) setPreviewFullscreen(false);
});
$("preview").addEventListener("cancel", event => {
  if (!$("preview").classList.contains("is-fullscreen")) return;
  event.preventDefault();
  exitPreviewFullscreen();
});
$("preview").addEventListener("close", exitPreviewFullscreen);
$("preview-image").addEventListener("error", () => {
  $("preview-image").hidden = true;
  $("preview-error").hidden = false;
});
$("preview").addEventListener("keydown", event => {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  event.preventDefault();
  stepPreview(event.key === "ArrowLeft" ? -1 : 1);
});
$("preview").addEventListener("wheel", event => {
  if (event.ctrlKey || event.metaKey) return;
  const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
  if (!delta) return;
  event.preventDefault();
  // Trackpads emit wheel events in bursts. Limit the navigation rate.
  const now = performance.now();
  if (now - lastPreviewWheel < 180) return;
  lastPreviewWheel = now;
  stepPreview(Math.sign(delta));
}, { passive: false });
async function copyText(target) {
  try {
    await navigator.clipboard.writeText(target.textContent);
    return "已复制";
  } catch {
    const range = document.createRange();
    range.selectNodeContents(target);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return "已选中，请手动复制";
  }
}

$("copy").addEventListener("click", async () => {
  clearTimeout(copyTimer);
  $("copy").textContent = await copyText($("prompt-text"));
  copyTimer = setTimeout(() => { $("copy").textContent = "复制原文"; }, 2000);
});
$("metadata-copy").addEventListener("click", async () => {
  const request = metadataRequest;
  const message = await copyText($("metadata-code"));
  if (request === metadataRequest && $("metadata-dialog").open) {
    $("metadata-copy").textContent = message;
  }
});

load();
