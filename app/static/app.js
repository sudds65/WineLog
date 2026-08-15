/* WineLog front end — no build step, no external requests. */
(function () {
  "use strict";

  // ── helpers ──────────────────────────────────────────────────────────

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const money = (cents, opts) => {
    const n = (cents || 0) / 100;
    return n.toLocaleString("en-US", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: opts && opts.whole ? 0 : 2,
      maximumFractionDigits: opts && opts.whole ? 0 : 2,
    });
  };

  const moneyShort = (cents) => {
    const n = (cents || 0) / 100;
    if (Math.abs(n) >= 1000) {
      const k = n / 1000;
      return "$" + (k % 1 === 0 ? k.toFixed(0) : k.toFixed(1)) + "k";
    }
    return "$" + Math.round(n);
  };

  // Dates arrive as plain YYYY-MM-DD; parse as local noon so no timezone
  // shift can move a purchase onto the previous day.
  const parseDay = (iso) => {
    if (!iso) return null;
    const [y, m, d] = String(iso).slice(0, 10).split("-").map(Number);
    if (!y || !m || !d) return null;
    return new Date(y, m - 1, d, 12, 0, 0);
  };

  const fmtDate = (iso, opts) => {
    const date = parseDay(iso);
    if (!date) return "—";
    return date.toLocaleDateString("en-US", opts || { month: "short", day: "numeric" });
  };

  const fmtDateLong = (iso) =>
    fmtDate(iso, { month: "short", day: "numeric", year: "numeric" });

  const todayISO = () => {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  };

  const esc = (value) =>
    String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));

  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;

  function toast(message) {
    const node = $("#toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(node._timer);
    node._timer = setTimeout(() => { node.hidden = true; }, 2800);
  }

  function setBusy(button, busy, label) {
    if (!button) return;
    if (busy) {
      button._label = button.innerHTML;
      button.disabled = true;
      button.innerHTML = `<span class="spinner"></span>${esc(label || "Working…")}`;
    } else {
      button.disabled = false;
      if (button._label) button.innerHTML = button._label;
    }
  }

  function showError(node, message) {
    if (!node) return;
    if (message) {
      node.textContent = message;
      node.hidden = false;
    } else {
      node.hidden = true;
    }
  }

  // ── API ──────────────────────────────────────────────────────────────

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }

  async function api(path, options) {
    const opts = Object.assign({ headers: {} }, options);
    opts.headers = Object.assign({ "X-WineLog-App": "1" }, opts.headers);
    opts.credentials = "same-origin";
    if (opts.json !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
      delete opts.json;
    }

    let response;
    try {
      response = await fetch(path, opts);
    } catch (err) {
      throw new ApiError("Can't reach the server. Are you on the VPN?", 0);
    }

    if (response.status === 401 && state.user) {
      state.user = null;
      showLogin("Your session expired. Please sign in again.");
      throw new ApiError("Session expired", 401);
    }

    const isJson = (response.headers.get("content-type") || "").includes("json");
    const body = isJson ? await response.json().catch(() => null) : null;

    if (!response.ok) {
      let message = `Something went wrong (${response.status}).`;
      if (body && typeof body.detail === "string") {
        message = body.detail;
      } else if (body && Array.isArray(body.detail) && body.detail.length) {
        const first = body.detail[0];
        const field = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : "";
        message = `${field ? field + ": " : ""}${first.msg || "Invalid value"}`;
      }
      throw new ApiError(message, response.status);
    }
    return body;
  }

  // ── state ────────────────────────────────────────────────────────────

  const state = {
    user: null,
    stats: null,
    receipts: null,
    settings: null,
    pending: null,      // parsed receipt awaiting confirmation
    insightPeriod: "year",
    loaded: {},
  };

  // ── auth ─────────────────────────────────────────────────────────────

  function showLogin(message) {
    $("#app").hidden = true;
    $("#login").hidden = false;
    showError($("#login-error"), message || "");
    const field = $("#login-username");
    if (field && !field.value) setTimeout(() => field.focus(), 60);
  }

  function showApp() {
    $("#login").hidden = true;
    $("#app").hidden = false;
    $("#topbar-user").textContent = state.user || "";
    $("#sidenav-user").textContent = state.user ? `Signed in as ${state.user}` : "";
  }

  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#login-submit");
    showError($("#login-error"), "");
    setBusy(button, true, "Signing in…");
    try {
      const result = await api("/api/login", {
        method: "POST",
        json: {
          username: $("#login-username").value,
          password: $("#login-password").value,
        },
      });
      state.user = result.username;
      $("#login-password").value = "";
      showApp();
      state.loaded = {};
      await route();
    } catch (err) {
      showError($("#login-error"), err.message);
    } finally {
      setBusy(button, false);
    }
  });

  async function logout() {
    try {
      await api("/api/logout", { method: "POST" });
    } catch (err) {
      /* signing out locally regardless */
    }
    state.user = null;
    state.stats = null;
    state.receipts = null;
    state.loaded = {};
    showLogin("Signed out.");
  }

  $("#logout-btn").addEventListener("click", logout);
  $("#logout-btn-desktop").addEventListener("click", logout);

  // ── routing ──────────────────────────────────────────────────────────

  const VIEWS = ["dashboard", "log", "add", "search", "settings"];

  function currentView() {
    const name = (location.hash || "").replace(/^#\/?/, "").split("?")[0];
    return VIEWS.includes(name) ? name : "dashboard";
  }

  async function route() {
    if (!state.user) return;
    const view = currentView();

    $$(".view").forEach((node) => {
      node.hidden = node.dataset.view !== view;
    });
    $$(".navlink, .tab").forEach((node) => {
      node.classList.toggle("is-active", node.dataset.view === view);
    });
    $("#main").scrollTop = 0;
    window.scrollTo(0, 0);

    try {
      if (view === "dashboard") await renderDashboard();
      else if (view === "log") await renderLog();
      else if (view === "add") await initAdd();
      else if (view === "search") await initSearch();
      else if (view === "settings") await renderSettings();
    } catch (err) {
      if (err.status !== 401) toast(err.message);
    }
  }

  window.addEventListener("hashchange", route);

  // ── stats loading ────────────────────────────────────────────────────

  async function loadStats(force) {
    if (!state.stats || force) state.stats = await api("/api/stats");
    return state.stats;
  }

  function invalidate() {
    state.stats = null;
    state.receipts = null;
    state.loaded = {};
  }

  // ── dashboard ────────────────────────────────────────────────────────

  async function renderDashboard() {
    const stats = await loadStats();
    const data = await api("/api/receipts?limit=5");

    $("#dash-sub").textContent = stats.term_end
      ? `Membership runs to ${fmtDateLong(stats.term_end)}.`
      : "";

    $("#hero-saved").textContent = money(stats.saved_cents);
    $("#hero-target").textContent = money(stats.target_cents, { whole: true });

    const pct = Math.max(0, Math.min(100, stats.progress_percent || 0));
    $("#meter-fill").style.width = `${pct}%`;
    $("#meter-percent").textContent = `${pct}%`;
    $("#meter-remaining").textContent = stats.broke_even
      ? "Breakeven reached 🎉"
      : `${money(stats.remaining_cents)} to go`;
    $("#meter-figure").setAttribute(
      "aria-label",
      `${money(stats.saved_cents)} saved of ${money(stats.target_cents)} — ${pct}% of the way to breakeven`
    );

    renderPace(stats);

    $("#stat-visits").textContent = stats.visit_count;
    $("#stat-items").textContent = plural(stats.item_count, "wine/beer line");
    $("#stat-avg").textContent = money(stats.avg_saved_per_visit_cents);
    $("#stat-spend").textContent = `${money(stats.paid_cents)} actually paid`;

    $("#stat-projected").textContent = stats.broke_even
      ? "Reached"
      : stats.projected_breakeven
      ? fmtDate(stats.projected_breakeven, { month: "short", year: "numeric" })
      : "—";
    $("#stat-projected-note").textContent = stats.broke_even
      ? "Everything from here is profit"
      : stats.days_to_breakeven
      ? `about ${plural(stats.days_to_breakeven, "day")} at this rate`
      : "log a few more visits";

    $("#stat-termend").textContent = stats.term_end
      ? fmtDate(stats.term_end, { month: "short", day: "numeric" })
      : "—";
    $("#stat-daysleft").textContent =
      stats.days_left == null
        ? ""
        : stats.days_left >= 0
        ? `${plural(stats.days_left, "day")} left`
        : "expired";

    renderChart(stats);
    renderChartTable(stats);
    renderCategorySplit(stats);
    renderRecent(data.receipts);
  }

  function renderPace(stats) {
    const node = $("#pace-note");
    node.classList.remove("pace--good", "pace--warn");

    if (stats.broke_even) {
      node.classList.add("pace--good");
      node.textContent =
        "You've made the membership back. Every pour from here is ahead of the game.";
      return;
    }
    if (!stats.visit_count) {
      node.textContent = "Log your first purchase to start the tally.";
      return;
    }
    if (stats.days_left != null && stats.days_left <= 0) {
      node.classList.add("pace--warn");
      node.textContent = `The membership term ended and you finished ${money(
        stats.remaining_cents
      )} short of breakeven.`;
      return;
    }

    const rate = money(stats.daily_rate_cents);
    const needed = stats.required_daily_cents != null ? money(stats.required_daily_cents) : null;
    if (stats.on_pace) {
      node.classList.add("pace--good");
      node.textContent = `On pace. You're saving ${rate}/day and only need ${
        needed || "less"
      }/day to break even by ${fmtDateLong(stats.term_end)}.`;
    } else if (needed) {
      node.classList.add("pace--warn");
      node.textContent = `Behind pace. You're saving ${rate}/day but need ${needed}/day to break even by ${fmtDateLong(
        stats.term_end
      )}.`;
    } else {
      node.textContent = `You're saving about ${rate}/day.`;
    }
  }

  function renderCategorySplit(stats) {
    const node = $("#category-split");
    const cats = (stats.categories || []).filter((c) => c.saved_cents > 0);
    if (!cats.length) {
      node.innerHTML = '<p class="empty">Nothing logged yet.</p>';
      return;
    }
    const total = cats.reduce((sum, c) => sum + c.saved_cents, 0);
    const label = { wine: "Wine", beer: "Beer", other: "Other" };
    node.innerHTML = cats
      .map((cat) => {
        const share = total ? Math.round((cat.saved_cents / total) * 100) : 0;
        return `
          <div class="splitrow">
            <span class="splitrow__name">${esc(label[cat.category] || cat.category)}</span>
            <span class="splitrow__bar"><span class="splitrow__fill" style="width:${share}%"></span></span>
            <span class="splitrow__val">${money(cat.saved_cents)}
              <span class="splitrow__pct">${share}%</span></span>
          </div>`;
      })
      .join("");
  }

  function itemLine(item) {
    return `
      <div class="line${item.qualifying ? "" : " line--muted"}">
        <div class="line__main">
          <span class="line__name">${esc(item.description)}</span>
          <span class="line__tags">
            ${item.serving ? `<span class="pill">${esc(item.serving)}</span>` : ""}
            ${item.qualifying
              ? `<span class="pill pill--${esc(item.category)}">${esc(item.category)}</span>`
              : '<span class="pill pill--muted">not counted</span>'}
          </span>
        </div>
        <div class="line__nums">
          <span class="line__saved">${item.qualifying ? "−" + money(item.discount_cents) : "—"}</span>
          <span class="line__list">${money(item.reg_price_cents)} list</span>
        </div>
      </div>`;
  }

  function receiptCard(receipt, opts) {
    const deletable = !opts || opts.deletable !== false;
    const items = receipt.items || [];
    const counted = items.filter((i) => i.qualifying);
    const skipped = items.filter((i) => !i.qualifying);

    const chips = [
      receipt.receipt_no ? `<span class="pill pill--muted">#${esc(receipt.receipt_no)}</span>` : "",
      receipt.source === "pdf" ? '<span class="pill pill--muted">from PDF</span>' : "",
    ].join("");

    const footParts = [];
    if (receipt.total_cents != null) footParts.push(`${money(receipt.total_cents)} charged`);
    if (receipt.tip_cents) footParts.push(`${money(receipt.tip_cents)} tip`);
    if (receipt.tax_cents) footParts.push(`${money(receipt.tax_cents)} tax`);

    return `
      <article class="receipt" data-receipt="${receipt.id}">
        <header class="receipt__head">
          <div>
            <div class="receipt__date">${esc(fmtDateLong(receipt.purchased_on))}</div>
            <div class="receipt__meta">
              ${receipt.merchant ? `<span>${esc(receipt.merchant)}</span>` : ""}${chips}
            </div>
          </div>
          <div class="receipt__side">
            <div class="receipt__saved">${money(receipt.saved_cents)}</div>
            <div class="row__sub">saved of ${money(receipt.pre_discount_cents)}</div>
          </div>
        </header>

        <div class="receipt__lines">
          ${counted.map(itemLine).join("")}
          ${skipped.length ? skipped.map(itemLine).join("") : ""}
        </div>

        <footer class="receipt__foot">
          <span>${footParts.join(" · ")}</span>
          ${deletable
            ? '<button class="btn btn--danger btn--sm" data-delete type="button">Remove</button>'
            : ""}
        </footer>
      </article>`;
  }

  function renderRecent(receipts) {
    const node = $("#recent-list");
    if (!receipts.length) {
      node.innerHTML = '<p class="empty">No purchases logged yet. <a href="#/add">Add one.</a></p>';
      return;
    }
    node.innerHTML = receipts.map((r) => receiptCard(r, { deletable: false })).join("");
  }

  // ── chart ────────────────────────────────────────────────────────────

  const SVG_NS = "http://www.w3.org/2000/svg";
  const svgEl = (name, attrs) => {
    const node = document.createElementNS(SVG_NS, name);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value != null) node.setAttribute(key, value);
    });
    return node;
  };

  let chartData = null;

  function renderChart(stats) {
    const host = $("#chart");
    const series = stats.series || [];
    host.innerHTML = "";

    if (!series.length) {
      host.innerHTML =
        '<p class="chart__empty">Your savings curve shows up here once you log a purchase.</p>';
      chartData = null;
      return;
    }

    const width = Math.max(260, host.clientWidth || 320);
    const height = width < 520 ? 208 : 254;
    const pad = { top: 16, right: 20, bottom: 26, left: width < 400 ? 40 : 50 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;

    const target = stats.target_cents || 1;
    const firstDay = parseDay(series[0].date);
    const lastDay = parseDay(series[series.length - 1].date);
    const today = parseDay(stats.today) || new Date();

    let x0 = parseDay(stats.term_start) || firstDay;
    let x1 = parseDay(stats.term_end) || today;
    if (firstDay < x0) x0 = firstDay;
    if (lastDay > x1) x1 = lastDay;
    if (today > x1) x1 = today;
    if (x1 <= x0) x1 = new Date(x0.getTime() + 86400000 * 30);

    const maxY = Math.max(target, series[series.length - 1].cumulative_cents) * 1.03;
    const spanX = x1 - x0;
    const sx = (date) => pad.left + ((parseDay(date) || date) - x0) / spanX * plotW;
    const sy = (cents) => pad.top + plotH - (cents / maxY) * plotH;

    const svg = svgEl("svg", {
      viewBox: `0 0 ${width} ${height}`,
      width,
      height,
      role: "img",
      "aria-label":
        `Cumulative savings reaching ${money(stats.saved_cents)} of a ` +
        `${money(stats.target_cents)} breakeven target. Full data in the table view.`,
    });

    // gridlines + y labels
    const ticks = width < 400 ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1];
    ticks.forEach((frac) => {
      const value = target * frac;
      const y = sy(value);
      svg.appendChild(
        svgEl("line", {
          x1: pad.left, x2: width - pad.right, y1: y, y2: y,
          stroke: "var(--grid)", "stroke-width": 1,
        })
      );
      const label = svgEl("text", {
        x: pad.left - 7, y: y + 3.5, "text-anchor": "end",
        "font-size": 10, fill: "var(--text-3)",
      });
      label.textContent = moneyShort(value);
      svg.appendChild(label);
    });

    // x labels
    const months = Math.max(1, Math.round(spanX / (86400000 * 30.4)));
    const step = Math.max(1, Math.ceil(months / (width < 400 ? 3 : 6)));
    for (let i = 0; i <= months; i += step) {
      const at = new Date(x0.getFullYear(), x0.getMonth() + i, 1, 12);
      if (at < x0 || at > x1) continue;
      const label = svgEl("text", {
        x: sx(at), y: height - 8, "text-anchor": "middle",
        "font-size": 10, fill: "var(--text-3)",
      });
      label.textContent = at.toLocaleDateString("en-US", { month: "short" });
      svg.appendChild(label);
    }

    // even-pace reference: straight run from term start to breakeven at term end
    const paceEnd = parseDay(stats.term_end);
    if (paceEnd) {
      svg.appendChild(
        svgEl("line", {
          x1: sx(x0), y1: sy(0), x2: sx(paceEnd), y2: sy(target),
          stroke: "var(--pace)", "stroke-width": 1.5,
          "stroke-dasharray": "5 4", "stroke-linecap": "round",
        })
      );
    }

    // breakeven reference line
    svg.appendChild(
      svgEl("line", {
        x1: pad.left, x2: width - pad.right, y1: sy(target), y2: sy(target),
        stroke: "var(--text-3)", "stroke-width": 1.5, "stroke-dasharray": "2 3",
      })
    );
    const breakevenLabel = svgEl("text", {
      x: pad.left + 4, y: sy(target) - 5,
      "font-size": 10, fill: "var(--text-2)", "font-weight": 600,
    });
    breakevenLabel.textContent = `Breakeven ${moneyShort(target)}`;
    svg.appendChild(breakevenLabel);

    // today marker
    if (today > x0 && today < x1) {
      svg.appendChild(
        svgEl("line", {
          x1: sx(today), x2: sx(today), y1: pad.top, y2: pad.top + plotH,
          stroke: "var(--grid)", "stroke-width": 1, "stroke-dasharray": "3 3",
        })
      );
    }

    // savings curve — stepped, because savings land on purchase days
    const points = series.map((point) => ({
      x: sx(point.date), y: sy(point.cumulative_cents), point,
    }));
    let path = `M ${sx(x0).toFixed(2)} ${sy(0).toFixed(2)}`;
    points.forEach((p) => {
      path += ` L ${p.x.toFixed(2)} ${sy(0 + (p.point.cumulative_cents - p.point.saved_cents)).toFixed(2)}`;
      path += ` L ${p.x.toFixed(2)} ${p.y.toFixed(2)}`;
    });
    const lastX = points[points.length - 1].x;
    const lastY = points[points.length - 1].y;
    const nowX = Math.max(lastX, Math.min(sx(today), width - pad.right));
    path += ` L ${nowX.toFixed(2)} ${lastY.toFixed(2)}`;

    svg.appendChild(
      svgEl("path", {
        d: `${path} L ${nowX.toFixed(2)} ${sy(0).toFixed(2)} L ${sx(x0).toFixed(2)} ${sy(0).toFixed(2)} Z`,
        fill: "var(--accent)", opacity: 0.12, stroke: "none",
      })
    );
    svg.appendChild(
      svgEl("path", {
        d: path, fill: "none", stroke: "var(--accent)",
        "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
      })
    );

    points.forEach((p) => {
      svg.appendChild(
        svgEl("circle", {
          cx: p.x, cy: p.y, r: 3.5, fill: "var(--accent)",
          stroke: "var(--surface)", "stroke-width": 2,
        })
      );
    });

    // direct label on the leading edge — no legend needed for one series
    const labelText = svgEl("text", {
      y: Math.max(pad.top + 9, lastY - 9),
      "font-size": 11.5, "font-weight": 660, fill: "var(--accent)",
    });
    labelText.textContent = money(stats.saved_cents);
    const nearRight = nowX > width - pad.right - 62;
    labelText.setAttribute("x", nearRight ? nowX - 6 : nowX + 6);
    labelText.setAttribute("text-anchor", nearRight ? "end" : "start");
    svg.appendChild(labelText);

    host.appendChild(svg);

    chartData = { points, pad, width, height, plotH, host };
    attachChartHover(host, svg);
  }

  function attachChartHover(host, svg) {
    const tip = document.createElement("div");
    tip.className = "chart__tip";
    tip.hidden = true;
    host.appendChild(tip);

    const crosshair = svgEl("line", {
      stroke: "var(--text-3)", "stroke-width": 1, "stroke-dasharray": "3 3", opacity: 0,
    });
    const halo = svgEl("circle", {
      r: 6, fill: "none", stroke: "var(--accent)", "stroke-width": 2, opacity: 0,
    });
    svg.appendChild(crosshair);
    svg.appendChild(halo);

    function move(event) {
      if (!chartData) return;
      const rect = svg.getBoundingClientRect();
      const clientX = event.touches ? event.touches[0].clientX : event.clientX;
      const x = ((clientX - rect.left) / rect.width) * chartData.width;

      let nearest = chartData.points[0];
      chartData.points.forEach((p) => {
        if (Math.abs(p.x - x) < Math.abs(nearest.x - x)) nearest = p;
      });

      crosshair.setAttribute("x1", nearest.x);
      crosshair.setAttribute("x2", nearest.x);
      crosshair.setAttribute("y1", chartData.pad.top);
      crosshair.setAttribute("y2", chartData.pad.top + chartData.plotH);
      crosshair.setAttribute("opacity", 1);
      halo.setAttribute("cx", nearest.x);
      halo.setAttribute("cy", nearest.y);
      halo.setAttribute("opacity", 1);

      tip.innerHTML =
        `<span class="tip__date">${esc(fmtDateLong(nearest.point.date))}</span>` +
        `<b>${money(nearest.point.cumulative_cents)}</b> total saved<br>` +
        `+${money(nearest.point.saved_cents)} that day`;
      tip.hidden = false;

      const scale = rect.width / chartData.width;
      const left = Math.min(Math.max(nearest.x * scale, 74), rect.width - 74);
      tip.style.left = `${left}px`;
      tip.style.top = `${Math.max(nearest.y * scale - 12, 8)}px`;
    }

    function leave() {
      tip.hidden = true;
      crosshair.setAttribute("opacity", 0);
      halo.setAttribute("opacity", 0);
    }

    svg.addEventListener("mousemove", move);
    svg.addEventListener("mouseleave", leave);
    svg.addEventListener("touchstart", move, { passive: true });
    svg.addEventListener("touchmove", move, { passive: true });
    svg.addEventListener("touchend", leave);
  }

  function renderChartTable(stats) {
    const node = $("#chart-table");
    const series = stats.series || [];
    if (!series.length) {
      node.innerHTML = "";
      return;
    }
    node.innerHTML = `
      <div class="table-wrap">
        <table class="data">
          <thead><tr><th>Date</th><th>Saved</th><th>Running total</th><th>Left to breakeven</th></tr></thead>
          <tbody>
            ${series
              .map(
                (row) => `<tr>
                  <td>${esc(fmtDateLong(row.date))}</td>
                  <td>${money(row.saved_cents)}</td>
                  <td>${money(row.cumulative_cents)}</td>
                  <td>${money(Math.max(0, stats.target_cents - row.cumulative_cents))}</td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>`;
  }

  $("#chart-table-toggle").addEventListener("click", (event) => {
    const node = $("#chart-table");
    const showing = node.hidden;
    node.hidden = !showing;
    event.currentTarget.setAttribute("aria-expanded", String(showing));
    event.currentTarget.textContent = showing ? "Hide table" : "Table";
  });

  let resizeTimer;
  window.addEventListener("resize", () => {
    if (currentView() !== "dashboard" || !state.stats) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => renderChart(state.stats), 160);
  });

  // ── purchases ────────────────────────────────────────────────────────

  async function renderLog() {
    const node = $("#log-list");
    node.innerHTML = '<p class="empty">Loading…</p>';
    const [stats, data] = await Promise.all([loadStats(), api("/api/receipts")]);

    $("#log-sub").textContent = `${plural(data.receipts.length, "visit")} · ${money(
      stats.saved_cents
    )} saved so far`;

    if (!data.receipts.length) {
      node.innerHTML = '<p class="empty">Nothing logged yet. <a href="#/add">Add a purchase.</a></p>';
      return;
    }
    node.innerHTML = data.receipts.map((r) => receiptCard(r)).join("");
  }

  $("#log-list").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-delete]");
    if (!button) return;
    const row = button.closest("[data-receipt]");
    const id = row.dataset.receipt;
    if (!confirm("Remove this purchase from the tally?")) return;
    setBusy(button, true, "Removing…");
    try {
      const result = await api(`/api/receipts/${id}`, { method: "DELETE" });
      state.stats = result.stats;
      state.receipts = null;
      toast("Purchase removed.");
      await renderLog();
    } catch (err) {
      setBusy(button, false);
      toast(err.message);
    }
  });

  // ── add: shared ──────────────────────────────────────────────────────

  $$(".segmented__btn[data-pane]").forEach((button) => {
    button.addEventListener("click", () => {
      const pane = button.dataset.pane;
      $$(".segmented__btn[data-pane]").forEach((b) => {
        const active = b === button;
        b.classList.toggle("is-active", active);
        b.setAttribute("aria-selected", String(active));
      });
      $$(".pane").forEach((p) => { p.hidden = p.dataset.pane !== pane; });
    });
  });

  async function initAdd() {
    const dateField = $("#manual-date");
    if (!dateField.value) dateField.value = todayISO();
    if (!state.settings) state.settings = await api("/api/settings");
    const percent = $("#manual-percent");
    if (!percent.dataset.touched) {
      percent.value = state.settings.discount_percent || 50;
    }
    updateManualPreview();
  }

  // ── add: manual ──────────────────────────────────────────────────────

  function parseAmount(raw) {
    const cleaned = String(raw || "").replace(/[$,\s]/g, "");
    if (!cleaned) return null;
    const value = Number(cleaned);
    return Number.isFinite(value) && value >= 0 ? value : null;
  }

  function updateManualPreview() {
    const amount = parseAmount($("#manual-amount").value);
    const percent = Number($("#manual-percent").value);
    const node = $("#manual-preview");
    if (amount == null || !Number.isFinite(percent)) {
      node.textContent = "Enter the pre-discount price to see the saving.";
      return;
    }
    const saved = Math.round(amount * 100 * (percent / 100));
    node.innerHTML = `You save <b>${money(saved)}</b> and pay <b>${money(
      Math.round(amount * 100) - saved
    )}</b> before tax and tip.`;
  }

  $("#manual-amount").addEventListener("input", updateManualPreview);
  $("#manual-percent").addEventListener("input", (event) => {
    event.currentTarget.dataset.touched = "1";
    updateManualPreview();
  });

  $("#manual-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    const error = $("#manual-error");
    showError(error, "");

    const amount = parseAmount($("#manual-amount").value);
    if (amount == null || amount <= 0) {
      showError(error, "Enter the full price before the discount, like 31.00");
      return;
    }

    setBusy(button, true, "Saving…");
    try {
      const result = await api("/api/purchases", {
        method: "POST",
        json: {
          purchased_on: $("#manual-date").value,
          description: $("#manual-description").value.trim(),
          pre_discount: amount.toFixed(2),
          discount_percent: Number($("#manual-percent").value),
          category: $("#manual-category").value,
        },
      });
      state.stats = result.stats;
      state.receipts = null;
      $("#manual-description").value = "";
      $("#manual-amount").value = "";
      updateManualPreview();
      toast(`Logged. ${money(result.stats.remaining_cents)} to breakeven.`);
      location.hash = "#/dashboard";
    } catch (err) {
      showError(error, err.message);
    } finally {
      setBusy(button, false);
    }
  });

  // ── add: PDF upload ──────────────────────────────────────────────────

  const drop = $("#drop");
  const fileInput = $("#file-input");

  $("#choose-file").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (fileInput.files && fileInput.files[0]) handleFile(fileInput.files[0]);
    fileInput.value = "";
  });

  ["dragenter", "dragover"].forEach((name) =>
    drop.addEventListener(name, (event) => {
      event.preventDefault();
      drop.classList.add("is-over");
    })
  );
  ["dragleave", "drop"].forEach((name) =>
    drop.addEventListener(name, (event) => {
      event.preventDefault();
      drop.classList.remove("is-over");
    })
  );
  drop.addEventListener("drop", (event) => {
    const file = event.dataTransfer && event.dataTransfer.files[0];
    if (file) handleFile(file);
  });

  async function handleFile(file) {
    const error = $("#upload-error");
    showError(error, "");
    $("#review").hidden = true;

    if (!/\.pdf$/i.test(file.name)) {
      showError(error, "That's not a PDF. Print the Gmail receipt to PDF and try again.");
      return;
    }

    const body = new FormData();
    body.append("file", file, file.name);
    const button = $("#choose-file");
    setBusy(button, true, "Reading…");
    try {
      const parsed = await api("/api/receipts/parse", { method: "POST", body });
      state.pending = parsed;
      renderReview(parsed);
    } catch (err) {
      showError(error, err.message);
    } finally {
      setBusy(button, false);
    }
  }

  function renderReview(parsed) {
    const node = $("#review");
    const items = parsed.items || [];
    const counted = items.filter((i) => i.qualifying);
    const savings = counted.reduce((sum, i) => sum + i.discount_cents, 0);

    const warnings = (parsed.warnings || [])
      .map((w) => `<p class="alert alert--warn">${esc(w)}</p>`)
      .join("");

    const duplicate = parsed.duplicate_of
      ? `<p class="alert alert--error">Already logged on ${esc(
          fmtDateLong(parsed.duplicate_of.purchased_on)
        )}. Saving again would double-count it.</p>`
      : "";

    node.className = "review";
    node.innerHTML = `
      <div class="review__head">
        <div>
          <div class="review__title">${esc(parsed.merchant || "Receipt")}</div>
          <div class="review__meta">
            ${esc(parsed.purchased_on ? fmtDateLong(parsed.purchased_on) : "date not found")}
            ${parsed.receipt_no ? ` · #${esc(parsed.receipt_no)}` : ""}
            ${parsed.total_cents != null ? ` · ${money(parsed.total_cents)} total` : ""}
          </div>
        </div>
        <div class="review__total">${money(savings)}<span class="row__sub"> counts</span></div>
      </div>
      ${duplicate}
      ${warnings}
      ${
        parsed.purchased_on
          ? ""
          : `<label class="field"><span class="field__label">Purchase date</span>
               <input class="input" type="date" id="review-date" value="${todayISO()}"></label>`
      }
      <p class="hint" style="margin:10px 0 8px">
        Ticked lines carry the founders discount and count toward breakeven.
        Untick anything that shouldn't.
      </p>
      ${items
        .map(
          (item, index) => `
        <label class="review__item${item.qualifying ? "" : " is-excluded"}" data-index="${index}">
          <input type="checkbox" ${item.qualifying ? "checked" : ""} data-toggle>
          <span class="row__main">
            <span class="row__title">${esc(item.description)}</span>
            <span class="row__meta">
              ${item.serving ? `<span class="pill">${esc(item.serving)}</span>` : ""}
              ${item.qualifying ? `<span class="pill pill--${esc(item.category)}">${esc(item.category)}</span>` : '<span class="pill pill--muted">no member discount</span>'}
              <span>${money(item.reg_price_cents)} list · paid ${money(item.paid_cents)}</span>
            </span>
          </span>
          <span class="row__side">
            <span class="row__saved">${item.discount_cents ? "−" + money(item.discount_cents) : "—"}</span>
          </span>
        </label>`
        )
        .join("")}
      <div class="review__actions">
        <button class="btn btn--ghost" type="button" id="review-cancel">Discard</button>
        <button class="btn btn--primary" type="button" id="review-save" ${
          parsed.duplicate_of ? "disabled" : ""
        }>Log this receipt</button>
      </div>`;
    node.hidden = false;

    $$("[data-toggle]", node).forEach((box) => {
      box.addEventListener("change", () => {
        const wrapper = box.closest(".review__item");
        const index = Number(wrapper.dataset.index);
        state.pending.items[index].qualifying = box.checked;
        wrapper.classList.toggle("is-excluded", !box.checked);
        const total = state.pending.items
          .filter((i) => i.qualifying)
          .reduce((sum, i) => sum + i.discount_cents, 0);
        $(".review__total", node).innerHTML =
          `${money(total)}<span class="row__sub"> counts</span>`;
      });
    });

    $("#review-cancel").addEventListener("click", () => {
      state.pending = null;
      node.hidden = true;
    });
    $("#review-save").addEventListener("click", saveReview);
    node.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function saveReview() {
    const parsed = state.pending;
    if (!parsed) return;
    const button = $("#review-save");
    const error = $("#upload-error");
    showError(error, "");

    const dateField = $("#review-date");
    const purchasedOn = parsed.purchased_on || (dateField && dateField.value);
    if (!purchasedOn) {
      showError(error, "Pick the purchase date first.");
      return;
    }

    setBusy(button, true, "Logging…");
    try {
      const result = await api("/api/receipts", {
        method: "POST",
        json: {
          purchased_on: purchasedOn,
          purchased_at: parsed.purchased_at,
          receipt_no: parsed.receipt_no,
          merchant: parsed.merchant,
          subtotal_cents: parsed.subtotal_cents,
          tax_cents: parsed.tax_cents,
          tip_cents: parsed.tip_cents,
          total_cents: parsed.total_cents,
          filename: parsed.filename,
          file_sha256: parsed.file_sha256,
          items: parsed.items.map((item) => ({
            description: item.description,
            detail: item.detail,
            category: item.qualifying ? item.category : "other",
            serving: item.serving,
            reg_price_cents: item.reg_price_cents,
            discount_cents: item.qualifying ? item.discount_cents : 0,
            paid_cents: item.paid_cents,
            qualifying: item.qualifying,
          })),
        },
      });
      state.stats = result.stats;
      state.receipts = null;
      state.pending = null;
      $("#review").hidden = true;
      toast(`Receipt logged. ${money(result.stats.remaining_cents)} to breakeven.`);
      location.hash = "#/dashboard";
    } catch (err) {
      showError(error, err.message);
      setBusy(button, false);
    }
  }

  // ── search ───────────────────────────────────────────────────────────

  let searchTimer;

  async function initSearch() {
    if (!state.loaded.search) {
      state.loaded.search = true;
      await Promise.all([runSearch(), loadInsights()]);
    }
  }

  function scheduleSearch() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 220);
  }

  ["#search-q", "#search-from", "#search-to", "#search-category", "#search-sort",
   "#search-include-all"].forEach((sel) => {
    const node = $(sel);
    node.addEventListener(node.tagName === "SELECT" || node.type === "checkbox" ||
                          node.type === "date" ? "change" : "input", scheduleSearch);
  });

  async function runSearch() {
    const params = new URLSearchParams();
    const q = $("#search-q").value.trim();
    if (q) params.set("q", q);
    if ($("#search-from").value) params.set("date_from", $("#search-from").value);
    if ($("#search-to").value) params.set("date_to", $("#search-to").value);
    if ($("#search-category").value) params.set("category", $("#search-category").value);
    if ($("#search-include-all").checked) params.set("include_non_counting", "true");
    params.set("sort", $("#search-sort").value);

    const node = $("#search-results");
    try {
      const data = await api(`/api/search?${params.toString()}`);
      const summary = data.summary;
      $("#search-summary").textContent = summary.item_count
        ? `${plural(summary.item_count, "line")} · ${money(summary.saved_cents)} saved on ` +
          `${money(summary.pre_discount_cents)} of list price`
        : "";

      if (!data.items.length) {
        node.innerHTML = '<p class="empty">Nothing matches those filters.</p>';
        return;
      }
      node.innerHTML = data.items
        .map(
          (item) => `
          <div class="row">
            <div class="row__main">
              <div class="row__title">${esc(item.description)}</div>
              <div class="row__meta">
                <span>${esc(fmtDateLong(item.purchased_on))}</span>
                ${item.serving ? `<span class="pill">${esc(item.serving)}</span>` : ""}
                ${item.qualifying
                  ? `<span class="pill pill--${esc(item.category)}">${esc(item.category)}</span>`
                  : '<span class="pill pill--muted">not counted</span>'}
              </div>
            </div>
            <div class="row__side">
              <div class="row__saved">${item.qualifying ? money(item.discount_cents) : "—"}</div>
              <div class="row__sub">${money(item.reg_price_cents)} list</div>
            </div>
          </div>`
        )
        .join("");
    } catch (err) {
      if (err.status !== 401) node.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
    }
  }

  $$(".segmented__btn[data-period]").forEach((button) => {
    button.addEventListener("click", () => {
      $$(".segmented__btn[data-period]").forEach((b) => {
        const active = b === button;
        b.classList.toggle("is-active", active);
        b.setAttribute("aria-selected", String(active));
      });
      state.insightPeriod = button.dataset.period;
      loadInsights();
    });
  });

  async function loadInsights() {
    const node = $("#insights");
    try {
      const data = await api(`/api/insights?period=${encodeURIComponent(state.insightPeriod)}`);
      const totals = data.totals || {};

      if (!totals.item_count) {
        node.innerHTML = `<p class="empty">Nothing logged for ${esc(data.label)} yet.</p>`;
        return;
      }

      const rows = [];
      const push = (question, value, detail) =>
        rows.push(`
          <div class="insight">
            <div class="insight__q">${esc(question)}</div>
            <div class="insight__a">
              <div class="insight__value">${value}</div>
              ${detail ? `<div class="insight__detail">${detail}</div>` : ""}
            </div>
          </div>`);

      if (data.priciest_item) {
        const item = data.priciest_item;
        push(
          "Priciest thing we ordered",
          `${esc(item.description)} — ${money(item.reg_price_cents)}`,
          `${esc(fmtDateLong(item.purchased_on))} · paid ${money(item.paid_cents)} after the discount`
        );
      }
      if (data.biggest_saving_item) {
        const item = data.biggest_saving_item;
        push(
          "Biggest single saving",
          `${money(item.discount_cents)} on ${esc(item.description)}`,
          esc(fmtDateLong(item.purchased_on))
        );
      }
      if (data.biggest_visit) {
        const visit = data.biggest_visit;
        push(
          "Best visit",
          `${money(visit.saved_cents)} saved`,
          `${esc(fmtDateLong(visit.purchased_on))} · ${plural(visit.item_count, "item")}`
        );
      }
      if (data.most_ordered && data.most_ordered.length) {
        const top = data.most_ordered[0];
        push(
          "Ordered most often",
          `${esc(top.label)} — ${plural(top.times, "time")}`,
          `${money(top.saved_cents)} saved on it · last on ${esc(fmtDateLong(top.last_ordered))}`
        );
      }
      if (data.busiest_month) {
        const month = data.busiest_month;
        const when = parseDay(`${month.month}-01`);
        push(
          "Best month",
          when ? when.toLocaleDateString("en-US", { month: "long", year: "numeric" }) : month.month,
          `${money(month.saved_cents)} saved over ${plural(month.visit_count, "visit")}`
        );
      }
      push(
        `Totals for ${data.label}`,
        `${money(totals.saved_cents)} saved`,
        `${plural(totals.visit_count, "visit")} · ${money(totals.pre_discount_cents)} list price · ` +
          `${money(totals.paid_cents)} paid`
      );

      const repeats = (data.most_ordered || []).filter((row) => row.times > 1);
      const repeatTable = repeats.length
        ? `<div class="table-wrap" style="margin-top:14px">
             <table class="data">
               <thead><tr><th>Repeat orders</th><th>Times</th><th>Saved</th></tr></thead>
               <tbody>${repeats
                 .map(
                   (row) => `<tr><td>${esc(row.label)}</td><td>${row.times}</td>
                     <td>${money(row.saved_cents)}</td></tr>`
                 )
                 .join("")}</tbody>
             </table>
           </div>`
        : "";

      node.innerHTML = rows.join("") + repeatTable;
    } catch (err) {
      if (err.status !== 401) node.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
    }
  }

  // ── settings ─────────────────────────────────────────────────────────

  async function renderSettings() {
    const settings = await api("/api/settings");
    state.settings = settings;
    $("#set-fee").value = ((Number(settings.membership_fee_cents) || 0) / 100).toFixed(2);
    $("#set-tax").value = ((Number(settings.membership_tax_cents) || 0) / 100).toFixed(2);
    $("#set-start").value = settings.term_start || "";
    $("#set-end").value = settings.term_end || "";
    $("#set-discount").value = settings.discount_percent || 50;
    $("#set-name").value = settings.member_name || "";
    updateTargetPreview();
  }

  function updateTargetPreview() {
    const fee = parseAmount($("#set-fee").value) || 0;
    const tax = parseAmount($("#set-tax").value) || 0;
    $("#target-preview").innerHTML =
      `Breakeven target: <b>${money(Math.round((fee + tax) * 100))}</b> ` +
      `— the fee plus any tax you paid on it.`;
  }

  $("#set-fee").addEventListener("input", updateTargetPreview);
  $("#set-tax").addEventListener("input", updateTargetPreview);

  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    showError($("#settings-error"), "");
    $("#settings-ok").hidden = true;
    setBusy(button, true, "Saving…");
    try {
      const result = await api("/api/settings", {
        method: "PUT",
        json: {
          membership_fee: (parseAmount($("#set-fee").value) || 0).toFixed(2),
          membership_tax: (parseAmount($("#set-tax").value) || 0).toFixed(2),
          term_start: $("#set-start").value || null,
          term_end: $("#set-end").value || null,
          discount_percent: Number($("#set-discount").value),
          member_name: $("#set-name").value,
        },
      });
      state.settings = result.settings;
      state.stats = result.stats;
      const ok = $("#settings-ok");
      ok.textContent = "Saved.";
      ok.hidden = false;
      setTimeout(() => { ok.hidden = true; }, 2600);
    } catch (err) {
      showError($("#settings-error"), err.message);
    } finally {
      setBusy(button, false);
    }
  });

  $("#password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    showError($("#pw-error"), "");
    setBusy(button, true, "Changing…");
    try {
      await api("/api/password", {
        method: "POST",
        json: {
          current_password: $("#pw-current").value,
          new_password: $("#pw-new").value,
        },
      });
      state.user = null;
      showLogin("Password changed. Sign in with the new one.");
    } catch (err) {
      showError($("#pw-error"), err.message);
    } finally {
      setBusy(button, false);
    }
  });

  // ── boot ─────────────────────────────────────────────────────────────

  (async function boot() {
    try {
      const me = await api("/api/me");
      state.user = me.username;
      showApp();
      if (!location.hash) location.hash = "#/dashboard";
      await route();
    } catch (err) {
      showLogin(err.status === 401 ? "" : err.message);
    }
  })();
})();
