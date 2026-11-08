"""Releasing what a kept run left behind, from a later process.

`keep` holds resources past the end of a run so the next one need not rebuild
them. Whoever asked for that eventually has to give them back, and by then the
`_RunState` that held their acquired values is gone. The journal's retention
records are what survives, so releasing from a later process means reading them
rather than re-running the workflow that created them.

Deliberately not fingerprint-checked. Resume refuses a journal whose workflow
shape changed, because reusing evidence across a different topology is unsound.
Releasing is the opposite: a VM left running does not care that the DAG moved on,
and refusing to release it is how it stays up.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from sonata_engine.core.inputs import TaskInputs
from sonata_engine.core.resource_task import Resource
from sonata_engine.core.slug import slugify
from sonata_engine.core.workflow import _StepScope
from sonata_engine.journal import SCHEMA_VERSION, JournalConfig


class UnknownRetainedResourceError(LookupError):
    """The journal names a retained resource the caller did not supply.

    Skipping it would report a clean teardown while the resource keeps running,
    which is the failure this whole path exists to remove.
    """


def _records(config: JournalConfig) -> list[dict[str, Any]]:
    if not config.path.exists():
        return []
    return [
        record
        for record in (
            json.loads(line)
            for line in config.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if isinstance(record, dict)
    ]


def _outstanding(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retention records with no matching release, newest state per resource."""
    released = {
        record.get("resource") for record in records if record.get("kind") == "released"
    }
    return [
        record
        for record in records
        if record.get("kind") == "retained" and record.get("resource") not in released
    ]


def release_retained(
    resources: Mapping[str, Resource[Any]], journal: JournalConfig
) -> tuple[str, ...]:
    """Release every resource the journal still records as retained.

    Resources the caller does not supply are released by nobody, but everything
    else still is: they are reported at the end rather than refused up front,
    because refusing would leave the supplied ones running too.

    Released in the order the records were written, which is already the order a
    run itself would have used: release units are spliced in reverse of
    acquisition, so the retention counter recorded them reverse-acquired. Every
    resource is attempted even when an earlier release fails -- stopping at the
    first would leave the rest running, which is the harm being addressed --
    and the errors are raised together at the end.

    Returns the titles released, in the order they were released.
    """
    outstanding = _outstanding(_records(journal))
    if not outstanding:
        return ()

    inputs = TaskInputs._for_resource_values({}, ())
    errors: list[BaseException] = []
    released: list[str] = []
    unknown: list[str] = []
    for record in sorted(outstanding, key=lambda item: int(item.get("order", 0))):
        title = str(record["resource"])
        resource = resources.get(title)
        if resource is None:
            # Release what we can and say what we could not. Refusing outright
            # would leave everything running -- including the resources the
            # caller *did* supply, which are usually the expensive ones.
            unknown.append(title)
            continue
        value = record.get("value")
        try:
            if resource.revive is not None:
                value = resource.revive(value)
            resource.release.run(
                replace(
                    inputs,
                    _upstream=value,
                    # No journal: the steps of a release run from a later
                    # process are not resumable, only the retention record is.
                    _step_scope=_StepScope(
                        prefix=f"retained/{slugify(title)}",
                        base_inputs=inputs,
                        jrnl=None,
                        resume=False,
                        journal_errors=errors,
                    ),
                )
            )
        except BaseException as error:  # NOSONAR S5754 - keep releasing; noqa: BLE001
            errors.append(error)
            continue
        _record_released(journal, record, title)
        released.append(title)

    if unknown:
        errors.append(
            UnknownRetainedResourceError(
                "journal records retained resources the caller did not supply: "
                + ", ".join(sorted(unknown))
            )
        )

    if errors:
        if len(errors) == 1:
            raise errors[0]
        combined = RuntimeError(
            "releasing retained resources failed:\n"
            + "\n".join(str(error) for error in errors)
        )
        for error in errors:
            combined.add_note(str(error))
        raise combined
    return tuple(released)


def _record_released(
    journal: JournalConfig, record: dict[str, Any], title: str
) -> None:
    entry = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": record.get("workflow_id"),
        "workflow_fingerprint": record.get("workflow_fingerprint"),
        "run_id": record.get("run_id"),
        "kind": "released",
        "resource": title,
    }
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
        handle.flush()
