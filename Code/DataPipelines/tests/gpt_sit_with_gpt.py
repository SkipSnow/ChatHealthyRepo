# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# gpt_sit_with_gpt.py — GPT drives the SIT for gpt_reader.py.
#
# GPT is the tester. It sends commands, Claude executes them.
# GPT must stop and file a bug report the moment it finds a defect.
#
# Usage: python gpt_sit_with_gpt.py

import base64
import importlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
BRAIN_DIR = PROJECT_ROOT / "brain"
TOKEN = os.environ.get("GPT_READER_TOKEN", "")
MAX_ITERATIONS = 100


def gpt_reader_call(config: dict, token: str = None) -> tuple:
    importlib.reload(__import__("gpt_reader"))
    import gpt_reader
    return gpt_reader.handle_gpt_reader(config, token or TOKEN)


def execute_command(raw: dict) -> dict:
    """Parse and execute whatever GPT sends. Very forgiving."""
    # Detect the intent
    action = raw.get("action", raw.get("command", raw.get("type", "")))

    # ── Submit final report ──
    if action == "submit_report":
        return {"done": True, "report": raw.get("report", raw)}

    # ── Check env vars ──
    if action == "check_env":
        keys = raw.get("keys", [])
        return {"configured": {k: bool(os.environ.get(k)) for k in keys}}

    # ── Write test (verify read-only) ──
    if action == "write_mongo_test":
        from pymongo import MongoClient
        conn = os.environ.get(raw.get("connection", "MONGO_READONLY_FRONTEND"), "")
        if not conn:
            return {"error": f"No connection string for: {raw.get('connection')}"}
        try:
            c = MongoClient(conn, serverSelectionTimeoutMS=10000)
            c[raw.get("database", "admin")][raw.get("collection", "write_test")].insert_one({"test": True})
            c.close()
            return {"write_result": "WRITE_SUCCEEDED — NOT READ ONLY"}
        except Exception as e:
            return {"write_result": f"WRITE_BLOCKED: {type(e).__name__}"}

    # ── Direct MongoDB query (for audit, manifest verification) ──
    if action == "query_mongo_direct":
        from pymongo import MongoClient
        conn = os.environ.get(raw.get("connection", "MONGO_FRONTEND_connectionString"), "")
        if not conn:
            return {"error": f"No connection string for: {raw.get('connection')}"}
        try:
            c = MongoClient(conn, serverSelectionTimeoutMS=15000)
            docs = list(c[raw.get("database", "admin")][raw.get("collection", "")].find(
                raw.get("query", {}), limit=min(raw.get("limit", 5), 10)))
            for d in docs:
                d["_id"] = str(d["_id"])
            c.close()
            return {"documents": docs, "count": len(docs)}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    # ── GPTReader call — accept ANY format ──
    # Try to find a GPTReader config in the command
    config = None

    # Format 1: {"action": "ReadArtifact", "project_path": "..."}
    if action in ("ReadArtifact", "Query", "InsertDocument", "Delete", "UpdateDocument"):
        config = raw

    # Format 2: {"action": "call_gpt_reader", "config": {...}}
    elif action == "call_gpt_reader":
        config = raw.get("config") or raw.get("request") or raw.get("payload") or raw.get("body") or {}
        if not config.get("action"):
            # Maybe GPT put the GPTReader action somewhere else
            config["action"] = raw.get("gpt_action") or raw.get("request_action") or raw.get("reader_action") or ""

    # Format 3: {"command": "ReadArtifact", ...}
    elif raw.get("command") in ("ReadArtifact", "Query"):
        config = dict(raw)
        config["action"] = config.pop("command")

    # Format 4: nested under "request" or "payload"
    elif "request" in raw and isinstance(raw["request"], dict):
        config = raw["request"]
    elif "payload" in raw and isinstance(raw["payload"], dict):
        config = raw["payload"]
    elif "config" in raw and isinstance(raw["config"], dict):
        config = raw["config"]

    if config and config.get("action"):
        tok = raw.get("token", None)
        if tok == "wrong-token" or tok == "invalid":
            tok = "wrong-token-12345"
        elif tok == "" or tok == "empty":
            tok = ""
        elif tok == "valid" or tok is None:
            tok = TOKEN
        start = time.time()
        try:
            status, resp = gpt_reader_call(config, tok)
            ms = int((time.time() - start) * 1000)
            # Truncate large content
            if isinstance(resp, dict):
                if "content" in resp and isinstance(resp["content"], str) and len(resp["content"]) > 500:
                    resp["content"] = resp["content"][:500] + f"...[truncated, {len(resp['content'])} total chars]"
                if "documents" in resp and isinstance(resp["documents"], list) and len(resp["documents"]) > 5:
                    resp["documents"] = resp["documents"][:5]
                    resp["_note"] = "truncated to 5 documents"
            return {"http_status": status, "response": resp, "elapsed_ms": ms}
        except Exception as e:
            ms = int((time.time() - start) * 1000)
            return {"http_status": "EXCEPTION", "error": str(e)[:300], "elapsed_ms": ms}

    # Nothing worked
    return {
        "error": "Could not parse your command.",
        "example_read": {"action": "ReadArtifact", "project_path": "brain/manifest/project_manifest.json"},
        "example_query": {"action": "Query", "database": "dev_PublicHealthData", "collection": "SpecialtyMetaData", "query": {}, "limit": 5},
        "example_auth_fail": {"action": "ReadArtifact", "project_path": "test", "token": "wrong-token"},
        "other_actions": ["check_env", "write_mongo_test", "query_mongo_direct", "submit_report"],
    }


