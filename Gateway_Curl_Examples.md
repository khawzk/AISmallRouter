# Gateway Curl Examples

These examples use the local prototype gateway.

The default local gateway key is `dev-gateway-key`.

For real use, set your own key:

```bash
export GATEWAY_API_KEY="your-local-gateway-key"
```

## Start In Mock Mode

Mock mode does not call Alibaba Cloud.

Use this mode to test the gateway without spending money.

```bash
python3 model_gateway.py --mock
```

## Run The Automated Demo Test

This starts the gateway on a temporary local port.

It does not spend provider credits.

```bash
python3 test_gateway.py
```

The test covers auth, OpenAPI contract, Postman collection, demo bundle, customer integration guide, production readiness, provider contracts, incident playbook, support policy, pilot checklist, executive brief, roadmap, model listing, customer self view, customer key lifecycle, audit events, provider lifecycle, model route lifecycle, routing, route strategy, policy presets, route preview, route decision summary, provider allow-list routing, capability routing, safety preview, cost estimate, customer key issue preview, invoice preview, fallback, model access, budgets, SQLite logs, alerts, access matrix, provider health, model catalog, customer reports, request activity, request detail lookup, and admin summary endpoints.

Open the visual dashboard:

```text
http://127.0.0.1:8787/
```

Fetch the OpenAPI contract:

```bash
curl http://127.0.0.1:8787/openapi.json
```

Fetch the Postman collection:

```bash
curl http://127.0.0.1:8787/postman_collection.json
```

Fetch the customer demo bundle manifest:

```bash
curl http://127.0.0.1:8787/v1/gateway/demo-bundle \
  -H "Authorization: Bearer dev-admin-key"
```

Fetch the production readiness report:

```bash
curl http://127.0.0.1:8787/v1/gateway/production-readiness \
  -H "Authorization: Bearer dev-admin-key"
```

Fetch the provider contract matrix:

```bash
curl http://127.0.0.1:8787/v1/gateway/provider-contracts \
  -H "Authorization: Bearer dev-admin-key"
```

Fetch the incident playbook:

```bash
curl http://127.0.0.1:8787/v1/gateway/incident-playbook \
  -H "Authorization: Bearer dev-admin-key"
```

Fetch the support policy:

```bash
curl http://127.0.0.1:8787/v1/gateway/support-policy \
  -H "Authorization: Bearer dev-admin-key"
```

Fetch the pilot checklist:

```bash
curl http://127.0.0.1:8787/v1/gateway/pilot-checklist \
  -H "Authorization: Bearer dev-admin-key"
```

Fetch the executive brief:

```bash
curl http://127.0.0.1:8787/v1/gateway/executive-brief \
  -H "Authorization: Bearer dev-admin-key"
```

Fetch the roadmap:

```bash
curl http://127.0.0.1:8787/v1/gateway/roadmap \
  -H "Authorization: Bearer dev-admin-key"
```

Fetch the customer integration guide:

```bash
curl http://127.0.0.1:8787/v1/gateway/integration-guide \
  -H "Authorization: Bearer dev-gateway-key"
```

Optional: test a very small request limit.

```bash
python3 model_gateway.py --mock --request-limit 1 --limit-window-seconds 60
```

Optional: store the SQLite database in a temporary folder.

```bash
python3 model_gateway.py --mock \
  --data-dir /tmp/aismallrouter-data \
  --log-dir /tmp/aismallrouter-logs
```

## List Models

```bash
curl http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer dev-gateway-key"
```

## Check Customer Self View

This uses a customer gateway key, not the admin key.

It shows the customer's allowed models, budget state, usage, recent requests, and invoice preview.

It does not show provider secrets.

```bash
curl http://127.0.0.1:8787/v1/gateway/me \
  -H "Authorization: Bearer dev-gateway-key"
```

## Check Customer Integration Guide

This returns customer-specific quickstart steps, safe code examples, and a go-live checklist.

It does not show provider secrets.

```bash
curl http://127.0.0.1:8787/v1/gateway/integration-guide \
  -H "Authorization: Bearer dev-gateway-key"
```

## Create, Rotate, And Disable A Customer Key

Create a customer:

```bash
curl http://127.0.0.1:8787/v1/gateway/customers \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "customer-demo",
    "name": "Customer Demo",
    "plan": "starter",
    "allowed_models": ["smart-fast"],
    "default_policy": "lowest_cost",
    "request_limit": 60,
    "token_budget": 10000,
    "cost_budget": 1.0
  }'
```

