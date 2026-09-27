"""Knowledge graph endpoint.

A pure projection of what is already stored (notes plus the typed edges between
them), so there is nothing to keep in sync: the graph cannot disagree with the
notes it draws.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from api.models import KnowledgeGraphResponse
from open_notebook.domain.knowledge_graph import (
    DEFAULT_MAX_NODES,
    build_knowledge_graph,
)
from open_notebook.domain.notebook import Notebook
from open_notebook.exceptions import (
    InvalidInputError,
    NotFoundError,
    OpenNotebookError,
)

router = APIRouter()


@router.get("/graph", response_model=KnowledgeGraphResponse)
async def get_knowledge_graph(
    notebook_id: Optional[str] = Query(
        None, description="Restrict the graph to one notebook (default: everything)"
    ),
    include_explorations: bool = Query(
        True, description="Include proposal nodes anchored on notes"
    ),
    max_nodes: int = Query(
        DEFAULT_MAX_NODES,
        ge=1,
        le=5000,
        description="Node cap; the response reports when it was hit",
    ),
):
    """Return nodes and edges for the knowledge graph.

    Nodes are notes (`includes` = tree, `relates_to` = web) and, optionally,
    exploration points anchored on leaves. Links reaching outside the requested
    scope are counted rather than returned, so a caller can distinguish "no
    connections" from "connections to another notebook".
    """
    try:
        if notebook_id:
            # A typo'd id would otherwise render as a legitimately empty graph,
            # which is indistinguishable from "this notebook has no notes".
            await Notebook.get(notebook_id)

        graph = await build_knowledge_graph(
            notebook_id=notebook_id,
            include_explorations=include_explorations,
            max_nodes=max_nodes,
        )
        return KnowledgeGraphResponse(**graph)
    except HTTPException:
        raise
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error building the knowledge graph: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error building the knowledge graph: {str(e)}"
        )
