import argparse
import html
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from budget import CONFIG, SCENARIO, digest, fingerprint, ordinary_slots, parse_config, worker_budget, worker_limit


ROOT = Path(__file__).resolve().parent
SQL = """
SELECT json_build_object(
  'max', current_setting('max_connections')::int,
  'reserved', current_setting('superuser_reserved_connections')::int,
  'worker', (SELECT count(*) FROM pg_stat_activity WHERE usename = 'worker' AND backend_type = 'client backend'),
  'checkout', (SELECT count(*) FROM pg_stat_activity WHERE usename = 'checkout' AND backend_type = 'client backend'),
  'total', (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'),
  'ordinary_roles', (SELECT count(*) FROM pg_roles WHERE rolname IN ('checkout', 'worker') AND NOT rolsuper)
);
"""
MIN_SAMPLES = {"baseline": 3, "saturation": 3, "rollback": 3, "regression": 12}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def matches(phase, config, probe, counts):
    if (counts["max"], counts["reserved"], counts["ordinary_roles"]) != (
        config["postgres_max_connections"], config["postgres_superuser_reserved"], 2,
    ):
        return False
    if phase == "saturation":
        return (
            counts["worker"] == ordinary_slots(config) and probe["status"] == 503
            and ("connection slots" in probe["detail"] or "too many clients" in probe["detail"])
        )
    return counts["worker"] == worker_limit(config) and probe["status"] == 200


def evidence(journal):
    raw = journal.read_bytes()
    rows = [json.loads(line) for line in raw.splitlines()]
    if rows[0].get("kind") != "run" or rows[-1].get("kind") != "complete":
        raise ValueError("Replay did not complete")
    run = rows[0]
    phases = ["baseline", "saturation", "rollback"] if run["mode"] == "incident" else ["regression"]
    if run["scenario"] != SCENARIO or [r["phase"] for r in rows if r["kind"] == "config"] != phases:
        raise ValueError("Replay scenario or phase order differs")
    records = []
    for phase in phases:
        config_row = next(row for row in rows if row["kind"] == "config" and row["phase"] == phase)
        config = parse_config(json.dumps(config_row["config"]))
        samples = [row for row in rows if row["kind"] == "sample" and row["phase"] == phase]
        if len(samples) != MIN_SAMPLES[phase] or not all(
            matches(phase, config, row["probe"], row["postgres"]) for row in samples
        ):
            raise ValueError(f"Replay evidence failed the {phase} checks")
        records.append({
            "label": "Controlled lab incident",
            "id": f"{run['id']}-{phase}", "phase": phase, "scenario": SCENARIO,
            "commit": run["commit"], "config": config, "config_sha256": fingerprint(config),
            "started_at": config_row["at"], "first_sample_at": samples[0]["at"],
            "last_sample_at": samples[-1]["at"], "journal_sha256": digest(raw),
            "samples": len(samples),
            "probe_failures": sum(row["probe"]["status"] != 200 for row in samples),
            "observed_worker": max(row["postgres"]["worker"] for row in samples),
            "observed_total": max(row["postgres"]["total"] for row in samples),
            "worker_upper_bound": worker_limit(config),
        })
    if run["mode"] == "incident" and records[0]["config"] != records[-1]["config"]:
        raise ValueError("Rollback differs from the baseline")
    return records


