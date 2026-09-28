import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from scripts.batch_submit import create_batch_jobs, load_input_list
from scripts.cron_sweep_jobs import SweepLock, assess_job, discover_jobs, sweep_once
from scripts.resume_job import build_pipeline_cmd
from core.job_state import ensure_job_layout, read_progress, resolve_coordination_root, update_progress


class DummyArgs:
    def __init__(self, **kwargs):
        self.source_lang = "en"
        self.target_language = "Chinese"
        self.translation_model = "deepseek"
        self.translation_style = "faithful"
        self.translation_context = "auto"
        self.translation_batch_size = 25
        self.translation_workers = 1
        self.asr_engine = "auto"
        self.tts_engine = "qwen3-tts"
        self.subtitle_mode = "target"
        self.source_srt = None
        self.terms_file = None
        self.profile = None
        self.env_file = None
        self.timing_risk_estimator = True
        self.allow_source_fallback = False
        for k, v in kwargs.items():
            setattr(self, k, v)


# ==============================================================================
# 1. Real CLI Arguments Compatibility (Fixing P1 bug 1)
# ==============================================================================
def test_build_pipeline_cmd_generates_valid_run_pipeline_arguments(tmp_path):
    """Verify that build_pipeline_cmd generates arguments that run_pipeline.py parses cleanly without unrecognized arguments."""
    from scripts.run_pipeline import main as _  # ensure module imports cleanly

    batch_dir = tmp_path / "batch"
    args = DummyArgs(timing_risk_estimator=True, allow_source_fallback=True)
    created = create_batch_jobs(["https://www.youtube.com/watch?v=xyz"], batch_dir, args)
    job_dir = Path(created[0]["job_dir"])
    config = json.loads((job_dir / "job_config.json").read_text(encoding="utf-8"))

    cmd = build_pipeline_cmd(config, job_dir)
    # cmd[0] is sys.executable, cmd[1] is run_pipeline.py
    cli_args = cmd[2:]

    # Ensure --timing-risk-estimator is present without trailing "True"
    assert "--timing-risk-estimator" in cli_args
    idx = cli_args.index("--timing-risk-estimator")
    if idx + 1 < len(cli_args):
        assert cli_args[idx + 1] != "True"

    # Now verify with run_pipeline.py's ACTUAL parse_args()!
    from scripts.run_pipeline import parse_args
    with patch("sys.argv", ["run_pipeline.py", *cli_args]):
        parsed = parse_args()

    assert parsed.url == "https://www.youtube.com/watch?v=xyz"
    assert parsed.target_language == "Chinese"
    assert parsed.timing_risk_estimator is True
    assert parsed.allow_source_fallback is True
    assert parsed.url == "https://www.youtube.com/watch?v=xyz"
    assert parsed.target_language == "Chinese"
    assert parsed.timing_risk_estimator is True
    assert parsed.allow_source_fallback is True


def test_build_pipeline_cmd_handles_false_paired_booleans(tmp_path):
    job_dir = tmp_path / "job_bool"
    ensure_job_layout(job_dir)
    config = {
        "url": "https://example.com/v.mp4",
        "timing_risk_estimator": False,
        "allow_atempo_overflow": False,
        "embed_cover": False,
        "early_original_output": False,
        "ignore_yt_dlp_config": False,
    }
    cmd = build_pipeline_cmd(config, job_dir)
    cli_args = cmd[2:]
    assert "--no-timing-risk-estimator" in cli_args
    assert "--no-atempo-overflow" in cli_args
    assert "--no-embed-cover" in cli_args
    assert "--no-early-original-output" in cli_args
    assert "--use-yt-dlp-config" in cli_args


# ==============================================================================
# 2. Concurrency Gating for Stalled Jobs Recovery (Fixing P1 bug 2)
# ==============================================================================
def test_cron_sweep_recovery_respects_max_parallel_limit(tmp_path):
    """When max_parallel=1, if 2 jobs are stalled, only 1 should be resumed."""
    # Setup 2 stalled jobs
    resumed = []

    def mock_subprocess(cmd, **_kwargs):
        resumed.append(cmd)

    for i in (1, 2):
        j = tmp_path / f"stalled_{i}"
        ensure_job_layout(j)
        (j / "job_config.json").write_text(json.dumps({"target_language": "Chinese"}), encoding="utf-8")
        (j / "job_pid.txt").write_text("99999999", encoding="utf-8")
        status_file = j / "pipeline_status.json"
        status_file.write_text(json.dumps({"status": "running"}), encoding="utf-8")
        past_time = time.time() - 10000
        os.utime(status_file, (past_time, past_time))
        update_progress(j, status="running", stale_count=0, resume_count=0)

    with patch("subprocess.check_call", side_effect=mock_subprocess):
        report = sweep_once(tmp_path, max_parallel=1, stale_sec=300)

    # Exactly 1 job should have been resumed to obey max_parallel=1!
    assert report["resumed"] == 1
    assert len(resumed) == 1
    assert report["active_running_total"] == 1


# ==============================================================================
# 3. Inter-Process Lock and Atomic Claims (Fixing P1 bug 3)
# ==============================================================================
def test_sweep_lock_prevents_concurrent_sweeps(tmp_path):
    lock_path = tmp_path / ".sweep.lock"
    with SweepLock(lock_path) as lock1:
        assert lock1 is not None
        # Try acquiring a second lock concurrently
        with SweepLock(lock_path) as lock2:
            assert lock2 is None

    # After exiting, lock can be acquired again
    with SweepLock(lock_path) as lock3:
        assert lock3 is not None


def test_start_detached_job_skips_when_job_already_running(tmp_path):
    from scripts.start_detached_job import main as start_main

    job_dir = tmp_path / "job_running"
    ensure_job_layout(job_dir)
    # Write current live process PID
    (job_dir / "job_pid.txt").write_text(str(os.getpid()), encoding="utf-8")

    with patch("sys.argv", ["start_detached_job.py", "--job-dir", str(job_dir)]):
        with patch("subprocess.Popen") as mock_popen:
            start_main()
            mock_popen.assert_not_called()


# ==============================================================================
# 4. Batch Submit Directory Collisions & Clean Overwrites (Fixing P1 bug 4)
# ==============================================================================
def test_batch_submit_appends_without_overwriting_old_jobs(tmp_path):
    batch_dir = tmp_path / "my_batch"
    args = DummyArgs()

    # First submission: job_001, job_002
    create_batch_jobs(["video1.mp4", "video2.mp4"], batch_dir, args)
    assert (batch_dir / "job_001").exists()
    assert (batch_dir / "job_002").exists()

    # Create dummy artifact in job_001
    artifact_file = batch_dir / "job_001" / "raw_audio.wav"
    artifact_file.write_text("old audio data", encoding="utf-8")

    # Second submission to same batch dir without overwrite: must create job_003 and job_004
    created2 = create_batch_jobs(["video3.mp4", "video4.mp4"], batch_dir, args, overwrite=False)
    assert len(created2) == 2
    assert created2[0]["job_name"] == "job_003"
    assert created2[1]["job_name"] == "job_004"
    assert (batch_dir / "job_003").exists()
    assert (batch_dir / "job_004").exists()

    # Old job_001 and its artifact must be untouched
    assert artifact_file.exists()
    assert artifact_file.read_text(encoding="utf-8") == "old audio data"


def test_batch_submit_cleans_artifacts_when_overwrite_specified(tmp_path):
    batch_dir = tmp_path / "my_batch_overwrite"
    args = DummyArgs()

    create_batch_jobs(["video1.mp4"], batch_dir, args)
    artifact_file = batch_dir / "job_001" / "raw_audio.wav"
    artifact_file.write_text("old audio data", encoding="utf-8")

    # Submit new input with overwrite=True
    create_batch_jobs(["new_video.mp4"], batch_dir, args, overwrite=True)
    # Old audio artifact must be wiped clean so it doesn't pollute the new video
    assert not artifact_file.exists()
    config = json.loads((batch_dir / "job_001" / "job_config.json").read_text(encoding="utf-8"))
    assert "new_video.mp4" in config["input_video"]


# ==============================================================================
# 5. Strict Stalled Evaluation & Grace Period (Fixing P2 bug 5)
# ==============================================================================
def test_cron_sweep_respects_grace_period_and_does_not_resume_fresh_job(tmp_path):
    """If process PID is dead but heartbeat is fresh (< stale_sec), do not resume."""
    job_dir = tmp_path / "fresh_job"
    ensure_job_layout(job_dir)
    (job_dir / "job_config.json").write_text(json.dumps({"target_language": "Chinese"}), encoding="utf-8")
    (job_dir / "job_pid.txt").write_text("99999999", encoding="utf-8")

    # Fresh heartbeat (timestamp is right now)
    status_file = job_dir / "pipeline_status.json"
    status_file.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    update_progress(job_dir, status="running")

    info = assess_job(job_dir, stale_sec=300)
    assert info["alive"] is False
    assert info["heartbeat_stale"] is False
    assert info["stalled"] is False  # Must not be marked stalled!

    resumed = []
    with patch("subprocess.check_call", side_effect=lambda cmd, **k: resumed.append(cmd)):
        report = sweep_once(tmp_path, max_parallel=2, stale_sec=300)

    # Must NOT resume a fresh job that is in grace period
    assert report["resumed"] == 0
    assert len(resumed) == 0


