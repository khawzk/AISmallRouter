#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_REGISTRY_PATH = "model_registry.json"
DEFAULT_GATEWAY_API_KEY = "dev-gateway-key"
DEFAULT_REQUEST_LIMIT = 60
DEFAULT_LIMIT_WINDOW_SECONDS = 60
DEFAULT_DASHBOARD_PATH = "dashboard.html"


def load_registry(path):
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)

    models = {}
    for model in data.get("models", []):
        if model.get("enabled", True):
            models[model["id"]] = model
    return models


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


def make_error(handler, status, message, code):
    make_json_response(
        handler,
        status,
        {
            "error": {
                "message": message,
                "type": "gateway_error",
                "code": code,
            }
        },
    )


def key_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def now_unix():
    return int(time.time())


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
            }
            for public_name, model in sorted(models.items())
        ],
    }


def gateway_status(server):
    return {
        "status": "ok",
        "mode": "mock" if server.mock_mode else "live",
        "request_limit": server.request_limit,
        "limit_window_seconds": server.limit_window_seconds,
        "models": [
            {
                "id": public_name,
                "provider": model["provider"],
                "upstream_model": model["upstream_model"],
                "capabilities": model.get("capabilities", []),
            }
            for public_name, model in sorted(server.models.items())
        ],
    }


