from __future__ import annotations

import pytest
from pydantic import ValidationError

from control_plane.applications import ApplicationInput, ApplicationService
from control_plane.errors import ApiError
from tests.conftest import World

IMAGE = "registry.example.com/foxtrot/goods@sha256:" + "a" * 64


async def test_image_crud_owner_version_and_archive(world: World) -> None:
    service = ApplicationService(world.repo, world.clock)
    body = ApplicationInput(name="goods", image={"imageRef": IMAGE})
    app = await service.create("alice", body)
    assert app.version == 1
    assert await service.list("bob") == []
    with pytest.raises(ApiError, match="no application"):
        await service.get("bob", app.id)
    with pytest.raises(ApiError, match="latest"):
        await service.update("alice", app.id, body, 4)
    updated = await service.update("alice", app.id, body, 1)
    assert updated.version == 2
    await service.archive("alice", app.id, 2)
    assert await service.list("alice") == []


def test_tags_and_embedded_registry_credentials_are_refused() -> None:
    for image in ("goods:latest", "user:password@registry/app@sha256:" + "a" * 64):
        with pytest.raises(ValidationError):
            ApplicationInput(name="bad", image={"imageRef": image})
