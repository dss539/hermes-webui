"""Run raw-window reconciliation and the real done-listener settle statements together."""
import json
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function
from tests.test_issue6112_done_settle_snapshot_guard import _harness

ROOT = Path(__file__).resolve().parents[1]
MESSAGES = (ROOT / "static/messages.js").read_text(encoding="utf-8")
UI = (ROOT / "static/ui.js").read_text(encoding="utf-8")


def _settle_window(incoming, answer="streamed final answer"):
    done = MESSAGES[MESSAGES.index("source.addEventListener('done'"):]
    reconciliation = done[done.index("const _currentTerminalSession="):done.index("const completedSid=")]
    installation = done[done.index("S.session=completedSession;"):done.index("if(typeof _hydrateTodosFromSession")]
    helpers = "\n".join(extract_function(MESSAGES, name) for name in [
        "_applyEmbeddedTerminalSession",
    ])
    rendering = "\n".join(extract_function(UI, name) for name in [
        "_isRecoveryControlMessageText", "_isRecoveryControlMessage", "_messageIsRenderable",
        "_getVisibleMessagesWithIdx", "_messageSessionIndexBase", "_messageSessionIndexForRawIdx",
    ])
    return _harness(f"""
{helpers}
{rendering}
let _oldestIdx=40, _messagesTruncated=true;
let _visWithIdxCache=null, _visWithIdxCacheLen=0, _visWithIdxCacheSrc=null;
const _isContextCompactionMessage=()=>false, _isPreservedCompressionTaskListMessage=()=>false;
const _messageHasReasoningPayload=()=>false, _assistantMessageHasVisibleContent=m=>!!m.content;
const S={{session:{{session_id:'same',message_count:44,tool_calls:[{{id:'before',assistant_msg_idx:0}}]}},messages:[
  {{id:41,role:'assistant',content:'older reply'}},{{id:42,role:'user',content:'question'}},
  {{id:43,role:'assistant',content:'old control',recovery_control:true}},{{id:44,role:'tool',content:'old result'}}
]}};
const streamId='stream', INFLIGHT={{}}, isActiveSession=true;
const d={{session:{json.dumps(incoming)}}}, activeSid='same';
{reconciliation}
const completedSid=completedSession.session_id, _pendingTitleUpdates=new Map(), _pendingTitleExpectedCurrent=new Map();
const _preSettle=S.messages, _doneFinalAnswer={json.dumps(answer)};
{installation}
const visible=_getVisibleMessagesWithIdx().map(({{m,rawIdx}})=>[m.content,_messageSessionIndexForRawIdx(rawIdx)]);
// A later authoritative window must not reuse a locally re-attached answer as a raw row.
const next=_applyEmbeddedTerminalSession({{session_id:completedSid,message_count:45,_messages_offset:42,
  _messages_truncated:true,messages:[{{role:'assistant',content:'control',recovery_control:true}},
    {{role:'tool',content:'result'}},{{role:'assistant',content:'persisted final answer'}}],tool_calls:[]}},
  S.session,S.messages,_oldestIdx);
console.log(JSON.stringify({{offset:_oldestIdx,truncated:_messagesTruncated,count:S.session.message_count,
  messages:S.messages,visible,calls:S.session.tool_calls,next}}));
""")


@pytest.mark.parametrize("snapshot_answer", ["streamed final answer", "rewritten final answer", None])
def test_done_keeps_raw_recovery_coordinates_and_final_answer(snapshot_answer):
    messages = [
        {"id": 43, "role": "assistant", "content": "control", "recovery_control": True},
        {"id": 44, "role": "tool", "content": "result"},
    ]
    if snapshot_answer:
        messages.append({"id": 45, "role": "assistant", "content": snapshot_answer})
    result = _settle_window({
        "session_id": "same", "message_count": 42 + len(messages), "_messages_offset": 42,
        "_messages_truncated": True, "messages": messages,
        "tool_calls": [{"id": "after", "assistant_msg_idx": 0}],
    })
    assert result["offset"] == 40  # merged window, not incoming tail's 42
    assert result["truncated"] is True
    assert result["messages"][2]["recovery_control"] is True
    assert result["messages"][3]["role"] == "tool"
    expected_answer = snapshot_answer or "streamed final answer"
    assert result["visible"] == [["older reply", 40], ["question", 41], [expected_answer, 44]]
    assert [(c["id"], c["assistant_msg_idx"]) for c in result["calls"]] == [("before", 0), ("after", 2)]
    assert len(result["messages"]) == 5  # no duplicate of rewritten/persisted final answer
    assert result["count"] == 42 + len(messages)  # never count a display-only fallback as persisted
    assert result["next"] is not None
    assert result["next"]["messages"][-1]["content"] == "persisted final answer"
    assert all(m["content"] != "streamed final answer" for m in result["next"]["messages"])


