"""Sidebar redraw regressions: no-op shape, bounded copying and raw counts."""
import copy
import json

import pytest

from api import models, routes


def test_sidebar_response_property_order_is_identical_on_cold_and_cache_paths(monkeypatch):
    monkeypatch.setattr(routes, '_session_attention_summary', lambda _sid: None)
    first = {'session_id': 'synthetic1', 'title': 'Synthetic', 'message_count': 2,
             'profile': 'default', 'updated_at': 1, 'source_tag': 'webui'}
    reverse = dict(reversed(list(first.items())))
    a = routes._sidebar_session_response_item(first, redact_enabled=False)
    b = routes._sidebar_session_response_item(reverse, redact_enabled=False)
    assert a == b
    assert json.dumps(a) == json.dumps(b), 'an unchanged cache hit must not change the browser render signature'


def test_cli_copy_does_not_walk_flat_metadata_with_generic_deepcopy(monkeypatch):
    original = copy.deepcopy
    walks = []
    def observed(value, *args, **kwargs):
        walks.append(type(value).__name__)
        return original(value, *args, **kwargs)
    monkeypatch.setattr(copy, 'deepcopy', observed)
    rows = [{'session_id': f'synthetic{i}', 'title': 'Synthetic', 'message_count': i,
             'archived': False, 'project_id': None} for i in range(50)]
    result = models._copy_cli_sessions(rows)
    assert result == rows and result is not rows and result[0] is not rows[0]
    assert not walks, 'flat scalar metadata must not take the generic recursive deepcopy path'


def test_cli_copy_keeps_nested_alias_and_cycle_isolation():
    shared = {'history': [{'synthetic': [1, 2]}]}
    row = {'session_id': 'synthetic1', 'routing': shared, 'also_routing': shared}
    rows = [row, row]
    row['cycle'] = rows
    cloned = models._copy_cli_sessions(rows)
    assert cloned[0] is cloned[1]
    assert cloned[0]['cycle'] is cloned
    assert cloned[0]['routing'] is cloned[0]['also_routing']
    cloned[0]['routing']['history'][0]['synthetic'].append(3)
    assert shared['history'][0]['synthetic'] == [1, 2]
    assert models._copy_cli_sessions([None, {'value': [1]}]) == [None, {'value': [1]}]


def test_both_cli_caches_isolate_caller_mutations():
    models.clear_cli_sessions_cache()
    key = ('synthetic-cache',)
    original = [{'session_id': 'synthetic', 'routing': {'history': [1]}}]
    try:
        stamp = models._cli_sessions_cache_invalidation_stamp()
        assert models._cache_cli_sessions_if_current(key, 45, stamp, original)
        original[0]['routing']['history'].append(2)
        fresh = models._copy_fresh_cli_sessions_cache_entry(key)
        assert fresh[0]['routing']['history'] == [1]
        fresh[0]['routing']['history'].append(3)
        stable = models._copy_last_known_good_cli_sessions(models._cli_sessions_stable_cache_identity(key), stamp)
        assert stable[0]['routing']['history'] == [1]
        stable[0]['routing']['history'].append(4)
        assert models._copy_fresh_cli_sessions_cache_entry(key)[0]['routing']['history'] == [1]
    finally:
        models.clear_cli_sessions_cache()


@pytest.mark.parametrize('exclude_hidden', [False, True])
@pytest.mark.parametrize('show_cron', [False, True])
def test_hidden_background_raw_counts_remain_part_of_response_contract(monkeypatch, exclude_hidden, show_cron):
    # Omitting the expensive optional background queries outright changes raw
    # cli_count, even when the final visible rows are identical. Preserve that
    # contract until a counted/delta projection can prove exact equivalence.
    sample = [
        {'session_id': 'syntheticweb', 'source_tag': 'webui', 'session_source': 'webui',
         'message_count': 2, 'title': 'Synthetic', 'last_message_at': 2, 'profile': 'default'},
        {'session_id': 'syntheticcron', 'source_tag': 'cron', 'session_source': 'cron',
         'message_count': 2, 'title': 'Synthetic cron', 'last_message_at': 1, 'profile': 'default', 'project_id': 'synthetic-project'},
    ]
    monkeypatch.setattr(routes, 'all_sessions', lambda **kwargs: [])
    monkeypatch.setattr(routes, '_enrich_sidebar_lineage_metadata', lambda rows: None)
    monkeypatch.setattr(routes, '_reconcile_stale_stream_state_for_session_rows', lambda rows: False)
    monkeypatch.setattr(routes, 'get_cli_sessions', lambda **kwargs: copy.deepcopy(sample))
    payload = routes._build_session_list_cache_payload(
        active_profile='default', all_profiles=True, show_cli_sessions=True,
        show_previous_messaging_sessions=False, show_cron_sessions=show_cron,
        exclude_hidden=exclude_hidden, visible_only=True)
    assert payload['cli_count'] == 2
    expected_visible = 1 if exclude_hidden and not show_cron else 2
    assert len(payload['sessions']) == expected_visible
