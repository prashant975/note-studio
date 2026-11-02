"""DPAPI credential protection, bound to the current Windows account."""
import base64
import ctypes
import os


def transform(value: str, decrypt: bool = False) -> str:
    if os.name != "nt":
        raise ValueError("Windows credentials must be authenticated on this computer.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    raw = base64.b64decode(value, validate=True) if decrypt else value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    method = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError("Windows could not protect or read this sign-in.")
    try:
        result = ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)
    return result.decode("utf-8") if decrypt else base64.b64encode(result).decode("ascii")
