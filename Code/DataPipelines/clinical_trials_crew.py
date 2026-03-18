"""
Clinical Trial Search Crew — Layer 2 DataPipeline.

Receives a patient condition (and optional location) from Layer 1, queries
ClinicalTrials.gov, assesses eligibility, and returns structured trial cards
via a 3-agent CrewAI crew.

Payload:
    condition   (str, required)  — e.g. "stage 3 pancreatic cancer"
    location    (str, optional)  — e.g. "New York, NY"
    max_results (int, optional)  — default 5, max 10

Returns:
    {
        "trials": [
            {
                "nct_id": "NCT...",
                "title": "...",
                "phase": "Phase 2",
                "status": "RECRUITING",
                "locations": ["city, state"],
                "eligibility_match": "Likely | Uncertain | Unlikely",
                "eligibility_rationale": "...",
                "url": "https://clinicaltrials.gov/study/NCT..."
            }
        ],
        "next_steps": "..."
    }
"""

import json
import logging
import os

import requests
from crewai import Agent, Crew, LLM, Process, Task
from crewai.tools import tool

CLINICALTRIALS_API = "https://clinicaltrials.gov/api/v2/studies"
MAX_RESULTS_CAP = 10


@tool("search_clinical_trials")
def search_clinical_trials(condition: str, location: str = "", max_results: int = 5) -> str:
    """
    Search ClinicalTrials.gov for actively recruiting studies.
    Returns trial details: NCT ID, title, phase, locations, summary, eligibility criteria.
    """
    params = {
        "query.cond": condition,
        "filter.overallStatus": "RECRUITING",
        "pageSize": min(int(max_results), MAX_RESULTS_CAP),
        "format": "json",
    }
    if location:
        params["query.locn"] = location

    try:
        response = requests.get(CLINICALTRIALS_API, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return f"ClinicalTrials.gov search failed: {exc}"

    studies = data.get("studies", [])
    if not studies:
        return "No recruiting trials found for this condition."

    results = []
    for study in studies:
        ps = study.get("protocolSection", {})
        id_mod = ps.get("identificationModule", {})
        status_mod = ps.get("statusModule", {})
        desc_mod = ps.get("descriptionModule", {})
        elig_mod = ps.get("eligibilityModule", {})
        contacts_mod = ps.get("contactsLocationsModule", {})
        design_mod = ps.get("designModule", {})

        nct_id = id_mod.get("nctId", "")
        title = id_mod.get("briefTitle", "")
        status = status_mod.get("overallStatus", "")
        summary = (desc_mod.get("briefSummary") or "")[:600]
        eligibility = (elig_mod.get("eligibilityCriteria") or "")[:800]
        phases = design_mod.get("phases", [])

        raw_locations = contacts_mod.get("locations", [])
        location_strs = [
            ", ".join(filter(None, [loc.get("facility"), loc.get("city"), loc.get("state")]))
            for loc in raw_locations[:3]
        ]

        results.append(
            f"NCT ID: {nct_id}\n"
            f"Title: {title}\n"
            f"Status: {status}\n"
            f"Phase: {', '.join(phases) or 'N/A'}\n"
            f"Locations: {'; '.join(location_strs) or 'See ClinicalTrials.gov'}\n"
            f"Summary: {summary}\n"
            f"Eligibility Criteria:\n{eligibility}"
        )

    return "\n\n---\n\n".join(results)


def run_clinical_trial_search(payload: dict) -> dict:
    """Entry point called from function_app.py dispatch."""
    condition = (payload.get("condition") or "").strip()
    if not condition:
        raise ValueError("'condition' is required in payload")

    location = (payload.get("location") or "").strip()
    max_results = min(int(payload.get("max_results", 5)), MAX_RESULTS_CAP)

    logging.info(
        "ClinicalTrialSearch: condition='%s' location='%s' max_results=%d",
        condition, location, max_results,
    )

    llm_search = LLM(model="gpt-4o-mini", api_key=os.getenv("OPENAI_API_KEY"))
    llm_haiku = LLM(
        model="anthropic/claude-haiku-4-5-20251001",
        api_key=os.getenv("Anthropic_API_KEY"),
    )

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    search_agent = Agent(
        role="Clinical Trial Search Specialist",
        goal="Find actively recruiting clinical trials that match the patient's condition.",
        backstory=(
            "You are an expert at querying ClinicalTrials.gov and extracting complete, "
            "accurate trial information including eligibility criteria and locations."
        ),
        tools=[search_clinical_trials],
        llm=llm_search,
        verbose=False,
    )

    eligibility_agent = Agent(
        role="Clinical Trial Eligibility Analyst",
        goal="Assess which trials the patient is most likely to qualify for.",
        backstory=(
            "You are a clinical research coordinator who matches patients to trials by "
            "carefully reading inclusion and exclusion criteria."
        ),
        llm=llm_haiku,
        verbose=False,
    )

    docs_agent = Agent(
        role="Clinical Trial Navigator",
        goal="Format trial results as a structured JSON object with clear next steps.",
        backstory=(
            "You help patients understand their clinical trial options and take the next step. "
            "You always return valid JSON and nothing else — no markdown, no explanation."
        ),
        llm=llm_haiku,
        verbose=False,
    )

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    search_task = Task(
        description=(
            f"Search ClinicalTrials.gov for recruiting trials for: '{condition}'. "
            f"Location filter: '{location or 'none'}'. Retrieve up to {max_results} trials. "
            "Use the search_clinical_trials tool and return all fields for every trial found."
        ),
        expected_output=(
            "A list of clinical trials with NCT IDs, titles, phases, statuses, "
            "locations, summaries, and eligibility criteria."
        ),
        agent=search_agent,
    )

    eligibility_task = Task(
        description=(
            f"Given the trials found above and the patient condition '{condition}', "
            "assess each trial's eligibility. For every trial assign one of: "
            "Likely / Uncertain / Unlikely — with a 1–2 sentence rationale per trial."
        ),
        expected_output=(
            "Per-trial eligibility assessment: NCT ID, match (Likely/Uncertain/Unlikely), "
            "and a brief rationale."
        ),
        agent=eligibility_agent,
        context=[search_task],
    )

    docs_task = Task(
        description=(
            "Using the trial data and eligibility assessments above, produce a JSON object "
            "with exactly this schema — return ONLY the JSON, no markdown fences:\n"
            "{\n"
            '  "trials": [\n'
            "    {\n"
            '      "nct_id": "NCT...",\n'
            '      "title": "...",\n'
            '      "phase": "Phase 2",\n'
            '      "status": "RECRUITING",\n'
            '      "locations": ["City, State"],\n'
            '      "eligibility_match": "Likely",\n'
            '      "eligibility_rationale": "...",\n'
            '      "url": "https://clinicaltrials.gov/study/NCT..."\n'
            "    }\n"
            "  ],\n"
            '  "next_steps": "Brief guidance on what the patient should do next."\n'
            "}"
        ),
        expected_output="A valid JSON object with a 'trials' array and a 'next_steps' string.",
        agent=docs_agent,
        context=[search_task, eligibility_task],
    )

    # ------------------------------------------------------------------
    # Crew
    # ------------------------------------------------------------------

    crew = Crew(
        agents=[search_agent, eligibility_agent, docs_agent],
        tasks=[search_task, eligibility_task, docs_task],
        process=Process.sequential,
        verbose=False,
    )

    result = crew.kickoff()
    raw = str(result).strip()

    # Strip markdown fences if the LLM added them anyway
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logging.warning("ClinicalTrialSearch: docs_agent returned non-JSON — wrapping raw output")
        return {"raw": raw}
