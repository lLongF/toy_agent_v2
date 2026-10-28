"""Windows 当前用户 DPAPI 加密的本地凭据；配置只保存不透明引用。"""
import base64
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from uuid import uuid4


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, decrypt=False):
    if os.name != "nt":
        raise ValueError("本机加密 Token 存储当前仅支持 Windows；其他平台请使用环境变量或临时输入")
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
    else:
        fn = crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError("本机 Token 加密存储不可用，或当前 Windows 用户无法解密")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


def save_token(token, root):
    token = str(token).strip()
    if not token:
        raise ValueError("请输入玩具 Token")
    encrypted = _crypt(token.encode("utf-8"))
    directory = Path(root).resolve() / ".runtime" / "credentials"
    directory.mkdir(parents=True, exist_ok=True)
    identity = uuid4().hex
    with (directory / (identity + ".dpapi")).open("xb") as stream:
        stream.write(encrypted)
    return "local:" + identity


def read_token(reference, root):
    import re
    if not re.fullmatch(r"local:[a-f0-9]{32}", reference):
        raise ValueError("无效的本机凭据引用")
    path = Path(root).resolve() / ".runtime" / "credentials" / (reference[6:] + ".dpapi")
    try:
        return _crypt(path.read_bytes(), decrypt=True).decode("utf-8")
    except OSError:
        raise ValueError("本机保存的 Token 不存在，请在编辑玩具中重新填写") from None
