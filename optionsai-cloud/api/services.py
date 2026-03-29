import io
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
import pandas as pd
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from api.conversations import ConversationState, ConversationStore, build_conversation_store_from_env
from api.retrieval import GuidanceRetrievalService

from api.chat_context import (
    latest_tool_step,
    looks_like_ambiguous_follow_up,
    summarize_thread_for_prompt,
    summarize_thread_state,
)
from api.chat_heuristics import (
    average_numeric,
    classify_price_oi_regime,
    comparison_fallback,
    contract_activity_fallback,
    conversational_fallback,
    extract_contract_month,
    extract_requested_date,
    heuristic_chat_orchestration,
    heuristic_comparison_plan,
    infer_contract_metric,
    infer_interpretation_focus,
    infer_iv_analysis_type,
    infer_market,
    infer_metric,
    infer_positioning_analysis_type,
    infer_window_days,
    iv_analysis_fallback,
    looks_like_comparison_question,
    looks_like_contract_question,
    looks_like_general_question,
    looks_like_greeting,
    looks_like_interpretation_question,
    looks_like_iv_question,
    looks_like_positioning_question,
    looks_like_snapshot_request,
    normalize_comparison_type,
    normalize_contract_metric,
    normalize_interpretation_focus,
    normalize_iv_analysis_type,
    normalize_market_name,
    normalize_metric_name,
    normalize_orchestration_plan,
    normalize_positioning_analysis_type,
    normalize_source_types,
    normalize_tool_plan,
    normalize_window_days,
    parse_json_object,
    positioning_fallback,
    series_direction,
    shorten_title,
)
from api.chat_prompts import (
    AGENTIC_MAX_STEPS,
    AGENTIC_TOOL_LOOP_SCHEMA,
    AGENTIC_TOOL_LOOP_SYSTEM_PROMPT,
    CHAT_ORCHESTRATION_SCHEMA,
    CHAT_ORCHESTRATION_SYSTEM_PROMPT,
    DATASET_CONTEXT_PROMPT,
    DEFAULT_RETRIEVAL_TOP_K,
    DEFAULT_TOOL_WINDOW_DAYS,
    DIRECT_ANSWER_SYSTEM_PROMPT,
    DIRECT_WRITER_MAX_TOKENS,
    DIRECT_WRITER_TEMPERATURE,
    HOUSE_STYLE_GUIDANCE,
    PLANNER_MAX_TOKENS,
    PLANNER_TEMPERATURE,
    PROMPT_VERSION,
    TOOL_ANSWER_SYSTEM_PROMPT,
    TOOL_SELECTION_SCHEMA,
    TOOL_WRITER_MAX_TOKENS,
    TOOL_WRITER_TEMPERATURE,
)
from api.metric_labels import (
    display_name_for_comparison_metric,
    display_name_for_contract_metric,
    display_name_for_iv_analysis,
    display_name_for_market,
    display_name_for_metric,
    display_name_for_positioning,
    is_missing_value,
    sentence_for_metric_series,
)
from analyze import (
    CSO_EXPIRY_CSV,
    FUTURES_DATES_CSV,
    OPTIONS_EXPIRY_CSV,
    SOFR_EXCEL,
    _contract_month_sort_key,
    build_daily_aggregates,
    build_atm_iv_series,
    build_historical_vol_series,
    build_iv_series,
    compute_aggregate_positioning,
    compute_call_side_activity,
    compute_cso_daily_summary,
    compute_daily_metrics,
    compute_iv_by_delta_curves,
    compute_iv_summary,
    compute_nearby_vs_deferred,
    compute_strike_migration,
    compute_z_scores,
    generate_fact_pack,
    get_market_context,
)


DEFAULT_CACHE_TTL_SECONDS = 60
DEFAULT_CHAT_SOFT_DEADLINE_SECONDS = 90.0
DEFAULT_CHAT_HEARTBEAT_SECONDS = 2.0
DEFAULT_CHAT_PARTIAL_MIN_TOOLS = 1
DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS = 120.0


logger = logging.getLogger(__name__)


@dataclass
class HistoricalState:
    options_df: pd.DataFrame
    futures_df: pd.DataFrame
    cso_df: pd.DataFrame
    options_expiry_df: pd.DataFrame
    futures_dates_df: pd.DataFrame
    cso_expiry_df: pd.DataFrame
    sofr_df: pd.DataFrame
    loaded_at: float

    @property
    def available_dates(self) -> list[str]:
        dates = set()
        for df in (self.options_df, self.futures_df, self.cso_df):
            if not df.empty and "date" in df.columns:
                dates.update(df["date"].astype(str).tolist())
        return sorted(dates)


def _normalize_prefix(prefix: str | None) -> str:
    if not prefix:
        return ""
    return prefix.strip().strip("/")


def _state_prefix(prefix: str) -> str:
    normalized = _normalize_prefix(prefix)
    if normalized.endswith("/state"):
        return normalized
    return f"{normalized}/state" if normalized else "state"


def _base_prefix(prefix: str) -> str:
    normalized = _normalize_prefix(prefix)
    if normalized.endswith("/state"):
        return normalized[: -len("/state")]
    return normalized


def _join_s3_key(*parts: str) -> str:
    return "/".join(part.strip("/") for part in parts if part and part.strip("/"))


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float):
        if pd.isna(value) or value in (float("inf"), float("-inf")):
            return None
        return value
    if pd.isna(value):
        return None
    return value


def _series_observation_summary(series: list[dict[str, Any]]) -> dict[str, Any]:
    clean_points = [
        point for point in series
        if not is_missing_value(point.get("value"))
    ]
    if not clean_points:
        return {
            "non_null_count": 0,
            "latest_observed_value": None,
            "latest_observed_date": None,
            "window_stats": None,
        }

    latest_observed = clean_points[-1]
    min_point = min(clean_points, key=lambda point: float(point["value"]))
    max_point = max(clean_points, key=lambda point: float(point["value"]))
    return {
        "non_null_count": len(clean_points),
        "latest_observed_value": latest_observed.get("value"),
        "latest_observed_date": latest_observed.get("date"),
        "window_stats": {
            "min_value": min_point.get("value"),
            "min_date": min_point.get("date"),
            "max_value": max_point.get("value"),
            "max_date": max_point.get("date"),
        },
    }


def _series_for_writer(series: list[dict[str, Any]], *, full_cap: int, tail: int) -> list[dict[str, Any]]:
    if len(series) <= full_cap:
        return list(series)
    return series[-tail:]


def _extract_level_target(message: str) -> tuple[float, float] | None:
    lowered = message.lower()
    if not looks_like_iv_question(message):
        return None
    match = re.search(
        r"\b(?:at|around|about|near|nearby|close to|touch(?:ed)?|hit|reach(?:ed)?)\s+(\d+(?:\.\d+)?)%?",
        lowered,
    )
    if not match:
        return None
    target = float(match.group(1))
    tolerance = 2.0 if any(word in lowered for word in ("around", "about", "near", "close to")) else 1.0
    return target, tolerance


def _last_date_near_target(
    series: list[dict[str, Any]],
    *,
    target: float,
    tolerance: float,
) -> dict[str, Any] | None:
    for point in reversed(series):
        value = point.get("value")
        if is_missing_value(value):
            continue
        if abs(float(value) - target) <= tolerance:
            return {
                "date": point.get("date"),
                "value": value,
            }
    return None


class S3HistoricalRepository:
    def __init__(self, bucket: str, prefix: str, region: str):
        self.bucket = bucket
        self.prefix = prefix
        self.region = region
        self.client = boto3.client("s3", region_name=region)
        self._cached_state: HistoricalState | None = None
        self._cached_at = 0.0

    def _read_object_bytes(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except (ClientError, BotoCoreError) as exc:
            raise RuntimeError(f"Unable to read s3://{self.bucket}/{key}") from exc

    def _read_csv(self, key: str) -> pd.DataFrame:
        data = self._read_object_bytes(key)
        return pd.read_csv(io.BytesIO(data))

    def _read_excel(self, key: str) -> pd.DataFrame:
        data = self._read_object_bytes(key)
        return pd.read_excel(io.BytesIO(data), engine="openpyxl")

    def load_state(self, force_refresh: bool = False) -> HistoricalState:
        now = time.time()
        if (
            not force_refresh
            and self._cached_state is not None
            and (now - self._cached_at) < DEFAULT_CACHE_TTL_SECONDS
        ):
            return self._cached_state

        state_prefix = _state_prefix(self.prefix)
        base_prefix = _base_prefix(self.prefix)

        options_key = _join_s3_key(state_prefix, "historical_options.csv")
        futures_key = _join_s3_key(state_prefix, "historical_futures.csv")
        cso_key = _join_s3_key(state_prefix, "historical_cso.csv")
        options_expiry_key = _join_s3_key(base_prefix, "reference", "dates", OPTIONS_EXPIRY_CSV.name)
        futures_dates_key = _join_s3_key(base_prefix, "reference", "dates", FUTURES_DATES_CSV.name)
        cso_expiry_key = _join_s3_key(base_prefix, "reference", "dates", CSO_EXPIRY_CSV.name)
        sofr_key = _join_s3_key(base_prefix, "reference", "rates", SOFR_EXCEL.name)

        state = HistoricalState(
            options_df=self._read_csv(options_key),
            futures_df=self._read_csv(futures_key),
            cso_df=self._read_csv(cso_key),
            options_expiry_df=self._read_csv(options_expiry_key),
            futures_dates_df=self._read_csv(futures_dates_key),
            cso_expiry_df=self._read_csv(cso_expiry_key),
            sofr_df=self._read_excel(sofr_key),
            loaded_at=now,
        )
        self._cached_state = state
        self._cached_at = now
        return state


class BedrockChatClient:
    def __init__(
        self,
        region: str,
        model_id: str,
        *,
        read_timeout_seconds: float = DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS,
    ):
        self.model_id = model_id
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(read_timeout=max(1.0, float(read_timeout_seconds))),
        )

    def chat(
        self,
        system_prompt: str,
        user_prompt: str | None = None,
        *,
        messages: list[dict[str, Any]] | None = None,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ) -> str:
        request_messages = messages or []
        if user_prompt is not None:
            request_messages.append({"role": "user", "content": [{"text": user_prompt}]})
        response = self.client.converse(
            modelId=self.model_id,
            system=[{"text": system_prompt}],
            messages=request_messages,
            inferenceConfig={"temperature": temperature, "maxTokens": max_tokens},
        )
        content = response.get("output", {}).get("message", {}).get("content", [])
        parts = [item.get("text", "") for item in content if item.get("text")]
        return "\n".join(parts).strip()

    def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str | None = None,
        *,
        messages: list[dict[str, Any]] | None = None,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ) -> Iterator[str]:
        request_messages = messages or []
        if user_prompt is not None:
            request_messages.append({"role": "user", "content": [{"text": user_prompt}]})
        response = self.client.converse_stream(
            modelId=self.model_id,
            system=[{"text": system_prompt}],
            messages=request_messages,
            inferenceConfig={"temperature": temperature, "maxTokens": max_tokens},
        )
        stream = response.get("stream") or ()
        for event in stream:
            delta_piece = _extract_stream_text_delta(event)
            if delta_piece:
                yield delta_piece


