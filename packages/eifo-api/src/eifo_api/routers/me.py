"""The signed-in user's own data.

Every route here is scoped to ``principal.user`` - no handler takes a user id
from the request, so there is no object for a caller to walk sideways through.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from eifo_api.converters import hydrate_titles, to_user, to_user_item
from eifo_api.deps import CsrfDep, PrincipalDep, SessionDep
from eifo_api.schemas import (
    ItemUpsert,
    MeResponse,
    Page,
    ProfilePatch,
    UserItemOut,
    UserOut,
)
from eifo_core.enums import ItemStatus
from eifo_core.models import Source, Title, User, UserItem
from eifo_core.types import utcnow

router = APIRouter(tags=["user"])

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 24


@router.get("/me", response_model=MeResponse, summary="The signed-in user")
def read_me(principal: PrincipalDep) -> MeResponse:
    """Who you are, and the CSRF token to write with."""
    return MeResponse(
        user=to_user(principal.user),
        csrf_token=principal.csrf_token,
        is_admin=principal.is_admin,
    )


@router.patch("/me", response_model=UserOut, summary="Update your profile")
def update_me(
    patch: ProfilePatch,
    principal: PrincipalDep,
    _csrf: CsrfDep,
    session: SessionDep,
) -> UserOut:
    """Apply the supplied fields; anything omitted is left as it was."""
    user = principal.user
    changes = patch.model_dump(exclude_unset=True)

    if "display_name" in changes:
        user.display_name = _clean_display_name(changes["display_name"])
    if "handle" in changes:
        user.handle = _claim_handle(session, user, changes["handle"])
    if "my_source_ids" in changes:
        user.my_source_ids = _known_source_ids(session, changes["my_source_ids"])
    if "is_public" in changes:
        user.is_public = _resolve_visibility(user, changes["is_public"])

    session.commit()
    return to_user(user)


@router.delete("/me", status_code=204, summary="Delete your account")
def delete_me(principal: PrincipalDep, _csrf: CsrfDep, session: SessionDep) -> Response:
    """Immediate hard delete of the user, their sessions and their items.

    No soft-delete and no grace period: this is the user's own data, not
    catalog data we might want back (docs.internal/09-auth-privacy.md).
    """
    session.delete(principal.user)
    session.commit()
    return Response(status_code=204)


@router.get("/me/items", response_model=Page[UserItemOut], summary="Your lists")
def list_my_items(
    principal: PrincipalDep,
    session: SessionDep,
    status: Annotated[ItemStatus | None, Query()] = None,
    rated: Annotated[bool | None, Query(description="Only titles you have rated")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> Page[UserItemOut]:
    """Your entries, newest first, each carrying the title card to render it."""
    filtered = select(UserItem).where(UserItem.user_id == principal.user.id)
    if status is not None:
        filtered = filtered.where(UserItem.status == status)
    if rated:
        filtered = filtered.where(UserItem.rating.is_not(None))

    total = session.scalar(select(func.count()).select_from(filtered.subquery())) or 0
    items = list(
        session.scalars(
            filtered.order_by(UserItem.updated_at.desc(), UserItem.title_id)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    )

    titles = {
        title.id: title for title in hydrate_titles(session, [item.title_id for item in items])
    }

    return Page(
        items=[to_user_item(item, title=titles.get(item.title_id)) for item in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.put("/me/items/{title_id}", response_model=UserItemOut, summary="Add or update an entry")
def upsert_my_item(
    title_id: int,
    body: ItemUpsert,
    principal: PrincipalDep,
    _csrf: CsrfDep,
    session: SessionDep,
) -> UserItemOut:
    """Set list membership, rating or note on one title.

    Partial: only the fields present in the body change, and an explicit null
    clears one. Clearing the last of them removes the row rather than leaving
    an entry that says nothing.
    """
    if session.get(Title, title_id) is None:
        raise HTTPException(status_code=404, detail=f"No title with id {title_id}")

    item = _find_item(session, principal.user, title_id)
    if item is None:
        item = UserItem(user_id=principal.user.id, title_id=title_id)
        session.add(item)

    changes = body.model_dump(exclude_unset=True)
    if "status" in changes:
        item.status = changes["status"]
    if "rating" in changes:
        item.rating = changes["rating"]
    if "note" in changes:
        item.note = _clean_note(changes["note"])

    if item.is_empty:
        session.delete(item)
        session.commit()
        return UserItemOut(title_id=title_id, updated_at=utcnow())

    session.commit()
    return to_user_item(item)


@router.delete("/me/items/{title_id}", status_code=204, summary="Remove an entry")
def delete_my_item(
    title_id: int,
    principal: PrincipalDep,
    _csrf: CsrfDep,
    session: SessionDep,
) -> Response:
    """Remove a title from your lists entirely, rating and note included."""
    item = _find_item(session, principal.user, title_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Title {title_id} is not in your lists")

    session.delete(item)
    session.commit()
    return Response(status_code=204)


def _find_item(session: Session, user: User, title_id: int) -> UserItem | None:
    return session.scalars(
        select(UserItem).where(UserItem.user_id == user.id, UserItem.title_id == title_id)
    ).one_or_none()


def _clean_display_name(value: str | None) -> str:
    name = (value or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="A display name cannot be blank.")
    return name


def _clean_note(value: str | None) -> str | None:
    """Whitespace-only is the same as no note at all."""
    note = (value or "").strip()
    return note or None


def _claim_handle(session: Session, user: User, handle: str | None) -> str | None:
    """Take a handle, unless another account already has it."""
    if handle is None:
        if user.is_public:
            raise HTTPException(
                status_code=422, detail="A public profile needs a handle. Make it private first."
            )
        return None

    taken = session.scalars(
        select(User.id).where(User.handle == handle, User.id != user.id)
    ).one_or_none()
    if taken is not None:
        raise HTTPException(status_code=409, detail=f"The handle {handle!r} is taken.")

    return handle


def _resolve_visibility(user: User, is_public: bool | None) -> bool:
    """Going public requires a handle - that is what the profile URL is."""
    if is_public and not user.handle:
        raise HTTPException(
            status_code=422, detail="Choose a handle before making your profile public."
        )
    return bool(is_public)


def _known_source_ids(session: Session, source_ids: list[int] | None) -> list[int]:
    """Keep the preset honest: only ids of sources that exist, deduplicated.

    A preset naming a source that was never tracked would silently filter the
    catalog down to nothing, which reads as a broken app rather than a stale
    setting.
    """
    wanted = list(dict.fromkeys(source_ids or []))
    if not wanted:
        return []

    known = set(session.scalars(select(Source.id).where(Source.id.in_(wanted))))
    unknown = [source_id for source_id in wanted if source_id not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown source ids: {unknown}")

    return wanted
