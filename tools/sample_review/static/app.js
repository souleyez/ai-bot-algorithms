const stateLabels = {
  pending: "待复核",
  positive: "AI 正样本",
  negative: "AI 负样本",
  discard: "淘汰",
};

const orderStorageKey = "sampleReviewStableOrder:v1";

const store = {
  items: [],
  nodes: new Map(),
  group: "all",
  decision: "all",
  search: "",
  activeId: null,
  saves: 0,
  deletedCount: 0,
  serverDeletedCount: 0,
  confirmedCount: 0,
  confirmedPositiveCount: 0,
  confirmedNegativeCount: 0,
  reviewQueueCount: 0,
  boxReviewQueueCount: 0,
  pendingDeletes: new Set(),
  algorithm: "takeaway",
  mode: "aiReview",
  annotationItem: null,
  annotations: [],
  selectedAnnotation: -1,
  pointerAction: null,
  annotationHadSavedBoxes: false,
  annotationAiBusy: false,
  bulkReviewBusy: false,
  viewRestoreToken: 0,
  reportAlgorithm: "takeaway",
  reportStatus: null,
  reportRunId: "",
  reportBusy: false,
  reportPollTimer: null,
};

const elements = {
  gallery: document.querySelector("#gallery"),
  template: document.querySelector("#itemTemplate"),
  groupFilter: document.querySelector("#groupFilter"),
  decisionFilter: document.querySelector("#decisionFilter"),
  searchInput: document.querySelector("#searchInput"),
  visibleCount: document.querySelector("#visibleCount"),
  emptyState: document.querySelector("#emptyState"),
  saveState: document.querySelector("#saveState"),
  uploadDialog: document.querySelector("#uploadDialog"),
  uploadForm: document.querySelector("#uploadForm"),
  uploadFiles: document.querySelector("#uploadFiles"),
  uploadCategory: document.querySelector("#uploadCategory"),
  uploadSelection: document.querySelector("#uploadSelection"),
  uploadResult: document.querySelector("#uploadResult"),
  uploadSubmit: document.querySelector("#uploadSubmit"),
  uploadDrop: document.querySelector("#uploadDrop"),
  annotateDialog: document.querySelector("#annotateDialog"),
  annotateImage: document.querySelector("#annotateImage"),
  annotateCanvas: document.querySelector("#annotateCanvas"),
  annotateTitle: document.querySelector("#annotateTitle"),
  annotateMeta: document.querySelector("#annotateMeta"),
  annotateHint: document.querySelector("#annotateHint"),
  annotationAuto: document.querySelector("#annotationAuto"),
  annotationDelete: document.querySelector("#annotationDelete"),
  annotationClear: document.querySelector("#annotationClear"),
  annotationSave: document.querySelector("#annotationSave"),
  reviewAll: document.querySelector("#reviewAll"),
  decisionControl: document.querySelector("#decisionFilter"),
  reportingPanel: document.querySelector("#reportingPanel"),
  filters: document.querySelector(".filters"),
  algorithmTabs: document.querySelector(".algorithm-tabs"),
  summary: document.querySelector(".summary"),
  uploadOpen: document.querySelector("#uploadOpen"),
  exportLink: document.querySelector(".export"),
  reportJobBadge: document.querySelector("#reportJobBadge"),
  reportRunId: document.querySelector("#reportRunId"),
  reportRunState: document.querySelector("#reportRunState"),
  reportEligible: document.querySelector("#reportEligible"),
  reportBoxes: document.querySelector("#reportBoxes"),
  reportSuccess: document.querySelector("#reportSuccess"),
  reportRemaining: document.querySelector("#reportRemaining"),
  reportDeduplicated: document.querySelector("#reportDeduplicated"),
  reportWithheld: document.querySelector("#reportWithheld"),
  reportDevices: document.querySelector("#reportDevices"),
  reportResult: document.querySelector("#reportResult"),
  reportPrepare: document.querySelector("#reportPrepare"),
  reportCanary: document.querySelector("#reportCanary"),
  reportSend: document.querySelector("#reportSend"),
  reportConfirmationWrap: document.querySelector("#reportConfirmationWrap"),
  reportConfirmation: document.querySelector("#reportConfirmation"),
  reportMessage: document.querySelector("#reportMessage"),
  reportHistory: document.querySelector("#reportHistory"),
};

function isBoxReview() {
  return store.mode === "boxReview";
}

function isReporting() {
  return store.mode === "reporting";
}

function queueEndpoint() {
  return isBoxReview() ? "api/box-review-items" : "api/items";
}

function configureWorkspaceVisibility() {
  const reporting = isReporting();
  elements.reportingPanel.hidden = !reporting;
  elements.filters.hidden = reporting;
  elements.gallery.hidden = reporting;
  elements.algorithmTabs.hidden = reporting;
  elements.summary.hidden = reporting;
  elements.uploadOpen.hidden = reporting;
  elements.exportLink.hidden = reporting;
  if (reporting) elements.emptyState.hidden = true;
}

function shortGroup(group) {
  return group.replace(/^\d+_/, "").replace(/_\d+$/, "");
}

function savedOrder() {
  try {
    const value = JSON.parse(window.localStorage.getItem(orderStorageKey) || "[]");
    return Array.isArray(value) ? value.filter((id) => typeof id === "string") : [];
  } catch {
    return [];
  }
}

function persistOrder() {
  try {
    window.localStorage.setItem(orderStorageKey, JSON.stringify(store.items.map((item) => item.id)));
  } catch {
    // The in-memory order remains stable even if browser storage is unavailable.
  }
}

function applySavedOrder(items) {
  const byId = new Map(items.map((item) => [item.id, item]));
  const ordered = [];
  for (const id of savedOrder()) {
    const item = byId.get(id);
    if (!item) continue;
    ordered.push(item);
    byId.delete(id);
  }
  for (const item of items) {
    if (!byId.has(item.id)) continue;
    ordered.push(item);
    byId.delete(item.id);
  }
  return ordered;
}

