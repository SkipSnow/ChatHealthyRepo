# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

# LoadProviderData is handled as an async Durable Functions task.
# The Router in function_app.py starts the 'provider_load_orchestrator' directly.
# This file is retained as a placeholder — it is not called by TASK_HANDLERS.


def run_load_provider_data(payload: dict = None) -> dict:
    return {"status": "LoadProviderData successfully called", "inserted": 0}
