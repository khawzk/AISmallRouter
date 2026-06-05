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

Open the visual dashboard:

```text
http://127.0.0.1:8787/
```

Optional: test a very small request limit.

```bash
python3 model_gateway.py --mock --request-limit 1 --limit-window-seconds 60
```

## List Models

```bash
curl http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer dev-gateway-key"
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
