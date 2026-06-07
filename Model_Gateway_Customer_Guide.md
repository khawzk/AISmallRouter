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
- Postman collection
- Demo bundle manifest
- Customer integration guide
- Follow-up email pack
- Pilot kickoff pack
- Pilot review pack
- Production transition pack
- Operating review pack
- Provider expansion pack
- Customer expansion pack
- Operational alerts
- Incident playbook
- Support policy
- Pilot checklist
- Executive brief
- Roadmap
- Decision guide
- FAQ
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
- Provider contract matrix
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
- Procurement review pack for customer internal approval
- Business case pack for pilot value and ROI discussion
- Implementation plan for delivery phases and owners
- Alternatives pack for build, buy, managed gateway, and OpenRouter-like comparison
- SOW draft for scope, deliverables, exclusions, and acceptance criteria
- Workshop agenda for the first customer meeting
- Decision log for workshop decisions and next actions
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

The prototype also includes `/postman_collection.json`.

This is a ready-to-import Postman collection.

It includes demo variables for:

- base URL
- customer gateway key
- admin key

This helps a customer technical team click through the main demo requests faster.

The prototype also includes `/v1/gateway/demo-bundle`.

This is a simple demo manifest.

It lists:

- dashboard URL
- customer self view
- OpenAPI contract
- Postman collection
- customer guide PDF
- demo script
- customer handoff package
- recommended demo order
- safe curl examples
- production notes

This helps a business team understand the story first.

Then the technical team can use the API contract and Postman collection.

## What The Handoff Checklist Shows

The prototype also includes `/v1/gateway/handoff-checklist`.

This is an admin-only checklist for customer meetings.

It explains:

- what can be shared with the customer
- what should stay internal or admin-only
- which materials support the business story
- which materials support the technical handoff
- what to check before the customer meeting
- what to do after the customer meeting

The main rule is simple:

- customers use gateway API keys
- provider API keys stay private
- admin-only links stay internal unless the team approves sharing them

This helps a non-technical team present the project safely and clearly.

The prototype also includes `/v1/gateway/integration-guide`.

This is a customer-facing integration guide.

A customer calls it with their own gateway key.

It returns:

- allowed model names
- recommended first model
- curl examples
- Python example
- JavaScript example
- streaming example
- go-live checklist
- support questions

The dashboard also shows an Integration command starter with local curl commands.

It uses placeholders for secrets.

It does not expose provider API keys.

## What The SDK Starter Shows

The prototype also includes `/v1/gateway/sdk-starter`.

A customer calls it with their own gateway key.

It returns:

- `.env.example`
- Python starter file
- JavaScript starter file
- first-run commands
- common error codes and fixes
- customer handoff checklist

It uses `YOUR_GATEWAY_API_KEY`.

It does not expose the real customer key or provider API keys.

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

- Layered architecture map
- Gateway options comparison
- Production readiness cards
- Customer handoff package
- Integration command starter
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
- Commercial policy
- Invoice preview
- Request activity feed
- Request detail lookup
- Usage by customer
- Usage by model
- Request summary by customer, model, provider, and error code
- Config check for demo keys and missing production settings
- Production readiness report
- Security review and go-live security gates
- Operations runbook and SLO-style watch plan
- Model evaluation plan and quality scorecards
- Customer migration plan and rollback checklist

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

## What The Incident Playbook Shows

The prototype includes `/v1/gateway/incident-playbook`.

This endpoint explains what to do when something goes wrong.

It includes common scenarios:

- Provider cannot serve traffic
- Recent customer requests are failing
- Customer is blocked by budget or limit
- Demo key or secret handling risk
- New provider is not ready for customer traffic

Each scenario includes:

- trigger
- signals to check
- current evidence
- operator steps
- customer-safe wording

It is not a legal SLA.

It helps sales, support, and technical teams explain the same problem in the same way.

## What The Support Policy Shows

The prototype includes `/v1/gateway/support-policy`.

This endpoint explains support expectations.

It separates support into:

- prototype demo
- technical pilot
- production target

It also explains:

- P1, P2, and P3 severity
- first checks for each severity
- escalation path
- customer-safe wording
- what is not included yet

It is not a legal production SLA.

It helps the customer understand what can be promised now and what needs a real production contract later.