class Lab:
    def __init__(self, mode, commit):
        self.id = "secondlook-" + uuid4().hex[:12]
        self.output = ROOT / "runs" / self.id
        self.output.mkdir(parents=True)
        self.journal = self.output / "events.jsonl"
        self.compose = ["docker", "compose", "-p", self.id, "-f", str(ROOT / "lab" / "compose.yaml")]
        self.env = os.environ.copy()
        self.configured = False
        self.record(kind="run", id=self.id, mode=mode, commit=commit, scenario=SCENARIO)
        print(f"Run output: {self.output}", flush=True)

    def record(self, **row):
        row["at"] = now()
        with self.journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    def command(self, *args, timeout=600):
        self.record(kind="command", args=args)
        result = subprocess.run(
            [*self.compose, *args], cwd=ROOT, env=self.env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        with (self.output / "compose.log").open("a", encoding="utf-8") as stream:
            stream.write(f"\n{now()} {args!r} (exit {result.returncode})\n{result.stdout}{result.stderr}")
        if result.returncode:
            raise RuntimeError(f"Docker Compose {args[0]} exited {result.returncode}. See compose.log.")
        return result.stdout

    def set_config(self, phase, config):
        self.env.update({
            "DB_MAX": str(config["postgres_max_connections"]),
            "DB_RESERVED": str(config["postgres_superuser_reserved"]),
            "CHECKOUT_MAX": str(config["checkout_connections"]),
            "WORKER_REPLICAS": str(config["worker_replicas"]),
            "WORKER_POOL_MAX": str(config["worker_pool_max"]),
        })
        self.configured = True
        self.record(kind="config", phase=phase, config=config)

    def sample(self, phase, kind):
        probe = json.loads(self.command("exec", "-T", "checkout", "python", "services.py", "probe", timeout=20))
        counts = json.loads(self.command(
            "exec", "-T", "db", "psql", "-XAt", "-v", "ON_ERROR_STOP=1",
            "-U", "postgres", "-d", "checkout_lab", "-c", SQL, timeout=20,
        ))
        self.record(kind=kind, phase=phase, probe=probe, postgres=counts)
        return probe, counts

    def measure(self, phase, config):
        deadline = time.monotonic() + 90
        while True:
            if matches(phase, config, *self.sample(phase, "warmup")):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{phase} did not reach its expected PostgreSQL and probe conditions")
            time.sleep(1)
        for _ in range(MIN_SAMPLES[phase]):
            if not matches(phase, config, *self.sample(phase, "sample")):
                raise RuntimeError(f"{phase} lost its expected PostgreSQL and probe conditions")
            time.sleep(1)

    def run(self, phases):
        try:
            for i, (phase, config) in enumerate(phases):
                if i:
                    self.command("logs", "--timestamps", "--no-color")
                self.set_config(phase, config)
                if not i:
                    self.command("up", "-d", "--wait", "--wait-timeout", "90", "db")
                    self.command("up", "-d", "--build", "checkout")
                    self.command("exec", "-T", "checkout", "python", "services.py", "ready")
                self.command("up", "-d", "--no-deps", "--force-recreate", "worker")
                self.measure(phase, config)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            self.record(kind="failed", detail=str(exc))
            raise
        finally:
            if self.configured:
                try:
                    self.command("logs", "--timestamps", "--no-color")
                finally:
                    self.command("down", "--volumes", timeout=90)
        self.record(kind="complete")


def committed_config(commit):
    git = ["git", "--git-dir", str(ROOT / ".git"), "--work-tree", str(ROOT)]
    sha = subprocess.check_output([*git, "rev-parse", "--verify", f"{commit}^{{commit}}"], text=True).strip()
    dirty = subprocess.check_output([
        *git, "status", "--porcelain", "--", "lab", "budget.py", "replay.py", CONFIG,
    ], text=True)
    head = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    if head != sha or dirty:
        raise ValueError("Regression requires the clean, checked-out commit under review")
    config = parse_config(subprocess.check_output([*git, "show", f"{sha}:{CONFIG}"]))
    if worker_limit(config) > worker_budget(config):
        raise ValueError("Regression requires a passing connection budget")
    return sha, config


def timeline(records, output):
    rows = []
    for item in records:
        config = html.escape(json.dumps(item["config"], indent=2))
        rows.append(
            f"<section><h2>{item['phase'].capitalize()}</h2>"
            f"<p>{item['first_sample_at']} to {item['last_sample_at']} UTC</p>"
            f"<p>{item['probe_failures']} checkout failures across {item['samples']} probes. "
            f"PostgreSQL observed {item['observed_worker']} worker connections and "
            f"{item['observed_total']} total client connections, including the observer. "
            f"The configured worker upper bound was {item['worker_upper_bound']}.</p>"
            f"<details><summary>Effective configuration</summary><pre>{config}</pre></details></section>"
        )
    output.write_text(
        "<!doctype html><html lang='en'><meta charset='utf-8'><title>Controlled lab incident</title>"
        "<style>body{font:16px system-ui;max-width:780px;margin:3rem auto;padding:0 1rem}"
        "section{padding:1rem 0;border-bottom:1px solid #bbb}pre{overflow:auto}</style>"
        "<h1>Controlled lab incident</h1>"
        "<p><a href='events.jsonl'>Raw events</a> · <a href='compose.log'>Compose and PostgreSQL output</a></p>"
        + "".join(rows), encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="mode", required=True)
    incident = commands.add_parser("incident")
    incident.add_argument("baseline", type=Path)
    incident.add_argument("overload", type=Path)
    regression = commands.add_parser("regression")
    regression.add_argument("commit")
    args = parser.parse_args()
    if args.mode == "incident":
        baseline, overload = (parse_config(path.read_bytes()) for path in (args.baseline, args.overload))
        if any(baseline[key] != overload[key] for key in baseline if not key.startswith("worker_")):
            parser.error("The incident must change only worker replicas and pool limits")
        if worker_limit(baseline) > worker_budget(baseline) or worker_limit(overload) <= ordinary_slots(overload):
            parser.error("The baseline must pass the budget and the overload must exceed ordinary slots")
        phases = [("baseline", baseline), ("saturation", overload), ("rollback", baseline)]
        commit = None
    else:
        commit, config = committed_config(args.commit)
        phases = [("regression", config)]
    lab = Lab(args.mode, commit)
    lab.run(phases)
    records = evidence(lab.journal)
    (lab.output / "records.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    timeline(records, lab.output / "timeline.html")


if __name__ == "__main__":
    main()
