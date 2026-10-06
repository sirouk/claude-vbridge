"""Private, atomic local job records; no execution or network access.

POSIX operations use pinned directory descriptors and O_NOFOLLOW. Windows uses
protected owner-only DACLs, rejects reparse points, and holds directory handles
without delete sharing. Failure to establish or verify privacy is fatal. Locks
are OS-owned and are released on close or process exit, not by deleting files.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Iterator
from pathlib import Path

_MAX_RECORD = 1024 * 1024


def _valid_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid job id")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ValueError("invalid job id") from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("invalid job id")
    return value


def _path(path: Path | str) -> Path:
    requested = Path(path).expanduser().absolute()
    # Allow system aliases such as macOS /var, but never resolve the boundary.
    if os.name == "posix":
        return requested.parent.resolve() / requested.name
    return requested


def _posix_check(fd: int, directory: bool = False, repair: bool = False) -> None:
    info = os.fstat(fd)
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or info.st_uid != os.getuid() or (not directory and info.st_nlink != 1):
        raise OSError("private storage requires an owned, unlinked regular file or directory")
    mode = 0o700 if directory else 0o600
    if repair:
        os.fchmod(fd, mode)
    if stat.S_IMODE(os.fstat(fd).st_mode) != mode:
        raise OSError("private storage permissions could not be verified")


def _posix_dir(path: Path) -> int:
    if path.is_symlink():
        raise ValueError("private state directory must not be a symlink")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _posix_check(fd, directory=True, repair=True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _posix_existing(dir_fd: int, name: str) -> None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    try:
        _posix_check(fd, repair=True)
    finally:
        os.close(fd)


def _posix_write(dir_fd: int, name: str, data: bytes) -> None:
    _posix_existing(dir_fd, name)
    temporary = f".{uuid.uuid4()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    # Failed writes intentionally retain their private temp file for diagnosis.
    with os.fdopen(fd, "wb") as stream:
        _posix_check(stream.fileno(), repair=True)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _posix_existing(dir_fd, name)
    os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.fsync(dir_fd)


class _WindowsSecurity:
    """Small ctypes boundary. No Windows chmod or permissive ACL fallback."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as w

        self.c = ctypes
        self.w = w
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.a = ctypes.WinDLL("advapi32", use_last_error=True)
        P = ctypes.c_void_p
        D = w.DWORD
        B = w.BOOL
        H = w.HANDLE
        self._bind(self.k, "GetCurrentProcess", [], H)
        self._bind(self.k, "CloseHandle", [H], B)
        self._bind(self.k, "LocalFree", [P], P)
        self._bind(self.k, "CreateDirectoryW", [w.LPCWSTR, P], B)
        self._bind(self.k, "CreateFileW", [w.LPCWSTR, D, D, P, D, D, H], H)
        self._bind(self.k, "GetFileInformationByHandle", [H, P], B)
        self._bind(self.k, "FlushFileBuffers", [H], B)
        self._bind(self.k, "ReadFile", [H, P, D, P, P], B)
        self._bind(self.k, "WriteFile", [H, P, D, P, P], B)
        self._bind(self.k, "MoveFileExW", [w.LPCWSTR, w.LPCWSTR, D], B)
        self._bind(self.a, "OpenProcessToken", [H, D, P], B)
        self._bind(self.a, "GetTokenInformation", [H, ctypes.c_int, P, D, P], B)
        self._bind(self.a, "ConvertSidToStringSidW", [P, P], B)
        self._bind(self.a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [w.LPCWSTR, D, P, P], B)
        self._bind(self.a, "GetSecurityInfo", [H, ctypes.c_int, D, P, P, P, P, P], D)
        self._bind(self.a, "SetSecurityInfo", [H, ctypes.c_int, D, P, P, P, P], D)
        self._bind(self.a, "GetSecurityDescriptorDacl", [P, P, P, P], B)
        self._bind(self.a, "GetSecurityDescriptorControl", [P, P, P], B)
        self._bind(self.a, "EqualSid", [P, P], B)
        self._bind(self.a, "GetAclInformation", [P, P, D, ctypes.c_int], B)
        self._bind(self.a, "GetAce", [P, D, P], B)

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("length", D), ("descriptor", P), ("inherit", B)]

        class FileInformation(ctypes.Structure):
            _fields_ = [
                ("attributes", D),
                ("creation", w.FILETIME),
                ("access", w.FILETIME),
                ("write", w.FILETIME),
                ("volume", D),
                ("size_high", D),
                ("size_low", D),
                ("links", D),
                ("index_high", D),
                ("index_low", D),
            ]

        self.SA = SecurityAttributes
        self.FI = FileInformation
        token = H()
        self._ok(self.a.OpenProcessToken(self.k.GetCurrentProcess(), 0x0008, ctypes.byref(token)))
        try:
            size = D()
            self.a.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not size.value:
                self._ok(False)
            self._token = ctypes.create_string_buffer(size.value)
            self._ok(self.a.GetTokenInformation(token, 1, self._token, size, ctypes.byref(size)))
            self.sid = ctypes.cast(self._token, ctypes.POINTER(P)).contents.value
            sid_text = P()
            self._ok(self.a.ConvertSidToStringSidW(self.sid, ctypes.byref(sid_text)))
            try:
                self.sid_text = ctypes.wstring_at(sid_text)
            finally:
                self.k.LocalFree(sid_text)
        finally:
            self.close(token)

    @staticmethod
    def _bind(library, name, arguments, result) -> None:
        function = getattr(library, name)
        function.argtypes = arguments
        function.restype = result

    def _ok(self, result) -> None:
        if not result:
            raise self.c.WinError(self.c.get_last_error())

    def close(self, handle) -> None:
        self._ok(self.k.CloseHandle(handle))

    def _descriptor(self, directory: bool):
        descriptor = self.c.c_void_p()
        flags = "OICI" if directory else ""
        sddl = f"O:{self.sid_text}D:P(A;{flags};FA;;;{self.sid_text})"
        self._ok(
            self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, self.c.byref(descriptor), None
            )
        )
        return descriptor

    def info(self, handle, directory: bool = False):
        info = self.FI()
        self._ok(self.k.GetFileInformationByHandle(handle, self.c.byref(info)))
        if info.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise OSError("private storage must not contain reparse points")
        if bool(info.attributes & 0x10) != directory or (not directory and info.links != 1):
            raise OSError("private storage requires an unlinked regular file or directory")
        return info

    def verify(self, handle, directory: bool, repair: bool = False) -> None:
        self.info(handle, directory)
        c = self.c
        owner, dacl, descriptor = c.c_void_p(), c.c_void_p(), c.c_void_p()
        error = self.a.GetSecurityInfo(
            handle, 1, 0x5, c.byref(owner), None, c.byref(dacl), None, c.byref(descriptor)
        )
        if error:
            raise c.WinError(error)
        try:
            if not owner.value or not self.a.EqualSid(owner, self.sid):
                raise OSError("private storage is owned by another user")
            if repair:
                secure = self._descriptor(directory)
                try:
                    present, defaulted, new_dacl = self.w.BOOL(), self.w.BOOL(), c.c_void_p()
                    self._ok(
                        self.a.GetSecurityDescriptorDacl(
                            secure, c.byref(present), c.byref(new_dacl), c.byref(defaulted)
                        )
                    )
                    error = self.a.SetSecurityInfo(handle, 1, 0x80000004, None, None, new_dacl, None)
                    if error:
                        raise c.WinError(error)
                finally:
                    self.k.LocalFree(secure)
                self.verify(handle, directory)
                return
            control, revision = c.c_ushort(), self.w.DWORD()
            self._ok(self.a.GetSecurityDescriptorControl(descriptor, c.byref(control), c.byref(revision)))
            if not control.value & 0x1000 or not dacl.value:  # SE_DACL_PROTECTED
                raise OSError("private storage DACL is not protected")
            acl_info = (self.w.DWORD * 3)()
            self._ok(self.a.GetAclInformation(dacl, acl_info, c.sizeof(acl_info), 2))
            if acl_info[0] != 1:
                raise OSError("private storage DACL is not owner-only")
            ace = c.c_void_p()
            self._ok(self.a.GetAce(dacl, 0, c.byref(ace)))
            header = c.string_at(ace, 8)
            expected_flags = 3 if directory else 0
            if (
                header[0] != 0
                or header[1] != expected_flags
                or int.from_bytes(header[4:8], "little") != 0x1F01FF
                or not self.a.EqualSid(ace.value + 8, self.sid)
            ):
                raise OSError("private storage DACL is not owner-only")
        finally:
            self.k.LocalFree(descriptor)

    def open(
        self,
        path: Path,
        directory: bool = False,
        create: bool = False,
        exclusive: bool = False,
        writable: bool = False,
        repair: bool = False,
    ):
        descriptor = self._descriptor(directory) if create else None
        try:
            attributes = self.SA(self.c.sizeof(self.SA), descriptor, False) if create else None
            access = 0x20000 | 0x80  # READ_CONTROL | FILE_READ_ATTRIBUTES
            if repair:
                access |= 0x40000  # WRITE_DAC
            if not directory:
                access |= 0x80000000  # GENERIC_READ
            if writable:
                access |= 0x40000000  # GENERIC_WRITE
            flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT / BACKUP_SEMANTICS
            handle = self.k.CreateFileW(
                str(path),
                access,
                0 if exclusive else 3,
                self.c.byref(attributes) if attributes else None,
                1 if create else 3,
                flags,
                None,
            )
            if handle == self.c.c_void_p(-1).value:
                raise self.c.WinError(self.c.get_last_error())
        finally:
            if descriptor:
                self.k.LocalFree(descriptor)
        try:
            self.info(handle, directory)
            if repair or create:
                self.verify(handle, directory, repair=repair)
            return handle
        except BaseException:
            self.close(handle)
            raise

    def directories(self, path: Path, create: bool = True):
        # Pin every ancestor without FILE_SHARE_DELETE. Refuse junctions and all
        # other reparses rather than trusting path-based ACL or rename checks.
        handles = []
        try:
            ancestors = list(reversed(path.parents)) + [path]
            for directory in ancestors:
                target = directory == path
                created = False
                if create and (target or directory != ancestors[0]):
                    descriptor = self._descriptor(True)
                    try:
                        attributes = self.SA(self.c.sizeof(self.SA), descriptor, False)
                        created = bool(self.k.CreateDirectoryW(str(directory), self.c.byref(attributes)))
                        if not created:
                            error = self.c.get_last_error()
                            if error != 183:
                                raise self.c.WinError(error)
                    finally:
                        self.k.LocalFree(descriptor)
                handle = self.open(directory, directory=True, repair=target and create)
                handles.append(handle)
                if created or (target and not create):
                    self.verify(handle, True)
            return handles
        except BaseException:
            for handle in reversed(handles):
                self.close(handle)
            raise

    def write(self, path: Path, data: bytes) -> None:
        self.existing(path)
        temporary = path.parent / f".{uuid.uuid4()}.tmp"
        handle = self.open(temporary, create=True, exclusive=True, writable=True)
        try:
            offset = 0
            while offset < len(data):
                block = data[offset : offset + 65536]
                written = self.w.DWORD()
                self._ok(self.k.WriteFile(handle, block, len(block), self.c.byref(written), None))
                if not written.value:
                    raise OSError("private file write made no progress")
                offset += written.value
            self._ok(self.k.FlushFileBuffers(handle))
            self.verify(handle, False)
        finally:
            self.close(handle)
        self.existing(path)
        self._ok(self.k.MoveFileExW(str(temporary), str(path), 0x9))

    def existing(self, path: Path) -> None:
        try:
            handle = self.open(path, repair=True)
        except OSError as error:
            if getattr(error, "winerror", None) == 2:
                return
            raise
        self.close(handle)

    def read(self, path: Path) -> bytes:
        handle = self.open(path, repair=True)
        try:
            info = self.info(handle)
            if (info.size_high << 32 | info.size_low) > _MAX_RECORD:
                raise ValueError("invalid job record: file is too large")
            chunks, total = [], 0
            while total <= _MAX_RECORD:
                block, count = self.c.create_string_buffer(65536), self.w.DWORD()
                self._ok(self.k.ReadFile(handle, block, len(block), self.c.byref(count), None))
                if not count.value:
                    break
                chunks.append(block.raw[: count.value])
                total += count.value
            if total > _MAX_RECORD:
                raise ValueError("invalid job record: file is too large")
            return b"".join(chunks)
        finally:
            self.close(handle)


