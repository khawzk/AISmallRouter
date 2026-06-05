#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
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
    "/v1/gateway/requests",
    "/v1/gateway/usage",
    "/v1/gateway/customers",
    "/v1/gateway/providers",
    "/v1/gateway/provider-health",
    "/v1/gateway/customer-usage",
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


def db_tail(path, table, limit=50):
    allowed = {"requests", "usage_records", "customers"}
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


def db_summary(path):
    if not os.path.exists(path):
        return {}
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        request_count = conn.execute("select count(*) as value from requests").fetchone()["value"]
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


def add_config_check(checks, severity, code, message, details=None):
    check = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if details is not None:
        check["details"] = details
    checks.append(check)


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


def gateway_status(server):
    return {
        "status": "ok",
        "mode": "mock" if server.mock_mode else "live",
        "database": server.db_path,
        "summary": db_summary(server.db_path),
        "config_check": gateway_config_check(server),
        "provider_summary": provider_status(server),
        "provider_health": provider_health(server),
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
    <p><a href="/">Dashboard</a> | <a href="/v1/gateway/status">Status JSON</a> | <a href="/v1/gateway/config-check">Config Check JSON</a> | <a href="/v1/gateway/provider-health">Provider Health JSON</a> | <a href="/v1/gateway/providers">Providers JSON</a> | <a href="/v1/gateway/customer-usage">Customer Usage JSON</a> | <a href="/v1/gateway/model-usage">Model Usage JSON</a> | <a href="/v1/gateway/requests">Requests JSON</a> | <a href="/v1/gateway/usage">Usage JSON</a> | <a href="/v1/gateway/customers">Customers JSON</a></p>
    <h2>Summary</h2>
    <table>
      <tbody>
        <tr><th>Total requests</th><td>{summary.get('request_count', 0)}</td></tr>
        <tr><th>Total tokens</th><td>{summary.get('usage', {}).get('total_tokens', 0)}</td></tr>
        <tr><th>Estimated cost</th><td>{summary.get('usage', {}).get('estimated_cost', 0)}</td></tr>
        <tr><th>Config status</th><td>{config_check.get('status')}</td></tr>
        <tr><th>Database</th><td>{server.db_path}</td></tr>
      </tbody>
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
    <h2>Usage By Customer</h2>
    <table>
      <thead><tr><th>Customer</th><th>Usage records</th><th>Total tokens</th><th>Est. cost</th></tr></thead>
      <tbody>{customer_usage_rows}</tbody>
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
        self.send_response(200 if path == "/health" or path in ADMIN_PATHS else 404)
        self.end_headers()

    def do_GET(self):
        path = self.route_path()
        if path in {"/", "/dashboard"}:
            make_html_response(self, 200, dashboard_html(self.server))
            return
        if path in ADMIN_PATHS and not self.authenticate_admin():
            return
        if path == "/admin":
            make_html_response(self, 200, admin_html(self.server))
            return
        if path == "/health":
            make_json_response(self, 200, {"status": "ok"})
            return
        if path == "/v1/gateway/status":
            make_json_response(self, 200, gateway_status(self.server))
            return
        if path == "/v1/gateway/config-check":
            make_json_response(self, 200, gateway_config_check(self.server))
            return
        if path == "/v1/gateway/requests":
            make_json_response(self, 200, {"data": db_tail(self.server.db_path, "requests", 100)})
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
        if path == "/v1/gateway/customer-usage":
            make_json_response(self, 200, {"data": usage_grouped_by(self.server.db_path, "customer_id")})
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
        if self.route_path() != "/v1/chat/completions":
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
        allowed = self.customer.get("allowed_models", ["*"])
        if "*" not in allowed and public_model not in allowed:
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
        model = self.server.models.get(public_model)
        if not model:
            raise GatewayError(f"Unknown model: {public_model}.", "unknown_model", 404)
        routing_policy = {
            "source": "model_registry",
            "fallback_enabled": not bool(payload.get("gateway_disable_fallback")),
            "requested_fallback_models": None,
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

        candidates = [public_model]
        for name in fallback_models:
            if name == public_model or name in candidates:
                continue
            if name not in self.server.models:
                raise GatewayError(f"Unknown fallback model: {name}.", "unknown_fallback_model", 404)
            if requested_fallbacks:
                self.ensure_model_access(name)
            candidates.append(name)
        routing_policy["candidates"] = candidates
        return candidates, routing_policy

    def route_trace(self, public_model, resolved_model, routing_policy):
        fallback_text = "disabled"
        if routing_policy.get("fallback_enabled"):
            fallback_text = ", ".join(routing_policy.get("candidates", [])[1:]) or "none"
        return [
            "Customer sends one OpenAI-compatible request",
            f"Gateway reads model = {public_model}",
            f"Model registry maps {public_model} to {resolved_model}",
            f"Routing policy source = {routing_policy.get('source')}, fallback = {fallback_text}",
            "Provider adapter prepares the upstream request",
        ]

    def handle_chat_completions(self, payload, started_at):
        public_model = payload.get("model")
        if not public_model:
            raise GatewayError("Missing required field: model.", "missing_model", 400)
        self.ensure_model_access(public_model)
        self.ensure_budget_available()
        stream = bool(payload.get("stream"))
        candidates, routing_policy = self.candidate_models(public_model, payload)
        errors = []
        for index, candidate_name in enumerate(candidates):
            model_config = self.server.models[candidate_name]
            if should_force_failover(payload, index):
                errors.append({"model": candidate_name, "error": "forced_failover"})
                continue
            payload_for_provider = dict(payload)
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
                    }
                )
                self.write_usage(response, public_model, model_config)
                response["gateway"]["customer_budget"] = customer_budget_status(self.server.db_path, self.customer)
                self.write_request_log(started_at, public_model, model_config["upstream_model"], model_config["provider"], 200, "ok")
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
    server.usage_by_key = {}
    server.request_log_path = os.path.join(args.log_dir, "requests.jsonl")
    server.usage_log_path = os.path.join(args.log_dir, "usage.jsonl")
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
