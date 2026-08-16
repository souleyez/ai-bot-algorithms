#!/usr/bin/env python3
"""Budgeted secondary visual assessment runtime.

Provider output is untrusted prediction data.  This module stamps provenance,
keeps attempts and observations immutable, and never writes human truth.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.algorithm_platform import evidence_ledger

try:
    from . import visual_registry
except ImportError:
    import visual_registry  # type: ignore[no-redef]


POLICY_SCHEMA = "ai-bot-secondary-recognition-policy-v1.schema.json"
PREDICTION_SCHEMA = "ai-bot-secondary-prediction-v1.schema.json"
REVIEW_FACT_SCHEMA = "ai-bot-review-fact-v1.schema.json"
RUNTIME_STATES = {"disabled", "shadow", "human_assisted"}
ERROR_ORDER = (
    "false_positive", "false_negative", "wrong_class", "geometry_error", "tag_error", "unusable",
)


class SecondaryRecognitionError(ValueError):
    pass


class RuntimeRevisionConflict(SecondaryRecognitionError):
    pass


class RuntimeIdempotencyConflict(SecondaryRecognitionError):
    pass


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS secondary_policy_runtime (
  policy_id TEXT PRIMARY KEY, policy_revision INTEGER NOT NULL, policy_digest TEXT NOT NULL,
  algorithm_key TEXT NOT NULL, runtime_state TEXT NOT NULL CHECK(runtime_state IN ('disabled','shadow','human_assisted')),
  state_revision INTEGER NOT NULL CHECK(state_revision >= 1), updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS secondary_runtime_receipts (
  idempotency_key TEXT PRIMARY KEY, request_fingerprint TEXT NOT NULL,
  response_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS secondary_assessment_jobs (
  job_id TEXT PRIMARY KEY, algorithm_key TEXT NOT NULL, item_id TEXT NOT NULL,
  base_review_revision INTEGER NOT NULL, image_sha256 TEXT NOT NULL,
  policy_id TEXT NOT NULL, policy_revision INTEGER NOT NULL, policy_digest TEXT NOT NULL,
  runtime_state TEXT NOT NULL, status TEXT NOT NULL, attempt_count INTEGER NOT NULL,
  observation_id TEXT, created_at TEXT NOT NULL, completed_at TEXT NOT NULL,
  UNIQUE(algorithm_key,item_id,base_review_revision,image_sha256,policy_id,policy_revision));
CREATE TABLE IF NOT EXISTS secondary_assessment_attempts (
  attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES secondary_assessment_jobs(job_id),
  attempt_number INTEGER NOT NULL, status TEXT NOT NULL, prediction_digest TEXT,
  latency_ms INTEGER NOT NULL, cost_microunits INTEGER NOT NULL,
  error_code TEXT NOT NULL, provider_receipt_ref TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(job_id,attempt_number));
CREATE TABLE IF NOT EXISTS secondary_observations (
  observation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES secondary_assessment_jobs(job_id),
  algorithm_key TEXT NOT NULL, item_id TEXT NOT NULL, base_review_revision INTEGER NOT NULL,
  image_sha256 TEXT NOT NULL, policy_id TEXT NOT NULL, policy_digest TEXT NOT NULL,
  canonical_observation_json TEXT NOT NULL, observation_digest TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS secondary_attempts_no_update BEFORE UPDATE ON secondary_assessment_attempts
BEGIN SELECT RAISE(ABORT,'secondary assessment attempts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS secondary_attempts_no_delete BEFORE DELETE ON secondary_assessment_attempts
BEGIN SELECT RAISE(ABORT,'secondary assessment attempts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS secondary_observations_no_update BEFORE UPDATE ON secondary_observations
BEGIN SELECT RAISE(ABORT,'secondary observations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS secondary_observations_no_delete BEFORE DELETE ON secondary_observations
BEGIN SELECT RAISE(ABORT,'secondary observations are immutable'); END;
"""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _content_digest(value: Mapping[str, Any]) -> str:
    return _digest({key: item for key, item in value.items() if key != "content_sha256"})