def ensure_private_dir(path: Path | str) -> Path:
    """Create/tighten an owned private directory; reject links and foreign owners."""
    path = _path(path)
    if os.name == "posix":
        os.close(_posix_dir(path))
    elif os.name == "nt":
        security = _WindowsSecurity()
        handles = security.directories(path)
        for handle in reversed(handles):
            security.close(handle)
    else:
        raise RuntimeError("private storage is unsupported on this platform")
    return path


def private_write(path: Path | str, text_or_bytes: str | bytes) -> None:
    """Atomically replace an owned private file; never follow links."""
    requested = Path(path).expanduser().absolute()
    path = _path(requested.parent) / requested.name
    data = text_or_bytes.encode("utf-8") if isinstance(text_or_bytes, str) else text_or_bytes
    if not isinstance(data, bytes):
        raise TypeError("private_write requires str or bytes")
    if os.name == "posix":
        fd = _posix_dir(path.parent)
        try:
            _posix_write(fd, path.name, data)
        finally:
            os.close(fd)
    elif os.name == "nt":
        security = _WindowsSecurity()
        handles = security.directories(path.parent)
        try:
            security.write(path, data)
        finally:
            for handle in reversed(handles):
                security.close(handle)
    else:
        raise RuntimeError("private storage is unsupported on this platform")


