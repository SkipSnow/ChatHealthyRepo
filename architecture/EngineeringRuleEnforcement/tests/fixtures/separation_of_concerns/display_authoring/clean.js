// Fixture: clean - pure plumbing, no display authoring.

function routeMessage(payload) {
  var iframe = document.getElementById("coreReactFrame");
  if (iframe && iframe.contentWindow) {
    iframe.contentWindow.postMessage(payload, "*");
  }
}

window.addEventListener("message", function (ev) {
  if (!ev.data || typeof ev.data !== "object") return;
  if (ev.data.type === "router:event") routeMessage(ev.data);
});