# ==============================================================================
# 6. L0 Resident Guard Missing Heartbeat Bootstrap & Stale Check (Fixing P2 bug 6)
# ==============================================================================
def test_l0_resident_guard_bootstraps_missing_heartbeat(tmp_path):
    hb_file = tmp_path / ".l1_heartbeat"
    log_file = tmp_path / "l0_bootstrap.log"
    script = SKILL_DIR / "scripts" / "l0_resident_guard.sh"

    cmd = [
        "sh",
        str(script),
        "--once",
        "--heartbeat-file",
        str(hb_file),
        "--jobs-dir",
        str(tmp_path),
        "--log-file",
        str(log_file),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    log_content = log_file.read_text(encoding="utf-8")
    assert "WARNING: L1 heartbeat file not found" in log_content
    assert "Triggering initial sweep" in log_content


# ==============================================================================
# 7. Full Lifecycle: Submit -> Launch -> Stall -> Recover -> Finish -> Next
# ==============================================================================
def test_full_lifecycle_submit_dispatch_stall_recovery(tmp_path):
    batch_dir = tmp_path / "batch_lifecycle"
    args = DummyArgs()
    created = create_batch_jobs(["v1.mp4", "v2.mp4"], batch_dir, args)
    assert len(created) == 2

    job1 = batch_dir / "job_001"
    job2 = batch_dir / "job_002"

    launched_cmds = []

    def mock_subprocess(cmd, **_kwargs):
        launched_cmds.append(cmd)
        # Check if launching job1
        if str(job1) in cmd:
            update_progress(job1, status="running")
            (job1 / "job_pid.txt").write_text(str(os.getpid()), encoding="utf-8")
        elif str(job2) in cmd:
            update_progress(job2, status="running")
            (job2 / "job_pid.txt").write_text(str(os.getpid()), encoding="utf-8")

    # Step 1: Initial sweep with max_parallel=1
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep1 = sweep_once(batch_dir, max_parallel=1)

    assert rep1["dispatched"] == 1
    assert rep1["active_running_total"] == 1
    # job1 running, job2 pending
    assert json.loads((job1 / "state" / "progress.json").read_text())["status"] == "running"
    assert json.loads((job2 / "state" / "progress.json").read_text())["status"] == "pending"

    # Step 2: Simulate job1 crashing / stalling
    (job1 / "job_pid.txt").write_text("99999999", encoding="utf-8")
    status_file1 = job1 / "pipeline_status.json"
    past = time.time() - 10000
    os.utime(status_file1, (past, past))

    # Second sweep: job1 is stalled. Available slots=1. job1 resumed, job2 stays pending!
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep2 = sweep_once(batch_dir, max_parallel=1, stale_sec=300)

    assert rep2["resumed"] == 1
    assert rep2["dispatched"] == 0
    assert rep2["active_running_total"] == 1
    assert json.loads((job2 / "state" / "progress.json").read_text())["status"] == "pending"

    # Step 3: Simulate job1 completing
    update_progress(job1, status="completed")
    (job1 / "pipeline_status.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")

    # Third sweep: job1 is completed (active=0). Available slots=1. job2 is dispatched!
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep3 = sweep_once(batch_dir, max_parallel=1, stale_sec=300)

    assert rep3["completed"] == 1
    assert rep3["dispatched"] == 1
    assert rep3["active_running_total"] == 1
    assert json.loads((job2 / "state" / "progress.json").read_text())["status"] == "running"


# ==============================================================================
# 8. L0 Default Directory Resolution (Fixing P1 bug)
# ==============================================================================
def test_l0_default_directory_resolves_to_repo_root(tmp_path):
    """Verify that l0_resident_guard.sh defaults to the actual repo root output, not skills/.../output."""
    script = SKILL_DIR / "scripts" / "l0_resident_guard.sh"
    log_file = tmp_path / "l0_default_dir.log"

    # Run with --once and specify only log file so default jobs-dir and heartbeat-file are tested
    cmd = [
        "sh",
        str(script),
        "--once",
        "--log-file",
        str(log_file),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    log_content = log_file.read_text(encoding="utf-8")

    # Expected repo root is the parent directory of skills
    repo_root = SKILL_DIR.parent.parent.resolve()
    expected_jobs_dir = str(repo_root / "output")
    expected_heartbeat = str(repo_root / "output" / ".l1_heartbeat")

    assert f"Heartbeat: {expected_heartbeat}" in log_content
    assert f"JobsDir: {expected_jobs_dir}" in log_content


# ==============================================================================
# 9. Overwrite Rejection on Active/Running Jobs (Fixing P1 bug)
# ==============================================================================
def test_batch_submit_overwrite_rejects_active_running_job(tmp_path):
    """Verify that --overwrite refuses to wipe out or reconfigure a job currently running."""
    batch_dir = tmp_path / "batch_active"
    args = DummyArgs()

    create_batch_jobs(["v1.mp4"], batch_dir, args)
    job1 = batch_dir / "job_001"
    # Mark job1 as running with live process PID
    update_progress(job1, status="running")
    (job1 / "job_pid.txt").write_text(str(os.getpid()), encoding="utf-8")
    artifact = job1 / "raw_audio.wav"
    artifact.write_text("in-progress audio", encoding="utf-8")

    # Attempting to overwrite must raise RuntimeError
    with pytest.raises(RuntimeError, match="Cannot overwrite active job"):
        create_batch_jobs(["new_v1.mp4"], batch_dir, args, overwrite=True)

    # In-progress artifact must NOT be deleted
    assert artifact.exists()
    assert artifact.read_text(encoding="utf-8") == "in-progress audio"


# ==============================================================================
# 10. Exact Max Resumes Count: 3 Retries (Fixing P2 counting issue)
# ==============================================================================
def test_cron_sweep_allows_exactly_max_resumes_attempts(tmp_path):
    """Verify that max_resumes=3 allows exactly 3 automatic resumes before structurally_stuck."""
    job_dir = tmp_path / "retry_job"
    ensure_job_layout(job_dir)
    (job_dir / "job_config.json").write_text(json.dumps({"target_language": "Chinese"}), encoding="utf-8")
    (job_dir / "job_pid.txt").write_text("99999999", encoding="utf-8")
    status_file = job_dir / "pipeline_status.json"
    status_file.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    update_progress(job_dir, status="running")

    def make_stalled():
        past = time.time() - 10000
        os.utime(status_file, (past, past))

    resumed_counts = []

    def mock_subprocess(cmd, **_kwargs):
        resumed_counts.append(cmd)
        # simulate resume_job updating resume_count
        prog = json.loads((job_dir / "state" / "progress.json").read_text(encoding="utf-8"))
        update_progress(job_dir, status="running", resume_count=int(prog.get("resume_count", 0)) + 1)

    with patch("subprocess.check_call", side_effect=mock_subprocess):
        # 1st stall -> Resume #1
        make_stalled()
        r1 = sweep_once(tmp_path, max_parallel=1, stale_sec=300, max_resumes=3)
        assert r1["resumed"] == 1
        assert len(resumed_counts) == 1

        # 2nd stall -> Resume #2
        make_stalled()
        r2 = sweep_once(tmp_path, max_parallel=1, stale_sec=300, max_resumes=3)
        assert r2["resumed"] == 1
        assert len(resumed_counts) == 2

        # 3rd stall -> Resume #3 (must allow 3rd attempt!)
        make_stalled()
        r3 = sweep_once(tmp_path, max_parallel=1, stale_sec=300, max_resumes=3)
        assert r3["resumed"] == 1
        assert len(resumed_counts) == 3

        # 4th stall -> Reached limit (3 retries done with no progress) -> structurally_stuck
        make_stalled()
        r4 = sweep_once(tmp_path, max_parallel=1, stale_sec=300, max_resumes=3)
        assert r4["resumed"] == 0
        assert r4["structurally_stuck"] == 1
        # No 4th resume attempt!
        assert len(resumed_counts) == 3

    final_prog = json.loads((job_dir / "state" / "progress.json").read_text(encoding="utf-8"))
    assert final_prog["status"] == "structurally_stuck"


# ==============================================================================
# 11. Initial Start Does Not Count as Retry (Fixing P1 bug)
# ==============================================================================
def test_start_detached_job_initial_start_does_not_count_as_retry(tmp_path):
    """Verify that start_detached_job.py and resume_job.py do not count initial startup as a retry."""
    from scripts.resume_job import main as resume_job_main
    from scripts.start_detached_job import main as start_detached_main

    batch_dir = tmp_path / "batch_init"
    args = DummyArgs()
    created = create_batch_jobs(["https://example.com/video1.mp4"], batch_dir, args)
    job_dir = Path(created[0]["job_dir"])

    init_prog = read_progress(job_dir)
    assert init_prog["status"] == "pending"
    assert init_prog["resume_count"] == 0
    assert init_prog["total_resumes"] == 0

    class MockProc:
        pid = 88888

    # Test 1: start_detached_job.py launches resume_job.py with --initial
    with patch("sys.argv", ["start_detached_job.py", "--job-dir", str(job_dir)]), \
         patch("subprocess.Popen", return_value=MockProc()) as mock_popen:
        start_detached_main()

    assert mock_popen.called
    launched_cmd = mock_popen.call_args[0][0]
    assert "--initial" in launched_cmd
    assert "--job-dir" in launched_cmd

    # After start_detached_job writes PID and marks running, resume_count must still be 0
    prog_after_detached = read_progress(job_dir)
    assert prog_after_detached["status"] == "running"
    assert prog_after_detached["resume_count"] == 0
    assert prog_after_detached["total_resumes"] == 0

    # Test 2: resume_job.py executed with --initial sets resume_count=0 and total_resumes=0
    with patch("sys.argv", ["resume_job.py", "--job-dir", str(job_dir), "--initial"]), \
         patch("subprocess.check_call") as mock_check_call:
        resume_job_main()

    assert mock_check_call.called
    prog_after_resume = read_progress(job_dir)
    assert prog_after_resume["status"] == "running"
    assert prog_after_resume["resume_count"] == 0
    assert prog_after_resume["total_resumes"] == 0

    # Verify event log contains initial_start
    work_log = job_dir / "logs" / "work.jsonl"
    assert work_log.exists()
    events = [json.loads(line)["event"] for line in work_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert "initial_start" in events


def test_resume_job_auto_detects_initial_versus_failure_recovery(tmp_path):
    """Verify that resume_job without --initial still distinguishes fresh pending job from a failed/stalled job."""
    from scripts.resume_job import main as resume_job_main

    batch_dir = tmp_path / "batch_detect"
    args = DummyArgs()
    created = create_batch_jobs(["https://example.com/v.mp4"], batch_dir, args)
    job_dir = Path(created[0]["job_dir"])

    # Fresh job: status=pending, existing_resumes=0 -> Auto-detected as initial start
    with patch("sys.argv", ["resume_job.py", "--job-dir", str(job_dir)]), \
         patch("subprocess.check_call") as mock_call1:
        resume_job_main()

    assert mock_call1.called
    prog = read_progress(job_dir)
    assert prog["resume_count"] == 0
    assert prog["total_resumes"] == 0
    assert prog["status"] == "running"

    # Simulate real failure / stall recovery
    update_progress(job_dir, status="failed", guardian_status="stalled")
    with patch("sys.argv", ["resume_job.py", "--job-dir", str(job_dir)]), \
         patch("subprocess.check_call") as mock_call2:
        resume_job_main()

    assert mock_call2.called
    prog2 = read_progress(job_dir)
    assert prog2["resume_count"] == 1
    assert prog2["total_resumes"] == 1
    assert prog2["status"] == "resuming"


def test_watch_job_first_run_stalled_in_preflight_recovers_and_enforces_limit(tmp_path):
    """Verify that a job stalling during its first run in preflight increments resume_count and respects retry limit."""
    from scripts.watch_job import main as watch_job_main
    from scripts.resume_job import main as resume_job_main

    job_dir = tmp_path / "preflight_stall_job"
    ensure_job_layout(job_dir)
    (job_dir / "job_config.json").write_text(json.dumps({"target_language": "Chinese"}), encoding="utf-8")
    (job_dir / "job_pid.txt").write_text("99999999", encoding="utf-8")  # Dead PID
    status_file = job_dir / "pipeline_status.json"
    status_file.write_text(json.dumps({"status": "running", "stage": "preflight"}), encoding="utf-8")

    # First run reaches running status in preflight with 0 resumes
    update_progress(job_dir, status="running", stage="preflight", resume_count=0, total_resumes=0)

    # Calling resume_job without --initial on this running/preflight job MUST increment resume_count to 1!
    with patch("sys.argv", ["resume_job.py", "--job-dir", str(job_dir)]), \
         patch("subprocess.check_call"):
        resume_job_main()

    p1 = read_progress(job_dir)
    assert p1["resume_count"] == 1, "Resume on running preflight job must count as retry 1!"
    assert p1["total_resumes"] == 1
    assert p1["status"] == "resuming"

    # Now verify watch_job guardian loop respects retry limit
    def mock_resume(cmd, **_kwargs):
        prog = read_progress(job_dir)
        update_progress(job_dir, status="running", stage="preflight", resume_count=int(prog.get("resume_count", 0)) + 1)

    past = time.time() - 10000
    os.utime(status_file, (past, past))

    # Reset to resume_count=2, so next stall hits limit (3)
    update_progress(job_dir, status="running", stage="preflight", resume_count=2, stale_count=2)

    with patch("subprocess.check_call", side_effect=mock_resume):
        # Next stall triggers 3rd resume
        with patch("sys.argv", ["watch_job.py", "--job-dir", str(job_dir), "--once", "--stale-sec", "300", "--max-resumes", "3"]):
            watch_job_main()

    p_after = read_progress(job_dir)
    assert p_after["resume_count"] == 3

    # Stalling at limit (resume_count >= max_resumes) marks structurally_stuck
    os.utime(status_file, (past, past))
    with patch("sys.argv", ["watch_job.py", "--job-dir", str(job_dir), "--once", "--stale-sec", "300", "--max-resumes", "3"]):
        watch_job_main()

    p_final = read_progress(job_dir)
    assert p_final["status"] == "structurally_stuck"
    assert p_final["guardian_status"] == "structurally_stuck"


# ==============================================================================
# 12. Stage Progress Resets Consecutive Failure Counts (Fixing P1 bug)
# ==============================================================================
def test_update_status_resets_consecutive_retries_on_stage_progress(tmp_path):
    """Verify that run_pipeline.py update_status resets resume_count/stale_count only when advancing to a new stage."""
    from scripts.run_pipeline import update_status

    job_dir = tmp_path / "stage_job"
    ensure_job_layout(job_dir)
    status_file = job_dir / "pipeline_status.json"

    # 1. Initial progression up to ASR
    update_status(status_file, "running", "preflight", stage="preflight")
    p1 = read_progress(job_dir)
    assert p1["max_stage"] == "preflight"
    assert p1["resume_count"] == 0

    update_status(status_file, "running", "downloading", stage="download")
    p2 = read_progress(job_dir)
    assert p2["max_stage"] == "download"
    assert p2["resume_count"] == 0

    update_status(status_file, "running", "transcribing", stage="asr")
    p3 = read_progress(job_dir)
    assert p3["max_stage"] == "asr"
    assert p3["resume_count"] == 0

    # 2. Simulate failure at ASR and two retries recorded by watchdog
    update_progress(job_dir, status="resuming", resume_count=2, total_resumes=2, stale_count=2)
    p4 = read_progress(job_dir)
    assert p4["resume_count"] == 2
    assert p4["max_stage"] == "asr"

    # 3. Pipeline retries: re-executes earlier stages (preflight, download, asr)
    # Stage ranks: preflight (10) < max_stage asr (40). MUST NOT reset failure count!
    update_status(status_file, "running", "preflight", stage="preflight")
    p5 = read_progress(job_dir)
    assert p5["resume_count"] == 2
    assert p5["stale_count"] == 2
    assert p5["max_stage"] == "asr"

    update_status(status_file, "running", "downloading", stage="download")
    p6 = read_progress(job_dir)
    assert p6["resume_count"] == 2
    assert p6["stale_count"] == 2
    assert p6["max_stage"] == "asr"

    update_status(status_file, "running", "transcribing", stage="asr")
    p7 = read_progress(job_dir)
    assert p7["resume_count"] == 2
    assert p7["stale_count"] == 2
    assert p7["max_stage"] == "asr"

    # 4. ASR succeeds and advances to translation stage (rank 60 > 40)!
    # MUST reset consecutive failures, while preserving lifetime total_resumes!
    update_status(status_file, "running", "translating", stage="translation")
    p8 = read_progress(job_dir)
    assert p8["resume_count"] == 0
    assert p8["stale_count"] == 0
    assert p8["max_stage"] == "translation"
    assert p8["total_resumes"] == 2
    assert p8["guardian_status"] == "healthy"

    # 5. Simulate another failure during translation (1 resume)
    update_progress(job_dir, status="resuming", resume_count=1, total_resumes=3, stale_count=1)
    # Advances to synthesis (rank 90 > 60)
    update_status(status_file, "running", "synthesizing", stage="synthesis")
    p9 = read_progress(job_dir)
    assert p9["resume_count"] == 0
    assert p9["stale_count"] == 0
    assert p9["max_stage"] == "synthesis"
    assert p9["total_resumes"] == 3

    # 6. Finally, job completes
    update_status(status_file, "completed", "Done")
    p10 = read_progress(job_dir)
    assert p10["resume_count"] == 0
    assert p10["stale_count"] == 0
    assert p10["max_stage"] == "completed"


def test_multistage_job_survives_when_progress_advances_across_stages(tmp_path):
    """Verify that a job with 3 total lifetime resumes is NOT killed if it made forward stage progress between failures."""
    from scripts.run_pipeline import update_status

    job_dir = tmp_path / "multistage_job"
    ensure_job_layout(job_dir)
    (job_dir / "job_config.json").write_text(json.dumps({"target_language": "Chinese"}), encoding="utf-8")
    (job_dir / "job_pid.txt").write_text("99999999", encoding="utf-8")
    status_file = job_dir / "pipeline_status.json"
    status_file.write_text(json.dumps({"status": "running"}), encoding="utf-8")

    def make_stalled():
        past = time.time() - 10000
        os.utime(status_file, (past, past))

    # Phase 1: Advance to ASR, then stall
    update_status(status_file, "running", "asr", stage="asr")
    make_stalled()

    with patch("subprocess.check_call"):
        r1 = sweep_once(tmp_path, max_parallel=1, stale_sec=300, max_resumes=3)
        assert r1["resumed"] == 1
        assert r1["structurally_stuck"] == 0

    # Phase 2: Resume runs, advances to translation (new stage progress!), then stalls
    # Stage advance resets consecutive resume_count to 0
    update_status(status_file, "running", "translating", stage="translation")
    p = read_progress(job_dir)
    assert p["resume_count"] == 0
    make_stalled()

    with patch("subprocess.check_call"):
        r2 = sweep_once(tmp_path, max_parallel=1, stale_sec=300, max_resumes=3)
        assert r2["resumed"] == 1
        assert r2["structurally_stuck"] == 0

    # Phase 3: Resume runs, advances to TTS (new stage progress!), then stalls
    update_status(status_file, "running", "tts", stage="tts")
    p = read_progress(job_dir)
    assert p["resume_count"] == 0
    make_stalled()

    with patch("subprocess.check_call"):
        r3 = sweep_once(tmp_path, max_parallel=1, stale_sec=300, max_resumes=3)
        # Even though 3 resumes have occurred overall across different stages,
        # consecutive retries without progress is only 1! Job MUST resume and NOT be structurally stuck!
        assert r3["resumed"] == 1
        assert r3["structurally_stuck"] == 0


# ==============================================================================
# 13. Cross-Batch Coordination and Shared Concurrency Limits (Fixing P1 bug)
# ==============================================================================
def test_cross_batch_sweep_shares_coordination_and_enforces_concurrency(tmp_path):
    """Verify that sweeping root 'output' and sub-batch 'output/batch_b' share coordination scope and respect max_parallel."""
    output_dir = tmp_path / "output"
    batch_a = output_dir / "batch_a"
    batch_b = output_dir / "batch_b"
    args = DummyArgs()

    create_batch_jobs(["a1.mp4", "a2.mp4"], batch_a, args)
    create_batch_jobs(["b1.mp4", "b2.mp4"], batch_b, args)

    launched = []
    def mock_subprocess(cmd, **_kwargs):
        launched.append(cmd)
        for job_path in [batch_a / "job_001", batch_a / "job_002", batch_b / "job_001", batch_b / "job_002"]:
            if str(job_path) in cmd:
                update_progress(job_path, status="running")
                (job_path / "job_pid.txt").write_text(str(os.getpid()), encoding="utf-8")

    # Step 1: Sweep from root 'output' with max_parallel=1 -> Dispatches 1 job in batch_a
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep1 = sweep_once(output_dir, max_parallel=1)

    assert rep1["dispatched"] == 1
    assert rep1["active_running_total"] == 1
    assert len(launched) == 1
    # batch_a/job_001 is running
    assert read_progress(batch_a / "job_001")["status"] == "running"
    # batch_b/job_001 is still pending
    assert read_progress(batch_b / "job_001")["status"] == "pending"

    # Step 2: Now run sweep specifically targeting 'output/batch_b' with max_parallel=1!
    # Because batch_a/job_001 is currently running, available_slots across the shared output scope must be 0!
    # Therefore, batch_b/job_001 MUST NOT be dispatched!
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep2 = sweep_once(batch_b, max_parallel=1)

    assert rep2["dispatched"] == 0, "Must not dispatch when another batch in the coordination root occupies the concurrency slot!"
    assert rep2["active_running_total"] == 1
    assert len(launched) == 1  # No second job was launched!
    assert read_progress(batch_b / "job_001")["status"] == "pending"

    # Step 3: Simulate batch_a/job_001 completing
    update_progress(batch_a / "job_001", status="completed")
    (batch_a / "job_001" / "pipeline_status.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")

    # Step 4: Sweep 'output/batch_b' again -> Concurrency slot is now available, batch_b/job_001 is dispatched!
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep3 = sweep_once(batch_b, max_parallel=1)

    assert rep3["dispatched"] == 1
    assert rep3["active_running_total"] == 1
    assert len(launched) == 2
    assert read_progress(batch_b / "job_001")["status"] == "running"


def test_cross_batch_sweep_lock_mutual_exclusion(tmp_path):
    """Verify that root output and sub-batches share the same .sweep.lock."""
    output_dir = tmp_path / "output"
    batch_a = output_dir / "batch_a"
    batch_b = output_dir / "batch_b"
    output_dir.mkdir(parents=True)
    batch_a.mkdir(parents=True)
    batch_b.mkdir(parents=True)

    coord_root = resolve_coordination_root(batch_b)
    assert coord_root == output_dir.resolve()

    # Lock held on root output
    with SweepLock(output_dir / ".sweep.lock") as lock_root:
        assert lock_root is not None
        # Attempting to sweep batch_b while root is locked must return status="locked"
        rep = sweep_once(batch_b, max_parallel=1)
        assert rep["status"] == "locked"


# ==============================================================================
# 14. Custom Named Batches & Persistent Coordination Root (Addressing Reviewer Bug)
# ==============================================================================
def test_custom_named_batches_under_same_parent_share_coordination_and_concurrency(tmp_path):
    """Verify reviewer scenario: custom batch names 'client_alpha' and 'client_beta' share coordination scope and strictly respect max_parallel."""
    parent = tmp_path / "clients"
    client_alpha = parent / "client_alpha"
    client_beta = parent / "client_beta"
    args = DummyArgs()

    create_batch_jobs(["alpha1.mp4", "alpha2.mp4"], client_alpha, args)
    create_batch_jobs(["beta1.mp4", "beta2.mp4"], client_beta, args)

    # Verify both resolved to the shared parent 'clients'
    assert resolve_coordination_root(client_alpha) == parent.resolve()
    assert resolve_coordination_root(client_beta) == parent.resolve()

    launched = []
    def mock_subprocess(cmd, **_kwargs):
        launched.append(cmd)
        for job_path in [client_alpha / "job_001", client_alpha / "job_002", client_beta / "job_001", client_beta / "job_002"]:
            if str(job_path) in cmd:
                update_progress(job_path, status="running")
                (job_path / "job_pid.txt").write_text(str(os.getpid()), encoding="utf-8")

    # Step 1: Sweep client_alpha with max_parallel=1 -> Dispatches 1 job
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep1 = sweep_once(client_alpha, max_parallel=1)

    assert rep1["dispatched"] == 1
    assert rep1["active_running_total"] == 1
    assert rep1["is_global_scope"] is True
    assert rep1["coordination_mode"] == "shared_root"
    assert len(launched) == 1
    assert read_progress(client_alpha / "job_001")["status"] == "running"

    # Step 2: Now sweep client_beta with max_parallel=1 -> MUST NOT launch second job!
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep2 = sweep_once(client_beta, max_parallel=1)

    assert rep2["dispatched"] == 0, "client_beta must NOT dispatch when client_alpha is occupying the slot!"
    assert rep2["active_running_total"] == 1
    assert len(launched) == 1  # Total running is still 1!
    assert read_progress(client_beta / "job_001")["status"] == "pending"

    # Step 3: Complete client_alpha/job_001
    update_progress(client_alpha / "job_001", status="completed")
    (client_alpha / "job_001" / "pipeline_status.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")

    # Step 4: Sweep client_beta again -> Now dispatches!
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep3 = sweep_once(client_beta, max_parallel=1)

    assert rep3["dispatched"] == 1
    assert rep3["active_running_total"] == 1
    assert len(launched) == 2
    assert read_progress(client_beta / "job_001")["status"] == "running"


def test_persistent_coordination_metadata_and_batch_meta_inspection(tmp_path):
    """Verify that batch creation writes .coordination_root, batch_meta.json, and job_config coordination_dir."""
    clients_dir = tmp_path / "custom_work"
    batch_dir = clients_dir / "project_x"
    args = DummyArgs()

    create_batch_jobs(["x1.mp4"], batch_dir, args)

    # 1. Check batch_meta.json
    meta_path = batch_dir / "batch_meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["coordination_dir"] == str(clients_dir.resolve())

    # 2. Check .coordination_root marker
    marker_path = batch_dir / ".coordination_root"
    assert marker_path.exists()
    assert marker_path.read_text(encoding="utf-8").strip() == str(clients_dir.resolve())

    # 3. Check job_config.json
    cfg_path = batch_dir / "job_001" / "job_config.json"
    assert cfg_path.exists()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["coordination_dir"] == str(clients_dir.resolve())


def test_uncoordinated_custom_batch_without_config_refuses_global_scope(tmp_path):
    """Verify that an isolated directory without coordination config does not claim global scope."""
    isolated_batch = tmp_path / "isolated_batch"
    job_dir = isolated_batch / "job_001"
    ensure_job_layout(job_dir)
    (job_dir / "job_config.json").write_text(json.dumps({"target_language": "Chinese"}), encoding="utf-8")
    (job_dir / "pipeline_status.json").write_text(json.dumps({"status": "pending"}), encoding="utf-8")

    # Sweep without --coordination-dir -> unconfigured local scope, refuses to claim global limit
    rep = sweep_once(isolated_batch, max_parallel=1, quiet=True)
    assert rep["is_global_scope"] is False
    assert rep["coordination_mode"] == "unconfigured_local"

    # Sweep with explicit --coordination-dir -> properly claims explicit global scope
    custom_coord = tmp_path / "global_coord"
    rep2 = sweep_once(isolated_batch, max_parallel=1, coordination_dir=custom_coord, quiet=True)
    assert rep2["is_global_scope"] is True
    assert rep2["coordination_mode"] == "explicit_cli"
    assert rep2["coordination_root"] == str(custom_coord.resolve())


# ==============================================================================
# 15. Independent Coordination Directory & Disjoint Batch Registry (Fixing Reviewer P1)
# ==============================================================================
def test_independent_coordination_dir_shares_concurrency_across_disjoint_batches(tmp_path):
    """Verify that an independent lock/coordination directory maintains a registry and enforces max_parallel across disjoint batches."""
    coord_dir = tmp_path / "independent_coord_dir"
    batch_1 = tmp_path / "project_a" / "batch_1"
    batch_2 = tmp_path / "project_b" / "batch_2"
    args = DummyArgs()

    create_batch_jobs(["a1.mp4", "a2.mp4"], batch_1, args)
    create_batch_jobs(["b1.mp4", "b2.mp4"], batch_2, args)

    # Ensure neither batch is in coord_dir
    assert not str(batch_1.resolve()).startswith(str(coord_dir.resolve()))
    assert not str(batch_2.resolve()).startswith(str(coord_dir.resolve()))

    launched = []
    def mock_subprocess(cmd, **_kwargs):
        launched.append(cmd)
        for job_path in [batch_1 / "job_001", batch_1 / "job_002", batch_2 / "job_001", batch_2 / "job_002"]:
            if str(job_path) in cmd:
                update_progress(job_path, status="running")
                (job_path / "job_pid.txt").write_text(str(os.getpid()), encoding="utf-8")

    # Step 1: Sweep batch_1 with independent coord_dir -> Dispatches 1 job
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep1 = sweep_once(batch_1, coordination_dir=coord_dir, max_parallel=1)

    assert rep1["dispatched"] == 1
    assert rep1["active_running_total"] == 1
    assert rep1["registered_batches_count"] == 1
    assert len(launched) == 1
    assert read_progress(batch_1 / "job_001")["status"] == "running"

    # Step 2: Sweep batch_2 with the SAME independent coord_dir -> MUST NOT launch second job!
    # Because batch_1 is registered in coord_dir and currently running, available slots must be 0!
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep2 = sweep_once(batch_2, coordination_dir=coord_dir, max_parallel=1)

    assert rep2["dispatched"] == 0, "batch_2 must NOT dispatch when batch_1 is occupying the concurrency slot via independent coord_dir!"
    assert rep2["active_running_total"] == 1
    assert rep2["registered_batches_count"] == 2
    assert len(launched) == 1  # Total running across disjoint directories is still strictly 1!
    assert read_progress(batch_2 / "job_001")["status"] == "pending"

    # Step 3: Complete batch_1/job_001
    update_progress(batch_1 / "job_001", status="completed")
    (batch_1 / "job_001" / "pipeline_status.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")

    # Step 4: Sweep batch_2 again -> Concurrency slot is now freed, batch_2/job_001 is dispatched!
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep3 = sweep_once(batch_2, coordination_dir=coord_dir, max_parallel=1)

    assert rep3["dispatched"] == 1
    assert rep3["active_running_total"] == 1
    assert len(launched) == 2
    assert read_progress(batch_2 / "job_001")["status"] == "running"

    # Step 5: Verify coordination directory maintains registered_batches.json
    reg_file = coord_dir / "registered_batches.json"
    assert reg_file.exists()
    reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
    assert str(batch_1.resolve()) in reg_data["batches"]
    assert str(batch_2.resolve()) in reg_data["batches"]


def test_coordination_dir_rejects_file_target(tmp_path):
    """Verify that specifying an existing file as --coordination-dir raises ValueError."""
    dummy_file = tmp_path / "coord_file.txt"
    dummy_file.write_text("not a directory", encoding="utf-8")
    batch_dir = tmp_path / "test_batch"
    batch_dir.mkdir(parents=True)

    import pytest
    with pytest.raises(ValueError, match="is a file, must be a directory"):
        sweep_once(batch_dir, coordination_dir=dummy_file)


def test_concurrent_register_batch_path_calls_no_data_loss(tmp_path):
    """Verify that multiple concurrent calls registering batches to the same coordination dir don't lose any registrations."""
    from concurrent.futures import ThreadPoolExecutor
    from core.job_state import get_registered_batches, register_batch_path

    coord_dir = tmp_path / "shared_coordination"
    num_batches = 12
    batch_dirs = []
    for i in range(num_batches):
        b = tmp_path / f"batch_{i:02d}"
        b.mkdir(parents=True)
        batch_dirs.append(b)

    def do_register(b):
        time.sleep(0.005)
        register_batch_path(coord_dir, b)

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(do_register, batch_dirs))

    registered = get_registered_batches(coord_dir)
    assert len(registered) == num_batches
    registered_strs = {str(p.resolve()) for p in registered}
    for b in batch_dirs:
        assert str(b.resolve()) in registered_strs


def test_concurrent_batch_submissions_atomic_registration(tmp_path):
    """Verify that multiple concurrent batch submissions with the same coordination-dir correctly register all batches and serialize safely."""
    from concurrent.futures import ThreadPoolExecutor

    coord_dir = tmp_path / "shared_coord"
    coord_dir.mkdir(parents=True)
    num_batches = 4
    batches = [tmp_path / f"client_batch_{i}" for i in range(num_batches)]

    def submit_one(b_dir):
        args = DummyArgs(coordination_dir=str(coord_dir))
        return create_batch_jobs(["item1.mp4", "item2.mp4"], b_dir, args)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(submit_one, batches))

    # All submissions should succeed and create 2 jobs each
    assert len(results) == num_batches
    for r in results:
        assert len(r) == 2

    # Verify all batches are registered in registered_batches.json
    reg_file = coord_dir / "registered_batches.json"
    assert reg_file.exists()
    reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
    assert len(reg_data["batches"]) == num_batches
    for b in batches:
        assert str(b.resolve()) in reg_data["batches"]

    # Verify that sweeping respects global concurrency limit across these concurrent batches
    launched = []
    def mock_subprocess(cmd, **_kwargs):
        launched.append(cmd)
        for b in batches:
            for job in [b / "job_001", b / "job_002"]:
                if str(job) in cmd:
                    update_progress(job, status="running")
                    (job / "job_pid.txt").write_text(str(os.getpid()), encoding="utf-8")

    # Sweep batch 0 -> dispatches 1 job
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep1 = sweep_once(batches[0], coordination_dir=coord_dir, max_parallel=1)
    assert rep1["dispatched"] == 1
    assert rep1["active_running_total"] == 1
    assert len(launched) == 1

    # Sweep batch 1 -> should NOT dispatch because max_parallel=1 is already occupied by batch 0
    with patch("subprocess.check_call", side_effect=mock_subprocess):
        rep2 = sweep_once(batches[1], coordination_dir=coord_dir, max_parallel=1)
    assert rep2["dispatched"] == 0
    assert rep2["active_running_total"] == 1
    assert len(launched) == 1


def test_registration_failure_halts_sweep_and_submission(tmp_path):
    """Verify that when registry write fails, sweep and batch submit halt immediately."""
    from scripts.batch_submit import create_batch_jobs
    from scripts.cron_sweep_jobs import sweep_once

    coord_dir = tmp_path / "failing_coord"
    coord_dir.mkdir(parents=True)
    batch_dir = tmp_path / "batch_fail"

    args = DummyArgs(coordination_dir=str(coord_dir))

    # Mock atomic_write_json to fail on registered_batches.json
    orig_write = __import__("core.job_runtime", fromlist=["atomic_write_json"]).atomic_write_json
    def fail_write(path, payload):
        if "registered_batches.json" in str(path):
            raise OSError("Disk full / permission denied")
        return orig_write(path, payload)

    with patch("core.job_state.atomic_write_json", side_effect=fail_write):
        # 1. create_batch_jobs must raise and halt
        with pytest.raises(OSError, match="Disk full"):
            create_batch_jobs(["item1.mp4"], batch_dir, args)

        # 2. sweep_once must return error and not dispatch jobs
        rep = sweep_once(batch_dir, coordination_dir=coord_dir, max_parallel=1)
        assert rep["status"] == "error"
        assert "Disk full" in rep["error"]


def test_watch_job_respects_global_concurrency_quota(tmp_path):
    """Verify that watch_job does not resume a stalled job if the shared concurrency limit is reached."""
    from scripts.watch_job import main as watch_job_main

    coord_dir = tmp_path / "shared_coord"
    coord_dir.mkdir(parents=True)

    job_running = tmp_path / "job_running"
    ensure_job_layout(job_running)
    (job_running / "job_config.json").write_text(json.dumps({"target_language": "Chinese", "coordination_dir": str(coord_dir)}), encoding="utf-8")
    (job_running / "job_pid.txt").write_text(str(os.getpid()), encoding="utf-8")
    update_progress(job_running, status="running")

    job_stalled = tmp_path / "job_stalled"
    ensure_job_layout(job_stalled)
    (job_stalled / "job_config.json").write_text(json.dumps({"target_language": "Chinese", "coordination_dir": str(coord_dir)}), encoding="utf-8")
    (job_stalled / "job_pid.txt").write_text("99999999", encoding="utf-8")
    status_file = job_stalled / "pipeline_status.json"
    status_file.write_text(json.dumps({"status": "running", "stage": "translation"}), encoding="utf-8")
    past = time.time() - 10000
    os.utime(status_file, (past, past))
    update_progress(job_stalled, status="running", stage="translation")

    resumed_jobs = []
    def mock_resume(cmd, **_kwargs):
        resumed_jobs.append(cmd)

    # Step 1: Run watch_job on job_stalled with max_parallel=1
    # job_running occupies the slot -> watch_job MUST NOT resume!
    with patch("sys.argv", ["watch_job.py", "--job-dir", str(job_stalled), "--once", "--max-parallel", "1", "--stale-sec", "300"]), \
         patch("subprocess.check_call", side_effect=mock_resume):
        watch_job_main()

    assert len(resumed_jobs) == 0, "watch_job must NOT resume when concurrency quota is saturated!"
    prog_stalled = read_progress(job_stalled)
    assert prog_stalled["guardian_status"] == "concurrency_queued"
    assert prog_stalled["resume_count"] == 0

    # Step 2: Simulate job_running completing -> concurrency slot freed
    update_progress(job_running, status="completed")
    (job_running / "pipeline_status.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")

    # Step 3: Run watch_job on job_stalled again -> Slot available, job_stalled is resumed!
    with patch("sys.argv", ["watch_job.py", "--job-dir", str(job_stalled), "--once", "--max-parallel", "1", "--stale-sec", "300"]), \
         patch("subprocess.check_call", side_effect=mock_resume):
        watch_job_main()

    assert len(resumed_jobs) == 1, "watch_job must resume once concurrency slot is free!"


def test_batch_submit_resolves_relative_paths_to_absolute(tmp_path):
    """Verify that relative terms_file, profile, env_file, source_srt are resolved to absolute paths at submit time."""
    from scripts.batch_submit import create_batch_jobs

    batch_dir = tmp_path / "batch_rel_paths"
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("API\t接口\n", encoding="utf-8")
    profile_file = tmp_path / "profile.yaml"
    profile_file.write_text("general: {}\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=BAR\n", encoding="utf-8")
    srt_file = tmp_path / "test.srt"
    srt_file.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

    cwd = Path.cwd()
    rel_terms = os.path.relpath(terms_file, cwd)
    rel_profile = os.path.relpath(profile_file, cwd)
    rel_env = os.path.relpath(env_file, cwd)
    rel_srt = os.path.relpath(srt_file, cwd)

    args = DummyArgs(
        terms_file=rel_terms,
        profile=rel_profile,
        env_file=rel_env,
        source_srt=rel_srt,
    )

    create_batch_jobs(["input.mp4"], batch_dir, args)

    cfg = json.loads((batch_dir / "job_001" / "job_config.json").read_text(encoding="utf-8"))
    assert os.path.isabs(cfg["terms_file"])
    assert Path(cfg["terms_file"]).resolve() == terms_file.resolve()
    assert os.path.isabs(cfg["profile"])
    assert Path(cfg["profile"]).resolve() == profile_file.resolve()
    assert os.path.isabs(cfg["env_file"])
    assert Path(cfg["env_file"]).resolve() == env_file.resolve()
    assert os.path.isabs(cfg["source_srt"])
    assert Path(cfg["source_srt"]).resolve() == srt_file.resolve()


def test_concurrent_state_writers_safe_and_no_filenotfound(tmp_path):
    """Verify that multiple concurrent threads writing to atomic_write_text and update_progress do not encounter FileNotFoundError or corruptions."""
    from concurrent.futures import ThreadPoolExecutor
    from core.job_runtime import atomic_write_text
    from core.job_state import update_progress, read_progress

    # 1. Concurrent atomic_write_text to the exact same file
    target_file = tmp_path / "shared_state.txt"
    def write_worker(idx):
        for i in range(15):
            atomic_write_text(target_file, f"writer {idx} iteration {i}\n")

    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(write_worker, range(10)))

    assert target_file.exists()

    # 2. Concurrent update_progress to the exact same job directory
    job_dir = tmp_path / "concurrent_job"
    ensure_job_layout(job_dir)
    def progress_worker(idx):
        for i in range(10):
            update_progress(job_dir, iteration=i, last_worker=idx)

    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(progress_worker, range(10)))

    final_prog = read_progress(job_dir)
    assert "iteration" in final_prog
    assert "last_worker" in final_prog


