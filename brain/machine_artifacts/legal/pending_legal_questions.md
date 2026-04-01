# Pending Legal Questions - ChatHealthy FindCare

## Overview

ChatHealthy FindCare is a healthcare navigation chatbot entering alpha release (v0.1.2, target 2026-03-31). The application collects, stores, and processes user data across several interaction patterns — each with distinct consent and compliance implications. The following questions require legal review before the product moves beyond alpha into beta (v0.5) and production (v1.0). Resolution of these questions will inform the consent UX, data retention policies, and terms of service.

These questions are ordered by release urgency. Q1-Q3 should be reviewed before beta. Q4 is a corporate governance item that should be resolved before any external funding or partnership activity.

---

## Q1: Unanswerable Question Consent

**Context:** When a user asks a question the system cannot answer from its loaded context documents (e.g., "Does ChatHealthy accept insurance?"), the system records the question to MongoDB (`AboutUs.AboutSkip` collection) after de-identifying it via an LLM-based HIPAA Safe Harbor scrub. This is done automatically — the user is not informed that their question is being recorded.

**Current Behavior:** The `record_unknown_question` tool is called by the LLM before composing a response. The question text and de-identified chat history are stored. No consent is requested. No disclosure is made to the user.

**Questions for Legal Review:**
1. Does recording a de-identified user question require explicit consent, or is a terms-of-service disclosure sufficient?
2. If disclosure is sufficient, what language must appear in the terms of service or chat interface?
3. Does the de-identification process (LLM-based, not rule-based) meet the standard required for the data to be considered non-PII?

---

## Q2: Contact Us / Lead Capture Consent

**Context:** When a user provides their email and contact details through the lead capture flow, the system implements a two-tier HIPAA consent protocol before storing conversation data alongside the contact record.

**Current Behavior:**
- **Tier 1 (Verbatim):** "May we save a verbatim transcript of this conversation with your contact details?" If yes, the full chat history is stored with `consent_verbatim=true`. The consent exchange itself is included in the stored transcript as evidence.
- **Tier 2 (De-identified Summary):** If Tier 1 is declined, the system asks: "May we save a de-identified summary instead?" If yes, the conversation is summarized by LLM, de-identified, and stored in the `notes` field with `consent_summary=true`.
- **Tier 3 (Contact Only):** If both are declined, only contact fields (email, name) are stored. No conversation content is retained.

**Questions for Legal Review:**
1. Is the in-chat verbal consent (captured in the transcript) legally sufficient, or is a separate signed/clicked consent artifact required?
2. For Tier 2 (de-identified summary), does the user need to review and approve the de-identified version before storage?
3. What retention period applies to each tier? Is indefinite retention acceptable for alpha/beta, or must a retention policy be in place before storing any user data?
4. Does the two-tier flow meet HIPAA minimum necessary standard — are we collecting only what is needed for the stated purpose?

---

## Q3: Safety Audit Trail Consent

**Context:** When the safety filter detects a potential medical emergency (e.g., "chest pain", "I want to kill myself"), the system immediately responds with a 911/ER directive and locks the chat session for 1 hour. An incident record is written to MongoDB (`Safety.emergency_incidents`) containing the triggering message (truncated to 500 characters), a de-identified chat history (truncated to 300 characters per message), the user's IP address, and timestamps.

**Current Behavior:** This data is stored automatically as an audit trail. No consent is requested. The rationale is that this is an operational safety log, not a user-requested data collection. The Boss has confirmed: "We are not asking for consent on this one because it is our audit trail."

**Questions for Legal Review:**
1. Does storing IP address and de-identified chat history for safety incidents require disclosure in the terms of service or privacy policy?
2. Is the HIPAA emergency exception applicable here, or does it only apply to covered entities providing treatment?
3. What is the appropriate retention period for safety incident records? Should they be purged after a defined period or retained indefinitely for liability protection?
4. If a user requests data deletion (e.g., under CCPA), does the safety audit trail qualify for an exception, or must it be deleted?

---

## Q4: Intellectual Property License Agreement — Skip Snow to ChatHealthy.AI

**Context:** Skip Snow is the sole founder and developer of ChatHealthy.AI. All code, architecture, design documents, and business plans were created by Skip Snow, with AI assistance from Claude (Anthropic) and ChatGPT (OpenAI). The codebase is in a public GitHub repository (`SkipSnow/ChatHealthyRepo`). There is currently no formal IP assignment or license agreement between Skip Snow (individual) and ChatHealthy.AI (the business entity).

**Current State:** All code carries the copyright notice "Copyright 2026 Skip Snow. All rights reserved. Licensed under the FindCare Evaluation License (FEL-1.0)." The AI tools used in development (Claude, ChatGPT) have terms of service that assign output ownership to the user. No third-party code with restrictive licenses has been incorporated.

**Questions for Legal Review (paralegal review sufficient):**
1. What form of IP agreement is appropriate — assignment, exclusive license, or contribution agreement?
2. Does the use of AI coding assistants (Claude, ChatGPT) create any IP ownership ambiguity that should be addressed in the agreement?
3. Should the agreement cover future contributions, or only existing IP as of a specific date?
4. Is the FindCare Evaluation License (FEL-1.0) — a custom license — legally enforceable, or should a standard open-source or proprietary license be adopted?
5. What is the minimum documentation needed to establish clear IP ownership before engaging investors or partners?
