"""The System Advisor's two reaches outside the process.

**Inward** - ``advisor/probe.py`` is the fifth module in the app allowed to start
a process (``test_no_code_execution.py`` pins the list).  It earns that by being
a frozen table: a caller picks a *name*, never a program, a flag or a path.  The
table is asserted here literally, so adding a command is a visible edit to a
security test and not a convenience.

**Outward** - ``advisor/sources.py`` may read catalog metadata from six exact
hosts.  The tests below hold the allowlist to exact matches, prove it did not
widen any other list, and show ``fetch_json`` refusing a URL *before* a request
exists - with a client object that fails the test if it is touched at all.
"""

from __future__ import annotations

import ast
import dataclasses
import subprocess

import pytest

from app.advisor import probe, sources
from app.core.errors import UpstreamUnavailable
from app.enable import hosts

# ---------------------------------------------------------------------------
# The frozen command table
# ---------------------------------------------------------------------------

ALLOWED_PROGRAMS = {"nvidia-smi", "system_profiler", "sysctl", "vm_stat", "ioreg",
                    "lspci"}

#: The whole vocabulary, spelled out.  A new row must be added here on purpose.
EXPECTED_COMMANDS = {
    "nvidia_static": ("nvidia-smi", (
        "--query-gpu=index,name,memory.total,driver_version,pci.bus_id,uuid",
        "--format=csv,noheader,nounits")),
    "nvidia_live": ("nvidia-smi", (
        "--query-gpu=index,utilization.gpu,memory.used,memory.total,"
        "temperature.gpu,power.draw,fan.speed",
        "--format=csv,noheader,nounits")),
    "nvidia_procs": ("nvidia-smi", (
        "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
        "--format=csv,noheader,nounits")),
    "mac_displays": ("system_profiler", ("SPDisplaysDataType", "-json")),
    "mac_sysctl": ("sysctl", (
        "-n", "machdep.cpu.brand_string", "hw.memsize", "hw.physicalcpu",
        "hw.logicalcpu", "hw.perflevel0.physicalcpu", "hw.perflevel1.physicalcpu")),
    "mac_swap": ("sysctl", ("-n", "vm.swapusage")),
    "mac_vm_stat": ("vm_stat", ()),
    "mac_gpu_stats": ("ioreg", ("-r", "-d", "1", "-w", "0", "-c", "IOAccelerator")),
    "linux_lspci": ("lspci", ("-mm", "-nn")),
}

#: Fixed system directories a program may be looked up in when PATH misses it.
SYSTEM_DIRS = {r"C:\Windows\System32", "/usr/bin", "/usr/local/bin", "/usr/sbin",
               "/sbin"}

SHELL_METACHARACTERS = set(";|&$`<>(){}[]*?!~'\"\\\n\r\t ")


def test_the_command_table_is_exactly_the_reviewed_vocabulary():
    actual = {name: (cmd.program, cmd.args) for name, cmd in probe.COMMANDS.items()}
    assert actual == EXPECTED_COMMANDS


def test_every_program_is_on_the_explicit_allowlist():
    programs = {cmd.program for cmd in probe.COMMANDS.values()}
    assert programs == ALLOWED_PROGRAMS
    assert programs <= {"nvidia-smi", "system_profiler", "sysctl", "vm_stat",
                        "ioreg", "lspci"}
    for program in programs:
        # A bare name: resolved by ``shutil.which``, never a path someone chose.
        assert "/" not in program and "\\" not in program
        assert not set(program) & SHELL_METACHARACTERS


def test_no_argument_contains_a_shell_metacharacter_or_a_path():
    for name, cmd in probe.COMMANDS.items():
        assert isinstance(cmd.args, tuple), name
        for arg in cmd.args:
            assert isinstance(arg, str) and arg, (name, arg)
            assert not set(arg) & SHELL_METACHARACTERS, (name, arg)
            assert "/" not in arg, (name, arg)
            assert ".." not in arg, (name, arg)
            assert "%" not in arg and "{" not in arg, f"{name}: no templating"