def test_emergency_sweep_does_not_mask_stale_l1_heartbeat(tmp_path):
    """Verify that emergency sweep creates heartbeat_emergency.json and does NOT refresh scheduled L1 heartbeat."""
    from scripts.cron_sweep_jobs import sweep_once

    coord_dir = tmp_path / "coord"
    coord_dir.mkdir(parents=True)
    l1_heartbeat = coord_dir / ".l1_heartbeat"
    past = time.time() - 10000
    l1_heartbeat.write_text(json.dumps({"timestamp": past, "mode": "scheduled"}), encoding="utf-8")
    os.utime(l1_heartbeat, (past, past))

    # Run emergency sweep
    rep = sweep_once(coord_dir, emergency=True, heartbeat_file=l1_heartbeat)
    assert rep["status"] == "ok"

    # L1 scheduled heartbeat must remain untouched!
    l1_mtime = l1_heartbeat.stat().st_mtime
    assert abs(l1_mtime - past) < 2, "Emergency sweep must NOT modify scheduled L1 heartbeat!"

    # Emergency heartbeat must be created
    emergency_file = coord_dir / "heartbeat_emergency.json"
    assert emergency_file.exists()
    em_data = json.loads(emergency_file.read_text(encoding="utf-8"))
    assert em_data["mode"] == "emergency"


