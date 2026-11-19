import ctypes
import json
import unittest
from unittest.mock import MagicMock, patch

from ncm_checker.api import ApiConnectionError, ApiError, ApiSessionExpired, NcmApi
from ncm_checker.session import SessionStore, SessionStoreError


class SessionStoreTest(unittest.TestCase):
    def test_store_is_optional_off_macos(self):
        store = SessionStore(3100)
        with patch("ncm_checker.session.sys.platform", "linux"):
            self.assertFalse(store.available())
            self.assertIsNone(store.load())
            store.forget()
            with self.assertRaises(SessionStoreError):
                store.save({"MUSIC_U": "fixture"})

    def test_native_keychain_add_read_update_delete(self):
        security = MagicMock()
        foundation = MagicMock()
        entries = {}
        buffers = {}
        current_item = ctypes.c_void_p(1)

        def find(keychain, service_len, service, account_len, account, size, data, item):
            self.assertIsNone(keychain)
            self.assertEqual(service_len, len(service))
            self.assertEqual(account_len, len(account))
            self.assertEqual(service, b"ncm_checker.netease_session")
            if account not in entries:
                return -25300
            if size is not None:
                value = entries[account]
                buffers[account] = ctypes.create_string_buffer(value)
                ctypes.cast(size, ctypes.POINTER(ctypes.c_uint32))[0] = len(value)
                ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffers[account])
            if item is not None:
                ctypes.cast(item, ctypes.POINTER(ctypes.c_void_p))[0] = current_item.value
            return 0

        def add(_keychain, _service_len, _service, _account_len, account, size, value, _item):
            self.assertEqual(size, len(value))
            if account in entries:
                return -25299
            entries[account] = value
            return 0

        def update(item, _attributes, size, value):
            self.assertEqual(item.value, current_item.value)
            self.assertEqual(size, len(value))
            entries[b"127.0.0.1:3100"] = value
            return 0

        def delete(item):
            self.assertEqual(item.value, current_item.value)
            entries.pop(b"127.0.0.1:3100")
            return 0

        security.SecKeychainFindGenericPassword.side_effect = find
        security.SecKeychainAddGenericPassword.side_effect = add
        security.SecKeychainItemModifyAttributesAndData.side_effect = update
        security.SecKeychainItemDelete.side_effect = delete

        first = {"MUSIC_U": "fixture1", "__csrf": "值"}
        second = {"MUSIC_U": "fixture2"}
        with (patch("ncm_checker.session.sys.platform", "darwin"),
              patch("ncm_checker.session.ctypes.CDLL", side_effect=[security, foundation] * 9)):
            store = SessionStore(3100)
            self.assertIsNone(store.load())
            store.save(first)
            self.assertEqual(SessionStore(3100).load(), first)
            self.assertIsNone(SessionStore(3200).load())
            store.save(second)
            self.assertEqual(store.load(), second)
            store.forget()
            store.forget()
            self.assertIsNone(store.load())
        self.assertEqual(security.SecKeychainItemFreeContent.call_count, 2)
        self.assertEqual(foundation.CFRelease.call_count, 2)
        self.assertEqual(entries, {})

    def test_corrupt_record_and_access_error_remain_distinct_from_missing(self):
        store = SessionStore(3100)
        keychain = MagicMock()
        keychain.read.return_value = b"not-the-login-data"
        with (patch.object(store, "available", return_value=True),
              patch.object(store, "_keychain", return_value=keychain)):
            with self.assertRaisesRegex(SessionStoreError, "无法读取") as error:
                store.load()
            self.assertNotIn("not-the-login-data", str(error.exception))
            keychain.read.side_effect = SessionStoreError("钥匙串拒绝访问")
            with self.assertRaisesRegex(SessionStoreError, "钥匙串拒绝访问"):
                store.load()
            with self.assertRaises(SessionStoreError):
                store.save({"MUSIC_U": 5})


class LoginStatusTest(unittest.TestCase):
    def setUp(self):
        self.api = NcmApi(3100)
        self.api.cookie = {"MUSIC_U": "fixture"}

    def test_wire_expiry_is_reported_separately_from_network_error(self):
        with patch("ncm_checker.api.http.client.HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = json.dumps({"code": 301}).encode()
            with self.assertRaises(ApiSessionExpired):
                self.api.login_status()
            connection.return_value.getresponse.side_effect = ConnectionRefusedError()
            with self.assertRaises(ApiConnectionError):
                self.api.login_status()

    def test_empty_profile_is_expired_but_malformed_response_is_not(self):
        with patch.object(self.api, "request", return_value={"data": {"profile": None}}):
            with self.assertRaises(ApiSessionExpired):
                self.api.login_status()
        with patch.object(self.api, "request", return_value={"data": []}):
            with self.assertRaises(ApiError) as error:
                self.api.login_status()
            self.assertNotIsInstance(error.exception, ApiSessionExpired)
        with patch.object(self.api, "request", return_value={"data": {"profile": {"userId": 99}}}):
            self.assertEqual(self.api.login_status()["userId"], 99)