def test_no_command_can_change_machine_state():
    """Read-only inventory: no write flag of any of these tools is present."""
    writing = {"-w=", "--set", "-e", "--gpu-reset", "-r=", "--persistence-mode",
               "-pm", "--power-limit", "-pl", "--compute-mode", "-c="}
    for name, cmd in probe.COMMANDS.items():
        if cmd.program == "sysctl":
            assert cmd.args[0] == "-n", "values only"
            assert all("=" not in a for a in cmd.args), "sysctl name=value would write"
            assert "-w" not in cmd.args
        if cmd.program == "nvidia-smi":
            assert all(a.startswith(("--query-", "--format=")) for a in cmd.args), name
        assert not set(cmd.args) & writing, name


def test_a_command_row_is_immutable():
    row = probe.COMMANDS["nvidia_static"]
    assert dataclasses.is_dataclass(row)
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.program = "sh"
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.args = ("-c", "id")
    with pytest.raises(TypeError):
        row.args[0] = "--evil"


def test_platforms_timeouts_and_fallback_directories_are_bounded():
    for name, cmd in probe.COMMANDS.items():
        assert cmd.platforms and set(cmd.platforms) <= {"win32", "linux", "darwin"}, name
        assert 0 < cmd.timeout_s <= probe.TIMEOUT_S <= 10, name
        assert set(cmd.fallback_dirs) <= SYSTEM_DIRS, name
    assert probe.FAST_TIMEOUT_S < probe.TIMEOUT_S
    assert probe.MAX_OUTPUT_BYTES == 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# run(): what reaches subprocess, and what never does
# ---------------------------------------------------------------------------

class _Recorder:
    """Stands in for ``subprocess.run`` inside the probe module."""

    def __init__(self, *, stdout: bytes = b"ok\n", returncode: int = 0,
                 raises: BaseException | None = None) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(args[0] if args else [], self.returncode,
                                           stdout=self.stdout, stderr=b"")


@pytest.fixture
def fake_host(monkeypatch):
    """Pretend every tool is installed on every platform; record what would run.

    Returns a function that installs a recorder for the wanted outcome and
    selects the platform the named command belongs to.
    """
    probe.reset_cache()
    monkeypatch.setattr(probe.shutil, "which", lambda program: f"/fake/bin/{program}")

    def install(name: str | None = None, **outcome) -> _Recorder:
        recorder = _Recorder(**outcome)
        monkeypatch.setattr(probe.subprocess, "run", recorder)
        platform = probe.COMMANDS[name].platforms[0] if name in probe.COMMANDS \
            else "linux"
        monkeypatch.setattr(probe, "_platform", lambda: platform)
        return recorder

    yield install
    probe.reset_cache()


@pytest.mark.parametrize("name", [
    "anything-else",
    "nvidia_static; rm -rf /",
    "nvidia_static ",
    " nvidia_static",
    "NVIDIA_STATIC",
    "nvidia_static\n",
    "nvidia-smi",
    "sysctl",
    "/usr/bin/id",
    "../nvidia_static",
    "sh -c id",
    "",
])
def test_a_name_that_is_not_in_the_table_starts_nothing(fake_host, name):
    recorder = fake_host()
    assert probe.run(name) is None
    assert probe.available(name) is False
    assert recorder.calls == [], "no process may be started for an unknown name"


@pytest.mark.parametrize("name", [None, 0, 3.5, ("nvidia_static",), b"nvidia_static"])
def test_a_non_string_name_starts_nothing(fake_host, name):
    recorder = fake_host()
    assert probe.run(name) is None
    assert recorder.calls == []


def test_a_command_for_another_platform_starts_nothing(fake_host, monkeypatch):
    recorder = fake_host("nvidia_static")
    monkeypatch.setattr(probe, "_platform", lambda: "darwin")
    assert probe.run("nvidia_static") is None
    assert probe.run("linux_lspci") is None
    assert probe.available("nvidia_static") is False
    monkeypatch.setattr(probe, "_platform", lambda: "linux")
    assert probe.run("mac_displays") is None
    assert recorder.calls == []


def test_a_missing_program_is_a_clean_none_and_starts_nothing(fake_host, monkeypatch):
    recorder = fake_host("linux_lspci")
    monkeypatch.setattr(probe.shutil, "which", lambda program: None)
    monkeypatch.setattr(probe.os.path, "isfile", lambda path: False)
    probe.reset_cache()
    assert probe.available("linux_lspci") is False
    assert probe.run("linux_lspci") is None
    assert recorder.calls == []


