// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// Architecture POC host. The React iframe holds capability widgets — each
// widget subscribes to ClientRouter broadcasts and calls router:render to
// paint into the parent's 7 named frames. Widgets are added here as each
// capability is ported.

import HeaderWidget from './components/HeaderWidget'
import MobileNavDrawerWidget from './components/MobileNavDrawerWidget'
import FooterWidget from './components/FooterWidget'
import WelcomeWidget from './components/WelcomeWidget'
import UserPromptWidget from './components/UserPromptWidget'
import SystemMessageWidget from './components/SystemMessageWidget'
import ProviderResultsWidget from './components/ProviderResultsWidget'
import ProviderDetailWidget from './components/ProviderDetailWidget'
import SpecialtyFilterWidget from './components/SpecialtyFilterWidget'
import ClinicalTrialsWidget from './components/ClinicalTrialsWidget'
import EvaluateCareSplashWidget from './components/EvaluateCareSplashWidget'
import SharedServicesSplashWidget from './components/SharedServicesSplashWidget'
import LegalPanelWidget from './components/LegalPanelWidget'
import OAuthLoginWidget from './components/OAuthLoginWidget'
import AboutChatHealthyWidget from './components/AboutChatHealthyWidget'

function App() {
  return (
    <>
      <HeaderWidget />
      <MobileNavDrawerWidget />
      <FooterWidget />
      <WelcomeWidget />
      <UserPromptWidget />
      <SystemMessageWidget />
      <ProviderResultsWidget />
      <ProviderDetailWidget />
      <SpecialtyFilterWidget />
      <ClinicalTrialsWidget />
      <EvaluateCareSplashWidget />
      <SharedServicesSplashWidget />
      <LegalPanelWidget />
      <OAuthLoginWidget />
      <AboutChatHealthyWidget />
    </>
  )
}

export default App
