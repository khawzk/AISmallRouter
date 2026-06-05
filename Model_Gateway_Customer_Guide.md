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
- OpenAPI contract
- Operational alerts
- Access matrix
- OpenAI-compatible chat endpoint
- Customer API key check
- Basic usage limit
- Request logs
- SQLite request and usage storage
- Provider, customer, model, and request summary endpoints
- Model registry
- Model routing alias
- Model route create, update, and disable actions
- Structured route decision summary
- Fallback routing test in mock mode
- Mock streaming
- Customer plans with token and cost budgets
- Bring Your Own Key provider mapping
- Provider create, update, and disable actions
- Request-level provider allow-list
- Request-level routing strategy control
- Named policy presets
- Customer default policy
- Capability routing control
- Local safety preview
- Customer key issue preview
- Customer key create, rotate, and disable actions
- Audit events for customer key lifecycle actions
- Invoice preview with CSV export
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

## What The OpenAPI Contract Shows

The prototype includes `/openapi.json`.

This is an API contract.

It describes:

- customer API endpoints
- admin API endpoints
- customer bearer key
- admin bearer key
- request and response shape at a high level

This helps a customer technical team import the gateway into Postman, Swagger tools, or SDK generators.

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

- Operational alerts
- Access matrix
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
- Operational alerts
- Access matrix
- Provider health and readiness
- Provider lifecycle actions
- Model catalog and routing plan
- Model route lifecycle actions
- Route preview before a live request
- Route decision summary
- Capability routing control
- Routing strategy control
- Policy presets
- Customer default policy
- Safety preview before provider call
- Cost estimate before a live request
- Customer key issue preview
- Customer key lifecycle actions
- Audit events
- Customer self view
- Customer usage reports
- Invoice preview
- Request activity feed
- Request detail lookup
- Usage by customer
- Usage by model
- Request summary by customer, model, provider, and error code
- Config check for demo keys and missing production settings

In the prototype, these endpoints use a separate demo admin key.

The local demo key is `dev-admin-key`.

In production, the admin key should be changed and protected.

## What Customer Self View Shows

The prototype includes `/v1/gateway/me`.

This endpoint is for the customer, not the admin.

The customer uses their own gateway key.

It shows:

- customer name and plan
- allowed public models
- default routing policy, if set
- budget state
- usage by model
- usage by provider
- recent requests
- invoice preview for this customer

It does not show raw API keys.

It does not show provider API keys.

This is useful because a non-technical customer can ask:

> What can I use, and how much have I used?

## What Operational Alerts Show

The prototype includes `/v1/gateway/alerts`.

It combines several signals:

- Config check warnings
- Provider readiness
- Customer budget state
- Recent request errors

Each alert includes a simple next step.

This helps non-technical users understand what needs attention before a live demo.

## What The Access Matrix Shows

The prototype includes `/v1/gateway/access-matrix`.

It shows:

- Which customers exist
- Which public models exist
- Whether each customer can use each model
- Why access is allowed or blocked
- Customer budget state
- Provider readiness for that model

This helps explain customer-level API control.

For example, one customer can be allowed to use `smart-fast` only.

Another customer can be allowed to use all models.

## What Provider Health Means

The prototype includes `/v1/gateway/provider-health`.

It answers a simple question:

Can this provider serve traffic now?

The endpoint checks:

- Is the provider enabled?
- Does it have at least one routed model?
- Does live mode have a server key or customer BYOK key?
- Are recent requests failing often?
- What is the recent average latency?

In mock mode, a provider can be `ready_mock`.

That means the provider can be explained in the demo without spending credits.

It does not mean the live provider key is ready.

## What Provider Lifecycle Shows

The prototype includes three admin actions:

- `/v1/gateway/providers`
- `/v1/gateway/providers/update`
- `/v1/gateway/providers/disable`

These actions update `model_registry.json` in the local prototype.

They can show a simple provider lifecycle:

- Add a provider
- Set the provider type
- Set the base URL
- Set the API key environment variable name
- Update provider config
- Disable the provider when it should stop

Disabling a provider also disables active model routes that point to it.

Every provider change writes an audit event.

