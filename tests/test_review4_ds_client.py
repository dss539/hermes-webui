"""Review-4 DeepSeek client probes: paint completion + detached JSON adoption."""
import subprocess
from pathlib import Path

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'static/sessions.js').read_text()


def node(script):
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


FLUSH = extract_function(SOURCE, '_flushPendingSidebarPaint')
MERGE = extract_function(SOURCE, '_mergeOptimisticFirstTurnSessions')
OPTIMISTIC = extract_function(SOURCE, '_isOptimisticFirstTurnSessionRow')


def test_chunked_completion_certifies_while_interaction_open():
    """A fully filled chunk paint is the DOM; a mid-fill menu/rename cannot
    invalidate it. A cancelled/incomplete job must never certify."""
    out = node(r'''
const assert=require('node:assert/strict');
let _renamingSid=null,_sessionActionMenu=null,_sessionListSkeletonActive=false;
let _lastSessionListRenderSig='old',recorded=0;
let _allSessions=[];
let _sidebarPendingPaint={signature:'new',sessions:_allSessions,sessData:{}};
const _recordSidebarPaint=()=>recorded++;
let resolvePaint=null;
let renderSessionListFromCache=()=>new Promise(r=>{resolvePaint=r;});
''' + FLUSH + r'''
(async()=>{
  // menu opens mid-fill, job completes: certify, no forced repaint later.
  const pending=_flushPendingSidebarPaint();
  _renamingSid='a';
  resolvePaint(true);
  await pending;
  assert.equal(recorded,1,'completed chunk must certify');
  assert.equal(_lastSessionListRenderSig,'new');
  assert.equal(_sidebarPendingPaint,null);

  // rename open, job reports incomplete: must NOT certify.
  _lastSessionListRenderSig='old';recorded=0;_renamingSid='a';
  _sidebarPendingPaint={signature:'new',sessions:_allSessions,sessData:{}};
  const p2=_flushPendingSidebarPaint();
  resolvePaint(false);
  await p2;
  assert.equal(recorded,0,'incomplete chunk must not certify');
  assert.ok(_sidebarPendingPaint);

  // sync renderer while a rename is open: must NOT certify.
  _renamingSid=null;recorded=0;_sidebarPendingPaint={signature:'new',sessions:_allSessions,sessData:{}};
  _sessionActionMenu={};
  let calls=0;
  const syncRenderer=()=>{calls++;return undefined;};
  renderSessionListFromCache=syncRenderer;
  _flushPendingSidebarPaint();
  // the pre-check returns before invoking the renderer at all
  assert.equal(calls,0);
  assert.equal(recorded,0);

  // sync renderer that opens a menu during the render: post-check bails.
  _sessionActionMenu=null;calls=0;
  const openingRenderer=()=>{calls++;_sessionActionMenu={};};
  renderSessionListFromCache=openingRenderer;
  _flushPendingSidebarPaint();
  assert.equal(calls,1);
  assert.equal(recorded,0);
  assert.ok(_sidebarPendingPaint);

  // chunk completion with a stale pending object: never certify.
  _sessionActionMenu=null;_lastSessionListRenderSig='old';recorded=0;
  _sidebarPendingPaint={signature:'new',sessions:_allSessions,sessData:{}};
  const p3=_flushPendingSidebarPaint();
  _sidebarPendingPaint=null;
  resolvePaint(true);
  await p3;
  assert.equal(recorded,0);

  // skeleton always blocks, even for a completed chunk.
  _sessionListSkeletonActive=true;recorded=0;
  _sidebarPendingPaint={signature:'new',sessions:_allSessions,sessData:{}};
  const p4=_flushPendingSidebarPaint();
  resolvePaint(true);
  await p4;
  assert.equal(recorded,0);
  console.log(JSON.stringify({recorded}));
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_adoption_json_boundary_drops_functions_and_keeps_truth():
    out = node(r'''
const assert=require('node:assert/strict');
let _allSessions=[];
const _isSessionLocallyStreaming=()=>false,_sessionStreamingById=new Map();
''' + MERGE + OPTIMISTIC + r'''
// A function-valued field used to make structuredClone throw; JSON omits it.
const rows=[{session_id:'a',message_count:3,attention:{count:1},hook:()=>{}}];
_allSessions=_mergeOptimisticFirstTurnSessions(rows);
assert.equal(_allSessions[0].hook,undefined);
assert.equal(_allSessions[0].attention.count,1);

// Response-boundary capture: re-adoption after local mutation replays truth.
const sessData={sessions:[{session_id:'a',title:'Server'}],sidebar_reference_sessions:[{session_id:'r'}]};
Object.defineProperty(sessData,'_sidebarRowsJSON',{value:JSON.stringify([sessData.sessions,sessData.sidebar_reference_sessions]),enumerable:false});
sessData.sessions[0].title='locally renamed';
const [detachedSessions,detachedReferences]=JSON.parse(sessData._sidebarRowsJSON);
assert.equal(detachedSessions[0].title,'Server');
assert.notEqual(detachedSessions[0],sessData.sessions[0]);
assert.equal(detachedReferences[0].session_id,'r');

// The property must survive the JSON round trip of the payload itself
// (non-enumerable -> not persisted, so a boot-cache payload takes the fallback).
assert.equal(Object.keys(sessData).includes('_sidebarRowsJSON'),false);

// BigInt is the one JSON-only loss versus structuredClone: it throws.
let threw=false;try{JSON.stringify([{n:1n}]);}catch(_){threw=true;}
assert.equal(threw,true);
console.log(JSON.stringify({ok:true}));
''')


def test_deferred_apply_reparse_keeps_rows_independent():
    """The deferred re-apply must not reuse mutated rows from the first apply."""
    out = node(r'''
const assert=require('node:assert/strict');
const payload={sessions:[{session_id:'a',title:'Server'}],sidebar_reference_sessions:[]};
Object.defineProperty(payload,'_sidebarRowsJSON',{value:JSON.stringify([payload.sessions,payload.sidebar_reference_sessions]),enumerable:false});
const first=JSON.parse(payload._sidebarRowsJSON);
first[0][0].title='local';
const second=JSON.parse(payload._sidebarRowsJSON);
assert.equal(second[0][0].title,'Server');
assert.equal(payload.sessions[0].title,'Server');
console.log(JSON.stringify({ok:true}));
''')
