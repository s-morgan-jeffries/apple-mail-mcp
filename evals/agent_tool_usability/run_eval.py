#!/usr/bin/env python3
"""Run blind agent evals against any OpenAI-compatible API.

Sends each scenario prompt (with tool descriptions as context) to a model
and saves responses for scoring. Includes rule-based automated scoring.

Usage:
    # Single model:
    python run_eval.py --model meta-llama/llama-3.3-70b-instruct

    # Multiple models in parallel:
    python run_eval.py --model meta-llama/llama-3.3-70b-instruct qwen/qwen-2.5-72b-instruct

    # Multiple runs for variance analysis:
    python run_eval.py --model meta-llama/llama-3.3-70b-instruct --runs 3

    # Specific scenarios:
    python run_eval.py --model meta-llama/llama-3.3-70b-instruct --scenarios 1,2,3

Requires OPENROUTER_API_KEY in macOS Keychain or environment variable.
Store in Keychain: security add-generic-password -a "openrouter" -s "apple-mail-mcp-evals" -w "KEY"
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI

from scenarios import SCENARIOS

SCRIPT_DIR = Path(__file__).parent
TOOL_DESCRIPTIONS_PATH = SCRIPT_DIR / "tool_descriptions.md"
SERVER_INSTRUCTIONS_PATH = SCRIPT_DIR / "server_instructions.md"

SYSTEM_PROMPT = """You are an email assistant. You have access to the tools described below. \
You have NO access to any codebase, documentation, or external knowledge. \
Based on the server instructions and tool descriptions, plan your response to the user's request.

List the exact tool calls you would make, in order, with all parameters. Explain your reasoning briefly.

## Server Instructions

{server_instructions}

## Tool Descriptions

{tool_descriptions}"""

TOOL_NAMES = [
    "list_mailboxes", "search_messages", "get_message",
    "send_email", "send_email_with_attachments",
    "get_attachments", "save_attachments",
    "mark_as_read", "flag_message",
    "move_messages", "create_mailbox", "delete_messages",
    "reply_to_message", "forward_message",
]

KEYCHAIN_SERVICE = "apple-mail-mcp-evals"
KEYCHAIN_ACCOUNT = "openrouter"


def get_api_key() -> str:
    """Get API key from environment, macOS Keychain, or .env file.

    Lookup order:
        1. OPENROUTER_API_KEY environment variable
        2. macOS Keychain (service: apple-mail-mcp-evals, account: openrouter)
        3. .env file at project root (deprecated — prints warning)

    To store your key in Keychain:
        security add-generic-password -a "openrouter" -s "apple-mail-mcp-evals" -w "YOUR_KEY"
    """
    # 1. Environment variable
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if api_key:
        return api_key

    # 2. macOS Keychain
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # Not on macOS or security command unavailable

    # 3. .env file (deprecated fallback)
    env_path = SCRIPT_DIR.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                api_key = line.split("=", 1)[1].strip()
                if api_key:
                    print("Warning: Reading API key from .env file. "
                          "Prefer macOS Keychain — see run_eval.py docstring.", file=sys.stderr)
                    return api_key

    return ""


def score_response_regex(response_text: str, scenario: dict) -> str:
    """Rule-based automated scoring of a model response.

    Returns: "PASS", "PARTIAL", "FAIL", or "MANUAL"
    """
    expected = scenario["expected"]
    expected_tools = expected.get("tools", [])
    key_params = expected.get("key_params", {})

    # Under-specified scenarios (expected tools is empty) require human judgment
    if not expected_tools:
        return "MANUAL"

    response_lower = response_text.lower()

    # Check which expected tools are mentioned in the response
    tools_found = []
    for tool in expected_tools:
        if re.search(rf'\b{re.escape(tool)}\b', response_lower):
            tools_found.append(tool)

    tools_match = set(expected_tools) == set(tools_found)

    if not tools_match:
        # Check if at least the primary tool (last in list) is present
        if expected_tools and re.search(rf'\b{re.escape(expected_tools[-1])}\b', response_lower):
            # Primary tool found but not all tools — could be PARTIAL
            pass
        else:
            return "FAIL"

    # Check key parameters
    params_found = 0
    params_total = 0
    for tool_name, params in key_params.items():
        for param_key, param_value in params.items():
            params_total += 1
            # Check if the param key is mentioned
            param_key_pattern = re.escape(param_key).replace("_", "[_\\s-]?")
            if re.search(param_key_pattern, response_lower):
                params_found += 1
            # Also check if the value is mentioned (for string values)
            elif isinstance(param_value, str) and param_value and param_value.lower() in response_lower:
                params_found += 1
            elif isinstance(param_value, list):
                # Check if list items are mentioned
                if all(str(v).lower() in response_lower for v in param_value):
                    params_found += 1

    if tools_match and (params_total == 0 or params_found == params_total):
        return "PASS"
    elif tools_match and params_found > 0:
        return "PARTIAL"
    elif tools_match:
        return "PARTIAL"
    else:
        return "PARTIAL"


SCORER_SYSTEM_PROMPT = """You are an evaluator scoring an AI email assistant's tool-call planning.

