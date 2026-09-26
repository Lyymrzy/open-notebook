"""Frontier proposals of the knowledge tree ("exploration points").

An exploration point is a question the AI thinks is worth answering next, tied
to a concrete place in the user's knowledge tree. It is what makes the tree
growable instead of static: leaves get proposals, the user picks one, the
result gets consolidated into knowledge and the leaf is no longer a leaf.

These are lightweight, persistent suggestions - not conversations. Answering one
happens in a throwaway Q&A session (see the LangGraph checkpointer), and only
the consolidated knowledge survives.
"""

from typing import Any, ClassVar, List, Literal, Optional

from loguru import logger

from open_notebook.database.repository import (
    ensure_record_id,
    repo_query,
    repo_update,
)
from open_notebook.domain.base import ObjectModel
from open_notebook.exceptions import DatabaseOperationError, InvalidInputError

ExplorationKind = Literal["gap", "depth", "crosslink", "contradiction"]
ExplorationStatus = Literal["proposed", "accepted", "dismissed", "consumed"]


class ExplorationPoint(ObjectModel):
    """A proposed next step, anchored on a knowledge note."""

    table_name: ClassVar[str] = "exploration_point"
    record_fields: ClassVar[set[str]] = {"notebook"}

    question: str
    rationale: Optional[str] = None
    # How the proposal was found: an uncovered area of the tree ("gap"), a topic
    # worth going deeper on ("depth"), a missing link between existing notes
    # ("crosslink") or two beliefs that cannot both hold ("contradiction").
    kind: Optional[ExplorationKind] = None
    status: Optional[ExplorationStatus] = "proposed"
    score: Optional[float] = None
    notebook: Optional[str] = None
    # Which producer generated the point ("frontier_scan", "user", ...).
    origin: Optional[str] = None

    async def anchor_to(self, note_id: str, reason: Optional[str] = None) -> Any:
        """Tie this proposal to the note it grows out of."""
        if not note_id:
            raise InvalidInputError("An anchor note ID must be provided")
        return await self.relate("explores", note_id, {"reason": reason} if reason else {})

    async def get_anchors(self) -> List[str]:
        """Return the note ids this proposal is attached to."""
        if not self.id:
            raise InvalidInputError("Cannot read anchors of an unsaved point")
        try:
            rows = await repo_query(
                "SELECT VALUE out FROM explores WHERE in = $id",
                {"id": ensure_record_id(self.id)},
            )
        except Exception as e:
            logger.error(f"Error fetching anchors for {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to fetch exploration anchors")
        return [str(row) for row in rows if row]

    async def set_status(self, status: ExplorationStatus) -> None:
        """Move the proposal along: accepted / dismissed / consumed."""
        if not self.id:
            raise InvalidInputError("Cannot update an unsaved exploration point")
        record_id = self.id
        try:
            await repo_update(self.table_name, record_id, {"status": status})
        except Exception as e:
            logger.error(f"Error updating exploration point {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to update exploration point")
        self.status = status

    @classmethod
    async def get_for_notebook(
        cls,
        notebook_id: str,
        statuses: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List["ExplorationPoint"]:
        """List a notebook's proposals in a single query, best first."""
        if not notebook_id:
            raise InvalidInputError("Notebook ID must be provided")
        query = (
            "SELECT * FROM exploration_point WHERE notebook = $notebook_id "
            "AND status IN $statuses ORDER BY score DESC, updated DESC"
        )
        vars: dict = {
            "notebook_id": ensure_record_id(notebook_id),
            "statuses": statuses or ["proposed", "accepted"],
        }
        if limit:
            query += " LIMIT $limit"
            vars["limit"] = limit
        try:
            rows = await repo_query(query, vars)
        except Exception as e:
            logger.error(f"Error fetching exploration points: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to fetch exploration points")
        return [cls(**row) for row in rows]

    @classmethod
    async def get_for_note(
        cls, note_id: str, statuses: Optional[List[str]] = None
    ) -> List["ExplorationPoint"]:
        """Proposals anchored on one note (the leaf's "what next?" list)."""
        if not note_id:
            raise InvalidInputError("Note ID must be provided")
        try:
            rows = await repo_query(
                """
                SELECT * FROM exploration_point
                WHERE id IN (SELECT VALUE in FROM explores WHERE out = $note_id)
                  AND status IN $statuses
                ORDER BY score DESC, updated DESC
                """,
                {
                    "note_id": ensure_record_id(note_id),
                    "statuses": statuses or ["proposed"],
                },
            )
        except Exception as e:
            logger.error(f"Error fetching exploration points for {note_id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to fetch exploration points for note")
        return [cls(**row) for row in rows]
