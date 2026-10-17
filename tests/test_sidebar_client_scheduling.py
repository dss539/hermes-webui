"""Client scheduling and ordering regressions; executable against a base cwd."""
import os
from pathlib import Path
import subprocess

import pytest

SOURCE = (Path.cwd() / 'static/sessions.js').read_text()


def section(start, end):
    return SOURCE[SOURCE.index(start):SOURCE.index(end, SOURCE.index(start))]


def node(script, tz=None):
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True,
                            env={**os.environ, **({'TZ': tz} if tz else {})})
    assert result.returncode == 0, result.stderr


ORDER_ENV = '''
const assert=require('node:assert/strict');
const store=new Map();let reads=[];
const localStorage={get length(){return store.size},key:i=>[...store.keys()][i]??null,
 getItem:k=>{reads.push(k);return store.get(k)??null},setItem:(k,v)=>store.set(k,String(v)),removeItem:k=>store.delete(k)};
const SESSION_COMPLETION_UNREAD_CLEARED_PREFIX='clear:';
const SESSION_COMPLETION_UNREAD_CLEARED_KEY='legacy';
const SESSION_COMPLETION_UNREAD_KEY='markers';
const SESSION_COMPLETION_UNREAD_CLEARED_TTL_MS=7000;
let _sessionCompletionUnreadClearedMemory={},markers={};
const _readStoredJsonMap=k=>JSON.parse(localStorage.getItem(k)||'{}');
const _getSessionCompletionUnread=()=>markers;
Date.now=()=>100;
'''


def order_source():
    return section('function _sessionCompletionUnreadClearedKey(', '// Merge the persisted marker map')


def test_per_session_intent_ignores_unrelated_record_values_without_losing_order():
    node(ORDER_ENV + '''
function _sessionCompletionUnreadOrder(m){return Number(m&&(m.unread_order||m.completed_at))||0;}
''' + order_source() + '''
for(let i=0;i<300;i++) store.set(_sessionCompletionUnreadClearedKey('unrelated-'+i,120+i),'20');
store.set(_sessionCompletionUnreadClearedKey('target',800),'20');
store.set(_sessionCompletionUnreadClearedKey('target',700),'20');
markers.target={unread_order:600};
const next=_nextSessionCompletionUnreadOrder('target');
assert.equal(next,801);
assert.equal(reads.some(k=>k.startsWith('clear:unrelated-')),false,'unrelated clear values were read');
''')


@pytest.mark.parametrize('sid', ['plain', 'a:b/with space', '雪'])
def test_targeted_order_matches_full_reader_for_legacy_versioned_memory_and_expiry(sid):
    import json
    node(ORDER_ENV + '''
function _sessionCompletionUnreadOrder(m){return Number(m&&(m.unread_order||m.completed_at))||0;}
''' + order_source() + f'const sid={json.dumps(sid)};\n' + '''
store.set('legacy',JSON.stringify({[sid]:400}));
store.set(_sessionCompletionUnreadClearedKey(sid),'500');
store.set(_sessionCompletionUnreadClearedKey(sid,600),'10');
store.set(_sessionCompletionUnreadClearedKey('other',900),'10');
_sessionCompletionUnreadClearedMemory[sid]={order:700,recorded_at:90};
assert.equal(_nextSessionCompletionUnreadOrder(sid),Math.max(100,_readSessionCompletionUnreadCleared()[sid])+1);
_sessionCompletionUnreadClearedMemory[sid]={order:9999,recorded_at:-8000};
assert.equal(_nextSessionCompletionUnreadOrder(sid),601);
store.set('markers',JSON.stringify({[sid]:{unread_order:900}}));
assert.equal(_nextSessionCompletionUnreadOrder(sid),901);
''')


@pytest.mark.parametrize('tz', ['UTC', 'America/New_York'])
def test_calendar_cache_expires_at_local_midnight_and_dst(tz):
    node('''const assert=require('node:assert/strict');
const _serverNowMs=()=>Date.now();
''' + section('function _sessionCalendarBoundaries(', 'function _formatSessionDate(') + '''
function oracle(ms){const n=new Date(ms),d=new Date(n.getFullYear(),n.getMonth(),n.getDate());
 const w=new Date(d);w.setDate(w.getDate()-((w.getDay()+6)%7));const last=new Date(w);last.setDate(last.getDate()-7);
 return {startOfToday:+d,startOfYesterday:+new Date(n.getFullYear(),n.getMonth(),n.getDate()-1),startOfWeek:+w,startOfLastWeek:+last};}
for(const value of ['2026-03-08T00:00:00','2026-03-08T01:59:00','2026-03-08T03:01:00','2026-03-09T00:00:00',
 '2026-11-01T00:00:00','2026-11-01T03:00:00','2026-11-02T00:00:00']){
 const ms=+new Date(value);assert.deepEqual(_sessionCalendarBoundaries(ms),oracle(ms));
 assert.deepEqual(_sessionCalendarBoundaries(ms+100),oracle(ms+100));
}
''', tz=tz)


def test_resume_updates_relative_times_without_rebuilding_collapsed_rows():
    node('''
const assert=require('node:assert/strict');let renders=0;
const _expandedChildSessionKeys=new Set(),_expandedLineageKeys=new Set();
const labels=[{dataset:{timestamp:'123'},classList:{contains:()=>false},textContent:'old'},
 {dataset:{timestamp:'456'},classList:{contains:()=>true},textContent:''}];
const list={dataset:{},querySelectorAll:()=>labels};const $=()=>list;
const _serverNowMs=()=>200,_sessionCalendarBoundaries=()=>({startOfToday:200});
const _formatRelativeSessionTime=ms=>'updated '+ms;
const renderSessionListFromCache=()=>renders++;
''' + section('function _refreshSidebarTimeLabels(', 'function ensureSessionTimeRefreshPoll(') + '''
_refreshSidebarTimeLabels();
assert.equal(labels[0].textContent,'updated 123');assert.equal(labels[1].textContent,'');assert.equal(renders,0);
_expandedChildSessionKeys.add('expanded');_refreshSidebarTimeLabels();assert.equal(renders,1);
_expandedChildSessionKeys.clear();list.dataset.sessionDateBoundary='100';
_refreshSidebarTimeLabels();assert.equal(renders,2,'a new day must regroup rows');
''')


def test_304_does_not_render_again_but_waits_for_the_applied_chunked_paint():
    node('''
const assert=require('node:assert/strict');
const $=()=>({value:''}),_sessionListQueryString=()=>'',_restoreSidebarBootCache=()=>{};
let _pendingSessionListPayload=null,_sessionListHasLoadedOnce=true,_renderSessionListGen=1;
let _profileSwitchListEmbargo=false,_serverTimeDelta=0,_sessionListLoadError=null,_sessionListSkeletonActive=false;
const _allProjects=[];
const _loadSidebarSessionListPayload=async()=>({sessData:{},projData:{projects:[]},unchanged:true});
let release,done=false,applies=0;
let _sessionListPaintJob={completion:new Promise(r=>release=r)};
const _applySessionListPayload=()=>applies++;
const _showSessionListLoadError=e=>{throw e};
''' + section('async function _runRenderSessionListRefresh(', 'let _sidebarSessionValidator') + '''
(async()=>{
 const result=_runRenderSessionListRefresh({},1).then(()=>done=true);
 await new Promise(r=>setTimeout(r,20));
 assert.equal(applies,0);assert.equal(done,false,'304 finished before its applied paint');
 release();await result;assert.equal(done,true);assert.equal(applies,0);
})().catch(e=>{console.error(e);process.exitCode=1});
''')