function mergeIncomingItems(incoming) {
  const incomingById = new Map(
    incoming
      .filter((item) => !store.pendingDeletes.has(item.id))
      .map((item) => [item.id, item]),
  );
  const merged = [];
  for (const current of store.items) {
    const latest = incomingById.get(current.id);
    if (!latest) {
      store.nodes.delete(current.id);
      continue;
    }
    Object.assign(current, latest);
    merged.push(current);
    incomingById.delete(current.id);
  }
  const added = [];
  for (const item of incoming) {
    if (!incomingById.has(item.id)) continue;
    merged.push(item);
    added.push(item);
    incomingById.delete(item.id);
  }
  store.items = merged;
  persistOrder();
  return added;
}

function applyHealth(health) {
  store.serverDeletedCount = health.deleted || 0;
  store.deletedCount = store.serverDeletedCount + store.pendingDeletes.size;
  store.confirmedCount = health.confirmed || 0;
  store.confirmedPositiveCount = health.confirmedPositive || 0;
  store.confirmedNegativeCount = health.confirmedNegative || 0;
  store.reviewQueueCount = health.reviewQueue || 0;
  store.boxReviewQueueCount = health.boxReviewQueue || 0;
}

function updateCounts() {
  const boxed = store.items.filter((item) => (item.aiAnnotations || []).length > 0).length;
  document.querySelector("#pendingCount").textContent = store.items.length;
  document.querySelector("#positiveCount").textContent = isBoxReview()
    ? boxed
    : store.items.filter((item) => item.aiDecision === "positive").length;
  document.querySelector("#negativeCount").textContent = isBoxReview()
    ? store.items.length - boxed
    : store.items.filter((item) => item.aiDecision === "negative").length;
  document.querySelector("#pendingLabel").textContent = isBoxReview() ? "待补框复审" : "待人工复核";
  document.querySelector("#positiveLabel").textContent = isBoxReview() ? "有 AI 候选框" : "AI 正样本";
  document.querySelector("#negativeLabel").textContent = isBoxReview() ? "待手动画框" : "AI 负样本";
  document.querySelector("#discardCount").textContent = store.deletedCount;
  const confirmed = document.querySelector("#confirmedCount");
  confirmed.textContent = store.confirmedCount;
  confirmed.closest(".summary-item").title = `已人工确认：正样本 ${store.confirmedPositiveCount}，负样本 ${store.confirmedNegativeCount}`;
  document.querySelector("#tabTakeawayCount").textContent = store.items.filter((item) => item.algorithm === "takeaway").length;
  document.querySelector("#tabDoorCount").textContent = store.items.filter((item) => item.algorithm === "door").length;
  document.querySelector("#tabWorkwearCount").textContent = store.items.filter((item) => item.algorithm === "workwear").length;
  document.querySelector("#aiQueueCount").textContent = store.reviewQueueCount;
  document.querySelector("#boxReviewCount").textContent = store.boxReviewQueueCount;
}

function bindBulkReview() {
  elements.reviewAll.addEventListener("click", async () => {
    const snapshot = visibleItems();
    if (store.bulkReviewBusy || !snapshot.length) return;
    const positive = snapshot.filter((item) => item.aiDecision === "positive").length;
    const negative = snapshot.filter((item) => item.aiDecision === "negative").length;
    const algorithmLabels = { takeaway: "外卖服", workwear: "新世界工服", door: "小门识别" };
    const confirmed = window.confirm(
      `确认当前“${algorithmLabels[store.algorithm]}”页面显示的 ${snapshot.length} 张 AI 初标全部正确吗？\n\n`
      + `正样本 ${positive} 张，负样本 ${negative} 张。\n`
      + "只处理当前来源、状态和搜索筛选下的图片；其他算法和之后新进来的图片不会被确认。",
    );
    if (!confirmed) return;
    store.bulkReviewBusy = true;
    const viewState = captureViewState();
    updateCounts();
    render(viewState);
    elements.saveState.textContent = "正在批量确认";
    try {
      const response = await fetch("api/review-all", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: snapshot.map((item) => item.id) }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      await refreshItems();
      elements.saveState.textContent = `已批量确认 ${result.reviewed} 张${result.skipped ? `，跳过 ${result.skipped} 张` : ""}`;
    } catch (error) {
      elements.saveState.textContent = `批量确认失败：${error.message}`;
    } finally {
      store.bulkReviewBusy = false;
      updateCounts();
      render(captureViewState());
    }
  });
}

function visibleItems() {
  const needle = store.search.trim().toLowerCase();
  const filtered = store.items.filter((item) => {
    if (item.algorithm !== store.algorithm) return false;
    if (store.group !== "all" && item.group !== store.group) return false;
    if (isBoxReview()) {
      const hasBoxes = (item.aiAnnotations || []).length > 0;
      if (store.decision === "with-boxes" && !hasBoxes) return false;
      if (store.decision === "without-boxes" && hasBoxes) return false;
    } else if (store.decision !== "all" && item.aiDecision !== store.decision) return false;
    if (needle && !`${item.index} ${item.filename} ${item.group}`.toLowerCase().includes(needle)) return false;
    return true;
  });
  return filtered;
}

function createSampleNode(item) {
  const node = elements.template.content.firstElementChild.cloneNode(true);
  node.dataset.id = item.id;
  node.querySelector(".image-wrap").addEventListener("click", () => {
    const current = store.items.find((candidate) => candidate.id === item.id);
    if (current) openAnnotation(current);
  });
  node.addEventListener("focusin", () => { store.activeId = item.id; });
  for (const button of node.querySelectorAll("[data-decision]")) {
    button.addEventListener("click", () => setDecision(item.id, button.dataset.decision));
  }
  return node;
}

