from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


OUTPUT = "Model_Gateway_Customer_Guide.pdf"


def on_page(canvas, doc):
    canvas.saveState()
    width, height = letter
    canvas.setFillColor(colors.white)
    canvas.rect(0, 0, width, height, stroke=0, fill=1)
    canvas.setStrokeColor(colors.HexColor("#D8DEE8"))
    canvas.setLineWidth(0.7)
    canvas.line(0.72 * inch, height - 0.62 * inch, width - 0.72 * inch, height - 0.62 * inch)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#637083"))
    canvas.drawString(0.72 * inch, height - 0.48 * inch, "AI Model Gateway Customer Guide")
    canvas.drawRightString(width - 0.72 * inch, 0.48 * inch, f"Page {doc.page}")
    canvas.restoreState()


def table_style(header=True):
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D8DEE8")),
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#C8D1DD")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("FONTSIZE", (0, 0), (-1, -1), 8.8),
        ("LEADING", (0, 0), (-1, -1), 11),
    ]
    if header:
        commands.extend(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF2FF")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#173C66")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ]
        )
    return TableStyle(commands)


def bullet(text, styles):
    return Paragraph(f"&bull; {text}", styles["BulletCustom"])


def build():
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="TitleCustom",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=24,
            leading=28,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#132B45"),
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SubtitleCustom",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=11,
            leading=15,
            textColor=colors.HexColor("#475569"),
            spaceAfter=14,
        )
    )
    styles.add(
        ParagraphStyle(
            name="BodyCustom",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=9.6,
            leading=13.8,
            textColor=colors.HexColor("#17212F"),
            spaceAfter=7,
        )
    )
    styles.add(
        ParagraphStyle(
            name="H1Custom",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=14.2,
            leading=17,
            textColor=colors.HexColor("#173C66"),
            spaceBefore=12,
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="H2Custom",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            textColor=colors.HexColor("#243B53"),
            spaceBefore=8,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="BulletCustom",
            parent=styles["BodyCustom"],
            leftIndent=12,
            firstLineIndent=-8,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="CodeCustom",
            parent=styles["BodyCustom"],
            fontName="Courier",
            fontSize=8,
            leading=10.5,
            backColor=colors.HexColor("#F4F7FA"),
            borderPadding=6,
            spaceBefore=4,
            spaceAfter=8,
        )
    )

    doc = BaseDocTemplate(
        OUTPUT,
        pagesize=letter,
        leftMargin=0.72 * inch,
        rightMargin=0.72 * inch,
        topMargin=0.82 * inch,
        bottomMargin=0.68 * inch,
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="default", frames=[frame], onPage=on_page)])

    story = []
    story.append(Paragraph("AI Model Gateway Customer Guide", styles["TitleCustom"]))
    story.append(
        Paragraph(
            "A simple reference for customers who want a multi-model AI platform direction, but may not have a large technical team yet.",
            styles["SubtitleCustom"],
        )
    )

    meta = Table(
        [
            ["Audience", "Business and product teams with basic technical awareness"],
            ["Prototype", "Local AI Model Gateway with Qwen / DashScope first"],
            ["Main idea", "One customer API, many model providers behind the scenes"],
            ["Demo mode", "Mock mode first, live Qwen mode only when needed"],
        ],
        colWidths=[1.35 * inch, 5.1 * inch],
    )
    meta.setStyle(table_style(header=False))
    story.append(meta)
    story.append(Spacer(1, 10))

    story.append(Paragraph("Executive Summary", styles["H1Custom"]))
    story.append(
        Paragraph(
            "An AI Model Gateway lets customers call one simple API while the platform manages model routing, provider differences, usage control, and future fallback logic. The first prototype uses Alibaba Cloud Model Studio / Qwen because it is available now and keeps testing cost low.",
            styles["BodyCustom"],
        )
    )
    for item in [
        "Start with Qwen and mock mode before adding more paid providers.",
        "Use an OpenAI-compatible API so customers can understand the integration quickly.",
        "Show routing visually: smart-fast is a customer-facing name that maps to qwen-plus.",
        "Keep enterprise API Gateway features optional until customer demand is clear.",
    ]:
        story.append(bullet(item, styles))

    story.append(Paragraph("Layered Architecture", styles["H1Custom"]))
    layers = Table(
        [
            ["Layer", "What It Means", "Example"],
            ["1. Customer Systems", "The customer's app, backend, or agent", "Website, chatbot, internal tool"],
            ["2. One Simple API", "One standard API format for customers", "/v1/chat/completions"],
            ["3. Gateway Entry Control", "Controls who can call and how much they can use", "API keys, limits, logs"],
            ["4. Model Control", "Chooses the public model and route", "smart-fast -> qwen-plus"],
            ["5. Provider Adapters", "Translates request and response formats", "DashScope adapter"],
            ["6. Model Providers", "The real upstream AI model provider", "Alibaba Cloud Qwen"],
        ],
        colWidths=[1.45 * inch, 3.0 * inch, 2.0 * inch],
    )
    layers.setStyle(table_style())
    story.append(layers)

    story.append(Paragraph("How Route Simulation Works", styles["H1Custom"]))
    story.append(
        Paragraph(
            "The dashboard explains routing without spending money. In mock mode, the gateway reads the requested model, checks the registry, maps the public model to a real upstream model, and returns a simulated response with a route trace.",
            styles["BodyCustom"],
        )
    )
    story.append(
        Paragraph(
            "Customer request: model = smart-fast<br/>Registry lookup: smart-fast maps to qwen-plus<br/>Provider adapter: prepare DashScope-compatible request<br/>Mock response: show the route trace without calling Alibaba Cloud",
            styles["CodeCustom"],
        )
    )

    story.append(Paragraph("What The Current Prototype Includes", styles["H1Custom"]))
    for item in [
        "Visual dashboard at the gateway root URL.",
        "OpenAI-compatible /v1/chat/completions endpoint.",
        "Customer API key authentication with Bearer token.",
        "Basic in-memory request limit.",
        "Model registry in model_registry.json.",
        "SQLite request and usage records for restart-safe demo data.",
        "Provider, customer, model, and request summary endpoints.",
        "Config check endpoint for demo keys and missing production settings.",
        "Request-level fallback controls for routing demos.",
        "Mock OpenAI-style tool call response for agent demos.",
        "Simple customer plans with token and cost budgets.",
        "Bring Your Own Key mapping through environment variables.",
        "Provider adapter scaffolds for OpenAI-compatible APIs and Claude-style APIs.",
        "Automated mock regression test that does not spend provider credits.",
        "Mock mode for demos and planning.",
        "Live Qwen mode when DASHSCOPE_API_KEY is available.",
    ]:
        story.append(bullet(item, styles))

    story.append(PageBreak())
    story.append(Paragraph("Main Technical Difficulties", styles["H1Custom"]))
    difficulties = Table(
        [
            ["Difficulty", "Why It Matters", "Prototype Status"],
            ["Provider format differences", "Each provider may use different request and response details.", "Handled first for DashScope-compatible chat"],
            ["Streaming", "Chat UIs and agents often need incremental tokens.", "Mock streaming included; live provider differences still need work"],
            ["Tool calling", "OpenAI, Claude, and Qwen may differ in tool format.", "Mock tool_calls plus basic normalization"],
            ["Token usage", "Billing and quota require reliable usage data.", "Stored in SQLite"],
            ["Fallback", "Retrying another model needs clear business rules.", "Registry fallback plus request-level controls"],
            ["Customer key management", "Each customer needs limits, logs, and access control.", "Minimum version plus BYOK mapping"],
            ["Cost control", "Different models have different prices and limits.", "Simple token and cost budgets included"],
        ],
        colWidths=[1.55 * inch, 3.15 * inch, 1.75 * inch],
    )
    difficulties.setStyle(table_style())
    story.append(difficulties)

    story.append(Paragraph("Why The Test Script Matters", styles["H1Custom"]))
    story.append(
        Paragraph(
            "The repository includes test_gateway.py. It starts the gateway in mock mode and checks API keys, model listing, route tracing, fallback, model access rules, token budget blocking, SQLite logs, and status output without leaking provider keys.",
            styles["BodyCustom"],
        )
    )

    story.append(Paragraph("Admin Summary Endpoints", styles["H1Custom"]))
    story.append(
        Paragraph(
            "The prototype exposes local admin JSON views for provider status, usage by customer, usage by model, and request summaries. These make the control layer easier to explain. The local demo uses a separate admin key. In production, this key should be changed and protected.",
            styles["BodyCustom"],
        )
    )

    story.append(Paragraph("Config Check", styles["H1Custom"]))
    story.append(
        Paragraph(
            "The prototype includes /v1/gateway/config-check. It warns about demo admin keys, demo customer keys, missing live provider keys, and direct secret values in local JSON files. This is not a full security audit, but it is a useful readiness checklist for customer demos.",
            styles["BodyCustom"],
        )
    )

    story.append(Paragraph("Recommended Roadmap", styles["H1Custom"]))
    roadmap = Table(
        [
            ["Phase", "Goal", "Result"],
            ["1. Current prototype", "Qwen-only gateway with mock dashboard", "Customer can understand the concept"],
            ["2. Live Qwen demo", "Use Model Studio API key for real chat", "Validate latency and response quality"],
            ["3. Gateway controls", "Persist logs, usage, model access rules, budgets, and BYOK mapping", "Better customer management"],
            ["4. More providers", "Enable OpenAI, Claude, Xiaomi adapters", "More complete OpenRouter-like direction"],
            ["5. Production", "Database, secrets, streaming, fallback, billing", "Enterprise-ready platform path"],
        ],
        colWidths=[1.45 * inch, 3.1 * inch, 1.9 * inch],
    )
    roadmap.setStyle(table_style())
    story.append(roadmap)

    story.append(Paragraph("Reference Documents", styles["H1Custom"]))
    refs = [
        "OpenRouter API Reference: https://openrouter.ai/docs/api/reference/overview/",
        "OpenRouter Authentication: https://openrouter.ai/docs/api-keys",
        "OpenRouter Limits: https://openrouter.ai/docs/api-reference/limits/",
        "OpenRouter Models API: https://openrouter.ai/docs/api/api-reference/models/get-models",
        "OpenRouter Model Fallbacks: https://openrouter.ai/docs/guides/routing/model-fallbacks",
        "OpenRouter Provider Routing: https://openrouter.ai/docs/guides/routing/provider-selection/",
        "OpenRouter BYOK: https://openrouter.ai/docs/use-cases/byok/",
        "Alibaba Cloud Model Studio DashScope API Reference: https://www.alibabacloud.com/help/doc-detail/3016809.html",
    ]
    for ref in refs:
        story.append(Paragraph(ref, styles["BodyCustom"]))

    story.append(Paragraph("Final Recommendation", styles["H1Custom"]))
    story.append(
        Paragraph(
            "Start small and explain clearly. Use the visual dashboard to show the customer-facing API, the routing layer, and the provider adapter. Use mock mode for discussion and live Qwen mode only when real model testing is needed. Add more providers only after the customer understands the value of the gateway.",
            styles["BodyCustom"],
        )
    )

    doc.build(story)


if __name__ == "__main__":
    build()
