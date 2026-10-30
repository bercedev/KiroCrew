"""The workflow_run opt-in fallback title/folder plumbing.

The MCP tool forwards the caller's ``title`` and ``folder`` into the run body
unchanged (no dashboard client); the ``/api/workflows/run`` handler resolves the
folder to an EXISTING folder id server-side through the one read-only resolver
(``mcp_dashboard_references._resolve_chat_folder_id``). Nothing is created — a
missing or ambiguous folder rejects the run with a caller-visible 400.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def _run_wf(args: dict) -> dict:
    """Call the workflow_run MCP handler with identity + _post stubbed, returning
    the body it POSTed to /api/workflows/run."""
    from kiro_crew.mcp_tools import workflows

    captured: dict = {}

    def _fake_post(path, body=None, **kw):
        captured["path"] = path
        captured["body"] = body
        return {"run_id": "wf_x", "name": body.get("name", "") if body else ""}

    with (
        patch.object(workflows, "_workflow_identity", return_value=("dashboard:chat-1", "")),
        patch.object(workflows.mcp_core, "_post", side_effect=_fake_post),
    ):
        workflows.workflow_run("workflow_run", args)
    return captured.get("body") or {}


def test_mcp_forwards_trimmed_title_raw() -> None:
    body = _run_wf({"source": "def workflow(ctx):\n    return 1", "title": "  MR review  "})
    assert body["title"] == "MR review"


def test_mcp_forwards_folder_ref_raw() -> None:
    body = _run_wf({"source": "def workflow(ctx):\n    return 1", "folder": "MR Reviews"})
    # Forwarded unchanged under ``folder`` for the server to resolve; the MCP
    # tool never resolves and never sets ``folder_id``.
    assert body["folder"] == "MR Reviews"
    assert "folder_id" not in body


def test_mcp_omits_absent_title_and_folder() -> None:
    body = _run_wf({"source": "def workflow(ctx):\n    return 1"})
    assert "title" not in body and "folder" not in body


# --- server-side folder resolution (existing folders only) -------------------


class _FakeFolderState:
    def __init__(self, folders=None) -> None:
        self._folders = list(folders or [])


class _FakeRequest:
    def __init__(self, state) -> None:
        self.app = {"state": state}


@pytest.mark.asyncio
async def test_resolver_empty_ref_files_nothing() -> None:
    # No folder given -> ("", None): the fallback stays unfiled (today's default).
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    fid, refusal = await _resolve_fallback_folder_id(_FakeRequest(_FakeFolderState()), "")
    assert (fid, refusal) == ("", None)


@pytest.mark.asyncio
async def test_resolver_existing_id_files_the_session() -> None:
    # An existing folder id resolves to itself.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState([{"id": "0123456789ab", "name": "MR"}]))
    fid, refusal = await _resolve_fallback_folder_id(req, "0123456789ab")
    assert (fid, refusal) == ("0123456789ab", None)


@pytest.mark.asyncio
async def test_resolver_existing_path_files_the_session() -> None:
    # An existing human path resolves to the folder's id — nothing created.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    folders = [
        {"id": "id-a", "name": "A", "parent_id": ""},
        {"id": "id-b", "name": "B", "parent_id": "id-a"},
    ]
    req = _FakeRequest(_FakeFolderState(folders))
    fid, refusal = await _resolve_fallback_folder_id(req, "A/B")
    assert (fid, refusal) == ("id-b", None)
    # The folder list is unchanged: resolution never writes.
    assert len(req.app["state"]._folders) == 2


@pytest.mark.asyncio
async def test_resolver_missing_folder_gives_caller_visible_400() -> None:
    # A folder that does not exist is refused with a 400, not silently dropped
    # and not created.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState([{"id": "id-a", "name": "A", "parent_id": ""}]))
    fid, refusal = await _resolve_fallback_folder_id(req, "Nope")
    assert fid == ""
    assert refusal is not None and refusal.status == 400
    assert len(req.app["state"]._folders) == 1  # nothing created


@pytest.mark.asyncio
async def test_resolver_unknown_id_gives_400() -> None:
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState())
    fid, refusal = await _resolve_fallback_folder_id(req, "0123456789ab")
    assert fid == ""
    assert refusal is not None and refusal.status == 400


@pytest.mark.asyncio
async def test_resolver_ambiguous_path_gives_400() -> None:
    # Two folders rendering the same path ``A`` is refused by the canonical
    # resolver's ambiguity check (pass the id instead) -> a caller-visible 400.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    folders = [
        {"id": "id-a1", "name": "A", "parent_id": ""},
        {"id": "id-a2", "name": "A", "parent_id": ""},
    ]
    req = _FakeRequest(_FakeFolderState(folders))
    fid, refusal = await _resolve_fallback_folder_id(req, "A")
    assert fid == ""
    assert refusal is not None and refusal.status == 400


# --- mate-home refusal (a fallback slot never files into a member's home) ----


def _mate_home_folders() -> list[dict]:
    """A member's home folder and a child folder inside it."""
    return [
        {
            "id": "home-1",
            "name": "Home",
            "parent_id": "",
            "mate_home": True,
            "owner_app": "member:store-x",
        },
        {"id": "child-1", "name": "Inside", "parent_id": "home-1"},
    ]


@pytest.mark.asyncio
async def test_resolver_refuses_mate_home_folder_by_id() -> None:
    # A member's home folder is refused with the shared 403; nothing is created.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState(_mate_home_folders()))
    fid, refusal = await _resolve_fallback_folder_id(req, "home-1")
    assert fid == ""
    assert refusal is not None and refusal.status == 403
    body = (
        refusal.body.decode() if isinstance(refusal.body, (bytes, bytearray)) else str(refusal.body)
    )
    assert "mate_home_locked" in body
    assert len(req.app["state"]._folders) == 2  # nothing created


@pytest.mark.asyncio
async def test_resolver_refuses_folder_inside_mate_home_by_id() -> None:
    # A folder nested inside a member's home is refused the same way.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState(_mate_home_folders()))
    fid, refusal = await _resolve_fallback_folder_id(req, "child-1")
    assert fid == ""
    assert refusal is not None and refusal.status == 403
    body = (
        refusal.body.decode() if isinstance(refusal.body, (bytes, bytearray)) else str(refusal.body)
    )
    assert "mate_home_locked" in body


@pytest.mark.asyncio
async def test_resolver_refuses_mate_home_folder_by_path() -> None:
    # The same refusal when the member's home is named by its human path.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState(_mate_home_folders()))
    fid, refusal = await _resolve_fallback_folder_id(req, "Home")
    assert fid == ""
    assert refusal is not None and refusal.status == 403


@pytest.mark.asyncio
async def test_resolver_refuses_folder_inside_mate_home_by_path() -> None:
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState(_mate_home_folders()))
    fid, refusal = await _resolve_fallback_folder_id(req, "Home/Inside")
    assert fid == ""
    assert refusal is not None and refusal.status == 403
