"""S45-15：冻结 Snapshot 的 Score 身份与 dispatch generation 解耦。"""

import hashlib
import json

import pytest
from app import langfuse_projection


def score_id(item_id="item-1", snapshot_id="snapshot-1", binding_id="binding-1"):
    return langfuse_projection.trajectory_score_id(
        item_id=item_id, snapshot_id=snapshot_id, binding_id=binding_id,
    )


def test_trajectory_projection_key_uses_the_frozen_canonical_scheme():
    payload = {"item_id": "item-1", "snapshot_id": "snapshot-1", "binding_id": "binding-1"}
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert score_id() == f"score:trajectory-v1:{expected}"
    assert score_id() == score_id()


@pytest.mark.parametrize("dimension", ["item_id", "snapshot_id", "binding_id"])
def test_each_frozen_identity_dimension_isolates_a_projection(dimension):
    assert score_id(**{dimension: "new-identity"}) != score_id()


def test_trajectory_key_cannot_collide_when_identifiers_contain_delimiters():
    assert score_id("a:b", "c", "d") != score_id("a", "b:c", "d")
    assert score_id("a", "b:c", "d") != score_id("a", "b", "c:d")


@pytest.mark.parametrize("dimension", ["item_id", "snapshot_id", "binding_id"])
@pytest.mark.parametrize("invalid", [None, "", " ", 7, True])
def test_trajectory_key_rejects_missing_or_non_string_identity(dimension, invalid):
    with pytest.raises(ValueError, match="TRAJECTORY_PROJECTION_IDENTITY_INVALID"):
        score_id(**{dimension: invalid})


def test_legacy_score_identity_is_not_rewritten():
    assert langfuse_projection.stable_score_id(
        item_id="item-1", generation=3, evaluator_id="intent_match",
    ) == "score:item-1:gen3:intent_match"
