// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// Architecture POC host. The React iframe holds capability widgets — each
// widget subscribes to ClientRouter broadcasts and calls router:render to
// paint into the parent's 7 named frames. Widgets are added here as each
// capability is ported.

import HeaderWidget from './components/HeaderWidget'
import MobileNavDrawerWidget from '@shared/displayChrome/MobileNavDrawerWidget'
import FooterWidget from '@shared/displayChrome/FooterWidget'
import WelcomeWidget from '@shared/displayChrome/WelcomeWidget'
import UserPromptWidget from '@shared/UtteranceManager/UserPromptWidget'
import SystemMessageWidget from '@shared/UtteranceManager/SystemMessageWidget'
import ProviderResultsWidget from '@providers/ProviderResultsWidget'
import FacilityResultsWidget from '@shared/FacilitySearch/FacilityResultsWidget'
import SelectedProvidersWidget from './components/SelectedProvidersWidget'
import ProviderSearchRefinementWidget from './components/ProviderSearchRefinementWidget'
import ProviderDetailWidget from '@findcare/ProviderDetail/ProviderDetailWidget'
import SpecialtyFilterWidget from '@findcare/SpecialtyFilter/SpecialtyFilterWidget'
import SessionDataWidget from '@shared/AuthorizationsAndAuthentications/SessionDataWidget'
import ContextSwitchWidget from '@shared/externalInterface/ContextSwitchWidget'
import ClinicalTrialsWidget from './components/ClinicalTrialsWidget'
import SelectedClinicalTrialsWidget from '@shared/ClinicalTrialSelection/SelectedClinicalTrialsWidget'
import NewQueryLoadingWidget from '@shared/crossComponentTimers/NewQueryLoadingWidget'
import EvaluateCareSplashWidget from '@shared/handoffToEvaluateCare/EvaluateCareSplashWidget'
import LegalPanelWidget from './components/LegalPanelWidget'
import OAuthLoginWidget from '@shared/AuthorizationsAndAuthentications/OAuthLoginWidget'
import AboutChatHealthyWidget from '@shared/AboutChatHealthy/AboutChatHealthyWidget'
import PanelNavWidget from '@shared/displayChrome/PanelNavWidget'
import PopupHost from '@shared/displayChrome/PopupHost'

function App() {
  return (
    <>
      <PopupHost />
      <HeaderWidget />
      <MobileNavDrawerWidget />
      <FooterWidget />
      <WelcomeWidget />
      <UserPromptWidget />
      <SystemMessageWidget />
      <ProviderResultsWidget />
      <FacilityResultsWidget />
      <SelectedProvidersWidget />
      <ProviderSearchRefinementWidget />
      <ProviderDetailWidget />
      <SpecialtyFilterWidget />
      <SessionDataWidget />
      <ContextSwitchWidget />
      <ClinicalTrialsWidget />
      <SelectedClinicalTrialsWidget />
      <NewQueryLoadingWidget />
      <EvaluateCareSplashWidget />
      <LegalPanelWidget />
      <OAuthLoginWidget />
      <AboutChatHealthyWidget />
      <PanelNavWidget />
    </>
  )
}

export default App