def check_private_file(path: Path | str) -> None:
    """Verify a private, owned regular file without silently fixing its security."""
    requested = Path(path).expanduser().absolute()
    path = _path(requested.parent) / requested.name
    if os.name == "posix":
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _posix_check(parent, directory=True)
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                _posix_check(fd)
            finally:
                os.close(fd)
        finally:
            os.close(parent)
    elif os.name == "nt":
        security = _WindowsSecurity()
        handles = security.directories(path.parent, create=False)
        try:
            handle = security.open(path)
            try:
                security.verify(handle, False)
            finally:
                security.close(handle)
        finally:
            for handle in reversed(handles):
                security.close(handle)
    else:
        raise RuntimeError("private storage is unsupported on this platform")


def secure_directory(path: Path | str) -> Path:
    """Alias for ensure_private_dir, for credentials and settings callers."""
    return ensure_private_dir(path)


def secure_file(path: Path | str) -> None:
    """Tighten and verify an existing owned regular file, without following links."""
    requested = Path(path).expanduser().absolute()
    path = _path(requested.parent) / requested.name
    if os.name == "posix":
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                _posix_check(fd, repair=True)
            finally:
                os.close(fd)
        finally:
            os.close(parent)
    elif os.name == "nt":
        security = _WindowsSecurity()
        handles = security.directories(path.parent, create=False)
        try:
            handle = security.open(path, repair=True)
            security.close(handle)
        finally:
            for handle in reversed(handles):
                security.close(handle)
    else:
        raise RuntimeError("private storage is unsupported on this platform")


