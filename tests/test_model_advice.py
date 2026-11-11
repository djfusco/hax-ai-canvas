"""Model suggestions for Course Consultant."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python")]
from model_advice import advise, annotate


class ModelAdviceTests(unittest.TestCase):
    def test_tiers(self):
        cases = {
            "anthropic/claude-sonnet-4.5": "recommended", "claude-sonnet-4-5-20250929": "recommended",
            "openai/gpt-4.1": "recommended", "gpt-4o": "recommended", "google/gemini-2.5-flash": "recommended",
            "anthropic/claude-opus-4.1": "slow", "claude-opus-4-1": "slow", "openai/o3": "slow", "o4-mini": "slow",
            "openai/gpt-5": "slow", "deepseek/deepseek-r1": "slow",
            "openai/gpt-4.1-nano": "small", "anthropic/claude-3.5-haiku": "small", "gpt-4o-mini": "small",
            "google/gemini-2.5-flash-lite": "small", "meta-llama/llama-3.1-8b-instruct": "small",
            "mistralai/mistral-large": "ok",
        }
        for model, tier in cases.items():
            with self.subTest(model=model):
                self.assertEqual(advise(model)["tier"], tier)
        self.assertEqual(advise("gpt-4o", {"input_limit": 16000})["tier"], "limited")
        self.assertEqual(advise("gpt-4o", {"output_limit": 4096})["tier"], "limited")
        self.assertEqual(advise("mistralai/mistral-large", {"reasoning": True})["tier"], "slow")
        self.assertEqual(advise("anthropic/claude-sonnet-4.5", {"reasoning": True})["tier"], "recommended")

    def test_suggestions_pick_one_current_model_per_family(self):
        models = [{"id": i} for i in ("anthropic/claude-opus-4.1", "anthropic/claude-sonnet-4.5",
                                      "anthropic/claude-sonnet-4", "openai/gpt-4.1:free", "openai/gpt-4.1",
                                      "openai/gpt-4.1-mini", "google/gemini-2.5-flash-preview", "google/gemini-2.5-flash")]
        suggested = annotate(models)
        self.assertEqual(suggested, ["anthropic/claude-sonnet-4.5", "openai/gpt-4.1", "google/gemini-2.5-flash"])
        self.assertEqual(models[0]["advice"]["tier"], "slow")
        self.assertTrue(all("advice" in m for m in models))


if __name__ == "__main__":
    unittest.main()