@pytest.mark.parametrize("role", ["assistant", "user"])
def test_recovery_tail_does_not_hide_authoritative_rewritten_answer(role):
    result = _harness(f"""
const question={{role:'user',content:'question'}};
const rewritten={{role:'assistant',content:'authoritative rewritten answer'}};
const control={{role:{json.dumps(role)},content:'[System: continue exactly where you left off]',recovery_control:true}};
const settled=[question,rewritten,control];
const out=_adoptDoneSnapshotMessages([question],settled,'streamed original answer');
console.log(JSON.stringify({{messages:out.messages}}));
""")
    assert len(result["messages"]) == 3
    assert result["messages"][1]["content"] == "authoritative rewritten answer"
    assert result["messages"][2]["recovery_control"] is True


def test_recovery_text_cannot_impersonate_the_streamed_answer():
    result = _harness("""
const question={role:'user',content:'question'};
const control={role:'assistant',content:'streamed answer',recovery_control:true};
const out=_adoptDoneSnapshotMessages([question],[question,control],'streamed answer');
console.log(JSON.stringify({messages:out.messages}));
""")
    assert len(result["messages"]) == 3
    assert result["messages"][-1] == {"role": "assistant", "content": "streamed answer"}


def test_done_metadata_and_anchor_skip_retained_recovery_tail():
    done = MESSAGES[MESSAGES.index("source.addEventListener('done'"):]
    start = done.index("lastAsst=", done.index("// Find the last assistant message once"))
    selection = done[start:done.index(";", start) + 1]
    attach = extract_function(MESSAGES, "_attachProjectedAnchorSceneToLastAssistant")
    result = _harness(f"""
{attach}
const S={{messages:[{{role:'user',content:'question'}},{{role:'assistant',content:'answer'}},
  {{role:'assistant',content:'control',recovery_control:true}}]}};
let lastAsst=null;
const _mergedTerminalSession=true, _ownsCurrentTerminalStream=false, _localTerminalAssistantIndex=-1;
{selection}
const streamId='stream',_anchorRegistry={{}};
const scene={{activity_rows:[{{role:'tool'}}]}};
const _projectLiveAnchorActivityScene=()=>scene;
const _completeSettledAnchorSceneForTurn=()=>scene;
const _anchorSceneHasOwnedOutcomes=()=>false,_anchorSceneHasWorklogWorthyRows=()=>true;
let persistedIndex=null;
const _persistSettledAnchorScene=(m,s,idx)=>{{persistedIndex=idx}};
_attachProjectedAnchorSceneToLastAssistant(S.messages);
console.log(JSON.stringify({{selected:lastAsst.content,persistedIndex,messages:S.messages}}));
""")
    assert result["selected"] == "answer"
    assert result["persistedIndex"] == 1
    assert "_anchor_activity_scene" in result["messages"][1]
    assert "_anchor_activity_scene" not in result["messages"][2]


def test_empty_rotated_snapshot_keeps_visible_window_cursor():
    result = _settle_window({
        "session_id": "rotated", "parent_session_id": "unrelated", "message_count": 0,
        "_messages_offset": 0, "_messages_truncated": False, "messages": [],
    }, answer="")
    assert result["offset"] == 40
    assert result["truncated"] is True
    assert result["visible"] == [["older reply", 40], ["question", 41]]
