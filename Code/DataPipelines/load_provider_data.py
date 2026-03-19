# LoadProviderData is handled as an async Durable Functions task.
# The Router in function_app.py starts the 'provider_load_orchestrator' directly.
# This file is retained as a placeholder — it is not called by TASK_HANDLERS.


def run_load_provider_data(payload: dict = None) -> dict:
    return {"status": "LoadProviderData successfully called", "inserted": 0}
