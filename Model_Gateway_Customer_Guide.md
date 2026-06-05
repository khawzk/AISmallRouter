# AI Model Gateway Customer Guide

## Who This Guide Is For

This guide is for customers who have a clear AI product idea, but may not have a deep technical team yet.

It explains the direction, the prototype, the demo, and the main technical challenges in simple English.

## One-Sentence Summary

An AI Model Gateway gives customers one simple API while we manage different AI model providers behind the scenes.

## The Problem

Many companies want to use AI models from different providers.

Examples:

- Alibaba Cloud Model Studio / Qwen
- OpenAI
- Claude
- Xiaomi models
- Other private or cloud models

Each provider can have different API details.

This makes integration hard for customers.

## The Proposed Direction

Build a small model gateway first.

The gateway should provide one OpenAI-compatible API to the customer.

Behind that API, the gateway can route requests to different model providers.

```text
Customer App
  -> One Simple AI API
    -> Gateway Entry Control
    -> Model Control
    -> Provider Adapter
    -> Model Provider
```

## What The Prototype Shows

The current prototype only uses Alibaba Cloud Model Studio / Qwen.

This is intentional.

It lets us test the idea with the API key we already have, without spending money on many providers.

The prototype supports:

- Visual dashboard
- OpenAI-compatible chat endpoint
- Customer API key check
- Basic usage limit
- Request logs
- SQLite request and usage storage
- Provider, customer, model, and request summary endpoints
- Model registry
- Model routing alias
- Fallback routing test in mock mode
- Mock streaming
- Customer plans with token and cost budgets
- Bring Your Own Key provider mapping
- Provider adapter scaffolds for OpenAI and Claude
- Automated mock regression test
- Mock mode
- Live Qwen mode

## What Mock Mode Means

Mock mode does not call Alibaba Cloud.

It simulates the gateway flow.

This is useful for demos, customer explanation, and product planning.

Example:

```text
Customer asks for: smart-fast
Gateway maps it to: qwen-plus
Gateway returns: mock response with route trace
```

## How Routing Works In The Prototype

Routing is controlled by `model_registry.json`.

Example:

```text
Public model name: smart-fast
Provider: dashscope
Real upstream model: qwen-plus
```

This means the customer can call `smart-fast`.

The gateway decides that `smart-fast` should use Qwen `qwen-plus`.

Later, the same public model name could route to another provider if needed.

## Why This Is Not Just An API Gateway

A normal API Gateway is good for general HTTP control.

It can help with:

- Authentication
- Rate limits
- Logs
- Security rules
- Traffic control

But model gateways need extra logic.

They must understand:

- Model names
- Provider differences
- Request format
- Response format
- Streaming format
- Tool calling format
- Token usage
- Fallback behavior
- Cost and usage tracking

So the first version includes minimum API Gateway-like features, but the core product is the Model Gateway.

## Current Demo URL

Start the server:

```bash
python3 model_gateway.py --mock
```

Open:

```text
http://127.0.0.1:8787/
```

If port `8787` is busy, use another port:

```bash
python3 model_gateway.py --mock --port 8790
```

Then open:

```text
http://127.0.0.1:8790/
```

## What The Customer Should Notice In The Demo

The dashboard shows:

- One API for customers
- Gateway entry control
- Model registry
- Model router
- Provider adapter
- Real model provider
- Route simulation
- Available models
- A test chat request

The most important part is the route simulation.

It shows how `smart-fast` becomes `qwen-plus`.

## Why The Test Script Matters

The repository includes `test_gateway.py`.

It starts the gateway in mock mode and checks the important demo behavior.

It proves the main flow without spending model credits.

The test checks:

- API key rejection
- Model listing
- Chat route tracing
- SQLite usage records
- Fallback routing
- Customer model access
- Token budget blocking
- Status output without provider key leaks

## What The Admin Endpoints Show

The prototype has simple admin JSON endpoints.

They help explain the control layer:

- Provider status
- Usage by customer
- Usage by model
- Request summary by customer, model, provider, and error code
- Config check for demo keys and missing production settings

In the prototype, these endpoints use a separate demo admin key.

The local demo key is `dev-admin-key`.

In production, the admin key should be changed and protected.

## Why Config Check Matters

The prototype includes `/v1/gateway/config-check`.

