"""Validation, rendering and compatibility helpers for Dashboard DMs."""

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import discord

VARIABLE = re.compile(r"\{([a-zA-Z_]+)\}")
ALLOWED_EMBED_KEYS = {
    "title", "description", "color", "author", "thumbnail", "image", "fields", "footer", "timestamp"
}
MAX_CONTENT_LENGTH = 2000
MAX_REASON_LENGTH = 1000


def substitute_variables(value, variables):
    if isinstance(value, str):
        return VARIABLE.sub(lambda match: str(variables.get(match.group(1), match.group(0))), value)
    if isinstance(value, list):
        return [substitute_variables(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: substitute_variables(item, variables) for key, item in value.items()}
    return value


def _valid_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    parsed = urlsplit(value)
    return parsed.scheme in {"https", "http"} and bool(parsed.netloc) and not parsed.username and not parsed.password


def render_embed(design, variables):
    if not isinstance(design, dict) or set(design) - ALLOWED_EMBED_KEYS:
        raise ValueError("Embed fields are invalid")
    rendered = substitute_variables(design, variables)
    data = {}
    for key in ("title", "description"):
        value = rendered.get(key)
        if value:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            data[key] = value.strip()
            if len(data[key]) > (256 if key == "title" else 4096):
                raise ValueError(f"{key} exceeds Discord's length limit")
    color = rendered.get("color")
    if color:
        if not isinstance(color, str) or not re.fullmatch(r"#?[0-9a-fA-F]{6}", color):
            raise ValueError("Color must be a six-digit hex value")
        data["color"] = int(color.lstrip("#"), 16)

    author = rendered.get("author")
    if author:
        if not isinstance(author, dict) or not isinstance(author.get("name"), str) or not author["name"].strip():
            raise ValueError("Author needs a name")
        author_data = {"name": author["name"][:256]}
        if author.get("url"):
            if not _valid_url(author["url"]):
                raise ValueError("Author URL is invalid")
            author_data["url"] = author["url"]
        if author.get("icon_url"):
            if not _valid_url(author["icon_url"]):
                raise ValueError("Author icon URL is invalid")
            author_data["icon_url"] = author["icon_url"]
        data["author"] = author_data

    for key in ("thumbnail", "image"):
        value = rendered.get(key)
        if value:
            url = value.get("url") if isinstance(value, dict) else value
            if not _valid_url(url):
                raise ValueError(f"{key} URL is invalid")
            data[key] = {"url": url}

    fields = rendered.get("fields", [])
    if not isinstance(fields, list) or len(fields) > 25:
        raise ValueError("Embed supports at most 25 fields")
    data["fields"] = []
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError("Embed field is invalid")
        name = str(field.get("name", "")).strip()
        value = str(field.get("value", "")).strip()
        if not name or not value or len(name) > 256 or len(value) > 1024:
            raise ValueError("Embed fields need a name (max 256) and value (max 1024)")
        data["fields"].append({"name": name, "value": value, "inline": bool(field.get("inline", False))})

    footer = rendered.get("footer")
    if footer:
        if isinstance(footer, str):
            footer = {"text": footer}
        if not isinstance(footer, dict) or not str(footer.get("text", "")).strip():
            raise ValueError("Footer needs text")
        footer_data = {"text": str(footer["text"])[:2048]}
        if footer.get("icon_url"):
            if not _valid_url(footer["icon_url"]):
                raise ValueError("Footer icon URL is invalid")
            footer_data["icon_url"] = footer["icon_url"]
        data["footer"] = footer_data

    if rendered.get("timestamp"):
        data["timestamp"] = datetime.now(timezone.utc).isoformat()
    if not any(data.get(key) for key in ("title", "description", "fields", "image", "thumbnail")):
        raise ValueError("Embed needs a title, description, field, or image")
    try:
        embed = discord.Embed.from_dict(data)
    except (TypeError, ValueError) as exc:
        raise ValueError("Embed design is invalid") from exc
    if len(embed) > 6000:
        raise ValueError("Embed text exceeds Discord's 6000-character limit")
    return embed


def split_message_design(design):
    """Return canonical ``content`` + ``embed`` pieces while keeping old templates compatible."""
    if not isinstance(design, dict):
        raise ValueError("Message design is invalid")
    if "embed" in design:
        content = design.get("content", "")
        embed_design = design.get("embed")
    else:
        content = design.get("content", "")
        embed_design = {key: value for key, value in design.items() if key != "content"}
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise ValueError("Message content must be text")
    content = content.strip()
    if len(content) > MAX_CONTENT_LENGTH:
        raise ValueError("Message content exceeds Discord's 2000-character limit")
    if embed_design is not None and not isinstance(embed_design, dict):
        raise ValueError("Embed design is invalid")
    if not content and not embed_design:
        raise ValueError("Message needs text or an Embed")
    return content, embed_design


def render_message(design, variables):
    """Render a Dashboard DM message. Returns discord-compatible kwargs."""
    content, embed_design = split_message_design(design)
    rendered_content = substitute_variables(content, variables).strip()
    if len(rendered_content) > MAX_CONTENT_LENGTH:
        raise ValueError("Rendered message content exceeds Discord's 2000-character limit")
    embed = render_embed(embed_design, variables) if embed_design else None
    if not rendered_content and embed is None:
        raise ValueError("Message needs text or an Embed")
    return {"content": rendered_content or None, "embed": embed}


def message_to_dict(message):
    return {
        "content": message.get("content") or "",
        "embed": message["embed"].to_dict() if message.get("embed") else None,
    }
