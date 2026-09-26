import os
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Literal, Optional, Union

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator
from surreal_commands import submit_command
from surrealdb import RecordID

from open_notebook.database.repository import (
    ensure_record_id,
    repo_query,
    repo_update,
)
from open_notebook.domain.base import ObjectModel
from open_notebook.exceptions import (
    DatabaseOperationError,
    InvalidInputError,
    NotFoundError,
)


class Notebook(ObjectModel):
    table_name: ClassVar[str] = "notebook"
    name: str
    description: str
    archived: Optional[bool] = False
    last_viewed_at: Optional[datetime] = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, v):
        if not v.strip():
            raise InvalidInputError("Notebook name cannot be empty")
        return v

    async def get_sources(self, include_full_text: bool = False) -> List["Source"]:
        try:
            source_projection = "" if include_full_text else " omit source.full_text"
            srcs = await repo_query(
                f"""
                select *{source_projection} from (
                select in as source from reference where out=$id
                fetch source
            ) order by source.updated desc
            """,
                {"id": ensure_record_id(self.id)},
            )
            return [Source(**src["source"]) for src in srcs] if srcs else []
        except Exception as e:
            logger.error(f"Error fetching sources for notebook {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError(e)

    async def get_notes(self, include_content: bool = False) -> List["Note"]:
        try:
            note_projection = (
                " omit note.embedding"
                if include_content
                else " omit note.content, note.embedding"
            )
            srcs = await repo_query(
                f"""
            select *{note_projection} from (
                select in as note from artifact where out=$id
                fetch note
            ) order by note.updated desc
            """,
                {"id": ensure_record_id(self.id)},
            )
            return [Note(**src["note"]) for src in srcs] if srcs else []
        except Exception as e:
            logger.error(f"Error fetching notes for notebook {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError(e)

    async def get_context(self) -> str:
        """
        Build long-form notebook context for podcast and LLM workflows.

        Normal list retrieval omits large source/note bodies, so this method uses
        opt-in full-content fetches and formats only substantive context blocks.
        """
        sources = await self.get_sources(include_full_text=True)
        notes = await self.get_notes(include_content=True)
        context_blocks = []

        insights_by_source = await SourceInsight.get_for_sources(
            [source.id for source in sources if source.id]
        )
        for source in sources:
            source_context = await source.get_context(
                context_size="long",
                insights=insights_by_source.get(source.id or "", []),
            )
            if isinstance(source_context, dict):
                title = source_context.get("title") or source.title or "Untitled source"
                full_text = source_context.get("full_text")
                insights = source_context.get("insights") or []

                content_parts = []
                if full_text:
                    content_parts.append(str(full_text))

                insight_lines = []
                for insight in insights:
                    if not isinstance(insight, dict):
                        continue

                    insight_content = insight.get("content")
                    if not insight_content:
                        continue

                    insight_type = insight.get("insight_type") or "Insight"
                    insight_lines.append(f"- {insight_type}: {insight_content}")

                if insight_lines:
                    content_parts.append("Insights:\n" + "\n".join(insight_lines))

                content = "\n\n".join(content_parts).strip()
            else:
                title = source.title or "Untitled source"
                content = str(source_context).strip()

            if content:
                context_blocks.append(f"## Source: {title}\n\n{content}")

        for note in notes:
            note_context = note.get_context(context_size="long")
            if isinstance(note_context, dict):
                title = note_context.get("title") or note.title or "Untitled note"
                content = note_context.get("content")
                content = str(content).strip() if content else ""
            else:
                title = note.title or "Untitled note"
                content = str(note_context).strip()

            if content:
                context_blocks.append(f"## Note: {title}\n\n{content}")

        return "\n\n".join(context_blocks)

    async def get_chat_sessions(self) -> List["ChatSession"]:
        try:
            srcs = await repo_query(
                """
                select * from (
                    select
                    <- chat_session as chat_session
                    from refers_to
                    where out=$id
                    fetch chat_session
                )
                order by chat_session.updated desc
            """,
                {"id": ensure_record_id(self.id)},
            )
            return (
                [ChatSession(**src["chat_session"][0]) for src in srcs] if srcs else []
            )
        except Exception as e:
            logger.error(
                f"Error fetching chat sessions for notebook {self.id}: {str(e)}"
            )
            logger.exception(e)
            raise DatabaseOperationError(e)

    async def get_delete_preview(self) -> Dict[str, Any]:
        """
        Get counts of items that would be affected by deleting this notebook.

        Returns a dict with:
        - note_count: Number of notes that will be deleted
        - exclusive_source_count: Sources only in this notebook (can be deleted)
        - shared_source_count: Sources in other notebooks (will be unlinked only)
        """
        try:
            notebook_id = ensure_record_id(self.id)

            # Count notes
            note_result = await repo_query(
                "SELECT count() as count FROM artifact WHERE out = $notebook_id GROUP ALL",
                {"notebook_id": notebook_id},
            )
            note_count = note_result[0]["count"] if note_result else 0

            # Get sources with count of references to OTHER notebooks
            # If assigned_others = 0, source is exclusive to this notebook
            # If assigned_others > 0, source is shared with other notebooks
            source_counts = await repo_query(
                """
                SELECT
                    id,
                    count(->reference[WHERE out != $notebook_id].out) as assigned_others
                FROM (SELECT VALUE <-reference.in AS sources FROM $notebook_id)[0]
                """,
                {"notebook_id": notebook_id},
            )

            exclusive_count = 0
            shared_count = 0
            for src in source_counts:
                if src.get("assigned_others", 0) == 0:
                    exclusive_count += 1
                else:
                    shared_count += 1

            return {
                "note_count": note_count,
                "exclusive_source_count": exclusive_count,
                "shared_source_count": shared_count,
            }
        except Exception as e:
            logger.error(f"Error getting delete preview for notebook {self.id}: {e}")
            logger.exception(e)
            raise DatabaseOperationError(e)

    async def delete(self, delete_exclusive_sources: bool = False) -> Dict[str, int]:
        """
        Delete notebook with cascade deletion of notes and optional source deletion.

        Args:
            delete_exclusive_sources: If True, also delete sources that belong
                                     only to this notebook. Default is False.

        Returns:
            Dict with counts: deleted_notes, deleted_sources, unlinked_sources,
            deleted_chat_sessions
        """
        if self.id is None:
            raise InvalidInputError("Cannot delete notebook without an ID")

        try:
            notebook_id = ensure_record_id(self.id)
            deleted_notes = 0
            deleted_sources = 0
            unlinked_sources = 0
            deleted_chat_sessions = 0

            # 1. Get and delete all notes linked to this notebook
            notes = await self.get_notes()
            for note in notes:
                await note.delete()
                deleted_notes += 1
            logger.info(f"Deleted {deleted_notes} notes for notebook {self.id}")

            # Delete artifact relationships
            await repo_query(
                "DELETE artifact WHERE out = $notebook_id",
                {"notebook_id": notebook_id},
            )

            # 2. Handle sources
            if delete_exclusive_sources:
                # Find sources with count of references to OTHER notebooks
                # If assigned_others = 0, source is exclusive to this notebook
                source_counts = await repo_query(
                    """
                    SELECT
                        id,
                        count(->reference[WHERE out != $notebook_id].out) as assigned_others
                    FROM (SELECT VALUE <-reference.in AS sources FROM $notebook_id)[0]
                    """,
                    {"notebook_id": notebook_id},
                )

                for src in source_counts:
                    source_id = src.get("id")
                    if source_id and src.get("assigned_others", 0) == 0:
                        # Exclusive source - delete it
                        try:
                            source = await Source.get(str(source_id))
                            await source.delete()
                            deleted_sources += 1
                        except Exception as e:
                            logger.warning(
                                f"Failed to delete exclusive source {source_id}: {e}"
                            )
                    else:
                        unlinked_sources += 1
            else:
                # Just count sources that will be unlinked
                source_result = await repo_query(
                    "SELECT count() as count FROM reference WHERE out = $notebook_id GROUP ALL",
                    {"notebook_id": notebook_id},
                )
                unlinked_sources = source_result[0]["count"] if source_result else 0

            # Delete reference relationships (unlink all sources)
            await repo_query(
                "DELETE reference WHERE out = $notebook_id",
                {"notebook_id": notebook_id},
            )
            logger.info(
                f"Unlinked {unlinked_sources} sources, deleted {deleted_sources} "
                f"exclusive sources for notebook {self.id}"
            )

            # 3. Delete chat sessions linked to this notebook
            chat_sessions = await self.get_chat_sessions()
            for chat_session in chat_sessions:
                await chat_session.delete()
                deleted_chat_sessions += 1
            logger.info(
                f"Deleted {deleted_chat_sessions} chat sessions for notebook {self.id}"
            )

            # 4. Delete the notebook record itself
            await super().delete()
            logger.info(f"Deleted notebook {self.id}")

            return {
                "deleted_notes": deleted_notes,
                "deleted_sources": deleted_sources,
                "unlinked_sources": unlinked_sources,
                "deleted_chat_sessions": deleted_chat_sessions,
            }

        except Exception as e:
            logger.error(f"Error deleting notebook {self.id}: {e}")
            logger.exception(e)
            raise DatabaseOperationError(f"Failed to delete notebook: {e}")


class Asset(BaseModel):
    file_path: Optional[str] = None
    url: Optional[str] = None


class SourceEmbedding(ObjectModel):
    table_name: ClassVar[str] = "source_embedding"
    content: str

    async def get_source(self) -> "Source":
        try:
            src = await repo_query(
                """
            select source.* from $id fetch source
            """,
                {"id": ensure_record_id(self.id)},
            )
            return Source(**src[0]["source"])
        except Exception as e:
            logger.error(f"Error fetching source for embedding {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError(e)


class SourceInsight(ObjectModel):
    table_name: ClassVar[str] = "source_insight"
    insight_type: str
    content: str

    @classmethod
    async def get_for_sources(
        cls, source_ids: List[str]
    ) -> Dict[str, List["SourceInsight"]]:
        """
        Batch-fetch insights for many sources in a single query.

        Building notebook/chat context otherwise calls get_insights() once
        per source - fine for one source, but O(n) round trips (each paying
        its own connection setup - no pooling in the repository layer) when
        a caller loops over every source in a notebook.
        """
        grouped: Dict[str, List[SourceInsight]] = {sid: [] for sid in source_ids if sid}
        if not grouped:
            return grouped
        try:
            result = await repo_query(
                "SELECT * FROM source_insight WHERE source IN $source_ids",
                {"source_ids": [ensure_record_id(sid) for sid in grouped]},
            )
        except Exception as e:
            logger.error(f"Error batch-fetching insights for sources: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to fetch insights for sources")
        for row in result:
            key = str(row.get("source"))
            grouped.setdefault(key, []).append(cls(**row))
        return grouped

    async def get_source(self) -> "Source":
        try:
            src = await repo_query(
                """
            select source.* from $id fetch source
            """,
                {"id": ensure_record_id(self.id)},
            )
            return Source(**src[0]["source"])
        except Exception as e:
            logger.error(f"Error fetching source for insight {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError(e)

    async def save_as_note(self, notebook_id: Optional[str] = None) -> Any:
        source = await self.get_source()
        note = Note(
            title=f"{self.insight_type} from source {source.title}",
            content=self.content,
        )
        await note.save()
        if notebook_id:
            await note.add_to_notebook(notebook_id)
        return note


class Source(ObjectModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    table_name: ClassVar[str] = "source"
    asset: Optional[Asset] = None
    title: Optional[str] = None
    topics: Optional[List[str]] = Field(default_factory=list)
    full_text: Optional[str] = None
    last_viewed_at: Optional[datetime] = None
    command: Optional[Union[str, RecordID]] = Field(
        default=None, description="Link to surreal-commands processing job"
    )

    @field_validator("command", mode="before")
    @classmethod
    def parse_command(cls, value):
        """Parse command field to ensure RecordID format"""
        if isinstance(value, str) and value:
            return ensure_record_id(value)
        return value

    @field_validator("id", mode="before")
    @classmethod
    def parse_id(cls, value):
        """Parse id field to handle both string and RecordID inputs"""
        if value is None:
            return None
        if isinstance(value, RecordID):
            return str(value)
        return str(value) if value else None

    async def get_status(self) -> Optional[str]:
        """Get the processing status of the associated command"""
        if not self.command:
            return None

        try:
            from surreal_commands import get_command_status

            status = await get_command_status(str(self.command))
            return status.status if status else "unknown"
        except Exception as e:
            logger.warning(f"Failed to get command status for {self.command}: {e}")
            return "unknown"

    async def get_processing_progress(self) -> Optional[Dict[str, Any]]:
        """Get detailed processing information for the associated command"""
        if not self.command:
            return None

        try:
            from surreal_commands import get_command_status

            status_result = await get_command_status(str(self.command))
            if not status_result:
                return None

            # Extract execution metadata if available
            result = getattr(status_result, "result", None)
            execution_metadata = (
                result.get("execution_metadata", {}) if isinstance(result, dict) else {}
            )

            return {
                "status": status_result.status,
                "started_at": execution_metadata.get("started_at"),
                "completed_at": execution_metadata.get("completed_at"),
                "error": getattr(status_result, "error_message", None),
                "result": result,
            }
        except Exception as e:
            logger.warning(f"Failed to get command progress for {self.command}: {e}")
            return None

    async def get_context(
        self,
        context_size: Literal["short", "long"] = "short",
        insights: Optional[List["SourceInsight"]] = None,
    ) -> Dict[str, Any]:
        # Callers looping over many sources can batch-fetch insights up front
        # via SourceInsight.get_for_sources() and pass them in here, instead
        # of paying a separate query per source.
        insight_objects = insights if insights is not None else await self.get_insights()
        insights = [insight.model_dump() for insight in insight_objects]
        if context_size == "long":
            return dict(
                id=self.id,
                title=self.title,
                insights=insights,
                full_text=self.full_text,
            )
        else:
            return dict(id=self.id, title=self.title, insights=insights)

    async def get_embedded_chunks(self) -> int:
        try:
            result = await repo_query(
                """
                select count() as chunks from source_embedding where source=$id GROUP ALL
                """,
                {"id": ensure_record_id(self.id)},
            )
            if len(result) == 0:
                return 0
            return result[0]["chunks"]
        except Exception as e:
            logger.error(f"Error fetching chunks count for source {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError(f"Failed to count chunks for source: {str(e)}")

    async def get_insights(self) -> List[SourceInsight]:
        try:
            result = await repo_query(
                """
                SELECT * FROM source_insight WHERE source=$id
                """,
                {"id": ensure_record_id(self.id)},
            )
            return [SourceInsight(**insight) for insight in result]
        except Exception as e:
            logger.error(f"Error fetching insights for source {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to fetch insights for source")

    async def add_to_notebook(self, notebook_id: str) -> Any:
        if not notebook_id:
            raise InvalidInputError("Notebook ID must be provided")
        await Notebook.get(notebook_id)  # raises NotFoundError if invalid/missing
        return await self.relate("reference", notebook_id)

    async def vectorize(self) -> str:
        """
        Submit vectorization as a background job using the embed_source command.

        This method leverages the job-based architecture to prevent HTTP connection
        pool exhaustion when processing large documents. The embed_source command:
        1. Detects content type from file path
        2. Chunks text using content-type aware splitter
        3. Generates all embeddings in batches
        4. Bulk inserts source_embedding records

        Returns:
            str: The command/job ID that can be used to track progress via the commands API

        Raises:
            ValueError: If source has no text to vectorize
            DatabaseOperationError: If job submission fails
        """
        logger.info(f"Submitting embed_source job for source {self.id}")

        try:
            if not self.full_text or not self.full_text.strip():
                raise ValueError(f"Source {self.id} has no text to vectorize")

            # Submit the embed_source command
            command_id = submit_command(
                "open_notebook",
                "embed_source",
                {"source_id": str(self.id)},
            )

            command_id_str = str(command_id)
            logger.info(
                f"Embed source job submitted for source {self.id}: "
                f"command_id={command_id_str}"
            )

            return command_id_str

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Failed to submit embed_source job for source {self.id}: {e}")
            logger.exception(e)
            raise DatabaseOperationError(e)

    async def add_insight(self, insight_type: str, content: str) -> str:
        """
        Submit insight creation as an async command (fire-and-forget).

        Submits a create_insight command that handles database operations with
        automatic retry logic for transaction conflicts. The command also submits
        an embed_insight command for async embedding.

        This method returns immediately after submitting the command - it does NOT
        wait for the insight to be created. Use this for batch operations where
        throughput is more important than immediate confirmation.

        Args:
            insight_type: Type/category of the insight
            content: The insight content text

        Returns:
            command_id for optional tracking

        Raises:
            InvalidInputError: If insight_type or content is empty
            DatabaseOperationError: If submitting the command fails. Matches
                vectorize()'s contract - callers (transformation.py, source.py)
                run inside surreal-commands jobs whose outer exception
                handling already retries transient failures, so a swallowed
                submission failure here previously meant a transformation
                could report success while the insight was silently never
                persisted.
        """
        if not insight_type or not content:
            raise InvalidInputError("Insight type and content must be provided")

        try:
            # Submit create_insight command (fire-and-forget)
            # Command handles retries internally for transaction conflicts
            command_id = submit_command(
                "open_notebook",
                "create_insight",
                {
                    "source_id": str(self.id),
                    "insight_type": insight_type,
                    "content": content,
                },
            )
            logger.info(
                f"Submitted create_insight command {command_id} for source {self.id} "
                f"(type={insight_type})"
            )
            return str(command_id)

        except Exception as e:
            logger.exception(f"Error submitting create_insight for source {self.id}: {e}")
            raise DatabaseOperationError(e)

    def _prepare_save_data(self) -> dict:
        """Override to ensure command field is always RecordID format for database"""
        data = super()._prepare_save_data()

        # Ensure command field is RecordID format if not None
        if data.get("command") is not None:
            data["command"] = ensure_record_id(data["command"])

        return data

    async def delete(self) -> bool:
        """Delete source and clean up associated file, embeddings, and insights."""
        # Clean up uploaded file if it exists
        if self.asset and self.asset.file_path:
            file_path = Path(self.asset.file_path)
            if file_path.exists():
                try:
                    os.unlink(file_path)
                    logger.info(f"Deleted file for source {self.id}: {file_path}")
                except Exception as e:
                    logger.warning(
                        f"Failed to delete file {file_path} for source {self.id}: {e}. "
                        "Continuing with database deletion."
                    )
            else:
                logger.debug(
                    f"File {file_path} not found for source {self.id}, skipping cleanup"
                )

        # Delete associated embeddings and insights to prevent orphaned records
        try:
            source_id = ensure_record_id(self.id)
            await repo_query(
                "DELETE source_embedding WHERE source = $source_id",
                {"source_id": source_id},
            )
            await repo_query(
                "DELETE source_insight WHERE source = $source_id",
                {"source_id": source_id},
            )
            logger.debug(f"Deleted embeddings and insights for source {self.id}")
        except Exception as e:
            logger.warning(
                f"Failed to delete embeddings/insights for source {self.id}: {e}. "
                "Continuing with source deletion."
            )

        # Call parent delete to remove database record
        return await super().delete()


class Note(ObjectModel):
    table_name: ClassVar[str] = "note"
    title: Optional[str] = None
    note_type: Optional[Literal["human", "ai"]] = None
    content: Optional[str] = None
    summary: Optional[str] = None
    # Knowledge-tree metadata (migration 25). A note is either written by a human
    # or produced by an AI pass; both live in the same tree, so these fields
    # record how an AI note came to be and whether it is still trusted.
    tags: Optional[List[str]] = None
    keywords: Optional[List[str]] = None
    note_kind: Optional[str] = None
    status: Optional[Literal["active", "stale", "archived"]] = None
    # Set when the note is a proposed revision rather than an accepted note:
    # "pending" until the user accepts or rejects it. AI never rewrites an
    # existing note in place.
    proposal_status: Optional[Literal["pending", "accepted", "rejected"]] = None
    generated_by: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None

    @field_validator("content")
    @classmethod
    def content_must_not_be_empty(cls, v):
        if v is not None and not v.strip():
            raise InvalidInputError("Note content cannot be empty")
        return v

    async def save(self) -> Optional[str]:
        """
        Save the note and submit embedding command.

        Overrides ObjectModel.save() to submit an async embed_note command
        after saving, instead of inline embedding.

        Returns:
            Optional[str]: The command_id if embedding was submitted, None
                otherwise (either no content to embed, or submission failed)
        """
        # Call parent save (without embedding)
        await super().save()

        # Submit embedding command (fire-and-forget) if note has content.
        # Unlike Source.vectorize()/add_insight() (explicit, dedicated calls
        # whose whole point is the submission), this runs automatically
        # inside save() - the note itself is already durably saved above,
        # so a submission hiccup here shouldn't fail an otherwise-successful
        # save with a 500. Best-effort: log and move on.
        if self.id and self.content and self.content.strip():
            try:
                command_id = submit_command(
                    "open_notebook",
                    "embed_note",
                    {"note_id": str(self.id)},
                )
                logger.debug(f"Submitted embed_note command {command_id} for {self.id}")
                return command_id
            except Exception as e:
                logger.error(f"Failed to submit embed_note command for {self.id}: {e}")
                return None

        return None

    async def add_to_notebook(self, notebook_id: str) -> Any:
        if not notebook_id:
            raise InvalidInputError("Notebook ID must be provided")
        await Notebook.get(notebook_id)  # raises NotFoundError if invalid/missing
        return await self.relate("artifact", notebook_id)

    # ---------------------------------------------------------- knowledge tree
    # The tree is carried by the `includes` edge instead of a parent field, so a
    # note can legitimately sit under more than one topic without being copied.

    async def _notes_from_id_query(self, id_query: str) -> List["Note"]:
        """Fetch notes from a query that yields a flat list of note ids."""
        if not self.id:
            raise InvalidInputError("Cannot traverse from an unsaved note")
        try:
            rows = await repo_query(id_query, {"id": ensure_record_id(self.id)})
            ids = [str(row) for row in rows if row]
            if not ids:
                return []
            notes = await repo_query(
                "SELECT * FROM note WHERE id IN $ids",
                {"ids": [ensure_record_id(note_id) for note_id in ids]},
            )
            return [Note(**row) for row in notes]
        except Exception as e:
            logger.error(f"Error traversing knowledge tree from {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to traverse the knowledge tree")

    async def add_child(self, child_id: str) -> Any:
        """Attach another note as a subtopic of this one (grows the tree)."""
        if not child_id:
            raise InvalidInputError("Child note ID must be provided")
        if self.id and str(child_id) == str(self.id):
            raise InvalidInputError("A note cannot include itself")
        await Note.get(child_id)  # raises NotFoundError if invalid/missing
        return await self.relate("includes", child_id)

    async def remove_child(self, child_id: str) -> None:
        """Detach a subtopic without deleting the note itself."""
        if not self.id or not child_id:
            raise InvalidInputError("Both parent and child IDs are required")
        try:
            await repo_query(
                "DELETE includes WHERE in = $parent AND out = $child",
                {
                    "parent": ensure_record_id(self.id),
                    "child": ensure_record_id(child_id),
                },
            )
        except Exception as e:
            logger.error(f"Error removing child {child_id} from {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to detach subtopic")

    async def link_related(self, other_id: str, reason: Optional[str] = None) -> Any:
        """Cross-reference another note (the web on top of the tree)."""
        if not other_id:
            raise InvalidInputError("Related note ID must be provided")
        return await self.relate(
            "relates_to", other_id, {"reason": reason} if reason else {}
        )

    async def get_children(self) -> List["Note"]:
        return await self._notes_from_id_query(
            "SELECT VALUE out FROM includes WHERE in = $id"
        )

    async def get_parents(self) -> List["Note"]:
        return await self._notes_from_id_query(
            "SELECT VALUE in FROM includes WHERE out = $id"
        )

    async def get_related(self) -> List["Note"]:
        return await self._notes_from_id_query(
            """
            SELECT VALUE out FROM relates_to WHERE in = $id
            UNION
            SELECT VALUE in FROM relates_to WHERE out = $id
            """
        )

    async def is_leaf(self) -> bool:
        """True when nothing grows out of this note yet.

        Leaves are where exploration points are proposed: the frontier of what
        the user has already understood.
        """
        if not self.id:
            raise InvalidInputError("Cannot test an unsaved note for leaf status")
        try:
            rows = await repo_query(
                "SELECT VALUE out FROM includes WHERE in = $id LIMIT 1",
                {"id": ensure_record_id(self.id)},
            )
        except Exception as e:
            logger.error(f"Error checking leaf status of {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to check leaf status")
        return not rows

    async def get_memories(
        self, statuses: Optional[List[str]] = None
    ) -> List[Any]:
        """Memories the AI derived from this note."""
        from open_notebook.domain.memory import MemoryItem

        if not self.id:
            raise InvalidInputError("Cannot read memories of an unsaved note")
        try:
            rows = await repo_query(
                """
                SELECT * FROM memory_item
                WHERE id IN (SELECT VALUE in FROM derived_from WHERE out = $id)
                  AND status IN $statuses
                ORDER BY updated DESC
                """,
                {
                    "id": ensure_record_id(self.id),
                    "statuses": statuses or ["active", "pending", "stale"],
                },
            )
        except Exception as e:
            logger.error(f"Error fetching memories for note {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to fetch note memories")
        return [MemoryItem(**row) for row in rows]

    async def get_exploration_points(
        self, statuses: Optional[List[str]] = None
    ) -> List[Any]:
        """Proposals anchored on this note (usually a leaf)."""
        from open_notebook.domain.exploration import ExplorationPoint

        if not self.id:
            raise InvalidInputError("Cannot read proposals of an unsaved note")
        return await ExplorationPoint.get_for_note(str(self.id), statuses=statuses)

    async def set_proposal_status(
        self, proposal_status: str, status: Optional[str] = None
    ) -> None:
        """Accept or reject a proposed revision, optionally changing its status."""
        if not self.id:
            raise InvalidInputError("Cannot update an unsaved note")
        data: Dict[str, Any] = {"proposal_status": proposal_status}
        if status is not None:
            data["status"] = status
        try:
            await repo_update(self.table_name, self.id, data)
        except Exception as e:
            logger.error(f"Error updating proposal status of {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to update proposal status")
        self.proposal_status = proposal_status
        if status is not None:
            self.status = status

    @classmethod
    async def get_knowledge_notes(
        cls, order_by: Optional[str] = "updated desc"
    ) -> List["Note"]:
        """Every real knowledge note - unaccepted proposals excluded.

        A refinement proposal is a note row with `proposal_status` set, so
        anything that lists or aggregates notes must go through this instead of
        `get_all()`, or half-baked AI revisions leak into the user's view.
        """
        query = "SELECT * FROM note WHERE proposal_status = NONE"
        if order_by:
            query += f" ORDER BY {cls._validate_order_by(order_by)}"
        try:
            rows = await repo_query(query)
        except Exception as e:
            logger.error(f"Error fetching knowledge notes: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to fetch knowledge notes")
        return [cls(**row) for row in rows]

    @classmethod
    async def get_pending_proposals(cls) -> List["Note"]:
        """Refinement proposals waiting for the user's decision."""
        try:
            rows = await repo_query(
                "SELECT * FROM note WHERE proposal_status = 'pending' "
                "ORDER BY updated DESC"
            )
        except Exception as e:
            logger.error(f"Error fetching pending proposals: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to fetch pending proposals")
        return [cls(**row) for row in rows]

    async def get_derived_from(self) -> List[Dict[str, Any]]:
        """Records this note was derived from, with the provenance edge data."""
        if not self.id:
            raise InvalidInputError("Cannot read provenance of an unsaved note")
        try:
            rows = await repo_query(
                "SELECT out, kind, previous_content FROM derived_from WHERE in = $id",
                {"id": ensure_record_id(self.id)},
            )
        except Exception as e:
            logger.error(f"Error fetching provenance for note {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to fetch note provenance")
        return rows

    async def apply_refinement(self, proposal: "Note") -> None:
        """Adopt an accepted refinement proposal into this note.

        The only place an existing note's body is replaced, and it only ever
        runs after the user accepted the proposal. The proposal is archived
        rather than deleted so the change stays auditable (the pre-revision text
        lives on the proposal's provenance edge, captured when it was created).
        """
        if not proposal or not proposal.content or not proposal.content.strip():
            raise InvalidInputError("A refinement proposal with content is required")

        self.title = proposal.title or self.title
        self.content = proposal.content
        self.status = "active"
        await self.save()
        await proposal.set_proposal_status("accepted", status="archived")

    def get_context(
        self, context_size: Literal["short", "long"] = "short"
    ) -> Dict[str, Any]:
        if context_size == "long":
            return dict(id=self.id, title=self.title, content=self.content)
        else:
            return dict(
                id=self.id,
                title=self.title,
                content=self.content[:100] if self.content else None,
            )


class ChatSession(ObjectModel):
    table_name: ClassVar[str] = "chat_session"
    nullable_fields: ClassVar[set[str]] = {"model_override"}
    title: Optional[str] = None
    model_override: Optional[str] = None

    async def relate_to_notebook(self, notebook_id: str) -> Any:
        if not notebook_id:
            raise InvalidInputError("Notebook ID must be provided")
        return await self.relate("refers_to", notebook_id)

    async def relate_to_source(self, source_id: str) -> Any:
        if not source_id:
            raise InvalidInputError("Source ID must be provided")
        return await self.relate("refers_to", source_id)


async def resolve_notebook_scope(notebook_ids: List[str]) -> List[str]:
    """Validate a notebook scope before it reaches text_search / vector_search.

    Every id must be a well-formed `notebook:<key>` id naming an existing
    notebook: a typo would otherwise silently return an empty result set that
    is indistinguishable from "no matches". Existence is checked with one query
    for the whole list. Returns the ids unchanged (empty list = whole knowledge
    base). Raises InvalidInputError for malformed ids, NotFoundError for
    unknown notebooks.
    """
    if not notebook_ids:
        return []

    # Only notebook ids are accepted: a source or note id would fail the typed
    # record<notebook> parameter inside SurrealDB instead of returning cleanly,
    # and "notebook:" with an empty key would blow up in RecordID.parse.
    record_ids: List[RecordID] = []
    invalid: List[str] = []
    for nb_id in notebook_ids:
        if not nb_id.startswith("notebook:") or not nb_id[len("notebook:") :]:
            invalid.append(nb_id)
            continue
        try:
            record_ids.append(ensure_record_id(nb_id))
        except Exception:
            invalid.append(nb_id)
    if invalid:
        raise InvalidInputError(f"Invalid notebook id(s): {', '.join(invalid)}")

    try:
        rows = await repo_query(
            "SELECT id FROM notebook WHERE id IN $ids", {"ids": record_ids}
        )
    except Exception as e:
        # Same contract as text_search / vector_search: log the raw driver
        # error here, surface a typed error to the API layer.
        logger.error(f"Error resolving notebook scope: {str(e)}")
        logger.exception(e)
        raise DatabaseOperationError("Failed to resolve notebook scope")
    # The driver returns ids as RecordID objects (unhashable, and never equal
    # to the request strings), so normalize to strings before comparing.
    found = {str(row["id"]) for row in rows}
    missing = [nb_id for nb_id in notebook_ids if nb_id not in found]
    if missing:
        raise NotFoundError(f"Notebook(s) not found: {', '.join(missing)}")
    return notebook_ids


def _scope_record_ids(notebook_ids: Optional[List[str]]) -> Optional[List[RecordID]]:
    """Normalize a notebook scope for fn::text_search / fn::vector_search.

    Returns None for an empty scope (global search) so the SurrealQL functions
    take their unfiltered path, otherwise the ids as RecordIDs.
    """
    if not notebook_ids:
        return None
    return [ensure_record_id(nb_id) for nb_id in notebook_ids]


async def text_search(
    keyword: str,
    results: int,
    source: bool = True,
    note: bool = True,
    notebook_ids: Optional[List[str]] = None,
):
    if not keyword:
        raise InvalidInputError("Search keyword cannot be empty")
    try:
        search_results = await repo_query(
            """
            select *
            from fn::text_search($keyword, $results, $source, $note, $notebook_ids)
            """,
            {
                "keyword": keyword,
                "results": results,
                "source": source,
                "note": note,
                "notebook_ids": _scope_record_ids(notebook_ids),
            },
        )
        return search_results
    except RuntimeError as e:
        # SurrealDB's search::highlight can compute a byte position that exceeds the
        # stored string length on large or multi-byte chunks, aborting the whole query
        # ("position overflow"). Fall back to vector search so the user still gets
        # results instead of a 500. See issue #648.
        if "position overflow" in str(e):
            logger.warning(
                f"Highlight position overflow, falling back to vector search: {str(e)}"
            )
            try:
                return await vector_search(
                    keyword, results, source, note, notebook_ids=notebook_ids
                )
            except Exception as ve:
                # Both search paths failed (e.g. no embedding model configured).
                # Surface the failure instead of returning [] — an empty list would
                # be indistinguishable from a legitimate "no matches" and mask a
                # total search outage from callers.
                logger.error(f"Vector search fallback also failed: {str(ve)}")
                logger.exception(ve)
                raise DatabaseOperationError(ve)
        logger.error(f"Error performing text search: {str(e)}")
        logger.exception(e)
        raise DatabaseOperationError(e)
    except Exception as e:
        logger.error(f"Error performing text search: {str(e)}")
        logger.exception(e)
        raise DatabaseOperationError(e)


async def vector_search(
    keyword: str,
    results: int,
    source: bool = True,
    note: bool = True,
    minimum_score=0.2,
    notebook_ids: Optional[List[str]] = None,
):
    if not keyword:
        raise InvalidInputError("Search keyword cannot be empty")
    try:
        from open_notebook.utils.embedding import generate_embedding

        # Use unified embedding function (handles chunking if query is very long)
        embed = await generate_embedding(keyword)
        search_results = await repo_query(
            """
            SELECT * FROM fn::vector_search($embed, $results, $source, $note, $minimum_score, $notebook_ids);
            """,
            {
                "embed": embed,
                "results": results,
                "source": source,
                "note": note,
                "minimum_score": minimum_score,
                "notebook_ids": _scope_record_ids(notebook_ids),
            },
        )
        # SurrealDB fn::vector_search declares ORDER BY similarity DESC, but the
        # SELECT * FROM fn::... wrapper can still return rows out of rank order.
        # Enforce descending similarity (stable by id for ties) before returning.
        return sorted(
            search_results or [],
            key=lambda item: (
                -float(item.get("similarity") or 0.0),
                str(item.get("id") or ""),
            ),
        )
    except Exception as e:
        logger.error(f"Error performing vector search: {str(e)}")
        logger.exception(e)
        raise DatabaseOperationError(e)