The prototype stores the environment variable name for the provider key.

It does not store the provider secret value.

Production should use a database, secret manager, approval workflow, readiness checks, and rollout controls.

## What The Model Catalog Shows

The prototype includes `/v1/gateway/model-catalog`.

It explains what each public model name means.

The catalog shows:

- Public model name
- Upstream provider model
- Provider status
- Fallback chain
- Capabilities
- Pricing metadata
- Requests, errors, tokens, and estimated cost

This is useful for non-technical customers.

They can see that `smart-fast` is a simple public name.

Behind it, the gateway can route to Qwen and fallback when needed.

## What Model Route Lifecycle Shows

The prototype includes three admin actions:

- `/v1/gateway/model-routes`
- `/v1/gateway/model-routes/update`
- `/v1/gateway/model-routes/disable`

These actions update `model_registry.json` in the local prototype.

They can show a simple model route lifecycle:

- Create a public model name
- Choose the provider
- Choose the upstream model
- Set fallback models
- Set capabilities
- Disable the route when it should stop

Every model route change writes an audit event.

This helps explain that the gateway manages model access, not only customer keys.

Production should add approval workflow, version history, rollback, and staged rollout.

## What Route Preview Shows

The prototype includes `/v1/gateway/route-preview`.

It is a dry run.

It does not call the model provider.

It shows:

- Can this customer use this model?
- Is the customer budget still available?
- Which model is the primary route?
- Which models are fallback routes?
- Which provider would handle the request?
- Is the provider ready?

This is useful before a customer demo.

You can explain the route without spending model credits.

## What Route Decision Summary Shows

Route preview and chat responses include `route_decision`.

It explains:

- What public model the customer requested
- What upstream model was selected
- Which provider was used
- Whether fallback was enabled
- Which provider and capability controls were applied
- Simple reasons for the routing decision

This is useful for support.

When a customer asks why a request used a certain route, the gateway can show the answer in plain language.

## What Provider Routing Control Shows

The prototype supports `gateway_allowed_providers`.

This lets a request say:

Only use these providers for this route.

For example, a route preview can allow only `dashscope`.

If the model route cannot use an allowed provider, the gateway returns a clear error.

This helps non-technical customers understand that the gateway is more than a normal API proxy.

It is also a control layer.

The customer can keep one model name.

The gateway can control which provider is allowed behind that model name.

## What Routing Strategy Shows

The prototype supports `gateway_route_strategy`.

This lets a request say:

Choose the route order using this strategy.

Supported strategies:

- `registry`
- `lowest_cost`
- `fastest`
- `healthiest`

`registry` uses the order in `model_registry.json`.

`lowest_cost` tries the lowest estimated price first.

`fastest` uses recent average latency.

`healthiest` uses provider health status.

The response includes `candidate_scores`.

This helps explain how a gateway can make routing decisions, not only store routes.

Production should use stronger cost data, latency windows, provider SLAs, and customer policy rules.

## What Policy Presets Show

The prototype supports `gateway_policy`.

A policy preset is a short name for common routing controls.

Current presets:

- `balanced`
- `lowest_cost`
- `fastest`
- `tool_ready`

For example:

`tool_ready` requires tool calling support and prefers healthy providers.

`lowest_cost` prefers the lowest estimated route cost.

The endpoint `/v1/gateway/policy-presets` lists the available presets.

If a request sends an explicit control, the explicit control wins.

This helps non-technical customers choose a simple policy name instead of many technical fields.

## What Customer Default Policy Shows

A customer can have a `default_policy`.

For example:

`default_policy = lowest_cost`

If the request does not send `gateway_policy`, the gateway uses the customer default.

If the request sends `gateway_policy`, the request value wins.

This helps customers keep simple applications.

They can use one gateway key and one default policy without sending routing controls every time.

## What Capability Routing Control Shows

The prototype supports `gateway_required_capabilities`.

This lets a route preview say:

Only use models that support these abilities.

For example:

- `stream=true` needs `streaming`
- `tools` needs `tools`

If no route supports the required ability, the gateway returns a clear error.

This helps customers understand why a gateway needs model metadata.