function updateSampleNode(node, item) {
  node.dataset.id = item.id;
  node.dataset.decision = isBoxReview() ? "positive" : item.aiDecision;
  const image = node.querySelector("img");
  if (image.getAttribute("src") !== item.imageUrl) image.src = item.imageUrl;
  image.alt = `${shortGroup(item.group)} 第 ${item.index} 张`;
  node.querySelector(".sample-index").textContent = `#${String(item.index).padStart(4, "0")}`;
  const confidence = Number(item.aiConfidence || 0);
  const candidateCount = (item.aiAnnotations || []).length;
  const hasEmbeddedBlue = (item.aiAnnotations || []).some((box) => box.source === "embedded-blue");
  node.querySelector(".sample-state").textContent = isBoxReview()
    ? (candidateCount
      ? `${hasEmbeddedBlue ? "蓝框" : "红框候选"} ${candidateCount}`
      : "待手动画框")
    : `${stateLabels[item.aiDecision]}${confidence ? ` ${Math.round(confidence * 100)}%` : ""}`;
  node.querySelector(".sample-group").textContent = shortGroup(item.group);
  node.querySelector(".sample-group").title = item.group;
  node.querySelector(".sample-file").textContent = item.filename;
  node.querySelector(".sample-file").title = item.filename;
  node.querySelector('[data-decision="positive"]').textContent = isBoxReview() ? "确认框" : "正样本";
  node.querySelector('[data-decision="negative"]').textContent = isBoxReview() ? "改负样本" : "负样本";
  node.querySelector('[data-decision="discard"]').textContent = "淘汰删除";
  const annotationCount = node.querySelector(".annotation-count");
  const effectiveAnnotations = item.annotations?.length ? item.annotations : item.aiAnnotations;
  if (effectiveAnnotations?.length) {
    annotationCount.hidden = false;
    annotationCount.textContent = `${effectiveAnnotations.length} 框`;
  } else {
    annotationCount.hidden = true;
    annotationCount.textContent = "";
  }
  node.querySelector(".decision-control").hidden = false;
}

function cardById(id) {
  if (!id) return null;
  return [...elements.gallery.children].find((node) => node.dataset.id === id) || null;
}

function captureViewState(preferredId = null) {
  const cards = [...elements.gallery.children];
  const active = document.activeElement;
  const focusedCard = active?.closest?.(".sample") || null;
  const preferredCard = cardById(preferredId);
  const anchor = preferredCard
    || focusedCard
    || cards.find((card) => card.getBoundingClientRect().bottom > 0)
    || cards[0]
    || null;
  const anchorIndex = anchor ? cards.indexOf(anchor) : 0;
  return {
    scrollX: window.scrollX,
    scrollY: window.scrollY,
    anchorId: anchor?.dataset.id || null,
    anchorIndex,
    anchorTop: anchor?.getBoundingClientRect().top ?? 0,
    focusId: focusedCard?.dataset.id || preferredId,
    focusDecision: active?.dataset?.decision || null,
    focusCard: active === focusedCard,
  };
}

function restoreViewState(viewState) {
  if (!viewState) return;
  const cards = [...elements.gallery.children];
  const anchor = cardById(viewState.anchorId)
    || cards[Math.min(viewState.anchorIndex, Math.max(0, cards.length - 1))]
    || null;
  const focusCard = cardById(viewState.focusId) || anchor;
  const restore = () => {
    window.scrollTo(viewState.scrollX, viewState.scrollY);
    if (!focusCard) return;
    const focusTarget = viewState.focusDecision
      ? focusCard.querySelector(`[data-decision="${viewState.focusDecision}"]`)
      : (viewState.focusCard ? focusCard : null);
    if (focusTarget) focusTarget.focus({ preventScroll: true });
    store.activeId = focusCard.dataset.id;
  };
  const token = ++store.viewRestoreToken;
  restore();
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      if (token === store.viewRestoreToken) restore();
    });
  });
}

function render(viewState = null) {
  configureWorkspaceVisibility();
  if (isReporting()) return;
  const fragment = document.createDocumentFragment();
  const items = visibleItems();
  for (const item of items) {
    let node = store.nodes.get(item.id);
    if (!node) {
      node = createSampleNode(item);
      store.nodes.set(item.id, node);
    }
    updateSampleNode(node, item);
    fragment.append(node);
  }
  elements.gallery.replaceChildren(fragment);
  elements.visibleCount.textContent = `${items.length} 张`;
  elements.reviewAll.hidden = isBoxReview();
  elements.reviewAll.disabled = isBoxReview() || store.bulkReviewBusy || items.length === 0;
  elements.reviewAll.textContent = store.bulkReviewBusy
    ? "批量确认中"
    : `本页全部审过（${items.length}）`;
  elements.emptyState.hidden = items.length !== 0;
  restoreViewState(viewState);
}

function reviewMutationKey(item, payload) {
  const fingerprint = JSON.stringify(payload);
  if (!item.pendingReviewMutation || item.pendingReviewMutation.fingerprint !== fingerprint) {
    item.pendingReviewMutation = { fingerprint, key: crypto.randomUUID() };
  }
  return item.pendingReviewMutation.key;
}

function clearReviewMutation(item) {
  delete item.pendingReviewMutation;
}

