// Fixture: violates REQ-B-002 - React changes persistent state directly,
// and makes outbound calls that do not resolve through ClientRouter.

import { useEffect } from "react"

export default function BadWidget() {
  useEffect(() => {
    localStorage.setItem("ch_pref", "dark")
    sessionStorage.setItem("temp", "x")
    document.cookie = "ch_session=abc; Path=/"
    fetch("https://example.com/some-endpoint", { method: "POST", body: "{}" })
  }, [])
  return null
}