## What The Pilot Checklist Shows

The prototype includes `/v1/gateway/pilot-checklist`.

This endpoint helps prepare a small customer trial.

It explains:

- recommended pilot scope
- business, technical, customer, and support roles
- before pilot checks
- during pilot checks
- after pilot review
- success criteria
- stop, extend, or productionize decision

It helps keep the pilot small, honest, and measurable.

## What The Pilot Scorecard Shows

The prototype includes `/v1/gateway/pilot-scorecard`.

This endpoint helps decide whether a customer pilot should stay in discovery, continue testing, or move toward production hardening.

It scores:

- first request completed
- request tracing available
- error rate acceptable
- budget still usable
- model access configured
- production gaps acknowledged

The result includes a score, decision, weak criteria, metrics, and next action for each customer.

## What The Onboarding Plan Shows

The prototype includes `/v1/gateway/onboarding-plan`.

This endpoint turns the demo into a simple customer pilot path.

It explains:

- Day 0 customer alignment
- Day 1 safe access setup
- Day 2 mock technical test
- Day 3 route and provider review
- Day 4 usage, cost, and support review
- Day 5 pilot decision

Each step has:

- owner
- actions
- evidence
- exit check

It helps a non-technical customer understand what happens after the demo.

## What The Executive Brief Shows

The prototype includes `/v1/gateway/executive-brief`.

This endpoint is for non-technical stakeholders.

It explains:

- one-sentence value
- why the gateway matters
- what the demo proves
- what is not production-ready yet
- recommended customer story
- pilot recommendation
- support and readiness position
- next step

This helps a business customer understand the direction before reading technical details.

## What The Roadmap Shows

The prototype includes `/v1/gateway/roadmap`.

This endpoint explains the path from prototype to production.

It includes:

- prototype explanation
- technical pilot
- production hardening
- multi-provider expansion

Each phase includes:

- goal
- deliverables
- exit criteria
- main risks

This helps a customer understand the next steps without jumping too quickly into production promises.

## What The Decision Guide Shows

The prototype includes `/v1/gateway/decision-guide`.

This endpoint helps answer an early customer question:

Should we use an API Gateway, a managed AI Gateway, a custom Model Gateway, or an OpenRouter-like platform?

It compares:

- normal API Gateway
- managed AI Gateway
- custom Model Gateway
- OpenRouter-like marketplace

It explains what each option is good at, what it does not solve, and when to choose it.

The dashboard also renders this as Gateway options comparison cards.

## What The FAQ Shows

The prototype includes `/v1/gateway/faq`.

This endpoint answers common customer questions.

Examples:

- Is this just an API Gateway?
- Why start with Qwen?
- Is this an OpenRouter clone?
- Will customer prompts or provider keys be exposed?
- How do we control cost?
- What happens if a provider fails?
- Is this ready for production?

It helps sales, support, and technical teams answer in the same simple language.

## What The Demo Script Shows

The prototype includes `/v1/gateway/demo-script`.

This endpoint gives a 15 minute customer walkthrough.

The dashboard also shows this as Presenter mode.

It tells the presenter:

- what to say first
- which endpoint to show
- how to explain routing
- how to answer common objections
- how to close with a small pilot

This helps a non-technical customer understand the idea without reading code.

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

## What Provider Contracts Show

The prototype includes `/v1/gateway/provider-contracts`.

This endpoint explains provider differences.

It compares:

- OpenAI-compatible providers
- Alibaba Cloud Model Studio compatible mode
- Anthropic Claude
- planned Xiaomi or other local model providers

It shows:

- auth method
- endpoint path
- request shape
- response shape
- streaming behavior
- tool calling behavior
- usage field behavior
- adapter status
- remaining gaps

This is why an AI Model Gateway is more than a normal API Gateway.

A normal API Gateway can forward HTTP.

An AI Model Gateway must also normalize model names, request shape, response shape, streaming, tool calls, usage, errors, fallback, and customer policy.

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

The dashboard also has a Provider lifecycle panel for create, update, and disable demos.

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

The dashboard also has a Model route lifecycle panel for create, update, and disable demos.

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

The dashboard also has a Customer lifecycle panel for preview, create, rotate, and disable demos.

