from __future__ import annotations

import asyncio
import datetime
import json
import re
from urllib.parse import urlparse
from typing import Any

from src.agent_loop import stream_agent_loop
from src.endpoint_resolver import resolve_endpoint, resolve_utility_fallback_candidates
from src.llm_core import llm_call_async_with_fallback
from src.orchestrator.schemas import GraphNode
from core.database import Session, SessionLocal

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", re.IGNORECASE)
_EVIDENCE_BULLET_RE = re.compile(r"^-\s*(.+?)\s+-\s+(https?://\S+)\s*$", re.IGNORECASE)
_RESEARCH_ANCHOR_RE = re.compile(r"#research-([A-Za-z0-9_-]+)", re.IGNORECASE)

# Lines where the model narrates imaginary tool calls instead of executing them.
# Matches patterns like: [web_search query="..."] [Step title: ...] [Preferred tools: ...]
_FAKE_DIRECTIVE_LINE_RE = re.compile(
    r"^\s*\[(?:web_search\b|Step[\s_]\w+|Preferred[\s_]tools|Dependencies|System[\s_]prompt[\s_]truncated)[^\]]*\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_model_directives(text: str) -> str:
    """Remove lines where the model narrated tool calls/directives instead of executing them."""
    cleaned = _FAKE_DIRECTIVE_LINE_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _strip_clarify_extras(text: str) -> str:
    """For clarify/constraint steps remove Example blocks and tool-planning meta-commentary."""
    paragraphs = re.split(r"\n{2,}", text)
    kept = []
    for para in paragraphs:
        p_low = para.strip().lower()
        if not p_low:
            continue
        # Drop Example: blocks
        if p_low.startswith("example"):
            continue
        # Drop meta-commentary like "To create this story I'll use web_search..."
        if re.match(
            r"(to (create|find|gather|generate|craft)|by (combining|using|searching))",
            p_low,
        ) and any(k in p_low for k in ("web_search", "i'll use", "i will use", "tool")):
            continue
        kept.append(para.strip())
    return "\n\n".join(kept)
_LOCAL_API_BASE = "http://localhost:7000"
_RESEARCH_WAIT_DEFAULT_SECONDS = 900
_RESEARCH_WAIT_POLL_SECONDS = 2.0
_MODEL_AUTO_MARKERS = {"", "auto", "default", "none", "null", "n/a", "na"}
_TOOL_NONE_MARKERS = {
    "",
    "none",
    "no_tool",
    "no_tools",
    "no-tool",
    "no-tools",
    "null",
    "n/a",
    "na",
    "auto",
    "default",
}
_TOOL_PREFIXES = {
    "tool",
    "tools",
    "autotool",
    "autotools",
    "model",
    "models",
}


def _normalize_tool_token(raw: str | None) -> str | None:
    token = str(raw or "").strip().lower()
    if not token:
        return None
    token = token.replace(" ", "_")
    if ":" in token:
        prefix, suffix = token.split(":", 1)
        if prefix.strip() in _TOOL_PREFIXES:
            token = suffix.strip()
    if token in _TOOL_NONE_MARKERS:
        return None
    return token or None


def _normalized_step_tools(step: GraphNode) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in (step.tools or []):
        token = _normalize_tool_token(raw)
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _resolve_step_model(step: GraphNode, fallback_model: str) -> str:
    candidate = str(getattr(step, "model", "") or "").strip()
    if not candidate:
        return fallback_model
    if candidate.lower() in _MODEL_AUTO_MARKERS:
        return fallback_model
    return candidate


def _session_model(session_id: str | None, owner: str | None = None) -> str | None:
    if not session_id:
        return None
    db = SessionLocal()
    try:
        q = db.query(Session).filter(Session.id == session_id)
        if owner is not None:
            q = q.filter(Session.owner == owner)
        row = q.first()
        m = (getattr(row, "model", "") or "").strip() if row else ""
        return m or None
    except Exception:
        return None
    finally:
        db.close()


def _resolve_candidates(
    owner: str | None = None,
    session_id: str | None = None,
) -> list[tuple[str, str, dict[str, str]]]:
    out: list[tuple[str, str, dict[str, str]]] = []
    seen: set[tuple[str, str]] = set()

    session_fallback_model = _session_model(session_id, owner=owner)

    def _add(candidate: tuple[str, str, dict[str, str]] | None) -> None:
        if not candidate:
            return
        url, model, headers = candidate
        model = (model or "").strip() or (session_fallback_model or "")
        if not url or not model:
            return
        key = (url, model)
        if key in seen:
            return
        seen.add(key)
        out.append((url, model, headers or {}))

    try:
        # Primary model comes from Utility (which itself falls back to Default
        # Chat when Utility is unset). This mirrors other background systems.
        _add(resolve_endpoint("utility", owner=owner))
        for cand in (resolve_utility_fallback_candidates(owner=owner) or []):
            _add(cand)
    except Exception:
        return out
    return out


def _step_relevant_tools(step: GraphNode) -> set[str] | None:
    names: set[str] = set()
    for low in _normalized_step_tools(step):
        # Planner labels can include pseudo-tools; map common ones to actual
        # tool names used by the agent runtime.
        if low.startswith("python_script") or low in {"python-script", "pandas"}:
            names.add("python")
            continue
        if low in {"deep_research", "deepresearch", "research", "research_job", "research_report"}:
            # Reuse existing deep-research system instead of improvising web scraping in-step.
            names.update({"trigger_research", "manage_research"})
            continue
        if low in {"agent", "agent_mode", "assistant"}:
            # Orchestrator execution already runs through the agent loop; keep core tools available.
            names.update({"web_search", "python", "bash", "read_file", "write_file"})
            continue
        names.add(low)
    return names or None


def _step_requests_deep_research(step: GraphNode) -> bool:
    for low in _step_relevant_tools(step) or set():
        if low in {
            "trigger_research",
            "deep_research",
            "deepresearch",
            "research",
            "research_job",
            "research_report",
            "manage_research",
        }:
            return True
    return False


def _step_requests_web_search(step: GraphNode) -> bool:
    return "web_search" in (_step_relevant_tools(step) or set())


def _internal_tool_headers(owner: str | None) -> dict[str, str]:
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN

    headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
    if owner:
        headers["X-Radian-Owner"] = owner
    return headers


def _derive_search_query(objective: str, step: GraphNode) -> tuple[str, str | None]:
    obj = re.sub(r"\s*\((revised|updated)\)\s*", " ", str(objective or "").strip(), flags=re.IGNORECASE).strip()
    step_text = re.sub(r"\s*\((revised|updated)\)\s*", " ", f"{step.title} {step.description}".strip(), flags=re.IGNORECASE).strip()
    generic_step = any(
        k in step_text.lower()
        for k in ("web_search", "research", "investigate", "find", "search", "look up")
    )
    if obj and (not step_text or generic_step):
        base = obj
    elif obj and step_text:
        base = f"{step_text}. Context: {obj}" if obj.lower() not in step_text.lower() else step_text
    else:
        base = step_text or obj
    query = base or "current relevant information"
    low = query.lower()
    time_filter = "day" if any(k in low for k in ("today", "trending", "latest", "right now", "current")) else None
    return query, time_filter


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    if txt.startswith("```"):
        m = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", txt, flags=re.IGNORECASE)
        if m:
            txt = m.group(1)
    else:
        m = re.search(r"(\{[\s\S]*\})", txt)
        if m:
            txt = m.group(1)
    try:
        obj = json.loads(txt)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


async def _plan_search_query(
    *,
    objective: str,
    step: GraphNode,
    owner: str | None,
    session_id: str | None,
    prior_context: str,
) -> tuple[str, str | None, str]:
    """Pick a concrete web search query for a step.

    Returns (query, time_filter, strategy), where strategy is llm|heuristic.
    """
    heuristic_query, heuristic_filter = _derive_search_query(objective, step)
    candidates = _resolve_candidates(owner=owner, session_id=session_id)
    if not candidates:
        return heuristic_query, heuristic_filter, "heuristic"

    today = datetime.date.today().isoformat()
    prompt = (
        f"Current date: {today}\n"
        f"Step title: {step.title}\n"
        f"Step description: {step.description}\n"
        f"Overall task (context only): {objective}\n\n"
        + (f"Prior step results (context only):\n{prior_context[:800]}\n\n" if prior_context else "")
        + "What FACTUAL INFORMATION must be fetched from the web to complete this step?\n"
          "Write a short, concrete search query (under 8 words) using present-tense terms. Use the actual current year in the query if a year is relevant.\n"
          "Do NOT include story tone, audience, output format, or any task constraints in the query.\n"
          "Use time_filter=day if the step needs today's/trending/latest data; otherwise null.\n"
          "Return JSON only: {\"query\": \"...\", \"time_filter\": \"day|week|month|year|null\"}"
    )
    try:
        planned = await llm_call_async_with_fallback(
            candidates,
            messages=[
                {"role": "system", "content": "You produce short, focused web search queries that retrieve raw factual data. Never include task output constraints or instructions in the query."},
                {"role": "user", "content": prompt},
            ],
            timeout=20,
        )
        parsed = _extract_json_object(str(planned or ""))
        if not parsed:
            return heuristic_query, heuristic_filter, "heuristic"

        query = str(parsed.get("query") or "").strip()
        tf_raw = parsed.get("time_filter")
        time_filter = str(tf_raw).strip().lower() if isinstance(tf_raw, str) else None
        if time_filter not in {"day", "week", "month", "year"}:
            time_filter = None
        if not query:
            return heuristic_query, heuristic_filter, "heuristic"
        # Keep provider-facing query bounded and clean.
        query = re.sub(r"\s+", " ", query).strip()[:240]
        return query, time_filter, "llm"
    except Exception:
        return heuristic_query, heuristic_filter, "heuristic"


async def _fallback_web_search(
    *,
    objective: str,
    step: GraphNode,
    owner: str | None,
    session_id: str | None,
    prior_context: str,
) -> tuple[str, list[dict[str, Any]], str | None, str, str | None, str]:
    """Best-effort local search fallback when model fails to call web_search."""
    import httpx

    query, time_filter, strategy = await _plan_search_query(
        objective=objective,
        step=step,
        owner=owner,
        session_id=session_id,
        prior_context=prior_context,
    )
    body: dict[str, Any] = {"query": query}
    if time_filter:
        body["time_filter"] = time_filter

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{_LOCAL_API_BASE}/api/search",
                json=body,
                headers=_internal_tool_headers(owner),
            )
    except Exception as exc:
        return "", [], f"fallback web search call failed: {exc}", query, time_filter, strategy

    if resp.status_code >= 400:
        return "", [], f"fallback web search HTTP {resp.status_code}", query, time_filter, strategy

    try:
        payload = resp.json() if resp.content else {}
    except Exception:
        payload = {}

    context = str(payload.get("context") or "").strip()
    sources_raw = payload.get("sources")
    sources: list[dict[str, Any]] = []
    if isinstance(sources_raw, list):
        for src in sources_raw:
            if not isinstance(src, dict):
                continue
            url = str(src.get("url") or "").strip()
            if not url:
                continue
            title = str(src.get("title") or src.get("name") or url).strip()
            sources.append({"title": title, "url": url})

    if not sources:
        return context, [], "fallback web search returned no sources", query, time_filter, strategy
    return context, sources, None, query, time_filter, strategy


