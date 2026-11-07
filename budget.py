import hashlib
import json


CONFIG = "connection-budget.json"
SCENARIO = "checkout-postgres-worker-pool-v1"
FIELDS = (
    "postgres_max_connections", "postgres_superuser_reserved", "checkout_connections",
    "headroom", "worker_replicas", "worker_pool_max",
)


def parse_config(raw):
    def unique_pairs(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate connection budget key")
        return result

    config = json.loads(raw, object_pairs_hook=unique_pairs)
    if not isinstance(config, dict) or set(config) != set(FIELDS):
        raise ValueError("Connection budget must contain exactly " + ", ".join(FIELDS))
    for name, value in config.items():
        minimum = 0 if name in ("headroom", "postgres_superuser_reserved") else 1
        if type(value) is not int or not minimum <= value <= 10000:
            raise ValueError(f"{name} must be an integer between {minimum} and 10000")
    if config["postgres_superuser_reserved"] >= config["postgres_max_connections"]:
        raise ValueError("PostgreSQL requires ordinary connection slots")
    return config


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(config):
    return digest(json.dumps(config, sort_keys=True, separators=(",", ":")).encode())


def worker_limit(config):
    return config["worker_replicas"] * config["worker_pool_max"]


def ordinary_slots(config):
    return config["postgres_max_connections"] - config["postgres_superuser_reserved"]


def worker_budget(config):
    return ordinary_slots(config) - config["checkout_connections"] - config["headroom"]
