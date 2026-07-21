"""Fire ProviderPipelineRunbook webhook for a smoke test.

Payload: state_scope=["VT","DE"], load_mode="full".
Webhook URL is pulled from KV secret PROVIDER-PIPELINE-WEBHOOK-URL.
"""
import json
import subprocess
import sys
import shutil
import urllib.request

_AZ = shutil.which("az")


def kv_secret(vault: str, name: str) -> str:
    p = subprocess.run(
        [_AZ, "keyvault", "secret", "show",
         "--vault-name", vault, "--name", name,
         "--query", "value", "-o", "tsv"],
        capture_output=True, text=True, shell=False,
    )
    if p.returncode != 0:
        sys.exit(f"FATAL: cannot read KV secret {name}: {p.stderr}")
    return p.stdout.strip()


def main():
    url = kv_secret("kv-chpipeline-dev", "PROVIDER-PIPELINE-WEBHOOK-URL")
    if not url:
        sys.exit("FATAL: PROVIDER-PIPELINE-WEBHOOK-URL is empty")
    body = json.dumps({
        "state_scope": ["VT", "DE"],
        "load_mode": "full",
    }).encode("utf-8")
    print(f"POST -> {url[:80]}...")
    print(f"body -> {body.decode('utf-8')}")
    req = urllib.request.Request(
        url,
        method="POST",
        headers={"Content-Type": "application/json"},
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            code = r.status
            resp = r.read().decode("utf-8", errors="replace")[:2000]
            print(f"HTTP {code}")
            print(resp)
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} {exc.reason}")
        try:
            print(exc.read().decode("utf-8", errors="replace")[:2000])
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
