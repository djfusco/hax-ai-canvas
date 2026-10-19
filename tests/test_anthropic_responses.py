"""Synthetic Anthropic HTTP responses through the real SDK; no live inference."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]
from ai_client import AIClient, AIResponseError
from mentor_assessment import AssessmentError, assess_design
from test_mentor_assessment import item, payload, response_for, workspace


class AnthropicResponseTests(unittest.TestCase):
    def client(self, content, stop="end_turn", statuses=None):
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
                "model": "claude-sonnet-5-5", "content": blocks, "stop_reason": stop,
                "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 10}})
        real_client = httpx.Client
        class FixtureClient(real_client):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(reply)
                super().__init__(*args, **kwargs)
        with patch("httpx.Client", FixtureClient):
            client = AIClient("anthropic", model="claude-sonnet-5-5",
                              api_key="synthetic-anthropic-fixture", isolated_credentials=True)
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
        self.assertNotIn("thinking", body, "Do not silently change model thinking configuration")

    def test_redacted_thinking_and_multiple_text_blocks_are_combined(self):
        client, requests = self.client([
            {"type": "redacted_thinking", "data": "private-redaction-fixture"}, self.thinking(),
            {"type": "text", "text": '{"reply":"part'}, {"type": "text", "text": 'ial"}'}])
        self.assertEqual(json.loads(client.call("System", "Prompt")), {"reply": "partial"})
        self.assertEqual(len(requests), 1)

    def test_text_only_legacy_response_still_works(self):
        client, _ = self.client([{"type": "text", "text": "  Legacy answer  "}])
        self.assertEqual(client.call("System", "Prompt"), "Legacy answer")

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
                with self.assertRaisesRegex(AIResponseError, "Thinking uses"):
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
        with self.assertRaisesRegex(AssessmentError, "baseline item 1 of 1.*output or context limit") as error:
            assess_design(workspace(), [item()], client, scope="baseline")
        self.assertNotIn("private-thinking-fixture", str(error.exception))
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
