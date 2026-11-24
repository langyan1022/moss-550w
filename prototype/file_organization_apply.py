from contextlib import ExitStack, contextmanager
import ctypes as c
from ctypes import wintypes as w
from pathlib import Path, PureWindowsPath


READ_ATTRIBUTES = 0x80
TRAVERSE = 0x20
DELETE = 0x10000
SYNCHRONIZE = 0x100000
SHARE_READ = 1
SHARE_WRITE = 2
SHARE_DELETE = 4
DIRECTORY = 0x10
REPARSE_POINT = 0x400
OPEN_EXISTING = 3
BACKUP_SEMANTICS = 0x02000000
OPEN_REPARSE_POINT = 0x00200000
NT_OPEN = 1
NT_CREATE = 2
NT_DIRECTORY = 1
NT_SYNCHRONOUS = 0x20
CASE_INSENSITIVE = 0x40
DRIVE_FIXED = 3
FILE_BASIC_INFO = 0
FILE_STANDARD_INFO = 1
FILE_ID_INFO = 18
NT_FILE_RENAME_INFORMATION = 10
INVALID_HANDLE = c.c_void_p(-1).value
CATEGORIES = {"文档", "图片", "音频", "视频", "压缩包"}
RETRY_PREVIEW = "文件或分类目录已变化，请重新查看整理预览。"


class _UnicodeString(c.Structure):
    _fields_ = [("length", w.USHORT), ("capacity", w.USHORT), ("buffer", w.LPWSTR)]


class _ObjectAttributes(c.Structure):
    _fields_ = [("length", w.ULONG), ("root", w.HANDLE), ("name", c.POINTER(_UnicodeString)),
                ("attributes", w.ULONG), ("security", c.c_void_p), ("quality", c.c_void_p)]


class _IoStatus(c.Structure):
    _fields_ = [("status", c.c_void_p), ("information", c.c_size_t)]


class _FileId(c.Structure):
    _fields_ = [("volume", c.c_ulonglong), ("identifier", c.c_ubyte * 16)]


class _FileBasic(c.Structure):
    _fields_ = [("created", c.c_longlong), ("accessed", c.c_longlong), ("written", c.c_longlong),
                ("changed", c.c_longlong), ("attributes", w.DWORD)]


class _FileStandard(c.Structure):
    _fields_ = [("allocation", c.c_longlong), ("size", c.c_longlong), ("links", w.DWORD),
                ("pending_delete", w.BYTE), ("directory", w.BYTE)]


class _RenameInfo(c.Structure):
    _fields_ = [("flags", w.DWORD), ("root", w.HANDLE), ("length", w.DWORD), ("name", w.WCHAR)]


_kernel = c.WinDLL("kernel32", use_last_error=True)
_nt = c.WinDLL("ntdll")
_kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
_kernel.CreateFileW.restype = w.HANDLE
_kernel.CloseHandle.argtypes = [w.HANDLE]
_kernel.CloseHandle.restype = w.BOOL
_kernel.GetDriveTypeW.argtypes = [w.LPCWSTR]
_kernel.GetDriveTypeW.restype = w.UINT
_kernel.GetFileInformationByHandleEx.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
_kernel.GetFileInformationByHandleEx.restype = w.BOOL
_nt.NtCreateFile.argtypes = [c.POINTER(w.HANDLE), w.DWORD, c.POINTER(_ObjectAttributes), c.POINTER(_IoStatus),
                             c.c_void_p, w.ULONG, w.ULONG, w.ULONG, w.ULONG, c.c_void_p, w.ULONG]
_nt.NtCreateFile.restype = w.LONG
_nt.NtSetInformationFile.argtypes = [w.HANDLE, c.POINTER(_IoStatus), c.c_void_p, w.ULONG, c.c_int]
_nt.NtSetInformationFile.restype = w.LONG
_nt.RtlNtStatusToDosError.argtypes = [w.LONG]
_nt.RtlNtStatusToDosError.restype = w.ULONG


def _validate(root, name, category):
    from file_organization import EXTENSIONS

    if (not root.is_absolute() or len(root.drive) != 2 or root.drive[1] != ":"
            or _kernel.GetDriveTypeW(root.anchor) != DRIVE_FIXED
            or any(part in {".", ".."} or any(char in part for char in ':<>*?"|')
                   or part.endswith((".", " ")) for part in root.parts[1:])):
        raise ValueError("只能归类本机固定磁盘上的目录。")
    if (not isinstance(name, str) or not name or name.endswith((".", " "))
            or any(char in '\\/:<>*?"|' or ord(char) < 32 for char in name)
            or PureWindowsPath(name).is_reserved()):
        raise ValueError("请选择整理预览中的普通文件。")
    if category not in CATEGORIES or EXTENSIONS.get(Path(name).suffix.lower()) != category:
        raise ValueError("文件类型或分类位置与整理预览不符。")