def test_write_job_config_preserves_translation_batch_size_and_coordination_dir(tmp_path):
    """Verify write_job_config preserves custom translation_batch_size and coordination_dir across stage re-writes."""
    from core.source_loader import write_job_config

    job_dir = tmp_path / "config_preserve_job"
    job_dir.mkdir(parents=True)

    class CustomArgs:
        url = "http://example.com/test.mp4"
        input_video = None
        source_srt = None
        source_lang = "en"
        target_language = "Chinese"
        subtitle_mode = "target"
        translation_model = "deepseek"
        translation_batch_size = 64
        coordination_dir = str(tmp_path / "custom_coord")
        model_config = None
        tts_engine = "qwen3-tts"
        ref_audio = None
        no_segments = False
        max_atempo = 1.3
        max_clip_ms = 4000
        max_overhang_ms = 1000
        download_format = "best"
        cookies_from_browser = None
        sub_langs = "en"
        ignore_yt_dlp_config = False
        allow_playlist = False
        playlist_items = None
        proxy = None
        concurrent_fragments = 1
        external_downloader = None
        list_formats = False
        preserve_gap_audio = False
        gap_audio_gain_db = 0.0
        gap_pad_ms = 50

    # First write
    cfg_file = write_job_config(job_dir, CustomArgs())
    cfg1 = json.loads(Path(cfg_file).read_text(encoding="utf-8"))
    assert cfg1["translation_batch_size"] == 64
    assert cfg1["coordination_dir"] == str(tmp_path / "custom_coord")

    # Second write simulating preflight / pipeline step where translation_batch_size was not passed
    class MinimalArgs:
        url = "http://example.com/test.mp4"
        input_video = None
        source_srt = None
        source_lang = "en"
        target_language = "Chinese"
        subtitle_mode = "target"
        translation_model = "deepseek"
        model_config = None
        tts_engine = "qwen3-tts"
        ref_audio = None
        no_segments = False
        max_atempo = 1.3
        max_clip_ms = 4000
        max_overhang_ms = 1000
        download_format = "best"
        cookies_from_browser = None
        sub_langs = "en"
        ignore_yt_dlp_config = False
        allow_playlist = False
        playlist_items = None
        proxy = None
        concurrent_fragments = 1
        external_downloader = None
        list_formats = False
        preserve_gap_audio = False
        gap_audio_gain_db = 0.0
        gap_pad_ms = 50

    cfg_file2 = write_job_config(job_dir, MinimalArgs())
    cfg2 = json.loads(Path(cfg_file2).read_text(encoding="utf-8"))
    assert cfg2["translation_batch_size"] == 64, "Existing translation_batch_size must be preserved!"
    assert cfg2["coordination_dir"] == str(tmp_path / "custom_coord")


