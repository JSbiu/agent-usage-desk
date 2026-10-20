"""Native Messaging framing: UTF-8 JSON preceded by a 32-bit byte count."""
import argparse
import json
import os
from pathlib import Path
import struct
import sys

from .service import Service
from .store import encode

MAX_FRAME = 1024 * 1024 - 1


def read_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise EOFError("不完整的消息")
        chunks.extend(chunk)
    return bytes(chunks)


def read_frame(stream):
    first = stream.read(4)
    if not first:
        return None
    header = first + read_exact(stream, 4 - len(first))
    size = struct.unpack("<I", header)[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError("消息超过大小限制。")
    return json.loads(read_exact(stream, size))


def write_frame(stream, value):
    data = encode(value).encode("utf-8")
    if len(data) > MAX_FRAME:
        data = encode({"id": value.get("id"), "ok": False,
                       "error": "返回数据过大，请缩小范围或使用分页。"}).encode("utf-8")
    stream.write(struct.pack("<I", len(data)) + data)
    stream.flush()


def serve(service, source, target):
    while True:
        message = read_frame(source)
        if message is None:
            return
        identifier = message.get("id") if isinstance(message, dict) else None
        try:
            if not isinstance(message, dict):
                raise ValueError("消息必须为对象。")
            def progress(value):
                write_frame(target, {"id": identifier, "type": "progress", "progress": value})
            data = service.request(message.get("op"), message.get("params"), progress)
            write_frame(target, {"id": identifier, "ok": True, "data": data})
        except (ValueError, RuntimeError, OSError) as exc:
            write_frame(target, {"id": identifier, "ok": False, "error": str(exc)})
        except Exception as exc:
            print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
            write_frame(target, {"id": identifier, "ok": False, "error": "本地读取器出错：" + type(exc).__name__})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("origin", nargs="?")
    parser.add_argument("--parent-window", default=None)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--once-json", help="内部调试：单次 JSON 请求")
    args = parser.parse_args()
    service = Service(args.cache_dir, args.home)
    if args.once_json:
        message = json.loads(args.once_json)
        print(encode(service.request(message["op"], message.get("params"))))
        return
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    try:
        serve(service, sys.stdin.buffer, sys.stdout.buffer)
    except (EOFError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)


if __name__ == "__main__":
    main()
