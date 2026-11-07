import argparse
import base64
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from budget import CONFIG, SCENARIO, fingerprint, ordinary_slots, parse_config, worker_budget, worker_limit
from hindsight import ApiError, Hindsight, api


MARKER = "<!-- secondlook-connection-budget -->"


def valid_url(url):
    if isinstance(url, str) and not re.search(r"[\s()<>]", url):
        parsed = urlsplit(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    return False


def source_line(item):
    record = item["record"]
    fact_ids = ", ".join(f"`{ident}`" for ident in item["fact_ids"] if re.fullmatch(r"[a-zA-Z0-9_-]+", ident))
    source = f"[source document]({item['source']})" if valid_url(item["source"]) else "source document"
    text = (
        f"- {record['phase'].capitalize()} at {record['first_sample_at']}. "
        f"{record['probe_failures']}/{record['samples']} failed probes, "
        f"{record['observed_worker']} observed worker connections. "
        f"{source} `{record['id']}`, recalled fact IDs {fact_ids}."
    )
    for name, label in (("raw_events", "raw events"), ("raw_logs", "Compose output")):
        if valid_url(record.get(name)):
            text += f" [{label}]({record[name]})"
    if record.get("journal_sha256"):
        text += f" Journal SHA-256 `{record['journal_sha256']}`."
    return text


def is_verification(record, sha, config):
    return (
        record.get("phase") == "regression" and record.get("scenario") == SCENARIO
        and record.get("commit") == sha and record.get("config") == config
        and record.get("config_sha256") == fingerprint(config)
        and type(record.get("samples")) is int and record["samples"] >= 12
        and record.get("probe_failures") == 0
        and record.get("observed_worker") == worker_limit(config)
        and re.fullmatch("[0-9a-f]{64}", record.get("journal_sha256", ""))
    )


def evaluate(before, after, patch, sha, memory):
    try:
        if not patch.strip():
            raise ValueError("The PR diff has no readable configuration patch")
        config = parse_config(after)
        prior = parse_config(before) if before else {}
    except ValueError as exc:
        return f"### Second Look advisory\n\n**Configuration could not be reviewed.** {exc}", []
    used, limit = worker_limit(config), worker_budget(config)
    passes = used <= limit
    verification = "Pending" if passes else "Budget exceeded"
    audit, sources = [], []
    status = "Off (retrieval bypassed)"
    if memory:
        try:
            client = Hindsight()
            audit = client.audit
            incidents = client.recall(
                "Controlled lab incident PostgreSQL exhausted worker connection slots, checkout probe failures and rollback recovery",
                SCENARIO,
            )
            sources = [item for item in incidents if item["record"].get("phase") in ("saturation", "rollback")]
            if passes:
                replays = client.recall(
                    f"Successful corrected PostgreSQL checkout worker connection regression for commit {sha} and scenario {SCENARIO}",
                    f"commit-{sha}",
                )
                verified = [item for item in replays if is_verification(item["record"], sha, config)]
                if verified:
                    verification = "Verified"
                    sources.extend(verified)
            status = f"Recalled {len(sources)} matching source documents from `{client.bank}`"
        except ApiError as exc:
            status = f"Unavailable ({exc})"
    changed = [f"- `{key}` {prior.get(key, 'unset')} → {value}" for key, value in config.items() if prior.get(key) != value]
    lines = [
        "### Second Look advisory", "",
        f"Reviewed commit `{sha}`. Effective configuration SHA-256 `{fingerprint(config)}`.", "",
        f"**Budget {'passes' if passes else 'exceeded'}.** "
        f"Configured workers can open {config['worker_replicas']} × {config['worker_pool_max']} = {used} connections. "
        f"PostgreSQL has {ordinary_slots(config)} ordinary slots, leaving {limit} for workers "
        f"after reserving {config['checkout_connections']} for checkout and {config['headroom']} for headroom.",
        "", f"**Replay safeguard** · {verification}", "", f"**Hindsight** · {status}",
        "", "**Configuration changes in the PR diff**", *(changed or ["- No effective values changed."]),
    ]
    if sources:
        lines.extend(["", "**Controlled lab incident and replay records**", *(source_line(item) for item in sources)])
    return "\n".join(lines), audit


def github_review(memory):
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    repo = os.environ["GITHUB_REPOSITORY"]
    number = payload["number"]
    sha = payload["pull_request"]["head"]["sha"]
    base_url = f"{os.environ['GITHUB_API_URL']}/repos/{repo}"

    def request(path, method="GET", body=None):
        return api(base_url + path, method=method, body=body, token=os.environ["GH_TOKEN"])

    pull = f"/pulls/{number}"
    snapshot = request(pull)
    if snapshot["head"]["sha"] != sha or snapshot["base"]["sha"] != payload["pull_request"]["base"]["sha"]:
        raise SystemExit("The PR changed since the event. Start a fresh review.")
    # The advisory handles one file. GitHub exposes the real patch in the PR files API.
    changed = None
    for page in range(1, 31):
        files = request(f"{pull}/files?per_page=100&page={page}")
        changed = next((item for item in files if item["filename"] == CONFIG), None)
        if changed or len(files) < 100:
            break
    if changed is None:
        raise SystemExit(f"The PR diff did not include {CONFIG}")

    def config_at(ref):
        response = request(f"/contents/{CONFIG}?ref={ref}")
        if response.get("type") != "file" or response["encoding"] != "base64":
            raise ValueError("Configuration is not a regular GitHub file")
        return base64.b64decode(response["content"])

    if changed["status"] not in ("modified", "added"):
        report = "### Second Look advisory\n\nThe proposed change removes or renames the connection budget."
    else:
        base = snapshot["base"]["sha"]
        comparison = request(f"/compare/{base}...{sha}")
        before = config_at(comparison["merge_base_commit"]["sha"]) if changed["status"] == "modified" else None
        after = config_at(sha)
        report, _ = evaluate(before, after, changed.get("patch", ""), sha, memory)

    run = f"{os.environ['GITHUB_SERVER_URL']}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    body = f"{MARKER}\n{report}\n\n[GitHub workflow run]({run})"
    current = request(pull)
    if (current["head"]["sha"], current["base"]["sha"]) != (sha, snapshot["base"]["sha"]):
        raise SystemExit("The PR changed during review. Start a fresh review.")
    comments_path = f"/issues/{number}/comments"
    for page in range(1, 101):
        comments = request(f"{comments_path}?per_page=100&page={page}")
        comment = next((item for item in comments if item["user"]["login"] == "github-actions[bot]"
                        and item["body"].startswith(MARKER)), None)
        if comment or len(comments) < 100:
            break
    if comment:
        published = request(f"/issues/comments/{comment['id']}", "PATCH", {"body": body})
    else:
        published = request(comments_path, "POST", {"body": body})
    print(f"Reviewed {sha}. Published advisory at {published['html_url']}")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
        stream.write(f"{body}\n\n[Published advisory]({published['html_url']})\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory", choices=["on", "off"], default="on")
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("github")
    local = commands.add_parser("local")
    local.add_argument("base")
    local.add_argument("head")
    local.add_argument("--audit", type=Path)
    args = parser.parse_args()
    memory = args.memory == "on"
    if args.mode == "github":
        github_review(memory)
        return

    def git(*command):
        return subprocess.check_output(["git", *command])

    sha = git("rev-parse", "--verify", f"{args.head}^{{commit}}").decode().strip()
    merge_base = git("merge-base", args.base, sha).decode().strip()
    patch = git("diff", merge_base, sha, "--", CONFIG).decode()
    before = git("show", f"{merge_base}:{CONFIG}")
    after = git("show", f"{sha}:{CONFIG}")
    report, audit = evaluate(before, after, patch, sha, memory)
    print(report)
    if args.audit:
        args.audit.write_text(json.dumps(audit, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
