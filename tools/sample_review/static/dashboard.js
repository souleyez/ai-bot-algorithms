const state = { data: null, expanded: new Set(), query: "", filter: "all" };
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
const number = (value) => new Intl.NumberFormat("zh-CN").format(Number(value || 0));
const shortTime = (epoch) => epoch ? new Date(epoch * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "暂无";
const algorithmLabel = (kind) => ({ workwear: "工服", takeaway: "外卖", door: "位移" }[kind] || kind || "抓拍");
const loginState = { csrf: "", action: "/_auth/request-code", email: "", busy: false };

function loginBusy(busy) {
  loginState.busy = busy;
  $("loginSubmit").disabled = busy;
  $("resendCode").disabled = busy;
  $("changeEmail").disabled = busy;
}

async function requestLogin(path, body) {
  if (loginState.busy) return;
  loginBusy(true);
  $("loginMessage").textContent = body ? "正在处理..." : "正在连接...";
  $("loginMessage").classList.remove("error");
  try {
    const response = await fetch(path, {
      method: body ? "POST" : "GET", body,
      cache: "no-store", credentials: "same-origin",
    });
    if (response.redirected && new URL(response.url).pathname === "/review") {
      window.location.assign("/review");
      return;
    }
    // Read the existing authentication form without inserting its HTML or scripts.
    const page = new DOMParser().parseFromString(await response.text(), "text/html");
    const form = page.querySelector('form[action="/_auth/verify-code"], form[action="/_auth/request-code"]');
    const csrf = form?.querySelector('input[name="csrf"]')?.value;
    if (!form || !csrf) throw new Error("登录服务暂不可用，请稍后重试。");
    loginState.csrf = csrf;
    loginState.action = form.getAttribute("action");
    const showCode = loginState.action === "/_auth/verify-code";
    const email = page.querySelector('input[name="email"]')?.value;
    if (email) loginState.email = email;
    $("loginEmail").value = loginState.email;
    $("loginEmailLabel").hidden = showCode;
    $("loginEmail").hidden = showCode;
    $("loginEmail").disabled = showCode;
    $("loginCodeLabel").hidden = !showCode;
    $("loginCode").hidden = !showCode;
    $("loginCode").disabled = !showCode;
    $("loginCode").required = showCode;
    $("loginCode").value = "";
    $("loginCodeActions").hidden = !showCode;
    $("loginSubmit").textContent = showCode ? "验证并进入复核" : "获取登录验证码";
    const error = page.querySelector(".error")?.textContent?.trim();
    $("loginMessage").textContent = error || (showCode ? `验证码已发送至 ${page.querySelector(".email")?.textContent || "受邀邮箱"}` : "仅限受邀用户使用");
    $("loginMessage").classList.toggle("error", Boolean(error));
    if ($("loginDialog").open) $(showCode ? "loginCode" : "loginEmail").focus();
  } catch (error) {
    loginState.csrf = "";
    $("loginMessage").textContent = "登录连接失败，请重试。";
    $("loginMessage").classList.add("error");
    $("loginSubmit").textContent = "重新连接";
  } finally {
    $("loginSubmit").formNoValidate = !loginState.csrf;
    loginBusy(false);
  }
}

function openLogin() {
  if (!$("loginDialog").open) $("loginDialog").showModal();
  requestLogin("/_auth/login?format=form");
}

function snapshotAge(data) {
  const value = Date.parse(data.generatedAt || "");
  return Number.isFinite(value) ? Math.max(0, Date.now() - value) : Infinity;
}

function aggregate(data) {
  return (data.devices || []).reduce((summary, device) => {
    const item = device.summary || {};
    const review = (device.reviewSamples || []).reduce((value, row) => ({ total: value.total + Number(row.total || 0), last24h: value.last24h + Number(row.last24h || 0) }), { total: 0, last24h: 0 });
    summary.reachable += device.reachable ? 1 : 0;
    summary.channels += Number(item.channels || 0);
    summary.reportedOnline += Number(item.reportedOnline || 0);
    summary.bindings += Number(item.algorithmBindings || 0);
    if (item.captureDataAvailable) {
      summary.captureSources += 1;
      summary.captures += Number(item.captures || 0);
      summary.captures24h += Number(item.captures24h || 0);
    } else if (review.total) {
      summary.syncFallbackSources += 1;
      summary.captures += review.total;
      summary.captures24h += review.last24h;
    }
    return summary;
  }, { reachable: 0, channels: 0, reportedOnline: 0, bindings: 0, captures: 0, captures24h: 0, captureSources: 0, syncFallbackSources: 0 });
}

function renderMetrics(data) {
  const totals = aggregate(data);
  $("gatewayMetric").textContent = number(data.catalogCount || (data.devices || []).length);
  $("gatewaySub").textContent = `${totals.reachable} 台当前可达`;
  $("reachableMetric").textContent = `${totals.reachable}/${data.catalogCount || 0}`;
  $("reachableSub").textContent = totals.reachable === data.catalogCount ? "全部可达" : `${(data.catalogCount || 0) - totals.reachable} 台需关注`;
  $("channelMetric").textContent = `${number(totals.reportedOnline)}/${number(totals.channels)}`;
  $("channelSub").textContent = "按设备上报状态统计";
  $("algorithmMetric").textContent = number(totals.bindings);
  $("algorithmSub").textContent = "当前通道绑定关系";
  $("captureMetric").textContent = number(totals.captures24h);
  $("captureSub").textContent = totals.captureSources ? `累计 ${number(totals.captures)} · ${totals.captureSources} 台盒端可读` : totals.syncFallbackSources ? `累计 ${number(totals.captures)} · 来自平台已同步` : "盒端计数暂不可读";
  const age = snapshotAge(data);
  $("freshnessMetric").textContent = data.generatedAt ? new Date(data.generatedAt).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" }) : "暂无快照";
  $("freshnessSub").textContent = age === Infinity ? "采集器尚未生成数据" : age > 15 * 60_000 ? `已延迟 ${Math.round(age / 60_000)} 分钟` : "快照新鲜";
  const authenticated = data.authenticated === true;
  $("identity").textContent = authenticated ? data.identity : "";
  $("identity").hidden = !authenticated;
  $("logoutLink").hidden = !authenticated;
  $("reviewEntry").textContent = authenticated ? "算法复核" : "登录 / 算法复核";
  $("reviewEntry").href = authenticated ? "/review" : "/_auth/login";
}

function statusMarkup(device) {
  if (!device.reachable) return '<span class="state"><span class="status-dot critical"></span>不可达</span>';
  if (Object.keys(device.errors || {}).length) return '<span class="state"><span class="status-dot warning"></span>部分数据</span>';
  return '<span class="state"><span class="status-dot healthy"></span>可达</span>';
}

function channelStatus(channel) {
  if (channel.reportedStatus === 1) return '<span class="state"><span class="status-dot healthy"></span>上报在线</span>';
  if (channel.reportedStatus === 0) return '<span class="state"><span class="status-dot warning"></span>上报离线</span>';
  return '<span class="state muted-value"><span class="status-dot"></span>未知</span>';
}

function channelDetails(device) {
  if (!device.reachable) return '<div class="empty-state">当前无法连接网关，保留目录记录，不把缺失数据记为零。</div>';
  if (!(device.channels || []).length) return '<div class="empty-state">网关可达，但没有读到通道清单。</div>';
  const rows = device.channels.map((channel) => {
    const algorithms = channel.algorithms || [];
    const captureAvailable = Boolean(device.summary?.captureDataAvailable);
    const total = algorithms.reduce((sum, item) => sum + Number(item.captures || 0), 0);
    const latest = Math.max(0, ...algorithms.map((item) => Number(item.lastCapture || 0)));
    const tags = algorithms.length ? algorithms.map((item) => `<span class="algorithm-tag" title="${escapeHtml(item.version || item.slot)}">${escapeHtml(item.name)} ${escapeHtml(item.slot)}</span>`).join("") : '<span class="muted-value">未绑定</span>';
    return `<tr><td>CH-${String(channel.channel).padStart(2, "0")}</td><td title="${escapeHtml(channel.location)}">${escapeHtml(channel.location || "未命名")}</td><td>${channelStatus(channel)}</td><td><div class="algorithm-list">${tags}</div></td><td class="number">${captureAvailable ? number(total) : "--"}</td><td class="number">${captureAvailable ? shortTime(latest) : "未读取"}</td></tr>`;
  }).join("");
  return `<table class="channel-table"><thead><tr><th>通道</th><th>位置</th><th>设备上报</th><th>采用算法</th><th>抓拍数</th><th>最后抓拍</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function filteredDevices() {
  const query = state.query.trim().toLowerCase();
  return (state.data?.devices || []).filter((device) => {
    if (state.filter === "online" && !device.reachable) return false;
    if (state.filter === "offline" && device.reachable && !Object.keys(device.errors || {}).length) return false;
    return !query || `${device.displayId} ${device.chipFamily} ${(device.tags || []).join(" ")}`.toLowerCase().includes(query);
  });
}

function renderGateways() {
  const devices = filteredDevices();
  if (!devices.length) {
    $("gatewayRows").innerHTML = '<tr><td colspan="9" class="loading-cell">没有符合条件的网关</td></tr>';
    return;
  }
  $("gatewayRows").innerHTML = devices.map((device) => {
    const open = state.expanded.has(device.displayId);
    const summary = device.summary || {};
    const review = (device.reviewSamples || []).reduce((value, row) => ({ total: value.total + Number(row.total || 0), last24h: value.last24h + Number(row.last24h || 0), latest: Math.max(value.latest, Number(row.latest || 0)) }), { total: 0, last24h: 0, latest: 0 });
    const channelValue = device.reachable ? `${number(summary.reportedOnline)}/${number(summary.channels)}` : "--";
    const captureReadable = device.reachable && summary.captureDataAvailable;
    const hasReviewFallback = !captureReadable && review.total > 0;
    const main = `<tr class="gateway-row ${open ? "open" : ""}" data-device="${escapeHtml(device.displayId)}" tabindex="0" aria-expanded="${open}"><td><span class="chevron">›</span></td><td><span class="gateway-name">${escapeHtml(device.displayId)}</span></td><td>${statusMarkup(device)}</td><td><span class="chip">${escapeHtml(device.chipFamily)}</span></td><td class="number">${channelValue}</td><td class="number">${device.reachable ? number(summary.algorithmBindings) : "--"}</td><td class="number" title="${hasReviewFallback ? "平台已同步抓拍" : "盒端抓拍库"}">${captureReadable ? number(summary.captures24h) : hasReviewFallback ? number(review.last24h) : "--"}</td><td class="number" title="${hasReviewFallback ? "平台已同步抓拍，不等于盒端完整累计" : "盒端抓拍累计"}">${captureReadable ? number(summary.captures) : hasReviewFallback ? `已同步 ${number(review.total)}` : "--"}</td><td class="number">${captureReadable ? shortTime(summary.lastCapture) : hasReviewFallback ? shortTime(review.latest) : "未读取"}</td></tr>`;
    const detail = open ? `<tr class="detail-row"><td colspan="9">${channelDetails(device)}</td></tr>` : "";
    return main + detail;
  }).join("");
  document.querySelectorAll(".gateway-row").forEach((row) => {
    const toggle = () => {
      const id = row.dataset.device;
      state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
      renderGateways();
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); } });
  });
}

function renderTrend(data) {
  const counts = new Map();
  for (const device of data.devices || []) for (const item of device.trend || []) counts.set(item.day, (counts.get(item.day) || 0) + Number(item.count || 0));
  const days = [...counts.entries()].sort(([a], [b]) => a.localeCompare(b)).slice(-7);
  if (!days.length) { $("trendChart").innerHTML = '<p class="empty-state">暂无可用趋势数据</p>'; return; }
  const maximum = Math.max(...days.map(([, value]) => value), 1);
  $("trendChart").innerHTML = days.map(([day, value]) => `<div class="trend-item"><div class="trend-track"><div class="trend-bar" style="height:${Math.max(2, Math.round(value / maximum * 100))}%" title="${number(value)} 次"></div></div><span class="trend-value">${number(value)}</span><span class="trend-label">${escapeHtml(day.slice(5))}</span></div>`).join("");
}

function renderAttention(data) {
  const issues = [];
  const age = snapshotAge(data);
  if (age > 15 * 60_000) issues.push({ level: "warning", title: "数据快照延迟", detail: age === Infinity ? "尚未生成网关快照" : `距上次采集约 ${Math.round(age / 60_000)} 分钟` });
  for (const device of data.devices || []) {
    if (!device.reachable) issues.push({ level: "critical", title: `${device.displayId} 网关不可达`, detail: "未将缺失的通道与抓拍数据记为零" });
    else if (Object.keys(device.errors || {}).length) issues.push({ level: "warning", title: `${device.displayId} 数据不完整`, detail: `未读到：${Object.keys(device.errors).join("、")}` });
  }
  $("attentionCount").textContent = number(issues.length);
  $("attentionList").innerHTML = issues.length ? issues.slice(0, 12).map((issue) => `<div class="attention-item"><span class="status-dot ${issue.level}"></span><div><strong>${escapeHtml(issue.title)}</strong><p>${escapeHtml(issue.detail)}</p></div></div>`).join("") : '<p class="empty-state">当前没有连接或数据读取异常</p>';
}

function renderRecent(data) {
  if (!data.authenticated) {
    $("recentCaptures").innerHTML = '<p class="empty-state capture-login"><a href="/_auth/login">登录查看抓拍与算法复核</a></p>';
    return;
  }
  const items = data.recentCaptures || [];
  $("recentCaptures").innerHTML = items.length ? items.map((item) => `<a class="capture-item" href="/review" title="进入样本复核"><img class="capture-image" src="${escapeHtml(item.imageUrl)}" alt="${escapeHtml(item.device)} 网关最新抓拍" loading="lazy"><span class="capture-meta"><strong>${escapeHtml(item.device)} · CH-${item.channel ?? "?"}</strong><span>${escapeHtml(algorithmLabel(item.algorithm))} · ${escapeHtml(shortTime(item.capturedAt))}</span></span></a>`).join("") : '<p class="empty-state">样本平台暂未同步可预览的抓拍</p>';
}

function render() {
  renderMetrics(state.data);
  renderGateways();
  renderTrend(state.data);
  renderAttention(state.data);
  renderRecent(state.data);
}

async function loadDashboard({ quiet = false } = {}) {
  const button = $("refreshButton");
  if (!quiet) button.classList.add("loading");
  try {
    let session = null;
    try {
      const response = await fetch("/api/dashboard/session", { cache: "no-store", credentials: "same-origin" });
      if (response.ok) session = await response.json();
    } catch { /* The public overview remains available if session verification fails. */ }
    if (session?.authenticated === true) {
      state.data = session;
      render();
      return;
    }
    const response = await fetch("/api/dashboard", { cache: "no-store", credentials: "omit" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    render();
  } catch (error) {
    $("gatewayRows").innerHTML = `<tr><td colspan="9" class="loading-cell">读取失败：${escapeHtml(error.message)}</td></tr>`;
  } finally {
    button.classList.remove("loading");
  }
}

$("searchInput").addEventListener("input", (event) => { state.query = event.target.value; renderGateways(); });
$("statusFilter").addEventListener("change", (event) => { state.filter = event.target.value; renderGateways(); });
$("refreshButton").addEventListener("click", () => loadDashboard());
$("closeLogin").addEventListener("click", () => $("loginDialog").close());
$("loginDialog").addEventListener("close", () => {
  const url = new URL(window.location.href);
  if (url.searchParams.has("login")) {
    url.searchParams.delete("login");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
});
document.addEventListener("click", (event) => {
  const link = event.target.closest('a[href="/_auth/login"]');
  if (link) { event.preventDefault(); openLogin(); }
});
$("loginForm").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!loginState.csrf) { requestLogin("/_auth/login?format=form"); return; }
  const body = new URLSearchParams({ csrf: loginState.csrf });
  if (loginState.action === "/_auth/verify-code") body.set("code", $("loginCode").value);
  else { loginState.email = $("loginEmail").value.trim(); body.set("email", loginState.email); }
  requestLogin(loginState.action, body);
});
$("resendCode").addEventListener("click", () => requestLogin("/_auth/request-code", new URLSearchParams({ csrf: loginState.csrf, email: loginState.email })));
$("changeEmail").addEventListener("click", () => requestLogin("/_auth/login?format=form"));
loadDashboard();
if (new URLSearchParams(window.location.search).get("login") === "1") openLogin();
setInterval(() => loadDashboard({ quiet: true }), 60_000);
