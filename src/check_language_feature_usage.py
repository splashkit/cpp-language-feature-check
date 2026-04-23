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

# ==================== UUID Utils ======================
# Divides up sets of queries
def get_query_set_splitter_prefix(run_uuid: str) -> str:
    return run_uuid + "SET"

# Divides up the queries within a set
def get_query_splitter_prefix(run_uuid: str, match_id: str) -> str:
    return run_uuid + match_id + "QUERY"

# Used to locate matches from a query
def get_query_matcher(run_uuid: str, match_id: str) -> str:
    return run_uuid + match_id + "MATCH"

# ==================== Types ======================

@dataclass
class MatchQuerySet:
    queries: List[str]
    id: str
    description: str
    comment: str | None = None # For leaving comments in the json

    def get_query_set_splitter(self, run_uuid: str) -> str:
        return get_query_set_splitter_prefix(run_uuid) + self.id

    def get_query_splitter(self, run_uuid: str, query_index: int) -> str:
        return get_query_splitter_prefix(run_uuid, self.id) + str(query_index)

    def get_query_matcher(self, run_uuid: str) -> str:
        return get_query_matcher(run_uuid, self.id)

# ==================== Clang Query Utils ======================

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

# Split a list of strings into a dict based on regex delimiter matches.
# Uses the first matching group as the key.
def split_list_by_regex_into_map(output: List[str], pattern: str) -> Dict[str, List[str]]:
    set_lines = {}
    current_set = None

    for item in output:
        match = re.match(pattern, item)

        if match:
            match_query_set_id = match[1]
            if match_query_set_id not in set_lines:
                set_lines[match_query_set_id] = []
            current_set = set_lines[match_query_set_id]
        elif current_set != None:
            current_set.append(item) # Add to the current sub-list

    return set_lines

# Parse clang-query output into match results.
# Returns list of (MatchQuerySet, formatted_error_lines) tuples for sets that had matches.
def clang_query_extract_matches(output: List[str], run_uuid: str, match_query_sets: List[MatchQuerySet]) -> List[Tuple[MatchQuerySet, List[str]]]:

    # Split output into rule sets, then into individual queries within each set
    match_sets = split_list_by_regex_into_map(output, r".*" + get_query_set_splitter_prefix(run_uuid) + r"(.+)")

    for key in match_sets.keys():
        match_sets[key] = split_list_by_regex_into_map(
            match_sets[key], r".*" + get_query_splitter_prefix(run_uuid, key) + r"(\d+)"
        )

    match_query_set_lookup = {m.id : m for m in match_query_sets}

    # Assemble final matches
    matched = []
    for set_key, queries in match_sets.items():
        if set_key not in match_query_set_lookup:
            raise RuntimeError(f"Unknown query set ID in output: {set_key}")

        match_query_set = match_query_set_lookup[set_key]

        set_match_count = 0
        set_lines = []

        for query_ix, lines in queries.items():
            # Filter empty lines and "Match #" headers
            lines = [x for x in lines if x != "" and "Match #" not in x]

            count_match = None if len(lines) < 1 else re.search(r'(\d+)\s+match(?:es)?', lines[-1])

            # Last line should contain the match count. If not, the match failed to run.
            if count_match == None:
                raise Exception(
                    f"Error: Failed to run {set_key} Query #{query_ix}:\n    " +
                    "\n    ".join(lines)
                )

            count = int(count_match.group(1))
            set_match_count += count

            # Format match lines (exclude the count line)
            match_tag = match_query_set.get_query_matcher(run_uuid)
            for i, line in enumerate(lines[:-1]):
                if match_tag in line:
                    newline = "\n" if i != 0 else ""
                    # Extract file:line:col location if present
                    # The regex hopefully allows some slight formatting changes across
                    # versions.
                    error_loc = re.search(
                        r'([^/:]+\.[^/:]+(?=[ :])(:\s*\d+\s*|)(:\s*\d+\s*|))', line
                    )
                    if error_loc:
                        set_lines.append(
                            f"{newline}{error_loc.group(1)}: error: {match_query_set.description}"
                        )
                    else:
                        set_lines.append(f"{newline}error: {match_query_set.description}")
                else:
                    set_lines.append(line)

        if set_match_count > 0:
            matched.append((match_query_set, set_lines))

    return matched

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
    # Uses UUID-tagged error messages as delimiters to parse clang-query output,
    # since there's no built-in way to separate results from multiple queries

    # Unique prefix so we avoid collisions with student code
    run_uuid = "LANGCHECK" + uuid.uuid4().hex

    # Start by joining the queries up
    list_of_queries = []
    for query_set in match_query_sets:
        # This line deliberately triggers a single line error that we can use to split the output
        # There isn't any way to just "print", so this will have to do...
        list_of_queries.append(query_set.get_query_set_splitter(run_uuid))

        for i, query in enumerate(query_set.queries):
            # Same here
            list_of_queries.append(query_set.get_query_splitter(run_uuid, i))

            # The actual query - bind it with a tag so we can find matches easily later
            list_of_queries.append(f"match {query}.bind('{query_set.get_query_matcher(run_uuid)}')")


    # Get the results from clang-query
    combined_query = "\n".join(list_of_queries)
    output = clang_query(source_file, combined_query)

    try:
        # Split the lines up into matches
        matched = clang_query_extract_matches(output.split("\n"), run_uuid, match_query_sets)

    except Exception:
        print("\n\nThere is likely a syntax error in the queries - see full output below.", file=sys.stderr)
        print(f"\n==== clang-query Input ====\n{combined_query}\n============================", file=sys.stderr)
        print(f"\n==== clang-query Output ====\n{output}\n============================", file=sys.stderr)
        print(f"\n\nAn error occurred! This was the full debug log - check the last few lines of error output for specific info.", file=sys.stderr)
        raise

    return matched

# ==================== Output Formatting ======================

def format_output_ids(matched: List[MatchQuerySet]) -> str:
    return ", ".join([x[0].id for x in matched])

def format_output_full_error_report(matched: List[MatchQuerySet]) -> str:
    return "\n\n".join(["\n".join(x[1]) for x in matched])

def format_output_brief_descriptions(matched: List[MatchQuerySet]) -> str:
    return "\n".join([x[0].description for x in matched])

# =========== Main Real (extracted so it can be tested...) ===========

def main_inner(source_file: str, ruleset: str, output_style: str, rules_path: str) -> str:
    rules = load_rules(rules_path)
    match_query_sets = resolve_rules(rules, ruleset)

    matches = check_usage(source_file, match_query_sets)

    if output_style == "id":
        return format_output_ids(matches)
    elif output_style == "desc":
        return format_output_full_error_report(matches)
    elif output_style == "brief":
        return format_output_brief_descriptions(matches)

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
