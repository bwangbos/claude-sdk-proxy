from __future__ import annotations

import pytest

pytest_plugins = ("pytester",)
_SKIPPED_REPORTS: set[tuple[str, str]] = set()


@pytest.fixture
def anyio_backend() -> str:
    """Run the single supported async backend exactly once per test."""
    return "asyncio"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("release-policy").addoption(
        "--forbid-skips",
        action="store_true",
        default=False,
        help="fail this pytest session if any collection or test report is skipped",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    _SKIPPED_REPORTS.clear()


def _record_skip(report: pytest.CollectReport | pytest.TestReport) -> None:
    if report.skipped:
        _SKIPPED_REPORTS.add((report.nodeid, getattr(report, "when", "collect")))


def pytest_collectreport(report: pytest.CollectReport) -> None:
    _record_skip(report)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    _record_skip(report)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not session.config.getoption("--forbid-skips") or not _SKIPPED_REPORTS:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        rendered = ", ".join(
            f"{nodeid}[{when}]" for nodeid, when in sorted(_SKIPPED_REPORTS)
        )
        reporter.write_sep("=", f"release policy forbids skipped reports: {rendered}")
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
