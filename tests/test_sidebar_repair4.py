"""Fresh exact fallback lifetime and canonical-index source-fence controls."""
from contextlib import closing
import json
import os
import sqlite3

import pytest
from api import agent_session_summaries as summaries, session_summary_index as index
from api.sidebar_revision import path_revision
from tests.test_sidebar_repair3 import build


@pytest.fixture(autouse=True)
def reset():
    summaries.clear()
    index.clear()
    yield
    summaries.clear()
    index.clear()


@pytest.mark.parametrize('consumer_raises', [False, True])
def test_ordered_fallback_is_fresh_readonly_and_closed(tmp_path, consumer_raises):
    path=build(tmp_path/'state.db', keys=(10,10,20))
    with summaries.summary_connection(path) as conn:
        retained=conn.sidebar_source
    handles=[]
    for _ in range(2):
        try:
            with summaries.summary_connection(path, ordered=True) as conn:
                handles.append(conn)
                assert conn is not retained
                with pytest.raises(sqlite3.OperationalError):
                    conn.execute('DELETE FROM sessions')
                conn.rollback()
                if consumer_raises:
                    raise ValueError('consumer failure is not replayed')
        except ValueError:
            assert consumer_raises
        with pytest.raises(sqlite3.ProgrammingError):
            handles[-1].execute('SELECT 1')
    assert handles[0] is not handles[1]
    assert retained.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]==3
    assert not retained.in_transaction
    assert summaries.counters()['tie_fallbacks']==2
    assert summaries.counters()['full_fact_rereads']==1


def test_fallback_diagnostics_are_opt_in_numeric_and_content_free(tmp_path,monkeypatch,capsys):
    path=build(tmp_path/'private-session-path.db',keys=(10,10,20))
    with summaries.summary_connection(path,ordered=True): pass
    assert capsys.readouterr().out==''
    monkeypatch.setenv('HERMES_WEBUI_SIDEBAR_DIAGNOSTICS','1')
    with summaries.summary_connection(path,ordered=True): pass
    line=capsys.readouterr().out.strip()
    assert line.startswith('[sidebar-summary] ')
    assert json.loads(line.split(' ',1)[1])=={'event':'fallback','tie_fallbacks':2,'legacy_fallbacks':2}
    assert str(tmp_path) not in line


@pytest.mark.parametrize('recreate',[False,True])
def test_canonical_rewrite_same_size_and_mtime_never_reuses_stale_rows(tmp_path,recreate,monkeypatch):
    path=tmp_path/'_index.json'
    old=[{'session_id':'a','title':'old'}];new=[{'session_id':'b','title':'new'}]
    path.write_text(json.dumps(old)); assert index.read_index(path)==old
    before=path.stat(); fence=path_revision(path)
    if recreate:path.unlink()
    path.write_text(json.dumps(new));os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    after=path.stat()
    assert after.st_size==before.st_size and after.st_mtime_ns==before.st_mtime_ns
    assert after.st_ctime_ns!=before.st_ctime_ns
    # Force inode reuse too: only the real ctime distinguishes this generation.
    # This is stronger than depending on the filesystem to reuse an inode.
    real_revision=index.path_revision
    def reused_inode(p):
        result=real_revision(p)
        return (fence[0],fence[1],result[2],result[3],result[4]) if p==path else result
    monkeypatch.setattr(index,'path_revision',reused_inode)
    assert index.read_index(path)==new
    index.clear()
    assert index.read_index(path)==new


def test_identical_rewrite_then_other_store_changed_image_reloads(tmp_path):
    path=tmp_path/'_index.json';rows=[{'session_id':'a','title':'old'}]
    path.write_text(json.dumps(rows));assert index.read_index(path)==rows
    other=index._Store(tmp_path)
    try:
        generation=other.generation
        path.write_text(json.dumps(rows));other.publish(path,rows,path_revision(path))
        assert other.generation==generation
        changed=[{'session_id':'a','title':'new'}]
        path.write_text(json.dumps(changed));other.publish(path,changed,path_revision(path))
        assert other.generation==generation+1
        assert index.read_index(path)==changed
    finally:other.conn.close()
