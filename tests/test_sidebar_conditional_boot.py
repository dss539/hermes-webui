"""Conditional sidebar freshness and last-known boot regressions."""
import io
import json
from pathlib import Path
import subprocess

import pytest
from api import models, routes, route_session_list_cache as cache

ROOT = Path(routes.__file__).parents[1]


def node(script):
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_normalization_precedes_source_fence(monkeypatch):
    cache._session_list_cache_clear()
    revision = [0]
    monkeypatch.setattr(cache, '_session_list_cache_resolved_source_stamp', lambda k: tuple(revision))
    def normalize(**kwargs):
        revision[0] = 1
    monkeypatch.setattr(models, 'prepare_sidebar_system_projects', normalize, raising=False)
    key = routes._session_list_cache_key('default', True, True, False, False, exclude_hidden=True, sidebar_source='webui')
    calls = []
    def builder():
        calls.append(revision[0])
        payload = {'sessions': [{'session_id': str(revision[0])}]}
        normalize()
        return payload
    result = routes._get_cached_session_list_payload(key=key, builder=builder)
    assert calls == [1]
    assert result['sessions'][0]['session_id'] == '1'


def test_failed_optional_normalization_retains_existing_builder_recovery(monkeypatch):
    cache._session_list_cache_clear()
    monkeypatch.setattr(cache, '_session_list_cache_resolved_source_stamp', lambda k: (0,))
    def unavailable(**kwargs):
        raise ValueError('Synthetic unavailable project catalog')
    monkeypatch.setattr(models, 'prepare_sidebar_system_projects', unavailable, raising=False)
    key=routes._session_list_cache_key('default', True, True, False, False, exclude_hidden=True, sidebar_source='webui')
    result=routes._get_cached_session_list_payload(key=key, builder=lambda: {'sessions':[{'session_id':'recovered'}]})
    assert result['sessions'][0]['session_id']=='recovered'


@pytest.mark.parametrize('change', ['sessions', 'active_profile', 'server_tz', 'cli_count'])
def test_validator_changes_for_actual_response_and_scope(change):
    from api.sidebar_response import session_list_etag
    response = {'sessions': [{'session_id': 'a', 'is_streaming': False}], 'server_time': 1,
                'active_profile': 'default', 'server_tz': '+0000', 'cli_count': 2}
    original = session_list_etag(response, ('default', True))
    response['server_time'] = 9
    assert session_list_etag(response, ('default', True)) == original
    assert session_list_etag(response, ('other', True)) != original
    response[change] = [{'session_id': 'a', 'is_streaming': True}] if change == 'sessions' else 'changed'
    assert session_list_etag(response, ('default', True)) != original


@pytest.mark.parametrize('form', ['weak', 'strong', 'list', 'wildcard'])
def test_conditional_response_has_no_body_and_changed_response_has_validator(form):
    from api.sidebar_response import send_session_list, session_list_etag
    class Handler:
        def __init__(self, validator=''):
            self.headers = {'If-None-Match': validator}
            self.wfile = io.BytesIO()
            self.sent = {}
            self.ended = False
        def send_response(self, status): self.status = status
        def send_header(self, key, value): self.sent[key] = value
        def end_headers(self): self.ended = True
    response = {'sessions': [], 'server_time': 1}
    etag = session_list_etag(response, ('default',))
    supplied={'weak':etag,'strong':etag.removeprefix('W/'),'list':'W/"other", '+etag,'wildcard':'*'}[form]
    h = Handler(supplied)
    send_session_list(h, response, ('default',))
    assert h.status == 304 and h.ended
    assert h.sent['X-Sidebar-Server-Time']=='1'
    assert h.sent['ETag'] == etag and h.sent['Cache-Control'] == 'no-store'
    assert h.wfile.getvalue() == b''
    h = Handler(etag)
    send_session_list(h, {'sessions': [{'session_id':'new'}], 'server_time': 2}, ('default',))
    assert h.status == 200 and h.sent['ETag'] != etag
    assert json.loads(h.wfile.getvalue())['sessions'][0]['session_id'] == 'new'


