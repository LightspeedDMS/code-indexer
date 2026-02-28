"""Wiki content rendering service (Stories #281, #282, #289)."""

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .wiki_cache import WikiCache

logger = logging.getLogger(__name__)


class WikiService:
    """Renders markdown articles with front matter parsing and image path rewriting."""

    def render_article(self, file_path: Path, repo_alias: str) -> Dict[str, Any]:
        """Render a markdown file to HTML with metadata extraction."""
        raw_content = file_path.read_text(encoding="utf-8")
        metadata, content = self._strip_front_matter(raw_content)
        content = self._strip_header_block(content, metadata)
        title = self._extract_title(metadata, file_path)
        html = self._render_markdown(content)
        html = self._rewrite_image_paths(html, repo_alias)
        return {"html": html, "title": title, "metadata": metadata}

    def _strip_front_matter(self, content: str) -> Tuple[Dict[str, Any], str]:
        """Parse YAML front matter, return (metadata, content_without_frontmatter)."""
        try:
            import frontmatter

            post = frontmatter.loads(content)
            return dict(post.metadata), post.content
        except (ModuleNotFoundError, ImportError):
            logger.debug(
                "frontmatter module not available, skipping front matter parsing"
            )
            return {}, content
        except Exception:
            logger.warning("Failed to parse front matter, treating as plain content")
            return {}, content

    def _strip_header_block(self, content: str, metadata: Dict[str, Any] = None) -> str:
        """Strip structured header block fields (Article Number/Title/Status) from body.

        Summary is preserved in the body. Extracted values are merged into metadata
        if a dict is provided. Handles bold markdown markers (**field:**) in addition
        to plain text (field:).
        """
        lines = content.split("\n")
        # Fields to strip from body (summary is intentionally absent — keep it)
        strip_fields = {
            "article number:": "article_number",
            "title:": None,  # Already shown as H1, just strip
            "publication status:": "publication_status",
        }
        result_lines = []
        i = 0
        # Preserve leading blank lines
        while i < len(lines) and not lines[i].strip():
            result_lines.append(lines[i])
            i += 1

        found_header = False
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            # Remove bold markers for field matching
            clean = stripped.replace("**", "").strip()
            lower_clean = clean.lower()

            matched = False
            for field_prefix, meta_key in strip_fields.items():
                if lower_clean.startswith(field_prefix):
                    matched = True
                    found_header = True
                    # Extract value and store in metadata if key provided
                    if meta_key and metadata is not None:
                        value = clean[len(field_prefix) :].strip()
                        metadata[meta_key] = value
                    break

            if matched:
                i += 1
                continue
            elif stripped == "---" and found_header:
                # Skip the --- separator that follows the header fields
                i += 1
                continue
            elif stripped == "" and found_header:
                # Skip blank lines within the header block
                i += 1
                continue
            else:
                # Not a header field — keep this and all remaining lines
                result_lines.extend(lines[i:])
                break

        if not result_lines or all(not line.strip() for line in result_lines):
            # Everything was leading whitespace; return remaining content
            return "\n".join(lines[i:]) if i < len(lines) else ""

        return "\n".join(result_lines).lstrip("\n")

    def _extract_title(self, metadata: Dict[str, Any], file_path: Path) -> str:
        """Extract title from front matter or derive from filename."""
        if metadata.get("title"):
            return str(metadata["title"])
        stem = file_path.stem
        return stem.replace("-", " ").replace("_", " ").title()

    def _render_markdown(self, content: str) -> str:
        """Render markdown to HTML using markdown-it-py."""
        from markdown_it import MarkdownIt

        md = MarkdownIt("commonmark", {"html": True})
        md.enable(["table", "strikethrough"])
        html = md.render(content)
        html = self._add_heading_ids(html)
        return html

    def _add_heading_ids(self, html: str) -> str:
        """Add IDs to heading elements for anchor link support."""

        def make_id(match: re.Match) -> str:
            tag = match.group(1)
            content_text = match.group(2)
            text = re.sub(r"<[^>]+>", "", content_text)
            slug = re.sub(r"[^\w\s-]", "", text.lower()).strip()
            slug = re.sub(r"[-\s]+", "-", slug)
            return f'<h{tag} id="{slug}">{content_text}</h{tag}>'

        return re.sub(r"<h([1-6])>(.*?)</h\1>", make_id, html, flags=re.DOTALL)

    def _rewrite_image_paths(self, html: str, repo_alias: str) -> str:
        """Rewrite relative image paths to wiki _assets route."""

        def rewrite_src(match: re.Match) -> str:
            prefix = match.group(1)
            src = match.group(2)
            suffix = match.group(3)
            if src.startswith(("http://", "https://", "/wiki/")):
                return match.group(0)
            clean_path = re.sub(r"^(\.\./)+", "", src)
            return f"{prefix}/wiki/{repo_alias}/_assets/{clean_path}{suffix}"

        return re.sub(r'(<img[^>]*\ssrc=["\'])([^"\']+)(["\'])', rewrite_src, html)

    # ------------------------------------------------------------------
    # Story #289: Article metadata display panel
    # ------------------------------------------------------------------

    def format_date_human_readable(self, date_value: Any) -> Optional[str]:
        """Format a date value to human-readable string like 'March 15, 2024'.

        Accepts: datetime objects, date objects, ISO 8601 strings (YYYY-MM-DD or
        YYYY-MM-DDTHH:MM:SS), or any string parseable by datetime.fromisoformat().
        Returns None on parse failure or None/empty input.
        """
        if date_value is None:
            return None
        if isinstance(date_value, datetime):
            return date_value.strftime("%B %-d, %Y")
        if isinstance(date_value, date):
            return date_value.strftime("%B %-d, %Y")
        value = str(date_value).strip()
        if not value:
            return None
        # Strip time component if present (e.g. "2024-03-15T10:30:00")
        date_part = value.split("T")[0].split(" ")[0]
        try:
            parsed = datetime.strptime(date_part, "%Y-%m-%d")
            return parsed.strftime("%B %-d, %Y")
        except ValueError:
            logger.warning(
                "format_date_human_readable: cannot parse date %r", date_value
            )
            return None

    # Keys to exclude from the metadata panel (internal / non-display fields)
    _METADATA_SKIP_KEYS = frozenset(
        {
            "visibility_class",
        }
    )

    # Human-friendly labels for well-known frontmatter keys
    _METADATA_LABELS: Dict[str, str] = {
        "original_article": "Salesforce Article",
        "article_number": "Salesforce Article",
        "publication_status": "Status",
        "created": "Created",
        "modified": "Modified",
        "updated": "Modified",
        "views": "Salesforce Views",
        "real_views": "Views",
        "visibility": "Visibility",
        "category": "Category",
        "draft": "Draft",
        "title": "Title",
        "author": "Author",
        "tags": "Tags",
        "description": "Description",
    }

    def prepare_metadata_context(
        self,
        metadata: Dict[str, Any],
        repo_alias: str,
        article_path: str,
        wiki_cache: "WikiCache",
    ) -> List[tuple]:
        """Prepare template context for the metadata panel (Story #289).

        Returns a list of (label, value) tuples for display.  An empty list
        signals to the template not to render the panel.
        """
        # Build a working dict of all displayable fields
        fields: Dict[str, Any] = {}

        # Copy all frontmatter keys
        for key, value in metadata.items():
            if key in self._METADATA_SKIP_KEYS:
                continue
            fields[key] = value

        # Normalise 'updated' → 'modified'
        if "updated" in fields and "modified" not in fields:
            fields["modified"] = fields.pop("updated")
        elif "updated" in fields:
            del fields["updated"]

        # Normalise 'original_article' → 'article_number'
        if "original_article" in fields and "article_number" not in fields:
            fields["article_number"] = fields.pop("original_article")
        elif "original_article" in fields:
            del fields["original_article"]

        # Draft flag → visibility
        if fields.get("draft") is True:
            fields["visibility"] = "draft"
        fields.pop("draft", None)

        # Format date fields
        for date_key in ("created", "modified"):
            if date_key in fields and fields[date_key] is not None:
                formatted = self.format_date_human_readable(fields[date_key])
                if formatted:
                    fields[date_key] = formatted
                else:
                    del fields[date_key]

        # Add view count from DB
        real_views = wiki_cache.get_view_count(repo_alias, article_path)
        if real_views > 0:
            fields["real_views"] = real_views

        # Build (label, value) list — strip empty values
        # Article number always first
        result: List[tuple] = []
        if "article_number" in fields:
            val = str(fields.pop("article_number")).strip()
            if val:
                result.append(
                    (self._METADATA_LABELS.get("article_number", "Article"), val)
                )

        for key, value in fields.items():
            str_value = str(value).strip() if value is not None else ""
            if not str_value:
                continue
            label = self._METADATA_LABELS.get(key, key.replace("_", " ").title())
            result.append((label, str_value))

        return result

    # ------------------------------------------------------------------
    # Story #282: Sidebar navigation, link rewriting, breadcrumbs
    # ------------------------------------------------------------------

    # Story #288: fallback category name for articles without a 'category' front matter field
    UNCATEGORIZED_LABEL = "Uncategorized"

    def build_sidebar_tree(
        self, repo_dir: Path, repo_alias: str
    ) -> List[Dict[str, Any]]:
        """Build hierarchical sidebar tree from repo's markdown files.

        All articles are placed into group['categories']. Articles without a
        'category' front matter field are assigned to the 'Uncategorized' category
        (Story #288 AC1). group['articles'] is kept for structural compatibility but
        is always empty.
        """
        tree: Dict[str, Dict[str, Any]] = {}
        for md_file in sorted(repo_dir.rglob("*.md")):
            rel = md_file.relative_to(repo_dir)
            if any(part.startswith(".") for part in rel.parts):
                continue
            try:
                raw = md_file.read_text(encoding="utf-8")
                metadata, _ = self._strip_front_matter(raw)
            except Exception as e:
                logger.warning("Failed to parse front matter for %s: %s", md_file, e)
                metadata = {}
            title = self._extract_title(metadata, md_file)
            raw_category = metadata.get("category")
            category = str(raw_category).strip() if raw_category is not None else ""
            # Normalize empty/missing category to the Uncategorized fallback
            if not category:
                category = self.UNCATEGORIZED_LABEL
            article_path = str(rel.with_suffix(""))
            group_key = rel.parts[0] if len(rel.parts) > 1 else ""
            if group_key not in tree:
                tree[group_key] = {
                    "name": group_key or "Root",
                    "path": group_key,
                    "articles": [],  # Always empty — kept for structural compatibility
                    "categories": {},
                }
            article = {"title": title, "path": article_path, "slug": article_path}
            tree[group_key]["categories"].setdefault(category, [])
            tree[group_key]["categories"][category].append(article)
        for group in tree.values():
            for cat_articles in group["categories"].values():
                cat_articles.sort(key=lambda a: a["title"].lower())
        return sorted(tree.values(), key=lambda g: g["name"].lower())

    def rewrite_links(self, html: str, repo_alias: str, current_dir: str) -> str:
        """Rewrite inter-article links in rendered HTML."""

        def _rewrite_href(match: re.Match) -> str:
            full_tag = match.group(0)
            quote_char = match.group(2)
            href = match.group(3)
            if href.startswith("#"):
                return full_tag
            if href.startswith(("http://", "https://")):
                if "target=" not in full_tag:
                    return full_tag[:-1] + ' target="_blank" rel="noopener">'
                return full_tag
            if href.startswith("/wiki/"):
                return full_tag
            if href.startswith("/articles/"):
                return full_tag
            if "/" in href:
                new_href = f"/wiki/{repo_alias}/{href}"
            else:
                if current_dir:
                    new_href = f"/wiki/{repo_alias}/{current_dir}/{href}"
                else:
                    new_href = f"/wiki/{repo_alias}/{href}"
            return full_tag.replace(
                f"href={quote_char}{href}{quote_char}",
                f"href={quote_char}{new_href}{quote_char}",
                1,
            )

        return re.sub(
            r'(<a\b[^>]*\bhref=(["\'])([^"\']*)\2[^>]*>)',
            _rewrite_href,
            html,
        )

    def build_breadcrumbs(
        self, article_path: str, repo_alias: str
    ) -> List[Dict[str, Any]]:
        """Build breadcrumb trail from article path."""
        crumbs: List[Dict[str, Any]] = [
            {"label": f"{repo_alias} Wiki Home", "url": f"/wiki/{repo_alias}/"}
        ]
        if not article_path:
            return crumbs
        parts = article_path.split("/")
        for i, part in enumerate(parts[:-1]):
            url = f"/wiki/{repo_alias}/{'/'.join(parts[: i + 1])}/"
            crumbs.append({"label": part, "url": url})
        last = parts[-1].replace("-", " ").replace("_", " ").title()
        crumbs.append({"label": last, "url": None})
        return crumbs

    # ------------------------------------------------------------------
    # Story #287: Article view tracking - front matter population
    # ------------------------------------------------------------------

    def populate_views_from_front_matter(
        self, repo_alias: str, repo_path: Path, wiki_cache: "WikiCache"
    ) -> None:
        """Scan all .md files in repo_path and seed wiki_article_views from front matter.

        Only runs when no existing view records exist for this repo (AC5).
        Skips files without a numeric 'views' field in front matter (AC2).
        Skips hidden directories (any path component starting with '.').
        The article_path stored is the relative path from repo root without .md extension.
        """
        existing = wiki_cache.get_all_view_counts(repo_alias)
        if existing:
            logger.debug(
                "populate_views_from_front_matter: skipping %s, %d records already exist",
                repo_alias,
                len(existing),
            )
            return

        for md_file in sorted(repo_path.rglob("*.md")):
            rel = md_file.relative_to(repo_path)
            if any(part.startswith(".") for part in rel.parts):
                continue
            try:
                raw = md_file.read_text(encoding="utf-8")
                metadata, _ = self._strip_front_matter(raw)
            except Exception as exc:
                logger.warning(
                    "populate_views_from_front_matter: failed to read %s: %s",
                    md_file,
                    exc,
                )
                continue

            views_raw = metadata.get("views")
            if views_raw is None:
                continue
            try:
                views = int(views_raw)
            except (TypeError, ValueError):
                logger.warning(
                    "populate_views_from_front_matter: non-numeric views field in %s: %r",
                    md_file,
                    views_raw,
                )
                continue
            if views < 0:
                logger.warning(
                    "populate_views_from_front_matter: negative views value %d in %s, skipping",
                    views,
                    md_file,
                )
                continue

            article_path = str(rel.with_suffix(""))
            try:
                wiki_cache.insert_initial_views(repo_alias, article_path, views)
            except Exception as exc:
                logger.warning(
                    "populate_views_from_front_matter: failed to insert views for %s/%s: %s",
                    repo_alias,
                    article_path,
                    exc,
                )
