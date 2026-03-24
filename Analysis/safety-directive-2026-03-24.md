# Safety Directive — ChatHealthy FindCare

**Authority: Skip Snow (Skip the Boss)**
**Date: 2026-03-24**
**Status: MANDATED — NON-NEGOTIABLE**

---

## Safety is NOT optional and NOT open for debate.

- No experimentation
- No A/B testing
- No reduction in sensitivity
- No tradeoffs against cost or UX

This is a **HARD SYSTEM INVARIANT**.

---

## Rules

### 1. Safety Precedence

```
safety > cost > UX
```

If ANY signal of possible emergency exists → IMMEDIATE STOP → return emergency response.

### 2. Conservative Escalation

- False positives are **acceptable**
- False negatives are **NOT acceptable**

If classification is uncertain, ambiguous, or low confidence → **MUST escalate**.

### 3. Dual Detection (Required)

Safety detection MUST use:
- Rule-based signals (explicit phrases)
- AI-based semantic classification (Haiku)

If **EITHER** indicates risk → escalate.

#### Haiku classification standard (approved refinement, 2026-03-24)

Haiku MUST classify only **clear, specific** emergency signals — not vague discomfort.

**Escalate:** explicit symptoms (chest pain, difficulty breathing, stroke), stated
emergencies (heart attack, overdose, suicide/self-harm crisis), severe acute trauma.

**Do NOT escalate:** vague statements like "I'm in pain" or "I don't feel well" —
those require clarifying questions from the chat agent.

Rationale: "I'm in a lot of pain" without specifics is not an ER signal. Escalating
it is a false positive that destroys the session unnecessarily. Approved by Skip the Boss.

### 4. Hard Stop — No Going Back

On safety trigger:
- Do NOT call gpt-4o-mini
- Do NOT invoke tools
- Do NOT continue conversation
- Session is permanently locked: every subsequent message in the session
  also returns the emergency response, regardless of content

**Dual lock implementation** (belt-and-suspenders):
- IP-based lock: module-level dict, 1-hour expiry, survives Gradio session restarts
- Session-history lock: checks conversation history for the emergency response string;
  survives container restarts and multi-worker environments where the IP dict may be lost

### 5. Fixed Response

Emergency response is hardcoded — no variation allowed:

> **Call 911 or go to the nearest emergency room immediately. Do not wait.**
>
> **This chat has been suspended.**

The response is bold and definitive. "May be" language is removed — the message makes no hedges.

### 6. Failure Default

If safety system fails (error, timeout, invalid output) → **DEFAULT TO ESCALATION**.

### 7. Governance

This policy cannot be modified without explicit approval from Skip the Boss.

---

## Implementation

`Code/ConversationalUX/ChatHealthyWhoAmIChat/app.py`

- `EMERGENCY_RESPONSE` — hardcoded string (Rule 5)
- `EMERGENCY_KEYWORDS` — explicit phrase list (Rule 3, rule-based)
- `_safety_check(message)` — dual detection, returns bool (Rules 3, 4, 6)
- `Me.chat()` — safety gate is the first line, before any model call (Rule 4)