This is not full enterprise IAM.

Production should use:

- database storage
- audit logs
- secret manager
- approval workflow
- customer admin UI

## What Audit Events Show

The prototype includes `/v1/gateway/audit-events`.

It records admin lifecycle actions.

For example:

- customer created
- customer key rotated
- customer disabled
- provider created, updated, or disabled
- model route created, updated, or disabled

Each event shows:

- actor
- action
- target customer, provider, or model route
- time
- safe details

The audit event can include masked keys and key hashes.

It does not store the full gateway API key.

The dashboard also has an Audit timeline section for recent changes.

This helps explain control history.

It is not a full compliance audit system.

## What The Commercial Policy Shows

The prototype includes `/v1/gateway/commercial-policy`.

This endpoint explains how to talk about pricing, budgets, invoice previews, and commercial boundaries before a real contract exists.

It covers:

- what can be shown now
- what needs contract approval later
- request limit behavior
- token budget behavior
- cost budget behavior
- invoice preview limits
- approval questions
- what is excluded from the prototype

This is not a legal quote, tax invoice, payment system, or audited billing ledger.

It is a simple commercial explanation for demos and pilots.

## What The Procurement Pack Shows

The prototype includes `/v1/gateway/procurement-pack`.

This endpoint helps a customer share the idea with procurement, legal, IT, security, and finance.

It explains:

- what this prototype is and is not
- which review tracks matter
- which documents are available now
- which endpoint can prove each point
- who needs to approve production
- which promises should wait for production approval
- which questions to send before a pilot or purchase meeting

This matters because many real customers cannot approve a pilot from a technical demo alone.

They often need a simple internal review pack.

The procurement pack does not replace a signed contract, legal security attestation, production SLA, final price quote, or tax invoice.

## What The Business Case Shows

The prototype includes `/v1/gateway/business-case`.

This endpoint helps a business sponsor explain why the gateway may be worth a pilot.

It shows:

- the simple value story
- value hypotheses to test
- pilot metrics from local usage records
- ROI inputs the customer must provide
- simple decision options
- what the prototype does not claim

The decision options are:

- stop after demo
- run a small pilot
- harden for production

This is not a formal ROI model.

It does not promise guaranteed savings, final production price, production SLA, security certification, or legal billing output.

It is a workshop tool for deciding whether the next step is worth doing.

## What The Implementation Plan Shows

The prototype includes `/v1/gateway/implementation-plan`.

This endpoint helps a customer understand what it would take to move from idea to pilot to production.

It shows:

- delivery phases
- rough duration ranges
- main work in each phase
- roles and owners
- estimate assumptions
- delivery risks
- acceptance evidence
- what should not be promised too early

The suggested path is:

- discovery and scope
- local prototype demo
- controlled pilot
- production hardening
- production launch

This is not a fixed delivery quote.

It does not promise a final price, guaranteed delivery date, production SLA, security certification, legal approval, or provider cost guarantee.

## What The Alternatives Pack Shows

The prototype includes `/v1/gateway/alternatives-pack`.

This endpoint helps a customer compare build, buy, managed gateway, and OpenRouter-like options.

It compares:

- direct provider integration
- normal API Gateway
- Alibaba Cloud AI Gateway
- Vercel AI Gateway
- OpenRouter-like platform
- custom AISmallRouter-style Model Gateway

It gives simple decision rules.

For example:

- use direct provider integration when there is only one provider
- use normal API Gateway when the main need is HTTP traffic control
- evaluate managed AI Gateway when the customer accepts a hosted platform
- evaluate OpenRouter-like options when broad model access is more important than private control
- build custom only when customer-owned controls, custom routing, private reports, or special rollout rules matter

Useful reference docs to review before a real customer recommendation:

- OpenRouter docs: `https://openrouter.ai/docs/faq`
- Vercel AI Gateway docs: `https://vercel.com/docs/ai-gateway/`
- Alibaba Cloud AI Gateway docs: `https://www.alibabacloud.com/help/en/api-gateway/ai-gateway/product-overview/what-is-an-ai-gateway`

This endpoint is not a live vendor benchmark, legal recommendation, final procurement decision, or promise to build a full OpenRouter clone.

## What The SOW Draft Shows

