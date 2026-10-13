"""TCP relay: listen on 0.0.0.0:<listen_port>, forward every connection to the proxy in <upstream_url> (host:port taken from it).

The session's proxy listens on 127.0.0.1, which a container on a bridge network cannot reach. A container can reach this
relay through host.docker.internal instead. It is a plain byte pipe, so CONNECT tunnels and credentials in the proxy URL
work unchanged.

    python3 proxy_relay.py http://user:pass@127.0.0.1:42223 42224
    python3 proxy_relay.py --rewrite http://user:pass@127.0.0.1:42223 host.docker.internal 42224   # prints the container-side URL
"""
import asyncio
import sys
from urllib.parse import urlsplit


def rewrite(url: str, host: str, port: int) -> str:
    u = urlsplit(url)
    auth = u.netloc.rsplit("@", 1)[0] + "@" if "@" in u.netloc else ""
    return f"{u.scheme}://{auth}{host}:{port}"


async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        w.close()


async def main(upstream: str, listen_port: int):
    u = urlsplit(upstream)

    async def handle(cr, cw):
        try:
            ur, uw = await asyncio.open_connection(u.hostname, u.port)
        except OSError:
            cw.close()
            return
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))

    server = await asyncio.start_server(handle, "0.0.0.0", listen_port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    if sys.argv[1] == "--rewrite":
        print(rewrite(sys.argv[2], sys.argv[3], int(sys.argv[4])))
    else:
        asyncio.run(main(sys.argv[1], int(sys.argv[2])))
