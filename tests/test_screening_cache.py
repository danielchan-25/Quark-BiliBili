import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from mediaflow.candidate_screening import screen_candidate


class CacheTests(unittest.TestCase):
    def test_cache_invalidation_usage_and_errors(self):
        response = SimpleNamespace(scores={'relevance':SimpleNamespace(score=2.9, confidence=.9)},
            nouls={'risk':SimpleNamespace(noul=.1)}, usage=SimpleNamespace(input_tokens=123,output_tokens=4),
            request_id='test-request',model='test-model')
        with tempfile.TemporaryDirectory() as tmp, patch('typesafe_sdk.TypeSafeClient') as client:
            request = client.return_value.__enter__.return_value.system_one
            request.return_value = response
            params = dict(api_key='never-log-this',keyword='CodeX',resource_category='AI',title='Tutorial',author='A',
                cache_path=Path(tmp)/'cache.db',log_path=Path(tmp)/'usage.jsonl')
            first = screen_candidate(**params)
            self.assertEqual(screen_candidate(**params), first)
            self.assertEqual(request.call_count,1)
            screen_candidate(**dict(params,title='Changed'))
            self.assertEqual(request.call_count,2)
            screen_candidate(**params,ttl_seconds=0)
            self.assertEqual(request.call_count,3)
            with patch.dict('os.environ', {'TYPESAFE_DEFAULT_MODEL':'different-model'}):
                screen_candidate(**params)
            self.assertEqual(request.call_count,4)
            with patch('mediaflow.candidate_screening.MAX_RISK',.05):
                self.assertEqual(screen_candidate(**params).status,'rejected')
            request.side_effect = RuntimeError('never-log-this')
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    screen_candidate(**dict(params,title='Fail'))
            self.assertEqual(request.call_count,7)
            raw=params['log_path'].read_text(encoding='utf-8')
            self.assertNotIn('never-log-this',raw)
            events=[json.loads(line) for line in raw.splitlines()]
            self.assertEqual(events[0]['input_tokens'],123)
            self.assertEqual(events[0]['actual_model'],'test-model')
            self.assertEqual(events[1]['input_tokens'],0)
            self.assertEqual(events[1]['estimated_avoided_input_tokens'],123)
            self.assertIsNone(events[-1]['input_tokens'])