def test_api_conditional_metadata_keeps_normal_json_contract():
    source = (ROOT/'static/workspace.js').read_text().split('\nfunction recordClientSSEError', 1)[0]
    node('''
const assert=require('node:assert/strict');
const document={baseURI:'http://example.test/'};
let response,options;
async function fetch(url,opts){options=opts;return response;}
''' + source + '''
(async()=>{
 response={status:200,ok:true,headers:{get:k=>k==='etag'?'W/"one"':'application/json'},json:async()=>({sessions:[]})};
 assert.deepEqual(await api('/api/sessions'),{sessions:[]});
 assert.deepEqual(await api('/api/sessions',{responseMeta:true}),{data:{sessions:[]},etag:'W/"one"',unchanged:false,serverTime:null});
 assert.equal('responseMeta' in options,false);
 response={status:304,ok:false,headers:{get:()=> 'W/"one"'},json:()=>{throw Error('must not parse 304')}};
 assert.deepEqual(await api('/api/sessions',{responseMeta:true}),{data:null,etag:'W/"one"',unchanged:true,serverTime:null});
})().catch(e=>{console.error(e);process.exitCode=1});
''')


BOOT_ENV = '''
const assert=require('node:assert/strict');
const S={activeProfile:'default'};
const t=key=>key==='session_updating_conversations'?'Updating conversations…':key;
let _sessionListHasLoadedOnce=false,_allSessions=[],_sidebarReferenceSessions=[],_allProjects=[];
let _profileSwitchListEmbargo=false,_sessionListSkeletonActive=false,_allSessionsScope=null,_showAllProfiles=true;
let paints=0,stored=null,notes=[];
let _otherProfileCount=0,_archivedWebuiCount=0,_archivedCliCount=0;
let _serverWebuiSessionCount=null,_serverCliSessionCount=null;
const list={dataset:{},setAttribute(k,v){this[k]=v},removeAttribute(k){delete this[k]},before(note){notes.push(note)}};
const $=id=>id==='sessionList'?list:null;
const document={baseURI:'http://example.test/',createElement:()=>({setAttribute(){}}),
 querySelector:()=>({lastElementChild:null,insertBefore:note=>notes.push(note)})};
const sessionStorage={getItem:()=>stored,setItem:(k,v)=>stored=v,removeItem:()=>stored=null};
const _requestedSessionSidebarSource=()=> 'webui';
const _sessionListExcludeHiddenEnabled=()=>true;
const _sessionListQueryString=()=>'?all_profiles=1';
const renderSessionListFromCache=()=>{paints++};
'''


def boot_source():
    source = (ROOT/'static/sessions.js').read_text()
    start = source.find('let _sidebarSessionValidator')
    if start < 0:
        return ''
    return source[start:source.index('async function _loadSidebarSessionListPayload', start)]


@pytest.mark.parametrize('different_scope', [False, True])
def test_boot_cache_is_scope_bound_and_never_marks_loaded(different_scope):
    node(BOOT_ENV + boot_source() + '''
stored=JSON.stringify({scope:JSON.stringify(['PROFILE','http://example.test/api/sessions?all_profiles=1']),sessions:[{session_id:'last-known'}],projects:[],references:[]});
_restoreSidebarBootCache('?all_profiles=1');
assert.equal(_sessionListHasLoadedOnce,false);
assert.equal(_sidebarSessionValidator,null);
''' .replace('PROFILE', 'other' if different_scope else 'default') + (
        "assert.equal(paints,0);assert.deepEqual(_allSessions,[]);" if different_scope else
        "assert.equal(paints,1);assert.equal(_allSessions[0].session_id,'last-known');assert.equal(list.dataset.freshness,'cached');assert.equal(list['aria-busy'],'true');assert.equal(notes[0].textContent,'Updating conversations…');"
    ))


def test_invalid_boot_cache_and_profile_switch_do_not_paint():
    node(BOOT_ENV + boot_source() + '''
stored='not json';_restoreSidebarBootCache('?all_profiles=1');assert.equal(paints,0);
_sidebarBootCacheAttempted=false;_profileSwitchListEmbargo=true;
stored=JSON.stringify({scope:JSON.stringify(['default','http://example.test/api/sessions?all_profiles=1']),sessions:[{session_id:'a'}],projects:[],references:[]});
_restoreSidebarBootCache('?all_profiles=1');assert.equal(paints,0);
''')


