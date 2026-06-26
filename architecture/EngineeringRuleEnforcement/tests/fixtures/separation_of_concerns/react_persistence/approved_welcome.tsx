// Fixture: a React widget that does a non-ClientRouter outbound read.
// Pattern matches WelcomeWidget's GET to /welcome - an approved out-of-band
// read on the enforcement entry's excluded_exact list. The worker WILL flag
// these calls; the test asserts the exclusion path stops it being reported.

import { useEffect } from "react"

export default function ApprovedReadWidget() {
  useEffect(() => {
    const origin = window.location.origin
    fetch(`${origin}/welcome`, { method: "POST" })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        // hand the structured payload to React to render
        return d
      })
      .catch(() => {})
  }, [])
  return null
}
