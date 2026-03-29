from __future__ import annotations

import time
from types import SimpleNamespace

import pandas as pd

import api.services as services_module
from api.chat_context import summarize_thread_state
from api.conversations import InMemoryConversationStore
from api.metric_labels import sentence_for_iv_analysis_series
from api.services import AnalyticsService


class DummyRepository:
    def load_state(self, force_refresh: bool = False) -> SimpleNamespace:
        return SimpleNamespace(available_dates=["2026-03-25", "2026-03-26"])


class DummyBedrockClient:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, object]] = []

    def chat(
        self,
        system_prompt: str,
        user_prompt: str | None = None,
        *,
        messages: list[dict[str, object]] | None = None,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return self.responses.pop(0) if self.responses else ""

    def chat_stream(self, *args, **kwargs):  # pragma: no cover - unused in these tests
        if False:
            yield args, kwargs


class DummyRetrievalService:
    def retrieve(self, query: str, top_k: int = 3, source_types: list[str] | None = None) -> list[dict[str, str]]:
        return []

    def get_context_bundle(self, *, max_chars: int = 7000) -> str:
        return "## Context\nUse evidence-first analysis."


def build_service(
    *,
    bedrock_responses: list[str] | None = None,
    chat_mode: str = "plan",
) -> tuple[AnalyticsService, DummyBedrockClient]:
    bedrock = DummyBedrockClient(bedrock_responses)
    service = AnalyticsService(
        repository=DummyRepository(),
        bedrock_client=bedrock,
        retrieval_service=DummyRetrievalService(),
        conversation_store=InMemoryConversationStore(),
        chat_mode=chat_mode,
        agentic_max_steps=3,
    )
    return service, bedrock


def sample_history() -> list[dict[str, object]]:
    return [
        {"role": "user", "text": "what was implied volatility over the past 2 weeks"},
        {
            "role": "assistant",
            "text": "IV answer",
            "as_of_date": "2026-03-26",
            "provenance": "data_tools",
            "tool_trace": [
                {
                    "tool": "get_iv_analysis",
                    "args": {
                        "analysis_type": "options_iv_series",
                        "window_days": 14,
                        "end_date": "2026-03-26",
                    },
                    "confidence": "medium",
                    "reasoning": "The question mentions implied volatility or IV.",
                }
            ],
        },
    ]


def test_heuristic_follow_up_inherits_last_tool_for_ambiguous_confirmation() -> None:
    service, _ = build_service()
    thread_state = summarize_thread_state(sample_history())

    plan = service._heuristic_plan("are you sure?", "2026-03-26", thread_state)

    assert plan["tool"] == "get_iv_analysis"
    assert plan["args"]["analysis_type"] == "options_iv_series"
    assert plan["args"]["window_days"] == 14
    assert plan["args"]["end_date"] == "2026-03-26"


def test_orchestrate_chat_includes_analytical_thread_in_planner_prompt() -> None:
    service, bedrock = build_service(
        bedrock_responses=['{"mode":"direct_answer","assistant_response":"ok","confidence":"high"}']
    )
    history = sample_history()
    thread_state = summarize_thread_state(history)
    conversation_context = service._conversation_context(history, thread_state)

    service._orchestrate_chat("are you sure?", "2026-03-26", history, conversation_context, thread_state)

    planner_call = bedrock.calls[0]
    user_prompt = str(planner_call["user_prompt"])
    system_prompt = str(planner_call["system_prompt"])
    assert "Analytical thread:" in user_prompt
    assert "Most recent analytic tool: get_iv_analysis" in user_prompt
    assert "Always-on context:" in system_prompt
    assert planner_call["max_tokens"] == 800
    assert planner_call["temperature"] == 0.25


def test_agentic_mode_runs_tool_loop_and_reuses_writer(monkeypatch) -> None:
    service, _ = build_service(
        chat_mode="agent",
        bedrock_responses=[
            '{"action":"tool","tool":"get_metric_series","args":{"metric":"options_volume","window_days":5},"confidence":"high","reasoning":"Need a series."}',
            '{"action":"final","assistant_response":"Draft answer","confidence":"high","reasoning":"Enough evidence."}',
        ],
    )

    def fake_execute_tool_step(message, requested_date, tool_name, args):
        return {
            "tool": tool_name,
            "args": {"metric": "options_volume", "window_days": 5, "end_date": "2026-03-26"},
            "result": {
                "metric": "options_volume",
                "metric_label": "total options volume",
                "window_days": 5,
                "end_date": "2026-03-26",
                "latest_value": 100,
                "starting_value": 80,
                "average_value": 90,
                "series": [
                    {"date": "2026-03-20", "value": 80},
                    {"date": "2026-03-26", "value": 100},
                ],
            },
            "fallback": "Total options volume was 100 on 2026-03-26.",
            "as_of_date": "2026-03-26",
        }

    monkeypatch.setattr(service, "_execute_tool_step", fake_execute_tool_step)
    monkeypatch.setattr(service, "_answer_from_tool_results", lambda *args, **kwargs: "Final composed answer")

    result = service._execute_agentic_turn(
        "what was options volume recently?",
        "2026-03-26",
        [],
        "",
        {},
    )

    assert result["answer"] == "Final composed answer"
    assert result["provenance"] == "data_tools"
    assert result["tool_trace"][0]["tool"] == "get_metric_series"


def test_query_metric_series_tracks_latest_observed_and_window_stats(monkeypatch) -> None:
    service, _ = build_service()

    def fake_load_state(force_refresh: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            available_dates=["2026-03-24", "2026-03-25", "2026-03-26"],
            options_df=pd.DataFrame(
                [
                    {"date": "2026-03-24", "volume": 10, "oi": 100, "oi_change": 5},
                    {"date": "2026-03-25", "volume": 12, "oi": 105, "oi_change": 5},
                    {"date": "2026-03-26", "volume": 8, "oi": 110, "oi_change": 5},
                ]
            ),
            futures_df=pd.DataFrame(
                [
                    {"date": "2026-03-24", "contract_month": "May-26", "volume": 1, "oi": 1, "oi_change": 0, "settle": 300},
                    {"date": "2026-03-25", "contract_month": "May-26", "volume": 1, "oi": 1, "oi_change": 0, "settle": 301},
                    {"date": "2026-03-26", "contract_month": "May-26", "volume": 1, "oi": 1, "oi_change": 0, "settle": 302},
                ]
            ),
            options_expiry_df=pd.DataFrame(),
            sofr_df=pd.DataFrame(),
        )

    monkeypatch.setattr(service.repository, "load_state", fake_load_state)
    monkeypatch.setattr(
        services_module,
        "build_daily_aggregates",
        lambda options_df, futures_df: pd.DataFrame(
            [{"date": "2026-03-24"}, {"date": "2026-03-25"}, {"date": "2026-03-26"}]
        ),
    )
    monkeypatch.setattr(
        services_module,
        "build_iv_series",
        lambda options_df, futures_df, options_expiry_df, sofr_df: pd.DataFrame(
            [
                {"date": "2026-03-24", "options_iv": 0.39},
                {"date": "2026-03-25", "options_iv": 0.40},
            ]
        ),
    )

    result = service.query_metric_series("options_iv", window_days=3, end_date="2026-03-26")

    assert result["latest_value"] is None
    assert result["end_date_value"] is None
    assert result["latest_observed_value"] == 40.0
    assert result["latest_observed_date"] == "2026-03-25"
    assert result["non_null_count"] == 2
    assert result["window_stats"] == {
        "min_value": 39.0,
        "min_date": "2026-03-24",
        "max_value": 40.0,
        "max_date": "2026-03-25",
    }


def test_query_metric_series_supports_realized_vol_and_atm_iv(monkeypatch) -> None:
    service, _ = build_service()

    def fake_load_state(force_refresh: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            available_dates=["2026-03-24", "2026-03-25", "2026-03-26"],
            options_df=pd.DataFrame(
                [
                    {"date": "2026-03-24", "volume": 10, "oi": 100, "oi_change": 5},
                    {"date": "2026-03-25", "volume": 12, "oi": 105, "oi_change": 5},
                    {"date": "2026-03-26", "volume": 8, "oi": 110, "oi_change": 5},
                ]
            ),
            futures_df=pd.DataFrame(
                [
                    {"date": "2026-03-24", "contract_month": "May-26", "volume": 1, "oi": 1, "oi_change": 0, "settle": 300},
                    {"date": "2026-03-25", "contract_month": "May-26", "volume": 1, "oi": 1, "oi_change": 0, "settle": 301},
                    {"date": "2026-03-26", "contract_month": "May-26", "volume": 1, "oi": 1, "oi_change": 0, "settle": 302},
                ]
            ),
            options_expiry_df=pd.DataFrame(),
            sofr_df=pd.DataFrame(),
        )

    monkeypatch.setattr(service.repository, "load_state", fake_load_state)
    monkeypatch.setattr(
        services_module,
        "build_daily_aggregates",
        lambda options_df, futures_df: pd.DataFrame(
            [{"date": "2026-03-24"}, {"date": "2026-03-25"}, {"date": "2026-03-26"}]
        ),
    )
    monkeypatch.setattr(
        services_module,
        "build_iv_series",
        lambda options_df, futures_df, options_expiry_df, sofr_df: pd.DataFrame(
            [{"date": "2026-03-24", "options_iv": 0.39}, {"date": "2026-03-25", "options_iv": 0.40}]
        ),
    )
    monkeypatch.setattr(
        services_module,
        "build_atm_iv_series",
        lambda options_df, futures_df, options_expiry_df, sofr_df: pd.DataFrame(
            [{"date": "2026-03-24", "atm_iv": 0.44}, {"date": "2026-03-25", "atm_iv": 0.45}]
        ),
    )
    monkeypatch.setattr(
        services_module,
        "build_historical_vol_series",
        lambda futures_df, max_date, lookback_days: [
            {"date": "2026-03-24", "hv_pct": 31.2},
            {"date": "2026-03-25", "hv_pct": 32.4},
            {"date": "2026-03-26", "hv_pct": 33.1},
        ],
    )

    realized = service.query_metric_series("realized_vol", window_days=3, end_date="2026-03-26")
    atm = service.query_metric_series("atm_iv", window_days=3, end_date="2026-03-26")

    assert realized["latest_value"] == 33.1
    assert realized["non_null_count"] == 3
    assert realized["window_stats"]["max_value"] == 33.1
    assert realized["window_stats"]["max_date"] == "2026-03-26"

    assert atm["latest_value"] is None
    assert atm["latest_observed_value"] == 45.0
    assert atm["latest_observed_date"] == "2026-03-25"
    assert atm["window_stats"]["min_value"] == 44.0
    assert atm["window_stats"]["max_value"] == 45.0


def test_sentence_for_iv_analysis_series_uses_latest_observed_when_end_date_missing() -> None:
    sentence = sentence_for_iv_analysis_series(
        {
            "analysis_type": "options_iv_series",
            "end_date": "2026-03-26",
            "window_days": 14,
            "latest_value": None,
            "latest_observed_value": 40.0,
            "latest_observed_date": "2026-03-25",
            "non_null_count": 10,
            "series": [
                {"date": "2026-03-25", "value": 40.0},
                {"date": "2026-03-26", "value": None},
            ],
        }
    )

    assert "missing on 2026-03-26" in sentence
    assert "40.0% on 2026-03-25" in sentence


def test_writer_summary_and_hints_use_short_iv_series() -> None:
    execution = {
        "tool": "get_iv_analysis",
        "fallback": "I don't have a complete options implied volatility over time figure.",
        "as_of_date": "2026-03-26",
        "result": {
            "analysis_type": "options_iv_series",
            "analysis_label": "options implied volatility over time",
            "latest_value": None,
            "end_date_value": None,
            "window_days": 14,
            "latest_observed_value": 40.0,
            "latest_observed_date": "2026-03-25",
            "non_null_count": 2,
            "window_stats": {
                "min_value": 39.0,
                "min_date": "2026-03-24",
                "max_value": 40.0,
                "max_date": "2026-03-25",
            },
            "series": [
                {"date": "2026-03-24", "value": 39.0},
                {"date": "2026-03-25", "value": 40.0},
                {"date": "2026-03-26", "value": None},
            ],
        },
    }

    summary = AnalyticsService._summarize_execution_for_writer(execution)
    hints = AnalyticsService._build_writer_hints("when was IV at 40?", [execution])

    assert summary["end_date_iv_missing"] is True
    assert len(summary["series"]) == 3
    assert summary["latest_observed_date"] == "2026-03-25"
    assert hints["threshold_query"]["last_match_date"] == "2026-03-25"
    assert hints["threshold_query"]["last_match_value"] == 40.0


def test_build_service_from_env_defaults_to_agent(monkeypatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("S3_BUCKET", "bucket")
    monkeypatch.setenv("S3_PREFIX", "prefix")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "model")
    monkeypatch.delenv("CHAT_MODE", raising=False)
    monkeypatch.setattr(services_module, "S3HistoricalRepository", lambda **kwargs: "repo")
    monkeypatch.setattr(services_module, "BedrockChatClient", lambda **kwargs: "bedrock")
    monkeypatch.setattr(services_module, "GuidanceRetrievalService", lambda **kwargs: "retrieval")
    monkeypatch.setattr(services_module, "build_conversation_store_from_env", lambda region: "store")

    service = services_module.build_service_from_env()

    assert service.chat_mode == "agent"


def test_build_service_from_env_prefers_agent_soft_deadline_and_bedrock_timeout(monkeypatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("S3_BUCKET", "bucket")
    monkeypatch.setenv("S3_PREFIX", "prefix")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "model")
    monkeypatch.setenv("CHAT_MODE", "agent")
    monkeypatch.setenv("CHAT_SOFT_DEADLINE_SECONDS", "40")
    monkeypatch.setenv("CHAT_AGENT_SOFT_DEADLINE_SECONDS", "95")
    monkeypatch.setenv("BEDROCK_READ_TIMEOUT_SECONDS", "150")
    monkeypatch.setattr(services_module, "S3HistoricalRepository", lambda **kwargs: "repo")
    monkeypatch.setattr(services_module, "GuidanceRetrievalService", lambda **kwargs: "retrieval")
    monkeypatch.setattr(services_module, "build_conversation_store_from_env", lambda region: "store")

    bedrock_kwargs: dict[str, object] = {}

    def fake_bedrock(**kwargs):
        bedrock_kwargs.update(kwargs)
        return "bedrock"

    monkeypatch.setattr(services_module, "BedrockChatClient", fake_bedrock)

    service = services_module.build_service_from_env()

    assert service.chat_mode == "agent"
    assert service.soft_deadline_seconds == 95.0
    assert bedrock_kwargs["read_timeout_seconds"] == 150.0


def test_run_tool_plan_steps_returns_partial_on_soft_deadline(monkeypatch) -> None:
    service, _ = build_service()
    service.heartbeat_seconds = 0.02

    def fake_execute_tool_step(message, requested_date, tool_name, args):
        if tool_name == "get_metric_series":
            time.sleep(0.01)
        else:
            time.sleep(0.25)
        return {
            "tool": tool_name,
            "args": args,
            "result": {"ok": True},
            "fallback": f"{tool_name} fallback",
            "as_of_date": "2026-03-26",
        }

    monkeypatch.setattr(service, "_execute_tool_step", fake_execute_tool_step)

    executions, tool_trace, soft_timed_out, planned_count = service._run_tool_plan_steps(
        [
            {"tool": "get_metric_series", "args": {"metric": "options_volume"}},
            {"tool": "get_market_snapshot", "args": {"date": "2026-03-26"}},
        ],
        "question",
        "2026-03-26",
        "high",
        "reason",
        soft_deadline_at=time.monotonic() + 0.08,
        min_partial_results=1,
    )

    assert soft_timed_out is True
    assert planned_count == 2
    assert len(executions) == 1
    assert executions[0]["tool"] == "get_metric_series"
    assert len(tool_trace) == 1
    assert tool_trace[0]["tool"] == "get_metric_series"


def test_stream_chat_turn_emits_soft_timeout_metadata(monkeypatch) -> None:
    service, _ = build_service(chat_mode="plan")
    service.soft_deadline_seconds = 0.05
    service.heartbeat_seconds = 0.01

    def slow_orchestrate(*args, **kwargs):
        time.sleep(0.2)
        return {"mode": "direct_answer", "assistant_response": "ok", "confidence": "high"}

    monkeypatch.setattr(service, "_orchestrate_chat", slow_orchestrate)

    events = list(
        service.stream_chat_turn(
            "analyze positioning",
            user_id="user-1",
            conversation_id=None,
            date="2026-03-26",
        )
    )
    kinds = [kind for kind, _ in events]
    done_payload = next(payload for kind, payload in events if kind == "done")

    assert "progress" in kinds
    assert "heartbeat" in kinds
    assert done_payload["response_mode"] == "partial_soft_timeout"
    assert done_payload["soft_timeout"] is True
    assert done_payload["completed_tools"] == []


def test_stream_chat_turn_emits_agent_progress_messages(monkeypatch) -> None:
    service, _ = build_service(
        chat_mode="agent",
        bedrock_responses=[
            '{"action":"tool","tool":"get_metric_series","args":{"metric":"options_volume","window_days":3},"confidence":"high"}',
            '{"action":"final","assistant_response":"done","confidence":"high"}',
        ],
    )

    def fake_execute_tool_step(message, requested_date, tool_name, args):
        return {
            "tool": tool_name,
            "args": args,
            "result": {
                "metric": "options_volume",
                "metric_label": "total options volume",
                "window_days": 3,
                "end_date": "2026-03-26",
                "latest_value": 123,
                "starting_value": 120,
                "average_value": 122,
                "series": [{"date": "2026-03-26", "value": 123}],
            },
            "fallback": "volume fallback",
            "as_of_date": "2026-03-26",
        }

    monkeypatch.setattr(service, "_execute_tool_step", fake_execute_tool_step)
    monkeypatch.setattr(service, "_answer_from_tool_results", lambda *args, **kwargs: "Final answer")

    events = list(
        service.stream_chat_turn(
            "what changed?",
            user_id="user-1",
            conversation_id=None,
            date="2026-03-26",
        )
    )

    progress_payloads = [payload for kind, payload in events if kind == "progress"]
    messages = [str(payload.get("message") or "") for payload in progress_payloads]

    assert any("Running analysis agent" in message for message in messages)
    assert any("Planning next step" in message for message in messages)
    assert any("Running get_metric_series" in message for message in messages)
    assert any("Writing answer" in message for message in messages)
