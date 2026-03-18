import asyncio
import json
import logging

from duckduckgo_search import DDGS
from mistralai.client import Mistral

log = logging.getLogger("mistral-bot")

MISTRAL_API_KEY: str = ""
MISTRAL_MODEL: str = ""
MAX_TOOL_ROUNDS: int = 3

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information. Use this for recent events, facts you're unsure about, or anything that may have changed after your training data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    }
                },
                "required": ["query"],
            },
        },
    }
]

mistral_client: Mistral | None = None


def init(api_key: str, model: str, max_tool_rounds: int) -> None:
    global MISTRAL_API_KEY, MISTRAL_MODEL, MAX_TOOL_ROUNDS, mistral_client
    MISTRAL_API_KEY = api_key
    MISTRAL_MODEL = model
    MAX_TOOL_ROUNDS = max_tool_rounds
    mistral_client = Mistral(api_key=MISTRAL_API_KEY)


def web_search(query: str) -> str:
    log.info("Web search: %s", query)
    try:
        results = DDGS().text(query, max_results=5)
        if not results:
            return json.dumps({"results": "No results found."})
        formatted = [
            {
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            }
            for r in results
        ]
        return json.dumps({"results": formatted})
    except Exception:
        log.exception("Web search failed")
        return json.dumps({"error": "Web search failed."})


TOOL_FUNCTIONS = {"web_search": web_search}


async def call_mistral(messages: list[dict]) -> str:
    """Call Mistral with function calling, handling tool call loops."""
    for _ in range(MAX_TOOL_ROUNDS):
        response = await asyncio.to_thread(
            lambda: mistral_client.chat.complete(
                model=MISTRAL_MODEL,
                messages=messages,
                tools=TOOLS,
            )
        )

        choice = response.choices[0]

        if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
            messages.append(choice.message)

            for tool_call in choice.message.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)

                if fn_name in TOOL_FUNCTIONS:
                    result = await asyncio.to_thread(TOOL_FUNCTIONS[fn_name], **fn_args)
                else:
                    result = json.dumps({"error": f"Unknown tool: {fn_name}"})

                messages.append(
                    {
                        "role": "tool",
                        "name": fn_name,
                        "content": result,
                        "tool_call_id": tool_call.id,
                    }
                )
        else:
            content = choice.message.content
            if isinstance(content, list):
                return "".join(
                    chunk.text if hasattr(chunk, "text") else str(chunk)
                    for chunk in content
                )
            return content or ""

    return "I ran out of search attempts. Please try rephrasing your question."
