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

   makeCall({op, payload, onEvent, onFinal, onError})
     Posts to SharedServices /gate as op + payload, reads the NDJSON
     stream, dispatches every {kind, data} envelope to subscribers and
     to the caller's onEvent. Calls onFinal once when {kind:'final'}
     arrives. Calls onError on any failure.

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
              data[a.name.substring(5)] = a.value;
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

  function _dispatchEvent(evt, caller) {
    if (!evt || typeof evt !== 'object') return;
    var kind = evt.kind || '';
    var handlers = _subscribers[kind] || [];
    for (var i = 0; i < handlers.length; i++) {
      try { handlers[i](evt.data || {}, evt); } catch (_) {}
    }
    if (caller && typeof caller.onEvent === 'function') {
      try { caller.onEvent(evt); } catch (_) {}
    }
  }

  function makeCall(args) {
    var op = args && args.op;
    if (!op) return Promise.reject(new Error('makeCall: op is required'));
    var payload = (args && args.payload) || {};
    var url = _sharedGateUrl() + '/gate';
    return fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/x-ndjson',
      },
      body: JSON.stringify({ op: op, payload: payload }),
    }).then(function (resp) {
      if (!resp.ok || !resp.body) {
        var err = new Error('ClientRouter.makeCall: gate failed HTTP ' + resp.status + ' for op=' + op);
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
    } else if (msg.type === 'router:makeCall') {
      makeCall({
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

  window.ClientRouter = {
    render: render,
    makeCall: makeCall,
    subscribe: subscribe,
  };
})();
