import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from budget import SCENARIO


class ApiError(RuntimeError):
    pass


def api(url, *, token=None, method="GET", body=None, headers=None):
    request_headers = {"Accept": "application/json", **(headers or {})}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=request_headers, method=method), timeout=90) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        raise ApiError(f"{method} returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise ApiError(f"{method} could not read a JSON response ({type(exc).__name__})") from exc


class Hindsight:
    def __init__(self, bank=None):
        self.base = os.environ.get("HINDSIGHT_URL", "").rstrip("/")
        self.token = os.environ.get("HINDSIGHT_API_KEY")
        self.bank = bank or os.environ.get("HINDSIGHT_BANK")
        origin = urlsplit(self.base)
        if not self.bank or not origin.hostname or not (
            origin.scheme == "https" or (origin.scheme == "http" and origin.hostname in ("localhost", "127.0.0.1"))
        ):
            raise ApiError("Configure HINDSIGHT_URL and HINDSIGHT_BANK")
        self.url = f"{self.base}/v1/default/banks/{quote(self.bank, safe='')}"
        self.audit = []

    def request(self, method, path="", body=None):
        response = api(f"{self.url}{path}", token=self.token, method=method, body=body)
        self.audit.append({"method": method, "path": path, "bank": self.bank, "response": response})
        return response

    def recall(self, query, tag):
        response = self.request("POST", "/memories/recall", {
            "query": query, "tags": [tag], "tags_match": "any_strict",
            "types": ["world", "experience"], "budget": "mid", "max_tokens": 3000,
        })
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise ApiError("Recall response omitted results")
        records = {}
        for hit in response["results"]:
            doc_id = hit.get("document_id")
            if not doc_id or not hit.get("id"):
                continue
            if doc_id not in records:
                path = f"/documents/{quote(doc_id, safe='')}"
                document = self.request("GET", path)
                try:
                    record = json.loads(document["original_text"])
                except (KeyError, ValueError, TypeError):
                    continue
                if not isinstance(record, dict) or record.get("id") != doc_id or record.get("scenario") != SCENARIO:
                    continue
                records[doc_id] = {"record": record, "source": self.url + path, "fact_ids": []}
            records[doc_id]["fact_ids"].append(hit["id"])
        return list(records.values())

    def retain(self, records):
        return self.request("POST", "/memories", {
            "async": False,
            "items": [{
                "content": json.dumps(record, sort_keys=True),
                "context": record["summary"],
                "timestamp": record["last_sample_at"],
                "document_id": record["id"],
                "tags": [f"commit-{record['commit']}" if record["phase"] == "regression" else SCENARIO],
            } for record in records],
        })


def main():
    from replay import evidence

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("new-bank")
    retain = commands.add_parser("retain")
    retain.add_argument("journal", type=Path)
    retain.add_argument("--raw-base-url", help="HTTPS directory containing the published replay files")
    args = parser.parse_args()
    if args.command == "new-bank":
        client = Hindsight("secondlook-" + uuid4().hex)
        client.request("PUT", body={"name": client.bank})
        print(client.bank)
        return
    client = Hindsight()
    records = evidence(args.journal)
    for record in records:
        record["summary"] = (
            f"Controlled lab incident, {record['phase']} phase of a PostgreSQL checkout worker "
            f"connection replay. Observed {record['observed_worker']} worker connections with "
            f"a configured maximum of {record['worker_upper_bound']}. "
            f"Measured {record['probe_failures']} failed checkout probes across {record['samples']} samples."
        )
        if args.raw_base_url:
            if urlsplit(args.raw_base_url).scheme != "https":
                parser.error("--raw-base-url requires HTTPS")
            record["raw_events"] = args.raw_base_url.rstrip("/") + "/events.jsonl"
            record["raw_logs"] = args.raw_base_url.rstrip("/") + "/compose.log"
    client.retain(records)
    for record in records:
        source = client.request("GET", f"/documents/{quote(record['id'], safe='')}")
        if json.loads(source["original_text"]) != record or source["memory_unit_count"] < 1:
            raise ApiError(f"Retained source {record['id']} did not produce matching memory")
    output = args.journal.with_name(f"hindsight-retain-{client.bank}.json")
    output.write_text(json.dumps(client.audit, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    try:
        main()
    except ApiError as exc:
        raise SystemExit(f"Hindsight unavailable. {exc}") from exc
