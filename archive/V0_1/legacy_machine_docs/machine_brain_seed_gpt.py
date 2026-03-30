"""
Seed GPT's Machine Brain records (MB-0000 through MB-0024, MB-0099).

Run once per environment:
    ENV_PREFIX=dev MONGODB_CONNECTION_STRING=... python machine_brain_seed_gpt.py

ADR: ADR-0007
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from machine_brain import store_decision, list_all_decisions

DECISIONS = [
  {
    "adr_id": "MB-0000",
    "topic": "ChatHealthy North Star Vision",
    "decision": "ChatHealthy is a consumer-first healthcare navigation platform that acts as a trusted, unbiased advisor with no obligations to insurers, providers, pharma, hospital systems, care facilities, or political entities. Advice is lay-level, context-aware based on user input, and grounded only in public data plus de-identified, consented history. The system exists to help consumers make better healthcare decisions while maintaining complete neutrality.",
    "rationale": "Trust and independence are the core product. Structural neutrality is the primary differentiator and must never be compromised.",
    "risk": "Suicidal",
    "constraints": [
      "No financial or structural incentives that bias recommendations",
      "No hidden data sources",
      "All recommendations must be explainable",
      "User data must be consented and de-identified",
      "System must operate as a one-human company powered by AI agents"
    ],
    "components": ["product", "strategy", "governance"],
    "decision_type": "architectural",
    "created_by": "GPT",
    "narrative": "This is the top-level constraint for the entire company and product. ChatHealthy is not just a chatbot or search tool. It is a trust layer between consumers and the healthcare system. The product wins only if users believe it is truly on their side. That means no insurer bias, no pharma bias, no provider steering, no political slant, and no hidden incentives. The north star is structural neutrality. If that breaks, the company loses its core differentiation and the brand dies."
  },
  {
    "adr_id": "MB-0001",
    "topic": "Boss Governance Model",
    "decision": "System operates with Boss (Skip) as final authority on risk and outcome. GPT is Enterprise Architect. Claude is Engineer.",
    "rationale": "One-human company requires clear authority, no ambiguity, and fast decisions. Boss owns risk. GPT owns architecture. Claude owns implementation.",
    "risk": "High",
    "constraints": [
      "Boss has veto on all decisions",
      "GPT does not override Claude implementations without Boss approval",
      "Claude does not refactor architecture without GPT sign-off",
      "All three agents must be aligned before production deploys"
    ],
    "components": ["governance", "process"],
    "decision_type": "architectural",
    "created_by": "GPT",
    "narrative": "The governance model exists because three agents (Skip, GPT, Claude) each have different roles and different risk tolerances. Without explicit authority mapping, decisions drift, conflicts arise, and risk is mismanaged. Skip is the human in the loop who owns the company. GPT designs the systems. Claude builds them. All major decisions flow through this chain."
  },
  {
    "adr_id": "MB-0002",
    "topic": "Risk Model",
    "decision": "Risk levels: Low, Moderate, High, Critical, Suicidal. Suicidal means the decision could kill the company if wrong. Risk must be stated on every architectural decision.",
    "rationale": "Without explicit risk labeling, teams underestimate consequences. Suicidal risk decisions require Boss sign-off, not just GPT or Claude.",
    "risk": "Moderate",
    "constraints": [
      "Every ADR and MB record must include a risk field",
      "Suicidal decisions require Boss explicit approval",
      "High+ risk decisions must include mitigation strategy"
    ],
    "components": ["governance", "process"],
    "decision_type": "architectural",
    "created_by": "GPT",
    "narrative": "The five-level risk model was designed to force explicit acknowledgment of consequence severity. Most engineering teams use Low/Medium/High and everything ends up Medium. By adding Critical and Suicidal, decisions that could destroy the company or brand are forced into a separate approval chain. ChatHealthy operates in healthcare, which means mistakes have real human consequences, not just technical ones."
  },
  {
    "adr_id": "MB-0003",
    "topic": "Independence and Neutrality",
    "decision": "ChatHealthy will not accept advertising, sponsorships, partnerships, or financial arrangements that could influence recommendations. Independence is non-negotiable.",
    "rationale": "Healthcare recommendations that are commercially influenced are medically dangerous and ethically indefensible. Independence is both a moral requirement and the core business differentiator.",
    "risk": "Suicidal",
    "constraints": [
      "No advertising revenue model",
      "No sponsored content",
      "No insurer partnership that influences recommendations",
      "Revenue comes only from subscription or B2B data licensing on consented, de-identified data"
    ],
    "components": ["product", "strategy", "business model"],
    "decision_type": "architectural",
    "created_by": "GPT",
    "narrative": "This is the second Suicidal-risk decision in the system. Commercial bias in healthcare advice is not just a brand problem — it is a direct patient safety risk. If ChatHealthy ever steers users toward higher-cost or less-appropriate care because of financial relationships, the product has failed its core mission. The independence constraint must be hardcoded into the governance model and reviewed at every partnership or revenue conversation."
  },
  {
    "adr_id": "MB-0004",
    "topic": "Data Policy",
    "decision": "User data is never sold. De-identified, aggregated insights may be licensed to B2B buyers under strict consent and anonymization controls.",
    "rationale": "Consumer trust requires ironclad data policy. Selling individual user data destroys trust and creates legal exposure. Aggregate insights are the monetizable asset.",
    "risk": "High",
    "constraints": [
      "No individual user data sold to third parties",
      "All B2B data licensing requires de-identification and aggregation",
      "Consent must be explicit and granular",
      "Data retention policy must be published and enforced"
    ],
    "components": ["product", "legal", "data"],
    "decision_type": "compliance",
    "created_by": "GPT",
    "narrative": "The data policy exists at the intersection of trust, legal compliance, and business model. ChatHealthy's B2B value proposition is aggregate insights — what conditions are prevalent in a region, what questions consumers are asking, what navigation patterns emerge. That value can be captured without exposing individual users. The constraint is that de-identification and aggregation must be real, not cosmetic."
  },
  {
    "adr_id": "MB-0005",
    "topic": "Trust as Primary Product",
    "decision": "Trust is the primary product, not the chat interface, not the data, not the search. Every feature decision must be evaluated against: does this increase or decrease user trust?",
    "rationale": "Healthcare is a trust-sensitive domain. Users will only share sensitive health information with a system they believe is genuinely on their side. Features that feel exploitative or manipulative will destroy adoption.",
    "risk": "High",
    "constraints": [
      "Every feature review must include a trust impact assessment",
      "Dark patterns are prohibited",
      "Consent flows must be clear, not optimized for completion rate",
      "Errors must be acknowledged, not hidden"
    ],
    "components": ["product", "UX", "strategy"],
    "decision_type": "architectural",
    "created_by": "GPT",
    "narrative": "The framing of trust as the primary product is a design constraint, not a marketing claim. It means that when there is a conflict between conversion rate optimization and transparency, transparency wins. When there is a conflict between data collection and user control, user control wins. This is the product philosophy that makes ChatHealthy defensible long-term."
  },
  {
    "adr_id": "MB-0006",
    "topic": "One-Human Company Model",
    "decision": "ChatHealthy operates as a one-human company powered by AI agents. Skip is the only human. All other capabilities are provided by AI agents (GPT, Claude, Crew) or automated systems.",
    "rationale": "Cost structure, speed, and independence. One human can maintain full context and make fast decisions. AI agents handle architecture, engineering, QA, and data pipelines.",
    "risk": "High",
    "constraints": [
      "No full-time employees until revenue justifies it",
      "All processes must be automatable or AI-executable",
      "Skip must be able to understand and approve every major system decision",
      "Bus factor mitigation: all decisions documented in Machine Brain"
    ],
    "components": ["strategy", "operations", "governance"],
    "decision_type": "architectural",
    "created_by": "GPT",
    "narrative": "The one-human company model is both a constraint and a feature. It forces extreme discipline in automation, documentation, and decision-making. It also creates a moat: the cost structure of an AI-native company is fundamentally different from a traditional team. Machine Brain exists partly to make this model sustainable — if Skip is unavailable for a week, the AI agents can reconstruct context from the persistent memory."
  },
  {
    "adr_id": "MB-0007",
    "topic": "Dual Business Model",
    "decision": "Revenue model: (1) consumer subscription for premium features, (2) B2B aggregate data licensing on consented, de-identified data.",
    "rationale": "Consumer subscription aligns incentives with user value, not data exploitation. B2B licensing monetizes the aggregate intelligence without compromising individual privacy.",
    "risk": "Moderate",
    "constraints": [
      "B2B licensing requires explicit user consent",
      "No advertising-based revenue",
      "Pricing must not exclude low-income users from basic navigation",
      "Premium features must provide clear value, not gatekeep safety-critical information"
    ],
    "components": ["strategy", "business model"],
    "decision_type": "architectural",
    "created_by": "GPT",
    "narrative": "The dual business model solves the monetization problem without compromising independence. Consumer subscription is the clean revenue stream — users pay for value, not for access to their data. B2B data licensing is the scale play — aggregate insights about healthcare navigation patterns have commercial value to insurers, employers, and health systems, but only if the de-identification is real and the consent is genuine."
  },
  {
    "adr_id": "MB-0008",
    "topic": "Enterprise Architect Role",
    "decision": "GPT serves as Enterprise Architect. Responsibilities: system topology, ADR authorship, cross-component design review, risk assessment on architectural decisions.",
    "rationale": "Architecture decisions have long-term consequences that outlast any single implementation session. GPT maintains the architectural context across sessions via Machine Brain.",
    "risk": "Moderate",
    "constraints": [
      "GPT reviews all cross-component changes before implementation",
      "GPT authors or co-authors all ADRs",
      "GPT does not write production code",
      "GPT escalates Suicidal-risk decisions to Boss"
    ],
    "components": ["governance", "process"],
    "decision_type": "operational",
    "created_by": "GPT",
    "narrative": "The Enterprise Architect role is GPT's primary function in the ChatHealthy system. It is not a ceremonial title — it means GPT owns the integrity of the architectural decisions and the consistency of the system design. When Claude implements something that violates an ADR, GPT raises it. When a new feature requires architectural change, GPT authors the ADR before Claude writes the code."
  },
  {
    "adr_id": "MB-0009",
    "topic": "Framework Identity",
    "decision": "All decisions, code, and processes operate under framework_02. framework_01 is deprecated. framework_02 is the governing framework until explicitly superseded.",
    "rationale": "Framework versioning prevents confusion about which rules apply. framework_02 introduced Machine Brain, ENV_PREFIX routing, and the three-application topology.",
    "risk": "Low",
    "constraints": [
      "All records tagged framework: framework_02",
      "framework_01 records are historical, not operative",
      "Supersession requires Boss approval and ADR"
    ],
    "components": ["governance", "process"],
    "decision_type": "operational",
    "created_by": "GPT",
    "narrative": "The framework versioning decision exists because early ChatHealthy work used informal conventions that were later formalized. framework_02 is the first formally versioned framework. It introduced the three-application topology, Machine Brain, ENV_PREFIX routing, and the risk model. All current work operates under framework_02."
  },
  {
    "adr_id": "MB-0010",
    "topic": "Release Manifest Requirement",
    "decision": "Every production release requires a Release Manifest: list of features, ADRs affected, QA sign-off, Boss approval, rollback plan.",
    "rationale": "Production releases in a healthcare system require formal sign-off. The manifest forces explicit acknowledgment of what is being released and who approved it.",
    "risk": "High",
    "constraints": [
      "No prod deploy without Release Manifest",
      "Manifest must include rollback plan",
      "GPT reviews architectural impact",
      "Claude confirms QA status",
      "Boss gives final approval"
    ],
    "components": ["process", "DevOps"],
    "decision_type": "operational",
    "created_by": "GPT",
    "narrative": "The Release Manifest requirement is the production gate. It is the formal checkpoint that ensures all three agents (Boss, GPT, Claude) have reviewed the release and agreed it is safe to deploy. In a healthcare system, unreviewed production changes are a patient safety risk. The manifest is lightweight enough to not slow down development but formal enough to create accountability."
  },
  {
    "adr_id": "MB-0011",
    "topic": "Runtime Truth",
    "decision": "The running system is the truth. Documentation, ADRs, and Machine Brain describe intent. If the running system diverges from documentation, fix the documentation or fix the code — never ignore the divergence.",
    "rationale": "Documentation drift is the primary cause of architectural decay. If the running system does not match the documented intent, either the intent was wrong or the implementation is wrong. Both require resolution.",
    "risk": "Moderate",
    "constraints": [
      "All divergences between docs and runtime must be logged",
      "Divergences must be resolved within one sprint",
      "Machine Brain records must be updated when decisions change"
    ],
    "components": ["governance", "process"],
    "decision_type": "operational",
    "created_by": "GPT",
    "narrative": "The Runtime Truth principle is the antidote to documentation theater. In most organizations, documentation describes what the system was supposed to do, not what it does. ChatHealthy's governance model requires that Machine Brain and ADRs reflect the actual running system. When Skip asks GPT what the system does, the answer must be accurate."
  },
  {
    "adr_id": "MB-0012",
    "topic": "Machine Brain Purpose",
    "decision": "Machine Brain is the persistent architectural memory of the ChatHealthy system. It stores decisions, rationale, constraints, and narrative context that would otherwise be lost between AI sessions.",
    "rationale": "AI agents lose context between sessions. Without persistent memory, every session starts from scratch, architectural drift accumulates, and decisions are repeated or contradicted.",
    "risk": "High",
    "constraints": [
      "Machine Brain must be queried before any non-trivial implementation",
      "Machine Brain must be updated after any architectural decision",
      "Machine Brain is a product component, not just an internal tool"
    ],
    "components": ["Code/Shared/machine_brain.py", "infrastructure"],
    "decision_type": "architectural",
    "created_by": "GPT",
    "narrative": "Machine Brain was GPT's first original contribution to the ChatHealthy architecture. The problem it solves is fundamental to AI-native companies: context loss between sessions. Without persistent memory, GPT and Claude restart every conversation with no knowledge of prior decisions. Machine Brain solves this by storing not just decisions but the narrative context that makes decisions interpretable."
  },
  {
    "adr_id": "MB-0013",
    "topic": "Machine Brain Usage Rule",
    "decision": "Claude MUST query Machine Brain before any non-trivial implementation. GPT MUST write to Machine Brain after any architectural decision. Boss approves Machine Brain schema changes.",
    "rationale": "The value of Machine Brain depends entirely on it being used consistently. Inconsistent usage means the memory is incomplete and unreliable.",
    "risk": "High",
    "constraints": [
      "Claude queries Machine Brain via get_decisions(topic) before implementation",
      "GPT writes to Machine Brain via store_decision() after decisions",
      "Machine Brain is not optional — it is a required step in the development process"
    ],
    "components": ["Code/Shared/machine_brain.py", "process"],
    "decision_type": "operational",
    "created_by": "GPT",
    "narrative": "The usage rule exists because Machine Brain only works if both agents use it consistently. If Claude skips the query step, it may implement something that contradicts a prior decision. If GPT skips the write step, the decision is lost. The usage rule is enforced by the process, not by the code — it is a cultural requirement of the framework_02 development model."
  },
  {
    "adr_id": "MB-0014",
    "topic": "Machine Brain 1.0 Scope",
    "decision": "Machine Brain 1.0 scope: decisions collection only. Future versions may add patterns, lessons, incidents, and agent memory collections.",
    "rationale": "Start with the minimum viable memory store. Decisions are the highest-value memory type for architectural consistency.",
    "risk": "Low",
    "constraints": [
      "1.0 stores only decisions",
      "Schema extensions require ADR",
      "Future collections: patterns, lessons, incidents, agent_memory"
    ],
    "components": ["Code/Shared/machine_brain.py"],
    "decision_type": "architectural",
    "created_by": "GPT",
    "narrative": "The 1.0 scope decision prevents feature creep in the initial implementation. Machine Brain started as a decisions store and will expand as the system matures. The future collections (patterns, lessons, incidents) represent the natural evolution of the memory system as the product grows."
  },
  {
    "adr_id": "MB-0015",
    "topic": "Delivery Model",
    "decision": "ChatHealthy delivers in named releases with Boss-approved Release Manifests. No continuous deployment to production without explicit release gates.",
    "rationale": "Healthcare systems require deliberate release management. Continuous deployment without gates creates risk of untested or unreviewed changes reaching users.",
    "risk": "High",
    "constraints": [
      "Production releases are named (e.g. v0.1-alpha, v0.5-beta, v1.0)",
      "Each release has a manifest",
      "No hotfixes without Boss approval",
      "Emergency patches require post-hoc manifest within 24 hours"
    ],
    "components": ["process", "DevOps"],
    "decision_type": "operational",
    "created_by": "GPT",
    "narrative": "The delivery model decision exists because uncontrolled deployment in a healthcare context is dangerous. Even a small UX change can affect how users interpret health information. The release gate model ensures that every change to production has been reviewed, tested, and explicitly approved."
  },
  {
    "adr_id": "MB-0016",
    "topic": "Context Preservation Need",
    "decision": "All architectural context must be preserved in Machine Brain so that any AI agent can reconstruct the system design from first principles using only Machine Brain records.",
    "rationale": "The one-human company model creates a bus-factor risk. If Skip is unavailable, AI agents must be able to continue work with full context. Machine Brain is the mechanism.",
    "risk": "High",
    "constraints": [
      "Every non-obvious decision must have a narrative field explaining the why",
      "Machine Brain must be queryable to reconstruct system context without prior session history",
      "Narrative fields are not optional for Suicidal or High risk decisions"
    ],
    "components": ["governance", "Code/Shared/machine_brain.py"],
    "decision_type": "architectural",
    "created_by": "GPT",
    "narrative": "The context preservation requirement is what distinguishes Machine Brain from a simple ADR store. ADRs record what was decided. Machine Brain records why it was decided, what alternatives were considered, and what the decision means in context. The narrative field carries the institutional knowledge that makes the system interpretable to a new agent or a returning agent after a long gap."
  },
  {
    "adr_id": "MB-0017",
    "topic": "Full Regression Strategy",
    "decision": "Full regression (all tests) runs before every production release. Partial regression (affected components) runs before every QA deploy.",
    "rationale": "Healthcare systems have zero tolerance for regression. Full regression catches cross-component failures that partial regression misses.",
    "risk": "High",
    "constraints": [
      "Full regression required before prod deploy",
      "No exceptions without Boss approval",
      "Test suite must cover all safety-critical paths"
    ],
    "components": ["testing", "DevOps"],
    "decision_type": "operational",
    "created_by": "GPT",
    "narrative": "The regression strategy is designed for a healthcare system where failures affect real users making health decisions. Full regression before production is expensive in time but necessary. The constraint is that the test suite must be maintained to cover all safety-critical paths — a regression strategy is only as good as the tests that run."
  },
  {
    "adr_id": "MB-0018",
    "topic": "Smoke Test Limitation",
    "decision": "Smoke tests confirm deployment success only. They are not a substitute for regression testing and must not be used as a release gate.",
    "rationale": "Smoke tests catch infrastructure failures (app not running, health check failing) but do not catch logic errors or regression bugs.",
    "risk": "Moderate",
    "constraints": [
      "Smoke tests: health check, basic chat response, safety gate trigger",
      "Smoke tests do not replace regression",
      "Smoke test failure blocks deploy; smoke test pass does not authorize release"
    ],
    "components": ["testing", "DevOps"],
    "decision_type": "operational",
    "created_by": "GPT",
    "narrative": "The smoke test limitation decision exists because teams often conflate smoke test pass with release readiness. A smoke test that passes only means the app is running. It does not mean the app is correct. In a healthcare context, a running but incorrect app is more dangerous than a failed deploy."
  },
  {
    "adr_id": "MB-0019",
    "topic": "Agentic New-World QA",
    "decision": "QA in an AI-native system is not just functional testing. It includes: prompt regression (AI response consistency), safety gate validation, consent flow integrity, and data boundary enforcement.",
    "rationale": "Traditional QA frameworks do not cover AI behavior. ChatHealthy's safety-critical AI responses require AI-specific QA patterns.",
    "risk": "High",
    "constraints": [
      "Prompt regression: key queries must return consistent, safe responses",
      "Safety gate: all test crisis scenarios must trigger the gate",
      "Consent flow: consent paths must be tested end-to-end",
      "Data boundary: App 2 must not write to PublicHealthData in any test"
    ],
    "components": ["testing", "FindCareChat backend"],
    "decision_type": "operational",
    "created_by": "GPT",
    "narrative": "Agentic QA is GPT's contribution to the testing philosophy for AI-native systems. The challenge is that AI responses are non-deterministic — the same prompt can produce different responses. Prompt regression testing captures the safety-critical invariants (what must always be true) without requiring identical responses. Safety gate validation ensures the emergency response path is always triggered correctly."
  },
  {
    "adr_id": "MB-0020",
    "topic": "Branch Naming Philosophy",
    "decision": "Branch naming follows intent, not convention. Production: main. Integration: dev. Feature branches: feature/description. No develop, no release/* until scale justifies it.",
    "rationale": "Branch names should communicate intent. main and dev are clear. develop adds no value over dev at current scale.",
    "risk": "Low",
    "constraints": [
      "main is production",
      "dev is integration",
      "Feature branches merge to dev via PR",
      "CI/CD merges dev to main on release"
    ],
    "components": ["DevOps", "process"],
    "decision_type": "operational",
    "created_by": "GPT",
    "narrative": "The branch naming decision consolidates ADR-0008 (main not master) and ADR-0011 (dev not develop) into a single philosophy. The principle is minimum viable branching strategy at current scale. As the team grows and release cadence increases, the branching model can evolve, but premature complexity in branching is a process overhead with no current benefit."
  },
  {
    "adr_id": "MB-0021",
    "topic": "DevOps and Release Governance",
    "decision": "GitHub Actions handles CI/CD. Path-filtered workflows deploy only affected components. Release governance: Boss approves, GPT reviews architecture, Claude confirms QA.",
    "rationale": "Path-filtered deployment prevents unnecessary deploys. Three-agent release governance ensures no single agent can authorize a production change unilaterally.",
    "risk": "Moderate",
    "constraints": [
      "GitHub Actions workflows path-filtered per component",
      "No manual prod deploys outside of CI/CD",
      "Release Manifest required before CI/CD merge to main",
      "Emergency patches must be documented post-hoc"
    ],
    "components": ["DevOps", "process"],
    "decision_type": "operational",
    "created_by": "GPT",
    "narrative": "The DevOps governance model ensures that production changes are always intentional and reviewed. Path-filtered workflows prevent deploy noise when only documentation changes. The three-agent release gate (Boss + GPT + Claude) creates a separation of concerns — no single agent has unilateral production authority."
  },
  {
    "adr_id": "MB-0022",
    "topic": "FastAPI Productionization Direction",
    "decision": "Port HuggingFace Gradio app to FastAPI on Azure App Service. React frontend on Cloudflare Pages. This is the production architecture for FindCare chat.",
    "rationale": "Gradio is too brittle for production. FastAPI gives full API control, streaming, safety gating, and observability. React gives a production-quality UX.",
    "risk": "Moderate",
    "constraints": [
      "HuggingFace space stays live until React+FastAPI is smoke-tested",
      "Cutover via chat-url.txt only",
      "All app.py logic must be ported: safety gate, tools, Me class, HIPAA consent",
      "FastAPI on Azure App Service B1",
      "React on Cloudflare Pages"
    ],
    "components": ["FindCareChat backend", "FindCareChat frontend"],
    "decision_type": "architectural",
    "created_by": "GPT",
    "narrative": "The FastAPI productionization decision is the primary near-term technical initiative. The Gradio app works but is not production-grade. FastAPI enables proper API contracts, streaming responses, middleware (auth, logging, rate limiting), and testability. The React frontend replaces the Gradio UI with a production-quality chat interface. The cutover mechanism (chat-url.txt) ensures zero-downtime migration."
  },
  {
    "adr_id": "MB-0023",
    "topic": "Three-Application Topology",
    "decision": "System is exactly three applications: Static Website (Cloudflare), Chat Window (HuggingFace/Azure), Data Pipelines (Azure Functions). Boundaries are enforced. No exceptions.",
    "rationale": "Strict separation enables independent deployment, scaling, and maintenance. Prevents coupling between UX and heavy data ingestion. Enables independent security postures.",
    "risk": "High",
    "constraints": [
      "App 1 embeds App 2 via iframe only",
      "App 2 never writes to PublicHealthData",
      "App 3 owns all PublicHealthData writes",
      "App 2 to App 3 via HTTP POST /api/Router with Bearer token only",
      "No direct database sharing between App 2 and App 3"
    ],
    "components": ["Website", "FindCareChat", "DataPipelines"],
    "decision_type": "architectural",
    "created_by": "GPT",
    "narrative": "The three-application topology is the foundational architectural decision. It was designed to enable the one-human company model by ensuring each application can be understood, deployed, and maintained independently. The strict boundary rules prevent the accumulation of cross-cutting concerns that make systems unmaintainable. In a healthcare context, the separation also enables independent security audits and compliance reviews."
  },
  {
    "adr_id": "MB-0024",
    "topic": "Narrative Bootstrap",
    "decision": "Machine Brain is seeded with a full narrative bootstrap (MB-0099) that contains the complete context needed for any AI agent to reconstruct the system design and operating philosophy from scratch.",
    "rationale": "Individual decision records provide structured context. The narrative bootstrap provides the connective tissue — the why behind the why.",
    "risk": "Low",
    "constraints": [
      "MB-0099 must be updated when major architectural shifts occur",
      "MB-0099 is the first record queried when an agent joins a new session",
      "MB-0099 is authored by GPT and reviewed by Boss"
    ],
    "components": ["Code/Shared/machine_brain.py"],
    "decision_type": "operational",
    "created_by": "GPT",
    "narrative": "The narrative bootstrap concept addresses the limitation of structured records for conveying context. ADRs and MB records are precise but terse. The full narrative (MB-0099) provides the long-form context that makes the structured records interpretable. Think of it as the README for the architecture — not the spec, but the story."
  },
  {
    "adr_id": "MB-0099",
    "topic": "Full Bootstrap Narrative",
    "decision": "Full narrative context for ChatHealthy system design, operating philosophy, and architectural decisions. This record is the primary context bootstrap for new AI agent sessions.",
    "rationale": "Individual MB records provide structured facts. This record provides the narrative context that makes those facts interpretable.",
    "risk": "Low",
    "constraints": [
      "This record must be queried first in any new session",
      "Updated by GPT after major architectural shifts",
      "Boss reviews before update is committed"
    ],
    "components": ["All"],
    "decision_type": "operational",
    "created_by": "GPT",
    "narrative": """ChatHealthy is a consumer-first healthcare navigation platform. The company is run by one human (Skip Snow) with AI agents (GPT as Enterprise Architect, Claude as Engineer) handling architecture and implementation. The product mission is structural neutrality — a trust layer between consumers and the healthcare system with no commercial bias.