async def _wait_for_deep_research_completion(
    research_session_id: str,
    *,
    owner: str | None,
    timeout_seconds: int,
) -> tuple[str | None, list[dict[str, Any]], str | None]:
    """Wait for a deep-research session to finish and return (result, sources, error)."""
    import httpx

    sid = str(research_session_id or "").strip()
    if not sid:
        return None, [], "missing research session id"

    deadline = asyncio.get_running_loop().time() + max(30, int(timeout_seconds or 0))
    headers = _internal_tool_headers(owner)
    async with httpx.AsyncClient(timeout=20.0) as client:
        while True:
            if asyncio.get_running_loop().time() >= deadline:
                return None, [], f"timed out waiting for deep research session {sid}"

            try:
                status_resp = await client.get(
                    f"{_LOCAL_API_BASE}/api/research/status/{sid}",
                    headers=headers,
                )
            except Exception as exc:
                return None, [], f"deep research status check failed: {exc}"

            if status_resp.status_code >= 400:
                return None, [], f"deep research status HTTP {status_resp.status_code} for {sid}"

            try:
                status_data = status_resp.json() if status_resp.content else {}
            except Exception:
                status_data = {}

            status = str(status_data.get("status") or "").strip().lower()
            if status in {"done", "error", "cancelled"}:
                try:
                    result_resp = await client.post(
                        f"{_LOCAL_API_BASE}/api/research/result-peek/{sid}",
                        headers=headers,
                    )
                except Exception as exc:
                    return None, [], f"deep research result fetch failed: {exc}"

                if result_resp.status_code >= 400:
                    return None, [], f"deep research result HTTP {result_resp.status_code} for {sid}"

                try:
                    payload = result_resp.json() if result_resp.content else {}
                except Exception:
                    payload = {}

                result_text = str(payload.get("result") or "").strip() or None
                sources_raw = payload.get("sources") or []
                sources = [s for s in sources_raw if isinstance(s, dict)] if isinstance(sources_raw, list) else []
                err = None
                if status == "error":
                    err = result_text or f"deep research session {sid} ended with error"
                elif status == "cancelled":
                    err = f"deep research session {sid} was cancelled"
                return result_text, sources, err

            await asyncio.sleep(_RESEARCH_WAIT_POLL_SECONDS)