It is not only choosing a provider.

It is checking whether the provider route can do the job.

## What Cost Estimate Shows

The prototype includes `/v1/gateway/cost-estimate`.

It is also a dry run.

It does not call the model provider.

It estimates:

- Prompt tokens
- Completion tokens
- Estimated cost
- Budget left after the estimate

This helps explain budget planning.

The estimate is not exact.

Real provider token usage can be different.

## What Customer Key Issue Preview Shows

The prototype includes `/v1/gateway/key-issue-preview`.

It creates a safe customer onboarding package.

It does not save the customer.

It shows:

- A generated gateway API key
- A masked key for display
- A customer config snippet
- Allowed models
- Request and budget limits
- Next steps before live testing

This helps explain how a customer would be added to the gateway.

In production, this would connect to a real customer database and secret manager.

## What Customer Key Lifecycle Shows

The prototype includes three admin actions:

- `/v1/gateway/customers`
- `/v1/gateway/customers/rotate-key`
- `/v1/gateway/customers/disable`

These actions update `customer_keys.json` in the local prototype.

They can show a simple customer lifecycle:

- Create a customer
- Give the customer one gateway API key
- Rotate the key when needed
- Disable the customer when access should stop

After key rotation, the old key stops working.

After disable, the customer key stops working.

This is not full enterprise IAM.

Production should use:

- database storage
- audit logs
- secret manager
- approval workflow
- customer admin UI

## What Audit Events Show

The prototype includes `/v1/gateway/audit-events`.

It records customer key lifecycle actions.

For example:

- customer created
- customer key rotated
- customer disabled

Each event shows:

- actor
- action
- target customer
- time
- safe details

The audit event can include masked keys and key hashes.

It does not store the full gateway API key.

This helps explain control history.

It is not a full compliance audit system.

## What Invoice Preview Shows

The prototype includes `/v1/gateway/invoice-preview`.

It uses local usage records to create an estimated billing preview.

It shows:

- Requests
- Errors
- Prompt tokens
- Completion tokens
- Total tokens
- Estimated cost
- Remaining budget
- Usage by model
- Usage by provider

It can return JSON or CSV.

This is not a legal invoice.

It is a simple way to explain how billing reports could work later.

## What Safety Preview Shows

The prototype includes `/v1/gateway/safety-preview`.

It checks for obvious sensitive data before a provider call.

Examples:

- Email addresses
- Possible phone numbers
- Possible API keys
- Secret assignments such as `api_key=...`

Chat requests can also opt in.

`gateway_safety_check=true` returns the safety result in gateway metadata.

`gateway_block_sensitive=true` blocks the request when sensitive data is detected.

`gateway_redact_sensitive=true` replaces detected sensitive data before the provider call.

This is useful for explaining gateway guardrails.

It is not a full DLP or compliance system.

## What Customer Reports Show

The prototype includes `/v1/gateway/customer-reports`.

It shows one report per customer.

The report answers:

- How many requests did this customer send?
- How many errors happened?
- How many tokens were used?
- How much budget is left?
- Which models and providers were used?
- What were the recent requests?

This is important for business conversations.

A model gateway is not only a technical router.

It is also a control and reporting layer for customers.

## What Request Activity Shows

The prototype includes `/v1/gateway/request-activity`.

It shows recent gateway decisions.

It can filter by customer, model, provider, code, status, and limit.

This helps answer:

- Which customer sent the request?
- Which public model was requested?
- Which upstream model was used?
- Which provider handled it?
- Did it succeed or fail?
- How long did it take?

This is useful for support and customer explanation.

## What Request Detail Shows

The prototype returns a `gateway.request_id` for each successful chat request.

The admin can use `/v1/gateway/request-detail` to look up that request.

It shows:

- Which customer sent it
- Which model was requested
- Which upstream model was used
- Which provider handled it
- Whether it succeeded or failed
- Latency
- Usage record, if available

This helps support teams explain one customer issue clearly.

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
- Add request-level provider allow-list
- Add capability routing control
- Add local safety preview
- Add customer key issue preview
- Add invoice preview with CSV export

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