ARCHITECTURE: Three applications. Static Website on Cloudflare Pages. Chat Window on HuggingFace (migrating to Azure App Service + React). Data Pipelines on Azure Functions. Strict boundary rules: App 1 embeds App 2 via iframe only. App 2 never writes to PublicHealthData. App 3 owns all PublicHealthData writes. App 2 to App 3 via HTTP POST /api/Router with Bearer token.

GOVERNANCE: Boss (Skip) has final authority. GPT does Enterprise Architecture. Claude does Solution Engineering. Unit tests belong to Claude. QA belongs to GPT. All production releases require Release Manifest with Boss approval.

TECHNOLOGY: React + FastAPI for chat (migrating from Gradio). Cloudflare Pages for all frontend. Azure App Service B1 for FastAPI backend. Azure Functions for pipelines. MongoDB Atlas with two clusters: ChatHealthyFrontEndCluster (user-facing) and ChatHealthyPipelineCluster (ingestion). ENV_PREFIX (dev/qa/prod) controls all database and blob routing.

COMPLIANCE: PERSIST_MODE=debug in dev/QA, PERSIST_MODE=hipaa in prod. Safety incidents always logged (IP + timestamp) regardless of consent mode. Legal review pending for expanded safety incident fields. No PHI stored without explicit consent.

