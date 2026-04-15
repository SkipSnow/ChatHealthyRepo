"""GPT Independent Audit — Iterative file request loop."""
import json
import os
import subprocess
import time
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "Code", ".env"))
REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def serve_file(path):
    full_path = path if os.path.isabs(path) else os.path.join(REPO_ROOT, path)
    if not os.path.exists(full_path):
        return f"FILE NOT FOUND: {path}"
    try:
        with open(full_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > 50000:
            return content[:50000] + f"\n...[TRUNCATED at 50K of {len(content)} chars]..."
        return content
    except Exception as e:
        return f"ERROR reading {path}: {e}"


def run_git(cmd):
    try:
        result = subprocess.run(
            cmd.split(), capture_output=True, text=True,
            cwd=REPO_ROOT, encoding="utf-8", errors="replace", timeout=30,
        )
        return (result.stdout or "") + (result.stderr or "")
    except Exception as e:
        return f"ERROR: {e}"


def main():
    with open(os.path.join(REPO_ROOT, "brain/machine_artifacts/content/project_manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)

    with open(os.path.join(REPO_ROOT, "test_output/lineage/04_final_report.json"), encoding="utf-8") as f:
        prior_audit = json.load(f)

    system_prompt = (
        "You are a senior software auditor conducting a FRESH, INDEPENDENT audit of the ChatHealthy.ai codebase.\n\n"
        "You are given:\n"
        "1. A project manifest listing all files\n"
        "2. A prior audit report (13 findings) produced by another AI (Claude)\n\n"
        "YOUR PROCESS:\n"
        "- Study the manifest to understand the project structure\n"
        "- Request specific files you need to read by responding with JSON:\n"
        '  {"action": "request_files", "files": ["path/to/file1", "path/to/file2"]}\n'
        "- Request git history by responding with:\n"
        '  {"action": "git_command", "command": "git log --oneline -20 -- path/to/file"}\n'
        "- After gathering enough evidence, produce your findings:\n"
        '  {"action": "findings", "data": { ... }}\n\n'
        "You MUST request and read at MINIMUM:\n"
        "- All brain artifacts in brain/machine_artifacts/content/\n"
        "- Key frontend files (App.tsx, FindCareApp.tsx, ChatWindow.tsx)\n"
        "- Backend main.py and all domain service files\n"
        "- Git history for files where you suspect capability loss\n"
        "- ai_operations.json for dead code\n"
        "- bugs.json for cross-reference\n\n"
        "Do thorough work. Request files in batches of 3-5. Read them carefully.\n"
        "After at least 10 rounds of file requests, produce your independent findings.\n"
        "Then compare against the prior audit.\n\n"
        "Final findings schema:\n"
        "{\n"
        '  "action": "findings",\n'
        '  "data": {\n'
        '    "reviewer": "GPT-4.1",\n'
        '    "review_date": "2026-04-14",\n'
        '    "files_reviewed": ["list"],\n'
        '    "independent_findings": [\n'
        '      {"finding_number": N, "title": "...", "category": "undocumented_loss|documented_unplanned|intentional_replacement", "priority": "critical|high|medium|low", "evidence": "...", "description": "..."}\n'
        "    ],\n"
        '    "comparison_with_prior_audit": [\n'
        '      {"prior_finding": N, "verdict": "AGREE|DISPUTE|UPGRADE|DOWNGRADE", "reasoning": "..."}\n'
        "    ],\n"
        '    "missed_by_prior_audit": [\n'
        '      {"title": "...", "priority": "...", "evidence": "..."}\n'
        "    ],\n"
        '    "overall_verdict": "accept_prior_audit|revise_with_corrections",\n'
        '    "corrections": ["..."]\n'
        "  }\n"
        "}\n"
    )

    manifest_text = json.dumps(manifest, indent=2)
    if len(manifest_text) > 30000:
        manifest_text = manifest_text[:30000] + "\n...[TRUNCATED]..."

    initial_msg = (
        f"Here is the project manifest:\n\n{manifest_text}\n\n"
        f"Here is the prior audit report (compare AFTER your own analysis):\n\n"
        f"{json.dumps(prior_audit, indent=2)}\n\n"
        f"Begin your audit. Request the files you need."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": initial_msg},
    ]

    log_path = os.path.join(REPO_ROOT, "test_output/lineage/05_gpt_audit_log.txt")
    log = open(log_path, "w", encoding="utf-8")
    log.write(f"GPT Independent Audit — Started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    log.write(f"{'=' * 80}\n\n")

    total_tokens_in = 0
    total_tokens_out = 0

    for iteration in range(150):
        print(f"\n--- Iteration {iteration + 1} ---")
        log.write(f"\n--- Iteration {iteration + 1} ---\n")

        response = client.chat.completions.create(
            model="gpt-4.1",
            temperature=0,
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=8000,
        )

        total_tokens_in += response.usage.prompt_tokens
        total_tokens_out += response.usage.completion_tokens

        gpt_msg = response.choices[0].message.content
        messages.append({"role": "assistant", "content": gpt_msg})

        try:
            action = json.loads(gpt_msg)
        except json.JSONDecodeError:
            print(f"  Non-JSON response, ending")
            log.write(f"  Non-JSON: {gpt_msg[:500]}\n")
            break

        action_type = action.get("action", "")
        log.write(f"  Action: {action_type}\n")

        if action_type == "request_files":
            files = action.get("files", [])
            print(f"  Requesting {len(files)} files: {[os.path.basename(f) for f in files]}")
            log.write(f"  Files: {files}\n")

            parts = []
            for fpath in files:
                content = serve_file(fpath)
                parts.append(f"=== {fpath} ===\n{content}")
                log.write(f"    Served: {fpath} ({len(content)} chars)\n")

            messages.append({"role": "user", "content": "\n\n".join(parts)})

        elif action_type == "git_command":
            cmd = action.get("command", "")
            print(f"  Git: {cmd}")
            log.write(f"  Git: {cmd}\n")

            output = run_git(cmd)
            log.write(f"    Output: {len(output)} chars\n")
            messages.append({"role": "user", "content": f"Git output:\n{output}"})

        elif action_type == "findings":
            print(f"  GPT produced findings!")
            log.write(f"  FINDINGS PRODUCED\n")

            findings = action.get("data", {})
            out_path = os.path.join(REPO_ROOT, "test_output/lineage/05_gpt_independent_review.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(findings, f, indent=2, ensure_ascii=False)

            print(f"\n{'=' * 60}")
            print(f"GPT Audit Complete after {iteration + 1} iterations")
            print(f"Tokens: in={total_tokens_in:,} out={total_tokens_out:,}")
            print(f"Files reviewed: {len(findings.get('files_reviewed', []))}")
            print(f"Independent findings: {len(findings.get('independent_findings', []))}")
            print(f"Verdict: {findings.get('overall_verdict', '?')}")
            print(f"Missed by prior: {len(findings.get('missed_by_prior_audit', []))}")

            for fr in findings.get("comparison_with_prior_audit", []):
                print(f"  F{fr.get('prior_finding', '?')}: {fr.get('verdict', '?')}")

            log.write(f"\nCompleted in {iteration + 1} iterations\n")
            log.write(f"Tokens: in={total_tokens_in:,} out={total_tokens_out:,}\n")
            break

        else:
            print(f"  Unknown action: {action_type}")
            log.write(f"  Unknown: {action_type}\n")
            messages.append({"role": "user", "content": "Continue. Request more files or produce findings."})

        # Trim if too long
        total_msg_len = sum(len(m["content"]) for m in messages)
        if total_msg_len > 800000:
            print(f"  Trimming ({total_msg_len:,} chars)")
            messages = messages[:2] + messages[-20:]

    log.write(f"\nTotal tokens: in={total_tokens_in:,} out={total_tokens_out:,}\n")
    log.close()
    print(f"\nLog: {log_path}")


if __name__ == "__main__":
    main()
