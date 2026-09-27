"""Frontier exploration: ask the model what the user should explore next.

Given the leaf notes of a notebook, this proposes questions worth answering
(see `prompts/explore/frontier.jinja`). The output is structured JSON, so it
follows the `graphs/ask.py` pattern: a PydanticOutputParser supplies the format
instructions, the model is provisioned with JSON structured output, and the
reply is parsed after `clean_thinking_content` strips any reasoning tags.

Proposals are suggestions only - nothing is generated here. Answering one is a
separate step the user triggers.
"""

from typing import List, Literal, Optional

from ai_prompter import Prompter
from langchain_core.output_parsers.pydantic import PydanticOutputParser
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from open_notebook.ai.provision import provision_langchain_model
from open_notebook.exceptions import OpenNotebookError
from open_notebook.utils import clean_thinking_content
from open_notebook.utils.error_classifier import classify_error
from open_notebook.utils.text_utils import extract_text_content

FRONTIER_MAX_TOKENS = 4096


class Suggestion(BaseModel):
    """One proposed direction, tied to the leaf it grows out of."""

    note_id: str = Field(description="ID of the leaf note this question belongs to")
    question: str = Field(description="The question worth exploring next")
    rationale: Optional[str] = Field(
        None, description="Why this is worth exploring, in one or two sentences"
    )
    kind: Literal["gap", "depth", "crosslink", "contradiction"] = Field(
        "gap", description="What kind of gap this fills"
    )
    score: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="Confidence this is worth exploring next"
    )


class FrontierPlan(BaseModel):
    suggestions: List[Suggestion] = Field(default_factory=list)


class ExploreState(TypedDict):
    context: str
    questions_per_leaf: int
    plan: FrontierPlan


async def propose_explorations(state: ExploreState, config: RunnableConfig) -> dict:
    """Ask the model for the notebook's next questions."""
    try:
        parser: PydanticOutputParser[FrontierPlan] = PydanticOutputParser(
            pydantic_object=FrontierPlan
        )
        system_prompt = Prompter(
            prompt_template="explore/frontier", parser=parser  # type: ignore[arg-type]
        ).render(
            data=state  # type: ignore[arg-type]
        )

        # "tools" is the model type the repo already provisions for JSON-
        # structured planning (see graphs/ask.py); `structured` is what makes
        # the provider emit JSON instead of prose.
        model = await provision_langchain_model(
            system_prompt,
            config.get("configurable", {}).get("model_id"),
            "tools",
            max_tokens=FRONTIER_MAX_TOKENS,
            structured=dict(type="json"),
        )
        ai_message = await model.ainvoke(system_prompt)
        content = clean_thinking_content(extract_text_content(ai_message.content))

        plan = parser.parse(content)
        # A thinking model that spends its budget reasoning returns a valid but
        # empty plan; that is a legitimate "nothing to propose", not an error.
        plan.suggestions = [
            suggestion
            for suggestion in plan.suggestions
            if suggestion.question.strip() and suggestion.note_id.strip()
        ]
        return {"plan": plan}
    except OpenNotebookError:
        raise
    except Exception as e:
        error_class, user_message = classify_error(e)
        raise error_class(user_message) from e


agent_state = StateGraph(ExploreState)
agent_state.add_node("propose", propose_explorations)  # type: ignore[type-var]
agent_state.add_edge(START, "propose")
agent_state.add_edge("propose", END)

graph = agent_state.compile()
