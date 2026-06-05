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
- Model registry
- Model routing alias
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

### 4. Token Usage

Some providers return token usage clearly.

Some providers return partial usage.

Some providers may not return usage in the same way.

This matters for cost tracking.

### 5. Fallback

Fallback means trying another model if the first one fails.

This sounds simple, but it needs rules.

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

## Recommended Roadmap

### Phase 1: Current Prototype

- One local gateway service
- Qwen / DashScope only
- Mock mode
- Visual dashboard
- Basic API key and usage limit

### Phase 2: Real Qwen Demo

- Use Alibaba Cloud Model Studio API key
- Test real non-streaming chat
- Track latency and errors
- Keep cost low

### Phase 3: Better Gateway Controls

- Save request logs to a file or database
- Add per-customer usage records
- Add model access rules
- Add a simple admin page

### Phase 4: More Providers

- Add OpenAI adapter
- Add Claude adapter
- Add Xiaomi adapter
- Keep OpenAI-compatible customer API

### Phase 5: Production Direction

- Add real database
- Add secure secret management
- Add streaming
- Add fallback
- Add cost tracking
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

