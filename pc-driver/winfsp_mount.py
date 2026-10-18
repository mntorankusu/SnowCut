r"""winfsp_mount.py: the machine's USB drive (A:) as a Windows drive letter, via WinFsp.

Three layers:

  DriveFS     Windows file-system semantics on top of the 0xC4 gateway client
              (cm110_fs.C4Client, or MockC4Client in tests / simulator mode).
              Pure Python, no WinFsp needed: the tests drive it directly.
  WinFspHost  ctypes binding to winfsp-x64.dll (no winfspy: it has no wheels for
              current Pythons and needs a compiler). Forwards WinFsp callbacks to
              DriveFS and maps Python exceptions to NTSTATUS.
  DriveMount  the one-click mount: opens the machine, mounts A: on the device if
              it is not already, shows the machine's busy pop-up for the whole
              mount (its touch UI must not access the drive meanwhile: fsys_Mount is
              not idempotent), starts WinFsp on a free drive letter, and watches
              the machine with a heartbeat. stop() undoes all of it in reverse.

What the firmware can do decides the design (tools/fs_handler.py, 2026-10):
  - OPEN with any write flag is fopen(path, "w"): it ALWAYS creates/truncates,
    and a write handle cannot read. There is no in-place edit.
  - WRITE must start at the device file's current end (no seek past EOF).
  - TRUNCATE is refused. DELETE / MKDIR / RMDIR / RENAME (a full move) exist
    from the 2026-10-16 handler on. DriveMount asks the client which ops this
    firmware refuses (refused_ops()) and those become STATUS_NOT_SUPPORTED.
    The firmware's FAT library will not remove or move a file it has open, so
    this module's own handles on it are settled and closed first.
  - 8 device handles in total, and names in LIST are <= 51 bytes, ASCII only.
So a file being written is held in a host-side mirror (spooled temp file) and:
  - stream mode (new file, or opened with truncate, e.g. Explorer copy/replace):
    writes that arrive in order go straight through to one "w" handle, so the
    copy progress Windows shows is real and disk-full errors reach the writer.
  - anything else (a write behind or past the streamed end, an edit of an existing
    file, a shrink) switches the file to spill mode: the device copy is rewritten
    from the mirror when the last writer closes the file, or on FlushFileBuffers.
Cleanup cannot report errors to Windows, so a failed commit goes to on_error
(the app shows it). Unmounting commits whatever is still pending.

File times are not stored by the gateway, so every entry shows the mount time.
Free space is not reported by the gateway either, so the volume reports a fixed size.
"""
import contextlib
import ctypes
import errno
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cm110_fs as sfs

# -- NTSTATUS ----------------------------------------------------------------------------------
STATUS_SUCCESS = 0x00000000
STATUS_BUFFER_OVERFLOW = 0x80000005
STATUS_DEVICE_BUSY = 0x80000011
STATUS_INVALID_DEVICE_REQUEST = 0xC0000010
STATUS_INVALID_PARAMETER = 0xC000000D
STATUS_END_OF_FILE = 0xC0000011
STATUS_ACCESS_DENIED = 0xC0000022
STATUS_OBJECT_NAME_INVALID = 0xC0000033
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_OBJECT_NAME_COLLISION = 0xC0000035
STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
STATUS_DISK_FULL = 0xC000007F
STATUS_DEVICE_NOT_CONNECTED = 0xC000009D
STATUS_MEDIA_WRITE_PROTECTED = 0xC00000A2
STATUS_FILE_IS_A_DIRECTORY = 0xC00000BA
STATUS_NOT_SUPPORTED = 0xC00000BB
STATUS_INTERNAL_ERROR = 0xC00000E5
STATUS_DIRECTORY_NOT_EMPTY = 0xC0000101
STATUS_NOT_A_DIRECTORY = 0xC0000103
STATUS_TOO_MANY_OPENED_FILES = 0xC000011F
STATUS_NO_MEDIA_IN_DEVICE = 0xC0000178
STATUS_IO_DEVICE_ERROR = 0xC0000185

FILE_ATTRIBUTE_READONLY, FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM = 0x01, 0x02, 0x04
FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_ARCHIVE, FILE_ATTRIBUTE_NORMAL = 0x10, 0x20, 0x80
FILE_DIRECTORY_FILE = 0x00000001                          # CreateOptions
FILE_WRITE_DATA, FILE_APPEND_DATA = 0x0002, 0x0004        # GrantedAccess
CLEANUP_DELETE = 0x01                                     # FspCleanupDelete

MAX_NAME = 51          # LIST record name field is 52 bytes incl NUL: longer names would vanish
ALLOC_UNIT = 4096
VOLUME_LABEL = "SnowCut USB"
VOLUME_TOTAL = 4 << 30  # the gateway has no statfs: report a fixed size
VOLUME_FREE = 2 << 30
CACHE_TTL = 10.0        # s (the machine's own UI is blocked by the busy pop-up while mounted)
_SPOOL_MAX = 32 << 20   # mirror stays in RAM up to this size, then spills to a temp file


class NtError(Exception):
    """An operation result that is a specific NTSTATUS rather than a Python OS error."""

    def __init__(self, status, msg=""):
        super().__init__(msg or "NTSTATUS %#010x" % status)
        self.status = status


def nt_status(e):
    """Python exception -> NTSTATUS (subclasses before their bases)."""
    if isinstance(e, NtError):
        return e.status
    if isinstance(e, FileNotFoundError):
        return STATUS_OBJECT_NAME_NOT_FOUND
    if isinstance(e, NotADirectoryError):
        return STATUS_NOT_A_DIRECTORY
    if isinstance(e, IsADirectoryError):
        return STATUS_FILE_IS_A_DIRECTORY
    if isinstance(e, FileExistsError):
        return STATUS_OBJECT_NAME_COLLISION
    if isinstance(e, sfs.BusyError):
        return STATUS_DEVICE_BUSY
    if isinstance(e, PermissionError):
        return STATUS_ACCESS_DENIED
    if isinstance(e, ConnectionError):
        return STATUS_DEVICE_NOT_CONNECTED
    if isinstance(e, OSError):
        if e.errno == errno.ENOSPC:
            return STATUS_DISK_FULL
        if e.errno == errno.ENOTEMPTY:
            return STATUS_DIRECTORY_NOT_EMPTY
        if "no medium" in str(e):
            return STATUS_NO_MEDIA_IN_DEVICE
        return STATUS_IO_DEVICE_ERROR
    if isinstance(e, ValueError):
        return STATUS_INVALID_PARAMETER
    return STATUS_INTERNAL_ERROR


def filetime_now():
    return int((time.time() + 11644473600) * 10_000_000)


# -- DriveFS: Windows semantics over the gateway -----------------------------------------------
class _Node:
    """One open file (shared by every Windows handle to it)."""

    def __init__(self, path, size):
        self.path = path            # device path, original case ("A:\DIR\FILE.JPG")
        self.opens = 0              # live Windows handles (Open/Create .. Close)
        self.writers = 0            # of those, opened with write access (until Cleanup)
        self.dev_size = size        # size of the device copy (when not writing)
        self.rh = None              # device read handle, opened on first read
        # write session (mirror is None when the file is not being written)
        self.mirror = None
        self.size = size            # logical size while writing
        self.stream = False         # in-order writes go through self.wh
        self.wh = None              # device "w" handle (stream mode)
        self.streamed = 0           # bytes written through wh
        self.dirty = False          # spill mode: the device copy must be rewritten
        self.on_device = True       # False: created here, nothing on the machine yet
        self.delete = False         # Windows asked to delete it (allowed only if not on_device)

    @property
    def writing(self):
        return self.mirror is not None

    @property
    def pending(self):
        """Something not yet on the machine."""
        return self.writing and (self.stream or self.dirty or not self.on_device)


