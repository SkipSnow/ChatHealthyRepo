// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// CorrectionsRenderer(text, corrections) -> HTML string
// Partner tool: sharedServices/Code/UtteranceManager/utterance_manager.py
//   class Correction(BaseModel) { original: str, corrected: str }
//   ClassifierOutput.corrections: list[Correction]
(function () {
  function esc(s) {
    return String(s == null ? '' : s)
      .split('&').join('&amp;')
      .split('<').join('&lt;')
      .split('>').join('&gt;')
      .split('"').join('&quot;')
      .split("'").join('&#39;');
  }
  function CorrectionsRenderer(text, corrections) {
    if (!corrections || !corrections.length) return esc(text || '');
    if (text == null) return '';
    var remaining = String(text);
    var html = '';
    for (var i = 0; i < corrections.length; i++) {
      var c = corrections[i] || {};
      var original = c.original || '';
      if (!original) continue;
      var marker = "(corrected from '" + original + "')";
      var markerIdx = remaining.indexOf(marker);
      if (markerIdx >= 0) {
        if (markerIdx > 0) html += esc(remaining.substring(0, markerIdx));
        html += '<span style="color:#dc2626;">' + esc(marker) + '</span>';
        remaining = remaining.substring(markerIdx + marker.length);
        continue;
      }
      var lower = remaining.toLowerCase();
      var idx = lower.indexOf(original.toLowerCase());
      if (idx < 0) continue;
      var matched = remaining.substring(idx, idx + original.length);
      if (idx > 0) html += esc(remaining.substring(0, idx));
      html += esc(c.corrected || '');
      html += '<span style="color:#dc2626;"> (corrected from \'' + esc(matched) + '\')</span>';
      remaining = remaining.substring(idx + original.length);
    }
    if (remaining) html += esc(remaining);
    return html;
  }
  window.CorrectionsRenderer = CorrectionsRenderer;
})();
