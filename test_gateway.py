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


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request_json(base_url, method="GET", path="/", api_key=None, payload=None):
    body = None
    headers = {}
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

        status, usage = request_json(self.base_url, path="/v1/gateway/usage")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(usage["data"]), 1)

        status, requests = request_json(self.base_url, path="/v1/gateway/requests")
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
        self.assertEqual(status, 200)
        customers = {customer["id"]: customer for customer in payload["customers"]}
        self.assertIn("budget", customers["dev"])
        self.assertIn("dashscope", customers["dev"]["byok_providers"])
        self.assertNotIn("provider_api_keys", customers["dev"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
