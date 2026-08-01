"""Explicit request and response schemas for hosted endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from .security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, normalized_email

ROLE_IDS = {
    "software_engineering",
    "machine_learning_ai",
    "data_science",
    "data_engineering",
    "quantitative_development",
    "product_management",
    "hardware_embedded",
    "other_engineering",
}
ALERT_FREQUENCIES = {"as_detected", "three_hour", "daily", "paused"}

TrimmedEmail = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=320)
]
Password = Annotated[
    str,
    StringConstraints(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH),
]
Token = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=32, max_length=512)
]


class AuthCredentials(BaseModel):
    email: TrimmedEmail
    password: Password

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalized_email(value)[0]


class EmailRequest(BaseModel):
    email: TrimmedEmail

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalized_email(value)[0]


class TokenRequest(BaseModel):
    token: Token


class ResetPasswordRequest(TokenRequest):
    password: Password


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    email_verified: bool
    created_at: datetime
    last_successful_scan_at: datetime | None = None


class AuthResponse(BaseModel):
    user: UserResponse
    verification_email_sent: bool | None = None


class AcceptedResponse(BaseModel):
    accepted: bool = True


class CompanyResponse(BaseModel):
    id: str
    name: str
    aliases: list[str]
    coverage: Literal["direct", "backstop", "delayed"]
    selectable: bool


class PreferencesBase(BaseModel):
    role_ids: list[str]
    preferred_locations: list[str]
    include_remote: bool
    internship_season: str
    alert_frequency: str
    globally_paused: bool

    @field_validator("role_ids")
    @classmethod
    def valid_roles(cls, values: list[str]) -> list[str]:
        if not 1 <= len(values) <= len(ROLE_IDS):
            raise ValueError("Select between 1 and 8 role categories.")
        if len(values) != len(set(values)):
            raise ValueError("role_ids must not contain duplicates.")
        unsupported = sorted(set(values) - ROLE_IDS)
        if unsupported:
            raise ValueError("Unsupported role ID.")
        return values

    @field_validator("preferred_locations")
    @classmethod
    def valid_locations(cls, values: list[str]) -> list[str]:
        if len(values) > 20:
            raise ValueError("At most 20 preferred locations are allowed.")
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 120 for value in cleaned):
            raise ValueError("Locations must contain between 1 and 120 characters.")
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("preferred_locations must not contain duplicates.")
        return cleaned

    @field_validator("internship_season")
    @classmethod
    def valid_season(cls, value: str) -> str:
        cleaned = value.strip()
        if not 1 <= len(cleaned) <= 80:
            raise ValueError(
                "internship_season must contain between 1 and 80 characters."
            )
        return cleaned

    @field_validator("alert_frequency")
    @classmethod
    def valid_frequency(cls, value: str) -> str:
        if value not in ALERT_FREQUENCIES:
            raise ValueError("Unsupported alert frequency.")
        return value


class PreferencesUpdate(PreferencesBase):
    pass


class PreferencesResponse(PreferencesBase):
    created_at: datetime
    updated_at: datetime


class CompanyWatchInput(BaseModel):
    company_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ]
    paused: bool = False


class WatchlistUpdate(BaseModel):
    companies: list[CompanyWatchInput] = Field(max_length=200)

    @field_validator("companies")
    @classmethod
    def unique_companies(
        cls, values: list[CompanyWatchInput]
    ) -> list[CompanyWatchInput]:
        ids = [value.company_id for value in values]
        if len(ids) != len(set(ids)):
            raise ValueError("companies must not contain duplicate company IDs.")
        return values


class CompanyWatchResponse(BaseModel):
    company_id: str
    paused: bool
    created_at: datetime
    updated_at: datetime


class CompanyRequestInput(BaseModel):
    company_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    career_url: AnyHttpUrl | None = None

    @field_validator("career_url")
    @classmethod
    def safe_career_url(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is None:
            return None
        parsed = urlsplit(str(value))
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("career_url must not contain credentials.")
        if len(str(value)) > 2048:
            raise ValueError("career_url is too long.")
        return value


class CompanyRequestResponse(BaseModel):
    id: uuid.UUID
    company_name: str
    career_url: str | None
    status: Literal["received"]
    created_at: datetime