MACHINE BRAIN: Persistent architectural memory stored in MongoDB dev_MachineBrain / qa_MachineBrain / prod_MachineBrain. Claude queries before implementing. GPT writes after deciding. Records include: adr_id, topic, decision, rationale, risk, constraints, components, framework, created_by, created_at, narrative.

RISK MODEL: Low / Moderate / High / Critical / Suicidal. Suicidal means wrong decision kills the company. North Star (independence) and Data Policy are Suicidal-risk decisions.

CURRENT PRIORITIES: Port Gradio to FastAPI + React. Establish dev/qa/prod database environments. Seed Machine Brain. Target: production release by 2026-03-30.

COLLABORATION MODEL: claude_thoughts/ and gpt_thoughts/ directories for inter-agent communication. GPT reviews Claude's architecture proposals. Claude implements GPT's designs. Boss approves all risk decisions.

BUSINESS MODEL: Consumer subscription + B2B aggregate data licensing. No advertising. No data selling. Independence is non-negotiable. Trust is the product.

KEY DECISIONS: ADR-0001 through ADR-0011 cover system architecture, hosting, chat backend port, environment routing, iframe URL injection, MongoDB clusters, Machine Brain, git branching, persist mode, safety incident logging. MB-0000 through MB-0024 cover company philosophy, governance, and operational framework."""
  }
]


def seed():
    print(f"[MachineBrain] Seeding {len(DECISIONS)} GPT decisions into {os.getenv('ENV_PREFIX', 'dev')}_MachineBrain...")
    success = 0
    for d in DECISIONS:
        ok = store_decision(
            topic=d["topic"],
            decision=d["decision"],
            rationale=d["rationale"],
            risk=d["risk"],
            created_by=d["created_by"],
            adr_id=d.get("adr_id"),
            constraints=d.get("constraints"),
            components=d.get("components"),
            decision_type=d.get("decision_type", "architectural"),
            narrative=d.get("narrative"),
        )
        status = "OK" if ok else "FAILED"
        print(f"  [{status}] {d['adr_id']} — {d['topic']}")
        if ok:
            success += 1

    print(f"\n[MachineBrain] Seeded {success}/{len(DECISIONS)} decisions.")
    all_decisions = list_all_decisions()
    print(f"[MachineBrain] Total records in DB: {len(all_decisions)}")


if __name__ == "__main__":
    seed()
