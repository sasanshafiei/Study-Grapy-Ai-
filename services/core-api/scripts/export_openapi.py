"""Print the public specification or compare it with a published artifact, without starting the app."""

import argparse
import json
from pathlib import Path

from app.main import app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=Path, metavar="ARTIFACT", help="fail if the published bytes differ")
    args = parser.parse_args()
    document = json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.check:
        if args.check.read_bytes() != document.encode("utf-8"):
            parser.exit(1, f"{args.check} differs from the public OpenAPI schema. Regenerate the artifact.\n")
    else:
        print(document, end="")


if __name__ == "__main__":
    main()
