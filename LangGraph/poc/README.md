# FindCare Chat — LangGraph POC

Topology-review sandbox for the FindCare `/chat` orchestration.
**Separate from the FindCare codebase on purpose.** This folder is not a POC
that ships; it exists only so we can see the graph in LangGraph Studio and
iterate on the topology before deciding what (if anything) lands in FindCare.

## What's here
- `chat_graph.py` — the graph topology, stub node bodies.
- `langgraph.json` — Studio config, points to `chat_graph.py:chat_graph`.
- `requirements.txt` — `langgraph` + `langgraph-cli[inmem]`.

## Run

```bash
# (one-time) install into an existing Python env that has the FindCare venv,
# or create a fresh one:
pip install -r requirements.txt

# Launch Studio (local dev runtime, no cloud):
langgraph dev
```

`langgraph dev` prints a URL — open it in a browser. Studio renders the graph
interactively. You can step through stub execution from the UI.

## Iteration workflow
1. I edit `chat_graph.py` (topology changes).
2. `langgraph dev` hot-reloads; you refresh the browser to see the new graph.
3. Once the topology is signed off, this folder is reference material — real
   implementation in FindCare is a separate, scoped task.
