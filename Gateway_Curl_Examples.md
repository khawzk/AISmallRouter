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

The test covers auth, model listing, routing, fallback, model access, budgets, SQLite logs, provider health, customer reports, and admin summary endpoints.

Open the visual dashboard:

```text
http://127.0.0.1:8787/
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

## Check Provider Health

```bash
curl http://127.0.0.1:8787/v1/gateway/provider-health \
  -H "Authorization: Bearer dev-admin-key"
```

## Check Customer Reports

```bash
curl http://127.0.0.1:8787/v1/gateway/customer-reports \
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

curl http://127.0.0.1:8787/v1/gateway/customer-usage \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/model-usage \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/request-summary \
  -H "Authorization: Bearer dev-admin-key"

curl http://127.0.0.1:8787/v1/gateway/config-check \
  -H "Authorization: Bearer dev-admin-key"
```

The JSON endpoints read from SQLite.

The config check warns about demo keys and missing production settings.

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
