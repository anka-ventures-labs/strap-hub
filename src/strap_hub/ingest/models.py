"""Wire models for digest schema 1."""

from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, FiniteFloat, ValidationError,
    ValidatorFunctionWrapHandler, field_validator,
    model_validator,
)


def validate_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def normalize_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.isoformat().replace("+00:00", "Z")


def validate_utf8(value: str) -> str:
    value.encode("utf-8")
    return value


Utf8String = Annotated[str, AfterValidator(validate_utf8)]
LocalDate = Annotated[
    str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(validate_date)
]
Timestamp = Annotated[
    str,
    Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}"
        r"T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|\+00:00)$"
    ),
    AfterValidator(normalize_timestamp),
]
SQLiteInt = Annotated[int, Field(ge=-(2**63), lt=2**63)]
ZoneMinutes = dict[Literal["z1", "z2", "z3", "z4", "z5"], FiniteFloat]


class PayloadModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Source(PayloadModel):
    app: Literal["strap-edge"]
    app_version: Utf8String
    algo_version: Annotated[SQLiteInt, Field(ge=0)]
    device_family: Utf8String


class Freshness(PayloadModel):
    # Null before the phone has stored any band record.
    last_band_record_at: Timestamp | None


class Day(PayloadModel):
    date: LocalDate
    metrics: dict[Utf8String, FiniteFloat]


class Segment(PayloadModel):
    start: Timestamp
    end: Timestamp
    stage: Literal["wake", "light", "deep", "rem", "unobserved"]

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if datetime.fromisoformat(self.start) >= datetime.fromisoformat(self.end):
            raise ValueError("segment end must be after start")
        return self


class Night(PayloadModel):
    date: LocalDate
    segments: list[Segment]

    @model_validator(mode="after")
    def validate_unique_starts(self) -> Self:
        if len({segment.start for segment in self.segments}) != len(self.segments):
            raise ValueError("segment starts must be unique within a night")
        return self


class Workout(PayloadModel):
    id: Utf8String
    date: LocalDate
    start: Timestamp
    end: Timestamp | None = None
    type: Utf8String
    duration_min: FiniteFloat | None = None
    strain: FiniteFloat | None = None
    calories: FiniteFloat | None = None
    max_hr: FiniteFloat | None = None
    hrr_bpm: FiniteFloat | None = None
    steps: SQLiteInt | None = None
    zone_min: ZoneMinutes | None = None

    @field_validator("zone_min", mode="wrap")
    @classmethod
    def omit_invalid_zones(cls, value: Any, validate: ValidatorFunctionWrapHandler) -> ZoneMinutes | None:
        try:
            return validate(value)
        except ValidationError:
            return None


class Digest(PayloadModel):
    schema_version: Annotated[int, Field(alias="schema", ge=1, le=1)]
    sent_at: Timestamp
    source: Source
    freshness: Freshness
    days: list[Day]
    nights: list[Night]
    workouts: list[Workout]
    revoked_workout_ids: list[Utf8String] = Field(default_factory=list)
    # Per-install, strictly increasing digest number (CONTRACT section 3). Optional for
    # older senders; without it a push can never clear a privacy tombstone.
    seq: Annotated[int, Field(ge=1)] | None = None
