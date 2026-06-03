from src.orchestrator.nodes import (
    _extract_json_object,
    _derive_search_query,
    _normalized_step_tools,
    _resolve_step_model,
    _step_requests_deep_research,
    _step_requests_web_search,
)
from src.orchestrator.schemas import GraphNode


def test_normalizes_prefixed_tool_tokens():
    step = GraphNode(
        title="x",
        description="x",
        tools=["autotools:web_search", "tools:python", "autotools:none", " read_file "],
    )

    assert _normalized_step_tools(step) == ["web_search", "python", "read_file"]


def test_detects_web_search_from_prefixed_tool():
    step = GraphNode(title="x", description="x", tools=["autotools:web_search"])

    assert _step_requests_web_search(step) is True


def test_detects_deep_research_aliases():
    step = GraphNode(title="x", description="x", tools=["autotools:deep_research"])

    assert _step_requests_deep_research(step) is True


def test_resolve_step_model_treats_auto_as_fallback():
    step = GraphNode(title="x", description="x", tools=[], model="auto")

    assert _resolve_step_model(step, "gpt-4.1") == "gpt-4.1"


def test_derive_search_query_prefers_step_text_and_day_filter_for_trending():
    step = GraphNode(
        title="Find trending topic",
        description="Search for the top trending topic of today.",
        tools=["web_search"],
    )

    query, time_filter = _derive_search_query(
        "Write a short, cute story based on the top trending topic of today (Revised)",
        step,
    )

    assert "trending" in query.lower()
    assert time_filter == "day"


def test_derive_search_query_uses_objective_when_step_is_generic():
    step = GraphNode(
        title="Trend research",
        description="Investigate current trends using web_search.",
        tools=["web_search"],
    )

    query, time_filter = _derive_search_query(
        "Find current tax filing deadlines for freelancers in California",
        step,
    )

    assert "california" in query.lower()
    assert "tax" in query.lower()
    assert time_filter == "day"


def test_extract_json_object_parses_fenced_json():
    raw = """```json
{"query": "latest rust releases", "time_filter": "month"}
```"""

    parsed = _extract_json_object(raw)
    assert parsed is not None
    assert parsed.get("query") == "latest rust releases"