def _dedupe_lines(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _extract_urls(text: str) -> list[str]:
    return _dedupe_lines(_URL_RE.findall(text or ""))


def _build_artifact_appendix(
    web_sources: list[dict[str, Any]],
    python_outputs: list[str],
    other_tool_outputs: list[str],
) -> str:
    sections: list[str] = []

    if web_sources:
        lines: list[str] = []
        for src in web_sources[:8]:
            if not isinstance(src, dict):
                continue
            title = str(src.get("title") or src.get("name") or src.get("url") or "source").strip()
            url = str(src.get("url") or "").strip()
            if not url:
                continue
            lines.append(f"- {title} - {url}")
        lines = _dedupe_lines(lines)
        if lines:
            sections.append("Evidence links:\n" + "\n".join(lines))

    if python_outputs:
        cleaned = _dedupe_lines(python_outputs)
        if cleaned:
            body = "\n\n".join(cleaned[:3])
            sections.append(f"Python results:\n{body}")

    if other_tool_outputs:
        cleaned = _dedupe_lines(other_tool_outputs)
        if cleaned:
            body = "\n\n".join(cleaned[:3])
            sections.append(f"Tool outputs:\n{body}")

    return "\n\n".join(sections).strip()


def _clean_url(url: str) -> str:
    raw = str(url or "").strip().rstrip(").,;]")
    if not raw:
        return ""
    try:
        p = urlparse(raw)
        if p.scheme not in {"http", "https"} or not p.netloc:
            return ""
        return raw
    except Exception:
        return ""


def _extract_evidence_sources(raw_output: str) -> list[dict[str, str]]:
    lines = str(raw_output or "").splitlines()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in lines:
        match = _EVIDENCE_BULLET_RE.match(line.strip())
        if not match:
            continue
        title = str(match.group(1) or "source").strip()
        url = _clean_url(match.group(2) or "")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"title": title or url, "url": url})
    return out[:12]


