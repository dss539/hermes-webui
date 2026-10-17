#!/usr/bin/env python3
"""Real Chromium gate for bounded keyed sidebar redraws and unread baselines.

Runs an agent-free server on loopback with temporary state. No real sessions,
model calls or production state. --screenshots DIR writes review evidence.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

CHECK = r'''() => {
 const checks=[];
 const ok=(condition,label)=>{if(!condition)throw Error(label);checks.push(label);};
 const now=Date.now()/1000;
 const rows=Array.from({length:351},(_,i)=>({session_id:'synthetic'+String(i).padStart(4,'0'),
  title:'Synthetic conversation '+String(i).padStart(4,'0'),message_count:22,
  last_message_at:now-i*3600,updated_at:now-i*3600,created_at:now-i*3600-1,
  profile:i%7===0?'synthetic-other':'default',source_tag:i===8?'cli':'webui',
  raw_source:i===8?'cli':'webui',session_source:i===8?'cli':'webui',is_cli_session:i===8,
  pinned:i>=1&&i<=3,project_id:i===5?'synthetic-project':null,archived:false}));
 rows[11].parent_session_id=rows[10].session_id;rows[11].relationship_type='child_session';
 rows[11].source_tag='subagent';rows[11].session_source='subagent';rows[11].raw_source='subagent';rows[11].read_only=true;
 rows[12].parent_session_id=rows[10].session_id;rows[12].session_source='fork';
 S.session=null;S.busy=false;_allSessions=rows;_sidebarReferenceSessions=[];
 _showAllProfiles=true;_showArchived=false;_sessionSourceFilter='webui';_activeProject=null;
 _serverWebuiSessionCount=350;_serverCliSessionCount=1;
 _allProjects=[{project_id:'synthetic-project',name:'Synthetic project',color:'#4488cc',profile:'default'}];
 _sessionListRefreshAnimationPending=false;_sessionListSkeletonActive=false;
 window._sidebarDensity='compact';window._showCliSessions=true;
 $('sessionSearch').value='';
 const rendered=()=>new Map([...document.querySelectorAll('.session-item')].map(e=>[e.dataset.sid,e]));
 renderSessionListFromCache();renderSessionListFromCache();
 let before=rendered();const changed=rows[2];changed.title='Synthetic changed title';
 renderSessionListFromCache();let after=rendered();
 ok(after.get(changed.session_id)!==before.get(changed.session_id),'changed row repaints');
 ok(after.get(changed.session_id).textContent.includes(changed.title),'changed title is visible');
 ok([...before].filter(([id,e])=>id!==changed.session_id&&after.get(id)===e).length===before.size-1,'unrelated visible rows retain DOM identity');
 changed.synthetic_cycle=changed;renderSessionListFromCache();
 ok(rendered().has(changed.session_id),'unserializable client metadata falls back to repaint');delete changed.synthetic_cycle;
 ok(after.size<80&&Number($('sessionList').dataset.sessionVirtualTotal)>300,'virtual window remains bounded');
 if(typeof _sessionRowDomCache!=='undefined')ok(_sessionRowDomCache.size<=after.size,'row cache retains only current window');
 const pinned=document.querySelector('.session-date-header.pinned');
 ok(pinned&&pinned.textContent.includes('Pinned'),'pinned/date grouping retained');
 ok(!!document.querySelector('.session-project-dot'),'project color retained');
 ok([...after.values()].some(e=>e.textContent.includes('synthetic-other'))===false,'compact mode stays compact');
 // Active, streaming, unread and attention must invalidate an otherwise equal row.
 S.session={...rows[2]};renderSessionListFromCache();
 ok(rendered().get(rows[2].session_id).classList.contains('active'),'active highlight updates');
 rows[4].is_streaming=true;renderSessionListFromCache();
 ok(rendered().get(rows[4].session_id).classList.contains('streaming'),'streaming indicator updates');
 rows[4].is_streaming=false;_markSessionCompletionUnread(rows[4].session_id,23);renderSessionListFromCache();
 ok(rendered().get(rows[4].session_id).classList.contains('unread'),'unread completion updates');
 rows[4].attention={kind:'approval',count:1};renderSessionListFromCache();
 ok(rendered().get(rows[4].session_id).classList.contains('attention-approval'),'approval indicator updates');
 // Open menu and title input must not lose their anchor during a repaint.
 let anchor=rendered().get(rows[2].session_id);_openSessionActionMenu(rows[2],anchor);
 const menu=_sessionActionMenu;renderSessionListFromCache();
 ok(anchor.isConnected&&_sessionActionMenu===menu,'open action menu keeps its anchor');closeSessionActionMenu();
 const title=anchor.querySelector('.session-title');_buildSessionRenameStarter(rows[2],title,()=>{})();
 const input=document.querySelector('.session-title-input');renderSessionListFromCache();
 ok(input&&input.isConnected,'rename input survives refresh');input.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
 // Query/project/source switches still use authoritative filter/group logic.
 $('sessionSearch').value='conversation 0005';renderSessionListFromCache();
 ok([...rendered().values()].every(e=>e.textContent.includes('0005')),'search results remain filtered');
 $('sessionSearch').value='';_activeProject='synthetic-project';renderSessionListFromCache();
 ok(rendered().size===1&&rendered().has(rows[5].session_id),'project filter retained');
 _activeProject=null;_sessionSourceFilter='cli';renderSessionListFromCache();
 ok(rendered().size===1&&rendered().has(rows[8].session_id),'CLI source filter retained');
 _sessionSourceFilter='webui';window._sidebarDensity='detailed';renderSessionListFromCache();
 ok(rendered().get(rows[7].session_id).querySelector('.session-meta').textContent.includes('synthetic-other'),'all-profile detail label retained');
 const parent=rows[10],key=_sidebarLineageKeyForRow(parent);_expandedChildSessionKeys.add(key);renderSessionListFromCache();
 ok(!!rendered().get(parent.session_id).querySelector('.session-child-sessions'),'child and fork expansion retained');
 _expandedChildSessionKeys.delete(key);renderSessionListFromCache();
 // A steady authoritative payload must not reconstruct any rows.
 const sessData={sessions:rows,active_profile:'default',webui_session_count:350,cli_session_count:1};
 _applySessionListPayload(sessData,{projects:_allProjects});_applySessionListPayload(sessData,{projects:_allProjects});
 before=rendered();_applySessionListPayload(sessData,{projects:_allProjects});after=rendered();
 ok([...before].every(([id,e])=>after.get(id)===e),'unchanged payload does not rebuild rows');
 // Shared-map persistence is batched, but each clear still writes a versioned
 // ordering fact during the original per-session intent, not at batch flush.
 const sid='synthetic-batch-race';const realSet=Storage.prototype.setItem;
 const seen=[];Storage.prototype.setItem=function(k,v){seen.push(k);return realSet.call(this,k,v);};
 try{
  _sidebarUnreadBaselineBatch={dirty:false,clears:new Map()};
  _hasUnreadForSession({session_id:sid,message_count:2});
  const order=_readSessionCompletionUnreadCleared()[sid];
  ok(order>0&&seen.some(k=>k.startsWith(SESSION_COMPLETION_UNREAD_CLEARED_PREFIX)&&k.includes(sid)),'batched baseline clear is durable immediately');
  ok(!seen.includes(SESSION_VIEWED_COUNTS_KEY),'baseline shared-map save waits for batch flush');
  const saved=_getSessionCompletionUnread();saved[sid]={message_count:3,unread_order:order+1,completed_at:Date.now()};
  _sidebarUnreadBaselineBatch=null;_writeSessionCompletionUnreadCleared([],0);_saveSessionViewedCounts();_saveSessionCompletionUnread();
  ok(_hasSessionCompletionUnread(sid),'completion ordered after baseline clear survives flush');
 }finally{Storage.prototype.setItem=realSet;_sidebarUnreadBaselineBatch=null;}
 const quotaRow={...rows[0],session_id:'synthetic-quota-retry',title:'Synthetic quota retry',is_streaming:true,profile:'default'};
 let rejected=false;
 Storage.prototype.setItem=function(k,v){
  if(!rejected&&k.startsWith(SESSION_COMPLETION_UNREAD_CLEARED_PREFIX)&&k.includes(quotaRow.session_id)){rejected=true;throw new DOMException('Synthetic quota failure','QuotaExceededError');}
  return realSet.call(this,k,v);
 };
 try{
  _allSessions=[quotaRow,...rows];renderSessionListFromCache();
  ok(rejected&&_readPersistedSessionCompletionUnreadCleared()[quotaRow.session_id]>0,'batch retries an immediate quota failure after pruning');
 }finally{Storage.prototype.setItem=realSet;_allSessions=rows;renderSessionListFromCache();}
 // A collapsed date group's actual click handler still hides and restores rows.
 const header=document.querySelector('.session-date-header.pinned');header.click();
 ok(![...rendered().keys()].some(id=>rows.slice(1,4).some(s=>s.session_id===id)),'date collapse retained');
 document.querySelector('.session-date-header.pinned').click();
 ok(rendered().has(rows[2].session_id),'date expand retained');
 return {checks,domRows:rendered().size,total:Number($('sessionList').dataset.sessionVirtualTotal)};
}'''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--screenshots',type=Path);parser.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[1]);args=parser.parse_args()
    from playwright.sync_api import sync_playwright
    repo=args.repo.resolve()
    port=int(os.getenv('SIDEBAR_GATE_PORT','8914'))
    with tempfile.TemporaryDirectory(prefix='sidebar-redraw-',dir=os.getenv('TMPDIR')) as temp:
        root=Path(temp);state=root/'webui';state.mkdir()
        (state/'settings.json').write_text(json.dumps({'password_hash':None,'check_for_updates':False}))
        env={k:v for k,v in os.environ.items() if not k.endswith('_API_KEY') and not k.startswith('HERMES_WEBUI_TLS_')}
        env.update(HERMES_HOME=str(root/'hermes'),HERMES_BASE_HOME=str(root/'hermes'),
                   HERMES_WEBUI_STATE_DIR=str(state),HERMES_WEBUI_HOST='127.0.0.1',HERMES_WEBUI_PORT=str(port),
                   HERMES_WEBUI_SKIP_ONBOARDING='1',HERMES_WEBUI_AGENT_DIR=str(root/'no-agent'),
                   HERMES_WEBUI_NO_DOTENV='1',HERMES_WEBUI_PRESERVE_ENV='1',HERMES_WEBUI_ISOLATED_PROFILE='1')
        with (root/'server.log').open('w') as log:
            server=subprocess.Popen([sys.executable,str(repo/'server.py')],cwd=repo,env=env,stdout=log,stderr=subprocess.STDOUT)
            try:
                for _ in range(150):
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=1) as response:
                            if response.status==200:break
                    except Exception:pass
                    if server.poll() is not None:raise RuntimeError('test server exited')
                    time.sleep(.2)
                else:raise RuntimeError('test server health timed out')
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True)
                    context=browser.new_context(viewport={'width':820,'height':1180},has_touch=True,bypass_csp=True)
                    page=context.new_page();page.add_init_script("try{localStorage.setItem('hermes-webui-sidebar-collapsed','0')}catch{}")
                    page.goto(f'http://127.0.0.1:{port}/',wait_until='domcontentloaded')
                    page.wait_for_function("typeof renderSessionListFromCache==='function'&&_sessionListHasLoadedOnce&&!_renderSessionListInFlight",timeout=60000)
                    page.wait_for_timeout(600)
                    result=page.evaluate(CHECK)
                    print(json.dumps(result,indent=2))
                    if args.screenshots:
                        args.screenshots.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(args.screenshots/'sidebar-tablet.png'))
                    browser.close()
            finally:
                server.terminate()
                try:server.wait(timeout=10)
                except subprocess.TimeoutExpired:server.kill();server.wait(timeout=10)
    return 0

if __name__=='__main__':raise SystemExit(main())
