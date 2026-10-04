"""Single-call baseline experiment.

Run `python3 single_call_experiment.py` in this folder.

This script is a baseline for comparison with the multi-step workflow in main.py:
1. It reads the same OpenRouter configuration, model, and test-file path from config.py.
2. It splits the Markdown test set into news items and processes them one by one.
3. It calls the model only once for each news item, asking the model to search the web and score
   the item using SIFT and Toulmin.
4. It saves the result as Markdown and JSON under the selected results folder.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from config import API_URL, MODEL, REQUEST_TIMEOUT_SECONDS, RESULTS_DIR, TEST_FILE, get_api_key


# =============================================================================
# Part 1: Single-call system prompt
# This is the key difference from main.py: no multi-step workflow, only one model call per item.
# =============================================================================

SYSTEM_PROMPT = """
Assess the reliability of this news item. Build a scoring system using SIFT and the Toulmin method:
below 40 = Not credible, 40 to below 70 = Questionable, 70 to 100 = Credible. Search the web,
research the item, give a reliability score, and briefly list the deductions.
""".strip()


# =============================================================================
# Part 2: Model output schema
# JSON Schema keeps the results stable for reproduction and comparison.
# =============================================================================

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 100},
        "grade": {"type": "string", "enum": ["Credible", "Questionable", "Not credible"]},
        "summary": {"type": "string"},
        "sift_assessment": {"type": "string"},
        "toulmin_assessment": {"type": "string"},
        "deductions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "point": {"type": "string"},
                    "deducted_score": {"type": "number", "minimum": 0, "maximum": 100},
                    "reason": {"type": "string"},
                },
                "required": ["point", "deducted_score", "reason"],
                "additionalProperties": False,
            },
        },
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["title", "url"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "score",
        "grade",
        "summary",
        "sift_assessment",
        "toulmin_assessment",
        "deductions",
        "sources",
    ],
    "additionalProperties": False,
}


# =============================================================================
# Part 3: Reading the Markdown test set
# This matches main.py: supports ## headings and numbered headings such as 1. or ### 1.
# =============================================================================

def read_test_news(path: Path) -> list[dict[str, str]]:
    """Read news items from Markdown."""
    text = path.read_text(encoding="utf-8")
    numbered_heading = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(\d+)\.(?:\*\*)?\s*(.*)$")
    numbered_items: list[dict[str, str]] = []
    current_id: str | None = None
    current_lines: list[str] = []
    for line in text.splitlines():
        match = numbered_heading.match(line)
        if match:
            if current_id and "\n".join(current_lines).strip():
                numbered_items.append(
                    {
                        "news_id": f"News {current_id}",
                        "text": "\n".join(current_lines).strip(),
                    }
                )
            current_id = match.group(1)
            current_lines = []
            rest = match.group(2).strip()
            if rest:
                current_lines.append(rest)
        elif current_id:
            current_lines.append(line)
    if current_id and "\n".join(current_lines).strip():
        numbered_items.append(
            {"news_id": f"News {current_id}", "text": "\n".join(current_lines).strip()}
        )
    if numbered_items:
        return numbered_items

    headings = list(re.finditer(r"(?m)^##[ 	]+(.+?)\s*$", text))
    if not headings:
        content = text.strip()
        return [{"news_id": "news_001", "text": content}] if content else []

    news_items: list[dict[str, str]] = []
    for index, match in enumerate(headings):
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        content = text[start:end].strip()
        if content:
            news_items.append({"news_id": match.group(1).strip(), "text": content})
    return news_items


# =============================================================================
# Part 4: Calling OpenRouter
# Each news item gets exactly one API call with OpenRouter web_search enabled.
# =============================================================================

def compact_citations(annotations: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Compress OpenRouter citation objects into titles and URLs."""
    citations: list[dict[str, str]] = []
    for annotation in annotations or []:
        citation = annotation.get("url_citation", annotation)
        url = citation.get("url") if isinstance(citation, dict) else None
        if url:
            citations.append({"title": citation.get("title", ""), "url": url})
    return citations


