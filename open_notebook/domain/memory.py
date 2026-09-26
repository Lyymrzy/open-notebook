"""AI memory plane: atomic memory cards and the slow-changing core profile.

This module is the "Plane M" of the two-plane model documented in migration 25:

* The user knowledge tree (Plane U) lives in `open_notebook/domain/notebook.py`
  and is authoritative: only the user writes there.
* Memory items are the AI's own recall layer. They are atomic, carry their
  provenance (via the `derived_from` edge) and can evolve - a newer memory can
  contradict or supersede an older one without anything being overwritten.

Memory items are deliberately NOT notes: they are not part of the knowledge
tree, they are not shown as notes in the UI, and they never appear in a
notebook's note list. Promoting one into the tree is an explicit, user-triggered
act (`promote_to_note`), so the tree only ever grows with intent.
"""

from datetime import datetime
from typing import Any, ClassVar, Dict, List, Literal, Optional

from loguru import logger
from pydantic import field_validator
from surreal_commands import submit_command

from open_notebook.database.repository import (
    ensure_record_id,
    repo_query,
    repo_update,
)
from open_notebook.domain.base import ObjectModel, RecordModel
from open_notebook.exceptions import DatabaseOperationError, InvalidInputError

MemoryStatus = Literal["active", "pending", "stale", "superseded"]
MemoryKind = Literal[
    "fact",
    "preference",
    "observation",
    "claim",
    "summary",
    "relationship",
]
MemoryOrigin = Literal["derived", "inferred", "confirmed"]

# Edge tables a memory item can be the source of, or participate in. Kept as a
# closed set because the relationship name is interpolated into SurrealQL (it
# names a table, so it cannot be bound as a query parameter) - same reasoning as
# the repository's `_ensure_safe_identifier`.
MEMORY_RELATIONS: Dict[str, str] = {
    "supports": "the target memory is consistent with this one",
    "contradicts": "the target memory conflicts with this one",
    "supersedes": "this memory replaces the target one",
    "derived_from": "provenance: source, note or memory this was derived from",
    "promoted_to": "the note this memory was accepted into",
}