def _read_design_requirements() -> list:
    path = PROJECT_ROOT / "brain_v0.1.3_design.json"
    return json.loads(path.read_text(encoding="utf-8")).get("requirements", [])


def _read_governance() -> dict:
    path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "corporate_governance.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _build_system_prompt(governance: dict) -> str:
    return json.dumps({
        "role": "Principal Hands-On SIT Tester",
        "identity": "GPT",
        "project": "ChatHealthy.ai — GPT Reader Service",
        "test_type": "System Integration Test",
        "rules": [
            "You are a hands-on SIT tester. Your ONLY job is to execute tests against a live service.",
            "You are NOT a code reviewer. You are NOT an architect. You are a TESTER.",
            f"You have {MAX_ITERATIONS} iterations. Each iteration: send ONE JSON command. I execute it and return the result.",
            "You MUST test ALL 35 requirements. Every single one.",
            "You MUST return structured output EXACTLY as specified in the user prompt below.",
            "For each requirement in your report: PASS if tested and working, FAIL if tested and broken, UNTESTED if you did not test it.",
            "Any requirement marked UNTESTED is a failure of YOUR testing, not a limitation of the system.",
            "When you find a BUG: log it and KEEP TESTING. Do not stop. Test every requirement regardless of bugs found.",
            "When you have attempted every testable requirement, submit your report. Every requirement MUST have a verdict.",
            "Do NOT submit until you have attempted every testable requirement. Early submission is a testing failure.",
        ],
        "how_to_send_commands": {
            "read_artifact": {"action": "ReadArtifact", "project_path": "brain/manifest/project_manifest.json"},
            "query": {"action": "Query", "database": "dev_PublicHealthData", "collection": "SpecialtyMetaData", "query": {}, "limit": 5},
            "auth_test_bad_token": {"action": "ReadArtifact", "project_path": "test", "token": "wrong-token"},
            "auth_test_empty_token": {"action": "ReadArtifact", "project_path": "test", "token": ""},
            "path_traversal": {"action": "ReadArtifact", "project_path": "../../etc/passwd"},
            "stale_snapshot": {"action": "ReadArtifact", "project_path": "brain/manifest/project_manifest.json", "manifest_snapshot_id": "snapshot_fake_stale"},
            "check_env": {"action": "check_env", "keys": ["MONGO_READONLY_FRONTEND", "MONGO_READONLY_PIPELINE"]},
            "write_test": {"action": "write_mongo_test", "connection": "MONGO_READONLY_FRONTEND", "database": "admin", "collection": "test"},
            "direct_mongo": {"action": "query_mongo_direct", "connection": "MONGO_FRONTEND_connectionString", "database": "admin", "collection": "gpt_reader_audit", "query": {}, "limit": 3},
            "submit": {"action": "submit_report", "report": {"...structured report as specified in user prompt..."}}
        },
        "governance_policies": [p.get("id", "") + ": " + p.get("name", "") for p in governance.get("policies", [])],
        "policy_core": governance.get("policy_core", ""),
    }, indent=2, default=str)


