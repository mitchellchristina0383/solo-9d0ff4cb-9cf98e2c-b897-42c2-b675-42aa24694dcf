/* TFTP 试跑台前端：轮询 JSON API 并渲染两路状态。无外部依赖。 */
(function () {
  "use strict";

  var LANE_NAMES = { A: "链路 A", B: "链路 B" };
  var STATUS_TEXT = {
    idle: "待开始", starting: "启动中", running: "下载中",
    done: "已完成", error: "失败"
  };

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function fmtElapsed(ms) {
    if (ms == null) return "—";
    if (ms < 1000) return ms + " ms";
    return (ms / 1000).toFixed(2) + " s";
  }

  var renderedEvents = { A: 0, B: 0 };
  var lastRunToken = { A: null, B: null };

  function laneModeFromUI(lane) {
    var checked = document.querySelector('input[name="mode-' + lane + '"]:checked');
    return checked ? checked.value : "normal";
  }

  function applyQuickMode() {
    var q = document.querySelector('input[name="quick-mode"]:checked').value;
    var map;
    if (q === "lose-first") map = { A: "lose-first", B: "lose-first" };
    else if (q === "normal") map = { A: "normal", B: "normal" };
    else map = { A: "lose-first", B: "normal" };
    ["A", "B"].forEach(function (id) {
      var radio = document.querySelector('input[name="mode-' + id + '"][value="' + map[id] + '"]');
      if (radio) radio.checked = true;
    });
    return map;
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json().then(function (j) { return { status: r.status, json: j }; }); });
  }

  function startBoth() {
    var modes = applyQuickMode();
    $("btn-start-both").disabled = true;
    postJSON("/api/both/start", { modes: modes }).then(function (res) {
      if (!res.json.ok) {
        $("both-hint").textContent = "启动被拒：" + (res.json.error || res.status);
        $("btn-start-both").disabled = false;
      } else {
        $("both-hint").textContent = "已同时发起（run " + res.json.run_token + "），正在真实 UDP 传输…";
      }
      setTimeout(refreshOnce, 120);
    }).catch(function (e) {
      $("both-hint").textContent = "请求失败：" + e;
      $("btn-start-both").disabled = false;
    });
  }

  function startLane(id, btn) {
    if (btn) btn.disabled = true;
    postJSON("/api/lane/" + id + "/start", { mode: laneModeFromUI(id) })
      .then(function () { setTimeout(refreshOnce, 100); });
  }

  function renderEvents(laneId, events) {
    var ul = $("log-" + laneId);
    if (!events || !events.length) return;
    if (renderedEvents[laneId] === 0) ul.innerHTML = "";
    for (var i = renderedEvents[laneId]; i < events.length; i++) {
      var ev = events[i];
      var li = document.createElement("li");
      li.className = "kind-" + esc(ev.kind || "info");
      li.innerHTML = '<span class="t">+' + esc(ev.t_ms) + 'ms</span>' + esc(ev.text);
      ul.appendChild(li);
    }
    renderedEvents[laneId] = events.length;
    ul.scrollTop = ul.scrollHeight;
  }

  function renderLane(id, lane) {
    var pill = $("status-" + id);
    pill.textContent = STATUS_TEXT[lane.status] || lane.status;
    pill.className = "status-pill " + lane.status;

    var r = lane.result || {};
    var running = lane.status === "starting" || lane.status === "running";
    $("elapsed-" + id).textContent = r.elapsed_ms != null ? fmtElapsed(r.elapsed_ms) : (running ? "…" : "—");
    $("bytes-" + id).textContent = r.bytes_received != null && lane.status !== "idle"
      ? r.bytes_received + " / 1024 B" : "—";
    $("term-" + id).textContent = r.terminator_acked ? "ACK 已发 ✓" : "—";
    $("retry-client-" + id).textContent =
      (r.retransmissions_seen != null && lane.status !== "idle") ? r.retransmissions_seen + " 次" : "—";

    var ss = lane.server_session;
    $("retry-server-" + id).textContent = ss
      ? ss.retransmit_count + " 次（超时 " + ss.timeout_retransmits +
        " / 重复RRQ " + ss.duplicate_rrq_resends + "）"
      : (lane.status === "done" || lane.status === "error" ? "未匹配" : "—");

    var badge = $("digest-badge-" + id);
    var actual = $("digest-actual-" + id);
    if (r.digest_actual) {
      actual.textContent = r.digest_actual;
      if (r.digest_match) {
        badge.textContent = "校验通过 · SHA-256 一致 ✓";
        badge.className = "badge ok";
      } else {
        badge.textContent = "校验失败 · 摘要不一致 ✗";
        badge.className = "badge bad";
      }
    } else if (lane.status === "error") {
      actual.textContent = r.error || "传输失败";
      badge.textContent = "校验失败 · 传输错误";
      badge.className = "badge bad";
    } else {
      actual.textContent = "—";
      badge.textContent = running ? "计算中…" : "未校验";
      badge.className = "badge";
    }

    var addrParts = [];
    if (r.client_addr) addrParts.push("终端 " + r.client_addr);
    if (r.server_addr) addrParts.push("服务端来源 " + r.server_addr);
    if (r.dropped_first_block) addrParts.push("已按脚本丢弃首次 block=1");
    if (r.error && lane.status === "error") addrParts.push("错误：" + r.error);
    $("addr-" + id).textContent = addrParts.join("　|　");

    if (lane.run_token && lane.run_token !== lastRunToken[id]) {
      lastRunToken[id] = lane.run_token;
      renderedEvents[id] = 0;
      $("log-" + id).innerHTML = '<li class="muted">运行已开始…</li>';
    }
    if (r.events) renderEvents(id, r.events);

    if (!running) $("btn-start-both").disabled = false;
  }

  function renderServer(server) {
    if (!server || server.reachable === false) {
      $("server-meta").textContent = "无法读取 TFTP 服务 stats：" +
        ((server && server.error) || "未知错误") + "（" + ((server && server.stats_url) || "") + "）";
      return;
    }
    var cfg = server.config || {};
    $("server-meta").textContent =
      "镜像 " + cfg.image_name + " / " + cfg.image_size + "B / block " + cfg.block_size +
      "B / 末块号 " + cfg.last_block_number + "；超时 " + cfg.timeout_seconds +
      "s，超时重试 " + cfg.max_retries + " 次；stats 正常。";

    var active = server.slots ? server.slots.active : 0;
    var max = server.slots ? server.slots.max : 2;
    $("slots-text").textContent = active + "/" + max;
    var dots = document.querySelectorAll(".slot-dot");
    dots.forEach(function (d, i) {
      d.className = "slot-dot " + (i < active ? "busy" : "free");
    });

    var body = $("sessions-body");
    var sessions = server.sessions || [];
    if (!sessions.length) {
      body.innerHTML = '<tr><td colspan="8" class="muted">暂无活动会话（名额全部空闲）</td></tr>';
    } else {
      body.innerHTML = sessions.map(function (s) {
        return "<tr>" +
          "<td><code>" + esc(s.client) + "</code></td>" +
          "<td>" + esc(s.tid_port) + "</td>" +
          '<td><span class="tag ' + esc(s.state) + '">' + esc(s.state) + "</span></td>" +
          "<td>" + esc(s.block) + " / " + esc(s.last_block) + "</td>" +
          "<td>" + (s.deadline_in_seconds != null ? s.deadline_in_seconds + " s" : "—") + "</td>" +
          "<td>" + esc(s.timeout_retransmits) + "</td>" +
          "<td>" + esc(s.duplicate_rrq_resends) + "（RRQ×" + esc(s.duplicate_rrq_count) + "）</td>" +
          "<td>" + esc(s.late_acks) + "</td>" +
          "</tr>";
      }).join("");
    }

    var t = server.totals || {};
    $("totals-line").textContent =
      "累计：发起 " + (t.started || 0) + "，完成 " + (t.completed || 0) +
      "，超时释放 " + (t.timed_out || 0) + "，客户端中止 " + (t.aborted || 0) +
      "，满载拒绝 " + (t.rejected_full || 0) +
      (server.last_reject ? "；最近拒绝 " + server.last_reject.client + " @ " + server.last_reject.at : "");
  }

  function refreshOnce() {
    return fetch("/api/state").then(function (r) { return r.json(); }).then(function (data) {
      if (data.lanes) {
        ["A", "B"].forEach(function (id) {
          if (data.lanes[id]) renderLane(id, data.lanes[id]);
        });
      }
      renderServer(data.server);
    }).catch(function () { /* 下一轮重试 */ });
  }

  // ------------------------------------------------------------ 满载自检
  var fulltestTimer = null;
  function startFullTest() {
    $("btn-fulltest").disabled = true;
    $("fulltest-summary").textContent = "自检运行中，预计数秒…";
    $("fulltest-log").innerHTML = "";
    postJSON("/api/full-test/start", {}).then(function (res) {
      if (!res.json.ok) {
        $("fulltest-summary").textContent = res.json.error || "无法启动";
        $("btn-fulltest").disabled = false;
        return;
      }
      if (fulltestTimer) clearInterval(fulltestTimer);
      fulltestTimer = setInterval(pollFullTest, 700);
      pollFullTest();
    });
  }

  var shownFullLogs = 0;
  function pollFullTest() {
    fetch("/api/full-test/state").then(function (r) { return r.json(); }).then(function (st) {
      var ul = $("fulltest-log");
      (st.log || []).slice(shownFullLogs).forEach(function (row) {
        var li = document.createElement("li");
        li.innerHTML = '<span class="t">[' + esc(row.t) + ']</span>' + esc(row.name) + " — " + esc(row.detail);
        ul.appendChild(li);
      });
      shownFullLogs = (st.log || []).length;
      if (st.status === "done" || st.status === "error") {
        clearInterval(fulltestTimer);
        $("btn-fulltest").disabled = false;
        var rep = st.report || {};
        $("fulltest-summary").innerHTML = rep.ok
          ? '<span style="color:var(--ok);font-weight:700">自检全部通过 ✓</span>'
          : '<span style="color:var(--bad);font-weight:700">自检存在未通过项 ✗（见下方流水）</span>';
        if (rep.fatal) $("fulltest-summary").textContent += " 异常：" + rep.fatal;
        refreshOnce();
      }
    });
  }

  // ------------------------------------------------------------ 绑定
  $("btn-start-both").addEventListener("click", startBoth);
  $("btn-fulltest").addEventListener("click", function () { shownFullLogs = 0; startFullTest(); });
  $("btn-refresh-stats").addEventListener("click", refreshOnce);
  document.querySelectorAll("button[data-lane]").forEach(function (btn) {
    btn.addEventListener("click", function () { startLane(btn.getAttribute("data-lane"), btn); });
  });

  applyQuickMode();
  refreshOnce();
  setInterval(refreshOnce, 800);
})();
