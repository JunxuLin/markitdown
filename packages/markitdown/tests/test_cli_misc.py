#!/usr/bin/env python3 -m pytest
import os
import subprocess
from markitdown import __version__

# This file contains CLI tests that are not directly tested by the FileTestVectors.
# This includes things like help messages, version numbers, and invalid flags.


def test_version() -> None:
    result = subprocess.run(
        ["python", "-m", "markitdown", "--version"], capture_output=True, text=True
    )

    assert result.returncode == 0, f"CLI exited with error: {result.stderr}"
    assert __version__ in result.stdout, f"Version not found in output: {result.stdout}"


def test_invalid_flag() -> None:
    result = subprocess.run(
        ["python", "-m", "markitdown", "--foobar"], capture_output=True, text=True
    )

    assert result.returncode != 0, f"CLI exited with error: {result.stderr}"
    assert (
        "unrecognized arguments" in result.stderr
    ), "Expected 'unrecognized arguments' to appear in STDERR"
    assert "SYNTAX" in result.stderr, "Expected 'SYNTAX' to appear in STDERR"


def test_epub_split_by_chapter_cli() -> None:
    """Test the --split-by-chapter CLI flag with an EPUB file."""
    epub_file = os.path.join(
        os.path.dirname(__file__), "test_files", "test.epub"
    )

    result_default = subprocess.run(
        ["python", "-m", "markitdown", epub_file],
        capture_output=True,
        text=True,
    )
    result_split = subprocess.run(
        ["python", "-m", "markitdown", epub_file, "--split-by-chapter"],
        capture_output=True,
        text=True,
    )

    assert result_default.returncode == 0, f"CLI failed: {result_default.stderr}"
    assert result_split.returncode == 0, f"CLI failed: {result_split.stderr}"

    assert "# Chapter 1: Test Content" in result_split.stdout
    assert "# Chapter 2: More Content" in result_split.stdout
    assert "---" in result_split.stdout
    assert "---" not in result_default.stdout


def test_epub_split_by_chapter_to_dir_cli() -> None:
    """Test the --split-by-chapter DIR CLI flag writes individual chapter files."""
    import tempfile

    epub_file = os.path.join(
        os.path.dirname(__file__), "test_files", "test.epub"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            ["python", "-m", "markitdown", epub_file, "--split-by-chapter", tmpdir],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"CLI failed: {result.stderr}"

        # README.md must exist at root
        assert os.path.exists(os.path.join(tmpdir, "README.md"))

        # Chapters subdir must exist and contain numbered .md files
        chapters_dir = os.path.join(tmpdir, "chapters")
        assert os.path.isdir(chapters_dir), "chapters/ subdir expected"
        chapter_files = os.listdir(chapters_dir)
        assert len(chapter_files) > 0, "Expected chapter files in chapters/"

        # All chapter content must be present somewhere in the output
        all_content = ""
        for root, _, files in os.walk(tmpdir):
            for f in files:
                if f.endswith(".md"):
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        all_content += fh.read()

        assert "# Chapter 1: Test Content" in all_content
        assert "# Chapter 2: More Content" in all_content


if __name__ == "__main__":
    """Runs this file's tests from the command line."""
    test_version()
    test_invalid_flag()
    test_epub_split_by_chapter_cli()
    test_epub_split_by_chapter_to_dir_cli()
    print("All tests passed!")