def _build_initial_prompt(requirements: list) -> str:
    parts = [
        "# SIT ASSIGNMENT: GPT Reader Service — ROUND 2 (Remaining Tests Only)",
        "",
        f"You have {MAX_ITERATIONS} iterations to test ONLY the remaining requirements listed below.",
        "DO NOT retest requirements that already passed. DO NOT test requirements marked NOT_TESTABLE.",
        "If you find a bug, log it and KEEP TESTING.",
        "Do NOT submit until you have attempted every REMAINING requirement.",
        "",
        "## ALREADY PASSED — DO NOT RETEST",
        "",
        "These requirements passed in Round 1. Do NOT send any commands to retest them:",
        "- R1: Read any file in manifest — PASS",
        "- R2: Query allowlisted databases — PASS",
        "- R3: Read-only access — PASS",
        "- R4: Bearer token auth — PASS",
        "- R5: Text as text, binary as base64 — PASS",
        "- R6: Manifest fields (project_path, mime_type, encoding, size, content_hash) — PASS",
        "- R8: Query cap 100 docs, default 10 — PASS",
        "- R9: Versioned manifest snapshot — PASS",
        "- R10: Governed read service via MongoDB — PASS",
        "- R13: No access outside project boundary — PASS",
        "- R23: project_path is canonical identifier — PASS",
        "- R24: local_path is optional metadata — PASS",
        "- R26: Audit logging with required fields — PASS",
        "- R27: Snapshot ID + stale returns 409 — PASS",
        "- R29: artifact_type and lifecycle_state in manifest — PASS",
        "- R33: Multi-cluster read access + cluster unavailable error — PASS",
        "- R34: All cluster access through Beta 0.2.1 — PASS",
        "",
        "## NOT TESTABLE — DO NOT ATTEMPT",
        "",
        "These are process, governance, or prompt-design requirements that cannot be tested via service calls:",
        "- R11: Local disk is source of truth (architectural decision)",
        "- R12: System prompt instructs manifest-first (prompt design)",
        "- R15: Token in secure config, never in prompt (ops process)",
        "- R16: Max iterations per assignment (orchestration layer)",
        "- R17: System prompt structure (prompt design)",
        "- R18: Boss not in routine loops (governance)",
        "- R20: Time budget per assignment (orchestration layer)",
        "- R21: System prompt is structured JSON (prompt design)",
        "- R22: max_think_seconds in system prompt (prompt design)",
        "- R25: Artifact creation discipline (GPT behavior rule)",
        "- R28: Update vs create artifacts (GPT behavior rule)",
        "- R30: Multi-storage resolution (DEFERRED to Framework 1.2)",
        "- R31: Pagination / continuation tokens (DEFERRED to Framework 1.2)",
        "- R32: Brain lock mutex (not in gpt_reader.py scope)",
        "",
        "## REMAINING — YOU MUST TEST THESE",
        "",
    ]
    # Only include the requirements that need testing
    remaining_ids = {"R7", "R14", "R19", "R35"}
    for r in requirements:
        if r["id"] in remaining_ids:
            parts.append(f"- **{r['id']}**: {r['requirement']}")
    parts.extend([
        "",
        "## AVAILABLE CONNECTIONS",
        "- MONGO_FRONTEND_connectionString — full access, FrontEnd cluster",
        "- MONGO_READONLY_FRONTEND — read-only user, FrontEnd cluster",
        "- MONGO_READONLY_PIPELINE — read-only user, Pipeline cluster",
        "",
        "The valid GPTReader bearer token is pre-loaded. Use token='wrong-token' or token='' to test auth failures.",
        "",
        "## REQUIRED STRUCTURED OUTPUT",
        "",
        "When you submit your report, it MUST be exactly this structure:",
        '{"action": "submit_report", "report": {',
        '  "report_title": "GPT SIT Report — Round 2 (Remaining)",',
        '  "design_version": "6.3",',
        '  "overall_verdict": "PASS or FAIL",',
        '  "summary": {"already_passed": 17, "not_testable": 14, "tested_this_round": 0, "pass": 0, "fail": 0, "untested": 0},',
        '  "results": [',
        '    {"requirement_id": "R7", "verdict": "PASS|FAIL|UNTESTED", "test_evidence": "exact command and response", "notes": ""},',
        '    {"requirement_id": "R14", "verdict": "...", "test_evidence": "...", "notes": ""},',
        '    {"requirement_id": "R19", "verdict": "...", "test_evidence": "...", "notes": ""},',
        '    {"requirement_id": "R35", "verdict": "...", "test_evidence": "...", "notes": ""}',
        '  ],',
        '  "bugs": [',
        '    {"bug_id": "BUG-001", "severity": "CRITICAL|HIGH|MEDIUM|LOW", "requirement": "R??",',
        '     "title": "short description", "expected": "what should happen",',
        '     "actual": "what actually happened", "impact": "why this matters",',
        '     "reproduction": "exact command that triggered it"}',
        '  ],',
        '  "sign_off": "Round 2 complete. Tested R7, R14, R19, R35. Results: ..."',
        '}}',
        "",
        "Verdicts: PASS = tested and working. FAIL = tested and broken. UNTESTED = you did not test it.",
        "You have exactly 4 requirements to test. Test all 4 before submitting.",
        "",
        "Begin. Send your first test command as JSON.",
    ])
    return "\n".join(parts)


