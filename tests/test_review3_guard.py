"""Independent schema/data-plan probes; only synthetic DBs under pytest tmp_path."""
import json
from pathlib import Path
import random
import sqlite3
import subprocess

import pytest
from api import agent_sessions as agent, agent_session_summaries as summaries

ROOT = Path.cwd()

@pytest.fixture(autouse=True)
def reset():
    summaries.clear()
    yield
    summaries.clear()

@pytest.fixture(scope='module')
def production():
    scope = {'__name__': 'review3_production'}
    exec(compile(subprocess.check_output(['git','show','cbd7e993:api/agent_sessions.py'],text=True), 'production.py', 'exec'), scope)
    return scope['read_importable_agent_session_rows']

def build(path, *, seed, shape='canonical', analyzed=False):
    fixture=json.loads((ROOT/'tests/fixtures/sidebar_agent_schema.json').read_text())
    rng=random.Random(seed)
    with sqlite3.connect(path) as c:
        for kind,name,sql in fixture:
            if kind=='table':
                if shape=='pk_desc' and name=='sessions':
                    sql=sql.replace('id TEXT PRIMARY KEY,','id TEXT PRIMARY KEY DESC,')
                c.execute(sql)
        ddls=[sql for kind,name,sql in fixture if kind=='index' and sql]
        if shape=='reverse_create':ddls.reverse()
        for sql in ddls:c.execute(sql)
        ids=[f's{i:03}' for i in range(80)]
        rng.shuffle(ids)
        for sid in ids:
            source=rng.choice(['cli']*3+['cron','subagent','telegram','webui','kanban'])
            started=rng.choice([100,200,300])
            c.execute('INSERT INTO sessions(id,source,title,model,started_at,message_count) VALUES(?,?,?,?,?,?)',(sid,source,sid,'model',started,1))
            for j in range(rng.randint(1,3)):
                c.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',(sid,rng.choice(['user','assistant']), 'synthetic',rng.choice([500,500,600])))
        if analyzed:c.execute('ANALYZE')
    return path

@pytest.mark.parametrize('shape',['canonical','pk_desc','reverse_create'])
@pytest.mark.parametrize('analyzed',[False,True])
def test_canonical_index_plan_matrix(tmp_path,production,shape,analyzed):
    mismatches=[]
    comparisons=0
    for seed in range(12):
        path=build(tmp_path/f'{seed}.db',seed=seed,shape=shape,analyzed=analyzed)
        for limit in (1,2,5,15,50,None):
            for scope in ({},{'include_sources':('cli',),'exclude_sources':None},
                          {'include_sources':('cron',),'exclude_sources':None},
                          {'include_sources':('cli','subagent'),'exclude_sources':None}):
                expected=production(path,limit=limit,**scope)
                actual=agent.read_importable_agent_session_rows(path,limit=limit,**scope)
                comparisons+=1
                if json.dumps(expected,sort_keys=True)!=json.dumps(actual,sort_keys=True):
                    mismatches.append({'seed':seed,'limit':limit,'scope':scope,
                        'expected':[row['id'] for row in expected], 'actual':[row['id'] for row in actual]})
    print('matrix',shape,analyzed,'comparisons',comparisons,'fallbacks',summaries.counters().get('legacy_fallbacks',0),'mismatches',json.dumps(mismatches))
    assert not mismatches
