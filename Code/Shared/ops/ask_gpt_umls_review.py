"""One-shot GPT consultation: UMLS license review for ChatHealthy."""
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "DataPipelines"))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

from openai import OpenAI
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# Load the extracted license terms
with open(os.path.join(os.path.dirname(__file__), "..", "..", "..",
          "brain", "machine_artifacts", "content", "legal.json")) as f:
    legal = json.load(f)

# Find UMLS license entry
umls = None
for rec in legal.get("records", []):
    if "UMLS" in rec.get("title", ""):
        umls = rec
        break

license_text = json.dumps(umls.get("license_agreement", {}), indent=2) if umls else "NOT FOUND"

SYSTEM = """You are the Enterprise Architect and legal advisor for ChatHealthy.ai.
ChatHealthy is a consumer-facing healthcare provider quality scoring platform.
It uses public CMS data + proprietary crosswalks to score providers.
GOV-003: IP Protection. Source code and architecture are NOT public.
You are under NDA."""

USER = f"""Review this UMLS Metathesaurus License Agreement summary for ChatHealthy.ai.

Our use case: We use UMLS to build a proprietary crosswalk that maps drug molecules to FDA indications to ICD-10-CM codes. This crosswalk is embedded in provider records for vector search. Users search by condition or drug name, the system finds providers who treat that condition.

Extracted license terms:
{license_text}

Please provide:
1. A plain-English summary of what ChatHealthy CAN and CANNOT do under this license (3-5 bullet points each)
2. Specific compliance requirements we must implement before going to production
3. Risk areas — anything that could get us in trouble
4. The SNOMED CT Appendix 2 implications — we checked SNOMED CT US Edition. What does that mean for us?
5. CPT Category 3 restrictions — can we use CPT codes in our consumer-facing product or not?
6. Recommended actions before alpha launch (May 1, 2026)"""

resp = client.chat.completions.create(
    model="gpt-4.1",
    messages=[
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER},
    ],
    max_tokens=3000,
)
print(resp.choices[0].message.content)