async function setDecision(id, decision) {
  const item = store.items.find((candidate) => candidate.id === id);
  if (!item) return;
  const viewState = captureViewState(id);
  if (decision === "discard") {
    await discardItem(item, viewState);
    return;
  }
  const boxReview = isBoxReview();
  const candidateAnnotations = item.aiAnnotations || [];
  if (boxReview && decision === "positive" && candidateAnnotations.length === 0) {
    openAnnotation(item);
    elements.annotateHint.textContent = "当前没有候选框，请手动画框后保存";
    return;
  }
  store.saves += 1;
  elements.saveState.textContent = "正在保存";
  try {
    const payload = { decision };
    if (boxReview && decision === "positive") payload.annotations = candidateAnnotations;
    const requestPayload = { ...payload, expectedRevision: item.reviewRevision || 0 };
    const response = await fetch(`api/items/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": reviewMutationKey(item, requestPayload),
      },
      body: JSON.stringify(requestPayload),
    });
    const saved = await response.json();
    if (!response.ok) throw new Error(saved.error || `HTTP ${response.status}`);
    if (!saved.humanReviewed) throw new Error("人工复核状态未保存");
    if (boxReview && decision === "positive" && !(saved.annotations || []).length) {
      throw new Error("目标框未保存");
    }
    clearReviewMutation(item);
    const itemIndex = store.items.findIndex((candidate) => candidate.id === id);
    if (itemIndex >= 0) store.items.splice(itemIndex, 1);
    store.nodes.delete(id);
    rebuildGroupFilter();
    updateCounts();
    render(viewState);
  } catch (error) {
    elements.saveState.textContent = "保存失败，请重试";
    return;
  } finally {
    store.saves -= 1;
  }
  elements.saveState.textContent = store.saves
    ? "正在保存"
    : boxReview && decision === "positive"
      ? "目标框已确认，已移出补框队列"
      : boxReview && decision === "negative"
        ? "已改为负样本，已移出补框队列"
        : "已复核，已移出待审队列";
}

async function discardItem(item, viewState = captureViewState(item.id)) {
  const itemIndex = store.items.findIndex((candidate) => candidate.id === item.id);
  if (itemIndex < 0 || store.pendingDeletes.has(item.id)) return;
  const removedItem = store.items[itemIndex];
  store.items.splice(itemIndex, 1);
  store.pendingDeletes.add(item.id);
  store.nodes.delete(item.id);
  store.activeId = null;
  store.deletedCount = store.serverDeletedCount + store.pendingDeletes.size;
  rebuildGroupFilter();
  updateCounts();
  render(viewState);
  store.saves += 1;
  elements.saveState.textContent = `已移除，后台删除中（${store.pendingDeletes.size}）`;
  try {
    const response = await fetch(`api/items/${encodeURIComponent(item.id)}`, { method: "DELETE" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    store.pendingDeletes.delete(item.id);
    store.deletedCount = store.serverDeletedCount + store.pendingDeletes.size;
    persistOrder();
    updateCounts();
    elements.saveState.textContent = store.pendingDeletes.size
      ? `已删除，剩余 ${store.pendingDeletes.size} 张后台处理中`
      : "已删除审核副本";
  } catch (error) {
    store.pendingDeletes.delete(item.id);
    store.items.splice(Math.min(itemIndex, store.items.length), 0, removedItem);
    store.deletedCount = store.serverDeletedCount + store.pendingDeletes.size;
    store.nodes.delete(item.id);
    rebuildGroupFilter();
    updateCounts();
    render(captureViewState());
    elements.saveState.textContent = "删除失败，请重试";
  } finally {
    store.saves -= 1;
  }
}

function rebuildGroupFilter() {
  const current = store.group;
  elements.groupFilter.replaceChildren(new Option("全部来源", "all"));
  const groups = [...new Set(
    store.items.filter((item) => item.algorithm === store.algorithm).map((item) => item.group)
  )].sort();
  for (const group of groups) {
    const option = document.createElement("option");
    option.value = group;
    option.textContent = `${shortGroup(group)} (${store.items.filter((item) => item.group === group).length})`;
    elements.groupFilter.append(option);
  }
  store.group = groups.includes(current) ? current : "all";
  elements.groupFilter.value = store.group;
}

async function refreshItems({ announce = false } = {}) {
  if (isReporting()) return;
  const viewState = captureViewState();
  const [response, healthResponse] = await Promise.all([
    fetch(queueEndpoint(), { cache: "no-store" }),
    fetch("healthz", { cache: "no-store" }),
  ]);
  if (!response.ok || !healthResponse.ok) throw new Error(`HTTP ${response.status}/${healthResponse.status}`);
  const [incoming, health] = await Promise.all([response.json(), healthResponse.json()]);
  const added = mergeIncomingItems(incoming);
  applyHealth(health);
  rebuildGroupFilter();
  updateCounts();
  render(viewState);
  if (announce && added.length) elements.saveState.textContent = `新增 ${added.length} 张，已追加到底部`;
}

function bindFilters() {
  elements.groupFilter.addEventListener("change", (event) => { store.group = event.target.value; render(); });
  elements.decisionFilter.addEventListener("change", (event) => { store.decision = event.target.value; render(); });
  elements.searchInput.addEventListener("input", (event) => { store.search = event.target.value; render(); });
  for (const button of document.querySelectorAll("[data-summary-filter]")) {
    button.addEventListener("click", () => {
      const requested = button.dataset.summaryFilter;
      store.decision = isBoxReview() && requested === "positive"
        ? "with-boxes"
        : isBoxReview() && requested === "negative"
          ? "without-boxes"
          : isBoxReview() && requested === "discard"
            ? "all"
            : requested;
      elements.decisionFilter.value = store.decision;
      render();
    });
  }
  for (const button of document.querySelectorAll("[data-algorithm]")) {
    button.addEventListener("click", () => {
      store.algorithm = button.dataset.algorithm;
      document.querySelectorAll("[data-algorithm]").forEach((candidate) => {
        candidate.classList.toggle("active", candidate === button);
      });
      store.group = "all";
      rebuildGroupFilter();
      render();
    });
  }
  document.addEventListener("keydown", (event) => {
    if (isReporting()) return;
    if (!store.activeId || ["INPUT", "SELECT"].includes(document.activeElement.tagName)) return;
    const map = { "1": "positive", "2": "negative", "3": "discard" };
    if (map[event.key]) {
      event.preventDefault();
      setDecision(store.activeId, map[event.key]);
    }
  });
}

function configureModeControls() {
  if (isReporting()) {
    document.querySelectorAll("[data-queue-mode]").forEach((button) => {
      button.classList.toggle("active", button.dataset.queueMode === store.mode);
    });
    configureWorkspaceVisibility();
    return;
  }
  elements.decisionFilter.replaceChildren(
    isBoxReview()
      ? new Option("全部待补框", "all")
      : new Option("全部 AI 初标", "all"),
    isBoxReview()
      ? new Option("有 AI 候选框", "with-boxes")
      : new Option("AI 正样本", "positive"),
    isBoxReview()
      ? new Option("待手动画框", "without-boxes")
      : new Option("AI 负样本", "negative"),
  );
  store.decision = "all";
  elements.decisionFilter.value = "all";
  document.querySelectorAll("[data-queue-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.queueMode === store.mode);
  });
}

function bindQueueMode() {
  for (const button of document.querySelectorAll("[data-queue-mode]")) {
    button.addEventListener("click", async () => {
      if (button.dataset.queueMode === store.mode) return;
      store.mode = button.dataset.queueMode;
      configureModeControls();
      if (isReporting()) {
        elements.saveState.textContent = "正在载入客户上报状态";
        try {
          await refreshReporting();
          elements.saveState.textContent = "客户上报默认关闭，请先生成预览";
        } catch (error) {
          elements.saveState.textContent = `上报状态载入失败：${error.message}`;
        }
        return;
      }
      store.items = [];
      store.nodes.clear();
      store.group = "all";
      rebuildGroupFilter();
      updateCounts();
      render();
      elements.saveState.textContent = isBoxReview() ? "正在载入补框复审区" : "正在载入 AI 初审区";
      try {
        await refreshItems();
        elements.saveState.textContent = isBoxReview()
          ? "点击图片复审 AI 候选框，保存后自动移出"
          : "已载入，修改自动保存";
      } catch (error) {
        elements.saveState.textContent = `切换失败：${error.message}`;
      }
    });
  }
}

function bindUpload() {
  document.querySelector("#uploadOpen").addEventListener("click", () => {
    elements.uploadResult.textContent = "";
    elements.uploadDialog.showModal();
  });
  document.querySelector("#uploadClose").addEventListener("click", () => elements.uploadDialog.close());
  elements.uploadFiles.addEventListener("change", () => {
    const count = elements.uploadFiles.files.length;
    elements.uploadSelection.textContent = count ? `已选择 ${count} 张` : "单张不超过 15 MB，一次最多 50 张";
  });
  for (const eventName of ["dragenter", "dragover"]) {
    elements.uploadDrop.addEventListener(eventName, (event) => {
      event.preventDefault();
      elements.uploadDrop.classList.add("dragging");
    });
  }
  for (const eventName of ["dragleave", "drop"]) {
    elements.uploadDrop.addEventListener(eventName, () => elements.uploadDrop.classList.remove("dragging"));
  }
  elements.uploadDrop.addEventListener("drop", (event) => {
    event.preventDefault();
    elements.uploadFiles.files = event.dataTransfer.files;
    elements.uploadSelection.textContent = `已选择 ${event.dataTransfer.files.length} 张`;
  });
  elements.uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const files = [...elements.uploadFiles.files];
    if (!files.length) return;
    if (files.length > 50) {
      elements.uploadResult.textContent = "一次最多上传 50 张";
      return;
    }
    const form = new FormData();
    form.append("category", elements.uploadCategory.value);
    for (const file of files) form.append("files", file, file.name);
    elements.uploadSubmit.disabled = true;
    elements.uploadResult.textContent = "正在上传并处理";
    try {
      const response = await fetch("api/upload", { method: "POST", body: form });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      elements.uploadResult.textContent = `新增 ${result.added.length} 张，跳过 ${result.skipped.length} 张`;
      elements.uploadFiles.value = "";
      elements.uploadSelection.textContent = "单张不超过 15 MB，一次最多 50 张";
      await refreshItems({ announce: true });
    } catch (error) {
      elements.uploadResult.textContent = `上传失败：${error.message}`;
    } finally {
      elements.uploadSubmit.disabled = false;
    }
  });
}

function canvasPoint(event) {
  const rect = elements.annotateCanvas.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
  };
}

function hitAnnotation(point) {
  for (let index = store.annotations.length - 1; index >= 0; index -= 1) {
    const box = store.annotations[index];
    if (point.x >= box.x && point.x <= box.x + box.w && point.y >= box.y && point.y <= box.y + box.h) {
      return index;
    }
  }
  return -1;
}

function annotationHandles(box) {
  return {
    nw: { x: box.x, y: box.y },
    ne: { x: box.x + box.w, y: box.y },
    se: { x: box.x + box.w, y: box.y + box.h },
    sw: { x: box.x, y: box.y + box.h },
  };
}

function hitAnnotationHandle(point) {
  if (store.selectedAnnotation < 0) return null;
  const box = store.annotations[store.selectedAnnotation];
  if (!box) return null;
  const rect = elements.annotateCanvas.getBoundingClientRect();
  const toleranceX = 12 / Math.max(1, rect.width);
  const toleranceY = 12 / Math.max(1, rect.height);
  for (const [name, handle] of Object.entries(annotationHandles(box))) {
    if (Math.abs(point.x - handle.x) <= toleranceX && Math.abs(point.y - handle.y) <= toleranceY) {
      return name;
    }
  }
  return null;
}

function updateAnnotationControls() {
  elements.annotationDelete.disabled = store.selectedAnnotation < 0;
  const canUseAi = (
    store.annotationItem
    && store.annotationItem.algorithm !== "door"
    && !store.annotationHadSavedBoxes
    && store.annotations.length === 0
    && !store.annotationAiBusy
  );
  elements.annotationAuto.disabled = !canUseAi;
  elements.annotationAuto.textContent = store.annotationAiBusy ? "AI 标注中" : "AI 预标注";
  elements.annotationClear.disabled = store.annotationAiBusy;
  elements.annotationSave.disabled = store.annotationAiBusy;
}

function drawAnnotations(preview = null) {
  const canvas = elements.annotateCanvas;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  const boxes = preview ? [...store.annotations, preview] : store.annotations;
  boxes.forEach((box, index) => {
    const selected = !preview && index === store.selectedAnnotation;
    const candidateColor = box.source === "embedded-blue" ? "#2457ff" : "#ff4d45";
    context.strokeStyle = selected ? "#ffe66d" : candidateColor;
    context.lineWidth = Math.max(3, canvas.width / 500);
    context.strokeRect(box.x * canvas.width, box.y * canvas.height, box.w * canvas.width, box.h * canvas.height);
    const label = box.label || "目标";
    context.font = `${Math.max(14, canvas.width / 55)}px Microsoft YaHei`;
    const labelWidth = context.measureText(label).width + 12;
    const labelHeight = Math.max(22, canvas.height / 28);
    const left = box.x * canvas.width;
    const top = Math.max(0, box.y * canvas.height - labelHeight);
    context.fillStyle = selected ? "#ffe66d" : candidateColor;
    context.fillRect(left, top, labelWidth, labelHeight);
    context.fillStyle = selected ? "#151715" : "#ffffff";
    context.fillText(label, left + 6, top + labelHeight - 6);
    if (selected) {
      const handleSize = Math.max(12, canvas.width / 90);
      context.fillStyle = "#ffe66d";
      context.strokeStyle = "#151715";
      context.lineWidth = Math.max(1, canvas.width / 900);
      for (const handle of Object.values(annotationHandles(box))) {
        const x = handle.x * canvas.width - handleSize / 2;
        const y = handle.y * canvas.height - handleSize / 2;
        context.fillRect(x, y, handleSize, handleSize);
        context.strokeRect(x, y, handleSize, handleSize);
      }
    }
  });
  updateAnnotationControls();
  elements.annotateHint.textContent = (
    `${store.annotations.length} 个框；拖拽空白处新增，拖动框可移动，拖动黄色角点可缩放`
  );
}

function openAnnotation(item) {
  store.annotationItem = item;
  const initialAnnotations = item.annotations?.length ? item.annotations : item.aiAnnotations;
  store.annotations = (initialAnnotations || []).map((box) => ({ ...box }));
  store.selectedAnnotation = -1;
  store.annotationHadSavedBoxes = store.annotations.length > 0;
  store.annotationAiBusy = false;
  elements.annotateTitle.textContent = `${shortGroup(item.group)} · 目标框标注`;
  elements.annotateMeta.textContent = item.filename;
  elements.annotateHint.textContent = "正在加载图片";
  updateAnnotationControls();
  elements.annotateImage.src = item.imageUrl;
  elements.annotateImage.onload = () => {
    elements.annotateCanvas.width = elements.annotateImage.naturalWidth;
    elements.annotateCanvas.height = elements.annotateImage.naturalHeight;
    drawAnnotations();
  };
  elements.annotateDialog.showModal();
}

function closeAnnotation() {
  elements.annotateDialog.close();
  store.annotationItem = null;
  store.pointerAction = null;
}

function bindAnnotation() {
  document.querySelector("#annotateClose").addEventListener("click", closeAnnotation);
  elements.annotateDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeAnnotation();
  });
  elements.annotateCanvas.addEventListener("pointerdown", (event) => {
    if (store.annotationAiBusy) return;
    const point = canvasPoint(event);
    const handle = hitAnnotationHandle(point);
    if (handle) {
      store.pointerAction = {
        mode: "resize",
        index: store.selectedAnnotation,
        handle,
        original: { ...store.annotations[store.selectedAnnotation] },
      };
      elements.annotateCanvas.setPointerCapture(event.pointerId);
      return;
    }
    const hit = hitAnnotation(point);
    store.selectedAnnotation = hit;
    store.pointerAction = hit >= 0
      ? { mode: "move", index: hit, start: point, original: { ...store.annotations[hit] } }
      : { mode: "draw", start: point };
    elements.annotateCanvas.setPointerCapture(event.pointerId);
    drawAnnotations();
  });
  elements.annotateCanvas.addEventListener("pointermove", (event) => {
    if (!store.pointerAction) return;
    const point = canvasPoint(event);
    const action = store.pointerAction;
    if (action.mode === "draw") {
      drawAnnotations({
        x: Math.min(action.start.x, point.x),
        y: Math.min(action.start.y, point.y),
        w: Math.abs(point.x - action.start.x),
        h: Math.abs(point.y - action.start.y),
        label: store.algorithm,
      });
      return;
    }
    if (action.mode === "resize") {
      const box = store.annotations[action.index];
      const original = action.original;
      const minSize = 0.01;
      const right = original.x + original.w;
      const bottom = original.y + original.h;
      if (action.handle.includes("w")) {
        box.x = Math.max(0, Math.min(point.x, right - minSize));
        box.w = right - box.x;
      } else {
        box.x = original.x;
        box.w = Math.max(minSize, Math.min(1, point.x) - original.x);
      }
      if (action.handle.includes("n")) {
        box.y = Math.max(0, Math.min(point.y, bottom - minSize));
        box.h = bottom - box.y;
      } else {
        box.y = original.y;
        box.h = Math.max(minSize, Math.min(1, point.y) - original.y);
      }
      drawAnnotations();
      return;
    }
    const box = store.annotations[action.index];
    box.x = Math.max(0, Math.min(1 - box.w, action.original.x + point.x - action.start.x));
    box.y = Math.max(0, Math.min(1 - box.h, action.original.y + point.y - action.start.y));
    drawAnnotations();
  });
  elements.annotateCanvas.addEventListener("pointerup", (event) => {
    if (!store.pointerAction) return;
    const point = canvasPoint(event);
    const action = store.pointerAction;
    if (action.mode === "draw") {
      const box = {
        x: Math.min(action.start.x, point.x),
        y: Math.min(action.start.y, point.y),
        w: Math.abs(point.x - action.start.x),
        h: Math.abs(point.y - action.start.y),
        label: store.algorithm,
      };
      if (box.w >= 0.01 && box.h >= 0.01) {
        store.annotations.push(box);
        store.selectedAnnotation = store.annotations.length - 1;
      }
    }
    store.pointerAction = null;
    drawAnnotations();
  });
  elements.annotationDelete.addEventListener("click", () => {
    if (store.selectedAnnotation < 0) return;
    store.annotations.splice(store.selectedAnnotation, 1);
    store.selectedAnnotation = -1;
    drawAnnotations();
  });
  elements.annotationClear.addEventListener("click", () => {
    store.annotations = [];
    store.selectedAnnotation = -1;
    drawAnnotations();
  });
  elements.annotationAuto.addEventListener("click", async () => {
    const item = store.annotationItem;
    if (
      !item || item.algorithm === "door" || store.annotationHadSavedBoxes
      || store.annotations.length || store.annotationAiBusy
    ) return;
    store.annotationAiBusy = true;
    updateAnnotationControls();
    elements.annotateHint.textContent = "正在调用 MiniMax 识别完整目标人形";
    try {
      const response = await fetch(`api/items/${encodeURIComponent(item.id)}/ai-annotations`, {
        method: "POST",
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      if (store.annotationItem !== item) return;
      store.annotations = (result.annotations || []).map((box) => ({
        x: box.x,
        y: box.y,
        w: box.w,
        h: box.h,
        label: box.label || item.algorithm,
      }));
      store.selectedAnnotation = store.annotations.length ? 0 : -1;
      drawAnnotations();
      elements.annotateHint.textContent = store.annotations.length
        ? `MiniMax 生成 ${store.annotations.length} 个候选框，请拖动或缩放确认，保存前不会入库`
        : "MiniMax 未发现完整目标人形，可手动画框";
    } catch (error) {
      if (store.annotationItem === item) {
        elements.annotateHint.textContent = `AI 预标注失败：${error.message}`;
      }
    } finally {
      store.annotationAiBusy = false;
      if (store.annotationItem === item) updateAnnotationControls();
    }
  });
  elements.annotationSave.addEventListener("click", async () => {
    const item = store.annotationItem;
    if (!item) return;
    if (isBoxReview() && store.annotations.length === 0) {
      elements.annotateHint.textContent = "复审至少保留一个目标框；如需排除图片，请回 AI 初审区修改判定";
      return;
    }
    elements.annotationSave.disabled = true;
    elements.annotationSave.textContent = "保存中";
    try {
      const requestPayload = {
        decision: "positive",
        annotations: store.annotations,
        expectedRevision: item.reviewRevision || 0,
      };
      const response = await fetch(`api/items/${encodeURIComponent(item.id)}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": reviewMutationKey(item, requestPayload),
        },
        body: JSON.stringify(requestPayload),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const saved = await response.json();
      Object.assign(item, saved);
      clearReviewMutation(item);
      elements.saveState.textContent = "目标框已保存";
      closeAnnotation();
      if (isBoxReview()) {
        await refreshItems();
      } else {
        render();
      }
    } catch (error) {
      elements.annotateHint.textContent = `保存失败：${error.message}`;
    } finally {
      elements.annotationSave.disabled = false;
      elements.annotationSave.textContent = "保存标注";
    }
  });
}