class _Ctx:
    """One Windows open handle (FileContext with UmFileContextIsUserContext2)."""
    __slots__ = ("path", "key", "node", "is_dir", "writable", "listing", "cleaned")

    def __init__(self, path, key, node, is_dir, writable):
        self.path, self.key, self.node = path, key, node
        self.is_dir, self.writable = is_dir, writable
        self.listing = None         # directory snapshot for ReadDirectory markers
        self.cleaned = False


class DriveFS:
    """Windows file operations on one device drive. Every public method takes the one lock:
    the gateway is serial anyway, and WinFsp may call from several dispatcher threads."""

    def __init__(self, client, drive=sfs.DEFAULT_DRIVE, read_only=False, on_error=None,
                 refused=sfs.REFUSED_OPS, now=None):
        self.client = client
        self.drive = drive.upper()[:1]
        self.read_only = read_only
        self.on_error = on_error or (lambda path, exc: None)
        self.refused = frozenset(refused)
        self.time = now if now is not None else filetime_now()
        self.lock = threading.RLock()
        self._nodes = {}            # key -> _Node
        self._ctxs = {}             # id -> _Ctx
        self._next_id = 1
        self._meta = {}             # key -> (t, attr, size)
        self._dirs = {}             # key -> (t, [(name, attr, size)])

    # -- paths / caches --
    def _dev_path(self, win_path):
        try:
            p = sfs.to_device_path(win_path or "\\", self.drive)
            sfs.encode_path(p)          # ASCII / length rules of the wire path
        except ValueError as e:
            raise NtError(STATUS_OBJECT_NAME_INVALID, str(e)) from None
        if p[0] != self.drive:
            raise NtError(STATUS_OBJECT_NAME_INVALID, win_path)
        return p

    def _root(self):
        return "%s:\\" % self.drive

    @staticmethod
    def _parent(path):
        head, _, name = path.rpartition("\\")
        if len(head) == 2:              # "A:"
            head += "\\"
        return head, name

    def _fresh(self, ent):
        return ent is not None and time.monotonic() - ent[0] < CACHE_TTL

    def _invalidate(self, path):
        key = path.upper()
        self._meta.pop(key, None)
        self._dirs.pop(self._parent(path)[0].upper(), None)

    def invalidate_all(self):
        with self.lock:
            self._meta.clear()
            self._dirs.clear()

    def _listing(self, path):
        key = path.upper()
        ent = self._dirs.get(key)
        if not self._fresh(ent):
            items = list(self.client.list_dir(path))
            now = time.monotonic()
            ent = (now, items)
            self._dirs[key] = ent
            base = path.rstrip("\\") + "\\"
            for name, attr, size in items:
                self._meta[(base + name).upper()] = (now, attr, size)
        return ent[1]

    def _stat(self, path):
        """(attr, size) of a device path, or FileNotFoundError when absent."""
        key = path.upper()
        if key == self._root().upper():
            return FILE_ATTRIBUTE_DIRECTORY, 0
        node = self._nodes.get(key)
        if node is not None and node.delete:
            raise FileNotFoundError(path)
        if node is not None and (node.writing or not node.on_device):
            return FILE_ATTRIBUTE_ARCHIVE, node.size
        ent = self._meta.get(key)
        if self._fresh(ent):
            return ent[1], ent[2]
        parent, _ = self._parent(path)
        dent = self._dirs.get(parent.upper())
        if self._fresh(dent):           # a fresh listing is complete: absent means absent
            raise FileNotFoundError(path)
        attr, size = self.client.stat(path)
        self._meta[key] = (time.monotonic(), attr, size)
        return attr, size

    def _info(self, attr, size):
        is_dir = sfs._is_dir_attr(attr)
        if is_dir:
            fa, size = FILE_ATTRIBUTE_DIRECTORY | (attr & 0x06), 0
        else:
            fa = attr & (FILE_ATTRIBUTE_READONLY | FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
                         | FILE_ATTRIBUTE_ARCHIVE)
            fa = fa or FILE_ATTRIBUTE_NORMAL
        return {"attr": fa, "size": size, "alloc": -(-size // ALLOC_UNIT) * ALLOC_UNIT,
                "time": self.time, "dir": is_dir}

    def _ctx_info(self, c):
        if c.is_dir:
            return self._info(FILE_ATTRIBUTE_DIRECTORY, 0)
        n = c.node
        return self._info(FILE_ATTRIBUTE_ARCHIVE, n.size if n.writing else n.dev_size)

    def _ctx(self, ctx_id):
        c = self._ctxs.get(ctx_id)
        if c is None:
            raise NtError(STATUS_INVALID_PARAMETER, "unknown file context %r" % (ctx_id,))
        return c

    def _new_ctx(self, path, node, is_dir, writable):
        cid = self._next_id
        self._next_id += 1
        self._ctxs[cid] = _Ctx(path, path.upper(), node, is_dir, writable)
        if node is not None:
            node.opens += 1
            if writable:
                node.writers += 1
        return cid

    def _node_for(self, path, size):
        key = path.upper()
        node = self._nodes.get(key)
        if node is None:
            node = self._nodes[key] = _Node(path, size)
        return node

    # -- lookups --
    def get_attributes(self, win_path):
        """GetSecurityByName: FILE_ATTRIBUTE_* of an existing path."""
        with self.lock:
            try:
                path = self._dev_path(win_path)
            except NtError:
                raise FileNotFoundError(win_path) from None  # such a name cannot exist here
            return self._info(*self._stat(path))["attr"]

    def open(self, win_path, create_options=0, granted_access=0):
        """-> (ctx_id, info)."""
        with self.lock:
            path = self._dev_path(win_path)
            attr, size = self._stat(path)
            if sfs._is_dir_attr(attr):
                return self._new_ctx(path, None, True, False), self._info(attr, size)
            writable = bool(granted_access & (FILE_WRITE_DATA | FILE_APPEND_DATA))
            node = self._node_for(path, size)
            if not node.writing:
                node.dev_size = node.size = size
            cid = self._new_ctx(path, node, False, writable)
            return cid, self._ctx_info(self._ctxs[cid])

    def create(self, win_path, create_options=0, granted_access=0):
        """A new file (or directory). WinFsp sends Create only for create-new."""
        with self.lock:
            if self.read_only:
                raise NtError(STATUS_MEDIA_WRITE_PROTECTED)
            path = self._dev_path(win_path)
            parent, name = self._parent(path)
            if len(name.encode("ascii")) > MAX_NAME:
                raise NtError(STATUS_OBJECT_NAME_INVALID,
                              "name longer than %d characters" % MAX_NAME)
            try:
                pattr, _ = self._stat(parent)
            except FileNotFoundError:
                raise NtError(STATUS_OBJECT_PATH_NOT_FOUND, parent) from None
            if not sfs._is_dir_attr(pattr):
                raise NtError(STATUS_OBJECT_PATH_NOT_FOUND, parent)
            try:
                self._stat(path)
                raise NtError(STATUS_OBJECT_NAME_COLLISION, path)
            except FileNotFoundError:
                pass
            if create_options & FILE_DIRECTORY_FILE:
                self._refusable("mkdir", self.client.mkdir, path)
                self._invalidate(path)
                return (self._new_ctx(path, None, True, False),
                        self._info(FILE_ATTRIBUTE_DIRECTORY, 0))
            old = self._nodes.get(path.upper())
            if old is not None and old.delete:   # deleted, its handles not yet closed
                self._nodes.pop(path.upper())
            node = self._node_for(path, 0)
            node.on_device = False
            self._start_write(node, truncate=True)
            cid = self._new_ctx(path, node, False, True)
            self._dirs.pop(parent.upper(), None)
            return cid, self._ctx_info(self._ctxs[cid])

    def _refusable(self, op, fn, *args):
        if op in self.refused:
            raise NtError(STATUS_NOT_SUPPORTED, "the machine's firmware cannot %s" % op)
        try:
            return fn(*args)
        except ValueError as e:      # 05/24: refused by this firmware
            raise NtError(STATUS_NOT_SUPPORTED, str(e)) from None

    # -- write sessions --
    def _start_write(self, node, truncate):
        if node.writing:
            if not truncate:
                return
            self._close_wh(node)
            node.mirror.close()
        elif node.rh is not None:
            self._close_handle(node, "rh")
        node.mirror = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX)
        node.stream, node.streamed, node.dirty = truncate, 0, False
        node.size = 0
        if not truncate:
            # An edit of an existing file: the machine can only rewrite it whole, so start from
            # its current content.
            self._load_mirror(node)

    def _load_mirror(self, node):
        h = self._open_device(node.path, sfs.O_READ)
        try:
            off = 0
            while off < node.dev_size:
                blk = self.client.read_at(h, off, min(64 * 1024, node.dev_size - off))
                if not blk:
                    break
                node.mirror.seek(off)
                node.mirror.write(blk)
                off += len(blk)
            node.size = off
        finally:
            self._close_quiet(h)

    def _to_spill(self, node):
        if node.stream:
            node.stream = False
            self._close_wh(node)
        node.dirty = True

    def _close_wh(self, node):
        if node.wh is not None:
            h, node.wh = node.wh, None
            self.client.close(h)

    def _close_handle(self, node, attr):
        h = getattr(node, attr)
        if h is not None:
            setattr(node, attr, None)
            self._close_quiet(h)

    def _close_quiet(self, h):
        try:
            self.client.close(h)
        except Exception:
            pass

    def _open_device(self, path, flags):
        """Open a device handle. When all 8 are taken, give back idle read handles once."""
        try:
            return self.client.open(path, flags)
        except OSError as e:
            if e.errno != errno.ENOSPC:
                raise
            freed = False
            for n in self._nodes.values():
                if n.rh is not None:
                    self._close_handle(n, "rh")
                    freed = True
            if not freed:
                raise NtError(STATUS_TOO_MANY_OPENED_FILES, str(e)) from None
            return self.client.open(path, flags)

    def _mirror_read(self, node, offset, length):
        length = max(0, min(length, node.size - offset))
        node.mirror.seek(0, os.SEEK_END)
        have = node.mirror.tell()
        data = b""
        if offset < have:
            node.mirror.seek(offset)
            data = node.mirror.read(min(length, have - offset))
        return data + bytes(length - len(data))

    def write(self, ctx_id, offset, data, to_eof=False, constrained=False):
        """-> (bytes written, info)."""
        with self.lock:
            if self.read_only:
                raise NtError(STATUS_MEDIA_WRITE_PROTECTED)
            c = self._ctx(ctx_id)
            if c.is_dir:
                raise IsADirectoryError(c.path)
            node = c.node
            if not node.writing:
                self._start_write(node, truncate=False)
            if to_eof:
                offset = node.size
            if constrained:             # paging I/O never extends the file
                if offset >= node.size:
                    return 0, self._ctx_info(c)
                data = data[:node.size - offset]
            data = bytes(data)
            if node.stream:
                if offset == node.streamed:
                    try:
                        if node.wh is None:
                            node.wh = self._open_device(
                                node.path, sfs.O_WRITE | sfs.O_CREATE | sfs.O_TRUNC)
                            node.on_device = True
                        self.client.write_at(node.wh, offset, data)
                    except Exception:
                        self._to_spill(node)   # what reached the machine is unknown now
                        raise
                    node.streamed += len(data)
                else:
                    self._to_spill(node)
            else:
                node.dirty = True
            node.mirror.seek(0, os.SEEK_END)
            if offset > node.mirror.tell():         # a hole: explicit zeros (tempfile may be
                node.mirror.write(bytes(offset - node.mirror.tell()))   # in RAM, no sparse)
            node.mirror.seek(offset)
            node.mirror.write(data)
            node.size = max(node.size, offset + len(data))
            return len(data), self._ctx_info(c)

    def set_file_size(self, ctx_id, new_size, allocation=False):
        with self.lock:
            c = self._ctx(ctx_id)
            if c.is_dir:
                raise IsADirectoryError(c.path)
            node = c.node
            cur = node.size if node.writing else node.dev_size
            if allocation and new_size >= cur:
                return self._ctx_info(c)    # allocation only: nothing to do
            if new_size == cur:
                return self._ctx_info(c)
            if self.read_only:
                raise NtError(STATUS_MEDIA_WRITE_PROTECTED)
            if not node.writing:
                self._start_write(node, truncate=(new_size == 0))
            if new_size < node.size:
                if node.stream and new_size < node.streamed:
                    self._to_spill(node)
                elif not node.stream:
                    node.dirty = True
                node.mirror.truncate(new_size)
            elif not node.stream:           # grow: zeros (stream mode adds them at commit)
                node.dirty = True
            node.size = new_size
            return self._ctx_info(c)

    def overwrite(self, ctx_id):
        """Open-with-truncate of an existing file (CREATE_ALWAYS, TRUNCATE_EXISTING, replace)."""
        with self.lock:
            if self.read_only:
                raise NtError(STATUS_MEDIA_WRITE_PROTECTED)
            c = self._ctx(ctx_id)
            if c.is_dir:
                raise IsADirectoryError(c.path)
            self._start_write(c.node, truncate=True)
            return self._ctx_info(c)

    def _commit(self, node):
        """Bring the machine's copy in line with the mirror. Raises on failure (dirty stays set
        so a later flush/close tries again)."""
        if not node.pending or node.delete:
            return
        try:
            if node.stream:
                if node.wh is None:
                    node.wh = self._open_device(node.path,
                                                sfs.O_WRITE | sfs.O_CREATE | sfs.O_TRUNC)
                    node.on_device = True
                while node.streamed < node.size:        # grown, never written: zeros
                    n = min(sfs.CHUNK, node.size - node.streamed)
                    self.client.write_at(node.wh, node.streamed, bytes(n))
                    node.streamed += n
                self._close_wh(node)
            else:
                h = self._open_device(node.path, sfs.O_WRITE | sfs.O_CREATE | sfs.O_TRUNC)
                node.on_device = True
                try:
                    off = 0
                    while off < node.size:
                        blk = self._mirror_read(node, off, 16 * sfs.CHUNK)
                        self.client.write_at(h, off, blk)
                        off += len(blk)
                except BaseException:
                    self._close_quiet(h)
                    raise
                self.client.close(h)        # durability gate: disk full surfaces here
            node.stream, node.dirty = False, False
        except Exception:
            node.stream, node.dirty = False, True
            self._close_handle(node, "wh")
            self._invalidate(node.path)
            raise
        self._invalidate(node.path)
        attr, size = self.client.stat(node.path)
        self._meta[node.path.upper()] = (time.monotonic(), attr, size)
        node.dev_size = size
        if size != node.size:
            node.dirty = True
            raise IOError("the machine stored %d of %d bytes" % (size, node.size))

    def _commit_reporting(self, node):
        try:
            self._commit(node)
            return True
        except Exception as e:
            self.on_error(node.path, e)
            return False

    def flush(self, ctx_id=None):
        """FlushFileBuffers (ctx) or volume flush (None)."""
        with self.lock:
            if ctx_id is None:
                errs = [n for n in list(self._nodes.values())
                        if n.dirty and not self._commit_reporting(n)]
                if errs:
                    raise IOError("could not save %s" % errs[0].path)
                return None
            c = self._ctx(ctx_id)
            if not c.is_dir and c.node.dirty:   # stream mode: data is already on the machine
                self._commit(c.node)
            return self._ctx_info(c)

    # -- reads --
    def read(self, ctx_id, offset, length):
        with self.lock:
            c = self._ctx(ctx_id)
            if c.is_dir:
                raise IsADirectoryError(c.path)
            node = c.node
            if node.writing:
                if offset >= node.size:
                    raise NtError(STATUS_END_OF_FILE)
                return self._mirror_read(node, offset, length)
            if offset >= node.dev_size:
                raise NtError(STATUS_END_OF_FILE)
            length = min(length, node.dev_size - offset)
            data = b""
            for attempt in range(2):
                if node.rh is None:
                    node.rh = self._open_device(node.path, sfs.O_READ)
                data += self.client.read_at(node.rh, offset + len(data), length - len(data))
                if len(data) == length:
                    return data
                # Short before the known end: the device reports a failed read the same
                # way as EOF (and its FILE is now in error). Never pass that on as the
                # end of the file, because a copy would be silently cut short. Retry once on a
                # fresh handle, then fail the read. (Seen when the machine's idle UI
                # unmounted A: mid-read. The busy pop-up DriveMount shows prevents it.)
                self._close_handle(node, "rh")
            raise IOError("read error in %s at %d (%d of %d bytes)"
                          % (node.path, offset, len(data), length))

    def get_file_info(self, ctx_id):
        with self.lock:
            return self._ctx_info(self._ctx(ctx_id))

    def read_directory(self, ctx_id, marker=None):
        """-> [(name, info)] after `marker`, '.'/'..' first (except at the root)."""
        with self.lock:
            c = self._ctx(ctx_id)
            if not c.is_dir:
                raise NotADirectoryError(c.path)
            if c.listing is None or marker is None:
                entries = {}
                for name, attr, size in self._listing(c.path):
                    entries[name.upper()] = (name, attr, size)
                base = c.key.rstrip("\\") + "\\"
                for key, n in self._nodes.items():          # files not (fully) on the machine yet
                    rest = key[len(base):]
                    if not key.startswith(base) or "\\" in rest:
                        continue
                    if n.delete:
                        entries.pop(rest, None)
                    elif n.writing or not n.on_device:
                        entries[rest] = (n.path.rsplit("\\", 1)[1], FILE_ATTRIBUTE_ARCHIVE, n.size)
                rows = sorted(entries.values(), key=lambda e: e[0].upper())
                out = [(name, self._info(attr, size)) for name, attr, size in rows]
                if c.key != self._root().upper():
                    d = self._info(FILE_ATTRIBUTE_DIRECTORY, 0)
                    out = [(".", d), ("..", d)] + out
                c.listing = out
            out = c.listing
            if marker is None:
                return list(out)
            for i, (name, _) in enumerate(out):
                if name.upper() == marker.upper():
                    return out[i + 1:]
            mk = marker.upper()             # marker vanished: continue in name order
            return [e for e in out if e[0] not in (".", "..") and e[0].upper() > mk]

    # -- delete / rename --
    def _under(self, path):
        """Open nodes at `path` or beneath it."""
        k = path.upper()
        return [n for key, n in self._nodes.items() if key == k or key.startswith(k + "\\")]

    def can_delete(self, ctx_id):
        """Windows asks before it marks a file for delete-on-close: say no now if it cannot
        happen, because the delete itself runs in Cleanup, which cannot report errors."""
        with self.lock:
            if self.read_only:
                raise NtError(STATUS_MEDIA_WRITE_PROTECTED)
            c = self._ctx(ctx_id)
            if c.is_dir:
                if c.key == self._root().upper():
                    raise NtError(STATUS_ACCESS_DENIED, "the root cannot be deleted")
                op = "rmdir"
            elif c.node is not None and not c.node.on_device:
                return                       # created here and never written: just forget it
            else:
                op = "delete"
            if op in self.refused:
                raise NtError(STATUS_NOT_SUPPORTED, "the machine's firmware cannot %s" % op)
            if c.is_dir and (self._listing(c.path) or [n for n in self._under(c.path)
                                                     if n.path.upper() != c.key and not n.delete]):
                raise NtError(STATUS_DIRECTORY_NOT_EMPTY, c.path)

    def rename(self, ctx_id, new_win_path, replace=False):
        """A move: the firmware's move() takes full paths, so folders can change too."""
        with self.lock:
            if self.read_only:
                raise NtError(STATUS_MEDIA_WRITE_PROTECTED)
            c = self._ctx(ctx_id)
            if "rename" in self.refused:
                raise NtError(STATUS_NOT_SUPPORTED, "the machine's firmware cannot rename")
            dst = self._dev_path(new_win_path)
            if len(self._parent(dst)[1].encode("ascii")) > MAX_NAME:
                raise NtError(STATUS_OBJECT_NAME_INVALID,
                              "name longer than %d characters" % MAX_NAME)
            if dst.upper() != c.key:            # (a case-only rename is the same entry)
                try:
                    dattr, _ = self._stat(dst)
                except FileNotFoundError:
                    dattr = None
                if dattr is not None:
                    if not replace:
                        raise NtError(STATUS_OBJECT_NAME_COLLISION, dst)
                    if sfs._is_dir_attr(dattr) or any(not n.delete for n in self._under(dst)):
                        raise NtError(STATUS_ACCESS_DENIED, "%s is a folder or open" % dst)
                    self._refusable("delete", self.client.delete, dst)
                    self._invalidate(dst)
            # The FAT library will not move a file it has open: put what is pending on the
            # machine and close this module's own device handles first. (Later writes to a moved
            # file just rewrite it at its new path.)
            for n in self._under(c.path):
                if n.pending and not n.delete:
                    self._commit(n)
                self._close_handle(n, "rh")
                self._close_handle(n, "wh")
            if dst.upper() == c.key and dst != c.path:
                self._case_rename(c.path, dst)
            else:
                self._refusable("rename", self.client.rename, c.path, dst)
            self._repath(c.path, dst)
            self.invalidate_all()               # listings below a moved folder are stale too

    def _case_rename(self, src, dst):
        """The library ignores a case-only move (same entry, LIVE 2026-10-16), so go
        through a temporary name. If the second step fails, put the original name back."""
        parent, name = self._parent(src)
        base = parent.rstrip("\\") + "\\"
        for i in range(100):
            tmp = base + "~cr%d.tmp" % i
            try:
                self._stat(tmp)
            except FileNotFoundError:
                break
        else:
            raise NtError(STATUS_ACCESS_DENIED, "no free temporary name in %s" % parent)
        self._refusable("rename", self.client.rename, src, tmp)
        try:
            self._refusable("rename", self.client.rename, tmp, dst)
        except BaseException:
            try:
                self.client.rename(tmp, src)
            except Exception as e:
                self.on_error(src, e)       # left as tmp: say so rather than lose track of it
            raise

    def _repath(self, old, new):
        """After a move: open nodes and handles at/below `old` now live at `new`."""
        ok = old.upper()

        def moved(key):
            return key == ok or key.startswith(ok + "\\")
        for key in [k for k in self._nodes if moved(k)]:
            n = self._nodes.pop(key)
            n.path = new + n.path[len(old):]
            self._nodes[n.path.upper()] = n
        for c in self._ctxs.values():
            if moved(c.key):
                c.path = new + c.path[len(old):]
                c.key = c.path.upper()
                c.listing = None

    # -- handle lifetime --
    def cleanup(self, ctx_id, flags=0):
        """Last user handle of this open closed. Errors go to on_error (Windows cannot accept them)."""
        with self.lock:
            c = self._ctxs.get(ctx_id)
            if c is None or c.cleaned:
                return
            c.cleaned = True
            node = c.node
            if flags & CLEANUP_DELETE:
                self._do_delete(c)
                return
            if node is None or not c.writable:
                return
            node.writers -= 1
            if node.writers == 0 and node.pending:
                self._commit_reporting(node)

    def _do_delete(self, c):
        node = c.node
        try:
            if c.is_dir:
                self._refusable("rmdir", self.client.rmdir, c.path)
            else:
                # Whatever was still to be written goes with the file. This module's handles on it
                # must close first (the library will not remove an open file).
                node.delete = True
                self._close_handle(node, "rh")
                self._close_handle(node, "wh")
                if node.mirror is not None:
                    node.mirror.close()
                    node.mirror = None
                if node.on_device:
                    self._refusable("delete", self.client.delete, c.path)
        except Exception as e:
            if node is not None:
                node.delete = False              # still there: keep showing it
                node.dev_size = 0
                self._meta.pop(c.key, None)
            self.on_error(c.path, e)
        self._invalidate(c.path)
        self._dirs.pop(c.key, None)

    def close(self, ctx_id):
        with self.lock:
            c = self._ctxs.pop(ctx_id, None)
            if c is None or c.node is None:
                return
            node = c.node
            node.opens -= 1
            if not c.cleaned and c.writable:
                node.writers -= 1
            if node.opens > 0:
                return
            if node.pending:                    # late (paging) writes after cleanup
                self._commit_reporting(node)
            self._release(node)

    def _release(self, node):
        self._close_handle(node, "rh")
        self._close_handle(node, "wh")
        if node.mirror is not None:
            node.mirror.close()
            node.mirror = None
        if self._nodes.get(node.path.upper()) is node:    # not a newer file of that name
            self._nodes.pop(node.path.upper())

    def pending_files(self):
        """Device paths with data not yet on the machine (or still open for writing)."""
        with self.lock:
            return [n.path for n in self._nodes.values() if n.pending and not n.delete]

    def shutdown(self):
        """Unmount: commit what is pending, close every device handle. -> list of failures."""
        failed = []
        with self.lock:
            for node in list(self._nodes.values()):
                if node.pending and not self._commit_reporting(node):
                    failed.append(node.path)
                self._release(node)
            self._ctxs.clear()
        return failed


