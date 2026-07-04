/* Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
   Licensed under the FindCare Evaluation License (FEL-1.0).

   ClientRouter.js — the one and only client-side router.

   Contract (per brain/BusinessArtifacts/architecture/FrontEnd/FrontEndArchitecture.pptx):

   render({target, append, popup, content})
     target  : string — name of a frame (Header, Footer, LeftPanel,
               RightPanel, UserMessage, UserPromptAndControl, MainWindow)
               OR the name of a popup container (e.g. AboutChatHealtyPopUP).
     append  : boolean — true: append content; false: replace.
     popup   : boolean — true: wrap content in a non-modal popup keyed by target.
     content : string — HTML fragment to inject.

   getStreamedPayloads({op, payload, onEvent, onFinal, onError})
     Posts to SharedServices /gate as op + payload, reads the NDJSON
     stream, dispatches every {kind, data} envelope to subscribers and
     to the caller's onEvent. Calls onFinal once when {kind:'final'}
     arrives. Calls onError on any failure. Use for ops where the server
     emits multiple payloads over time (utterances, tool dispatch).

   getFullPayload({op, payload})
     Posts to SharedServices /gate as op + payload, reads a single JSON
     response body, returns it as a Promise. Use for ops where the
     server returns one complete payload (peer_urls, peer_health,
     session, verify_token, transfer_to_findcare).

   subscribe(kind, handler)
     Subscribes a handler to every stream event with this kind. Used by
     React widgets to react to tool output without each subscribing to
     /gate independently. Returns an unsubscribe function.

   The wrapper exposes window.ClientRouter as the single API surface.
   Cross-iframe callers (React widget in the chat iframe) post a
   message of type 'router:render' or 'router:makeCall' to the wrapper;
   ClientRouter receives the postMessage and acts on it.
*/
(function () {

  function _getEnvServiceUrls() {
    return (window._envServiceUrls && window._envServiceUrls.sharedservices)
      ? window._envServiceUrls
      : { sharedservices: 'https://localhost:8002' };
  }

  function _sharedGateUrl() {
    var urls = _getEnvServiceUrls();
    return urls.sharedservices;
  }

  function _frameElement(target) {
    var el = document.getElementById('frame_' + target);
    return el;
  }

  function _popupOverlay(target) {
    var existing = document.getElementById('popup_' + target);
    if (existing) return existing;
    var overlay = document.createElement('div');
    overlay.id = 'popup_' + target;
    overlay.className = 'ch-popup-overlay';
    overlay.style.cssText = [
      'display:block', 'position:fixed',
      'top:50%', 'left:50%', 'transform:translate(-50%,-50%)',
      'min-width:32em', 'max-width:60em', 'max-height:80vh',
      'overflow:auto', 'z-index:1000', 'background:#fff',
      'border:0.25em solid #0b7a75',
      'box-shadow:0 0.5em 2em rgba(0,0,0,0.25)',
      'border-radius:0.5em', 'padding:0',
    ].join(';');
    var close = document.createElement('a');
    close.href = '#';
    close.textContent = '×';
    close.title = 'Close';
    close.setAttribute('aria-label', 'Close');
    close.style.cssText = [
      'position:absolute', 'top:0.25em', 'right:0.5em',
      'font-size:1.75em', 'line-height:1', 'font-weight:700',
      'color:#0b7a75', 'text-decoration:none', 'cursor:pointer',
      'padding:0.1em 0.4em', 'border-radius:0.3em',
    ].join(';');
    close.addEventListener('mouseover', function () { close.style.background = '#f0fffe'; });
    close.addEventListener('mouseout',  function () { close.style.background = 'transparent'; });
    close.addEventListener('click', function (ev) {
      ev.preventDefault();
      overlay.parentNode && overlay.parentNode.removeChild(overlay);
    });
    overlay.appendChild(close);
    var body = document.createElement('div');
    body.className = 'ch-popup-body';
    body.style.cssText = 'padding:1em';
    overlay.appendChild(body);
    document.body.appendChild(overlay);
    return overlay;
  }

  function _popupBody(target) {
    var overlay = _popupOverlay(target);
    return overlay.querySelector('.ch-popup-body');
  }

  var _subscribers = {};

  // Full signed SessionToken (wire object). Carried across every /gate
  // call so SS validates and hydrates the same user_object each time.
  // Replaces the earlier bearer-only GUID pattern: /gate now verifies
  // the signature on every non-trivial op, not just the tail-32 lookup.
  var _sessionToken = null;

  function _captureSessionToken(evt) {
    if (!evt || typeof evt !== 'object') return;
    var st = evt.session_token;
    if (!st || typeof st !== 'object') return;
    if (typeof st.token !== 'string' || st.token.length < 32) return;
    _sessionToken = st;
  }

  // bootstrap — fetch a freshly-minted SessionToken from SS's /auth/issue
  // endpoint. Called by the wrapper on page load so ClientRouter holds a
  // valid signed token before the first non-trivial /gate call fires.
  // /auth/issue itself is un-authenticated; every downstream /gate call
  // is validated using the token this returns.
  function bootstrap() {
    var url = _sharedGateUrl() + '/auth/issue';
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }).then(function (resp) {
      if (!resp.ok) {
        throw new Error('ClientRouter.bootstrap: /auth/issue failed HTTP ' + resp.status);
      }
      return resp.json();
    }).then(function (st) {
      _sessionToken = st;
      return st;
    });
  }

  function _bindActions(root) {
    if (!root || !root.querySelectorAll) return;
    var nodes = root.querySelectorAll('[data-router-action]');
    for (var i = 0; i < nodes.length; i++) {
      (function (el) {
        var action = el.getAttribute('data-router-action');
        var tag = (el.tagName || '').toLowerCase();
        var listen = (tag === 'form') ? 'submit'
          : (tag === 'input' || tag === 'select' || tag === 'textarea') ? 'change'
          : 'click';
        el.addEventListener(listen, function (ev) {
          ev.preventDefault();
          var data = {};
          var attrs = el.attributes;
          for (var j = 0; j < attrs.length; j++) {
            var a = attrs[j];
            if (a.name.indexOf('data-') === 0 && a.name !== 'data-router-action') {
              // Convert hyphens to underscores so widgets read keys
              // verbatim — e.g. data-trial-idx is accessible as
              // msg.data.trial_idx (HTML attribute names are
              // hyphenated; JS object property convention is _).
              data[a.name.substring(5).replace(/-/g, '_')] = a.value;
            }
          }
          if (tag === 'form') {
            var inputs = el.querySelectorAll('input[name], select[name], textarea[name]');
            for (var k = 0; k < inputs.length; k++) {
              data[inputs[k].name] = inputs[k].value;
            }
          } else if (tag === 'input' || tag === 'select' || tag === 'textarea') {
            data.value = el.value;
          }
          var iframe = document.querySelector('iframe[data-frame="MainWindow"]');
          if (iframe && iframe.contentWindow) {
            iframe.contentWindow.postMessage({
              type: 'router:action',
              action: action,
              data: data,
            }, '*');
          }
        });
      })(nodes[i]);
    }
    // Drag sources: any draggable element with data-drag-payload writes
    // its payload to dataTransfer on dragstart. The payload is whatever
    // the widget set (typically an NPI or other id the drop target needs).
    var dragSrc = root.querySelectorAll('[draggable="true"][data-drag-payload]');
    for (var s = 0; s < dragSrc.length; s++) {
      (function (el) {
        el.addEventListener('dragstart', function (ev) {
          ev.dataTransfer.setData('text/plain', el.getAttribute('data-drag-payload') || '');
          ev.dataTransfer.effectAllowed = 'move';
          el.style.opacity = '0.5';
        });
        el.addEventListener('dragend', function () { el.style.opacity = ''; });
      })(dragSrc[s]);
    }
    // Drop targets: any element with data-router-drop-action accepts
    // drops and fires a router:action with the dropped payload at data.dropped.
    // Receiving widget then invokes getStreamedPayloads to mutate state server-side.
    var dropTgt = root.querySelectorAll('[data-router-drop-action]');
    for (var d = 0; d < dropTgt.length; d++) {
      (function (el) {
        var action = el.getAttribute('data-router-drop-action');
        el.addEventListener('dragover', function (ev) {
          ev.preventDefault();
          ev.dataTransfer.dropEffect = 'move';
        });
        el.addEventListener('drop', function (ev) {
          ev.preventDefault();
          var dropped = ev.dataTransfer.getData('text/plain');
          if (!dropped) return;
          var iframe = document.querySelector('iframe[data-frame="MainWindow"]');
          if (iframe && iframe.contentWindow) {
            iframe.contentWindow.postMessage({
              type: 'router:action',
              action: action,
              data: { dropped: dropped },
            }, '*');
          }
        });
      })(dropTgt[d]);
    }
  }

  function render(args) {
    var target = args && args.target;
    if (!target) return;
    var content = (args && args.content) || '';
    var append = !!(args && args.append);
    var popup = !!(args && args.popup);
    var sink = popup ? _popupBody(target) : _frameElement(target);
    if (!sink) return;
    if (append) {
      var tmp = document.createElement('div');
      tmp.innerHTML = content;
      while (tmp.firstChild) sink.appendChild(tmp.firstChild);
    } else {
      sink.innerHTML = content;
    }
    _bindActions(sink);
  }

  // merge — fill a named region inside a frame's scaffold without
  // disturbing the rest of the frame. The scaffold (painted by render)
  // defines the region ids; merge replaces the innerHTML of the element
  // whose id matches `region`. No-op when the region element is absent
  // (scaffold not yet painted) so widgets can subscribe defensively.
  function merge(args) {
    var target = args && args.target;
    var region = args && args.region;
    if (!target || !region) return;
    var content = (args && args.content) || '';
    var frame = _frameElement(target);
    if (!frame) return;
    var sink = frame.querySelector('#' + region);
    if (!sink) return;
    sink.innerHTML = content;
    _bindActions(sink);
  }

  function _dispatchEvent(evt, caller) {
    if (!evt || typeof evt !== 'object') return;
    // Capture the freshly-restamped SessionToken from any stream event
    // that carries one, so the next /gate call sends the current signed
    // token (with an advanced nonce) rather than a stale copy.
    _captureSessionToken(evt);
    var kind = evt.kind || '';
    var handlers = _subscribers[kind] || [];
    for (var i = 0; i < handlers.length; i++) {
      try { handlers[i](evt.data || {}, evt); } catch (_) {}
    }
    if (caller && typeof caller.onEvent === 'function') {
      try { caller.onEvent(evt); } catch (_) {}
    }
  }

  function getStreamedPayloads(args) {
    var op = args && args.op;
    if (!op) return Promise.reject(new Error('getStreamedPayloads: op is required'));
    var payload = (args && args.payload) || {};
    var url = _sharedGateUrl() + '/gate';
    // Thread the session GUID so SS reloads the same user_object on
    // every call instead of minting a fresh one (which would lose the
    // session_conversation_history that powers the SharedServices
    // splash + UM transcript window).
    var body = { op: op, payload: payload };
    if (_sessionToken) body.session_token = _sessionToken;
    return fetch(url, {
      method: 'POST',
      // No credentials:'include' on the fetch. HF Spaces' edge proxy
      // strips Access-Control-Allow-Credentials from the OPTIONS
      // preflight response, which blocks credentialed cross-origin
      // requests entirely. We thread the signed session token via the
      // body-level `session_token` field instead — SS verifies it at
      // app.py /gate before dispatching any non-trivial op. No cookies
      // anywhere in the architecture.
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/x-ndjson',
      },
      body: JSON.stringify(body),
    }).then(function (resp) {
      if (!resp.ok || !resp.body) {
        var err = new Error('ClientRouter.getStreamedPayloads: gate failed HTTP ' + resp.status + ' for op=' + op);
        if (args && typeof args.onError === 'function') args.onError(err);
        throw err;
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) {
            if (buffer.trim()) {
              try { _dispatchEvent(JSON.parse(buffer.trim()), args); } catch (_) {}
            }
            if (args && typeof args.onFinal === 'function') args.onFinal();
            return;
          }
          buffer += decoder.decode(r.value, { stream: true });
          var nl;
          while ((nl = buffer.indexOf('\n')) >= 0) {
            var line = buffer.substring(0, nl).trim();
            buffer = buffer.substring(nl + 1);
            if (!line) continue;
            try { _dispatchEvent(JSON.parse(line), args); }
            catch (_) {}
          }
          return pump();
        });
      }
      return pump();
    }).catch(function (err) {
      if (args && typeof args.onError === 'function') args.onError(err);
      throw err;
    });
  }

  // getFullPayload — for /gate ops in _TRIVIAL_GATE_OPS (peer_urls, peer_health,
  // session, verify_token, transfer_to_findcare) that return plain JSON
  // instead of NDJSON. Same credentials discipline as getStreamedPayloads so the
  // session cookie threads through. Returns a Promise of the parsed JSON
  // body. Use this for the bootstrap peer_urls fetch and any other
  // request/response op that does not need streaming.
  function getFullPayload(args) {
    var op = args && args.op;
    if (!op) return Promise.reject(new Error('getFullPayload: op is required'));
    var payload = (args && args.payload) || {};
    var url = _sharedGateUrl() + '/gate';
    var body = { op: op, payload: payload };
    // Trivial ops are pre-authentication utilities (peer_urls,
    // peer_health, etc.) and do not require the signed token. Include
    // it when we already hold one so downstream logging/audit can see
    // the caller's session, but never require it here.
    if (_sessionToken) body.session_token = _sessionToken;
    return fetch(url, {
      method: 'POST',
      // No credentials:'include' for the same reason as getStreamedPayloads —
      // see comment there. Trivial ops carry the token when we have
      // one but do not require it.
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (resp) {
      if (!resp.ok) {
        throw new Error('ClientRouter.getFullPayload: gate failed HTTP ' + resp.status + ' for op=' + op);
      }
      return resp.json();
    });
  }

  function subscribe(kind, handler) {
    if (!kind || typeof handler !== 'function') return function () {};
    if (!_subscribers[kind]) _subscribers[kind] = [];
    _subscribers[kind].push(handler);
    return function () {
      var arr = _subscribers[kind] || [];
      var idx = arr.indexOf(handler);
      if (idx >= 0) arr.splice(idx, 1);
    };
  }

  window.addEventListener('message', function (event) {
    var msg = event.data;
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'router:render') {
      render({
        target: msg.target,
        append: msg.append,
        popup: msg.popup,
        content: msg.content,
      });
    } else if (msg.type === 'router:merge') {
      merge({
        target: msg.target,
        region: msg.region,
        content: msg.content,
      });
    } else if (msg.type === 'router:makeCall') {
      getStreamedPayloads({
        op: msg.op,
        payload: msg.payload,
        onEvent: function (evt) {
          var iframe = document.querySelector('iframe[data-frame="MainWindow"]');
          if (iframe && iframe.contentWindow) {
            iframe.contentWindow.postMessage({
              type: 'router:event',
              call_id: msg.call_id,
              evt: evt,
            }, '*');
          }
        },
        onFinal: function () {
          var iframe = document.querySelector('iframe[data-frame="MainWindow"]');
          if (iframe && iframe.contentWindow) {
            iframe.contentWindow.postMessage({
              type: 'router:final',
              call_id: msg.call_id,
            }, '*');
          }
        },
        onError: function (err) {
          var iframe = document.querySelector('iframe[data-frame="MainWindow"]');
          if (iframe && iframe.contentWindow) {
            iframe.contentWindow.postMessage({
              type: 'router:error',
              call_id: msg.call_id,
              error: (err && err.message) || String(err),
            }, '*');
          }
        },
      });
    } else if (msg.type === 'router:subscribe-broadcast') {
      var unsub = subscribe(msg.kind, function (data, evt) {
        var iframe = document.querySelector('iframe[data-frame="MainWindow"]');
        if (iframe && iframe.contentWindow) {
          iframe.contentWindow.postMessage({
            type: 'router:event-broadcast',
            kind: msg.kind,
            data: data,
            evt: evt,
          }, '*');
        }
      });
      window['__unsub_' + msg.kind] = unsub;
    } else if (msg.type === 'router:exec') {
      try { new Function(String(msg.code || ''))(); } catch (_) {}
    }
  });

  function getSessionToken() {
    return _sessionToken;
  }

  function getSessionGuid() {
    if (!_sessionToken || typeof _sessionToken.token !== 'string') return null;
    return _sessionToken.token.slice(-32);
  }

  // Serialize a /gate request body with the current session token
  // attached. Exposed on window so the Playwright smoke suite can build
  // valid /gate posts from page.evaluate blocks without duplicating the
  // token-attach logic. Callers pass the op + payload; the helper
  // returns a ready-to-POST string.
  function gateBody(args) {
    var body = {
      op: (args && args.op) || 'boot',
      payload: (args && args.payload) || {},
    };
    if (args && args.intent) body.intent = args.intent;
    if (_sessionToken) body.session_token = _sessionToken;
    return JSON.stringify(body);
  }
  window._gateBody = gateBody;

  window.ClientRouter = {
    render: render,
    merge: merge,
    getStreamedPayloads: getStreamedPayloads,
    getFullPayload: getFullPayload,
    subscribe: subscribe,
    bootstrap: bootstrap,
    getSessionToken: getSessionToken,
    getSessionGuid: getSessionGuid,
    gateBody: gateBody,
  };
})();