@pytest.mark.parametrize("name", sorted(EXPECTED_COMMANDS))
def test_every_command_is_started_as_a_list_argv_without_a_shell(fake_host, name):
    recorder = fake_host(name, stdout=b"some output\n")
    assert probe.run(name) == "some output\n"
    assert len(recorder.calls) == 1
    args, kwargs = recorder.calls[0]
    assert len(args) == 1, "argv is the only positional argument"
    argv = args[0]
    program, expected_args = EXPECTED_COMMANDS[name]
    assert isinstance(argv, list), "a list argv, never a command string"
    assert argv == [f"/fake/bin/{program}", *expected_args]
    assert all(isinstance(part, str) for part in argv)
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == probe.COMMANDS[name].timeout_s
    assert 0 < kwargs["timeout"] <= probe.TIMEOUT_S
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False
    assert "env" not in kwargs and "cwd" not in kwargs and "executable" not in kwargs


def test_a_timeout_is_none_not_an_exception(fake_host):
    recorder = fake_host("nvidia_live", raises=subprocess.TimeoutExpired(
        cmd=["nvidia-smi"], timeout=probe.FAST_TIMEOUT_S))
    assert probe.run("nvidia_live") is None
    assert len(recorder.calls) == 1


@pytest.mark.parametrize("error", [
    FileNotFoundError("gone"), PermissionError("denied"), OSError("exec format error")])
def test_a_program_that_cannot_start_is_none_not_an_exception(fake_host, error):
    recorder = fake_host("linux_lspci", raises=error)
    assert probe.run("linux_lspci") is None
    assert len(recorder.calls) == 1


@pytest.mark.parametrize("code", [1, 2, 9, 127, 255, -9])
def test_a_non_zero_exit_is_none_even_when_it_printed_something(fake_host, code):
    fake_host("nvidia_static", stdout=b"NVIDIA-SMI has failed\n", returncode=code)
    assert probe.run("nvidia_static") is None


def test_output_is_capped_at_the_byte_limit(fake_host):
    fake_host("mac_displays", stdout=b"x" * (probe.MAX_OUTPUT_BYTES + 4096))
    text = probe.run("mac_displays")
    assert len(text) == probe.MAX_OUTPUT_BYTES


def test_undecodable_output_is_replaced_not_raised(fake_host):
    fake_host("mac_swap", stdout=b"total = 1.00G \xff\xfe used = 0.00M")
    text = probe.run("mac_swap")
    assert "total = 1.00G" in text
    assert "\ufffd" in text


def test_empty_output_is_an_empty_string_not_none(fake_host):
    fake_host("mac_vm_stat", stdout=b"")
    assert probe.run("mac_vm_stat") == ""


def test_the_resolved_program_path_is_cached_and_can_be_forgotten(fake_host,
                                                                  monkeypatch):
    fake_host("linux_lspci")
    lookups: list[str] = []

    def which(program: str) -> str:
        lookups.append(program)
        return f"/fake/bin/{program}"

    monkeypatch.setattr(probe.shutil, "which", which)
    probe.reset_cache()
    probe.run("linux_lspci")
    probe.run("linux_lspci")
    assert lookups == ["lspci"]
    probe.reset_cache()
    probe.run("linux_lspci")
    assert lookups == ["lspci", "lspci"]


# ---------------------------------------------------------------------------
# Static: who may start a process, and with what
# ---------------------------------------------------------------------------