@contextmanager
def _root_handle(root):
    root_handle = _kernel.CreateFileW(root.anchor, TRAVERSE | READ_ATTRIBUTES, SHARE_READ | SHARE_WRITE, None,
                                     OPEN_EXISTING, BACKUP_SEMANTICS | OPEN_REPARSE_POINT, None)
    if root_handle == INVALID_HANDLE:
        raise c.WinError(c.get_last_error())
    with ExitStack() as stack:
        stack.callback(_kernel.CloseHandle, root_handle)
        handle = root_handle
        _directory(handle)
        for part in root.parts[1:]:
            handle = _open_relative(handle, part, directory=True)
            stack.callback(_kernel.CloseHandle, handle)
            _directory(handle)
        yield handle


def _open_relative(parent, name, *, directory=False, create=False, rename=False, share=None):
    size = len(name.encode("utf-16-le"))
    buffer = c.create_unicode_buffer(name)
    text = _UnicodeString(size, size + 2, c.cast(buffer, w.LPWSTR))
    attributes = _ObjectAttributes(c.sizeof(_ObjectAttributes), parent, c.pointer(text), CASE_INSENSITIVE, None, None)
    access = READ_ATTRIBUTES | SYNCHRONIZE
    if directory:
        access |= TRAVERSE
    if rename:
        access |= DELETE
    if share is None:
        share = SHARE_READ | SHARE_WRITE if directory else SHARE_READ
    handle = w.HANDLE()
    result = _nt.NtCreateFile(c.byref(handle), access, c.byref(attributes), c.byref(_IoStatus()), None, 0,
                              share, NT_CREATE if create else NT_OPEN,
                              NT_SYNCHRONOUS | OPEN_REPARSE_POINT | (NT_DIRECTORY if create else 0), None, 0)
    if result < 0:
        raise c.WinError(_nt.RtlNtStatusToDosError(result))
    return handle.value


@contextmanager
def _child(parent, name, *, missing=False, **options):
    try:
        handle = _open_relative(parent, name, **options)
    except FileNotFoundError:
        if not missing:
            raise
        yield None
        return
    try:
        yield handle
    finally:
        _kernel.CloseHandle(handle)


def _info(handle, kind, structure):
    values = structure()
    if not _kernel.GetFileInformationByHandleEx(handle, kind, c.byref(values), c.sizeof(values)):
        raise c.WinError(c.get_last_error())
    return values


def _identity(handle, basic):
    identity = _info(handle, FILE_ID_INFO, _FileId)
    if not any(identity.identifier) or not basic.created:
        raise ValueError("Windows 无法提供可核对的文件身份。")
    return {"volume": str(identity.volume), "id": bytes(identity.identifier).hex(), "created": str(basic.created)}


def _directory(handle):
    basic = _info(handle, FILE_BASIC_INFO, _FileBasic)
    if basic.attributes & REPARSE_POINT or not basic.attributes & DIRECTORY:
        raise ValueError("目录位置不是普通目录，或含有目录链接。")
    return {"identity": _identity(handle, basic), "written": str(basic.written), "changed": str(basic.changed)}


def _file(handle):
    basic = _info(handle, FILE_BASIC_INFO, _FileBasic)
    if basic.attributes & (DIRECTORY | REPARSE_POINT):
        raise ValueError("请选择整理预览中的普通文件。")
    standard = _info(handle, FILE_STANDARD_INFO, _FileStandard)
    if standard.pending_delete:
        raise ValueError("文件已待删除，无法核对归类结果。")
    return {"identity": _identity(handle, basic), "written": str(basic.written), "changed": str(basic.changed),
            "size": str(standard.size), "attributes": basic.attributes, "links": standard.links}


def _same_file_after_rename(before, after):
    return all(before[field] == after[field] for field in ("identity", "written", "size", "attributes", "links"))


def _windows_error(error):
    code = getattr(error, "winerror", None)
    if code in {32, 33}:
        return ValueError("文件或目录正被其他程序占用，请关闭后重新核对。")
    if code in {80, 183}:
        return ValueError("目标已出现同名文件或目录，请重新查看整理预览。")
    if code in {2, 3}:
        return ValueError(RETRY_PREVIEW)
    if code == 5:
        return ValueError("当前 Windows 账户无法执行该文件操作。")
    return ValueError(f"Windows 无法完成文件操作（错误 {code or error.errno}）。")


