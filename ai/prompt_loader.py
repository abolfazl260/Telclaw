"""Safe category-specific extraction prompt loading."""

from pathlib import Path


PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
PROMPT_FILES = {
    "housinglist": "housinglist.txt",
    "transferlist": "transferlist.txt",
    "joblist": "joblist.txt",
}


def load_prompt(category: str) -> str:
    """Load the prompt for a supported category without accepting arbitrary paths."""
    try:
        filename = PROMPT_FILES[category]
    except KeyError as exc:
        raise ValueError(f"Unsupported extraction category: {category!r}") from exc

    path = PROMPT_DIR / filename
    if not path.is_file():
        raise RuntimeError(f"Missing extraction prompt for category '{category}': {path}")

    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Unable to load extraction prompt for category '{category}': {path}") from exc


def render_prompt(category: str, message_text: str = "") -> str:
    """Render a category prompt with only the supported dynamic values."""
    prompt = load_prompt(category)
    try:
        return prompt.format(category=category, message_text=message_text)
    except KeyError as exc:
        raise RuntimeError(f"Unsupported placeholder in extraction prompt for '{category}': {exc}") from exc


__all__ = ["PROMPT_DIR", "PROMPT_FILES", "load_prompt", "render_prompt"]
