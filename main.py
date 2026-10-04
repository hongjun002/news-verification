"""Main program for the news verification course project.

Run `python3 main.py` in this folder.
The program reads the Markdown test file, processes each news item separately, and saves JSON results.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import (
    API_URL,
    BATCH_SIZE,
    MODEL,
    REQUEST_TIMEOUT_SECONDS,
    RESULTS_DIR,
    TEST_FILE,
    get_api_key,
)


# =============================================================================
# Part 1: Structured output schemas
# These JSON Schemas restrict the model to fields that the program can read.
# =============================================================================

CLAIM_TYPES = ["fact", "causal", "prediction", "opinion"]
INITIAL_LOGIC_LABELS = ["valid", "invalid", "insufficient"]
FACT_COMPARISONS = ["consistent", "contradictory", "mixed", "not_found"]
CONTEXT_MATCH_LABELS = ["matched", "mismatched", "uncertain"]
FACT_EVIDENCE_OUTCOMES = [
    "supported",
    "refuted",
    "no_relevant_evidence",
    "conflicting_evidence",
    "low_quality_sources_only",
]
LOGIC_EVIDENCE_OUTCOMES = [
    "supports_logic",
    "refutes_logic",
    "no_relevant_evidence",
    "conflicting_evidence",
    "low_quality_sources_only",
]
SOURCE_QUALITY_LABELS = ["reliable", "mixed", "weak", "none"]
EXPECTED_SOURCE_QUALITY = {
    "supported": "reliable",
    "refuted": "reliable",
    "supports_logic": "reliable",
    "refutes_logic": "reliable",
    "no_relevant_evidence": "none",
    "conflicting_evidence": "mixed",
    "low_quality_sources_only": "weak",
}

SOURCES_SCHEMA = {
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
}

CLAIMS_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "claim_text": {"type": "string"},
                    "claim_type": {"type": "string", "enum": CLAIM_TYPES},
                    "supporting_details": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["claim_text", "claim_type", "supporting_details"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}

ARGUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "analyses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "logic_chain": {"type": "string"},
                    "fact_premises": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "initial_logic": {
                        "type": "string",
                        "enum": INITIAL_LOGIC_LABELS,
                    },
                    "reason": {"type": "string"},
                },
                "required": [
                    "claim_id",
                    "logic_chain",
                    "fact_premises",
                    "initial_logic",
                    "reason",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["analyses"],
    "additionalProperties": False,
}

FACT_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "claim_id": {"type": "string"},
                    "evidence_finding": {"type": "string"},
                    "comparison": {
                        "type": "string",
                        "enum": FACT_COMPARISONS,
                    },
                    "source_quality": {
                        "type": "string",
                        "enum": SOURCE_QUALITY_LABELS,
                    },
                    "context_match": {
                        "type": "string",
                        "enum": CONTEXT_MATCH_LABELS,
                    },
                    "reason": {"type": "string"},
                    "sources": SOURCES_SCHEMA,
                },
                "required": [
                    "item_id",
                    "claim_id",
                    "evidence_finding",
                    "comparison",
                    "source_quality",
                    "context_match",
                    "reason",
                    "sources",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

LOGIC_SUPPLEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "evidence_outcome": {
                        "type": "string",
                        "enum": LOGIC_EVIDENCE_OUTCOMES,
                    },
                    "source_quality": {
                        "type": "string",
                        "enum": SOURCE_QUALITY_LABELS,
                    },
                    "reason": {"type": "string"},
                    "sources": SOURCES_SCHEMA,
                },
                "required": [
                    "claim_id",
                    "evidence_outcome",
                    "source_quality",
                    "reason",
                    "sources",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


# =============================================================================
# Part 2: Prompts for the API calls
# Fact checking and logic supplementation use different prompts, inputs, and labels.
# =============================================================================

CLAIM_SYSTEM_PROMPT = """
Extract and classify 1 to 3 core claims from the news item.
Only extract claims that determine the article's main conclusion or overall credibility.
Use a counterfactual standard: if the statement were false but would not materially change the
article's main conclusion, do not treat it as a core claim.
Core claims must be non-duplicative and independent from one another. Details about the same
event, such as time, location, configuration, performance numbers, or other specifics, usually
must not be split into separate core claims.
Claim types must be:
- fact: a factual statement whose truth can be checked directly;
- causal: a causal inference;
- prediction: a claim about the future;
- opinion: an evaluative, normative, or argumentative claim.
Do not discard checkable details related to a core claim that do not meet the core-claim standard.
Put them into that claim's supporting_details. Return an empty array if there are none. Do not
repeat the same detail under multiple claims.
Preserve the factual meaning of the original text faithfully. Do not rewrite or correct numbers,
dates, organizations, people, directions of increase or decrease, negations, or whether something
is predicted or has already happened. If one sentence contains multiple checkable facts, split them
into separate supporting_details so each item can be checked independently.
Do not browse the web, do not verify truth, and do not analyze argument structure. Only extract and
classify claims.
""".strip()

ARGUMENT_SYSTEM_PROMPT = """
Analyze causal, predictive, or opinion claims that have already been frozen by the program.
Strictly use the original news text to build the chain: factual premises -> reasoning relation ->
core claim. Extract objectively checkable factual premises and make an initial logic judgment.
Do not add premises that are not present in the original text. Return the same claim_id from the input.
"The news says so" is not a valid reason for the logic to hold. A small number of companies or
individual cases cannot directly prove a broad trend, and co-occurrence cannot directly prove
causation. If the text uses strong conclusions such as proves, causes, drives, or confirms a trend,
but the factual premises do not support that strength, mark the logic as insufficient or invalid.
valid means the factual premises are actually sufficient to derive the core claim.
initial_logic must be one of: valid, invalid, insufficient.
""".strip()

FACT_CHECK_SYSTEM_PROMPT = """
You are a factual proposition checker. This request checks only factual claims or factual premises;
do not evaluate reasoning relations.
This request contains exactly one item, and you must use web search. Use the full news_text to
identify the checked item's context before searching. Build the search query with the checked
statement plus relevant anchors such as entity, event/report name, publication date, year, quarter,
and disputed number. If the first result belongs to a different context, refine the query once
inside this same request before judging.
Prioritize official documents, authoritative institutions, primary materials, and reliable news
sources. Government data, regulatory filings, company announcements, financial reports, original
research, and reliable news reports may all count as reliable sources. One reliable source directly
relevant to the proposition is enough to support it; multiple sources or independent third-party
corroboration are not required.
The absence of independent third-party data is not itself an error and must not be used as a
standalone deduction reason. Mark an item as refuted only when reliable evidence clearly contradicts
the original text. If the search does not find sufficient relevant evidence, mark it as
no_relevant_evidence, not refuted. Unsigned reposts, content farms, and social-media rumors are weak
sources.
Keep item_id and claim_id unchanged.
First write, in evidence_finding, the fact actually stated by the reliable source. Then compare the
original text item by item: subject, time, number, unit, direction of increase/decrease, negation,
and whether the statement is predicted or already happened.
Before assigning comparison, decide context_match:
- matched: the evidence refers to the same entity and the same relevant event, report, year,
  quarter, date range, or time period as the checked item;