def _extract_stream_text_delta(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    block = event.get("contentBlockDelta")
    if not isinstance(block, dict):
        return ""
    delta = block.get("delta") or {}
    if not isinstance(delta, dict):
        return ""
    return str(delta.get("text") or "")


class AnalyticsService:
    def __init__(
        self,
        repository: S3HistoricalRepository,
        bedrock_client: BedrockChatClient,
        retrieval_service: GuidanceRetrievalService,
        conversation_store: ConversationStore,
        *,
        chat_mode: str = "plan",
        agentic_max_steps: int = AGENTIC_MAX_STEPS,
        soft_deadline_seconds: float = DEFAULT_CHAT_SOFT_DEADLINE_SECONDS,
        heartbeat_seconds: float = DEFAULT_CHAT_HEARTBEAT_SECONDS,
        partial_min_tools: int = DEFAULT_CHAT_PARTIAL_MIN_TOOLS,
    ):
        self.repository = repository
        self.bedrock_client = bedrock_client
        self.retrieval_service = retrieval_service
        self.conversation_store = conversation_store
        self.chat_mode = chat_mode.strip().lower() if chat_mode else "plan"
        self.agentic_max_steps = max(1, int(agentic_max_steps))
        self.soft_deadline_seconds = max(0.0, float(soft_deadline_seconds))
        self.heartbeat_seconds = max(0.5, float(heartbeat_seconds))
        self.partial_min_tools = max(1, int(partial_min_tools))

    def list_conversations(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.conversation_store.list_conversations(user_id, limit=limit)

    def get_conversation(self, conversation_id: str, user_id: str) -> ConversationState:
        conversation = self.conversation_store.get_conversation(conversation_id, user_id)
        if conversation is None:
            raise ValueError(f"Conversation '{conversation_id}' was not found.")
        return conversation

    def available_dates(self) -> list[str]:
        return self.repository.load_state().available_dates

    def latest_date(self) -> str:
        dates = self.available_dates()
        if not dates:
            raise RuntimeError("No historical dates are available in S3 state.")
        return dates[-1]

    def build_snapshot(self, date: str | None = None, force_refresh: bool = False) -> dict[str, Any]:
        state = self.repository.load_state(force_refresh=force_refresh)
        as_of_date = date or self.latest_date()

        metrics = compute_daily_metrics(as_of_date, state.options_df)
        if metrics is None:
            raise ValueError(f"No options data found for {as_of_date}.")

        market_context = get_market_context(as_of_date, state.futures_df)
        cso_summary = compute_cso_daily_summary(as_of_date, state.cso_df)
        daily_aggregates = build_daily_aggregates(state.options_df, state.futures_df)
        z_scores = compute_z_scores(as_of_date, daily_aggregates)

        metrics["aggregate_positioning"] = compute_aggregate_positioning(as_of_date, state.options_df)
        metrics["strike_migration"] = compute_strike_migration(as_of_date, state.options_df)
        metrics["call_side_activity"] = compute_call_side_activity(as_of_date, state.options_df)
        metrics["nearby_vs_deferred"] = compute_nearby_vs_deferred(as_of_date, state.options_df)

        try:
            iv_summary = compute_iv_summary(
                as_of_date,
                state.options_df,
                state.futures_df,
                state.options_expiry_df,
                state.sofr_df,
            )
        except Exception:
            iv_summary = None

        if iv_summary:
            metrics["iv_summary"] = iv_summary

        fact_pack = generate_fact_pack(
            metrics=metrics,
            market_context=market_context,
            z_scores=z_scores,
            cso_summary=cso_summary,
            for_ai=True,
        )

        return _sanitize(
            {
                "date": as_of_date,
                "market_context": market_context,
                "metrics": metrics,
                "cso_summary": cso_summary,
                "z_scores": z_scores,
                "fact_pack": fact_pack,
                "available_dates": state.available_dates,
            }
        )

    def query_metric_series(
        self,
        metric: str,
        window_days: int = 30,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        state = self.repository.load_state()
        target_date = end_date or self.latest_date()

        options_daily = (
            state.options_df.groupby("date")
            .agg(
                options_total_oi=("oi", "sum"),
                options_volume=("volume", "sum"),
                options_oi_change=("oi_change", "sum"),
            )
            .reset_index()
        )
        futures_daily = (
            state.futures_df.sort_values(
                "contract_month",
                key=lambda series: series.map(_contract_month_sort_key),
            )
            .groupby("date")
            .first()
            .reset_index()[["date", "volume", "oi", "oi_change", "settle"]]
            .rename(
                columns={
                    "volume": "futures_volume",
                    "oi": "futures_oi",
                    "oi_change": "futures_oi_change",
                    "settle": "front_month_settle",
                }
            )
        )
        aggregate = build_daily_aggregates(state.options_df, state.futures_df)
        iv_series = build_iv_series(
            state.options_df,
            state.futures_df,
            state.options_expiry_df,
            state.sofr_df,
        )
        merged = (
            options_daily.merge(futures_daily, on="date", how="left")
            .merge(aggregate, on="date", how="left", suffixes=("", "_agg"))
            .merge(iv_series, on="date", how="left")
        )

        atm_series = build_atm_iv_series(
            state.options_df,
            state.futures_df,
            state.options_expiry_df,
            state.sofr_df,
        )
        if not atm_series.empty:
            merged = merged.merge(atm_series[["date", "atm_iv"]], on="date", how="left")
        elif "atm_iv" not in merged.columns:
            merged["atm_iv"] = pd.NA

        max_date_for_hv = str(merged["date"].max()) if not merged.empty else target_date
        hv_lookback_days = max(len(merged) + 50, max(window_days, 0) + 50, 200)
        hv_series = build_historical_vol_series(
            state.futures_df,
            max_date=max_date_for_hv,
            lookback_days=hv_lookback_days,
        )
        if hv_series:
            hv_df = pd.DataFrame(hv_series).rename(columns={"hv_pct": "realized_vol"})
            merged = merged.merge(hv_df[["date", "realized_vol"]], on="date", how="left")
        elif "realized_vol" not in merged.columns:
            merged["realized_vol"] = pd.NA

        merged = merged.sort_values("date").reset_index(drop=True)

        if metric not in merged.columns:
            raise ValueError(f"Unsupported metric '{metric}'.")

        if target_date not in merged["date"].values:
            raise ValueError(f"Date '{target_date}' is not available.")

        target_idx = merged.index[merged["date"] == target_date][0]
        start_idx = max(0, target_idx - max(window_days - 1, 0))
        window_df = merged.iloc[start_idx : target_idx + 1][["date", metric]].copy()
        window_df = window_df.rename(columns={metric: "value"})

        if metric in {"options_iv", "atm_iv"}:
            window_df["value"] = window_df["value"].apply(
                lambda value: round(float(value) * 100, 2) if pd.notna(value) else value
            )

        latest_value = window_df.iloc[-1]["value"]
        baseline = window_df.iloc[0]["value"]
        values = [value for value in window_df["value"].tolist() if pd.notna(value)]
        average_value = sum(values) / len(values) if values else None
        series = window_df.to_dict(orient="records")
        series_stats = _series_observation_summary(series)

        return _sanitize(
            {
                "metric": metric,
                "metric_label": display_name_for_metric(metric),
                "window_days": window_days,
                "end_date": target_date,
                "end_date_value": latest_value,
                "latest_value": latest_value,
                "starting_value": baseline,
                "average_value": average_value,
                **series_stats,
                "series": series,
            }
        )

    def get_contract_activity(
        self,
        *,
        contract_month: str | None = None,
        market: str = "options",
        metric: str = "volume",
        window_days: int = DEFAULT_TOOL_WINDOW_DAYS,
        end_date: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        state = self.repository.load_state()
        market_name = normalize_market_name(market)
        target_date = date or end_date or self.latest_date()

        frame = self._market_frame(state, market_name)
        if frame.empty:
            raise ValueError(f"No {market_name} data is available.")

        if contract_month:
            contract_df = frame[frame["contract_month"] == contract_month].copy()
            if contract_df.empty:
                raise ValueError(f"No {market_name} rows found for contract '{contract_month}'.")

            metric_name = normalize_contract_metric(metric, market_name)
            if metric_name not in contract_df.columns:
                raise ValueError(f"Unsupported contract metric '{metric_name}' for {market_name}.")

            daily_series = (
                contract_df.groupby("date")[metric_name]
                .sum()
                .reset_index(name="value")
                .sort_values("date")
                .reset_index(drop=True)
            )
            if target_date not in daily_series["date"].values:
                raise ValueError(f"Date '{target_date}' is not available for contract '{contract_month}'.")

            target_idx = daily_series.index[daily_series["date"] == target_date][0]
            start_idx = max(0, target_idx - max(window_days - 1, 0))
            sliced = daily_series.iloc[start_idx : target_idx + 1].copy()
            return _sanitize(
                {
                    "market": market_name,
                    "market_label": display_name_for_market(market_name),
                    "contract_month": contract_month,
                    "metric": metric_name,
                    "metric_label": display_name_for_contract_metric(metric_name),
                    "end_date": target_date,
                    "window_days": window_days,
                    "latest_value": sliced.iloc[-1]["value"],
                    "starting_value": sliced.iloc[0]["value"],
                    "series": sliced.to_dict(orient="records"),
                }
            )

        if target_date not in frame["date"].astype(str).values:
            raise ValueError(f"Date '{target_date}' is not available.")

        daily_df = frame[frame["date"].astype(str) == str(target_date)].copy()
        if daily_df.empty:
            raise ValueError(f"No {market_name} data found for {target_date}.")

        metric_name = normalize_contract_metric(metric, market_name)
        breakdown = (
            daily_df.groupby("contract_month")
            .agg(
                volume=("volume", "sum"),
                oi=("oi", "sum"),
                oi_change=("oi_change", "sum"),
            )
            .reset_index()
            .sort_values("contract_month", key=lambda c: c.map(_contract_month_sort_key))
        )
        if market_name == "futures" and "settle" in daily_df.columns:
            settles = (
                daily_df.sort_values("contract_month", key=lambda c: c.map(_contract_month_sort_key))
                .groupby("contract_month", as_index=False)
                .first()[["contract_month", "settle"]]
            )
            breakdown = breakdown.merge(settles, on="contract_month", how="left")

        if metric_name in breakdown.columns:
            breakdown = breakdown.sort_values(metric_name, ascending=False)

        return _sanitize(
            {
                "market": market_name,
                "market_label": display_name_for_market(market_name),
                "date": target_date,
                "metric": metric_name,
                "metric_label": display_name_for_contract_metric(metric_name),
                "contracts": breakdown.to_dict(orient="records"),
            }
        )

    def get_iv_analysis(
        self,
        *,
        analysis_type: str,
        window_days: int = 10,
        end_date: str | None = None,
        date: str | None = None,
        contract_month: str | None = None,
    ) -> dict[str, Any]:
        state = self.repository.load_state()
        target_date = date or end_date or self.latest_date()
        analysis_kind = normalize_iv_analysis_type(analysis_type)

        if analysis_kind == "options_iv_series":
            series_payload = dict(
                self.query_metric_series("options_iv", window_days=window_days, end_date=target_date)
            )
            series_payload["analysis_type"] = analysis_kind
            series_payload["analysis_label"] = display_name_for_iv_analysis(analysis_kind)
            return _sanitize(series_payload)

        if analysis_kind == "atm_iv_series":
            atm_series = build_atm_iv_series(
                state.options_df,
                state.futures_df,
                state.options_expiry_df,
                state.sofr_df,
            )
            atm_payload = dict(
                self._slice_named_series(atm_series, "atm_iv", target_date, window_days, pct_scale=True)
            )
            atm_payload["analysis_type"] = analysis_kind
            atm_payload["analysis_label"] = display_name_for_iv_analysis(analysis_kind)
            return _sanitize(atm_payload)

        if analysis_kind == "iv_vs_realized_vol":
            atm_series = build_atm_iv_series(
                state.options_df,
                state.futures_df,
                state.options_expiry_df,
                state.sofr_df,
            )
            atm_window = self._slice_named_series(atm_series, "atm_iv", target_date, window_days, pct_scale=True)
            hv_series = build_historical_vol_series(
                state.futures_df,
                max_date=target_date,
                lookback_days=window_days,
            )
            hv_by_date = {item["date"]: item["hv_pct"] for item in hv_series}
            combined = []
            for point in atm_window["series"]:
                combined.append(
                    {
                        "date": point["date"],
                        "atm_iv_pct": point["value"],
                        "realized_vol_pct": hv_by_date.get(point["date"]),
                    }
                )
            return _sanitize(
                {
                    "analysis_type": analysis_kind,
                    "analysis_label": display_name_for_iv_analysis(analysis_kind),
                    "end_date": target_date,
                    "window_days": window_days,
                    "series": combined,
                }
            )

        if analysis_kind == "iv_term_structure":
            snapshot = self.build_snapshot(date=target_date)
            avg_by_expiry = ((snapshot["metrics"].get("iv_summary") or {}).get("avg_by_expiry")) or {}
            rows = [
                {"contract_month": contract, "iv_pct": round(float(value) * 100, 2)}
                for contract, value in sorted(avg_by_expiry.items(), key=lambda item: _contract_month_sort_key(item[0]))
            ]
            if contract_month:
                rows = [row for row in rows if row["contract_month"] == contract_month]
            return _sanitize(
                {
                    "analysis_type": analysis_kind,
                    "analysis_label": display_name_for_iv_analysis(analysis_kind),
                    "date": target_date,
                    "contracts": rows,
                }
            )

        if analysis_kind == "iv_by_delta":
            curves = compute_iv_by_delta_curves(
                target_date,
                state.options_df,
                state.futures_df,
                state.options_expiry_df,
                state.sofr_df,
            )
            if contract_month:
                curves = {contract_month: curves.get(contract_month, {})}
            return _sanitize(
                {
                    "analysis_type": analysis_kind,
                    "analysis_label": display_name_for_iv_analysis(analysis_kind),
                    "date": target_date,
                    "curves": _sanitize(curves),
                }
            )

        raise ValueError(f"Unsupported IV analysis type '{analysis_kind}'.")

    def get_positioning_analysis(
        self,
        *,
        analysis_type: str,
        date: str | None = None,
        lookback_days: int = 5,
    ) -> dict[str, Any]:
        state = self.repository.load_state()
        target_date = date or self.latest_date()
        analysis_kind = normalize_positioning_analysis_type(analysis_type)

        if analysis_kind == "aggregate_positioning":
            result = compute_aggregate_positioning(target_date, state.options_df, lookback_days=lookback_days)
        elif analysis_kind == "strike_migration":
            result = compute_strike_migration(target_date, state.options_df, lookback_days=max(lookback_days, 3))
        elif analysis_kind == "call_side_activity":
            result = compute_call_side_activity(target_date, state.options_df, lookback_days=lookback_days)
        elif analysis_kind == "nearby_vs_deferred":
            result = compute_nearby_vs_deferred(target_date, state.options_df, lookback_days=max(lookback_days, 3))
        else:
            raise ValueError(f"Unsupported positioning analysis type '{analysis_kind}'.")

        if result is None:
            raise ValueError(f"No positioning analysis available for '{analysis_kind}' on {target_date}.")
        return _sanitize(
            {
                "analysis_type": analysis_kind,
                "analysis_label": display_name_for_positioning(analysis_kind),
                "date": target_date,
                "result": result,
            }
        )

    def compare_periods(
        self,
        *,
        comparison_type: str,
        metric: str | None = None,
        primary_date: str | None = None,
        secondary_date: str | None = None,
        end_date: str | None = None,
        window_days: int = DEFAULT_TOOL_WINDOW_DAYS,
        market: str = "options",
        contract_month: str | None = None,
    ) -> dict[str, Any]:
        comparison_kind = normalize_comparison_type(comparison_type)

        if comparison_kind == "two_dates":
            if not primary_date or not secondary_date:
                raise ValueError("two_dates comparison requires primary_date and secondary_date.")
            if metric:
                normalized_metric = normalize_metric_name(metric) or normalize_contract_metric(metric, normalize_market_name(market))
                if contract_month:
                    first = self.get_contract_activity(
                        contract_month=contract_month,
                        market=market,
                        metric=normalized_metric or metric,
                        window_days=1,
                        end_date=primary_date,
                    )
                    second = self.get_contract_activity(
                        contract_month=contract_month,
                        market=market,
                        metric=normalized_metric or metric,
                        window_days=1,
                        end_date=secondary_date,
                    )
                    first_value = first["latest_value"]
                    second_value = second["latest_value"]
                else:
                    first = self.query_metric_series(normalized_metric or "options_volume", window_days=1, end_date=primary_date)
                    second = self.query_metric_series(normalized_metric or "options_volume", window_days=1, end_date=secondary_date)
                    first_value = first["latest_value"]
                    second_value = second["latest_value"]
                return _sanitize(
                    {
                        "comparison_type": comparison_kind,
                        "metric": normalized_metric,
                        "metric_label": display_name_for_comparison_metric(normalized_metric),
                        "primary_date": primary_date,
                        "secondary_date": secondary_date,
                        "primary_value": first_value,
                        "secondary_value": second_value,
                        "absolute_change": second_value - first_value if first_value is not None and second_value is not None else None,
                    }
                )

            first_snapshot = self.build_snapshot(date=primary_date)
            second_snapshot = self.build_snapshot(date=secondary_date)
            return _sanitize(
                {
                    "comparison_type": comparison_kind,
                    "primary_date": primary_date,
                    "secondary_date": secondary_date,
                    "primary_overview": first_snapshot["metrics"]["overview"],
                    "secondary_overview": second_snapshot["metrics"]["overview"],
                }
            )

        if comparison_kind == "window_vs_previous":
            normalized_metric = normalize_metric_name(metric) or normalize_contract_metric(metric, normalize_market_name(market))
            target_date = end_date or self.latest_date()
            if contract_month:
                series_payload = self.get_contract_activity(
                    contract_month=contract_month,
                    market=market,
                    metric=normalized_metric or "volume",
                    window_days=window_days * 2,
                    end_date=target_date,
                )
            else:
                series_payload = self.query_metric_series(
                    normalized_metric or "options_volume",
                    window_days=window_days * 2,
                    end_date=target_date,
                )
            series = series_payload["series"]
            if len(series) < 2:
                raise ValueError("Not enough history to compare windows.")
            current_window = series[-window_days:]
            previous_window = series[-(window_days * 2) : -window_days]
            current_avg = average_numeric([point["value"] for point in current_window])
            previous_avg = average_numeric([point["value"] for point in previous_window])
            return _sanitize(
                {
                    "comparison_type": comparison_kind,
                    "metric": normalized_metric,
                    "metric_label": display_name_for_comparison_metric(normalized_metric),
                    "end_date": target_date,
                    "window_days": window_days,
                    "current_window_average": current_avg,
                    "previous_window_average": previous_avg,
                    "absolute_change": current_avg - previous_avg if current_avg is not None and previous_avg is not None else None,
                    "current_window": current_window,
                    "previous_window": previous_window,
                }
            )

        if comparison_kind == "period_summary":
            target_date = end_date or self.latest_date()
            dates = self.available_dates()
            if target_date not in dates:
                raise ValueError(f"Date '{target_date}' is not available.")
            end_idx = dates.index(target_date)
            start_idx = max(0, end_idx - max(window_days - 1, 0))
            start_date = dates[start_idx]
            start_snapshot = self.build_snapshot(date=start_date)
            end_snapshot = self.build_snapshot(date=target_date)
            start_overview = start_snapshot["metrics"]["overview"]
            end_overview = end_snapshot["metrics"]["overview"]
            return _sanitize(
                {
                    "comparison_type": comparison_kind,
                    "start_date": start_date,
                    "end_date": target_date,
                    "volume_change": end_overview["total_volume"] - start_overview["total_volume"],
                    "oi_change_change": end_overview["total_oi_change"] - start_overview["total_oi_change"],
                    "total_oi_change": end_overview["total_oi"] - start_overview["total_oi"],
                    "start_overview": start_overview,
                    "end_overview": end_overview,
                }
            )

        raise ValueError(f"Unsupported comparison type '{comparison_kind}'.")

    def interpret_market_context(
        self,
        *,
        focus: str,
        date: str | None = None,
        window_days: int = 15,
    ) -> dict[str, Any]:
        target_date = date or self.latest_date()
        focus_name = normalize_interpretation_focus(focus)

        if focus_name == "open_interest_price":
            oi_series = self.query_metric_series("options_total_oi", window_days=window_days, end_date=target_date)
            oi_change_series = self.query_metric_series("options_oi_change", window_days=window_days, end_date=target_date)
            price_series = self.query_metric_series("front_month_settle", window_days=window_days, end_date=target_date)
            volume_series = self.query_metric_series("options_volume", window_days=window_days, end_date=target_date)
            snapshot = self.build_snapshot(date=target_date)
            regime = classify_price_oi_regime(price_series, oi_series)
            return _sanitize(
                {
                    "analysis_type": "open_interest_price",
                    "date": target_date,
                    "window_days": window_days,
                    "headline": regime["headline"],
                    "regime_hint": regime,
                    "price_series": price_series["series"][-5:],
                    "oi_series": oi_series["series"][-5:],
                    "oi_change_series": oi_change_series["series"][-5:],
                    "volume_series": volume_series["series"][-5:],
                    "snapshot_overview": ((snapshot.get("metrics") or {}).get("overview")) or {},
                    "market_context": snapshot.get("market_context"),
                }
            )

        if focus_name == "implied_volatility":
            iv_series = self.get_iv_analysis(
                analysis_type="options_iv_series",
                window_days=window_days,
                end_date=target_date,
            )
            iv_vs_realized = self.get_iv_analysis(
                analysis_type="iv_vs_realized_vol",
                window_days=max(window_days, 10),
                end_date=target_date,
            )
            term_structure = self.get_iv_analysis(
                analysis_type="iv_term_structure",
                date=target_date,
            )
            return _sanitize(
                {
                    "analysis_type": "implied_volatility",
                    "date": target_date,
                    "window_days": window_days,
                    "headline": iv_analysis_fallback(iv_series),
                    "iv_series": iv_series.get("series")[-5:],
                    "latest_value": iv_series.get("latest_value"),
                    "iv_vs_realized": iv_vs_realized.get("series")[-5:],
                    "term_structure": term_structure.get("contracts")[:5],
                }
            )

        if focus_name == "positioning":
            aggregate = self.get_positioning_analysis(
                analysis_type="aggregate_positioning",
                date=target_date,
                lookback_days=max(window_days, 5),
            )
            nearby = self.get_positioning_analysis(
                analysis_type="nearby_vs_deferred",
                date=target_date,
                lookback_days=max(window_days, 5),
            )
            return _sanitize(
                {
                    "analysis_type": "positioning",
                    "date": target_date,
                    "window_days": window_days,
                    "headline": positioning_fallback(aggregate),
                    "aggregate_summary": ((aggregate.get("result") or {}).get("summary")) or [],
                    "nearby_vs_deferred": ((nearby.get("result") or {}).get("summary")) or [],
                }
            )

        snapshot = self.build_snapshot(date=target_date)
        return _sanitize(
            {
                "analysis_type": "market_summary",
                "date": target_date,
                "window_days": window_days,
                "headline": self._snapshot_fallback(snapshot),
                "overview": ((snapshot.get("metrics") or {}).get("overview")) or {},
                "fact_pack": (snapshot.get("fact_pack") or [])[:5],
                "market_context": snapshot.get("market_context"),
            }
        )

    def _market_frame(self, state: HistoricalState, market: str) -> pd.DataFrame:
        if market == "options":
            return state.options_df.copy()
        if market == "futures":
            return state.futures_df.copy()
        if market == "cso":
            return state.cso_df.copy()
        raise ValueError(f"Unsupported market '{market}'.")

    @staticmethod
    def _slice_named_series(
        frame: pd.DataFrame,
        value_column: str,
        end_date: str,
        window_days: int,
        *,
        pct_scale: bool = False,
    ) -> dict[str, Any]:
        if frame.empty or value_column not in frame.columns:
            raise ValueError(f"No series data available for '{value_column}'.")
        if end_date not in frame["date"].astype(str).values:
            raise ValueError(f"Date '{end_date}' is not available.")
        series = frame.sort_values("date").reset_index(drop=True).copy()
        target_idx = series.index[series["date"] == end_date][0]
        start_idx = max(0, target_idx - max(window_days - 1, 0))
        sliced = series.iloc[start_idx : target_idx + 1][["date", value_column]].copy()
        if pct_scale:
            sliced[value_column] = sliced[value_column].apply(
                lambda value: round(float(value) * 100, 2) if pd.notna(value) else value
            )
        sliced = sliced.rename(columns={value_column: "value"})
        return _sanitize(
            {
                "metric": value_column,
                "metric_label": display_name_for_metric(value_column),
                "window_days": window_days,
                "end_date": end_date,
                "latest_value": sliced.iloc[-1]["value"],
                "starting_value": sliced.iloc[0]["value"],
                "average_value": average_numeric(sliced["value"].tolist()),
                "series": sliced.to_dict(orient="records"),
            }
        )

    def answer_chat(
        self,
        message: str,
        *,
        user_id: str,
        conversation_id: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        normalized = message.strip()
        if not normalized:
            raise ValueError("Message cannot be empty.")

        conversation = (
            self.conversation_store.get_conversation(conversation_id, user_id)
            if conversation_id
            else None
        )
        if conversation is None:
            conversation = self.conversation_store.create_conversation(user_id, title_hint=normalized)

        history = list(conversation.messages[-12:])
        requested_date = extract_requested_date(normalized) or date or self._infer_follow_up_date(history)
        thread_state = summarize_thread_state(history)
        conversation_context = self._conversation_context(history, thread_state)
        if self.chat_mode == "agent":
            result = self._execute_agentic_turn(
                normalized,
                requested_date,
                history,
                conversation_context,
                thread_state,
            )
        else:
            orchestration = self._orchestrate_chat(
                normalized,
                requested_date,
                history,
                conversation_context,
                thread_state,
            )
            result = self._execute_orchestration(
                normalized,
                requested_date,
                orchestration,
                history,
                conversation_context,
            )

        conversation.messages.append(self._make_message_record(role="user", text=normalized))
        conversation.messages.append(
            self._make_message_record(
                role="assistant",
                text=str(result.get("answer") or ""),
                metadata=self._metadata_for_persistence(
                    {
                        "provenance": result.get("provenance"),
                        "as_of_date": result.get("as_of_date"),
                        "data": result.get("data"),
                        "tool_trace": result.get("tool_trace") or [],
                        "sources": result.get("sources") or [],
                        "response_mode": result.get("response_mode"),
                        "soft_timeout": result.get("soft_timeout"),
                        "soft_timeout_reason": result.get("soft_timeout_reason"),
                        "completed_tools": result.get("completed_tools") or [],
                        "elapsed_ms": result.get("elapsed_ms"),
                        "soft_deadline_seconds": result.get("soft_deadline_seconds"),
                        "prompt_version": PROMPT_VERSION,
                    }
                ),
            )
        )
        if conversation.title == "New chat":
            conversation.title = shorten_title(normalized)
        self.conversation_store.save_conversation(conversation)

        logger.info(
            "chat_turn user=%s conversation=%s provenance=%s tools=%s",
            user_id,
            conversation.conversation_id,
            result.get("provenance"),
            [t.get("tool") for t in (result.get("tool_trace") or [])],
        )

        return {
            **result,
            "conversation_id": conversation.conversation_id,
            "conversation_title": conversation.title,
            "prompt_version": PROMPT_VERSION,
        }

    def stream_chat_turn(
        self,
        message: str,
        *,
        user_id: str,
        conversation_id: str | None = None,
        date: str | None = None,
    ) -> Iterator[tuple[str, Any]]:
        """
        Yields ("delta", str) text chunks from Bedrock streaming, then ("done", metadata_dict)
        where metadata matches ChatResponse fields except answer (client concatenates deltas).
        """
        normalized = message.strip()
        if not normalized:
            raise ValueError("Message cannot be empty.")

        conversation = (
            self.conversation_store.get_conversation(conversation_id, user_id)
            if conversation_id
            else None
        )
        if conversation is None:
            conversation = self.conversation_store.create_conversation(user_id, title_hint=normalized)

        history = list(conversation.messages[-12:])
        requested_date = extract_requested_date(normalized) or date or self._infer_follow_up_date(history)
        thread_state = summarize_thread_state(history)
        conversation_context = self._conversation_context(history, thread_state)
        started_at = time.monotonic()
        soft_deadline_at = (
            started_at + self.soft_deadline_seconds
            if self.soft_deadline_seconds > 0
            else None
        )
        last_heartbeat_at = started_at
        progress_events: list[dict[str, Any]] = []
        progress_lock = threading.Lock()
        progress_cursor = 0
        last_progress_message = "Working..."

        def elapsed_ms() -> int:
            return int((time.monotonic() - started_at) * 1000)

        def progress_payload(stage: str, status: str, **extra: Any) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "stage": stage,
                "status": status,
                "elapsed_ms": elapsed_ms(),
            }
            payload.update(extra)
            return payload

        def drain_progress_events() -> Iterator[tuple[str, Any]]:
            nonlocal progress_cursor
            while True:
                with progress_lock:
                    if progress_cursor >= len(progress_events):
                        break
                    payload = progress_events[progress_cursor]
                    progress_cursor += 1
                yield ("progress", payload)

        def maybe_heartbeat(stage: str, *, force: bool = False) -> tuple[str, Any] | None:
            nonlocal last_heartbeat_at, last_progress_message
            now = time.monotonic()
            if not force and (now - last_heartbeat_at) < self.heartbeat_seconds:
                return None
            last_heartbeat_at = now
            return (
                "heartbeat",
                {
                    "stage": stage,
                    "elapsed_ms": elapsed_ms(),
                    "message": last_progress_message,
                },
            )

        result: dict[str, Any]
        if self.chat_mode == "agent":
            yield (
                "progress",
                progress_payload(
                    "agentic_turn",
                    "started",
                    message="Running analysis agent...",
                ),
            )
            last_progress_message = "Running analysis agent..."
            forced_heartbeat = maybe_heartbeat("agentic_turn", force=True)
            if forced_heartbeat:
                yield forced_heartbeat
            with ThreadPoolExecutor(max_workers=1) as executor:
                def on_agent_progress(stage: str, payload: dict[str, Any]) -> None:
                    event = {"stage": stage, "status": "update", "elapsed_ms": elapsed_ms()}
                    event.update(payload)
                    with progress_lock:
                        progress_events.append(event)

                future = executor.submit(
                    self._execute_agentic_turn,
                    normalized,
                    requested_date,
                    history,
                    conversation_context,
                    thread_state,
                    on_agent_progress,
                )
                while not future.done():
                    for _, progress_payload_event in drain_progress_events():
                        message = progress_payload_event.get("message")
                        if isinstance(message, str) and message.strip():
                            last_progress_message = message.strip()
                        yield ("progress", progress_payload_event)
                    heartbeat = maybe_heartbeat("agentic_turn")
                    if heartbeat:
                        yield heartbeat
                    if soft_deadline_at and time.monotonic() >= soft_deadline_at:
                        future.cancel()
                        result = self._build_soft_timeout_result(
                            message=normalized,
                            executions=[],
                            tool_trace=[],
                            reasoning="Soft deadline reached during agentic turn.",
                        )
                        break
                    time.sleep(0.1)
                else:
                    result = future.result()
                for _, progress_payload_event in drain_progress_events():
                    message = progress_payload_event.get("message")
                    if isinstance(message, str) and message.strip():
                        last_progress_message = message.strip()
                    yield ("progress", progress_payload_event)
            yield (
                "progress",
                progress_payload(
                    "agentic_turn",
                    "completed",
                    message="Analysis complete.",
                ),
            )
        else:
            yield (
                "progress",
                progress_payload("planning", "started", message="Planning response..."),
            )
            last_progress_message = "Planning response..."
            forced_heartbeat = maybe_heartbeat("planning", force=True)
            if forced_heartbeat:
                yield forced_heartbeat
            with ThreadPoolExecutor(max_workers=1) as executor:
                plan_future = executor.submit(
                    self._orchestrate_chat,
                    normalized,
                    requested_date,
                    history,
                    conversation_context,
                    thread_state,
                )
                while not plan_future.done():
                    heartbeat = maybe_heartbeat("planning")
                    if heartbeat:
                        yield heartbeat
                    if soft_deadline_at and time.monotonic() >= soft_deadline_at:
                        plan_future.cancel()
                        result = self._build_soft_timeout_result(
                            message=normalized,
                            executions=[],
                            tool_trace=[],
                            reasoning="Soft deadline reached during planning.",
                        )
                        break
                    time.sleep(0.1)
                else:
                    orchestration = plan_future.result()
                    yield (
                        "progress",
                        progress_payload("planning", "completed", message="Plan ready."),
                    )
                    yield (
                        "progress",
                        progress_payload("executing", "started", message="Executing analysis..."),
                    )
                    last_progress_message = "Executing analysis..."
                    forced_heartbeat = maybe_heartbeat("executing", force=True)
                    if forced_heartbeat:
                        yield forced_heartbeat

                    def on_progress(stage: str, payload: dict[str, Any]) -> None:
                        event = {"stage": stage, "status": "update", "elapsed_ms": elapsed_ms()}
                        event.update(payload)
                        with progress_lock:
                            progress_events.append(event)

                    execution_future = executor.submit(
                        self._execute_orchestration,
                        normalized,
                        requested_date,
                        orchestration,
                        history,
                        conversation_context,
                        soft_deadline_at=soft_deadline_at,
                        partial_min_tools=self.partial_min_tools,
                        progress_callback=on_progress,
                    )
                    while not execution_future.done():
                        for _, progress_payload_event in drain_progress_events():
                            message = progress_payload_event.get("message")
                            if isinstance(message, str) and message.strip():
                                last_progress_message = message.strip()
                            yield ("progress", progress_payload_event)
                        heartbeat = maybe_heartbeat("executing")
                        if heartbeat:
                            yield heartbeat
                        if soft_deadline_at and time.monotonic() >= soft_deadline_at:
                            execution_future.cancel()
                            result = self._build_soft_timeout_result(
                                message=normalized,
                                executions=[],
                                tool_trace=[],
                                reasoning="Soft deadline reached before orchestration completed.",
                            )
                            break
                        time.sleep(0.1)
                    else:
                        result = execution_future.result()
                    for _, progress_payload_event in drain_progress_events():
                        message = progress_payload_event.get("message")
                        if isinstance(message, str) and message.strip():
                            last_progress_message = message.strip()
                        yield ("progress", progress_payload_event)
                    yield (
                        "progress",
                        progress_payload("executing", "completed", message="Analysis complete."),
                    )

        conversation.messages.append(self._make_message_record(role="user", text=normalized))
        conversation.messages.append(
            self._make_message_record(
                role="assistant",
                text=str(result.get("answer") or ""),
                metadata=self._metadata_for_persistence(
                    {
                        "provenance": result.get("provenance"),
                        "as_of_date": result.get("as_of_date"),
                        "data": result.get("data"),
                        "tool_trace": result.get("tool_trace") or [],
                        "sources": result.get("sources") or [],
                        "response_mode": result.get("response_mode"),
                        "soft_timeout": result.get("soft_timeout"),
                        "soft_timeout_reason": result.get("soft_timeout_reason"),
                        "completed_tools": result.get("completed_tools") or [],
                        "elapsed_ms": elapsed_ms(),
                        "soft_deadline_seconds": result.get("soft_deadline_seconds"),
                        "prompt_version": PROMPT_VERSION,
                    }
                ),
            )
        )
        if conversation.title == "New chat":
            conversation.title = shorten_title(normalized)
        self.conversation_store.save_conversation(conversation)

        logger.info(
            "chat_turn_stream user=%s conversation=%s provenance=%s tools=%s",
            user_id,
            conversation.conversation_id,
            result.get("provenance"),
            [t.get("tool") for t in (result.get("tool_trace") or [])],
        )

        if result.get("answer"):
            yield ("delta", str(result.get("answer")))

        meta = {
            key: value
            for key, value in {
                **result,
                "elapsed_ms": elapsed_ms(),
                "conversation_id": conversation.conversation_id,
                "conversation_title": conversation.title,
                "prompt_version": PROMPT_VERSION,
            }.items()
            if key != "answer"
        }
        yield ("done", meta)

    def _run_tool_plan_steps(
        self,
        steps: list[dict[str, Any]],
        message: str,
        requested_date: str | None,
        confidence: str,
        reasoning: str,
        *,
        soft_deadline_at: float | None = None,
        min_partial_results: int = 1,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, int]:
        indexed: list[tuple[str, dict[str, Any]]] = []
        for step in steps[:5]:
            tool_name = str(step.get("tool") or "").strip()
            if not tool_name:
                continue
            indexed.append((tool_name, step.get("args") or {}))
        if not indexed:
            return [], [], False, 0

        workers = min(5, len(indexed))
        completed: dict[int, dict[str, Any]] = {}
        soft_timed_out = False
        required_for_partial = max(1, int(min_partial_results))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_index = {
                executor.submit(self._execute_tool_step, message, requested_date, tn, args): idx
                for idx, (tn, args) in enumerate(indexed)
            }
            pending = set(future_to_index.keys())
            while pending:
                done, pending = wait(
                    pending,
                    timeout=max(0.1, self.heartbeat_seconds / 2.0),
                    return_when=FIRST_COMPLETED,
                )
                for done_future in done:
                    idx = future_to_index[done_future]
                    completed[idx] = done_future.result()
                if soft_deadline_at and time.monotonic() >= soft_deadline_at:
                    completed_count = len(completed)
                    if completed_count == 0 or completed_count >= required_for_partial:
                        soft_timed_out = True
                        break
            if soft_timed_out:
                for pending_future in pending:
                    pending_future.cancel()

        ordered_indexes = sorted(completed.keys())
        executions = [completed[idx] for idx in ordered_indexes]
        tool_trace: list[dict[str, Any]] = []
        for idx, execution in zip(ordered_indexes, executions, strict=True):
            tool_name, default_args = indexed[idx]
            args_out = execution.get("args") if isinstance(execution.get("args"), dict) else default_args
            tool_trace.append(
                {
                    "tool": tool_name,
                    "args": _sanitize(args_out),
                    "confidence": confidence,
                    "reasoning": reasoning,
                }
            )
        return executions, tool_trace, soft_timed_out, len(indexed)

    _STORE_DATA_MAX_JSON_CHARS = 100_000

    @classmethod
    def _metadata_for_persistence(cls, metadata: dict[str, Any]) -> dict[str, Any]:
        """Avoid DynamoDB item size failures: drop oversized tool payloads from stored threads."""
        data = metadata.get("data")
        if data is None:
            return _sanitize(metadata)
        try:
            raw = json.dumps(data, default=str)
        except (TypeError, ValueError):
            return _sanitize(metadata)
        if len(raw) <= cls._STORE_DATA_MAX_JSON_CHARS:
            return _sanitize(metadata)
        trimmed = dict(metadata)
        trimmed["data"] = {
            "_storage_truncated": True,
            "_approx_json_chars": len(raw),
        }
        return _sanitize(trimmed)

    def _always_on_context_for_prompt(self) -> str:
        try:
            context_bundle = self.retrieval_service.get_context_bundle()
        except Exception as exc:
            logger.warning("Failed loading always-on context bundle: %s", exc)
            return "(none)"
        return context_bundle or "(none)"

    def _orchestrate_chat(
        self,
        message: str,
        requested_date: str | None,
        history: list[dict[str, Any]],
        conversation_context: str,
        thread_state: dict[str, Any],
    ) -> dict[str, Any]:
        if looks_like_greeting(message) and not history:
            return heuristic_chat_orchestration(message, requested_date, None)

        latest_date = self.latest_date()
        always_on_context = self._always_on_context_for_prompt()
        prompt = (
            f"{CHAT_ORCHESTRATION_SYSTEM_PROMPT}\n\n{CHAT_ORCHESTRATION_SCHEMA}\n\n"
            f"Always-on context:\n{always_on_context}\n\n"
            f"Latest available date: {latest_date}\n"
            f"Requested date from user, if already parsed: {requested_date or 'none'}\n"
            f"Suggested default window for trend questions: {DEFAULT_TOOL_WINDOW_DAYS} days.\n"
            f"Prompt version: {PROMPT_VERSION}"
        )
        user_prompt = (
            f"Conversation context:\n{conversation_context or '(none)'}\n\n"
            f"Analytical thread:\n{summarize_thread_for_prompt(thread_state) or '(none)'}\n\n"
            f"Recent turns:\n{self._trim_history_for_prompt(history) or '(none)'}\n\n"
            f"User question:\n{message}"
        )
        try:
            raw_plan = self.bedrock_client.chat(
                prompt,
                user_prompt,
                max_tokens=PLANNER_MAX_TOKENS,
                temperature=PLANNER_TEMPERATURE,
            )
            parsed = parse_json_object(raw_plan)
            if parsed:
                return normalize_orchestration_plan(parsed, message, requested_date, thread_state)
        except Exception as exc:
            logger.warning("Planner failed; falling back to heuristics: %s", exc)
        return heuristic_chat_orchestration(
            message,
            requested_date,
            self._heuristic_plan(message, requested_date, thread_state),
        )

    def _execute_orchestration(
        self,
        message: str,
        requested_date: str | None,
        orchestration: dict[str, Any],
        history: list[dict[str, Any]],
        conversation_context: str,
        *,
        soft_deadline_at: float | None = None,
        partial_min_tools: int | None = None,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        def progress(stage: str, **payload: Any) -> None:
            if callable(progress_callback):
                progress_callback(stage, payload)

        min_tools = max(1, int(partial_min_tools or self.partial_min_tools))
        mode = str(orchestration.get("mode") or "tool_plan")
        confidence = str(orchestration.get("confidence") or "medium")
        reasoning = str(orchestration.get("reasoning") or "")
        use_retrieval = bool(orchestration.get("use_retrieval"))
        retrieval_query = self._build_retrieval_query(
            message,
            history,
            str(orchestration.get("retrieval_query") or message).strip(),
        )
        retrieval_types = normalize_source_types(orchestration.get("retrieval_source_types"))
        steps = orchestration.get("steps") or []

        if mode == "direct_answer":
            progress("writing_answer", mode=mode)
            if soft_deadline_at and time.monotonic() >= soft_deadline_at:
                return self._build_soft_timeout_result(
                    message=message,
                    executions=[],
                    tool_trace=[],
                    reasoning="Soft deadline reached before drafting a direct response.",
                )
            return self._build_direct_response(
                message=message,
                assistant_response=str(orchestration.get("assistant_response") or "").strip(),
                use_retrieval=use_retrieval,
                retrieval_query=retrieval_query,
                retrieval_source_types=retrieval_types,
                confidence=confidence,
                reasoning=reasoning,
                history=history,
                conversation_context=conversation_context,
            )

        if len(steps) == 1 and str(steps[0].get("tool") or "") == "answer_general_question":
            progress("writing_answer", mode=mode)
            if soft_deadline_at and time.monotonic() >= soft_deadline_at:
                return self._build_soft_timeout_result(
                    message=message,
                    executions=[],
                    tool_trace=[],
                    reasoning="Soft deadline reached before composing a general answer.",
                )
            return self._build_direct_response(
                message=message,
                assistant_response=str(orchestration.get("assistant_response") or "").strip(),
                use_retrieval=use_retrieval,
                retrieval_query=retrieval_query,
                retrieval_source_types=retrieval_types,
                confidence=confidence,
                reasoning=reasoning or "The model determined this was better answered conversationally.",
                history=history,
                conversation_context=conversation_context,
            )

        progress("running_tools", planned_steps=len(steps))
        executions, tool_trace, soft_timed_out, planned_count = self._run_tool_plan_steps(
            steps,
            message,
            requested_date,
            confidence,
            reasoning,
            soft_deadline_at=soft_deadline_at,
            min_partial_results=min_tools,
        )
        progress(
            "running_tools_complete",
            completed_steps=len(executions),
            planned_steps=planned_count,
            soft_timed_out=soft_timed_out,
        )

        if soft_timed_out:
            return self._build_soft_timeout_result(
                message=message,
                executions=executions,
                tool_trace=tool_trace,
                reasoning=(
                    "Soft deadline reached while running tool plan."
                    if executions
                    else "Soft deadline reached before any tool results completed."
                ),
            )

        if not executions:
            return self._build_direct_response(
                message=message,
                assistant_response="",
                use_retrieval=use_retrieval,
                retrieval_query=retrieval_query,
                retrieval_source_types=retrieval_types,
                confidence="low",
                reasoning=reasoning or "No reliable tool plan was produced, so I answered conversationally.",
                history=history,
                conversation_context=conversation_context,
            )

        if any(item["tool"] == "get_positioning_analysis" for item in executions) and not retrieval_types:
            retrieval_types = ["rules_of_thumb", "interpretation_guidance", "commentary_guidance"]
            use_retrieval = True

        if soft_deadline_at and time.monotonic() >= soft_deadline_at:
            return self._build_soft_timeout_result(
                message=message,
                executions=executions,
                tool_trace=tool_trace,
                reasoning="Soft deadline reached before synthesis stage.",
            )

        sources = self.retrieval_service.retrieve(
            retrieval_query,
            top_k=DEFAULT_RETRIEVAL_TOP_K,
            source_types=retrieval_types,
        ) if use_retrieval else []
        fallback = self._multi_tool_fallback(executions)
        if soft_deadline_at and time.monotonic() >= soft_deadline_at:
            return self._build_soft_timeout_result(
                message=message,
                executions=executions,
                tool_trace=tool_trace,
                sources=sources,
                reasoning="Soft deadline reached before final writer call.",
            )
        progress("writing_answer", mode="tool_plan")
        answer = self._answer_from_tool_results(
            message,
            executions,
            fallback,
            sources,
            history=history,
            conversation_context=conversation_context,
        )
        return {
            "answer": answer,
            "provenance": "data_tools_with_guidance" if sources else "data_tools",
            "as_of_date": next((item["as_of_date"] for item in reversed(executions) if item["as_of_date"]), None),
            "data": executions[0]["result"] if len(executions) == 1 else {
                "tool_results": [
                    {"tool": item["tool"], "result": item["result"]}
                    for item in executions
                ]
            },
            "tool_trace": tool_trace,
            "sources": sources,
        }

    def _build_direct_response(
        self,
        *,
        message: str,
        assistant_response: str,
        use_retrieval: bool,
        retrieval_query: str,
        retrieval_source_types: list[str],
        confidence: str,
        reasoning: str,
        history: list[dict[str, Any]],
        conversation_context: str,
    ) -> dict[str, Any]:
        sources = self.retrieval_service.retrieve(
            retrieval_query,
            top_k=DEFAULT_RETRIEVAL_TOP_K,
            source_types=retrieval_source_types,
        ) if use_retrieval else []
        fallback = assistant_response or conversational_fallback(message)
        system_prompt = DIRECT_ANSWER_SYSTEM_PROMPT.format(house_style=HOUSE_STYLE_GUIDANCE)
        always_on_context = self._always_on_context_for_prompt()
        writer_prompt = (
            f"Always-on context:\n{always_on_context}\n\n"
            f"Conversation context:\n{conversation_context or '(none)'}\n\n"
            f"Recent turns:\n{self._trim_history_for_prompt(history) or '(none)'}\n\n"
            f"User question:\n{message}\n\n"
            f"Draft assistant response:\n{assistant_response or '(none)'}\n\n"
            f"Retrieved guidance:\n{json.dumps(sources, indent=2)}"
        )
        if sources:
            answer = self._invoke_bedrock_with_fallback(
                system_prompt,
                writer_prompt,
                fallback=fallback,
                max_tokens=DIRECT_WRITER_MAX_TOKENS,
                temperature=DIRECT_WRITER_TEMPERATURE,
            )
        else:
            answer = assistant_response or self._invoke_bedrock_with_fallback(
                system_prompt,
                writer_prompt,
                fallback=fallback,
                max_tokens=DIRECT_WRITER_MAX_TOKENS,
                temperature=DIRECT_WRITER_TEMPERATURE,
            )
        return {
            "answer": answer,
            "provenance": "general_interpretation",
            "as_of_date": None,
            "data": None,
            "tool_trace": [
                {
                    "tool": "direct_answer",
                    "args": {},
                    "confidence": confidence,
                    "reasoning": reasoning,
                }
            ],
            "sources": sources,
        }

    @staticmethod
    def _make_message_record(
        *,
        role: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "id": str(uuid.uuid4()),
            "role": role,
            "text": text,
            "created_at": datetime.now(UTC).isoformat(),
        }
        if metadata:
            payload.update(_sanitize(metadata))
        return payload

    @staticmethod
    def _trim_history_for_prompt(history: list[dict[str, Any]], limit: int = 10) -> str:
        snippets = []
        for item in history[-limit:]:
            role = str(item.get("role") or "user").capitalize()
            text = " ".join(str(item.get("text") or "").split())
            if not text:
                continue
            snippets.append(f"{role}: {text}")
        return "\n".join(snippets)

    @staticmethod
    def _conversation_context(
        history: list[dict[str, Any]],
        thread_state: dict[str, Any] | None = None,
    ) -> str:
        derived = thread_state or summarize_thread_state(history)
        context_bits = []
        latest_as_of = derived.get("latest_as_of_date")
        if latest_as_of:
            context_bits.append(f"Last referenced dataset date: {latest_as_of}")
        latest_provenance = derived.get("latest_provenance")
        if latest_provenance:
            context_bits.append(f"Last answer provenance: {latest_provenance}")
        last_step = latest_tool_step(derived)
        if last_step:
            context_bits.append(f"Last analytic tool: {last_step['tool']}")
        return "\n".join(context_bits)

    @staticmethod
    def _infer_follow_up_date(history: list[dict[str, Any]]) -> str | None:
        for item in reversed(history):
            as_of_date = item.get("as_of_date")
            if as_of_date:
                return str(as_of_date)
        return None

    @staticmethod
    def _build_retrieval_query(
        message: str,
        history: list[dict[str, Any]],
        raw_query: str,
    ) -> str:
        previous_user = next(
            (
                str(item.get("text") or "")
                for item in reversed(history)
                if item.get("role") == "user" and item.get("text")
            ),
            "",
        )
        if previous_user and previous_user.lower() not in raw_query.lower():
            return f"{previous_user}\n{raw_query}".strip()
        return raw_query or message

    def _execute_agentic_turn(
        self,
        message: str,
        requested_date: str | None,
        history: list[dict[str, Any]],
        conversation_context: str,
        thread_state: dict[str, Any],
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        def progress(stage: str, **payload: Any) -> None:
            if callable(progress_callback):
                progress_callback(stage, payload)

        agentic = self._run_agentic_loop(
            message=message,
            requested_date=requested_date,
            history=history,
            conversation_context=conversation_context,
            thread_state=thread_state,
            progress_callback=progress_callback,
        )
        if not agentic["executions"]:
            progress("writing_answer", message="Writing answer...")
            return self._build_direct_response(
                message=message,
                assistant_response=agentic["assistant_response"],
                use_retrieval=agentic["use_retrieval"],
                retrieval_query=self._build_retrieval_query(
                    message,
                    history,
                    agentic["retrieval_query"],
                ),
                retrieval_source_types=agentic["retrieval_source_types"],
                confidence=agentic["confidence"],
                reasoning=agentic["reasoning"] or "Agentic mode chose to answer without tools.",
                history=history,
                conversation_context=conversation_context,
            )

        retrieval_types = list(agentic["retrieval_source_types"])
        use_retrieval = bool(agentic["use_retrieval"])
        if any(item["tool"] == "get_positioning_analysis" for item in agentic["executions"]) and not retrieval_types:
            retrieval_types = ["rules_of_thumb", "interpretation_guidance", "commentary_guidance"]
            use_retrieval = True
        retrieval_query = self._build_retrieval_query(
            message,
            history,
            agentic["retrieval_query"],
        )
        progress("retrieval", message="Gathering guidance...")
        sources = self.retrieval_service.retrieve(
            retrieval_query,
            top_k=DEFAULT_RETRIEVAL_TOP_K,
            source_types=retrieval_types,
        ) if use_retrieval else []
        fallback = agentic["assistant_response"] or self._multi_tool_fallback(agentic["executions"])
        progress("writing_answer", message="Writing answer...")
        answer = self._answer_from_tool_results(
            message,
            agentic["executions"],
            fallback,
            sources,
            history=history,
            conversation_context=conversation_context,
        )
        return {
            "answer": answer,
            "provenance": "data_tools_with_guidance" if sources else "data_tools",
            "as_of_date": next(
                (item["as_of_date"] for item in reversed(agentic["executions"]) if item["as_of_date"]),
                None,
            ),
            "data": agentic["executions"][0]["result"] if len(agentic["executions"]) == 1 else {
                "tool_results": [
                    {"tool": item["tool"], "result": item["result"]}
                    for item in agentic["executions"]
                ]
            },
            "tool_trace": agentic["tool_trace"],
            "sources": sources,
        }

    def _run_agentic_loop(
        self,
        *,
        message: str,
        requested_date: str | None,
        history: list[dict[str, Any]],
        conversation_context: str,
        thread_state: dict[str, Any],
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        def progress(stage: str, **payload: Any) -> None:
            if callable(progress_callback):
                progress_callback(stage, payload)

        executions: list[dict[str, Any]] = []
        tool_trace: list[dict[str, Any]] = []
        retrieval_query = message
        retrieval_source_types: list[str] = []
        use_retrieval = False
        assistant_response = ""
        confidence = "medium"
        reasoning = "Agentic mode gathered evidence iteratively."
        seen_steps: set[tuple[str, str]] = set()

        always_on_context = self._always_on_context_for_prompt()
        for step_idx in range(self.agentic_max_steps):
            progress(
                "agent_planning",
                message=f"Planning next step ({step_idx + 1} of {self.agentic_max_steps})...",
            )
            loop_prompt = (
                f"{AGENTIC_TOOL_LOOP_SYSTEM_PROMPT}\n\n"
                f"{DATASET_CONTEXT_PROMPT}\n\n"
                f"Always-on context:\n{always_on_context}\n\n"
                f"{AGENTIC_TOOL_LOOP_SCHEMA}\n\n"
                f"Latest available date: {self.latest_date()}\n"
                f"Requested date from user, if already parsed: {requested_date or 'none'}\n"
                f"Current loop step: {step_idx + 1} of {self.agentic_max_steps}\n"
                f"Prompt version: {PROMPT_VERSION}"
            )
            prior_results = [self._summarize_execution_for_writer(item) for item in executions]
            user_prompt = (
                f"Conversation context:\n{conversation_context or '(none)'}\n\n"
                f"Analytical thread:\n{summarize_thread_for_prompt(thread_state) or '(none)'}\n\n"
                f"Recent turns:\n{self._trim_history_for_prompt(history) or '(none)'}\n\n"
                f"User question:\n{message}\n\n"
                f"Prior tool results in this turn:\n{json.dumps(prior_results, indent=2)}"
            )
            try:
                raw_plan = self.bedrock_client.chat(
                    loop_prompt,
                    user_prompt,
                    max_tokens=PLANNER_MAX_TOKENS,
                    temperature=PLANNER_TEMPERATURE,
                )
            except Exception as exc:
                logger.warning("Agentic loop failed; falling back to planner mode: %s", exc)
                break
            parsed = parse_json_object(raw_plan)
            if not parsed:
                logger.warning("Agentic loop produced invalid JSON; falling back to current evidence.")
                break

            confidence = str(parsed.get("confidence") or confidence)
            reasoning = str(parsed.get("reasoning") or reasoning)
            retrieval_query = str(parsed.get("retrieval_query") or retrieval_query).strip() or message
            retrieval_source_types = normalize_source_types(
                parsed.get("retrieval_source_types")
            ) or retrieval_source_types
            use_retrieval = bool(parsed.get("use_retrieval")) or use_retrieval

            if str(parsed.get("action") or "").strip().lower() == "final":
                progress("agent_finalizing", message="Finalizing response from collected evidence...")
                assistant_response = str(parsed.get("assistant_response") or "").strip()
                break

            tool_name = str(parsed.get("tool") or "").strip()
            if not tool_name:
                break
            progress("agent_tool", message=f"Running {tool_name}...")
            normalized_plan = normalize_tool_plan(
                {
                    "tool": tool_name,
                    "args": parsed.get("args") or {},
                    "use_retrieval": use_retrieval,
                    "retrieval_query": retrieval_query,
                    "retrieval_source_types": retrieval_source_types,
                    "confidence": confidence,
                    "reasoning": reasoning,
                },
                message,
                requested_date,
                thread_state,
            )
            cache_key = (
                normalized_plan["tool"],
                json.dumps(normalized_plan["args"], sort_keys=True, default=str),
            )
            if cache_key in seen_steps:
                logger.info("Agentic loop stopped on duplicate tool request: %s", normalized_plan["tool"])
                break
            seen_steps.add(cache_key)
            execution = self._execute_tool_step(
                message,
                requested_date,
                normalized_plan["tool"],
                normalized_plan["args"],
            )
            progress("agent_tool_complete", message=f"Completed {normalized_plan['tool']}.")
            executions.append(execution)
            tool_trace.append(
                {
                    "tool": normalized_plan["tool"],
                    "args": _sanitize(execution.get("args") or normalized_plan["args"]),
                    "confidence": confidence,
                    "reasoning": reasoning,
                }
            )

        return {
            "executions": executions,
            "tool_trace": tool_trace,
            "use_retrieval": use_retrieval,
            "retrieval_query": retrieval_query,
            "retrieval_source_types": retrieval_source_types,
            "assistant_response": assistant_response,
            "confidence": confidence,
            "reasoning": reasoning,
        }

    def _execute_tool_step(
        self,
        message: str,
        requested_date: str | None,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name == "get_available_dates":
            limit = int(args.get("limit") or 30)
            dates = self.available_dates()
            return {
                "tool": tool_name,
                "args": {"limit": limit},
                "result": {"available_dates": dates[-limit:]},
                "fallback": (
                    f"I have {len(dates)} dates available, ranging from {dates[0]} to {dates[-1]}."
                    if dates
                    else "No historical dates are available."
                ),
                "as_of_date": dates[-1] if dates else None,
            }

        if tool_name == "get_metric_series":
            metric = normalize_metric_name(args.get("metric")) or infer_metric(message) or "options_volume"
            end_date = args.get("end_date") or requested_date
            window_days = normalize_window_days(args.get("window_days")) or infer_window_days(message)
            result = self.query_metric_series(metric=metric, window_days=window_days, end_date=end_date)
            label = str(result.get("metric_label") or "") or display_name_for_metric(metric)
            return {
                "tool": tool_name,
                "args": {"metric": metric, "window_days": window_days, "end_date": result["end_date"]},
                "result": result,
                "fallback": sentence_for_metric_series(
                    metric=metric,
                    metric_label=label,
                    latest_value=result["latest_value"],
                    end_date=str(result["end_date"]),
                    window_days=int(result["window_days"]),
                    latest_observed_value=result.get("latest_observed_value"),
                    latest_observed_date=result.get("latest_observed_date"),
                    non_null_count=int(result.get("non_null_count") or 0),
                ),
                "as_of_date": result["end_date"],
            }

        if tool_name == "get_market_snapshot":
            snapshot = self.build_snapshot(date=args.get("date") or requested_date)
            return {
                "tool": tool_name,
                "args": {"date": snapshot["date"]},
                "result": snapshot,
                "fallback": self._snapshot_fallback(snapshot),
                "as_of_date": snapshot["date"],
            }

        if tool_name == "get_contract_activity":
            market = normalize_market_name(args.get("market") or infer_market(message))
            metric = normalize_contract_metric(args.get("metric"), market) or "volume"
            result = self.get_contract_activity(
                contract_month=args.get("contract_month"),
                market=market,
                metric=metric,
                window_days=normalize_window_days(args.get("window_days")) or infer_window_days(message),
                end_date=args.get("end_date") or requested_date,
                date=args.get("date"),
            )
            return {
                "tool": tool_name,
                "args": {
                    "contract_month": args.get("contract_month"),
                    "market": market,
                    "metric": metric,
                    "window_days": result.get("window_days"),
                    "date": result.get("date"),
                    "end_date": result.get("end_date"),
                },
                "result": result,
                "fallback": contract_activity_fallback(result),
                "as_of_date": result.get("date") or result.get("end_date"),
            }

        if tool_name == "get_iv_analysis":
            analysis_type = normalize_iv_analysis_type(args.get("analysis_type"))
            result = self.get_iv_analysis(
                analysis_type=analysis_type,
                window_days=normalize_window_days(args.get("window_days")) or infer_window_days(message),
                end_date=args.get("end_date") or requested_date,
                date=args.get("date"),
                contract_month=args.get("contract_month"),
            )
            return {
                "tool": tool_name,
                "args": {
                    "analysis_type": analysis_type,
                    "window_days": result.get("window_days"),
                    "date": result.get("date"),
                    "end_date": result.get("end_date"),
                    "contract_month": args.get("contract_month"),
                },
                "result": result,
                "fallback": iv_analysis_fallback(result),
                "as_of_date": result.get("date") or result.get("end_date"),
            }

        if tool_name == "get_positioning_analysis":
            analysis_type = normalize_positioning_analysis_type(args.get("analysis_type"))
            result = self.get_positioning_analysis(
                analysis_type=analysis_type,
                date=args.get("date") or requested_date,
                lookback_days=normalize_window_days(args.get("lookback_days")) or infer_window_days(message),
            )
            return {
                "tool": tool_name,
                "args": {
                    "analysis_type": analysis_type,
                    "lookback_days": normalize_window_days(args.get("lookback_days")) or infer_window_days(message),
                    "date": result.get("date"),
                },
                "result": result,
                "fallback": positioning_fallback(result),
                "as_of_date": result.get("date"),
            }

        if tool_name == "compare_periods":
            result = self.compare_periods(
                comparison_type=normalize_comparison_type(args.get("comparison_type")),
                metric=args.get("metric"),
                primary_date=args.get("primary_date"),
                secondary_date=args.get("secondary_date"),
                end_date=args.get("end_date") or requested_date,
                window_days=normalize_window_days(args.get("window_days")) or infer_window_days(message),
                market=normalize_market_name(args.get("market") or infer_market(message)),
                contract_month=args.get("contract_month"),
            )
            return {
                "tool": tool_name,
                "args": _sanitize(args),
                "result": result,
                "fallback": comparison_fallback(result),
                "as_of_date": result.get("end_date") or result.get("secondary_date"),
            }

        if tool_name == "interpret_market_context":
            focus = normalize_interpretation_focus(args.get("focus"))
            result = self.interpret_market_context(
                focus=focus,
                date=args.get("date") or requested_date,
                window_days=normalize_window_days(args.get("window_days")) or infer_window_days(message),
            )
            return {
                "tool": tool_name,
                "args": {
                    "focus": focus,
                    "date": result.get("date"),
                    "window_days": result.get("window_days"),
                },
                "result": result,
                "fallback": result.get("headline") or "Interpretation context is available.",
                "as_of_date": result.get("date"),
            }

        logger.warning("Unknown or unsupported tool from planner: %s", tool_name)
        return {
            "tool": tool_name or "answer_general_question",
            "args": _sanitize(args),
            "result": {"message": "No structured dataset tool was used."},
            "fallback": "I can answer broader market questions, but this response is not grounded in the historical OptionsAI datasets.",
            "as_of_date": None,
        }

    def _answer_from_tool_results(
        self,
        message: str,
        executions: list[dict[str, Any]],
        fallback: str,
        guidance_sources: list[dict[str, Any]] | None = None,
        *,
        history: list[dict[str, Any]],
        conversation_context: str,
    ) -> str:
        always_on_context = self._always_on_context_for_prompt()
        prompt = (
            f"Always-on context:\n{always_on_context}\n\n"
            f"Conversation context:\n{conversation_context or '(none)'}\n\n"
            f"Recent turns:\n{self._trim_history_for_prompt(history) or '(none)'}\n\n"
            f"User question:\n{message}\n\n"
            f"Analytics summaries:\n{json.dumps([self._summarize_execution_for_writer(item) for item in executions], indent=2)}\n\n"
            f"Question-derived hints:\n{json.dumps(self._build_writer_hints(message, executions), indent=2)}\n\n"
            f"Retrieved guidance:\n{json.dumps(guidance_sources or [], indent=2)}"
        )
        return self._invoke_bedrock_with_fallback(
            TOOL_ANSWER_SYSTEM_PROMPT.format(house_style=HOUSE_STYLE_GUIDANCE),
            prompt,
            fallback=fallback,
            max_tokens=TOOL_WRITER_MAX_TOKENS,
            temperature=TOOL_WRITER_TEMPERATURE,
        )

    @staticmethod
    def _multi_tool_fallback(executions: list[dict[str, Any]]) -> str:
        if len(executions) == 1:
            return executions[0]["fallback"]
        tool_names = ", ".join(item["tool"] for item in executions)
        return f"I combined results from {tool_names} to answer the question."

    def _build_soft_timeout_result(
        self,
        *,
        message: str,
        executions: list[dict[str, Any]],
        tool_trace: list[dict[str, Any]],
        sources: list[dict[str, Any]] | None = None,
        reasoning: str,
    ) -> dict[str, Any]:
        completed_tools = [str(item.get("tool") or "") for item in executions if item.get("tool")]
        if executions:
            fallback = self._multi_tool_fallback(executions)
            answer = (
                f"{fallback} I hit the response-time soft limit, so this is a best-effort answer "
                "from completed tool results."
            )
            data: dict[str, Any] | None
            if len(executions) == 1:
                data = executions[0]["result"]
            else:
                data = {
                    "tool_results": [
                        {"tool": item["tool"], "result": item["result"]}
                        for item in executions
                    ]
                }
            provenance = "data_tools_with_guidance" if sources else "data_tools"
            as_of_date = next(
                (item.get("as_of_date") for item in reversed(executions) if item.get("as_of_date")),
                None,
            )
        else:
            answer = (
                "I hit the response-time soft limit before the data workflow completed, so I can only "
                "share a partial status right now. "
                f"{conversational_fallback(message)}"
            )
            data = None
            provenance = "general_interpretation"
            as_of_date = None
        return {
            "answer": answer,
            "provenance": provenance,
            "as_of_date": as_of_date,
            "data": data,
            "tool_trace": tool_trace,
            "sources": sources or [],
            "response_mode": "partial_soft_timeout",
            "soft_timeout": True,
            "soft_timeout_reason": reasoning,
            "completed_tools": completed_tools,
            "soft_deadline_seconds": self.soft_deadline_seconds,
        }

    @staticmethod
    def _summarize_execution_for_writer(execution: dict[str, Any]) -> dict[str, Any]:
        tool = execution.get("tool")
        result = execution.get("result") or {}
        summary: dict[str, Any] = {
            "tool": tool,
            "as_of_date": execution.get("as_of_date"),
            "headline": execution.get("fallback"),
        }

        if tool == "get_metric_series":
            series = result.get("series") or []
            summary["metric"] = result.get("metric")
            summary["metric_label"] = result.get("metric_label")
            summary["window_days"] = result.get("window_days")
            summary["latest_value"] = result.get("latest_value")
            summary["end_date_value"] = result.get("end_date_value")
            summary["starting_value"] = result.get("starting_value")
            summary["average_value"] = result.get("average_value")
            summary["non_null_count"] = result.get("non_null_count")
            summary["latest_observed_value"] = result.get("latest_observed_value")
            summary["latest_observed_date"] = result.get("latest_observed_date")
            summary["window_stats"] = result.get("window_stats")
            summary["end_date_value_missing"] = is_missing_value(result.get("latest_value"))
            summary["trend"] = series_direction(series)
            summary["point_count"] = len(series)
            summary["series"] = _series_for_writer(series, full_cap=90, tail=24)
            return _sanitize(summary)

        if tool == "get_market_snapshot":
            overview = ((result.get("metrics") or {}).get("overview")) or {}
            summary["overview"] = overview
            summary["market_context"] = result.get("market_context")
            summary["fact_pack"] = (result.get("fact_pack") or [])[:12]
            return _sanitize(summary)

        if tool == "get_contract_activity":
            summary["market"] = result.get("market")
            summary["market_label"] = result.get("market_label")
            summary["metric"] = result.get("metric")
            summary["metric_label"] = result.get("metric_label")
            summary["contract_month"] = result.get("contract_month")
            summary["top_contracts"] = (result.get("contracts") or [])[:10]
            summary["latest_value"] = result.get("latest_value")
            tail = 16
            s = result.get("series") or []
            summary["series"] = s[-tail:] if len(s) > tail else list(s)
            return _sanitize(summary)

        if tool == "get_iv_analysis":
            summary["analysis_type"] = result.get("analysis_type") or result.get("metric")
            summary["analysis_label"] = result.get("analysis_label")
            summary["metric_label"] = result.get("metric_label")
            summary["latest_value"] = result.get("latest_value")
            summary["end_date_value"] = result.get("end_date_value")
            summary["window_days"] = result.get("window_days")
            s = result.get("series") or []
            summary["non_null_count"] = result.get("non_null_count")
            summary["latest_observed_value"] = result.get("latest_observed_value")
            summary["latest_observed_date"] = result.get("latest_observed_date")
            summary["window_stats"] = result.get("window_stats")
            summary["end_date_iv_missing"] = bool(s) and is_missing_value(result.get("latest_value"))
            summary["series"] = _series_for_writer(s, full_cap=90, tail=28)
            summary["contracts"] = (result.get("contracts") or [])[:8]
            return _sanitize(summary)

        if tool == "get_positioning_analysis":
            payload = result.get("result") or {}
            summary["analysis_type"] = result.get("analysis_type")
            summary["analysis_label"] = result.get("analysis_label")
            summary["summary"] = payload.get("summary") or payload
            return _sanitize(summary)

        if tool == "compare_periods":
            summary["comparison"] = result
            summary["metric_label"] = result.get("metric_label")
            return _sanitize(summary)

        if tool == "interpret_market_context":
            return _sanitize(result)

        summary["result"] = result
        return _sanitize(summary)

    @staticmethod
    def _build_writer_hints(message: str, executions: list[dict[str, Any]]) -> dict[str, Any]:
        target_info = _extract_level_target(message)
        if not target_info:
            return {}

        target, tolerance = target_info
        for execution in executions:
            result = execution.get("result") or {}
            series = result.get("series") or []
            if not series:
                continue
            match = _last_date_near_target(series, target=target, tolerance=tolerance)
            if not match:
                continue
            label = (
                result.get("analysis_label")
                or result.get("metric_label")
                or result.get("analysis_type")
                or result.get("metric")
            )
            return _sanitize(
                {
                    "threshold_query": {
                        "label": label,
                        "target": target,
                        "tolerance": tolerance,
                        "last_match_date": match.get("date"),
                        "last_match_value": match.get("value"),
                    }
                }
            )
        return _sanitize(
            {
                "threshold_query": {
                    "target": target,
                    "tolerance": tolerance,
                    "last_match_date": None,
                    "last_match_value": None,
                }
            }
        )

    def _plan_tool_call(self, message: str, requested_date: str | None) -> dict[str, Any]:
        latest_date = self.latest_date()
        planner_prompt = (
            f"{DATASET_CONTEXT_PROMPT}\n\n{TOOL_SELECTION_SCHEMA}\n\n"
            f"Latest available date: {latest_date}\n"
            f"Requested date from user, if already parsed: {requested_date or 'none'}\n"
            f"Suggested default window if the user asks for a trend and does not specify one: {DEFAULT_TOOL_WINDOW_DAYS} days."
        )
        try:
            raw_plan = self.bedrock_client.chat(
                planner_prompt,
                f"User question:\n{message}",
                max_tokens=300,
                temperature=0.0,
            )
        except Exception:
            return self._heuristic_plan(message, requested_date)
        parsed = parse_json_object(raw_plan)
        if not parsed or "tool" not in parsed:
            return self._heuristic_plan(message, requested_date)
        return normalize_tool_plan(parsed, message, requested_date)

    def _execute_tool_plan(
        self,
        message: str,
        requested_date: str | None,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = str(plan.get("tool", "")).strip()
        args = plan.get("args") or {}
        retrieval_query = (plan.get("retrieval_query") or message).strip()
        retrieval_types = normalize_source_types(plan.get("retrieval_source_types"))
        use_retrieval = bool(plan.get("use_retrieval"))
        confidence = str(plan.get("confidence") or "medium")
        reasoning = str(plan.get("reasoning") or "")

        if tool_name == "get_available_dates":
            limit = int(args.get("limit") or 30)
            dates = self.available_dates()
            data = {"available_dates": dates[-limit:]}
            fallback = (
                f"I have {len(dates)} dates available, ranging from {dates[0]} to {dates[-1]}."
                if dates
                else "No historical dates are available."
            )
            return self._build_tool_response(
                message=message,
                tool_name=tool_name,
                tool_args={"limit": limit},
                tool_result=data,
                fallback=fallback,
                as_of_date=dates[-1] if dates else None,
                use_retrieval=use_retrieval,
                retrieval_query=retrieval_query,
                retrieval_source_types=retrieval_types,
                confidence=confidence,
                reasoning=reasoning,
            )

        if tool_name == "get_metric_series":
            metric = normalize_metric_name(args.get("metric")) or infer_metric(message) or "options_volume"
            end_date = args.get("end_date") or requested_date
            window_days = normalize_window_days(args.get("window_days")) or infer_window_days(message)
            metric_data = self.query_metric_series(metric=metric, window_days=window_days, end_date=end_date)
            ml = str(metric_data.get("metric_label") or "") or display_name_for_metric(metric)
            fallback = sentence_for_metric_series(
                metric=metric,
                metric_label=ml,
                latest_value=metric_data["latest_value"],
                end_date=str(metric_data["end_date"]),
                window_days=int(metric_data["window_days"]),
                latest_observed_value=metric_data.get("latest_observed_value"),
                latest_observed_date=metric_data.get("latest_observed_date"),
                non_null_count=int(metric_data.get("non_null_count") or 0),
            )
            return self._build_tool_response(
                message=message,
                tool_name=tool_name,
                tool_args={"metric": metric, "window_days": window_days, "end_date": metric_data["end_date"]},
                tool_result=metric_data,
                fallback=fallback,
                as_of_date=metric_data["end_date"],
                use_retrieval=use_retrieval,
                retrieval_query=retrieval_query,
                retrieval_source_types=retrieval_types,
                confidence=confidence,
                reasoning=reasoning,
            )

        if tool_name == "get_market_snapshot":
            target_date = args.get("date") or requested_date
            snapshot = self.build_snapshot(date=target_date)
            return self._build_tool_response(
                message=message,
                tool_name=tool_name,
                tool_args={"date": snapshot["date"]},
                tool_result=snapshot,
                fallback=self._snapshot_fallback(snapshot),
                as_of_date=snapshot["date"],
                use_retrieval=use_retrieval,
                retrieval_query=retrieval_query,
                retrieval_source_types=retrieval_types,
                confidence=confidence,
                reasoning=reasoning,
            )

        if tool_name == "get_contract_activity":
            market = normalize_market_name(args.get("market") or infer_market(message))
            metric = normalize_contract_metric(args.get("metric"), market) or "volume"
            payload = self.get_contract_activity(
                contract_month=args.get("contract_month"),
                market=market,
                metric=metric,
                window_days=normalize_window_days(args.get("window_days")) or infer_window_days(message),
                end_date=args.get("end_date") or requested_date,
                date=args.get("date"),
            )
            fallback = contract_activity_fallback(payload)
            return self._build_tool_response(
                message=message,
                tool_name=tool_name,
                tool_args={
                    "contract_month": args.get("contract_month"),
                    "market": market,
                    "metric": metric,
                    "window_days": payload.get("window_days"),
                    "date": payload.get("date"),
                    "end_date": payload.get("end_date"),
                },
                tool_result=payload,
                fallback=fallback,
                as_of_date=payload.get("date") or payload.get("end_date"),
                use_retrieval=use_retrieval,
                retrieval_query=retrieval_query,
                retrieval_source_types=retrieval_types,
                confidence=confidence,
                reasoning=reasoning,
            )

        if tool_name == "get_iv_analysis":
            analysis_type = normalize_iv_analysis_type(args.get("analysis_type"))
            payload = self.get_iv_analysis(
                analysis_type=analysis_type,
                window_days=normalize_window_days(args.get("window_days")) or infer_window_days(message),
                end_date=args.get("end_date") or requested_date,
                date=args.get("date"),
                contract_month=args.get("contract_month"),
            )
            return self._build_tool_response(
                message=message,
                tool_name=tool_name,
                tool_args={
                    "analysis_type": analysis_type,
                    "window_days": payload.get("window_days"),
                    "date": payload.get("date"),
                    "end_date": payload.get("end_date"),
                    "contract_month": args.get("contract_month"),
                },
                tool_result=payload,
                fallback=iv_analysis_fallback(payload),
                as_of_date=payload.get("date") or payload.get("end_date"),
                use_retrieval=use_retrieval,
                retrieval_query=retrieval_query,
                retrieval_source_types=retrieval_types,
                confidence=confidence,
                reasoning=reasoning,
            )

        if tool_name == "get_positioning_analysis":
            analysis_type = normalize_positioning_analysis_type(args.get("analysis_type"))
            payload = self.get_positioning_analysis(
                analysis_type=analysis_type,
                date=args.get("date") or requested_date,
                lookback_days=normalize_window_days(args.get("lookback_days")) or infer_window_days(message),
            )
            return self._build_tool_response(
                message=message,
                tool_name=tool_name,
                tool_args={
                    "analysis_type": analysis_type,
                    "lookback_days": normalize_window_days(args.get("lookback_days")) or infer_window_days(message),
                    "date": payload.get("date"),
                },
                tool_result=payload,
                fallback=positioning_fallback(payload),
                as_of_date=payload.get("date"),
                use_retrieval=True if not plan.get("use_retrieval") else use_retrieval,
                retrieval_query=retrieval_query,
                retrieval_source_types=retrieval_types or ["rules_of_thumb", "interpretation_guidance", "commentary_guidance"],
                confidence=confidence,
                reasoning=reasoning,
            )

        if tool_name == "compare_periods":
            payload = self.compare_periods(
                comparison_type=normalize_comparison_type(args.get("comparison_type")),
                metric=args.get("metric"),
                primary_date=args.get("primary_date"),
                secondary_date=args.get("secondary_date"),
                end_date=args.get("end_date") or requested_date,
                window_days=normalize_window_days(args.get("window_days")) or infer_window_days(message),
                market=normalize_market_name(args.get("market") or infer_market(message)),
                contract_month=args.get("contract_month"),
            )
            return self._build_tool_response(
                message=message,
                tool_name=tool_name,
                tool_args=args,
                tool_result=payload,
                fallback=comparison_fallback(payload),
                as_of_date=payload.get("end_date") or payload.get("secondary_date"),
                use_retrieval=use_retrieval,
                retrieval_query=retrieval_query,
                retrieval_source_types=retrieval_types,
                confidence=confidence,
                reasoning=reasoning,
            )

        answer = self._invoke_bedrock_with_fallback(
            (
                "You are OptionsAI's market assistant. Give a helpful, substantive answer when you can. "
                "Make it explicit when you are answering from general knowledge rather than the dataset."
            ),
            message,
            fallback=(
                "I can answer broader market questions, but this response is not grounded "
                "in the historical OptionsAI datasets."
            ),
            max_tokens=DIRECT_WRITER_MAX_TOKENS,
            temperature=DIRECT_WRITER_TEMPERATURE,
        )
        sources = self.retrieval_service.retrieve(
            retrieval_query,
            top_k=DEFAULT_RETRIEVAL_TOP_K,
            source_types=retrieval_types,
        ) if use_retrieval else []
        return {
            "answer": answer,
            "provenance": "general_interpretation",
            "as_of_date": None,
            "data": None,
            "tool_trace": [
                {
                    "tool": tool_name or "answer_general_question",
                    "args": args,
                    "confidence": confidence,
                    "reasoning": reasoning,
                }
            ],
            "sources": sources,
        }

    def _build_tool_response(
        self,
        *,
        message: str,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: dict[str, Any],
        fallback: str,
        as_of_date: str | None,
        use_retrieval: bool,
        retrieval_query: str,
        retrieval_source_types: list[str],
        confidence: str,
        reasoning: str,
    ) -> dict[str, Any]:
        sources = self.retrieval_service.retrieve(
            retrieval_query,
            top_k=DEFAULT_RETRIEVAL_TOP_K,
            source_types=retrieval_source_types,
        ) if use_retrieval else []
        answer = self._answer_from_tool_result(message, tool_name, tool_result, fallback, sources)
        return {
            "answer": answer,
            "provenance": "data_tools_with_guidance" if sources else "data_tools",
            "as_of_date": as_of_date,
            "data": tool_result,
            "tool_trace": [
                {
                    "tool": tool_name,
                    "args": _sanitize(tool_args),
                    "confidence": confidence,
                    "reasoning": reasoning,
                }
            ],
            "sources": sources,
        }

    def _answer_from_tool_result(
        self,
        message: str,
        tool_name: str,
        tool_result: dict[str, Any],
        fallback: str,
        guidance_sources: list[dict[str, Any]] | None = None,
    ) -> str:
        prompt = (
            f"User question:\n{message}\n\n"
            f"Tool used: {tool_name}\n\n"
            f"Tool result:\n{json.dumps(tool_result, indent=2)}\n\n"
            f"Retrieved guidance:\n{json.dumps(guidance_sources or [], indent=2)}"
        )
        return self._invoke_bedrock_with_fallback(
            TOOL_ANSWER_SYSTEM_PROMPT.format(house_style=HOUSE_STYLE_GUIDANCE),
            prompt,
            fallback=fallback,
            max_tokens=TOOL_WRITER_MAX_TOKENS,
            temperature=TOOL_WRITER_TEMPERATURE,
        )

    def _heuristic_plan(
        self,
        message: str,
        requested_date: str | None,
        thread_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        lowered = message.lower()
        metric = infer_metric(message)
        if "available date" in lowered or ("date" in lowered and "available" in lowered):
            return {"tool": "get_available_dates", "args": {"limit": 30}, "use_retrieval": False, "confidence": "high", "reasoning": "The user explicitly asked about available dates."}
        if looks_like_interpretation_question(message):
            return {
                "tool": "interpret_market_context",
                "args": {
                    "focus": infer_interpretation_focus(message),
                    "window_days": infer_window_days(message),
                    "date": requested_date,
                },
                "use_retrieval": True,
                "retrieval_query": message,
                "retrieval_source_types": ["rules_of_thumb", "interpretation_guidance", "commentary_guidance"],
                "confidence": "medium",
                "reasoning": "The question asks for an interpretation of market behavior, so a combined context is more useful than a single series.",
            }
        if looks_like_iv_question(message):
            return {
                "tool": "get_iv_analysis",
                "args": {
                    "analysis_type": infer_iv_analysis_type(message),
                    "window_days": infer_window_days(message),
                    "end_date": requested_date,
                },
                "use_retrieval": "mean" in lowered or "interpret" in lowered or "suggest" in lowered,
                "retrieval_query": message,
                "retrieval_source_types": ["rules_of_thumb", "interpretation_guidance", "commentary_guidance"],
                "confidence": "medium",
                "reasoning": "The question mentions implied volatility or IV.",
            }
        if looks_like_positioning_question(message):
            return {
                "tool": "get_positioning_analysis",
                "args": {
                    "analysis_type": infer_positioning_analysis_type(message),
                    "date": requested_date,
                    "lookback_days": infer_window_days(message),
                },
                "use_retrieval": True,
                "retrieval_query": message,
                "retrieval_source_types": ["rules_of_thumb", "interpretation_guidance", "commentary_guidance"],
                "confidence": "medium",
                "reasoning": "The question asks about positioning-style behavior.",
            }
        if looks_like_comparison_question(message):
            return heuristic_comparison_plan(message, requested_date, thread_state)
        if looks_like_contract_question(message):
            return {
                "tool": "get_contract_activity",
                "args": {
                    "market": infer_market(message),
                    "metric": infer_contract_metric(message),
                    "contract_month": extract_contract_month(message),
                    "window_days": infer_window_days(message),
                    "end_date": requested_date,
                    "date": requested_date if looks_like_snapshot_request(message, requested_date) else None,
                },
                "use_retrieval": "interpret" in lowered or "mean" in lowered,
                "retrieval_query": message,
                "retrieval_source_types": ["interpretation_guidance", "commentary_guidance"],
                "confidence": "medium",
                "reasoning": "The question mentions a contract month or expiry-style concept.",
            }
        if metric is not None:
            return {
                "tool": "get_metric_series",
                "args": {
                    "metric": metric,
                    "window_days": infer_window_days(message),
                    "end_date": requested_date,
                },
                "use_retrieval": "mean" in lowered or "interpret" in lowered or "suggest" in lowered,
                "retrieval_query": message,
                "retrieval_source_types": ["interpretation_guidance", "commentary_guidance"],
                "confidence": "medium",
                "reasoning": "The question maps to a standard metric series.",
            }
        if looks_like_ambiguous_follow_up(message):
            previous_step = latest_tool_step(thread_state or {})
            if previous_step and previous_step.get("tool") not in {"direct_answer", "answer_general_question"}:
                inherited_args = dict(previous_step.get("args") or {})
                if requested_date:
                    if "end_date" in inherited_args:
                        inherited_args["end_date"] = requested_date
                    elif "date" in inherited_args:
                        inherited_args["date"] = requested_date
                return {
                    "tool": previous_step["tool"],
                    "args": inherited_args,
                    "use_retrieval": bool((thread_state or {}).get("latest_provenance") == "data_tools_with_guidance"),
                    "retrieval_query": message,
                    "retrieval_source_types": ["rules_of_thumb", "interpretation_guidance", "commentary_guidance"],
                    "confidence": "high",
                    "reasoning": "The follow-up is terse and ambiguous, so I continued the prior analytical tool with inherited arguments.",
                }
        if looks_like_snapshot_request(message, requested_date):
            return {
                "tool": "get_market_snapshot",
                "args": {"date": requested_date},
                "use_retrieval": "mean" in lowered or "interpret" in lowered or "suggest" in lowered,
                "retrieval_query": message,
                "retrieval_source_types": ["rules_of_thumb", "interpretation_guidance", "commentary_guidance"],
                "confidence": "medium",
                "reasoning": "The question asks for a date-specific or latest market summary.",
            }
        if looks_like_general_question(message):
            return {
                "tool": "answer_general_question",
                "args": {},
                "use_retrieval": True,
                "retrieval_query": message,
                "retrieval_source_types": ["rules_of_thumb", "interpretation_guidance", "commentary_guidance"],
                "confidence": "low",
                "reasoning": "The question appears educational rather than dataset-specific.",
            }
        return {
            "tool": "get_market_snapshot",
            "args": {"date": requested_date},
            "use_retrieval": False,
            "confidence": "low",
            "reasoning": "Fallback to a market snapshot when no better tool match is available.",
        }

    def _invoke_bedrock_with_fallback(
        self,
        system_prompt: str,
        user_prompt: str,
        fallback: str,
        *,
        max_tokens: int = TOOL_WRITER_MAX_TOKENS,
        temperature: float = TOOL_WRITER_TEMPERATURE,
    ) -> str:
        try:
            response = self.bedrock_client.chat(
                system_prompt,
                user_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return response or fallback
        except Exception as exc:
            logger.warning("Bedrock call failed; using fallback: %s", exc)
            return fallback

    @staticmethod
    def _snapshot_fallback(snapshot: dict[str, Any]) -> str:
        overview = snapshot["metrics"]["overview"]
        return (
            f"As of {snapshot['date']}, total options volume was {overview['total_volume']:,}, "
            f"total open interest was {overview['total_oi']:,}, and net open interest change was "
            f"{overview['total_oi_change']:+,}. Front-month futures settled at "
            f"{snapshot['market_context']['settle'] if snapshot.get('market_context') else 'N/A'}."
        )



def build_service_from_env() -> AnalyticsService:
    region = os.environ.get("AWS_REGION", "us-east-2")
    bucket = os.environ["S3_BUCKET"]
    prefix = os.environ["S3_PREFIX"]
    model_id = os.environ["BEDROCK_MODEL_ID"]
    chat_mode = os.environ.get("CHAT_MODE", "agent")
    agentic_max_steps = int(os.environ.get("AGENTIC_MAX_STEPS", str(AGENTIC_MAX_STEPS)))
    soft_deadline_seconds = float(
        os.environ.get("CHAT_SOFT_DEADLINE_SECONDS", str(DEFAULT_CHAT_SOFT_DEADLINE_SECONDS))
    )
    agent_soft_deadline_raw = os.environ.get("CHAT_AGENT_SOFT_DEADLINE_SECONDS")
    if chat_mode == "agent" and agent_soft_deadline_raw not in (None, ""):
        soft_deadline_seconds = float(agent_soft_deadline_raw)
    heartbeat_seconds = float(
        os.environ.get("CHAT_HEARTBEAT_SECONDS", str(DEFAULT_CHAT_HEARTBEAT_SECONDS))
    )
    partial_min_tools = int(
        os.environ.get("CHAT_PARTIAL_MIN_TOOLS", str(DEFAULT_CHAT_PARTIAL_MIN_TOOLS))
    )
    bedrock_read_timeout_seconds = float(
        os.environ.get(
            "BEDROCK_READ_TIMEOUT_SECONDS",
            str(DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS),
        )
    )
    repository = S3HistoricalRepository(bucket=bucket, prefix=prefix, region=region)
    bedrock_client = BedrockChatClient(
        region=region,
        model_id=model_id,
        read_timeout_seconds=bedrock_read_timeout_seconds,
    )
    retrieval_service = GuidanceRetrievalService(bucket=bucket, prefix=prefix, region=region)
    conversation_store = build_conversation_store_from_env(region)
    return AnalyticsService(
        repository=repository,
        bedrock_client=bedrock_client,
        retrieval_service=retrieval_service,
        conversation_store=conversation_store,
        chat_mode=chat_mode,
        agentic_max_steps=agentic_max_steps,
        soft_deadline_seconds=soft_deadline_seconds,
        heartbeat_seconds=heartbeat_seconds,
        partial_min_tools=partial_min_tools,
    )
