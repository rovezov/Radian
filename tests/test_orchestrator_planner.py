"""Tests for the graph-based planner module.

_sanitize_plan has been removed as part of the migration to graph-based
execution plans. These tests cover the remaining planner logic:
- _classify_complex: LLM-based complexity classification with length fallback
- _select_blueprint: description-based blueprint selection
- _extract_json_payload: JSON extraction from LLM output
"""

import pytest

from src.orchestrator.planner import _extract_json_payload, _classify_complex, _select_blueprint
from src.orchestrator.schemas import ExecutionPlan, GraphNode, GraphEdge


# ---------------------------------------------------------------------------
# _extract_json_payload
# ---------------------------------------------------------------------------


def test_extract_json_payload_bare_json():
    raw = '{"plan_id": "abc", "nodes": []}'
    result = _extract_json_payload(raw)
    assert result is not None
    assert '"plan_id"' in result


def test_extract_json_payload_fenced_json():
    raw = '```json\n{"plan_id": "abc", "nodes": []}\n```'
    result = _extract_json_payload(raw)
    assert result is not None
    assert '"plan_id"' in result


def test_extract_json_payload_radian_plan_fence():
    raw = '```radian-plan\n{"plan_id": "abc", "nodes": []}\n```'
    result = _extract_json_payload(raw)
    assert result is not None
    assert '"plan_id"' in result


def test_extract_json_payload_returns_none_for_empty():
    assert _extract_json_payload("") is None
    assert _extract_json_payload("   ") is None


def test_extract_json_payload_extracts_from_surrounding_text():
    raw = 'Here is the plan:\n{"plan_id": "abc", "nodes": []} done.'
    result = _extract_json_payload(raw)
    assert result is not None
    assert '"plan_id"' in result


# ---------------------------------------------------------------------------
# _classify_complex — length-based fallback path (no LLM endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_complex_short_message_is_simple():
    result = await _classify_complex("Hello, how are you?", "", "", None)
    assert result is False


@pytest.mark.asyncio
async def test_classify_complex_long_message_is_complex():
    long_msg = "x " * 100  # 200 chars — above threshold
    result = await _classify_complex(long_msg, "", "", None)
    assert result is True


@pytest.mark.asyncio
async def test_classify_complex_exactly_180_is_simple():
    msg = "a" * 180
    result = await _classify_complex(msg, "", "", None)
    assert result is False  # 180 is NOT > 180


@pytest.mark.asyncio
async def test_classify_complex_181_is_complex():
    msg = "a" * 181
    result = await _classify_complex(msg, "", "", None)
    assert result is True


# ---------------------------------------------------------------------------
# _select_blueprint — fallback path with no LLM endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_blueprint_returns_empty_when_no_blueprints():
    # With no blueprints registered and no LLM, should return ""
    # We import all_blueprints to see what's registered; if empty, returns "".
    from src.orchestrator.blueprints import all_blueprints
    blueprints = all_blueprints()
    if not blueprints:
        result = await _select_blueprint("some objective", "", "", None)
        assert result == ""
    else:
        # At least returns one of the registered names.
        result = await _select_blueprint("some objective", "", "", None)
        registered_names = {bp.name for bp in blueprints}
        assert result in registered_names

