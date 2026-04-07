"""One-shot GPT consultation: crosswalk patent + RAG structure review."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "DataPipelines"))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

from openai import OpenAI
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

SYSTEM = """You are the Enterprise Architect for ChatHealthy.ai. You advise on data architecture, patent strategy, and RAG optimization.

Corporate governance:
- GOV-001: Provider Neutrality. No revenue from care-chain entities.
- GOV-011: AI Translates, System Answers. The AI turns user language into structured queries. The system answers from real data.
- GOV-013: Design For Today. No speculative architecture.
- GOV-003: IP Protection. Source code, architecture, and design are NOT public. Copyright Skip Snow.

You are under NDA. This conversation is confidential IP discussion."""

USER = """Skip Snow (Founder/CEO) and Claude (Principal Engineer) need your review on a patentable method we are building.

## THE METHOD

We are building a proprietary crosswalk that connects prescription data to clinical indications for consumer-facing provider quality scoring. Here is the chain:

1. CMS Medicare Part D Prescriber PUF (public data): gives us NPI + drug (brand/generic name) + claim counts per provider
2. Drug name to molecule: brand/generic maps to active ingredient (via RxNorm or our own normalization)
3. Molecule to FDA-approved indication: we build this crosswalk from public FDA sources (DailyMed SPLs, DrugCentral, FDA Orange Book). The molecule maps to one or more approved indications.
4. Indication to ICD-10-CM diagnosis code: each indication maps to diagnosis codes
5. Result per provider: we now know what conditions each provider treats, how frequently (claim counts), and at what cost (brand vs generic ratio)

GPT already confirmed there is NO existing public crosswalk from molecule to ICD-10-CM. We are building it. This crosswalk, combined with our scoring method, is our patentable IP.

## HOW WE USE IT FOR RAG

The crosswalk data lands on each provider record in MongoDB. When a user says "I have high blood pressure" or "I take Lisinopril-HCTZ 10-12.5mg":
- The system maps the user input to an indication (Hypertension, I10)
- Vector search finds providers whose embedding contains that indication
- The provider record has cost measures (generic ratio band, on-label band) and frequency data
- The scoring engine ranks providers, user adjusts weights via mixing board UI

## OUR PROPOSED PROVIDER RECORD STRUCTURE

We want to organize the provider JSON so that RAG retrieval and embedding are maximally efficient. Our current thinking is INDICATION-FIRST:

The provider record has a can_prescribe node. Under it, an indications array. Each indication has: indication name, icd10_codes array, cost_band (5% band string), frequency_band (5% band string), and a molecules array. Each molecule has: molecule name, brand_claims (int), generic_claims (int), total_claims (int). At the provider level under cost_measures: generic_ratio_band and on_label_band (both 5% band strings, 20 buckets each).

## QUESTIONS FOR YOU

1. Patent viability: Is this method (CMS prescription data + proprietary molecule-to-indication crosswalk + consumer-facing quality scoring with cost/frequency bands) patentable? What claims would be strongest?

2. Crosswalk construction: What is the best combination of FREE public data sources to build the molecule-to-indication-to-ICD-10 crosswalk? Rank by completeness and reliability.

3. RAG optimization: Is indication-first the right organization for the JSON that gets embedded? Or would a different structure produce better vector search results? Specifically:
   a. Should cost_band and frequency_band be AT the indication level or at provider level?
   b. Should ICD-10 codes be in the embedding text or just in the structured data?
   c. Would 5% bands (20 buckets) produce good clustering in embedding space?
   d. Any structural changes that would make cosine similarity work better for our use case?

4. Gotchas: What could go wrong with this approach that we should design around now?"""

resp = client.chat.completions.create(
    model="gpt-4.1",
    messages=[
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER},
    ],
    max_tokens=4000,
)
print(resp.choices[0].message.content)
