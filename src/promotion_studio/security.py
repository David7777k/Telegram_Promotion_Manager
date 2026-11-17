"""Windows user-bound encryption. No plaintext fallback."""
import ctypes
import os
from ctypes import wintypes


class SecretError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class DPAPI:
    _UI_FORBIDDEN = 1

    def __init__(self):
        if os.name != "nt":
            raise SecretError("DPAPI требует Windows")
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._protect = crypt32.CryptProtectData
        self._unprotect = crypt32.CryptUnprotectData
        blob = ctypes.POINTER(_Blob)
        shared = [blob, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob]
        self._protect.argtypes = [blob, wintypes.LPCWSTR, *shared]
        self._unprotect.argtypes = [blob, ctypes.c_void_p, *shared]
        self._protect.restype = self._unprotect.restype = wintypes.BOOL
        self._free = kernel32.LocalFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = ctypes.c_void_p

    def protect(self, plaintext: bytes) -> bytes:
        return self._transform(self._protect, plaintext)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self._transform(self._unprotect, ciphertext)

    def _transform(self, operation, data):
        source = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        incoming = _Blob(len(data), source)
        outgoing = _Blob()
        try:
            if not operation(ctypes.byref(incoming), None, None, None, None,
                             self._UI_FORBIDDEN, ctypes.byref(outgoing)):
                error = ctypes.get_last_error()
                raise SecretError(f"Секрет недоступен в текущем профиле Windows (DPAPI {error})")
            return ctypes.string_at(outgoing.data, outgoing.size)
        finally:
            ctypes.memset(source, 0, len(data))
            if outgoing.data:
                ctypes.memset(outgoing.data, 0, outgoing.size)
                self._free(outgoing.data)


class SecretStore:
    """Store and retrieve encrypted account credentials on the DB owner's thread."""

    _KINDS = frozenset({"bot_token", "api_hash", "mtproto_session"})

    def __init__(self, connection, protector):
        self._connection = connection
        self._protector = protector

    def _purpose(self, account_id, kind):
        if kind not in self._KINDS or not account_id or "\0" in account_id:
            raise ValueError("Invalid credential scope")
        return f"promotion-studio\0{account_id}\0{kind}\0".encode("utf-8")

    def put(self, account_id, kind, value: bytes):
        purpose = self._purpose(account_id, kind)
        if not value:
            raise ValueError("Empty credential")
        cipher = self._protector.protect(purpose + value)
        self._connection.execute(
            """INSERT INTO account_secrets VALUES (?, ?, ?, 1)
               ON CONFLICT(account_id,secret_kind)
               DO UPDATE SET ciphertext=excluded.ciphertext""",
            (account_id, kind, cipher),
        )

    def get(self, account_id, kind):
        purpose = self._purpose(account_id, kind)
        row = self._connection.execute(
            "SELECT ciphertext FROM account_secrets WHERE account_id=? AND secret_kind=?",
            (account_id, kind),
        ).fetchone()
        if row is None:
            return None
        plain = self._protector.unprotect(row[0])
        if not plain.startswith(purpose):
            raise SecretError("Назначение сохранённого секрета не совпадает")
        return plain[len(purpose):]

    def delete(self, account_id, kind):
        self._purpose(account_id, kind)
        self._connection.execute(
            "DELETE FROM account_secrets WHERE account_id=? AND secret_kind=?", (account_id, kind)
        )

