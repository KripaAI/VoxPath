from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.client import Client

import memory
import railway
import services

mcp = FastMCP(
    name="VoxPath MCP",
    instructions=(
        "Expose VoxPath's capabilities as MCP tools: news, Indian "
        "markets, and Indian Railways lookups. This server is the "
        "single tool registry used by both the text agent and the live voice agent."
    ),
    version="0.2.0",
)


async def call_tool(name: str, arguments: dict | None = None):
    """Invoke an MCP tool by name via an in-process client. Single executor used
    by the HTTP API and the voice bridge so all tools run through this server."""
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments or {})
    if getattr(result, "is_error", False):
        raise RuntimeError(f"MCP tool '{name}' returned an error.")
    if result.structured_content is not None:
        return result.structured_content
    if result.data is not None:
        return result.data
    if result.content:
        return result.content[0].text
    return None


@mcp.resource("voxpath://health", name="health_status", description="Current service health")
def health_status() -> dict[str, str]:
    return {"status": "ok"}


@mcp.resource(
    "voxpath://capabilities",
    name="capabilities",
    description="List of capabilities currently available through the VoxPath MCP server",
)
def capabilities() -> dict[str, object]:
    return {
        "server": "VoxPath MCP",
        "tools": [
            "get_stock_price",
            "list_news_sources",
            "crawl_news",
            "get_latest_headlines",
            "get_latest_news",
            "get_pnr_status",
            "resolve_station_code",
            "get_live_train_status",
            "get_train_schedule",
            "search_trains",
            "check_seat_availability",
            "get_fare",
            "save_memory",
            "search_memories",
            "update_memory",
            "delete_memory",
            "list_memories",
        ],
        "resources": [
            "voxpath://health",
            "voxpath://capabilities",
            "voxpath://news/sources",
        ],
    }


@mcp.resource(
    "voxpath://news/sources",
    name="news_sources",
    description="List of configured news sources available to the VoxPath crawler",
)
def news_sources() -> dict[str, object]:
    return {"sources": services.list_news_sources()}


@mcp.tool(
    description=(
        "Get the latest price of ANY Indian-listed stock by company name or ticker, "
        "or 'NIFTY' for the index. Pass the company name or symbol exactly as given "
        "by the user — never guess or convert company names into ticker symbols."
    )
)
def get_stock_price(ticker: str) -> dict[str, str]:
    return services.get_stock_price_smart(ticker)


@mcp.tool(description="List the configured news sources available to the crawler")
def list_news_sources() -> dict[str, object]:
    return {"sources": services.list_news_sources()}


@mcp.tool(description="Run the integrated news crawler across all or matching sources")
def crawl_news(source_query: str | None = None, dry_run: bool = False, limit: int | None = None) -> dict[str, object]:
    return services.crawl_news(source_query=source_query, dry_run=dry_run, limit=limit)


@mcp.tool(description="Read the latest stored headlines, optionally filtered by source or language")
def get_latest_headlines(limit: int = 10, source: str | None = None, language: str | None = None) -> dict[str, object]:
    return services.get_latest_headlines(limit=limit, source=source, language=language)


@mcp.tool(
    description=(
        "Fetch the latest Indian news. Each headline comes with its article url, "
        "source, publish time and usually a summary. Filters (all optional): "
        "region, an Indian state or city such as 'Bihar', 'Uttar Pradesh' or "
        "'Patna'; category, one of india, world, business, sports, technology, "
        "science, entertainment, regional; topic, free-text words that must "
        "appear, such as 'cricket' or 'RBI'; language, 'en' or 'hi'. Omit all for "
        "top national headlines. limit is 1-10. If the result has a note, the "
        "headlines are NOT what was asked for: say so."
    )
)
def get_latest_news(
    region: str | None = None,
    topic: str | None = None,
    category: str | None = None,
    language: str | None = None,
    limit: int = 5,
) -> dict[str, object]:
    return services.get_latest_news(region=region, topic=topic, category=category, language=language, limit=limit)



@mcp.tool(description="Check a train ticket booking status by its 10-digit PNR number.")
def get_pnr_status(pnr: str) -> dict[str, object]:
    return railway.get_pnr_status(pnr)


@mcp.tool(description="Look up the IRCTC station code(s) for a city or station name (e.g. 'Delhi' -> 'NDLS').")
def resolve_station_code(station_name: str) -> dict[str, object]:
    return railway.resolve_station_code(station_name)


@mcp.tool(description="Get the live running status of a train: current location, delay, running status. Date optional (DD-MM-YYYY).")
def get_live_train_status(train_number: str, date: str | None = None) -> dict[str, object]:
    return railway.get_live_train_status(train_number, date)


@mcp.tool(description="Get a train's full route and timetable with all stops and timings.")
def get_train_schedule(train_number: str) -> dict[str, object]:
    return railway.get_train_schedule(train_number)


@mcp.tool(description="Find all trains between two stations or cities (accepts station codes or city names). Date optional (DD-MM-YYYY).")
async def search_trains(source: str, destination: str, date: str | None = None) -> dict[str, object]:
    return await railway.search_trains(source, destination, date)


@mcp.tool(description="Check class-wise seat availability between two station codes on a date (DD-MM-YYYY).")
def check_seat_availability(source: str, destination: str, date: str, train_number: str | None = None) -> dict[str, object]:
    return railway.check_seat_availability(source, destination, date, train_number)


@mcp.tool(description="Get class-wise ticket fares for a train between two station codes. Date optional (DD-MM-YYYY).")
def get_fare(train_number: str, source: str, destination: str, date: str | None = None) -> dict[str, object]:
    return railway.get_fare(train_number, source, destination, date)





@mcp.tool(
    description=(
        "Remember one durable fact about the user for future "
        "conversations (a preference, a name, a home station, "
        "a recurring interest). Only for things still true "
        "next week -- never for the answer to the current question."
    )
)
async def save_memory(text: str) -> dict[str, object]:
    return await memory.save_memory(text)


@mcp.tool(
    description=(
        "Recall facts saved about the user in earlier conversations. Call "
        "this before answering anything that depends on who the user is or "
        "what they prefer, and when they refer to something they told you before."
    )
)
async def search_memories(query: str, limit: int = 3) -> dict[str, object]:
    return await memory.search_memories(query, limit)


@mcp.tool(
    description=(
        "Correct a fact the user has changed, for example a move to a "
        "new city or a new seat preference. Search first to get the "
        "memory id, then call this with the new wording. Prefer it over "
        "saving a second memory: two contradictory facts both come back later."
    )
)
async def update_memory(memory_id: str, text: str) -> dict[str, object]:
    return await memory.update_memory(memory_id, text)


@mcp.tool(
    description=(
        "Forget one saved fact, when the user asks you to or when it is no "
        "longer true and has no replacement. Search first to get the memory id."
    )
)
async def delete_memory(memory_id: str) -> dict[str, object]:
    return await memory.delete_memory(memory_id)


@mcp.tool(
    description=(
        "List everything saved about the user, newest first. "
        "Use when they ask what you remember or know about them."
    )
)
async def list_memories(limit: int = 50) -> dict[str, object]:
    return await memory.list_memories(limit)