def _normalize_report_markdown(
    report: str,
    *,
    objective: str,
    blueprint_type: str,
    sources: list[dict[str, str]],
) -> str:
    text = str(report or "").strip()
    if not text:
        text = f"## {blueprint_type.title()} Report\n\n### Objective\n{objective.strip() or 'N/A'}\n\nNo final output was captured."

    # Keep only vetted markdown links from collected evidence.
    allowed_urls = {s["url"] for s in sources if s.get("url")}

    def _link_rewrite(match: re.Match) -> str:
        label = str(match.group(1) or "source").strip()
        url = _clean_url(match.group(2) or "")
        if url and url in allowed_urls:
            return f"[{label}]({url})"
        return label

    text = _MARKDOWN_LINK_RE.sub(_link_rewrite, text)
    # Normalize odd spacing artifacts around punctuation and parentheses.
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"([\$\d])\(", r"\1 (", text)
    text = re.sub(r"\)\s*([A-Za-z])", r") \1", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # Add lightweight inline citations when numeric claims lack any citation marker.
    if sources:
        cited_lines: list[str] = []
        citation_idx = 1
        for line in text.splitlines():
            stripped = line.strip()
            has_number_claim = bool(re.search(r"\$\d|\d+%|\bpoints?\b|\bannual\b|\byearly\b|\bcredit limit\b", stripped, re.IGNORECASE))
            has_citation = bool(re.search(r"\[\d+\]", stripped))
            if has_number_claim and not has_citation and not stripped.startswith("#"):
                line = f"{line} [{citation_idx}]"
                citation_idx = (citation_idx % len(sources)) + 1
            cited_lines.append(line)
        text = "\n".join(cited_lines).strip()

    # Ensure a deterministic sources section using verified links.
    text_wo_sources = re.sub(r"\n## Sources[\s\S]*$", "", text, flags=re.IGNORECASE).strip()
    if sources:
        src_lines = [f"{idx}. [{s['title']}]({s['url']})" for idx, s in enumerate(sources, start=1)]
        return f"{text_wo_sources}\n\n## Sources\n" + "\n".join(src_lines)
    return text_wo_sources


def _heuristic_final_report(
    raw_output: str,
    objective: str,
    blueprint_type: str,
) -> str:
    raw = str(raw_output or "").strip()
    if not raw:
        return f"## {blueprint_type.title()} Report\n\nNo final output was captured."

    lines = [line.rstrip() for line in raw.splitlines()]
    cleaned: list[str] = []
    skip_prefixes = (
        "orchestrator run completed",
        "research objective:",
        "assumptions:",
        "success criteria:",
        "i will now use",
        "stay tuned",
        "once you have this python script ready",
        "to find the best credit cards",
        "to calculate the estimated value gain",
        "here's an example of how the script might look like",
    )
    for line in lines:
        stripped = line.strip()
        low = stripped.lower()
        if not stripped:
            cleaned.append("")
            continue
        if any(low.startswith(prefix) for prefix in skip_prefixes):
            continue
        cleaned.append(line)

    text = "\n".join(cleaned).strip()
    if not text:
        text = raw
    sources = _extract_evidence_sources(raw)
    base = (
        f"## {blueprint_type.title()} Report\n\n"
        f"### Objective\n{objective.strip() or 'N/A'}\n\n"
        f"### Findings\n{text}"
    ).strip()
    return _normalize_report_markdown(
        base,
        objective=objective,
        blueprint_type=blueprint_type,
        sources=sources,
    )


async def format_final_report(
    raw_output: str,
    objective: str,
    blueprint_type: str,
    owner: str | None = None,
    session_id: str | None = None,
) -> str:
    raw = str(raw_output or "").strip()
    if not raw:
        return ""
    sources = _extract_evidence_sources(raw)

    candidates = _resolve_candidates(owner=owner, session_id=session_id)
    if not candidates:
        return _heuristic_final_report(raw, objective, blueprint_type)

    try:
        prompt = (
            "Rewrite the raw orchestrator execution output into a polished, user-facing final report in markdown.\n"
            "Rules:\n"
            "- Remove process narration, tool chatter, future-tense planning, and duplicated reasoning.\n"
            "- Do not say you are about to search, calculate, or explain what tool was used unless a brief method note is essential.\n"
            "- Keep the concrete recommendations, calculations, caveats, and evidence.\n"
            "- Use clear headings and bullets/tables when helpful.\n"
            "- Add inline citation markers like [1], [2] after evidence-backed factual claims (numbers, fees, limits, rewards, valuations).\n"
            "- Use ONLY the provided evidence URLs for links. Do not invent or guess URLs.\n"
            "- End with a '## Sources' section using the provided numbered sources.\n"
            "- Return only the final report markdown.\n\n"
            f"Objective:\n{objective}\n\n"
            f"Blueprint:\n{blueprint_type}\n\n"
            + (
                "Provided evidence sources (numbered):\n"
                + "\n".join(
                    f"[{idx}] {s['title']} - {s['url']}" for idx, s in enumerate(sources, start=1)
                )
                + "\n\n"
                if sources
                else ""
            )
            +
            f"Raw output:\n{raw}"
        )
        rewritten = await llm_call_async_with_fallback(
            candidates,
            messages=[
                {
                    "role": "system",
                    "content": "You turn noisy orchestration output into polished final answers.",
                },
                {"role": "user", "content": prompt},
            ],
            timeout=30,
        )
        text = str(rewritten or "").strip()
        final = text or _heuristic_final_report(raw, objective, blueprint_type)
        return _normalize_report_markdown(
            final,
            objective=objective,
            blueprint_type=blueprint_type,
            sources=sources,
        )
    except Exception:
        return _heuristic_final_report(raw, objective, blueprint_type)


async def execute_step(
    step: GraphNode,
    objective: str,
    owner: str | None,
    session_id: str,
    prior_node_outputs: list[dict[str, str]] | None = None,
) -> str:
    candidates = _resolve_candidates(owner=owner, session_id=session_id)
    if not candidates:
        raise RuntimeError("No utility model endpoint available for orchestrator step")

    endpoint_url, model, headers = candidates[0]
    effective_model = _resolve_step_model(step, model)
    relevant_tools = _step_relevant_tools(step)
    research_wait_ids: list[str] = []
    research_sources: list[dict[str, Any]] = []
    prior_outputs = [item for item in (prior_node_outputs or []) if isinstance(item, dict)]
    relevant_prior_outputs = prior_outputs[-3:]

    context_blocks: list[str] = []
    for item in relevant_prior_outputs:
        node_id = str(item.get("node_id") or "").strip() or "unknown"
        title = str(item.get("title") or "").strip() or node_id
        output = str(item.get("output") or "").strip()
        if not output:
            continue
        context_blocks.append(
            f"Step {title} ({node_id}) output:\n{output}"
        )
    prior_context = "\n\n".join(context_blocks).strip()

    must_execute_tool = bool((relevant_tools or set()) & {"python", "bash", "web_search", "read_file", "write_file"})
    preferred_tools_text = ", ".join(sorted(relevant_tools)) if relevant_tools else "none"

    # A no-tool step should stay tool-free so planning/constraint steps do not
    # drift into improvised tool execution loops.
    if not relevant_tools:
        clarify_like = any(k in f"{step.title} {step.description}".lower() for k in ("clarify", "constraint", "interpret"))
        no_tool_prompt = (
            f"Overall objective:\n{objective}\n\n"
            f"Step title: {step.title}\n"
            f"Step description: {step.description}\n"
            + (f"\nRelevant completed step outputs:\n{prior_context}\n" if prior_context else "")
            + "\nRules: this step has no tools. Produce the step result directly in plain text. "
            "Do not output shell/python snippets, pseudo-tool calls, or setup instructions."
            + (" For clarify/constraint steps: return only the restated objective and a numbered list of constraints. Do not write any example content, story drafts, or sample deliverables." if clarify_like else "")
        )
        no_tool_result = await llm_call_async_with_fallback(
            candidates,
            messages=[
                {
                    "role": "system",
                    "content": "Execute a single orchestration step without tool usage.",
                },
                {"role": "user", "content": no_tool_prompt},
            ],
            timeout=30,
        )
        result_text = _strip_model_directives(str(no_tool_result or "").strip())
        if clarify_like:
            result_text = _strip_clarify_extras(result_text)
        return result_text or "(no output)"

    messages = [
        {
            "role": "system",
            "content": (
                "You are executing one step in a background orchestrator. "
                "Use tools when available and return a concise, concrete output. "
                "If the step lists an execution tool such as python, bash, or web_search, use it to produce the result instead of describing how someone else could do it."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Overall objective:\n{objective}\n\n"
                f"Step title: {step.title}\n"
                f"Step description: {step.description}\n"
                f"Preferred tools: {preferred_tools_text}\n"
                + (f"\nRelevant completed step outputs:\n{prior_context}\n" if prior_context else "")
                + ("\nRequired behavior: execute the needed tools now and return the actual computed/search results for this step. Do not return instructions telling the user how to run code or what command to execute.\n" if must_execute_tool else "")
                + "Produce result text for this step only."
            ),
        },
    ]

    full_text = ""
    tool_summaries: list[str] = []
    web_sources: list[dict[str, Any]] = []
    python_outputs: list[str] = []
    other_tool_outputs: list[str] = []
    saw_web_search_signal = False

    async for event_str in stream_agent_loop(
        endpoint_url=endpoint_url,
        model=effective_model,
        messages=messages,
        session_id=session_id,
        owner=owner,
        headers=headers,
        max_rounds=10,
        relevant_tools=relevant_tools,
    ):
        if event_str.startswith("data: ") and not event_str.startswith("data: [DONE]"):
            try:
                data = json.loads(event_str[6:])
            except json.JSONDecodeError:
                continue
            if "delta" in data:
                full_text += data["delta"]
            elif data.get("type") == "web_sources":
                raw_sources = data.get("data") or []
                if isinstance(raw_sources, list):
                    if raw_sources:
                        saw_web_search_signal = True
                    web_sources.extend(src for src in raw_sources if isinstance(src, dict))
            elif data.get("type") == "tool_output":
                tool_name = str(data.get("tool") or "").strip().lower()
                out = (data.get("stdout") or data.get("output") or "").strip()
                if tool_name == "trigger_research":
                    sid = str(data.get("research_session_id") or "").strip()
                    if not sid and out:
                        m = _RESEARCH_ANCHOR_RE.search(out)
                        if m:
                            sid = str(m.group(1) or "").strip()
                    if sid and sid not in research_wait_ids:
                        research_wait_ids.append(sid)
                if out:
                    tool_summaries.append(out[:400])
                    trimmed = out[:1200].strip()
                    if tool_name == "python":
                        python_outputs.append(trimmed)
                    elif tool_name == "web_search":
                        saw_web_search_signal = True
                        for url in _extract_urls(trimmed)[:5]:
                            web_sources.append({"title": url, "url": url})
                    elif tool_name and tool_name not in {"bash", "python"}:
                        other_tool_outputs.append(f"[{tool_name}] {trimmed}")

    if _step_requests_deep_research(step):
        wait_timeout = _RESEARCH_WAIT_DEFAULT_SECONDS
        try:
            if isinstance(step.inputs, dict) and step.inputs.get("research_timeout_seconds") is not None:
                wait_timeout = int(step.inputs.get("research_timeout_seconds") or _RESEARCH_WAIT_DEFAULT_SECONDS)
            elif isinstance(step.inputs, dict) and step.inputs.get("max_time") is not None:
                wait_timeout = int(step.inputs.get("max_time") or _RESEARCH_WAIT_DEFAULT_SECONDS)
        except Exception:
            wait_timeout = _RESEARCH_WAIT_DEFAULT_SECONDS

        if not research_wait_ids:
            other_tool_outputs.append(
                "[trigger_research] Step requested deep research but no research session id was emitted."
            )

        for sid in research_wait_ids:
            result_text, sources, wait_error = await _wait_for_deep_research_completion(
                sid,
                owner=owner,
                timeout_seconds=wait_timeout,
            )
            if sources:
                research_sources.extend(sources)
            if result_text:
                trimmed_report = result_text[:8000].strip()
                other_tool_outputs.append(f"[deep_research:{sid}]\n{trimmed_report}")
            if wait_error:
                other_tool_outputs.append(f"[deep_research:{sid}] {wait_error}")

    if research_sources:
        for src in research_sources:
            url = str(src.get("url") or "").strip()
            if not url:
                continue
            title = str(src.get("title") or src.get("name") or url).strip()
            web_sources.append({"title": title, "url": url})

    if _step_requests_web_search(step):
        verified_web_sources = [
            src for src in web_sources
            if str(src.get("url") or "").strip().lower().startswith(("http://", "https://"))
        ]
        if not verified_web_sources:
            fallback_context, fallback_sources, fallback_error, fallback_query, fallback_time_filter, fallback_strategy = await _fallback_web_search(
                objective=objective,
                step=step,
                owner=owner,
                session_id=session_id,
                prior_context=prior_context,
            )
            other_tool_outputs.append(
                f"[web_search:fallback_query] strategy={fallback_strategy}; query={fallback_query}; time_filter={fallback_time_filter or 'none'}"
            )
            if fallback_sources:
                web_sources.extend(fallback_sources)
                source_titles = "; ".join(
                    str(s.get("title") or s.get("url") or "")[:80]
                    for s in fallback_sources[:5]
                )
                other_tool_outputs.append(
                    f"[web_search:fallback] fetched {len(fallback_sources)} sources — {source_titles}"
                )
                if fallback_context:
                    # The model generated full_text BEFORE the search ran, so it
                    # responded from training knowledge (hallucinated data). Now that
                    # we have real results, regenerate the step output with them.
                    clean_ctx = fallback_context
                    # Strip "IMPORTANT INSTRUCTIONS" block that's aimed at chat UI, not us.
                    instr_pos = clean_ctx.upper().find("IMPORTANT INSTRUCTIONS")
                    if instr_pos > 0:
                        clean_ctx = clean_ctx[:instr_pos].strip()
                    clean_ctx = clean_ctx[:3500]
                    regen_messages = [
                        {
                            "role": "system",
                            "content": (
                                "You are completing one step in a background workflow. "
                                "Base your response only on the web search results provided below. "
                                "Do not add facts from training knowledge. "
                                "Cite sources using [1], [2], etc. where relevant."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Overall objective:\n{objective}\n\n"
                                f"This step: {step.title}\n{step.description}\n"
                                + (f"\nPrior steps output (context):\n{prior_context[:600]}\n" if prior_context else "")
                                + f"\nWeb search results retrieved on {datetime.date.today().isoformat()}:\n{clean_ctx}\n\n"
                                "Produce the output for this step using only the results above."
                            ),
                        },
                    ]
                    regen_result = await llm_call_async_with_fallback(
                        candidates, messages=regen_messages, timeout=45
                    )
                    if regen_result and regen_result.strip():
                        full_text = _strip_model_directives(regen_result.strip())
            else:
                if fallback_error:
                    other_tool_outputs.append(f"[web_search:fallback] {fallback_error}")
                if saw_web_search_signal:
                    raise RuntimeError(
                        "Step requested web_search but no verified source URLs were captured from the tool output."
                    )
                raise RuntimeError(
                    "Step requested web_search but no web_search tool output was captured. Refusing unverified generated trends."
                )

    appendix = _build_artifact_appendix(web_sources, python_outputs, other_tool_outputs)

    if full_text.strip():
        text = _strip_model_directives(full_text)
        if appendix:
            lower_text = text.lower()
            appendix_parts = [part for part in appendix.split("\n\n") if part.strip()]
            missing_parts = [part for part in appendix_parts if part.lower() not in lower_text]
            if missing_parts:
                text = text + "\n\n" + "\n\n".join(missing_parts)
        return text

    if tool_summaries:
        text = "\n\n".join(tool_summaries[-5:]).strip()
        if appendix:
            text = text + "\n\n" + appendix
        return text.strip()

    # Last-resort single call so a step does not end empty.
    fallback_text = await llm_call_async_with_fallback(
        candidates,
        messages=[
            {"role": "system", "content": "Summarize completed work for this step."},
            {"role": "user", "content": f"Step: {step.title}\nObjective: {objective}"},
        ],
        timeout=20,
    )
    text = (fallback_text or "").strip()
    if appendix:
        text = (text + "\n\n" + appendix).strip() if text else appendix
    return text or "(no output)"


async def reflect(
    step: GraphNode,
    output: str,
    owner: str | None = None,
    session_id: str | None = None,
) -> tuple[bool, str]:
    qc = step.quality_check
    if qc.type == "none":
        return True, "No quality gate for this step"

    if qc.type == "script":
        try:
            safe_globals = {"__builtins__": {"len": len, "any": any, "all": all}}
            safe_locals = {"output": output}
            passed = bool(eval(qc.criteria, safe_globals, safe_locals))
            return passed, "Script gate passed" if passed else "Script gate failed"
        except Exception as e:
            return False, f"Script quality check error: {e}"

    candidates = _resolve_candidates(owner=owner, session_id=session_id)
    if not candidates:
        return bool(output.strip()), "Fallback quality check without model"

    verdict = await llm_call_async_with_fallback(
        candidates,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are checking whether an automated workflow step produced a valid output. "
                    "If the output meaningfully addresses the step's stated goal, reply PASS. "
                    "Reply FAIL only if the output is empty, completely off-topic, or clearly wrong. "
                    "Creative writing, stories, summaries, and structured plans all count as valid outputs when they match the step goal. "
                    "Reply exactly with: PASS: <reason> or FAIL: <reason>."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Step goal: {step.title} — {step.description}\n"
                    f"Criteria: {qc.criteria or 'Output is non-empty and addresses the step goal.'}\n\n"
                    f"Output to evaluate:\n{output}"
                ),
            },
        ],
        timeout=20,
    )
    v = (verdict or "").strip()
    if v.upper().startswith("PASS"):
        return True, v
    if v.upper().startswith("FAIL"):
        return False, v
    return bool(output.strip()), v or "Fallback quality verdict"


