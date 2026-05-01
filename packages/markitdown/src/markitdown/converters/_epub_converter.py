import base64
import io
import mimetypes
import os
import posixpath
import re
import zipfile
from defusedxml import minidom
from xml.dom.minidom import Document

from bs4 import BeautifulSoup
from typing import BinaryIO, Any, Dict, List, Optional, Tuple

from ._html_converter import HtmlConverter
from .._base_converter import DocumentConverterResult
from ..converter_utils.images import resolve_images_dir
from .._stream_info import StreamInfo

ACCEPTED_MIME_TYPE_PREFIXES = [
    "application/epub",
    "application/epub+zip",
    "application/x-epub+zip",
]

ACCEPTED_FILE_EXTENSIONS = [".epub"]

MIME_TYPE_MAPPING = {
    ".html": "text/html",
    ".xhtml": "application/xhtml+xml",
}

# epub:type values → output subdirectory name
_EPUB_TYPE_TO_SUBDIR = {
    "frontmatter": "front-matter",
    "bodymatter": "chapters",
    "backmatter": "back-matter",
}

# Spine item basenames that are always noise and should be skipped in organized mode
_SKIP_BASENAMES = {"navigation", "nav", "toc", "eula"}


class EpubConverter(HtmlConverter):
    """
    Converts EPUB files to Markdown. Style information (e.g.m headings) and tables are preserved where possible.
    """

    def __init__(self):
        super().__init__()
        self._html_converter = HtmlConverter()

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()

        if extension in ACCEPTED_FILE_EXTENSIONS:
            return True

        for prefix in ACCEPTED_MIME_TYPE_PREFIXES:
            if mimetype.startswith(prefix):
                return True

        return False

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> DocumentConverterResult:
        with zipfile.ZipFile(file_stream, "r") as z:
            # Locate content.opf
            container_dom = minidom.parse(z.open("META-INF/container.xml"))
            opf_path = container_dom.getElementsByTagName("rootfile")[0].getAttribute(
                "full-path"
            )

            # Parse content.opf
            opf_dom = minidom.parse(z.open(opf_path))
            metadata: Dict[str, Any] = {
                "title": self._get_text_from_node(opf_dom, "dc:title"),
                "authors": self._get_all_texts_from_nodes(opf_dom, "dc:creator"),
                "language": self._get_text_from_node(opf_dom, "dc:language"),
                "publisher": self._get_text_from_node(opf_dom, "dc:publisher"),
                "date": self._get_text_from_node(opf_dom, "dc:date"),
                "description": self._get_text_from_node(opf_dom, "dc:description"),
                "identifier": self._get_text_from_node(opf_dom, "dc:identifier"),
            }

            # Extract manifest items (ID → href mapping)
            manifest = {
                item.getAttribute("id"): item.getAttribute("href")
                for item in opf_dom.getElementsByTagName("item")
            }

            # Extract spine order (ID refs)
            spine_items = opf_dom.getElementsByTagName("itemref")
            spine_order = [item.getAttribute("idref") for item in spine_items]

            # Convert spine order to actual file paths
            base_path = "/".join(opf_path.split("/")[:-1])
            spine = [
                f"{base_path}/{manifest[item_id]}" if base_path else manifest[item_id]
                for item_id in spine_order
                if item_id in manifest
            ]

            # Reverse map: full zip path → manifest item ID (better stem hint than filename)
            file_to_id: Dict[str, str] = {
                (f"{base_path}/{href}" if base_path else href): item_id
                for item_id, href in manifest.items()
            }

            split_by_chapter = kwargs.get("split_by_chapter", False)
            organize = split_by_chapter and not kwargs.get("no_organize", False)

            # Resolve image saving options
            save_images = kwargs.get("save_images", False)
            actual_images_dir: str | None = None
            # For organized mode, images always go to assets/ inside the output dir.
            # For non-organized split, use chapters_output_dir for relative path computation.
            chapters_output_dir: str | None = kwargs.get("chapters_output_dir", None)
            if save_images:
                if organize and chapters_output_dir:
                    # Images land in {chapters_output_dir}/assets/ — computed later per-file
                    actual_images_dir = os.path.join(chapters_output_dir, "assets")
                    os.makedirs(actual_images_dir, exist_ok=True)
                else:
                    actual_images_dir, _ = resolve_images_dir(
                        save_images, stream_info, "epub",
                        relative_to=chapters_output_dir,
                    )

            namelist_set = set(z.namelist())
            markdown_content: List[str] = []
            chapter_filenames: List[str] = []

            # Track chapter counter for bodymatter numbering
            chapter_counter = [0]
            # Track nav content to fold into README
            nav_toc_lines: List[str] = []

            # --- Pass 1 (organized mode only): cache HTML and build stem→out_path map ---
            # This allows us to rewrite cross-chapter links (c03.xhtml#Page_N → ../chapters/03-foo.md)
            cached_html: Dict[str, bytes] = {}
            stem_to_outpath: Dict[str, str] = {}
            if organize:
                _pre_counter: List[int] = [0]
                for file in spine:
                    if file not in namelist_set:
                        continue
                    file_stem = os.path.splitext(os.path.basename(file))[0]
                    item_id = file_to_id.get(file, file_stem)
                    stem_hint = item_id  # prefer manifest ID (e.g. "c01") over filename stem
                    with z.open(file) as f:
                        _html = f.read()
                    cached_html[file] = _html
                    if stem_hint.lower() in _SKIP_BASENAMES or file_stem.lower() in _SKIP_BASENAMES:
                        continue
                    epub_type = self._get_epub_type(_html)
                    subdir = self._epub_type_to_subdir(epub_type, stem_hint)
                    title = self._get_chapter_title(_html)
                    out_fn = self._map_to_output_filename(title, stem_hint, subdir, _pre_counter)
                    out_path_pre = f"{subdir}/{out_fn}" if subdir else out_fn
                    # Key by file stem (links use filename) AND item_id (both may appear)
                    stem_to_outpath[file_stem] = out_path_pre
                    if item_id != file_stem:
                        stem_to_outpath[item_id] = out_path_pre

            for file in spine:
                if file not in namelist_set:
                    continue

                html_bytes = cached_html[file] if file in cached_html else None
                if html_bytes is None:
                    with z.open(file) as f:
                        html_bytes = f.read()

                file_stem = os.path.splitext(os.path.basename(file))[0]
                item_id = file_to_id.get(file, file_stem)
                stem = item_id  # prefer manifest ID for type/subdir heuristics

                if organize:
                    # Detect epub:type and derive subdir / filename
                    epub_type = self._get_epub_type(html_bytes)
                    subdir = self._epub_type_to_subdir(epub_type, stem)
                    title = self._get_chapter_title(html_bytes)

                    # Skip noise files (navigation, eula) — fold nav into README
                    if stem.lower() in _SKIP_BASENAMES or file_stem.lower() in _SKIP_BASENAMES:
                        if any(k in stem.lower() for k in ("nav", "toc", "navigation")):
                            nav_toc_lines = self._extract_nav_links(html_bytes)
                        continue

                    # Compute per-file image prefix relative to this file's subdir
                    if actual_images_dir and chapters_output_dir:
                        file_dir = os.path.join(chapters_output_dir, subdir) if subdir else chapters_output_dir
                        md_images_prefix: str | None = os.path.relpath(
                            os.path.abspath(actual_images_dir),
                            os.path.abspath(file_dir),
                        )
                    else:
                        md_images_prefix = None

                    html_bytes = self._resolve_images(
                        html_bytes, file, z, namelist_set,
                        actual_images_dir, md_images_prefix,
                    )

                    # Inject Obsidian block-ref markers on <li id="..."> elements
                    # so that fragment links like references.md#^b01-bib-0003 resolve.
                    html_bytes = self._inject_block_refs(html_bytes)

                    out_filename = self._map_to_output_filename(
                        title, stem, subdir, chapter_counter
                    )
                    out_path = f"{subdir}/{out_filename}" if subdir else out_filename
                else:
                    # Non-organized: compute global image prefix
                    if save_images and actual_images_dir:
                        _, md_images_prefix = resolve_images_dir(
                            save_images, stream_info, "epub",
                            relative_to=chapters_output_dir,
                        )
                    else:
                        md_images_prefix = None

                    html_bytes = self._resolve_images(
                        html_bytes, file, z, namelist_set,
                        actual_images_dir, md_images_prefix,
                    )
                    out_path = file_stem + ".md"

                filename = os.path.basename(file)
                extension = os.path.splitext(filename)[1].lower()
                mimetype = MIME_TYPE_MAPPING.get(extension)
                converted_content = self._html_converter.convert(
                    io.BytesIO(html_bytes),
                    StreamInfo(
                        mimetype=mimetype,
                        extension=extension,
                        filename=filename,
                    ),
                    keep_data_uris=actual_images_dir is None,
                )
                md = converted_content.markdown.strip()
                if organize and stem_to_outpath:
                    md = self._rewrite_epub_links(md, stem_to_outpath, out_path)
                markdown_content.append(md)
                chapter_filenames.append(out_path)

            # Build metadata block
            metadata_markdown = []
            for key, value in metadata.items():
                if isinstance(value, list):
                    value = ", ".join(value)
                if value:
                    metadata_markdown.append(f"**{key.capitalize()}:** {value}")
            metadata_str = "\n".join(metadata_markdown)

            if organize:
                # Generate README.md with metadata + TOC
                readme_lines = [f"# {metadata.get('title') or 'Book'}", "", metadata_str]
                if nav_toc_lines:
                    readme_lines += ["", "## Table of Contents", ""] + nav_toc_lines
                readme_content = "\n".join(readme_lines)
                markdown_content.insert(0, readme_content)
                chapter_filenames.insert(0, "README.md")
            else:
                markdown_content.insert(0, metadata_str)
                chapter_filenames.insert(0, "metadata.md")

            chapter_separator = "\n\n---\n\n" if split_by_chapter else "\n\n"
            chapters = (
                list(zip(chapter_filenames, markdown_content))
                if split_by_chapter
                else None
            )

            return DocumentConverterResult(
                markdown=chapter_separator.join(markdown_content),
                title=metadata["title"],
                chapters=chapters,
            )

    # ------------------------------------------------------------------
    # Organized-output helpers
    # ------------------------------------------------------------------

    def _get_epub_type(self, html_bytes: bytes) -> Optional[str]:
        """Return the epub:type of the document's <body> or first <section>."""
        soup = BeautifulSoup(html_bytes, "html.parser")
        for tag in ("body", "section", "article", "div"):
            el = soup.find(tag)
            if el:
                val = el.get("epub:type") or el.get("data-epub-type")
                if val:
                    # May be a space-separated list; take the first recognised value
                    for token in str(val).split():
                        if token in _EPUB_TYPE_TO_SUBDIR:
                            return token
        return None

    def _epub_type_to_subdir(self, epub_type: Optional[str], stem_hint: str) -> str:
        """Map an epub:type (or spine item ID) to an output subdirectory."""
        if epub_type in _EPUB_TYPE_TO_SUBDIR:
            return _EPUB_TYPE_TO_SUBDIR[epub_type]

        s = stem_hint.lower().strip()

        # --- Explicit keyword sets for common EPUB item IDs and slugs ---
        _FRONT_KEYWORDS = {
            # cover variants
            "cover", "cvi", "cov",
            # title page variants
            "title", "tp", "htp", "halftitle", "half-title",
            # copyright / legal
            "copyright", "cop", "legal",
            # front matter sections
            "dedication", "ded",
            "acknowledgment", "acknowledgments", "ack",
            "foreword", "fore",
            "preface", "pre",
            "introduction", "intro",
            "epigraph", "epi",
            "frontmatter", "front-matter", "fm",
        }
        _BACK_KEYWORDS = {
            # bibliography / references
            "bibliography", "bib", "references", "ref",
            # index
            "index", "ind",
            # appendices
            "appendix", "app", "appendices",
            # back-matter catch-alls
            "backmatter", "back-matter", "bm",
            # about / afterword
            "about", "ata", "afterword", "aft",
            # glossary / notes
            "glossary", "glo", "notes",
            # author bio
            "author",
        }

        # Strip trailing digits and separators, check exact keyword match first
        base = s.rstrip("0123456789_- ")
        if base in _FRONT_KEYWORDS or s in _FRONT_KEYWORDS:
            return "front-matter"
        if base in _BACK_KEYWORDS or s in _BACK_KEYWORDS:
            return "back-matter"

        # Check if the stem starts with a keyword (e.g. "bm1", "ata2", "cop-r1")
        for kw in _FRONT_KEYWORDS:
            if s.startswith(kw):
                return "front-matter"
        for kw in _BACK_KEYWORDS:
            if s.startswith(kw):
                return "back-matter"

        # Single-letter prefix fallback (f=front, b=back, c=chapter)
        if s.startswith("f"):
            return "front-matter"
        if s.startswith("b"):
            return "back-matter"
        if s.startswith("c"):
            return "chapters"
        return ""  # root level — chapter_counter will still number it if subdir=="chapters"

    def _get_chapter_title(self, html_bytes: bytes) -> str:
        """Extract a human-readable title from an XHTML spine item."""
        soup = BeautifulSoup(html_bytes, "html.parser")
        # Prefer <head><title>
        head_title = soup.find("title")
        if head_title and head_title.text.strip():
            return head_title.text.strip()
        # Fall back to first heading
        for tag in ("h1", "h2", "h3"):
            h = soup.find(tag)
            if h and h.text.strip():
                return h.text.strip()
        return ""

    @staticmethod
    def _slugify(text: str) -> str:
        """Convert a title string into a filesystem-safe slug."""
        text = text.lower()
        text = re.sub(r"[^\w\s-]", "", text)
        text = re.sub(r"[\s_]+", "-", text).strip("-")
        return text or "untitled"

    def _map_to_output_filename(
        self,
        title: str,
        stem_hint: str,
        subdir: str,
        chapter_counter: List[int],
    ) -> str:
        """Derive a human-readable .md filename from a chapter title."""
        if subdir == "chapters":
            # Match "1 Title", "Chapter 5 Title", "5. Title", etc.
            num_match = re.match(
                r"^(?:chapter\s+)?(\d+)\s*[.:]?\s*(.+)$", title, re.IGNORECASE
            )
            if num_match:
                num = int(num_match.group(1))
                rest = num_match.group(2).strip()
                chapter_counter[0] = max(chapter_counter[0], num)
            else:
                chapter_counter[0] += 1
                num = chapter_counter[0]
                rest = title or stem_hint
            slug = self._slugify(rest)
            return f"{num:02d}-{slug}.md"
        else:
            slug = self._slugify(title or stem_hint)
            return f"{slug}.md"

    def _extract_nav_links(self, html_bytes: bytes) -> List[str]:
        """Extract top-level chapter links from a navigation XHTML file as a markdown list."""
        soup = BeautifulSoup(html_bytes, "html.parser")
        lines: List[str] = []

        # Only use the epub:type="toc" nav element to avoid page-list entries
        toc_nav = soup.find("nav", attrs={"epub:type": "toc"})
        if not toc_nav:
            toc_nav = soup.find("nav", id="toc")
        if not toc_nav:
            toc_nav = soup  # fallback: whole document

        # Only take top-level <li> entries (skip deeply nested page refs)
        top_ol = toc_nav.find("ol")
        if not top_ol:
            return lines

        for li in top_ol.find_all("li", recursive=False):
            a = li.find("a", href=True)
            if a:
                text = a.get_text(strip=True)
                if text:
                    lines.append(f"- {text}")
        return lines

    def _inject_block_refs(self, html_bytes: bytes) -> bytes:
        """Append Obsidian block-reference markers to <li id="..."> elements.

        Transforms ``<li id="b01-bib-0003"><b>3.</b> text</li>`` so that the
        converted markdown line ends with `` ^b01-bib-0003``.  This lets Obsidian
        resolve fragment links like ``references.md#^b01-bib-0003`` and scroll to
        the correct entry.
        """
        soup = BeautifulSoup(html_bytes, "html.parser")
        changed = False
        for li in soup.find_all("li", id=True):
            li_id = li.get("id", "")
            if li_id:
                li.append(f" ^{li_id}")
                changed = True
        return str(soup).encode("utf-8") if changed else html_bytes

    def _rewrite_epub_links(
        self,
        markdown: str,
        stem_to_outpath: Dict[str, str],
        this_outpath: str,
    ) -> str:
        """Rewrite epub-internal links like (c03.xhtml#Page_24) to relative markdown paths.

        Cross-chapter links in the original HTML reference other spine items by filename
        (e.g., href="c03.xhtml#Page_24"). After markdown conversion these survive as
        [text](c03.xhtml#Page_24). This method replaces them with paths relative to the
        current file, e.g., [text](../chapters/03-what-is-intelligence.md).

        Fragment identifiers (e.g., #b01-bib-0003) are converted to Obsidian block
        references (#^b01-bib-0003); page-break fragments (#Page_N) are dropped.

        Same-document anchor links (#ind20 style) are left unchanged — they cannot be
        resolved since HTML id attributes are not preserved in markdown.

        Also escapes [[text](link)] patterns to \\[[text](link)\\] to prevent Obsidian
        from misinterpreting the leading [[ as a wikilink.
        """
        this_dir = os.path.dirname(this_outpath)  # e.g. "back-matter", "chapters", ""

        def replace_match(m: re.Match) -> str:  # type: ignore[type-arg]
            stem = m.group(1)
            fragment = m.group(2) or ""  # e.g. "#b01-bib-0003" or ""
            if stem in stem_to_outpath:
                target = stem_to_outpath[stem]
                rel = os.path.relpath(target, this_dir) if this_dir else target
                rel = rel.replace(os.sep, "/")  # forward slashes for markdown
                # Convert anchor IDs to Obsidian block-ref format: #id → #^id
                # Drop bare page-break fragments (#Page_N) — no matching anchors exist
                if fragment:
                    if re.match(r"#[Pp]age_\d+", fragment):
                        fragment = ""
                    elif not fragment.startswith("#^"):
                        fragment = f"#^{fragment[1:]}"
                return f"]({rel}{fragment})"
            return m.group(0)  # unknown target — leave as-is

        # Rewrite ](stem.xhtml) / ](stem.xhtml#fragment) — capture fragment separately
        markdown = re.sub(
            r"\]\(([^)#/\s]+)\.x?html(#[^)]+)?\)", replace_match, markdown
        )

        # Fix Obsidian wikilink conflict: [[text](link)] → \[[text](link)\]
        # Without this, Obsidian treats [[ as a wikilink opener and ignores the inner link.
        markdown = re.sub(
            r"\[\[([^\]\[]+)\]\(([^)]+)\)\]",
            r"\\[[\1](\2)\\]",
            markdown,
        )

        return markdown

    # ------------------------------------------------------------------
    # Image resolution
    # ------------------------------------------------------------------

    def _resolve_images(
        self,
        html_bytes: bytes,
        html_path: str,
        z: zipfile.ZipFile,
        namelist_set: set,
        images_dir: str | None,
        md_images_prefix: str | None,
    ) -> bytes:
        """Rewrite <img src> attributes so images survive HTML-to-Markdown conversion.

        If *images_dir* is given, each image is extracted there and the src is
        replaced with *md_images_prefix*/filename (a path relative to the markdown
        file).  Otherwise the image is embedded as a base64 data URI.
        """
        soup = BeautifulSoup(html_bytes, "html.parser")
        changed = False
        html_dir = posixpath.dirname(html_path)

        for img in soup.find_all("img"):
            src = img.get("src", "")
            if not src or src.startswith("data:") or src.startswith("http"):
                continue
            resolved = posixpath.normpath(posixpath.join(html_dir, src))
            if resolved not in namelist_set:
                continue
            img_bytes = z.read(resolved)
            if images_dir:
                img_filename = os.path.basename(resolved)
                with open(os.path.join(images_dir, img_filename), "wb") as out:
                    out.write(img_bytes)
                img["src"] = f"{md_images_prefix}/{img_filename}"
            else:
                mime, _ = mimetypes.guess_type(resolved)
                mime = mime or "image/jpeg"
                b64 = base64.b64encode(img_bytes).decode("ascii")
                img["src"] = f"data:{mime};base64,{b64}"
            changed = True

        return soup.encode("utf-8") if changed else html_bytes

    # ------------------------------------------------------------------
    # OPF / DOM helpers
    # ------------------------------------------------------------------

    def _get_text_from_node(self, dom: Document, tag_name: str) -> str | None:
        """Convenience function to extract a single occurrence of a tag (e.g., title)."""
        texts = self._get_all_texts_from_nodes(dom, tag_name)
        if len(texts) > 0:
            return texts[0]
        else:
            return None

    def _get_all_texts_from_nodes(self, dom: Document, tag_name: str) -> List[str]:
        """Helper function to extract all occurrences of a tag (e.g., multiple authors)."""
        texts: List[str] = []
        for node in dom.getElementsByTagName(tag_name):
            if node.firstChild and hasattr(node.firstChild, "nodeValue"):
                texts.append(node.firstChild.nodeValue.strip())
        return texts