The prototype includes `/v1/gateway/sow-draft`.

This endpoint helps turn the gateway discussion into a clear project scope.

It shows:

- proposed scope
- deliverables
- out-of-scope items
- milestones
- acceptance criteria
- assumptions
- customer inputs needed
- risk controls
- open items before signature

This is useful because customer projects can become confusing when scope, legal terms, billing, and production promises are mixed together too early.

The SOW draft is only a starting point.

It is not a signed contract, final quote, tax invoice, production SLA, security certification, or legal approval.

## What The Workshop Agenda Shows

The prototype includes `/v1/gateway/workshop-agenda`.

This endpoint helps run the first customer meeting.

It shows:

- who should attend
- what to prepare before the meeting
- a simple meeting agenda
- questions to ask
- demo order
- expected meeting outputs
- follow-up actions
- red flags

This is useful because many AI gateway discussions are too broad at the start.

The workshop agenda helps turn the idea into one use case, one provider path, one pilot owner, and one next decision.

It is a meeting guide, not a contract, quote, SLA, or production approval.

## What The Decision Log Shows

The prototype includes `/v1/gateway/decision-log`.

This endpoint helps record what was decided after a customer workshop.

It shows:

- decision records
- owner for each decision
- evidence endpoints
- open questions
- next actions
- decision status meaning
- meeting note template

This is useful because customer decisions can disappear into chat messages or meeting memory.

The decision log keeps the next step clear.

It is a planning record, not a contract, legal approval, invoice, SLA, or security certification.

## What The Follow-up Email Shows

The prototype includes `/v1/gateway/follow-up-email`.

This endpoint helps write the customer email after a workshop.

It shows:

- subject options
- a customer email draft
- customer-safe recap points
- links to include
- decision summary
- next actions
- internal checklist
- things not to claim

This is useful because a customer may understand the demo but still need a clear next step.

The follow-up email turns the workshop into a simple recap and a request for the next decision.

It is a communication draft, not a signed contract, final quote, invoice, SLA, or security approval.

## What The Pilot Kickoff Shows

The prototype includes `/v1/gateway/pilot-kickoff`.

This endpoint helps start a controlled customer pilot.

It shows:

- kickoff goals
- required attendees
- pre-kickoff checklist
- meeting agenda
- technical start points
- success metrics
- operating rhythm
- risks to watch
- handoff actions after kickoff

This is useful because a pilot needs more than a demo.

The kickoff pack makes the pilot small, measurable, and easier to manage.

It is a pilot planning pack, not production approval, a signed SOW, a final SLA, or a security certification.

## What The Pilot Review Shows

The prototype includes `/v1/gateway/pilot-review`.

This endpoint helps review a pilot and decide the next step.

It shows:

- review inputs
- review meeting agenda
- stop / extend / production hardening options
- evidence summary
- customer report snapshot
- go/no-go checks
- recommended decision wording
- follow-up outputs
- things not to claim

This is useful because a pilot should end with a clear decision.

The pilot review pack helps the customer choose whether to stop, extend the pilot, or move toward production hardening.

It is a decision support pack, not production approval, a signed SOW, legal approval, a final quote, or a security certification.

## What The Production Transition Shows

The prototype includes `/v1/gateway/production-transition`.

This endpoint helps move from a successful pilot review into production hardening.

It shows:

- transition trigger
- hardening phases
- role handoff
- P0 controls before live traffic
- P1 items for limited production
- customer questions before SOW
- internal kickoff agenda
- customer-safe message
- things not to claim

This is useful because a pilot result is not the same as production approval.

The transition pack explains what must happen before real production traffic.

It is a transition plan, not production approval, a signed SOW, final price, legal approval, or security certification.

## What The Operating Review Shows

The prototype includes `/v1/gateway/operating-review`.

This endpoint helps run weekly or monthly reviews after the gateway is being tested or used.

It shows:

- review cadence
- meeting agenda
- operating signals
- customer snapshots
- expand / hold / fix / pause decision options
- owner follow-ups
- review outputs
- things not to claim

This is useful because a gateway needs ongoing ownership after launch or limited production.

The operating review pack helps the team decide whether to expand usage, hold steady, fix blockers, or pause.

It is an operating review guide, not an SLA report, legal audit, final invoice, or security certification.