def test_received_projection_revalidation_still_requires_adoption():
    source = (ROOT/'static/sessions.js').read_text()
    start=source.index('async function _loadSidebarSessionListPayload')
    end=source.index('async function _drainRenderSessionListQueue',start)
    node(BOOT_ENV+boot_source()+source[start:end]+'''
let requests=[],reply={data:{sessions:[{session_id:'new'}]},etag:'W/"new"',unchanged:false};
async function api(path,opts){if(path.startsWith('/api/projects'))return {projects:[]};requests.push(opts);return JSON.parse(JSON.stringify(reply));}
(async()=>{
 let result=await _loadSidebarSessionListPayload('?all_profiles=1',{});
 assert.equal(requests[0].headers['If-None-Match'],undefined);
 // A fetched-but-unapplied payload can be conditionally revalidated, but
 // must still be adopted and painted if the server validates it.
 reply={unchanged:true,etag:'W/"new"'};
 const revalidated=await _loadSidebarSessionListPayload('?all_profiles=1',{});
 assert.equal(requests[1].headers['If-None-Match'],'W/"new"');
 assert.equal(Object.hasOwn(revalidated,'unchanged'),false);
 assert.equal(revalidated.sessData.sessions[0].session_id,'new');
 _sessionListHasLoadedOnce=true;
 _sidebarSessionValidator={...result.sessData._sidebarValidator,sessData:result.sessData};
 reply={unchanged:true,etag:'W/"new"'};
 result=await _loadSidebarSessionListPayload('?all_profiles=1',{});
 assert.equal(Object.hasOwn(result,'unchanged'),false);assert.equal(result.sessData.sessions[0].session_id,'new');
 assert.equal(requests[2].headers['If-None-Match'],'W/"new"');
 S.activeProfile='other';reply={data:{sessions:[]},etag:'W/"other"',unchanged:false};
 await _loadSidebarSessionListPayload('?all_profiles=1',{});
 assert.equal(requests[3].headers['If-None-Match'],undefined);
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_last_known_unread_render_cannot_write_baselines_or_generations():
    source=(ROOT/'static/sessions.js').read_text()
    start=source.index('function _hasUnreadForSession(')
    end=source.index('// Keep the sidebar polling snapshot',start)
    node('''
const assert=require('node:assert/strict');
let _sidebarShowingLastKnown=true,writes=0,completion=false,counts={};
const _hasSessionCompletionUnread=()=>completion;
const _getSessionViewedCounts=()=>counts;
const _sessionTranscriptGenerationForUnread=(id,g)=>Number(g)||0;
const _sessionViewedCountRecord=x=>typeof x==='object'?x:{message_count:Number(x)||0,transcript_generation:0};
const _setSessionViewedCount=()=>writes++;
''' + source[start:end] + '''
assert.equal(_hasUnreadForSession({session_id:'a',message_count:8}),false);
assert.equal(writes,0);assert.deepEqual(counts,{});
counts={a:{message_count:4,transcript_generation:1}};
assert.equal(_hasUnreadForSession({session_id:'a',message_count:8,transcript_generation:2}),false);
assert.equal(writes,0);
completion=true;assert.equal(_hasUnreadForSession({session_id:'a',message_count:8}),true);
completion=false;_sidebarShowingLastKnown=false;
_hasUnreadForSession({session_id:'a',message_count:8,transcript_generation:2});
assert.equal(writes,1);
''')


def test_boot_persistence_waits_for_paint_and_newest_payload_wins():
    node(BOOT_ENV+boot_source()+'''
let release;
let _sessionListPaintJob={completion:new Promise(r=>release=r)};
(async()=>{
 _sessionListHasLoadedOnce=true;
 _saveSidebarBootCache({sessions:[{session_id:'obsolete'}]});
 await new Promise(r=>setTimeout(r,10));
 assert.equal(stored,null,'persistence preempted paint');
 _saveSidebarBootCache({sessions:[{session_id:'newest'}]});
 await new Promise(r=>setTimeout(r,10));assert.equal(stored,null);
 release();await new Promise(r=>setTimeout(r,10));
 assert.equal(JSON.parse(stored).sessions[0].session_id,'newest');
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_boot_persistence_uses_server_metadata_not_local_optimism():
    node(BOOT_ENV+boot_source()+'''
(async()=>{
 _sessionListHasLoadedOnce=true;
 _allSessions=[{session_id:'local-only',messages:[{content:'not metadata'}]}];
 _saveSidebarBootCache({sessions:[{session_id:'server',title:'known',pending_user_message:'private prompt'}]});
 await new Promise(r=>setTimeout(r,10));
 const entry=JSON.parse(stored);
 assert.deepEqual(entry.sessions,[{session_id:'server',title:'known'}]);
 assert.equal(stored.includes('private prompt'),false);
 assert.equal(stored.includes('local-only'),false);
 _saveSidebarBootCache({sessions:[{session_id:'huge',title:'x'.repeat(1024*1024)}]});
 await new Promise(r=>setTimeout(r,10));assert.equal(stored,null);
})().catch(e=>{console.error(e);process.exitCode=1});
''')
