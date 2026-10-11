"""仅使用当前 Windows 账户 DPAPI 的本地连接保存；不落盘明文。"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile


MAGIC = b'Paper2Video-DPAPI-v1\n'
MAX_BYTES = 1048576


class DataBlob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def crypt(data, decrypt=False):
    if os.name != 'nt':
        raise OSError('本地密钥保存当前仅支持 Windows 账户加密')
    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = DataBlob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    free = ctypes.WinDLL('kernel32', use_last_error=True).LocalFree
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p if decrypt else ctypes.c_wchar_p,
                        ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                        wintypes.DWORD, ctypes.POINTER(DataBlob)]
    function.restype = wintypes.BOOL
    try:
        # UI_FORBIDDEN；不使用 LOCAL_MACHINE，避免放宽到本机其他账户。
        if not function(ctypes.byref(source), None if decrypt else 'Paper2Video model connections',
                        None, None, None, 1, ctypes.byref(output)):
            raise OSError('Windows 账户加密操作失败，不能保存或恢复本地密钥')
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if output.pbData:
            ctypes.memset(output.pbData, 0, output.cbData)
            free(output.pbData)


class CredentialFile:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return []
        if self.path.stat().st_size > MAX_BYTES:
            raise ValueError('本地连接文件体积异常')
        encrypted = self.path.read_bytes()
        if not encrypted.startswith(MAGIC):
            raise ValueError('本地连接文件格式异常')
        payload = json.loads(crypt(encrypted[len(MAGIC):], decrypt=True))
        if not isinstance(payload, dict) or set(payload) != {'version', 'connections'} or payload['version'] != 1:
            raise ValueError('本地连接文件版本异常')
        if not isinstance(payload['connections'], list) or len(payload['connections']) > 100:
            raise ValueError('本地连接记录异常')
        return payload['connections']

    def save(self, records):
        if len(records) > 100:
            raise ValueError('本地保存连接数量超过100个')
        if not records:
            self.path.unlink(missing_ok=True)
            return
        plaintext = json.dumps({'version': 1, 'connections': records}, ensure_ascii=False).encode('utf-8')
        if len(plaintext) > MAX_BYTES // 2:
            raise ValueError('本地连接保存体积超限')
        encrypted = MAGIC + crypt(plaintext)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            # 临时文件也只写密文；替换失败时保留原文件。
            with tempfile.NamedTemporaryFile(dir=self.path.parent, suffix='.dpapi.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
