"""Review-3 DeepSeek client paint-release probes (my copy only)."""
import subprocess
from pathlib import Path

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'static/sessions.js').read_text()


def _functions(names):
    out = []
    for name in names:
        prefix = 'async ' if name == '_loadSidebarSessionListPayload' else ''
        out.append(prefix + extract_function(SOURCE, name))
    return '\n'.join(out)


def node(script):
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_rename_release_paints_once_with_pending():
    fn = extract_function(SOURCE, '_buildSessionRenameStarter')
    out = node(r'''
const assert=require('node:assert/strict');
let _renamingSid=null,_loadingSessionId=null,input,paints=0;
const _isReadOnlySession=()=>false,closeSessionActionMenu=()=>{},
  _sessionDisplayTitle=s=>s.title,_markNonCredentialInput=()=>{},
  setTimeout=fn=>{fn();return 0;};
const document={createElement:()=>({addEventListener(){},isConnected:true,replaceWith(){},focus(){},select(){},value:''})};
const display={replaceWith:el=>{input=el;}};
let pending=true;
const renderSessionListFromCache=()=>{paints++;};
const _resumePendingSidebarPaint=()=>{paints++;pending=false;};
const _sidebarPendingPaint={};
const session={session_id:'a',title:'Old'};
const _allSessions=[session];
const S={session:null};
''' + fn + r'''
_buildSessionRenameStarter(session,display,()=>{})();
input.onkeydown({key:'Escape',preventDefault(){},stopPropagation(){}});
console.log(JSON.stringify({paints,pending}));
assert.equal(paints,1,'rename release must paint exactly once');
assert.equal(_renamingSid,null);
''')


def test_rename_release_paints_once_without_pending():
    fn = extract_function(SOURCE, '_buildSessionRenameStarter')
    out = node(r'''
const assert=require('node:assert/strict');
let _renamingSid=null,_loadingSessionId=null,input,paints=0,resumes=0;
const _isReadOnlySession=()=>false,closeSessionActionMenu=()=>{},
  _sessionDisplayTitle=s=>s.title,_markNonCredentialInput=()=>{},
  setTimeout=fn=>{fn();return 0;};
const document={createElement:()=>({addEventListener(){},isConnected:true,replaceWith(){},focus(){},select(){},value:''})};
const display={replaceWith:el=>{input=el;}};
const renderSessionListFromCache=()=>{paints++;};
const _resumePendingSidebarPaint=()=>{resumes++;};
const _sidebarPendingPaint=null;
const session={session_id:'a',title:'Old'};
const _allSessions=[session];
const S={session:null};
''' + fn + r'''
_buildSessionRenameStarter(session,display,()=>{})();
input.onkeydown({key:'Escape',preventDefault(){},stopPropagation(){}});
console.log(JSON.stringify({paints,resumes}));
assert.equal(paints,1);
assert.equal(resumes,0);
''')


def test_flush_pending_paint_bails_and_records():
    flush = extract_function(SOURCE, '_flushPendingSidebarPaint')
    out = node(r'''
const assert=require('node:assert/strict');
let _allSessions=[];let _renamingSid=null,_sessionActionMenu=null,_sessionListSkeletonActive=false;
let _lastSessionListRenderSig='old',recorded=0;
const _recordSidebarPaint=()=>recorded++;
let mode='sync';
const renderSessionListFromCache=()=>{ if(mode==='false') return false; if(mode==='promiseFalse') return Promise.resolve(false); return undefined; };
let _sidebarPendingPaint={signature:'new',sessions:_allSessions,sessData:{}};
''' + flush + r'''
(async()=>{
  _flushPendingSidebarPaint();
  assert.equal(recorded,1);assert.equal(_lastSessionListRenderSig,'new');assert.equal(_sidebarPendingPaint,null);
  // A renderer that declines must not certify the DOM.
  _lastSessionListRenderSig='old';recorded=0;_sidebarPendingPaint={signature:'new',sessions:_allSessions,sessData:{}};mode='false';
  _flushPendingSidebarPaint();
  assert.equal(recorded,0);assert.equal(_lastSessionListRenderSig,'old');assert.ok(_sidebarPendingPaint);
  // An async completion that reports an incomplete paint must not certify either.
  mode='promiseFalse';
  await _flushPendingSidebarPaint();
  assert.equal(recorded,0);assert.ok(_sidebarPendingPaint);
  console.log(JSON.stringify({recorded}));
})().catch(e=>{console.error(e);process.exitCode=1});
''')
