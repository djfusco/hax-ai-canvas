"""Real provider SDKs with synthetic HTTP transports: never live model access."""
import copy
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from ai_client import AIClient, AIResponseError, HAS_ANTHROPIC, HAS_GEMINI, HAS_OPENAI
from model_capabilities import ModelCapabilityError, resolve_capabilities
from mentor_assessment import (
    response_schema, assess_design, assessment_current, AssessmentError, SYSTEM, MAX_TOKENS,
)
from test_mentor_assessment import item, workspace, payload, response_for


@unittest.skipUnless(HAS_ANTHROPIC and HAS_OPENAI and HAS_GEMINI, "Install repository provider SDK dependencies.")
class ModelClientMatrixTests(unittest.TestCase):
    def client(self, provider, model, response=None, *, capabilities=None, statuses=None):
        requests = []
        statuses = list(statuses or [])

        def reply(request):
            requests.append(request)
            status = statuses.pop(0) if statuses else 200
            if status != 200:
                return httpx.Response(status, json={
                    "error": {"code": status, "type": "api_error", "status": "UNAVAILABLE",
                              "message": "private-upstream-fixture"},
                    "type": "error"})
            body = response(request) if callable(response) else response
            return httpx.Response(200, json=body)

        real_client = httpx.Client

        class FixtureClient(real_client):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(reply)
                super().__init__(*args, **kwargs)

        # Every synchronous SDK request is intercepted; the socket guard also
        # fails closed if an SDK ever ignores the injected transport.
        with patch("httpx.Client", FixtureClient), patch.object(
                socket, "create_connection", side_effect=AssertionError("Live network prohibited")):
            client = AIClient(provider, model=model, api_key="synthetic-provider-fixture",
                              isolated_credentials=True, capabilities=capabilities,
                              base_url="https://fixture.azure-api.net/anthropic" if provider == "nebula" else None)
        self.addCleanup(client.close)
        return client, requests

    @staticmethod
    def chat(model="gpt-4o", *, reason="stop", content="  Final answer  ", refusal=None):
        return {"id": "chatcmpl_fixture", "object": "chat.completion", "created": 1, "model": model,
                "choices": [{"index": 0, "finish_reason": reason,
                             "message": {"role": "assistant", "content": content, "refusal": refusal}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    @staticmethod
    def responses(*, status="completed", output=None):
        return {"id": "resp_fixture", "object": "response", "created_at": 1,
                "model": "gpt-5-pro", "status": status, "error": None,
                "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
                "output": output if output is not None else [
                    {"id": "rs_fixture", "type": "reasoning",
                     "summary": [{"type": "summary_text", "text": "private-thinking-fixture"}]},
                    {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                     "content": [{"type": "output_text", "text": "Final answer", "annotations": []}]}]}

    @staticmethod
    def gemini(*, reason="STOP", parts=None, blocked=None):
        body = {"candidates": [{"content": {"role": "model", "parts": parts if parts is not None else [
            {"text": "Final answer"}]}, "finishReason": reason, "index": 0}],
                "modelVersion": "gemini-2.5-flash",
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}
        if blocked:
            body["promptFeedback"] = {"blockReason": blocked}
        return body

    @staticmethod
    def anthropic(model="claude-sonnet-5-5"):
        return {"id": "msg_fixture", "type": "message", "role": "assistant", "model": model,
                "content": [{"type": "text", "text": "Final answer"}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1}}

    def call(self, client, *args, **kwargs):
        with patch.object(socket, "create_connection", side_effect=AssertionError("Live network prohibited")):
            return client.call(*args, **kwargs)

    def assert_timeout(self, request, seconds):
        timeout = request.extensions.get("timeout", {})
        self.assertEqual(timeout.get("read"), seconds, timeout)

    def test_native_anthropic_schema_and_effort_are_merged(self):
        for model in ("claude-sonnet-4-5", "claude-sonnet-4-6", "claude-sonnet-5-5"):
            client, requests = self.client("anthropic", model, self.anthropic(model))
            self.assertEqual(client.call_structured("System", "Prompt", response_schema()), "Final answer")
            body = json.loads(requests[0].content)
            self.assertEqual(body["output_config"]["format"],
                             {"type": "json_schema", "schema": response_schema()})
            if model != "claude-sonnet-4-5":
                self.assertEqual(body["output_config"]["effort"], "medium")
            self.assertLessEqual(body["max_tokens"], 16384)
            self.assertEqual(len(requests), 1)

    def test_native_openai_chat_and_responses_schema_formats(self):
        for model in ("gpt-4o", "gpt-5", "gpt-5-pro"):
            response = self.responses() if model == "gpt-5-pro" else self.chat(model)
            client, requests = self.client("openai", model, response)
            self.assertEqual(client.call_structured("System", "Prompt", response_schema()), "Final answer")
            body = json.loads(requests[0].content)
            config = (body["text"]["format"] if model == "gpt-5-pro"
                      else body["response_format"]["json_schema"])
            self.assertEqual(config["schema"], response_schema())
            self.assertIs(config["strict"], True)
            self.assertIs(body["store"], False)
            self.assertEqual(len(requests), 1)

    def test_native_gemini_json_schema_and_mime_are_serialized(self):
        client, requests = self.client("gemini", "gemini-2.5-flash", self.gemini())
        self.assertEqual(client.call_structured("System", "Prompt", response_schema(),
                                                timeout=77), "Final answer")
        config = json.loads(requests[0].content)["generationConfig"]
        self.assertEqual(config["responseJsonSchema"], response_schema())
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertLessEqual(config["maxOutputTokens"], 16384)
        self.assert_timeout(requests[0], 77)

    def test_unknown_gateway_legacy_and_explicit_false_do_not_receive_native_options(self):
        disabled = resolve_capabilities("anthropic", "claude-sonnet-5-5", {
            "capabilities": {"structured_outputs": {"supported": False}}})
        for provider, model, response, profile in (
                ("nebula", "claude-sonnet-5-5", self.anthropic(), None),
                ("anthropic", "future-text", self.anthropic("future-text"), None),
                ("anthropic", "claude-sonnet-5-5", self.anthropic(), disabled),
                ("openai", "gpt-4o-2024-05-13", self.chat(), None),
                ("openai", "future-text", self.chat(), None),
                ("gemini", "future-text", self.gemini(), None)):
            client, requests = self.client(provider, model, response, capabilities=profile)
            client.call_structured("System", "Prompt", response_schema())
            body = json.loads(requests[0].content)
            self.assertNotIn("format", body.get("output_config", {}))
            self.assertNotIn("response_format", body)
            self.assertNotIn("responseJsonSchema", body.get("generationConfig", {}))
            self.assertEqual(client.preflight_structured("System", "Prompt", response_schema()),
                             client.preflight("System", "Prompt"))
            self.assertEqual(len(requests), 1)

    def test_native_schema_bytes_are_budgeted_before_any_network_call(self):
        profile = resolve_capabilities("openai", "gpt-4o")
        profile.update(input_limit=1000, context_window=5096)
        client, requests = self.client("openai", "gpt-4o", self.chat(), capabilities=profile)
        client.preflight("System", "Prompt", max_tokens=100)
        with self.assertRaises(ModelCapabilityError):
            client.call_structured("System", "Prompt", response_schema(), max_tokens=100)
        self.assertEqual(requests, [])
        normal, requests = self.client("openai", "gpt-4o", self.chat())
        plain = normal.preflight("System", "Prompt")
        structured = normal.preflight_structured("System", "Prompt", response_schema())
        overhead = len(json.dumps(response_schema(), ensure_ascii=False,
                                  separators=(",", ":")).encode()) + len("\nRESPONSE JSON SCHEMA:\n")
        self.assertEqual(structured["input_estimate"] - plain["input_estimate"], overhead)

    def test_schema_requests_keep_completion_checks_and_never_downgrade_or_retry(self):
        for provider, model, response in (
                ("anthropic", "claude-sonnet-5-5", {**self.anthropic(), "stop_reason": "max_tokens"}),
                ("openai", "gpt-4o", self.chat(reason="length")),
                ("openai", "gpt-5-pro", self.responses(status="incomplete")),
                ("gemini", "gemini-2.5-flash", self.gemini(reason="MAX_TOKENS"))):
            client, requests = self.client(provider, model, response)
            with patch("ai_client.time.sleep") as sleep, self.assertRaises(AIResponseError):
                client.call_structured("System", "Prompt", response_schema())
            sleep.assert_not_called()
            self.assertEqual(len(requests), 1)
        client, requests = self.client("anthropic", "claude-sonnet-5-5", self.anthropic(), statuses=[400])
        with self.assertRaises(Exception):
            client.call_structured("System", "Prompt", response_schema())
        self.assertEqual(len(requests), 1)

    def test_real_sdk_assessment_chunks_account_for_native_schema_and_reject_fake_quotes(self):
        def response(request):
            body = json.loads(request.content)
            data = payload(body["messages"][-1]["content"])
            return self.chat(content=json.dumps(response_for(data)))
        profile = resolve_capabilities("openai", "gpt-4o")
        profile.update(input_limit=18000, context_window=22096, output_limit=4096)
        client, requests = self.client("openai", "gpt-4o", response, capabilities=profile)
        original = item(html="<p>" + "Compare documented evidence. " * 500 + "</p>")
        report = assess_design(workspace(), [original], client)
        self.assertGreater(len(requests), 1)
        self.assertTrue(assessment_current([original], report))
        self.assertEqual(report["coverage"]["chunks_evaluated"], len(requests))
        for request in requests:
            body = json.loads(request.content)
            plan = client.preflight_structured(SYSTEM, body["messages"][-1]["content"],
                                               response_schema(), max_tokens=MAX_TOKENS)
            self.assertLessEqual(plan["input_estimate"], 18000)
            self.assertEqual(body["response_format"]["json_schema"]["schema"], response_schema())

        def forged(request):
            data = payload(json.loads(request.content)["messages"][-1]["content"])
            value = response_for(data)
            value["evidence"][0]["quote"] = "PRIVATE_FABRICATED_QUOTE"
            return self.chat(content=json.dumps(value))
        client, requests = self.client("openai", "gpt-4o", forged)
        with self.assertRaisesRegex(AssessmentError, "evidence quote did not exactly match") as error:
            assess_design(workspace(), [item()], client)
        self.assertNotIn("PRIVATE_FABRICATED_QUOTE", str(error.exception))
        self.assertEqual(len(requests), 1)

    def test_openai_chat_normal_budget_storage_role_and_explicit_timeout(self):
        client, requests = self.client("openai", "gpt-4o", self.chat())
        self.assertEqual(self.call(client, "System", "Prompt", max_tokens=3500, timeout=73), "Final answer")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        body = json.loads(request.content)
        self.assertEqual(request.url.path, "/v1/chat/completions")
        self.assertEqual(body["max_completion_tokens"], 3500)
        self.assertIs(body["store"], False)
        self.assertEqual(body["messages"][0], {"role": "system", "content": "System"})
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)
        self.assert_timeout(request, 73)
        self.assertEqual(client._client.max_retries, 0)

    def test_openai_reasoning_chat_uses_developer_medium_and_single_bounded_allowance(self):
        client, requests = self.client("openai", "gpt-5", self.chat("gpt-5"))
        self.assertEqual(self.call(client, "System", "Prompt", max_tokens=3500), "Final answer")
        body = json.loads(requests[0].content)
        self.assertEqual(body["messages"][0]["role"], "developer")
        self.assertEqual(body["reasoning_effort"], "medium")
        self.assertEqual(body["max_completion_tokens"], 11692)
        self.assertIs(body["store"], False)
        self.assertEqual(len(requests), 1)

    def test_openai_responses_only_profile_omits_unsupported_medium_effort(self):
        client, requests = self.client("openai", "gpt-5-pro", self.responses())
        self.assertEqual(self.call(client, "System", "Prompt", max_tokens=3500, timeout=81), "Final answer")
        request = requests[0]
        body = json.loads(request.content)
        self.assertEqual(request.url.path, "/v1/responses")
        self.assertEqual(body["max_output_tokens"], 11692)
        self.assertEqual(body["instructions"], "System")
        self.assertEqual(body["input"], "Prompt")
        self.assertIs(body["store"], False)
        self.assertNotIn("reasoning", body)
        self.assertNotIn("messages", body)
        self.assert_timeout(request, 81)

    def test_gemini_real_config_validates_output_and_timeout_without_guessing_thinking_level(self):
        client, requests = self.client("gemini", "gemini-2.5-flash", self.gemini())
        self.assertEqual(self.call(client, "System", "Prompt", max_tokens=3500, timeout=79), "Final answer")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        body = json.loads(request.content)
        self.assertEqual(request.url.path, "/v1beta/models/gemini-2.5-flash:generateContent")
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 11692)
        self.assertNotIn("thinkingConfig", body["generationConfig"])
        self.assertNotIn("http_options", body["generationConfig"])
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "System\n\nPrompt")
        self.assert_timeout(request, 79)

    def test_gemini_thoughts_are_excluded_and_text_blocks_combined(self):
        client, requests = self.client("gemini", "gemini-2.5-flash", self.gemini(parts=[
            {"text": "private-thinking-fixture", "thought": True},
            {"text": "Final "}, {"text": "answer"}]))
        self.assertEqual(self.call(client, "System", "Prompt"), "Final answer")
        self.assertEqual(len(requests), 1)

    def test_anthropic_advertised_cap_clamps_output_and_false_effort_is_honored(self):
        capabilities = resolve_capabilities("anthropic", "claude-sonnet-5-5", {
            "max_input_tokens": 64000, "max_tokens": 5000,
            "capabilities": {"thinking": {"supported": True},
                             "effort": {"medium": {"supported": False}}}})
        client, requests = self.client("anthropic", "claude-sonnet-5-5", self.anthropic(),
                                       capabilities=capabilities)
        self.assertEqual(self.call(client, "System", "Prompt", max_tokens=3500, timeout=77), "Final answer")
        body = json.loads(requests[0].content)
        self.assertEqual(body["max_tokens"], 5000)
        self.assertNotIn("output_config", body)
        self.assertNotIn("thinking", body)
        self.assertEqual(client._client.max_retries, 0)
        self.assert_timeout(requests[0], 77)

    def test_gateway_opaque_deployment_has_no_speculative_effort(self):
        client, requests = self.client("nebula", "course-image-reviewer",
                                       self.anthropic("course-image-reviewer"))
        self.assertEqual(self.call(client, "System", "Prompt", max_tokens=3500), "Final answer")
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], "course-image-reviewer")
        self.assertEqual(body["max_tokens"], 3500)
        self.assertNotIn("output_config", body)
        self.assertNotIn("thinking", body)
        self.assertEqual(requests[0].url.host, "fixture.azure-api.net")

    def test_preflight_oversize_and_unsupported_models_send_zero_http_requests(self):
        for provider, model, response in (
                ("openai", "gpt-4o", self.chat()),
                ("openai", "text-embedding-3-small", self.chat()),
                ("anthropic", "claude-sonnet-5-5", self.anthropic()),
                ("gemini", "gemini-2.5-flash", self.gemini())):
            with self.subTest(provider=provider, model=model):
                client, requests = self.client(provider, model, response)
                with patch("ai_client.time.sleep") as sleep, self.assertRaises(ModelCapabilityError):
                    self.call(client, "System", "x" * 1100000, max_tokens=4096)
                self.assertEqual(requests, [])
                sleep.assert_not_called()
        client, requests = self.client("openai", "text-embedding-3-small", self.chat())
        with self.assertRaises(ModelCapabilityError):
            self.call(client, "", "Small prompt")
        self.assertEqual(requests, [])

    def test_offline_budget_and_fit_have_no_http_side_effects(self):
        client, requests = self.client("openai", "gpt-5", self.chat())
        self.assertTrue(client.fits("System", "Prompt", 3500))
        self.assertFalse(client.fits("", "x" * 400000, 3500))
        self.assertEqual(client.input_budget(3500), 272000)
        self.assertEqual(client.preflight("System", "Prompt", 3500)["max_output_tokens"], 11692)
        self.assertEqual(requests, [])

    def test_openai_chat_incomplete_refusal_and_tool_only_responses_are_not_retried(self):
        cases = (self.chat(reason="length", content="private-answer-fixture"),
                 self.chat(refusal="private-refusal-fixture"),
                 self.chat(reason="content_filter", content="private-filter-fixture"),
                 self.chat(reason="tool_calls", content=None),
                 self.chat(content=None), self.chat(content="  "))
        for response in cases:
            with self.subTest(reason=response["choices"][0]["finish_reason"]):
                client, requests = self.client("openai", "gpt-5", response)
                with patch("ai_client.time.sleep") as sleep, self.assertRaises(AIResponseError) as failure:
                    self.call(client, "System", "Prompt")
                self.assertNotIn("private-", str(failure.exception))
                sleep.assert_not_called()
                self.assertEqual(len(requests), 1)

    def test_responses_incomplete_refusal_tool_and_unfinished_message_are_not_retried(self):
        unfinished = self.responses()
        unfinished["output"][1]["status"] = "in_progress"
        refusal = self.responses(output=[{
            "id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "refusal", "refusal": "private-refusal-fixture"}]}])
        tool = self.responses(output=[{
            "id": "fc_fixture", "type": "function_call", "call_id": "call_fixture",
            "name": "unsupported", "arguments": "{}", "status": "completed"}])
        for response in (self.responses(status="incomplete"), self.responses(status="failed"),
                         unfinished, refusal, tool, self.responses(output=[])):
            with self.subTest(status=response["status"]):
                client, requests = self.client("openai", "gpt-5-pro", response)
                with patch("ai_client.time.sleep") as sleep, self.assertRaises(AIResponseError) as failure:
                    self.call(client, "System", "Prompt")
                self.assertNotIn("private-", str(failure.exception))
                sleep.assert_not_called()
                self.assertEqual(len(requests), 1)

    def test_gemini_blocked_truncated_thought_only_and_tools_are_not_retried(self):
        cases = (self.gemini(blocked="SAFETY"),
                 self.gemini(reason="MAX_TOKENS", parts=[{"text": "private-partial-fixture"}]),
                 self.gemini(reason="SAFETY"),
                 self.gemini(parts=[{"text": "private-thinking-fixture", "thought": True}]),
                 self.gemini(parts=[{"functionCall": {"name": "unsupported", "args": {}}}]))
        for response in cases:
            with self.subTest(reason=response["candidates"][0]["finishReason"]):
                client, requests = self.client("gemini", "gemini-2.5-flash", response)
                with patch("ai_client.time.sleep") as sleep, self.assertRaises(AIResponseError) as failure:
                    self.call(client, "System", "Prompt")
                self.assertNotIn("private-", str(failure.exception))
                sleep.assert_not_called()
                self.assertEqual(len(requests), 1)

    def test_transient_retry_has_no_nested_sdk_retry_or_body_logging(self):
        for provider, model, response in (
                ("openai", "gpt-5", self.chat("gpt-5")),
                ("openai", "gpt-5-pro", self.responses()),
                ("anthropic", "claude-sonnet-5-5", self.anthropic()),
                ("gemini", "gemini-2.5-flash", self.gemini())):
            with self.subTest(provider=provider, model=model):
                client, requests = self.client(provider, model, response, statuses=[503, 200])
                with self.assertLogs("ai_client", level="WARNING") as log, patch("ai_client.time.sleep") as sleep:
                    self.assertEqual(self.call(client, "System", "Prompt"), "Final answer")
                self.assertEqual(len(requests), 2, "Only application-level retry is allowed.")
                sleep.assert_called_once_with(1)
                self.assertNotIn("private-upstream-fixture", " ".join(log.output))

    def test_deterministic_http_failure_is_single_request_and_logs_no_body(self):
        for provider, model, response in (
                ("openai", "gpt-4o", self.chat()),
                ("anthropic", "claude-sonnet-5-5", self.anthropic()),
                ("gemini", "gemini-2.5-flash", self.gemini())):
            with self.subTest(provider=provider):
                client, requests = self.client(provider, model, response, statuses=[400])
                with self.assertLogs("ai_client", level="WARNING") as log, patch("ai_client.time.sleep") as sleep:
                    with self.assertRaises(Exception):
                        self.call(client, "System", "Prompt")
                self.assertEqual(len(requests), 1)
                sleep.assert_not_called()
                self.assertNotIn("private-upstream-fixture", " ".join(log.output))

    def test_snapshot_is_copied_before_requests_and_never_interprets_endpoint_fields(self):
        profile = resolve_capabilities("anthropic", "claude-sonnet-5-5", {
            "max_input_tokens": 64000, "max_tokens": 5000,
            "url": "https://evil.example", "api": "https://evil.example",
            "source": "private-upstream-fixture"})
        original = copy.deepcopy(profile)
        client, requests = self.client("anthropic", "claude-sonnet-5-5", self.anthropic(),
                                       capabilities=profile)
        profile["output_limit"] = 8000
        profile["warnings"].append("private-mutated-fixture")
        self.assertEqual(self.call(client, "System", "Prompt"), "Final answer")
        self.assertEqual(json.loads(requests[0].content)["max_tokens"], 5000)
        self.assertEqual(requests[0].url.host, "api.anthropic.com")
        self.assertEqual(client.capabilities, original)


if __name__ == "__main__":
    unittest.main()
