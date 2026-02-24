"""Wiki content rendering service (Stories #281, #282)."""
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


class WikiService:
    """Renders markdown articles with front matter parsing and image path rewriting."""

    def render_article(self, file_path: Path, repo_alias: str) -> Dict[str, Any]:
        """Render a markdown file to HTML with metadata extraction."""
        raw_content = file_path.read_text(encoding="utf-8")
        metadata, content = self._strip_front_matter(raw_content)
        content = self._strip_header_block(content)
        title = self._extract_title(metadata, file_path)
        html = self._render_markdown(content)
        html = self._rewrite_image_paths(html, repo_alias)
        return {"html": html, "title": title, "metadata": metadata}

    def _strip_front_matter(self, content: str) -> Tuple[Dict[str, Any], str]:
        """Parse YAML front matter, return (metadata, content_without_frontmatter)."""
        import frontmatter
        try:
            post = frontmatter.loads(content)
            return dict(post.metadata), post.content
        except Exception:
            logger.warning("Failed to parse front matter, treating as plain content")
            return {}, content

    def _strip_header_block(self, content: str) -> str:
        """Strip structured header block (Article Number/Title/Status/Summary + ---)."""
        lines = content.split("\n")
        header_keywords = {"article number:", "title:", "publication status:", "summary:"}
        i = 0
        while i < len(lines) and not lines[i].strip():
            i += 1
        found_header_field = False
        start = i
        while i < len(lines):
            line = lines[i].strip()
            lower_line = line.lower()
            if any(lower_line.startswith(kw) for kw in header_keywords):
                found_header_field = True
                i += 1
            elif line == "---" and found_header_field:
                return "\n".join(lines[i + 1 :]).lstrip("\n")
            elif line == "" and found_header_field:
                i += 1
            elif not found_header_field:
                break
            else:
                break
        return content

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
    # Story #282: Sidebar navigation, link rewriting, breadcrumbs
    # ------------------------------------------------------------------

    def build_sidebar_tree(self, repo_dir: Path, repo_alias: str) -> List[Dict[str, Any]]:
        """Build hierarchical sidebar tree from repo's markdown files."""
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
            category = str(metadata.get("category", "")).strip()
            article_path = str(rel.with_suffix(""))
            group_key = rel.parts[0] if len(rel.parts) > 1 else ""
            if group_key not in tree:
                tree[group_key] = {
                    "name": group_key or "Root",
                    "path": group_key,
                    "articles": [],
                    "categories": {},
                }
            article = {"title": title, "path": article_path, "slug": article_path}
            if category:
                tree[group_key]["categories"].setdefault(category, [])
                tree[group_key]["categories"][category].append(article)
            else:
                tree[group_key]["articles"].append(article)
        for group in tree.values():
            group["articles"].sort(key=lambda a: a["title"].lower())
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

    def build_breadcrumbs(self, article_path: str, repo_alias: str) -> List[Dict[str, Any]]:
        """Build breadcrumb trail from article path."""
        crumbs: List[Dict[str, Any]] = [
            {"label": "Wiki Home", "url": f"/wiki/{repo_alias}/"}
        ]
        if not article_path:
            return crumbs
        parts = article_path.split("/")
        for i, part in enumerate(parts[:-1]):
            url = f"/wiki/{repo_alias}/{'/'.join(parts[:i + 1])}/"
            crumbs.append({"label": part, "url": url})
        last = parts[-1].replace("-", " ").replace("_", " ").title()
        crumbs.append({"label": last, "url": None})
        return crumbs
