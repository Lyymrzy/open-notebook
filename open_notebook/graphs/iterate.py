"""Knowledge iteration: turn scattered material into a durable note.

Two modes share one shape - assemble material, write *one* Markdown note whose
first line is its title:

* ``synthesize`` - material (notes and/or sources) becomes a brand new note.
* ``refine`` - one existing note plus new evidence becomes its next revision,
  returned as a proposal the user reviews. The original is never touched here:
  applying a proposal is a separate, explicit step.

The context is pre-assembled by the caller (see ``commands/iterate_commands.py``)
and travels in the human message, not in the template. The template therefore
holds only rules - user-supplied instructions are passed as a plain render
variable, never compiled as template source
(see docs/7-DEVELOPMENT/security.md, GHSA-f35w-wx37-26q7).
"""

from typing import Literal, Optional, Tuple

from ai_prompter import Prompter
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from open_notebook.ai.provision import provision_langchain_model
from open_notebook.exceptions import OpenNotebookError
from open_notebook.utils import clean_thinking_content
from open_notebook.utils.error_classifier import classify_error
from open_notebook.utils.text_utils import extract_text_content

IterateMode = Literal["synthesize", "refine"]

# The model is asked for a leading H1 title instead of a JSON envelope: a long
# Markdown note JSON-escaped by the model is a real source of malformed output,
# and one call is enough this way.
TITLE_PREFIX = "# "

MAX_TITLE_LENGTH = 200


class IterateState(TypedDict):
    """``context`` is the pre-assembled material, ``instructions`` the optional
    user guidance, ``output``/``title`` the parsed model reply."""

    mode: IterateMode
    context: str
    instructions: Optional[str]
    output: str
    title: str


def split_titled_markdown(raw: str) -> Tuple[Optional[str], str]:
    """Split a model reply into (title, body).

    Expects the first non-empty line to be an H1 title, as the templates
    instruct. Returns (None, raw) when it is not - the caller decides whether
    that is fatal or just means "no title".
    """
    text = (raw or "").strip()
    if not text:
        return None, ""

    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(TITLE_PREFIX):
            title = stripped[len(TITLE_PREFIX) :].strip()[:MAX_TITLE_LENGTH]
            body = "\n".join(lines[index + 1 :]).strip()
            return (title or None), body
        break
    return None, text


async def compose_note(state: dict, config: RunnableConfig) -> dict:
    """Render the mode's template and ask the model for one note."""
    try:
        mode = state.get("mode", "synthesize")
        if mode not in ("synthesize", "refine"):
            # Permanent, caller-side bug: never a retryable LLM failure.
            raise ValueError(f"Unknown iterate mode: {mode}")

        system_prompt = Prompter(prompt_template=f"iterate/{mode}").render(data=state)
        material = state.get("context") or ""
        payload = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=material),
        ]

        chain = await provision_langchain_model(
            str(payload),
            config.get("configurable", {}).get("model_id"),
            "transformation",
            max_tokens=8192,
        )
        response = await chain.ainvoke(payload)

        raw = clean_thinking_content(extract_text_content(response.content))
        title, body = split_titled_markdown(raw)
        return {"output": body or raw, "title": title or ""}
    except OpenNotebookError:
        raise
    except Exception as e:
        error_class, user_message = classify_error(e)
        raise error_class(user_message) from e


agent_state = StateGraph(IterateState)
agent_state.add_node("compose", compose_note)  # type: ignore[type-var]
agent_state.add_edge(START, "compose")
agent_state.add_edge("compose", END)

graph = agent_state.compile()
