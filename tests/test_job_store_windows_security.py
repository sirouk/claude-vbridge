"""Native Windows storage negatives on synthetic temporary state only.

No desktop APIs, network, account files, or inference calls. Windows helpers
are imported inside guarded fixtures/tests. These tests need a real Windows
runner; collection on other platforms must only produce skips.
"""

import base64
import os
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows storage security checks")


@pytest.fixture
def security():
    from vbridge.job_store import _WindowsSecurity

    return _WindowsSecurity()


def security_snapshot(security, path, directory=False):
    """Read owner, control and raw DACL without repairing the synthetic object."""
    c, w = security.c, security.w
    flags = 0x00200000 | (0x02000000 if directory else 0)
    handle = security.k.CreateFileW(str(path), 0x20080, 3, None, 3, flags, None)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    owner, dacl, descriptor = c.c_void_p(), c.c_void_p(), c.c_void_p()
    try:
        error = security.a.GetSecurityInfo(
            handle, 1, 5, c.byref(owner), None, c.byref(dacl), None, c.byref(descriptor)
        )
        if error:
            raise c.WinError(error)
        sid_text = c.c_void_p()
        security._ok(security.a.ConvertSidToStringSidW(owner, c.byref(sid_text)))
        try:
            owner_text = c.wstring_at(sid_text)
        finally:
            security.k.LocalFree(sid_text)
        control, revision = c.c_ushort(), w.DWORD()
        security._ok(security.a.GetSecurityDescriptorControl(descriptor, c.byref(control), c.byref(revision)))
        header = c.string_at(dacl, 8)
        acl = c.string_at(dacl, int.from_bytes(header[2:4], "little"))
        return owner_text, control.value, acl
    finally:
        if descriptor.value:
            security.k.LocalFree(descriptor)
        security.close(handle)


def set_synthetic_dacl(security, path, sddl, protected=True):
    c, w = security.c, security.w
    descriptor, dacl = c.c_void_p(), c.c_void_p()
    present, defaulted = w.BOOL(), w.BOOL()
    security._ok(
        security.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None)
    )
    try:
        security._ok(
            security.a.GetSecurityDescriptorDacl(
                descriptor, c.byref(present), c.byref(dacl), c.byref(defaulted)
            )
        )
        # Open without repair: tamper only a test-owned file, preserving its owner.
        handle = security.k.CreateFileW(str(path), 0x60000, 3, None, 3, 0x00200000, None)
        if handle == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            flags = 0x80000004 if protected else 0x20000004
            error = security.a.SetSecurityInfo(handle, 1, flags, None, None, dacl, None)
            if error:
                raise c.WinError(error)
        finally:
            security.close(handle)
    finally:
        security.k.LocalFree(descriptor)


@pytest.mark.parametrize("tamper", ["extra_ace", "unprotected"])
def test_tampered_dacl_refused_without_mutation_then_owned_file_repaired(tmp_path, security, tamper):
    from vbridge.job_store import check_private_file, private_write, secure_file

    path = tmp_path / "state" / "synthetic.txt"
    private_write(path, b"synthetic contents")
    suffix = "(A;;FR;;;WD)" if tamper == "extra_ace" else ""
    set_synthetic_dacl(
        security, path, f"D:P(A;;FA;;;{security.sid_text}){suffix}", protected=tamper != "unprotected"
    )
    before = security_snapshot(security, path)
    assert (before[1] & 0x1000) == (0 if tamper == "unprotected" else 0x1000)
    with pytest.raises(OSError, match="DACL"):
        check_private_file(path)
    assert security_snapshot(security, path) == before
    assert path.read_bytes() == b"synthetic contents"
    secure_file(path)
    check_private_file(path)
    assert path.read_bytes() == b"synthetic contents"


