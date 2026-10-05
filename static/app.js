"use strict";

const $ = (id) => document.getElementById(id);
const state = { config: null, user: null, upload: null, jobId: null, pollTimer: null };
const STAGES = ["queued", "extracting", "translating", "rebuilding", "done"];

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...options });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const msg = (data && (data.detail || data.message)) || `Request failed (${res.status})`;
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = res.status;
    throw err;
  }
  return data;
}

function show(el, visible) { el.classList.toggle("hidden", !visible); }
function setError(el, msg) { el.textContent = msg || ""; show(el, !!msg); }
function notice(msg) { $("notice").textContent = msg; show($("notice"), !!msg); }

// ------------------------------------------------------------------ account
async function loadMe() {
  const data = await api("/api/me");
  state.user = data.user;
  const u = state.user;
  show($("login-btn"), !u);
  show($("logout-btn"), !!u);
  show($("user-email"), !!u);
  show($("balance"), !!u);
  show($("buy-btn"), !!u && state.config && state.config.payments_enabled);
  if (u) {
    $("user-email").textContent = u.email;
    $("balance").textContent = `${u.balance} credits` + (u.reserved ? ` (${u.reserved} reserved)` : "");
  }
  updateTranslateButton();
}

function setupLogin() {
  const dlg = $("login-dialog");
  $("login-btn").onclick = () => { $("login-msg").textContent = ""; dlg.showModal(); };
  $("login-cancel").onclick = () => dlg.close();
  $("login-form").onsubmit = async (e) => {
    e.preventDefault();
    $("login-msg").textContent = "Sending…";
    try {
      const r = await api("/api/auth/request", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: $("login-email").value }),
      });
      $("login-msg").textContent = r.message;
    } catch (err) { $("login-msg").textContent = err.message; }
  };
  $("logout-btn").onclick = async () => { await api("/api/auth/logout", { method: "POST" }); await loadMe(); };
}

// ------------------------------------------------------------------ payments
function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) return resolve();
    const s = document.createElement("script");
    s.src = src; s.onload = resolve; s.onerror = () => reject(new Error("Could not load the payment form."));
    document.head.appendChild(s);
  });
}

async function buyCredits() {
  try {
    const order = await api("/api/checkout", { method: "POST" });
    await loadScript("https://checkout.razorpay.com/v1/checkout.js");
    const before = state.user ? state.user.balance : 0;
    const rzp = new window.Razorpay({
      key: order.key_id, amount: order.amount, currency: order.currency, order_id: order.order_id,
      name: order.name, description: order.description, prefill: { email: order.email },
      // Credits are only added by the server-side webhook; here we just wait for it.
      handler: () => waitForCredits(before),
    });
    rzp.open();
  } catch (err) { notice(err.message); }
}

async function waitForCredits(before) {
  notice("Payment received. Your credits will appear as soon as Razorpay confirms it…");
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    await loadMe();
    if (state.user && state.user.balance > before) { notice("Credits added. Thank you!"); return; }
  }
  notice("Payment is still being confirmed. Refresh the page in a minute to see your credits.");
}

// ------------------------------------------------------------------ upload
function setupDropZone() {
  const drop = $("drop");
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.add("over");
  }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.remove("over");
  }));
  drop.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]); });
  $("file-input").addEventListener("change", (e) => { if (e.target.files[0]) uploadFile(e.target.files[0]); });
}

async function uploadFile(file) {
  setError($("error"), "");
  state.upload = null;
  show($("file-info"), false);
  updateTranslateButton();
  const maxMb = state.config.max_upload_mb;
  if (!file.name.toLowerCase().endsWith(".pdf")) return setError($("error"), "Only PDF files are accepted.");
  if (file.size > maxMb * 1024 * 1024) return setError($("error"), `The file is larger than ${maxMb} MB.`);
  $("drop-text").textContent = `Uploading ${file.name}…`;
  const form = new FormData();
  form.append("file", file);
  try {
    state.upload = await api("/api/upload", { method: "POST", body: form });
    const u = state.upload;
    $("file-name").textContent = u.filename;
    $("file-pages").textContent = u.pages;
    $("estimate").textContent = u.credit_estimate;
    $("cpp").textContent = u.credits_per_page;
    $("sample-pages").textContent = u.sample_pages;
    show($("file-info"), true);
    $("drop-text").innerHTML = "<strong>Drop another PDF</strong> or click to replace";
  } catch (err) {
    setError($("error"), err.message);
    $("drop-text").innerHTML = "<strong>Drop a PDF here</strong> or click to choose a file";
  }
  updateTranslateButton();
}