def _advisor_sources(app_dir):
    return sorted(p for p in (app_dir / "advisor").rglob("*.py")
                  if "__pycache__" not in p.parts)


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_probe_is_the_only_advisor_module_that_imports_subprocess(app_dir):
    users = []
    for path in _advisor_sources(app_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if "subprocess" in _imports(tree):
            users.append(path.name)
    assert users == ["probe.py"], users


def test_no_advisor_module_reaches_a_process_or_code_another_way(app_dir):
    banned_imports = {"pty", "pexpect", "multiprocessing", "runpy", "importlib",
                      "pickle", "marshal", "shelve"}
    #: Process-starting attributes of ``os`` (``platform.system()`` is not one).
    banned_os_calls = {"system", "popen", "execv", "execve", "execvp", "execl",
                       "spawnv", "spawnl", "spawnve", "startfile", "posix_spawn",
                       "fork"}
    banned_builtins = {"eval", "exec", "compile", "__import__"}
    offenders = []
    for path in _advisor_sources(app_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [(path.name, name) for name in _imports(tree) & banned_imports]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in banned_builtins:
                offenders.append((path.name, node.lineno, func.id))
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                    and func.value.id == "os" and func.attr in banned_os_calls:
                offenders.append((path.name, node.lineno, f"os.{func.attr}"))
            elif isinstance(func, ast.Attribute) and func.attr == "Popen":
                offenders.append((path.name, node.lineno, "Popen"))
    assert not offenders, offenders


def test_the_one_subprocess_call_is_run_with_shell_false(app_dir):
    path = app_dir / "advisor" / "probe.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name)
             and node.func.value.id == "subprocess"]
    assert len(calls) == 1, "exactly one place a process is started"
    (call,) = calls
    assert call.func.attr == "run"
    keywords = {kw.arg: kw.value for kw in call.keywords}
    assert isinstance(keywords["shell"], ast.Constant) and keywords["shell"].value is False
    assert ast.unparse(keywords["stdin"]) == "subprocess.DEVNULL"
    assert "timeout" in keywords
    # The argv is a list display built from the resolved program and the row's
    # constant arguments - not a string, and not a caller's value.
    assert isinstance(call.args[0], ast.List)
    assert ast.unparse(call.args[0]) == "[exe, *command.args]"


def test_every_caller_names_a_command_with_a_string_literal(app_dir):
    """No advisor code builds a probe name from data."""
    literal_names: set[str] = set()
    dynamic: list[tuple[str, int, str]] = []
    for path in _advisor_sources(app_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            is_probe_call = (isinstance(func, ast.Attribute)
                             and func.attr in ("run", "available")
                             and isinstance(func.value, ast.Name)
                             and func.value.id == "probe")
            is_wrapper = isinstance(func, ast.Name) and func.id == "attempt"
            if not (is_probe_call or is_wrapper):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                literal_names.add(arg.value)
            else:
                dynamic.append((path.name, node.lineno, ast.unparse(node)))
    # The single non-literal call is the timing wrapper in hardware._gpus, whose
    # own callers are all literals (collected above as ``attempt(...)``).
    assert [(name, call) for name, _line, call in dynamic] == [
        ("hardware.py", "probe.run(name)")], dynamic
    assert literal_names <= set(probe.COMMANDS), literal_names - set(probe.COMMANDS)
    assert literal_names == set(probe.COMMANDS), "every row is used; none is dead"


# ---------------------------------------------------------------------------
# The catalog host allowlist
# ---------------------------------------------------------------------------

CATALOG_HOSTS = {"huggingface.co", "civitai.com", "api.github.com",
                 "raw.githubusercontent.com", "openmodeldb.info", "ollama.com"}


def test_the_catalog_allowlist_is_six_exact_hosts_and_no_suffix_rule():
    assert set(hosts.CATALOG_HOSTS_EXACT) == CATALOG_HOSTS
    assert isinstance(hosts.CATALOG_HOSTS_EXACT, frozenset)
    assert hosts.CATALOG_HOSTS_SUFFIX == ()
    assert hosts.describe()["catalog_hosts"] == sorted(CATALOG_HOSTS)


@pytest.mark.parametrize("host", sorted(CATALOG_HOSTS))
def test_each_catalog_host_is_allowed_for_catalog_reads(host):
    assert hosts.host_allowed(host, hosts.KIND_CATALOG) is True
    assert hosts.host_allowed(host.upper() + ".", hosts.KIND_CATALOG) is True
    assert hosts.check(f"https://{host}/api/x", kind=hosts.KIND_CATALOG).host == host


@pytest.mark.parametrize("host", [
    "evil.huggingface.co", "cdn-lfs.huggingface.co", "cdn-lfs-us-1.huggingface.co",
    "huggingface.co.evil.test", "xhuggingface.co", "huggingface.com", "hf.co",
    "www.civitai.com", "image.civitai.com", "civitai.com.evil.test", "civitai.red",
    "github.com", "gist.github.com", "api.github.com.evil.test", "evil.api.github.com",
    "githubusercontent.com", "objects.githubusercontent.com",
    "gist.githubusercontent.com", "evil.raw.githubusercontent.com",
    "www.openmodeldb.info", "openmodeldb.info.evil.test",
    "registry.ollama.ai", "www.ollama.com", "ollama.com.evil.test",
    "gitlab.com", "codeberg.org",
    "evil.test", "localhost", "127.0.0.1", "169.254.169.254", "[::1]",
    "hugg\u0131ngface.co",          # a dotless-i homograph
    "", None,
])
def test_nothing_else_is_allowed_for_catalog_reads(host):
    assert hosts.host_allowed(host, hosts.KIND_CATALOG) is False


@pytest.mark.parametrize("url", [
    "http://huggingface.co/api/models",                    # not https
    "ftp://huggingface.co/api/models",
    "//huggingface.co/api/models",
    "huggingface.co/api/models",
    "https://huggingface.co@evil.test/api/models",         # userinfo trick
    "https://user:pw@huggingface.co/api/models",
    "https://evil.huggingface.co/api/models",
    "https://huggingface.co.evil.test/api/models",
    "https://evil.test/huggingface.co",
    "https://evil.test/?next=https://huggingface.co",
    "https://evil.test#@huggingface.co",
    "https://huggingface.co\\@evil.test/",
    "https://huggingface.co\n.evil.test/",
    "https://104.18.0.1/api/models",
    "file:///etc/passwd",
    "",
])
def test_check_refuses_every_url_that_is_not_plainly_a_catalog_host(url):
    with pytest.raises(hosts.HostNotAllowed):
        hosts.check(url, kind=hosts.KIND_CATALOG)


def test_the_catalog_list_did_not_widen_any_other_list():
    """Pinned literally: the model, git and release lists are what they were."""
    assert set(hosts.MODEL_HOSTS_EXACT) == {"civitai.com", "huggingface.co", "hf.co"}
    assert hosts.MODEL_HOSTS_SUFFIX == (".civitai.com", ".huggingface.co", ".hf.co")
    assert set(hosts.GIT_HOSTS_EXACT) == {"github.com", "gitlab.com", "codeberg.org"}
    assert hosts.GIT_HOSTS_SUFFIX == ()
    assert set(hosts.RELEASE_HOSTS_EXACT) == {
        "api.github.com", "github.com", "objects.githubusercontent.com",
        "release-assets.githubusercontent.com"}
    assert hosts.RELEASE_HOSTS_SUFFIX == (".githubusercontent.com",)
    assert {hosts.KIND_MODEL, hosts.KIND_GIT, hosts.KIND_RELEASE,
            hosts.KIND_CATALOG} == {"model", "git", "release", "catalog"}


@pytest.mark.parametrize("host", ["openmodeldb.info", "ollama.com", "api.github.com",
                                  "raw.githubusercontent.com"])
def test_a_catalog_only_host_cannot_serve_weights_or_a_git_remote(host):
    assert hosts.host_allowed(host, hosts.KIND_MODEL) is False
    assert hosts.host_allowed(host, hosts.KIND_GIT) is False
    assert hosts.host_allowed(host) is False, "the default kind is the model list"


@pytest.mark.parametrize("host", ["openmodeldb.info", "ollama.com", "huggingface.co",
                                  "civitai.com"])
def test_a_catalog_host_is_not_thereby_a_release_host(host):
    assert hosts.host_allowed(host, hosts.KIND_RELEASE) is False


def test_an_unknown_kind_falls_back_to_the_model_list_not_the_catalog_list():
    assert hosts.host_allowed("ollama.com", "catalogue") is False
    assert hosts.host_allowed("openmodeldb.info", "") is False


def test_a_redirect_from_a_catalog_host_is_checked_against_the_catalog_list():
    current = hosts.check("https://huggingface.co/api/models", kind=hosts.KIND_CATALOG)
    onward = hosts.check_redirect("https://civitai.com/api/v1/models",
                                  current=current, hop=0)
    assert onward.kind == hosts.KIND_CATALOG
    assert hosts.check_redirect("/api/models?x=1", current=current, hop=0).host == \
        "huggingface.co"
    for location in ("https://cdn-lfs.huggingface.co/blob",   # fine for weights only
                     "https://evil.test/", "//evil.test/x", "http://huggingface.co/x",
                     "", None):
        with pytest.raises(UpstreamUnavailable):
            hosts.check_redirect(location, current=current, hop=0)


# ---------------------------------------------------------------------------
# fetch_json: refused before a request exists
# ---------------------------------------------------------------------------

class _UntouchableClient:
    """Any use of this client is a test failure."""

    def __getattr__(self, name):
        raise AssertionError(f"the HTTP client was touched ({name}) for a refused URL")


class _Response:
    def __init__(self, status: int, *, headers: dict | None = None,
                 body: bytes = b"", chunk: int = 65536) -> None:
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._chunk = chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        for start in range(0, len(self._body), self._chunk):
            yield self._body[start:start + self._chunk]


class _ScriptedClient:
    """Answers ``stream("GET", url)`` from a queue and records every request."""

    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str]] = []
        self.closed = False

    def stream(self, method: str, url: str, **kwargs):
        assert not kwargs, f"no per-request headers, cookies or auth: {kwargs}"
        self.requests.append((method, url))
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("url", [
    "https://evil.test/api/models",
    "https://evil.huggingface.co/api/models",
    "https://huggingface.co.evil.test/api/models",
    "https://huggingface.co@evil.test/api/models",
    "https://cdn-lfs.huggingface.co/some/blob",
    "https://github.com/owner/repo",
    "https://127.0.0.1:8188/object_info",
    "https://localhost/x",
    "http://huggingface.co/api/models",
    "http://civitai.com/api/v1/models",
    "file:///etc/passwd",
    "",
    None,
])
def test_fetch_json_refuses_a_url_off_the_allowlist_before_any_request(url):
    with pytest.raises(hosts.HostNotAllowed):
        sources.fetch_json(url, client=_UntouchableClient())


