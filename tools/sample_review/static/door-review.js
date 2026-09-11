"use strict";
const $ = (id) => document.getElementById(id);
const cards = new Map();
let loading = false;
let cursor = "";
let generation = 0;
const timeText = (value) => value ? new Date(value).toLocaleString("zh-CN", {hour12:false}) : "待确认";

async function request(url, options = {}) {
  const response = await fetch(url, {...options, cache:"no-store"});
  const type = response.headers.get("content-type") || "";
  if (!type.includes("application/json")) throw new Error("登录已失效，请重新登录后重试");
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "请求失败，请稍后重试");
  return body;
}

function addCard(item) {
  if (cards.has(item.id)) return;
  const node = $("doorCard").content.firstElementChild.cloneNode(true);
  const form = node.querySelector("form");
  const output = node.querySelector("output");
  const submit = form.querySelector("button");
  const img = node.querySelector("img");
  const controls = form.elements;
  img.src = item.imageUrl;
  img.alt = `62821 通道 ${item.channel} ${item.filename}`;
  img.onerror = () => { output.textContent = "图片读取失败，不能据此判断门状态"; submit.disabled = true; };
  node.querySelector(".channel-label").textContent = `通道 ${item.channel}`;
  node.querySelector("time").textContent = timeText(item.capturedAt);
  node.querySelector(".filename").textContent = item.filename;
  controls.state.value = item.state;
  controls.verdict.value = item.verdict || "uncertain";
  controls.notes.value = item.notes;
  if (item.revision) output.textContent = `已复核 · 第 ${item.revision} 版`;
  node.querySelector(".open-image").onclick = () => {
    $("previewImage").src = item.imageUrl;
    $("previewCaption").textContent = `通道 ${item.channel} · ${timeText(item.capturedAt)} · ${item.filename}`;
    $("preview").showModal();
  };
  let retry = null;
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const payload = {state:controls.state.value, verdict:controls.verdict.value,
      notes:controls.notes.value, expectedRevision:item.revision};
    const body = JSON.stringify(payload);
    if (!retry || retry.body !== body) retry = {body, key:crypto.randomUUID()};
    submit.disabled = true;
    output.className = "";
    output.textContent = "正在保存";
    try {
      const result = await request(`/api/door-review/${encodeURIComponent(item.id)}`, {
        method:"PATCH", headers:{"Content-Type":"application/json", "Idempotency-Key":retry.key}, body});
      item.revision = result.revision;
      retry = null;
      output.textContent = `已保存 · 第 ${result.revision} 版`;
      await refresh(false, true);
    } catch (error) {
      output.className = "save-error";
      output.textContent = error.message;
    } finally { submit.disabled = !img.complete || !img.naturalWidth; }
  };
  cards.set(item.id, node);
  $("doorGallery").append(node);
}

async function refresh(more = false, countsOnly = false) {
  if (loading) return;
  loading = true;
  const current = generation;
  const params = new URLSearchParams({channel:$("channel").value, status:$("filter").value,
    after:more ? cursor : ""});
  try {
    const data = await request(`/api/door-review?${params}`);
    if (current !== generation) return;
    $("error").hidden = true;
    for (const key of ["total", "pending", "reviewed"]) $(key).textContent = data.counts[key];
    $("states").textContent = `${data.counts.open} / ${data.counts.closed}`;
    $("latest").textContent = `最新抓拍：${timeText(data.latestCaptureAt)}`;
    const existingChannels = new Set([...$("channel").options].map((o) => o.value));
    for (const channel of data.channels) {
      if (!existingChannels.has(String(channel))) $("channel").add(new Option(`通道 ${channel}`, channel));
    }
    if (!countsOnly) {
      data.items.forEach(addCard);
      if (more || !cursor) {
        cursor = data.nextCursor;
        $("more").hidden = !data.hasMore;
      }
    }
    $("empty").hidden = cards.size > 0;
    $("empty").textContent = data.counts.total ? "当前筛选下没有抓拍" : "尚未接入 62821 m101 抓拍；采集链路待验证。";
    $("status").textContent = `已刷新 ${new Date().toLocaleTimeString("zh-CN", {hour12:false})}`;
  } catch (error) {
    $("error").hidden = false;
    $("error").textContent = error.message;
    $("status").textContent = "读取失败";
  } finally {
    loading = false;
    if (current !== generation) refresh();
  }
}

for (const id of ["channel", "filter"]) $(id).onchange = () => {
  generation += 1;
  cards.clear(); $("doorGallery").replaceChildren(); cursor = ""; refresh();
};
$("refresh").onclick = () => refresh();
$("more").onclick = () => refresh(true);
$("closePreview").onclick = () => $("preview").close();
refresh();
setInterval(() => { if (!document.hidden && !$("preview").open) refresh(); }, 60000);
