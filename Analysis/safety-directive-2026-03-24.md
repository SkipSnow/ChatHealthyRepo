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

### 4. Hard Stop — No Going Back

On safety trigger:
- Do NOT call gpt-4o-mini
- Do NOT invoke tools
- Do NOT continue conversation
- Session is permanently locked: every subsequent message in the session
  also returns the emergency response, regardless of content

### 5. Fixed Response

Emergency response is hardcoded — no variation allowed:

> "This may be a medical emergency. Call 911 or go to the nearest emergency room immediately. Do not wait."

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
