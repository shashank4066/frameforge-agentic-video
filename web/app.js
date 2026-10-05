(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const STAGES = [
    ["concept", "Concept", "✦"], ["script", "Script", "≡"],
    ["scenes", "Scenes", "▦"], ["visuals", "Visuals", "▧"],
    ["voice", "Voice", "♫"], ["subtitles", "Captions", "CC"],
    ["validate", "Quality", "✓"], ["compose", "Compose", "▶"]
  ];
  const TERMINAL = new Set(["completed", "failed", "cancelled"]);
  const STATUS_LABELS = { queued: "Queued", running: "In production", awaiting_review: "Needs your review", completed: "Completed", failed: "Failed", cancelled: "Cancelled" };
  const state = {
    jobs: [], selectedId: null, job: null, events: [], artifacts: [], tab: "overview",
    connected: false, config: null, busy: false, refreshing: false,
    editorKey: null, editorDirty: false, autoReviewId: null, timer: null, toastTimer: null,
    healthCheckedAt: 0, selectedVersion: null, editorSignature: null, artifactSignature: null, eventSignature: null
  };

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = String(content);
    return node;
  }
  function display(id, show) { $(id).hidden = !show; }
  function text(id, value) { $(id).textContent = value == null ? "" : String(value); }
  function description(value) {
    if (!value) return "An unexpected error occurred. Please try again.";
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(item => item.msg || String(item)).join("; ");
    return value.message || JSON.stringify(value);
  }
  function toast(message, isError = false) {
    const node = $("toast");
    node.textContent = message;
    node.className = `toast${isError ? " error" : ""}`;
    node.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { node.hidden = true; }, isError ? 6500 : 3500);
  }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...options.headers }, signal: controller.signal });
      let payload;
      try { payload = await response.json(); } catch { payload = {}; }
      if (!response.ok) {
        const error = new Error(description(payload.detail || payload.error || `Request failed (${response.status}).`));
        error.status = response.status;
        throw error;
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The server took too long to respond. Try again shortly.");
      if (error instanceof TypeError) throw new Error("The local server is unavailable. Start the server and this studio will reconnect.");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function setConnection(connected) {
    state.connected = connected;
    $("connection-dot").className = `status-dot ${connected ? "connected" : "offline"}`;
    text("connection-label", connected ? "Studio online" : "Server unavailable");
    const rendererMissing = connected && state.config?.ffmpeg_available === false;
    display("connection-notice", !connected || rendererMissing);
    text("connection-notice", rendererMissing ? "The video renderer is unavailable. Install FFmpeg, then restart the server to enable productions." : "The local server is unavailable. Keep your brief here; the studio will reconnect automatically when the server starts.");
  }
  function formatDate(value, options = {}) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Just now" : new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", ...options }).format(date);
  }
  function safeUrl(value) {
    if (typeof value !== "string" || !value) return null;
    try {
      const url = new URL(value, location.origin);
      return ["http:", "https:"].includes(url.protocol) && url.origin === location.origin ? url.href : null;
    } catch { return null; }
  }
  function artifactType(artifact) {
    const kind = String(artifact.kind || "").toLowerCase();
    const name = String(artifact.name || artifact.url || "").toLowerCase().split("?")[0];
    if (kind.includes("image") || kind === "visual" || /\.(png|jpe?g|webp|gif)$/.test(name)) return "image";
    if (kind.includes("audio") || kind === "voice" || /\.(wav|mp3|m4a|ogg)$/.test(name)) return "audio";
    if (kind.includes("video") || /\.(mp4|webm|mov)$/.test(name)) return "video";
    return "file";
  }
  function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value <= 0) return "Download file";
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }
  function progressOf(job) {
    if (job.status === "completed") return 100;
    const value = Number(job.progress || 0);
    return Math.max(0, Math.min(100, value));
  }
  function isPlanReview(job) {
    return job.status === "awaiting_review" && job.review_checkpoint !== "media";
  }

  function renderJobs() {
    text("job-count", state.jobs.length);
    const container = $("production-list");
    container.replaceChildren();
    if (!state.jobs.length) {
      container.append(element("div", "empty-productions", "Your first production will appear here. Bring an idea to life above."));
      return;
    }
    for (const job of state.jobs) {
      const card = element("button", `production-card${job.id === state.selectedId ? " selected" : ""}`);
      card.type = "button";
      card.setAttribute("aria-label", `Open ${job.title || "Untitled production"}: ${STATUS_LABELS[job.status] || job.status}`);
      const top = element("div", "production-card-top");
      top.append(element("span", "production-card-icon", job.status === "completed" ? "▶" : "✦"), element("span", `status-pill ${job.status}`, STATUS_LABELS[job.status] || job.status));
      card.append(top, element("h3", "", job.title || "Untitled production"));
      card.append(element("p", "", `${job.duration_seconds || "—"} sec · ${job.aspect_ratio || "16:9"} · ${job.style || "cinematic"}`));
      const footer = element("div", "production-card-footer");
      footer.append(element("span", "", formatDate(job.created_at)), element("span", "", job.provider_mode === "live" ? "AI PRODUCTION ↗" : "DEMO RENDER ↗"));
      card.append(footer);
      card.addEventListener("click", () => selectJob(job.id, true));
      container.append(card);
    }
  }

  function renderWorkflow(job) {
    const current = STAGES.findIndex(stage => stage[0] === job.current_stage);
    const list = $("workflow");
    list.replaceChildren();
    STAGES.forEach(([key, label, icon], index) => {
      const done = job.status === "completed" || (current >= 0 && index < current);
      const active = !TERMINAL.has(job.status) && current === index;
      const item = element("li", done ? "done" : active ? "current" : "");
      item.append(element("span", "stage-indicator", done ? "✓" : icon), element("span", "", label));
      item.title = `${label}: ${done ? "complete" : active ? "current stage" : "pending"}`;
      if (active) item.setAttribute("aria-current", "step");
      list.append(item);
    });
  }

  function renderScenes(job, editable) {
    const key = `${job.id}:${editable ? "review" : "readonly"}`;
    const changed = state.editorKey !== key;
    if (state.editorDirty && !changed) return;
    const signature = JSON.stringify([key, job.script, job.scenes]);
    if (!changed && signature === state.editorSignature) return;
    state.editorSignature = signature;
    state.editorKey = key;
    if (changed) { state.editorDirty = false; $("review-notes").value = ""; }
    $("script-editor").value = job.script || "";
    $("script-editor").readOnly = true;
    const container = $("scene-editors");
    container.replaceChildren();
    const scenes = Array.isArray(job.scenes) ? job.scenes : [];
    scenes.forEach((scene, index) => {
      const panel = element("div", "scene-editor");
      panel.dataset.index = String(index);
      const header = element("div", "scene-editor-header");
      header.append(element("h5", "", `Scene ${String(index + 1).padStart(2, "0")}`));
      const durationLabel = element("label", "scene-duration");
      durationLabel.append(element("span", "", "Duration"));
      const duration = element("input", "scene-duration-input");
      duration.type = "number"; duration.min = "1"; duration.max = "60"; duration.step = "0.1";
      duration.value = String(scene.duration_seconds ?? 4); duration.readOnly = !editable;
      duration.setAttribute("aria-label", `Scene ${index + 1} duration in seconds`);
      duration.addEventListener("input", () => { state.editorDirty = true; });
      durationLabel.append(duration, element("span", "", "sec"));
      header.append(durationLabel);
      const fields = element("div", "scene-fields");
      for (const [field, label] of [["narration", "Narration"], ["visual_prompt", "Visual prompt"]]) {
        const wrapper = element("div");
        const input = element("textarea");
        input.className = `scene-${field}`;
        input.id = `scene-${index}-${field}`;
        input.rows = 3; input.maxLength = field === "narration" ? 1600 : 2000; input.readOnly = !editable;
        input.value = scene[field] || "";
        input.addEventListener("input", () => {
          state.editorDirty = true;
          if (field === "narration") syncScriptFromNarrations();
        });
        const fieldLabel = element("label", "", label); fieldLabel.htmlFor = input.id;
        wrapper.append(fieldLabel, input); fields.append(wrapper);
      }
      panel.append(header, fields); container.append(panel);
    });
  }

  function syncScriptFromNarrations() {
    $("script-editor").value = Array.from($("scene-editors").querySelectorAll(".scene-narration"))
      .map(input => input.value.trim()).filter(Boolean).join(" ");
  }

  function renderArtifacts() {
    const usable = state.artifacts.filter(artifact => safeUrl(artifact.url));
    const signature = JSON.stringify([state.job?.id, state.job?.status === "completed", usable]);
    if (state.artifactSignature === signature) return;
    state.artifactSignature = signature;
    const videos = usable.filter(artifact => artifactType(artifact) === "video");
    const video = $("final-video");
    const final = videos.find(artifact => String(artifact.name || "").split("/").pop() === "final.mp4"
      || (state.job?.output_url && safeUrl(artifact.url) === safeUrl(state.job.output_url)));
    display("final-video", !!final);
    display("video-placeholder", !final);
    if (final) {
      const url = safeUrl(final.url);
      if (video.dataset.artifactUrl !== url) {
        video.src = url; video.dataset.artifactUrl = url; video.load();
      }
    } else if (video.dataset.artifactUrl) {
      video.pause(); video.removeAttribute("src"); delete video.dataset.artifactUrl; video.load();
    }
    display("artifact-section", usable.length > 0);
    text("artifact-total", `${usable.length} FILE${usable.length === 1 ? "" : "S"}`);
    const list = $("artifact-list"); list.replaceChildren();
    for (const artifact of usable) {
      const link = element("a", "artifact-link");
      link.href = safeUrl(artifact.url); link.download = artifact.name || "artifact";
      const extension = String(artifact.name || "FILE").split(".").pop().slice(0, 4).toUpperCase();
      link.append(element("span", "artifact-icon", extension));
      const info = element("div");
      info.append(element("strong", "", artifact.name || "Production artifact"), element("small", "", formatBytes(artifact.size_bytes)));
      link.append(info, element("span", "", "↓")); list.append(link);
    }
    const media = usable.filter(artifact => ["image", "audio"].includes(artifactType(artifact))
      || (artifactType(artifact) === "video" && artifact !== final));
    const showMedia = media.length > 0 && state.job?.status !== "completed";
    display("media-preview", showMedia);
    const grid = $("media-grid"); grid.replaceChildren();
    if (showMedia) for (const artifact of media) {
      const type = artifactType(artifact);
      const card = element("div", `media-card${type === "audio" ? " audio-card" : ""}`);
      if (type === "image") {
        const img = element("img"); img.src = safeUrl(artifact.url); img.alt = artifact.name || "Generated scene"; img.loading = "lazy";
        card.append(img, element("div", "", artifact.name));
      } else if (type === "video") {
        const preview = element("video"); preview.controls = true; preview.preload = "metadata";
        preview.playsInline = true; preview.src = safeUrl(artifact.url);
        preview.setAttribute("aria-label", artifact.name || "Generated video scene");
        card.append(preview, element("div", "", artifact.name || "Generated video scene"));
      } else {
        const audio = element("audio"); audio.controls = true; audio.preload = "none"; audio.src = safeUrl(artifact.url);
        card.append(element("div", "", artifact.name || "Narration"), audio);
      }
      grid.append(card);
    }
  }

  function renderEvents() {
    text("event-count", state.events.length || "");
    const signature = JSON.stringify([state.job?.id, state.events]);
    if (state.eventSignature === signature) return;
    state.eventSignature = signature;
    const list = $("event-list"); list.replaceChildren();
    if (!state.events.length) { list.append(element("li", "empty-productions", "Agent updates will appear as the production moves forward.")); return; }
    for (const event of state.events.slice().reverse()) {
      const item = element("li", `event ${event.level === "error" ? "error" : event.level === "warning" ? "warning" : ""}`);
      const header = element("div", "event-header");
      header.append(element("span", "event-stage", event.stage || "Production"), element("time", "event-time", formatDate(event.created_at, { hour: "2-digit", minute: "2-digit", second: "2-digit" })));
      item.append(header, element("p", "", event.message || "")); list.append(item);
    }
  }

  function renderJob() {
    const job = state.job;
    display("empty-workspace", !job); display("job-workspace", !!job);
    if (!job) return;
    const reviewing = job.status === "awaiting_review";
    const planReview = isPlanReview(job);
    const mediaReview = reviewing && !planReview;
    text("workspace-label", reviewing ? "YOUR DIRECTION MATTERS" : job.status === "completed" ? "READY FOR THE WORLD" : "AGENTS AT WORK");
    text("job-title", job.title || "Untitled production");
    text("job-mode", job.provider_mode === "live" ? "AI PRODUCTION" : "DEMO PRODUCTION · LOCAL MEDIA");
    text("job-meta", `${job.duration_seconds} seconds · ${job.aspect_ratio} · ${job.style}${job.cost_usd != null ? ` · $${Number(job.cost_usd).toFixed(3)} reported cost` : ""}`);
    text("job-status", STATUS_LABELS[job.status] || job.status);
    $("job-status").className = `status-pill ${job.status}`;
    text("job-id", `PRODUCTION ${String(job.id).slice(0, 8).toUpperCase()}`);
    const progress = Math.round(progressOf(job));
    text("progress-percent", `${progress}%`); $("progress-bar").style.width = `${progress}%`;
    const stageName = STAGES.find(stage => stage[0] === job.current_stage)?.[1] || "Production";
    const progressMessage = reviewing ? (planReview ? "Storyboard ready for your review" : "Media ready for your review") : job.status === "completed" ? "Your final cut is ready" : job.status === "failed" ? "Production needs attention" : job.status === "cancelled" ? "Production cancelled" : job.status === "queued" ? "Your production is in the queue" : `${stageName} agent is working`;
    text("progress-message", progressMessage);
    renderWorkflow(job);
    display("job-error", !!job.error); if (job.error) text("job-error", description(job.error));
    display("cancel-job", !TERMINAL.has(job.status));
    display("retry-job", job.status === "failed" || job.status === "cancelled");
    display("approve-media", mediaReview); display("plan-approval", planReview);
    display("review-callout", reviewing);
    text("review-callout-title", planReview ? "Your creative direction is ready" : "The media is ready for a look");
    text("review-callout-message", planReview ? "Review the script and scene prompts before generation begins." : "Preview the scene images and narration, then approve the final assembly.");
    text("open-review", planReview ? "Review plan →" : "Review media ↓");
    text("scene-count", job.scenes?.length || "");
    text("storyboard-mode", planReview ? "EDITABLE REVIEW" : job.scenes?.length ? "PRODUCTION PLAN" : "WAITING FOR AGENT");
    text("storyboard-instructions", planReview ? "Edit narration, prompts and timing in the scenes below. Your approval starts generation." : job.scenes?.length ? "Your approved script, visual direction and scene timing." : "The script and scene plan will appear here.");
    renderScenes(job, planReview);
    text("preview-title", planReview ? "The story is yours to shape" : mediaReview ? "One review away from the final cut" : job.status === "failed" ? "Let's get this production moving again" : job.status === "cancelled" ? "This production was cancelled" : job.status === "completed" ? "Production complete" : "Your final cut is on its way");
    text("preview-caption", planReview ? "Open the storyboard to review your script and scene plan." : mediaReview ? "Preview the generated media below, then approve assembly." : job.status === "failed" ? "See the error above and retry when the issue is resolved." : job.status === "cancelled" ? "You can retry it whenever you're ready." : job.status === "completed" ? "Download your production files below." : "Follow the agents' progress or explore the storyboard as it takes shape.");
    text("preview-mode-note", job.provider_mode === "live" ? "LIVE · CONFIGURED AI PROVIDERS" : "DEMO · LOCALLY COMPOSED MEDIA");
    renderArtifacts(); renderEvents();
    if (planReview && state.autoReviewId !== job.id) { state.autoReviewId = job.id; selectTab("storyboard"); }
    else if (mediaReview && state.autoReviewId !== `${job.id}:media`) { state.autoReviewId = `${job.id}:media`; selectTab("overview"); }
    setBusy(state.busy);
  }

  function selectTab(name) {
    state.tab = name;
    for (const button of document.querySelectorAll("[data-tab]")) {
      const active = button.dataset.tab === name;
      button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      display(`panel-${button.dataset.tab}`, active);
    }
  }

  async function selectJob(id, scroll = false) {
    if (id !== state.selectedId) {
      state.selectedId = id; state.job = state.jobs.find(job => job.id === id) || null;
      state.editorDirty = false; state.editorKey = null; state.selectedVersion = null;
      state.artifacts = []; state.events = []; state.autoReviewId = null;
      selectTab("overview"); renderJobs(); renderJob();
    }
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
      if (state.selectedId !== id) return;
      state.job = job;
      await refreshDetails(id);
      renderJob();
      if (scroll) $("workspace-title").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) { toast(error.message, true); }
  }

  async function refreshDetails(id) {
    const results = await Promise.allSettled([
      api(`/api/jobs/${encodeURIComponent(id)}/events`),
      api(`/api/jobs/${encodeURIComponent(id)}/artifacts`)
    ]);
    if (state.selectedId !== id) return;
    if (results[0].status === "fulfilled") state.events = Array.isArray(results[0].value.events) ? results[0].value.events : [];
    if (results[1].status === "fulfilled") state.artifacts = Array.isArray(results[1].value.artifacts) ? results[1].value.artifacts : [];
  }

  async function refreshConfig() {
    const results = await Promise.allSettled([api("/api/health"), api("/api/config")]);
    state.healthCheckedAt = Date.now();
    if (results[0].status === "fulfilled" || results[1].status === "fulfilled") setConnection(true);
    if (results[1].status === "fulfilled") state.config = results[1].value;
    else if (results[0].status === "fulfilled") state.config = results[0].value;
    if (!state.config) return;
    $("live-option").disabled = !state.config.live_ready;
    $("live-option").textContent = state.config.live_ready ? "Live · AI providers" : "Live · setup required";
    text("system-details", `${state.config.ffmpeg_available ? "Video export ready" : "Video export unavailable"} · ${state.config.live_ready ? "AI providers ready" : "Demo available"}`);
    if (state.config.ffmpeg_available === false) {
      display("connection-notice", true);
      text("connection-notice", "The video renderer is unavailable. Install FFmpeg, then restart the server to enable productions.");
    }
  }

  async function refresh(silent = true) {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      const payload = await api("/api/jobs");
      state.jobs = (Array.isArray(payload.jobs) ? payload.jobs : []).sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
      setConnection(true); renderJobs();
      if (!state.selectedId && state.jobs.length) {
        const active = state.jobs.find(job => !TERMINAL.has(job.status));
        state.selectedId = (active || state.jobs[0]).id;
      }
      if (state.selectedId) {
        const selected = state.selectedId;
        const job = await api(`/api/jobs/${encodeURIComponent(selected)}`);
        if (state.selectedId === selected) {
          state.job = job;
          const version = `${job.id}:${job.updated_at}:${job.status}:${job.current_stage}`;
          if (version !== state.selectedVersion || !TERMINAL.has(job.status)) {
            await refreshDetails(selected); state.selectedVersion = version;
          }
          renderJob(); renderJobs();
        }
      }
      if (Date.now() - state.healthCheckedAt > 30000) await refreshConfig();
      if (!silent) toast("Productions are up to date.");
    } catch (error) {
      if (!error.status) setConnection(false);
      if (!silent) toast(error.message, true);
    } finally { state.refreshing = false; }
  }
  function schedulePoll() {
    clearTimeout(state.timer);
    const active = state.jobs.some(job => !TERMINAL.has(job.status) && job.status !== "awaiting_review");
    const delay = document.hidden ? 15000 : !state.connected ? 10000 : active ? 2800 : 8000;
    state.timer = setTimeout(async () => { await refresh(); schedulePoll(); }, delay);
  }

  function setBusy(busy) {
    state.busy = busy;
    for (const id of ["create-button", "approve-plan", "approve-media", "cancel-job", "retry-job"]) $(id).disabled = busy;
  }

  async function createJob(event) {
    event.preventDefault();
    if (state.busy) return;
    display("form-error", false);
    const form = new FormData($("create-form"));
    const brief = String(form.get("brief") || "").trim();
    if (brief.length < 10) { text("form-error", "Give the agents a little more direction — at least 10 characters."); display("form-error", true); return; }
    const payload = {
      title: String(form.get("title") || "").trim() || null, brief,
      duration_seconds: Number(form.get("duration_seconds")), aspect_ratio: form.get("aspect_ratio"),
      style: form.get("style"), provider_mode: form.get("provider_mode"), review_required: $("review-required").checked
    };
    if (!payload.title) delete payload.title;
    setBusy(true); $("create-button").lastElementChild.textContent = "…";
    try {
      const result = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
      const job = result.job || result;
      if (!job.id) throw new Error("The server did not return a production ID. Refresh your productions.");
      state.jobs.unshift(job); state.selectedId = null;
      await selectJob(job.id, true);
      toast("Production created. Your agents are on it.");
      schedulePoll();
    } catch (error) { text("form-error", error.message); display("form-error", true); }
    finally { setBusy(false); $("create-button").lastElementChild.textContent = "→"; }
  }

  function reviewedPlan() {
    const panels = Array.from($("scene-editors").children);
    const scenes = panels.map((panel, index) => ({
      id: state.job.scenes[index].id,
      narration: panel.querySelector(".scene-narration").value.trim(),
      visual_prompt: panel.querySelector(".scene-visual_prompt").value.trim(),
      duration_seconds: Number(panel.querySelector(".scene-duration-input").value)
    }));
    if (!scenes.length) throw new Error("The scene plan is still being prepared.");
    const script = scenes.map(scene => scene.narration).join(" ");
    if (!script.trim()) throw new Error("Add scene narration before approving the storyboard.");
    if (scenes.some(scene => !scene.narration || scene.visual_prompt.length < 5 || !Number.isFinite(scene.duration_seconds) || scene.duration_seconds < 1 || scene.duration_seconds > 60)) throw new Error("Every scene needs narration, a visual prompt of at least 5 characters, and a duration of 1–60 seconds.");
    const total = scenes.reduce((sum, scene) => sum + scene.duration_seconds, 0);
    if (Math.abs(total - state.job.duration_seconds) > 0.5) throw new Error(`Scene durations total ${total.toFixed(1)} seconds. Adjust them to ${state.job.duration_seconds} seconds before approving.`);
    return { script, scenes, notes: $("review-notes").value.trim() };
  }

  async function jobAction(action) {
    if (!state.job || state.busy) return;
    let payload = {};
    try { if (action === "approve" && isPlanReview(state.job)) payload = reviewedPlan(); }
    catch (error) { toast(error.message, true); return; }
    const id = state.job.id; setBusy(true);
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(id)}/${action}`, { method: "POST", body: JSON.stringify(payload) });
      state.editorDirty = false; state.editorKey = null; state.selectedVersion = null;
      if (result.id && state.selectedId === id) state.job = result;
      const message = action === "approve" ? "Approved. Your production is moving forward." : action === "retry" ? "Production queued for another run." : "Production cancelled.";
      toast(message);
      if (action !== "cancel") selectTab("overview");
      await refresh(); schedulePoll();
    } catch (error) { toast(error.message, true); }
    finally { setBusy(false); }
  }

  $("create-form").addEventListener("submit", createJob);
  $("brief").addEventListener("input", () => text("brief-count", `${$("brief").value.length.toLocaleString()} / 4,000`));
  $("duration").addEventListener("input", () => { $("duration-output").value = $("duration").value; });
  $("script-editor").addEventListener("input", () => { state.editorDirty = true; });
  $("review-notes").addEventListener("input", () => { state.editorDirty = true; });
  $("provider-mode").addEventListener("change", () => text("mode-explanation", $("provider-mode").value === "live" ? "Live mode calls configured AI providers. Provider usage may incur charges. Review checkpoints let you approve the plan before media generation." : "Demo uses deterministic scripts, designed scene cards and offline speech when available. AI generation requires configured providers."));
  $("refresh-jobs").addEventListener("click", () => refresh(false));
  $("approve-plan").addEventListener("click", () => jobAction("approve"));
  $("approve-media").addEventListener("click", () => jobAction("approve"));
  $("cancel-job").addEventListener("click", () => jobAction("cancel"));
  $("retry-job").addEventListener("click", () => jobAction("retry"));
  $("open-review").addEventListener("click", () => {
    if (isPlanReview(state.job)) selectTab("storyboard");
    else $("media-preview").scrollIntoView({ behavior: "smooth", block: "center" });
  });
  for (const tab of document.querySelectorAll("[data-tab]")) {
    tab.addEventListener("click", () => selectTab(tab.dataset.tab));
    tab.addEventListener("keydown", event => {
      const tabs = Array.from(document.querySelectorAll("[data-tab]"));
      const index = tabs.indexOf(tab);
      let next;
      if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
      if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
      if (event.key === "Home") next = tabs[0];
      if (event.key === "End") next = tabs[tabs.length - 1];
      if (next) { event.preventDefault(); selectTab(next.dataset.tab); next.focus(); }
    });
  }
  const samples = {
    coffee: { title: "A better morning", brief: "Create a warm, cinematic introduction to a sustainable coffee brand. Show the journey from sunlit coffee farms to a carefully brewed morning cup. Speak to people who care about thoughtful rituals and responsible sourcing. End with an invitation to make tomorrow's coffee a little better." },
    space: { title: "A little further", brief: "Tell an inspiring story about our curiosity for space. Begin with someone looking up at the stars, move through the beauty of distant planets, and finish on the idea that discovery starts with a simple question. Use a poetic, optimistic voice for an audience of young explorers." }
  };
  for (const button of document.querySelectorAll("[data-prompt]")) button.addEventListener("click", () => {
    const sample = samples[button.dataset.prompt];
    $("title").value = sample.title; $("brief").value = sample.brief;
    $("brief").dispatchEvent(new Event("input")); $("brief").focus();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); schedulePoll(); });
  selectTab("overview");
  (async () => { await refreshConfig(); await refresh(); schedulePoll(); })();
})();