def call_single_check(api_key: str, news_id: str, news_text: str) -> dict[str, Any]:
    """Run one web-enabled model judgment for one news item."""
    payload: dict[str, Any] = {
        "model": MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"news_id": news_id, "news_text": news_text},
                    ensure_ascii=False,
                ),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "single_call_news_evaluation",
                "strict": True,
                "schema": RESULT_SCHEMA,
            },
        },
        "provider": {"require_parameters": True},
        "tools": [{"type": "openrouter:web_search"}],
    }

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            response_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter returned HTTP {exc.code}: {error_text[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect to OpenRouter: {exc.reason}") from exc

    raw = json.loads(response_text)
    message = raw["choices"][0]["message"]
    result = json.loads(message.get("content", "{}"))

    usage = raw.get("usage", {})
    server_usage = usage.get("server_tool_use", {}) or {}
    server_usage_details = usage.get("server_tool_use_details", {}) or {}
    web_search_requests = max(
        server_usage.get("web_search_requests", 0) or 0,
        server_usage_details.get("web_search_requests", 0) or 0,
    )

    return {
        "news_id": news_id,
        "model": raw.get("model", MODEL),
        "score": result["score"],
        "grade": result["grade"],
        "summary": result["summary"],
        "sift_assessment": result["sift_assessment"],
        "toulmin_assessment": result["toulmin_assessment"],
        "deductions": result["deductions"],
        "sources": result["sources"],
        "web_search_requests": web_search_requests,
        "citation_log": compact_citations(message.get("annotations", [])),
    }


# =============================================================================
# Part 5: Saving results
# Markdown is easy to read; JSON is convenient for later tables or statistics.
# =============================================================================

def format_markdown(results: list[dict[str, Any]], input_file: Path = TEST_FILE) -> str:
    """Format all baseline results as Markdown."""
    lines = [
        "# Single-Call Baseline Results",
        "",
        f"- Run time: {datetime.now().isoformat(timespec='seconds')}",
        f"- Model: `{MODEL}`",
        f"- Input file: `{input_file.name}`",
        "- Method: each news item receives one web-enabled model call using SIFT and Toulmin scoring.",
        "",
    ]

    for index, item in enumerate(results, start=1):
        lines.extend(
            [
                f"## {index}. {item['news_id']}",
                "",
                f"- Score: {item.get('score')}",
                f"- Grade: {item.get('grade')}",
                f"- Recorded web-search requests: {item.get('web_search_requests', 0)}",
                f"- Overall assessment: {item.get('summary', '')}",
                f"- SIFT assessment: {item.get('sift_assessment', '')}",
                f"- Toulmin assessment: {item.get('toulmin_assessment', '')}",
                "",
                "### Deductions",
                "",
            ]
        )
        if item.get("deductions"):
            for deduction in item.get("deductions", []):
                lines.append(
                    f"- {deduction.get('point')}: deducted {deduction.get('deducted_score')} points. {deduction.get('reason')}"
                )
        elif item.get("status") == "processing_error":
            lines.append(f"- This item failed: {item.get('error')}")
        else:
            lines.append("- No obvious deductions.")

        lines.extend(["", "### Sources", ""])
        if item.get("sources"):
            for source in item.get("sources", []):
                lines.append(f"- {source.get('title')}: {source.get('url')}")
        else:
            lines.append("- The model did not return sources.")
        lines.append("")

    return "\n".join(lines)


def run_single_file(api_key: str, test_file: Path, results_dir: Path) -> list[dict[str, Any]]:
    """Run one test file item by item; each item is recorded before the next starts."""
    news_items = read_test_news(test_file)
    if not news_items:
        raise ValueError(f"No news items were found in {test_file}.")

    results_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for item in news_items:
        print(f"Processing: {item['news_id']}")
        try:
            results.append(call_single_check(api_key, item["news_id"], item["text"]))
        except Exception as exc:
            results.append(
                {
                    "news_id": item["news_id"],
                    "status": "processing_error",
                    "error": str(exc),
                    "score": None,
                    "grade": "Run failed",
                    "summary": str(exc),
                    "sift_assessment": "",
                    "toulmin_assessment": "",
                    "deductions": [],
                    "sources": [],
                    "web_search_requests": 0,
                }
            )
            print(f"This item failed; the error was recorded and processing continues: {exc}")

    md_path = results_dir / "single_call_experiment_results.md"
    json_path = results_dir / "single_call_experiment_results.json"
    md_path.write_text(format_markdown(results, test_file), encoding="utf-8")
    json_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Saved Markdown result: {md_path}")
    print(f"Saved JSON result: {json_path}")
    return results


def parse_args() -> argparse.Namespace:
    """Read optional command-line arguments; defaults still come from config.py."""
    parser = argparse.ArgumentParser(description="Run the single-call baseline experiment.")
    parser.add_argument("--input", type=Path, default=TEST_FILE, help="Test-set Markdown file")
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR, help="Result output folder")
    return parser.parse_args()


def main() -> None:
    """Read the test set, run the baseline item by item, and save results."""
    args = parse_args()
    api_key = get_api_key()
    run_single_file(api_key, args.input, args.output_dir)


if __name__ == "__main__":
    main()