def test_fetch_json_returns_parsed_json_from_an_allowlisted_host():
    client = _ScriptedClient(_Response(200, body=b'{"models": [1, 2, 3]}', chunk=5))
    status, doc = sources.fetch_json("https://ollama.com/api/tags", client=client)
    assert (status, doc) == (200, {"models": [1, 2, 3]})
    assert client.requests == [("GET", "https://ollama.com/api/tags")]
    assert client.closed is False, "a caller's client is the caller's to close"


def test_fetch_json_refuses_a_redirect_that_leaves_the_allowlist():
    client = _ScriptedClient(_Response(302, headers={"location": "https://evil.test/x"}))
    with pytest.raises(UpstreamUnavailable) as refused:
        sources.fetch_json("https://huggingface.co/api/models", client=client)
    assert client.requests == [("GET", "https://huggingface.co/api/models")], \
        "the redirect target was never requested"
    assert refused.value.details["from_host"] == "huggingface.co"


@pytest.mark.parametrize("location", [
    "https://cdn-lfs.huggingface.co/blob",       # a model CDN is not a catalog host
    "http://huggingface.co/api/models",          # downgrade
    "//evil.test/x",
    "https://huggingface.co@evil.test/",
    "https://169.254.169.254/latest/meta-data",
    "file:///etc/passwd",
    "",
])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_fetch_json_never_follows_a_redirect_off_the_list(status, location):
    client = _ScriptedClient(_Response(status, headers={"location": location}))
    with pytest.raises(UpstreamUnavailable):
        sources.fetch_json("https://civitai.com/api/v1/models", client=client)
    assert len(client.requests) == 1


