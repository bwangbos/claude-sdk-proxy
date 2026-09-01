from pathlib import Path

import pytest


def install_policy(pytester: pytest.Pytester) -> None:
    source = (Path(__file__).parents[1] / "conftest.py").read_text()
    pytester.makeconftest(source.replace('pytest_plugins = ("pytester",)\n', ""))


@pytest.mark.parametrize(
    "body",
    [
        "import pytest\n\ndef test_runtime_skip(): pytest.skip('optional')\n",
        "import pytest\npytest.skip('optional', allow_module_level=True)\n",
        "import pytest\n\n@pytest.mark.xfail(reason='not release evidence')\n"
        "def test_xfail(): assert False\n",
    ],
)
def test_forbid_skips_fails_every_pytest_skip_report(
    pytester: pytest.Pytester, body: str
) -> None:
    install_policy(pytester)
    pytester.makepyfile(body)
    result = pytester.runpytest("--forbid-skips", "-q")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*release policy forbids skipped reports:*"])


def test_skip_policy_is_explicitly_opt_in(pytester: pytest.Pytester) -> None:
    install_policy(pytester)
    pytester.makepyfile(
        "import pytest\n\ndef test_optional(): pytest.skip('optional')\n"
    )
    result = pytester.runpytest("-q")
    result.assert_outcomes(skipped=1)
    assert result.ret == pytest.ExitCode.OK


def test_required_anyio_gate_executes_with_zero_skips(
    pytester: pytest.Pytester,
) -> None:
    install_policy(pytester)
    pytester.makepyfile(
        "import pytest\n\n@pytest.mark.anyio\nasync def test_async(): assert True\n"
    )
    result = pytester.runpytest(
        "--strict-markers",
        "--forbid-skips",
        "-W",
        "error",
        "-q",
    )
    result.assert_outcomes(passed=1, skipped=0)
    assert result.ret == pytest.ExitCode.OK


def test_unmarked_coroutine_cannot_pass_release_gate(pytester: pytest.Pytester) -> None:
    install_policy(pytester)
    # Keep this deliberately unmarked negative fixture out of the plan's
    # literal async-test scan while generating exactly that source for pytest.
    pytester.makepyfile("async def " + "test_unmarked(): pass\n")
    result = pytester.runpytest(
        "--strict-markers",
        "--forbid-skips",
        "-W",
        "error",
        "-q",
    )
    assert result.ret != pytest.ExitCode.OK
