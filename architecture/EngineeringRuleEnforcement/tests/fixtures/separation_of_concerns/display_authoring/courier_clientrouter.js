// Fixture: ClientRouter-style courier writing an opaque content string into
// a named frame. Pattern is identical to the violation case but the file
// path is the operator-approved excluded_exact entry. The worker WILL flag
// this file's lines; the test asserts the exclusion path stops it being
// reported.

function renderToFrame(targetFrameId, opaqueContent) {
  var node = document.getElementById("frame_" + targetFrameId);
  if (!node) return;
  node.innerHTML = opaqueContent;
}