def test_foreign_owner_refused_before_dacl_repair(tmp_path, security):
    from vbridge.job_store import check_private_file, ensure_private_dir, secure_file

    parent = ensure_private_dir(tmp_path / "state")
    path = parent / "foreign-owner.txt"
    c = security.c
    descriptor = c.c_void_p()
    # Builtin Administrators is a synthetic foreign owner, not an inspected user.
    security._ok(
        security.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"O:BAD:P(A;;FA;;;{security.sid_text})", 1, c.byref(descriptor), None
        )
    )
    try:
        attributes = security.SA(c.sizeof(security.SA), descriptor, False)
        handle = security.k.CreateFileW(str(path), 0xC0060000, 3, c.byref(attributes), 1, 0, None)
        if handle == c.c_void_p(-1).value:
            error = c.get_last_error()
            if error in (5, 1307, 1314):
                pytest.skip(f"Token cannot create synthetic foreign-owned object: winerror {error}")
            raise c.WinError(error)
        security.close(handle)
    finally:
        security.k.LocalFree(descriptor)
    before = security_snapshot(security, path)
    assert before[0] != security.sid_text
    for operation in (check_private_file, secure_file):
        with pytest.raises(OSError, match="owned by another user"):
            operation(path)
        assert security_snapshot(security, path) == before
    assert path.read_bytes() == b""


def test_junction_boundary_and_ancestor_refused_without_target_mutation(tmp_path, security):
    from vbridge.job_store import JobStore, ensure_private_dir, private_write

    target = ensure_private_dir(tmp_path / "target")
    sentinel = target / "sentinel.txt"
    private_write(sentinel, b"unchanged synthetic target")
    before_dir = security_snapshot(security, target, directory=True)
    before_file = security_snapshot(security, sentinel)
    link = tmp_path / "junction"

    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    script = (
        "$ErrorActionPreference='Stop'; "
        f"New-Item -ItemType Junction -Path {quote(link)} -Target {quote(target)} | Out-Null"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, "Synthetic junction fixture creation failed"
    for path in (link, link / "child"):
        with pytest.raises(OSError, match="reparse"):
            ensure_private_dir(path)
        with pytest.raises(OSError, match="reparse"):
            JobStore(path)
    assert security_snapshot(security, target, directory=True) == before_dir
    assert security_snapshot(security, sentinel) == before_file
    assert sentinel.read_bytes() == b"unchanged synthetic target"
    assert set(p.name for p in target.iterdir()) == {"sentinel.txt"}


def test_hardlinked_record_refused_without_external_target_mutation(tmp_path, security):
    from vbridge.job_store import JobStore, private_write

    store = JobStore(tmp_path / "state")
    external = store.home / "external.txt"
    private_write(external, b"unchanged synthetic target")
    record_id = str(uuid.uuid4())
    record = store.base / f"{record_id}.json"
    before = security_snapshot(security, external)
    os.link(external, record)
    try:
        with pytest.raises(OSError, match="unlinked regular"):
            list(store.records())
        with pytest.raises(OSError, match="unlinked regular"):
            store.write({"id": record_id})
        assert external.read_bytes() == b"unchanged synthetic target"
        assert external.stat().st_nlink == 2
        assert security_snapshot(security, external) == before
    finally:
        store.close()


def test_killed_store_owner_releases_exclusive_lock(tmp_path):
    from vbridge.job_store import JobStore

    home, marker = tmp_path / "state", tmp_path / "ready"
    code = (
        "import pathlib,sys,time; from vbridge.job_store import JobStore; "
        "store=JobStore(pathlib.Path(sys.argv[1])); "
        "pathlib.Path(sys.argv[2]).write_text('ready'); time.sleep(60)"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", code, str(home), str(marker)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "Synthetic store owner did not initialize"
        with pytest.raises(RuntimeError, match="already has an owner"):
            JobStore(home)
        owner.kill()
        owner.wait(timeout=5)
        replacement = JobStore(home)
        try:
            assert list(replacement.records()) == []
        finally:
            replacement.close()
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