Rotate the customer key:

```bash
curl http://127.0.0.1:8787/v1/gateway/customers/rotate-key \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"customer_id": "customer-demo"}'
```

Disable the customer:

```bash
curl http://127.0.0.1:8787/v1/gateway/customers/disable \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"customer_id": "customer-demo"}'
```

This updates `customer_keys.json` in the running prototype.

The customer's `default_policy` is used when a request does not send `gateway_policy`.

An explicit request `gateway_policy` overrides the customer default.

## Create, Update, And Disable A Provider

Create a provider:

```bash
curl http://127.0.0.1:8787/v1/gateway/providers \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "provider_id": "demo-provider",
    "name": "Demo Provider",
    "type": "openai_compatible",
    "base_url": "https://example.com/v1",
    "api_key_env": "DEMO_PROVIDER_API_KEY"
  }'
```

Update the provider:

```bash
curl http://127.0.0.1:8787/v1/gateway/providers/update \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "provider_id": "demo-provider",
    "name": "Demo Provider Updated",
    "api_key_env": "DEMO_PROVIDER_API_KEY_2"
  }'
```

Disable the provider:

```bash
curl http://127.0.0.1:8787/v1/gateway/providers/disable \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"provider_id": "demo-provider"}'
```

This updates `model_registry.json`.

Disabling a provider also disables active model routes that point to it.

## Create, Update, And Disable A Model Route

Create a public model route:

```bash
curl http://127.0.0.1:8787/v1/gateway/model-routes \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model_id": "customer-fast",
    "provider": "dashscope",
    "upstream_model": "qwen-plus",
    "fallback_models": ["qwen-turbo"],
    "capabilities": ["chat", "streaming"],
    "pricing": {
      "prompt_per_1k": 0,
      "completion_per_1k": 0
    }
  }'
```

Update the route:

```bash
curl http://127.0.0.1:8787/v1/gateway/model-routes/update \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model_id": "customer-fast",
    "fallback_models": ["smart-fast"],
    "capabilities": ["chat", "streaming", "tools"]
  }'
```

Disable the route:

```bash
curl http://127.0.0.1:8787/v1/gateway/model-routes/disable \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"model_id": "customer-fast"}'
```

This updates `model_registry.json` in the running prototype.

## Check Audit Events

Audit events show customer key lifecycle actions.

They do not store full API keys.

```bash
curl "http://127.0.0.1:8787/v1/gateway/audit-events?target_id=customer-demo" \
  -H "Authorization: Bearer dev-admin-key"
```

Filter by action:

```bash
curl "http://127.0.0.1:8787/v1/gateway/audit-events?action=customer.key_rotated" \
  -H "Authorization: Bearer dev-admin-key"
```

## Check Operational Alerts

```bash
curl http://127.0.0.1:8787/v1/gateway/alerts \
  -H "Authorization: Bearer dev-admin-key"
```

## Check Access Matrix

```bash
curl http://127.0.0.1:8787/v1/gateway/access-matrix \
  -H "Authorization: Bearer dev-admin-key"
```

## Check Provider Health

```bash
curl http://127.0.0.1:8787/v1/gateway/provider-health \
  -H "Authorization: Bearer dev-admin-key"
```

## Check Model Catalog

```bash
curl http://127.0.0.1:8787/v1/gateway/model-catalog \
  -H "Authorization: Bearer dev-admin-key"
```

## Preview A Route Without Calling A Provider

```bash
curl http://127.0.0.1:8787/v1/gateway/route-preview \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "dev",
    "model": "smart-fast"
}'
```

The response includes `route_decision`.

Use it to explain why the gateway selected a model and provider.

## Preview A Route With Provider Control

This asks the gateway to use only DashScope routes.

It does not call a provider.

```bash
curl http://127.0.0.1:8787/v1/gateway/route-preview \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "dev",
    "model": "smart-fast",
    "gateway_allowed_providers": ["dashscope"]
  }'
```

Try `["openai"]` in mock mode to see a clear no-route error.

## Preview A Route With Strategy Control

This asks the gateway to reorder candidates by estimated cost.

It does not call a provider.

```bash
curl http://127.0.0.1:8787/v1/gateway/route-preview \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "dev",
    "model": "smart-fast",
    "gateway_route_strategy": "lowest_cost"
  }'
```

