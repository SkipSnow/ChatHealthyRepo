// Fixture: violates REQ-B-001 - authors display content from non-React .js.
// Three independent violations: innerHTML assignment, createElement chain,
// template-literal HTML.

function bad1() {
  var el = document.getElementById("frame_MainWindow");
  el.innerHTML = "<div>hello world</div>";
}

function bad2() {
  var span = document.createElement("span");
  span.textContent = "shipping decision: yes";
  document.body.appendChild(span);
}

function bad3() {
  return `<section><p>nope</p></section>`;
}
