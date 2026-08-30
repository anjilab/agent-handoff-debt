"""SWE-bench Verified task download, materialization, and validation helpers."""

from __future__ import annotations

import json
import re
import random
import shutil
import tomllib
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

PROMPT_TEMPLATE_PATH = Path("templates/swebench_openhands_default.md")
ELIGIBLE_DIFFICULTIES = {"15 min - 1 hour", "1-4 hours"}
TARGET_DIFFICULTY_CYCLE = [
    "15 min - 1 hour",
    "15 min - 1 hour",
    "1-4 hours",
]


def slugify_instance_id(instance_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", instance_id.strip())
    slug = slug.strip("._-")
    if not slug:
        raise ValueError("SWE-bench instance_id cannot be empty")
    return slug


def official_swebench_image(instance_id: str) -> str:
    repo, name = instance_id.split("__", 1)
    return f"docker.io/swebench/sweb.eval.x86_64.{repo}_1776_{name}:latest".lower()


def repo_dir_name(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


def benchmark_repo_path(repo: str) -> str:
    return "/testbed"


def render_prompt_template(
    *,
    repo_path: str,
    problem_statement: str,
    base_commit: str,
    template_path: Path = PROMPT_TEMPLATE_PATH,
) -> str:
    template = template_path.read_text()
    # The OpenHands benchmark template uses an `instance.*` namespace. Keep
    # these replacements tiny so we can mirror the upstream prompt directly.
    values = {
        "{{ repo_path }}": repo_path,
        "{{ problem_statement }}": problem_statement,
        "{{ base_commit }}": base_commit,
        "{{ instance.repo_path }}": repo_path,
        "{{ instance.problem_statement }}": problem_statement,
        "{{ instance.base_commit }}": base_commit,
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    return template.rstrip() + "\n"


@dataclass(frozen=True)
class SwebenchInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    test_patch: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    difficulty: str = "unknown"
    created_at: str = ""
    hints_text: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SwebenchInstance":
        return cls(
            instance_id=str(data["instance_id"]),
            repo=str(data["repo"]),
            base_commit=str(data["base_commit"]),
            problem_statement=str(data["problem_statement"]),
            test_patch=str(data.get("test_patch") or ""),
            fail_to_pass=_parse_test_list(data.get("FAIL_TO_PASS")),
            pass_to_pass=_parse_test_list(data.get("PASS_TO_PASS")),
            difficulty=str(data.get("difficulty") or "unknown"),
            created_at=str(data.get("created_at") or ""),
            hints_text=str(data.get("hints_text") or ""),
        )


def _parse_test_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    raise ValueError(f"Expected SWE-bench test list, got {type(value).__name__}")


def load_swebench_instances(path: Path) -> list[SwebenchInstance]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return [
            SwebenchInstance.from_dict(json.loads(line))
            for line in path.read_text().splitlines()
            if line.strip()
        ]
    if suffix == ".json":
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            data = data.get("instances") or data.get("data") or [data]
        if not isinstance(data, list):
            raise ValueError(f"Expected JSON list of SWE-bench instances in {path}")
        return [SwebenchInstance.from_dict(item) for item in data]
    if suffix == ".toml":
        data = tomllib.loads(path.read_text())
        instances = data.get("instances") or []
        if not isinstance(instances, list):
            raise ValueError(f"Expected [[instances]] entries in {path}")
        return [SwebenchInstance.from_dict(item) for item in instances]
    raise ValueError(f"Unsupported SWE-bench manifest extension: {path.suffix}")


def download_swebench_verified_jsonl(
    out_path: Path,
    *,
    limit: int,
    offset: int = 0,
    dataset: str = "SWE-bench/SWE-bench_Verified",
    split: str = "test",
) -> None:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    query = urllib.parse.urlencode(
        {
            "dataset": dataset,
            "config": "default",
            "split": split,
            "offset": offset,
            "length": limit,
        }
    )
    url = f"https://datasets-server.huggingface.co/rows?{query}"
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("rows") or []
    if not rows:
        raise RuntimeError(f"No rows returned by Hugging Face datasets API: {url}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for item in rows:
            row = item.get("row") if isinstance(item, dict) else None
            if not isinstance(row, dict):
                raise RuntimeError(f"Unexpected row payload from Hugging Face API: {item!r}")
            f.write(json.dumps(row, sort_keys=True) + "\n")


def select_instances(
    instances: Iterable[SwebenchInstance],
    *,
    instance_ids: list[str],
    limit: int | None,
) -> list[SwebenchInstance]:
    items = list(instances)
    if instance_ids:
        by_id = {item.instance_id: item for item in items}
        missing = [instance_id for instance_id in instance_ids if instance_id not in by_id]
        if missing:
            raise ValueError(f"Missing SWE-bench instance ids: {', '.join(missing)}")
        return [by_id[instance_id] for instance_id in instance_ids]
    if limit is not None:
        return items[:limit]
    return items


def ordered_instances(
    instances: Iterable[SwebenchInstance],
    *,
    seed: int,
) -> list[SwebenchInstance]:
    """Return a repo-covered, difficulty-balanced, prefix-stable order."""

    by_bucket_repo: dict[str, dict[str, list[SwebenchInstance]]] = {
        difficulty: {} for difficulty in TARGET_DIFFICULTY_CYCLE
    }
    for instance in instances:
        if instance.difficulty not in ELIGIBLE_DIFFICULTIES:
            continue
        by_bucket_repo.setdefault(instance.difficulty, {}).setdefault(
            instance.repo, []
        ).append(instance)

    rng = random.Random(seed)
    for by_repo in by_bucket_repo.values():
        for items in by_repo.values():
            rng.shuffle(items)

    ordered: list[SwebenchInstance] = []
    selected_by_repo: Counter[str] = Counter()
    cycle_index = 0

    while any(items for by_repo in by_bucket_repo.values() for items in by_repo.values()):
        preferred = TARGET_DIFFICULTY_CYCLE[cycle_index % len(TARGET_DIFFICULTY_CYCLE)]
        cycle_index += 1
        difficulty = (
            preferred
            if any(by_bucket_repo.get(preferred, {}).values())
            else _next_available_difficulty(by_bucket_repo)
        )
        if difficulty is None:
            break
        by_repo = by_bucket_repo[difficulty]
        repo = _next_repo_for_bucket(by_repo, selected_by_repo)
        ordered.append(by_repo[repo].pop(0))
        selected_by_repo[repo] += 1
    return ordered


def _next_available_difficulty(
    by_bucket_repo: dict[str, dict[str, list[SwebenchInstance]]],
) -> str | None:
    for difficulty in TARGET_DIFFICULTY_CYCLE:
        if any(by_bucket_repo.get(difficulty, {}).values()):
            return difficulty
    return None


def _next_repo_for_bucket(
    by_repo: dict[str, list[SwebenchInstance]],
    selected_by_repo: Counter[str],
) -> str:
    candidates = [repo for repo, items in by_repo.items() if items]
    return min(candidates, key=lambda repo: (selected_by_repo[repo], repo))


def _difficulty_rank(difficulty: str) -> int:
    return {
        ">4 hours": 4,
        "1-4 hours": 3,
        "15 min - 1 hour": 2,
        "<15 min fix": 1,
    }.get(difficulty, 0)


def write_selected_manifests(
    instances: list[SwebenchInstance],
    *,
    output_dir: Path,
    sizes: list[int],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [_instance_source_row(instance) for instance in instances]
    _write_jsonl(output_dir / "ordered.jsonl", rows)
    (output_dir / "ordered.txt").write_text(
        "".join(f"{instance.instance_id}\n" for instance in instances)
    )

    for size in sorted(set(sizes)):
        selected = rows[:size]
        suffix = f"{size:03d}"
        _write_jsonl(output_dir / f"selected_{suffix}.jsonl", selected)
        (output_dir / f"selected_{suffix}.txt").write_text(
            "".join(f"{row['instance_id']}\n" for row in selected)
        )
    (output_dir / "selection_report.md").write_text(selection_report(instances, sizes=sizes))


def selection_report(instances: list[SwebenchInstance], *, sizes: list[int]) -> str:
    lines = [
        "# SWE-bench Verified Selection",
        "",
        (
            "Selection is deterministic and prefix-stable: larger task sets keep "
            "all earlier tasks."
        ),
        "",
        (
            "Policy: repository coverage plus difficulty balance. The "
            "eligible difficulty bands are `15 min - 1 hour` and `1-4 hours`; "
            "tasks labeled `<15 min fix`, `>4 hours`, or unknown are excluded. "
            "The prefix order targets roughly two `15 min - 1 hour` tasks for "
            "each `1-4 hours` task, while spreading selections across "
            "repositories within each difficulty bucket."
        ),
        "",
        "## Prefixes",
        "",
    ]
    for size in sorted(set(sizes)):
        available = min(size, len(instances))
        lines.append(
            f"- first {size}: `selected_{size:03d}.jsonl` ({available} rows available)"
        )
    lines.extend(["", "## Repository Distribution", ""])
    for repo, count in sorted(Counter(instance.repo for instance in instances).items()):
        lines.append(f"- `{repo}`: {count}")
    lines.extend(["", "## Difficulty Distribution", ""])
    for difficulty, count in sorted(
        Counter(instance.difficulty for instance in instances).items()
    ):
        lines.append(f"- `{difficulty}`: {count}")
    lines.extend(["", "## Ordered Tasks", ""])
    for index, instance in enumerate(instances, start=1):
        lines.append(
            f"{index}. `{instance.instance_id}` - repo `{instance.repo}`"
        )
    return "\n".join(lines) + "\n"


def _instance_source_row(instance: SwebenchInstance) -> dict[str, Any]:
    return {
        "FAIL_TO_PASS": json.dumps(instance.fail_to_pass),
        "PASS_TO_PASS": json.dumps(instance.pass_to_pass),
        "base_commit": instance.base_commit,
        "instance_id": instance.instance_id,
        "difficulty": instance.difficulty,
        "created_at": instance.created_at,
        "problem_statement": instance.problem_statement,
        "repo": instance.repo,
        "test_patch": instance.test_patch,
        "hints_text": instance.hints_text,
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def materialize_swebench_task(
    instance: SwebenchInstance,
    *,
    tasks_dir: Path,
    configs_dir: Path,
    overwrite: bool = False,
) -> tuple[Path, Path]:
    # Materialization keeps task text/config local; the repo comes from /testbed
    # inside the OpenHands SWE-bench image at run time.
    task_slug = slugify_instance_id(instance.instance_id)
    task_dir = tasks_dir / task_slug
    prompt_path = task_dir / "task.md"
    config_path = configs_dir / f"{task_slug}.toml"
    preserved_server_image = _existing_openhands_server_image(config_path)

    if task_dir.exists():
        if overwrite:
            shutil.rmtree(task_dir)
            if config_path.exists():
                config_path.unlink()
            task_dir.mkdir(parents=True)
        elif prompt_path.exists() or config_path.exists():
            raise FileExistsError(f"Task directory already exists: {task_dir}")
    else:
        task_dir.mkdir(parents=True)
    configs_dir.mkdir(parents=True, exist_ok=True)

    try:
        prompt_path.write_text(build_task_prompt(instance))
        config_path.write_text(
            "\n".join(
                [
                    "[task]",
                    f"repo = {json.dumps(instance.repo)}",
                    f"repo_dir = {json.dumps(repo_dir_name(instance.repo))}",
                    f"remote_repo_path = {json.dumps(benchmark_repo_path(instance.repo))}",
                    f"prompt = {json.dumps(prompt_path.as_posix())}",
                    f"difficulty = {json.dumps(instance.difficulty)}",
                    f"swebench_base_commit = {json.dumps(instance.base_commit)}",
                    f"swebench_official_image = {json.dumps(official_swebench_image(instance.instance_id))}",
                    *(
                        [f"openhands_server_image = {json.dumps(preserved_server_image)}"]
                        if preserved_server_image
                        else []
                    ),
                    *build_official_swebench_validation_lines(instance),
                    "",
                ]
            )
        )
    except Exception:
        if prompt_path.exists():
            prompt_path.unlink()
        if config_path.exists():
            config_path.unlink()
        raise
    return config_path, task_dir


def build_task_prompt(instance: SwebenchInstance) -> str:
    return render_prompt_template(
        repo_path=benchmark_repo_path(instance.repo),
        problem_statement=instance.problem_statement,
        base_commit=instance.base_commit,
    )


def _existing_openhands_server_image(config_path: Path) -> str | None:
    if not config_path.exists():
        return None
    data = tomllib.loads(config_path.read_text())
    task = data.get("task") or {}
    value = task.get("openhands_server_image")
    return str(value) if value else None


def build_official_swebench_validation_lines(instance: SwebenchInstance) -> list[str]:
    return [
        'validation_backend = "swebench_official"',
        'swebench_dataset = "SWE-bench/SWE-bench_Verified"',
        'swebench_split = "test"',
        f"swebench_instance_id = {json.dumps(instance.instance_id)}",
        'swebench_model_name = "handoff-debt"',
    ]
