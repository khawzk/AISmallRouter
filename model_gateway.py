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
ADMIN_PATHS = {
    "/admin",
    "/v1/gateway/status",
    "/v1/gateway/audit-events",
    "/v1/gateway/requests",
    "/v1/gateway/usage",
    "/v1/gateway/customers",
    "/v1/gateway/providers",
    "/v1/gateway/provider-health",
    "/v1/gateway/customer-reports",
    "/v1/gateway/policy-presets",
    "/v1/gateway/demo-bundle",
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
        "/v1/gateway/model-catalog": "Model catalog and routing plan",
        "/v1/gateway/customer-reports": "Customer usage and budget reports",
        "/v1/gateway/request-activity": "Filtered request activity",
        "/v1/gateway/request-detail": "One request detail by request_id",
        "/v1/gateway/customer-usage": "Usage grouped by customer",
        "/v1/gateway/invoice-preview": "Invoice preview JSON or CSV",
        "/v1/gateway/model-usage": "Usage grouped by model",
        "/v1/gateway/request-summary": "Request summary by dimensions",
        "/v1/gateway/demo-bundle": "Customer demo bundle manifest",
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
        request_item("Policy Presets", "GET", "/v1/gateway/policy-presets", "admin_api_key"),
        request_item("Provider Health", "GET", "/v1/gateway/provider-health", "admin_api_key"),
        request_item("Model Catalog", "GET", "/v1/gateway/model-catalog", "admin_api_key"),
        request_item("Audit Events", "GET", "/v1/gateway/audit-events", "admin_api_key"),
        request_item("Customer Reports", "GET", "/v1/gateway/customer-reports", "admin_api_key"),
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
                "name": "OpenAPI contract",
                "url": f"{base_url}/openapi.json",
                "audience": "customer technical",
                "purpose": "Import or inspect the API contract.",
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
                "show": "/",
                "talk_track": "Customer apps call one OpenAI-compatible API while the gateway manages providers and routes.",
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
                "show": "/v1/gateway/route-preview",
                "talk_track": "Route preview explains provider, fallback, capability, policy, and cost-aware decisions before spending credits.",
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
                "show": "/v1/gateway/status",
                "talk_track": "Admin views show provider health, customer usage, alerts, audit events, and invoice preview.",
            },
            {
                "step": 6,
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
        ],
        "production_notes": [
            "Replace demo keys before production.",
            "Move customer, provider, and route config from local JSON to a database.",
            "Store provider secrets in a secret manager.",
            "Add approval workflow, audit retention, billing rules, and deployment controls.",
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
        "provider_summary": provider_status(server),
        "provider_health": provider_health(server),
        "model_catalog": model_catalog(server),
        "access_matrix": access_matrix(server),
        "customer_reports": customer_reports(server),
        "invoice_preview": invoice_preview(server),
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
        if path == "/v1/gateway/policy-presets":
            make_json_response(self, 200, {"data": policy_presets_view()})
            return
        if path == "/v1/gateway/demo-bundle":
            make_json_response(self, 200, demo_bundle(self.server))
            return
        if path == "/v1/gateway/model-catalog":
            make_json_response(self, 200, {"data": model_catalog(self.server)})
            return
        if path == "/v1/gateway/customer-reports":
            make_json_response(self, 200, {"data": customer_reports(self.server)})
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
