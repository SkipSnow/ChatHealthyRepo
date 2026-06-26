// Fixture: clean - opaque plumbing only, no domain-vocabulary tokens.

function forwardOpaquePayload(target, payload) {
  if (!target || !target.contentWindow) return;
  target.contentWindow.postMessage(payload, "*");
}

window.addEventListener("message", function (ev) {
  var iframe = document.getElementById("coreReactFrame");
  forwardOpaquePayload(iframe, ev.data);
});
