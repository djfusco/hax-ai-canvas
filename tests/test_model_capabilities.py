import copy
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from model_capabilities import (
    AUTO_OUTPUT_CAP, ModelCapabilityError, PROTOCOL_MARGIN,
    estimate_input_tokens, plan_request, resolve_capabilities, validate_capabilities,
)


class CapabilityTests(unittest.TestCase):
    def test_documented_exact_openai_profiles(self):
        cases = (
            ("gpt-4o", 128000, 128000, 16384, False, "chat_completions"),
            ("gpt-4o-2024-05-13", 128000, 128000, 4096, False, "chat_completions"),
            ("gpt-4o-mini-2024-07-18", 128000, 128000, 16384, False, "chat_completions"),
            ("gpt-4.1-mini-2025-04-14", 1047576, 1047576, 32768, False, "chat_completions"),
            ("gpt-5-2025-08-07", 400000, 272000, 128000, True, "chat_completions"),
            ("gpt-5-mini", 400000, 272000, 128000, True, "chat_completions"),
            ("gpt-5-nano", 400000, 272000, 128000, True, "chat_completions"),
            ("o3-2025-04-16", 200000, 200000, 100000, True, "chat_completions"),
            ("o4-mini", 200000, 200000, 100000, True, "chat_completions"),
            ("gpt-5-pro", 400000, 400000, 272000, True, "responses"),
            ("gpt-6-astra", 1050000, 922000, 128000, True, "chat_completions"),
        )
        for model, context, limit, output, reasoning, api in cases:
            with self.subTest(model=model):
                profile = resolve_capabilities("openai", model)
                self.assertEqual(profile["status"], "documented")
                self.assertEqual((profile["context_window"], profile["input_limit"],
                                  profile["output_limit"], profile["reasoning"], profile["api"]),
                                 (context, limit, output, reasoning, api))
                self.assertEqual(validate_capabilities("openai", model, profile), profile)
        pro = plan_request("openai", "gpt-5-pro", "", "Example", 3500)
        self.assertIsNone(pro["reasoning_effort"])
        self.assertEqual(pro["output_parameter"], "max_output_tokens")

    def test_exact_ids_do_not_invent_dated_or_future_support(self):
        for provider, model in (("openai", "gpt-5-enterprise"), ("openai", "gpt-5-20990101"),
                                ("openai", "gpt-6-astra-2026-10-01"),
                                ("anthropic", "claude-sonnet-5-5-20261001"),
                                ("anthropic", "claude-sonnet-99"),
                                ("gemini", "gemini-2.5-flash-unknown")):
            profile = resolve_capabilities(provider, model)
            self.assertEqual(profile["status"], "unverified")
            self.assertEqual((profile["input_limit"], profile["context_window"], profile["output_limit"]),
                             (32768, 32768, 4096))
            self.assertFalse(profile["reasoning"])
            self.assertIsNone(profile["effort"])
            self.assertTrue(any("Unverified" in w for w in profile["warnings"]))

    def test_gateway_names_never_inherit_direct_model_profiles(self):
        for model in ("claude-sonnet-5-5", "gpt-5-pro", "audio", "campus-production"):
            profile = resolve_capabilities("nebula", model)
            self.assertEqual(profile["status"], "unverified")
            self.assertEqual(profile["api"], "messages")
            self.assertIsNone(profile["effort"])
        profile = resolve_capabilities("nebula", "claude-sonnet-5-5",
                                       {"max_input_tokens": 64000, "max_tokens": 9000})
        self.assertEqual(profile["status"], "provider_metadata")
        self.assertEqual(profile["input_limit"], 64000)
        self.assertIsNone(profile["effort"])
        self.assertFalse(profile["reasoning"])

    def test_anthropic_advertised_limits_and_support_take_precedence(self):
        metadata = {"max_input_tokens": 80000, "max_tokens": 12000, "capabilities": {
            "thinking": {"supported": True, "types": {"adaptive": {"supported": True}}},
            "effort": {"supported": True, "medium": {"supported": True}},
            "structured_outputs": {"supported": True}},
            "lifecycle": "active", "url": "https://evil.example", "warnings": ["UPSTREAM SECRET"]}
        profile = resolve_capabilities("anthropic", "claude-sonnet-5-5", metadata)
        self.assertEqual(profile["status"], "provider_metadata")
        self.assertEqual(profile["input_limit"], 80000)
        self.assertEqual(profile["context_window"], 80000)
        self.assertEqual(profile["output_limit"], 12000)
        self.assertEqual(profile["effort"], "medium")
        self.assertTrue(profile["reasoning"])
        self.assertNotIn("evil", str(profile))
        self.assertNotIn("UPSTREAM", str(profile))
        self.assertNotIn("structured_outputs", profile)
        self.assertEqual(validate_capabilities("anthropic", "claude-sonnet-5-5", profile), profile)

    def test_false_support_flags_override_documented_fallback(self):
        profile = resolve_capabilities("anthropic", "claude-sonnet-5-5", {"capabilities": {
            "thinking": {"supported": False}, "effort": {
                "supported": False, "medium": {"supported": True}}}})
        self.assertFalse(profile["reasoning"])
        self.assertIsNone(profile["effort"])
        self.assertTrue(any("partial" in w for w in profile["warnings"]))

    def test_metadata_model_scope_and_partial_provenance_are_explicit(self):
        wrong = resolve_capabilities("anthropic", "future-text", {
            "id": "different", "max_input_tokens": 1000000, "max_tokens": 128000})
        self.assertEqual(wrong["status"], "unverified")
        self.assertEqual(wrong["input_limit"], 32768)
        partial = resolve_capabilities("anthropic", "claude-sonnet-5-5", {
            "capabilities": {"effort": {"medium": {"supported": False}}}})
        self.assertEqual(partial["status"], "provider_metadata")
        self.assertEqual(partial["input_limit"], 1000000)
        self.assertTrue(any("partial" in w for w in partial["warnings"]))
        retired = resolve_capabilities("openai", "gpt-5-pro", {"lifecycle": "retired"})
        self.assertEqual(validate_capabilities("openai", "gpt-5-pro", retired), retired)

    def test_invalid_raw_metadata_never_enlarges_limits(self):
        for bad in (True, False, -1, 0, 1.5, float("nan"), float("inf"), "1000000",
                    10000001, [], {}, "https://evil.example"):
            with self.subTest(bad=bad):
                profile = resolve_capabilities("anthropic", "claude-sonnet-5-5",
                                               {"max_input_tokens": bad, "max_tokens": bad})
                self.assertEqual(profile["input_limit"], 1000000)
                self.assertEqual(profile["output_limit"], 128000)
                self.assertTrue(any("invalid" in w for w in profile["warnings"]))
        profile = resolve_capabilities("anthropic", "new-text",
                                       {"max_input_tokens": 12000, "max_tokens": -3})
        self.assertEqual(profile["input_limit"], 12000)
        self.assertEqual(profile["output_limit"], 4096)

    def test_openai_listing_cannot_fabricate_undocumented_protocol_fields(self):
        profile = resolve_capabilities("openai", "unknown", {
            "max_tokens": 1000000, "context_window": 9999999, "api": "responses",
            "source": "trusted", "capabilities": {"effort": {"medium": {"supported": True}}}})
        self.assertEqual(profile["status"], "unverified")
        self.assertEqual(profile["output_limit"], 4096)
        self.assertEqual(profile["api"], "chat_completions")
        self.assertIsNone(profile["effort"])

    def test_gemini_metadata_is_independent_input_output_and_thinking(self):
        profile = resolve_capabilities("gemini", "future-text", {
            "inputTokenLimit": 50000, "outputTokenLimit": 25000,
            "supportedGenerationMethods": ["generateContent", "countTokens"], "thinking": True})
        self.assertEqual(profile["status"], "provider_metadata")
        self.assertIsNone(profile["context_window"])
        self.assertTrue(profile["reasoning"])
        self.assertIsNone(profile["effort"])
        result = plan_request("gemini", "future-text", "", "x" * (50000 - PROTOCOL_MARGIN), 4096, profile)
        self.assertEqual(result["input_estimate"], 50000)
        self.assertEqual(result["available_input_tokens"], 50000)

    def test_unsupported_models_and_provider_retirement_block_offline(self):
        for provider, model, metadata in (
                ("openai", "text-embedding-3-small", None),
                ("openai", "gpt-3.5-turbo-instruct", None),
                ("anthropic", "claude-opus-4-20250514", None),
                ("anthropic", "future-text", {"lifecycle": "retired"}),
                ("gemini", "future-text", {"supportedGenerationMethods": ["embedContent"]})):
            profile = resolve_capabilities(provider, model, metadata)
            self.assertEqual(profile["status"], "unsupported")
            self.assertEqual(validate_capabilities(provider, model, profile), profile)
            with self.assertRaises(ModelCapabilityError):
                plan_request(provider, model, "", "Test", 4096, profile)

    def test_snapshot_validation_is_strict_and_copies_mutables(self):
        original = resolve_capabilities("openai", "gpt-4o")
        for field, bad in (
                ("version", True), ("version", 2), ("provider", "nebula"), ("model", "other"),
                ("input_limit", True), ("input_limit", 0), ("input_limit", 10000001),
                ("output_limit", "4096"), ("output_limit", float("inf")),
                ("context_window", 10), ("api", "https://evil.example"), ("api", []),
                ("effort", "high"), ("reasoning", 1), ("status", "trusted"),
                ("output_parameter", "endpoint"), ("source", "https://evil.example"),
                ("warnings", ["Arbitrary safe-looking upstream warning"]), ("warnings", "text")):
            with self.subTest(field=field, bad=bad), self.assertRaises(ModelCapabilityError):
                validate_capabilities("openai", "gpt-4o", {**original, field: bad})
        with self.assertRaises(ModelCapabilityError):
            validate_capabilities("openai", "gpt-4o", {**original, "endpoint": "evil"})
        with self.assertRaises(ModelCapabilityError):
            validate_capabilities("openai", "gpt-4o", {"input_limit": 1000})
        copied = validate_capabilities("openai", "gpt-4o", original)
        copied["warnings"].clear()
        self.assertTrue(original["warnings"])

    def test_utf8_estimate_is_conservative_and_not_character_division(self):
        self.assertEqual(estimate_input_tokens("", ""), PROTOCOL_MARGIN)
        self.assertEqual(estimate_input_tokens("é漢", "🙂"), 9 + PROTOCOL_MARGIN)
        self.assertGreater(estimate_input_tokens("", "🙂" * 100), estimate_input_tokens("", "a" * 100))
        for bad in (None, b"bytes", 4, []):
            with self.assertRaises(ModelCapabilityError):
                estimate_input_tokens("", bad)
        with self.assertRaises(ModelCapabilityError):
            estimate_input_tokens("", "\ud800")

    def test_output_clamps_and_sonnet_assessment_allowance(self):
        result = plan_request("anthropic", "claude-sonnet-5-5", "System", "Prompt", 3500)
        self.assertEqual(result["max_output_tokens"], 11692)
        self.assertEqual(result["reasoning_effort"], "medium")
        for model in ("gpt-4o", "gpt-5", "gpt-5-pro"):
            self.assertLessEqual(plan_request("openai", model, "", "", 100000)["max_output_tokens"], AUTO_OUTPUT_CAP)
        self.assertEqual(plan_request("openai", "gpt-4o-2024-05-13", "", "", 8000)["max_output_tokens"], 4096)
        for invalid in (True, 0, -1, 1.5, "4096", 2000001):
            with self.assertRaises(ModelCapabilityError):
                plan_request("openai", "gpt-4o", "", "", invalid)

    def test_combined_context_boundary_does_not_shrink_output(self):
        profile = resolve_capabilities("nebula", "small", {"max_input_tokens": 5000, "max_tokens": 1000})
        result = plan_request("nebula", "small", "", "a" * (4000 - PROTOCOL_MARGIN), 1000, profile)
        self.assertEqual(result["input_estimate"], 4000)
        self.assertEqual(result["max_output_tokens"], 1000)
        self.assertEqual(result["available_input_tokens"], 4000)
        with self.assertRaises(ModelCapabilityError):
            plan_request("nebula", "small", "", "a" * (4001 - PROTOCOL_MARGIN), 1000, profile)
        self.assertEqual(profile["output_limit"], 1000)

    def test_distinct_input_ceiling_and_too_small_answer_fail(self):
        profile = resolve_capabilities("openai", "gpt-6-astra")
        self.assertEqual(plan_request("openai", "gpt-6-astra", "", "", 4096)["available_input_tokens"], 922000)
        with self.assertRaises(ModelCapabilityError):
            plan_request("openai", "gpt-6-astra", "", "x" * 922000, 4096)
        tiny = resolve_capabilities("nebula", "tiny", {"max_input_tokens": 1000, "max_tokens": 200})
        with self.assertRaises(ModelCapabilityError):
            plan_request("nebula", "tiny", "", "", 4096, tiny)

    def test_planner_is_offline_and_does_not_mutate_snapshot(self):
        profile = resolve_capabilities("anthropic", "claude-sonnet-5-5")
        original = copy.deepcopy(profile)
        with patch.object(socket, "create_connection", side_effect=AssertionError("network prohibited")):
            result = plan_request("anthropic", "claude-sonnet-5-5", "S", "P", 3500, profile)
            self.assertEqual(result["input_estimate"], PROTOCOL_MARGIN + 2)
            with self.assertRaises(ModelCapabilityError):
                plan_request("anthropic", "claude-sonnet-5-5", "", "x" * 1000000, 3500, profile)
        self.assertEqual(profile, original)


if __name__ == "__main__":
    unittest.main()