def test_concurrent_watch_job_resumes_respect_mutex_and_max_parallel(tmp_path):
    """Verify that when two watchdogs run concurrently on two stalled jobs sharing max_parallel=1, only ONE resumes."""
    from concurrent.futures import ThreadPoolExecutor
    from scripts.watch_job import main as watch_job_main

    coord_dir = tmp_path / "shared_watch_coord"
    coord_dir.mkdir(parents=True)

    job1 = tmp_path / "stalled_job_1"
    ensure_job_layout(job1)
    (job1 / "job_config.json").write_text(json.dumps({"target_language": "Chinese", "coordination_dir": str(coord_dir)}), encoding="utf-8")
    (job1 / "job_pid.txt").write_text("99999991", encoding="utf-8")
    status_file1 = job1 / "pipeline_status.json"
    status_file1.write_text(json.dumps({"status": "running", "stage": "translation"}), encoding="utf-8")
    past = time.time() - 10000
    os.utime(status_file1, (past, past))
    update_progress(job1, status="running", stage="translation")

    job2 = tmp_path / "stalled_job_2"
    ensure_job_layout(job2)
    (job2 / "job_config.json").write_text(json.dumps({"target_language": "Chinese", "coordination_dir": str(coord_dir)}), encoding="utf-8")
    (job2 / "job_pid.txt").write_text("99999992", encoding="utf-8")
    status_file2 = job2 / "pipeline_status.json"
    status_file2.write_text(json.dumps({"status": "running", "stage": "translation"}), encoding="utf-8")
    os.utime(status_file2, (past, past))
    update_progress(job2, status="running", stage="translation")

    resumed_jobs = []
    def mock_resume(cmd, **_kwargs):
        # Simulate execution delay during resume launch
        time.sleep(0.05)
        resumed_jobs.append(cmd)

    def run_watch(target_job):
        watch_job_main(["--job-dir", str(target_job), "--once", "--max-parallel", "1", "--stale-sec", "300"])

    with patch("subprocess.check_call", side_effect=mock_resume):
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(run_watch, [job1, job2]))

    # Exactly ONE job must have resumed, never both!
    assert len(resumed_jobs) == 1, f"Expected exactly 1 resume due to max_parallel=1, but got {len(resumed_jobs)}"