# -- WinFsp binding (ctypes) -------------------------------------------------------------------
if sys.platform == "win32":
    from ctypes import wintypes as _wt

    _NTSTATUS = ctypes.c_long
    _P = ctypes.c_void_p
    _BOOL8 = ctypes.c_ubyte
    _U32, _U64, _ULONG = ctypes.c_uint32, ctypes.c_uint64, ctypes.c_ulong

    class FSP_FSCTL_FILE_INFO(ctypes.Structure):
        _fields_ = [("FileAttributes", _U32), ("ReparseTag", _U32),
                    ("AllocationSize", _U64), ("FileSize", _U64),
                    ("CreationTime", _U64), ("LastAccessTime", _U64),
                    ("LastWriteTime", _U64), ("ChangeTime", _U64),
                    ("IndexNumber", _U64), ("HardLinks", _U32), ("EaSize", _U32)]

    class FSP_FSCTL_VOLUME_INFO(ctypes.Structure):
        _fields_ = [("TotalSize", _U64), ("FreeSize", _U64),
                    ("VolumeLabelLength", ctypes.c_uint16),
                    ("VolumeLabel", ctypes.c_wchar * 32)]

    class FSP_FSCTL_VOLUME_PARAMS(ctypes.Structure):
        # The C bitfields are one UINT32 each here (Flags / Flags2), with the bits below.
        _fields_ = [("Version", ctypes.c_uint16), ("SectorSize", ctypes.c_uint16),
                    ("SectorsPerAllocationUnit", ctypes.c_uint16),
                    ("MaxComponentLength", ctypes.c_uint16),
                    ("VolumeCreationTime", _U64), ("VolumeSerialNumber", _U32),
                    ("TransactTimeout", _U32), ("IrpTimeout", _U32), ("IrpCapacity", _U32),
                    ("FileInfoTimeout", _U32), ("Flags", _U32),
                    ("Prefix", ctypes.c_wchar * 192), ("FileSystemName", ctypes.c_wchar * 16),
                    ("Flags2", _U32), ("VolumeInfoTimeout", _U32), ("DirInfoTimeout", _U32),
                    ("SecurityTimeout", _U32), ("StreamInfoTimeout", _U32),
                    ("EaTimeout", _U32), ("FsextControlCode", _U32), ("Reserved32", _U32),
                    ("Reserved64", _U64 * 2)]

    assert ctypes.sizeof(FSP_FSCTL_FILE_INFO) == 72
    assert ctypes.sizeof(FSP_FSCTL_VOLUME_INFO) == 88
    assert ctypes.sizeof(FSP_FSCTL_VOLUME_PARAMS) == 504

    # FSP_FSCTL_VOLUME_PARAMS bitfield positions (inc/winfsp/fsctl.h, in declaration order)
    VP_CASE_PRESERVED_NAMES = 1 << 1
    VP_UNICODE_ON_DISK = 1 << 2
    VP_PERSISTENT_ACLS = 1 << 3
    VP_READ_ONLY_VOLUME = 1 << 9
    VP_FLUSH_AND_PURGE_ON_CLEANUP = 1 << 14
    VP_UM_FILE_CONTEXT_IS_USER_CONTEXT2 = 1 << 16

    _DIR_INFO_SIZE = 104   # FSP_FSCTL_DIR_INFO: UINT16 Size, FILE_INFO @8, 24-byte union
    _DIR_INFO_FILEINFO_OFF = 8

    _CB = ctypes.CFUNCTYPE
    _PFI = ctypes.POINTER(FSP_FSCTL_FILE_INFO)
    # FSP_FILE_SYSTEM_INTERFACE: 33 named slots + Reserved[31] (inc/winfsp/winfsp.h)
    _IFACE_SLOTS = ("GetVolumeInfo", "SetVolumeLabel", "GetSecurityByName", "Create", "Open",
                    "Overwrite", "Cleanup", "Close", "Read", "Write", "Flush", "GetFileInfo",
                    "SetBasicInfo", "SetFileSize", "CanDelete", "Rename", "GetSecurity",
                    "SetSecurity", "ReadDirectory", "ResolveReparsePoints", "GetReparsePoint",
                    "SetReparsePoint", "DeleteReparsePoint", "GetStreamInfo",
                    "GetDirInfoByName", "Control", "SetDelete", "CreateEx", "OverwriteEx",
                    "GetEa", "SetEa", "Obsolete0", "DispatcherStopped")
    _IFACE_LEN = 64
    _PROTOS = {
        "GetVolumeInfo": _CB(_NTSTATUS, _P, ctypes.POINTER(FSP_FSCTL_VOLUME_INFO)),
        "GetSecurityByName": _CB(_NTSTATUS, _P, ctypes.c_wchar_p, ctypes.POINTER(_U32), _P,
                                 ctypes.POINTER(ctypes.c_size_t)),
        "Create": _CB(_NTSTATUS, _P, ctypes.c_wchar_p, _U32, _U32, _U32, _P, _U64,
                      ctypes.POINTER(_P), _PFI),
        "Open": _CB(_NTSTATUS, _P, ctypes.c_wchar_p, _U32, _U32, ctypes.POINTER(_P), _PFI),
        "Overwrite": _CB(_NTSTATUS, _P, _P, _U32, _BOOL8, _U64, _PFI),
        "Cleanup": _CB(None, _P, _P, ctypes.c_wchar_p, _ULONG),
        "Close": _CB(None, _P, _P),
        "Read": _CB(_NTSTATUS, _P, _P, _P, _U64, _ULONG, ctypes.POINTER(_ULONG)),
        "Write": _CB(_NTSTATUS, _P, _P, _P, _U64, _ULONG, _BOOL8, _BOOL8,
                     ctypes.POINTER(_ULONG), _PFI),
        "Flush": _CB(_NTSTATUS, _P, _P, _PFI),
        "GetFileInfo": _CB(_NTSTATUS, _P, _P, _PFI),
        "SetBasicInfo": _CB(_NTSTATUS, _P, _P, _U32, _U64, _U64, _U64, _U64, _PFI),
        "SetFileSize": _CB(_NTSTATUS, _P, _P, _U64, _BOOL8, _PFI),
        "CanDelete": _CB(_NTSTATUS, _P, _P, ctypes.c_wchar_p),
        "Rename": _CB(_NTSTATUS, _P, _P, ctypes.c_wchar_p, ctypes.c_wchar_p, _BOOL8),
        "GetSecurity": _CB(_NTSTATUS, _P, _P, _P, ctypes.POINTER(ctypes.c_size_t)),
        "ReadDirectory": _CB(_NTSTATUS, _P, _P, ctypes.c_wchar_p, ctypes.c_wchar_p, _P,
                             _ULONG, ctypes.POINTER(_ULONG)),
    }

    class FSP_FILE_SYSTEM_INTERFACE(ctypes.Structure):
        _fields_ = [("slots", _P * _IFACE_LEN)]