def _rename(source, destination, name):
    encoded = name.encode("utf-16-le")
    payload = c.create_string_buffer(c.sizeof(_RenameInfo) + len(encoded))
    header = _RenameInfo.from_buffer(payload)
    header.root, header.length = destination, len(encoded)
    c.memmove(c.addressof(payload) + _RenameInfo.name.offset, encoded, len(encoded))
    result = _nt.NtSetInformationFile(source, c.byref(_IoStatus()), c.byref(payload), c.sizeof(payload),
                                      NT_FILE_RENAME_INFORMATION)
    if result < 0:
        raise c.WinError(_nt.RtlNtStatusToDosError(result))


def _paths(snapshot):
    return Path(snapshot["root"]), snapshot["name"], snapshot["category"]


def _receipt(root, name, category, status, **extra):
    return {"status": status, "source": str(root / name), "destination": str(root / category / name), **extra}


def snapshot_file(root: Path, name: str, category: str) -> dict:
    _validate(root, name, category)
    try:
        with _root_handle(root) as anchor, _child(anchor, name) as source:
            root_id = _directory(anchor)["identity"]
            source_state = _file(source)
            if source_state["identity"]["volume"] != root_id["volume"]:
                raise ValueError("只支持在同一个本机卷上归类文件。")
            with _child(anchor, category, directory=True, missing=True) as destination:
                directory = _directory(destination) if destination is not None else None
                if directory is not None:
                    if directory["identity"]["volume"] != root_id["volume"]:
                        raise ValueError("只支持在同一个本机卷上归类文件。")
                    with _child(destination, name, missing=True) as existing:
                        if existing is not None:
                            raise ValueError("目标已经有同名项目。")
            return {"root": str(root), "name": name, "category": category, "root_id": root_id,
                    "source": source_state, "destination_dir": directory}
    except OSError as error:
        raise _windows_error(error) from error


def apply_file(snapshot: dict) -> dict:
    root, name, category = _paths(snapshot)
    _validate(root, name, category)
    try:
        with _root_handle(root) as anchor, _child(anchor, name, rename=True) as source, ExitStack() as stack:
            root_id = _directory(anchor)["identity"]
            if root_id != snapshot["root_id"] or _file(source) != snapshot["source"]:
                raise ValueError(RETRY_PREVIEW)
            destination = stack.enter_context(_child(anchor, category, directory=True, missing=True))
            expected = snapshot["destination_dir"]
            current = _directory(destination) if destination is not None else None
            if current != expected:
                raise ValueError(RETRY_PREVIEW)
            if destination is None:
                destination = stack.enter_context(_child(anchor, category, directory=True, create=True))
                current = _directory(destination)
            if current["identity"]["volume"] != root_id["volume"]:
                raise ValueError("只支持在同一个本机卷上归类文件。")
            _rename(source, destination, name)
            try:
                share = SHARE_READ | SHARE_DELETE
                with (_child(anchor, name, missing=True, share=share) as original,
                      _child(destination, name, missing=True, share=share) as moved):
                    if original is None and moved is not None and _same_file_after_rename(snapshot["source"], _file(moved)):
                        return _receipt(root, name, category, "moved")
            except (OSError, ValueError):
                return _receipt(root, name, category, "unknown")
    except OSError as error:
        raise _windows_error(error) from error
    return _receipt(root, name, category, "unknown")


def recover_file(snapshot: dict) -> dict:
    root, name, category = _paths(snapshot)
    unknown = _receipt(root, name, category, "unknown")
    try:
        _validate(root, name, category)
        with _root_handle(root) as anchor, _child(anchor, name, missing=True) as source:
            if _directory(anchor)["identity"] != snapshot["root_id"]:
                return unknown
            with _child(anchor, category, directory=True, missing=True) as destination:
                current = _directory(destination) if destination is not None else None
                expected = snapshot["destination_dir"]
                if destination is None:
                    if source is not None and _file(source) == snapshot["source"]:
                        return _receipt(root, name, category, "not_moved", requires_new_preview=expected is not None)
                    return unknown
                with _child(destination, name, missing=True) as moved:
                    if source is not None and _file(source) == snapshot["source"] and moved is None:
                        return _receipt(root, name, category, "not_moved", requires_new_preview=current != expected)
                    if (source is None and moved is not None and (expected is None or current["identity"] == expected["identity"])
                            and _same_file_after_rename(snapshot["source"], _file(moved))):
                        return _receipt(root, name, category, "moved")
    except (OSError, ValueError):
        return unknown
    return unknown
