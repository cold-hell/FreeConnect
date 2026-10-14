"""Интеграционная проверка: наш прокси реально доносит трафик до Telegram.

Тест поднимает настоящий прокси (ядро tg-ws-proxy), подключается к нему как клиент
Telegram — формирует obfuscated2-init с секретом, отправляет MTProto-запрос `req_pq_multi`
и ждёт ответ `resPQ` от сервера Telegram. Если ответ разобрался — значит работает вся
цепочка: handshake по секрету -> определение дата-центра -> WebSocket к веб-входу
Telegram -> перешифровка -> обратный путь.

В обычном прогоне тест пропускается (нужна сеть): включить переменной окружения
    FREECONNECT_TG_NET=1
"""
from __future__ import annotations

import hashlib
import os
import socket
import struct
import time
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from freeconnect import tgproxy

NET = os.environ.get("FREECONNECT_TG_NET") == "1"

PROTO_TAG_ABRIDGED = b"\xef\xef\xef\xef"
RESERVED_STARTS = {b"\x48\x45\x41\x44", b"\x50\x4f\x53\x54", b"\x47\x45\x54\x20",
                   b"\xee\xee\xee\xee", b"\xdd\xdd\xdd\xdd", b"\x16\x03\x01\x02",
                   b"\x4f\x50\x54\x49"}
REQ_PQ_MULTI = bytes.fromhex("f18e7ebe")   # req_pq_multi#be7e8ef1
RES_PQ = 0x05162463                        # resPQ#05162463


def _ctr(key: bytes, iv: bytes, skip: int = 0):
    enc = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    if skip:
        enc.update(b"\x00" * skip)
    return enc


def build_init(secret: bytes, dc: int = 2):
    """Готовит 64-байтный obfuscated2-init так, как его ждёт прокси, и возвращает
    (init, ключ, iv) — ключ с iv нужны для шифрования последующего потока."""
    while True:
        rnd = bytearray(os.urandom(64))
        if rnd[0] == 0xEF:
            continue
        if bytes(rnd[:4]) in RESERVED_STARTS:
            continue
        if bytes(rnd[4:8]) == b"\x00\x00\x00\x00":
            continue
        break
    prekey, iv = bytes(rnd[8:40]), bytes(rnd[40:56])
    key = hashlib.sha256(prekey + secret).digest()
    keystream = _ctr(key, iv).update(bytes(rnd))
    ks_tail = bytes(keystream[i] ^ rnd[i] for i in range(56, 64))
    tail_plain = PROTO_TAG_ABRIDGED + struct.pack("<h", dc) + os.urandom(2)
    tail = bytes(tail_plain[i] ^ ks_tail[i] for i in range(8))
    return bytes(rnd[:56]) + tail, key, iv


def client_ciphers(prekey: bytes, iv: bytes, secret: bytes):
    """Шифр исходящего потока (прокси расшифровывает его) и входящего (прокси шифрует)."""
    out = _ctr(hashlib.sha256(prekey + secret).digest(), iv, skip=64)
    reversed_pair = (prekey + iv)[::-1]
    incoming = _ctr(hashlib.sha256(reversed_pair[:32] + secret).digest(),
                    reversed_pair[32:48])
    return out, incoming


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("соединение закрыто")
        data += chunk
    return data


def mtproto_exchange(port: int, secret_hex: str, timeout: float = 20.0) -> int:
    """Отправляет req_pq_multi и возвращает конструктор ответа (ожидаем resPQ)."""
    secret = bytes.fromhex(secret_hex)
    init, key, iv = build_init(secret, dc=2)
    prekey, ctr_iv = init[8:40], init[40:56]
    out_cipher, in_cipher = client_ciphers(prekey, ctr_iv, secret)

    body = REQ_PQ_MULTI + os.urandom(16)          # req_pq_multi + nonce:int128
    msg_id = (int(time.time()) << 32) & ~1
    message = struct.pack("<Q", 0) + struct.pack("<Q", msg_id) + \
        struct.pack("<I", len(body)) + body
    assert len(message) % 4 == 0
    frame = bytes([len(message) // 4]) + message   # abridged-кадр

    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(init)
        sock.sendall(out_cipher.update(frame))
        # В obfuscated2 шифруется весь поток, включая байт длины кадра.
        head = in_cipher.update(_recv_exactly(sock, 1))
        length = head[0]
        if length >= 0x7F:
            more = in_cipher.update(_recv_exactly(sock, 3))
            length = struct.unpack("<I", b"\x00" + more)[0]
        payload = in_cipher.update(_recv_exactly(sock, length * 4))

    auth_key_id, _srv_msg_id, body_len = struct.unpack("<QQI", payload[:20])
    if auth_key_id != 0:
        raise AssertionError(f"ожидался ответ без ключа (auth_key_id=0), получено {auth_key_id}")
    if body_len + 20 > len(payload):
        raise AssertionError("ответ короче заявленной длины")
    return struct.unpack("<I", payload[20:24])[0]


@unittest.skipUnless(NET, "нужна сеть: включите FREECONNECT_TG_NET=1")
class TestTelegramEndToEnd(unittest.TestCase):
    def test_req_pq_gets_respq_through_our_proxy(self):
        proxy = tgproxy.TgProxy()
        port = proxy.start(port=0, secret=os.urandom(16).hex(),
                           dc_ips={"2": "149.154.167.220"},
                           cfproxy=True, h2=True, pool_size=2)
        try:
            constructor = mtproto_exchange(port, proxy.secret())
        finally:
            proxy.stop()
        self.assertEqual(constructor, RES_PQ,
                         f"ожидался resPQ (0x{RES_PQ:08x}), получено 0x{constructor:08x}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
