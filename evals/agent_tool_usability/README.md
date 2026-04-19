# Blind Agent Tool Usability Evals

Tests whether LLMs can correctly use Apple Mail MCP tools from descriptions alone.

## How It Works

1. Each scenario is a natural language user prompt (e.g., "What mailboxes do I have?")
2. The LLM receives ONLY the server instructions, tool names, descriptions, and parameter schemas — no code, no docs
3. The LLM plans which tool(s) to call and with what parameters
4. Automated scoring compares against expected tools and critical parameters

## Scoring Rubric

- **PASS (2 pts):** Correct tool(s) with all critical parameters correct
- **PARTIAL (1 pt):** Correct primary tool(s), at least one required param correct
- **FAIL (0 pts):** Wrong tool selected or critical parameters entirely wrong
- **MANUAL:** Requires human judgment (under-specified prompts where the expected behavior is to ask for clarification)

## Files

- `scenarios.py` — Eval scenarios with expected tools and key params
- `run_eval.py` — Runner that sends prompts to LLMs via OpenRouter
- `tool_descriptions.md` — Tool descriptions as shown to the LLM (server instructions + per-tool signatures)
- `server_instructions.md` — Server instructions fragment (also embedded in tool_descriptions.md)
- `results/` — Raw JSON results per model (git-ignored; checked in via `.gitkeep`)

## Running

`openai` is not a runtime dependency of the MCP server — install it locally before running:

```bash
pip install openai
```

Store your OpenRouter key in the macOS Keychain:

```bash
security add-generic-password -a "openrouter" -s "apple-mail-mcp-evals" -w "YOUR_KEY"
```

Then run:

```bash
# Single model, regex scoring
python evals/agent_tool_usability/run_eval.py \
    --model meta-llama/llama-3.3-70b-instruct

# Multiple models in parallel
python evals/agent_tool_usability/run_eval.py \
    --model meta-llama/llama-3.3-70b-instruct qwen/qwen-2.5-72b-instruct

# Variance analysis with LLM scoring
python evals/agent_tool_usability/run_eval.py \
    --model anthropic/claude-sonnet-4 \
    --runs 5 \
    --scorer-model anthropic/claude-3.5-haiku

# Subset of scenarios
python evals/agent_tool_usability/run_eval.py \
    --model meta-llama/llama-3.3-70b-instruct \
    --scenarios 1,2,3
```

Results land in `results/raw_<model_slug>.json`.

## Status

Implemented. Eval framework is a developer tool and is not wired into `make check-all`.