def test_fetch_json_follows_a_redirect_that_stays_on_the_list_and_rechecks_it():
    client = _ScriptedClient(
        _Response(301, headers={"location": "/api/v2/models"}),
        _Response(307, headers={"location": "https://raw.githubusercontent.com/a/b.json"}),
        _Response(200, body=b"[]"))
    status, doc = sources.fetch_json("https://openmodeldb.info/api/v1/models.json",
                                     client=client)
    assert (status, doc) == (200, [])
    assert [url for _method, url in client.requests] == [
        "https://openmodeldb.info/api/v1/models.json",
        "https://openmodeldb.info/api/v2/models",
        "https://raw.githubusercontent.com/a/b.json"]


def test_fetch_json_gives_up_on_a_redirect_loop():
    loop = [_Response(302, headers={"location": "/again"}) for _ in range(20)]
    client = _ScriptedClient(*loop)
    with pytest.raises(UpstreamUnavailable):
        sources.fetch_json("https://ollama.com/api/tags", client=client)
    assert len(client.requests) <= hosts.MAX_REDIRECTS + 1


def test_fetch_json_refuses_an_oversized_body_without_buffering_all_of_it():
    body = b"[" + b"0," * 4000 + b"0]"
    client = _ScriptedClient(_Response(200, body=body, chunk=512))
    with pytest.raises(UpstreamUnavailable):
        sources.fetch_json("https://ollama.com/api/tags", client=client, max_bytes=2048)
    assert sources.MAX_JSON_BYTES == 12 * 1024 * 1024


