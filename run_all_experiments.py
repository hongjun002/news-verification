"""One-command reproduction script.

Run `python3 run_all_experiments.py` in this folder.

The script runs two experiment groups in a fixed order:
1. It first sends the three test sets in test_sets through the multi-step workflow in main.py and
   generates the project-flow test report.
2. It then sends the same three test sets through the single-call baseline in
   single_call_experiment.py and generates the baseline test report.

Each Markdown file is processed item by item. One news item is completed and saved before the next
item starts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import MODEL, PROJECT_DIR, RESULTS_DIR, get_api_key
from main import run_news_file
from single_call_experiment import run_single_file


# =============================================================================
# Part 1: Test files and output folders
# The three test sets are included in the package so the instructor can reproduce the project in Codespaces.
# =============================================================================

TEST_SET_DIR = PROJECT_DIR / "test_sets"
TEST_FILES = [
    TEST_SET_DIR / "news_text_evaluation_A.md",
    TEST_SET_DIR / "news_text_evaluation_B.md",
    TEST_SET_DIR / "news_text_evaluation_C.md",
]

PROJECT_RESULTS_DIR = RESULTS_DIR / "project_flow"
SINGLE_CALL_RESULTS_DIR = RESULTS_DIR / "single_call_flow"


# =============================================================================
# Part 2: Report helpers
# Both workflows save JSON. These helpers extract the key scores, grades, and deductions into Markdown.
# =============================================================================

def load_json(path: Path) -> Any:
    """Read a JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def project_deductions(result: dict[str, Any]) -> list[str]:
    """Extract deduction points from the multi-step workflow result."""
    points = result.get("deduction_analysis", {}).get("deduction_points", [])
    lines: list[str] = []
    for point in points:
        deduction = point.get("deduction_from_article_score", 0)
        if deduction <= 0:
            continue
        claim_id = point.get("claim_id", "")
        category = point.get("category_name", point.get("category", "Deduction item"))
        reason = point.get("reason", "")
        lines.append(f"- {category} ({claim_id}): deducted {deduction} points. {reason}")
    return lines or ["- No obvious deductions."]


def single_call_deductions(result: dict[str, Any]) -> list[str]:
    """Extract deduction points from the single-call baseline result."""
    deductions = result.get("deductions") or []
    if not deductions:
        return ["- No obvious deductions."]
    return [
        f"- {item.get('point')}: deducted {item.get('deducted_score')} points. {item.get('reason')}"
        for item in deductions
    ]


def build_project_report(records: list[dict[str, Any]]) -> str:
    """Build the combined report for the multi-step workflow."""
    lines = [
        "# Multi-Step Project Workflow Test Report",
        "",
        f"- Model: `{MODEL}`",
        "- Workflow: frozen core claims, argument-structure analysis, fact checking, logic supplementation, and programmatic scoring.",
        "- Processing rule: each test set is processed one news item at a time; the next item starts only after the current item is saved.",
        "",
    ]
    for record in records:
        lines.extend([f"## {record['test_file']}", ""])
        for item in record["results"]:
            if item.get("status") != "success":
                lines.extend(
                    [
                        f"### {item.get('news_id')}",
                        "",
                        f"- Status: {item.get('status')}",
                        f"- Error: {item.get('error')}",
                        "",
                    ]
                )
                continue
            result = load_json(record["output_dir"] / item["result_file"])
            audit = result.get("web_search_audit", {})
            stats = result.get("test_statistics", {})
            lines.extend(
                [
                    f"### {item['news_id']}",
                    "",
                    f"- Score: {result.get('article_score_raw')}",
                    f"- Grade: {result.get('article_grade')}",
                    f"- Web-search status: {audit.get('status')}",
                    f"- Confirmed web-search calls: {audit.get('confirmed_call_count')}/{audit.get('online_api_call_count')}",
                    f"- Technical failures: {stats.get('technical_failure_count', 0)}",
                    "",
                    "#### Deductions",
                    "",
                    *project_deductions(result),
                    "",
                ]
            )
    return "\n".join(lines)


def build_single_call_report(records: list[dict[str, Any]]) -> str:
    """Build the combined report for the single-call baseline."""
    lines = [
        "# Single-Call Baseline Test Report",
        "",
        f"- Model: `{MODEL}`",
        "- Workflow: each news item receives one web-enabled model call, and the model uses SIFT and Toulmin to score reliability.",
        "- Processing rule: each test set is processed one news item at a time; the next item starts only after the current item is saved.",
        "",
    ]
    for record in records:
        lines.extend([f"## {record['test_file']}", ""])
        for result in record["results"]:
            lines.extend(
                [
                    f"### {result.get('news_id')}",
                    "",
                    f"- Score: {result.get('score')}",
                    f"- Grade: {result.get('grade')}",
                    f"- Recorded web-search requests: {result.get('web_search_requests', 0)}",
                    "",
                    "#### Deductions",
                    "",
                    *single_call_deductions(result),
                    "",
                ]
            )
    return "\n".join(lines)


# =============================================================================
# Part 3: One-command run
# Run the project workflow first, then the baseline, so the two reports are easy to compare.
# =============================================================================

def main() -> None:
    """Run the three test sets through both workflows."""
    for test_file in TEST_FILES:
        if not test_file.exists():
            raise FileNotFoundError(f"Test set not found: {test_file}")

    api_key = get_api_key()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    project_records: list[dict[str, Any]] = []
    print("Starting the multi-step project workflow.")
    for test_file in TEST_FILES:
        output_dir = PROJECT_RESULTS_DIR / test_file.stem
        print(f"\nProject workflow test set: {test_file.name}")
        summary = run_news_file(api_key, test_file, output_dir)
        project_records.append(
            {"test_file": test_file.name, "output_dir": output_dir, "results": summary}
        )
    project_report = RESULTS_DIR / "project_flow_test_report.md"
    project_report.write_text(build_project_report(project_records), encoding="utf-8")
    print(f"\nProject workflow test report: {project_report}")

    single_records: list[dict[str, Any]] = []
    print("\nStarting the single-call baseline experiment.")
    for test_file in TEST_FILES:
        output_dir = SINGLE_CALL_RESULTS_DIR / test_file.stem
        print(f"\nBaseline test set: {test_file.name}")
        results = run_single_file(api_key, test_file, output_dir)
        single_records.append({"test_file": test_file.name, "results": results})
    single_report = RESULTS_DIR / "single_call_test_report.md"
    single_report.write_text(build_single_call_report(single_records), encoding="utf-8")
    print(f"\nBaseline test report: {single_report}")

    print("\nAll reproduction experiments finished.")


if __name__ == "__main__":
    main()