const reportStateLabels = {
  prepared: "预览已生成",
  canary_succeeded: "测试发送成功",
  sending: "正在批量发送",
  completed: "发送完成",
  failed: "已停止",
};

async function reportingRequest(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

function selectedReportingRun() {
  const runs = store.reportStatus?.recentRuns || [];
  const selected = store.reportStatus?.selectedRun;
  return (selected?.runId === store.reportRunId ? selected : null)
    || runs.find((run) => run.runId === store.reportRunId)
    || runs.find((run) => run.algorithm === store.reportAlgorithm)
    || null;
}

function reportDeviceText(summary) {
  const entries = Object.entries(summary?.by_device_algorithm || {});
  return entries.length
    ? entries.map(([key, count]) => `${key.split("|")[0]}：${count} 张`).join("；")
    : "—";
}

function renderReporting() {
  const status = store.reportStatus || { algorithms: [], recentRuns: [], activeJob: null };
  for (const algorithm of status.algorithms || []) {
    const prefix = algorithm.key === "takeaway" ? "reportTakeaway" : "reportWorkwear";
    const geid = document.querySelector(`#${prefix}Geid`);
    const gcid = document.querySelector(`#${prefix}Gcid`);
    if (geid) geid.textContent = algorithm.reportGeid;
    if (gcid) gcid.textContent = algorithm.reportGcid;
  }
  document.querySelectorAll("[data-report-algorithm]").forEach((button) => {
    button.classList.toggle("active", button.dataset.reportAlgorithm === store.reportAlgorithm);
  });
  const run = selectedReportingRun();
  if (run) store.reportRunId = run.runId;
  const summary = run?.summary || {};
  const active = status.activeJob;
  const automatic = status.automatic || { enabled: false };
  elements.reportJobBadge.textContent = active
    ? `${active.algorithm === "takeaway" ? "外卖服" : "新世界工服"}正在发送`
    : automatic.paused
      ? "自动发送已熔断暂停"
      : automatic.enabled
        ? "自动发送已启用"
        : "自动发送未启用";
  elements.reportJobBadge.classList.toggle("active", Boolean(active) || (automatic.enabled && !automatic.paused));
  elements.reportRunId.textContent = run?.runId || "尚未生成预览";
  elements.reportRunState.textContent = run ? (reportStateLabels[run.state] || run.state) : "待预览";
  elements.reportRunState.dataset.state = run?.state || "empty";
  elements.reportEligible.textContent = run?.manifestItems || 0;
  elements.reportBoxes.textContent = summary.ai_boxes || 0;
  elements.reportSuccess.textContent = run?.success || 0;
  elements.reportRemaining.textContent = run?.remaining || 0;
  elements.reportDeduplicated.textContent = summary.already_reported || 0;
  elements.reportWithheld.textContent = summary.missing_capture || 0;
  elements.reportDevices.textContent = reportDeviceText(summary);
  elements.reportResult.textContent = run
    ? `成功 ${run.success}；失败 ${run.failed}；不明 ${run.unknown}`
    : "尚未发送";
  const locked = store.reportBusy || Boolean(active);
  elements.reportPrepare.disabled = locked;
  elements.reportCanary.disabled = locked || !run || run.manifestItems <= 0 || run.canarySuccess;
  elements.reportConfirmationWrap.hidden = !run?.canarySuccess || run.remaining <= 0;
  elements.reportConfirmation.placeholder = run?.confirmationPhrase || "";
  elements.reportSend.disabled = locked || !run?.canarySuccess || run.remaining <= 0
    || elements.reportConfirmation.value !== run.confirmationPhrase;
  if (!store.reportBusy) {
    if (automatic.lastError) elements.reportMessage.textContent = `自动发送已暂停：${automatic.lastError}`;
    else if (automatic.enabled && automatic.enabledAt) {
      const result = automatic.lastResults?.[store.reportAlgorithm] || {};
      elements.reportMessage.textContent = `自动发送已启用；历史截止于 ${automatic.enabledAt}，最近一轮发送 ${result.sent || 0} 条、去重 ${result.deduplicated || 0} 条。`;
    }
    else if (run?.lastError) elements.reportMessage.textContent = `已停止：${run.lastError}`;
    else if (run?.state === "completed") elements.reportMessage.textContent = "本批次已全部发送完成，发送开关已关闭。";
    else if (run?.state === "sending") elements.reportMessage.textContent = `正在发送，当前成功 ${run.success} 条。`;
  }
  elements.reportHistory.replaceChildren();
  const runs = (status.recentRuns || []).filter((item) => item.algorithm === store.reportAlgorithm);
  if (!runs.length) {
    elements.reportHistory.append(Object.assign(document.createElement("span"), { textContent: "暂无批次" }));
  } else {
    for (const item of runs) {
      const button = document.createElement("button");
      button.type = "button";
      button.classList.toggle("active", item.runId === run?.runId);
      button.innerHTML = `<strong>${item.runId}</strong><span>${reportStateLabels[item.state] || item.state} · 成功 ${item.success}/${item.manifestItems}</span>`;
      button.addEventListener("click", () => {
        store.reportRunId = item.runId;
        elements.reportConfirmation.value = "";
        renderReporting();
      });
      elements.reportHistory.append(button);
    }
  }
}

function scheduleReportingPoll() {
  if (store.reportPollTimer) window.clearTimeout(store.reportPollTimer);
  const running = Boolean(store.reportStatus?.activeJob)
    || selectedReportingRun()?.state === "sending";
  if (!running || !isReporting()) {
    store.reportPollTimer = null;
    return;
  }
  store.reportPollTimer = window.setTimeout(() => {
    refreshReporting().catch((error) => {
      elements.reportMessage.textContent = `状态刷新失败：${error.message}`;
    });
  }, 2000);
}

async function refreshReporting() {
  const query = store.reportRunId ? `?runId=${encodeURIComponent(store.reportRunId)}` : "";
  store.reportStatus = await reportingRequest(`api/reporting/status${query}`);
  renderReporting();
  scheduleReportingPoll();
}

async function runReportingAction(action, payload, busyMessage) {
  if (store.reportBusy) return null;
  store.reportBusy = true;
  elements.reportMessage.textContent = busyMessage;
  renderReporting();
  try {
    const result = await reportingRequest(`api/reporting/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    store.reportRunId = result.runId;
    elements.reportConfirmation.value = "";
    await refreshReporting();
    return result;
  } catch (error) {
    elements.reportMessage.textContent = `操作失败：${error.message}`;
    throw error;
  } finally {
    store.reportBusy = false;
    renderReporting();
  }
}

function bindReporting() {
  for (const button of document.querySelectorAll("[data-report-algorithm]")) {
    button.addEventListener("click", () => {
      store.reportAlgorithm = button.dataset.reportAlgorithm;
      store.reportRunId = "";
      elements.reportConfirmation.value = "";
      renderReporting();
    });
  }
  elements.reportConfirmation.addEventListener("input", renderReporting);
  elements.reportPrepare.addEventListener("click", async () => {
    try {
      await runReportingAction(
        "prepare",
        { algorithm: store.reportAlgorithm },
        "正在生成只读预览，不会发送数据",
      );
      elements.reportMessage.textContent = "预览已生成，请核对数量后发送 1 条测试。";
    } catch {
      // The shared action handler has rendered the error.
    }
  });
  elements.reportCanary.addEventListener("click", async () => {
    const run = selectedReportingRun();
    if (!run || !window.confirm(`确认用新算法 ID 发送 1 条测试吗？\n\n批次：${run.runId}`)) return;
    try {
      await runReportingAction("canary", { runId: run.runId }, "正在发送 1 条测试");
      elements.reportMessage.textContent = "测试发送成功。输入确认短语后才可发送剩余全部。";
    } catch {
      // The shared action handler has rendered the error.
    }
  });
  elements.reportSend.addEventListener("click", async () => {
    const run = selectedReportingRun();
    if (!run || elements.reportConfirmation.value !== run.confirmationPhrase) return;
    try {
      await runReportingAction(
        "send",
        { runId: run.runId, confirmation: elements.reportConfirmation.value },
        "批量发送已启动，遇到首个异常会自动停止",
      );
      elements.reportMessage.textContent = "批量发送进行中，页面将自动刷新进度。";
    } catch {
      // The shared action handler has rendered the error.
    }
  });
}

async function start() {
  bindFilters();
  bindQueueMode();
  bindUpload();
  bindAnnotation();
  bindBulkReview();
  bindReporting();
  configureWorkspaceVisibility();
  try {
    const [response, healthResponse] = await Promise.all([
      fetch(queueEndpoint(), { cache: "no-store" }),
      fetch("healthz", { cache: "no-store" }),
    ]);
    if (!response.ok || !healthResponse.ok) throw new Error(`HTTP ${response.status}/${healthResponse.status}`);
    const [items, health] = await Promise.all([response.json(), healthResponse.json()]);
    store.items = applySavedOrder(items);
    persistOrder();
    applyHealth(health);
    configureModeControls();
    rebuildGroupFilter();
    elements.saveState.textContent = "已载入，修改自动保存";
    updateCounts();
    render();
    window.setInterval(() => {
      const operation = isReporting() ? refreshReporting() : refreshItems({ announce: true });
      operation.catch(() => { elements.saveState.textContent = "自动刷新失败，下轮重试"; });
    }, 60_000);
  } catch (error) {
    elements.saveState.textContent = "载入失败，请刷新";
    elements.emptyState.hidden = false;
    elements.emptyState.textContent = `无法载入样本：${error.message}`;
  }
}

start();
