from pathlib import Path

import pytest

from slop.config import SlopError
from slop.gen.images import job_prompt, load_jobs, resolve_ref
from slop.gen.project_inputs import candidate_directory, stage_references, write_candidate


def test_job_prompt_stays_inside_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    inside = project / "prompt.txt"
    inside.write_text("  Sample prompt  ", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("Outside sample", encoding="utf-8")

    assert job_prompt(project, {"prompt_file": "prompt.txt"}) == "Sample prompt"
    assert job_prompt(project, {"prompt_file": str(inside)}) == "Sample prompt"
    for name in ("../outside.txt", str(outside)):
        with pytest.raises(SlopError, match="inside the project"):
            job_prompt(project, {"prompt_file": name})


def test_job_reference_is_staged_without_changing_its_id(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    picture = project / "reference.png"
    picture.write_bytes(b"sample")
    ref = resolve_ref(project, "reference.png", in_project=True)

    assert ref["id"] == "reference.png"
    assert ref["project_file"] is True
    with stage_references(project, [ref["src"]], [ref["src"]]) as sources:
        staged = Path(sources[0])
        assert staged != picture
        assert staged.suffix == ".png"
        assert staged.read_bytes() == b"sample"
    assert not staged.exists()


def test_job_reference_rejects_symlink_and_keeps_explicit_ref(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    picture = tmp_path / "reference.png"
    picture.write_bytes(b"sample")
    (project / "linked.png").symlink_to(picture)

    with pytest.raises(SlopError, match="symbolic links"):
        resolve_ref(project, "linked.png", in_project=True)
    assert resolve_ref(project, str(picture))["src"] == str(picture)


def test_job_plate_is_an_identifier(tmp_path):
    assert candidate_directory(tmp_path, "shot-2") == tmp_path / "plates" / "cands" / "shot-2"
    with pytest.raises(SlopError, match="plate ID"):
        candidate_directory(tmp_path, "../shot-2")


def test_job_manifest_and_output_require_project_paths(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    manifest = tmp_path / "source.json"
    manifest.write_text('[{"plate":"shot-2"}]', encoding="utf-8")
    link = project / "jobs.json"
    link.symlink_to(manifest)
    with pytest.raises(SlopError, match="symbolic links"):
        load_jobs(project, link)

    directory = candidate_directory(project, "shot-2")
    first = write_candidate(project, directory, ".png", b"first", {"job": "shot-2"})
    second = write_candidate(project, directory, ".png", b"second", {"job": "shot-2"})
    assert first.name == "1.png" and first.read_bytes() == b"first"
    assert second.name == "2.png" and second.with_suffix(".json").is_file()
