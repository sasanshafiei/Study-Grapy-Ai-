"""The pinned mastery model and the learner pseudonyms (no I/O, no database).

Nothing is accepted automatically: the protocol, model name, model version, state epoch
and pseudonym configuration are pinned in the environment. A capability probe or an update
response that differs is a mismatch that blocks delivery (app/mastery/delivery.py) and
hides stored predictions (app/dashboard/student.py) until an operator reconciles it.

Pseudonyms. The AI service never receives a user id or a course id, only
HMAC-SHA256(MASTERY_PSEUDONYM_KEY, "studygraph.mastery/1|<kind>|<namespace>|...") in hex.
MASTERY_PSEUDONYM_NAMESPACE separates deployments (staging and production pseudonyms of the
same learner differ even with a shared key). The key id stored with every prediction is
itself an HMAC under the key (it reveals nothing about the key); a key rotation changes it,
so predictions made under another key are never shown. The key and the learner ids are never
logged; reprs show "set"/"unset" only.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass, field

PROTOCOL = "studygraph.mastery/1"
MIN_KEY_LENGTH = 32
MODEL_FIELD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,99}$")
NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,62}$")
CANONICAL_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class Pins:
    model_name: str
    model_version: str
    state_epoch: uuid.UUID
    namespace: str
    _key: bytes = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"Pins(model={self.model_name}/{self.model_version}, epoch={self.state_epoch}, "
            f"namespace={self.namespace}, key=set)"
        )

    def _mac(self, *parts: str) -> str:
        message = "|".join((PROTOCOL, *parts)).encode("utf-8")
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    @property
    def key_id(self) -> str:
        return self._mac("key-id", self.namespace)[:16]

    def learner_ref(self, user_id: uuid.UUID, course_id: uuid.UUID) -> str:
        return self._mac("learner", self.namespace, str(user_id), str(course_id))

    def course_ref(self, course_id: uuid.UUID) -> str:
        return self._mac("course", self.namespace, str(course_id))


def _present(value: object) -> bool:
    return value is not None and (not isinstance(value, str) or value != "")


def validate_pins(settings) -> None:
    """Malformed pins are refused at start-up; missing ones only while delivery is enabled."""
    name, version = settings.mastery_model_name, settings.mastery_model_version
    epoch, namespace = settings.mastery_state_epoch, settings.mastery_pseudonym_namespace
    key = settings.mastery_pseudonym_key
    for label, value in (("MASTERY_MODEL_NAME", name), ("MASTERY_MODEL_VERSION", version)):
        if _present(value) and not MODEL_FIELD.match(value):
            raise ValueError(f"{label} must match {MODEL_FIELD.pattern}")
    if _present(epoch) and not CANONICAL_UUID.match(epoch):
        raise ValueError("MASTERY_STATE_EPOCH must be a lower-case canonical UUID")
    if _present(namespace) and not NAMESPACE.match(namespace):
        raise ValueError(f"MASTERY_PSEUDONYM_NAMESPACE must match {NAMESPACE.pattern}")
    if key is not None:
        secret = key.get_secret_value()
        if len(secret) < MIN_KEY_LENGTH or secret != secret.strip() or any(ch.isspace() for ch in secret):
            raise ValueError(f"MASTERY_PSEUDONYM_KEY must be at least {MIN_KEY_LENGTH} characters, without spaces")
    if settings.mastery_delivery_enabled:
        missing = [
            label
            for label, value in (
                ("MASTERY_MODEL_NAME", name), ("MASTERY_MODEL_VERSION", version), ("MASTERY_STATE_EPOCH", epoch),
                ("MASTERY_PSEUDONYM_NAMESPACE", namespace), ("MASTERY_PSEUDONYM_KEY", key),
            )
            if not _present(value)
        ]  # fmt: skip
        if missing:
            raise ValueError(f"{', '.join(missing)} must be set while MASTERY_DELIVERY_ENABLED is true")


def pins_of(settings) -> Pins | None:
    """The complete pin set, or None when any part is missing (then nothing is sent or shown)."""
    key = settings.mastery_pseudonym_key
    values = (
        settings.mastery_model_name, settings.mastery_model_version, settings.mastery_state_epoch,
        settings.mastery_pseudonym_namespace,
    )  # fmt: skip
    if not all(_present(v) for v in values) or key is None:
        return None
    return Pins(
        model_name=settings.mastery_model_name,
        model_version=settings.mastery_model_version,
        state_epoch=uuid.UUID(settings.mastery_state_epoch),
        namespace=settings.mastery_pseudonym_namespace,
        _key=key.get_secret_value().encode("utf-8"),
    )
