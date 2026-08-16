#!/usr/bin/env python3
"""Load the reviewed visual-task registry used by sample review.

The registry is content-addressed.  This module never invents digests, fetches
``latest`` over the network, or falls back to an embedded algorithm dictionary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_ROOT = REPOSITORY_ROOT / "platform" / "visual-task-registry"


class RegistryValidationError(RuntimeError):
    """The reviewed registry is incomplete, ambiguous, or digest-invalid."""


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_digest(payload: Mapping[str, Any]) -> str:
    stripped = {key: value for key, value in payload.items() if key != "content_sha256"}
    return hashlib.sha256(_canonical_json(stripped).encode("utf-8")).hexdigest()


def _load_json(path: Path, *, require_digest: bool = True) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryValidationError(f"cannot load {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RegistryValidationError(f"{path}: root must be an object")
    if require_digest:
        recorded = payload.get("content_sha256")
        actual = _content_digest(payload)
        if recorded != actual:
            raise RegistryValidationError(
                f"{path}: content digest mismatch ({recorded!r} != {actual})"
            )
    return payload


def _require(payload: Mapping[str, Any], path: Path, *keys: str) -> None:
    missing = [key for key in keys if key not in payload]
    if missing:
        raise RegistryValidationError(f"{path}: missing {', '.join(missing)}")


@dataclass(frozen=True)
class AlgorithmEntry:
    algorithm_key: str
    display_name: str
    task_type: str
    onboarding_state: str
    registry_updated_at: str
    visual_semantics_version_ref: str
    visual_semantics_content_sha256: str
    task_profile_ref: str
    task_profile_content_sha256: str
    taxonomy_version_ref: str
    taxonomy_content_sha256: str
    annotation_contract: str
    review_policy_ref: str
    review_policy_content_sha256: str
    review_group_key: str
    capture_stream_key: str
    source_mapping_ref: str
    source_mapping_content_sha256: str
    publication_policy_ref: str
    publication_policy_content_sha256: str
    max_snapshot_bytes: int
    min_eligible_changes: int
    label_aliases: Mapping[str, str]
    tag_keys: frozenset[str]
    bbox_min_area: float
    bbox_max_count: int
    box_match_iou_threshold: float
    canonical_duplicate_tiebreak: str
    secondary_allowed: bool
    secondary_max_observations: int
    secondary_policy_ref: str
    secondary_policy_content_sha256: str

    @property
    def accepted(self) -> bool:
        return self.onboarding_state == "accepted"

    def normalize_label(self, label: str) -> str:
        normalized = label.strip()
        key = self.label_aliases.get(normalized)
        if key is None:
            raise RegistryValidationError(
                f"algorithm {self.algorithm_key}: unknown taxonomy label {label!r}"
            )
        return key


def _docs_by_id(root: Path, directory: str, id_key: str) -> dict[str, tuple[Path, dict[str, Any]]]:
    result: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in sorted((root / directory).glob("*.json")):
        payload = _load_json(path)
        value = payload.get(id_key)
        if not isinstance(value, str) or not value:
            raise RegistryValidationError(f"{path}: invalid {id_key}")
        if value in result:
            raise RegistryValidationError(f"duplicate {id_key}: {value}")
        result[value] = (path, payload)
    return result


def _resolve(
    docs: Mapping[str, tuple[Path, dict[str, Any]]],
    ref: str,
    prefix: str,
    digest: str,
    owner: Path,
) -> tuple[Path, dict[str, Any]]:
    if not isinstance(ref, str) or not ref.startswith(prefix):
        raise RegistryValidationError(f"{owner}: invalid reference {ref!r}")
    key = ref.removeprefix(prefix)
    resolved = docs.get(key)
    if resolved is None:
        digest_matches = [
            candidate
            for candidate in docs.values()
            if candidate[1].get("content_sha256") == digest
        ]
        if len(digest_matches) != 1:
            raise RegistryValidationError(f"{owner}: unresolved reference {ref}")
        resolved = digest_matches[0]
    if resolved[1].get("content_sha256") != digest:
        raise RegistryValidationError(f"{owner}: digest mismatch for {ref}")
    return resolved


def _taxonomy_maps(path: Path, taxonomy: dict[str, Any]) -> tuple[dict[str, str], frozenset[str]]:
    entries = taxonomy.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RegistryValidationError(f"{path}: entries must be non-empty")
    aliases: dict[str, str] = {}
    tag_keys: set[str] = set()
    seen_keys: set[str] = set()
    parent_links: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RegistryValidationError(f"{path}: taxonomy entry must be an object")
        _require(entry, path, "key", "kind", "target_scope", "training_role", "status")
        key = entry["key"]
        kind = entry["kind"]
        if not isinstance(key, str) or key in seen_keys:
            raise RegistryValidationError(f"{path}: duplicate or invalid taxonomy key {key!r}")
        seen_keys.add(key)
        if kind == "class":
            if entry["training_role"] != "target":
                raise RegistryValidationError(f"{path}: class {key} must be a target")
            if key.startswith(("scene.", "quality.", "business.", "attribute.")):
                raise RegistryValidationError(f"{path}: class uses reserved tag namespace {key}")
            values = [key, *(entry.get("aliases") or [])]
            for value in values:
                if not isinstance(value, str) or not value or value in aliases:
                    raise RegistryValidationError(f"{path}: duplicate/invalid alias {value!r}")
                aliases[value] = key
        else:
            prefix = f"{kind}."
            if kind not in {"scene", "quality", "business", "attribute"} or not key.startswith(prefix):
                raise RegistryValidationError(f"{path}: invalid tag namespace {key}")
            if entry["training_role"] == "target" or entry.get("required_geometry", "none") != "none":
                raise RegistryValidationError(f"{path}: tag {key} cannot be a geometry target")
            if entry["target_scope"] not in {"image", "review"}:
                raise RegistryValidationError(f"{path}: v1 tag {key} has unsupported scope")
            tag_keys.add(key)
        parent = entry.get("parent_key")
        if parent:
            parent_links[key] = parent
    for key, parent in parent_links.items():
        if parent not in seen_keys:
            raise RegistryValidationError(f"{path}: missing parent {parent} for {key}")
        chain = {key}
        cursor = parent
        while cursor in parent_links:
            if cursor in chain:
                raise RegistryValidationError(f"{path}: taxonomy parent cycle at {cursor}")
            chain.add(cursor)
            cursor = parent_links[cursor]
    return aliases, frozenset(tag_keys)


def load_registry(root: Path = DEFAULT_REGISTRY_ROOT) -> dict[str, AlgorithmEntry]:
    algorithms = _docs_by_id(root, "algorithms", "algorithm_key")
    profiles = _docs_by_id(root, "profiles", "profile_id")
    taxonomies = _docs_by_id(root, "taxonomy", "taxonomy_version_id")
    semantics = _docs_by_id(root, "semantics", "bundle_id")
    source_mappings = _docs_by_id(root, "source-mappings", "mapping_id")
    review_policies = _docs_by_id(root, "review-policies", "policy_id")
    publication_policies = _docs_by_id(root, "publication-policies", "policy_id")
    secondary_policies = _docs_by_id(root, "secondary-policies", "policy_id")
    cost_models = _docs_by_id(root, "cost-models", "model_id")
    head_path = root / "accepted-head.json"
    head = _load_json(head_path, require_digest=False)
    accepted_head = head.get("accepted_entries")
    if not isinstance(accepted_head, dict):
        raise RegistryValidationError(f"{head_path}: accepted_entries must be an object")

    result: dict[str, AlgorithmEntry] = {}
    for algorithm_key, (algorithm_path, algorithm) in algorithms.items():
        _require(
            algorithm,
            algorithm_path,
            "schema_version",
            "display_name",
            "declared_task_family",
            "onboarding_state",
            "source_mapping_state",
            "publication_policy_ref",
            "publication_policy_content_sha256",
        )
        if algorithm["schema_version"] != "ai-bot-visual-algorithm-registry.v1":
            raise RegistryValidationError(f"{algorithm_path}: unsupported schema_version")
        onboarding = algorithm["onboarding_state"]
        if onboarding not in {"accepted", "catalog_only"}:
            raise RegistryValidationError(f"{algorithm_path}: invalid onboarding_state")
        policy_path, publication = _resolve(
            publication_policies,
            algorithm["publication_policy_ref"],
            "publication-policy:",
            algorithm["publication_policy_content_sha256"],
            algorithm_path,
        )
        _resolve(
            cost_models,
            publication["cost_model_ref"],
            "cost-model:",
            publication["cost_model_content_sha256"],
            policy_path,
        )
        if onboarding == "catalog_only":
            if algorithm_key in accepted_head:
                raise RegistryValidationError(f"{head_path}: catalog-only {algorithm_key} is accepted")
            result[algorithm_key] = AlgorithmEntry(
                algorithm_key=algorithm_key,
                display_name=algorithm["display_name"],
                task_type=algorithm["declared_task_family"],
                onboarding_state=onboarding,
                registry_updated_at=algorithm["created_at"],
                visual_semantics_version_ref="",
                visual_semantics_content_sha256="",
                task_profile_ref="",
                task_profile_content_sha256="",
                taxonomy_version_ref="",
                taxonomy_content_sha256="",
                annotation_contract="",
                review_policy_ref="",
                review_policy_content_sha256="",
                review_group_key="",
                capture_stream_key="",
                source_mapping_ref="",
                source_mapping_content_sha256="",
                publication_policy_ref=algorithm["publication_policy_ref"],
                publication_policy_content_sha256=algorithm["publication_policy_content_sha256"],
                max_snapshot_bytes=int(publication["max_snapshot_bytes"]),
                min_eligible_changes=int(publication["min_eligible_changes"]),
                label_aliases={},
                tag_keys=frozenset(),
                bbox_min_area=0.0,
                bbox_max_count=0,
                box_match_iou_threshold=0.0,
                canonical_duplicate_tiebreak="",
                secondary_allowed=False,
                secondary_max_observations=0,
                secondary_policy_ref="",
                secondary_policy_content_sha256="",
            )
            continue

        _require(
            algorithm,
            algorithm_path,
            "visual_semantics_bundle_ref",
            "visual_semantics_bundle_content_sha256",
            "source_mapping_ref",
            "source_mapping_content_sha256",
        )
        if algorithm.get("source_mapping_state") != "accepted":
            raise RegistryValidationError(f"{algorithm_path}: accepted algorithm lacks accepted mapping")
        head_entry = accepted_head.get(algorithm_key)
        if not isinstance(head_entry, dict):
            raise RegistryValidationError(f"{head_path}: missing accepted {algorithm_key}")
        if head_entry.get("ref") != f"algorithm:{algorithm_key}.v1" or head_entry.get(
            "content_sha256"
        ) != algorithm.get("content_sha256"):
            raise RegistryValidationError(f"{head_path}: accepted digest mismatch for {algorithm_key}")
        semantics_path, semantic = _resolve(
            semantics,
            algorithm["visual_semantics_bundle_ref"],
            "semantics:",
            algorithm["visual_semantics_bundle_content_sha256"],
            algorithm_path,
        )
        if semantic.get("algorithm_key") != algorithm_key:
            raise RegistryValidationError(f"{semantics_path}: algorithm mismatch")
        profile_path, profile = _resolve(
            profiles,
            semantic["task_profile_ref"],
            "profile:",
            semantic["task_profile_content_sha256"],
            semantics_path,
        )
        taxonomy_path, taxonomy = _resolve(
            taxonomies,
            semantic["taxonomy_version_ref"],
            "taxonomy:",
            semantic["taxonomy_content_sha256"],
            semantics_path,
        )
        review_path, review_policy = _resolve(
            review_policies,
            semantic["review_policy_ref"],
            "review-policy:",
            semantic["review_policy_content_sha256"],
            semantics_path,
        )
        if profile.get("algorithm_key") != algorithm_key:
            raise RegistryValidationError(f"{profile_path}: algorithm mismatch")
        if profile.get("taxonomy_version_ref") != semantic["taxonomy_version_ref"] or profile.get(
            "taxonomy_content_sha256"
        ) != semantic["taxonomy_content_sha256"]:
            raise RegistryValidationError(f"{profile_path}: taxonomy binding mismatch")
        if profile.get("review_policy_ref") != semantic["review_policy_ref"] or profile.get(
            "review_policy_content_sha256"
        ) != semantic["review_policy_content_sha256"]:
            raise RegistryValidationError(f"{profile_path}: review policy binding mismatch")
        if profile.get("annotation_contract") != semantic["annotation_contract"] or review_policy.get(
            "annotation_contract"
        ) != semantic["annotation_contract"]:
            raise RegistryValidationError(f"{semantics_path}: annotation contract mismatch")
        if review_policy.get("canonical_duplicate_tiebreak") != (
            "highest_review_revision_then_earliest_item_id"
        ):
            raise RegistryValidationError(f"{review_path}: unsupported duplicate tiebreak")
        secondary = profile.get("secondary_observation_contract")
        if not isinstance(secondary, dict) or (
            secondary.get("allowed") is False and secondary.get("max_observations_per_item") != 0
        ):
            raise RegistryValidationError(f"{profile_path}: invalid secondary observation contract")
        secondary_policy_ref = str(algorithm.get("secondary_recognition_policy_ref") or "")
        secondary_policy_digest = str(algorithm.get("secondary_recognition_policy_content_sha256") or "")
        if bool(secondary_policy_ref) != bool(secondary_policy_digest):
            raise RegistryValidationError(f"{algorithm_path}: incomplete secondary policy binding")
        if bool(secondary.get("allowed")) != bool(secondary_policy_ref):
            raise RegistryValidationError(f"{algorithm_path}: secondary profile/policy binding mismatch")
        if secondary_policy_ref:
            secondary_path, secondary_policy = _resolve(
                secondary_policies,
                secondary_policy_ref,
                "secondary-policy:",
                secondary_policy_digest,
                algorithm_path,
            )
            if secondary_policy.get("algorithm_key") != algorithm_key:
                raise RegistryValidationError(f"{secondary_path}: algorithm mismatch")
            secondary_bindings = {
                "visual_semantics_bundle_ref": semantic["bundle_id"],
                "visual_semantics_bundle_content_sha256": semantic["content_sha256"],
                "task_profile_ref": profile["profile_id"],
                "task_profile_content_sha256": profile["content_sha256"],
                "taxonomy_version_ref": taxonomy["taxonomy_version_id"],
                "taxonomy_content_sha256": taxonomy["content_sha256"],
                "annotation_contract": semantic["annotation_contract"],
            }
            if any(secondary_policy.get(key) != value for key, value in secondary_bindings.items()):
                raise RegistryValidationError(f"{secondary_path}: semantic binding mismatch")
            if secondary_policy.get("runtime_initial_state") != "disabled":
                raise RegistryValidationError(f"{secondary_path}: runtime must start disabled")
        mapping_path, mapping = _resolve(
            source_mappings,
            algorithm["source_mapping_ref"],
            "source-mapping:",
            algorithm["source_mapping_content_sha256"],
            algorithm_path,
        )
        if mapping.get("algorithm_key") != algorithm_key:
            raise RegistryValidationError(f"{mapping_path}: algorithm mismatch")
        label_aliases, tag_keys = _taxonomy_maps(taxonomy_path, taxonomy)
        result[algorithm_key] = AlgorithmEntry(
            algorithm_key=algorithm_key,
            display_name=algorithm["display_name"],
            task_type=profile["task_type"],
            onboarding_state=onboarding,
            registry_updated_at=algorithm["created_at"],
            visual_semantics_version_ref=semantic["bundle_id"],
            visual_semantics_content_sha256=semantic["content_sha256"],
            task_profile_ref=profile["profile_id"],
            task_profile_content_sha256=profile["content_sha256"],
            taxonomy_version_ref=taxonomy["taxonomy_version_id"],
            taxonomy_content_sha256=taxonomy["content_sha256"],
            annotation_contract=semantic["annotation_contract"],
            review_policy_ref=review_policy["policy_id"],
            review_policy_content_sha256=review_policy["content_sha256"],
            review_group_key=mapping["review_group_key"],
            capture_stream_key=mapping["capture_stream_key"],
            source_mapping_ref=algorithm["source_mapping_ref"],
            source_mapping_content_sha256=algorithm["source_mapping_content_sha256"],
            publication_policy_ref=algorithm["publication_policy_ref"],
            publication_policy_content_sha256=algorithm["publication_policy_content_sha256"],
            max_snapshot_bytes=int(publication["max_snapshot_bytes"]),
            min_eligible_changes=int(publication["min_eligible_changes"]),
            label_aliases=label_aliases,
            tag_keys=tag_keys,
            bbox_min_area=float(review_policy["bbox_min_area"]),
            bbox_max_count=int(review_policy["bbox_max_count"]),
            box_match_iou_threshold=float(review_policy["box_match_iou_threshold"]),
            canonical_duplicate_tiebreak=str(review_policy["canonical_duplicate_tiebreak"]),
            secondary_allowed=bool(secondary["allowed"]),
            secondary_max_observations=int(secondary["max_observations_per_item"]),
            secondary_policy_ref=secondary_policy_ref,
            secondary_policy_content_sha256=secondary_policy_digest,
        )
    if set(accepted_head) != {key for key, entry in result.items() if entry.accepted}:
        raise RegistryValidationError(f"{head_path}: accepted set does not match algorithm entries")
    return result


def load_default_registry() -> dict[str, AlgorithmEntry]:
    return load_registry(DEFAULT_REGISTRY_ROOT)


def accepted_algorithms() -> dict[str, AlgorithmEntry]:
    return {key: entry for key, entry in load_default_registry().items() if entry.accepted}


def export_visual_semantics(algorithm_key: str) -> dict[str, Any]:
    """Return the exact reviewed semantic documents for one accepted algorithm."""
    entry = accepted_algorithms().get(algorithm_key)
    if entry is None:
        raise KeyError("algorithm is not accepted")

    def by_digest(directory: str, digest: str) -> dict[str, Any]:
        matches = [
            payload
            for path in sorted((DEFAULT_REGISTRY_ROOT / directory).glob("*.json"))
            if (payload := _load_json(path)).get("content_sha256") == digest
        ]
        if len(matches) != 1:
            raise RegistryValidationError(
                f"algorithm {algorithm_key}: unresolved {directory} digest"
            )
        return matches[0]

    return {
        "algorithm_key": algorithm_key,
        "visual_semantics": by_digest(
            "semantics", entry.visual_semantics_content_sha256
        ),
        "task_profile": by_digest("profiles", entry.task_profile_content_sha256),
        "taxonomy": by_digest("taxonomy", entry.taxonomy_content_sha256),
        "review_policy": by_digest(
            "review-policies", entry.review_policy_content_sha256
        ),
    }


def legacy_algorithm_for(row: Mapping[str, Any]) -> str:
    source_kind = str(row["source_kind"] or "").strip()
    normalized = source_kind.removeprefix("upload-")
    matches = [
        key
        for key, entry in accepted_algorithms().items()
        if normalized in {entry.review_group_key, entry.capture_stream_key}
    ]
    return matches[0] if len(matches) == 1 else ""