def test_registry_locking_failure_aborts_and_does_not_modify_registry(tmp_path):
    """Verify that if flock on .registry.lock fails, register_batch_path raises RuntimeError and doesn't write."""
    import fcntl
    from core.job_state import register_batch_path

    coord_dir = tmp_path / "coord_lock_fail"
    coord_dir.mkdir(parents=True)
    registry_file = coord_dir / "registered_batches.json"
    registry_file.write_text(json.dumps({"batches": {}}), encoding="utf-8")

    batch_dir = tmp_path / "test_batch"
    batch_dir.mkdir(parents=True)

    orig_flock = fcntl.flock
    def fail_flock(fd, op):
        if op & fcntl.LOCK_EX:
            raise OSError("Locking failed: Resource temporarily unavailable")
        return orig_flock(fd, op)

    with patch("fcntl.flock", side_effect=fail_flock):
        with pytest.raises(RuntimeError, match="Failed to acquire registry lock"):
            register_batch_path(coord_dir, batch_dir)

    # registered_batches.json must NOT have been modified
    data = json.loads(registry_file.read_text(encoding="utf-8"))
    assert str(batch_dir.resolve()) not in data["batches"]


def test_corrupt_registry_file_halts_sweep_and_watchdog_without_wiping(tmp_path):
    """Verify that corrupt registered_batches.json raises RuntimeError, preserves file, and halts sweep & watch_job."""
    from core.job_state import get_registered_batches, register_batch_path
    from scripts.cron_sweep_jobs import sweep_once
    from scripts.watch_job import main as watch_job_main

    coord_dir = tmp_path / "corrupt_coord"
    coord_dir.mkdir(parents=True)
    registry_file = coord_dir / "registered_batches.json"
    corrupt_content = '{"batches": {INVALID_JSON'
    registry_file.write_text(corrupt_content, encoding="utf-8")

    batch_dir = tmp_path / "batch_1"
    batch_dir.mkdir(parents=True)
    job = batch_dir / "job_001"
    ensure_job_layout(job)
    (job / "job_config.json").write_text(json.dumps({"target_language": "Chinese", "coordination_dir": str(coord_dir)}), encoding="utf-8")
    status_file = job / "pipeline_status.json"
    status_file.write_text(json.dumps({"status": "running", "stage": "translation"}), encoding="utf-8")
    past = time.time() - 3600
    os.utime(status_file, (past, past))
    update_progress(job, status="running", stage="translation")

    # 1. get_registered_batches must raise RuntimeError
    with pytest.raises(RuntimeError, match="Failed to read coordination registry"):
        get_registered_batches(coord_dir)

    # 2. register_batch_path must raise RuntimeError and preserve the file content
    with pytest.raises(RuntimeError, match="Failed to read coordination registry"):
        register_batch_path(coord_dir, batch_dir)
    assert registry_file.read_text(encoding="utf-8") == corrupt_content

    # 3. sweep_once must return error and not dispatch any jobs
    resumed = []
    with patch("subprocess.Popen", side_effect=lambda *a, **kw: resumed.append(a)):
        rep = sweep_once(batch_dir, coordination_dir=coord_dir, max_parallel=1)
    assert rep["status"] == "error"
    assert "Failed to register batch" in rep["error"] or "Failed to discover active jobs" in rep["error"]
    assert len(resumed) == 0
    assert registry_file.read_text(encoding="utf-8") == corrupt_content

    # 4. watch_job must defer resume, set guardian_status to concurrency_queued, and not resume
    watch_resumed = []
    with patch("subprocess.check_call", side_effect=lambda *a, **kw: watch_resumed.append(a)):
        watch_job_main(["--job-dir", str(job), "--once", "--max-parallel", "1", "--stale-sec", "300", "--coordination-dir", str(coord_dir)])
    assert len(watch_resumed) == 0
    prog = read_progress(job)
    assert prog.get("guardian_status") == "concurrency_queued"
    assert registry_file.read_text(encoding="utf-8") == corrupt_content


