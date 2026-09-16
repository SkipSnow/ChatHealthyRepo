// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// TranscriptWidget — the single conversation surface. It owns
// frame_UserMessage (directly above the prompt) and shows the running
// dialogue between the person and the model(s), turn by turn, attributed
// per speaker, auto-scrolling to the latest.
//
// It decides nothing about the conversation. The agent (SharedServices
// UniversalNavigationTool) owns the conversation record: which utterances
// are turns, who spoke each one, their order, and the paint directive, all
// read from the session's Mongo-backed session_conversation_history. This
// widget asks the agent for that record (op:'conversation_record') on load
// and receives it again on kind:'transcript' after every turn, then paints
// it. The only thing decided here is display: a speaker maps to a style.
//
// Attribution style (per speaker the agent names):
//   user    → green text, the person's words in bold
//   machine → blue text
// The vocabulary is open so a Talk-About-Care room can attribute several
// machine speakers later without this widget changing its contract.

import { useEffect, useRef } from 'react'

const TARGET = 'UserMessage'
const TRANSCRIPT_REGION = 'ch_transcript'
// A subordinate region of the same frame for result-adjacent controls
// (the provider narrowing chips + result summary) that are NOT conversation
// turns. It is laid down here so those widgets have a stable place to merge
// into and never share the transcript record. See report notes.
const ATTACHMENTS_REGION = 'ch_result_attachments'

interface Turn {
  speaker?: string
  text?: string
}

// The style each speaker maps to. Display only — the agent decided the
// speaker; this decides how it looks.
const SPEAKER_STYLE: Record<string, { label: string; color: string; bold: boolean }> = {
  user:    { label: 'You',         color: '#15803d', bold: true },
  machine: { label: 'FindCare AI', color: '#1d4ed8', bold: false },
}
const UNKNOWN_STYLE = { label: 'Speaker', color: '#374151', bold: false }

// Escape without a regular expression: Rule-008 statement 4 forbids regexes
// in executable front-end code, so this uses split/join like the other
// widgets on this surface.
function _esc(s: any): string {
  return String(s == null ? '' : s)
    .split('&').join('&amp;')
    .split('<').join('&lt;')
    .split('>').join('&gt;')
    .split('"').join('&quot;')
    .split("'").join('&#39;')
}

function buildTurnHtml(turn: Turn): string {
  const speaker = String(turn.speaker || '')
  const style = SPEAKER_STYLE[speaker] || UNKNOWN_STYLE
  const text = _esc(turn.text || '')
  if (!text) return ''
  const weight = style.bold ? 'font-weight:700;' : ''
  return (
    `<div data-testid="transcript-turn" data-speaker="${_esc(speaker)}"` +
    ` style="padding:0.3em 0.75em;line-height:1.35;">` +
      `<span style="display:block;font-size:0.72em;font-weight:600;` +
      `text-transform:uppercase;letter-spacing:0.04em;color:${style.color};">` +
      `${_esc(style.label)}</span>` +
      `<div style="color:${style.color};${weight}overflow-wrap:anywhere;">${text}</div>` +
    `</div>`
  )
}

function buildScaffoldHtml(): string {
  // frame_UserMessage sits between the results window and the prompt. The
  // transcript is a bounded scroll so the running dialogue never pushes the
  // prompt off the screen; the attachments region below it holds the
  // result-narrowing controls (not conversation turns).
  return (
    `<div style="display:flex;flex-direction:column;min-height:0;">` +
      `<div id="${TRANSCRIPT_REGION}" data-testid="conversation-transcript"` +
      ` role="log" aria-live="polite"` +
      ` style="overflow-y:auto;max-height:32vh;background:#fff;` +
      `padding:0.25em 0;border-top:0.0625em solid #e5e7eb;"></div>` +
      `<div id="${ATTACHMENTS_REGION}"></div>` +
    `</div>`
  )
}

export default function TranscriptWidget() {
  // The turns currently shown, so an append directive extends what is on
  // screen and a replace directive supplants it. Held in a ref because the
  // painting happens through the router, not through React's own DOM.
  const shownRef = useRef<Turn[]>([])

  useEffect(() => {
    function postRenderScaffold() {
      window.parent.postMessage({
        type: 'router:render', target: TARGET, append: false, popup: false,
        content: buildScaffoldHtml(),
      }, '*')
    }

    function paintTurns(turns: Turn[]) {
      const html = turns.map(buildTurnHtml).join('')
      window.parent.postMessage({
        type: 'router:merge', target: TARGET, region: TRANSCRIPT_REGION,
        content: html,
      }, '*')
      // Auto-scroll to the latest turn. The transcript region lives in the
      // parent document; router:exec is how a widget reaches it, the same
      // mechanism the loading timer uses.
      window.parent.postMessage({
        type: 'router:exec',
        code: `(function(){var el=document.getElementById('${TRANSCRIPT_REGION}');`
            + `if(el){el.scrollTop=el.scrollHeight;}})();`,
      }, '*')
    }

    function askAgentForRecord() {
      window.parent.postMessage({
        type: 'router:makeCall', op: 'conversation_record', payload: {},
        call_id: 'conversation-record-' + Date.now(),
      }, '*')
    }

    // Lay the surface down, subscribe to the agent's pushes, then ask for
    // the record so a reload rehydrates the dialogue.
    postRenderScaffold()
    window.parent.postMessage({
      type: 'router:subscribe-broadcast', kind: 'transcript',
    }, '*')
    askAgentForRecord()

    function onMessage(ev: MessageEvent) {
      const msg = ev.data
      if (!msg || typeof msg !== 'object') return

      if (msg.type === 'router:event-broadcast' && msg.kind === 'transcript') {
        const data = msg.data || {}
        const turns: Turn[] = Array.isArray(data.turns) ? data.turns : []
        // The paint directive is the agent's, not this widget's: append
        // extends the record on screen, otherwise it replaces it.
        shownRef.current = data.append ? shownRef.current.concat(turns) : turns
        paintTurns(shownRef.current)
        return
      }

      // Home resets to the first-load view: a blank conversation surface.
      // The persistent record is untouched in Mongo; a reload rehydrates it.
      if (msg.type === 'router:action' && msg.action === 'goto_home') {
        shownRef.current = []
        postRenderScaffold()
        return
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])

  return null
}
