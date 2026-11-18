(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let templates = [];
  let batches = [];
  let reports = [];
  let gaps = null;
  let selected = null;
  let stampValue = "";
  let traceValue = "WM-ORD-131";
  let traceResult = null;
  let gapDay = "2026-11-16";
  let scope = "仅缺口";
  let taskDetail = null;
  let lastTaskId = null;
  let tasks = [];

  const SCOPE_NOTES = {
    仅缺口: "只给仍缺印的行补新印；已盖印行的水印原样保留，不重盖。",
    整批重落: "先整笔回滚本批印，再全量重落；已盖印行的水印会被替换。",
  };

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(j.error || ("HTTP " + r.status));
      err.payload = j;
      throw err;
    }
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function refresh() {
    const meta = await api("/api/meta");
    document.getElementById("lab-name").textContent = meta.lab_name || "血缘水印台";
    templates = (await api("/api/templates")).items || [];
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/gap-reports")).items || [];
    gaps = await api("/api/gaps?as_of_date=" + encodeURIComponent(gapDay));
    tasks = (await api("/api/backfills")).items || [];
    if (lastTaskId) {
      taskDetail = await api("/api/backfills/" + lastTaskId).catch(() => null);
    }
  }

  function syncDock() {
    document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.tab === tab);
    });
  }

  function chip(text, kind) {
    return `<span class="chip ${kind || ""}">${esc(text)}</span>`;
  }

  function renderTemplates() {
    return `
      <h2>水印模板</h2>
      <table>
        <thead><tr><th>表</th><th>列</th><th>版本</th><th>状态</th></tr></thead>
        <tbody>
          ${templates
            .map(
              (t) => `<tr>
            <td>${esc(t.table_name)}</td>
            <td>${esc(t.column_name)}</td>
            <td>${esc(t.version_no)}</td>
            <td>${chip(t.status)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function batchCards() {
    return `
      <div class="cards">
        ${batches
          .map(
            (b) => `<div class="cardish batch-card">
          <div class="row" style="justify-content:space-between;margin:0">
            <strong>${esc(b.run_code)}</strong>
            ${chip(b.status, b.status === "已盖印" ? "ok" : "warn")}
          </div>
          <p class="muted" style="margin:0.4rem 0">${esc(b.table_name)} · ${esc(b.biz_date)}</p>
          <p style="margin:0">水印：<code>${esc(b.watermark_value || "未盖印")}</code></p>
          ${
            b.status === "待盖印"
              ? `<div class="row"><button type="button" class="ghost" data-pick="${b.id}">盖印</button></div>`
              : ""
          }
        </div>`
          )
          .join("")}
      </div>`;
  }

  function renderBatches() {
    return `
      <h2>运行批次</h2>
      <p class="muted">主链：选批盖印 → 用水印追索下游 → 缺口对账 / 补盖</p>
      ${batchCards()}
      ${
        selected
          ? `<div class="cardish">
        <h3>盖印</h3>
        <p>${esc(selected.run_code)} · ${esc(selected.table_name)}</p>
        <label>水印值<input id="stamp-input" value="${esc(stampValue)}" /></label>
        <button type="button" id="btn-stamp">写入盖印</button>
      </div>`
          : ""
      }`;
  }

  function renderTrace() {
    const src = (traceResult && traceResult.sources) || [];
    const down = (traceResult && traceResult.downstream) || [];
    return `
      <h2>下游追索</h2>
      <label>水印值<input id="trace-input" value="${esc(traceValue)}" placeholder="例如 WM-ORD-131" /></label>
      <div class="row"><button type="button" id="btn-trace">追索</button></div>
      ${
        traceResult
          ? `<div>
        <h3>源批次</h3>
        <ul>${src.map((s) => `<li>${esc(s.run_code)} · ${esc(s.table_name)}</li>`).join("")}</ul>
        <h3>下游</h3>
        <ul>${down
          .map(
            (d) =>
              `<li>${esc(d.run_code)} · ${esc(d.status)} · ${esc(d.watermark_value || "未盖印")}</li>`
          )
          .join("")}</ul>
      </div>`
          : ""
      }`;
  }

  function renderGapRows() {
    if (!gaps) return "";
    return `
      <table>
        <thead><tr><th>编号</th><th>运行码</th><th>表</th><th>业务日</th><th>状态</th><th>水印</th><th>标记</th></tr></thead>
        <tbody>
          ${(gaps.rows || [])
            .map(
              (r) => `<tr class="${r.is_gap ? "gap-row" : ""}">
            <td>${esc(r.id)}</td>
            <td>${esc(r.run_code)}</td>
            <td>${esc(r.table_name)}</td>
            <td>${esc(r.biz_date)}</td>
            <td>${chip(r.status, r.is_gap ? "warn" : "ok")}</td>
            <td><code>${esc(r.watermark_value || "未盖印")}</code></td>
            <td>${r.is_gap ? chip("缺口", "warn") : chip("已盖", "ok")}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderScopePicker() {
    return `
      <div class="row scope-picker">
        ${Object.keys(SCOPE_NOTES)
          .map(
            (s) => `<label class="scope-option ${scope === s ? "on" : ""}">
          <input type="radio" name="scope" value="${esc(s)}" ${scope === s ? "checked" : ""} />
          <span>${esc(s)}</span>
          <small class="muted">${esc(SCOPE_NOTES[s])}</small>
        </label>`
          )
          .join("")}
      </div>
      <div class="row">
        ${chip("范围：" + scope, "scope")}
        <button type="button" id="btn-backfill">按范围补盖</button>
      </div>`;
  }

  function renderDetail() {
    if (!taskDetail) return "";
    const t = taskDetail.task || {};
    const rounds = taskDetail.rounds || [];
    return `
      <div class="cardish" id="backfill-detail">
        <h3>补盖详情 #${esc(t.id)} ${chip(taskDetail.scope_chip || ("范围：" + t.scope), "scope")}</h3>
        <p class="muted" style="margin-top:0">${esc(t.message || "")}</p>
        <div class="row">
          ${chip("状态：" + (t.status || "-"), t.status === "成功" ? "ok" : "warn")}
          ${chip("应盖 " + (t.expected_count || 0))}
          ${chip("补盖前已盖 " + (t.stamped_before || 0))}
          ${chip("补盖前缺口 " + (t.gap_before || 0))}
          ${chip("新盖 " + (t.new_count || 0), "ok")}
          ${chip("保留 " + (t.preserved_count || 0))}
          ${chip("替换 " + (t.replaced_count || 0), t.replaced_count ? "warn" : "")}
          ${chip("跳过 " + (t.skipped_count || 0))}
        </div>
        ${
          (t.detail || []).length
            ? `<h4>行级命运</h4>
          <table>
            <thead><tr><th>运行码</th><th>表</th><th>命运</th><th>补前水印</th><th>补后水印</th></tr></thead>
            <tbody>
              ${(t.detail || [])
                .map(
                  (d) => `<tr>
                <td>${esc(d.run_code)}</td>
                <td>${esc(d.table_name)}</td>
                <td>${chip(d.fate, d.fate === "替换" ? "warn" : d.fate === "新盖" ? "ok" : "")}</td>
                <td><code>${esc(d.before_watermark || "未盖印")}</code></td>
                <td><code>${esc(d.after_watermark || "未盖印")}</code></td>
              </tr>`
                )
                .join("")}
            </tbody>
          </table>`
            : ""
        }
        ${
          rounds.length
            ? `<h4>回放进度</h4>
          <div class="rounds">
            ${rounds
              .map(
                (r) => `<div class="cardish round ${r.aligned ? "aligned" : "drift"}">
              <div class="row" style="justify-content:space-between;margin:0">
                <strong>第 ${esc(r.round_no)} 轮</strong>
                ${chip(r.scope_chip, "scope")}
                ${chip(r.aligned ? "数字对齐" : "未对齐", r.aligned ? "ok" : "warn")}
              </div>
              <div class="row">
                ${chip("命运", "fate")}
                ${(r.fate_chips || []).map((c) => chip(c, "fate")).join("")}
                ${chip("应盖 " + r.total_count)}
              </div>
              <p class="muted" style="margin:0">${esc(r.message)}</p>
            </div>`
              )
              .join("")}
          </div>
          <div class="row"><button type="button" id="btn-replay">回放两轮</button></div>`
            : `<div class="row"><button type="button" id="btn-replay">回放两轮</button></div>`
        }
      </div>`;
  }

  function renderTaskHistory() {
    if (!tasks.length) return "";
    return `
      <h3>补盖任务（本机库）</h3>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>范围</th><th>状态</th><th>新盖</th><th>保留</th><th>替换</th><th></th></tr></thead>
        <tbody>
          ${tasks
            .map(
              (t) => `<tr>
            <td>${esc(t.id)}</td>
            <td>${esc(t.as_of_date)}</td>
            <td>${chip("范围：" + t.scope, "scope")}</td>
            <td>${chip(t.status, t.status === "成功" ? "ok" : "warn")}</td>
            <td>${esc(t.new_count)}</td>
            <td>${esc(t.preserved_count)}</td>
            <td>${esc(t.replaced_count)}</td>
            <td><button type="button" class="ghost" data-task="${t.id}">看详情</button></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderGaps() {
    return `
      <h2>缺口对账与补盖</h2>
      <label>截至日期<input id="gap-date" value="${esc(gapDay)}" /></label>
      <div class="row">
        <button type="button" id="btn-gap">生成对账</button>
        <button type="button" class="ghost" id="btn-refresh-gap">刷新缺口</button>
      </div>
      ${
        gaps
          ? `<div class="row">
        ${chip("应盖印 " + gaps.expected_count)}
        ${chip("已盖印 " + gaps.stamped_count, "ok")}
        ${chip("缺口 " + gaps.gap_count, gaps.gap_count ? "warn" : "ok")}
      </div>
      <p class="muted">${esc(gaps.summary)}</p>
      ${renderScopePicker()}
      ${renderGapRows()}
      ${renderDetail()}
      ${renderTaskHistory()}`
          : ""
      }
      <h3>对账历史</h3>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>缺口</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${esc(r.id)}</td>
            <td>${esc(r.as_of_date)}</td>
            <td>${esc(r.gap_count)}</td>
            <td>${esc(r.summary)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "templates") view.innerHTML = renderTemplates();
    else if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "trace") view.innerHTML = renderTrace();
    else view.innerHTML = renderGaps();

    view.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.pick);
        selected = batches.find((b) => b.id === id) || null;
        stampValue = selected ? "WM-" + selected.run_code.toUpperCase() : "";
        render();
      };
    });
    const stampInput = document.getElementById("stamp-input");
    if (stampInput) {
      stampInput.oninput = () => (stampValue = stampInput.value);
      document.getElementById("btn-stamp").onclick = async () => {
        setMsg("");
        try {
          await api("/api/batches/" + selected.id + "/stamp", {
            method: "POST",
            body: JSON.stringify({ watermark_value: stampValue }),
          });
          selected = null;
          await refresh();
          setMsg("盖印完成");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const traceInput = document.getElementById("trace-input");
    if (traceInput) {
      traceInput.oninput = () => (traceValue = traceInput.value);
      document.getElementById("btn-trace").onclick = async () => {
        setMsg("");
        try {
          traceResult = await api("/api/trace", {
            method: "POST",
            body: JSON.stringify({ watermark_value: traceValue }),
          });
          setMsg("追索完成");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const gapBtn = document.getElementById("btn-gap");
    if (gapBtn) {
      gapBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/gap-reports", {
            method: "POST",
            body: JSON.stringify({ as_of_date: gapDay }),
          });
          await refresh();
          setMsg(r.report.summary);
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const gapDate = document.getElementById("gap-date");
    if (gapDate) {
      gapDate.oninput = () => (gapDay = gapDate.value);
    }
    const refreshGap = document.getElementById("btn-refresh-gap");
    if (refreshGap) {
      refreshGap.onclick = async () => {
        setMsg("");
        try {
          gaps = await api("/api/gaps?as_of_date=" + encodeURIComponent(gapDay));
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    view.querySelectorAll(".scope-option input").forEach((el) => {
      el.onchange = () => {
        scope = el.value;
        render();
      };
    });
    const backfillBtn = document.getElementById("btn-backfill");
    if (backfillBtn) {
      backfillBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/backfills", {
            method: "POST",
            body: JSON.stringify({ as_of_date: gapDay, scope }),
          });
          lastTaskId = r.task.id;
          taskDetail = r;
          await refresh();
          setMsg(r.task.message);
          render();
        } catch (e) {
          setMsg(e.message);
          // 补盖失败也把任务照亮：范围芯片 + 失败原因。
          const failedId = e.payload && e.payload.task_id;
          if (failedId) {
            lastTaskId = failedId;
            taskDetail = await api("/api/backfills/" + failedId).catch(() => null);
            render();
          }
        }
      };
    }
    view.querySelectorAll("[data-task]").forEach((btn) => {
      btn.onclick = async () => {
        setMsg("");
        try {
          lastTaskId = Number(btn.dataset.task);
          taskDetail = await api("/api/backfills/" + lastTaskId);
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    });
    const replayBtn = document.getElementById("btn-replay");
    if (replayBtn && lastTaskId) {
      replayBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/backfills/" + lastTaskId + "/replay", {
            method: "POST",
            body: JSON.stringify({ rounds: 2 }),
          });
          taskDetail = r;
          await refresh();
          setMsg("回放完成，两轮数字与范围芯片已对齐");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
  }

  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncDock();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(() => {
      syncDock();
      render();
    })
    .catch((e) => setMsg(e.message));
})();