class JobStore:
    """One exclusive local jobs store. Call close() at application shutdown."""

    def __init__(self, home: Path | str):
        self.home = _path(home)
        self.base = self.home / "jobs"
        self._dir_fd = self._lock_fd = -1
        self._handles = []
        self._lock_handle = None
        self._security = None
        self._closed = False
        try:
            if os.name == "posix":
                import fcntl

                home_fd = _posix_dir(self.home)
                try:
                    try:
                        os.mkdir("jobs", 0o700, dir_fd=home_fd)
                    except FileExistsError:
                        pass
                    self._dir_fd = os.open(
                        "jobs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home_fd
                    )
                    _posix_check(self._dir_fd, directory=True, repair=True)
                finally:
                    os.close(home_fd)
                self._lock_fd = os.open(
                    ".lock",
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=self._dir_fd,
                )
                _posix_check(self._lock_fd, repair=True)
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError("jobs store already has an owner") from None
            elif os.name == "nt":
                self._security = _WindowsSecurity()
                self._handles.extend(self._security.directories(self.home))
                self._handles.extend(self._security.directories(self.base))
                lock = self.base / ".lock"
                try:
                    try:
                        self._lock_handle = self._security.open(
                            lock, create=True, exclusive=True, writable=True
                        )
                    except OSError as error:
                        if getattr(error, "winerror", None) not in (80, 183):
                            raise
                        self._lock_handle = self._security.open(
                            lock, exclusive=True, writable=True, repair=True
                        )
                except OSError as error:
                    if getattr(error, "winerror", None) in (32, 33):
                        raise RuntimeError("jobs store already has an owner") from None
                    raise
            else:
                raise RuntimeError("private storage is unsupported on this platform")
        except BaseException:
            self.close()
            raise

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("jobs store is closed")

    def write(self, record: dict) -> None:
        """Durably commit one bounded JSON object identified by a canonical UUID4."""
        self._check_open()
        if not isinstance(record, dict):
            raise ValueError("invalid job record")
        name = f"{_valid_id(record.get('id'))}.json"
        data = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if len(data) > _MAX_RECORD:
            raise ValueError("invalid job record: file is too large")
        if self._security is None:
            _posix_write(self._dir_fd, name, data)
        else:
            self._security.write(self.base / name, data)

    def records(self) -> Iterator[dict]:
        """Yield validated records. Invalid names are ignored; unsafe files fail closed."""
        self._check_open()
        names = os.listdir(self._dir_fd if self._security is None else self.base)
        for name in names:
            self._check_open()
            if not name.endswith(".json"):
                continue
            try:
                job_id = _valid_id(name[:-5])
            except ValueError:
                continue
            if self._security is None:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._dir_fd)
                with os.fdopen(fd, "rb") as stream:
                    _posix_check(stream.fileno(), repair=True)
                    if os.fstat(stream.fileno()).st_size > _MAX_RECORD:
                        raise ValueError("invalid job record: file is too large")
                    data = stream.read(_MAX_RECORD + 1)
            else:
                data = self._security.read(self.base / name)
            if len(data) > _MAX_RECORD:
                raise ValueError("invalid job record: file is too large")
            record = json.loads(data)
            if not isinstance(record, dict) or record.get("id") != job_id:
                raise ValueError("invalid job record")
            yield record

    def close(self) -> None:
        """Release exclusive ownership. Idempotent; never delete lock files."""
        self._closed = True
        if self._lock_fd >= 0:
            os.close(self._lock_fd)
            self._lock_fd = -1
        if self._dir_fd >= 0:
            os.close(self._dir_fd)
            self._dir_fd = -1
        if self._security is not None:
            if self._lock_handle is not None:
                self._security.close(self._lock_handle)
                self._lock_handle = None
            for handle in reversed(self._handles):
                self._security.close(handle)
            self._handles.clear()