Supported strategies:

- `registry`
- `lowest_cost`
- `fastest`
- `healthiest`

The response includes `candidate_scores` in `route_decision`.

## Preview A Route With A Policy Preset

List policy presets:

```bash
curl http://127.0.0.1:8787/v1/gateway/policy-presets \
  -H "Authorization: Bearer dev-admin-key"
```

Use a preset:

```bash
curl http://127.0.0.1:8787/v1/gateway/route-preview \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "dev",
    "model": "smart-fast",
    "gateway_policy": "tool_ready"
  }'
```

Presets are shortcuts.

Explicit request controls override preset controls.

## Preview A Route With Capability Control

This asks the gateway to use only routes that support tool calling.

It does not call a provider.

```bash
curl http://127.0.0.1:8787/v1/gateway/route-preview \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "dev",
    "model": "smart-fast",
    "gateway_required_capabilities": ["tools"]
  }'
```

Try `["vision"]` to see a clear no-capability error.

## Estimate Cost Without Calling A Provider

```bash
curl http://127.0.0.1:8787/v1/gateway/cost-estimate \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "dev",
    "model": "smart-fast",
    "prompt": "Explain the gateway.",
    "max_tokens": 256
}'
```

## Preview A New Customer Key

This generates a safe onboarding package.

It does not save the customer.

```bash
curl http://127.0.0.1:8787/v1/gateway/key-issue-preview \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "customer-demo",
    "name": "Customer Demo",
    "plan": "starter",
    "allowed_models": ["smart-fast"],
    "request_limit": 60,
    "token_budget": 10000,
    "cost_budget": 1.0
  }'
```

## Check Customer Reports

```bash
curl http://127.0.0.1:8787/v1/gateway/customer-reports \
  -H "Authorization: Bearer dev-admin-key"
```

## Preview Customer Invoice

This is an estimated billing preview.

It is not a legal invoice.

```bash
curl "http://127.0.0.1:8787/v1/gateway/invoice-preview?customer_id=dev" \
  -H "Authorization: Bearer dev-admin-key"
```

CSV output:

```bash
curl "http://127.0.0.1:8787/v1/gateway/invoice-preview?customer_id=dev&format=csv" \
  -H "Authorization: Bearer dev-admin-key"
```

## Preview Request Safety

This checks for obvious sensitive data before a provider call.

```bash
curl http://127.0.0.1:8787/v1/gateway/safety-preview \
  -H "Authorization: Bearer dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {
        "role": "user",
        "content": "My email is user@example.com"
      }
    ]
}'
```

## Redact Sensitive Data Before Provider Call

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer dev-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart-fast",
    "messages": [
      {
        "role": "user",
        "content": "My email is user@example.com"
      }
    ],
    "gateway_redact_sensitive": true,
    "stream": false
  }'
```

## Check Request Activity

```bash
curl "http://127.0.0.1:8787/v1/gateway/request-activity?customer_id=dev&status=success&limit=10" \
  -H "Authorization: Bearer dev-admin-key"
```

## Check One Request Detail

Use a `gateway.request_id` returned by `/v1/chat/completions`.

```bash
curl "http://127.0.0.1:8787/v1/gateway/request-detail?request_id=abc123" \
  -H "Authorization: Bearer dev-admin-key"
```

## Chat Completion In Mock Mode

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer dev-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart-fast",
    "messages": [
      {
        "role": "user",
        "content": "Explain this gateway in one short sentence."
      }
    ],
    "stream": false
  }'
```

## Streaming In Mock Mode

```bash
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer dev-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart-fast",
    "messages": [
      {
        "role": "user",
        "content": "Stream a short answer."
      }
    ],
    "stream": true
  }'
```

## Tool Calling Shape In Mock Mode

Mock mode returns an OpenAI-style `tool_calls` response.

It does not execute the tool.

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer dev-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart-fast",
    "messages": [
      {
        "role": "user",
        "content": "Check the weather."
      }
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "Get weather for a city.",
          "parameters": {
            "type": "object",
            "properties": {
              "city": {
                "type": "string"
              }
            },
            "required": ["city"]
          }
        }
      }
    ],
    "tool_choice": "auto",
    "stream": false
  }'
```

## Test Fallback Routing

Mock mode can force the first route to fail so the gateway uses the fallback model.

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer dev-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart-fast",
    "gateway_force_failover": true,
    "messages": [
      {
        "role": "user",
        "content": "Show fallback routing."
      }
    ],
    "stream": false
  }'
```

