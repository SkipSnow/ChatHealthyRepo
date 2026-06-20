// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// ClinicalTrialRenderer(trial, target, task)
// Partner tool: FindCare/ClinicalTrials/clinical_trials_models.py
//   class Trial(BaseModel)
(function () {
  function esc(s) {
    return String(s == null ? '' : s)
      .split('&').join('&amp;')
      .split('<').join('&lt;')
      .split('>').join('&gt;')
      .split('"').join('&quot;')
      .split("'").join('&#39;');
  }
  function val(v) {
    if (v === null || v === undefined) return '—';
    var s = String(v).trim();
    return s.length === 0 ? '—' : esc(s);
  }
  function join(arr, sep) {
    if (!Array.isArray(arr) || arr.length === 0) return '';
    sep = sep || ', ';
    return arr.filter(function (x) { return x; }).join(sep);
  }
  function yn(v) { return v ? 'yes' : 'no'; }
  function sectionTitle(title) {
    return '<h3 style="font-size:1em;color:#0b7a75;font-weight:700;border-bottom:0.125em solid #d8e2e1;margin:1em 0 0.5em;padding-bottom:0.25em;">' + esc(title) + '</h3>';
  }
  function kv4(rows) {
    if (!rows || rows.length === 0) return '';
    var html = '<table style="width:100%;border-collapse:collapse;font-size:0.95em;margin-bottom:0.5em;"><tbody>';
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      html += '<tr>'
        + '<td style="padding:0.25em 0.5em;background:#f9fafb;color:#6b7280;font-weight:600;border:0.0625em solid #e5e7eb;width:18%;">' + val(r[0]) + '</td>'
        + '<td style="padding:0.25em 0.5em;color:#374151;border:0.0625em solid #e5e7eb;width:32%;">' + val(r[1]) + '</td>'
        + '<td style="padding:0.25em 0.5em;background:#f9fafb;color:#6b7280;font-weight:600;border:0.0625em solid #e5e7eb;width:18%;">' + val(r[2]) + '</td>'
        + '<td style="padding:0.25em 0.5em;color:#374151;border:0.0625em solid #e5e7eb;width:32%;">' + val(r[3]) + '</td>'
        + '</tr>';
    }
    html += '</tbody></table>';
    return html;
  }
  function outcomeTable(outcomes) {
    if (!outcomes || outcomes.length === 0) return '';
    var html = '<table style="width:100%;border-collapse:collapse;font-size:0.95em;margin-bottom:0.5em;"><thead>'
      + '<tr><th style="padding:0.25em 0.5em;background:#f0fffe;color:#0b7a75;border:0.0625em solid #d8e2e1;text-align:left;">Measure</th>'
      + '<th style="padding:0.25em 0.5em;background:#f0fffe;color:#0b7a75;border:0.0625em solid #d8e2e1;text-align:left;">Description</th>'
      + '<th style="padding:0.25em 0.5em;background:#f0fffe;color:#0b7a75;border:0.0625em solid #d8e2e1;text-align:left;">Time frame</th></tr></thead><tbody>';
    for (var i = 0; i < outcomes.length; i++) {
      var o = outcomes[i] || {};
      html += '<tr>'
        + '<td style="padding:0.25em 0.5em;border:0.0625em solid #e5e7eb;color:#374151;">' + val(o.measure) + '</td>'
        + '<td style="padding:0.25em 0.5em;border:0.0625em solid #e5e7eb;color:#374151;">' + val(o.description) + '</td>'
        + '<td style="padding:0.25em 0.5em;border:0.0625em solid #e5e7eb;color:#374151;">' + val(o.time_frame) + '</td>'
        + '</tr>';
    }
    html += '</tbody></table>';
    return html;
  }
  function inlineBlock(label, value) {
    if (!value) return '';
    return '<div style="margin:0.5em 0;"><strong style="color:#374151;">' + esc(label) + ':</strong> ' + esc(value) + '</div>';
  }
  function paraSection(title, body) {
    if (!body) return '';
    return sectionTitle(title)
      + '<div style="white-space:pre-wrap;color:#374151;margin-bottom:0.5em;">' + esc(body) + '</div>';
  }
  function bullets(items) {
    if (!items || items.length === 0) return '';
    var html = '<ul style="margin:0.25em 0 0.5em 1.25em;color:#374151;">';
    for (var i = 0; i < items.length; i++) {
      html += '<li style="margin:0.125em 0;">' + items[i] + '</li>';
    }
    html += '</ul>';
    return html;
  }

  function ClinicalTrialRenderer(trial, target, task) {
    if (typeof target === 'string') target = document.getElementById(target);
    if (!trial) {
      if (target && typeof target.innerHTML !== 'undefined') target.innerHTML = '';
      return '';
    }
    var title = trial.brief_title || '(no title)';
    var phases = join(trial.phases) || '—';
    var conds = join(trial.conditions) || '—';
    var kw = join(trial.keywords);
    var di = trial.design_info || {};
    var rp = trial.responsible_party || {};
    var ipd = trial.ipd_sharing || {};
    var topRows = [
      ['National Clinical Trial ID', trial.nct_id, 'Status', trial.overall_status],
      ['Phase', phases, 'Enrollment', trial.enrollment_count],
      ['Study type', trial.study_type, 'Allocation', di.allocation],
      ['Sponsor', trial.lead_sponsor_name, 'Sponsor type', trial.lead_sponsor_class],
      ['Organization', trial.organization && trial.organization.full_name, 'Org class', trial.organization && trial.organization['class']],
      ['Start', trial.start_date, 'Primary completion', trial.primary_completion_date],
      ['First submit', trial.study_first_submit_date, 'Last update', trial.last_update_post_date],
      ['Min age', trial.minimum_age, 'Max age', trial.maximum_age],
      ['Sex', trial.sex, 'Healthy vol.', yn(trial.healthy_volunteers)],
      ['DMC oversight', yn(trial.oversight_has_dmc), 'FDA-regulated drug', yn(trial.is_fda_regulated_drug)],
      ['FDA-regulated device', yn(trial.is_fda_regulated_device), 'Unapproved device', yn(trial.is_unapproved_device)],
      ['Pediatric postmarket study (PPSD)', yn(trial.is_ppsd), 'FDAAA 801 violation', yn(trial.fdaaa_801_violation)],
    ];
    var designRows = [
      ['Allocation', di.allocation, 'Intervention model', di.intervention_model],
      ['Primary purpose', di.primary_purpose, 'Observational model', di.observational_model],
      ['Time perspective', di.time_perspective, 'Masking', di.masking],
      ['Who masked', join(di.who_masked), 'Target duration', trial.target_duration],
      ['Bio-spec retention', trial.bio_spec && trial.bio_spec.retention, 'Expanded access', join(trial.expanded_access_types)],
    ];
    var respPartyRows = [
      ['Type', rp.type, 'Investigator', rp.investigator_full_name],
      ['Investigator title', rp.investigator_title, 'Investigator affil.', rp.investigator_affiliation],
    ];
    var hasRespParty = rp.type || rp.investigator_full_name || rp.investigator_title || rp.investigator_affiliation;
    var eligRows = [
      ['Sex', trial.sex, 'Min age', trial.minimum_age],
      ['Max age', trial.maximum_age, 'Healthy volunteers', yn(trial.healthy_volunteers)],
      ['Gender description', trial.gender_description, 'Std ages', ''],
    ];
    var ipdRows = [
      ['IPD sharing', ipd.ipd_sharing, 'Time frame', ipd.time_frame],
      ['Info types', join(ipd.info_types), 'URL', ipd.url],
    ];
    var hasIpd = ipd.ipd_sharing || ipd.description || ipd.access_criteria || ipd.url;

    var html = '<div data-testid="clinical-trial" data-task="' + esc(task || 'full-detail') + '">';
    html += '<h2 style="font-size:1.25em;color:#0b7a75;margin:0 0 0.25em;">' + esc(title) + '</h2>';
    if (trial.official_title && trial.official_title !== title) {
      html += '<div style="font-style:italic;color:#6b7280;margin-bottom:0.5em;">' + esc(trial.official_title) + '</div>';
    }
    if (trial.acronym) html += inlineBlock('Acronym', trial.acronym);
    html += kv4(topRows);
    html += paraSection('Brief summary', trial.brief_summary);
    html += paraSection('Detailed description', trial.detailed_description);
    html += sectionTitle('Conditions') + '<div style="color:#374151;margin-bottom:0.5em;">' + esc(conds) + '</div>';
    if (kw) html += inlineBlock('Keywords', kw);
    html += sectionTitle('Design') + kv4(designRows);
    if (di.intervention_model_description) html += inlineBlock('Intervention model description', di.intervention_model_description);
    if (di.masking_description) html += inlineBlock('Masking description', di.masking_description);
    if (trial.bio_spec && trial.bio_spec.description) html += inlineBlock('Bio-spec description', trial.bio_spec.description);
    if (hasRespParty) html += sectionTitle('Responsible party') + kv4(respPartyRows);

    var collabs = (trial.collaborators || []).map(function (c) {
      return '<strong>' + esc(c.name || '') + '</strong> (' + esc(c['class'] || '') + ')';
    });
    if (collabs.length) html += sectionTitle('Collaborators') + bullets(collabs);

    var secIds = (trial.secondary_id_infos || []).map(function (s) {
      var line = esc(s.id || '') + ' — ' + esc(s.type || '');
      if (s.domain) line += ' / ' + esc(s.domain);
      return line;
    });
    if (secIds.length) html += sectionTitle('Secondary IDs') + bullets(secIds);

    var arms = (trial.arm_groups || []).map(function (a) {
      var ints = join(a.intervention_names);
      var parts = ['<strong>' + esc(a.label || '') + '</strong> <em>(' + esc(a.type || '') + ')</em>'];
      if (a.description) parts.push(esc(a.description));
      if (ints) parts.push('Interventions: ' + esc(ints));
      return '<div style="margin-bottom:0.5em;">' + parts.join('<br/>') + '</div>';
    });
    if (arms.length) html += sectionTitle('Arms') + arms.join('');

    var interventions = (trial.interventions || []).map(function (i) {
      var parts = ['<strong>' + esc(i.type || '') + ': ' + esc(i.name || '') + '</strong>'];
      if (i.description) parts.push(esc(i.description));
      if (i.arm_group_labels && i.arm_group_labels.length) parts.push('Arms: ' + esc(join(i.arm_group_labels)));
      if (i.other_names && i.other_names.length) parts.push('Other names: ' + esc(join(i.other_names)));
      return '<div style="margin-bottom:0.5em;">' + parts.join('<br/>') + '</div>';
    });
    if (interventions.length) html += sectionTitle('Interventions') + interventions.join('');

    if ((trial.primary_outcomes || []).length) html += sectionTitle('Primary outcomes') + outcomeTable(trial.primary_outcomes);
    if ((trial.secondary_outcomes || []).length) html += sectionTitle('Secondary outcomes') + outcomeTable(trial.secondary_outcomes);
    if ((trial.other_outcomes || []).length) html += sectionTitle('Other outcomes') + outcomeTable(trial.other_outcomes);

    html += sectionTitle('Eligibility') + kv4(eligRows);
    if (trial.eligibility_criteria) {
      html += '<div style="margin-top:0.25em;color:#374151;"><strong>Criteria:</strong></div>'
        + '<div style="white-space:pre-wrap;color:#374151;margin-bottom:0.5em;">' + esc(trial.eligibility_criteria) + '</div>';
    }

    var centralContacts = (trial.central_contacts || []).map(function (c) {
      var bits = ['<strong>' + esc(c.name || '') + '</strong> <em>(' + esc(c.role || '') + ')</em>'];
      var inner = [];
      if (c.phone) inner.push('📞 ' + esc(c.phone) + (c.phone_ext ? ' x' + esc(c.phone_ext) : ''));
      if (c.email) inner.push('✉ ' + esc(c.email));
      if (inner.length) bits.push(' — ' + inner.join(' · '));
      if (c.npi) bits.push(' <a href="/provider/' + esc(c.npi) + '" style="color:#0b7a75;">[Provider record]</a>');
      return bits.join('');
    });
    if (centralContacts.length) html += sectionTitle('Central contacts') + bullets(centralContacts);

    var officials = (trial.overall_officials || []).map(function (o) {
      return '<strong>' + esc(o.name || '') + '</strong> <em>(' + esc(o.role || '') + ')</em> — ' + esc(o.affiliation || '');
    });
    if (officials.length) html += sectionTitle('Overall officials') + bullets(officials);

    var locs = trial.locations || [];
    if (locs.length) {
      function siteCell(l) {
        if (!l) return '';
        var where = [l.facility, [l.city, l.state, l.zip].filter(function (x) { return x; }).join(', '), l.country]
          .filter(function (x) { return x; }).join(' — ');
        var status = l.status ? ' <em>(' + esc(l.status) + ')</em>' : '';
        var travel = (l.distance && l.duration) ? ' · ' + esc(l.distance) + ' · ' + esc(l.duration) : '';
        return esc(where) + status + travel;
      }
      var siteRows = '';
      for (var si = 0; si < locs.length; si += 2) {
        var label = si === 0 ? 'Sites' : '';
        siteRows += '<tr>'
          + '<td style="padding:0.25em 0.5em;background:#f9fafb;color:#6b7280;font-weight:600;border:0.0625em solid #e5e7eb;width:8%;">' + esc(label) + '</td>'
          + '<td style="padding:0.25em 0.5em;color:#374151;border:0.0625em solid #e5e7eb;width:46%;">' + siteCell(locs[si]) + '</td>'
          + '<td style="padding:0.25em 0.5em;color:#374151;border:0.0625em solid #e5e7eb;width:46%;">' + siteCell(locs[si + 1]) + '</td>'
          + '</tr>';
      }
      html += sectionTitle('Sites (' + locs.length + ')')
        + '<table style="width:100%;border-collapse:collapse;font-size:0.95em;margin-bottom:0.5em;"><tbody>' + siteRows + '</tbody></table>';
    }

    var references = (trial.references || []).map(function (r) {
      var pubmed = r.pmid ? ' <a href="https://pubmed.ncbi.nlm.nih.gov/' + esc(r.pmid) + '/" target="_blank" rel="noopener noreferrer" style="color:#0b7a75;">(PubMed)</a>' : '';
      return '<em>(' + esc(r.type || '') + ')</em> ' + esc(r.citation || '') + pubmed;
    });
    if (references.length) html += sectionTitle('References') + bullets(references);

    var seeAlso = (trial.see_also_links || []).map(function (s) {
      return '<a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer" style="color:#0b7a75;">' + esc(s.label || s.url) + '</a>';
    });
    if (seeAlso.length) html += sectionTitle('See also') + bullets(seeAlso);

    if (hasIpd) {
      html += sectionTitle('IPD sharing') + kv4(ipdRows);
      if (ipd.description) html += inlineBlock('Description', ipd.description);
      if (ipd.access_criteria) html += inlineBlock('Access criteria', ipd.access_criteria);
    }

    var condLeaves = (trial.condition_browse_leaves || []).map(function (b) {
      return esc(b.name || '') + (b.relevance ? ' <em>(relevance: ' + esc(String(b.relevance).toLowerCase()) + ')</em>' : '');
    });
    if (condLeaves.length) html += sectionTitle('MeSH-mapped conditions') + bullets(condLeaves);

    var interventionLeaves = (trial.intervention_browse_leaves || []).map(function (b) {
      return esc(b.name || '') + (b.relevance ? ' <em>(relevance: ' + esc(String(b.relevance).toLowerCase()) + ')</em>' : '');
    });
    if (interventionLeaves.length) html += sectionTitle('MeSH-mapped interventions') + bullets(interventionLeaves);

    var largeDocs = (trial.large_docs || []).map(function (d) {
      var flags = [d.has_protocol && 'Protocol', d.has_sap && 'SAP', d.has_icf && 'ICF'].filter(function (x) { return x; }).join(', ');
      return '<strong>' + esc(d.label || d.filename || '') + '</strong>'
        + (flags ? ' <em>(' + esc(flags) + ')</em>' : '')
        + ' — ' + esc(d.date || d.upload_date || '');
    });
    if (largeDocs.length) html += sectionTitle('Study documents') + bullets(largeDocs);

    var violations = ((trial.violation_annotation && trial.violation_annotation.violation_events) || []).map(function (v) {
      return '<strong>' + esc(v.type || '') + '</strong> — ' + esc(v.description || '') + ' <em>(issued ' + esc(v.issued_date || '') + ')</em>';
    });
    if (violations.length) html += sectionTitle('Compliance violations') + bullets(violations);

    if (trial.study_url) {
      html += '<div style="margin-top:1em;"><a href="' + esc(trial.study_url) + '" target="_blank" rel="noopener noreferrer" style="color:#0b7a75;font-weight:600;">View on ClinicalTrials.gov →</a></div>';
    }
    html += '</div>';

    if (target && typeof target.innerHTML !== 'undefined') {
      target.innerHTML = html;
    }
    return html;
  }

  window.ClinicalTrialRenderer = ClinicalTrialRenderer;
})();
