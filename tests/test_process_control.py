"""Run real subprocess trees; these checks cover lifecycle, not implementation shape."""
import os
import subprocess
import sys
import time

from agent_service.process_control import spawn, stop


def test_owned_child_utf8_and_exit():
    process=spawn([sys.executable,'-c',"print('精确金额 118.15')"],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    output,error=process.process.communicate(timeout=10)
    assert process.wait()==0
    assert output.decode('utf-8').strip()=='精确金额 118.15'
    assert not error


def test_stopping_owned_tree_stops_descendant(tmp_path):
    beat=tmp_path/'heartbeat'
    child=tmp_path/'descendant.py'
    child.write_text("import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nwhile True:\n p.write_text(str(time.time_ns()))\n time.sleep(.03)\n",encoding='utf-8')
    parent=tmp_path/'parent.py'
    parent.write_text("import subprocess,sys,time\nsubprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\nwhile True:time.sleep(1)\n",encoding='utf-8')
    process=spawn([sys.executable,str(parent),str(child),str(beat)])
    try:
        end=time.monotonic()+10
        while not beat.exists() and time.monotonic()<end:time.sleep(.05)
        assert beat.exists(),'descendant never started'
        stop(process,timeout=5)
        current=beat.stat().st_mtime_ns
        time.sleep(.2)
        assert beat.stat().st_mtime_ns==current,'descendant outlived its owned process tree'
    finally:
        if process.poll() is None:stop(process)
