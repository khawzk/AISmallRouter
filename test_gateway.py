#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from model_gateway import normalize_tools_for_anthropic


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request_json(base_url, method="GET", path="/", api_key=None, payload=None, headers=None):
    body = None
    headers = dict(headers or {})
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base_url + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            text = response.read().decode("utf-8")
            return response.status, json.loads(text)
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8")
        finally:
            exc.close()
        return exc.code, json.loads(text)


def request_text(base_url, method="GET", path="/", api_key=None, payload=None, headers=None):
    body = None
    headers = dict(headers or {})
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base_url + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8")
        finally:
            exc.close()
        return exc.code, text


class GatewayServer:
    def __init__(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.data_dir = os.path.join(self.temp_dir.name, "data")
        self.log_dir = os.path.join(self.temp_dir.name, "logs")
        self.process = None

    def __enter__(self):
        command = [
            sys.executable,
            os.path.join(ROOT_DIR, "model_gateway.py"),
            "--mock",
            "--quiet",
            "--port",
            str(self.port),
            "--data-dir",
            self.data_dir,
            "--log-dir",
            self.log_dir,
        ]
        self.process = subprocess.Popen(
            command,
            cwd=ROOT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.wait_until_ready()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.process:
            if self.process.stdout:
                self.process.stdout.close()
            if self.process.stderr:
                self.process.stderr.close()
        self.temp_dir.cleanup()

    def wait_until_ready(self):
        deadline = time.time() + 10
        last_error = None
        while time.time() < deadline:
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                raise RuntimeError(f"Gateway exited early.\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
            try:
                status, payload = request_json(self.base_url, path="/health")
                if status == 200 and payload.get("status") == "ok":
                    return
            except Exception as exc:
                last_error = exc
                time.sleep(0.1)
        raise RuntimeError(f"Gateway did not become ready: {last_error}")


class GatewayPrototypeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = GatewayServer().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.gateway.__exit__(None, None, None)

    @property
    def base_url(self):
        return self.gateway.base_url

    def test_models_requires_valid_gateway_key(self):
        status, payload = request_json(self.base_url, path="/v1/models", api_key="wrong-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_api_key")

        status, payload = request_json(self.base_url, path="/v1/models", api_key="dev-gateway-key")
        self.assertEqual(status, 200)
        model_ids = {model["id"] for model in payload["data"]}
        self.assertIn("smart-fast", model_ids)

    def test_chat_completion_records_route_usage_and_budget(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "Explain the gateway briefly."}],
                "stream": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "smart-fast")
        self.assertEqual(payload["gateway"]["resolved_model"], "qwen-plus")
        self.assertIn("customer_budget", payload["gateway"])
        self.assertEqual(payload["gateway"]["route_decision"]["summary"], "smart-fast -> qwen-plus via dashscope")
        self.assertIn("Required capabilities", " ".join(payload["gateway"]["route_decision"]["reasons"]))
        request_id = payload["gateway"]["request_id"]
        self.assertTrue(request_id)

        status, detail = request_json(self.base_url, path=f"/v1/gateway/request-detail?request_id={request_id}")
        self.assertEqual(status, 401)

        status, detail = request_json(
            self.base_url,
            path=f"/v1/gateway/request-detail?request_id={request_id}",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["request_id"], request_id)
        self.assertEqual(detail["customer_id"], "dev")
        self.assertEqual(detail["model"], "smart-fast")
        self.assertEqual(detail["resolved_model"], "qwen-plus")
        self.assertEqual(detail["outcome"], "success")
        self.assertIn("summary", detail)
        self.assertIn("usage", detail)

        status, detail = request_json(
            self.base_url,
            path="/v1/gateway/request-detail?request_id=missing",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 404)
        self.assertEqual(detail["error"]["code"], "request_not_found")

        status, usage = request_json(self.base_url, path="/v1/gateway/usage", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(usage["data"]), 1)

        status, requests = request_json(self.base_url, path="/v1/gateway/requests", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertTrue(any(row["code"] == "ok" for row in requests["data"]))

    def test_forced_fallback_routes_to_fallback_model(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "gateway_force_failover": True,
                "messages": [{"role": "user", "content": "Show fallback."}],
                "stream": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["gateway"]["resolved_model"], "qwen-turbo")
        self.assertEqual(payload["gateway"]["fallback_attempts"][0]["error"], "forced_failover")
        self.assertEqual(payload["gateway"]["routing_policy"]["source"], "model_registry")
        self.assertEqual(payload["gateway"]["route_decision"]["selected_public_model"], "qwen-turbo")
        self.assertEqual(len(payload["gateway"]["route_decision"]["fallback_attempts"]), 1)

    def test_request_can_disable_fallback(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "gateway_force_failover": True,
                "gateway_disable_fallback": True,
                "messages": [{"role": "user", "content": "Do not fallback."}],
                "stream": False,
            },
        )
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "route_failed")

    def test_request_can_choose_fallback_models(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "gateway_force_failover": True,
                "gateway_fallback_models": ["qwen-turbo"],
                "messages": [{"role": "user", "content": "Use custom fallback list."}],
                "stream": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["gateway"]["resolved_model"], "qwen-turbo")
        self.assertEqual(payload["gateway"]["routing_policy"]["source"], "request")
        self.assertEqual(payload["gateway"]["routing_policy"]["candidates"], ["smart-fast", "qwen-turbo"])

    def test_request_can_limit_allowed_providers(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "gateway_allowed_providers": ["dashscope"],
                "messages": [{"role": "user", "content": "Use provider allow list."}],
                "stream": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["gateway"]["provider"], "dashscope")
        self.assertEqual(payload["gateway"]["routing_policy"]["source"], "request")
        self.assertEqual(payload["gateway"]["routing_policy"]["allowed_providers"], ["dashscope"])

    def test_request_rejects_provider_policy_without_route(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "gateway_allowed_providers": ["openai"],
                "messages": [{"role": "user", "content": "Do not use DashScope."}],
                "stream": False,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "no_allowed_provider_route")

    def test_mock_tool_call_response_uses_openai_shape(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "Check the weather."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Get weather for a city.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "city": {"type": "string"}
                                },
                                "required": ["city"],
                            },
                        },
                    }
                ],
                "tool_choice": "auto",
                "stream": False,
            },
        )
        self.assertEqual(status, 200)
        choice = payload["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        tool_call = choice["message"]["tool_calls"][0]
        self.assertEqual(tool_call["type"], "function")
        self.assertEqual(tool_call["function"]["name"], "get_weather")
        self.assertEqual(payload["gateway"]["tool_support"], "mock_tool_call")
        self.assertIn("tools", payload["gateway"]["routing_policy"]["required_capabilities"])
        self.assertEqual(payload["gateway"]["routing_policy"]["candidates"], ["smart-fast"])

    def test_safety_preview_and_optional_blocking(self):
        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/safety-preview",
            payload={"messages": [{"role": "user", "content": "My email is user@example.com"}]},
        )
        self.assertEqual(status, 401)

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/safety-preview",
            api_key="dev-admin-key",
            payload={"messages": [{"role": "user", "content": "token=sk_test_1234567890abcdef"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["status"], "blocked")
        self.assertGreaterEqual(preview["summary"]["critical"], 1)
        self.assertTrue(preview["redaction_available"])
        self.assertGreaterEqual(preview["summary"]["redactions"], 1)
        self.assertIn("[REDACTED_", preview["redacted_preview"]["messages"][0]["content"])
        self.assertIn("not a full DLP", preview["note"])

        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "My email is user@example.com"}],
                "gateway_safety_check": True,
                "stream": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["gateway"]["safety_preview"]["status"], "review")

        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "My email is user@example.com"}],
                "gateway_redact_sensitive": True,
                "stream": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["gateway"]["redaction_applied"])
        self.assertIn("[REDACTED_EMAIL]", payload["choices"][0]["message"]["content"])
        self.assertNotIn("user@example.com", payload["choices"][0]["message"]["content"])

        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "api_key=sk_test_1234567890abcdef"}],
                "gateway_block_sensitive": True,
                "stream": False,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "safety_blocked")
        self.assertGreaterEqual(payload["error"]["details"]["summary"]["critical"], 1)

    def test_anthropic_tool_normalization(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "lookup_order",
                    "description": "Lookup an order.",
                    "parameters": {
                        "type": "object",
                        "properties": {"order_id": {"type": "string"}},
                        "required": ["order_id"],
                    },
                },
            }
        ]
        normalized = normalize_tools_for_anthropic(tools)
        self.assertEqual(normalized[0]["name"], "lookup_order")
        self.assertEqual(normalized[0]["input_schema"]["properties"]["order_id"]["type"], "string")

    def test_customer_model_access_is_enforced(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="demo-limited-key",
            payload={
                "model": "qwen-plus",
                "messages": [{"role": "user", "content": "Should be denied."}],
                "stream": False,
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "model_not_allowed")

    def test_customer_token_budget_blocks_next_request(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="demo-budget-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "Use some demo tokens."}],
                "stream": False,
            },
        )
        self.assertEqual(status, 200)

        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="demo-budget-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "This should be blocked."}],
                "stream": False,
            },
        )
        self.assertEqual(status, 402)
        self.assertEqual(payload["error"]["code"], "token_budget_exceeded")

    def test_status_exposes_customer_budget_without_provider_secret(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/status")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, payload = request_json(
            self.base_url,
            path="/v1/gateway/status",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        customers = {customer["id"]: customer for customer in payload["customers"]}
        self.assertIn("budget", customers["dev"])
        self.assertIn("dashscope", customers["dev"]["byok_providers"])
        self.assertNotIn("provider_api_keys", customers["dev"])
        reports = {report["id"]: report for report in payload["customer_reports"]}
        self.assertIn("dev", reports)
        self.assertIn("budget_state", reports["dev"])
        self.assertNotIn("provider_api_keys", reports["dev"])
        self.assertIn("request_activity", payload)
        self.assertIn("invoice_preview", payload)
        self.assertIn("totals", payload["invoice_preview"])
        catalog = {model["id"]: model for model in payload["model_catalog"]}
        self.assertIn("smart-fast", catalog)
        self.assertEqual(catalog["smart-fast"]["route_chain"][0], "smart-fast")
        self.assertIn("alerts", payload)
        self.assertIn("summary", payload["alerts"])
        self.assertIn("alerts", payload["alerts"])
        self.assertIn("access_matrix", payload)
        matrix = {row["id"]: row for row in payload["access_matrix"]}
        self.assertIn("dev", matrix)
        self.assertGreaterEqual(matrix["dev"]["allowed_count"], 1)

    def test_admin_summary_endpoints(self):
        request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "Create usage for summary endpoints."}],
                "stream": False,
            },
        )

        status, providers = request_json(self.base_url, path="/v1/gateway/providers")
        self.assertEqual(status, 401)

        status, providers = request_json(self.base_url, path="/v1/gateway/providers", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        dashscope = {provider["id"]: provider for provider in providers["data"]}["dashscope"]
        self.assertIn("smart-fast", dashscope["models"])
        self.assertIn("dev", dashscope["byok_customers"])
        self.assertNotIn("provider_api_keys", dashscope)

        status, provider_health = request_json(self.base_url, path="/v1/gateway/provider-health")
        self.assertEqual(status, 401)

        status, provider_health = request_json(self.base_url, path="/v1/gateway/provider-health", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        dashscope_health = {provider["id"]: provider for provider in provider_health["data"]}["dashscope"]
        self.assertIn(dashscope_health["status"], {"ready", "ready_mock", "degraded"})
        self.assertIn("smart-fast", dashscope_health["models"])
        self.assertIn("recent", dashscope_health)
        self.assertGreaterEqual(dashscope_health["recent"]["requests"], 1)
        self.assertIn("live_ready", dashscope_health)
        self.assertIn("mock_ready", dashscope_health)

        status, alerts = request_json(self.base_url, path="/v1/gateway/alerts")
        self.assertEqual(status, 401)

        status, alerts = request_json(self.base_url, path="/v1/gateway/alerts", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertIn(alerts["status"], {"ok", "warning", "critical"})
        self.assertIn("summary", alerts)
        alert_codes = {alert["code"] for alert in alerts["alerts"]}
        self.assertIn("default_admin_key", alert_codes)
        self.assertTrue(all("next_step" in alert for alert in alerts["alerts"]))

        status, access_matrix = request_json(self.base_url, path="/v1/gateway/access-matrix")
        self.assertEqual(status, 401)

        status, access_matrix = request_json(self.base_url, path="/v1/gateway/access-matrix", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        matrix = {row["id"]: row for row in access_matrix["data"]}
        self.assertIn("dev", matrix)
        self.assertIn("demo-limited", matrix)
        dev_models = {item["model"]: item for item in matrix["dev"]["models"]}
        limited_models = {item["model"]: item for item in matrix["demo-limited"]["models"]}
        self.assertTrue(dev_models["qwen-plus"]["allowed"])
        self.assertFalse(limited_models["qwen-plus"]["allowed"])
        self.assertEqual(limited_models["qwen-plus"]["reason"], "not listed in customer allowed_models")

        status, model_catalog = request_json(self.base_url, path="/v1/gateway/model-catalog")
        self.assertEqual(status, 401)

        status, model_catalog = request_json(self.base_url, path="/v1/gateway/model-catalog", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        catalog = {model["id"]: model for model in model_catalog["data"]}
        self.assertIn("smart-fast", catalog)
        self.assertEqual(catalog["smart-fast"]["provider"], "dashscope")
        self.assertEqual(catalog["smart-fast"]["upstream_model"], "qwen-plus")
        self.assertIn("qwen-turbo", catalog["smart-fast"]["fallback_models"])
        self.assertIn("provider_status", catalog["smart-fast"])
        self.assertIn("pricing", catalog["smart-fast"])
        self.assertGreaterEqual(catalog["smart-fast"]["usage"]["requests"], 1)

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "model": "smart-fast"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(preview["allowed"])
        self.assertEqual(preview["customer"]["id"], "dev")
        self.assertEqual(preview["routes"][0]["public_model"], "smart-fast")
        self.assertEqual(preview["routes"][0]["upstream_model"], "qwen-plus")
        self.assertIn("qwen-turbo", preview["routing_policy"]["candidates"])
        self.assertEqual(preview["routing_policy"]["required_capabilities"], ["chat"])
        self.assertEqual(preview["route_decision"]["summary"], "smart-fast -> qwen-plus via dashscope")
        self.assertIn("Fallback candidates", " ".join(preview["route_decision"]["reasons"]))

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "model": "smart-fast", "gateway_required_capabilities": ["tools"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["routing_policy"]["required_capabilities"], ["chat", "tools"])
        self.assertEqual(preview["routing_policy"]["candidates"], ["smart-fast"])
        self.assertTrue(all("tools" in route["capabilities"] for route in preview["routes"]))

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "model": "smart-fast", "gateway_required_capabilities": ["vision"]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(preview["error"]["code"], "no_capability_route")

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "model": "smart-fast", "gateway_allowed_providers": ["dashscope"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["routing_policy"]["allowed_providers"], ["dashscope"])
        self.assertTrue(all(route["provider"] == "dashscope" for route in preview["routes"]))

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "model": "smart-fast", "gateway_allowed_providers": ["openai"]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(preview["error"]["code"], "no_allowed_provider_route")

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "demo-limited", "model": "qwen-plus"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(preview["error"]["code"], "model_not_allowed")

        status, estimate = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/cost-estimate",
            payload={"customer_id": "dev", "model": "smart-fast", "prompt": "Estimate this request.", "max_tokens": 128},
        )
        self.assertEqual(status, 401)

        status, estimate = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/cost-estimate",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "model": "smart-fast", "prompt": "Estimate this request.", "max_tokens": 128},
        )
        self.assertEqual(status, 200)
        self.assertEqual(estimate["model"], "smart-fast")
        self.assertEqual(estimate["resolved_model"], "qwen-plus")
        self.assertEqual(estimate["provider"], "dashscope")
        self.assertGreaterEqual(estimate["estimate"]["prompt_tokens"], 1)
        self.assertEqual(estimate["estimate"]["completion_tokens"], 128)
        self.assertIn("budget_after_estimate", estimate)
        self.assertFalse(estimate["budget_after_estimate"]["would_exceed_budget"])

        status, key_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/key-issue-preview",
            payload={"customer_id": "customer-a", "allowed_models": ["smart-fast"]},
        )
        self.assertEqual(status, 401)

        status, key_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/key-issue-preview",
            api_key="dev-admin-key",
            payload={
                "customer_id": "customer-a",
                "name": "Customer A",
                "plan": "starter",
                "allowed_models": ["smart-fast"],
                "request_limit": 12,
                "token_budget": 900,
                "cost_budget": 0.25,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(key_preview["customer"]["id"], "customer-a")
        self.assertEqual(key_preview["customer"]["api_key"], key_preview["generated_api_key_masked"])
        self.assertTrue(key_preview["generated_api_key"].startswith("aisr_"))
        self.assertEqual(key_preview["config_snippet"]["allowed_models"], ["smart-fast"])
        self.assertNotEqual(key_preview["config_snippet"]["api_key"], key_preview["customer"]["api_key"])
        self.assertIn("does not persist", key_preview["note"])
        self.assertTrue(any("route-preview" in step for step in key_preview["next_steps"]))

        status, key_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/key-issue-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "allowed_models": ["smart-fast"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(key_preview["error"]["code"], "customer_already_exists")

        status, key_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/key-issue-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "customer-key-conflict", "api_key": "dev-gateway-key", "allowed_models": ["smart-fast"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(key_preview["error"]["code"], "api_key_already_exists")

        status, key_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/key-issue-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "customer-b", "allowed_models": ["missing-model"]},
        )
        self.assertEqual(status, 404)
        self.assertEqual(key_preview["error"]["code"], "unknown_allowed_model")

        status, customer_usage = request_json(self.base_url, path="/v1/gateway/customer-usage", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        customer_ids = {row["id"] for row in customer_usage["data"]}
        self.assertIn("dev", customer_ids)

        status, customer_reports = request_json(self.base_url, path="/v1/gateway/customer-reports")
        self.assertEqual(status, 401)

        status, customer_reports = request_json(self.base_url, path="/v1/gateway/customer-reports", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        reports = {row["id"]: row for row in customer_reports["data"]}
        self.assertIn("dev", reports)
        self.assertIn("request_summary", reports["dev"])
        self.assertGreaterEqual(reports["dev"]["request_summary"]["requests"], 1)
        self.assertIn("usage_by_model", reports["dev"])
        self.assertIn("recent_requests", reports["dev"])
        self.assertNotIn("provider_api_keys", reports["dev"])

        status, invoice = request_json(self.base_url, path="/v1/gateway/invoice-preview")
        self.assertEqual(status, 401)

        status, invoice = request_json(self.base_url, path="/v1/gateway/invoice-preview", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertIn("totals", invoice)
        self.assertGreaterEqual(invoice["totals"]["customers"], 1)
        invoice_by_customer = {row["customer"]["id"]: row for row in invoice["data"]}
        self.assertIn("dev", invoice_by_customer)
        self.assertIn("estimated_cost", invoice_by_customer["dev"])
        self.assertIn("usage_by_model", invoice_by_customer["dev"])
        self.assertIn("not a legal invoice", invoice_by_customer["dev"]["note"])

        status, invoice = request_json(
            self.base_url,
            path="/v1/gateway/invoice-preview?customer_id=dev",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(invoice["data"]), 1)
        self.assertEqual(invoice["data"][0]["customer"]["id"], "dev")

        status, invoice = request_json(
            self.base_url,
            path="/v1/gateway/invoice-preview?customer_id=missing",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 404)
        self.assertEqual(invoice["error"]["code"], "unknown_customer")

        status, csv_preview = request_text(
            self.base_url,
            path="/v1/gateway/invoice-preview?customer_id=dev&format=csv",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertIn("invoice_id,customer_id,plan", csv_preview)
        self.assertIn(",dev,internal-demo,", csv_preview)

        status, model_usage = request_json(self.base_url, path="/v1/gateway/model-usage", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        model_ids = {row["id"] for row in model_usage["data"]}
        self.assertIn("smart-fast", model_ids)

        status, request_summary = request_json(self.base_url, path="/v1/gateway/request-summary", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertIn("by_customer", request_summary)
        self.assertIn("by_code", request_summary)

        status, activity = request_json(self.base_url, path="/v1/gateway/request-activity")
        self.assertEqual(status, 401)

        status, activity = request_json(
            self.base_url,
            path="/v1/gateway/request-activity?customer_id=dev&status=success&limit=5",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(activity["filters"]["customer_id"], "dev")
        self.assertEqual(activity["filters"]["status"], "success")
        self.assertLessEqual(len(activity["data"]), 5)
        self.assertTrue(any(row["customer_id"] == "dev" for row in activity["data"]))
        self.assertTrue(all(row["outcome"] == "success" for row in activity["data"]))
        self.assertIn("summary", activity["data"][0])
        self.assertNotIn("provider_api_keys", activity["data"][0])

    def test_admin_page_accepts_query_admin_key_for_browser_demo(self):
        request = urllib.request.Request(self.base_url + "/admin?admin_key=dev-admin-key")
        with urllib.request.urlopen(request, timeout=5) as response:
            html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("Gateway Admin", html)

    def test_config_check_reports_demo_warnings(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/config-check")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, payload = request_json(
            self.base_url,
            path="/v1/gateway/config-check",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "warning")
        codes = {check["code"] for check in payload["checks"]}
        self.assertIn("default_admin_key", codes)
        self.assertIn("demo_customer_keys", codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
