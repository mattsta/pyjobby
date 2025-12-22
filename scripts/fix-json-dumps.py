#!/usr/bin/env python3
"""
Fix json.dumps() calls in tests - pass dicts directly to asyncpg.

The orjson codec is configured in conftest.py, so we should pass
Python dicts directly instead of calling json.dumps().
"""

import re
import sys
from pathlib import Path


def fix_test_file(file_path: Path) -> None:
    """Fix json.dumps calls in a test file."""
    content = file_path.read_text()

    # Pattern: json.dumps(expression)
    # Replace with just: expression

    # More complex patterns that capture dict literals, method calls, etc
    # We need to match balanced parentheses/braces

    # For simple variable references: json.dumps(var_name)
    content = re.sub(r"json\.dumps\((\w+(?:\[[\w\'\"]+\])?)\)", r"\1", content)

    # For dict literals: json.dumps({"key": "value", ...})
    # This is tricky - we need to match balanced braces
    def replace_json_dumps_dict(text):
        """Replace json.dumps({...}) with just {...}"""
        pattern = r"json\.dumps\(\s*(\{[^}]*\})\s*\)"
        return re.sub(pattern, r"\1", text)

    content = replace_json_dumps_dict(content)

    # Remove json import if it's no longer used
    lines = content.split("\n")
    has_json_usage = any(
        "json." in line and "import json" not in line for line in lines
    )

    if not has_json_usage:
        # Remove json import
        new_lines = []
        for line in lines:
            if line.strip() == "import json" or line.strip().startswith("import json,"):
                # Skip this line
                continue
            elif "import json" in line:
                # Remove from multi-import
                line = re.sub(r",?\s*json\s*,?", "", line)
                if line.strip() not in ["import", "from"]:
                    new_lines.append(line)
            else:
                new_lines.append(line)
        content = "\n".join(new_lines)

    file_path.write_text(content)
    print(f"Fixed {file_path}")


def main():
    if len(sys.argv) < 2:
        # Default to result passing file for backward compatibility
        files = [Path("tests/test_phase2_result_passing.py")]
    else:
        files = [Path(f) for f in sys.argv[1:]]

    for file_path in files:
        if file_path.exists():
            fix_test_file(file_path)
        else:
            print(f"File not found: {file_path}")
            sys.exit(1)


if __name__ == "__main__":
    main()
