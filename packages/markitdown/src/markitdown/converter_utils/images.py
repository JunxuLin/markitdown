import os
import re

from .._stream_info import StreamInfo


def resolve_images_dir(
    save_images: bool | str,
    stream_info: StreamInfo,
    fallback_name: str,
    relative_to: str | None = None,
) -> tuple[str, str]:
    """Resolve the images directory and markdown prefix from a ``save_images`` kwarg.

    Parameters
    ----------
    save_images:
        - ``str``  — use this path directly as both the directory and the
                     markdown image prefix.
        - ``True`` — auto-derive ``images_{stem}`` from *stream_info.filename*,
                     falling back to *fallback_name* when no filename is available.
    stream_info:
        Stream metadata; ``stream_info.filename`` is used for auto-naming.
    fallback_name:
        Format-specific fallback stem (e.g. ``"epub"``, ``"pdf"``) used when
        no filename is available and *save_images* is ``True``.
    relative_to:
        Optional directory that the markdown prefix should be relative to.
        When set (e.g. the directory where chapter ``.md`` files will be written),
        the prefix is computed as ``os.path.relpath(actual_images_dir, relative_to)``
        so that image references inside those files resolve correctly.

    Returns
    -------
    (actual_images_dir, md_images_prefix)
        The directory to write images into, and the prefix to use in markdown
        ``![alt](prefix/filename)`` references.  The directory is created
        (including any parents) before returning.
    """
    if isinstance(save_images, str):
        # User specified a directory — place images in an assets/ subdirectory
        # so the output folder stays organised.
        actual_images_dir = os.path.join(save_images, "assets")
        md_images_prefix = os.path.join(save_images, "assets")
    else:
        file_stem = re.sub(
            r"[^\w\-]", "_", os.path.splitext(stream_info.filename or fallback_name)[0]
        )
        actual_images_dir = f"images_{file_stem}"
        md_images_prefix = f"./images_{file_stem}"

    if relative_to is not None:
        md_images_prefix = os.path.relpath(
            os.path.abspath(actual_images_dir), os.path.abspath(relative_to)
        )

    os.makedirs(actual_images_dir, exist_ok=True)
    return actual_images_dir, md_images_prefix
