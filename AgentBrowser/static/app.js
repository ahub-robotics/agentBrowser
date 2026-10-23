const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
let state = null,
  busy = false,
  automatic = false;
const goals = {
  flights: 'Find one-way flights from Zurich to London on September 20, 2026, for one adult in economy. Stop when matching flight options are visible. Do not select or book a flight.',
  travel: 'Find a Design stay in Lisbon with Free cancellation and open Casa Flora.',
  research:
    "Open the article about using finite choices to control browser agents.",
};
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const percent = (value) => `${(value * 100).toFixed(value < 0.01 ? 1 : 0)}%`;
const limitLabels = {
  inventory_exhausted: "All collected candidates have been presented",
  extraction_incomplete: "More page content may need to be discovered",
  window_limit: "Candidate window limit reached",
  decision_budget: "Decision limit reached",
  time_budget: "Time limit reached",
  action_budget: "Browser action limit reached",
  inspection_limit: "Page inspection limit reached",
  budget_insufficient: "Remaining candidates exceed the request budget",
  candidate_budget_exceeded: "The request exceeds its candidate budget",
  no_progress: "Actions produced no observed progress",
  observation_not_ready: "The page did not become ready within the observation limit",
  repeated_state: "The same state keeps recurring without progress",
  verification_budget: "Evidence review limit reached",
  verification_no_progress: "Requested outcomes could not be verified",
  verification_invalid_response: "The evidence review returned an invalid result",
  verification_unconfigured: "Configure the evidence reviewer to verify completion",
  verification_provider_error: "The evidence reviewer is unavailable",
};
function decisionBinding() {
  const d = state?.decision;
  if (!d?.inventory_id) return { fingerprint: d?.fingerprint || state?.page?.fingerprint };
  return Object.fromEntries(
    ["decision_id", "inventory_id", "window_id", "selection_digest", "fingerprint"]
      .map((key) => [key, d[key]]),
  );
}
function renderCoverage(page) {
  const coverage = page?.candidate_coverage;
  $("candidate-coverage").hidden = !coverage;
  if (!coverage) return;
  $("candidate-window").textContent = `Window ${coverage.window_id}`;
  $("candidate-counts").textContent =
    `${coverage.nodes_admitted} elements · ${coverage.actions_admitted} actions`;
  $("candidate-progress").textContent =
    `${coverage.presented_unique_actions} unique actions presented · ${coverage.pending_known} collected actions pending`;
  $("candidate-scope").textContent = [
    coverage.budget_complete ? "Window includes the full inventory" : "Window includes part of the inventory",
    coverage.extraction_complete ? "Extraction complete within its scope" : "Page extraction incomplete",
  ].join(". ") + ".";
  const reason = state.stop_reason || coverage.termination_reason;
  $("candidate-limit").textContent = coverage.more_available && !state.stop_reason
    ? "Jev can request another window when it needs more candidates."
    : limitLabels[reason] || (reason ? "Candidate inspection stopped at a limit" : "No further candidate window available");
}
async function call(name, body = {}) {
  const response = await fetch(`/api/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Demo-Token": token },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "Request failed");
  state = data;
  render();
  return data;
}
function controls() {
  const live = state?.page && !["done", "blocked", "unverified"].includes(state.status);
  $("start").disabled = busy;
  $("scenario").disabled = busy;
  $("goal").disabled = busy;
  $("choose").disabled = busy || !live;
  $("execute").disabled = busy || !state?.decision || !live;
  $("auto").disabled = busy || !live;
  $("auto").hidden = automatic;
  $("stop").hidden = !automatic;
  $("execute").textContent = state?.decision?.operation === "MORE_CANDIDATES"
    ? "Inspect more candidates" : "Execute choice";
  $("download").disabled = !(
    state?.history?.length || state?.candidate_controls?.length || state?.decisions?.length || state?.model_calls
  );
}
async function perform(fn, label) {
  if (busy) return;
  busy = true;
  $("error").hidden = true;
  controls();
  $("status").textContent = label;
  try {
    await fn();
  } catch (error) {
    automatic = false;
    try {
      state = await fetch("/api/state").then((r) => r.json());
      render();
    } catch {
      /* Preserve the original failure if the server disconnected. */
    }
    $("error").textContent = error.message;
    $("error").hidden = false;
    $("status").textContent = "Paused · needs attention";
  } finally {
    busy = false;
    controls();
  }
}
function render() {
  if (!state) return;
  $("helper").textContent = `Text helper · ${state.text_model}`;
  $("plan").innerHTML = (state.plan || [])
    .map(
      (goal, i) =>
        `<div class="plan-step ${i === state.plan_index ? "current" : ""}"><span>${i < state.plan_index ? "✓" : i + 1}</span>${escape(goal)}</div>`,
    )
    .join("");
  const page = state.page,
    d =
      state.decision ||
      (state.status === "done" ? state.decisions?.at(-1) : null);
  const labels = {
    idle: "Ready to explore",
    ready: "Page observed · ready for a decision",
    observing: "Waiting for observable page content",
    predicted: "Choice ready · inspect or execute",
    done: "Task complete · evidence review satisfied",
    blocked: "Stopped · no supported next action",
    unverified: "Stopped · completion could not be verified",
  };
  $("status").textContent = labels[state.status] || state.status;
  if (state.status === "done" && state.verification?.status !== "satisfied") {
    $("status").textContent = "Jev reports complete · result unverified";
  }
  const verification = state.verification;
  $("verification").hidden = !verification?.requirements?.length;
  $("verification").innerHTML = (verification?.requirements || []).map((requirement) =>
    `<p><strong>${escape(requirement.status)}</strong> · ${escape(requirement.description)}</p>`,
  ).join("");
  renderCoverage(page);
  if (!page) {
    controls();
    return;
  }
  $("empty").hidden = true;
  $("screenshot").hidden = false;
  // Match the preview to the actual CDP viewport instead of relying on a
  // fixed desktop ratio, which crops screenshots at other viewport sizes.
  if (Number.isFinite(page.w) && Number.isFinite(page.h) && page.w > 0 && page.h > 0) {
    $("viewport").style.aspectRatio = `${page.w} / ${page.h}`;
  }
  $("screenshot").src = `data:image/jpeg;base64,${page.screenshot}`;
  $("url").textContent = page.url;
  $("page-title").textContent = page.title;
  $("action-count").textContent = `${state.elements.length} elements`;
  const chosen = page.actions.find((a) => a.id === d?.choice);
  $("choice-title").textContent = d
    ? d.operation === "MORE_CANDIDATES" ? "Inspect more candidates" : chosen?.label || d.choice
    : "Choose an action";
  $("latency").textContent = d ? `${d.latency_ms} ms` : "—";
  $("confidence").textContent = d?.target_confidence != null ? percent(d.target_confidence) : "—";
  $("completion").textContent = d ? d.operation : "—";
  $("ranking-note").textContent = d ? "Ranked by Jev" : "Unranked";
  const op = Object.entries(d?.operation_probabilities || {}).sort((a,b)=>b[1]-a[1]);
  $("operation-choices").innerHTML = op.map(([name,p]) =>
    `<span class="operation-choice ${name === d.operation ? 'best' : ''}">${escape(name)} <b>${percent(p)}</b></span>`).join('');
  const probability = e => d?.target_probabilities?.[e.index] ??
    Math.max(-1, ...(e.options || []).map(o=>d?.target_probabilities?.[o.index] ?? -1));
  const selectedIndex = d?.target?.split(':')[0];
  const elements = [...state.elements];
  if (d) elements.sort((a,b)=>probability(b)-probability(a));
  $("choices").innerHTML = elements.map(e => {
    const p = probability(e);
    return `<div class="choice ${selectedIndex === e.index ? 'best' : ''}" data-action="${escape(e.index)}"><span class="choice-id">[${escape(e.index)}]</span><div class="choice-label">${escape(e.label)}<small>${escape(e.role)} · ${escape(e.operations.join(' / '))}${e.value ? ' · '+escape(e.value) : ''}${e.checked !== undefined ? ' · checked '+escape(e.checked) : ''}</small>${p >= 0 ? `<div class="bar" style="--probability:${p*100}%"></div>` : ''}</div><span class="probability">${p >= 0 ? percent(p) : '—'}</span></div>`;
  }).join('');
  const targets = new Map();
  for (const a of page.actions) if (a.rect && !targets.has(a.node)) targets.set(a.node, a);
  $("targets").innerHTML = [...targets.values()].map((a,i) => {
    const index=String(i+1);
    return `<div class="target ${index === selectedIndex ? 'selected' : ''}" data-action="${index}" style="left:${100*a.rect.x/page.w}%;top:${100*a.rect.y/page.h}%;width:${100*a.rect.w/page.w}%;height:${100*a.rect.h/page.h}%"><span>${index}</span></div>`;
  }).join('');
  $("targets").hidden = !$("overlays").checked;
  $("history").innerHTML = state.history.length
    ? state.history
        .map(
          (h) =>
            `<div class="trace-row"><span class="number">${String(h.step).padStart(2, "0")}</span><div>${escape(h.action)}${h.text ? ` <b>“${escape(h.text)}”</b><small>${escape(h.text_helper)}</small>` : ""}</div><span class="time">${h.latency_ms} ms · ${percent(h.probability)}</span><span class="effect">${h.page_changed ? "Page changed" : "No change observed"}</span></div>`,
        )
        .join("")
    : '<p class="muted">Each executed action leaves an observed result.</p>';
  const reconsults = state.candidate_controls?.length || 0;
  $("candidate-history").hidden = !reconsults;
  $("candidate-history").textContent =
    `${reconsults} candidate reconsultations · no browser actions executed by these requests`;
  $("step-count").textContent = [
    `${state.history.length} actions`,
    `${state.model_calls ?? state.decisions?.length ?? 0} Jev decisions`,
    `${reconsults} reconsultations`,
    `${state.verification_calls || 0} evidence reviews`,
    `${(state.elapsed_ms / 1000).toFixed(2)} s`,
  ].join(" · ");
  $("step-count").title = `${state.model_http_attempts ?? 0} Jev HTTP attempts · ` +
    `${state.verification_http_attempts ?? 0} review HTTP attempts`;
  const usage = state.model_usage;
  const usagePanel = $("usage-summary");
  usagePanel.hidden = !usage?.calls;
  usagePanel.innerHTML = usage?.calls
    ? `<strong>Model usage · ${usage.calls} calls</strong><ul>${usage.models.map((model) => {
      const cost = model.cost_usd == null ? "cost unavailable"
        : `$${model.cost_usd.toFixed(6)} USD${model.cost_basis === "published_rate" ? " (published TypeSafe rate)" : ""}`;
      return `<li>${escape(model.component)} · ${escape(model.model)}: ${model.calls} calls · ` +
        `${model.input_tokens} input / ${model.output_tokens} output tokens · ${cost}</li>`;
    }).join("")}</ul><p>${usage.cost_usd == null
      ? `Total cost unavailable; ${usage.missing_cost_calls} call(s) had no usable price. Known subtotal: $${usage.known_cost_usd.toFixed(6)} USD.`
      : `Total cost: $${usage.cost_usd.toFixed(6)} USD${usage.cost_basis === "published_rate" ? " (calculated from TypeSafe's published rate)" : ""}.`}</p>`
    : "";
  $("model-state").textContent = JSON.stringify(
    d?.request || {
      goal: state.goal,
      url: page.url,
      text: page.text,
      actions: page.actions.map(({ rect, node, ...rest }) => rest),
      candidate_coverage: page.candidate_coverage,
    },
    null,
    2,
  );
  controls();
}
$("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  automatic = false;
  perform(
    () =>
      call("reset", { scenario: $("scenario").value, goal: $("goal").value }),
    "Opening a fresh browser…",
  );
});
$("scenario").addEventListener("change", () => {
  $("goal").value = goals[$("scenario").value];
});
$("choose").addEventListener("click", () =>
  perform(() => call("predict"), "Jev is comparing the actions…"),
);
$("execute").addEventListener("click", () =>
  perform(
    () => call("act", decisionBinding()),
    "Executing the choice…",
  ),
);
$("auto").addEventListener("click", () =>
  perform(async () => {
    automatic = true;
    controls();
    while (automatic && state.page && !["done", "blocked", "unverified"].includes(state.status)) {
      $("status").textContent = "Running…";
      if ($("pace").checked) {
        await call("predict");
        if (["done", "blocked", "unverified"].includes(state.status)) break;
        if (!state.decision) continue;
        await new Promise(resolve => setTimeout(resolve, 450));
        if (!automatic) break;
        await call("act", decisionBinding());
      } else {
        await call("tick");
      }
      if (["done", "blocked", "unverified"].includes(state.status)) break;
    }
    automatic = false;
  }, "Running the browser…"),
);
$("stop").addEventListener("click", () => {
  automatic = false;
  $("status").textContent = "Pausing after the current request…";
  controls();
});
$("overlays").addEventListener("change", () => {
  $("targets").hidden = !$("overlays").checked;
});
$("choices").addEventListener("pointerover", (event) => {
  const id = event.target.closest("[data-action]")?.dataset.action;
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === id || t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    );
});
$("choices").addEventListener("pointerleave", () =>
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    ),
);
$("download").addEventListener("click", () => {
  const { page, ...rest } = state;
  const blob = new Blob(
    [
      JSON.stringify(
        { ...rest, page: { ...page, screenshot: undefined } },
        null,
        2,
      ),
    ],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "typesafe-browser-trace.json";
  a.click();
  URL.revokeObjectURL(url);
});
fetch("/api/state")
  .then((r) => r.json())
  .then((s) => {
    state = s;
    render();
  })
  .catch(() => {
    $("status").textContent = "Cannot reach local demo server";
  });