def dashboard_html(server):
    state = json.dumps(gateway_status(server))
    dashboard_path = os.getenv("GATEWAY_DASHBOARD_PATH", DEFAULT_DASHBOARD_PATH)
    if os.path.exists(dashboard_path):
        with open(dashboard_path, "r", encoding="utf-8") as file:
            return file.read().replace("__GATEWAY_STATE__", state)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Model Gateway Prototype</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: #f6f7f9;
      color: #18202b;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 24px;
      margin-bottom: 22px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 30px; line-height: 1.15; letter-spacing: 0; }}
    h2 {{ margin: 0 0 14px; font-size: 18px; letter-spacing: 0; }}
    p {{ margin: 0; color: #657386; line-height: 1.55; }}
    section {{
      background: #ffffff;
      border: 1px solid #d8dde5;
      border-radius: 8px;
      padding: 18px;
    }}
    .status {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
    .pill {{
      border: 1px solid #d8dde5;
      background: #ffffff;
      border-radius: 999px;
      padding: 7px 11px;
      color: #657386;
      font-size: 13px;
      white-space: nowrap;
    }}
    .pill.good {{ color: #1b8f55; background: #eaf8f0; border-color: #bee6cd; }}
    .pill.warn {{ color: #a15c00; background: #fff4df; border-color: #f0d49f; }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.15fr) minmax(320px, 0.85fr);
      gap: 18px;
      align-items: start;
    }}
    .layers {{ display: grid; gap: 12px; }}
    .layer {{
      display: grid;
      grid-template-columns: 160px 1fr;
      border: 1px solid #d8dde5;
      border-radius: 8px;
      overflow: hidden;
      min-height: 78px;
    }}
    .layer-title {{
      display: flex;
      align-items: center;
      padding: 14px;
      font-weight: 700;
      background: #eef1f5;
      border-right: 1px solid #d8dde5;
    }}
    .layer:nth-child(2) .layer-title {{ background: #eaf2ff; }}
    .layer:nth-child(3) .layer-title {{ background: #eaf8f0; }}
    .layer:nth-child(4) .layer-title {{ background: #f1edff; }}
    .layer:nth-child(5) .layer-title {{ background: #fff4df; }}
    .layer-body {{
      padding: 12px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-content: center;
    }}
    .node {{
      border: 1px solid #d8dde5;
      background: #ffffff;
      border-radius: 8px;
      padding: 9px 10px;
      min-width: 128px;
      font-size: 13px;
      color: #2a3441;
    }}
    .node strong {{ display: block; margin-bottom: 3px; font-size: 13px; }}
    .node span {{ color: #657386; font-size: 12px; }}
    .side {{ display: grid; gap: 18px; }}
    .route-flow {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 8px;
      margin-top: 14px;
    }}
    .route-step {{
      border: 1px solid #d8dde5;
      background: #ffffff;
      border-radius: 8px;
      padding: 10px;
      min-height: 86px;
      position: relative;
    }}
    .route-step b {{ display: block; margin-bottom: 5px; font-size: 13px; }}
    .route-step span {{ color: #657386; font-size: 12px; line-height: 1.4; }}
    .route-step.active {{ border-color: #1677ff; background: #eef6ff; }}
    .route-step.final {{ border-color: #1b8f55; background: #eaf8f0; }}
    .metric-row {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin: 18px 0;
    }}
    .metric {{
      background: #ffffff;
      border: 1px solid #d8dde5;
      border-radius: 8px;
      padding: 14px;
    }}
    .metric b {{ display: block; font-size: 22px; margin-bottom: 4px; }}
    .metric span {{ color: #657386; font-size: 13px; }}
    .models {{ display: grid; gap: 10px; }}
    .model {{
      border: 1px solid #d8dde5;
      border-radius: 8px;
      padding: 12px;
      background: #ffffff;
    }}
    .model-top {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
      font-weight: 700;
    }}
    .small {{ color: #657386; font-size: 13px; line-height: 1.45; }}
    .tester {{ display: grid; gap: 10px; }}
    label {{ display: grid; gap: 6px; color: #657386; font-size: 13px; }}
    input, textarea, select {{
      width: 100%;
      border: 1px solid #d8dde5;
      border-radius: 8px;
      padding: 10px;
      font: inherit;
      color: #18202b;
      background: #ffffff;
    }}
    textarea {{ min-height: 92px; resize: vertical; }}
    button {{
      border: 0;
      background: #1677ff;
      color: #ffffff;
      border-radius: 8px;
      padding: 11px 14px;
      font-weight: 700;
      cursor: pointer;
    }}
    button:disabled {{ opacity: 0.55; cursor: wait; }}
    pre {{
      margin: 0;
      background: #111827;
      color: #e6edf6;
      border-radius: 8px;
      padding: 12px;
      overflow: auto;
      min-height: 120px;
      max-height: 320px;
      font-size: 12px;
      line-height: 1.45;
    }}
    .explain {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-top: 18px;
    }}
    .explain div {{
      border: 1px solid #d8dde5;
      background: #ffffff;
      border-radius: 8px;
      padding: 14px;
    }}
    .explain strong {{ display: block; margin-bottom: 6px; }}
    @media (max-width: 840px) {{
      main {{ padding: 18px; }}
      header, .grid {{ display: block; }}
      .status {{ justify-content: flex-start; margin-top: 14px; }}
      section {{ margin-bottom: 14px; }}
      .layer {{ grid-template-columns: 1fr; }}
      .layer-title {{ border-right: 0; border-bottom: 1px solid #d8dde5; }}
      .explain {{ grid-template-columns: 1fr; }}
      .route-flow, .metric-row {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>AI Model Gateway Prototype</h1>
        <p>One simple API for customers. The gateway handles keys, limits, model routing, provider adapters, and model providers behind the scenes.</p>
      </div>
      <div class="status">
        <span class="pill good">Server: online</span>
        <span class="pill" id="modePill">Mode</span>
        <span class="pill" id="limitPill">Limit</span>
      </div>
    </header>

    <div class="metric-row">
      <div class="metric"><b>1 API</b><span>Customers call one OpenAI-compatible endpoint.</span></div>
      <div class="metric"><b>2 Models</b><span>Current registry exposes qwen-plus and smart-fast.</span></div>
      <div class="metric"><b>0 Spend</b><span>Mock mode demonstrates routing without provider calls.</span></div>
    </div>

    <div class="grid">
      <section>
        <h2>Layered Architecture</h2>
        <div class="layers">
          <div class="layer">
            <div class="layer-title">1. Customer</div>
            <div class="layer-body">
              <div class="node"><strong>Customer App</strong><span>Website, backend, agent</span></div>
            </div>
          </div>
          <div class="layer">
            <div class="layer-title">2. One API</div>
            <div class="layer-body">
              <div class="node"><strong>OpenAI-Compatible API</strong><span>/v1/chat/completions</span></div>
            </div>
          </div>
          <div class="layer">
            <div class="layer-title">3. Entry Control</div>
            <div class="layer-body">
              <div class="node"><strong>Customer API Keys</strong><span>Who is calling?</span></div>
              <div class="node"><strong>Usage Limits</strong><span>How much can they use?</span></div>
              <div class="node"><strong>Request Logs</strong><span>What happened?</span></div>
            </div>
          </div>
          <div class="layer">
            <div class="layer-title">4. Model Control</div>
            <div class="layer-body">
              <div class="node"><strong>Model Registry</strong><span>Which models exist?</span></div>
              <div class="node"><strong>Model Router</strong><span>Where should it go?</span></div>
              <div class="node"><strong>Fallback Rules</strong><span>Future step</span></div>
            </div>
          </div>
          <div class="layer">
            <div class="layer-title">5. Providers</div>
            <div class="layer-body">
              <div class="node"><strong>Qwen Adapter</strong><span>smart-fast to qwen-plus</span></div>
              <div class="node"><strong>Alibaba Cloud</strong><span>Model Studio / DashScope</span></div>
            </div>
          </div>
        </div>

        <h2 style="margin-top: 20px;">Route Simulation</h2>
        <p>This is how the gateway simulates routing today. In live mode, the last step calls Alibaba Cloud Model Studio.</p>
        <div class="route-flow">
          <div class="route-step"><b>1. Request</b><span>Customer sends model = smart-fast.</span></div>
          <div class="route-step active"><b>2. Registry</b><span>Gateway checks model_registry.json.</span></div>
          <div class="route-step active"><b>3. Router</b><span>smart-fast maps to qwen-plus.</span></div>
          <div class="route-step"><b>4. Adapter</b><span>Request becomes DashScope compatible.</span></div>
          <div class="route-step final"><b>5. Response</b><span>Mock response returns route_trace.</span></div>
        </div>
      </section>

      <div class="side">
        <section>
          <h2>Available Models</h2>
          <div class="models" id="models"></div>
        </section>

        <section>
          <h2>Try Chat Completion</h2>
          <div class="tester">
            <label>Gateway API Key
              <input id="apiKey" value="dev-gateway-key">
            </label>
            <label>Model
              <select id="modelSelect"></select>
            </label>
            <label>Prompt
              <textarea id="prompt">Explain this gateway in one short sentence.</textarea>
            </label>
            <button id="sendButton" type="button">Send Test Request</button>
            <pre id="output">Click Send Test Request to call /v1/chat/completions.</pre>
            <div class="small" id="routeTrace">Route trace will appear after a test request.</div>
          </div>
        </section>
      </div>
    </div>

    <div class="explain">
      <div><strong>For customers</strong><p>They call one API and do not need to learn every model provider.</p></div>
      <div><strong>For operations</strong><p>We can manage API keys, usage limits, logs, and model access in one place.</p></div>
      <div><strong>For future growth</strong><p>We can add OpenAI, Claude, Xiaomi, fallback, and billing later.</p></div>
    </div>
  </main>

  <script>
    const state = {state};
    const modePill = document.getElementById("modePill");
    const limitPill = document.getElementById("limitPill");
    const modelsEl = document.getElementById("models");
    const modelSelect = document.getElementById("modelSelect");
    const output = document.getElementById("output");
    const routeTrace = document.getElementById("routeTrace");
    const button = document.getElementById("sendButton");

    modePill.textContent = "Mode: " + state.mode;
    modePill.className = state.mode === "mock" ? "pill good" : "pill warn";
    limitPill.textContent = "Limit: " + state.request_limit + " per " + state.limit_window_seconds + "s";

    state.models.forEach((model) => {{
      const item = document.createElement("div");
      item.className = "model";
      item.innerHTML = `
        <div class="model-top">
          <span>${{model.id}}</span>
          <span class="small">${{model.provider}}</span>
        </div>
        <div class="small">Upstream model: ${{model.upstream_model}}</div>
        <div class="small">Capabilities: ${{model.capabilities.join(", ")}}</div>
      `;
      modelsEl.appendChild(item);

      const option = document.createElement("option");
      option.value = model.id;
      option.textContent = model.id;
      modelSelect.appendChild(option);
    }});

    async function sendTestRequest() {{
      button.disabled = true;
      output.textContent = "Sending request...";
      try {{
        const response = await fetch("/v1/chat/completions", {{
          method: "POST",
          headers: {{
            "Authorization": "Bearer " + document.getElementById("apiKey").value,
            "Content-Type": "application/json"
          }},
          body: JSON.stringify({{
            model: modelSelect.value,
            messages: [
              {{ role: "user", content: document.getElementById("prompt").value }}
            ],
            stream: false
          }})
        }});
        const data = await response.json();
        output.textContent = JSON.stringify(data, null, 2);
        if (data.gateway && data.gateway.route_trace) {{
          routeTrace.innerHTML = "<b>Route trace:</b><br>" + data.gateway.route_trace.map((step) => "• " + step).join("<br>");
        }} else {{
          routeTrace.textContent = "No gateway route trace returned.";
        }}
      }} catch (error) {{
        output.textContent = String(error);
        routeTrace.textContent = "Request failed before route trace was available.";
      }} finally {{
        button.disabled = false;
      }}
    }}

    button.addEventListener("click", sendTestRequest);
  </script>
</body>
</html>"""


def mock_completion(request_payload, public_model, upstream_model):
    messages = request_payload.get("messages", [])
    last_user_message = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            last_user_message = message.get("content", "")
            break

    content = (
        "Mock response from the Model Gateway. "
        f"The public model '{public_model}' was routed to upstream model '{upstream_model}'."
    )
    if last_user_message:
        content += f" Last user message: {last_user_message[:120]}"

    return {
        "id": f"chatcmpl-mock-{now_unix()}",
        "object": "chat.completion",
        "created": now_unix(),
        "model": public_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "gateway": {
            "mode": "mock",
            "route_trace": [
                "Customer sends one OpenAI-compatible request",
                f"Gateway reads model = {public_model}",
                f"Model registry maps {public_model} to {upstream_model}",
                "Provider adapter prepares the upstream request",
                "Mock mode returns a simulated provider response",
            ],
        },
    }


def call_openai_compatible_provider(model_config, request_payload, timeout):
    api_key_env = model_config["api_key_env"]
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise RuntimeError(f"Missing provider API key. Set {api_key_env}.")

    upstream_payload = dict(request_payload)
    upstream_payload["model"] = model_config["upstream_model"]

    url = model_config["base_url"].rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(upstream_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw_body = response.read().decode("utf-8")
        return json.loads(raw_body)


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "ModelGatewayPrototype/0.1"

    def log_message(self, format_text, *args):
        if self.server.quiet:
            return
        super().log_message(format_text, *args)

    def do_HEAD(self):
        if self.path in {"/", "/dashboard"}:
            html = dashboard_html(self.server)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode("utf-8"))))
            self.end_headers()
            return

        if self.path in {"/health", "/v1/gateway/status"}:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            return

        make_error(self, 404, "Route not found.", "route_not_found")

    def do_GET(self):
        if self.path in {"/", "/dashboard"}:
            make_html_response(self, 200, dashboard_html(self.server))
            return

        if self.path == "/health":
            make_json_response(self, 200, {"status": "ok"})
            return

        if self.path == "/v1/gateway/status":
            make_json_response(self, 200, gateway_status(self.server))
            return

        if self.path == "/v1/models":
            if not self.check_auth():
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

        if not self.check_auth():
            return
        if not self.check_usage_limit():
            return

        started_at = time.perf_counter()
        request_payload = self.read_json_body()
        if request_payload is None:
            return

        public_model = request_payload.get("model")
        if not public_model:
            make_error(self, 400, "Missing required field: model.", "missing_model")
            return

        model_config = self.server.models.get(public_model)
        if not model_config:
            make_error(self, 404, f"Unknown model: {public_model}.", "unknown_model")
            return

        if request_payload.get("stream"):
            make_error(
                self,
                400,
                "Streaming is not supported in this first prototype. Set stream=false.",
                "stream_not_supported",
            )
            return

        try:
            if self.server.mock_mode:
                response_payload = mock_completion(
                    request_payload,
                    public_model=public_model,
                    upstream_model=model_config["upstream_model"],
                )
            else:
                response_payload = call_openai_compatible_provider(
                    model_config,
                    request_payload,
                    timeout=self.server.timeout,
                )
                response_payload["model"] = public_model
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            self.write_request_log(started_at, public_model, model_config["provider"], exc.code)
            make_json_response(
                self,
                exc.code,
                {
                    "error": {
                        "message": "The upstream provider returned an error.",
                        "type": "provider_error",
                        "code": f"upstream_{exc.code}",
                        "provider": model_config["provider"],
                        "details": body[:1000],
                    }
                },
            )
            return
        except Exception as exc:
            self.write_request_log(started_at, public_model, model_config["provider"], 500)
            make_json_response(
                self,
                500,
                {
                    "error": {
                        "message": str(exc),
                        "type": "gateway_error",
                        "code": "gateway_exception",
                    }
                },
            )
            return

        self.write_request_log(started_at, public_model, model_config["provider"], 200)
        make_json_response(self, 200, response_payload)

    def check_auth(self):
        expected_key = self.server.gateway_api_key
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            make_error(self, 401, "Missing Authorization Bearer token.", "missing_auth")
            return False

        provided_key = header.removeprefix("Bearer ").strip()
        if provided_key != expected_key:
            make_error(self, 401, "Invalid gateway API key.", "invalid_api_key")
            return False

        self.customer_api_key = provided_key
        return True

    def check_usage_limit(self):
        limit = self.server.request_limit
        if limit <= 0:
            return True

        api_key = getattr(self, "customer_api_key", "")
        hashed_key = key_hash(api_key)
        current_time = time.time()
        window_seconds = self.server.limit_window_seconds
        usage_state = self.server.usage_by_key.get(hashed_key)

        if not usage_state or current_time >= usage_state["reset_at"]:
            usage_state = {
                "count": 0,
                "reset_at": current_time + window_seconds,
            }

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
        except ValueError:
            make_error(self, 400, "Invalid Content-Length.", "invalid_content_length")
            return None

        try:
            body = self.rfile.read(length).decode("utf-8")
            return json.loads(body)
        except json.JSONDecodeError:
            make_error(self, 400, "Request body must be valid JSON.", "invalid_json")
            return None

    def write_request_log(self, started_at, model, provider, status):
        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
        auth = self.headers.get("Authorization", "")
        api_key = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        log_record = {
            "created": now_unix(),
            "api_key_hash": key_hash(api_key) if api_key else None,
            "model": model,
            "provider": provider,
            "status": status,
            "latency_ms": latency_ms,
            "mock_mode": self.server.mock_mode,
        }
        print(json.dumps(log_record, ensure_ascii=False), flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description="Minimal OpenAI-compatible model gateway prototype.")
    parser.add_argument("--host", default=os.getenv("GATEWAY_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("GATEWAY_PORT", DEFAULT_PORT)))
    parser.add_argument("--registry", default=os.getenv("MODEL_REGISTRY", DEFAULT_REGISTRY_PATH))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("UPSTREAM_TIMEOUT", "60")))
    parser.add_argument(
        "--request-limit",
        type=int,
        default=int(os.getenv("GATEWAY_REQUEST_LIMIT", DEFAULT_REQUEST_LIMIT)),
        help="Max authenticated requests per API key in the limit window. Use 0 to disable.",
    )
    parser.add_argument(
        "--limit-window-seconds",
        type=int,
        default=int(os.getenv("GATEWAY_LIMIT_WINDOW_SECONDS", DEFAULT_LIMIT_WINDOW_SECONDS)),
    )
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
    models = load_registry(args.registry)
    gateway_api_key = os.getenv("GATEWAY_API_KEY", DEFAULT_GATEWAY_API_KEY)

    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    server.models = models
    server.gateway_api_key = gateway_api_key
    server.mock_mode = args.mock
    server.timeout = args.timeout
    server.quiet = args.quiet
    server.request_limit = args.request_limit
    server.limit_window_seconds = args.limit_window_seconds
    server.usage_by_key = {}

    mode = "mock" if args.mock else "live"
    print(f"Model Gateway listening on http://{args.host}:{args.port}")
    print(f"Mode: {mode}")
    print(f"Models: {', '.join(sorted(models.keys()))}")
    print(f"Request limit: {args.request_limit} per {args.limit_window_seconds}s")
    print("Gateway API key env: GATEWAY_API_KEY")
    server.serve_forever()


if __name__ == "__main__":
    main()