It helps explain what must change before production.

Examples:

- Demo admin key is still being used.
- Demo customer keys are still being used.
- A provider key is missing in live mode.
- A provider secret appears directly in a local JSON file.

This is not a full security audit.

It is a simple readiness checklist for the demo gateway.

## Technical Challenges

### 1. Different Provider Formats

OpenAI, Claude, Qwen, and other providers may not use the same exact format.

The gateway needs adapters to translate between formats.

### 2. Streaming

Streaming is harder than normal responses.

Different providers may send stream chunks differently.

This matters for chat UIs and agent apps.

### 3. Tool Calling

Tool calling is not fully standard across providers.

The gateway must normalize tool requests and tool results.

The prototype can show an OpenAI-style mock `tool_calls` response.

It does not execute the tool.

Tool execution should happen in the customer app or agent runtime.

### 4. Token Usage

Some providers return token usage clearly.

Some providers return partial usage.

Some providers may not return usage in the same way.

This matters for cost tracking.

### 5. Fallback

Fallback means trying another model if the first one fails.

This sounds simple, but it needs rules.

The prototype supports request-level fallback controls.

A request can use the registry fallback list, disable fallback, or provide its own fallback model list.

Example questions:

- When should fallback happen?
- Which model should be next?
- Should we retry if the first answer is incomplete?
- Should fallback be allowed for expensive models?

### 6. Customer Key Management

If many customers use the gateway, we need to manage:

- API keys
- Usage limits
- Model access
- Request logs
- Cost limits
- Abuse protection

### 7. Cost Control

Different models have different prices.

The gateway should help route by cost, quality, speed, or customer plan.

The prototype now has simple customer budgets.

Simple meaning:

- Request limit protects the gateway from too many calls in a short time.
- Token budget controls total recorded token usage.
- Cost budget controls estimated total spend.
- Model access controls which public models a customer can use.

This is not full billing yet.

It is a demo of the control layer that full billing would need.

## Recommended Roadmap

### Phase 1: Current Prototype

- One local gateway service
- Qwen / DashScope only
- Mock mode
- Visual dashboard
- Basic API key and usage limit
- Basic token and cost budget control

### Phase 2: Real Qwen Demo

- Use Alibaba Cloud Model Studio API key
- Test real non-streaming chat
- Track latency and errors
- Keep cost low

### Phase 3: Better Gateway Controls

- Save request logs to a file and database
- Add per-customer usage records
- Add model access rules
- Add token and cost budgets
- Add a simple admin page

### Phase 4: More Providers

- Add OpenAI adapter
- Add Claude adapter
- Add provider-specific tool calling normalization
- Add customer-owned provider keys
- Add Xiaomi adapter
- Keep OpenAI-compatible customer API

### Phase 5: Production Direction

- Add real database
- Add secure secret management
- Add streaming
- Add fallback
- Add production billing and invoices
- Optional: put an enterprise API Gateway in front

## OpenRouter Concepts We Can Learn From

OpenRouter is a public example of a multi-model routing product.

Useful reference concepts:

- One API for many models
- OpenAI-compatible API style
- Model listing
- API keys
- Usage limits
- Provider routing
- Fallback models
- Bring your own provider key

We are not copying OpenRouter.

We are using similar public concepts to guide a smaller prototype.

## Reference Documents

- OpenRouter API Reference: https://openrouter.ai/docs/api/reference/overview/
- OpenRouter Authentication: https://openrouter.ai/docs/api-keys
- OpenRouter Limits: https://openrouter.ai/docs/api-reference/limits/
- OpenRouter Models API: https://openrouter.ai/docs/api/api-reference/models/get-models
- OpenRouter Model Fallbacks: https://openrouter.ai/docs/guides/routing/model-fallbacks
- OpenRouter Provider Routing: https://openrouter.ai/docs/guides/routing/provider-selection/
- OpenRouter BYOK: https://openrouter.ai/docs/use-cases/byok/
- Alibaba Cloud Model Studio DashScope API Reference: https://www.alibabacloud.com/help/doc-detail/3016809.html

## Final Recommendation

Start small.

Use Qwen first because we already have access.

Use mock mode for customer explanation.

Use live mode only when we need to test real model quality.

After the customer understands the value, add more providers and stronger gateway controls step by step.
