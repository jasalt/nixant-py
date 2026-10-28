import pytest
from typer.testing import CliRunner

from nixant.cli import app
from nixant.errors import UsageError
from nixant.naming import validate_instance_name


@pytest.mark.parametrize("name", ["a", "shop-dev", "a" * 63, "x1-y2"])
def test_valid_names(name: str) -> None:
    assert validate_instance_name(name) == name


@pytest.mark.parametrize("name", ["", "1a", "-a", "a-", "A", "a_b", "a" * 64, "a b"])
def test_invalid_names(name: str) -> None:
    with pytest.raises(UsageError):
        validate_instance_name(name)


def test_name_command_removed() -> None:
    result = CliRunner().invoke(app, ["name", "dev", "other"])
    assert result.exit_code == 2