## What The Provider Expansion Shows

The prototype includes `/v1/gateway/provider-expansion`.

This endpoint helps plan how to add more model providers.

It shows:

- expansion principles
- provider candidates
- approval gates
- test matrix
- expansion phases
- customer questions
- current provider context
- things not to claim

This is useful when a customer asks for OpenAI, Claude, Xiaomi, OpenRouter-like routing, or broad model coverage.

The provider expansion pack keeps the answer honest: add one provider at a time, test the contract, approve data and cost rules, then expose it to customers.

It is an expansion planning pack, not a live vendor benchmark, procurement approval, provider certification, final price, or full OpenRouter clone promise.

## What The Customer Expansion Shows

The prototype includes `/v1/gateway/customer-expansion`.

This endpoint helps expand one customer from a small pilot to more teams, workflows, models, providers, or traffic.

It shows:

- expansion principles
- expansion stages
- control gates
- expansion decisions
- customer snapshot
- access snapshot
- customer questions
- things not to claim

This is useful because one successful pilot should not automatically become unlimited usage.

The customer expansion plan keeps growth controlled with clear owners, budgets, access rules, data rules, support path, and review dates.

It is an expansion plan, not a signed contract, unlimited usage approval, production SLA, final price, or security certification.

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

## What Customer Success Summary Shows

The prototype includes `/v1/gateway/customer-success`.

This endpoint turns customer usage data into account health.

It shows:

- healthy, watch, and at-risk customer counts
- health status per customer
- risk reasons
- recommended follow-up action
- simple business metrics
- meeting questions
- evidence endpoints for support

This helps sales, support, and customer success teams decide who needs attention before the next customer call.

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

## Why Production Readiness Matters

The prototype includes `/v1/gateway/production-readiness`.

This is a management-friendly report.

It explains what is demo-ready and what still needs work.

The dashboard also shows this as Production readiness cards.

It groups the work into:

- Security
- Provider readiness
- Customer controls
- Routing and fallback
- Observability
- Billing
- Documentation and handoff

Each area has:

- status
- plain English explanation
- evidence
- next step

This is not a full security audit.

It helps a customer understand why a prototype is not the same as a production platform.

## What The Deployment Readiness Guide Shows

The prototype includes `/v1/gateway/deployment-readiness`.

This endpoint explains how to move from local demo to a controlled pilot or production deployment.

It covers:

- local demo stage
- live Qwen test stage
- technical pilot stage
- production target stage
- required environment variables and files
- preflight checks
- operational health checks
- rollback plan
- deployment options

This is not a one-command production deploy.

It is a plain-English guide for the customer technical team to understand what must be configured, secured, monitored, and approved before real traffic.

## What The Customer Migration Plan Shows

The prototype includes `/v1/gateway/migration-plan`.

This endpoint explains how a customer can move from direct model provider calls to one gateway API without switching everything at once.

It covers:

- current-state discovery
- shadow gateway setup
- mock and evaluation testing
- limited live pilot
- gradual cutover
- production decision
- cutover checklist
- rollback plan
- evidence endpoints

This is not a one-click migration tool.

It is a simple cutover plan for customer conversations.

The main message is simple:

Start with one customer, one workflow, one or two public model names, mock mode first, then controlled live traffic. Keep the old provider path available until rollback is tested.

## What The Production Backlog Shows

The prototype includes `/v1/gateway/production-backlog`.

This endpoint turns prototype gaps into prioritized engineering tasks.

It groups work into:

- P0 production blockers
- P1 pilot and limited-production hardening
- P2 scale and marketplace expansion

It covers:

- secrets
- storage
- data governance
- operations
- change control
- provider contracts
- billing
- tenant controls
- marketplace planning
- deployment automation

This is not an automatic project plan.

It is a simple backlog that helps business, technical, security, platform, and support owners decide what must be funded before a production commitment.

## What The Launch Plan Shows

The prototype includes `/v1/gateway/launch-plan`.

This endpoint converts readiness gaps into go-live gates.

It explains:

- security and secrets gate
- provider live readiness gate
- customer access and budget gate
- routing and fallback gate
- support and observability gate
- billing and commercial rules gate
- customer handoff gate

Each gate has:

