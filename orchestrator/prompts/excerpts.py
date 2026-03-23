"""Parse STANDARDS.md into sections for excerpt injection."""

import re
from pathlib import Path


def load_standards(standards_path: Path) -> dict[str, str]:
    """Parse STANDARDS.md into sections keyed by section number.

    Returns e.g. {"1": "## 1. File Format Standards\\n...", "2": "## 2. Circuit..."}
    """
    content = standards_path.read_text()
    sections: dict[str, str] = {}

    # Split on ## N. headers
    pattern = re.compile(r"^(## \d+\..*)", re.MULTILINE)
    parts = pattern.split(content)

    # parts alternates between non-header text and header matches
    # Skip the preamble (parts[0]), then pair headers with bodies
    i = 1
    while i < len(parts):
        header = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""

        # Extract section number from header
        num_match = re.match(r"## (\d+)\.", header)
        if num_match:
            section_num = num_match.group(1)
            sections[section_num] = (header + body).strip()

        i += 2

    return sections
