#!/usr/bin/env python3
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from model_gateway import normalize_tools_for_anthropic


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request_json(base_url, method="GET", path="/", api_key=None, payload=None, headers=None):
    body = None
    headers = dict(headers or {})
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


def request_text(base_url, method="GET", path="/", api_key=None, payload=None, headers=None):
    body = None
    headers = dict(headers or {})
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
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8")
        finally:
            exc.close()
        return exc.code, text


class GatewayServer:
    def __init__(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.data_dir = os.path.join(self.temp_dir.name, "data")
        self.log_dir = os.path.join(self.temp_dir.name, "logs")
        self.customers_path = os.path.join(self.temp_dir.name, "customer_keys.json")
        self.registry_path = os.path.join(self.temp_dir.name, "model_registry.json")
        shutil.copyfile(os.path.join(ROOT_DIR, "customer_keys.json"), self.customers_path)
        shutil.copyfile(os.path.join(ROOT_DIR, "model_registry.json"), self.registry_path)
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
            "--customers",
            self.customers_path,
            "--registry",
            self.registry_path,
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

    def test_openapi_contract_lists_main_endpoints(self):
        status, spec = request_json(self.base_url, path="/openapi.json")
        self.assertEqual(status, 200)
        self.assertEqual(spec["openapi"], "3.0.3")
        self.assertEqual(spec["info"]["title"], "AISmallRouter Gateway API")
        self.assertIn("customerBearerAuth", spec["components"]["securitySchemes"])
        self.assertIn("adminBearerAuth", spec["components"]["securitySchemes"])
        for path in [
            "/v1/models",
            "/v1/chat/completions",
            "/v1/gateway/me",
            "/v1/gateway/integration-guide",
            "/v1/gateway/sdk-starter",
            "/v1/gateway/status",
            "/v1/gateway/policy-presets",
            "/v1/gateway/customers",
            "/v1/gateway/providers",
            "/v1/gateway/model-routes",
            "/v1/gateway/audit-events",
            "/v1/gateway/customer-success",
            "/v1/gateway/demo-bundle",
            "/v1/gateway/handoff-checklist",
            "/v1/gateway/production-readiness",
            "/v1/gateway/deployment-readiness",
            "/v1/gateway/production-backlog",
            "/v1/gateway/launch-plan",
            "/v1/gateway/change-management",
            "/v1/gateway/data-governance",
            "/v1/gateway/security-review",
            "/v1/gateway/provider-contracts",
            "/v1/gateway/evaluation-plan",
            "/v1/gateway/incident-playbook",
            "/v1/gateway/support-policy",
            "/v1/gateway/pilot-checklist",
            "/v1/gateway/pilot-scorecard",
            "/v1/gateway/discovery-checklist",
            "/v1/gateway/proposal-summary",
            "/v1/gateway/onboarding-plan",
            "/v1/gateway/executive-brief",
            "/v1/gateway/roadmap",
            "/v1/gateway/decision-guide",
            "/v1/gateway/faq",
            "/v1/gateway/demo-script",
        ]:
            self.assertIn(path, spec["paths"])
        self.assertEqual(
            spec["paths"]["/v1/chat/completions"]["post"]["security"],
            [{"customerBearerAuth": []}],
        )
        self.assertEqual(
            spec["paths"]["/v1/gateway/status"]["get"]["security"],
            [{"adminBearerAuth": []}],
        )

    def test_postman_collection_lists_demo_requests(self):
        status, collection = request_json(self.base_url, path="/postman_collection.json")
        self.assertEqual(status, 200)
        self.assertIn("AISmallRouter", collection["info"]["name"])
        variables = {item["key"]: item["value"] for item in collection["variable"]}
        self.assertIn("base_url", variables)
        self.assertEqual(variables["gateway_api_key"], "dev-gateway-key")
        self.assertEqual(variables["admin_api_key"], "dev-admin-key")
        folders = {folder["name"]: folder for folder in collection["item"]}
        self.assertIn("Customer API", folders)
        self.assertIn("Admin Control Plane", folders)
        customer_names = {item["name"] for item in folders["Customer API"]["item"]}
        admin_names = {item["name"] for item in folders["Admin Control Plane"]["item"]}
        self.assertIn("Chat Completion", customer_names)
        self.assertIn("Customer Integration Guide", customer_names)
        self.assertIn("Customer SDK Starter", customer_names)
        self.assertIn("Route Preview", admin_names)
        self.assertIn("Production Readiness", admin_names)
        self.assertIn("Deployment Readiness", admin_names)
        self.assertIn("Production Backlog", admin_names)
        self.assertIn("Provider Contracts", admin_names)
        self.assertIn("Evaluation Plan", admin_names)
        self.assertIn("Incident Playbook", admin_names)
        self.assertIn("Launch Plan", admin_names)
        self.assertIn("Change Management", admin_names)
        self.assertIn("Data Governance", admin_names)
        self.assertIn("Security Review", admin_names)
        self.assertIn("Customer Success", admin_names)
        self.assertIn("Support Policy", admin_names)
        self.assertIn("Pilot Checklist", admin_names)
        self.assertIn("Pilot Scorecard", admin_names)
        self.assertIn("Discovery Checklist", admin_names)
        self.assertIn("Proposal Summary", admin_names)
        self.assertIn("Onboarding Plan", admin_names)
        self.assertIn("Executive Brief", admin_names)
        self.assertIn("Roadmap", admin_names)
        self.assertIn("Decision Guide", admin_names)
        self.assertIn("FAQ", admin_names)
        self.assertIn("Demo Script", admin_names)
        self.assertIn("Handoff Checklist", admin_names)
        self.assertIn("Create Customer", admin_names)
        chat = next(item for item in folders["Customer API"]["item"] if item["name"] == "Chat Completion")
        self.assertEqual(chat["request"]["auth"]["bearer"][0]["value"], "{{gateway_api_key}}")
        self.assertIn("gateway_policy", chat["request"]["body"]["raw"])

    def test_demo_bundle_manifest_lists_handoff_materials(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/demo-bundle", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, bundle = request_json(self.base_url, path="/v1/gateway/demo-bundle", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(bundle["object"], "gateway.demo_bundle")
        self.assertEqual(bundle["demo_keys"]["customer_key"], "dev-gateway-key")
        entry_urls = {item.get("url") or item.get("path") for item in bundle["entry_points"]}
        self.assertIn(f"{self.base_url}/openapi.json", entry_urls)
        self.assertIn(f"{self.base_url}/postman_collection.json", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/sdk-starter", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/handoff-checklist", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/production-readiness", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/deployment-readiness", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/production-backlog", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/launch-plan", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/change-management", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/data-governance", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/security-review", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/customer-success", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/provider-contracts", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/evaluation-plan", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/incident-playbook", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/support-policy", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/pilot-checklist", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/pilot-scorecard", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/discovery-checklist", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/proposal-summary", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/onboarding-plan", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/executive-brief", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/roadmap", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/decision-guide", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/faq", entry_urls)
        self.assertIn(f"{self.base_url}/v1/gateway/demo-script", entry_urls)
        self.assertIn("Model_Gateway_Customer_Guide.pdf", entry_urls)
        self.assertGreaterEqual(len(bundle["recommended_demo_flow"]), 5)
        self.assertGreaterEqual(len(bundle["quick_commands"]), 4)
        self.assertIn("Replace demo keys before production.", bundle["production_notes"])
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(bundle))

    def test_handoff_checklist_groups_shareable_and_admin_materials(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/handoff-checklist", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, checklist = request_json(self.base_url, path="/v1/gateway/handoff-checklist", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(checklist["object"], "gateway.handoff_checklist")
        group_names = {group["group"] for group in checklist["handoff_groups"]}
        self.assertIn("Business overview", group_names)
        self.assertIn("Customer technical handoff", group_names)
        self.assertIn("Pilot decision package", group_names)
        self.assertIn("Internal readiness package", group_names)
        self.assertTrue(any(group["share_with_customer"] for group in checklist["handoff_groups"]))
        self.assertTrue(any(not group["share_with_customer"] for group in checklist["handoff_groups"]))
        technical_group = next(group for group in checklist["handoff_groups"] if group["group"] == "Customer technical handoff")
        internal_group = next(group for group in checklist["handoff_groups"] if group["group"] == "Internal readiness package")
        self.assertTrue(any(item["name"] == "OpenAPI contract" for item in technical_group["items"]))
        self.assertTrue(any(item["name"] == "Data governance" for item in internal_group["items"]))
        self.assertTrue(any("provider API keys" in rule for rule in checklist["handoff_rules"]))
        self.assertGreaterEqual(len(checklist["before_customer_meeting"]), 3)
        self.assertGreaterEqual(len(checklist["after_customer_meeting"]), 3)
        self.assertNotIn("sk-", json.dumps(checklist))
        self.assertNotIn("provider_api_keys", json.dumps(checklist))

    def test_provider_contracts_explain_adapter_differences(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/provider-contracts", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, contracts = request_json(self.base_url, path="/v1/gateway/provider-contracts", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(contracts["object"], "gateway.provider_contracts")
        configured_ids = {provider["id"] for provider in contracts["configured_providers"]}
        self.assertIn("dashscope", configured_ids)
        contract_types = {contract["type"] for contract in contracts["provider_type_contracts"]}
        self.assertIn("openai_compatible", contract_types)
        self.assertIn("anthropic", contract_types)
        self.assertIn("xiaomi_planned", contract_types)
        by_type = {contract["type"]: contract for contract in contracts["provider_type_contracts"]}
        self.assertEqual(by_type["openai_compatible"]["adapter_status"], "implemented")
        self.assertEqual(by_type["anthropic"]["adapter_status"], "scaffolded")
        self.assertEqual(by_type["xiaomi_planned"]["adapter_status"], "planned")
        self.assertTrue(any("normalizes model names" in line for line in contracts["plain_english"]))
        self.assertNotIn("sk-", json.dumps(contracts))

    def test_evaluation_plan_scores_models_before_routing_live_traffic(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/evaluation-plan", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, plan = request_json(self.base_url, path="/v1/gateway/evaluation-plan", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(plan["object"], "gateway.evaluation_plan")
        dimensions = {item["name"] for item in plan["evaluation_dimensions"]}
        self.assertIn("Task quality", dimensions)
        self.assertIn("Reliability", dimensions)
        self.assertIn("Latency", dimensions)
        self.assertIn("Cost", dimensions)
        self.assertIn("Safety and data handling", dimensions)
        self.assertIn("Fallback behavior", dimensions)
        eval_ids = {item["id"] for item in plan["sample_eval_set"]}
        self.assertIn("security_boundary", eval_ids)
        self.assertIn("fallback_reasoning", eval_ids)
        self.assertGreaterEqual(len(plan["model_scorecards"]), 2)
        for card in plan["model_scorecards"]:
            self.assertIn(card["decision"], {"pilot_ready", "needs_more_evidence", "discovery_only"})
            self.assertGreaterEqual(card["score"], 0)
            self.assertLessEqual(card["score"], 100)
            self.assertGreaterEqual(len(card["criteria"]), 6)
        self.assertEqual(plan["scoring_rules"]["max_score"], 100)
        self.assertIn("/v1/gateway/model-catalog", plan["evidence_endpoints"])
        self.assertIn("/v1/gateway/security-review", plan["evidence_endpoints"])
        self.assertIn("not a full offline evaluation platform", plan["prototype_note"])
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(plan))
        self.assertNotIn("sk-", json.dumps(plan))
        self.assertNotIn("provider_api_keys", json.dumps(plan))

    def test_incident_playbook_lists_support_scenarios(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/incident-playbook", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, playbook = request_json(self.base_url, path="/v1/gateway/incident-playbook", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(playbook["object"], "gateway.incident_playbook")
        self.assertIn("summary", playbook)
        self.assertGreaterEqual(playbook["summary"]["scenario_count"], 5)
        scenario_ids = {scenario["id"] for scenario in playbook["scenarios"]}
        self.assertIn("provider_not_ready", scenario_ids)
        self.assertIn("recent_request_errors", scenario_ids)
        self.assertIn("customer_budget_blocked", scenario_ids)
        self.assertIn("provider_contract_gap", scenario_ids)
        for scenario in playbook["scenarios"]:
            self.assertIn("operator_steps", scenario)
            self.assertIn("customer_message", scenario)
            self.assertIn("signals", scenario)
        recent_error = next(scenario for scenario in playbook["scenarios"] if scenario["id"] == "recent_request_errors")
        self.assertIn("/v1/gateway/request-detail?request_id=...", recent_error["signals"])
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(playbook))

    def test_support_policy_describes_sla_stages(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/support-policy", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, policy = request_json(self.base_url, path="/v1/gateway/support-policy", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(policy["object"], "gateway.support_policy")
        self.assertEqual(policy["policy_status"], "prototype")
        stages = {level["stage"] for level in policy["service_levels"]}
        self.assertIn("prototype_demo", stages)
        self.assertIn("technical_pilot", stages)
        self.assertIn("production_target", stages)
        severities = {level["severity"] for level in policy["severity_levels"]}
        self.assertEqual(severities, {"P1", "P2", "P3"})
        self.assertGreaterEqual(len(policy["escalation_path"]), 3)
        self.assertIn("current_readiness", policy)
        self.assertIn("not a legal production SLA", policy["plain_english"])
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(policy))

    def test_pilot_checklist_lists_customer_trial_steps(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/pilot-checklist", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, checklist = request_json(self.base_url, path="/v1/gateway/pilot-checklist", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(checklist["object"], "gateway.pilot_checklist")
        self.assertEqual(checklist["pilot_stage"], "pre_pilot")
        phases = {phase["phase"] for phase in checklist["phases"]}
        self.assertIn("Before pilot", phases)
        self.assertIn("During pilot", phases)
        self.assertIn("After pilot", phases)
        self.assertGreaterEqual(len(checklist["roles"]), 4)
        self.assertGreaterEqual(len(checklist["success_criteria"]), 5)
        self.assertIn("productionize", checklist["exit_decision"])
        self.assertEqual(checklist["current_context"]["demo_bundle"], "/v1/gateway/demo-bundle")
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(checklist))

    def test_onboarding_plan_lists_customer_pilot_path(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/onboarding-plan", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, plan = request_json(self.base_url, path="/v1/gateway/onboarding-plan", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(plan["object"], "gateway.onboarding_plan")
        self.assertIn("5 working days", plan["recommended_timeline"])
        days = {step["day"] for step in plan["steps"]}
        self.assertEqual(days, {"Day 0", "Day 1", "Day 2", "Day 3", "Day 4", "Day 5"})
        for step in plan["steps"]:
            self.assertIn("owner", step)
            self.assertIn("actions", step)
            self.assertIn("evidence", step)
            self.assertIn("exit_check", step)
        self.assertGreaterEqual(len(plan["roles"]), 4)
        self.assertTrue(any(item["endpoint"] == "/v1/gateway/demo-bundle" for item in plan["handoff_artifacts"]))
        self.assertEqual(plan["current_readiness"]["prototype_only"], True)
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(plan))

    def test_executive_brief_summarizes_business_story(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/executive-brief", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, brief = request_json(self.base_url, path="/v1/gateway/executive-brief", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(brief["object"], "gateway.executive_brief")
        self.assertIn("one simple model API", brief["one_sentence"])
        self.assertGreaterEqual(len(brief["why_it_matters"]), 4)
        self.assertGreaterEqual(len(brief["what_the_demo_proves"]), 5)
        self.assertIn("No legal production SLA.", brief["what_is_not_production_yet"])
        self.assertGreaterEqual(len(brief["recommended_customer_story"]), 5)
        self.assertEqual(brief["support_position"]["support_policy"], "/v1/gateway/support-policy")
        self.assertEqual(brief["readiness_position"]["production_readiness"], "/v1/gateway/production-readiness")
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(brief))

    def test_roadmap_describes_prototype_to_production_path(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/roadmap", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, roadmap = request_json(self.base_url, path="/v1/gateway/roadmap", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(roadmap["object"], "gateway.roadmap")
        self.assertEqual(roadmap["current_position"]["stage"], "prototype")
        phase_names = {phase["phase"] for phase in roadmap["phases"]}
        self.assertIn("1. Prototype explanation", phase_names)
        self.assertIn("2. Technical pilot", phase_names)
        self.assertIn("3. Production hardening", phase_names)
        self.assertIn("4. Multi-provider expansion", phase_names)
        for phase in roadmap["phases"]:
            self.assertIn("deliverables", phase)
            self.assertIn("exit_criteria", phase)
            self.assertIn("main_risks", phase)
        self.assertIn("/v1/gateway/executive-brief", roadmap["reference_endpoints"])
        self.assertIn("/v1/gateway/provider-contracts", roadmap["reference_endpoints"])
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(roadmap))

    def test_decision_guide_compares_gateway_options(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/decision-guide", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, guide = request_json(self.base_url, path="/v1/gateway/decision-guide", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(guide["object"], "gateway.decision_guide")
        option_names = {option["option"] for option in guide["options"]}
        self.assertIn("Normal API Gateway", option_names)
        self.assertIn("Managed AI Gateway", option_names)
        self.assertIn("Custom Model Gateway", option_names)
        self.assertIn("OpenRouter-like Marketplace", option_names)
        normal_api = next(option for option in guide["options"] if option["option"] == "Normal API Gateway")
        self.assertIn("Provider request and response normalization", normal_api["not_enough_for"])
        self.assertIn("/v1/gateway/provider-contracts", guide["evidence_to_show"])
        self.assertIn("/v1/gateway/roadmap", guide["evidence_to_show"])
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(guide))

    def test_faq_answers_common_customer_questions(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/faq", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, faq = request_json(self.base_url, path="/v1/gateway/faq", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(faq["object"], "gateway.faq")
        questions = {item["question"] for item in faq["questions"]}
        self.assertIn("Is this just an API Gateway?", questions)
        self.assertIn("Is this an OpenRouter clone?", questions)
        self.assertIn("How do we control cost?", questions)
        self.assertIn("Is this ready for production?", questions)
        self.assertGreaterEqual(len(faq["questions"]), 10)
        for item in faq["questions"]:
            self.assertIn("short_answer", item)
            self.assertIn("show", item)
        self.assertIn("/v1/gateway/decision-guide", faq["suggested_demo_order"])
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(faq))

    def test_demo_script_guides_customer_walkthrough(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/demo-script", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, script = request_json(self.base_url, path="/v1/gateway/demo-script", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(script["object"], "gateway.demo_script")
        self.assertEqual(script["duration_minutes"], 15)
        self.assertIn("opening_line", script)
        self.assertIn("closing_line", script)
        self.assertGreaterEqual(len(script["before_demo_checklist"]), 5)
        self.assertGreaterEqual(len(script["steps"]), 8)
        self.assertGreaterEqual(len(script["likely_questions"]), 5)
        shows = {step["show"] for step in script["steps"]}
        self.assertIn(f"{self.base_url}/v1/gateway/executive-brief", shows)
        self.assertIn(f"{self.base_url}/v1/gateway/decision-guide", shows)
        self.assertIn(f"{self.base_url}/v1/gateway/route-preview", shows)
        self.assertIn(f"{self.base_url}/v1/gateway/faq", shows)
        self.assertIn(f"{self.base_url}/v1/gateway/pilot-checklist", shows)
        questions = {item["question"] for item in script["likely_questions"]}
        self.assertIn("Is this just an API Gateway?", questions)
        self.assertIn("Is this like OpenRouter?", questions)
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(script))

    def test_production_readiness_summarizes_go_live_gaps(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/production-readiness", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, readiness = request_json(self.base_url, path="/v1/gateway/production-readiness", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(readiness["object"], "gateway.production_readiness")
        self.assertIn(readiness["overall_status"], {"ready", "needs_work", "blocked"})
        self.assertTrue(readiness["prototype_only"])
        areas = {category["area"] for category in readiness["categories"]}
        self.assertIn("Security", areas)
        self.assertIn("Provider Readiness", areas)
        self.assertIn("Billing", areas)
        self.assertIn("Documentation And Handoff", areas)
        for category in readiness["categories"]:
            self.assertIn(category["status"], {"ready", "needs_work", "blocked"})
            self.assertIn("plain_english", category)
            self.assertIn("next_step", category)
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(readiness))

    def test_deployment_readiness_lists_environment_and_preflight_checks(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/deployment-readiness", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, guide = request_json(self.base_url, path="/v1/gateway/deployment-readiness", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(guide["object"], "gateway.deployment_readiness")
        stages = {stage["stage"] for stage in guide["deployment_stages"]}
        self.assertIn("local_demo", stages)
        self.assertIn("live_qwen_test", stages)
        self.assertIn("technical_pilot", stages)
        self.assertIn("production_target", stages)
        env_names = {item["name"] for item in guide["required_environment"]}
        self.assertIn("GATEWAY_ADMIN_API_KEY", env_names)
        self.assertIn("DASHSCOPE_API_KEY", env_names)
        self.assertIn("model_registry.json", env_names)
        preflight = {item["check"] for item in guide["preflight_checks"]}
        self.assertIn("Admin key replaced", preflight)
        self.assertIn("Data handling policy agreed", preflight)
        ops = {item["endpoint"] for item in guide["operational_checks"]}
        self.assertIn("/health", ops)
        self.assertIn("/v1/models", ops)
        self.assertIn("/v1/gateway/provider-health", ops)
        self.assertGreaterEqual(len(guide["rollback_plan"]), 3)
        self.assertIn("/v1/gateway/config-check", guide["reference_endpoints"])
        self.assertIn("local demo", guide["plain_english"])
        self.assertNotIn("sk-", json.dumps(guide))

    def test_production_backlog_prioritizes_hardening_work(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/production-backlog", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, backlog = request_json(self.base_url, path="/v1/gateway/production-backlog", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(backlog["object"], "gateway.production_backlog")
        priorities = {item["priority"] for item in backlog["work_items"]}
        self.assertEqual(priorities, {"P0", "P1", "P2"})
        areas = {item["area"] for item in backlog["work_items"]}
        self.assertIn("secrets", areas)
        self.assertIn("storage", areas)
        self.assertIn("data_governance", areas)
        self.assertIn("provider_contracts", areas)
        for item in backlog["work_items"]:
            self.assertIn("owner", item)
            self.assertIn("done_when", item)
            self.assertIn("evidence", item)
        summary = {item["priority"]: item["count"] for item in backlog["summary_by_priority"]}
        self.assertEqual(summary["P0"], 5)
        self.assertEqual(summary["P1"], 3)
        self.assertEqual(summary["P2"], 2)
        self.assertIn("/v1/gateway/deployment-readiness", backlog["reference_endpoints"])
        self.assertIn("readiness_status", backlog["current_context"])
        self.assertIn("funded", backlog["next_best_action"])
        self.assertNotIn("sk-", json.dumps(backlog))

    def test_pilot_scorecard_scores_customer_pilot_progress(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/pilot-scorecard", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "Create pilot scorecard evidence."}],
                "stream": False,
            },
        )
        status, scorecard = request_json(self.base_url, path="/v1/gateway/pilot-scorecard", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(scorecard["object"], "gateway.pilot_scorecard")
        self.assertEqual(scorecard["scoring"]["max_score"], 100)
        self.assertIn("average_score", scorecard["summary"])
        cards = {item["customer_id"]: item for item in scorecard["scorecards"]}
        self.assertIn("dev", cards)
        dev = cards["dev"]
        self.assertIn(dev["decision"], {"keep_in_discovery", "extend_pilot", "plan_production_hardening"})
        criteria = {item["name"]: item for item in dev["criteria"]}
        self.assertIn("First request completed", criteria)
        self.assertIn("Request tracing available", criteria)
        self.assertIn("Production gaps acknowledged", criteria)
        self.assertGreaterEqual(dev["score"], 0)
        self.assertLessEqual(dev["score"], 100)
        self.assertIn("/v1/gateway/production-backlog", scorecard["reference_endpoints"])
        self.assertNotIn("provider_api_keys", json.dumps(scorecard))
        self.assertNotIn("sk-", json.dumps(scorecard))

    def test_launch_plan_lists_go_live_gates(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/launch-plan", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, plan = request_json(self.base_url, path="/v1/gateway/launch-plan", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(plan["object"], "gateway.launch_plan")
        self.assertIn(plan["decision"], {"go", "conditional_go", "no_go"})
        gate_ids = {gate["id"] for gate in plan["gates"]}
        self.assertIn("security", gate_ids)
        self.assertIn("provider", gate_ids)
        self.assertIn("billing", gate_ids)
        self.assertIn("handoff", gate_ids)
        for gate in plan["gates"]:
            self.assertIn(gate["status"], {"ready", "needs_work", "blocked"})
            self.assertIn("owner", gate)
            self.assertIn("required_evidence", gate)
            self.assertIn("approval_question", gate)
            self.assertIn("next_step", gate)
        self.assertGreaterEqual(len(plan["required_signoffs"]), 4)
        self.assertGreaterEqual(len(plan["rollout_stages"]), 4)
        self.assertIn("/v1/gateway/production-readiness", plan["reference_endpoints"])
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(plan))

    def test_change_management_lists_approval_and_rollback_paths(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/change-management", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, plan = request_json(self.base_url, path="/v1/gateway/change-management", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(plan["object"], "gateway.change_management")
        change_types = {item["type"] for item in plan["change_types"]}
        self.assertIn("customer_access_change", change_types)
        self.assertIn("provider_change", change_types)
        self.assertIn("model_route_change", change_types)
        for item in plan["change_types"]:
            self.assertIn("approval_owner", item)
            self.assertIn("before_change", item)
            self.assertIn("after_change", item)
            self.assertIn("rollback_path", item)
        self.assertIn("/v1/gateway/audit-events", plan["evidence_endpoints"])
        self.assertIn(plan["current_launch_decision"], {"go", "conditional_go", "no_go"})
        self.assertIn("approval workflow", plan["prototype_note"])
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(plan))

    def test_data_governance_explains_privacy_and_retention_gaps(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/data-governance", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, review = request_json(self.base_url, path="/v1/gateway/data-governance", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(review["object"], "gateway.data_governance")
        areas = {item["area"] for item in review["categories"]}
        self.assertIn("Prompt and response data", areas)
        self.assertIn("Provider secrets", areas)
        self.assertIn("Logging and retention", areas)
        for item in review["categories"]:
            self.assertIn("plain_english", item)
            self.assertIn("what_this_prototype_does", item)
            self.assertIn("production_gap", item)
            self.assertIn("owner", item)
        self.assertIn("/v1/gateway/safety-preview", review["evidence_endpoints"])
        self.assertIn("/v1/gateway/request-detail", review["evidence_endpoints"])
        self.assertIn("retention", " ".join(review["policy_questions"]).lower())
        self.assertIn("not a compliance certification", review["prototype_note"])
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(review))
        self.assertNotIn("sk-", json.dumps(review))

    def test_security_review_explains_threats_and_go_live_gates(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/security-review", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, review = request_json(self.base_url, path="/v1/gateway/security-review", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(review["object"], "gateway.security_review")
        self.assertEqual(review["security_posture"]["current_stage"], "prototype")
        risks = {item["risk"] for item in review["threats"]}
        self.assertIn("Customer key leak", risks)
        self.assertIn("Provider secret exposure", risks)
        self.assertIn("Sensitive prompt data in logs", risks)
        self.assertIn("Wrong customer or model access", risks)
        self.assertIn("Unsafe admin change", risks)
        for item in review["threats"]:
            self.assertIn("current_control", item)
            self.assertIn("production_control_needed", item)
            self.assertIn("evidence", item)
        self.assertTrue(any("provider API keys" in item["question"] for item in review["customer_security_questions"]))
        self.assertTrue(any("secret manager" in gate for gate in review["go_live_security_gates"]))
        self.assertIn("/v1/gateway/data-governance", review["evidence_endpoints"])
        self.assertIn("SOC 2", review["prototype_note"])
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(review))
        self.assertNotIn("sk-", json.dumps(review))
        self.assertNotIn("provider_api_keys", json.dumps(review))

    def test_discovery_checklist_turns_broad_idea_into_scope(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/discovery-checklist", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, checklist = request_json(self.base_url, path="/v1/gateway/discovery-checklist", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(checklist["object"], "gateway.discovery_checklist")
        sections = {item["section"] for item in checklist["discovery_sections"]}
        self.assertIn("Customer goal", sections)
        self.assertIn("Model and provider scope", sections)
        self.assertIn("Data and governance", sections)
        self.assertIn("Production expectation", sections)
        for item in checklist["discovery_sections"]:
            self.assertIn("questions", item)
            self.assertIn("evidence_to_collect", item)
            self.assertIn("red_flags", item)
        fits = {item["fit"] for item in checklist["fit_assessment"]}
        self.assertIn("Normal API Gateway may be enough", fits)
        self.assertIn("Custom Model Gateway is useful", fits)
        self.assertIn("OpenRouter-like marketplace is later", fits)
        self.assertIn("/v1/gateway/data-governance", checklist["reference_endpoints"])
        self.assertIn("one customer", checklist["recommended_first_pilot"]["scope"])
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(checklist))

    def test_proposal_summary_packages_customer_scope(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/proposal-summary", api_key="dev-gateway-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, summary = request_json(self.base_url, path="/v1/gateway/proposal-summary", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(summary["object"], "gateway.proposal_summary")
        self.assertIn("one controlled API", summary["customer_problem"])
        self.assertGreaterEqual(len(summary["phase_one_scope"]), 6)
        self.assertIn("Public OpenRouter-style marketplace.", summary["not_in_phase_one"])
        deliverables = {item["name"] for item in summary["customer_deliverables"]}
        self.assertIn("Discovery checklist", deliverables)
        self.assertIn("Customer guide PDF", deliverables)
        decisions = {item["decision"] for item in summary["decision_points"]}
        self.assertIn("Build custom gateway or use managed gateway", decisions)
        self.assertIn("Mock or live test", decisions)
        self.assertGreaterEqual(len(summary["main_risks"]), 4)
        self.assertIn("/v1/gateway/discovery-checklist", summary["reference_endpoints"])
        self.assertIn("readiness_status", summary["current_context"])
        self.assertIn("not to build every provider", summary["customer_safe_close"])
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(summary))
        self.assertNotIn("sk-", json.dumps(summary))

    def test_customer_integration_guide_lists_safe_code_examples(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/integration-guide", api_key="wrong-key")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_api_key")

        status, guide = request_json(self.base_url, path="/v1/gateway/integration-guide", api_key="dev-gateway-key")
        self.assertEqual(status, 200)
        self.assertEqual(guide["object"], "customer.integration_guide")
        self.assertEqual(guide["customer"]["id"], "dev")
        self.assertEqual(guide["base_url"], self.base_url)
        self.assertIn("smart-fast", guide["models"]["allowed"])
        self.assertIn(guide["models"]["recommended"], guide["models"]["allowed"])
        self.assertIn("curl_chat", guide["code_examples"])
        self.assertIn("python", guide["code_examples"])
        self.assertIn("javascript", guide["code_examples"])
        self.assertIn("YOUR_GATEWAY_API_KEY", guide["code_examples"]["curl_chat"])
        self.assertNotIn("dev-gateway-key", json.dumps(guide["code_examples"]))
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(guide))
        self.assertGreaterEqual(len(guide["go_live_checklist"]), 5)

        status, starter = request_json(self.base_url, path="/v1/gateway/sdk-starter", api_key="wrong-key")
        self.assertEqual(status, 401)
        self.assertEqual(starter["error"]["code"], "invalid_api_key")

        status, starter = request_json(self.base_url, path="/v1/gateway/sdk-starter", api_key="dev-gateway-key")
        self.assertEqual(status, 200)
        self.assertEqual(starter["object"], "customer.sdk_starter")
        self.assertEqual(starter["customer"]["id"], "dev")
        self.assertIn(".env.example", starter["files"])
        self.assertIn("gateway_client.py", starter["files"])
        self.assertIn("gateway-client.mjs", starter["files"])
        self.assertIn("YOUR_GATEWAY_API_KEY", starter["files"][".env.example"])
        self.assertIn("common_errors", starter)
        error_codes = {item["code"] for item in starter["common_errors"]}
        self.assertIn("invalid_api_key", error_codes)
        self.assertIn("model_not_allowed", error_codes)
        self.assertGreaterEqual(len(starter["handoff_checklist"]), 4)
        self.assertNotIn("dev-gateway-key", json.dumps(starter["files"]))
        self.assertNotIn("DASHSCOPE_API_KEY", json.dumps(starter))

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
        self.assertEqual(payload["gateway"]["route_decision"]["summary"], "smart-fast -> qwen-plus via dashscope")
        self.assertIn("Required capabilities", " ".join(payload["gateway"]["route_decision"]["reasons"]))
        request_id = payload["gateway"]["request_id"]
        self.assertTrue(request_id)

        status, detail = request_json(self.base_url, path=f"/v1/gateway/request-detail?request_id={request_id}")
        self.assertEqual(status, 401)

        status, detail = request_json(
            self.base_url,
            path=f"/v1/gateway/request-detail?request_id={request_id}",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["request_id"], request_id)
        self.assertEqual(detail["customer_id"], "dev")
        self.assertEqual(detail["model"], "smart-fast")
        self.assertEqual(detail["resolved_model"], "qwen-plus")
        self.assertEqual(detail["outcome"], "success")
        self.assertIn("summary", detail)
        self.assertIn("usage", detail)

        status, detail = request_json(
            self.base_url,
            path="/v1/gateway/request-detail?request_id=missing",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 404)
        self.assertEqual(detail["error"]["code"], "request_not_found")

        status, usage = request_json(self.base_url, path="/v1/gateway/usage", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(usage["data"]), 1)

        status, requests = request_json(self.base_url, path="/v1/gateway/requests", api_key="dev-admin-key")
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
        self.assertEqual(payload["gateway"]["routing_policy"]["source"], "model_registry")
        self.assertEqual(payload["gateway"]["route_decision"]["selected_public_model"], "qwen-turbo")
        self.assertEqual(len(payload["gateway"]["route_decision"]["fallback_attempts"]), 1)

    def test_request_can_disable_fallback(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "gateway_force_failover": True,
                "gateway_disable_fallback": True,
                "messages": [{"role": "user", "content": "Do not fallback."}],
                "stream": False,
            },
        )
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "route_failed")

    def test_request_can_choose_fallback_models(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "gateway_force_failover": True,
                "gateway_fallback_models": ["qwen-turbo"],
                "messages": [{"role": "user", "content": "Use custom fallback list."}],
                "stream": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["gateway"]["resolved_model"], "qwen-turbo")
        self.assertEqual(payload["gateway"]["routing_policy"]["source"], "request")
        self.assertEqual(payload["gateway"]["routing_policy"]["candidates"], ["smart-fast", "qwen-turbo"])

    def test_request_can_limit_allowed_providers(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "gateway_allowed_providers": ["dashscope"],
                "messages": [{"role": "user", "content": "Use provider allow list."}],
                "stream": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["gateway"]["provider"], "dashscope")
        self.assertEqual(payload["gateway"]["routing_policy"]["source"], "request")
        self.assertEqual(payload["gateway"]["routing_policy"]["allowed_providers"], ["dashscope"])

    def test_request_rejects_provider_policy_without_route(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "gateway_allowed_providers": ["openai"],
                "messages": [{"role": "user", "content": "Do not use DashScope."}],
                "stream": False,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "no_allowed_provider_route")

    def test_mock_tool_call_response_uses_openai_shape(self):
        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "Check the weather."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Get weather for a city.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "city": {"type": "string"}
                                },
                                "required": ["city"],
                            },
                        },
                    }
                ],
                "tool_choice": "auto",
                "stream": False,
            },
        )
        self.assertEqual(status, 200)
        choice = payload["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        tool_call = choice["message"]["tool_calls"][0]
        self.assertEqual(tool_call["type"], "function")
        self.assertEqual(tool_call["function"]["name"], "get_weather")
        self.assertEqual(payload["gateway"]["tool_support"], "mock_tool_call")
        self.assertIn("tools", payload["gateway"]["routing_policy"]["required_capabilities"])
        self.assertEqual(payload["gateway"]["routing_policy"]["candidates"], ["smart-fast"])

    def test_safety_preview_and_optional_blocking(self):
        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/safety-preview",
            payload={"messages": [{"role": "user", "content": "My email is user@example.com"}]},
        )
        self.assertEqual(status, 401)

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/safety-preview",
            api_key="dev-admin-key",
            payload={"messages": [{"role": "user", "content": "token=sk_test_1234567890abcdef"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["status"], "blocked")
        self.assertGreaterEqual(preview["summary"]["critical"], 1)
        self.assertTrue(preview["redaction_available"])
        self.assertGreaterEqual(preview["summary"]["redactions"], 1)
        self.assertIn("[REDACTED_", preview["redacted_preview"]["messages"][0]["content"])
        self.assertIn("not a full DLP", preview["note"])

        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "My email is user@example.com"}],
                "gateway_safety_check": True,
                "stream": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["gateway"]["safety_preview"]["status"], "review")

        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "My email is user@example.com"}],
                "gateway_redact_sensitive": True,
                "stream": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["gateway"]["redaction_applied"])
        self.assertIn("[REDACTED_EMAIL]", payload["choices"][0]["message"]["content"])
        self.assertNotIn("user@example.com", payload["choices"][0]["message"]["content"])

        status, payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "api_key=sk_test_1234567890abcdef"}],
                "gateway_block_sensitive": True,
                "stream": False,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "safety_blocked")
        self.assertGreaterEqual(payload["error"]["details"]["summary"]["critical"], 1)

    def test_anthropic_tool_normalization(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "lookup_order",
                    "description": "Lookup an order.",
                    "parameters": {
                        "type": "object",
                        "properties": {"order_id": {"type": "string"}},
                        "required": ["order_id"],
                    },
                },
            }
        ]
        normalized = normalize_tools_for_anthropic(tools)
        self.assertEqual(normalized[0]["name"], "lookup_order")
        self.assertEqual(normalized[0]["input_schema"]["properties"]["order_id"]["type"], "string")

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
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, payload = request_json(
            self.base_url,
            path="/v1/gateway/status",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        customers = {customer["id"]: customer for customer in payload["customers"]}
        self.assertIn("budget", customers["dev"])
        self.assertIn("dashscope", customers["dev"]["byok_providers"])
        self.assertNotIn("provider_api_keys", customers["dev"])
        reports = {report["id"]: report for report in payload["customer_reports"]}
        self.assertIn("dev", reports)
        self.assertIn("budget_state", reports["dev"])
        self.assertNotIn("provider_api_keys", reports["dev"])
        self.assertIn("customer_success", payload)
        self.assertIn("accounts", payload["customer_success"])
        self.assertIn("request_activity", payload)
        self.assertIn("audit_events", payload)
        self.assertIn("audit_event_count", payload["summary"])
        self.assertIn("invoice_preview", payload)
        self.assertIn("totals", payload["invoice_preview"])
        self.assertIn("production_readiness", payload)
        self.assertIn("categories", payload["production_readiness"])
        self.assertGreaterEqual(len(payload["production_readiness"]["categories"]), 5)
        self.assertIn("deployment_readiness", payload)
        self.assertIn("deployment_stages", payload["deployment_readiness"])
        self.assertIn("production_backlog", payload)
        self.assertIn("work_items", payload["production_backlog"])
        self.assertIn("pilot_scorecard", payload)
        self.assertIn("scorecards", payload["pilot_scorecard"])
        self.assertIn("handoff_checklist", payload)
        self.assertIn("handoff_groups", payload["handoff_checklist"])
        self.assertIn("launch_plan", payload)
        self.assertIn("gates", payload["launch_plan"])
        self.assertIn("change_management", payload)
        self.assertIn("change_types", payload["change_management"])
        self.assertIn("data_governance", payload)
        self.assertIn("categories", payload["data_governance"])
        self.assertIn("security_review", payload)
        self.assertIn("threats", payload["security_review"])
        self.assertIn("evaluation_plan", payload)
        self.assertIn("model_scorecards", payload["evaluation_plan"])
        self.assertIn("discovery_checklist", payload)
        self.assertIn("discovery_sections", payload["discovery_checklist"])
        self.assertIn("proposal_summary", payload)
        self.assertIn("phase_one_scope", payload["proposal_summary"])
        catalog = {model["id"]: model for model in payload["model_catalog"]}
        self.assertIn("smart-fast", catalog)
        self.assertEqual(catalog["smart-fast"]["route_chain"][0], "smart-fast")
        self.assertIn("alerts", payload)
        self.assertIn("summary", payload["alerts"])
        self.assertIn("alerts", payload["alerts"])
        self.assertIn("access_matrix", payload)
        matrix = {row["id"]: row for row in payload["access_matrix"]}
        self.assertIn("dev", matrix)
        self.assertGreaterEqual(matrix["dev"]["allowed_count"], 1)

    def test_admin_summary_endpoints(self):
        request_json(
            self.base_url,
            method="POST",
            path="/v1/chat/completions",
            api_key="dev-gateway-key",
            payload={
                "model": "smart-fast",
                "messages": [{"role": "user", "content": "Create usage for summary endpoints."}],
                "stream": False,
            },
        )

        status, providers = request_json(self.base_url, path="/v1/gateway/providers")
        self.assertEqual(status, 401)

        status, providers = request_json(self.base_url, path="/v1/gateway/providers", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        dashscope = {provider["id"]: provider for provider in providers["data"]}["dashscope"]
        self.assertIn("smart-fast", dashscope["models"])
        self.assertIn("dev", dashscope["byok_customers"])
        self.assertNotIn("provider_api_keys", dashscope)

        status, provider_health = request_json(self.base_url, path="/v1/gateway/provider-health")
        self.assertEqual(status, 401)

        status, provider_health = request_json(self.base_url, path="/v1/gateway/provider-health", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        dashscope_health = {provider["id"]: provider for provider in provider_health["data"]}["dashscope"]
        self.assertIn(dashscope_health["status"], {"ready", "ready_mock", "degraded"})
        self.assertIn("smart-fast", dashscope_health["models"])
        self.assertIn("recent", dashscope_health)
        self.assertGreaterEqual(dashscope_health["recent"]["requests"], 1)
        self.assertIn("live_ready", dashscope_health)
        self.assertIn("mock_ready", dashscope_health)

        status, alerts = request_json(self.base_url, path="/v1/gateway/alerts")
        self.assertEqual(status, 401)

        status, alerts = request_json(self.base_url, path="/v1/gateway/alerts", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertIn(alerts["status"], {"ok", "warning", "critical"})
        self.assertIn("summary", alerts)
        alert_codes = {alert["code"] for alert in alerts["alerts"]}
        self.assertIn("default_admin_key", alert_codes)
        self.assertTrue(all("next_step" in alert for alert in alerts["alerts"]))

        status, access_matrix = request_json(self.base_url, path="/v1/gateway/access-matrix")
        self.assertEqual(status, 401)

        status, access_matrix = request_json(self.base_url, path="/v1/gateway/access-matrix", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        matrix = {row["id"]: row for row in access_matrix["data"]}
        self.assertIn("dev", matrix)
        self.assertIn("demo-limited", matrix)
        dev_models = {item["model"]: item for item in matrix["dev"]["models"]}
        limited_models = {item["model"]: item for item in matrix["demo-limited"]["models"]}
        self.assertTrue(dev_models["qwen-plus"]["allowed"])
        self.assertFalse(limited_models["qwen-plus"]["allowed"])
        self.assertEqual(limited_models["qwen-plus"]["reason"], "not listed in customer allowed_models")

        status, model_catalog = request_json(self.base_url, path="/v1/gateway/model-catalog")
        self.assertEqual(status, 401)

        status, model_catalog = request_json(self.base_url, path="/v1/gateway/model-catalog", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        catalog = {model["id"]: model for model in model_catalog["data"]}
        self.assertIn("smart-fast", catalog)
        self.assertEqual(catalog["smart-fast"]["provider"], "dashscope")
        self.assertEqual(catalog["smart-fast"]["upstream_model"], "qwen-plus")
        self.assertIn("qwen-turbo", catalog["smart-fast"]["fallback_models"])
        self.assertIn("provider_status", catalog["smart-fast"])
        self.assertIn("pricing", catalog["smart-fast"])
        self.assertGreaterEqual(catalog["smart-fast"]["usage"]["requests"], 1)

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "model": "smart-fast"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(preview["allowed"])
        self.assertEqual(preview["customer"]["id"], "dev")
        self.assertEqual(preview["routes"][0]["public_model"], "smart-fast")
        self.assertEqual(preview["routes"][0]["upstream_model"], "qwen-plus")
        self.assertIn("qwen-turbo", preview["routing_policy"]["candidates"])
        self.assertEqual(preview["routing_policy"]["required_capabilities"], ["chat"])
        self.assertEqual(preview["route_decision"]["summary"], "smart-fast -> qwen-plus via dashscope")
        self.assertIn("Fallback candidates", " ".join(preview["route_decision"]["reasons"]))

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "model": "smart-fast", "gateway_required_capabilities": ["tools"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["routing_policy"]["required_capabilities"], ["chat", "tools"])
        self.assertEqual(preview["routing_policy"]["candidates"], ["smart-fast"])
        self.assertTrue(all("tools" in route["capabilities"] for route in preview["routes"]))

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "model": "smart-fast", "gateway_required_capabilities": ["vision"]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(preview["error"]["code"], "no_capability_route")

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "model": "smart-fast", "gateway_allowed_providers": ["dashscope"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["routing_policy"]["allowed_providers"], ["dashscope"])
        self.assertTrue(all(route["provider"] == "dashscope" for route in preview["routes"]))

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "model": "smart-fast", "gateway_allowed_providers": ["openai"]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(preview["error"]["code"], "no_allowed_provider_route")

        status, preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "demo-limited", "model": "qwen-plus"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(preview["error"]["code"], "model_not_allowed")

        status, estimate = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/cost-estimate",
            payload={"customer_id": "dev", "model": "smart-fast", "prompt": "Estimate this request.", "max_tokens": 128},
        )
        self.assertEqual(status, 401)

        status, estimate = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/cost-estimate",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "model": "smart-fast", "prompt": "Estimate this request.", "max_tokens": 128},
        )
        self.assertEqual(status, 200)
        self.assertEqual(estimate["model"], "smart-fast")
        self.assertEqual(estimate["resolved_model"], "qwen-plus")
        self.assertEqual(estimate["provider"], "dashscope")
        self.assertGreaterEqual(estimate["estimate"]["prompt_tokens"], 1)
        self.assertEqual(estimate["estimate"]["completion_tokens"], 128)
        self.assertIn("budget_after_estimate", estimate)
        self.assertFalse(estimate["budget_after_estimate"]["would_exceed_budget"])

        status, key_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/key-issue-preview",
            payload={"customer_id": "customer-a", "allowed_models": ["smart-fast"]},
        )
        self.assertEqual(status, 401)

        status, key_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/key-issue-preview",
            api_key="dev-admin-key",
            payload={
                "customer_id": "customer-a",
                "name": "Customer A",
                "plan": "starter",
                "allowed_models": ["smart-fast"],
                "request_limit": 12,
                "token_budget": 900,
                "cost_budget": 0.25,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(key_preview["customer"]["id"], "customer-a")
        self.assertEqual(key_preview["customer"]["api_key"], key_preview["generated_api_key_masked"])
        self.assertTrue(key_preview["generated_api_key"].startswith("aisr_"))
        self.assertEqual(key_preview["config_snippet"]["allowed_models"], ["smart-fast"])
        self.assertNotEqual(key_preview["config_snippet"]["api_key"], key_preview["customer"]["api_key"])
        self.assertIn("does not persist", key_preview["note"])
        self.assertTrue(any("route-preview" in step for step in key_preview["next_steps"]))

        status, key_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/key-issue-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "allowed_models": ["smart-fast"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(key_preview["error"]["code"], "customer_already_exists")

        status, key_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/key-issue-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "customer-key-conflict", "api_key": "dev-gateway-key", "allowed_models": ["smart-fast"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(key_preview["error"]["code"], "api_key_already_exists")

        status, key_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/key-issue-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "customer-b", "allowed_models": ["missing-model"]},
        )
        self.assertEqual(status, 404)
        self.assertEqual(key_preview["error"]["code"], "unknown_allowed_model")

        status, created = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/customers",
            api_key="dev-admin-key",
            payload={
                "customer_id": "lifecycle-customer",
                "name": "Lifecycle Customer",
                "plan": "trial",
                "api_key": "lifecycle-key-1",
                "allowed_models": ["smart-fast"],
                "request_limit": 5,
                "token_budget": 1000,
                "cost_budget": 0.25,
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["object"], "customer.created")
        self.assertEqual(created["customer"]["id"], "lifecycle-customer")
        self.assertEqual(created["generated_api_key"], "lifecycle-key-1")
        self.assertNotIn("provider_api_keys", json.dumps(created))

        status, default_policy_customer = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/customers",
            api_key="dev-admin-key",
            payload={
                "customer_id": "default-policy-customer",
                "name": "Default Policy Customer",
                "api_key": "default-policy-key",
                "allowed_models": ["smart-fast"],
                "default_policy": "tool_ready",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(default_policy_customer["customer"]["default_policy"], "tool_ready")

        status, default_policy_view = request_json(
            self.base_url,
            path="/v1/gateway/me",
            api_key="default-policy-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(default_policy_view["customer"]["default_policy"], "tool_ready")

        status, default_policy_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "default-policy-customer", "model": "smart-fast"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(default_policy_preview["routing_policy"]["gateway_policy"]["name"], "tool_ready")
        self.assertEqual(default_policy_preview["routing_policy"]["gateway_policy_source"], "customer_default")
        self.assertIn("tools", default_policy_preview["routing_policy"]["required_capabilities"])

        status, override_policy_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={
                "customer_id": "default-policy-customer",
                "model": "smart-fast",
                "gateway_policy": "balanced",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(override_policy_preview["routing_policy"]["gateway_policy"]["name"], "balanced")
        self.assertEqual(override_policy_preview["routing_policy"]["gateway_policy_source"], "request")

        status, invalid_default_policy = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/customers",
            api_key="dev-admin-key",
            payload={
                "customer_id": "bad-default-policy",
                "api_key": "bad-default-policy-key",
                "allowed_models": ["smart-fast"],
                "default_policy": "missing-policy",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid_default_policy["error"]["code"], "unknown_gateway_policy")

        status, lifecycle_view = request_json(self.base_url, path="/v1/gateway/me", api_key="lifecycle-key-1")
        self.assertEqual(status, 200)
        self.assertEqual(lifecycle_view["customer"]["id"], "lifecycle-customer")
        self.assertEqual({model["id"] for model in lifecycle_view["models"]}, {"smart-fast"})

        status, duplicate = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/customers",
            api_key="dev-admin-key",
            payload={"customer_id": "lifecycle-customer", "api_key": "another-key", "allowed_models": ["smart-fast"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(duplicate["error"]["code"], "customer_already_exists")

        status, rotated = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/customers/rotate-key",
            api_key="dev-admin-key",
            payload={"customer_id": "lifecycle-customer", "api_key": "lifecycle-key-2"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(rotated["object"], "customer.key_rotated")
        self.assertEqual(rotated["generated_api_key"], "lifecycle-key-2")
        self.assertIn("old_api_key_hash", rotated)

        status, old_key_view = request_json(self.base_url, path="/v1/gateway/me", api_key="lifecycle-key-1")
        self.assertEqual(status, 401)
        self.assertEqual(old_key_view["error"]["code"], "invalid_api_key")

        status, new_key_view = request_json(self.base_url, path="/v1/gateway/me", api_key="lifecycle-key-2")
        self.assertEqual(status, 200)
        self.assertEqual(new_key_view["customer"]["id"], "lifecycle-customer")

        status, disabled = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/customers/disable",
            api_key="dev-admin-key",
            payload={"customer_id": "lifecycle-customer"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(disabled["object"], "customer.disabled")
        self.assertFalse(disabled["customer"]["enabled"])

        status, disabled_view = request_json(self.base_url, path="/v1/gateway/me", api_key="lifecycle-key-2")
        self.assertEqual(status, 401)
        self.assertEqual(disabled_view["error"]["code"], "invalid_api_key")

        status, audit = request_json(self.base_url, path="/v1/gateway/audit-events")
        self.assertEqual(status, 401)

        status, audit = request_json(
            self.base_url,
            path="/v1/gateway/audit-events?target_id=lifecycle-customer",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        actions = {event["action"] for event in audit["data"]}
        self.assertIn("customer.created", actions)
        self.assertIn("customer.key_rotated", actions)
        self.assertIn("customer.disabled", actions)
        audit_text = json.dumps(audit)
        self.assertNotIn("lifecycle-key-1", audit_text)
        self.assertNotIn("lifecycle-key-2", audit_text)
        self.assertIn("api_key_hash", audit_text)

        status, audit = request_json(
            self.base_url,
            path="/v1/gateway/audit-events?action=customer.key_rotated&limit=5",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertTrue(all(event["action"] == "customer.key_rotated" for event in audit["data"]))

        status, created_route = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/model-routes",
            api_key="dev-admin-key",
            payload={
                "model_id": "demo-managed-route",
                "provider": "dashscope",
                "upstream_model": "qwen-plus",
                "fallback_models": ["smart-fast"],
                "capabilities": ["chat", "streaming"],
                "pricing": {"prompt_per_1k": 0, "completion_per_1k": 0},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(created_route["object"], "model_route.created")
        self.assertEqual(created_route["model"]["id"], "demo-managed-route")

        status, model_list = request_json(self.base_url, path="/v1/models", api_key="dev-gateway-key")
        self.assertEqual(status, 200)
        self.assertIn("demo-managed-route", {model["id"] for model in model_list["data"]})

        status, route_preview_payload = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={"customer_id": "dev", "model": "demo-managed-route"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(route_preview_payload["routes"][0]["public_model"], "demo-managed-route")

        status, duplicate_route = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/model-routes",
            api_key="dev-admin-key",
            payload={"model_id": "demo-managed-route", "provider": "dashscope", "upstream_model": "qwen-plus"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(duplicate_route["error"]["code"], "model_route_already_exists")

        status, bad_route = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/model-routes",
            api_key="dev-admin-key",
            payload={"model_id": "bad-route", "provider": "openai", "upstream_model": "gpt-4o-mini"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(bad_route["error"]["code"], "unknown_provider")

        status, updated_route = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/model-routes/update",
            api_key="dev-admin-key",
            payload={
                "model_id": "demo-managed-route",
                "fallback_models": ["qwen-turbo"],
                "capabilities": ["chat", "streaming", "tools"],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated_route["object"], "model_route.updated")
        self.assertEqual(updated_route["model"]["fallback_models"], ["qwen-turbo"])
        self.assertIn("tools", updated_route["model"]["capabilities"])

        status, catalog_after_update = request_json(
            self.base_url,
            path="/v1/gateway/model-catalog",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        route_catalog = {model["id"]: model for model in catalog_after_update["data"]}
        self.assertEqual(route_catalog["demo-managed-route"]["fallback_models"], ["qwen-turbo"])

        status, disabled_route = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/model-routes/disable",
            api_key="dev-admin-key",
            payload={"model_id": "demo-managed-route"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(disabled_route["object"], "model_route.disabled")
        self.assertFalse(disabled_route["model"]["enabled"])

        status, model_list_after_disable = request_json(self.base_url, path="/v1/models", api_key="dev-gateway-key")
        self.assertEqual(status, 200)
        self.assertNotIn("demo-managed-route", {model["id"] for model in model_list_after_disable["data"]})

        status, route_audit = request_json(
            self.base_url,
            path="/v1/gateway/audit-events?target_id=demo-managed-route",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        route_actions = {event["action"] for event in route_audit["data"]}
        self.assertIn("model_route.created", route_actions)
        self.assertIn("model_route.updated", route_actions)
        self.assertIn("model_route.disabled", route_actions)

        status, cheap_route = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/model-routes",
            api_key="dev-admin-key",
            payload={
                "model_id": "strategy-cheap",
                "provider": "dashscope",
                "upstream_model": "qwen-turbo",
                "capabilities": ["chat"],
                "pricing": {"prompt_per_1k": 0.001, "completion_per_1k": 0.001},
            },
        )
        self.assertEqual(status, 201)

        status, expensive_route = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/model-routes",
            api_key="dev-admin-key",
            payload={
                "model_id": "strategy-main",
                "provider": "dashscope",
                "upstream_model": "qwen-plus",
                "fallback_models": ["strategy-cheap"],
                "capabilities": ["chat"],
                "pricing": {"prompt_per_1k": 0.02, "completion_per_1k": 0.02},
            },
        )
        self.assertEqual(status, 201)

        status, strategy_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={
                "customer_id": "dev",
                "model": "strategy-main",
                "gateway_route_strategy": "lowest_cost",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(strategy_preview["routing_policy"]["route_strategy"], "lowest_cost")
        self.assertEqual(strategy_preview["routing_policy"]["candidates"], ["strategy-cheap", "strategy-main"])
        self.assertEqual(strategy_preview["route_decision"]["selected_public_model"], "strategy-cheap")
        self.assertEqual(strategy_preview["routes"][0]["public_model"], "strategy-cheap")
        self.assertIn("candidate_scores", strategy_preview["route_decision"])

        status, invalid_strategy = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={
                "customer_id": "dev",
                "model": "strategy-main",
                "gateway_route_strategy": "random",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid_strategy["error"]["code"], "invalid_route_strategy")

        status, presets = request_json(self.base_url, path="/v1/gateway/policy-presets")
        self.assertEqual(status, 401)

        status, presets = request_json(self.base_url, path="/v1/gateway/policy-presets", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertIn("lowest_cost", presets["data"])
        self.assertIn("tool_ready", presets["data"])

        status, policy_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={
                "customer_id": "dev",
                "model": "strategy-main",
                "gateway_policy": "lowest_cost",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(policy_preview["routing_policy"]["source"], "policy")
        self.assertEqual(policy_preview["routing_policy"]["gateway_policy"]["name"], "lowest_cost")
        self.assertEqual(policy_preview["routing_policy"]["route_strategy"], "lowest_cost")
        self.assertEqual(policy_preview["route_decision"]["selected_public_model"], "strategy-cheap")

        status, tool_policy_preview = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={
                "customer_id": "dev",
                "model": "smart-fast",
                "gateway_policy": "tool_ready",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(tool_policy_preview["routing_policy"]["gateway_policy"]["name"], "tool_ready")
        self.assertIn("tools", tool_policy_preview["routing_policy"]["required_capabilities"])
        self.assertEqual(tool_policy_preview["routing_policy"]["route_strategy"], "healthiest")

        status, unknown_policy = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/route-preview",
            api_key="dev-admin-key",
            payload={
                "customer_id": "dev",
                "model": "smart-fast",
                "gateway_policy": "unknown-policy",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(unknown_policy["error"]["code"], "unknown_gateway_policy")

        status, created_provider = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/providers",
            api_key="dev-admin-key",
            payload={
                "provider_id": "demo-provider",
                "name": "Demo Provider",
                "type": "openai_compatible",
                "base_url": "https://example.com/v1",
                "api_key_env": "DEMO_PROVIDER_API_KEY",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(created_provider["object"], "provider.created")
        self.assertEqual(created_provider["provider"]["id"], "demo-provider")
        self.assertNotIn("provider_api_keys", json.dumps(created_provider))
        self.assertNotIn("api_key_value", json.dumps(created_provider))

        status, provider_list = request_json(self.base_url, path="/v1/gateway/providers", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertIn("demo-provider", {provider["id"] for provider in provider_list["data"]})

        status, duplicate_provider = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/providers",
            api_key="dev-admin-key",
            payload={
                "provider_id": "demo-provider",
                "base_url": "https://example.com/v1",
                "api_key_env": "DEMO_PROVIDER_API_KEY",
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(duplicate_provider["error"]["code"], "provider_already_exists")

        status, bad_provider = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/providers",
            api_key="dev-admin-key",
            payload={
                "provider_id": "bad-provider",
                "type": "bad_type",
                "base_url": "https://example.com/v1",
                "api_key_env": "BAD_PROVIDER_API_KEY",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(bad_provider["error"]["code"], "invalid_provider_type")

        status, provider_route = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/model-routes",
            api_key="dev-admin-key",
            payload={
                "model_id": "demo-provider-route",
                "provider": "demo-provider",
                "upstream_model": "demo-model",
                "capabilities": ["chat"],
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(provider_route["model"]["provider"], "demo-provider")

        status, updated_provider = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/providers/update",
            api_key="dev-admin-key",
            payload={
                "provider_id": "demo-provider",
                "name": "Demo Provider Updated",
                "api_key_env": "DEMO_PROVIDER_API_KEY_2",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated_provider["object"], "provider.updated")
        self.assertEqual(updated_provider["provider"]["name"], "Demo Provider Updated")
        self.assertEqual(updated_provider["provider"]["api_key_env"], "DEMO_PROVIDER_API_KEY_2")

        status, disabled_provider = request_json(
            self.base_url,
            method="POST",
            path="/v1/gateway/providers/disable",
            api_key="dev-admin-key",
            payload={"provider_id": "demo-provider"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(disabled_provider["object"], "provider.disabled")
        self.assertIn("demo-provider-route", disabled_provider["disabled_model_routes"])

        status, catalog_after_provider_disable = request_json(
            self.base_url,
            path="/v1/gateway/model-catalog",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertNotIn("demo-provider-route", {model["id"] for model in catalog_after_provider_disable["data"]})

        status, provider_audit = request_json(
            self.base_url,
            path="/v1/gateway/audit-events?target_id=demo-provider",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        provider_actions = {event["action"] for event in provider_audit["data"]}
        self.assertIn("provider.created", provider_actions)
        self.assertIn("provider.updated", provider_actions)
        self.assertIn("provider.disabled", provider_actions)

        status, customer_usage = request_json(self.base_url, path="/v1/gateway/customer-usage", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        customer_ids = {row["id"] for row in customer_usage["data"]}
        self.assertIn("dev", customer_ids)

        status, customer_reports = request_json(self.base_url, path="/v1/gateway/customer-reports")
        self.assertEqual(status, 401)

        status, customer_reports = request_json(self.base_url, path="/v1/gateway/customer-reports", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        reports = {row["id"]: row for row in customer_reports["data"]}
        self.assertIn("dev", reports)
        self.assertIn("request_summary", reports["dev"])
        self.assertGreaterEqual(reports["dev"]["request_summary"]["requests"], 1)
        self.assertIn("usage_by_model", reports["dev"])
        self.assertIn("recent_requests", reports["dev"])
        self.assertNotIn("provider_api_keys", reports["dev"])

        status, customer_success = request_json(self.base_url, path="/v1/gateway/customer-success")
        self.assertEqual(status, 401)

        status, customer_success = request_json(self.base_url, path="/v1/gateway/customer-success", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertEqual(customer_success["object"], "gateway.customer_success")
        self.assertIn("totals", customer_success)
        self.assertIn("accounts", customer_success)
        accounts = {account["id"]: account for account in customer_success["accounts"]}
        self.assertIn("dev", accounts)
        self.assertIn(accounts["dev"]["health_status"], {"healthy", "watch", "at_risk"})
        self.assertIn("recommended_action", accounts["dev"])
        self.assertIn("meeting_questions", accounts["dev"])
        self.assertIn("business_summary", accounts["dev"])
        self.assertIn("request_activity", accounts["dev"]["evidence"])
        self.assertNotIn("provider_api_keys", json.dumps(customer_success))

        status, self_view = request_json(self.base_url, path="/v1/gateway/me")
        self.assertEqual(status, 401)
        self.assertEqual(self_view["error"]["code"], "missing_auth")

        status, self_view = request_json(self.base_url, path="/v1/gateway/me", api_key="wrong-key")
        self.assertEqual(status, 401)
        self.assertEqual(self_view["error"]["code"], "invalid_api_key")

        status, self_view = request_json(self.base_url, path="/v1/gateway/me", api_key="dev-gateway-key")
        self.assertEqual(status, 200)
        self.assertEqual(self_view["object"], "customer.gateway_profile")
        self.assertEqual(self_view["customer"]["id"], "dev")
        self.assertIn("budget", self_view)
        self.assertIn("budget_state", self_view)
        self.assertIn("invoice_preview", self_view)
        self.assertIn("usage_by_model", self_view)
        self.assertIn("recent_requests", self_view)
        self.assertNotIn("api_key", json.dumps(self_view))
        self.assertNotIn("provider_api_keys", json.dumps(self_view))
        self.assertGreaterEqual(len(self_view["models"]), 1)

        status, limited_view = request_json(self.base_url, path="/v1/gateway/me", api_key="demo-limited-key")
        self.assertEqual(status, 200)
        limited_models = {model["id"] for model in limited_view["models"]}
        self.assertEqual(limited_models, {"smart-fast"})
        self.assertEqual(limited_view["models"][0]["usage"]["scope"], "this_customer")
        self.assertEqual(limited_view["models"][0]["usage"]["total_tokens"], 0)
        self.assertGreaterEqual(limited_view["model_access"]["blocked_count"], 1)
        self.assertEqual(len(limited_view["invoice_preview"]["data"]), 1)
        self.assertEqual(limited_view["invoice_preview"]["data"][0]["customer"]["id"], "demo-limited")

        status, invoice = request_json(self.base_url, path="/v1/gateway/invoice-preview")
        self.assertEqual(status, 401)

        status, invoice = request_json(self.base_url, path="/v1/gateway/invoice-preview", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertIn("totals", invoice)
        self.assertGreaterEqual(invoice["totals"]["customers"], 1)
        invoice_by_customer = {row["customer"]["id"]: row for row in invoice["data"]}
        self.assertIn("dev", invoice_by_customer)
        self.assertIn("estimated_cost", invoice_by_customer["dev"])
        self.assertIn("usage_by_model", invoice_by_customer["dev"])
        self.assertIn("not a legal invoice", invoice_by_customer["dev"]["note"])

        status, invoice = request_json(
            self.base_url,
            path="/v1/gateway/invoice-preview?customer_id=dev",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(invoice["data"]), 1)
        self.assertEqual(invoice["data"][0]["customer"]["id"], "dev")

        status, invoice = request_json(
            self.base_url,
            path="/v1/gateway/invoice-preview?customer_id=missing",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 404)
        self.assertEqual(invoice["error"]["code"], "unknown_customer")

        status, csv_preview = request_text(
            self.base_url,
            path="/v1/gateway/invoice-preview?customer_id=dev&format=csv",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertIn("invoice_id,customer_id,plan", csv_preview)
        self.assertIn(",dev,internal-demo,", csv_preview)

        status, model_usage = request_json(self.base_url, path="/v1/gateway/model-usage", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        model_ids = {row["id"] for row in model_usage["data"]}
        self.assertIn("smart-fast", model_ids)

        status, request_summary = request_json(self.base_url, path="/v1/gateway/request-summary", api_key="dev-admin-key")
        self.assertEqual(status, 200)
        self.assertIn("by_customer", request_summary)
        self.assertIn("by_code", request_summary)

        status, activity = request_json(self.base_url, path="/v1/gateway/request-activity")
        self.assertEqual(status, 401)

        status, activity = request_json(
            self.base_url,
            path="/v1/gateway/request-activity?customer_id=dev&status=success&limit=5",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(activity["filters"]["customer_id"], "dev")
        self.assertEqual(activity["filters"]["status"], "success")
        self.assertLessEqual(len(activity["data"]), 5)
        self.assertTrue(any(row["customer_id"] == "dev" for row in activity["data"]))
        self.assertTrue(all(row["outcome"] == "success" for row in activity["data"]))
        self.assertIn("summary", activity["data"][0])
        self.assertNotIn("provider_api_keys", activity["data"][0])

    def test_admin_page_accepts_query_admin_key_for_browser_demo(self):
        request = urllib.request.Request(self.base_url + "/admin?admin_key=dev-admin-key")
        with urllib.request.urlopen(request, timeout=5) as response:
            html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("Gateway Admin", html)

    def test_dashboard_exposes_presenter_mode(self):
        status, html = request_text(self.base_url, path="/")
        self.assertEqual(status, 200)
        self.assertIn("Presenter mode", html)
        self.assertIn("demoScriptSteps", html)
        self.assertIn("/v1/gateway/demo-script?admin_key=", html)
        self.assertIn("15 minute customer walkthrough", html)
        self.assertIn("Layered architecture map", html)
        self.assertIn("architecture-map", html)
        self.assertIn("Provider adapters", html)
        self.assertIn("Gateway options comparison", html)
        self.assertIn("gatewayOptionsList", html)
        self.assertIn("/v1/gateway/decision-guide?admin_key=", html)
        self.assertIn("Production readiness", html)
        self.assertIn("readinessList", html)
        self.assertIn("Production launch plan", html)
        self.assertIn("launchGateList", html)
        self.assertIn("/v1/gateway/launch-plan?admin_key=", html)
        self.assertIn("Change management", html)
        self.assertIn("changeManagementList", html)
        self.assertIn("/v1/gateway/change-management?admin_key=", html)
        self.assertIn("Data governance", html)
        self.assertIn("dataGovernanceList", html)
        self.assertIn("/v1/gateway/data-governance?admin_key=", html)
        self.assertIn("Security review", html)
        self.assertIn("securityReviewList", html)
        self.assertIn("/v1/gateway/security-review?admin_key=", html)
        self.assertIn("Evaluation plan", html)
        self.assertIn("evaluationPlanList", html)
        self.assertIn("/v1/gateway/evaluation-plan?admin_key=", html)
        self.assertIn("Discovery checklist", html)
        self.assertIn("discoveryChecklistList", html)
        self.assertIn("/v1/gateway/discovery-checklist?admin_key=", html)
        self.assertIn("Proposal summary", html)
        self.assertIn("proposalSummaryList", html)
        self.assertIn("/v1/gateway/proposal-summary?admin_key=", html)
        self.assertIn("Deployment readiness", html)
        self.assertIn("deploymentReadinessList", html)
        self.assertIn("/v1/gateway/deployment-readiness?admin_key=", html)
        self.assertIn("Production backlog", html)
        self.assertIn("productionBacklogList", html)
        self.assertIn("/v1/gateway/production-backlog?admin_key=", html)
        self.assertIn("Pilot scorecard", html)
        self.assertIn("pilotScorecardList", html)
        self.assertIn("/v1/gateway/pilot-scorecard?admin_key=", html)
        self.assertIn("Handoff checklist", html)
        self.assertIn("handoffChecklistList", html)
        self.assertIn("/v1/gateway/handoff-checklist?admin_key=", html)
        self.assertIn("Customer success summary", html)
        self.assertIn("customerSuccessList", html)
        self.assertIn("Customer handoff package", html)
        self.assertIn("/openapi.json", html)
        self.assertIn("/postman_collection.json", html)
        self.assertIn("Model_Gateway_Customer_Guide.pdf", html)
        self.assertIn("SDK starter", html)
        self.assertIn("commandSdkStarter", html)
        self.assertIn("/v1/gateway/sdk-starter", html)
        self.assertIn("Customer onboarding plan", html)
        self.assertIn("/v1/gateway/onboarding-plan?admin_key=", html)
        self.assertIn("onboardingPlan", html)
        self.assertIn("Integration command starter", html)
        self.assertIn("commandListModels", html)
        self.assertIn("/v1/gateway/integration-guide", html)
        self.assertIn("Customer lifecycle", html)
        self.assertIn("createCustomerButton", html)
        self.assertIn("/v1/gateway/customers/rotate-key", html)
        self.assertIn("/v1/gateway/customers/disable", html)
        self.assertIn("Provider lifecycle", html)
        self.assertIn("createProviderButton", html)
        self.assertIn("/v1/gateway/providers/disable", html)
        self.assertIn("Model route lifecycle", html)
        self.assertIn("createRouteButton", html)
        self.assertIn("/v1/gateway/model-routes/disable", html)
        self.assertIn("Audit timeline", html)
        self.assertIn("auditList", html)
        self.assertIn("state.audit_events", html)

    def test_config_check_reports_demo_warnings(self):
        status, payload = request_json(self.base_url, path="/v1/gateway/config-check")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_admin_key")

        status, payload = request_json(
            self.base_url,
            path="/v1/gateway/config-check",
            api_key="dev-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "warning")
        codes = {check["code"] for check in payload["checks"]}
        self.assertIn("default_admin_key", codes)
        self.assertIn("demo_customer_keys", codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
