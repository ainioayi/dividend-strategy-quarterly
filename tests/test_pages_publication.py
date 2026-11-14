"""用真实本地Git远端验证排队发布不会回退数据，不访问GitHub。"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/pages.yml"


def git(folder, *arguments):
    result = subprocess.run(
        ["git", "-C", str(folder), *arguments], check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout.strip()


def workflow_steps():
    return re.split(r"(?m)^      - ", WORKFLOW.read_text(encoding="utf-8"))[1:]


def checkout_ref(event_sha):
    step = next(step for step in workflow_steps() if "uses: actions/checkout@" in step)
    configured = re.search(r"(?m)^          ref:\s*(\S+)", step)
    return configured[1].strip("\"'") if configured else event_sha


def publication_gate(folder):
    step = next((step for step in workflow_steps() if "id: publish_revision" in step), None)
    if step is None:
        return subprocess.CompletedProcess([], 0, "", "")
    script = step.split("        run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in script.splitlines() if line.startswith("          "))
    bash = shutil.which("bash")
    if os.name == "nt":
        # Windows系统bash可能指向未安装发行版的WSL，使用现有Git自带的shell。
        bash = str(Path(shutil.which("git")).resolve().parents[1] / "bin/bash.exe")
    return subprocess.run([bash, "-c", script], cwd=folder, capture_output=True, text=True, encoding="utf-8")


def repositories(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    publisher = tmp_path / "publisher"
    publisher.mkdir()
    git(publisher, "init", "-b", "main")
    git(publisher, "config", "user.name", "发布回归测试")
    git(publisher, "config", "user.email", "publication-test@example.invalid")
    git(publisher, "config", "core.autocrlf", "false")
    (publisher / "site").mkdir()
    (publisher / "site/stock-bond.json").write_text(json.dumps({"as_of": "2026-11-11"}), encoding="utf-8")
    (publisher / "site/chart.css").write_text("body{color:black}\n", encoding="utf-8")
    git(publisher, "add", ".")
    git(publisher, "commit", "-m", "旧数据")
    git(publisher, "remote", "add", "origin", str(remote))
    git(publisher, "push", "-u", "origin", "main")
    return remote, publisher


def publish_new_data(publisher):
    (publisher / "site/stock-bond.json").write_text(json.dumps({"as_of": "2026-11-13"}), encoding="utf-8")
    git(publisher, "add", ".")
    git(publisher, "commit", "-m", "定时任务发布新数据")
    git(publisher, "push", "origin", "HEAD:main")


def start_queued_job(remote, folder, event_sha):
    subprocess.run(["git", "clone", "-b", "main", str(remote), str(folder)], check=True, capture_output=True)
    git(folder, "checkout", checkout_ref(event_sha))


def test_queued_css_event_uses_new_data_without_generated_changes(tmp_path):
    remote, publisher = repositories(tmp_path)
    (publisher / "site/chart.css").write_text("body{color:green}\n", encoding="utf-8")
    git(publisher, "add", ".")
    git(publisher, "commit", "-m", "排队的CSS变更")
    git(publisher, "push")
    event_sha = git(publisher, "rev-parse", "HEAD")
    publish_new_data(publisher)
    queued = tmp_path / "queued-job"
    start_queued_job(remote, queued, event_sha)
    git(queued, "add", "site/stock-bond.json")
    assert not git(queued, "diff", "--cached", "--name-only")
    result = publication_gate(queued)
    assert result.returncode == 0, result.stdout + result.stderr
    artifact = json.loads((queued / "site/stock-bond.json").read_text(encoding="utf-8"))
    assert artifact["as_of"] == "2026-11-13"
    assert (queued / "site/chart.css").read_text(encoding="utf-8").strip() == "body{color:green}"


def test_remote_advances_after_validation_without_local_changes_stops_publication(tmp_path):
    remote, publisher = repositories(tmp_path)
    event_sha = git(publisher, "rev-parse", "HEAD")
    queued = tmp_path / "running-job"
    start_queued_job(remote, queued, event_sha)
    publish_new_data(publisher)
    assert not git(queued, "diff", "--cached", "--name-only")
    result = publication_gate(queued)
    assert result.returncode != 0, "main已出现新数据时，必须阻止打包当前旧快照"
    assert json.loads((publisher / "site/stock-bond.json").read_text(encoding="utf-8"))["as_of"] == "2026-11-13"
    assert git(queued, "rev-parse", "HEAD") == event_sha
