import os
import re
import shutil
import struct

INDEX_MAGIC = 0xC103CAC3
HEADER_SIZE = 368
BLOCK_SIZES = {1: 36, 2: 256, 3: 1024, 4: 4096, 5: 8, 6: 104, 7: 48}
KEY_PREFIX_RE = re.compile(r"^\d+/\d+/")


def url_of_key(key):
    """Strip Chromium cache key prefixes and return the bare URL.

    Handles plain keys ("1/0/https://...") and split-cache / double-keyed
    entries ("1/0/_dk_https://site https://site https://host/path").
    """
    key = KEY_PREFIX_RE.sub("", key)
    if key.startswith("_dk_") or " " in key:
        key = key.rsplit(" ", 1)[-1]
    return key


class CacheError(Exception):
    pass


def _get16bits(data, i):
    return data[i] | (data[i + 1] << 8)


def super_fast_hash(data):
    length = len(data)
    h = length
    rem = length & 3
    n = length >> 2
    i = 0
    mask = 0xFFFFFFFF
    for _ in range(n):
        h = (h + _get16bits(data, i)) & mask
        tmp = ((_get16bits(data, i + 2) << 11) ^ h) & mask
        h = (((h << 16) & mask) ^ tmp) & mask
        h = (h + (h >> 11)) & mask
        i += 4
    if rem == 3:
        h = (h + _get16bits(data, i)) & mask
        h ^= (h << 16) & mask
        h ^= (data[i + 2] << 18) & mask
        h = (h + (h >> 11)) & mask
    elif rem == 2:
        h = (h + _get16bits(data, i)) & mask
        h ^= (h << 11) & mask
        h = (h + (h >> 17)) & mask
    elif rem == 1:
        h = (h + data[i]) & mask
        h ^= (h << 10) & mask
        h = (h + (h >> 1)) & mask
    h &= mask
    h ^= (h << 3) & mask
    h = (h + (h >> 5)) & mask
    h ^= (h << 4) & mask
    h = (h + (h >> 17)) & mask
    h ^= (h << 25) & mask
    h = (h + (h >> 6)) & mask
    return h & mask


def _addr_info(addr):
    if addr == 0:
        return None
    ftype = (addr >> 28) & 0x7
    if ftype == 0:
        return ("file", addr & 0x0FFFFFFF, 0, 1, 0)
    return (
        "block",
        (addr & 0x00FF0000) >> 16,
        addr & 0xFFFF,
        ((addr >> 24) & 0x3) + 1,
        ftype,
    )