@pytest.mark.parametrize("body", [b"<html>Just a moment...</html>", b"", b"{not json",
                                  b"\xff\xfe\x00"])
def test_fetch_json_treats_a_non_json_body_as_an_upstream_failure(body):
    client = _ScriptedClient(_Response(200, body=body))
    with pytest.raises(UpstreamUnavailable):
        sources.fetch_json("https://civitai.com/api/v1/models", client=client)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_fetch_json_raises_on_an_error_status_unless_the_caller_accepts_it(status):
    client = _ScriptedClient(_Response(status, body=b'{"error": "x"}'))
    with pytest.raises(UpstreamUnavailable) as failure:
        sources.fetch_json("https://huggingface.co/api/models/a/b", client=client)
    assert failure.value.details == {"host": "huggingface.co", "status": status}
    accepted = _ScriptedClient(_Response(status, body=b'{"error": "x"}'))
    assert sources.fetch_json("https://huggingface.co/api/models/a/b", client=accepted,
                              accept_statuses=(status,)) == (status, None)


def test_a_transport_error_becomes_an_upstream_failure_naming_only_the_host():
    import httpx

    class _Broken(_ScriptedClient):
        def stream(self, method, url, **kwargs):
            raise httpx.ConnectError("dns failure for a very private reason")

    with pytest.raises(UpstreamUnavailable) as failure:
        sources.fetch_json("https://ollama.com/api/tags", client=_Broken())
    assert failure.value.details == {"host": "ollama.com", "error": "ConnectError"}
    assert "private reason" not in failure.value.message


def test_fetched_json_is_data_even_when_it_looks_like_code():
    """A hostile document is parsed, returned, and nothing else happens."""
    body = (b'{"__class__": "os.system", "__reduce__": ["os.system", ["id"]],'
            b' "siblings": "__import__(\'os\').system(\'id\')"}')
    client = _ScriptedClient(_Response(200, body=body))
    _status, doc = sources.fetch_json("https://huggingface.co/api/models/a/b",
                                      client=client)
    assert isinstance(doc, dict)
    assert doc["__reduce__"] == ["os.system", ["id"]]
    assert sources.normalize_hf(doc) is None, "and it is not a catalog entry"


# ---------------------------------------------------------------------------
# head_status: the link check asks the model allowlist, and follows nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://evil.test/model.safetensors",
    "https://openmodeldb.info/x.pth",
    "https://ollama.com/library/demo",
    "http://huggingface.co/a/b/resolve/main/x.safetensors",
    "https://huggingface.co@evil.test/x",
    "",
])
def test_head_status_does_not_request_a_url_off_the_model_allowlist(url):
    result = sources.head_status(url, client=_UntouchableClient())
    assert result["state"] == "unchecked"
    assert result["status"] is None


class _HeadClient:
    def __init__(self, status: int, headers: dict | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self.requests: list[str] = []

    def head(self, url: str, **kwargs):
        assert not kwargs
        self.requests.append(url)
        return _Response(self.status, headers=self.headers)


@pytest.mark.parametrize("status,state", [
    (200, "ok"), (302, "ok"), (307, "ok"), (401, "gated"), (403, "gated"),
    (404, "dead"), (429, "unreachable"), (500, "unreachable"),
])
def test_head_status_maps_the_answer_and_never_follows_the_redirect(status, state):
    url = "https://huggingface.co/a/b/resolve/main/x.safetensors"
    client = _HeadClient(status, {"location": "https://evil.test/elsewhere",
                                  "x-linked-size": "12345"})
    result = sources.head_status(url, client=client)
    assert result["state"] == state
    assert result["status"] == status
    assert client.requests == [url], "one HEAD, and the Location is not requested"
    if state == "ok":
        assert result["bytes"] == 12345
