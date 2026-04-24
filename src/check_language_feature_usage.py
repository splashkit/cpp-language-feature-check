#!/usr/bin/env python3

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Tuple
import re
import traceback
import os.path
import uuid

# ==================== Constants ======================

# Return codes
SUCCESS = 0
CODE_HAD_ISSUES = 32
CODE_CHECKING_FAILED = 64

CLANG_QUERY_PREAMBLE = """\
set traversal IgnoreUnlessSpelledInSource
set bind-root false
enable output diag
disable output print
disable output detailed-ast
disable output dump
"""

# ==================== Types ======================
@dataclass
class MatchQuerySet:
    queries: List[str]
    id: str
    description: str
    comment: str | None = None # For leaving comments in the json

# ==================== Clang Query Utils ======================

MATCHES_REGEX = re.compile(r'^(\d+)\s+match(?:es)?.{0,3}$')

# Run clang-query and return the result

# Note: Deliberately ignores any stderr output,
# since the only thing captured in it (that I can see)
# are issues with parsing the student's code.
# We can't guarantee student's code will parse perfectly
# on our system (they could have their own include file, etc).
# Clang does a best effort parse in these cases.
# Syntax errors for the `match` queries are in stdout and are extracted
# later manually

def clang_query(source_file: str, query: str) -> str:
    full_query = CLANG_QUERY_PREAMBLE + query + "\n"

    result = subprocess.run(
        ["clang-query", source_file, "--"],
        input=full_query,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    return result.stdout

# Extract "0 matches, 1 match, etc"
def clang_query_extract_matches(output: List[str]) -> List[int]:
    extracted_numbers = []

    for line in output:
        match = MATCHES_REGEX.match(line)
        if match:
            number = int(match.group(1))
            extracted_numbers.append(number)

    return extracted_numbers

def clang_query_extract_messages(output: List[str], run_uuid: str, match_query_sets: List[MatchQuerySet]) -> str:
    match_query_set_lookup = {m.id : m for m in match_query_sets}

    result = []
    for line in output:
        # Filter out newlines, "Match #<n>" lines, or "<n> match(es)" lines
        if line == "" or "Match #" in line or MATCHES_REGEX.match(line):
            continue

        # Identify lines like:
        # `/home/user/myprogram/global_const_unformatted.cpp:10:15: note: "LANGCHECK[uuid]FormattingConstant" binds here`
        # and transform them into:
        # `global_const_unformatted.cpp:10:15: error: Double check how you've formatted your constants (make sure to use UPPER_CASE).`
        if run_uuid in line:
            newline = "\n" if len(result) > 0 else ""

            # Extract match rule id
            match_id = re.search(
                run_uuid + r'(\w*)', line
            ).group(1)

            if match_id not in match_query_set_lookup:
                raise RuntimeError(f"Unknown match rule set ID in output: {match_id}")

            match_query_set = match_query_set_lookup[match_id]

            # Extract file:line:col location if present
            # The regex hopefully allows some slight formatting changes across
            # versions.
            error_loc = re.search(
                r'([^/:]+\.[^/:]+(?=[ :])(:\s*\d+\s*|)(:\s*\d+\s*|))', line
            )

            if error_loc:
                line = f"{newline}{error_loc.group(1)}: error: {match_query_set.description}"
            else:
                line = f"{newline}error: {match_query_set.description}"

        result.append(line)

    return "\n".join(result)

# ==================== Rule Loading ======================

def load_rules(path: str) -> Dict:
    with open(path, "r") as f:
        return json.load(f)

# Recursively include all the MatchQuerySets for the chosen ruleset (e.g chapter)
def resolve_rules(
    rules: Dict,
    ruleset: str
) -> List[MatchQuerySet]:
    chapter = rules[ruleset]
    matches: List[MatchQuerySet] = []

    for include in chapter.get("includes", []):
        matches.extend(resolve_rules(rules, include))

    for rule in chapter.get("rules", []):
        matches.append(MatchQuerySet(**rule))

    return matches

# ==================== Main Logic ======================

def check_usage(source_file: str, match_query_sets: List[MatchQuerySet]) -> None:
    # Runs all the match queries against a file.
    # This is done in a single call to clang-query for efficiency.

    # Unique prefix so we avoid collisions with student code
    run_uuid = "LANGCHECK" + uuid.uuid4().hex

    # Start by joining the queries up
    list_of_queries = []
    for query_set in match_query_sets:
        for i, query in enumerate(query_set.queries):
            # Bind the query it with a tag so we can find matches easily later
            list_of_queries.append(f"match {query}.bind('{run_uuid + query_set.id}')")


    # Get the results from clang-query
    combined_query = "\n".join(list_of_queries)
    output = clang_query(source_file, combined_query).split("\n")
    # Find the number of matches for each query
    match_counts = clang_query_extract_matches(output)
    # There should be the same number as there were match statements
    expected_match_length = sum([len(x.queries) for x in match_query_sets])

    if len(match_counts) != expected_match_length:
        # If not, something is wrong, abort checking
        print(f"Error: Mismatch between expected result count and clang-query output.", file=sys.stderr)
        print(f"Expected {expected_match_length} match counts, got {len(match_counts)}.", file=sys.stderr)
        print("There is likely a syntax error in the queries - see output below.", file=sys.stderr)
        print(f"\n==== clang-query Input ====\n{combined_query}\n============================", file=sys.stderr)
        print(f"\n==== clang-query Output ====\n{output}\n============================", file=sys.stderr)
        sys.exit(CODE_CHECKING_FAILED)

    # Now we can step through and pair up each match count with its associated MatchQuerySet
    idx = 0
    matched = []
    for matchQuerySet in match_query_sets:
        total_matches = sum(match_counts[idx : idx + len(matchQuerySet.queries)])
        idx += len(matchQuerySet.queries)

        if total_matches > 0:
            matched.append(matchQuerySet)

    messages = clang_query_extract_messages(output, run_uuid, match_query_sets)

    return matched, messages

# ==================== Output Formatting ======================

def format_output_ids(matched: List[MatchQuerySet], messages : str) -> str:
    return ", ".join([x.id for x in matched])

def format_output_full_error_report(matched: List[MatchQuerySet], messages : str) -> str:
    return messages # this function just exists for consistency...

def format_output_brief_descriptions(matched: List[MatchQuerySet], messages : str) -> str:
    return "\n".join([x.description for x in matched])

# =========== Main Real (extracted so it can be tested...) ===========

def main_inner(source_file: str, ruleset: str, output_style: str, rules_path: str) -> str:
    rules = load_rules(rules_path)
    match_query_sets = resolve_rules(rules, ruleset)

    matches, messages = check_usage(source_file, match_query_sets)

    if output_style == "id":
        return format_output_ids(matches, messages)
    elif output_style == "desc":
        return format_output_full_error_report(matches, messages)
    elif output_style == "brief":
        return format_output_brief_descriptions(matches, messages)

# ==================== Command Line Usage ======================

def main() -> None:
    try:
        if len(sys.argv) != 5:
            print(f"Usage: {sys.argv[0]} <source-file> <rules-file> <rules> <output-style {{id=ID's only (comma delimited)|desc=full line reporting + descriptions|brief=Descriptions only (newline delimited)}}>")
            sys.exit(CODE_CHECKING_FAILED)

        source_file = sys.argv[1]
        rules_file = sys.argv[2]
        ruleset = sys.argv[3]
        output_style = sys.argv[4]

        # check inputs
        assert os.path.isfile(rules_file), f"Rule set file doesn't exist {rules_file}"
        assert os.path.isfile(source_file), f"Input source file doesn't exist: {source_file}"
        assert output_style=="id" or output_style == "desc" or output_style == "brief", f"Invalid output-style {output_style}"

        output = main_inner(source_file, ruleset, output_style, rules_file)

        # Print the actual result on stdout
        print(output)

        if len(output) > 0:
            sys.exit(CODE_HAD_ISSUES)

    except Exception as e:
        print("Error:", traceback.format_exc(), file=sys.stderr)
        sys.exit(CODE_CHECKING_FAILED)

    sys.exit(SUCCESS)

if __name__ == "__main__":
    main()