Disable fallback for one request:

```bash
curl -i http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer dev-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart-fast",
    "gateway_force_failover": true,
    "gateway_disable_fallback": true,
    "messages": [
      {
        "role": "user",
        "content": "Do not use fallback."
      }
    ],
    "stream": false
  }'
```

Choose fallback models for one request:

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer dev-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart-fast",
    "gateway_force_failover": true,
    "gateway_fallback_models": ["qwen-turbo"],
    "messages": [
      {
        "role": "user",
        "content": "Use my fallback list."
      }
    ],
    "stream": false
  }'
```

## Test Customer Model Access

`demo-limited-key` can only use `smart-fast`.

This should return `403 Forbidden`:

```bash
curl -i http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer demo-limited-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-plus",
    "messages": [
      {
        "role": "user",
        "content": "Should be denied."
      }
    ],
    "stream": false
  }'
```

## View Admin And Logs

Open:

```text
http://127.0.0.1:8787/admin?admin_key=dev-admin-key
```

Or fetch JSON:

```bash
curl http://127.0.0.1:8787/v1/gateway/requests \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/usage \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/customers \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/providers \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/provider-contracts \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/customer-usage \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/model-usage \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/request-summary \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/config-check \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/production-readiness \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/incident-playbook \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/support-policy \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/pilot-checklist \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/executive-brief \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/roadmap \
  -H "Authorization: Bearer dev-admin-key"
```

The JSON endpoints read from SQLite.

The config check warns about demo keys and missing production settings.

The production readiness report explains go-live gaps in plain English.

The incident playbook explains common failure scenarios, operator steps, and customer-safe wording.

The support policy explains prototype, pilot, and production support expectations.

The pilot checklist explains what to prepare before, during, and after a customer trial.

The executive brief explains the business story for non-technical stakeholders.

The roadmap explains the path from prototype to pilot, production hardening, and multi-provider expansion.

In this prototype, admin endpoints use the demo admin key `dev-admin-key`.

In production, change the admin key with `GATEWAY_ADMIN_API_KEY`.

The default database path is:

```text
data/aismallrouter.db
```

## Test Invalid API Key

```bash
curl http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer wrong-key"
```

## Test Usage Limit

Start the gateway with:

```bash
python3 model_gateway.py --mock --request-limit 1 --limit-window-seconds 60
```

Then call `/v1/models` twice with the same API key.

The second request should return `429 Too Many Requests`.

## Test Token Budget

`demo-budget-key` has a very small token budget.

The first chat request should pass.

The next request should return `402` after the stored usage reaches the budget.

```bash
curl -i http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer demo-budget-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart-fast",
    "messages": [
      {
        "role": "user",
        "content": "Use some demo tokens."
      }
    ],
    "stream": false
  }'
```

Fetch customer budget status:

```bash
curl http://127.0.0.1:8787/v1/gateway/status \
  -H "Authorization: Bearer dev-admin-key"
```

## Start In Live Qwen Mode

Live mode calls Alibaba Cloud Model Studio / DashScope.

Use this only when you want to spend real provider credits.

```bash
export DASHSCOPE_API_KEY="your-model-studio-api-key"
python3 model_gateway.py
```

Then call:

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer dev-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart-fast",
    "messages": [
      {
        "role": "user",
        "content": "Say hello from Qwen through the gateway."
      }
    ],
    "stream": false
  }'
```

## Bring Your Own Key Example

In `customer_keys.json`, a customer can map a provider to an environment variable.

```json
{
  "id": "dev",
  "name": "Development Customer",
  "plan": "internal-demo",
  "api_key": "dev-gateway-key",
  "token_budget": 50000,
  "cost_budget": 5.0,
  "provider_api_keys": {
    "dashscope": "env:DASHSCOPE_API_KEY"
  },
  "allowed_models": ["*"],
  "enabled": true
}
```

The customer still calls the gateway with the gateway key.

The gateway uses the provider key only when it calls the provider.

## Enable More Providers Later

`model_registry.json` already contains disabled examples for OpenAI and Anthropic.

To test them later:

- Add a provider API key as an environment variable.
- Enable the provider.
- Enable a model that points to that provider.
- Test in mock mode first.
- Test in live mode only when you are ready to spend provider credits.
