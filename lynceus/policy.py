"""Typed input contract. Protection travels with every immutable revision."""
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Principal(Strict):
    type: Literal["public", "group", "workspace_members", "user"]
    id: str | None = None

    @model_validator(mode="after")
    def scoped(self):
        if self.type != "public" and not self.id:
            raise ValueError("A non-public audience needs a registered ID")
        if self.type == "public" and self.id is not None:
            raise ValueError("Public audience has no ID")
        return self


class Requirement(Strict):
    type: Literal["agreement"] = "agreement"
    id: str = Field(min_length=1, max_length=200)


class Audience(Strict):
    any_of: list[Principal] = Field(min_length=1, max_length=50)


class Requirements(Strict):
    all_of: list[Requirement] = Field(default_factory=list, max_length=50)


class Protection(Strict):
    classification: Literal["public", "internal", "restricted"]
    audience: Audience
    requirements: Requirements = Field(default_factory=Requirements)

    @model_validator(mode="after")
    def consistent(self):
        public = any(p.type == "public" for p in self.audience.any_of)
        if self.classification == "public":
            if len(self.audience.any_of) != 1 or not public or self.requirements.all_of:
                raise ValueError("Public content must have a public audience and no restrictions")
        elif public:
            raise ValueError("Internal and restricted content need a scoped audience")
        if self.classification == "restricted" and not self.requirements.all_of:
            raise ValueError("Restricted content needs an explicit agreement requirement")
        if self.classification == "internal" and self.requirements.all_of:
            raise ValueError("Use restricted classification when an agreement is required")
        return self


class Source(Strict):
    uri: str = Field(min_length=1, max_length=4096)
    credential_ref: str | None = None

    @model_validator(mode="after")
    def location(self):
        u = urlsplit(self.uri)
        if u.scheme not in {"file", "http", "https"}:
            raise ValueError("Use an absolute file://, http:// or https:// URI")
        if u.username or u.password:
            raise ValueError("Store credentials outside the source URI")
        if u.scheme == "file" and (u.netloc not in ("", "localhost") or not u.path.startswith("/")):
            raise ValueError("Local file URIs must identify an absolute path on this worker")
        if u.scheme in {"http", "https"} and not u.hostname:
            raise ValueError("HTTP source needs a hostname")
        return self


class InputRecord(Strict):
    schema_version: Literal[1] = 1
    document_type: Literal["pdf"] = "pdf"
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    source: Source
    protection: Protection
    processing: Literal["local_only"] = "local_only"


def public_protection():
    return {"classification": "public", "audience": {"any_of": [{"type": "public"}]}, "requirements": {"all_of": []}}


def can_access(policy, user):
    p = Protection.model_validate(policy)
    if p.classification == "public":
        return True
    if not user:
        return False
    match = any((a.type == "group" and a.id in user.get("groups", [])) or (a.type == "workspace_members" and a.id in user.get("workspaces", [])) or (a.type == "user" and a.id == user["id"]) for a in p.audience.any_of)
    return match and all(r.id in user.get("agreements", []) for r in p.requirements.all_of)
