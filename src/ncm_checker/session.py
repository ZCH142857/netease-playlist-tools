from __future__ import annotations

import ctypes
import json
import sys


class SessionStoreError(RuntimeError):
    pass


_SERVICE = b"ncm_checker.netease_session"
_ITEM_NOT_FOUND = -25300
_DUPLICATE_ITEM = -25299
_VOID_P = ctypes.c_void_p
_CHAR_P = ctypes.c_char_p
_UINT32 = ctypes.c_uint32
_STATUS = ctypes.c_int32


def _check_status(status: int) -> None:
    if status:
        raise SessionStoreError(f"macOS 钥匙串操作未完成（OSStatus {status}）")


class _DarwinKeychain:
    def __init__(self, account: bytes):
        try:
            security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
            foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self.find = security.SecKeychainFindGenericPassword
            self.add = security.SecKeychainAddGenericPassword
            self.modify = security.SecKeychainItemModifyAttributesAndData
            self.delete = security.SecKeychainItemDelete
            self.free_content = security.SecKeychainItemFreeContent
            self.release = foundation.CFRelease
        except (OSError, AttributeError):
            raise SessionStoreError("无法连接 macOS 钥匙串") from None

        search_types = [_VOID_P, _UINT32, _CHAR_P, _UINT32, _CHAR_P]
        self.find.argtypes = search_types + [
            ctypes.POINTER(_UINT32), ctypes.POINTER(_VOID_P), ctypes.POINTER(_VOID_P),
        ]
        self.find.restype = _STATUS
        self.add.argtypes = search_types + [_UINT32, _CHAR_P, ctypes.POINTER(_VOID_P)]
        self.add.restype = _STATUS
        self.modify.argtypes = [_VOID_P, _VOID_P, _UINT32, _CHAR_P]
        self.modify.restype = _STATUS
        self.delete.argtypes = [_VOID_P]
        self.delete.restype = _STATUS
        self.free_content.argtypes = [_VOID_P, _VOID_P]
        self.free_content.restype = _STATUS
        self.release.argtypes = [_VOID_P]
        self.release.restype = None
        self.search_args = (None, len(_SERVICE), _SERVICE, len(account), account)

    def read(self) -> bytes | None:
        size = _UINT32()
        data = _VOID_P()
        status = self.find(*self.search_args, ctypes.byref(size), ctypes.byref(data), None)
        if status == _ITEM_NOT_FOUND:
            return None
        _check_status(status)
        try:
            return ctypes.string_at(data, size.value)
        finally:
            self.free_content(None, data)

    def write(self, value: bytes) -> None:
        status = self.add(*self.search_args, len(value), value, None)
        if status != _DUPLICATE_ITEM:
            _check_status(status)
            return
        item = _VOID_P()
        _check_status(self.find(*self.search_args, None, None, ctypes.byref(item)))
        try:
            _check_status(self.modify(item, None, len(value), value))
        finally:
            self.release(item)

    def remove(self) -> None:
        item = _VOID_P()
        status = self.find(*self.search_args, None, None, ctypes.byref(item))
        if status == _ITEM_NOT_FOUND:
            return
        _check_status(status)
        try:
            status = self.delete(item)
            if status != _ITEM_NOT_FOUND:
                _check_status(status)
        finally:
            self.release(item)


class SessionStore:
    """Keep one authenticated session for each local API port in macOS Keychain."""

    def __init__(self, port: int):
        self.account = f"127.0.0.1:{port}".encode("ascii")

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin"

    def _keychain(self) -> _DarwinKeychain:
        if not self.available():
            raise SessionStoreError("登录记忆需要 macOS 钥匙串")
        return _DarwinKeychain(self.account)

    def load(self) -> dict[str, str] | None:
        if not self.available():
            return None
        raw = self._keychain().read()
        if raw is None:
            return None
        try:
            cookie = json.loads(raw)
        except (UnicodeError, ValueError):
            raise SessionStoreError("钥匙串中的登录记录无法读取，请重新登录") from None
        if not _valid_cookie(cookie):
            raise SessionStoreError("钥匙串中的登录记录不完整，请重新登录")
        return cookie

    def save(self, cookie: dict[str, str]) -> None:
        if not _valid_cookie(cookie):
            raise SessionStoreError("当前登录记录不完整，无法保存")
        self._keychain().write(json.dumps(cookie, ensure_ascii=True, separators=(",", ":")).encode("ascii"))

    def forget(self) -> None:
        if self.available():
            self._keychain().remove()


def _valid_cookie(cookie: object) -> bool:
    return (
        isinstance(cookie, dict)
        and bool(cookie.get("MUSIC_U"))
        and all(isinstance(key, str) and isinstance(value, str) for key, value in cookie.items())
    )
