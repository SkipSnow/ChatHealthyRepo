// Fixture: clean React widget - all server traffic through ClientRouter.

import { useEffect } from "react"

export default function CleanWidget() {
  useEffect(() => {
    window.parent.postMessage({
      type: "router:makeCall",
      op: "boot",
      payload: {},
      call_id: "boot-1",
    }, "*")
  }, [])
  return null
}
