import pytest

from app.core.errors import DomainError
from app.workspace.path_policy import validate_path_glob, validate_relative_path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "C:/windows/system32",
        "a/../../b",
        "../up",
        "dir/..",
        "a//b",
        "a/./b",
        "a\\b",
        "dir:stream/file",
        "file:ads",
        "trail./x",
        "trail /x",
        "a\x00b",
        "x" * 2000,
    ],
)
def test_rejected_paths(path):
    with pytest.raises(DomainError) as caught:
        validate_relative_path(path)
    assert caught.value.code == "PATH_REJECTED"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/app.py", "src/app.py"),
        ("docs/read me.md", "docs/read me.md"),
        ("a/b/c.txt", "a/b/c.txt"),
        ("README.md", "README.md"),
    ],
)
def test_accepted_paths(path, expected):
    assert validate_relative_path(path) == expected


def test_path_glob_rules():
    assert validate_path_glob(None) is None
    assert validate_path_glob("src/*.py") == "src/*.py"
    for bad in ["", "/abs/*.py", "../*.py", "a:b/*.py", "a\\b/*.py"]:
        with pytest.raises(DomainError):
            validate_path_glob(bad)
