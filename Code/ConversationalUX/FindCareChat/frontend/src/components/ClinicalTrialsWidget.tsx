// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// Clinical trials React widget — POC of the new client-side architecture.
// Tool produces structured Pydantic data. This widget consumes the
// structured data, builds HTML for the Main Window (trial detail),
// Left Panel (bullet list + pagination), and Right Panel (section
// jump list), and hands each HTML fragment to the wrapper's
// ClientRouter via router:render postMessage.

import { useEffect, useRef, useState } from 'react'

function esc(s: any): string {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

function _slug(s: string): string {
  return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
}

function firstSentence(text: string): string {
  const s = String(text || '').trim()
  if (!s) return ''
  const m = s.match(/^[\s\S]*?[.!?](?=\s|$)/)
  if (!m) return s
  return m[0].length < s.length ? m[0] + '...' : m[0]
}

function nearestDistance(trial: any): string {
  const locs = trial.locations || []
  const withDist = locs.filter((l: any) => l && l.distance && l.duration)
  if (!withDist.length) return '—'
  const ranked = withDist.slice().sort((a: any, b: any) => {
    const na = parseFloat(String(a.distance).replace(/[^0-9.]/g, '')) || 0
    const nb = parseFloat(String(b.distance).replace(/[^0-9.]/g, '')) || 0
    return na - nb
  })
  return `${ranked[0].distance} · ${ranked[0].duration}`
}

function ageRange(trial: any): string {
  const a = (trial.minimum_age || '').trim()
  const b = (trial.maximum_age || '').trim()
  if (!a && !b) return '—'
  if (a && b) return `${a} – ${b}`
  return a || b
}

function buildLeftPanel(
  trials: any[],
  selectedIdx: number,
  hasPrev: boolean,
  hasMore: boolean,
  pageStart: number,
  totalCount: number | null,
  searchContext: any,
): string {
  const ctx = searchContext || {}
  const queryParts: string[] = []
  if (ctx.condition) queryParts.push(`condition: ${ctx.condition}`)
  if (ctx.age_years != null) queryParts.push(`subject age: ${ctx.age_years}`)
  if (ctx.sex) queryParts.push(`subject sex: ${ctx.sex}`)
  if (ctx.gender) queryParts.push(`subject gender: ${ctx.gender}`)
  if (ctx.user_location) queryParts.push(`location: ${ctx.user_location}`)
  const queryStr = queryParts.join(', ')
  const end = pageStart + trials.length - 1
  const countStr = (typeof totalCount === 'number' && totalCount > 0)
    ? ` — showing ${pageStart} to ${end} of ${totalCount}` : ''
  const header = queryStr
    ? `Clinical trials — ${esc(queryStr)}${esc(countStr)}`
    : `Clinical trials${esc(countStr)}`

  const items = trials.map((t: any, i: number) => {
    const nct = esc(t.nct_id || '(no id)')
    const title = esc(t.brief_title || '')
    const summary = esc(firstSentence(t.brief_summary || ''))
    const conds = esc((t.conditions || []).join(', ') || '—')
    const dist = esc(nearestDistance(t))
    const ages = esc(ageRange(t))
    const sex = esc(t.sex || '—')
    const selected = i === selectedIdx ? 'background:#f0fffe;' : ''
    return `
      <li style="margin-bottom:0.75em;padding:0.4em;${selected}border-radius:0.3em;">
        <a href="#" data-router-action="trial:select" data-trial-idx="${i}"
           style="color:#0b7a75;font-weight:700;text-decoration:underline;cursor:pointer;">${nct}</a>
        <div style="font-size:0.9em;color:#374151;margin:0.15em 0 0.25em 0;">${title}</div>
        ${summary ? `<div style="font-size:0.85em;color:#4b5563;font-style:italic;margin:0.15em 0 0.35em 0;">${summary}</div>` : ''}
        <ul style="margin:0.25em 0 0 1em;padding:0;font-size:0.9em;color:#4b5563;">
          <li><strong>Conditions:</strong> ${conds}</li>
          <li><strong>Nearest site:</strong> ${dist}</li>
          <li><strong>Age range:</strong> ${ages}</li>
          <li><strong>Sex:</strong> ${sex}</li>
        </ul>
      </li>`
  }).join('')
  const moreLink = hasMore
    ? `<a href="#" data-router-action="trial:page" data-direction="next"
          style="color:#0b7a75;text-decoration:underline;font-weight:700;cursor:pointer;">more trials</a>`
    : ''
  const backLink = hasPrev
    ? `<a href="#" data-router-action="trial:page" data-direction="prev"
          style="color:#0b7a75;text-decoration:underline;font-weight:700;cursor:pointer;">back</a>`
    : ''
  const ctrlRow = (moreLink || backLink)
    ? `<div style="margin-top:0.75em;font-size:0.95em;color:#374151;">${backLink}${(moreLink && backLink) ? ' &nbsp;|&nbsp; ' : ''}${moreLink}</div>`
    : ''

  return `
    <div style="padding:1em;box-sizing:border-box;">
      <div style="font-size:1em;font-weight:700;color:#0b7a75;text-transform:uppercase;margin-bottom:0.5em;">${header}</div>
      <ul style="list-style:disc;padding-left:1.25em;margin:0;">${items}</ul>
      ${ctrlRow}
    </div>`
}

function sectionTitle(title: string): string {
  const sid = 'ct-sec-' + _slug(title)
  return `<h3 id="${sid}" style="font-size:1em;color:#0b7a75;font-weight:700;border-bottom:0.125em solid #d8e2e1;margin:1em 0 0.5em;padding-bottom:0.25em;">${esc(title)}</h3>`
}

function buildMainWindow(trial: any): string {
  if (!trial) return '<div style="padding:1em;color:#6b7280;">No trial selected.</div>'
  const di = trial.design_info || {}
  const ipd = trial.ipd_sharing || {}
  const yn = (v: any) => (v ? 'yes' : 'no')
  const inlineKV = (label: string, value: any) =>
    value ? `<div style="margin:0.4em 0;"><strong>${esc(label)}:</strong> ${esc(value)}</div>` : ''
  const facts = `
    <table style="width:100%;border-collapse:collapse;font-size:0.95em;margin-bottom:0.5em;"><tbody>
      <tr><td style="background:#f9fafb;color:#6b7280;font-weight:600;border:1px solid #e5e7eb;padding:0.25em 0.5em;">NCT ID</td>
          <td style="padding:0.25em 0.5em;border:1px solid #e5e7eb;color:#374151;">${esc(trial.nct_id)}</td>
          <td style="background:#f9fafb;color:#6b7280;font-weight:600;border:1px solid #e5e7eb;padding:0.25em 0.5em;">Status</td>
          <td style="padding:0.25em 0.5em;border:1px solid #e5e7eb;color:#374151;">${esc(trial.overall_status)}</td></tr>
      <tr><td style="background:#f9fafb;color:#6b7280;font-weight:600;border:1px solid #e5e7eb;padding:0.25em 0.5em;">Phase</td>
          <td style="padding:0.25em 0.5em;border:1px solid #e5e7eb;color:#374151;">${esc((trial.phases || []).join(', ') || '—')}</td>
          <td style="background:#f9fafb;color:#6b7280;font-weight:600;border:1px solid #e5e7eb;padding:0.25em 0.5em;">Sponsor</td>
          <td style="padding:0.25em 0.5em;border:1px solid #e5e7eb;color:#374151;">${esc(trial.lead_sponsor_name)}</td></tr>
      <tr><td style="background:#f9fafb;color:#6b7280;font-weight:600;border:1px solid #e5e7eb;padding:0.25em 0.5em;">Sex</td>
          <td style="padding:0.25em 0.5em;border:1px solid #e5e7eb;color:#374151;">${esc(trial.sex || '—')}</td>
          <td style="background:#f9fafb;color:#6b7280;font-weight:600;border:1px solid #e5e7eb;padding:0.25em 0.5em;">Healthy vol.</td>
          <td style="padding:0.25em 0.5em;border:1px solid #e5e7eb;color:#374151;">${yn(trial.healthy_volunteers)}</td></tr>
      <tr><td style="background:#f9fafb;color:#6b7280;font-weight:600;border:1px solid #e5e7eb;padding:0.25em 0.5em;">Min age</td>
          <td style="padding:0.25em 0.5em;border:1px solid #e5e7eb;color:#374151;">${esc(trial.minimum_age || '—')}</td>
          <td style="background:#f9fafb;color:#6b7280;font-weight:600;border:1px solid #e5e7eb;padding:0.25em 0.5em;">Max age</td>
          <td style="padding:0.25em 0.5em;border:1px solid #e5e7eb;color:#374151;">${esc(trial.maximum_age || '—')}</td></tr>
    </tbody></table>
  `
  const sections: string[] = []
  sections.push(`<h2 style="margin:0 0 0.5em 0;color:#0b7a75;font-size:1.25em;">${esc(trial.brief_title || '')}</h2>`)
  if (trial.official_title && trial.official_title !== trial.brief_title) {
    sections.push(`<div style="font-style:italic;color:#6b7280;margin-bottom:0.5em;">${esc(trial.official_title)}</div>`)
  }
  sections.push(facts)
  if (trial.brief_summary) {
    sections.push(sectionTitle('Brief summary'))
    sections.push(`<div style="white-space:pre-wrap;color:#374151;margin-bottom:0.5em;">${esc(trial.brief_summary)}</div>`)
  }
  if (trial.detailed_description) {
    sections.push(sectionTitle('Detailed description'))
    sections.push(`<div style="white-space:pre-wrap;color:#374151;margin-bottom:0.5em;">${esc(trial.detailed_description)}</div>`)
  }
  if ((trial.conditions || []).length) {
    sections.push(sectionTitle('Conditions'))
    sections.push(`<div style="color:#374151;margin-bottom:0.5em;">${esc((trial.conditions || []).join(', '))}</div>`)
  }
  if (trial.eligibility_criteria) {
    sections.push(sectionTitle('Eligibility'))
    sections.push(`<div style="white-space:pre-wrap;color:#374151;margin-bottom:0.5em;">${esc(trial.eligibility_criteria)}</div>`)
  }
  if ((trial.locations || []).length) {
    sections.push(sectionTitle(`Sites (${trial.locations.length})`))
    const siteRows = (trial.locations || []).map((l: any) => {
      const where = [l.facility, [l.city, l.state, l.zip].filter(Boolean).join(', '), l.country].filter(Boolean).join(' — ')
      const status = l.status ? ` <em>(${esc(l.status)})</em>` : ''
      const travel = (l.distance && l.duration) ? ` · ${esc(l.distance)} · ${esc(l.duration)}` : ''
      return `<li>${esc(where)}${status}${travel}</li>`
    }).join('')
    sections.push(`<ul style="margin:0.25em 0 0.5em 1.25em;color:#374151;">${siteRows}</ul>`)
  }
  if (trial.study_url) {
    sections.push(`<div style="margin-top:1em;"><a href="${esc(trial.study_url)}" target="_blank" rel="noopener noreferrer" style="color:#0b7a75;font-weight:600;">View on ClinicalTrials.gov →</a></div>`)
  }
  return `<div style="max-width:70em;margin:0 auto;padding:1em;background:#fff;border:0.125em solid #e5e7eb;border-radius:0.5em;font-size:1em;line-height:1.6;color:#1f2937;">${sections.join('')}</div>`
}

function buildRightPanel(trial: any): string {
  if (!trial) return ''
  const headers = [
    trial.brief_summary ? 'Brief summary' : null,
    trial.detailed_description ? 'Detailed description' : null,
    (trial.conditions || []).length ? 'Conditions' : null,
    trial.eligibility_criteria ? 'Eligibility' : null,
    (trial.locations || []).length ? `Sites (${trial.locations.length})` : null,
  ].filter(Boolean) as string[]
  if (!headers.length) return ''
  const items = headers.map((h: string) => {
    const sid = 'ct-sec-' + _slug(h)
    return `<li style="margin:0.25em 0;"><a href="#" data-router-action="trial:scroll" data-anchor="${esc(sid)}" style="color:#0b7a75;text-decoration:underline;cursor:pointer;">${esc(h)}</a></li>`
  }).join('')
  return `
    <div style="padding:1em;box-sizing:border-box;">
      <div style="font-size:1em;font-weight:700;color:#0b7a75;text-transform:uppercase;margin-bottom:0.5em;">Trial sections</div>
      <ul style="list-style:none;padding:0;margin:0;font-size:0.9em;">${items}</ul>
    </div>`
}

function buildLoadingBanner(criteria: string, pageStart: number, pageSize: number, totalCount: number | null): string {
  const endRange = pageStart + pageSize - 1
  const totalSuffix = totalCount ? ` of ${totalCount}` : ''
  return `<div style="padding:2em;text-align:center;color:#0b7a75;">
    <div style="font-size:1em;color:#374151;margin-bottom:0.5em;">${esc(criteria)} — retrieving records ${pageStart}–${endRange}${esc(totalSuffix)}</div>
    <div id="ct-loading-timer" style="font-size:1em;font-weight:700;">0s</div>
  </div>`
}

export default function ClinicalTrialsWidget() {
  const [trials, setTrials] = useState<any[]>([])
  const [selectedIdx, setSelectedIdx] = useState<number>(0)
  const [totalCount, setTotalCount] = useState<number | null>(null)
  const [pageSize, setPageSize] = useState<number>(10)
  const [pageStart, setPageStart] = useState<number>(1)
  const nextCursorRef = useRef<string | null>(null)
  const prevCursorsRef = useRef<string[]>([])
  const lastQueryRef = useRef<any>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    function postRender(target: string, content: string) {
      window.parent.postMessage({
        type: 'router:render',
        target: target,
        append: false,
        popup: false,
        content: content,
      }, '*')
    }
    function postSubscribe(kind: string) {
      window.parent.postMessage({ type: 'router:subscribe-broadcast', kind: kind }, '*')
    }

    postSubscribe('trials')
    postSubscribe('intent_classified')

    function applyTrialsData(data: any, newPageStart: number) {
      const list = data.trials || []
      setTrials(list)
      setSelectedIdx(0)
      nextCursorRef.current = data.cursor || null
      const tc = typeof data.total_count === 'number' ? data.total_count : null
      if (tc !== null) setTotalCount(tc)
      const ps = typeof data.page_size === 'number' ? data.page_size : 10
      setPageSize(ps)
      setPageStart(newPageStart)
      const ctx = data.search_context || lastQueryRef.current || {}
      lastQueryRef.current = {
        condition: ctx.condition || '',
        user_location: ctx.user_location || null,
        age_years: ctx.age_years ?? null,
        sex: ctx.sex || null,
        gender: ctx.gender || null,
      }
      const hasPrev = prevCursorsRef.current.length > 0
      const hasMore = !!data.cursor
      postRender('LeftPanel', buildLeftPanel(list, 0, hasPrev, hasMore, newPageStart, tc !== null ? tc : totalCount, ctx))
      postRender('MainWindow', buildMainWindow(list[0] || null))
      postRender('RightPanel', buildRightPanel(list[0] || null))
    }

    function startLoadingTimer(criteria: string, startN: number) {
      postRender('MainWindow', buildLoadingBanner(criteria, startN, pageSize, totalCount))
      postRender('RightPanel', '')
      if (timerRef.current) clearInterval(timerRef.current)
      const t0 = Date.now()
      timerRef.current = setInterval(() => {
        const sec = Math.round((Date.now() - t0) / 1000)
        const banner = buildLoadingBanner(criteria, startN, pageSize, totalCount).replace(
          /<div id="ct-loading-timer"[^>]*>[^<]*<\/div>/,
          `<div id="ct-loading-timer" style="font-size:1em;font-weight:700;">${sec}s</div>`,
        )
        postRender('MainWindow', banner)
      }, 1000)
    }

    function stopLoadingTimer() {
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
    }

    function onMessage(ev: MessageEvent) {
      const msg = ev.data
      if (!msg || typeof msg !== 'object') return

      if (msg.type === 'router:event-broadcast') {
        if (msg.kind === 'trials') {
          stopLoadingTimer()
          applyTrialsData(msg.data || {}, pageStart || 1)
        }
        if (msg.kind === 'intent_classified') {
          const d = msg.data || {}
          if (d.action === 'findClinicalTrials') {
            const parts: string[] = []
            if (d.condition) parts.push(`condition: ${d.condition}`)
            if (d.age_years != null) parts.push(`subject age: ${d.age_years}`)
            if (d.sex) parts.push(`subject sex: ${d.sex}`)
            if (d.gender) parts.push(`subject gender: ${d.gender}`)
            if (d.user_location) parts.push(`location: ${d.user_location}`)
            const criteria = parts.join(', ')
            lastQueryRef.current = {
              condition: d.condition || '',
              user_location: d.user_location || null,
              age_years: d.age_years ?? null,
              sex: d.sex || null,
              gender: d.gender || null,
            }
            startLoadingTimer(criteria || 'clinical trials', 1)
          }
        }
      }

      if (msg.type === 'router:action') {
        if (msg.action === 'trial:select') {
          const idx = parseInt(String((msg.data || {}).trial_idx || '0'), 10)
          setSelectedIdx(idx)
          const t = trials[idx]
          if (t) {
            postRender('MainWindow', buildMainWindow(t))
            postRender('RightPanel', buildRightPanel(t))
            const hasPrev = prevCursorsRef.current.length > 0
            const hasMore = !!nextCursorRef.current
            postRender('LeftPanel', buildLeftPanel(trials, idx, hasPrev, hasMore, pageStart, totalCount, lastQueryRef.current))
          }
        }
        if (msg.action === 'trial:scroll') {
          const anchorId = String((msg.data || {}).anchor || '')
          if (anchorId) {
            window.parent.postMessage({
              type: 'router:exec',
              code: `var a = document.getElementById('${anchorId}'); if (a && a.scrollIntoView) a.scrollIntoView({behavior:'smooth', block:'start'});`,
            }, '*')
          }
        }
        if (msg.action === 'trial:page') {
          const dir = String((msg.data || {}).direction || '')
          const last = lastQueryRef.current
          if (!last) return
          let cursor: string | null = null
          let newPageStart = pageStart
          if (dir === 'next') {
            cursor = nextCursorRef.current
            if (!cursor) return
            prevCursorsRef.current.push(cursor)
            newPageStart = pageStart + trials.length
          } else if (dir === 'prev') {
            prevCursorsRef.current.pop()
            cursor = prevCursorsRef.current.length
              ? prevCursorsRef.current[prevCursorsRef.current.length - 1] : null
            newPageStart = Math.max(1, pageStart - pageSize)
          } else { return }
          const parts: string[] = []
          if (last.condition) parts.push(`condition: ${last.condition}`)
          if (last.age_years != null) parts.push(`subject age: ${last.age_years}`)
          if (last.sex) parts.push(`subject sex: ${last.sex}`)
          if (last.gender) parts.push(`subject gender: ${last.gender}`)
          if (last.user_location) parts.push(`location: ${last.user_location}`)
          startLoadingTimer(parts.join(', '), newPageStart)
          setPageStart(newPageStart)
          window.parent.postMessage({
            type: 'router:makeCall',
            call_id: `trial-page-${Date.now()}`,
            op: 'clinical_trials_page',
            payload: {
              condition: last.condition,
              user_location: last.user_location || null,
              page_size: pageSize,
              cursor: cursor,
              age_years: last.age_years,
              sex: last.sex,
              gender: last.gender,
            },
          }, '*')
        }
      }
    }

    window.addEventListener('message', onMessage)
    return () => {
      stopLoadingTimer()
      window.removeEventListener('message', onMessage)
    }
  }, [trials, pageStart, pageSize, totalCount])

  return null
}