def call_gpt(messages: list) -> tuple:
    import openai
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model="gpt-5.3-chat-latest",
        messages=messages,
    )
    return (response.choices[0].message.content,
            response.usage.prompt_tokens,
            response.usage.completion_tokens)


def generate_pdf(report: dict, output_path: str):
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    def clean(s):
        return str(s).encode("latin-1", errors="replace").decode("latin-1") if s else ""

    pdf = FPDF()
    pdf.add_page("L")
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "ChatHealthy.ai", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "GPT SIT Report - GPT Reader Service", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.set_font("Helvetica", "", 10)
    s = report.get("summary", {})
    pdf.cell(0, 6,
             f"Date: {report.get('date', datetime.now().strftime('%Y-%m-%d'))}  |  "
             f"Iterations: {report.get('iterations_used', '?')}/{MAX_ITERATIONS}  |  "
             f"Verdict: {report.get('overall_verdict', '?')}",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.cell(0, 6,
             f"Pass: {s.get('pass', 0)}  Fail: {s.get('fail', 0)}  Not Testable: {s.get('not_testable', 0)}",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, "Copyright 2026 Skip Snow. All rights reserved.", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.ln(5)

    # Results table
    col_w = [15, 18, 100, 120]
    pdf.set_font("Helvetica", "B", 7)
    for h, w in zip(["Req", "Verdict", "Evidence", "Notes"], col_w):
        pdf.cell(w, 7, h, border=1, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 6)
    for r in report.get("results", []):
        pdf.cell(col_w[0], 6, str(r.get("requirement_id", "")), border=1, align="C")
        pdf.cell(col_w[1], 6, str(r.get("verdict", "")), border=1, align="C")
        pdf.cell(col_w[2], 6, clean(str(r.get("test_evidence", r.get("evidence", "")))[:65]), border=1)
        pdf.cell(col_w[3], 6, clean(str(r.get("notes", ""))[:78]), border=1)
        pdf.ln()

    # Bugs
    bugs = report.get("bugs", [])
    if bugs:
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, f"BUGS FOUND: {len(bugs)}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        for b in bugs:
            pdf.set_font("Helvetica", "B", 8)
            pdf.cell(0, 6, f"{b.get('bug_id', '?')} [{b.get('severity', '?')}] {clean(str(b.get('title', ''))[:80])}",
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", "", 7)
            pdf.cell(0, 5, f"  Expected: {clean(str(b.get('expected', ''))[:120])}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.cell(0, 5, f"  Actual:   {clean(str(b.get('actual', ''))[:120])}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.cell(0, 5, f"  Impact:   {clean(str(b.get('impact', ''))[:120])}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)

    pdf.ln(3)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, clean(str(report.get("sign_off", ""))[:300]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.output(output_path)


def main():
    print("=" * 60)
    print("GPT Reader SIT — GPT Drives, Claude Executes")
    print(f"Max iterations: {MAX_ITERATIONS}")
    print("=" * 60)

    requirements = _read_design_requirements()
    governance = _read_governance()
    system_prompt = _build_system_prompt(governance)
    initial_prompt = _build_initial_prompt(requirements)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": initial_prompt},
    ]

    total_in = total_out = iteration = 0
    execution_log = []
    last_http = None
    repeat_count = 0
    report = None

    while iteration < MAX_ITERATIONS:
        iteration += 1
        print(f"\n--- Iteration {iteration}/{MAX_ITERATIONS} ---")

        gpt_text, tin, tout = call_gpt(messages)
        total_in += tin
        total_out += tout

        try:
            command = json.loads(gpt_text)
        except (json.JSONDecodeError, TypeError):
            messages.append({"role": "assistant", "content": str(gpt_text)})
            messages.append({"role": "user", "content": '{"error": "Invalid JSON. Send a JSON object."}'})
            continue

        if not isinstance(command, dict):
            messages.append({"role": "assistant", "content": str(gpt_text)})
            messages.append({"role": "user", "content": '{"error": "Response must be a JSON object, not a primitive."}'})
            continue

        action = command.get("action", "")
        print(f"  Action: {action}")

        # Check for report submission
        if action == "submit_report" or "report" in command and "results" in command.get("report", {}):
            report = command.get("report", command)
            print("  >> Report submitted!")
            break

        # Execute
        result = execute_command(command)
        execution_log.append({"iteration": iteration, "command": command, "result": result})

        if result.get("done"):
            report = result.get("report", {})
            print("  >> Report submitted!")
            break

        result_summary = json.dumps(result, default=str)[:300]
        print(f"  Result: {result_summary}")

        # Circuit breaker — detect stuck loop
        current_http = result.get("http_status")
        if current_http == last_http and (current_http == 400 or result.get("error")):
            repeat_count += 1
        else:
            repeat_count = 0
        last_http = current_http

        messages.append({"role": "assistant", "content": gpt_text})

        if repeat_count >= 2:
            feedback = json.dumps({
                **result,
                "_WARNING": f"You have sent {repeat_count + 1} commands with the same error. CHANGE YOUR APPROACH. "
                            "Send the GPTReader payload directly, e.g.: "
                            '{"action": "ReadArtifact", "project_path": "brain/manifest/project_manifest.json"}'
            }, default=str)
            repeat_count = 0  # Reset so it warns again if still stuck
        else:
            feedback = json.dumps(result, default=str)

        messages.append({"role": "user", "content": feedback})

    # Ensure report is a dict
    if isinstance(report, str):
        try:
            report = json.loads(report)
        except Exception:
            report = {"overall_verdict": "INCOMPLETE", "raw": report}
    if not isinstance(report, dict):
        report = {"overall_verdict": "INCOMPLETE", "raw": str(report)}

    if not report:
        messages.append({"role": "user", "content":
            f"Testing complete ({iteration} iterations used). Submit your final report NOW using action 'submit_report'."})
        gpt_text, tin, tout = call_gpt(messages)
        total_in += tin
        total_out += tout
        try:
            command = json.loads(gpt_text)
            report = command.get("report", command)
        except Exception:
            report = {"overall_verdict": "INCOMPLETE", "error": "Could not parse final report"}

    # Metadata
    report["iterations_used"] = iteration
    report["tokens"] = {"input": total_in, "output": total_out}
    report["generated_by"] = "GPT-4o (interactive SIT)"
    report["timestamp"] = datetime.now(timezone.utc).isoformat()
    report["execution_log"] = execution_log

    # Save
    json_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "gpt_sit_report.json"
    json_path.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    print(f"\nJSON: {json_path}")

    pdf_path = BRAIN_DIR / "BusinessArtifacts" / "gpt_sit_report.pdf"
    generate_pdf(report, str(pdf_path))
    print(f"PDF: {pdf_path}")

    # Print summary
    s = report.get("summary", {})
    print(f"\n{'=' * 60}")
    print(f"VERDICT: {report.get('overall_verdict', '?')}")
    print(f"Iterations: {iteration}/{MAX_ITERATIONS}  |  Tokens: {total_in:,} in / {total_out:,} out")
    print(f"Pass: {s.get('pass', '?')}  Fail: {s.get('fail', '?')}  Not Testable: {s.get('not_testable', '?')}")

    for r in report.get("results", []):
        print(f"  [{r.get('verdict', '?'):13s}] {r.get('requirement_id', '?')}")

    bugs = report.get("bugs", [])
    if bugs:
        print(f"\nBUGS ({len(bugs)}):")
        for b in bugs:
            print(f"  [{b.get('severity', '?')}] {b.get('bug_id', '?')}: {b.get('title', '?')}")

    return report


if __name__ == "__main__":
    main()
