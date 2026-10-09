import shutil
import subprocess
from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.persistence.models import Repository
from app.workspace.manager import WorkspaceManager
from app.workspace.reader import read_file
from app.workspace.search import search_code

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@test")
    git(repo, "config", "user.name", "t")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 8)) + "\nneedle here\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("needle in readme\nplain\n", encoding="utf-8")
    (repo / "src" / "extra.py").write_text("needle one\nneedle two\nneedle three\n", encoding="utf-8")
    (repo / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    (repo / "big.txt").write_text("x" * 4096, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial")
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, sha


@pytest.fixture
def workspace(settings, source_repo):
    repo_dir, _sha = source_repo
    settings.max_code_file_bytes = 2048
    settings.max_read_lines = 4
    settings.max_search_matches = 10
    manager = WorkspaceManager(settings, token="", remote_url=lambda _repo: str(repo_dir))
    return manager


def repo_row():
    return Repository(
        id=uuid4(), provider="github", external_id="1", full_name="octo/demo", default_branch="main"
    )


def test_read_file_range_and_bounds(settings, workspace, source_repo):
    _, sha = source_repo
    repo = repo_row()
    slice_ = read_file(settings, workspace, repo, sha, "src/app.py", start_line=2, end_line=4)
    assert slice_.text == "line 2\nline 3\nline 4"
    assert (slice_.line_start, slice_.line_end, slice_.total_lines, slice_.truncated) == (2, 4, 8, True)

    full = read_file(settings, workspace, repo, sha, "src/app.py")
    assert full.line_start == 1 and full.line_end == 4 and full.truncated  # max_read_lines = 4


def test_read_file_rejections(settings, workspace, source_repo):
    _, sha = source_repo
    repo = repo_row()
    with pytest.raises(DomainError) as caught:
        read_file(settings, workspace, repo, sha, "../.env")
    assert caught.value.code == "PATH_REJECTED"
    with pytest.raises(DomainError) as caught:
        read_file(settings, workspace, repo, sha, "bin.dat")
    assert caught.value.code == "CODE_BINARY_UNSUPPORTED"
    with pytest.raises(DomainError) as caught:
        read_file(settings, workspace, repo, sha, "big.txt")
    assert caught.value.code == "CODE_FILE_TOO_LARGE"
    with pytest.raises(DomainError) as caught:
        read_file(settings, workspace, repo, sha, "missing.txt")
    assert caught.value.code == "CODE_PATH_NOT_FOUND"
    with pytest.raises(DomainError) as caught:
        read_file(settings, workspace, repo, sha, "src/app.py", start_line=99)
    assert caught.value.code == "CODE_LINE_RANGE"


def test_search_fixed_string_with_glob_and_pagination(settings, workspace, source_repo):
    _, sha = source_repo
    repo = repo_row()
    results = search_code(settings, workspace, repo, sha, "needle")
    assert results.total_matches == 5  # 1 + 1 + 3
    paths = [hit.path for hit in results.hits]
    assert paths == sorted(paths)  # deterministic tree order

    py_only = search_code(settings, workspace, repo, sha, "needle", path_glob="*.py")
    assert {hit.path for hit in py_only.hits} == {"src/app.py", "src/extra.py"}

    settings.max_search_matches = 2
    page1 = search_code(settings, workspace, repo, sha, "needle")
    assert len(page1.hits) == 2 and page1.next_cursor and page1.truncated
    page2 = search_code(settings, workspace, repo, sha, "needle", cursor=page1.next_cursor)
    assert len(page2.hits) == 2
    page3 = search_code(settings, workspace, repo, sha, "needle", cursor=page2.next_cursor)
    assert len(page3.hits) == 1 and page3.next_cursor is None


def test_search_special_characters_are_literal(settings, workspace, source_repo):
    _, sha = source_repo
    repo = repo_row()
    assert search_code(settings, workspace, repo, sha, "line 1").total_matches == 1
    assert search_code(settings, workspace, repo, sha, "n[ee]dle").total_matches == 0
    assert search_code(settings, workspace, repo, sha, "zzz").total_matches == 0


def test_unknown_sha_is_unavailable(settings, workspace, source_repo):
    repo = repo_row()
    with pytest.raises(DomainError) as caught:
        read_file(settings, workspace, repo, "0" * 40, "README.md")
    assert caught.value.code == "CODE_UNAVAILABLE"


def test_invalid_sha_format_rejected(settings, workspace):
    repo = repo_row()
    with pytest.raises(DomainError) as caught:
        read_file(settings, workspace, repo, "not-a-sha!", "README.md")
    assert caught.value.code == "CODE_UNAVAILABLE"


def test_git_env_includes_proxy_and_token(settings):
    import base64

    settings.github_git_proxy = "http://127.0.0.1:7897"
    manager = WorkspaceManager(settings, token="secret-token")
    env = manager._auth_env()
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    encoded = base64.b64encode(b"x-access-token:secret-token").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {encoded}"
    assert env["GIT_CONFIG_KEY_1"] == "http.proxy"
    assert env["GIT_CONFIG_VALUE_1"] == "http://127.0.0.1:7897"
    assert env["GIT_CONFIG_KEY_2"] == "https.proxy"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_mirror_reuses_and_fetches_new_commits(settings, workspace, source_repo):
    repo_dir, sha1 = source_repo
    repo = repo_row()
    first = read_file(settings, workspace, repo, sha1, "README.md")
    assert "needle" in first.text

    (repo_dir / "new.txt").write_text("added later\n", encoding="utf-8")
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-qm", "second")
    sha2 = git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    second = read_file(settings, workspace, repo, sha2, "new.txt")
    assert second.text == "added later"
