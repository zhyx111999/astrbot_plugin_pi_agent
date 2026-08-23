"""Formatting and parsing helpers for the Pi legacy compatibility route."""

from .models import SessionInfo, UIRequest


def strip_command_prefix(message: str, prefix: str) -> str:
    """Remove the leading `/prefix` or `prefix` from a message string."""
    text = message.strip()
    for candidate in (f"/{prefix}", prefix):
        if text.startswith(candidate):
            return text[len(candidate) :].strip()
    return text


def parse_subcommand(text: str) -> tuple[str, str]:
    """Split the text after the command prefix into subcommand and the rest.

    Returns a tuple (subcommand, rest). If `text` is empty, both are empty.
    """
    parts = text.split(maxsplit=1)
    if not parts:
        return "", ""
    subcommand = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    return subcommand, rest


def format_session_info(info: SessionInfo) -> str:
    """Format a SessionInfo for display to the user."""
    lines = [
        f"Session: {info.session_id}",
        f"Name: {info.session_name or '(unnamed)'}",
        f"Working directory: {info.cwd}",
        f"File: {info.session_file}",
        f"Messages: {info.message_count}",
    ]
    if info.thinking_level:
        lines.append(f"Thinking level: {info.thinking_level}")
    return "\n".join(lines)


def format_session_list(
    sessions: list[SessionInfo],
    directory: str | None = None,
    page: int = 1,
    page_size: int = 10,
    total: int = 0,
) -> str:
    """Format a page of sessions for display to the user.

    When ``directory`` is provided, the CWD line is hidden because all sessions
    belong to the same directory. Pagination info is appended unless this is
    the only page.
    """
    if not sessions:
        return "No sessions found."

    lines = ["Available sessions:"]
    start_index = (page - 1) * page_size + 1
    for idx, info in enumerate(sessions, start=start_index):
        name = info.session_name or "(unnamed)"
        ts = info.timestamp or "unknown"
        snippet = info.first_message_snippet or "(no preview)"
        lines.append(f"{idx}. {name}")
        lines.append(f"   ID: {info.session_id}")
        if directory is None:
            lines.append(f"   CWD: {info.cwd}")
        lines.append(f"   Messages: {info.message_count}")
        lines.append(f"   Created: {ts}")
        lines.append(f"   First message: {snippet}")

    total_pages = max(1, (total + page_size - 1) // page_size)
    if total_pages > 1:
        if page < total_pages:
            lines.append(f"\nPage {page}/{total_pages}. Use /pi next to view more.")
        else:
            lines.append(f"\nPage {page}/{total_pages}. End of list.")
    return "\n".join(lines)


def format_ui_request(request: UIRequest) -> str:
    """Format a pi extension UI request for display to the user."""
    lines = [f"[Request #{request.local_id}]"]

    if request.title:
        lines.append(f"Title: {request.title}")
    if request.message:
        lines.append(f"Message: {request.message}")

    if request.method == "confirm":
        lines.append(
            f"Reply with: /pi confirm {request.local_id} yes  or  /pi confirm {request.local_id} no"
        )
    elif request.method == "select":
        if request.options:
            lines.append("Options:")
            for idx, option in enumerate(request.options, start=1):
                lines.append(f"  {idx}. {option}")
        lines.append(
            f"Reply with: /pi select {request.local_id} <option>  or  /pi select {request.local_id} <number>"
        )
    elif request.method == "input":
        lines.append(f"Reply with: /pi input {request.local_id} <value>")
    elif request.method == "editor":
        if request.prefill:
            lines.append(f"Prefill: {request.prefill}")
        lines.append(f"Reply with: /pi edit {request.local_id} <text>")
    else:
        lines.append(f"Reply with: /pi input {request.local_id} <value>")

    return "\n".join(lines)


def format_commands_list(commands: list[dict]) -> str:
    """Format the list of pi slash commands for display."""
    if not commands:
        return "No slash commands available."

    entries = []
    for cmd in commands:
        name = cmd.get("name", "unknown")
        description = cmd.get("description", "")
        source = cmd.get("source", "unknown")
        if description:
            entries.append(f"/{name} ({source}) ➡️ {description}")
        else:
            entries.append(f"/{name} ({source})")
    return "Available pi commands:\n\n" + "\n\n".join(entries)


def resolve_select_option(request: UIRequest, value: str) -> str | None:
    """Resolve a user reply for a select request.

    Accepts either the option text or a 1-based index.
    Returns the option text if valid, otherwise None.
    """
    value = value.strip()
    if not request.options:
        return value

    # Try to match by exact text first.
    for option in request.options:
        if value == option:
            return option

    # Then try to interpret as a 1-based index.
    try:
        idx = int(value)
        if 1 <= idx <= len(request.options):
            return request.options[idx - 1]
    except ValueError:
        pass

    return None


def parse_ui_reply_args(rest: str) -> tuple[int | None, str]:
    """Parse the local ID and value from a UI reply command.

    Returns (local_id, value). local_id is None if the ID is invalid.
    """
    parts = rest.split(maxsplit=1)
    if not parts:
        return None, ""
    try:
        local_id = int(parts[0])
    except ValueError:
        return None, rest

    value = parts[1] if len(parts) > 1 else ""
    return local_id, value


# ------------------------------------------------------------------
# Tree helpers
# ------------------------------------------------------------------


def extract_active_branch(tree: list[dict], leaf_id: str | None) -> list[dict]:
    """Return entries on the active branch from root to leaf.

    The tree is a list of root nodes, each with ``entry`` and ``children``.
    """
    if not tree or not leaf_id:
        return []

    node_map = {}

    def _walk(nodes: list[dict]) -> None:
        for node in nodes:
            entry = node.get("entry", {})
            node_map[entry.get("id")] = node
            _walk(node.get("children", []))

    _walk(tree)

    branch = []
    seen = set()
    current_id = leaf_id
    while current_id is not None and current_id not in seen:
        node = node_map.get(current_id)
        if node is None:
            break
        branch.append(node["entry"])
        seen.add(current_id)
        current_id = node["entry"].get("parentId")

    return list(reversed(branch))


def _filter_user_entries(entries: list[dict]) -> list[dict]:
    """Return only entries that are user messages."""
    result = []
    for entry in entries:
        if entry.get("type") != "message":
            continue
        msg = entry.get("message", {})
        if msg.get("role") == "user":
            result.append(entry)
    return result


def _extract_user_text(entry: dict, max_length: int = 80) -> str:
    """Extract a single-line display text from a user message entry."""
    msg = entry.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        text = "".join(parts)
    else:
        text = ""

    text = text.replace("\n", " ").strip()
    if not text:
        return "(empty)"
    if len(text) > max_length:
        return text[: max_length - 1] + "…"
    return text


def format_tree_entries(entries: list[dict], max_length: int = 80) -> str:
    """Format user-only entries on the active branch as numbered lines."""
    user_entries = _filter_user_entries(entries)
    if not user_entries:
        return "No user messages on the active branch."

    lines = ["User messages on the active branch:"]
    for idx, entry in enumerate(user_entries, start=1):
        text = _extract_user_text(entry, max_length=max_length)
        timestamp = entry.get("timestamp", "") or ""
        # Use just the date portion if the timestamp looks ISO-8601.
        if "T" in timestamp:
            timestamp = timestamp.split("T")[0]
        lines.append(f"{idx}. {timestamp} {text}")

    lines.append("\nReply with `/pi tree <number>` to fork from that entry.")
    return "\n".join(lines)


def resolve_tree_entry_id(entries: list[dict], number: int) -> str | None:
    """Map a 1-based display number to the corresponding user entry id."""
    user_entries = _filter_user_entries(entries)
    if 1 <= number <= len(user_entries):
        return user_entries[number - 1].get("id")
    return None
