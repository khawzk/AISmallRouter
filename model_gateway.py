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


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_REGISTRY_PATH = "model_registry.json"
DEFAULT_CUSTOMERS_PATH = "customer_keys.json"
DEFAULT_DASHBOARD_PATH = "dashboard.html"
DEFAULT_LOG_DIR = "logs"
DEFAULT_DATA_DIR = "data"
DEFAULT_GATEWAY_API_KEY = "dev-gateway-key"
DEFAULT_REQUEST_LIMIT = 60
DEFAULT_LIMIT_WINDOW_SECONDS = 60


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
        "request_limit": customer.get("request_limit"),
        "limit_window_seconds": customer.get("limit_window_seconds"),
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
                request_limit integer,
                limit_window_seconds integer,
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
        if "byok_providers" not in customer_columns:
            conn.execute("alter table customers add column byok_providers text")
        conn.commit()


def sync_customers_to_db(path, customers_by_key):
    with sqlite3.connect(path) as conn:
        for api_key, customer in customers_by_key.items():
            conn.execute(
                """
                insert into customers (
                    id, name, api_key_hash, request_limit,
                    limit_window_seconds, allowed_models, byok_providers, enabled
                )
                values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    name=excluded.name,
                    api_key_hash=excluded.api_key_hash,
                    request_limit=excluded.request_limit,
                    limit_window_seconds=excluded.limit_window_seconds,
                    allowed_models=excluded.allowed_models,
                    byok_providers=excluded.byok_providers,
                    enabled=excluded.enabled
                """,
                (
                    customer["id"],
                    customer.get("name", customer["id"]),
                    key_hash(api_key),
                    int(customer.get("request_limit", DEFAULT_REQUEST_LIMIT)),
                    int(customer.get("limit_window_seconds", DEFAULT_LIMIT_WINDOW_SECONDS)),
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
        "request_limit": server.default_request_limit,
        "limit_window_seconds": server.default_limit_window_seconds,
        "customers": [
            public_customer_view(customer)
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
            return file.read().replace("__GATEWAY_STATE__", state)
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
    <p><a href="/">Dashboard</a> | <a href="/v1/gateway/status">Status JSON</a> | <a href="/v1/gateway/requests">Requests JSON</a> | <a href="/v1/gateway/usage">Usage JSON</a> | <a href="/v1/gateway/customers">Customers JSON</a></p>
    <h2>Summary</h2>
    <table>
      <tbody>
        <tr><th>Total requests</th><td>{summary.get('request_count', 0)}</td></tr>
        <tr><th>Total tokens</th><td>{summary.get('usage', {}).get('total_tokens', 0)}</td></tr>
        <tr><th>Estimated cost</th><td>{summary.get('usage', {}).get('estimated_cost', 0)}</td></tr>
        <tr><th>Database</th><td>{server.db_path}</td></tr>
      </tbody>
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
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
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
                "route_trace": route_trace + ["Mock mode returns a simulated provider response"],
            },
        }

    def mock_stream(self, model_config, request_payload, route_trace):
        completion = self.mock_completion(model_config, request_payload, route_trace)
        content = completion["choices"][0]["message"]["content"]
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
                return json.loads(response.read().decode("utf-8"))
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
                return self.openai_response_from_anthropic(upstream, request_payload["model"])
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
    server_version = "AISmallRouter/0.3"

    def log_message(self, format_text, *args):
        if self.server.quiet:
            return
        super().log_message(format_text, *args)

    def do_HEAD(self):
        if self.path in {"/", "/dashboard", "/admin"}:
            html = admin_html(self.server) if self.path == "/admin" else dashboard_html(self.server)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode("utf-8"))))
            self.end_headers()
            return
        self.send_response(200 if self.path in {"/health", "/v1/gateway/status"} else 404)
        self.end_headers()

    def do_GET(self):
        if self.path in {"/", "/dashboard"}:
            make_html_response(self, 200, dashboard_html(self.server))
            return
        if self.path == "/admin":
            make_html_response(self, 200, admin_html(self.server))
            return
        if self.path == "/health":
            make_json_response(self, 200, {"status": "ok"})
            return
        if self.path == "/v1/gateway/status":
            make_json_response(self, 200, gateway_status(self.server))
            return
        if self.path == "/v1/gateway/requests":
            make_json_response(self, 200, {"data": db_tail(self.server.db_path, "requests", 100)})
            return
        if self.path == "/v1/gateway/usage":
            make_json_response(self, 200, {"data": db_tail(self.server.db_path, "usage_records", 100)})
            return
        if self.path == "/v1/gateway/customers":
            make_json_response(self, 200, {"data": db_tail(self.server.db_path, "customers", 100)})
            return
        if self.path == "/v1/models":
            if not self.authenticate():
                return
            if not self.check_usage_limit():
                return
            make_json_response(self, 200, openai_style_model_list(self.server.models))
            return
        make_error(self, 404, "Route not found.", "route_not_found")

    def do_POST(self):
        if self.path != "/v1/chat/completions":
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

    def candidate_models(self, public_model):
        model = self.server.models.get(public_model)
        if not model:
            raise GatewayError(f"Unknown model: {public_model}.", "unknown_model", 404)
        candidates = [public_model] + model.get("fallback_models", [])
        return [name for name in candidates if name in self.server.models]

    def route_trace(self, public_model, resolved_model):
        return [
            "Customer sends one OpenAI-compatible request",
            f"Gateway reads model = {public_model}",
            f"Model registry maps {public_model} to {resolved_model}",
            "Provider adapter prepares the upstream request",
        ]

    def handle_chat_completions(self, payload, started_at):
        public_model = payload.get("model")
        if not public_model:
            raise GatewayError("Missing required field: model.", "missing_model", 400)
        self.ensure_model_access(public_model)
        stream = bool(payload.get("stream"))
        candidates = self.candidate_models(public_model)
        errors = []
        for index, candidate_name in enumerate(candidates):
            model_config = self.server.models[candidate_name]
            if should_force_failover(payload, index):
                errors.append({"model": candidate_name, "error": "forced_failover"})
                continue
            payload_for_provider = dict(payload)
            payload_for_provider["model"] = public_model
            route_trace = self.route_trace(public_model, model_config["upstream_model"])
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
                    }
                )
                self.write_usage(response, public_model, model_config)
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


def parse_args():
    parser = argparse.ArgumentParser(description="AISmallRouter: OpenAI-compatible model gateway prototype.")
    parser.add_argument("--host", default=os.getenv("GATEWAY_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("GATEWAY_PORT", DEFAULT_PORT)))
    parser.add_argument("--registry", default=os.getenv("MODEL_REGISTRY", DEFAULT_REGISTRY_PATH))
    parser.add_argument("--customers", default=os.getenv("CUSTOMER_KEYS", DEFAULT_CUSTOMERS_PATH))
    parser.add_argument("--log-dir", default=os.getenv("GATEWAY_LOG_DIR", DEFAULT_LOG_DIR))
    parser.add_argument("--data-dir", default=os.getenv("GATEWAY_DATA_DIR", DEFAULT_DATA_DIR))
    parser.add_argument("--db-path", default=os.getenv("GATEWAY_DB_PATH"))
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
    server.serve_forever()


if __name__ == "__main__":
    main()
