"""Native host observations are refreshed on every tick, without live writes."""
import os
import subprocess

import pytest

from hermes_cli import kanban_db_dispatch as kbd


@pytest.mark.platforms("macos")
def test_native_sampler_reads_logicalcpu_vm_load_and_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(os, "getloadavg", lambda: (25.0, 1.0, 1.0))
    calls = []
    def sysctl(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] <= 2
        key = argv[-1]
        return subprocess.CompletedProcess(argv, 0, stdout={
            "hw.logicalcpu": "20\n", "kern.memorystatus_vm_pressure_level": "2\n",
        }[key])
    monkeypatch.setattr(subprocess, "run", sysctl)
    sample = kbd._host_pressure_sample()
    assert sample == {"load1": 25.0, "logicalcpu": 20, "vm": 2, "stall_age": None}
    marker = tmp_path / "run" / "loop_stall.ts"
    marker.parent.mkdir()
    marker.touch()
    sample = kbd._host_pressure_sample()
    assert 0 <= sample["stall_age"] < 2
    assert len(calls) == 4
    assert {tuple(c) for c in calls} == {
        ("/usr/sbin/sysctl", "-n", "hw.logicalcpu"),
        ("/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"),
    }


@pytest.mark.platforms("macos")
def test_native_sampler_does_not_swallow_sysctl_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    def broken(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)
    monkeypatch.setattr(subprocess, "run", broken)
    with pytest.raises(subprocess.TimeoutExpired):
        kbd._host_pressure_sample()
