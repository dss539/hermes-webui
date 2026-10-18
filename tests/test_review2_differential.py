"""Compare real production source with repaired readers on prior synthetic fixtures."""
import ast
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest
from api import agent_sessions as current, agent_session_summaries as summaries

ROOT = Path.cwd()
PRIOR = Path('/home/smithers/projects/webui-perf-1017/output/review-sidebar-deepseek')


def load_definitions(filename, names, directory):
    tree = ast.parse((PRIOR / filename).read_text())
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
        if isinstance(node, ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in node.targets):
            nodes.append(node)
    scope = {'ROOT': directory, 'sqlite3':sqlite3, 'Path':Path}
    exec(compile(ast.Module(body=nodes,type_ignores=[]), filename, 'exec'), scope)
    return scope


@pytest.fixture
def production():
    source = subprocess.check_output(['git','show','cbd7e993:api/agent_sessions.py'], text=True)
    namespace = {'__name__':'review2_production_reader'}
    exec(compile(source,'production_agent_sessions.py','exec'),namespace)
    summaries.clear()
    yield namespace
    summaries.clear()


def exact(value):
    return json.dumps(value,sort_keys=True,allow_nan=True)


def test_prior_tie_matrix_against_real_production(tmp_path, production):
    defs = load_definitions('probe_tie2.py', {'DDL','build','cases'}, tmp_path)
    comparisons = 0
    for name, rows in defs['cases']().items():
        path = defs['build'](name, rows)
        for limit in (1,2,3,8,50):
            kwargs = dict(limit=limit,include_sources=('cron',),exclude_sources=None)
            expected = production['read_importable_agent_session_rows'](path,**kwargs)
            got = current.read_importable_agent_session_rows(path,**kwargs)
            assert exact(got) == exact(expected), (name,limit,got,expected)
            comparisons += 1
    print('prior_tie_comparisons=',comparisons)


def test_prior_differential_matrix_against_real_production(tmp_path, production):
    defs = load_definitions('probe_diff.py',{'SESSIONS_DDL','MESSAGES_DDL','MSG_INS','build','fixtures'},tmp_path)
    comparisons = 0
    mismatches = []
    for name, path in defs['fixtures']().items():
        for limit in (None,1,2,5,50):
            for scope in ({},{'include_sources':('cli',),'exclude_sources':None},
                          {'project_assignment':'assigned'},{'project_assignment':'unassigned'}):
                expected = production['read_importable_agent_session_rows'](path,limit=limit,**scope)
                got = current.read_importable_agent_session_rows(path,limit=limit,**scope)
                if exact(got) != exact(expected):
                    mismatches.append({'fixture':name,'limit':limit,'scope':scope,
                                       'actual_ids':[r['id'] for r in got],
                                       'expected_ids':[r['id'] for r in expected]})
                comparisons += 1
        ids = [f's{i:03d}' for i in range(120)] + ['a','b','c','root','seg2','seg3','child','x','y','z','m']
        if exact(current.read_session_lineage_metadata(path,ids)) != exact(production['read_session_lineage_metadata'](path,ids)):
            mismatches.append({'fixture':name,'lineage':True})
        comparisons += 1
    print('prior_differential_comparisons=',comparisons,'mismatches=',json.dumps(mismatches))
    assert not mismatches


@pytest.mark.parametrize('session_indexes', [False, True])
def test_different_start_times_tie_repair_control(tmp_path, production, session_indexes):
    defs = load_definitions('probe_diff.py',{'SESSIONS_DDL','MESSAGES_DDL','MSG_INS','build','fixtures'},tmp_path)
    path = defs['fixtures']()['ties_messages']
    if session_indexes:
        with closing(sqlite3.connect(path)) as c:
            c.executescript('CREATE INDEX idx_sessions_source ON sessions(source); CREATE INDEX idx_sessions_source_id ON sessions(source,id); CREATE INDEX idx_sessions_started ON sessions(started_at DESC);')
    source = subprocess.check_output(['git','show','e5ca03c3:api/agent_sessions.py'],text=True)
    previous = {'__name__':'review2_pre_repair_reader'}
    exec(compile(source,'previous_agent_sessions.py','exec'),previous)
    kwargs = dict(limit=1,include_sources=('cli',),exclude_sources=None)
    expected = production['read_importable_agent_session_rows'](path,**kwargs)
    before = previous['read_importable_agent_session_rows'](path,**kwargs)
    after = current.read_importable_agent_session_rows(path,**kwargs)
    print('session_indexes=',session_indexes,'production=',[r['id'] for r in expected],
          'pre_repair=',[r['id'] for r in before],'repaired=',[r['id'] for r in after])
    if not session_indexes:
        assert exact(before) == exact(expected)
    assert exact(after) == exact(expected)
