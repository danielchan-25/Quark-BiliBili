import sys
import types
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from mediaflow.candidate_screening import screen_candidate


class CandidateScreeningTests(unittest.TestCase):
    def _screen(self, score: float, confidence: float, risk: float):
        response = types.SimpleNamespace(
            scores={"relevance": types.SimpleNamespace(score=score, confidence=confidence)},
            nouls={"risk": types.SimpleNamespace(noul=risk)},
        )

        class Client:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def system_one(self, **_kwargs):
                return response

        fake_sdk = types.SimpleNamespace(
            TypeSafeClient=Client,
            Score=lambda **_kwargs: _kwargs,
            Noul=lambda **_kwargs: _kwargs,
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"typesafe_sdk": fake_sdk}):
            return screen_candidate(
                api_key="test-key",
                keyword="CodeX 教程",
                resource_category="AI",
                title="CodeX 使用教程",
                author="creator",
                cache_path=Path(tmp) / 'cache.db', log_path=Path(tmp) / 'usage.jsonl',
            )

    def test_approves_a_high_confidence_low_risk_candidate(self):
        result = self._screen(score=2.4, confidence=0.8, risk=0.1)
        self.assertEqual(result.status, "approved")

    def test_rejects_a_risky_or_uncertain_candidate(self):
        self.assertEqual(self._screen(score=2.5, confidence=0.6, risk=0.1).status, "rejected")
        self.assertEqual(self._screen(score=2.5, confidence=0.8, risk=0.3).status, "rejected")

    def test_requires_an_api_key(self):
        with self.assertRaisesRegex(RuntimeError, "TYPESAFE_API_KEY"):
            screen_candidate(
                api_key=None,
                keyword="CodeX 教程",
                resource_category="AI",
                title="CodeX 使用教程",
                author="creator",
            )


if __name__ == "__main__":
    unittest.main()
