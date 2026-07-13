const config = window.SMART_STOPOVER_CONFIG || {};
const API_URL = config.API_URL || "http://127.0.0.1:8000/api/search";
const HEALTH_URL = config.HEALTH_URL || API_URL.replace(/\/api\/search\/?$/, "/api/health");

const form = document.getElementById("searchForm");
const submitButton = document.getElementById("submitButton");
const demoButton = document.getElementById("demoButton");
const healthCheckButton = document.getElementById("healthCheckButton");
const statusBox = document.getElementById("statusBox");
const summaryBox = document.getElementById("summaryBox");
const confirmationBox = document.getElementById("confirmationBox");
const resultsContainer = document.getElementById("resultsContainer");
const resultCount = document.getElementById("resultCount");

let lastPayload = null;

function todayPlus(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

document.getElementById("earliestDepartureDate").value = todayPlus(30);

function splitIata(value) {
  return String(value || "")
    .split(",")
    .map((x) => x.trim().toUpperCase())
    .filter(Boolean);
}

function numberValue(id, fallback = 0) {
  const raw = document.getElementById(id).value;
  const value = Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

function stringValue(id) {
  return document.getElementById(id).value.trim();
}

function setStatus(message, type = "idle") {
  statusBox.className = `status-box ${type}`;
  statusBox.textContent = message;
}

function buildPayload(forceContinue = false) {
  const key = stringValue("serpapiApiKey");
  const payload = {
    departure_airports: splitIata(stringValue("departureAirports")),
    destination_airports: splitIata(stringValue("destinationAirports")),
    earliest_departure_date: stringValue("earliestDepartureDate"),
    max_trip_days: numberValue("maxTripDays", 7),
    min_destination_hours: numberValue("minDestinationHours", 96),
    min_stopover_hours: numberValue("minStopoverHours", 12),
    average_direct_price: numberValue("averageDirectPrice", 120),
    currency: stringValue("currency") || "EUR",
    force_continue: forceContinue,
    top_b_for_bc_api: numberValue("topBForBcApi", 35),
    top_b_after_bc_for_ab_api: numberValue("topBAfterBcForAbApi", 30),
    top_bc_flights_per_bc_pair: numberValue("topBcFlightsPerBcPair", 3),
    total_api_budget: numberValue("totalApiBudget", 250),
    bc_api_budget: numberValue("bcApiBudget", 150),
    ab_api_budget: numberValue("abApiBudget", 100),
    cache_ttl_hours: numberValue("cacheTtlHours", 48)
  };

  if (key) {
    payload.serpapi_api_key = key;
  }

  return payload;
}

function validatePayload(payload) {
  if (!payload.departure_airports.length) throw new Error("Add at least one departure airport.");
  if (!payload.destination_airports.length) throw new Error("Add at least one destination airport.");
  if (!payload.earliest_departure_date) throw new Error("Choose an earliest departure date.");
  if (!payload.average_direct_price || payload.average_direct_price <= 0) throw new Error("Average direct price must be greater than zero.");
}

async function runSearch(payload) {
  validatePayload(payload);
  lastPayload = payload;
  confirmationBox.classList.add("hidden");
  setStatus("Search running. This can take a while if live API calls are needed...", "loading");
  submitButton.disabled = true;
  submitButton.textContent = "Searching...";
  renderSummary(null);
  renderResults([]);

  try {
    const response = await fetch(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      const detail = data.detail || data.message || `HTTP ${response.status}`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }

    handleResponse(data);
  } catch (error) {
    setStatus(`Error: ${error.message}`, "error");
    console.error(error);
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "Start search";
  }
}

function handleResponse(data) {
  const status = data.status || "unknown";
  renderSummary(data.summary || {});

  if (status === "needs_confirmation") {
    setStatus(data.summary?.message || "The best B → C segment is expensive. Confirm if you want to continue.", "warning");
    showConfirmation(data.summary || {});
    renderResults([]);
    return;
  }

  const itineraries = data.itineraries || [];
  if (itineraries.length) {
    setStatus(`Search completed. Found ${itineraries.length} final itineraries.`, "success");
  } else {
    setStatus(`Search completed with status: ${status}. No final itinerary was returned with these constraints.`, status.includes("partial") ? "warning" : "idle");
  }
  renderResults(itineraries);
}

function showConfirmation(summary) {
  const price = formatMoney(summary.best_BC_price, summary.currency || "EUR");
  const ratio = summary.best_BC_ratio != null ? `${(summary.best_BC_ratio * 100).toFixed(1)}%` : "not available";
  confirmationBox.innerHTML = `
    <strong>Continue anyway?</strong>
    <p>The best B → C segment found costs ${price}, equal to ${ratio} of the direct benchmark. Continuing will consume A → B API budget.</p>
    <button id="continueButton" type="button" class="primary-button">Continue search</button>
  `;
  confirmationBox.classList.remove("hidden");
  document.getElementById("continueButton").addEventListener("click", () => {
    const nextPayload = { ...(lastPayload || buildPayload()), force_continue: true };
    runSearch(nextPayload);
  });
}

function renderSummary(summary) {
  summaryBox.innerHTML = "";
  if (!summary) return;

  const items = [
    ["Final itineraries", summary.final_itineraries],
    ["Offline candidates", summary.offline_candidates],
    ["B → C API calls", summary.api_calls_bc],
    ["A → B API calls", summary.api_calls_ab],
    ["B → C flights", summary.bc_flights],
    ["A → B flights", summary.ab_flights],
    ["Cache hits B → C", summary.cache_hits_bc],
    ["Cache hits A → B", summary.cache_hits_ab]
  ];

  for (const [label, value] of items) {
    if (value === undefined || value === null) continue;
    const div = document.createElement("div");
    div.className = "summary-item";
    div.innerHTML = `<strong>${value}</strong><span>${label}</span>`;
    summaryBox.appendChild(div);
  }
}

function renderResults(items) {
  resultCount.textContent = `${items.length} result${items.length === 1 ? "" : "s"}`;

  if (!items.length) {
    resultsContainer.className = "results-container empty-state";
    resultsContainer.textContent = "Results will appear here after the search.";
    return;
  }

  resultsContainer.className = "results-container";
  resultsContainer.innerHTML = "";

  items.forEach((item, index) => {
    const card = document.createElement("article");
    card.className = "result-card";
    const currency = item.currency_AB || item.currency_BC || "EUR";
    card.innerHTML = `
      <div class="result-card-header">
        <div>
          <p class="route-title">${escapeHtml(item.origin_A)} → ${escapeHtml(item.stopover_B)} → ${escapeHtml(item.destination_C)}</p>
          <div class="route-meta">#${index + 1} · ${escapeHtml(item.stopover_city || item.stopover_name || "Stopover")} ${item.stopover_country ? "· " + escapeHtml(item.stopover_country) : ""}</div>
        </div>
        <div class="score-pill">${formatNumber(item.final_itinerary_score, 1)} / 10</div>
      </div>

      <div class="kpi-row">
        <div class="kpi"><strong>${formatMoney(item.total_price, currency)}</strong><span>Total price</span></div>
        <div class="kpi"><strong>${formatMoney(item.saving_abs, currency)}</strong><span>Saving</span></div>
        <div class="kpi"><strong>${formatNumber(item.saving_pct, 1)}%</strong><span>Saving %</span></div>
        <div class="kpi"><strong>${formatNumber(item.stopover_hours, 1)}h</strong><span>Stopover</span></div>
      </div>

      <div class="legs">
        <div class="leg">
          <h4>Leg 1 · A → B</h4>
          <p><strong>${escapeHtml(item.origin_A)} → ${escapeHtml(item.stopover_B)}</strong></p>
          <p>${formatDateTime(item.departure_datetime_AB)} → ${formatDateTime(item.arrival_datetime_AB)}</p>
          <p>${escapeHtml(item.airline_name_AB || "Unknown airline")} ${item.flight_number_AB ? "· " + escapeHtml(item.flight_number_AB) : ""}</p>
          <small>${formatMoney(item.price_AB, currency)}</small>
        </div>
        <div class="leg">
          <h4>Leg 2 · B → C</h4>
          <p><strong>${escapeHtml(item.stopover_B)} → ${escapeHtml(item.destination_C)}</strong></p>
          <p>${formatDateTime(item.departure_datetime_BC)} → ${formatDateTime(item.arrival_datetime_BC)}</p>
          <p>${escapeHtml(item.airline_name_BC || "Unknown airline")} ${item.flight_number_BC ? "· " + escapeHtml(item.flight_number_BC) : ""}</p>
          <small>${formatMoney(item.price_BC, currency)}</small>
        </div>
      </div>
    `;
    resultsContainer.appendChild(card);
  });
}

function formatMoney(value, currency = "EUR") {
  const n = Number(value);
  if (!Number.isFinite(n)) return "n/a";
  return new Intl.NumberFormat("en-US", { style: "currency", currency, maximumFractionDigits: 0 }).format(n);
}

function formatNumber(value, digits = 1) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "n/a";
  return n.toFixed(digits);
}

function formatDateTime(value) {
  if (!value) return "n/a";
  const d = new Date(String(value).replace(" ", "T"));
  if (Number.isNaN(d.getTime())) return escapeHtml(String(value));
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  try {
    runSearch(buildPayload(false));
  } catch (error) {
    setStatus(`Error: ${error.message}`, "error");
  }
});

healthCheckButton.addEventListener("click", async () => {
  setStatus("Checking backend...", "loading");
  try {
    const response = await fetch(HEALTH_URL);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    const keyText = data.server_serpapi_key_present ? "server key configured" : "no server key, use the frontend key field";
    setStatus(`Backend ok. DB exists: ${data.db_exists}. ${keyText}.`, data.db_exists ? "success" : "warning");
  } catch (error) {
    setStatus(`Backend check failed: ${error.message}`, "error");
  }
});

demoButton.addEventListener("click", async () => {
  try {
    const response = await fetch("demo_response.json");
    const data = await response.json();
    setStatus("Demo results loaded. No API calls were made.", "success");
    renderSummary(data.summary || {});
    renderResults(data.itineraries || []);
  } catch (error) {
    setStatus(`Could not load demo results: ${error.message}`, "error");
  }
});
