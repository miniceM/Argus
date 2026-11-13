"""验收判定的反例；纯本地，不访问真实 Langfuse 或 Agent。"""
import copy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import verify


class EvidenceTests(unittest.TestCase):
    def fixture(self):
        item = {'id': 'item-1', 'dispatch_generation': 1, 'trace_id': 'a' * 32,
                'observation_id': 'b' * 16, 'dataset_item_id': 'case-1',
                'scores': {name: True for name in verify.METRICS}}
        observations = [{'id': item['observation_id'], 'traceId': item['trace_id'],
                         'name': 'remote-agent-http', 'type': 'TOOL'}]
        experiments = [{'id': item['observation_id'], 'traceId': item['trace_id'], 'experimentId': 'exp-1',
                        'experimentDatasetId': 'dataset-1', 'experimentItemId': 'case-1'}]
        scores = [{'id': 'score:item-1:gen1:' + name, 'name': name, 'source': 'API', 'value': 1,
                   'subject': {'kind': 'observation', 'id': item['observation_id'], 'traceId': item['trace_id']}}
                  for name in verify.METRICS]
        return item, observations, scores, experiments

    def test_exact_identity_accepts_and_wrong_source_subject_join_value_rejects(self):
        original = self.fixture()
        self.assertEqual(len(verify.verify_remote_item(*original, 'dataset-1', 'exp-1')), 4)
        for field in ('source', 'subject', 'join', 'value', 'generation', 'trace', 'duplicate'):
            item, observations, scores, experiments = copy.deepcopy(original)
            if field == 'source':
                scores[0]['source'] = 'EVAL'
            elif field == 'subject':
                scores[0]['subject']['id'] = 'other-observation'
            elif field == 'join':
                experiments[0]['experimentItemId'] = 'other-case'
            elif field == 'value':
                scores[0]['value'] = 0
            elif field == 'generation':
                item['dispatch_generation'] = 2
            elif field == 'trace':
                observations[0]['traceId'] = 'c' * 32
            else:
                scores.append(copy.deepcopy(scores[0]))
            with self.subTest(field=field), self.assertRaises(common.Stop):
                verify.verify_remote_item(item, observations, scores, experiments, 'dataset-1', 'exp-1')

    def test_cursor_pagination_and_repeated_cursor_rejection(self):
        pages = iter([{'data': [1], 'meta': {'cursor': 'next'}}, {'data': [2], 'meta': {'cursor': None}}])
        self.assertEqual(verify.read_pages(lambda path: next(pages), '/api/test', {'limit': 1}), [1, 2])
        for page in ({'data': [], 'meta': {'cursor': 'repeat'}}, {'data': []}):
            with self.assertRaises(common.Stop):
                verify.read_pages(lambda path: page, '/api/test', {})

    def test_partial_failed_launch_returns_without_reinvoking_agent(self):
        terminal = {'id': 'launch-1', 'status': 'PARTIAL_FAILED'}
        with mock.patch.object(verify, 'api', return_value=terminal) as api, \
             mock.patch.object(verify.time, 'sleep') as sleep:
            self.assertEqual(verify.poll_launch({}, 'launch-1'), terminal)
            api.assert_called_once_with({}, '/api/v1/experiment-launches/launch-1')
            sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
