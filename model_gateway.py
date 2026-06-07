#!/usr/bin/env python3
import argparse
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_REGISTRY_PATH = "model_registry.json"
DEFAULT_CUSTOMERS_PATH = "customer_keys.json"
DEFAULT_DASHBOARD_PATH = "dashboard.html"
DEFAULT_LOG_DIR = "logs"
DEFAULT_DATA_DIR = "data"
DEFAULT_GATEWAY_API_KEY = "dev-gateway-key"
DEFAULT_ADMIN_API_KEY = "dev-admin-key"
DEFAULT_REQUEST_LIMIT = 60
DEFAULT_LIMIT_WINDOW_SECONDS = 60
PROVIDER_TYPE_CONTRACTS = {
    "openai_compatible": {
        "label": "OpenAI-compatible",
        "adapter_status": "implemented",
        "examples": ["Alibaba Cloud Model Studio compatible mode", "OpenAI", "Xiaomi OpenAI-compatible endpoints if offered"],
        "auth": "Bearer token header",
        "chat_endpoint": "/chat/completions",
        "request_shape": "OpenAI chat.completions style",
        "response_shape": "OpenAI chat.completion style",
        "streaming": "Server-Sent Events in OpenAI-compatible chunks",
        "tools": "Pass through OpenAI-style tools when the upstream provider supports them",
        "usage": "Use provider usage fields when available; estimate in mock mode",
        "main_risk": "Compatible providers may still differ in model names, tool behavior, errors, and streaming details.",
        "next_step": "Test each provider with chat, stream, tools, error, timeout, and usage cases before production.",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "adapter_status": "scaffolded",
        "examples": ["Claude Messages API"],
        "auth": "x-api-key header plus anthropic-version",
        "chat_endpoint": "/messages",
        "request_shape": "Anthropic messages style",
        "response_shape": "Normalized back to OpenAI chat.completion style",
        "streaming": "Provider-specific stream events need production hardening",
        "tools": "OpenAI tools need normalization into Anthropic tool schema",
        "usage": "Normalize Anthropic usage into prompt, completion, and total tokens",
        "main_risk": "Tool calls, system prompts, stop reasons, and stream events are not the same as OpenAI.",
        "next_step": "Add live Claude contract tests before enabling customer traffic.",
    },
    "xiaomi_planned": {
        "label": "Xiaomi or other local model provider",
        "adapter_status": "planned",
        "examples": ["Future Xiaomi model API", "Other regional model APIs"],
        "auth": "Unknown until provider docs are confirmed",
        "chat_endpoint": "Unknown until provider docs are confirmed",
        "request_shape": "Must be mapped after provider documentation is reviewed",
        "response_shape": "Must be normalized into OpenAI chat.completion style",
        "streaming": "Unknown until tested",
        "tools": "Unknown until tested",
        "usage": "Unknown until tested",
        "main_risk": "Provider docs, auth, streaming, tools, and billing details may be different.",
        "next_step": "Collect official provider docs, add a disabled provider config, then implement adapter tests.",
    },
}
ADMIN_PATHS = {
    "/admin",
    "/v1/gateway/status",
    "/v1/gateway/audit-events",
    "/v1/gateway/requests",
    "/v1/gateway/usage",
    "/v1/gateway/customers",
    "/v1/gateway/providers",
    "/v1/gateway/provider-health",
    "/v1/gateway/provider-contracts",
    "/v1/gateway/customer-reports",
    "/v1/gateway/customer-success",
    "/v1/gateway/commercial-policy",
    "/v1/gateway/procurement-pack",
    "/v1/gateway/business-case",
    "/v1/gateway/implementation-plan",
    "/v1/gateway/alternatives-pack",
    "/v1/gateway/policy-presets",
    "/v1/gateway/demo-bundle",
    "/v1/gateway/handoff-checklist",
    "/v1/gateway/production-readiness",
    "/v1/gateway/deployment-readiness",
    "/v1/gateway/migration-plan",
    "/v1/gateway/production-backlog",
    "/v1/gateway/launch-plan",
    "/v1/gateway/change-management",
    "/v1/gateway/data-governance",
    "/v1/gateway/security-review",
    "/v1/gateway/operations-runbook",
    "/v1/gateway/incident-playbook",
    "/v1/gateway/support-policy",
    "/v1/gateway/pilot-checklist",
    "/v1/gateway/pilot-scorecard",
    "/v1/gateway/evaluation-plan",
    "/v1/gateway/discovery-checklist",
    "/v1/gateway/proposal-summary",
    "/v1/gateway/onboarding-plan",
    "/v1/gateway/executive-brief",
    "/v1/gateway/roadmap",
    "/v1/gateway/decision-guide",
    "/v1/gateway/faq",
    "/v1/gateway/demo-script",
    "/v1/gateway/request-activity",
    "/v1/gateway/model-catalog",
    "/v1/gateway/route-preview",
    "/v1/gateway/key-issue-preview",
    "/v1/gateway/safety-preview",
    "/v1/gateway/request-detail",
    "/v1/gateway/alerts",
    "/v1/gateway/access-matrix",
    "/v1/gateway/cost-estimate",
    "/v1/gateway/customer-usage",
    "/v1/gateway/invoice-preview",
    "/v1/gateway/model-usage",
    "/v1/gateway/request-summary",
    "/v1/gateway/config-check",
}


class GatewayError(Exception):
    def __init__(self, message, code="gateway_error", status=500, details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details


class ProviderError(Exception):
    def __init__(self, message, status=502, body=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body


def now_unix():
    return int(time.time())


def key_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def mask_key(value):
    if not value:
        return ""
    if len(value) <= 14:
        return value[:4] + "..."
    return value[:8] + "..." + value[-6:]


def resolve_secret(value):
    if not value:
        return None
    if isinstance(value, str) and value.startswith("env:"):
        return os.getenv(value.removeprefix("env:"))
    return value


def public_customer_view(customer):
    provider_keys = customer.get("provider_api_keys", {})
    return {
        "id": customer["id"],
        "name": customer.get("name", customer["id"]),
        "plan": customer.get("plan", "prototype"),
        "default_policy": customer.get("default_policy"),
        "request_limit": customer.get("request_limit"),
        "limit_window_seconds": customer.get("limit_window_seconds"),
        "token_budget": customer.get("token_budget"),
        "cost_budget": customer.get("cost_budget"),
        "allowed_models": customer.get("allowed_models", []),
        "byok_providers": sorted(provider_keys.keys()),
    }


def read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path, payload):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)
        file.write("\n")


def append_jsonl(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def init_db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            create table if not exists requests (
                id integer primary key autoincrement,
                created integer not null,
                request_id text,
                customer_id text,
                api_key_hash text,
                model text,
                resolved_model text,
                provider text,
                status integer,
                code text,
                latency_ms real,
                mock_mode integer
            )
            """
        )
        conn.execute(
            """
            create table if not exists usage_records (
                id integer primary key autoincrement,
                created integer not null,
                customer_id text,
                model text,
                resolved_model text,
                provider text,
                prompt_tokens integer,
                completion_tokens integer,
                total_tokens integer,
                estimated_cost real,
                mock_mode integer
            )
            """
        )
        conn.execute(
            """
            create table if not exists customers (
                id text primary key,
                name text,
                api_key_hash text,
                plan text,
                request_limit integer,
                limit_window_seconds integer,
                token_budget integer,
                cost_budget real,
                allowed_models text,
                byok_providers text,
                enabled integer
            )
            """
        )
        conn.execute(
            """
            create table if not exists audit_events (
                id integer primary key autoincrement,
                created integer not null,
                actor text,
                action text,
                target_type text,
                target_id text,
                status text,
                details text
            )
            """
        )
        customer_columns = {
            row[1]
            for row in conn.execute("pragma table_info(customers)").fetchall()
        }
        customer_migrations = {
            "plan": "alter table customers add column plan text",
            "token_budget": "alter table customers add column token_budget integer",
            "cost_budget": "alter table customers add column cost_budget real",
        }
        for column, sql in customer_migrations.items():
            if column not in customer_columns:
                conn.execute(sql)
        if "byok_providers" not in customer_columns:
            conn.execute("alter table customers add column byok_providers text")
        conn.commit()


def sync_customers_to_db(path, customers_by_key):
    with sqlite3.connect(path) as conn:
        for api_key, customer in customers_by_key.items():
            conn.execute(
                """
                insert into customers (
                    id, name, api_key_hash, plan, request_limit,
                    limit_window_seconds, token_budget, cost_budget,
                    allowed_models, byok_providers, enabled
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    name=excluded.name,
                    api_key_hash=excluded.api_key_hash,
                    plan=excluded.plan,
                    request_limit=excluded.request_limit,
                    limit_window_seconds=excluded.limit_window_seconds,
                    token_budget=excluded.token_budget,
                    cost_budget=excluded.cost_budget,
                    allowed_models=excluded.allowed_models,
                    byok_providers=excluded.byok_providers,
                    enabled=excluded.enabled
                """,
                (
                    customer["id"],
                    customer.get("name", customer["id"]),
                    key_hash(api_key),
                    customer.get("plan", "prototype"),
                    int(customer.get("request_limit", DEFAULT_REQUEST_LIMIT)),
                    int(customer.get("limit_window_seconds", DEFAULT_LIMIT_WINDOW_SECONDS)),
                    customer.get("token_budget"),
                    customer.get("cost_budget"),
                    json.dumps(customer.get("allowed_models", ["*"])),
                    json.dumps(sorted(customer.get("provider_api_keys", {}).keys())),
                    1 if customer.get("enabled", True) else 0,
                ),
            )
        conn.commit()


def insert_request_db(path, record):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            insert into requests (
                created, request_id, customer_id, api_key_hash, model,
                resolved_model, provider, status, code, latency_ms, mock_mode
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("created"),
                record.get("request_id"),
                record.get("customer_id"),
                record.get("api_key_hash"),
                record.get("model"),
                record.get("resolved_model"),
                record.get("provider"),
                record.get("status"),
                record.get("code"),
                record.get("latency_ms"),
                1 if record.get("mock_mode") else 0,
            ),
        )
        conn.commit()


def insert_usage_db(path, record):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            insert into usage_records (
                created, customer_id, model, resolved_model, provider,
                prompt_tokens, completion_tokens, total_tokens,
                estimated_cost, mock_mode
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("created"),
                record.get("customer_id"),
                record.get("model"),
                record.get("resolved_model"),
                record.get("provider"),
                record.get("prompt_tokens"),
                record.get("completion_tokens"),
                record.get("total_tokens"),
                record.get("estimated_cost"),
                1 if record.get("mock_mode") else 0,
            ),
        )
        conn.commit()


def insert_audit_db(path, record):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            insert into audit_events (
                created, actor, action, target_type, target_id, status, details
            )
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("created"),
                record.get("actor"),
                record.get("action"),
                record.get("target_type"),
                record.get("target_id"),
                record.get("status"),
                json.dumps(record.get("details", {}), ensure_ascii=False),
            ),
        )
        conn.commit()


def db_tail(path, table, limit=50):
    allowed = {"requests", "usage_records", "customers", "audit_events"}
    if table not in allowed or not os.path.exists(path):
        return []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        if table == "customers":
            rows = conn.execute("select * from customers order by id asc").fetchall()
        else:
            rows = conn.execute(
                f"select * from {table} order by id desc limit ?",
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def bounded_int(value, default, minimum=1, maximum=500):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def request_activity(path, filters=None, limit=50):
    filters = filters or {}
    if not os.path.exists(path):
        return []
    allowed_filters = {
        "customer_id": "customer_id",
        "model": "model",
        "provider": "provider",
        "code": "code",
    }
    clauses = []
    values = []
    for filter_name, column in allowed_filters.items():
        value = filters.get(filter_name)
        if value:
            clauses.append(f"{column} = ?")
            values.append(value)
    status_filter = filters.get("status")
    if status_filter == "error":
        clauses.append("status >= 400")
    elif status_filter == "success":
        clauses.append("status < 400")
    where = f"where {' and '.join(clauses)}" if clauses else ""
    values.append(bounded_int(limit, 50))
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select
                created, request_id, customer_id, model, resolved_model,
                provider, status, code, latency_ms, mock_mode
            from requests
            {where}
            order by id desc
            limit ?
            """,
            values,
        ).fetchall()
    activity = []
    for row in rows:
        record = dict(row)
        status_code = int(record.get("status") or 0)
        record["outcome"] = "error" if status_code >= 400 else "success"
        record["summary"] = (
            f"{record.get('customer_id') or 'unknown'} used "
            f"{record.get('model') or 'unknown'} via "
            f"{record.get('provider') or 'none'}: {record.get('code') or status_code}"
        )
        activity.append(record)
    return activity


def audit_events(path, filters=None, limit=50):
    filters = filters or {}
    if not os.path.exists(path):
        return []
    allowed_filters = {
        "actor": "actor",
        "action": "action",
        "target_type": "target_type",
        "target_id": "target_id",
        "status": "status",
    }
    clauses = []
    values = []
    for filter_name, column in allowed_filters.items():
        value = filters.get(filter_name)
        if value:
            clauses.append(f"{column} = ?")
            values.append(value)
    where = f"where {' and '.join(clauses)}" if clauses else ""
    values.append(bounded_int(limit, 50))
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select
                created, actor, action, target_type, target_id, status, details
            from audit_events
            {where}
            order by id desc
            limit ?
            """,
            values,
        ).fetchall()
    events = []
    for row in rows:
        record = dict(row)
        try:
            record["details"] = json.loads(record.get("details") or "{}")
        except json.JSONDecodeError:
            record["details"] = {}
        record["summary"] = (
            f"{record.get('actor') or 'unknown'} {record.get('action') or 'changed'} "
            f"{record.get('target_type') or 'target'} {record.get('target_id') or 'unknown'}"
        )
        events.append(record)
    return events


def request_detail(path, request_id):
    if not request_id or not os.path.exists(path):
        return None
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        request_row = conn.execute(
            """
            select
                created, request_id, customer_id, model, resolved_model,
                provider, status, code, latency_ms, mock_mode
            from requests
            where request_id = ?
            """,
            (request_id,),
        ).fetchone()
        if not request_row:
            return None
        usage_row = conn.execute(
            """
            select
                prompt_tokens, completion_tokens, total_tokens,
                estimated_cost, mock_mode
            from usage_records
            where customer_id = ?
              and model = ?
              and resolved_model = ?
              and provider = ?
              and abs(created - ?) <= 2
            order by id desc
            limit 1
            """,
            (
                request_row["customer_id"],
                request_row["model"],
                request_row["resolved_model"],
                request_row["provider"],
                request_row["created"],
            ),
        ).fetchone()
    detail = dict(request_row)
    status_code = int(detail.get("status") or 0)
    detail["outcome"] = "error" if status_code >= 400 else "success"
    detail["summary"] = (
        f"{detail.get('customer_id') or 'unknown'} used "
        f"{detail.get('model') or 'unknown'} via "
        f"{detail.get('provider') or 'none'}: {detail.get('code') or status_code}"
    )
    detail["usage"] = dict(usage_row) if usage_row else None
    return detail


def db_summary(path):
    if not os.path.exists(path):
        return {}
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        request_count = conn.execute("select count(*) as value from requests").fetchone()["value"]
        audit_event_count = conn.execute("select count(*) as value from audit_events").fetchone()["value"]
        usage = conn.execute(
            """
            select
                coalesce(sum(prompt_tokens), 0) as prompt_tokens,
                coalesce(sum(completion_tokens), 0) as completion_tokens,
                coalesce(sum(total_tokens), 0) as total_tokens,
                coalesce(sum(estimated_cost), 0) as estimated_cost
            from usage_records
            """
        ).fetchone()
        by_customer = conn.execute(
            """
            select customer_id, count(*) as requests
            from requests
            group by customer_id
            order by requests desc
            """
        ).fetchall()
    return {
        "request_count": request_count,
        "audit_event_count": audit_event_count,
        "usage": dict(usage),
        "requests_by_customer": [dict(row) for row in by_customer],
    }


def customer_usage_summary(path, customer_id):
    empty = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated_cost": 0,
    }
    if not customer_id or not os.path.exists(path):
        return empty
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        usage = conn.execute(
            """
            select
                coalesce(sum(prompt_tokens), 0) as prompt_tokens,
                coalesce(sum(completion_tokens), 0) as completion_tokens,
                coalesce(sum(total_tokens), 0) as total_tokens,
                coalesce(sum(estimated_cost), 0) as estimated_cost
            from usage_records
            where customer_id = ?
            """,
            (customer_id,),
        ).fetchone()
    return dict(usage) if usage else empty


def usage_grouped_by(path, field):
    allowed = {"customer_id", "model", "resolved_model", "provider"}
    if field not in allowed or not os.path.exists(path):
        return []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select
                {field} as id,
                count(*) as usage_records,
                coalesce(sum(prompt_tokens), 0) as prompt_tokens,
                coalesce(sum(completion_tokens), 0) as completion_tokens,
                coalesce(sum(total_tokens), 0) as total_tokens,
                coalesce(sum(estimated_cost), 0) as estimated_cost
            from usage_records
            group by {field}
            order by total_tokens desc
            """
        ).fetchall()
    return [dict(row) for row in rows]


def request_grouped_by(path, field):
    allowed = {"customer_id", "model", "resolved_model", "provider", "code"}
    if field not in allowed or not os.path.exists(path):
        return []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select
                {field} as id,
                count(*) as requests,
                coalesce(avg(latency_ms), 0) as avg_latency_ms,
                sum(case when status >= 400 then 1 else 0 end) as errors
            from requests
            group by {field}
            order by requests desc
            """
        ).fetchall()
    return [dict(row) for row in rows]


def recent_provider_metrics(path, provider_id, limit=50):
    empty = {
        "sample_size": 0,
        "requests": 0,
        "errors": 0,
        "error_rate": 0,
        "avg_latency_ms": 0,
        "last_code": None,
        "last_status": None,
    }
    if not provider_id or not os.path.exists(path):
        return empty
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select status, code, latency_ms
            from requests
            where provider = ?
            order by id desc
            limit ?
            """,
            (provider_id, limit),
        ).fetchall()
    if not rows:
        return empty
    errors = sum(1 for row in rows if int(row["status"] or 0) >= 400)
    latencies = [float(row["latency_ms"] or 0) for row in rows]
    return {
        "sample_size": len(rows),
        "requests": len(rows),
        "errors": errors,
        "error_rate": round(errors / len(rows), 4),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 2),
        "last_code": rows[0]["code"],
        "last_status": rows[0]["status"],
    }


def customer_budget_status(path, customer):
    usage = customer_usage_summary(path, customer.get("id"))
    token_budget = customer.get("token_budget")
    cost_budget = customer.get("cost_budget")
    remaining_tokens = None
    remaining_cost = None
    if token_budget is not None:
        remaining_tokens = int(token_budget) - int(usage.get("total_tokens") or 0)
    if cost_budget is not None:
        remaining_cost = round(float(cost_budget) - float(usage.get("estimated_cost") or 0), 8)
    return {
        "usage": usage,
        "token_budget": token_budget,
        "cost_budget": cost_budget,
        "remaining_tokens": remaining_tokens,
        "remaining_cost": remaining_cost,
    }


def customer_budget_state(budget):
    token_budget = budget.get("token_budget")
    cost_budget = budget.get("cost_budget")
    remaining_tokens = budget.get("remaining_tokens")
    remaining_cost = budget.get("remaining_cost")
    states = []
    if token_budget is not None and remaining_tokens is not None:
        if remaining_tokens <= 0:
            states.append("blocked")
        elif remaining_tokens <= max(1, int(token_budget) * 0.2):
            states.append("warning")
        else:
            states.append("ok")
    if cost_budget is not None and remaining_cost is not None:
        if remaining_cost <= 0:
            states.append("blocked")
        elif remaining_cost <= float(cost_budget) * 0.2:
            states.append("warning")
        else:
            states.append("ok")
    if "blocked" in states:
        return "blocked"
    if "warning" in states:
        return "warning"
    if "ok" in states:
        return "ok"
    return "unlimited"


def customer_dimension_usage(path, customer_id, field):
    allowed = {"model", "provider"}
    if field not in allowed or not customer_id or not os.path.exists(path):
        return []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select
                {field} as id,
                count(*) as usage_records,
                coalesce(sum(total_tokens), 0) as total_tokens,
                coalesce(sum(estimated_cost), 0) as estimated_cost
            from usage_records
            where customer_id = ?
            group by {field}
            order by total_tokens desc
            """,
            (customer_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def customer_recent_requests(path, customer_id, limit=5):
    if not customer_id or not os.path.exists(path):
        return []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select
                created, request_id, model, resolved_model, provider,
                status, code, latency_ms, mock_mode
            from requests
            where customer_id = ?
            order by id desc
            limit ?
            """,
            (customer_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def customer_reports(server):
    request_by_customer = {
        row["id"]: row
        for row in request_grouped_by(server.db_path, "customer_id")
        if row.get("id") is not None
    }
    rows = []
    for customer in sorted(server.customers_by_key.values(), key=lambda item: item.get("id", "")):
        customer_id = customer["id"]
        budget = customer_budget_status(server.db_path, customer)
        request_summary = request_by_customer.get(
            customer_id,
            {
                "id": customer_id,
                "requests": 0,
                "avg_latency_ms": 0,
                "errors": 0,
            },
        )
        request_count = int(request_summary.get("requests") or 0)
        error_count = int(request_summary.get("errors") or 0)
        rows.append(
            {
                **public_customer_view(customer),
                "request_summary": {
                    "requests": request_count,
                    "errors": error_count,
                    "error_rate": round(error_count / request_count, 4) if request_count else 0,
                    "avg_latency_ms": round(float(request_summary.get("avg_latency_ms") or 0), 2),
                },
                "budget": budget,
                "budget_state": customer_budget_state(budget),
                "usage_by_model": customer_dimension_usage(server.db_path, customer_id, "model"),
                "usage_by_provider": customer_dimension_usage(server.db_path, customer_id, "provider"),
                "recent_requests": customer_recent_requests(server.db_path, customer_id),
            }
        )
    return rows


def customer_success_summary(server):
    reports = customer_reports(server)
    accounts = []
    for report in reports:
        summary = report.get("request_summary", {})
        budget = report.get("budget", {})
        usage = budget.get("usage", {})
        request_count = int(summary.get("requests") or 0)
        error_rate = float(summary.get("error_rate") or 0)
        token_budget = budget.get("token_budget")
        total_tokens = int(usage.get("total_tokens") or 0)
        token_percent = round((total_tokens / token_budget) * 100, 2) if token_budget else None
        risk_reasons = []
        if request_count == 0:
            risk_reasons.append("No requests yet.")
        if error_rate >= 0.2:
            risk_reasons.append("Error rate is high.")
        if report.get("budget_state") in {"warning", "blocked"}:
            risk_reasons.append(f"Budget state is {report.get('budget_state')}.")
        if token_percent is not None and token_percent >= 80:
            risk_reasons.append("Token budget is close to limit.")
        if not report.get("allowed_models"):
            risk_reasons.append("No model access is configured.")

        if report.get("budget_state") == "blocked" or error_rate >= 0.5:
            health_status = "at_risk"
            risk_level = "high"
            recommended_action = "Review budget, recent errors, and support notes before the next customer call."
        elif risk_reasons:
            health_status = "watch"
            risk_level = "medium"
            recommended_action = "Confirm first use case, budget headroom, and whether support can trace recent requests."
        else:
            health_status = "healthy"
            risk_level = "low"
            recommended_action = "Continue pilot and collect feedback for the next route or provider decision."

        accounts.append(
            {
                "id": report["id"],
                "name": report.get("name"),
                "plan": report.get("plan"),
                "health_status": health_status,
                "risk_level": risk_level,
                "risk_reasons": risk_reasons,
                "recommended_action": recommended_action,
                "business_summary": {
                    "requests": request_count,
                    "errors": int(summary.get("errors") or 0),
                    "error_rate": error_rate,
                    "total_tokens": total_tokens,
                    "estimated_cost": round(float(usage.get("estimated_cost") or 0), 8),
                    "budget_state": report.get("budget_state"),
                    "token_budget_used_percent": token_percent,
                },
                "meeting_questions": [
                    "Did the customer complete the first mock request?",
                    "Can support trace the last request with gateway.request_id?",
                    "Are the current allowed models and budget enough for the next test?",
                    "Does the customer need live Qwen now, or is mock mode still enough?",
                ],
                "evidence": {
                    "customer_report": "/v1/gateway/customer-reports",
                    "request_activity": f"/v1/gateway/request-activity?customer_id={report['id']}",
                    "invoice_preview": f"/v1/gateway/invoice-preview?customer_id={report['id']}",
                    "customer_self_view": "/v1/gateway/me",
                },
            }
        )

    totals = {
        "customers": len(accounts),
        "healthy": sum(1 for account in accounts if account["health_status"] == "healthy"),
        "watch": sum(1 for account in accounts if account["health_status"] == "watch"),
        "at_risk": sum(1 for account in accounts if account["health_status"] == "at_risk"),
    }
    return {
        "object": "gateway.customer_success",
        "title": "AISmallRouter Customer Success Summary",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This summary helps business and support teams see which customers are healthy, which need follow-up, and what evidence to show.",
        "totals": totals,
        "accounts": accounts,
        "next_best_action": (
            "Follow up with at-risk customers first."
            if totals["at_risk"]
            else "Review watch customers before expanding the pilot."
            if totals["watch"]
            else "Continue the pilot and collect customer feedback."
        ),
    }


def access_matrix(server):
    rows = []
    catalog_by_id = {model["id"]: model for model in model_catalog(server)}
    for customer in sorted(server.customers_by_key.values(), key=lambda item: item.get("id", "")):
        budget = customer_budget_status(server.db_path, customer)
        budget_state = customer_budget_state(budget)
        model_access = []
        for public_name in sorted(server.models.keys()):
            model = catalog_by_id.get(public_name, {})
            allowed = customer_model_allowed(customer, public_name)
            reason = "allowed by wildcard" if "*" in customer.get("allowed_models", ["*"]) else "allowed by customer model list"
            if not allowed:
                reason = "not listed in customer allowed_models"
            model_access.append(
                {
                    "model": public_name,
                    "allowed": allowed,
                    "reason": reason,
                    "provider": model.get("provider"),
                    "provider_status": model.get("provider_status"),
                    "upstream_model": model.get("upstream_model"),
                    "fallback_models": model.get("fallback_models", []),
                    "capabilities": model.get("capabilities", []),
                }
            )
        rows.append(
            {
                **public_customer_view(customer),
                "budget_state": budget_state,
                "budget": budget,
                "models": model_access,
                "allowed_count": sum(1 for item in model_access if item["allowed"]),
                "blocked_count": sum(1 for item in model_access if not item["allowed"]),
            }
        )
    return rows


def invoice_preview(server, customer_id=None):
    reports = customer_reports(server)
    if customer_id:
        reports = [report for report in reports if report.get("id") == customer_id]
        if not reports:
            raise GatewayError(f"Unknown customer: {customer_id}.", "unknown_customer", 404)
    invoices = []
    for report in reports:
        budget = report.get("budget", {})
        usage = budget.get("usage", {})
        summary = report.get("request_summary", {})
        invoices.append(
            {
                "invoice_id": f"preview-{report['id']}-{now_unix()}",
                "customer": public_customer_view(report),
                "period": "current local data",
                "mode": "mock" if server.mock_mode else "live",
                "currency": "USD-estimate",
                "requests": int(summary.get("requests") or 0),
                "errors": int(summary.get("errors") or 0),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "estimated_cost": round(float(usage.get("estimated_cost") or 0), 8),
                "budget": {
                    "token_budget": budget.get("token_budget"),
                    "cost_budget": budget.get("cost_budget"),
                    "remaining_tokens": budget.get("remaining_tokens"),
                    "remaining_cost": budget.get("remaining_cost"),
                    "state": report.get("budget_state"),
                },
                "usage_by_model": report.get("usage_by_model", []),
                "usage_by_provider": report.get("usage_by_provider", []),
                "note": "Invoice preview uses local estimated usage. It is not a legal invoice.",
            }
        )
    totals = {
        "customers": len(invoices),
        "requests": sum(invoice["requests"] for invoice in invoices),
        "errors": sum(invoice["errors"] for invoice in invoices),
        "total_tokens": sum(invoice["total_tokens"] for invoice in invoices),
        "estimated_cost": round(sum(invoice["estimated_cost"] for invoice in invoices), 8),
    }
    return {
        "mode": "mock" if server.mock_mode else "live",
        "format": "preview",
        "totals": totals,
        "data": invoices,
        "note": "This is a billing explanation preview. Production billing needs invoices, payment state, refunds, tax, and audited records.",
    }


def invoice_preview_csv(invoice_payload):
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "invoice_id",
            "customer_id",
            "plan",
            "period",
            "mode",
            "requests",
            "errors",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost",
            "budget_state",
            "remaining_tokens",
            "remaining_cost",
            "note",
        ],
    )
    writer.writeheader()
    for invoice in invoice_payload.get("data", []):
        customer = invoice.get("customer", {})
        budget = invoice.get("budget", {})
        writer.writerow(
            {
                "invoice_id": invoice.get("invoice_id"),
                "customer_id": customer.get("id"),
                "plan": customer.get("plan"),
                "period": invoice.get("period"),
                "mode": invoice.get("mode"),
                "requests": invoice.get("requests"),
                "errors": invoice.get("errors"),
                "prompt_tokens": invoice.get("prompt_tokens"),
                "completion_tokens": invoice.get("completion_tokens"),
                "total_tokens": invoice.get("total_tokens"),
                "estimated_cost": invoice.get("estimated_cost"),
                "budget_state": budget.get("state"),
                "remaining_tokens": budget.get("remaining_tokens"),
                "remaining_cost": budget.get("remaining_cost"),
                "note": invoice.get("note"),
            }
        )
    return output.getvalue()


def commercial_policy(server):
    invoice = invoice_preview(server)
    reports = customer_reports(server)
    return {
        "object": "gateway.commercial_policy",
        "title": "AISmallRouter Commercial Policy",
        "audience": "business owner, customer sponsor, finance owner, and gateway owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This policy explains how to talk about pricing, budgets, invoice previews, and commercial boundaries before a real contract exists.",
        "commercial_position": {
            "current_stage": "prototype_preview",
            "customer_safe_message": "The gateway can estimate usage and budget impact, but it is not a legal invoice, quote, payment system, or tax document.",
            "what_can_be_shown_now": [
                "Estimated token usage from local records.",
                "Estimated model cost from registry pricing metadata.",
                "Customer token and cost budgets.",
                "Invoice preview JSON and CSV.",
                "Budget warning or blocked state.",
            ],
            "what_needs_contract_later": [
                "Final pricing units and currency.",
                "Billing period and payment terms.",
                "Overage behavior and approval owner.",
                "Refund, credit, and dispute process.",
                "Tax, legal invoice fields, and audited records.",
            ],
        },
        "budget_rules": [
            {"rule": "Request limit", "prototype_behavior": "Limits requests per configured time window.", "production_question": "Should limits reset monthly, daily, hourly, or by contract period?"},
            {"rule": "Token budget", "prototype_behavior": "Blocks requests when recorded token usage reaches the customer token budget.", "production_question": "Who can approve a token budget increase?"},
            {"rule": "Cost budget", "prototype_behavior": "Blocks requests when estimated cost reaches the customer cost budget.", "production_question": "Should overage be blocked, allowed, or require approval?"},
            {"rule": "Invoice preview", "prototype_behavior": "Shows local estimated usage and CSV export.", "production_question": "What fields are required for legal invoice and finance export?"},
        ],
        "customer_commercial_summary": [
            {
                "customer_id": report.get("id"),
                "plan": report.get("plan"),
                "budget_state": report.get("budget_state"),
                "requests": int((report.get("request_summary") or {}).get("requests") or 0),
                "estimated_cost": float((report.get("budget", {}).get("usage") or {}).get("estimated_cost") or 0),
                "commercial_note": "Estimate only; not a legal invoice.",
            }
            for report in reports
        ],
        "invoice_preview_summary": invoice.get("totals", {}),
        "approval_questions": [
            "Who owns the customer commercial relationship?",
            "What model pricing should be shown to the customer?",
            "What happens when budget is reached?",
            "Who approves budget increases?",
            "What invoice fields, tax fields, and payment terms are required?",
            "How are refunds, credits, and disputes handled?",
        ],
        "excluded_from_prototype": [
            "Legal quotation",
            "Tax invoice",
            "Payment collection",
            "Refund workflow",
            "Currency conversion",
            "Audited billing ledger",
        ],
        "evidence_endpoints": [
            "/v1/gateway/cost-estimate",
            "/v1/gateway/invoice-preview",
            "/v1/gateway/customer-reports",
            "/v1/gateway/customer-success",
            "/v1/gateway/production-backlog",
            "/v1/gateway/proposal-summary",
        ],
        "next_best_action": "Use this policy with the proposal summary before discussing price or production billing with a customer.",
        "prototype_note": "This is a commercial explanation for demos and pilots. Production still needs approved pricing, legal terms, finance review, tax handling, payment records, and audited billing data.",
    }


def procurement_pack(server):
    readiness = production_readiness(server)
    security = security_review(server)
    governance = data_governance_review(server)
    commercial = commercial_policy(server)
    return {
        "object": "gateway.procurement_pack",
        "title": "AISmallRouter Procurement And Vendor Review Pack",
        "audience": "customer sponsor, procurement, legal, IT, security, finance, and gateway owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This pack helps a customer share the gateway idea with procurement, legal, IT, security, and finance before a pilot or purchase discussion. It lists what evidence exists today, what still needs approval, and which team should answer each question.",
        "customer_safe_summary": {
            "what_this_is": "A prototype package for internal review, pilot planning, and early vendor discussion.",
            "what_this_is_not": "It is not a signed contract, legal security attestation, production SLA, final price quote, or tax invoice.",
            "best_use": "Share it before a customer workshop so business, IT, security, and finance can ask the right questions early.",
        },
        "review_tracks": [
            {
                "track": "Business sponsor",
                "question": "What problem does the gateway solve and who owns the outcome?",
                "evidence": ["/v1/gateway/executive-brief", "/v1/gateway/proposal-summary", "/v1/gateway/roadmap"],
                "prototype_answer": "One API can hide provider differences and make model access easier to explain.",
                "needs_before_production": "Approved business owner, pilot success criteria, and go-live decision path.",
            },
            {
                "track": "IT and platform",
                "question": "How will the gateway be deployed, monitored, changed, and rolled back?",
                "evidence": ["/v1/gateway/deployment-readiness", "/v1/gateway/operations-runbook", "/v1/gateway/change-management"],
                "prototype_answer": "The demo shows config checks, runbook steps, rollout gates, and rollback guidance.",
                "needs_before_production": "Chosen hosting option, CI/CD, secrets manager, observability, backups, and rollback owner.",
            },
            {
                "track": "Security",
                "question": "How are customer keys, provider keys, logs, and abuse risks controlled?",
                "evidence": ["/v1/gateway/security-review", "/v1/gateway/access-matrix", "/v1/gateway/audit-events"],
                "prototype_answer": "The demo separates customer keys from provider keys, checks admin access, and records audit events.",
                "needs_before_production": "Threat model approval, secret rotation, WAF or API gateway policy, audit retention, and incident process.",
            },
            {
                "track": "Data and privacy",
                "question": "What data is logged and how long is it kept?",
                "evidence": ["/v1/gateway/data-governance", "/v1/gateway/request-activity", "/v1/gateway/request-detail"],
                "prototype_answer": "The demo explains prompt/log handling and shows request records for investigation.",
                "needs_before_production": "Approved prompt logging policy, retention period, deletion process, and data residency decision.",
            },
            {
                "track": "Finance and commercial",
                "question": "How are budgets, estimates, invoice previews, and overage rules explained?",
                "evidence": ["/v1/gateway/commercial-policy", "/v1/gateway/invoice-preview", "/v1/gateway/cost-estimate"],
                "prototype_answer": "The demo can estimate usage and budget impact, but it is not a legal invoice or quote.",
                "needs_before_production": "Final pricing, billing period, payment terms, tax fields, refund policy, and audited billing records.",
            },
            {
                "track": "Customer technical team",
                "question": "How does the customer integrate and test safely?",
                "evidence": ["/v1/gateway/integration-guide", "/v1/gateway/sdk-starter", "/openapi.json", "/postman_collection.json"],
                "prototype_answer": "The demo gives OpenAI-compatible requests, starter code, model access, and safe mock mode.",
                "needs_before_production": "Production base URL, production keys, support channel, rate limits, and test acceptance path.",
            },
        ],
        "document_checklist": [
            {"document": "Customer guide PDF", "status": "available", "source": "Model_Gateway_Customer_Guide.pdf"},
            {"document": "OpenAPI contract", "status": "available", "source": "/openapi.json"},
            {"document": "Postman collection", "status": "available", "source": "/postman_collection.json"},
            {"document": "Security review", "status": "prototype_available", "source": "/v1/gateway/security-review"},
            {"document": "Data governance review", "status": "prototype_available", "source": "/v1/gateway/data-governance"},
            {"document": "Commercial policy", "status": "prototype_available", "source": "/v1/gateway/commercial-policy"},
            {"document": "Production SLA", "status": "not_ready", "source": "/v1/gateway/support-policy"},
            {"document": "Signed legal terms", "status": "not_in_prototype", "source": "customer contract"},
        ],
        "approval_matrix": [
            {"owner": "Business sponsor", "approves": "Pilot value, budget owner, and success criteria."},
            {"owner": "IT/platform", "approves": "Deployment path, network access, monitoring, and rollback."},
            {"owner": "Security", "approves": "Key handling, audit, incident process, and abuse controls."},
            {"owner": "Data/privacy", "approves": "Prompt logging, retention, deletion, and data location."},
            {"owner": "Finance/legal", "approves": "Pricing, invoice terms, tax fields, and contract language."},
        ],
        "current_risk_snapshot": {
            "production_readiness_status": readiness.get("overall_status"),
            "security_stage": (security.get("security_posture") or {}).get("current_stage"),
            "data_governance_categories": len(governance.get("categories", [])),
            "commercial_stage": (commercial.get("commercial_position") or {}).get("current_stage"),
        },
        "questions_to_send_before_meeting": [
            "Who will own the pilot decision?",
            "Which providers and models must be included first?",
            "Can prompts and responses be logged for debugging?",
            "What retention period is acceptable?",
            "What budget limit should stop requests?",
            "Who approves production access and emergency changes?",
            "What documents does procurement or security require before approval?",
        ],
        "red_lines": [
            "Do not promise production SLA until support and hosting are approved.",
            "Do not call invoice preview a legal invoice.",
            "Do not expose provider API keys to customers.",
            "Do not enable prompt logging for real customers without written approval.",
            "Do not add a new live provider without adapter tests and rollback plan.",
        ],
        "next_best_action": "Use this pack after the discovery checklist and proposal summary, before a pilot or procurement review meeting.",
        "evidence_endpoints": [
            "/v1/gateway/discovery-checklist",
            "/v1/gateway/proposal-summary",
            "/v1/gateway/security-review",
            "/v1/gateway/data-governance",
            "/v1/gateway/commercial-policy",
            "/v1/gateway/deployment-readiness",
            "/v1/gateway/operations-runbook",
        ],
    }


def business_case(server):
    reports = customer_reports(server)
    invoice = invoice_preview(server)
    readiness = production_readiness(server)
    procurement = procurement_pack(server)
    total_requests = sum(int((report.get("request_summary") or {}).get("requests") or 0) for report in reports)
    total_tokens = sum(int((report.get("budget", {}).get("usage") or {}).get("total_tokens") or 0) for report in reports)
    total_cost = sum(float((report.get("budget", {}).get("usage") or {}).get("estimated_cost") or 0) for report in reports)
    active_customers = [
        report.get("id")
        for report in reports
        if int((report.get("request_summary") or {}).get("requests") or 0) > 0
    ]
    return {
        "object": "gateway.business_case",
        "title": "AISmallRouter Business Case",
        "audience": "business sponsor, customer sponsor, finance, procurement, and gateway owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This business case explains why one model gateway may be worth a pilot, what value can be measured, and what still cannot be promised from prototype data.",
        "customer_safe_summary": {
            "one_sentence": "One gateway can reduce integration work, make model choice easier to control, and give business teams clearer usage and budget visibility.",
            "prototype_boundary": "The numbers below are pilot estimates from local records. They are not guaranteed savings, a formal ROI, a quote, or a production billing report.",
            "best_use": "Use this before an executive or finance conversation to agree what success should be measured during a pilot.",
        },
        "value_hypotheses": [
            {
                "value": "Less integration work",
                "why_it_matters": "Customer teams call one OpenAI-compatible API instead of building a separate integration for every provider.",
                "pilot_measure": "Count how many provider-specific code paths are avoided in the first use case.",
                "evidence": ["/v1/gateway/integration-guide", "/openapi.json", "/postman_collection.json"],
            },
            {
                "value": "Better provider control",
                "why_it_matters": "The business can start with Qwen and add OpenAI, Claude, Xiaomi, or other providers behind the same customer API later.",
                "pilot_measure": "Show route preview, fallback order, provider health, and provider contract differences.",
                "evidence": ["/v1/gateway/route-preview", "/v1/gateway/provider-health", "/v1/gateway/provider-contracts"],
            },
            {
                "value": "Budget visibility",
                "why_it_matters": "Usage, token budgets, cost estimates, and invoice preview make spending easier to discuss before production billing exists.",
                "pilot_measure": "Review estimated usage, cost, budget state, and invoice preview after pilot traffic.",
                "evidence": ["/v1/gateway/customer-reports", "/v1/gateway/cost-estimate", "/v1/gateway/invoice-preview"],
            },
            {
                "value": "Lower change risk",
                "why_it_matters": "Model/provider changes can be previewed, audited, and rolled back without changing the customer app endpoint.",
                "pilot_measure": "Run one route change in mock mode and inspect audit events, change management, and rollback notes.",
                "evidence": ["/v1/gateway/audit-events", "/v1/gateway/change-management", "/v1/gateway/model-catalog"],
            },
            {
                "value": "Clearer internal approval",
                "why_it_matters": "Procurement, legal, IT, security, and finance get a shared review pack instead of only a technical demo.",
                "pilot_measure": "Confirm which approval questions are answered and which remain production blockers.",
                "evidence": ["/v1/gateway/procurement-pack", "/v1/gateway/security-review", "/v1/gateway/data-governance"],
            },
        ],
        "pilot_metrics": {
            "customer_count": len(reports),
            "active_customers": active_customers,
            "requests_recorded": total_requests,
            "tokens_recorded": total_tokens,
            "estimated_cost": round(total_cost, 6),
            "invoice_preview_totals": invoice.get("totals", {}),
            "production_readiness_status": readiness.get("overall_status"),
        },
        "roi_inputs_to_collect": [
            "How many provider integrations would the customer otherwise build?",
            "How many engineering days does one provider integration usually take?",
            "How often does the customer expect to change models or providers?",
            "What is the cost of a failed provider route during a customer workflow?",
            "What budget limit should stop or warn before spend grows?",
            "Which internal approval documents delay the pilot today?",
        ],
        "simple_roi_formula": {
            "description": "Use this only as a workshop formula, not as a formal finance model.",
            "formula": "estimated_value = avoided_integration_days + avoided_change_risk + improved_budget_control - gateway_build_and_run_cost",
            "prototype_can_estimate": [
                "usage volume",
                "token and estimated model cost",
                "number of customers and models",
                "route and provider control points",
                "open production gaps",
            ],
            "customer_must_provide": [
                "engineering day cost",
                "current provider integration effort",
                "risk cost of outage or wrong model route",
                "finance-approved pricing and billing assumptions",
            ],
        },
        "decision_options": [
            {
                "option": "Stop after demo",
                "when_to_choose": "The customer likes the idea but has no active use case, owner, or budget.",
                "next_step": "Keep the demo as reference material and restart discovery later.",
            },
            {
                "option": "Run a small pilot",
                "when_to_choose": "The customer has one use case, one provider path, and a sponsor who can review results.",
                "next_step": "Use onboarding plan, pilot checklist, and scorecard.",
            },
            {
                "option": "Harden for production",
                "when_to_choose": "The pilot proves value and the customer needs real traffic, compliance, billing, and support.",
                "next_step": "Use production backlog, launch plan, procurement pack, and operations runbook.",
            },
        ],
        "not_claimed": [
            "Guaranteed cost savings",
            "Formal ROI",
            "Final production price",
            "Legal invoice or tax report",
            "Production SLA",
            "Security certification",
        ],
        "recommended_next_action": "Use this with the pilot scorecard and procurement pack to decide whether the next step is stop, pilot, or production hardening.",
        "evidence_endpoints": [
            "/v1/gateway/pilot-scorecard",
            "/v1/gateway/customer-reports",
            "/v1/gateway/commercial-policy",
            "/v1/gateway/procurement-pack",
            "/v1/gateway/production-backlog",
            "/v1/gateway/launch-plan",
        ],
        "procurement_context": {
            "review_tracks": [track.get("track") for track in procurement.get("review_tracks", [])],
            "red_line_count": len(procurement.get("red_lines", [])),
        },
    }


def implementation_plan(server):
    readiness = production_readiness(server)
    backlog = production_backlog(server)
    launch = launch_plan(server)
    migration = migration_plan(server)
    deployment = deployment_readiness(server)
    business = business_case(server)
    p0_items = [
        item.get("title")
        for item in backlog.get("work_items", [])
        if item.get("priority") == "P0"
    ]
    return {
        "object": "gateway.implementation_plan",
        "title": "AISmallRouter Implementation Plan",
        "audience": "customer sponsor, delivery owner, platform owner, security owner, finance owner, and gateway owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This plan turns the gateway idea into delivery steps. It explains what can be done in a prototype, what a pilot needs, what production hardening needs, and which evidence should prove each stage.",
        "customer_safe_summary": {
            "one_sentence": "Start with a small scoped pilot, prove routing and reporting value, then harden security, operations, billing, and deployment before production traffic.",
            "not_a_fixed_quote": "This is an implementation estimate for planning. It is not a fixed delivery contract, final price, or guaranteed timeline.",
            "best_use": "Use this after the business case and procurement pack to decide the next funded step.",
        },
        "delivery_phases": [
            {
                "phase": "Phase 0: Discovery and scope",
                "typical_duration": "2 to 5 working days",
                "goal": "Confirm first use case, first provider, owner, limits, success criteria, and customer review path.",
                "main_work": [
                    "Run discovery checklist.",
                    "Confirm customer sponsor and technical contact.",
                    "Choose first model route and fallback expectation.",
                    "Agree what data may be logged during pilot.",
                    "Confirm pilot budget and stop conditions.",
                ],
                "exit_evidence": ["/v1/gateway/discovery-checklist", "/v1/gateway/proposal-summary", "/v1/gateway/business-case"],
            },
            {
                "phase": "Phase 1: Local prototype demo",
                "typical_duration": "2 to 5 working days",
                "goal": "Show one API, model routing, mock responses, customer keys, usage records, and customer-friendly explanation materials.",
                "main_work": [
                    "Run mock gateway locally.",
                    "Show dashboard, OpenAPI, Postman, and customer guide PDF.",
                    "Use route preview and cost estimate.",
                    "Review commercial and procurement boundaries.",
                ],
                "exit_evidence": ["/", "/openapi.json", "/postman_collection.json", "/v1/gateway/demo-bundle"],
            },
            {
                "phase": "Phase 2: Controlled pilot",
                "typical_duration": "1 to 2 weeks",
                "goal": "Let one customer or internal team test a narrow workflow with clear limits and support path.",
                "main_work": [
                    "Issue pilot customer key.",
                    "Set request, token, and cost budgets.",
                    "Run integration guide or SDK starter.",
                    "Collect request activity, usage, and customer feedback.",
                    "Use pilot scorecard to decide stop, extend, or harden.",
                ],
                "exit_evidence": ["/v1/gateway/onboarding-plan", "/v1/gateway/pilot-checklist", "/v1/gateway/pilot-scorecard", "/v1/gateway/customer-reports"],
            },
            {
                "phase": "Phase 3: Production hardening",
                "typical_duration": "3 to 6 weeks for a small first production path",
                "goal": "Replace demo shortcuts with production controls for secrets, deployment, observability, security, support, billing, and change management.",
                "main_work": [
                    "Move customer and provider config from local JSON to managed storage.",
                    "Move provider secrets to a secret manager.",
                    "Add role-based admin access and stronger audit retention.",
                    "Connect deployment, monitoring, backup, and rollback.",
                    "Approve billing, tax, support, and incident rules.",
                ],
                "exit_evidence": ["/v1/gateway/production-backlog", "/v1/gateway/security-review", "/v1/gateway/data-governance", "/v1/gateway/operations-runbook"],
            },
            {
                "phase": "Phase 4: Production launch",
                "typical_duration": "1 to 2 weeks after hardening gates pass",
                "goal": "Roll out real traffic gradually with owners, rollback, support, and customer communication ready.",
                "main_work": [
                    "Run launch gates.",
                    "Start with low traffic and limited models.",
                    "Monitor errors, latency, budget, and provider health.",
                    "Keep rollback and customer wording ready.",
                    "Review launch results before expanding.",
                ],
                "exit_evidence": ["/v1/gateway/launch-plan", "/v1/gateway/migration-plan", "/v1/gateway/request-activity", "/v1/gateway/alerts"],
            },
        ],
        "role_plan": [
            {"role": "Business sponsor", "responsibility": "Owns use case, value, budget, and go/no-go decisions."},
            {"role": "Gateway owner", "responsibility": "Owns routing behavior, model registry, provider adapters, and release quality."},
            {"role": "Platform owner", "responsibility": "Owns hosting, deployment, monitoring, backups, and rollback."},
            {"role": "Security owner", "responsibility": "Owns key handling, secret storage, audit, abuse controls, and security review."},
            {"role": "Data/privacy owner", "responsibility": "Owns prompt logging, retention, deletion, export, and data residency decisions."},
            {"role": "Finance/legal owner", "responsibility": "Owns pricing terms, invoice rules, tax fields, refund policy, and contract language."},
            {"role": "Customer technical contact", "responsibility": "Owns integration testing, request examples, and pilot feedback."},
        ],
        "estimate_assumptions": [
            "The first pilot uses one customer team and one primary model route.",
            "Mock mode is accepted before live provider spend.",
            "Alibaba Cloud Model Studio / Qwen is the first real provider path.",
            "OpenAI, Claude, Xiaomi, or other providers are added after adapter contract review.",
            "Production requires a real secret manager, managed storage, deployment process, and approved support policy.",
        ],
        "delivery_risks": [
            {"risk": "Provider API differences", "mitigation": "Use provider contract tests before enabling live traffic."},
            {"risk": "Unclear data policy", "mitigation": "Approve prompt logging and retention before any real customer production traffic."},
            {"risk": "Budget rules not approved", "mitigation": "Use commercial policy and invoice preview as estimates until finance approves rules."},
            {"risk": "No owner for production support", "mitigation": "Use support policy, operations runbook, and launch plan before go-live."},
            {"risk": "Scope expands too early", "mitigation": "Start with one use case, one route, and one customer team."},
        ],
        "acceptance_evidence": [
            {"stage": "Prototype", "evidence": ["/", "/v1/gateway/demo-bundle", "/openapi.json", "/postman_collection.json"]},
            {"stage": "Pilot", "evidence": ["/v1/gateway/pilot-scorecard", "/v1/gateway/customer-reports", "/v1/gateway/request-activity"]},
            {"stage": "Production hardening", "evidence": ["/v1/gateway/production-readiness", "/v1/gateway/production-backlog", "/v1/gateway/security-review"]},
            {"stage": "Launch", "evidence": ["/v1/gateway/launch-plan", "/v1/gateway/migration-plan", "/v1/gateway/operations-runbook"]},
        ],
        "current_context": {
            "production_readiness_status": readiness.get("overall_status"),
            "launch_decision": launch.get("current_launch_decision") or launch.get("decision"),
            "migration_stage": migration.get("stage") or migration.get("current_stage"),
            "deployment_stage": deployment.get("stage") or deployment.get("current_stage"),
            "p0_backlog_items": p0_items,
            "business_case_decision_options": [item.get("option") for item in business.get("decision_options", [])],
        },
        "recommended_next_action": "Choose whether the next funded step is discovery, a controlled pilot, or production hardening. Do not jump to production until P0 controls are closed.",
        "not_claimed": [
            "Fixed implementation price",
            "Guaranteed delivery date",
            "Production SLA",
            "Security certification",
            "Legal or procurement approval",
            "Provider cost guarantee",
        ],
        "evidence_endpoints": [
            "/v1/gateway/business-case",
            "/v1/gateway/procurement-pack",
            "/v1/gateway/deployment-readiness",
            "/v1/gateway/production-backlog",
            "/v1/gateway/launch-plan",
            "/v1/gateway/migration-plan",
        ],
    }


def alternatives_pack(server):
    decision = decision_guide(server)
    procurement = procurement_pack(server)
    implementation = implementation_plan(server)
    return {
        "object": "gateway.alternatives_pack",
        "title": "AISmallRouter Alternatives Pack",
        "audience": "customer sponsor, solution architect, procurement, platform owner, and gateway owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This pack explains when to use a normal API Gateway, a managed AI Gateway, an OpenRouter-like platform, or a custom customer-owned Model Gateway.",
        "customer_safe_summary": {
            "one_sentence": "Do not build a custom gateway just because it is interesting. Build it only when customer-owned controls, custom routing, private usage reports, or special rollout rules matter.",
            "best_use": "Use this before committing to build work, because the cheapest good answer may be a managed gateway or direct provider integration.",
            "prototype_boundary": "This is a planning comparison. It is not a live vendor benchmark, legal recommendation, or procurement score.",
        },
        "alternatives": [
            {
                "option": "Direct provider integration",
                "plain_english": "The customer app calls one provider directly, such as Qwen, OpenAI, or Claude.",
                "best_for": "One provider, one stable model, one team, and low need for central control.",
                "tradeoffs": [
                    "Fastest start.",
                    "Lowest gateway complexity.",
                    "Harder to switch providers later.",
                    "Each provider format, key, usage report, and error shape becomes app work.",
                ],
                "choose_when": "The customer only needs one model provider and can accept provider-specific integration.",
            },
            {
                "option": "Normal API Gateway",
                "plain_english": "A standard HTTP API gateway handles auth, rate limits, routing, WAF, logs, and network policy.",
                "best_for": "Enterprise traffic control around APIs.",
                "tradeoffs": [
                    "Strong for HTTP security and traffic policy.",
                    "Does not automatically normalize AI model request, response, streaming, tool, usage, or fallback differences.",
                    "Can sit in front of a model gateway later.",
                ],
                "choose_when": "The customer mainly needs network/API control and does not need model-aware routing.",
            },
            {
                "option": "Alibaba Cloud AI Gateway",
                "plain_english": "A managed cloud-native AI gateway can connect AI apps to model services, tools, and agents inside Alibaba Cloud scenarios.",
                "best_for": "Alibaba Cloud-centered deployments and teams that want managed gateway capabilities near Model Studio or cloud runtime services.",
                "tradeoffs": [
                    "Good fit when the customer already uses Alibaba Cloud platform controls.",
                    "May reduce custom platform work.",
                    "Customer-specific UX, reporting, procurement pack, custom billing language, or private route lifecycle may still need custom code around it.",
                ],
                "choose_when": "The customer prefers Alibaba Cloud managed controls and most providers/routes fit that platform.",
            },
            {
                "option": "Vercel AI Gateway",
                "plain_english": "A managed AI gateway gives one API for many models, with provider routing, fallback, usage, and observability concepts.",
                "best_for": "Teams already using Vercel or Vercel AI SDK patterns and wanting managed multi-provider access.",
                "tradeoffs": [
                    "Can reduce provider integration and observability work.",
                    "Useful when hosted platform ownership is acceptable.",
                    "May not fit customers that require private tenancy, custom procurement materials, local-only control, or custom provider contracts.",
                ],
                "choose_when": "The customer wants a managed multi-provider gateway and accepts the platform ownership model.",
            },
            {
                "option": "OpenRouter-like platform",
                "plain_english": "A public model access platform gives one API to many models and providers, often with model discovery and BYOK-style choices.",
                "best_for": "Fast access to many public models and marketplace-style model choice.",
                "tradeoffs": [
                    "Great reference for unified API and model catalog experience.",
                    "May be faster than building broad provider coverage yourself.",
                    "May not satisfy customers who need private customer-owned controls, custom procurement evidence, special billing, or local deployment ownership.",
                ],
                "choose_when": "The customer values broad model access more than private gateway ownership.",
            },
            {
                "option": "Custom AISmallRouter-style Model Gateway",
                "plain_english": "A private gateway that owns customer keys, model aliases, provider adapters, routing policy, budgets, reports, handoff material, and rollout rules.",
                "best_for": "Customer-specific control, explanation, reporting, procurement support, and phased production hardening.",
                "tradeoffs": [
                    "Best control and customer-specific story.",
                    "More engineering responsibility.",
                    "Must build security, storage, monitoring, billing, support, and adapter tests before production.",
                ],
                "choose_when": "The customer needs private controls, custom reporting, BYOK mapping, audit, special route lifecycle, or customer-facing explanation material.",
            },
        ],
        "comparison_matrix": [
            {"criterion": "Fastest first demo", "strongest_option": "Direct provider integration or OpenRouter-like platform", "custom_gateway_note": "Use mock mode to explain value without broad provider coverage."},
            {"criterion": "Enterprise HTTP/security controls", "strongest_option": "Normal API Gateway", "custom_gateway_note": "Can integrate with an enterprise gateway later."},
            {"criterion": "Managed multi-model access", "strongest_option": "Vercel AI Gateway or Alibaba Cloud AI Gateway", "custom_gateway_note": "Use custom gateway only when managed defaults are not enough."},
            {"criterion": "Broad public model marketplace", "strongest_option": "OpenRouter-like platform", "custom_gateway_note": "Do not try to clone marketplace breadth in phase one."},
            {"criterion": "Private customer-specific controls", "strongest_option": "Custom AISmallRouter-style Model Gateway", "custom_gateway_note": "This is the strongest reason to build."},
            {"criterion": "Customer explanation pack", "strongest_option": "Custom AISmallRouter-style Model Gateway", "custom_gateway_note": "Dashboard, PDF, business case, procurement pack, and implementation plan are local assets."},
        ],
        "decision_rules": [
            "If there is only one provider and no need for custom reporting, use direct provider integration first.",
            "If the main need is WAF, rate limits, network policy, and API logs, use a normal API Gateway.",
            "If the customer accepts a managed platform and needs many providers quickly, evaluate managed AI Gateway options.",
            "If the customer needs broad public model access and marketplace-style discovery, evaluate OpenRouter-like options.",
            "If the customer needs private customer controls, BYOK mapping, custom route lifecycle, audit, budget reports, and customer-specific explanation material, use a custom Model Gateway.",
        ],
        "reference_sources": [
            {
                "name": "OpenRouter documentation",
                "url": "https://openrouter.ai/docs/faq",
                "what_to_verify": "Unified API, supported models, BYOK behavior, rate limits, billing, and provider routing rules.",
            },
            {
                "name": "Vercel AI Gateway documentation",
                "url": "https://vercel.com/docs/ai-gateway/",
                "what_to_verify": "Unified API, provider routing, fallbacks, observability, retention, pricing, and supported models.",
            },
            {
                "name": "Alibaba Cloud AI Gateway documentation",
                "url": "https://www.alibabacloud.com/help/en/api-gateway/ai-gateway/product-overview/what-is-an-ai-gateway",
                "what_to_verify": "Cloud-native AI Gateway scope, Model Studio integration, supported providers, routing, and deployment fit.",
            },
        ],
        "current_recommendation": {
            "short_answer": "For this prototype, continue as a private custom Model Gateway demo, but keep managed AI Gateway and OpenRouter-like options in the comparison.",
            "why": "The current project is mainly about customer-specific explanation, routing control, budget reports, procurement support, and production planning, not marketplace breadth.",
            "next_step": "Use the decision guide first, then use this alternatives pack before approving build or pilot scope.",
        },
        "evidence_endpoints": [
            "/v1/gateway/decision-guide",
            "/v1/gateway/business-case",
            "/v1/gateway/procurement-pack",
            "/v1/gateway/implementation-plan",
            "/v1/gateway/provider-contracts",
        ],
        "current_context": {
            "decision_guide_options": [item.get("option") for item in decision.get("options", [])],
            "procurement_tracks": [item.get("track") for item in procurement.get("review_tracks", [])],
            "implementation_phases": [item.get("phase") for item in implementation.get("delivery_phases", [])],
        },
        "not_claimed": [
            "Live vendor benchmark",
            "Legal procurement decision",
            "Final product recommendation",
            "Guaranteed cheapest option",
            "Complete OpenRouter clone",
        ],
    }


def customer_self_view(server, customer):
    customer_id = customer["id"]
    catalog = model_catalog(server)
    usage_by_model = customer_dimension_usage(server.db_path, customer_id, "model")
    usage_by_model_map = {row["id"]: row for row in usage_by_model}
    allowed_models = []
    for model in catalog:
        if not customer_model_allowed(customer, model["id"]):
            continue
        model_view = dict(model)
        customer_usage = usage_by_model_map.get(model["id"], {})
        model_view["usage"] = {
            "usage_records": int(customer_usage.get("usage_records") or 0),
            "total_tokens": int(customer_usage.get("total_tokens") or 0),
            "estimated_cost": float(customer_usage.get("estimated_cost") or 0),
            "scope": "this_customer",
        }
        allowed_models.append(model_view)
    invoice = invoice_preview(server, customer_id)
    report = customer_reports(server)
    current_report = next((row for row in report if row.get("id") == customer_id), {})
    budget = customer_budget_status(server.db_path, customer)
    return {
        "object": "customer.gateway_profile",
        "mode": "mock" if server.mock_mode else "live",
        "customer": public_customer_view(customer),
        "budget": budget,
        "budget_state": customer_budget_state(budget),
        "models": allowed_models,
        "model_access": {
            "allowed_count": len(allowed_models),
            "blocked_count": max(0, len(catalog) - len(allowed_models)),
        },
        "request_summary": current_report.get("request_summary", {}),
        "usage_by_model": usage_by_model,
        "usage_by_provider": customer_dimension_usage(server.db_path, customer_id, "provider"),
        "recent_requests": customer_recent_requests(server.db_path, customer_id),
        "invoice_preview": invoice,
        "note": "This is a customer self-service view. It shows only this customer's access, usage, budget, and invoice preview. It does not expose provider secrets.",
    }


def customer_integration_guide(server, customer):
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    visible_models = [
        model_id
        for model_id in sorted(server.models.keys())
        if customer_model_allowed(customer, model_id)
    ]
    recommended_model = visible_models[0] if visible_models else "smart-fast"
    api_key_placeholder = "YOUR_GATEWAY_API_KEY"
    chat_body = {
        "model": recommended_model,
        "messages": [{"role": "user", "content": "Explain this gateway in one sentence."}],
        "stream": False,
    }
    stream_body = dict(chat_body)
    stream_body["stream"] = True
    return {
        "object": "customer.integration_guide",
        "mode": "mock" if server.mock_mode else "live",
        "base_url": base_url,
        "customer": {
            "id": customer.get("id"),
            "name": customer.get("name", customer.get("id")),
            "plan": customer.get("plan", "prototype"),
            "api_key_masked": mask_key(customer.get("api_key", "")),
            "default_policy": customer.get("default_policy"),
        },
        "models": {
            "recommended": recommended_model,
            "allowed": visible_models,
            "note": "Use the public model name. The gateway hides the upstream provider model.",
        },
        "quickstart_steps": [
            "Set GATEWAY_BASE_URL to the gateway URL.",
            "Set GATEWAY_API_KEY to the customer gateway key.",
            "Call /v1/models to confirm access.",
            "Call /v1/chat/completions with an OpenAI-compatible request body.",
            "Use stream=true only when the client can read Server-Sent Events.",
            "Ask the gateway owner for policy, budget, and invoice rules before production.",
        ],
        "code_examples": {
            "curl_list_models": (
                f"curl {base_url}/v1/models "
                f"-H 'Authorization: Bearer {api_key_placeholder}'"
            ),
            "curl_chat": (
                f"curl {base_url}/v1/chat/completions "
                f"-H 'Authorization: Bearer {api_key_placeholder}' "
                "-H 'Content-Type: application/json' "
                f"-d '{json.dumps(chat_body, separators=(',', ':'))}'"
            ),
            "curl_stream": (
                f"curl -N {base_url}/v1/chat/completions "
                f"-H 'Authorization: Bearer {api_key_placeholder}' "
                "-H 'Content-Type: application/json' "
                f"-d '{json.dumps(stream_body, separators=(',', ':'))}'"
            ),
            "python": (
                "import os, requests\n\n"
                f"base_url = os.getenv('GATEWAY_BASE_URL', '{base_url}')\n"
                "api_key = os.environ['GATEWAY_API_KEY']\n"
                "response = requests.post(\n"
                "    f'{base_url}/v1/chat/completions',\n"
                "    headers={'Authorization': f'Bearer {api_key}'},\n"
                f"    json={json.dumps(chat_body, indent=4)},\n"
                "    timeout=60,\n"
                ")\n"
                "print(response.json()['choices'][0]['message']['content'])"
            ),
            "javascript": (
                f"const baseUrl = process.env.GATEWAY_BASE_URL || '{base_url}';\n"
                "const apiKey = process.env.GATEWAY_API_KEY;\n"
                "const response = await fetch(`${baseUrl}/v1/chat/completions`, {\n"
                "  method: 'POST',\n"
                "  headers: {\n"
                "    Authorization: `Bearer ${apiKey}`,\n"
                "    'Content-Type': 'application/json'\n"
                "  },\n"
                f"  body: JSON.stringify({json.dumps(chat_body, indent=2)})\n"
                "});\n"
                "const data = await response.json();\n"
                "console.log(data.choices[0].message.content);"
            ),
        },
        "go_live_checklist": [
            "Replace local demo keys with real customer keys.",
            "Confirm allowed models and default routing policy.",
            "Confirm request, token, and cost budgets.",
            "Confirm whether prompts may contain sensitive data.",
            "Confirm logging, retention, and invoice rules.",
            "Run one mock request before switching to live provider mode.",
        ],
        "support_questions": [
            "Which model should this customer use first?",
            "Should this customer be allowed to use fallback providers?",
            "Who receives usage or budget alerts?",
            "What should happen when the monthly budget is reached?",
        ],
    }


def customer_sdk_starter(server, customer):
    guide = customer_integration_guide(server, customer)
    base_url = guide["base_url"]
    model = guide["models"]["recommended"]
    api_key_placeholder = "YOUR_GATEWAY_API_KEY"
    return {
        "object": "customer.sdk_starter",
        "mode": "mock" if server.mock_mode else "live",
        "base_url": base_url,
        "customer": guide["customer"],
        "recommended_model": model,
        "purpose": "Copy these starter files into a customer project to run the first gateway request safely.",
        "files": {
            ".env.example": (
                f"GATEWAY_BASE_URL={base_url}\n"
                f"GATEWAY_API_KEY={api_key_placeholder}\n"
                f"GATEWAY_MODEL={model}\n"
                "GATEWAY_STREAM=false\n"
            ),
            "gateway_client.py": (
                "import os, requests\n\n"
                "base_url = os.getenv('GATEWAY_BASE_URL')\n"
                "api_key = os.getenv('GATEWAY_API_KEY')\n"
                "model = os.getenv('GATEWAY_MODEL', 'smart-fast')\n\n"
                "response = requests.post(\n"
                "    f'{base_url}/v1/chat/completions',\n"
                "    headers={'Authorization': f'Bearer {api_key}'},\n"
                "    json={\n"
                "        'model': model,\n"
                "        'messages': [{'role': 'user', 'content': 'Explain this gateway in one sentence.'}],\n"
                "        'stream': False,\n"
                "    },\n"
                "    timeout=60,\n"
                ")\n"
                "response.raise_for_status()\n"
                "print(response.json()['choices'][0]['message']['content'])\n"
            ),
            "gateway-client.mjs": (
                "const baseUrl = process.env.GATEWAY_BASE_URL;\n"
                "const apiKey = process.env.GATEWAY_API_KEY;\n"
                "const model = process.env.GATEWAY_MODEL || 'smart-fast';\n\n"
                "const response = await fetch(`${baseUrl}/v1/chat/completions`, {\n"
                "  method: 'POST',\n"
                "  headers: {\n"
                "    Authorization: `Bearer ${apiKey}`,\n"
                "    'Content-Type': 'application/json'\n"
                "  },\n"
                "  body: JSON.stringify({\n"
                "    model,\n"
                "    messages: [{ role: 'user', content: 'Explain this gateway in one sentence.' }],\n"
                "    stream: false\n"
                "  })\n"
                "});\n\n"
                "if (!response.ok) throw new Error(await response.text());\n"
                "const data = await response.json();\n"
                "console.log(data.choices[0].message.content);\n"
            ),
        },
        "first_run_commands": [
            "Copy .env.example to .env and set GATEWAY_API_KEY.",
            "python3 gateway_client.py",
            "node --env-file=.env gateway-client.mjs",
            "curl $GATEWAY_BASE_URL/v1/models -H \"Authorization: Bearer $GATEWAY_API_KEY\"",
        ],
        "common_errors": [
            {"code": "missing_auth", "meaning": "Authorization header is missing.", "fix": "Send Authorization: Bearer YOUR_GATEWAY_API_KEY."},
            {"code": "invalid_api_key", "meaning": "The customer key is wrong or disabled.", "fix": "Ask the gateway owner to confirm or rotate the customer key."},
            {"code": "model_not_allowed", "meaning": "The customer cannot use that public model.", "fix": "Use an allowed model from /v1/models or ask for access."},
            {"code": "token_budget_exceeded", "meaning": "The customer token budget is reached.", "fix": "Review /v1/gateway/me and ask the gateway owner to adjust budget."},
            {"code": "route_failed", "meaning": "No route candidate could serve the request.", "fix": "Ask support to check route preview, provider health, and request detail."},
        ],
        "handoff_checklist": [
            "Customer can list models.",
            "Customer can run one non-streaming chat request.",
            "Customer saves gateway.request_id for support.",
            "Customer knows whether stream=true is supported by their client.",
            "Customer knows who to contact for model access, budget, or provider questions.",
        ],
        "safety_note": "Do not put provider API keys in customer code. The customer only uses the gateway API key.",
    }


def provider_status(server):
    rows = []
    request_by_provider = {
        row["id"]: row
        for row in request_grouped_by(server.db_path, "provider")
        if row.get("id") is not None
    }
    usage_by_provider = {
        row["id"]: row
        for row in usage_grouped_by(server.db_path, "provider")
        if row.get("id") is not None
    }
    for provider_id, provider in sorted(server.providers.items()):
        models = [
            public_name
            for public_name, model in sorted(server.models.items())
            if model.get("provider") == provider_id
        ]
        configured = bool(os.getenv(provider.get("api_key_env", "")))
        byok_customers = [
            customer["id"]
            for customer in server.customers_by_key.values()
            if provider_id in customer.get("provider_api_keys", {})
        ]
        rows.append(
            {
                "id": provider_id,
                "name": provider.get("name", provider_id),
                "type": provider.get("type", "openai_compatible"),
                "base_url": provider.get("base_url"),
                "models": models,
                "api_key_env": provider.get("api_key_env"),
                "server_key_configured": configured,
                "byok_customers": byok_customers,
                "requests": request_by_provider.get(provider_id, {}),
                "usage": usage_by_provider.get(provider_id, {}),
            }
        )
    return rows


def provider_health(server):
    rows = []
    for provider_id, provider in sorted(server.providers.items()):
        models = [
            public_name
            for public_name, model in sorted(server.models.items())
            if model.get("provider") == provider_id
        ]
        env_name = provider.get("api_key_env")
        server_key_configured = bool(env_name and os.getenv(env_name))
        byok_customers = [
            customer["id"]
            for customer in server.customers_by_key.values()
            if provider_id in customer.get("provider_api_keys", {})
        ]
        byok_ready_customers = [
            customer["id"]
            for customer in server.customers_by_key.values()
            if resolve_secret(customer.get("provider_api_keys", {}).get(provider_id))
        ]
        has_key_path = server_key_configured or bool(byok_ready_customers)
        metrics = recent_provider_metrics(server.db_path, provider_id)
        status = "ready"
        reason = "Provider has an enabled model and a usable key path."
        if server.mock_mode and not has_key_path:
            status = "ready_mock"
            reason = "Mock mode can demonstrate this provider without a paid provider key."
        if not models:
            status = "not_ready"
            reason = "No enabled model is routed to this provider."
        elif not server.mock_mode and not has_key_path:
            status = "not_ready"
            reason = "Live mode needs a server provider key or a customer BYOK key."
        elif metrics["sample_size"] >= 3 and metrics["error_rate"] >= 0.5:
            status = "degraded"
            reason = "Recent requests show a high provider error rate."
        rows.append(
            {
                "id": provider_id,
                "name": provider.get("name", provider_id),
                "type": provider.get("type", "openai_compatible"),
                "status": status,
                "reason": reason,
                "models": models,
                "live_ready": bool(models and has_key_path),
                "mock_ready": bool(models),
                "api_key_env": env_name,
                "server_key_configured": server_key_configured,
                "byok_customers": byok_customers,
                "byok_ready_customers": byok_ready_customers,
                "recent": metrics,
            }
        )
    return rows


def provider_contracts(server):
    health_by_provider = {provider["id"]: provider for provider in provider_health(server)}
    configured = []
    for provider_id, provider in sorted(server.providers.items()):
        provider_type = provider.get("type", "openai_compatible")
        contract = PROVIDER_TYPE_CONTRACTS.get(provider_type, {})
        configured.append(
            {
                "id": provider_id,
                "name": provider.get("name", provider_id),
                "type": provider_type,
                "adapter_status": contract.get("adapter_status", "unknown"),
                "contract_label": contract.get("label", provider_type),
                "base_url": provider.get("base_url"),
                "api_key_env": provider.get("api_key_env"),
                "api_version": provider.get("api_version"),
                "models": [
                    public_name
                    for public_name, model in sorted(server.models.items())
                    if model.get("provider") == provider_id
                ],
                "health": health_by_provider.get(provider_id, {}),
                "contract_gaps": provider_contract_gaps(provider_type),
            }
        )
    return {
        "object": "gateway.provider_contracts",
        "mode": "mock" if server.mock_mode else "live",
        "summary": {
            "configured_providers": len(configured),
            "implemented_adapter_types": [
                key
                for key, contract in sorted(PROVIDER_TYPE_CONTRACTS.items())
                if contract.get("adapter_status") == "implemented"
            ],
            "scaffolded_adapter_types": [
                key
                for key, contract in sorted(PROVIDER_TYPE_CONTRACTS.items())
                if contract.get("adapter_status") == "scaffolded"
            ],
            "planned_adapter_types": [
                key
                for key, contract in sorted(PROVIDER_TYPE_CONTRACTS.items())
                if contract.get("adapter_status") == "planned"
            ],
        },
        "configured_providers": configured,
        "provider_type_contracts": [
            {"type": provider_type, **contract}
            for provider_type, contract in sorted(PROVIDER_TYPE_CONTRACTS.items())
        ],
        "plain_english": [
            "A normal API gateway can forward HTTP requests.",
            "An AI model gateway also normalizes model names, request shapes, response shapes, streams, tool calls, usage, errors, fallbacks, and customer policy.",
            "Even OpenAI-compatible providers need contract tests because compatible does not always mean identical.",
        ],
        "next_step": "Before enabling a new provider, add it disabled in model_registry.json, map its contract, run mock tests, then run a small live test with limits.",
    }


def provider_contract_gaps(provider_type):
    if provider_type == "openai_compatible":
        return [
            "Confirm model names and pricing.",
            "Test stream chunk shape.",
            "Test tool call pass-through.",
            "Normalize provider-specific errors.",
        ]
    if provider_type == "anthropic":
        return [
            "Harden live streaming normalization.",
            "Test tool schema conversion.",
            "Normalize stop reasons and usage fields.",
            "Add live contract tests before enabling routes.",
        ]
    return [
        "Collect official provider documentation.",
        "Confirm auth, endpoint path, request shape, response shape, streaming, tools, and usage.",
        "Implement an adapter and contract tests.",
    ]


def model_catalog(server):
    provider_health_by_id = {provider["id"]: provider for provider in provider_health(server)}
    request_by_model = {
        row["id"]: row
        for row in request_grouped_by(server.db_path, "model")
        if row.get("id") is not None
    }
    usage_by_model_map = {
        row["id"]: row
        for row in usage_grouped_by(server.db_path, "model")
        if row.get("id") is not None
    }
    rows = []
    for public_name, model in sorted(server.models.items()):
        provider_id = model.get("provider")
        provider = server.providers.get(provider_id, {})
        pricing = model.get("pricing", {})
        fallback_models = model.get("fallback_models", [])
        route_chain = [public_name] + fallback_models
        missing_fallbacks = [name for name in fallback_models if name not in server.models]
        health = provider_health_by_id.get(provider_id, {})
        requests = request_by_model.get(public_name, {})
        usage = usage_by_model_map.get(public_name, {})
        rows.append(
            {
                "id": public_name,
                "provider": provider_id,
                "provider_name": provider.get("name", provider_id),
                "provider_type": provider.get("type", "openai_compatible"),
                "provider_status": health.get("status", "unknown"),
                "upstream_model": model.get("upstream_model"),
                "capabilities": model.get("capabilities", []),
                "fallback_models": fallback_models,
                "route_chain": route_chain,
                "missing_fallbacks": missing_fallbacks,
                "pricing": {
                    "prompt_per_1k": float(pricing.get("prompt_per_1k", 0)),
                    "completion_per_1k": float(pricing.get("completion_per_1k", 0)),
                },
                "usage": {
                    "requests": int(requests.get("requests") or 0),
                    "errors": int(requests.get("errors") or 0),
                    "avg_latency_ms": round(float(requests.get("avg_latency_ms") or 0), 2),
                    "total_tokens": int(usage.get("total_tokens") or 0),
                    "estimated_cost": float(usage.get("estimated_cost") or 0),
                },
                "routing_note": (
                    f"{public_name} routes to {model.get('upstream_model')} on {provider_id}"
                    + (f", then can fallback to {', '.join(fallback_models)}" if fallback_models else "")
                ),
            }
        )
    return rows


def customer_by_id(server, customer_id):
    for customer in server.customers_by_key.values():
        if customer.get("id") == customer_id:
            return customer
    return None


def customer_model_allowed(customer, public_model):
    allowed = customer.get("allowed_models", ["*"])
    return "*" in allowed or public_model in allowed


def requested_allowed_providers(payload):
    if "gateway_allowed_providers" not in payload:
        return None
    providers = payload.get("gateway_allowed_providers")
    if not isinstance(providers, list) or not all(isinstance(provider, str) for provider in providers):
        raise GatewayError(
            "gateway_allowed_providers must be a list of provider ids.",
            "invalid_provider_policy",
            400,
        )
    normalized = []
    for provider in providers:
        provider = provider.strip()
        if provider and provider not in normalized:
            normalized.append(provider)
    if not normalized:
        raise GatewayError(
            "gateway_allowed_providers must include at least one provider id.",
            "invalid_provider_policy",
            400,
        )
    return normalized


POLICY_PRESETS = {
    "balanced": {
        "description": "Use registry order with normal fallback.",
        "controls": {
            "gateway_route_strategy": "registry",
        },
    },
    "lowest_cost": {
        "description": "Prefer the lowest estimated route cost.",
        "controls": {
            "gateway_route_strategy": "lowest_cost",
        },
    },
    "fastest": {
        "description": "Prefer the lowest recent average latency.",
        "controls": {
            "gateway_route_strategy": "fastest",
        },
    },
    "tool_ready": {
        "description": "Require tool calling support and prefer healthy providers.",
        "controls": {
            "gateway_route_strategy": "healthiest",
            "gateway_required_capabilities": ["tools"],
        },
    },
}


def policy_presets_view():
    return {
        name: {
            "description": preset["description"],
            "controls": dict(preset["controls"]),
        }
        for name, preset in sorted(POLICY_PRESETS.items())
    }


def validate_policy_name(policy_name, field_name="gateway_policy"):
    if policy_name in {None, ""}:
        return None
    if not isinstance(policy_name, str):
        raise GatewayError(f"{field_name} must be a string.", "invalid_gateway_policy", 400)
    policy_name = policy_name.strip()
    if policy_name not in POLICY_PRESETS:
        raise GatewayError(
            f"Unknown {field_name}.",
            "unknown_gateway_policy",
            400,
            {"allowed": sorted(POLICY_PRESETS.keys())},
        )
    return policy_name


def apply_customer_default_policy(payload, customer):
    merged = dict(payload)
    if "gateway_policy" not in merged:
        default_policy = validate_policy_name(customer.get("default_policy"), "default_policy")
        if default_policy:
            merged["gateway_policy"] = default_policy
            merged["gateway_policy_source"] = "customer_default"
    return merged


def apply_policy_preset(payload):
    policy_name = payload.get("gateway_policy")
    if policy_name is None:
        payload = dict(payload)
        payload["gateway_policy_applied"] = None
        return payload
    policy_name = validate_policy_name(policy_name, "gateway_policy")
    merged = dict(payload)
    applied_controls = {}
    for key, value in POLICY_PRESETS[policy_name]["controls"].items():
        if key not in merged:
            merged[key] = value
            applied_controls[key] = value
    merged["gateway_policy_applied"] = {
        "name": policy_name,
        "description": POLICY_PRESETS[policy_name]["description"],
        "controls": dict(POLICY_PRESETS[policy_name]["controls"]),
        "applied_controls": applied_controls,
        "explicit_controls_kept": [
            key
            for key in POLICY_PRESETS[policy_name]["controls"]
            if key in payload
        ],
    }
    return merged


def requested_capabilities(payload):
    capabilities = ["chat"]
    if payload.get("stream"):
        capabilities.append("streaming")
    if payload.get("tools"):
        capabilities.append("tools")
    if "gateway_required_capabilities" in payload:
        requested = payload.get("gateway_required_capabilities")
        if not isinstance(requested, list) or not all(isinstance(name, str) for name in requested):
            raise GatewayError(
                "gateway_required_capabilities must be a list of capability names.",
                "invalid_capability_policy",
                400,
            )
        for capability in requested:
            capability = capability.strip()
            if capability and capability not in capabilities:
                capabilities.append(capability)
    return capabilities


def requested_route_strategy(payload):
    strategy = payload.get("gateway_route_strategy", "registry")
    if not isinstance(strategy, str):
        raise GatewayError(
            "gateway_route_strategy must be a string.",
            "invalid_route_strategy",
            400,
        )
    strategy = strategy.strip() or "registry"
    allowed = {"registry", "lowest_cost", "fastest", "healthiest"}
    if strategy not in allowed:
        raise GatewayError(
            "gateway_route_strategy must be registry, lowest_cost, fastest, or healthiest.",
            "invalid_route_strategy",
            400,
            {"allowed": sorted(allowed)},
        )
    return strategy


def route_cost_score(model_config):
    pricing = model_config.get("pricing", {})
    return float(pricing.get("prompt_per_1k", 0)) + float(pricing.get("completion_per_1k", 0))


def route_latency_scores(path):
    return {
        row["id"]: float(row.get("avg_latency_ms") or 0)
        for row in request_grouped_by(path, "model")
        if row.get("id") is not None
    }


def route_health_scores(server):
    order = {"ready": 0, "ready_mock": 1, "degraded": 2, "not_ready": 3}
    return {
        row["id"]: order.get(row.get("status"), 9)
        for row in provider_health(server)
    }


def score_route_candidates(server, candidates):
    latency_by_model = route_latency_scores(server.db_path)
    health_by_provider = route_health_scores(server)
    scores = []
    for index, name in enumerate(candidates):
        model = server.models[name]
        provider_id = model.get("provider")
        latency = latency_by_model.get(name)
        scores.append(
            {
                "model": name,
                "provider": provider_id,
                "registry_index": index,
                "cost_score": route_cost_score(model),
                "latency_ms": latency,
                "health_score": health_by_provider.get(provider_id, 9),
            }
        )
    return scores


def apply_route_strategy(server, candidates, strategy):
    scores = score_route_candidates(server, candidates)
    if strategy == "registry":
        return candidates, scores
    score_by_model = {score["model"]: score for score in scores}
    if strategy == "lowest_cost":
        ordered = sorted(
            candidates,
            key=lambda name: (
                score_by_model[name]["cost_score"],
                score_by_model[name]["health_score"],
                score_by_model[name]["registry_index"],
            ),
        )
    elif strategy == "fastest":
        ordered = sorted(
            candidates,
            key=lambda name: (
                score_by_model[name]["latency_ms"] is None,
                score_by_model[name]["latency_ms"] if score_by_model[name]["latency_ms"] is not None else 999999999,
                score_by_model[name]["health_score"],
                score_by_model[name]["registry_index"],
            ),
        )
    elif strategy == "healthiest":
        ordered = sorted(
            candidates,
            key=lambda name: (
                score_by_model[name]["health_score"],
                score_by_model[name]["cost_score"],
                score_by_model[name]["registry_index"],
            ),
        )
    else:
        ordered = candidates
    ordered_scores = [score_by_model[name] for name in ordered]
    return ordered, ordered_scores


def build_candidate_models(server, customer, public_model, payload):
    payload = apply_customer_default_policy(payload, customer)
    payload = apply_policy_preset(payload)
    model = server.models.get(public_model)
    if not model:
        raise GatewayError(f"Unknown model: {public_model}.", "unknown_model", 404)
    if not customer_model_allowed(customer, public_model):
        raise GatewayError(f"Customer is not allowed to use model: {public_model}.", "model_not_allowed", 403)
    routing_policy = {
        "source": "model_registry",
        "fallback_enabled": not bool(payload.get("gateway_disable_fallback")),
        "requested_fallback_models": None,
        "allowed_providers": requested_allowed_providers(payload),
        "required_capabilities": requested_capabilities(payload),
        "route_strategy": requested_route_strategy(payload),
        "gateway_policy": payload.get("gateway_policy_applied"),
        "gateway_policy_source": payload.get("gateway_policy_source") or ("request" if payload.get("gateway_policy_applied") else None),
    }
    fallback_models = model.get("fallback_models", [])
    requested_fallbacks = False
    if "gateway_fallback_models" in payload:
        requested = payload.get("gateway_fallback_models")
        if not isinstance(requested, list) or not all(isinstance(name, str) for name in requested):
            raise GatewayError(
                "gateway_fallback_models must be a list of model names.",
                "invalid_routing_policy",
                400,
            )
        fallback_models = requested
        requested_fallbacks = True
        routing_policy["source"] = "request"
        routing_policy["requested_fallback_models"] = requested
    if not routing_policy["fallback_enabled"]:
        fallback_models = []
        routing_policy["source"] = "request"
    if "gateway_required_capabilities" in payload:
        routing_policy["source"] = "request"
    if "gateway_route_strategy" in payload:
        routing_policy["source"] = "request"
    if payload.get("gateway_policy_applied"):
        routing_policy["source"] = "policy"

    candidates = [public_model]
    for name in fallback_models:
        if name == public_model or name in candidates:
            continue
        if name not in server.models:
            raise GatewayError(f"Unknown fallback model: {name}.", "unknown_fallback_model", 404)
        if requested_fallbacks and not customer_model_allowed(customer, name):
            raise GatewayError(f"Customer is not allowed to use model: {name}.", "model_not_allowed", 403)
        candidates.append(name)

    allowed_providers = routing_policy["allowed_providers"]
    if allowed_providers is not None:
        routing_policy["source"] = "request"
        candidates = [
            name
            for name in candidates
            if server.models[name].get("provider") in allowed_providers
        ]
        if not candidates:
            raise GatewayError(
                "No route candidate matches gateway_allowed_providers.",
                "no_allowed_provider_route",
                400,
                {"allowed_providers": allowed_providers, "requested_model": public_model},
            )
    required_capabilities = routing_policy["required_capabilities"]
    candidates = [
        name
        for name in candidates
        if set(required_capabilities).issubset(set(server.models[name].get("capabilities", [])))
    ]
    if not candidates:
        raise GatewayError(
            "No route candidate supports the required capabilities.",
            "no_capability_route",
            400,
            {"required_capabilities": required_capabilities, "requested_model": public_model},
        )
    routing_policy["candidates_before_strategy"] = list(candidates)
    candidates, scores = apply_route_strategy(server, candidates, routing_policy["route_strategy"])
    routing_policy["candidate_scores"] = scores
    routing_policy["candidates"] = candidates
    return candidates, routing_policy


def route_decision(public_model, model_config, routing_policy, customer_id=None, fallback_attempts=None):
    fallback_attempts = fallback_attempts or []
    selected_public_model = model_config.get("id") or public_model
    selected_provider = model_config.get("provider")
    selected_upstream = model_config.get("upstream_model")
    required_capabilities = routing_policy.get("required_capabilities") or ["chat"]
    allowed_providers = routing_policy.get("allowed_providers") or ["any"]
    route_strategy = routing_policy.get("route_strategy") or "registry"
    gateway_policy = routing_policy.get("gateway_policy")
    gateway_policy_source = routing_policy.get("gateway_policy_source")
    fallback_enabled = bool(routing_policy.get("fallback_enabled"))
    fallback_candidates = list(routing_policy.get("candidates", []))[1:]
    reasons = [
        f"Customer requested public model {public_model}.",
        f"The selected route is {selected_public_model} on provider {selected_provider}.",
        f"The provider model is {selected_upstream}.",
        f"Route strategy: {route_strategy}.",
        f"Required capabilities: {', '.join(required_capabilities)}.",
        f"Allowed providers: {', '.join(allowed_providers)}.",
    ]
    if fallback_attempts:
        reasons.append(f"Previous route attempts failed: {len(fallback_attempts)}.")
    if gateway_policy:
        reasons.append(f"Gateway policy preset applied: {gateway_policy.get('name')} from {gateway_policy_source}.")
    if fallback_enabled and fallback_candidates:
        reasons.append(f"Fallback candidates were available: {', '.join(fallback_candidates)}.")
    elif fallback_enabled:
        reasons.append("Fallback was enabled, but no extra fallback candidate was needed.")
    else:
        reasons.append("Fallback was disabled for this request.")
    return {
        "summary": f"{public_model} -> {selected_upstream} via {selected_provider}",
        "customer_id": customer_id,
        "requested_model": public_model,
        "selected_public_model": selected_public_model,
        "resolved_model": selected_upstream,
        "provider": selected_provider,
        "policy_source": routing_policy.get("source"),
        "gateway_policy": gateway_policy,
        "gateway_policy_source": gateway_policy_source,
        "route_strategy": route_strategy,
        "candidate_scores": routing_policy.get("candidate_scores", []),
        "fallback_enabled": fallback_enabled,
        "fallback_candidates": fallback_candidates,
        "fallback_attempts": fallback_attempts,
        "allowed_providers": allowed_providers,
        "required_capabilities": required_capabilities,
        "reasons": reasons,
    }


def route_preview(server, payload):
    public_model = payload.get("model")
    if not public_model:
        raise GatewayError("Missing required field: model.", "missing_model", 400)
    customer_id = payload.get("customer_id") or "dev"
    customer = customer_by_id(server, customer_id)
    if not customer:
        raise GatewayError(f"Unknown customer: {customer_id}.", "unknown_customer", 404)
    budget = customer_budget_status(server.db_path, customer)
    budget_state = customer_budget_state(budget)
    candidates, routing_policy = build_candidate_models(server, customer, public_model, payload)
    provider_health_by_id = {provider["id"]: provider for provider in provider_health(server)}
    routes = []
    for index, candidate_name in enumerate(candidates):
        model = server.models[candidate_name]
        provider_id = model.get("provider")
        provider = server.providers.get(provider_id, {})
        health = provider_health_by_id.get(provider_id, {})
        routes.append(
            {
                "index": index,
                "public_model": candidate_name,
                "upstream_model": model.get("upstream_model"),
                "provider": provider_id,
                "provider_name": provider.get("name", provider_id),
                "provider_status": health.get("status", "unknown"),
                "capabilities": model.get("capabilities", []),
                "capability_match": True,
                "pricing": model.get("pricing", {}),
                "reason": "primary route" if index == 0 else "fallback route",
            }
        )
    allowed = budget_state != "blocked"
    primary_model = server.models[candidates[0]]
    return {
        "mode": "mock" if server.mock_mode else "live",
        "customer": public_customer_view(customer),
        "model": public_model,
        "allowed": allowed,
        "blocked_reason": "customer_budget_blocked" if not allowed else None,
        "budget": budget,
        "budget_state": budget_state,
        "routing_policy": routing_policy,
        "route_decision": route_decision(public_model, primary_model, routing_policy, customer_id),
        "routes": routes,
        "route_trace": [
            f"Customer = {customer_id}",
            f"Requested model = {public_model}",
            f"Access allowed = {customer_model_allowed(customer, public_model)}",
            f"Budget state = {budget_state}",
            f"Allowed providers = {', '.join(routing_policy.get('allowed_providers') or ['any'])}",
            f"Required capabilities = {', '.join(routing_policy.get('required_capabilities') or ['chat'])}",
            f"Route candidates = {', '.join(candidates)}",
        ],
    }


def cost_estimate(server, payload):
    public_model = payload.get("model")
    if not public_model:
        raise GatewayError("Missing required field: model.", "missing_model", 400)
    customer_id = payload.get("customer_id") or "dev"
    customer = customer_by_id(server, customer_id)
    if not customer:
        raise GatewayError(f"Unknown customer: {customer_id}.", "unknown_customer", 404)
    candidates, routing_policy = build_candidate_models(server, customer, public_model, payload)
    primary_model = server.models[candidates[0]]
    prompt_tokens = estimate_tokens(payload.get("messages", []), payload.get("prompt", ""))
    completion_tokens = bounded_int(payload.get("max_tokens"), 256, minimum=1, maximum=200000)
    pricing = primary_model.get("pricing", {})
    prompt_cost = prompt_tokens / 1000 * float(pricing.get("prompt_per_1k", 0))
    completion_cost = completion_tokens / 1000 * float(pricing.get("completion_per_1k", 0))
    estimated_cost = round(prompt_cost + completion_cost, 8)
    budget = customer_budget_status(server.db_path, customer)
    remaining_tokens = budget.get("remaining_tokens")
    remaining_cost = budget.get("remaining_cost")
    total_tokens = prompt_tokens + completion_tokens
    after_tokens = None if remaining_tokens is None else remaining_tokens - total_tokens
    after_cost = None if remaining_cost is None else round(remaining_cost - estimated_cost, 8)
    blocked_after_estimate = bool(
        (after_tokens is not None and after_tokens < 0)
        or (after_cost is not None and after_cost < 0)
    )
    return {
        "mode": "mock" if server.mock_mode else "live",
        "customer": public_customer_view(customer),
        "model": public_model,
        "resolved_model": primary_model.get("upstream_model"),
        "provider": primary_model.get("provider"),
        "routing_policy": routing_policy,
        "estimate": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "prompt_cost": round(prompt_cost, 8),
            "completion_cost": round(completion_cost, 8),
            "estimated_cost": estimated_cost,
            "pricing": {
                "prompt_per_1k": float(pricing.get("prompt_per_1k", 0)),
                "completion_per_1k": float(pricing.get("completion_per_1k", 0)),
            },
        },
        "budget": budget,
        "budget_after_estimate": {
            "remaining_tokens": after_tokens,
            "remaining_cost": after_cost,
            "would_exceed_budget": blocked_after_estimate,
        },
        "note": "This is a local estimate. Real provider token usage can differ.",
    }


def key_issue_preview(server, payload):
    customer_id = (payload.get("customer_id") or "").strip()
    if not customer_id:
        raise GatewayError("Missing required field: customer_id.", "missing_customer_id", 400)
    if customer_by_id(server, customer_id):
        raise GatewayError(f"Customer already exists: {customer_id}.", "customer_already_exists", 409)

    allowed_models = payload.get("allowed_models", ["smart-fast"])
    if not isinstance(allowed_models, list) or not all(isinstance(name, str) for name in allowed_models):
        raise GatewayError("allowed_models must be a list of model names.", "invalid_allowed_models", 400)
    allowed_models = [name.strip() for name in allowed_models if name.strip()]
    if not allowed_models:
        raise GatewayError("allowed_models must include at least one model or '*'.", "invalid_allowed_models", 400)
    unknown_models = [
        name
        for name in allowed_models
        if name != "*" and name not in server.models
    ]
    if unknown_models:
        raise GatewayError(
            "allowed_models includes unknown model names.",
            "unknown_allowed_model",
            404,
            {"unknown_models": unknown_models},
        )

    provider_api_keys = payload.get("provider_api_keys", {})
    if provider_api_keys is None:
        provider_api_keys = {}
    if not isinstance(provider_api_keys, dict):
        raise GatewayError("provider_api_keys must be an object.", "invalid_provider_api_keys", 400)
    unknown_providers = [
        provider_id
        for provider_id in provider_api_keys
        if provider_id not in server.providers
    ]
    if unknown_providers:
        raise GatewayError(
            "provider_api_keys includes unknown providers.",
            "unknown_provider",
            404,
            {"unknown_providers": unknown_providers},
        )
    default_policy = validate_policy_name(payload.get("default_policy"), "default_policy")

    requested_api_key = payload.get("api_key")
    if requested_api_key and requested_api_key in server.customers_by_key:
        raise GatewayError("Gateway API key already belongs to an existing customer.", "api_key_already_exists", 409)
    api_key = requested_api_key or "aisr_" + secrets.token_urlsafe(24)
    while api_key in server.customers_by_key:
        api_key = "aisr_" + secrets.token_urlsafe(24)
    try:
        cost_budget = float(payload.get("cost_budget", 1.0))
    except (TypeError, ValueError):
        raise GatewayError("cost_budget must be a number.", "invalid_cost_budget", 400)

    customer_config = {
        "id": customer_id,
        "name": payload.get("name") or customer_id,
        "plan": payload.get("plan") or "prototype",
        "api_key": api_key,
        "request_limit": bounded_int(payload.get("request_limit"), 60, minimum=1, maximum=100000),
        "limit_window_seconds": bounded_int(payload.get("limit_window_seconds"), 60, minimum=1, maximum=86400),
        "token_budget": bounded_int(payload.get("token_budget"), 10000, minimum=1, maximum=1000000000),
        "cost_budget": cost_budget,
        "allowed_models": allowed_models,
        "enabled": True,
    }
    if default_policy:
        customer_config["default_policy"] = default_policy
    if provider_api_keys:
        customer_config["provider_api_keys"] = provider_api_keys

    public_config = dict(customer_config)
    public_config["api_key"] = mask_key(api_key)
    if "provider_api_keys" in public_config:
        public_config["provider_api_keys"] = {
            provider_id: mask_key(value) if value and not str(value).startswith("env:") else value
            for provider_id, value in provider_api_keys.items()
        }

    return {
        "mode": "mock" if server.mock_mode else "live",
        "customer": public_config,
        "generated_api_key": api_key,
        "generated_api_key_masked": mask_key(api_key),
        "api_key_hash": key_hash(api_key),
        "config_snippet": customer_config,
        "next_steps": [
            "Give the generated API key to the customer only once.",
            "Store config_snippet in customer_keys.json or a production customer database.",
            "Use /v1/gateway/route-preview to confirm model access before the first live request.",
            "Use /v1/gateway/customer-reports to review usage after testing.",
        ],
        "note": "This preview does not persist the customer. It is a safe demo for key issuing workflow.",
    }


def load_customer_config(path):
    data = read_json(path, {"customers": []})
    customers = data.get("customers", [])
    if not isinstance(customers, list):
        raise GatewayError("customer_keys.json must contain a customers list.", "invalid_customer_config", 500)
    return data


def reload_customer_runtime(server):
    server.customers_by_key = load_customers(server.customers_path)
    sync_customers_to_db(server.db_path, server.customers_by_key)


def customer_key_package(customer):
    public_customer = public_customer_view(customer)
    api_key = customer.get("api_key", "")
    public_customer["api_key_masked"] = mask_key(api_key)
    public_customer["api_key_hash"] = key_hash(api_key) if api_key else ""
    public_customer["enabled"] = bool(customer.get("enabled", True))
    return public_customer


def write_audit_event(server, action, target_type, target_id, status="success", details=None):
    safe_details = details or {}
    record = {
        "created": now_unix(),
        "actor": "admin",
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "status": status,
        "details": safe_details,
    }
    append_jsonl(server.audit_log_path, record)
    insert_audit_db(server.db_path, record)
    return record


def customer_create(server, payload):
    config = load_customer_config(server.customers_path)
    existing_ids = {customer.get("id") for customer in config.get("customers", [])}
    customer_id = (payload.get("customer_id") or "").strip()
    if customer_id in existing_ids:
        raise GatewayError(f"Customer already exists: {customer_id}.", "customer_already_exists", 409)

    preview = key_issue_preview(server, payload)
    customer_config = preview["config_snippet"]
    config.setdefault("customers", []).append(customer_config)
    write_json(server.customers_path, config)
    reload_customer_runtime(server)
    write_audit_event(
        server,
        "customer.created",
        "customer",
        customer_config["id"],
        details={
            "plan": customer_config.get("plan"),
            "default_policy": customer_config.get("default_policy"),
            "allowed_models": customer_config.get("allowed_models", []),
            "request_limit": customer_config.get("request_limit"),
            "token_budget": customer_config.get("token_budget"),
            "cost_budget": customer_config.get("cost_budget"),
            "api_key_hash": key_hash(customer_config["api_key"]),
            "api_key_masked": mask_key(customer_config["api_key"]),
        },
    )
    return {
        "object": "customer.created",
        "customer": customer_key_package(customer_config),
        "generated_api_key": customer_config["api_key"],
        "generated_api_key_masked": mask_key(customer_config["api_key"]),
        "next_steps": [
            "Give the generated API key to the customer only once.",
            "Ask the customer to call /v1/gateway/me to confirm access.",
            "Use /v1/gateway/route-preview before the first live request.",
        ],
        "note": "This prototype stores the customer in customer_keys.json. Production should use a database and secret manager.",
    }


def find_customer_config(config, customer_id):
    for index, customer in enumerate(config.get("customers", [])):
        if customer.get("id") == customer_id:
            return index, customer
    return None, None


def customer_disable(server, payload):
    customer_id = (payload.get("customer_id") or "").strip()
    if not customer_id:
        raise GatewayError("Missing required field: customer_id.", "missing_customer_id", 400)
    config = load_customer_config(server.customers_path)
    index, customer = find_customer_config(config, customer_id)
    if customer is None:
        raise GatewayError(f"Unknown customer: {customer_id}.", "unknown_customer", 404)
    customer = dict(customer)
    customer["enabled"] = False
    config["customers"][index] = customer
    write_json(server.customers_path, config)
    reload_customer_runtime(server)
    write_audit_event(
        server,
        "customer.disabled",
        "customer",
        customer_id,
        details={
            "api_key_hash": key_hash(customer.get("api_key", "")) if customer.get("api_key") else "",
            "api_key_masked": mask_key(customer.get("api_key", "")),
        },
    )
    return {
        "object": "customer.disabled",
        "customer": customer_key_package(customer),
        "note": "The customer key is disabled in customer_keys.json and removed from the active runtime map.",
    }


def customer_rotate_key(server, payload):
    customer_id = (payload.get("customer_id") or "").strip()
    if not customer_id:
        raise GatewayError("Missing required field: customer_id.", "missing_customer_id", 400)
    config = load_customer_config(server.customers_path)
    index, customer = find_customer_config(config, customer_id)
    if customer is None:
        raise GatewayError(f"Unknown customer: {customer_id}.", "unknown_customer", 404)
    old_key = customer.get("api_key", "")
    requested_api_key = payload.get("api_key")
    api_key = requested_api_key or "aisr_" + secrets.token_urlsafe(24)
    existing_keys = {
        item.get("api_key")
        for item in config.get("customers", [])
        if item.get("id") != customer_id
    }
    if api_key in existing_keys:
        raise GatewayError("Gateway API key already belongs to another customer.", "api_key_already_exists", 409)
    while api_key in existing_keys:
        api_key = "aisr_" + secrets.token_urlsafe(24)

    customer = dict(customer)
    customer["api_key"] = api_key
    customer["enabled"] = bool(payload.get("enabled", customer.get("enabled", True)))
    config["customers"][index] = customer
    write_json(server.customers_path, config)
    reload_customer_runtime(server)
    write_audit_event(
        server,
        "customer.key_rotated",
        "customer",
        customer_id,
        details={
            "old_api_key_hash": key_hash(old_key) if old_key else "",
            "old_api_key_masked": mask_key(old_key),
            "new_api_key_hash": key_hash(api_key),
            "new_api_key_masked": mask_key(api_key),
        },
    )
    return {
        "object": "customer.key_rotated",
        "customer": customer_key_package(customer),
        "generated_api_key": api_key,
        "generated_api_key_masked": mask_key(api_key),
        "old_api_key_masked": mask_key(old_key),
        "old_api_key_hash": key_hash(old_key) if old_key else "",
        "next_steps": [
            "Give the new API key to the customer only once.",
            "Remove the old key from the customer's application config.",
            "Call /v1/gateway/me with the new key to confirm access.",
        ],
    }


def load_registry_config(path):
    data = read_json(path, {"providers": [], "models": []})
    if not isinstance(data.get("providers", []), list) or not isinstance(data.get("models", []), list):
        raise GatewayError("model_registry.json must contain providers and models lists.", "invalid_model_registry", 500)
    return data


def reload_registry_runtime(server):
    registry = load_registry(server.registry_path)
    server.providers = registry["providers"]
    server.models = registry["models"]


def find_model_config(config, model_id):
    for index, model in enumerate(config.get("models", [])):
        if model.get("id") == model_id:
            return index, model
    return None, None


def find_provider_config(config, provider_id):
    for index, provider in enumerate(config.get("providers", [])):
        if provider.get("id") == provider_id:
            return index, provider
    return None, None


def active_provider_ids_from_config(config):
    return {
        provider.get("id")
        for provider in config.get("providers", [])
        if provider.get("id") and provider.get("enabled", True)
    }


def provider_payload(payload, existing=None):
    existing = existing or {}
    provider_id = (payload.get("provider_id") or payload.get("id") or existing.get("id") or "").strip()
    if not provider_id:
        raise GatewayError("Missing required field: provider_id.", "missing_provider_id", 400)
    name = (payload.get("name") or existing.get("name") or provider_id).strip()
    provider_type = (payload.get("type") or existing.get("type") or "openai_compatible").strip()
    if provider_type not in {"openai_compatible", "anthropic"}:
        raise GatewayError(
            "type must be openai_compatible or anthropic.",
            "invalid_provider_type",
            400,
        )
    base_url = (payload.get("base_url") or existing.get("base_url") or "").strip()
    if not base_url:
        raise GatewayError("Missing required field: base_url.", "missing_base_url", 400)
    api_key_env = (payload.get("api_key_env") or existing.get("api_key_env") or "").strip()
    if not api_key_env:
        raise GatewayError("Missing required field: api_key_env.", "missing_api_key_env", 400)

    provider = {
        "id": provider_id,
        "name": name,
        "type": provider_type,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "enabled": bool(payload.get("enabled", existing.get("enabled", True))),
    }
    api_version = (payload.get("api_version") or existing.get("api_version") or "").strip()
    if api_version:
        provider["api_version"] = api_version
    return provider


def provider_public_view(provider):
    view = {
        "id": provider.get("id"),
        "name": provider.get("name", provider.get("id")),
        "type": provider.get("type", "openai_compatible"),
        "base_url": provider.get("base_url"),
        "api_key_env": provider.get("api_key_env"),
        "enabled": bool(provider.get("enabled", True)),
    }
    if provider.get("api_version"):
        view["api_version"] = provider.get("api_version")
    return view


def provider_create(server, payload):
    config = load_registry_config(server.registry_path)
    provider_id = (payload.get("provider_id") or payload.get("id") or "").strip()
    if not provider_id:
        raise GatewayError("Missing required field: provider_id.", "missing_provider_id", 400)
    _, existing = find_provider_config(config, provider_id)
    if existing is not None:
        raise GatewayError(f"Provider already exists: {provider_id}.", "provider_already_exists", 409)
    provider = provider_payload(payload)
    config.setdefault("providers", []).append(provider)
    write_json(server.registry_path, config)
    reload_registry_runtime(server)
    write_audit_event(
        server,
        "provider.created",
        "provider",
        provider["id"],
        details=provider_public_view(provider),
    )
    return {
        "object": "provider.created",
        "provider": provider_public_view(provider),
        "note": "This prototype stores providers in model_registry.json. Production should use a database, secret manager, approval workflow, and readiness checks.",
    }


def provider_update(server, payload):
    config = load_registry_config(server.registry_path)
    provider_id = (payload.get("provider_id") or payload.get("id") or "").strip()
    if not provider_id:
        raise GatewayError("Missing required field: provider_id.", "missing_provider_id", 400)
    index, existing = find_provider_config(config, provider_id)
    if existing is None:
        raise GatewayError(f"Unknown provider: {provider_id}.", "unknown_provider", 404)
    provider = provider_payload(payload, existing=existing)
    config["providers"][index] = provider
    write_json(server.registry_path, config)
    reload_registry_runtime(server)
    write_audit_event(
        server,
        "provider.updated",
        "provider",
        provider["id"],
        details=provider_public_view(provider),
    )
    return {
        "object": "provider.updated",
        "provider": provider_public_view(provider),
    }


def provider_disable(server, payload):
    config = load_registry_config(server.registry_path)
    provider_id = (payload.get("provider_id") or payload.get("id") or "").strip()
    if not provider_id:
        raise GatewayError("Missing required field: provider_id.", "missing_provider_id", 400)
    index, existing = find_provider_config(config, provider_id)
    if existing is None:
        raise GatewayError(f"Unknown provider: {provider_id}.", "unknown_provider", 404)
    provider = dict(existing)
    provider["enabled"] = False
    disabled_routes = []
    for model in config.get("models", []):
        if model.get("provider") == provider_id and model.get("enabled", True):
            model["enabled"] = False
            disabled_routes.append(model.get("id"))
    config["providers"][index] = provider
    write_json(server.registry_path, config)
    reload_registry_runtime(server)
    details = provider_public_view(provider)
    details["disabled_model_routes"] = disabled_routes
    write_audit_event(
        server,
        "provider.disabled",
        "provider",
        provider_id,
        details=details,
    )
    return {
        "object": "provider.disabled",
        "provider": provider_public_view(provider),
        "disabled_model_routes": disabled_routes,
        "note": "The disabled provider and its active model routes are removed from the active runtime maps.",
    }


def active_model_ids_from_config(config, exclude_model_id=None):
    return {
        model.get("id")
        for model in config.get("models", [])
        if model.get("id") and model.get("enabled", True) and model.get("id") != exclude_model_id
    }


def model_route_payload(server, payload, existing=None, config=None):
    config = config or load_registry_config(server.registry_path)
    existing = existing or {}
    model_id = (payload.get("model_id") or payload.get("id") or existing.get("id") or "").strip()
    if not model_id:
        raise GatewayError("Missing required field: model_id.", "missing_model_id", 400)

    provider_id = (payload.get("provider") or existing.get("provider") or "").strip()
    if provider_id not in active_provider_ids_from_config(config):
        raise GatewayError("Provider is unknown or disabled.", "unknown_provider", 404, {"provider": provider_id})

    upstream_model = (payload.get("upstream_model") or existing.get("upstream_model") or "").strip()
    if not upstream_model:
        raise GatewayError("Missing required field: upstream_model.", "missing_upstream_model", 400)

    capabilities = payload.get("capabilities", existing.get("capabilities", ["chat"]))
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise GatewayError("capabilities must be a list of strings.", "invalid_capabilities", 400)
    capabilities = [item.strip() for item in capabilities if item.strip()]
    if not capabilities:
        raise GatewayError("capabilities must include at least one value.", "invalid_capabilities", 400)

    fallback_models = payload.get("fallback_models", existing.get("fallback_models", []))
    if not isinstance(fallback_models, list) or not all(isinstance(item, str) for item in fallback_models):
        raise GatewayError("fallback_models must be a list of model ids.", "invalid_fallback_models", 400)
    fallback_models = [item.strip() for item in fallback_models if item.strip()]
    if model_id in fallback_models:
        raise GatewayError("fallback_models cannot include the model itself.", "invalid_fallback_models", 400)
    active_models = active_model_ids_from_config(config, exclude_model_id=model_id)
    unknown_fallbacks = [name for name in fallback_models if name not in active_models]
    if unknown_fallbacks:
        raise GatewayError(
            "fallback_models includes unknown or disabled model ids.",
            "unknown_fallback_model",
            404,
            {"unknown_fallback_models": unknown_fallbacks},
        )

    pricing = payload.get("pricing", existing.get("pricing", {})) or {}
    if not isinstance(pricing, dict):
        raise GatewayError("pricing must be an object.", "invalid_pricing", 400)
    try:
        prompt_price = float(pricing.get("prompt_per_1k", 0))
        completion_price = float(pricing.get("completion_per_1k", 0))
    except (TypeError, ValueError):
        raise GatewayError("pricing values must be numbers.", "invalid_pricing", 400)

    return {
        "id": model_id,
        "provider": provider_id,
        "upstream_model": upstream_model,
        "fallback_models": fallback_models,
        "capabilities": capabilities,
        "pricing": {
            "prompt_per_1k": prompt_price,
            "completion_per_1k": completion_price,
        },
        "enabled": bool(payload.get("enabled", existing.get("enabled", True))),
    }


def model_route_public_view(model_config):
    return {
        "id": model_config.get("id"),
        "provider": model_config.get("provider"),
        "upstream_model": model_config.get("upstream_model"),
        "fallback_models": model_config.get("fallback_models", []),
        "capabilities": model_config.get("capabilities", []),
        "pricing": model_config.get("pricing", {}),
        "enabled": bool(model_config.get("enabled", True)),
    }


def model_route_create(server, payload):
    config = load_registry_config(server.registry_path)
    model_id = (payload.get("model_id") or payload.get("id") or "").strip()
    if not model_id:
        raise GatewayError("Missing required field: model_id.", "missing_model_id", 400)
    _, existing = find_model_config(config, model_id)
    if existing is not None:
        raise GatewayError(f"Model route already exists: {model_id}.", "model_route_already_exists", 409)
    model_config = model_route_payload(server, payload, config=config)
    config.setdefault("models", []).append(model_config)
    write_json(server.registry_path, config)
    reload_registry_runtime(server)
    write_audit_event(
        server,
        "model_route.created",
        "model_route",
        model_config["id"],
        details=model_route_public_view(model_config),
    )
    return {
        "object": "model_route.created",
        "model": model_route_public_view(model_config),
        "note": "This prototype stores model routes in model_registry.json. Production should use a database and approval workflow.",
    }


def model_route_update(server, payload):
    config = load_registry_config(server.registry_path)
    model_id = (payload.get("model_id") or payload.get("id") or "").strip()
    if not model_id:
        raise GatewayError("Missing required field: model_id.", "missing_model_id", 400)
    index, existing = find_model_config(config, model_id)
    if existing is None:
        raise GatewayError(f"Unknown model route: {model_id}.", "unknown_model_route", 404)
    model_config = model_route_payload(server, payload, existing=existing, config=config)
    config["models"][index] = model_config
    write_json(server.registry_path, config)
    reload_registry_runtime(server)
    write_audit_event(
        server,
        "model_route.updated",
        "model_route",
        model_config["id"],
        details=model_route_public_view(model_config),
    )
    return {
        "object": "model_route.updated",
        "model": model_route_public_view(model_config),
    }


def model_route_disable(server, payload):
    config = load_registry_config(server.registry_path)
    model_id = (payload.get("model_id") or payload.get("id") or "").strip()
    if not model_id:
        raise GatewayError("Missing required field: model_id.", "missing_model_id", 400)
    index, existing = find_model_config(config, model_id)
    if existing is None:
        raise GatewayError(f"Unknown model route: {model_id}.", "unknown_model_route", 404)
    model_config = dict(existing)
    model_config["enabled"] = False
    config["models"][index] = model_config
    write_json(server.registry_path, config)
    reload_registry_runtime(server)
    write_audit_event(
        server,
        "model_route.disabled",
        "model_route",
        model_id,
        details=model_route_public_view(model_config),
    )
    return {
        "object": "model_route.disabled",
        "model": model_route_public_view(model_config),
        "note": "The disabled route is removed from the active runtime model map.",
    }


def add_config_check(checks, severity, code, message, details=None):
    check = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if details is not None:
        check["details"] = details
    checks.append(check)


def add_alert(alerts, severity, area, code, message, next_step, details=None):
    alert = {
        "severity": severity,
        "area": area,
        "code": code,
        "message": message,
        "next_step": next_step,
    }
    if details is not None:
        alert["details"] = details
    alerts.append(alert)


def gateway_alerts(server):
    alerts = []
    for check in gateway_config_check(server).get("checks", []):
        if check.get("severity") in {"critical", "warning"}:
            add_alert(
                alerts,
                check["severity"],
                "configuration",
                check["code"],
                check["message"],
                "Review /v1/gateway/config-check before using this gateway with customers.",
                check.get("details"),
            )

    for provider in provider_health(server):
        status = provider.get("status")
        if status == "not_ready":
            add_alert(
                alerts,
                "critical",
                "provider",
                "provider_not_ready",
                f"Provider {provider['id']} is not ready.",
                "Enable at least one model and configure a server key or customer BYOK key.",
                {"provider": provider["id"], "reason": provider.get("reason")},
            )
        elif status == "degraded":
            add_alert(
                alerts,
                "warning",
                "provider",
                "provider_degraded",
                f"Provider {provider['id']} has a high recent error rate.",
                "Review recent request activity and consider fallback routing.",
                {"provider": provider["id"], "recent": provider.get("recent")},
            )
        elif status == "ready_mock":
            add_alert(
                alerts,
                "info",
                "provider",
                "provider_mock_only",
                f"Provider {provider['id']} is ready for mock demos only.",
                "Configure the provider key before a live customer test.",
                {"provider": provider["id"]},
            )

    for report in customer_reports(server):
        if report.get("budget_state") == "blocked":
            add_alert(
                alerts,
                "critical",
                "customer",
                "customer_budget_blocked",
                f"Customer {report['id']} has reached a budget limit.",
                "Increase the customer budget or disable the key until billing is reviewed.",
                {"customer": report["id"], "budget": report.get("budget")},
            )
        elif report.get("budget_state") == "warning":
            add_alert(
                alerts,
                "warning",
                "customer",
                "customer_budget_low",
                f"Customer {report['id']} is close to a budget limit.",
                "Warn the customer or raise the budget before requests are blocked.",
                {"customer": report["id"], "budget": report.get("budget")},
            )

    recent_errors = request_activity(server.db_path, {"status": "error"}, limit=5)
    if recent_errors:
        add_alert(
            alerts,
            "warning",
            "requests",
            "recent_request_errors",
            "Recent request errors were recorded.",
            "Open request activity or request detail to review the failed calls.",
            {"count": len(recent_errors), "requests": recent_errors},
        )

    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda alert: (severity_rank.get(alert["severity"], 3), alert["area"], alert["code"]))
    return {
        "status": "critical" if any(alert["severity"] == "critical" for alert in alerts) else "warning" if any(alert["severity"] == "warning" for alert in alerts) else "ok",
        "summary": {
            "critical": sum(1 for alert in alerts if alert["severity"] == "critical"),
            "warning": sum(1 for alert in alerts if alert["severity"] == "warning"),
            "info": sum(1 for alert in alerts if alert["severity"] == "info"),
        },
        "alerts": alerts,
    }


def gateway_config_check(server):
    checks = []
    if server.admin_api_key == DEFAULT_ADMIN_API_KEY:
        add_config_check(
            checks,
            "warning",
            "default_admin_key",
            "The gateway is using the local demo admin key.",
            {"change_with": "GATEWAY_ADMIN_API_KEY"},
        )
    if not server.admin_api_key:
        add_config_check(
            checks,
            "critical",
            "admin_unprotected",
            "Admin endpoints are not protected by an admin key.",
        )

    demo_gateway_keys = {DEFAULT_GATEWAY_API_KEY, "demo-limited-key", "demo-budget-key"}
    demo_customers = [
        customer.get("id")
        for api_key, customer in server.customers_by_key.items()
        if api_key in demo_gateway_keys
    ]
    if demo_customers:
        add_config_check(
            checks,
            "warning",
            "demo_customer_keys",
            "Some customers are using demo gateway API keys.",
            {"customers": demo_customers},
        )

    for customer in server.customers_by_key.values():
        plain_secret_providers = [
            provider_id
            for provider_id, value in customer.get("provider_api_keys", {}).items()
            if value and not str(value).startswith("env:")
        ]
        if plain_secret_providers:
            add_config_check(
                checks,
                "warning",
                "plain_provider_secret",
                "A customer provider key appears to be stored directly in customer_keys.json.",
                {
                    "customer": customer.get("id"),
                    "providers": plain_secret_providers,
                },
            )
        if customer.get("token_budget") is None:
            add_config_check(
                checks,
                "info",
                "missing_token_budget",
                "A customer has no token budget.",
                {"customer": customer.get("id")},
            )
        if customer.get("cost_budget") is None:
            add_config_check(
                checks,
                "info",
                "missing_cost_budget",
                "A customer has no cost budget.",
                {"customer": customer.get("id")},
            )

    active_provider_ids = set(server.providers.keys())
    model_provider_ids = {model.get("provider") for model in server.models.values()}
    for provider_id, provider in sorted(server.providers.items()):
        env_name = provider.get("api_key_env")
        server_key_configured = bool(env_name and os.getenv(env_name))
        byok_customers = [
            customer.get("id")
            for customer in server.customers_by_key.values()
            if provider_id in customer.get("provider_api_keys", {})
        ]
        if provider_id not in model_provider_ids:
            add_config_check(
                checks,
                "info",
                "provider_has_no_enabled_models",
                "An enabled provider has no enabled models.",
                {"provider": provider_id},
            )
        if not server.mock_mode and not server_key_configured and not byok_customers:
            add_config_check(
                checks,
                "critical",
                "provider_key_missing",
                "Live mode needs a provider key or BYOK customer key.",
                {"provider": provider_id, "api_key_env": env_name},
            )
        elif server.mock_mode and not server_key_configured and not byok_customers:
            add_config_check(
                checks,
                "info",
                "provider_key_missing_in_mock",
                "A provider key is not configured. This is acceptable in mock mode.",
                {"provider": provider_id, "api_key_env": env_name},
            )

    orphan_provider_ids = sorted(model_provider_ids - active_provider_ids)
    if orphan_provider_ids:
        add_config_check(
            checks,
            "critical",
            "model_provider_missing",
            "Some enabled models reference a provider that is not active.",
            {"providers": orphan_provider_ids},
        )

    severity_rank = {"info": 0, "warning": 1, "critical": 2}
    max_severity = max((severity_rank.get(check["severity"], 0) for check in checks), default=0)
    status = "ok"
    if max_severity == 1:
        status = "warning"
    if max_severity >= 2:
        status = "critical"
    return {
        "status": status,
        "mode": "mock" if server.mock_mode else "live",
        "checks": checks,
        "summary": {
            "critical": sum(1 for check in checks if check["severity"] == "critical"),
            "warning": sum(1 for check in checks if check["severity"] == "warning"),
            "info": sum(1 for check in checks if check["severity"] == "info"),
        },
    }


def production_readiness(server):
    config = gateway_config_check(server)
    alerts = gateway_alerts(server)
    provider_rows = provider_health(server)
    customers = [public_customer_view(customer) for customer in server.customers_by_key.values()]
    models = model_catalog(server)
    check_codes = {check.get("code") for check in config.get("checks", [])}
    alert_summary = alerts.get("summary", {})
    live_ready_providers = [
        provider["id"]
        for provider in provider_rows
        if provider.get("status") == "ready_live"
    ]
    mock_only_providers = [
        provider["id"]
        for provider in provider_rows
        if provider.get("status") == "ready_mock"
    ]
    not_ready_providers = [
        provider["id"]
        for provider in provider_rows
        if provider.get("status") in {"not_ready", "degraded"}
    ]

    categories = [
        {
            "area": "Security",
            "status": "needs_work" if {"default_admin_key", "demo_customer_keys", "plain_provider_secret"} & check_codes else "ready",
            "plain_english": "Replace demo keys and move secrets to a secret manager before production.",
            "evidence": {
                "default_admin_key": "default_admin_key" in check_codes,
                "demo_customer_keys": "demo_customer_keys" in check_codes,
                "plain_provider_secret": "plain_provider_secret" in check_codes,
            },
            "next_step": "Use real customer keys, rotate the admin key, and store provider credentials outside JSON files.",
        },
        {
            "area": "Provider Readiness",
            "status": "blocked" if not_ready_providers else "needs_work" if mock_only_providers and not live_ready_providers else "ready",
            "plain_english": "Mock-ready providers can explain routing, but live traffic needs real provider keys or BYOK.",
            "evidence": {
                "ready_live": live_ready_providers,
                "ready_mock": mock_only_providers,
                "not_ready_or_degraded": not_ready_providers,
            },
            "next_step": "Configure provider API keys, test live calls with low limits, and keep fallback providers ready.",
        },
        {
            "area": "Customer Controls",
            "status": "needs_work" if any(customer.get("token_budget") is None or customer.get("cost_budget") is None for customer in customers) else "ready",
            "plain_english": "Every production customer should have access rules, request limits, token budgets, and cost budgets.",
            "evidence": {
                "customers": len(customers),
                "customers_without_token_budget": [
                    customer["id"] for customer in customers if customer.get("token_budget") is None
                ],
                "customers_without_cost_budget": [
                    customer["id"] for customer in customers if customer.get("cost_budget") is None
                ],
            },
            "next_step": "Confirm allowed models, default policy, request limits, token budgets, and cost budgets per customer.",
        },
        {
            "area": "Routing And Fallback",
            "status": "ready" if any(model.get("fallback_models") for model in models) else "needs_work",
            "plain_english": "Fallback routes make outages easier to explain and recover from.",
            "evidence": {
                "models": len(models),
                "models_with_fallback": [
                    model["id"] for model in models if model.get("fallback_models")
                ],
                "policy_presets": sorted(POLICY_PRESETS.keys()),
            },
            "next_step": "Define fallback chains and default policies for customer-facing models.",
        },
        {
            "area": "Observability",
            "status": "ready" if alert_summary.get("critical", 0) == 0 else "blocked",
            "plain_english": "Production needs request history, usage records, alerts, and a support workflow.",
            "evidence": {
                "alerts": alert_summary,
                "request_activity_endpoint": "/v1/gateway/request-activity",
                "request_detail_endpoint": "/v1/gateway/request-detail",
                "audit_endpoint": "/v1/gateway/audit-events",
            },
            "next_step": "Connect logs and alerts to the real operations process before production traffic.",
        },
        {
            "area": "Billing",
            "status": "needs_work",
            "plain_english": "The prototype estimates usage and invoice data, but production billing needs approved pricing and finance rules.",
            "evidence": {
                "invoice_preview_endpoint": "/v1/gateway/invoice-preview",
                "cost_estimate_endpoint": "/v1/gateway/cost-estimate",
                "customer_report_endpoint": "/v1/gateway/customer-reports",
            },
            "next_step": "Agree pricing, tax, invoice timing, credits, refunds, and budget enforcement rules.",
        },
        {
            "area": "Documentation And Handoff",
            "status": "ready",
            "plain_english": "The prototype includes customer-facing docs, OpenAPI, Postman, demo bundle, and an integration guide.",
            "evidence": {
                "openapi": "/openapi.json",
                "postman": "/postman_collection.json",
                "demo_bundle": "/v1/gateway/demo-bundle",
                "integration_guide": "/v1/gateway/integration-guide",
                "pdf": "Model_Gateway_Customer_Guide.pdf",
            },
            "next_step": "Use the demo bundle first, then hand OpenAPI and Postman to the customer technical team.",
        },
    ]
    category_statuses = {category["status"] for category in categories}
    overall = "blocked" if "blocked" in category_statuses else "needs_work" if "needs_work" in category_statuses else "ready"
    return {
        "object": "gateway.production_readiness",
        "overall_status": overall,
        "mode": "mock" if server.mock_mode else "live",
        "prototype_only": True,
        "executive_summary": (
            "This gateway is ready for customer explanation and mock demos, "
            "but production use still needs real secrets, provider readiness, billing rules, and operating controls."
            if overall != "ready"
            else "This gateway has no blocking readiness items in the local prototype checks."
        ),
        "categories": categories,
        "next_demo_step": "Use /v1/gateway/demo-bundle to walk a customer through the story.",
        "next_production_step": "Start with Security and Provider Readiness before any live customer traffic.",
    }


def launch_plan(server):
    readiness = production_readiness(server)
    category_by_area = {category["area"]: category for category in readiness.get("categories", [])}

    def gate(gate_id, name, owner, readiness_area, required_evidence, approval_question):
        category = category_by_area.get(readiness_area, {})
        status = category.get("status", "needs_work")
        return {
            "id": gate_id,
            "name": name,
            "owner": owner,
            "status": status,
            "readiness_area": readiness_area,
            "plain_english": category.get("plain_english", "This gate needs review before production."),
            "required_evidence": required_evidence,
            "current_evidence": category.get("evidence", {}),
            "approval_question": approval_question,
            "next_step": category.get("next_step", "Name an owner and collect evidence before approval."),
        }

    gates = [
        gate(
            "security",
            "Security and secrets",
            "Gateway owner",
            "Security",
            [
                "Admin key is rotated away from the local demo value.",
                "Customer keys are stored outside local JSON.",
                "Provider secrets are stored in environment variables or a secret manager.",
            ],
            "Are customer and provider secrets safe enough for real traffic?",
        ),
        gate(
            "provider",
            "Provider live readiness",
            "Technical owner",
            "Provider Readiness",
            [
                "At least one provider has a live key or approved BYOK path.",
                "Live chat, stream, error, timeout, and usage behavior are tested.",
                "Provider fallback behavior is documented.",
            ],
            "Can the selected provider handle the first customer production use case?",
        ),
        gate(
            "customer_controls",
            "Customer access and budgets",
            "Customer success owner",
            "Customer Controls",
            [
                "Customer allowed models are approved.",
                "Request limits, token budget, and cost budget are set.",
                "Customer-facing integration guide is reviewed.",
            ],
            "Can this customer use the gateway without unexpected access or spend?",
        ),
        gate(
            "routing",
            "Routing and fallback",
            "Gateway owner",
            "Routing And Fallback",
            [
                "Public model names map to approved upstream models.",
                "Fallback models are approved or explicitly disabled.",
                "Route preview matches the customer policy.",
            ],
            "Is the model route predictable and explainable enough for support?",
        ),
        gate(
            "observability",
            "Support and observability",
            "Support owner",
            "Observability",
            [
                "Request activity and request detail can trace a customer issue.",
                "Audit events show admin changes.",
                "Incident playbook and support policy are reviewed.",
            ],
            "Can support investigate a failed or disputed request?",
        ),
        gate(
            "billing",
            "Billing and commercial rules",
            "Business owner",
            "Billing",
            [
                "Pricing, credits, refunds, and invoice timing are agreed.",
                "Invoice preview is treated as estimate until finance approval.",
                "Budget enforcement behavior is approved.",
            ],
            "Can the business explain cost before the customer sends production traffic?",
        ),
        gate(
            "handoff",
            "Customer handoff",
            "Customer success owner",
            "Documentation And Handoff",
            [
                "Dashboard, OpenAPI, Postman, guide PDF, and integration guide are ready.",
                "Customer onboarding plan is shared.",
                "Pilot checklist exit decision is recorded.",
            ],
            "Does the customer have the material needed to start safely?",
        ),
    ]
    blockers = [
        {
            "gate": item["id"],
            "name": item["name"],
            "status": item["status"],
            "next_step": item["next_step"],
        }
        for item in gates
        if item["status"] in {"blocked", "needs_work"}
    ]
    decision = "no_go" if any(item["status"] == "blocked" for item in gates) else "conditional_go" if blockers else "go"
    return {
        "object": "gateway.launch_plan",
        "title": "AISmallRouter Production Launch Plan",
        "audience": "business, technical, support, and operations owners",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This launch plan converts readiness gaps into go-live gates. It shows what must be approved before a customer uses the gateway for production traffic.",
        "decision": decision,
        "decision_meaning": {
            "go": "All local launch gates look ready.",
            "conditional_go": "There are no hard blockers, but named owners must close remaining work before broad production traffic.",
            "no_go": "At least one gate is blocked. Do not promise production traffic yet.",
        }[decision],
        "gates": gates,
        "blockers": blockers,
        "required_signoffs": [
            {"owner": "Business owner", "signs_off": "Customer value, pricing, and commercial risk."},
            {"owner": "Technical owner", "signs_off": "Provider behavior, route correctness, and live test evidence."},
            {"owner": "Support owner", "signs_off": "Incident handling, traceability, and customer wording."},
            {"owner": "Gateway owner", "signs_off": "Secrets, configuration, rollout, and rollback control."},
        ],
        "rollout_stages": [
            {"stage": "Internal live test", "traffic": "internal only", "exit_check": "One live Qwen request succeeds and is traceable."},
            {"stage": "Single customer pilot", "traffic": "one named customer, low limits", "exit_check": "Usage, cost, support, and fallback story are clear."},
            {"stage": "Limited production", "traffic": "small approved group", "exit_check": "No unresolved P1/P2 issues and billing rules are approved."},
            {"stage": "Broader rollout", "traffic": "more customers", "exit_check": "Operations, billing, and provider contracts are production-owned."},
        ],
        "reference_endpoints": [
            "/v1/gateway/production-readiness",
            "/v1/gateway/provider-contracts",
            "/v1/gateway/customer-reports",
            "/v1/gateway/invoice-preview",
            "/v1/gateway/incident-playbook",
            "/v1/gateway/onboarding-plan",
        ],
        "next_best_action": blockers[0]["next_step"] if blockers else "Record signoffs and start with an internal live test.",
    }


def change_management_plan(server):
    launch = launch_plan(server)
    return {
        "object": "gateway.change_management",
        "title": "AISmallRouter Change Management Plan",
        "audience": "gateway owner, support owner, and customer technical owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This plan explains how to change customers, providers, or model routes without surprising a customer.",
        "change_types": [
            {
                "type": "customer_access_change",
                "examples": ["create customer", "rotate customer key", "disable customer", "change allowed models"],
                "risk_level": "medium",
                "approval_owner": "Customer success owner",
                "before_change": [
                    "Confirm customer id, allowed models, request limits, token budget, and cost budget.",
                    "Preview key issue package before saving a new customer.",
                    "Confirm who receives the one-time customer key.",
                ],
                "after_change": [
                    "Ask customer to call /v1/gateway/me.",
                    "Check access matrix and audit events.",
                    "Record whether old keys should stop working.",
                ],
                "rollback_path": "Rotate the key again, disable the customer, or restore the previous customer config from version control.",
            },
            {
                "type": "provider_change",
                "examples": ["create provider", "update provider base URL", "change API key environment variable", "disable provider"],
                "risk_level": "high",
                "approval_owner": "Gateway owner",
                "before_change": [
                    "Review provider contract and health.",
                    "Confirm provider key environment variable exists outside JSON.",
                    "Run route preview for affected public models.",
                ],
                "after_change": [
                    "Check provider health.",
                    "Check model catalog for affected routes.",
                    "Run one mock request before any live request.",
                ],
                "rollback_path": "Disable the provider, restore the previous provider config from version control, or route affected models to fallback providers.",
            },
            {
                "type": "model_route_change",
                "examples": ["create public model route", "change upstream model", "change fallback list", "disable route"],
                "risk_level": "high",
                "approval_owner": "Technical owner",
                "before_change": [
                    "Run route preview with the target customer.",
                    "Confirm pricing, capabilities, fallback, and provider contract.",
                    "Check whether customer docs or SDK examples need a model name update.",
                ],
                "after_change": [
                    "Check model catalog.",
                    "Run /v1/models with customer key.",
                    "Run one mock chat request and save gateway.request_id.",
                ],
                "rollback_path": "Restore the previous route config from version control, disable the new route, or switch customer default policy back to the previous route.",
            },
        ],
        "approval_checklist": [
            "Business owner understands customer impact.",
            "Technical owner confirms route/provider behavior.",
            "Support owner can trace and explain the change.",
            "Gateway owner confirms rollback path.",
            "Audit event will be created by the lifecycle endpoint.",
        ],
        "evidence_endpoints": [
            "/v1/gateway/route-preview",
            "/v1/gateway/provider-health",
            "/v1/gateway/model-catalog",
            "/v1/gateway/access-matrix",
            "/v1/gateway/audit-events",
            "/v1/gateway/request-activity",
            "/v1/gateway/launch-plan",
        ],
        "current_launch_decision": launch.get("decision"),
        "next_best_action": "Use route preview and audit events for every provider or model route change before showing it to a customer.",
        "prototype_note": "The prototype writes local JSON and audit events. Production should add approval workflow, config version history, rollback automation, and staged rollout.",
    }


def data_governance_review(server):
    config = gateway_config_check(server)
    categories = [
        {
            "area": "Prompt and response data",
            "status": "prototype_ready",
            "plain_english": "The gateway receives prompts, sends them to the selected provider, and returns one normalized response to the customer.",
            "what_this_prototype_does": [
                "Uses one OpenAI-compatible request shape for the customer.",
                "Can run in mock mode without sending prompt text to a paid provider.",
                "Stores request metadata and preview content for local demo tracing.",
            ],
            "production_gap": "Define what prompt and response content may be logged, masked, retained, exported, or deleted.",
            "owner": "Gateway owner and customer data owner",
        },
        {
            "area": "Provider secrets",
            "status": "needs_production_hardening" if config.get("issues") else "prototype_ready",
            "plain_english": "Provider API keys should never be shown to customers or stored inside public model records.",
            "what_this_prototype_does": [
                "Stores provider API key environment variable names, not raw provider key values.",
                "Hides provider secrets from status, route preview, OpenAPI, Postman, dashboard, and demo bundle outputs.",
                "Supports customer BYOK mapping without exposing the provider key in customer-facing responses.",
            ],
            "production_gap": "Move all secrets to a secret manager, add rotation workflow, and restrict who can view or change secret mappings.",
            "owner": "Security owner and gateway owner",
        },
        {
            "area": "Customer gateway keys",
            "status": "prototype_ready",
            "plain_english": "Each customer should have their own gateway key, allowed models, request limits, and budget controls.",
            "what_this_prototype_does": [
                "Authenticates customer calls with a gateway API key.",
                "Shows only masked key material after setup.",
                "Can rotate or disable a customer key through admin lifecycle endpoints.",
            ],
            "production_gap": "Move customer keys from local JSON to a database or key management system with approval, audit, and emergency disable controls.",
            "owner": "Gateway owner",
        },
        {
            "area": "Logging and retention",
            "status": "needs_policy",
            "plain_english": "Logs help support teams debug requests, but logs can also contain sensitive business data.",
            "what_this_prototype_does": [
                "Writes request, usage, and audit records to local SQLite for demo tracing.",
                "Shows request activity and request detail for support explanation.",
                "Does not implement automatic deletion, retention windows, or export controls yet.",
            ],
            "production_gap": "Approve retention period, delete/export process, log masking rules, and access rules before production traffic.",
            "owner": "Customer data owner and support owner",
        },
        {
            "area": "Sensitive data preview",
            "status": "prototype_ready",
            "plain_english": "The gateway can preview obvious sensitive data patterns before a request is sent.",
            "what_this_prototype_does": [
                "Provides /v1/gateway/safety-preview for local redaction and blocking explanation.",
                "Can redact simple email, phone, and API-key-like patterns in demo mode.",
                "Keeps this as a preview control, not a full compliance engine.",
            ],
            "production_gap": "Add customer-specific data policies, stronger PII detection, allow/block rules, and human review workflow.",
            "owner": "Security owner and customer data owner",
        },
        {
            "area": "Customer visibility",
            "status": "prototype_ready",
            "plain_english": "Customers should see their own usage, limits, models, and integration guide, not other customers or provider secrets.",
            "what_this_prototype_does": [
                "Provides /v1/gateway/me for customer-specific access and usage.",
                "Provides customer reports for admin account review.",
                "Separates customer bearer key endpoints from admin bearer key endpoints.",
            ],
            "production_gap": "Add tenant isolation tests, role-based admin permissions, and customer export/download controls.",
            "owner": "Gateway owner and customer success owner",
        },
    ]
    return {
        "object": "gateway.data_governance",
        "title": "AISmallRouter Data Governance Review",
        "audience": "business owner, security owner, customer data owner, and gateway owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This review explains what happens to prompt data, logs, customer keys, and provider secrets before a customer trusts the gateway.",
        "categories": categories,
        "policy_questions": [
            "Can prompts and responses be stored for support, or should only metadata be stored?",
            "What is the retention period for request logs, usage logs, and audit logs?",
            "Who can view customer usage and request detail?",
            "What sensitive data should be blocked, redacted, or allowed?",
            "Can a customer bring their own provider key, and who can rotate it?",
            "What must be deleted when a customer leaves?",
        ],
        "evidence_endpoints": [
            "/v1/gateway/safety-preview",
            "/v1/gateway/me",
            "/v1/gateway/request-activity",
            "/v1/gateway/request-detail",
            "/v1/gateway/audit-events",
            "/v1/gateway/config-check",
            "/v1/gateway/production-readiness",
        ],
        "recommended_next_action": "Agree a simple logging and retention policy before any real customer production traffic.",
        "prototype_note": "This is a governance explanation, not a compliance certification. Production still needs legal review, secret manager, access control, retention automation, and deletion workflow.",
    }


def security_review(server):
    config = gateway_config_check(server)
    governance = data_governance_review(server)
    readiness = production_readiness(server)
    return {
        "object": "gateway.security_review",
        "title": "AISmallRouter Security Review",
        "audience": "business owner, customer security reviewer, gateway owner, and support owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This review explains the main security questions a customer will ask before trusting one gateway with many AI providers.",
        "security_posture": {
            "current_stage": "prototype",
            "customer_safe_message": "The prototype is safe for local explanation and mock demos. It is not yet a production security certification.",
            "strongest_current_controls": [
                "Customer requests use gateway API keys instead of provider API keys.",
                "Admin endpoints require a separate admin key.",
                "Provider secret values are not returned in customer, dashboard, OpenAPI, Postman, or demo bundle responses.",
                "Mock mode can explain routing without sending prompts to a paid provider.",
                "Request, usage, and audit records help explain what happened during a demo.",
            ],
            "main_production_gaps": [
                "Replace local JSON keys with a real key store or identity provider.",
                "Move provider secrets to a managed secret manager with rotation and access review.",
                "Add role-based admin permissions instead of one shared admin key.",
                "Define log retention, redaction, deletion, and export policy.",
                "Add tenant isolation tests and security review before live customer traffic.",
            ],
        },
        "threats": [
            {
                "risk": "Customer key leak",
                "what_could_happen": "Someone with a customer gateway key could call allowed models under that customer.",
                "current_control": "Keys are scoped to allowed models, request limits, token budgets, cost budgets, and can be rotated or disabled.",
                "production_control_needed": "Store keys in a real key management system, add expiry, owner approval, emergency disable, and anomaly alerts.",
                "evidence": ["/v1/gateway/me", "/v1/gateway/customers/rotate-key", "/v1/gateway/customers/disable"],
            },
            {
                "risk": "Provider secret exposure",
                "what_could_happen": "A provider API key could let someone spend credits or access provider resources directly.",
                "current_control": "Provider records use API key environment variable names and responses avoid raw provider secrets.",
                "production_control_needed": "Use a managed secret store, rotation schedule, restricted operators, and secret access audit.",
                "evidence": ["/v1/gateway/config-check", "/v1/gateway/provider-health", "/v1/gateway/data-governance"],
            },
            {
                "risk": "Sensitive prompt data in logs",
                "what_could_happen": "Support logs could capture customer business data or personal data.",
                "current_control": "Safety preview can show obvious email, phone, and API-key-like patterns before sending a request.",
                "production_control_needed": "Agree retention rules, log masking, deletion workflow, and customer-specific data policy.",
                "evidence": ["/v1/gateway/safety-preview", "/v1/gateway/request-activity", "/v1/gateway/data-governance"],
            },
            {
                "risk": "Wrong customer or model access",
                "what_could_happen": "A customer could see or use a model they should not access.",
                "current_control": "Customer config has allowed_models, and /v1/models only returns models available to that customer.",
                "production_control_needed": "Add tenant isolation tests, customer export controls, and stronger policy review before changes.",
                "evidence": ["/v1/models", "/v1/gateway/access-matrix", "/v1/gateway/route-preview"],
            },
            {
                "risk": "Unsafe admin change",
                "what_could_happen": "A route, provider, or key change could break customer traffic or send traffic to an unapproved provider.",
                "current_control": "Lifecycle endpoints write audit events and change management describes before/after checks.",
                "production_control_needed": "Add approval workflow, staged rollout, config version history, and automated rollback.",
                "evidence": ["/v1/gateway/change-management", "/v1/gateway/audit-events", "/v1/gateway/model-catalog"],
            },
        ],
        "customer_security_questions": [
            {
                "question": "Will our users ever receive provider API keys?",
                "short_answer": "No. Customers use gateway keys. Provider keys stay behind the gateway.",
            },
            {
                "question": "Can we test without sending prompts to a real model provider?",
                "short_answer": "Yes. Mock mode explains routing and returns simulated responses without provider spend.",
            },
            {
                "question": "Can each customer have different model access?",
                "short_answer": "Yes. The prototype supports per-customer allowed models, request limits, and budgets.",
            },
            {
                "question": "Is this production-certified today?",
                "short_answer": "No. It is a prototype. Use the production backlog and launch plan before any production promise.",
            },
        ],
        "go_live_security_gates": [
            "Replace demo gateway and admin keys.",
            "Store provider secrets in a managed secret manager.",
            "Define customer log retention, masking, export, and deletion rules.",
            "Add role-based admin access and audit review.",
            "Run tenant isolation, fallback, budget, and provider contract tests.",
            "Agree support escalation and incident wording with the customer.",
        ],
        "evidence_endpoints": [
            "/v1/gateway/config-check",
            "/v1/gateway/data-governance",
            "/v1/gateway/production-readiness",
            "/v1/gateway/production-backlog",
            "/v1/gateway/change-management",
            "/v1/gateway/incident-playbook",
            "/v1/gateway/access-matrix",
            "/v1/gateway/audit-events",
        ],
        "current_context": {
            "config_issue_count": len(config.get("issues", [])),
            "data_governance_categories": len(governance.get("categories", [])),
            "readiness_status": readiness.get("overall_status"),
        },
        "next_best_action": "Use this security review before a customer pilot, then convert open risks into production backlog items.",
        "prototype_note": "This is a security explanation for planning and customer discussion. It is not a penetration test, SOC 2 report, legal compliance review, or production approval.",
    }


def incident_playbook(server):
    alerts = gateway_alerts(server)
    alert_codes = {alert.get("code") for alert in alerts.get("alerts", [])}
    recent_errors = request_activity(server.db_path, {"status": "error"}, limit=5)
    provider_rows = provider_health(server)
    customer_rows = customer_reports(server)
    scenarios = [
        {
            "id": "provider_not_ready",
            "title": "Provider cannot serve traffic",
            "trigger": "Provider health is not_ready or live mode has no usable provider key.",
            "signals": [
                "/v1/gateway/provider-health",
                "/v1/gateway/alerts",
                "/v1/gateway/production-readiness",
            ],
            "current_evidence": {
                "active": "provider_not_ready" in alert_codes,
                "providers": [
                    provider
                    for provider in provider_rows
                    if provider.get("status") in {"not_ready", "degraded"}
                ],
            },
            "operator_steps": [
                "Check provider health and confirm whether the gateway is in mock or live mode.",
                "Confirm server provider key or customer BYOK key is available.",
                "Use fallback routing if a healthy fallback provider exists.",
                "Keep customer-facing model names stable while changing upstream routes.",
            ],
            "customer_message": "The public model route is being checked. We can keep the same customer API while reviewing the upstream provider path.",
        },
        {
            "id": "recent_request_errors",
            "title": "Recent customer requests are failing",
            "trigger": "Recent request activity includes error responses.",
            "signals": [
                "/v1/gateway/request-activity?status=error",
                "/v1/gateway/request-detail?request_id=...",
                "/v1/gateway/alerts",
            ],
            "current_evidence": {
                "active": bool(recent_errors),
                "recent_errors": recent_errors,
            },
            "operator_steps": [
                "Open request activity and filter by customer, model, provider, or error code.",
                "Use request detail with gateway.request_id from the customer response.",
                "Check route decision, selected provider, upstream model, and latency.",
                "If the problem is provider-specific, try fallback or provider allow-list controls.",
            ],
            "customer_message": "Please share the gateway.request_id. We can trace the route decision without exposing provider secrets.",
        },
        {
            "id": "customer_budget_blocked",
            "title": "Customer is blocked by budget or limit",
            "trigger": "Customer budget state is warning or blocked.",
            "signals": [
                "/v1/gateway/customer-reports",
                "/v1/gateway/me",
                "/v1/gateway/invoice-preview",
            ],
            "current_evidence": {
                "active": any(customer.get("budget_state") in {"warning", "blocked"} for customer in customer_rows),
                "customers": [
                    {
                        "id": customer.get("id"),
                        "budget_state": customer.get("budget_state"),
                        "budget": customer.get("budget"),
                    }
                    for customer in customer_rows
                    if customer.get("budget_state") in {"warning", "blocked"}
                ],
            },
            "operator_steps": [
                "Check customer report and invoice preview.",
                "Confirm whether request, token, or cost budget caused the block.",
                "Decide whether to raise budget, disable the key, or keep the block.",
                "Record the decision as an audit event in production.",
            ],
            "customer_message": "The gateway is enforcing the agreed usage controls. We can review usage and budget before changing limits.",
        },
        {
            "id": "demo_key_or_secret_risk",
            "title": "Demo key or secret handling risk",
            "trigger": "Config check finds demo keys or direct provider secrets.",
            "signals": [
                "/v1/gateway/config-check",
                "/v1/gateway/production-readiness",
            ],
            "current_evidence": {
                "active": bool({"default_admin_key", "demo_customer_keys", "plain_provider_secret"} & alert_codes),
                "matching_alerts": [
                    alert for alert in alerts.get("alerts", [])
                    if alert.get("code") in {"default_admin_key", "demo_customer_keys", "plain_provider_secret"}
                ],
            },
            "operator_steps": [
                "Rotate the admin key and customer demo keys.",
                "Move provider secrets into environment variables or a secret manager.",
                "Re-run config check before any live customer traffic.",
                "Do not send provider secrets to customers or store them in docs.",
            ],
            "customer_message": "The demo uses local keys for explanation. Production will use rotated keys and secret management.",
        },
        {
            "id": "provider_contract_gap",
            "title": "New provider is not ready for customer traffic",
            "trigger": "Provider contract is scaffolded or planned, not fully implemented and tested.",
            "signals": [
                "/v1/gateway/provider-contracts",
                "/v1/gateway/model-catalog",
                "/v1/gateway/route-preview",
            ],
            "current_evidence": {
                "active": True,
                "contract_endpoint": "/v1/gateway/provider-contracts",
            },
            "operator_steps": [
                "Confirm official provider docs for auth, chat, streaming, tools, usage, and errors.",
                "Add provider as disabled first.",
                "Write mock and live contract tests.",
                "Enable one limited test customer before broad rollout.",
            ],
            "customer_message": "Adding a provider means validating its AI contract, not only forwarding HTTP.",
        },
    ]
    return {
        "object": "gateway.incident_playbook",
        "mode": "mock" if server.mock_mode else "live",
        "summary": {
            "alert_status": alerts.get("status"),
            "active_alerts": alerts.get("summary", {}),
            "recent_error_count": len(recent_errors),
            "scenario_count": len(scenarios),
        },
        "use_this_when": [
            "A customer says a model request failed.",
            "A provider is not ready or degraded.",
            "A customer asks why usage is blocked.",
            "A new provider is being prepared.",
            "A sales or support person needs simple customer wording.",
        ],
        "scenarios": scenarios,
        "escalation_rule": "Escalate to engineering when a provider is degraded, a request_id cannot be traced, or a live provider contract is not tested.",
        "not_included": [
            "This is not a legal SLA.",
            "This is not a full incident management system.",
            "This does not page people or change routes automatically.",
        ],
    }


def operations_runbook(server):
    alerts = gateway_alerts(server)
    providers = provider_health(server)
    customers = customer_reports(server)
    summary = db_summary(server.db_path)
    request_summary = request_grouped_by(server.db_path, "status")
    total_requests = int(summary.get("requests") or 0)
    total_errors = int(summary.get("errors") or 0)
    error_rate = round(total_errors / total_requests, 4) if total_requests else 0
    avg_latency = round(float(summary.get("avg_latency_ms") or 0), 2)
    return {
        "object": "gateway.operations_runbook",
        "title": "AISmallRouter Operations Runbook",
        "audience": "support owner, gateway owner, platform owner, and business owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This runbook explains what to watch after the gateway is used by a customer, who should act, and what evidence to collect before changing routes or promises.",
        "operating_stage": "prototype_demo" if server.mock_mode else "live_test",
        "slo_targets": [
            {"name": "Availability target", "prototype_target": "Dashboard and /health should respond during demos.", "production_target": "Define legal uptime only after production hardening and support contract.", "current_signal": "/health"},
            {"name": "Error rate target", "prototype_target": "Keep recent demo error rate under 5%.", "production_target": "Agree customer-specific error budget and alert threshold.", "current_signal": f"{error_rate} recent error rate"},
            {"name": "Latency target", "prototype_target": "Track average latency for explanation.", "production_target": "Set workflow-specific latency SLO after live tests.", "current_signal": f"{avg_latency} ms average latency"},
            {"name": "Budget target", "prototype_target": "Show warning or blocked state clearly.", "production_target": "Agree budget alerts, overage behavior, and owner approval.", "current_signal": "customer reports and invoice preview"},
        ],
        "daily_checks": [
            {"check": "Gateway health", "owner": "Platform owner", "evidence": "/health", "action_if_bad": "Restart local demo or service, then check deployment readiness."},
            {"check": "Provider health", "owner": "Gateway owner", "evidence": "/v1/gateway/provider-health", "action_if_bad": "Use route preview and fallback policy before changing customer-facing route."},
            {"check": "Customer errors", "owner": "Support owner", "evidence": "/v1/gateway/request-activity?status=error", "action_if_bad": "Ask for gateway.request_id and open request detail."},
            {"check": "Customer budget", "owner": "Business owner", "evidence": "/v1/gateway/customer-reports", "action_if_bad": "Confirm whether to raise budget, keep block, or stop test."},
            {"check": "Security and config warnings", "owner": "Gateway owner", "evidence": "/v1/gateway/alerts", "action_if_bad": "Run config check and security review before live traffic."},
        ],
        "alert_actions": [
            {"alert_area": "provider", "first_action": "Check provider health and route preview.", "customer_message": "The public model route is being checked while we review upstream provider health."},
            {"alert_area": "requests", "first_action": "Find request_id in request detail.", "customer_message": "Please share the gateway.request_id so we can trace the route decision."},
            {"alert_area": "budget", "first_action": "Open customer report and invoice preview.", "customer_message": "The gateway is enforcing agreed usage controls; we can review usage before changing limits."},
            {"alert_area": "security", "first_action": "Open config check, data governance, and security review.", "customer_message": "The demo uses local controls; production requires approved secret and data handling rules."},
        ],
        "ownership": [
            {"role": "Support owner", "owns": ["first customer response", "request_id collection", "incident wording"]},
            {"role": "Gateway owner", "owns": ["route preview", "provider health", "model route changes", "security review"]},
            {"role": "Platform owner", "owns": ["deployment health", "logs", "monitoring", "rollback execution"]},
            {"role": "Business owner", "owns": ["budget decisions", "pilot decision", "customer expectation"]},
        ],
        "current_signals": {
            "request_summary": summary,
            "status_breakdown": request_summary,
            "active_alerts": alerts.get("summary", {}),
            "providers": [
                {
                    "id": provider.get("id"),
                    "status": provider.get("status"),
                    "reason": provider.get("reason"),
                    "live_ready": provider.get("live_ready"),
                    "mock_ready": provider.get("mock_ready"),
                    "recent": provider.get("recent", {}),
                }
                for provider in providers
            ],
            "customer_count": len(customers),
        },
        "evidence_endpoints": [
            "/health",
            "/v1/gateway/alerts",
            "/v1/gateway/provider-health",
            "/v1/gateway/request-activity",
            "/v1/gateway/request-detail",
            "/v1/gateway/customer-reports",
            "/v1/gateway/invoice-preview",
            "/v1/gateway/incident-playbook",
            "/v1/gateway/support-policy",
        ],
        "next_best_action": "Use this runbook during a customer pilot review to decide who watches the gateway and what happens when a signal turns bad.",
        "prototype_note": "This is an operations runbook for demos and pilots, not a legal SLA. Production still needs real monitoring, alert routing, on-call ownership, retention policy, and legal SLA terms.",
    }


def support_policy(server):
    readiness = production_readiness(server)
    playbook = incident_playbook(server)
    return {
        "object": "gateway.support_policy",
        "mode": "mock" if server.mock_mode else "live",
        "policy_status": "prototype",
        "plain_english": (
            "The current gateway can support customer demos and technical pilots, "
            "but it is not a legal production SLA."
        ),
        "service_levels": [
            {
                "stage": "prototype_demo",
                "goal": "Explain the model gateway concept and test local mock flows.",
                "availability_target": "best_effort",
                "response_time_target": "same business day when actively demoing",
                "included": [
                    "Mock requests",
                    "Customer-facing dashboard",
                    "OpenAPI and Postman handoff",
                    "Route preview",
                    "Readiness and incident playbook explanation",
                ],
                "not_included": [
                    "Legal uptime commitment",
                    "Automatic failover guarantee",
                    "Production billing guarantee",
                    "Provider SLA pass-through",
                ],
            },
            {
                "stage": "technical_pilot",
                "goal": "Let a small customer group test controlled live or mock usage.",
                "availability_target": "business-hours support target",
                "response_time_target": "P1 same business day, P2 next business day, P3 planned backlog",
                "included": [
                    "Named customer keys",
                    "Budget and limit controls",
                    "Request tracing with gateway.request_id",
                    "Provider readiness checks",
                    "Manual fallback review",
                ],
                "not_included": [
                    "24/7 support",
                    "Guaranteed latency",
                    "Automatic incident paging",
                    "Final invoice or tax workflow",
                ],
            },
            {
                "stage": "production_target",
                "goal": "Future state for real customer traffic after production hardening.",
                "availability_target": "to be agreed in customer contract",
                "response_time_target": "to be agreed by severity level",
                "included": [
                    "Secret manager",
                    "Database-backed customer and route config",
                    "Approval workflow",
                    "Provider contract tests",
                    "Monitoring and alert routing",
                    "Billing rules",
                    "Operational runbooks",
                ],
                "not_included": [
                    "Available in the current single-file prototype",
                    "Provider uptime beyond upstream provider agreements",
                ],
            },
        ],
        "severity_levels": [
            {
                "severity": "P1",
                "meaning": "Customer production traffic is blocked or many requests are failing.",
                "first_checks": [
                    "/v1/gateway/alerts",
                    "/v1/gateway/provider-health",
                    "/v1/gateway/request-activity?status=error",
                ],
                "target_action": "Triage provider health, budget blocks, and recent errors before changing routes.",
            },
            {
                "severity": "P2",
                "meaning": "One customer or one model route has degraded behavior.",
                "first_checks": [
                    "/v1/gateway/request-detail?request_id=...",
                    "/v1/gateway/model-catalog",
                    "/v1/gateway/route-preview",
                ],
                "target_action": "Use request_id and route preview to explain the specific route decision.",
            },
            {
                "severity": "P3",
                "meaning": "Question, onboarding, documentation, or planned provider work.",
                "first_checks": [
                    "/v1/gateway/demo-bundle",
                    "/v1/gateway/integration-guide",
                    "/v1/gateway/provider-contracts",
                ],
                "target_action": "Use docs and contract matrix to plan the next change.",
            },
        ],
        "escalation_path": [
            "Sales or support collects customer, public model name, timestamp, and gateway.request_id if available.",
            "Operator checks incident playbook and alerts.",
            "Engineer checks provider contract, route decision, request detail, and recent request activity.",
            "Business owner approves budget, pricing, or SLA changes.",
        ],
        "customer_safe_words": [
            "The gateway keeps the customer API stable while we inspect the upstream route.",
            "We can trace the request using gateway.request_id without exposing provider secrets.",
            "The prototype shows the operating model, but production SLA terms must be agreed separately.",
        ],
        "current_readiness": {
            "overall_status": readiness.get("overall_status"),
            "prototype_only": readiness.get("prototype_only"),
            "readiness_endpoint": "/v1/gateway/production-readiness",
            "incident_playbook_endpoint": "/v1/gateway/incident-playbook",
            "active_alerts": playbook.get("summary", {}).get("active_alerts", {}),
        },
    }


def pilot_checklist(server):
    readiness = production_readiness(server)
    support = support_policy(server)
    return {
        "object": "gateway.pilot_checklist",
        "mode": "mock" if server.mock_mode else "live",
        "pilot_stage": "pre_pilot",
        "plain_english": "Use this checklist before inviting a customer to test the gateway.",
        "recommended_scope": {
            "duration": "1 to 2 weeks for a first technical pilot",
            "customers": "1 small customer team or internal champion",
            "models": ["smart-fast"],
            "traffic": "low-volume test traffic first",
            "mode": "mock first, live Qwen only when a real provider test is needed",
        },
        "roles": [
            {
                "role": "Business owner",
                "responsibility": "Confirms pilot goal, customer success criteria, and commercial boundaries.",
            },
            {
                "role": "Technical owner",
                "responsibility": "Runs gateway, checks provider readiness, and handles route or adapter issues.",
            },
            {
                "role": "Customer technical contact",
                "responsibility": "Tests API calls using OpenAPI, Postman, or integration guide examples.",
            },
            {
                "role": "Support contact",
                "responsibility": "Collects request_id, checks incident playbook, and shares customer-safe updates.",
            },
        ],
        "phases": [
            {
                "phase": "Before pilot",
                "goal": "Make sure the customer can test safely.",
                "checks": [
                    "Confirm pilot goal and success criteria.",
                    "Issue or confirm customer gateway key.",
                    "Confirm allowed models and default policy.",
                    "Confirm request, token, and cost limits.",
                    "Share integration guide, OpenAPI, and Postman collection.",
                    "Run production readiness and explain prototype-only gaps.",
                    "Confirm support policy and escalation contact.",
                ],
                "evidence": [
                    "/v1/gateway/integration-guide",
                    "/openapi.json",
                    "/postman_collection.json",
                    "/v1/gateway/production-readiness",
                    "/v1/gateway/support-policy",
                ],
            },
            {
                "phase": "During pilot",
                "goal": "Observe usage and handle issues with traceable evidence.",
                "checks": [
                    "Use request_id for every reported issue.",
                    "Review request activity and request detail.",
                    "Watch customer usage, budget state, and provider health.",
                    "Use route preview before changing routing controls.",
                    "Use incident playbook for customer-safe wording.",
                ],
                "evidence": [
                    "/v1/gateway/request-activity",
                    "/v1/gateway/request-detail?request_id=...",
                    "/v1/gateway/customer-reports",
                    "/v1/gateway/provider-health",
                    "/v1/gateway/incident-playbook",
                ],
            },
            {
                "phase": "After pilot",
                "goal": "Decide whether to stop, extend, or productionize.",
                "checks": [
                    "Review request count, errors, latency, tokens, and estimated cost.",
                    "Review which models and providers were used.",
                    "Confirm whether fallback, BYOK, billing, and support expectations were clear.",
                    "List production blockers and owner for each blocker.",
                    "Decide next action: stop, extend pilot, or plan production hardening.",
                ],
                "evidence": [
                    "/v1/gateway/customer-reports",
                    "/v1/gateway/invoice-preview",
                    "/v1/gateway/model-usage",
                    "/v1/gateway/provider-contracts",
                    "/v1/gateway/production-readiness",
                ],
            },
        ],
        "success_criteria": [
            "Customer can list models and send a chat request.",
            "Customer understands public model names versus upstream provider models.",
            "Support can trace a request using gateway.request_id.",
            "Usage and budget reports are understandable.",
            "Production gaps are documented before live customer traffic.",
        ],
        "exit_decision": {
            "stop": "Customer value is unclear or provider contract risk is too high.",
            "extend_pilot": "Customer value is clear but usage, routing, or support questions need more testing.",
            "productionize": "Customer value is clear and production readiness gaps have named owners.",
        },
        "current_context": {
            "readiness_status": readiness.get("overall_status"),
            "support_policy_status": support.get("policy_status"),
            "demo_bundle": "/v1/gateway/demo-bundle",
        },
    }


def pilot_scorecard(server):
    reports = customer_reports(server)
    success = customer_success_summary(server)
    readiness = production_readiness(server)
    scorecards = []
    for report in reports:
        summary = report.get("request_summary", {})
        budget = report.get("budget", {})
        usage = budget.get("usage", {})
        request_count = int(summary.get("requests") or 0)
        error_rate = float(summary.get("error_rate") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        has_allowed_models = bool(report.get("allowed_models"))
        has_traceable_request = bool(report.get("recent_requests"))
        budget_state = report.get("budget_state")

        criteria = [
            {
                "name": "First request completed",
                "status": "passed" if request_count > 0 else "not_started",
                "score": 20 if request_count > 0 else 0,
                "evidence": "/v1/gateway/customer-reports",
            },
            {
                "name": "Request tracing available",
                "status": "passed" if has_traceable_request else "not_started",
                "score": 15 if has_traceable_request else 0,
                "evidence": "/v1/gateway/request-detail?request_id=...",
            },
            {
                "name": "Error rate acceptable",
                "status": "passed" if request_count > 0 and error_rate < 0.2 else "needs_work" if request_count > 0 else "not_started",
                "score": 20 if request_count > 0 and error_rate < 0.2 else 8 if request_count > 0 else 0,
                "evidence": "/v1/gateway/request-activity",
            },
            {
                "name": "Budget still usable",
                "status": "passed" if budget_state in {"ok", "unlimited"} else "needs_work",
                "score": 15 if budget_state in {"ok", "unlimited"} else 5,
                "evidence": "/v1/gateway/customer-reports",
            },
            {
                "name": "Model access configured",
                "status": "passed" if has_allowed_models else "blocked",
                "score": 15 if has_allowed_models else 0,
                "evidence": "/v1/gateway/access-matrix",
            },
            {
                "name": "Production gaps acknowledged",
                "status": "passed" if readiness.get("prototype_only") else "needs_work",
                "score": 15 if readiness.get("prototype_only") else 5,
                "evidence": "/v1/gateway/production-readiness",
            },
        ]
        total_score = sum(item["score"] for item in criteria)
        if total_score >= 80 and request_count > 0 and error_rate < 0.2:
            decision = "plan_production_hardening"
            next_action = "Use production backlog and launch plan to decide what must be funded before production."
        elif total_score >= 50:
            decision = "extend_pilot"
            next_action = "Run more mock or live Qwen tests and review unresolved criteria."
        else:
            decision = "keep_in_discovery"
            next_action = "Confirm use case, model access, and first request before expanding the pilot."
        scorecards.append(
            {
                "customer_id": report.get("id"),
                "customer_name": report.get("name"),
                "score": total_score,
                "decision": decision,
                "next_action": next_action,
                "metrics": {
                    "requests": request_count,
                    "errors": int(summary.get("errors") or 0),
                    "error_rate": error_rate,
                    "total_tokens": total_tokens,
                    "budget_state": budget_state,
                },
                "criteria": criteria,
            }
        )

    decisions = {}
    for card in scorecards:
        decisions[card["decision"]] = decisions.get(card["decision"], 0) + 1
    return {
        "object": "gateway.pilot_scorecard",
        "title": "AISmallRouter Pilot Scorecard",
        "audience": "business owner, customer success, support owner, and customer technical owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This scorecard helps decide whether a customer pilot should stay in discovery, continue testing, or move toward production hardening.",
        "scoring": {
            "max_score": 100,
            "decision_rules": [
                "80 or higher with successful requests: plan production hardening.",
                "50 to 79: extend the pilot and close weak criteria.",
                "Below 50: keep the customer in discovery before expanding scope.",
            ],
        },
        "summary": {
            "customers": len(scorecards),
            "average_score": round(sum(card["score"] for card in scorecards) / len(scorecards), 2) if scorecards else 0,
            "decisions": decisions,
            "customer_success_totals": success.get("totals", {}),
        },
        "scorecards": scorecards,
        "reference_endpoints": [
            "/v1/gateway/pilot-checklist",
            "/v1/gateway/customer-reports",
            "/v1/gateway/customer-success",
            "/v1/gateway/request-activity",
            "/v1/gateway/production-readiness",
            "/v1/gateway/production-backlog",
        ],
        "next_best_action": "Review the lowest-scoring criteria with the customer before changing scope or promising production.",
    }


def evaluation_plan(server):
    catalog = model_catalog(server)
    health_rows = {row["id"]: row for row in provider_health(server)}
    model_rows = []
    for model in catalog:
        provider_id = model.get("provider")
        health = health_rows.get(provider_id, {})
        recent = model.get("usage", {})
        score = 0
        criteria = []

        def add(name, passed, points, evidence):
            nonlocal score
            if passed:
                score += points
            criteria.append(
                {
                    "criterion": name,
                    "passed": bool(passed),
                    "points": points if passed else 0,
                    "max_points": points,
                    "evidence": evidence,
                }
            )

        add("Chat route exists", True, 20, f"Public model {model.get('id')} routes to {model.get('upstream_model')}.")
        add("Provider is mock-ready", bool(health.get("mock_ready")), 15, f"Provider status is {health.get('status', 'unknown')}.")
        add("Provider is live-ready", bool(health.get("live_ready")), 15, f"Live-ready is {health.get('live_ready', False)}.")
        add("Streaming capability known", "streaming" in model.get("capabilities", []), 10, f"Capabilities: {', '.join(model.get('capabilities', [])) or 'none'}.")
        add("Fallback route available", bool(model.get("fallback_models")), 10, f"Fallback chain: {', '.join(model.get('fallback_models', [])) or 'none'}.")
        add("Recent demo traffic exists", int(recent.get("requests") or 0) > 0, 10, f"Recent requests: {recent.get('requests', 0)}.")
        add("Error rate acceptable", float(recent.get("error_rate") or 0) <= 0.05, 10, f"Recent error rate: {recent.get('error_rate', 0)}.")
        add("Pricing metadata present", bool(model.get("pricing")), 10, "Pricing metadata exists." if model.get("pricing") else "Pricing metadata missing.")

        if score >= 80:
            decision = "pilot_ready"
            next_action = "Use this model in a controlled customer pilot with agreed success criteria."
        elif score >= 55:
            decision = "needs_more_evidence"
            next_action = "Run more mock or live tests before using this model with a customer."
        else:
            decision = "discovery_only"
            next_action = "Keep this model in discovery until provider, route, fallback, and usage evidence improve."

        model_rows.append(
            {
                "model": model.get("id"),
                "provider": provider_id,
                "score": score,
                "decision": decision,
                "next_action": next_action,
                "criteria": criteria,
            }
        )

    return {
        "object": "gateway.evaluation_plan",
        "title": "AISmallRouter Model Evaluation Plan",
        "audience": "business owner, customer technical owner, data owner, support owner, and gateway owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This plan explains how to compare models before routing real customer traffic. It keeps quality, speed, cost, safety, and support evidence visible.",
        "evaluation_dimensions": [
            {"name": "Task quality", "question": "Does the model answer the customer's real use case well?", "example_evidence": "Golden prompts, expected answer notes, human review, and customer acceptance."},
            {"name": "Reliability", "question": "Does the route work repeatedly without confusing errors?", "example_evidence": "Request activity, request detail, provider health, and incident playbook."},
            {"name": "Latency", "question": "Is the response fast enough for the customer workflow?", "example_evidence": "Recent average latency and route strategy tests."},
            {"name": "Cost", "question": "Is the model affordable under the customer's budget?", "example_evidence": "Cost estimate, invoice preview, and customer budget state."},
            {"name": "Safety and data handling", "question": "Can the request be sent without exposing sensitive data?", "example_evidence": "Safety preview, data governance review, and security review."},
            {"name": "Fallback behavior", "question": "What happens if the first provider is unavailable?", "example_evidence": "Fallback route, forced fallback test, and provider contract review."},
        ],
        "sample_eval_set": [
            {"id": "basic_answer", "prompt": "Explain this gateway in one short sentence.", "checks": ["answer is concise", "mentions one API", "mentions model routing"]},
            {"id": "customer_support", "prompt": "A customer says their request failed. Explain the first checks.", "checks": ["mentions request_id", "mentions request activity", "uses customer-safe wording"]},
            {"id": "cost_control", "prompt": "Explain how token and cost budgets protect a customer.", "checks": ["mentions token budget", "mentions cost budget", "does not claim legal billing"]},
            {"id": "security_boundary", "prompt": "Can customers see provider API keys?", "checks": ["says no", "mentions gateway keys", "mentions provider secrets stay private"]},
            {"id": "fallback_reasoning", "prompt": "Explain why fallback routing matters.", "checks": ["mentions provider failure", "mentions alternate model", "mentions policy"]},
        ],
        "scoring_rules": {
            "max_score": 100,
            "decision_rules": [
                "80 or higher: pilot-ready if customer use case is approved.",
                "55 to 79: collect more evidence before customer pilot.",
                "Below 55: discovery-only until route, provider, and usage evidence improve.",
            ],
            "human_review_note": "Automated signals are not enough. A customer or domain owner should review quality for real use cases.",
        },
        "model_scorecards": model_rows,
        "recommended_eval_flow": [
            "Start with mock mode and the sample eval set.",
            "Run the same prompts through each candidate model route.",
            "Record answer quality, latency, errors, token usage, and estimated cost.",
            "Review safety preview and data governance before live prompts.",
            "Use route preview to confirm fallback and provider policy.",
            "Promote only pilot-ready models into customer-facing docs.",
        ],
        "evidence_endpoints": [
            "/v1/gateway/model-catalog",
            "/v1/gateway/provider-health",
            "/v1/gateway/request-activity",
            "/v1/gateway/request-detail",
            "/v1/gateway/cost-estimate",
            "/v1/gateway/safety-preview",
            "/v1/gateway/security-review",
            "/v1/gateway/route-preview",
        ],
        "next_best_action": "Use this evaluation plan before adding a new provider or promising quality-based routing to a customer.",
        "prototype_note": "This is an evaluation plan and scoring explanation. It is not a full offline evaluation platform, benchmark suite, or automatic model ranking system yet.",
    }


def handoff_checklist(server):
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    return {
        "object": "gateway.handoff_checklist",
        "title": "AISmallRouter Customer Handoff Checklist",
        "audience": "business owner, customer technical owner, support owner, and gateway owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This checklist shows what to share with a customer, who should read it, and what should stay internal or admin-only.",
        "handoff_groups": [
            {
                "group": "Business overview",
                "owner": "Business owner",
                "share_with_customer": True,
                "items": [
                    {"name": "Visual dashboard", "url": f"{base_url}/", "purpose": "Explain the gateway concept visually."},
                    {"name": "Customer guide PDF", "path": "Model_Gateway_Customer_Guide.pdf", "purpose": "Simple English explanation of direction, architecture, and hard parts."},
                    {"name": "Proposal summary", "url": f"{base_url}/v1/gateway/proposal-summary", "purpose": "Explain phase one scope, exclusions, risks, and next steps."},
                    {"name": "Discovery checklist", "url": f"{base_url}/v1/gateway/discovery-checklist", "purpose": "Confirm scope before promising an OpenRouter-like platform."},
                ],
            },
            {
                "group": "Customer technical handoff",
                "owner": "Customer technical owner",
                "share_with_customer": True,
                "items": [
                    {"name": "OpenAPI contract", "url": f"{base_url}/openapi.json", "purpose": "Inspect or generate client code from the API shape."},
                    {"name": "Postman collection", "url": f"{base_url}/postman_collection.json", "purpose": "Click through demo requests."},
                    {"name": "Customer integration guide", "url": f"{base_url}/v1/gateway/integration-guide", "purpose": "Customer-key examples for curl, Python, JavaScript, and streaming."},
                    {"name": "Customer SDK starter", "url": f"{base_url}/v1/gateway/sdk-starter", "purpose": "Starter files, .env template, first-run commands, and common errors."},
                ],
            },
            {
                "group": "Pilot decision package",
                "owner": "Customer success owner",
                "share_with_customer": True,
                "items": [
                    {"name": "Pilot checklist", "url": f"{base_url}/v1/gateway/pilot-checklist", "purpose": "Prepare before, during, and after a pilot."},
                    {"name": "Pilot scorecard", "url": f"{base_url}/v1/gateway/pilot-scorecard", "purpose": "Decide discovery, extended pilot, or production hardening."},
                    {"name": "Customer success summary", "url": f"{base_url}/v1/gateway/customer-success", "purpose": "Review customer health and follow-up actions."},
                    {"name": "Customer reports", "url": f"{base_url}/v1/gateway/customer-reports", "purpose": "Show usage, errors, tokens, budget, and recent requests."},
                ],
            },
            {
                "group": "Internal readiness package",
                "owner": "Gateway owner",
                "share_with_customer": False,
                "items": [
                    {"name": "Production readiness", "url": f"{base_url}/v1/gateway/production-readiness", "purpose": "Explain demo-ready versus production-ready gaps."},
                    {"name": "Deployment readiness", "url": f"{base_url}/v1/gateway/deployment-readiness", "purpose": "Review environment, preflight, operations, rollback, and deployment options."},
                    {"name": "Production backlog", "url": f"{base_url}/v1/gateway/production-backlog", "purpose": "Prioritize P0, P1, and P2 hardening work."},
                    {"name": "Change management", "url": f"{base_url}/v1/gateway/change-management", "purpose": "Review approval and rollback for config changes."},
                    {"name": "Data governance", "url": f"{base_url}/v1/gateway/data-governance", "purpose": "Review prompt handling, logs, retention, customer keys, and provider secrets."},
                ],
            },
        ],
        "handoff_rules": [
            "Do not send provider API keys to the customer.",
            "Use customer gateway keys only for customer-facing requests.",
            "Admin endpoints are for internal demo, support, and readiness review unless explicitly approved.",
            "Use mock mode first when explaining the flow.",
            "Move to live Qwen only after data handling and provider key strategy are understood.",
        ],
        "before_customer_meeting": [
            "Open the dashboard and confirm it loads.",
            "Run /v1/models with the customer key.",
            "Run one mock chat request and save gateway.request_id.",
            "Open the proposal summary and pilot scorecard.",
            "Confirm which links are customer-shareable and which are admin-only.",
        ],
        "after_customer_meeting": [
            "Record whether the customer wants discovery, extended pilot, or production hardening.",
            "Update the proposal summary scope if the first use case changed.",
            "Review pilot scorecard weak criteria.",
            "Use production backlog only if the customer wants production commitment.",
        ],
        "next_best_action": "Use this checklist as the meeting handoff page before sending technical artifacts to a customer.",
    }


def executive_brief(server):
    readiness = production_readiness(server)
    support = support_policy(server)
    pilot = pilot_checklist(server)
    return {
        "object": "gateway.executive_brief",
        "title": "AISmallRouter Executive Brief",
        "audience": "business and non-technical customer stakeholders",
        "mode": "mock" if server.mock_mode else "live",
        "one_sentence": "AISmallRouter gives customers one simple model API while the gateway manages provider routing, customer controls, usage, and support evidence behind the scenes.",
        "why_it_matters": [
            "Customers do not need to learn every provider API.",
            "The platform can hide upstream model and provider changes behind public model names.",
            "Support can trace requests with gateway.request_id.",
            "Business teams can explain cost, access, pilot scope, and production gaps clearly.",
        ],
        "what_the_demo_proves": [
            "One OpenAI-compatible customer API.",
            "Model alias routing from smart-fast to Qwen.",
            "Customer keys, limits, allowed models, and budget views.",
            "Provider readiness, route preview, fallback, and policy controls.",
            "OpenAPI, Postman, customer guide PDF, and integration guide handoff.",
        ],
        "what_is_not_production_yet": [
            "No legal production SLA.",
            "No production secret manager or customer database.",
            "No automated incident paging.",
            "Provider contracts still need live tests before broad rollout.",
            "Billing is an estimate, not a legal invoice.",
        ],
        "recommended_customer_story": [
            "Start with the dashboard and explain one API for many providers.",
            "Show /v1/models and /v1/gateway/me to explain customer access.",
            "Show route preview and provider contracts to explain why this is more than an API Gateway.",
            "Show production readiness, support policy, and pilot checklist to set honest expectations.",
            "Show the onboarding plan to turn the discussion into a small 5 day pilot path.",
            "Give the technical team OpenAPI, Postman, and the integration guide.",
        ],
        "pilot_recommendation": pilot.get("recommended_scope", {}),
        "support_position": {
            "policy_status": support.get("policy_status"),
            "plain_english": support.get("plain_english"),
            "support_policy": "/v1/gateway/support-policy",
        },
        "readiness_position": {
            "overall_status": readiness.get("overall_status"),
            "prototype_only": readiness.get("prototype_only"),
            "production_readiness": "/v1/gateway/production-readiness",
        },
        "customer_next_step": "Run a small technical pilot with mock mode first, then test live Qwen only when the customer needs a real provider result.",
        "internal_next_step": "Name owners for production readiness gaps before promising live customer traffic.",
    }


def discovery_checklist(server):
    return {
        "object": "gateway.discovery_checklist",
        "title": "AISmallRouter Customer Discovery Checklist",
        "audience": "business owner, sales, solution architect, customer success, and customer technical owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "Use this checklist before building or promising an OpenRouter-like gateway. It turns a broad customer idea into clear scope, risks, and next steps.",
        "why_it_matters": [
            "A customer may say they need one AI API, but they may actually need governance, billing, model choice, privacy, fallback, or only a simple proxy.",
            "A normal API Gateway may be enough for simple HTTP control, but it does not solve provider adapters, usage records, model policy, or customer-specific model access.",
            "A small discovery step prevents over-promising production readiness too early.",
        ],
        "discovery_sections": [
            {
                "section": "Customer goal",
                "plain_english": "Find the real business reason for the gateway.",
                "questions": [
                    "What customer workflow will call the gateway first?",
                    "Is the goal cost control, model choice, private model access, compliance, fallback, or simpler integration?",
                    "Who will decide whether the pilot worked?",
                ],
                "evidence_to_collect": ["first use case", "pilot owner", "success metric"],
                "red_flags": ["No named use case", "No pilot owner", "Customer expects production SLA immediately"],
            },
            {
                "section": "Model and provider scope",
                "plain_english": "List the first models and providers before discussing a marketplace.",
                "questions": [
                    "Which provider must work first: Alibaba Cloud Model Studio, OpenAI, Claude, Xiaomi, or another provider?",
                    "Are public model aliases enough, or does the customer need to choose exact upstream models?",
                    "Does the customer need streaming, tool calling, embeddings, images, or only chat?",
                ],
                "evidence_to_collect": ["provider list", "required capabilities", "first public model names"],
                "red_flags": ["Too many providers in phase one", "Unknown provider docs", "No agreed first model"],
            },
            {
                "section": "Customer access and tenant rules",
                "plain_english": "Understand who can use which model and under what limits.",
                "questions": [
                    "How many customer teams or tenants will use the gateway?",
                    "Does each customer need separate API keys, budgets, and allowed models?",
                    "Does the customer bring their own provider key or use a shared provider key?",
                ],
                "evidence_to_collect": ["tenant list", "allowed model matrix", "BYOK decision"],
                "red_flags": ["Shared keys across customers", "No tenant isolation expectation", "Unclear owner for key rotation"],
            },
            {
                "section": "Data and governance",
                "plain_english": "Agree what can be logged, retained, shown, or deleted.",
                "questions": [
                    "Can prompts and responses be stored for support?",
                    "What is the retention period for request logs, usage logs, and audit logs?",
                    "What sensitive data should be blocked, redacted, or allowed?",
                ],
                "evidence_to_collect": ["logging policy", "retention policy", "sensitive data examples"],
                "red_flags": ["No retention answer", "PII expected in prompts", "No rule for customer data deletion"],
            },
            {
                "section": "Commercial and reporting",
                "plain_english": "Clarify whether the gateway must support usage reports, budgets, or invoice preview.",
                "questions": [
                    "Does the customer need internal chargeback, customer billing, or only usage visibility?",
                    "Which numbers matter: requests, tokens, model cost, error rate, latency, or all of them?",
                    "Who receives usage reports and how often?",
                ],
                "evidence_to_collect": ["pricing model", "report audience", "budget rule"],
                "red_flags": ["Need billing but no pricing rule", "No cost owner", "Provider cost units are not understood"],
            },
            {
                "section": "Production expectation",
                "plain_english": "Separate a demo, a pilot, and a production service.",
                "questions": [
                    "Is this for a demo, a small pilot, limited production, or full production?",
                    "What uptime, support hours, incident response, and rollback expectations exist?",
                    "Who approves provider changes, model route changes, and customer key changes?",
                ],
                "evidence_to_collect": ["support stage", "approval owner", "rollback expectation"],
                "red_flags": ["Production traffic before secret manager and monitoring", "No support owner", "No approval path"],
            },
        ],
        "fit_assessment": [
            {
                "fit": "Normal API Gateway may be enough",
                "when": "Customer only needs auth, rate limits, HTTP routing, and logs for one provider with one stable API shape.",
                "next_step": "Use the decision guide before building custom model routing.",
            },
            {
                "fit": "Managed AI Gateway may be enough",
                "when": "Customer accepts a hosted gateway, standard provider integrations, and platform-level cost tracking.",
                "next_step": "Compare managed AI gateway features against privacy, region, and customer ownership needs.",
            },
            {
                "fit": "Custom Model Gateway is useful",
                "when": "Customer needs private customer controls, model aliases, provider adapter logic, BYOK, audit, custom reports, or special rollout rules.",
                "next_step": "Start with one customer, one provider, one or two public models, and mock mode.",
            },
            {
                "fit": "OpenRouter-like marketplace is later",
                "when": "Customer needs many providers, model discovery, pricing comparison, public model marketplace behavior, and broader provider operations.",
                "next_step": "Do not start here. Prove the private gateway control layer first.",
            },
        ],
        "recommended_first_pilot": {
            "scope": "one customer, one use case, one provider, one or two public model aliases",
            "first_provider": "Alibaba Cloud Model Studio / Qwen if this is the only available API key",
            "mode": "mock first, live only after route and data handling are explained",
            "success_evidence": [
                "/v1/models works with customer key",
                "/v1/chat/completions returns an OpenAI-compatible response",
                "/v1/gateway/route-preview explains the route",
                "/v1/gateway/customer-reports shows usage",
                "/v1/gateway/data-governance explains data handling gaps",
            ],
        },
        "reference_endpoints": [
            "/v1/gateway/decision-guide",
            "/v1/gateway/provider-contracts",
            "/v1/gateway/data-governance",
            "/v1/gateway/onboarding-plan",
            "/v1/gateway/pilot-checklist",
            "/v1/gateway/production-readiness",
        ],
        "next_best_action": "Use this checklist in the first customer meeting, then create a small Day 0 onboarding plan only after the first use case and provider scope are clear.",
    }


def proposal_summary(server):
    discovery = discovery_checklist(server)
    readiness = production_readiness(server)
    launch = launch_plan(server)
    governance = data_governance_review(server)
    return {
        "object": "gateway.proposal_summary",
        "title": "AISmallRouter Customer Proposal Summary",
        "audience": "customer sponsor, business owner, solution architect, and technical owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This is a simple customer-facing scope summary. It explains what the first gateway pilot should include, what it should not promise yet, and what must be decided before production.",
        "customer_problem": "The customer wants one controlled API for AI model access instead of integrating each provider separately.",
        "recommended_positioning": [
            "Start as a private customer-owned Model Gateway, not a public model marketplace.",
            "Use OpenAI-compatible customer API shape for easier adoption.",
            "Use Alibaba Cloud Model Studio / Qwen first if that is the available live provider key.",
            "Keep OpenAI, Claude, Xiaomi, and other providers as planned adapter work after the first pilot is clear.",
            "Use mock mode first so business and technical teams can understand routing without spending provider credits.",
        ],
        "phase_one_scope": [
            "One customer or internal pilot team.",
            "One use case and one owner who can decide if the pilot worked.",
            "One provider path first, with Qwen / DashScope as the practical live option.",
            "One or two public model aliases such as smart-fast.",
            "Customer gateway key, allowed models, request limit, token budget, and cost budget.",
            "OpenAI-compatible /v1/models and /v1/chat/completions.",
            "Dashboard, route preview, request trace, customer report, and simple invoice preview.",
            "Data governance review and production readiness review before live production traffic.",
        ],
        "not_in_phase_one": [
            "Public OpenRouter-style marketplace.",
            "Full production SLA.",
            "Automatic legal or compliance certification.",
            "Many providers added at the same time.",
            "Guaranteed fallback across providers without provider contract tests.",
            "Final billing engine or finance system integration.",
        ],
        "customer_deliverables": [
            {"name": "Visual dashboard", "purpose": "Explain request routing and gateway controls."},
            {"name": "OpenAPI contract", "purpose": "Let technical users inspect the API shape."},
            {"name": "Postman collection", "purpose": "Click through customer and admin demo requests."},
            {"name": "Customer guide PDF", "purpose": "Explain direction, architecture, and technical difficulties in simple English."},
            {"name": "Discovery checklist", "purpose": "Confirm use case, provider scope, data rules, reporting, and production expectations."},
            {"name": "Pilot checklist", "purpose": "Keep the test small and measurable."},
        ],
        "decision_points": [
            {
                "decision": "Build custom gateway or use managed gateway",
                "how_to_decide": "Use the decision guide. Choose custom gateway when private customer controls, model aliases, BYOK, audit, custom reports, or special rollout rules matter.",
                "evidence": "/v1/gateway/decision-guide",
            },
            {
                "decision": "First provider",
                "how_to_decide": "Start with the provider key that is actually available. For this prototype, that is Alibaba Cloud Model Studio / Qwen.",
                "evidence": "/v1/gateway/provider-contracts",
            },
            {
                "decision": "Mock or live test",
                "how_to_decide": "Use mock mode for explanation. Use live mode only when the customer needs real model output and data handling is agreed.",
                "evidence": "/v1/gateway/data-governance",
            },
            {
                "decision": "Pilot or production",
                "how_to_decide": "Treat the current project as pilot-ready discussion material. Production needs stronger secrets, database, monitoring, billing, approval workflow, and support ownership.",
                "evidence": "/v1/gateway/production-readiness",
            },
        ],
        "main_risks": [
            {
                "risk": "Customer expects full OpenRouter-like marketplace immediately.",
                "mitigation": "Position marketplace behavior as a later phase after private gateway controls are proven.",
            },
            {
                "risk": "Provider APIs differ even when they look OpenAI-compatible.",
                "mitigation": "Use provider contracts and adapter tests before adding providers.",
            },
            {
                "risk": "Prompt logs or request details include sensitive customer data.",
                "mitigation": "Agree logging, retention, redaction, access, and deletion rules before production.",
            },
            {
                "risk": "Prototype is mistaken for production service.",
                "mitigation": "Show launch plan, support policy, and readiness gaps before live customer traffic.",
            },
        ],
        "recommended_next_steps": [
            "Run the discovery checklist with one customer champion.",
            "Confirm the first use case, first provider, and first public model alias.",
            "Show the dashboard and run one mock chat request.",
            "Review data governance and production readiness before any live production promise.",
            "Use the onboarding plan for a 5 working day technical pilot.",
        ],
        "reference_endpoints": [
            "/v1/gateway/discovery-checklist",
            "/v1/gateway/demo-bundle",
            "/v1/gateway/decision-guide",
            "/v1/gateway/provider-contracts",
            "/v1/gateway/data-governance",
            "/v1/gateway/onboarding-plan",
            "/v1/gateway/pilot-checklist",
            "/v1/gateway/production-readiness",
            "/v1/gateway/launch-plan",
        ],
        "current_context": {
            "recommended_first_pilot": discovery.get("recommended_first_pilot"),
            "readiness_status": readiness.get("overall_status"),
            "launch_decision": launch.get("decision"),
            "governance_next_action": governance.get("recommended_next_action"),
        },
        "customer_safe_close": "The first useful step is not to build every provider. The first useful step is to prove one controlled model access path that the customer, support team, and technical team can all understand.",
    }


def deployment_readiness(server):
    config = gateway_config_check(server)
    readiness = production_readiness(server)
    return {
        "object": "gateway.deployment_readiness",
        "title": "AISmallRouter Deployment Readiness Guide",
        "audience": "customer technical owner, gateway owner, platform owner, and security owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This guide explains what must be prepared before moving from a local demo to a customer pilot or production deployment.",
        "deployment_stages": [
            {
                "stage": "local_demo",
                "purpose": "Explain the gateway idea without paid provider spend.",
                "run_command": "python3 model_gateway.py --mock --port 8795",
                "data_store": "local SQLite and local JSON files",
                "good_for": ["customer explanation", "mock route demo", "documentation review"],
                "not_good_for": ["production traffic", "real SLA", "shared customer secrets"],
            },
            {
                "stage": "live_qwen_test",
                "purpose": "Call Alibaba Cloud Model Studio / Qwen for a small controlled test.",
                "run_command": "DASHSCOPE_API_KEY=... python3 model_gateway.py --port 8795",
                "data_store": "local SQLite and local JSON files",
                "good_for": ["one internal live test", "latency and response quality check"],
                "not_good_for": ["multi-customer production", "long-term secret storage"],
            },
            {
                "stage": "technical_pilot",
                "purpose": "Let one customer or internal team test the API with limits and reports.",
                "run_command": "Run behind a controlled internal service endpoint with admin key and customer keys replaced.",
                "data_store": "database preferred; local files acceptable only for a short private pilot",
                "good_for": ["one use case", "small request volume", "measured pilot"],
                "not_good_for": ["public marketplace", "unbounded traffic", "contracted SLA"],
            },
            {
                "stage": "production_target",
                "purpose": "Run real customer traffic with operational ownership.",
                "run_command": "Deploy as a managed service with database, secret manager, monitoring, backups, and rollback workflow.",
                "data_store": "production database, secret manager, log retention, and backup policy",
                "good_for": ["approved customer traffic", "supportable operations"],
                "not_good_for": ["unreviewed provider adapters", "unknown billing rules", "unapproved data retention"],
            },
        ],
        "required_environment": [
            {
                "name": "GATEWAY_ADMIN_API_KEY",
                "required_for": "admin endpoints",
                "prototype_default": DEFAULT_ADMIN_API_KEY,
                "production_rule": "Must be changed before any shared environment.",
            },
            {
                "name": "customer gateway API keys",
                "required_for": "customer calls to /v1/models and /v1/chat/completions",
                "prototype_default": "dev-gateway-key and demo keys",
                "production_rule": "Issue per customer, store safely, rotate, and disable when needed.",
            },
            {
                "name": "DASHSCOPE_API_KEY",
                "required_for": "live Alibaba Cloud Model Studio / Qwen calls",
                "prototype_default": "not required in mock mode",
                "production_rule": "Store in secret manager or customer BYOK mapping, not in repo files.",
            },
            {
                "name": "model_registry.json",
                "required_for": "provider configs and public model routes",
                "prototype_default": "local file",
                "production_rule": "Move to database or controlled config store with approvals and rollback.",
            },
            {
                "name": "customer_keys.json",
                "required_for": "customer access, budgets, and BYOK mapping",
                "prototype_default": "local file",
                "production_rule": "Move to database or key management system with audit history.",
            },
            {
                "name": "SQLite request log",
                "required_for": "demo request, usage, and audit records",
                "prototype_default": "local database file",
                "production_rule": "Move to managed database with retention, backup, and access control.",
            },
        ],
        "preflight_checks": [
            {
                "check": "Admin key replaced",
                "why": "Default admin key is fine for local demo only.",
                "evidence": "/v1/gateway/config-check",
                "current_status": "needs_work" if any(item.get("code") == "default_admin_key" for item in config.get("checks", [])) else "ready",
            },
            {
                "check": "Provider key strategy agreed",
                "why": "Live provider calls need server-side key or customer BYOK policy.",
                "evidence": "/v1/gateway/provider-health",
                "current_status": "ready" if server.mock_mode else "check_provider_health",
            },
            {
                "check": "Customer keys and budgets issued",
                "why": "Each customer should have separate access, allowed models, and limits.",
                "evidence": "/v1/gateway/access-matrix",
                "current_status": "ready" if server.customers_by_key else "needs_work",
            },
            {
                "check": "Data handling policy agreed",
                "why": "Prompt logs and request detail can contain sensitive data.",
                "evidence": "/v1/gateway/data-governance",
                "current_status": "needs_policy",
            },
            {
                "check": "Readiness blockers owned",
                "why": "A demo can work while production gaps remain.",
                "evidence": "/v1/gateway/production-readiness",
                "current_status": readiness.get("overall_status"),
            },
        ],
        "operational_checks": [
            {"name": "Health check", "endpoint": "/health", "expected": "200 OK"},
            {"name": "Admin status", "endpoint": "/v1/gateway/status", "expected": "admin auth required and status JSON after valid admin key"},
            {"name": "Model list", "endpoint": "/v1/models", "expected": "customer auth required and OpenAI-compatible model list"},
            {"name": "Mock chat", "endpoint": "/v1/chat/completions", "expected": "OpenAI-compatible chat.completion response"},
            {"name": "Route preview", "endpoint": "/v1/gateway/route-preview", "expected": "route decision without provider spend"},
            {"name": "Provider health", "endpoint": "/v1/gateway/provider-health", "expected": "mock-ready or live-ready provider explanation"},
        ],
        "rollback_plan": [
            "Keep the last known good model_registry.json and customer_keys.json in version control for prototype demos.",
            "For pilot, record every customer, provider, and model route change in audit events.",
            "For production, use database migrations, config versions, staged rollout, and one-command rollback.",
            "If a live provider fails, switch affected public models to mock/demo mode or approved fallback route before broad customer traffic.",
        ],
        "deployment_options": [
            {
                "option": "single VM or internal server",
                "fit": "simple private pilot",
                "requirements": ["reverse proxy", "TLS", "process manager", "secret environment variables", "backup plan"],
            },
            {
                "option": "container service",
                "fit": "repeatable pilot and production path",
                "requirements": ["container image", "health check", "secret manager", "managed database", "logs and metrics"],
            },
            {
                "option": "serverless or managed functions",
                "fit": "low-ops API deployment if streaming/runtime limits are acceptable",
                "requirements": ["streaming support review", "timeout review", "persistent database", "central secret manager"],
            },
        ],
        "reference_endpoints": [
            "/health",
            "/v1/gateway/config-check",
            "/v1/gateway/provider-health",
            "/v1/gateway/data-governance",
            "/v1/gateway/production-readiness",
            "/v1/gateway/launch-plan",
            "/v1/gateway/change-management",
        ],
        "next_best_action": "Keep local demo on mock mode, then run one live Qwen test only after admin key, customer key, provider key, and data handling policy are understood.",
    }


def migration_plan(server):
    readiness = production_readiness(server)
    deployment = deployment_readiness(server)
    evaluation = evaluation_plan(server)
    return {
        "object": "gateway.migration_plan",
        "title": "AISmallRouter Customer Migration Plan",
        "audience": "business owner, customer technical owner, platform owner, support owner, and gateway owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This plan explains how a customer can move from direct model provider calls to one gateway API without switching everything at once.",
        "migration_strategy": {
            "recommended_style": "phased_cutover",
            "why": "Start with one customer, one workflow, one or two public model names, mock mode first, then controlled live traffic.",
            "do_not_do": [
                "Do not move all traffic on day one.",
                "Do not remove the old provider path before rollback is tested.",
                "Do not promise production SLA until launch gates are approved.",
            ],
        },
        "phases": [
            {
                "phase": "0. Current-state discovery",
                "owner": "Customer technical owner",
                "goal": "Understand current direct provider calls, models, prompts, latency needs, cost concerns, and data rules.",
                "actions": [
                    "List current provider endpoints and model names.",
                    "Collect 5 to 10 representative prompts.",
                    "Confirm who owns provider keys and billing.",
                    "Confirm whether prompts may be logged during testing.",
                ],
                "exit_check": "First workflow and candidate models are agreed.",
            },
            {
                "phase": "1. Shadow gateway setup",
                "owner": "Gateway owner",
                "goal": "Create equivalent public model names and customer key without changing customer production traffic.",
                "actions": [
                    "Create or confirm customer gateway key.",
                    "Map public model names to upstream provider models.",
                    "Run /v1/models and /v1/gateway/route-preview.",
                    "Keep mock mode for explanation unless live provider key is approved.",
                ],
                "exit_check": "Customer can call the gateway in test without affecting existing traffic.",
            },
            {
                "phase": "2. Mock and evaluation test",
                "owner": "Customer technical owner",
                "goal": "Show routing, fallback, usage, cost estimate, and model evaluation before live cutover.",
                "actions": [
                    "Run sample evaluation prompts through the gateway.",
                    "Review evaluation plan and model scorecards.",
                    "Check safety preview and data governance.",
                    "Save request_id values for support review.",
                ],
                "exit_check": "Customer accepts the route behavior and evidence needed for live test.",
            },
            {
                "phase": "3. Limited live pilot",
                "owner": "Gateway owner and support owner",
                "goal": "Send a small controlled amount of real traffic through the gateway.",
                "actions": [
                    "Enable live provider key only for the approved provider.",
                    "Start with one workflow and low request volume.",
                    "Monitor request activity, budget, latency, and provider health.",
                    "Keep the old direct provider path available.",
                ],
                "exit_check": "Live pilot has acceptable quality, latency, cost, and support evidence.",
            },
            {
                "phase": "4. Gradual cutover",
                "owner": "Customer platform owner",
                "goal": "Move traffic in small steps while keeping rollback simple.",
                "actions": [
                    "Move internal test traffic first.",
                    "Move a small customer cohort or low-risk workflow.",
                    "Increase traffic only after error, latency, budget, and support checks pass.",
                    "Use route preview before every model or provider change.",
                ],
                "exit_check": "Traffic can be increased without new support or cost surprises.",
            },
            {
                "phase": "5. Production decision",
                "owner": "Business owner",
                "goal": "Decide whether to stop, extend pilot, or fund production hardening.",
                "actions": [
                    "Review pilot scorecard.",
                    "Review production readiness and migration evidence.",
                    "Name owners for open security, billing, monitoring, and support gaps.",
                    "Agree whether the old provider path remains as emergency rollback.",
                ],
                "exit_check": "Production decision is explicit and funded if needed.",
            },
        ],
        "cutover_checklist": [
            "Customer gateway key works.",
            "Allowed models are correct.",
            "Route preview shows the intended provider and fallback path.",
            "Evaluation plan has been reviewed for the first workflow.",
            "Cost estimate and budget limits are understood.",
            "Safety preview and data governance questions are answered.",
            "Support owner knows how to use request_id and request detail.",
            "Rollback path to direct provider or previous route is tested.",
        ],
        "rollback_plan": [
            "Keep the old direct provider integration available during pilot.",
            "If gateway auth fails, rotate or reissue customer gateway key and move traffic back to old provider path.",
            "If provider route fails, disable the new route or switch to approved fallback model.",
            "If cost or latency is unexpected, pause live traffic and return to mock or old provider path.",
            "If sensitive data policy is unclear, stop live testing until data governance is approved.",
        ],
        "customer_message": "We do not need to migrate everything at once. We can start with one workflow, prove routing and reporting, keep rollback available, then decide whether production hardening is worth funding.",
        "evidence_endpoints": [
            "/v1/gateway/discovery-checklist",
            "/v1/gateway/evaluation-plan",
            "/v1/gateway/route-preview",
            "/v1/gateway/request-activity",
            "/v1/gateway/request-detail",
            "/v1/gateway/customer-reports",
            "/v1/gateway/production-readiness",
            "/v1/gateway/launch-plan",
            "/v1/gateway/change-management",
        ],
        "current_context": {
            "readiness_status": readiness.get("overall_status"),
            "deployment_stage_count": len(deployment.get("deployment_stages", [])),
            "model_scorecard_count": len(evaluation.get("model_scorecards", [])),
        },
        "next_best_action": "Use this migration plan after discovery and before any live customer traffic cutover.",
        "prototype_note": "This is a migration plan for demos and pilots. Production migration still needs customer-specific architecture, runbooks, monitoring, approval workflow, and rollback testing.",
    }


def production_backlog(server):
    readiness = production_readiness(server)
    deployment = deployment_readiness(server)
    launch = launch_plan(server)
    return {
        "object": "gateway.production_backlog",
        "title": "AISmallRouter Production Hardening Backlog",
        "audience": "business owner, gateway owner, platform owner, security owner, and support owner",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This backlog turns the prototype gaps into prioritized engineering work. It helps the team see what must be built before real production traffic.",
        "priority_meaning": {
            "P0": "Must be done before production customer traffic.",
            "P1": "Needed for a serious pilot or limited production.",
            "P2": "Useful for scale, automation, or later multi-provider expansion.",
        },
        "work_items": [
            {
                "id": "P0-001",
                "priority": "P0",
                "area": "secrets",
                "title": "Move provider and admin secrets to a secret manager",
                "owner": "Security owner",
                "why": "Local demo keys and environment variables are not enough for shared production traffic.",
                "evidence": ["/v1/gateway/config-check", "/v1/gateway/deployment-readiness"],
                "depends_on": ["deployment target selected"],
                "done_when": "Provider keys, admin key, and customer BYOK secrets are stored outside repo files and can be rotated.",
            },
            {
                "id": "P0-002",
                "priority": "P0",
                "area": "storage",
                "title": "Move customer, provider, model route, usage, and audit data to a database",
                "owner": "Platform owner",
                "why": "Local JSON and SQLite are useful for demos but fragile for production operations.",
                "evidence": ["/v1/gateway/deployment-readiness", "/v1/gateway/audit-events"],
                "depends_on": ["database selected", "backup policy"],
                "done_when": "Config and logs are stored in managed persistence with backup, retention, and access controls.",
            },
            {
                "id": "P0-003",
                "priority": "P0",
                "area": "data_governance",
                "title": "Approve logging, retention, redaction, and deletion policy",
                "owner": "Customer data owner",
                "why": "Prompt data and request detail may contain sensitive customer information.",
                "evidence": ["/v1/gateway/data-governance", "/v1/gateway/safety-preview"],
                "depends_on": ["customer data policy owner named"],
                "done_when": "The team can say what is logged, who can view it, how long it is retained, and how deletion works.",
            },
            {
                "id": "P0-004",
                "priority": "P0",
                "area": "operations",
                "title": "Add production monitoring, alert routing, and incident ownership",
                "owner": "Support owner",
                "why": "A customer-facing gateway needs fast diagnosis and clear customer wording when requests fail.",
                "evidence": ["/v1/gateway/alerts", "/v1/gateway/incident-playbook", "/v1/gateway/support-policy"],
                "depends_on": ["support owner named", "deployment target selected"],
                "done_when": "Health, errors, latency, provider readiness, and budget alerts go to named owners.",
            },
            {
                "id": "P0-005",
                "priority": "P0",
                "area": "change_control",
                "title": "Add approval workflow and rollback for customer, provider, and model route changes",
                "owner": "Gateway owner",
                "why": "Provider and route changes can affect cost, data handling, and customer behavior.",
                "evidence": ["/v1/gateway/change-management", "/v1/gateway/audit-events"],
                "depends_on": ["database-backed config"],
                "done_when": "Every production config change has approval, version history, audit event, and rollback path.",
            },
            {
                "id": "P1-001",
                "priority": "P1",
                "area": "provider_contracts",
                "title": "Add live contract tests for Qwen and each future provider",
                "owner": "Technical owner",
                "why": "OpenAI-compatible providers can still differ in auth, streaming, tools, errors, and usage fields.",
                "evidence": ["/v1/gateway/provider-contracts", "/v1/gateway/provider-health"],
                "depends_on": ["provider docs", "provider test keys"],
                "done_when": "Each enabled provider has request, response, streaming, tool, usage, and error tests.",
            },
            {
                "id": "P1-002",
                "priority": "P1",
                "area": "billing",
                "title": "Define production pricing, budgets, invoice, and chargeback rules",
                "owner": "Business owner",
                "why": "Usage records are only useful if the customer understands cost and budget behavior.",
                "evidence": ["/v1/gateway/invoice-preview", "/v1/gateway/customer-reports"],
                "depends_on": ["provider pricing confirmed"],
                "done_when": "Pricing units, invoice fields, budget actions, and customer reporting cadence are approved.",
            },
            {
                "id": "P1-003",
                "priority": "P1",
                "area": "tenant_controls",
                "title": "Harden tenant isolation and role-based admin access",
                "owner": "Security owner",
                "why": "Customers should only see their own access, usage, keys, and request details.",
                "evidence": ["/v1/gateway/me", "/v1/gateway/access-matrix", "/v1/gateway/request-detail"],
                "depends_on": ["identity model selected"],
                "done_when": "Customer and admin roles are separated, tested, and audited.",
            },
            {
                "id": "P2-001",
                "priority": "P2",
                "area": "marketplace",
                "title": "Design OpenRouter-like model catalog and provider marketplace features",
                "owner": "Product owner",
                "why": "Marketplace behavior is useful later, but should not block the first private gateway pilot.",
                "evidence": ["/v1/gateway/decision-guide", "/v1/gateway/model-catalog"],
                "depends_on": ["private gateway pilot success"],
                "done_when": "Model discovery, provider comparison, pricing display, and customer model selection are specified.",
            },
            {
                "id": "P2-002",
                "priority": "P2",
                "area": "automation",
                "title": "Automate deployment, smoke tests, and rollback",
                "owner": "Platform owner",
                "why": "Manual deployment is acceptable for demo but risky for repeated customer environments.",
                "evidence": ["/v1/gateway/deployment-readiness", "/openapi.json"],
                "depends_on": ["deployment target selected"],
                "done_when": "A deployment pipeline runs health, model list, mock chat, route preview, and rollback tests.",
            },
        ],
        "summary_by_priority": [
            {"priority": "P0", "count": 5, "meaning": "Production blockers."},
            {"priority": "P1", "count": 3, "meaning": "Pilot and limited-production hardening."},
            {"priority": "P2", "count": 2, "meaning": "Scale and marketplace expansion."},
        ],
        "recommended_sequence": [
            "Close P0 secrets and database work first.",
            "Approve data governance before live customer traffic.",
            "Add monitoring, support ownership, and change control before production promise.",
            "Run provider contract tests before adding OpenAI, Claude, Xiaomi, or other providers.",
            "Treat OpenRouter-like marketplace features as P2 until the private gateway pilot succeeds.",
        ],
        "current_context": {
            "readiness_status": readiness.get("overall_status"),
            "launch_decision": launch.get("decision"),
            "deployment_next_action": deployment.get("next_best_action"),
        },
        "reference_endpoints": [
            "/v1/gateway/production-readiness",
            "/v1/gateway/deployment-readiness",
            "/v1/gateway/data-governance",
            "/v1/gateway/change-management",
            "/v1/gateway/provider-contracts",
            "/v1/gateway/invoice-preview",
        ],
        "next_best_action": "Use this backlog after the proposal summary to decide what must be funded before a production commitment.",
    }


def onboarding_plan(server):
    readiness = production_readiness(server)
    pilot = pilot_checklist(server)
    return {
        "object": "gateway.onboarding_plan",
        "title": "AISmallRouter Customer Onboarding Plan",
        "audience": "business, customer success, support, and customer technical teams",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This plan turns the prototype into a simple customer pilot path. It explains what happens first, who owns it, and what evidence proves the step is done.",
        "recommended_timeline": "5 working days for a small technical pilot setup, then a separate production hardening decision.",
        "principles": [
            "Start in mock mode so the customer can understand routing without spending provider credits.",
            "Use one customer, one or two public model names, and one clear use case first.",
            "Show simple evidence at every step: model list, route preview, request trace, usage report, and readiness gaps.",
            "Do not promise production traffic until secrets, billing, monitoring, support, and approval workflow are ready.",
        ],
        "roles": [
            {"role": "Business owner", "responsibility": "Confirms the customer problem, pilot value, and decision timeline."},
            {"role": "Technical owner", "responsibility": "Runs the API test, reviews OpenAPI/Postman, and checks route behavior."},
            {"role": "Support owner", "responsibility": "Confirms request tracing, incident wording, and escalation path."},
            {"role": "Gateway owner", "responsibility": "Issues keys, configures allowed models, reviews budgets, and explains readiness gaps."},
        ],
        "steps": [
            {
                "day": "Day 0",
                "title": "Customer alignment",
                "goal": "Make sure the customer understands the gateway idea before any integration work.",
                "owner": "Business owner",
                "actions": [
                    "Show the dashboard and executive brief.",
                    "Explain one customer API, model aliases, provider adapters, usage records, and production gaps.",
                    "Agree the first use case and the public model name to test.",
                ],
                "evidence": ["/", "/v1/gateway/executive-brief", "/v1/gateway/decision-guide"],
                "exit_check": "Customer agrees the pilot is worth a small technical test.",
            },
            {
                "day": "Day 1",
                "title": "Safe access setup",
                "goal": "Prepare a customer key, allowed models, budgets, and integration examples.",
                "owner": "Gateway owner",
                "actions": [
                    "Use key issue preview before saving any customer.",
                    "Confirm allowed models and default policy.",
                    "Share the customer integration guide, OpenAPI contract, and Postman collection.",
                ],
                "evidence": [
                    "/v1/gateway/key-issue-preview",
                    "/v1/gateway/integration-guide",
                    "/openapi.json",
                    "/postman_collection.json",
                ],
                "exit_check": "Customer has a safe local key package and a first API command.",
            },
            {
                "day": "Day 2",
                "title": "Mock technical test",
                "goal": "Prove the customer can call the gateway without live provider spend.",
                "owner": "Technical owner",
                "actions": [
                    "List models with the customer key.",
                    "Send one mock chat request.",
                    "Record the gateway.request_id and route trace.",
                ],
                "evidence": ["/v1/models", "/v1/chat/completions", "/v1/gateway/request-detail?request_id=..."],
                "exit_check": "Customer can send a request and support can trace it.",
            },
            {
                "day": "Day 3",
                "title": "Route and provider review",
                "goal": "Explain why each provider needs adapter work before adding more models.",
                "owner": "Gateway owner",
                "actions": [
                    "Run route preview for the test model.",
                    "Review provider contracts for Qwen, OpenAI-compatible, Claude, and Xiaomi-planned providers.",
                    "Confirm fallback, allowed provider, and capability rules for the pilot.",
                ],
                "evidence": ["/v1/gateway/route-preview", "/v1/gateway/provider-contracts", "/v1/gateway/policy-presets"],
                "exit_check": "Customer understands that provider expansion is adapter and contract work, not only API wiring.",
            },
            {
                "day": "Day 4",
                "title": "Usage, cost, and support review",
                "goal": "Make the pilot measurable and supportable.",
                "owner": "Support owner",
                "actions": [
                    "Review customer report, invoice preview, and cost estimate.",
                    "Review support policy and incident playbook.",
                    "Confirm what is demo-ready and what is not production-ready.",
                ],
                "evidence": [
                    "/v1/gateway/customer-reports",
                    "/v1/gateway/invoice-preview",
                    "/v1/gateway/cost-estimate",
                    "/v1/gateway/support-policy",
                    "/v1/gateway/incident-playbook",
                    "/v1/gateway/production-readiness",
                ],
                "exit_check": "Customer has a clear cost, support, and readiness story.",
            },
            {
                "day": "Day 5",
                "title": "Pilot decision",
                "goal": "Decide whether to stop, extend the pilot, or plan production hardening.",
                "owner": "Business owner",
                "actions": [
                    "Review pilot checklist success criteria.",
                    "Name owners for production blockers if the customer wants live traffic.",
                    "Choose stop, extend pilot, or productionize.",
                ],
                "evidence": ["/v1/gateway/pilot-checklist", "/v1/gateway/roadmap", "/v1/gateway/production-readiness"],
                "exit_check": "Decision is recorded with clear next owners.",
            },
        ],
        "handoff_artifacts": [
            {"name": "Dashboard", "endpoint": "/", "purpose": "Explain the concept visually."},
            {"name": "Demo bundle", "endpoint": "/v1/gateway/demo-bundle", "purpose": "Collect the customer handoff links and commands."},
            {"name": "Integration guide", "endpoint": "/v1/gateway/integration-guide", "purpose": "Give the customer practical API examples."},
            {"name": "Pilot checklist", "endpoint": "/v1/gateway/pilot-checklist", "purpose": "Keep pilot scope small and measurable."},
            {"name": "Production readiness", "endpoint": "/v1/gateway/production-readiness", "purpose": "Avoid promising production too early."},
        ],
        "success_criteria": pilot.get("success_criteria", []),
        "current_readiness": {
            "overall_status": readiness.get("overall_status"),
            "prototype_only": readiness.get("prototype_only"),
        },
        "next_best_action": "Use Day 0 and Day 1 with one customer champion before enabling any live provider testing.",
    }


def gateway_roadmap(server):
    readiness = production_readiness(server)
    return {
        "object": "gateway.roadmap",
        "title": "AISmallRouter Prototype To Production Roadmap",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "This roadmap shows how to move from a local prototype to a customer pilot and then to a production-ready gateway.",
        "current_position": {
            "stage": "prototype",
            "readiness_status": readiness.get("overall_status"),
            "prototype_only": readiness.get("prototype_only"),
        },
        "phases": [
            {
                "phase": "1. Prototype explanation",
                "goal": "Help business and technical users understand the model gateway idea.",
                "deliverables": [
                    "Visual dashboard",
                    "Mock chat request",
                    "Route preview",
                    "Executive brief",
                    "Customer guide PDF",
                ],
                "exit_criteria": [
                    "Customer understands one API for many providers.",
                    "Customer understands public model names versus upstream provider models.",
                    "Customer agrees whether a technical pilot is useful.",
                ],
                "main_risks": [
                    "Customer expects production behavior from a prototype.",
                    "Provider live keys are not configured yet.",
                ],
            },
            {
                "phase": "2. Technical pilot",
                "goal": "Let a small customer team test controlled API flows.",
                "deliverables": [
                    "Customer gateway key",
                    "Integration guide",
                    "OpenAPI contract",
                    "Postman collection",
                    "Pilot checklist",
                    "Request tracing with gateway.request_id",
                ],
                "exit_criteria": [
                    "Customer can send a model request.",
                    "Support can trace requests and explain route decisions.",
                    "Usage, budget, and support expectations are clear.",
                ],
                "main_risks": [
                    "Customer scope grows before controls are ready.",
                    "Live provider behavior differs from mock behavior.",
                ],
            },
            {
                "phase": "3. Production hardening",
                "goal": "Prepare the gateway for real customer traffic.",
                "deliverables": [
                    "Database-backed customer and route config",
                    "Secret manager integration",
                    "Approval workflow",
                    "Monitoring and alert routing",
                    "Billing rules",
                    "Operational runbooks",
                ],
                "exit_criteria": [
                    "Production readiness blockers have named owners.",
                    "Support policy and SLA terms are agreed.",
                    "Provider keys, logs, billing, and audit rules are production-safe.",
                ],
                "main_risks": [
                    "Billing rules are unclear.",
                    "Secrets or customer keys are not managed safely.",
                    "No agreed incident ownership.",
                ],
            },
            {
                "phase": "4. Multi-provider expansion",
                "goal": "Add more providers after the control layer is understood.",
                "deliverables": [
                    "Provider contract matrix",
                    "Disabled provider configs",
                    "Live provider contract tests",
                    "Fallback routing rules",
                    "Provider allow-list policies",
                ],
                "exit_criteria": [
                    "Each provider has auth, request, response, streaming, tools, usage, and error tests.",
                    "Fallback behavior is explained and approved.",
                    "Customer policy controls decide which providers can be used.",
                ],
                "main_risks": [
                    "OpenAI-compatible providers still behave differently.",
                    "Claude, Xiaomi, or other providers require custom normalization.",
                    "Fallback can change cost or compliance behavior.",
                ],
            },
        ],
        "recommended_next_action": "Use the executive brief and pilot checklist with one customer champion before promising production traffic.",
        "reference_endpoints": [
            "/v1/gateway/executive-brief",
            "/v1/gateway/pilot-checklist",
            "/v1/gateway/onboarding-plan",
            "/v1/gateway/production-readiness",
            "/v1/gateway/launch-plan",
            "/v1/gateway/provider-contracts",
            "/v1/gateway/support-policy",
        ],
    }


def decision_guide(server):
    readiness = production_readiness(server)
    return {
        "object": "gateway.decision_guide",
        "title": "AI Gateway Build Or Buy Decision Guide",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "Use this guide to explain whether a customer needs a normal API Gateway, a managed AI Gateway, a custom Model Gateway, or an OpenRouter-like platform.",
        "options": [
            {
                "option": "Normal API Gateway",
                "best_for": "General HTTP traffic control.",
                "good_at": [
                    "Authentication",
                    "Rate limits",
                    "WAF or IP rules",
                    "Request logs",
                    "Traffic routing",
                ],
                "not_enough_for": [
                    "Provider request and response normalization",
                    "Model alias routing",
                    "Tool calling differences",
                    "Provider usage and cost normalization",
                    "Fallback across AI providers",
                ],
                "recommendation": "Use it in front of the Model Gateway when enterprise traffic controls are needed.",
            },
            {
                "option": "Managed AI Gateway",
                "best_for": "Teams that want faster managed provider access and platform-level routing features.",
                "good_at": [
                    "Unified AI provider entry point",
                    "Centralized monitoring",
                    "Provider failover features",
                    "Managed infrastructure",
                ],
                "not_enough_for": [
                    "Highly custom customer billing rules",
                    "Custom onboarding workflow",
                    "Internal product-specific policy",
                    "Customer-specific demo and support package",
                ],
                "recommendation": "Consider it when managed operations matter more than custom control.",
            },
            {
                "option": "Custom Model Gateway",
                "best_for": "Teams that need customer-specific controls and a product-owned routing layer.",
                "good_at": [
                    "Public model aliases",
                    "Customer keys and budgets",
                    "Provider adapter control",
                    "BYOK mapping",
                    "Customer reports and pilot workflows",
                ],
                "not_enough_for": [
                    "Enterprise WAF by itself",
                    "Global production operations without more infrastructure",
                    "Legal SLA without production hardening",
                ],
                "recommendation": "This prototype explores this path with Qwen first, then more providers later.",
            },
            {
                "option": "OpenRouter-like Marketplace",
                "best_for": "Public multi-provider model marketplace or broad model catalog.",
                "good_at": [
                    "Large model catalog",
                    "Provider routing choices",
                    "Customer credit and usage experience",
                    "Marketplace-style discovery",
                ],
                "not_enough_for": [
                    "Private customer-specific business rules unless built in",
                    "Full control over every provider contract",
                    "Internal-only compliance and billing rules",
                ],
                "recommendation": "Use OpenRouter as a reference pattern, not as something to copy blindly.",
            },
        ],
        "recommended_path_now": [
            "Use the current custom Model Gateway prototype for customer explanation.",
            "Keep normal API Gateway features optional until enterprise traffic controls are needed.",
            "Use Qwen / Model Studio first because the key is available.",
            "Add providers only after provider contracts and pilot success are clear.",
        ],
        "decision_questions": [
            "Does the customer only need HTTP traffic control?",
            "Does the customer need model aliases, provider adapters, and fallback logic?",
            "Does the customer need custom budgets, reporting, onboarding, and support wording?",
            "Does the customer prefer managed infrastructure over custom control?",
            "Is the goal a private customer gateway or a public model marketplace?",
        ],
        "evidence_to_show": [
            "/v1/gateway/provider-contracts",
            "/v1/gateway/route-preview",
            "/v1/gateway/production-readiness",
            "/v1/gateway/executive-brief",
            "/v1/gateway/roadmap",
        ],
        "current_readiness": {
            "overall_status": readiness.get("overall_status"),
            "prototype_only": readiness.get("prototype_only"),
        },
    }


def gateway_faq(server):
    readiness = production_readiness(server)
    return {
        "object": "gateway.faq",
        "title": "AISmallRouter Customer FAQ",
        "mode": "mock" if server.mock_mode else "live",
        "plain_english": "Use this FAQ when a business or technical customer asks common questions about the model gateway.",
        "questions": [
            {
                "question": "Is this just an API Gateway?",
                "short_answer": "No. A normal API Gateway controls HTTP traffic. A Model Gateway also handles AI-specific model routing, provider adapters, usage, fallback, and customer policy.",
                "show": ["/v1/gateway/decision-guide", "/v1/gateway/provider-contracts"],
            },
            {
                "question": "Why start with Alibaba Cloud Model Studio / Qwen?",
                "short_answer": "Because we already have a usable key and can test the routing idea with low cost and low risk before adding more providers.",
                "show": ["/v1/models", "/v1/gateway/route-preview"],
            },
            {
                "question": "Is this an OpenRouter clone?",
                "short_answer": "No. OpenRouter is a useful public reference for multi-model routing ideas. This prototype is a smaller customer-controlled gateway focused on our own customer access, routing, and reporting needs.",
                "show": ["/v1/gateway/decision-guide", "/v1/gateway/roadmap"],
            },
            {
                "question": "Will customer prompts or provider keys be exposed?",
                "short_answer": "The prototype avoids exposing provider secrets in customer views. Production should add a real secret manager, stronger retention policy, and approved data handling rules.",
                "show": ["/v1/gateway/me", "/v1/gateway/config-check", "/v1/gateway/production-readiness"],
            },
            {
                "question": "Can customers bring their own provider key?",
                "short_answer": "The prototype supports a simple BYOK mapping through environment variables. Production should move this to a secure customer database and secret manager.",
                "show": ["/v1/gateway/me", "/v1/gateway/provider-health"],
            },
            {
                "question": "How do we control cost?",
                "short_answer": "The gateway can show token budgets, cost budgets, cost estimates, customer reports, and invoice preview. Current billing is an estimate, not a legal invoice.",
                "show": ["/v1/gateway/cost-estimate", "/v1/gateway/customer-reports", "/v1/gateway/invoice-preview"],
            },
            {
                "question": "What happens if a provider fails?",
                "short_answer": "The gateway can explain provider health, fallback candidates, request errors, and route decisions. Production should add stronger monitoring and automated operations.",
                "show": ["/v1/gateway/provider-health", "/v1/gateway/incident-playbook", "/v1/gateway/route-preview"],
            },
            {
                "question": "Can we add OpenAI, Claude, Xiaomi, or other providers later?",
                "short_answer": "Yes, but each provider needs a contract check for auth, request shape, response shape, streaming, tools, usage, and errors.",
                "show": ["/v1/gateway/provider-contracts", "/v1/gateway/roadmap"],
            },
            {
                "question": "Is this ready for production?",
                "short_answer": "Not yet. It is ready for explanation, mock demos, and careful technical pilots. Production needs secrets, database, monitoring, billing, approval workflow, and SLA work.",
                "show": ["/v1/gateway/production-readiness", "/v1/gateway/support-policy", "/v1/gateway/pilot-checklist"],
            },
            {
                "question": "What is the next customer step?",
                "short_answer": "Use the executive brief to align on value, then run a small technical pilot with mock mode first.",
                "show": ["/v1/gateway/executive-brief", "/v1/gateway/pilot-checklist"],
            },
            {
                "question": "How do we start with a customer?",
                "short_answer": "Use the onboarding plan: align on Day 0, issue safe access on Day 1, run mock tests on Day 2, review routing and support, then decide whether to stop, extend, or productionize.",
                "show": ["/v1/gateway/onboarding-plan", "/v1/gateway/pilot-checklist"],
            },
        ],
        "suggested_demo_order": [
            "/v1/gateway/executive-brief",
            "/v1/gateway/decision-guide",
            "/v1/gateway/provider-contracts",
            "/v1/gateway/pilot-checklist",
            "/v1/gateway/onboarding-plan",
            "/v1/gateway/production-readiness",
        ],
        "current_readiness": {
            "overall_status": readiness.get("overall_status"),
            "prototype_only": readiness.get("prototype_only"),
        },
    }


def demo_script(server):
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    admin_key = server.admin_api_key or DEFAULT_ADMIN_API_KEY
    return {
        "object": "gateway.demo_script",
        "title": "AISmallRouter 15 Minute Customer Demo Script",
        "mode": "mock" if server.mock_mode else "live",
        "duration_minutes": 15,
        "plain_english": "Use this script when you need to explain the gateway to a customer who may not be technical.",
        "opening_line": "Today we will show how one customer API can hide model provider complexity while still keeping access, routing, cost, and support controls visible.",
        "before_demo_checklist": [
            "Start the gateway in mock mode when you do not want to spend provider credits.",
            "Open the dashboard before the customer meeting starts.",
            "Use the local demo admin key only for local demos.",
            "Keep provider secrets and real customer keys out of the screen share.",
            "Prepare the executive brief, decision guide, FAQ, pilot checklist, OpenAPI contract, and Postman collection.",
            "Prepare the onboarding plan if the customer asks what happens after the demo.",
        ],
        "steps": [
            {
                "minute": "0-2",
                "title": "Set the business context",
                "show": f"{base_url}/v1/gateway/executive-brief",
                "talk_track": "The customer does not need to learn every model provider API. They call one API, and the gateway manages model access, routing, usage, and handoff material.",
                "proof_point": "The executive brief gives the one-sentence value, demo story, known risks, and next step.",
            },
            {
                "minute": "2-4",
                "title": "Explain why this is more than a normal API Gateway",
                "show": f"{base_url}/v1/gateway/decision-guide",
                "talk_track": "A normal API Gateway is good for HTTP control. A Model Gateway also needs AI-specific provider adapters, model aliases, fallback, usage normalization, and customer policy.",
                "proof_point": "The decision guide compares normal API Gateway, managed AI Gateway, custom Model Gateway, and OpenRouter-like marketplace options.",
            },
            {
                "minute": "4-6",
                "title": "Show the customer-facing API",
                "show": f"{base_url}/v1/models",
                "curl": f"curl {base_url}/v1/models -H 'Authorization: Bearer {DEFAULT_GATEWAY_API_KEY}'",
                "talk_track": "The customer sees simple public model names such as smart-fast. They do not need to know the upstream provider model name.",
                "proof_point": "The model list returns OpenAI-compatible model metadata.",
            },
            {
                "minute": "6-8",
                "title": "Show how routing is decided",
                "show": f"{base_url}/v1/gateway/route-preview",
                "curl": f"curl {base_url}/v1/gateway/route-preview -H 'Authorization: Bearer {admin_key}' -H 'Content-Type: application/json' -d '{{\"customer_id\":\"dev\",\"model\":\"smart-fast\",\"gateway_policy\":\"lowest_cost\"}}'",
                "talk_track": "Route preview lets us explain the model decision before calling a real provider. This is useful for sales, support, and technical review.",
                "proof_point": "The route decision shows model alias, provider choice, policy, fallback candidates, and score reasons.",
            },
            {
                "minute": "8-10",
                "title": "Run the mock request",
                "show": f"{base_url}/v1/chat/completions",
                "curl": f"curl {base_url}/v1/chat/completions -H 'Authorization: Bearer {DEFAULT_GATEWAY_API_KEY}' -H 'Content-Type: application/json' -d '{{\"model\":\"smart-fast\",\"messages\":[{{\"role\":\"user\",\"content\":\"Explain this gateway in one sentence.\"}}]}}'",
                "talk_track": "Mock mode returns a provider-like answer and route trace without using paid model credits. This lets the customer understand the flow safely.",
                "proof_point": "The response is OpenAI-compatible, so customer apps can start with a familiar shape.",
            },
            {
                "minute": "10-12",
                "title": "Show cost and support controls",
                "show": f"{base_url}/v1/gateway/production-readiness",
                "talk_track": "A real gateway is also about control: budgets, reports, audit events, incident wording, and production gaps. This is why the work is larger than connecting one API.",
                "proof_point": "Production readiness explains what is demo-ready and what still needs hardening.",
            },
            {
                "minute": "12-14",
                "title": "Answer likely objections",
                "show": f"{base_url}/v1/gateway/faq",
                "talk_track": "Use the FAQ to keep answers simple and consistent when the customer asks about OpenRouter, API Gateway, Qwen, cost, keys, or production readiness.",
                "proof_point": "The FAQ gives short answers and the best endpoint to show for each question.",
            },
            {
                "minute": "14-15",
                "title": "Show the onboarding path",
                "show": f"{base_url}/v1/gateway/onboarding-plan",
                "talk_track": "The next step should be small: one customer, one or two models, mock mode first, clear success criteria, and no production SLA until the hardening work is done.",
                "proof_point": "The onboarding plan turns the conversation into a Day 0 to Day 5 pilot path with owners, actions, and evidence.",
            },
            {
                "minute": "15",
                "title": "Confirm the pilot checklist",
                "show": f"{base_url}/v1/gateway/pilot-checklist",
                "talk_track": "The onboarding plan says what happens by day. The pilot checklist says how we know whether the test worked.",
                "proof_point": "The pilot checklist defines scope, roles, checks, success criteria, and decision options.",
            },
        ],
        "likely_questions": [
            {
                "question": "Is this just an API Gateway?",
                "answer": "No. A normal API Gateway controls HTTP traffic. This gateway also handles model aliases, provider adapters, routing policy, usage records, fallback, and customer reports.",
                "show": "/v1/gateway/decision-guide",
            },
            {
                "question": "Is this like OpenRouter?",
                "answer": "OpenRouter is a useful reference for a multi-model marketplace. This project is a smaller customer-owned gateway focused on private access, customer controls, and explainable routing.",
                "show": "/v1/gateway/decision-guide",
            },
            {
                "question": "Can we add OpenAI, Claude, Xiaomi, or other providers?",
                "answer": "Yes, but each provider needs adapter work for auth, request shape, response shape, streaming, tools, usage, errors, and billing behavior.",
                "show": "/v1/gateway/provider-contracts",
            },
            {
                "question": "Can this run without spending model credits?",
                "answer": "Yes for demos. Mock mode shows the request path and response shape without calling the live provider.",
                "show": "/v1/gateway/demo-bundle",
            },
            {
                "question": "Is it production-ready today?",
                "answer": "No. It is demo-ready and pilot-ready with care. Production needs stronger secret storage, database, monitoring, billing, approval workflow, and SLA work.",
                "show": "/v1/gateway/production-readiness",
            },
        ],
        "closing_line": "The useful idea is not only one API. The useful idea is one controlled model access layer that business, support, and technical teams can all understand.",
        "follow_up_package": [
            f"{base_url}/",
            f"{base_url}/openapi.json",
            f"{base_url}/postman_collection.json",
            "Model_Gateway_Customer_Guide.pdf",
            f"{base_url}/v1/gateway/pilot-checklist",
            f"{base_url}/v1/gateway/onboarding-plan",
        ],
    }


def read_jsonl_tail(path, limit=50):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as file:
        lines = file.readlines()[-limit:]
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def estimate_tokens(messages, content):
    text = content or ""
    for message in messages or []:
        value = message.get("content", "")
        if isinstance(value, str):
            text += " " + value
    return max(1, len(text) // 4)


SAFETY_PATTERNS = [
    {
        "type": "email",
        "severity": "warning",
        "pattern": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "explanation": "Email address detected.",
    },
    {
        "type": "phone",
        "severity": "warning",
        "pattern": re.compile(r"\b(?:\+?\d[\d\s().-]{7,}\d)\b"),
        "explanation": "Possible phone number detected.",
    },
    {
        "type": "api_key",
        "severity": "critical",
        "pattern": re.compile(r"\b(?:sk|pk|ak|aisr)_[A-Za-z0-9_-]{16,}\b"),
        "explanation": "Possible API key detected.",
    },
    {
        "type": "secret_assignment",
        "severity": "critical",
        "pattern": re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*[A-Za-z0-9_./+=-]{8,}"),
        "explanation": "Possible secret value detected.",
    },
]


def mask_sample(value):
    value = str(value)
    if len(value) <= 8:
        return value[0:2] + "..."
    return value[:4] + "..." + value[-3:]


def payload_text_segments(payload):
    segments = []
    if isinstance(payload.get("prompt"), str):
        segments.append({"location": "prompt", "text": payload["prompt"]})
    for index, message in enumerate(payload.get("messages") or []):
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, str):
            segments.append({"location": f"messages[{index}].content", "text": content})
    return segments


def redact_sensitive_text(text):
    redacted = text
    applied = []
    for rule in SAFETY_PATTERNS:
        replacement = f"[REDACTED_{rule['type'].upper()}]"
        redacted, count = rule["pattern"].subn(replacement, redacted)
        if count:
            applied.append({"type": rule["type"], "count": count})
    return redacted, applied


def redact_sensitive_payload(payload):
    redacted_payload = dict(payload)
    redactions = []
    if isinstance(redacted_payload.get("prompt"), str):
        redacted_text, applied = redact_sensitive_text(redacted_payload["prompt"])
        redacted_payload["prompt"] = redacted_text
        for item in applied:
            redactions.append({"location": "prompt", **item})
    messages = []
    for index, message in enumerate(redacted_payload.get("messages") or []):
        if not isinstance(message, dict):
            messages.append(message)
            continue
        message_copy = dict(message)
        if isinstance(message_copy.get("content"), str):
            redacted_text, applied = redact_sensitive_text(message_copy["content"])
            message_copy["content"] = redacted_text
            for item in applied:
                redactions.append({"location": f"messages[{index}].content", **item})
        messages.append(message_copy)
    if "messages" in redacted_payload:
        redacted_payload["messages"] = messages
    return redacted_payload, redactions


def safety_preview(payload):
    findings = []
    for segment in payload_text_segments(payload):
        for rule in SAFETY_PATTERNS:
            for match in rule["pattern"].finditer(segment["text"]):
                findings.append(
                    {
                        "type": rule["type"],
                        "severity": rule["severity"],
                        "location": segment["location"],
                        "sample": mask_sample(match.group(0)),
                        "explanation": rule["explanation"],
                    }
                )
    severity_rank = {"critical": 2, "warning": 1, "info": 0}
    max_rank = max((severity_rank.get(item["severity"], 0) for item in findings), default=0)
    status = "blocked" if max_rank >= 2 else "review" if max_rank == 1 else "clear"
    redacted_payload, redactions = redact_sensitive_payload(payload)
    return {
        "status": status,
        "findings": findings,
        "redaction_available": bool(redactions),
        "redactions": redactions,
        "redacted_preview": {
            "prompt": redacted_payload.get("prompt"),
            "messages": redacted_payload.get("messages", []),
        },
        "summary": {
            "critical": sum(1 for item in findings if item["severity"] == "critical"),
            "warning": sum(1 for item in findings if item["severity"] == "warning"),
            "total": len(findings),
            "redactions": sum(item["count"] for item in redactions),
        },
        "next_step": (
            "Remove secrets before sending this request to a model provider."
            if status == "blocked"
            else "Review personal data before sending this request to a model provider."
            if status == "review"
            else "No obvious sensitive data was detected by the local preview."
        ),
        "note": "This is a simple local preview. It is not a full DLP or compliance system.",
    }


def first_openai_function_tool(tools):
    for tool in tools or []:
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            function = tool["function"]
            if function.get("name"):
                return function
    return None


def load_registry(path):
    data = read_json(path, {"providers": [], "models": []})
    providers = {
        provider["id"]: provider
        for provider in data.get("providers", [])
        if provider.get("enabled", True)
    }
    models = {}
    for model in data.get("models", []):
        if model.get("enabled", True):
            model = dict(model)
            provider_id = model["provider"]
            if provider_id not in providers:
                raise GatewayError(f"Model {model['id']} references disabled provider {provider_id}.")
            model["provider_config"] = providers[provider_id]
            models[model["id"]] = model
    return {"providers": providers, "models": models}


def load_customers(path):
    data = read_json(
        path,
        {
            "customers": [
                {
                    "id": "dev",
                    "name": "Development Customer",
                    "api_key": DEFAULT_GATEWAY_API_KEY,
                    "request_limit": DEFAULT_REQUEST_LIMIT,
                    "limit_window_seconds": DEFAULT_LIMIT_WINDOW_SECONDS,
                    "allowed_models": ["*"],
                    "enabled": True,
                }
            ]
        },
    )
    customers_by_key = {}
    for customer in data.get("customers", []):
        if not customer.get("enabled", True):
            continue
        api_key = customer.get("api_key")
        if not api_key:
            continue
        customer = dict(customer)
        customer.setdefault("request_limit", DEFAULT_REQUEST_LIMIT)
        customer.setdefault("limit_window_seconds", DEFAULT_LIMIT_WINDOW_SECONDS)
        customer.setdefault("allowed_models", ["*"])
        customers_by_key[api_key] = customer
    return customers_by_key


def make_json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_html_response(handler, status, html):
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_csv_response(handler, status, filename, content):
    body = content.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/csv; charset=utf-8")
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_error(handler, status, message, code, details=None):
    payload = {
        "error": {
            "message": message,
            "type": "gateway_error",
            "code": code,
        }
    }
    if details is not None:
        payload["error"]["details"] = details
    make_json_response(handler, status, payload)


def sse_chunk(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def openai_style_model_list(models):
    return {
        "object": "list",
        "data": [
            {
                "id": public_name,
                "object": "model",
                "created": 0,
                "owned_by": model["provider"],
                "capabilities": model.get("capabilities", []),
                "upstream_model": model.get("upstream_model"),
                "fallback_models": model.get("fallback_models", []),
            }
            for public_name, model in sorted(models.items())
        ],
    }


def openapi_spec(server):
    def json_response(description="JSON response"):
        return {
            "description": description,
            "content": {
                "application/json": {
                    "schema": {"type": "object"}
                }
            },
        }

    def json_request(description="JSON request body"):
        return {
            "description": description,
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"type": "object"}
                }
            },
        }

    customer_security = [{"customerBearerAuth": []}]
    admin_security = [{"adminBearerAuth": []}]
    paths = {
        "/health": {
            "get": {
                "summary": "Health check",
                "responses": {"200": json_response("Gateway is running.")},
            }
        },
        "/v1/models": {
            "get": {
                "summary": "List OpenAI-compatible public models",
                "security": customer_security,
                "responses": {"200": json_response("OpenAI-compatible model list.")},
            }
        },
        "/v1/chat/completions": {
            "post": {
                "summary": "Create an OpenAI-compatible chat completion",
                "security": customer_security,
                "requestBody": json_request("OpenAI-compatible chat request plus optional gateway controls."),
                "responses": {"200": json_response("OpenAI-compatible chat completion or stream metadata.")},
            }
        },
        "/v1/gateway/me": {
            "get": {
                "summary": "Customer self-service profile",
                "security": customer_security,
                "responses": {"200": json_response("Customer profile, allowed models, budget, usage, and invoice preview.")},
            }
        },
        "/v1/gateway/integration-guide": {
            "get": {
                "summary": "Customer integration guide",
                "security": customer_security,
                "responses": {"200": json_response("Customer-specific quickstart, code examples, and go-live checklist.")},
            }
        },
        "/v1/gateway/sdk-starter": {
            "get": {
                "summary": "Customer SDK starter pack",
                "security": customer_security,
                "responses": {"200": json_response("Customer-specific env template, starter files, commands, and common errors.")},
            }
        },
        "/v1/gateway/status": {
            "get": {
                "summary": "Admin gateway status summary",
                "security": admin_security,
                "responses": {"200": json_response("Gateway status and control-plane summary.")},
            }
        },
        "/v1/gateway/policy-presets": {
            "get": {
                "summary": "List named gateway policy presets",
                "security": admin_security,
                "responses": {"200": json_response("Policy preset names and controls.")},
            }
        },
        "/v1/gateway/route-preview": {
            "post": {
                "summary": "Dry-run route selection",
                "security": admin_security,
                "requestBody": json_request("Route preview request with customer_id, model, and optional gateway controls."),
                "responses": {"200": json_response("Route decision without calling a provider.")},
            }
        },
        "/v1/gateway/cost-estimate": {
            "post": {
                "summary": "Dry-run cost estimate",
                "security": admin_security,
                "requestBody": json_request("Cost estimate request."),
                "responses": {"200": json_response("Estimated tokens, cost, and budget impact.")},
            }
        },
        "/v1/gateway/safety-preview": {
            "post": {
                "summary": "Preview obvious sensitive data",
                "security": admin_security,
                "requestBody": json_request("Prompt or chat payload to inspect locally."),
                "responses": {"200": json_response("Local sensitive-data preview.")},
            }
        },
        "/v1/gateway/key-issue-preview": {
            "post": {
                "summary": "Preview a new customer key package without saving",
                "security": admin_security,
                "requestBody": json_request("Customer onboarding settings."),
                "responses": {"200": json_response("Generated key preview and config snippet.")},
            }
        },
        "/v1/gateway/customers": {
            "get": {
                "summary": "List customer records",
                "security": admin_security,
                "responses": {"200": json_response("Customer records without raw secrets.")},
            },
            "post": {
                "summary": "Create and persist a customer",
                "security": admin_security,
                "requestBody": json_request("Customer config, limits, allowed models, and optional default_policy."),
                "responses": {"201": json_response("Created customer and one-time generated key.")},
            },
        },
        "/v1/gateway/customers/rotate-key": {
            "post": {
                "summary": "Rotate a customer gateway key",
                "security": admin_security,
                "requestBody": json_request("customer_id and optional api_key."),
                "responses": {"200": json_response("Rotated key response.")},
            }
        },
        "/v1/gateway/customers/disable": {
            "post": {
                "summary": "Disable a customer",
                "security": admin_security,
                "requestBody": json_request("customer_id."),
                "responses": {"200": json_response("Disabled customer response.")},
            }
        },
        "/v1/gateway/providers": {
            "get": {
                "summary": "List providers",
                "security": admin_security,
                "responses": {"200": json_response("Provider configs without provider secrets.")},
            },
            "post": {
                "summary": "Create and persist a provider",
                "security": admin_security,
                "requestBody": json_request("Provider id, type, base_url, and api_key_env."),
                "responses": {"201": json_response("Created provider response.")},
            },
        },
        "/v1/gateway/providers/update": {
            "post": {
                "summary": "Update a provider",
                "security": admin_security,
                "requestBody": json_request("Provider updates."),
                "responses": {"200": json_response("Updated provider response.")},
            }
        },
        "/v1/gateway/providers/disable": {
            "post": {
                "summary": "Disable a provider and its active model routes",
                "security": admin_security,
                "requestBody": json_request("provider_id."),
                "responses": {"200": json_response("Disabled provider response.")},
            }
        },
        "/v1/gateway/model-routes": {
            "post": {
                "summary": "Create and persist a public model route",
                "security": admin_security,
                "requestBody": json_request("Model route config."),
                "responses": {"201": json_response("Created model route response.")},
            }
        },
        "/v1/gateway/model-routes/update": {
            "post": {
                "summary": "Update a model route",
                "security": admin_security,
                "requestBody": json_request("Model route updates."),
                "responses": {"200": json_response("Updated model route response.")},
            }
        },
        "/v1/gateway/model-routes/disable": {
            "post": {
                "summary": "Disable a model route",
                "security": admin_security,
                "requestBody": json_request("model_id."),
                "responses": {"200": json_response("Disabled model route response.")},
            }
        },
    }
    for path, summary in {
        "/v1/gateway/alerts": "Operational alerts",
        "/v1/gateway/access-matrix": "Customer and model access matrix",
        "/v1/gateway/config-check": "Local config readiness check",
        "/v1/gateway/requests": "Recent raw request records",
        "/v1/gateway/audit-events": "Audit events with filters",
        "/v1/gateway/usage": "Recent raw usage records",
        "/v1/gateway/provider-health": "Provider health and readiness",
        "/v1/gateway/provider-contracts": "Provider adapter contract matrix",
        "/v1/gateway/model-catalog": "Model catalog and routing plan",
        "/v1/gateway/evaluation-plan": "Model evaluation and quality plan",
        "/v1/gateway/customer-reports": "Customer usage and budget reports",
        "/v1/gateway/request-activity": "Filtered request activity",
        "/v1/gateway/request-detail": "One request detail by request_id",
        "/v1/gateway/customer-usage": "Usage grouped by customer",
        "/v1/gateway/customer-success": "Customer success account health summary",
        "/v1/gateway/commercial-policy": "Commercial policy and billing boundaries",
        "/v1/gateway/procurement-pack": "Procurement and vendor review pack",
        "/v1/gateway/business-case": "Business case and pilot ROI discussion pack",
        "/v1/gateway/implementation-plan": "Implementation estimate and delivery plan",
        "/v1/gateway/alternatives-pack": "Build, buy, managed gateway, and OpenRouter-like alternatives pack",
        "/v1/gateway/invoice-preview": "Invoice preview JSON or CSV",
        "/v1/gateway/model-usage": "Usage grouped by model",
        "/v1/gateway/request-summary": "Request summary by dimensions",
        "/v1/gateway/demo-bundle": "Customer demo bundle manifest",
        "/v1/gateway/handoff-checklist": "Customer handoff checklist",
        "/v1/gateway/production-readiness": "Production readiness report",
        "/v1/gateway/deployment-readiness": "Deployment readiness guide",
        "/v1/gateway/migration-plan": "Customer migration and cutover plan",
        "/v1/gateway/production-backlog": "Production hardening backlog",
        "/v1/gateway/launch-plan": "Production launch plan",
        "/v1/gateway/change-management": "Change management and rollback plan",
        "/v1/gateway/data-governance": "Data governance and privacy review",
        "/v1/gateway/security-review": "Security review and threat model",
        "/v1/gateway/operations-runbook": "Operations runbook and SLO watch plan",
        "/v1/gateway/incident-playbook": "Incident response playbook",
        "/v1/gateway/support-policy": "Support policy and SLA stage guide",
        "/v1/gateway/pilot-checklist": "Customer pilot checklist",
        "/v1/gateway/pilot-scorecard": "Customer pilot scorecard",
        "/v1/gateway/discovery-checklist": "Customer discovery checklist",
        "/v1/gateway/proposal-summary": "Customer proposal and scope summary",
        "/v1/gateway/onboarding-plan": "Customer onboarding plan",
        "/v1/gateway/executive-brief": "Executive customer brief",
        "/v1/gateway/roadmap": "Prototype to production roadmap",
        "/v1/gateway/decision-guide": "AI gateway build or buy decision guide",
        "/v1/gateway/faq": "Customer FAQ",
        "/v1/gateway/demo-script": "Customer demo script",
    }.items():
        paths[path] = {
            "get": {
                "summary": summary,
                "security": admin_security,
                "responses": {"200": json_response(summary)},
            }
        }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "AISmallRouter Gateway API",
            "version": "0.1.0-prototype",
            "description": "OpenAI-compatible model gateway prototype with customer, provider, model route, routing policy, usage, billing preview, safety, and audit controls.",
        },
        "servers": [{"url": f"http://{server.server_address[0]}:{server.server_address[1]}"}],
        "tags": [
            {"name": "customer", "description": "Customer-facing OpenAI-compatible endpoints."},
            {"name": "admin", "description": "Admin control-plane and reporting endpoints."},
        ],
        "components": {
            "securitySchemes": {
                "customerBearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Customer gateway API key, for example dev-gateway-key in local mock mode.",
                },
                "adminBearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Admin API key, for example dev-admin-key in local mock mode.",
                },
            }
        },
        "paths": paths,
    }


def postman_collection(server):
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"

    def bearer_auth(variable_name):
        return {
            "type": "bearer",
            "bearer": [
                {"key": "token", "value": f"{{{{{variable_name}}}}}", "type": "string"}
            ],
        }

    def request_item(name, method, path, auth_variable=None, body=None, description=""):
        item = {
            "name": name,
            "request": {
                "method": method,
                "header": [],
                "url": {
                    "raw": "{{base_url}}" + path,
                    "host": ["{{base_url}}"],
                    "path": [part for part in path.strip("/").split("/") if part],
                },
                "description": description,
            },
        }
        if auth_variable:
            item["request"]["auth"] = bearer_auth(auth_variable)
        if body is not None:
            item["request"]["header"].append({"key": "Content-Type", "value": "application/json"})
            item["request"]["body"] = {
                "mode": "raw",
                "raw": json.dumps(body, indent=2),
                "options": {"raw": {"language": "json"}},
            }
        return item

    customer_items = [
        request_item("Health", "GET", "/health"),
        request_item("OpenAPI Contract", "GET", "/openapi.json"),
        request_item("List Models", "GET", "/v1/models", "gateway_api_key"),
        request_item("Customer Self View", "GET", "/v1/gateway/me", "gateway_api_key"),
        request_item("Customer Integration Guide", "GET", "/v1/gateway/integration-guide", "gateway_api_key"),
        request_item("Customer SDK Starter", "GET", "/v1/gateway/sdk-starter", "gateway_api_key"),
        request_item(
            "Chat Completion",
            "POST",
            "/v1/chat/completions",
            "gateway_api_key",
            {
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "Explain this gateway in one sentence."}],
                "stream": False,
                "gateway_policy": "balanced",
            },
        ),
    ]
    admin_items = [
        request_item("Gateway Status", "GET", "/v1/gateway/status", "admin_api_key"),
        request_item("Handoff Checklist", "GET", "/v1/gateway/handoff-checklist", "admin_api_key"),
        request_item("Policy Presets", "GET", "/v1/gateway/policy-presets", "admin_api_key"),
        request_item("Provider Health", "GET", "/v1/gateway/provider-health", "admin_api_key"),
        request_item("Provider Contracts", "GET", "/v1/gateway/provider-contracts", "admin_api_key"),
        request_item("Evaluation Plan", "GET", "/v1/gateway/evaluation-plan", "admin_api_key"),
        request_item("Production Readiness", "GET", "/v1/gateway/production-readiness", "admin_api_key"),
        request_item("Deployment Readiness", "GET", "/v1/gateway/deployment-readiness", "admin_api_key"),
        request_item("Migration Plan", "GET", "/v1/gateway/migration-plan", "admin_api_key"),
        request_item("Production Backlog", "GET", "/v1/gateway/production-backlog", "admin_api_key"),
        request_item("Launch Plan", "GET", "/v1/gateway/launch-plan", "admin_api_key"),
        request_item("Change Management", "GET", "/v1/gateway/change-management", "admin_api_key"),
        request_item("Data Governance", "GET", "/v1/gateway/data-governance", "admin_api_key"),
        request_item("Security Review", "GET", "/v1/gateway/security-review", "admin_api_key"),
        request_item("Operations Runbook", "GET", "/v1/gateway/operations-runbook", "admin_api_key"),
        request_item("Incident Playbook", "GET", "/v1/gateway/incident-playbook", "admin_api_key"),
        request_item("Support Policy", "GET", "/v1/gateway/support-policy", "admin_api_key"),
        request_item("Pilot Checklist", "GET", "/v1/gateway/pilot-checklist", "admin_api_key"),
        request_item("Pilot Scorecard", "GET", "/v1/gateway/pilot-scorecard", "admin_api_key"),
        request_item("Discovery Checklist", "GET", "/v1/gateway/discovery-checklist", "admin_api_key"),
        request_item("Proposal Summary", "GET", "/v1/gateway/proposal-summary", "admin_api_key"),
        request_item("Onboarding Plan", "GET", "/v1/gateway/onboarding-plan", "admin_api_key"),
        request_item("Executive Brief", "GET", "/v1/gateway/executive-brief", "admin_api_key"),
        request_item("Roadmap", "GET", "/v1/gateway/roadmap", "admin_api_key"),
        request_item("Decision Guide", "GET", "/v1/gateway/decision-guide", "admin_api_key"),
        request_item("FAQ", "GET", "/v1/gateway/faq", "admin_api_key"),
        request_item("Demo Script", "GET", "/v1/gateway/demo-script", "admin_api_key"),
        request_item("Model Catalog", "GET", "/v1/gateway/model-catalog", "admin_api_key"),
        request_item("Audit Events", "GET", "/v1/gateway/audit-events", "admin_api_key"),
        request_item("Customer Reports", "GET", "/v1/gateway/customer-reports", "admin_api_key"),
        request_item("Customer Success", "GET", "/v1/gateway/customer-success", "admin_api_key"),
        request_item("Commercial Policy", "GET", "/v1/gateway/commercial-policy", "admin_api_key"),
        request_item("Procurement Pack", "GET", "/v1/gateway/procurement-pack", "admin_api_key"),
        request_item("Business Case", "GET", "/v1/gateway/business-case", "admin_api_key"),
        request_item("Implementation Plan", "GET", "/v1/gateway/implementation-plan", "admin_api_key"),
        request_item("Alternatives Pack", "GET", "/v1/gateway/alternatives-pack", "admin_api_key"),
        request_item("Invoice Preview", "GET", "/v1/gateway/invoice-preview", "admin_api_key"),
        request_item(
            "Route Preview",
            "POST",
            "/v1/gateway/route-preview",
            "admin_api_key",
            {
                "customer_id": "dev",
                "model": "smart-fast",
                "gateway_policy": "lowest_cost",
            },
        ),
        request_item(
            "Cost Estimate",
            "POST",
            "/v1/gateway/cost-estimate",
            "admin_api_key",
            {
                "customer_id": "dev",
                "model": "smart-fast",
                "prompt": "Explain the gateway.",
                "max_tokens": 256,
            },
        ),
        request_item(
            "Safety Preview",
            "POST",
            "/v1/gateway/safety-preview",
            "admin_api_key",
            {
                "messages": [{"role": "user", "content": "My email is demo@example.com"}]
            },
        ),
        request_item(
            "Create Customer",
            "POST",
            "/v1/gateway/customers",
            "admin_api_key",
            {
                "customer_id": "customer-demo",
                "name": "Customer Demo",
                "api_key": "customer-demo-key",
                "allowed_models": ["smart-fast"],
                "default_policy": "lowest_cost",
            },
        ),
        request_item(
            "Rotate Customer Key",
            "POST",
            "/v1/gateway/customers/rotate-key",
            "admin_api_key",
            {"customer_id": "customer-demo"},
        ),
        request_item(
            "Create Provider",
            "POST",
            "/v1/gateway/providers",
            "admin_api_key",
            {
                "provider_id": "demo-provider",
                "name": "Demo Provider",
                "type": "openai_compatible",
                "base_url": "https://example.com/v1",
                "api_key_env": "DEMO_PROVIDER_API_KEY",
            },
        ),
        request_item(
            "Create Model Route",
            "POST",
            "/v1/gateway/model-routes",
            "admin_api_key",
            {
                "model_id": "customer-fast",
                "provider": "dashscope",
                "upstream_model": "qwen-plus",
                "fallback_models": ["qwen-turbo"],
                "capabilities": ["chat", "streaming"],
            },
        ),
    ]
    return {
        "info": {
            "name": "AISmallRouter Gateway Prototype",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            "description": "Postman collection for the local AISmallRouter model gateway prototype.",
        },
        "variable": [
            {"key": "base_url", "value": base_url},
            {"key": "gateway_api_key", "value": DEFAULT_GATEWAY_API_KEY},
            {"key": "admin_api_key", "value": server.admin_api_key or DEFAULT_ADMIN_API_KEY},
        ],
        "item": [
            {"name": "Customer API", "item": customer_items},
            {"name": "Admin Control Plane", "item": admin_items},
        ],
    }


def demo_bundle(server):
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    return {
        "object": "gateway.demo_bundle",
        "name": "AISmallRouter Customer Demo Bundle",
        "mode": "mock" if server.mock_mode else "live",
        "base_url": base_url,
        "demo_keys": {
            "customer_key": DEFAULT_GATEWAY_API_KEY,
            "admin_key": server.admin_api_key or DEFAULT_ADMIN_API_KEY,
            "note": "Local demo keys only. Change them before production use.",
        },
        "entry_points": [
            {
                "name": "Visual dashboard",
                "url": f"{base_url}/",
                "audience": "non-technical",
                "purpose": "Show the gateway concept, request route, model catalog, and local console.",
            },
            {
                "name": "Customer self view",
                "url": f"{base_url}/v1/gateway/me",
                "audience": "customer technical",
                "purpose": "Show what one customer can use and how much they have used.",
                "auth": "customerBearerAuth",
            },
            {
                "name": "Customer integration guide",
                "url": f"{base_url}/v1/gateway/integration-guide",
                "audience": "customer technical",
                "purpose": "Show customer-specific quickstart steps, code examples, and go-live checklist.",
                "auth": "customerBearerAuth",
            },
            {
                "name": "Customer SDK starter",
                "url": f"{base_url}/v1/gateway/sdk-starter",
                "audience": "customer technical",
                "purpose": "Show .env template, starter Python and JavaScript files, first-run commands, and common errors.",
                "auth": "customerBearerAuth",
            },
            {
                "name": "Handoff checklist",
                "url": f"{base_url}/v1/gateway/handoff-checklist",
                "audience": "business, customer technical, support, and gateway owners",
                "purpose": "Show what to share with customers, what is admin-only, meeting checks, and follow-up actions.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "OpenAPI contract",
                "url": f"{base_url}/openapi.json",
                "audience": "customer technical",
                "purpose": "Import or inspect the API contract.",
            },
            {
                "name": "Production readiness report",
                "url": f"{base_url}/v1/gateway/production-readiness",
                "audience": "business and technical",
                "purpose": "Explain what is demo-ready and what still needs production work.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Deployment readiness guide",
                "url": f"{base_url}/v1/gateway/deployment-readiness",
                "audience": "customer technical, platform, security, and gateway owners",
                "purpose": "Show deployment stages, environment requirements, preflight checks, operational checks, rollback, and deployment options.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Migration plan",
                "url": f"{base_url}/v1/gateway/migration-plan",
                "audience": "business, customer technical, platform, support, and gateway owners",
                "purpose": "Show phased customer migration, cutover checklist, rollback path, and evidence endpoints.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Production hardening backlog",
                "url": f"{base_url}/v1/gateway/production-backlog",
                "audience": "business, gateway, platform, security, and support owners",
                "purpose": "Turn prototype gaps into prioritized P0, P1, and P2 engineering tasks.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Production launch plan",
                "url": f"{base_url}/v1/gateway/launch-plan",
                "audience": "business, technical, support, and operations",
                "purpose": "Show go-live gates, signoffs, rollout stages, blockers, and next action.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Change management plan",
                "url": f"{base_url}/v1/gateway/change-management",
                "audience": "gateway owner, support, and customer technical",
                "purpose": "Show change approval checklist, rollback paths, and evidence endpoints.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Data governance review",
                "url": f"{base_url}/v1/gateway/data-governance",
                "audience": "business, security, customer data, and gateway owners",
                "purpose": "Explain prompt handling, logs, retention, customer keys, provider secrets, and production gaps.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Security review",
                "url": f"{base_url}/v1/gateway/security-review",
                "audience": "business, customer security, gateway, and support owners",
                "purpose": "Explain key risks, current controls, production controls, customer security questions, and go-live gates.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Operations runbook",
                "url": f"{base_url}/v1/gateway/operations-runbook",
                "audience": "support, gateway, platform, and business owners",
                "purpose": "Show SLO-style targets, daily checks, alert actions, ownership, and evidence endpoints for customer pilots.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Customer success summary",
                "url": f"{base_url}/v1/gateway/customer-success",
                "audience": "business, support, and customer success",
                "purpose": "Show customer health, risk reasons, recommended action, and evidence for follow-up.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Commercial policy",
                "url": f"{base_url}/v1/gateway/commercial-policy",
                "audience": "business, customer sponsor, finance, and gateway owners",
                "purpose": "Explain pricing assumptions, budget behavior, invoice preview limits, exclusions, and production contract questions.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Procurement review pack",
                "url": f"{base_url}/v1/gateway/procurement-pack",
                "audience": "customer sponsor, procurement, legal, IT, security, finance, and gateway owner",
                "purpose": "Package review tracks, evidence documents, approval owners, red lines, and meeting questions for pilot or purchase review.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Business case",
                "url": f"{base_url}/v1/gateway/business-case",
                "audience": "business sponsor, customer sponsor, finance, procurement, and gateway owner",
                "purpose": "Explain value hypotheses, pilot metrics, ROI inputs to collect, decision options, and what the prototype does not claim.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Implementation plan",
                "url": f"{base_url}/v1/gateway/implementation-plan",
                "audience": "customer sponsor, delivery, platform, security, finance, and gateway owners",
                "purpose": "Explain delivery phases, rough duration ranges, roles, risks, assumptions, and acceptance evidence.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Alternatives pack",
                "url": f"{base_url}/v1/gateway/alternatives-pack",
                "audience": "customer sponsor, solution architect, procurement, platform owner, and gateway owner",
                "purpose": "Compare direct provider integration, API Gateway, managed AI Gateway, OpenRouter-like options, and custom AISmallRouter.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Provider contract matrix",
                "url": f"{base_url}/v1/gateway/provider-contracts",
                "audience": "business and technical",
                "purpose": "Show why different model providers need adapters, tests, and normalization.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Evaluation plan",
                "url": f"{base_url}/v1/gateway/evaluation-plan",
                "audience": "business, customer technical, data, support, and gateway owners",
                "purpose": "Show how model quality, reliability, latency, cost, safety, and fallback should be compared before routing real traffic.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Incident playbook",
                "url": f"{base_url}/v1/gateway/incident-playbook",
                "audience": "business and support",
                "purpose": "Show common failure scenarios, operator steps, and customer-safe wording.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Support policy",
                "url": f"{base_url}/v1/gateway/support-policy",
                "audience": "business and support",
                "purpose": "Explain prototype, pilot, and production support expectations.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Pilot checklist",
                "url": f"{base_url}/v1/gateway/pilot-checklist",
                "audience": "business and technical",
                "purpose": "Show what to prepare before, during, and after a customer pilot.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Pilot scorecard",
                "url": f"{base_url}/v1/gateway/pilot-scorecard",
                "audience": "business, customer success, support, and customer technical",
                "purpose": "Score pilot progress and recommend discovery, extended pilot, or production hardening.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Discovery checklist",
                "url": f"{base_url}/v1/gateway/discovery-checklist",
                "audience": "sales, solution architect, business, and customer technical",
                "purpose": "Turn a broad OpenRouter-like idea into clear scope, risks, evidence, and pilot next steps.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Proposal summary",
                "url": f"{base_url}/v1/gateway/proposal-summary",
                "audience": "customer sponsor, business, solution architect, and technical owner",
                "purpose": "Summarize recommended scope, exclusions, deliverables, decision points, risks, and next steps.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Onboarding plan",
                "url": f"{base_url}/v1/gateway/onboarding-plan",
                "audience": "business, support, and customer technical",
                "purpose": "Show a simple day-by-day path from first customer meeting to pilot decision.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Executive brief",
                "url": f"{base_url}/v1/gateway/executive-brief",
                "audience": "business and non-technical",
                "purpose": "Show a one-page business summary, demo story, risks, and next step.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Roadmap",
                "url": f"{base_url}/v1/gateway/roadmap",
                "audience": "business and technical",
                "purpose": "Show phases from prototype to pilot, production hardening, and multi-provider expansion.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Decision guide",
                "url": f"{base_url}/v1/gateway/decision-guide",
                "audience": "business and technical",
                "purpose": "Compare normal API Gateway, managed AI Gateway, custom Model Gateway, and OpenRouter-like marketplace paths.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "FAQ",
                "url": f"{base_url}/v1/gateway/faq",
                "audience": "business and technical",
                "purpose": "Answer common customer questions about API Gateway, OpenRouter, cost, safety, providers, pilot, and production gaps.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Demo script",
                "url": f"{base_url}/v1/gateway/demo-script",
                "audience": "business and sales",
                "purpose": "Give a 15 minute customer walkthrough with talk track, endpoints to show, and likely questions.",
                "auth": "adminBearerAuth",
            },
            {
                "name": "Postman collection",
                "url": f"{base_url}/postman_collection.json",
                "audience": "customer technical",
                "purpose": "Click through common demo requests quickly.",
            },
            {
                "name": "Customer guide PDF",
                "path": "Model_Gateway_Customer_Guide.pdf",
                "audience": "business and technical",
                "purpose": "Simple English explanation of the direction, architecture, and hard parts.",
            },
        ],
        "recommended_demo_flow": [
            {
                "step": 1,
                "title": "Explain the one API idea",
                "show": "/ and /v1/gateway/demo-script",
                "talk_track": "Use the demo script as the presenter guide, then show that customer apps call one OpenAI-compatible API while the gateway manages providers and routes.",
            },
            {
                "step": 2,
                "title": "Show models and customer access",
                "show": "/v1/models, /v1/gateway/me, and /v1/gateway/integration-guide",
                "talk_track": "The customer sees public model names, their own access, and practical integration examples, not provider secrets.",
            },
            {
                "step": 3,
                "title": "Preview route decision",
                "show": "/v1/gateway/route-preview and /v1/gateway/provider-contracts",
                "talk_track": "Route preview explains provider choice, while provider contracts explain why adapters and tests are needed.",
            },
            {
                "step": 4,
                "title": "Run a mock chat request",
                "show": "/v1/chat/completions",
                "talk_track": "Mock mode returns a provider-like response and route trace without paid model usage.",
            },
            {
                "step": 5,
                "title": "Show control plane",
                "show": "/v1/gateway/status, /v1/gateway/production-readiness, /v1/gateway/deployment-readiness, /v1/gateway/launch-plan, and /v1/gateway/data-governance",
                "talk_track": "Admin views show provider health, customer usage, alerts, readiness, deployment checks, launch gates, data governance, audit events, and invoice preview.",
            },
            {
                "step": 6,
                "title": "Agree onboarding plan",
                "show": "/v1/gateway/discovery-checklist, /v1/gateway/proposal-summary, /v1/gateway/onboarding-plan, and /v1/gateway/pilot-checklist",
                "talk_track": "Use discovery to confirm the first use case and provider scope, proposal summary to align expectations, then onboarding and pilot checklist to keep the work measurable.",
            },
            {
                "step": 7,
                "title": "Hand off technical artifacts",
                "show": "/openapi.json and /postman_collection.json",
                "talk_track": "The customer technical team can import the contract or Postman collection.",
            },
        ],
        "quick_commands": [
            {
                "name": "List models",
                "command": f"curl {base_url}/v1/models -H 'Authorization: Bearer {DEFAULT_GATEWAY_API_KEY}'",
            },
            {
                "name": "Customer self view",
                "command": f"curl {base_url}/v1/gateway/me -H 'Authorization: Bearer {DEFAULT_GATEWAY_API_KEY}'",
            },
            {
                "name": "Customer integration guide",
                "command": f"curl {base_url}/v1/gateway/integration-guide -H 'Authorization: Bearer {DEFAULT_GATEWAY_API_KEY}'",
            },
            {
                "name": "Customer SDK starter",
                "command": f"curl {base_url}/v1/gateway/sdk-starter -H 'Authorization: Bearer {DEFAULT_GATEWAY_API_KEY}'",
            },
            {
                "name": "Demo script",
                "command": f"curl {base_url}/v1/gateway/demo-script -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Route preview",
                "command": f"curl {base_url}/v1/gateway/route-preview -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}' -H 'Content-Type: application/json' -d '{{\"customer_id\":\"dev\",\"model\":\"smart-fast\",\"gateway_policy\":\"lowest_cost\"}}'",
            },
            {
                "name": "Mock chat",
                "command": f"curl {base_url}/v1/chat/completions -H 'Authorization: Bearer {DEFAULT_GATEWAY_API_KEY}' -H 'Content-Type: application/json' -d '{{\"model\":\"smart-fast\",\"messages\":[{{\"role\":\"user\",\"content\":\"Explain this gateway in one sentence.\"}}]}}'",
            },
            {
                "name": "Gateway status",
                "command": f"curl {base_url}/v1/gateway/status -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Handoff checklist",
                "command": f"curl {base_url}/v1/gateway/handoff-checklist -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Production readiness",
                "command": f"curl {base_url}/v1/gateway/production-readiness -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Deployment readiness",
                "command": f"curl {base_url}/v1/gateway/deployment-readiness -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Migration plan",
                "command": f"curl {base_url}/v1/gateway/migration-plan -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Production backlog",
                "command": f"curl {base_url}/v1/gateway/production-backlog -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Launch plan",
                "command": f"curl {base_url}/v1/gateway/launch-plan -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Change management",
                "command": f"curl {base_url}/v1/gateway/change-management -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Data governance",
                "command": f"curl {base_url}/v1/gateway/data-governance -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Security review",
                "command": f"curl {base_url}/v1/gateway/security-review -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Operations runbook",
                "command": f"curl {base_url}/v1/gateway/operations-runbook -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Customer success",
                "command": f"curl {base_url}/v1/gateway/customer-success -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Commercial policy",
                "command": f"curl {base_url}/v1/gateway/commercial-policy -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Procurement pack",
                "command": f"curl {base_url}/v1/gateway/procurement-pack -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Business case",
                "command": f"curl {base_url}/v1/gateway/business-case -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Implementation plan",
                "command": f"curl {base_url}/v1/gateway/implementation-plan -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Alternatives pack",
                "command": f"curl {base_url}/v1/gateway/alternatives-pack -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Provider contracts",
                "command": f"curl {base_url}/v1/gateway/provider-contracts -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Evaluation plan",
                "command": f"curl {base_url}/v1/gateway/evaluation-plan -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Incident playbook",
                "command": f"curl {base_url}/v1/gateway/incident-playbook -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Support policy",
                "command": f"curl {base_url}/v1/gateway/support-policy -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Pilot checklist",
                "command": f"curl {base_url}/v1/gateway/pilot-checklist -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Pilot scorecard",
                "command": f"curl {base_url}/v1/gateway/pilot-scorecard -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Discovery checklist",
                "command": f"curl {base_url}/v1/gateway/discovery-checklist -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Proposal summary",
                "command": f"curl {base_url}/v1/gateway/proposal-summary -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Onboarding plan",
                "command": f"curl {base_url}/v1/gateway/onboarding-plan -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Executive brief",
                "command": f"curl {base_url}/v1/gateway/executive-brief -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Roadmap",
                "command": f"curl {base_url}/v1/gateway/roadmap -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "Decision guide",
                "command": f"curl {base_url}/v1/gateway/decision-guide -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
            {
                "name": "FAQ",
                "command": f"curl {base_url}/v1/gateway/faq -H 'Authorization: Bearer {server.admin_api_key or DEFAULT_ADMIN_API_KEY}'",
            },
        ],
        "production_notes": [
            "Replace demo keys before production.",
            "Move customer, provider, and route config from local JSON to a database.",
            "Store provider secrets in a secret manager.",
            "Add approval workflow, audit retention, billing rules, and deployment controls.",
            "Approve prompt logging, log retention, data deletion, and sensitive data handling rules.",
        ],
    }


def gateway_status(server):
    return {
        "status": "ok",
        "mode": "mock" if server.mock_mode else "live",
        "database": server.db_path,
        "summary": db_summary(server.db_path),
        "alerts": gateway_alerts(server),
        "config_check": gateway_config_check(server),
        "handoff_checklist": handoff_checklist(server),
        "provider_summary": provider_status(server),
        "provider_health": provider_health(server),
        "model_catalog": model_catalog(server),
        "evaluation_plan": evaluation_plan(server),
        "access_matrix": access_matrix(server),
        "customer_reports": customer_reports(server),
        "customer_success": customer_success_summary(server),
        "commercial_policy": commercial_policy(server),
        "procurement_pack": procurement_pack(server),
        "business_case": business_case(server),
        "implementation_plan": implementation_plan(server),
        "alternatives_pack": alternatives_pack(server),
        "pilot_scorecard": pilot_scorecard(server),
        "invoice_preview": invoice_preview(server),
        "production_readiness": production_readiness(server),
        "deployment_readiness": deployment_readiness(server),
        "migration_plan": migration_plan(server),
        "production_backlog": production_backlog(server),
        "launch_plan": launch_plan(server),
        "change_management": change_management_plan(server),
        "data_governance": data_governance_review(server),
        "security_review": security_review(server),
        "operations_runbook": operations_runbook(server),
        "discovery_checklist": discovery_checklist(server),
        "proposal_summary": proposal_summary(server),
        "request_activity": request_activity(server.db_path, limit=10),
        "audit_events": audit_events(server.db_path, limit=10),
        "usage_by_customer": usage_grouped_by(server.db_path, "customer_id"),
        "usage_by_model": usage_grouped_by(server.db_path, "model"),
        "request_limit": server.default_request_limit,
        "limit_window_seconds": server.default_limit_window_seconds,
        "customers": [
            {
                **public_customer_view(customer),
                "budget": customer_budget_status(server.db_path, customer),
            }
            for customer in server.customers_by_key.values()
        ],
        "models": [
            {
                "id": public_name,
                "provider": model["provider"],
                "upstream_model": model["upstream_model"],
                "fallback_models": model.get("fallback_models", []),
                "capabilities": model.get("capabilities", []),
            }
            for public_name, model in sorted(server.models.items())
        ],
    }


def dashboard_html(server):
    state = json.dumps(gateway_status(server), ensure_ascii=False)
    dashboard_path = os.getenv("GATEWAY_DASHBOARD_PATH", DEFAULT_DASHBOARD_PATH)
    if os.path.exists(dashboard_path):
        with open(dashboard_path, "r", encoding="utf-8") as file:
            return (
                file.read()
                .replace("__GATEWAY_STATE__", state)
                .replace("__ADMIN_API_KEY__", server.admin_api_key)
            )
    return "<!doctype html><title>Model Gateway</title><h1>Model Gateway</h1>"


def admin_html(server):
    request_rows = ""
    for record in db_tail(server.db_path, "requests", 25):
        request_rows += (
            "<tr>"
            f"<td>{record.get('created', '')}</td>"
            f"<td>{record.get('customer_id', '')}</td>"
            f"<td>{record.get('model', '')}</td>"
            f"<td>{record.get('resolved_model', '')}</td>"
            f"<td>{record.get('provider', '')}</td>"
            f"<td>{record.get('status', '')}</td>"
            f"<td>{record.get('latency_ms', '')}</td>"
            "</tr>"
        )
    usage_rows = ""
    for record in db_tail(server.db_path, "usage_records", 25):
        usage_rows += (
            "<tr>"
            f"<td>{record.get('created', '')}</td>"
            f"<td>{record.get('customer_id', '')}</td>"
            f"<td>{record.get('model', '')}</td>"
            f"<td>{record.get('provider', '')}</td>"
            f"<td>{record.get('prompt_tokens', '')}</td>"
            f"<td>{record.get('completion_tokens', '')}</td>"
            f"<td>{record.get('estimated_cost', '')}</td>"
            "</tr>"
        )
    customer_rows = ""
    for record in db_tail(server.db_path, "customers", 100):
        customer_rows += (
            "<tr>"
            f"<td>{record.get('id', '')}</td>"
            f"<td>{record.get('name', '')}</td>"
            f"<td>{record.get('plan', '')}</td>"
            f"<td>{record.get('request_limit', '')}</td>"
            f"<td>{record.get('token_budget', '')}</td>"
            f"<td>{record.get('cost_budget', '')}</td>"
            f"<td>{record.get('allowed_models', '')}</td>"
            f"<td>{record.get('byok_providers', '')}</td>"
            "</tr>"
        )
    provider_rows = ""
    for record in provider_status(server):
        provider_rows += (
            "<tr>"
            f"<td>{record.get('id', '')}</td>"
            f"<td>{record.get('type', '')}</td>"
            f"<td>{', '.join(record.get('models', []))}</td>"
            f"<td>{record.get('api_key_env', '')}</td>"
            f"<td>{record.get('server_key_configured', '')}</td>"
            f"<td>{', '.join(record.get('byok_customers', []))}</td>"
            f"<td>{record.get('usage', {}).get('total_tokens', 0)}</td>"
            "</tr>"
        )
    provider_health_rows = ""
    for record in provider_health(server):
        recent = record.get("recent", {})
        provider_health_rows += (
            "<tr>"
            f"<td>{record.get('id', '')}</td>"
            f"<td>{record.get('status', '')}</td>"
            f"<td>{record.get('reason', '')}</td>"
            f"<td>{record.get('live_ready', '')}</td>"
            f"<td>{record.get('mock_ready', '')}</td>"
            f"<td>{recent.get('requests', 0)}</td>"
            f"<td>{recent.get('errors', 0)}</td>"
            f"<td>{recent.get('avg_latency_ms', 0)}</td>"
            "</tr>"
        )
    model_catalog_rows = ""
    for record in model_catalog(server):
        usage = record.get("usage", {})
        model_catalog_rows += (
            "<tr>"
            f"<td>{record.get('id', '')}</td>"
            f"<td>{record.get('provider', '')}</td>"
            f"<td>{record.get('provider_status', '')}</td>"
            f"<td>{record.get('upstream_model', '')}</td>"
            f"<td>{' -> '.join(record.get('route_chain', []))}</td>"
            f"<td>{', '.join(record.get('capabilities', []))}</td>"
            f"<td>{usage.get('requests', 0)}</td>"
            f"<td>{usage.get('errors', 0)}</td>"
            f"<td>{usage.get('total_tokens', 0)}</td>"
            "</tr>"
        )
    customer_usage_rows = ""
    for record in usage_grouped_by(server.db_path, "customer_id"):
        customer_usage_rows += (
            "<tr>"
            f"<td>{record.get('id', '')}</td>"
            f"<td>{record.get('usage_records', 0)}</td>"
            f"<td>{record.get('total_tokens', 0)}</td>"
            f"<td>{record.get('estimated_cost', 0)}</td>"
            "</tr>"
        )
    customer_report_rows = ""
    for record in customer_reports(server):
        budget = record.get("budget", {})
        summary = record.get("request_summary", {})
        customer_report_rows += (
            "<tr>"
            f"<td>{record.get('id', '')}</td>"
            f"<td>{record.get('plan', '')}</td>"
            f"<td>{record.get('budget_state', '')}</td>"
            f"<td>{summary.get('requests', 0)}</td>"
            f"<td>{summary.get('errors', 0)}</td>"
            f"<td>{summary.get('avg_latency_ms', 0)}</td>"
            f"<td>{budget.get('usage', {}).get('total_tokens', 0)}</td>"
            f"<td>{budget.get('remaining_tokens', '')}</td>"
            f"<td>{budget.get('remaining_cost', '')}</td>"
            "</tr>"
        )
    access_matrix_rows = ""
    for customer in access_matrix(server):
        for model in customer.get("models", []):
            access_matrix_rows += (
                "<tr>"
                f"<td>{customer.get('id', '')}</td>"
                f"<td>{customer.get('plan', '')}</td>"
                f"<td>{customer.get('budget_state', '')}</td>"
                f"<td>{model.get('model', '')}</td>"
                f"<td>{model.get('allowed', '')}</td>"
                f"<td>{model.get('provider', '')}</td>"
                f"<td>{model.get('provider_status', '')}</td>"
                f"<td>{model.get('reason', '')}</td>"
                "</tr>"
            )
    model_usage_rows = ""
    for record in usage_grouped_by(server.db_path, "model"):
        model_usage_rows += (
            "<tr>"
            f"<td>{record.get('id', '')}</td>"
            f"<td>{record.get('usage_records', 0)}</td>"
            f"<td>{record.get('total_tokens', 0)}</td>"
            f"<td>{record.get('estimated_cost', 0)}</td>"
            "</tr>"
        )
    config_check = gateway_config_check(server)
    alerts = gateway_alerts(server)
    alert_rows = ""
    for record in alerts.get("alerts", []):
        alert_rows += (
            "<tr>"
            f"<td>{record.get('severity', '')}</td>"
            f"<td>{record.get('area', '')}</td>"
            f"<td>{record.get('code', '')}</td>"
            f"<td>{record.get('message', '')}</td>"
            f"<td>{record.get('next_step', '')}</td>"
            "</tr>"
        )
    config_rows = ""
    for record in config_check.get("checks", []):
        config_rows += (
            "<tr>"
            f"<td>{record.get('severity', '')}</td>"
            f"<td>{record.get('code', '')}</td>"
            f"<td>{record.get('message', '')}</td>"
            f"<td>{json.dumps(record.get('details', {}))}</td>"
            "</tr>"
        )
    activity_rows = ""
    for record in request_activity(server.db_path, limit=50):
        activity_rows += (
            "<tr>"
            f"<td>{record.get('created', '')}</td>"
            f"<td>{record.get('outcome', '')}</td>"
            f"<td>{record.get('customer_id', '')}</td>"
            f"<td>{record.get('model', '')}</td>"
            f"<td>{record.get('resolved_model', '')}</td>"
            f"<td>{record.get('provider', '')}</td>"
            f"<td>{record.get('code', '')}</td>"
            f"<td>{record.get('latency_ms', '')}</td>"
            "</tr>"
        )
    summary = db_summary(server.db_path)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gateway Admin</title>
  <style>
    body {{ margin: 0; background: #0b0d10; color: #f3f5f7; font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 16px; font-size: 24px; }}
    h2 {{ margin: 28px 0 10px; font-size: 16px; }}
    table {{ width: 100%; border-collapse: collapse; background: #11141a; border: 1px solid #2b3038; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #2b3038; text-align: left; font-size: 13px; vertical-align: top; }}
    th {{ color: #a7b0bf; background: #151923; }}
    a {{ color: #41d99d; }}
  </style>
</head>
<body>
  <main>
    <h1>Gateway Admin</h1>
    <p><a href="/">Dashboard</a> | <a href="/v1/gateway/status">Status JSON</a> | <a href="/v1/gateway/alerts">Alerts JSON</a> | <a href="/v1/gateway/access-matrix">Access Matrix JSON</a> | <a href="/v1/gateway/config-check">Config Check JSON</a> | <a href="/v1/gateway/provider-health">Provider Health JSON</a> | <a href="/v1/gateway/model-catalog">Model Catalog JSON</a> | <a href="/v1/gateway/customer-reports">Customer Reports JSON</a> | <a href="/v1/gateway/request-activity">Request Activity JSON</a> | <a href="/v1/gateway/providers">Providers JSON</a> | <a href="/v1/gateway/customer-usage">Customer Usage JSON</a> | <a href="/v1/gateway/model-usage">Model Usage JSON</a> | <a href="/v1/gateway/requests">Requests JSON</a> | <a href="/v1/gateway/usage">Usage JSON</a> | <a href="/v1/gateway/customers">Customers JSON</a></p>
    <h2>Summary</h2>
    <table>
      <tbody>
        <tr><th>Total requests</th><td>{summary.get('request_count', 0)}</td></tr>
        <tr><th>Total tokens</th><td>{summary.get('usage', {}).get('total_tokens', 0)}</td></tr>
        <tr><th>Estimated cost</th><td>{summary.get('usage', {}).get('estimated_cost', 0)}</td></tr>
        <tr><th>Config status</th><td>{config_check.get('status')}</td></tr>
        <tr><th>Alert status</th><td>{alerts.get('status')}</td></tr>
        <tr><th>Database</th><td>{server.db_path}</td></tr>
      </tbody>
    </table>
    <h2>Alerts</h2>
    <table>
      <thead><tr><th>Severity</th><th>Area</th><th>Code</th><th>Message</th><th>Next step</th></tr></thead>
      <tbody>{alert_rows}</tbody>
    </table>
    <h2>Config Check</h2>
    <table>
      <thead><tr><th>Severity</th><th>Code</th><th>Message</th><th>Details</th></tr></thead>
      <tbody>{config_rows}</tbody>
    </table>
    <h2>Recent Requests</h2>
    <table>
      <thead><tr><th>Created</th><th>Customer</th><th>Public model</th><th>Resolved</th><th>Provider</th><th>Status</th><th>Latency ms</th></tr></thead>
      <tbody>{request_rows}</tbody>
    </table>
    <h2>Request Activity</h2>
    <table>
      <thead><tr><th>Created</th><th>Outcome</th><th>Customer</th><th>Public model</th><th>Resolved</th><th>Provider</th><th>Code</th><th>Latency ms</th></tr></thead>
      <tbody>{activity_rows}</tbody>
    </table>
    <h2>Recent Usage</h2>
    <table>
      <thead><tr><th>Created</th><th>Customer</th><th>Model</th><th>Provider</th><th>Prompt</th><th>Completion</th><th>Est. cost</th></tr></thead>
      <tbody>{usage_rows}</tbody>
    </table>
    <h2>Provider Status</h2>
    <table>
      <thead><tr><th>ID</th><th>Type</th><th>Models</th><th>Key env</th><th>Server key?</th><th>BYOK customers</th><th>Total tokens</th></tr></thead>
      <tbody>{provider_rows}</tbody>
    </table>
    <h2>Provider Health</h2>
    <table>
      <thead><tr><th>ID</th><th>Status</th><th>Reason</th><th>Live ready?</th><th>Mock ready?</th><th>Recent requests</th><th>Errors</th><th>Avg latency ms</th></tr></thead>
      <tbody>{provider_health_rows}</tbody>
    </table>
    <h2>Model Catalog</h2>
    <table>
      <thead><tr><th>Public model</th><th>Provider</th><th>Provider status</th><th>Upstream model</th><th>Route chain</th><th>Capabilities</th><th>Requests</th><th>Errors</th><th>Total tokens</th></tr></thead>
      <tbody>{model_catalog_rows}</tbody>
    </table>
    <h2>Usage By Customer</h2>
    <table>
      <thead><tr><th>Customer</th><th>Usage records</th><th>Total tokens</th><th>Est. cost</th></tr></thead>
      <tbody>{customer_usage_rows}</tbody>
    </table>
    <h2>Customer Reports</h2>
    <table>
      <thead><tr><th>Customer</th><th>Plan</th><th>Budget state</th><th>Requests</th><th>Errors</th><th>Avg latency ms</th><th>Total tokens</th><th>Remaining tokens</th><th>Remaining cost</th></tr></thead>
      <tbody>{customer_report_rows}</tbody>
    </table>
    <h2>Access Matrix</h2>
    <table>
      <thead><tr><th>Customer</th><th>Plan</th><th>Budget state</th><th>Model</th><th>Allowed?</th><th>Provider</th><th>Provider status</th><th>Reason</th></tr></thead>
      <tbody>{access_matrix_rows}</tbody>
    </table>
    <h2>Usage By Model</h2>
    <table>
      <thead><tr><th>Model</th><th>Usage records</th><th>Total tokens</th><th>Est. cost</th></tr></thead>
      <tbody>{model_usage_rows}</tbody>
    </table>
    <h2>Customers</h2>
    <table>
      <thead><tr><th>ID</th><th>Name</th><th>Plan</th><th>Req limit</th><th>Token budget</th><th>Cost budget</th><th>Allowed models</th><th>BYOK providers</th></tr></thead>
      <tbody>{customer_rows}</tbody>
    </table>
  </main>
</body>
</html>"""


class OpenAICompatibleAdapter:
    def __init__(self, mock_mode, customer=None):
        self.mock_mode = mock_mode
        self.customer = customer or {}

    def complete(self, model_config, request_payload, timeout, route_trace):
        if self.mock_mode:
            return self.mock_completion(model_config, request_payload, route_trace)
        return self.live_completion(model_config, request_payload, timeout)

    def stream(self, model_config, request_payload, timeout, route_trace):
        if self.mock_mode:
            yield from self.mock_stream(model_config, request_payload, route_trace)
            return
        yield from self.live_stream(model_config, request_payload, timeout)

    def mock_completion(self, model_config, request_payload, route_trace):
        public_model = request_payload["model"]
        upstream_model = model_config["upstream_model"]
        last_user_message = ""
        for message in reversed(request_payload.get("messages", [])):
            if message.get("role") == "user":
                last_user_message = message.get("content", "")
                break
        content = (
            "Mock response from the Model Gateway. "
            f"The public model '{public_model}' was routed to upstream model '{upstream_model}'."
        )
        if last_user_message:
            content += f" Last user message: {last_user_message[:160]}"
        message = {"role": "assistant", "content": content}
        finish_reason = "stop"
        function_tool = first_openai_function_tool(request_payload.get("tools", []))
        if function_tool:
            finish_reason = "tool_calls"
            message["content"] = None
            message["tool_calls"] = [
                {
                    "id": f"call_mock_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": function_tool["name"],
                        "arguments": json.dumps(
                            {
                                "mock": True,
                                "reason": "AISmallRouter mock tool call",
                                "last_user_message": last_user_message[:120],
                            }
                        ),
                    },
                }
            ]
        completion_tokens = estimate_tokens([], content)
        prompt_tokens = estimate_tokens(request_payload.get("messages", []), "")
        return {
            "id": f"chatcmpl-mock-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": now_unix(),
            "model": public_model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "gateway": {
                "mode": "mock",
                "provider": model_config["provider"],
                "resolved_model": upstream_model,
                "tool_support": "mock_tool_call" if function_tool else "none_requested",
                "route_trace": route_trace + ["Mock mode returns a simulated provider response"],
            },
        }

    def mock_stream(self, model_config, request_payload, route_trace):
        completion = self.mock_completion(model_config, request_payload, route_trace)
        content = completion["choices"][0]["message"].get("content") or "Mock response requested a tool call."
        chunk_id = completion["id"]
        for word in content.split(" "):
            yield {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": completion["created"],
                "model": request_payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": word + " "},
                        "finish_reason": None,
                    }
                ],
            }
            time.sleep(0.01)
        yield {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": completion["created"],
            "model": request_payload["model"],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": completion["usage"],
            "gateway": completion["gateway"],
        }

    def live_completion(self, model_config, request_payload, timeout):
        provider = model_config["provider_config"]
        api_key = self.provider_api_key(provider)
        if not api_key:
            raise GatewayError(f"Missing provider API key. Set {provider['api_key_env']}.", "missing_provider_key", 500)
        upstream_payload = self.provider_payload(request_payload)
        upstream_payload["model"] = model_config["upstream_model"]
        upstream_payload["stream"] = False
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(upstream_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                payload.setdefault("gateway", {})
                if request_payload.get("tools"):
                    payload["gateway"]["tool_support"] = "openai_compatible_passthrough"
                return payload
        except urllib.error.HTTPError as exc:
            raise ProviderError("The upstream provider returned an error.", exc.code, exc.read().decode("utf-8", errors="replace"))

    def live_stream(self, model_config, request_payload, timeout):
        provider = model_config["provider_config"]
        api_key = self.provider_api_key(provider)
        if not api_key:
            raise GatewayError(f"Missing provider API key. Set {provider['api_key_env']}.", "missing_provider_key", 500)
        upstream_payload = self.provider_payload(request_payload)
        upstream_payload["model"] = model_config["upstream_model"]
        upstream_payload["stream"] = True
        upstream_payload.setdefault("stream_options", {"include_usage": True})
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(upstream_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    yield json.loads(data)
        except urllib.error.HTTPError as exc:
            raise ProviderError("The upstream provider returned an error.", exc.code, exc.read().decode("utf-8", errors="replace"))

    def provider_payload(self, request_payload):
        return {
            key: value
            for key, value in request_payload.items()
            if not key.startswith("gateway_")
        }

    def provider_api_key(self, provider):
        byok = self.customer.get("provider_api_keys", {})
        customer_key = resolve_secret(byok.get(provider["id"]))
        if customer_key:
            return customer_key
        return os.getenv(provider["api_key_env"])


class AnthropicAdapter(OpenAICompatibleAdapter):
    def live_completion(self, model_config, request_payload, timeout):
        provider = model_config["provider_config"]
        api_key = self.provider_api_key(provider)
        if not api_key:
            raise GatewayError(f"Missing provider API key. Set {provider['api_key_env']}.", "missing_provider_key", 500)
        upstream_payload = self.provider_payload(request_payload)
        upstream_payload["model"] = model_config["upstream_model"]
        url = provider["base_url"].rstrip("/") + "/messages"
        request = urllib.request.Request(
            url,
            data=json.dumps(upstream_payload).encode("utf-8"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": provider.get("api_version", "2023-06-01"),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                upstream = json.loads(response.read().decode("utf-8"))
                payload = self.openai_response_from_anthropic(upstream, request_payload["model"])
                payload.setdefault("gateway", {})
                if request_payload.get("tools"):
                    payload["gateway"]["tool_support"] = "anthropic_normalized"
                return payload
        except urllib.error.HTTPError as exc:
            raise ProviderError("The upstream provider returned an error.", exc.code, exc.read().decode("utf-8", errors="replace"))

    def live_stream(self, model_config, request_payload, timeout):
        raise GatewayError(
            "Anthropic live streaming is not implemented in this prototype yet.",
            "streaming_not_implemented",
            501,
        )

    def provider_payload(self, request_payload):
        messages = []
        system_text = None
        for message in request_payload.get("messages", []):
            role = message.get("role")
            if role == "system":
                system_text = message.get("content", "")
                continue
            if role in {"user", "assistant"}:
                messages.append({"role": role, "content": message.get("content", "")})
        payload = {
            "messages": messages,
            "max_tokens": request_payload.get("max_tokens", 1024),
        }
        if system_text:
            payload["system"] = system_text
        if "temperature" in request_payload:
            payload["temperature"] = request_payload["temperature"]
        if "tools" in request_payload:
            payload["tools"] = normalize_tools_for_anthropic(request_payload["tools"])
        return payload

    def openai_response_from_anthropic(self, upstream, public_model):
        text_parts = []
        tool_calls = []
        for item in upstream.get("content", []):
            if item.get("type") == "text":
                text_parts.append(item.get("text", ""))
            if item.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": item.get("id"),
                        "type": "function",
                        "function": {
                            "name": item.get("name"),
                            "arguments": json.dumps(item.get("input", {})),
                        },
                    }
                )
        message = {"role": "assistant", "content": "\n".join(text_parts)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        usage = upstream.get("usage", {})
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        return {
            "id": upstream.get("id", f"chatcmpl-anthropic-{uuid.uuid4().hex[:12]}"),
            "object": "chat.completion",
            "created": now_unix(),
            "model": public_model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": upstream.get("stop_reason") or "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }


def normalize_tools_for_anthropic(tools):
    normalized = []
    for tool in tools or []:
        if tool.get("type") != "function":
            continue
        function = tool.get("function", {})
        normalized.append(
            {
                "name": function.get("name"),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return normalized


def adapter_for(model_config, mock_mode, customer):
    provider_type = model_config["provider_config"].get("type", "openai_compatible")
    if provider_type == "openai_compatible":
        return OpenAICompatibleAdapter(mock_mode, customer)
    if provider_type == "anthropic":
        return AnthropicAdapter(mock_mode, customer)
    raise GatewayError(f"Unsupported provider type: {provider_type}.", "unsupported_provider_type", 500)


def should_force_failover(payload, candidate_index):
    value = payload.get("gateway_force_failover")
    return bool(value and candidate_index == 0)


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "AISmallRouter/0.9"

    def log_message(self, format_text, *args):
        if self.server.quiet:
            return
        super().log_message(format_text, *args)

    def parsed_path(self):
        return urlparse(self.path)

    def route_path(self):
        return self.parsed_path().path

    def do_HEAD(self):
        path = self.route_path()
        if path in {"/", "/dashboard", "/admin"}:
            if path == "/admin" and not self.authenticate_admin():
                return
            html = admin_html(self.server) if path == "/admin" else dashboard_html(self.server)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode("utf-8"))))
            self.end_headers()
            return
        if path in ADMIN_PATHS and not self.authenticate_admin():
            return
        self.send_response(200 if path in {"/health", "/openapi.json", "/postman_collection.json"} or path in ADMIN_PATHS else 404)
        self.end_headers()

    def do_GET(self):
        path = self.route_path()
        query = parse_qs(self.parsed_path().query)
        if path in {"/", "/dashboard"}:
            make_html_response(self, 200, dashboard_html(self.server))
            return
        if path == "/openapi.json":
            make_json_response(self, 200, openapi_spec(self.server))
            return
        if path == "/postman_collection.json":
            make_json_response(self, 200, postman_collection(self.server))
            return
        if path in ADMIN_PATHS and not self.authenticate_admin():
            return
        if path == "/admin":
            make_html_response(self, 200, admin_html(self.server))
            return
        if path == "/health":
            make_json_response(self, 200, {"status": "ok"})
            return
        if path == "/v1/gateway/me":
            if not self.authenticate():
                return
            make_json_response(self, 200, customer_self_view(self.server, self.customer))
            return
        if path == "/v1/gateway/integration-guide":
            if not self.authenticate():
                return
            make_json_response(self, 200, customer_integration_guide(self.server, self.customer))
            return
        if path == "/v1/gateway/sdk-starter":
            if not self.authenticate():
                return
            make_json_response(self, 200, customer_sdk_starter(self.server, self.customer))
            return
        if path == "/v1/gateway/status":
            make_json_response(self, 200, gateway_status(self.server))
            return
        if path == "/v1/gateway/alerts":
            make_json_response(self, 200, gateway_alerts(self.server))
            return
        if path == "/v1/gateway/access-matrix":
            make_json_response(self, 200, {"data": access_matrix(self.server)})
            return
        if path == "/v1/gateway/config-check":
            make_json_response(self, 200, gateway_config_check(self.server))
            return
        if path == "/v1/gateway/handoff-checklist":
            make_json_response(self, 200, handoff_checklist(self.server))
            return
        if path == "/v1/gateway/production-readiness":
            make_json_response(self, 200, production_readiness(self.server))
            return
        if path == "/v1/gateway/deployment-readiness":
            make_json_response(self, 200, deployment_readiness(self.server))
            return
        if path == "/v1/gateway/migration-plan":
            make_json_response(self, 200, migration_plan(self.server))
            return
        if path == "/v1/gateway/production-backlog":
            make_json_response(self, 200, production_backlog(self.server))
            return
        if path == "/v1/gateway/launch-plan":
            make_json_response(self, 200, launch_plan(self.server))
            return
        if path == "/v1/gateway/change-management":
            make_json_response(self, 200, change_management_plan(self.server))
            return
        if path == "/v1/gateway/data-governance":
            make_json_response(self, 200, data_governance_review(self.server))
            return
        if path == "/v1/gateway/security-review":
            make_json_response(self, 200, security_review(self.server))
            return
        if path == "/v1/gateway/operations-runbook":
            make_json_response(self, 200, operations_runbook(self.server))
            return
        if path == "/v1/gateway/incident-playbook":
            make_json_response(self, 200, incident_playbook(self.server))
            return
        if path == "/v1/gateway/support-policy":
            make_json_response(self, 200, support_policy(self.server))
            return
        if path == "/v1/gateway/pilot-checklist":
            make_json_response(self, 200, pilot_checklist(self.server))
            return
        if path == "/v1/gateway/pilot-scorecard":
            make_json_response(self, 200, pilot_scorecard(self.server))
            return
        if path == "/v1/gateway/discovery-checklist":
            make_json_response(self, 200, discovery_checklist(self.server))
            return
        if path == "/v1/gateway/proposal-summary":
            make_json_response(self, 200, proposal_summary(self.server))
            return
        if path == "/v1/gateway/onboarding-plan":
            make_json_response(self, 200, onboarding_plan(self.server))
            return
        if path == "/v1/gateway/executive-brief":
            make_json_response(self, 200, executive_brief(self.server))
            return
        if path == "/v1/gateway/roadmap":
            make_json_response(self, 200, gateway_roadmap(self.server))
            return
        if path == "/v1/gateway/decision-guide":
            make_json_response(self, 200, decision_guide(self.server))
            return
        if path == "/v1/gateway/faq":
            make_json_response(self, 200, gateway_faq(self.server))
            return
        if path == "/v1/gateway/demo-script":
            make_json_response(self, 200, demo_script(self.server))
            return
        if path == "/v1/gateway/requests":
            make_json_response(self, 200, {"data": db_tail(self.server.db_path, "requests", 100)})
            return
        if path == "/v1/gateway/audit-events":
            filters = {
                key: values[0]
                for key, values in query.items()
                if values and key in {"actor", "action", "target_type", "target_id", "status"}
            }
            limit = bounded_int(query.get("limit", [50])[0], 50)
            make_json_response(
                self,
                200,
                {
                    "filters": filters,
                    "limit": limit,
                    "data": audit_events(self.server.db_path, filters, limit),
                },
            )
            return
        if path == "/v1/gateway/usage":
            make_json_response(self, 200, {"data": db_tail(self.server.db_path, "usage_records", 100)})
            return
        if path == "/v1/gateway/customers":
            make_json_response(self, 200, {"data": db_tail(self.server.db_path, "customers", 100)})
            return
        if path == "/v1/gateway/providers":
            make_json_response(self, 200, {"data": provider_status(self.server)})
            return
        if path == "/v1/gateway/provider-health":
            make_json_response(self, 200, {"data": provider_health(self.server)})
            return
        if path == "/v1/gateway/provider-contracts":
            make_json_response(self, 200, provider_contracts(self.server))
            return
        if path == "/v1/gateway/policy-presets":
            make_json_response(self, 200, {"data": policy_presets_view()})
            return
        if path == "/v1/gateway/demo-bundle":
            make_json_response(self, 200, demo_bundle(self.server))
            return
        if path == "/v1/gateway/model-catalog":
            make_json_response(self, 200, {"data": model_catalog(self.server)})
            return
        if path == "/v1/gateway/evaluation-plan":
            make_json_response(self, 200, evaluation_plan(self.server))
            return
        if path == "/v1/gateway/customer-reports":
            make_json_response(self, 200, {"data": customer_reports(self.server)})
            return
        if path == "/v1/gateway/customer-success":
            make_json_response(self, 200, customer_success_summary(self.server))
            return
        if path == "/v1/gateway/commercial-policy":
            make_json_response(self, 200, commercial_policy(self.server))
            return
        if path == "/v1/gateway/procurement-pack":
            make_json_response(self, 200, procurement_pack(self.server))
            return
        if path == "/v1/gateway/business-case":
            make_json_response(self, 200, business_case(self.server))
            return
        if path == "/v1/gateway/implementation-plan":
            make_json_response(self, 200, implementation_plan(self.server))
            return
        if path == "/v1/gateway/alternatives-pack":
            make_json_response(self, 200, alternatives_pack(self.server))
            return
        if path == "/v1/gateway/request-activity":
            filters = {
                key: values[0]
                for key, values in query.items()
                if values and key in {"customer_id", "model", "provider", "code", "status"}
            }
            limit = bounded_int(query.get("limit", [50])[0], 50)
            make_json_response(
                self,
                200,
                {
                    "filters": filters,
                    "limit": limit,
                    "data": request_activity(self.server.db_path, filters, limit),
                },
            )
            return
        if path == "/v1/gateway/request-detail":
            detail = request_detail(self.server.db_path, query.get("request_id", [""])[0])
            if not detail:
                make_error(self, 404, "Request record was not found.", "request_not_found")
                return
            make_json_response(self, 200, detail)
            return
        if path == "/v1/gateway/customer-usage":
            make_json_response(self, 200, {"data": usage_grouped_by(self.server.db_path, "customer_id")})
            return
        if path == "/v1/gateway/invoice-preview":
            try:
                payload = invoice_preview(self.server, query.get("customer_id", [""])[0])
            except GatewayError as exc:
                make_error(self, exc.status, exc.message, exc.code, exc.details)
                return
            if query.get("format", ["json"])[0] == "csv":
                filename = "aismallrouter-invoice-preview.csv"
                if query.get("customer_id", [""])[0]:
                    filename = f"aismallrouter-{query.get('customer_id', [''])[0]}-invoice-preview.csv"
                make_csv_response(self, 200, filename, invoice_preview_csv(payload))
                return
            make_json_response(self, 200, payload)
            return
        if path == "/v1/gateway/model-usage":
            make_json_response(self, 200, {"data": usage_grouped_by(self.server.db_path, "model")})
            return
        if path == "/v1/gateway/request-summary":
            make_json_response(
                self,
                200,
                {
                    "by_customer": request_grouped_by(self.server.db_path, "customer_id"),
                    "by_model": request_grouped_by(self.server.db_path, "model"),
                    "by_provider": request_grouped_by(self.server.db_path, "provider"),
                    "by_code": request_grouped_by(self.server.db_path, "code"),
                },
            )
            return
        if path == "/v1/models":
            if not self.authenticate():
                return
            if not self.check_usage_limit():
                return
            make_json_response(self, 200, openai_style_model_list(self.server.models))
            return
        make_error(self, 404, "Route not found.", "route_not_found")

    def do_POST(self):
        path = self.route_path()
        if path == "/v1/gateway/route-preview":
            if not self.authenticate_admin():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                make_json_response(self, 200, route_preview(self.server, payload))
            except GatewayError as exc:
                make_error(self, exc.status, exc.message, exc.code, exc.details)
            return
        if path == "/v1/gateway/cost-estimate":
            if not self.authenticate_admin():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                make_json_response(self, 200, cost_estimate(self.server, payload))
            except GatewayError as exc:
                make_error(self, exc.status, exc.message, exc.code, exc.details)
            return
        if path == "/v1/gateway/key-issue-preview":
            if not self.authenticate_admin():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                make_json_response(self, 200, key_issue_preview(self.server, payload))
            except GatewayError as exc:
                make_error(self, exc.status, exc.message, exc.code, exc.details)
            return
        if path == "/v1/gateway/customers":
            if not self.authenticate_admin():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                make_json_response(self, 201, customer_create(self.server, payload))
            except GatewayError as exc:
                make_error(self, exc.status, exc.message, exc.code, exc.details)
            return
        if path == "/v1/gateway/customers/disable":
            if not self.authenticate_admin():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                make_json_response(self, 200, customer_disable(self.server, payload))
            except GatewayError as exc:
                make_error(self, exc.status, exc.message, exc.code, exc.details)
            return
        if path == "/v1/gateway/customers/rotate-key":
            if not self.authenticate_admin():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                make_json_response(self, 200, customer_rotate_key(self.server, payload))
            except GatewayError as exc:
                make_error(self, exc.status, exc.message, exc.code, exc.details)
            return
        if path == "/v1/gateway/providers":
            if not self.authenticate_admin():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                make_json_response(self, 201, provider_create(self.server, payload))
            except GatewayError as exc:
                make_error(self, exc.status, exc.message, exc.code, exc.details)
            return
        if path == "/v1/gateway/providers/update":
            if not self.authenticate_admin():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                make_json_response(self, 200, provider_update(self.server, payload))
            except GatewayError as exc:
                make_error(self, exc.status, exc.message, exc.code, exc.details)
            return
        if path == "/v1/gateway/providers/disable":
            if not self.authenticate_admin():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                make_json_response(self, 200, provider_disable(self.server, payload))
            except GatewayError as exc:
                make_error(self, exc.status, exc.message, exc.code, exc.details)
            return
        if path == "/v1/gateway/model-routes":
            if not self.authenticate_admin():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                make_json_response(self, 201, model_route_create(self.server, payload))
            except GatewayError as exc:
                make_error(self, exc.status, exc.message, exc.code, exc.details)
            return
        if path == "/v1/gateway/model-routes/update":
            if not self.authenticate_admin():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                make_json_response(self, 200, model_route_update(self.server, payload))
            except GatewayError as exc:
                make_error(self, exc.status, exc.message, exc.code, exc.details)
            return
        if path == "/v1/gateway/model-routes/disable":
            if not self.authenticate_admin():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                make_json_response(self, 200, model_route_disable(self.server, payload))
            except GatewayError as exc:
                make_error(self, exc.status, exc.message, exc.code, exc.details)
            return
        if path == "/v1/gateway/safety-preview":
            if not self.authenticate_admin():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            make_json_response(self, 200, safety_preview(payload))
            return
        if path != "/v1/chat/completions":
            make_error(self, 404, "Route not found.", "route_not_found")
            return
        if not self.authenticate():
            return
        if not self.check_usage_limit():
            return
        started_at = time.perf_counter()
        payload = self.read_json_body()
        if payload is None:
            return
        try:
            self.handle_chat_completions(payload, started_at)
        except GatewayError as exc:
            self.write_request_log(started_at, payload.get("model"), None, None, exc.status, exc.code)
            make_error(self, exc.status, exc.message, exc.code, exc.details)
        except ProviderError as exc:
            self.write_request_log(started_at, payload.get("model"), None, None, exc.status, "provider_error")
            make_error(self, exc.status, exc.message, f"upstream_{exc.status}", exc.body[:1000] if exc.body else None)

    def authenticate(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            make_error(self, 401, "Missing Authorization Bearer token.", "missing_auth")
            return False
        api_key = header.removeprefix("Bearer ").strip()
        customer = self.server.customers_by_key.get(api_key)
        if not customer:
            make_error(self, 401, "Invalid gateway API key.", "invalid_api_key")
            return False
        self.customer_api_key = api_key
        self.customer = customer
        return True

    def authenticate_admin(self):
        if not self.server.admin_api_key:
            return True
        header = self.headers.get("Authorization", "")
        token = ""
        if header.startswith("Bearer "):
            token = header.removeprefix("Bearer ").strip()
        query = parse_qs(self.parsed_path().query)
        query_token = query.get("admin_key", [""])[0]
        if token == self.server.admin_api_key or query_token == self.server.admin_api_key:
            return True
        make_error(self, 401, "Missing or invalid admin API key.", "invalid_admin_key")
        return False

    def check_usage_limit(self):
        customer = getattr(self, "customer", None)
        if not customer:
            return False
        limit = int(customer.get("request_limit", self.server.default_request_limit))
        if limit <= 0:
            return True
        window_seconds = int(customer.get("limit_window_seconds", self.server.default_limit_window_seconds))
        hashed_key = key_hash(self.customer_api_key)
        current_time = time.time()
        usage_state = self.server.usage_by_key.get(hashed_key)
        if not usage_state or current_time >= usage_state["reset_at"]:
            usage_state = {"count": 0, "reset_at": current_time + window_seconds}
        if usage_state["count"] >= limit:
            retry_after = max(1, int(usage_state["reset_at"] - current_time))
            body = json.dumps(
                {
                    "error": {
                        "message": "The gateway usage limit was reached for this API key.",
                        "type": "gateway_error",
                        "code": "usage_limit_exceeded",
                    }
                }
            ).encode("utf-8")
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", str(retry_after))
            self.end_headers()
            self.wfile.write(body)
            self.server.usage_by_key[hashed_key] = usage_state
            return False
        usage_state["count"] += 1
        self.server.usage_by_key[hashed_key] = usage_state
        return True

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            return json.loads(body)
        except ValueError:
            make_error(self, 400, "Invalid Content-Length.", "invalid_content_length")
            return None
        except json.JSONDecodeError:
            make_error(self, 400, "Request body must be valid JSON.", "invalid_json")
            return None

    def ensure_model_access(self, public_model):
        if not customer_model_allowed(self.customer, public_model):
            raise GatewayError(f"Customer is not allowed to use model: {public_model}.", "model_not_allowed", 403)

    def ensure_budget_available(self):
        budget = customer_budget_status(self.server.db_path, self.customer)
        token_budget = budget.get("token_budget")
        cost_budget = budget.get("cost_budget")
        usage = budget.get("usage", {})
        if token_budget is not None and int(usage.get("total_tokens") or 0) >= int(token_budget):
            raise GatewayError(
                "The customer token budget has been reached.",
                "token_budget_exceeded",
                402,
                budget,
            )
        if cost_budget is not None and float(usage.get("estimated_cost") or 0) >= float(cost_budget):
            raise GatewayError(
                "The customer cost budget has been reached.",
                "cost_budget_exceeded",
                402,
                budget,
            )

    def candidate_models(self, public_model, payload):
        return build_candidate_models(self.server, self.customer, public_model, payload)

    def route_trace(self, public_model, resolved_model, routing_policy):
        fallback_text = "disabled"
        if routing_policy.get("fallback_enabled"):
            fallback_text = ", ".join(routing_policy.get("candidates", [])[1:]) or "none"
        provider_text = ", ".join(routing_policy.get("allowed_providers") or ["any"])
        capability_text = ", ".join(routing_policy.get("required_capabilities") or ["chat"])
        route_strategy = routing_policy.get("route_strategy") or "registry"
        policy_text = (routing_policy.get("gateway_policy") or {}).get("name") or "none"
        return [
            "Customer sends one OpenAI-compatible request",
            f"Gateway reads model = {public_model}",
            f"Model registry maps {public_model} to {resolved_model}",
            f"Decision summary = {public_model} -> {resolved_model}",
            f"Routing policy source = {routing_policy.get('source')}, policy = {policy_text}, strategy = {route_strategy}, fallback = {fallback_text}",
            f"Allowed providers = {provider_text}",
            f"Required capabilities = {capability_text}",
            "Provider adapter prepares the upstream request",
        ]

    def handle_chat_completions(self, payload, started_at):
        public_model = payload.get("model")
        if not public_model:
            raise GatewayError("Missing required field: model.", "missing_model", 400)
        self.ensure_model_access(public_model)
        self.ensure_budget_available()
        safety = safety_preview(payload)
        if payload.get("gateway_block_sensitive") and safety["summary"]["total"]:
            raise GatewayError(
                "The request was blocked by the local gateway safety preview.",
                "safety_blocked",
                400,
                safety,
            )
        provider_payload_source = payload
        redaction_applied = False
        if payload.get("gateway_redact_sensitive"):
            provider_payload_source, redactions = redact_sensitive_payload(payload)
            redaction_applied = bool(redactions)
            safety["redaction_applied"] = redaction_applied
        stream = bool(payload.get("stream"))
        candidates, routing_policy = self.candidate_models(public_model, payload)
        errors = []
        for index, candidate_name in enumerate(candidates):
            model_config = self.server.models[candidate_name]
            if should_force_failover(payload, index):
                errors.append({"model": candidate_name, "error": "forced_failover"})
                continue
            payload_for_provider = dict(provider_payload_source)
            payload_for_provider["model"] = public_model
            route_trace = self.route_trace(public_model, model_config["upstream_model"], routing_policy)
            try:
                adapter = adapter_for(model_config, self.server.mock_mode, self.customer)
                if stream:
                    self.stream_response(adapter, model_config, payload_for_provider, route_trace, started_at)
                    return
                response = adapter.complete(model_config, payload_for_provider, self.server.timeout, route_trace)
                response["model"] = public_model
                response.setdefault("gateway", {})
                response["gateway"].update(
                    {
                        "public_model": public_model,
                        "resolved_model": model_config["upstream_model"],
                        "provider": model_config["provider"],
                        "fallback_attempts": errors,
                        "routing_policy": routing_policy,
                        "route_decision": route_decision(
                            public_model,
                            model_config,
                            routing_policy,
                            self.customer.get("id"),
                            errors,
                        ),
                    }
                )
                if payload.get("gateway_safety_check") or payload.get("gateway_redact_sensitive"):
                    response["gateway"]["safety_preview"] = safety
                    response["gateway"]["redaction_applied"] = redaction_applied
                self.write_usage(response, public_model, model_config)
                response["gateway"]["customer_budget"] = customer_budget_status(self.server.db_path, self.customer)
                request_record = self.write_request_log(started_at, public_model, model_config["upstream_model"], model_config["provider"], 200, "ok")
                response["gateway"]["request_id"] = request_record["request_id"]
                make_json_response(self, 200, response)
                return
            except (ProviderError, GatewayError) as exc:
                errors.append({"model": candidate_name, "error": getattr(exc, "code", getattr(exc, "message", str(exc)))})
                if index == len(candidates) - 1:
                    raise
        raise GatewayError("No route candidate could serve the request.", "route_failed", 502, errors)

    def stream_response(self, adapter, model_config, payload, route_trace, started_at):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        final_usage = None
        for event in adapter.stream(model_config, payload, self.server.timeout, route_trace):
            if event.get("usage"):
                final_usage = event["usage"]
            self.wfile.write(sse_chunk(event))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        if final_usage:
            self.write_usage(
                {"usage": final_usage},
                payload["model"],
                model_config,
            )
        self.write_request_log(started_at, payload["model"], model_config["upstream_model"], model_config["provider"], 200, "ok_stream")

    def write_request_log(self, started_at, public_model, resolved_model, provider, status, code):
        record = {
            "created": now_unix(),
            "request_id": uuid.uuid4().hex[:12],
            "customer_id": getattr(self, "customer", {}).get("id"),
            "api_key_hash": key_hash(getattr(self, "customer_api_key", "")) if getattr(self, "customer_api_key", "") else None,
            "model": public_model,
            "resolved_model": resolved_model,
            "provider": provider,
            "status": status,
            "code": code,
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "mock_mode": self.server.mock_mode,
        }
        append_jsonl(self.server.request_log_path, record)
        insert_request_db(self.server.db_path, record)
        if not self.server.quiet:
            print(json.dumps(record, ensure_ascii=False), flush=True)
        return record

    def write_usage(self, response, public_model, model_config):
        usage = response.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        pricing = model_config.get("pricing", {})
        estimated_cost = (
            prompt_tokens / 1000 * float(pricing.get("prompt_per_1k", 0))
            + completion_tokens / 1000 * float(pricing.get("completion_per_1k", 0))
        )
        record = {
            "created": now_unix(),
            "customer_id": getattr(self, "customer", {}).get("id"),
            "model": public_model,
            "resolved_model": model_config.get("upstream_model"),
            "provider": model_config.get("provider"),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "estimated_cost": round(estimated_cost, 8),
            "mock_mode": self.server.mock_mode,
        }
        append_jsonl(self.server.usage_log_path, record)
        insert_usage_db(self.server.db_path, record)
        return record


def parse_args():
    parser = argparse.ArgumentParser(description="AISmallRouter: OpenAI-compatible model gateway prototype.")
    parser.add_argument("--host", default=os.getenv("GATEWAY_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("GATEWAY_PORT", DEFAULT_PORT)))
    parser.add_argument("--registry", default=os.getenv("MODEL_REGISTRY", DEFAULT_REGISTRY_PATH))
    parser.add_argument("--customers", default=os.getenv("CUSTOMER_KEYS", DEFAULT_CUSTOMERS_PATH))
    parser.add_argument("--log-dir", default=os.getenv("GATEWAY_LOG_DIR", DEFAULT_LOG_DIR))
    parser.add_argument("--data-dir", default=os.getenv("GATEWAY_DATA_DIR", DEFAULT_DATA_DIR))
    parser.add_argument("--db-path", default=os.getenv("GATEWAY_DB_PATH"))
    parser.add_argument("--admin-key", default=os.getenv("GATEWAY_ADMIN_API_KEY", DEFAULT_ADMIN_API_KEY))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("UPSTREAM_TIMEOUT", "60")))
    parser.add_argument("--request-limit", type=int, default=int(os.getenv("GATEWAY_REQUEST_LIMIT", DEFAULT_REQUEST_LIMIT)))
    parser.add_argument("--limit-window-seconds", type=int, default=int(os.getenv("GATEWAY_LIMIT_WINDOW_SECONDS", DEFAULT_LIMIT_WINDOW_SECONDS)))
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--mock",
        action="store_true",
        default=os.getenv("GATEWAY_MOCK_MODE", "").lower() in {"1", "true", "yes"},
        help="Return mock responses without calling a paid provider.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    registry = load_registry(args.registry)
    customers_by_key = load_customers(args.customers)
    os.makedirs(args.log_dir, exist_ok=True)
    os.makedirs(args.data_dir, exist_ok=True)
    db_path = args.db_path or os.path.join(args.data_dir, "aismallrouter.db")
    init_db(db_path)
    sync_customers_to_db(db_path, customers_by_key)
    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    server.providers = registry["providers"]
    server.models = registry["models"]
    server.customers_by_key = customers_by_key
    server.mock_mode = args.mock
    server.timeout = args.timeout
    server.admin_api_key = args.admin_key
    server.quiet = args.quiet
    server.default_request_limit = args.request_limit
    server.default_limit_window_seconds = args.limit_window_seconds
    server.customers_path = args.customers
    server.registry_path = args.registry
    server.usage_by_key = {}
    server.request_log_path = os.path.join(args.log_dir, "requests.jsonl")
    server.usage_log_path = os.path.join(args.log_dir, "usage.jsonl")
    server.audit_log_path = os.path.join(args.log_dir, "audit_events.jsonl")
    server.db_path = db_path
    mode = "mock" if args.mock else "live"
    print(f"AISmallRouter listening on http://{args.host}:{args.port}")
    print(f"Mode: {mode}")
    print(f"Models: {', '.join(sorted(server.models.keys()))}")
    print(f"Customers: {', '.join(customer['id'] for customer in customers_by_key.values())}")
    print(f"Logs: {args.log_dir}")
    print(f"Database: {db_path}")
    print("Admin: protected" if args.admin_key else "Admin: unprotected")
    config = gateway_config_check(server)
    print(
        "Config check: "
        f"{config['status']} "
        f"({config['summary']['critical']} critical, "
        f"{config['summary']['warning']} warning, "
        f"{config['summary']['info']} info)"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