- owner
- required evidence
- current evidence
- approval question
- status
- next step

It also shows required signoffs and rollout stages.

This helps a customer understand what must be approved before production traffic.

## What The Change Management Plan Shows

The prototype includes `/v1/gateway/change-management`.

This endpoint explains how to change customers, providers, or model routes without surprising a customer.

It includes:

- change types
- risk level
- approval owner
- before-change checklist
- after-change validation
- rollback path
- evidence endpoints

This is not automatic rollback yet.

It is a simple operating plan for safer demos, pilots, and production discussions.

## What The Data Governance Review Shows

The prototype includes `/v1/gateway/data-governance`.

This endpoint explains what happens to prompt data, logs, customer gateway keys, and provider secrets.

It is written for business, security, customer data, and gateway owners.

It covers:

- prompt and response handling
- provider secret handling
- customer gateway key handling
- logging and retention
- sensitive data preview
- customer visibility
- production gaps

This is not a compliance certification.

It is a simple checklist for customer trust discussions before real production traffic.

The main message is simple:

Before production, the customer and gateway owner must agree how prompts are logged, how long logs are kept, who can see request details, how secrets are rotated, and how customer data can be deleted.

## What The Security Review Shows

The prototype includes `/v1/gateway/security-review`.

This endpoint explains the main security questions a customer will ask before trusting one gateway with many AI providers.

It is written for business, customer security, gateway, and support owners.

It covers:

- customer gateway key risk
- provider secret risk
- sensitive prompt data in logs
- wrong customer or model access
- unsafe admin changes
- current prototype controls
- production controls still needed
- go-live security gates

This is not a penetration test, SOC 2 report, legal compliance review, or production approval.

It is a simple threat model for customer conversations.

The main message is simple:

The prototype is good for local explanation and mock demos. Before production, the team still needs real key storage, secret manager, role-based admin access, log retention policy, tenant isolation tests, and security review.

## What The Operations Runbook Shows

The prototype includes `/v1/gateway/operations-runbook`.

This endpoint explains what to watch after the gateway is used by a customer, who should act, and what evidence to collect before changing routes or promises.

It is written for support, gateway, platform, and business owners.

It covers:

- SLO-style targets
- daily checks
- alert actions
- support, gateway, platform, and business owners
- current signals
- evidence endpoints

This is not a legal SLA or full monitoring system.

It is a simple operations runbook for customer pilots.

The main message is simple:

Before production, the team still needs real monitoring, alert routing, on-call ownership, retention policy, and legal SLA terms.

## What The Model Evaluation Plan Shows

The prototype includes `/v1/gateway/evaluation-plan`.

This endpoint explains how to compare models before routing real customer traffic.

It is written for business, customer technical, data, support, and gateway owners.

It covers:

- task quality
- reliability
- latency
- cost
- safety and data handling
- fallback behavior
- sample evaluation prompts
- model scorecards
- evidence endpoints

This is not a full benchmark platform yet.

It is a simple quality plan for customer conversations.

The main message is simple:

Do not promise "best model" routing until the team has tested real customer prompts, reviewed quality with a human owner, checked cost and latency, and confirmed safety and fallback behavior.

## What The Discovery Checklist Shows

The prototype includes `/v1/gateway/discovery-checklist`.

Use this before promising an OpenRouter-like gateway.

It helps turn a broad customer idea into clear scope.

It covers:

- customer goal
- model and provider scope
- customer access and tenant rules
- data and governance
- commercial and reporting needs
- production expectation

It also shows:

- questions to ask
- evidence to collect
- red flags
- fit assessment
- recommended first pilot

The simple message is:

Do not start with every provider and every feature.

Start with one customer, one use case, one provider, one or two public model names, mock mode first, and clear success evidence.

## What The Proposal Summary Shows

The prototype includes `/v1/gateway/proposal-summary`.

Use this after the discovery checklist.

It gives a simple customer-facing scope summary.

It explains:

- customer problem
- recommended positioning
- phase one scope
- what is not in phase one
- customer deliverables
- decision points
- main risks
- recommended next steps

This is not a legal quote or final contract.

It is a plain-English scope note that helps the customer understand what the first pilot should include and what should wait until later.

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
- Customer onboarding plan
- Data governance review
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