def test_corrupt_registry_format_raises_runtime_error_and_halts_sweep(tmp_path):
    """Verify that a registry with invalid data format (e.g. batches is a list/string) raises RuntimeError and halts sweep."""
    from core.job_state import get_registered_batches, register_batch_path
    from scripts.cron_sweep_jobs import sweep_once

    coord_dir = tmp_path / "format_coord"
    coord_dir.mkdir(parents=True)
    registry_file = coord_dir / "registered_batches.json"
    registry_file.write_text(json.dumps({"batches": "not_a_dict"}), encoding="utf-8")

    batch_dir = tmp_path / "batch_1"
    batch_dir.mkdir(parents=True)
    job = batch_dir / "job_001"
    ensure_job_layout(job)
    (job / "job_config.json").write_text(json.dumps({"target_language": "Chinese"}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Corrupt registry format"):
        get_registered_batches(coord_dir)

    with pytest.raises(RuntimeError, match="Corrupt registry format"):
        register_batch_path(coord_dir, batch_dir)

    # If register succeeds (e.g. mocked or already registered), discovering active jobs fails
    with patch("scripts.cron_sweep_jobs.register_batch_path", return_value=[batch_dir]):
        rep = sweep_once(batch_dir, coordination_dir=coord_dir, max_parallel=1)
        assert rep["status"] == "error"
        assert "Failed to discover active jobs" in rep["error"]


def test_watch_job_resume_launch_failure_revokes_slot_and_marks_retryable(tmp_path):
    """Verify that when resume launch fails in watch_job, quota is revoked immediately and job is marked retryable."""
    from scripts.cron_sweep_jobs import count_global_active_jobs, sweep_once
    from scripts.watch_job import main as watch_job_main, run_status

    coord_dir = tmp_path / "coord_fail"
    coord_dir.mkdir(parents=True)

    batch_dir = tmp_path / "batch_fail"
    batch_dir.mkdir(parents=True)
    job = batch_dir / "job_001"
    ensure_job_layout(job)
    (job / "job_config.json").write_text(json.dumps({"target_language": "Chinese", "coordination_dir": str(coord_dir)}), encoding="utf-8")
    status_file = job / "pipeline_status.json"
    status_file.write_text(json.dumps({"status": "running", "stage": "translation"}), encoding="utf-8")
    past = time.time() - 3600
    os.utime(status_file, (past, past))
    update_progress(job, status="running", stage="translation")

    # Mock subprocess.check_call to fail when running resume_job.py
    def mock_fail_call(cmd, **_kwargs):
        if "resume_job.py" in str(cmd):
            raise subprocess.CalledProcessError(1, cmd)
        return 0

    with patch("subprocess.check_call", side_effect=mock_fail_call):
        with pytest.raises(subprocess.CalledProcessError):
            watch_job_main(["--job-dir", str(job), "--once", "--max-parallel", "1", "--stale-sec", "300", "--coordination-dir", str(coord_dir)])

    # 1. State must reflect launch failure, not stuck in resuming
    prog = read_progress(job)
    assert prog.get("status") == "failed"
    assert prog.get("guardian_status") == "launch_failed"

    # 2. Concurrency quota must NOT be occupied!
    global_running, _, _ = count_global_active_jobs(coord_dir, stale_sec=300)
    assert len(global_running) == 0, f"Failed job must NOT occupy concurrency slot, but found {global_running}"

    # 3. Status must immediately identify it as stalled/retryable without waiting for stale_sec
    st = run_status(job, 300)
    assert st.get("stalled") is True

    # 4. Sweep can resume this retryable job
    resumed = []
    with patch("subprocess.check_call", side_effect=lambda cmd, **kw: resumed.append(cmd)):
        rep = sweep_once(batch_dir, coordination_dir=coord_dir, max_parallel=1, stale_sec=300)
        assert rep["status"] == "ok"
        assert len(resumed) == 1


def test_concurrent_guardians_same_job_no_duplicate_resume(tmp_path):
    """Verify that two watchdogs watching the same stalled job do NOT both execute resume."""
    from concurrent.futures import ThreadPoolExecutor
    from scripts.watch_job import main as watch_job_main

    coord_dir = tmp_path / "coord_same_job"
    coord_dir.mkdir(parents=True)

    batch_dir = tmp_path / "batch_same"
    batch_dir.mkdir(parents=True)
    job = batch_dir / "job_001"
    ensure_job_layout(job)
    (job / "job_config.json").write_text(json.dumps({"target_language": "Chinese", "coordination_dir": str(coord_dir)}), encoding="utf-8")
    status_file = job / "pipeline_status.json"
    status_file.write_text(json.dumps({"status": "running", "stage": "translation"}), encoding="utf-8")
    past = time.time() - 3600
    os.utime(status_file, (past, past))
    update_progress(job, status="running", stage="translation")

    resumed_cmds = []
    def mock_resume(cmd, **_kwargs):
        time.sleep(0.05)
        resumed_cmds.append(cmd)

    def run_watch():
        watch_job_main(["--job-dir", str(job), "--once", "--max-parallel", "1", "--stale-sec", "300", "--coordination-dir", str(coord_dir)])

    with patch("subprocess.check_call", side_effect=mock_resume):
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda f: f(), [run_watch, run_watch]))

    # Must be resumed exactly ONCE!
    assert len(resumed_cmds) == 1, f"Expected exactly 1 resume on same job, but got {len(resumed_cmds)}"
    prog = read_progress(job)
    assert int(prog.get("stale_count", 0)) == 1


