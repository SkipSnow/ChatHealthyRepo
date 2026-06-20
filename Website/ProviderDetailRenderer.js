// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// ProviderDetailRenderer(detail, target, task)
// Partner tool: FindCare/ProviderDetail/provider_detail_models.py
//   class ProviderDetailOutput(BaseModel)
(function () {
  function esc(s) {
    return String(s == null ? '' : s)
      .split('&').join('&amp;')
      .split('<').join('&lt;')
      .split('>').join('&gt;')
      .split('"').join('&quot;')
      .split("'").join('&#39;');
  }
  function sectionLabel(t) {
    return '<div style="padding:0.5em 1em;font-size:0.85em;font-weight:600;color:#6b7280;text-transform:uppercase;border-bottom:0.125em solid #e5e7eb;background:#f9fafb;">' + esc(t) + '</div>';
  }
  function emptyRow(t) {
    return '<div style="padding:0.5em 1em;font-size:0.85em;color:#9ca3af;font-style:italic;">' + esc(t) + '</div>';
  }

  function renderIdentity(id, fallbackNpi) {
    var fullName = [id.name_prefix, id.first_name, id.middle_name, id.last_name]
      .filter(function (s) { return s; }).join(' ');
    return '<div style="padding:0.75em 1em;border-bottom:0.125em solid #e5e7eb;background:#f8fffe;">'
      + '<div style="font-weight:600;color:#1f2937;font-size:1em;">' + esc(fullName) + '</div>'
      + (id.credentials ? '<div style="font-size:0.85em;color:#6b7280;">' + esc(id.credentials) + '</div>' : '')
      + (id.primary_taxonomy_display ? '<div style="font-size:0.85em;color:#0b7a75;">' + esc(id.primary_taxonomy_display) + '</div>' : '')
      + '<div style="font-size:0.85em;color:#9ca3af;margin-top:0.25em;">NPI: ' + esc(id.npi || fallbackNpi || '') + '</div>'
      + '<div style="font-size:0.85em;color:#6b7280;">Status: ' + (id.status_active ? 'Active' : 'Inactive') + '</div>'
      + (id.enumeration_date ? '<div style="font-size:0.85em;color:#9ca3af;">Enumerated: ' + esc(id.enumeration_date) + '</div>' : '')
      + '</div>';
  }

  function renderAddresses(addresses) {
    var rows = (addresses || []).map(function (a) {
      var typeLabel = a.address_type === 'business' ? 'Business'
                    : a.address_type === 'practice' ? 'Practice'
                    : esc(a.address_type || '');
      var lineParts = [a.line1, a.line2].filter(function (s) { return s; });
      var cityLine = [a.city, a.state, a.zip].filter(function (s) { return s; }).join(', ');
      var c = a.county || null;
      var countyText = '';
      if (c && c.name) {
        countyText = c.name + (c.urban === true ? ' · Urban' : c.urban === false ? ' · Rural' : '');
      }
      return '<div style="padding:0.5em 1em;border-bottom:0.125em solid #f3f4f6;">'
        + '<div style="font-size:0.75em;font-weight:600;color:#0b7a75;text-transform:uppercase;">' + typeLabel + '</div>'
        + lineParts.map(function (s) { return '<div style="font-size:0.9em;color:#374151;">' + esc(s) + '</div>'; }).join('')
        + (cityLine ? '<div style="font-size:0.9em;color:#374151;">' + esc(cityLine) + '</div>' : '')
        + (a.country && a.country !== 'US' ? '<div style="font-size:0.85em;color:#6b7280;">' + esc(a.country) + '</div>' : '')
        + (a.phone ? '<div style="font-size:0.85em;color:#6b7280;">Phone: ' + esc(a.phone) + '</div>' : '')
        + (countyText ? '<div style="font-size:0.85em;color:#6b7280;">County: ' + esc(countyText) + '</div>' : '')
        + '</div>';
    }).join('');
    return sectionLabel('Addresses') + (rows || emptyRow('No addresses on file.'));
  }

  function renderLicenses(licenses) {
    var rows = (licenses || []).map(function (l) {
      return '<div style="padding:0.5em 1em;border-bottom:0.125em solid #f3f4f6;font-size:0.9em;color:#374151;">'
        + esc(l.state || '') + (l.state && l.number ? ' · ' : '') + esc(l.number || '')
        + '</div>';
    }).join('');
    return sectionLabel('Licenses') + (rows || emptyRow('No licenses on file.'));
  }

  function renderInsurance(insurance) {
    var rows = (insurance || []).map(function (ins) {
      return '<div style="padding:0.5em 1em;border-bottom:0.125em solid #f3f4f6;">'
        + '<div style="font-size:0.9em;color:#374151;">' + esc(ins.brand || '') + '</div>'
        + '<div style="font-size:0.85em;color:#6b7280;">'
        + esc(ins.insurance_type || '')
        + (ins.state ? ' · ' + esc(ins.state) : '')
        + (ins.raw_value ? ' · ' + esc(ins.raw_value) : '')
        + '</div></div>';
    }).join('');
    return sectionLabel('Insurance') + (rows || emptyRow('No insurance identifiers on file.'));
  }

  function renderResearchSites(sites) {
    sites = sites || {};
    var keys = Object.keys(sites);
    var rows = keys.map(function (k) {
      var s = sites[k] || {};
      return '<div style="padding:0.75em 1em;border-bottom:0.125em solid #e5e7eb;">'
        + '<div style="font-weight:600;color:#0b7a75;font-size:0.95em;">'
        + '<a href="' + esc(s.url || '#') + '" target="_blank" rel="noopener noreferrer" style="color:#0b7a75;">'
        + esc(s.name || k) + '</a></div>'
        + '<div style="font-size:0.85em;color:#6b7280;margin-top:0.25em;">' + esc(s.guidance || '') + '</div>'
        + '</div>';
    }).join('');
    return sectionLabel('Research Sites') + (rows || emptyRow('No research sites available.'));
  }

  function ProviderDetailRenderer(detail, target, task) {
    if (!detail) return '';
    if (typeof target === 'string') target = document.getElementById(target);
    var id = detail.identity || {};
    var fullName = [id.name_prefix, id.first_name, id.middle_name, id.last_name]
      .filter(function (s) { return s; }).join(' ');
    var html = ''
      + '<div data-testid="provider-detail" data-task="' + esc(task || 'full-detail') + '">'
      + '<div style="padding:1em;border-bottom:0.25em solid #0b7a75;background:#f0fffe;">'
      + '<div style="font-size:1em;font-weight:700;color:#0b7a75;text-transform:uppercase;">Provider Detail</div>'
      + '<div style="font-size:1em;color:#374151;margin-top:0.25em;font-weight:600;">' + esc(detail.provider_name || fullName || '') + '</div>'
      + '</div>'
      + renderIdentity(id, detail.npi)
      + renderAddresses(detail.addresses)
      + renderLicenses(detail.licenses)
      + renderInsurance(detail.insurance)
      + renderResearchSites(detail.research_sites)
      + '</div>';
    if (target && typeof target.innerHTML !== 'undefined') {
      target.innerHTML = html;
    }
    return html;
  }

  window.ProviderDetailRenderer = ProviderDetailRenderer;
})();