- mismatched: the evidence refers to a different event, report, year, quarter, date range, or time
  period;
- uncertain: the evidence context cannot be established clearly.
Only matched evidence may support or refute the item. If the evidence is mismatched or uncertain,
set comparison to not_found and source_quality to none.
comparison must be:
- consistent: reliable sources are substantively consistent with the checked item;
- contradictory: reliable sources substantively contradict the checked item;
- mixed: reliable sources conflict with one another;
- not_found: sufficient reliable evidence was not found, or only weak sources were found.
Different numbers, opposite directions, wrong organizations, or a prediction written as an already
occurred fact must be marked contradictory. Do not mark an item consistent merely because the broad
subject or event is similar. If any substantive part of a compound factual statement is wrong, the
item cannot be consistent. source_quality must map as follows: consistent/contradictory = reliable,
mixed = mixed; not_found = none when there are no sources, or weak when only weak sources were found.
Give a concise reason and source title/URL for each item. The program will derive the final evidence
label from comparison.
""".strip()

LOGIC_SUPPLEMENT_SYSTEM_PROMPT = """
You are a logic-relation supplement checker. Do not re-check factual premises and do not rewrite
the core claim.
This request contains exactly one item. It applies only to an original logic chain that was initially
judged as insufficient. You must use web search to look for the missing relationship evidence, then
judge whether the reasoning relation holds. Government data, regulatory filings, company
announcements, financial reports, original research, and reliable news reports may all count as
reliable support. One directly relevant reliable source is enough to support the relation; multiple
sources or independent third-party corroboration are not required. The absence of independent
third-party data is not itself proof that the logic relation is wrong and must not be used as a
standalone deduction reason. Mark refutes_logic only when reliable evidence clearly shows the
original logic relation is wrong. If sufficient evidence is not found, mark no_relevant_evidence.
Keep claim_id unchanged.
evidence_outcome must be:
- supports_logic: reliable evidence supports the logic relation;
- refutes_logic: reliable evidence shows the logic relation is wrong;
- no_relevant_evidence: no relevant evidence was found after search;
- conflicting_evidence: reliable sources conflict with one another;
- low_quality_sources_only: only low-quality sources were found.
source_quality must map as follows: supports_logic/refutes_logic = reliable,
no_relevant_evidence = none, conflicting_evidence = mixed,
low_quality_sources_only = weak.
Give a concise reason and source title/URL for each item.
""".strip()


# =============================================================================
# Part 3: Calling OpenRouter
# Non-search calls do not pass tools; search calls use only openrouter:web_search.
# =============================================================================


def compact_citations(annotations: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Compress OpenRouter citation objects into titles and URLs for saving."""
    citations: list[dict[str, str]] = []
    for annotation in annotations or []:
        citation = annotation.get("url_citation", annotation)
        url = citation.get("url") if isinstance(citation, dict) else None
        if url:
            citations.append({"title": citation.get("title", ""), "url": url})
    return citations


