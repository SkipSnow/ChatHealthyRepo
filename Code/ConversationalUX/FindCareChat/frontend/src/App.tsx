// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).

import FindCareApp from './components/FindCareApp'
import SpecialtyFilter from '@findcare/SpecialtyFilter/SpecialtyFilter'

function App() {
  // Option B routing: when loaded as ?mode=filter (inside the parent's
  // leftPanel as a sub-iframe), render only the SpecialtyFilter (which
  // owns its own Apply Filter cell + session-verification cell). Otherwise
  // render the chat.
  const mode = new URLSearchParams(window.location.search).get('mode')
  if (mode === 'filter') {
    return <SpecialtyFilter />
  }
  return <FindCareApp />
}

export default App
