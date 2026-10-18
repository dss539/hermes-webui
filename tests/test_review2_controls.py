"""Small pre-repair control and source-stamp cost probe, never live state."""
from pathlib import Path
import json
import statistics
import subprocess
import sys
import time

from api import sidebar_revision as revision


def old_module(path):
    source = subprocess.check_output(['git','show',f'e5ca03c3:{path}'],text=True)
    namespace = {'__name__':'review2_control_module'}
    exec(compile(source,path,'exec'),namespace)
    return namespace


def test_pre_repair_two_process_derivative_control(tmp_path):
    old = old_module('api/session_summary_index.py')
    read, publish = old['read_index'], old['publish_index']
    path = tmp_path/'_index.json'
    def other(rows):
        path.write_text(json.dumps(rows))
        code = "import json,sys,subprocess; ns={'__name__':'control'}; exec(compile(subprocess.check_output(['git','show','e5ca03c3:api/session_summary_index.py'],text=True),'old.py','exec'),ns); print(json.dumps(ns['read_index'](sys.argv[1])))"
        result = subprocess.run([sys.executable,'-c',code,str(path)],check=True,capture_output=True,text=True)
        assert json.loads(result.stdout) == rows
    try:
        path.write_text('[{"session_id":"a","title":"old"}]')
        read(path)
        other([{'session_id':'b','title':'new'}])
        assert read(path) == [{'session_id':'b','title':'new'}]
        # Leave this process stale again before it publishes an empty index.
        other([{'session_id':'c','title':'newer'}])
        path.write_text('[]')
        st=path.stat()
        publish(path,[],(st.st_dev,st.st_ino,st.st_mtime_ns,st.st_size))
        old['clear']()
        assert read(path) == []
        print('pre_repair_external_commit_and_restart_control=PASS')
    finally:
        old['clear']()


def test_sidecar_stamp_cost_comparison(tmp_path):
    for i in range(2000):
        (tmp_path/f'{i}.json').write_text('{}')
    old=old_module('api/sidebar_revision.py')['sidecar_revision']
    timings={'pre_repair':[], 'repaired':[]}
    for _ in range(8):
        for label, function in [('pre_repair',old),('repaired',revision.sidecar_revision)]:
            started=time.perf_counter()
            stamp=function(tmp_path)
            timings[label].append((time.perf_counter()-started)*1000)
            assert len(stamp)==2000
    assert old(tmp_path)==revision.sidecar_revision(tmp_path)
    print('sidecar_2000_files_median_ms=',json.dumps({key:statistics.median(values) for key,values in timings.items()}))