def _now(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise SecondaryRecognitionError("runtime time must be timezone-aware")
    return result.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    required = {
        "secondary_attempts_no_update", "secondary_attempts_no_delete",
        "secondary_observations_no_update", "secondary_observations_no_delete",
    }
    triggers = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if not required.issubset(triggers):
        raise RuntimeError("secondary observation immutability guards are unavailable")


def load_policy(path: Path, entry: visual_registry.AlgorithmEntry | None = None) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecondaryRecognitionError(f"cannot load secondary policy: {exc}") from exc
    if not isinstance(policy, dict):
        raise SecondaryRecognitionError("secondary policy must be an object")
    try:
        evidence_ledger.validate_contract_payload(POLICY_SCHEMA, policy)
    except evidence_ledger.EvidenceError as exc:
        raise SecondaryRecognitionError(str(exc)) from exc
    if policy.get("content_sha256") != _content_digest(policy):
        raise SecondaryRecognitionError("secondary policy content digest mismatch")
    if policy.get("review_state") != "accepted" or policy.get("runtime_initial_state") != "disabled":
        raise SecondaryRecognitionError("secondary policy is not accepted and disabled by default")
    resolved = entry or visual_registry.accepted_algorithms().get(str(policy.get("algorithm_key")))
    if resolved is None:
        raise SecondaryRecognitionError("secondary policy algorithm is not accepted")
    bindings = {
        "algorithm_key": resolved.algorithm_key,
        "task_type": resolved.task_type,
        "visual_semantics_bundle_ref": resolved.visual_semantics_version_ref,
        "visual_semantics_bundle_content_sha256": resolved.visual_semantics_content_sha256,
        "task_profile_ref": resolved.task_profile_ref,
        "task_profile_content_sha256": resolved.task_profile_content_sha256,
        "taxonomy_version_ref": resolved.taxonomy_version_ref,
        "taxonomy_content_sha256": resolved.taxonomy_content_sha256,
        "annotation_contract": resolved.annotation_contract,
    }
    if any(policy.get(key) != expected for key, expected in bindings.items()):
        raise SecondaryRecognitionError("secondary policy semantic binding mismatch")
    if not resolved.secondary_allowed or resolved.secondary_max_observations < 1:
        raise SecondaryRecognitionError("accepted task profile does not allow secondary observations")
    return policy


def load_bound_policies() -> dict[str, dict[str, Any]]:
    entries = visual_registry.accepted_algorithms()
    result: dict[str, dict[str, Any]] = {}
    policy_root = visual_registry.DEFAULT_REGISTRY_ROOT / "secondary-policies"
    for algorithm_key, entry in entries.items():
        if not entry.secondary_policy_ref:
            continue
        matches: list[Path] = []
        for path in sorted(policy_root.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict) and raw.get("content_sha256") == entry.secondary_policy_content_sha256:
                matches.append(path)
        if len(matches) != 1:
            raise SecondaryRecognitionError(f"algorithm {algorithm_key} secondary policy is unresolved")
        policy = load_policy(matches[0], entry)
        if policy["policy_id"] != entry.secondary_policy_ref:
            raise SecondaryRecognitionError(f"algorithm {algorithm_key} secondary policy ref mismatch")
        result[algorithm_key] = policy
    return result


def ensure_policy_runtime(connection: sqlite3.Connection, policy: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    timestamp = _timestamp(_now(now))
    existing = connection.execute(
        "SELECT * FROM secondary_policy_runtime WHERE policy_id=?", (policy["policy_id"],)
    ).fetchone()
    if existing is None:
        connection.execute(
            "INSERT INTO secondary_policy_runtime VALUES (?,?,?,?,?,?,?)",
            (policy["policy_id"], policy["policy_revision"], policy["content_sha256"],
             policy["algorithm_key"], "disabled", 1, timestamp),
        )
        return {"policy_id": policy["policy_id"], "runtime_state": "disabled", "state_revision": 1}
    if (existing[1], existing[2], existing[3]) != (
        policy["policy_revision"], policy["content_sha256"], policy["algorithm_key"],
    ):
        raise SecondaryRecognitionError("runtime policy binding drifted")
    return {"policy_id": existing[0], "runtime_state": existing[4], "state_revision": int(existing[5])}


def transition_runtime(
    connection: sqlite3.Connection,
    policy: Mapping[str, Any],
    *,
    expected_revision: int,
    next_state: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if next_state not in RUNTIME_STATES or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", idempotency_key):
        raise SecondaryRecognitionError("invalid runtime transition")
    current = ensure_policy_runtime(connection, policy, now=now)
    request = {"operation": "transition", "policy_id": policy["policy_id"], "expected_revision": expected_revision, "next_state": next_state}
    fingerprint = _digest(request)
    receipt = connection.execute(
        "SELECT request_fingerprint,response_json FROM secondary_runtime_receipts WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if receipt is not None:
        if receipt[0] != fingerprint:
            raise RuntimeIdempotencyConflict("runtime idempotency key conflicts")
        response = json.loads(receipt[1]); response["replayed"] = True
        return response
    if current["state_revision"] != expected_revision:
        raise RuntimeRevisionConflict("secondary runtime revision is stale")
    allowed = {
        "disabled": {"shadow"},
        "shadow": {"disabled", "human_assisted"},
        "human_assisted": {"disabled", "shadow"},
    }
    if next_state not in allowed[current["runtime_state"]]:
        raise SecondaryRecognitionError("secondary runtime transition is not allowed")
    revision = expected_revision + 1
    timestamp = _timestamp(_now(now))
    connection.execute(
        "UPDATE secondary_policy_runtime SET runtime_state=?,state_revision=?,updated_at=? WHERE policy_id=? AND state_revision=?",
        (next_state, revision, timestamp, policy["policy_id"], expected_revision),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise RuntimeRevisionConflict("secondary runtime revision is stale")
    response = {"policy_id": policy["policy_id"], "runtime_state": next_state, "state_revision": revision, "replayed": False}
    connection.execute(
        "INSERT INTO secondary_runtime_receipts VALUES (?,?,?,?)",
        (idempotency_key, fingerprint, _canonical(response), timestamp),
    )
    return response


def _validate_prediction(prediction: dict[str, Any], entry: visual_registry.AlgorithmEntry) -> str:
    try:
        canonical = evidence_ledger.validate_contract_payload(PREDICTION_SCHEMA, prediction)
    except evidence_ledger.EvidenceError as exc:
        raise SecondaryRecognitionError(str(exc)) from exc
    boxes = prediction["boxes"]
    labels = sorted(set(prediction["label_keys"]))
    for box in boxes:
        if not all(math.isfinite(float(box[key])) for key in ("x", "y", "w", "h")):
            raise SecondaryRecognitionError("secondary box is not finite")
        if float(box["x"]) + float(box["w"]) > 1 or float(box["y"]) + float(box["h"]) > 1:
            raise SecondaryRecognitionError("secondary box exceeds normalized bounds")
        if entry.normalize_label(str(box["label_key"])) != box["label_key"]:
            raise SecondaryRecognitionError("secondary box label is not canonical")
    box_labels = sorted({box["label_key"] for box in boxes})
    for label in labels:
        if entry.normalize_label(str(label)) != label:
            raise SecondaryRecognitionError("secondary label is not canonical")
    for tag in prediction["tag_assertions"]:
        if tag["tag_key"] not in entry.tag_keys:
            raise SecondaryRecognitionError("secondary tag is outside the pinned taxonomy")
    if entry.annotation_contract == "bbox.v1":
        if prediction["decision"] == "positive" and (not boxes or labels != box_labels):
            raise SecondaryRecognitionError("positive detection prediction requires matching boxes and labels")
        if prediction["decision"] == "negative" and (boxes or labels):
            raise SecondaryRecognitionError("negative detection prediction cannot contain boxes or labels")
    elif boxes:
        raise SecondaryRecognitionError("classification prediction cannot contain boxes")
    return canonical


def validate_observation(observation: dict[str, Any]) -> None:
    evidence_ledger.validate_contract_definition(REVIEW_FACT_SCHEMA, "secondaryObservation", observation)


def _budget_status(connection: sqlite3.Connection, policy: Mapping[str, Any], now: datetime) -> str | None:
    limits = policy["limits"]
    minute = _timestamp(now - timedelta(minutes=1))
    requests = int(connection.execute(
        """SELECT count(*) FROM secondary_assessment_attempts a
             JOIN secondary_assessment_jobs j ON j.job_id=a.job_id
             WHERE j.policy_id=? AND j.policy_digest=? AND a.created_at>=?""",
        (policy["policy_id"], policy["content_sha256"], minute),
    ).fetchone()[0])
    if requests >= int(limits["max_requests_per_minute"]):
        return "rate_limited"
    day = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    daily = int(connection.execute(
        """SELECT COALESCE(sum(a.cost_microunits),0) FROM secondary_assessment_attempts a
             JOIN secondary_assessment_jobs j ON j.job_id=a.job_id
             WHERE j.policy_id=? AND j.policy_digest=? AND substr(a.created_at,1,10)=?""",
        (policy["policy_id"], policy["content_sha256"], day),
    ).fetchone()[0])
    monthly = int(connection.execute(
        """SELECT COALESCE(sum(a.cost_microunits),0) FROM secondary_assessment_attempts a
             JOIN secondary_assessment_jobs j ON j.job_id=a.job_id
             WHERE j.policy_id=? AND j.policy_digest=? AND substr(a.created_at,1,7)=?""",
        (policy["policy_id"], policy["content_sha256"], month),
    ).fetchone()[0])
    if daily >= int(limits["daily_cost_limit_microunits"]) or monthly >= int(limits["monthly_cost_limit_microunits"]):
        return "budget_exhausted"
    return None


def run_assessment(
    connection: sqlite3.Connection,
    policy: Mapping[str, Any],
    entry: visual_registry.AlgorithmEntry,
    *,
    item_id: str,
    base_review_revision: int,
    image_bytes: bytes,
    image_pixels: int,
    image_sha256: str,
    idempotency_key: str,
    provider: Callable[[dict[str, Any], bytes], dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", idempotency_key):
        raise SecondaryRecognitionError("invalid assessment idempotency key")
    current_time = _now(now)
    runtime = ensure_policy_runtime(connection, policy, now=current_time)
    request = {
        "operation": "assess", "policy_id": policy["policy_id"], "policy_digest": policy["content_sha256"],
        "algorithm_key": entry.algorithm_key, "item_id": item_id, "base_review_revision": base_review_revision,
        "image_sha256": image_sha256,
    }
    fingerprint = _digest(request)
    receipt = connection.execute(
        "SELECT request_fingerprint,response_json FROM secondary_runtime_receipts WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if receipt is not None:
        if receipt[0] != fingerprint:
            raise RuntimeIdempotencyConflict("assessment idempotency key conflicts")
        response = json.loads(receipt[1]); response["replayed"] = True
        return response
    if runtime["runtime_state"] == "disabled":
        raise SecondaryRecognitionError("secondary runtime is disabled")
    if len(image_bytes) > int(policy["limits"]["max_image_bytes"]) or image_pixels > int(policy["limits"]["max_image_pixels"]):
        raise SecondaryRecognitionError("secondary image exceeds policy limits")
    if hashlib.sha256(image_bytes).hexdigest() != image_sha256:
        raise SecondaryRecognitionError("secondary image digest mismatch")
    job_id = f"secondary-job:{uuid.uuid4()}"
    timestamp = _timestamp(current_time)
    budget = _budget_status(connection, policy, current_time)
    initial_status = budget or "running"
    connection.execute(
        "INSERT INTO secondary_assessment_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, entry.algorithm_key, item_id, base_review_revision, image_sha256,
         policy["policy_id"], policy["policy_revision"], policy["content_sha256"],
         runtime["runtime_state"], initial_status, 0, None, timestamp, timestamp),
    )
    if budget:
        response = {"job_id": job_id, "status": budget, "attempt_count": 0, "observation_id": None, "replayed": False}
        connection.execute(
            "INSERT INTO secondary_runtime_receipts VALUES (?,?,?,?)",
            (idempotency_key, fingerprint, _canonical(response), timestamp),
        )
        return response
    observation: dict[str, Any] | None = None
    final_status = "failed"
    attempt_count = 0
    for attempt_number in range(1, int(policy["limits"]["max_attempts"]) + 1):
        attempt_count = attempt_number
        attempt_id = f"secondary-attempt:{uuid.uuid4()}"
        status = "failed"
        error_code = "provider_error"
        prediction_digest: str | None = None
        latency_ms = 0
        cost = 0
        provider_receipt = ""
        try:
            output = provider({"prediction_schema": policy["response_schema_version"], "policy_id": policy["policy_id"]}, image_bytes)
            if not isinstance(output, dict) or not isinstance(output.get("prediction"), dict):
                raise SecondaryRecognitionError("provider returned no prediction payload")
            latency_ms = int(output.get("latency_ms", 0))
            cost = int(output.get("cost_microunits", 0))
            provider_receipt = str(output.get("provider_receipt_ref", ""))
            if latency_ms < 0 or latency_ms > 3_600_000 or cost < 0:
                raise SecondaryRecognitionError("provider usage is outside bounds")
            if provider_receipt and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", provider_receipt):
                raise SecondaryRecognitionError("provider receipt is not a bounded opaque reference")
            prediction = output["prediction"]
            _validate_prediction(prediction, entry)
            prediction_digest = _digest(prediction)
            observation_id = f"secondary:{uuid.uuid4()}"
            observation = {
                "observation_id": observation_id,
                "observation_role": "secondary_multimodal",
                "provider_ref": policy["provider_ref"], "model_ref": policy["model_ref"],
                "task_profile_ref": entry.task_profile_ref,
                "task_profile_content_sha256": entry.task_profile_content_sha256,
                "inference_policy_digest": policy["inference_policy_digest"],
                "prompt_template_digest": policy["prompt_template_digest"],
                "inference_config_digest": policy["inference_config_digest"],
                "response_schema_version": policy["response_schema_version"],
                "image_sha256": image_sha256, "taxonomy_version_ref": entry.taxonomy_version_ref,
                "taxonomy_content_sha256": entry.taxonomy_content_sha256,
                "decision": prediction["decision"], "boxes": prediction["boxes"],
                "label_keys": prediction["label_keys"], "tag_assertions": prediction["tag_assertions"],
                "assessed_at": timestamp, "latency_ms": latency_ms,
            }
            if "confidence" in prediction:
                observation["confidence"] = prediction["confidence"]
            if provider_receipt:
                observation["provider_receipt_ref"] = provider_receipt
            evidence_ledger.validate_contract_definition(REVIEW_FACT_SCHEMA, "secondaryObservation", observation)
            canonical_observation = _canonical(observation)
            connection.execute(
                "INSERT INTO secondary_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (observation_id, job_id, entry.algorithm_key, item_id, base_review_revision,
                 image_sha256, policy["policy_id"], policy["content_sha256"], canonical_observation,
                 _digest(observation), timestamp),
            )
            status = "succeeded"; error_code = ""; final_status = "succeeded"
        except TimeoutError:
            status = "timeout"; error_code = "provider_timeout"
        except (SecondaryRecognitionError, evidence_ledger.EvidenceError, visual_registry.RegistryValidationError):
            status = "malformed"; error_code = "provider_prediction_invalid"
        except Exception:
            status = "failed"; error_code = "provider_error"
        connection.execute(
            "INSERT INTO secondary_assessment_attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (attempt_id, job_id, attempt_number, status, prediction_digest, latency_ms, cost,
             error_code, provider_receipt, timestamp),
        )
        if observation is not None:
            break
    connection.execute(
        "UPDATE secondary_assessment_jobs SET status=?,attempt_count=?,observation_id=?,completed_at=? WHERE job_id=?",
        (final_status, attempt_count, observation["observation_id"] if observation else None, timestamp, job_id),
    )
    response = {
        "job_id": job_id, "status": final_status, "attempt_count": attempt_count,
        "observation_id": observation["observation_id"] if observation else None, "replayed": False,
    }
    connection.execute(
        "INSERT INTO secondary_runtime_receipts VALUES (?,?,?,?)",
        (idempotency_key, fingerprint, _canonical(response), timestamp),
    )
    return response


def visible_observations(
    connection: sqlite3.Connection, algorithm_key: str, item_id: str, base_review_revision: int,
    *, human_assisted_only: bool = True,
) -> list[dict[str, Any]]:
    state_clause = "AND r.runtime_state='human_assisted'" if human_assisted_only else ""
    try:
        rows = connection.execute(
            f"""SELECT o.canonical_observation_json,o.observation_digest FROM secondary_observations o
                 JOIN secondary_policy_runtime r ON r.policy_id=o.policy_id AND r.policy_digest=o.policy_digest
                 WHERE o.algorithm_key=? AND o.item_id=? AND o.base_review_revision=? {state_clause}
                 ORDER BY o.created_at,o.observation_id LIMIT 8""",
            (algorithm_key, item_id, base_review_revision),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    result: list[dict[str, Any]] = []
    for row in rows:
        if hashlib.sha256(row[0].encode("utf-8")).hexdigest() != row[1]:
            raise RuntimeError("secondary observation digest mismatch")
        result.append(json.loads(row[0]))
    return result


def bindable_observations(
    connection: sqlite3.Connection,
    entry: visual_registry.AlgorithmEntry,
    item_id: str,
    base_review_revision: int,
    image_sha256: str,
) -> list[dict[str, Any]]:
    observations = visible_observations(
        connection, entry.algorithm_key, item_id, base_review_revision, human_assisted_only=True,
    )
    if len(observations) > entry.secondary_max_observations:
        raise SecondaryRecognitionError("secondary observation count exceeds the task profile")
    for observation in observations:
        bindings = {
            "observation_role": "secondary_multimodal",
            "task_profile_ref": entry.task_profile_ref,
            "task_profile_content_sha256": entry.task_profile_content_sha256,
            "taxonomy_version_ref": entry.taxonomy_version_ref,
            "taxonomy_content_sha256": entry.taxonomy_content_sha256,
            "image_sha256": image_sha256,
        }
        if any(observation.get(key) != value for key, value in bindings.items()):
            raise SecondaryRecognitionError("secondary observation binding drifted")
        evidence_ledger.validate_contract_definition(REVIEW_FACT_SCHEMA, "secondaryObservation", observation)
    return observations


def _iou(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    x1, y1 = max(float(left["x"]), float(right["x"])), max(float(left["y"]), float(right["y"]))
    x2 = min(float(left["x"]) + float(left["w"]), float(right["x"]) + float(right["w"]))
    y2 = min(float(left["y"]) + float(left["h"]), float(right["y"]) + float(right["h"]))
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = float(left["w"]) * float(left["h"]) + float(right["w"]) * float(right["h"]) - intersection
    return intersection / union if union > 0 else 0.0


def compare_to_human(
    observation: Mapping[str, Any], human_truth: Mapping[str, Any], entry: visual_registry.AlgorithmEntry,
) -> list[str]:
    errors: set[str] = set()
    human_decision = human_truth.get("decision")
    if human_decision == "unusable":
        return ["unusable"]
    if observation.get("decision") == "positive" and human_decision == "negative":
        errors.add("false_positive")
    if observation.get("decision") == "negative" and human_decision == "positive":
        errors.add("false_negative")
    if sorted(observation.get("label_keys", [])) != sorted(human_truth.get("label_keys", [])):
        errors.add("wrong_class")
    observed_tags = sorted(item["tag_key"] for item in observation.get("tag_assertions", []))
    if observed_tags != sorted(human_truth.get("tag_keys", [])):
        errors.add("tag_error")
    observed_boxes = observation.get("boxes", [])
    human_boxes = human_truth.get("boxes", [])
    if len(observed_boxes) != len(human_boxes):
        errors.add("geometry_error")
    else:
        unmatched = list(human_boxes)
        for observed in observed_boxes:
            candidates = [
                (index, target) for index, target in enumerate(unmatched)
                if target.get("label_key") == observed.get("label_key")
                and _iou(observed, target) >= entry.box_match_iou_threshold
            ]
            if not candidates:
                errors.add("geometry_error"); break
            unmatched.pop(candidates[0][0])
    return [error for error in ERROR_ORDER if error in errors]
