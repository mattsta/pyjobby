#!/usr/bin/env python3
"""
Script to fix pytestmark asyncio issues in test files.

Removes global pytestmark and adds @pytest.mark.asyncio only to async functions.
"""

import re
import sys
from pathlib import Path


def fix_test_file(file_path: Path) -> None:
    """Fix a single test file."""
    content = file_path.read_text()

    # Remove global pytestmark line
    content = re.sub(
        r"^pytestmark = pytest\.mark\.asyncio\s*\n", "", content, flags=re.MULTILINE
    )

    # Find all async def test_ functions and add decorator if not present
    def add_decorator(match):
        indent = match.group(1)
        func_def = match.group(2)

        # Check if @pytest.mark.asyncio is already above this function
        # by looking at the lines before
        return f"{indent}@pytest.mark.asyncio\n{indent}{func_def}"

    # Match async test functions that don't already have the decorator
    # Look for:  optional indent, "async def test_", function name, parameters
    pattern = r"^(\s*)async def (test_[a-zA-Z0-9_]+\([^)]*\):)"

    # First, collect all async test functions
    lines = content.split("\n")
    new_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Check if this is an async test function
        match = re.match(pattern, line)
        if match:
            indent = match.group(1)
            # Check if previous non-empty line has the decorator
            prev_line_idx = i - 1
            while prev_line_idx >= 0 and not lines[prev_line_idx].strip():
                prev_line_idx -= 1

            has_decorator = False
            if prev_line_idx >= 0:
                if "@pytest.mark.asyncio" in lines[prev_line_idx]:
                    has_decorator = True

            if not has_decorator:
                # Add the decorator
                new_lines.append(f"{indent}@pytest.mark.asyncio")

        new_lines.append(line)
        i += 1

    content = "\n".join(new_lines)

    # Write back
    file_path.write_text(content)
    print(f"Fixed {file_path}")


def main():
    if len(sys.argv) < 2:
        print("Usage: fix-pytestmark.py <test_file1.py> [test_file2.py ...]")
        sys.exit(1)

    for file_path_str in sys.argv[1:]:
        file_path = Path(file_path_str)
        if not file_path.exists():
            print(f"Error: {file_path} does not exist")
            continue

        fix_test_file(file_path)


if __name__ == "__main__":
    main()
