"""PR cost controls must suppress drafts without laundering failed ready checks."""
from pathlib import Path

import pytest
import yaml

from tests.ci import _gha_expr as gha

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINTS = {
    'ci.yaml': ('detect', 'all-checks-pass', 'ci-timings'),
    'nix.yml': ('detect',),
    'pm-bundle.yml': ('bundle',),
    'termux-verify.yml': ('contracts', 'tui'),
    'plugin-catalog-ci.yml': ('structural', 'pinned-source-validate'),
    'windows-bundle-sdk.yml': ('windows-bundle-tools',),
    'docker.yml': ('mode',),
    'sandbox-image.yml': ('build',),
}


def workflow(name):
    return yaml.safe_load((ROOT / '.github/workflows' / name).read_text())


@pytest.mark.parametrize('name', ENTRYPOINTS)
def test_drafts_do_not_allocate_pr_test_entrypoints(name):
    data = workflow(name)
    events = data.get('on', data.get(True))
    assert {'opened', 'synchronize', 'reopened', 'ready_for_review'} <= set(events['pull_request']['types'])
    for event, draft, expected in [('pull_request', True, False), ('pull_request', False, True), ('push', None, True), ('workflow_call', None, True)]:
        context = {
            'github': {'repository': 'NousResearch/hermes-agent', 'event_name': event,
                       'event': {'pull_request': {'draft': draft}}},
            '__status__': {'always': True, 'success': False, 'failure': True, 'cancelled': False},
        }
        for job in ENTRYPOINTS[name]:
            assert gha.condition(data['jobs'][job].get('if'), context) is expected, (name, job, event, draft)


@pytest.mark.parametrize('result', ['failure', 'cancelled', 'skipped'])
def test_ready_aggregate_still_reports_after_unsuccessful_detection(result):
    data = workflow('ci.yaml')
    aggregate = data['jobs']['all-checks-pass']
    context = {
        'github': {'event_name': 'pull_request', 'event': {'pull_request': {'draft': False}}},
        'needs': {'detect': {'result': result}},
        '__status__': {'always': True, 'success': False, 'failure': result == 'failure', 'cancelled': result == 'cancelled'},
    }
    assert gha.condition(aggregate['if'], context)
    assert aggregate['name'] == 'All required checks pass (v2)'
    assert 'detect' in aggregate['needs']
    context['github']['event']['pull_request']['draft'] = True
    assert not gha.condition(aggregate['if'], context)