class MemoryItem(ObjectModel):
    """An atomic, provenance-tracked unit of AI memory.

    Mirrors the Zettelkasten-style memory card: the content plus the structured
    attributes (keywords, tags, context) that make it linkable and revisable.
    """

    table_name: ClassVar[str] = "memory_item"
    record_fields: ClassVar[set[str]] = {"notebook"}
    nullable_fields: ClassVar[set[str]] = {
        "kind",
        "keywords",
        "tags",
        "context",
        "embedding",
        "confidence",
        "importance",
        "status",
        "origin",
        "notebook",
        "access_count",
        "last_accessed",
        "valid_from",
        "valid_until",
    }

    content: str
    kind: Optional[MemoryKind] = None
    keywords: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    context: Optional[str] = None
    embedding: Optional[List[float]] = None
    confidence: Optional[float] = None
    importance: Optional[float] = None
    status: Optional[MemoryStatus] = "active"
    # Mixed write policy: "derived" (automatically extracted from a source, note
    # or answer, safe to store unattended), "inferred" (the model's own
    # conclusion, needs confirmation) and "confirmed" (a human accepted it).
    origin: Optional[MemoryOrigin] = "derived"
    notebook: Optional[str] = None
    access_count: Optional[int] = None
    last_accessed: Optional[datetime] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None

    @field_validator("content")
    @classmethod
    def content_must_not_be_empty(cls, v):
        if v is not None and not v.strip():
            raise InvalidInputError("Memory content cannot be empty")
        return v

    async def save(self) -> Optional[str]:
        """Save the memory item and submit its embedding command.

        Same contract as `Note.save()`: the record is already durable when the
        embedding is submitted, so a submission hiccup is logged rather than
        raised (the memory is still usable through keyword search).

        Returns:
            Optional[str]: The command_id if embedding was submitted, else None.
        """
        await super().save()

        if self.id and self.content and self.content.strip():
            try:
                command_id = submit_command(
                    "open_notebook",
                    "embed_memory",
                    {"memory_id": str(self.id)},
                )
                logger.debug(
                    f"Submitted embed_memory command {command_id} for {self.id}"
                )
                return command_id
            except Exception as e:
                logger.error(
                    f"Failed to submit embed_memory command for {self.id}: {e}"
                )
                return None

        return None

    # ------------------------------------------------------------------ status

    async def set_status(
        self, status: MemoryStatus, valid_until: Optional[datetime] = None
    ) -> None:
        """Move the memory to another lifecycle state without re-embedding.

        Used for the invalidation paths: `stale` when the note/source it was
        derived from changed, `superseded` when a newer memory replaced it
        (together with `valid_until`, so the old fact stays readable as history).
        """
        if not self.id:
            raise InvalidInputError("Cannot update an unsaved memory")
        record_id = self.id

        data: Dict[str, Any] = {"status": status}
        if valid_until is not None:
            data["valid_until"] = valid_until

        try:
            await repo_update(self.table_name, record_id, data)
        except Exception as e:
            logger.error(f"Error updating memory {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to update memory status")

        self.status = status
        if valid_until is not None:
            self.valid_until = valid_until

    async def approve(self) -> None:
        """Accept a pending memory (mixed write policy: the human reviewed it)."""
        self.origin = "confirmed"
        await self.set_status("active")

    # --------------------------------------------------------------- relations

    def _ensure_relation(self, relationship: str) -> str:
        if relationship not in MEMORY_RELATIONS:
            raise InvalidInputError(
                f"Unknown memory relationship: {relationship}. "
                f"Expected one of: {', '.join(sorted(MEMORY_RELATIONS))}"
            )
        return relationship

    async def link(
        self, target_id: str, relationship: str, data: Optional[Dict] = None
    ) -> Any:
        """Create a typed edge from this memory to another record."""
        return await self.relate(
            self._ensure_relation(relationship), target_id, data or {}
        )

    async def relate_provenance(self, target_id: str, kind: Optional[str] = None) -> Any:
        """Record where this memory came from (source, note or other memory).

        Provenance is not optional in practice: a memory without it cannot be
        re-derived or invalidated when its origin changes.
        """
        if not target_id:
            raise InvalidInputError("Provenance target ID must be provided")
        return await self.relate("derived_from", target_id, {"kind": kind} if kind else {})

    async def supersede(
        self, older: "MemoryItem", reason: Optional[str] = None
    ) -> None:
        """Mark `older` as replaced by this memory, keeping it as history.

        The edge points from the newer memory to the older one (this memory
        `supersedes` that one), and the older one is closed off with
        `valid_until` instead of being deleted.
        """
        if not older or not older.id:
            raise InvalidInputError("A saved memory is required to supersede")

        await self.link("supersedes", older.id, {"reason": reason} if reason else {})
        await older.set_status("superseded", valid_until=datetime.now())

    async def get_provenance(self) -> List[Dict[str, Any]]:
        """Return the records this memory was derived from."""
        if not self.id:
            raise InvalidInputError("Cannot read provenance of an unsaved memory")
        try:
            rows = await repo_query(
                "SELECT out, kind FROM derived_from WHERE in = $id",
                {"id": ensure_record_id(self.id)},
            )
        except Exception as e:
            logger.error(f"Error fetching provenance for {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to fetch memory provenance")
        return rows

    async def get_related_ids(self, relationship: str) -> List[str]:
        """Return the ids on the other side of `relationship` (either direction)."""
        if not self.id:
            raise InvalidInputError("Cannot read relations of an unsaved memory")
        rel = self._ensure_relation(relationship)
        try:
            rows = await repo_query(
                f"""
                SELECT VALUE out FROM {rel} WHERE in = $id
                UNION
                SELECT VALUE in FROM {rel} WHERE out = $id
                """,
                {"id": ensure_record_id(self.id)},
            )
        except Exception as e:
            logger.error(f"Error fetching {rel} for {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError(f"Failed to fetch {rel} relations")
        return [str(row) for row in rows if row]

    # --------------------------------------------------------------- promotion

    async def promote_to_note(
        self, notebook_id: Optional[str] = None, title: Optional[str] = None
    ) -> Any:
        """Accept this memory into the user knowledge tree as a note.

        This is the only path from the memory plane into the knowledge tree, it
        is always user-triggered, and it leaves a `promoted_to` edge behind so
        the note can be traced back to the memory it came from.
        """
        # Imported lazily: notebook.py is the knowledge-tree module and importing
        # it at module level would create a cycle (Note reads its memories).
        from open_notebook.domain.notebook import Note

        if not self.content or not self.content.strip():
            raise InvalidInputError("Cannot promote an empty memory")

        note = Note(
            title=title or _derive_title(self.content),
            content=self.content,
            note_type="ai",
            note_kind="promoted_memory",
            status="active",
            tags=self.tags,
            keywords=self.keywords,
        )
        await note.save()
        if notebook_id:
            await note.add_to_notebook(notebook_id)

        await self.link("promoted_to", str(note.id))
        self.origin = "confirmed"
        return note

    # ---------------------------------------------------------------- queries

    @classmethod
    async def get_for_notebook(
        cls,
        notebook_id: str,
        statuses: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List["MemoryItem"]:
        """Fetch a notebook's memories in a single query.

        Excludes superseded memories by default: they are history, retrievable
        on purpose but noise in any listing.
        """
        if not notebook_id:
            raise InvalidInputError("Notebook ID must be provided")
        wanted = statuses or ["active", "pending", "stale"]
        query = (
            "SELECT * FROM memory_item WHERE notebook = $notebook_id "
            "AND status IN $statuses ORDER BY updated DESC"
        )
        vars: Dict[str, Any] = {
            "notebook_id": ensure_record_id(notebook_id),
            "statuses": wanted,
        }
        if limit:
            query += " LIMIT $limit"
            vars["limit"] = limit
        try:
            rows = await repo_query(query, vars)
        except Exception as e:
            logger.error(f"Error fetching memories for {notebook_id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to fetch memories")
        return [cls(**row) for row in rows]

    @classmethod
    async def list_pending(cls, limit: int = 50) -> List["MemoryItem"]:
        """Memories awaiting human confirmation (mixed write policy)."""
        try:
            rows = await repo_query(
                "SELECT * FROM memory_item WHERE status = 'pending' "
                "ORDER BY updated DESC LIMIT $limit",
                {"limit": limit},
            )
        except Exception as e:
            logger.error(f"Error listing pending memories: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to list pending memories")
        return [cls(**row) for row in rows]

    @classmethod
    async def find_neighbors(
        cls,
        embedding: List[float],
        limit: int = 5,
        minimum_score: float = 0.6,
        notebook_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Semantic neighbours of a memory, used for link/evolution decisions.

        Returns raw rows (id/content/similarity) so callers can decide whether a
        neighbour should be linked, contradicted or superseded.
        """
        if not embedding:
            raise InvalidInputError("An embedding is required to find neighbours")
        try:
            return await repo_query(
                "SELECT * FROM fn::memory_search($embed, $limit, $minimum_score, $notebook_ids)",
                {
                    "embed": embedding,
                    "limit": limit,
                    "minimum_score": minimum_score,
                    "notebook_ids": (
                        [ensure_record_id(notebook_id)] if notebook_id else None
                    ),
                },
            )
        except Exception as e:
            logger.error(f"Error finding memory neighbours: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to find memory neighbours")


class MemoryCore(RecordModel):
    """Slow-changing memory about the person, not about the content.

    Kept as a single editable record so the user can always read and correct
    what the AI believes about them (prompt context gets this injected, it is
    never silently inferred from behaviour).
    """

    record_id: ClassVar[str] = "open_notebook:memory_core"
    profile: str = ""
    domains: Optional[List[str]] = None
    preferences: Optional[List[str]] = None
    updated: Optional[datetime] = None


def _derive_title(content: str, max_length: int = 80) -> str:
    """First non-empty line of the memory, trimmed to something note-sized."""
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:max_length]
    return content[:max_length]


async def memory_search(
    keyword: str,
    results: int = 10,
    minimum_score: float = 0.2,
    notebook_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Semantic search over the AI memory plane.

    Kept separate from `notebook.vector_search` (which searches sources and
    notes) because the two planes are meant to stay distinguishable: callers
    that fuse them can label the results by origin and keep the user's own notes
    authoritative in the prompt.
    """
    if not keyword:
        raise InvalidInputError("Search keyword cannot be empty")

    from open_notebook.utils.embedding import generate_embedding

    try:
        embed = await generate_embedding(keyword)
        rows = await repo_query(
            """
            SELECT * FROM fn::memory_search($embed, $results, $minimum_score, $notebook_ids);
            """,
            {
                "embed": embed,
                "results": results,
                "minimum_score": minimum_score,
                "notebook_ids": (
                    [ensure_record_id(nb) for nb in notebook_ids]
                    if notebook_ids
                    else None
                ),
            },
        )
    except Exception as e:
        logger.error(f"Error performing memory search: {str(e)}")
        logger.exception(e)
        raise DatabaseOperationError(e)

    # Same defensive ordering as vector_search: the SELECT * FROM fn::... wrapper
    # can return rows out of rank order even though the function sorts them.
    return sorted(
        rows or [],
        key=lambda item: (
            -float(item.get("similarity") or 0.0),
            str(item.get("id") or ""),
        ),
    )