function selectedMode() { return document.querySelector("input[name=mode]:checked").value; }

function updateTranslateButton() {
  const btn = $("translate-btn");
  btn.disabled = !state.upload;
  if (!state.upload) {
    btn.textContent = "Translate";
  } else if (selectedMode() === "full") {
    btn.textContent = `Translate (${state.upload.credit_estimate} credits)`;
  } else {
    btn.textContent = "Translate free sample";
  }
}

// ------------------------------------------------------------------ jobs
async function startJob() {
  setError($("error"), "");
  const mode = selectedMode();
  if (mode === "full" && !state.user) {
    setError($("error"), "Please sign in to translate the full document. The free sample needs no account.");
    return;
  }
  $("translate-btn").disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_id: state.upload.file_id, target: $("target").value, mode }),
    });
    state.jobId = job.id;
    if (mode === "full") state.upload = null;   // the server deletes the upload after a full run
    show($("progress-card"), true);
    show($("result"), false);
    setError($("job-error"), "");
    $("preview").innerHTML = "";
    renderStage(job);
    pollJob();
    loadMe();
  } catch (err) {
    setError($("error"), err.message);
  }
  updateTranslateButton();
}

function renderStage(job) {
  const idx = STAGES.indexOf(job.stage);
  document.querySelectorAll("#steps li").forEach((li) => {
    const i = STAGES.indexOf(li.dataset.stage);
    li.className = "";
    if (job.status === "failed") {
      if (li.dataset.stage === "done") li.classList.add("failed");
    } else if (i < idx || job.status === "done") li.classList.add("complete");
    else if (i === idx) li.classList.add("active");
  });
}

function pollJob() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    let job;
    try { job = await api(`/api/jobs/${state.jobId}`); } catch (err) { return pollJob(); }
    renderStage(job);
    if (job.status === "done") return showResult(job);
    if (job.status === "failed") {
      setError($("job-error"), job.error || "Translation failed.");
      loadMe();
      return;
    }
    pollJob();
  }, 1000);
}

async function showResult(job) {
  loadMe();
  const link = await api(`/api/jobs/${job.id}/download`);
  $("download-btn").href = link.url;
  const ow = $("overflow");
  if (job.overflow_count > 0) {
    ow.textContent = `${job.overflow_count} text block(s) did not fit in their original space and were ` +
      "printed smaller than usual. Please check those spots.";
  }
  show(ow, job.overflow_count > 0);
  const preview = $("preview");
  preview.innerHTML = "";
  for (let p = 1; p <= link.pages; p++) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = `Page ${p}`;
    img.src = `${link.preview_url}${p}.png`;
    preview.appendChild(img);
  }
  show($("result"), true);
  if (job.mode === "sample") {
    notice("This is the free sample. Choose “Full document” to translate every page.");
  }
}

// ------------------------------------------------------------------ init
async function init() {
  state.config = await api("/api/config");
  const c = state.config;
  $("lim-mb").textContent = c.max_upload_mb;
  $("lim-pages").textContent = c.max_pages;
  $("expiry-note").textContent = `Files are deleted after ${c.output_retention_days} days.`;
  $("sample-pages").textContent = c.free_sample_pages;
  $("target").innerHTML = c.languages.map((l) => `<option value="${l.code}">${l.name}</option>`).join("");
  $("buy-btn").textContent = `Buy ${c.plan.credits} credits · ₹${c.plan.price_inr}`;
  $("buy-btn").onclick = buyCredits;
  $("translate-btn").onclick = startJob;
  document.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", updateTranslateButton));
  setupLogin();
  setupDropZone();
  const params = new URLSearchParams(location.search);
  if (params.get("login") === "ok") notice("You are signed in.");
  if (params.get("login") === "invalid") notice("That sign-in link is invalid or expired. Please request a new one.");
  if (params.has("login")) history.replaceState(null, "", "/");
  await loadMe();
}

init().catch((err) => notice(err.message));
