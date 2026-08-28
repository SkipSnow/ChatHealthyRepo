// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// PopupHost — React owns the popup, chrome and all.
//
// The overlay, its border, the close control and the print control used to be
// built by ClientRouter in the parent document. That is display authored
// outside React, and it is why the parent needed a print handler: the window
// belonged to the parent, so only the parent could print it. Owning the
// window here removes both.
//
// Widgets ask for a popup by posting ch:popup to their own window. Content is
// opaque to this component: it renders what it is handed and decides nothing
// about it.

import { useEffect, useRef, useState } from 'react'

type PopupState = { target: string; content: string; title: string } | null

export default function PopupHost() {
  const [popup, setPopup] = useState<PopupState>(null)
  const bodyRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    function onMessage(ev: MessageEvent) {
      const msg = ev.data
      if (!msg || typeof msg !== 'object') return
      if (msg.type === 'ch:popup') {
        setPopup({
          target: String(msg.target || ''),
          content: String(msg.content || ''),
          title: String(msg.title || ''),
        })
      } else if (msg.type === 'ch:popup-close') {
        setPopup(null)
      } else if (msg.type === 'ch:popup-print') {
        // Print one element of this window. The browser's own print
        // dialogue offers Save as PDF, so no PDF library is needed on
        // either side, and no state changes.
        const src = document.getElementById(String(msg.element || ''))
        if (!src) return
        const clone = src.cloneNode(true) as HTMLElement
        clone.querySelectorAll('[data-print-omit]')
          .forEach(el => el.parentNode && el.parentNode.removeChild(el))
        // A scrollable pane prints as its visible slice unless released.
        clone.querySelectorAll<HTMLElement>('*').forEach(el => {
          if (el.style && el.style.maxHeight) {
            el.style.maxHeight = 'none'
            el.style.overflow = 'visible'
          }
        })
        const w = window.open('', '_blank')
        if (!w) return
        const title = String(msg.title || 'ChatHealthy')
          .split('<').join('').split('&').join('')
        w.document.open()
        w.document.write(
          '<!doctype html><html><head><meta charset="utf-8"><title>' +
          title + '</title></head><body style="font-family:system-ui,sans-serif;">' +
          clone.innerHTML + '</body></html>')
        w.document.close()
        w.focus()
        // Give the new document a tick to lay out; printing an unlaid-out
        // document yields a blank first page.
        w.setTimeout(() => w.print(), 150)
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])

  // Controls inside the popup content still travel the router's action path,
  // so a widget binds behaviour the same way whether its content is in a
  // frame or in this window.
  useEffect(() => {
    const root = bodyRef.current
    if (!root) return
    const nodes = root.querySelectorAll('[data-router-action]')
    const bound: Array<[Element, string, EventListener]> = []
    nodes.forEach(el => {
      const action = el.getAttribute('data-router-action') || ''
      const tag = (el.tagName || '').toLowerCase()
      const evt = tag === 'form' ? 'submit'
        : (tag === 'input' || tag === 'select' || tag === 'textarea') ? 'change'
        : 'click'
      const handler: EventListener = e => {
        e.preventDefault()
        const data: Record<string, string> = {}
        Array.from(el.attributes).forEach(a => {
          if (a.name.startsWith('data-') && a.name !== 'data-router-action') {
            data[a.name.substring(5).split('-').join('_')] = a.value
          }
        })
        window.postMessage({ type: 'router:action', action, data }, '*')
      }
      el.addEventListener(evt, handler)
      bound.push([el, evt, handler])
    })
    return () => bound.forEach(([el, evt, h]) => el.removeEventListener(evt, h))
  }, [popup])

  if (!popup) return null

  return (
    <div
      role="dialog"
      aria-label={popup.title || 'ChatHealthy'}
      style={{
        position: 'fixed', top: '50%', left: '50%',
        transform: 'translate(-50%,-50%)',
        minWidth: '32em', maxWidth: '60em', maxHeight: '80vh',
        overflow: 'auto', zIndex: 1000, background: '#fff',
        border: '0.25em solid #0b7a75',
        boxShadow: '0 0.5em 2em rgba(0,0,0,0.25)',
        borderRadius: '0.5em', padding: 0,
      }}
    >
      <a
        href="#"
        title="Close"
        aria-label="Close"
        onClick={e => { e.preventDefault(); setPopup(null) }}
        style={{
          position: 'absolute', top: '0.25em', right: '0.5em',
          fontSize: '1.75em', lineHeight: 1, fontWeight: 700,
          color: '#0b7a75', textDecoration: 'none', cursor: 'pointer',
          padding: '0.1em 0.4em', borderRadius: '0.3em',
        }}
      >×</a>
      <div
        ref={bodyRef}
        className="ch-popup-body"
        style={{ padding: '1em' }}
        dangerouslySetInnerHTML={{ __html: popup.content }}
      />
    </div>
  )
}
