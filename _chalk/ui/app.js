const state = {
  project: null,
  selectedId: null,
  view: "segment",
  capturedTime: 0,
};

const $ = (selector) => document.querySelector(selector);
const els = {
  projectTitle: $("#project-title"),
  buildState: $("#build-state"),
  buildLabel: $("#build-label"),
  refresh: $("#refresh-button"),
  segmentCount: $("#segment-count"),
  segmentList: $("#segment-list"),
  fullCutButton: $("#full-cut-button"),
  fullCutDetail: $("#full-cut-detail"),
  overallPercent: $("#overall-percent"),
  overallProgress: $("#overall-progress"),
  nextAction: $("#next-action"),
  nowShowingLabel: $("#now-showing-label"),
  nowShowingTitle: $("#now-showing-title"),
  artifactMeta: $("#artifact-meta"),
  videoFrame: $("#video-frame"),
  video: $("#review-video"),
  timecode: $("#timecode-badge"),
  segmentView: $("#segment-view-button"),
  fullView: $("#full-view-button"),
  reviewSummary: $("#review-summary"),
  runway: $("#runway-track"),
  selectedIndex: $("#selected-index"),
  selectedTitle: $("#selected-title"),
  selectedId: $("#selected-id"),
  narration: $("#narration-preview"),
  approval: $("#approval-badge"),
  approve: $("#approve-button"),
  stages: $("#stage-list"),
  noteCount: $("#note-count"),
  notes: $("#note-list"),
  noteForm: $("#note-form"),
  noteMessage: $("#note-message"),
  noteCategory: $("#note-category"),
  noteSeverity: $("#note-severity"),
  noteSubmit: $('#note-form button[type="submit"]'),
  captureTime: $("#capture-time-button"),
  inspectorScope: $("#inspector-scope-label"),
  scopeNote: $("#scope-note"),
  versionDetails: $("#version-details"),
  toast: $("#toast"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatTime(seconds) {
  const safe = Number.isFinite(Number(seconds)) ? Math.max(0, Number(seconds)) : 0;
  const minutes = Math.floor(safe / 60);
  const remainder = safe - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${remainder.toFixed(1).padStart(4, "0")}`;
}

function shortHash(value) {
  if (!value) return "—";
  return String(value).replace("sha256:", "").slice(0, 9);
}

function segmentState(segment) {
  if ((segment.openNotes || []).length) return "attention";
  if (segment.cut?.current && segment.approval?.current) return "current";
  if (segment.cut?.current || segment.render?.current) return "stale";
  return "working";
}

function stageRows(segment) {
  const take = segment.take || {};
  const transcript = segment.transcript || {};
  const render = segment.render || {};
  const cut = segment.cut || {};
  return [
    ["Script", Number(segment.wordCount) > 0, `${segment.wordCount || 0} words`],
    [
      "Visual",
      Boolean(segment.scene?.valid ?? segment.scene?.exists),
      (segment.scene?.valid ?? segment.scene?.exists)
        ? "scene valid"
        : segment.scene?.exists
          ? "scene invalid"
          : "missing",
    ],
    [
      "Take",
      Boolean(take.current),
      take.current ? take.label || "current" : take.selected ? "stale" : "not recorded",
    ],
    [
      "Timing",
      Boolean(transcript.current),
      transcript.current ? "aligned" : transcript.exists ? "stale" : "estimated",
    ],
    ["Proxy", Boolean(render.current), render.current ? "current" : render.exists ? "stale" : "missing"],
    ["A/V cut", Boolean(cut.current), cut.current ? "current" : cut.exists ? "stale" : "missing"],
  ];
}

function projectPercent(project) {
  const stages = (project.segments || []).flatMap(stageRows);
  if (!stages.length) return 0;
  return Math.round((stages.filter(([, done]) => done).length / stages.length) * 100);
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("visible");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => els.toast.classList.remove("visible"), 2600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}

async function loadProject({ quiet = false } = {}) {
  if (!quiet) {
    els.buildState.dataset.state = "working";
    els.buildLabel.textContent = "Reading project";
  }
  try {
    state.project = await api("/api/project");
    if (!state.selectedId || !state.project.segments.some((s) => s.id === state.selectedId)) {
      state.selectedId = state.project.segments[0]?.id || null;
    }
    render();
  } catch (error) {
    els.buildState.dataset.state = "attention";
    els.buildLabel.textContent = "Project unavailable";
    showToast(`Could not read project: ${error.message}`);
  }
}

function renderHeader(project) {
  els.projectTitle.textContent = project.title || "Untitled video";
  const open = project.openNoteCount || 0;
  const stale = project.segments.filter((segment) => !segment.cut?.current).length;
  els.buildState.dataset.state = open ? "attention" : stale ? "working" : "current";
  els.buildLabel.textContent = open
    ? `${open} open observation${open === 1 ? "" : "s"}`
    : stale
      ? `${stale} segment${stale === 1 ? "" : "s"} changing`
      : "Current cut";
}

function renderSequence(project) {
  const percent = project.readinessPercent ?? projectPercent(project);
  els.segmentCount.textContent = project.segments.length;
  els.overallPercent.textContent = `${percent}%`;
  els.overallProgress.setAttribute("aria-valuenow", percent);
  els.overallProgress.querySelector("span").style.width = `${percent}%`;
  els.nextAction.textContent = project.nextAction || "Review the next changed segment.";
  els.fullCutDetail.textContent = project.fullCut?.current
    ? `${formatTime(project.fullCut.durationSeconds)} · current ${project.fullCut.profile || "draft"}`
    : project.fullCut?.exists
      ? "A newer segment has not been assembled"
      : "No assembled cut yet";

  els.segmentList.innerHTML = project.segments
    .map((segment, index) => {
      const active = segment.id === state.selectedId ? " active" : "";
      const status = segmentState(segment);
      const statusLabel = {
        attention: "open feedback",
        current: "current and approved",
        stale: "in progress",
        working: "source incomplete",
      }[status];
      const detail = segment.cut?.current
        ? `${formatTime(segment.cut.durationSeconds)} · cut current`
        : segment.take?.selected
          ? "take selected · needs review"
          : segment.scene?.exists
            ? "visual draft"
            : "source incomplete";
      return `
        <button class="segment-button${active}" data-segment="${escapeHtml(segment.id)}" type="button"${
          active ? ' aria-current="step"' : ""
        }>
          <span class="segment-number">${String(index + 1).padStart(2, "0")}</span>
          <span>
            <strong>${escapeHtml(segment.title)}</strong>
            <small>${escapeHtml(detail)}</small>
          </span>
          <i class="status-pin ${status}" aria-hidden="true"></i>
          <span class="sr-only">Status: ${escapeHtml(statusLabel)}</span>
        </button>`;
    })
    .join("");

  els.segmentList.querySelectorAll("[data-segment]").forEach((button) => {
    button.addEventListener("click", () => selectSegment(button.dataset.segment));
  });
}

function renderRunway(project) {
  els.runway.innerHTML = project.segments
    .map((segment, index) => {
      const status = segmentState(segment);
      const statusLabel = status === "attention" ? "open feedback" : status === "current" ? "current" : "in progress";
      const active = segment.id === state.selectedId ? " active" : "";
      return `
        <button class="runway-item${active}" data-runway-segment="${escapeHtml(segment.id)}" type="button" aria-pressed="${
          active ? "true" : "false"
        }">
          <i class="runway-state ${status}" aria-hidden="true"></i>
          <strong>${escapeHtml(segment.title)}</strong>
          <small>${String(index + 1).padStart(2, "0")} · ${segment.wordCount || 0}w</small>
          <span class="sr-only">Status: ${escapeHtml(statusLabel)}</span>
        </button>`;
    })
    .join("");
  els.runway.querySelectorAll("[data-runway-segment]").forEach((button) => {
    button.addEventListener("click", () => selectSegment(button.dataset.runwaySegment));
  });
}

function selectedSegment() {
  return state.project?.segments.find((segment) => segment.id === state.selectedId) || null;
}

function setVideo(source) {
  const next = source || "";
  if (els.video.dataset.source !== next) {
    els.video.pause();
    els.video.removeAttribute("src");
    els.video.dataset.source = next;
    if (next) {
      els.video.src = next;
      els.video.load();
    }
    state.capturedTime = 0;
    els.captureTime.textContent = `Use ${formatTime(0)}`;
  }
  els.videoFrame.classList.toggle("has-video", Boolean(next));
}

function renderTheater(project, segment) {
  const full = state.view === "full";
  const artifact = full ? project.fullCut : segment?.cut;
  els.nowShowingLabel.textContent = full ? "Full rough cut" : "Now showing";
  els.nowShowingTitle.textContent = full
    ? project.title || "Full cut"
    : segment?.title || "Choose a segment";
  els.segmentView.classList.toggle("active", !full);
  els.fullView.classList.toggle("active", full);
  els.segmentView.setAttribute("aria-pressed", String(!full));
  els.fullView.setAttribute("aria-pressed", String(full));
  els.fullCutButton.setAttribute("aria-pressed", String(full));
  setVideo(artifact?.url);

  const chips = [];
  if (artifact?.profile) chips.push(artifact.profile);
  if (artifact?.timing) chips.push(artifact.timing);
  if (artifact?.current) chips.push("current");
  else if (artifact?.exists) chips.push("stale");
  els.artifactMeta.innerHTML = chips
    .map((chip) => `<span class="meta-chip">${escapeHtml(chip)}</span>`)
    .join("");

  if (artifact?.exists) {
    els.reviewSummary.innerHTML = `<span>${formatTime(artifact.durationSeconds)} · ${escapeHtml(
      artifact.current ? "exact inputs current" : "newer source exists",
    )}</span>`;
  } else {
    els.reviewSummary.innerHTML = "<span>No playable artifact yet</span>";
  }
}

function renderInspector(segment) {
  if (!segment) return;
  const full = state.view === "full";
  const index = state.project.segments.findIndex((item) => item.id === segment.id) + 1;
  els.inspectorScope.textContent = full ? "Full-cut review" : "Selected segment";
  els.selectedIndex.textContent = full ? "ALL" : String(index).padStart(2, "0");
  els.selectedTitle.textContent = full ? state.project.title : segment.title;
  els.selectedId.textContent = full ? "assembled sequence" : segment.id;
  els.narration.textContent = full
    ? "Review transitions, global pacing, continuity, loudness, and whether the full argument lands."
    : segment.narration || "No spoken text yet.";

  const approval = segment.approval || {};
  els.approval.className = full
    ? `approval-badge ${state.project.fullCut?.current ? "current" : "stale"}`
    : `approval-badge ${approval.current ? "current" : approval.exists ? "stale" : ""}`;
  els.approval.textContent = full
    ? state.project.fullCut?.current
      ? "full cut current"
      : "full cut stale"
    : approval.current
      ? "approved"
      : approval.exists
        ? "approval stale"
        : "not approved";
  els.approve.disabled = full || !segment.cut?.current || approval.current;
  els.approve.textContent = full
    ? "Switch to Segment view to approve"
    : approval.current
      ? "This exact cut is approved"
      : "Approve this exact cut";

  const currentCuts = state.project.segments.filter((item) => item.cut?.current).length;
  const inspectorStages = full
    ? [
        [
          "Segment cuts",
          currentCuts === state.project.segments.length,
          `${currentCuts}/${state.project.segments.length} current`,
        ],
        [
          "Full cut",
          Boolean(state.project.fullCut?.current),
          state.project.fullCut?.current
            ? "assembled and current"
            : state.project.fullCut?.exists
              ? "assembly stale"
              : "not assembled",
        ],
        [
          "Full-cut feedback",
          (state.project.fullCut?.openNotes || []).length === 0,
          `${(state.project.fullCut?.openNotes || []).length} open`,
        ],
      ]
    : stageRows(segment);
  els.stages.innerHTML = inspectorStages
    .map(([label, done, detail]) => {
      const attention = !done && (full || ["Take", "Timing", "A/V cut"].includes(label));
      return `
        <li class="stage-item ${done ? "done" : attention ? "attention" : ""}">
          <span class="stage-icon">${done ? "✓" : attention ? "!" : "·"}</span>
          <span>${escapeHtml(label)}</span>
          <small>${escapeHtml(detail)}</small>
        </li>`;
    })
    .join("");

  const notes = full
    ? state.project.fullCut?.openNotes || []
    : segment.openNotes || [];
  els.noteCount.textContent = notes.length;
  els.notes.innerHTML = notes
    .map(
      (note) => `
        <article class="note-card ${escapeHtml(note.severity || "note")}">
          <div class="note-meta">
            <span>${escapeHtml(note.category || "note")} · ${formatTime(note.timeSeconds)}</span>
            <span>${escapeHtml(note.id)}</span>
          </div>
          <p>${escapeHtml(note.message)}</p>
          <button data-resolve-note="${escapeHtml(note.id)}" type="button">Mark resolved</button>
        </article>`,
    )
    .join("");
  els.notes.querySelectorAll("[data-resolve-note]").forEach((button) => {
    button.addEventListener("click", () => resolveNote(button.dataset.resolveNote));
  });

  els.scopeNote.hidden = !full;
  els.noteForm.setAttribute("aria-disabled", "false");

  const versions = [
    ["Script", full ? null : segment.narrationHash],
    ["Scene", full ? null : segment.scene?.hash],
    ["Take", full ? null : segment.take?.id || segment.take?.audioHash],
    [
      "Cut",
      full
        ? state.project.fullCut?.artifactId || state.project.fullCut?.hash
        : segment.cut?.artifactId || segment.cut?.hash,
    ],
  ];
  els.versionDetails.innerHTML = versions
    .map(
      ([label, value]) =>
        `<div><dt>${escapeHtml(label)}</dt><dd title="${escapeHtml(value || "")}">${escapeHtml(
          shortHash(value),
        )}</dd></div>`,
    )
    .join("");
}

function render() {
  const project = state.project;
  if (!project) return;
  const segment = selectedSegment();
  renderHeader(project);
  renderSequence(project);
  renderRunway(project);
  renderTheater(project, segment);
  renderInspector(segment);
}

function selectSegment(id) {
  state.selectedId = id;
  state.view = "segment";
  render();
}

function setView(view) {
  state.view = view;
  render();
}

async function addNote(event) {
  event.preventDefault();
  const segment = selectedSegment();
  const message = els.noteMessage.value.trim();
  if (!segment || !message) return;
  const full = state.view === "full";
  try {
    await api("/api/notes", {
      method: "POST",
      body: JSON.stringify({
        segmentId: full ? null : segment.id,
        scope: full ? "project" : "segment",
        message,
        category: els.noteCategory.value,
        severity: els.noteSeverity.value,
        timeSeconds: state.capturedTime,
        artifactId: full
          ? state.project.fullCut?.artifactId || null
          : segment.cut?.artifactId || null,
      }),
    });
    els.noteMessage.value = "";
    showToast(`Saved feedback at ${formatTime(state.capturedTime)}`);
    await loadProject({ quiet: true });
  } catch (error) {
    showToast(`Could not save note: ${error.message}`);
  }
}

async function resolveNote(id) {
  const resolution = window.prompt("What changed, and what did you check?", "Checked the revised cut.");
  if (resolution === null) return;
  try {
    await api(`/api/notes/${encodeURIComponent(id)}/resolve`, {
      method: "POST",
      body: JSON.stringify({ resolution }),
    });
    showToast("Feedback resolved");
    await loadProject({ quiet: true });
  } catch (error) {
    showToast(`Could not resolve note: ${error.message}`);
  }
}

async function approveSegment() {
  const segment = selectedSegment();
  if (state.view === "full" || !segment) return;
  try {
    await api(`/api/segments/${encodeURIComponent(segment.id)}/approve`, {
      method: "POST",
      body: "{}",
    });
    showToast("Approval bound to this exact cut");
    await loadProject({ quiet: true });
  } catch (error) {
    showToast(`Could not approve: ${error.message}`);
  }
}

function captureCurrentTime() {
  state.capturedTime = els.video.currentTime || 0;
  els.captureTime.textContent = `Use ${formatTime(state.capturedTime)}`;
  showToast(`Captured ${formatTime(state.capturedTime)}`);
}

els.refresh.addEventListener("click", () => loadProject());
els.fullCutButton.addEventListener("click", () => setView("full"));
els.segmentView.addEventListener("click", () => setView("segment"));
els.fullView.addEventListener("click", () => setView("full"));
els.video.addEventListener("timeupdate", () => {
  els.timecode.textContent = formatTime(els.video.currentTime);
  els.captureTime.textContent = `Use ${formatTime(els.video.currentTime)}`;
});
els.captureTime.addEventListener("click", captureCurrentTime);
els.noteForm.addEventListener("submit", addNote);
els.approve.addEventListener("click", approveSegment);

loadProject();
