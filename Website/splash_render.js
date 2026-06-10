(function () {
  var IDENTITY_ORDER = [
    "user_type", "guid", "origin", "server_env", "created_at", "expires_at",
  ];
  var IDENTITY_HIDDEN = new Set(["token", "signature"]);

  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt !== undefined && txt !== null) e.textContent = String(txt);
    return e;
  }

  function fmtValue(v) {
    if (v === null || v === undefined) return "";
    if (typeof v === "object") {
      try { return JSON.stringify(v, null, 2); } catch (_) { return String(v); }
    }
    return String(v);
  }

  function renderIdentity(identity) {
    var section = el("section", "ch-splash__identity");
    section.appendChild(el("h3", "ch-splash__heading", "Identity"));
    var dl = el("dl", "ch-splash__id-list");
    var ordered = [];
    var seen = new Set();
    IDENTITY_ORDER.forEach(function (k) {
      if (k in identity) { ordered.push(k); seen.add(k); }
    });
    Object.keys(identity).forEach(function (k) {
      if (seen.has(k) || IDENTITY_HIDDEN.has(k)) return;
      ordered.push(k);
    });
    ordered.forEach(function (k) {
      dl.appendChild(el("dt", "ch-splash__id-key", k));
      dl.appendChild(el("dd", "ch-splash__id-val", fmtValue(identity[k])));
    });
    section.appendChild(dl);
    return section;
  }

  function renderRow(at, body, n) {
    var row = el("div", "ch-splash__row");
    var head = el("div", "ch-splash__row-at");
    if (n !== undefined && n !== null) {
      head.appendChild(el("span", "ch-splash__row-n", "#" + n + " "));
    }
    head.appendChild(document.createTextNode(at || ""));
    row.appendChild(head);
    var b = el("div", "ch-splash__row-body");
    if (body instanceof Node) b.appendChild(body);
    else b.textContent = body == null ? "" : String(body);
    row.appendChild(b);
    return row;
  }

  function renderThread(title, modifier, items, formatter) {
    var box = el("div", "ch-splash__thread ch-splash__thread--" + modifier);
    box.appendChild(el("div", "ch-splash__thread-title", title));
    var body = el("div", "ch-splash__thread-body");
    if (!items || items.length === 0) {
      body.appendChild(el("div", "ch-splash__thread-empty", "(no entries yet)"));
    } else {
      items.forEach(function (it) { body.appendChild(formatter(it)); });
    }
    box.appendChild(body);
    return box;
  }

  function fmtUtterance(it) {
    var b = el("span");
    var actor = it.actor || "?";
    b.appendChild(el("strong", "ch-splash__actor ch-splash__actor--" + actor, actor + ":"));
    b.appendChild(document.createTextNode(" " + (it.text || "")));
    return renderRow(it.at, b, it.n);
  }

  function fmtAction(it) {
    var b = el("span");
    b.appendChild(el("strong", null, it.tool_name || "?"));
    var inJson = it.input_json && Object.keys(it.input_json).length
      ? JSON.stringify(it.input_json) : "";
    var outJson = it.output_json && Object.keys(it.output_json).length
      ? JSON.stringify(it.output_json) : "";
    if (inJson) {
      if (inJson.length > 120) inJson = inJson.slice(0, 120) + "…";
      b.appendChild(document.createTextNode("(" + inJson + ")"));
    }
    if (outJson) {
      if (outJson.length > 120) outJson = outJson.slice(0, 120) + "…";
      b.appendChild(document.createTextNode(" → " + outJson));
    }
    return renderRow(it.at, b, it.n);
  }

  function renderThreads(threads) {
    var section = el("section", "ch-splash__history");
    section.appendChild(el("h3", "ch-splash__heading", "Session Conversation History"));
    if (!threads || threads.empty) {
      section.appendChild(el(
        "p", "ch-splash__empty",
        "No utterances or actions yet. Type a prompt or click around to see this grow."
      ));
      return section;
    }
    var grid = el("div", "ch-splash__threads");
    grid.appendChild(renderThread("Utterances", "utterances", threads.utterances, fmtUtterance));
    grid.appendChild(renderThread("Actions", "actions", threads.actions, fmtAction));
    section.appendChild(grid);
    return section;
  }

  function renderSplash(data, mountEl) {
    if (!mountEl) return;
    while (mountEl.firstChild) mountEl.removeChild(mountEl.firstChild);
    var root = el("div", "ch-splash");
    root.appendChild(el("h2", "ch-splash__title", "Shared Services — User Object"));
    var intro = el("p", "ch-splash__intro");
    intro.appendChild(document.createTextNode(
      "Live evidence that the entrance code completed. The cookie carries only the GUID; the user object lives in "
    ));
    intro.appendChild(el("code", null, "admin.Sessions"));
    intro.appendChild(document.createTextNode("."));
    root.appendChild(intro);
    root.appendChild(renderIdentity(data && data.identity || {}));
    root.appendChild(renderThreads(data && data.threads));
    mountEl.appendChild(root);
  }

  window.renderSplash = renderSplash;
})();
