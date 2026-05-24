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

  function renderRow(at, body) {
    var row = el("div", "ch-splash__row");
    row.appendChild(el("div", "ch-splash__row-at", at));
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

  function fmtPerson(it) {
    return renderRow(it.at, '"' + (it.text || "") + '"');
  }

  function fmtPersonToSystem(it) {
    var b = el("span");
    b.appendChild(el("strong", null, it.event_type || "?"));
    if (it.value !== null && it.value !== undefined && it.value !== "") {
      var tail = typeof it.value === "object" ? JSON.stringify(it.value) : String(it.value);
      if (tail.length > 90) tail = tail.slice(0, 90);
      b.appendChild(document.createTextNode(" · " + tail));
    }
    return renderRow(it.at, b);
  }

  function fmtMachine(it) {
    if (it.kind === "tool_result") {
      var b = el("span");
      b.appendChild(document.createTextNode("tool_result from "));
      b.appendChild(el("strong", null, it.tool_name || "?"));
      if (it.tool_result !== null && it.tool_result !== undefined) {
        var tail = JSON.stringify(it.tool_result);
        if (tail.length > 120) tail = tail.slice(0, 120);
        b.appendChild(document.createTextNode(" → " + tail));
      }
      return renderRow(it.at, b);
    }
    return renderRow(it.at, it.text || "(empty pedantic response)");
  }

  function fmtLlmToSystem(it) {
    var b = el("span");
    if (it.kind === "llm_clarification") {
      b.appendChild(el("strong", null, "→ Person:"));
      var text = " " + (it.llm_response || "");
      if (Array.isArray(it.info_sought) && it.info_sought.length) {
        text += " · seeking: " + it.info_sought.join(", ");
      }
      b.appendChild(document.createTextNode(text));
    } else if (it.kind === "tool_invocation") {
      b.appendChild(el("strong", null, "→ Machine:"));
      b.appendChild(document.createTextNode(" invoke "));
      b.appendChild(el("strong", null, it.tool_name || "?"));
      if (it.tool_args !== null && it.tool_args !== undefined) {
        var args = JSON.stringify(it.tool_args);
        if (args.length > 120) args = args.slice(0, 120);
        b.appendChild(document.createTextNode("(" + args + ")"));
      }
    } else {
      b.textContent = it.kind || "";
    }
    return renderRow(it.at, b);
  }

  function renderThreads(threads) {
    var section = el("section", "ch-splash__history");
    section.appendChild(el("h3", "ch-splash__heading", "Session Conversation History"));
    if (!threads || threads.empty) {
      section.appendChild(el(
        "p", "ch-splash__empty",
        "No UX events or utterances yet. Click around or type a prompt to see this grow."
      ));
      return section;
    }
    var grid = el("div", "ch-splash__threads");
    grid.appendChild(renderThread("Person", "person", threads.person, fmtPerson));
    grid.appendChild(renderThread("Person → System", "p2s", threads.person_to_system, fmtPersonToSystem));
    grid.appendChild(renderThread("Machine", "machine", threads.machine, fmtMachine));
    grid.appendChild(renderThread("LLM → System", "llm2s", threads.llm_to_system, fmtLlmToSystem));
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
