"""
The traceability table has to cite tests that exist.

A requirements matrix is only worth the confidence it creates, and a row citing
a test name that was never written creates confidence with nothing underneath
it. Sixty of these accumulated across Tasks 1.1 and 1.2 — names written as
intentions, before the tests, that the eventual tests never took. Nothing
failed, because nothing checked.

This module is that check. Every citation in the `Test` / `Tests` column is
resolved against the real suite, by parsing the test files rather than by
running them: an import-time index is fast, needs no database, and cannot be
fooled by a test that is collected but skipped.

Run it directly to list what does not resolve:

    .venv\\Scripts\\python.exe -m tests.test_traceability

## What counts as evidence

A row may cite a test (`test_ledger.py::TestLots`), or it may cite something
that is not a test at all — a database constraint, a trigger, a migration. Both
are legitimate; an `EXCLUDE USING gist` constraint *is* the enforcement, and
saying so is more honest than pointing at a test that only observes it. The
distinguishing mark is the citation's shape, and unresolvable shapes are
reported rather than assumed harmless.

A row may also be honestly `Deferred`. What it may not be is `Done` on the
strength of a test that does not exist.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRACEABILITY = REPO_ROOT / "docs" / "requirements" / "traceability.md"

#: Where approved tasks are declared. `CLAUDE.md` fixes the Phase 0 order in
#: prose; every later phase declares its tasks as `### Task X.Y` headings.
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
TASK_BREAKDOWNS = (
    REPO_ROOT / "docs" / "tasks" / "phase-1-task-breakdown.md",
    REPO_ROOT / "docs" / "tasks" / "phase-2-task-breakdown.md",
    REPO_ROOT / "docs" / "tasks" / "phase-3-task-breakdown.md",
    REPO_ROOT / "docs" / "tasks" / "phase-4-task-breakdown.md",
)

#: Where test modules live. Anything outside these trees is not a test, so a
#: citation that resolves into one of them is the only kind that counts.
TEST_ROOTS = (REPO_ROOT / "tests", REPO_ROOT / "apps")

#: A citation naming something other than a test: a constraint, a trigger, a
#: module, a policy. Recognised so the table can say "the database enforces
#: this" without the row being read as an unresolved test reference.
NON_TEST_MARKERS = ("—", "-", "n/a", "N/A")

#: Statuses a row may carry, and what each one promises.
#:
#: `Done` is the only one that obliges the row to cite an automated test, which
#: is why the others exist at all. A requirement satisfied by a decision has
#: nothing to run (`Documented`); one satisfied by a field's shape is inspected
#: rather than exercised (`Verified`); and one genuinely not built is better
#: recorded as `Deferred` than quietly dropped.
#: `Withdrawn` is not a synonym for deleted. A requirement that was approved,
#: built, and then removed from the product is itself a fact the matrix should
#: keep: the row stays, says so, and cites the migration that withdrew it
#: instead of a test that no longer exists.
KNOWN_STATUSES = frozenset(
    {"Done", "Partial", "Deferred", "Documented", "Verified", "Specified", "Withdrawn"}
)


@dataclass(frozen=True)
class Citation:
    """One backtick-quoted reference from a `Test` cell."""

    raw: str
    file_hint: str | None
    name: str | None


@dataclass
class Row:
    """One requirement row, with the line number that produced it."""

    line_number: int
    identifier: str
    status: str
    test_cell: str
    citations: list[Citation] = field(default_factory=list)


@dataclass
class SuiteIndex:
    """
    Every test name in the repository, addressable the way the table cites them.

    Citations use bare file names (`test_ledger.py`) rather than paths, because
    the table is read by people. A bare name that matches two files is an
    ambiguity the index refuses to resolve rather than guess at.
    """

    by_basename: dict[str, list[Path]]
    names_by_path: dict[Path, set[str]]

    def resolve_file(self, basename: str) -> tuple[Path | None, str | None]:
        # A citation may give the full repository path instead of the bare
        # name. Both are readable; only the bare name can be ambiguous.
        if "/" in basename or "\\" in basename:
            full = REPO_ROOT / Path(basename.replace("\\", "/"))
            if full in self.names_by_path:
                return full, None
            return None, f"no test file at {basename}"

        candidates = self.by_basename.get(basename, [])
        if not candidates:
            return None, f"no test file named {basename}"
        if len(candidates) > 1:
            listed = ", ".join(str(path.relative_to(REPO_ROOT)) for path in candidates)
            return None, f"{basename} is ambiguous ({listed})"
        return candidates[0], None

    def has_name(self, path: Path, name: str) -> bool:
        return name in self.names_by_path.get(path, set())


def build_test_index() -> SuiteIndex:
    """
    Parse every `test_*.py` and record what a citation could legitimately name.

    Three shapes are indexed for each file: the bare class name (`TestLots`),
    the bare function name (`test_an_expired_lot_cannot_be_issued`, whether
    module-level or a method), and the qualified pair (`TestLots::test_x`).
    Methods are indexed unqualified as well because that is how the table cites
    them, and requiring the class would be a churn of edits with no added
    truth.
    """
    by_basename: dict[str, list[Path]] = {}
    names_by_path: dict[Path, set[str]] = {}

    for root in TEST_ROOTS:
        for path in sorted(root.rglob("test_*.py")):
            names: set[str] = set()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    names.add(node.name)
                elif isinstance(node, ast.ClassDef):
                    names.add(node.name)
                    for child in node.body:
                        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                            names.add(child.name)
                            names.add(f"{node.name}::{child.name}")
            names_by_path[path] = names
            by_basename.setdefault(path.name, []).append(path)

    return SuiteIndex(by_basename=by_basename, names_by_path=names_by_path)


def parse_citations(cell: str) -> list[Citation]:
    """
    Split a `Test` cell into its individual references.

    Only backtick-quoted text is a citation. Prose in the same cell ("plus the
    trigger", "see ADR-021") is commentary and is deliberately ignored — the
    alternative is a table where every explanatory word has to be escaped.
    """
    citations: list[Citation] = []
    for quoted in re.findall(r"`([^`]+)`", cell):
        text = quoted.strip()
        if not text or text in NON_TEST_MARKERS:
            continue
        if "::" in text:
            head, _, name = text.partition("::")
            citations.append(Citation(raw=text, file_hint=head.strip() or None, name=name.strip()))
        elif text.endswith(".py"):
            citations.append(Citation(raw=text, file_hint=text, name=None))
        else:
            # A constraint, trigger, service or module name. Not a test claim,
            # so not this module's business.
            continue
    return citations


def parse_rows(markdown: str) -> list[Row]:
    """
    Read the requirement rows out of the document.

    The file holds several tables with different column counts, so the `Test`
    column is found by its header rather than by position. A row is only read
    once a header has established where its columns are; stray pipes in prose
    therefore parse as nothing at all.
    """
    rows: list[Row] = []
    test_column: int | None = None
    status_column: int | None = None
    identifier_column: int | None = None

    for line_number, line in enumerate(markdown.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        lowered = [cell.lower() for cell in cells]

        if "status" in lowered and any(cell in {"test", "tests"} for cell in lowered):
            test_column = next(
                index for index, cell in enumerate(lowered) if cell in {"test", "tests"}
            )
            status_column = lowered.index("status")
            identifier_column = 0
            continue

        if set(stripped) <= set("|-: "):  # the ---|---|--- separator
            continue
        if test_column is None or status_column is None or identifier_column is None:
            continue
        if max(test_column, status_column) >= len(cells):
            continue

        identifier = cells[identifier_column].strip("` ")
        if not re.fullmatch(r"[A-Z]{2,4}-\d{3}", identifier):
            continue

        rows.append(
            Row(
                line_number=line_number,
                identifier=identifier,
                status=cells[status_column],
                test_cell=cells[test_column],
            )
        )
    return rows


def resolve(rows: list[Row], index: SuiteIndex) -> list[str]:
    """
    Check every citation, and return one message per problem.

    A citation written as `::name` inherits the file from the last full
    citation above it — the table's own shorthand for a run of rows covered by
    one test module. The inheritance is sequential and document-wide, exactly
    as a reader following the column downwards would resolve it.
    """
    problems: list[str] = []
    current_file: Path | None = None

    for row in rows:
        where = f"line {row.line_number} {row.identifier}"

        if row.status not in KNOWN_STATUSES:
            problems.append(f"{where}: unknown status {row.status!r}")

        row.citations = parse_citations(row.test_cell)

        if row.status == "Done" and not row.citations:
            problems.append(f"{where}: marked Done and cites no evidence")

        for citation in row.citations:
            if citation.file_hint is not None:
                resolved, failure = index.resolve_file(citation.file_hint)
                if failure is not None:
                    problems.append(f"{where}: {failure} (cites `{citation.raw}`)")
                    continue
                current_file = resolved

            if citation.name is None:
                continue
            if current_file is None:
                problems.append(
                    f"{where}: `{citation.raw}` has no file, and none was cited above it"
                )
                continue
            if not index.has_name(current_file, citation.name):
                relative = current_file.relative_to(REPO_ROOT)
                problems.append(f"{where}: {relative} has no {citation.name!r}")

    return problems


def audit() -> list[str]:
    index = build_test_index()
    rows = parse_rows(TRACEABILITY.read_text(encoding="utf-8"))
    if not rows:
        return ["the traceability table parsed to zero rows"]
    return resolve(rows, index)


# ---------------------------------------------------------------------------
# Coverage: does every approved task appear here at all?
# ---------------------------------------------------------------------------
#
# The check above resolves each row to a real test, which a task with **no
# rows** passes trivially. That is not a hypothetical: Task 0.6, the
# accounting journal kernel every other module posts through, had no section
# and no rows while a hundred of its tests passed — and this file reported
# clean the whole time. Citations were never the only thing worth checking;
# they were only the thing that had been checked.


def approved_tasks() -> dict[str, str]:
    """
    Every task the plan approved, as `id -> title`.

    Read from the documents that declare them rather than from a list kept
    here, so a task added to a breakdown is in scope for this check the
    moment it is written down, without anybody remembering to update a test.
    """
    tasks: dict[str, str] = {}

    # Phase 0 is prose in CLAUDE.md: "0.1 bootstrap · 0.2 custom User · ..."
    text = CLAUDE_MD.read_text(encoding="utf-8")
    phase_zero = re.search(r"Phase 0 task order:(.+?)(?:\n\n|##)", text, re.DOTALL)
    if phase_zero:
        for number, title in re.findall(r"(0\.\d)\s+([^·\n]+)", phase_zero.group(1)):
            tasks[number] = title.strip(" .·")

    for breakdown in TASK_BREAKDOWNS:
        for number, title in re.findall(
            r"^###\s+Task\s+(\d+\.\d+[A-Z]?)\s+—\s+(.+)$",
            breakdown.read_text(encoding="utf-8"),
            re.MULTILINE,
        ):
            tasks[number] = title.strip()

    return tasks


def covered_tasks(markdown: str) -> set[str]:
    """
    The tasks this document actually accounts for, three ways.

    A `## Task X.Y` heading, a task number in a `Task` column, or an explicit
    row in the Coverage table for the ones whose evidence legitimately lives
    somewhere else. The third is deliberately explicit: declaring a task
    covered is then a visible line in a diff rather than an absence nobody
    notices.
    """
    covered: set[str] = set(re.findall(r"^##\s+Task\s+(\d+\.\d+[A-Z]?)\b", markdown, re.MULTILINE))

    # The column mapping deliberately survives an intervening heading, exactly
    # as `parse_rows` lets it: the Phase 1 rows sit below "How to read the
    # evidence column" while using the header declared above it, and a reset
    # on every heading would silently stop reading two hundred and eighty rows.
    task_column: int | None = None
    in_coverage = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_coverage = stripped.lower().startswith("## coverage")
            continue
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        lowered = [cell.lower() for cell in cells]

        if in_coverage:
            # The Coverage table's first column is the task itself.
            if cells and re.fullmatch(r"\d+\.\d+[A-Z]?", cells[0]):
                covered.add(cells[0])
            continue

        if "task" in lowered:
            task_column = lowered.index("task")
            continue
        if task_column is not None and task_column < len(cells):
            for number in re.findall(r"\d+\.\d+[A-Z]?", cells[task_column]):
                covered.add(number)

    return covered


def test_every_approved_task_is_represented_in_traceability() -> None:
    """
    A task with no traceability at all must fail, even when every citation
    that *is* written resolves perfectly.

    The fix for a failure here is a section, rows, or — when the evidence
    genuinely lives elsewhere — an explicit line in the Coverage table saying
    where and why. It is never to delete the task from its breakdown.
    """
    markdown = TRACEABILITY.read_text(encoding="utf-8")
    tasks = approved_tasks()
    assert tasks, "parsed no approved tasks; the breakdown format must have changed"

    covered = covered_tasks(markdown)
    missing = sorted(
        f"{number} ({title})" for number, title in tasks.items() if number not in covered
    )
    assert not missing, (
        "approved tasks with no traceability coverage at all:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd a `## Task X.Y` section, rows carrying the number in a Task "
        "column, or a Coverage row saying where the evidence lives."
    )


def test_the_traceability_table_is_parsed_at_all() -> None:
    """
    A parser that silently matches nothing would make every other check pass.

    This is the guard on the guard: if a future edit changes the table's shape
    enough that the column headers stop being recognised, this fails first and
    says so, instead of the suite going quietly green on an empty list.
    """
    rows = parse_rows(TRACEABILITY.read_text(encoding="utf-8"))
    assert len(rows) > 200, f"only parsed {len(rows)} requirement rows"
    assert any(row.identifier.startswith("INV-") for row in rows)
    assert any(row.identifier.startswith("ORG-") for row in rows)


def test_every_cited_test_exists() -> None:
    """
    No row may cite a test that was never written.

    This is the check that was missing while sixty rows pointed at names from a
    plan. Failure output lists each row and what could not be found, so the fix
    is to correct the citation — never to delete the row.
    """
    problems = audit()
    assert not problems, "traceability cites tests that do not exist:\n  " + "\n  ".join(problems)


def test_no_row_claims_done_without_evidence() -> None:
    """A `Done` row with an empty evidence column is an unsupported claim."""
    rows = parse_rows(TRACEABILITY.read_text(encoding="utf-8"))
    bare = [
        f"line {row.line_number} {row.identifier}"
        for row in rows
        if row.status == "Done" and not parse_citations(row.test_cell)
    ]
    assert not bare, "rows marked Done with no evidence cited:\n  " + "\n  ".join(bare)


if __name__ == "__main__":  # pragma: no cover - developer entry point
    found = audit()
    for problem in found:
        print(problem)
    print(f"\n{len(found)} unresolved.")
    sys.exit(1 if found else 0)