def call_openrouter(
    api_key: str,
    schema_name: str,
    schema: dict[str, Any],
    system_prompt: str,
    user_content: dict[str, Any],
    request_type: str,
    item_ids: list[str],
    attempt: int,
    use_web_search: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Send one OpenRouter request and return structured content plus a compact call log."""
    payload: dict[str, Any] = {
        "model": MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(user_content, ensure_ascii=False),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema,
            },
        },
        "provider": {"require_parameters": True},
    }
    if use_web_search:
        payload["tools"] = [{"type": "openrouter:web_search"}]

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
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            response_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"OpenRouter returned HTTP {exc.code}: {error_text[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect to OpenRouter: {exc.reason}") from exc

    raw = json.loads(response_text)
    message = raw["choices"][0]["message"]
    content = message.get("content", "")
    if not isinstance(content, str):
        raise ValueError("The model response content is not a JSON string.")
    parsed = json.loads(content)

    usage = raw.get("usage", {})
    # OpenRouter documents server_tool_use; some responses use server_tool_use_details.
    # Read both so successful searches are not mistaken for missing searches.
    server_usage = usage.get("server_tool_use", {}) or {}
    server_usage_details = usage.get("server_tool_use_details", {}) or {}
    web_search_requests = max(
        server_usage.get("web_search_requests", 0) or 0,
        server_usage_details.get("web_search_requests", 0) or 0,
    )
    citation_log = compact_citations(message.get("annotations", []))
    if not use_web_search:
        search_confirmed = None
        confirmation_basis = "not_applicable"
    elif web_search_requests > 0:
        search_confirmed = True
        confirmation_basis = "usage.web_search_requests"
    else:
        search_confirmed = False
        confirmation_basis = "no_web_search_request_record"

    log = {
        "request_type": request_type,
        "attempt": attempt,
        "item_ids": item_ids,
        "response_id": raw.get("id"),
        "model": raw.get("model", MODEL),
        "usage": usage,
        "web_search_requests": web_search_requests,
        "citation_log": citation_log,
        # Search count determines whether search happened; citations are saved as evidence logs only.
        "web_search_confirmed": search_confirmed,
        "web_search_confirmation_basis": confirmation_basis,
    }
    return parsed, log


# =============================================================================
# Part 4: Reading the Markdown test set
# Each second-level heading (##) and following body text form one news item.
# =============================================================================


def read_test_news(path: Path) -> list[dict[str, str]]:
    """Read news items from Markdown; supports ## headings and numbered headings such as 1. or ### 1."""
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

    headings = list(re.finditer(r"(?m)^##[ \t]+(.+?)\s*$", text))
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


def safe_filename(name: str) -> str:
    """Convert a news title into a safe JSON filename."""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", name).strip("_")
    return cleaned[:80] or "news"


def normalized_statement(text: str) -> str:
    """Remove punctuation and whitespace to avoid checking or deducting the same detail twice."""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", text).lower()


def number_tokens(text: str) -> set[str]:
    """Extract numbers and remove thousands separators to detect invented numeric changes."""
    return {
        token.replace(",", "")
        for token in re.findall(r"\d[\d,]*(?:\.\d+)?", text)
    }


# =============================================================================
# Part 5: Freeze core claims and analyze argument structure
# Once claim_id, claim_text, and claim_type are created, later steps read them only from frozen_claims.
# =============================================================================


def extract_and_freeze_claims(
    api_key: str, news_text: str, api_logs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """API call 1: extract and classify; retry once if numbers not in the source appear."""
    source_numbers = number_tokens(news_text)
    claims: list[dict[str, Any]] = []
    for attempt in (1, 2):
        user_content: dict[str, Any] = {"news_text": news_text}
        if attempt == 2:
            user_content["retry_instruction"] = (
                "The previous result contained numbers not present in the original text. Re-extract and strictly preserve the original numbers."
            )
        data, log = call_openrouter(
            api_key,
            "extract_claims",
            CLAIMS_SCHEMA,
            CLAIM_SYSTEM_PROMPT,
            user_content,
            "extract_claims",
            ["news"],
            attempt,
        )
        api_logs.append(log)

        claims = data.get("claims", [])
        if not 1 <= len(claims) <= 3:
            raise ValueError("The number of core claims must be between 1 and 3.")
        extracted_parts: list[str] = []
        for claim in claims:
            extracted_parts.append(str(claim.get("claim_text", "")))
            extracted_parts.extend(
                str(detail) for detail in claim.get("supporting_details", [])
            )
        extracted_text = " ".join(extracted_parts)
        unexpected_numbers = number_tokens(extracted_text) - source_numbers
        if not unexpected_numbers:
            break
        if attempt == 2:
            raise ValueError(
                "Technical failure: claim extraction contained numbers not present in the original text twice in a row."
            )

    frozen: list[dict[str, Any]] = []
    for index, claim in enumerate(claims, start=1):
        claim_text = str(claim.get("claim_text", "")).strip()
        claim_type = claim.get("claim_type")
        if not claim_text or claim_type not in CLAIM_TYPES:
            raise ValueError("Core claim text or type is invalid.")
        supporting_details = [
            str(detail).strip()
            for detail in claim.get("supporting_details", [])
            if str(detail).strip()
        ]
        frozen.append(
            {
                "claim_id": f"claim_{index}",
                "claim_text": claim_text,
                "claim_type": claim_type,
                "supporting_details": list(dict.fromkeys(supporting_details)),
            }
        )
    return frozen


def analyze_arguments(
    api_key: str,
    news_text: str,
    frozen_claims: list[dict[str, Any]],
    api_logs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """API call 2: analyze only argumentative claims and validate returned claim_id values."""
    # Later API calls receive only the three frozen identity fields; supporting_details are handled separately by the program.
    argument_claims = [
        {
            "claim_id": c["claim_id"],
            "claim_text": c["claim_text"],
            "claim_type": c["claim_type"],
        }
        for c in frozen_claims
        if c["claim_type"] != "fact"
    ]
    if not argument_claims:
        return {}

    data, log = call_openrouter(
        api_key,
        "analyze_arguments",
        ARGUMENT_SCHEMA,
        ARGUMENT_SYSTEM_PROMPT,
        {"news_text": news_text, "frozen_claims": argument_claims},
        "analyze_arguments",
        [c["claim_id"] for c in argument_claims],
        1,
    )
    api_logs.append(log)

    returned = data.get("analyses", [])
    by_id: dict[str, list[dict[str, Any]]] = {}
    for item in returned:
        by_id.setdefault(item.get("claim_id", ""), []).append(item)

    analyses: dict[str, dict[str, Any]] = {}
    for frozen in argument_claims:
        claim_id = frozen["claim_id"]
        matches = by_id.get(claim_id, [])
        if len(matches) != 1:
            raise ValueError(f"Argument analysis did not uniquely return the frozen {claim_id}.")
        item = matches[0]
        if item.get("initial_logic") not in INITIAL_LOGIC_LABELS:
            raise ValueError(f"The initial logic label for {claim_id} is invalid.")

        # premise_id is also generated by the program so the model cannot change item identity.
        premises = [
            {
                "premise_id": f"{claim_id}_premise_{i}",
                "premise_text": str(text).strip(),
            }
            for i, text in enumerate(item.get("fact_premises", []), start=1)
            if str(text).strip()
        ]
        analyses[claim_id] = {
            "claim_id": claim_id,
            "logic_chain": item.get("logic_chain", ""),
            "fact_premises": premises,
            "initial_logic": item["initial_logic"],
            "initial_logic_reason": item.get("reason", ""),
        }
    return analyses


# =============================================================================
# Part 6: One-item web checks with one targeted retry
# Each request checks one item; multiple one-item requests run concurrently. Only failed or missing items are retried once.
# =============================================================================


def chunks(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split items into small batches using BATCH_SIZE."""
    return [items[i : i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]


def run_check_round(
    items: list[dict[str, Any]],
    attempt: int,
    id_field: str,
    result_field: str,
    allowed_labels: list[str],
    batch_caller: Callable[[list[dict[str, Any]], int], tuple[dict[str, Any], dict[str, Any]]],
    api_logs: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Run one concurrent batch round and validate IDs, completeness, and output labels."""
    accepted: dict[str, dict[str, Any]] = {}
    failed: dict[str, str] = {}
    batches = chunks(items)
    if not batches:
        return accepted, failed

    with ThreadPoolExecutor(max_workers=len(batches)) as executor:
        future_map = {
            executor.submit(batch_caller, batch, attempt): batch for batch in batches
        }
        for future in as_completed(future_map):
            batch = future_map[future]
            batch_ids = [item[id_field] for item in batch]
            try:
                data, log = future.result()
                api_logs.append(log)
                returned = data.get(result_field, [])
            except Exception as exc:
                api_logs.append(
                    {
                        "request_type": "fact_check"
                        if id_field == "item_id"
                        else "logic_supplement",
                        "attempt": attempt,
                        "item_ids": batch_ids,
                        "error": str(exc),
                    }
                )
                for item_id in batch_ids:
                    failed[item_id] = str(exc)
                continue

            # If search count is 0, the model did not perform the required web search.
            # First-round failures enter the one targeted retry; second-round failures become not_searched.
            if log.get("web_search_confirmed") is not True:
                for item_id in batch_ids:
                    failed[item_id] = "not_searched"
                continue

            grouped: dict[str, list[dict[str, Any]]] = {}
            for result in returned if isinstance(returned, list) else []:
                grouped.setdefault(result.get(id_field, ""), []).append(result)

            expected = {item[id_field]: item for item in batch}
            for item_id, original in expected.items():
                matches = grouped.get(item_id, [])
                if len(matches) != 1:
                    failed[item_id] = "Result missing or duplicated"
                    continue
                result = matches[0]
                if result.get("claim_id") != original["claim_id"]:
                    failed[item_id] = "claim_id does not match the frozen record"
                    continue
                outcome = result.get("evidence_outcome")
                if outcome not in allowed_labels:
                    failed[item_id] = "Evidence outcome label is invalid"
                    continue
                if result.get("source_quality") != EXPECTED_SOURCE_QUALITY[outcome]:
                    failed[item_id] = "Evidence outcome and source quality are inconsistent"
                    continue
                accepted[item_id] = result
    return accepted, failed


def run_with_one_retry(
    items: list[dict[str, Any]],
    id_field: str,
    result_field: str,
    allowed_labels: list[str],
    batch_caller: Callable[[list[dict[str, Any]], int], tuple[dict[str, Any], dict[str, Any]]],
    success_builder: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    failure_builder: Callable[[dict[str, Any], str], dict[str, Any]],
    api_logs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Process all items first, then call only failed or missing items once more."""
    if not items:
        return []

    accepted, failed = run_check_round(
        items,
        1,
        id_field,
        result_field,
        allowed_labels,
        batch_caller,
        api_logs,
    )

    item_by_id = {item[id_field]: item for item in items}
    retry_items = [item_by_id[item_id] for item_id in failed]
    if retry_items:
        retry_accepted, retry_failed = run_check_round(
            retry_items,
            2,
            id_field,
            result_field,
            allowed_labels,
            batch_caller,
            api_logs,
        )
        accepted.update(retry_accepted)
        failed = retry_failed

    final_results: list[dict[str, Any]] = []
    for item in items:
        item_id = item[id_field]
        if item_id in accepted:
            final_results.append(success_builder(item, accepted[item_id]))
        else:
            final_results.append(
                failure_builder(item, failed.get(item_id, "No valid result after retry"))
            )
    return final_results


def run_fact_checks(
    api_key: str,
    news_text: str,
    items: list[dict[str, Any]],
    api_logs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fact-check request: distinguish support, refutation, no evidence, conflict, and weak sources."""

    comparison_map = {
        "consistent": "supported",
        "contradictory": "refuted",
        "mixed": "conflicting_evidence",
        "not_found": "no_relevant_evidence",
    }

    def batch_caller(batch: list[dict[str, Any]], attempt: int):
        data, log = call_openrouter(
            api_key,
            "fact_check_results",
            FACT_CHECK_SCHEMA,
            FACT_CHECK_SYSTEM_PROMPT,
            {"news_text": news_text, "items": batch},
            "fact_check",
            [item["item_id"] for item in batch],
            attempt,
            use_web_search=True,
        )
        # The final evidence label is generated by the program from comparison; free-form model labels are not accepted.
        for result in data.get("results", []):
            if result.get("context_match") != "matched":
                result["comparison"] = "not_found"
                result["source_quality"] = "none"
            comparison = result.get("comparison")
            outcome = comparison_map.get(comparison)
            if comparison == "not_found" and result.get("source_quality") == "weak":
                outcome = "low_quality_sources_only"
            result["evidence_outcome"] = outcome
        return data, log

    score_map = {
        "supported": 100,
        "refuted": 0,
        "no_relevant_evidence": 75,
        "conflicting_evidence": 50,
        "low_quality_sources_only": 65,
    }
    verdict_map = {
        "supported": "supported",
        "refuted": "refuted",
        "no_relevant_evidence": "insufficient_evidence",
        "conflicting_evidence": "insufficient_evidence",
        "low_quality_sources_only": "insufficient_evidence",
    }

    def success_builder(original: dict[str, Any], result: dict[str, Any]):
        outcome = result["evidence_outcome"]
        return {
            **original,
            "execution_status": "searched",
            "evidence_finding": result.get("evidence_finding", ""),
            "comparison": result.get("comparison"),
            "context_match": result.get("context_match"),
            "evidence_outcome": outcome,
            "source_quality": result["source_quality"],
            "verdict": verdict_map[outcome],
            "reason": result.get("reason", ""),
            "sources": result.get("sources", []),
            "score": score_map[outcome],
            "status": "success",
        }

    def failure_builder(original: dict[str, Any], reason: str):
        status = "not_searched" if reason == "not_searched" else "technical_failure"
        return {
            **original,
            "execution_status": status,
            "evidence_finding": "",
            "comparison": None,
            "context_match": None,
            "evidence_outcome": None,
            "source_quality": None,
            "verdict": None,
            "reason": (
                "The model did not perform web search; it still did not search after one retry."
                if status == "not_searched"
                else reason
            ),
            "sources": [],
            "score": 50,
            "status": status,
        }

    return run_with_one_retry(
        items,
        "item_id",
        "results",
        FACT_EVIDENCE_OUTCOMES,
        batch_caller,
        success_builder,
        failure_builder,
        api_logs,
    )


def run_logic_supplements(
    api_key: str,
    news_text: str,
    items: list[dict[str, Any]],
    api_logs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Logic supplement request: distinguish support, refutation, no evidence, conflict, and weak sources."""

    def batch_caller(batch: list[dict[str, Any]], attempt: int):
        return call_openrouter(
            api_key,
            "logic_supplement_results",
            LOGIC_SUPPLEMENT_SCHEMA,
            LOGIC_SUPPLEMENT_SYSTEM_PROMPT,
            {"news_text": news_text, "items": batch},
            "logic_supplement",
            [item["claim_id"] for item in batch],
            attempt,
            use_web_search=True,
        )

    score_map = {
        "supports_logic": 100,
        "refutes_logic": 0,
        "no_relevant_evidence": 75,
        "conflicting_evidence": 50,
        "low_quality_sources_only": 65,
    }
    verdict_map = {
        "supports_logic": "valid",
        "refutes_logic": "invalid",
        "no_relevant_evidence": "insufficient",
        "conflicting_evidence": "insufficient",
        "low_quality_sources_only": "insufficient",
    }

    def success_builder(original: dict[str, Any], result: dict[str, Any]):
        outcome = result["evidence_outcome"]
        return {
            **original,
            "execution_status": "searched",
            "evidence_outcome": outcome,
            "source_quality": result["source_quality"],
            "verdict": verdict_map[outcome],
            "reason": result.get("reason", ""),
            "sources": result.get("sources", []),
            "score": score_map[outcome],
            "status": "success",
        }

    def failure_builder(original: dict[str, Any], reason: str):
        status = "not_searched" if reason == "not_searched" else "technical_failure"
        return {
            **original,
            "execution_status": status,
            "evidence_outcome": None,
            "source_quality": None,
            "verdict": None,
            "reason": (
                "The model did not perform web search; it still did not search after one retry."
                if status == "not_searched"
                else reason
            ),
            "sources": [],
            "score": 50,
            "status": status,
        }

    return run_with_one_retry(
        items,
        "claim_id",
        "results",
        LOGIC_EVIDENCE_OUTCOMES,
        batch_caller,
        success_builder,
        failure_builder,
        api_logs,
    )


# =============================================================================
# Part 7: Programmatic scoring
# The model returns only labels, reasons, and evidence; all scores, weights, averages, and grades are computed here.
# =============================================================================


def article_grade(raw_score: float) -> str:
    """Use the unrounded raw score to assign the grade."""
    if raw_score >= 70:
        return "Credible"
    if raw_score >= 40:
        return "Questionable"
    return "Not credible"


def explanation_for_type(claim_type: str) -> str:
    """State the evaluation boundary for the three argumentative claim types."""
    return {
        "causal": "Evaluate factual premises and the causal relation",
        "prediction": "Evaluate the basis for the prediction, not whether the future outcome is true",
        "opinion": "Evaluate support and reasoning, not whether the opinion itself is true",
    }.get(claim_type, "Check the factual claim")


def combined_check_status(checks: list[dict[str, Any]]) -> str:
    """Combine check statuses; request errors take priority, then missing search."""
    statuses = {item.get("status") for item in checks}
    if "technical_failure" in statuses:
        return "technical_failure"
    if "not_searched" in statuses:
        return "not_searched"
    return "success"


def supporting_detail_score(detail_results: list[dict[str, Any]]) -> float:
    """Score supporting details; if any detail is refuted, this component is capped at 50."""
    average_score = sum(item["score"] for item in detail_results) / len(
        detail_results
    )
    if any(item.get("verdict") == "refuted" for item in detail_results):
        return min(average_score, 50)
    return average_score


def build_deduction_analysis(
    claim_results: list[dict[str, Any]], raw_score: float
) -> dict[str, Any]:
    """Summarize deductions from problematic claims; perfect claims neither add nor dilute the article score."""
    points: list[dict[str, Any]] = []

    def add_point(
        claim: dict[str, Any],
        category: str,
        category_name: str,
        component_score: float,
        component_weight: float,
        reason: str,
        status: str,
        related_checks: list[dict[str, Any]],
    ) -> None:
        deduction_within_claim = (100 - component_score) * component_weight
        if deduction_within_claim <= 0:
            return
        points.append(
            {
                "deduction_id": f"deduction_{len(points) + 1}",
                "claim_id": claim["claim_id"],
                "claim_text": claim["claim_text"],
                "category": category,
                "category_name": category_name,
                "component_score": component_score,
                "component_weight_within_claim": component_weight,
                "deduction_within_claim": deduction_within_claim,
                "deduction_from_article_score": deduction_within_claim,
                "reason": reason,
                "status": status,
                "related_checks": related_checks,
            }
        )

    for claim in claim_results:
        if claim["claim_type"] == "fact":
            check = claim["fact_check"]
            add_point(
                claim,
                "fact_claim",
                "Factual claim check",
                float(check["score"]),
                float(claim.get("fact_check_weight", 1.0)),
                check.get("reason", ""),
                check.get("status", "success"),
                [
                    {
                        "item_id": check.get("item_id"),
                        "statement": check.get("statement"),
                        "execution_status": check.get("execution_status"),
                        "evidence_finding": check.get("evidence_finding"),
                        "comparison": check.get("comparison"),
                        "evidence_outcome": check.get("evidence_outcome"),
                        "source_quality": check.get("source_quality"),
                        "verdict": check.get("verdict"),
                        "score": check.get("score"),
                        "status": check.get("status"),
                        "reason": check.get("reason"),
                    }
                ],
            )
            detail_component = claim.get("supporting_detail_component", {})
            if detail_component.get("weight", 0) > 0 and detail_component.get(
                "score", 100
            ) < 100:
                problem_details = [
                    item
                    for item in detail_component["detail_checks"]
                    if item.get("verdict") != "supported"
                    or item.get("status") != "success"
                ]
                add_point(
                    claim,
                    "supporting_details",
                    "Supporting detail check",
                    float(detail_component["score"]),
                    float(detail_component["weight"]),
                    "；".join(
                        f"{item.get('statement', 'supporting detail')}: {item.get('reason', '')}"
                        for item in problem_details
                    )
                    or "Not all supporting details were supported by reliable evidence.",
                    detail_component["status"],
                    problem_details,
                )
            continue

        reliability = claim["fact_reliability"]
        if reliability["weight"] > 0 and reliability["score"] < 100:
            problem_premises = [
                item
                for item in reliability["premise_checks"]
                if item.get("verdict") != "supported"
                or item.get("status") == "technical_failure"
            ]
            reason = "；".join(
                f"{item.get('statement', 'factual premise')}: {item.get('reason', '')}"
                for item in problem_premises
            ) or "Not all factual premises were supported by reliable evidence."
            add_point(
                claim,
                "fact_reliability",
                "Factual premise reliability",
                float(reliability["score"]),
                float(reliability["weight"]),
                reason,
                reliability["status"],
                [
                    {
                        "item_id": item.get("item_id"),
                        "statement": item.get("statement"),
                        "execution_status": item.get("execution_status"),
                        "evidence_finding": item.get("evidence_finding"),
                        "comparison": item.get("comparison"),
                        "evidence_outcome": item.get("evidence_outcome"),
                        "source_quality": item.get("source_quality"),
                        "verdict": item.get("verdict"),
                        "score": item.get("score"),
                        "status": item.get("status"),
                        "reason": item.get("reason"),
                    }
                    for item in problem_premises
                ],
            )

        detail_component = claim.get("supporting_detail_component", {})
        if detail_component.get("weight", 0) > 0 and detail_component.get(
            "score", 100
        ) < 100:
            problem_details = [
                item
                for item in detail_component["detail_checks"]
                if item.get("verdict") != "supported"
                or item.get("status") != "success"
            ]
            add_point(
                claim,
                "supporting_details",
                "Supporting detail check",
                float(detail_component["score"]),
                float(detail_component["weight"]),
                "；".join(
                    f"{item.get('statement', 'supporting detail')}: {item.get('reason', '')}"
                    for item in problem_details
                )
                or "Not all supporting details were supported by reliable evidence.",
                detail_component["status"],
                problem_details,
            )

        logic = claim["logic_quality"]
        if logic["score"] < 100:
            add_point(
                claim,
                "logic_quality",
                "Logic quality",
                float(logic["score"]),
                float(logic["weight"]),
                logic.get("reason", ""),
                logic["status"],
                [],
            )

    # The article score floor is 0; if raw deductions exceed 100, only the effective deductions are counted.
    remaining_deduction = 100 - raw_score
    for point in points:
        applied = min(
            point["deduction_from_article_score"], max(0, remaining_deduction)
        )
        point["deduction_from_article_score"] = applied
        remaining_deduction -= applied

    return {
        "reference_full_score": 100,
        "final_article_score": raw_score,
        "total_deduction": 100 - raw_score,
        "problem_claim_count": sum(
            item["score"] < 100 for item in claim_results
        ),
        "calculated_deduction_sum": sum(
            point["deduction_from_article_score"] for point in points
        ),
        "deduction_points": points,
    }


def analyze_one_news(api_key: str, news_id: str, news_text: str) -> dict[str, Any]:
    """Run the full revised workflow for one news item."""
    api_logs: list[dict[str, Any]] = []
    frozen_claims = extract_and_freeze_claims(api_key, news_text, api_logs)
    argument_analyses = analyze_arguments(api_key, news_text, frozen_claims, api_logs)

    # Build fact-check items: core facts, argument premises, and supporting_details use the same request type,
    # but supporting_details receive a smaller scoring weight.
    fact_items: list[dict[str, Any]] = []
    for claim in frozen_claims:
        if claim["claim_type"] == "fact":
            fact_items.append(
                {
                    "claim_id": claim["claim_id"],
                    "claim_text": claim["claim_text"],
                    "claim_type": claim["claim_type"],
                    "item_id": claim["claim_id"],
                    "item_type": "fact_claim",
                    "statement": claim["claim_text"],
                }
            )
        else:
            for premise in argument_analyses[claim["claim_id"]]["fact_premises"]:
                fact_items.append(
                    {
                        "claim_id": claim["claim_id"],
                        "claim_text": claim["claim_text"],
                        "claim_type": claim["claim_type"],
                        "item_id": premise["premise_id"],
                        "item_type": "fact_premise",
                        "statement": premise["premise_text"],
                    }
                )

        existing_statements = {
            normalized_statement(item["statement"])
            for item in fact_items
            if item["claim_id"] == claim["claim_id"]
        }
        for detail_index, detail in enumerate(claim["supporting_details"], start=1):
            normalized_detail = normalized_statement(detail)
            if not normalized_detail or normalized_detail in existing_statements:
                continue
            fact_items.append(
                {
                    "claim_id": claim["claim_id"],
                    "claim_text": claim["claim_text"],
                    "claim_type": claim["claim_type"],
                    "item_id": f"{claim['claim_id']}_detail_{detail_index}",
                    "item_type": "supporting_detail",
                    "statement": detail,
                }
            )
            existing_statements.add(normalized_detail)

    # Only claims initially judged insufficient enter logic-relation supplement checking.
    logic_items: list[dict[str, Any]] = []
    for claim in frozen_claims:
        if claim["claim_type"] == "fact":
            continue
        analysis = argument_analyses[claim["claim_id"]]
        if analysis["initial_logic"] == "insufficient":
            logic_items.append(
                {
                    "claim_id": claim["claim_id"],
                    "claim_text": claim["claim_text"],
                    "claim_type": claim["claim_type"],
                    "logic_chain": analysis["logic_chain"],
                    "fact_premises": analysis["fact_premises"],
                }
            )

    fact_results = run_fact_checks(api_key, news_text, fact_items, api_logs)
    logic_results = run_logic_supplements(api_key, news_text, logic_items, api_logs)
    fact_by_item = {item["item_id"]: item for item in fact_results}
    logic_by_claim = {item["claim_id"]: item for item in logic_results}

    # Aggregate by frozen claim_id; never adopt new claim text or type from later API responses.
    claim_results: list[dict[str, Any]] = []
    for claim in frozen_claims:
        claim_id = claim["claim_id"]
        detail_results = [
            item
            for item in fact_results
            if item["claim_id"] == claim_id
            and item["item_type"] == "supporting_detail"
        ]
        if claim["claim_type"] == "fact":
            fact_check = fact_by_item[claim_id]
            if detail_results:
                core_weight = 0.7
                detail_weight = 0.3
                detail_score = supporting_detail_score(detail_results)
            else:
                core_weight = 1.0
                detail_weight = 0.0
                detail_score = None
            claim_score = fact_check["score"] * core_weight
            if detail_score is not None:
                claim_score += detail_score * detail_weight
            claim_results.append(
                {
                    **claim,
                    "score": claim_score,
                    "status": combined_check_status([fact_check, *detail_results]),
                    "fact_check": fact_check,
                    "fact_check_weight": core_weight,
                    "supporting_detail_component": {
                        "score": detail_score,
                        "weight": detail_weight,
                        "status": (
                            combined_check_status(detail_results)
                            if detail_results
                            else "not_applicable"
                        ),
                        "detail_checks": detail_results,
                    },
                    "result_note": explanation_for_type("fact"),
                }
            )
            continue

        analysis = argument_analyses[claim_id]
        premise_results = [
            fact_by_item[premise["premise_id"]]
            for premise in analysis["fact_premises"]
        ]

        # Core factual premises still follow the rule: if any one is refuted, this component is 0.
        # Otherwise, use the average check score. supporting_details use a separate smaller weight.
        if not premise_results:
            premise_component = {
                "score": None,
                "weight": 0.0,
                "status": "not_applicable",
                "premise_checks": [],
            }
        else:
            if any(item["verdict"] == "refuted" for item in premise_results):
                reliability_score = 0
            else:
                reliability_score = sum(
                    item["score"] for item in premise_results
                ) / len(premise_results)
            premise_component = {
                "score": reliability_score,
                "weight": 0.5 if detail_results else 0.6,
                "status": combined_check_status(premise_results),
                "premise_checks": premise_results,
            }

        if detail_results:
            detail_component = {
                "score": supporting_detail_score(detail_results),
                "weight": 0.2 if premise_results else 0.3,
                "status": combined_check_status(detail_results),
                "detail_checks": detail_results,
            }
        else:
            detail_component = {
                "score": None,
                "weight": 0.0,
                "status": "not_applicable",
                "detail_checks": [],
            }

        # Factual reliability refers only to core factual premises that determine the argument, so the strict
        # rule remains: if any core factual premise is refuted, this component is 0. Descriptive details are scored separately with lower weight.
        fact_reliability = premise_component
        logic_weight = 1.0 - premise_component["weight"] - detail_component["weight"]

        # Logic quality: valid = 100, invalid = 0, and insufficient uses the separate web supplement result.
        if analysis["initial_logic"] == "valid":
            logic_quality = {
                "score": 100,
                "weight": logic_weight,
                "status": "success",
                "initial_logic": "valid",
                "reason": analysis["initial_logic_reason"],
                "supplement": None,
            }
        elif analysis["initial_logic"] == "invalid":
            logic_quality = {
                "score": 0,
                "weight": logic_weight,
                "status": "success",
                "initial_logic": "invalid",
                "reason": analysis["initial_logic_reason"],
                "supplement": None,
            }
        else:
            supplement = logic_by_claim[claim_id]
            logic_quality = {
                "score": supplement["score"],
                "weight": logic_weight,
                "status": supplement["status"],
                "initial_logic": "insufficient",
                "reason": supplement["reason"],
                "supplement": supplement,
            }

        # Weights are fully computed by the program: core premises take 50%/60%, details take 20%/30%,
        # and the remaining weight goes to logic quality.
        claim_score = logic_quality["score"] * logic_quality["weight"]
        if fact_reliability["score"] is not None:
            claim_score += fact_reliability["score"] * fact_reliability["weight"]
        if detail_component["score"] is not None:
            claim_score += detail_component["score"] * detail_component["weight"]

        overall_status = combined_check_status(
            premise_results
            + detail_results
            + ([logic_quality["supplement"]] if logic_quality["supplement"] else [])
        )
        claim_results.append(
            {
                **claim,
                "score": claim_score,
                "status": overall_status,
                "logic_chain": analysis["logic_chain"],
                "fact_reliability": fact_reliability,
                "supporting_detail_component": detail_component,
                "logic_quality": logic_quality,
                "result_note": explanation_for_type(claim["claim_type"]),
            }
        )

    # Perfect claims do not add points or dilute the score; deductions from problematic claims are subtracted from 100.
    claim_deductions = [max(0, 100 - item["score"]) for item in claim_results]
    raw_score = max(0, 100 - sum(claim_deductions))
    deduction_analysis = build_deduction_analysis(claim_results, raw_score)

    # Completion rate is used only for the test report, not for article scoring.
    all_required_checks = fact_results + logic_results
    required_count = len(all_required_checks)
    successful_count = sum(item["status"] == "success" for item in all_required_checks)
    technical_failure_count = sum(
        item["status"] == "technical_failure" for item in all_required_checks
    )
    not_searched_count = sum(
        item["status"] == "not_searched" for item in all_required_checks
    )
    completion_rate = successful_count / required_count if required_count else 1.0

    # The web-search audit reads OpenRouter search counts; citation objects are evidence logs only.
    # A search count of 0 triggers one retry; checks still not searched are marked not_searched and temporarily scored 50.
    online_logs = [
        log
        for log in api_logs
        if log.get("request_type") in {"fact_check", "logic_supplement"}
    ]
    search_call_logs = [
        {
            "request_type": log.get("request_type"),
            "attempt": log.get("attempt"),
            "item_ids": log.get("item_ids", []),
            "web_search_requests": log.get("web_search_requests", 0),
            "url_citation_count": len(log.get("citation_log", [])),
            "confirmed": log.get("web_search_confirmed", False),
            "confirmation_basis": log.get(
                "web_search_confirmation_basis", "request_error"
            ),
        }
        for log in online_logs
    ]
    confirmed_search_calls = sum(log["confirmed"] is True for log in search_call_logs)
    if not all_required_checks:
        search_status = "not_required"
    elif all(
        item.get("execution_status") == "searched" for item in all_required_checks
    ):
        search_status = "confirmed"
    else:
        search_status = "not_confirmed"

    return {
        "news_id": news_id,
        "original_text": news_text,
        "analysis_time_utc": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "article_score_raw": raw_score,
        "article_grade": article_grade(raw_score),
        "deduction_analysis": deduction_analysis,
        "test_statistics": {
            "required_check_count": required_count,
            "successful_check_count": successful_count,
            "technical_failure_count": technical_failure_count,
            "not_searched_count": not_searched_count,
            "completion_rate": completion_rate,
        },
        "web_search_audit": {
            "status": search_status,
            "online_api_call_count": len(search_call_logs),
            "confirmed_call_count": confirmed_search_calls,
            "unconfirmed_call_count": len(search_call_logs)
            - confirmed_search_calls,
            "calls": search_call_logs,
            "scope_note": "Each web-search request contains one check item, so the search record maps directly to that item.",
        },
        "frozen_claims": frozen_claims,
        "claim_results": claim_results,
        "api_call_logs": api_logs,
    }


# =============================================================================
# Part 8: Run the test set item by item and save results
# The next news item starts only after the current item is completed and saved.
# =============================================================================


def save_json(path: Path, data: dict[str, Any]) -> None:
    """Save results as readable JSON."""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_news_file(api_key: str, test_file: Path, results_dir: Path) -> list[dict[str, Any]]:
    """Process one test file item by item; the next item starts only after the current one is saved."""
    if not test_file.exists():
        raise FileNotFoundError(f"Test set not found: {test_file}")

    news_items = read_test_news(test_file)
    if not news_items:
        raise ValueError("No processable news items were found in the test set.")

    results_dir.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []

    print(f"Read {len(news_items)} news items; processing them one by one.")
    for index, news in enumerate(news_items, start=1):
        news_id = news["news_id"]
        print(f"\n[{index}/{len(news_items)}] Starting: {news_id}")
        try:
            result = analyze_one_news(api_key, news_id, news["text"])
            output_path = results_dir / f"{safe_filename(news_id)}.json"
            save_json(output_path, result)
            summary.append(
                {
                    "news_id": news_id,
                    "status": "success",
                    "article_score_raw": result["article_score_raw"],
                    "article_grade": result["article_grade"],
                    "web_search_status": result["web_search_audit"]["status"],
                    **result["test_statistics"],
                    "result_file": output_path.name,
                }
            )
            print(
                f"Done: raw score {result['article_score_raw']:.2f}, "
                f"grade {result['article_grade']}, result saved to {output_path.name}"
            )
        except Exception as exc:
            # A failure on one item does not stop later test items.
            error_result = {
                "news_id": news_id,
                "status": "processing_error",
                "error": str(exc),
                "analysis_time_utc": datetime.now(timezone.utc).isoformat(),
            }
            output_path = results_dir / f"{safe_filename(news_id)}_error.json"
            save_json(output_path, error_result)
            summary.append(
                {
                    "news_id": news_id,
                    "status": "processing_error",
                    "error": str(exc),
                    "result_file": output_path.name,
                }
            )
            print(f"This item failed; the error was saved and processing continues: {exc}")

    save_json(
        results_dir / "summary.json",
        {
            "model": MODEL,
            "test_file": test_file.name,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "results": summary,
        },
    )
    print(f"\nAll processing finished. Summary file: {results_dir / 'summary.json'}")
    return summary


def parse_args() -> argparse.Namespace:
    """Read optional command-line arguments; defaults still come from config.py."""
    parser = argparse.ArgumentParser(description="Run the multi-step news verification workflow.")
    parser.add_argument("--input", type=Path, default=TEST_FILE, help="Test-set Markdown file")
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR, help="Result output folder")
    return parser.parse_args()


def main() -> None:
    """Program entry point."""
    args = parse_args()
    api_key = get_api_key()
    run_news_file(api_key, args.input, args.output_dir)


if __name__ == "__main__":
    main()