class WinFspMissing(RuntimeError):
    """WinFsp is not installed (or this Python cannot load it)."""


WINFSP_URL = "https://winfsp.dev/rel/"
_DLL = None


def _dll_name():
    import platform
    m = platform.machine().lower()
    if ctypes.sizeof(ctypes.c_void_p) == 4:
        return "winfsp-x86.dll"
    return "winfsp-a64.dll" if m in ("arm64", "aarch64") else "winfsp-x64.dll"


def load_winfsp():
    """The WinFsp DLL (what FspLoad does: PATH first, then InstallDir from the registry)."""
    global _DLL
    if _DLL is not None:
        return _DLL
    if sys.platform != "win32":
        raise WinFspMissing("WinFsp drives are only available on Windows")
    name = _dll_name()
    candidates = [name]
    try:
        import winreg
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WinFsp", 0,
                                    winreg.KEY_READ | view) as k:
                    d = winreg.QueryValueEx(k, "InstallDir")[0]
                    candidates.append(os.path.join(d, "bin", name))
            except OSError:
                pass
    except ImportError:
        pass
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates.append(os.path.join(pf86, "WinFsp", "bin", name))
    for c in candidates:
        try:
            dll = ctypes.CDLL(c)
            break
        except OSError:
            continue
    else:
        raise WinFspMissing("WinFsp is not installed. Install it from %s (the default "
                            "\"Core\" feature is enough), then try again." % WINFSP_URL)
    P = ctypes.c_void_p
    dll.FspFileSystemCreate.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(FSP_FSCTL_VOLUME_PARAMS),
                                        ctypes.POINTER(FSP_FILE_SYSTEM_INTERFACE),
                                        ctypes.POINTER(P)]
    dll.FspFileSystemCreate.restype = ctypes.c_long
    dll.FspFileSystemSetMountPoint.argtypes = [P, ctypes.c_wchar_p]
    dll.FspFileSystemSetMountPoint.restype = ctypes.c_long
    dll.FspFileSystemStartDispatcher.argtypes = [P, ctypes.c_ulong]
    dll.FspFileSystemStartDispatcher.restype = ctypes.c_long
    for fn in ("FspFileSystemStopDispatcher", "FspFileSystemRemoveMountPoint",
               "FspFileSystemDelete"):
        getattr(dll, fn).argtypes = [P]
        getattr(dll, fn).restype = None
    dll.FspFileSystemAddDirInfo.argtypes = [P, P, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    dll.FspFileSystemAddDirInfo.restype = ctypes.c_ubyte
    _DLL = dll
    return dll


def winfsp_available():
    """(ok, reason) without raising."""
    try:
        load_winfsp()
        return True, ""
    except WinFspMissing as e:
        return False, str(e)


def _signed(status):
    return status - (1 << 32) if status & 0x80000000 else status


def _security_descriptor():
    """Self-relative SD: full access for SYSTEM, Administrators, Everyone."""
    adv = ctypes.windll.advapi32
    psd, size = ctypes.c_void_p(), ctypes.c_ulong()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            ctypes.c_wchar_p("O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;WD)"), 1,
            ctypes.byref(psd), ctypes.byref(size)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(psd, size.value)
    finally:
        ctypes.windll.kernel32.LocalFree(psd)


def free_drive_letters():
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [chr(ord("A") + i) for i in range(25, 2, -1) if not mask & (1 << i)]  # Z: .. D:


class WinFspHost:
    """One WinFsp file system serving a DriveFS on a drive letter."""

    def __init__(self, fs, letter=None, threads=2, log=None, trace=False):
        self.fs = fs
        self.want_letter = letter
        self.threads = threads
        self.log = log or (lambda msg: None)
        self.trace = trace          # log every callback and its NTSTATUS (debugging)
        self.letter = None
        self._fsp = None
        self._iface = None
        self._cbs = []              # keep the ctypes thunks alive

    # -- callbacks --
    def _fill(self, pfi, info):
        fi = pfi[0]
        fi.FileAttributes = info["attr"]
        fi.ReparseTag = 0
        fi.AllocationSize = info["alloc"]
        fi.FileSize = info["size"]
        fi.CreationTime = fi.LastAccessTime = fi.LastWriteTime = fi.ChangeTime = info["time"]
        fi.IndexNumber = 0
        fi.HardLinks = 0
        fi.EaSize = 0

    def _guard(self, what, fn):
        try:
            r = fn()
            return _signed(STATUS_SUCCESS if r is None else r)
        except Exception as e:
            st = nt_status(e)
            if st in (STATUS_INTERNAL_ERROR, STATUS_IO_DEVICE_ERROR, STATUS_INVALID_PARAMETER):
                self.log("%s: %s" % (what, e))
            return _signed(st)

    def _put_sd(self, sd, psize):
        if not psize:
            return None
        need = len(self._sd)
        have = psize[0]
        psize[0] = need
        if have < need:
            return STATUS_BUFFER_OVERFLOW
        if sd:
            ctypes.memmove(sd, self._sd, need)
        return None

    def _make_callbacks(self):
        fs = self.fs

        def get_volume_info(_fs, vi):
            def go():
                v = vi[0]
                v.TotalSize, v.FreeSize = VOLUME_TOTAL, VOLUME_FREE
                v.VolumeLabel = VOLUME_LABEL
                v.VolumeLabelLength = 2 * len(VOLUME_LABEL)
            return self._guard("GetVolumeInfo", go)

        def get_security_by_name(_fs, name, pattr, sd, psize):
            def go():
                attr = fs.get_attributes(name)
                if pattr:
                    pattr[0] = attr
                return self._put_sd(sd, psize)
            return self._guard("GetSecurityByName", go)

        def create(_fs, name, opts, access, attrs, sd, alloc, pctx, pfi):
            def go():
                cid, info = fs.create(name, opts, access)
                pctx[0] = cid
                self._fill(pfi, info)
            return self._guard("Create", go)

        def open_(_fs, name, opts, access, pctx, pfi):
            def go():
                cid, info = fs.open(name, opts, access)
                pctx[0] = cid
                self._fill(pfi, info)
            return self._guard("Open", go)

        def overwrite(_fs, ctx, attrs, replace, alloc, pfi):
            return self._guard("Overwrite", lambda: self._fill(pfi, fs.overwrite(ctx)))

        def cleanup(_fs, ctx, name, flags):
            self._guard("Cleanup", lambda: fs.cleanup(ctx, flags))

        def close(_fs, ctx):
            self._guard("Close", lambda: fs.close(ctx))

        def read(_fs, ctx, buf, offset, length, pdone):
            def go():
                data = fs.read(ctx, offset, length)
                ctypes.memmove(buf, data, len(data))
                pdone[0] = len(data)
            return self._guard("Read", go)

        def write(_fs, ctx, buf, offset, length, to_eof, constrained, pdone, pfi):
            def go():
                n, info = fs.write(ctx, offset, ctypes.string_at(buf, length),
                                   bool(to_eof), bool(constrained))
                pdone[0] = n
                self._fill(pfi, info)
            return self._guard("Write", go)

        def flush(_fs, ctx, pfi):
            def go():
                info = fs.flush(ctx or None)
                if info is not None and pfi:
                    self._fill(pfi, info)
            return self._guard("Flush", go)

        def get_file_info(_fs, ctx, pfi):
            return self._guard("GetFileInfo", lambda: self._fill(pfi, fs.get_file_info(ctx)))

        def set_basic_info(_fs, ctx, attrs, ct, at, wt, cht, pfi):
            # The gateway stores neither attributes nor times: accept and ignore.
            return self._guard("SetBasicInfo", lambda: self._fill(pfi, fs.get_file_info(ctx)))

        def set_file_size(_fs, ctx, size, alloc, pfi):
            return self._guard("SetFileSize",
                               lambda: self._fill(pfi, fs.set_file_size(ctx, size, bool(alloc))))

        def can_delete(_fs, ctx, name):
            return self._guard("CanDelete", lambda: fs.can_delete(ctx))

        def rename(_fs, ctx, name, new_name, replace):
            return self._guard("Rename", lambda: fs.rename(ctx, new_name, bool(replace)))

        def get_security(_fs, ctx, sd, psize):
            return self._guard("GetSecurity", lambda: self._put_sd(sd, psize))

        def read_directory(_fs, ctx, pattern, marker, buf, length, pdone):
            def go():
                pdone[0] = 0
                for name, info in fs.read_directory(ctx, marker):
                    if not self._add_dir_info(name, info, buf, length, pdone):
                        return None             # buffer full: WinFsp asks again from here
                load_winfsp().FspFileSystemAddDirInfo(None, buf, length, pdone)   # end marker
            return self._guard("ReadDirectory", go)

        impl = {"GetVolumeInfo": get_volume_info, "GetSecurityByName": get_security_by_name,
                "Create": create, "Open": open_, "Overwrite": overwrite, "Cleanup": cleanup,
                "Close": close, "Read": read, "Write": write, "Flush": flush,
                "GetFileInfo": get_file_info, "SetBasicInfo": set_basic_info,
                "SetFileSize": set_file_size, "CanDelete": can_delete, "Rename": rename,
                "GetSecurity": get_security, "ReadDirectory": read_directory}
        iface = FSP_FILE_SYSTEM_INTERFACE()
        for slot, fn in impl.items():
            if self.trace and slot not in ("GetVolumeInfo",):
                fn = self._traced(slot, fn)
            thunk = _PROTOS[slot](fn)
            self._cbs.append(thunk)
            iface.slots[_IFACE_SLOTS.index(slot)] = ctypes.cast(thunk, ctypes.c_void_p).value
        return iface

    def _traced(self, slot, fn):
        def show(a):
            if a is None or isinstance(a, (str, int)):
                return repr(a) if not isinstance(a, int) or a < 0x10000 else "%#x" % a
            return "*"                                  # pointer argument
        def wrapper(*args):
            r = fn(*args)
            st = "" if r is None else " -> %#010x" % (r & 0xFFFFFFFF)
            self.log("%s(%s)%s" % (slot, ", ".join(show(a) for a in args[1:]), st))
            return r
        return wrapper

    def _add_dir_info(self, name, info, buf, length, pdone):
        raw = name.encode("utf-16-le")
        size = _DIR_INFO_SIZE + len(raw)
        di = ctypes.create_string_buffer(size)
        ctypes.c_uint16.from_buffer(di, 0).value = size
        fi = FSP_FSCTL_FILE_INFO.from_buffer(di, _DIR_INFO_FILEINFO_OFF)
        self._fill(ctypes.pointer(fi), info)
        ctypes.memmove(ctypes.addressof(di) + _DIR_INFO_SIZE, raw, len(raw))
        return bool(load_winfsp().FspFileSystemAddDirInfo(di, buf, length, pdone))

    # -- lifetime --
    def start(self):
        dll = load_winfsp()
        self._sd = _security_descriptor()
        vp = FSP_FSCTL_VOLUME_PARAMS()
        vp.Version = ctypes.sizeof(FSP_FSCTL_VOLUME_PARAMS)
        vp.SectorSize = 512
        vp.SectorsPerAllocationUnit = ALLOC_UNIT // 512
        vp.MaxComponentLength = 255
        vp.VolumeCreationTime = self.fs.time
        vp.VolumeSerialNumber = 0x5CC10000 | (int(time.time()) & 0xFFFF)
        vp.FileInfoTimeout = 1000
        vp.Flags = (VP_CASE_PRESERVED_NAMES | VP_UNICODE_ON_DISK | VP_PERSISTENT_ACLS
                    | VP_FLUSH_AND_PURGE_ON_CLEANUP | VP_UM_FILE_CONTEXT_IS_USER_CONTEXT2
                    | (VP_READ_ONLY_VOLUME if self.fs.read_only else 0))
        vp.FileSystemName = "SnowCut"
        self._iface = self._make_callbacks()
        fsp = ctypes.c_void_p()
        st = dll.FspFileSystemCreate("WinFsp.Disk", ctypes.byref(vp), ctypes.byref(self._iface),
                                     ctypes.byref(fsp))
        if st != 0:
            raise OSError("WinFsp could not create the file system (NTSTATUS %#010x)"
                          % (st & 0xFFFFFFFF))
        self._fsp = fsp
        try:
            letters = [self.want_letter.upper()[:1]] if self.want_letter else free_drive_letters()
            if not letters:
                raise OSError("no free drive letter")
            st = dll.FspFileSystemSetMountPoint(fsp, letters[0] + ":")
            if st != 0:
                raise OSError("could not mount on %s: (NTSTATUS %#010x)"
                              % (letters[0], st & 0xFFFFFFFF))
            self.letter = letters[0]
            st = dll.FspFileSystemStartDispatcher(fsp, self.threads)
            if st != 0:
                raise OSError("WinFsp dispatcher failed (NTSTATUS %#010x)" % (st & 0xFFFFFFFF))
        except BaseException:
            self.stop()
            raise
        return self.letter

    def stop(self):
        fsp, self._fsp = self._fsp, None
        if fsp is None:
            return
        dll = load_winfsp()
        dll.FspFileSystemStopDispatcher(fsp)
        dll.FspFileSystemRemoveMountPoint(fsp)
        dll.FspFileSystemDelete(fsp)
        self.letter = None


# -- DriveMount: the one-click mount -----------------------------------------------------------
BUSY_TEXT = "Mounted on PC"     # <= 14 bytes: the machine's pop-up while the drive is mounted
HEARTBEAT_S = 3.0               # inside the busy pop-up's 15 s expiry
_LOST_AFTER = 2                 # failed heartbeats in a row


def _demo_client():
    """Simulator: an in-memory drive (same firmware limits applied by DriveFS)."""
    c = sfs.MockC4Client()
    h = c.open("A:\\README.TXT", sfs.O_WRITE | sfs.O_CREATE)
    c.write_at(h, 0, b"Simulated USB drive (no machine connected).\r\n")
    c.close(h)
    c.mkdir("A:\\SCAN")
    return c


class DriveMount:
    """Mount the machine's USB drive as a Windows drive. stop() unmounts it.

    While mounted this object owns the machine's USB handle (Windows allows one), so the app
    must not start scans / cuts / feeds until stop(). on_error(path, exc): a file could not be
    saved (from a WinFsp thread). on_lost(message): the machine stopped answering (from the
    heartbeat thread), and stop() should be called in response.
    """

    def __init__(self, mock=False, drive=sfs.DEFAULT_DRIVE, letter=None, read_only=False,
                 on_error=None, on_lost=None, log=None, trace=False):
        self.trace = trace
        self.mock = mock
        self.drive = drive.upper()[:1]
        self.want_letter = letter
        self.read_only = read_only
        self.on_error = on_error or (lambda path, exc: None)
        self.on_lost = on_lost or (lambda msg: None)
        self.log = log or (lambda msg: None)
        self.letter = None
        self.fs = None
        self._dev = None
        self._client = None
        self._host = None
        self._we_mounted = False
        self._busy_shown = False
        self._stop = threading.Event()
        self._hb = None

    @property
    def mounted(self):
        return self.letter is not None

    def start(self):
        """Blocking (device-side mount can take seconds). -> drive letter, e.g. "Z"."""
        load_winfsp()                       # fail before touching the machine
        try:
            if self.mock:
                self._client = _demo_client()
            else:
                import memdump
                self._dev = memdump._dev()
                sfs.from_usb_closeall(self._dev)
                self._client = sfs.C4Client(self._dev)
                if not self._client.mount_state(self.drive)[0]:
                    self._client.mount(self.drive)
                    self._we_mounted = True
                self._show_busy(BUSY_TEXT)
            try:
                refused = self._client.refused_ops()
            except Exception as e:
                self.log("could not ask the firmware which file ops it has: %s" % e)
                refused = sfs.REFUSED_OPS_OLD
            self.fs = DriveFS(self._client, self.drive, read_only=self.read_only,
                              on_error=self.on_error, refused=refused)
            self._host = WinFspHost(self.fs, self.want_letter, log=self.log, trace=self.trace)
            self.letter = self._host.start()
        except BaseException:
            self.stop()
            raise
        if not self.mock:
            self._stop.clear()
            self._hb = threading.Thread(target=self._heartbeat, daemon=True,
                                        name="snowcut-drive-heartbeat")
            self._hb.start()
        return self.letter

    def pending_files(self):
        return self.fs.pending_files() if self.fs is not None else []

    def _show_busy(self, msg):
        try:
            import busy as _busy
            self._client.send_vendor_cdb(_busy.busy_cdb(msg))
            self._busy_shown = msg is not None
        except Exception as e:
            self.log("busy pop-up: %s" % e)     # cosmetic (older firmware has none)

    def _heartbeat(self):
        """Refreshes the busy pop-up (any vendor command does) and notices a lost machine."""
        fails = 0
        while not self._stop.wait(HEARTBEAT_S):
            try:
                mounted = self._client.mount_state(self.drive)[0]
            except Exception as e:
                fails += 1
                if fails >= _LOST_AFTER:
                    self.on_lost("The machine stopped answering (%s)" % e)
                    return
                continue
            fails = 0
            if not mounted:
                self.on_lost("The machine unmounted its USB drive")
                return

    def stop(self):
        """Unmount everything. Safe to call twice / after a failed start. -> files not saved."""
        self._stop.set()
        if self._hb is not None and self._hb is not threading.current_thread():
            self._hb.join(timeout=HEARTBEAT_S + 6)
        self._hb = None
        failed = []
        if self._host is not None:
            with contextlib.suppress(Exception):
                self._host.stop()           # no new Windows requests from here on
            self._host = None
        if self.fs is not None:
            failed = self.fs.shutdown()
            self.fs = None
        if self._client is not None and not self.mock:
            with contextlib.suppress(Exception):
                self._client.close_all()
            if self._we_mounted:
                try:
                    self._client.unmount(self.drive)
                except Exception as e:
                    self.log("unmount %s: on the machine: %s" % (self.drive, e))
            if self._busy_shown:
                self._show_busy(None)
        self._we_mounted = False
        self._client = None
        if self._dev is not None:
            import memdump
            memdump.close_dev(self._dev)
            self._dev = None
        self.letter = None
        return failed


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Mount the machine's USB drive as a Windows drive "
                                             "(needs WinFsp). Ctrl+C unmounts.")
    ap.add_argument("letter", nargs="?", help="drive letter (default: first free from Z:)")
    ap.add_argument("--mock", action="store_true", help="in-memory drive, no machine")
    ap.add_argument("--read-only", action="store_true")
    ap.add_argument("--trace", action="store_true", help="log every WinFsp callback")
    ap.add_argument("--stop-file", help="unmount when this file appears (unattended runs)")
    a = ap.parse_args(argv)
    lost = threading.Event()

    def on_lost(msg):
        print(msg, file=sys.stderr)
        lost.set()

    m = DriveMount(mock=a.mock, letter=a.letter, read_only=a.read_only,
                   on_error=lambda p, e: print("could not save %s: %s" % (p, e), file=sys.stderr),
                   on_lost=on_lost, log=lambda msg: print(msg, file=sys.stderr, flush=True),
                   trace=a.trace)
    try:
        letter = m.start()
    except Exception as e:
        print("mount failed: %s" % e, file=sys.stderr)
        return 2
    print("mounted on %s: (Ctrl+C to unmount)" % letter, flush=True)
    try:
        while not lost.wait(0.5):
            if a.stop_file and os.path.exists(a.stop_file):
                break
    except KeyboardInterrupt:
        pass
    failed = m.stop()
    print("unmounted" + (". NOT saved: %s" % ", ".join(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
