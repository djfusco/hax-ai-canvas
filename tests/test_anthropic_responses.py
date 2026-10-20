"""Synthetic Anthropic HTTP responses through the real SDK; no live inference."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]
from ai_client import AIClient, AIResponseError, _anthropic_request_options
from mentor_assessment import AssessmentError, assess_design
from test_mentor_assessment import item, payload, response_for, workspace


class AnthropicResponseTests(unittest.TestCase):
    def client(self, content, stop="end_turn", statuses=None, model="claude-sonnet-5-5", provider="anthropic"):
        requests = []
        statuses = list(statuses or [])
        def reply(request):
            requests.append(request)
            status = statuses.pop(0) if statuses else 200
            if status != 200:
                return httpx.Response(status, json={
                    "type": "error", "error": {"type": "api_error", "message": "private-upstream-fixture"}})
            blocks = content(request) if callable(content) else content
            return httpx.Response(200, json={
                "id": "msg_synthetic", "type": "message", "role": "assistant",
                "model": model, "content": blocks,
                "stop_reason": stop(request) if callable(stop) else stop,
                "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 10}})
        real_client = httpx.Client
        class FixtureClient(real_client):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(reply)
                super().__init__(*args, **kwargs)
        with patch("httpx.Client", FixtureClient):
            client = AIClient(provider, model=model,
                              api_key="synthetic-anthropic-fixture", isolated_credentials=True,
                              base_url="https://fixture.azure-api.net/anthropic" if provider == "nebula" else None)
        self.addCleanup(client.close)
        return client, requests

    @staticmethod
    def thinking():
        return {"type": "thinking", "thinking": "private-thinking-fixture", "signature": "synthetic"}

    def test_thinking_before_text_uses_final_answer_only_once(self):
        client, requests = self.client([self.thinking(), {"type": "text", "text": '{"reply":"Answer"}'}])
        self.assertEqual(client.call("System", "Prompt"), '{"reply":"Answer"}')
        self.assertEqual(len(requests), 1)
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], "claude-sonnet-5-5")
        self.assertNotIn("thinking", body)

    def test_reasoning_requests_use_medium_effort_and_bounded_headroom(self):
        client, requests = self.client([{"type": "text", "text": "Answer"}])
        for requested in (1400, 3500, 7000, 16000):
            client.call("System", "Prompt", max_tokens=requested)
            body = json.loads(requests[-1].content)
            self.assertEqual(body["max_tokens"], min(requested + 8192, 16384))
            self.assertEqual(body["output_config"], {"effort": "medium"})
            self.assertNotIn("thinking", body, "Do not disable thinking or send obsolete budget_tokens.")
        self.assertEqual(len(requests), 4)

    def test_unknown_models_and_gateways_keep_original_compatible_parameters(self):
        for provider, model in (
            ("anthropic", "claude-sonnet-4-5"),
            ("anthropic", "claude-future-unknown"),
            ("anthropic", "claude-sonnet-5-5-custom"),
            ("nebula", "claude-sonnet-5-5"),
        ):
            with self.subTest(provider=provider, model=model):
                client, requests = self.client([{"type": "text", "text": "Answer"}],
                                               model=model, provider=provider)
                client.call("System", "Prompt", max_tokens=3500)
                body = json.loads(requests[0].content)
                self.assertEqual(body["max_tokens"], 3500)
                self.assertNotIn("output_config", body)

    def test_unknown_dated_aliases_are_not_assumed_capable_and_legacy_calls_are_unchanged(self):
        self.assertEqual(_anthropic_request_options("anthropic", "claude-sonnet-5-5-20261001", 3500, True),
                         {"max_tokens": 3500})
        self.assertEqual(_anthropic_request_options("anthropic", "claude-sonnet-5-5", 3500, False),
                         {"max_tokens": 3500})
        self.assertEqual(_anthropic_request_options("anthropic", "claude-sonnet-5-5", 32768, True)["max_tokens"],
                         16384, "All isolated mentor calls retain the shared application spending ceiling.")

    def test_thirty_item_reasoning_scope_succeeds_with_one_request_per_item(self):
        def content(request):
            data = payload(json.loads(request.content)["messages"][0]["content"])
            return [self.thinking(), {"type": "text", "text": json.dumps(response_for(data))}]
        def stop(request):
            body = json.loads(request.content)
            # A synthetic provider requiring room for thinking plus a report.
            return ("end_turn" if body["max_tokens"] >= 8192 and
                    body.get("output_config", {}).get("effort") == "medium" else "max_tokens")
        client, requests = self.client(content, stop=stop)
        report = assess_design(workspace(), [item(str(index)) for index in range(30)], client, scope="baseline")
        self.assertEqual(report["coverage"]["items_evaluated"], 30)
        # 30 per-item assessment requests; synthesis adds more (best-effort, failure is safe)
        item_requests = [r for r in requests
                         if "Assess this one item" in json.loads(r.content)["messages"][0]["content"]]
        self.assertEqual(len(item_requests), 30)
        self.assertTrue(all(json.loads(r.content)["max_tokens"] == 11692 for r in item_requests))

    def test_redacted_thinking_and_multiple_text_blocks_are_combined(self):
        client, requests = self.client([
            {"type": "redacted_thinking", "data": "private-redaction-fixture"}, self.thinking(),
            {"type": "text", "text": '{"reply":"part'}, {"type": "text", "text": 'ial"}'}])
        self.assertEqual(json.loads(client.call("System", "Prompt")), {"reply": "partial"})
        self.assertEqual(len(requests), 1)

    def test_text_only_legacy_response_still_works(self):
        client, requests = self.client([{"type": "text", "text": "  Legacy answer  "}], model="claude-haiku-4-5")
        self.assertEqual(client.call("System", "Prompt"), "Legacy answer")
        self.assertNotIn("output_config", json.loads(requests[0].content))

    def test_thinking_only_and_empty_answers_are_not_retried(self):
        for content in ([self.thinking()], [], [{"type": "text", "text": "  "}]):
            with self.subTest(content_type=len(content)):
                client, requests = self.client(content)
                with patch("ai_client.time.sleep") as sleep:
                    with self.assertRaisesRegex(AIResponseError, "no answer text"):
                        client.call("System", "Prompt")
                sleep.assert_not_called()
                self.assertEqual(len(requests), 1)

    def test_truncated_text_or_thinking_only_is_not_accepted_or_retried(self):
        for stop in ("max_tokens", "model_context_window_exceeded"):
            with self.subTest(stop=stop):
                client, requests = self.client([self.thinking(), {"type": "text", "text": '{"partial":'}], stop)
                with self.assertRaisesRegex(AIResponseError, "Thinking uses" if stop == "max_tokens" else "context window"):
                    client.call("System", "Prompt")
                self.assertEqual(len(requests), 1)

    def test_refusal_and_unexpected_completion_are_safe_and_not_retried(self):
        for stop in ("refusal", "tool_use", "pause_turn"):
            with self.subTest(stop=stop):
                client, requests = self.client([{"type": "text", "text": "private-response-fixture"}], stop)
                with self.assertRaises(AIResponseError) as failure:
                    client.call("System", "Prompt")
                self.assertNotIn("private-response-fixture", str(failure.exception))
                self.assertEqual(len(requests), 1)

    def test_transient_http_failure_retries_once_without_nested_sdk_retries(self):
        client, requests = self.client([{"type": "text", "text": "Recovered"}], statuses=[503, 200])
        with patch("ai_client.time.sleep") as sleep:
            self.assertEqual(client.call("System", "Prompt"), "Recovered")
        self.assertEqual(len(requests), 2)
        sleep.assert_called_once_with(1)

    def test_deterministic_attribute_error_is_never_retried_or_logged_verbatim(self):
        client, _ = self.client([])
        client._call_once = Mock(side_effect=AttributeError("private-diagnostic-fixture"))
        with self.assertLogs("ai_client", level="WARNING") as log, patch("ai_client.time.sleep") as sleep:
            with self.assertRaises(AttributeError):
                client.call("System", "Prompt")
        self.assertEqual(client._call_once.call_count, 1)
        sleep.assert_not_called()
        self.assertNotIn("private-diagnostic-fixture", " ".join(log.output))

    def test_auth_or_bad_request_does_not_retry(self):
        client, requests = self.client([], statuses=[400])
        with patch("ai_client.time.sleep") as sleep:
            with self.assertRaises(Exception):
                client.call("System", "Prompt")
        self.assertEqual(len(requests), 1)
        sleep.assert_not_called()

    def test_real_sdk_thinking_response_completes_framework_assessment(self):
        def content(request):
            prompt = json.loads(request.content)["messages"][0]["content"]
            result = response_for(payload(prompt))
            return [self.thinking(), {"type": "text", "text": json.dumps(result)}]
        client, requests = self.client(content)
        report = assess_design(workspace(), [item()], client, scope="baseline")
        self.assertEqual(report["coverage"]["items_evaluated"], 1)
        self.assertNotIn("private-thinking-fixture", json.dumps(report))
        self.assertEqual(len(requests), 1)

    def test_token_limit_reaches_user_safe_assessment_error(self):
        client, requests = self.client([self.thinking()], "max_tokens")
        with self.assertRaisesRegex(AssessmentError, "baseline item 1 of 1.*output-token allowance") as error:
            assess_design(workspace(), [item()], client, scope="baseline")
        self.assertNotIn("private-thinking-fixture", str(error.exception))
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