You will receive a user request, expected tools and parameters, a scoring rubric, and the assistant's response.
Score the response strictly according to the rubric.

Return ONLY a JSON object with exactly two fields:
{"score": "PASS", "justification": "one sentence"}

The score MUST be one of: "PASS", "PARTIAL", or "FAIL".
Do not wrap the JSON in markdown code fences. Do not include any other text."""


def score_response_llm(client: OpenAI, scorer_model: str, response_text: str, scenario: dict) -> dict:
    """LLM-based scoring of a model response.

    Uses an LLM to evaluate the response against the scenario's scoring rubric,
    providing semantic understanding instead of regex pattern matching.

    Returns: {"score": "PASS"|"PARTIAL"|"FAIL"|"MANUAL"|"ERROR", "justification": str}
    """
    expected = scenario["expected"]
    expected_tools = expected.get("tools", [])

    key_params = expected.get("key_params", {})
    user_prompt = (
        f"## User Request\n{scenario['prompt']}\n\n"
        f"## Expected Tools\n{json.dumps(expected_tools) if expected_tools else 'None — the model should ask for clarification, not call tools'}\n\n"
        f"## Expected Key Parameters\n{json.dumps(key_params, indent=2)}\n\n"
        f"## Scoring Rubric\n{scenario['scoring_notes']}\n\n"
        f"## Assistant Response to Score\n{response_text}"
    )

    max_retries = 1
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=scorer_model,
                messages=[
                    {"role": "system", "content": SCORER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0,
                max_tokens=150,
            )
            content = response.choices[0].message.content.strip()
            # Strip markdown fences if present
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(content)
            score = parsed.get("score", "").upper()
            if score in ("PASS", "PARTIAL", "FAIL"):
                return {"score": score, "justification": parsed.get("justification", "")}
            # Invalid score label — retry
        except json.JSONDecodeError:
            if attempt < max_retries:
                continue
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1)
                continue
            return {"score": "ERROR", "justification": f"Scorer error: {e}"}

    return {"score": "ERROR", "justification": "Failed to parse scorer response"}


def run_scenario(client: OpenAI, model: str, scenario: dict, tool_descriptions: str,
                 server_instructions: str = "", scorer_model: str | None = None) -> dict:
    """Run a single scenario and return the result."""
    system = SYSTEM_PROMPT.format(tool_descriptions=tool_descriptions, server_instructions=server_instructions)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": scenario["prompt"]},
        ],
        temperature=0,
        max_tokens=2048,
    )

    content = response.choices[0].message.content
    usage = response.usage

    if scorer_model:
        score_result = score_response_llm(client, scorer_model, content, scenario)
        auto_score = score_result["score"]
    else:
        auto_score = score_response_regex(content, scenario)
        score_result = None

    result = {
        "id": scenario["id"],
        "name": scenario["name"],
        "category": scenario["category"],
        "prompt": scenario["prompt"],
        "response": content,
        "auto_score": auto_score,
        "scoring_notes": scenario["scoring_notes"],
        "safety_critical": scenario["safety_critical"],
        "model": model,
        "input_tokens": usage.prompt_tokens if usage else None,
        "output_tokens": usage.completion_tokens if usage else None,
    }

    if score_result:
        result["score_justification"] = score_result["justification"]
        result["scorer_model"] = scorer_model

    return result


def run_model(client: OpenAI, model: str, scenarios: list, tool_descriptions: str,
              output_dir: Path, runs: int = 1, server_instructions: str = "",
              scorer_model: str | None = None) -> dict:
    """Run all scenarios for a single model. Returns summary dict."""
    model_short = model.split("/")[-1]
    all_results = []
    total_input = 0
    total_output = 0

    for run_num in range(1, runs + 1):
        run_label = f" run {run_num}/{runs}" if runs > 1 else ""
        for i, scenario in enumerate(scenarios, 1):
            print(f"  [{model_short}]{run_label} [{i}/{len(scenarios)}] {scenario['name']}...", end=" ", flush=True)
            try:
                result = run_scenario(client, model, scenario, tool_descriptions, server_instructions, scorer_model)
                result["run"] = run_num
                all_results.append(result)
                if result["input_tokens"]:
                    total_input += result["input_tokens"]
                    total_output += result["output_tokens"]
                print(f"{result['auto_score']}")
            except Exception as e:
                print(f"ERROR: {e}")
                all_results.append({
                    "id": scenario["id"],
                    "name": scenario["name"],
                    "run": run_num,
                    "error": str(e),
                    "auto_score": "ERROR",
                })

    # Save results
    model_slug = model.replace("/", "_")
    output_path = output_dir / f"raw_{model_slug}.json"
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2)

    # Compute score summary
    scores = [r.get("auto_score", "ERROR") for r in all_results if "error" not in r]
    score_map = {"PASS": 2, "PARTIAL": 1, "FAIL": 0, "MANUAL": 0, "ERROR": 0}
    total_points = sum(score_map.get(s, 0) for s in scores)
    max_points = len(scores) * 2

    return {
        "model": model,
        "output_path": str(output_path),
        "scenarios": len(scenarios),
        "runs": runs,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "auto_scores": {
            "PASS": scores.count("PASS"),
            "PARTIAL": scores.count("PARTIAL"),
            "FAIL": scores.count("FAIL"),
            "MANUAL": scores.count("MANUAL"),
            "ERROR": scores.count("ERROR"),
        },
        "auto_score_total": f"{total_points}/{max_points}",
    }


def print_summary(summaries: list, scenarios: list, runs: int, scorer_model: str | None = None):
    """Print a formatted summary table."""
    print(f"\n{'='*70}")
    if scorer_model:
        scorer_short = scorer_model.split("/")[-1]
        print(f"Auto-Scoring Summary (LLM-scored by {scorer_short})")
    else:
        print("Auto-Scoring Summary (rule-based, not model-scored)")
    print(f"{'='*70}")
    for s in sorted(summaries, key=lambda x: x["model"]):
        model_short = s["model"].split("/")[-1]
        sc = s["auto_scores"]
        print(f"\n  {model_short}:")
        manual_str = f", {sc['MANUAL']} MANUAL" if sc['MANUAL'] else ""
        print(f"    Score: {s['auto_score_total']} "
              f"({sc['PASS']} PASS, {sc['PARTIAL']} PARTIAL, {sc['FAIL']} FAIL{manual_str})")
        print(f"    Tokens: {s['total_tokens']}")
        print(f"    Output: {s['output_path']}")

    if runs > 1:
        print(f"\n  Note: {runs} runs per scenario. Scores above are aggregated across all runs.")
        print("  Check raw JSON for per-run breakdown.")


def main():
    parser = argparse.ArgumentParser(description="Run blind agent evals via OpenRouter")
    parser.add_argument("--model", nargs="+",
                        default=["meta-llama/llama-3.3-70b-instruct"],
                        help="Model ID(s) — multiple models run in parallel")
    parser.add_argument("--scenarios", default=None,
                        help="Comma-separated scenario IDs (default: all)")
    parser.add_argument("--runs", type=int, default=1,
                        help="Number of runs per scenario for variance analysis (default: 1)")
    parser.add_argument("--scorer-model", default=None,
                        help="Model for LLM-based scoring (default: regex scorer). "
                             "Example: anthropic/claude-3.5-haiku")
    parser.add_argument("--output", default=str(SCRIPT_DIR / "results"),
                        help="Output directory")
    args = parser.parse_args()

    api_key = get_api_key()
    if not api_key:
        print("Error: OPENROUTER_API_KEY not found.")
        print("Store it in macOS Keychain:")
        print('  security add-generic-password -a "openrouter" -s "apple-mail-mcp-evals" -w "YOUR_KEY"')
        print("Or set OPENROUTER_API_KEY as an environment variable.")
        sys.exit(1)

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    tool_descriptions = TOOL_DESCRIPTIONS_PATH.read_text()
    server_instructions = SERVER_INSTRUCTIONS_PATH.read_text() if SERVER_INSTRUCTIONS_PATH.exists() else ""

    scenarios = SCENARIOS
    if args.scenarios:
        ids = {int(x) for x in args.scenarios.split(",")}
        scenarios = [s for s in SCENARIOS if s["id"] in ids]

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    models = args.model

    scorer_model = args.scorer_model

    print(f"Models: {', '.join(models)}")
    print(f"Scenarios: {len(scenarios)}")
    if args.runs > 1:
        print(f"Runs per scenario: {args.runs}")
    if scorer_model:
        print(f"Scorer: {scorer_model} (LLM)")
    else:
        print("Scorer: regex (rule-based)")
    print(f"Output: {args.output}")
    if len(models) > 1:
        print(f"Running {len(models)} models in parallel")
    print()

    summaries = []
    if len(models) == 1:
        summary = run_model(client, models[0], scenarios, tool_descriptions, output_dir, args.runs, server_instructions, scorer_model)
        summaries.append(summary)
    else:
        with ThreadPoolExecutor(max_workers=len(models)) as executor:
            futures = {
                executor.submit(run_model, client, model, scenarios, tool_descriptions, output_dir, args.runs, server_instructions, scorer_model): model
                for model in models
            }
            for future in as_completed(futures):
                model = futures[future]
                try:
                    summary = future.result()
                    summaries.append(summary)
                except Exception as e:
                    print(f"\n{model} FAILED: {e}")

    print_summary(summaries, scenarios, args.runs, scorer_model)


if __name__ == "__main__":
    main()