class CacheReader:
    def __init__(self, cache_dir):
        self.cache_dir = os.path.abspath(cache_dir)
        self.index_path = os.path.join(self.cache_dir, "index")
        self._handles = {}
        with open(self.index_path, "rb") as f:
            self.index = f.read()
        if len(self.index) < HEADER_SIZE + 4:
            raise CacheError("cache index is too small")
        magic, = struct.unpack_from("<I", self.index, 0)
        if magic != INDEX_MAGIC:
            raise CacheError("unsupported cache index magic: 0x%08x" % magic)
        self.table_size, = struct.unpack_from("<I", self.index, 28)
        if self.table_size <= 0 or self.table_size & (self.table_size - 1):
            raise CacheError("invalid cache table size: %d" % self.table_size)
        if HEADER_SIZE + self.table_size * 4 > len(self.index):
            raise CacheError("truncated cache index")

    def close(self):
        for handle in self._handles.values():
            try:
                handle.close()
            except OSError:
                pass
        self._handles.clear()

    def _handle(self, fnum):
        handle = self._handles.get(fnum)
        if handle is None:
            path = os.path.join(self.cache_dir, "data_%d" % fnum)
            handle = open(path, "rb")
            self._handles[fnum] = handle
        return handle

    def _read_at(self, addr, size):
        info = _addr_info(addr)
        if not info:
            return None
        kind, fnum, block, nblocks, ftype = info
        if kind == "file":
            path = os.path.join(self.cache_dir, "f_%06x" % fnum)
            if not os.path.exists(path):
                return None
            with open(path, "rb") as f:
                return f.read(size)
        block_size = BLOCK_SIZES.get(ftype)
        if block_size is None:
            return None
        handle = self._handle(fnum)
        handle.seek(8192 + block * block_size)
        return handle.read(size)

    def _parse_entry(self, addr):
        info = _addr_info(addr)
        if not info or info[0] != "block":
            return None
        kind, fnum, block, nblocks, ftype = info
        block_size = BLOCK_SIZES.get(ftype)
        if block_size is None:
            return None
        handle = self._handle(fnum)
        handle.seek(8192 + block * block_size)
        raw = handle.read(block_size * nblocks)
        if len(raw) < 96:
            return None
        h, nxt, rank, reuse, refetch, state = struct.unpack_from("<6I", raw, 0)
        ctime, = struct.unpack_from("<q", raw, 24)
        key_len, long_key = struct.unpack_from("<2I", raw, 32)
        data_size = struct.unpack_from("<4I", raw, 40)
        data_addr = struct.unpack_from("<4I", raw, 56)
        flags, = struct.unpack_from("<I", raw, 72)
        self_hash, = struct.unpack_from("<I", raw, 92)
        if key_len == 0 or key_len > 1_000_000:
            return None
        if long_key == 0 and 96 + key_len <= len(raw):
            key = raw[96 : 96 + key_len]
        else:
            key = self._read_at(long_key, key_len)
        if not key:
            return None
        return {
            "addr": addr,
            "hash": h,
            "next": nxt,
            "state": state,
            "ctime": ctime,
            "key_len": key_len,
            "long_key": long_key,
            "data_size": data_size,
            "data_addr": data_addr,
            "flags": flags,
            "self_hash": self_hash,
            "key": key,
        }

    def iter_entries(self):
        seen = set()
        for bucket in range(self.table_size):
            addr, = struct.unpack_from(
                "<I", self.index, HEADER_SIZE + bucket * 4
            )
            chain = 0
            while addr and chain < 100:
                if addr in seen:
                    break
                seen.add(addr)
                entry = self._parse_entry(addr)
                if not entry:
                    break
                yield entry
                addr = entry["next"]
                chain += 1

    def _find_key(self, key):
        h = super_fast_hash(key)
        bucket = h & (self.table_size - 1)
        addr, = struct.unpack_from(
            "<I", self.index, HEADER_SIZE + bucket * 4
        )
        chain = 0
        while addr and chain < 100:
            entry = self._parse_entry(addr)
            if not entry:
                return None
            if entry["hash"] == h and entry["key"] == key:
                return entry
            addr = entry["next"]
            chain += 1
        return None

    def find(self, url):
        entry = self._find_key(("1/0/" + url).encode())
        if entry:
            return entry
        for entry in self.iter_entries():
            if url_of_key(entry["key"].decode("utf-8", "replace")) == url:
                return entry
        return None

    def url_entries(self, prefix=None):
        result = {}
        for entry in self.iter_entries():
            try:
                key = entry["key"].decode("utf-8")
            except UnicodeDecodeError:
                continue
            url = url_of_key(key)
            if not url.startswith(("http://", "https://")):
                continue
            if prefix and not url.startswith(prefix):
                continue
            old = result.get(url)
            if old is None or (old["state"] != 0 and entry["state"] == 0):
                result[url] = entry
            elif old["state"] == entry["state"] == 0 and entry["ctime"] > old["ctime"]:
                result[url] = entry
        return result

    def body_path(self, entry):
        if entry["state"] != 0:
            return None
        size = entry["data_size"][1]
        if size <= 0:
            return None
        info = _addr_info(entry["data_addr"][1])
        if not info or info[0] != "file":
            return None
        path = os.path.join(self.cache_dir, "f_%06x" % info[1])
        if not os.path.exists(path):
            return None
        if os.path.getsize(path) != size:
            return None
        return path

    def read_body(self, entry):
        if entry["state"] != 0:
            return None
        size = entry["data_size"][1]
        if size <= 0:
            return None
        info = _addr_info(entry["data_addr"][1])
        if not info:
            return None
        if info[0] == "file":
            path = os.path.join(self.cache_dir, "f_%06x" % info[1])
            try:
                with open(path, "rb") as f:
                    data = f.read(size)
            except OSError:
                return None
            return data if len(data) == size else None
        block_size = BLOCK_SIZES.get(info[4])
        if block_size is None or block_size * info[3] < size:
            return None
        try:
            handle = self._handle(info[1])
            handle.seek(8192 + info[2] * block_size)
            data = handle.read(size)
        except OSError:
            return None
        return data if len(data) == size else None

    def copy_body(self, entry, dest):
        src = self.body_path(entry)
        size = entry["data_size"][1]
        if src is None and size <= 0:
            return False
        tmp = dest + ".cache.part"
        try:
            if src:
                shutil.copyfile(src, tmp)
            else:
                data = self.read_body(entry)
                if data is None:
                    return False
                with open(tmp, "wb") as f:
                    f.write(data)
            if os.path.getsize(tmp) != size:
                return False
            os.replace(tmp, dest)
            return True
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