def test_cron_sweep_resume_launch_failure_revokes_slot_and_unblocks_queue(tmp_path):
    """Verify that when L1 sweep encounters resume launch failure, quota is revoked and other jobs are not blocked."""
    from scripts.cron_sweep_jobs import count_global_active_jobs, sweep_once

    coord_dir = tmp_path / "coord_l1_fail"
    coord_dir.mkdir(parents=True)

    # Batch 1 with stalled job
    batch_1 = tmp_path / "batch_1"
    batch_1.mkdir(parents=True)
    job_stalled = batch_1 / "job_001"
    ensure_job_layout(job_stalled)
    (job_stalled / "job_config.json").write_text(json.dumps({"target_language": "Chinese", "coordination_dir": str(coord_dir)}), encoding="utf-8")
    status_file1 = job_stalled / "pipeline_status.json"
    status_file1.write_text(json.dumps({"status": "running", "stage": "translation"}), encoding="utf-8")
    past = time.time() - 3600
    os.utime(status_file1, (past, past))
    update_progress(job_stalled, status="running", stage="translation")

    # Batch 2 with pending job
    batch_2 = tmp_path / "batch_2"
    batch_2.mkdir(parents=True)
    job_pending = batch_2 / "job_002"
    ensure_job_layout(job_pending)
    (job_pending / "job_config.json").write_text(json.dumps({"target_language": "Chinese", "coordination_dir": str(coord_dir)}), encoding="utf-8")
    status_file2 = job_pending / "pipeline_status.json"
    status_file2.write_text(json.dumps({"status": "pending"}), encoding="utf-8")
    update_progress(job_pending, status="pending")

    # 1. Sweep Batch 1: resume fails
    def mock_fail_resume(cmd, **_kwargs):
        if "resume_job.py" in str(cmd):
            raise subprocess.CalledProcessError(1, cmd)
        return 0

    with patch("subprocess.check_call", side_effect=mock_fail_resume):
        rep1 = sweep_once(batch_1, coordination_dir=coord_dir, max_parallel=1, stale_sec=300)
        assert rep1["status"] == "ok"
        assert rep1["resumed"] == 0
        assert rep1["failed"] == 1

    # Stalled job must be marked launch_failed, not left as resuming
    prog1 = read_progress(job_stalled)
    assert prog1.get("status") == "failed"
    assert prog1.get("guardian_status") == "launch_failed"

    # Concurrency slot must be completely released (0 active)
    global_running, _, _ = count_global_active_jobs(coord_dir, stale_sec=300)
    assert len(global_running) == 0, f"Expected 0 active jobs in coordination directory, but found: {global_running}"

    # 2. Sweep Batch 2 with max_parallel=1: must NOT be blocked by the failed job in Batch 1!
    dispatched = []
    with patch("subprocess.check_call", side_effect=lambda cmd, **kw: dispatched.append(cmd)):
        rep2 = sweep_once(batch_2, coordination_dir=coord_dir, max_parallel=1, stale_sec=300)
        assert rep2["status"] == "ok"
        assert rep2["dispatched"] == 1
        assert len(dispatched) == 1


def test_alive_pid_with_failed_status_counted_in_global_concurrency(tmp_path):
    """Verify that a job with failed/launch_failed status whose PID is alive is counted in concurrency slots."""
    from core.job_state import register_batch_path
    from scripts.cron_sweep_jobs import count_global_active_jobs, sweep_once

    coord_dir = tmp_path / "coord_alive_fail"
    coord_dir.mkdir(parents=True)

    batch_1 = tmp_path / "batch_1"
    batch_1.mkdir(parents=True)
    register_batch_path(coord_dir, batch_1)
    job_alive = batch_1 / "job_001"
    ensure_job_layout(job_alive)
    (job_alive / "job_config.json").write_text(json.dumps({"target_language": "Chinese", "coordination_dir": str(coord_dir)}), encoding="utf-8")
    # Mark job as failed/launch_failed, but give it our own current process PID (definitely alive!)
    (job_alive / "job_pid.txt").write_text(f"{os.getpid()}\n", encoding="utf-8")
    update_progress(job_alive, status="failed", guardian_status="launch_failed", pid=os.getpid())

    # 1. count_global_active_jobs must recognize the living process and count it
    global_running, _, _ = count_global_active_jobs(coord_dir, stale_sec=300)
    assert len(global_running) == 1
    assert global_running[0].resolve() == job_alive.resolve()

    # 2. Sweep another batch with max_parallel=1: must NOT dispatch any jobs because slot is occupied
    batch_2 = tmp_path / "batch_2"
    batch_2.mkdir(parents=True)
    job_pending = batch_2 / "job_002"
    ensure_job_layout(job_pending)
    (job_pending / "job_config.json").write_text(json.dumps({"target_language": "Chinese", "coordination_dir": str(coord_dir)}), encoding="utf-8")
    update_progress(job_pending, status="pending")

    dispatched = []
    with patch("subprocess.check_call", side_effect=lambda cmd, **kw: dispatched.append(cmd)):
        rep = sweep_once(batch_2, coordination_dir=coord_dir, max_parallel=1, stale_sec=300)
        assert rep["status"] == "ok"
        assert rep["dispatched"] == 0
        assert len(dispatched) == 0


def test_start_detached_job_terminates_child_on_bookkeeping_failure(tmp_path):
    """Verify that start_detached_job terminates the spawned child process if writing PID/progress fails."""
    from unittest.mock import MagicMock
    from scripts.start_detached_job import main as start_detached_main

    job = tmp_path / "job_kill_test"
    ensure_job_layout(job)
    (job / "job_config.json").write_text(json.dumps({"target_language": "Chinese"}), encoding="utf-8")
    update_progress(job, status="pending")

    mock_proc = MagicMock()
    mock_proc.pid = 99999

    with patch("subprocess.Popen", return_value=mock_proc):
        with patch.object(Path, "write_text", side_effect=OSError("Disk write failed")):
            with pytest.raises(OSError, match="Disk write failed"):
                start_detached_main(["--job-dir", str(job), "--", "dummy_command"])

    # Verify terminate was called to prevent orphan process
    assert mock_proc.terminate.called
    assert mock_proc.wait.called


def test_discover_jobs_permission_error_halts_sweep(tmp_path):
    """Verify that discover_jobs raises RuntimeError on directory scan failure, and sweep_once halts without dispatch."""
    from scripts.cron_sweep_jobs import discover_jobs, sweep_once

    coord_dir = tmp_path / "coord_perm"
    coord_dir.mkdir(parents=True)

    batch_dir = tmp_path / "batch_perm"
    batch_dir.mkdir(parents=True)
    job_pending = batch_dir / "job_001"
    ensure_job_layout(job_pending)
    (job_pending / "job_config.json").write_text(json.dumps({"target_language": "Chinese", "coordination_dir": str(coord_dir)}), encoding="utf-8")
    update_progress(job_pending, status="pending")

    # Mock iterdir on batch_dir to raise PermissionError
    orig_iterdir = Path.iterdir

    def mock_iterdir(self):
        if self.resolve() == batch_dir.resolve():
            raise PermissionError("Access denied")
        return orig_iterdir(self)

    with patch.object(Path, "iterdir", mock_iterdir):
        # 1. discover_jobs directly raises RuntimeError
        with pytest.raises(RuntimeError, match="Cannot enumerate directory"):
            discover_jobs(batch_dir)

        # 2. sweep_once aborts safely with status=error
        dispatched = []
        with patch("subprocess.check_call", side_effect=lambda cmd, **kw: dispatched.append(cmd)):
            rep = sweep_once(batch_dir, coordination_dir=coord_dir, max_parallel=1)
            assert rep["status"] == "error"
            assert "Failed to discover" in rep["error"]
            assert len(dispatched) == 0


def test_profile_relative_terms_file_resolves_against_profile_dir(tmp_path):
    """Verify that a relative terms_file in a profile resolves relative to the profile file directory."""
    from scripts.run_pipeline import apply_translation_config

    profile_dir = tmp_path / "my_profiles"
    profile_dir.mkdir(parents=True)
    profile_file = profile_dir / "custom.yaml"
    profile_file.write_text("translation:\n  terms_file: glossary.txt\n", encoding="utf-8")

    glossary_file = profile_dir / "glossary.txt"
    glossary_file.write_text("API\t接口\n", encoding="utf-8")

    class Args:
        profile = str(profile_file)
        terms_file = None
        translation_style = "faithful"

    config = {
        "translation": {
            "terms_file": "glossary.txt",
            "context": "auto",
        }
    }

    # Run in a different directory (simulating job dir as cwd)
    other_cwd = tmp_path / "job_cwd"
    other_cwd.mkdir()
    old_cwd = os.getcwd()
    try:
        os.chdir(other_cwd)
        args = Args()
        apply_translation_config(args, config)
        assert args.terms_file == str(glossary_file.resolve())
    finally:
        os.chdir(old_cwd)


def test_l0_plist_generation_and_install_creates_logs(tmp_path):
    """Verify that l0_resident_guard.sh --generate-plist and --install-plist work dynamically and create output/logs."""
    target_plist = tmp_path / "com.videodubber.l0guard.plist"
    script = Path(__file__).resolve().parent.parent / "scripts" / "l0_resident_guard.sh"

    # 1. Test --generate-plist
    result = subprocess.run(
        [str(script), "--generate-plist", str(target_plist)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert target_plist.is_file()
    plist_text = target_plist.read_text(encoding="utf-8")
    assert "__PROJECT_ROOT__" not in plist_text
    assert "com.videodubber.l0guard" in plist_text
    assert "output/logs/l0_launchd.log" in plist_text

    # 2. Test --install-plist to another target
    install_target = tmp_path / "LaunchAgents" / "com.videodubber.l0guard.plist"
    result = subprocess.run(
        [str(script), "--install-plist", str(install_target)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert install_target.is_file()
    assert "__PROJECT_ROOT__" not in install_target.read_text(encoding="utf-8")





