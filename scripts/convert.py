"""Convert the five Rules/mihomo lists into two domain-list-community inputs."""

import argparse
import json
import re
from pathlib import Path


SOURCES = {
    "cdn": ("domainset/cdn.list", "non_ip/cdn.list", "non_ip/microsoft_cdn.list"),
    "download": ("domainset/download.list", "non_ip/download.list"),
}
TYPES = {
    "DOMAIN": "full",
    "DOMAIN-SUFFIX": "domain",
    "DOMAIN-KEYWORD": "keyword",
    "DOMAIN-WILDCARD": "regexp",
}
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
KEYWORD = re.compile(r"[a-z0-9._-]+")
WILDCARD = re.compile(r"[a-z0-9.*?-]+")
GLOB_TO_REGEX = {".": r"\.", "*": ".*", "?": "."}


def convert_line(line: str, domainset: bool) -> str:
    if domainset:
        if line.startswith("+."):
            kind, value = "domain", line[2:]
        else:
            kind, value = "full", line
    else:
        fields = line.split(",")
        if len(fields) != 2:
            raise ValueError("expected exactly RULE-TYPE,value")
        rule, value = (field.strip() for field in fields)
        if rule not in TYPES:
            raise ValueError(f"unsupported rule type: {rule!r}")
        kind = TYPES[rule]

    if not value.isascii():
        raise ValueError(f"expected an ASCII domain or pattern: {value!r}")
    value = value.lower()
    if kind in {"full", "domain"}:
        if len(value) > 253 or not all(LABEL.fullmatch(label) for label in value.split(".")):
            raise ValueError(f"invalid domain: {value!r}")
    elif kind == "keyword":
        if not KEYWORD.fullmatch(value):
            raise ValueError(f"invalid keyword: {value!r}")
    elif kind == "regexp":
        if not WILDCARD.fullmatch(value):
            raise ValueError(f"invalid domain wildcard: {value!r}")
        value = "^" + "".join(GLOB_TO_REGEX.get(c, c) for c in value) + "$"
    return f"{kind}:{value}"


def read_rules(path: Path, domainset: bool) -> list[str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{path}: cannot read input: {exc.strerror}") from exc
    try:
        lines = raw.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        number = raw.count(b"\n", 0, exc.start) + 1
        raise ValueError(f"{path}:{number}: invalid UTF-8") from exc

    rules = []
    for number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rules.append(convert_line(line, domainset))
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
    if not rules:
        raise ValueError(f"{path}: no rules found")
    return rules


def convert(source: Path, output: Path) -> dict:
    categories = {}
    counts = {}
    for category, paths in SOURCES.items():
        rules = set()
        for relative in paths:
            entries = read_rules(source / relative, relative.startswith("domainset/"))
            counts[f"mihomo/{relative}"] = len(entries)
            rules.update(entries)
        categories[category] = sorted(rules)

    # Do not let leftover files in a reused directory become extra GeoSite tags.
    output.mkdir(parents=True)
    for category, rules in categories.items():
        (output / category).write_text("\n".join(rules) + "\n", encoding="utf-8", newline="\n")
    return {
        "inputs": counts,
        "categories": {category: len(rules) for category, rules in categories.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="path to Rules/mihomo")
    parser.add_argument("--output", type=Path, required=True, help="new directory for DLC input")
    args = parser.parse_args()
    try:
        summary = convert(args.source, args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
