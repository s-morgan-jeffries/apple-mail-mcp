# Blind Agent Tool Usability Evals

Tests whether LLMs can correctly use Apple Mail MCP tools from descriptions alone.

## How It Works

1. Each scenario is a natural language user prompt (e.g., "What mailboxes do I have?")
2. The LLM receives ONLY tool names, descriptions, and parameter schemas — no code, no docs
3. The LLM plans which tool(s) to call and with what parameters
4. Automated scoring compares against expected tools and critical parameters

## Scoring Rubric

- **PASS (2 pts):** Correct tool(s) with all critical parameters correct
- **PARTIAL (1 pt):** Correct primary tool(s), at least one required param correct
- **FAIL (0 pts):** Wrong tool selected or critical parameters entirely wrong
- **MANUAL:** Requires human judgment

## Files to Create

See the OmniFocus MCP and Apple Calendar MCP sibling projects for the established eval framework:

- `scenarios.py` — Eval scenarios with expected tools and key params
- `run_eval.py` — Runner that sends prompts to LLMs via API
- `tool_descriptions.md` — Tool descriptions as shown to the LLM
- `server_instructions.md` — Server instructions context
- `results/` — Raw JSON results per model + scored summary

## Running

```bash
# Once implemented:
python run_eval.py --model claude-sonnet-4-20250514 --runs 5
```

## Status

Stub — eval framework not yet implemented. See INITIAL_ISSUES.md issue #10.
