# Second Look

Second Look reviews PostgreSQL connection budgets on GitHub pull requests. It calculates the proposed worker limit from the configuration diff, then uses Hindsight to retrieve incident and replay evidence. The advisory distinguishes a configuration that passes the budget from one that has also passed a replay under worker load.

## Run the controlled lab incident

You need Git, Python 3.12, and a running Linux Docker engine with the Docker Compose plugin. The Python CLI uses the standard library. Docker builds the lab services and installs their PostgreSQL client inside the images.

From the repository root, run:

~~~sh
python replay.py incident connection-budget.json lab/overload.json
~~~

The runner creates a separate Compose project with PostgreSQL, a checkout API, and workers on an internal network. It measures checkout health at baseline, under saturation, and after restoring the baseline settings. The worker and checkout services connect as separate non-superuser PostgreSQL roles. The lab uses disposable credentials and data, and the runner removes its Compose project and volumes when finished.

The command prints an output directory under `runs/`. Open `timeline.html` for the measured timeline. Inspect `events.jsonl` for probe results and connection counts, `compose.log` for command and service output, and `records.json` for the derived evidence.

## Review a configuration change

Edit `connection-budget.json` on a topic branch. To propose the overload from the lab, copy `lab/overload.json` over `connection-budget.json` and commit the change. Open a pull request from that branch in the same repository.

The reviewer compares the configured maximum with the available worker budget:

~~~text
Worker upper bound = worker_replicas × worker_pool_max
Worker budget = postgres_max_connections − postgres_superuser_reserved
                − checkout_connections − headroom
~~~

In this lab, PostgreSQL has 20 slots, 3 are reserved for superusers, and the budget reserves 3 for checkout and 3 for headroom. That leaves 11 for workers. The overloaded configuration can request 20 connections. The corrected configuration caps workers at 9 using 3 replicas and 3 connections per replica.

To run the deterministic review locally after committing a configuration change:

~~~sh
python -X utf8 review.py --memory off local main HEAD
~~~

GitHub Actions runs the reviewer from the trusted base commit when an in-repository PR changes the configuration. It reads the GitHub diff and updates one advisory comment. The review workflow does not start containers or connect to the lab database.

## Enable Hindsight and verify a replay

Create a dedicated Hindsight bank and an API key scoped to it. In the repository's **Settings → Secrets and variables → Actions**, configure:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `HINDSIGHT_API_KEY` | Scoped Hindsight API key |
| Variable | `HINDSIGHT_URL` | API endpoint, such as `https://api.hindsight.vectorize.io` for Hindsight Cloud |
| Variable | `HINDSIGHT_BANK` | Exact bank ID |
| Variable | `SECONDLOOK_MEMORY` | Optional. Defaults to `on`. Set to `off` to bypass retrieval. |

Publish the measured incident before the memory-enabled review. Copy the four output files to `evidence/<run-id>/` on a separate branch named `codex/demo-evidence`, creating the branch from `main` if needed. Preserve the raw journal bytes by staging with `git -c core.autocrlf=false add -- evidence/<run-id>/`. Commit and push the evidence branch. In GitHub Actions, run **Retain measured lab evidence** from `main` with the replay directory name as `run_id` and the evidence branch's tip SHA as `evidence_commit`. The workflow validates the recorded results, sends them to Hindsight, and saves the API response as an artifact.

Once the PR advisory reports **Budget passes** and **Replay safeguard · Pending**, check out the exact reviewed PR commit with a clean working tree. Run:

~~~sh
python replay.py regression HEAD
~~~

The regression records 12 successful checkout probes under the configured worker load before it emits a passing record. Publish and retain its output using the same evidence workflow. Then rerun the PR's advisory workflow for the same commit. The safeguard changes to **Verified** when Hindsight retrieves evidence that matches the reviewed commit, effective configuration, and replay scenario. The advisory includes the SHA-256 digest recorded from the raw journal so the cited run can be audited.

## Recorded demo

The controlled lab incident measured 17 worker connections and 3 failed checkout probes under overload. All 3 measured probes succeeded after rollback. The corrected commit held 9 worker connections with 12 successful checkout probes. The advisory moved from **Pending** to **Verified** for that same commit after evidence retention.

- [Demo pull request and advisory](https://github.com/aganirudh/secondlook-demo/pull/1#issuecomment-6705659244)
- [Incident output](https://github.com/aganirudh/secondlook-demo/tree/f1ccb1a92bc287daaf4dee9852bfafa0587eb992/evidence/secondlook-8900e4c176f9)
- [Corrected replay output](https://github.com/aganirudh/secondlook-demo/tree/d8445dd5a81db268f2b5c409aa7c806c75e6631b/evidence/secondlook-06d7dc5f7b2a)
- [Final advisory workflow](https://github.com/aganirudh/secondlook-demo/actions/runs/42364472119/attempts/2)

These links require access to the demo repository. Raw local runs are ignored by Git.
